from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from importlib import import_module
import json
from threading import Event
from typing import Any, cast

import numpy as np
import pytest

from coat_sim.core import film_ideal_sensitivity as ideal
from coat_sim.core import _film_ideal_sensitivity_core as core
from coat_sim.core.film_design_service import simulate_frozen
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective,
    FilmOptimizationRequest,
    FilmThicknessVariable,
)
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def problem(*, two=False, mode="absolute", upper=0.05, max_evaluations=1000, max_iterations=100):
    service, synthesis = setup_problem(low=550.0, high=550.0, samples=1, upper=upper)
    design = deepcopy(dict(synthesis.environment))
    design["front_layers"] = [
        dict(
            id="low-film",
            material_id="low",
            optical_set_id="nk",
            thickness=dict(value=80.0, unit="nm"),
            note="ideal derivative validation fixture",
        )
    ]
    if two:
        design["front_layers"].append(
            dict(
                id="high-film",
                material_id="high",
                optical_set_id="nk",
                thickness=dict(value=40.0, unit="nm"),
                note="second stable film",
            )
        )
    variables = tuple(
        FilmThicknessVariable(
            "front", row["id"], 60.0 if i == 0 else 20.0, 145.0 if i == 0 else 80.0
        )
        for i, row in enumerate(design["front_layers"])
    )
    return service, ideal.FilmIdealSensitivityRequest(
        design,
        7,
        synthesis.objectives,
        variables,
        mode,
        max_evaluations=max_evaluations,
        max_iterations=max_iterations,
        policy="strict_nominal",
        objective_ids=tuple(f"objective-{i}" for i in range(len(synthesis.objectives))),
    )


def analysis(service, request):
    result = ideal.analyze_film_ideal_sensitivity(service, request)
    assert result.state == "completed", result.message
    assert result.best_analysis is not None
    return result


def with_strict_bounds(request, *, lower=(), upper=()):
    lower, upper = set(lower), set(upper)
    objectives = tuple(replace(objective, pointwise=replace(
        objective.pointwise, strict_lower=index in lower, strict_upper=index in upper))
        for index, objective in enumerate(request.objectives))
    return replace(request, objectives=objectives)


@pytest.mark.parametrize("mode", ["absolute", "relative"])
@pytest.mark.parametrize("unit", ["nm", "um", "qw"])
def test_ideal_definition_physical_units_and_relative_one_percent_scaling(mode, unit):
    service, request = problem(two=True, mode=mode)
    request = replace(
        request,
        objectives=(
            FilmOptimizationObjective(500.0, 600.0, 5, (0.0, 42.0), pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=1.0), normalization_scale=0.3, weight=2.0),
        ),
    )
    if unit != "nm":
        changed = deepcopy(request.design_snapshot)
        for row in changed["front_layers"]:
            n = np.sqrt(1.5) if row["id"] == "low-film" else 2.2
            value = row["thickness"]["value"]
            row["thickness"] = dict(
                value=value / 1000.0 if unit == "um" else value * 4 * n / 550.0, unit=unit
            )
        request = replace(request, design_snapshot=changed)
    result = analysis(service, request)
    evaluator = core._Evaluator(result.frozen_request)
    data = evaluator.evaluate(result.frozen_request.design_snapshot)
    thickness = evaluator.physical(result.frozen_request.design_snapshot)
    amplitude = np.ones(2) if mode == "absolute" else np.asarray(list(thickness.values()))
    expected = np.mean(np.sum((data.nominal_jacobian * amplitude / 0.3) ** 2, axis=1))
    assert result.best_analysis.objective_value == pytest.approx(expected, rel=2e-14)
    assert result.best_thickness_nm["low-film"] == pytest.approx(80.0)
    assert result.best_thickness_nm["high-film"] == pytest.approx(40.0)
    # Relative is the response to a unit fraction, never silently a 1% error.
    if mode == "relative":
        one_percent = np.mean(np.sum((data.nominal_jacobian * amplitude * 0.01 / 0.3) ** 2, axis=1))
        assert one_percent == pytest.approx(expected * 0.0001)


