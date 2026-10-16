"""The last explicit cdep save, never file order or session drafts, owns opening."""
from copy import deepcopy
import os
from unittest.mock import patch

import pytest

from coat_sim.core.film_design import new_design
from coat_sim.core.project_default_loading import load_project_default
from coat_sim.core.project_dependencies import ProjectError, json_bytes
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_package import export_project, import_project
from tests.test_project_compute import compute_for
from tests.test_project_core import setup_project

pytestmark = pytest.mark.contract


def configuration(store, identity):
    return deepcopy(store.resolve_document(identity)["configuration"])


@pytest.mark.parametrize("route", ["create", "add", "process_save", "service", "compute_new",
                                   "save", "save_many", "write_update", "write_new", "compute_update"])
def test_every_explicit_single_configuration_save_atomically_sets_opening_root(tmp_path, route):
    store, _external, process_id, first = setup_project(tmp_path)
    second = store.add_deposition_configuration("Second", process_id)
    assert store.manifest["default_configuration_id"] == second
    document = configuration(store, first)
    document["name"] = "Last explicitly saved"
    service = ProjectResourceService(store)
    compute = compute_for(store)
    if route == "create":
        saved = store.create_document("deposition_configuration", document, make_default=False)
    elif route == "add":
        saved = store.add_deposition_configuration(document["name"], process_id)
    elif route == "process_save":
        process = deepcopy(store.resolve_document(process_id)["runtime"]["profile"])
        saved, saved_process = store.save_deposition_configuration(document["name"], process,
            process_document_id=process_id, make_default=False)
        assert saved_process == process_id
    elif route == "service":
        saved = service.save_configuration(document["name"], process_id, make_default=False)
    elif route == "compute_new":
        saved = compute.save_configuration(process_id, {}, document["name"], make_default=False)
    elif route == "save":
        store.save_document(first, document)
        saved = first
    elif route == "save_many":
        store.save_documents({first: document})
        saved = first
    elif route == "write_update":
        assert store.write_documents(updates={first: document}) == []
        saved = first
    elif route == "write_new":
        saved, = store.write_documents([("deposition_configuration", document)])
    else:
        compute.load_configuration(first)
        saved = compute.save_configuration(process_id, {}, document["name"], configuration_id=first,
                                           make_default=False)
    reopened = ProjectStore.open(store.project_file)
    plan = load_project_default(reopened)
    assert reopened.manifest["default_configuration_id"] == saved == plan.configuration_id
    assert plan.process_document_id == process_id
    graph = plan.graph
    assert graph is not None and graph["configuration"]["name"] == document["name"]


def test_unchanged_config_save_updates_root_but_matching_root_is_a_noop(tmp_path):
    store, _external, process_id, first = setup_project(tmp_path)
    saved = configuration(store, first)
    store.add_deposition_configuration("More recently created", process_id)
    before = store.manifest["revision"]
    store.save_document(first, saved)
    assert store.manifest["default_configuration_id"] == first
    assert store.manifest["revision"] == before + 1
    before_bytes = store.project_file.read_bytes()
    store.save_document(first, saved)
    assert store.project_file.read_bytes() == before_bytes


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("route", ["save", "write"])
def test_unordered_multiple_configuration_save_preserves_root_independent_of_mapping_order(tmp_path, reverse, route):
    store, _external, process_id, first = setup_project(tmp_path)
    second = store.add_deposition_configuration("Second", process_id)
    latest = store.add_deposition_configuration("Latest outside batch", process_id)
    ids = [first, second] if not reverse else [second, first]
    updates = {identity: configuration(store, identity) | {"notes": "Batch edit"} for identity in ids}
    if route == "save":
        store.save_documents(updates)
    else:
        store.write_documents(updates=updates)
    assert store.manifest["default_configuration_id"] == latest
    assert all(configuration(store, identity)["notes"] == "Batch edit" for identity in ids)
    store.save_documents(updates, default_configuration_id=first)
    assert store.manifest["default_configuration_id"] == first
    before = store.project_file.read_bytes()
    with pytest.raises(ProjectError, match="saved in this transaction"):
        store.save_documents(updates, default_configuration_id=latest)
    assert store.project_file.read_bytes() == before


def test_unordered_new_configuration_batch_does_not_guess_last_created_item(tmp_path):
    store, _external, _process_id, first = setup_project(tmp_path)
    document = configuration(store, first)
    created = store.write_documents([("deposition_configuration", document | {"name": "A"}),
                                     ("deposition_configuration", document | {"name": "B"})])
    assert len(created) == 2 and store.manifest["default_configuration_id"] == first


@pytest.mark.parametrize("selection", ["existing", "clear"])
def test_process_only_save_cannot_supply_or_clear_an_unrelated_opening_root(tmp_path, selection):
    store, _external, process_id, first = setup_project(tmp_path)
    second = store.add_deposition_configuration("Latest", process_id)
    process = deepcopy(store.resolve_document(process_id)["runtime"]["profile"])
    before, manifest = store.project_file.read_bytes(), store.manifest
    with pytest.raises(ProjectError) as blocked:
        store.save_documents({process_id: process | {"name": "Only process"}},
                             default_configuration_id=first if selection == "existing" else None)
    assert blocked.value.code == "dependency_conflict"
    assert store.project_file.read_bytes() == before and store.manifest == manifest
    assert store.manifest["default_configuration_id"] == second


