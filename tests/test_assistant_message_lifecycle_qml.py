"""Persisted offline send/error/reopen sequences through the real chat bridge."""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
import shiboken6
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


def _safe_property(item, name, default=None):
    if item is None or not shiboken6.isValid(item):
        return default
    try:
        value = item.property(name)
        return default if value is None else value
    except RuntimeError:
        return default


def _paint_failure_diagnostics(window, bridge, expected, phases=(), model_events=(), list_events=()):
    """Snapshot the data model and realized QML tree without changing either."""
    visual = []
    tree_error = ""
    if window is None or not shiboken6.isValid(window):
        tree_error = "window wrapper is invalid"
    else:
        try:
            visual = list(visual_items(window.contentItem()))
        except RuntimeError as exc:
            tree_error = f"visual tree changed during inspection: {type(exc).__name__}"

    def geometry(item):
        if item is None or not shiboken6.isValid(item):
            return None
        try:
            point = item.mapToScene(QPointF(0, 0))
            return [round(point.x(), 2), round(point.y(), 2),
                    round(item.width(), 2), round(item.height(), 2)]
        except RuntimeError:
            return None

    def short_row(row):
        if not isinstance(row, dict):
            return {"row_type": type(row).__name__}
        text = str(row.get("text", ""))
        key = str(row.get("key", ""))
        sources = getattr(bridge, "_body_sources", {}) if bridge is not None else {}
        source = sources.get(key, ("", False))[0]
        document = row.get("document")
        return {
            "key": key, "role": str(row.get("role", "")),
            "text_length": len(text), "source_length": len(str(source)),
            "source_has_expected": expected.split("<br>", 1)[0] in str(source),
            "has_document": document is not None,
            "document_source_length": len(str(_safe_property(document, "sourceText", ""))),
            "has_steps": row.get("hasSteps") is True,
        }

    result = {"tree_error": tree_error, "expected": expected,
              "expected_source": expected.split("<br>", 1)[0],
              "phases": list(phases), "model_events": list(model_events),
              "list_events": list(list_events),
              "font_family": bundled_font_family(),
              "qt_quick_backend": os.environ.get("QSG_RHI_BACKEND", "unspecified")}
    if window is not None and shiboken6.isValid(window):
        try:
            result["window"] = {
                "visible": bool(window.isVisible()), "exposed": bool(window.isExposed()),
                "visibility": str(window.visibility()),
                "size": [round(window.width(), 2), round(window.height(), 2)],
                "dpr": round(window.devicePixelRatio(), 3),
            }
        except RuntimeError:
            result["window"] = "invalidated during inspection"

    transcript = next((item for item in visual
                       if shiboken6.isValid(item) and item.objectName() == "chatTranscript"), None)
    if transcript is not None:
        model = _safe_property(transcript, "model")
        try:
            model_count = int(model.rowCount()) if model is not None and shiboken6.isValid(model) else None
            result["transcript"] = {
                "visible": bool(transcript.isVisible()), "size": geometry(transcript),
                "count": _safe_property(transcript, "count"),
                "model_count": model_count,
                "contentY": _safe_property(transcript, "contentY"),
                "contentHeight": _safe_property(transcript, "contentHeight"),
                "originY": _safe_property(transcript, "originY"),
                "atYBeginning": _safe_property(transcript, "atYBeginning"),
                "atYEnd": _safe_property(transcript, "atYEnd"),
            }
        except RuntimeError:
            result["transcript"] = "invalidated during inspection"

    delegates, bodies, loaders, editors = [], [], [], []
    for item in visual:
        if not shiboken6.isValid(item):
            continue
        try:
            name = str(item.objectName())
            class_name = str(item.metaObject().className())
            turn_data = _safe_property(item, "turnData")
            if isinstance(turn_data, dict):
                delegates.append({
                    "index": _safe_property(item, "index"),
                    "key": str(turn_data.get("key", "")),
                    "role": str(turn_data.get("role", "")),
                    "text_length": len(str(turn_data.get("text", ""))),
                    "visible": bool(item.isVisible()), "size": geometry(item),
                    "pooled": _safe_property(item, "delegatePooled"),
                })
            if name.startswith("chatTurnBody_"):
                document = _safe_property(item, "document")
                bodies.append({
                    "name": name, "visible": bool(item.isVisible()),
                    "size": geometry(item), "suspended": _safe_property(item, "suspended"),
                    "document": document is not None,
                    "rendered_ready": _safe_property(item, "renderedDocumentReady"),
                    "fallback_length": len(str(_safe_property(item, "fallbackText", ""))),
                })
            if "Loader" in class_name:
                child = _safe_property(item, "item")
                loaders.append({
                    "name": name, "active": _safe_property(item, "active"),
                    "status": _safe_property(item, "status"),
                    "progress": _safe_property(item, "progress"),
                    "item": (str(child.metaObject().className())
                             if child is not None and shiboken6.isValid(child) else None),
                    "visible": bool(item.isVisible()), "size": geometry(item),
                })
            if "QQuickTextEdit" in class_name:
                source = str(_safe_property(item, "renderedSource", "") or "")
                text = str(_safe_property(item, "text", "") or "")
                editors.append({
                    "name": name, "visible": bool(item.isVisible()),
                    "size": geometry(item), "rendered_length": len(source),
                    "rendered_has_expected": expected in source,
                    "text_length": len(text),
                    "text_has_expected": expected.split("<br>", 1)[0] in text,
                    "format": _safe_property(item, "textFormat"),
                })
        except RuntimeError:
            continue
    result["delegates"], result["bodies"] = delegates, bodies
    result["loaders"], result["text_editors"] = loaders, editors

    if bridge is None or not shiboken6.isValid(bridge):
        result["bridge"] = "bridge wrapper is invalid"
        return result
    try:
        model = bridge._turn_model
        result["bridge"] = {
            "session": str(bridge.currentSessionId),
            "project_index": int(bridge.projectContextIndex),
            "project_generation": int(bridge._project_context_generation),
            "busy": bool(bridge.busy),
            "history_loading": bool(bridge.historyLoading),
            "history_projection_pending": bool(bridge.historyProjectionPending),
            "projection": ({key: bridge._history_projection.get(key)
                            for key in ("cursor", "tail_ready", "pending", "loaded_older_count")}
                           if bridge._history_projection is not None else None),
            "model_valid": bool(model is not None and shiboken6.isValid(model)),
            "model_count": int(model.rowCount()) if model is not None and shiboken6.isValid(model) else None,
            "model_rows": [short_row(row) for row in getattr(model, "_rows", [])],
            "messages": [{"role": str(row.get("role", "")),
                          "content_length": len(str(row.get("content", ""))),
                          "content_has_expected": expected.split("<br>", 1)[0]
                            in str(row.get("content", "")),
                          "request_present": bool(row.get("request_id"))}
                         for row in bridge._messages],
            "body_sources": {key: {"length": len(str(value[0])),
                                    "has_expected": expected.split("<br>", 1)[0]
                                        in str(value[0])}
                              for key, value in bridge._body_sources.items()},
            "renderer": {"closed": bool(bridge._renderer._closed),
                         "working": bool(bridge._renderer._working),
                         "pending": len(bridge._renderer._pending),
                         "live_pending": len(bridge._renderer._live_pending),
                         "documents": [{"identity": key,
                                        "source_length": len(str(doc.source)),
                                        "final": bool(doc._final),
                                        "blocks": int(doc.model.rowCount())}
                                       for key, doc in bridge._renderer.documents.items()]},
        }
    except (RuntimeError, AttributeError, TypeError, ValueError) as exc:
        result["bridge"] = f"state invalidated during inspection: {type(exc).__name__}"
    return result


