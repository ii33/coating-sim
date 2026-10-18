"""Discussion is enforced before RPC, side effects, old approvals or delegation."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from fastmcp import Client
import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from coat_sim.agent import runtime
from coat_sim.agent.storage import AssistantStore
from coat_sim.agent.tool_dispatch import ProjectGateway
from coat_sim.mcp import server


TARGET = {"client_id": "client", "client_generation": "client-generation", "project_id": "project",
          "session_generation": "session-generation", "root": "/synthetic/project",
          "project_file": "/synthetic/project/name.coatproj"}
DENIED = {
    "save_design", "save_film_design", "prepare_temporary_design", "save_generated_design",
    "save_simulation_asset", "write_data_file", "make_data_directory", "move_data_path", "delete_data_path",
    "project_open", "project_manage", "project_bbs_import_attachment", "update_window", "window_action", "open_window", "close_window",
    "submit_single_source", "submit_process_recipe", "submit_mask_optimization", "submit_film_optimization",
    "submit_appearance_render", "run_python_script", "submit_python_script", "cancel_calculation_job",
    "estimate_compute_task", "estimate_film_optimization", "estimate_appearance_render", "estimate_mask_optimization",
    "request_compute_confirmation", "create_default_workflow", "create_design_workflow",
    "advance_design_workflow", "cancel_design_workflow", "render_design", "render_design_workflow",
    "visualize_calculation_result", "export_pdf", "export_simulation_asset", "create_material", "update_material",
    "developer_files", "developer_access", "developer_persona", "developer_report_issue", "developer_checks",
    "simulate_process_recipe", "simulate_single_source", "external_action", "unknown_legacy_save_alias",
}


async def _actual_run(calls=(), *, recovered=False, cancel=False, mode="chat", bind=True, switch_after_write=False, switch_back=False, instructions=None, previous_mode=None):
    registered = await server.mcp.list_tools()
    rpc = []
    returned: list[ToolReturnPart] = []
    observed_tools = []
    ui: dict[str, AssistantStore] = {}
    class Peer:
        async def list_tools(self):
            return registered
        async def call_tool_mcp(self, name, arguments):
            rpc.append((name, arguments))
            allowed = {"read_window", "project_editor_snapshot", "validate_film_design", "read_calculation_result", "project_bbs_comment", "project_bbs_import_attachment"}
            if switch_after_write:
                allowed.add("save_design")
            assert name in allowed
            if name == "save_design":
                ui["store"].set_ui_session_mode("discussion-session", "chat")
                if switch_back:
                    ui["store"].set_ui_session_mode("discussion-session", mode)
            return SimpleNamespace(structured_content={"available": True, "snapshot_id": "snapshot", "status": "read"},
                                   content=[], is_error=False)
    requests = 0
    def respond(messages, info):
        nonlocal requests
        requests += 1
        observed_tools.append({tool.name for tool in info.function_tools})
        if instructions is not None:
            instructions.append(info.instructions)
        if cancel:
            raise asyncio.CancelledError()
        if (requests == 1 and calls) or (switch_after_write and requests == 2):
            selected = calls[:1] if switch_after_write and requests == 1 else calls[1:] if switch_after_write else calls
            parts = []
            for index, item in enumerate(selected):
                name, arguments = item[:2]
                entry = item[2] if len(item) > 2 else "project_action"
                envelope = {"tool_name": name, "arguments": arguments} if entry == "project_action" else arguments
                parts.append(ToolCallPart(entry, envelope, tool_call_id="call-" + str(index)))
            return ModelResponse(parts=parts)
        for message in messages:
            returned.extend(part for part in message.parts if isinstance(part, ToolReturnPart))
        return ModelResponse(parts=[TextPart("Discussion plan prepared")])
    async def stream(messages, info):
        response = respond(messages, info)
        for index, part in enumerate(response.parts):
            if isinstance(part, ToolCallPart):
                yield {index: DeltaToolCall(part.tool_name, json.dumps(part.args), tool_call_id=part.tool_call_id)}
            elif isinstance(part, TextPart):
                yield part.content
    writer = MagicMock()
    writer.redact.side_effect = str
    writer.quota_config = {}
    writer.consume_decision_answers.return_value = []
    writer.pending_messages.return_value = []
    writer.consume_compaction.return_value = False
    external = MagicMock()
    external.sync = AsyncMock()
    external.configs = [{"id": "synthetic"}]
    external.execute = AsyncMock(side_effect=AssertionError("External action prohibited"))
    mcp = MagicMock()
    mcp.__aenter__ = AsyncMock()
    mcp.__aexit__ = AsyncMock(return_value=False)
    config = {"assistant_enabled": True, "assistant_model": "offline-discussion-test",
              "assistant_api_url": "http://localhost:12345/v1", "task_mode": mode,
              "assistant_system_language": "zh-CN"}
    command: dict[str, Any] = {"config": config, "session_id": "discussion-session", "request_id": "discussion-request",
               "api_key": "", "prompt": "读取界面并讨论方案，不执行任何操作"}
    cancel_jobs = AsyncMock()
    with tempfile.TemporaryDirectory(prefix="discussion-test-") as directory, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"COAT_DEVELOPMENT": "1", "COAT_RELEASE_BUILD": "0"}))
        stack.enter_context(patch.object(runtime, "STATE_DIRECTORY", Path(directory)))
        stack.enter_context(patch.object(runtime, "HARNESS_DATABASE", Path(directory) / "harness.sqlite3"))
        store = AssistantStore(Path(directory) / "state.sqlite3")
        ui["store"] = store
        with store._db() as database:
            database.execute("INSERT INTO conversations VALUES(?,?,?)", (command["session_id"], "Synthetic UI", 0))
        if previous_mode is not None:
            store.set_ui_session_mode(command["session_id"], previous_mode)
            store.save_history(command["session_id"], runtime.ModelMessagesTypeAdapter.dump_json([
                ModelRequest(parts=[runtime.UserPromptPart("此前的模式是什么？")]),
                ModelResponse(parts=[TextPart("当前为讨论模式，只能讨论。")])]))
        if bind:
            store.set_ui_session_mode(command["session_id"], mode)
            store.bind_ui_request(command["session_id"], command["request_id"], mode)
        if recovered:
            store.save_history(command["session_id"], runtime.ModelMessagesTypeAdapter.dump_json([
                ModelRequest(parts=[runtime.UserPromptPart("以前批准过提交")]),
                ModelResponse(parts=[ToolCallPart("project_action", {"tool_name": "submit_process_recipe",
                    "arguments": {"approved": True, "mode": "operate"}}, tool_call_id="old-write")])]),
                request_id="old-request", state="active")
            ledger = runtime.JobLedger()
            ledger.intent(command["session_id"], "old-request", "submit_process_recipe", "old-request:old-write")
        profile = runtime._chat_model(config["assistant_model"], runtime.reasoning_capabilities(
            config["assistant_api_url"], config["assistant_model"]), MagicMock()).profile
        stack.enter_context(patch.object(runtime, "_business_server", return_value=(Peer(), mcp)))
        stack.enter_context(patch.object(runtime, "external_pool", return_value=external))
        stack.enter_context(patch("coat_sim.core.backend_memory.successful_backend_memory", return_value={}))
        stack.enter_context(patch.object(runtime, "read_api_key", side_effect=AssertionError("No credential reads")))
        stack.enter_context(patch.object(runtime, "_chat_model", return_value=FunctionModel(respond, stream_function=stream, profile=profile)))
        stack.enter_context(patch.object(runtime, "AsyncOpenAI"))
        stack.enter_context(patch.object(runtime, "_cancel_jobs", cancel_jobs))
        # Proactor checks isinstance(handle, socket.socket) for its existing
        # self pipe. Preserve the class while still forbidding every new socket.
        stack.enter_context(patch.object(socket.socket, "__init__", side_effect=AssertionError("No network")))
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_task(command, writer)
        else:
            await runtime.run_task(command, writer)
    external.execute.assert_not_awaited()
    cancel_jobs.assert_not_awaited()
    return rpc, returned, observed_tools


def test_actual_model_project_action_cannot_dispatch_mutations_or_legacy_aliases():
    calls = [(name, {"approved": True, "mode": "operate", "origin": "desktop"}) for name in sorted(DENIED)]
    rpc, returned, tools = asyncio.run(_actual_run(calls))
    assert rpc == []
    assert len(returned) == len(calls)
    assert all(part.content["error"]["code"] == "discussion_read_only" for part in returned)
    assert all(part.content["error"]["effect"] == "none" for part in returned)
    assert all("external_action" not in names and "delegate_task" not in names for names in tools)


def test_discussion_reads_current_ui_and_validation_but_rejects_export_and_extra_actions():
    calls = [
        ("read_window", {"window_type": "film_design", "project_target": TARGET}),
        ("project_editor_snapshot", {"window_type": "film_optimization", "project_target": TARGET}),
        ("validate_film_design", {"data": {}}),
        ("read_calculation_result", {"run_id": "existing", "export_data": False}),
        ("read_calculation_result", json.dumps({"run_id": "existing", "export_data": True})),
        ("read_calculation_result", {"run_id": "existing", "export_data": "false"}),
        ("read_window", {"window_type": "film_design", "action": "save"}),
        ("project_editor_snapshot", {"window_type": "film_design", "project_target": TARGET, "fields": {"name": "changed"}}),
    ]
    rpc, returned, _tools = asyncio.run(_actual_run(calls))
    assert sorted(name for name, _ in rpc) == sorted(["read_window", "project_editor_snapshot", "validate_film_design", "read_calculation_result"])
    errors = [part.content.get("error", {}).get("code") for part in returned]
    assert errors[-4:] == ["discussion_read_only", "discussion_read_only", "invalid_arguments", "invalid_arguments"]


def test_discussion_can_read_every_registered_editor_module():
    from coat_sim.project_ui import PROJECT_EDITOR_DETAILS
    calls = [("project_editor_snapshot", {"window_type": module, "project_target": TARGET})
             for module in PROJECT_EDITOR_DETAILS]
    rpc, returned, _tools = asyncio.run(_actual_run(calls))
    # Read-only tool calls may run concurrently; preserve exact multiplicity,
    # without assuming completion order across independent windows.
    assert sorted(arguments["window_type"] for _name, arguments in rpc) == sorted(PROJECT_EDITOR_DETAILS)
    assert all("error" not in part.content for part in returned)


def test_recovered_history_and_old_approved_request_do_not_resume_or_release():
    rpc, _returned, _tools = asyncio.run(_actual_run(recovered=True))
    assert rpc == []


def test_cancelling_discussion_never_cancels_preexisting_background_jobs():
    rpc, _returned, _tools = asyncio.run(_actual_run(cancel=True))
    assert rpc == []


def test_discussion_catalog_hides_execution_and_estimate_receipt_schemas():
    async def check():
        tools = await server.mcp.list_tools()
        peer = SimpleNamespace(list_tools=AsyncMock(return_value=tools))
        gateway = ProjectGateway(peer, runtime.allowed_project_tools("chat", development=True),
                                 runtime.READ_TOOLS, str, read_only_mode=True)
        result = await gateway.catalog()
        names = {row["name"] for row in result["tools"]}
        assert "project_editor_snapshot" in names
        assert not names & DENIED
        assert result["read_only"] is True and result["preferred_workflow"] is None
        assert not set(result["batch_schema_arguments"]["tool_names"]) & DENIED
    asyncio.run(check())


def test_background_success_in_discussion_never_advances_export_workflow():
    calls = []
    class Peer:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def call_tool_mcp(self, name, arguments):
            calls.append(name)
            assert name in {"calculation_job_status", "calculation_job_result"}
            payload = {"job_id": "old-job", "state": "succeeded"} if name.endswith("status") else {"kind": "existing_result"}
            return SimpleNamespace(structured_content=payload, content=[], is_error=False)
    meter = SimpleNamespace(mode="chat", writer=SimpleNamespace(redact=str),
        ledger=SimpleNamespace(update_job=lambda value: None), _observe_tool=lambda *args: None,
        archive=SimpleNamespace(project=lambda value, **kwargs: value), _upsert_artifact=lambda value: None)
    store = SimpleNamespace(request_was_stopped=lambda *args: False)
    result = asyncio.run(runtime._background_completion_context({"job_id": "old-job", "workflow_run_id": "old-workflow",
        "origin": {"session_id": "session", "request_id": "old-request"}},
        client=cast(Client, Peer()), meter=cast(runtime.BudgetAndTools, meter),
        store=cast(AssistantStore, store), session="session"))
    assert calls == ["calculation_job_status", "calculation_job_result"]
    assert result["workflow"]["state"] == "not_advanced_in_discussion"


@pytest.mark.parametrize("mode", ["operate", "research"])
@pytest.mark.parametrize("switch_back", [False, True])
def test_dynamic_ui_mode_epoch_blocks_later_writes_external_actions_and_old_run(mode, switch_back):
    first = {"kind": "source", "path": "data/sources/first.csrc", "data": {}, "overwrite": False}
    second = {**first, "path": "data/sources/second.csrc"}
    rpc, returned, _tools = asyncio.run(_actual_run([
        ("save_design", first), ("save_design", second),
        ("external_action", {"server_id": "synthetic", "tool_name": "write", "arguments": {}}, "external_action"),
        ("read_calculation_result", {"run_id": "existing", "export_data": True}),
        ("read_window", {"window_type": "film_design"}),
    ], mode=mode, switch_after_write=True, switch_back=switch_back))
    assert [name for name, _arguments in rpc].count("save_design") == 1
    assert "read_window" in {name for name, _arguments in rpc}
    blocked = [part.content.get("error", {}).get("code") for part in returned]
    assert blocked.count("assistant_dispatch_blocked") == 3
    assert "read_calculation_result" not in {name for name, _arguments in rpc}


def test_missing_ui_provenance_blocks_operational_writes_even_with_forged_modes():
    rpc, returned, _tools = asyncio.run(_actual_run([
        ("save_design", {"kind": "source", "path": "data/sources/file.csrc", "data": {}, "overwrite": False}),
        ("external_action", {"server_id": "synthetic", "tool_name": "write", "arguments": {"mode": "research"}}, "external_action"),
    ], mode="operate", bind=False))
    assert rpc == []
    assert all(part.content["error"]["code"] == "assistant_dispatch_blocked" for part in returned)


def test_mode_change_between_prepared_submit_and_automatic_release_does_not_release():
    from tests.test_ai_film_operations_contract import _FakeClient, _RuntimeHarness
    class SwitchPeer(_FakeClient):
        async def call_tool_mcp(self, name, arguments):
            reply = await super().call_tool_mcp(name, arguments)
            if name == "submit_film_optimization":
                meter.store.set_ui_session_mode(meter.session, "chat")
            return reply
    peer = SwitchPeer()
    meter = _RuntimeHarness(client=peer)
    gateway = ProjectGateway(peer, runtime.MAIN_TOOLS, runtime.READ_TOOLS, str,
                             shared=meter.shared["gateway_guard"])
    async def execute(arguments):
        return await gateway.execute(arguments["tool_name"], arguments["arguments"])
    result = asyncio.run(meter.wrap_tool_execute(None,
        call=SimpleNamespace(tool_name="project_action", tool_call_id="submit"), tool_def=None,
        args={"tool_name": "submit_film_optimization", "arguments": {"request": {}}}, handler=execute))
    assert [name for name, _arguments in peer.calls] == ["submit_film_optimization"]
    assert result["release_blocked"]["code"] == "assistant_dispatch_blocked"


def test_cancel_cleanup_rechecks_authority_between_each_job():
    calls = []
    allowed = [True]
    class Peer:
        async def call_tool_mcp(self, name, arguments):
            return SimpleNamespace(structured_content={"jobs": []}, is_error=False)
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            allowed[0] = False  # The UI changes mode after the first atomic RPC.
            return {"job_id": arguments["job_id"], "state": "cancelled"}
    ledger = SimpleNamespace(unsettled=lambda *args: [
        {"job_id": "one", "client_request_id": "one"}, {"job_id": "two", "client_request_id": "two"}],
        update_job=lambda value: None)
    asyncio.run(runtime._cancel_jobs(cast(Client, Peer()), cast(runtime.JobLedger, ledger), "session", "request", set(),
                                    can_mutate=lambda: allowed[0]))
    assert calls == [("cancel_calculation_job", {"job_id": "one"})]


@pytest.mark.parametrize("mode", ["chat", "operate", "research"])
def test_each_model_request_sees_current_ui_mode_over_old_history(mode):
    instructions: list[str] = []
    _rpc, _returned, tools = asyncio.run(_actual_run(
        [("read_window", {"window_type": "film_design"})], mode=mode,
        previous_mode="chat", instructions=instructions))
    assert len(instructions) == 2
    for text in instructions:
        assert '"current_mode": "' + mode + '"' in text
        assert '"request_mode": "' + mode + '"' in text
        assert '"current_generation": ' + ("1" if mode == "chat" else "2") in text
        assert '"side_effects_allowed": ' + ("false" if mode == "chat" else "true") in text
    assert all(("delegate_task" in names) == (mode != "chat") for names in tools)


@pytest.mark.parametrize("switch_back", [False, True])
def test_request_boundary_observes_revoked_epoch_without_upgrading_old_turn(switch_back):
    instructions: list[str] = []
    first = {"kind": "source", "path": "data/sources/first.csrc", "data": {}, "overwrite": False}
    rpc, returned, _tools = asyncio.run(_actual_run([
        ("save_design", first), ("save_design", {**first, "path": "data/sources/second.csrc"}),
    ], mode="operate", switch_after_write=True, switch_back=switch_back, instructions=instructions))
    assert '"side_effects_allowed": true' in instructions[0]
    for text in instructions[1:]:
        assert '"side_effects_allowed": false' in text
        assert '"request_generation": 1' in text
        assert '"current_generation": ' + ("3" if switch_back else "2") in text
        assert '"current_mode": "' + ("operate" if switch_back else "chat") + '"' in text
        assert "不能恢复或升级旧请求" in text
    assert len(rpc) == 1
    assert any(part.content.get("error", {}).get("code") == "assistant_dispatch_blocked" for part in returned)


def test_missing_authority_is_reported_as_unknown_not_configured_operate_mode():
    instructions: list[str] = []
    asyncio.run(_actual_run(mode="operate", bind=False, instructions=instructions))
    assert '"current_mode": "unknown"' in instructions[0]
    assert '"side_effects_allowed": false' in instructions[0]


@pytest.mark.parametrize("role", ["main", "execute_machine", "analysis", "summary"])
def test_live_upgrade_cannot_upgrade_chat_turn_or_config_override_authority(tmp_path, role):
    store = AssistantStore(tmp_path / "state.sqlite3")
    with store._db() as database:
        database.execute("INSERT INTO conversations VALUES(?,?,?)", ("session", "UI", 0))
    store.set_ui_session_mode("session", "chat")
    store.bind_ui_request("session", "request", "chat")
    meter = object.__new__(runtime.BudgetAndTools)
    meter.store, meter.session, meter.request = store, "session", "request"
    meter.mode, meter.role = "chat", role
    meter.config = {"task_mode": "research"}
    meter.shared, meter.writer = {}, MagicMock()
    store.set_ui_session_mode("session", "operate")
    text = meter._current_mode_instruction()
    assert '"current_mode": "operate"' in text
    assert '"request_mode": "chat"' in text
    assert '"tool_scope_mode": "chat"' in text
    assert '"side_effects_allowed": false' in text
    assert '"current_generation": 2' in text
    assert "不能恢复或升级旧请求" in text


def test_unreadable_authority_emits_bounded_state_without_path_or_error_details():
    meter = object.__new__(runtime.BudgetAndTools)
    meter.store = cast(AssistantStore, SimpleNamespace(path="/private/credentials/missing.sqlite3"))
    meter.session, meter.request, meter.mode, meter.role = "session", "request", "operate", "execute_machine"
    meter.shared, meter.writer = {}, MagicMock()
    text = meter._current_mode_instruction()
    assert '"current_mode": "unknown"' in text
    assert '"side_effects_allowed": false' in text
    assert "/private/credentials" not in text
    args, kwargs = meter.writer.emit.call_args
    assert args == ("diagnostic", "request")
    assert kwargs["source"] == "execute_machine"
    assert kwargs["phase"] == "mode_authority"
    assert kwargs["side_effects_allowed"] is False
    assert "/private/credentials" not in str(kwargs)
    meter._current_mode_instruction()
    meter.writer.emit.assert_called_once()
