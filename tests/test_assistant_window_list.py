"""Qt/offline regressions for the desktop assistant's open-window menu."""
from pathlib import Path
import tempfile
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, Property, Qt, QUrl, Signal, Slot
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from coat_sim.ui import assistant, assistant_desktop

ROOT = Path(__file__).resolve().parents[1]


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


class FakeTrayDesktop(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.calls = []
        self._sound = False
        self._hidden = False
        self._volume = 70

    @Property(bool, notify=changed)
    def soundEnabled(self):
        return self._sound

    @Property(bool, notify=changed)
    def hidden(self):
        return self._hidden

    @Property(int, notify=changed)
    def notificationSoundVolumePercent(self):
        return self._volume

    @Slot(int, int, result="QVariantMap")
    def trayMenuPosition(self, _width, _height):
        return {"x": 20, "y": 20}

    @Slot()
    def showQueue(self):
        self.calls.append(("queue",))

    @Slot(bool)
    def setSoundEnabled(self, enabled):
        self._sound = bool(enabled)
        self.calls.append(("sound", self._sound))
        self.changed.emit()

    @Slot(int)
    def setNotificationSoundVolumePercent(self, value):
        self._volume = int(value)
        self.calls.append(("volume", self._volume))
        self.changed.emit()

    @Slot(int)
    def previewNotificationSound(self, value):
        self.calls.append(("preview", int(value)))

    @Slot()
    def openNotification(self):
        self.calls.append(("notification",))

    @Slot()
    def toggleVisibility(self):
        self._hidden = not self._hidden
        self.calls.append(("visibility", self._hidden))
        self.changed.emit()


class FakeControllerBridge(QObject):
    openRequested = Signal()
    toggleRequested = Signal()
    settingsRequested = Signal()
    closeRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled = True
        self.provider_calls = 0
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


class AssistantWindowListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.patchers = [
            patch.object(assistant_desktop, "load_ui_settings",
                         lambda: {"assistant_notification_sound": False}),
            patch.object(assistant_desktop, "QSoundEffect", None),
            patch.object(assistant_desktop, "CURSOR_PATH",
                         Path(self.temp_dir.name) / "cursor.json"),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)
        self.engine = QQmlApplicationEngine()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            self.desktop = assistant_desktop.DesktopAssistantBridge(self.engine)
        self.windows = []
        self.addCleanup(self._clean_windows)

    def _clean_windows(self):
        self.desktop.shutdown()
        for window in self.windows:
            try:
                window.close()
                window.deleteLater()
            except RuntimeError:
                pass
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def make_window(self, title, flags=None, show=True):
        window = QQuickWindow()
        window.setTitle(title)
        if flags is not None:
            window.setFlags(flags)
        self.windows.append(window)
        if show:
            window.show()
            self.app.processEvents()
        return window

    def test_external_window_titles_have_a_nonempty_display_fallback(self):
        self.desktop.set_external_windows([
            {"id": "empty-title", "title": ""},
            {"id": "space-title", "title": "   "},
            {"id": "null-title", "title": None},
            {"id": "project-title", "title": " 工程 A · 膜系设计 "},
        ])
        titles = {row["id"]: row["title"] for row in self.desktop.openWindows}
        self.assertEqual(titles["empty-title"], "未命名窗口")
        self.assertEqual(titles["space-title"], "未命名窗口")
        self.assertEqual(titles["null-title"], "未命名窗口")
        self.assertEqual(titles["project-title"], "工程 A · 膜系设计")

    def test_lists_only_visible_activatable_windows_and_refreshes(self):
        bubble = self.make_window("看板娘", Qt.Tool)
        self.desktop.controller = SimpleNamespace(_bubble=bubble)
        self.make_window("对话窗口")
        hidden = self.make_window("未打开窗口", show=False)
        popup = self.make_window("弹出菜单", Qt.Popup)
        self.make_window("设计窗口")

        self.desktop.refreshWindows()
        titles = {item["title"] for item in self.desktop.openWindows}
        self.assertEqual(titles, {"对话窗口", "设计窗口"})

        self.desktop.closeWindow(next(item["id"] for item in self.desktop.openWindows
                                       if item["title"] == "对话窗口"))
        self.app.processEvents()
        self.assertEqual([item["title"] for item in self.desktop.openWindows], ["设计窗口"])
        self.assertFalse(hidden.isVisible())
        self.assertTrue(popup.isVisible())

    def test_repeated_activation_raises_without_permanent_always_on_top(self):
        window = TrackingWindow("重复激活测试窗口")
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        self.desktop.refreshWindows()
        identity = next(item["id"] for item in self.desktop.openWindows
                        if item["title"] == "重复激活测试窗口")

        for _ in range(3):
            self.assertTrue(self.desktop.activateWindow(identity))

        self.assertEqual(window.raise_calls, 3)
        self.assertEqual(window.activate_calls, 3)
        self.assertFalse(window.flags() & Qt.WindowStaysOnTopHint)

    def test_controller_owns_one_window_group_and_closes_without_provider_work(self):
        engine = QQmlApplicationEngine()
        created = []

        def create_window(controller, name, _context):
            window = TrackingWindow(name)
            created.append(window)
            return object(), window

        with patch.object(assistant, "AssistantBridge", FakeControllerBridge), \
                patch.object(assistant_desktop.AssistantController, "_create_window", create_window):
            controller = assistant_desktop.AssistantController(engine)

        try:
            from typing import cast
            bridge = cast(FakeControllerBridge, controller.bridge)
            self.assertIs(controller.parent(), engine)
            self.assertIs(bridge.parent(), controller)
            self.assertIs(controller._context.parentContext(), engine.rootContext())
            self.assertEqual(len(created), 2)
            window_group = (controller._bubble, controller._chat)
            self.assertEqual(set(window_group), set(created))

            for _ in range(3):
                controller.show_chat()
                controller._ensure_windows()

            self.assertEqual((controller._bubble, controller._chat), window_group)
            self.assertEqual(len(created), 2)
            self.assertEqual(bridge.provider_calls, 0)
        finally:
            bridge = cast(FakeControllerBridge, controller.bridge)
            controller.shutdown()
            self.app.processEvents()
            self.assertEqual(bridge.shutdown_calls, 1)
            self.assertEqual(bridge.provider_calls, 0)
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()

    def test_notification_volume_control_saves_clamped_setting(self):
        saved = []
        with patch.object(assistant_desktop, "save_ui_settings",
                          lambda value: saved.append(dict(value)) or value):
            self.desktop.setNotificationSoundVolumePercent(140)

        self.assertEqual(self.desktop.notificationSoundVolumePercent, 100)
        self.assertEqual(saved[-1]["assistant_notification_volume_percent"], 100)
        self.assertFalse(saved[-1]["assistant_notification_sound"])

    def test_cancelled_dirty_close_stays_listed_then_last_window_disappears(self):
        component = QQmlComponent(self.engine)
        component.setData(
            b'import QtQuick\n'
            b'Window { title: "Dirty close test"; visible: true; '
            b'property bool allowClose: false; '
            b'onClosing: function(close) { close.accepted = allowClose } }',
            QUrl(),
        )
        self.assertFalse(component.isError(), component.errors())
        window = component.create()
        self.assertIsNotNone(window, component.errors())
        self.windows.append(window)
        self.app.processEvents()
        self.desktop.refreshWindows()
        identity = self.desktop.openWindows[0]["id"]

        self.assertFalse(self.desktop.closeWindow(identity))
        self.assertTrue(window.isVisible())
        self.assertEqual(len(self.desktop.openWindows), 1)

        window.setProperty("allowClose", True)
        self.assertTrue(self.desktop.closeWindow(identity))
        self.assertFalse(window.isVisible())
        self.assertEqual(self.desktop.openWindows, [])

    def test_context_menu_removes_only_the_four_requested_entries(self):
        self.engine.rootContext().setContextProperty("desktop", self.desktop)

        def create_window(name, context):
            qml_path = ROOT / "coat_sim" / "ui" / "qml" / name
            component = QQmlComponent(
                self.engine, QUrl.fromLocalFile(str(qml_path)))
            self.assertFalse(component.isError(), component.errors())
            window = component.create(context)
            self.assertIsNotNone(window, component.errors())
            return component, window

        self.desktop.controller = SimpleNamespace(
            bridge=SimpleNamespace(enabled=True),
            toggle_chat=lambda: None,
            _create_window=create_window,
        )
        self.desktop._create_tray()
        menu_window = self.desktop._tray_menu_window
        self.assertIsNotNone(menu_window)
        tray_menu = menu_window.findChild(QObject, "assistantTrayQuickMenu")
        self.assertIsNotNone(tray_menu)
        self.assertTrue(QMetaObject.invokeMethod(menu_window, "openAtCursor"))
        QTest.qWait(60)
        waiting = menu_window.findChild(QObject, "assistantCancelExitWaitingItem")
        self.assertEqual(waiting.property("height"), 0)
        self.desktop.set_application_exit_waiting(True)
        self.app.processEvents()
        self.assertGreater(waiting.property("height"), 0)
        self.desktop.set_application_exit_waiting(False)
        self.app.processEvents()
        self.assertEqual(waiting.property("height"), 0)
        self.assertTrue(QMetaObject.invokeMethod(menu_window, "closeMenu"))
        QTest.qWait(60)
        menu_item_names = (
            "assistantTrayQueueItem",
            "assistantTraySoundItem",
            "assistantTrayVolumeSlider",
            "assistantTrayNotificationItem",
            "assistantTrayVisibilityItem",
        )
        menu_items = [menu_window.findChild(QObject, name) for name in menu_item_names]
        self.assertTrue(all(item is not None for item in menu_items), menu_item_names)
        tray_labels = [tray_menu.findChild(QObject, name).property("text")
                       for name in menu_item_names if name != "assistantTrayVolumeSlider"]
        bubble_qml = (ROOT / "coat_sim/ui/qml/AssistantBubble.qml").read_text(encoding="utf-8")
        for label in ("展开会话列表", "查看 Token 统计", "查看待决事项", "设置"):
            self.assertNotIn(f'menu.addAction("{label}"',
                             (ROOT / "coat_sim/ui/assistant_desktop.py").read_text(encoding="utf-8"))
            self.assertNotIn(f'text: "{label}"', bubble_qml)
            self.assertNotIn(label, tray_labels)
        self.assertIn("窗口列表", bubble_qml)
        self.assertIn("查看计算队列", tray_labels)

    def test_shell_menu_actions_route_to_desktop_slots(self):
        engine = QQmlApplicationEngine()
        desktop = FakeTrayDesktop()
        engine.rootContext().setContextProperty("desktop", desktop)
        component = QQmlComponent(engine, QUrl.fromLocalFile(
            str(ROOT / "coat_sim/ui/qml/ShellMenu.qml")))
        self.assertFalse(component.isError(), component.errors())
        window = component.create()
        self.assertIsNotNone(window, component.errors())
        def cleanup():
            try:
                window.close()
            except RuntimeError:
                pass
            engine.deleteLater()
        self.addCleanup(cleanup)
        self.assertTrue(QMetaObject.invokeMethod(window, "openAtCursor"))

        menu = window.findChild(QObject, "assistantTrayQuickMenu")
        queue = window.findChild(QObject, "assistantTrayQueueItem")
        sound = window.findChild(QObject, "assistantTraySoundItem")
        preview = window.findChild(QObject, "assistantTrayPreviewSoundButton")
        notification = window.findChild(QObject, "assistantTrayNotificationItem")
        visibility = window.findChild(QObject, "assistantTrayVisibilityItem")
        slider = window.findChild(QObject, "assistantTrayVolumeSlider")
        self.assertTrue(all(item is not None for item in
                            (menu, queue, sound, preview, notification, visibility, slider)))

        for item in (queue, sound, notification, visibility):
            self.assertTrue(QMetaObject.invokeMethod(item, "click"), item.objectName())
        self.assertTrue(QMetaObject.invokeMethod(preview, "click"))
        slider.setProperty("value", 43)
        self.assertTrue(QMetaObject.invokeMethod(slider, "moved"))
        self.assertIn(("queue",), desktop.calls)
        self.assertIn(("sound", True), desktop.calls)
        self.assertIn(("notification",), desktop.calls)
        self.assertIn(("visibility", True), desktop.calls)
        self.assertIn(("preview", 70), desktop.calls)
        self.assertIn(("volume", 43), desktop.calls)

    def test_session_button_text_and_new_session_position(self):
        source = (ROOT / "coat_sim/ui/qml/AssistantChatWindow.qml").read_text(encoding="utf-8")
        session = ('ThemeControls.ThemeButton { objectName: "chatSessionListButton"; '
                   'text: "会话列表"; onClicked: sessionDrawer.open() }')
        new_session = ('ThemeControls.ThemeButton { objectName: "chatNewSessionButton"; '
                        'flat: true; text: "新会话"; enabled: !assistant.busy; '
                        'onClicked: chatWindow.startNewSession() }')
        fill = "Item { Layout.fillWidth: true }"
        self.assertIn(session, source)
        self.assertLess(source.index(session), source.index(new_session))
        self.assertEqual(source[source.index(session) + len(session):
                                source.index(fill, source.index(session))].strip(),
                         new_session)


if __name__ == "__main__":
    unittest.main()
