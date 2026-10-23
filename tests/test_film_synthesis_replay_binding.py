"""Physical history proof is bound to immutable inputs, not self-consistent JSON."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import threading
from typing import cast

import pytest

from coat_sim.core.film_auto_synthesis import (
    _proxy, freeze_film_synthesis_request, synthesize_film_design,
)
from coat_sim.core.film_design import validate_design
from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
from coat_sim.core.film_synthesis_io import film_synthesis_result_to_payload
from coat_sim.core.film_synthesis_jobs import is_synthesis_result, replay_synthesis_result
from coat_sim.scheduler.jobs import JobSpec, canonical
from coat_sim.scheduler.worker import _film_reply_matches_payload
from coat_sim.ui.compute_queue import (
    _SynthesisQueueResult, _annotate_result_availability, _read_queue_result,
)
from tests.test_film_auto_synthesis import setup_problem
from tests.test_film_synthesis_nominal_surfaces import _FileStore, _bind_row, _route_legacy

pytestmark = [pytest.mark.contract]


def _history(root, *, upper=.05, low=550., high=550., samples=1, legacy=False):
    service, request = setup_problem(upper=upper, low=low, high=high, samples=samples)
    if upper < .04:
        request = freeze_film_synthesis_request(service, replace(request,
            search_strategy="hybrid", max_evaluations=3, max_cycles=1))
    result = synthesize_film_design(service, request)
    payload = film_synthesis_result_to_payload("bound-history", result)
    if legacy:
        payload.pop("acceptance_semantics")
        payload["result"].pop("solver_acceptable")
        payload["result"]["frozen_request"].pop("strict_upper_bound_objective_indices")
    row = {"job_id": "bound-history-job", "kind": "film_synthesis", "state": "succeeded",
        "result": payload, "origin": {"project_id": "project-a", "project_root": "/project-a"}}
    _bind_row(root, row)
    path = root / "history.json"
    path.write_text(json.dumps(row), encoding="utf-8")
    return row, path, result


def _persist(path, row):
    path.write_text(json.dumps(row), encoding="utf-8")


def _coordinated_candidate(result):
    candidate = deepcopy(result.candidate_design)
    candidate["front_layers"] = [{"id": "legal-but-stale-spectrum", "material_id": "high",
        "optical_set_id": "nk", "thickness": {"value": 550. / (4 * 2.2), "unit": "nm"}}]
    candidate = validate_design(candidate)
    training, validation = deepcopy(result.training_score), deepcopy(result.validation_score)
    training["design"] = deepcopy(candidate)
    validation["design"] = deepcopy(candidate)
    return replace(result, candidate_design=candidate, training_score=training,
                   validation_score=validation)


@pytest.mark.parametrize("mutation", ["incident_medium", "coordinated_designs"])
@pytest.mark.parametrize("legacy", [False, True])
def test_stale_saved_spectra_never_open_or_turn_green(tmp_path, mutation, legacy):
    row, path, result = _history(tmp_path, legacy=legacy)
    if mutation == "incident_medium":
        row["result"]["result"]["candidate_design"]["incident_medium"]["n"] = 4.
        assert not is_synthesis_result(row["result"])
    else:
        forged = _coordinated_candidate(result)
        true_score = evaluate_frozen_structure_candidate(_proxy(result.frozen_request),
            forged.candidate_design, derived_validation_grid=False)["score"]
        assert true_score["errors"][0].actual > .05
        row["result"] = film_synthesis_result_to_payload("bound-history", forged)
        assert row["result"]["result"]["training_score"]["errors"][0]["actual"] == pytest.approx(.04)
        assert is_synthesis_result(row["result"])  # Cheap structure is not physical proof.
        if legacy:
            row["result"].pop("acceptance_semantics")
            row["result"]["result"].pop("solver_acceptable")
            row["result"]["result"]["frozen_request"].pop("strict_upper_bound_objective_indices")
    _persist(path, row)
    original = path.read_bytes()
    listed = deepcopy(row)
    _annotate_result_availability(tmp_path, [listed])
    assert listed["resultAvailable"] is False
    assert listed["synthesisNominalStatus"] == "unknown"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_read_queue_result, None, _FileStore(path), row["job_id"])
        with pytest.raises(ValueError, match="physical|frozen|identity|safely"):
            future.result(timeout=15)
    assert path.read_bytes() == original


@pytest.mark.parametrize("mutation", ["material", "topology", "units", "score_design", "revision", "snapshot"])
def test_cheap_identity_rejects_frozen_physical_contract_violations(tmp_path, mutation, monkeypatch):
    row, _path, _result = _history(tmp_path)
    payload = row["result"]
    body = payload["result"]
    if mutation == "material":
        for name in ("candidate_design",):
            body[name]["front_layers"] = [{"id": "foreign", "material_id": "glass",
                "optical_set_id": "nk", "thickness": {"value": 50., "unit": "nm"}}]
    elif mutation == "topology":
        body["candidate_design"]["back_layers"] = [{"id": "foreign-back", "material_id": "high",
            "optical_set_id": "nk", "thickness": {"value": 50., "unit": "nm"}}]
    elif mutation == "units":
        body["candidate_design"]["front_layers"] = [{"id": "wrong-unit", "material_id": "high",
            "optical_set_id": "nk", "thickness": {"value": .05, "unit": "um"}}]
    elif mutation == "score_design":
        body["training_score"]["design"]["incident_medium"]["n"] = 4.
    elif mutation == "revision":
        body["design_revision"] += 1
    else:
        body["frozen_request"]["frozen_material_snapshot"] = None
    import coat_sim.core.film_synthesis_jobs as jobs
    import coat_sim.core.film_synthesis_audit as audit
    def forbidden(*_args, **_kwargs):
        raise AssertionError("cheap validation must not run TMM/audit")
    monkeypatch.setattr(jobs, "evaluate_frozen_structure_candidate", forbidden)
    monkeypatch.setattr(audit, "audit_frozen_synthesis_candidate", forbidden)
    assert not is_synthesis_result(payload)


@pytest.mark.parametrize("mutation", ["digest", "request_id", "spec", "missing_spec", "wrong_job", "kind", "envelope"])
def test_history_requires_real_matching_jobspec_before_replay(tmp_path, mutation, monkeypatch):
    row, path, _result = _history(tmp_path)
    spec_path = tmp_path / "artifacts/runtime/scheduler/jobs" / row["job_id"] / "spec.json"
    if mutation == "digest":
        row["input_digest"] = "forged"
    elif mutation == "request_id":
        row["result"]["request_id"] = "foreign-request"
    elif mutation == "spec":
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["inputs"]["changed"] = "new-input"
        spec_path.write_text(canonical(spec), encoding="utf-8")
    elif mutation == "missing_spec":
        spec_path.unlink()
    elif mutation == "wrong_job":
        row["job_id"] = "other-job"
    elif mutation == "envelope":
        data = json.loads(spec_path.read_text(encoding="utf-8"))
        data["parameters"]["payload"]["kind"] = "forged-job-envelope"
        spec = JobSpec.parse(data)
        spec_path.write_text(canonical(spec.to_dict()), encoding="utf-8")
        row["input_digest"] = spec.fingerprint
    else:
        row["kind"] = "film_optimization"
    _persist(path, row)
    import coat_sim.core.film_synthesis_jobs as jobs
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unbound source must be rejected before replay")
    monkeypatch.setattr(jobs, "replay_synthesis_result", forbidden)
    _annotate_result_availability(tmp_path, [row])
    assert not row["resultAvailable"] and row["synthesisNominalStatus"] != "passed"
    with pytest.raises(ValueError):
        _read_queue_result(None, _FileStore(path), row["job_id"])


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_queue_lifecycle_cannot_reuse_completed_success(tmp_path, state):
    row, path, _result = _history(tmp_path)
    row["state"] = state
    _persist(path, row)
    _annotate_result_availability(tmp_path, [row])
    assert row["resultAvailable"] is False
    assert row["synthesisNominalStatus"] != "passed"
    with pytest.raises(ValueError, match="lifecycle"):
        _read_queue_result(None, _FileStore(path), row["job_id"])


def test_background_reader_replays_current_and_rechecks_source_before_routing(tmp_path, monkeypatch):
    row, path, _result = _history(tmp_path)
    original = path.read_bytes()
    import coat_sim.core.film_synthesis_jobs as jobs
    replay = jobs.replay_synthesis_result
    thread_ids = []
    def recorded(value):
        thread_ids.append(threading.get_ident())
        return replay(value)
    monkeypatch.setattr(jobs, "replay_synthesis_result", recorded)
    with ThreadPoolExecutor(max_workers=1) as pool:
        loaded, asset = pool.submit(_read_queue_result, None, _FileStore(path), row["job_id"]).result(timeout=15)
    assert isinstance(loaded, _SynthesisQueueResult) and asset == ""
    assert thread_ids and thread_ids[0] != threading.get_ident()
    assert loaded.payload["physical_replay_verification"]["physical_replay_performed"] is True
    assert loaded.source_job == json.loads(original)
    queue, inspected = _route_legacy(_FileStore(path), loaded)
    assert inspected and "read-only" in queue.status
    spec_path = tmp_path / "artifacts/runtime/scheduler/jobs" / row["job_id"] / "spec.json"
    spec = JobSpec("film_synthesis", {}, {})
    spec_path.write_text(canonical(spec.to_dict()), encoding="utf-8")
    queue, inspected = _route_legacy(_FileStore(path), loaded)
    assert not inspected and "Unable to open" in queue.status
    assert path.read_bytes() == original


@pytest.mark.parametrize("change", ["result", "snapshot", "targets", "grid", "layer_id", "spec", "state", "running", "queued", "digest", "root", "job", "malformed_result", "missing_result"])
def test_small_status_cache_never_authorizes_changed_source(tmp_path, monkeypatch, change):
    row, _path, result = _history(tmp_path)
    cache: OrderedDict[str, dict[str, object]] = OrderedDict()
    import coat_sim.core.film_synthesis_jobs as jobs
    replay = jobs.replay_synthesis_result
    calls = []
    def recorded(value):
        calls.append(1)
        return replay(value)
    monkeypatch.setattr(jobs, "replay_synthesis_result", recorded)
    _annotate_result_availability(tmp_path, [row], synthesis_cache=cache)
    assert row["synthesisNominalStatus"] == "passed" and len(calls) == 1
    _annotate_result_availability(tmp_path, [row], synthesis_cache=cache)
    assert len(calls) == 1
    root = tmp_path
    if change == "result":
        row["result"] = film_synthesis_result_to_payload("bound-history", _coordinated_candidate(result))
    elif change == "snapshot":
        snapshot = row["result"]["result"]["frozen_request"]["frozen_material_snapshot"]
        snapshot["materials"][0]["name"] += " changed"
    elif change in {"targets", "grid"}:
        objective = row["result"]["result"]["frozen_request"]["objectives"][0]
        objective["upper_bound" if change == "targets" else "wavelength_min_nm"] = .03 if change == "targets" else 549.
    elif change == "layer_id":
        row["result"] = film_synthesis_result_to_payload("bound-history", _coordinated_candidate(result))
        row["result"]["result"]["candidate_design"]["front_layers"][0]["id"] = "changed-id"
    elif change == "spec":
        spec_path = tmp_path / "artifacts/runtime/scheduler/jobs" / row["job_id"] / "spec.json"
        spec_path.write_text(canonical(JobSpec("film_synthesis", {}, {}).to_dict()), encoding="utf-8")
    elif change in {"state", "running", "queued"}:
        row["state"] = "cancelled" if change == "state" else change
    elif change == "digest":
        row["input_digest"] = "changed"
    elif change == "root":
        root = tmp_path / "other-root"
    elif change == "malformed_result":
        row["result"] = None
    elif change == "missing_result":
        row.pop("result")
    else:
        row["job_id"] = "other-job"
    _annotate_result_availability(root, [row], synthesis_cache=cache)
    assert row["synthesisNominalStatus"] != "passed" and not row["resultAvailable"]
    assert all("result" not in value and "payload" not in value for value in cache.values())
    assert len(cache) <= 64


def test_replay_compares_every_actual_not_just_passing_aggregates(tmp_path):
    row, _path, _result = _history(tmp_path, low=540., high=560., samples=3)
    payload = row["result"]
    assert payload["result"]["training_score"]["max_error"] == 0.
    payload["result"]["training_score"]["errors"][1]["actual"] = .041
    # Still below .05: max/RMS and normalized/weighted violations remain zero.
    assert is_synthesis_result(payload)
    with pytest.raises(ValueError, match="physical condition does not replay"):
        replay_synthesis_result(payload)


@pytest.mark.parametrize("upper,status", [(.05, "passed"), (.0395, "failed")])
def test_listing_background_proof_preserves_nominal_solver_split(tmp_path, upper, status, monkeypatch):
    row, path, _result = _history(tmp_path, upper=upper)
    import coat_sim.core.film_synthesis_jobs as jobs
    replay = jobs.replay_synthesis_result
    threads = []
    def recorded(value):
        threads.append(threading.get_ident())
        return replay(value)
    monkeypatch.setattr(jobs, "replay_synthesis_result", recorded)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_annotate_result_availability, tmp_path, [row]).result(timeout=15)
    assert threads and all(thread != threading.get_ident() for thread in threads)
    assert row["synthesisNominalStatus"] == status
    assert row["resultAvailable"] is True
    assert "求解容差可接受：通过" in row["synthesisAcceptanceSummary"]
    assert canonical(json.loads(path.read_text(encoding="utf-8"))["result"]) == canonical(row["result"])


@pytest.mark.parametrize("legacy", [False, True])
def test_old_optional_execution_field_keeps_original_frozen_identity(tmp_path, legacy):
    row, path, result = _history(tmp_path, legacy=legacy)
    body = row["result"]["result"]
    body["frozen_request"].pop("resource_guard_resolution")
    # Persist a genuine old JobSpec, rather than running today's submission
    # builder, which intentionally resolves newly submitted execution defaults.
    spec_path = tmp_path / "artifacts/runtime/scheduler/jobs" / row["job_id"] / "spec.json"
    original_spec = JobSpec.parse_frozen_json(spec_path.read_text(encoding="utf-8"))
    parameters = deepcopy(original_spec.parameters)
    parameters["payload"]["request"]["request"].pop("resource_guard_resolution")
    spec = JobSpec("film_synthesis", parameters, original_spec.inputs)
    spec_path.write_text(canonical(spec.to_dict()), encoding="utf-8")
    row["input_digest"] = spec.fingerprint
    _persist(path, row)
    original = path.read_bytes()
    loaded, _ = _read_queue_result(None, _FileStore(path), row["job_id"])
    assert isinstance(loaded, _SynthesisQueueResult)
    assert loaded.payload["result"]["frozen_request"]["resource_guard_resolution"] is None
    assert _film_reply_matches_payload(loaded.payload, spec.parameters["payload"])
    queue, inspected = _route_legacy(_FileStore(path), loaded)
    assert inspected and "read-only" in queue.status
    assert path.read_bytes() == original
    # A persisted measured resolution cannot impersonate the immutable old
    # None/default identity, even if it is a valid policy on this host.
    body["frozen_request"]["resource_guard_resolution"] = deepcopy(
        result.frozen_request.resource_guard_resolution)
    _persist(path, row)
    _annotate_result_availability(tmp_path, [row])
    assert not row["resultAvailable"] and row["synthesisNominalStatus"] != "passed"
    with pytest.raises(ValueError, match="binding"):
        _read_queue_result(None, _FileStore(path), row["job_id"])


def test_actual_queue_page_runs_replay_on_its_background_worker(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from coat_sim.ui.compute_queue import ComputeQueueView
    row, _path, _result = _history(tmp_path)
    fake_store = SimpleNamespace(root=tmp_path,
        queue_jobs_page=lambda *_args, **_kwargs: {"jobs": [deepcopy(row)]})
    fake_view = SimpleNamespace(store=fake_store, PAGE_SIZE=12,
        _owns_job=lambda _row: True, _synthesis_replay_cache=OrderedDict())
    import coat_sim.core.film_synthesis_jobs as jobs
    replay = jobs.replay_synthesis_result
    calls = []
    def recorded(value):
        calls.append(threading.get_ident())
        return replay(value)
    monkeypatch.setattr(jobs, "replay_synthesis_result", recorded)
    with ThreadPoolExecutor(max_workers=1) as pool:
        page = pool.submit(ComputeQueueView._read_page, cast(ComputeQueueView, fake_view), "history", None).result(timeout=15)
    assert calls and all(thread != threading.get_ident() for thread in calls)
    assert page["jobs"][0]["synthesisNominalStatus"] == "passed"
    assert page["jobs"][0]["resultAvailable"]


def test_replay_cache_bounds_small_statuses_and_does_not_trust_persisted_flags(tmp_path, monkeypatch):
    row, _path, result = _history(tmp_path)
    cache: OrderedDict[str, dict[str, object]] = OrderedDict()
    for index in range(66):
        current = deepcopy(row)
        current["job_id"] = f"bound-history-{index}"
        _bind_row(tmp_path, current)
        _annotate_result_availability(tmp_path, [current], synthesis_cache=cache)
        assert current["synthesisNominalStatus"] == "passed"
    assert len(cache) == 64
    assert all(set(value) == {"resultAvailable", "resultUnavailableReason",
        "synthesisNominalStatus", "synthesisAcceptanceSummary"} for value in cache.values())
    import coat_sim.core.film_synthesis_jobs as jobs
    replay = jobs.replay_synthesis_result
    calls = []
    def recorded(value):
        calls.append(1)
        return replay(value)
    monkeypatch.setattr(jobs, "replay_synthesis_result", recorded)
    row["result"] = film_synthesis_result_to_payload("bound-history", _coordinated_candidate(result))
    row["result"]["physical_replay_verification"] = {
        "physical_replay_performed": True, "read_only": True}
    row["result"]["legacy_compatibility"] = {"physical_replay_performed": True}
    _annotate_result_availability(tmp_path, [row], synthesis_cache=cache)
    assert not row["resultAvailable"] and row["synthesisNominalStatus"] != "passed"
    assert len(calls) == 1
    _annotate_result_availability(tmp_path, [row], synthesis_cache=cache)
    assert len(calls) == 1  # An unchanged physical failure is safely cached too.
    assert not row["resultAvailable"]
    row["result"]["review"]["warnings"].append("changed original source")
    _annotate_result_availability(tmp_path, [row], synthesis_cache=cache)
    assert len(calls) == 2
    assert len(cache) <= 64
