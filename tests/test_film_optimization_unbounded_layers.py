"""Regression coverage for optimization requests beyond the old 8-variable cap."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core.film_optimization import (
    FilmOptimizationRequest,
    FilmThicknessVariable,
    optimize_film_design,
)
from coat_sim.core.film_optimization_job import (
    _replay_candidate_scores,
    _validate_replay_request,
    execute_film_optimization_job,
    freeze_job_payload,
)
from coat_sim.core.film_optimization_structure import (
    freeze_film_structure_optimization_request,
    film_structure_request_from_payload,
    film_structure_request_to_payload,
)
from tests.test_film_optimization_needle import (
    _objective,
    _service as make_service,
)
from tests.test_film_optimization_structure import (
    _request as structure_request,
    _service as make_structure_service,
)


def _many_layer_design(design, count: int) -> dict:
    result = deepcopy(design)
    template = deepcopy(result["front_layers"][0])
    result["front_layers"] = []
    for index in range(count):
        layer = deepcopy(template)
        layer["id"] = f"regression-layer-{index:04d}"
        layer["thickness"] = {"value": 100.0, "unit": "nm"}
        result["front_layers"].append(layer)
    return result


@pytest.mark.parametrize("layer_count", [44, 60, 129])
def test_optimizer_scores_and_replays_every_layer_and_variable(tmp_path, layer_count):
    service, one_layer_design = make_service(tmp_path)
    design = _many_layer_design(one_layer_design, layer_count)
    layer_ids = [row["id"] for row in design["front_layers"]]
    variables = tuple(
        FilmThicknessVariable("front", layer_id, 99.0, 101.0)
        for layer_id in layer_ids
    )
    objective = _objective()
    request = FilmOptimizationRequest(
        design_snapshot=design,
        design_revision=23,
        variables=variables,
        objectives=(objective,),
        max_evaluations=1,
        max_iterations=1,
        validation_samples_multiplier=1,
    )

    # The replay path validates the same complete variable list as the primary
    # optimizer; it must not reintroduce the legacy eight-variable ceiling.
    _validate_replay_request(request)
    result = optimize_film_design(service, request)

    assert result.evaluations == 1
    assert result.validation_complete
    assert len(result.best_design["front_layers"]) == layer_count
    assert [row["id"] for row in result.best_design["front_layers"]] == layer_ids
    assert len(result.best_thickness_nm) == layer_count
    assert set(result.best_thickness_nm) == set(layer_ids)
    assert result.best_thickness_nm[layer_ids[-1]] == pytest.approx(100.0)
    assert result.best_design["front_layers"][-1]["id"] == layer_ids[-1]

    replay = _replay_candidate_scores(service, result.best_design, (objective,))
    assert len(replay["per_condition_errors"]) == 1
    assert replay["max_normalized_error"] == pytest.approx(
        result.max_normalized_error
    )


def test_129_layer_structure_budget_is_accepted_by_freeze():
    service, design = make_structure_service()
    design = _many_layer_design(design, 129)
    base = structure_request(design, mode="needle_only", variables=False,
                             max_total_layers=129,
                             max_total_thickness_nm=20_000.0)

    frozen = freeze_film_structure_optimization_request(service, base)

    assert frozen.max_total_layers == 129
    assert len(frozen.base_request.design_snapshot["front_layers"]) == 129
    assert frozen.base_request.variables == ()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"max_total_layers": 0}, "max_total_layers"),
        ({"max_total_layers": True}, "max_total_layers"),
        ({"max_total_layers": 4097}, "max_total_layers"),
    ],
)
def test_structure_budget_rejects_invalid_layer_limits_explicitly(changes, message):
    service, design = make_structure_service()
    request = structure_request(design, mode="needle_only", variables=False,
                                **changes)

    with pytest.raises(ValueError, match=message):
        freeze_film_structure_optimization_request(service, request)


def test_invalid_selected_layer_is_reported_without_silent_truncation(tmp_path):
    service, design = make_service(tmp_path)
    request = FilmOptimizationRequest(
        design_snapshot=design,
        design_revision=23,
        variables=(FilmThicknessVariable("front", "missing-layer", 99.0, 101.0),),
        objectives=(_objective(),),
        max_evaluations=1,
        max_iterations=1,
    )

    with pytest.raises(ValueError, match="selected front layer id is not present"):
        optimize_film_design(service, request)



@pytest.mark.parametrize("layer_count", [44, 60, 129])
def test_structure_queue_payload_and_cancelled_result_keep_complete_variables(layer_count):
    service, design = make_structure_service()
    design = _many_layer_design(design, layer_count)
    variables = tuple(FilmThicknessVariable("front", row["id"], 99.0, 101.0)
                      for row in design["front_layers"])
    request = structure_request(design, mode="growth_only", variables=False,
                                max_total_layers=layer_count + 1,
                                max_total_thickness_nm=20000.0,
                                max_evaluations=2)
    request = replace(request, base_request=replace(request.base_request, variables=variables))
    frozen = freeze_film_structure_optimization_request(service, request)
    payload = film_structure_request_to_payload(frozen)
    restored = film_structure_request_from_payload(payload)
    assert restored.base_request.variables == variables
    assert restored.max_total_layers == layer_count + 1
    cancel = Event()

    def after_first_complete_score(progress):
        if progress.message == "scored the frozen base design":
            cancel.set()

    result = execute_film_optimization_job(
        service, freeze_job_payload(service, frozen), cancel_event=cancel,
        progress_callback=after_first_complete_score)
    assert result["state"] == "cancelled"
    assert len(result["request"]["base_request"]["variables"]) == layer_count
    assert result["request"]["base_request"]["variables"][-1]["layer_id"] == variables[-1].layer_id
    assert len(result["candidate"]["design"]["front_layers"]) == layer_count
    assert result["candidate"]["design"]["front_layers"][-1]["id"] == variables[-1].layer_id
    assert result["candidate"]["quality"]["validation_complete"] is False
