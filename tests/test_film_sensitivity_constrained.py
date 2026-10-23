"""Physical scene derivatives and exact nominal-preserving acceptance contract."""
from copy import deepcopy
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from coat_sim.core import film_sensitivity as sensitivity
from coat_sim.core import film_sensitivity_constrained as constrained
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmThicknessVariable
from tests.test_film_sensitivity import problem

pytestmark = pytest.mark.algorithm


def feasible_problem(*, objective="finite_scenario_expected", samples=1):
    service, request = problem(objective=objective, operation="optimize")
    measured = sensitivity.analyze_film_sensitivity(service, replace(request, operation="analyze"))
    assert measured.best_analysis is not None
    actual = measured.best_analysis.nominal_conditions[0].actual
    objective_row = replace(request.objectives[0], upper_bound=actual + .0001,
        wavelength_min_nm=550., wavelength_max_nm=550. if samples == 1 else 560., wavelength_samples=samples)
    return service, replace(request, objectives=(objective_row,),
        errors=(sensitivity.FilmThicknessError("low-film", absolute_nm=5., relative_fraction=.01),),
        optimization_policy="nominal_preserving", max_evaluations=40, max_iterations=30)


@pytest.mark.parametrize("objective", ["finite_scenario_expected", "finite_scenario_worst"])
def test_physical_constrained_search_improves_in_few_cloud_evaluations_and_keeps_all_nominal_samples(objective):
    service, request = feasible_problem(objective=objective)
    checkpoints: list[dict[str, Any]] = []
    result = sensitivity.optimize_film_sensitivity(service, request, checkpoint_callback=checkpoints.append)
    assert result.state == "completed", result.message
    assert result.termination_reason == "constrained_tolerance"
    assert result.improved and result.nominal_targets_met
    assert result.evaluations < 17  # no old 17-point-per-axis startup
    for payload in checkpoints + [sensitivity.film_sensitivity_result_to_payload(result)]:
        checked = sensitivity.film_sensitivity_result_from_payload(
            sensitivity.validate_film_sensitivity_result_payload(payload, expected_request=result.frozen_request))
        assert checked.nominal_targets_met
        assert checked.best_analysis is not None
        assert all(row.actual <= request.objectives[row.objective_index].upper_bound
                   for row in checked.best_analysis.nominal_conditions)


@pytest.mark.parametrize("metric,polarization", [("R", "s"), ("R", "p"), ("T", "unpolarized")])
@pytest.mark.parametrize("affected", ["both", "selected_only", "unselected_only"])
def test_scene_analytic_chain_jacobian_matches_physical_finite_difference(metric, polarization, affected):
    service, request = problem(two=True, objective="finite_scenario_expected", operation="optimize")
    errors: tuple[sensitivity.FilmThicknessError, ...] = (
        sensitivity.FilmThicknessError("low-film", absolute_nm=1.2, relative_fraction=.02),
        sensitivity.FilmThicknessError("high-film", magnitude=.01, unit="relative_fraction", absolute_nm=.4),
    )
    if affected == "selected_only":
        errors = errors[1:]
    elif affected == "unselected_only":
        errors = errors[:1]
    request = replace(request, errors=errors, variables=(request.variables[1],),
        scenarios=(sensitivity.FilmSensitivityScenario("scene", tuple(.7 if i == 0 else -.6 for i in range(len(errors)))),),
        objectives=(FilmOptimizationObjective(500., 600., 5, (37.,), polarization, metric,
            "range", upper_bound=.001),), optimization_policy="nominal_preserving")
    frozen = sensitivity.freeze_film_sensitivity_request(service, request)
    evaluator = sensitivity._Evaluator(frozen, None)
    evaluator.evaluate(frozen.design_snapshot)
    data = evaluator.solver_data
    assert data is not None
    analytic = data["scenario_jac"].copy()
    nominal = data["nominal_jac"].copy()
    h = .001
    scenes, nominals = [], []
    for sign in (-1., 1.):
        changed = deepcopy(frozen.design_snapshot)
        changed["front_layers"][1]["thickness"]["value"] += sign * h
        evaluator.evaluate(changed)
        data = evaluator.solver_data
        assert data is not None
        scenes.append(data["scenario_values"].copy())
        nominals.append(data["nominal"].copy())
    np.testing.assert_allclose(analytic[:, :, 0], (scenes[1] - scenes[0]) / (2 * h), rtol=2e-8, atol=2e-11)
    np.testing.assert_allclose(nominal[:, 0], (nominals[1] - nominals[0]) / (2 * h), rtol=2e-8, atol=2e-11)


