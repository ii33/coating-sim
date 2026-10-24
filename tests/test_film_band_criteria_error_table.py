from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core import film_thickness_error_table as table
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint as Constraint
from coat_sim.core.film_optimization import FilmOptimizationRequest
from coat_sim.core.film_sensitivity_workflow_jobs import (
    build_job_payload, deserialize_job_payload, workflow_for_request,
)
from tests.test_film_band_criteria_sensitivity import evaluated, problem

pytestmark = pytest.mark.algorithm


def table_problem(**kwargs):
    service, sensitivity = problem(**kwargs)
    base = FilmOptimizationRequest(sensitivity.design_snapshot, sensitivity.design_revision,
                                   sensitivity.variables, sensitivity.objectives)
    request = table.thickness_error_table_request_from_optimization(
        base, rows=(table.FilmThicknessErrorParameter(
            "layer", bias=0.2, bias_unit="absolute_nm", uncertainty_kind="bounded",
            uncertainty_magnitude=0.5, uncertainty_unit="absolute_nm"),),
        include_finite_bias=True,
    )
    return service, request


def test_mean_only_table_retains_raw_error_rows_and_separate_nominal_evidence():
    service, request = table_problem(pointwise=Constraint(enabled=False),
                                     mean=Constraint(lower_bound=0.0, upper_bound=None))
    result = table.execute_film_thickness_error_table_request(service, request)
    assert result.state == "completed", result.message
    analysis = result.analysis
    assert analysis is not None
    assert len(analysis.conditions) == 10
    assert analysis.expected_condition_count == analysis.completed_condition_count == 10
    assert analysis.nominal_targets_met and analysis.finite_bias_targets_met
    assert all(row.nominal_condition_pass is None for row in analysis.conditions)
    assert all(row.finite_bias_condition_pass is None for row in analysis.conditions)
    assert len(analysis.nominal_conditions) == len(analysis.finite_bias_conditions) == 2
    assert all(row.reduction == "mean" and row.wavelength_nm is None
               for row in analysis.nominal_conditions)
    q = np.array([0.125, 0.25, 0.25, 0.25, 0.125])
    assert analysis.nominal_conditions[0].actual == pytest.approx(
        q @ np.asarray([row.nominal_value for row in analysis.conditions[:5]], dtype=float))
    payload = table.film_thickness_error_table_result_to_payload(result)
    assert payload["schema_version"] == 2
    table.validate_film_thickness_error_table_result_payload(payload, replay=True)
    job = build_job_payload(None, result.frozen_request)
    restored = deserialize_job_payload(job)
    assert restored == result.frozen_request
    assert workflow_for_request(restored) == "film_thickness_error_table"


def test_mean_and_pointwise_nominal_rules_use_actual_band_not_raw_flag_all():
    frozen, _evaluator, data, _evidence = evaluated()
    q = np.array([0.125, 0.25, 0.25, 0.25, 0.125])
    upper = (float(q @ data.values[:5]) + float(max(data.values[:5]))) / 2
    service, request = table_problem(pointwise=Constraint(enabled=False, upper_bound=0.0),
                                     mean=Constraint(lower_bound=None, upper_bound=upper))
    objective = replace(request.objectives[0], angles_deg=(0.0,))
    request = replace(request, objectives=(objective,))
    mean_result = table.execute_film_thickness_error_table_request(service, request)
    assert mean_result.analysis is not None
    assert mean_result.analysis.nominal_targets_met
    both = replace(objective, pointwise=Constraint(lower_bound=None, upper_bound=upper))
    both_result = table.execute_film_thickness_error_table_request(
        service, replace(request, objectives=(both,)))
    assert both_result.analysis is not None
    assert not both_result.analysis.nominal_targets_met
    assert any(row.nominal_condition_pass is False for row in both_result.analysis.conditions)
    assert len(both_result.analysis.nominal_conditions) == 6
    assert [row.bounded_first_order_l1 for row in mean_result.analysis.conditions] == [
        row.bounded_first_order_l1 for row in both_result.analysis.conditions]
    assert [row.bias_shift for row in mean_result.analysis.conditions] == [
        row.bias_shift for row in both_result.analysis.conditions]
    assert frozen.objectives[0].wavelength_samples == 5


def test_table_per_point_physics_is_not_reduced_to_the_mean():
    frozen, _evaluator, data, _evidence = evaluated()
    service, request = table_problem(pointwise=Constraint(enabled=False),
                                     mean=Constraint(lower_bound=0.0, upper_bound=None))
    result = table.execute_film_thickness_error_table_request(service, request)
    assert result.analysis is not None
    for index, row in enumerate(result.analysis.conditions):
        derivative = data.nominal_jacobian[index, 0]
        assert row.nominal_value == data.values[index]
        assert row.bias_shift == pytest.approx(derivative * 0.2)
        assert row.bounded_first_order_l1 == pytest.approx(abs(derivative) * 0.5)
    assert len({row.bounded_first_order_l1 for row in result.analysis.conditions}) > 1
    assert result.evaluations == 4 and result.jacobian_evaluations == 2
    assert frozen.objectives[0].angles_deg == (0.0, 35.0)


def test_current_table_payload_rejects_legacy_and_inconsistent_mean_evidence():
    service, request = table_problem(pointwise=Constraint(enabled=False),
                                     mean=Constraint(lower_bound=0.0, upper_bound=None))
    result = table.execute_film_thickness_error_table_request(service, request)
    payload = table.film_thickness_error_table_result_to_payload(result)
    corrupt = deepcopy(payload)
    corrupt["analysis"]["nominal_conditions"][0]["actual"] += 0.01
    with pytest.raises(ValueError):
        table.validate_film_thickness_error_table_result_payload(corrupt, replay=False)
    corrupt = deepcopy(payload)
    corrupt["analysis"]["conditions"][0]["nominal_condition_pass"] = True
    with pytest.raises(ValueError):
        table.film_thickness_error_table_result_from_payload(corrupt)
    old = table.film_thickness_error_table_request_to_payload(result.frozen_request)
    old["schema_version"] = 1
    with pytest.raises(ValueError):
        table.film_thickness_error_table_request_from_payload(old)
    old["schema_version"] = 2
    old["strict_upper_bound_objective_indices"] = []
    with pytest.raises(ValueError):
        table.film_thickness_error_table_request_from_payload(old)


def test_incomplete_mean_only_table_keeps_unknown_overall_acceptance():
    service, request = table_problem(pointwise=Constraint(enabled=False),
                                     mean=Constraint(lower_bound=0.0, upper_bound=None))
    request = replace(request, max_evaluations=2)
    result = table.execute_film_thickness_error_table_request(service, request)
    assert result.state == "partial" and result.termination_reason == "evaluation_limit"
    assert result.analysis is not None
    assert len(result.analysis.conditions) == 5
    assert result.analysis.nominal_targets_met is None
    assert result.analysis.finite_bias_targets_met is None
    assert result.analysis.nominal_conditions == result.analysis.finite_bias_conditions == ()
    table.film_thickness_error_table_result_to_payload(result)
