"""Committed film thickness precision and zero-layer physics contracts."""
from copy import deepcopy
from typing import Any, cast

import numpy as np
import pytest
from PySide6.QtGui import QGuiApplication

from coat_sim.core.film_design import load_design, save_design, validate_design
from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable,
    optimize_film_design,
)
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_target_sampling import _snapshot


def _variable_rows(controller: FilmOptimizationQuickController) -> list[dict[str, Any]]:
    """The optimizer variable rows, re-read from the live Qt Property each call."""
    return cast(list[dict[str, Any]], controller.variables)


def test_zero_layer_matches_absent_layer_and_survives_save(tmp_path):
    service, _library, design = fixture(tmp_path)
    zero = deepcopy(design)
    zero["front_layers"][0]["thickness"]["value"] = 0.0
    assert validate_design(zero)["front_layers"][0]["thickness"]["value"] == 0
    absent = deepcopy(design)
    absent["front_layers"] = []
    settings = {"wavelength_min_nm": 500.0, "wavelength_max_nm": 600.0, "samples": 7}
    actual, reference = service.simulate(zero, settings), service.simulate(absent, settings)
    for key in ("R", "T", "A"):
        np.testing.assert_allclose(actual[key], reference[key], rtol=0, atol=1e-12)
    path = tmp_path / "zero.cfilm"
    save_design(path, zero)
    assert load_design(path)["front_layers"][0]["thickness"]["value"] == 0
    zero["front_layers"][0]["thickness"]["value"] = -0.001
    with pytest.raises(ValueError):
        validate_design(zero)


def test_redundant_layer_reaches_zero_without_deleting_its_id(tmp_path):
    service, _library, design = fixture(tmp_path)
    removed = deepcopy(design)
    removed["front_layers"] = []
    settings = {"wavelength_min_nm": 500.0, "wavelength_max_nm": 600.0, "samples": 7}
    target = service.simulate(removed, settings)
    layer_id = design["front_layers"][0]["id"]
    objectives = tuple(
        FilmOptimizationObjective(pointwise=FilmObjectiveConstraint(mode="value", target_value=float(target["R"][index]), lower_bound=None, upper_bound=None), wavelength_min_nm=500.0 + index * (100.0 / 6.0), wavelength_max_nm=500.0 + index * (100.0 / 6.0), wavelength_samples=1, metric="R")
        for index in range(7)
    )
    result = optimize_film_design(service, FilmOptimizationRequest(
        design_snapshot=design, design_revision=1,
        variables=(FilmThicknessVariable("front", layer_id, 0.0, None),),
        objectives=objectives, acceptable_normalized_error=1e-10,
        max_iterations=100, max_evaluations=300,
    ))
    assert result.state == "completed"
    assert result.best_thickness_nm[layer_id] == 0.0
    assert result.best_design["front_layers"][0]["id"] == layer_id
    assert len(result.best_design["front_layers"]) == 1
    actual = service.simulate(result.best_design, settings)
    for metric in ("R", "T", "A"):
        np.testing.assert_allclose(actual[metric], target[metric], rtol=0, atol=1e-12)


def test_user_thickness_edits_commit_three_decimals_without_rewriting_history(tmp_path):
    service, _library, design = fixture(tmp_path)
    design["front_layers"][0]["thickness"]["value"] = 100.123456
    session = FilmDesignEditSession(service)
    assert session.apply_optimized_design(design, session.snapshot().design_revision)
    layer_id = design["front_layers"][0]["id"]
    assert session.snapshot().design["front_layers"][0]["thickness"]["value"] == 100.123456
    assert session.set_layer_value("front", layer_id, "thickness.nm", "127.1236")
    assert session.snapshot().design["front_layers"][0]["thickness"] == {"value": 127.124, "unit": "nm"}
    assert session.set_layer_value("front", layer_id, "thickness.nm", "0")
    assert session.snapshot().design["front_layers"][0]["thickness"]["value"] == 0
    assert not session.set_layer_value("front", layer_id, "thickness.nm", "-0.1")
    assert not session.set_layer_value("front", layer_id, "thickness.nm", "1e100")


