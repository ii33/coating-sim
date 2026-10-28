"""Independent optional limits; exercise real local repair and complete grid accounting."""
from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective, optimize_film_design
from coat_sim.core.film_reduction import (
    film_reduction_request_from_payload, film_reduction_request_to_payload,
    film_reduction_result_from_payload, film_reduction_result_to_payload,
    freeze_film_reduction_request, reduce_film_design,
)
from tests.test_film_reduction import Service, layer, request, _meaningful_repair_source


def _long_thickness_source():
    goal = FilmOptimizationObjective(550, 550, 1,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.2))
    source = request([layer("retained", 200)], mode="thickness", lower=1, objective=goal,
                     max_iterations=200, max_evaluations=1000)
    return replace(source, base_request=replace(source.base_request,
        initial_step_fraction=0.001, min_step_fraction=0.0005))


def _long_response(design, *_):
    return 0.1 if design["front_layers"][0]["thickness"]["value"] >= 100 else 0.9


def test_default_continues_past_previous_200_iterations_and_1000_full_grids():
    service = Service(_long_response)
    result = reduce_film_design(service, _long_thickness_source())
    assert result.state == "completed" and result.summary["reduction_achieved"]
    assert result.base_result.iterations > 200
    assert result.base_result.evaluations > 1000
    assert result.base_result.evaluations == len(service.calls)
    assert 100 <= result.best_thickness_nm["retained"] < 100.2
    assert result.base_result.termination_reason == "step_tolerance"
    assert "budget" not in result.message.lower()
    assert result.frozen_request.max_iterations is result.frozen_request.max_evaluations is None
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    assert payload["schema_version"] == 3
    assert film_reduction_result_from_payload(payload).base_result.evaluations > 1000


def _slow_repair_source():
    goal = FilmOptimizationObjective(550, 550, 1,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.010005))
    source = _meaningful_repair_source()
    return replace(source, base_request=replace(source.base_request, objectives=(goal,),
                                               min_step_fraction=1e-8))


def _slow_repair_response(design, *_):
    if len(design["front_layers"]) == 2:
        return 0.01
    return 0.01 + abs(design["front_layers"][0]["thickness"]["value"] - 93.14159) / 200


def test_survivor_repair_is_not_truncated_at_hidden_8_iterations_or_35_evaluations():
    service = Service(_slow_repair_response)
    result = reduce_film_design(service, _slow_repair_source())
    assert result.summary["source_feasible"] and result.summary["reduction_achieved"]
    assert result.metrics["candidate"]["layer_count"] == 1
    assert abs(result.best_thickness_nm["survivor"] - 93.14159) <= 0.001
    assert result.base_result.iterations > 8 and result.base_result.evaluations > 35
    assert result.base_result.evaluations == len(service.calls)
    assert result.base_result.termination_reason == "proposal_exhausted"
    assert result.base_result.targets_met and result.base_result.validation_targets_met


def test_unlimited_inner_repair_cancels_at_next_forward_boundary_and_keeps_prior_audit():
    event = Event()
    def response(design, waves, calls):
        if len(calls) == 40:
            event.set()
        return _slow_repair_response(design, waves, calls)
    service = Service(response)
    result = reduce_film_design(service, _slow_repair_source(), cancel_event=event)
    assert len(service.calls) == result.base_result.evaluations == 40
    assert result.state == result.base_result.termination_reason == "cancelled"
    assert result.best_design == result.frozen_request.base_request.design_snapshot
    assert result.candidate_verified and result.base_result.validation_complete
    assert result.metrics["baseline"] == result.metrics["candidate"]


