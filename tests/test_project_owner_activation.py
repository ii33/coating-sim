"""Authenticated navigation to an existing project owner; never acquire ownership twice."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil

import pytest

from coat_sim import assistant_process as assistant_process_module
from coat_sim.assistant_process import (
    ASSISTANT_OPERATIONS,
    ApplicationClientRegistry,
    _Client,
)
from coat_sim.core.project_store import ProjectError, ProjectStore
from coat_sim.project_activation import (
    ACTIVATION_FIELDS,
    activate_shared_project,
    activation_target,
    find_shared_project_owner,
    remote_project_entries,
)
from coat_sim.project_recovery import ProjectOwnerLease
from coat_sim.ui.control import UiControlError


pytestmark = pytest.mark.contract


class FakeAuthenticatedOwnerTransport:
    """Deterministic fake wire endpoint, while retaining real registry identity checks."""

    def __init__(self, client: _Client, calls: list[tuple[str, dict]], state: dict):
        self.client = client
        self.calls = calls
        self.state = state

    def __call__(self, *_args, **_kwargs):
        owner = self

        class FakeUiControlClient:
            def __init__(self, *_client_args, **_client_kwargs):
                pass

            def request(self, operation: str, **arguments):
                owner.calls.append((operation, dict(arguments)))
                if operation == "lifecycle_status":
                    if owner.state.get("disconnected"):
                        raise UiControlError("ui_unavailable", "共享助手连接不可用。")
                    return {
                        "application": "coat_sim",
                        "nonce": arguments.get("nonce"),
                        "pid": owner.client.endpoint["pid"],
                        "process_start": owner.client.process_start,
                        "generation": owner.state.get("reported_client_generation", owner.client.generation),
                        "role": owner.client.role,
                        "project_sessions": deepcopy(owner.state.get("projects", owner.client.projects)),
                    }
                if operation == "open_window":
                    if owner.state.get("open_error") is not None:
                        raise owner.state["open_error"]
                    if owner.state.get("old_assistant"):
                        raise UiControlError("invalid_operation", "此版本共享助手不支持激活工程窗口。")
                    result = {
                        "instance_id": owner.state.get("instance_id", "film-window-new"),
                        "project_id": arguments.get("project_id"),
                        "generation": arguments.get("session_generation"),
                    }
                    if owner.state.get("wrong_open_response"):
                        result["generation"] = "wrong-session-generation"
                    return result
                if operation == "read_window":
                    if owner.state.get("read_window_disconnected"):
                        raise UiControlError("ui_unavailable", "窗口读取暂不可用。")
                    return {
                        "open": owner.state.get("window_open", True),
                        "visible": owner.state.get("window_visible", True),
                        "instance_id": owner.state.get("reported_instance_id", arguments.get("instance_id")),
                        "project_id": owner.state.get("reported_project_id", arguments.get("project_id")),
                        "session_generation": owner.state.get(
                            "reported_session_generation", arguments.get("session_generation")),
                    }
                raise AssertionError(f"Unexpected owner operation: {operation}")

        return FakeUiControlClient()


@pytest.fixture
def project_owner(tmp_path, monkeypatch):
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
    calls: list[tuple[str, dict]] = []
    state = {"projects": [dict(project)]}
    process_identities = {client.endpoint["pid"]: ("alive", client.process_start)}
    monkeypatch.setattr(assistant_process_module, "_process_identity",
                        lambda pid: process_identities.get(pid, ("dead", None)))
    monkeypatch.setattr(assistant_process_module, "UiControlClient",
                        FakeAuthenticatedOwnerTransport(client, calls, state))
    target = activation_target(client.public(), project)
    return registry, client, store, project, target, calls, state, process_identities


def _assert_no_owner_open(calls):
    assert not any(operation == "open_window" for operation, _arguments in calls)


def _assert_original_lease_still_excludes_a_second_owner(store):
    with pytest.raises(ProjectError, match="active writable owner"):
        ProjectOwnerLease.acquire(store.project_file, store.manifest["id"])


def test_registry_activates_retained_session_through_exact_owner_without_second_lease(
        project_owner, monkeypatch):
    registry, client, store, project, target, calls, _state, _process_identities = project_owner
    original = store.project_file.read_bytes()
    acquired = []
    original_acquire = ProjectOwnerLease.acquire

    def track_acquire(*args, **kwargs):
        acquired.append((args, kwargs))
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(ProjectOwnerLease, "acquire", track_acquire)
    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"])
    try:
        acquired.clear()  # Ignore the single lease deliberately held by the existing owner.
        result = registry.activate_project(target)

        assert result == {
            "activated": True,
            "instance_id": "film-window-new",
            "project_id": project["project_id"],
            "session_generation": project["generation"],
            "client_generation": client.generation,
        }
        assert [operation for operation, _arguments in calls].count("open_window") == 1
        assert calls[-2:] == [("open_window", {
            "project_id": project["project_id"],
            "session_generation": project["generation"],
            "module_key": "film_design",
        }), ("read_window", {
            "project_id": project["project_id"],
            "session_generation": project["generation"],
            "module_key": "film_design",
            "instance_id": "film-window-new",
        })]
        assert not any(operation == "list_windows" for operation, _arguments in calls)
        assert len(registry._clients) == 1
        assert registry._clients[client.client_id] is client
        assert client.projects == [project]
        assert acquired == []
        assert store.project_file.read_bytes() == original
        # The original writable owner remains exclusive after navigation.
        _assert_original_lease_still_excludes_a_second_owner(store)
    finally:
        lease.close()


@pytest.mark.parametrize("bad_target", [
    lambda target: {key: value for key, value in target.items() if key != "root"},
    lambda target: target | {"extra": "not-allowed"},
    lambda target: target | {"root": "  "},
    lambda target: target | {"project_file": None},
    lambda target: target | {"client_generation": 7},
])
def test_registry_requires_exactly_six_nonempty_string_arguments(project_owner, bad_target):
    registry, _client, _store, _project, target, calls, _state, _identities = project_owner
    with pytest.raises(UiControlError) as error:
        registry.activate_project(bad_target(target))
    assert error.value.code == "invalid_arguments"
    _assert_no_owner_open(calls)


def test_registry_rejects_stale_client_and_session_generations(project_owner):
    registry, client, _store, project, target, calls, state, _identities = project_owner
    with pytest.raises(UiControlError) as stale_client:
        registry.activate_project(target | {"client_generation": "old-client-generation"})
    assert stale_client.value.code == "stale_client"
    _assert_no_owner_open(calls)

    state["projects"] = [project | {"generation": "session-generation-2"}]
    with pytest.raises(UiControlError) as stale_session:
        registry.activate_project(target)
    assert stale_session.value.code == "stale_project"
    _assert_no_owner_open(calls)
    assert registry._clients[client.client_id] is client


def test_registry_rechecks_os_pid_creation_identity_before_activation(project_owner):
    registry, _client, store, _project, target, calls, state, process_identities = project_owner
    before = store.project_file.read_bytes()
    state["draft"] = {"dirty": True, "name": "Unsaved local draft"}
    process_identities[8101] = ("alive", "creation-8101-reused")

    with pytest.raises(UiControlError) as error:
        registry.activate_project(target)

    assert error.value.code == "client_exited"
    assert "原工程进程" in str(error.value) or "核验" in str(error.value)
    _assert_no_owner_open(calls)
    assert state["draft"] == {"dirty": True, "name": "Unsaved local draft"}
    assert store.project_file.read_bytes() == before


def test_registry_rejects_changed_client_generation_from_fresh_status_challenge(project_owner):
    registry, store_client, store, _project, target, calls, state, _identities = project_owner
    state["reported_client_generation"] = "replacement-client-generation"

    with pytest.raises(UiControlError) as error:
        registry.activate_project(target)

    assert error.value.code == "process_identity_mismatch"
    assert "原工程进程" in str(error.value) or "核验" in str(error.value)
    _assert_no_owner_open(calls)
    assert registry._clients[store_client.client_id] is store_client
    assert store.project_file.is_file()


@pytest.mark.parametrize("identity_change", ["different-root", "different-descriptor"])
def test_registry_rejects_same_id_with_different_registered_root_or_descriptor(project_owner, identity_change):
    registry, _client, store, _project, target, calls, _state, _identities = project_owner
    if identity_change == "different-root":
        target = target | {"root": str(store.root.parent / "copied-root")}
    else:
        target = target | {"project_file": str(store.root / "different.coatproj")}

    with pytest.raises(UiControlError) as error:
        registry.activate_project(target)

    assert error.value.code == "project_identity_mismatch"
    _assert_no_owner_open(calls)


def test_registry_rejects_exit_in_progress_without_changing_owner_or_draft(project_owner):
    registry, client, store, project, target, calls, state, _identities = project_owner
    state["draft"] = {"dirty": True, "name": "Unsaved"}
    before = store.project_file.read_bytes()
    registry._exiting = True

    with pytest.raises(UiControlError) as error:
        registry.activate_project(target)

    assert error.value.code == "exit_in_progress"
    assert "正在退出" in str(error.value)
    _assert_no_owner_open(calls)
    assert registry._clients[client.client_id] is client
    assert client.projects == [project]
    assert state["draft"] == {"dirty": True, "name": "Unsaved"}
    assert store.project_file.read_bytes() == before


def test_registry_reports_disconnected_owner_in_chinese_and_preserves_draft(project_owner):
    registry, client, store, project, target, calls, state, _identities = project_owner
    state["disconnected"] = True
    state["draft"] = {"dirty": True, "name": "Unsaved"}
    before = store.project_file.read_bytes()
    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"])

    try:
        with pytest.raises(UiControlError) as error:
            registry.activate_project(target)

        assert error.value.code == "ui_unavailable"
        assert "原工程进程" in str(error.value) or "当前内容仍保留" in str(error.value)
        _assert_no_owner_open(calls)
        assert registry._clients[client.client_id] is client
        assert client.projects == [project]
        assert state["draft"] == {"dirty": True, "name": "Unsaved"}
        assert store.project_file.read_bytes() == before
        _assert_original_lease_still_excludes_a_second_owner(store)
    finally:
        lease.close()


def test_old_registry_owner_rejects_activation_without_replacing_lock_or_draft(project_owner):
    registry, client, store, project, target, calls, state, _identities = project_owner
    state["old_assistant"] = True
    state["draft"] = {"dirty": True, "name": "Unsaved"}
    before = store.project_file.read_bytes()
    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"])

    try:
        with pytest.raises(UiControlError) as error:
            registry.activate_project(target)

        assert error.value.code == "invalid_operation"
        assert "原工程窗口未能激活" in str(error.value)
        assert "当前内容仍保留" in str(error.value)
        assert calls[-1] == ("open_window", {
            "project_id": project["project_id"],
            "session_generation": project["generation"],
            "module_key": "film_design",
        })
        assert registry._clients[client.client_id] is client
        assert client.projects == [project]
        assert state["draft"] == {"dirty": True, "name": "Unsaved"}
        assert store.project_file.read_bytes() == before
        _assert_original_lease_still_excludes_a_second_owner(store)
    finally:
        lease.close()


@pytest.mark.parametrize("code, retryable", [
    ("project_starting", True),
    ("project_identity_mismatch", False),
    ("process_identity_mismatch", False),
    ("permission_denied", False),
])
def test_registry_preserves_project_starting_hint_without_marking_denials_transient(
        project_owner, code, retryable):
    registry, client, store, project, target, calls, state, _identities = project_owner
    original = UiControlError(code, "owner refused this operation")
    state["open_error"] = original
    state["draft"] = {"dirty": True, "name": "Keep this draft"}
    before = store.project_file.read_bytes()

    with pytest.raises(UiControlError) as error:
        registry.activate_project(target)

    assert error.value.code == code and error.value.__cause__ is original
    assert ("请稍后重试" in str(error.value)) is retryable
    assert "当前内容仍保留" in str(error.value)
    assert sum(operation == "open_window" for operation, _arguments in calls) == 1
    assert calls[-1] == ("open_window", {
        "project_id": project["project_id"],
        "session_generation": project["generation"], "module_key": "film_design"})
    assert not any(operation == "read_window" for operation, _arguments in calls)
    assert registry._clients[client.client_id] is client and client.projects == [project]
    assert state["draft"] == {"dirty": True, "name": "Keep this draft"}
    assert store.project_file.read_bytes() == before


def test_registry_rejects_unconfirmed_owner_response(project_owner):
    registry, _client, store, _project, target, calls, state, _identities = project_owner
    state["wrong_open_response"] = True
    before = store.project_file.read_bytes()

    with pytest.raises(UiControlError) as error:
        registry.activate_project(target)

    assert error.value.code == "activation_unconfirmed"
    assert "尚未确认激活" in str(error.value)
    assert calls[-1] == ("open_window", {
        "project_id": target["project_id"],
        "session_generation": target["session_generation"],
        "module_key": "film_design",
    })
    assert store.project_file.read_bytes() == before


def test_registry_rejects_whitespace_instance_id_before_reading_window(project_owner):
    registry, _client, store, _project, target, calls, state, _identities = project_owner
    state["instance_id"] = "   \t"
    before = store.project_file.read_bytes()

    with pytest.raises(UiControlError) as error:
        registry.activate_project(target)

    assert error.value.code == "activation_unconfirmed"
    assert calls[-1][0] == "open_window"
    assert not any(operation == "read_window" for operation, _arguments in calls)
    assert store.project_file.read_bytes() == before


@pytest.mark.parametrize("window_state", ["pending", "invisible"])
def test_registry_requires_open_visible_window_confirmation(project_owner, window_state):
    registry, _client, store, _project, target, calls, state, _identities = project_owner
    if window_state == "pending":
        state["window_open"] = False
    else:
        state["window_visible"] = False
    before = store.project_file.read_bytes()

    with pytest.raises(UiControlError) as error:
        registry.activate_project(target)

    assert error.value.code == "activation_unconfirmed"
    assert "尚未确认可见" in str(error.value)
    assert [operation for operation, _arguments in calls][-2:] == ["open_window", "read_window"]
    assert store.project_file.read_bytes() == before


@pytest.mark.parametrize("failure", [
    ProjectError("integrity_error", "Project document digest does not match: source-id"),
    ProjectError("invalid_manifest", "Document path must be a typed named project revision"),
    ProjectError("unsafe_path", "Project paths cannot use links or reparse points: project-root"),
    OSError(13, "Permission denied", "project.coatproj"),
])
@pytest.mark.parametrize("route", ["activation", "window"])
def test_activation_validation_diagnostic_survives_ipc_without_weakening_rejection(
        project_owner, monkeypatch, failure, route):
    registry, client, store, project, target, calls, state, _identities = project_owner
    state["draft"] = {"dirty": True, "name": "Keep unsaved work"}
    before = store.project_file.read_bytes()

    def fail_open(*_args, **_kwargs):
        raise failure

    class SerializedAssistant:
        def request(self, operation, **arguments):
            try:
                if operation == "activate_project":
                    return registry.activate_project(arguments)
                assert operation == "project_window"
                return registry.project_window(arguments["target"], "list_windows", {})
            except UiControlError as error:
                # The wire transports code/message, not the local exception chain.
                raise UiControlError(error.code, str(error)) from None

    monkeypatch.setattr(ProjectStore, "open", fail_open)
    with pytest.raises(UiControlError) as caught:
        if route == "activation":
            activate_shared_project(SerializedAssistant(), target)
        else:
            SerializedAssistant().request("project_window", target=target)

    assert caught.value.code == "project_identity_mismatch"
    assert str(failure) in str(caught.value)
    assert getattr(failure, "code", type(failure).__name__) in str(caught.value)
    assert ("未激活其他工程" if route == "activation" else "未操作其他工程") in str(caught.value)
    _assert_no_owner_open(calls)
    assert registry._clients[client.client_id] is client
    assert client.projects == [project]
    assert state["draft"] == {"dirty": True, "name": "Keep unsaved work"}
    assert store.project_file.read_bytes() == before


def test_changed_default_dependency_reports_real_failure_and_retains_exact_owner(
        project_owner, tmp_path):
    registry, client, _empty_store, project, _target, calls, state, _identities = project_owner
    example = Path(__file__).resolve().parents[1] / "example" / "synthetic-demo"
    root = tmp_path / "copied synthetic demo 工程"
    shutil.copytree(example, root)
    store = ProjectStore.open(root / "project.coatproj")
    project.update(project_id=store.manifest["id"], root=str(store.root),
                   project_file=str(store.project_file), name=store.manifest["name"])
    client.projects = [dict(project)]
    state["projects"] = [dict(project)]
    state["draft"] = {"dirty": True, "name": "Retained in owner"}
    target = activation_target(client.public(), project)
    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"])
    try:
        assert registry.activate_project(target)["activated"] is True
        source = next(row for row in store.manifest["documents"] if row["kind"] == "source")
        source_path = store.root / source["path"]
        original_source = source_path.read_bytes()
        source_path.write_bytes(b"{}\n")
        before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        calls.clear()

        with pytest.raises(UiControlError) as caught:
            registry.activate_project(target)

        assert caught.value.code == "project_identity_mismatch"
        assert "integrity_error" in str(caught.value)
        assert source["id"] in str(caught.value)
        _assert_no_owner_open(calls)
        assert registry._clients[client.client_id] is client
        assert state["draft"] == {"dirty": True, "name": "Retained in owner"}
        assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
        # Repair only the test's deliberate corruption before testing the lease;
        # admission otherwise stops earlier at the unchanged integrity boundary.
        source_path.write_bytes(original_source)
        _assert_original_lease_still_excludes_a_second_owner(store)
    finally:
        lease.close()


def test_project_validation_diagnostic_preserves_nested_os_error_and_bounds_cycles():
    failure = ProjectError("io_error", "Cannot read project document: project.coatproj")
    denied = PermissionError(13, "Permission denied", "project.coatproj")
    failure.__cause__ = denied
    detail = assistant_process_module._project_validation_detail(failure)
    assert "io_error" in detail and str(failure) in detail
    assert "PermissionError" in detail and str(denied) in detail
    denied.__cause__ = failure
    assert assistant_process_module._project_validation_detail(failure) == detail


def test_shared_owner_helpers_use_authenticated_listing_and_exact_activation_target(project_owner):
    _registry, client, store, project, target, _calls, _state, _identities = project_owner

    class FakeAssistantClient:
        def __init__(self):
            self.list_calls = 0

        def list_clients(self):
            self.list_calls += 1
            return [client.public()]

        def request(self, operation, **arguments):
            assert operation == "activate_project"
            assert arguments == target
            return {
                "activated": True,
                "instance_id": "film-window-new",
                "project_id": project["project_id"],
                "session_generation": project["generation"],
                "client_generation": client.generation,
            }

    assistant = FakeAssistantClient()
    assert find_shared_project_owner(assistant, store, own_generation="another-client-generation") == target
    assert assistant.list_calls == 1
    assert set(target) == ACTIVATION_FIELDS
    assert activate_shared_project(assistant, target)["activated"] is True
    assert assistant.list_calls == 1


@pytest.mark.parametrize(("change", "expected_code"), [
    ("different-id", "project_identity_conflict"),
    ("different-root", "project_identity_conflict"),
    ("different-descriptor", "project_descriptor_conflict"),
])
def test_owner_discovery_rejects_project_id_root_and_descriptor_aliases(
        project_owner, change, expected_code):
    _registry, client, store, project, _target, _calls, _state, _identities = project_owner
    row = dict(project)
    if change == "different-id":
        row["project_id"] = "copied-project-id"
    elif change == "different-root":
        row["root"] = str(store.root.parent / "other-root")
    else:
        row["project_file"] = str(store.root / "other.coatproj")

    class FakeAssistantClient:
        def list_clients(self):
            return [{**client.public(), "project_sessions": [row]}]

    before = store.project_file.read_bytes()
    with pytest.raises(UiControlError) as error:
        find_shared_project_owner(FakeAssistantClient(), store, own_generation="new-launcher")
    assert error.value.code == expected_code
    assert store.project_file.read_bytes() == before


def test_owner_discovery_rejects_unavailable_owner_and_wraps_listing_failure(project_owner):
    _registry, client, store, _project, _target, _calls, _state, _identities = project_owner

    class UnavailableAssistant:
        def list_clients(self):
            return [{**client.public("unavailable")}]

    with pytest.raises(UiControlError) as unavailable:
        find_shared_project_owner(UnavailableAssistant(), store, own_generation="new-launcher")
    assert unavailable.value.code == "project_owner_unavailable"
    assert "无法联系原窗口" in str(unavailable.value)

    class DisconnectedAssistant:
        def list_clients(self):
            raise UiControlError("assistant_unavailable", "not running")

    with pytest.raises(UiControlError) as discovery:
        find_shared_project_owner(DisconnectedAssistant(), store, own_generation="new-launcher")
    assert discovery.value.code == "project_discovery_unavailable"
    assert "未改变工程当前内容" in str(discovery.value)


def test_remote_project_rows_are_detached_read_only_and_skip_this_launcher(project_owner):
    _registry, client, _store, project, target, _calls, _state, _identities = project_owner
    own = client.public() | {"generation": "this-launcher-generation"}
    remote = client.public() | {"state": "unavailable"}
    rows = remote_project_entries([own, remote], own_generation="this-launcher-generation")

    assert len(rows) == 1
    assert rows[0] == {
        "projectId": project["project_id"],
        "name": project["name"],
        "root": project["root"],
        "descriptorFile": project["project_file"],
        "active": False,
        "defaultState": "remote_session",
        "defaultFilmSnapshot": False,
        "defaultFilmDraft": False,
        "remote": True,
        "available": False,
        "sessionGeneration": project["generation"],
        "activationTarget": target,
    }
    rows[0]["activationTarget"]["client_generation"] = "tampered-copy"
    assert client.generation == target["client_generation"]
    assert "open_window" not in ASSISTANT_OPERATIONS
    assert "activate_project" in ASSISTANT_OPERATIONS


def test_shared_activation_translates_legacy_assistant_and_checks_response(project_owner):
    _registry, client, store, project, target, _calls, _state, _identities = project_owner

    class OldAssistant:
        def request(self, operation, **arguments):
            assert operation == "activate_project"
            assert arguments == target
            raise UiControlError("invalid_operation", "Unknown operation")

    lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"])
    try:
        with pytest.raises(UiControlError) as unsupported:
            activate_shared_project(OldAssistant(), target)
        assert unsupported.value.code == "activation_unsupported"
        assert "不支持工程会话激活" in str(unsupported.value)
        _assert_original_lease_still_excludes_a_second_owner(store)
    finally:
        lease.close()

    class StaleReplyAssistant:
        def request(self, _operation, **_arguments):
            return {
                "activated": True,
                "instance_id": "window",
                "project_id": project["project_id"],
                "session_generation": "expired-session",
                "client_generation": client.generation,
            }

    with pytest.raises(UiControlError) as unconfirmed:
        activate_shared_project(StaleReplyAssistant(), target)
    assert unconfirmed.value.code == "activation_unconfirmed"
    assert "未创建第二个可写会话" in str(unconfirmed.value)

