from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time

import pytest

from coat_sim.core import film_synthesis_audit as audit
from coat_sim.core import film_synthesis_jobs as jobs
from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request, synthesize_film_design
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload, film_synthesis_result_to_payload
from coat_sim.scheduler.result_capability import is_best_candidate_result
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def _cancelled_payload():
    """Real full one-layer training result; prior audited checkpoint is bare.

    Frozen refs: glass/nk, low/nk, high/nk. No simulated score is invented.
    """
    service, request = setup_problem(upper=.03)
    request = freeze_film_synthesis_request(service, replace(request,
        seed_designs=(deepcopy(request.environment),), max_evaluations=None))
    payload = film_synthesis_request_to_payload("cancel-complete-worker-reply", request)
    job = jobs.build_job_payload(jobs.canonical_parameters(None, {"payload": payload}))
    event = Event()

    def progress(value, **_kwargs):
        if value.get("stage") == "zero_layer_trial":
            event.set()

    with TemporaryDirectory(prefix="film-cancel-result-") as directory:
        return jobs.execute_job({"payload": job}, event, progress,
            Path(directory) / "best-candidate.json", lambda *_args: None, directory)


def test_complete_training_cancel_result_is_typed_viewable_without_audit_replay(monkeypatch):
    original = audit.audit_frozen_synthesis_candidate

    def checked(*args, **kwargs):
        assert not kwargs["cancel_event"].is_set(), "cancel must not launch another audit"
        return original(*args, **kwargs)

    monkeypatch.setattr(audit, "audit_frozen_synthesis_candidate", checked)
    payload = _cancelled_payload()
    body = payload["result"]
    assert body["state"] == "cancelled" and not body["targets_met"]
    assert len(body["candidate_design"]["front_layers"]) == 1
    assert body["training_score"]["max_error"] == 0
    assert body["validation_score"] is None and not body["validation_complete"]
    assert payload["best_candidate_audit"]["available"] is False
    assert payload["best_candidate_audit"]["targets_met"] is None
    assert payload["review"]["per_objective"][0]["score_scope"] == "training"
    assert payload["review"]["per_objective"][0]["targets_met"] is None
    assert payload["review"]["cancel_snapshot"] == {"source": "worker_completed_reply",
        "replay_performed": False, "candidate_available": True}
    monkeypatch.setattr(jobs, "evaluate_frozen_structure_candidate", lambda *_a, **_k: pytest.fail("TMM replay"))
    monkeypatch.setattr(audit, "audit_frozen_synthesis_candidate", lambda *_a, **_k: pytest.fail("audit replay"))
    assert jobs.is_synthesis_result(payload) and is_best_candidate_result(payload)
    assert jobs.is_synthesis_result(json.loads(json.dumps(payload)))
    print("complete worker cancel", body["candidate_provenance"], payload["best_candidate_audit"])


@pytest.mark.parametrize("mutation", ["missing_flag", "false_complete", "wrong_state", "half_grid",
    "wrong_score_design", "wrong_candidate", "foreign_material", "nonfinite", "wrong_aggregate",
    "fake_audit", "fake_full_audit", "policy_drift", "bad_snapshot", "wrong_layer_review"])
def test_explicit_cancel_flag_does_not_bypass_frozen_input_and_complete_score_checks(mutation):
    payload = json.loads(json.dumps(_cancelled_payload()))
    body = payload["result"]
    if mutation == "missing_flag":
        body.pop("candidate_provenance")
    elif mutation == "false_complete":
        body["candidate_provenance"]["training_complete"] = False
    elif mutation == "wrong_state":
        body["state"] = "completed"
    elif mutation == "half_grid":
        body["training_score"]["errors"].pop()
    elif mutation == "wrong_score_design":
        body["training_score"]["design"]["front_layers"][0]["thickness"]["value"] += 1
    elif mutation == "wrong_candidate":
        body["candidate_design"]["front_layers"][0]["thickness"]["value"] = 0
    elif mutation == "foreign_material":
        body["candidate_design"]["front_layers"][0]["material_id"] = "foreign"
    elif mutation == "nonfinite":
        body["training_score"]["errors"][0]["actual"] = float("nan")
    elif mutation == "wrong_aggregate":
        body["training_score"]["max_error"] = 3.
    elif mutation == "fake_audit":
        body["candidate_provenance"]["validation_matches_candidate"] = True
        body["validation_complete"] = True
        body["validation_score"] = deepcopy(body["training_score"])
    elif mutation == "fake_full_audit":
        body["candidate_provenance"]["validation_matches_candidate"] = True
        body["validation_complete"] = True
        body["validation_score"] = deepcopy(body["training_score"])
        payload["best_candidate_audit"].update(available=True, targets_met=True, audit_max_normalized_error=0.)
    elif mutation == "policy_drift":
        body["candidate_design"]["assume_lossless"] = True
    elif mutation == "bad_snapshot":
        body["frozen_request"]["frozen_material_snapshot"]["sha256"] = "0" * 64
    elif mutation == "wrong_layer_review":
        payload["review"]["layers"][0]["thickness_nm"] += 1
    assert not jobs.is_synthesis_result(payload), mutation


