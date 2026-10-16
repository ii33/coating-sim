"""Isolated checks for typed structure requests, the real queue worker, and UI wiring.

Run with ``.conda/python.exe -m pytest -p no:cacheprovider
tests/test_film_structure_queue_ui.py``. The queue test invokes the production
``scheduler.worker.run`` in a child process and uses only temporary fixture data.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

if TYPE_CHECKING:
    from coat_sim.ui.compute_target import ComputeTarget

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from coat_sim.core.film_optimization import (
    FilmOptimizationRequest,
    FilmOptimizationObjective,
    FilmThicknessVariable,
    optimize_film_design,
)
from coat_sim.core.film_optimization_structure import (
    FilmNeedleMaterialReference,
    FilmStructureOptimizationRequest,
    FilmStructureOptimizationResult,
    film_structure_request_to_payload,
    freeze_film_structure_optimization_request,
)
from coat_sim.scheduler.jobs import canonical, freeze_spec
from coat_sim.scheduler.result_capability import is_film_optimization_result
from coat_sim.scheduler.store import SchedulerStore
from coat_sim.ui.film_optimization_quick_controller import (
    FilmOptimizationQuickController,
    FilmOptimizationSourceSnapshot,
)
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController

# Rebuilt input fixture only: the original independent acceptance file is absent.
# Its unknown algorithm coverage remains a documented gap.
from tests.film_structure_fixtures import fixture as _fixture, request as _request
from tests.film_performance_fixtures import fixture as in_memory_service_fixture


def _structure_request(design):
    objective = FilmOptimizationObjective(
        wavelength_min_nm=550.0,
        wavelength_max_nm=550.1,
        wavelength_samples=2,
        angles_deg=(0.0,),
        polarization="unpolarized",
        metric="R",
        mode="range",
        lower_bound=0.0,
        upper_bound=0.02,
    )
    base = _request(design, [objective], bounds=(100.0, 200.0),
                    max_iterations=2, max_evaluations=80)
    return FilmStructureOptimizationRequest(
        base_request=base,
        candidate_materials=(FilmNeedleMaterialReference(
            "acceptance-film", "film-set"),),
        mode="combined",
        max_total_thickness_nm=500.0,
        min_thickness_nm=0.5,
        max_thickness_nm=200.0,
        max_total_layers=8,
        max_added_layers=1,
        position_samples=2,
        candidate_budget=8,
        needle_thickness_samples=3,
        growth_cycles=1,
        seed=17,
    )


def test_structure_request_freezes_only_explicit_candidates_and_rejects_bad_limits(tmp_path):
    service, _library, design = _fixture(tmp_path)
    request = _structure_request(design)
    frozen = freeze_film_structure_optimization_request(service, request)
    payload = film_structure_request_to_payload(frozen)
    assert payload["kind"] == "coat_sim.film_structure_optimization_request"
    assert "solver" not in payload and "solver_id" not in payload
    assert payload["candidate_materials"] == [{
        "material_id": "acceptance-film", "optical_set_id": "film-set"}]
    snapshot = payload["base_request"]["frozen_material_snapshot"]
    assert snapshot["sha256"]
    captured_ids = {row["id"] for row in snapshot["materials"]}
    assert captured_ids == {"acceptance-glass", "acceptance-film"}

    invalid = FilmStructureOptimizationRequest(
        base_request=request.base_request,
        candidate_materials=request.candidate_materials,
        max_total_layers=4097,
    )
    with pytest.raises((TypeError, ValueError)):
        freeze_film_structure_optimization_request(service, invalid)


def test_real_queue_worker_replays_structure_request_and_writes_typed_candidate(tmp_path):
    service, _library, design = _fixture(tmp_path)
    request = _structure_request(design)
    spec = freeze_spec(service, "film_optimization", {"request": request})
    payload = spec["parameters"]["payload"]
    assert payload["request"]["kind"] == "coat_sim.film_structure_optimization_request"
    assert "solver" not in payload["request"] and "solver_id" not in payload["request"]

    store = SchedulerStore(tmp_path)
    code = (
        "from coat_sim.scheduler.worker import run; import sys; "
        "raise SystemExit(run(sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4]))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(ROOT), str(TESTS), env.get("PYTHONPATH", ""))))
    evidence: dict[str, object] = {"runs": []}
    artifact_dir = ROOT / "tests/artifacts/film-structure-queue-ui"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = artifact_dir / "worker-evidence.json"
    runs: list[dict[str, object]] = []
    for _ in range(2):
        job_id = uuid4().hex
        owner_instance = uuid4().hex
        directory = tmp_path / "artifacts/runtime/scheduler/jobs" / job_id
        directory.mkdir(parents=True)
        store.submit(spec, job_id=job_id)
        (directory / "spec.json").write_text(canonical(spec), encoding="utf-8")
        (directory / "launch.json").write_text(
            json.dumps({"instance": owner_instance}), encoding="utf-8")

        completed = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), job_id,
             str(os.getpid()), owner_instance],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        run_evidence: dict[str, object] = {
            "job_id": job_id,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        runs.append(run_evidence)
        evidence["runs"] = runs
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr

        outcome = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
        run_evidence["outcome"] = outcome
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        assert outcome["state"] == "succeeded", outcome
        result = outcome["result"]
        assert is_film_optimization_result(result)
        assert result["kind"] == "coat_sim.film_structure_optimization_result"
        assert result["state"] == "completed"
        assert result["partial_result"]["is_final"] is True
        assert result["partial_result"]["candidate_verified"] is True
        assert result["partial_result"]["validation_complete"] is True
        assert result["candidate"]["design_revision"] == request.base_request.design_revision
        assert result["structure"]["mode"] == "combined"
        assert result["structure"]["topology_audit"]
        assert any(row["stage"] in {
            "needle_after_growth_sensitivity", "growth_escape",
        } for row in result["structure"]["trace"])
        assert result["structure"]["counters"]["candidate_count"] > 0

        invalid_final = json.loads(json.dumps(result))
        invalid_final["partial_result"]["best_so_far"] = True
        assert not is_film_optimization_result(invalid_final)
        invalid_quality_flag = json.loads(json.dumps(result))
        invalid_quality_flag["partial_result"]["validation_complete"] = False
        assert not is_film_optimization_result(invalid_quality_flag)

        checkpoint = json.loads(
            (directory / "best-candidate.json").read_text(encoding="utf-8"))
        run_evidence["checkpoint"] = checkpoint
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        assert is_film_optimization_result(checkpoint["result"])
        assert checkpoint["result"]["kind"] == "coat_sim.film_structure_optimization_result"
        assert checkpoint["result"]["partial_result"]["candidate_verified"] is True
        assert checkpoint["result"]["partial_result"]["is_final"] is False
        invalid_checkpoint = json.loads(json.dumps(checkpoint["result"]))
        invalid_checkpoint["partial_result"]["best_so_far"] = False
        assert not is_film_optimization_result(invalid_checkpoint)
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    assert len({row["job_id"] for row in runs}) == 2


def test_structure_controls_reject_invalid_values_and_show_explicit_materials(tmp_path):
    service, _library, design = _fixture(tmp_path)
    layer = design["front_layers"][0]
    source = FilmOptimizationSourceSnapshot(
        service=service,
        design=design,
        design_revision=19,
        path="",
        layers_by_side={"front": [{
            "id": layer["id"], "physicalNmValue": layer["thickness"]["value"],
            "name": "Synthetic acceptance layer"}], "back": []},
    )
    controller = FilmOptimizationQuickController(source)
    try:
        controller.setSearchMode("structure")
        options = controller.structureMaterialOptions
        assert any(row["material_id"] == "acceptance-film" for row in options)
        assert controller.setStructureMaterialSelected(
            "acceptance-film", "film-set", True)
        assert any(row["material_id"] == "acceptance-film" and row["selected"]
                   for row in controller.structureMaterialOptions)
        original_dmin = controller.structureOptions["min_thickness_nm"]
        assert not controller.setStructureOption("min_thickness_nm", "nan")
        assert controller.structureOptions["min_thickness_nm"] == original_dmin
        controller.setStructureOption("min_thickness_nm", 300.0)
        assert controller.structureConfigurationError
        assert not controller.startOptimization()
    finally:
        controller.shutdown(1000)
        controller.deleteLater()


def test_structure_window_source_exposes_sidebar_and_no_footer_status_controls():
    qml = (ROOT / "coat_sim/ui/qml/FilmOptimizationWindow.qml").read_text(encoding="utf-8")
    assert 'objectName: "filmOptimizationStructureOptions"' in qml
    assert 'objectName: "filmOptimizationStructureMaterialList"' in qml
    assert 'objectName: "filmStructureDmaxField"' in qml
    assert 'objectName: "filmOptimizationToolbar"' in qml
    assert 'objectName: "filmOptimizationStartButton"' in qml
    assert 'objectName: "filmOptimizationCancelButton"' in qml
    assert 'objectName: "filmOptimizationApplyButton"' in qml
    assert 'objectName: "filmOptimizationSaveButton"' in qml
    assert 'objectName: "filmOptimizationDetailsButton"' not in qml
    assert 'objectName: "filmOptimizationSidebarToggle"' in qml
    assert 'ResponsiveOperationBar {' in qml
    assert 'objectName: "filmOptimizationTargetsTab"' in qml
    assert 'objectName: "filmOptimizationConstraintsTab"' in qml
    assert 'objectName: "filmOptimizationAlgorithmTab"' in qml
    assert 'objectName: "filmOptimizationSetupContent"' in qml
    assert 'objectName: "filmOptimizationDetailsSidebar"' in qml
    assert 'ThemeSidebar {' in qml
    assert 'window.contentTopMargin' in qml
    assert 'visible: window.controller.searchMode === "fixed_topology"' in qml
    assert 'id="filmOptimizationFooterStatus"' not in qml
    assert "contentWidth: availableWidth" in qml
    assert "ScrollBar.AlwaysOff" in qml


def test_structure_window_offscreen_renders_controls_and_sidebar(tmp_path):
    import shiboken6

    app = QGuiApplication.instance() or QGuiApplication([])
    from coat_sim.ui.fonts import bundled_font_family
    from coat_sim.ui.theme_service import ThemeService

    font_family = bundled_font_family()
    assert font_family, "bundled Noto CJK font was not registered"
    app.setFont(QFont(font_family))
    theme = ThemeService.instance()
    theme.configure(mode="light", font_scale=1.0, density_scale=1.0)
    engine = QQmlEngine()
    theme.attach_engine(engine)
    engine.addImportPath(str(ROOT / "coat_sim/ui/qml"))

    service, _library, design = _fixture(tmp_path)
    layer = design["front_layers"][0]
    controller = FilmOptimizationQuickController(FilmOptimizationSourceSnapshot(
        service=service,
        design=design,
        design_revision=19,
        path="",
        layers_by_side={"front": [{
            "id": layer["id"], "physicalNmValue": layer["thickness"]["value"],
            "name": "Synthetic acceptance layer"}], "back": []},
    ))
    controller.setSearchMode("structure")
    window = None
    component = QQmlComponent(engine)
    try:
        component.loadUrl(QUrl.fromLocalFile(str(
            ROOT / "coat_sim/ui/qml/FilmOptimizationWindow.qml")))
        assert component.status() != QQmlComponent.Error, "\n".join(
            error.toString() for error in component.errors())
        window = component.createWithInitialProperties({"controller": controller})
        assert isinstance(window, QQuickWindow), "\n".join(
            error.toString() for error in component.errors())
        window.resize(1100, 760)
        window.setVisible(True)
        QTest.qWait(250)
        app.processEvents()

        def click_item(item):
            point = item.mapToScene(
                QPointF(item.width() / 2, item.height() / 2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, point)
            app.processEvents()

        def find_visual_item(parent, object_name):
            if parent.objectName() == object_name:
                return parent
            for child in parent.childItems():
                found = find_visual_item(child, object_name)
                if found is not None:
                    return found
            return None

        toolbar = window.findChild(QQuickItem, "filmOptimizationToolbar")
        assert toolbar is not None and toolbar.isVisible()
        for name, expected_text in (
                ("filmOptimizationStartButton", "开始优化"),
                ("filmOptimizationCancelButton", "取消"),
                ("filmOptimizationApplyButton", "应用"),
                ("filmOptimizationSaveButton", "另存候选")):
            button = window.findChild(QQuickItem, name)
            assert button is not None and button.isVisible(), name
            assert button.property("text") == expected_text
            origin = button.mapToItem(toolbar, QPointF(0, 0))
            end = button.mapToItem(toolbar, QPointF(button.width(), button.height()))
            assert origin.x() >= 0 and origin.y() >= 0, name
            assert end.x() <= toolbar.width() + 1, (name, end.x(), toolbar.width())
            assert end.y() <= toolbar.height() + 1, name

        details_button = window.findChild(QQuickItem, "filmOptimizationSidebarToggle")
        sidebar = window.findChild(QQuickItem, "filmOptimizationDetailsSidebar")
        assert sidebar is not None and not sidebar.isVisible()
        click_item(details_button)
        assert window.property("detailsExpanded") is True
        assert sidebar.isVisible()
        click_item(details_button)
        assert window.property("detailsExpanded") is False
        assert not sidebar.isVisible()
        click_item(details_button)
        assert window.property("detailsExpanded") is True
        assert sidebar.isVisible()

        setup_content = window.findChild(QQuickItem, "filmOptimizationSetupContent")
        targets_tab = window.findChild(QQuickItem, "filmOptimizationTargetsTab")
        constraints_tab = window.findChild(QQuickItem, "filmOptimizationConstraintsTab")
        algorithm_tab = window.findChild(QQuickItem, "filmOptimizationAlgorithmTab")
        assert setup_content is not None
        assert targets_tab is not None and constraints_tab is not None and algorithm_tab is not None
        assert setup_content.property("currentIndex") == 0
        objective_list = window.findChild(QQuickItem, "filmOptimizationObjectiveList")
        wavelength = window.findChild(QQuickItem, "filmOptimizationWavelengthMinField")
        target_button = window.findChild(QQuickItem, "computeTargetButton")
        assert objective_list is not None and objective_list.isVisible()
        assert wavelength is not None and wavelength.isVisible()
        assert target_button is not None and target_button.isVisible()

        click_item(constraints_tab)
        assert setup_content.property("currentIndex") == 1
        variable_scroll = window.findChild(QQuickItem, "filmOptimizationVariableScroll")
        assert variable_scroll is not None and variable_scroll.isVisible()
        lower_bound = find_visual_item(
            variable_scroll, f"filmOptimizationLower_{layer['id']}")
        upper_bound = find_visual_item(
            variable_scroll, f"filmOptimizationUpper_{layer['id']}")
        assert lower_bound is not None and lower_bound.isVisible()
        assert upper_bound is not None and upper_bound.isVisible()

        constraints_output = ROOT / "tests/artifacts/film-structure-queue-ui/constraints-mode.png"
        constraints_output.parent.mkdir(parents=True, exist_ok=True)
        constraints_image = window.grabWindow()
        assert not constraints_image.isNull()
        assert constraints_image.save(str(constraints_output))
        assert constraints_image.width() == 1100 and constraints_image.height() == 760

        click_item(algorithm_tab)
        assert setup_content.property("currentIndex") == 2
        structure = window.findChild(QQuickItem, "filmOptimizationStructureOptions")
        material_list = window.findChild(QQuickItem, "filmOptimizationStructureMaterialList")
        dmax = window.findChild(QQuickItem, "filmStructureDmaxField")
        scroll = window.findChild(QQuickItem, "filmOptimizationSolverOptionsScroll")
        options_content = window.findChild(
            QQuickItem, "filmOptimizationSolverOptionsContent")
        horizontal_scrollbar = window.findChild(
            QQuickItem, "filmOptimizationOptionsHorizontalScrollBar")
        assert structure is not None and structure.isVisible()
        assert material_list is not None and material_list.isVisible()
        assert dmax is not None and dmax.isVisible()
        assert scroll is not None and options_content is not None
        assert options_content.x() >= 3.0
        assert abs(options_content.width() + options_content.x() * 2
                   - scroll.property("availableWidth")) < 1.0
        assert horizontal_scrollbar is not None and not horizontal_scrollbar.isVisible()
        target_popup = window.findChild(QObject, "computeTargetPopup")
        assert target_button is not None and target_popup is not None
        assert not target_popup.property("visible")
        click_item(target_button)
        app.processEvents()
        assert target_popup.property("visible")
        assert window.findChild(QQuickItem, "computeHostSelector") is not None
        assert window.findChild(QQuickItem, "computeDeviceSelector") is not None
        QTest.keyClick(window, Qt.Key.Key_Escape)
        app.processEvents()
        assert not target_popup.property("visible")

        output = ROOT / "tests/artifacts/film-structure-queue-ui/structure-mode.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        image = window.grabWindow()
        assert not image.isNull()
        assert image.save(str(output))
        assert image.width() == 1100 and image.height() == 760

    finally:
        if window is not None and shiboken6.isValid(window):
            window.setProperty("allowClose", True)
            cast(QQuickWindow, window).hide()
            shiboken6.delete(window)
        controller.shutdown(1000)
        cast("ComputeTarget", controller.computeTarget)._executor.shutdown(
            wait=True, cancel_futures=True)
        shiboken6.delete(controller)
        shiboken6.delete(component)
        shiboken6.delete(engine)
        app.processEvents()


def test_structure_result_application_requires_final_holdout_and_current_source_token(tmp_path):
    service, _library, design = _fixture(tmp_path)
    request = _structure_request(design).base_request
    completed = optimize_film_design(service, request)
    assert completed.state == "completed"
    assert completed.validation_complete

    layer = design["front_layers"][0]

    class Session:
        def __init__(self):
            self.design = design
            self.design_revision = request.design_revision
            self.path = ""

        def snapshot(self):
            return SimpleNamespace(
                design_revision=self.design_revision,
                design=self.design,
                path=self.path,
            )

    class Source:
        def __init__(self, session):
            self.service = service
            self.session = session
            self.pendingEditEpoch = 7
            self.applied = []

        def applyOptimizedDesign(self, candidate, revision, edit_epoch):
            self.applied.append((candidate, revision, edit_epoch))
            if (revision != self.session.design_revision
                    or edit_epoch != self.pendingEditEpoch):
                return False
            self.session.design = candidate
            self.session.design_revision += 1
            return True

    def make_controller():
        snapshot = FilmOptimizationSourceSnapshot(
            service=service,
            design=design,
            design_revision=request.design_revision,
            path="",
            layers_by_side={"front": [{
                "id": layer["id"],
                "physicalNmValue": layer["thickness"]["value"],
                "name": "Synthetic acceptance layer",
            }], "back": []},
        )
        controller = FilmOptimizationQuickController(snapshot)
        session = Session()
        source = Source(session)
        controller.session = session
        controller._source_controller = source
        controller._detached_source = False
        controller._source_revision = request.design_revision
        controller._operation_edit_epoch = source.pendingEditEpoch
        controller._operation_serial = 1
        controller._active_operation_id = 1
        controller._busy = True
        return controller, source, session

    def structure_result(base_result, *, is_final=True):
        return SimpleNamespace(
            base_result=base_result,
            topology_audit=(),
            is_final=is_final,
        )

    try:
        controller, source, _session = make_controller()
        controller._on_finished(1, structure_result(completed, is_final=False), [], None)
        assert controller.hasCandidate and not controller.canApply
        assert not controller.applyCandidate()
        assert source.applied == []
        controller.shutdown(1000)
        controller.deleteLater()

        controller, source, _session = make_controller()
        incomplete = replace(completed, validation_complete=False)
        controller._on_finished(1, structure_result(incomplete), [], None)
        assert controller.hasCandidate and not controller.canApply
        assert source.applied == []
        controller.shutdown(1000)
        controller.deleteLater()

        for terminal_error in (
                {"state": "cancelled", "message": "cancelled"},
                {"state": "failed", "message": "failed"}):
            controller, source, _session = make_controller()
            controller._on_finished(1, structure_result(completed), [], terminal_error)
            assert controller.hasCandidate and not controller.canApply
            assert source.applied == []
            controller.shutdown(1000)
            controller.deleteLater()

        controller, source, _session = make_controller()
        controller._operation_serial = 2
        controller._active_operation_id = 2
        controller._on_finished(1, structure_result(completed), [], None)
        assert controller._active_operation_id == 2
        assert not controller.hasCandidate
        assert source.applied == []
        controller._on_finished(2, structure_result(completed), [], None)
        assert len(source.applied) == 1
        controller.shutdown(1000)
        controller.deleteLater()

        controller, source, session = make_controller()
        session.design_revision += 1
        controller._on_finished(1, structure_result(completed), [], None)
        assert controller.candidateStale and not controller.canApply
        assert source.applied == []
        controller.shutdown(1000)
        controller.deleteLater()

        controller, source, _session = make_controller()
        source.pendingEditEpoch += 1
        controller._on_finished(1, structure_result(completed), [], None)
        assert controller.candidateStale and not controller.canApply
        assert source.applied == []
        controller.shutdown(1000)
        controller.deleteLater()

        controller, source, session = make_controller()
        controller._on_finished(1, structure_result(completed), [], None)
        assert len(source.applied) == 1
        assert source.applied[0][1:] == (request.design_revision, 7)
        assert session.design_revision == request.design_revision + 1
        controller.shutdown(1000)
        controller.deleteLater()

        # Fixed-topology completion keeps its existing completed-state gate.
        controller, source, session = make_controller()
        controller._on_finished(1, completed, [], None)
        assert len(source.applied) == 1
        assert session.design_revision == request.design_revision + 1
        controller.shutdown(1000)
        controller.deleteLater()
    finally:
        QCoreApplication.processEvents()


def test_final_completion_applies_to_real_source_and_refreshes_simulation_once(tmp_path):
    app = QGuiApplication.instance() or QGuiApplication([])
    service, design, frozen = in_memory_service_fixture(layers=4)

    class MemoryMaterialLibrary:
        def __init__(self, rows):
            self.rows = {str(row["id"]): dict(row) for row in rows}

        def get(self, identifier):
            from copy import deepcopy
            return deepcopy(self.rows[str(identifier)])

        def _records(self, material_ids=None):
            from copy import deepcopy
            rows = (self.rows.values() if material_ids is None else
                    (self.rows[str(identifier)] for identifier in material_ids))
            return [deepcopy(row) for row in rows]

        def options_many(self, purposes):
            from copy import deepcopy
            result = {purpose: [] for purpose in purposes}
            for row in self.rows.values():
                purpose = "substrate" if row["id"] == "glass" else "film"
                if purpose in result:
                    result[purpose].append(deepcopy(row))
            return result

    library_rows = [dict(row, design_ready=True)
                    for row in frozen["materials"]]
    service.materials = MemoryMaterialLibrary(library_rows)
    simulation_calls = []
    simulate = service.simulate

    def count_simulation(candidate, settings=None, *, cancel_event=None):
        simulation_calls.append(settings)
        return simulate(candidate, settings, cancel_event=cancel_event)

    service.simulate = count_simulation
    settings = QSettings(str(tmp_path / "film-source-geometry.ini"),
                         QSettings.Format.IniFormat)
    source = FilmDesignQuickController(
        root=tmp_path, service=service, geometry_settings=settings)
    simulation = None
    optimization = None
    try:
        initial = source.session.snapshot()
        assert source.session.apply_optimized_design(design, initial.design_revision)
        app.processEvents()
        base = source.session.snapshot()

        simulation = FilmSimulationQuickController(
            root=tmp_path, service=service, source_controller=source)
        assert simulation.setSimulationSettings({
            "wavelength_min_nm": 500.0,
            "wavelength_max_nm": 501.0,
            "samples": 2,
            "angle_deg": 0.0,
            "polarization": "unpolarized",
        })
        optimization = FilmOptimizationQuickController(source)
        objective = FilmOptimizationObjective(
            500.0, 501.0, 2, metric="R", mode="range",
            lower_bound=0.0, upper_bound=1.0,
        )
        request = FilmOptimizationRequest(
            design_snapshot=base.design,
            design_revision=base.design_revision,
            variables=(FilmThicknessVariable("front", "layer-0", 45.0, 120.0),),
            objectives=(objective,),
            max_iterations=2,
            max_evaluations=8,
            initial_step_fraction=0.25,
            min_step_fraction=1e-4,
            acceptable_normalized_error=0.01,
            frozen_material_snapshot=frozen,
            solver="baseline",
            seed=2,
            validation_samples_multiplier=2,
        )
        completed = optimize_film_design(service, request)
        assert completed.state == "completed"
        assert completed.targets_met and completed.validation_complete
        assert completed.design_revision == base.design_revision
        # Exercise the structure completion route with the real verified base
        # result. The typed wrapper carries no invented score or topology data.
        structure_completed = FilmStructureOptimizationResult(
            base_result=completed,
            mode="combined",
            trace=(),
            counters={},
            topology_audit=(),
            candidate_verified=True,
            is_final=True,
        )

        simulation_calls.clear()
        delivered_results = []

        def record_result_delivery():
            if (simulation._result is not None and not simulation._busy
                    and not simulation._stale
                    and (not delivered_results
                         or simulation._result is not delivered_results[-1])):
                delivered_results.append(simulation._result)

        simulation.stateChanged.connect(record_result_delivery)
        generation_before_apply = simulation._generation
        configuration_before_apply = source.session.snapshot().design.get("workflow_settings", {})
        optimization._operation_serial = 1
        optimization._active_operation_id = 1
        optimization._operation_edit_epoch = source.pendingEditEpoch
        optimization._source_revision = base.design_revision
        optimization._busy = True
        optimization._on_finished(1, structure_completed, [], None)

        applied = source.session.snapshot()
        assert applied.design_revision == base.design_revision + 1
        assert applied.dirty
        assert optimization._structure_result is structure_completed
        expected_design = dict(structure_completed.base_result.best_design)
        expected_design["workflow_settings"] = configuration_before_apply
        assert applied.design == expected_design
        assert simulation._source_design_revision == applied.design_revision
        assert simulation._design == expected_design
        assert simulation._generation == generation_before_apply + 1

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            QCoreApplication.processEvents()
            if (simulation._result is not None and not simulation._busy
                    and not simulation._stale):
                break
            time.sleep(0.01)

        assert simulation._result is not None, simulation._error or simulation._status
        assert not simulation._busy and not simulation._stale
        assert simulation._result["wavelength_nm"] == pytest.approx([500.0, 501.0])
        assert simulation_calls == [simulation._current_settings()]
        assert len(delivered_results) == 1
        assert delivered_results[0] is simulation._result
    finally:
        if optimization is not None:
            optimization.shutdown(1000)
            optimization.deleteLater()
        if simulation is not None:
            simulation.close()
            simulation.deleteLater()
        source.deleteLater()
        QCoreApplication.processEvents()

