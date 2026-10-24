"""Independent real text-entry checks for optimizer target-bound drafts."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, cast

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QSettings, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_optimization import freeze_film_optimization_request
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from tests.film_structure_fixtures import fixture as optical_fixture
from tests.test_film_synthesis_gui_review import _Scene, _wait
from tests.test_film_synthesis_gui_usability_review import _choose_combo

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def bounds_gui(tmp_path):
    app = QGuiApplication.instance() or QGuiApplication([])
    _service, _library, design = optical_fixture(tmp_path)
    service = FilmDesignService(tmp_path)
    sources: list[FilmDesignQuickController] = []
    controllers: list[FilmOptimizationQuickController] = []
    scenes: list[_Scene] = []

    def create(mode="synthesis", *, path=None):
        source = FilmDesignQuickController(service=service,
            geometry_settings=QSettings(str(tmp_path / f"bounds-{len(sources)}.ini"), QSettings.Format.IniFormat))
        sources.append(source)
        assert source.initializationReady and source.session is not None
        if path is None:
            initial = deepcopy(design)
            if mode == "synthesis":
                initial["front_layers"] = []
            assert source.session.apply_optimized_design(initial, source.session.snapshot().design_revision)
        else:
            assert source.session.request_open_file(str(path))
        controller = FilmOptimizationQuickController(source)
        controllers.append(controller)
        _wait(lambda: not controller._initial_loading)
        scene = _Scene(controller, "FilmOptimizationWindow.qml")
        scenes.append(scene)
        if path is None:
            scene.click(scene.item("filmOptimizationAlgorithmTab"))
            _choose_combo(scene, "filmOptimizationSearchModeBox", mode)
            if mode == "synthesis":
                for row in cast(list[dict[str, Any]], controller.property("structureMaterialOptions")):
                    assert controller.setStructureMaterialSelected(row["material_id"], row["optical_set_id"],
                        row["material_id"] == "acceptance-film" and row["optical_set_id"] == "film-set")
            scene.click(scene.item("filmOptimizationTargetsTab"))
            scene.edit(scene.item("filmOptimizationWavelengthMinField"), "550")
            scene.edit(scene.item("filmOptimizationWavelengthMaxField"), "550")
        else:
            scene.click(scene.item("filmOptimizationTargetsTab"))
        _wait(lambda: cast(QObject, controller.property("computeTarget")).property("canSubmit"))
        return service, source, controller, scene

    yield create
    for controller in controllers:
        controller.cancelOptimization()
        controller.shutdown(5000)
    for source in sources:
        source.markSourceClosed()
    for scene in reversed(scenes):
        scene.close()
    for controller in controllers:
        controller.deleteLater()
    for source in sources:
        source.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    client = SchedulerClient(tmp_path)
    if client.endpoint.exists():
        client.stop()
    assert app is not None


def _type(scene: _Scene, field_name: str, text: str, *, commit="enter", unchanged_error=None):
    field = scene.item("filmOptimization" + field_name + "Field")
    scene.click(field)
    QTest.keyClick(scene.window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(scene.window, Qt.Key.Key_Backspace)
    assert field.property("text") == ""
    typed = ""
    for character in text:
        QTest.keyClick(scene.window, Qt.Key(ord(character.upper())))
        typed += character
        QTest.qWait(5)
        assert field.property("text") == typed
        if unchanged_error is not None:
            assert cast(QObject, scene.window.property("controller")).property("error") == unchanged_error
    if commit == "enter":
        QTest.keyClick(scene.window, Qt.Key.Key_Return)
    elif commit == "blur":
        scene.click(scene.item("filmOptimizationWeightField"))
    QTest.qWait(30)
    assert field.property("text") == text
    return field


def _bounds(controller):
    row = cast(dict[str, Any], controller.property("selectedObjective"))
    return float(row["pointwise"]["lower_bound_percent"]), float(row["pointwise"]["upper_bound_percent"])


def _select_target(scene: _Scene, index: int):
    item = scene.visual_item(scene.item("filmOptimizationObjectiveList"), f"itemAtIndex({index})")
    assert isinstance(item, QQuickItem)
    scene.click(item)


def _assert_no_old_request(service, controller, scene):
    jobs = service.root / "artifacts/runtime/scheduler/jobs"
    before = set(jobs.glob("*/spec.json"))
    assert not controller.property("canStart")
    button = scene.item("filmOptimizationStartButton")
    point = button.mapToScene(QPointF(button.width() / 2., button.height() / 2.)).toPoint()
    QTest.mouseClick(scene.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    assert controller.startOptimization() is False
    assert controller.startOptimizationFromShortcut() is False
    assert not controller.property("busy")
    assert set(jobs.glob("*/spec.json")) == before
    assert str(controller.property("error")).strip()
    with pytest.raises(ValueError):
        controller._build_synthesis_request()
    with pytest.raises(ValueError):
        controller.exportOptimizationConfiguration()


@pytest.mark.parametrize("direction", ["up", "down"])
def test_reversed_intermediate_pair_is_not_clamped_and_other_endpoint_can_repair(bounds_gui, direction):
    _service, _source, controller, scene = bounds_gui()
    if direction == "up":
        _type(scene, "LowerBound", "12.75")
        assert str(controller.property("error"))
        _type(scene, "UpperBound", "12.9", commit="blur")
        expected = (12.75, 12.9)
    else:
        _type(scene, "UpperBound", "20")
        _type(scene, "LowerBound", "12")
        _type(scene, "UpperBound", "0.2", commit="blur")
        assert str(controller.property("error"))
        _type(scene, "LowerBound", "0.1")
        expected = (.1, .2)
    assert _bounds(controller) == expected
    assert controller.property("canStart"), controller.property("error")
    objective = freeze_film_synthesis_request(_service, controller._build_synthesis_request()).objectives[0]
    assert objective.pointwise.lower_bound == expected[0] / 100.
    assert objective.pointwise.upper_bound == expected[1] / 100.


@pytest.mark.parametrize("draft", ["", "1e", "-0.25", "101"])
def test_invalid_enter_and_blur_retain_raw_text_and_never_submit_old_valid_bounds(bounds_gui, draft):
    service, _source, controller, scene = bounds_gui()
    scene.click(scene.item("filmOptimizationAddObjectiveButton"))
    scene.edit(scene.item("filmOptimizationWavelengthMinField"), "550")
    scene.edit(scene.item("filmOptimizationWavelengthMaxField"), "550")
    _select_target(scene, 0)
    assert not controller.property("error")
    _type(scene, "LowerBound", draft, commit="none", unchanged_error="")
    QTest.keyClick(scene.window, Qt.Key.Key_Return)
    QTest.qWait(30)
    field = scene.item("filmOptimizationLowerBoundField")
    assert field.property("text") == draft
    assert str(controller.property("error"))
    scene.click(scene.item("filmOptimizationWeightField"))
    assert field.property("text") == draft
    warning = scene.item("filmOptimizationTargetBoundsWarning")
    assert warning.isVisible() and str(warning.property("text")).strip()
    _assert_no_old_request(service, controller, scene)
    _select_target(scene, 1)
    _assert_no_old_request(service, controller, scene)
    _select_target(scene, 0)
    assert scene.item("filmOptimizationLowerBoundField").property("text") == draft
    if draft == "1e":
        _type(scene, "WavelengthMin", "600", commit="blur")
        assert scene.item("filmOptimizationWavelengthMinField").property("text") == "600"
    _type(scene, "LowerBound", "0")
    if draft == "1e":
        # Repairing target bounds cannot silently clear a separate bad wavelength pair.
        _assert_no_old_request(service, controller, scene)
        assert scene.item("filmOptimizationWavelengthMinField").property("text") == "600"
        _type(scene, "WavelengthMax", "600", commit="blur")
    assert _bounds(controller) == (0., 5.)
    assert controller.property("canStart"), controller.property("error")


def test_target_switch_and_actual_file_reopen_keep_focused_pair_in_configuration(bounds_gui, tmp_path):
    service, source, controller, scene = bounds_gui()
    _type(scene, "UpperBound", "20")
    _type(scene, "LowerBound", "12.34567", commit="blur")
    assert scene.item("filmOptimizationLowerBoundField").property("text") == "12.34567"
    _type(scene, "LowerBound", "12.7500", commit="blur")
    assert scene.item("filmOptimizationLowerBoundField").property("text") == "12.7500"
    scene.click(scene.item("filmOptimizationAddObjectiveButton"))
    scene.edit(scene.item("filmOptimizationWavelengthMinField"), "550")
    scene.edit(scene.item("filmOptimizationWavelengthMaxField"), "550")
    _type(scene, "LowerBound", "2.125")
    _type(scene, "UpperBound", "3.75", commit="none")
    _select_target(scene, 0)
    assert _bounds(controller) == (12.75, 20.)
    assert scene.item("filmOptimizationLowerBoundField").property("text") == "12.7500"
    _select_target(scene, 1)
    assert _bounds(controller) == (2.125, 3.75)
    _type(scene, "UpperBound", "3.875", commit="none")
    with pytest.raises(ValueError):
        controller.exportOptimizationConfiguration()
    scene.click(scene.item("filmOptimizationWeightField"))
    configuration = controller.exportOptimizationConfiguration()
    assert configuration["objectives"][1]["pointwise"]["upper_bound_percent"] == 3.875
    path = tmp_path / "actual-bounds.cfilm"
    assert source.saveFilePath(str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["workflow_settings"]["optimization"]["objectives"][1]["pointwise"]["upper_bound_percent"] == 3.875
    _service, _reopened, restored, restored_scene = bounds_gui(path=path)
    assert restored.exportOptimizationConfiguration() == configuration
    _select_target(restored_scene, 1)
    assert _bounds(restored) == (2.125, 3.875)
    request = freeze_film_synthesis_request(service, restored._build_synthesis_request())
    assert request.objectives[0].pointwise.lower_bound == .1275
    assert request.objectives[1].pointwise.upper_bound == .03875
    assert restored.property("canStart"), restored.property("error")


@pytest.mark.parametrize("mode", ["fixed_topology", "synthesis"])
@pytest.mark.parametrize("target", ["0", "12.5"])
def test_zero_and_equal_bounds_freeze_as_exact_value_in_ordinary_and_n0_modes(bounds_gui, mode, target):
    service, _source, controller, scene = bounds_gui(mode)
    _type(scene, "UpperBound", target, commit="blur")
    _type(scene, "LowerBound", target)
    assert _bounds(controller) == (float(target), float(target))
    assert controller.property("canStart"), controller.property("error")
    if mode == "synthesis":
        request = freeze_film_synthesis_request(service, controller._build_synthesis_request())
        assert request.environment["front_layers"] == request.environment["back_layers"] == []
        assert request.seed_designs == ()
        objective = request.objectives[0]
    else:
        ordinary = freeze_film_optimization_request(service, controller._build_request())
        assert len(ordinary.design_snapshot["front_layers"]) == 1
        assert len(ordinary.variables) == 1
        objective = ordinary.objectives[0]
    assert objective.pointwise.mode == "value"
    assert objective.pointwise.target_value == float(target) / 100.
    assert objective.pointwise.lower_bound is None and objective.pointwise.upper_bound is None


@pytest.mark.parametrize("mode", ["fixed_topology", "synthesis"])
def test_real_gui_start_completes_with_edited_bounds_in_typed_queue(bounds_gui, mode, request):
    service, _source, controller, scene = bounds_gui(mode)
    # A broad acceptance interval isolates text delivery and actual dispatch.
    # It makes no antireflection performance claim. Default budgets stay intact.
    _type(scene, "UpperBound", "12.5", commit="blur")
    _type(scene, "LowerBound", "0")
    assert controller.property("canStart"), controller.property("error")
    scene.click(scene.item("filmOptimizationStartButton"))
    _wait(lambda: bool(controller.queueJobId), seconds=10.)
    job_id = controller.queueJobId
    path = service.root / "artifacts/runtime/scheduler/jobs" / job_id / "spec.json"
    _wait(path.exists, seconds=10.)
    spec = json.loads(path.read_text(encoding="utf-8"))
    payload = spec["parameters"]["payload"]
    frozen = payload["request"]["request"] if mode == "synthesis" else payload["request"]
    objective = frozen["objectives"][0]
    assert objective["pointwise"]["mode"] == "range"
    assert objective["pointwise"]["lower_bound"] == 0. and objective["pointwise"]["upper_bound"] == .125
    assert objective["wavelength_min_nm"] == objective["wavelength_max_nm"] == 550.
    assert spec["executor"] == ("film_synthesis" if mode == "synthesis" else "film_optimization")
    if mode == "synthesis":
        assert frozen["environment"]["front_layers"] == frozen["environment"]["back_layers"] == []
        assert frozen["seed_designs"] == []
        assert frozen["candidate_materials"] == [
            {"material_id": "acceptance-film", "optical_set_id": "film-set"}]
    else:
        assert len(frozen["design_snapshot"]["front_layers"]) == 1
        assert len(frozen["variables"]) == 1
    _wait(lambda: not controller.property("busy"), seconds=30.)
    if mode == "synthesis":
        result = controller._synthesis_payload
        assert result is not None, controller.property("error")
        assert result["result"]["state"] == "completed", result
        assert result["result"]["targets_met"] is True
        assert result["best_candidate_audit"]["targets_met"] is True
    else:
        completed = controller._result
        assert completed is not None, controller.property("error")
        assert completed.state == "completed", completed
        assert completed.targets_met is True
    request.node.user_properties.append(("real_queue_evidence", json.dumps({
        "job_id": job_id, "executor": spec["executor"], "objective": objective,
        "state": "completed", "targets_met": True, "default_budgets_unchanged": True,
        "purpose": "bounds input and dispatch; no antireflection performance claim",
    }, ensure_ascii=False)))
