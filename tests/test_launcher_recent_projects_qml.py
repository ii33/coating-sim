import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

from pathlib import Path
import tempfile
from typing import Any, ClassVar, cast

from PySide6.QtCore import (QCoreApplication, QEvent, QPoint, QPointF, QRectF,
                            Qt, QUrl, QObject, Property, Signal, Slot)
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from coat_sim.ui.launcher import LauncherBridge, QML_DIR
from coat_sim.ui import launcher as launcher_module
from coat_sim.ui.settings import apply_font_settings, configure_qt_dpi_scale
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.launcher_test_isolation import LauncherTestIsolation


class _ProjectBridge(QObject):
    projectsChanged = Signal()

    def __init__(self):
        super().__init__()
        self._entries: list[dict[str, Any]] = []
        self._busy = False
        self._error = ""
        self.calls: list[tuple[Any, ...]] = []
        self._plan: dict = {}

    @Property(dict, notify=projectsChanged)
    def projectDeletionPlan(self):
        return dict(self._plan)

    @Slot(str, str, result=bool)
    def prepareRecentProjectDeletion(self, descriptor, identity):
        self._plan = {"descriptorFile": descriptor, "projectId": identity,
                      "token": "plan", "directoryPath": str(Path(descriptor).parent)}
        self.projectsChanged.emit()
        return True

    @Slot(str, str, str, result=bool)
    def trashRecentProjectWithPlan(self, descriptor, identity, token):
        assert token == "plan"
        return self.trashRecentProject(descriptor, identity)

    @Property(list, notify=projectsChanged)
    def projectEntries(self):
        return [dict(row) for row in self._entries]

    @Property(bool, notify=projectsChanged)
    def projectBusy(self):
        return self._busy

    @Property(str, notify=projectsChanged)
    def projectError(self):
        return self._error

    @Slot(str, result=bool)
    def openProject(self, descriptor):
        self.calls.append(("open", descriptor))
        return True

    @Slot(str, str, result=bool)
    def createProject(self, name, root):
        self.calls.append(("create", name, root))
        return True

    @Slot(str)
    def removeRecentProject(self, descriptor):
        self.calls.append(("remove", descriptor))
        self._entries = [row for row in self._entries
                         if row["descriptorFile"] != descriptor]
        self.projectsChanged.emit()

    @Slot(str, str, str, result=bool)
    def copyRecentProject(self, descriptor, identity, destination):
        self.calls.append(("copy", descriptor, destination))
        return True

    @Slot(str, str, str, result=bool)
    def exportRecentProject(self, descriptor, identity, destination):
        self.calls.append(("export", descriptor, destination))
        return True

    @Slot(str, str, result=bool)
    def trashRecentProject(self, descriptor, identity):
        self.calls.append(("trash", descriptor, identity))
        return True

    @Slot()
    def refreshProjects(self):
        self.calls.append(("refresh",))
        self.projectsChanged.emit()

    def set_entries(self, entries):
        self._entries = [dict(row) for row in entries]
        self.projectsChanged.emit()


