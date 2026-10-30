"""Regression tests for the full material closure of editable frozen films."""

from copy import deepcopy
from typing import cast

import pytest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_process_import import (
    _sha256,
    build_film_process_import,
    freeze_complete_design,
    preview_film_design_import,
    validate_frozen_design_snapshot,
)
from coat_sim.core.project_dependencies import ProjectError, document_material_references
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionHost, ProjectStoreView
from tests.test_project_core import setup_project
from tests.test_project_film_service import material


pytestmark = pytest.mark.contract


def _material_with_sets(identity: str, *set_ids: str, n: float = 2.0) -> dict:
    base = material(identity, n)
    template = base["optical_sets"][0]
    optical_sets = []
    for index, set_id in enumerate(set_ids):
        optical_set = deepcopy(template)
        optical_set["id"] = set_id
        optical_set["name"] = f"Synthetic {identity} {set_id}"
        optical_set["n"] = [n + index * 0.1, n + index * 0.1]
        optical_sets.append(optical_set)
    base["optical_sets"] = optical_sets
    return base


def editable_design_and_materials() -> tuple[dict, dict[str, dict]]:
    """A film with distinct roles plus two datasets on one shared material."""
    design = new_design("Complete editable closure", "complete-editable-closure")
    design["first_surface"] = "front"
    design["substrate"].update(material_id="substrate", optical_set_id="substrate-bulk",
                              sides="single")
    design["front_layers"] = [{
        "id": "front-active", "material_id": "shared-face", "optical_set_id": "front-film",
        "thickness": {"value": 100.0, "unit": "nm"}, "note": "active face",
    }]
    design["back_layers"] = [{
        "id": "back-inactive", "material_id": "shared-face", "optical_set_id": "back-film",
        "thickness": {"value": 80.0, "unit": "nm"}, "note": "inactive face",
    }]
    design["incident_medium"] = {
        "kind": "material", "material_id": "incident-medium", "optical_set_id": "incident-n",
    }
    design["exit_medium"] = {
        "kind": "material", "material_id": "exit-medium", "optical_set_id": "exit-n",
    }
    design["expression"] = {
        "text": "", "in_sync": False,
        "symbols": {"U": {
            "material_id": "unused-symbol", "optical_set_id": "symbol-n",
            "thickness": {"value": 75.0, "unit": "nm"}, "note": "not expanded",
        }},
    }
    design["workflow_settings"] = {
        "simulation": {
            "view_preferences": {"tab": "advanced", "axes": ["wavelength", "reflectance"]},
            "extension_metadata": {"source": "synthetic", "enabled": True},
        },
        "optimization": {
            "version": 2,
            "targets": [],
            "structure_materials": [{
                "material_id": "optimization-candidate", "optical_set_id": "candidate-n",
            }],
            "extension_metadata": {"labels": ["candidate", {"priority": 2}]},
        },
    }
    design = validate_design(design)
    materials = {
        "substrate": _material_with_sets("substrate", "substrate-bulk", n=1.5),
        "shared-face": _material_with_sets("shared-face", "front-film", "back-film", n=2.0),
        "incident-medium": _material_with_sets("incident-medium", "incident-n", n=1.0),
        "exit-medium": _material_with_sets("exit-medium", "exit-n", n=1.1),
        "unused-symbol": _material_with_sets("unused-symbol", "symbol-n", n=1.7),
        "optimization-candidate": _material_with_sets("optimization-candidate", "candidate-n", n=2.2),
    }
    return design, materials


def _file_snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _workspace_for(store: ProjectStore) -> ProjectFilmWorkspace:
    host = ProjectSessionHost(lambda _root: cast(ProjectStoreView, store))
    binding = host.open_project(store.root).binding
    return ProjectFilmWorkspace.for_project(host, binding)


