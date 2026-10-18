"""Offline mouse/menu regressions. No user settings, model or network access."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

from PySide6.QtCore import QObject, Property, QCoreApplication, QEvent, QPoint, QPointF, QRect, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from coat_sim.ui import assistant_desktop
from coat_sim.ui.about import AboutBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot
from coat_sim.version import BUILD_IDENTITY

ROOT = Path(__file__).resolve().parents[1]
QML = ROOT / "coat_sim/ui/qml"


def _visual_window_for(item: QObject) -> QQuickWindow:
    """Find a popup surface without calling QQuickItem.window() on its child."""
    assert isinstance(item, QQuickItem)
    for window in QGuiApplication.allWindows():
        if isinstance(window, QQuickWindow):
            content = window.contentItem()
            if content is item or content.isAncestorOf(item):
                return window
    raise AssertionError(f"no Qt Quick surface contains {item.objectName()!r}")


class AssistantStub(QObject):
    def __init__(self):
        super().__init__()
        self.calls = []

    @Property(int, constant=True)
    def iconSize(self):
        return 72

    @Property(QUrl, constant=True)
    def portraitUrl(self):
        return QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/assets/assistant.png"))

    @Slot()
    def toggleChat(self):
        self.calls.append("chat")

    @Slot()
    def showConversations(self):
        self.calls.append("conversations")

    @Slot()
    def showTokenStatistics(self):
        self.calls.append("tokens")


class ThemeStub(QObject):
    snapshotChanged = Signal()

    def __init__(self):
        super().__init__()
        self._mode = "light"

    @Property("QVariantMap", notify=snapshotChanged)
    def snapshot(self):
        return build_theme_snapshot(self._mode)

    @Property("QVariantMap", notify=snapshotChanged)
    def tokenValues(self):
        return build_theme_snapshot(self._mode)

    @Property("QVariantMap", notify=snapshotChanged)
    def assets(self):
        return {}

    def set_mode(self, mode):
        self._mode = mode
        self.snapshotChanged.emit()


class BubbleTests(unittest.TestCase):
    bubble: QQuickWindow
    menu: QObject

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for target, replacement in (
            ("load_ui_settings", lambda: {"assistant_notification_sound": False}),
            ("save_ui_settings", lambda _settings: None),
            ("QSoundEffect", None),
            ("CURSOR_PATH", Path(self.directory.name) / "cursor.json"),
        ):
            patcher = patch.object(assistant_desktop, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.engine = QQmlApplicationEngine()
        self.assistant = AssistantStub()
        self.theme = ThemeStub()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            self.desktop = assistant_desktop.DesktopAssistantBridge(self.engine)
        self.engine.rootContext().setContextProperty("assistant", self.assistant)
        self.engine.rootContext().setContextProperty("desktop", self.desktop)
        self.engine.rootContext().setContextProperty("Theme", self.theme)
        self.component = QQmlComponent(self.engine, QUrl.fromLocalFile(str(QML / "AssistantBubble.qml")))
        self.assertFalse(self.component.isError(), self.component.errors())
        bubble = self.component.create()
        assert isinstance(bubble, QQuickWindow)
        self.bubble = bubble
        menu = self.bubble.findChild(QObject, "assistantContextMenu")
        assert menu is not None
        self.menu = menu
        self.bubble.setPosition(100, 100)
        self.bubble.show()
        QTest.qWait(40)

    def tearDown(self):
        self.menu.setProperty("visible", False)
        self.bubble.close()
        self.bubble.deleteLater()
        self.desktop.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def open_menu(self):
        QTest.mouseClick(self.bubble, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, QPoint(36, 36))
        QTest.qWait(80)
        self.assertTrue(self.menu.property("opened"))
        self.assertTrue(self.menu.property("visible"))
        self.assertEqual(self.assistant.calls, [])

    def popup_window(self):
        windows = [window for window in QGuiApplication.allWindows()
                   if window.isVisible() and window is not self.bubble
                   and window.width() >= self.menu.property("width")]
        self.assertGreaterEqual(len(windows), 1)
        # The root menu and its window-list submenu use separate popup windows.
        # The root menu is narrower, and is the one receiving the Right key.
        return min(windows, key=lambda window: window.width())

    def test_production_theme_menu_has_clickable_surface_and_reopens(self):
        from coat_sim.ui.theme_service import configure_theme_from_settings

        theme = configure_theme_from_settings({"theme_mode": "light"})
        theme.attach_engine(self.engine)
        QTest.qWait(40)
        queue_item = self.bubble.findChild(QQuickItem, "assistantQueueMenuItem")
        assert queue_item is not None
        signal_index = queue_item.metaObject().indexOfSignal("triggered()")
        self.assertGreaterEqual(signal_index, 0)
        triggered = QSignalSpy(queue_item, queue_item.metaObject().method(signal_index))
        self.assertTrue(triggered.isValid())
        for cycle in range(3):
            self.open_menu()
            self.assertGreaterEqual(self.menu.property("width"), 230)
            self.assertGreater(queue_item.width(), 100)
            popup = _visual_window_for(queue_item)
            bounds = popup.screen().availableGeometry()
            self.assertTrue(bounds.contains(popup.geometry()),
                            (bounds, popup.geometry()))
            point = queue_item.mapToGlobal(QPointF(queue_item.width() / 2,
                                                   queue_item.height() / 2))
            QTest.mouseClick(popup, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPoint(round(point.x() - popup.x()),
                                    round(point.y() - popup.y())))
            QTest.qWait(80)
            self.assertEqual(triggered.count(), cycle + 1)
            self.assertFalse(self.menu.property("visible"))
            QTest.mouseClick(self.bubble, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPoint(36, 36))
            QTest.qWait(40)
            self.assertEqual(self.assistant.calls, ["chat"])
            self.assistant.calls.clear()

    def click_root_item(self, name):
        item = self.bubble.findChild(QQuickItem, name)
        assert item is not None
        self.assertTrue(item.isVisible())
        popup = _visual_window_for(item)
        point = item.mapToGlobal(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(popup, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(round(point.x() - popup.x()),
                                round(point.y() - popup.y())))
        QTest.qWait(80)
        self.assertFalse(self.menu.property("visible"))

    def test_hidden_waiting_item_takes_no_space_and_visible_actions_route(self):
        calls = []

        def hide_windows():
            calls.append("hide")
            self.bubble.hide()

        self.desktop.controller = SimpleNamespace(
            _bubble=self.bubble, hide_windows=hide_windows)
        self.desktop.set_application_exit_callback(lambda: calls.append("exit"))
        self.desktop.set_application_exit_cancel_callback(lambda: calls.append("cancel"))
        self.open_menu()
        waiting = self.bubble.findChild(QQuickItem, "assistantCancelExitWaitingItem")
        assert waiting is not None
        self.assertFalse(waiting.isVisible())
        self.assertEqual(waiting.height(), 0)
        normal_height = self.menu.property("height")
        self.menu.setProperty("visible", False)
        QTest.qWait(100)
        self.desktop.set_application_exit_waiting(True)
        self.open_menu()
        self.assertTrue(waiting.isVisible())
        self.assertGreater(waiting.height(), 0)
        self.assertAlmostEqual(self.menu.property("height") - normal_height,
                               waiting.height(), delta=1)
        self.click_root_item("assistantCancelExitWaitingItem")
        self.assertEqual(calls, ["cancel"])
        self.desktop.set_application_exit_waiting(False)
        self.open_menu()
        waiting = self.bubble.findChild(QQuickItem, "assistantCancelExitWaitingItem")
        assert waiting is not None
        self.assertIsNotNone(waiting)
        self.assertEqual(waiting.height(), 0)
        self.assertAlmostEqual(self.menu.property("height"), normal_height, delta=1)
        self.click_root_item("assistantSoundMenuItem")
        self.assertTrue(self.desktop.soundEnabled)
        self.open_menu()
        self.click_root_item("assistantExitApplicationItem")
        self.assertEqual(calls[-1], "exit")
        self.open_menu()
        with patch.object(assistant_desktop.QSystemTrayIcon,
                          "isSystemTrayAvailable", return_value=True):
            self.click_root_item("assistantHideMenuItem")
        self.assertEqual(calls[-1], "hide")
        self.assertFalse(self.bubble.isVisible())

    def test_root_labels_and_colors_in_empty_and_project_window_states(self):
        self.desktop.controller = SimpleNamespace(_bubble=self.bubble)
        self.desktop.set_application_exit_callback(lambda: None)
        window_menu = self.bubble.findChild(QObject, "assistantWindowListMenu")
        assert window_menu is not None
        for mode in ("light", "dark"):
            self.theme.set_mode(mode)
            self.open_menu()
            self.assertEqual(window_menu.property("title"), "窗口列表（暂无窗口）")
            self.assertFalse(window_menu.property("enabled"))
            popup = self.popup_window()
            items = [node for node in self.menu.findChildren(QQuickItem)
                     if node.inherits("QQuickMenuItem") and node.isVisible()
                     and popup.contentItem().isAncestorOf(node)]
            self.assertEqual([node.property("text") for node in items],
                             ["窗口列表（暂无窗口）", "查看计算队列", "气泡提示音",
                              "退出应用", "隐藏看板娘"])
            background = self.menu.findChild(QObject, "assistantContextMenu_background")
            assert background is not None
            for item in items:
                self.assertTrue(item.property("text").strip())
                label = item.property("contentItem")
                color = label.property("color")
                self.assertIsInstance(color, QColor)
                self.assertGreater(color.alpha(), 0)
                self.assertNotEqual(color, background.property("color"))
            self.menu.setProperty("visible", False)
            QTest.qWait(100)
        project = QQuickWindow()
        project.setTitle("工程 A · 膜系设计")
        project.show()
        self.addCleanup(project.close)
        self.open_menu()
        self.assertEqual(window_menu.property("title"), "窗口列表")
        self.assertTrue(window_menu.property("enabled"))
        self.menu.setProperty("currentIndex", 0)
        QTest.keyClick(self.popup_window(), Qt.Key.Key_Right)
        QTest.qWait(80)
        self.assertTrue(window_menu.property("opened"))
        rows = [node for node in self.bubble.findChildren(QQuickItem)
                if node.inherits("QQuickMenuItem")
                and node.objectName().startswith("assistantWindowListItem_")]
        self.assertTrue(any(node.property("text") == "工程 A · 膜系设计"
                            for node in rows))
        self.assertTrue(all(node.property("text").strip() for node in rows))

    def test_opens_only_after_right_release_and_stays_open(self):
        QTest.mousePress(self.bubble, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, QPoint(36, 36))
        QTest.qWait(40)
        self.assertFalse(self.menu.property("visible"))
        QTest.mouseRelease(self.bubble, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, QPoint(36, 36))
        QTest.qWait(120)
        self.assertTrue(self.menu.property("opened"))
        self.assertTrue(self.menu.property("focus"))
        self.assertEqual(self.assistant.calls, [])

    def test_escape_dismisses_and_repeated_right_click_reopens(self):
        for _ in range(3):
            self.open_menu()
            QTest.keyClick(self.popup_window(), Qt.Key.Key_Escape)
            QTest.qWait(80)
            self.assertFalse(self.menu.property("visible"))

    def test_window_list_menu_item_opens_submenu(self):
        windows = []
        for title in ("测试窗口一", "测试窗口二"):
            window = QQuickWindow()
            window.setTitle(title)
            window.show()
            windows.append(window)
        self.addCleanup(lambda: [window.close() for window in windows])
        self.desktop.controller = SimpleNamespace(_bubble=self.bubble)
        self.open_menu()
        self.menu.setProperty("currentIndex", 0)
        QTest.keyClick(self.popup_window(), Qt.Key.Key_Right)
        QTest.qWait(80)
        window_menu = self.bubble.findChild(QObject, "assistantWindowListMenu")
        assert window_menu is not None
        self.assertIsNotNone(window_menu)
        self.assertTrue(window_menu.property("opened"))
        self.assertEqual(self.assistant.calls, [])

    def test_window_list_rows_follow_light_and_dark_theme(self):
        window = QQuickWindow()
        window.setTitle("主题适配窗口")
        window.show()
        self.addCleanup(window.close)
        self.desktop.controller = SimpleNamespace(_bubble=self.bubble)
        self.open_menu()
        self.menu.setProperty("currentIndex", 0)
        QTest.keyClick(self.popup_window(), Qt.Key.Key_Right)
        QTest.qWait(80)

        identity = next(row["id"] for row in self.desktop.openWindows
                        if row["title"] == "主题适配窗口")
        row = self.bubble.findChild(QObject, "assistantWindowListItem_" + identity)
        self.assertIsNotNone(row)
        background = row.findChild(
            QObject, "assistantWindowListItem_background_" + identity)
        close_button = row.findChild(QObject, "assistantCloseWindowButton")
        close_pointer_area = row.findChild(QObject, "assistantCloseWindowPointerArea")
        close_background = row.findChild(QObject, "assistantCloseWindowButton_background")
        close_label = row.findChild(QObject, "assistantCloseWindowButtonLabel")
        label = row.findChild(QObject, "assistantWindowListItemLabel")
        menu_background = self.menu.findChild(QObject, "assistantContextMenu_background")
        self.assertIsNotNone(background)
        self.assertIsNotNone(close_button)
        self.assertIsNotNone(close_pointer_area)
        self.assertIsNotNone(close_background)
        self.assertIsNotNone(close_label)
        self.assertIsNotNone(label)
        assert menu_background is not None
        self.assertTrue(row.property("highlighted"))

        submenu_window = _visual_window_for(close_button)
        label_global_point = label.mapToGlobal(
            QPointF(label.width() / 2, label.height() / 2))
        label_point = QPoint(round(label_global_point.x() - submenu_window.x()),
                             round(label_global_point.y() - submenu_window.y()))
        QTest.mouseMove(submenu_window, label_point)
        QTest.qWait(100)
        self.assertFalse(close_pointer_area.property("containsMouse"))

        light = build_theme_snapshot("light")
        expected_light_background = light[
            "colorAccent" if row.property("highlighted") else
            "colorSurfaceRaised" if row.property("hovered") else "colorSurface"]
        expected_light_text = light[
            "colorTextOnAccent" if row.property("highlighted") else "colorText"]
        self.assertEqual(background.property("color"), QColor(expected_light_background))
        self.assertEqual(label.property("color"), QColor(expected_light_text))
        self.assertEqual(close_background.property("color"), QColor(0, 0, 0, 0))
        self.assertEqual(close_label.property("color"), QColor(light["colorTextOnAccent"]))
        self.assertEqual(menu_background.property("color"), QColor(light["colorSurface"]))

        close_global_point = close_button.mapToGlobal(
            QPointF(close_button.width() / 2, close_button.height() / 2))
        close_point = QPoint(round(close_global_point.x() - submenu_window.x()),
                             round(close_global_point.y() - submenu_window.y()))
        QTest.mouseMove(submenu_window, close_point)
        QTest.qWait(100)
        self.assertTrue(close_pointer_area.property("containsMouse"))
        self.assertEqual(close_background.property("color"), QColor(light["colorSurfaceRaised"]))
        self.assertEqual(close_label.property("color"), QColor(light["colorText"]))

        self.theme.set_mode("dark")
        QTest.qWait(60)
        dark = build_theme_snapshot("dark")
        self.assertEqual(background.property("color"), QColor(dark["colorAccent"]))
        self.assertEqual(label.property("color"), QColor(dark["colorTextOnAccent"]))
        self.assertEqual(close_background.property("color"), QColor(dark["colorSurfaceRaised"]))
        self.assertEqual(close_label.property("color"), QColor(dark["colorText"]))
        self.assertEqual(menu_background.property("color"), QColor(dark["colorSurface"]))
        self.assertEqual(close_background.property("radius"), dark["radiusSm"])

        QTest.mouseMove(submenu_window, label_point)
        QTest.qWait(100)
        self.assertFalse(close_pointer_area.property("containsMouse"))
        self.assertEqual(close_background.property("color"), QColor(0, 0, 0, 0))
        self.assertEqual(close_label.property("color"), QColor(dark["colorTextOnAccent"]))

    def test_clicking_window_name_activates_without_setting_always_on_top(self):
        class TrackingWindow(QQuickWindow):
            def __init__(self):
                super().__init__()
                self.setTitle("可激活窗口")
                self.raise_calls = 0
                self.activate_calls = 0

            def raise_(self):
                self.raise_calls += 1
                super().raise_()

            def requestActivate(self):
                self.activate_calls += 1
                super().requestActivate()

        window = TrackingWindow()
        window.show()
        self.addCleanup(window.close)
        self.desktop.controller = SimpleNamespace(_bubble=self.bubble)
        self.open_menu()
        self.menu.setProperty("currentIndex", 0)
        QTest.keyClick(self.popup_window(), Qt.Key.Key_Right)
        QTest.qWait(80)
        identity = next(row["id"] for row in self.desktop.openWindows
                        if row["title"] == "可激活窗口")
        row = self.bubble.findChild(QObject, "assistantWindowListItem_" + identity)
        self.assertIsNotNone(row)
        row.click()
        QTest.qWait(80)
        self.assertEqual((window.raise_calls, window.activate_calls), (1, 1))
        self.assertFalse(window.flags() & Qt.WindowStaysOnTopHint)

    def test_window_close_button_does_not_activate_row_and_captures_menu(self):
        class TrackingWindow(QQuickWindow):
            def __init__(self, title):
                super().__init__()
                self.setTitle(title)
                self.raise_calls = 0
                self.activate_calls = 0

            def raise_(self):
                self.raise_calls += 1
                super().raise_()

            def requestActivate(self):
                self.activate_calls += 1
                super().requestActivate()

        first = TrackingWindow("第一测试窗口")
        second = TrackingWindow("第二测试窗口")
        first.show()
        second.show()
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.desktop.controller = SimpleNamespace(_bubble=self.bubble)
        self.open_menu()
        self.menu.setProperty("currentIndex", 0)
        QTest.keyClick(self.popup_window(), Qt.Key.Key_Right)
        QTest.qWait(100)

        row_id = next(row["id"] for row in self.desktop.openWindows
                      if row["title"] == "第一测试窗口")
        row = self.bubble.findChild(QObject, "assistantWindowListItem_" + row_id)
        self.assertIsNotNone(row)
        close_button = row.findChild(QObject, "assistantCloseWindowButton")
        self.assertIsNotNone(close_button)
        pointer_area = close_button.findChild(QObject, "assistantCloseWindowPointerArea")
        self.assertIsNotNone(pointer_area)
        submenu_window = _visual_window_for(close_button)
        image = submenu_window.grabWindow()
        self.assertFalse(image.isNull())
        screenshot = Path(tempfile.gettempdir()) / f"assistant-window-list-{row_id}.png"
        self.assertTrue(image.save(str(screenshot)))

        global_point = close_button.mapToGlobal(
            QPointF(close_button.width() / 2, close_button.height() / 2))
        click_point = QPoint(round(global_point.x() - submenu_window.x()),
                             round(global_point.y() - submenu_window.y()))
        QTest.mouseMove(submenu_window, click_point)
        QTest.qWait(100)
        self.assertTrue(pointer_area.property("containsMouse"))
        QTest.mousePress(submenu_window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, click_point)
        QTest.qWait(30)
        self.assertTrue(row.property("closeButtonPressed"))
        QTest.mouseRelease(submenu_window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, click_point)
        QTest.qWait(100)
        self.assertFalse(first.isVisible())
        self.assertTrue(second.isVisible())
        self.assertEqual((first.raise_calls, first.activate_calls), (0, 0))
        self.assertFalse(any(row["title"] == "第一测试窗口" for row in self.desktop.openWindows))
        self.assertTrue(any(row["title"] == "第二测试窗口" for row in self.desktop.openWindows))
        print(f"Window list screenshot: {screenshot}")

    def test_left_click_keeps_chat_toggle_without_menu(self):
        QTest.mouseClick(self.bubble, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(36, 36))
        QTest.qWait(40)
        self.assertEqual(self.assistant.calls, ["chat"])
        self.assertFalse(self.menu.property("visible"))

    def test_left_drag_does_not_toggle_chat_or_open_menu(self):
        QTest.mousePress(self.bubble, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(15, 15))
        QTest.mouseMove(self.bubble, QPoint(45, 45), 20)
        QTest.mouseRelease(self.bubble, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(45, 45))
        QTest.qWait(40)
        self.assertEqual(self.assistant.calls, [])
        self.assertFalse(self.menu.property("visible"))

    def test_theme_field_selection_and_cursor_follow_light_and_dark_tokens(self):
        controls = (QML / "theme_controls").as_uri()
        component = QQmlComponent(self.engine)
        component.setData(
            f'import QtQuick\nimport QtQuick.Controls\nimport "{controls}" as ThemeControls\n'
            'Window { width: 320; height: 100; visible: true; '
            'ThemeControls.ThemeField { objectName: "themeFieldProbe"; '
            'text: "selected text"; focus: true; '
            'Component.onCompleted: select(0, text.length) } }'.encode(),
            QUrl.fromLocalFile(str(QML / "AssistantBubble.qml")),
        )
        self.assertFalse(component.isError(), component.errors())
        window = component.create()
        self.assertIsNotNone(window, component.errors())
        def cleanup_probe(component=component, probe=window):
            try:
                probe.close()
            except RuntimeError:
                pass
        self.addCleanup(cleanup_probe)
        QTest.qWait(60)
        field = window.findChild(QObject, "themeFieldProbe")
        cursor = field.findChild(QObject, "themeFieldCursor")
        self.assertIsNotNone(cursor)
        self.assertEqual(field.property("selectedText"), "selected text")
        self.assertGreater(cursor.property("width"), 0)
        self.assertGreater(cursor.property("height"), 0)

        for mode in ("light", "dark"):
            self.theme.set_mode(mode)
            QTest.qWait(40)
            tokens = build_theme_snapshot(mode)
            self.assertEqual(field.property("selectionColor"), QColor(tokens["colorSelection"]))
            self.assertEqual(field.property("selectedTextColor"), QColor(tokens["colorText"]))
            self.assertEqual(cursor.property("color"), QColor(tokens["colorText"]))
            self.assertTrue(cursor.property("visible"))

    def test_menu_position_stays_inside_screen_edges(self):
        screen = QGuiApplication.primaryScreen()
        bounds = screen.availableGeometry()
        menu_width, menu_height = 240, min(250, bounds.height())
        for x in (bounds.left(), bounds.right() - 72):
            for y in (bounds.top(), bounds.bottom() - 72):
                point = self.desktop.menuPosition(x, y, 72, 72, menu_width, menu_height)
                rect = QRect(x + point["x"], y + point["y"], menu_width, menu_height)
                self.assertTrue(bounds.contains(rect), (bounds, rect))


class AboutLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_small_offset_preserves_responsive_size_and_anchors(self):
        engine = QQmlApplicationEngine()
        bridge = AboutBridge(engine)
        self.assertEqual(bridge.property("internalVersion"), BUILD_IDENTITY.version)
        self.assertEqual(bridge.property("gitCommitLabel"),
                         "Git 提交：未记录（源码归档或未提供构建元数据）"
                         if BUILD_IDENTITY.git_sha is None
                         else f"Git 提交：{BUILD_IDENTITY.git_sha}")
        engine.rootContext().setContextProperty("bridge", bridge)
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML / "AboutWindow.qml")))
        self.assertFalse(component.isError(), component.errors())
        window = component.createWithInitialProperties({
            "initialMaximized": False,
            "internalVersion": bridge.property("internalVersion"),
            "gitCommitLabel": bridge.property("gitCommitLabel"),
        })
        self.assertIsInstance(window, QQuickWindow)
        window.show()
        QTest.qWait(80)
        try:
            visible_text = [item.property("text") for item in window.findChildren(QObject)]
            self.assertIn("内部版本：" + BUILD_IDENTITY.version, visible_text)
            self.assertIn(bridge.property("gitCommitLabel"), visible_text)
            character = next(item for item in window.findChildren(QObject)
                             if item.property("visualOffsetY") is not None)
            hero = window.findChild(QObject, "aboutHero")
            sheet = window.findChild(QObject, "aboutContentSheet")
            self.assertIsNotNone(hero)
            self.assertIsNotNone(sheet)
            image_widths = []
            for width, height in ((900, 620), (960, 700), (1364, 1024), (1920, 1080)):
                window.resize(width, height)
                QTest.qWait(40)
                self.assertEqual(character.property("visualOffsetY"), 8)
                image_height = character.property("height")
                sheet_y = sheet.property("y")
                rendered_height = window.height()
                self.assertGreaterEqual(rendered_height, 620)
                self.assertAlmostEqual(sheet_y, hero.property("height") - 30, delta=0.05)
                aspect = character.property("aspect")
                expected_image_width = min(
                    window.width() * 0.44, 740,
                    max(1, sheet_y / (0.998 - 0.008)) * aspect,
                )
                self.assertLessEqual(character.property("width"), expected_image_width + 0.05)
                image_widths.append(character.property("width"))
                self.assertAlmostEqual(image_height, character.property("width") / aspect, delta=0.05)
                self.assertAlmostEqual(
                    character.property("y") + image_height * 0.998,
                    sheet_y + image_height * 0.008 + 8,
                    delta=0.05,
                )
                self.assertGreaterEqual(character.property("y"), 0)
                self.assertLess(character.property("y") + image_height, sheet_y + 23)
                self.assertFalse(window.grabWindow().isNull())
            self.assertGreater(image_widths[-1], image_widths[0], image_widths)
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
