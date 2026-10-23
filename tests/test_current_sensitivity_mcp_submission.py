"""Current formats traverse registered MCP release confirmation and real workers.

Only the scheduler transport is in memory. No IPC listener or child process is
started; durable receipts, worker specifications and outcome files are real.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from typing import Any

import pytest

from coat_sim.core.api import CoatService
from coat_sim.core.compute_tasks import ComputeTaskStore
from coat_sim.core import film_sensitivity_workflow_jobs as jobs
from coat_sim.scheduler import submission
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.scheduler.io import write_json, write_text
from coat_sim.scheduler.jobs import canonical
from coat_sim.scheduler.store import SchedulerStore
from tests.test_film_sensitivity_workflow_jobs import problem

pytestmark = pytest.mark.algorithm


def _call(name, arguments):
    from coat_sim.mcp import server
    response = asyncio.run(server.mcp.call_tool(name, arguments))
    for field in ('structured_content', 'structuredContent'):
        value = getattr(response, field, None)
        if value is not None:
            return value
    for block in getattr(response, 'content', ()) or ():
        if getattr(block, 'text', None):
            return json.loads(block.text)
    raise AssertionError('registered MCP tool returned no JSON')


class _WorkerTransport:
    def __init__(self, root):
        self.root = root
        self.store = SchedulerStore(root)
        self.submissions: list[dict[str, Any]] = []
        self.worker_calls = 0

    def request(self, action, arguments):
        if action == 'refresh_estimates':
            return {'refreshed': True}
        if action == 'status':
            return self.store.status(arguments['job_id'])
        if action == 'submit':
            from coat_sim.scheduler import worker
            self.submissions.append(deepcopy(arguments))
            row = self.store.submit(**arguments)
            directory = self.root / 'artifacts/runtime/scheduler/jobs' / row['job_id']
            directory.mkdir(parents=True, exist_ok=True)
            write_text(directory / 'spec.json', canonical(arguments['spec']))
            write_json(directory / 'launch.json', {'instance': 'current-format-review'})
            self.worker_calls += 1
            assert worker.run(self.root, row['job_id'], os.getpid(), 'current-format-review') == 0
            outcome = json.loads((directory / 'outcome.json').read_text(encoding='utf-8'))
            return self.store.transition(row['job_id'], **outcome)
        raise AssertionError(f'unexpected scheduler action: {action}')


@pytest.fixture
def mcp_peer(tmp_path, monkeypatch):
    from coat_sim.mcp import server
    service = CoatService(tmp_path)
    transport = _WorkerTransport(tmp_path)

    def factory(root=None, **_kwargs):
        return SchedulerClient(root or tmp_path, transport=transport)

    monkeypatch.setattr(server, 'service', service)
    monkeypatch.setattr('coat_sim.scheduler.SchedulerClient', factory)
    monkeypatch.setattr(submission, 'SchedulerClient', factory)
    monkeypatch.setattr('subprocess.Popen', lambda *_args, **_kwargs: pytest.fail('unexpected child process'))
    return service, transport


@pytest.mark.parametrize('kind', jobs.WORKFLOW_KINDS)
def test_registered_current_mcp_requires_confirmation_then_runs_exact_worker_once(kind, mcp_peer):
    from coat_sim.mcp import server
    service, peer = mcp_peer
    film_service, request = problem(kind)
    payload = jobs.workflow_request_to_payload(jobs.freeze_workflow_request(film_service, request))
    store = ComputeTaskStore(service.root)

    async def discovery():
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        assert 'plan_film_sensitivity' not in tools and 'submit_film_sensitivity' not in tools
        assert tools['plan_' + kind].annotations.read_only_hint is True
        assert tools['submit_' + kind].annotations.read_only_hint is False
    asyncio.run(discovery())
    planned = _call('plan_' + kind, {'payload': payload})
    assert planned['ok'] and planned['request'] == payload
    assert not store.path.exists() or store.snapshot()['confirmations'] == []
    assert not peer.submissions and peer.worker_calls == 0
    first = _call('submit_' + kind, {'payload': payload, 'client_request_id': 'current-confirmed'})
    assert first['state'] == 'prepared' and first['needs_release_request']
    assert peer.worker_calls == 0
    duplicate = _call('submit_' + kind, {'payload': payload, 'client_request_id': 'current-confirmed'})
    assert duplicate['estimate_id'] == first['estimate_id'] and peer.worker_calls == 0
    pending = store.request(first['estimate_id'])
    assert pending['release_policy'] == 'manual'
    assert peer.worker_calls == 0
    accepted = submission.confirm_and_submit(service, first['estimate_id'], True)
    assert peer.worker_calls == len(peer.submissions) == 1
    assert accepted['kind'] == kind and accepted['state'] == 'succeeded'
    assert accepted['result']['state'] == 'completed'
    expected = peer.submissions[0]['spec']
    assert expected['executor'] == kind
    assert expected['parameters']['payload']['schema_version'] == 1
    assert expected['parameters']['payload']['request'] == payload
    assert jobs.result_matches_job(accepted['result'], expected['parameters']['payload'])
    jobs.validate_workflow_result(accepted['result'], expected_request=payload, replay=True)
    repeated = _call('submit_' + kind, {'payload': payload, 'client_request_id': 'current-confirmed'})
    assert repeated['job_id'] == accepted['job_id'] and peer.worker_calls == 1


@pytest.mark.parametrize('kind', jobs.WORKFLOW_KINDS)
@pytest.mark.parametrize('client_id', ['', False, 0])
def test_current_submission_rejects_falsey_ids_before_estimation(kind, client_id, mcp_peer):
    service, peer = mcp_peer
    film_service, request = problem(kind)
    payload = jobs.workflow_request_to_payload(jobs.freeze_workflow_request(film_service, request))
    with pytest.raises(ValueError, match='client_request_id'):
        jobs.submit_workflow(service, payload, client_id)
    assert not peer.submissions and peer.worker_calls == 0
    with ComputeTaskStore(service.root).connection() as database:
        assert database.execute('SELECT COUNT(*) FROM estimates').fetchone()[0] == 0


@pytest.mark.parametrize('kind', jobs.WORKFLOW_KINDS)
def test_current_discussion_dispatch_cannot_submit_or_estimate(kind):
    from tests.test_assistant_discussion_mode import _actual_run
    film_service, request = problem(kind)
    payload = jobs.workflow_request_to_payload(jobs.freeze_workflow_request(film_service, request))
    calls = [('submit_' + kind, {'payload': payload}),
             ('estimate_compute_task', {'kind': kind, 'parameters': {'payload': payload}})]
    rpc, replies, _tools = asyncio.run(_actual_run(calls, mode='chat'))
    assert not rpc and len(replies) == 2
    assert all(reply.content['error']['code'] == 'discussion_read_only' for reply in replies)