@pytest.mark.parametrize(
    "metric,polarization",
    [("R", "s"), ("R", "p"), ("R", "unpolarized"), ("T", "s"), ("T", "p"), ("T", "unpolarized")],
)
def test_real_forward_and_analytic_constraint_jacobian_match_canonical_and_fd(metric, polarization):
    service, request = problem(two=True)
    request = replace(
        request,
        objectives=(
            FilmOptimizationObjective(500.0, 600.0, 5, (0.0, 42.0), polarization, metric, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=1.0)),
        ),
    )
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    evaluator = core._Evaluator(frozen)
    data = evaluator.evaluate(frozen.design_snapshot)
    assert frozen.frozen_material_snapshot is not None
    for i, angle in enumerate((0.0, 42.0)):
        changed = deepcopy(dict(frozen.frozen_material_snapshot))
        changed["settings"].update(
            wavelength_min_nm=500.0,
            wavelength_max_nm=600.0,
            samples=5,
            angle_deg=angle,
            polarization=polarization,
        )
        import hashlib

        changed.pop("sha256")
        changed["sha256"] = hashlib.sha256(
            json.dumps(changed, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
        direct = simulate_frozen(".", changed)[metric]
        np.testing.assert_allclose(data.values[i * 5 : (i + 1) * 5], direct, atol=2e-14)
    for col, variable in enumerate(frozen.variables):
        values = []
        for sign in (-1.0, 1.0):
            changed = deepcopy(dict(frozen.design_snapshot))
            row = next(r for r in changed["front_layers"] if r["id"] == variable.layer_id)
            row["thickness"]["value"] += sign * 0.001
            values.append(evaluator.evaluate(changed).values)
        np.testing.assert_allclose(
            data.nominal_jacobian[:, col], (values[1] - values[0]) / 0.002, rtol=3e-8, atol=2e-11
        )


def test_sensitivity_columns_are_independent_of_variables_and_frozen_in_selected_order():
    service, request = problem(two=True)
    request = replace(
        request, variables=(request.variables[0],), sensitivity_layer_ids=("high-film", "low-film")
    )
    result = analysis(service, request)
    assert result.frozen_request.sensitivity_layer_ids == ("high-film", "low-film")
    evaluator = core._Evaluator(result.frozen_request)
    assert evaluator.columns == ("high-film", "low-film")
    assert evaluator.evaluate(result.frozen_request.design_snapshot).nominal_jacobian.shape == (
        1,
        1,
    )
    only = analysis(service, replace(request, sensitivity_layer_ids=("high-film",)))
    assert 0 < only.best_analysis.objective_value < result.best_analysis.objective_value
    assert result.cache_identity != only.cache_identity


def test_true_weighted_mean_square_not_sqrt_or_mean_of_objectives():
    service, request = problem()
    objectives = (
        FilmOptimizationObjective(500.0, 600.0, 3, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=1.0), weight=9.0, normalization_scale=0.2),
        FilmOptimizationObjective(550.0, 550.0, 1, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=1.0), weight=1.0, normalization_scale=0.7),
    )
    result = analysis(service, replace(request, objectives=objectives,
        objective_ids=tuple(f"objective-{i}" for i in range(len(objectives)))))
    evaluator = core._Evaluator(result.frozen_request)
    data = evaluator.evaluate(result.frozen_request.design_snapshot)
    jac = data.nominal_jacobian[:, 0]
    expected = (9.0 * np.sum((jac[:3] / 0.2) ** 2) + (jac[3] / 0.7) ** 2) / 28.0
    assert result.best_analysis.objective_value == pytest.approx(expected)
    assert result.best_analysis.objective_value != pytest.approx(np.sqrt(expected))


@pytest.mark.parametrize(
    "value,low,high,side",
    [
        (80.0, 60.0, 145.0, "central"),
        (60.0, 60.0, 145.0, "right"),
        (145.0, 60.0, 145.0, "left"),
        (0.0, 0.0, 1.0, "right"),
    ],
)
def test_bounded_fd_stencils_and_quadratic_exactness(value, low, high, side):
    x1, x2 = core._bounded_stencil(value, low, high)
    assert low <= x1 <= high and low <= x2 <= high and len({value, x1, x2}) == 3
    assert (
        (x1 < value < x2)
        if side == "central"
        else ((value < x1 < x2) if side == "right" else (x2 < x1 < value))
    )

    def f(x):
        return (x - value) ** 2 + 3 * (x - value) + 5

    derivative = core._three_point_derivative(f(value), f(x1), f(x2), value, x1, x2)
    assert derivative == pytest.approx(3.0, rel=1e-10)


def test_fd_uses_actual_unequal_representable_float_offsets():
    value = float(2**53)
    low, high = value - 3.0, value + 6.0
    x1, x2 = core._bounded_stencil(value, low, high)
    assert (value - x1) != (x2 - value)

    def f(x):
        return (x - value) ** 2 + 3 * (x - value) + 5

    assert core._three_point_derivative(f(value), f(x1), f(x2), value, x1, x2) == pytest.approx(
        3.0
    )


@pytest.mark.parametrize(
    "value,low,high",
    [(1.0, 1.0, 1.0), (1.0, 1.0, np.nextafter(1.0, np.inf)), (0.0, 0.0, np.nextafter(0.0, 1.0))],
)
def test_fd_insufficient_representable_domain_is_explicit(value, low, high):
    with pytest.raises(core._FDDomainInsufficient, match="fd_domain_insufficient"):
        core._bounded_stencil(value, low, high)


@pytest.mark.parametrize("mode", ["absolute", "relative"])
def test_real_objective_gradient_h_half_diagnostic_and_relative_chain(mode):
    service, request = problem(two=True, mode=mode)
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    evaluator = core._Evaluator(frozen)
    base = evaluator.evaluate(frozen.design_snapshot).analysis.objective_value
    estimates = []
    for multiplier in (1.0, 0.5):
        gradient = []
        for variable in frozen.variables:
            value = evaluator.physical(frozen.design_snapshot)[variable.layer_id]
            x1, x2 = core._bounded_stencil(
                value, variable.lower_nm, variable.upper_nm, step_scale=multiplier
            )
            f = []
            for x in (x1, x2):
                changed = core._candidate_design(frozen.design_snapshot, (variable,), (x,))
                f.append(evaluator.evaluate(changed, probe=True).analysis.objective_value)
            gradient.append(core._three_point_derivative(base, f[0], f[1], value, x1, x2))
        estimates.append(gradient)
    np.testing.assert_allclose(estimates[0], estimates[1], rtol=3e-7, atol=1e-12)
    assert evaluator.probes == 8


def test_nominal_constraint_jacobian_is_unscaled_J_then_z_span_only():
    service, request = problem(two=True, mode="relative")
    request = replace(
        request,
        objectives=(
            FilmOptimizationObjective(500.0, 600.0, 3, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=0.001, upper_bound=0.5), normalization_scale=0.3),
        ),
    )
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    data = core._Evaluator(frozen).evaluate(frozen.design_snapshot)
    spans = np.asarray([85.0, 60.0])
    rows = core._strict_nominal_constraint_rows(data, frozen, [0, 1], spans)
    np.testing.assert_allclose(rows[1][0], data.nominal_jacobian[0] * spans / 0.3)
    np.testing.assert_allclose(rows[1][1], -data.nominal_jacobian[0] * spans / 0.3)


