"""Project-owned metadata editing, identity fencing and real Qt publication."""
from __future__ import annotations

from unittest.mock import patch
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtQuick import QQuickItem

from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_recovery import ProjectOwnerLease
from coat_sim.ui.project_film_host import ProjectFilmNavigation
from coat_sim.ui.project_runtime_owner import ProjectRuntimeOwner
from tests.test_project_runtime_management import (
    _click, _eval, _events, _find_item, _show_management_item,
    management_qml as _management_qml,
)

management_qml = _management_qml

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def information_owner(tmp_path):
    owner = ProjectRuntimeOwner()
    stores = [ProjectStore.create(tmp_path / key, name, summary="原摘要", notes="原备注")
              for key, name in (("a", "工程甲"), ("b", "工程乙"))]
    sessions = []
    for store in stores:
        owner._staged[store.root] = store
        session = owner.host.open_project(store.root)
        owner._leases[session.binding.project_id] = ProjectOwnerLease.acquire(
            store.project_file, session.binding.project_id)
        sessions.append(session)
    try:
        yield owner, stores, sessions
    finally:
        for route, view in tuple(owner.desktop._views.items()):
            view[0].setProperty("allowClose", True)
            view[0].setProperty("projectCloseAuthorized", True)
            view[0].setProperty("closeAuthorized", True)
            with owner.desktop.authorized_project_close(route.binding):
                view[0].close()
        owner.stop()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        _events()


def _save(owner, info, name="新工程名称", summary="新摘要", notes="新备注"):
    return owner.saveProjectInformation(info["projectId"], info["sessionGeneration"],
                                        info["revision"], name, summary, notes)


def test_information_publication_is_project_scoped_descriptor_only_and_notifies(information_owner):
    owner, stores, sessions = information_owner
    store, other = stores
    session = sessions[0]
    resource_id = store.create_document("source", {"schema_version": 2, "kind": "point", "name": "独立资源名称"})
    record = store.manifest["documents"][0]
    resource_path = store.root / record["path"]
    resource_bytes = resource_path.read_bytes()
    other_bytes = other.project_file.read_bytes()
    workspace = ProjectFilmWorkspace.for_project(owner.host, session.binding)
    resource_service = workspace.service
    navigation = ProjectFilmNavigation(owner.desktop, workspace)
    observed: list[str] = []
    name_changes = []
    owner.projectInformationChanged.connect(observed.append)
    navigation.projectNameChanged.connect(lambda: name_changes.append(navigation.projectName))
    before = owner.projectInformation(session.binding.project_id)
    before["summary"] = "detached local mutation"
    assert owner.projectInformation(session.binding.project_id)["summary"] == "原摘要"

    assert _save(owner, before, "  改名后的工程  ", "新摘要\n第二行", "新备注 <b>原样文本</b>")

    saved = ProjectStore.open(store.project_file).manifest
    assert saved["id"] == session.binding.project_id
    assert (saved["name"], saved["summary"], saved["notes"]) == (
        "改名后的工程", "新摘要\n第二行", "新备注 <b>原样文本</b>")
    assert saved["documents"] == [record]
    assert resource_path.read_bytes() == resource_bytes
    assert saved["documents"][0]["id"] == resource_id
    assert other.project_file.read_bytes() == other_bytes
    assert owner.host.sessions()[0].binding == session.binding
    assert owner.host.sessions()[0].name == "改名后的工程"
    assert ProjectFilmWorkspace.for_project(owner.host, session.binding) is workspace
    assert workspace.service is resource_service
    assert observed == [session.binding.project_id]
    assert name_changes == ["改名后的工程"]
    assert owner.projectEntries[0]["name"] == "改名后的工程"
    navigation.deleteLater()


