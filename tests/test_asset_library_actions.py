"""Asset deletion stays previewed and recoverable through the owning service."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.material_business import MaterialLibrary
from coat_sim.ui.global_resources import GlobalResourcesBridge
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.test_launcher_information_global_resources import global_root as global_root, seed_source
from tests.test_project_global_resources import profile
from tests.test_project_last_window_close import desktop as desktop, drain_events, spin_until
from tests.test_scoped_resource_libraries import open_library, settled
from tests.test_unified_asset_materials import seed_project_material

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def seed_global_material(global_root):
    record = profile()
    record["id"] = "asset-ui-test-glass"
    record["name"] = "Asset UI test glass"
    directory = global_root / "data/materials"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (record["id"] + ".json")).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def action_settled(assets):
    spin_until(lambda: assets.deletionBusy, lambda busy: not busy, "asset action")
    settled(assets)
    QTest.qWait(20)


def click(window, name):
    item = cast(Any, window.findChild(QQuickItem, name))
    pending = [window.contentItem()]
    while item is None and pending:
        candidate = pending.pop()
        if candidate.objectName() == name:
            item = candidate
        pending.extend(candidate.childItems())
    assert item is not None, name
    assert item.isEnabled(), name
    item.clicked.emit()


def visible(window, name):
    dialog = window.findChild(QObject, name)
    return dialog is not None and bool(dialog.property("visible"))


@pytest.fixture
def global_assets(global_root):
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    assets: Any = GlobalResourcesBridge()
    engine.rootContext().setContextProperty("bridge", assets)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path(__file__).parents[1] / "coat_sim/ui/qml/ResourceLibraryWindow.qml")))
    window = cast(Any, component.create())
    assert window is not None, component.errorString()
    window.show()
    settled(assets)
    yield window, assets, engine
    action_settled(assets)
    assets.dispose()
    window.setProperty("allowClose", True)
    window.deleteLater()
    engine.deleteLater()
    drain_events()


def test_global_delete_preview_requires_confirmation_and_cancel_keeps_asset(global_assets, global_root):
    window, assets, _engine = global_assets
    path = seed_source(global_root)
    original = path.read_bytes()
    assert assets.browseLibrary()
    settled(assets)
    identity = next(row["id"] for row in assets.transferCatalog["rows"] if row["kind"] == "source")
    window.setProperty("selectedIds", [identity])
    assert window.findChild(QQuickItem, "assetLibraryRecycleButton") is None
    assert window.findChild(QObject, "assetDeletionHistoryDialog") is None
    click(window, "assetLibraryDeleteButton")
    action_settled(assets)
    assert visible(window, "assetDeletionPreviewDialog")
    assert assets.deletionPlan["scope"] == "global" and assets.deletionPlan["can_apply"]
    assert path.read_bytes() == original
    confirm = window.findChild(QQuickItem, "assetDeletionConfirmButton")
    assert confirm is not None and confirm.isEnabled()
    click(window, "assetDeletionCancelButton")
    assert not assets.deletionPlan and path.read_bytes() == original
    assert not assets.confirmAssetDeletion()


def test_project_ui_delete_remains_owner_scoped_and_blocks_stale_or_external_sources(desktop, global_root):
    first, second = desktop.open("First"), desktop.open("Second")
    first_store = desktop.adapter.host.store_for(first.session.binding)
    second_store = desktop.adapter.host.store_for(second.session.binding)
    identity = first_store.create_document("source", {"schema_version": 2, "kind": "point", "device_type": "custom",
        "source_exponent": 1., "name": "Only first"})
    second_store.create_document("source", {"schema_version": 2, "kind": "point", "device_type": "custom",
        "source_exponent": 2., "name": "Only second"})
    before_second = deepcopy(second_store.manifest)
    global_path = seed_source(global_root)
    global_bytes = global_path.read_bytes()
    _instance, _route, (window, assets, *_rest) = open_library(desktop, first)
    assert assets.previewAssetDeletion([identity])
    action_settled(assets)
    assert assets.deletionPlan["can_apply"] and assets.deletionPlan["project_id"] == first.session.binding.project_id
    click(window, "assetDeletionConfirmButton")
    assert assets.assetApplying
    with pytest.raises(ValueError, match="正在提交"):
        desktop.adapter.desktop.open_project_module(first.workspace, "source")
    assert not assets.previewLibraryTransfer([identity], "global")
    assert not assets.beginTransfer("import")
    assert not assets.inspectTransferSource("global")
    assert not assets.previewTransfer([identity], "global")
    assert not assets.applyTransfer({})
    from coat_sim.ui.project_application_client import ProjectApplicationClient
    from coat_sim.control_errors import UiControlError
    bridge: Any = QObject()
    bridge.project_runtime = desktop.adapter
    bridge.project_lifecycle = None
    client = ProjectApplicationClient(bridge, None)
    try:
        with pytest.raises(UiControlError) as managed:
            client.dispatch("project_manage", {"client_generation": client.generation,
                "session_generation": first.session.binding.generation,
                "project_operation": "create_document", "arguments": {
                    "project_id": first.session.binding.project_id, "root": str(first.store.project_file)}})
        assert managed.value.code == "window_busy"
    finally:
        client.stop()
        bridge.deleteLater()
    original_catalog = assets.transferCatalog["catalog_id"]
    assets.cancelTransfer()
    assert assets.transferCatalog["catalog_id"] == original_catalog
    assert not first.window.contentItem().isEnabled() and second.window.contentItem().isEnabled()
    action_settled(assets)
    assert identity not in {row["id"] for row in first_store.manifest["documents"]}
    assert second_store.manifest == before_second and global_path.read_bytes() == global_bytes
    assert not assets.confirmAssetDeletion()
    assert assets.browseSource("global")
    settled(assets)
    foreign = assets.transferCatalog["rows"][0]["id"]
    assert not assets.previewAssetDeletion([foreign])
    assert "导入来源不可删除" in assets.deletionMessage
    assert global_path.read_bytes() == global_bytes


def test_clean_embedded_project_material_is_one_paired_asset_and_dirty_input_blocks(desktop):
    project = desktop.open()
    row = seed_project_material(desktop, project)
    _instance, _route, (window, assets, *_rest) = open_library(desktop, project)
    assert assets.openMaterialAsset(row["id"])
    library = assets.materialLibrary
    original = library.draftData()
    library.setField("notes", "Keep exact unsaved input")
    assert not assets.previewAssetDeletion([row["id"]])
    assert library.draftData()["notes"] == "Keep exact unsaved input"
    assert library.save()
    settled(assets)
    assert assets.previewAssetDeletion([row["id"]])
    action_settled(assets)
    plan = assets.deletionPlan
    assert plan["can_apply"], plan
    assert {item["kind"] for item in plan["rows"]} == {"material_profile", "optical_material"}
    assert library.currentId == original["id"]
    click(window, "assetDeletionCancelButton")
    assert library.currentId == original["id"] and not library.dirty
    planned_ids = {item["id"] for item in plan["rows"]}
    retained = [item for item in assets.service.store.manifest["documents"]
                if item["kind"] in {"material_profile", "optical_material"}]
    assert {item["id"] for item in retained} == planned_ids
    assert assets.openMaterialAsset(row["id"])
    assert assets.materialLibrary.draftData()["notes"] == "Keep exact unsaved input"


def test_unsaved_embedded_optical_dialog_and_native_buffers_cannot_start_delete(desktop):
    project = desktop.open()
    row = seed_project_material(desktop, project)
    _instance, _route, (window, assets, *_rest) = open_library(desktop, project)
    assert assets.openMaterialAsset(row["id"])
    window.setProperty("projectPendingTextEdited", True)
    assert not assets.previewAssetDeletion([row["id"]])
    window.setProperty("projectPendingTextEdited", False)
    modal = QObject(window)
    modal.setObjectName("opticalDatasetUnconfirmedForm")
    modal.setProperty("visible", True)
    modal.setProperty("modal", True)
    assert not assets.previewAssetDeletion([row["id"]])
    assert assets.materialLibrary.currentId == "glass"
    modal.setProperty("visible", False)
    modal.deleteLater()


def test_global_material_delete_preview_preserves_paired_dataset(global_assets, global_root):
    window, assets, _engine = global_assets
    record = seed_global_material(global_root)
    library = MaterialLibrary(directory=global_root / "data/materials")
    original = library.read(record["id"])["record"]
    assert assets.browseLibrary()
    settled(assets)
    identity = next(row["id"] for row in assets.transferCatalog["rows"]
                    if row["kind"] == "material_profile" and row["material_id"] == record["id"])
    assert assets.openMaterialAsset(identity)
    spin_until(lambda: assets.materialLibrary.loading, lambda busy: not busy, "material viewer")
    assert assets.previewAssetDeletion([identity])
    action_settled(assets)
    assert assets.materialLibrary.currentId == record["id"]
    assert len(assets.deletionPlan["rows"]) == 2
    assert window.findChild(QQuickItem, "assetLibraryRecycleButton") is None
    click(window, "assetDeletionCancelButton")
    assert library.read(record["id"])["record"] == original
    assert assets.materialLibrary.currentId == record["id"]
    assert len(library.catalog_records()) == 1


def test_cancelled_preview_late_result_releases_token_without_removing_asset(global_assets, global_root, monkeypatch):
    from threading import Event
    window, assets, _engine = global_assets
    path = seed_source(global_root)
    assert assets.browseLibrary()
    settled(assets)
    identity = assets.transferCatalog["rows"][0]["id"]
    entered, release = Event(), Event()
    original = assets.service.preview_resource_deletion
    def slow_preview(catalog_id, identities):
        entered.set()
        assert release.wait(3)
        return original(catalog_id, identities)
    monkeypatch.setattr(assets.service, "preview_resource_deletion", slow_preview)
    ready = []
    assets.deletionPreviewReady.connect(lambda: ready.append(True))
    assert assets.previewAssetDeletion([identity])
    assert entered.wait(2)
    assets.cancelAssetDeletion()
    release.set()
    action_settled(assets)
    assert not ready and not assets.deletionPlan and path.exists()
    assert not assets.service.resource_deletions._plans
    assert not visible(window, "assetDeletionPreviewDialog")


def test_unsupported_global_binary_explains_limitation_without_calling_delete(global_assets, monkeypatch):
    window, assets, _engine = global_assets
    identity = "0" * 32
    assets._transfer_catalog["rows"] = [{"id": identity, "kind": "binary_asset", "name": "Referenced input"}]
    assets._transfer_catalog["categories"] = ["binary_asset"]
    assets.transferChanged.emit()
    monkeypatch.setattr(assets.service, "preview_resource_deletion",
                        lambda *_: pytest.fail("unsupported global input must not admit core deletion"))
    assert not assets.previewAssetDeletion([identity])
    assert "暂不支持单独回收" in assets.deletionMessage
    message = window.findChild(QQuickItem, "assetDeletionMessage")
    assert message is not None and message.property("visible") and "保持不变" in message.property("text")
