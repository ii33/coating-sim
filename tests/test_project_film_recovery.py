"""Headless recovery contracts for project-owned film editor drafts."""

from copy import deepcopy
from pathlib import Path

import pytest

from coat_sim.core.film_design import validate_design
from coat_sim.core.film_design_session import AIR_MATERIAL_ID, AIR_OPTICAL_SET_ID
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_film_recovery import (
    film_recovery_current_token,
    restore_film_recovery,
    snapshot_film_recovery,
    validate_film_recovery,
)
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionHost
from tests.test_project_film_service import bound_design, material


pytestmark = pytest.mark.contract


def file_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def workspace_at(root: Path, label: str = "Project"):
    store = ProjectStore.create(root, label)
    host = ProjectSessionHost(lambda _root: store)
    binding = host.open_project(store.root).binding
    return ProjectFilmWorkspace.for_project(host, binding), store


def complete_design() -> tuple[dict, dict]:
    design = bound_design()
    design["front_layers"] = [{
        "id": "front-layer", "material_id": "shared", "optical_set_id": "visible",
        "thickness": {"value": 80.0, "unit": "nm"}, "note": "front",
    }]
    design["expression"] = {
        "text": "H * 2", "in_sync": False,
        "symbols": {"H": {
            "material_id": "shared", "optical_set_id": "visible",
            "thickness": {"value": 90.0, "unit": "nm"}, "note": "symbol",
        }},
    }
    design["workflow_settings"]["optimization"]["structure_materials"] = [
        {"material_id": "shared", "optical_set_id": "visible"},
    ]
    design = validate_design(design)
    record = material()
    second_set = deepcopy(record["optical_sets"][0])
    second_set.update(id="infrared", name="Synthetic infrared",
                      wavelength_min=700.0, wavelength_max=1200.0,
                      wavelengths=[700.0, 1200.0])
    record["optical_sets"].append(second_set)
    return design, record


def prepare_dirty_saved_film(workspace: ProjectFilmWorkspace):
    design, record = complete_design()
    saved = workspace.service.create_film(design, materials={"shared": record})
    assert workspace.editor.load_saved_design(saved.document_id)
    assert workspace.editor.set_design_value("name", "Recovered draft")
    # Retain the exact editor spelling in the separate recovery expression field.
    assert workspace.editor.set_expression_text("h * 2")
    return saved


def test_snapshot_is_detached_complete_and_read_only(tmp_path):
    workspace, store = workspace_at(tmp_path / "project")
    saved = prepare_dirty_saved_film(workspace)
    before_files = file_bytes(store.root)
    before_manifest = store.manifest

    snapshot = snapshot_film_recovery(workspace)
    validated = validate_film_recovery(workspace, snapshot)

    assert snapshot["project_id"] == workspace.binding.project_id
    assert snapshot["dirty"] is True
    assert snapshot["managed_document_id"] == saved.document_id
    assert snapshot["managed_document_revision"] == saved.revision
    assert snapshot["expression_text"] == "h * 2"
    assert validated["film"]["design"]["expression"]["text"] == "H * 2"
    assert {row["id"] for row in validated["film"]["materials"][0]["optical_sets"]} == {
        "visible", "infrared",
    }
    assert before_manifest == store.manifest
    assert before_files == file_bytes(store.root)

    detached = deepcopy(snapshot)
    detached["film"]["design"]["name"] = "mutated external copy"
    assert workspace.editor.snapshot().design["name"] == "Recovered draft"


def test_blank_project_and_incomplete_symbol_are_recoverable_without_air_fallback(tmp_path):
    workspace, _store = workspace_at(tmp_path / "empty-project")
    editor = workspace.editor
    assert editor.set_design_value("name", "An intentionally incomplete draft")
    assert editor.set_expression_text("H")

    recovery = snapshot_film_recovery(workspace)
    checked = validate_film_recovery(workspace, recovery)
    design = checked["film"]["design"]
    # New drafts bind the shipped Air medium, but incomplete substrate/symbol
    # references must remain blank rather than being silently replaced by Air.
    assert [row["id"] for row in checked["film"]["materials"]] == [AIR_MATERIAL_ID]
    air = {"kind": "material", "material_id": AIR_MATERIAL_ID,
           "optical_set_id": AIR_OPTICAL_SET_ID}
    assert design["incident_medium"] == design["exit_medium"] == air
    assert design["substrate"]["material_id"] == ""
    assert design["substrate"]["optical_set_id"] == ""
    assert design["expression"]["symbols"]["H"]["material_id"] == ""
    assert checked["expression_text"] == "H"

    assert editor.set_design_value("name", "A newer active draft")
    token = film_recovery_current_token(workspace)
    assert restore_film_recovery(
        workspace, recovery, approved=True,
        expected_current_design_revision=token["design_revision"],
        expected_current_session_revision=token["session_revision"],
        expected_current_document_id=token["managed_document_id"],
        expected_current_document_revision=token["managed_document_revision"],
    )
    restored = editor.snapshot()
    assert restored.design["name"] == "An intentionally incomplete draft"
    assert restored.design["expression"]["symbols"]["H"]["material_id"] == ""
    assert restored.path is None and restored.dirty


