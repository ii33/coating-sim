"""Launcher navigation is limited to project management and global actions."""

from __future__ import annotations

import os
from typing import ClassVar, cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

import unittest

from PySide6.QtCore import (QCoreApplication, QEvent, QPoint, QPointF, QRectF,
                            Qt, QUrl, QObject, Property, Signal, Slot)
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from coat_sim.ui.fonts import bundled_font_family
from coat_sim.ui.launcher import QML_DIR
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot


class LauncherRouterStub(QObject):
    """Capture global actions without starting real module windows in this QML test."""

    shutdownConfirmationRequested = Signal()
    launchError = Signal(str)

    def __init__(self):
        super().__init__()
        self.open_calls: list[str] = []
        self.instance_ids: dict[str, str] = {}

    @Property(int, constant=True)
    def backgroundWidth(self):
        return 1

    @Property(int, constant=True)
    def backgroundHeight(self):
        return 1

    @Property(QUrl, constant=True)
    def backgroundUrl(self):
        return QUrl()

    @Property(bool, constant=True)
    def softwareMode(self):
        return True

    @Slot(str, result=str)
    def openWindow(self, module_key):
        self.open_calls.append(module_key)
        return self.instance_ids.setdefault(module_key, f"global-{module_key}")

    @Slot()
    def closeLauncher(self):
        pass

    @Slot()
    def confirmShutdown(self):
        pass

    @Slot()
    def cancelShutdown(self):
        pass


