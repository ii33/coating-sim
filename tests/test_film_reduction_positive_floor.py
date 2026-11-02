"""Positive minimums protect survivors, not selected layers deleted by reduction."""
from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_reduction import (
    FilmReductionResult,
    film_reduction_result_from_payload,
    film_reduction_result_to_payload,
    freeze_film_reduction_request,
    reduce_film_design,
    validate_film_reduction_candidate_topology,
    validate_film_reduction_payload,
)
from tests.test_film_reduction import Service, layer, request
from tests.test_film_reduction_physics import ar_objectives, independent_check

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


@pytest.mark.parametrize("mode", ["layer_count", "combined"])
def test_real_positive_floor_spacer_delete_and_merge_remains_optically_valid(mode):
    source = request([layer("low-a", 60.0), layer("high-spacer", 0.001, "high"),
                      layer("low-b", 60.0)], mode=mode, objective=ar_objectives()[0],
                     max_iterations=8, max_evaluations=80)
    variables = tuple(replace(var, lower_nm=0.001 if var.layer_id == "high-spacer" else 10.0)
                      for var in source.base_request.variables)
    source = replace(source, base_request=replace(source.base_request,
        variables=variables, objectives=ar_objectives()))
    original = deepcopy(source.base_request.design_snapshot)
    service = Service()
    result = reduce_film_design(service, source)
    assert result.state == "completed"
    assert result.summary["source_feasible"] and result.summary["candidate_feasible"]
    assert result.summary["reduction_achieved"]
    assert result.metrics["candidate"]["layer_count"] == 1
    survivor = result.best_design["front_layers"][0]
    assert survivor["id"] in {"low-a", "low-b"}
    assert 10.0 <= survivor["thickness"]["value"] <= 200.0
    assert source.base_request.design_snapshot == original
    independent_check(original, ar_objectives())
    independent_check(result.best_design, ar_objectives())
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    assert film_reduction_result_from_payload(payload).best_design == result.best_design
    assert validate_film_reduction_payload(service, payload)["reduction"]["summary"]["candidate_feasible"]


@pytest.mark.parametrize("mode", ["layer_count", "combined"])
def test_positive_floors_allow_selected_deletions_on_both_sides_but_not_fixed(mode):
    source = request([layer("front-fixed", 25), layer("front-selected", 20)],
                     back=[layer("back-selected", 15), layer("back-fixed", 30)],
                     selected=[("front", "front-selected"), ("back", "back-selected")],
                     lower=10, mode=mode)
    before = deepcopy(source.base_request.design_snapshot)
    result = reduce_film_design(Service(lambda *_: 0.1), source)
    assert result.state == "completed" and result.summary["reduction_achieved"]
    assert result.best_design["front_layers"] == before["front_layers"][:1]
    assert result.best_design["back_layers"] == before["back_layers"][1:]
    assert set(result.topology_audit["removed_layer_ids"]) == {"front-selected", "back-selected"}
    tampered = deepcopy(result.best_design)
    tampered["front_layers"] = []
    with pytest.raises(ValueError, match="selected|unselected"):
        validate_film_reduction_candidate_topology(result.frozen_request, tampered)


@pytest.mark.parametrize("mode", ["layer_count", "combined", "thickness"])
def test_retained_selected_layer_must_stay_within_original_bounds(mode):
    source = request([layer("retained", 30)], mode=mode, lower=10)
    variables = tuple(replace(var, upper_nm=30) for var in source.base_request.variables)
    source = replace(source, base_request=replace(source.base_request, variables=variables))
    frozen = freeze_film_reduction_request(Service(), source)
    for value in (10.0, 30.0):
        candidate = deepcopy(frozen.base_request.design_snapshot)
        candidate["front_layers"][0]["thickness"]["value"] = value
        validate_film_reduction_candidate_topology(frozen, candidate)
    for value in (0.001, 9.99, 30.01):
        candidate = deepcopy(frozen.base_request.design_snapshot)
        candidate["front_layers"][0]["thickness"]["value"] = value
        with pytest.raises(ValueError, match="retained selected layer violates its own bounds"):
            validate_film_reduction_candidate_topology(frozen, candidate)


def test_thickness_mode_positive_floor_preserves_topology_even_if_optically_deletable():
    result = reduce_film_design(Service(lambda *_: 0.1), request(mode="thickness", lower=7))
    assert result.summary["reduction_achieved"]
    assert result.best_thickness_nm == {"a": 7.0}
    assert result.topology_audit["removed_layer_ids"] == []
    candidate = deepcopy(result.best_design)
    candidate["front_layers"] = []
    with pytest.raises(ValueError, match="preserve topology"):
        validate_film_reduction_candidate_topology(result.frozen_request, candidate)


@pytest.mark.parametrize("mode", ["layer_count", "combined"])
def test_positive_floor_does_not_relax_optical_gate_or_publish_subminimum(mode):
    objective = FilmOptimizationObjective(550, 550, 1,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.2))
    source = request(mode=mode, lower=10, objective=objective)
    # The retained source and subminimum intermediate values pass optics; the
    # actual deletion does not. Subminimum intermediates must never be results.
    def optics(design, *_):
        return 0.1 if design["front_layers"] else 0.9
    checkpoints: list[FilmReductionResult] = []
    result = reduce_film_design(Service(optics), source, checkpoint_callback=checkpoints.append)
    assert result.state == "completed" and result.summary["source_feasible"]
    for item in [*checkpoints, result]:
        validate_film_reduction_candidate_topology(item.frozen_request, item.best_design)
        assert item.best_design["front_layers"]
        assert item.best_design["front_layers"][0]["thickness"]["value"] >= 10
        assert item.summary["candidate_feasible"]
    assert result.metrics["candidate"]["layer_count"] == 1
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    payload["candidate"]["design"]["front_layers"][0]["thickness"]["value"] = 9.0
    with pytest.raises(ValueError, match="retained selected layer violates its own bounds"):
        film_reduction_result_from_payload(payload)


