"""Explicit desktop project selection and immutable worker-scope handoff."""
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QUrl
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from tests.test_assistant_token_budget_contract import real_bridge as _real_bridge_fixture
from tests.test_assistant_bubble import ThemeStub
from coat_sim.assistant_process import AssistantProcessClient
from coat_sim.project_activation import ACTIVATION_FIELDS, activation_target
from coat_sim.ui import assistant
from coat_sim.ui.preview_cursor import register_preview_cursor

real_bridge = _real_bridge_fixture
pytestmark = [pytest.mark.contract, pytest.mark.qt]


def clients():
    return [{"client_id": "host-one", "generation": "owner-one", "state": "alive",
             "project_sessions": [{"project_id": "project-a", "generation": "session-a",
                 "root": "/opened/project-a", "project_file": "/opened/project-a/project.coatproj",
                 "name": "工程 A"}, {"project_id": "project-b", "generation": "session-b",
                 "root": "/opened/project-b", "project_file": "/opened/project-b/project.coatproj",
                 "name": "工程 B"}]}]


class InlineThread:
    def __init__(self, *, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


def discover(bridge, monkeypatch, rows=None):
    rows = clients() if rows is None else rows
    monkeypatch.setattr(assistant, "Thread", InlineThread)
    monkeypatch.setattr(AssistantProcessClient, "list_clients", lambda _self: deepcopy(rows))
    bridge.refreshProjectContexts()
    return rows


def test_authenticated_candidates_require_explicit_user_selection(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    one = clients()
    one[0]["project_sessions"] = one[0]["project_sessions"][:1]
    discover(bridge, monkeypatch, one)
    assert bridge.projectContexts == [{"text": "全局"}, {"text": "工程 A"}]
    assert bridge.projectContextIndex == 0  # A unique open project is not authority.
    assert bridge._project_context_target is None
    assert worker.stdin.commands() == []
    assert bridge.selectProjectContext(1)
    command, = worker.stdin.commands()
    assert command["op"] == "configure"
    assert set(command["assistant_project_target"]) == ACTIVATION_FIELDS
    assert command["assistant_project_target"] == activation_target(one[0], one[0]["project_sessions"][0])
    assert "assistant_project_target" not in command["config"]
    assert "assistant_project_target" not in bridge._config
    assert not bridge.selectProjectContext(9)
    assert bridge.projectContextIndex == 1


def test_run_transmits_original_detached_target_and_keeps_global_available(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    discover(bridge, monkeypatch)
    bridge.selectProjectContext(1)
    row, _ = bridge._prepare_message("记住工程偏好")
    assert row is not None
    assert row["assistant_project_target"] is not bridge._project_context_target
    assert bridge._start_message(row)
    configure, run = worker.stdin.commands()
    assert run["op"] == "run"
    assert run["assistant_project_target"] == configure["assistant_project_target"]
    assert "assistant_project_target" not in run["config"]


def test_no_selection_run_is_explicitly_global(real_bridge):
    bridge, worker = real_bridge
    assert bridge.sendMessage("记住全局偏好")
    command, = worker.stdin.commands()
    assert command["assistant_project_target"] is None


def test_worker_start_or_restart_syncs_context_before_run(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    discover(bridge, monkeypatch)
    bridge.selectProjectContext(1)
    worker.stdin.lines.clear()
    monkeypatch.setattr(bridge, "_ensure_worker", assistant.AssistantBridge._ensure_worker.__get__(bridge))
    bridge._run_coordinator.worker = None
    monkeypatch.setattr(bridge._run_coordinator, "ensure_worker",
                        lambda _event: setattr(bridge._run_coordinator, "worker", worker))
    assert bridge.sendMessage("明确的工程消息")
    configure, run = worker.stdin.commands()
    assert configure["op"] == "configure"
    assert run["op"] == "run"
    assert configure["assistant_project_target"] == run["assistant_project_target"]


def test_switch_revokes_busy_scope_and_never_rebinds_queued_messages(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    discover(bridge, monkeypatch)
    bridge.selectProjectContext(1)
    assert bridge.sendMessage("A 正在运行")
    assert bridge.sendMessage("A 的排队消息")
    queued = deepcopy(bridge._pending_messages[0])
    attachment = {"path": "/draft.png", "size": 1}
    bridge._attachments = [attachment]
    assert bridge.selectProjectContext(2)
    assert worker.stdin.commands()[-1]["op"] == "configure"
    assert worker.stdin.commands()[-1]["assistant_project_target"]["project_id"] == "project-b"
    assert bridge._pending_messages[0]["paused"] is True
    assert bridge._pending_messages[0]["assistant_project_target"] == queued["assistant_project_target"]
    assert bridge._attachments == [attachment]
    assert not bridge.steerMessage("B 的插话")
    assert not bridge.sendDecisionSupplement("B 的补充")
    assert bridge.selectProjectContext(1)
    bridge._set_busy(False)
    assert not bridge._start_message(bridge._pending_messages[0])
    assert "撤回输入框" in bridge.statusMessage
    assert sum(command["op"] == "run" for command in worker.stdin.commands()) == 1


@pytest.mark.parametrize("change", ["close", "offline", "owner_generation", "session_generation", "root", "descriptor"])
def test_close_or_any_identity_change_revokes_without_guessing(real_bridge, monkeypatch, change):
    bridge, worker = real_bridge
    rows = discover(bridge, monkeypatch)
    bridge.selectProjectContext(1)
    if change == "close":
        rows[0]["project_sessions"].pop(0)
    elif change == "offline":
        rows[0]["state"] = "unknown"
    elif change == "owner_generation":
        rows[0]["generation"] = "replacement-owner"
    elif change == "session_generation":
        rows[0]["project_sessions"][0]["generation"] = "reopened-session"
    else:
        key = "root" if change == "root" else "project_file"
        rows[0]["project_sessions"][0][key] += "-changed"
    discover(bridge, monkeypatch, rows)
    assert bridge.projectContextIndex == 0
    assert bridge._project_context_target is None
    assert worker.stdin.commands()[-1]["assistant_project_target"] is None
    assert "已切回全局" in bridge.projectContextStatus
    discover(bridge, monkeypatch)
    assert bridge.projectContextIndex == 0  # Reconnection never restores authority.


def test_discovery_failure_revokes_and_global_requests_remain_usable(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    discover(bridge, monkeypatch)
    bridge.selectProjectContext(1)

    def unavailable(_self):
        raise OSError("endpoint disappeared")

    monkeypatch.setattr(AssistantProcessClient, "list_clients", unavailable)
    bridge.refreshProjectContexts()
    assert not bridge.projectContextLoading
    assert "无法核验" in bridge.projectContextStatus
    assert bridge.projectContexts == [{"text": "全局"}]
    assert bridge.sendMessage("继续全局讨论")
    assert worker.stdin.commands()[-1]["assistant_project_target"] is None


def test_refresh_does_not_revoke_unchanged_target_or_rewrite_config(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    rows = discover(bridge, monkeypatch)
    original_config = deepcopy(bridge._config)
    bridge.selectProjectContext(1)
    epoch = bridge._project_context_generation
    rows[0]["project_sessions"].reverse()
    discover(bridge, monkeypatch, rows)
    assert bridge.projectContextIndex == 2
    assert bridge._project_context_generation == epoch
    assert len(worker.stdin.commands()) == 1
    assert bridge._config == original_config


def test_refresh_is_single_flight_and_late_result_cannot_restore_closed_bridge(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    pending = []

    class DeferredThread(InlineThread):
        def start(self):
            pending.append(self.target)

    monkeypatch.setattr(assistant, "Thread", DeferredThread)
    monkeypatch.setattr(AssistantProcessClient, "list_clients", lambda _self: clients())
    bridge.refreshProjectContexts()
    bridge.refreshProjectContexts()
    assert bridge.projectContextLoading
    assert len(pending) == 1
    bridge._closed = True
    pending.pop()()
    assert bridge._project_context_rows == []
    assert worker.stdin.commands() == []


def test_real_qml_selector_has_theme_controls_and_changes_host_selection(real_bridge, monkeypatch, tmp_path):
    bridge, worker = real_bridge
    discover(bridge, monkeypatch)
    register_preview_cursor()
    engine = QQmlApplicationEngine()
    theme = ThemeStub()
    engine.rootContext().setContextProperty("assistant", bridge)
    engine.rootContext().setContextProperty("Theme", theme)
    qml = Path(assistant.__file__).parent / "qml" / "AssistantChatWindow.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(qml)))
    assert not component.isError(), component.errors()
    window = component.create()
    assert isinstance(window, QQuickWindow), component.errors()
    try:
        selector = window.findChild(QObject, "assistantProjectContextSelector")
        refresh = window.findChild(QObject, "assistantProjectContextRefresh")
        assert isinstance(selector, QQuickItem) and isinstance(refresh, QQuickItem)
        for width, height in ((560, 420), (860, 720)):
            window.resize(width, height)
            window.show()
            QTest.qWait(30)
            assert 0 < selector.width() < width
            assert selector.x() + selector.width() <= refresh.x()
            assert refresh.x() + refresh.width() <= width
        image = window.grabWindow()
        assert not image.isNull()
        assert image.save(str(tmp_path / "assistant-context-ui.png"))
        assert selector.property("count") == 3
        assert selector.property("currentIndex") == 0
        cast(Any, selector).activated.emit(1)
        QCoreApplication.processEvents()
        assert bridge.projectContextIndex == 1
        assert selector.property("currentIndex") == 1
        assert worker.stdin.commands()[-1]["assistant_project_target"]["project_id"] == "project-a"
        bridge.selectProjectContext(0)
        QCoreApplication.processEvents()
        assert selector.property("currentIndex") == 0
    finally:
        window.hide()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_real_ui_wire_snapshots_feed_worker_authority_and_revoke_old_scope(real_bridge, monkeypatch):
    from io import StringIO
    from coat_sim.agent.worker import ProtocolWriter

    bridge, worker = real_bridge
    discover(bridge, monkeypatch)
    bridge.selectProjectContext(1)
    assert bridge.sendMessage("工程 A 请求")
    configure, run = worker.stdin.commands()
    writer = ProtocolWriter(StringIO())
    writer.configure_project_target(configure["assistant_project_target"])
    first = writer.project_binding_snapshot(run["assistant_project_target"])
    assert first.verify()
    assert first.providers()["memory_project_id"]
    bridge.selectProjectContext(2)
    writer.configure_project_target(worker.stdin.commands()[-1]["assistant_project_target"])
    assert not first.verify()
    with pytest.raises(ValueError, match="stale_project_binding"):
        writer.project_binding_snapshot(run["assistant_project_target"])
    bridge.selectProjectContext(1)
    writer.configure_project_target(worker.stdin.commands()[-1]["assistant_project_target"])
    assert not first.verify()  # Same target never revives an already-revoked request.
    reopened = writer.project_binding_snapshot(run["assistant_project_target"])
    assert reopened.verify()
    assert reopened.providers()["memory_project_id"] == first.providers()["memory_project_id"]


def test_failed_revocation_stops_old_worker_before_continuing(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    discover(bridge, monkeypatch)
    bridge.selectProjectContext(1)
    assert bridge.sendMessage("工程 A 请求")

    def broken_pipe(_value):
        raise OSError("closed")

    monkeypatch.setattr(worker.stdin, "write", broken_pipe)
    assert bridge.selectProjectContext(2)
    assert bridge._worker is None
    assert worker.return_code == 0


def test_explicit_runtime_root_is_shared_by_host_ui_and_child_worker(tmp_path, monkeypatch):
    import os
    from coat_sim.ui import assistant_process_host

    selected = tmp_path / "alternate-runtime"
    monkeypatch.setenv("COAT_ASSISTANT_RUNTIME_ROOT", str(tmp_path / "wrong-default"))

    class StopBeforeCreatingApplication(Exception):
        pass

    def check_runtime():
        assert os.environ["COAT_ASSISTANT_RUNTIME_ROOT"] == str(selected.resolve())
        raise StopBeforeCreatingApplication()

    monkeypatch.setattr(assistant_process_host, "configure_qt_dpi_scale", check_runtime)
    with pytest.raises(StopBeforeCreatingApplication):
        assistant_process_host.run_assistant(selected)


def test_manual_compaction_carries_selected_target(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    discover(bridge, monkeypatch)
    bridge.selectProjectContext(1)
    assert bridge.compactContext()
    configure, compact = worker.stdin.commands()
    assert compact["op"] == "compact"
    assert compact["assistant_project_target"] == configure["assistant_project_target"]


def test_manual_compaction_cannot_use_old_active_request_after_scope_switch(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    discover(bridge, monkeypatch)
    bridge.selectProjectContext(1)
    assert bridge.sendMessage("工程 A 请求")
    bridge.selectProjectContext(2)
    assert not bridge.compactContext()
    assert all(command["op"] != "compact" for command in worker.stdin.commands())


def test_background_notifications_wait_for_original_explicit_scope(real_bridge, monkeypatch):
    from coat_sim.agent.worker import ProjectMemoryBinding

    bridge, worker = real_bridge
    rows = discover(bridge, monkeypatch)
    target = activation_target(rows[0], rows[0]["project_sessions"][0])
    scope = ProjectMemoryBinding(target, lambda: True).memory_project_id
    bridge._store.record_event(bridge._session, "original-a", "project_memory_scope", {"scope": scope})
    assert bridge._store.receive_background_notification(bridge._session, "original-a", "job-a",
                                                        {"job_id": "job-a", "state": "succeeded",
                                                         "origin": {"session_id": bridge._session, "request_id": "original-a"}})
    bridge.selectProjectContext(2)
    bridge._drain_queue()
    assert len(bridge._store.pending_background_notifications(bridge._session)) == 1
    assert all(command["op"] != "run" for command in worker.stdin.commands())
    bridge.selectProjectContext(1)
    bridge._drain_queue()
    assert bridge._store.pending_background_notifications(bridge._session) == []
    run = worker.stdin.commands()[-1]
    assert run["op"] == "run"
    assert run["assistant_project_target"] == target
    assert run["background_notification"]["job_id"] == "job-a"


def test_legacy_global_notification_cannot_adopt_selected_project(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    discover(bridge, monkeypatch)
    assert bridge._store.receive_background_notification(bridge._session, "legacy-global", "job-g",
                                                        {"job_id": "job-g", "state": "succeeded",
                                                         "origin": {"session_id": bridge._session, "request_id": "legacy-global"}})
    bridge.selectProjectContext(1)
    bridge._drain_queue()
    assert len(bridge._store.pending_background_notifications(bridge._session)) == 1
    bridge.selectProjectContext(0)
    bridge._drain_queue()
    run = worker.stdin.commands()[-1]
    assert run["op"] == "run"
    assert run["assistant_project_target"] is None


def test_busy_compaction_does_not_reconfigure_existing_worker(real_bridge, monkeypatch):
    bridge, worker = real_bridge
    assert bridge.sendMessage("保持当前请求配置")
    monkeypatch.setattr(bridge, "_ensure_worker", assistant.AssistantBridge._ensure_worker.__get__(bridge))
    monkeypatch.setattr(bridge._run_coordinator, "ensure_worker", lambda _event: None)
    monkeypatch.setattr(bridge, "_worker_config", lambda: pytest.fail("active worker config must stay fixed"))
    assert bridge.compactContext()
    assert [command["op"] for command in worker.stdin.commands()] == ["run", "compact"]