def _seed_service(tmp_path, *, with_default_process=False, design_ready_state="absent",
                  with_unused_dataset=False):
    if with_default_process:
        store, _external, _process_id, _configuration_id = setup_project(tmp_path)
    else:
        store = ProjectStore.create(tmp_path / "project", "Project")
    service = ProjectFilmService(store)
    design, materials = editable_design_and_materials()
    if with_unused_dataset:
        unused = _material_with_sets("substrate", "unused-substrate")["optical_sets"][0]
        materials["substrate"]["optical_sets"].append(unused)
    if design_ready_state != "absent":
        ready_value = {"null": None, "false": False, "true": True}[design_ready_state]
        for record in materials.values():
            record["design_ready"] = ready_value
    service.create_film(design, materials=materials)
    return store, service, design, materials


def _snapshot_material_pairs(snapshot):
    return {(row["id"], optical_set["id"])
            for row in snapshot["materials"] for optical_set in row["optical_sets"]}


class _SyntheticMaterialProvider:
    """Minimal no-global provider for malformed unreferenced-set coverage."""

    def __init__(self, records: list[dict]) -> None:
        self._values = deepcopy(records)

    def _records(self) -> list[dict]:
        return deepcopy(self._values)

    def get(self, material_id: str) -> dict:
        return deepcopy(next(row for row in self._values if row["id"] == material_id))


def test_freeze_complete_design_captures_full_editable_closure_and_preserves_inputs(tmp_path):
    store, service, design, materials = _seed_service(tmp_path)
    original_design, original_materials = deepcopy(design), deepcopy(materials)
    manifest_before = store.manifest
    files_before = _file_snapshot(store.root)

    snapshot = freeze_complete_design(service, design)

    expected_pairs = set(document_material_references("film", design))
    assert expected_pairs == {
        ("substrate", "substrate-bulk"),
        ("shared-face", "front-film"),
        ("shared-face", "back-film"),
        ("incident-medium", "incident-n"),
        ("exit-medium", "exit-n"),
        ("unused-symbol", "symbol-n"),
        ("optimization-candidate", "candidate-n"),
    }
    assert _snapshot_material_pairs(snapshot) == expected_pairs
    assert len([row for row in snapshot["materials"] if row["id"] == "shared-face"]) == 1
    face = next(row for row in snapshot["materials"] if row["id"] == "shared-face")
    assert {row["id"] for row in face["optical_sets"]} == {"front-film", "back-film"}
    assert snapshot["design"]["workflow_settings"] == design["workflow_settings"]
    assert design == original_design
    assert materials == original_materials
    assert store.manifest == manifest_before
    assert _file_snapshot(store.root) == files_before


def test_complete_snapshot_initializes_pathless_draft_and_first_save_keeps_closure(tmp_path):
    _source_store, service, design, _materials = _seed_service(tmp_path / "source")
    snapshot = freeze_complete_design(service, design)
    store = ProjectStore.create(tmp_path / "target", "Target")
    workspace = _workspace_for(store)
    expected_pairs = set(document_material_references("film", design))
    manifest_before = store.manifest
    files_before = _file_snapshot(store.root)

    assert workspace.initialize_frozen_draft(snapshot)
    editor = workspace.editor
    draft = editor.snapshot()
    assert draft.path is None
    assert draft.dirty
    assert draft.design["workflow_settings"]["optimization"] == design["workflow_settings"]["optimization"]
    assert store.manifest == manifest_before
    assert _file_snapshot(store.root) == files_before
    assert editor.save_current(), editor.snapshot().error

    saved = editor.snapshot()
    assert saved.path is not None
    assert not saved.dirty
    film_row = next(row for row in store.manifest["documents"]
                    if row["kind"] == "film" and row["id"] in saved.path)
    graph = ProjectStore.open(store.project_file).resolve_document(film_row["id"])
    saved_pairs = _snapshot_material_pairs({"materials": graph["materials"]})
    assert saved_pairs == expected_pairs
    saved_design = next(row["data"] for row in graph["documents"]
                        if row["id"] == film_row["id"])
    assert set(document_material_references("film", saved_design)) == saved_pairs
    assert saved_design["workflow_settings"]["optimization"] == design["workflow_settings"]["optimization"]


