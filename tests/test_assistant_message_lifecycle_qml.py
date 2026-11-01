"""Persisted offline send/error/reopen sequences through the real chat bridge."""
import hashlib
import html
import json
import os
import sys
import time
from pathlib import Path
from typing import Protocol, cast

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
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


class _ProbeTestWindow(Protocol):
    """Methods supplied dynamically by the QML Window in the probe self-test."""

    def setActualText(self, value: str) -> None: ...
    def setEditorVisible(self, value: bool) -> None: ...
    def modelValue(self) -> str: ...
    def removeEditor(self) -> None: ...


_NATIVE_PROBE_QML = b'''import QtQuick
QtObject {
    property var selectedEditor: null
    property real savedOpacity: 1

    function inspect(root, expectedSource, expectedPlain) {
        selectedEditor = null
        let stack = [root]
        let editors = []
        while (stack.length) {
            let item = stack.pop()
            if (!item) continue
            if (typeof item.renderedSource === "string"
                    && typeof item.getText === "function"
                    && typeof item.getFormattedText === "function") {
                const source = String(item.renderedSource)
                const plain = String(item.getText(0, item.length))
                const normalizedPlain = plain.replace(/\\u2028|\\u2029|\\r\\n/g, "\\n")
                const formatted = String(item.getFormattedText(0, item.length))
                const position = item.mapToItem(root, 0, 0)
                const eligible = item.visible && item.width > 0 && item.height > 0
                    && source.indexOf(expectedSource) >= 0
                    && normalizedPlain.indexOf(expectedPlain) >= 0
                editors.push({source: source, plain: plain, formatted: formatted,
                              visible: item.visible, width: item.width, height: item.height,
                              x: position.x, y: position.y, eligible: eligible})
                if (eligible && selectedEditor === null) selectedEditor = item
            }
            const children = item.children
            if (children !== undefined) {
                for (let i = children.length - 1; i >= 0; --i) stack.push(children[i])
            }
        }
        return JSON.stringify({editors: editors, found: selectedEditor !== null})
    }

    function hideSelected() {
        if (!selectedEditor) return false
        savedOpacity = selectedEditor.opacity
        selectedEditor.opacity = 0
        return true
    }
    function restoreSelected() {
        if (!selectedEditor) return false
        selectedEditor.opacity = savedOpacity
        return true
    }
}'''


def _new_native_probe(engine):
    component = QQmlComponent(engine)
    component.setData(_NATIVE_PROBE_QML, QUrl())
    probe = component.create()
    assert probe is not None, component.errors()
    # The component owns the dynamic QML type; keep it alive with the probe.
    return component, probe


def _inspect_native_probe(probe, window, expected):
    # The source check remains exact, including HTML escaping and <br>.  The
    # second check reads the TextEdit's document, not the model/source cache.
    expected_plain = html.unescape(expected.replace("<br>", "\n"))
    return json.loads(probe.inspect(window.contentItem(), expected, expected_plain))


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


