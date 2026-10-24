"""Synthesis configuration is an N0 path in the existing optimization page."""
from copy import deepcopy

import pytest
from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication

from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_restore_layout import optimizer, window_runtime

pytestmark = pytest.mark.qt


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def test_n0_configuration_roundtrip_and_cpu_target_keep_source_unchanged(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    design["front_layers"] = []
    original = deepcopy(design)
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode("synthesis")
        assert controller.computeTarget.task == "film_synthesis"
        assert controller.variables == []
        for row in controller.structureMaterialOptions:
            assert controller.setStructureMaterialSelected(row["material_id"], row["optical_set_id"], True)
        request = controller._build_synthesis_request()
        assert request.environment["front_layers"] == [] and request.seed_designs == ()
        assert request.max_cycles is None and request.max_evaluations is None
        assert request.objectives[0].pointwise.upper_bound == pytest.approx(.05)
        assert controller.setSynthesisOption("max_cycles", "3")
        saved = controller.exportOptimizationConfiguration()
        assert saved["synthesis"]["options"]["max_cycles"] == 3
        assert session.snapshot().design == original
        assert controller.setSearchMode("fixed_topology")
        assert controller.computeTarget.task == "film_optimization"
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.searchMode == "synthesis" and controller.computeTarget.task == "film_synthesis"
        assert controller.synthesisOptions["max_evaluations"] is None
        with window_runtime(app, controller) as (window, warnings):
            assert window.findChild(QObject, "filmSynthesisOptions") is not None
            assert "自动合成" in window.title()
            assert not any("ReferenceError" in row or "TypeError" in row for row in warnings), warnings


def test_explicit_seed_and_invalid_budget_never_change_original(app, tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, session):
        assert controller.setSearchMode("synthesis")
        assert controller.setSynthesisUseCurrentSeed(True)
        request = controller._build_synthesis_request()
        assert request.seed_designs[0]["front_layers"] == design["front_layers"]
        before = session.snapshot()
        assert not controller.setSynthesisOption("max_layers", "2.5")
        assert not controller.canStart
        assert controller.setSynthesisOption("max_layers", "")
        assert controller.synthesisOptions["max_layers"] is None
        assert session.snapshot().design == before.design
        assert session.snapshot().design_revision == before.design_revision


def test_n0_uses_explicit_project_purpose_projection_without_role_fields(app, tmp_path):
    from coat_sim.core.project_film_service import ProjectOpticalSnapshot
    service, _library, design = fixture(tmp_path)
    design["front_layers"] = []
    records = [service.materials.get(identity) for identity in ("acceptance-film", "acceptance-glass")]
    records = [{key: row[key] for key in ("id", "name", "optical_sets", "design_ready", "notes") if key in row}
               for row in records]
    service.materials = ProjectOpticalSnapshot(records, usage={
        "acceptance-film": ("film",), "acceptance-glass": ("substrate",)})
    assert all("roles" not in row and "category" not in row for row in service.materials._records())
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode("synthesis")
        rows = controller.structureMaterialOptions
        assert [(row["material_id"], row["optical_set_id"]) for row in rows] == [("acceptance-film", "film-set")]
        assert not rows[0]["selected"]
        assert controller.setStructureMaterialSelected("acceptance-film", "film-set", True)
        saved = controller.exportOptimizationConfiguration()
        assert controller.setSearchMode("fixed_topology")
        assert controller.loadOptimizationConfiguration(saved)
        assert controller.structureMaterialOptions[0]["selected"]
        request = controller._build_synthesis_request()
        assert request.candidate_materials[0].material_id == "acceptance-film"


def test_synthesis_progress_reports_backend_layers_heartbeat_and_stall_flags(app):
    from coat_sim.core.film_optimization import FilmOptimizationProgress, FilmOptimizationRequest
    from coat_sim.scheduler.desktop import RemoteCancelEvent
    from coat_sim.ui.film_optimization_quick_controller import _FilmSynthesisTask
    request = FilmOptimizationRequest({}, 0, (), ())
    worker = _FilmSynthesisTask(None, request, None, "private-test", RemoteCancelEvent(None, "private-test"), 7)
    messages: list[FilmOptimizationProgress] = []
    worker.signals.progress.connect(lambda _serial, item: messages.append(item))
    worker._publish_queue_progress({"stage": "audit_incumbent", "layer_count": 3,
        "evaluations": 12, "cycle": 1, "status": "audited best"})
    worker._publish_queue_progress({"stage": "audit_progress", "layer_count": 4,
        "evaluations": 14, "cycle": 2, "status": "17 physical evaluations, 121 wavelength points",
        "heartbeat_sequence": 8, "algorithm_progress_age_seconds": 31.2,
        "worker_heartbeat_age_seconds": 0., "stalled": True, "unresponsive": False})
    assert len(messages) == 2
    last = messages[-1]
    assert last.iteration == 2 and last.evaluations == 14
    assert "当前报告层数 4" in last.message and "已审计最好层数 3" in last.message
    assert "heartbeat 8" in last.message and "algorithm_progress_age_seconds=31.2" in last.message
    assert "stalled=True" in last.message and "unresponsive=False" in last.message
    assert "17 physical evaluations, 121 wavelength points" in last.message


def test_angle_preparation_keeps_separate_inactive_editable_closure(app, tmp_path):
    from threading import Event
    from coat_sim.core.materials import default_material_profile
    from coat_sim.ui.film_optimization_quick_controller import _AngleGridPreparationTask, _PreparedAngleGrid
    from tests.test_film_optimization_needle import _optical
    service, library, design = fixture(tmp_path)
    optical = _optical("inactive-set", 2.8)
    optical["k"] = None
    material = default_material_profile("Inactive exit", id="inactive-exit")
    material.update(category="other", roles=["medium"], optical_constants=[optical])
    library.write(material)
    design["exit_medium"] = {"kind": "material", "material_id": "inactive-exit", "optical_set_id": "inactive-set"}
    with optimizer(service, design) as (controller, _source, _session):
        assert controller.setSearchMode("synthesis")
        assert controller.setObjectiveField("wavelength_min_nm", 550.)
        assert controller.setObjectiveField("wavelength_max_nm", 550.)
        request = controller._build_synthesis_request(prepared_angles=((0.,),))
        worker = _AngleGridPreparationTask(tuple(deepcopy(controller._objectives)), request,
                                          "synthesis", service, Event(), 3)
        results: list[tuple[object, object]] = []
        worker.signals.finished.connect(lambda _serial, result, _baseline, error: results.append((result, error)))
        worker.run()
        assert len(results) == 1 and results[0][1] is None, results
        prepared = results[0][0]
        assert isinstance(prepared, _PreparedAngleGrid)
        assert prepared.request.frozen_material_snapshot is not None
        assert "inactive-exit" not in {row["id"] for row in prepared.request.frozen_material_snapshot["materials"]}
        assert prepared.editable_snapshot is not None
        captured = next(row for row in prepared.editable_snapshot["materials"] if row["id"] == "inactive-exit")
        assert captured["optical_sets"][0]["n"] == [2.8, 2.8]
        assert captured["optical_sets"][0]["k"] is None
        optical["n"] = [9., 9.]
        material["optical_constants"] = [optical]
        library.write(material, update=True)
        assert captured["optical_sets"][0]["n"] == [2.8, 2.8]
