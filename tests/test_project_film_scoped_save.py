"""Film saves preserve unrelated obsolete source bytes without accepting them."""

from copy import deepcopy
import hashlib
from unittest.mock import patch

import pytest

from coat_sim.core.film_design import validate_design
from coat_sim.core.project_dependencies import ProjectError, json_bytes, normalize_document
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_store import ProjectStore
from tests.design_test_fixtures import _RECTANGULAR_SOURCE
from tests.test_asset_catalog_diagnostics import obsolete_source
from tests.test_project_core import setup_project, file_snapshot
from tests.test_project_film_service import bound_design, material, EmptyCatalog

pytestmark = pytest.mark.contract


def legacy_project(tmp_path):
    store, _external, process, configuration = setup_project(tmp_path)
    source = store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    design = bound_design()
    design["front_layers"] = [
        dict(
            id=f"layer-{index}",
            material_id="shared",
            optical_set_id="visible",
            thickness=dict(value=100.0 + index, unit="nm"),
            note="Synthetic saved stack",
        )
        for index in range(86)
    ]
    service = ProjectFilmService(store, global_material_library=EmptyCatalog())
    film = service.create_film(validate_design(design), materials={"shared": material()})
    manifest = store.manifest
    row = next(r for r in manifest["documents"] if r["id"] == source)
    path = store.root / row["path"]
    # Self-contained historical input fixture, not a migration or user file edit.
    raw = json_bytes(obsolete_source())
    path.write_bytes(raw)
    row["sha256"] = hashlib.sha256(raw).hexdigest()
    store.project_file.write_bytes(json_bytes(manifest))
    opened = ProjectStore.open(store.project_file)
    return (
        opened,
        ProjectFilmService(opened, global_material_library=EmptyCatalog()),
        film.document_id,
        source,
        process,
        configuration,
        raw,
    )


def test_existing_86_layer_film_save_does_not_revalidate_or_rewrite_unrelated_source(tmp_path):
    store, service, film_id, source, process, configuration, raw = legacy_project(tmp_path)
    original = store.manifest
    untouched = {r["id"]: deepcopy(r) for r in original["documents"] if r["kind"] != "film"}
    before = file_snapshot(store.root)
    current = service.load_document(film_id)
    changed = deepcopy(current.design)
    changed["front_layers"][42]["thickness"]["value"] = 172.125
    saved = service.save_document(film_id, changed, expected_revision=current.revision)
    assert saved.design == changed and len(saved.design["front_layers"]) == 86
    assert {r["id"]: r for r in store.manifest["documents"] if r["id"] in untouched} == untouched
    assert store.manifest["default_configuration_id"] == configuration
    for r in untouched.values():
        assert (store.root / r["path"]).read_bytes() == before[r["path"]]
    assert (store.root / untouched[source]["path"]).read_bytes() == raw
    reloaded = ProjectFilmService(
        ProjectStore.open(store.project_file), global_material_library=EmptyCatalog()
    )
    assert reloaded.load_document(film_id).design == changed
    with pytest.raises(ProjectError, match="aperture_kind"):
        store.resolve_document(source)
    with pytest.raises(ValueError, match="aperture_kind"):
        normalize_document("source", obsolete_source())


def test_new_film_can_be_saved_without_migrating_unrelated_legacy_source(tmp_path):
    store, service, _film_id, source, _process, _configuration, raw = legacy_project(tmp_path)
    row = next(r for r in store.manifest["documents"] if r["id"] == source)
    new = service.create_film(bound_design(), materials={"shared": material()})
    assert service.load_document(new.document_id).design == new.design
    assert next(r for r in store.manifest["documents"] if r["id"] == source) == row
    assert (store.root / row["path"]).read_bytes() == raw


def test_scoped_film_save_retains_strict_changed_data_and_material_validation(tmp_path):
    store, service, film_id, _source, _process, _configuration, _raw = legacy_project(tmp_path)
    current = service.load_document(film_id)
    for mutation in ("unknown", "missing_material", "missing_set"):
        changed = deepcopy(current.design)
        if mutation == "unknown":
            changed["unknown_saved_field"] = True
        elif mutation == "missing_material":
            changed["front_layers"][0]["material_id"] = "absent"
        else:
            changed["front_layers"][0]["optical_set_id"] = "absent"
        before = file_snapshot(store.root)
        with pytest.raises(ValueError):
            service.save_document(film_id, changed, expected_revision=current.revision)
        assert file_snapshot(store.root) == before


