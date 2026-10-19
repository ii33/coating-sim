"""Offline managed resource creation, closure and stale-save contracts."""
from copy import deepcopy
import shutil
import pytest
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_material_library import ProjectMaterialLibrary
from coat_sim.core.materials import default_material_profile
from tests.test_input_snapshot import fixture

pytestmark = pytest.mark.contract


class EmptyCatalog:
    """Keep local resource tests explicit about not exercising global data."""

    def catalog_records(self):
        return []


def service(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    fixture(external)
    store = ProjectStore.create(tmp_path / "project", "Resource project")
    api = ProjectResourceService(store)
    api._project_film_service = ProjectFilmService(store, global_material_library=EmptyCatalog())
    return api, external


def test_external_load_copies_closure_and_handles_survive_generations(tmp_path):
    api, external = service(tmp_path)
    doc = api.load("process", external / "recipe.cproc")
    assert doc.path.startswith(str(api.root / "data" / "processes"))
    current_handle = api.document_handle(api._identity_for_path(doc.path))
    assert api.load("process", current_handle).data == doc.data
    assert len(api.store.manifest["documents"]) == 6
    shutil.rmtree(external)
    assert api.load("process", doc.path).runtime["steps"][0]["target_nm"] == 1
    changed = deepcopy(doc.data)
    changed["name"] = "Changed"
    api.save("process", doc.path, changed, overwrite=True)
    assert api.load("process", doc.path).data["name"] == "Changed"
    assert api.load("process", current_handle).data["name"] == "Changed"
    snapshot = api.freeze_process(changed)
    assert snapshot["runtime"]["steps"][0]["target_nm"] == 1


def test_resource_create_save_and_atomic_default_from_empty_project(tmp_path):
    api, external = service(tmp_path)
    imported = api.load("process", external / "recipe.cproc")
    machine = api.load("machine", api.resolve_reference(imported.path, imported.data["machine_file"]))
    machine.data["description"] = "Machine copy"
    new_machine = api.create_resource("machine", machine.data)
    process = deepcopy(imported.data)
    process["name"] = "Created process"
    process["machine_file"] = api.document_handle(new_machine).name
    new_process = api.create_resource("process", process)
    revision = api.store.manifest["revision"]
    config = api.save_configuration("Chosen", new_process, make_default=True)
    assert api.store.manifest["revision"] == revision + 1
    assert api.resolve_default()["root_document_id"] == config
    assert ProjectStore.open(api.store.project_file).resolve_default()["runtime"]["profile"]["name"] == "Created process"


def test_new_dependency_binding_and_stale_editor_saves(tmp_path):
    api, external = service(tmp_path)
    doc = api.load("process", external / "recipe.cproc")
    one, two = api.fork_editor(), api.fork_editor()
    a, b = one.load("process", doc.path), two.load("process", doc.path)
    a.data["name"] = "A"
    one.save("process", a.path, a.data, overwrite=True)
    b.data["name"] = "B"
    with pytest.raises(ValueError, match="changed since"):
        two.save("process", b.path, b.data, overwrite=True)
    assert api.load("process", doc.path).data["name"] == "A"
    imported_machine = api.load("machine", external / "machines" / "machine.cmac")
    a = one.load("process", doc.path)
    a.data["machine_file"] = api.document_handle(api._identity_for_path(imported_machine.path)).name
    one.save("process", a.path, a.data, overwrite=True)


def test_multi_document_transaction_rejects_invalid_runtime_and_stale_revision(tmp_path):
    api, external = service(tmp_path)
    doc = api.load("process", external / "recipe.cproc")
    graph = api.resolve_default() or api.resolve_document(api._identity_for_path(doc.path))
    source = next(row for row in graph["documents"] if row["kind"] == "source")
    process = next(row for row in graph["documents"] if row["kind"] == "process")
    updates = {source["id"]: deepcopy(source["data"]), process["id"]: deepcopy(process["data"])}
    updates[source["id"]]["source_exponent"] = 2
    updates[process["id"]]["steps"][0]["source_slot"] = 99
    before = api.store.manifest
    with pytest.raises(ValueError):
        api.store.save_documents(updates)
    assert api.store.manifest == before
    updates[process["id"]]["steps"][0]["source_slot"] = 1
    api.store.save_documents(updates, expected_revisions={source["id"]: source["sha256"]})
    with pytest.raises(ValueError, match="changed since"):
        api.store.save_documents(updates, expected_revisions={source["id"]: source["sha256"]})


def test_complete_material_profiles_are_isolated_and_import_is_atomic(tmp_path):
    api, _ = service(tmp_path)
    library = ProjectMaterialLibrary(api, global_library=EmptyCatalog())
    assert library.catalog_records() == []
    record = default_material_profile()
    record["name"] = "Empirical material"
    library.write(record)
    assert library.read(record["id"])["record"]["resistance_model"] == record["resistance_model"]
    package = {"kind": "coat_sim.material_package", "records": [record]}
    from coat_sim.core.material_business import MATERIAL_PACKAGE_KIND
    package["kind"] = MATERIAL_PACKAGE_KIND
    plan = library.preview_import(package)["plan"]
    revision = api.store.manifest["revision"]
    result = library.apply_import(plan, {record["id"]: "copy"})
    assert result["copied"] == 1
    assert api.store.manifest["revision"] == revision + 1
    assert len(library.catalog_records()) == 2
    second = ProjectResourceService(ProjectStore.create(tmp_path / "second", "Second"))
    assert ProjectMaterialLibrary(second, global_library=EmptyCatalog()).catalog_records() == []


def test_frozen_project_gate_blocks_headless_mutation_and_external_load(tmp_path):
    api, external = service(tmp_path)
    doc = api.load("process", external / "recipe.cproc")
    api._gate.frozen = True
    with pytest.raises(ValueError, match="paused"):
        api.save("process", doc.path, doc.data, overwrite=True)
    with pytest.raises(ValueError, match="paused"):
        api.load("machine", external / "machines" / "machine.cmac")
    assert api.load("process", doc.path).valid


def test_full_profile_optics_are_atomic_and_removing_referenced_dataset_rejects(tmp_path):
    from coat_sim.core.film_design import new_design
    from tests.test_project_core import material
    api, _ = service(tmp_path)
    library = ProjectMaterialLibrary(api, global_library=EmptyCatalog())
    profile = default_material_profile()
    profile["id"] = "film"
    profile["optical_constants"] = material()["optical_sets"]
    before = api.store.manifest["revision"]
    library.write(profile)
    assert api.store.manifest["revision"] == before + 1
    film_service = ProjectFilmService(api.store, global_material_library=EmptyCatalog())
    assert [item["id"] for item in film_service.material_options("film")] == ["film", "builtin_ideal_air"]
    design = new_design("Linked")
    design["front_layers"] = [{"id": "layer", "material_id": "film", "optical_set_id": "set",
        "thickness": {"value": 100., "unit": "nm"}, "note": ""}]
    api.store.create_document("film", design)
    prior = api.store.manifest
    changed = deepcopy(profile)
    changed["optical_constants"] = []
    with pytest.raises(ValueError, match="optical set"):
        library.write(changed, update=True)
    assert api.store.manifest == prior
    assert library.read("film")["record"]["optical_constants"] == profile["optical_constants"]


def test_optical_only_material_import_requires_explicit_conflict_and_stale_plan_rejected(tmp_path):
    from coat_sim.core.material_business import MATERIAL_PACKAGE_KIND
    from tests.test_project_core import material
    api, _ = service(tmp_path)
    record = material()
    optical_id = api.store.create_document("optical_material", {"kind": "coat_sim.project_optical_material", "version": 1, "material": record})
    library = ProjectMaterialLibrary(api, global_library=EmptyCatalog())
    profile = default_material_profile()
    profile["id"] = record["id"]
    profile["optical_constants"] = record["optical_sets"]
    plan = library.preview_import({"kind": MATERIAL_PACKAGE_KIND, "records": [profile]})["plan"]
    assert plan["rows"][0]["conflict"] == "optical"
    with pytest.raises(ValueError, match="conflict"):
        library.apply_import(plan, {})
    assert not library.catalog_records()
    changed = deepcopy(record)
    changed["name"] = "Updated elsewhere"
    api.store.save_document(optical_id, {"kind": "coat_sim.project_optical_material", "version": 1, "material": changed})
    with pytest.raises(ValueError, match="changed after"):
        library.apply_import(plan, {"film": "overwrite"})
    plan = library.preview_import({"kind": MATERIAL_PACKAGE_KIND, "records": [profile]})["plan"]
    assert library.apply_import(plan, {"film": "copy"})["copied"] == 1
    assert api.store.resolve_document(optical_id)["materials"][0]["name"] == "Updated elsewhere"


def test_opening_resource_selection_follows_first_machine_slot_not_graph_uuid_order(tmp_path):
    from dataclasses import replace
    from coat_sim.core.project_default_loading import load_project_default
    from coat_sim.core.project_dependencies import json_bytes
    from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
    from coat_sim.project_session import ProjectSessionHost
    api, external = service(tmp_path)
    doc = api.load('process', external / 'recipe.cproc')
    identity = api._identity_for_path(doc.path)
    api.save_configuration('Default', identity, make_default=True)
    host = ProjectSessionHost(lambda _root: api.store)
    binding = host.open_project(api.root).binding
    workspace = ProjectResourceWorkspace.for_project(host, binding)
    plan = load_project_default(api.store)
    graph = plan.graph
    assert graph is not None
    machine = next(row for row in graph['documents'] if row['kind'] == 'machine')
    from pathlib import Path
    first_mask = Path(machine['data']['masks'][0]['file']).stem
    # Force graph ordering opposite to the machine's semantically ordered slots.
    graph['documents'].sort(key=lambda row: row['id'] == first_mask)
    plan = replace(plan, _graph_bytes=json_bytes(graph))
    assert workspace.initialize_default(plan)
    assert workspace.initial_document('mask') == first_mask