def test_audit_can_only_be_reused_for_the_identical_cancel_candidate():
    service, request = setup_problem(upper=.05)
    event = Event()
    result = synthesize_film_design(service, request, cancel_event=event,
        progress_callback=lambda value: event.set() if value.stage == "validation_complete" else None)
    payload = film_synthesis_result_to_payload("same-design-audit-cancel", result)
    assert jobs.is_synthesis_result(payload) and is_best_candidate_result(payload)
    payload["result"]["validation_score"]["design"]["name"] = "another candidate"
    assert not jobs.is_synthesis_result(payload)


def test_real_scheduler_cancel_preserves_current_reply_instead_of_old_checkpoint(tmp_path):
    from coat_sim.core.api import CoatService
    from coat_sim.scheduler.client import SchedulerClient
    from coat_sim.scheduler.jobs import freeze_spec
    from tests.test_film_synthesis_jobs import _cancel_payload

    root = tmp_path / "real-cancel-worker"
    client = SchedulerClient(root)
    job_id = "synthesis-current-cancel-reply"
    directory = root / "artifacts/runtime/scheduler/jobs" / job_id
    try:
        parameters = jobs.canonical_parameters(None, {"payload": _cancel_payload()})
        spec = freeze_spec(CoatService(root), "film_synthesis", parameters)
        client.submit(spec, job_id=job_id, metadata={"execution_project_root": str(root.resolve())})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = client.status(job_id)
            if (directory / "best-candidate.json").is_file() and (status.get("progress") or {}).get("heartbeat_sequence", 0) >= 1:
                break
            assert status["state"] not in {"succeeded", "failed", "cancelled"}, status
            time.sleep(.04)
        else:
            pytest.fail("real worker did not publish its audit checkpoint/heartbeat")
        client.cancel(job_id)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = client.status(job_id)
            if status["state"] in {"cancelled", "failed", "succeeded"}:
                break
            time.sleep(.04)
        assert status["state"] == "cancelled", status
        payload = client.result(job_id)
        assert jobs.is_synthesis_result(payload)
        assert payload["result"]["candidate_provenance"]["selection"] == "completed_training_at_cancel"
        assert payload["result"]["termination_reason"] != "audited_checkpoint_recovered"
        assert payload["review"]["cancel_snapshot"]["replay_performed"] is False
        assert "recovery_replay_work" not in payload.get("work_counters", {})
        outcome = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
        assert outcome["cancel_snapshot"]["source"] == "worker_completed_reply"
        # The public RPC deliberately hides the absolute business root.
        expected_public = deepcopy(outcome["result"])
        expected_public["execution_project_root"] = payload["execution_project_root"]
        assert expected_public == payload
        print("real scheduler current cancel", payload["result"]["candidate_provenance"], payload["review"]["cancel_snapshot"])
    finally:
        client.stop()