def test_expected_rms_and_worst_epigraph_analytic_gradient_match_finite_difference():
    service, request = problem(two=True, objective="finite_scenario_expected", operation="optimize")
    request = replace(request, optimization_policy="nominal_preserving",
        objectives=(replace(request.objectives[0], weight=9., normalization_scale=.1),),
        errors=tuple(replace(error, relative_fraction=.02) for error in request.errors),
        scenarios=(replace(request.scenarios[0], weight=3.), replace(request.scenarios[1], weight=7.)))
    frozen = sensitivity.freeze_film_sensitivity_request(service, request)
    evaluator = sensitivity._Evaluator(frozen, None)
    evaluator.evaluate(frozen.design_snapshot)
    data = evaluator.solver_data
    assert data is not None
    args = data["scenario_values"], data["scenario_jac"], data["conditions"]
    loss, gradient = constrained._expected_loss(*args, [3., 7.])
    baseline = evaluator.evaluate(frozen.design_snapshot)
    assert loss == pytest.approx(baseline.robustness_value)
    slack, jac = constrained._worst_epigraph(*args, .1)
    h = .001
    for column in range(2):
        samples, faces = [], []
        for sign in (-1., 1.):
            changed = deepcopy(frozen.design_snapshot)
            changed["front_layers"][column]["thickness"]["value"] += sign * h
            analysis = evaluator.evaluate(changed)
            samples.append(analysis.robustness_value)
            current = evaluator.solver_data
            assert current is not None
            faces.append(constrained._worst_epigraph(current["scenario_values"], current["scenario_jac"], current["conditions"], .1)[0])
        assert gradient[column] == pytest.approx((samples[1] - samples[0]) / (2 * h), rel=3e-8, abs=1e-11)
        np.testing.assert_allclose(jac[:, column], (faces[1] - faces[0]) / (2 * h), rtol=3e-8, atol=1e-11)
    assert len(slack) == 2 and np.all(jac[:, -1] == 1.)


def test_initial_infeasible_is_reported_not_repaired_or_relabelled_by_tolerance():
    service, request = feasible_problem()
    request = replace(request, objectives=(replace(request.objectives[0], upper_bound=0.),),
                      acceptable_normalized_error=1.)
    checkpoints: list[dict[str, Any]] = []
    result = sensitivity.optimize_film_sensitivity(service, request, checkpoint_callback=checkpoints.append)
    assert not checkpoints  # No nominal-infeasible candidate is advertised as a checkpoint.
    assert result.state == "failed" and result.termination_reason == "initial_nominal_infeasible"
    assert result.evaluations == 1 and result.iterations == 0
    assert result.solver_acceptable and not result.nominal_targets_met
    assert not result.improved and result.best_thickness_nm == {"low-film": 80.}
    sensitivity.validate_film_sensitivity_result_payload(sensitivity.film_sensitivity_result_to_payload(result))


def test_zero_thickness_and_falsely_successful_infeasible_solver_trial_never_become_incumbent(monkeypatch):
    service, request = feasible_problem()
    request = replace(request, errors=(replace(request.errors[0], absolute_nm=0.),),
        variables=(FilmThicknessVariable("front", "low-film", 0., 145.),))
    checkpoints: list[dict[str, Any]] = []
    def false_success(fun, x0, *, constraints, **_kwargs):
        zero = np.zeros_like(x0)
        fun(zero)
        assert np.any(constraints[0]["fun"](zero) < 0.)
        return SimpleNamespace(x=zero, success=True, status=0, message="false success")
    monkeypatch.setattr(constrained, "minimize", false_success)
    result = sensitivity.optimize_film_sensitivity(service, request, checkpoint_callback=checkpoints.append)
    assert result.state == "partial" and result.termination_reason == "constrained_nominal_rejected"
    assert result.nominal_targets_met and result.best_thickness_nm["low-film"] == 80.
    assert not result.improved
    assert all(payload["best_analysis"]["nominal_targets_met"] for payload in checkpoints)


