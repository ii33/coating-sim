"""Formal independent sensitivity executor and queue contracts."""

from copy import deepcopy
from dataclasses import replace
from threading import Event
from typing import Any

import pytest

from coat_sim.core.film_sensitivity import validate_film_sensitivity_result_payload
from tests.test_film_sensitivity import problem


def test_job_execution_binding_checkpoint_recovery_and_scheduler_cancel_semantics():
    from coat_sim.core import film_sensitivity_jobs as jobs

    service, request = problem(operation="optimize")
    job = jobs.build_job_payload(service, replace(request, max_evaluations=2))
    progress: list[dict[str, Any]] = []
    checkpoints: list[tuple[str, dict[str, Any]]] = []
    payload = jobs.execute_job(
        {"payload": job},
        Event(),
        progress.append,
        "memory-checkpoint",
        lambda path, value: checkpoints.append((path, value)),
    )
    assert jobs.is_sensitivity_result(payload) and jobs.result_matches_job(payload, job)
    assert payload["state"] == "partial" and payload["is_final"]
    assert all(row["stage"].startswith("film_sensitivity:") for row in progress)
    assert checkpoints and checkpoints[-1][0] == "memory-checkpoint"
    recovered = jobs.validate_checkpoint(checkpoints[-1][1], expected_job=job)
    assert recovered["state"] == "cancelled" and recovered["is_final"]
    validate_film_sensitivity_result_payload(
        recovered, expected_request=jobs.deserialize_job_payload(job)
    )
    cancelled = jobs.cancelled_reply(payload)
    assert (
        cancelled["state"] == "cancelled"
        and cancelled["termination_reason"] == "scheduler_cancel_won"
    )
    validate_film_sensitivity_result_payload(
        cancelled, expected_request=jobs.deserialize_job_payload(job)
    )
    foreign = deepcopy(job)
    foreign["request"]["design_revision"] += 1
    assert not jobs.result_matches_job(payload, foreign)
    with pytest.raises(ValueError, match="bound"):
        jobs.validate_checkpoint(checkpoints[-1][1], expected_job=foreign)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(schema_version=True),
    lambda p: p.update(extra="not permitted"),
    lambda p: p.update(kind="coat_sim.film_optimization_job"),
    lambda p: p["request"].update(frozen_material_snapshot=None),
])
def test_worker_rejects_wrong_envelope_or_live_material_dependency(mutation):
    from coat_sim.core import film_sensitivity_jobs as jobs

    service, request = problem()
    job = jobs.build_job_payload(service, request)
    mutation(job)
    with pytest.raises(ValueError):
        jobs.deserialize_job_payload(job)
    with pytest.raises(ValueError):
        jobs.execute_job({"payload": job}, Event(), lambda _item: None,
                         "unused", lambda *_args: None)


def test_job_spec_frozen_worker_and_replay_ignore_live_catalog_and_copy_inputs():
    from coat_sim.core import film_sensitivity_jobs as jobs
    from coat_sim.scheduler import freeze_spec
    from coat_sim.scheduler.jobs import JobSpec

    service, request = problem(two=True)
    spec = freeze_spec(service, "film_sensitivity", {"request": request})
    parsed = JobSpec.parse_frozen(spec)
    assert parsed.executor == "film_sensitivity"
    frozen = jobs.deserialize_job_payload(parsed.parameters["payload"])
    service.materials.records.clear()
    request.design_snapshot["front_layers"][0]["thickness"]["value"] = 123.0
    checkpoints = []
    result = jobs.execute_job(parsed.parameters, Event(), lambda _item: None, "checkpoint",
                              lambda path, item: checkpoints.append((path, deepcopy(item))))
    assert result["state"] == "completed"
    assert result["best_thickness_nm"]["low-film"] == 80.0
    assert jobs.result_matches_job(result, parsed.parameters["payload"])
    validate_film_sensitivity_result_payload(result, expected_request=frozen, replay=True)
    assert parsed.fingerprint == JobSpec.parse_frozen(spec).fingerprint


@pytest.fixture
def scored_checkpoint():
    from coat_sim.core import film_sensitivity_jobs as jobs

    service, request = problem(operation="optimize")
    job = jobs.build_job_payload(service, replace(request, max_evaluations=2))
    checkpoints = []
    result = jobs.execute_job({"payload": job}, Event(), lambda _item: None, "checkpoint",
                              lambda _path, value: checkpoints.append(deepcopy(value)))
    assert checkpoints
    return job, result, checkpoints[-1]