def test_initial_nominal_infeasible_fails_without_pseudo_feasible_best_including_empty_coating():
    service, request = problem(upper=0.0)
    for design in (request.design_snapshot, dict(request.design_snapshot, front_layers=[])):
        selected = () if not design["front_layers"] else request.variables
        result = ideal.execute_film_ideal_sensitivity_request(
            service, replace(request, design_snapshot=design, variables=selected)
        )
        assert (
            result.state == "failed" and result.termination_reason == "initial_nominal_infeasible"
        )
        assert (
            result.baseline_analysis is not None
            and not result.baseline_analysis.nominal_targets_met
        )
        assert (
            result.best_design is None
            and result.best_analysis is None
            and result.best_thickness_nm == {}
        )
        ideal.validate_film_ideal_sensitivity_result_payload(
            ideal.film_ideal_sensitivity_result_to_payload(result),
            expected_request=result.frozen_request,
        )


def test_strict_bounds_use_exact_original_check_and_single_inward_ulp():
    service, request = problem()
    initial = analysis(service, request).best_analysis.nominal_conditions[0].actual
    request = with_strict_bounds(replace(
        request,
        objectives=(FilmOptimizationObjective(550.0, 550.0, 1, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=initial, upper_bound=None)),),
    ), lower=(0,))
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.termination_reason == "initial_nominal_infeasible"
    assert analysis(service, with_strict_bounds(request)).best_analysis.nominal_targets_met
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    data = core._Evaluator(frozen).evaluate(frozen.design_snapshot)
    rows = core._strict_nominal_constraint_rows(data, frozen, [0], np.asarray([85.0]))
    assert rows[0][0] == initial - np.nextafter(initial, np.inf)
    upper = with_strict_bounds(replace(
        request,
        objectives=(replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, lower_bound=None, upper_bound=initial)),),
    ), upper=(0,))
    assert (
        ideal.execute_film_ideal_sensitivity_request(service, upper).termination_reason
        == "initial_nominal_infeasible"
    )


def test_all_fixed_retains_design_and_sensitivity_column_but_does_not_claim_optimization():
    service, request = problem()
    request = replace(request, variables=(FilmThicknessVariable("front", "low-film", 80.0, 80.0),))
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.state == "failed" and result.termination_reason == "all_variables_fixed"
    assert result.best_thickness_nm == {"low-film": 80.0}
    assert result.best_analysis is not None
    assert result.best_analysis.objective_value > 0 and result.probe_evaluations == 0
    assert ideal.estimate_film_ideal_sensitivity_resources(request)["free_variables"] == 0
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result)
    )


def test_block_budget_aborts_half_gradient_and_retains_only_previous_complete_feasible_candidate():
    service, request = problem(max_evaluations=2)
    checkpoints: list[dict[str, Any]] = []
    result = ideal.execute_film_ideal_sensitivity_request(
        service, request, checkpoint_callback=checkpoints.append
    )
    assert result.state == "partial" and result.termination_reason == "evaluation_limit"
    assert result.forward_evaluations == result.jacobian_evaluations == 2
    assert result.probe_evaluations == 2 and result.fd_gradient_evaluations == 0
    assert result.best_thickness_nm == {"low-film": 80.0}
    assert result.best_analysis == result.baseline_analysis
    assert len(checkpoints) == 1
    ideal.validate_film_ideal_sensitivity_result_payload(
        checkpoints[0], expected_request=result.frozen_request
    )


def test_raw_block_budget_cannot_be_bypassed_by_one_multiblock_candidate():
    service, request = problem(max_evaluations=1)
    request = replace(
        request,
        objectives=(FilmOptimizationObjective(500.0, 600.0, 5000, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=1.0)),),
    )
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.termination_reason == "evaluation_limit" and result.forward_evaluations == 1
    assert (
        result.evaluations == 1 and result.baseline_analysis is None and result.best_design is None
    )


def test_cancel_during_raw_probe_block_never_publishes_partial_gradient(monkeypatch):
    service, request = problem()
    event = Event()
    original = core._simulate_thickness_jacobian_arrays

    def cancel_after_second(*args, **kwargs):
        answer = original(*args, **kwargs)
        event.set()  # Baseline was the first block, this scalar probe is second.
        return answer

    monkeypatch.setattr(core, "_simulate_thickness_jacobian_arrays", cancel_after_second)
    result = ideal.execute_film_ideal_sensitivity_request(service, request, cancel_event=event)
    assert result.state == "cancelled" and result.forward_evaluations == 2
    assert result.fd_gradient_evaluations == 0 and result.best_thickness_nm == {"low-film": 80.0}
    assert result.best_analysis == result.baseline_analysis


def test_exact_request_owned_cache_has_explicit_hits_and_probes():
    service, request = problem()
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    evaluator = core._Evaluator(frozen)
    first = evaluator.evaluate(frozen.design_snapshot)
    assert evaluator.evaluate(deepcopy(frozen.design_snapshot), probe=True) is first
    assert (
        evaluator.forward == 1
        and evaluator.evaluations == 1
        and evaluator.probes == 1
        and evaluator.cache_hits == 1
    )
    changed = deepcopy(frozen.design_snapshot)
    changed["front_layers"][0]["thickness"]["value"] = np.nextafter(80.0, 81.0)
    evaluator.evaluate(changed)
    assert evaluator.evaluations == 2 and evaluator.forward == 2
    assert core._request_identity(frozen) != core._request_identity(
        replace(frozen, sensitivity_mode="relative")
    )