def test_scoped_film_save_commit_failure_keeps_original_files_and_input(tmp_path):
    store, service, film_id, _source, _process, _configuration, _raw = legacy_project(tmp_path)
    current = service.load_document(film_id)
    changed = deepcopy(current.design)
    changed["front_layers"][0]["thickness"]["value"] = 117.0
    copy = deepcopy(changed)
    before = file_snapshot(store.root)
    with patch(
        "coat_sim.core.project_store.replace_with_retry",
        side_effect=PermissionError("synthetic descriptor failure"),
    ):
        with pytest.raises(ProjectError, match="transaction"):
            service.save_document(film_id, changed, expected_revision=current.revision)
    assert changed == copy and file_snapshot(store.root) == before
    assert service.load_document(film_id).design == current.design


def test_film_scope_does_not_ignore_invalid_referenced_optical_material(tmp_path):
    store, service, film_id, _source, _process, _configuration, _raw = legacy_project(tmp_path)
    current = service.load_document(film_id)
    manifest = store.manifest
    row = next(row for row in manifest["documents"] if row["kind"] == "optical_material")
    document = store.resolve_document(row["id"])["documents"][0]["data"]
    document["unknown_material_field"] = True
    raw = json_bytes(document)
    (store.root / row["path"]).write_bytes(raw)
    row["sha256"] = hashlib.sha256(raw).hexdigest()
    store.project_file.write_bytes(json_bytes(manifest))
    current_store = ProjectStore.open(store.project_file)
    before = file_snapshot(store.root)
    with pytest.raises(ProjectError, match="material|光学|字段"):
        current_store.save_film_document(
            current.design, document_id=film_id, expected_revision=current.revision
        )
    assert file_snapshot(store.root) == before


def test_film_scope_rejects_an_obsolete_source_in_its_declared_dependency_closure(tmp_path):
    store, service, film_id, source, _process, _configuration, _raw = legacy_project(tmp_path)
    current = service.load_document(film_id)
    manifest = store.manifest
    row = next(row for row in manifest["documents"] if row["id"] == film_id)
    row["dependencies"].append(source)
    store.project_file.write_bytes(json_bytes(manifest))
    current_store = ProjectStore.open(store.project_file)
    before = file_snapshot(store.root)
    with pytest.raises(ProjectError, match="aperture_kind"):
        current_store.save_film_document(
            current.design, document_id=film_id, expected_revision=current.revision
        )
    assert file_snapshot(store.root) == before


def test_film_scope_keeps_ordinary_source_creation_and_default_open_strict(tmp_path):
    store, _service, _film_id, source, process, _configuration, _raw = legacy_project(tmp_path)
    before = file_snapshot(store.root)
    with pytest.raises(ProjectError, match="aperture_kind"):
        store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    assert file_snapshot(store.root) == before
    # An opening/default process still resolves all of its own sources strictly.
    assert store.resolve_document(process)["root_document_id"] == process
    with pytest.raises(ProjectError, match="aperture_kind"):
        store.resolve_document(source)


def test_film_scope_noop_preserves_manifest_revision_and_every_byte(tmp_path):
    store, service, film_id, _source, _process, _configuration, _raw = legacy_project(tmp_path)
    current = service.load_document(film_id)
    before = file_snapshot(store.root)
    saved = service.save_document(film_id, current.design, expected_revision=current.revision)
    assert saved == current
    assert file_snapshot(store.root) == before


