"""Real film/deposition resource bars and staged information recovery."""
import pytest
from PySide6.QtCore import Q_ARG, QCoreApplication, QEvent, QMetaObject, QObject, Qt, QUrl
from PySide6.QtQuick import QQuickItem

from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionHost
from coat_sim.ui.project_film_host import ProjectFilmDesktop
from tests.test_project_core import setup_project
from tests.test_project_film_controller_adapter import MemorySettings
from tests.test_project_film_desktop import desktop as desktop
from tests.test_project_film_desktop import events
from tests.test_project_film_service import bound_design, material

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def view_for(client, workspace, key):
    return next(view for route, view in client._views.items()
                if route.binding == workspace.binding and route.module_key == key)


def activate_document(window, resource, identity):
    picker = window.findChild(QQuickItem, "projectManagedDocumentPicker")
    index = next(index for index, row in enumerate(resource.documents) if row["id"] == identity)
    picker.setProperty("currentIndex", index)
    assert QMetaObject.invokeMethod(picker, "activated", Qt.ConnectionType.DirectConnection, Q_ARG(int, index))
    events()
    return picker


def test_three_film_bars_share_editor_dirty_cancel_and_complete_workflow(desktop):
    client, stores = desktop
    workspace = client.open_project(next(iter(stores)))
    saved = workspace.service.create_film(bound_design(), materials={"shared": material()})
    other_design = bound_design()
    other_design["name"] = "Other film"
    other = workspace.service.create_film(other_design)
    assert workspace.editor.request_load_saved_design(saved.document_id)
    for key in ("film_simulation", "film_optimization"):
        client.open_project_module(workspace, key)
    events()
    views = [view_for(client, workspace, key) for key in ("film_design", "film_simulation", "film_optimization")]
    bars = [view[1]._project_input_resources for view in views]
    assert all(bar.editor is workspace.editor and bar.currentDocumentId == saved.document_id for bar in bars)
    assert len(client._views) == 3
    source = views[0][1]
    assert source.setDesignValue("name", "Keep working film")
    picker = activate_document(views[1][0], bars[1], other.document_id)
    decision = views[1][0].findChild(QObject, "projectResourceReplaceDialog")
    assert decision.property("visible")
    for window in (views[0][0], views[2][0]):
        assert not window.findChild(QObject, "projectResourceReplaceDialog").property("visible")
    assert str(picker.property("currentValue")) == saved.document_id
    assert bars[1].confirmReplace("cancel")
    decision.close()
    assert source.currentDesign()["name"] == "Keep working film" and source.dirty
    views[1][1].setParameter("samples", "73")
    assert views[2][1].setSolverOption("max_iterations", 19)
    assert bars[2].saveCurrent(), bars[2].status
    film = workspace.service.load_document(saved.document_id).design
    assert film["name"] == "Keep working film"
    assert film["workflow_settings"]["simulation"]["parameters"]["samples"] == 73
    assert film["workflow_settings"]["optimization"]["solver"]["max_iterations"] == 19
    target = workspace.service.root / "data" / "film-export"
    target.mkdir(parents=True)
    assert bars[2].exportDocument(QUrl.fromLocalFile(str(target)))
    assert len(list(target.glob("resource-export-*/*.cfilm"))) == 1
    assert not bars[2].dirty and bars[2].currentDocumentId == saved.document_id
    activate_document(views[1][0], bars[1], other.document_id)
    assert all(bar.currentDocumentId == other.document_id for bar in bars)
    assert len(client._views) == 3
    for window, *_rest in views:
        assert len(window.findChildren(QQuickItem, "projectManagedDocumentPicker")) == 1
        window.show()
        for width in (1280, 720):
            window.setProperty("width", width)
            events()
            bar = window.findChild(QQuickItem, "projectResourceControls")
            for item in bar.childItems():
                if item.isVisible() and item.width() > 0:
                    assert item.x() >= -1 and item.x() + item.width() <= bar.width() + 1


def test_film_information_view_edit_cancel_token_and_independent_project(desktop):
    client, stores = desktop
    workspaces = [client.open_project(root) for root in stores]
    left, right = workspaces
    for workspace in workspaces:
        saved = workspace.service.create_film(bound_design(), materials={"shared": material()})
        assert workspace.editor.request_load_saved_design(saved.document_id)
    window, source, *_rest = view_for(client, left, "film_design")
    resources = source._project_input_resources
    assert not window.findChild(QQuickItem, "filmDesignNameField").isVisible()
    assert not window.findChild(QQuickItem, "filmDesignNoteButton").isVisible()
    button = window.findChild(QQuickItem, "projectResourceInformationButton")
    assert QMetaObject.invokeMethod(button, "clicked", Qt.ConnectionType.DirectConnection)
    dialog = window.findChild(QObject, "projectResourceInformationDialog")
    assert dialog.property("visible") and not dialog.property("editing")
    assert QMetaObject.invokeMethod(dialog, "beginEditing", Qt.ConnectionType.DirectConnection)
    fields = [window.findChild(QQuickItem, name) for name in
              ("projectResourceInformationName", "projectResourceInformationSummary", "projectResourceInformationNotes")]
    fields[0].setProperty("text", "Discard name")
    cancel = window.findChild(QQuickItem, "projectResourceInformationCancelButton")
    assert QMetaObject.invokeMethod(cancel, "clicked", Qt.ConnectionType.DirectConnection)
    assert source.currentDesign()["name"] == "Bound project film" and not source.dirty
    assert QMetaObject.invokeMethod(button, "clicked", Qt.ConnectionType.DirectConnection)
    assert QMetaObject.invokeMethod(dialog, "beginEditing", Qt.ConnectionType.DirectConnection)
    for field, value in zip(fields, ("Named film", "Film overview", "Film notes"), strict=True):
        field.setProperty("text", value)
    assert QMetaObject.invokeMethod(dialog, "applyEdit", Qt.ConnectionType.DirectConnection)
    assert not dialog.property("visible")
    assert resources.saveCurrent(), resources.status
    identity = resources.currentDocumentId
    film = left.service.load_document(identity).design
    assert (film["name"], film["summary"], film["note"]) == ("Named film", "Film overview", "Film notes")
    assert right.editor.snapshot().design["name"] == "Bound project film"
    other_id = right.service.list_saved_designs()[0]["design_id"]
    assert not resources.openDocument(other_id)
    info = resources.resourceInformation
    assert source.setDesignValue("note", "Concurrent note")
    assert not resources.updateResourceInformation("Stale", "Stale", "Stale", info["token"])
    assert source.currentDesign()["note"] == "Concurrent note"
    assert resources.saveCopyCurrent()
    assert resources.currentDocumentId != identity
    assert left.service.load_document(identity).design["note"] == "Film notes"