@pytest.mark.parametrize("mutation", [
    lambda p: p["result"]["best_analysis"].update(robustness_value=123.0),
    lambda p: p["result"]["baseline_analysis"]["linear_conditions"][0]["derivative_per_nm"].__setitem__(0, 123.0),
    lambda p: p["result"]["best_design"]["front_layers"][0]["thickness"].update(value=100.0),
    lambda p: p["result"]["best_thickness_nm"].update({"low-film": 123.0}),
    lambda p: p["result"]["frozen_request"]["errors"][0].update(magnitude=2.0),
    lambda p: p["job"]["request"].update(design_revision=123),
])
def test_checkpoint_recovery_replays_scores_derivatives_design_and_request(scored_checkpoint, mutation):
    from coat_sim.core import film_sensitivity_jobs as jobs

    job, _result, checkpoint = scored_checkpoint
    damaged = deepcopy(checkpoint)
    mutation(damaged)
    with pytest.raises(ValueError):
        jobs.validate_checkpoint(damaged, expected_job=job)


class _QueueStore:
    def __init__(self, root, row):
        self.root = root
        self.row = row

    def read_job(self, _job_id):
        return deepcopy(self.row)


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_queue_reads_typed_replayed_sensitivity_and_rejects_foreign_job_kind(scored_checkpoint, tmp_path, state):
    from coat_sim.core import film_sensitivity_jobs as jobs
    from coat_sim.scheduler.jobs import JobSpec, canonical
    from coat_sim.scheduler.result_capability import job_has_viewable_result
    from coat_sim.ui.compute_queue import _annotate_result_availability, _read_queue_result

    job, result, _checkpoint = scored_checkpoint
    if state in {"failed", "cancelled"}:
        result = jobs.terminal_reply(result, state)
    spec = JobSpec("film_sensitivity", {"payload": job}, {})
    directory = tmp_path / "artifacts/runtime/scheduler/jobs/sensitivity-test"
    directory.mkdir(parents=True)
    (directory / "spec.json").write_text(canonical(spec.to_dict()), encoding="utf-8")
    row = {"job_id": "sensitivity-test", "kind": "film_sensitivity", "state": state,
           "input_digest": spec.fingerprint, "result": result}
    assert job_has_viewable_result(row)
    _annotate_result_availability(tmp_path, [row])
    assert row["resultAvailable"]
    loaded, asset = _read_queue_result(None, _QueueStore(tmp_path, row), row["job_id"])
    assert loaded == result and loaded is not result and asset == ""
    foreign = {**row, "kind": "film_optimization"}
    assert not job_has_viewable_result(foreign)
    _annotate_result_availability(tmp_path, [foreign])
    assert not foreign["resultAvailable"]
    with pytest.raises(ValueError, match="does not belong"):
        _read_queue_result(None, _QueueStore(tmp_path, foreign), foreign["job_id"])
    tampered = deepcopy(row)
    tampered["result"]["best_analysis"]["linear_sensitivity_norm"] += 1.0
    with pytest.raises(ValueError, match="replay"):
        _read_queue_result(None, _QueueStore(tmp_path, tampered), tampered["job_id"])


def test_scheduler_checkpoint_loader_binds_persisted_spec_and_digest_without_ipc(scored_checkpoint, tmp_path):
    import json

    from coat_sim.scheduler.jobs import JobSpec, canonical
    from coat_sim.scheduler.result_capability import load_best_candidate_checkpoint
    from coat_sim.scheduler.store import SchedulerStore

    job, _result, checkpoint = scored_checkpoint
    spec = JobSpec("film_sensitivity", {"payload": job}, {})
    store = SchedulerStore(tmp_path)
    job_id = "checkpoint-sensitivity"
    store.submit(spec.to_dict(), job_id=job_id)
    directory = tmp_path / "artifacts/runtime/scheduler/jobs" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "spec.json").write_text(canonical(spec.to_dict()), encoding="utf-8")
    (directory / "best-candidate.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    recovered, reason = load_best_candidate_checkpoint(tmp_path, job_id)
    assert recovered is not None and reason == ""
    assert recovered["state"] == "cancelled" and recovered["termination_reason"] == "scored_checkpoint_recovered"
    foreign = deepcopy(spec.to_dict())
    foreign["parameters"]["payload"]["request"]["design_revision"] += 1
    (directory / "spec.json").write_text(canonical(foreign), encoding="utf-8")
    rejected, reason = load_best_candidate_checkpoint(tmp_path, job_id)
    assert rejected is None and "identity mismatch" in reason


@pytest.mark.parametrize("field", ["design_revision", "acceptable_normalized_error", "error_model"])
def test_worker_terminal_and_cancel_reply_reject_other_frozen_sensitivity_input(scored_checkpoint, field):
    from coat_sim.scheduler.worker import _completed_film_cancel_reply, _film_reply_matches_payload

    job, result, _checkpoint = scored_checkpoint
    assert _film_reply_matches_payload(result, job)
    assert _completed_film_cancel_reply(result, job)["state"] == "cancelled"
    foreign = deepcopy(job)
    foreign["request"][field] = {"design_revision": 999, "acceptable_normalized_error": .123,
                                  "error_model": "common_mode"}[field]
    assert not _film_reply_matches_payload(result, foreign)
    assert _completed_film_cancel_reply(result, foreign) is None
    assert not _film_reply_matches_payload(result, {"schema_version": 1, "kind": "foreign"})
