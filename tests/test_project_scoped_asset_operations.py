"""Local asset operations retain unrelated obsolete files, never accept them."""
from copy import deepcopy
from pathlib import Path

import pytest

from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_resource_service import ProjectResourceService
from tests.test_project_film_scoped_save import legacy_project

pytestmark = pytest.mark.contract


def _unchanged_files(store, rows):
    return {row["path"]: (store.root / row["path"]).read_bytes() for row in rows}


def test_delete_and_restore_film_do_not_parse_or_rewrite_unrelated_old_source(tmp_path):
    store, film_service, film_id, source_id, _process, _configuration, raw = legacy_project(tmp_path)
    resources = ProjectResourceService(store)
    original = deepcopy(store.manifest)
    others = [row for row in original["documents"] if row["id"] != film_id]
    untouched = _unchanged_files(store, others)
    film = film_service.load_document(film_id)
    catalog = resources.inspect_resources("project")
    assert any("aperture_kind" in warning for warning in catalog["warnings"])
    preview = resources.preview_resource_deletion(catalog["catalog_id"], [film_id])
    assert preview["can_apply"] and preview["affected_ids"] == [film_id]
    assert store.manifest == original  # Preview is not consent to mutate.
    receipt = resources.apply_resource_deletion(preview["plan_id"])
    assert receipt["recoverable"] and receipt["affected_ids"] == [film_id]
    assert store.manifest["documents"] == others
    assert _unchanged_files(store, others) == untouched
    assert Path(receipt["recovery_path"]).is_dir()
    with pytest.raises(ProjectError, match="aperture_kind"):
        store.resolve_document(source_id)
    restored = resources.restore_resource_deletion(receipt["receipt_id"])
    assert restored["restored"] and film_service.load_document(film_id).design == film.design
    assert [row for row in store.manifest["documents"] if row["id"] != film_id] == others
    assert _unchanged_files(store, others) == untouched
    row = next(row for row in others if row["id"] == source_id)
    assert (store.root / row["path"]).read_bytes() == raw
    with pytest.raises(ProjectError, match="aperture_kind"):
        store.resolve_document(source_id)


def test_selected_film_export_new_package_is_scoped_and_keeps_bad_source_out(tmp_path):
    from coat_sim.project_package import read_project_package

    store, _films, film_id, _source, _process, _configuration, _raw = legacy_project(tmp_path)
    resources = ProjectResourceService(store)
    before = _unchanged_files(store, store.manifest["documents"])
    descriptor = store.project_file.read_bytes()
    catalog = resources.inspect_resources("project")
    destination = tmp_path / "selected-film.cproject"
    preview = resources.preview_resource_transfer(catalog["catalog_id"], [film_id], str(destination))
    resources.apply_resource_transfer(preview["plan_id"])
    package = read_project_package(destination)
    assert {row["kind"] for row in package["manifest"]["documents"]} == {"film", "optical_material"}
    assert len([row for row in package["manifest"]["documents"] if row["kind"] == "film"]) == 1
    assert store.project_file.read_bytes() == descriptor
    assert _unchanged_files(store, store.manifest["documents"]) == before


@pytest.mark.parametrize("operation", ["create", "save", "batch", "import", "template", "material", "export", "checkpoint"])
def test_current_local_operation_matrix_never_rewrites_unrelated_unsupported_source(tmp_path, operation):
    from types import SimpleNamespace

    from coat_sim.core.materials import default_material_profile
    from coat_sim.core.project_dependencies import json_bytes
    from coat_sim.core.project_material_library import ProjectMaterialLibrary
    from tests.design_test_fixtures import _RECTANGULAR_SOURCE
    from tests.test_project_film_service import expression_template, material

    store, films, film_id, source_id, _process, _configuration, raw = legacy_project(tmp_path)
    source_row = next(row for row in store.manifest["documents"] if row["id"] == source_id)
    source_path = store.root / source_row["path"]
    current = deepcopy(_RECTANGULAR_SOURCE)
    current["name"] = "Independent current source"
    if operation == "create":
        created = store.create_document("source", current)
        assert store.resolve_document(created)["root_document_id"] == created
    elif operation == "save":
        good = next(row for row in store.manifest["documents"] if row["kind"] == "source" and row["id"] != source_id)
        value = store._read_record(good)
        value["source_exponent"] = 2.5
        store.save_document(good["id"], value)
        assert store._read_record(store._index()[good["id"]])["source_exponent"] == 2.5
    elif operation == "batch":
        ids = store.write_documents([("source", current)])
        assert len(ids) == 1
        changed = store._read_record(store._index()[ids[0]])
        changed["source_exponent"] = 2.5
        store.save_documents({ids[0]: changed})
        assert store._read_record(store._index()[ids[0]])["source_exponent"] == 2.5
    elif operation == "import":
        external = tmp_path / "current.csrc"
        external.write_bytes(json_bytes(current))
        created = store.import_document(external)
        assert store.resolve_document(created)["root_document_id"] == created
    elif operation == "template":
        saved = films.save_expression_template(expression_template(), materials={"symbol": material("symbol")})
        saved["note"] = "Current template update"
        films.save_expression_template(saved)
        assert films.load_expression_template(saved["template_id"])["note"] == saved["note"]
    elif operation == "material":
        library = ProjectMaterialLibrary(ProjectResourceService(store),
            global_library=SimpleNamespace(catalog_records=lambda: []))
        saved = library.write(default_material_profile("Current material", id="current-material"))
        assert saved["created"]
    elif operation == "export":
        exported = store.export_document(film_id, tmp_path / "selected-export")
        assert {row["kind"] for row in exported["documents"]} == {"film", "optical_material"}
    else:
        store.record_recovery([film_id])
        assert store.recover_session()["document_ids"] == [film_id]
    assert next(row for row in store.manifest["documents"] if row["id"] == source_id) == source_row
    assert source_path.read_bytes() == raw
    with pytest.raises(ProjectError, match="aperture_kind"):
        store.resolve_document(source_id)


