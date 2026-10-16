"""Detached owners cannot impersonate or accidentally close a launcher."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication

from coat_sim.ui.control import UiControlClient, UiControlError, WindowRegistry
from coat_sim.ui.launcher import LauncherBridge
from coat_sim.ui.project_application_client import ProjectApplicationClient

pytestmark = [pytest.mark.contract, pytest.mark.qt]


@pytest.fixture
def router():
    app = cast(QApplication, QApplication.instance() or QApplication([]))
    engine = QQmlApplicationEngine()
    bridge = LauncherBridge(engine, with_launcher=False)
    bridge.project_runtime = SimpleNamespace(host=SimpleNamespace(sessions=lambda: ()))
    registry = WindowRegistry(bridge)
    yield bridge, registry
    registry.close()
    bridge._shutdown_timer.stop()
    bridge.deleteLater()
    engine.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("operation", ["open_window", "read_window", "close_window"])
def test_explicit_runtime_window_registry_rejects_launcher(router, operation):
    _bridge, registry = router
    with pytest.raises(UiControlError) as caught:
        registry.dispatch(operation, {"window_type": "launcher"})
    assert caught.value.code == "global_window_unavailable_in_runtime"
    listing = registry.dispatch("list_windows", {})
    assert "launcher" not in listing["available_windows"]
    assert not any(row["window_type"] == "launcher" for row in listing["windows"])


def test_runtime_application_close_launcher_cannot_shut_down_project(router):
    bridge, registry = router
    client = ProjectApplicationClient(bridge, registry, role="project_host")
    try:
        with pytest.raises(UiControlError) as caught:
            client.dispatch("close_window", {"window_type": "launcher"})
        assert caught.value.code == "global_window_unavailable_in_runtime"
        assert not bridge._closing
        status = client.status("nonce")
        assert status["role"] == "project_host" and status["scope"] == "project_runtime"
    finally:
        client.stop()


def test_generic_open_launcher_with_only_runtime_starts_true_launcher(monkeypatch, tmp_path):
    import coat_sim.ui.control as module
    monkeypatch.delenv("COAT_UI_PID", raising=False)
    client = UiControlClient(tmp_path)
    monkeypatch.setattr(client, "_endpoints", lambda: [{"role": "project_host", "pid": 55}])
    calls = []
    def start(arguments):
        calls.append(arguments)
        return {"window_type": "launcher", "pid": 77, "state": "loading"}
    monkeypatch.setattr(client, "_start_ui", start)
    monkeypatch.setattr(module, "build_opener", lambda *_args: pytest.fail("runtime must not receive launcher request"))
    assert client.request("open_window", window_type="launcher")["pid"] == 77
    assert calls == [{"window_type": "launcher"}]


def test_generic_launcher_request_ignores_preferred_runtime_and_reuses_real_picker(monkeypatch, tmp_path):
    import coat_sim.ui.control as module
    monkeypatch.delenv("COAT_UI_PID", raising=False)
    client = UiControlClient(tmp_path)
    monkeypatch.setattr(client, "_endpoints", lambda: [
        {"role": "project_host", "pid": 55, "port": 1055, "token": "runtime"},
        {"role": "launcher", "pid": 77, "port": 1077, "token": "picker"}])
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self, _size):
            return json.dumps({"ok": True, "result": {"pid": 77}}).encode()
    def opened(request, timeout):
        assert request.full_url == "http://127.0.0.1:1077/call"
        return Response()
    monkeypatch.setattr(module, "build_opener", lambda *_args: SimpleNamespace(open=opened))
    monkeypatch.setattr(client, "_start_ui", lambda *_args: pytest.fail("real launcher should be reused"))
    result: Any = client.request("open_window", window_type="launcher")
    assert result["pid"] == 77 and client.target_pid == 77


@pytest.mark.parametrize("key", ["main", "film_design", "film_simulation", "process", "settings", "about"])
def test_runtime_rejects_unbound_business_and_global_windows(router, key):
    bridge, registry = router
    client = ProjectApplicationClient(bridge, registry, role="project_host")
    try:
        for operation in ("open_window", "read_window", "close_window"):
            with pytest.raises(UiControlError) as caught:
                client.dispatch(operation, {"window_type": key})
            assert caught.value.code == ("global_window_unavailable_in_runtime"
                                          if key in {"settings", "about"} else "project_identity_required")
        assert not bridge._windows and bridge._launcher_window is None
    finally:
        client.stop()


def test_explicit_cli_module_host_keeps_desktop_scope_and_legacy_dispatch(router):
    bridge, registry = router
    bridge.project_runtime = None
    calls = []
    def legacy_dispatch(operation, arguments):
        calls.append((operation, arguments))
        return {"state": "standalone"}
    client = ProjectApplicationClient(bridge, SimpleNamespace(dispatch=legacy_dispatch), role="project_host")
    try:
        assert client.dispatch("open_window", {"window_type": "main"}) == {"state": "standalone"}
        assert calls == [("open_window", {"window_type": "main"})]
        status = client.status("nonce")
        assert status["role"] == "project_host" and status["scope"] == "standalone"
        assert "main" in registry.dispatch("list_windows", {})["available_windows"]
    finally:
        client.stop()


def test_explicit_cli_host_business_request_retains_legacy_capability(monkeypatch, tmp_path):
    import coat_sim.ui.control as module
    monkeypatch.delenv("COAT_UI_PID", raising=False)
    client = UiControlClient(tmp_path, target_pid=77)
    monkeypatch.setattr(client, "_endpoints", lambda: [
        {"role": "project_host", "window_type": "main", "pid": 77, "port": 1077, "token": "cli"}])
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self, _size):
            return b'{"ok": true, "result": {"pid": 77}}'
    def opened(request, timeout):
        assert request.full_url == "http://127.0.0.1:1077/call"
        return Response()
    monkeypatch.setattr(module, "build_opener", lambda *_args: SimpleNamespace(open=opened))
    assert client.request("open_window", window_type="main")["pid"] == 77


def test_generic_unscoped_business_request_never_guesses_or_spawns(monkeypatch, tmp_path):
    monkeypatch.delenv("COAT_UI_PID", raising=False)
    client = UiControlClient(tmp_path)
    monkeypatch.setattr(client, "_endpoints", lambda: [])
    monkeypatch.setattr(client, "_start_ui", lambda *_args: pytest.fail("unscoped business open must not spawn"))
    with pytest.raises(UiControlError) as caught:
        client.request("open_window", window_type="main")
    assert caught.value.code == "project_identity_required"


def test_thin_launcher_advertises_only_global_windows_and_rejects_business(router):
    bridge, registry = router
    bridge.project_runtime = None
    bridge.project_launcher = SimpleNamespace()
    listing = registry.dispatch("list_windows", {})
    assert listing["scope"] == "launcher"
    assert listing["available_windows"] == ["launcher", "settings", "about"]
    assert listing["same_process_windows"] == []
    for key in ("main", "film_design", "source"):
        with pytest.raises(UiControlError) as caught:
            registry.dispatch("open_window", {"window_type": key})
        assert caught.value.code == "project_identity_required"
    assert not bridge._windows


@pytest.mark.parametrize("key", ["launcher", "settings", "about"])
def test_global_starts_use_true_launcher_without_cli_module(monkeypatch, tmp_path, key):
    import coat_sim.ui.control as module
    monkeypatch.delenv("COAT_UI_PID", raising=False)
    client = UiControlClient(tmp_path)
    commands = []
    class Channel:
        def child_environment(self):
            return {}
        def close(self):
            pass
    monkeypatch.setattr(module, "UiReadyChannel", Channel)
    def popen(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module.threading, "Thread", lambda **_kwargs: SimpleNamespace(start=lambda: None))
    result = client._start_ui({"window_type": key})
    assert result["state"] == "loading" and result["window_type"] == key
    assert commands and commands[0][-1] == str(tmp_path / "launcher.pyw")
    assert "--module" not in commands[0]