@pytest.mark.parametrize("design_ready_state", ["absent", "null", "false", "true"])
def test_same_project_frozen_snapshot_save_preserves_optional_material_design_ready(
        tmp_path, design_ready_state):
    store, service, design, _materials = _seed_service(
        tmp_path, design_ready_state=design_ready_state)
    snapshot = freeze_complete_design(service, design)
    workspace = _workspace_for(store)

    assert workspace.initialize_frozen_draft(snapshot)
    assert workspace.editor.save_current(), workspace.editor.snapshot().error
    saved = workspace.editor.snapshot()
    assert saved.path is not None
    film_id = next(row["id"] for row in store.manifest["documents"]
                   if row["kind"] == "film" and row["id"] in saved.path)
    graph = ProjectStore.open(store.project_file).resolve_document(film_id)

    expected_presence = design_ready_state != "absent"
    expected_value = {"null": None, "false": False, "true": True}.get(design_ready_state)
    for record in graph["materials"]:
        assert ("design_ready" in record) is expected_presence
        if expected_presence:
            assert record["design_ready"] is expected_value


def test_same_project_frozen_snapshot_save_preserves_unused_dataset_on_referenced_material(tmp_path):
    store, service, design, _materials = _seed_service(tmp_path, with_unused_dataset=True)
    snapshot = freeze_complete_design(service, design)
    expected_material = service.materials.get("substrate")
    workspace = _workspace_for(store)

    assert workspace.initialize_frozen_draft(snapshot)
    assert workspace.editor.save_current(), workspace.editor.snapshot().error
    saved = workspace.editor.snapshot()
    assert saved.path is not None
    film_id = next(row["id"] for row in store.manifest["documents"]
                   if row["kind"] == "film" and row["id"] in saved.path)
    graph = ProjectStore.open(store.project_file).resolve_document(film_id)
    persisted_material = next(row for row in graph["materials"] if row["id"] == "substrate")

    assert persisted_material == expected_material
    assert {row["id"] for row in persisted_material["optical_sets"]} == {
        "substrate-bulk", "unused-substrate"}


def test_freeze_rejects_invalid_unused_dataset_on_referenced_material_without_mutation(
        tmp_path, monkeypatch):
    store, service, design, _materials = _seed_service(tmp_path)
    provider_records = service.materials._records()
    substrate = next(row for row in provider_records if row["id"] == "substrate")
    invalid_unused = _material_with_sets("substrate", "invalid-unused")["optical_sets"][0]
    invalid_unused["n"] = [float("nan"), 1.5]
    substrate["optical_sets"].append(invalid_unused)
    monkeypatch.setattr(service, "materials", _SyntheticMaterialProvider(provider_records))
    original_design = deepcopy(design)
    manifest_before = store.manifest
    files_before = _file_snapshot(store.root)

    with pytest.raises(ValueError):
        freeze_complete_design(service, design)

    assert design == original_design
    assert store.manifest == manifest_before
    assert _file_snapshot(store.root) == files_before


def test_freeze_prefers_captured_used_dataset_and_retains_provider_sets_in_order(
        tmp_path, monkeypatch):
    store, service, design, _materials = _seed_service(tmp_path)
    provider_records = service.materials._records()
    face = next(row for row in provider_records if row["id"] == "shared-face")
    unused_face_set = _material_with_sets("shared-face", "unused-face")["optical_sets"][0]
    face["optical_sets"].append(unused_face_set)
    monkeypatch.setattr(service, "materials", _SyntheticMaterialProvider(provider_records))

    original_freeze = service.freeze_design

    def freeze_with_updated_active_set(value, settings=None):
        captured = original_freeze(value, settings)
        face_record = next(row for row in captured["materials"] if row["id"] == "shared-face")
        active_set = next(row for row in face_record["optical_sets"]
                          if row["id"] == "front-film")
        active_set["n"] = [3.0, 3.0]
        return captured

    monkeypatch.setattr(service, "freeze_design", freeze_with_updated_active_set)
    snapshot = freeze_complete_design(service, design)

    face_snapshot = next(row for row in snapshot["materials"] if row["id"] == "shared-face")
    assert [row["id"] for row in face_snapshot["optical_sets"]] == [
        "front-film", "back-film", "unused-face"]
    active = next(row for row in face_snapshot["optical_sets"] if row["id"] == "front-film")
    back = next(row for row in face_snapshot["optical_sets"] if row["id"] == "back-film")
    assert active["n"] == [3.0, 3.0]
    assert back["n"] == [2.1, 2.1]


