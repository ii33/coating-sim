"""MCP project-window operations stay bound to an authenticated project owner."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import TypeAdapter, ValidationError

from coat_sim import assistant_process as assistant_process_module
from coat_sim.assistant_process import ApplicationClientRegistry, AssistantProcessClient, _Client
from coat_sim.core.project_store import ProjectStore
from coat_sim.mcp import server
from coat_sim.project_activation import activation_target
from coat_sim.project_management import ProjectManagementService
from coat_sim.ui.control import UiControlError


pytestmark = pytest.mark.contract


TARGET = {
    "client_id": "client-owner",
    "client_generation": "client-generation-1",
    "project_id": "project-id-1",
    "session_generation": "session-generation-1",
    "root": "/tmp/project",
    "project_file": "/tmp/project/project.coatproj",
}


def _window_type(value: str) -> server.UiWindowType:
    """Validate canonical module rows against the public MCP window union."""
    return TypeAdapter[server.UiWindowType](server.UiWindowType).validate_python(value)


@pytest.mark.parametrize("change", [
    lambda target: {key: value for key, value in target.items() if key != "root"},
    lambda target: target | {"unexpected": "field"},
    lambda target: target | {"client_id": ""},
    lambda target: target | {"client_id": "   "},
    lambda target: target | {"project_file": None},
    lambda target: target | {"client_generation": True},
    lambda target: target | {"session_generation": 1},
])
def test_project_target_requires_exact_six_nonempty_strict_strings(change):
    with pytest.raises(ValidationError):
        server.ProjectTarget.model_validate(change(TARGET))


def test_project_target_schema_is_closed_and_requires_every_identity_field():
    schema = server.ProjectTarget.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(TARGET)
    assert set(schema["properties"]) == set(TARGET)
    assert all(schema["properties"][field]["type"] == "string" for field in TARGET)


def test_registered_mcp_windows_reference_the_same_strict_project_target_and_agent_allowlists():
    from coat_sim.agent.runtime import COORDINATOR_TOOLS, EXECUTION_TOOLS, MAIN_TOOLS, READ_TOOLS

    registered = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    window_tools = {"list_windows", "open_window", "read_window", "update_window", "window_action", "close_window"}
    assert "project_open" in registered and "project_open" in MAIN_TOOLS
    assert "project_open" not in READ_TOOLS
    assert "project_open" not in COORDINATOR_TOOLS
    assert "project_open" in EXECUTION_TOOLS["windows"]
    assert window_tools <= registered.keys()
    for name in window_tools:
        schema = getattr(registered[name], "input_schema", None)
        if schema is None:
            schema = registered[name].inputSchema
        target_property = schema["properties"]["project_target"]
        target_ref = next(item["$ref"] for item in target_property.get("anyOf", [target_property])
                          if "$ref" in item)
        target_schema = schema["$defs"][target_ref.rsplit("/", 1)[-1]]
        assert target_schema["additionalProperties"] is False
        assert set(target_schema["required"]) == set(TARGET)
        assert set(target_schema["properties"]) == set(TARGET)
        assert all(target_schema["properties"][field]["type"] == "string" for field in TARGET)


def test_mcp_sdk_rejects_malformed_project_target_before_transport(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, operation, **arguments):
        calls.append((operation, arguments))
        return {"ok": True}

    monkeypatch.setattr(AssistantProcessClient, "request", request)
    for bad_target in (
            {key: value for key, value in TARGET.items() if key != "root"},
            TARGET | {"extra": "field"},
            TARGET | {"client_generation": True},
            TARGET | {"root": "   "}):
        with pytest.raises(ToolError):
            asyncio.run(server.mcp.call_tool("list_windows", {"project_target": bad_target}))
    assert calls == []


def test_project_window_client_request_contains_only_bound_identity_and_arguments(monkeypatch):
    calls = []
    reply = {"windows": [{"module_key": "film_design", "instance_id": "designer-1"}]}

    def request(self, operation, **arguments):
        calls.append((operation, arguments))
        return reply

    monkeypatch.setattr(AssistantProcessClient, "request", request)

    assert server._project_ui_call("open_window", TARGET, window_type="film_design") is reply
    assert calls == [("project_window", {
        "project_target": TARGET,
        "window_operation": "open_window",
        "arguments": {"window_type": "film_design"},
    })]


def test_project_list_adds_client_generation_and_exact_project_target(monkeypatch):
    project = {
        "project_id": "project-id-1",
        "generation": "session-generation-1",
        "root": "/tmp/project",
        "project_file": "/tmp/project/project.coatproj",
        "name": "Study",
    }
    client = {
        "client_id": "client-owner",
        "generation": "client-generation-1",
        "state": "alive",
        "project_sessions": [project],
    }
    monkeypatch.setattr(AssistantProcessClient, "list_clients", lambda self: [client])

    result = server.project_list()

    row = result["projects"][0]
    assert row["client_generation"] == client["generation"]
    assert row["project_target"] == activation_target(client, project)
    assert result["state"] == "ready"


def test_legacy_window_calls_without_project_target_keep_the_generic_route(monkeypatch):
    calls = []

    def generic(operation, **arguments):
        calls.append((operation, arguments))
        return {"ok": True}

    monkeypatch.setattr(server, "_ui_call", generic)

    server.list_windows()
    server.open_window("film_design")
    server.read_window("film_design")
    server.update_window("film_design")
    server.window_action("film_design", "export_csv")
    server.close_window("film_design")

    assert calls == [
        ("list_windows", {}),
        ("open_window", {"window_type": "film_design", "design_path": None, "fields": None,
                          "data": None, "intended_path": None, "pointers": None, "force": False}),
        ("read_window", {"window_type": "film_design"}),
        ("update_window", {"window_type": "film_design", "fields": None, "pointers": None,
                            "design_path": None, "data": None, "intended_path": None, "force": False}),
        ("window_action", {"window_type": "film_design", "action": "export_csv", "arguments": None}),
        ("close_window", {"window_type": "film_design", "force": False}),
    ]


def test_bound_list_open_read_and_close_use_project_window_route(monkeypatch):
    calls = []

    def request(self, operation, **arguments):
        calls.append((operation, arguments))
        return {"ok": True}

    monkeypatch.setattr(AssistantProcessClient, "request", request)

    server.list_windows(project_target=server.ProjectTarget.model_validate(TARGET))
    server.open_window("film_design", project_target=server.ProjectTarget.model_validate(TARGET))
    server.read_window("film_design", project_target=server.ProjectTarget.model_validate(TARGET))
    server.close_window("film_design", project_target=server.ProjectTarget.model_validate(TARGET))

    assert [call[0] for call in calls] == ["project_window"] * 4
    assert [call[1]["window_operation"] for call in calls] == [
        "list_windows", "open_window", "read_window", "close_window"]
    assert all(call[1]["project_target"] == TARGET for call in calls)
    assert calls[0][1]["arguments"] == {}
    assert calls[1][1]["arguments"]["window_type"] == "film_design"
    assert calls[2][1]["arguments"]["window_type"] == "film_design"
    assert calls[3][1]["arguments"]["window_type"] == "film_design"


def test_bound_routes_accept_all_project_modules_and_reject_global_only_targets(monkeypatch):
    from coat_sim.project_modules import PROJECT_MODULES

    calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, operation, **arguments):
        calls.append((operation, arguments))
        return {"ok": True}

    monkeypatch.setattr(AssistantProcessClient, "request", request)

    for module_key, _label in PROJECT_MODULES:
        server.open_window(_window_type(module_key), project_target=server.ProjectTarget.model_validate(TARGET))
        server.read_window(_window_type(module_key), project_target=server.ProjectTarget.model_validate(TARGET))
    assert len(calls) == 2 * len(PROJECT_MODULES)
    assert all(call[1]["window_operation"] in {"open_window", "read_window"} for call in calls)

    calls.clear()
    for global_only in ("launcher", "settings", "about"):
        with pytest.raises(ToolError):
            server.open_window(global_only, project_target=server.ProjectTarget.model_validate(TARGET))
        with pytest.raises(ToolError):
            server.read_window(global_only, project_target=server.ProjectTarget.model_validate(TARGET))
    assert calls == []


def test_bound_update_and_actions_require_snapshot_and_compute_never_routes(monkeypatch, registered_project_owner):
    # Guardless/unknown edits cannot reach the existing owner or a global
    # fallback. Real supported writes require both a fresh snapshot and instance.
    registry, _client, _store, _project, target, owner_calls, _state, _identities = registered_project_owner
    shared_calls = []

    def shared_request(self, operation, **arguments):
        shared_calls.append((operation, arguments))
        return registry.project_window(arguments["project_target"],
            arguments["window_operation"], arguments["arguments"])

    monkeypatch.setattr(AssistantProcessClient, "request", shared_request)
    monkeypatch.setattr(server, "_ui_call", lambda *args, **kwargs: pytest.fail("global UI fallback"))
    with pytest.raises(ToolError, match="requires_snapshot"):
        server.update_window("film_design", project_target=target)
    with pytest.raises(ToolError, match="requires_snapshot"):
        server.window_action("film_design", "export_csv", project_target=target)
    for module_key, action in (("film_simulation", "calculate"),
                               ("film_optimization", "start_optimization"),
                               ("main", "calculate"),
                               ("mask", "compute_weights")):
        with pytest.raises(ToolError, match="compute_confirmation_required|unsupported_operation"):
            server.window_action(_window_type(module_key), action, project_target=target)
    assert shared_calls == []  # Public validation rejects missing guards before any transport.
    assert not any(operation in {"update_window", "window_action"}
                   for operation, _arguments in owner_calls)


class _FakeOwnerTransport:
    def __init__(self, client, calls, state):
        self.client = client
        self.calls = calls
        self.state = state

    def __call__(self, *_args, **_kwargs):
        owner = self

        class FakeUiControlClient:
            def __init__(self, *_client_args, **_client_kwargs):
                pass

            def request(self, operation, **arguments):
                owner.calls.append((operation, arguments))
                if operation == "lifecycle_status":
                    if owner.state.get("disconnected"):
                        raise UiControlError("ui_unavailable", "project owner not reachable")
                    nonce = ("wrong-nonce" if owner.state.get("wrong_nonce") else arguments.get("nonce"))
                    return {
                        "application": "coat_sim",
                        "nonce": nonce,
                        "pid": owner.client.endpoint["pid"],
                        "process_start": owner.client.process_start,
                        "generation": owner.state.get("reported_client_generation", owner.client.generation),
                        "role": owner.client.role,
                        "project_sessions": deepcopy(owner.state.get("projects", owner.client.projects)),
                    }
                if operation == "open_window":
                    return {"instance_id": "window-1", "project_id": arguments.get("project_id"),
                            "generation": arguments.get("session_generation"),
                            "capabilities": {"operations": ["list_windows", "open_window", "read_window", "close_window"]}}
                if operation == "read_window":
                    return {"open": True, "visible": True, "instance_id": "window-1",
                            "project_id": arguments.get("project_id"),
                            "session_generation": arguments.get("session_generation"),
                            "module_key": arguments.get("module_key"),
                            "capabilities": {"operations": ["list_windows", "open_window", "read_window", "close_window"]}}
                if operation == "list_windows":
                    return {"project_id": arguments.get("project_id"),
                            "session_generation": arguments.get("session_generation"),
                            "windows": [], "available_windows": [],
                            "capabilities": {"operations": ["list_windows", "open_window", "read_window", "close_window"]}}
                if operation == "close_window":
                    return {"accepted": True, "project_id": arguments.get("project_id"),
                            "session_generation": arguments.get("session_generation")}
                return {"ok": True}

        return FakeUiControlClient()


@pytest.fixture
def registered_project_owner(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "Study", "Study")
    project = {
        "project_id": store.manifest["id"],
        "generation": "session-generation-1",
        "root": str(store.root),
        "project_file": str(store.project_file),
        "name": "Study",
    }
    client = _Client(
        client_id="client-owner",
        endpoint={"pid": 8101, "root": str(tmp_path / "runtime"), "role": "launcher"},
        generation="client-generation-1",
        process_start="creation-8101-1",
        role="launcher",
        projects=[dict(project)],
    )
    registry = ApplicationClientRegistry()
    registry._clients[client.client_id] = client
    calls: list[tuple[str, dict[str, Any]]] = []
    state = {"projects": [dict(project)]}
    process_identities = {8101: ("alive", client.process_start)}
    monkeypatch.setattr(assistant_process_module, "_process_identity",
                        lambda pid: process_identities.get(pid, ("dead", None)))
    monkeypatch.setattr(assistant_process_module, "UiControlClient",
                        _FakeOwnerTransport(client, calls, state))
    target = activation_target(client.public(), project)
    return registry, client, store, project, target, calls, state, process_identities


@pytest.mark.parametrize("change,expected_code", [
    (lambda target: target | {"client_generation": "old-client-generation"}, "stale_client"),
    (lambda target: target | {"session_generation": "old-session-generation"}, "stale_project"),
    (lambda target: target | {"root": "/tmp/copied-project"}, "project_identity_mismatch"),
    (lambda target: target | {"project_file": "/tmp/copied.coatproj"}, "project_identity_mismatch"),
])
def test_registry_rejects_stale_or_mismatched_window_target_without_ui_fallback(
        registered_project_owner, change, expected_code):
    registry, _client, _store, _project, target, calls, _state, _identities = registered_project_owner

    with pytest.raises(UiControlError) as error:
        registry.project_window(change(target), "list_windows", {})

    assert error.value.code == expected_code
    assert not any(operation not in {"lifecycle_status"} for operation, _arguments in calls)


@pytest.mark.parametrize("change,expected_code", [
    (lambda target: target | {"client_generation": "stale-client"}, "stale_client"),
    (lambda target: target | {"session_generation": "stale-project"}, "stale_project"),
    (lambda target: target | {"root": "/tmp/copied-root"}, "project_identity_mismatch"),
    (lambda target: target | {"project_file": "/tmp/copied.coatproj"}, "project_identity_mismatch"),
])
def test_stale_mcp_project_target_never_falls_back_to_global_ui(
        registered_project_owner, monkeypatch, change, expected_code):
    registry, _client, _store, _project, target, owner_calls, _state, _identities = registered_project_owner

    def shared_request(self, operation, **arguments):
        assert operation == "project_window"
        return registry.project_window(arguments["project_target"],
            arguments["window_operation"], arguments["arguments"])

    monkeypatch.setattr(AssistantProcessClient, "request", shared_request)
    monkeypatch.setattr(server, "_ui_call", lambda *args, **kwargs: pytest.fail("global UI fallback"))
    with pytest.raises(ToolError, match=expected_code):
        server.list_windows(project_target=change(target))
    assert not any(operation != "lifecycle_status" for operation, _arguments in owner_calls)


def test_registry_rejects_pid_reuse_bad_fresh_challenge_and_unavailable_owner(
        registered_project_owner):
    registry, _client, _store, _project, target, calls, state, process_identities = registered_project_owner

    process_identities[8101] = ("alive", "creation-8101-reused")
    with pytest.raises(UiControlError) as reused_pid:
        registry.project_window(target, "list_windows", {})
    assert reused_pid.value.code == "client_exited"
    process_identities[8101] = ("alive", "creation-8101-1")

    state["wrong_nonce"] = True
    with pytest.raises(UiControlError) as bad_challenge:
        registry.project_window(target, "list_windows", {})
    assert bad_challenge.value.code == "process_identity_mismatch"
    state.pop("wrong_nonce")

    # A fresh challenge returning a replacement client generation is also stale.
    state["reported_client_generation"] = "replacement-client-generation"
    with pytest.raises(UiControlError) as replacement:
        registry.project_window(target, "list_windows", {})
    assert replacement.value.code == "process_identity_mismatch"
    state.pop("reported_client_generation")

    state["disconnected"] = True
    with pytest.raises(UiControlError) as unavailable:
        registry.project_window(target, "list_windows", {})
    assert unavailable.value.code in {"ui_unavailable", "client_unavailable", "project_owner_unavailable"}
    assert not any(operation != "lifecycle_status" for operation, _arguments in calls)


def test_registry_rejects_copied_project_id_with_different_descriptor(registered_project_owner, tmp_path):
    import shutil

    registry, _client, store, _project, target, calls, _state, _identities = registered_project_owner
    copied_root = tmp_path / "Copied"
    shutil.copytree(store.root, copied_root)
    copied_file = copied_root / store.project_file.name
    copied_target = target | {"root": str(copied_root), "project_file": str(copied_file)}

    assert ProjectStore.open(copied_file).manifest["id"] == target["project_id"]
    with pytest.raises(UiControlError) as error:
        registry.project_window(copied_target, "list_windows", {})
    assert error.value.code == "project_identity_mismatch"
    assert not any(operation != "lifecycle_status" for operation, _arguments in calls)


def test_registry_routes_only_project_modules_and_preserves_actual_window_capabilities(
        registered_project_owner):
    from coat_sim.project_modules import PROJECT_MODULES

    registry, _client, _store, _project, target, calls, _state, _identities = registered_project_owner
    module_keys = {module for module, _label in PROJECT_MODULES}
    for module in sorted(module_keys):
        opened = registry.project_window(target, "open_window", {"window_type": module})
        assert opened["instance_id"] == "window-1"
        read = registry.project_window(target, "read_window", {"window_type": module})
        assert read["capabilities"]["operations"] == ["list_windows", "open_window", "read_window", "close_window"]
        assert read["module_key"] == module
    dispatched = [(operation, arguments) for operation, arguments in calls
                  if operation in {"open_window", "read_window"}]
    assert {arguments["module_key"] for _operation, arguments in dispatched} == module_keys
    assert all(arguments["module_key"] == arguments["window_type"] for _operation, arguments in dispatched)
    calls.clear()
    for global_only in ("launcher", "settings", "about"):
        with pytest.raises(UiControlError) as error:
            registry.project_window(target, "open_window", {"window_type": global_only})
        assert error.value.code == "invalid_window"
    assert calls == []


@pytest.mark.parametrize("operation,bad_reply", [
    ("open_window", {"instance_id": "window-1", "project_id": "wrong", "generation": "session-generation-1"}),
    ("read_window", {"open": True, "project_id": "project-id-1", "session_generation": "wrong"}),
    ("close_window", {"accepted": True, "project_id": "wrong", "session_generation": "session-generation-1"}),
    ("list_windows", {"project_id": "project-id-1", "session_generation": "session-generation-1",
                       "windows": [{"project_id": "other", "session_generation": "session-generation-1"}]}),
])
def test_registry_rejects_window_reply_with_wrong_session_identity(
        registered_project_owner, monkeypatch, operation, bad_reply):
    registry, client, _store, _project, target, calls, _state, _identities = registered_project_owner

    monkeypatch.setattr(registry, "_call", lambda _client, _operation, **_arguments: bad_reply)
    arguments = {} if operation == "list_windows" else {"window_type": "film_design"}
    with pytest.raises(UiControlError) as error:
        registry.project_window(target, operation, arguments)
    assert error.value.code == "window_unconfirmed"


def test_project_open_authorizes_descriptor_and_returns_only_a_confirmed_target(tmp_path, monkeypatch):
    from coat_sim.ui import project_runtime_client

    authorized_root = tmp_path / "authorized"
    store = ProjectStore.create(authorized_root / "Study", "Study")
    monkeypatch.setattr(server, "_project_management_service",
                        lambda: ProjectManagementService(authorized_roots=(authorized_root,)))
    from coat_sim.project_activation import activation_target
    project = {"project_id": store.manifest["id"], "generation": "session-generation-1",
               "root": str(store.root), "project_file": str(store.project_file)}
    client = {"client_id": "client-owner", "generation": "client-generation-1"}
    target = activation_target(client, project)
    events: list[tuple[Any, ...]] = []

    def open_runtime(project_file, *, client):
        events.append(("open", project_file, client))
        return store

    def discover_owner(shared, selected, own_generation):
        events.append(("discover", selected, own_generation))
        return target

    def activate(shared, selected_target):
        events.append(("activate", selected_target))
        return {"activated": True, "instance_id": "designer-1",
                "project_id": target["project_id"], "session_generation": target["session_generation"],
                "client_generation": target["client_generation"]}

    monkeypatch.setattr(project_runtime_client, "open_project_runtime", open_runtime)
    monkeypatch.setattr("coat_sim.project_activation.find_shared_project_owner", discover_owner)
    monkeypatch.setattr("coat_sim.project_activation.activate_shared_project", activate)

    result = server.project_open(str(store.project_file))

    assert result["opened"] is True
    assert result["project_target"] == target
    assert [item[0] for item in events] == ["open", "discover", "activate"]
    assert events[0][1] == str(store.project_file)
    assert isinstance(events[0][2], AssistantProcessClient)


def test_project_open_rejects_unauthorized_path_before_starting_runtime(tmp_path, monkeypatch):
    from coat_sim.ui import project_runtime_client

    authorized_root = tmp_path / "authorized"
    outside_store = ProjectStore.create(tmp_path / "outside" / "Study", "Study")
    monkeypatch.setattr(server, "_project_management_service",
                        lambda: ProjectManagementService(authorized_roots=(authorized_root,)))
    starts = []
    monkeypatch.setattr(project_runtime_client, "open_project_runtime",
                        lambda project_file: starts.append(project_file))

    with pytest.raises(ToolError, match="project_root_not_authorized"):
        server.project_open(str(outside_store.project_file))
    assert starts == []


def test_project_open_never_claims_success_when_runtime_or_owner_confirmation_fails(tmp_path, monkeypatch):
    from coat_sim.ui import project_runtime_client

    authorized_root = tmp_path / "authorized"
    store = ProjectStore.create(authorized_root / "Study", "Study")
    monkeypatch.setattr(server, "_project_management_service",
                        lambda: ProjectManagementService(authorized_roots=(authorized_root,)))

    def fail_open(project_file):
        raise UiControlError("project_start_failed", "startup was not confirmed")

    def fail_open_with_client(project_file, *, client):
        return fail_open(project_file)

    monkeypatch.setattr(project_runtime_client, "open_project_runtime", fail_open_with_client)
    with pytest.raises(ToolError, match="project_start_failed"):
        server.project_open(str(store.project_file))

    monkeypatch.setattr(project_runtime_client, "open_project_runtime",
                        lambda project_file, *, client: store)
    monkeypatch.setattr("coat_sim.project_activation.find_shared_project_owner",
                        lambda shared, selected, own_generation: None)
    with pytest.raises(ToolError):
        server.project_open(str(store.project_file))

    monkeypatch.setattr("coat_sim.project_activation.find_shared_project_owner",
                        lambda shared, selected, own_generation: TARGET)
    monkeypatch.setattr("coat_sim.project_activation.activate_shared_project",
                        lambda shared, target: (_ for _ in ()).throw(
                            UiControlError("activation_unconfirmed", "owner did not confirm")))
    with pytest.raises(ToolError, match="activation_unconfirmed"):
        server.project_open(str(store.project_file))


def test_project_open_import_path_is_qt_free_with_fake_runtime_and_owner(tmp_path):
    source = Path(__file__).resolve().parents[1]
    code = r'''import importlib.abc, json, pathlib, sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("PySide6"):
            raise AssertionError("project_open must remain Qt-free in the MCP host")
sys.meta_path.insert(0, NoQt())
from coat_sim.mcp import server
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_management import ProjectManagementService
from coat_sim.project_activation import activation_target
from coat_sim.ui import project_runtime_client
import coat_sim.project_activation as activation
root = pathlib.Path(sys.argv[1])
store = ProjectStore.create(root / "Study", "Study")
server._project_management_service = lambda: ProjectManagementService(authorized_roots=(root,))
project = {"project_id": store.manifest["id"], "generation": "session-generation-1",
           "root": str(store.root), "project_file": str(store.project_file)}
client = {"client_id": "client-owner", "generation": "client-generation-1"}
target = activation_target(client, project)
project_runtime_client.open_project_runtime = lambda project_file, client=None: store
activation.find_shared_project_owner = lambda shared, selected, own_generation: target
activation.activate_shared_project = lambda shared, selected: {"activated": True,
    "instance_id": "window-1", "project_id": target["project_id"],
    "session_generation": target["session_generation"], "client_generation": target["client_generation"]}
result = server.project_open(str(store.project_file))
assert result["opened"] is True and result["project_target"] == target
print(json.dumps({"ok": True, "qt_loaded": any(name.startswith("PySide6") for name in sys.modules)}))
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "authorized")], cwd=source,
        env={**os.environ, "PYTHONPATH": str(source)}, capture_output=True, text=True,
        timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '{"ok": true, "qt_loaded": false}'



def test_shared_control_error_preserves_historical_import_identity_and_payload():
    from coat_sim.control_errors import UiControlError as SharedControlError
    assert SharedControlError is UiControlError
    error = SharedControlError("stale_project", "original message")
    assert isinstance(error, ValueError)
    assert error.code == "stale_project"
    assert str(error) == "original message"
    assert error.args == ("original message",)


def test_snapshot_registry_forwards_only_bounded_fields_to_exact_owner(registered_project_owner, monkeypatch):
    registry, _client, _store, project, target, calls, _state, _ids = registered_project_owner
    original = registry._call
    def call(client, operation, **arguments):
        if operation == "lifecycle_status":
            return original(client, operation, **arguments)
        calls.append((operation, arguments))
        return {"available": True, "snapshot_id": "a" * 64,
                "project_id": project["project_id"], "session_generation": project["generation"]}
    monkeypatch.setattr(registry, "_call", call)
    payload = {"window_type": "film_design", "detail": "layers", "offset": 7,
               "limit": 3, "expected_snapshot_id": "a" * 64}
    result = registry.project_window(target, "project_editor_snapshot", payload)
    forwarded = calls[-1]
    assert forwarded[0] == "project_editor_snapshot"
    assert forwarded[1] == {"project_id": project["project_id"],
        "session_generation": project["generation"], "client_generation": "client-generation-1",
        "project_root": project["root"], "project_file": project["project_file"],
        "module_key": "film_design", **payload}
    assert result["project_target"] == target
    assert calls[0][0] == "lifecycle_status"
    for bad in (target | {"session_generation": "old"}, target | {"client_generation": "old"},
                target | {"root": "/wrong"}):
        before = sum(operation == "project_editor_snapshot" for operation, _args in calls)
        with pytest.raises(UiControlError):
            registry.project_window(bad, "project_editor_snapshot", payload)
        assert sum(operation == "project_editor_snapshot" for operation, _args in calls) == before


def test_snapshot_registry_rejects_wrong_response_generation(registered_project_owner, monkeypatch):
    registry, _client, _store, project, target, _calls, _state, _ids = registered_project_owner
    original = registry._call
    def call(client, operation, **arguments):
        if operation == "lifecycle_status":
            return original(client, operation, **arguments)
        return {"available": True, "project_id": project["project_id"], "session_generation": "old"}
    monkeypatch.setattr(registry, "_call", call)
    with pytest.raises(UiControlError) as error:
        registry.project_window(target, "project_editor_snapshot", {"window_type": "film_design"})
    assert error.value.code == "window_unconfirmed"


@pytest.mark.parametrize("operation,payload", [
    ("update_window", {"fields": {"name": "Changed"}}),
    ("window_action", {"action": "save", "arguments": {}}),
])
def test_guarded_bound_edits_reach_same_exact_owner_and_validate_response(
        registered_project_owner, monkeypatch, operation, payload):
    registry, _client, _store, project, target, calls, _state, _ids = registered_project_owner
    original = registry._call
    def call(client, requested, **arguments):
        if requested == "lifecycle_status":
            return original(client, requested, **arguments)
        calls.append((requested, arguments))
        return {"accepted": True, "project_id": project["project_id"],
                "session_generation": project["generation"]}
    monkeypatch.setattr(registry, "_call", call)
    payload = {"window_type": "film_design", "instance_id": "current-window",
               "expected_snapshot_id": "a" * 64, **payload}
    result = registry.project_window(target, operation, payload)
    assert result["accepted"] and result["project_target"] == target
    assert calls[-1][0] == operation
    assert calls[-1][1]["instance_id"] == "current-window"
    assert calls[-1][1]["expected_snapshot_id"] == "a" * 64
    extra: Mapping[str, object]
    for extra in ({"force": True}, {"force": False}, {"data": {}}, {"design_path": "/other"}):
        count = len(calls)
        with pytest.raises(UiControlError) as error:
            registry.project_window(target, operation, payload | extra)
        assert error.value.code == "invalid_arguments"
        assert len(calls) == count
