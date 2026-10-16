"""Offline project persistence, dependency, isolation and failure contracts."""
import hashlib
import json
import os
import shutil
from unittest.mock import patch

import pytest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.project_dependencies import json_bytes
from coat_sim.core.project_paths import PROJECT_LAYOUT_DIRECTORIES
from coat_sim.core.project_store import MANIFEST_FILENAME, PROJECT_VERSION, ProjectError, ProjectStore
from tests.test_input_snapshot import fixture, write_json

pytestmark = pytest.mark.contract


def setup_project(tmp_path, name="Project"):
    external = tmp_path / "external"
    external.mkdir()
    fixture(external)
    store = ProjectStore.create(tmp_path / "project", name)
    process_id = store.import_document(external / "recipe.cproc")
    configuration = store.add_deposition_configuration("Default", process_id, {
        "settings": {"grid_size": 21, "time_steps": 2}, "backend": "CPU"})
    store.set_default_configuration(configuration)
    return store, external, process_id, configuration


def by_kind(graph, kind):
    return next(row for row in graph["documents"] if row["kind"] == kind)


def file_snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def material(identifier="film", n=2.0):
    return {"id": identifier, "name": "Synthetic " + identifier, "notes": "test fixture",
            "optical_sets": [{"id": "set", "name": "Synthetic set", "kind": "tabulated",
                              "wavelength_unit": "nm", "wavelength_min": 400., "wavelength_max": 700.,
                              "wavelengths": [400., 700.], "n": [n, n], "k": [0., 0.],
                              "source": "Synthetic analytical fixture", "notes": "Not production data"}]}


def film(path):
    design = new_design("Portable film")
    design["front_layers"] = [{"id": "layer", "material_id": "film", "optical_set_id": "set",
                               "thickness": {"value": 100., "unit": "nm"}, "note": "test"}]
    design["workflow_settings"] = {"simulation": {"samples": 17, "use_k": True},
                                   "optimization": {"solver": "synthetic-test", "targets": []}}
    design = validate_design(design)
    write_json(path, design)
    return design


def test_empty_projects_detached_and_two_open_stores_do_not_change_cwd(tmp_path):
    cwd = os.getcwd()
    first = ProjectStore.create(tmp_path / "a", "A")
    second = ProjectStore.create(tmp_path / "b", "B")
    assert first.resolve_default() is None
    assert second.resolve_default() is None
    assert first.manifest["id"] != second.manifest["id"]
    copy = first.manifest
    copy["name"] = "external mutation"
    assert first.manifest["name"] == "A"
    assert ProjectStore.open(first.project_file).resolve_default() is None
    assert os.getcwd() == cwd


def test_create_keeps_directory_layout_and_original_version_one_json_contract(tmp_path):
    root = tmp_path / "new-project"
    store = ProjectStore.create(root, "新工程")
    assert store.root == root.resolve()
    assert store.project_file == root.resolve() / "project.coatproj"
    assert MANIFEST_FILENAME == "project.coatproj"
    assert PROJECT_VERSION == 1
    expected = {"kind": "coat_sim.project", "version": 1,
                "id": store.manifest["id"], "name": "新工程", "revision": 0,
                "documents": [], "default_configuration_id": None}
    assert json.loads(store.project_file.read_text(encoding="utf-8")) == expected
    assert store.project_file.read_bytes() == json_bytes(expected)
    assert not (root / "project.json").exists()
    assert {path.name for path in root.iterdir() if path.is_file()} == {"project.coatproj"}
    assert all((root / relative).is_dir() for relative in PROJECT_LAYOUT_DIRECTORIES)
    reopened = ProjectStore.open(store.project_file)
    assert reopened.root == store.root
    assert reopened.project_file == store.project_file
    assert reopened.manifest == expected


@pytest.mark.parametrize("entry", ["directory", "descriptor-directory", "legacy-json", "package"])
def test_open_rejects_directory_old_manifest_and_package_without_auto_discovery(tmp_path, entry):
    store = ProjectStore.create(tmp_path / "project", "P")
    if entry == "directory":
        path = store.root
    elif entry == "descriptor-directory":
        path = store.root / "folder.coatproj"
        path.mkdir()
    else:
        path = store.root / ("project.json" if entry == "legacy-json" else "project.cproject")
        path.write_bytes(store.project_file.read_bytes())
    before = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        ProjectStore.open(path)
    assert error.value.code == "invalid_project_file"
    assert file_snapshot(store.root) == before