@pytest.mark.parametrize("failure", ["revision", "generation", "other_generation", "closed", "pending_close", "exit", "publish", "missing_owner"])
def test_information_rejects_stale_or_unavailable_targets_without_writing(information_owner, failure):
    owner, stores, sessions = information_owner
    store = stores[0]
    info = owner.projectInformation(sessions[0].binding.project_id)
    if failure == "revision":
        store.update_information({"summary": "更新的摘要"}, expected_revision=info["revision"])
    if failure in {"generation", "other_generation"}:
        info["sessionGeneration"] = "old-generation" if failure == "generation" else sessions[1].binding.generation
    if failure == "closed":
        assert owner.host.close_project(sessions[0].binding, prepare_close=lambda *_: True)
    if failure == "missing_owner":
        owner._leases.pop(info["projectId"]).close()
    if failure == "pending_close":
        owner.project_closer._reviews[info["projectId"]] = (info["sessionGeneration"], "signature")
    before = [store.project_file.read_bytes() for store in stores]
    if failure == "exit":
        with owner.host.exit_admission_freeze():
            accepted = _save(owner, info)
    elif failure == "publish":
        with patch("coat_sim.core.project_store.replace_with_retry", side_effect=OSError("read-only destination")):
            accepted = _save(owner, info)
    else:
        accepted = _save(owner, info)
    assert not accepted
    assert owner.projectError
    assert [store.project_file.read_bytes() for store in stores] == before


def test_management_real_owner_save_cancel_and_stale_failure(management_qml, information_owner):
    app, engine, window, root, fake = management_qml
    owner, stores, sessions = information_owner
    store = stores[0]
    project_id = sessions[0].binding.project_id
    root.setProperty("projectBridge", owner)
    try:
        assert _eval(engine, root, f"openFor('{project_id}')") is True
        name = _find_item(root, "projectRuntimeInformationNameField")
        summary = _find_item(root, "projectRuntimeInformationSummaryField")
        notes = _find_item(root, "projectRuntimeInformationNotesField")
        name.setProperty("text", "取消名称")
        summary.setProperty("text", "取消摘要")
        notes.setProperty("text", "取消备注")
        before = store.project_file.read_bytes()
        cancel = _find_item(root, "projectRuntimeInformationCancelButton")
        _show_management_item(root, cancel)
        _click(window, cancel)
        assert store.project_file.read_bytes() == before
        assert [field.property("text") for field in (name, summary, notes)] == ["工程甲", "原摘要", "原备注"]

        name.setProperty("text", "Qt 已保存工程名称")
        summary.setProperty("text", "Qt 已保存摘要\n第二行")
        notes.setProperty("text", "Qt 已保存备注")
        save = _find_item(root, "projectRuntimeInformationSaveButton")
        _show_management_item(root, save)
        _click(window, save)
        saved = ProjectStore.open(store.project_file).manifest
        assert (saved["name"], saved["summary"], saved["notes"]) == (
            "Qt 已保存工程名称", "Qt 已保存摘要\n第二行", "Qt 已保存备注")
        assert _find_item(root, "projectRuntimeManagementProjectId").property("text") == "当前工程：Qt 已保存工程名称"
        assert not root.property("informationDirty")

        name.setProperty("text", "陈旧保存须保留名称")
        summary.setProperty("text", "陈旧保存须保留摘要")
        notes.setProperty("text", "陈旧保存须保留备注")
        store.update_information({"notes": "其他已保存更新"}, expected_revision=saved["revision"])
        before = store.project_file.read_bytes()
        _click(window, save)
        assert store.project_file.read_bytes() == before
        assert [field.property("text") for field in (name, summary, notes)] == [
            "陈旧保存须保留名称", "陈旧保存须保留摘要", "陈旧保存须保留备注"]
        assert root.property("informationDirty") and root.property("localError")
        _click(window, cancel)
        assert notes.property("text") == "其他已保存更新"
        assert not root.property("informationDirty")
    finally:
        root.setProperty("projectBridge", fake)


def test_open_designer_and_resource_name_views_refresh_together(information_owner):
    owner, stores, sessions = information_owner
    session = sessions[0]
    workspace = owner.desktop.open_project(stores[0].root)
    owner.desktop.open_project_module(workspace, "source")
    _events()
    views = [view for route, view in owner.desktop._views.items() if route.binding == session.binding]
    assert len(views) == 2
    info = owner.projectInformation(session.binding.project_id)
    assert _save(owner, info, "当前工程显示新名称")
    _events()
    assert all(view[1].projectName == "当前工程显示新名称" for view in views)
    for view in views:
        labels = (view[0].findChildren(QQuickItem, "projectNavigationName")
                  + view[0].findChildren(QQuickItem, "filmDesignProjectName"))
        assert labels
        assert all(label.property("text") == "当前工程显示新名称" for label in labels)