def test_real_regular_optimizer_cancel_keeps_full_worker_best_without_holdout_replay(tmp_path):
    from coat_sim.core.api import CoatService
    from coat_sim.scheduler.client import SchedulerClient
    from coat_sim.scheduler.jobs import freeze_spec
    from coat_sim.scheduler.result_capability import is_film_optimization_result
    from tests.film_performance_fixtures import fixture
    from tests.test_film_optimization_forward_cache import _request

    root = tmp_path / "real-regular-cancel-worker"
    _service, design, frozen = fixture(layers=4)
    request = replace(_request(design, frozen, solver="differential_evolution", samples=33, budget=100000),
                      max_iterations=100000, population_size=12)
    client = SchedulerClient(root)
    job_id = "regular-current-cancel-reply"
    directory = root / "artifacts/runtime/scheduler/jobs" / job_id
    try:
        spec = freeze_spec(CoatService(root), "film_optimization", {"request": request})
        client.submit(spec, job_id=job_id)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = client.status(job_id)
            if (directory / "best-candidate.json").is_file():
                break
            assert status["state"] not in {"succeeded", "failed", "cancelled"}, status
            time.sleep(.02)
        else:
            pytest.fail("regular worker did not publish its complete baseline")
        client.cancel(job_id)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = client.status(job_id)
            if status["state"] in {"cancelled", "failed", "succeeded"}:
                break
            time.sleep(.04)
        assert status["state"] == "cancelled", status
        payload = client.result(job_id)
        assert is_film_optimization_result(payload)
        assert payload["state"] == "cancelled"
        assert payload["candidate"]["per_condition_errors"]
        assert payload["solver_validation"]["validation_complete"] is False
        assert payload["solver_validation"]["validation_evaluations"] == 0
        assert payload["execution"]["cancel_snapshot"]["source"] == "worker_completed_reply"
        outcome = json.loads((directory / "outcome.json").read_text(encoding="utf-8"))
        assert outcome["result"] == payload
        assert outcome["cancel_snapshot"]["replay_performed"] is False
        print("regular complete cancel", payload["candidate"]["evaluations"], payload["execution"]["cancel_snapshot"])
    finally:
        client.stop()


@pytest.mark.parametrize("kind", ["film_synthesis", "film_optimization"])
def test_daemon_cancel_without_complete_reply_does_not_replay_checkpoint(monkeypatch, kind):
    from coat_sim.scheduler.daemon import Scheduler
    from coat_sim.scheduler import result_capability

    scheduler = Scheduler.__new__(Scheduler)
    monkeypatch.setattr(scheduler, "memory_status", lambda _job: {"kind": kind})
    monkeypatch.setattr(result_capability, "load_best_candidate_checkpoint",
        lambda *_a, **_k: pytest.fail("active cancellation must not replay a historical candidate"))
    outcome = scheduler._cancelled_outcome("current", {"state": "cancelled", "result": None})
    assert outcome["state"] == "cancelled" and outcome["result"] is None
    assert "without cancellation replay" in outcome["result_unavailable_reason"]


def test_daemon_current_cancel_preserves_exact_completed_training_candidate(monkeypatch, tmp_path):
    from coat_sim.scheduler.daemon import Scheduler
    from coat_sim.scheduler import result_capability

    payload = _cancelled_payload()
    from coat_sim.core.api import CoatService
    from coat_sim.scheduler.jobs import JobSpec, canonical, freeze_spec
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.runtime = tmp_path / "runtime"
    directory = scheduler.runtime / "jobs" / "current"
    directory.mkdir(parents=True)
    request_payload = {"schema_version": 5, "kind": "coat_sim.film_synthesis_request",
        "request_id": payload["request_id"], "request": payload["result"]["frozen_request"], "physical_structures": []}
    parameters = jobs.canonical_parameters(None, {"payload": request_payload})
    spec = freeze_spec(CoatService(tmp_path), "film_synthesis", parameters)
    (directory / "spec.json").write_text(canonical(spec), encoding="utf-8")
    digest = JobSpec.parse_frozen_json(canonical(spec)).fingerprint
    monkeypatch.setattr(scheduler, "memory_status", lambda _job: {"kind": "film_synthesis", "input_digest": digest})
    monkeypatch.setattr(result_capability, "load_best_candidate_checkpoint",
        lambda *_a, **_k: pytest.fail("older audited checkpoint must not replace current cancel snapshot"))
    outcome = scheduler._cancelled_outcome("current", {"state": "cancelled", "result": payload})
    assert outcome["result"] == payload
    assert outcome["result"]["result"]["validation_score"] is None
    attached = scheduler._attach_best_candidate_checkpoint("current", outcome)
    assert attached["result"] == payload


