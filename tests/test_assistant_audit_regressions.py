"""Offline reproductions for AS-01/02/03; synthetic MCP peers only."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from pydantic_ai.messages import ToolCallPart
from coat_sim.agent import runtime
from coat_sim.agent.tool_dispatch import ProjectGateway, STRONG_IDEMPOTENT_TOOLS
from coat_sim.mcp import server


class Peer:
    def __init__(self, registered):
        self.registered, self.calls, self.state = registered, [], None
        self.fail = False
    async def list_tools(self):
        return self.registered
    async def call_tool_mcp(self, name, args):
        self.calls.append((name, args))
        if self.fail:
            raise OSError('unknown transport outcome')
        self.state = args.get('content', self.state)
        return SimpleNamespace(structured_content={'ok': True, 'content': self.state}, content=[], is_error=False)


def test_ordinary_mutation_a_b_a_across_shared_gateways():
    async def check():
        peer = Peer(await server.mcp.list_tools())
        shared: dict[str, Any] = {}
        parent = ProjectGateway(peer, {'write_data_file'}, runtime.READ_TOOLS, str, shared)
        child = ProjectGateway(peer, {'write_data_file'}, runtime.READ_TOOLS, str, shared)
        for gateway, content in ((parent, 'A'), (child, 'B'), (parent, 'A')):
            assert (await gateway.execute('write_data_file', {'path': 'tmp/audit.txt', 'content': content, 'overwrite': True}))['ok']
        assert peer.state == 'A' and len(peer.calls) == 3
        # An immediate replay without an intervening write remains suppressed.
        assert (await child.execute('write_data_file', {'path': 'tmp/audit.txt', 'content': 'A', 'overwrite': True}))['reused_result']
        assert len(peer.calls) == 3
    asyncio.run(check())


def test_strong_submission_cache_survives_intervening_mutation():
    async def check():
        schema = {'type': 'object', 'properties': {'client_request_id': {'type': 'string'}}, 'additionalProperties': False}
        registered = [SimpleNamespace(name=name, description='', inputSchema=schema)
                      for name in ('submit_single_source', 'write_data_file')]
        peer = Peer(registered)
        gateway = ProjectGateway(peer, {'submit_single_source', 'write_data_file'}, set(), str)
        assert (await gateway.execute('submit_single_source', {'client_request_id': 'a'}))['ok']
        assert (await gateway.execute('write_data_file', {}))['ok']
        assert (await gateway.execute('submit_single_source', {'client_request_id': 'b'}))['reused_result']
        assert len(peer.calls) == 2
    assert runtime.SUBMIT_TOOLS <= STRONG_IDEMPOTENT_TOOLS
    asyncio.run(check())


def test_unknown_mutation_barrier_survives_other_write():
    async def check():
        peer = Peer(await server.mcp.list_tools())
        gateway = ProjectGateway(peer, {'write_data_file'}, runtime.READ_TOOLS, str)
        args = {'path': 'tmp/audit.txt', 'content': 'A', 'overwrite': True}
        peer.fail = True
        assert (await gateway.execute('write_data_file', args))['error']['effect'] == 'unknown'
        peer.fail = False
        await gateway.execute('write_data_file', {**args, 'content': 'B'})
        assert (await gateway.execute('write_data_file', args))['dispatch_blocked']
        assert len(peer.calls) == 2
    asyncio.run(check())


def test_live_gui_reads_refresh_and_immutable_reads_stay_bounded():
    async def check():
        peer = Peer(await server.mcp.list_tools())
        gateway = ProjectGateway(peer, {'read_window', 'input_schema'}, runtime.READ_TOOLS, str)
        for revision in range(4):
            peer.state = revision
            assert (await gateway.execute('read_window', {'window_type': 'main'}))['content'] == revision
        assert len(peer.calls) == 4
        for _ in range(3):
            assert (await gateway.execute('input_schema', {'kind': 'machine'}))['ok']
        assert (await gateway.execute('input_schema', {'kind': 'machine'}))['error']['code'] == 'repeated_read'
    asyncio.run(check())


def test_analyst_cannot_export_or_write_receipts(tmp_path):
    async def check():
        peer = Peer(await server.mcp.list_tools())
        writer = MagicMock()
        writer.redact.side_effect = str
        with patch.object(runtime, 'STATE_DIRECTORY', tmp_path), patch.object(runtime, 'dispatch_authority', return_value={'allowed': True}):
            meter = runtime.BudgetAndTools(MagicMock(), {'task_mode': 'research'}, 's', 'r', 'offline', writer, MagicMock(), client=cast(runtime.Client, peer), role='analyst')
            async def forbidden(args):
                raise AssertionError('Forbidden analyst dispatch')
            for name, args in [('read_calculation_result', {'run_id': 'r', 'export_data': True}),
                               ('estimate_compute_task', {}), ('estimate_film_optimization', {}), ('estimate_appearance_render', {})]:
                call = ToolCallPart(name, args, tool_call_id=name)
                result = await meter.wrap_tool_execute(None, call=call, tool_def=None, args=args, handler=forbidden)
                assert result['ok'] is False
            assert peer.calls == []
            call = ToolCallPart('read_window', {'window_type': 'main'}, tool_call_id='read')
            await meter.wrap_tool_execute(None, call=call, tool_def=None, args=call.args, handler=forbidden)
            assert len(peer.calls) == 1
    asyncio.run(check())
    source = Path(runtime.__file__).read_text()
    assert 'toolsets=[mcp.filtered(lambda ctx, tool: tool.name in DISCUSSION_READ_TOOLS)]' in source
    assert 'project_bbs_comment' not in runtime.DISCUSSION_READ_TOOLS