def test_pending_information_uses_unified_close_cancel_return_and_discard(management_qml, information_owner):
    app, engine, window, root, fake = management_qml
    owner, stores, sessions = information_owner
    session = sessions[0]
    project_id = session.binding.project_id
    root.setProperty("projectBridge", owner)
    reviews: list[str] = []
    returned: list[str] = []
    owner.projectCloseConfirmationRequested.connect(reviews.append)
    owner.desktop.managementRequested.connect(returned.append)
    try:
        assert _eval(engine, root, f"openFor('{project_id}')") is True
        name = _find_item(root, "projectRuntimeInformationNameField")
        summary = _find_item(root, "projectRuntimeInformationSummaryField")
        name.setProperty("text", "等待处理的工程名称")
        summary.setProperty("text", "等待处理的工程摘要")
        before = stores[0].project_file.read_bytes()
        pending = owner.pending_project_information(session.binding)
        assert pending["name"] == "等待处理的工程名称"
        assert pending["summary"] == "等待处理的工程摘要"
        assert not owner.closeProject(project_id)
        assert reviews == [project_id]
        assert stores[0].project_file.read_bytes() == before
        owner.cancelCloseProject(project_id)
        assert owner.pending_project_information(session.binding) == pending
        assert name.property("text") == "等待处理的工程名称"

        assert not owner.closeProject(project_id)
        assert owner.returnToSaveProject(project_id)
        assert returned == [project_id]
        assert _eval(engine, root, f"openFor('{project_id}')") is True
        assert name.property("text") == "等待处理的工程名称"
        assert summary.property("text") == "等待处理的工程摘要"
        assert stores[0].project_file.read_bytes() == before
        assert not owner.closeProject(project_id)
        assert owner.confirmCloseProject(project_id)
        assert stores[0].project_file.read_bytes() == before
        assert not owner.pending_project_information(session.binding)
        assert [row.binding for row in owner.host.sessions()] == [sessions[1].binding]
    finally:
        root.setProperty("projectBridge", fake)


def test_last_window_close_finds_pending_information_and_cancel_keeps_window(management_qml, information_owner):
    app, engine, window, root, fake = management_qml
    owner, stores, sessions = information_owner
    session = sessions[0]
    owner.desktop.open_project(stores[0].root)
    _events()
    view = next(value[0] for route, value in owner.desktop._views.items() if route.binding == session.binding)
    root.setProperty("projectBridge", owner)
    reviews: list[str] = []
    owner.projectCloseConfirmationRequested.connect(reviews.append)
    try:
        assert _eval(engine, root, f"openFor('{session.binding.project_id}')") is True
        _find_item(root, "projectRuntimeInformationNotesField").setProperty("text", "未保存的工程备注")
        assert owner.pending_project_information(session.binding)
        assert not view.close()
        _events()
        assert reviews == [session.binding.project_id], (owner.projectError, owner.project_closer._last_window_requests,
                                                        owner.project_closer._reviews)
        owner.cancelCloseProject(session.binding.project_id)
        _events()
        assert view.isVisible()
        assert owner.pending_project_information(session.binding)["notes"] == "未保存的工程备注"
        assert _find_item(root, "projectRuntimeInformationNotesField").property("text") == "未保存的工程备注"
    finally:
        root.setProperty("projectBridge", fake)


def test_pending_information_change_invalidates_an_existing_close_decision(information_owner):
    owner, stores, sessions = information_owner
    session = sessions[0]
    info = owner.projectInformation(session.binding.project_id)
    assert owner.stageProjectInformationEdit(info["projectId"], info["sessionGeneration"], info["revision"],
                                             "第一次输入", "", "")
    assert not owner.closeProject(info["projectId"])
    assert owner.stageProjectInformationEdit(info["projectId"], info["sessionGeneration"], info["revision"],
                                             "确认期间的新输入", "", "")
    before = stores[0].project_file.read_bytes()
    assert not owner.confirmCloseProject(info["projectId"])
    assert stores[0].project_file.read_bytes() == before
    assert "确认期间" in owner.projectError
    assert owner.host.store_for(session.binding) is stores[0]
    assert owner.pending_project_information(session.binding)["name"] == "确认期间的新输入"