@pytest.mark.parametrize("mutation", ["snapshot", "objective", "request_id", "queue_digest"])
def test_cancel_reply_must_match_current_persisted_job_not_only_itself(monkeypatch, tmp_path, mutation):
    from coat_sim.core.api import CoatService
    from coat_sim.scheduler.daemon import Scheduler
    from coat_sim.scheduler.jobs import JobSpec, canonical, freeze_spec
    from coat_sim.scheduler.worker import _film_reply_matches_payload

    actual = _cancelled_payload()
    environment = deepcopy(actual["result"]["frozen_request"])
    if mutation == "snapshot":
        # A second entirely valid task has another frozen source dataset.
        service, request = setup_problem(upper=.03)
        service.materials.records["glass"]["optical_sets"][0]["n"] = [1.6] * 3
        request = freeze_film_synthesis_request(service, replace(request,
            seed_designs=(deepcopy(request.environment),), max_evaluations=None))
        from dataclasses import asdict
        environment = asdict(request)
    elif mutation == "objective":
        environment["objectives"][0]["pointwise"]["upper_bound"] = .02
    request_payload = {"schema_version": 5, "kind": "coat_sim.film_synthesis_request",
        "request_id": "another-valid-task" if mutation == "request_id" else actual["request_id"],
        "request": environment, "physical_structures": []}
    parameters = jobs.canonical_parameters(None, {"payload": request_payload})
    spec = freeze_spec(CoatService(tmp_path), "film_synthesis", parameters)
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.runtime = tmp_path / "runtime"
    directory = scheduler.runtime / "jobs" / "current"
    directory.mkdir(parents=True)
    (directory / "spec.json").write_text(canonical(spec), encoding="utf-8")
    digest = JobSpec.parse_frozen_json(canonical(spec)).fingerprint
    monkeypatch.setattr(scheduler, "memory_status", lambda _job: {"kind": "film_synthesis",
        "input_digest": "0" * 64 if mutation == "queue_digest" else digest})
    assert jobs.is_synthesis_result(actual), "the other task is internally valid"
    if mutation != "queue_digest":
        assert not _film_reply_matches_payload(actual, parameters["payload"])
    outcome = scheduler._cancelled_outcome("current", {"state": "cancelled", "result": actual})
    assert outcome["result"] is None
    assert "without cancellation replay" in outcome["result_unavailable_reason"]


@pytest.mark.parametrize("kind", ["film_synthesis", "film_optimization"])
def test_remote_cancel_without_reply_uses_parent_guard_without_checkpoint_replay(monkeypatch, kind):
    from coat_sim.scheduler.remote_server import RemoteScheduler
    from coat_sim.scheduler import result_capability

    scheduler = RemoteScheduler.__new__(RemoteScheduler)
    monkeypatch.setattr(scheduler, "memory_status", lambda _job: {"kind": kind})
    monkeypatch.setattr(result_capability, "load_best_candidate_checkpoint",
        lambda *_a, **_k: pytest.fail("remote active cancel cannot replay old checkpoint"))
    result = scheduler._attach_best_candidate_checkpoint("current", {"state": "cancelled", "result": None})
    assert result["result"] is None and "without cancellation replay" in result["result_unavailable_reason"]


def test_remote_current_regular_reply_is_bound_and_preserved_without_replay(monkeypatch, tmp_path):
    from coat_sim.core.api import CoatService
    from coat_sim.core.film_optimization_job import execute_film_optimization_job
    from coat_sim.scheduler.remote_server import RemoteScheduler
    from coat_sim.scheduler.jobs import JobSpec, canonical, freeze_spec
    from coat_sim.scheduler.worker import _completed_film_cancel_reply
    from tests.film_performance_fixtures import fixture
    from tests.test_film_optimization_forward_cache import _request

    service, design, frozen = fixture(layers=4)
    request = _request(design, frozen)
    spec = freeze_spec(CoatService(tmp_path), "film_optimization", {"request": request})
    event = Event()
    event.set()
    reply = execute_film_optimization_job(service, spec["parameters"]["payload"], cancel_event=event)
    saved = _completed_film_cancel_reply(reply, spec["parameters"]["payload"])
    assert saved is not None
    scheduler = RemoteScheduler.__new__(RemoteScheduler)
    scheduler.runtime = tmp_path / "runtime"
    directory = scheduler.runtime / "jobs" / "current"
    directory.mkdir(parents=True)
    (directory / "spec.json").write_text(canonical(spec), encoding="utf-8")
    digest = JobSpec.parse_frozen_json(canonical(spec)).fingerprint
    monkeypatch.setattr(scheduler, "memory_status", lambda _job: {"kind": "film_optimization", "input_digest": digest})
    result = scheduler._attach_best_candidate_checkpoint("current", {"state": "cancelled", "result": saved})
    assert result["result"] == saved
    assert result["result"]["candidate"]["per_condition_errors"]