def test_frozen_n_k_and_use_k_are_never_rewritten_and_live_edits_do_not_change_replay():
    service, request = problem()
    record = service.materials.records["low"]["optical_sets"][0]
    record["k"] = [0.015] * 3
    changed = deepcopy(request.design_snapshot)
    changed["use_k"] = True
    result = analysis(service, replace(request, design_snapshot=changed))
    frozen = result.frozen_request
    assert frozen.design_snapshot["use_k"] is True
    assert (
        next(r for r in frozen.frozen_material_snapshot["materials"] if r["id"] == "low")[
            "optical_sets"
        ][0]["k"]
        == [0.015] * 3
    )
    service.materials.records.clear()
    replay = analysis(
        None,
        ideal.film_ideal_sensitivity_request_from_payload(
            ideal.film_ideal_sensitivity_request_to_payload(frozen)
        ),
    )
    assert result == replay
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result), expected_request=frozen
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "nan",
        "grid",
        "target",
        "mode",
        "columns",
        "material",
        "revision",
        "candidate",
        "score",
        "condition",
    ],
)
def test_closed_serialization_and_replay_reject_misbinding(mutation):
    service, request = problem()
    result = analysis(service, request)
    payload = ideal.film_ideal_sensitivity_result_to_payload(result)
    if mutation == "unknown":
        payload["unexpected"] = True
    elif mutation == "nan":
        payload["best_analysis"]["objective_value"] = float("nan")
    elif mutation == "grid":
        payload["frozen_request"]["objectives"][0]["wavelength_nm"] = 550.0
    elif mutation == "target":
        payload["frozen_request"]["objectives"][0]["upper_bound"] = 0.5
    elif mutation == "mode":
        payload["frozen_request"]["sensitivity_mode"] = "relative"
    elif mutation == "columns":
        payload["frozen_request"]["sensitivity_layer_ids"] = []
    elif mutation == "material":
        payload["best_design"]["use_k"] = False
    elif mutation == "revision":
        payload["design_revision"] += 1
    elif mutation == "candidate":
        payload["best_thickness_nm"]["low-film"] += 1.0
    elif mutation == "score":
        payload["best_analysis"]["objective_value"] *= 2
    else:
        payload["best_analysis"]["nominal_conditions"][0]["wavelength_nm"] += 1.0
    with pytest.raises(ValueError):
        ideal.validate_film_ideal_sensitivity_result_payload(
            payload, expected_request=result.frozen_request
        )


def test_resource_estimate_accounts_independently_for_columns_probes_and_slsqp_and_rejects_without_truncation(
    monkeypatch,
):
    service, request = problem(two=True)
    resources = ideal.estimate_film_ideal_sensitivity_resources(request)
    assert resources["objective_probes_per_gradient"] == 4
    assert resources["raw_blocks_per_gradient"] == 4
    assert (
        resources["estimated_jacobian_bytes"] > 0
        and resources["estimated_slsqp_workspace_bytes"] > 0
    )
    monkeypatch.setattr(core, "MAX_WORKING_BYTES", 1)
    with pytest.raises(ValueError, match="no grid or columns were dropped"):
        ideal.freeze_film_ideal_sensitivity_request(service, request)


def test_factory_requires_explicit_mode_and_does_not_leak_old_solver_error_controls():
    service, request = problem()
    base = FilmOptimizationRequest(
        request.design_snapshot,
        7,
        request.variables,
        request.objectives,
        solver="powell",
        max_iterations=333,
        max_evaluations=444,
    )
    with pytest.raises(TypeError):
        cast(Any, ideal.ideal_sensitivity_request_from_optimization)(base)
    result = ideal.ideal_sensitivity_request_from_optimization(base, sensitivity_mode="absolute",
        policy="strict_nominal", objective_ids=("objective-0",), budgets=())
    assert result.max_iterations is None and result.max_evaluations is None
    assert result.sensitivity_layer_ids == ("low-film",)
    payload = ideal.film_ideal_sensitivity_request_to_payload(result)
    assert (
        payload["kind"] == ideal.REQUEST_KIND
        and "solver" not in payload
        and "error_model" not in payload
    )
    assert ideal.film_ideal_sensitivity_request_from_payload(payload) == result


@pytest.mark.parametrize("mode", ["absolute", "relative"])
def test_real_ideal_optimization_improves_raw_F_and_preserves_every_nominal_bound(mode):
    service, request = problem(mode=mode)
    request = replace(
        request,
        objectives=(FilmOptimizationObjective(545.0, 555.0, 11, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=0.012)),),
    )
    checkpoints: list[dict[str, Any]] = []
    result = ideal.execute_film_ideal_sensitivity_request(
        service, request, checkpoint_callback=checkpoints.append
    )
    assert result.state == "completed", result.message
    assert result.best_analysis is not None
    assert result.baseline_analysis is not None
    assert result.solver_objective_scale is not None
    assert (
        result.improved
        and result.best_analysis.objective_value < result.baseline_analysis.objective_value * 0.02
    )
    assert result.best_analysis.nominal_targets_met
    assert len(result.best_analysis.nominal_conditions) == 11
    assert result.solver_objective_scale == result.baseline_analysis.objective_value
    assert result.baseline_solver_objective_value == 1.0
    assert result.best_solver_objective_value == pytest.approx(
        result.best_analysis.objective_value / result.solver_objective_scale
    )
    assert result.frozen_request.solver_objective_ftol == 1e-10
    assert result.scipy_version and result.resolved_max_iterations == 100
    assert result.probe_evaluations == 2 * result.fd_gradient_evaluations
    assert all(c["best_analysis"]["nominal_targets_met"] for c in checkpoints)
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result),
        expected_request=result.frozen_request,
    )