def test_nonempty_unresolved_material_reference_is_not_treated_as_blank(tmp_path):
    workspace, _store = workspace_at(tmp_path / "empty-project")
    editor = workspace.editor
    assert editor.add_layer("front")
    layer_id = editor.snapshot().design["front_layers"][0]["id"]
    assert editor.set_layer_value("front", layer_id, "material_id", "missing-material")
    assert editor.set_layer_value("front", layer_id, "optical_set_id", "visible")

    with pytest.raises(ProjectError) as error:
        snapshot_film_recovery(workspace)
    assert error.value.code == "missing_dependency"


def test_validation_rejects_wrong_project_bad_checksum_and_missing_closure(tmp_path):
    first, _store = workspace_at(tmp_path / "first")
    prepare_dirty_saved_film(first)
    snapshot = snapshot_film_recovery(first)
    second, _other_store = workspace_at(tmp_path / "second")

    with pytest.raises(ProjectError, match="different project"):
        validate_film_recovery(second, snapshot)

    malformed = deepcopy(snapshot)
    malformed["expression_text"] = "changed"
    with pytest.raises(ProjectError, match="checksum"):
        validate_film_recovery(first, malformed)

    incomplete = deepcopy(snapshot)
    incomplete["film"]["materials"] = []
    incomplete["sha256"] = "0" * 64
    from coat_sim.core.project_film_recovery import _digest
    incomplete["sha256"] = _digest(incomplete)
    with pytest.raises(ProjectError, match="closure"):
        validate_film_recovery(first, incomplete)


def test_validation_rejects_stale_saved_source_revision(tmp_path):
    workspace, _store = workspace_at(tmp_path / "project")
    saved = prepare_dirty_saved_film(workspace)
    recovery = snapshot_film_recovery(workspace)
    newer = deepcopy(saved.design)
    newer["note"] = "changed on disk"
    workspace.service.save_document(saved.document_id, newer,
                                    expected_revision=saved.revision)

    with pytest.raises(ProjectError, match="changed"):
        validate_film_recovery(workspace, recovery)


def test_restore_is_approved_revision_guarded_unsaved_and_never_overwrites_source(tmp_path):
    workspace, store = workspace_at(tmp_path / "project")
    saved = prepare_dirty_saved_film(workspace)
    recovery = snapshot_film_recovery(workspace)
    assert workspace.editor.set_design_value("name", "Draft user reviewed")
    current = film_recovery_current_token(workspace)
    before_files = file_bytes(store.root)
    before_manifest = store.manifest

    assert not restore_film_recovery(
        workspace, recovery, approved=False,
        expected_current_design_revision=current["design_revision"],
        expected_current_session_revision=current["session_revision"],
        expected_current_document_id=current["managed_document_id"],
        expected_current_document_revision=current["managed_document_revision"],
    )
    assert workspace.editor.snapshot().design["name"] == "Draft user reviewed"

    assert restore_film_recovery(
        workspace, recovery, approved=True,
        expected_current_design_revision=current["design_revision"],
        expected_current_session_revision=current["session_revision"],
        expected_current_document_id=current["managed_document_id"],
        expected_current_document_revision=current["managed_document_revision"],
    )
    restored = workspace.editor.snapshot()
    assert restored.design["name"] == "Recovered draft"
    assert restored.design["expression"]["text"] == "h * 2"
    assert restored.path is None
    assert restored.dirty
    assert saved.document_id in restored.message
    assert "新建项目膜系文档" in restored.message
    assert before_files == file_bytes(store.root)
    assert before_manifest == store.manifest

    assert workspace.editor.save_current()
    films = [row for row in store.manifest["documents"] if row["kind"] == "film"]
    assert len(films) == 2
    assert workspace.service.load_document(saved.document_id).design["name"] == saved.design["name"]


def test_restore_rejects_stale_live_revision_and_pending_expression_work(tmp_path):
    workspace, _store = workspace_at(tmp_path / "project")
    prepare_dirty_saved_film(workspace)
    recovery = snapshot_film_recovery(workspace)

    old_token = film_recovery_current_token(workspace)
    assert workspace.editor.set_workflow_configuration("simulation", {"samples": 23})
    after_edit = workspace.editor.snapshot()
    assert after_edit.design_revision == old_token["design_revision"]
    assert not restore_film_recovery(
        workspace, recovery, approved=True,
        expected_current_design_revision=old_token["design_revision"],
        expected_current_session_revision=old_token["session_revision"],
        expected_current_document_id=old_token["managed_document_id"],
        expected_current_document_revision=old_token["managed_document_revision"],
    )
    assert workspace.editor.workflow_configuration("simulation")["samples"] == 23

    current = film_recovery_current_token(workspace)
    assert workspace.editor.expand_expression() is not None
    assert not restore_film_recovery(
        workspace, recovery, approved=True,
        expected_current_design_revision=current["design_revision"],
        expected_current_session_revision=current["session_revision"],
        expected_current_document_id=current["managed_document_id"],
        expected_current_document_revision=current["managed_document_revision"],
    )

