from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace

import numpy as np
import pytest

from coat_sim.core import _film_ideal_sensitivity_core as core
from coat_sim.core import film_ideal_sensitivity as ideal
from coat_sim.core.film_design import new_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint as Constraint
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmThicknessVariable
from coat_sim.core.film_sensitivity_workflow_jobs import (
    build_job_payload, deserialize_job_payload, result_matches_job,
)
from tests.test_film_auto_synthesis import Service

pytestmark = pytest.mark.algorithm


def problem(*, pointwise=None, mean=None, mode="absolute", scale=1.0):
    records = []
    for name, index in (("glass", 1.5), ("film", 2.2)):
        optical = dict(id="nk", name=name, kind="tabulated", notes="synthetic fixture",
                       source="synthetic oracle", wavelength_unit="nm",
                       wavelength_min=450.0, wavelength_max=700.0,
                       wavelengths=[450.0, 550.0, 700.0], n=[index] * 3, k=[0.0] * 3)
        records.append(dict(id=name, name=name, design_ready=True, optical_sets=[optical]))
    design = new_design(design_id="band-sensitivity-fixture")
    design["substrate"].update(material_id="glass", optical_set_id="nk")
    design["front_layers"] = [dict(id="layer", material_id="film", optical_set_id="nk",
                                  thickness=dict(value=80.0, unit="nm"), note="fixture")]
    objective = FilmOptimizationObjective(
        500.0, 600.0, 5, (0.0, 35.0),
        pointwise=pointwise or Constraint(lower_bound=0.0, upper_bound=None),
        mean=mean or Constraint(enabled=False), normalization_scale=scale,
    )
    request = ideal.FilmIdealSensitivityRequest(
        design, 7, (objective,), (FilmThicknessVariable("front", "layer", 60.0, 145.0),),
        mode, max_iterations=1, max_evaluations=100, objective_ids=("row",),
        budgets=(ideal.FilmIdealDegradationBudget("row", 0.5),),
    )
    return Service(records), request


def evaluated(**kwargs):
    service, request = problem(**kwargs)
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, request)
    evaluator = core._Evaluator(frozen)
    data = evaluator.evaluate(frozen.design_snapshot)
    return frozen, evaluator, data, core._baseline_evidence(frozen, data)


@pytest.mark.parametrize("mean_only", [False, True])
def test_lower_guard_retains_each_original_point_when_pointwise_disabled(mean_only):
    settings = dict(pointwise=Constraint(enabled=not mean_only, lower_bound=0.0,
                                         upper_bound=None))
    if mean_only:
        settings["mean"] = Constraint(lower_bound=0.0, upper_bound=None)
    frozen, _evaluator, data, baseline = evaluated(**settings)
    assert frozen.guard_directions == ("lower",)
    changed = data.values.copy()
    changed[0] = np.nextafter(changed[0] - 0.005, -np.inf)
    changed[1] += 0.05  # Mean compensation cannot hide the original point's loss.
    candidate = replace(data, values=changed)
    assert not core._guard(candidate, frozen, baseline).guard_pass
    rows = core._constraint_rows(candidate, frozen, [0], np.array([85.0]), baseline)
    assert len(rows[0]) == 10 and np.min(rows[0]) < 0
    assert len(data.values) == 10
    assert len(data.analysis.nominal_conditions) == (2 if mean_only else 10)


def test_enabled_direction_union_is_frozen_and_survives_payload_job_roundtrip():
    frozen, _evaluator, data, baseline = evaluated(
        pointwise=Constraint(lower_bound=0.0, upper_bound=None),
        mean=Constraint(lower_bound=None, upper_bound=0.8),
    )
    assert frozen.guard_directions == ("both",)
    payload = ideal.film_ideal_sensitivity_request_to_payload(frozen)
    restored = ideal.film_ideal_sensitivity_request_from_payload(payload)
    assert restored.guard_directions == ("both",)
    assert core._request_identity(restored) == core._request_identity(frozen)
    job = build_job_payload(None, restored)
    assert deserialize_job_payload(job).guard_directions == ("both",)
    # Even a subsequently edited target cannot reinterpret the locked guard.
    changed_objective = replace(frozen.objectives[0], mean=Constraint(enabled=False))
    edited = replace(frozen, objectives=(changed_objective,))
    refrozen = ideal.freeze_film_ideal_sensitivity_request(None, edited)
    assert refrozen.guard_directions == ("both",)
    raised = replace(data, values=data.values + 0.006)
    assert not core._guard(raised, refrozen, baseline).guard_pass


def test_disabled_bound_sides_do_not_contribute_to_direction():
    frozen, *_ = evaluated(pointwise=Constraint(enabled=False),
                           mean=Constraint(lower_bound=0.0, upper_bound=None))
    assert frozen.guard_directions == ("lower",)


@pytest.mark.parametrize("mode", ["absolute", "relative"])
def test_full_grid_f_definition_and_scale_are_unchanged_for_mean_only(mode):
    frozen, evaluator, data, _baseline = evaluated(
        pointwise=Constraint(enabled=False),
        mean=Constraint(lower_bound=0.0, upper_bound=None), mode=mode, scale=0.3,
    )
    amplitude = 1.0 if mode == "absolute" else 80.0
    expected = np.mean(np.sum((data.nominal_jacobian * amplitude / 0.3) ** 2, axis=1))
    assert data.analysis.objective_value == pytest.approx(expected, rel=2e-14)
    assert evaluator.scalar_objective(frozen.design_snapshot) == pytest.approx(expected, rel=2e-14)
    resources = ideal.estimate_film_ideal_sensitivity_resources(frozen)
    assert resources["condition_points"] == 10
    assert resources["condition_blocks_per_evaluation"] == 2
    assert resources["inequality_constraints"] == 10


