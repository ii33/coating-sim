"""Committed scope-local writes refresh catalog tokens without adopting drafts."""
from __future__ import annotations

from copy import deepcopy
from threading import Event

import pytest
from PySide6.QtCore import QObject, QCoreApplication
from PySide6.QtTest import QTest

from coat_sim.core.material_business import MaterialLibrary
from coat_sim.ui.project_resources import capture_project_resources_snapshot
from tests.test_asset_library_actions import global_assets as global_assets, action_settled
from tests.test_launcher_information_global_resources import global_root as global_root
from tests.test_project_global_resources import profile
from tests.test_project_last_window_close import desktop as desktop, spin_until
from tests.test_scoped_resource_libraries import open_library, qml, settled
from tests.test_unified_asset_materials import seed_project_material

pytestmark = [pytest.mark.qt, pytest.mark.contract]


def refresh_settled(assets):
    QCoreApplication.processEvents()
    spin_until(lambda: bool(assets.transferBusy or assets.catalogRefreshPending), lambda value: not value,
               "committed catalog refresh")
    QTest.qWait(20)


def source(name="Created"):
    return {"schema_version": 2, "kind": "point", "device_type": "custom", "source_exponent": 1., "name": name}


def test_background_global_save_refreshes_catalog_before_recycle_without_manual_refresh(global_assets, global_root, monkeypatch):
    window, assets, _engine = global_assets
    MaterialLibrary(directory=global_root / "data/materials").write(profile())
    assert assets.browseLibrary()
    settled(assets)
    identity = next(row["id"] for row in assets.transferCatalog["rows"] if row["kind"] == "material_profile")
    assert assets.openMaterialAsset(identity)
    library = assets.materialLibrary
    spin_until(lambda: library.loading, lambda value: not value, "material loaded")
    previous = assets.transferCatalog["catalog_id"]
    library.setField("name", "Background renamed material")
    assert library.saveInBackground()
    spin_until(lambda: library.loading, lambda value: not value, "background save")
    refresh_settled(assets)
    assert assets.transferCatalog["catalog_id"] != previous
    assert next(row for row in assets.transferCatalog["rows"] if row["id"] == identity)["name"] == "Background renamed material"
    assert assets.previewAssetDeletion([identity])
    action_settled(assets)
    assert assets.deletionPlan["can_apply"], assets.deletionPlan
    assert assets.materialLibrary is library and library.currentId == "glass"
    assets.cancelAssetDeletion()  # Reading fresh preview never deletes anything.
    assert MaterialLibrary(directory=global_root / "data/materials").read("glass")["record"]["name"] == "Background renamed material"
    assert window.property("operationBusy") is False
    # A failed background write must not claim a committed change or replace
    # the draft/catalog while reporting the error.
    previous = assets.transferCatalog["catalog_id"]
    library.setField("notes", "Keep the failed-save input")
    unsaved = deepcopy(library.draftData())

    def fail_save(*_args):
        raise OSError("injected write failure")

    monkeypatch.setattr("coat_sim.ui.source_materials._save_library_record", fail_save)
    assert library.saveInBackground()
    spin_until(lambda: library.loading, lambda value: not value, "failed background save")
    refresh_settled(assets)
    assert assets.transferCatalog["catalog_id"] == previous
    assert library.dirty and library.draftData() == unsaved
    assert "failed" in library.status.lower()


def test_clean_global_save_and_material_import_also_refresh_without_dirty_edge(global_assets, global_root, tmp_path):
    _window, assets, _engine = global_assets
    assert assets.newMaterialAsset()
    library = assets.materialLibrary
    spin_until(lambda: library.loading, lambda value: not value, "empty editor catalog")
    assert not library.dirty
    previous = assets.transferCatalog["catalog_id"]
    assert library.saveInBackground()
    spin_until(lambda: library.loading, lambda value: not value, "clean background save")
    refresh_settled(assets)
    assert assets.transferCatalog["catalog_id"] != previous
    assert any(row.get("material_id") == library.currentId for row in assets.transferCatalog["rows"])
    package_library = MaterialLibrary(directory=tmp_path / "package-materials")
    package_library.write(profile("imported"))
    package = tmp_path / "materials.json"
    package_library.export_single_file("imported", package)
    previous = assets.transferCatalog["catalog_id"]
    assert library.previewImport(str(package))
    spin_until(lambda: library.loading, lambda value: not value, "import preview")
    assert library.applyImport({})
    spin_until(lambda: library.loading, lambda value: not value, "import publication")
    refresh_settled(assets)
    assert assets.transferCatalog["catalog_id"] != previous
    assert any(row.get("material_id") == "imported" for row in assets.transferCatalog["rows"])