def test_full_project_export_refuses_unsupported_document_with_exact_path(tmp_path):
    from coat_sim.project_package import export_project

    store, _films, _film_id, source_id, _process, _configuration, raw = legacy_project(tmp_path)
    row = store._index()[source_id]
    destination = tmp_path / "whole.cproject"
    before = store.project_file.read_bytes()
    with pytest.raises(ProjectError) as rejected:
        export_project(store, destination)
    assert row["path"] in str(rejected.value) and "aperture_kind" in str(rejected.value)
    assert not destination.exists() and store.project_file.read_bytes() == before
    assert (store.root / row["path"]).read_bytes() == raw


def test_same_content_simulation_save_keeps_revision_and_returns_current_receipt(tmp_path):
    from tests.test_project_simulation_resources import ray

    store, _films, _film_id, _source, _process, _configuration, _raw = legacy_project(tmp_path)
    resources = ProjectResourceService(store)
    first = resources.save_simulation_resource("raytrace_request", ray(), name="Current ray")
    identity = Path(first.path).stem
    before = store.project_file.read_bytes()
    revision = store._index()[identity]["sha256"]
    second = resources.save_simulation_resource("raytrace_request", ray(), name="Current ray",
        document_id=identity, expected_revision=revision)
    assert second.path == first.path and second.data == first.data
    assert second.runtime is not None
    assert second.runtime["revision"] == revision
    assert store.project_file.read_bytes() == before


def test_unsupported_optical_content_can_be_deleted_without_reading_its_name(tmp_path):
    from coat_sim.core.project_store import ProjectStore
    from tests.test_project_film_service import material
    from tests.test_project_mutation_capture import _rewrite

    store = ProjectStore.create(tmp_path / "project", "Unsupported")
    identity = store.create_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1, "material": material()})
    _rewrite(store, identity, {"material": None})
    resources = ProjectResourceService(store)
    catalog = resources.inspect_resources("project")
    assert identity in {row["id"] for row in catalog["unavailable_rows"]}
    preview = resources.preview_resource_deletion(catalog["catalog_id"], [identity])
    assert preview["can_apply"] and preview["unsupported"]
    result = resources.apply_resource_deletion(preview["plan_id"])
    assert result["recoverable"] is False and store.manifest["documents"] == []


def test_delete_empty_film_does_not_need_unrelated_opaque_optical_identity(tmp_path):
    from coat_sim.core.film_design import new_design
    from coat_sim.core.project_store import ProjectStore
    from tests.test_project_film_service import material
    from tests.test_project_mutation_capture import _rewrite

    store = ProjectStore.create(tmp_path / "project", "Unrelated")
    film_id = store.save_film_document(new_design("Empty current film"))
    optical = store.create_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1, "material": material()})
    _rewrite(store, optical, content=b"not JSON")
    resources = ProjectResourceService(store)
    catalog = resources.inspect_resources("project")
    preview = resources.preview_resource_deletion(catalog["catalog_id"], [film_id])
    assert preview["can_apply"]
    resources.apply_resource_deletion(preview["plan_id"])
    assert set(store._index()) == {optical}


def test_empty_current_film_creation_does_not_index_unrelated_opaque_optics(tmp_path):
    from coat_sim.core.film_design import new_design
    from coat_sim.core.project_store import ProjectStore
    from tests.test_project_film_service import material
    from tests.test_project_mutation_capture import _rewrite

    store = ProjectStore.create(tmp_path / "project", "Current")
    optical = store.create_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1, "material": material()})
    _rewrite(store, optical, content=b"not JSON")
    identity = store.create_document("film", new_design("No material references"))
    assert store.resolve_document(identity)["root_document_id"] == identity


