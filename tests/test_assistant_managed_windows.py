"""Global assistant windows stay bound to exact project/process generations."""
from __future__ import annotations

import pytest

from coat_sim.assistant_process import ApplicationClientRegistry, _Client
from coat_sim.core.project_store import ProjectError, ProjectStore
from coat_sim.ui.control import UiControlError

pytestmark = pytest.mark.contract


def _registry(monkeypatch):
    registry = ApplicationClientRegistry()
    client = _Client("client-one", {"pid": 901, "root": "/runtime", "role": "launcher"},
                     "owner-generation", "creation-901", "launcher",
                     [{"project_id": "project-a", "generation": "session-a", "root": "/project-a",
                       "project_file": "/project-a/First.coatproj", "name": "A"},
                      {"project_id": "project-b", "generation": "session-b", "root": "/project-b",
                       "project_file": "/project-b/Second.coatproj", "name": "B"}])
    registry._clients[client.client_id] = client
    monkeypatch.setattr(registry, "list_clients", lambda: [client.public()])
    monkeypatch.setattr(ApplicationClientRegistry, "_status", classmethod(
        lambda _cls, _client: {"project_sessions": list(client.projects)}))
    calls = []

    def call(_client, operation, **arguments):
        calls.append((operation, arguments))
        if operation == "list_windows":
            return {"windows": [
                {"project_id": "project-a", "session_generation": "session-a", "module_key": "film_design",
                 "instance_id": "same-instance", "title": "A / Film", "visible": True},
                {"project_id": "project-b", "session_generation": "session-b", "module_key": "film_design",
                 "instance_id": "same-instance", "title": "B / Film", "visible": True},
                {"project_id": "project-a", "session_generation": "old-session", "module_key": "film_design",
                 "instance_id": "stale", "title": "Stale", "visible": True},
                {"module_key": "launcher", "instance_id": "hidden", "title": "Launcher", "open": True, "visible": False},
                {"module_key": "about", "instance_id": "closed", "title": "Closed", "open": False, "visible": True},
                {"module_key": "about", "instance_id": "unverified", "title": "Unverified", "visible": False},
            ]}
        return {"accepted": True}

    monkeypatch.setattr(ApplicationClientRegistry, "_call", staticmethod(call))
    return registry, client, calls


def test_remote_window_actions_keep_project_and_instance_binding(monkeypatch):
    registry, _client, calls = _registry(monkeypatch)
    windows = registry.list_windows()
    assert [row["title"] for row in windows] == ["A / Film", "B / Film", "Launcher"]
    assert windows[0]["id"] != windows[1]["id"]
    assert registry.window_action(windows[0]["id"], "close_window") == {"accepted": True}
    assert calls[-1] == ("close_window", {
        "instance_id": "same-instance", "module_key": "film_design", "window_type": "film_design",
        "project_id": "project-a", "session_generation": "session-a"})


def test_hidden_open_launcher_can_activate_its_exact_instance(monkeypatch):
    registry, _client, calls = _registry(monkeypatch)
    launcher = next(row for row in registry.list_windows() if row["module_key"] == "launcher")
    assert launcher["instance_id"] == "hidden"
    assert registry.window_action(launcher["id"], "open_window") == {"accepted": True}
    assert calls[-1] == ("open_window", {
        "instance_id": "hidden", "module_key": "launcher", "window_type": "launcher"})


def test_remote_window_late_actions_reject_reopened_project_or_client(monkeypatch):
    registry, client, calls = _registry(monkeypatch)
    identity = registry.list_windows()[0]["id"]
    client.projects[0] = {**client.projects[0], "generation": "reopened-session"}
    with pytest.raises(UiControlError) as stale:
        registry.window_action(identity, "open_window")
    assert stale.value.code == "stale_project"
    assert not any(operation == "open_window" for operation, _args in calls)
    registry._clients[client.client_id] = _Client(client.client_id, client.endpoint,
        "replacement-generation", client.process_start, client.role, client.projects)
    with pytest.raises(UiControlError) as replaced:
        registry.window_action(identity, "open_window")
    assert replaced.value.code == "stale_client"


def test_exit_gate_rejects_remote_window_action_admission(monkeypatch):
    registry, _client, _calls = _registry(monkeypatch)
    identity = registry.list_windows()[0]["id"]
    registry._exiting = True
    with pytest.raises(UiControlError) as exiting:
        registry.window_action(identity, "close_window")
    assert exiting.value.code == "exit_in_progress"


@pytest.fixture
def management_projects(tmp_path, monkeypatch):
    registry, client, calls = _registry(monkeypatch)
    stores = []
    for index, name in enumerate(("First", "Second")):
        store = ProjectStore.create(tmp_path / name, name)
        descriptor = store.root / (name + ".coatproj")
        store.project_file.rename(descriptor)
        store = ProjectStore.open(descriptor)
        client.projects[index] = {**client.projects[index], "project_id": store.manifest["id"],
                                  "root": str(store.root), "project_file": str(store.project_file)}
        stores.append(store)
    return registry, client, calls, stores


