"""Host terminal integration: local disposable programs only, no network/install."""
import asyncio
from dataclasses import replace
import sys
import time

import pytest

from coat_sim.agent.toolkit.adapter import ToolkitAdapter, build_registry
from coat_sim.agent.toolkit_config import toolkit_setting_defaults


def host(tmp_path, *, mode='operate', capabilities=None, authority=None, **kwargs):
    caps = ['run'] if capabilities is None else capabilities
    config = {'assistant_toolkit_enabled': True, 'assistant_toolkit_allow_model_context': True,
              'assistant_toolkit_workspace_root': str(tmp_path), 'task_mode': mode,
              'assistant_toolkit_capabilities': caps, 'assistant_toolkit_profile_capabilities': caps}
    adapter = ToolkitAdapter(build_registry(), config, installation_root=tmp_path.parent / 'install',
        authority=authority or (lambda: {'allowed': True, 'mode': mode}), **kwargs)
    adapter.enable_host_terminal()
    return adapter


def invoke(adapter, name, args):
    return asyncio.run(adapter.action(name, args))


def finished(adapter, task_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = invoke(adapter, 'terminal_poll', {'task_id': task_id})
        assert result['ok'], result
        if result['data']['state'] != 'running':
            return result['data']
        time.sleep(.01)
    pytest.fail('Local terminal did not finish')


def test_explicit_gate_defaults_and_truthful_status(tmp_path):
    assert 'run' not in toolkit_setting_defaults({})['assistant_toolkit_capabilities']
    adapter = host(tmp_path, capabilities=[])
    try:
        assert not invoke(adapter, 'terminal_start', {'argv': [sys.executable, '-V']})['ok']
        assert not invoke(adapter, 'run', {'argv': [sys.executable, '-V']})['ok']
        status = adapter.status()['data']
        assert status['terminal']['isolation'] == 'none'
        assert 'chroot' in status['terminal']['warning']
        assert next(row for row in status['capabilities'] if row['capability'] == 'run')['status'] == 'disabled'
        assert not adapter._host_terminal._sessions
    finally:
        adapter.close()


@pytest.mark.parametrize('mode', ['chat', 'research'])
def test_non_operate_mode_cannot_start_even_with_run_enabled(tmp_path, mode):
    adapter = host(tmp_path, mode=mode)
    try:
        for name in ('run', 'terminal_start'):
            assert invoke(adapter, name, {'argv': [sys.executable, '-V']})['error']['code'] == 'authorization_denied'
        status = adapter.status()['data']['capabilities']
        assert next(row for row in status if row['capability'] == 'run')['status'] == 'execution_authority_denied'
        assert not adapter._host_terminal._sessions
    finally:
        adapter.close()


def test_terminal_roundtrip_scope_and_run_backend(tmp_path):
    adapter, other = host(tmp_path), host(tmp_path)
    try:
        args = {'argv': [sys.executable, '-c', "print('hello 终端')"]}
        started = invoke(adapter, 'terminal_start', args)
        assert started['ok'], started
        assert started['data']['isolation'] == 'none'
        assert 'not OS sandboxed' in started['data']['warning']
        task_id = started['data']['task_id']
        assert invoke(other, 'terminal_poll', {'task_id': task_id})['error']['code'] == 'task_not_found'
        result = finished(adapter, task_id)
        assert result['state'] == 'completed' and result['exit_code'] == 0
        assert result['stdout'].strip() == 'hello 终端'
        assert finished(adapter, task_id)['stdout'] == result['stdout']
        sync = invoke(adapter, 'run', args)
        assert sync['ok'] and sync['data']['isolation'] == 'none'
        assert sync['data']['stdout'] == result['stdout']
        # Enabling the host terminal does not enable shell/install providers.
        assert 'execution' not in adapter.providers
    finally:
        adapter.close()
        other.close()


def test_run_timeout_is_structured_error(tmp_path):
    adapter = host(tmp_path)
    try:
        result = invoke(adapter, 'run', {'argv': [sys.executable, '-c', 'import time; time.sleep(10)'], 'timeout_ms': 20})
        assert result['error']['code'] == 'timeout'
    finally:
        adapter.close()


def test_schema_rejects_owner_root_env_and_pid(tmp_path):
    adapter = host(tmp_path)
    try:
        for key in ('scope_id', 'workspace_root', 'env', 'pid', 'stdin'):
            result = invoke(adapter, 'terminal_start', {'argv': [sys.executable, '-V'], key: 'injected'})
            assert result['error']['code'] == 'invalid_arguments'
        assert not adapter._host_terminal._sessions
    finally:
        adapter.close()


def test_live_grant_revocation_stops_process_and_withholds_output(tmp_path):
    grant = [True]
    adapter = host(tmp_path, live_grant=lambda: grant[0])
    try:
        started = invoke(adapter, 'terminal_start', {'argv': [sys.executable, '-c', 'import time; time.sleep(30)']})
        task_id = started['data']['task_id']
        session = adapter._host_terminal._sessions[task_id]
        grant[0] = False  # Same callback covers settings and project epochs.
        assert session.finished.wait(5)
        assert session.process.poll() is not None
        assert not invoke(adapter, 'terminal_poll', {'task_id': task_id})['ok']
    finally:
        adapter.close()


def test_profile_narrowing_stops_owned_process(tmp_path):
    adapter = host(tmp_path)
    try:
        started = invoke(adapter, 'terminal_start', {'argv': [sys.executable, '-c', 'import time; time.sleep(30)']})
        session = adapter._host_terminal._sessions[started['data']['task_id']]
        adapter.capabilities = frozenset()
        adapter.context = replace(adapter.context, allowed_capabilities=frozenset())
        assert session.finished.wait(5)
        assert session.process.poll() is not None
    finally:
        adapter.close()


def test_close_and_cancel_only_owned_tasks(tmp_path):
    adapter = host(tmp_path)
    started = invoke(adapter, 'terminal_start', {'argv': [sys.executable, '-c', 'import time; time.sleep(30)']})
    task_id = started['data']['task_id']
    assert invoke(adapter, 'terminal_cancel', {'task_id': task_id})['ok']
    assert finished(adapter, task_id)['state'] == 'cancelled'
    started = invoke(adapter, 'terminal_start', {'argv': [sys.executable, '-c', 'import time; time.sleep(30)']})
    session = adapter._host_terminal._sessions[started['data']['task_id']]
    adapter.close()
    adapter.close()
    assert session.finished.is_set() and session.process.poll() is not None


def test_live_authority_mode_change_stops_owned_process(tmp_path):
    authority = {'allowed': True, 'mode': 'operate'}
    adapter = host(tmp_path, authority=lambda: dict(authority))
    try:
        started = invoke(adapter, 'terminal_start', {'argv': [sys.executable, '-c', 'import time; time.sleep(30)']})
        session = adapter._host_terminal._sessions[started['data']['task_id']]
        authority.update(allowed=False, mode='chat')
        assert session.finished.wait(5)
        assert session.process.poll() is not None
        denied = invoke(adapter, 'terminal_start', {'argv': [sys.executable, '-V']})
        assert denied['error']['code'] == 'authorization_denied'
    finally:
        adapter.close()


@pytest.mark.parametrize('outcome', ['return', 'failure', 'cancel'])
def test_runtime_closes_request_scope_on_every_exit(monkeypatch, outcome):
    from coat_sim.agent import runtime

    class Owned:
        closes = 0
        def close(self):
            self.closes += 1

    owned = Owned()
    async def task(command, writer, *, owned_toolkits, **kwargs):
        owned_toolkits.append(owned)
        runtime._ACTIVE_TOOLKITS.add(owned)
        if outcome == 'failure':
            raise ValueError('preparation failed')
        if outcome == 'cancel':
            raise asyncio.CancelledError()
    monkeypatch.setattr(runtime, '_run_task', task)
    if outcome == 'failure':
        with pytest.raises(ValueError, match='preparation failed'):
            asyncio.run(runtime.run_task({}, None))
    elif outcome == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runtime.run_task({}, None))
    else:
        asyncio.run(runtime.run_task({}, None))
    assert owned.closes == 1
    assert owned not in runtime._ACTIVE_TOOLKITS