def test_optimization_defaults_select_all_without_invented_upper_and_preserve_bounds(tmp_path):
    QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    second = deepcopy(design["front_layers"][0])
    second["id"] = "second-layer"
    second["thickness"]["value"] = 0.0
    design["front_layers"].append(second)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        rows = _variable_rows(controller)
        assert len(rows) == 2 and all(row["enabled"] and row["available"] for row in rows)
        assert all(row["lower_nm"] == 0 and row["upper_nm"] is None for row in rows)
        assert controller.canStart
        assert controller._build_request().variables[0].upper_nm is None
        assert controller.setSolver("powell")
        assert not controller.canStart and "上界" in cast(str, controller.statusText)
        assert controller.setSolver("baseline")
        assert controller.canStart
        configuration = controller.exportOptimizationConfiguration()
        configuration["variables"][0].update(lower_nm=12.5, upper_nm=180.0)
        assert controller.loadOptimizationConfiguration(configuration)
        assert _variable_rows(controller)[0]["lower_nm"] == 12.5
        assert _variable_rows(controller)[0]["upper_nm"] == 180.0
        assert _variable_rows(controller)[1]["upper_nm"] is None
        assert controller.setVariableBound("front", "second-layer", "upper_nm", "200.1236")
        assert _variable_rows(controller)[1]["upper_nm"] == 200.124
        assert not controller.setVariableBound("front", "second-layer", "upper_nm", "1e100")
        assert controller.setVariableBound("front", "second-layer", "upper_nm", "200.124")
        assert controller._build_request().variables == (
            FilmThicknessVariable("front", "existing-stable-layer", 12.5, 180.0),
            FilmThicknessVariable("front", "second-layer", 0.0, 200.124),
        )
    finally:
        controller.shutdown(1000)


def test_zero_initial_layer_runs_default_half_unbounded_solver(tmp_path):
    QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    design["front_layers"][0]["thickness"]["value"] = 0.0
    layer_id = design["front_layers"][0]["id"]
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        assert controller.canStart
        controller._objectives[0].update(
            wavelength_min_nm=550.0, wavelength_max_nm=550.0,
            wavelength_samples=1)
        assert controller.setObjectiveField("pointwise.lower_bound_percent", "0")
        assert controller.setObjectiveField("pointwise.upper_bound_percent", "0")
        controller._max_iterations = 8
        controller._max_evaluations = 20
        request = controller._build_request()
    finally:
        controller.shutdown(1000)
    assert request.solver == "baseline"
    assert request.objectives[0].pointwise.mode == "value"
    assert request.objectives[0].pointwise.target_value == 0.0
    assert request.variables == (FilmThicknessVariable("front", layer_id, 0.0, None),)
    from coat_sim.core.film_optimization_job import _validate_replay_request
    _validate_replay_request(request)
    settings = {"wavelength_min_nm": 550.0, "wavelength_max_nm": 550.0, "samples": 1}
    baseline_r = float(service.simulate(design, settings)["R"][0])
    assert baseline_r > request.acceptable_normalized_error
    result = optimize_film_design(service, request)
    assert result.state == "completed"
    assert result.evaluations > 1
    assert result.best_design["front_layers"][0]["thickness"]["value"] > 0
    assert result.max_normalized_error < baseline_r
    from dataclasses import replace
    with pytest.raises(ValueError, match="explicit upper_nm"):
        optimize_film_design(service, replace(request, solver="powell"))
    bounded = replace(request, variables=(FilmThicknessVariable("front", layer_id, 0.0, 200.0),))
    result = optimize_film_design(service, bounded)
    assert result.state == "completed"
    gradient_result = optimize_film_design(service, replace(bounded, solver="lbfgsb"))
    assert gradient_result.state == "completed"
    assert gradient_result.best_thickness_nm[layer_id] >= 0
    from coat_sim.core.film_optimization_derivatives import simulate_with_thickness_jacobian
    settings = {"wavelength_min_nm": 550.0, "wavelength_max_nm": 550.0, "samples": 1}
    frozen = service.freeze_design(design, settings)
    derivative = simulate_with_thickness_jacobian(
        design, frozen, bounded.variables, settings)
    epsilon = 0.001
    near_zero = deepcopy(design)
    near_zero["front_layers"][0]["thickness"]["value"] = epsilon
    forward_slope = (float(service.simulate(near_zero, settings)["R"][0]) - baseline_r) / epsilon
    jacobian_at_zero = float(np.asarray(derivative["jacobian_R"])[0, 0])
    assert np.isfinite(jacobian_at_zero)
    assert jacobian_at_zero == pytest.approx(forward_slope, abs=1e-5)
    from threading import Event
    from coat_sim.core.film_optimization_job import (
        execute_film_optimization_job, freeze_job_payload, validate_checkpoint,
    )
    checkpoints: list[dict[str, Any]] = []
    queued = execute_film_optimization_job(
        service, freeze_job_payload(service, request), cancel_event=Event(),
        checkpoint_callback=checkpoints.append)
    assert queued["state"] == "completed"
    assert queued["candidate"]["design"]["front_layers"][0]["thickness"]["value"] > 0
    assert checkpoints
    validate_checkpoint(service, checkpoints[-1])