def _visual_tree_snapshot(window):
    """Record traversal decisions independently of the normal visual_items walker.

    This runs only for diagnostics: do not retain wrappers or alter the paint
    assertion. Paths identify the index in each parent's childItems() list.
    """
    nodes: list[dict[str, object]] = []
    result: dict[str, object] = {"nodes": nodes, "truncated": False}

    def visit(item, path):
        if len(nodes) >= 5000:
            result["truncated"] = True
            return
        item_type = type(item)
        row = {"path": path,
               "python_type": f"{item_type.__module__}.{item_type.__qualname__}",
               "valid": None, "qt_class": None, "object_name": None,
               "skip_reason": None}
        nodes.append(row)
        if item is None:
            row["valid"] = False
            row["skip_reason"] = "none"
            return
        try:
            row["valid"] = bool(shiboken6.isValid(item))
            if not row["valid"]:
                row["skip_reason"] = "invalid_wrapper"
                return
            if hasattr(item, "metaObject"):
                row["qt_class"] = str(item.metaObject().className())
                row["object_name"] = str(item.objectName())
            if not isinstance(item, QQuickItem):
                row["skip_reason"] = "not_qquickitem"
                return
            children = item.childItems()
            row["child_count"] = len(children)
            for index, child in enumerate(children):
                visit(child, f"{path}/{index}")
        except (RuntimeError, TypeError, AttributeError) as exc:
            row["skip_reason"] = f"inspection_error:{type(exc).__name__}"

    try:
        if window is None or not shiboken6.isValid(window):
            result["root_error"] = "invalid_window"
        else:
            visit(window.contentItem(), "contentItem")
    except (RuntimeError, TypeError, AttributeError) as exc:
        result["root_error"] = f"content_item_error:{type(exc).__name__}"
    return result


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

    result = {"tree_error": tree_error, "visual_tree_before_grab": _visual_tree_snapshot(window),
              "expected": expected,
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


def assert_painted(window, expected, probe, bridge=None, phases=(), model_events=(), list_events=(),
                   qml_warnings=(), diagnostic_dir=None, diagnostic_label=""):
    for _ in range(100):
        QTest.qWait(20)
        native_state = _inspect_native_probe(probe, window, expected)
        if native_state["found"]:
            break
    if not native_state["found"]:
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
        diagnostics["native_probe"] = native_state
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
                finally:
                    diagnostics["visual_tree_after_grab"] = _visual_tree_snapshot(window)
                state_path.write_text(json.dumps(
                    diagnostics, ensure_ascii=False, indent=2, default=str) + "\n",
                    encoding="utf8")
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                diagnostics["artifact_error"] = type(exc).__name__
        raise AssertionError(
            f"Missing rendered body {expected!r}; diagnostics="
            f"{json.dumps(diagnostics, ensure_ascii=False, default=str, sort_keys=True)}")
    QTest.qWait(60)
    native_state = _inspect_native_probe(probe, window, expected)
    assert native_state["found"], "Selected TextEdit disappeared before pixel check"
    editor = next(row for row in native_state["editors"] if row["eligible"])
    dpr = window.devicePixelRatio()
    rect = (round(editor["x"] * dpr), round(editor["y"] * dpr),
            round(editor["width"] * dpr), round(editor["height"] * dpr))
    shown = window.grabWindow().copy(*rect)
    assert probe.hideSelected(), "Selected TextEdit disappeared before pixel check"
    try:
        QTest.qWait(30)
        hidden = window.grabWindow().copy(*rect)
    finally:
        assert probe.restoreSelected(), "Selected TextEdit disappeared during pixel check"
    QTest.qWait(30)
    pixels = sum(shown.pixel(x, y) != hidden.pixel(x, y)
                 for y in range(shown.height()) for x in range(shown.width()))
    if pixels <= 20:
        raise AssertionError(f"Body {expected!r} painted only {pixels} pixels at {rect!r}")


def test_visual_tree_snapshot_reports_types_and_skips():
    class RootProvider:
        def __init__(self, root):
            self.root = root

        def contentItem(self):
            return self.root

    root = QQuickItem()
    root.setObjectName("diagnosticRoot")
    child = QQuickItem(root)
    child.setObjectName("diagnosticChild")
    rows = _visual_tree_snapshot(RootProvider(root))["nodes"]
    assert [row["object_name"] for row in rows] == ["diagnosticRoot", "diagnosticChild"]
    assert all(row["valid"] and row["qt_class"] == "QQuickItem"
               and row["python_type"].endswith(".QQuickItem")
               and row["skip_reason"] is None for row in rows)
    assert rows[0]["path"] == "contentItem" and rows[0]["child_count"] == 1
    assert rows[1]["path"] == "contentItem/0"

    non_item = QObject()
    row = _visual_tree_snapshot(RootProvider(non_item))["nodes"][0]
    assert row["valid"] and row["qt_class"] == "QObject"
    assert row["skip_reason"] == "not_qquickitem"
    invalid = QQuickItem()
    shiboken6.delete(invalid)
    row = _visual_tree_snapshot(RootProvider(invalid))["nodes"][0]
    assert row["valid"] is False and row["skip_reason"] == "invalid_wrapper"
    row = _visual_tree_snapshot(RootProvider(None))["nodes"][0]
    assert row["valid"] is False and row["skip_reason"] == "none"


def test_native_probe_requires_real_visible_editor_text_and_escaped_source(monkeypatch):
    engine = QQmlEngine()
    probe_component, probe = _new_native_probe(engine)
    component = QQmlComponent(engine)
    component.setData(b'''import QtQuick
Window {
    width: 300; height: 100
    ListModel { id: sourceModel; ListElement { body: "<tag>" } }
    function modelValue() { return sourceModel.get(0).body }
    function setActualText(value) { editorLoader.item.text = value }
    function setEditorVisible(value) { editorLoader.item.visible = value }
    function removeEditor() { editorLoader.active = false }
    Loader {
        id: editorLoader
        active: true
        sourceComponent: TextEdit {
            property string renderedSource: "&lt;tag&gt;<br>"
            textFormat: TextEdit.RichText
            text: renderedSource
            width: 280; height: 60
            font.pixelSize: 24
        }
    }
}''', QUrl())
    window = component.create()
    assert isinstance(window, QQuickWindow), component.errors()
    qml_window = cast(_ProbeTestWindow, window)
    window.show()
    QCoreApplication.processEvents()
    try:
        state = _inspect_native_probe(probe, window, "&lt;tag&gt;<br>")
        assert state["found"]
        editor = next(row for row in state["editors"] if row["eligible"])
        assert "<tag>" in editor["plain"] and "&lt;tag&gt;" in editor["formatted"]
        assert not _inspect_native_probe(probe, window, "<tag>")["found"]
        # Simulate the Python walker dropping a branch while the actual QML
        # tree remains intact. The same strict painted-pixel assertion works.
        monkeypatch.setattr(sys.modules[__name__], "visual_items", lambda _item: iter(()))
        assert_painted(window, "&lt;tag&gt;<br>", probe)

        # The cached source and model remain populated, but an empty actual
        # TextEdit document must never satisfy the body assertion.
        qml_window.setActualText("&lt;tag&gt;")
        assert not _inspect_native_probe(probe, window, "&lt;tag&gt;<br>")["found"]
        qml_window.setActualText("")
        assert qml_window.modelValue() == "<tag>"
        assert not _inspect_native_probe(probe, window, "&lt;tag&gt;<br>")["found"]
        monkeypatch.setattr(QTest, "qWait", lambda _ms: None)
        with pytest.raises(AssertionError, match="Missing rendered body"):
            assert_painted(window, "&lt;tag&gt;<br>", probe)
        qml_window.setActualText("&lt;tag&gt;<br>")
        qml_window.setEditorVisible(False)
        assert not _inspect_native_probe(probe, window, "&lt;tag&gt;<br>")["found"]
        qml_window.removeEditor()
        assert qml_window.modelValue() == "<tag>"
        assert not _inspect_native_probe(probe, window, "&lt;tag&gt;<br>")["found"]
        with pytest.raises(AssertionError, match="Missing rendered body"):
            assert_painted(window, "&lt;tag&gt;<br>", probe)
    finally:
        window.hide()
        window.deleteLater()
        probe.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_paint_failure_captures_tree_before_and_after_grab_without_passing(monkeypatch, tmp_path):
    engine = QQmlEngine()
    probe_component, probe = _new_native_probe(engine)
    window = QQuickWindow()
    child = QQuickItem(window.contentItem())
    child.setObjectName("diagnosticChild")
    window.resize(100, 100)
    window.show()
    QCoreApplication.processEvents()
    # Exercise the failure path deterministically without delaying the test.
    monkeypatch.setattr(QTest, "qWait", lambda _ms: None)
    try:
        with pytest.raises(AssertionError, match="Missing rendered body"):
            assert_painted(window, "missing", probe, diagnostic_dir=tmp_path)
        state_file = next(tmp_path.glob("assistant-message-*.json"))
        diagnostics = json.loads(state_file.read_text(encoding="utf8"))
        assert diagnostics["screenshot"]["written"]
        for key in ("visual_tree_before_grab", "visual_tree_after_grab"):
            assert not diagnostics[key]["truncated"]
            assert any(row["object_name"] == "diagnosticChild"
                       for row in diagnostics[key]["nodes"])
    finally:
        window.hide()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        probe.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


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
    probe_component, probe = _new_native_probe(engine)
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
        assert_painted(window, '你好<br>', probe, bridge, phases, model_events, list_events,
                       qml_warnings, diagnostic_dir, lifecycle)
        if lifecycle == "switch_global" and os.environ.get("COAT_ASSISTANT_LIFECYCLE_TRACE") == "1":
            print("assistant_lifecycle_qml_state " + json.dumps(
                _paint_failure_diagnostics(
                    window, bridge, '你好<br>', phases, model_events, list_events),
                ensure_ascii=False, default=str), file=sys.stderr, flush=True)
        assert_painted(window, 'stale_project_binding', probe, bridge, phases, model_events, list_events,
                       qml_warnings, diagnostic_dir, lifecycle)
        # Read the same SQLite rows back once more without manually replacing
        # message/model/cache state or resetting invalidation flags.
        bridge._reload_messages()
        settle_history(bridge)
        assert_painted(window, 'stale_project_binding', probe, bridge, phases, model_events, list_events,
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
        probe.deleteLater()
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
