"""Real SDK contract regressions; all mutation/UI boundaries are inert spies."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.core import data_files
from coat_sim.core.capabilities import MCP_ERROR_MARKER, POLICIES
from coat_sim.core.mcp_contracts import tool_annotations
from coat_sim.mcp import server


def call(name, arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


@pytest.mark.parametrize('tool,base,field', [
    ('write_data_file', {'path': 'data/tmp/audit.txt', 'content': 'synthetic'}, 'overwrite'),
    ('delete_data_path', {'path': 'data/tmp/audit.txt'}, 'recursive'),
    ('update_window', {'window_type': 'main'}, 'force'),
    ('save_simulation_asset', {'run_id': 'audit', 'name': 'audit'}, 'confirmed'),
])
@pytest.mark.parametrize('value', [1, 0, 'true', 'false', None])
def test_flags_fail_before_sdk(tool, base, field, value):
    with patch.object(MCPServer, 'call_tool', new_callable=AsyncMock) as sdk:
        with pytest.raises(ToolError, match='invalid_arguments') as exc:
            call(tool, {**base, field: value})
        sdk.assert_not_called()
        envelope = json.loads(str(exc.value).split(MCP_ERROR_MARKER)[1])
        assert envelope['effect'] == 'none'


@pytest.mark.parametrize('extra', ['project_target', 'project_id', 'root', 'overwite'])
@pytest.mark.parametrize('tool,base', [
    ('write_data_file', {'path': 'data/tmp/audit.txt', 'content': 'synthetic', 'overwrite': True}),
    ('delete_data_path', {'path': 'data/tmp/audit.txt', 'recursive': True}),
])
def test_invalid_target_never_reaches_data_owner(tool, base, extra):
    with patch.object(data_files, tool, return_value={'spy': True}) as owner:
        with pytest.raises(ToolError, match='invalid_arguments'):
            call(tool, {**base, extra: {'project_id': 'other'}})
        owner.assert_not_called()


@pytest.mark.parametrize('flag', [True, False])
@pytest.mark.parametrize('tool,base,field', [
    ('write_data_file', {'path': 'data/tmp/audit.txt', 'content': 'synthetic'}, 'overwrite'),
    ('delete_data_path', {'path': 'data/tmp/audit.txt'}, 'recursive'),
])
def test_real_json_booleans_reach_real_sdk_handler(tool, base, field, flag):
    with patch.object(data_files, tool, return_value={'spy': True}) as owner:
        result = call(tool, {**base, field: flag})
        assert not result.is_error
        assert (owner.call_args.args[1] if tool == 'delete_data_path' else owner.call_args.kwargs[field]) is flag


def test_every_registered_envelope_is_closed_and_invalid_calls_are_inert():
    tools = asyncio.run(server.mcp.list_tools())
    with patch.object(MCPServer, 'call_tool', new_callable=AsyncMock) as sdk:
        for tool in tools:
            assert tool.input_schema['additionalProperties'] is False
            with pytest.raises(ToolError, match='invalid_arguments'):
                call(tool.name, {'__unknown_argument': True})
        sdk.assert_not_called()


@pytest.mark.parametrize('settings', [
    {'grid_size': '21'}, {'grid_size': True}, {'grid_size': 21.0},
    {'grid_size': 20}, {'unknown_setting': 1}, {'duration_s': float('inf')},
])
def test_nested_ref_constraints_before_submission(settings):
    with patch.object(server, '_submit_job') as submit:
        with pytest.raises(ToolError, match='invalid_arguments'):
            call('submit_single_source', {'settings': settings})
        submit.assert_not_called()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), {1, 2}, {1: 'value'}])
def test_any_domain_content_must_still_be_finite_json(value):
    with patch.object(server, '_project_research_store') as store:
        with pytest.raises(ToolError, match='invalid_arguments'):
            call('project_research_save', {'root': 'p.coatproj', 'project_id': 'p',
                                           'record': {'custom': value}})
        store.assert_not_called()


def test_domain_any_maps_keep_legitimate_keys():
    record = {'custom': {'unmodeled_key': [1, True, None, 'value'], 'domain_sequence': (1.0, 2.0)}}
    with patch.object(server, '_project_research_store') as store:
        store.return_value.save.return_value = {'evidence_id': 'synthetic'}
        result = call('project_research_save', {'root': 'p.coatproj', 'project_id': 'p', 'record': record})
        assert not result.is_error
        store.return_value.save.assert_called_once_with(record)


def test_nested_project_target_closed_at_ref_before_ui():
    target = {field: 'synthetic' for field in ('client_id', 'client_generation', 'project_id',
             'session_generation', 'root', 'project_file')}
    with patch.object(server, '_project_ui_call') as ui:
        with pytest.raises(ToolError, match='invalid_arguments'):
            call('update_window', {'window_type': 'film_design', 'project_target': {**target, 'extra': 1}})
        ui.assert_not_called()


def test_discovery_annotations_follow_effects_and_mixed_tools_are_conservative():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert all(tool.annotations is not None for tool in tools.values())
    for name, policy in POLICIES.items():
        assert tools[name].annotations.read_only_hint == (policy.effect == 'read')
    for name in ('read_data_file', 'project_editor_snapshot', 'project_list'):
        assert tools[name].annotations.read_only_hint is True
    for name in ('read_calculation_result', 'developer_issue_reports', 'estimate_compute_task',
                 'delete_data_path', 'write_data_file', 'window_action'):
        if name in tools:
            assert tools[name].annotations.read_only_hint is False
            assert tools[name].annotations.destructive_hint is True
            assert tools[name].annotations.idempotent_hint is False
    with pytest.raises(ValueError, match='explicit effect classification'):
        tool_annotations('new_unclassified_tool')


def test_documented_compute_gate_and_standalone_exception():
    with patch.object(server, '_ui_call', return_value={'spy': True}) as ui:
        with pytest.raises(ToolError, match='compute_confirmation_required'):
            call('window_action', {'window_type': 'main', 'action': 'calculate'})
        ui.assert_not_called()
        assert not call('window_action', {'window_type': 'film_simulation', 'action': 'calculate'}).is_error
        ui.assert_called_once()
    text = (Path(server.__file__).parents[1] / 'agent/skills/coat-mcp/SKILL.md').read_text()
    assert 'bound update/window_action 当前明确不支持' not in text
    assert '旧的同步沉积入口' not in text
    assert 'expected_snapshot_id' in text and 'estimate_compute_task → request_compute_confirmation' in text


@pytest.mark.parametrize('arguments', [[], 'not-an-object', True, 1])
def test_nonobject_arguments_fail_before_sdk(arguments):
    with patch.object(MCPServer, 'call_tool', new_callable=AsyncMock) as sdk:
        with pytest.raises(ToolError, match='invalid_arguments'):
            call('project_list', arguments)
        sdk.assert_not_called()
