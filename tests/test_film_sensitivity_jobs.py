"""Removed sensitivity products fail before job creation or checkpoint recovery."""
from importlib.util import find_spec
import json
from types import SimpleNamespace
from typing import cast

import pytest

from coat_sim.core import film_sensitivity_workflow_jobs as jobs
from coat_sim.core import compute_estimation
from coat_sim.scheduler.jobs import EXECUTORS, JobSpec, freeze_spec
from coat_sim.scheduler.result_capability import load_best_candidate_checkpoint
from coat_sim.scheduler.worker import _completed_film_cancel_reply, _film_reply_matches_payload


REMOVED = (
    {'schema_version': 1, 'kind': 'coat_sim.film_sensitivity_request'},
    {'schema_version': 1, 'kind': 'coat_sim.film_ideal_sensitivity_request'},
)


def test_removed_process_modules_and_executor_are_absent():
    for name in ('film_sensitivity', 'film_sensitivity_constrained', 'film_sensitivity_jobs'):
        assert find_spec('coat_sim.core.' + name) is None
    assert 'film_sensitivity' not in EXECUTORS
    assert 'film_sensitivity' not in compute_estimation.KINDS
    assert 'process' in EXECUTORS and 'process' in compute_estimation.KINDS


@pytest.mark.parametrize('payload', REMOVED)
def test_removed_requests_do_not_freeze_estimate_submit_or_create_jobs(payload, tmp_path, monkeypatch):
    service = SimpleNamespace(root=tmp_path)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('removed request reached admission')
    monkeypatch.setattr('coat_sim.scheduler.submission.submit_confirmed', forbidden)
    before = sorted(tmp_path.rglob('*'))
    with pytest.raises(ValueError, match='unsupported'):
        jobs.workflow_request_from_payload(payload)
    with pytest.raises(ValueError, match='unsupported'):
        jobs.build_job_payload(service, payload)
    with pytest.raises(ValueError, match='unsupported'):
        jobs.submit_workflow(service, payload)
    executor = payload['kind'].removeprefix('coat_sim.').removesuffix('_request')
    with pytest.raises(ValueError):
        compute_estimation.canonical_parameters(service, executor, {'payload': payload})
    with pytest.raises(ValueError):
        freeze_spec(service, executor, {'request': payload})
    with pytest.raises(ValueError):
        JobSpec.parse(JobSpec(executor, {'payload': {'schema_version': 1,
            'kind': 'coat_sim.' + executor + '_job', 'request': payload}}, {}).to_dict())
    assert sorted(tmp_path.rglob('*')) == before


@pytest.mark.parametrize('executor', ['film_sensitivity', 'film_ideal_sensitivity'])
def test_removed_result_never_becomes_cancel_reply_or_recoverable_checkpoint(executor, tmp_path):
    result = {'schema_version': 1, 'kind': 'coat_sim.' + executor + '_result',
              'is_final': True, 'state': 'completed'}
    job = {'schema_version': 1, 'kind': 'coat_sim.' + executor + '_job',
           'request': {'schema_version': 1, 'kind': 'coat_sim.' + executor + '_request'}}
    assert not _film_reply_matches_payload(result, job)
    assert _completed_film_cancel_reply(result, job) is None
    directory = tmp_path / 'artifacts/runtime/scheduler/jobs/old-input'
    directory.mkdir(parents=True)
    checkpoint = {'schema_version': 1, 'kind': 'coat_sim.' + executor + '_checkpoint',
                  'job': job, 'result': result}
    pointer = directory / 'best-candidate.json'
    pointer.write_text(json.dumps(checkpoint), encoding='utf-8')
    original = pointer.read_bytes()
    recovered, reason = load_best_candidate_checkpoint(tmp_path, 'old-input')
    assert recovered is None and 'unsupported' in reason.lower()
    assert pointer.read_bytes() == original


@pytest.mark.parametrize('executor', ['film_sensitivity', 'film_ideal_sensitivity'])
def test_scheduler_rejects_removed_input_before_creating_worker_directory(executor, tmp_path):
    from coat_sim.scheduler.daemon import Scheduler
    spec = JobSpec(executor, {'payload': {'schema_version': 1,
        'kind': 'coat_sim.' + executor + '_job',
        'request': {'schema_version': 1, 'kind': 'coat_sim.' + executor + '_request'}}}, {})
    scheduler = SimpleNamespace(runtime=tmp_path)
    with pytest.raises(ValueError):
        Scheduler.start_job(cast(Scheduler, scheduler), {'job_id': 'must-not-start', 'input_digest': spec.fingerprint}, spec.to_dict())
    assert not (tmp_path / 'jobs').exists()