def test_deposition_input_bar_preserves_process_dirty_cancel_metadata_and_reopen(tmp_path):
    store, _external, process_id, original_id = setup_project(tmp_path)
    client = ProjectFilmDesktop(ProjectSessionHost(lambda _root: store), geometry_settings=MemorySettings())
    try:
        workspace = client.open_project(store.root)
        client.open_project_module(workspace, "main")
        window, bridge, *_rest = view_for(client, workspace, "main")
        resources = bridge._project_input_resources
        assert resources.currentDocumentId == original_id
        assert resources.saveCopyCurrent()
        current_id = resources.currentDocumentId
        bridge.setField("grid_size", "37")
        picker = activate_document(window, resources, original_id)
        assert resources._pending_replacement is not None
        assert str(picker.property("currentValue")) == current_id
        assert resources.confirmReplace("cancel")
        window.findChild(QObject, "projectResourceReplaceDialog").close()
        assert bridge.fields["grid_size"] == "37" and resources.currentDocumentId == current_id
        assert not resources.newDocument()
        assert resources.confirmReplace("save")
        assert store.resolve_document(current_id)["configuration"]["parameters"]["settings"]["grid_size"] == 37
        assert resources.currentDocumentId == "" and bridge._project_compute.process_document_id == process_id
        info = resources.resourceInformation
        assert resources.updateResourceInformation("New deposition inputs", "Overview", "Notes", info["token"])
        assert resources.saveCurrent(), resources.status
        new_id = resources.currentDocumentId
        assert new_id not in {original_id, current_id}
        assert resources.openDocument(original_id) and resources.openDocument(new_id)
        assert resources.resourceInformation["summary"] == "Overview"
        assert resources.resourceInformation["notes"] == "Notes"
        export = store.root / "data" / "configuration-export"
        export.mkdir(parents=True)
        assert resources.exportDocument(QUrl.fromLocalFile(str(export)))
        assert len(list(export.glob("resource-export-*/*.cdep"))) == 1
        assert window.findChild(QQuickItem, "projectDepositionConfigurationSelector") is None
        assert len(window.findChildren(QQuickItem, "projectManagedDocumentPicker")) == 1
        info = resources.resourceInformation
        assert resources.stageInformationEdit("Unconfirmed", "Pending", "Pending", info["token"])
        checkpoint = bridge._project_compute.snapshot()
        bridge._project_compute.validate_recovery(checkpoint)
        assert checkpoint["draft"]["pending_information"]["name"] == "Unconfirmed"
        assert not resources.saveCurrent()
        resources.clearInformationEdit()
        assert resources.resourceInformation["name"] == "New deposition inputs"
    finally:
        for _route, view in tuple(client._views.items()):
            view[0].setProperty("allowClose", True)
            view[0].setProperty("projectCloseAuthorized", True)
            view[0].close()
        events()
        client.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_film_information_recovery_keeps_unconfirmed_buffer_and_unsaved_copy(desktop):
    from coat_sim.core.project_film_recovery import (
        film_recovery_current_token, restore_film_recovery, snapshot_film_recovery, validate_film_recovery,
    )
    client, stores = desktop
    workspace = client.open_project(next(iter(stores)))
    saved = workspace.service.create_film(bound_design(), materials={"shared": material()})
    assert workspace.editor.request_load_saved_design(saved.document_id)
    source = client.controller_for(workspace, "film_design")
    resources = source._project_input_resources
    info = resources.resourceInformation
    assert resources.stageInformationEdit("Unconfirmed", "Pending summary", "Pending notes", info["token"])
    assert source.dirty and not workspace.editor.snapshot().dirty
    assert not workspace.editor.save_current()
    checkpoint = snapshot_film_recovery(workspace)
    assert checkpoint["pending_information"]["name"] == "Unconfirmed"
    assert checkpoint["film"]["design"]["name"] == "Bound project film"
    validate_film_recovery(workspace, checkpoint)
    resources.clearInformationEdit()
    token = film_recovery_current_token(workspace)
    assert restore_film_recovery(workspace, checkpoint, approved=True,
        expected_current_design_revision=token["design_revision"],
        expected_current_session_revision=token["session_revision"],
        expected_current_document_id=token["managed_document_id"],
        expected_current_document_revision=token["managed_document_revision"])
    events()
    assert resources.pendingInformationEdit["token"] == resources.resourceInformation["token"]
    dialog = view_for(client, workspace, "film_design")[0].findChild(QObject, "projectResourceInformationDialog")
    assert dialog.property("visible") and dialog.property("editing")
    resources.clearInformationEdit()
    dialog.close()
    assert source.dirty and workspace.editor.snapshot().path is None
    assert source.currentDesign()["name"] == "Bound project film"
    assert ProjectStore.open(workspace.service.project_store.project_file).manifest
