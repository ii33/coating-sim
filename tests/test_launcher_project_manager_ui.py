import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

import unittest
import tempfile
from pathlib import Path
from time import monotonic
from typing import ClassVar, cast

from PySide6.QtCore import (QCoreApplication, QEvent, QPoint, QPointF, QRectF, Qt,
                            QUrl, QObject, Property, Signal, Slot, QMetaObject)
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QSignalSpy, QTest

from coat_sim.ui.launcher import LauncherBridge, QML_DIR
from coat_sim.ui import launcher as launcher_module
from coat_sim.ui.settings import apply_font_settings, configure_qt_dpi_scale
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.launcher_test_isolation import LauncherTestIsolation


class FakeProjectLauncher(QObject):
    """A thin recent-project bridge; session ownership lives elsewhere."""

    projectsChanged = Signal()

    def __init__(self):
        super().__init__()
        self._entries = []
        self._busy = False
        self._error = ""
        self.calls = []

    @Property(list, notify=projectsChanged)
    def projectEntries(self):
        return [dict(entry) for entry in self._entries]

    @Property(bool, notify=projectsChanged)
    def projectBusy(self):
        return self._busy

    @Property(str, notify=projectsChanged)
    def projectError(self):
        return self._error

    @Slot(str, str, result=bool)
    def createProject(self, name, root_path):
        self.calls.append(("create", name, root_path))
        self._error = ""
        self._busy = True
        self.projectsChanged.emit()
        return True

    @Slot(str, result=bool)
    def openProject(self, descriptor_file):
        self.calls.append(("open", descriptor_file))
        self._error = ""
        self._busy = True
        self.projectsChanged.emit()
        return True

    @Slot(str)
    def removeRecentProject(self, descriptor_file):
        self.calls.append(("remove", descriptor_file))
        self._entries = [entry for entry in self._entries
                         if entry["descriptorFile"] != descriptor_file]
        self.projectsChanged.emit()

    @Slot()
    def refreshProjects(self):
        self.calls.append(("refresh",))
        self.projectsChanged.emit()

    def set_entries(self, entries):
        self._entries = []
        for entry in entries:
            row = dict(entry)
            row.setdefault("lastOpened", "2026-10-14T12:00:00+00:00")
            row.setdefault("available", True)
            row.setdefault("active", False)
            self._entries.append(row)
        self.projectsChanged.emit()

    def finish(self, *, entry=None, error=""):
        self._busy = False
        self._error = error
        if entry is not None:
            row = dict(entry)
            row.setdefault("lastOpened", "2026-10-14T12:00:00+00:00")
            row.setdefault("available", True)
            row.setdefault("active", False)
            descriptor_file = row["descriptorFile"]
            self._entries = [row, *(existing for existing in self._entries
                                     if existing["descriptorFile"] != descriptor_file)]
        self.projectsChanged.emit()


