"""Current queued AUTO controls retain identity; incomplete formats are rejected."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from threading import Event

import pytest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core import compute_resources
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload, canonical_parameters, execute_job, is_synthesis_result,
)
from coat_sim.scheduler.jobs import JobSpec, canonical
from coat_sim.scheduler.worker import _film_reply_matches_payload
from tests.test_film_auto_synthesis import setup_problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def _frozen_job(monkeypatch):
    probes = []
    def measure_memory():
        probes.append("memory")
        return 8 * 1024**3
    monkeypatch.setattr(compute_resources, "available_memory_bytes", measure_memory)
    service, request = setup_problem(upper=.05)
    assert request.parallel_batch_size is None and request.resource_guard_resolution is None
    request = freeze_film_synthesis_request(service, replace(request,
        search_strategy="hybrid", cpu_workers=1, max_evaluations=3, max_working_set_bytes=0))
    envelope = film_synthesis_request_to_payload("persisted-auto", request)
    job = build_job_payload(canonical_parameters(None, {"payload": envelope}))
    assert probes == ["memory"]
    return job, probes


def test_current_persisted_job_execution_preserves_exact_identity(tmp_path, monkeypatch):
    job, probes = _frozen_job(monkeypatch)
    spec = JobSpec.parse_frozen_json(canonical(JobSpec("film_synthesis", {"payload": job}, {}).to_dict())).to_spec()
    before = canonical(spec.to_dict())
    fingerprint = spec.fingerprint
    def write_json(path, value):
        Path(path).write_text(json.dumps(value), encoding="utf-8")
    cancel = Event()
    response = execute_job(spec.parameters, cancel, lambda *args, **kwargs: None,
        tmp_path / "checkpoint.json", write_json, str(tmp_path))
    assert canonical(spec.to_dict()) == before
    assert spec.fingerprint == fingerprint
    assert not cancel.is_set()
    assert response["result"]["state"] == "completed" and response["result"]["targets_met"]
    assert response["result"]["evaluations"] <= job["request"]["request"]["max_evaluations"] == 3
    assert is_synthesis_result(response)
    assert _film_reply_matches_payload(response, job)
    assert canonical(response["result"]["frozen_request"]) == canonical(job["request"]["request"])
    resolved = response["result"]["performance"]["resolved_execution_controls"]
    assert resolved["parallel_batch_size"] == 1
    assert resolved["resource_guard_resolution"]["effective_bytes"] == 4 * 1024**3
    assert probes == ["memory"]
    wrong_batch = deepcopy(response)
    wrong_batch["result"]["frozen_request"]["parallel_batch_size"] = 2
    assert not _film_reply_matches_payload(wrong_batch, job)
    wrong_guard = deepcopy(response)
    wrong_guard["result"]["frozen_request"]["resource_guard_resolution"]["effective_bytes"] += 1
    assert not _film_reply_matches_payload(wrong_guard, job)
    wrong_target = deepcopy(response)
    wrong_target["result"]["frozen_request"]["objectives"][0]["pointwise"]["upper_bound"] = .051
    assert not _film_reply_matches_payload(wrong_target, job)
    wrong_optics = deepcopy(response)
    wrong_optics["result"]["frozen_request"]["environment"]["incident_medium"]["n"] = 1.01
    assert not _film_reply_matches_payload(wrong_optics, job)


@pytest.mark.parametrize("missing", (("parallel_batch_size",), ("resource_guard_resolution",),
    ("parallel_batch_size", "resource_guard_resolution"), ("search_strategy",)))
def test_incomplete_persisted_current_job_is_rejected_without_reconstructing_defaults(tmp_path, monkeypatch, missing):
    job, probes = _frozen_job(monkeypatch)
    for name in missing:
        del job["request"]["request"][name]
    spec = JobSpec.parse_frozen_json(canonical(JobSpec("film_synthesis", {"payload": job}, {}).to_dict())).to_spec()
    before, fingerprint = canonical(spec.to_dict()), spec.fingerprint
    effects = []
    cancel = Event()
    with pytest.raises(ValueError, match="every field"):
        execute_job(spec.parameters, cancel, lambda *args, **kwargs: effects.append("report"),
            tmp_path / "checkpoint.json", lambda *args: effects.append("write"), str(tmp_path))
    assert canonical(spec.to_dict()) == before and spec.fingerprint == fingerprint
    assert probes == ["memory"] and effects == []
    assert not cancel.is_set() and not (tmp_path / "checkpoint.json").exists()


def test_schema_one_persisted_job_is_rejected_without_compatibility_fallback(tmp_path, monkeypatch):
    job, probes = _frozen_job(monkeypatch)
    job["request"]["schema_version"] = 1
    before = canonical(job)
    effects = []
    with pytest.raises(ValueError, match="schema_version"):
        execute_job({"payload": job}, Event(), lambda *args, **kwargs: effects.append("report"),
            tmp_path / "checkpoint.json", lambda *args: effects.append("write"), str(tmp_path))
    assert canonical(job) == before
    assert probes == ["memory"] and effects == []
