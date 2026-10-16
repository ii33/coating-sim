"""Save work-count and identity contracts, without wall-clock test thresholds."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from coat_sim.core.project_film_service import ProjectFilmService, ProjectOpticalSnapshot
from coat_sim.core.project_film_session import ProjectFilmEditSession
from coat_sim.core.project_store import ProjectStore
from tests.test_project_film_service import EmptyCatalog, bound_design, material, service_at

pytestmark = pytest.mark.contract


class CountingCatalog(EmptyCatalog):
    def __init__(self):
        self.reads = 0

    def catalog_records(self):
        self.reads += 1
        return []


def test_normal_save_checks_sources_once_without_rebuilding_unchanged_picker(tmp_path):
    catalog = CountingCatalog()
    store = ProjectStore.create(tmp_path / "project", "Save efficiency")
    service = ProjectFilmService(store, global_material_library=catalog)
    saved = service.create_film(bound_design(), materials={"shared": material()})
    editor = ProjectFilmEditSession(service)
    assert editor.load_saved_design(saved.document_id)
    assert editor.set_design_value("note", "User's exact note")
    original_catalog = editor._catalog_options
    reads = catalog.reads
    with patch.object(editor, "_refresh_project_materials", wraps=editor._refresh_project_materials) as picker:
        assert editor.save_current(), editor.snapshot().error
    assert catalog.reads == reads + 1  # Re-read each Save; no stale cross-window cache.
    assert picker.call_count == 0
    assert editor._catalog_options is original_catalog
    assert service.load_document(saved.document_id).design["note"] == "User's exact note"
    # A second Save still rechecks mutable inputs rather than assuming freshness.
    assert editor.save_current(), editor.snapshot().error
    assert catalog.reads == reads + 2


def test_changed_material_is_rechecked_before_save_and_preserves_dirty_draft(tmp_path):
    service = service_at(tmp_path / "project")
    saved = service.create_film(bound_design(), materials={"shared": material()})
    editor = ProjectFilmEditSession(service)
    assert editor.load_saved_design(saved.document_id)
    assert editor.set_design_value("name", "Keep this draft")
    row = next(row for row in service.project_store.manifest["documents"]
               if row["kind"] == "optical_material")
    graph = service.project_store.resolve_document(row["id"])
    envelope = next(row["data"] for row in graph["documents"] if row["id"] == graph["root_document_id"])
    envelope["material"]["optical_sets"][0]["n"] = [3.0, 3.0]
    service.project_store.save_document(row["id"], envelope)
    before = service.project_store.manifest
    assert not editor.save_current()
    current = editor.snapshot()
    assert current.dirty and current.design["name"] == "Keep this draft"
    assert "material changed" in current.error
    assert service.project_store.manifest == before
    assert service.catalog_material("shared")["optical_sets"][0]["n"] == [3.0, 3.0]
    assert service.materials.get("shared")["optical_sets"][0]["n"] == [2.0, 2.0]


def test_composed_owned_snapshots_never_revalidate_and_all_public_reads_detach():
    primary = ProjectOpticalSnapshot([material("primary")], usage={"primary": {"film"}})
    fallback = ProjectOpticalSnapshot([material("fallback")], usage={"fallback": {"medium"}})
    with patch("coat_sim.core.project_film_service.normalize_optical_material",
               side_effect=AssertionError("owned values already normalized")):
        merged = ProjectOpticalSnapshot._combine(primary, fallback)
    options = merged.options_many(("film", "medium"))
    options["film"][0]["optical_sets"][0]["n"][0] = 99.0
    merged.get("fallback")["optical_sets"].clear()
    assert primary.get("primary")["optical_sets"][0]["n"] == [2.0, 2.0]
    assert fallback.get("fallback")["optical_sets"][0]["n"] == [2.0, 2.0]
    assert merged.options("film")[0]["optical_sets"][0]["n"] == [2.0, 2.0]


def test_generation_replaces_contents_retains_managed_identity_and_saves_once(tmp_path):
    service, editor, _catalog, saved, inputs = generated_editor(tmp_path)
    before = editor.snapshot()
    preview = editor.preview_generated_design(inputs)
    candidate = preview["candidate_design"]
    assert editor.apply_generated_design(candidate, before.design_revision)
    applied = editor.snapshot()
    assert applied.path == before.path
    assert applied.design["id"] == before.design["id"]
    assert applied.design["front_layers"][0]["id"] == candidate["front_layers"][0]["id"]
    assert applied.dirty
    assert editor.set_layer_value("front", applied.design["front_layers"][0]["id"],
                                  "thickness.value", 73.12345678901234)
    assert editor.save_current(), editor.snapshot().error
    persisted = service.load_document(saved.document_id)
    assert persisted.design == editor.snapshot().design
    assert persisted.design["front_layers"][0]["thickness"]["value"] == 73.12345678901234
    assert len(service.list_saved_designs()) == 1
    assert editor.save_copy_current(), editor.snapshot().error
    assert len(service.list_saved_designs()) == 2


class MutableOpticalCatalog:
    def __init__(self):
        selected = material("new-film", n=1.38)
        selected["optical_sets"][0]["id"] = "user-selected-set"
        self.records = [selected, material("air", n=1.0)]

    def catalog_records(self):
        return [{"record": {"id": row["id"], "name": row["name"],
                            "notes": row["notes"], "category": "film",
                            "roles": ["film", "substrate", "medium"],
                            "optical_constants": deepcopy(row["optical_sets"])}}
                for row in self.records]


def generated_editor(tmp_path, *, frozen=False):
    catalog = MutableOpticalCatalog()
    store = ProjectStore.create(tmp_path / "project", "Generator snapshots")
    service = ProjectFilmService(store, global_material_library=catalog)
    saved = service.create_film(bound_design(), materials={"shared": material()})
    editor = ProjectFilmEditSession(service)
    if frozen:
        from coat_sim.core.film_process_import import freeze_complete_design
        assert editor.initialize_frozen_draft(freeze_complete_design(service, saved.design))
    else:
        assert editor.load_saved_design(saved.document_id)
    inputs = {"template_id": "ar_single_qw", "wavelength_nm": 550.0,
              "role_bindings": {"F": {"material_id": "new-film",
                                         "optical_set_id": "user-selected-set"}},
              "incident_binding": {"material_id": "air", "optical_set_id": "visible"},
              "substrate_binding": {"material_id": "shared", "optical_set_id": "visible"},
              "assume_lossless": False}
    return service, editor, catalog, saved, inputs


def test_real_generated_design_pins_selected_dataset_and_detects_new_material_change(tmp_path):
    service, editor, catalog, saved, inputs = generated_editor(tmp_path)
    preview = editor.preview_generated_design(inputs)
    assert editor.apply_generated_design(preview["candidate_design"], preview["base_design_revision"])
    current = editor.snapshot()
    assert current.design["id"] == saved.design["id"]
    assert current.design["front_layers"][0]["optical_set_id"] == "user-selected-set"
    assert current.design["front_layers"][0]["thickness"]["value"] == 550.0 / (4.0 * 1.38)
    catalog.records[0]["optical_sets"][0]["n"] = [1.7, 1.7]
    assert not editor.save_current()
    assert "material changed" in editor.snapshot().error
    assert editor.snapshot().dirty
    assert editor.snapshot().design == current.design
    assert service.materials.get("new-film")["optical_sets"][0]["n"] == [1.38, 1.38]
    assert service.catalog_material("new-film")["optical_sets"][0]["n"] == [1.7, 1.7]
    assert service.load_document(saved.document_id).design == saved.design


def test_material_refresh_invalidates_preview_and_undo_restores_original_material_context(tmp_path):
    service, editor, catalog, _saved, inputs = generated_editor(tmp_path)
    before = editor.snapshot()
    retained = deepcopy(editor._retained_material_snapshots)
    preview = editor.preview_generated_design(inputs)
    catalog.records[0]["optical_sets"][0]["n"] = [1.7, 1.7]
    editor.refresh_project_materials()
    assert not editor.apply_generated_design(preview["candidate_design"], preview["base_design_revision"])
    assert editor.snapshot().design == before.design
    fresh = editor.preview_generated_design(inputs)
    assert editor.apply_generated_design(fresh["candidate_design"], fresh["base_design_revision"])
    applied = editor.snapshot()
    assert editor._retained_material_snapshots["new-film"]["optical_sets"][0]["n"] == [1.7, 1.7]
    assert editor.undo_generated_design(applied.design_revision)
    assert editor.snapshot().design == before.design
    assert editor.snapshot().path == before.path
    assert editor._retained_material_snapshots == retained
    assert editor._frozen_draft_materials is None
    assert service.materials.get("shared")["optical_sets"][0]["n"] == [2.0, 2.0]


def test_frozen_default_generation_carries_new_complete_closure_on_first_save(tmp_path):
    service, editor, _catalog, saved, inputs = generated_editor(tmp_path, frozen=True)
    before_frozen = deepcopy(editor._frozen_draft_materials)
    preview = editor.preview_generated_design(inputs)
    assert editor.apply_generated_design(preview["candidate_design"], preview["base_design_revision"])
    applied = editor.snapshot()
    assert {"new-film", "shared", "air"} == set(editor._frozen_draft_materials)
    assert editor.undo_generated_design(applied.design_revision)
    assert editor._frozen_draft_materials == before_frozen
    preview = editor.preview_generated_design(inputs)
    assert editor.apply_generated_design(preview["candidate_design"], preview["base_design_revision"])
    assert editor.save_current(), editor.snapshot().error
    assert not editor.snapshot().dirty and editor.snapshot().path != str(saved.handle)
    graph = service.project_store.resolve_document(service._handle_id(editor.snapshot().path))
    assert {row["id"] for row in graph["materials"]} == {"new-film", "shared", "air"}
    assert editor.snapshot().design["front_layers"][0]["optical_set_id"] == "user-selected-set"


def test_save_refreshes_picker_when_new_project_capture_changes_material_roles(tmp_path):
    service, editor, _catalog, saved, inputs = generated_editor(tmp_path)
    assert "new-film" in {row["id"] for row in editor._catalog_options["medium"]}
    preview = editor.preview_generated_design(inputs)
    assert editor.apply_generated_design(preview["candidate_design"], preview["base_design_revision"])
    assert editor.save_current(), editor.snapshot().error
    # This global record is now project-owned and used as a film only. Its
    # saved picker roles must be reflected immediately, not on a later refresh.
    assert "new-film" in {row["id"] for row in editor._catalog_options["film"]}
    assert "new-film" not in {row["id"] for row in editor._catalog_options["medium"]}
    assert editor._picker_catalog_source is service._project_materials
    assert len(service.list_saved_designs()) == 1
    assert service.load_document(saved.document_id).design == editor.snapshot().design


def test_superseded_preview_cannot_rebind_old_thickness_to_new_material_snapshot(tmp_path):
    service, editor, catalog, saved, inputs = generated_editor(tmp_path)
    first = editor.preview_generated_design(inputs)
    catalog.records[0]["optical_sets"][0]["n"] = [1.7, 1.7]
    editor.refresh_project_materials()
    latest = editor.preview_generated_design(inputs)
    before = editor.snapshot()
    assert not editor.apply_generated_design(first["candidate_design"], first["base_design_revision"])
    assert editor.snapshot() == before
    assert service.load_document(saved.document_id).design == saved.design
    assert editor.apply_generated_design(latest["candidate_design"], latest["base_design_revision"])
    assert editor.snapshot().design["front_layers"][0]["thickness"]["value"] == 550.0 / (4.0 * 1.7)


def test_generated_apply_rejects_unregistered_and_modified_candidates(tmp_path):
    _service, editor, _catalog, _saved, inputs = generated_editor(tmp_path)
    before = editor.snapshot()
    unknown = deepcopy(before.design)
    unknown["id"] = "unregistered-candidate"
    assert not editor.apply_generated_design(unknown, before.design_revision)
    preview = editor.preview_generated_design(inputs)
    changed = deepcopy(preview["candidate_design"])
    changed["front_layers"][0]["thickness"]["value"] += 1.0
    assert not editor.apply_generated_design(changed, preview["base_design_revision"])
    assert editor.snapshot() == before
    assert editor.apply_generated_design(preview["candidate_design"], preview["base_design_revision"])


def test_failed_repreview_invalidates_previous_core_candidate(tmp_path):
    _service, editor, _catalog, _saved, inputs = generated_editor(tmp_path)
    preview = editor.preview_generated_design(inputs)
    with pytest.raises(ValueError):
        editor.preview_generated_design({**inputs, "template_id": "missing-template"})
    assert not editor.apply_generated_design(preview["candidate_design"], preview["base_design_revision"])


def test_frozen_undo_preserves_later_workflow_and_its_pinned_material_closure(tmp_path):
    service, editor, catalog, _saved, inputs = generated_editor(tmp_path, frozen=True)
    before = editor.snapshot()
    preview = editor.preview_generated_design(inputs)
    assert editor.apply_generated_design(preview["candidate_design"], preview["base_design_revision"])
    configuration = editor.workflow_configuration("optimization")
    configuration["structure_materials"] = [{
        "material_id": "new-film", "optical_set_id": "user-selected-set"}]
    assert editor.set_workflow_configuration("optimization", configuration)
    assert editor.undo_generated_design(editor.snapshot().design_revision)
    restored = editor.snapshot()
    assert restored.design["id"] == before.design["id"]
    assert restored.design["front_layers"] == before.design["front_layers"]
    assert restored.design["workflow_settings"]["optimization"] == configuration
    assert restored.dirty and restored.path == before.path
    assert set(editor._frozen_draft_materials) == {"shared", "new-film"}
    assert editor._retained_material_snapshots["new-film"]["optical_sets"][0]["n"] == [1.38, 1.38]
    assert editor.save_current(), editor.snapshot().error
    graph = service.project_store.resolve_document(service._handle_id(editor.snapshot().path))
    assert {row["id"] for row in graph["materials"]} == {"shared", "new-film"}
    assert service.load_document(service._handle_id(editor.snapshot().path)).design == editor.snapshot().design
    # The closure retained during Undo remains provenance, not a live alias.
    catalog.records[0]["optical_sets"][0]["n"] = [1.7, 1.7]
    assert service.materials.get("new-film")["optical_sets"][0]["n"] == [1.38, 1.38]


def test_frozen_undo_keeps_added_workflow_material_stale_save_protection(tmp_path):
    service, editor, catalog, saved, inputs = generated_editor(tmp_path, frozen=True)
    preview = editor.preview_generated_design(inputs)
    assert editor.apply_generated_design(preview["candidate_design"], preview["base_design_revision"])
    configuration = editor.workflow_configuration("optimization")
    configuration["structure_materials"] = [{
        "material_id": "new-film", "optical_set_id": "user-selected-set"}]
    assert editor.set_workflow_configuration("optimization", configuration)
    catalog.records[0]["optical_sets"][0]["n"] = [1.7, 1.7]
    editor.refresh_project_materials()
    assert editor.undo_generated_design(editor.snapshot().design_revision)
    assert editor._frozen_draft_materials["new-film"]["optical_sets"][0]["n"] == [1.38, 1.38]
    assert not editor.save_current()
    assert "material changed" in editor.snapshot().error
    assert editor.snapshot().dirty
    assert service.load_document(saved.document_id).design == saved.design


def test_reference_preview_uses_the_same_core_candidate_source_guard(tmp_path):
    _service, editor, catalog, _saved, inputs = generated_editor(tmp_path)
    reference_inputs = {**inputs, "recipe_id": "comsol_ideal_single_ar", "mapping_confirmed": True}
    first = editor.preview_reference_design(reference_inputs)
    catalog.records[0]["optical_sets"][0]["n"] = [1.7, 1.7]
    editor.refresh_project_materials()
    latest = editor.preview_reference_design(reference_inputs)
    assert not editor.apply_generated_design(first["candidate_design"], first["base_design_revision"])
    assert editor.apply_generated_design(latest["candidate_design"], latest["base_design_revision"])
    assert editor.snapshot().design["front_layers"][0]["optical_set_id"] == "user-selected-set"


def test_frozen_undo_adds_workflow_only_material_without_retaining_generated_stack(tmp_path):
    service, editor, catalog, _saved, inputs = generated_editor(tmp_path, frozen=True)
    catalog.records.append(material("optimizer-only", n=1.6))
    editor.refresh_project_materials()
    preview = editor.preview_generated_design(inputs)
    assert editor.apply_generated_design(preview["candidate_design"], preview["base_design_revision"])
    configuration = editor.workflow_configuration("optimization")
    configuration["structure_materials"] = [{
        "material_id": "optimizer-only", "optical_set_id": "visible"}]
    assert editor.set_workflow_configuration("optimization", configuration)
    assert editor.undo_generated_design(editor.snapshot().design_revision)
    assert set(editor._frozen_draft_materials) == {"shared", "optimizer-only"}
    assert editor.save_current(), editor.snapshot().error
    graph = service.project_store.resolve_document(service._handle_id(editor.snapshot().path))
    assert {row["id"] for row in graph["materials"]} == {"shared", "optimizer-only"}


def test_incompatible_undo_workflow_reference_fails_before_restoring_any_state(tmp_path):
    service, editor, _catalog, _saved, inputs = generated_editor(tmp_path, frozen=True)
    preview = editor.preview_generated_design(inputs)
    assert editor.apply_generated_design(preview["candidate_design"], preview["base_design_revision"])
    configuration = editor.workflow_configuration("optimization")
    configuration["structure_materials"] = [{
        "material_id": "shared", "optical_set_id": "not-in-original-snapshot"}]
    assert editor.set_workflow_configuration("optimization", configuration)
    before = editor.snapshot()
    frozen = deepcopy(editor._frozen_draft_materials)
    retained = deepcopy(editor._retained_material_snapshots)
    provider = service.materials
    assert not editor.undo_generated_design(before.design_revision)
    after = editor.snapshot()
    assert after.design == before.design and after.path == before.path
    assert after.design_revision == before.design_revision and after.dirty == before.dirty
    assert editor._frozen_draft_materials == frozen
    assert editor._retained_material_snapshots == retained and service.materials is provider
    assert editor.can_undo_generated_design(after.design_revision)
    assert "not-in-original-snapshot" in after.error


@pytest.mark.qt
def test_controller_keeps_undo_available_after_a_recoverable_workflow_conflict(tmp_path):
    qt = pytest.importorskip("PySide6.QtCore")
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from tests.test_project_film_controller_adapter import MemorySettings
    service, editor, _catalog, _saved, inputs = generated_editor(tmp_path, frozen=True)
    controller = FilmDesignQuickController(
        service=service, edit_session=editor, geometry_settings=MemorySettings())
    try:
        preview = controller.generateDesignPreview(inputs)
        assert preview["ok"] and controller.applyGeneratedDesign(preview["preview_token"])
        configuration = editor.workflow_configuration("optimization")
        configuration["structure_materials"] = [{
            "material_id": "shared", "optical_set_id": "not-in-original-snapshot"}]
        assert editor.set_workflow_configuration("optimization", configuration)
        assert not controller.undoGeneratedDesign()
        assert controller.canUndoGeneratedDesign
        configuration["structure_materials"] = [{
            "material_id": "new-film", "optical_set_id": "user-selected-set"}]
        assert editor.set_workflow_configuration("optimization", configuration)
        assert controller.undoGeneratedDesign()
        assert not controller.canUndoGeneratedDesign
        assert editor.workflow_configuration("optimization") == configuration
        assert editor.save_current(), editor.snapshot().error
    finally:
        controller.markSourceClosed()
        controller.deleteLater()
        qt.QCoreApplication.sendPostedEvents(None, qt.QEvent.Type.DeferredDelete)