class LauncherProjectManagerUiTests(unittest.TestCase):
    app: QGuiApplication
    _isolation_dir: ClassVar[tempfile.TemporaryDirectory[str]]
    _isolation: ClassVar[LauncherTestIsolation]

    @classmethod
    def setUpClass(cls):
        cls._isolation_dir = tempfile.TemporaryDirectory(
            prefix="coat-launcher-project-manager-isolation-")
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
        self.theme = ThemeEngineBridge(
            build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {},
            parent=self.engine)
        self.engine.rootContext().setContextProperty("Theme", self.theme)
        self.launcher = LauncherBridge(
            self.engine, software_mode=True, with_launcher=False,
            window_settings=self._isolation.create_settings())
        self.projects = FakeProjectLauncher()
        self.engine.rootContext().setContextProperty("launcher", self.launcher)
        self.engine.rootContext().setContextProperty("launcherProjects", self.projects)
        self.engine.load(QUrl.fromLocalFile(str(QML_DIR / "LauncherWindow.qml")))
        roots = self.engine.rootObjects()
        self.assertTrue(roots, "LauncherWindow.qml did not create a window")
        self.window = cast(QQuickWindow, roots[0])
        self.window.show()
        self.window.requestActivate()
        QTest.qWait(60)
        self.app.processEvents()

    def tearDown(self):
        self.launcher.shutdown()
        self.window.hide()
        self.window.deleteLater()
        self.engine.deleteLater()
        self.launcher.deleteLater()
        self.projects.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _item(self, object_name):
        item = cast(QQuickItem | None, self.window.findChild(QQuickItem, object_name))
        if item is None:
            root = self.window.contentItem()
            pending = [] if root is None else [root]
            while pending:
                current = pending.pop()
                # Some Qt builds surface non-item handles in the QML child
                # graph; only real QQuickItems expose objectName()/childItems().
                if not isinstance(current, QQuickItem):
                    continue
                if current.objectName() == object_name:
                    return current
                pending.extend(current.childItems())
        self.assertIsNotNone(item, f"missing QML item {object_name}")
        return item

    @staticmethod
    def _ancestors(item):
        parent = item.parentItem()
        while parent is not None:
            yield parent
            parent = parent.parentItem()

    def _evaluate_qml(self, target, expression):
        result = QQmlExpression(QQmlEngine.contextForObject(target), target, expression)
        value, _ = result.evaluate()
        self.assertFalse(result.hasError(), result.error().toString())
        return value

    def _signal_spy(self, target, signature):
        index = target.metaObject().indexOfSignal(signature)
        self.assertGreaterEqual(index, 0, f"{target.objectName()} has no {signature}")
        spy = QSignalSpy(target, target.metaObject().method(index))
        self.assertTrue(spy.isValid())
        return spy

    def _click(self, object_name):
        if object_name.startswith(("launcherOpenRecentProject_", "launcherRemoveRecentProject_")):
            project_list = self._item("launcherProjectList")
            index = int(object_name.rsplit("_", 1)[1])
            self._evaluate_qml(project_list, f"positionViewAtIndex({index}, 4)")
            QTest.qWait(30)
            self.app.processEvents()
        item = self._item(object_name)
        self.assertTrue(item.isVisible(), f"{object_name} is hidden")
        self.assertTrue(item.property("enabled") is not False, f"{object_name} is disabled")
        position = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        self.assertTrue(QRectF(0, 0, self.window.width(), self.window.height()).contains(position),
                        f"{object_name} is outside the window")
        ancestor = item.parentItem()
        while ancestor is not None:
            if ancestor.clip():
                local_position = ancestor.mapFromScene(position)
                self.assertTrue(QRectF(0, 0, ancestor.width(), ancestor.height()).contains(
                    local_position), f"{object_name} is clipped by {ancestor.objectName()}")
            ancestor = ancestor.parentItem()
        point = QPoint(round(position.x()), round(position.y()))
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, point)
        self.app.processEvents()

    def _wait_for_handle(self, module_key):
        deadline = monotonic() + 5
        while monotonic() < deadline:
            handles = self.launcher._handles_for(module_key)
            if handles and handles[0].window.isVisible():
                return handles[0]
            self.app.processEvents()
            QTest.qWait(20)
        self.fail(f"global {module_key} window did not open")

    def _wait_for_no_handle(self, module_key):
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if not self.launcher._handles_for(module_key):
                return
            self.app.processEvents()
            QTest.qWait(20)
        self.fail(f"global {module_key} window did not close")

    def test_default_project_home_create_and_async_failure(self):
        self.assertTrue(self._item("launcherProjectManager").isVisible())
        self.assertTrue(self._item("launcherProjectEmptyStatus").isVisible())
        self.assertTrue(self._item("launcherSettingsButton").isVisible())
        self.assertTrue(self._item("launcherAboutHelpButton").isVisible())
        self.assertIsNone(self.window.findChild(QQuickItem, "launcherLegacyModulesButton"))

        self._click("launcherNewProjectButton")
        name = self._item("launcherProjectNameField")
        parent = self._item("launcherProjectParentPathField")
        name.setProperty("text", "Coating study")
        parent.setProperty("text", "/tmp/research")
        self._click("launcherConfirmCreateProjectButton")
        self.assertEqual(self.projects.calls[-1],
                         ("create", "Coating study", "/tmp/research/Coating study"))
        self.assertTrue(self.projects.projectBusy)
        self.assertFalse(self._item("launcherOpenProjectButton").property("enabled"))
        self.assertTrue(self._item("launcherProjectBusyStatus").isVisible())

        self.projects.finish(error="Project destination already exists")
        self.assertEqual(self._item("launcherProjectError").property("text"),
                         "Project destination already exists")
        self.assertTrue(self._item("launcherProjectError").isVisible())

    def test_open_existing_project_records_recent_descriptor_and_keeps_project_home_visible(self):
        field = self._item("launcherOpenProjectPathField")
        descriptor_file = "/tmp/existing-study/film stack.coatproj"
        field.setProperty("text", descriptor_file)
        self.assertEqual(field.property("accessibleLabel"), "已有工程文件")
        self.assertIn(".coatproj", field.property("accessibleDescription"))
        self.assertEqual(self._item("launcherOpenProjectButton").property("text"), "打开工程文件")
        self._click("launcherOpenProjectButton")
        self.assertEqual(self.projects.calls[-1], ("open", descriptor_file))
        self.assertTrue(self.projects.projectBusy)
        self.assertFalse(self._item("launcherOpenProjectButton").property("enabled"))

        last_opened = "2026-10-14T12:00:00+00:00"
        self.projects.finish(entry={
            "name": "Existing study",
            "descriptorFile": descriptor_file,
            "lastOpened": last_opened,
            "available": True,
            "active": False,
        })
        QTest.qWait(50)
        self.app.processEvents()
        row = self._item("launcherProjectEntry_0")
        self.assertTrue(row.isVisible())
        project_entries = self.projects.property("projectEntries")
        assert isinstance(project_entries, list)
        self.assertEqual(project_entries[0]["descriptorFile"], descriptor_file)
        self.assertEqual(project_entries[0]["name"], "Existing study")
        self.assertEqual(project_entries[0]["lastOpened"], last_opened)
        self.assertTrue(project_entries[0]["available"])
        details = self._evaluate_qml(row, "root.rowDetails(projectRow.modelData)")
        self.assertIn(descriptor_file, details)
        self.assertIn(last_opened, details)
        self.assertIsNone(self.window.findChild(QObject, "launcherRecentProjectDescriptor_0"))
        self.assertIsNone(self.window.findChild(QObject, "launcherRecentProjectState_0"))
        self._click("launcherOpenRecentProject_0")
        self.assertEqual(self.projects.calls[-1], ("open", descriptor_file))
        self.assertTrue(self._item("launcherProjectManager").isVisible())

    def test_open_project_dialog_accepts_cproj_url_and_cancel_preserves_field(self):
        dialog = self.window.findChild(QObject, "launcherOpenProjectFileDialog")
        assert dialog is not None
        self._evaluate_qml(dialog, "options = options | FileDialog.DontUseNativeDialog")
        filter_values = dialog.property("nameFilters")
        assert isinstance(filter_values, list)
        filters = " ".join(filter_values)
        self.assertIn("*.coatproj", filters)
        self.assertIn("*.COATPROJ", filters)

        with tempfile.TemporaryDirectory(prefix="launcher-project-file-") as tmp:
            folder = Path(tmp) / "Research 空格 25%20 #"
            folder.mkdir()
            chosen_file = folder / "film stack #2 100%20.coatproj"
            chosen_file.write_text("{}", encoding="utf-8")
            chosen_url = QUrl.fromEncoded(QUrl.fromLocalFile(str(chosen_file)).toEncoded())
            cancelled_file = folder / "cancelled choice.coatproj"
            cancelled_file.write_text("{}", encoding="utf-8")
            cancelled_url = QUrl.fromEncoded(QUrl.fromLocalFile(str(cancelled_file)).toEncoded())

            self.projects.finish(entry={
                "name": "Still open",
                "descriptorFile": "/tmp/still-open/active study.coatproj",
                "available": True,
            })
            previous_value = "/tmp/still-open/active study.coatproj"
            self._item("launcherOpenProjectPathField").setProperty("text", previous_value)
            calls_before_cancel = list(self.projects.calls)
            self.assertTrue(dialog.setProperty("selectedFile", cancelled_url))
            cancelled_accepted = self._signal_spy(dialog, "accepted()")
            cancelled_rejected = self._signal_spy(dialog, "rejected()")
            self._click("launcherBrowseOpenProjectButton")
            self.assertTrue(dialog.property("visible"))
            self.assertEqual(dialog.property("selectedFile"), cancelled_url)
            self.assertTrue(QMetaObject.invokeMethod(dialog, "reject"))
            QTest.qWait(40)
            self.assertFalse(dialog.property("visible"))
            self.assertEqual(cancelled_rejected.count(), 1)
            self.assertEqual(cancelled_accepted.count(), 0)
            self.assertEqual(self._item("launcherOpenProjectPathField").property("text"),
                             previous_value)
            self.assertEqual(self.projects.calls, calls_before_cancel)
            browse = self._item("launcherBrowseOpenProjectButton")
            self.assertEqual(self._item("launcherProjectManager").property(
                "focusReturnItem").objectName(), browse.objectName())

            # Setting a typed URL before open mirrors the fallback FileDialog
            # test pattern while still executing the production accepted handler.
            self.assertTrue(dialog.setProperty("selectedFile", chosen_url))
            accepted = self._signal_spy(dialog, "accepted()")
            self._click("launcherBrowseOpenProjectButton")
            self.assertTrue(dialog.property("visible"))
            self.assertEqual(dialog.property("selectedFile"), chosen_url)
            self.assertEqual(self._evaluate_qml(dialog, "launcher.localFilePath(selectedFile)"),
                             chosen_file.as_posix())
            self.assertTrue(QMetaObject.invokeMethod(dialog, "accept"))
            deadline = monotonic() + 2
            while monotonic() < deadline and dialog.property("visible"):
                self.app.processEvents()
                QTest.qWait(20)
            self.assertFalse(dialog.property("visible"))
            self.assertEqual(accepted.count(), 1,
                             "FileDialog acceptance should occur exactly once")
            self.assertEqual(self._item("launcherOpenProjectPathField").property("text"),
                             chosen_file.as_posix())
            self.assertEqual(self.projects.calls, [("open", chosen_file.as_posix())])
            self.assertTrue(self.projects.projectBusy)
            self.assertEqual(self._item("launcherProjectManager").property(
                "focusReturnItem").objectName(), browse.objectName())

    def test_manual_mixed_case_cproj_path_passes_to_project_bridge(self):
        mixed_case_path = "/tmp/study/custom descriptor.CoAtPrOj"
        self._item("launcherOpenProjectPathField").setProperty("text", mixed_case_path)
        self._click("launcherOpenProjectButton")
        self.assertEqual(self.projects.calls, [("open", mixed_case_path)])

    def test_project_file_picker_action_bar_stays_reachable_at_small_window_size(self):
        self.window.resize(640, 480)
        QTest.qWait(80)
        self.app.processEvents()
        for name in ("launcherOpenProjectPathField", "launcherBrowseOpenProjectButton",
                     "launcherNewProjectButton", "launcherOpenProjectButton"):
            item = self._item(name)
            self.assertTrue(item.isVisible(), name)
            bounds = item.mapRectToItem(self.window.contentItem(),
                                        QRectF(0, 0, item.width(), item.height()))
            self.assertGreaterEqual(bounds.left(), -1, f"{name} starts outside the window")
            self.assertLessEqual(bounds.right(), self.window.width() + 1,
                                 f"{name} is horizontally clipped")
        self.assertIn("工程文件", self._item("launcherProjectEmptyStatus").property("text"))
        open_dialog = self.window.findChild(QObject, "launcherOpenProjectFileDialog")
        assert open_dialog is not None
        filter_values = open_dialog.property("nameFilters")
        assert isinstance(filter_values, list)
        filters = " ".join(filter_values)
        self.assertIn("*.coatproj", filters)
        self.assertIn("*.COATPROJ", filters)

    def test_mixed_recent_home_shows_all_four_actions_without_initial_scroll_at_640x540(self):
        self.projects.set_entries([
            {"name": "宽带反射膜系方案与长名称可读性检查",
             "descriptorFile": "/work/research/Coating study/Multilayer optical design.coatproj",
             "lastOpened": "2026-10-15T06:40:00+00:00", "available": True,
             "active": False},
            {"name": "外置盘工程（当前不可用）",
             "descriptorFile": "/missing/external-drive/Archived named study.coatproj",
             "lastOpened": "2026-10-14T13:00:00+00:00", "available": False,
             "active": False},
        ])
        self.window.resize(640, 540)
        QTest.qWait(100)
        self.app.processEvents()

        manager = self._item("launcherProjectManager")
        scroll = self._item("launcherProjectManagerScroll")
        viewport = scroll.property("contentItem")
        self.assertIsInstance(viewport, QQuickItem)
        viewport = cast(QQuickItem, viewport)
        self.assertAlmostEqual(float(viewport.property("contentY")), 0.0, delta=0.5,
                               msg="the default home must start at the top of its scroll area")
        self.assertGreater(self._item("launcherRecentProjectName_0").height(), 0)
        self.assertGreater(self._item("launcherRecentProjectSummary_0").height(), 0,
                           "the second line should show the summary")
        self.assertIsNone(self.window.findChild(QObject, "launcherRecentProjectState_0"),
                          "the date belongs in hover details, not an extra row")

        footer = self._item("launcherDesignedByFooter")
        manager_rect = manager.mapRectToScene(
            QRectF(0, 0, manager.width(), manager.height()))
        footer_rect = footer.mapRectToScene(
            QRectF(0, 0, footer.width(), footer.height()))
        four_actions = (
            "launcherNewProjectButton",
            "launcherOpenProjectButton",
            "launcherSettingsButton",
            "launcherAboutHelpButton",
        )
        for object_name in four_actions:
            button = self._item(object_name)
            self.assertTrue(button.isVisible(), object_name)
            bounds = button.mapRectToScene(
                QRectF(0, 0, button.width(), button.height()))
            self.assertGreater(button.height(), 0, object_name)
            self.assertGreaterEqual(bounds.top(), manager_rect.top(), object_name)
            self.assertLess(bounds.bottom(), manager_rect.bottom(),
                            f"{object_name} falls outside the manager")
            self.assertLess(bounds.bottom(), footer_rect.top(),
                            f"{object_name} overlaps the Designed by footer")
            for ancestor in self._ancestors(button):
                if ancestor.clip():
                    clipped_bounds = button.mapRectToItem(
                        ancestor, QRectF(0, 0, button.width(), button.height()))
                    self.assertTrue(QRectF(0, 0, ancestor.width(), ancestor.height()).contains(
                        clipped_bounds), f"{object_name} is clipped by {ancestor.objectName()}")

        # The two workflow controls in the main scroll area must also fit in
        # the initial viewport; only recent rows use their own list scrolling.
        viewport_bounds = QRectF(0, 0, viewport.width(), viewport.height())
        for object_name in ("launcherNewProjectButton", "launcherOpenProjectButton"):
            button = self._item(object_name)
            button_bounds = button.mapRectToItem(
                viewport, QRectF(0, 0, button.width(), button.height()))
            self.assertTrue(viewport_bounds.contains(button_bounds),
                            f"{object_name} is not initially inside the scroll viewport")

    def test_lightweight_launcher_has_no_project_owner_or_business_menus(self):
        for name in (
            "launcherPackageButton",
            "launcherImportPackageForm",
            "launcherCloseProjectConfirm",
            "launcherRecoveryCandidatesController",
            "launcherLegacyModulesButton",
        ):
            self.assertIsNone(self.window.findChild(QObject, name), name)
        self.assertFalse(hasattr(self.projects, "host"))
        self.assertFalse(hasattr(self.projects, "activateProject"))

    def test_global_settings_and_about_buttons_reuse_and_reopen_module_windows(self):
        for module_key, button_name in (
            ("settings", "launcherSettingsButton"),
            ("about", "launcherAboutHelpButton"),
        ):
            button = self._item(button_name)
            self._click(button_name)
            first = self._wait_for_handle(module_key)
            first_id = first.instance_id
            self.assertTrue(first.window.isVisible())

            self._click(button_name)
            QTest.qWait(50)
            self.assertEqual(len(self.launcher._handles_for(module_key)), 1)
            self.assertEqual(self.launcher._handles_for(module_key)[0].instance_id, first_id)

            first.window.close()
            self._wait_for_no_handle(module_key)
            button.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
            QTest.keyClick(self.window, Qt.Key.Key_Space)
            self.app.processEvents()
            reopened = self._wait_for_handle(module_key)
            self.assertNotEqual(reopened.instance_id, first_id)
            self.assertTrue(reopened.window.isVisible())
            reopened.window.close()
            self._wait_for_no_handle(module_key)

    def test_recent_project_list_scrolls_and_opens_the_selected_descriptor(self):
        entries = [
            {"name": f"Study {index}",
             "descriptorFile": f"/tmp/study-{index}/project-{index}.coatproj",
             "lastOpened": f"2026-10-{14 - index:02d}T12:00:00+00:00",
             "available": True, "active": False}
            for index in range(8)
        ]
        self.projects.set_entries(entries)
        self.window.resize(640, 480)
        QTest.qWait(80)
        self.app.processEvents()
        project_list = self._item("launcherProjectList")
        self.assertGreater(float(project_list.property("contentHeight")), project_list.height())
        target = entries[-1]["descriptorFile"]
        max_scroll = max(0.0, float(project_list.property("contentHeight"))
                         - project_list.height())
        project_list.setProperty("contentY", max_scroll)
        QTest.qWait(40)
        self.app.processEvents()
        self._click("launcherOpenRecentProject_7")
        self.assertEqual(self.projects.calls[-1], ("open", target))

    def test_footer_is_reserved_and_manager_scrolls_at_small_sizes(self):
        self.theme._replace_snapshot(
            build_theme_snapshot("light", font_scale=9 / 12),
            1, "light", "light", "qa", "QA", {})
        captures = os.environ.get("LAUNCHER_CAPTURE_DIR")
        if captures:
            Path(captures).mkdir(parents=True, exist_ok=True)

        for width, height, label in ((1000, 700, "default"), (640, 480, "small")):
            self.window.resize(width, height)
            QTest.qWait(80)
            self.app.processEvents()

            scroll = self._item("launcherProjectManagerScroll")
            viewport = scroll.property("contentItem")
            self.assertIsInstance(viewport, QQuickItem)
            viewport = cast(QQuickItem, viewport)
            max_scroll = max(0.0, float(viewport.property("contentHeight")) - viewport.height())
            viewport.setProperty("contentY", max_scroll)
            QTest.qWait(30)
            self.app.processEvents()
            settings_button = self._item("launcherSettingsButton")
            about_button = self._item("launcherAboutHelpButton")
            footer = self._item("launcherDesignedByFooter")
            manager = self._item("launcherProjectManager")
            footer_top_left = footer.mapToScene(QPointF(0, 0))
            manager_top = manager.mapToScene(QPointF(0, 0))
            manager_bottom = manager_top.y() + manager.height()
            for button in (settings_button, about_button):
                button_top_left = button.mapToScene(QPointF(0, 0))
                button_bottom = button_top_left.y() + button.height()
                self.assertTrue(button.isVisible())
                self.assertGreater(button.height(), 0)
                self.assertLessEqual(button_bottom, manager_bottom + 1,
                                     f"{button.objectName()} is outside the manager at {width}x{height}")
                self.assertLessEqual(button_bottom, footer_top_left.y() + 1,
                                     f"{button.objectName()} overlaps footer at {width}x{height}")

            if captures:
                image = self.window.grabWindow()
                self.assertTrue(image.save(str(Path(captures) / f"launcher-{label}.png")))

    def test_large_explicit_font_and_density_keep_global_actions_fixed_and_project_controls_scrollable(self):
        self.theme._replace_snapshot(
            build_theme_snapshot("light", font_scale=17 / 12, density_scale=1.25),
            1, "light", "light", "qa", "QA", {})
        self.window.resize(640, 480)
        QTest.qWait(100)
        self.app.processEvents()

        scroll = self._item("launcherProjectManagerScroll")
        viewport = scroll.property("contentItem")
        self.assertIsInstance(viewport, QQuickItem)
        viewport = cast(QQuickItem, viewport)
        content_height = float(viewport.property("contentHeight"))
        self.assertGreater(content_height, scroll.height(),
                           "17 pt font at 125% density should use the existing vertical scroll")
        card = self._item("launcherProjectCard")
        empty_state = self._item("launcherProjectEmptyStatus")
        empty_bounds = empty_state.mapRectToItem(
            card, QRectF(0, 0, empty_state.width(), empty_state.height()))
        self.assertTrue(QRectF(0, 0, card.width(), card.height()).contains(empty_bounds),
                        "large-font empty-state text should remain inside its project card")
        manager = self._item("launcherProjectManager")
        footer = self._item("launcherDesignedByFooter")
        manager_bounds = manager.mapRectToScene(
            QRectF(0, 0, manager.width(), manager.height()))
        footer_bounds = footer.mapRectToScene(
            QRectF(0, 0, footer.width(), footer.height()))
        for object_name in ("launcherSettingsButton", "launcherAboutHelpButton"):
            button = self._item(object_name)
            button_bounds = button.mapRectToScene(
                QRectF(0, 0, button.width(), button.height()))
            self.assertTrue(button.isVisible())
            self.assertLessEqual(button_bounds.bottom(), manager_bounds.bottom() + 0.5)
            self.assertLess(button_bounds.bottom(), footer_bounds.top())

        max_scroll = max(0.0, content_height - viewport.height())
        viewport.setProperty("contentY", max_scroll)
        QTest.qWait(60)
        self.app.processEvents()
        viewport_bounds = QRectF(0, 0, viewport.width(), viewport.height())
        scroll_offset = float(viewport.property("contentY"))
        for object_name in (
            "launcherOpenProjectPathField",
            "launcherBrowseOpenProjectButton",
            "launcherNewProjectButton",
            "launcherOpenProjectButton",
        ):
            item = self._item(object_name)
            content_bounds = item.mapRectToItem(
                viewport, QRectF(0, 0, item.width(), item.height()))
            visible_bounds = content_bounds.translated(0, -scroll_offset)
            self.assertGreaterEqual(content_bounds.left(), -1,
                                    f"{object_name} starts beyond the narrow viewport")
            self.assertLessEqual(content_bounds.right(), viewport.width() + 1,
                                 f"{object_name} is horizontally clipped in the narrow viewport")
            self.assertTrue(viewport_bounds.contains(visible_bounds),
                            f"{object_name} is clipped after scrolling: "
                            f"item={visible_bounds}, viewport={viewport_bounds}")

        for object_name, module_key in (
            ("launcherSettingsButton", "settings"),
            ("launcherAboutHelpButton", "about"),
        ):
            self._click(object_name)
            opened = self._wait_for_handle(module_key)
            self.assertTrue(opened.window.isVisible())
            opened.window.close()
            self._wait_for_no_handle(module_key)


if __name__ == "__main__":
    unittest.main()
