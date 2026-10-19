"""Persisted offline send/error/reopen sequences through the real chat bridge."""
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest

from coat_sim.ui.assistant import AssistantBridge
from coat_sim.ui.fonts import bundled_font_family
from tests.test_assistant_project_context import discover
from coat_sim.ui.preview_cursor import register_preview_cursor
from tests.test_assistant_message_body_qml import _Theme, visual_items
from tests.test_assistant_token_budget_contract import real_bridge as _real_bridge_fixture

real_bridge = _real_bridge_fixture
pytestmark = [pytest.mark.qt, pytest.mark.contract]
ERROR = "stale_project_binding: 工程已切换、关闭或者不可核验；请在当前窗口重新选择工程。"


def settle_history(bridge):
    # Real completion fence: the queued persist/read tasks and the Qt-side
    # projection must all finish, not just a fixed number of qWait cycles.
    queued = bridge._storage_pool.submit(lambda: None)
    deadline = time.monotonic() + 10.0
    while True:
        QTest.qWait(20)
        if (
            queued.done()
            and not bridge.historyLoading
            and not bridge.historyProjectionPending
        ):
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                "Offline history reload did not settle: "
                f"queue_done={queued.done()} "
                f"historyLoading={bridge.historyLoading} "
                f"historyProjectionPending={bridge.historyProjectionPending} "
                f"historyError={bridge.historyError!r}"
            )


def assert_painted(window, expected):
    for _ in range(100):
        QTest.qWait(20)
        editors = [item for item in visual_items(window.contentItem())
                   if 'QQuickTextEdit' in item.metaObject().className()
                   and item.isVisible() and expected in str(item.property('renderedSource') or '')]
        if editors:
            break
    if not editors:
        sources = [str(item.property('renderedSource'))
                   for item in visual_items(window.contentItem())
                   if 'QQuickTextEdit' in item.metaObject().className()]
        raise AssertionError(f"Missing rendered body {expected!r}; sources={sources!r}")
    editor = editors[0]
    QTest.qWait(60)
    point = editor.mapToItem(window.contentItem(), QPointF(0, 0))
    dpr = window.devicePixelRatio()
    rect = (round(point.x() * dpr), round(point.y() * dpr),
            round(editor.width() * dpr), round(editor.height() * dpr))
    shown = window.grabWindow().copy(*rect)
    editor.setOpacity(0)
    QTest.qWait(30)
    hidden = window.grabWindow().copy(*rect)
    editor.setOpacity(1)
    QTest.qWait(30)
    pixels = sum(shown.pixel(x, y) != hidden.pixel(x, y)
                 for y in range(shown.height()) for x in range(shown.width()))
    if pixels <= 20:
        raise AssertionError(f"Body {expected!r} painted only {pixels} pixels at {rect!r}")


@pytest.mark.parametrize('lifecycle', ['hidden_first_show', 'visible_error', 'hide_reopen', 'switch_session',
                                      'switch_global', 'cold_bridge'])
def test_persisted_error_round_body_survives_real_lifecycle(real_bridge, monkeypatch, tmp_path, lifecycle):
    bridge, worker = real_bridge
    if lifecycle == 'switch_global':
        discover(bridge, monkeypatch)
        assert bridge.selectProjectContext(1)
    monkeypatch.setattr(bridge, 'refreshProjectContexts', lambda: None)
    bundled_font_family()
    register_preview_cursor()
    engine = QQmlEngine()
    theme = _Theme('light')
    engine.rootContext().setContextProperty('assistant', bridge)
    engine.rootContext().setContextProperty('Theme', theme)
    path = Path(__file__).parents[1] / 'coat_sim/ui/qml/AssistantChatWindow.qml'
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
    window = component.create()
    assert isinstance(window, QQuickWindow), component.errors()
    reopened = None
    try:
        window.resize(860, 1000)
        settle_history(bridge)
        if lifecycle != 'hidden_first_show':
            window.show()
            QTest.qWait(100)
        if lifecycle == 'hide_reopen':
            window.hide()
        if lifecycle != 'switch_global':
            assert bridge.selectProjectContext(0)
        assert bridge.sendMessage('你好')
        request = bridge._request_id
        # No token arrives: the terminal error is the entire assistant body.
        bridge._on_worker_event({'process': worker.pid, 'payload': {
            'event': 'error', 'request_id': request, 'text': ERROR}})
        assert not bridge.busy
        settle_history(bridge)
        session = bridge.currentSessionId
        persisted = bridge._store.messages(session)
        assert any(row['content'] == '你好' and row['role'] == 'user' for row in persisted)
        assert any(row['content'] == ERROR and row['role'] == 'assistant' for row in persisted)
        if lifecycle == 'switch_global':
            assert bridge.selectProjectContext(0)
            assert bridge.projectContextIndex == 0
        if lifecycle == 'cold_bridge':
            reopened = AssistantBridge()
            monkeypatch.setattr(reopened, 'refreshProjectContexts', lambda: None)
            bridge = reopened
            engine.rootContext().setContextProperty('assistant', bridge)
            settle_history(bridge)
            assert bridge.currentSessionId == session
        if lifecycle == 'switch_session':
            bridge.newConversation()
            settle_history(bridge)
            bridge.selectConversation(session)
            settle_history(bridge)
            assert bridge.currentSessionId == session
        window.show()
        assert_painted(window, '你好<br>')
        assert_painted(window, 'stale_project_binding')
        # Read the same SQLite rows back once more without manually replacing
        # message/model/cache state or resetting invalidation flags.
        bridge._reload_messages()
        settle_history(bridge)
        assert_painted(window, 'stale_project_binding')
        assert window.grabWindow().save(str(tmp_path / (lifecycle + '.png')))
    finally:
        window.hide()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        if reopened is not None:
            reopened.shutdown()
            reopened._storage_pool.shutdown(wait=True, cancel_futures=True)
            reopened._prefs._shutdown_key_lookup()
            reopened._prefs._shutdown_probe_budget()
            reopened._prefs._shutdown_mcp()