def test_failed_validation_or_manifest_publication_cannot_change_saved_entry(tmp_path):
    store, _external, process_id, first = setup_project(tmp_path)
    document = configuration(store, first)
    before, manifest = store.project_file.read_bytes(), store.manifest
    with pytest.raises(ProjectError):
        store.create_document("deposition_configuration", document | {"process_document_id": "0" * 32})
    assert store.project_file.read_bytes() == before and store.manifest == manifest
    with patch("coat_sim.core.project_store.replace_with_retry", side_effect=OSError("save interrupted")):
        with pytest.raises(ProjectError) as interrupted:
            ProjectResourceService(store).save_configuration("Must not become entry", process_id)
    assert interrupted.value.code == "io_error"
    assert store.project_file.read_bytes() == before and store.manifest == manifest
    assert load_project_default(ProjectStore.open(store.project_file)).configuration_id == first


def test_stale_store_and_loaded_configuration_cas_keep_newer_successful_entry(tmp_path):
    store, _external, process_id, first = setup_project(tmp_path)
    stale_store = ProjectStore.open(store.project_file)
    compute = compute_for(store)
    compute.load_configuration(first)
    store.save_document(first, configuration(store, first) | {"notes": "Newer writer"})
    second = store.add_deposition_configuration("Last success", process_id)
    before = store.project_file.read_bytes()
    with pytest.raises(ProjectError) as stale:
        compute.save_configuration(process_id, {}, "Stale save", configuration_id=first)
    assert stale.value.code == "stale_document"
    with pytest.raises(ProjectError) as other_stale:
        stale_store.add_deposition_configuration("Stale whole-store save", process_id)
    assert other_stale.value.code == "stale_project"
    assert store.project_file.read_bytes() == before
    assert ProjectStore.open(store.project_file).manifest["default_configuration_id"] == second


def test_unrelated_saves_recovery_and_mtime_do_not_select_a_configuration(tmp_path):
    store, _external, process_id, first = setup_project(tmp_path)
    second = store.add_deposition_configuration("Last configuration", process_id)
    process = deepcopy(store.resolve_document(process_id)["runtime"]["profile"])
    store.save_document(process_id, process | {"name": "Only process edited"})
    machine = next(row for row in store.resolve_document(process_id)["documents"] if row["kind"] == "machine")
    store.save_document(machine["id"], machine["data"] | {"name": "Only machine edited"})
    store.create_document("film", new_design("Only film saved"))
    store.update_information({"notes": "Only project notes edited"})
    store.record_recovery([first])
    first_path = next(store.root / row["path"] for row in store.manifest["documents"] if row["id"] == first)
    os.utime(first_path, (2_000_000_000, 2_000_000_000))
    assert load_project_default(ProjectStore.open(store.project_file)).configuration_id == second


def test_single_import_preserves_target_but_later_explicit_save_selects_it(tmp_path):
    store, _external, _process_id, first = setup_project(tmp_path)
    external = tmp_path / "imported.cdep"
    external.write_bytes(json_bytes(configuration(store, first) | {"name": "Imported, not edited"}))
    before = store.project_file.read_bytes()
    # Standalone .cdep import was never a supported dependency-capture format.
    with pytest.raises(ProjectError) as unsupported:
        store.import_document(external)
    assert unsupported.value.code == "dependency_conflict" and store.project_file.read_bytes() == before
    source_root = tmp_path / "source-project"
    source_root.mkdir()
    source, _source_external, _source_process, source_config = setup_project(source_root)
    package = tmp_path / "source.cproject"
    export_project(source, package)
    service = ProjectResourceService(store)
    catalog = service.inspect_resources(package)
    preview = service.preview_resource_transfer(catalog["catalog_id"], [source_config], target="project")
    result = service.apply_resource_transfer(preview["plan_id"])
    imported = result["id_map"][source_config]
    assert store.manifest["default_configuration_id"] == first
    store.save_document(imported, configuration(store, imported))
    assert load_project_default(ProjectStore.open(store.project_file)).configuration_id == imported


def test_complete_package_preserves_last_saved_root_not_last_created_document(tmp_path):
    store, _external, process_id, first = setup_project(tmp_path)
    store.add_deposition_configuration("Last created but not last saved", process_id)
    store.save_document(first, configuration(store, first))
    package = tmp_path / "saved.cproject"
    export_project(store, package)
    imported = import_project(package, tmp_path / "restored")
    restored = ProjectStore.open(imported.project_file)
    assert restored.manifest["id"] != store.manifest["id"]
    assert load_project_default(restored).configuration_id == first


def test_no_configuration_keeps_initial_state_and_other_project_saves_are_isolated(tmp_path):
    store, _external, process_id, first = setup_project(tmp_path)
    untouched = ProjectStore.create(tmp_path / "empty", "Empty")
    assert load_project_default(untouched).state == "empty_project"
    untouched.create_document("film", new_design("No configuration here"))
    assert load_project_default(untouched).state == "no_default"
    before = untouched.project_file.read_bytes()
    second = store.add_deposition_configuration("Last successful config", process_id)
    compute = compute_for(store)
    resources = ProjectResourceWorkspace.for_project(compute.host, compute.binding)
    plan = compute.load_default()
    assert plan.configuration_id == second and first != second
    assert resources.initialize_default(plan)
    assert resources.initial_document("process") == process_id
    assert untouched.project_file.read_bytes() == before
    assert load_project_default(untouched).configuration_id is None