@pytest.mark.parametrize("limits,reason", [
    ({"max_iterations": 12}, "iteration_limit"),
    ({"max_evaluations": 25}, "evaluation_budget"),
])
def test_explicit_global_limit_also_limits_inner_solver_without_budget_overshoot(limits, reason):
    service = Service(_slow_repair_response)
    result = reduce_film_design(service, replace(_slow_repair_source(), **limits))
    assert result.state == "completed" and result.base_result.termination_reason == reason
    assert "user configured" in result.message and "search not exhausted" in result.message
    assert not result.summary["reduction_achieved"]
    assert result.candidate_verified and result.base_result.validation_complete
    assert len(service.calls) == result.base_result.evaluations
    for name, value in limits.items():
        counter = "iterations" if name == "max_iterations" else "evaluations"
        assert getattr(result.base_result, counter) <= value
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    assert film_reduction_result_from_payload(payload).base_result.termination_reason == reason


def test_impossible_goal_stops_by_numerical_convergence_and_proposal_exhaustion_without_budget():
    service = Service(lambda *_: 0.5)
    result = reduce_film_design(service, _slow_repair_source())
    assert result.state == "completed" and result.base_result.termination_reason == "proposal_exhausted"
    assert not result.summary["reduction_achieved"] and not result.summary["source_feasible"]
    assert result.base_result.iterations > 8 and result.base_result.evaluations > 35
    assert "budget" not in result.message.lower()
    assert result.best_design == result.frozen_request.base_request.design_snapshot


def test_audit_only_explicit_limit_uses_exactly_two_grids_and_is_distinct_from_no_improvement():
    service = Service(lambda *_: 0.1)
    result = reduce_film_design(service, replace(request(), max_evaluations=2))
    assert result.base_result.evaluations == 2 and result.base_result.validation_evaluations == 1
    assert result.base_result.termination_reason == "evaluation_budget"
    assert not result.summary["reduction_achieved"]


def test_ordinary_baseline_retains_its_explicit_finite_controls_and_unlimited_api_is_baseline_only():
    service = Service(_slow_repair_response)
    source = _slow_repair_source().base_request
    result = optimize_film_design(service, replace(source, max_iterations=1, max_evaluations=2))
    assert result.iterations <= 1 and result.evaluations <= 2
    with pytest.raises(ValueError, match="baseline solver"):
        optimize_film_design(service, replace(source, solver="powell"), baseline_limits=(None, None))


def test_v2_history_preserves_original_frozen_caps_and_exact_payload_roundtrip():
    frozen = freeze_film_reduction_request(Service(), request())
    old = film_reduction_request_to_payload(frozen)
    old.pop("limits")
    old["schema_version"] = 2
    restored = film_reduction_request_from_payload(old)
    assert restored.max_iterations == restored.base_request.max_iterations
    assert restored.max_evaluations == restored.base_request.max_evaluations
    assert film_reduction_request_to_payload(restored) == old
    result = reduce_film_design(Service(lambda *_: 0.1), restored)
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    assert payload["schema_version"] == 2
    assert film_reduction_result_to_payload(result.frozen_request, film_reduction_result_from_payload(payload)) == payload


@pytest.mark.parametrize("mutation", [
    lambda p: p["limits"].update(max_evaluations=True),
    lambda p: p["limits"].update(max_evaluations=1),
    lambda p: p["limits"].update(max_iterations=0),
    lambda p: p["limits"].update(max_seconds=2),
    lambda p: p.pop("limits"),
    lambda p: p.update(schema_version=1),
])
def test_new_limits_are_strict_and_cannot_be_dropped_or_smuggled(mutation):
    payload = film_reduction_request_to_payload(freeze_film_reduction_request(Service(), request()))
    mutation(payload)
    with pytest.raises(ValueError):
        film_reduction_request_from_payload(payload)


def test_mixed_result_request_versions_and_fake_unlimited_budget_counters_are_rejected():
    result = reduce_film_design(Service(lambda *_: 0.1), request())
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    mixed = deepcopy(payload)
    mixed["schema_version"] = 2
    with pytest.raises(ValueError, match="versions differ"):
        film_reduction_result_from_payload(mixed)
    payload["solver_validation"]["termination_reason"] = "evaluation_budget"
    # The schema permits a budget stop only if that budget was frozen explicitly.
    with pytest.raises(ValueError, match="budget"):
        film_reduction_result_from_payload(payload)