@pytest.mark.parametrize("stage", ["baseline_complete", "search", "improved", "iteration"])
def test_constrained_cancellation_keeps_only_replayable_nominal_feasible_incumbents(stage):
    service, request = feasible_problem()
    event = Event()
    checkpoints: list[dict[str, Any]] = []
    def progress(value):
        if value.stage == stage:
            event.set()
    result = sensitivity.optimize_film_sensitivity(service, request, cancel_event=event,
        progress_callback=progress, checkpoint_callback=checkpoints.append)
    assert result.state == "cancelled" and result.nominal_targets_met
    for payload in checkpoints + [sensitivity.film_sensitivity_result_to_payload(result)]:
        sensitivity.validate_film_sensitivity_result_payload(payload, expected_request=result.frozen_request)


@pytest.mark.parametrize("limit,value", [("max_evaluations", 1), ("max_iterations", 1)])
def test_constrained_budget_never_claims_convergence_and_retains_feasible_incumbent(limit, value):
    service, request = feasible_problem()
    result = sensitivity.optimize_film_sensitivity(service, replace(request, **{limit: value}))
    assert result.state == "partial", result.message
    assert result.termination_reason == ("evaluation_limit" if limit == "max_evaluations" else "iteration_limit")
    assert result.nominal_targets_met
    sensitivity.validate_film_sensitivity_result_payload(sensitivity.film_sensitivity_result_to_payload(result))


def test_linear_objective_is_not_silently_changed_and_legacy_payload_identity_is_preserved():
    service, request = feasible_problem()
    with pytest.raises(ValueError, match="finite-scenario"):
        sensitivity.optimize_film_sensitivity(service, replace(request, robustness_objective="linear_sensitivity_norm"))
    _, legacy = problem()
    payload = sensitivity.film_sensitivity_request_to_payload(legacy)
    assert "optimization_policy" not in payload and "error_amplitude_semantics" not in payload
    assert all("absolute_nm" not in error and "relative_fraction" not in error for error in payload["errors"])
    assert sensitivity.film_sensitivity_request_from_payload(payload) == legacy


def test_strict_upper_constraint_uses_only_nextafter_and_final_exact_operator():
    service, request = feasible_problem()
    frozen = sensitivity.freeze_film_sensitivity_request(service, replace(request, strict_upper_bound_objective_indices=(0,)))
    evaluator = sensitivity._Evaluator(frozen, None)
    evaluator.evaluate(frozen.design_snapshot)
    data = evaluator.solver_data
    assert data is not None
    upper = frozen.objectives[0].upper_bound
    assert upper is not None
    conditions = data["conditions"]
    jac = data["nominal_jac"]
    inclusive = constrained._constraint_rows(np.asarray([upper]), jac, conditions)[0]
    strict = constrained._constraint_rows(np.asarray([upper]), jac, conditions, (0,))[0]
    assert inclusive[0] == 0. and strict[0] == np.nextafter(upper, -np.inf) - upper


def test_expected_loss_stays_finite_for_finite_extreme_weights_and_responses():
    objective = FilmOptimizationObjective(550., 550., 1, mode="range", upper_bound=0., weight=1e300)
    other = replace(objective, weight=1e-300)
    conditions = [(0, 550., 0., objective), (1, 550., 0., other)]
    # A product of raw weights or squaring raw 1e300 residuals would overflow.
    values = np.asarray([[1., 1e300], [2., 1e300]])
    jac = np.ones((2, 2, 1))
    loss, gradient = constrained._expected_loss(values, jac, conditions, [1e300, 1e-300])
    assert np.isfinite(loss) and np.isfinite(gradient).all()
    assert loss == pytest.approx(np.sqrt(2.))
    assert gradient[0] == pytest.approx(1 / np.sqrt(2.))


