"""Contracts for a pathless draft seeded from a frozen project default film."""

from copy import deepcopy
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_service import simulation_settings
from coat_sim.core.film_process_import import _sha256, build_film_process_import
from coat_sim.core.project_default_loading import load_project_default
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionHost, ProjectStoreView
from tests.test_project_core import material, setup_project


pytestmark = pytest.mark.contract


def file_snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def frozen_film_project(root: Path, *, design_id: str = "shared-default-film",
                         material_n: float = 2.0) -> tuple[ProjectStore, dict]:
    """Make a normal configured project whose selected process embeds a film."""
    root.mkdir(parents=True, exist_ok=True)
    store, _external, process_id, _configuration_id = setup_project(root, root.name)

    design = new_design("Frozen default film", design_id)
    design["substrate"].update(material_id="film", optical_set_id="set")
    design["front_layers"] = [{
        "id": "layer", "material_id": "film", "optical_set_id": "set",
        "thickness": {"value": 100.0, "unit": "nm"}, "note": "synthetic fixture",
    }]
    design["workflow_settings"] = {"optimization": {"fixture": "preserve"}}
    design = validate_design(design)
    settings = simulation_settings({
        "wavelength_min_nm": 500.0,
        "wavelength_max_nm": 600.0,
        "samples": 5,
        "angle_deg": 0.0,
        "polarization": "s",
    })
    snapshot = {
        "kind": "coat_sim.frozen_film_design",
        "schema_version": 1,
        "design": design,
        "settings": settings,
        "materials": [material("film", material_n)],
    }
    snapshot["sha256"] = _sha256(snapshot)

    graph = store.resolve_default()
    process = deepcopy(next(row["data"] for row in graph["documents"]
                            if row["kind"] == "process"))
    imported = build_film_process_import(
        snapshot, "front", {"film": 1}, graph["runtime"]["machine"])
    process.update({key: value for key, value in imported.items() if key != "preview"})
    process["schema_version"] = 7
    store.save_document(process_id, process)
    return store, snapshot


def workspace_for(stores: list[ProjectStore], store: ProjectStore) -> ProjectFilmWorkspace:
    by_root = {item.root.resolve(): item for item in stores}
    host = ProjectSessionHost(lambda root: cast(ProjectStoreView, by_root[root.resolve()]))
    session = host.open_project(store.root)
    return ProjectFilmWorkspace.for_project(host, session.binding)


def test_initializing_frozen_default_is_pathless_dirty_and_writes_nothing(tmp_path):
    store, frozen = frozen_film_project(tmp_path / "project")
    workspace = workspace_for([store], store)
    snapshot = load_project_default(store).film_design_snapshot
    assert snapshot == frozen
    files_before = file_snapshot(store.root)
    manifest_before = store.manifest

    assert workspace.initialize_frozen_draft(snapshot)
    editor = workspace.editor
    draft = editor.snapshot()
    expected = deepcopy(snapshot["design"])
    expected["workflow_settings"]["simulation"] = snapshot["settings"]

    assert draft.design == expected
    assert draft.path is None
    assert draft.dirty
    assert editor.workflow_configuration("simulation") == snapshot["settings"]
    assert editor.workflow_configuration("optimization") == {"fixture": "preserve"}

    replacement = deepcopy(snapshot)
    replacement["design"]["name"] = "Must not replace the active draft"
    replacement["sha256"] = _sha256(replacement)
    assert not workspace.initialize_frozen_draft(replacement)
    assert editor.snapshot().design == expected
    assert store.manifest == manifest_before
    assert file_snapshot(store.root) == files_before
    assert not (store.root / "documents").exists()


def test_frozen_default_does_not_replace_an_already_edited_pristine_editor(tmp_path):
    store, frozen = frozen_film_project(tmp_path / "project")
    workspace = workspace_for([store], store)
    editor = workspace.editor
    assert editor.set_design_value("name", "Keep my current draft")
    before = editor.snapshot()
    files_before = file_snapshot(store.root)

    assert not workspace.initialize_frozen_draft(frozen)
    after = editor.snapshot()
    assert after.design == before.design
    assert after.dirty == before.dirty
    assert after.path == before.path is None
    assert file_snapshot(store.root) == files_before


def test_same_id_material_conflict_blocks_save_without_partial_writes(tmp_path):
    store, frozen = frozen_film_project(tmp_path / "project")
    existing = new_design("Existing unrelated film", "existing-project-film")
    existing["substrate"].update(material_id="film", optical_set_id="set")
    store.create_document("film", existing, materials={"film": material("film", 9.0)})
    workspace = workspace_for([store], store)
    assert workspace.initialize_frozen_draft(frozen)
    editor = workspace.editor
    simulated = workspace.service.simulate(
        editor.snapshot().design, editor.workflow_configuration("simulation"))
    assert simulated["R"] == pytest.approx([1.0 / 9.0] * 5)
    manifest_before = store.manifest
    files_before = file_snapshot(store.root)

    assert not editor.save_current()
    assert "changed after this film draft captured it" in editor.snapshot().error
    assert editor.snapshot().path is None
    assert editor.snapshot().dirty
    assert store.manifest == manifest_before
    assert file_snapshot(store.root) == files_before
    assert len([row for row in store.manifest["documents"] if row["kind"] == "film"]) == 1

    assert not editor.request_new_design()
    assert editor.resolve_pending_action(True)
    assert workspace.service.materials.get("film")["optical_sets"][0]["n"] == [9.0, 9.0]
    assert not editor.snapshot().dirty


