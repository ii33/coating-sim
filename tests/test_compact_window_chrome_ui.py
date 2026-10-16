r"""Offscreen rendered and interactive QA for the shared Qt Quick window chrome.

Run as a single isolated process with the workspace interpreter, for example:
    $env:QT_SCALE_FACTOR = '1.5'
    .\.conda\python.exe -c "import runpy; runpy.run_path('tests/test_compact_window_chrome_ui.py')['test_compact_window_chrome_ui']()"

Screenshots and a hash manifest are written only to the dedicated QA evidence
directory below. No real application settings or network access are used.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")
os.environ.setdefault("PYTHONUTF8", "1")

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
QML_DIR = ROOT / "coat_sim" / "ui" / "qml"
EVIDENCE = ROOT / "build" / "qa_ui_chrome_20261011_luna_9f2b"
sys.path.insert(0, str(TESTS))

# Apply the same fail-closed QSettings guard as the rest of the Qt Quick suite.
import qt_test_guard  # noqa: E402,F401

from PySide6.QtCore import QEvent, QObject, QCoreApplication, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import (QAccessible, QColor, QFont, QFontDatabase, QGuiApplication,
                           QMouseEvent, QWheelEvent, QWindow)  # noqa: E402
from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from coat_sim.ui.theme_service import ThemeEngineBridge  # noqa: E402
from coat_sim.ui.theme_tokens import build_theme_snapshot  # noqa: E402


_HARNESS = r'''import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "." as LocalComponents
import "theme_controls" as ThemeControls

LocalComponents.AppWindow {
    id: qaWindow
    objectName: "qaWindow"
    width: 900
    height: 640
    minimumWidth: 0
    minimumHeight: 0
    title: "QA long project title — a complete dirty document caption stays available"
    caption: title
    statusText: "Ready"
    statusSeverity: "info"
    statusDetail: ""
    property bool qaF4Triggered: false
    property string qaItemAtFirstName: sidebarRows.count > 0 && sidebarRows.itemAt(0)
                                      ? sidebarRows.itemAt(0).objectName : ""
    property string qaItemAtLastName: sidebarRows.count > 39 && sidebarRows.itemAt(39)
                                     ? sidebarRows.itemAt(39).objectName : ""

    Shortcut {
        sequence: "F4"
        context: Qt.WindowShortcut
        onActivated: qaWindow.qaF4Triggered = true
    }

    ThemeControls.ThemeSidebar {
        id: qaSidebar
        objectName: "qaSidebar"
        x: 16
        y: qaWindow.contentTopMargin + 8
        width: 328
        height: 300
        title: "Rendered sidebar with a long heading that wraps inside the available width"

        Text {
            objectName: "qaSidebarLongText"
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
            color: Theme.snapshot.colorText
            font.pointSize: Theme.snapshot.fontSizeBody
            text: "Long unbroken sidebar diagnostic " + "wrap-this-content-".repeat(20)
        }

        Repeater {
            id: sidebarRows
            objectName: "qaSidebarRepeater"
            model: 40
            delegate: ThemeControls.ThemeLabel {
                required property int index
                objectName: "qaSidebarRow_" + index
                Layout.fillWidth: true
                wrapMode: Text.WrapAnywhere
                text: "Scrollable sidebar row " + index + " — " + "row-detail-".repeat(4)
            }
        }
    }
}
'''


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _find_item(root: QQuickItem, object_name: str) -> QQuickItem | None:
    pending = [root]
    while pending:
        item = pending.pop()
        if item.objectName() == object_name:
            return item
        pending.extend(item.childItems())
    return None


def _find_object(root, object_name: str):
    matches = root.findChildren(QObject, object_name)
    return matches[0] if matches else None


def _find_tooltip_for_text(root, full_text: str):
    for candidate in root.findChildren(QObject):
        class_name = str(candidate.metaObject().className())
        if "ToolTip" in class_name and str(candidate.property("text")) == full_text:
            return candidate
    return None


def _accessible_text(item, text_kind: QAccessible.Text) -> str:
    interface = QAccessible.queryAccessibleInterface(item)
    if interface is None:
        return ""
    return str(interface.text(text_kind))


def _point_in_item(window, item: QQuickItem, x_ratio: float = 0.5,
                   y_ratio: float = 0.5) -> QPoint:
    scene = item.mapToScene(QPointF(item.width() * x_ratio, item.height() * y_ratio))
    return QPoint(round(scene.x()), round(scene.y()))


def _left_in_root(root: QQuickItem, item: QQuickItem) -> float:
    return float(item.mapToItem(root, QPointF(0, 0)).x())


def _popup_rect_in_root(popup, root: QQuickItem) -> tuple[float, float, float, float]:
    parent = popup.property("parent")
    x = float(popup.property("x"))
    y = float(popup.property("y"))
    if isinstance(parent, QQuickItem):
        origin = parent.mapToItem(root, QPointF(x, y))
        x, y = float(origin.x()), float(origin.y())
    return x, y, x + float(popup.property("width")), y + float(popup.property("height"))


def _click_item(app, window, item: QQuickItem, *, double: bool = False) -> None:
    point = _point_in_item(window, item)
    if double:
        QTest.mouseDClick(window, Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier, point, 20)
    else:
        QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, point, 20)
    app.processEvents()


def _save_screenshot(window, filename: str) -> tuple[int, int]:
    image = window.grabWindow()
    _assert(not image.isNull(), f"could not render screenshot {filename}")
    path = EVIDENCE / filename
    _assert(image.save(str(path)), f"could not save screenshot {path}")
    return image.width(), image.height()


def _create_window(app, mode: str):
    engine = QQmlEngine()
    engine.setNetworkAccessManagerFactory(qt_test_guard.NoNetworkAccessManagerFactory())
    bridge = ThemeEngineBridge(
        build_theme_snapshot(mode), 0, mode, mode, "qa", f"QA {mode}", {}, parent=engine
    )
    engine.rootContext().setContextProperty("Theme", bridge)
    component = QQmlComponent(engine)
    component.setData(_HARNESS.encode("utf-8"), QUrl.fromLocalFile(str(QML_DIR / "qa_harness.qml")))
    _assert(component.status() == QQmlComponent.Status.Ready,
            "QML harness did not compile: " + "\n".join(str(error) for error in component.errors()))
    window = component.create()
    _assert(window is not None,
            "QML harness did not create: " + "\n".join(str(error) for error in component.errors()))
    QQmlEngine.setObjectOwnership(window, QQmlEngine.ObjectOwnership.CppOwnership)
    window.setGeometry(100, 100, 900, 640)
    window.showNormal()
    app.processEvents()
    QTest.qWait(40)
    app.processEvents()
    return engine, bridge, component, window


def _install_capture_fonts(app) -> dict[str, object]:
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    loaded: list[dict[str, object]] = []
    for filename in ("segoeui.ttf", "msyh.ttc", "msyhbd.ttc"):
        path = fonts_dir / filename
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        loaded.append({"file": filename, "id": font_id, "families": list(families)})
    families = [family for record in loaded for family in record["families"]]
    preferred = next((name for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
                      if name in families), None)
    if preferred:
        app.setFont(QFont(preferred))
    return {"loaded": loaded, "applicationFont": preferred}


def _assert_non_overlapping(window, width: int) -> tuple[QQuickItem, QQuickItem]:
    window.resize(width, 640)
    QCoreApplication.processEvents()
    chrome = window.contentItem()
    caption = _find_item(chrome, "windowChromeCaption")
    status = _find_item(chrome, "windowChromeStatus")
    drag = _find_item(chrome, "windowChromeTitleDrag")
    minimize = _find_item(chrome, "windowChromeMinimizeButton")
    maximize = _find_item(chrome, "windowChromeMaximizeButton")
    close = _find_item(chrome, "windowChromeCloseButton")
    _assert(all(item is not None for item in (caption, status, drag, minimize, maximize, close)),
            f"chrome hit target missing at logical width {width}")
    _assert(status.width() > 0, f"status hit region collapsed at logical width {width}")
    _assert(_left_in_root(chrome, caption) + caption.width() <= _left_in_root(chrome, status) + 0.5,
            f"caption overlaps status at logical width {width}")
    _assert(_left_in_root(chrome, status) + status.width() <= _left_in_root(chrome, minimize) - 1,
            f"status overlaps system buttons at logical width {width}")
    _assert(_left_in_root(chrome, status) >= _left_in_root(chrome, drag) - 0.5
            and _left_in_root(chrome, status) + status.width()
            <= _left_in_root(chrome, drag) + drag.width() + 0.5,
            f"status text is outside the title-drag hit region at logical width {width}")
    _assert(_left_in_root(chrome, drag) + drag.width() <= _left_in_root(chrome, minimize) - 1,
            f"title drag hit region overlaps system buttons at logical width {width}")
    _assert(_left_in_root(chrome, close) + close.width() <= width,
            f"close button is outside logical window width {width}")
    return status, drag


def _test_theme_and_status(app, mode: str, dpr: float) -> dict[str, object]:
    engine, bridge, component, window = _create_window(app, mode)
    metrics: dict[str, object] = {"mode": mode}
    try:
        snapshot = build_theme_snapshot(mode)
        chrome = window.contentItem()
        caption = _find_item(chrome, "windowChromeCaption")
        status = _find_item(chrome, "windowChromeStatus")
        status_text = _find_item(chrome, "windowChromeStatusText")
        status_tooltip = _find_object(window, "windowChromeStatusToolTip")
        buttons = {
            name: _find_item(chrome, name)
            for name in ("windowChromeMinimizeButton", "windowChromeMaximizeButton",
                         "windowChromeCloseButton")
        }
        _assert(all(item is not None for item in (caption, status, status_text)),
                f"chrome semantic object missing in {mode} theme")
        _assert(status_tooltip is None,
                "shared window status must not create an interactive tooltip")
        _assert(not bool(status.property("activeFocusOnTab")),
                "status presentation unexpectedly entered the keyboard focus order")
        _assert(all("MouseArea" not in str(child.metaObject().className())
                    for child in status.childItems()),
                "status presentation unexpectedly contains an input handler")
        _assert("TextInput" not in str(status_text.metaObject().className())
                and "TextEdit" not in str(status_text.metaObject().className()),
                "status presentation unexpectedly became an editable text control")
        _assert(all(item is not None for item in buttons.values()),
                f"system button missing in {mode} theme")
        expected_bar = max(36.0, float(snapshot["controlHeightSm"])
                           + 2 * float(snapshot["spaceXs"]))
        _assert(abs(float(window.property("titleBarHeight")) - expected_bar) < 0.01,
                f"titleBarHeight must use logical theme tokens at DPR {dpr}: {window.property('titleBarHeight')}")
        _assert(abs(float(window.property("contentTopMargin"))
                    - float(window.property("titleBarHeight"))) < 0.01,
                "contentTopMargin stopped matching the shared title bar height")
        _assert(str(window.property("themePaletteMode")) == mode,
                f"window theme mode binding is stale for {mode}")
        _assert(QColor(caption.property("color")) == QColor(snapshot["colorText"]),
                f"caption ink does not follow the {mode} theme token")
        _assert(float(window.devicePixelRatio()) == dpr,
                f"Qt DPR mismatch: expected {dpr}, got {window.devicePixelRatio()}")

        # Verify the actual accessibility interfaces, not source-text labels.
        for name, item in buttons.items():
            accessible_name = _accessible_text(item, QAccessible.Text.Name)
            expected = {"windowChromeMinimizeButton": "最小化",
                        "windowChromeMaximizeButton": "最大化或还原",
                        "windowChromeCloseButton": "关闭"}[name]
            _assert(expected in accessible_name,
                    f"{name} accessible name missing: {accessible_name!r}")

        transitions = [
            ("info", "Ready", ""),
            ("warning", "Input needs attention", "Review the highlighted source settings."),
            ("error", "Calculation failed", "The backend returned a detailed error."),
            ("success", "Saved", "All updates were committed."),
            ("busy", "Calculating", "Sampling and deposition are still running."),
            ("info", "Ready again", ""),
        ]
        for severity, short_text, detail in transitions:
            window.setProperty("statusSeverity", severity)
            window.setProperty("statusText", short_text)
            window.setProperty("statusDetail", detail)
            app.processEvents()
            _assert(bool(status.property("visible")),
                    f"status disappeared during {severity} transition")
            rendered = str(status_text.property("text"))
            _assert(rendered.endswith(short_text),
                    f"status binding stale for {severity}: {rendered!r}")
            accessible_name = _accessible_text(status, QAccessible.Text.Name)
            accessible_value = detail or short_text
            _assert(accessible_value in accessible_name,
                    f"status accessible name stale for {severity}: {accessible_name!r}")

        # A long bilingual diagnostic remains available to accessibility while
        # the visible status is a single elided, noninteractive line.
        long_message = ("长状态说明：采样与沉积阶段返回诊断，含输入来源与恢复建议。" * 160
                        + " Long diagnostic details include source, retry guidance, and task identifiers. " * 24)
        window.setProperty("statusSeverity", "error")
        window.setProperty("statusText", "Long diagnostic available")
        window.setProperty("statusDetail", long_message)
        app.processEvents()
        cjk_characters = sum("\u4e00" <= character <= "\u9fff" for character in long_message)
        _assert(cjk_characters >= 1000,
                f"long status must include >=1000 CJK characters; got {cjk_characters}")
        accessible_name = _accessible_text(status, QAccessible.Text.Name)
        _assert(long_message in accessible_name,
                "long diagnostic is absent from the rendered accessibility name")
        _assert(status.width() < len(long_message),
                "long status test did not exercise an elided visible line")

        _assert(status_tooltip is None and _find_object(window, "windowChromeStatusTipFullText") is None,
                "status tooltip or its scrollable content was not removed")
        metrics.update({"longStatusCharacters": len(long_message),
                        "statusWidth": float(status.width()),
                        "statusAccessibleNameCharacters": len(accessible_name),
                        "statusTooltipPresent": False})
        _save_screenshot(window, f"{mode}_long_status_static.png")

        # Supported minimum and below-minimum layout stress. The 480 px run is
        # deliberately synthetic; the production window declares 720 px min.
        for width in (720, 480):
            status, drag = _assert_non_overlapping(window, width)
            if width == 720:
                narrow_point = _point_in_item(window, status)
                QTest.mouseMove(window, narrow_point, 10)
                app.processEvents()
                QTest.qWait(60)
                app.processEvents()
                _save_screenshot(window, f"{mode}_narrow720_long_status.png")
            metrics.setdefault("widthChecks", []).append({
                "logicalWidth": width,
                "statusX": float(status.x()),
                "statusWidth": float(status.width()),
                "dragRight": float(drag.x() + drag.width()),
            })
            # Restore a full size for action hit testing and the stable image.
            window.resize(900, 640)
            app.processEvents()

        # Exercise caption hover and preserve the complete (elided) caption.
        caption_tooltip = _find_tooltip_for_text(window, str(window.property("caption")))
        _assert(caption_tooltip is not None,
                "caption tooltip object is not exposed for full caption access")
        caption_point = _point_in_item(window, caption, x_ratio=0.3)
        QTest.mouseMove(window, caption_point, 10)
        app.processEvents()
        _assert(bool(caption_tooltip.property("visible")),
                "caption hover did not reveal the complete caption")
        _assert(str(caption_tooltip.property("text")) == str(window.property("caption")),
                "caption tooltip does not preserve caption / dirty marker text")

        # Status is visually above the title area but must not capture pointer
        # input: pressing and dragging its text routes to the ordinary handler.
        drag = _find_item(window.contentItem(), "windowChromeTitleDrag")
        status_point = _point_in_item(window, status)
        QTest.mousePress(window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, status_point)
        _assert(bool(drag.property("pressed")),
                "status-area press did not reach the title-drag MouseArea")
        moved_point = QPoint(status_point.x() + 36, status_point.y() + 18)
        global_moved_point = window.mapToGlobal(moved_point)
        move_event = QMouseEvent(QEvent.Type.MouseMove, QPointF(moved_point),
                                 QPointF(global_moved_point), Qt.MouseButton.NoButton,
                                 Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QCoreApplication.sendEvent(window, move_event)
        app.processEvents()
        _assert(bool(drag.property("moving")),
                "status-area press/move did not engage title-bar window movement")
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, moved_point)
        app.processEvents()
        metrics["statusAreaDragRoutedToTitleBar"] = True

        # Double-clicking the same presentation-only region keeps native
        # title-bar maximize/restore semantics.
        window.showNormal()
        window.resize(900, 640)
        app.processEvents()
        _click_item(app, window, status, double=True)
        metrics["statusAreaDoubleClickMaximized"] = (
            window.visibility() == QWindow.Visibility.Maximized)
        _assert(metrics["statusAreaDoubleClickMaximized"],
                "status-area double-click did not maximize the window")
        _click_item(app, window, status, double=True)
        metrics["statusAreaDoubleClickRestored"] = (
            window.visibility() == QWindow.Visibility.Windowed)
        _assert(metrics["statusAreaDoubleClickRestored"],
                "second status-area double-click did not restore the window")

        # Title-drag area responds to a drag in the title region. Offscreen
        # cannot certify the native OS move loop; this checks the fallback hit.
        drag = _find_item(window.contentItem(), "windowChromeTitleDrag")
        before = (window.x(), window.y())
        drag_point = _point_in_item(window, drag, x_ratio=0.12, y_ratio=0.7)
        QTest.mousePress(window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, drag_point)
        _assert(bool(drag.property("pressed")), "title-drag press missed its rendered hit area")
        moved_point = QPoint(drag_point.x() + 36, drag_point.y() + 18)
        global_moved_point = window.mapToGlobal(moved_point)
        move_event = QMouseEvent(QEvent.Type.MouseMove, QPointF(moved_point),
                                 QPointF(global_moved_point), Qt.MouseButton.NoButton,
                                 Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QCoreApplication.sendEvent(window, move_event)
        app.processEvents()
        drag_engaged = bool(drag.property("moving"))
        native_move_accepted = not bool(drag.property("fallbackMove"))
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier,
                           QPoint(drag_point.x() + 36, drag_point.y() + 18))
        app.processEvents()
        after = (window.x(), window.y())
        metrics["titleDragGeometryBeforeAfter"] = [list(before), list(after)]
        metrics["titleDragEngaged"] = drag_engaged
        metrics["nativeMoveAcceptedButUnverifiableOffscreen"] = native_move_accepted and after == before
        _assert(drag_engaged, "title-drag hit region did not engage after a press-and-drag")

        # Button actions go through the same measured hit regions as a user click.
        _click_item(app, window, buttons["windowChromeMaximizeButton"])
        _assert(window.visibility() == QWindow.Visibility.Maximized,
                "maximize button did not maximize the QML window")
        _click_item(app, window, _find_item(window.contentItem(), "windowChromeMaximizeButton"))
        _assert(window.visibility() == QWindow.Visibility.Windowed,
                "maximize button did not restore the QML window")
        _click_item(app, window, buttons["windowChromeMinimizeButton"])
        _assert(window.visibility() == QWindow.Visibility.Minimized,
                "minimize button did not minimize the QML window")
        window.showNormal()
        app.processEvents()

        # Alt+F4 is a platform window-manager gesture, but send the actual key
        # sequence once so the supported window path is represented in evidence.
        QTest.keyClick(window, Qt.Key.Key_F4, Qt.KeyboardModifier.AltModifier)
        app.processEvents()
        metrics["altF4ClosedOnOffscreen"] = not window.isVisible()
        if window.isVisible():
            metrics["altF4Limit"] = "offscreen platform did not translate Alt+F4 into QCloseEvent"
        else:
            window.showNormal()
            app.processEvents()

        # Existing close-button behavior remains intact and closes this window.
        _click_item(app, window, _find_item(window.contentItem(), "windowChromeCloseButton"))
        _assert(not window.isVisible(), "close button did not close the QML window")

        # Store the final rendered chrome too, after all repeated states/actions.
        window.showNormal()
        app.processEvents()
        metrics["finalScreenshotDevicePixels"] = list(_save_screenshot(window, f"{mode}_final.png"))
        return metrics
    finally:
        window.close()
        engine.deleteLater()
        app.processEvents()


def _test_sidebar_render_and_scroll(app, mode: str) -> dict[str, object]:
    engine, bridge, component, window = _create_window(app, mode)
    try:
        window.resize(900, 640)
        app.processEvents()
        sidebar = _find_item(window.contentItem(), "qaSidebar")
        long_text = _find_item(window.contentItem(), "qaSidebarLongText")
        scroll = _find_item(window.contentItem(), "themeSidebarScroll")
        _assert(all(item is not None for item in (sidebar, long_text, scroll)),
                "ThemeSidebar did not create its visual sidebar, wrap row, and ScrollView")
        _assert(sidebar.y() >= float(window.property("contentTopMargin")),
                "ThemeSidebar content was laid out beneath the title chrome")
        _assert(float(long_text.property("width")) > 0,
                "long sidebar text received no available width")
        _assert(int(long_text.property("lineCount")) > 1,
                "long sidebar text did not visibly wrap to more than one line")
        rows = [_find_item(window.contentItem(), f"qaSidebarRow_{index}")
                for index in range(40)]
        _assert(all(row is not None for row in rows), "not all caller-owned sidebar rows rendered")
        scroll_obj = _find_object(window, "themeSidebarScroll")
        _assert(scroll_obj is not None, "ThemeSidebar ScrollView object is not inspectable")
        repeater = _find_object(window, "qaSidebarRepeater")
        _assert(repeater is not None and int(repeater.property("count")) == 40,
                "ThemeSidebar's Repeater did not instantiate the expected 40 rows")
        _assert(str(window.property("qaItemAtFirstName")) == "qaSidebarRow_0"
                and str(window.property("qaItemAtLastName")) == "qaSidebarRow_39",
                "Repeater.itemAt did not return its first and last rendered row")
        content_column = _find_item(window.contentItem(), "themeSidebarContent")
        _assert(content_column is not None, "ThemeSidebar body layout is not a visual child")
        _assert(all(row.parentItem() is content_column for row in rows),
                "caller-owned Repeater rows are not visual children of ThemeSidebar.body")
        _assert(all(row.width() > 0 and row.height() > 0 for row in rows),
                "one or more caller-owned Repeater rows has no rendered geometry")
        viewport = scroll_obj.property("contentItem")
        _assert(isinstance(viewport, QQuickItem),
                "ThemeSidebar ScrollView did not expose its visual Flickable viewport")
        content_y_before = float(viewport.property("contentY"))
        local = _point_in_item(window, scroll, x_ratio=0.8, y_ratio=0.8)
        global_point = window.mapToGlobal(local)
        wheel = QWheelEvent(QPointF(local), QPointF(global_point), QPoint(), QPoint(0, -480),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.ScrollUpdate, False)
        QCoreApplication.sendEvent(window, wheel)
        app.processEvents()
        QTest.qWait(80)
        app.processEvents()
        content_y_after = float(viewport.property("contentY"))
        max_scroll = float(viewport.property("contentHeight")) - float(viewport.property("height"))
        row_parent_names = sorted({row.parentItem().objectName() for row in rows})
        metrics = {"sidebarTitleHeight": float(sidebar.height()),
                   "longTextWidth": float(long_text.width()),
                   "longTextHeight": float(long_text.height()),
                   "rowCount": len(rows),
                   "repeaterCount": int(repeater.property("count")),
                   "itemAtFirst": str(window.property("qaItemAtFirstName")),
                   "itemAtLast": str(window.property("qaItemAtLastName")),
                   "rowVisualParentNames": row_parent_names,
                   "firstRowHeight": float(rows[0].height()),
                   "lastRowHeight": float(rows[-1].height()),
                   "contentYBefore": content_y_before,
                   "contentYAfterWheel": content_y_after,
                   "maxScroll": max_scroll}
        _assert(max_scroll > 0, f"ThemeSidebar has no vertical overflow: {metrics}")
        _assert(content_y_after > content_y_before,
                f"wheel did not scroll ThemeSidebar: {metrics}")
        _save_screenshot(window, f"{mode}_sidebar_scrolled.png")
        return metrics
    finally:
        window.close()
        engine.deleteLater()
        app.processEvents()


def _manifest(dpr: float, results: dict[str, object]) -> None:
    paths = [QML_DIR / "AppWindow.qml", QML_DIR / "WindowChrome.qml",
             QML_DIR / "theme_controls" / "ThemeSidebar.qml", Path(__file__).resolve()]
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    payload = {
        "qtScaleFactor": os.environ.get("QT_SCALE_FACTOR", "1"),
        "devicePixelRatio": dpr,
        "platform": os.environ.get("QT_QPA_PLATFORM"),
        "qmlSha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in paths},
        "results": results,
    }
    (EVIDENCE / f"run_dpr_{str(dpr).replace('.', '_')}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_compact_window_chrome_ui() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    QAccessible.setActive(True)
    app = QGuiApplication.instance() or QGuiApplication([])
    font_metrics = _install_capture_fonts(app)
    scale = float(os.environ.get("QT_SCALE_FACTOR", "1"))
    screen = app.primaryScreen()
    dpr = float(screen.devicePixelRatio()) if screen else float(app.devicePixelRatio())
    _assert(abs(dpr - scale) < 0.05,
            f"Qt high-DPI test did not honor QT_SCALE_FACTOR={scale}: observed {dpr}")
    results = {
        mode: _test_theme_and_status(app, mode, dpr)
        for mode in ("light", "dark")
    }
    results["sidebar_light"] = _test_sidebar_render_and_scroll(app, "light")
    results["captureFonts"] = font_metrics
    _manifest(dpr, results)


if __name__ == "__main__":
    test_compact_window_chrome_ui()