def test_hard_nominal_gate_accepts_infeasible_fd_probes_but_never_retains_them():
    service, request = problem(max_evaluations=3)
    exact = analysis(service, request).best_analysis.nominal_conditions[0].actual
    request = replace(
        request,
        objectives=(FilmOptimizationObjective(550.0, 550.0, 1, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=exact, upper_bound=None)),),
    )
    checkpoints: list[dict[str, Any]] = []
    result = ideal.execute_film_ideal_sensitivity_request(
        service, request, checkpoint_callback=checkpoints.append
    )
    assert result.state == "completed" and result.termination_reason == "no_improvement"
    assert result.fd_gradient_evaluations == 1 and result.probe_evaluations == 2
    assert result.best_thickness_nm == {"low-film": 80.0}
    assert result.best_analysis == result.baseline_analysis
    assert len(checkpoints) == 1


def test_too_narrow_free_variable_fails_fd_without_discarding_baseline():
    service, request = problem()
    request = replace(
        request,
        variables=(FilmThicknessVariable("front", "low-film", 80.0, np.nextafter(80.0, 81.0)),),
    )
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.state == "failed" and result.termination_reason == "fd_domain_insufficient"
    assert result.best_thickness_nm == {"low-film": 80.0} and result.fd_gradient_evaluations == 0
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result)
    )


def test_fixed_variable_is_removed_only_from_solver_dof_not_sensitivity(monkeypatch):
    service, request = problem(two=True)
    request = replace(
        request,
        variables=(request.variables[0], FilmThicknessVariable("front", "high-film", 40.0, 40.0)),
        objectives=(replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, upper_bound=1.0)),),
    )
    scipy_optimize: Any = import_module("scipy.optimize")

    original = scipy_optimize.minimize
    seen = []

    def inspect_minimize(fun, x0, *args, **kwargs):
        seen.append(len(x0))
        return original(fun, x0, *args, **kwargs)

    monkeypatch.setattr(scipy_optimize, "minimize", inspect_minimize)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert seen == [1]
    assert result.best_thickness_nm["high-film"] == 40.0
    assert result.frozen_request.sensitivity_layer_ids == ("low-film", "high-film")


def test_zero_derivative_objective_stops_explicitly_without_division_or_yield_claim(monkeypatch):
    service, request = problem()
    original = core.simulate_with_thickness_jacobian

    def zero_jacobian(*args, **kwargs):
        output = original(*args, **kwargs)
        for key in ("jacobian_R", "jacobian_T"):
            output[key] = np.zeros_like(output[key])
        return output

    monkeypatch.setattr(core, "simulate_with_thickness_jacobian", zero_jacobian)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.state == "completed" and result.termination_reason == "zero_sensitivity_objective"
    assert result.best_analysis is not None
    assert result.best_analysis.objective_value == 0.0 and result.probe_evaluations == 0
    assert result.solver_objective_scale is None and result.best_solver_objective_value is None
    assert "not a finite-error" in result.message
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result)
    )


def test_no_explicit_iteration_budget_records_actual_scipy_default_and_does_not_pass_maxiter(
    monkeypatch,
):
    service, request = problem(max_iterations=None)
    scipy_optimize: Any = import_module("scipy.optimize")

    original = scipy_optimize.minimize
    seen = []

    def inspect_options(*args, **kwargs):
        seen.append(kwargs["options"])
        return original(*args, **kwargs)

    monkeypatch.setattr(scipy_optimize, "minimize", inspect_options)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert "maxiter" not in seen[0] and seen[0]["ftol"] == 1e-10
    assert result.resolved_max_iterations is not None
    assert result.resolved_max_iterations > 0 and result.scipy_version


@pytest.mark.parametrize("missing", ["unknown", "k_domain"])
def test_strict_source_gate_rejects_unknown_or_out_of_domain_k_but_allows_explicit_user_ignore(
    missing,
):
    service, request = problem()
    optical = service.materials.records["low"]["optical_sets"][0]
    optical["k"] = None
    if missing == "k_domain":
        optical.pop("n")
        optical.pop("k")
        optical.pop("wavelengths")
        optical.update(kind="cauchy", parameters=dict(A=float(np.sqrt(1.5)), B=0.0, C=0.0))
        optical["k_data"] = dict(
            wavelength_unit="nm",
            wavelength_min=500.0,
            wavelength_max=540.0,
            wavelengths=[500.0, 540.0],
            k=[0.0, 0.0],
        )
    with pytest.raises(ValueError, match="low/nk.*550-550 nm"):
        ideal.freeze_film_ideal_sensitivity_request(service, request)
    changed = deepcopy(request.design_snapshot)
    changed["use_k"] = False
    result = analysis(service, replace(request, design_snapshot=changed))
    assert result.frozen_request.design_snapshot["use_k"] is False
    captured = next(
        r for r in result.frozen_request.frozen_material_snapshot["materials"] if r["id"] == "low"
    )["optical_sets"][0]
    assert captured.get("k") is None