def test_project_store_changes_coalesce_and_do_not_refresh_another_project(desktop, monkeypatch):
    first, second = desktop.open("First"), desktop.open("Second")
    _a, _route, (_window, assets, *_rest) = open_library(desktop, first)
    _b, _other_route, (_other_window, other, *_rest) = open_library(desktop, second)
    before_other = deepcopy(other.transferCatalog)
    inspect = assets.service.inspect_resources
    calls = []

    def inspecting(source):
        calls.append(source)
        return inspect(source)

    monkeypatch.setattr(assets.service, "inspect_resources", inspecting)
    store = assets.service.store
    one = store.create_document("source", source("First new asset"))
    two = store.create_document("source", source("Second new asset"))
    QCoreApplication.processEvents()
    assert assets.catalogRefreshPending
    assert not assets.previewAssetDeletion([one]) and "目录正在更新" in assets.deletionMessage
    refresh_settled(assets)
    assert calls == ["project"]
    assert {one, two}.issubset({row["id"] for row in assets.transferCatalog["rows"]})
    assert other.transferCatalog == before_other


def test_commit_during_initial_read_is_not_lost_and_old_token_is_replaced(desktop, monkeypatch):
    project = desktop.open()
    _a, _route, (_window, assets, *_rest) = open_library(desktop, project)
    inspect = assets.service.inspect_resources
    captured, release = Event(), Event()
    calls = []

    def delayed(source):
        result = inspect(source)
        calls.append(result["catalog_id"])
        if len(calls) == 1:
            captured.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(assets.service, "inspect_resources", delayed)
    try:
        assert assets.browseLibrary()
        spin_until(captured.is_set, bool, "in-flight initial catalog")
        identity = assets.service.store.create_document("source", source("While reading"))
        QCoreApplication.processEvents()
        assert assets.catalogRefreshPending and assets.transferBusy
        release.set()
        refresh_settled(assets)
        assert len(calls) == 2 and assets.transferCatalog["catalog_id"] == calls[-1]
        assert any(row["id"] == identity for row in assets.transferCatalog["rows"])
    finally:
        release.set()


def test_refresh_preserves_later_material_selection_and_exact_dirty_input(desktop):
    project = desktop.open()
    first = seed_project_material(desktop, project, "first")
    seed_project_material(desktop, project, "second")
    _a, _route, (window, assets, *_rest) = open_library(desktop, project)
    second = next(row for row in assets.transferCatalog["rows"]
                  if row["kind"] == "material_profile" and row.get("material_id") == "second")
    assert assets.openMaterialAsset(first["id"])
    library = assets.materialLibrary
    library.setField("name", "Saved first material")
    assert library.save()
    assert assets.openMaterialAsset(second["id"])
    assert library.currentId == "second"
    library.setField("notes", "Keep exact newer second input")
    dirty = deepcopy(library.draftData())
    refresh_settled(assets)
    assert assets.materialLibrary is library and library.currentId == "second"
    assert library.dirty and library.draftData() == dirty
    assert window.property("informationId") == second["id"]
    assert next(row for row in assets.transferCatalog["rows"] if row["id"] == first["id"])["name"] == "Saved first material"


def test_external_import_source_wins_over_local_refresh_and_close_cancels_callbacks(desktop, global_root):
    project = desktop.open()
    _a, _route, (window, assets, *_rest) = open_library(desktop, project)
    assert assets.browseSource("global")
    settled(assets)
    before = deepcopy(assets.transferCatalog)
    assets.service.store.create_document("source", source("Owned change"))
    QCoreApplication.processEvents()
    refresh_settled(assets)
    assert assets.transferCatalog == before and assets.transferCatalog["source_type"] == "global"
    assets.service.store.create_document("source", source("Queued before disposal"))
    assets.dispose()  # Its queued notification has not run yet.
    window.hide()
    assets.service.store.create_document("source", source("After view disposal"))
    QTest.qWait(35)
    assert not window.isVisible() and not assets.service.store._change_observers
    assert assets._disposed and not assets.catalogRefreshPending and not assets.transferBusy
    assert not assets._catalog_refresh_timer.isActive()
    window.setProperty("projectCloseAuthorized", True)


