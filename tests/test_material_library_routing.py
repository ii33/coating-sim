import os
import tempfile
import time
import unittest
import ctypes
from pathlib import Path
from typing import Any, ClassVar, cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QTest

from coat_sim.core import materials
from coat_sim.ui.launcher import LauncherBridge, MODULES, QML_DIR
from coat_sim.ui.settings import apply_font_settings, configure_qt_dpi_scale
from coat_sim.ui import launcher as launcher_module
from tests.launcher_test_isolation import LauncherTestIsolation


class MaterialLibraryRoutingTests(unittest.TestCase):
    app: ClassVar[QGuiApplication]
    _isolation_dir: ClassVar[tempfile.TemporaryDirectory[str]]
    _isolation: ClassVar[LauncherTestIsolation]

    @classmethod
    def setUpClass(cls):
        cls._isolation_dir = tempfile.TemporaryDirectory(
            prefix="coat-material-routing-isolation-")
        cls._isolation = LauncherTestIsolation(
            cls._isolation_dir.name, launcher_module).apply()
        configure_qt_dpi_scale()
        existing_app = QGuiApplication.instance()
        cls.app = (cast(QGuiApplication, existing_app) if existing_app is not None
                   else QGuiApplication([]))
        apply_font_settings(app=cls.app)

    @classmethod
    def tearDownClass(cls):
        cls._isolation.stop()
        cls._isolation_dir.cleanup()

    def setUp(self):
        self.engine = QQmlApplicationEngine()
        from coat_sim.ui.theme_service import ThemeEngineBridge
        from coat_sim.ui.theme_tokens import build_theme_snapshot
        self.theme = ThemeEngineBridge(
            build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {},
            parent=self.engine)
        self.engine.rootContext().setContextProperty("Theme", self.theme)
        self.launcher = LauncherBridge(
            self.engine, software_mode=True, with_launcher=False,
            window_settings=self._isolation.create_settings())
        self.engine.rootContext().setContextProperty("launcher", self.launcher)
        self.engine.load(QUrl.fromLocalFile(str(QML_DIR / "LauncherWindow.qml")))
        self.launcher_window = cast(QQuickWindow, self.engine.rootObjects()[0])
        self.launcher_window.show()
        self.app.processEvents()
        QTest.qWait(40)

    def tearDown(self):
        handle = self._material_handle()
        if handle is not None:
            library = cast(Any, handle.bridge).materialLibrary
            if library is not None:
                library.waitForBackgroundTasks()
            handle.window.setProperty("allowClose", True)
            confirmation = handle.window.findChild(QObject, "assetMaterialCloseDialog")
            if confirmation is not None:
                confirmation.close()
            handle.window.close()
        self.launcher.shutdown()
        self.launcher_window.close()
        self.engine.deleteLater()
        self.app.processEvents()

    def _capture(self, window):
        screen = window.screen() or self.app.primaryScreen()
        self.assertIsNotNone(screen, "Qt did not expose a screen for GUI capture")
        return screen.grabWindow(int(window.winId()))

    def _visual_items(self, root):
        pending = [root.contentItem()] if hasattr(root, "contentItem") else [root]
        result = []
        seen = set()
        while pending:
            current = pending.pop()
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(current)
            if isinstance(current, QQuickItem):
                pending.extend(current.childItems())
            pending.extend(current.children())
        return result

    def _chrome(self, window):
        return next(item for item in self._visual_items(window)
                    if item.property("hostWindow") == window)

    def _chrome_button(self, chrome, index):
        names = ("windowChromeMinimizeButton", "windowChromeMaximizeButton",
                 "windowChromeCloseButton")
        return next(item for item in self._visual_items(chrome)
                    if isinstance(item, QQuickItem)
                    and item.objectName() == names[index])

    def _material_handle(self):
        # The legacy logical route now shares the global asset window.
        handles = self.launcher._handles_for("global_resources")
        return handles[-1] if handles else None

    def _select_optical_reference(self, library):
        optical_profile = next(
            (row for row in materials.builtin_material_profiles()
             if row.get("optical_constants")),
            None,
        )
        self.assertIsNotNone(optical_profile, "no built-in optical reference material is available")
        assert optical_profile is not None
        optical_profile_id = cast(str, optical_profile["id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
            library.loading or
            not any(row["id"] == optical_profile_id for row in library.entries)
        ):
            self.app.processEvents()
            QTest.qWait(20)
        self.assertTrue(any(row["id"] == optical_profile_id for row in library.entries),
                        "built-in material catalog did not finish loading")
        self.assertTrue(library.selectMaterial("builtin:" + optical_profile_id))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (library.loading or not library.opticalSets):
            self.app.processEvents()
            QTest.qWait(20)
        self.assertTrue(library.opticalSets, "built-in optical reference groups did not load")

    def _dialog_button(self, dialog, text):
        return next((item for item in self._visual_items(dialog.property("contentItem"))
                     if isinstance(item, QQuickItem) and item.property("text") == text), None)

    def _mouse_click_item(self, window, item):
        scene_center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(round(scene_center.x()), round(scene_center.y())))
        self.app.processEvents()

    def _native_pointer_drag(self, window, local_start, delta):
        self.assertEqual(self.app.platformName(), "windows",
                         "native top-level drag verification requires the Windows Qt backend")
        user32 = cast(Any, ctypes).windll.user32
        class Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        old_cursor = Point()
        if not user32.GetCursorPos(ctypes.byref(old_cursor)):
            return False
        old_foreground = user32.GetForegroundWindow()
        hwnd = int(window.winId())
        try:
            window.showNormal()
            window.raise_()
            window.requestActivate()
            user32.SetForegroundWindow(hwnd)
            self.assertTrue(QTest.qWaitForWindowActive(window, 1000),
                            "test-owned material window did not become active")
            start = window.mapToGlobal(local_start)
            self.assertTrue(user32.SetCursorPos(start.x(), start.y()))
            QTest.qWait(100)
            user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
            self.app.processEvents()
            for step in range(1, 6):
                x = start.x() + delta.x() * step // 5
                y = start.y() + delta.y() * step // 5
                self.assertTrue(user32.SetCursorPos(x, y))
                QTest.qWait(45)
                self.app.processEvents()
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
            QTest.qWait(120)
            self.app.processEvents()
        finally:
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            user32.SetCursorPos(old_cursor.x, old_cursor.y)
            if old_foreground:
                user32.SetForegroundWindow(old_foreground)
        return True

    def _native_double_click(self, window, local_point):
        self.assertEqual(self.app.platformName(), "windows",
                         "native double-click verification requires the Windows Qt backend")
        user32 = cast(Any, ctypes).windll.user32
        hwnd = int(window.winId())
        window.raise_()
        window.requestActivate()
        user32.SetForegroundWindow(hwnd)
        self.assertTrue(QTest.qWaitForWindowActive(window, 1000),
                        "test-owned material window did not become active")
        point = window.mapToGlobal(local_point)
        if not user32.SetCursorPos(point.x(), point.y()):
            return False
        QTest.qWait(60)
        for _ in range(2):
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            QTest.qWait(40)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            QTest.qWait(90)
            self.app.processEvents()
        QTest.qWait(120)
        self.app.processEvents()
        return True

    def test_material_library_is_not_a_launcher_entry_but_internal_route_still_opens(self):
        screenshot_dir = os.environ.get("COAT_PREVIEW_SCREENSHOT_DIR")
        if screenshot_dir:
            Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        QTest.qWait(80)
        launcher_image = self._capture(self.launcher_window)
        self.assertFalse(launcher_image.isNull(), "project launcher did not render")
        if screenshot_dir:
            self.assertTrue(launcher_image.save(str(Path(screenshot_dir) / "launcher-project-home.png")))

        material_entry = next((item for item in self._visual_items(self.launcher_window)
                               if isinstance(item, QQuickItem)
                               and item.objectName() == "launcherMenuEntry_materials"), None)
        self.assertIsNone(material_entry, "business module unexpectedly has a launcher entry")
        global_action_names = {item.objectName() for item in self._visual_items(self.launcher_window)
                               if isinstance(item, QQuickItem)}
        self.assertIn("launcherSettingsButton", global_action_names)
        self.assertIn("launcherAboutHelpButton", global_action_names)
        self.assertIn("materials", [spec.key for spec in MODULES],
                      "the internal module route was removed with its launcher entry")
        self.assertTrue(self.launcher.openWindow("materials"),
                        "internal material-library route should remain available")
        self.app.processEvents()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self._material_handle() is None:
            self.app.processEvents()
            QTest.qWait(20)
        handle = self._material_handle()
        self.assertIsNotNone(handle, "material alias did not open the unified asset library")
        self.assertTrue(handle.window.isVisible())
        self.assertEqual(handle.key, "global_resources")
        self.assertFalse(self.launcher._handles_for("materials"),
                         "material alias opened a second physical material window")
        assets = cast(Any, handle.bridge)
        library = assets.materialLibrary
        self.assertIsNotNone(library, "material alias did not retain a material editor")
        self.assertEqual(assets.libraryScope, "global")
        self.assertIsNone(assets.materialResources,
                          "global material route acquired a project-bound controller")
        self.assertEqual(self.launcher.open_window("materials"), handle.instance_id)
        self.assertEqual(self.launcher.open_window("global_resources"), handle.instance_id)
        self.assertIs(assets.materialLibrary, library,
                      "reopening the alias replaced its existing material draft owner")
        self.assertEqual(len(self.launcher._handles_for("global_resources")), 1)

        self._select_optical_reference(library)

        role_filter = handle.window.findChild(QObject, "materialRoleFilter")
        self.assertIsNotNone(role_filter, "material library filter is missing")
        self.assertFalse(role_filter.property("visible"),
                         "embedded editor duplicated the asset browser material filter")
        self.assertIsNotNone(handle.window.findChild(QObject, "resourceLibraryMaterialUse"))
        self.assertEqual(role_filter.property("count"), 2)
        self.assertGreaterEqual(role_filter.property("currentIndex"), 0)
        self.assertEqual(role_filter.property("currentIndex"), 0)
        self.assertEqual(role_filter.property("currentValue"), "film")
        self.assertEqual(library.roleFilter, "film")
        self.assertTrue(library.filteredEntries,
                        "the default film filter should show selectable materials")
        library.setRoleFilter("all")
        self.assertEqual(library.roleFilter, "film",
                         "a removed filter value must not leave the UI and model out of sync")
        QTest.qWait(80)
        library_image = self._capture(handle.window)
        self.assertFalse(library_image.isNull(), "unified asset material editor did not render")
        if screenshot_dir:
            self.assertTrue(library_image.save(str(Path(screenshot_dir) / "material-library.png")))

    def test_material_library_uses_project_chrome_and_guards_dirty_close(self):
        self.assertTrue(self.launcher.openWindow("materials"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self._material_handle() is None:
            self.app.processEvents()
            QTest.qWait(20)
        handle = self._material_handle()
        self.assertIsNotNone(handle)
        window = handle.window
        assets = cast(Any, handle.bridge)
        library = assets.materialLibrary
        self.assertIsNotNone(library)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and library.loading:
            self.app.processEvents()
            QTest.qWait(20)

        self._select_optical_reference(library)

        self.assertTrue(window.flags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(window.title(), "全局资产库")
        editor = window.findChild(QObject, "assetMaterialEditor")
        self.assertIsNotNone(editor)
        self.assertFalse(editor.property("modal"),
                         "material editor's modal overlay is blocking the host titlebar")
        self.assertEqual(editor.property("title"), "",
                         "the host titlebar already names the library")
        self.assertIs(window.property("editorInstance"), editor)
        self.assertTrue(editor.property("embeddedInAssetLibrary"))
        editor_parent = cast(QQuickItem, editor.property("parent"))
        self.assertAlmostEqual(float(editor.property("width")), editor_parent.width(), delta=1)
        self.assertAlmostEqual(float(editor.property("height")), editor_parent.height(), delta=1)
        chrome = self._chrome(window)
        close_button = self._chrome_button(chrome, 2)
        close_center = close_button.mapToItem(
            window.contentItem(), QPointF(close_button.width() / 2, close_button.height() / 2)
        )
        editor_origin = editor_parent.mapToItem(
            window.contentItem(), QPointF(float(editor.property("x")), float(editor.property("y"))))
        self.assertGreaterEqual(editor_origin.y(), close_center.y() + close_button.height() / 2,
                                "the embedded editor overlaps the top-right window controls")
        drag = window.findChild(QObject, "windowChromeTitleDrag")
        self.assertIsNotNone(drag, "WindowChrome title-drag MouseArea was not instantiated")
        drag_point = drag.mapToScene(QPointF(drag.width() / 2, drag.height() / 2))
        start_pos = QPoint(round(drag_point.x()), round(drag_point.y()))
        # Native pointer dispatch belongs to the Windows platform plugin. The
        # portable titlebar controls and dirty-close flow below still run here.
        if self.app.platformName() == "windows":
            start_x, start_y = window.x(), window.y()
            if self._native_pointer_drag(window, start_pos, QPoint(56, 34)):
                self.assertTrue((window.x(), window.y()) != (start_x, start_y),
                                "a native Windows mouse drag did not move the material top-level window")

            if self._native_double_click(window, start_pos):
                self.assertTrue(window.windowState() & Qt.WindowState.WindowMaximized,
                                "native titlebar double-click did not maximize the material window")
                if self._native_double_click(window, start_pos):
                    self.assertFalse(window.windowState() & Qt.WindowState.WindowMaximized,
                                     "native titlebar double-click did not restore the material window")

        self._mouse_click_item(window, self._chrome_button(chrome, 1))
        self.assertTrue(window.windowState() & Qt.WindowState.WindowMaximized)
        self._mouse_click_item(window, self._chrome_button(chrome, 1))
        self.assertFalse(window.windowState() & Qt.WindowState.WindowMaximized)
        self._mouse_click_item(window, self._chrome_button(chrome, 0))
        self.assertTrue(window.windowState() & Qt.WindowState.WindowMinimized)
        window.showNormal()
        self.app.processEvents()

        optical_sets = library.opticalSets
        self.assertTrue(any(row["notes"].strip() for row in optical_sets),
                        "built-in optical conditions are missing")
        reference_sections = library.referenceSections
        self.assertTrue(any(
            str(section.get("source", "")).strip()
            or str(section.get("url", "")).strip()
            or "http" in str(section.get("body", ""))
            for section in reference_sections
        ),
                        "built-in optical provenance is missing")

        library.newMaterial()
        library.setField("name", "Temporary titlebar test draft")
        self.assertTrue(library.dirty)
        self._mouse_click_item(window, self._chrome_button(chrome, 2))
        confirmation = window.findChild(QObject, "assetMaterialCloseDialog")
        self.assertIsNotNone(confirmation)
        self.assertTrue(confirmation.property("visible"),
                        "dirty close did not ask for confirmation")
        self.assertTrue(window.isVisible(), "close confirmation prematurely closed the host window")

        cancel = self._dialog_button(confirmation, "取消")
        self.assertIsNotNone(cancel)
        cancel.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Space)
        self.app.processEvents()
        self.assertFalse(confirmation.property("visible"))
        self.assertTrue(window.isVisible())
        self.assertTrue(library.dirty, "cancelling close discarded the material draft")
        self.assertEqual(library.draftData()["name"], "Temporary titlebar test draft")
        self.assertIs(assets.materialLibrary, library, "cancelling close replaced the draft owner")

        self._mouse_click_item(window, self._chrome_button(chrome, 2))
        discard = self._dialog_button(confirmation, "放弃并关闭")
        self.assertIsNotNone(discard)
        discard.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Space)
        self.app.processEvents()
        self.assertIsNone(self._material_handle(),
                          "discard-and-close did not unregister the host window")


if __name__ == "__main__":
    unittest.main()
