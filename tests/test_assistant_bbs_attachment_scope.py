"""BBS source-file imports are foreground main mutations, not comment effects."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.agent import runtime
from coat_sim.agent.tool_dispatch import ProjectGateway
from coat_sim.assistant_process import AssistantProcessClient
from coat_sim.core.api import CoatService
from coat_sim.core.project_store import ProjectStore
from coat_sim.mcp import server
from tests.test_assistant_discussion_comments import COMMENT
from tests.test_assistant_discussion_mode import _actual_run


IMPORT = {key: value for key, value in COMMENT.items() if key not in {'title', 'body'}}
IMPORT['file'] = '/synthetic/project/source.txt'


@pytest.mark.parametrize('mode', ['operate', 'research'])
def test_main_runtime_can_import_in_effectful_modes(mode):
    rpc, returned, _ = asyncio.run(_actual_run([('project_bbs_import_attachment', IMPORT)], mode=mode))
    assert rpc == [('project_bbs_import_attachment', IMPORT)]
    assert 'error' not in returned[0].content


def test_discussion_accepts_rich_comment_but_never_imports():
    rich = {**COMMENT, 'body_format': 'markdown', 'attachments': []}
    rpc, returned, _ = asyncio.run(_actual_run([
        ('project_bbs_comment', rich), ('project_bbs_import_attachment', IMPORT),
        ('project_manage', {'operation': 'bbs_import_attachment', 'parameters': {'file': IMPORT['file']}})]))
    assert rpc == [('project_bbs_comment', rich)]
    assert all(value.content['error']['code'] == 'discussion_read_only' for value in returned[1:])
    assert not runtime.discussion_operation_allowed('project_bbs_comment', {**rich, 'file': IMPORT['file']})


def test_import_is_absent_from_all_delegate_and_background_scopes():
    name = 'project_bbs_import_attachment'
    assert name in runtime.allowed_project_tools('operate')
    assert name in runtime.allowed_project_tools('research')
    assert name not in runtime.READ_TOOLS | runtime.DISCUSSION_TOOLS | runtime._COMMON | runtime.COORDINATOR_TOOLS
    assert all(name not in scope for scope in runtime.EXECUTION_TOOLS.values())
    assert all(name not in runtime.allowed_project_tools(mode, background=True)
        for mode in ('chat', 'operate', 'research'))


@pytest.mark.parametrize('read_only,grant', [(False, False), (True, False), (True, True)])
def test_gateway_blocks_dedicated_and_generic_import_without_foreground_grant(read_only, grant):
    async def check():
        peer = SimpleNamespace(list_tools=AsyncMock(return_value=await server.mcp.list_tools()),
            call_tool_mcp=AsyncMock())
        gateway = ProjectGateway(peer, runtime.MAIN_TOOLS, runtime.READ_TOOLS, str,
            read_only_mode=read_only, allow_attachment_import=grant)
        catalog = await gateway.catalog()
        assert 'project_bbs_import_attachment' not in {row['name'] for row in catalog['tools']}
        for name, arguments in [
            ('project_bbs_import_attachment', IMPORT),
            ('project_manage', {'operation': 'bbs_import_attachment', 'root': IMPORT['root'],
                'parameters': {'file': IMPORT['file']}}),
        ]:
            result = await gateway.execute(name, arguments)
            assert result['ok'] is False and result['error']['effect'] == 'none'
        peer.call_tool_mcp.assert_not_awaited()
    asyncio.run(check())


def test_main_catalog_discovers_import_as_mutation():
    async def check():
        peer = SimpleNamespace(list_tools=AsyncMock(return_value=await server.mcp.list_tools()))
        gateway = ProjectGateway(peer, runtime.MAIN_TOOLS, runtime.READ_TOOLS, str,
            allow_attachment_import=True)
        catalog = await gateway.catalog(['project_bbs_import_attachment'])
        tool = catalog['tools'][0]
        assert tool['name'] == 'project_bbs_import_attachment'
        assert 'file' in tool['input_schema']['required']
        assert tool['name'] not in gateway.read_only
        assert (await gateway.catalog())['read_only'] is False
    asyncio.run(check())


def test_real_mcp_import_rejects_source_outside_authorized_roots(tmp_path, monkeypatch):
    approved = tmp_path / 'approved'
    approved.mkdir()
    project = ProjectStore.create(approved / 'project', 'Attachment boundary')
    source = tmp_path / 'private.txt'
    source.write_text('Must not be copied', encoding='utf-8')
    monkeypatch.setattr(server, 'service', CoatService(approved))
    monkeypatch.setattr(AssistantProcessClient, 'list_clients', lambda _: [])
    with pytest.raises(ToolError, match='project_root_not_authorized'):
        server.project_bbs_import_attachment(str(project.project_file), project.manifest['id'],
            'outside-source', 0, str(source))
    assert ProjectStore.open(project.project_file).manifest['revision'] == 0
    assert source.read_text(encoding='utf-8') == 'Must not be copied'
