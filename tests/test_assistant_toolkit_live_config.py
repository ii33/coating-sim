"""Offline live settings revocation across the JSONL host and toolkit threads."""
import asyncio
import io
import json
import queue
import threading
import types

import pytest

from coat_sim.agent import worker
from coat_sim.agent.toolkit import ToolRegistry, ToolSpec
from coat_sim.agent.toolkit.adapter import ToolkitAdapter
from coat_sim.agent.toolkit.fs import atomic_write


def configured(root):
    return {
        'assistant_toolkit_enabled': True,
        'assistant_toolkit_allow_model_context': True,
        'assistant_toolkit_workspace_root': str(root),
        'assistant_toolkit_capabilities': ['fs', 'doc'],
        'assistant_toolkit_profile_capabilities': ['fs', 'doc'],
        'task_mode': 'operate',
    }


CHANGES = [
    {'assistant_toolkit_enabled': False},
    {'assistant_toolkit_allow_model_context': False},
    {'assistant_toolkit_workspace_root': ''},
    {'assistant_toolkit_capabilities': ['doc']},
    {'assistant_toolkit_profile_capabilities': ['doc']},
]


@pytest.mark.parametrize('change', CHANGES)
def test_relevant_settings_revoke_and_cannot_restore_old_generation(tmp_path, change):
    writer = worker.ProtocolWriter(io.StringIO())
    original = configured(tmp_path)
    writer.configure_toolkit(original, replace=True)
    generation = writer.toolkit_grant_snapshot()
    assert writer.toolkit_generation_is_current(generation)
    assert writer.configure_toolkit(change)
    assert not writer.toolkit_generation_is_current(generation)
    assert writer.configure_toolkit(original, replace=True)
    assert not writer.toolkit_generation_is_current(generation)
    assert writer.toolkit_grant_snapshot() == generation + 2


def test_normalized_noops_and_unrelated_updates_preserve_grant(tmp_path):
    writer = worker.ProtocolWriter(io.StringIO())
    writer.configure_toolkit(configured(tmp_path), replace=True)
    generation = writer.toolkit_grant_snapshot()
    for update in ({}, {'theme': 'dark', 'api_key': 'never-retain'},
                   {'assistant_daily_token_limit': 100},
                   {'assistant_toolkit_workspace_root': f'  {tmp_path}  '},
                   {'assistant_toolkit_capabilities': ['doc', 'fs', 'fs']},
                   {'assistant_toolkit_profile_capabilities': ('doc', 'fs')}):
        assert not writer.configure_toolkit(update)
        assert writer.toolkit_generation_is_current(generation)
    assert 'never-retain' not in repr(writer._toolkit_config)
    assert not writer.toolkit_generation_is_current(True)
    assert not writer.toolkit_generation_is_current(str(generation))


@pytest.mark.parametrize('change', [
    {'assistant_toolkit_enabled': 'true'},
    {'assistant_toolkit_allow_model_context': 1},
    {'assistant_toolkit_workspace_root': None},
    {'assistant_toolkit_workspace_root': '/invalid\x00root'},
    {'assistant_toolkit_capabilities': 'fs'},
    {'assistant_toolkit_capabilities': ['fs', 'unknown']},
    {'assistant_toolkit_profile_capabilities': {'fs': True}},
])
def test_malformed_explicit_updates_revoke(tmp_path, change):
    writer = worker.ProtocolWriter(io.StringIO())
    writer.configure_toolkit(configured(tmp_path), replace=True)
    generation = writer.toolkit_grant_snapshot()
    assert writer.configure_toolkit(change)
    assert not writer.toolkit_generation_is_current(generation)


