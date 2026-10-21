"""Time limits retain scored evidence without acquiring cancellation authority."""
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_jobs as jobs
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def time_limit_payload(monkeypatch, tmp_path, *, mode="training"):
    service, request = setup_problem(upper=.05 if mode == "audited" else .03,
                                   low=550., high=550., samples=1)
    request = synthesis.freeze_film_synthesis_request(service, replace(request,
        seed_designs=(deepcopy(request.environment),), max_evaluations=None, max_seconds=1.))
    payload = film_synthesis_request_to_payload("time-limit-worker-reply", request)
    job = jobs.build_job_payload(jobs.canonical_parameters(None, {"payload": payload}))
    clock = [0.]
    monkeypatch.setattr(synthesis, "monotonic", lambda: clock[0])
    stop_stage = {"training": "zero_layer_trial", "audited": "validation_complete",
                  "empty": "freezing"}[mode]

    def progress(value, **_kwargs):
        if value.get("stage") == stop_stage:
            clock[0] = 2.

    response = jobs.execute_job({"payload": job}, Event(), progress,
        tmp_path / "checkpoint.json", lambda *_args: None, str(tmp_path))
    return json.loads(json.dumps(response, allow_nan=False))


@pytest.mark.parametrize("mode", ["training", "audited", "empty"])
def test_real_execute_job_time_limit_roundtrip_retains_only_matching_evidence(monkeypatch, tmp_path, mode):
    payload = time_limit_payload(monkeypatch, tmp_path, mode=mode)
    body = payload["result"]
    assert body["state"] == "not_found" and body["termination_reason"] == "time_limit"
    assert body["targets_met"] is False
    assert jobs.is_synthesis_result(payload)
    assert "cancel_snapshot" not in payload["review"]
    assert payload["review"]["limits_nm_and_counts"]["max_seconds"] == 1.
    assert body["frozen_request"]["max_seconds"] == 1.
    if mode == "empty":
        assert body["candidate_design"] is None and body["training_score"] is None
        assert body["evaluations"] == 0
        assert body["validation_complete"] is False
    else:
        assert body["candidate_design"] is not None and body["training_score"] is not None
        provenance = body["candidate_provenance"]
        assert provenance["selection"] == "completed_training_at_time_limit"
        assert provenance["frozen_at_time_limit"] is True
        assert "frozen_at_cancel" not in provenance
        assert provenance["validation_matches_candidate"] is (mode == "audited")
        if mode == "training":
            assert len(body["candidate_design"]["front_layers"]) == 1
            assert body["validation_score"] is None
            assert payload["review"]["per_objective"][0]["score_scope"] == "training"
            assert payload["best_candidate_audit"]["targets_met"] is None
        else:
            assert body["validation_complete"] is True
            assert body["training_score"]["design"] == body["validation_score"]["design"]
            assert payload["best_candidate_audit"]["available"] is True
    assert body["performance"]["cleanup_complete"] is True


@pytest.mark.parametrize("mutation", ["cancel_state", "completed_state", "wrong_reason",
    "cancel_selection", "cancel_flag", "no_max_seconds", "invalid_max_seconds",
    "false_complete", "half_grid", "wrong_design", "wrong_counter", "cancel_snapshot", "bad_provenance_type"])
def test_time_limit_provenance_cannot_forge_cancel_or_bypass_frozen_evidence(monkeypatch, tmp_path, mutation):
    payload = time_limit_payload(monkeypatch, tmp_path)
    body = payload["result"]
    if mutation == "cancel_state":
        body["state"] = "cancelled"
    elif mutation == "completed_state":
        body["state"] = "completed"
    elif mutation == "wrong_reason":
        body["termination_reason"] = "evaluation_limit"
    elif mutation == "cancel_selection":
        body["candidate_provenance"]["selection"] = "completed_training_at_cancel"
    elif mutation == "cancel_flag":
        body["candidate_provenance"]["frozen_at_cancel"] = True
    elif mutation == "no_max_seconds":
        body["frozen_request"].pop("max_seconds")
    elif mutation == "invalid_max_seconds":
        body["frozen_request"]["max_seconds"] = 0.
    elif mutation == "false_complete":
        body["candidate_provenance"]["training_complete"] = False
    elif mutation == "half_grid":
        body["training_score"]["errors"].pop()
    elif mutation == "wrong_design":
        body["training_score"]["design"]["front_layers"][0]["thickness"]["value"] += 1.
    elif mutation == "wrong_counter":
        body["validation_evaluations"] = body["evaluations"]
    elif mutation == "cancel_snapshot":
        payload["review"]["cancel_snapshot"] = {"source": "worker_completed_reply", "candidate_available": True}
    elif mutation == "bad_provenance_type":
        body["candidate_provenance"] = ["completed_training_at_time_limit"]
    assert not jobs.is_synthesis_result(payload)


def test_time_limit_audit_must_match_candidate_and_empty_reply_requires_budget(monkeypatch, tmp_path):
    audited = time_limit_payload(monkeypatch, tmp_path, mode="audited")
    audited["result"]["validation_score"]["design"]["name"] = "different candidate"
    assert not jobs.is_synthesis_result(audited)
    empty = time_limit_payload(monkeypatch, tmp_path, mode="empty")
    empty["result"]["frozen_request"]["max_seconds"] = None
    assert not jobs.is_synthesis_result(empty)


@pytest.mark.parametrize("provenance", [None, {}])
def test_audited_time_limit_requires_explicit_timed_provenance(monkeypatch, tmp_path, provenance):
    payload = time_limit_payload(monkeypatch, tmp_path, mode="audited")
    if provenance is None:
        payload["result"].pop("candidate_provenance")
    else:
        payload["result"]["candidate_provenance"] = provenance
    assert not jobs.is_synthesis_result(payload)
