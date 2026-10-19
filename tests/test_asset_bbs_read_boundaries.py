"""Discussion reads and asset presentation keep their actual dependency scope."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject

from coat_sim.core.film_design import new_design
from coat_sim.core.project_bbs import ProjectBbsService
from coat_sim.core.project_bbs_attachments import import_attachment, read_attachment
from coat_sim.core.project_dependencies import ProjectError, json_bytes
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore
from tests.test_asset_catalog_diagnostics import obsolete_source
from tests.design_test_fixtures import _RECTANGULAR_SOURCE
from tests.test_project_global_resources import optical_material
from tests.test_project_last_window_close import desktop as desktop
from tests.test_scoped_resource_libraries import open_library, qml

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def damage_source(store, identity):
    manifest = store.manifest
    row = next(row for row in manifest["documents"] if row["id"] == identity)
    path = store.root / row["path"]
    path.write_bytes(json_bytes(obsolete_source()))
    row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    store.project_file.write_bytes(json_bytes(manifest))
    return ProjectStore.open(store.project_file), path


def hashes(store):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in store.root.rglob("*") if path.is_file()}


def test_bbs_reads_and_attachment_resolve_with_unrelated_invalid_source(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    bad = store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    source = tmp_path / "note.txt"
    source.write_text("User attachment", encoding="utf-8")
    attachment = import_attachment(store, source)
    service = ProjectBbsService(store)
    post = service.create_post("journal", "Entry", "Original", attachments=[attachment])
    reply = service.reply(post["post_id"], "Reply", "Reply body", expected_post_revision=1)
    service.update_post(post["post_id"], "Entry", "Revised", expected_post_revision=1)
    store, bad_path = damage_source(store, bad)
    before = hashes(store)
    service = ProjectBbsService(store)
    assert service.list_posts()["total"] == 1
    assert service.read_post(post["post_id"])["body"] == "Revised"
    assert service.list_posts(parent_id=post["post_id"])["posts"][0]["post_id"] == reply["post_id"]
    assert service.history(post["post_id"])["total"] == 2
    assert read_attachment(store, attachment)[1] == b"User attachment"
    untouched = {row["id"]: deepcopy(row) for row in store.manifest["documents"]
                 if row["kind"] not in {"project_bbs_post", "binary_asset"}}
    created = service.create_post("journal", "New", "Independent BBS publication")
    second_reply = service.reply(created["post_id"], "Reply", "Independent reply", expected_post_revision=1)
    other = tmp_path / "new.txt"
    other.write_text("New attachment after the design became invalid", encoding="utf-8")
    newly_attached = import_attachment(store, other)
    service.update_post(created["post_id"], "New", "Attached", expected_post_revision=1,
                        attachments=[newly_attached])
    assert service.read_post(created["post_id"])["revision"] == 2
    assert service.read_post(second_reply["post_id"])["parent_id"] == created["post_id"]
    assert read_attachment(store, newly_attached)[1] == other.read_bytes()
    assert {row["id"]: row for row in store.manifest["documents"] if row["id"] in untouched} == untouched
    assert bad_path.exists()
    # Every previous immutable document remains present and byte-identical.
    assert all(Path(path).read_bytes() and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
               for path, digest in before.items() if Path(path) != store.project_file)
    with pytest.raises(ProjectError, match="aperture_kind"):
        store.resolve_document(bad)


@pytest.mark.parametrize("damaged", ["post", "attachment"])
def test_bbs_own_corruption_remains_a_visible_failure(tmp_path, damaged):
    store = ProjectStore.create(tmp_path / "project", "Project")
    source = tmp_path / "note.txt"
    source.write_text("User attachment", encoding="utf-8")
    attachment = import_attachment(store, source)
    post = ProjectBbsService(store).create_post("journal", "Entry", "Body", attachments=[attachment])
    identity = post["post_id"] if damaged == "post" else attachment["asset_id"]
    row = next(row for row in store.manifest["documents"] if row["id"] == identity)
    path = store.root / row["path"]
    path.write_bytes(b"{}")
    before = hashes(store)
    for read in (lambda: ProjectBbsService(store).list_posts(),
                 lambda: ProjectBbsService(store).read_post(post["post_id"]),
                 lambda: ProjectBbsService(store).history(post["post_id"])):
        with pytest.raises(ProjectError) as error:
            read()
        assert error.value.code == "integrity_error" and str(path) in str(error.value)
    assert hashes(store) == before


def test_empty_bbs_does_not_read_independent_designs_or_orphan_attachments(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    bad = store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    source = tmp_path / "unposted.txt"
    source.write_text("Unposted attachment", encoding="utf-8")
    attachment = import_attachment(store, source)
    store, _ = damage_source(store, bad)
    before = hashes(store)
    assert ProjectBbsService(store).list_posts()["posts"] == []
    catalog = ProjectResourceService(store).inspect_resources()
    row = next(row for row in catalog["rows"] if row["id"] == attachment["asset_id"])
    assert row["asset_role"] == "bbs_attachment" and row["referenced_by"] == []
    assert row["asset_name"] == "unposted.txt" and "BBS" in row["summary"]
    assert len(catalog["warnings"]) == 1 and "aperture_kind" in catalog["warnings"][0]
    assert hashes(store) == before


def test_asset_window_shows_frozen_optics_and_attachment_semantics(desktop, tmp_path):
    project = desktop.open()
    store = desktop.adapter.host.store_for(project.session.binding)
    optics = store.create_document("optical_material", optical_material("frozen-only"))
    design = new_design("Uses frozen material")
    design["front_layers"] = [{"id": "layer", "material_id": "frozen-only", "optical_set_id": "set",
                                "thickness": {"value": 100, "unit": "nm"}, "note": ""}]
    store.create_document("film", design)
    source = tmp_path / "unposted.txt"
    source.write_text("User attachment", encoding="utf-8")
    attachment = import_attachment(store, source)
    _instance, _route, (window, bridge, engine, *_rest) = open_library(desktop, project)
    assert qml(engine, window, "categoryKey") == "material_profile"
    assert qml(engine, window, "visibleRows.map(row => row.id)") == [optics]
    categories = qml(engine, window, "categories")
    assert {"key": "material_profile", "label": "材料"} in categories
    assert all(row["key"] != "optical_material" for row in categories)
    assert {"key": "binary_asset", "label": "依赖文件与附件"} in categories
    qml(engine, window, "selectCategory('binary_asset')")
    assert qml(engine, window, "visibleRows.map(row => row.id)") == [attachment["asset_id"]]
    qml(engine, window, f"inspectAsset('{attachment['asset_id']}')")
    assert not bridge.materialEditorActive
    assert window.property("informationId") == attachment["asset_id"]
    metadata = cast(Any, window.findChild(QObject, "assetBinaryMetadata"))
    assert metadata is not None and "BBS 附件" in metadata.property("text")
    assert "已保存引用：0" in metadata.property("text")
    assert attachment["sha256"] in metadata.property("text")
    # An attachment cannot be published into the global design library.
    with pytest.raises(ProjectError, match="BBS") as error:
        bridge.service.preview_resource_transfer(bridge.transferCatalog["catalog_id"],
                                                 [attachment["asset_id"]], "global")
    assert error.value.code == "unsupported_transfer"


def test_saved_bbs_catalog_and_attachment_package_roundtrip(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    source = tmp_path / "report.txt"
    source.write_text("Attached report", encoding="utf-8")
    attachment = import_attachment(store, source)
    post = ProjectBbsService(store).create_post("journal", "Entry", "Body", attachments=[attachment])
    api = ProjectResourceService(store)
    catalog = api.inspect_resources()
    assert not catalog["warnings"]
    row = next(row for row in catalog["rows"] if row["id"] == attachment["asset_id"])
    assert row["referenced_by"] == [post["post_id"]]
    record = next(row for row in catalog["rows"] if row["id"] == post["post_id"])
    assert "BBS" in record["summary"] and "Body" not in record["summary"]
    package = tmp_path / "attachment.cproject"
    plan = api.preview_resource_transfer(catalog["catalog_id"], [attachment["asset_id"]], package)
    api.apply_resource_transfer(plan["plan_id"])
    receiver = ProjectStore.create(tmp_path / "receiver", "Receiver")
    target = ProjectResourceService(receiver)
    imported = target.inspect_resources(package)
    assert len(imported["rows"]) == 1 and imported["rows"][0]["asset_role"] == "bbs_attachment"
    plan = target.preview_resource_transfer(imported["catalog_id"], [imported["rows"][0]["id"]])
    target.apply_resource_transfer(plan["plan_id"])
    row = next(row for row in receiver.manifest["documents"] if row["kind"] == "binary_asset")
    descriptor = dict(attachment, asset_id=row["id"])
    assert read_attachment(receiver, descriptor)[1] == b"Attached report"
    api.close_resource_exchange()
    target.close_resource_exchange()


def test_bbs_scope_rejects_design_publication_and_preserves_default(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    records, data = store._read_all()
    before = hashes(store)
    with pytest.raises(ProjectError) as error:
        store._publish(records, data, discussion_only=True)
    assert error.value.code == "bbs_invalid_arguments" and hashes(store) == before
    assert store.manifest["default_configuration_id"] is None


def test_bbs_publication_never_parses_unrelated_invalid_source(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "Project")
    bad = store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    store, _ = damage_source(store, bad)
    original = store._read_record
    def scoped_read(row):
        assert row["kind"] != "source", "A BBS operation parsed an unrelated source"
        return original(row)
    monkeypatch.setattr(store, "_read_record", scoped_read)
    attachment_source = tmp_path / "independent.txt"
    attachment_source.write_text("Independent attachment", encoding="utf-8")
    attachment = import_attachment(store, attachment_source)
    service = ProjectBbsService(store)
    post = service.create_post("journal", "Entry", "Body", attachments=[attachment])
    reply = service.reply(post["post_id"], "Reply", "Body", expected_post_revision=1)
    service.update_post(post["post_id"], "Entry", "Revised", expected_post_revision=1)
    assert service.history(post["post_id"])["total"] == 2
    assert service.list_posts(parent_id=post["post_id"])["posts"][0]["post_id"] == reply["post_id"]
    assert read_attachment(store, attachment)[1] == b"Independent attachment"


def test_saved_design_roles_project_frozen_materials_without_guessing_names(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    ids = {name: store.create_document("optical_material", optical_material(name))
           for name in ("layer-a", "layer-b", "plate", "air", "name-says-film-but-unknown")}
    design = new_design("Explicit saved roles")
    design["front_layers"] = [
        {"id": "layer1", "material_id": "layer-a", "optical_set_id": "set",
         "thickness": {"value": 100, "unit": "nm"}, "note": ""},
        {"id": "layer2", "material_id": "layer-b", "optical_set_id": "set",
         "thickness": {"value": 100, "unit": "nm"}, "note": ""},
    ]
    design["substrate"].update(material_id="plate", optical_set_id="set")
    design["incident_medium"] = {"kind": "material", "material_id": "air", "optical_set_id": "set"}
    store.create_document("film", design)
    before = hashes(store)
    rows = {row["material_id"]: row for row in ProjectResourceService(store).inspect_resources()["rows"]
            if row["kind"] == "optical_material"}
    assert rows["layer-a"]["material_categories"] == ["film"]
    assert rows["layer-b"]["material_categories"] == ["film"]
    assert rows["plate"]["material_categories"] == ["substrate"]
    assert rows["air"]["material_uses"] == ["medium"] and rows["air"]["material_categories"] == []
    unknown = rows["name-says-film-but-unknown"]
    assert unknown["material_uses"] == [] and unknown["material_categories"] == []
    assert unknown["material_classification_source"] == "unknown"
    assert len(ids) == 5 and hashes(store) == before


def test_material_editor_projection_preserves_substrate_and_medium_roles(desktop):
    from tests.test_unified_asset_materials import wait_for_material_idle
    project = desktop.open()
    store = desktop.adapter.host.store_for(project.session.binding)
    plate = store.create_document("optical_material", optical_material("plate"))
    air = store.create_document("optical_material", optical_material("air"))
    design = new_design("Explicit substrate and medium")
    design["substrate"].update(material_id="plate", optical_set_id="set")
    design["incident_medium"] = {"kind": "material", "material_id": "air", "optical_set_id": "set"}
    store.create_document("film", design)
    before = deepcopy(store.manifest)
    _id, _route, (window, bridge, engine, *_rest) = open_library(desktop, project)
    assert bridge.openMaterialAsset(plate)
    wait_for_material_idle(bridge.materialLibrary, "plate", "saved substrate projection")
    assert bridge.materialLibrary.roles == ["substrate"]
    assert bridge.materialLibrary.category == "substrate" and not bridge.materialLibrary.dirty
    assert window.property("categoryKey") == "material_profile"
    assert window.property("materialUse") == "substrate"
    assert bridge.openMaterialAsset(air)
    wait_for_material_idle(bridge.materialLibrary, "air", "saved medium projection")
    assert bridge.materialLibrary.roles == ["medium"]
    assert bridge.materialLibrary.category == "" and not bridge.materialLibrary.dirty
    assert store.manifest == before
    assert all(row["key"] != "optical_material" for row in qml(engine, window, "categories"))
