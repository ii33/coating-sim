"""Frozen provenance and captured draft values across import and document switches."""

from copy import deepcopy
import json

import pytest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.project_dependencies import ProjectError
from tests.test_project_film_default_draft import frozen_film_project, workspace_for


pytestmark = pytest.mark.contract


def test_confirmed_same_film_import_keeps_frozen_materials_through_save(tmp_path):
    store, frozen = frozen_film_project(tmp_path / "project")
    workspace = workspace_for([store], store)
    editor = workspace.editor
    assert workspace.initialize_frozen_draft(frozen)

    before = editor.snapshot()
    candidate = json.loads(json.dumps(before.design))
    candidate["front_layers"][0]["thickness"]["value"] = 137.0
    candidate = validate_design(candidate)
    assert editor.request_imported_design(candidate, before.design_revision)
    pending = editor.snapshot().pending_confirmation
    assert pending is not None and pending["kind"] == "import_replace"
    assert editor.resolve_pending_action(True)

    changed = editor.snapshot()
    assert changed.design["front_layers"][0]["thickness"]["value"] == 137.0
    assert changed.path is None and changed.dirty
    assert workspace.service._draft_materials is not None
    assert workspace.service.materials.get("film")["optical_sets"][0]["n"] == [2.0, 2.0]

    spectrum = workspace.service.simulate(
        changed.design, editor.workflow_configuration("simulation"))
    assert spectrum["R"] == pytest.approx([1.0 / 9.0] * 5)

    assert editor.save_current()
    saved = editor.snapshot()
    assert saved.path is not None and not saved.dirty
    films = [row for row in store.manifest["documents"] if row["kind"] == "film"]
    materials = [row for row in store.manifest["documents"] if row["kind"] == "optical_material"]
    assert len(films) == len(materials) == 1
    graph = store.resolve_document(films[0]["id"])
    persisted = next(row["data"] for row in graph["documents"]
                     if row["id"] == films[0]["id"])
    assert persisted["front_layers"][0]["thickness"]["value"] == 137.0
    assert graph["materials"][0]["id"] == "film"
    assert graph["materials"][0]["optical_sets"][0]["n"] == [2.0, 2.0]
    # Saving releases the process-default provenance overlay, then adopts a
    # managed document whose own captured values still protect this editor.
    assert editor._frozen_draft_materials is None
    assert editor._retained_material_snapshots["film"] == graph["materials"][0]
    frozen_after_save = workspace.service.freeze_design(
        saved.design, editor.workflow_configuration("simulation"))
    optical_document = next(row for row in graph["documents"]
                            if row["kind"] == "optical_material")
    changed_material = deepcopy(optical_document["data"])
    changed_material["material"]["optical_sets"][0]["n"] = [3.0, 3.0]
    store.save_document(optical_document["id"], changed_material)
    editor.refresh_project_materials()

    assert workspace.service.catalog_material("film")["optical_sets"][0]["n"] == [3.0, 3.0]
    assert workspace.service.materials.get("film")["optical_sets"][0]["n"] == [2.0, 2.0]
    assert frozen_after_save["materials"][0]["optical_sets"][0]["n"] == [2.0, 2.0]
    refreshed = editor.snapshot()
    assert refreshed.design == saved.design and not refreshed.dirty
    assert workspace.service.freeze_design(
        refreshed.design, editor.workflow_configuration("simulation")) == frozen_after_save
    simulated = workspace.service.simulate(
        refreshed.design, editor.workflow_configuration("simulation"))
    assert simulated["R"] == pytest.approx(spectrum["R"])

    assert editor.set_design_value("note", "Keep this film's captured values")
    manifest_before_stale_save = store.manifest
    assert not editor.save_current()
    assert "changed after this film draft captured it" in editor.snapshot().error
    assert editor.snapshot().dirty
    assert store.manifest == manifest_before_stale_save

    assert not editor.request_load_saved_design(films[0]["id"])
    assert editor.resolve_pending_action(True)
    assert workspace.service.materials.get("film")["optical_sets"][0]["n"] == [3.0, 3.0]
    reloaded_spectrum = workspace.service.simulate(
        editor.snapshot().design, editor.workflow_configuration("simulation"))
    assert reloaded_spectrum["R"] == pytest.approx([1.0 / 4.0] * 5)
    assert frozen_after_save["materials"][0]["optical_sets"][0]["n"] == [2.0, 2.0]


@pytest.mark.parametrize("switch", ["new", "load_saved", "import_managed"])
def test_reset_or_managed_document_switch_clears_frozen_overlay(tmp_path, switch):
    store, frozen = frozen_film_project(tmp_path / "project")
    workspace = workspace_for([store], store)
    editor = workspace.editor
    managed = workspace.service.create_film(new_design("Managed document"))
    catalog_before = workspace.service.material_options_many(("film", "substrate", "medium"))
    manifest_before_browse = store.manifest
    assert not any(row["kind"] == "optical_material"
                   for row in manifest_before_browse["documents"])
    assert workspace.initialize_frozen_draft(frozen)
    assert store.manifest == manifest_before_browse
    assert workspace.service.materials.get("film")["optical_sets"][0]["n"] == [2.0, 2.0]

    if switch == "new":
        assert not editor.request_new_design()
        assert editor.resolve_pending_action(True)
    elif switch == "load_saved":
        assert not editor.request_load_saved_design(managed.document_id)
        assert editor.resolve_pending_action(True)
        assert editor.snapshot().design == managed.design
        assert editor.snapshot().path == str(managed.handle)
    else:
        imported_path = tmp_path / "imported.cfilm"
        imported = new_design("Imported managed document")
        imported_path.write_text(json.dumps(imported), encoding="utf-8")
        assert not editor.request_import_film(str(imported_path))
        assert editor.resolve_pending_action(True)
        assert editor.snapshot().design["name"] == "Imported managed document"
        assert editor.snapshot().path is not None

    assert workspace.service._draft_materials is None
    assert editor._frozen_draft_materials is None
    assert editor._retained_material_snapshots == {}
    # Global picker visibility survives releasing this document's overlay,
    # but browsing it must not materialize unrelated project dependencies.
    assert workspace.service.material_options_many(("film", "substrate", "medium")) == catalog_before
    assert not any(row["kind"] == "optical_material"
                   for row in store.manifest["documents"])
    with pytest.raises(ProjectError) as error:
        workspace.service.materials.get("film")
    assert error.value.code == "missing_dependency"
