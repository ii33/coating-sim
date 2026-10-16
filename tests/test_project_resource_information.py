"""Information is draft metadata, with separate recoverable unconfirmed buffers."""
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject, QPointF, Qt, QUrl
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.api import CoatService
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionHost
from coat_sim.ui.source_substrate import SourceBridge
from tests.qt_test_guard import qml_item_window
from tests.test_input_snapshot import fixture
from tests.test_project_resource_adapter import project as project

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def click(item, app):
    assert item is not None and item.isEnabled() and item.isVisible()
    surface = qml_item_window(item)
    point = item.mapToItem(surface.contentItem(), QPointF(item.width() / 2, item.height() / 2)).toPoint()
    QTest.mouseClick(surface, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    app.processEvents()


@pytest.mark.parametrize("key", ["source", "substrate", "machine", "mask", "process", "materials"])
def test_information_three_fields_confirm_then_save_roundtrip(project, tmp_path, key):
    app, stores, _host, _film, resources, view = project
    external = tmp_path / "external"
    fixture(external)
    resources.service.load("process", external / "recipe.cproc")
    window, controller = view(key)
    if key != "materials":
        identity = resources.service.list_documents(key)[0]["id"]
        assert controller.openDocument(identity)
        if controller.currentDocumentId != identity:
            assert controller.bridge.confirmDocumentReplace(True)
        app.processEvents()
        assert controller.currentDocumentId == identity
    current = dict(controller.resourceInformation)
    assert set(current["editable_fields"]) == {"name", "summary", "notes"}
    before = deepcopy(stores["First"].manifest)
    assert controller.updateResourceInformation("中文 <HL> & 名称", "中文概述 <HL> & 保留\n第二行",
                                                "中文备注 <HL> & 原文\n" + "长备注\n" * 200, current["token"])
    assert stores["First"].manifest == before  # Confirm edits a draft, never disk.
    assert controller.dirty
    assert controller.saveCurrent(), controller.status
    saved_id = controller.currentDocumentId
    assert saved_id and not controller.dirty
    app.processEvents()
    picker = window.findChild(QQuickItem, "projectManagedDocumentPicker")
    assert picker is not None and picker.property("currentText") == "中文 <HL> & 名称"
    if key != "materials":
        assert "中文 <HL> & 名称" in str(window.property("caption"))
    service = ProjectResourceService(ProjectStore.open(stores["First"].project_file))
    data = service.load_document(saved_id).data
    if key == "materials":
        data = data["material"]
    assert data["name"] == "中文 <HL> & 名称"
    assert data["description"] == "中文概述 <HL> & 保留\n第二行"
    assert data["notes"] == "中文备注 <HL> & 原文\n" + "长备注\n" * 200


@pytest.mark.parametrize(("key", "limit"), [("source", 120), ("process", 100)])
def test_maximum_length_resource_names_save_independent_copy_without_changing_original(project, tmp_path, key, limit):
    app, stores, _host, _film, resources, view = project
    if key == "process":
        external = tmp_path / "external"
        fixture(external)
        resources.service.load("process", external / "recipe.cproc")
    _window, controller = view(key)
    if key == "process":
        identity = resources.service.list_documents("process")[0]["id"]
        assert controller.openDocument(identity)
        if controller.currentDocumentId != identity:
            assert controller.bridge.confirmDocumentReplace(True)
        app.processEvents()
        assert controller.currentDocumentId == identity
    information = controller.resourceInformation
    name = "名" * limit
    assert controller.updateResourceInformation(name, information["summary"], information["notes"],
                                                information["token"])
    assert controller.saveCurrent(), controller.status
    original_id = controller.currentDocumentId
    original = deepcopy(resources.service.load_document(original_id).data)
    assert original["name"] == name
    assert controller.saveCopyCurrent(), controller.status
    app.processEvents()
    copy_id = controller.currentDocumentId
    assert copy_id and copy_id != original_id
    copied = resources.service.load_document(copy_id).data
    assert copied["name"] == name[:limit - len(" 副本")] + " 副本"
    assert len(copied["name"]) == limit
    reopened = ProjectResourceService(ProjectStore.open(stores["First"].project_file))
    assert reopened.load_document(original_id).data == original
    assert reopened.load_document(copy_id).data == copied


@pytest.mark.parametrize(("key", "fallback_name", "relative_path"), [
    ("source", "未命名源", "parts/source.csrc"),
    ("machine", "未命名机器", "machines/machine.cmac"),
    ("mask", "未命名修正板", "parts/mask0.cmsk"),
])
def test_unnamed_resource_copy_gets_independent_name_without_replacing_overview_or_original(
        project, tmp_path, key, fallback_name, relative_path):
    app, stores, _host, _film, resources, view = project
    external = tmp_path / "external"
    fixture(external)
    source_path = external / relative_path
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    assert "name" not in raw
    raw.update(description="原概述 <HL> & 保留", notes="原备注\n保持完整")
    source_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    resources.service.load("process", external / "recipe.cproc")
    _window, controller = view(key)
    identities = [row["id"] for row in resources.service.list_documents(key)]
    original_id = next(identity for identity in identities
                       if resources.service.load_document(identity).data["description"] == raw["description"])
    assert controller.openDocument(original_id)
    if controller.currentDocumentId != original_id:
        assert controller.bridge.confirmDocumentReplace(True)
    app.processEvents()
    original = deepcopy(resources.service.load_document(original_id).data)
    assert "name" not in original
    assert controller.saveCopyCurrent(), controller.status
    copy_id = controller.currentDocumentId
    assert copy_id and copy_id != original_id
    reopened = ProjectResourceService(ProjectStore.open(stores["First"].project_file))
    assert reopened.load_document(original_id).data == original
    copied = reopened.load_document(copy_id).data
    assert copied == original | {"name": fallback_name + " 副本"}
    assert next(row for row in reopened.list_documents(key) if row["id"] == copy_id)["name"] == copied["name"]
    assert source_path.read_text(encoding="utf-8") == json.dumps(raw, ensure_ascii=False)


def test_unconfirmed_information_buffer_journals_reopens_and_cancel_stays_clean(project):
    app, stores, _host, _film, resources, view = project
    _window, source = view("source")
    assert source.saveCurrent()
    identity = source.currentDocumentId
    saved = deepcopy(source.document_data())
    saved_manifest = deepcopy(stores["First"].manifest)
    token = source.resourceInformation["token"]
    notes = "Long unconfirmed note\n" * 300
    assert source.stageInformationEdit("Recovered but unconfirmed", "Unconfirmed overview", notes, token)
    assert source.document_data() == saved and not source.bridge.dirty and source.dirty
    snapshot = resources.snapshot_pending_edits()
    pending = snapshot["drafts"]["source"]["pending_information"]
    assert pending["notes"] == notes and pending["model_dirty"] is False
    assert not resources.save_all()
    assert stores["First"].manifest == saved_manifest

    fresh_host = ProjectSessionHost(lambda _root: resources.store)
    binding = fresh_host.open_project(resources.store.root).binding
    recovered = ProjectResourceWorkspace.for_project(fresh_host, binding)
    assert recovered.restore_recovery(snapshot)
    assert not recovered.save_all()  # Headless recovery cannot confirm the buffer.
    recovered_window, restored = view("source", ProjectFilmWorkspace.for_project(fresh_host, binding))
    for _ in range(200):
        dialog = recovered_window.findChild(QObject, "projectResourceInformationDialog")
        if dialog is not None and dialog.property("visible"):
            break
        QTest.qWait(10)
    assert dialog is not None and dialog.property("visible") and dialog.property("editing")
    assert restored.currentDocumentId == identity
    assert restored.document_data() == saved and not restored.bridge.dirty
    assert recovered_window.findChild(QQuickItem, "projectResourceInformationNotes").property("text") == notes
    assert not restored.saveCurrent()
    click(recovered_window.findChild(QQuickItem, "projectResourceInformationCancelButton"), app)
    assert restored.pendingInformationEdit == {}
    assert not restored.dirty and not restored.bridge.dirty
    assert restored.document_data() == saved
    assert stores["First"].manifest == saved_manifest
    recovered.dispose()


def test_information_token_conflict_and_bad_recovery_cannot_replace_metadata(project):
    _app, _stores, _host, _film, resources, view = project
    _window, source = view("source")
    assert source.saveCurrent()
    token = source.resourceInformation["token"]
    source.bridge.setMetadata("description", "newer overview")
    assert not source.updateResourceInformation("old name", "old overview", "old notes", token)
    assert source.bridge.description == "newer overview"
    assert "old name" not in str(source.document_data())
    current = source.resourceInformation
    assert source.stageInformationEdit("pending", "pending", "pending", current["token"])
    snapshot = resources.snapshot()
    snapshot["drafts"]["source"]["pending_information"]["token"] = "f" * 64
    fresh_host = ProjectSessionHost(lambda _root: resources.store)
    binding = fresh_host.open_project(resources.store.root).binding
    recovered = ProjectResourceWorkspace.for_project(fresh_host, binding)
    with pytest.raises(ProjectError, match="different resource metadata"):
        recovered.validate_recovery(snapshot)
    assert recovered.controllers == {}
    recovered.dispose()


def test_independent_source_information_uses_ordinary_file_save(tmp_path):
    source = SourceBridge(CoatService(tmp_path))
    information = cast(Any, source.informationController)
    assert "name" not in source.documentData()  # No empty key injected into old payloads.
    try:
        current = information.resourceInformation
        assert information.updateResourceInformation("Global source", "Global overview", "Global notes", current["token"])
        path = Path(tmp_path) / "named.csrc"
        assert source.saveAsPath(QUrl.fromLocalFile(str(path)))
        data = json.loads(path.read_text())
        assert (data["name"], data["description"], data["notes"]) == (
            "Global source", "Global overview", "Global notes")
    finally:
        from PySide6.QtCore import QCoreApplication, QEvent
        source.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_independent_source_has_shared_information_only_bar(project, tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtQml import QQmlApplicationEngine
    from coat_sim.ui.source_substrate import launch_source_designer

    app, *_unused = project
    engine = QQmlApplicationEngine()
    handle = launch_source_designer(engine=engine, service=CoatService(tmp_path))
    app.processEvents()
    try:
        window = handle.window
        assert window.property("projectResources") is None
        info = window.findChild(QQuickItem, "projectResourceInformationButton")
        assert info is not None and info.isVisible() and info.isEnabled()
        for name in ("projectManagedDocumentPicker", "projectResourceNewButton",
                     "projectResourceSaveButton", "projectResourceTransferMenuButton"):
            action = window.findChild(QQuickItem, name)
            assert action is not None and not action.isVisible()
        assert window.findChild(QQuickItem, "sourceSaveButton").isVisible()
        click(info, app)
        dialog = window.findChild(QObject, "projectResourceInformationDialog")
        assert dialog is not None and dialog.property("visible")
        click(window.findChild(QQuickItem, "projectResourceInformationEditButton"), app)
        for name in ("projectResourceInformationName", "projectResourceInformationSummary",
                     "projectResourceInformationNotes"):
            field = window.findChild(QQuickItem, name)
            assert field is not None and field.isVisible() and field.isEnabled()
        click(window.findChild(QQuickItem, "projectResourceInformationCancelButton"), app)
        assert not handle.bridge.dirty and not list(tmp_path.rglob("*.csrc"))
    finally:
        handle.window.setProperty("projectCloseAuthorized", True)
        handle.window.close()
        handle.window.deleteLater()
        handle.bridge.deleteLater()
        handle.context.deleteLater()
        handle.component.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