def test_management_routes_only_to_explicit_active_session_generation(management_projects):
    registry, client, calls, (first, second) = management_projects
    arguments = {"root": str(first.project_file), "project_id": first.manifest["id"], "operation_id": "bound-save",
                 "expected_revision": 1, "configuration_id": None}
    with pytest.raises(UiControlError) as missing:
        registry.manage_project("set_default", arguments)
    assert missing.value.code == "stale_project"
    result = registry.manage_project("set_default", arguments, session_generation="session-a")
    assert result == {"accepted": True}
    assert calls[-1] == ("project_manage", {"project_operation": "set_default", "arguments": arguments,
                                           "session_generation": "session-a", "client_generation": client.generation})
    with pytest.raises(UiControlError) as wrong_root:
        registry.manage_project("set_default", {**arguments, "root": str(second.project_file)}, session_generation="session-a")
    assert wrong_root.value.code == "project_identity_mismatch"


@pytest.mark.parametrize("entry", ["directory", "legacy-json", "package"])
def test_management_rejects_old_entry_paths_before_active_owner_dispatch(management_projects, entry):
    registry, _client, calls, (first, _second) = management_projects
    if entry == "directory":
        target = first.root
    else:
        target = first.root / ("project.json" if entry == "legacy-json" else "project.cproject")
        target.write_bytes(first.project_file.read_bytes())
    with pytest.raises(ProjectError) as error:
        registry.manage_project("inspect", {"root": str(target), "project_id": first.manifest["id"]},
                                session_generation="session-a")
    assert error.value.code == "invalid_project_file"
    assert calls == []


def test_management_same_directory_descriptor_cannot_alias_registered_file(management_projects):
    registry, _client, calls, (first, _second) = management_projects
    alias = first.root / "Another.coatproj"
    alias.write_bytes(first.project_file.read_bytes())
    original = first.project_file.read_bytes()
    with pytest.raises(UiControlError) as error:
        registry.manage_project("inspect", {"root": str(alias), "project_id": first.manifest["id"]},
                                session_generation="session-a")
    assert error.value.code == "project_identity_mismatch"
    assert calls == []
    assert first.project_file.read_bytes() == alias.read_bytes() == original


def test_management_refresh_rejects_changed_descriptor_binding(management_projects, monkeypatch):
    registry, client, calls, (first, _second) = management_projects
    monkeypatch.setattr(ApplicationClientRegistry, "_status", classmethod(
        lambda _cls, _client: {"project_sessions": [
            {**client.projects[0], "project_file": str(first.root / "Replacement.coatproj")}
        ]}))
    with pytest.raises(UiControlError) as error:
        registry.manage_project("inspect", {"root": str(first.project_file), "project_id": first.manifest["id"]},
                                session_generation="session-a")
    assert error.value.code == "stale_project"
    assert calls == []


def test_foreground_handoff_only_targets_verified_registered_process(monkeypatch):
    import coat_sim.assistant_process as module
    registry, client, calls = _registry(monkeypatch)
    identity = registry.list_windows()[0]['id']
    monkeypatch.setattr(module, '_process_identity', lambda pid: ('alive', client.process_start))
    grants = []
    def grant_foreground(pid):
        grants.append(pid)
        return True

    monkeypatch.setattr(module, '_allow_foreground_process', grant_foreground)
    assert registry.prepare_window_activation(identity)
    assert grants == [client.endpoint['pid']]
    assert not any(op == 'open_window' for op, _ in calls)
    monkeypatch.setattr(module, '_process_identity', lambda pid: ('alive', 'reused-pid'))
    with pytest.raises(UiControlError, match='Cannot verify'):
        registry.prepare_window_activation(identity)
    assert len(grants) == 1


def test_foreground_handoff_preserves_os_denial_and_rejects_stale_target(monkeypatch):
    import coat_sim.assistant_process as module
    registry, client, _ = _registry(monkeypatch)
    identity = registry.list_windows()[0]['id']
    monkeypatch.setattr(module, '_process_identity', lambda pid: ('alive', client.process_start))
    monkeypatch.setattr(module, '_allow_foreground_process', lambda pid: False)
    assert registry.prepare_window_activation(identity) is False
    registry._clients.clear()
    with pytest.raises(UiControlError) as stale:
        registry.prepare_window_activation(identity)
    assert stale.value.code == 'stale_client'
    with pytest.raises(UiControlError) as missing:
        registry.prepare_window_activation('unknown-window')
    assert missing.value.code == 'stale_window'