def test_feasible_zero_initial_layer_completes_once_without_changing_optics(tmp_path):
    QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    design["front_layers"][0]["thickness"]["value"] = 0.0
    layer_id = design["front_layers"][0]["id"]
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    try:
        controller._objectives[0].update(
            wavelength_min_nm=550.0, wavelength_max_nm=550.0, wavelength_samples=1)
        request = controller._build_request()
    finally:
        controller.shutdown(1000)
    objective = request.objectives[0]
    assert objective.pointwise.mode == "range"
    assert (objective.pointwise.lower_bound, objective.pointwise.upper_bound) == (0.0, 0.05)
    settings = {"wavelength_min_nm": 550, "wavelength_max_nm": 550, "samples": 1}
    before = service.simulate(design, settings)
    assert 0.0 <= float(before["R"][0]) <= 0.05
    result = optimize_film_design(service, request)
    assert result.state == "completed" and result.evaluations == 1
    assert result.max_normalized_error <= request.acceptable_normalized_error
    assert result.best_thickness_nm[layer_id] == 0.0
    assert result.best_design["front_layers"][0]["id"] == layer_id
    after = service.simulate(result.best_design, settings)
    for metric in ("R", "T", "A"):
        np.testing.assert_allclose(after[metric], before[metric], rtol=0, atol=1e-12)


@pytest.fixture
def controller_with_unavailable_layer(tmp_path):
    QGuiApplication.instance() or QGuiApplication([])
    service, _library, design = fixture(tmp_path)
    available = deepcopy(design["front_layers"][0])
    available["id"] = "available-zero-layer"
    available["thickness"]["value"] = 0.0
    design["front_layers"].append(available)
    source = _snapshot(service, design)
    source.layers_by_side["front"][0]["physicalNmValue"] = None
    controller = FilmOptimizationQuickController(source)
    try:
        yield controller, design["front_layers"][0]["id"], available["id"]
    finally:
        controller.shutdown(1000)


def test_defaults_select_available_layers_and_keep_unavailable_layer_visible_unselected(
        controller_with_unavailable_layer):
    controller, unavailable_id, available_id = controller_with_unavailable_layer
    unavailable, available = _variable_rows(controller)
    assert [unavailable["layer_id"], available["layer_id"]] == [unavailable_id, available_id]
    assert not unavailable["enabled"] and not unavailable["available"]
    assert unavailable["physical_nm"] is None
    assert available["enabled"] and available["available"]
    assert available["physical_nm"] == 0.0
    request = controller._build_request()
    assert [variable.layer_id for variable in request.variables] == [available_id]
    assert [layer["id"] for layer in request.design_snapshot["front_layers"]] == [
        unavailable_id, available_id]


def test_unavailable_layer_cannot_be_manually_enabled_as_a_variable(controller_with_unavailable_layer):
    controller, unavailable_id, available_id = controller_with_unavailable_layer
    controller.setVariableEnabled("front", unavailable_id, True)
    unavailable = _variable_rows(controller)[0]
    assert unavailable["layer_id"] == unavailable_id
    assert not unavailable["enabled"] and not unavailable["available"]
    assert [variable.layer_id for variable in controller._build_request().variables] == [available_id]


def test_restored_selected_unavailable_layer_blocks_request_until_deselected(controller_with_unavailable_layer):
    controller, unavailable_id, available_id = controller_with_unavailable_layer
    saved = controller.exportOptimizationConfiguration()
    next(row for row in saved["variables"] if row["layer_id"] == unavailable_id)["enabled"] = True
    assert controller.loadOptimizationConfiguration(saved)
    unavailable = _variable_rows(controller)[0]
    assert unavailable["enabled"] and not unavailable["available"]
    assert "不可用" in str(controller.property("statusText"))
    assert "可取消选择该层" in str(controller.property("statusText"))
    with pytest.raises(ValueError, match="不可用"):
        controller._build_request()
    controller.setVariableEnabled("front", unavailable_id, False)
    assert not _variable_rows(controller)[0]["enabled"]
    assert [variable.layer_id for variable in controller._build_request().variables] == [available_id]
