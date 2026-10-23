"""Old queued AUTO controls cannot rewrite or weaken immutable job identity."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from threading import Event

import pytest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
from coat_sim.core.film_synthesis_jobs import (
    build_job_payload, canonical_parameters, execute_job, is_synthesis_result,
)
from coat_sim.scheduler.jobs import JobSpec, canonical
from coat_sim.scheduler.worker import _film_reply_matches_payload
from tests.test_film_auto_synthesis import setup_problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


@pytest.mark.parametrize("missing", ((), ("parallel_batch_size",),
    ("resource_guard_resolution",), ("parallel_batch_size", "resource_guard_resolution")))
def test_old_persisted_job_execution_preserves_exact_identity(tmp_path, missing):
    service, request = setup_problem(upper=.05)
    request = freeze_film_synthesis_request(service, replace(request,
        search_strategy="hybrid", cpu_workers=1, max_evaluations=3))
    envelope = film_synthesis_request_to_payload("persisted-auto", request)
    job = build_job_payload(canonical_parameters(None, {"payload": envelope}))
    for name in missing:
        job["request"]["request"].pop(name)
    spec = JobSpec.parse_frozen_json(canonical(JobSpec("film_synthesis", {"payload": job}, {}).to_dict())).to_spec()
    before = canonical(spec.to_dict())
    def write_json(path, value):
        Path(path).write_text(json.dumps(value), encoding="utf-8")
    response = execute_job(spec.parameters, Event(), lambda *args, **kwargs: None,
        tmp_path / "checkpoint.json", write_json, str(tmp_path))
    assert canonical(spec.to_dict()) == before
    assert response["result"]["state"] == "completed" and response["result"]["targets_met"]
    assert is_synthesis_result(response)
    assert _film_reply_matches_payload(response, job)
    resolved = response["result"]["performance"]["resolved_execution_controls"]
    assert resolved["parallel_batch_size"] == 1
    assert resolved["resource_guard_resolution"]["effective_bytes"] > 0
    for name in missing:
        assert response["result"]["frozen_request"][name] is None
        injected = deepcopy(response)
        injected["result"]["frozen_request"][name] = deepcopy(resolved[name])
        assert not _film_reply_matches_payload(injected, job)
    wrong_target = deepcopy(response)
    wrong_target["result"]["frozen_request"]["objectives"][0]["upper_bound"] = .051
    assert not _film_reply_matches_payload(wrong_target, job)
    wrong_optics = deepcopy(response)
    wrong_optics["result"]["frozen_request"]["environment"]["incident_medium"]["n"] = 1.01
    assert not _film_reply_matches_payload(wrong_optics, job)
