"""Selected exchanges validate their operation closure, not unrelated schemas."""
from copy import deepcopy
from pathlib import Path

import pytest

from coat_sim.core.film_design import new_design
from coat_sim.core.materials import default_material_profile
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_operation_scope import ScopedRecords
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore
from tests.design_test_fixtures import _RECTANGULAR_SOURCE
from tests.test_project_core import file_snapshot, material
from tests.test_project_mutation_capture import _rewrite

pytestmark = pytest.mark.contract


def _resource(store, kind, *, incoming=True):
    optical = material("shared", n=2.0 if incoming else 1.5)
    if kind == "material_profile":
        store.create_document("optical_material", {
            "kind": "coat_sim.project_optical_material", "version": 1, "material": optical})
        profile = default_material_profile("Shared material", id="shared")
        profile["notes"] = "Incoming" if incoming else "Existing"
        profile["optical_constants"] = optical["optical_sets"]
        return store.create_document(kind, {
            "kind": "coat_sim.project_material_profile", "version": 1, "material": profile})
    reference = {"material_id": "shared", "optical_set_id": "set",
                 "thickness": {"value": 100.0 if incoming else 50.0, "unit": "nm"}, "note": ""}
    if kind == "film":
        value = new_design("Incoming film" if incoming else "Existing film", design_id="shared-film")
        value["front_layers"] = [{"id": "layer", **reference}]
    else:
        value = {"schema_version": 1, "template_id": "shared-template",
                 "name": "Incoming template" if incoming else "Existing template", "note": "",
                 "expression": {"text": "A", "symbols": {"A": reference}}}
    return store.create_document(kind, value, materials={"shared": optical})


def _unsupported_source(store, *, opaque=False):
    identity = store.create_document("source", deepcopy(_RECTANGULAR_SOURCE))
    value = store._read_record(store._index()[identity])
    value["unsupported_test_field"] = True
    if opaque:
        _rewrite(store, identity, content=b"not a JSON document\n")
    else:
        _rewrite(store, identity, value)
    return identity


def _unchanged(store, rows):
    return {row["id"]: (deepcopy(row), (store.root / row["path"]).read_bytes()) for row in rows}


@pytest.mark.parametrize("kind", ["film", "expression_template", "material_profile"])
@pytest.mark.parametrize("action", ["import", "copy", "overwrite"])
@pytest.mark.parametrize("opaque", [False, True])
def test_current_selected_exchange_preserves_unrelated_unsupported_target(tmp_path, kind, action, opaque):
    source = ProjectStore.create(tmp_path / "source", "Current source")
    selected = _resource(source, kind)
    target = ProjectStore.create(tmp_path / "target", "Current target")
    existing = _resource(target, kind, incoming=False) if action != "import" else None
    unsupported = _unsupported_source(target, opaque=opaque)
    original = _unchanged(target, target.manifest["documents"])
    before_source, before_target = file_snapshot(source.root), file_snapshot(target.root)
    resources = ProjectResourceService(target)
    catalog = resources.inspect_resources(source.project_file)
    plan = resources.preview_resource_transfer(catalog["catalog_id"], [selected])
    assert file_snapshot(target.root) == before_target
    capture = resources.resource_exchange._plans[plan["plan_id"]].target_records
    assert isinstance(capture, ScopedRecords)
    assert capture.base_records == target._index() and set(capture) == set(original)
    decisions = {row["id"]: action for row in plan["rows"] if row["conflict"]}
    result = resources.apply_resource_transfer(plan["plan_id"], decisions)
    copied = result["id_map"][selected]
    assert target.resolve_document(copied)["root_document_id"] == copied
    if action == "overwrite":
        assert copied == existing
    else:
        assert copied != existing
    replaced = set(result["replaced_ids"])
    for identity, (row, content) in original.items():
        assert (target.root / row["path"]).read_bytes() == content
        if identity not in replaced:
            assert target._index()[identity] == row
    assert target._index()[unsupported] == original[unsupported][0]
    assert target.manifest["default_configuration_id"] is None
    assert file_snapshot(source.root) == before_source
    with pytest.raises(ProjectError):
        target.resolve_document(unsupported)
    with pytest.raises(ProjectError):
        target.snapshot_saved_inputs()


def test_empty_film_import_does_not_need_unrelated_opaque_optical_identity(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    selected = source.save_film_document(new_design("No optical references"))
    target = ProjectStore.create(tmp_path / "target", "Target")
    optical = target.create_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1, "material": material()})
    _rewrite(target, optical, content=b"unreadable optical metadata")
    original = _unchanged(target, target.manifest["documents"])
    resources = ProjectResourceService(target)
    catalog = resources.inspect_resources(source.project_file)
    plan = resources.preview_resource_transfer(catalog["catalog_id"], [selected])
    result = resources.apply_resource_transfer(plan["plan_id"])
    assert target.resolve_document(result["id_map"][selected])["materials"] == []
    assert _unchanged(target, [target._index()[optical]]) == original


