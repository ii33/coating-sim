"""Offscreen QML and owner-routing contracts for the in-project manager."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt, QUrl, QObject, Property, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression, QJSValue
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionHost, ProjectStoreView
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.ui.project_film_host import ProjectFilmDesktop, ProjectFilmNavigation
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot


def _events() -> None:
    QCoreApplication.processEvents()
    QTest.qWait(50)
    QCoreApplication.processEvents()


def _eval(engine: QQmlEngine, scope: QObject, source: str):
    expression = QQmlExpression(engine.rootContext(), scope, source)
    result = expression.evaluate()
    assert not expression.hasError(), expression.error().toString()
    value = result[0] if isinstance(result, tuple) else result
    return value.toVariant() if isinstance(value, QJSValue) else value


def _find_item(root: QQuickItem | QQuickWindow, object_name: str) -> QQuickItem:
    found = root.findChild(QQuickItem, object_name)
    if found is not None:
        return found
    pending = [root.contentItem() if isinstance(root, QQuickWindow) else root]
    while pending:
        parent = pending.pop()
        for child in parent.childItems():
            if child.objectName() == object_name:
                return child
            pending.append(child)
    raise AssertionError(f"QML item not found: {object_name}")


@pytest.mark.parametrize("window_root", [False, True], ids=["item", "window"])
def test_find_item_supports_qobject_and_visual_children_and_missing_targets(window_root: bool) -> None:
    app = QGuiApplication.instance() or QGuiApplication([])
    assert isinstance(app, QGuiApplication)
    root = QQuickWindow() if window_root else QQuickItem()
    visual_root = root.contentItem() if isinstance(root, QQuickWindow) else root
    branch, visual_child, qobject_child = QQuickItem(), QQuickItem(), QQuickItem()
    branch.setParentItem(visual_root)
    visual_child.setParentItem(branch)
    visual_child.setObjectName("visualOnlyTarget")
    qobject_child.setParent(root)
    qobject_child.setParentItem(visual_root)
    qobject_child.setObjectName("qobjectTarget")
    try:
        assert root.findChild(QQuickItem, "visualOnlyTarget") is None
        assert _find_item(root, "visualOnlyTarget") is visual_child
        assert _find_item(root, "qobjectTarget") is qobject_child
        with pytest.raises(AssertionError, match="QML item not found: missingTarget"):
            _find_item(root, "missingTarget")
    finally:
        for item in (visual_child, branch, qobject_child, root):
            item.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def _show_management_item(root: QQuickItem, item: QQuickItem) -> None:
    scroll = root.findChild(QObject, "projectRuntimeManagementScroll")
    assert scroll is not None
    flickable = scroll.property("contentItem")
    content = flickable.property("contentItem")
    point = item.mapToItem(content, QPointF(0, 0))
    flickable.setProperty("contentY", max(0, point.y() - 20))
    _events()


def _click(window: QQuickWindow, item: QQuickItem) -> None:
    assert item.isVisible() and item.width() > 0 and item.height() > 0, item.objectName()
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point.toPoint())
    _events()


class _RecoveryBridge(QObject):
    projectsChanged = Signal()
    recoveryConfirmationRequested = Signal(str, str, str, int, str)

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []
        self._error = ""
        self._busy = False
        self.information = {"projectId": "project-42", "sessionGeneration": "session-42",
                            "revision": 4, "name": "当前工程", "summary": "摘要", "notes": "备注"}
        self.information_calls = []
        self.save_information_succeeds = True
        self.rows = [{
            "recordId": "record-1", "generation": "generation-1", "revision": 4,
            "updatedAt": "2026-10-15T04:00:00+00:00", "reviewed": False,
            "stale": False, "sections": ["film"],
        }]

    @Property(str, notify=projectsChanged)
    def projectError(self):
        return self._error

    @Property(bool, notify=projectsChanged)
    def projectBusy(self):
        return self._busy

    @Slot(str, str, int, str, str, str, result=bool)
    def stageProjectInformationEdit(self, *_args):
        return True

    @Slot(str, str)
    def clearProjectInformationEdit(self, *_args):
        pass

    @Slot(str, result="QVariantMap")
    def projectInformation(self, project_id):
        return {**self.information, "projectId": project_id}

    @Slot(str, str, int, str, str, str, result=bool)
    def saveProjectInformation(self, project_id, generation, revision, name, summary, notes):
        self.information_calls.append((project_id, generation, revision, name, summary, notes))
        if not self.save_information_succeeds:
            self._error = "保存失败；输入仍保留。"
            self.projectsChanged.emit()
            return False
        self.information.update(name=name.strip(), summary=summary, notes=notes, revision=revision + 1)
        self._error = ""
        self.projectsChanged.emit()
        return True

    @Slot(str, result=list)
    def recoveryCandidates(self, project_id):
        self.calls.append(("candidates", project_id))
        return [dict(row) for row in self.rows]

    @Slot(str, str, str, int, str, result=bool)
    def reviewRecovery(self, project_id, generation, section, revision, record_id=""):
        self.calls.append(("review", project_id, generation, section, revision, record_id))
        self.recoveryConfirmationRequested.emit(project_id, generation, section, revision, record_id)
        return True

    @Slot(str, result=bool)
    def restoreRecovery(self, project_id):
        self.calls.append(("restore", project_id))
        return True

    @Slot(str)
    def cancelRecovery(self, project_id):
        self.calls.append(("cancel", project_id))

    @Slot(str, str, str, int, result=bool)
    def archiveReviewedRecovery(self, project_id, generation, section, revision):
        self.calls.append(("archive", project_id, generation, section, revision))
        return True

    @Slot(str, str, result=bool)
    def exportProjectPackage(self, project_id, destination):
        self.calls.append(("export", project_id, destination))
        return True

    @Slot(str, str, result=bool)
    def importProjectPackage(self, source, destination):
        self.calls.append(("import", source, destination))
        return True


@pytest.fixture
def management_qml():
    app = QGuiApplication.instance() or QGuiApplication([])
    assert isinstance(app, QGuiApplication)
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    bridge = _RecoveryBridge()
    path = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/ProjectRuntimeManagement.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
    assert not component.isError(), component.errorString()
    root = component.createWithInitialProperties({
        "projectBridge": bridge,
        "themeTokens": theme.snapshot,
    })
    assert isinstance(root, QQuickItem), component.errorString()
    window = QQuickWindow()
    window.resize(800, 620)
    root.setParent(window.contentItem())
    root.setParentItem(window.contentItem())
    window.show()
    window.requestActivate()
    _events()
    try:
        yield app, engine, window, root, bridge
    finally:
        window.close()
        window.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_navigation_management_signal_uses_bound_project_id():
    def unused_store(_root) -> ProjectStoreView:
        raise AssertionError("navigation must not load a store")
    host = ProjectSessionHost(unused_store)
    desktop = ProjectFilmDesktop(host)
    workspace = SimpleNamespace(binding=SimpleNamespace(project_id="project-bound-id"))
    navigation = ProjectFilmNavigation(desktop, cast(ProjectFilmWorkspace, workspace))
    received: list[str] = []
    desktop.managementRequested.connect(received.append)

    navigation.openProjectManagement()

    assert received == ["project-bound-id"]
    desktop.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_real_project_designer_toolbar_exposes_and_dispatches_management(tmp_path):
    app = QGuiApplication.instance() or QGuiApplication([])
    assert isinstance(app, QGuiApplication)
    store = ProjectStore.create(tmp_path / "Bound study", "Bound study")
    def bound_store(root) -> ProjectStoreView:
        assert root == store.root
        return store
    host = ProjectSessionHost(bound_store)
    desktop = ProjectFilmDesktop(host)
    events: list[str] = []
    desktop.managementRequested.connect(events.append)
    workspace = desktop.open_project(store.root)
    _events()
    controller = desktop.controller_for(workspace, "film_design")
    window = next(view[0] for route, view in desktop._views.items()
                  if route.binding == workspace.binding and route.module_key == "film_design")
    selector = window.findChild(QQuickItem, "projectModuleSelectorRoot")
    assert selector is not None
    assert selector.property("canManageProject") is True
    assert callable(getattr(controller, "openProjectManagement", None))

    engine = desktop._views[next(route for route in desktop._views
                                 if route.binding == workspace.binding
                                 and route.module_key == "film_design")][2]
    try:
        assert _eval(engine, selector, "openMenu()") is None
        _events()
        action = window.findChild(QQuickItem, "projectManagementAction")
        separator = window.findChild(QQuickItem, "projectCloseSeparator")
        close_action = window.findChild(QQuickItem, "projectCloseAction")
        assert action is not None and action.property("text") == "工程管理…"
        assert separator is not None and close_action is not None
        assert (action.mapToScene(QPointF(0, 0)).y()
                < separator.mapToScene(QPointF(0, 0)).y()
                < close_action.mapToScene(QPointF(0, 0)).y())
        _click(window, action)
        assert events == [workspace.binding.project_id]
    finally:
        window.setProperty("allowClose", True)
        window.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        _events()
        desktop.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_selector_adds_independent_management_action_and_preserves_close():
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    selector_path = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/ProjectModuleSelector.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(selector_path)))
    assert not component.isError(), component.errorString()

    class Navigation(QObject):
        @Property(str, constant=True)
        def projectName(self):
            return "工程甲"

        @Property(list, constant=True)
        def projectModules(self):
            return [{"key": "film_design", "name": "膜系设计"}]

        def __init__(self):
            super().__init__()
            self.management_calls = 0
            self.close_calls = 0

        @Slot(str, result=str)
        def openProjectModule(self, key):
            return key

        @Slot()
        def openProjectManagement(self):
            self.management_calls += 1

        @Slot(result=bool)
        def closeProject(self):
            self.close_calls += 1
            return True

    navigation = Navigation()
    root = component.createWithInitialProperties({
        "navigation": navigation,
        "currentModuleKey": "film_design",
    })
    assert isinstance(root, QQuickItem), component.errorString()
    window = QQuickWindow()
    window.resize(640, 420)
    root.setParent(window.contentItem())
    root.setParentItem(window.contentItem())
    window.show()
    _events()
    try:
        button = _find_item(root, "projectModuleSelector")
        _click(window, button)
        menu = root.findChild(QObject, "projectModuleMenu")
        assert menu is not None and menu.property("visible")
        manage = _find_item(root, "projectManagementAction")
        close = _find_item(root, "projectCloseAction")
        assert manage.property("text") == "工程管理…"
        assert close.property("text") == "关闭工程"
        _click(window, manage)
        assert navigation.management_calls == 1
        assert navigation.close_calls == 0
    finally:
        window.close()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_management_panel_confirms_or_cancels_recovery_and_routes_package_calls(management_qml):
    app, engine, window, root, bridge = management_qml
    assert _eval(engine, root, "openFor('project-42')") is True
    QTest.qWait(120)
    app.processEvents()
    management_dialog = root.findChild(QObject, "projectRuntimeManagementDialog")
    assert management_dialog is not None and management_dialog.property("visible")

    recovery_button = _find_item(root, "projectRuntimeOpenRecoveryButton")
    _show_management_item(root, recovery_button)
    _click(window, recovery_button)
    candidates_dialog = root.findChild(QObject, "launcherRecoveryCandidatesDialog")
    assert candidates_dialog is not None and candidates_dialog.property("visible"), bridge.calls
    candidates_scroll = root.findChild(QQuickItem, "launcherRecoveryCandidatesScroll")
    assert candidates_scroll is not None
    restore = _find_item(candidates_scroll.property("contentItem"),
                         "launcherRecoveryRestore_generation-1_film")
    _click(window, restore)
    confirm = root.findChild(QObject, "projectRuntimeRecoveryConfirmDialog")
    assert confirm is not None and confirm.property("visible")
    assert bridge.calls[-1] == ("review", "project-42", "generation-1", "film", 4, "record-1")
    QTest.qWait(100)
    app.processEvents()
    cancel_button = window.findChild(QQuickItem, "projectRuntimeRecoveryCancelButton")
    assert cancel_button is not None
    _click(window, cancel_button)
    assert ("cancel", "project-42") in bridge.calls
    assert not confirm.property("visible")

    _click(window, restore)
    QTest.qWait(100)
    app.processEvents()
    confirm_button = window.findChild(QQuickItem, "projectRuntimeRecoveryConfirmButton")
    assert confirm_button is not None
    _click(window, confirm_button)
    assert bridge.calls[-1] == ("restore", "project-42")
    assert root.property("statusMessage") == "所选类别的异常退出内容已恢复，尚未保存。"

    close_candidates = window.findChild(QQuickItem, "launcherRecoveryCandidatesClose")
    assert close_candidates is not None
    _click(window, close_candidates)
    assert not candidates_dialog.property("visible")
    # Package controls are below the recovery section; bring them into the
    # ScrollView viewport before sending a real pointer event.
    management_scroll = root.findChild(QObject, "projectRuntimeManagementScroll")
    assert management_scroll is not None
    flickable = management_scroll.property("contentItem")
    assert flickable is not None
    flickable.setProperty("contentY", flickable.property("contentHeight"))
    QTest.qWait(100)
    app.processEvents()
    source = _find_item(root, "projectRuntimeImportSourceField")
    destination = _find_item(root, "projectRuntimeImportDestinationField")
    source.setProperty("text", "/tmp/source.cproject")
    destination.setProperty("text", "/tmp/new-project")
    assert _eval(engine, root, "submitImport()") is True
    assert bridge.calls[-1] == ("import", "/tmp/source.cproject", "/tmp/new-project")

    assert _eval(engine, root, "submitExport('file:///tmp/saved.cproject')") is True
    assert bridge.calls[-1] == ("export", "project-42", "file:///tmp/saved.cproject")


def test_dismiss_for_review_cancels_pending_review_and_closes_all_surfaces(management_qml):
    app, engine, window, root, bridge = management_qml
    dismissed = []
    root.dismissed.connect(lambda: dismissed.append(True))
    assert _eval(engine, root, "openFor('project-42')") is True
    QTest.qWait(120)
    app.processEvents()
    management_dialog = root.findChild(QObject, "projectRuntimeManagementDialog")
    assert management_dialog is not None and management_dialog.property("visible")
    recovery_button = _find_item(root, "projectRuntimeOpenRecoveryButton")
    _show_management_item(root, recovery_button)
    _click(window, recovery_button)
    candidates_dialog = root.findChild(QObject, "launcherRecoveryCandidatesDialog")
    candidates_scroll = root.findChild(QQuickItem, "launcherRecoveryCandidatesScroll")
    assert candidates_dialog is not None and candidates_scroll is not None

    archive = _find_item(candidates_scroll.property("contentItem"),
                         "launcherRecoveryArchive_generation-1")
    _click(window, archive)
    archive_confirm = root.findChild(QObject, "launcherRecoveryArchiveConfirm")
    assert archive_confirm is not None and archive_confirm.property("visible")
    _eval(engine, root, "dismissForReview()")
    app.processEvents()
    assert not archive_confirm.property("visible")
    assert not candidates_dialog.property("visible")
    assert not management_dialog.property("visible")
    assert bridge.calls == [("candidates", "project-42")]
    assert dismissed == [True]

    assert _eval(engine, root, "openFor('project-42')") is True
    QTest.qWait(120)
    app.processEvents()
    recovery_button = _find_item(root, "projectRuntimeOpenRecoveryButton")
    _show_management_item(root, recovery_button)
    _click(window, recovery_button)
    candidates_dialog = root.findChild(QObject, "launcherRecoveryCandidatesDialog")
    candidates_scroll = root.findChild(QQuickItem, "launcherRecoveryCandidatesScroll")
    assert candidates_dialog is not None and candidates_scroll is not None
    restore = _find_item(candidates_scroll.property("contentItem"),
                         "launcherRecoveryRestore_generation-1_film")
    _click(window, restore)
    confirm = root.findChild(QObject, "projectRuntimeRecoveryConfirmDialog")
    assert confirm is not None and confirm.property("visible")

    _eval(engine, root, "dismissForReview()")
    app.processEvents()
    assert not management_dialog.property("visible")
    assert not confirm.property("visible")
    assert not candidates_dialog.property("visible")
    assert bridge.calls[-1] == ("cancel", "project-42")
    assert dismissed == [True, True]
    assert not any(call[0] in {"restore", "import", "export"} for call in bridge.calls)



def test_project_information_save_cancel_and_failed_save_keep_exact_fields(management_qml):
    app, engine, window, root, bridge = management_qml
    assert _eval(engine, root, "openFor('project-42')") is True
    name = _find_item(root, "projectRuntimeInformationNameField")
    summary = _find_item(root, "projectRuntimeInformationSummaryField")
    notes = _find_item(root, "projectRuntimeInformationNotesField")
    assert name.property("text") == "当前工程"
    assert summary.property("text") == "摘要"
    assert notes.property("text") == "备注"
    name.setProperty("text", "取消的名称")
    summary.setProperty("text", "未保存摘要")
    cancel = _find_item(root, "projectRuntimeInformationCancelButton")
    _show_management_item(root, cancel)
    _click(window, cancel)
    assert bridge.information_calls == []
    assert name.property("text") == "当前工程"
    assert not root.property("informationDirty")

    name.setProperty("text", "  新工程名称  ")
    summary.setProperty("text", "新工程摘要\n第二行")
    notes.setProperty("text", "新工程备注 <b>原样保留</b>")
    bridge.save_information_succeeds = False
    save = _find_item(root, "projectRuntimeInformationSaveButton")
    _show_management_item(root, save)
    _click(window, save)
    assert bridge.information_calls[-1] == (
        "project-42", "session-42", 4, "  新工程名称  ", "新工程摘要\n第二行", "新工程备注 <b>原样保留</b>")
    assert name.property("text") == "  新工程名称  "
    assert summary.property("text") == "新工程摘要\n第二行"
    assert notes.property("text") == "新工程备注 <b>原样保留</b>"
    assert root.property("informationDirty")
    assert "保存失败" in root.property("localError")

    bridge.save_information_succeeds = True
    _click(window, save)
    assert name.property("text") == "新工程名称"
    assert not root.property("informationDirty")
    label = _find_item(root, "projectRuntimeManagementProjectId")
    assert label.property("text") == "当前工程：新工程名称"
    assert root.property("statusMessage") == "工程信息已保存。"
    assert not save.property("enabled")


def test_project_information_close_decision_and_review_interruption(management_qml):
    app, engine, window, root, bridge = management_qml
    assert _eval(engine, root, "openFor('project-42')") is True
    name = _find_item(root, "projectRuntimeInformationNameField")
    name.setProperty("text", "尚未保存的工程名")
    _click(window, _find_item(root, "projectRuntimeManagementCloseButton"))
    discard = root.findChild(QObject, "projectRuntimeInformationDiscardDialog")
    assert discard is not None and discard.property("visible")
    _click(window, _find_item(root, "projectRuntimeInformationReturnButton"))
    assert not discard.property("visible")
    assert name.property("text") == "尚未保存的工程名"
    assert bridge.information_calls == []

    _eval(engine, root, "dismissForReview()")
    assert _eval(engine, root, "openFor('project-42')") is True
    assert name.property("text") == "尚未保存的工程名"
    _click(window, _find_item(root, "projectRuntimeManagementCloseButton"))
    _click(window, _find_item(root, "projectRuntimeInformationDiscardButton"))
    assert name.property("text") == "当前工程"
    assert not root.property("informationDirty")
    assert bridge.information_calls == []
    assert _eval(engine, root, "openFor('another-project')") is True
    assert _eval(engine, root, "information.projectId") == "another-project"


def test_switching_management_project_requires_discard_or_return_for_unsaved_information(management_qml):
    _app, engine, window, root, bridge = management_qml
    assert _eval(engine, root, "openFor('project-42')") is True
    name = _find_item(root, "projectRuntimeInformationNameField")
    name.setProperty("text", "Keep this project input")
    assert _eval(engine, root, "openFor('another-project')") is False
    assert root.property("projectId") == "project-42"
    assert name.property("text") == "Keep this project input"
    _click(window, _find_item(root, "projectRuntimeInformationReturnButton"))
    assert root.property("pendingManagementProjectId") == ""
    assert root.property("projectId") == "project-42"
    assert name.property("text") == "Keep this project input"
    assert _eval(engine, root, "openFor('another-project')") is False
    discard = _find_item(root, "projectRuntimeInformationDiscardButton")
    assert discard.property("text") == "放弃并切换"
    _click(window, discard)
    assert root.property("projectId") == "another-project"
    assert name.property("text") == "当前工程"
    assert not root.property("informationDirty")
    assert bridge.information_calls == []
