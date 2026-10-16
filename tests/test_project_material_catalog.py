"""Project material views read the global catalog without importing or mutating it."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from coat_sim.core.material_business import (
    ApplicationMaterialCatalog,
    MaterialLibrary,
    application_material_library,
)
from coat_sim.core.materials import default_material_profile
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_material_library import ProjectMaterialLibrary
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.film_design import new_design


pytestmark = pytest.mark.contract


def _global_profile(identity="global-film", n=2.0):
    profile = default_material_profile("Global film", id=identity)
    profile["roles"] = ["film"]
    profile["optical_constants"] = [{
        "id": "visible", "name": "Visible synthetic", "kind": "tabulated",
        "wavelength_unit": "nm", "wavelength_min": 400.0, "wavelength_max": 700.0,
        "wavelengths": [400.0, 700.0], "n": [n, n], "k": [0.0, 0.0],
        "source": "Synthetic test data",
    }]
    return profile


class StaticCatalog:
    def __init__(self, records):
        self.records = deepcopy(records)

    def catalog_records(self):
        return [{"record": deepcopy(record), "builtin": False, "overridden": False,
                 "readonly": True} for record in self.records]

    def read(self, identity):
        record = next(row for row in self.records if row["id"] == identity)
        return {"record": deepcopy(record), "builtin": False, "overridden": False,
                "readonly": True, "editable": True}


class BrokenCatalog:
    def catalog_records(self):
        raise ValueError("bad global JSON fixture")

    def read(self, identity):
        raise ValueError("bad global JSON fixture")


def test_default_global_view_is_32_read_only_records_without_copy_or_lock(tmp_path):
    missing_global = tmp_path / "application-data" / "materials"
    global_catalog = application_material_library()
    store = ProjectStore.create(tmp_path / "project", "Global catalog view")
    library = ProjectMaterialLibrary(ProjectResourceService(store), global_library=global_catalog)

    rows = library.catalog_records()
    assert len(rows) == 32
    assert all(row["origin"] == "global" and row["readonly"] for row in rows)
    assert store.manifest["documents"] == []
    selected = next(row["record"]["id"] for row in rows
                    if row["record"].get("optical_constants"))
    output = tmp_path / "global-material.cmat"
    library.export_single_file(selected, output)
    assert output.is_file()
    assert store.manifest["documents"] == []
    missing_store = ProjectStore.create(tmp_path / "missing-global-project", "Missing global folder")
    missing_library = ProjectMaterialLibrary(
        ProjectResourceService(missing_store), global_library=ApplicationMaterialCatalog(missing_global))
    assert missing_library.catalog_records()
    assert missing_store.manifest["documents"] == []
    assert not missing_global.exists()
    assert not list(tmp_path.rglob(".material-library.lock"))


def test_broken_global_catalog_preserves_local_material_records(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Local survives global read error")
    service = ProjectResourceService(store)
    local = ProjectMaterialLibrary(service, global_library=StaticCatalog([]))
    profile = default_material_profile("Owned", id="owned-profile")
    local.write(profile)

    degraded = ProjectMaterialLibrary(service, global_library=BrokenCatalog())
    rows = degraded.catalog_records()
    assert [(row["record"]["id"], row["origin"]) for row in rows] == [
        ("owned-profile", "project")]
    assert "bad global JSON fixture" in degraded.global_catalog_error
    assert degraded.read("owned-profile")["record"] == profile


@pytest.mark.qt
def test_malformed_application_catalog_keeps_local_material_and_does_not_lock_global(tmp_path):
    from PySide6.QtCore import QCoreApplication
    from coat_sim.ui.project_material_library import ProjectMaterialLibraryBridge

    app = QCoreApplication.instance() or QCoreApplication([])
    global_dir = tmp_path / "application-materials"
    global_dir.mkdir()
    (global_dir / "malformed.json").write_text("{ invalid", encoding="utf-8")
    global_library = ApplicationMaterialCatalog(global_dir)
    store = ProjectStore.create(tmp_path / "project", "Malformed global catalog")
    resource_service = ProjectResourceService(store)
    profile = default_material_profile("Owned remains", id="owned-despite-broken-global")
    ProjectMaterialLibrary(resource_service, global_library=StaticCatalog([])).write(profile)

    degraded = ProjectMaterialLibrary(resource_service, global_library=global_library)
    rows = degraded.catalog_records()
    assert [(row["record"]["id"], row["origin"]) for row in rows] == [
        (profile["id"], "project")]
    assert "无法读取材料文件" in degraded.global_catalog_error
    assert degraded.read(profile["id"])["record"] == profile

    bridge = ProjectMaterialLibraryBridge(resource_service, global_library=global_library)
    try:
        status = bridge.property("status")
        assert isinstance(status, str)
        assert "全局材料库读取失败" in status
        assert "项目材料仍可用" in status
        assert any(row["id"] == profile["id"] for row in bridge._entries)
        assert not (global_dir / ".material-library.lock").exists()
    finally:
        bridge.waitForBackgroundTasks()
        bridge.deleteLater()
        app.processEvents()


def test_global_builtin_export_and_edit_are_project_cow_only(tmp_path):
    global_dir = tmp_path / "global" / "materials"
    provider = ApplicationMaterialCatalog(global_dir)
    builtin = next(row["record"] for row in provider.catalog_records()
                   if row["builtin"] and row["record"].get("optical_constants"))
    original = provider.read(builtin["id"])["record"]
    store = ProjectStore.create(tmp_path / "project", "Copy on write")
    library = ProjectMaterialLibrary(ProjectResourceService(store), global_library=provider)

    with pytest.raises(ProjectError) as missing:
        library.delete(builtin["id"])
    assert missing.value.code == "missing_document"
    changed = deepcopy(builtin)
    changed["name"] += " project copy"
    result = library.write(changed)

    assert result["created"] is True
    effective = next(row for row in library.catalog_records()
                     if row["record"]["id"] == builtin["id"])
    assert effective["origin"] == "project" and not effective["readonly"]
    assert library.read(builtin["id"])["record"]["name"] == changed["name"]
    assert provider.read(builtin["id"])["record"] == original
    assert not global_dir.exists()
    kinds = {row["kind"] for row in store.manifest["documents"]}
    assert kinds == {"material_profile", "optical_material"}


def test_real_global_custom_and_override_remain_unchanged_after_reads_exports_and_cow(tmp_path):
    global_dir = tmp_path / "application-materials"
    writer = MaterialLibrary(directory=global_dir)
    custom = _global_profile("global-user-custom", n=2.2)
    writer.write(custom)
    app_catalog = ApplicationMaterialCatalog(global_dir)
    builtin = next(row["record"] for row in app_catalog.catalog_records()
                   if row["builtin"] and row["record"].get("optical_constants"))
    override = deepcopy(builtin)
    override["name"] += " global override"
    writer.save_builtin_override(override)
    lock = global_dir / ".material-library.lock"
    lock.unlink(missing_ok=True)

    def global_files():
        return {str(path.relative_to(global_dir)):
                (path.read_bytes(), path.stat().st_mtime_ns)
                for path in global_dir.rglob("*") if path.is_file()}

    before = global_files()
    store = ProjectStore.create(tmp_path / "project", "Global user and override COW")
    library = ProjectMaterialLibrary(
        ProjectResourceService(store), global_library=app_catalog)
    rows = library.catalog_records()
    assert any(row["record"]["id"] == custom["id"] and row["origin"] == "global"
               for row in rows)
    builtin_row = next(row for row in rows if row["record"]["id"] == builtin["id"])
    assert builtin_row["origin"] == "global" and builtin_row["builtin"] and builtin_row["readonly"]
    assert builtin_row["overridden"]
    library.export_single_file(custom["id"], tmp_path / "custom.cmat")
    library.export_single_file(builtin["id"], tmp_path / "override.cmat")

    custom_copy = deepcopy(custom)
    custom_copy["name"] += " project copy"
    library.write(custom_copy)
    builtin_copy = deepcopy(override)
    builtin_copy["name"] += " project copy"
    library.write(builtin_copy)

    assert global_files() == before
    assert not lock.exists()
    owned_ids = {row["record"]["id"] for row in library.catalog_records()
                 if row["origin"] == "project"}
    assert {custom["id"], builtin["id"]}.issubset(owned_ids)


def test_other_category_optical_records_need_explicit_picker_roles(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Other optical records")
    untyped_global = _global_profile("global-monitor-optics")
    untyped_global["category"] = "other"
    untyped_global["roles"] = []
    explicit_global = _global_profile("explicit-film-role")
    explicit_global["category"] = "other"
    explicit_global["roles"] = ["film"]
    provider = StaticCatalog([untyped_global, explicit_global])
    service = ProjectFilmService(store, global_material_library=provider)
    assert "global-monitor-optics" not in {
        row["id"] for purpose in ("film", "substrate", "medium")
        for row in service.material_options(purpose)}
    assert "explicit-film-role" in {row["id"] for row in service.material_options("film")}

    local = deepcopy(untyped_global)
    local["id"] = "local-monitor-optics"
    local.pop("roles", None)
    library = ProjectMaterialLibrary(
        ProjectResourceService(store), global_library=StaticCatalog([]))
    library.write(local)
    service.refresh_materials()
    assert "local-monitor-optics" not in {
        row["id"] for purpose in ("film", "substrate", "medium")
        for row in service.material_options(purpose)}


def test_global_film_capture_is_referenced_only_and_saved_closure_survives_bad_global(tmp_path):
    profile = _global_profile()
    provider = StaticCatalog([profile, _global_profile("unrelated-global-material", n=4.0)])
    store = ProjectStore.create(tmp_path / "project", "Capture explicit global dependency")
    service = ProjectFilmService(store, global_material_library=provider)
    design = new_design("Capture one global material")
    design["front_layers"] = [{
        "id": "layer-global", "material_id": profile["id"], "optical_set_id": "visible",
        "thickness": {"value": 80.0, "unit": "nm"}, "note": "",
    }]
    design["substrate"].update(material_id=profile["id"], optical_set_id="visible")
    saved = service.create_film(design)
    graph = store.resolve_document(saved.document_id)
    assert {row["id"] for row in graph["materials"]} == {profile["id"]}
    assert len([row for row in graph["documents"] if row["kind"] == "optical_material"]) == 1
    assert len([row for row in graph["documents"] if row["kind"] == "film"]) == 1

    empty_store = ProjectStore.create(tmp_path / "explicit-empty", "Do not infer an empty map")
    explicit_empty = ProjectFilmService(empty_store, global_material_library=provider)
    with pytest.raises(ProjectError) as error:
        explicit_empty.create_film(design, materials={})
    assert error.value.code == "missing_dependency"
    assert empty_store.manifest["documents"] == []

    provider.records[0]["optical_constants"][0]["n"] = [3.0, 3.0]
    service.refresh_materials()
    assert service.materials.get(profile["id"])["optical_sets"][0]["n"] == [2.0, 2.0]
    reopened = ProjectFilmService(store, global_material_library=BrokenCatalog())
    assert "bad global JSON fixture" in reopened.global_catalog_error
    assert reopened.load_document(saved.document_id).design == saved.design
    assert reopened.materials.get(profile["id"])["optical_sets"][0]["n"] == [2.0, 2.0]


def test_stale_global_first_cow_preserves_bridge_draft(tmp_path):
    from PySide6.QtCore import QCoreApplication
    from coat_sim.ui.project_material_library import ProjectMaterialLibraryBridge

    app = QCoreApplication.instance() or QCoreApplication([])
    profile = _global_profile()
    provider = StaticCatalog([profile])
    store = ProjectStore.create(tmp_path / "project", "Stale global copy")
    film_service = ProjectFilmService(store, global_material_library=provider)
    design = new_design("Capture global before edit")
    design["front_layers"] = [{
        "id": "layer-global", "material_id": profile["id"], "optical_set_id": "visible",
        "thickness": {"value": 80.0, "unit": "nm"}, "note": "",
    }]
    # The default draft is deliberately unbound. This calculation fixture needs
    # a concrete substrate dependency instead of relying on catalog order.
    design["substrate"].update(material_id=profile["id"], optical_set_id="visible")
    saved = film_service.create_film(design)
    resource_service = ProjectResourceService(store)
    bridge = ProjectMaterialLibraryBridge(resource_service, global_library=provider)
    try:
        key = bridge._global_material_keys[profile["id"]]
        assert bridge.selectMaterial(key)
        assert not bridge.loading
        assert bridge.currentId == profile["id"]
        bridge.setField("name", "User's first project copy")
        assert bridge.dirty

        optical_row = next(row for row in store.manifest["documents"]
                           if row["kind"] == "optical_material")
        optical = store.resolve_document(optical_row["id"])["documents"]
        envelope = next(row["data"] for row in optical if row["id"] == optical_row["id"])
        changed = deepcopy(envelope)
        changed["material"]["optical_sets"][0]["n"] = [3.0, 3.0]
        store.save_document(optical_row["id"], changed)
        before_failed_save = store.manifest

        assert not bridge.save()
        assert bridge.dirty
        assert bridge._draft["name"] == "User's first project copy"
        status = bridge.property("status")
        assert isinstance(status, str)
        assert "changed since it was loaded" in status
        assert "未保存的修改已保留" in status
        assert store.manifest == before_failed_save
        assert saved.document_id in {row["id"] for row in store.manifest["documents"]}
        assert not any(row["kind"] == "material_profile" for row in store.manifest["documents"])
    finally:
        bridge.waitForBackgroundTasks()
        bridge.deleteLater()
        app.processEvents()


def test_saved_film_global_snapshot_can_be_promoted_without_rewriting_frozen_values(tmp_path):
    profile = _global_profile()
    provider = StaticCatalog([profile])
    store = ProjectStore.create(tmp_path / "project", "Promote captured film material")
    film_service = ProjectFilmService(store, global_material_library=provider)
    design = new_design("Capture, then edit global material")
    design["front_layers"] = [{
        "id": "layer-global", "material_id": profile["id"], "optical_set_id": "visible",
        "thickness": {"value": 80.0, "unit": "nm"}, "note": "",
    }]
    # New project drafts stay unbound; this persisted fixture explicitly uses
    # the same material as its substrate so the whole dependency can be frozen.
    design["substrate"].update(material_id=profile["id"], optical_set_id="visible")
    saved = film_service.create_film(design)
    frozen_before_edit = film_service.freeze_design(saved.design)
    library = ProjectMaterialLibrary(ProjectResourceService(store), global_library=provider)
    expected = library.revisions(profile["id"])
    changed = deepcopy(profile)
    changed["name"] = "Project-owned version"
    changed["optical_constants"][0]["n"] = [2.6, 2.6]
    library.write(changed, expected_revisions=expected)

    assert provider.read(profile["id"])["record"] == profile
    assert any(row["kind"] == "material_profile" for row in store.manifest["documents"])
    frozen_after_edit = film_service.freeze_design(saved.design)
    assert frozen_before_edit["materials"][0]["optical_sets"][0]["n"] == [2.0, 2.0]
    assert frozen_after_edit["materials"][0]["optical_sets"][0]["n"] == [2.0, 2.0]

    film_service.refresh_materials()
    assert film_service.materials.get(profile["id"])["optical_sets"][0]["n"] == [2.6, 2.6]
    assert frozen_after_edit["materials"][0]["optical_sets"][0]["n"] == [2.0, 2.0]


@pytest.mark.qt
def test_material_save_refreshes_only_bound_film_picker_and_stale_draft_cannot_save(tmp_path):
    from PySide6.QtCore import QCoreApplication
    from coat_sim.core.project_film_session import ProjectFilmWorkspace
    from coat_sim.project_session import ProjectSessionHost
    from coat_sim.ui.project_material_library import ProjectMaterialLibraryBridge
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController

    class MemorySettings:
        def value(self, _key, default=None):
            return default

        def setValue(self, _key, _value):
            pass

        def sync(self):
            pass

    app = QCoreApplication.instance() or QCoreApplication([])
    profile = _global_profile()
    provider = StaticCatalog([profile])
    stores = {name: ProjectStore.create(tmp_path / name, name) for name in ("first", "second")}
    host = ProjectSessionHost(lambda root: next(
        store for store in stores.values() if store.root == root.resolve()))
    bindings = {name: host.open_project(store.root).binding for name, store in stores.items()}
    first = ProjectFilmWorkspace.for_project(host, bindings["first"])
    second = ProjectFilmWorkspace.for_project(host, bindings["second"])
    for workspace in (first, second):
        workspace.service.global_material_library = provider
        workspace.service.refresh_materials()

    design = new_design("Live picker snapshot test")
    design["front_layers"] = [{
        "id": "layer-global", "material_id": profile["id"], "optical_set_id": "visible",
        "thickness": {"value": 80.0, "unit": "nm"}, "note": "",
    }]
    design["substrate"].update(material_id=profile["id"], optical_set_id="visible")
    saved = first.service.create_film(design)
    first_editor = first.editor
    second_editor = second.editor
    assert first_editor.load_saved_design(saved.document_id)
    assert first_editor.set_design_value("note", "keep this dirty film draft")
    first_before = first_editor.snapshot()
    second_before = second_editor.snapshot()
    controller = FilmDesignQuickController(
        service=first.service, edit_session=first_editor,
        geometry_settings=MemorySettings(), project_context=first)

    resource_service = ProjectResourceService(
        stores["first"], session_host=host, binding=bindings["first"])
    bridge = ProjectMaterialLibraryBridge(
        resource_service, global_library=provider)
    try:
        assert bridge.selectMaterial(bridge._global_material_keys[profile["id"]])
        bridge.waitForBackgroundTasks()
        app.processEvents()
        bridge.setField("name", "Edited project-owned profile")
        optical = bridge.opticalSetDetails("visible")
        table = "\n".join(
            f"{wavelength}\t2.6\t{k}"
            for wavelength, k in zip(optical["wavelengths"], optical["k"], strict=True))
        form = {
            "id": "visible", "name": optical["name"], "notes": optical["notes"],
            "source": optical["source"], "kind": "tabulated",
            "wavelength_unit": optical["wavelength_unit"],
            "wavelength_min": optical["wavelength_min"],
            "wavelength_max": optical["wavelength_max"], "table": table,
        }
        assert bridge.updateOpticalSet("visible", json.dumps(form))
        assert bridge.dirty
        assert bridge.save(), bridge.status

        first_after_refresh = first_editor.snapshot()
        assert first_after_refresh.design == first_before.design
        assert first_after_refresh.dirty
        assert first_after_refresh.design_revision == first_before.design_revision
        assert first.service.materials.get(profile["id"])["optical_sets"][0]["n"] == [2.0, 2.0]
        first_row = next(row for row in first_after_refresh.material_options["film"]
                         if row["id"] == profile["id"])
        assert first_row["name"] == "Edited project-owned profile"
        assert "2.6" in first_row["optical_sets"][0]["value_range_text"]
        app.processEvents()
        film_options = controller.property("filmOptions")
        assert isinstance(film_options, list)
        quick_row = next(row for row in film_options if row["id"] == profile["id"])
        assert quick_row["name"] == "Edited project-owned profile"

        second_after = second_editor.snapshot()
        assert second_after.material_options == second_before.material_options
        second_row = next(row for row in second_after.material_options["film"]
                          if row["id"] == profile["id"])
        assert second_row["name"] == profile["name"]
        assert second.service.materials.get(profile["id"])["optical_sets"][0]["n"] == [2.0, 2.0]

        assert not first_editor.save_current()
        stale = first_editor.snapshot()
        assert stale.dirty and stale.design["note"] == "keep this dirty film draft"
        assert "changed after this film draft captured it" in stale.error

        assert bridge.newMaterial()
        bridge.setField("name", "Project-only film profile")
        bridge.setCategory("film")
        assert bridge.addOpticalSet(json.dumps({
            "name": "Project-only visible", "notes": "", "source": "synthetic",
            "kind": "tabulated", "wavelength_unit": "nm",
            "wavelength_min": 400.0, "wavelength_max": 700.0,
            "table": "400\t2.9\t0\n700\t2.9\t0",
        }))
        new_id = bridge.draftData()["id"]
        assert bridge.save(), bridge.status
        app.processEvents()
        film_options = controller.property("filmOptions")
        assert isinstance(film_options, list)
        assert new_id in {row["id"] for row in film_options}
        assert new_id not in {row["id"] for row in second_editor.snapshot().material_options["film"]}
        assert second_editor.snapshot().revision == second_before.revision
    finally:
        bridge.waitForBackgroundTasks()
        bridge.deleteLater()
        controller.markSourceClosed()
        controller.deleteLater()
        app.processEvents()


@pytest.mark.qt
def test_broken_global_catalog_keeps_saved_film_usable_and_warns_controller(tmp_path):
    from PySide6.QtCore import QCoreApplication
    from coat_sim.core.project_film_session import ProjectFilmWorkspace
    from coat_sim.project_session import ProjectSessionHost
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController

    class MemorySettings:
        def value(self, _key, default=None):
            return default

        def setValue(self, _key, _value):
            pass

        def sync(self):
            pass

    app = QCoreApplication.instance() or QCoreApplication([])
    profile = _global_profile()
    store = ProjectStore.create(tmp_path / "project", "Broken global catalog")
    writer = ProjectFilmService(store, global_material_library=StaticCatalog([profile]))
    design = new_design("Saved project film remains usable")
    design["front_layers"] = [{
        "id": "layer-global", "material_id": profile["id"], "optical_set_id": "visible",
        "thickness": {"value": 80.0, "unit": "nm"}, "note": "",
    }]
    design["substrate"].update(material_id=profile["id"], optical_set_id="visible")
    saved = writer.create_film(design)

    host = ProjectSessionHost(lambda _root: store)
    binding = host.open_project(store.root).binding
    workspace = ProjectFilmWorkspace.for_project(host, binding)
    global_dir = tmp_path / "application-materials"
    global_dir.mkdir()
    (global_dir / "malformed.json").write_text("{ invalid", encoding="utf-8")
    workspace.service.global_material_library = ApplicationMaterialCatalog(global_dir)
    workspace.service.refresh_materials()
    editor = workspace.editor
    assert editor.load_saved_design(saved.document_id)
    frozen = workspace.service.freeze_design(saved.design)
    assert frozen["materials"][0]["id"] == profile["id"]
    assert "无法读取材料文件" in workspace.service.global_catalog_error

    controller = FilmDesignQuickController(
        service=workspace.service, edit_session=editor,
        geometry_settings=MemorySettings(), project_context=workspace)
    try:
        assert controller.initializationReady
        state = controller.property("uiState")
        film_options = controller.property("filmOptions")
        assert isinstance(state, dict) and isinstance(film_options, list)
        assert "无法读取材料文件" in state["catalogWarning"]
        assert any(row["id"] == profile["id"] for row in film_options)
    finally:
        controller.markSourceClosed()
        controller.deleteLater()
        app.processEvents()