def test_orphaned_project_material_record_does_not_break_draft_activation(tmp_path):
    store, frozen = frozen_film_project(tmp_path / "project")
    old_owner = new_design("Old material owner", "old-material-owner")
    old_owner["substrate"].update(material_id="orphan", optical_set_id="set")
    owner_id = store.create_document(
        "film", old_owner, materials={"orphan": material("orphan", 4.0)})
    # ProjectStore retains dependency documents after a film stops referencing them.
    store.save_document(owner_id, new_design("Old material owner", "old-material-owner"))
    orphan_rows = [row for row in store.manifest["documents"]
                   if row["kind"] == "optical_material"]
    assert len(orphan_rows) == 1

    workspace = workspace_for([store], store)
    files_before = file_snapshot(store.root)

    assert workspace.initialize_frozen_draft(frozen)
    assert workspace.editor.snapshot().design["id"] == frozen["design"]["id"]
    assert file_snapshot(store.root) == files_before


def test_invalid_frozen_default_leaves_pristine_editor_and_project_unchanged(tmp_path):
    store, frozen = frozen_film_project(tmp_path / "project")
    workspace = workspace_for([store], store)
    editor = workspace.editor
    pristine = editor.snapshot()
    before = file_snapshot(store.root)
    invalid = deepcopy(frozen)
    invalid["materials"][0]["optical_sets"][0]["n"] = [4.0, 4.0]

    assert not workspace.initialize_frozen_draft(invalid)
    assert "摘要不匹配" in editor.snapshot().error
    assert editor.snapshot().design == pristine.design
    assert editor.snapshot().path is None
    assert not editor.snapshot().dirty
    assert file_snapshot(store.root) == before


def test_failed_picker_refresh_rolls_back_frozen_material_overlay(tmp_path):
    store, frozen = frozen_film_project(tmp_path / "project")
    workspace = workspace_for([store], store)
    editor = workspace.editor
    before = editor.snapshot()
    materials_before = deepcopy(workspace.service.material_options("substrate"))
    files_before = file_snapshot(store.root)

    with patch.object(editor, "_refresh_project_materials", side_effect=OSError("read failed")):
        assert not workspace.initialize_frozen_draft(frozen)
    assert "read failed" in editor.snapshot().error
    assert editor.snapshot().design == before.design
    assert editor.snapshot().path is None
    assert not editor.snapshot().dirty
    assert workspace.service.material_options("substrate") == materials_before
    assert file_snapshot(store.root) == files_before
    assert workspace.initialize_frozen_draft(frozen)


def test_frozen_default_save_persists_one_managed_film_and_its_materials(tmp_path):
    store, frozen = frozen_film_project(tmp_path / "project")
    workspace = workspace_for([store], store)
    assert workspace.initialize_frozen_draft(frozen)
    editor = workspace.editor

    assert editor.save_current()
    saved = editor.snapshot()
    films = [row for row in store.manifest["documents"] if row["kind"] == "film"]
    materials = [row for row in store.manifest["documents"] if row["kind"] == "optical_material"]
    assert len(films) == len(materials) == 1
    assert saved.path == str(workspace.service.document_handle(films[0]["id"]))
    assert not saved.dirty
    persisted = ProjectStore.open(store.project_file).resolve_document(films[0]["id"])
    persisted_design = next(row["data"] for row in persisted["documents"]
                            if row["id"] == films[0]["id"])
    assert persisted_design == saved.design
    assert persisted["materials"][0]["optical_sets"][0]["n"] == [2.0, 2.0]
    assert persisted_design["id"] == frozen["design"]["id"]


def test_same_id_frozen_films_simulate_with_each_projects_own_values(tmp_path):
    first_store, first_frozen = frozen_film_project(
        tmp_path / "first", design_id="shared-design-id", material_n=2.0)
    second_store, second_frozen = frozen_film_project(
        tmp_path / "second", design_id="shared-design-id", material_n=3.0)
    stores = [first_store, second_store]
    first = workspace_for(stores, first_store)
    second = workspace_for(stores, second_store)

    assert first.initialize_frozen_draft(first_frozen)
    assert second.initialize_frozen_draft(second_frozen)
    left_design = first.editor.snapshot().design
    right_design = second.editor.snapshot().design
    settings = first.editor.workflow_configuration("simulation")
    left = first.service.simulate(left_design, settings)
    right = second.service.simulate(
        right_design, second.editor.workflow_configuration("simulation"))

    assert left_design["id"] == right_design["id"] == "shared-design-id"
    assert left["R"] == pytest.approx([1.0 / 9.0] * 5)
    assert right["R"] == pytest.approx([0.25] * 5)
    assert first_store.manifest["documents"] != second_store.manifest["documents"]
    assert not [path for store in stores for path in store.root.rglob("*.sqlite3")]