def test_strict_source_gate_is_applied_to_frozen_replay_not_only_live_freeze():
    service, request = problem()
    result = analysis(service, request)
    payload = ideal.film_ideal_sensitivity_result_to_payload(result)
    frozen = payload["frozen_request"]["frozen_material_snapshot"]
    next(r for r in frozen["materials"] if r["id"] == "low")["optical_sets"][0]["k"] = None
    import hashlib

    frozen.pop("sha256")
    frozen["sha256"] = hashlib.sha256(
        json.dumps(frozen, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
    changed_request = ideal.film_ideal_sensitivity_request_from_payload(payload["frozen_request"])
    payload["cache_identity"] = core._request_identity(changed_request)
    payload["baseline_evidence"]["request_identity"] = payload["cache_identity"]
    evidence = ideal._evidence_from_payload(payload["baseline_evidence"])
    payload["baseline_evidence"]["digest"] = core._evidence_digest(evidence)
    with pytest.raises(ValueError, match="low/nk"):
        ideal.validate_film_ideal_sensitivity_result_payload(payload, replay=False)


def test_original_infrared_grid_is_kept_and_strict_lower_not_inferred_from_hr_target():
    service, request = problem()
    for record in service.materials.records.values():
        optical = record["optical_sets"][0]
        optical.update(
            wavelength_min=800.0, wavelength_max=1200.0, wavelengths=[800.0, 1000.0, 1200.0]
        )
    request = replace(
        request,
        objectives=(FilmOptimizationObjective(900.0, 1100.0, 201, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=0.0, upper_bound=None)),),
        max_evaluations=3,
    )
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.baseline_analysis is not None
    assert result.frozen_request.objectives[0].wavelength_samples == 201
    assert result.frozen_request.objectives[0].pointwise.strict_lower is False
    assert len(result.baseline_analysis.nominal_conditions) == 201
    assert result.baseline_analysis.nominal_conditions[0].wavelength_nm == 900.0
    assert result.baseline_analysis.nominal_conditions[-1].wavelength_nm == 1100.0


def test_synthetic_hr_stack_uses_original_ge_099_operator_and_improves_with_fixed_topology():
    service, request = problem()
    design = deepcopy(request.design_snapshot)
    design["front_layers"] = []
    for i in range(6):
        for name, n in (("low", np.sqrt(1.5)), ("high", 2.2)):
            design["front_layers"].append(
                dict(
                    id=f"{name}-{i}",
                    material_id=name,
                    optical_set_id="nk",
                    thickness=dict(
                        value=550.0 / (4 * n) + (5.0 if i == 0 and name == "low" else 0.0),
                        unit="nm",
                    ),
                    note="synthetic HR oracle",
                )
            )
    request = replace(
        request,
        design_snapshot=design,
        variables=(FilmThicknessVariable("front", "low-0", 90.0, 135.0),),
        objectives=(FilmOptimizationObjective(549.0, 551.0, 3, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=0.99, upper_bound=None)),),
    )
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.baseline_analysis is not None
    assert result.best_analysis is not None
    assert result.best_design is not None
    assert result.baseline_analysis.nominal_targets_met, result.baseline_analysis
    assert result.best_analysis.nominal_targets_met
    assert result.frozen_request.objectives[0].pointwise.strict_lower is False
    assert len(result.best_design["front_layers"]) == 12
    assert result.improved
    ideal.validate_film_ideal_sensitivity_result_payload(
        ideal.film_ideal_sensitivity_result_to_payload(result)
    )


def test_tiny_squared_objective_replay_has_no_large_absolute_tolerance_loophole():
    service, request = problem()
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.best_analysis is not None
    assert result.best_analysis.objective_value < 1e-20
    payload = ideal.film_ideal_sensitivity_result_to_payload(result)
    payload["best_analysis"]["objective_value"] *= 2.0
    payload["best_solver_objective_value"] = (
        payload["best_analysis"]["objective_value"] / payload["solver_objective_scale"]
    )
    with pytest.raises(ValueError, match="physical replay differs"):
        ideal.validate_film_ideal_sensitivity_result_payload(payload)


def test_tiny_initial_F_normalization_overflow_is_explicit_and_keeps_feasible_baseline(monkeypatch):
    service, request = problem()
    original = core.simulate_with_thickness_jacobian

    def tiny_at_baseline(design, *args, **kwargs):
        output = original(design, *args, **kwargs)
        value = design["front_layers"][0]["thickness"]["value"]
        output["jacobian_R"] = np.full_like(output["jacobian_R"], 1e-160 if value == 80.0 else 1.0)
        return output

    monkeypatch.setattr(core, "simulate_with_thickness_jacobian", tiny_at_baseline)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.state == "failed" and result.termination_reason == "numerical_failure"
    assert "conditioning overflows" in result.message
    assert result.fd_gradient_evaluations == 0 and result.best_thickness_nm == {"low-film": 80.0}
    assert result.best_analysis == result.baseline_analysis
    assert result.solver_objective_scale is not None
    assert result.solver_objective_scale > 0.0


@pytest.mark.parametrize(
    "state,reason", [("cancelled", "scheduler_cancel_won"), ("failed", "scheduler_failed")]
)
def test_formal_scheduler_terminal_overrides_retain_replayable_feasible_incumbent(state, reason):
    service, request = problem()
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    payload = ideal.film_ideal_sensitivity_result_to_payload(
        replace(result, state=state, termination_reason=reason)
    )
    ideal.validate_film_ideal_sensitivity_result_payload(
        payload, expected_request=result.frozen_request
    )
    payload["termination_reason"] = "unknown_scheduler_reason"
    with pytest.raises(ValueError, match="termination reason"):
        ideal.validate_film_ideal_sensitivity_result_payload(payload, replay=False)


