r"""Targeted offline UI checks for the remaining compact-chrome pages.

Run in a fresh interpreter at each requested scale so Qt reads the scale before
constructing the application, for example:

    $env:QT_SCALE_FACTOR = '1.5'
    .\.conda\python.exe -c "import runpy; t=runpy.run_path('tests/test_remaining_windows_compact_chrome_ui.py'); t['test_remaining_window_status_contracts'](); t['test_popup_header_render_and_keyboard']()"
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import urllib.request
import unittest
from pathlib import Path
from typing import NotRequired, TypedDict
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
os.environ.setdefault("PYTHONUTF8", "1")

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
QML_DIR = ROOT / "coat_sim" / "ui" / "qml"
DPR_TAG = os.environ.get("QT_SCALE_FACTOR", "1").replace(".", "_")
EVIDENCE = ROOT / "build" / f"qa_ui_remaining_windows_final_20261012_dpr_{DPR_TAG}"
sys.path.insert(0, str(TESTS))

# Guard QSettings and QML networking before application modules or QApplication.
import qt_test_guard  # noqa: E402,F401

from PySide6.QtCore import (  # noqa: E402
    QAbstractListModel, QModelIndex, QObject, Property, QCoreApplication, QPoint,
    QPointF, QSettings, Qt, QUrl, Signal, Slot, QEvent,
)
from PySide6.QtGui import QAccessible, QFont, QFontDatabase, QImage  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression  # noqa: E402
from PySide6.QtQuick import QQuickItem, QQuickWindow  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from coat_sim.ui.theme_tokens import build_theme_snapshot  # noqa: E402
from coat_sim.ui.source_materials import MaterialLibraryBridge  # noqa: E402
from coat_sim.ui.preview_cursor import register_preview_cursor  # noqa: E402


_HARNESS = r'''import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Window {
    id: qaWindow
    objectName: "qaWindow"
    width: 560
    height: 320
    visible: true
    color: Theme.snapshot.colorWindow
    property string qaStatusText: ""
    property string qaSeverity: "info"
    property string qaDetail: ""

    Dialog {
        id: qaDialog
        objectName: "qaPopupDialog"
        parent: Overlay.overlay
        x: 8
        y: 8
        width: parent ? parent.width - 16 : 520
        height: parent ? parent.height - 16 : 280
        modal: false
        visible: true
        header: ThemeControls.ThemePopupHeader {
            objectName: "qaPopupHeader"
            title: "材料库"
            statusText: qaWindow.qaStatusText
            statusSeverity: qaWindow.qaSeverity
            statusDetail: qaWindow.qaDetail
            tooltipBoundsItem: Overlay.overlay
        }
        contentItem: ColumnLayout {
            Button { objectName: "qaFirstTabStop"; text: "动作一" }
            Button { objectName: "qaLastTabStop"; text: "动作二" }
        }
    }
}
'''


_MATERIAL_HARNESS = r'''import QtQuick
import QtQuick.Controls
import QtQuick.Window
import "../coat_sim/ui/qml" as CoatUi

CoatUi.AppWindow {
    id: host
    objectName: "qaMaterialHost"
    width: 880
    height: 640
    visible: true
    title: "材料状态测试"
    property bool hostedMode: false
    statusText: hostedMode
        ? (materialLibrary.valid
           ? ((materialLibrary.dirty ? "● 有未保存修改 · " : "") + materialLibrary.status)
           : materialLibrary.error)
        : ""
    statusSeverity: hostedMode && !materialLibrary.valid ? "error"
        : hostedMode && materialLibrary.loading ? "busy" : "info"
    statusDetail: statusText

    CoatUi.MaterialLibraryDialog {
        id: materialDialog
        objectName: "qaMaterialDialog"
        library: materialLibrary
        modal: false
        visible: true
        hostedInWindowChrome: host.hostedMode
    }
}
'''


_QA_NETWORK_FACTORIES: list[object] = []
_QA_ENGINE_INSTALLS: list[dict[str, object]] = []


def _install_network_factory(engine: QQmlEngine):
    factory = qt_test_guard.NoNetworkAccessManagerFactory()
    engine.setNetworkAccessManagerFactory(factory)
    _QA_NETWORK_FACTORIES.append(factory)
    _QA_ENGINE_INSTALLS.append({"factory": factory, "installed": True})
    return factory


class _Theme(QObject):
    snapshotChanged = Signal()

    def __init__(self, mode: str):
        super().__init__()
        self._snapshot = build_theme_snapshot(mode)

    @Property("QVariantMap", notify=snapshotChanged)
    def snapshot(self):
        return self._snapshot

    def set_mode(self, mode: str) -> None:
        self._snapshot = build_theme_snapshot(mode)
        self.snapshotChanged.emit()

    def set_small_control_height(self, height: float) -> None:
        self._snapshot = dict(self._snapshot)
        self._snapshot["controlHeightSm"] = height
        self.snapshotChanged.emit()


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class TurnModel(QAbstractListModel):
    """Empty ConversationModel-shaped fixture for the real chat window."""

    TurnRole = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, page_size: int, history_limit: int):
        super().__init__()
        # Retain the fixture parameters so the page's test setup is explicit;
        # no conversation rows are needed for the owner-status regression.
        self.page_size = page_size
        self.history_limit = history_limit

    def roleNames(self):
        return {self.TurnRole: b"turnData"}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 0


class AssistantStub(QObject):
    """Small current-contract surface needed to instantiate AssistantChatWindow."""

    transcriptChanged = Signal()
    historyProjectionChanged = Signal()
    historyProjectionBatchAboutToInsert = Signal(int)
    historyProjectionBatchInserted = Signal()
    streamingChanged = Signal()
    conversationChanged = Signal()
    sessionsRequested = Signal()
    statisticsRequested = Signal()
    contextChanged = Signal()
    configurationChanged = Signal()
    queueChanged = Signal()
    attachmentRequested = Signal()
    applicationRequested = Signal("QVariantMap")
    previewRequested = Signal("QVariantMap")
    draftRestored = Signal(str)

    def __init__(self, turn_model: TurnModel):
        super().__init__()
        self._turn_model = turn_model

    @Property("QVariantList", constant=True)
    def attachments(self): return []

    @Property(str, constant=True)
    def attachmentDirectory(self): return ""

    @Property(bool, constant=True)
    def busy(self): return False

    @Property(bool, constant=True)
    def cancelling(self): return False

    @Property(bool, constant=True)
    def compacting(self): return False

    @Property(str, constant=True)
    def compactionStatus(self): return ""

    @Property("QVariantList", constant=True)
    def completions(self): return []

    @Property("QVariantMap", constant=True)
    def contextUsage(self):
        return {"used": 0, "ratio": 0, "known": False, "label": "", "capacity": 0,
                "total_capacity": 0, "output_reserved": 0, "compaction_trigger": 0,
                "compaction_target": 0, "messages": 0, "tools": 0, "instructions": 0,
                "media": 0, "overhead": 0, "actual": False}

    @Property(str, constant=True)
    def conversationText(self): return ""

    @Property(str, constant=True)
    def currentSessionId(self): return ""

    @Property(bool, constant=True)
    def hasOlderHistory(self): return False

    @Property(str, constant=True)
    def historyError(self): return ""

    @Property(bool, constant=True)
    def historyLoading(self): return False

    @Property(bool, constant=True)
    def historyProjectionPending(self): return False

    @Property("QVariantList", constant=True)
    def otherModelTokenUsage(self): return []

    @Property("QVariantList", constant=True)
    def pendingMessages(self): return []

    @Property(int, constant=True)
    def queuedCount(self): return 0

    @Property("QVariantList", constant=True)
    def reasoningOptions(self): return ["auto", "none", "minimal", "low", "medium", "high"]

    @Property(str, constant=True)
    def reasoningStrength(self): return "auto"

    @Property(bool, constant=True)
    def reasoningSupported(self): return False

    @Property("QVariantList", constant=True)
    def sessions(self): return []

    @Property(str, constant=True)
    def statusMessage(self): return ""

    @Property(str, constant=True)
    def taskMode(self): return "chat"

    @Property(str, constant=True)
    def tokenSummary(self): return ""

    @Property(QObject, constant=True)
    def turnModel(self): return self._turn_model

    @Property(int, constant=True)
    def wheelScrollLines(self): return 3

    @Slot(str, result=int)
    def visibleTurnIndex(self, _key: str) -> int: return -1

    @Slot(str, result=bool)
    def isPreviewableImageLink(self, _link: str) -> bool: return False

    @Slot(str)
    def refreshAppearance(self, _color: str) -> None: pass

    @Slot(int, result=str)
    def tokenText(self, value: int) -> str: return str(value)


class StatusAssistant(AssistantStub):
    """Status values are constant to exercise QML context-property rebinding."""

    def __init__(self, turn_model: TurnModel, message: str, busy: bool):
        super().__init__(turn_model)
        self._qa_status_message = message
        self._qa_busy = busy

    @Property(str, constant=True)
    def statusMessage(self) -> str: return self._qa_status_message

    @Property(bool, constant=True)
    def busy(self) -> bool: return self._qa_busy


def _rects_intersect(a: list[float], b: list[float]) -> bool:
    return (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
            and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])


def _rect_inside(inner: list[float], outer: list[float], tolerance: float = 1.0) -> bool:
    return (inner[0] >= outer[0] - tolerance
            and inner[1] >= outer[1] - tolerance
            and inner[0] + inner[2] <= outer[0] + outer[2] + tolerance
            and inner[1] + inner[3] <= outer[1] + outer[3] + tolerance)


def _isolate_settings() -> None:
    settings_root = EVIDENCE / "isolated_qsettings"
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                      str(settings_root / "user"))
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.SystemScope,
                      str(settings_root / "system"))
    QCoreApplication.setOrganizationName("CoatSimRemainingWindowsQA")
    QCoreApplication.setApplicationName("CompactChrome")


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_remaining_window_status_contracts() -> dict[str, str]:
    """Check source ownership and ensure only real statuses use shared chrome."""
    assistant = _source("coat_sim/ui/qml/AssistantChatWindow.qml")
    _assert("anchors.topMargin: chatChrome.barHeight" in assistant,
            "chat page top margin must follow its standalone WindowChrome")
    _assert(assistant.count("anchors.topMargin: chatChrome.barHeight") == 2,
            "both chat page and session drawer must use the public chrome height")
    _assert("statusText: assistant.statusMessage === \"准备就绪。\" ? \"\" : assistant.statusMessage" in assistant,
            "chat status must remain bound to the assistant-owned status message")
    _assert("statusSeverity: assistant.busy ? \"busy\" : \"info\"" in assistant,
            "chat severity must follow the typed busy state")
    _assert("anchors.topMargin: 40" not in assistant,
            "chat page and drawer must not retain the old fixed top gap")
    _assert("visible: !assistant.busy && assistant.statusMessage" not in assistant,
            "the old bottom-only status label must not duplicate the chrome status")
    _assert("onClosing: function(close) { close.accepted = false; cancelPendingScrollRestore(); hide() }" in assistant,
            "chat hide/close lifecycle must remain intact")

    launcher = _source("coat_sim/ui/qml/LauncherWindow.qml")
    _assert("statusText: errorToast.visible ? errorText.text : \"\"" in launcher,
            "launcher status must mirror the existing visible launch error")
    _assert("statusSeverity: errorToast.visible ? \"error\" : \"info\"" in launcher,
            "launcher error severity must follow its existing toast visibility")
    _assert("visible: opacity > 0" in launcher and "interval: 15000" in launcher
            and "onTriggered: errorToast.opacity = 0" in launcher,
            "the existing toast visibility and 15 second expiry must remain the owner")
    _assert("panelY: Math.round(0.08 * height)" in launcher,
            "launcher artwork/panel composition must remain unchanged")

    about = _source("coat_sim/ui/qml/AboutWindow.qml")
    _assert("y: 0" in about and "y: hero.height - 30" in about,
            "About window full-bleed artwork offsets must remain unchanged")

    host = _source("coat_sim/ui/qml/MaterialLibraryHost.qml")
    dialog = _source("coat_sim/ui/qml/MaterialLibraryDialog.qml")
    _assert("statusText: editorInstance === null" in host and "statusDetail: statusText" in host,
            "host status must derive from its loaded bridge without a second cache")
    _assert("!bridge.valid ? \"error\"" in host and "bridge.loading ? \"busy\"" in host,
            "host status severity must prioritize invalid state then loading")
    _assert("ThemeControls.ThemePopupHeader" in dialog
            and "tooltipBoundsItem: Overlay.overlay" in dialog,
            "standalone material dialog must use its bounded popup header")
    _assert("statusText: dialog.hostedInWindowChrome ? \"\"" in dialog,
            "hosted material mode must leave status presentation to the host chrome")
    _assert("objectName: \"materialLibraryInlineStatus\"" not in dialog,
            "the duplicate bottom status placeholder must be removed")
    _assert("opticalSetCurveStatus" in dialog and "SourceValueField" in dialog,
            "field validation and local optical-curve statuses must remain in the body")

    for path in ("coat_sim/ui/qml/ThemePageDialog.qml",):
        _assert("ThemePopupHeader" not in _source(path),
                "the stateless generic dialog base must not gain business status")

    return {
        "assistant": hashlib.sha256(assistant.encode("utf-8")).hexdigest(),
        "launcher": hashlib.sha256(launcher.encode("utf-8")).hexdigest(),
        "material_host": hashlib.sha256(host.encode("utf-8")).hexdigest(),
        "material_dialog": hashlib.sha256(dialog.encode("utf-8")).hexdigest(),
    }


def _install_capture_fonts(app: QApplication) -> dict[str, object]:
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    loaded: list[dict[str, object]] = []
    for filename in ("segoeui.ttf", "msyh.ttc", "msyhbd.ttc"):
        path = fonts / filename
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        loaded.append({"file": filename, "id": font_id, "families": list(families)})
    families = [family for row in loaded for family in row["families"]]
    preferred = next((name for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
                      if name in families), None)
    if preferred:
        app.setFont(QFont(preferred))
    return {"loaded": loaded, "applicationFont": preferred}


def _find(root: QObject, name: str):
    matches = root.findChildren(QObject, name)
    return matches[0] if matches else None


def _find_visual_item(root: QQuickItem, name: str) -> QQuickItem | None:
    pending = [root]
    while pending:
        item = pending.pop()
        if item.objectName() == name:
            return item
        pending.extend(item.childItems())
    return None


def _component(engine: QQmlEngine, source: str, base_url: QUrl | None = None) -> QQmlComponent:
    component = QQmlComponent(engine)
    component.setData(
        source.encode("utf-8"),
        base_url or QUrl.fromLocalFile(str(QML_DIR / "qa_popup_header.qml")),
    )
    _assert(component.status() == QQmlComponent.Status.Ready,
            "QML did not compile: " + "\n".join(error.toString() for error in component.errors()))
    return component


def _evaluate_qml(engine: QQmlEngine, scope: QObject, expression: str):
    context = QQmlEngine.contextForObject(scope)
    _assert(context is not None, f"no QML context for expression: {expression}")
    value = QQmlExpression(context, scope, expression)
    result = value.evaluate()
    _assert(not value.hasError(),
            f"QML expression failed ({expression}): {value.error().toString()}")
    # PySide6 returns (value, isUndefined) for QQmlExpression.evaluate().
    return result[0] if isinstance(result, tuple) and len(result) == 2 else result


def _wait_for_popup_header_layout(header, title, status, timeout_ms: int = 2000) -> None:
    """Wait for Qt Quick's scheduled layout pass before measuring geometry.

    Changing a child's visibility invalidates RowLayout asynchronously. One
    processEvents() call can therefore expose the previous title width while
    the newly visible status still has its unpositioned initial coordinates.
    Keep the geometry contract intact, and give the actual layout a bounded
    opportunity to reach it before the assertions below inspect its values.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        QCoreApplication.processEvents()
        title_origin = title.mapToItem(header, 0, 0)
        status_origin = status.mapToItem(header, 0, 0)
        if (bool(status.property("visible"))
                and float(status.property("width")) > 0
                and title_origin.x() + float(title.property("width")) <= status_origin.x() + 1
                and status_origin.x() + float(status.property("width"))
                <= float(header.property("width")) + 1):
            return
        if time.monotonic() >= deadline:
            raise AssertionError("popup header layout did not settle within the bounded deadline")
        QTest.qWait(10)


