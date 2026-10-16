"""Authenticated cross-process routing retains exact project identity."""
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
    project_launcher: Any
    project_lifecycle: Any
    launchError = Signal(str)

    def _uncommitted_project_view_draft(self, _project_id=None):
        return False


@pytest.fixture
def client(tmp_path):
    QGuiApplication.instance() or QGuiApplication([])
    first = ProjectStore.create(tmp_path / "A", "Same name")
    second = ProjectStore.create(tmp_path / "B", "Same name")
    named_descriptor = first.root / "Named study.coatproj"
    first.project_file.rename(named_descriptor)
    first = ProjectStore.open(named_descriptor)
    descriptors = {store.root: store.project_file for store in (first, second)}
    host = ProjectSessionHost(lambda root: ProjectStore.open(descriptors[root]))
    a, b = host.open_project(first.root), host.open_project(second.root)
    window = QWindow()
    route = host.reserve_module(a.binding.project_id, "film_design").route
    host.attach_window(route, window)
    bridge = Bridge()
    bridge.project_launcher = SimpleNamespace(host=host, projectsChanged=SimpleNamespace(emit=lambda: None))
    registry = SimpleNamespace(dispatch=lambda _operation, _args: {"windows": []})
    bridge.project_lifecycle = None
    value = ProjectApplicationClient(bridge, registry)
    yield value, a, b, route
    value.stop()
    window.close()


def test_window_rows_keep_owner_generation_and_instance(client):
    value, a, b, route = client
    result = value.dispatch("list_windows", {})
    assert len(result["windows"]) == 1
    row = result["windows"][0]
    assert row["project_id"] == a.binding.project_id
    assert row["session_generation"] == a.binding.generation
    assert row["instance_id"] == route.route_id
    with pytest.raises(UiControlError, match="窗口实例"):
        value.dispatch("read_window", {"project_id": b.binding.project_id,
                       "session_generation": b.binding.generation, "module_key": "film_design",
                       "instance_id": route.route_id})


def test_active_management_requires_both_generations(client):
    value, a, _b, _route = client
    store = value.bridge.project_launcher.host.store_for(a.binding)
    args = {"client_generation": value.generation,
            "session_generation": a.binding.generation,
            "project_operation": "inspect", "arguments": {
                "root": str(store.project_file), "project_id": a.binding.project_id}}
    assert value.dispatch("project_manage", args)["project_id"] == a.binding.project_id
    with pytest.raises(UiControlError, match="进程身份"):
        value.dispatch("project_manage", args | {"client_generation": "old"})
    with pytest.raises(UiControlError, match="会话身份"):
        value.dispatch("project_manage", args | {"session_generation": "old"})


def test_exit_gate_rejects_mutation_but_keeps_status_readable(client):
    value, _a, _b, _route = client
    value.bridge.project_lifecycle = SimpleNamespace(active=True)
    with pytest.raises(UiControlError, match="暂停"):
        value.dispatch("open_window", {"window_type": "settings"})
    assert value.dispatch("list_windows", {})["windows"]
    status = value.dispatch("lifecycle_status", {"nonce": "challenge"})
    assert status["nonce"] == "challenge"
    for session in value.bridge.project_launcher.host.sessions():
        store = value.bridge.project_launcher.host.store_for(session.binding)
        row = next(row for row in status["project_sessions"] if row["project_id"] == session.binding.project_id)
        assert row["root"] == str(store.root)
        assert row["project_file"] == str(store.project_file)
        assert store.project_file.is_file()
    assert status["project_sessions"][0]["project_file"].endswith("Named study.coatproj")
    assert len(status["project_sessions"]) == 2


def test_legacy_menu_uses_live_instance_and_hidden_launcher_identity(client):
    value, _a, _b, _route = client
    settings = QWindow()
    settings.setTitle("Settings")
    launcher = QWindow()
    launcher.setTitle("Launcher")
    value.bridge._launcher_window = launcher
    value.bridge._windows = {"settings-instance": SimpleNamespace(
        key="settings", instance_id="settings-instance", window=settings)}
    value.registry.dispatch = lambda _op, _args: {"windows": [
        {"window_type": "launcher"}, {"window_type": "settings"}]}
    rows = value.dispatch("list_windows", {})["windows"]
    assert rows[0]["open"] and not rows[0]["visible"]
    assert rows[0]["instance_id"]
    assert rows[1]["instance_id"] == "settings-instance"
    assert rows[1]["title"] == "Settings"
    value.bridge._windows.clear()
    with pytest.raises(UiControlError, match="窗口实例"):
        value.dispatch("close_window", {"window_type": "settings", "instance_id": "settings-instance"})
    value.bridge._launcher_window = None


def test_legacy_launcher_close_hides_without_registry_shutdown(client):
    value, a, b, _route = client
    launcher = QWindow()
    launcher.show()
    value.bridge._launcher_window = launcher
    value.bridge.closeLauncher = launcher.hide
    row = value._enrich_legacy({"window_type": "launcher"})
    result = value.dispatch("close_window", {"window_type": "launcher", "instance_id": row["instance_id"]})
    assert result["accepted"] and not launcher.isVisible()
    assert {session.binding.project_id for session in value._sessions()} == {a.binding.project_id, b.binding.project_id}


def test_legacy_identity_matches_registry_selected_handle(client):
    value, _a, _b, _route = client
    first, selected = QWindow(), QWindow()
    value.bridge._windows = {"first": SimpleNamespace(key="settings", instance_id="first", window=first)}
    value.bridge._existing_window = lambda key: SimpleNamespace(key=key, instance_id="selected", window=selected)
    assert value._legacy_identity("settings") == (selected, "selected")
    with pytest.raises(UiControlError, match="窗口实例"):
        value.dispatch("close_window", {"window_type": "settings", "instance_id": "first"})