@pytest.mark.parametrize(
    ("missing_material", "missing_set"),
    [("unused-symbol", "symbol-n"), ("optimization-candidate", "candidate-n")],
)
def test_legacy_numerical_snapshot_is_usable_but_cannot_seed_incomplete_editable_draft(
        tmp_path, missing_material, missing_set):
    store, service, design, _materials = _seed_service(tmp_path, with_default_process=True)
    snapshot = freeze_complete_design(service, design)
    snapshot["materials"] = [row for row in snapshot["materials"]
                            if row["id"] != missing_material]
    snapshot["sha256"] = _sha256(snapshot)
    legacy_snapshot = validate_frozen_design_snapshot(snapshot)

    preview = preview_film_design_import(legacy_snapshot, "front")
    assert [row["material_id"] for row in preview["rows"]] == ["shared-face"]
    machine = store.resolve_default()["runtime"]["machine"]
    imported = build_film_process_import(legacy_snapshot, "front", {"shared-face": 1}, machine)
    assert imported["film_design_import"]["snapshot_sha256"] == legacy_snapshot["sha256"]

    workspace = _workspace_for(store)
    project_material = workspace.service.materials.get(missing_material)
    assert project_material["id"] == missing_material
    assert missing_set in {row["id"] for row in project_material["optical_sets"]}
    editor = workspace.editor
    pristine = editor.snapshot()
    manifest_before = store.manifest
    files_before = _file_snapshot(store.root)
    assert not workspace.initialize_frozen_draft(legacy_snapshot)
    failed = editor.snapshot()
    assert f"{missing_material}/{missing_set}" in failed.error
    assert failed.design == pristine.design
    assert failed.path is None
    assert not failed.dirty
    assert store.manifest == manifest_before
    assert _file_snapshot(store.root) == files_before


@pytest.mark.parametrize(
    ("reference_kind", "material_id", "available_set", "missing_set"),
    [
        ("symbol", "unused-symbol", "symbol-n", "missing-symbol-n"),
        ("candidate", "optimization-candidate", "candidate-n", "missing-candidate-n"),
    ],
)
def test_freeze_fails_for_missing_bound_symbol_or_candidate_dataset_without_mutation(
        tmp_path, reference_kind, material_id, available_set, missing_set):
    store, service, design, _materials = _seed_service(tmp_path)
    changed = deepcopy(design)
    if reference_kind == "symbol":
        changed["expression"]["symbols"]["U"]["optical_set_id"] = missing_set
    else:
        changed["workflow_settings"]["optimization"]["structure_materials"][0][
            "optical_set_id"] = missing_set
    original_changed = deepcopy(changed)
    manifest_before = store.manifest
    files_before = _file_snapshot(store.root)

    with pytest.raises(ValueError) as error:
        freeze_complete_design(service, changed)

    assert material_id in str(error.value)
    assert missing_set in str(error.value)
    assert available_set != missing_set
    assert changed == original_changed
    assert store.manifest == manifest_before
    assert _file_snapshot(store.root) == files_before


@pytest.mark.parametrize(
    ("malformation", "error_code"),
    [("unsupported_version", "unsupported_version"), ("duplicate_candidate", "dependency_conflict")],
)
def test_freeze_rejects_malformed_typed_candidate_configuration_without_mutation(
        tmp_path, malformation, error_code):
    store, service, design, _materials = _seed_service(tmp_path)
    changed = deepcopy(design)
    optimization = changed["workflow_settings"]["optimization"]
    if malformation == "unsupported_version":
        optimization["version"] = 4
    else:
        optimization["structure_materials"].append(
            deepcopy(optimization["structure_materials"][0]))
    changed_before = deepcopy(changed)
    manifest_before = store.manifest
    files_before = _file_snapshot(store.root)

    with pytest.raises(ProjectError) as error:
        freeze_complete_design(service, changed)

    assert error.value.code == error_code
    assert changed == changed_before
    assert store.manifest == manifest_before
    assert _file_snapshot(store.root) == files_before