def test_film_service_material_catalog_rejects_opaque_input_without_mutation(tmp_path):
    from coat_sim.core.project_film_service import ProjectFilmService
    from coat_sim.core.project_store import ProjectStore
    from tests.test_project_film_service import EmptyCatalog, material
    from tests.test_project_mutation_capture import _rewrite

    store = ProjectStore.create(tmp_path / "project", "Current")
    optical = store.create_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1, "material": material()})
    _rewrite(store, optical, content=b"not JSON")
    before = store.project_file.read_bytes()
    with pytest.raises(ProjectError, match="JSON"):
        ProjectFilmService(store, global_material_library=EmptyCatalog())
    assert store.project_file.read_bytes() == before
    assert (store.root / store._index()[optical]["path"]).read_bytes() == b"not JSON"


def test_film_store_create_and_save_empty_film_share_actual_reference_scope(tmp_path):
    from coat_sim.core.film_design import new_design
    from coat_sim.core.project_store import ProjectStore
    from tests.test_project_film_service import material
    from tests.test_project_mutation_capture import _rewrite

    store = ProjectStore.create(tmp_path / "project", "Current")
    optical = store.create_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1, "material": material()})
    _rewrite(store, optical, content=b"not JSON")
    before = deepcopy(store._index()[optical])
    design = new_design("Empty current film")
    identity = store.save_film_document(design)
    design["note"] = "Current change through the film store entry point"
    store.save_film_document(design, document_id=identity, expected_revision=store._index()[identity]["sha256"])
    assert store.resolve_document(identity)["documents"][0]["data"] == design
    assert store._index()[optical] == before
    assert (store.root / before["path"]).read_bytes() == b"not JSON"


def test_unbound_current_ray_does_not_stage_unrelated_unsupported_binary(tmp_path):
    from coat_sim.core.project_store import ProjectStore
    from tests.test_project_mutation_capture import _rewrite
    from tests.test_project_simulation_resources import asset, ray

    store = ProjectStore.create(tmp_path / "project", "Current")
    resources = ProjectResourceService(store)
    owned = resources.capture_simulation_asset("deposition_asset", asset(tmp_path / "external"))
    _rewrite(store, owned["document_id"], {"unsupported": True})
    saved = resources.save_simulation_resource("raytrace_request", ray(), name="No binary binding")
    assert saved.runtime is not None
    assert saved.runtime["payload"] == ray()


@pytest.mark.parametrize("binding", ["owned", "external"])
def test_bound_current_ray_skips_untrusted_lookup_but_keeps_new_asset_quota_validation(tmp_path, binding):
    from coat_sim.core.project_store import ProjectStore
    from tests.test_project_mutation_capture import _rewrite
    from tests.test_project_simulation_resources import asset, ray

    store = ProjectStore.create(tmp_path / "project", "Current")
    resources = ProjectResourceService(store)
    unused = resources.capture_simulation_asset("deposition_asset", asset(tmp_path / "unused", "Unused"))
    selected = asset(tmp_path / "selected", "Selected")
    if binding == "owned":
        owned = resources.capture_simulation_asset("deposition_asset", selected)
        selected = store.binary_asset_path(owned["document_id"])
        unsupported: dict[str, object] = {"role": "deposition_asset"}
    else:
        unsupported = {"unsupported": True}
    _rewrite(store, unused["document_id"], unsupported)
    row = deepcopy(store._index()[unused["document_id"]])
    original = (store.root / row["path"]).read_bytes()
    descriptor = store.project_file.read_bytes()
    if binding == "owned":
        saved = resources.save_simulation_resource("raytrace_request", ray(selected), name="Bound current ray")
        assert saved.runtime is not None
        assert Path(saved.runtime["payload"]["deposition_asset"]["path"]).read_bytes() == selected.read_bytes()
    else:
        # Adding binary bytes needs a provable project-wide quota. A malformed
        # retained envelope cannot prove it; this is a typed error, not KeyError.
        with pytest.raises(ProjectError, match="binary_asset.*" + row["path"]):
            resources.save_simulation_resource("raytrace_request", ray(selected), name="Bound current ray")
        assert store.project_file.read_bytes() == descriptor
    assert store._index()[unused["document_id"]] == row
    assert (store.root / row["path"]).read_bytes() == original


def test_reselecting_same_default_still_rejects_its_actual_unsupported_dependency(tmp_path):
    from tests.test_project_core import setup_project
    from tests.test_project_mutation_capture import _unsupported_source

    store, _external, _process, configuration = setup_project(tmp_path)
    _unsupported_source(store)
    before = store.project_file.read_bytes()
    with pytest.raises(ProjectError):
        store.set_default_configuration(configuration)
    assert store.project_file.read_bytes() == before
