"""Candidate warm starts are first fresh physics, never old solver evidence."""
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_io import (
    SCHEMA_VERSION, film_synthesis_request_from_payload, film_synthesis_request_to_payload,
)
from coat_sim.core.film_synthesis_jobs import build_job_payload, deserialize_job_payload
from tests.test_film_synthesis_jobs import _request

pytestmark = pytest.mark.algorithm


@pytest.mark.parametrize("strategy", ["hybrid", "legacy"])
def test_tiny_new_budget_scores_saved_best_first_not_bare_environment(strategy, monkeypatch):
    request = replace(_request(), search_strategy=strategy,
        warm_start_seed_index=0, max_evaluations=2)
    captured = []
    original = synthesis.evaluate_frozen_structure_candidate

    def evaluate(frozen, design, **kwargs):
        captured.append(physical_design_hash(design))
        return original(frozen, design, **kwargs)

    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", evaluate)
    before = deepcopy(request)
    result = synthesis.synthesize_film_design(None, request)
    expected = physical_design_hash(request.seed_designs[0])
    assert captured and captured[0] == expected
    assert result.evaluations == 2 and result.validation_evaluations == 1
    assert result.candidate_design is not None
    assert physical_design_hash(result.candidate_design) == expected
    assert result.performance["search"]["warm_start"]["saved_score_reused"] is False
    assert result.performance["search"]["warm_start"]["solver_state_preserved"] is False
    assert request == before


def test_null_selector_preserves_bare_first_path(monkeypatch):
    request = replace(_request(), max_evaluations=2)
    assert request.warm_start_seed_index is None
    captured = []
    original = synthesis.evaluate_frozen_structure_candidate

    def evaluate(frozen, design, **kwargs):
        captured.append(physical_design_hash(design))
        return original(frozen, design, **kwargs)

    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", evaluate)
    result = synthesis.synthesize_film_design(None, request)
    assert captured[0] == physical_design_hash(request.environment)
    assert "warm_start" not in result.performance["search"]


@pytest.mark.parametrize("index", [True, False, -1, 1, 0., "0"])
def test_selector_requires_real_in_range_integer(index):
    with pytest.raises(ValueError, match="warm_start_seed_index"):
        synthesis._validated(replace(_request(), warm_start_seed_index=index))


def test_current_wire_requires_explicit_selector_no_v4_migration():
    request = replace(_request(), warm_start_seed_index=0)
    payload = json.loads(json.dumps(film_synthesis_request_to_payload("warm", request)))
    assert SCHEMA_VERSION == payload["schema_version"] == 6
    assert payload["request"]["warm_start_seed_index"] == 0
    assert film_synthesis_request_from_payload(payload)[1] == request
    job = build_job_payload({"payload": payload, "physical_structure_metadata": []})
    assert deserialize_job_payload(job)[1] == request
    old = deepcopy(payload)
    old["schema_version"] = 4
    with pytest.raises(ValueError, match="current schema 6.*no migration"):
        film_synthesis_request_from_payload(old)
    del payload["request"]["warm_start_seed_index"]
    with pytest.raises(ValueError, match="every field"):
        film_synthesis_request_from_payload(payload)


def test_cancel_at_warm_admission_returns_no_new_candidate_and_runs_no_optics(monkeypatch):
    request = replace(_request(), warm_start_seed_index=0)
    event = Event()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("physics")
        raise AssertionError("cancelled admission must not calculate old or new candidate")

    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", forbidden)
    result = synthesis.synthesize_film_design(None, request, cancel_event=event,
        progress_callback=lambda item: event.set() if item.stage == "warm_restart" else None)
    assert result.state == "cancelled" and result.evaluations == 0
    assert result.candidate_design is None and result.training_score is None
    assert not calls


def test_cancel_after_first_complete_warm_score_keeps_new_score_without_audit(monkeypatch):
    request = replace(_request(), warm_start_seed_index=0)
    event = Event()
    original = synthesis.evaluate_frozen_structure_candidate
    calls = []

    def evaluate(frozen, design, **kwargs):
        calls.append(physical_design_hash(design))
        return original(frozen, design, **kwargs)

    def observe(item):
        if item.stage == "incumbent":
            event.set()

    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", evaluate)
    result = synthesis.synthesize_film_design(None, request, cancel_event=event,
        progress_callback=observe)
    assert result.state == "cancelled" and result.evaluations == 1
    assert result.validation_evaluations == 0 and result.validation_score is None
    assert result.candidate_design is not None
    assert calls == [physical_design_hash(request.seed_designs[0])]