def test_selected_export_to_existing_named_descriptor_keeps_full_raw_target_and_source(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    selected = _resource(source, "film")
    _unsupported_source(source)
    target = ProjectStore.create(tmp_path / "target", "Target")
    unsupported = _unsupported_source(target, opaque=True)
    named = target.project_file.with_name("explicit-target.coatproj")
    target.project_file.rename(named)
    target = ProjectStore.open(named)
    original = _unchanged(target, target.manifest["documents"])
    before_source = file_snapshot(source.root)
    resources = ProjectResourceService(source)
    catalog = resources.inspect_resources("project")
    assert catalog["unavailable_rows"]
    plan = resources.preview_resource_transfer(catalog["catalog_id"], [selected], named)
    result = resources.apply_resource_transfer(plan["plan_id"])
    reopened = ProjectStore.open(named)
    assert reopened.resolve_document(result["id_map"][selected])["root_document_id"]
    assert _unchanged(reopened, [reopened._index()[unsupported]]) == original
    assert file_snapshot(source.root) == before_source
    assert not (target.root / "project.coatproj").exists()


@pytest.mark.parametrize("kind", ["film", "expression_template", "optical_material", "material_profile"])
@pytest.mark.parametrize("identity_form", ["opaque", "noncanonical"])
def test_relevant_unknown_business_identity_refuses_preview(tmp_path, kind, identity_form):
    source = ProjectStore.create(tmp_path / "source", "Source")
    selected = _resource(source, "material_profile" if kind == "optical_material" else kind)
    target = ProjectStore.create(tmp_path / "target", "Target")
    target_root = _resource(target, "material_profile" if kind == "optical_material" else kind, incoming=False)
    unknown = (next(row["id"] for row in target.manifest["documents"] if row["kind"] == kind)
               if kind == "optical_material" else target_root)
    if identity_form == "opaque":
        _rewrite(target, unknown, content=b"unknown identity")
    else:
        value = target._read_record(target._index()[unknown])
        if kind in {"material_profile", "optical_material"}:
            value["material"]["id"] = " shared"
        else:
            value["template_id" if kind == "expression_template" else "id"] = " shared"
        _rewrite(target, unknown, value)
    resources = ProjectResourceService(target)
    catalog = resources.inspect_resources(source.project_file)
    before = file_snapshot(target.root)
    with pytest.raises(ProjectError) as rejected:
        resources.preview_resource_transfer(catalog["catalog_id"], [selected])
    assert rejected.value.code == "reference_unconfirmed"
    assert file_snapshot(target.root) == before


def test_duplicate_relevant_film_business_identity_is_not_arbitrarily_chosen(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    selected = source.save_film_document(new_design("Incoming", design_id="shared-film"))
    target = ProjectStore.create(tmp_path / "target", "Target")
    target.save_film_document(new_design("First", design_id="shared-film"))
    target.save_film_document(new_design("Second", design_id="shared-film"))
    resources = ProjectResourceService(target)
    catalog = resources.inspect_resources(source.project_file)
    before = file_snapshot(target.root)
    with pytest.raises(ProjectError) as rejected:
        resources.preview_resource_transfer(catalog["catalog_id"], [selected])
    assert rejected.value.code == "dependency_conflict"
    assert file_snapshot(target.root) == before


def test_known_nonmatching_duplicate_film_id_does_not_block_unrelated_import(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    selected = source.save_film_document(new_design("Incoming", design_id="incoming-film"))
    target = ProjectStore.create(tmp_path / "target", "Target")
    target.save_film_document(new_design("First existing", design_id="other-film"))
    target.save_film_document(new_design("Second existing", design_id="other-film"))
    _unsupported_source(target, opaque=True)
    original = _unchanged(target, target.manifest["documents"])
    resources = ProjectResourceService(target)
    catalog = resources.inspect_resources(source.project_file)
    plan = resources.preview_resource_transfer(catalog["catalog_id"], [selected])
    assert plan["rows"][0]["conflict"] == ""
    result = resources.apply_resource_transfer(plan["plan_id"])
    assert target.resolve_document(result["id_map"][selected])["root_document_id"]
    assert _unchanged(target, [target._index()[key] for key in original]) == original


def test_global_preview_retains_existing_strict_destination_identity_rules(tmp_path, monkeypatch):
    source = ProjectStore.create(tmp_path / "source", "Source")
    selected = source.save_film_document(new_design("Incoming", design_id="incoming-film"))
    existing = ProjectStore.create(tmp_path / "existing-global-records", "Global snapshot")
    existing.save_film_document(new_design("File-backed design", design_id="shared-film"))
    existing.save_film_document(new_design("Database-backed design", design_id="shared-film"))
    snapshot = existing.snapshot_saved_inputs()
    monkeypatch.setattr("coat_sim.core.project_global_resources.read_global_resources", lambda: deepcopy(snapshot))
    resources = ProjectResourceService(source)
    catalog = resources.inspect_resources("project")
    before = file_snapshot(source.root)
    plan = resources.preview_resource_transfer(catalog["catalog_id"], [selected], "global")
    assert plan["rows"][0]["conflict"] == ""
    assert plan["rows"][0]["choices"] == ["import", "skip"]
    resources.cancel_resource_transfer(plan["plan_id"])
    assert file_snapshot(source.root) == before


@pytest.mark.parametrize("target_template_id,action", [("shared", "copy"), ("shared", "overwrite"), ("Shared", "import")])
def test_project_template_exact_id_conflicts_and_case_distinct_import_are_executable(tmp_path, target_template_id, action):
    source = ProjectStore.create(tmp_path / "source", "Source")
    template = {"schema_version": 1, "template_id": "shared", "name": "Incoming", "note": "",
                "expression": {"text": "A", "symbols": {"A": {
                    "material_id": "shared", "optical_set_id": "set",
                    "thickness": {"value": 100.0, "unit": "nm"}, "note": ""}}}}
    selected = source.create_document("expression_template", template, materials={"shared": material("shared")})
    target = ProjectStore.create(tmp_path / "target", "Target")
    existing = target.create_document("expression_template", {
        **deepcopy(template), "template_id": target_template_id, "name": "Existing"},
        materials={"shared": material("shared")})
    _unsupported_source(target)
    original = _unchanged(target, target.manifest["documents"])
    before_source = file_snapshot(source.root)
    resources = ProjectResourceService(target)
    catalog = resources.inspect_resources(source.project_file)
    plan = resources.preview_resource_transfer(catalog["catalog_id"], [selected])
    row = next(row for row in plan["rows"] if row["id"] == selected)
    if action == "import":
        assert row["conflict"] == "" and row["target_id"] == ""
    else:
        assert row["conflict"] == "different" and row["target_id"] == existing
    assert action in row["choices"]
    result = resources.apply_resource_transfer(plan["plan_id"], {selected: action})
    incoming = result["id_map"][selected]
    document = target.resolve_document(incoming)["documents"][0]["data"]
    if action == "overwrite":
        assert incoming == existing and document["template_id"] == "shared"
    elif action == "copy":
        assert incoming != existing and document["template_id"].casefold() != "shared"
    else:
        assert incoming != existing and document["template_id"] == "shared"
        assert target._read_record(target._index()[existing])["template_id"] == "Shared"
    for identity, (saved_row, content) in original.items():
        assert (target.root / saved_row["path"]).read_bytes() == content
        if identity not in result["replaced_ids"]:
            assert target._index()[identity] == saved_row
    assert file_snapshot(source.root) == before_source


def test_unknown_possible_optical_referrer_refuses_overwrite_atomically(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    selected = source.create_document("optical_material", {
        "kind": "coat_sim.project_optical_material", "version": 1, "material": material("shared", n=2.0)})
    target = ProjectStore.create(tmp_path / "target", "Target")
    target_film = _resource(target, "film", incoming=False)
    _rewrite(target, target_film, content=b"unknown possible material references")
    resources = ProjectResourceService(target)
    catalog = resources.inspect_resources(source.project_file)
    plan = resources.preview_resource_transfer(catalog["catalog_id"], [selected])
    before = file_snapshot(target.root)
    with pytest.raises(ProjectError) as rejected:
        resources.apply_resource_transfer(plan["plan_id"], {selected: "overwrite"})
    assert rejected.value.code == "reference_unconfirmed"
    assert file_snapshot(target.root) == before


@pytest.mark.parametrize("interruption", ["cancel", "stale_manifest", "failed_commit"])
def test_scoped_preview_cancel_stale_and_failed_commit_preserve_target(tmp_path, monkeypatch, interruption):
    source = ProjectStore.create(tmp_path / "source", "Source")
    selected = _resource(source, "film")
    target = ProjectStore.create(tmp_path / "target", "Target")
    _unsupported_source(target, opaque=True)
    resources = ProjectResourceService(target)
    catalog = resources.inspect_resources(source.project_file)
    original = file_snapshot(target.root)
    plan = resources.preview_resource_transfer(catalog["catalog_id"], [selected])
    assert file_snapshot(target.root) == original
    if interruption == "cancel":
        resources.cancel_resource_transfer(plan["plan_id"])
    elif interruption == "stale_manifest":
        winner = ProjectStore.open(target.project_file)
        winner.create_document("film", new_design("Concurrent winner"))
    else:
        def fail(*_args):
            raise PermissionError("Synthetic descriptor publication failure")
        monkeypatch.setattr("coat_sim.core.project_store.replace_with_retry", fail)
    before_apply = file_snapshot(target.root)
    with pytest.raises(ProjectError) as rejected:
        resources.apply_resource_transfer(plan["plan_id"])
    assert rejected.value.code in {"expired_plan", "stale_target", "stale_project", "io_error"}
    assert file_snapshot(target.root) == before_apply


def test_unavailable_selected_source_remains_rejected_without_writing_target(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    unavailable = _unsupported_source(source)
    target = ProjectStore.create(tmp_path / "target", "Target")
    resources = ProjectResourceService(target)
    catalog = resources.inspect_resources(source.project_file)
    assert unavailable in {row["id"] for row in catalog["unavailable_rows"]}
    before = file_snapshot(target.root)
    with pytest.raises(ProjectError) as rejected:
        resources.preview_resource_transfer(catalog["catalog_id"], [unavailable])
    assert rejected.value.code == "unsupported_resource"
    assert file_snapshot(target.root) == before


def test_full_reuse_requires_current_target_binary_and_verifies_its_payload(tmp_path):
    source = ProjectResourceService(ProjectStore.create(tmp_path / "source", "Source"))
    target = ProjectResourceService(ProjectStore.create(tmp_path / "target", "Target"))
    spectrum = tmp_path / "spectrum.csv"
    spectrum.write_text("500,1\n600,2\n")
    selected = source.capture_simulation_asset("spd_csv", spectrum)["document_id"]
    owned = target.capture_simulation_asset("spd_csv", spectrum)
    _unsupported_source(target.store)
    catalog = target.inspect_resources(source.store.project_file)
    plan = target.preview_resource_transfer(catalog["catalog_id"], [selected])
    assert plan["rows"][0]["conflict"] == "identical"
    payload = Path(owned["path"])
    payload.write_bytes(payload.read_bytes().replace(b"500,1", b"500,9"))
    before = file_snapshot(target.root)
    with pytest.raises(ProjectError) as rejected:
        target.apply_resource_transfer(plan["plan_id"])
    assert rejected.value.code == "integrity_error"
    assert file_snapshot(target.root) == before


def test_successful_full_reuse_keeps_target_revision_and_releases_asset_spool(tmp_path):
    source = ProjectResourceService(ProjectStore.create(tmp_path / "source", "Source"))
    target = ProjectResourceService(ProjectStore.create(tmp_path / "target", "Target"))
    spectrum = tmp_path / "spectrum.csv"
    spectrum.write_text("500,1\n600,2\n")
    selected = source.capture_simulation_asset("spd_csv", spectrum)["document_id"]
    owned = target.capture_simulation_asset("spd_csv", spectrum)["document_id"]
    _unsupported_source(target.store, opaque=True)
    catalog = target.inspect_resources(source.store.project_file)
    plan = target.preview_resource_transfer(catalog["catalog_id"], [selected])
    before = file_snapshot(target.root)
    result = target.apply_resource_transfer(plan["plan_id"])
    assert result["reused"] == 1 and result["id_map"][selected] == owned
    assert file_snapshot(target.root) == before
    assert target.resource_exchange._spool_bytes == 0


def test_full_reuse_validates_target_binary_envelope_not_just_business_key(tmp_path):
    source = ProjectResourceService(ProjectStore.create(tmp_path / "source", "Source"))
    target = ProjectResourceService(ProjectStore.create(tmp_path / "target", "Target"))
    spectrum = tmp_path / "spectrum.csv"
    spectrum.write_text("500,1\n600,2\n")
    selected = source.capture_simulation_asset("spd_csv", spectrum)["document_id"]
    owned = target.capture_simulation_asset("spd_csv", spectrum)["document_id"]
    value = target.store._read_record(target.store._index()[owned])
    value["unsupported_test_field"] = True
    _rewrite(target.store, owned, value)
    catalog = target.inspect_resources(source.store.project_file)
    plan = target.preview_resource_transfer(catalog["catalog_id"], [selected])
    before = file_snapshot(target.root)
    with pytest.raises(ProjectError) as rejected:
        target.apply_resource_transfer(plan["plan_id"])
    assert rejected.value.code == "invalid_document"
    assert file_snapshot(target.root) == before
