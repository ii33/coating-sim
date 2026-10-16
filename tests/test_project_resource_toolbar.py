"""Real QML checks for the shared project resource operation bar."""
from pathlib import Path
from copy import deepcopy
import json
import pytest
import time
from PySide6.QtCore import QCoreApplication, QMetaObject, QObject, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QSignalSpy, QTest

from coat_sim.core import data_files, material_business
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.ui.project_resource_adapter import _find_text_item
from tests.test_project_resource_adapter import project as project
from tests.qt_test_guard import qml_item_window

pytestmark = [pytest.mark.contract, pytest.mark.qt]


@pytest.fixture(autouse=True)
def isolated_transfer_library(tmp_path, monkeypatch):
    """Toolbar fixtures exercise real exchange I/O, not the application library.

    Import initially inspects global resources even when the next user action
    selects a project file. Bundled research materials are unrelated to this
    interaction and make its duration dependent on the runner's CPU/library.
    """
    root = tmp_path / "application-library"
    root.mkdir()
    monkeypatch.setattr(data_files, "PROJECT_ROOT", root)
    monkeypatch.setattr(data_files, "DATA_ROOT", root / "data")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [])
    yield root


def wait_for(app, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
    app.processEvents()
    return bool(predicate())


def complete_transfer(app, controller, kind, action):
    """Observe this admitted operation's terminal signal, including failure."""
    finished = QSignalSpy(controller.transferOperationFinished)
    assert action() is not False, controller.transferStatus
    operation_id = controller.transferOperationId
    assert wait_for(app, lambda: finished.count() > 0), (
        kind, controller.transferBusy, controller.transferStatus, controller.transferTimings)
    assert finished.count() == 1
    generation, actual_kind, success, error = finished.at(0)
    assert generation == operation_id and operation_id > 0 and actual_kind == kind
    assert success, error
    assert not controller.transferBusy


def click_item(item, app):
    surface = qml_item_window(item)
    assert surface is not None
    point = surface.contentItem().mapFromItem(
        item, QPointF(max(2, item.width() - 10), item.height() / 2)).toPoint()
    QTest.mouseClick(surface, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, point)
    app.processEvents()


def click_open_popup_button(dialog, item, app):
    """Prepare coordinates only after the popup's rendered layout is stable."""
    assert wait_for(app, lambda: bool(dialog.property("opened")))
    surface = qml_item_window(item)
    assert surface is not None
    rendered = QSignalSpy(surface.frameSwapped)
    previous_geometry = None
    observed_frames = 0

    def layout_ready():
        nonlocal previous_geometry, observed_frames
        if rendered.count() == observed_frames:
            return False
        observed_frames = rendered.count()
        position = item.mapToItem(surface.contentItem(), QPointF(0, 0))
        geometry = (position.x(), position.y(), item.width(), item.height(),
                    surface.width(), surface.height())
        ready = (geometry == previous_geometry and bool(dialog.property("opened"))
                 and item.isVisible() and item.isEnabled()
                 and item.width() > 0 and item.height() > 0)
        previous_geometry = geometry
        if not ready:
            surface.update()
        return ready

    surface.update()
    assert wait_for(app, layout_ready), (item.objectName(), previous_geometry, observed_frames)
    click_item(item, app)  # Exactly one real mouse click; never retry a miss.


def evaluate_qml(scope, expression):
    context = QQmlEngine.contextForObject(scope)
    query = QQmlExpression(context, scope, expression)
    value, undefined = query.evaluate()
    assert not query.hasError(), query.error().toString()
    assert not undefined, expression
    return value


def run_qml(scope, expression):
    context = QQmlEngine.contextForObject(scope)
    query = QQmlExpression(context, scope, expression)
    value, _undefined = query.evaluate()
    assert not query.hasError(), query.error().toString()
    return value


def capture_review(window, name):
    review = Path("artifacts/project-resource-toolbar-review")
    review.mkdir(parents=True, exist_ok=True)
    capture = window.grabWindow()
    assert not capture.isNull()
    path = review / name
    assert capture.save(str(path))
    assert path.stat().st_size > 0


@pytest.mark.parametrize("key", ["source", "substrate", "machine", "mask", "process", "materials"])
def test_shared_resource_operation_bar_has_explicit_actions(project, key):
    app, _stores, _host, _film, _resources, view = project
    window, controller = view(key)
    bar = window.findChild(QQuickItem, "projectResourceControls")
    assert bar is not None and bar.isVisible()
    assert window.findChild(QQuickItem, "projectManagedDocumentPicker") is not None
    assert window.findChild(QQuickItem, "projectResourceNewButton") is not None
    save_name = "sourceSaveButton" if key == "source" else "projectResourceSaveButton"
    assert window.findChild(QQuickItem, save_name) is not None
    transfer = window.findChild(QQuickItem, "projectResourceTransferMenuButton")
    assert transfer is not None
    assert transfer.property("text") == "导入/导出"
    assert window.findChild(QQuickItem, "projectResourceMoreActionsButton") is not None
    if not transfer.isVisible():
        click_item(window.findChild(QQuickItem, "projectResourceMoreActionsButton"), app)
        compact_transfer = window.findChild(QQuickItem, "projectResourceOverflowTransferAction")
        assert compact_transfer is not None and compact_transfer.isVisible()
        assert compact_transfer.isEnabled() == transfer.isEnabled()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        app.processEvents()
    info_button = window.findChild(QQuickItem, "projectResourceInformationButton")
    assert info_button is not None and info_button.isVisible()
    assert controller.moduleKey == key
    narrow_widths = {
        "source": 640, "substrate": 1000, "machine": 920,
        "mask": 960, "process": 720, "materials": 760,
    }
    window.resize(narrow_widths[key], max(600, int(window.height())))
    app.processEvents()
    surface = qml_item_window(bar)
    top_left = surface.contentItem().mapFromItem(bar, QPointF(0, 0))
    assert top_left.x() >= -1
    assert top_left.x() + bar.width() <= surface.width() + 1
    review = Path("artifacts/project-resource-toolbar-review")
    review.mkdir(parents=True, exist_ok=True)
    capture = window.grabWindow()
    assert not capture.isNull()
    image_path = review / f"shared-bar-{key}-{narrow_widths[key]}.png"
    assert capture.save(str(image_path))
    assert image_path.stat().st_size > 0


def test_module_picker_tracks_current_resource_and_restores_it_after_dirty_cancel(project):
    app, _stores, _host, _film, _resources, view = project
    window, controller = view("source")
    assert controller.saveCurrent(), controller.status
    original_id = str(controller.currentDocumentId)
    assert controller.saveCopyCurrent(), controller.status
    current_id = str(controller.currentDocumentId)
    assert current_id != original_id
    picker = window.findChild(QQuickItem, "projectManagedDocumentPicker")
    assert picker is not None
    assert str(picker.property("currentValue")) == current_id
    rows = list(controller.documents)
    assert {str(row["id"]) for row in rows} == {original_id, current_id}
    assert all(row["kind"] == "source" for row in rows)

    controller.bridge.setField("source_exponent", "4")
    original_index = next(index for index, row in enumerate(rows)
                          if str(row["id"]) == original_id)
    click_item(picker, app)
    popup = picker.findChild(QObject, "themeComboBoxPopup")
    assert popup is not None and popup.property("visible")
    option = evaluate_qml(picker, f"popup.contentItem.itemAtIndex({original_index})")
    assert option is not None
    click_item(option, app)
    assert wait_for(app, lambda: controller.bridge._pending_document_replace is not None)
    assert int(picker.property("currentIndex")) == next(
        index for index, row in enumerate(controller.documents)
        if str(row["id"]) == current_id)

    # Cancel through the actual dirty-replacement dialog's Escape path. This
    # exercises both the popup activation and the user's cancellation event.
    QTest.keyClick(window, Qt.Key.Key_Escape)
    app.processEvents()
    assert wait_for(app, lambda: controller.bridge._pending_document_replace is None)
    assert str(picker.property("currentValue")) == current_id
    assert str(controller.currentDocumentId) == current_id
    assert int(picker.property("currentIndex")) == next(
        index for index, row in enumerate(controller.documents)
        if str(row["id"]) == current_id)
    assert controller.bridge._fields["source_exponent"] == "4"


def test_transfer_dialog_is_shared_and_cancel_does_not_change_editor_or_project(project):
    app, stores, _host, _film, _resources, view = project
    window, controller = view("source")
    assert controller.saveCurrent(), controller.status
    identity = str(controller.currentDocumentId)
    draft_before = controller.document_data()
    manifest_before = stores["First"].manifest
    bar = window.findChild(QQuickItem, "projectResourceControls")
    assert bar is not None
    menu_button = window.findChild(QQuickItem, "projectResourceTransferMenuButton")
    click_item(menu_button, app)
    export_item = window.findChild(QQuickItem, "projectResourceExportMenuItem")
    assert export_item is not None and export_item.isVisible()
    complete_transfer(app, controller, "inspect", lambda: click_item(export_item, app))

    dialog = window.findChild(QObject, "projectResourceTransferDialog")
    assert dialog is not None and dialog.property("visible")
    for name in ("projectResourceExportGlobalTarget", "projectResourceExportFileTarget",
                 "projectResourceTransferCategoryPicker", "projectResourceTransferPreviewButton",
                 "projectResourceTransferApplyButton"):
        assert window.findChild(QQuickItem, name) is not None
    rows = controller.transferCatalog["rows"]
    assert any(str(row["id"]) == identity and row["kind"] == "source" for row in rows)

    cancel_button = window.findChild(QQuickItem, "projectResourceTransferCancelButton")
    assert cancel_button is not None and cancel_button.isEnabled()
    click_item(cancel_button, app)
    assert wait_for(app, lambda: not controller.transferActive)
    assert not controller.transferActive
    assert controller.currentDocumentId == identity
    assert controller.document_data() == draft_before
    assert stores["First"].manifest == manifest_before


def test_transfer_menu_instantiates_window_specific_actions_for_real_mouse_click(project):
    app, _stores, _host, _film, _resources, view = project
    window, _controller = view("source")
    bar = window.findChild(QQuickItem, "projectResourceControls")
    transfer = window.findChild(QQuickItem, "projectResourceTransferMenuButton")
    assert bar is not None and transfer is not None
    bar.setProperty("extraTransferActions", [{
        "key": "simulation_report", "text": "导出仿真报告…", "enabled": True,
    }])
    received = []
    bar.extraTransferActionRequested.connect(lambda key: received.append(str(key)))
    app.processEvents()

    click_item(transfer, app)
    extra = window.findChild(QQuickItem, "projectResourceExtraAction_simulation_report")
    assert extra is not None and extra.isVisible()
    click_item(extra, app)
    assert received == ["simulation_report"]
    dialog = window.findChild(QObject, "projectResourceTransferDialog")
    assert dialog is not None and not dialog.property("visible")


def test_transfer_menu_keeps_full_resource_and_dependency_export(project, tmp_path):
    app, stores, _host, _film, _resources, view = project
    window, controller = view("source")
    assert controller.saveCurrent(), controller.status
    identity = controller.currentDocumentId
    draft_before = deepcopy(controller.document_data())
    manifest_before = deepcopy(stores["First"].manifest)
    cwd_exports_before = set(Path.cwd().glob("resource-export-*"))
    menu_button = window.findChild(QQuickItem, "projectResourceTransferMenuButton")
    click_item(menu_button, app)
    bundle_item = window.findChild(QQuickItem, "projectResourceExportBundleMenuItem")
    assert bundle_item is not None and bundle_item.isVisible() and bundle_item.isEnabled()
    folder_dialog = window.findChild(QObject, "projectResourceBundleFolderDialog")
    assert folder_dialog is not None
    # Programmatic selection is a Qt Quick dialog contract, not a portable
    # native OS picker interaction. Switch only this test presentation to the
    # deterministic non-native implementation before selecting/accepting.
    run_qml(folder_dialog, "options = FolderDialog.DontUseNativeDialog")
    click_item(bundle_item, app)
    assert folder_dialog.property("visible")
    target = QUrl.fromLocalFile(str(tmp_path))
    assert folder_dialog.setProperty("currentFolder", target)
    assert folder_dialog.setProperty("selectedFolder", target)
    assert Path(folder_dialog.property("selectedFolder").toLocalFile()).resolve() == tmp_path.resolve()
    accepted_folders = []
    folder_dialog.accepted.connect(lambda: accepted_folders.append(folder_dialog.property("selectedFolder")))
    assert QMetaObject.invokeMethod(folder_dialog, "accept", Qt.ConnectionType.DirectConnection)
    app.processEvents()
    assert accepted_folders == [target]
    assert "已导出项目资源及完整依赖" in str(controller.status)
    exported = list(tmp_path.glob("resource-export-*"))
    assert len(exported) == 1 and exported[0].is_dir()
    assert any(exported[0].iterdir())
    assert set(Path.cwd().glob("resource-export-*")) == cwd_exports_before
    assert controller.currentDocumentId == identity
    assert controller.document_data() == draft_before
    assert stores["First"].manifest == manifest_before


def test_information_dialog_edit_is_local_until_confirmed_and_cancel_discards_buffer(project):
    app, _stores, _host, _film, _resources, view = project
    window, controller = view("source")
    assert controller.saveCurrent(), controller.status
    before = dict(controller.resourceInformation)
    info_button = window.findChild(QQuickItem, "projectResourceInformationButton")
    assert info_button is not None and info_button.isVisible()
    click_item(info_button, app)

    dialog = window.findChild(QObject, "projectResourceInformationDialog")
    assert dialog is not None and dialog.property("visible")
    capture_review(window, "information-viewer.png")
    information_scroll = window.findChild(QQuickItem, "projectResourceInformationScrollView")
    assert information_scroll is not None
    flickable = next((item for item in information_scroll.findChildren(QQuickItem)
                      if item.metaObject().className() == "QQuickFlickable"), None)
    assert flickable is not None
    assert flickable.property("contentHeight") <= flickable.height() + 1
    for object_name in (
        "projectResourceInformationNameHeading",
        "projectResourceInformationNameViewer",
        "projectResourceInformationSummaryHeading",
        "projectResourceInformationSummaryViewer",
        "projectResourceInformationNotesHeading",
        "projectResourceInformationNotesViewer",
    ):
        viewer = window.findChild(QQuickItem, object_name)
        assert viewer is not None and viewer.isVisible()
        viewport_position = viewer.mapToItem(information_scroll, QPointF(0, 0))
        assert viewport_position.y() >= -1, object_name
        assert viewport_position.y() + viewer.height() <= information_scroll.height() + 1, object_name
    edit_button = window.findChild(QQuickItem, "projectResourceInformationEditButton")
    assert edit_button is not None and edit_button.isVisible()
    click_item(edit_button, app)
    capture_review(window, "information-editor.png")
    summary = next((item for item in window.findChildren(QQuickItem)
                    if item.property("accessibleLabel") == "资源概述"), None)
    assert summary is not None and summary.isVisible()

    def replace_text(item, value):
        assert item.isEnabled(), item.objectName()
        click_item(item, app)
        input_window = qml_item_window(item)
        assert input_window is not None
        item.forceActiveFocus()
        app.processEvents()
        QTest.qWait(20)
        assert item.hasActiveFocus(), item.objectName()
        QTest.keyClick(input_window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        for character in value:
            key = Qt.Key(ord(character.upper()))
            modifier = (Qt.KeyboardModifier.ShiftModifier
                       if character.isupper() else Qt.KeyboardModifier.NoModifier)
            QTest.keyClick(input_window, key, modifier)
        app.processEvents()
        assert item.property("text") == value, (item.property("accessibleLabel"), item.property("text"))

    replace_text(summary, "cancelled overview")
    cancel = window.findChild(QQuickItem, "projectResourceInformationCancelButton")
    click_item(cancel, app)
    assert not controller.dirty
    assert controller.resourceInformation["summary"] == before["summary"]

    click_item(info_button, app)
    click_item(window.findChild(QQuickItem, "projectResourceInformationEditButton"), app)
    summary = next(item for item in window.findChildren(QQuickItem)
                   if item.property("accessibleLabel") == "资源概述")
    if "name" in controller.resourceInformation["editable_fields"]:
        name_field = next(item for item in window.findChildren(QQuickItem)
                          if item.property("accessibleLabel") == "资源名称")
        replace_text(name_field, "named source")
    replace_text(summary, "confirmed overview")
    save_info = window.findChild(QQuickItem, "projectResourceInformationSaveButton")
    assert save_info is not None and save_info.isEnabled()
    click_item(save_info, app)
    assert not dialog.property("visible"), (controller.resourceInformation,
                                             controller.status, dialog.property("editing"))
    assert controller.resourceInformation["summary"] == "confirmed overview"
    assert controller.dirty


@pytest.mark.parametrize("pending_resize", [False, True])
def test_unconfirmed_information_edit_recovers_only_for_matching_resource(project, pending_resize, monkeypatch):
    app, _stores, _host, _film, _resources, view = project
    window, controller = view("source")
    assert controller.saveCurrent(), controller.status
    original = dict(controller.resourceInformation)
    before = controller.document_data()
    info_button = window.findChild(QQuickItem, "projectResourceInformationButton")
    click_item(info_button, app)
    dialog = window.findChild(QObject, "projectResourceInformationDialog")
    click_item(window.findChild(QQuickItem, "projectResourceInformationEditButton"), app)

    pending_summary = "Recovered after reopening information"
    assert controller.stageInformationEdit(
        original["name"], pending_summary, original["notes"], original["token"])
    assert controller.pendingInformationEdit["summary"] == pending_summary
    assert controller.document_data() == before

    # Hiding the dialog without choosing Cancel models a window/dialog teardown;
    # it must retain the tentative buffer for a matching resource reopen.
    assert QMetaObject.invokeMethod(dialog, "close", Qt.ConnectionType.DirectConnection)
    click_item(info_button, app)
    assert dialog.property("visible")
    assert dialog.property("editing")
    summary = window.findChild(QQuickItem, "projectResourceInformationSummary")
    assert summary.property("text") == pending_summary
    assert controller.document_data() == before

    # opened ends the enter transition, but the recovered editing form can
    # still have a pending RowLayout polish. Coordinates need rendered layout.
    assert wait_for(app, lambda: bool(dialog.property("opened")))
    resized = []
    if pending_resize:
        # Native resize/layout work can be queued after Popup.opened. Keep it
        # pending until pointer preparation, rather than sleeping through it.
        def resize_window():
            window.resize(window.width() + 160, window.height())
            resized.append(True)
        QTimer.singleShot(0, resize_window)
        original_mouse_click = QTest.mouseClick

        def dispatch_after_pending_frame(surface, *args, **kwargs):
            # Reproduce an event-loop/render turn between target-coordinate
            # calculation and native dispatch. Still send exactly one click.
            rendered = QSignalSpy(surface.frameSwapped)
            surface.update()
            assert wait_for(app, lambda: rendered.count() > 0)
            original_mouse_click(surface, *args, **kwargs)

        monkeypatch.setattr(QTest, "mouseClick", dispatch_after_pending_frame)
    cancel = window.findChild(QQuickItem, "projectResourceInformationCancelButton")
    clicked = QSignalSpy(cancel.clicked)
    click_open_popup_button(dialog, cancel, app)
    assert not pending_resize or resized == [True]
    assert clicked.count() == 1
    assert wait_for(app, lambda: not dialog.property("visible"))
    assert not controller.dirty
    assert not controller.pendingInformationEdit
    assert controller.document_data() == before


def test_information_roundtrip_keeps_markup_and_multiline_text_plain(project):
    app, _stores, _host, _film, _resources, view = project
    window, controller = view("source")
    assert controller.saveCurrent(), controller.status
    before = dict(controller.resourceInformation)
    name = "镀层 <HL> & source"
    summary_text = "中文概述 <HL> & alloy\n第二行说明"
    notes_text = "第一行 <HL> &\n第二行 " + ("多行备注内容 " * 600)
    draft_before = controller.document_data()
    info_button = window.findChild(QQuickItem, "projectResourceInformationButton")
    click_item(info_button, app)
    dialog = window.findChild(QObject, "projectResourceInformationDialog")
    assert dialog is not None and wait_for(app, lambda: dialog.property("visible"))
    click_item(window.findChild(QQuickItem, "projectResourceInformationEditButton"), app)
    assert dialog.property("editing")

    def ime_replace(object_name, expected):
        field = window.findChild(QQuickItem, object_name)
        assert field is not None and field.isEnabled()
        click_item(field, app)
        field.forceActiveFocus()
        app.processEvents()
        QTest.qWait(20)
        assert field.hasActiveFocus(), object_name
        input_window = qml_item_window(field)
        assert input_window is not None and input_window.activeFocusItem() is not None
        event = QInputMethodEvent()
        event.setCommitString(expected)
        assert QCoreApplication.sendEvent(input_window.activeFocusItem(), event)
        app.processEvents()
        assert field.property("text") == expected
    ime_replace("projectResourceInformationName", name)
    ime_replace("projectResourceInformationSummary", summary_text)
    ime_replace("projectResourceInformationNotes", notes_text)
    assert controller.pendingInformationEdit["name"] == name
    assert controller.pendingInformationEdit["summary"] == summary_text
    assert controller.pendingInformationEdit["notes"] == notes_text
    assert controller.document_data() == draft_before
    assert controller.resourceInformation["summary"] == before["summary"]
    capture_review(window, "information-long-note-editor.png")

    click_item(window.findChild(QQuickItem, "projectResourceInformationSaveButton"), app)
    assert wait_for(app, lambda: not dialog.property("visible"))
    assert controller.resourceInformation["name"] == name
    assert controller.resourceInformation["summary"] == summary_text
    assert controller.resourceInformation["notes"] == notes_text

    click_item(window.findChild(QQuickItem, "projectResourceInformationButton"), app)
    for object_name, expected in (
        ("projectResourceInformationNameViewer", name),
        ("projectResourceInformationSummaryViewer", summary_text),
        ("projectResourceInformationNotesViewer", notes_text),
    ):
        viewer = window.findChild(QQuickItem, object_name)
        assert viewer is not None and viewer.isVisible()
        assert viewer.property("text") == expected
        assert evaluate_qml(viewer, "textFormat") == 0  # Text.PlainText
    assert float(dialog.property("height")) <= window.height() - 32
    assert controller.dirty
    saved_id = str(controller.currentDocumentId)
    assert controller.saveCurrent(), controller.status
    assert not controller.dirty
    assert controller.openDocument(saved_id), controller.status
    assert controller.resourceInformation["name"] == name
    assert controller.resourceInformation["summary"] == summary_text
    assert controller.resourceInformation["notes"] == notes_text
    assert controller.dirty is False
    information_scroll = window.findChild(QQuickItem, "projectResourceInformationScrollView")
    scrollbar = window.findChild(QObject, "projectResourceInformationVerticalScrollBar")
    notes_viewer = window.findChild(QQuickItem, "projectResourceInformationNotesViewer")
    assert information_scroll is not None and scrollbar is not None and notes_viewer is not None
    assert scrollbar.property("visible")
    flickable = next((item for item in information_scroll.findChildren(QQuickItem)
                      if item.metaObject().className() == "QQuickFlickable"), None)
    assert flickable is not None
    assert wait_for(app, lambda: flickable.property("contentHeight") > flickable.height())
    flickable.setProperty("contentY", flickable.property("contentHeight") - flickable.height())
    app.processEvents()
    assert wait_for(app, lambda: (
        notes_viewer.mapToItem(information_scroll, QPointF(0, 0)).y() < 0
        and notes_viewer.mapToItem(information_scroll, QPointF(0, 0)).y()
            + notes_viewer.height() <= information_scroll.height() + 2
    ))
    capture_review(window, "information-long-note-viewer.png")


def test_transfer_preview_change_clears_stale_plan_and_conflicts_need_a_choice(project):
    app, _stores, _host, _film, _resources, view = project
    window, controller = view("source")
    assert controller.saveCurrent(), controller.status
    identity = str(controller.currentDocumentId)
    complete_transfer(app, controller, "inspect", lambda: controller.beginTransfer("export"))
    catalog_id = controller.transferCatalog["catalog_id"]
    complete_transfer(app, controller, "preview", lambda: controller.previewTransfer([identity], "global"))
    assert controller.transferPlan.get("plan_id")
    plan = controller.transferPlan
    assert plan["selected_ids"] == [identity]

    bar = window.findChild(QQuickItem, "projectResourceControls")
    assert bar is not None
    # Selecting a different target invalidates both the local plan view and the
    # backend's one-shot plan, so the old result cannot be applied accidentally.
    assert QMetaObject.invokeMethod(bar, "invalidatePreview", Qt.ConnectionType.DirectConnection)
    app.processEvents()
    assert controller.transferPlan == {}
    assert controller.transferCatalog["catalog_id"] == catalog_id
    assert controller.transferStatus == "请选择资源，然后重新预览。"
    controller.cancelTransfer()


def test_import_package_category_preview_is_read_only_and_capturable(project, tmp_path):
    app, stores, _host, _film, _resources, view = project
    window, controller = view("source")
    assert controller.saveCurrent(), controller.status
    identity = str(controller.currentDocumentId)

    donor = ProjectStore.create(tmp_path / "donor", "Donor")
    donor_identity = donor.create_document("source", controller.service.load_document(identity).data)
    donor_service = ProjectResourceService(donor)
    donor_information = donor_service.resource_information(donor_identity)
    donor_name = "Project <HL> & source"
    donor_service.save_resource_information(
        donor_identity, {"name": donor_name}, expected_revision=donor_information["token"])
    donor_path = donor.project_file.with_name("arbitrary-name.coatproj")
    donor.project_file.rename(donor_path)
    donor_bytes = donor_path.read_bytes()
    destination_before = stores["First"].manifest

    bar = window.findChild(QQuickItem, "projectResourceControls")
    click_item(window.findChild(QQuickItem, "projectResourceTransferMenuButton"), app)
    complete_transfer(app, controller, "inspect", lambda: click_item(
        window.findChild(QQuickItem, "projectResourceImportMenuItem"), app))
    assert controller.transferCatalog["source_type"] == "global"
    assert controller.transferCatalog["rows"] == []  # The real, private test library is empty.
    transfer_dialog = window.findChild(QObject, "projectResourceTransferDialog")
    assert transfer_dialog is not None and transfer_dialog.property("visible")
    donor_url = QUrl.fromLocalFile(str(donor_path)).toString()
    complete_transfer(app, controller, "inspect", lambda: run_qml(
        bar, "inspectSource(" + json.dumps(donor_url) + ")"))
    assert controller.transferCatalog.get("rows")
    assert controller.transferCatalog["rows"][0]["id"] == donor_identity

    category = window.findChild(QQuickItem, "projectResourceTransferCategoryPicker")
    click_item(category, app)
    popup = category.findChild(QObject, "themeComboBoxPopup")
    assert popup is not None
    source_index = 1 + controller.transferCatalog["categories"].index("source")
    assert wait_for(app, lambda: bool(popup.property("visible")))
    assert wait_for(app, lambda: evaluate_qml(
        category, f"popup.contentItem.itemAtIndex({source_index})") is not None)
    source_option = evaluate_qml(category, f"popup.contentItem.itemAtIndex({source_index})")
    assert source_option is not None
    click_item(source_option, app)

    assert wait_for(app, lambda: str(bar.property("categoryFilter")) == "source")
    select_name = "projectResourceTransferSelect_" + donor_identity
    assert wait_for(app, lambda: _find_text_item(window, select_name) is not None)
    select = _find_text_item(window, select_name)
    assert select is not None and select.isVisible()
    assert str(select.property("text")) == donor_name
    assert evaluate_qml(select, "contentItem.textFormat") == 0
    click_item(select, app)
    preview = window.findChild(QQuickItem, "projectResourceTransferPreviewButton")
    assert preview is not None and preview.isEnabled()
    complete_transfer(app, controller, "preview", lambda: click_item(preview, app))
    assert controller.transferPlan.get("plan_id")
    assert controller.transferPlan["selected_ids"] == [donor_identity]
    preview_label = _find_text_item(window, "projectResourceTransferPreview_" + donor_identity)
    assert preview_label is not None
    assert str(preview_label.property("text")) == "源 · " + donor_name
    assert evaluate_qml(preview_label, "textFormat") == 0

    review = Path("artifacts/project-resource-toolbar-review")
    review.mkdir(parents=True, exist_ok=True)
    assert transfer_dialog.property("visible")
    capture = window.grabWindow()
    assert not capture.isNull()
    screenshot = review / "import-coatproj-category-preview.png"
    assert capture.save(str(screenshot)) and screenshot.stat().st_size > 0

    click_item(window.findChild(QQuickItem, "projectResourceTransferCancelButton"), app)
    assert wait_for(app, lambda: not controller.transferActive)
    assert stores["First"].manifest == destination_before
    assert donor_path.read_bytes() == donor_bytes