def _capture_history_phase(bridge, label, model_events, list_events):
    model = bridge._turn_model
    return {
        "phase": label,
        "session": str(bridge.currentSessionId),
        "project_index": int(bridge.projectContextIndex),
        "project_generation": int(bridge._project_context_generation),
        "busy": bool(bridge.busy),
        "history_loading": bool(bridge.historyLoading),
        "history_projection_pending": bool(bridge.historyProjectionPending),
        "model_count": int(model.rowCount()),
        "model_keys": [str(row.get("key", "")) for row in model._rows],
        "message_roles": [str(row.get("role", "")) for row in bridge._messages],
        "model_event_count": len(model_events),
        "list_event_count": len(list_events),
    }


def assert_painted(window, expected, bridge=None, phases=(), model_events=(), list_events=(),
                   qml_warnings=(), diagnostic_dir=None, diagnostic_label=""):
    for _ in range(100):
        QTest.qWait(20)
        try:
            items = list(visual_items(window.contentItem()))
        except RuntimeError:
            items = []
        editors = []
        for item in items:
            try:
                if (shiboken6.isValid(item)
                        and 'QQuickTextEdit' in item.metaObject().className()
                        and item.isVisible()
                        and expected in str(item.property('renderedSource') or '')):
                    editors.append(item)
            except RuntimeError:
                continue
        if editors:
            break
    if not editors:
        sources = []
        try:
            items = list(visual_items(window.contentItem()))
        except RuntimeError:
            items = []
        for item in items:
            try:
                if (shiboken6.isValid(item)
                        and 'QQuickTextEdit' in item.metaObject().className()):
                    sources.append(str(item.property('renderedSource') or ''))
            except RuntimeError:
                continue
        diagnostics = _paint_failure_diagnostics(
            window, bridge, expected, phases, model_events, list_events)
        diagnostics["legacy_sources"] = sources
        diagnostics["qml_warnings"] = list(qml_warnings)
        if diagnostic_dir is not None:
            try:
                diagnostic_dir = Path(diagnostic_dir)
                diagnostic_dir.mkdir(parents=True, exist_ok=True)
                suffix = hashlib.sha256(expected.encode("utf8")).hexdigest()[:10]
                label = "".join(character for character in diagnostic_label
                                 if character.isalnum() or character in "-_-")[:48]
                stem = f"assistant-message-{label + '-' if label else ''}{suffix}"
                screenshot_path = diagnostic_dir / f"{stem}.png"
                state_path = diagnostic_dir / f"{stem}.json"
                diagnostics["state_file"] = str(state_path)
                try:
                    screenshot = window.grabWindow() if shiboken6.isValid(window) else None
                    diagnostics["screenshot"] = {
                        "path": str(screenshot_path),
                        "written": bool(screenshot is not None and not screenshot.isNull()
                                         and screenshot.save(str(screenshot_path))),
                        "size": ([int(screenshot.width()), int(screenshot.height())]
                                 if screenshot is not None and not screenshot.isNull() else None),
                    }
                except (RuntimeError, OSError) as exc:
                    diagnostics["screenshot"] = {
                        "path": str(screenshot_path), "error": type(exc).__name__}
                state_path.write_text(json.dumps(
                    diagnostics, ensure_ascii=False, indent=2, default=str) + "\n",
                    encoding="utf8")
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                diagnostics["artifact_error"] = type(exc).__name__
        raise AssertionError(
            f"Missing rendered body {expected!r}; diagnostics="
            f"{json.dumps(diagnostics, ensure_ascii=False, default=str, sort_keys=True)}")
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
def test_persisted_error_round_body_survives_real_lifecycle(
        real_bridge, monkeypatch, tmp_path, request: pytest.FixtureRequest, lifecycle):
    bridge, worker = real_bridge
    model_events: list[dict[str, object]] = []
    list_events: list[dict[str, object]] = []
    phases: list[dict[str, object]] = []
    model = bridge._turn_model
    def note_model(event, *args):
        event_row = {"event": event, "count": int(model.rowCount()),
                     "keys": [str(row.get("key", "")) for row in model._rows]}
        if event in ("about_insert", "inserted", "about_remove", "removed"):
            event_row.update(parent_valid=bool(args[0].isValid()),
                             first=int(args[1]), last=int(args[2]))
        elif event == "data_changed":
            event_row.update(first=int(args[0].row()), last=int(args[1].row()),
                             roles=[int(role) for role in args[2]])
        model_events.append(event_row)
    for signal_name, event_name in (
            ("rowsAboutToBeInserted", "about_insert"), ("rowsInserted", "inserted"),
            ("rowsAboutToBeRemoved", "about_remove"), ("rowsRemoved", "removed"),
            ("modelAboutToBeReset", "about_reset"), ("modelReset", "reset"),
            ("dataChanged", "data_changed")):
        signal = getattr(model, signal_name, None)
        if signal is not None:
            signal.connect(lambda *args, name=event_name: note_model(name, *args))
    if lifecycle == 'switch_global':
        discover(bridge, monkeypatch)
        phases.append(_capture_history_phase(bridge, "before_project_select", model_events, list_events))
        assert bridge.selectProjectContext(1)
        phases.append(_capture_history_phase(bridge, "after_project_select", model_events, list_events))
    monkeypatch.setattr(bridge, 'refreshProjectContexts', lambda: None)
    bundled_font_family()
    register_preview_cursor()
    engine = QQmlEngine()
    qml_warnings: list[str] = []

    def capture_qml_warnings(warnings):
        qml_warnings.extend(str(warning.toString()) for warning in warnings)

    engine.warnings.connect(capture_qml_warnings)
    theme = _Theme('light')
    engine.rootContext().setContextProperty('assistant', bridge)
    engine.rootContext().setContextProperty('Theme', theme)
    path = Path(__file__).parents[1] / 'coat_sim/ui/qml/AssistantChatWindow.qml'
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
    window = component.create()
    assert isinstance(window, QQuickWindow), component.errors()
    try:
        transcript = next(item for item in visual_items(window.contentItem())
                          if item.objectName() == "chatTranscript")
        count_signal = getattr(transcript, "countChanged", None)
        if count_signal is not None:
            count_signal.connect(lambda *args: list_events.append({
                "event": "count_changed", "count": _safe_property(transcript, "count"),
                "model_count": model.rowCount(),
            }))
    except (StopIteration, RuntimeError):
        phases.append({"phase": "transcript_missing_during_window_create"})
    reopened = None
    try:
        window.resize(860, 1000)
        settle_history(bridge)
        if lifecycle == 'switch_global':
            phases.append(_capture_history_phase(
                bridge, "initial_history_settled", model_events, list_events))
        if lifecycle != 'hidden_first_show':
            window.show()
            QTest.qWait(100)
        if lifecycle == 'hide_reopen':
            window.hide()
        if lifecycle != 'switch_global':
            assert bridge.selectProjectContext(0)
        assert bridge.sendMessage('你好')
        if lifecycle == 'switch_global':
            phases.append(_capture_history_phase(bridge, "after_send", model_events, list_events))
        request_id = bridge._request_id
        # No token arrives: the terminal error is the entire assistant body.
        bridge._on_worker_event({'process': worker.pid, 'payload': {
            'event': 'error', 'request_id': request_id, 'text': ERROR}})
        assert not bridge.busy
        if lifecycle == 'switch_global':
            phases.append(_capture_history_phase(
                bridge, "after_error_before_settle", model_events, list_events))
        settle_history(bridge)
        if lifecycle == 'switch_global':
            phases.append(_capture_history_phase(
                bridge, "after_error_settled", model_events, list_events))
        session = bridge.currentSessionId
        persisted = bridge._store.messages(session)
        assert any(row['content'] == '你好' and row['role'] == 'user' for row in persisted)
        assert any(row['content'] == ERROR and row['role'] == 'assistant' for row in persisted)
        if lifecycle == 'switch_global':
            assert bridge.selectProjectContext(0)
            assert bridge.projectContextIndex == 0
            # Project context changes worker authority, not the selected chat
            # session or its transcript model. This operation has no async
            # storage/projection completion to wait for.
            assert not bridge.historyLoading
            assert not bridge.historyProjectionPending
            phases.append(_capture_history_phase(
                bridge, "after_global_switch", model_events, list_events))
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
        if lifecycle == 'switch_global':
            phases.append(_capture_history_phase(
                bridge, "before_first_paint", model_events, list_events))
        diagnostic_option = request.config.getoption("qt_diagnostic_log", default=None)
        diagnostic_dir = (Path(diagnostic_option).parent / "assistant-message-lifecycle"
                          if diagnostic_option else tmp_path)
        assert_painted(window, '你好<br>', bridge, phases, model_events, list_events,
                       qml_warnings, diagnostic_dir, lifecycle)
        if lifecycle == "switch_global" and os.environ.get("COAT_ASSISTANT_LIFECYCLE_TRACE") == "1":
            print("assistant_lifecycle_qml_state " + json.dumps(
                _paint_failure_diagnostics(
                    window, bridge, '你好<br>', phases, model_events, list_events),
                ensure_ascii=False, default=str), file=sys.stderr, flush=True)
        assert_painted(window, 'stale_project_binding', bridge, phases, model_events, list_events,
                       qml_warnings, diagnostic_dir, lifecycle)
        # Read the same SQLite rows back once more without manually replacing
        # message/model/cache state or resetting invalidation flags.
        bridge._reload_messages()
        settle_history(bridge)
        assert_painted(window, 'stale_project_binding', bridge, phases, model_events, list_events,
                       qml_warnings, diagnostic_dir, lifecycle)
        assert window.grabWindow().save(str(tmp_path / (lifecycle + '.png')))
        if lifecycle == "switch_global" and os.environ.get("COAT_ASSISTANT_LIFECYCLE_TRACE") == "1":
            print("assistant_lifecycle_trace " + json.dumps(
                {"phases": phases, "model_events": model_events, "list_events": list_events,
                 "font_family": bundled_font_family()},
                ensure_ascii=False, default=str), file=sys.stderr, flush=True)
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
            reopened.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            assert not shiboken6.isValid(reopened)