@pytest.mark.parametrize("operation", ["deletion", "transfer"])
def test_commit_invalidates_in_flight_preview_without_applying_it(desktop, monkeypatch, tmp_path, operation):
    project = desktop.open()
    store = desktop.adapter.host.store_for(project.session.binding)
    identity = store.create_document("source", source("Selected original"))
    _a, _route, (_window, assets, *_rest) = open_library(desktop, project)
    captured, release = Event(), Event()
    method = "preview_resource_deletion" if operation == "deletion" else "preview_resource_transfer"
    original = getattr(assets.service, method)

    def delayed(*args):
        result = original(*args)
        captured.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(assets.service, method, delayed)
    try:
        if operation == "deletion":
            assert assets.previewAssetDeletion([identity])
        else:
            assert assets.previewLibraryTransfer([identity], str(tmp_path / "not-published.cproject"))
        spin_until(captured.is_set, bool, "in-flight preview")
        created = store.create_document("source", source("Commit during preview"))
        QCoreApplication.processEvents()
        assert assets.catalogRefreshPending
        assert not assets.confirmAssetDeletion()
        release.set()
        spin_until(lambda: assets.deletionBusy, lambda value: not value, "cancelled deletion preview")
        refresh_settled(assets)
        assert not assets.deletionPlan and not assets.transferPlan
        assert {identity, created}.issubset({row["id"] for row in assets.transferCatalog["rows"]})
        assert {identity, created}.issubset({row["id"] for row in store.manifest["documents"]})
        assert not (tmp_path / "not-published.cproject").exists()
    finally:
        release.set()


def test_catalog_swap_keeps_open_optical_form_and_unconfirmed_text(desktop):
    project = desktop.open()
    row = seed_project_material(desktop, project)
    _a, _route, (window, assets, engine, *_rest) = open_library(desktop, project)
    assert assets.openMaterialAsset(row["id"])
    QTest.qWait(60)
    editor = window.property("editorInstance")
    assert editor is not None and editor.property("visible")
    qml(engine, editor, "openSetEditor('set')")
    form = editor.property("setEditorInstance")
    assert form is not None and form.property("visible")
    name = form.findChild(QObject, "opticalEditorName")
    assert name is not None
    name.setProperty("text", "Exact unconfirmed dataset name")
    original_draft = deepcopy(assets.materialLibrary.draftData())
    old = assets.transferCatalog["catalog_id"]
    assets.service.store.create_document("source", source("Sibling saved while editing data"))
    refresh_settled(assets)
    assert assets.transferCatalog["catalog_id"] != old
    assert editor is window.property("editorInstance") and editor.property("visible")
    assert form is editor.property("setEditorInstance") and form.property("visible")
    assert name.property("text") == "Exact unconfirmed dataset name"
    assert assets.materialLibrary.draftData() == original_draft
    qml(engine, form, "close()")


def test_failed_catalog_refresh_keeps_display_but_cannot_preview_stale_deletion(desktop, monkeypatch):
    project = desktop.open()
    store = desktop.adapter.host.store_for(project.session.binding)
    identity = store.create_document("source", source("Selected"))
    _a, _route, (_window, assets, *_rest) = open_library(desktop, project)
    inspect = assets.service.inspect_resources
    previous = assets.transferCatalog["catalog_id"]

    def fail_read(_source):
        raise OSError("injected catalog read failure")

    monkeypatch.setattr(assets.service, "inspect_resources", fail_read)
    store.create_document("source", source("Committed change"))
    refresh_settled(assets)
    assert assets.libraryFailed and assets.transferCatalog["catalog_id"] == previous
    assert capture_project_resources_snapshot(assets, None, "project_resources") == (None, "catalog_refresh_failed")
    assert not assets.previewAssetDeletion([identity])
    assert "重试刷新" in assets.deletionMessage and not assets.deletionPlan
    monkeypatch.setattr(assets.service, "inspect_resources", inspect)
    assert assets.browseLibrary()
    refresh_settled(assets)
    assert not assets.libraryFailed and assets.transferCatalog["catalog_id"] != previous
    assert assets.previewAssetDeletion([identity])
    action_settled(assets)
    assert assets.deletionPlan["can_apply"]
    assets.cancelAssetDeletion()