def test_application_exit_rechecks_information_edited_during_review(management_qml, information_owner):
    from coat_sim.ui.project_application_lifecycle import ProjectApplicationLifecycle
    from tests.test_project_application_lifecycle import Bridge
    _app, engine, window, root, fake = management_qml
    owner, stores, sessions = information_owner
    session = sessions[0]
    bridge: Any = Bridge()
    bridge.project_runtime = owner
    bridge._project_management_window = window
    bridge.dirty = False
    lifecycle = ProjectApplicationLifecycle(bridge, "exit-generation")
    notices = []
    bridge.shutdownConfirmationRequested.connect(lambda: notices.append(True))
    args = {"client_generation": "exit-generation", "exit_id": "review-information"}
    root.setProperty("projectBridge", owner)
    before = stores[0].project_file.read_bytes()
    try:
        assert _eval(engine, root, f"openFor('{session.binding.project_id}')") is True
        notes = _find_item(root, "projectRuntimeInformationNotesField")
        notes.setProperty("text", "First review input")
        assert lifecycle.dispatch("lifecycle_prepare", args)["state"] == "pending"
        assert len(notices) == 1 and notes.isEnabled()
        notes.setProperty("text", "New input after review started")
        assert owner.pending_project_information(session.binding)["notes"] == "New input after review started"
        lifecycle.decide(True)
        assert lifecycle.dispatch("lifecycle_prepare", args)["state"] == "pending"
        assert len(notices) == 2 and not owner.host.admission_frozen
        assert stores[0].project_file.read_bytes() == before and not bridge._closing
        lifecycle.decide(False)
        assert lifecycle.dispatch("lifecycle_prepare", args)["state"] == "declined"
        assert notes.property("text") == "New input after review started"
        assert owner.pending_project_information(session.binding)["notes"] == "New input after review started"
    finally:
        lifecycle._timer.stop()
        lifecycle._cancel()
        root.setProperty("projectBridge", fake)


def test_application_exit_ready_freezes_management_fields_and_cancel_restores(management_qml, information_owner):
    from coat_sim.ui.project_application_lifecycle import ProjectApplicationLifecycle
    from tests.test_project_application_lifecycle import Bridge
    _app, engine, window, root, fake = management_qml
    owner, stores, sessions = information_owner
    session = sessions[0]
    bridge: Any = Bridge()
    bridge.project_runtime = owner
    bridge._project_management_window = window
    bridge.dirty = False
    lifecycle = ProjectApplicationLifecycle(bridge, "exit-generation")
    args = {"client_generation": "exit-generation", "exit_id": "freeze-information"}
    root.setProperty("projectBridge", owner)
    before = stores[0].project_file.read_bytes()
    try:
        assert _eval(engine, root, f"openFor('{session.binding.project_id}')") is True
        notes = _find_item(root, "projectRuntimeInformationNotesField")
        notes.setProperty("text", "Retain across global exit cancel")
        assert lifecycle.dispatch("lifecycle_prepare", args)["state"] == "pending"
        lifecycle.decide(True)
        ready = lifecycle.dispatch("lifecycle_prepare", args)
        assert ready["state"] == "ready" and owner.host.admission_frozen
        assert not notes.isEnabled() and not window.contentItem().isEnabled()
        pending = owner.pending_project_information(session.binding)
        assert not owner.stageProjectInformationEdit(session.binding.project_id, session.binding.generation,
            pending["revision"], "Rejected during freeze", "", "")
        owner.clearProjectInformationEdit(session.binding.project_id, session.binding.generation)
        assert owner.pending_project_information(session.binding) == pending
        lifecycle.dispatch("lifecycle_cancel", args | {"permit_id": ready["permit_id"]})
        assert notes.isEnabled() and window.contentItem().isEnabled()
        assert not owner.host.admission_frozen and not bridge._closing
        assert notes.property("text") == "Retain across global exit cancel"
        assert owner.pending_project_information(session.binding)["notes"] == "Retain across global exit cancel"
        assert stores[0].project_file.read_bytes() == before
    finally:
        lifecycle._timer.stop()
        lifecycle._cancel()
        root.setProperty("projectBridge", fake)
