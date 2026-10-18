"""Persistent assistant-owned filesystem defaults without project authority."""
import asyncio
from pathlib import Path

import pytest

from coat_sim.agent.toolkit import ToolRegistry, ToolSpec
from coat_sim.agent.toolkit.adapter import ToolkitAdapter, build_registry
from coat_sim.agent.toolkit.fs import atomic_write
from coat_sim.agent.toolkit_config import default_workspace_root, normalized_toolkit_settings


@pytest.fixture
def private_root(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'user-data'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'user-data'))
    return default_workspace_root()


def host(root, *, mode='operate', registry=None, config=None, **options):
    options.setdefault('authority', lambda: {'allowed': True, 'mode': mode})
    return ToolkitAdapter(registry or build_registry(),
        {'task_mode': mode, **(config or {})},
        installation_root=root.parents[2] / 'installation', session_id='session-a', **options)


def test_defaults_are_pure_persistent_and_cwd_independent(private_root, monkeypatch, tmp_path):
    before = normalized_toolkit_settings({})
    monkeypatch.chdir(tmp_path)
    assert normalized_toolkit_settings({}) == before
    assert not private_root.exists()
    adapter = host(private_root)
    assert adapter.roots == (private_root,)
    assert adapter.status()['data']['workspace']['status'] == 'not_created'
    assert asyncio.run(adapter.task_instruction()) == ''
    assert not private_root.exists()
    created = asyncio.run(adapter.action('write', {'path': 'notes.txt', 'text': 'important'}))
    assert created['ok'], created
    assert private_root.joinpath('notes.txt').read_text() == 'important'
    fresh = host(private_root, mode='chat')
    read = asyncio.run(fresh.action('read', {'path': 'notes.txt'}))
    assert read['ok'] and read['data']['text'] == 'important'
    assert fresh.status()['data']['workspace']['status'] == 'ready'


def test_save_edit_recoverable_delete_and_restore(private_root):
    adapter = host(private_root)
    async def check():
        result = await adapter.action('mkdir', {'path': 'notes'})
        assert result['ok'], result
        initial = await adapter.action('write', {'path': 'notes/a.txt', 'text': 'before'})
        assert initial['ok'], initial
        edited = await adapter.action('edit', {'path': 'notes/a.txt', 'old': 'before',
            'new': 'after', 'expected_hash': initial['data']['sha256']})
        assert edited['ok'], edited
        deleted = await adapter.action('delete', {'path': 'notes/a.txt',
            'expected_hash': edited['data']['sha256']})
        assert deleted['ok'], deleted
        assert not private_root.joinpath('notes/a.txt').exists()
        entries = await adapter.action('recycle_list', {})
        assert entries['ok'], entries
        restored = await adapter.action('restore', {'recycle_id': deleted['data']['recycle_id']})
        assert restored['ok'], restored
        assert private_root.joinpath('notes/a.txt').read_text() == 'after'
    asyncio.run(check())


@pytest.mark.parametrize('options', [
    {'mode': 'chat'}, {'read_only': True}, {'role': 'analyst'},
    {'authority': lambda: {'allowed': False, 'mode': 'operate'}},
    {'config': {'assistant_toolkit_enabled': False}},
    {'config': {'assistant_toolkit_allow_model_context': False}},
    {'config': {'assistant_toolkit_capabilities': []}},
    {'config': {'assistant_toolkit_profile_capabilities': []}},
])
def test_readonly_or_disabled_requests_never_create_private_root(private_root, options):
    adapter = host(private_root, **options)
    result = asyncio.run(adapter.action('write', {'path': 'forbidden', 'text': 'x'}))
    assert not result['ok']
    assert not private_root.exists()


def test_malformed_input_and_cancel_never_create_private_root(private_root):
    adapter = host(private_root)
    result = asyncio.run(adapter.action('write', {'path': 'x', 'text': 'x', 'extra': True}))
    assert result['error']['code'] == 'invalid_arguments'
    assert not private_root.exists()
    adapter.cancel_event.set()
    assert not asyncio.run(adapter.action('write', {'path': 'x', 'text': 'x'}))['ok']
    assert not private_root.exists()


