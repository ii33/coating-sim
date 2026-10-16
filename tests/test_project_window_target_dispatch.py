"""Bound navigation exposes only real window status and preserves owner Close."""
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication, QWindow

from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionHost
from coat_sim.ui.control import UiControlError
from coat_sim.ui.project_application_client import ProjectApplicationClient

pytestmark = [pytest.mark.contract, pytest.mark.qt]


class Bridge(QObject):
    project_runtime: Any
    project_lifecycle: Any = None
    launchError = Signal(str)


@pytest.fixture
def bound_client(tmp_path):
    QGuiApplication.instance() or QGuiApplication([])
    stores = [ProjectStore.create(tmp_path / name, name) for name in ("A", "B")]
    descriptors = {store.root: store.project_file for store in stores}
    host = ProjectSessionHost(lambda root: ProjectStore.open(descriptors[root]))
    sessions = [host.open_project(store.root) for store in stores]
    windows = [QWindow(), QWindow()]
    for session, window in zip(sessions, windows, strict=True):
        route = host.reserve_module(session.binding.project_id, "film_design").route
        host.attach_window(route, window)
    bridge = Bridge()
    bridge.project_runtime = SimpleNamespace(host=host)
    value = ProjectApplicationClient(bridge, SimpleNamespace(
        dispatch=lambda *_args: pytest.fail("A bound request reached the unbound registry")))
    store, session = stores[0], sessions[0]
    args = {"project_id": session.binding.project_id, "session_generation": session.binding.generation,
            "client_generation": value.generation, "project_root": str(store.root),
            "project_file": str(store.project_file)}
    yield value, args, sessions, windows
    value.stop()
    for window in windows:
        window.close()
    for session in sessions:
        host.close_project(session.binding, prepare_close=lambda _binding, _routes: True)


def test_bound_list_and_read_are_scoped_window_status_without_editor_claims(bound_client):
    client, args, sessions, _windows = bound_client
    listed = client.dispatch("list_windows", args)
    assert len(listed["windows"]) == 1
    assert listed["windows"][0]["project_id"] == sessions[0].binding.project_id
    assert listed["capabilities"]["editor_snapshot"] is False
    assert "update_window" not in listed["capabilities"]["operations"]
    read = client.dispatch("read_window", args | {"module_key": "film_design"})
    assert read["open"] is True
    assert read["session_generation"] == args["session_generation"]
    assert read["capabilities"]["content_scope"] == "window_status"
    assert "document" not in read and "fields" not in read


def test_closed_bound_window_retains_session_and_explicit_capabilities(bound_client):
    client, args, _sessions, _windows = bound_client
    read = client.dispatch("read_window", args | {"module_key": "mask"})
    assert read["state"] == "closed" and read["open"] is False
    assert read["session_generation"] == args["session_generation"]
    assert read["capabilities"]["force_bypasses_review"] is False


@pytest.mark.parametrize("change,code", [
    ({"client_generation": "old"}, "stale_client"),
    ({"project_root": "/other"}, "project_identity_mismatch"),
    ({"project_file": "/other.coatproj"}, "project_identity_mismatch"),
    ({"module_key": "settings"}, "invalid_window"),
])
def test_owner_rechecks_bound_identity_without_unbound_fallback(bound_client, change, code):
    client, args, _sessions, _windows = bound_client
    with pytest.raises(UiControlError) as exc:
        client.dispatch("read_window", args | {"module_key": "film_design"} | change)
    assert exc.value.code == code


def test_bound_close_force_still_calls_native_owner_close_and_keeps_declined_window(bound_client, monkeypatch):
    client, args, _sessions, windows = bound_client
    calls = []
    def decline_close():
        calls.append("native_close")
        return False
    monkeypatch.setattr(windows[0], "close", decline_close)
    result = client.dispatch("close_window", args | {"module_key": "film_design", "force": True})
    assert calls == ["native_close"]
    assert result["accepted"] is False and result["force_applied"] is False
    assert result["close_review"] == "native_owner"
    assert client.dispatch("read_window", args | {"module_key": "film_design"})["open"] is True


@pytest.mark.parametrize("operation", ["update_window", "window_action"])
def test_bound_editor_operations_do_not_reach_legacy_adapter(bound_client, operation):
    client, args, _sessions, _windows = bound_client
    with pytest.raises(UiControlError) as exc:
        client.dispatch(operation, args | {"module_key": "film_design", "force": True})
    assert exc.value.code in {"requires_snapshot", "invalid_arguments"}


def test_shared_host_has_only_fixed_project_window_dispatch():
    from coat_sim.ui.assistant_process_host import AssistantProcessHost
    calls = []

    def project_window(target, operation, arguments):
        calls.append((target, operation, arguments))
        return {"routed": True}

    host: Any = SimpleNamespace(_worker=None, registry=SimpleNamespace(project_window=project_window))
    payload = {"project_target": {"opaque": "validated by registry"},
               "window_operation": "list_windows", "arguments": {}}
    assert AssistantProcessHost.dispatch(host, "project_window", payload) == {"routed": True}
    assert calls == [(payload["project_target"], "list_windows", {})]
    with pytest.raises(UiControlError) as exc:
        AssistantProcessHost.dispatch(host, "project_window", payload | {"endpoint": "not-accepted"})
    assert exc.value.code == "invalid_arguments" and len(calls) == 1
    host._worker = object()
    with pytest.raises(UiControlError) as exc:
        AssistantProcessHost.dispatch(host, "project_window", payload)
    assert exc.value.code == "exit_in_progress" and len(calls) == 1