def test_active_store_can_save_film_while_its_unrelated_default_source_is_obsolete(tmp_path):
    store, _external, process, configuration = setup_project(tmp_path)
    source = next(row["id"] for row in store.manifest["documents"] if row["kind"] == "source")
    store.save_document(source, deepcopy(_RECTANGULAR_SOURCE))
    service = ProjectFilmService(store, global_material_library=EmptyCatalog())
    film = service.create_film(bound_design(), materials={"shared": material()})
    manifest = store.manifest
    row = next(row for row in manifest["documents"] if row["id"] == source)
    raw = json_bytes(obsolete_source())
    (store.root / row["path"]).write_bytes(raw)
    row["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest_bytes = json_bytes(manifest)
    store.project_file.write_bytes(manifest_bytes)
    # Model an already-admitted historical descriptor, not a tolerant opener.
    # Normal project opening is separately and deliberately still rejected.
    active = ProjectStore(
        store.root,
        manifest,
        hashlib.sha256(manifest_bytes).hexdigest(),
        project_file=store.project_file,
    )
    editor_service = ProjectFilmService(active, global_material_library=EmptyCatalog())
    changed = deepcopy(film.design)
    changed["note"] = "Preserve latest unsaved work without migrating the source"
    saved = editor_service.save_document(film.document_id, changed, expected_revision=film.revision)
    assert editor_service.load_document(film.document_id).design == changed == saved.design
    assert active.manifest["default_configuration_id"] == configuration
    assert (active.root / row["path"]).read_bytes() == raw
    with pytest.raises(ProjectError, match="aperture_kind"):
        active.resolve_document(process)
    with pytest.raises(ProjectError, match="aperture_kind"):
        ProjectStore.open(active.project_file)


def test_changed_film_can_capture_new_material_without_rewriting_existing_materials(tmp_path):
    store, service, film_id, _source, _process, _configuration, _raw = legacy_project(tmp_path)
    current = service.load_document(film_id)
    existing = {
        row["id"]: deepcopy(row)
        for row in store.manifest["documents"]
        if row["kind"] == "optical_material"
    }
    before = file_snapshot(store.root)
    changed = deepcopy(current.design)
    changed["front_layers"][0]["material_id"] = "new-material"
    saved = service.save_document(
        film_id,
        changed,
        expected_revision=current.revision,
        materials={"new-material": material("new-material")},
    )
    graph = store.resolve_document(saved.document_id)
    assert {record["id"] for record in graph["materials"]} == {"shared", "new-material"}
    assert saved.design == changed
    for identity, row in existing.items():
        assert next(item for item in store.manifest["documents"] if item["id"] == identity) == row
        assert (store.root / row["path"]).read_bytes() == before[row["path"]]


def test_retained_file_read_errors_are_project_errors_and_do_not_commit(tmp_path):
    store, service, film_id, source, _process, _configuration, _raw = legacy_project(tmp_path)
    current = service.load_document(film_id)
    changed = deepcopy(current.design)
    changed["note"] = "Must stay unsaved"
    target = store.root / next(
        row["path"] for row in store.manifest["documents"] if row["id"] == source
    )
    before = file_snapshot(store.root)
    from pathlib import Path

    original = Path.open

    def failed_read(path, *args, **kwargs):
        if path == target:
            raise PermissionError("Synthetic fixture read failure")
        return original(path, *args, **kwargs)

    with patch.object(Path, "open", failed_read):
        with pytest.raises(ProjectError) as error:
            service.save_document(film_id, changed, expected_revision=current.revision)
    assert error.value.code == "io_error"
    assert file_snapshot(store.root) == before


def test_retained_nonordinary_file_metadata_is_rejected_before_read(tmp_path):
    import os
    from pathlib import Path
    import stat

    store, service, film_id, source, _process, _configuration, _raw = legacy_project(tmp_path)
    current = service.load_document(film_id)
    changed = deepcopy(current.design)
    changed["note"] = "Must not read nonordinary metadata"
    target = store.root / next(
        row["path"] for row in store.manifest["documents"] if row["id"] == source
    )
    before = file_snapshot(store.root)
    original = Path.lstat

    def nonordinary_metadata(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == target:
            fields = list(info)
            fields[0] = stat.S_IFDIR | 0o700
            return os.stat_result(fields)
        return info

    # Only ordinary, self-created files exist. This mock changes metadata, not
    # filesystem objects; no special file or directory-replacement test runs.
    with patch.object(Path, "lstat", nonordinary_metadata):
        with pytest.raises(ProjectError) as error:
            service.save_document(film_id, changed, expected_revision=current.revision)
    assert error.value.code == "unsafe_path"
    assert file_snapshot(store.root) == before