def test_physical_scene_boundary_is_representable_under_the_actual_error_expression(monkeypatch):
    service, request = feasible_problem()
    request = replace(request, variables=(FilmThicknessVariable("front", "low-film", 0., 145.),),
        objectives=(replace(request.objectives[0], upper_bound=1.),),
        errors=(sensitivity.FilmThicknessError("low-film", absolute_nm=.3, relative_fraction=.2),),
        scenarios=(sensitivity.FilmSensitivityScenario("minus", (-1.,)),))
    def visit_lower(fun, x0, *, bounds, **_kwargs):
        point = np.asarray([bounds[0][0]])
        fun(point)  # Real finite TMM; no clipping of the perturbed thickness.
        return SimpleNamespace(x=point, success=True, status=0, message="endpoint")
    monkeypatch.setattr(constrained, "minimize", visit_lower)
    result = sensitivity.optimize_film_sensitivity(service, request)
    assert result.state == "completed", result.message
    assert result.evaluations == 2
    sensitivity.validate_film_sensitivity_result_payload(sensitivity.film_sensitivity_result_to_payload(result))


def test_worst_epigraph_keeps_finite_extreme_scaled_values_and_derivatives():
    objective = FilmOptimizationObjective(550., 550., 1, mode="range", upper_bound=0.,
        weight=1e300, normalization_scale=1e-200)
    values = np.asarray([[1e-100]])
    jacobian = np.asarray([[[1e-100]]])
    rows, jac = constrained._worst_epigraph(values, jacobian, [(0, 550., 0., objective)], 2e250)
    assert np.isfinite(rows).all() and np.isfinite(jac).all()
    assert rows[0] == pytest.approx(1e250)
    assert jac[0, 0] == pytest.approx(-1e250)
    assert jac[0, 1] == 1.


def test_normalized_upper_endpoint_is_exact_and_replayable_with_real_tmm(monkeypatch):
    service, request = feasible_problem()
    upper = 165.09756267871117
    request = replace(request,
        variables=(FilmThicknessVariable("front", "low-film", 32.90145329482026, upper),),
        objectives=(replace(request.objectives[0], lower_bound=.0075, upper_bound=None),))
    def visit_upper(fun, x0, *, bounds, **_kwargs):
        point = np.asarray([bounds[0][1]])
        fun(point)
        return SimpleNamespace(x=point, success=True, status=0, message="endpoint")
    monkeypatch.setattr(constrained, "minimize", visit_upper)
    result = sensitivity.optimize_film_sensitivity(service, request)
    assert result.state == "completed" and result.improved
    assert result.best_thickness_nm["low-film"] == upper
    sensitivity.validate_film_sensitivity_result_payload(sensitivity.film_sensitivity_result_to_payload(result))


def test_real_slsqp_upper_endpoint_never_archives_one_ulp_above_original_bound():
    service, request = feasible_problem()
    upper = 82.20098960421895
    request = replace(request,
        variables=(FilmThicknessVariable("front", "low-film", 16.889770652853393, upper),))
    result = sensitivity.optimize_film_sensitivity(service, request)
    assert result.state == "completed" and result.improved, result.message
    assert result.best_thickness_nm["low-film"] <= upper
    sensitivity.validate_film_sensitivity_result_payload(sensitivity.film_sensitivity_result_to_payload(result))


def test_interior_initial_anchor_does_not_alias_a_representable_upper_endpoint(monkeypatch):
    service, request = feasible_problem()
    upper = float(np.nextafter(80., np.inf))
    request = replace(request, variables=(FilmThicknessVariable("front", "low-film", np.spacing(80.) / 2, upper),))
    def visit_upper(fun, x0, *, bounds, **_kwargs):
        assert 0. < x0[0] < 1.
        point = np.asarray([bounds[0][1]])
        fun(point)
        return SimpleNamespace(x=point, success=True, status=0, message="endpoint")
    monkeypatch.setattr(constrained, "minimize", visit_upper)
    result = sensitivity.optimize_film_sensitivity(service, request)
    assert result.state == "completed", result.message
    assert result.evaluations == 2  # Initial 80 and the distinct exact upper.
    sensitivity.validate_film_sensitivity_result_payload(sensitivity.film_sensitivity_result_to_payload(result))
