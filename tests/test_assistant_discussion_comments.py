"""Main discussion may append BBS comments without granting general writes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coat_sim.agent import runtime
from coat_sim.agent.storage import AssistantStore
from coat_sim.agent.tool_dispatch import ProjectGateway
from coat_sim.mcp import server
from tests.test_assistant_discussion_mode import _actual_run


COMMENT = {'root': '/synthetic/project', 'project_id': 'project', 'operation_id': 'comment-1',
    'expected_revision': 0, 'title': 'Observation', 'body': 'This is an important observation.'}


def test_main_discussion_actual_runtime_can_comment_but_not_other_writes():
    calls = [('project_bbs_comment', COMMENT),
        ('project_bbs_write', {'operation': 'archive'}),
        ('project_manage', {'operation': 'bbs_create'}),
        ('toolkit_action', {'tool_name': 'write', 'arguments': {'path': 'forbidden', 'text': 'x'}}, 'toolkit_action'),
        ('toolkit_action', {'tool_name': 'memory_upsert', 'arguments': {}}, 'toolkit_action')]
    rpc, returned, observed = asyncio.run(_actual_run(calls))
    assert rpc == [('project_bbs_comment', COMMENT)]
    assert 'project_action' in observed[0]
    assert not any(part.content.get('ok') is True for part in returned[1:])


def test_discussion_comment_without_ui_request_binding_is_denied():
    rpc, returned, _ = asyncio.run(_actual_run([('project_bbs_comment', COMMENT)], bind=False))
    assert not rpc
    assert returned[0].content['error']['code'] == 'assistant_dispatch_blocked'


def test_comment_authority_cannot_survive_away_and_back_mode_epoch(tmp_path):
    store = AssistantStore(tmp_path / 'state.sqlite3')
    session = store.new_session('Synthetic comment authority')
    store.set_ui_session_mode(session, 'chat')
    store.bind_ui_request(session, 'comment-request', 'chat')
    assert runtime.bbs_comment_authority(store, session, 'comment-request')['allowed']
    assert not runtime.dispatch_authority(store, session, 'comment-request')['allowed']
    store.set_ui_session_mode(session, 'operate')
    store.set_ui_session_mode(session, 'chat')
    assert not runtime.bbs_comment_authority(store, session, 'comment-request')['allowed']


def test_background_and_delegated_roles_never_receive_comment_exception():
    assert 'project_bbs_comment' not in runtime.READ_TOOLS
    assert 'project_bbs_comment' in runtime.allowed_project_tools('chat')
    assert 'project_bbs_comment' not in runtime.allowed_project_tools('chat', background=True)
    assert 'project_bbs_comment' not in runtime.allowed_project_tools('operate', background=True)
    assert 'project_bbs_comment' not in runtime.DISCUSSION_READ_TOOLS
    assert 'project_bbs_comment' not in runtime._COMMON
    assert all('project_bbs_comment' not in tools for tools in runtime.EXECUTION_TOOLS.values())


@pytest.mark.parametrize('authority', [lambda: False, lambda: 'true', None])
def test_gateway_requires_exact_live_comment_authority(authority):
    async def check():
        tools = await server.mcp.list_tools()
        peer = SimpleNamespace(list_tools=AsyncMock(return_value=tools), call_tool_mcp=AsyncMock())
        gateway = ProjectGateway(peer, runtime.MAIN_TOOLS, runtime.READ_TOOLS, str,
            read_only_mode=True, allow_discussion_comments=True, comment_authority=authority)
        result = await gateway.execute('project_bbs_comment', COMMENT)
        assert result['error']['code'] == 'assistant_dispatch_blocked'
        peer.call_tool_mcp.assert_not_awaited()
    asyncio.run(check())


def test_comment_keeps_mutation_metadata_and_unknown_effect_barrier():
    async def check():
        tools = await server.mcp.list_tools()
        peer = SimpleNamespace(list_tools=AsyncMock(return_value=tools),
            call_tool_mcp=AsyncMock(side_effect=TimeoutError('synthetic timeout')))
        gateway = ProjectGateway(peer, runtime.allowed_project_tools('chat'), runtime.READ_TOOLS, str,
            read_only_mode=True, allow_discussion_comments=True, comment_authority=lambda: True)
        catalog = await gateway.catalog()
        assert catalog['read_only'] is False
        assert 'project_bbs_comment' in {row['name'] for row in catalog['tools']}
        result = await gateway.execute('project_bbs_comment', COMMENT)
        assert result['error']['effect'] == 'unknown'
        again = await gateway.execute('project_bbs_comment', COMMENT)
        assert again['dispatch_blocked']
        assert peer.call_tool_mcp.await_count == 1
    asyncio.run(check())


@pytest.mark.parametrize('extra', [{'operation': 'update'}, {'post_type': 'report'}, {'archived': True}, {'actor': 'user'}])
def test_comment_cannot_smuggle_generic_write_arguments(extra):
    assert not runtime.discussion_operation_allowed('project_bbs_comment', {**COMMENT, **extra})
    async def check():
        tools = await server.mcp.list_tools()
        peer = SimpleNamespace(list_tools=AsyncMock(return_value=tools), call_tool_mcp=AsyncMock())
        gateway = ProjectGateway(peer, runtime.MAIN_TOOLS, runtime.READ_TOOLS, str,
            read_only_mode=True, allow_discussion_comments=True, comment_authority=lambda: True)
        result = await gateway.execute('project_bbs_comment', {**COMMENT, **extra})
        assert result['error']['code'] == 'invalid_arguments'
        for name in ('project_bbs_write', 'project_manage'):
            result = await gateway.execute(name, {'operation': 'bbs_create'})
            assert result['error']['code'] == 'discussion_read_only'
        peer.call_tool_mcp.assert_not_awaited()
    asyncio.run(check())
