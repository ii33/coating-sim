"""Independent result-envelope/replay checks without material file writes."""
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import sys

import pytest

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
from test_film_structure_preview import _fixture
from coat_sim.core.film_optimization_structure import (
    FilmStructureCounters, FilmStructureTraceEvent, evaluate_frozen_structure_candidate,
    film_structure_checkpoint_to_payload, film_structure_result_from_payload,
    film_structure_result_to_payload, validate_film_structure_payload,
)
from coat_sim.core.film_structure_payload_contract import validate_structure_result_payload_schema


@pytest.fixture(scope="module")
def valid_result():
    request, result = _fixture()
    evaluation = evaluate_frozen_structure_candidate(request, result.best_design)
    score = evaluation["score"]
    targets = score["max_error"] <= request.base_request.acceptable_normalized_error
    base = replace(result.base_result,
        max_normalized_error=score["max_error"],
        weighted_max_normalized_error=score["weighted_max_error"],
        weighted_rms_error=score["weighted_rms_error"], targets_met=targets,
        per_condition_errors=score["errors"], validation_evaluations=1,
        validation_max_normalized_error=score["max_error"],
        validation_weighted_rms_error=score["weighted_rms_error"],
        validation_targets_met=targets, validation_per_condition_errors=score["errors"],
    )
    result = replace(result, base_result=base, counters=FilmStructureCounters(
        objective_evaluations=2, score_evaluations=1, validation_evaluations=1,
        local_iterations=1, candidate_count=1, needle_candidate_count=1))
    return request, result, film_structure_result_to_payload(request, result)


def test_result_schema_and_frozen_replay_agree_on_complete_candidate(valid_result):
    _, _, payload = valid_result
    validate_structure_result_payload_schema(payload)
    parsed = film_structure_result_from_payload(payload)
    assert parsed.candidate_verified is True
    replay = validate_film_structure_payload(None, payload)
    assert replay["partial_result"]["validation_complete"] is True
    assert replay["structure"]["topology_audit"]["base_order_preserved"] is True


@pytest.mark.parametrize("path", [(), ("candidate",), ("candidate", "quality"),
    ("structure",), ("structure", "counters"), ("partial_result",), ("request", "limits")])
def test_result_rejects_unknown_fields_before_coercion(valid_result, path):
    payload = deepcopy(valid_result[2])
    target = payload
    for key in path:
        target = target[key]
    target["unknown_field"] = 1
    with pytest.raises(ValueError, match="fields"):
        film_structure_result_from_payload(payload)


@pytest.mark.parametrize("path,value", [
    (("schema_version",), True),
    (("candidate", "design_revision"), True),
    (("candidate", "quality", "targets_met"), "False"),
    (("candidate", "quality", "validation_complete"), 1),
    (("candidate", "quality", "max_normalized_error"), "0.2"),
    (("candidate", "quality", "weighted_rms_error"), float("nan")),
    (("structure", "counters", "objective_evaluations"), True),
    (("structure", "counters", "local_iterations"), 1.5),
    (("partial_result", "candidate_verified"), "yes"),
    (("partial_result", "is_final"), 1),
])
def test_result_rejects_boolean_integer_and_text_numeric_coercion(valid_result, path, value):
    payload = deepcopy(valid_result[2])
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        film_structure_result_from_payload(payload)


@pytest.mark.parametrize("counter,scope,budget", [
    ("objective_evaluations", "base_request", "max_evaluations"),
    ("local_iterations", "base_request", "max_iterations"),
    ("candidate_count", "limits", "candidate_budget"),
])
def test_result_cannot_claim_work_above_frozen_budget(valid_result, counter, scope, budget):
    payload = deepcopy(valid_result[2])
    payload["structure"]["counters"][counter] = payload["request"][scope][budget] + 1
    with pytest.raises(ValueError, match="budget"):
        film_structure_result_from_payload(payload)


def test_replay_checks_individual_optical_errors_and_actual_old_material(valid_result):
    payload = deepcopy(valid_result[2])
    payload["candidate"]["per_condition_errors"][0]["actual"] += 0.01
    with pytest.raises(ValueError, match="per-condition"):
        validate_film_structure_payload(None, payload)
    payload = deepcopy(valid_result[2])
    payload["candidate"]["design"]["front_layers"][0].update(
        material_id="structure_bench_H", optical_set_id="H_const")
    with pytest.raises(ValueError, match="metadata/material"):
        validate_film_structure_payload(None, payload)


def test_added_needle_ids_cannot_claim_different_lineage(valid_result):
    payload = deepcopy(valid_result[2])
    payload["structure"]["added_needle_layer_ids"] = ["fictional-id"]
    with pytest.raises(ValueError):
        validate_film_structure_payload(None, payload)


def test_nonfinal_checkpoint_can_contain_a_completed_verified_candidate(valid_result):
    request, result, _ = valid_result
    payload = film_structure_checkpoint_to_payload(request, result)
    assert payload["state"] == "completed"
    assert payload["partial_result"]["is_final"] is False
    parsed = film_structure_result_from_payload(payload)
    assert parsed.candidate_verified and not parsed.is_final
    replay = validate_film_structure_payload(None, payload)
    assert replay["partial_result"]["best_so_far"] is True


def test_trace_uses_nonnegative_increasing_event_indices(valid_result):
    payload = deepcopy(valid_result[2])
    payload["structure"]["trace"] = [asdict(FilmStructureTraceEvent(
        sequence=0, stage="baseline", candidate_kind="baseline"))]
    validate_structure_result_payload_schema(payload)


@pytest.mark.parametrize("indices", [[-1], [0, 0], [1, 0], [True]])
def test_trace_rejects_negative_duplicate_decreasing_and_boolean_indices(valid_result, indices):
    payload = deepcopy(valid_result[2])
    payload["structure"]["trace"] = [asdict(FilmStructureTraceEvent(
        sequence=value, stage="baseline", candidate_kind="baseline")) for value in indices]
    with pytest.raises(ValueError, match="sequence"):
        validate_structure_result_payload_schema(payload)