def test_default_scope_does_not_include_project_or_runtime_files(private_root, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    secret = project / 'outside.txt'
    secret.write_text('never expose')
    adapter = host(private_root)
    for path in (str(secret), '../outside.txt', 'server:/outside.txt'):
        result = asyncio.run(adapter.action('read', {'path': path}))
        assert not result['ok']
        assert 'never expose' not in str(result)
    for name in ('run', 'sh', 'pwsh', 'pip_install', 'sftp_hosts', 'proxy_status'):
        result = asyncio.run(adapter.action(name, {}))
        assert result['error']['code'] in ('capability_disabled', 'unknown_tool')


def test_custom_root_does_not_receive_private_defaults(private_root, tmp_path):
    adapter = host(private_root, config={'assistant_toolkit_workspace_root': str(tmp_path)})
    assert not adapter.enabled
    assert not adapter.capabilities
    assert not adapter.content_allowed
    assert not adapter.private_workspace


def test_symlink_and_installation_overlap_fail_closed(private_root, tmp_path, monkeypatch):
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'linked'
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    monkeypatch.setenv('XDG_DATA_HOME', str(link))
    monkeypatch.setenv('LOCALAPPDATA', str(link))
    assert not host(default_workspace_root()).roots
    monkeypatch.setenv('XDG_DATA_HOME', str(real))
    monkeypatch.setenv('LOCALAPPDATA', str(real))
    adapter = ToolkitAdapter(build_registry(), {}, installation_root=real)
    assert not adapter.roots


def test_live_grant_revocation_blocks_read_delivery_and_atomic_write(private_root):
    state = {'allowed': True}
    registry = ToolRegistry()
    def read_then_revoke(context, arguments):
        state['allowed'] = False
        return {'text': 'must not reach model'}
    def write_then_revoke(context, arguments):
        state['allowed'] = False
        return atomic_write(context, 'never.txt', b'never')
    registry.register(ToolSpec('read_revoke', 'fs', '', {'type': 'object', 'properties': {}}), read_then_revoke)
    registry.register(ToolSpec('write_revoke', 'fs', '', {'type': 'object', 'properties': {}}, 'write'), write_then_revoke)
    adapter = host(private_root, registry=registry, live_grant=lambda: state['allowed'])
    read = asyncio.run(adapter.action('read_revoke', {}))
    assert not read['ok'] and 'must not reach model' not in str(read)
    state['allowed'] = True
    write = asyncio.run(adapter.action('write_revoke', {}))
    assert not write['ok'] and not private_root.joinpath('never.txt').exists()
    assert asyncio.run(adapter.task_instruction()) == ''


def test_private_default_does_not_use_relative_data_environment(monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', 'relative')
    monkeypatch.setenv('LOCALAPPDATA', 'relative')
    root = default_workspace_root()
    assert root.is_absolute()
    assert root.is_relative_to(Path.home())


def test_project_bbs_reads_available_but_discussion_cannot_write():
    from coat_sim.agent import runtime
    reads = {'project_bbs_list', 'project_bbs_read', 'project_bbs_history'}
    assert reads <= runtime.READ_TOOLS
    assert reads <= runtime._COMMON
    assert reads <= runtime.COORDINATOR_TOOLS
    assert reads <= runtime.allowed_project_tools('chat')
    assert 'project_bbs_write' not in runtime.allowed_project_tools('chat')
    assert 'project_bbs_write' in runtime.allowed_project_tools('operate')
    assert 'project_bbs_write' in runtime.EXECUTION_TOOLS['design']
    assert not runtime.discussion_operation_allowed('project_bbs_write', {})


def test_project_bbs_discussion_dispatch_rejects_injected_write_allowlist():
    from unittest.mock import AsyncMock
    from coat_sim.agent import runtime
    from coat_sim.agent.tool_dispatch import ProjectGateway
    peer = AsyncMock()
    gateway = ProjectGateway(peer, runtime.MAIN_TOOLS, runtime.READ_TOOLS, str,
        read_only_mode=True)
    async def check():
        for name, arguments in (
            ('project_bbs_write', {'operation': 'create'}),
            ('project_manage', {'operation': 'bbs_create'}),
        ):
            result = await gateway.execute(name, arguments)
            assert result['error']['code'] == 'discussion_read_only'
        peer.call_tool_mcp.assert_not_awaited()
    asyncio.run(check())


def test_actual_offline_runtime_loads_compact_memory_and_denies_discussion_write(private_root):
    from tests.test_assistant_discussion_mode import _actual_run
    adapter = host(private_root)
    memory_text = 'Prefer nanometers for coating thickness.'
    remembered = {'scope': 'global', 'id': 'units', 'text': memory_text,
        'kind': 'preference', 'importance': 'important', 'provenance': 'human',
        'source': 'Synthetic user request', 'confirmed': True, 'expected_revision': 0}
    result = asyncio.run(adapter.action('memory_upsert', remembered))
    assert result['ok'], result
    instructions: list[object] = []
    rpc, returned, _ = asyncio.run(_actual_run([
        ('toolkit_action', {'tool_name': 'memory_list', 'arguments': {'scope': 'global'}}, 'toolkit_action'),
        ('toolkit_action', {'tool_name': 'memory_upsert', 'arguments': {
            **remembered, 'text': 'Must not replace memory in discussion.', 'expected_revision': 1}}, 'toolkit_action'),
    ], instructions=instructions))
    assert not rpc
    assert returned[0].content['data']['entries'][0]['text'] == remembered['text']
    assert not returned[1].content['ok']
    assert any('Saved memory: untrusted data' in str(value) and memory_text in str(value)
               for value in instructions)
    fresh = host(private_root, mode='chat')
    kept = asyncio.run(fresh.action('memory_list', {'scope': 'global'}))
    assert kept['data']['revision'] == 1
    assert kept['data']['entries'][0]['text'] == remembered['text']