def test_current_structure_cancel_preserves_complete_training_without_replay(monkeypatch):
    from coat_sim.core.film_optimization_job import execute_film_optimization_job
    from coat_sim.core import film_optimization_structure as structure
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    from coat_sim.scheduler.result_capability import is_film_optimization_result
    from tests.film_performance_fixtures import fixture
    from tests.test_film_optimization_forward_cache import _request

    service, design, frozen = fixture(layers=4)
    # This local insertion pool explicitly authorizes the synthetic high film.
    from coat_sim.core.film_process_import import _sha256
    next(row for row in frozen["materials"] if row["id"] == "high")["design_ready"] = True
    frozen["sha256"] = _sha256(frozen)
    request = structure.FilmStructureOptimizationRequest(_request(design, frozen),
        candidate_materials=(FilmNeedleMaterialReference("high", "optical"),),
        position_samples=1, candidate_budget=2)
    frozen_request = structure.freeze_film_structure_optimization_request(service, request)
    job = {"schema_version": 2, "kind": "coat_sim.film_optimization_job",
        "request": structure.film_structure_request_to_payload(frozen_request)}
    event = Event()

    def progress(value):
        if value.message == "scored the frozen base design":
            event.set()

    monkeypatch.setattr(structure, "validate_film_structure_payload",
        lambda *_a, **_k: pytest.fail("current structure cancel must not replay TMM/holdout"))
    payload = execute_film_optimization_job(service, job, cancel_event=event, progress_callback=progress)
    assert payload["state"] == "cancelled" and payload["candidate"]["per_condition_errors"]
    assert payload["candidate"]["quality"]["validation_complete"] is False
    assert payload["partial_result"]["candidate_verified"] is False
    assert payload["partial_result"]["is_final"] is True
    marker = payload["structure"]["topology_audit"]["cancel_snapshot"]
    assert marker == {"source": "worker_completed_reply", "replay_performed": False,
        "candidate_available": True, "training_complete": True}
    assert is_film_optimization_result(payload)
    decoded = structure.film_structure_result_from_payload(payload)
    assert decoded.candidate_verified is False and decoded.base_result.validation_complete is False
    assert decoded.best_design == design
    for mutation in ("missing_marker", "unselected_layer", "wrong_score"):
        invalid = deepcopy(payload)
        if mutation == "missing_marker":
            invalid["structure"]["topology_audit"].pop("cancel_snapshot")
        elif mutation == "unselected_layer":
            invalid["candidate"]["design"]["front_layers"][1]["thickness"]["value"] += 1
        else:
            invalid["candidate"]["quality"]["max_normalized_error"] += .1
        assert not is_film_optimization_result(invalid), mutation
    print("structure complete cancel", marker, payload["structure"]["counters"])


def test_structure_cancel_accepted_after_core_completed_retains_complete_real_candidate(monkeypatch):
    from coat_sim.core.film_optimization_job import execute_film_optimization_job
    from coat_sim.core import film_optimization_structure as structure
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    from coat_sim.core.film_process_import import _sha256
    from coat_sim.scheduler.result_capability import is_film_optimization_result
    from tests.film_performance_fixtures import fixture
    from tests.test_film_optimization_forward_cache import _request

    service, design, frozen = fixture(layers=4)
    next(row for row in frozen["materials"] if row["id"] == "high")["design_ready"] = True
    frozen["sha256"] = _sha256(frozen)
    request = structure.FilmStructureOptimizationRequest(_request(design, frozen),
        candidate_materials=(FilmNeedleMaterialReference("high", "optical"),),
        position_samples=1, candidate_budget=2)
    request = structure.freeze_film_structure_optimization_request(service, request)
    job = {"schema_version": 2, "kind": "coat_sim.film_optimization_job",
        "request": structure.film_structure_request_to_payload(request)}
    original = structure.optimize_film_design_with_structure
    event = Event()
    completed = []

    def complete_then_cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.state == "completed" and result.candidate_verified is False
        completed.append(result)
        event.set()  # Real completion-boundary race, before job-wrapper replay.
        return result

    monkeypatch.setattr(structure, "optimize_film_design_with_structure", complete_then_cancel)
    monkeypatch.setattr(structure, "validate_film_structure_payload",
        lambda *_a, **_k: pytest.fail("late cancel must not add a physical replay"))
    payload = execute_film_optimization_job(service, job, cancel_event=event)
    assert completed and payload["state"] == "cancelled"
    assert payload["candidate"]["design"] == completed[0].best_design
    assert payload["candidate"]["per_condition_errors"]
    assert payload["candidate"]["quality"]["validation_complete"] is completed[0].base_result.validation_complete
    assert payload["partial_result"]["candidate_verified"] is False
    assert payload["partial_result"]["is_final"] is completed[0].is_final
    assert payload["structure"]["topology_audit"]["core_terminal_state_before_cancel"] == "completed"
    assert is_film_optimization_result(payload)
    print("late complete structure", payload["candidate"]["quality"], payload["structure"]["topology_audit"]["cancel_snapshot"])