def test_physical_mean_nominal_pass_does_not_require_disabled_local_target():
    frozen, _evaluator, data, _baseline = evaluated()
    values = data.values[:5]
    average = float(np.array([0.125, 0.25, 0.25, 0.25, 0.125]) @ values)
    upper = (average + float(np.max(values))) / 2
    objective = replace(frozen.objectives[0], angles_deg=(0.0,),
                        pointwise=Constraint(enabled=False, lower_bound=None, upper_bound=0.0),
                        mean=Constraint(lower_bound=None, upper_bound=upper))
    request = replace(frozen, objectives=(objective,), kernel_identity=None, guard_directions=())
    result = ideal.analyze_film_ideal_sensitivity(None, request)
    assert result.best_analysis is not None
    assert result.best_analysis.nominal_targets_met
    assert result.best_analysis.nominal_conditions[0].reduction == "mean"
    assert result.best_analysis.nominal_conditions[0].wavelength_nm is None
    both = replace(objective, pointwise=Constraint(lower_bound=None, upper_bound=upper))
    both_result = ideal.analyze_film_ideal_sensitivity(None, replace(request, objectives=(both,)))
    assert both_result.best_analysis is not None
    assert not both_result.best_analysis.nominal_targets_met
    assert both_result.best_analysis.objective_value == result.best_analysis.objective_value


def test_result_replay_preserves_raw_grid_and_rejects_old_schema():
    service, request = problem(pointwise=Constraint(enabled=False),
                               mean=Constraint(lower_bound=1.0, upper_bound=None))
    result = ideal.analyze_film_ideal_sensitivity(service, request)
    assert result.state == "completed" and not result.nominal_targets_met
    payload = ideal.film_ideal_sensitivity_result_to_payload(result)
    assert payload["schema_version"] == 3
    assert len(payload["best_analysis"]["full_grid_values"]) == 10
    ideal.validate_film_ideal_sensitivity_result_payload(payload, replay=True)
    ideal.validate_film_ideal_sensitivity_result_payload(payload, replay=False)
    bad = deepcopy(payload)
    bad["best_analysis"]["full_grid_values"][0] += 0.1
    with pytest.raises(ValueError):
        ideal.validate_film_ideal_sensitivity_result_payload(bad, replay=False)
    old = ideal.film_ideal_sensitivity_request_to_payload(result.frozen_request)
    old["schema_version"] = 2
    with pytest.raises(ValueError):
        ideal.film_ideal_sensitivity_request_from_payload(old)
    job = build_job_payload(None, result.frozen_request)
    assert not result_matches_job(payload, job)  # Analyze cannot impersonate optimize.


def test_shared_strict_mean_constraint_jacobian_and_shape_checker():
    frozen, _evaluator, data, _baseline = evaluated(
        pointwise=Constraint(enabled=False),
        mean=Constraint(lower_bound=0.0, upper_bound=None, strict_lower=True),
    )
    frozen = replace(frozen, policy="strict_nominal", budgets=())
    rows = core._strict_nominal_constraint_rows(data, frozen, [0], np.array([85.0]))
    assert len(rows[0]) == 2
    q = np.array([0.125, 0.25, 0.25, 0.25, 0.125])
    assert rows[1][0, 0] == pytest.approx(float(q @ data.nominal_jacobian[:5, 0]) * 85)
    core._validate_analysis_shape(data.analysis, frozen)
    corrupted = replace(data.analysis, nominal_conditions=tuple(
        replace(row, actual=row.actual + 0.01) for row in data.analysis.nominal_conditions))
    with pytest.raises(ValueError):
        core._validate_analysis_shape(corrupted, frozen)
    assert asdict(data.analysis)["full_grid_values"] == tuple(data.values)


def test_bounded_policy_starts_from_nominal_failure_and_keeps_f0_conditioning():
    service, request = problem(pointwise=Constraint(enabled=False),
                               mean=Constraint(lower_bound=1.0, upper_bound=None), scale=0.3)
    result = ideal.execute_film_ideal_sensitivity_request(service, request)
    assert result.baseline_analysis is not None
    assert result.baseline_guard is not None
    assert result.best_guard is not None
    assert result.termination_reason != "initial_nominal_infeasible"
    assert not result.baseline_analysis.nominal_targets_met
    assert result.baseline_guard.guard_pass
    assert result.best_guard.guard_pass
    assert result.frozen_request.guard_directions == ("lower",)
    assert result.solver_objective_scale == result.baseline_analysis.objective_value
    assert result.baseline_solver_objective_value == 1.0
    payload = ideal.film_ideal_sensitivity_result_to_payload(result)
    ideal.validate_film_ideal_sensitivity_result_payload(payload, replay=True)
    job = build_job_payload(None, result.frozen_request)
    assert result_matches_job(payload, job)


@pytest.mark.parametrize("mean", [
    Constraint(mode="value", target_value=0.3, lower_bound=None, upper_bound=None),
    Constraint(lower_bound=0.0, upper_bound=0.9),
])
def test_mean_value_and_bilateral_bounds_freeze_both_directions(mean):
    frozen, *_ = evaluated(pointwise=Constraint(enabled=False), mean=mean)
    assert frozen.guard_directions == ("both",)