def test_descriptor_symlink_is_rejected_before_canonicalization(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    link = tmp_path / "linked.coatproj"
    link.symlink_to(store.project_file)
    before = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        ProjectStore.open(link)
    assert error.value.code == "unsafe_path"
    assert file_snapshot(store.root) == before


def test_legacy_only_project_is_not_silently_migrated_or_discovered(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    legacy = store.root / "project.json"
    store.project_file.rename(legacy)
    before = file_snapshot(store.root)
    for path, expected_code in ((store.root, "invalid_project_file"),
                                (legacy, "invalid_project_file"),
                                (store.project_file, "missing_dependency")):
        with pytest.raises(ProjectError) as error:
            ProjectStore.open(path)
        assert error.value.code == expected_code
    assert file_snapshot(store.root) == before


@pytest.mark.parametrize("raw, code", [
    (b"{", "invalid_json"),
    (b'{"kind":"coat_sim.project","kind":"coat_sim.project"}', "invalid_json"),
    (b'{"kind":"not-a-project"}', "invalid_manifest"),
    (b"[]", "invalid_document"),
])
def test_descriptor_extension_does_not_bypass_json_or_manifest_validation(tmp_path, raw, code):
    descriptor = tmp_path / "invalid.coatproj"
    descriptor.write_bytes(raw)
    with pytest.raises(ProjectError) as error:
        ProjectStore.open(descriptor)
    assert error.value.code == code
    assert descriptor.read_bytes() == raw


def test_complete_default_closure_survives_external_deletion_and_project_move(tmp_path):
    store, external, process_id, config = setup_project(tmp_path)
    before = file_snapshot(external)
    graph = store.resolve_default()
    assert graph["root_document_id"] == config
    assert len(graph["documents"]) == 7
    assert by_kind(graph, "process")["data"]["schema_version"] == 7  # supported v6 migration
    assert len([row for row in graph["documents"] if row["kind"] == "source"]) == 1
    assert before == file_snapshot(external)
    shutil.rmtree(external)
    moved = tmp_path / "moved"
    shutil.move(store.root, moved)
    reopened = ProjectStore.open(moved / MANIFEST_FILENAME)
    assert reopened.resolve_default()["runtime"] == graph["runtime"]
    assert reopened.resolve_document(process_id)["input_snapshot"] == graph["input_snapshot"]


def test_normal_open_uses_last_saved_configuration_and_ignores_recovery(tmp_path):
    store, external, first_process, first_config = setup_project(tmp_path)
    recipe = json.loads((external / "recipe.cproc").read_text())
    recipe["steps"][0]["target_nm"] = 9.
    write_json(external / "recipe.cproc", recipe)
    other_process = store.import_document(external / "recipe.cproc")
    other_config = store.add_deposition_configuration("Other", other_process)
    store.record_recovery([first_config])
    assert store.manifest["default_configuration_id"] == other_config
    assert ProjectStore.open(store.project_file).resolve_default()["runtime"]["steps"][0]["target_nm"] == 9.
    store.save_document(first_config, store.resolve_document(first_config)["configuration"])
    assert ProjectStore.open(store.project_file).resolve_default()["runtime"]["steps"][0]["target_nm"] == 1.
    store.set_default_configuration(None)
    assert ProjectStore.open(store.project_file).resolve_default() is None
    assert store.resolve_document(first_process)["runtime"]["steps"][0]["target_nm"] == 1.
    assert first_config in {row["id"] for row in store.manifest["documents"]}


def test_saved_dependency_edit_rebases_ancestors_and_open_loads_current_version(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    graph = store.resolve_default()
    source = by_kind(graph, "source")
    previous_manifest = store.manifest
    source["data"]["source_exponent"] = 3.
    store.save_document(source["id"], source["data"])
    assert store.manifest["revision"] == previous_manifest["revision"] + 1
    assert {row["id"] for row in store.manifest["documents"]} == {row["id"] for row in previous_manifest["documents"]}
    reopened = ProjectStore.open(store.project_file)
    assert reopened.resolve_default()["runtime"]["machine"]["sources"][0]["profile"]["source_exponent"] == 3.
    # Prior generation is unchanged, and a previously resolved job snapshot is detached.
    assert graph["runtime"]["machine"]["sources"][0]["profile"]["source_exponent"] == 1.
    for row in previous_manifest["documents"]:
        assert hashlib.sha256((store.root / row["path"]).read_bytes()).hexdigest() == row["sha256"]
    before = store.manifest
    store.save_document(source["id"], source["data"])
    assert store.manifest == before


def test_simultaneous_projects_and_copied_identity_remain_storage_isolated(tmp_path):
    external = tmp_path / "point.csrc"
    write_json(external, {"schema_version": 2, "kind": "point", "source_exponent": 1.})
    a = ProjectStore.create(tmp_path / "a", "A")
    b = ProjectStore.create(tmp_path / "b", "B")
    a_id = a.import_document(external)
    b_id = b.import_document(external)
    value = a.resolve_document(a_id)["documents"][0]["data"]
    value["source_exponent"] = 5.
    a.save_document(a_id, value)
    assert b.resolve_document(b_id)["documents"][0]["data"]["source_exponent"] == 1.
    shutil.copytree(a.root, tmp_path / "copied")
    copy = ProjectStore.open(tmp_path / "copied" / MANIFEST_FILENAME)
    assert copy.manifest["id"] == a.manifest["id"]  # no silently invented clone identity
    value["source_exponent"] = 7.
    copy.save_document(a_id, value)
    assert a.resolve_document(a_id)["documents"][0]["data"]["source_exponent"] == 5.


def test_same_basename_different_dependencies_never_merge(tmp_path):
    store, external, process_id, _ = setup_project(tmp_path)
    first = external / "parts" / "source.csrc"
    second = external / "other" / "source.csrc"
    write_json(second, {"schema_version": 2, "kind": "point", "source_exponent": 4.})
    machine = json.loads((external / "machines" / "machine.cmac").read_text())
    machine["sources"][1]["file"] = "../other/source.csrc"
    write_json(external / "machines" / "machine.cmac", machine)
    another = store.import_document(external / "recipe.cproc")
    runtime = store.resolve_document(another)["runtime"]
    assert [entry["profile"]["source_exponent"] for entry in runtime["machine"]["sources"]] == [1., 4.]
    assert runtime["machine"]["sources"][0]["file"] != runtime["machine"]["sources"][1]["file"]
    assert first.exists() and store.resolve_document(process_id)["runtime"]["steps"][0]["target_nm"] == 1.


def test_missing_import_dependency_preserves_entire_project(tmp_path):
    store, external, _, _ = setup_project(tmp_path)
    before = file_snapshot(store.root)
    (external / "parts" / "source.csrc").unlink()
    with pytest.raises(ProjectError) as error:
        store.import_document(external / "recipe.cproc")
    assert error.value.code == "missing_dependency"
    assert file_snapshot(store.root) == before


@pytest.mark.parametrize("raw", ['{"schema_version":2,"kind":"point","kind":"point"}',
                                  '{"schema_version":2,"kind":"point","source_exponent":NaN}',
                                  '{"schema_version":2,"kind":"point","source_exponent":1e999}'])
def test_strict_json_import_rejects_duplicate_and_nonfinite(tmp_path, raw):
    path = tmp_path / "source.csrc"
    path.write_text(raw)
    store = ProjectStore.create(tmp_path / "project", "P")
    with pytest.raises(ProjectError) as error:
        store.import_document(path)
    assert error.value.code == "invalid_json"
    assert store.manifest["documents"] == []


def test_typed_mismatch_and_unsupported_legacy_design_rejected(tmp_path):
    path = tmp_path / "legacy.json"
    write_json(path, {"schema_version": 2, "kind": "point"})
    store = ProjectStore.create(tmp_path / "project", "P")
    with pytest.raises(ProjectError, match="suffix"):
        store.import_document(path)
    path = tmp_path / "process.cproc"
    write_json(path, {"schema_version": 2, "kind": "point"})
    with pytest.raises(ProjectError) as error:
        store.import_document(path)
    assert error.value.code == "invalid_document"


@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_manifest_unsupported_versions_require_explicit_migration(tmp_path, version):
    store = ProjectStore.create(tmp_path / "project", "P")
    value = store.manifest
    value["version"] = version
    write_json(store.project_file, value)
    with pytest.raises(ProjectError) as error:
        ProjectStore.open(store.project_file)
    assert error.value.code == "unsupported_version"


@pytest.mark.parametrize("bad_path", ["../escape.csrc", "/absolute.csrc", "objects/../escape.csrc",
                                       "C:/outside.csrc", "objects/x/CON.csrc", "objects/x/file.csrc "])
def test_manifest_path_escapes_are_rejected(tmp_path, bad_path):
    source = tmp_path / "source.csrc"
    write_json(source, {"schema_version": 2, "kind": "point"})
    store = ProjectStore.create(tmp_path / "project", "P")
    store.import_document(source)
    value = store.manifest
    value["documents"][0]["path"] = bad_path
    write_json(store.project_file, value)
    with pytest.raises(ProjectError) as error:
        ProjectStore.open(store.project_file)
    assert error.value.code in ("unsafe_path", "invalid_manifest")


def test_links_and_link_ancestors_rejected_for_import_and_project_reads(tmp_path):
    source = tmp_path / "source.csrc"
    write_json(source, {"schema_version": 2, "kind": "point"})
    link = tmp_path / "linked.csrc"
    link.symlink_to(source)
    store = ProjectStore.create(tmp_path / "project", "P")
    with pytest.raises(ProjectError) as error:
        store.import_document(link)
    assert error.value.code == "unsafe_path"
    identity = store.import_document(source)
    row = store.manifest["documents"][0]
    managed = store.root / row["path"]
    managed.unlink()
    managed.symlink_to(source)
    with pytest.raises(ProjectError) as error:
        store.resolve_document(identity)
    assert error.value.code == "unsafe_path"
    root_link = tmp_path / "root-link"
    root_link.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(ProjectError):
        ProjectStore.open(root_link / MANIFEST_FILENAME)


def test_missing_conflicting_and_tampered_default_dependencies_fail_without_fallback(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    source = by_kind(store.resolve_default(), "source")
    target = store.root / source["path"]
    target.write_text('{"schema_version":2,"kind":"point"}')
    with pytest.raises(ProjectError) as error:
        ProjectStore.open(store.project_file)
    assert error.value.code == "integrity_error"
    target.unlink()
    with pytest.raises(ProjectError) as error:
        ProjectStore.open(store.project_file)
    assert error.value.code == "missing_dependency"


def test_manifest_dependency_id_disagreement_is_not_silent(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    value = store.manifest
    machine = next(row for row in value["documents"] if row["kind"] == "machine")
    machine["dependencies"] = []
    write_json(store.project_file, value)
    with pytest.raises(ProjectError) as error:
        ProjectStore.open(store.project_file)
    assert error.value.code == "dependency_conflict"


def test_stale_writer_and_explicit_writer_lock_preserve_state(tmp_path):
    source = tmp_path / "source.csrc"
    write_json(source, {"schema_version": 2, "kind": "point"})
    first = ProjectStore.create(tmp_path / "project", "P")
    stale = ProjectStore.open(first.project_file)
    first.import_document(source)
    before = file_snapshot(first.root)
    with pytest.raises(ProjectError) as error:
        stale.import_document(source)
    assert error.value.code == "stale_project"
    assert file_snapshot(first.root) == before
    (first.root / ".project-write.lock").write_text("interrupted writer")
    with pytest.raises(ProjectError) as error:
        first.import_document(source)
    assert error.value.code == "project_locked"
    assert (first.root / ".project-write.lock").read_text() == "interrupted writer"


def test_failed_atomic_publication_preserves_manifest_and_no_staged_files(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    before = file_snapshot(store.root)
    row = by_kind(store.resolve_default(), "source")
    row["data"]["source_exponent"] = 8.
    with patch("coat_sim.core.project_store.replace_with_retry", side_effect=PermissionError("test lock")):
        with pytest.raises(ProjectError) as error:
            store.save_document(row["id"], row["data"])
    assert error.value.code == "io_error"
    assert file_snapshot(store.root) == before
    assert not (store.root / ".project-write.lock").exists()


def test_film_material_closure_workflow_roundtrip_and_explicit_export(tmp_path):
    path = tmp_path / "design.cfilm"
    design = film(path)
    store = ProjectStore.create(tmp_path / "project", "Film project")
    with pytest.raises(ProjectError) as error:
        store.import_document(path)
    assert error.value.code == "missing_dependency"
    record = material()
    identity = store.import_document(path, materials={"film": record})
    record["name"] = "caller mutation"
    graph = store.resolve_document(identity)
    assert graph["materials"][0]["name"] == "Synthetic film"
    assert by_kind(graph, "film")["data"] == design
    path.unlink()
    exported = store.export_document(identity, tmp_path / "export")
    film_row = next(row for row in exported["documents"] if row["kind"] == "film")
    assert json.loads((tmp_path / "export" / film_row["path"]).read_text()) == design
    resource_row = next(row for row in exported["documents"] if row["kind"] == "optical_material")
    assert json.loads((tmp_path / "export" / resource_row["path"]).read_text())["material"] == graph["materials"][0]
    with pytest.raises(ProjectError) as error:
        store.export_document(identity, tmp_path / "export")
    assert error.value.code == "destination_exists"


def test_material_id_conflict_rolls_back_and_existing_project_resource_reuses_explicitly(tmp_path):
    path = tmp_path / "design.cfilm"
    film(path)
    store = ProjectStore.create(tmp_path / "project", "P")
    first = store.import_document(path, materials={"film": material()})
    second = store.import_document(path)  # existing project record is explicit local membership
    assert len([row for row in store.manifest["documents"] if row["kind"] == "optical_material"]) == 1
    assert store.resolve_document(first)["materials"] == store.resolve_document(second)["materials"]
    before = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        store.import_document(path, materials={"film": material(n=3.)})
    assert error.value.code == "dependency_conflict"
    assert file_snapshot(store.root) == before


def test_process_export_references_are_portable_for_existing_service(tmp_path):
    store, external, process_id, _ = setup_project(tmp_path)
    output = tmp_path / "export"
    exported = store.export_document(process_id, output)
    process_row = next(row for row in exported["documents"] if row["kind"] == "process")
    shutil.rmtree(external)
    from coat_sim.core.api import CoatService
    runtime = CoatService(output).load("process", output / process_row["path"]).runtime
    assert runtime is not None
    assert runtime["steps"][0]["target_nm"] == 1.
    for row in exported["documents"]:
        if row["kind"] in ("process", "machine"):
            content = json.loads((output / row["path"]).read_text())
            from coat_sim.core.project_dependencies import file_references
            for owner, field, _ in file_references(row["kind"], content):
                assert (output / owner[field]).is_file()


def test_recovery_is_explicit_identity_revision_bound_and_normal_open_ignores_corruption(tmp_path):
    store, _, process_id, _ = setup_project(tmp_path)
    assert store.recover_session() is None
    store.record_recovery([process_id])
    recovery = store.recover_session()
    assert recovery is not None
    assert recovery["document_ids"] == [process_id]
    assert recovery["graphs"][0]["root_document_id"] == process_id
    row = by_kind(store.resolve_default(), "source")
    row["data"]["source_exponent"] = 2.
    store.save_document(row["id"], row["data"])
    with pytest.raises(ProjectError) as error:
        store.recover_session()
    assert error.value.code == "stale_recovery"
    (store.root / "recovery.json").write_text("corrupt")
    assert ProjectStore.open(store.project_file).resolve_default() is not None
    with pytest.raises(ProjectError):
        store.recover_session()


def test_configuration_validation_and_wrong_root_are_atomic(tmp_path):
    store, _, process_id, _ = setup_project(tmp_path)
    before = file_snapshot(store.root)
    for parameters in ({"settings": {"grid_size": 20}}, {"backend": "unknown"},
                       {"fast_cycle": "yes"}, {"time_step_s": 1., "settings": {"time_steps": 5}},
                       {"process_path": "/global/recipe.cproc"}, {"algorithm": "bad"}):
        with pytest.raises(ProjectError):
            store.add_deposition_configuration("Bad", process_id, parameters)
        assert file_snapshot(store.root) == before
    with pytest.raises(ProjectError):
        store.set_default_configuration(process_id)
    assert file_snapshot(store.root) == before


def test_import_retains_embedded_frozen_process_film_and_source_material_snapshots(tmp_path):
    from coat_sim.core.film_design_service import simulation_settings
    from coat_sim.core.film_process_import import _sha256, build_film_process_import
    store, external, _, _ = setup_project(tmp_path)
    path = tmp_path / "film.cfilm"
    design = film(path)
    snapshot = {"kind": "coat_sim.frozen_film_design", "schema_version": 1,
                "design": design, "settings": simulation_settings({}), "materials": [material()]}
    snapshot["sha256"] = _sha256(snapshot)
    process = json.loads((external / "recipe.cproc").read_text())
    machine = store.resolve_default()["runtime"]["machine"]
    imported = build_film_process_import(snapshot, "front", {"film": 1}, machine)
    process.update({key: value for key, value in imported.items() if key != "preview"})
    process["schema_version"] = 7
    write_json(external / "recipe.cproc", process)
    from coat_sim.core.materials import default_material_profile
    source_path = external / "parts" / "source.csrc"
    source_data = json.loads(source_path.read_text())
    source_data["material"] = default_material_profile("Synthetic source material", id="snapshot-source")
    write_json(source_path, source_data)
    identity = store.import_document(external / "recipe.cproc")
    captured = by_kind(store.resolve_document(identity), "process")["data"]
    assert captured["film_design_snapshot"] == snapshot
    assert captured["film_design_import"] == imported["film_design_import"]
    assert by_kind(store.resolve_document(identity), "source")["data"]["material"]["id"] == "snapshot-source"
    assert not store.resolve_document(identity)["materials"]  # embedded, no fabricated live linkage


def test_saved_machine_can_bind_an_explicitly_imported_project_source(tmp_path):
    store, external, _, _ = setup_project(tmp_path)
    new_path = external / "another.csrc"
    write_json(new_path, {"schema_version": 2, "kind": "point", "source_exponent": 9.})
    identity = store.import_document(new_path)
    machine = by_kind(store.resolve_default(), "machine")
    new_source = store.resolve_document(identity)["documents"][0]
    machine["data"]["sources"][0]["file"] = new_source["path"].split("/")[-1]
    store.save_document(machine["id"], machine["data"])
    runtime = ProjectStore.open(store.project_file).resolve_default()["runtime"]
    assert [source["profile"]["source_exponent"] for source in runtime["machine"]["sources"]] == [9., 1.]
    machine["data"]["sources"][0]["file"] = str(new_path)
    with pytest.raises(ProjectError) as error:
        store.save_document(machine["id"], machine["data"])
    assert error.value.code == "unsafe_path"


def test_generation_hardlink_failure_uses_portable_copy_fallback(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    row = by_kind(store.resolve_default(), "source")
    row["data"]["source_exponent"] = 6.
    with patch("coat_sim.core.project_store.os.link", side_effect=OSError("unsupported host")):
        store.save_document(row["id"], row["data"])
    assert ProjectStore.open(store.project_file).resolve_default()["runtime"]["machine"]["sources"][0]["profile"]["source_exponent"] == 6.


def test_read_and_write_project_capacity_are_bounded(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    row = by_kind(store.resolve_default(), "source")
    before = file_snapshot(store.root)
    with patch("coat_sim.core.project_store.MAX_PROJECT_BYTES", 100):
        with pytest.raises(ProjectError) as error:
            store.resolve_default()
        assert error.value.code == "capacity_exceeded"
        with pytest.raises(ProjectError):
            store.save_document(row["id"], row["data"])
    assert file_snapshot(store.root) == before


def test_open_and_resolve_never_call_simulation_or_read_recovery(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    (store.root / "recovery.json").write_text("corrupt")
    with patch("coat_sim.core.simulator.simulate", side_effect=AssertionError("calculation started")), \
         patch("coat_sim.core.process_simulator.simulate_process", side_effect=AssertionError("calculation started")), \
         patch.object(ProjectStore, "recover_session", side_effect=AssertionError("recovery read")):
        assert ProjectStore.open(store.project_file).resolve_default()["runtime"]["errors"] == []


def test_writer_metadata_is_identifiable_and_not_auto_broken(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "P")
    with store._writer():
        owner = json.loads((store.root / ".project-write.lock").read_text())
        assert owner["project_id"] == store.manifest["id"]
        assert owner["revision"] == store.manifest["revision"]
        assert owner["pid"] == os.getpid()
        assert len(owner["writer_id"]) == 32 and owner["created_at"].endswith("+00:00")
        with pytest.raises(ProjectError) as error:
            with store._writer():
                raise AssertionError("must not break existing lock")
        assert error.value.code == "project_locked"
        assert json.loads((store.root / ".project-write.lock").read_text()) == owner
    assert not (store.root / ".project-write.lock").exists()


def test_symbol_only_and_structure_candidate_only_film_materials_are_portable(tmp_path):
    design = new_design("Symbol and candidate closure")
    design["expression"] = {"text": "A", "in_sync": False, "symbols": {
        "A": {"material_id": "symbol_only", "optical_set_id": "set",
              "thickness": {"value": 90., "unit": "nm"}, "note": "unexpanded symbol"}}}
    design["workflow_settings"] = {"optimization": {
        "version": 1, "search_mode": "structure", "structure_mode": "combined",
        "structure_materials": [{"material_id": "candidate_only", "optical_set_id": "set"}]}}
    design = validate_design(design)
    external = tmp_path / "external"
    external.mkdir()
    source = external / "draft.cfilm"
    write_json(source, design)
    store = ProjectStore.create(tmp_path / "project", "P")
    with pytest.raises(ProjectError) as error:
        store.import_document(source, materials={"symbol_only": material("symbol_only")})
    assert error.value.code == "missing_dependency"
    identity = store.import_document(source, materials={
        "symbol_only": material("symbol_only"), "candidate_only": material("candidate_only")})
    shutil.rmtree(external)
    graph = ProjectStore.open(store.project_file).resolve_document(identity)
    assert {row["id"] for row in graph["materials"]} == {"symbol_only", "candidate_only"}
    assert by_kind(graph, "film")["data"] == design
    export = store.export_document(identity, tmp_path / "export")
    film_row = next(row for row in export["documents"] if row["kind"] == "film")
    resource_records = [json.loads((tmp_path / "export" / row["path"]).read_text())["material"]
                        for row in export["documents"] if row["kind"] == "optical_material"]
    second = ProjectStore.create(tmp_path / "second", "Q")
    second_id = second.import_document(tmp_path / "export" / film_row["path"],
                                       materials={row["id"]: row for row in resource_records})
    copied = second.resolve_document(second_id)
    assert by_kind(copied, "film")["data"] == design
    assert copied["materials"] == graph["materials"]


@pytest.mark.parametrize("configuration,code", [
    ({"version": 2, "structure_materials": []}, "unsupported_version"),
    ({"structure_materials": []}, "unsupported_version"),
    ({"version": 1, "structure_materials": "global-catalog"}, "invalid_document"),
    ({"version": 1, "structure_materials": [{"material_id": "candidate", "file": "/global"}]}, "invalid_document"),
    ({"version": 1, "structure_materials": [{"material_id": "candidate", "optical_set_id": "set"}] * 2}, "dependency_conflict"),
])
def test_typed_film_candidate_references_reject_unknown_or_malformed_configuration(tmp_path, configuration, code):
    design = new_design("Bad candidate refs")
    design["workflow_settings"] = {"optimization": configuration}
    source = tmp_path / "draft.cfilm"
    write_json(source, design)
    store = ProjectStore.create(tmp_path / "project", "P")
    before = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        store.import_document(source)
    assert error.value.code == code
    assert file_snapshot(store.root) == before


def test_lock_cleanup_failure_reports_warning_without_disguising_a_committed_save(tmp_path):
    source = tmp_path / "source.csrc"
    write_json(source, {"schema_version": 2, "kind": "point"})
    store = ProjectStore.create(tmp_path / "project", "P")
    original_unlink = type(source).unlink

    def locked_unlink(path, *args, **kwargs):
        if path.name == ".project-write.lock":
            raise PermissionError("test cleanup lock")
        return original_unlink(path, *args, **kwargs)

    with patch("pathlib.Path.unlink", locked_unlink):
        identity = store.import_document(source)
    assert store.manifest["revision"] == 1
    assert ProjectStore.open(store.project_file).manifest == store.manifest
    assert store.resolve_document(identity)["documents"][0]["kind"] == "source"
    warning = store.maintenance_warnings[0]
    assert warning["code"] == "writer_lock_cleanup_failed"
    warning["code"] = "caller mutation"
    assert store.maintenance_warnings[0]["code"] == "writer_lock_cleanup_failed"
    assert (store.root / ".project-write.lock").exists()
    with pytest.raises(ProjectError) as error:
        store.import_document(source)
    assert error.value.code == "project_locked"


def test_lock_cleanup_failure_preserves_original_rollback_error(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    row = by_kind(store.resolve_default(), "source")
    row["data"]["source_exponent"] = 2.
    before = store.manifest
    original_unlink = type(store.root).unlink

    def locked_unlink(path, *args, **kwargs):
        if path.name == ".project-write.lock":
            raise PermissionError("test cleanup lock")
        return original_unlink(path, *args, **kwargs)

    with patch("pathlib.Path.unlink", locked_unlink), \
         patch("coat_sim.core.project_store.replace_with_retry", side_effect=PermissionError("publication failure")):
        with pytest.raises(ProjectError) as error:
            store.save_document(row["id"], row["data"])
    assert error.value.code == "io_error"
    assert store.manifest == before == ProjectStore.open(store.project_file).manifest
    assert store.maintenance_warnings[0]["code"] == "writer_lock_cleanup_failed"


def test_generation_collision_never_deletes_preexisting_contents(tmp_path):
    source = tmp_path / "source.csrc"
    write_json(source, {"schema_version": 2, "kind": "point"})
    store = ProjectStore.create(tmp_path / "project", "P")
    generation = "a" * 32
    existing = store.root / "objects" / generation
    existing.mkdir()
    (existing / "keep.txt").write_text("unrelated pre-existing content")
    before = file_snapshot(store.root)
    with patch("coat_sim.core.project_store._new_id", return_value=generation):
        with pytest.raises(ProjectError) as error:
            store.import_document(source)
    assert error.value.code == "io_error"
    assert file_snapshot(store.root) == before


def test_configuration_identity_collision_never_replaces_unrelated_member(tmp_path):
    store, _, process_id, _ = setup_project(tmp_path)
    unrelated = store.create_document("film", new_design("Unrelated draft"))
    before = file_snapshot(store.root)
    graph = store.resolve_document(unrelated)
    with patch("coat_sim.core.project_store._new_id", return_value=unrelated):
        with pytest.raises(ProjectError) as error:
            store.add_deposition_configuration("Must not replace draft", process_id)
    assert error.value.code == "dependency_conflict"
    assert file_snapshot(store.root) == before
    assert store.resolve_document(unrelated) == graph


def test_concurrently_created_export_directory_is_never_replaced(tmp_path):
    from coat_sim.core.project_store import _rename_directory_no_replace
    store, _, process_id, _ = setup_project(tmp_path)
    destination = tmp_path / "export"

    def target_appears(source, target):
        target.mkdir()
        _rename_directory_no_replace(source, target)

    before = file_snapshot(store.root)
    with patch("coat_sim.core.project_store._rename_directory_no_replace", target_appears):
        with pytest.raises(ProjectError) as error:
            store.export_document(process_id, destination)
    assert error.value.code == "destination_exists"
    assert destination.is_dir() and list(destination.iterdir()) == []
    assert file_snapshot(store.root) == before
    assert not list(tmp_path.glob(".project-export-*"))