class _MaterialActionGeometry(TypedDict):
    rect: list[float]
    visible: bool
    enabled: bool
    clippedAncestors: list[dict[str, object]]
    textDarkPixels: NotRequired[int]


def test_material_library_standalone_and_hosted_status_modes() -> dict[str, object]:
    """Exercise real dialog bindings against an isolated, temporary bridge."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    _isolate_settings()
    app = QApplication.instance() or QApplication([])
    fonts = _install_capture_fonts(app)
    app.setQuitOnLastWindowClosed(False)
    theme = _Theme("light")
    host = dialog = engine = library = None
    with tempfile.TemporaryDirectory(prefix="material-library-", dir=str(EVIDENCE)) as temporary:
        library = MaterialLibraryBridge(directory=Path(temporary) / "materials")
        engine = QQmlEngine()
        network_factory = _install_network_factory(engine)
        engine.rootContext().setContextProperty("Theme", theme)
        engine.rootContext().setContextProperty("materialLibrary", library)
        # Use the tests directory as the import base for the project's real QML tree.
        component = _component(
            engine,
            _MATERIAL_HARNESS,
            QUrl.fromLocalFile(str(ROOT / "tests" / "_remaining_windows_material_host.qml")),
        )
        try:
            with (
                patch("socket.create_connection",
                      side_effect=AssertionError("Python network disabled")),
                patch("socket.socket.connect",
                      side_effect=AssertionError("Python network disabled")),
                patch("urllib.request.urlopen",
                      side_effect=AssertionError("Python network disabled")),
            ):
                _assert(component.status() == QQmlComponent.Status.Ready,
                        "material harness did not compile: "
                        + "\n".join(error.toString() for error in component.errors()))
                host = component.create()
                _assert(host is not None,
                        "material harness did not create: "
                        + "\n".join(error.toString() for error in component.errors()))
                assert isinstance(host, QQuickWindow), "material harness must create a real window"
                QQmlEngine.setObjectOwnership(host, QQmlEngine.ObjectOwnership.CppOwnership)
                host.show()
                dialog = _find(host, "qaMaterialDialog")
                _assert(dialog is not None, "real MaterialLibraryDialog was not instantiated")

                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and (library.loading or not library.valid):
                    QCoreApplication.processEvents()
                    QTest.qWait(25)
                _assert(library.valid and not library.loading,
                        "isolated temporary material catalog did not reach its loaded state: "
                        f"valid={library.valid} loading={library.loading} error={library.error}")

                status = _find(dialog, "themePopupHeaderStatus")
                status_text = _find(dialog, "themePopupHeaderStatusText")
                _assert(status is not None and status_text is not None,
                        "material popup header status controls are missing")
                library.setField("description", "isolated UI fixture draft; never saved")
                QCoreApplication.processEvents()
                _assert(bool(library.dirty), "fixture edit must remain an in-memory draft")
                _assert(bool(status.property("visible")),
                        "standalone material dialog must show its bridge-owned status")
                _assert("有未保存修改" in str(status_text.property("text")),
                        "standalone status must preserve the library dirty prefix")

                standalone_status = str(status_text.property("text"))
                standalone_image = _screenshot(host, "material_standalone_status.png")
                header = _find(dialog, "themePopupHeaderTitleText")
                _assert(header is not None, "material popup title label is missing")
                header_item = _find(dialog, "materialLibraryPopupHeader")
                _assert(header_item is not None, "material popup header is missing")
                header_origin = header_item.mapToItem(host.contentItem(), 0, 0)
                title_origin = header.mapToItem(header_item, 0, 0)
                status_origin = status.mapToItem(header_item, 0, 0)
                close_button = _find_visual_item(host.contentItem(), "windowChromeCloseButton")
                _assert(close_button is not None, "host WindowChrome close button is missing")
                close_origin = close_button.mapToItem(host.contentItem(), 0, 0)
                close_rect = [float(close_origin.x()), float(close_origin.y()),
                              float(close_button.property("width")),
                              float(close_button.property("height"))]
                _assert(float(dialog.property("width")) > 0 and float(dialog.property("height")) > 0,
                        "material popup must have positive geometry in both presentation modes")
                _assert(title_origin.x() + float(header.property("width"))
                        <= status_origin.x() + 1,
                        "standalone material title and status must not overlap")
                _assert(status_origin.x() + float(status.property("width"))
                        <= float(header_item.property("width")) + 1,
                        "standalone material status target must stay inside its header")
                status_rect = [float(status_origin.x() + header_origin.x()),
                               float(status_origin.y() + header_origin.y()),
                               float(status.property("width")), float(status.property("height"))]
                _assert(not _rects_intersect(status_rect, close_rect),
                        "standalone material status hit target overlaps host close button: "
                        f"status={status_rect} close={close_rect}")
                tooltip = _find(dialog, "themePopupHeaderStatusToolTip")
                tooltip_bounds = header_item.property("tooltipBoundsItem")
                _assert(tooltip is not None and tooltip_bounds is not None,
                        "material status tooltip or its bounds item is missing")
                status_center = status.mapToItem(
                    host.contentItem(),
                    float(status.property("width")) / 2,
                    float(status.property("height")) / 2,
                )
                status_point = QPoint(round(status_center.x()), round(status_center.y()))
                QTest.mouseMove(host, status_point)
                QCoreApplication.processEvents()
                QTest.qWait(40)
                bounds_origin = tooltip_bounds.mapToItem(host.contentItem(), 0, 0)
                tooltip_rect = [float(bounds_origin.x() + tooltip.property("x")),
                                float(bounds_origin.y() + tooltip.property("y")),
                                float(tooltip.property("width")), float(tooltip.property("height"))]
                _assert(bool(tooltip.property("visible")),
                        "standalone material full-status tooltip did not become visible")
                _assert(not _rects_intersect(tooltip_rect, close_rect),
                        "standalone material status tooltip overlaps host close button: "
                        f"tooltip={tooltip_rect} close={close_rect}")
                tooltip_image = _screenshot(host, "material_standalone_status_tooltip.png")
                QTest.mouseMove(host, QPoint(4, round(host.height()) - 4))
                QCoreApplication.processEvents()
                standalone_geometry = {
                    "dialog": [float(dialog.property("x")), float(dialog.property("y")),
                               float(dialog.property("width")), float(dialog.property("height"))],
                    "headerInWindow": [float(header_origin.x()), float(header_origin.y()),
                                       float(header_item.property("width")),
                                       float(header_item.property("height"))],
                    "windowChromeTopMargin": float(host.property("contentTopMargin")),
                    "windowChromeClose": close_rect,
                    "title": [float(title_origin.x()), float(title_origin.y()),
                              float(header.property("width")), float(header.property("height"))],
                    "status": [float(status_origin.x()), float(status_origin.y()),
                               float(status.property("width")), float(status.property("height"))],
                    "statusInWindow": status_rect,
                    "statusTooltipInWindow": tooltip_rect,
                    "statusTextWidth": float(status_text.property("width")),
                }
                baseline_control_height = float(theme.snapshot["controlHeightSm"])
                theme.set_small_control_height(48.0)
                QCoreApplication.processEvents()
                QTest.qWait(50)
                high_inset = float(host.property("contentTopMargin"))
                _assert(high_inset > 40,
                        f"large Theme control size must exercise inset>40, got {high_inset}")
                high_dialog = [float(dialog.property("x")), float(dialog.property("y")),
                               float(dialog.property("width")), float(dialog.property("height"))]
                high_header_origin = header_item.mapToItem(host.contentItem(), 0, 0)
                high_title_origin = header.mapToItem(header_item, 0, 0)
                high_status_origin = status.mapToItem(header_item, 0, 0)
                _assert(high_title_origin.x() + float(header.property("width"))
                        <= high_status_origin.x() + 1,
                        "large-theme material title and status must not overlap")
                high_status_rect = [float(high_status_origin.x() + high_header_origin.x()),
                                    float(high_status_origin.y() + high_header_origin.y()),
                                    float(status.property("width")), float(status.property("height"))]
                high_close_origin = close_button.mapToItem(host.contentItem(), 0, 0)
                high_close_rect = [float(high_close_origin.x()), float(high_close_origin.y()),
                                   float(close_button.property("width")),
                                   float(close_button.property("height"))]
                _assert(high_dialog[1] >= high_inset - 1,
                        "large-theme standalone dialog must start below its chrome inset: "
                        f"dialog={high_dialog} inset={high_inset}")
                _assert(high_dialog[1] + high_dialog[3] <= float(host.height()) - 3,
                        "large-theme standalone dialog must retain its bottom margin: "
                        f"dialog={high_dialog} windowHeight={host.height()}")
                _assert(not _rects_intersect(high_status_rect, high_close_rect),
                        "large-theme material status target must not overlap window close: "
                        f"status={high_status_rect} close={high_close_rect}")
                high_header_rect = [float(high_header_origin.x()), float(high_header_origin.y()),
                                    float(header_item.property("width")),
                                    float(header_item.property("height"))]
                high_title_rect = [float(high_title_origin.x()), float(high_title_origin.y()),
                                   float(header.property("width")),
                                   float(header.property("height"))]
                high_status_center = status.mapToItem(
                    host.contentItem(),
                    float(status.property("width")) / 2,
                    float(status.property("height")) / 2,
                )
                QTest.mouseMove(host, QPoint(round(high_status_center.x()),
                                             round(high_status_center.y())))
                QCoreApplication.processEvents()
                QTest.qWait(40)
                high_bounds_origin = tooltip_bounds.mapToItem(host.contentItem(), 0, 0)
                high_tooltip_rect = [float(high_bounds_origin.x() + tooltip.property("x")),
                                     float(high_bounds_origin.y() + tooltip.property("y")),
                                     float(tooltip.property("width")),
                                     float(tooltip.property("height"))]
                _assert(bool(tooltip.property("visible")),
                        "large-theme material status tooltip must remain reachable")
                _assert(not _rects_intersect(high_tooltip_rect, high_close_rect),
                        "large-theme status tooltip must not overlap the window close button: "
                        f"tooltip={high_tooltip_rect} close={high_close_rect}")

                window_rect = [0.0, 0.0, float(host.width()), float(host.height())]
                dialog_rect = [float(dialog.property("x")), float(dialog.property("y")),
                               float(dialog.property("width")), float(dialog.property("height"))]
                toolbar = _find(dialog, "materialLibraryActionToolbar")
                _assert(toolbar is not None and toolbar.isVisible(),
                        "material top operation bar must be rendered")
                toolbar_origin = toolbar.mapToItem(host.contentItem(), 0, 0)
                toolbar_rect = [float(toolbar_origin.x()), float(toolbar_origin.y()),
                                float(toolbar.width()), float(toolbar.height())]
                _assert(_rect_inside(toolbar_rect, window_rect)
                        and _rect_inside(toolbar_rect, dialog_rect),
                        f"material top operation bar must stay inside its host: {toolbar_rect}")
                toolbar_actions: dict[str, _MaterialActionGeometry] = {}
                toolbar_clipped_ancestors: list[dict[str, object]] = []
                for object_name in ("newMaterialButton", "saveMaterialButton",
                                    "importMaterialButton", "exportLibraryButton",
                                    "exportSingleMaterialButton"):
                    button = _find(dialog, object_name)
                    _assert(button is not None,
                            f"material top action is missing: {object_name}")
                    _assert(button.parentItem() is toolbar,
                            f"material operation must share the top bar: {object_name}")
                    button_origin = button.mapToItem(host.contentItem(), 0, 0)
                    button_rect = [float(button_origin.x()), float(button_origin.y()),
                                   float(button.width()), float(button.height())]
                    _assert(button.isVisible() and bool(button.property("visible")),
                            f"material top action is not visibly rendered: {object_name}")
                    if object_name != "exportSingleMaterialButton":
                        _assert(bool(button.property("enabled")),
                                f"isolated material top action is unexpectedly disabled: {object_name}")
                    _assert(_rect_inside(button_rect, toolbar_rect)
                            and _rect_inside(button_rect, dialog_rect),
                            f"material top action falls outside the operation bar: "
                            f"{object_name}={button_rect} toolbar={toolbar_rect}")
                    action_clips: list[dict[str, object]] = []
                    parent_item = button.parentItem()
                    while parent_item is not None:
                        parent_origin = parent_item.mapToItem(host.contentItem(), 0, 0)
                        parent_rect = [float(parent_origin.x()), float(parent_origin.y()),
                                       float(parent_item.width()), float(parent_item.height())]
                        if bool(parent_item.property("clip")):
                            _assert(_rect_inside(button_rect, parent_rect),
                                    f"material top action is clipped by an ancestor: "
                                    f"{object_name}={button_rect} clip={parent_rect}")
                            action_clips.append({"type": str(parent_item.metaObject().className()),
                                                 "rect": parent_rect})
                        parent_item = parent_item.parentItem()
                    toolbar_clipped_ancestors.extend(action_clips)
                    toolbar_actions[object_name] = {
                        "rect": button_rect, "visible": bool(button.isVisible()),
                        "enabled": bool(button.property("enabled")),
                        "clippedAncestors": action_clips,
                    }
                role_filter = _find(dialog, "materialRoleFilter")
                refresh = _find(dialog, "refreshMaterialLibraryButton")
                _assert(role_filter is not None and refresh is not None
                        and role_filter.parentItem() is refresh.parentItem(),
                        "material refresh must stay beside its category filter")
                _assert(_find(dialog, "materialLibraryCloseKeepDraftButton") is None,
                        "the removed keep-draft close action must not remain")
                _assert(_find(dialog, "copyMaterialButton") is None,
                        "the removed copy-as-user action must not remain")
                use_button = _find(dialog, "useMaterialButton")
                _assert(use_button is not None and use_button.isVisible(),
                        "the source/substrate use-selected-material action must remain")
                use_parent = use_button.parentItem()
                while use_parent is not None and "DialogButtonBox" not in str(use_parent.metaObject().className()):
                    use_parent = use_parent.parentItem()
                _assert(use_parent is not None,
                        "the preserved material handoff action must keep its footer owner")
                QTest.mouseMove(host, QPoint(4, round(host.height()) - 4))
                _find(dialog, "newMaterialButton").forceActiveFocus(Qt.FocusReason.TabFocusReason)
                QCoreApplication.processEvents()
                QTest.qWait(50)
                high_density_image = _screenshot(host, "material_standalone_high_density.png")
                captured = QImage(str(high_density_image["path"]))
                _assert(not captured.isNull(), "high-density material screenshot could not be reloaded")
                pixel_scale = float(host.devicePixelRatio())
                _assert(captured.width() == round(float(host.width()) * pixel_scale)
                        and captured.height() == round(float(host.height()) * pixel_scale),
                        "high-density material screenshot pixel size does not match its DPR")
                for object_name, action in toolbar_actions.items():
                    if not action["enabled"]:
                        continue
                    x, y, width, height = action["rect"]
                    x0 = max(0, round((x + width * 0.2) * pixel_scale))
                    x1 = min(captured.width(), round((x + width * 0.8) * pixel_scale))
                    y0 = max(0, round((y + height * 0.15) * pixel_scale))
                    y1 = min(captured.height(), round((y + height * 0.85) * pixel_scale))
                    dark_pixels = 0
                    for pixel_y in range(y0, y1):
                        for pixel_x in range(x0, x1):
                            color = captured.pixelColor(pixel_x, pixel_y)
                            if max(color.red(), color.green(), color.blue()) < 150:
                                dark_pixels += 1
                    _assert(dark_pixels > 0,
                            f"material top action label has no visible dark text pixels: {object_name}")
                    action["textDarkPixels"] = dark_pixels
                high_toolbar_geometry = {
                    "rect": toolbar_rect,
                    "visible": bool(toolbar.isVisible()),
                    "clippedAncestors": toolbar_clipped_ancestors,
                    "actions": toolbar_actions,
                    "requiresScroll": False,
                    "imageDpr": pixel_scale,
                    "imagePixelSize": [captured.width(), captured.height()],
                }
                QTest.mouseMove(host, QPoint(4, round(host.height()) - 4))
                theme.set_small_control_height(baseline_control_height)
                QCoreApplication.processEvents()
                QTest.qWait(30)
                host.setProperty("hostedMode", True)
                QCoreApplication.processEvents()
                QTest.qWait(30)
                _assert(not bool(status.property("visible")),
                        "hosted material dialog must hide its inline duplicate status")
                _assert("有未保存修改" in str(host.property("statusText")),
                        "host chrome must receive the same bridge-owned draft status")
                _assert(str(host.property("statusSeverity")) == "info",
                        "valid loaded material draft must keep informational severity")
                host_image = _screenshot(host, "material_hosted_status.png")
                return {
                    "standaloneStatus": standalone_status,
                    "hostedStatus": str(host.property("statusText")),
                    "hostedSeverity": str(host.property("statusSeverity")),
                    "bridgeValid": bool(library.valid),
                    "bridgeDirtyInMemory": bool(library.dirty),
                    "temporaryDirectory": str(Path(temporary).resolve()),
                    "networkBlocked": list(network_factory.blocked_urls),
                    "fonts": fonts,
                    "screenshots": [standalone_image, tooltip_image, high_density_image, host_image],
                    "headerWidth": float(header_item.property("width")),
                    "titleWidth": float(header.property("width")),
                    "statusWidth": float(status.property("width")),
                    "statusTextWidth": float(status_text.property("width")),
                    "headerGeometry": {
                        "standalone": standalone_geometry,
                        "standaloneHighDensity": {
                            "dialog": high_dialog,
                            "contentTopMargin": high_inset,
                            "windowHeight": float(host.height()),
                            "headerInWindow": high_header_rect,
                            "title": high_title_rect,
                            "statusInWindow": high_status_rect,
                            "close": high_close_rect,
                            "tooltipInWindow": high_tooltip_rect,
                            "operationBar": high_toolbar_geometry,
                        },
                    },
                    "devicePixelRatio": float(host.devicePixelRatio()),
                    "pixelWindowSize": [round(float(host.width()) * float(host.devicePixelRatio())),
                                        round(float(host.height()) * float(host.devicePixelRatio()))],
                }
        finally:
            if dialog is not None:
                dialog.close()
            if host is not None:
                host.close()
                host.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            if engine is not None:
                engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


def _screenshot(window: QQuickWindow, name: str) -> dict[str, object]:
    image = window.grabWindow()
    _assert(not image.isNull(), f"rendered image is empty: {name}")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / name
    _assert(image.save(str(path)), f"could not save screenshot: {path}")
    return {"path": str(path), "width": image.width(), "height": image.height()}


def test_popup_header_render_and_keyboard() -> dict[str, object]:
    _isolate_settings()
    app = QApplication.instance() or QApplication([])
    fonts = _install_capture_fonts(app)
    mode = "light"
    theme = _Theme(mode)
    engine = QQmlEngine()
    network_factory = _install_network_factory(engine)
    engine.rootContext().setContextProperty("Theme", theme)
    component = _component(engine, _HARNESS)
    window = component.create()
    _assert(window is not None,
            "popup header harness did not create: "
            + "\n".join(error.toString() for error in component.errors()))
    QQmlEngine.setObjectOwnership(window, QQmlEngine.ObjectOwnership.CppOwnership)
    results: dict[str, object] = {
        "devicePixelRatio": float(window.devicePixelRatio()),
        "fonts": fonts,
        "screenshots": [],
    }
    try:
        window.show()
        QCoreApplication.processEvents()
        QTest.qWait(100)
        QCoreApplication.processEvents()

        status = _find(window, "themePopupHeaderStatus")
        status_text = _find(window, "themePopupHeaderStatusText")
        header_item = _find(window, "qaPopupHeader")
        title_item = _find(window, "themePopupHeaderTitleText")
        tooltip = _find(window, "themePopupHeaderStatusToolTip")
        scroll = _find(window, "themePopupHeaderStatusTipScroll")
        first_tab = _find(window, "qaFirstTabStop")
        last_tab = _find(window, "qaLastTabStop")
        _assert(all(item is not None for item in (status, status_text, header_item, title_item, tooltip, scroll,
                                                  first_tab, last_tab)),
                "popup header rendered controls are incomplete")

        long_error = "材料库读取失败。请检查本地材料目录和文件权限。" + (
            " 详细错误说明及可重试条件。" * 45
        )
        window.setProperty("qaStatusText", "材料库读取失败")
        window.setProperty("qaSeverity", "error")
        window.setProperty("qaDetail", long_error)
        _wait_for_popup_header_layout(header_item, title_item, status)
        _assert(bool(status.property("visible")), "business status must render in popup header")
        _assert(float(status.property("height")) <= 48,
                "long status must not expand the title row")
        title_origin = title_item.mapToItem(header_item, 0, 0)
        status_origin = status.mapToItem(header_item, 0, 0)
        normal_geometry = {
            "header": [float(header_item.property("width")), float(header_item.property("height"))],
            "title": [float(title_origin.x()), float(title_origin.y()),
                      float(title_item.property("width")), float(title_item.property("height"))],
            "status": [float(status_origin.x()), float(status_origin.y()),
                       float(status.property("width")), float(status.property("height"))],
            "statusTextWidth": float(status_text.property("width")),
        }
        _assert(title_origin.x() + float(title_item.property("width"))
                <= status_origin.x() + 1,
                "visible popup header title and status target must not overlap: "
                f"title={normal_geometry['title']} status={normal_geometry['status']} "
                f"header={normal_geometry['header']}")
        _assert(status_origin.x() + float(status.property("width"))
                <= float(header_item.property("width")) + 1,
                "visible popup status target must stay inside its header")
        _assert(float(status_text.property("width")) <= float(status.property("width")) + 1,
                "visible popup status text must elide within its hit target")
        interface = QAccessible.queryAccessibleInterface(status)
        accessible_name = str(interface.text(QAccessible.Text.Name)) if interface else ""
        _assert("错误：" in accessible_name and "读取失败" in accessible_name,
                "popup status must expose severity and the full source text accessibly")

        first_tab.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
        tab_reached_status = False
        for _ in range(8):
            QTest.keyClick(window, Qt.Key.Key_Tab)
            QCoreApplication.processEvents()
            if bool(status.property("activeFocus")):
                tab_reached_status = True
                break
        _assert(tab_reached_status,
                "popup status must be reachable from the dialog's Tab focus chain")
        status_center = status.mapToItem(
            window.contentItem(),
            float(status.property("width")) / 2,
            float(status.property("height")) / 2,
        )
        status_point = QPoint(round(status_center.x()), round(status_center.y()))
        QTest.mouseMove(window, status_point)
        QCoreApplication.processEvents()
        _assert(bool(tooltip.property("visible")), "hovering popup status must open its tooltip")
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         status_point)
        QCoreApplication.processEvents()
        _assert(bool(status.property("activeFocus")),
                "clicking the open status tooltip anchor must preserve status focus")
        _assert(bool(tooltip.property("visible")), "focusing popup status must open its full tooltip")
        full_text = _find(window, "themePopupHeaderStatusTipFullText")
        _assert(full_text is not None and str(full_text.property("text")).endswith(long_error),
                "tooltip must preserve the complete error source text")
        tip_bounds = _find(window, "qaPopupHeader").property("tooltipBoundsItem")
        tip_anchor = status.mapToItem(tip_bounds, 0, 0)
        _assert(float(tooltip.property("y")) >= tip_anchor.y() + float(status.property("height")),
                "long status tooltip must stay below the title/status row: "
                f"tipY={tooltip.property('y')} anchorY={tip_anchor.y()} "
                f"statusH={status.property('height')} boundsH={tip_bounds.property('height')}")
        _assert(float(tooltip.property("y")) + float(tooltip.property("height"))
                <= float(tip_bounds.property("height")) - 7,
                "long status tooltip must stay inside its overlay without covering window controls: "
                f"y={tooltip.property('y')} h={tooltip.property('height')} "
                f"bounds={tip_bounds.property('height')}")
        scroll_content = scroll.property("contentItem")
        before = float(scroll_content.property("contentY") or 0)
        QTest.keyClick(window, Qt.Key.Key_PageDown)
        QCoreApplication.processEvents()
        after = float(scroll_content.property("contentY") or 0)
        _assert(after > before, "PageDown must scroll a long popup status tooltip")
        results["screenshots"].append(_screenshot(window, "popup_header_light_long_error.png"))

        QTest.keyClick(window, Qt.Key.Key_Escape)
        QCoreApplication.processEvents()
        _assert(not bool(tooltip.property("visible")), "Escape must close the full-status tooltip")

        window.setProperty("qaStatusText", "材料目录重载完成。" + "新状态应从顶部读取。" * 35)
        window.setProperty("qaSeverity", "success")
        window.setProperty("qaDetail", "材料目录重载完成。" + "新状态应从顶部读取。" * 35)
        QCoreApplication.processEvents()
        QTest.qWait(30)
        QCoreApplication.processEvents()
        _assert(bool(tooltip.property("visible")),
                "a new owner status must be readable after Escape dismissed the prior one")
        _assert(float(scroll.property("contentItem").property("contentY") or 0) == 0,
                "a replacement status must reset its tooltip scroll position")

        window.setProperty("qaStatusText", "任务正在取消，已暂停后续队列。")
        window.setProperty("qaSeverity", "busy")
        window.setProperty("qaDetail", "任务正在取消，已暂停后续队列。")
        theme.set_mode("dark")
        QCoreApplication.processEvents()
        QTest.qWait(50)
        _assert("进行中：" in str(status_text.property("text")),
                "busy popup status must retain its severity label")
        results["screenshots"].append(_screenshot(window, "popup_header_dark_busy.png"))

        # The same page can switch themes and narrow widths repeatedly without
        # losing its status owner or making the title/status overlap.
        window.resize(390, 300)
        narrow_geometry = None
        for index, next_mode in enumerate(("light", "dark", "light")):
            theme.set_mode(next_mode)
            window.setProperty("qaStatusText", f"状态切换 {index + 1} · 处理中")
            window.setProperty("qaSeverity", "busy" if index % 2 == 0 else "info")
            window.setProperty("qaDetail", "重复切换后仍保留完整状态文本。" * 12)
            _wait_for_popup_header_layout(header_item, title_item, status)
            title_origin = title_item.mapToItem(header_item, 0, 0)
            status_origin = status.mapToItem(header_item, 0, 0)
            narrow_geometry = {
                "window": [float(window.width()), float(window.height())],
                "header": [float(header_item.property("width")), float(header_item.property("height"))],
                "title": [float(title_origin.x()), float(title_origin.y()),
                          float(title_item.property("width")), float(title_item.property("height"))],
                "status": [float(status_origin.x()), float(status_origin.y()),
                           float(status.property("width")), float(status.property("height"))],
                "statusTextWidth": float(status_text.property("width")),
            }
            _assert(title_origin.x() + float(title_item.property("width"))
                    <= status_origin.x() + 1,
                    "narrow popup header title and status target must not overlap: "
                    f"title={narrow_geometry['title']} status={narrow_geometry['status']} "
                    f"header={narrow_geometry['header']}")
            _assert(status_origin.x() + float(status.property("width"))
                    <= float(header_item.property("width")) + 1,
                    "narrow popup status target must stay inside its header")
            results["screenshots"].append(
                _screenshot(window, f"popup_header_narrow_{next_mode}_{index + 1}.png"))
        _assert(float(status.property("width")) > 0,
                "status must keep a nonzero hit target in the narrow dialog")

        window.setProperty("qaStatusText", "")
        window.setProperty("qaSeverity", "info")
        window.setProperty("qaDetail", "")
        QCoreApplication.processEvents()
        _assert(not bool(status.property("visible")),
                "clearing the owner status must hide its popup status control")
        results["screenshots"].append(_screenshot(window, "popup_header_status_cleared.png"))

        results["networkBlocked"] = list(network_factory.blocked_urls)
        results["sourceHashes"] = test_remaining_window_status_contracts()
        results["headerGeometry"] = {"normal": normal_geometry, "narrow": narrow_geometry}
        results["logicalWindowSize"] = [float(window.width()), float(window.height())]
        results["pixelWindowSize"] = [
            round(float(window.width()) * float(window.devicePixelRatio())),
            round(float(window.height()) * float(window.devicePixelRatio())),
        ]
        _assert(not results["networkBlocked"], "UI QA attempted external network access")
        (EVIDENCE / "manifest.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return results
    finally:
        window.close()
        window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_assistant_chat_status_clears_from_owner() -> dict[str, object]:
    """Drive real chat-window bindings with a local current-contract fixture."""
    _isolate_settings()
    app = QApplication.instance() or QApplication([])
    fonts = _install_capture_fonts(app)
    app.setQuitOnLastWindowClosed(False)
    register_preview_cursor()
    model = TurnModel(4, 8)

    theme = _Theme("light")
    engine = QQmlEngine()
    network_factory = _install_network_factory(engine)
    current_status = {"message": "", "busy": False}
    assistant_refs: list[QObject] = []

    def set_status(message: str, busy: bool = False) -> None:
        # Replacing this in-memory stub context object invalidates real QML
        # bindings while preserving the frozen constant property shape.
        assistant = StatusAssistant(model, message, busy)
        assistant_refs.append(assistant)
        engine.rootContext().setContextProperty("assistant", assistant)
        current_status.update(message=message, busy=busy)

    set_status("", False)
    engine.rootContext().setContextProperty("Theme", theme)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML_DIR / "AssistantChatWindow.qml")))
    _assert(component.status() == QQmlComponent.Status.Ready,
            "chat QML did not compile: "
            + "\n".join(error.toString() for error in component.errors()))
    window = component.create()
    _assert(window is not None,
            "chat QML did not instantiate: "
            + "\n".join(error.toString() for error in component.errors()))
    QQmlEngine.setObjectOwnership(window, QQmlEngine.ObjectOwnership.CppOwnership)
    images: list[dict[str, object]] = []
    try:
        window.show()
        QCoreApplication.processEvents()
        QTest.qWait(100)
        set_status("\u8fde\u63a5\u5931\u8d25\uff1a\u672c\u5730\u6d4b\u8bd5\u670d\u52a1\u4e0d\u53ef\u7528")
        QCoreApplication.processEvents()
        error_text = str(_evaluate_qml(engine, window, "chatChrome.statusText"))
        error_severity = str(_evaluate_qml(engine, window, "chatChrome.statusSeverity"))
        qml_assistant_status = str(_evaluate_qml(engine, window, "assistant.statusMessage"))
        _assert(error_text == current_status["message"],
                "chat error detail must remain visible through the chrome status owner: "
                f"chrome={error_text!r} assistant={current_status['message']!r} "
                f"qmlAssistant={qml_assistant_status!r} "
                f"metaStatus={assistant_refs[-1].metaObject().indexOfProperty('statusMessage')}")
        _assert(error_severity == "info",
                "chat status severity must follow the existing typed busy state")
        images.append(_screenshot(window, "chat_owner_error_visible.png"))

        set_status("\u6b63\u5728\u751f\u6210\u56de\u590d\u2026", busy=True)
        QCoreApplication.processEvents()
        _assert(str(_evaluate_qml(engine, window, "chatChrome.statusText")) == current_status["message"],
                "chat busy detail must remain visible")
        _assert(str(_evaluate_qml(engine, window, "chatChrome.statusSeverity")) == "busy",
                "chat busy severity must follow the assistant busy property")
        images.append(_screenshot(window, "chat_owner_busy.png"))

        set_status("\u51c6\u5907\u5c31\u7eea\u3002", busy=False)
        QCoreApplication.processEvents()
        QTest.qWait(40)
        _assert(str(_evaluate_qml(engine, window, "chatChrome.statusText")) == "",
                "chat status must clear when the assistant owner returns to ready")
        _assert(str(_evaluate_qml(engine, window, "chatChrome.statusSeverity")) == "info",
                "cleared chat status must return to informational severity")
        images.append(_screenshot(window, "chat_owner_status_cleared.png"))
        return {
            "devicePixelRatio": float(window.devicePixelRatio()),
            "logicalWindowSize": [float(window.width()), float(window.height())],
            "pixelWindowSize": [round(float(window.width()) * float(window.devicePixelRatio())),
                                round(float(window.height()) * float(window.devicePixelRatio()))],
            "errorText": error_text,
            "errorSeverity": error_severity,
            "clearedStatus": str(_evaluate_qml(engine, window, "chatChrome.statusText")),
            "fonts": fonts,
            "screenshots": images,
            "networkBlocked": list(network_factory.blocked_urls),
            "fixture": "in-memory TurnModel and AssistantStub only; no real service/session used",
        }
    finally:
        window.hide()
        window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


class _LauncherStub(QObject):
    launchError = Signal(str)

    @Property(int, constant=True)
    def backgroundWidth(self):
        return 1024

    @Property(int, constant=True)
    def backgroundHeight(self):
        return 768

    @Property(bool, constant=True)
    def softwareMode(self):
        return True

    @Property("QVariantList", constant=True)
    def loadingKeys(self):
        return []

    @Property("QVariantList", constant=True)
    def openingKeys(self):
        return []

    @Property(str, constant=True)
    def backgroundUrl(self):
        return ""

    @Slot()
    def shutdown(self):
        return None

    @Slot()
    def confirmShutdown(self):
        return None

    @Slot()
    def cancelShutdown(self):
        return None

    @Slot(int)
    def launch(self, _index: int):
        return None


def test_launcher_error_toast_expires_after_15_seconds() -> dict[str, object]:
    """Verify the visible error is mirrored to chrome only for the toast lifetime."""
    _isolate_settings()
    app = QApplication.instance() or QApplication([])
    fonts = _install_capture_fonts(app)
    app.setQuitOnLastWindowClosed(False)
    launcher = _LauncherStub()
    theme = _Theme("light")
    engine = QQmlEngine()
    network_factory = _install_network_factory(engine)
    engine.rootContext().setContextProperty("launcher", launcher)
    engine.rootContext().setContextProperty("Theme", theme)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML_DIR / "LauncherWindow.qml")))
    _assert(component.status() == QQmlComponent.Status.Ready,
            "launcher QML did not compile: "
            + "\n".join(error.toString() for error in component.errors()))
    window = component.create()
    _assert(window is not None,
            "launcher QML did not instantiate: "
            + "\n".join(error.toString() for error in component.errors()))
    QQmlEngine.setObjectOwnership(window, QQmlEngine.ObjectOwnership.CppOwnership)
    try:
        window.resize(900, 620)
        window.show()
        QCoreApplication.processEvents()
        QTest.qWait(100)
        launcher.launchError.emit("\u6d4b\u8bd5\u5931\u8d25\uff1a\u64cd\u4f5c\u5df2\u53d6\u6d88")
        QCoreApplication.processEvents()
        QTest.qWait(250)
        QCoreApplication.processEvents()
        visible_expression = bool(_evaluate_qml(engine, window, "errorToast.visible"))
        running_expression = bool(_evaluate_qml(engine, window, "errorTimer.running"))
        visible_status = str(window.property("statusText"))
        visible_severity = str(window.property("statusSeverity"))
        _assert(visible_expression and running_expression,
                "launch-error event must show the existing toast and start its timer: "
                f"visible={visible_expression} timer={running_expression} "
                f"status={window.property('statusText')!r} severity={window.property('statusSeverity')!r}")
        _assert(visible_status == "\u6d4b\u8bd5\u5931\u8d25\uff1a\u64cd\u4f5c\u5df2\u53d6\u6d88"
                and visible_severity == "error",
                "visible launcher error must be mirrored to the app-window status")
        first_image = _screenshot(window, "launcher_error_toast_and_chrome_status.png")
        started = time.monotonic()
        QTest.qWait(16300)
        QCoreApplication.processEvents()
        elapsed = time.monotonic() - started
        hidden_expression = bool(_evaluate_qml(engine, window, "errorToast.visible"))
        timer_running = bool(_evaluate_qml(engine, window, "errorTimer.running"))
        cleared_status = str(window.property("statusText"))
        cleared_severity = str(window.property("statusSeverity"))
        _assert(not hidden_expression and not timer_running,
                "existing 15 second toast timer must expire and stop")
        _assert(cleared_status == "" and cleared_severity == "info",
                "chrome status must clear when the existing toast becomes invisible")
        second_image = _screenshot(window, "launcher_error_status_expired.png")
        return {
            "devicePixelRatio": float(window.devicePixelRatio()),
            "logicalWindowSize": [float(window.width()), float(window.height())],
            "pixelWindowSize": [round(float(window.width()) * float(window.devicePixelRatio())),
                                round(float(window.height()) * float(window.devicePixelRatio()))],
            "visibleStatus": visible_status,
            "visibleSeverity": visible_severity,
            "clearedStatus": cleared_status,
            "elapsedSeconds": round(elapsed, 3),
            "toastVisibleAfterExpiry": hidden_expression,
            "timerRunningAfterExpiry": timer_running,
            "fonts": fonts,
            "screenshots": [first_image, second_image],
            "networkBlocked": list(network_factory.blocked_urls),
            "fixture": "in-memory launcher error signal only; no modules/services launched",
        }
    finally:
        window.hide()
        window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


_QA_CASE_RESULTS: dict[str, object] = {}


class _RemainingWindowsSuite(unittest.TestCase):
    def test_source_contracts(self):
        _QA_CASE_RESULTS["sourceContracts"] = test_remaining_window_status_contracts()

    def test_popup_header(self):
        _QA_CASE_RESULTS["popupHeader"] = test_popup_header_render_and_keyboard()

    def test_material_modes(self):
        _QA_CASE_RESULTS["materialModes"] = test_material_library_standalone_and_hosted_status_modes()

    def test_chat_status_clear(self):
        _QA_CASE_RESULTS["chatStatus"] = test_assistant_chat_status_clears_from_owner()

    def test_launcher_toast_expiry(self):
        _QA_CASE_RESULTS["launcherExpiry"] = test_launcher_error_toast_expires_after_15_seconds()


def _owned_source_hashes() -> dict[str, str]:
    paths = (
        "coat_sim/ui/qml/AboutWindow.qml",
        "coat_sim/ui/qml/LauncherWindow.qml",
        "coat_sim/ui/qml/MaterialLibraryHost.qml",
        "coat_sim/ui/qml/AssistantChatWindow.qml",
        "coat_sim/ui/qml/ThemePageDialog.qml",
        "coat_sim/ui/qml/MaterialLibraryDialog.qml",
        "coat_sim/ui/qml/theme_controls/ThemePopupHeader.qml",
        "tests/test_remaining_windows_compact_chrome_ui.py",
    )
    return {path: hashlib.sha256(_source(path).encode("utf-8")).hexdigest()
            for path in paths}


def run_remaining_windows_qa() -> dict[str, object]:
    """Run all targeted checks and save a source-bound DPR manifest."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    _QA_CASE_RESULTS.clear()
    _QA_NETWORK_FACTORIES.clear()
    _QA_ENGINE_INSTALLS.clear()
    before = _owned_source_hashes()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(_RemainingWindowsSuite)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    after = _owned_source_hashes()
    network_urls = [url for record in _QA_ENGINE_INSTALLS
                    for url in getattr(record["factory"], "blocked_urls", [])]
    manifest: dict[str, object] = {
        "exitCode": 0 if result.wasSuccessful() and before == after else 1,
        "testCaseCount": result.testsRun,
        "testFailures": [str(failure[1]) for failure in result.failures],
        "testErrors": [str(error[1]) for error in result.errors],
        "sourceHashesBefore": before,
        "sourceHashesAfter": after,
        "sourceHashesMatch": before == after,
        "networkFactoryInstalledCount": len(_QA_ENGINE_INSTALLS),
        "networkFactoryAllInstalled": all(row["installed"] for row in _QA_ENGINE_INSTALLS),
        "networkFactoriesRetained": len(_QA_NETWORK_FACTORIES),
        "blockedNetworkRequests": network_urls,
        "caseResults": _QA_CASE_RESULTS,
        "dprEnvironment": os.environ.get("QT_SCALE_FACTOR", "1"),
        "applicationDpr": float(QApplication.instance().primaryScreen().devicePixelRatio()),
    }
    dpr_suffix = str(os.environ.get("QT_SCALE_FACTOR", "1")).replace(".", "_")
    manifest_path = EVIDENCE / f"manifest_dpr_{dpr_suffix}.json"
    manifest["manifestPath"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _assert(manifest["exitCode"] == 0, "one or more targeted UI checks failed")
    _assert(manifest["networkFactoryInstalledCount"] == result.testsRun - 1,
            "every runtime test engine must install a retained no-network factory")
    _assert(not network_urls, "a QML network request escaped the no-network guard")
    return manifest