class TestLauncherRecentProjectsQml:
    _isolation_dir: ClassVar[tempfile.TemporaryDirectory[str]]
    _isolation: ClassVar[LauncherTestIsolation]
    app: ClassVar[QGuiApplication]
    @classmethod
    def setup_class(cls):
        cls._isolation_dir = tempfile.TemporaryDirectory(prefix="coat-recent-qml-")
        cls._isolation = LauncherTestIsolation(
            cls._isolation_dir.name, launcher_module).apply()
        configure_qt_dpi_scale()
        existing = QGuiApplication.instance()
        cls.app = cast(QGuiApplication, existing) if existing else QGuiApplication([])
        apply_font_settings(app=cls.app)

    @classmethod
    def teardown_class(cls):
        cls._isolation.stop()
        cls._isolation_dir.cleanup()

    def setup_method(self):
        self.engine = QQmlApplicationEngine()
        self.theme = ThemeEngineBridge(
            build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {},
            parent=self.engine)
        self.engine.rootContext().setContextProperty("Theme", self.theme)
        self.launcher = LauncherBridge(
            self.engine, software_mode=True, with_launcher=False,
            window_settings=self._isolation.create_settings())
        self.project_bridge = _ProjectBridge()
        self.engine.rootContext().setContextProperty("launcher", self.launcher)
        self.engine.rootContext().setContextProperty("launcherProjects", self.project_bridge)
        self.engine.load(QUrl.fromLocalFile(str(QML_DIR / "LauncherWindow.qml")))
        roots = self.engine.rootObjects()
        assert roots, "LauncherWindow.qml did not create a window"
        self.window = cast(QQuickWindow, roots[0])
        self.window.show()
        self.window.requestActivate()
        QTest.qWait(80)
        self.app.processEvents()

    def teardown_method(self):
        self.launcher.shutdown()
        self.window.hide()
        self.window.deleteLater()
        self.engine.deleteLater()
        self.launcher.deleteLater()
        self.project_bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _item(self, object_name: str) -> QQuickItem:
        item = cast(QQuickItem | None, self.window.findChild(QQuickItem, object_name))
        if item is None:
            pending = [self.window.contentItem()]
            while pending:
                current = pending.pop()
                if current.objectName() == object_name:
                    item = current
                    break
                pending.extend(current.childItems())
        assert item is not None, f"missing QML item {object_name}"
        return item

    def _click(self, object_name: str):
        if object_name.startswith(("launcherOpenRecentProject_", "launcherRemoveRecentProject_",
                                   "launcherDeleteRecentProject_")):
            project_list = self._item("launcherProjectList")
            index = int(object_name.rsplit("_", 1)[1])
            expression = QQmlExpression(QQmlEngine.contextForObject(project_list), project_list,
                                        f"positionViewAtIndex({index}, 4)")
            expression.evaluate()
            assert not expression.hasError(), expression.error().toString()
            QTest.qWait(30)
            self.app.processEvents()
        item = self._item(object_name)
        assert item.isVisible(), f"{object_name} is hidden"
        assert item.property("enabled") is not False, f"{object_name} is disabled"
        position = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        assert QRectF(0, 0, self.window.width(), self.window.height()).contains(position)
        ancestor = item.parentItem()
        while ancestor is not None:
            if ancestor.clip():
                assert QRectF(0, 0, ancestor.width(), ancestor.height()).contains(ancestor.mapFromScene(position))
            ancestor = ancestor.parentItem()
        point = QPoint(round(position.x()), round(position.y()))
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, point)
        self.app.processEvents()

    def test_recent_rows_open_by_descriptor_and_missing_rows_explain_then_can_be_removed(self):
        live = str(Path(tempfile.gettempdir()) / "live.coatproj")
        missing = str(Path(tempfile.gettempdir()) / "missing.coatproj")
        self.project_bridge.set_entries([
            {"descriptorFile": live, "name": "Live", "lastOpened": "2026-10-14T12:00:00+00:00", "available": True},
            {"descriptorFile": missing, "name": "Missing", "lastOpened": "2026-10-13T12:00:00+00:00", "available": False},
        ])
        QTest.qWait(80)
        self.app.processEvents()

        self._click("launcherOpenRecentProject_0")
        assert self.project_bridge.calls[-1] == ("open", live)

        self._click("launcherOpenRecentProject_1")
        error = self._item("launcherProjectValidationError")
        assert error.isVisible()
        assert missing in error.property("text")
        assert self.project_bridge.calls[-1] == ("open", live)

        self._click("launcherRemoveRecentProject_1")
        assert self.project_bridge.calls[-1] == ("open", live)
        assert self._item("launcherConfirmRemoveProject").property("text") == "仅移出最近列表"
        assert not self._item("launcherDeleteProjectFilesChoice").property("enabled")
        self._click("launcherCancelRemoveProject")
        assert len(self.project_bridge._entries) == 2
        self._click("launcherRemoveRecentProject_1")
        self._click("launcherConfirmRemoveProject")
        assert self.project_bridge.calls[-1] == ("remove", missing)
        assert [row["descriptorFile"] for row in self.project_bridge._entries] == [live]

    def test_launcher_keeps_open_create_refresh_settings_help_and_drops_session_package_menus(self):
        self._click("launcherRefreshProjectsButton")
        assert self.project_bridge.calls[-1] == ("refresh",)

        path_field = self._item("launcherOpenProjectPathField")
        path_field.setProperty("text", "typed.coatproj")
        self._click("launcherOpenProjectButton")
        assert self.project_bridge.calls[-1] == ("open", "typed.coatproj")

        self._click("launcherNewProjectButton")
        dialog = self.window.findChild(QObject, "launcherCreateProjectDialog")
        assert dialog is not None and dialog.property("visible")
        self._item("launcherProjectNameField").setProperty("text", "New study")
        self._item("launcherProjectParentPathField").setProperty("text", tempfile.gettempdir())
        self._click("launcherConfirmCreateProjectButton")
        assert self.project_bridge.calls[-1][0:2] == ("create", "New study")

        assert self.window.findChild(QQuickItem, "launcherPackageButton") is None
        assert self.window.findChild(QQuickItem, "launcherCloseProjectConfirm") is None
        assert self.window.findChild(QQuickItem, "launcherOpenRecoveryCandidates_0") is None


    def test_two_line_rows_keep_name_left_actions_right_and_details_only_on_hover(self):
        name = "A very long full project name " * 4
        summary = "The full summary remains available in hover details " * 5
        descriptor = str(Path(tempfile.gettempdir()) / "Study" / "custom.coatproj")
        self.project_bridge.set_entries([{
            "descriptorFile": descriptor, "name": name, "summary": summary,
            "notes": "Full notes", "lastOpened": "2026-10-14T12:00:00+00:00",
            "available": True, "informationAvailable": True,
        }])
        QTest.qWait(60)
        for width, scale in ((640, .75), (1000, 1), (1380, 1.5), (640, 1.5), (640, .75)):
            self.theme._replace_snapshot(build_theme_snapshot("light", font_scale=scale),
                                         1, "light", "light", "qa", "QA", {})
            self.window.setWidth(width)
            QTest.qWait(50)
            row = self._item("launcherProjectEntry_0")
            title = self._item("launcherRecentProjectName_0")
            subtitle = self._item("launcherRecentProjectSummary_0")
            copy = self._item("launcherCopyRecentProject_0")
            export = self._item("launcherExportRecentProject_0")
            remove = self._item("launcherRemoveRecentProject_0")
            assert title.property("maximumLineCount") == 1
            assert subtitle.property("maximumLineCount") == 1
            assert title.mapToScene(QPointF()).x() < copy.mapToScene(QPointF()).x()
            assert copy.mapToScene(QPointF()).y() == export.mapToScene(QPointF()).y() == remove.mapToScene(QPointF()).y()
            assert subtitle.mapToScene(QPointF()).y() >= copy.mapToScene(QPointF(0, copy.height())).y()
            assert remove.mapToItem(row, QPointF(remove.width(), 0)).x() <= row.width()
            assert row.height() < 100 * scale
            assert title.width() >= 35 * scale
            if width == 640 and scale == 1.5:
                QTest.mouseMove(self.window, QPoint(self.window.width() - 12, self.window.height() - 12))
                QTest.qWait(650)
                self.window.grabWindow().save("/tmp/deposition-sim-recent-cards-large-font-preview.png")
        assert self.window.findChild(QObject, "launcherProjectInformationSection") is None
        assert self.window.findChild(QObject, "launcherRecentProjectDescriptor_0") is None
        assert self.window.findChild(QObject, "launcherRecentProjectState_0") is None
        context = QQmlEngine.contextForObject(row)
        expression = QQmlExpression(context, row, "root.rowDetails(projectRow.modelData)")
        details = expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        if isinstance(details, tuple):
            details = details[0]
        assert name in details and summary in details and descriptor in details and "Full notes" in details
        QTest.mouseMove(self.window, QPoint(self.window.width() - 12, self.window.height() - 12))
        QTest.qWait(650)
        scale_label = "2x" if self.window.devicePixelRatio() >= 1.5 else "1x"
        self.window.grabWindow().save("/tmp/deposition-sim-recent-cards-layout-" + scale_label + ".png")

    def test_copy_export_and_single_remove_with_optional_whole_folder_trash(self):
        descriptor = str(Path(tempfile.gettempdir()) / "Study" / "project.coatproj")
        self.project_bridge.set_entries([{
            "descriptorFile": descriptor, "name": "Study", "summary": "Goal",
            "lastOpened": "2026-10-14T12:00:00+00:00", "available": True,
            "informationAvailable": True,
            "projectId": "a" * 32,
        }])
        QTest.qWait(60)
        self._click("launcherCopyRecentProject_0")
        self._item("launcherProjectFileDestination").setProperty("text", str(Path(tempfile.gettempdir()) / "Copy"))
        self._click("launcherConfirmProjectFileAction")
        assert self.project_bridge.calls[-1] == ("copy", descriptor, str(Path(tempfile.gettempdir()) / "Copy"))
        self._click("launcherExportRecentProject_0")
        self._item("launcherProjectFileDestination").setProperty("text", str(Path(tempfile.gettempdir()) / "export.cproject"))
        self._click("launcherConfirmProjectFileAction")
        assert self.project_bridge.calls[-1] == ("export", descriptor, str(Path(tempfile.gettempdir()) / "export.cproject"))
        before = list(self.project_bridge.calls)
        self._click("launcherRemoveRecentProject_0")
        assert not self._item("launcherDeleteProjectFilesChoice").property("checked")
        assert self.window.findChild(QQuickItem, "launcherDeleteRecentProject_0") is None
        assert self._item("launcherDeleteProjectFilesChoice").property("text") == "同时将整个工程文件夹移入回收站"
        assert self.window.findChild(QQuickItem, "launcherDeleteProjectScopeChoice") is None
        assert self._item("launcherCancelRemoveProject").hasActiveFocus()
        self._click("launcherDeleteProjectFilesChoice")
        assert "包含文件夹内全部内容" in self._item("launcherRemoveProjectScope").property("text")
        assert "Study" in self._item("launcherRemoveProjectName").property("text")
        assert descriptor in self._item("launcherRemoveProjectDescriptor").property("text")
        assert str(Path(descriptor).parent) in self._item("launcherRemoveProjectScope").property("text")
        assert self._item("launcherConfirmRemoveProject").property("text") == "移入回收站并移除"
        self._click("launcherCancelRemoveProject")
        assert self.project_bridge.calls == before and self.project_bridge._entries
        self._click("launcherRemoveRecentProject_0")
        self._click("launcherDeleteProjectFilesChoice")
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.app.processEvents()
        assert self.project_bridge.calls == before and self.project_bridge._entries
        self._click("launcherRemoveRecentProject_0")
        self._click("launcherDeleteProjectFilesChoice")
        self._click("launcherConfirmRemoveProject")
        assert self.project_bridge.calls[-1] == ("trash", descriptor, "a" * 32)
        assert self.project_bridge._entries
        confirm = self._item("launcherConfirmRemoveProject")
        repeated = QQmlExpression(QQmlEngine.contextForObject(confirm), confirm, "root.confirmRemove()")
        repeated.evaluate()
        assert not repeated.hasError(), repeated.error().toString()
        assert self.project_bridge.calls.count(("trash", descriptor, "a" * 32)) == 1
        self._click("launcherRemoveRecentProject_0")
        assert self._item("launcherConfirmRemoveProject").property("text") == "仅移出最近列表"
        self._click("launcherCancelRemoveProject")

    def test_metadata_is_rendered_as_plain_text(self):
        self.project_bridge.set_entries([{
            "descriptorFile": "/tmp/Study/project.coatproj", "name": "<b>Project</b>",
            "summary": "<a href='https://example.invalid'>Summary</a>",
            "notes": "<img src='https://example.invalid/image'>", "available": True,
            "informationAvailable": True, "lastOpened": "2026-10-14T12:00:00+00:00",
        }])
        QTest.qWait(60)
        for object_name in ("launcherRecentProjectName_0", "launcherRecentProjectSummary_0"):
            item = self._item(object_name)
            expression = QQmlExpression(QQmlEngine.contextForObject(item), item, "textFormat === Text.PlainText")
            assert expression.evaluate()[0] is True
            assert not expression.hasError()
        row = self._item("launcherProjectEntry_0")
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row,
            "detailsText.textFormat === Text.PlainText && detailsText.text.indexOf('<b>Project</b>') >= 0")
        assert expression.evaluate()[0] is True and not expression.hasError()

    def test_hover_details_really_paint_full_text_and_dismiss_on_leave(self):
        name = "Optical coating research project"
        summary = "Compare broadband coating performance and verify deposition uniformity."
        descriptor = "/tmp/Research study/custom-project.coatproj"
        self.theme._replace_snapshot(build_theme_snapshot("light", font_scale=.75),
                                     1, "light", "light", "qa", "QA", {})
        self.project_bridge.set_entries([{
            "descriptorFile": descriptor, "name": name, "summary": summary,
            "notes": "Full notes remain visible here.", "available": True,
            "informationAvailable": True, "lastOpened": "2026-10-16T01:00:00+00:00",
        }])
        self.window.resize(1000, 700)
        QTest.mouseMove(self.window, QPoint(980, 680))
        QTest.qWait(650)
        row = self._item("launcherProjectEntry_0")
        position = row.mapToScene(QPointF(24, row.height() / 2))
        QTest.mouseMove(self.window, QPoint(round(position.x()), round(position.y())))
        QTest.qWait(900)
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row,
                                    "rowDetailsTip.visible && rowDetailsTip.opened")
        assert expression.evaluate()[0] is True and not expression.hasError()
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row, "detailsText")
        content = expression.evaluate()[0]
        assert isinstance(content, QQuickItem)
        text = content.property("text")
        assert name in text and summary in text and descriptor in text
        grabbed = self.window.grabWindow()
        origin = content.mapToScene(QPointF())
        ratio = grabbed.width() / self.window.width()
        left, top = round((origin.x() + 2) * ratio), round((origin.y() + 2) * ratio)
        right = min(grabbed.width(), round((origin.x() + content.width() - 2) * ratio))
        bottom = min(grabbed.height(), round((origin.y() + content.height() - 2) * ratio))
        dark_pixels = sum(1 for y in range(max(0, top), bottom) for x in range(max(0, left), right)
                          if max(grabbed.pixelColor(x, y).red(), grabbed.pixelColor(x, y).green(),
                                 grabbed.pixelColor(x, y).blue()) < 110)
        assert dark_pixels > 200, "visible tooltip must paint text, not an empty surface"
        scale_label = "2x" if self.window.devicePixelRatio() >= 1.5 else "1x"
        grabbed.save("/tmp/deposition-sim-recent-cards-hover-" + scale_label + ".png")
        QTest.mouseMove(self.window, QPoint(980, 680))
        QTest.qWait(300)
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row, "!rowDetailsTip.visible")
        assert expression.evaluate()[0] is True and not expression.hasError()

    def test_long_hover_details_are_screen_bounded_and_scroll_at_maximum_font(self):
        self.theme._replace_snapshot(build_theme_snapshot("light", font_scale=8 / 3),
                                     1, "light", "light", "qa", "QA", {})
        self.window.resize(1000, 700)
        self.project_bridge.set_entries([{
            "descriptorFile": "/tmp/Research study/a complete exact project path.coatproj",
            "name": "Complete name " * 7, "summary": "S" * 20000, "notes": "N" * 20000,
            "available": True, "informationAvailable": True,
            "lastOpened": "2026-10-16T01:00:00+00:00",
        }])
        QTest.qWait(100)
        row = self._item("launcherProjectEntry_0")
        position = row.mapToScene(QPointF(24, row.height() / 2))
        QTest.mouseMove(self.window, QPoint(round(position.x()), round(position.y())))
        QTest.qWait(900)
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row,
            "rowDetailsTip.visible && rowDetailsTip.width >= 220 && rowDetailsTip.width <= 360"
            " && rowDetailsTip.height <= 360"
            " && rowDetailsTip.x >= rowDetailsTip.boundsLeft && rowDetailsTip.y >= rowDetailsTip.boundsTop"
            " && rowDetailsTip.x + rowDetailsTip.width <= rowDetailsTip.boundsRight + 1"
            " && rowDetailsTip.y + rowDetailsTip.height <= rowDetailsTip.boundsBottom + 1"
            " && detailsScroll.contentHeight > detailsScroll.availableHeight"
            " && detailsText.text.indexOf('a complete exact project path.coatproj') >= 0")
        assert expression.evaluate()[0] is True and not expression.hasError(), expression.error().toString()
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row,
                                    "detailsScroll.contentItem.contentY = detailsScroll.contentHeight - detailsScroll.availableHeight")
        expression.evaluate()
        QTest.qWait(50)
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row, "detailsScroll.contentItem.contentY > 0")
        assert expression.evaluate()[0] is True and not expression.hasError()

        self.theme._replace_snapshot(build_theme_snapshot("light", font_scale=.75),
                                     2, "light", "light", "qa", "QA", {})
        for window_width in (640, 480):
            QTest.mouseMove(self.window, QPoint(10, 10))
            QTest.qWait(50)
            self.window.resize(window_width, 480)
            QTest.qWait(80)
            project_list = self._item("launcherProjectList")
            expression = QQmlExpression(QQmlEngine.contextForObject(project_list), project_list,
                                        "projectList.contentY = 0")
            expression.evaluate()
            row = self._item("launcherProjectEntry_0")
            position = row.mapToScene(QPointF(24, min(row.height() / 2, project_list.height() / 2)))
            QTest.mouseMove(self.window, QPoint(round(position.x()), round(position.y())))
            QTest.qWait(850)
            if window_width == 480:
                expression = QQmlExpression(QQmlEngine.contextForObject(row), row,
                    "!rowDetailsTip.sidePlacement && !rowDetailsTip.visible")
                assert expression.evaluate()[0] is True and not expression.hasError()
                continue
            extra_bound = " && rowDetailsTip.sidePlacement"
            expression = QQmlExpression(QQmlEngine.contextForObject(row), row,
                "rowDetailsTip.visible && rowDetailsTip.opened && rowDetailsTip.width >= 220"
                " && rowDetailsTip.width <= 360 && rowDetailsTip.height <= 360"
                " && rowDetailsTip.x >= rowDetailsTip.boundsLeft && rowDetailsTip.y >= rowDetailsTip.boundsTop"
                " && rowDetailsTip.x + rowDetailsTip.width <= rowDetailsTip.boundsRight + 1"
                " && rowDetailsTip.y + rowDetailsTip.height <= rowDetailsTip.boundsBottom + 1"
                + extra_bound)
            bounds_ok = expression.evaluate()[0]
            details = QQmlExpression(QQmlEngine.contextForObject(row), row,
                                     "JSON.stringify({width: rowDetailsTip.width, side: rowDetailsTip.sideSpace,"
                                     " x: rowDetailsTip.x, y: rowDetailsTip.y, right: rowDetailsTip.boundsRight,"
                                     " bottom: rowDetailsTip.boundsBottom})")
            assert bounds_ok is True and not expression.hasError(), details.evaluate()[0]

    def test_details_overlay_does_not_block_underlying_clicks_and_closes_on_interaction(self):
        self.window.resize(1000, 520)
        descriptor = "/tmp/Research study/tooltip-click-through.coatproj"
        self.project_bridge.set_entries([{
            "descriptorFile": descriptor, "name": "Tooltip click-through",
            "summary": "Details should remain readable without covering launcher controls.",
            "notes": "Long metadata keeps the passive overlay over lower controls. " * 50,
            "available": True, "informationAvailable": True,
            "lastOpened": "2026-10-16T01:00:00+00:00",
        }])
        path_field = self._item("launcherOpenProjectPathField")
        path_field.setProperty("text", "clicked-through.coatproj")
        QTest.qWait(80)
        row = self._item("launcherProjectEntry_0")
        position = row.mapToScene(QPointF(24, row.height() / 2))
        QTest.mouseMove(self.window, QPoint(round(position.x()), round(position.y())))
        QTest.qWait(850)
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row,
                                    "rowDetailsTip.visible && rowDetailsTip.opened")
        assert expression.evaluate()[0] is True and not expression.hasError()

        # Keep the details beside the recent row, outside the lower form
        # controls, and prove the real button remains clickable while it shows.
        target = self._item("launcherOpenProjectButton")
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row, "rowDetailsTip")
        tip = expression.evaluate()[0]
        assert tip is not None
        target_origin = target.mapToScene(QPointF())
        tip_rect = QRectF(float(tip.property("x")), float(tip.property("y")),
                          float(tip.property("width")), float(tip.property("height")))
        target_rect = QRectF(target_origin.x(), target_origin.y(), target.width(), target.height())
        assert not tip_rect.intersects(target_rect), "hover details should stay outside launcher controls"
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row, "!rowDetailsTip.enabled")
        assert expression.evaluate()[0] is True and not expression.hasError()
        click_center = target_rect.center()
        # Exercise the hard case too: even if a future narrow layout puts the
        # visible overlay over a control, its non-interactive surface must let
        # the actual click reach that control.
        tip.setProperty("x", click_center.x() - float(tip.property("width")) / 2)
        tip.setProperty("y", click_center.y() - float(tip.property("height")) / 2)
        QTest.qWait(30)
        overlapping_rect = QRectF(float(tip.property("x")), float(tip.property("y")),
                                  float(tip.property("width")), float(tip.property("height")))
        assert tip.property("visible") and overlapping_rect.contains(click_center)
        click_point = QPoint(round(click_center.x()), round(click_center.y()))
        QTest.mousePress(self.window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, click_point)
        self.app.processEvents()
        assert not tip.property("visible"), "dismiss on press, before releasing the underlying control"
        QTest.mouseRelease(self.window, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, click_point)
        self.app.processEvents()
        assert self.project_bridge.calls[-1] == ("open", "clicked-through.coatproj")
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row, "!rowDetailsTip.visible")
        assert expression.evaluate()[0] is True and not expression.hasError()

        # Re-entering a row shows its own current details, but leave dismisses
        # them immediately so a later click or scroll is never delayed.
        self.project_bridge.set_entries([{
            "descriptorFile": "/tmp/Research study/new-row.coatproj",
            "name": "Current row", "summary": "fresh details", "notes": "", "available": True,
        }])
        row = self._item("launcherProjectEntry_0")
        position = row.mapToScene(QPointF(24, row.height() / 2))
        QTest.mouseMove(self.window, QPoint(round(position.x()), round(position.y())))
        QTest.qWait(850)
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row,
                                    "rowDetailsTip.text.indexOf('Current row') >= 0"
                                    " && rowDetailsTip.text.indexOf('Tooltip click-through') < 0")
        assert expression.evaluate()[0] is True and not expression.hasError()
        self.project_bridge.set_entries([{
            "descriptorFile": "/tmp/Research study/replaced-row.coatproj",
            "name": "Replacement row", "summary": "replacement details", "notes": "", "available": True,
        }])
        QTest.qWait(40)
        row = self._item("launcherProjectEntry_0")
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row,
                                    "!rowDetailsTip.visible || (rowDetailsTip.text.indexOf('Replacement row') >= 0"
                                    " && rowDetailsTip.text.indexOf('Current row') < 0)")
        assert expression.evaluate()[0] is True and not expression.hasError()
        QTest.mouseMove(self.window, QPoint(980, 500))
        QTest.qWait(50)
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row, "!rowDetailsTip.visible")
        assert expression.evaluate()[0] is True and not expression.hasError()

        QTest.mouseMove(self.window, QPoint(round(position.x()), round(position.y())))
        QTest.qWait(850)
        self._click("launcherRemoveRecentProject_0")
        remove_dialog = self.window.findChild(QObject, "launcherRemoveProjectDialog")
        assert remove_dialog is not None and remove_dialog.property("visible")
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row, "!rowDetailsTip.visible")
        assert expression.evaluate()[0] is True and not expression.hasError()
        self._click("launcherCancelRemoveProject")

        self.project_bridge.set_entries([{
            "descriptorFile": f"/tmp/Research study/scroll-{index}.coatproj",
            "name": f"Scrollable row {index}", "summary": "", "notes": "", "available": True,
        } for index in range(12)])
        QTest.qWait(80)
        row = self._item("launcherProjectEntry_0")
        position = row.mapToScene(QPointF(24, row.height() / 2))
        QTest.mouseMove(self.window, QPoint(round(position.x()), round(position.y())))
        QTest.qWait(850)
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row,
                                    "rowDetailsTip.visible && rowDetailsTip.opened")
        assert expression.evaluate()[0] is True and not expression.hasError()
        project_list = self._item("launcherProjectList")
        expression = QQmlExpression(QQmlEngine.contextForObject(project_list), project_list,
                                    "projectList.contentY = projectList.contentY + 60")
        expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        QTest.qWait(50)
        expression = QQmlExpression(QQmlEngine.contextForObject(row), row, "!rowDetailsTip.visible")
        assert expression.evaluate()[0] is True and not expression.hasError()

    def _eval(self, item, code):
        expression = QQmlExpression(QQmlEngine.contextForObject(item), item, code)
        result = expression.evaluate()[0]
        assert not expression.hasError(), expression.error().toString()
        return result

    def _hover_row(self, index=0):
        QTest.mouseMove(self.window, QPoint(self.window.width() - 2, 2))
        project_list = self._item("launcherProjectList")
        self._eval(project_list, f"positionViewAtIndex({index}, 4)")
        QTest.qWait(30)
        row = self._item(f"launcherProjectEntry_{index}")
        position = row.mapToScene(QPointF(24, min(row.height() / 2, project_list.height() / 2)))
        QTest.mouseMove(self.window, position.toPoint())
        QTest.qWait(650)
        return row

    def test_hover_never_covers_any_project_or_action_during_resize(self):
        self.project_bridge.set_entries([{
            "descriptorFile": f"/tmp/Study {i}/project.coatproj", "name": f"Project {i}",
            "summary": "Long summary " * 8, "notes": "Long notes " * 100,
            "available": True, "informationAvailable": True,
        } for i in range(12)])
        manager = self._item("launcherProjectManager")
        for width, scale in ((1000, .75), (640, 1.5), (480, 1.5),
                             (640, 8 / 3), (1380, 1), (1000, .75)):
            self.theme._replace_snapshot(build_theme_snapshot("light", font_scale=scale),
                                         1, "light", "light", "qa", "QA", {})
            self.window.resize(width, 700)
            QTest.qWait(80)
            row = self._hover_row()
            tip = self._eval(row, "rowDetailsTip")
            if tip.property("visible"):
                origin = manager.mapToScene(QPointF())
                protected = QRectF(origin.x(), origin.y(), manager.width(), manager.height())
                rectangle = QRectF(float(tip.property("x")), float(tip.property("y")),
                                   float(tip.property("width")), float(tip.property("height")))
                assert not rectangle.intersects(protected), (width, scale, rectangle, protected)
                assert QRectF(0, 0, self.window.width(), self.window.height()).contains(rectangle)
            else:
                assert not tip.property("sidePlacement"), (width, scale)
            # A real click on a different row must open that row, even after
            # the first row's long metadata became visible.
            self._click("launcherOpenRecentProject_1")
            assert self.project_bridge.calls[-1] == ("open", "/tmp/Study 1/project.coatproj")
            assert not tip.property("visible")

    def test_narrow_recent_list_has_no_details_entry_and_keeps_three_actions(self):
        descriptor = "/tmp/Narrow study/project.coatproj"
        self.project_bridge.set_entries([{
            "descriptorFile": descriptor, "name": "Narrow study",
            "projectId": "b" * 32,
            "summary": "Summary remains visible", "notes": "Private project notes",
            "available": True, "informationAvailable": True,
        }])
        self.theme._replace_snapshot(build_theme_snapshot("light", font_scale=1.5),
                                     1, "light", "light", "qa", "QA", {})
        self.window.resize(640, 480)
        QTest.qWait(80)
        row = self._hover_row()
        assert self._eval(row, "!rowDetailsTip.sidePlacement && !rowDetailsTip.visible")
        assert self.window.findChild(QObject, "launcherShowRecentProjectDetails_0") is None
        assert self.window.findChild(QObject, "launcherFullProjectDetails") is None
        assert self.window.findChild(QObject, "launcherProjectInformationSection") is None
        assert self._item("launcherRecentProjectName_0").property("text") == "Narrow study"
        assert self._item("launcherRecentProjectSummary_0").property("text") == "Summary remains visible"
        for action in ("Copy", "Export", "Remove"):
            self._hover_row()
            button = self._item(f"launcher{action}RecentProject_0")
            assert button.isVisible() and button.isEnabled()
            self._click(f"launcher{action}RecentProject_0")
            dialog_name = "launcherRemoveProjectDialog" if action in ("Remove", "Delete") else "launcherProjectFileActionDialog"
            action_dialog = self.window.findChild(QObject, dialog_name)
            assert action_dialog is not None and action_dialog.property("visible")
            QTest.keyClick(self.window, Qt.Key.Key_Escape)
            QTest.qWait(150)

    def test_recent_actions_are_idle_transparent_and_animate_with_keyboard_focus(self):
        self.project_bridge.set_entries([{
            "descriptorFile": "/tmp/Study/project.coatproj", "projectId": "1" * 32,
            "name": "Coating study", "summary": "Saved-input comparison",
            "available": True, "informationAvailable": True,
            "lastOpened": "2026-10-16T03:00:00+00:00",
        }])
        self.window.resize(1000, 700)
        QTest.mouseMove(self.window, QPoint(980, 680))
        QTest.qWait(300)
        actions = [self._item("launcher" + action + "RecentProject_0")
                   for action in ("Copy", "Export", "Remove")]
        geometry = [(button.width(), button.height(), button.x(), button.y()) for button in actions]
        for button in actions:
            background = self._item(button.objectName() + "_background")
            expression = QQmlExpression(QQmlEngine.contextForObject(background), background,
                                        "color.a === 0 && border.width === 0")
            assert expression.evaluate()[0] is True and not expression.hasError()
            assert self._item(button.objectName() + "_interaction").opacity() == 0
            assert self._item(button.objectName() + "_keyboardFocus").opacity() == 0
            expression = QQmlExpression(QQmlEngine.contextForObject(button), button,
                                        "Accessible.name === text && focusPolicy === Qt.StrongFocus")
            assert expression.evaluate()[0] is True and not expression.hasError()
        refresh = self._item("launcherRefreshProjectsButton_background")
        expression = QQmlExpression(QQmlEngine.contextForObject(refresh), refresh,
                                    "color.a > 0 && border.width > 0")
        assert expression.evaluate()[0] is True and not expression.hasError()
        self.window.grabWindow().save("/tmp/deposition-recent-transparent-idle.png")

        copy = actions[0]
        position = copy.mapToScene(QPointF(copy.width() / 2, copy.height() / 2))
        point = QPoint(round(position.x()), round(position.y()))
        QTest.mouseMove(self.window, point)
        QTest.qWait(180)
        assert self._item(copy.objectName() + "_interaction").opacity() > 0
        self.window.grabWindow().save("/tmp/deposition-recent-transparent-hover.png")
        hover_opacity = self._item(copy.objectName() + "_interaction").opacity()
        QTest.mousePress(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        QTest.qWait(160)
        assert copy.property("down") is True
        assert self._item(copy.objectName() + "_interaction").opacity() > hover_opacity
        self.window.grabWindow().save("/tmp/deposition-recent-transparent-pressed.png")
        QTest.mouseRelease(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        QTest.qWait(80)
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        QTest.mouseMove(self.window, QPoint(980, 680))
        QTest.qWait(300)
        assert self._item(copy.objectName() + "_interaction").opacity() == 0
        assert geometry == [(button.width(), button.height(), button.x(), button.y()) for button in actions]

        self._item("launcherOpenRecentProject_0").forceActiveFocus(Qt.FocusReason.OtherFocusReason)
        QTest.keyClick(self.window, Qt.Key.Key_Tab)
        QTest.qWait(180)
        assert copy.property("visualFocus") is True
        assert self._item(copy.objectName() + "_keyboardFocus").opacity() == 1
        self.window.grabWindow().save("/tmp/deposition-recent-transparent-keyboard.png")
        QTest.keyClick(self.window, Qt.Key.Key_Tab)
        assert actions[1].hasActiveFocus()
        QTest.keyClick(self.window, Qt.Key.Key_Tab)
        assert actions[2].hasActiveFocus()
        QTest.keyClick(self.window, Qt.Key.Key_Space)
        QTest.qWait(100)
        self._click("launcherCancelRemoveProject")
        assert not self.project_bridge.calls
        self.project_bridge._busy = True
        self.project_bridge.projectsChanged.emit()
        QTest.qWait(180)
        for action in ("Copy", "Export", "Remove"):
            button = self._item("launcher" + action + "RecentProject_0")
            assert button.property("enabled") is False
            assert self._item(button.objectName() + "_interaction").opacity() == 0
