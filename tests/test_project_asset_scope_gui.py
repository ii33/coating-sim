"""Real asset-library confirmation stays scoped around an unrelated legacy row."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.project_dependencies import ProjectError, json_bytes
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_store import ProjectStore
from tests.design_test_fixtures import _RECTANGULAR_SOURCE
from tests.test_asset_catalog_diagnostics import obsolete_source
from tests.test_project_film_service import EmptyCatalog, bound_design, material
from tests.test_project_last_window_close import desktop as desktop, spin_until
from tests.test_scoped_resource_libraries import open_library, settled

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def create_legacy_film_project(root: Path) -> tuple[ProjectStore, str, str, bytes, bytes]:
    """Seed a valid film beside an unrelated, byte-preserved obsolete source."""
    store = ProjectStore.create(root, "Legacy source beside film")
    source_id = store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    film_service = ProjectFilmService(store, global_material_library=EmptyCatalog())
    film = film_service.create_film(bound_design(), materials={"shared": material()})

    source_row = next(row for row in store.manifest["documents"] if row["id"] == source_id)
    source_path = store.root / source_row["path"]
    source_bytes = json_bytes(obsolete_source())
    source_path.write_bytes(source_bytes)
    manifest = store.manifest
    next(row for row in manifest["documents"] if row["id"] == source_id)["sha256"] = hashlib.sha256(source_bytes).hexdigest()
    store.project_file.write_bytes(json_bytes(manifest))
    store = ProjectStore.open(store.project_file)

    film_row = next(row for row in manifest["documents"] if row["id"] == film.document_id)
    film_path = store.root / film_row["path"]
    return store, film.document_id, source_id, source_bytes, film_path.read_bytes()


def admit_project(desktop, store: ProjectStore):
    adapter = desktop.adapter
    assert adapter.openProject(str(store.project_file))
    spin_until(lambda: adapter.projectBusy, lambda busy: not busy, "project admission")
    assert adapter.projectError == ""
    session = next(row for row in adapter.host.sessions()
                   if row.binding.project_id == store.manifest["id"])
    workspace = ProjectFilmWorkspace.for_project(adapter.host, session.binding)
    return SimpleNamespace(store=store, session=session, workspace=workspace)


def find_item(window, name: str) -> QQuickItem:
    pending = [window.contentItem()]
    while pending:
        candidate = pending.pop()
        if candidate.objectName() == name:
            return cast(QQuickItem, candidate)
        pending.extend(candidate.childItems())
    raise AssertionError(f"QML item not found: {name}")


def click_qml(window, name: str) -> None:
    """Send a real mouse click to the visible Quick item, not its clicked signal."""
    item = find_item(window, name)
    assert item.isVisible(), name
    assert item.isEnabled(), name
    point = item.mapToScene(item.boundingRect().center())
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(round(point.x()), round(point.y())))


def wait_asset_action(assets: Any) -> None:
    spin_until(lambda: assets.deletionBusy, lambda busy: not busy, "asset action")
    settled(assets)
    QTest.qWait(20)


def visible(window, name: str) -> bool:
    item = window.findChild(QObject, name)
    return item is not None and bool(item.property("visible"))


def test_real_asset_library_confirmation_ignores_unrelated_obsolete_source(desktop, tmp_path):
    original_store, film_id, source_id, old_source_bytes, film_bytes = create_legacy_film_project(
        tmp_path / "legacy-project")
    project = admit_project(desktop, original_store)
    _route_id, _route, (window, assets, *_rest) = open_library(desktop, project)

    catalog = assets.transferCatalog
    assert catalog["source_type"] == "project"
    assert any(row["id"] == film_id and row["kind"] == "film" for row in catalog["rows"])
    source_path = original_store.root / next(
        row["path"] for row in original_store.manifest["documents"] if row["id"] == source_id)
    assert source_path.read_bytes() == old_source_bytes
    assert hashlib.sha256(old_source_bytes).hexdigest() == next(
        row["sha256"] for row in original_store.manifest["documents"] if row["id"] == source_id)
    assert any(str(source_path) in warning and "aperture_kind" in warning for warning in catalog["warnings"])
    with pytest.raises(ProjectError, match="aperture_kind"):
        assets.service.store.resolve_document(source_id)

    window.setProperty("categoryKey", "film")
    window.setProperty("selectedIds", [film_id])
    before_manifest = deepcopy(original_store.manifest)
    deletion_finished: list[bool] = []
    assets.deletionFinished.connect(deletion_finished.append)

    # The first actual QML click only opens review. A real cancel keeps every
    # asset and byte in place before the same operation is explicitly confirmed.
    click_qml(window, "assetLibraryDeleteButton")
    wait_asset_action(assets)
    assert visible(window, "assetDeletionPreviewDialog")
    assert assets.deletionPlan["can_apply"]
    assert assets.deletionPlan["affected_ids"] == [film_id]
    assert len(assets.deletionPlan["rows"]) == 1
    assert assets.deletionPlan["rows"][0]["id"] == film_id
    click_qml(window, "assetDeletionCancelButton")
    assert not assets.deletionPlan
    assert source_path.read_bytes() == old_source_bytes
    assert original_store.project_file.read_bytes() == json_bytes(before_manifest)

    # Re-preview, then confirm with a mouse event through the modal's real
    # button. The obsolete row remains diagnosable and must not be rewritten.
    click_qml(window, "assetLibraryDeleteButton")
    wait_asset_action(assets)
    assert assets.deletionPlan["can_apply"]
    confirm = find_item(window, "assetDeletionConfirmButton")
    assert confirm.isEnabled()
    click_qml(window, "assetDeletionConfirmButton")
    assert assets.deletionBusy
    wait_asset_action(assets)
    assert deletion_finished == [True], assets.deletionMessage

    current_manifest = json.loads(original_store.project_file.read_text(encoding="utf-8"))
    active = {row["id"]: row for row in current_manifest["documents"]}
    before_rows = {row["id"]: row for row in before_manifest["documents"]}
    assert set(active) == set(before_rows) - {film_id}, (
        assets.deletionMessage, sorted(active), sorted(before_rows))
    assert active[source_id] == before_rows[source_id]
    assert source_path.read_bytes() == old_source_bytes
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == active[source_id]["sha256"]
    original_film_path = original_store.root / before_rows[film_id]["path"]
    assert original_film_path.read_bytes() == film_bytes
    assert hashlib.sha256(original_film_path.read_bytes()).hexdigest() == hashlib.sha256(film_bytes).hexdigest()
    assert assets.transferCatalog["warnings"] and any(
        "aperture_kind" in warning for warning in assets.transferCatalog["warnings"])

    # Use the bridge's ordinary recovery flow; it must restore only the film,
    # while continuing to reject and preserve the unrelated old source.
    assert assets.loadAssetDeletions()
    assert assets.deletionBusy
    wait_asset_action(assets)
    receipt = next(row for row in assets.deletionRecords if film_id in row["affected_ids"])
    assert receipt["recoverable"]
    assert assets.restoreAssetDeletion(receipt["receipt_id"])
    assert assets.deletionBusy
    wait_asset_action(assets)

    restored = json.loads(original_store.project_file.read_text(encoding="utf-8"))
    restored_rows = {row["id"]: row for row in restored["documents"]}
    restored_film_row = restored_rows[film_id]
    assert restored_film_row["id"] == before_rows[film_id]["id"] == film_id
    assert restored_film_row["kind"] == before_rows[film_id]["kind"] == "film"
    assert restored_film_row["dependencies"] == before_rows[film_id]["dependencies"]
    restored_film_path = original_store.root / restored_film_row["path"]
    assert restored_film_path.is_file()
    assert json.loads(restored_film_path.read_text(encoding="utf-8")) == json.loads(film_bytes)
    assert original_film_path.read_bytes() == film_bytes  # Earlier generations are immutable, not unlinked.
    assert restored_rows[source_id] == before_rows[source_id]
    assert source_path.read_bytes() == old_source_bytes
    with pytest.raises(ProjectError, match="aperture_kind"):
        assets.service.store.resolve_document(source_id)


def test_real_asset_library_can_only_delete_unsupported_source_explicitly(desktop, tmp_path, monkeypatch):
    original_store, film_id, source_id, old_source_bytes, film_bytes = create_legacy_film_project(
        tmp_path / "unsupported-project")
    project = admit_project(desktop, original_store)
    _route_id, _route, (window, assets, *_rest) = open_library(desktop, project)

    catalog = assets.transferCatalog
    unavailable = next(row for row in catalog["unavailable_rows"] if row["id"] == source_id)
    assert unavailable["kind"] == "source"
    assert unavailable["unsupported"] is True and unavailable["read_only"] is True
    assert unavailable["dependencies"] == []
    assert source_id not in {row["id"] for row in catalog["rows"]}
    source_path = original_store.root / next(
        row["path"] for row in original_store.manifest["documents"] if row["id"] == source_id)
    assert source_path.read_bytes() == old_source_bytes
    source_hash = hashlib.sha256(old_source_bytes).hexdigest()
    assert source_hash == next(row["sha256"] for row in original_store.manifest["documents"]
                               if row["id"] == source_id)
    assert not assets.service.list_resource_deletions()

    # The unavailable source is present in the real QML source category, but
    # selecting its row opens diagnostics only, never an editor.
    window.setProperty("categoryKey", "source")
    QTest.qWait(30)
    click_qml(window, "globalResourceInformation_0")
    assert window.property("informationId") == source_id
    assert not window.property("materialPaneVisible")
    assert not assets.materialEditorActive
    assert not assets.openMaterialAsset(source_id)
    click_qml(window, "globalResourceSelect_0")
    assert window.property("selectedIds").toVariant() == [source_id]

    # The QML export control is visibly disabled for this unsupported row.
    # Separately, its bridge API rejects the same identity if called directly.
    export_button = find_item(window, "globalResourcesExportButton")
    assert export_button.isVisible() and not export_button.isEnabled()
    assert not assets.previewLibraryTransfer([source_id], "global")
    assert not assets.transferBusy
    assert "所选资源类型不属于当前窗口" in assets.transferStatus
    assert not assets.transferPlan

    # An explicit failed export attempt makes the page offer refresh before
    # another mutation. Refresh the same project library, then reselect.
    click_qml(window, "globalResourcesRefreshButton")
    settled(assets)
    assert not assets.libraryFailed
    window.setProperty("categoryKey", "source")
    window.setProperty("selectedIds", [source_id])

    before_manifest = deepcopy(original_store.manifest)
    before_rows = {row["id"]: row for row in before_manifest["documents"]}
    original_film_path = original_store.root / before_rows[film_id]["path"]
    assert original_film_path.read_bytes() == film_bytes
    apply_results: list[dict] = []
    apply_deletion = assets.service.apply_resource_deletion

    def capture_apply_result(plan_id):
        result = apply_deletion(plan_id)
        apply_results.append(result)
        return result

    monkeypatch.setattr(assets.service, "apply_resource_deletion", capture_apply_result)

    # Deletion remains an explicit, mouse-confirmed action. Cancelling its
    # preview changes neither the manifest, immutable files nor recovery list.
    click_qml(window, "assetLibraryDeleteButton")
    wait_asset_action(assets)
    assert visible(window, "assetDeletionPreviewDialog")
    plan = assets.deletionPlan
    assert plan["action"] == "delete_unsupported_asset"
    assert plan["unsupported"] is True and plan["can_apply"] is True
    assert plan["selected_ids"] == plan["affected_ids"] == [source_id]
    assert [row["id"] for row in plan["rows"]] == [source_id]
    assert "不受当前格式支持" in assets.deletionMessage
    click_qml(window, "assetDeletionCancelButton")
    assert not assets.deletionPlan
    assert original_store.project_file.read_bytes() == json_bytes(before_manifest)
    assert source_path.read_bytes() == old_source_bytes
    assert not assets.service.list_resource_deletions()

    # Confirm the explicit unsupported-delete action using the real modal
    # button. The unsupported file is not unlinked and no restore receipt is
    # created; only its active manifest membership disappears.
    click_qml(window, "assetLibraryDeleteButton")
    wait_asset_action(assets)
    assert assets.deletionPlan["action"] == "delete_unsupported_asset"
    click_qml(window, "assetDeletionConfirmButton")
    assert assets.deletionBusy
    wait_asset_action(assets)
    assert len(apply_results) == 1
    result = apply_results[0]
    assert result["action"] == "delete_unsupported_asset"
    assert result["affected_ids"] == [source_id]
    assert result["recoverable"] is False

    current_manifest = json.loads(original_store.project_file.read_text(encoding="utf-8"))
    active = {row["id"]: row for row in current_manifest["documents"]}
    assert set(active) == set(before_rows) - {source_id}
    assert active[film_id] == before_rows[film_id]
    assert source_path.is_file() and source_path.read_bytes() == old_source_bytes
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash
    assert original_film_path.read_bytes() == film_bytes
    assert "不作为当前资产恢复" in assets.deletionMessage
    assert not assets.service.list_resource_deletions()

    assert assets.loadAssetDeletions()
    assert assets.deletionBusy
    wait_asset_action(assets)
    assert assets.deletionRecords == []
