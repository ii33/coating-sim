"""Selective project-resource transfer contracts (headless, no GUI)."""
from copy import deepcopy
import zipfile

import pytest

from coat_sim.core.film_design import new_design
from coat_sim.core.project_store import ProjectError, ProjectStore
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.project_package import export_project
from tests.test_input_snapshot import write_json
from tests.test_project_core import file_snapshot, material, setup_project

pytestmark = pytest.mark.contract


def _row(rows, identity):
    return next(row for row in rows if row["id"] == identity)


def _service(store):
    return ProjectResourceService(store)


def _copy_target(tmp_path):
    """A target with a live default makes transfer isolation observable."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    store, _external, _process_id, default_id = setup_project(tmp_path, name="Target")
    return _service(store), store, default_id


def _source_project(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store, external, process_id, _configuration_id = setup_project(tmp_path, name="Source")
    # A valid but unrelated source proves that selecting one root does not mean
    # importing every resource in the source project.
    unrelated = tmp_path / "unrelated.csrc"
    write_json(unrelated, {"schema_version": 2, "kind": "point", "source_exponent": 7.})
    unrelated_id = store.import_document(unrelated)
    return store, external, process_id, unrelated_id


def test_selective_project_transfer_imports_only_selected_dependency_closure(tmp_path):
    source_store, _external, process_id, unrelated_id = _source_project(tmp_path / "source")
    target_api, target_store, target_default_id = _copy_target(tmp_path / "target")
    before_source = file_snapshot(source_store.root)
    before_target = file_snapshot(target_store.root)
    source_closure = source_store.resolve_document(process_id)
    closure_ids = {item["id"] for item in source_closure["documents"]}

    catalog = target_api.inspect_resources(source_store.project_file)
    assert catalog["catalog_id"]
    assert _row(catalog["rows"], process_id)["kind"] == "process"
    assert unrelated_id not in closure_ids
    preview = target_api.preview_resource_transfer(
        catalog["catalog_id"], [process_id], target="project")

    assert set(preview["selected_ids"]) == {process_id}
    assert set(preview["dependency_ids"]) == closure_ids - {process_id}
    assert {row["id"] for row in preview["rows"] if row["selected"]} == {process_id}
    # Browsing and previewing are both observational. The target's existing
    # project and its default selection remain byte-for-byte intact.
    assert file_snapshot(source_store.root) == before_source
    assert file_snapshot(target_store.root) == before_target

    result = target_api.apply_resource_transfer(preview["plan_id"])
    assert set(result["id_map"]) == closure_ids
    assert set(result["id_map"].values()).issubset(
        {row["id"] for row in target_store.manifest["documents"]})
    assert unrelated_id not in result["id_map"]
    assert target_store.manifest["default_configuration_id"] == target_default_id
    copied_process = target_store.resolve_document(result["id_map"][process_id])
    assert {item["kind"] for item in copied_process["documents"]} == {
        "process", "machine", "substrate", "source", "mask"}
    assert source_store.resolve_document(process_id) == source_closure


def test_inspect_and_transfer_one_standalone_resource_file(tmp_path):
    source_store, external, _process_id, _unrelated_id = _source_project(tmp_path / "source")
    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    standalone = external / "parts" / "source.csrc"
    before_source_file = standalone.read_bytes()
    catalog = target_api.inspect_resources(standalone)
    assert len(catalog["rows"]) == 1
    selected = catalog["rows"][0]
    assert selected["kind"] == "source"
    before_target = file_snapshot(target_store.root)

    preview = target_api.preview_resource_transfer(catalog["catalog_id"], [selected["id"]])
    result = target_api.apply_resource_transfer(preview["plan_id"])

    copied_id = result["id_map"][selected["id"]]
    assert target_store.resolve_document(copied_id)["documents"][0]["data"]["source_exponent"] == 1.
    assert standalone.read_bytes() == before_source_file
    assert file_snapshot(target_store.root) != before_target


def test_explicit_arbitrary_name_coatproj_remains_a_v1_json_descriptor(tmp_path):
    source_store, _external, process_id, _unrelated_id = _source_project(tmp_path / "source")
    descriptor = source_store.root / "research-copy.coatproj"
    source_store.project_file.rename(descriptor)
    opened = ProjectStore.open(descriptor)
    target_api, _target_store, _default_id = _copy_target(tmp_path / "target")

    catalog = target_api.inspect_resources(descriptor)

    assert opened.project_file == descriptor
    assert opened.manifest["version"] == 1
    assert catalog["source_type"] == "project"
    assert _row(catalog["rows"], process_id)["kind"] == "process"


def test_package_inspection_is_read_only_and_leaves_no_unpack_copy(tmp_path, monkeypatch):
    # Make the temporary validation copy observable under pytest's temp root.
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    source_store, _external, process_id, _unrelated_id = _source_project(tmp_path / "source")
    package = tmp_path / "portable-name.cproject"
    export_project(source_store, package)
    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    before = file_snapshot(tmp_path)

    catalog = target_api.inspect_resources(package)

    assert _row(catalog["rows"], process_id)["kind"] == "process"
    assert catalog["rows"]
    assert all(row["read_only"] for row in catalog["rows"])
    assert file_snapshot(tmp_path) == before
    assert not list(tmp_path.glob("coat-resource-browser-*"))
    assert file_snapshot(target_store.root) == file_snapshot(tmp_path / "target" / "project")


def test_project_package_source_is_validated_before_it_is_browsed(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    source_store, _external, _process_id, _unrelated_id = _source_project(tmp_path / "source")
    package = tmp_path / "damaged.cproject"
    export_project(source_store, package)
    # An otherwise valid archive with an extra entry violates the existing
    # package import contract and must not become a browsable resource catalog.
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr("unexpected.txt", "not a managed input")
    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    before = file_snapshot(target_store.root)

    with pytest.raises((ProjectError, ValueError, OSError)):
        target_api.inspect_resources(package)

    assert file_snapshot(target_store.root) == before
    assert not list(tmp_path.glob("coat-resource-browser-*"))


@pytest.mark.parametrize("invalid_source_name", ["folder", "project.json"])
def test_directory_and_legacy_project_json_are_not_resource_sources(tmp_path, invalid_source_name):
    source_store = ProjectStore.create(tmp_path / "source", "Empty")
    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    invalid_source = source_store.root if invalid_source_name == "folder" else source_store.root / "project.json"
    if invalid_source_name == "project.json":
        invalid_source.write_bytes(source_store.project_file.read_bytes())
    before = file_snapshot(target_store.root)

    with pytest.raises((ProjectError, ValueError, OSError)):
        target_api.inspect_resources(invalid_source)

    assert file_snapshot(target_store.root) == before
    assert source_store.project_file.is_file()


def test_cancelled_transfer_changes_neither_source_nor_target(tmp_path):
    source_store, _external, process_id, _unrelated_id = _source_project(tmp_path / "source")
    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    before_source = file_snapshot(source_store.root)
    before_target = file_snapshot(target_store.root)
    catalog = target_api.inspect_resources(source_store.project_file)
    preview = target_api.preview_resource_transfer(catalog["catalog_id"], [process_id])

    target_api.cancel_resource_transfer(preview["plan_id"])

    assert file_snapshot(source_store.root) == before_source
    assert file_snapshot(target_store.root) == before_target
    with pytest.raises((ProjectError, ValueError)):
        target_api.apply_resource_transfer(preview["plan_id"])


def test_releasing_catalog_also_expires_its_pending_transfer_plan(tmp_path):
    source_store, _external, process_id, _unrelated_id = _source_project(tmp_path / "source")
    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    before = file_snapshot(target_store.root)
    catalog = target_api.inspect_resources(source_store.project_file)
    preview = target_api.preview_resource_transfer(catalog["catalog_id"], [process_id])

    target_api.release_resource_catalog(catalog["catalog_id"])

    with pytest.raises(ProjectError) as error:
        target_api.apply_resource_transfer(preview["plan_id"])
    assert error.value.code == "expired_plan"
    assert file_snapshot(target_store.root) == before


@pytest.mark.parametrize("resource_kind", ["film", "expression_template"])
def test_copy_conflicting_material_rewrites_film_and_template_references(tmp_path, resource_kind):
    source_store = ProjectStore.create(tmp_path / "source", "Optical source")
    source_material = material("shared_material", n=2.0)
    if resource_kind == "film":
        resource = new_design("Source film")
        resource["front_layers"] = [{
            "id": "source-layer", "material_id": "shared_material", "optical_set_id": "set",
            "thickness": {"value": 100., "unit": "nm"}, "note": "source"}]
        resource_id = source_store.create_document(
            "film", resource, materials={"shared_material": source_material})
        reference_path: tuple[str | int, ...] = ("front_layers", 0, "material_id")
    else:
        resource = {
            "schema_version": 1, "template_id": "source-template", "name": "Source template",
            "note": "synthetic fixture", "expression": {"text": "A", "symbols": {
                "A": {"material_id": "shared_material", "optical_set_id": "set",
                      "thickness": {"value": 90., "unit": "nm"}, "note": "symbol"}}}}
        resource_id = source_store.create_document(
            "expression_template", resource, materials={"shared_material": source_material})
        reference_path = ("expression", "symbols", "A", "material_id")

    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    existing_material = material("shared_material", n=3.0)
    existing_optical_id = target_store.create_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1,
        "material": existing_material})
    before_source = file_snapshot(source_store.root)
    before_existing = target_store.resolve_document(existing_optical_id)
    catalog = target_api.inspect_resources(source_store.project_file)
    preview = target_api.preview_resource_transfer(catalog["catalog_id"], [resource_id])
    source_optical = next(row for row in catalog["rows"] if row["kind"] == "optical_material")
    conflict = _row(preview["rows"], source_optical["id"])
    assert conflict["conflict"] == "different"
    assert "copy" in conflict["choices"]
    before_apply = file_snapshot(target_store.root)

    result = target_api.apply_resource_transfer(
        preview["plan_id"], decisions={source_optical["id"]: "copy"})

    copied_resource_id = result["id_map"][resource_id]
    copied_graph = target_store.resolve_document(copied_resource_id)
    copied_row = next(row for row in copied_graph["documents"] if row["kind"] == resource_kind)
    optical_row = next(row for row in copied_graph["documents"] if row["kind"] == "optical_material")
    new_material_id = optical_row["data"]["material"]["id"]
    reference = copied_row["data"]
    for key in reference_path:
        reference = reference[key]
    assert new_material_id != "shared_material"
    assert reference == new_material_id
    assert optical_row["id"] == result["id_map"][source_optical["id"]]
    assert optical_row["data"]["material"]["optical_sets"] == source_material["optical_sets"]
    after_existing = target_store.resolve_document(existing_optical_id)
    existing_data = next(row["data"] for row in after_existing["documents"]
                         if row["id"] == existing_optical_id)
    before_existing_data = next(row["data"] for row in before_existing["documents"]
                                if row["id"] == existing_optical_id)
    assert existing_data == before_existing_data
    assert file_snapshot(source_store.root) == before_source
    assert file_snapshot(target_store.root) != before_apply


def test_identical_material_dependency_is_reused_without_duplicate_record(tmp_path):
    source_store = ProjectStore.create(tmp_path / "source", "Source")
    shared = material("shared_material", n=2.0)
    film_doc = new_design("Identical material film")
    film_doc["front_layers"] = [{
        "id": "layer", "material_id": "shared_material", "optical_set_id": "set",
        "thickness": {"value": 100., "unit": "nm"}, "note": ""}]
    film_id = source_store.create_document("film", film_doc,
                                          materials={"shared_material": shared})
    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    existing_id = target_store.create_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1, "material": shared})
    old_count = len(target_store.manifest["documents"])
    catalog = target_api.inspect_resources(source_store.project_file)
    preview = target_api.preview_resource_transfer(catalog["catalog_id"], [film_id])
    source_optical = next(row for row in catalog["rows"] if row["kind"] == "optical_material")
    assert _row(preview["rows"], source_optical["id"])["conflict"] == "identical"

    result = target_api.apply_resource_transfer(preview["plan_id"])

    assert result["id_map"][source_optical["id"]] == existing_id
    assert len(target_store.manifest["documents"]) == old_count + 1
    assert len([row for row in target_store.manifest["documents"]
                if row["kind"] == "optical_material"]) == 1
    copied = target_store.resolve_document(result["id_map"][film_id])
    assert next(row["data"] for row in copied["documents"] if row["kind"] == "film").get(
        "front_layers")[0]["material_id"] == "shared_material"


def test_apply_rejects_missing_conflict_decision_atomically(tmp_path):
    source_store = ProjectStore.create(tmp_path / "source", "Source")
    source_material = material("shared_material", n=2.0)
    film_doc = new_design("Conflict film")
    film_doc["front_layers"] = [{
        "id": "layer", "material_id": "shared_material", "optical_set_id": "set",
        "thickness": {"value": 100., "unit": "nm"}, "note": ""}]
    film_id = source_store.create_document("film", film_doc,
                                          materials={"shared_material": source_material})
    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    target_store.create_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1,
        "material": material("shared_material", n=3.0)})
    catalog = target_api.inspect_resources(source_store.project_file)
    preview = target_api.preview_resource_transfer(catalog["catalog_id"], [film_id])
    before = file_snapshot(target_store.root)

    with pytest.raises((ProjectError, ValueError)):
        target_api.apply_resource_transfer(preview["plan_id"], decisions={})

    assert file_snapshot(target_store.root) == before


def test_apply_detects_changed_source_and_does_not_publish_target(tmp_path):
    source_store, _external, process_id, _unrelated_id = _source_project(tmp_path / "source")
    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    catalog = target_api.inspect_resources(source_store.project_file)
    preview = target_api.preview_resource_transfer(catalog["catalog_id"], [process_id])
    process = next(row for row in source_store.resolve_document(process_id)["documents"]
                   if row["id"] == process_id)
    changed = deepcopy(process["data"])
    changed["name"] = "Changed after preview"
    source_store.save_document(process_id, changed)
    before_target = file_snapshot(target_store.root)

    with pytest.raises(ProjectError) as error:
        target_api.apply_resource_transfer(preview["plan_id"])

    assert error.value.code == "stale_source"
    assert file_snapshot(target_store.root) == before_target


def test_apply_detects_stale_target_project_and_preserves_concurrent_winner(tmp_path):
    source_store, _external, process_id, _unrelated_id = _source_project(tmp_path / "source")
    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    catalog = target_api.inspect_resources(source_store.project_file)
    preview = target_api.preview_resource_transfer(catalog["catalog_id"], [process_id])
    concurrently_opened = ProjectStore.open(target_store.project_file)
    concurrently_opened.create_document("film", new_design("Concurrent winner"))
    winner = file_snapshot(target_store.root)

    with pytest.raises(ProjectError) as error:
        target_api.apply_resource_transfer(preview["plan_id"])

    assert error.value.code in {"stale_project", "stale_target"}
    assert file_snapshot(target_store.root) == winner


def test_frozen_project_gate_blocks_transfer_without_partial_write(tmp_path):
    source_store, _external, process_id, _unrelated_id = _source_project(tmp_path / "source")
    target_api, target_store, _default_id = _copy_target(tmp_path / "target")
    catalog = target_api.inspect_resources(source_store.project_file)
    preview = target_api.preview_resource_transfer(catalog["catalog_id"], [process_id])
    before = file_snapshot(target_store.root)
    target_api._gate.frozen = True

    with pytest.raises(ProjectError):
        target_api.apply_resource_transfer(preview["plan_id"])

    assert file_snapshot(target_store.root) == before


def test_export_to_arbitrary_named_target_descriptor_never_changes_source(tmp_path):
    source, _external, process_id, _unrelated = _source_project(tmp_path / "source")
    target = ProjectStore.create(tmp_path / "destination", "Destination")
    named = target.project_file.with_name("任意工程名.coatproj")
    target.project_file.rename(named)
    target = ProjectStore.open(named)
    source_api = _service(source)
    catalog = source_api.inspect_resources("project")
    before = file_snapshot(source.root)
    plan = source_api.preview_resource_transfer(catalog["catalog_id"], [process_id], named)
    result = source_api.apply_resource_transfer(plan["plan_id"])
    assert file_snapshot(source.root) == before
    reopened = ProjectStore.open(named)
    assert reopened.resolve_document(result["id_map"][process_id])["runtime"]["profile"]["name"]
    assert not (target.root / "project.coatproj").exists()
    assert reopened.manifest["default_configuration_id"] is None


def test_selected_export_package_does_not_replace_existing_archive(tmp_path):
    source, _external, process_id, unrelated = _source_project(tmp_path / "source")
    api = _service(source)
    catalog = api.inspect_resources("project")
    package = tmp_path / "selection.cproject"
    plan = api.preview_resource_transfer(catalog["catalog_id"], [process_id], package)
    result = api.apply_resource_transfer(plan["plan_id"])
    from coat_sim.project_package import import_project
    copy = import_project(package, tmp_path / "restored")
    assert len(copy.manifest["documents"]) == len(result["id_map"])
    assert unrelated not in result["id_map"]
    before = package.read_bytes()
    with pytest.raises(ProjectError) as error:
        api.preview_resource_transfer(catalog["catalog_id"], [process_id], package)
    assert error.value.code == "destination_exists"
    assert package.read_bytes() == before


def test_skipped_root_does_not_need_decision_for_its_conflicting_dependency(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    design = new_design("Skipped")
    design["front_layers"] = [{"id": "layer", "material_id": "film", "optical_set_id": "set",
        "thickness": {"value": 100., "unit": "nm"}, "note": ""}]
    identity = source.create_document("film", design, materials={"film": material()})
    target = ProjectStore.create(tmp_path / "target", "Target")
    target.create_document("optical_material", {"kind": "coat_sim.project_optical_material",
        "version": 1, "material": material(n=3.)})
    api = _service(target)
    catalog = api.inspect_resources(source.project_file)
    plan = api.preview_resource_transfer(catalog["catalog_id"], [identity])
    before = file_snapshot(target.root)
    result = api.apply_resource_transfer(plan["plan_id"], {identity: "skip"})
    assert result["selected_ids"] == []
    assert result["skipped"] == len(plan["rows"])
    assert file_snapshot(target.root) == before


def test_overwrite_that_breaks_existing_target_material_graph_is_atomic(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    empty = material()
    empty["optical_sets"] = []
    identity = source.create_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1, "material": empty})
    target = ProjectStore.create(tmp_path / "target", "Target")
    film = new_design("Target dependent")
    film["front_layers"] = [{"id": "layer", "material_id": "film", "optical_set_id": "set",
        "thickness": {"value": 100., "unit": "nm"}, "note": ""}]
    target.create_document("film", film, materials={"film": material()})
    api = _service(target)
    catalog = api.inspect_resources(source.project_file)
    plan = api.preview_resource_transfer(catalog["catalog_id"], [identity])
    before = file_snapshot(target.root)
    with pytest.raises(ProjectError):
        api.apply_resource_transfer(plan["plan_id"], {identity: "overwrite"})
    assert file_snapshot(target.root) == before


def test_same_film_id_requires_explicit_copy_and_gets_new_internal_identity(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    target = ProjectStore.create(tmp_path / "target", "Target")
    original = new_design("Original", design_id="same-film")
    source_id = source.create_document("film", original)
    changed = deepcopy(original)
    changed["name"] = "Existing other design"
    target_id = target.create_document("film", changed)
    api = _service(target)
    catalog = api.inspect_resources(source.project_file)
    plan = api.preview_resource_transfer(catalog["catalog_id"], [source_id])
    assert _row(plan["rows"], source_id)["conflict"] == "different"
    with pytest.raises(ProjectError):
        api.apply_resource_transfer(plan["plan_id"])
    result = api.apply_resource_transfer(plan["plan_id"], {source_id: "copy"})
    imported = target.resolve_document(result["id_map"][source_id])["documents"][0]["data"]
    assert imported["id"] != "same-film"
    assert target.resolve_document(target_id)["documents"][0]["data"] == changed


def test_export_uses_open_target_owner_and_cannot_bypass_its_frozen_gate(tmp_path):
    from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
    from coat_sim.project_session import ProjectSessionHost
    source, _external, process_id, _unrelated = _source_project(tmp_path / "source")
    target = ProjectStore.create(tmp_path / "target", "Target")
    stores = {source.root: source, target.root: target}
    host = ProjectSessionHost(lambda root: stores[root])
    source_binding = host.open_project(source.root).binding
    target_binding = host.open_project(target.root).binding
    source_workspace = ProjectResourceWorkspace.for_project(host, source_binding)
    target_workspace = ProjectResourceWorkspace.for_project(host, target_binding)
    api = source_workspace.service
    catalog = api.inspect_resources("project")
    plan = api.preview_resource_transfer(catalog["catalog_id"], [process_id], target.project_file)
    target_workspace.service._gate.frozen = True
    before = file_snapshot(target.root)
    with pytest.raises(ProjectError) as error:
        api.apply_resource_transfer(plan["plan_id"])
    assert error.value.code == "project_editing_frozen"
    assert file_snapshot(target.root) == before
    target_workspace.service._gate.frozen = False
    result = api.apply_resource_transfer(plan["plan_id"])
    assert target_workspace.store is target
    assert target.resolve_document(result["id_map"][process_id])["runtime"]["profile"]["name"]