def test_same_budget_unconditioned_contrast_exposes_small_F_early_stop_without_changing_public_F(
    monkeypatch,
):
    service, request = problem()
    initial_F = analysis(service, request).best_analysis.objective_value
    scipy_optimize: Any = import_module("scipy.optimize")

    original = scipy_optimize.minimize

    def unconditioned(fun, x0, *args, **kwargs):
        gradient = kwargs["jac"]
        kwargs["jac"] = lambda z: gradient(z) * initial_F
        return original(lambda z: fun(z) * initial_F, x0, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(scipy_optimize, "minimize", unconditioned)
        raw = ideal.execute_film_ideal_sensitivity_request(service, request)
    conditioned = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert raw.termination_reason == "no_improvement" and not raw.improved
    assert conditioned.termination_reason == "slsqp_converged"
    assert raw.best_analysis is not None
    assert conditioned.best_analysis is not None
    assert raw.best_analysis.objective_value == initial_F
    assert conditioned.best_analysis.objective_value < initial_F * 1e-10
    assert raw.frozen_request.max_evaluations == conditioned.frozen_request.max_evaluations == 1000
    assert raw.best_analysis.nominal_targets_met and conditioned.best_analysis.nominal_targets_met


def test_resource_forward_workspace_includes_unselected_fixed_layers():
    _service, request = problem(two=True)
    narrow = replace(
        request, variables=(request.variables[0],), sensitivity_layer_ids=("low-film",)
    )
    many = deepcopy(narrow.design_snapshot)
    for i in range(25):
        layer = deepcopy(many["front_layers"][1])
        layer["id"] = f"fixed-extra-{i}"
        many["front_layers"].append(layer)
    few_estimate = ideal.estimate_film_ideal_sensitivity_resources(narrow)
    many_estimate = ideal.estimate_film_ideal_sensitivity_resources(
        replace(narrow, design_snapshot=many)
    )
    assert many_estimate["derivative_columns"] == few_estimate["derivative_columns"] == 1
    assert many_estimate["estimated_jacobian_bytes"] == few_estimate["estimated_jacobian_bytes"]
    assert (
        many_estimate["estimated_forward_workspace_bytes"]
        > few_estimate["estimated_forward_workspace_bytes"]
    )
    assert many_estimate["estimated_prepared_optical_cache_bytes"] > 0


def test_true_all_fixed_failure_cannot_be_relabelled_as_slsqp_convergence():
    service, request = problem()
    request = replace(request, variables=(FilmThicknessVariable("front", "low-film", 80.0, 80.0),))
    fixed = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert fixed.state == "failed" and fixed.termination_reason == "all_variables_fixed"
    assert fixed.baseline_analysis is not None
    assert fixed.baseline_analysis.objective_value > 0.0 and fixed.solver_objective_scale is None
    payload = ideal.film_ideal_sensitivity_result_to_payload(fixed)
    payload.update(state="completed", termination_reason="slsqp_converged")
    with pytest.raises(ValueError, match="free physical thickness"):
        ideal.validate_film_ideal_sensitivity_result_payload(
            payload, expected_request=fixed.frozen_request, replay=True
        )


@pytest.mark.parametrize(
    "field", ["solver_objective_scale", "resolved_max_iterations", "scipy_version"]
)
def test_true_positive_F_completion_requires_fixed_conditioning_and_solver_metadata(field):
    service, request = problem()
    completed = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert completed.baseline_analysis is not None
    assert completed.state == "completed" and completed.baseline_analysis.objective_value > 0.0
    payload = ideal.film_ideal_sensitivity_result_to_payload(completed)
    payload[field] = None
    if field == "solver_objective_scale":
        payload["baseline_solver_objective_value"] = None
        payload["best_solver_objective_value"] = None
    with pytest.raises(ValueError, match="solver metadata"):
        ideal.validate_film_ideal_sensitivity_result_payload(
            payload, expected_request=completed.frozen_request, replay=True
        )


def test_full_topology_resource_gate_rejects_4096_layers_before_freeze_or_forward_and_keeps_89_layer_case(
    monkeypatch,
):
    service, request = problem()

    def topology(count):
        design = deepcopy(request.design_snapshot)
        original = design["front_layers"][0]
        design["front_layers"] = [dict(deepcopy(original), id=f"layer-{i}") for i in range(count)]
        return replace(
            request,
            design_snapshot=design,
            variables=(FilmThicknessVariable("front", "layer-0", 60.0, 145.0),),
            sensitivity_layer_ids=("layer-0",),
            objectives=(
                FilmOptimizationObjective(500.0, 600.0, 4096, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=1.0)),
            ),
        )

    large = topology(4096)
    estimate = ideal.estimate_film_ideal_sensitivity_resources(large)
    # Sixteen-million wavelength/layer states require at least five complex128
    # arrays even before the selected-column derivative/optimizer workspaces.
    physical_lower_bound = 5 * 16 * 4096 * 4096
    assert (
        estimate["estimated_forward_workspace_bytes"]
        >= physical_lower_bound
        > core.MAX_WORKING_BYTES
    )
    assert estimate["derivative_columns"] == 1

    def unexpected(*_args, **_kwargs):
        raise AssertionError(
            "resource rejection must precede material freeze or forward allocation"
        )

    monkeypatch.setattr(service, "freeze_design", unexpected)
    with pytest.raises(ValueError, match="working resource limit exceeded"):
        ideal.freeze_film_ideal_sensitivity_request(service, large)
    small = topology(89)
    estimate_89 = ideal.estimate_film_ideal_sensitivity_resources(small)
    assert estimate_89["condition_points"] == 4096
    assert estimate_89["estimated_working_bytes"] < core.MAX_WORKING_BYTES
    ideal._check_resources(small)


