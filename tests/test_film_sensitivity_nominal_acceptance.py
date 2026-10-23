"""Independent sensitivity never relabels numerical tolerance as nominal pass."""
from dataclasses import replace
import math

import pytest

from coat_sim.core.film_sensitivity import (
    analyze_film_sensitivity, film_sensitivity_request_from_payload,
    film_sensitivity_request_to_payload, film_sensitivity_result_from_payload,
    film_sensitivity_result_to_payload, validate_film_sensitivity_result_payload,
)
from tests.test_film_sensitivity import problem

pytestmark = pytest.mark.algorithm


def measured_problem():
    service, request = problem()
    # A positive error below the representable thickness ULP preserves the
    # exact same finite samples without bypassing nonzero-error validation.
    request = replace(request, errors=tuple(replace(e, magnitude=math.ulp(80.) / 4) for e in request.errors),
                      acceptable_normalized_error=.001)
    baseline = analyze_film_sensitivity(service, request)
    assert baseline.best_analysis is not None
    actual = baseline.best_analysis.nominal_conditions[0].actual
    assert actual > .0005
    return service, request, actual


def test_actual_nominal_and_finite_violation_is_not_hidden_by_solver_tolerance():
    service, request, actual = measured_problem()
    request = replace(request, objectives=(replace(request.objectives[0], upper_bound=actual-.0005),))
    result = analyze_film_sensitivity(service, request)
    assert result.best_analysis is not None
    assert result.best_analysis.nominal_max_normalized_error == pytest.approx(.0005)
    assert result.solver_acceptable is True
    assert result.targets_met is False
    assert not result.best_analysis.nominal_targets_met
    assert not any(row.finite_targets_met for row in result.best_analysis.finite_scenarios)
    payload = film_sensitivity_result_to_payload(result)
    assert payload['solver_acceptable'] is True
    assert validate_film_sensitivity_result_payload(payload) == payload
    payload['best_analysis']['nominal_targets_met'] = True
    with pytest.raises(ValueError, match='nominal'):
        film_sensitivity_result_from_payload(payload)


@pytest.mark.parametrize('direction,expected', [(-1, False), (0, False), (1, True)])
def test_explicit_strict_upper_is_bound_operator_not_margin(direction, expected):
    service, request, actual = measured_problem()
    bound = actual if direction == 0 else math.nextafter(actual, math.inf if direction > 0 else -math.inf)
    request = replace(request, objectives=(replace(request.objectives[0], upper_bound=bound),),
                      strict_upper_bound_objective_indices=(0,))
    result = analyze_film_sensitivity(service, request)
    assert result.best_analysis is not None
    assert result.best_analysis.nominal_targets_met is expected
    assert all(row.finite_targets_met is expected for row in result.best_analysis.finite_scenarios)
    assert result.solver_acceptable is expected
    payload = film_sensitivity_request_to_payload(result.frozen_request)
    assert payload['strict_upper_bound_objective_indices'] == [0]
    assert film_sensitivity_request_from_payload(payload).strict_upper_bound_objective_indices == (0,)


def test_unflagged_upper_stays_inclusive_and_empty_field_keeps_legacy_request_identity():
    service, request, actual = measured_problem()
    request = replace(request, objectives=(replace(request.objectives[0], upper_bound=actual),))
    result = analyze_film_sensitivity(service, request)
    assert result.nominal_targets_met
    assert result.solver_acceptable is True
    payload = film_sensitivity_request_to_payload(result.frozen_request)
    assert 'strict_upper_bound_objective_indices' not in payload
    assert film_sensitivity_request_from_payload(payload).strict_upper_bound_objective_indices == ()
    old = film_sensitivity_result_to_payload(result)
    old.pop('solver_acceptable')
    restored = film_sensitivity_result_from_payload(old)
    assert restored.solver_acceptable is True
    assert restored.cache_identity == result.cache_identity


@pytest.mark.parametrize('indices', [(True,), (-1,), (1,), (0, 0), '0'])
def test_strict_upper_indices_reject_invalid_or_ambiguous_values(indices):
    service, request, _ = measured_problem()
    with pytest.raises(ValueError, match='strict_upper'):
        analyze_film_sensitivity(service, replace(request, strict_upper_bound_objective_indices=indices))
