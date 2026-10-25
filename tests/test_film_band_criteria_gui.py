"""Real Qt editing and small physical submissions for both band reductions."""
from copy import deepcopy
import json

import pytest
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_bounds_edit_review import bounds_gui as bounds_gui
from tests.test_film_optimization_restore_layout import optimizer
from tests.test_film_synthesis_gui_review import _Scene, _wait

pytestmark = [pytest.mark.qt, pytest.mark.contract]


def _type(scene, name, literal, *, key=Qt.Key.Key_Tab):
    field = scene.item(name)
    scene.click(field)
    QTest.keyClick(scene.window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(scene.window, Qt.Key.Key_Backspace)
    for character in literal:
        QTest.keyClick(scene.window, Qt.Key(ord(character.upper())))
    if key is not None:
        QTest.keyClick(scene.window, key)
    QTest.qWait(20)
    assert field.property("text") == literal
    return field


def _both(scene, controller):
    _type(scene, "filmOptimizationUpperBoundField", "100")
    scene.click(scene.item("filmOptimizationMeanEnabledCheck"))
    assert controller._objectives[0]["mean"]["enabled"] is True


def test_current_nested_configuration_defaults_validation_and_exact_value(tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        configuration = controller.exportOptimizationConfiguration()
        assert configuration["version"] == 2
        band = configuration["objectives"][0]
        assert band["pointwise"]["enabled"] is True
        assert band["pointwise"]["lower_bound_percent"] == 0
        assert band["pointwise"]["upper_bound_percent"] == 5
        assert band["mean"]["enabled"] is False
        assert not {"mode", "target_value_percent", "lower_bound_percent", "upper_bound_percent"} & set(band)
        identity = band["id"]
        for mutate in (lambda row: row.update(version=1),
                       lambda row: row["objectives"][0].update(lower_bound_percent=1),
                       lambda row: row["objectives"][0]["mean"].update(enabled=1)):
            bad = deepcopy(configuration)
            mutate(bad)
            assert not controller.loadOptimizationConfiguration(bad)
        assert controller.loadOptimizationConfiguration(configuration)
        assert controller.setObjectiveField("pointwise.upper_bound_percent", "12.5")
        assert controller.setObjectiveField("pointwise.lower_bound_percent", "12.5")
        criterion = controller._build_request().objectives[0].pointwise
        assert criterion.mode == "value" and criterion.target_value == .125
        assert criterion.lower_bound is None and criterion.upper_bound is None
        assert controller._objectives[0]["id"] == identity
        assert controller.setObjectiveConstraintEnabled("pointwise", False)
        assert not controller.canStart
        with pytest.raises(ValueError, match="at least one"):
            controller._build_request()


def test_qml_independent_bounds_tab_shift_tab_disabled_partial_save_reopen(bounds_gui, tmp_path):
    _service, source, controller, scene = bounds_gui("fixed_topology")
    _both(scene, controller)
    lower = _type(scene, "filmOptimizationMeanLowerBoundField", "1.2500")
    upper = scene.item("filmOptimizationMeanUpperBoundField")
    assert upper.hasActiveFocus()
    _type(scene, "filmOptimizationMeanUpperBoundField", "2.7500", key=None)
    QTest.keyClick(scene.window, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
    QTest.qWait(20)
    assert lower.hasActiveFocus()
    band = controller._build_request().objectives[0]
    assert (band.mean.lower_bound, band.mean.upper_bound) == (.0125, .0275)
    assert (band.pointwise.lower_bound, band.pointwise.upper_bound) == (0, 1)
    assert _type(scene, "filmOptimizationMeanLowerBoundField", "1e").hasActiveFocus() is False
    assert controller.objectiveBoundsError and not controller.canStart
    with pytest.raises(ValueError):
        controller._build_request()
    scene.click(scene.item("filmOptimizationMeanEnabledCheck"))
    assert not lower.isEnabled() and lower.property("text") == "1e"
    assert controller.canStart, controller.error
    saved = controller.exportOptimizationConfiguration()
    assert saved["objectives"][0]["mean"]["bounds_drafts"] == {"lower_bound_percent": "1e"}
    assert controller._build_request().objectives[0].mean.enabled is False
    path = tmp_path / "two-band-criteria.cfilm"
    assert source.saveFilePath(str(path))
    assert json.loads(path.read_text(encoding="utf-8"))["workflow_settings"]["optimization"] == saved
    scene.window.close()
    QTest.qWait(20)
    assert not scene.window.isVisible()
    _service, _reopened, restored, reopened = bounds_gui("fixed_topology", path=path)
    literal = reopened.item("filmOptimizationMeanLowerBoundField")
    assert not literal.isEnabled() and literal.property("text") == "1e"
    assert restored.canStart, restored.error
    reopened.click(reopened.item("filmOptimizationMeanEnabledCheck"))
    assert literal.isEnabled() and literal.property("text") == "1e"
    with pytest.raises(ValueError):
        restored._build_request()
    _type(reopened, "filmOptimizationMeanLowerBoundField", "1.25")
    assert restored.canStart, restored.error
    assert restored._build_request().objectives[0].mean.lower_bound == .0125


def test_qml_group_drafts_and_focus_survive_objective_switch(tmp_path):
    service, _library, design = fixture(tmp_path)
    with optimizer(service, design) as (controller, _source, _session):
        scene = _Scene(controller, "FilmOptimizationWindow.qml")
        try:
            _both(scene, controller)
            controller.addObjective()
            assert len(controller.objectives) == 2
            controller.selectObjective(0)
            field = _type(scene, "filmOptimizationMeanLowerBoundField", "1e", key=None)
            assert field.hasActiveFocus()
            # changed notifications update text bindings without recreating the field.
            for _ in range(3):
                controller.changed.emit()
                QTest.qWait(5)
                assert scene.item("filmOptimizationMeanLowerBoundField") is field
                assert field.hasActiveFocus() and field.property("text") == "1e"
            for index in (1, 0):
                listing = scene.item("filmOptimizationObjectiveList")
                scene.click(scene.visual_item(listing, f"itemAtIndex({index})"))
                assert controller._selected_objective == index
            assert scene.item("filmOptimizationMeanLowerBoundField").property("text") == "1e"
            with pytest.raises(ValueError):
                controller._build_request()
            scene.click(scene.item("filmOptimizationMeanEnabledCheck"))
            assert controller._build_request().objectives[0].mean.enabled is False
        finally:
            scene.close()


@pytest.mark.parametrize("mode", ["fixed_topology", "synthesis"])
@pytest.mark.parametrize("mean_only", [False, True])
def test_real_qml_small_physical_submission_and_band_results(bounds_gui, mean_only, mode):
    service, _source, controller, scene = bounds_gui(mode)
    _both(scene, controller)
    # Seven supported physical wavelength points; no forward-model mocks.
    assert controller.setObjectiveField("wavelength_min_nm", 450)
    assert controller.setObjectiveField("wavelength_max_nm", 690)
    assert controller.setObjectiveField("wavelength_samples", 7)
    assert controller.setSolverOption("max_iterations", 1)
    assert controller.setSolverOption("max_evaluations", 3)
    if mode == "synthesis":
        assert controller.setSynthesisOption("max_cycles", 1)
        assert controller.setSynthesisOption("max_evaluations", 3)
    bound = None
    if mean_only and mode == "fixed_topology":
        spectra = [service.simulate(controller._design, {
            "wavelength_min_nm": 450, "wavelength_max_nm": 690, "samples": count,
            "angle_deg": 0, "polarization": "unpolarized"}) for count in (7, 13)]
        means = [float(np.trapezoid(row["R"], row["wavelength_nm"]) / 240) for row in spectra]
        peak = min(max(row["R"]) for row in spectra)
        bound = (max(means) + peak) / 2
        assert max(means) < bound < peak
        _type(scene, "filmOptimizationUpperBoundField", format(bound * 100, ".17g"))
        _type(scene, "filmOptimizationMeanUpperBoundField", format(bound * 100, ".17g"))
    if mean_only:
        scene.click(scene.item("filmOptimizationEnabledCheck"))
    assert controller.canStart, controller.error
    scene.click(scene.item("filmOptimizationStartButton"))
    _wait(lambda: bool(controller.queueJobId) or bool(controller.error) and not controller.busy, seconds=10)
    assert controller.queueJobId, controller.error
    job_id = controller.queueJobId
    spec_path = service.root / "artifacts/runtime/scheduler/jobs" / job_id / "spec.json"
    _wait(spec_path.exists, seconds=10)
    payload = json.loads(spec_path.read_text(encoding="utf-8"))["parameters"]["payload"]
    envelope = payload["request"]
    assert envelope["schema_version"] == (5 if mode == "synthesis" else 2)
    frozen = envelope["request"] if mode == "synthesis" else envelope
    assert len(frozen["objectives"]) == 1
    assert frozen["objectives"][0]["mean"]["enabled"] is True
    assert frozen["objectives"][0]["pointwise"]["enabled"] is (not mean_only)
    _wait(lambda: not controller.busy, seconds=30)
    assert controller._result is not None, controller.error
    assert controller._result.validation_complete
    assert controller._result.targets_met
    assert controller.plotData["wavelength_nm"] == [450, 490, 530, 570, 610, 650, 690]
    assert "平均值：通过" in controller.objectiveCriteriaSummary
    assert ("绝对值（逐点）：未启用" if mean_only else "绝对值（逐点）：通过") in controller.objectiveCriteriaSummary
    assert controller.conditionErrors
    if mean_only:
        assert all(row.reduction == "mean" and row.wavelength_nm is None
                   for row in controller._result.per_condition_errors)
        assert all(row["reduction"] == "mean" for row in controller.conditionErrors)
        assert controller.plotData["lower_bound"] is None
        assert controller._result.validation_targets_met is True
        if bound is not None:
            assert max(controller.plotData["candidate"]) > bound
            assert max(controller._result.validation_raw_coverage[0].actual_values) > bound