@pytest.mark.parametrize('change', CHANGES)
@pytest.mark.parametrize('effect', ['read', 'write'])
def test_live_config_withholds_reads_and_prevents_pending_writes(tmp_path, change, effect):
    writer = worker.ProtocolWriter(io.StringIO())
    config = configured(tmp_path)
    writer.configure_toolkit(config, replace=True)
    generation = writer.toolkit_grant_snapshot()
    entered, release = threading.Event(), threading.Event()
    registry = ToolRegistry()

    def handler(context, arguments):
        entered.set()
        assert release.wait(5)
        if effect == 'write':
            atomic_write(context, 'must-not-exist', b'forbidden')
        return {'private_content': 'must not reach the model'}

    registry.register(ToolSpec('pending', 'fs', '',
        {'type': 'object', 'properties': {}}, effect), handler)
    host = ToolkitAdapter(registry, config, installation_root=tmp_path.parent / 'install',
        authority=lambda: {'allowed': True, 'mode': 'operate'},
        live_grant=lambda: writer.toolkit_generation_is_current(generation))

    async def check():
        pending = asyncio.create_task(host.action('pending', {}))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            writer.configure_toolkit(change)
        finally:
            release.set()
        result = await pending
        assert not result['ok']
        assert 'must not reach the model' not in repr(result)
        assert not (tmp_path / 'must-not-exist').exists()

    asyncio.run(check())


def test_expansion_only_available_to_new_request(tmp_path):
    writer = worker.ProtocolWriter(io.StringIO())
    config = configured(tmp_path)
    config['assistant_toolkit_capabilities'] = ['fs']
    writer.configure_toolkit(config, replace=True)
    generation = writer.toolkit_grant_snapshot()
    registry = ToolRegistry()
    registry.register(ToolSpec('inspect', 'doc', '', {'type': 'object', 'properties': {}}),
                      lambda context, args: {})
    host = ToolkitAdapter(registry, config, installation_root=tmp_path.parent / 'install',
        live_grant=lambda: writer.toolkit_generation_is_current(generation))
    expanded = configured(tmp_path)
    writer.configure_toolkit(expanded)
    assert 'doc' not in host.capabilities
    assert not asyncio.run(host.action('inspect', {}))['ok']
    new_generation = writer.toolkit_grant_snapshot()
    next_host = ToolkitAdapter(registry, expanded, installation_root=tmp_path.parent / 'install',
        live_grant=lambda: writer.toolkit_generation_is_current(new_generation))
    assert asyncio.run(next_host.action('inspect', {}))['ok']


def test_worker_binds_request_and_live_config_without_rebasing_or_busy_run_revocation(tmp_path, monkeypatch):
    lines: queue.Queue[str] = queue.Queue()
    writer = worker.ProtocolWriter(io.StringIO())
    monkeypatch.setattr(writer, 'bind_request', lambda request, session: None)

    async def check():
        started, finish, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        rejected, configured_event = asyncio.Event(), asyncio.Event()
        captured = []

        async def run_task(command, output, **kwargs):
            captured.append(command['_toolkit_grant_generation'])
            started.set()
            await finish.wait()
            assert not output.toolkit_generation_is_current(captured[0])
            finished.set()

        fake_runtime = types.SimpleNamespace(run_task=run_task, compact_context=run_task)
        monkeypatch.setitem(worker.sys.modules, 'coat_sim.agent.runtime', fake_runtime)
        monkeypatch.setattr(worker.sys, 'stdin', types.SimpleNamespace(readline=lambda size: lines.get()))
        emit = writer.emit

        def observed_emit(event, request_id='', **values):
            emit(event, request_id, **values)
            if event == 'error' and request_id == 'busy':
                rejected.set()
            if event == 'status':
                configured_event.set()

        monkeypatch.setattr(writer, 'emit', observed_emit)
        serving = asyncio.create_task(worker.serve(writer))

        def send(command):
            lines.put(json.dumps(command) + '\n')

        try:
            command = {'op': 'run', 'request_id': 'first', 'session_id': 's', 'prompt': 'test',
                       'config': configured(tmp_path), '_toolkit_grant_generation': 999999}
            send(command)
            await asyncio.wait_for(started.wait(), 5)
            assert captured == [writer.toolkit_grant_snapshot()]
            assert captured[0] != 999999
            send({**command, 'request_id': 'busy', 'config': {'assistant_toolkit_enabled': False}})
            await asyncio.wait_for(rejected.wait(), 5)
            assert writer.toolkit_generation_is_current(captured[0])
            send({'op': 'configure', 'config': {'assistant_toolkit_allow_model_context': False}})
            await asyncio.wait_for(configured_event.wait(), 5)
            assert not writer.toolkit_generation_is_current(captured[0])
            finish.set()
            await asyncio.wait_for(finished.wait(), 5)
        finally:
            send({'op': 'shutdown'})
            lines.put('')
            await asyncio.wait_for(serving, 5)

    asyncio.run(check())