def test_4098_nonbinary_original_grid_matches_all_single_point_physics_and_preserves_block_count():
    service, request = problem()
    objective = FilmOptimizationObjective(500.123, 600.789, 4098, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=1.0), polarization="s")
    request = replace(request, objectives=(objective,))
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    evaluator = core._Evaluator(frozen)
    full = evaluator.evaluate(frozen.design_snapshot)
    expected_waves = np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm, 4098)
    recorded_waves = np.asarray([row.wavelength_nm for row in full.analysis.nominal_conditions])
    assert recorded_waves.tobytes() == expected_waves.tobytes()
    assert evaluator.forward == evaluator.jacobian == 2
    singles, single_jacobians = [], []
    for wave in expected_waves:
        single = core.simulate_with_thickness_jacobian(
            frozen.design_snapshot,
            evaluator.materials,
            evaluator.derivative_variables,
            dict(
                wavelength_min_nm=float(wave),
                wavelength_max_nm=float(wave),
                samples=1,
                angle_deg=0.0,
                polarization="s",
                first_surface="front",
            ),
            wavelengths_nm=np.asarray([wave]),
        )
        singles.append(single["R"][0])
        single_jacobians.append(single["jacobian_R"][0])
    np.testing.assert_array_equal(full.values, np.asarray(singles))
    np.testing.assert_array_equal(full.nominal_jacobian, np.asarray(single_jacobians))


def test_resampled_return_grid_is_rejected_not_labelled_as_original(monkeypatch):
    service, request = problem()
    request = replace(
        request,
        objectives=(
            FilmOptimizationObjective(500.123, 600.789, 4098, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=1.0)),
        ),
    )
    original = core.simulate_with_thickness_jacobian

    def replace_grid(*args, **kwargs):
        output = original(*args, **kwargs)
        waves = np.asarray(output["wavelength_nm"], dtype=float)
        waves[1] = np.nextafter(waves[1], np.inf)
        output["wavelength_nm"] = waves
        return output

    monkeypatch.setattr(core, "simulate_with_thickness_jacobian", replace_grid)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.state == "failed" and result.termination_reason == "numerical_failure"
    assert "changed the exact original wavelength grid" in result.message
    assert (
        result.forward_evaluations == 1
        and result.best_design is None
        and result.baseline_analysis is None
    )


def test_4098_grid_cancellation_at_first_raw_block_prevents_second_block_and_partial_analysis(
    monkeypatch,
):
    service, request = problem()
    request = replace(
        request,
        objectives=(
            FilmOptimizationObjective(500.123, 600.789, 4098, pointwise=FilmObjectiveConstraint(mode="range", target_value=None, lower_bound=None, upper_bound=1.0)),
        ),
    )
    event = Event()
    original = core.simulate_with_thickness_jacobian
    calls = []

    def cancel(*args, **kwargs):
        calls.append(np.asarray(kwargs["wavelengths_nm"]).copy())
        output = original(*args, **kwargs)
        event.set()
        return output

    monkeypatch.setattr(core, "simulate_with_thickness_jacobian", cancel)
    result = ideal.execute_film_ideal_sensitivity_request(service, request, cancel_event=event)
    assert result.state == "cancelled"
    assert len(calls) == 1 and len(calls[0]) == core.BLOCK_SAMPLES == 4096
    assert result.forward_evaluations == result.jacobian_evaluations == 1
    assert result.baseline_analysis is None and result.best_design is None


def test_original_grid_index_163_strict_nominal_decision_matches_exact_singleton():
    service, request = problem()
    objective = replace(
        request.objectives[0],
        wavelength_min_nm=500.0,
        wavelength_max_nm=600.0,
        wavelength_samples=4098,
    )
    request = replace(request, objectives=(objective,))
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    evaluator = core._Evaluator(frozen)
    waves = np.linspace(500.0, 600.0, 4098)
    wave = float(waves[163])
    single_objective = replace(
        objective, wavelength_min_nm=wave, wavelength_max_nm=wave, wavelength_samples=1
    )
    singleton = analysis(None, replace(frozen, objectives=(single_objective,)))
    boundary = singleton.best_analysis.nominal_conditions[0].actual
    target = replace(objective, pointwise=replace(objective.pointwise, upper_bound=boundary))
    strict_request = with_strict_bounds(replace(frozen, objectives=(target,)), upper=(0,))
    full = analysis(None, strict_request)
    row = full.best_analysis.nominal_conditions[163]
    exact_single = analysis(
        None,
        replace(
            strict_request,
            objectives=(
                replace(
                    strict_request.objectives[0], wavelength_min_nm=wave,
                    wavelength_max_nm=wave, wavelength_samples=1
                ),
            ),
        ),
    )
    assert row.wavelength_nm == wave and row.actual == boundary
    from coat_sim.core.film_nominal_acceptance import nominal_condition_pass
    assert not nominal_condition_pass(target.pointwise, row.actual, strict_upper=row.strict_upper)
    assert exact_single.best_analysis.nominal_targets_met is False
    assert full.forward_evaluations == 2 and exact_single.forward_evaluations == 1
    # The general optics API endpoint-only call constructs its own grid.
    # It is not the exact-grid path required by current ideal sensitivity.
    endpoint_only = core.simulate_with_thickness_jacobian(
        frozen.design_snapshot,
        evaluator.materials,
        evaluator.derivative_variables,
        dict(
            wavelength_min_nm=float(waves[0]),
            wavelength_max_nm=float(waves[4095]),
            samples=4096,
            angle_deg=0.0,
            polarization="unpolarized",
            first_surface="front",
        ),
    )
    assert endpoint_only["R"][163] < boundary
    assert nominal_condition_pass(target.pointwise, endpoint_only["R"][163], strict_upper=True)