class LauncherNavigationTests(unittest.TestCase):
    app: QGuiApplication
    font_family: ClassVar[str]

    @classmethod
    def setUpClass(cls):
        existing_app = QGuiApplication.instance()
        cls.app = (cast(QGuiApplication, existing_app) if existing_app is not None
                   else QGuiApplication([]))
        cls.font_family = bundled_font_family()
        if not cls.font_family:
            raise RuntimeError("bundled Noto font was not registered for launcher UI test")

    def setUp(self):
        self.engine = QQmlApplicationEngine()
        self.theme = ThemeEngineBridge(
            build_theme_snapshot("light", font_scale=9 / 12),
            0, "light", "light", "qa", "QA", {}, parent=self.engine)
        self.engine.rootContext().setContextProperty("Theme", self.theme)
        self.launcher = LauncherRouterStub()
        self.engine.rootContext().setContextProperty("launcher", self.launcher)
        self.engine.load(QUrl.fromLocalFile(str(QML_DIR / "LauncherWindow.qml")))
        roots = self.engine.rootObjects()
        self.assertTrue(roots, "LauncherWindow.qml did not create a window")
        self.window = cast(QQuickWindow, roots[0])
        self.window.show()
        self.window.requestActivate()
        QTest.qWait(50)
        self.app.processEvents()

    def tearDown(self):
        self.window.hide()
        self.window.deleteLater()
        self.engine.deleteLater()
        self.launcher.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _item(self, name: str) -> QQuickItem | None:
        item = self.window.findChild(QQuickItem, name)
        if item is not None:
            return item
        pending = [self.window.contentItem()]
        while pending:
            current = pending.pop()
            if current.objectName() == name:
                return current
            pending.extend(current.childItems())
        return None

    def _required_item(self, name: str) -> QQuickItem:
        item = self._item(name)
        self.assertIsNotNone(item, f"missing QML item {name}")
        return cast(QQuickItem, item)

    def _click(self, item: QQuickItem) -> None:
        position = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        point = QPoint(round(position.x()), round(position.y()))
        self.assertTrue(QRectF(0, 0, self.window.width(), self.window.height()).contains(position))
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, point)
        self.app.processEvents()

    def test_launcher_home_has_no_business_module_or_titlebar_menu_entry(self):
        self.assertEqual(self.window.property("caption"), "")
        self.assertTrue(self._required_item("launcherProjectManager").isVisible())
        for obsolete in (
            "launcherLegacyModulesButton",
            "launcherProjectHomeButton",
            "launcherReturnButton",
            "launcherMenuEntry_main",
            "launcherMenuEntry_process",
            "launcherMenuEntry_film",
            "launcherMenuEntry_ray_trace",
        ):
            self.assertIsNone(self._item(obsolete), f"obsolete module-menu path remains: {obsolete}")
        snapshot = cast(dict[str, str | float | int], self.theme.property("snapshot"))
        self.assertEqual(snapshot["fontFamily"], self.font_family)
        self.assertEqual(snapshot["fontSizeBody"], 9)
        self.assertTrue(self._required_item("launcherSettingsButton").isVisible())
        self.assertTrue(self._required_item("launcherAboutHelpButton").isVisible())

    def test_repeated_mouse_and_keyboard_global_actions_use_settings_and_about_routes(self):
        settings = self._required_item("launcherSettingsButton")
        about = self._required_item("launcherAboutHelpButton")

        self._click(settings)
        self._click(settings)
        settings.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
        QTest.keyClick(self.window, Qt.Key.Key_Space)
        self.app.processEvents()

        self._click(about)
        self._click(about)
        about.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
        QTest.keyClick(self.window, Qt.Key.Key_Space)
        self.app.processEvents()

        self.assertEqual(self.launcher.open_calls,
                         ["settings", "settings", "settings", "about", "about", "about"])
        self.assertEqual(self.launcher.instance_ids,
                         {"settings": "global-settings", "about": "global-about"})

    def test_minimum_window_keeps_fixed_global_actions_and_scrolls_project_controls(self):
        self.window.resize(640, 480)
        QTest.qWait(70)
        self.app.processEvents()

        scroll = self._required_item("launcherProjectManagerScroll")
        viewport = scroll.property("contentItem")
        self.assertIsInstance(viewport, QQuickItem)
        viewport = cast(QQuickItem, viewport)
        self.assertAlmostEqual(float(viewport.property("contentY")), 0.0, delta=0.5,
                               msg="minimum-size home should open at the top")

        footer = self._required_item("launcherDesignedByFooter")
        manager = self._required_item("launcherProjectManager")
        title = self._required_item("launcherProjectManagerTitle")
        window_bounds = QRectF(0, 0, self.window.width(), self.window.height())
        global_names = ("launcherSettingsButton", "launcherAboutHelpButton")

        def assert_global_actions_safe():
            manager_bounds = manager.mapRectToScene(
                QRectF(0, 0, manager.width(), manager.height()))
            footer_bounds = footer.mapRectToScene(
                QRectF(0, 0, footer.width(), footer.height()))
            title_bounds = title.mapRectToScene(
                QRectF(0, 0, title.width(), title.height()))
            bounds_by_name: dict[str, QRectF] = {}
            for object_name in global_names:
                button = self._required_item(object_name)
                button_bounds = button.mapRectToScene(
                    QRectF(0, 0, button.width(), button.height()))
                self.assertTrue(button.isVisible(), f"{object_name} is hidden at 640x480")
                self.assertGreater(button.height(), 0, object_name)
                self.assertTrue(window_bounds.contains(button_bounds),
                                f"{object_name} extends outside the window content")
                self.assertTrue(manager_bounds.contains(button_bounds),
                                f"{object_name} extends outside the launcher manager")
                self.assertLess(button_bounds.bottom(), footer_bounds.top(),
                                f"{object_name} overlaps the attribution footer")
                self.assertLess(title_bounds.bottom(), button_bounds.top(),
                                f"{object_name} is covered by or overlaps the title")
                parent = button.parentItem()
                while parent is not None:
                    if parent.clip():
                        clipped_bounds = button.mapRectToItem(
                            parent, QRectF(0, 0, button.width(), button.height()))
                        self.assertTrue(QRectF(0, 0, parent.width(), parent.height()).contains(
                            clipped_bounds),
                            f"{object_name} is clipped by {parent.objectName()}")
                    parent = parent.parentItem()
                bounds_by_name[object_name] = button_bounds
            return bounds_by_name

        # At the normal launcher scale, the screenshot-sized first viewport
        # must keep both global actions complete and above the fixed attribution.
        assert_global_actions_safe()

        # Exercise a real outer-scroll state as well. Large accessibility
        # sizing expands the project controls, while the global bar remains
        # fixed at the same screen coordinates.
        self.theme._replace_snapshot(
            build_theme_snapshot("light", font_scale=17 / 12, density_scale=1.25),
            1, "light", "light", "qa", "QA", {})
        QTest.qWait(100)
        self.app.processEvents()
        viewport.setProperty("contentY", 0.0)
        self.app.processEvents()
        fixed_before_scroll = assert_global_actions_safe()
        content_height = float(viewport.property("contentHeight"))
        max_scroll = max(0.0, content_height - viewport.height())
        self.assertGreater(max_scroll, 0.0,
                           "large-font project controls should exercise the outer scroll")
        viewport.setProperty("contentY", max_scroll)
        QTest.qWait(60)
        self.app.processEvents()
        self.assertGreater(float(viewport.property("contentY")), 0.0)
        fixed_after_scroll = assert_global_actions_safe()
        for object_name in global_names:
            self.assertEqual(fixed_after_scroll[object_name], fixed_before_scroll[object_name],
                             f"{object_name} moved when project content scrolled")

        # These workflow controls remain in the scrollable project area and
        # must be completely reachable after moving that viewport.
        viewport_bounds = viewport.mapRectToScene(
            QRectF(0, 0, viewport.width(), viewport.height()))
        for object_name in (
            "launcherOpenProjectPathField",
            "launcherBrowseOpenProjectButton",
            "launcherNewProjectButton",
            "launcherOpenProjectButton",
        ):
            control = self._required_item(object_name)
            self.assertTrue(control.isVisible(), f"{object_name} should remain scroll-reachable")
            visible_bounds = control.mapRectToScene(
                QRectF(0, 0, control.width(), control.height()))
            self.assertTrue(viewport_bounds.contains(visible_bounds),
                            f"{object_name} is clipped after scrolling: "
                            f"item={visible_bounds}, viewport={viewport_bounds}")
            parent = control.parentItem()
            while parent is not None:
                if parent.clip():
                    clip_bounds = parent.mapRectToScene(
                        QRectF(0, 0, parent.width(), parent.height()))
                    self.assertTrue(clip_bounds.contains(visible_bounds),
                                    f"{object_name} is clipped by {parent.objectName()}")
                parent = parent.parentItem()


if __name__ == "__main__":
    unittest.main()
