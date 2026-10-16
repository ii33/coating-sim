"""Project film adapters reuse the numerical implementation without global data."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
from unittest.mock import patch
from typing import cast

import pytest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_store import ProjectStore


pytestmark = pytest.mark.contract


class EmptyCatalog:
    """Keep project-local tests independent of the application material catalog."""

    def catalog_records(self):
        return []


def material(identity: str = "shared", n: float = 2.0) -> dict:
    return {"id": identity, "name": "Synthetic " + identity, "notes": "Test fixture",
            "optical_sets": [{"id": "visible", "name": "Synthetic visible", "kind": "tabulated",
                              "wavelength_unit": "nm", "wavelength_min": 400.0,
                              "wavelength_max": 700.0, "wavelengths": [400.0, 700.0],
                              "n": [n, n], "k": [0.0, 0.0], "source": "Synthetic fixture"}]}


def bound_design() -> dict:
    design = new_design("Bound project film")
    design["substrate"].update(material_id="shared", optical_set_id="visible")
    design["workflow_settings"] = {"simulation": {"samples": 17, "use_k": True},
                                   "optimization": {"version": 1, "targets": []}}
    return validate_design(design)


def service_at(root: Path) -> ProjectFilmService:
    return ProjectFilmService(ProjectStore.create(root, root.name),
                              global_material_library=EmptyCatalog())


def assert_error(code: str, call):
    with pytest.raises(ProjectError) as error:
        call()
    assert error.value.code == code


def test_empty_project_session_uses_no_global_catalog_or_sqlite(tmp_path):
    with patch("coat_sim.core.film_design_service.CoatService", side_effect=AssertionError("global root")), \
         patch("coat_sim.core.film_design_service.FilmMaterialLibrary", side_effect=AssertionError("catalog")):
        service = service_at(tmp_path / "project")
        session = FilmDesignEditSession(service)
        assert service.material_options_many(("film", "substrate", "medium")) == {
            "film": [], "substrate": [], "medium": []}
        assert session.snapshot().design["substrate"]["material_id"] == ""
        assert session.save_to_library("First empty film")
        assert len(service.list_saved_designs()) == 1
        assert not (service.root / "data" / "film_design").exists()
        assert not list(service.root.rglob("*.sqlite3"))
        assert service.project_store.manifest["default_configuration_id"] is None


def test_create_save_reload_keeps_managed_id_internal_id_and_workflow(tmp_path):
    service = service_at(tmp_path / "project")
    created = service.create_film(bound_design(), materials={"shared": material()})
    old_revision = created.revision
    old_project_revision = created.project_revision
    edited = deepcopy(created.design)
    edited["name"] = "Edited"
    edited["workflow_settings"]["simulation"]["samples"] = 33
    saved = service.save_document(created.document_id, edited, expected_revision=old_revision)
    assert saved.document_id == created.document_id
    assert saved.design["id"] == created.design["id"]
    assert saved.project_revision > old_project_revision
    assert saved.revision != old_revision
    assert saved.handle == created.handle
    assert not saved.handle.exists()
    reopened = ProjectFilmService(ProjectStore.open(cast(ProjectStore, service.project_store).project_file)).load_document(saved.document_id)
    assert reopened.design == edited
    assert reopened.design["workflow_settings"]["optimization"] == {"version": 1, "targets": []}
    assert len([row for row in service.project_store.manifest["documents"] if row["kind"] == "film"]) == 1


def test_same_material_id_in_two_projects_produces_independent_existing_numerics(tmp_path):
    first, second = service_at(tmp_path / "first"), service_at(tmp_path / "second")
    left = first.create_film(bound_design(), materials={"shared": material(n=2.0)})
    right = second.create_film(bound_design(), materials={"shared": material(n=3.0)})
    settings = {"wavelength_min_nm": 450.0, "wavelength_max_nm": 650.0,
                "samples": 5, "angle_deg": 0.0, "polarization": "s"}
    left_result = first.simulate(left.design, settings)
    right_result = second.simulate(right.design, settings)
    assert left_result["R"] == pytest.approx([1.0 / 9.0] * 5)
    assert right_result["R"] == pytest.approx([0.25] * 5)
    assert first.freeze_design(left.design, settings)["materials"][0]["optical_sets"][0]["n"] == [2.0, 2.0]
    assert second.freeze_design(right.design, settings)["materials"][0]["optical_sets"][0]["n"] == [3.0, 3.0]
    assert first.project_id != second.project_id


def test_import_copies_file_and_resources_then_relocation_uses_project_only(tmp_path):
    source = tmp_path / "outside.cfilm"
    source.write_text(json.dumps(bound_design()), encoding="utf-8")
    original = source.read_bytes()
    service = service_at(tmp_path / "project")
    imported = service.import_film(source, materials={"shared": material()})
    assert source.read_bytes() == original
    source.unlink()
    moved = tmp_path / "moved"
    shutil.move(service.root, moved)
    reopened = ProjectFilmService(ProjectStore.open(moved / cast(ProjectStore, service.project_store).project_file.name))
    assert reopened.load_document(imported.document_id).design == imported.design
    assert reopened.materials.get("shared")["optical_sets"][0]["n"] == [2.0, 2.0]


def test_missing_and_conflicting_materials_fail_without_fallback_or_partial_save(tmp_path):
    service = service_at(tmp_path / "project")
    assert_error("missing_dependency", lambda: service.create_film(bound_design()))
    assert service.project_store.manifest["documents"] == []
    created = service.create_film(bound_design(), materials={"shared": material()})
    before = deepcopy(service.project_store.manifest)
    changed = deepcopy(created.design)
    changed["name"] = "Should fail"
    assert_error("dependency_conflict", lambda: service.save_document(
        created.document_id, changed, expected_revision=created.revision,
        materials={"shared": material(n=9.0)}))
    assert service.project_store.manifest == before
    assert service.load_document(created.document_id).design == created.design


def test_stale_revision_and_changed_internal_design_id_cannot_overwrite(tmp_path):
    service = service_at(tmp_path / "project")
    created = service.create_film()
    newer = deepcopy(created.design)
    newer["note"] = "newer saved note"
    saved = service.save_document(created.document_id, newer, expected_revision=created.revision)
    assert_error("stale_document", lambda: service.save_document(
        created.document_id, created.design, expected_revision=created.revision))
    changed_id = deepcopy(saved.design)
    changed_id["id"] = "a-different-design"
    assert_error("dependency_conflict", lambda: service.save_document(
        saved.document_id, changed_id, expected_revision=saved.revision))
    assert service.load_document(created.document_id).design["note"] == "newer saved note"


def test_headless_save_does_not_refresh_legacy_editor_base_and_hide_staleness(tmp_path):
    service = service_at(tmp_path / "project")
    created = service.create_film()
    old_editor_draft = service.load(created.handle)
    headless = service.load_document(created.document_id)
    newer = deepcopy(headless.design)
    newer["note"] = "AI saved this after editor load"
    service.save_document(headless.document_id, newer, expected_revision=headless.revision)
    old_editor_draft["note"] = "stale editor data"
    assert_error("stale_document", lambda: service.save(created.handle, old_editor_draft, overwrite=True))
    assert service.load_document(created.document_id).design["note"] == newer["note"]


def test_unrelated_document_change_does_not_stale_unchanged_film_bytes(tmp_path):
    service = service_at(tmp_path / "project")
    first = service.create_film()
    service.create_film(new_design("An unrelated design"))
    changed = deepcopy(first.design)
    changed["note"] = "still based on the same saved film"
    saved = service.save_document(first.document_id, changed, expected_revision=first.revision)
    assert saved.design["note"] == changed["note"]


def test_legacy_managed_handle_session_save_writes_manifest_not_alias_or_old_objects(tmp_path):
    service = service_at(tmp_path / "project")
    created = service.create_film()
    old_row = next(row for row in service.project_store.manifest["documents"] if row["id"] == created.document_id)
    old_bytes = (service.root / old_row["path"]).read_bytes()
    session = FilmDesignEditSession(service)
    assert session.request_open_file(str(created.handle))
    assert session.set_design_value("note", "managed session edit")
    assert session.set_workflow_configuration("simulation", {"samples": 19})
    assert session.save_current()
    current = service.load_document(created.document_id)
    assert current.design["note"] == "managed session edit"
    assert current.design["workflow_settings"]["simulation"]["samples"] == 19
    assert not created.handle.exists()
    assert (service.root / old_row["path"]).read_bytes() == old_bytes
    assert not session.snapshot().dirty


def test_external_paths_and_raw_object_paths_are_not_legacy_save_targets(tmp_path):
    service = service_at(tmp_path / "project")
    created = service.create_film()
    row = next(row for row in service.project_store.manifest["documents"] if row["id"] == created.document_id)
    for target in (tmp_path / "outside.cfilm", service.root / row["path"]):
        assert_error("unmanaged_path", lambda target=target: service.save(target, created.design, overwrite=True))
    assert not (tmp_path / "outside.cfilm").exists()
    assert_error("revision_required", lambda: service.save(created.handle, created.design, overwrite=True))
    with pytest.raises(ValueError, match="overwrite=True"):
        service.save(created.handle, created.design)


def test_saved_catalog_uses_managed_ids_and_rejects_ambiguous_design_payload_ids(tmp_path):
    service = service_at(tmp_path / "project")
    design = new_design("Catalog design")
    assert service.save_saved_design(design)["id"] == design["id"]
    catalog = service.list_saved_designs()
    managed_id = catalog[0]["design_id"]
    assert managed_id != design["id"]
    loaded = service.load_saved_design(managed_id)
    loaded["note"] = "catalog update"
    service.save_saved_design(loaded)
    assert service.load_document(managed_id).design["note"] == "catalog update"
    duplicate = service.create_film(design)
    assert duplicate.document_id != managed_id
    assert_error("ambiguous_document", lambda: service.save_saved_design(design))


def test_symbol_and_candidate_resources_are_local_and_picker_usage_is_explicit(tmp_path):
    service = service_at(tmp_path / "project")
    design = bound_design()
    design["expression"] = {"text": "H", "in_sync": False, "symbols": {
        "H": {"material_id": "symbol", "optical_set_id": "visible",
              "thickness": {"value": 90.0, "unit": "nm"}, "note": "Symbol only"}}}
    design["workflow_settings"]["optimization"]["structure_materials"] = [
        {"material_id": "candidate", "optical_set_id": "visible"}]
    created = service.create_film(design, materials={
        "shared": material(), "symbol": material("symbol"), "candidate": material("candidate")})
    graph = service.project_store.resolve_document(created.document_id)
    assert {row["id"] for row in graph["materials"]} == {"shared", "symbol", "candidate"}
    assert {row["id"] for row in service.material_options("substrate")} == {"shared"}
    assert {row["id"] for row in service.material_options("film")} == {"symbol", "candidate"}
    assert service.material_options("medium") == []
    frozen = service.freeze_design(created.design)
    from coat_sim.core.film_optimization_needle import (
        FilmNeedleMaterialReference, _candidate_payload_from_materials)
    candidate_payload = _candidate_payload_from_materials(
        service, frozen, [FilmNeedleMaterialReference("candidate", "visible")])
    assert "candidate" in {row["id"] for row in candidate_payload["materials"]}


def test_save_new_binding_copies_required_materials_in_same_store_transaction(tmp_path):
    service = service_at(tmp_path / "project")
    created = service.create_film(bound_design(), materials={"shared": material()})
    changed = deepcopy(created.design)
    changed["front_layers"] = [{"id": "new-layer", "material_id": "new-material",
                                "optical_set_id": "visible", "thickness": {"unit": "nm", "value": 50.0},
                                "note": ""}]
    assert_error("missing_dependency", lambda: service.save_document(
        created.document_id, changed, expected_revision=created.revision))
    saved = service.save_document(created.document_id, changed, expected_revision=created.revision,
                                  materials={"new-material": material("new-material", 1.5)})
    assert saved.document_id == created.document_id
    assert {row["id"] for row in service.project_store.resolve_document(saved.document_id)["materials"]} == {
        "shared", "new-material"}


def expression_template(identity: str = "template-one") -> dict:
    return {"schema_version": 1, "template_id": identity, "name": "Reusable expression", "note": "",
            "expression": {"text": "H", "symbols": {
                "H": {"material_id": "symbol", "optical_set_id": "visible",
                      "thickness": {"unit": "nm", "value": 75.0}, "note": ""}}}}


def test_expression_templates_save_as_managed_resources_and_survive_reopen(tmp_path):
    service = service_at(tmp_path / "project")
    template = expression_template()
    assert_error("missing_dependency", lambda: service.save_expression_template(template))
    saved = service.save_expression_template(template, materials={"symbol": material("symbol")})
    row = service.list_expression_templates()[0]
    identity = row["document_id"]
    assert service.project_store.resolve_document(identity)["materials"][0]["id"] == "symbol"
    saved["note"] = "Edited template"
    service.save_expression_template(saved)
    assert service.list_expression_templates()[0]["document_id"] == identity
    reopened = ProjectFilmService(ProjectStore.open(cast(ProjectStore, service.project_store).project_file),
                                  global_material_library=EmptyCatalog())
    assert reopened.load_expression_template(template["template_id"])["note"] == "Edited template"
    assert reopened.material_options("substrate") == []
    assert {row["id"] for row in reopened.material_options("film")} == {"symbol"}
    assert not list(service.root.rglob("*.sqlite3"))


def test_template_updates_require_loaded_revision_and_reject_changed_saved_data(tmp_path):
    first = service_at(tmp_path / "project")
    template = first.save_expression_template(expression_template(), materials={"symbol": material("symbol")})
    second = ProjectFilmService(first.project_store)
    assert_error("revision_required", lambda: second.save_expression_template(template))
    second.load_expression_template(template["template_id"])
    template["note"] = "first editor update"
    first.save_expression_template(template)
    template["note"] = "stale second editor"
    assert_error("stale_document", lambda: second.save_expression_template(template))


def test_admitted_numerical_snapshot_keeps_owned_values_when_provider_is_refreshed(tmp_path):
    service = service_at(tmp_path / "project")
    created = service.create_film(bound_design(), materials={"shared": material()})
    admitted = service.freeze_design(created.design)
    owned_provider = service.materials
    # A real source change installs a new provider. An unrelated film or a
    # no-op refresh may reuse immutable-owned values without changing them.
    row = next(row for row in service.project_store.manifest["documents"]
               if row["kind"] == "optical_material")
    graph = service.project_store.resolve_document(row["id"])
    envelope = next(item["data"] for item in graph["documents"] if item["id"] == row["id"])
    envelope["material"]["optical_sets"][0]["n"] = [3.0, 3.0]
    service.project_store.save_document(row["id"], envelope)
    service.refresh_materials()
    assert service.materials is not owned_provider
    assert owned_provider.get("shared")["optical_sets"][0]["n"] == [2.0, 2.0]
    admitted["materials"][0]["optical_sets"][0]["n"][0] = 9.0
    assert service.materials.get("shared")["optical_sets"][0]["n"] == [3.0, 3.0]


def test_tfcalc_reader_uses_explicit_local_plugin_and_no_material_fallback(tmp_path):
    service = service_at(tmp_path / "project")
    source = tmp_path / "plain.tfd"
    source.write_text("VERSION*1*\nLAYERS*1*\nLAYER*1*H*100*100*\nEOF*\n", encoding="utf-8")
    document = service.read_tfcalc(source)
    assert len(document.layers) == 1
    assert service.material_options("film") == []