@pytest.mark.parametrize('key', ['', 'test-placeholder-not-a-real-secret'])
def test_runtime_client_authorization_boundary_preserves_existing_behavior(key):
    import httpx2
    from openai import AsyncOpenAI, Omit
    from typing import Mapping, cast

    async def observe(typed_boundary):
        observed = []
        def respond(request):
            observed.append(request.headers.get('authorization'))
            return httpx2.Response(200, json={'id': 'test', 'object': 'chat.completion',
                'created': 0, 'model': 'test', 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]})
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as transport:
            original_headers = {'Authorization': Omit()} if not key else None
            headers = cast(Mapping[str, str], original_headers) if typed_boundary and not key else original_headers
            async with AsyncOpenAI(api_key=key or 'coat-local-no-auth',
                    base_url='https://example.invalid/v1' if key else 'http://localhost:1234/v1',
                    default_headers=cast(Mapping[str, str] | None, headers), http_client=transport) as client:
                try:
                    await client.chat.completions.create(model='test', messages=[{'role': 'user', 'content': 'hello'}])
                except TypeError as exc:
                    return observed, type(exc), str(exc)
        return observed, None, ''
    original = asyncio.run(observe(False))
    typed = asyncio.run(observe(True))
    assert typed == original
    if key:
        assert typed == (['Bearer ' + key], None, '')
    else:
        # SDK 3.29 rejects the pre-existing default-header Omit flow before
        # transmission. This terminal patch preserves, but does not fix, that
        # separate compatibility issue. Other SDKs may omit it successfully.
        assert typed[0] in ([], [None])
        if typed[1] is not None:
            assert typed[1] is TypeError and 'authentication method' in typed[2]