@pytest.mark.parametrize("stop", ["budget", "cancel"])
def test_positive_floor_interrupted_search_preserves_last_complete_audit(stop):
    event = Event()
    def optics(_design, _waves, calls):
        if stop == "cancel" and len(calls) == 3:
            event.set()
        return 0.1
    objective = FilmOptimizationObjective(550, 550, 1,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.2))
    source = request(lower=10, mode="layer_count", objective=objective)
    if stop == "budget":
        source = replace(source, max_evaluations=2)
    service = Service(optics)
    result = reduce_film_design(service, source, cancel_event=event)
    assert result.state == ("cancelled" if stop == "cancel" else "completed")
    assert not result.summary["reduction_achieved"]
    assert result.best_design == result.frozen_request.base_request.design_snapshot
    assert result.candidate_verified and result.base_result.validation_complete
    assert result.base_result.evaluations <= (2 if stop == "budget" else 3)
    validate_film_reduction_candidate_topology(result.frozen_request, result.best_design)
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    assert film_reduction_result_from_payload(payload).best_design == result.best_design


def _continuation_source_and_response():
    """A flat endpoint basin needs continuation to transport the survivor.

    Drop starts at 20 nm with a 10 nm floor. Two survivors shift in turn
    as drop goes 20 -> 10 -> 5 -> 0. The joint feasible region is deliberately
    discontinuous so a one-axis sweep from the source cannot reach the endpoint.
    """
    objective = FilmOptimizationObjective(550, 550, 1,
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0, upper_bound=0.2))
    source = request([layer("drop", 20, "high"), layer("survivor-a", 40),
                      layer("survivor-b", 40, "high")],
                     mode="layer_count", lower=10, objective=objective)
    variables = tuple(replace(var, upper_nm=120) for var in source.base_request.variables)
    source = replace(source, base_request=replace(source.base_request, variables=variables))

    def response(design, *_):
        rows = {row["id"]: row["thickness"]["value"] for row in design["front_layers"]}
        if not {"survivor-a", "survivor-b"} <= rows.keys():
            return 0.9
        drop = rows.get("drop", 0)
        desired = {20: (40, 40), 10: (60, 40), 5: (60, 60), 0: (80, 60)}.get(drop)
        return (0.1 if desired is not None and abs(rows["survivor-a"] - desired[0]) < 5
                and abs(rows["survivor-b"] - desired[1]) < 5 else 0.9)
    return source, response


def _subminimum_drop(design):
    return any(row["id"] == "drop" and 0 < row["thickness"]["value"] < 10
               for row in design["front_layers"])


def test_continuation_finds_audited_endpoint_without_publishing_subminimum_rows():
    source, response = _continuation_source_and_response()
    service = Service(response)
    checkpoints: list[FilmReductionResult] = []
    result = reduce_film_design(service, source, checkpoint_callback=checkpoints.append)
    assert any(_subminimum_drop(design) for design, _settings in service.calls)
    assert result.state == "completed" and result.summary["reduction_achieved"]
    assert [row["id"] for row in result.best_design["front_layers"]] == ["survivor-a", "survivor-b"]
    assert result.summary["candidate_feasible"] and result.candidate_verified
    assert result.base_result.validation_complete
    for item in [*checkpoints, result]:
        assert not _subminimum_drop(item.best_design)
        validate_film_reduction_candidate_topology(item.frozen_request, item.best_design)
    payload = film_reduction_result_to_payload(result.frozen_request, result)
    validate_film_reduction_payload(service, payload)


def test_cancel_inside_subminimum_continuation_never_publishes_it():
    source, response = _continuation_source_and_response()
    event = Event()
    def cancelling(design, waves, calls):
        value = response(design, waves, calls)
        if _subminimum_drop(design):
            event.set()
        return value
    service = Service(cancelling)
    checkpoints: list[FilmReductionResult] = []
    result = reduce_film_design(service, source, cancel_event=event,
                                checkpoint_callback=checkpoints.append)
    assert event.is_set() and result.state == "cancelled"
    assert _subminimum_drop(service.calls[-1][0])
    assert result.candidate_verified and result.base_result.validation_complete
    assert not result.summary["reduction_achieved"]
    for item in [*checkpoints, result]:
        assert not _subminimum_drop(item.best_design)
        validate_film_reduction_candidate_topology(item.frozen_request, item.best_design)


def test_budget_exhaustion_inside_subminimum_continuation_keeps_audited_incumbent():
    source, response = _continuation_source_and_response()
    probe = Service(response)
    reduce_film_design(probe, source)
    first = next(i for i, (design, _) in enumerate(probe.calls, 1) if _subminimum_drop(design))
    service = Service(response)
    result = reduce_film_design(service, replace(source, max_evaluations=first + 1))
    assert any(_subminimum_drop(design) for design, _settings in service.calls)
    assert result.base_result.evaluations <= first + 1
    assert result.state == "completed" and result.base_result.termination_reason == "evaluation_budget"
    assert result.candidate_verified and result.base_result.validation_complete
    assert not result.summary["reduction_achieved"]
    assert not _subminimum_drop(result.best_design)
    validate_film_reduction_candidate_topology(result.frozen_request, result.best_design)
