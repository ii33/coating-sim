"""Optional metadata remains separate from numerical inputs and file paths."""
from copy import deepcopy
import json
from typing import Any

import pytest
from pydantic import TypeAdapter

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_expression_store import _normalize_template
from coat_sim.core.project_dependencies import ProjectError, normalize_document
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.resource_information import with_resource_information
from coat_sim.core.schemas import MachineDesign, MaskDesign, SourceDesign, design_data
from coat_sim.core.source_profiles import validate_source_profile
from coat_sim.project_package import export_project, import_project
from tests.test_project_core import setup_project

pytestmark = pytest.mark.contract


def _without_information(kind, value):
    result = deepcopy(value)
    for key in (("name", "summary", "note") if kind in {"film", "expression_template"}
                else ("name", "description", "notes")):
        result.pop(key, None)
    return result


def test_optional_fields_do_not_rewrite_older_normalized_documents(tmp_path):
    store, _external, _process, _configuration = setup_project(tmp_path)
    for row in store.manifest["documents"]:
        data = store._read_record(row)
        assert normalize_document(row["kind"], data) == data
        if row["kind"] in {"source", "machine", "mask"}:
            assert "name" not in data
        if row["kind"] == "deposition_configuration":
            assert "description" not in data and "notes" not in data
    film = new_design()
    assert "summary" not in film
    assert validate_design(film) == film
    template = _normalize_template({"schema_version": 1, "template_id": "template",
        "name": "Expression", "note": "", "expression": {"text": "A", "symbols": {"A": {"material_id": "", "optical_set_id": "",
            "thickness": {"value": 100., "unit": "nm"}, "note": ""}}}})
    assert "summary" not in template
    assert _normalize_template(template) == template


@pytest.mark.parametrize("kind", ["source", "machine", "mask"])
def test_unnamed_legacy_designs_survive_plain_typed_json_roundtrips(tmp_path, kind):
    store, _external, _process, _configuration = setup_project(tmp_path)
    row = next(item for item in store.manifest["documents"] if item["kind"] == kind)
    original = store._read_record(row)
    assert "name" not in original
    model: Any = {"source": SourceDesign, "machine": MachineDesign, "mask": MaskDesign}[kind]
    adapter: TypeAdapter[Any] = TypeAdapter(model)
    typed = adapter.validate_python(original)
    assert "name" not in typed.model_dump()
    serialized = typed.model_dump_json()
    assert "name" not in json.loads(serialized)
    assert design_data(kind, json.loads(serialized)) == original
    assert design_data(kind, adapter.validate_json(serialized)) == original


@pytest.mark.parametrize("name", [None, "", "  ", "名" * 121])
def test_explicit_source_name_constraints_remain_at_core_and_information_boundaries(name):
    source = validate_source_profile({"schema_version": 2, "kind": "point"})
    with pytest.raises(ValueError):
        validate_source_profile({**source, "name": name})
    with pytest.raises(ValueError):
        TypeAdapter(SourceDesign).validate_python({**source, "name": name})
    with pytest.raises(ProjectError):
        with_resource_information("source", source, {"name": name})


@pytest.mark.parametrize("name", ["源", "名" * 120])
def test_explicit_valid_source_names_survive_typed_json_without_physical_changes(name):
    original = validate_source_profile({"schema_version": 2, "kind": "point"})
    named = with_resource_information("source", original, {"name": name})
    typed = TypeAdapter(SourceDesign).validate_python(named)
    assert design_data("source", json.loads(typed.model_dump_json())) == named
    assert named["name"] == name
    assert _without_information("source", named) == _without_information("source", original)


def test_three_fields_save_reopen_package_and_selective_transfer_without_numerical_changes(tmp_path):
    source, _external, _process, _configuration = setup_project(tmp_path)
    source.create_document("film", new_design("Film"))
    source.create_document("expression_template", {"schema_version": 1,
        "template_id": "template", "name": "Expression", "note": "",
        "expression": {"text": "A", "symbols": {"A": {"material_id": "", "optical_set_id": "",
            "thickness": {"value": 100., "unit": "nm"}, "note": ""}}}})
    api = ProjectResourceService(source)
    before = source.snapshot_saved_inputs()["documents"]
    expected = {}
    selected = []
    for row in source.manifest["documents"]:
        kind, identity = row["kind"], row["id"]
        original = api.resource_information(identity)
        result = api.save_resource_information(identity, {"name": "名称 " + kind,
            "summary": "概述：独立摘要", "notes": "备注：独立注释"}, expected_revision=original["token"])
        assert result["name"] == "名称 " + kind
        assert result["summary"] == "概述：独立摘要"
        assert result["notes"] == "备注：独立注释"
        assert result["token"] != original["token"]
        expected[identity] = api.load_document(identity).data
        assert _without_information(kind, expected[identity]) == _without_information(kind, before[identity])
        selected.append(identity)
    reopened = ProjectStore.open(source.project_file)
    assert reopened.snapshot_saved_inputs()["documents"] == expected
    archive = tmp_path / "metadata.cproject"
    export_project(source, archive)
    restored = import_project(archive, tmp_path / "restored")
    assert restored.snapshot_saved_inputs()["documents"] == expected
    target = ProjectResourceService(ProjectStore.create(tmp_path / "target", "Target"))
    catalog = target.inspect_resources(source.project_file)
    plan = target.preview_resource_transfer(catalog["catalog_id"], selected)
    result = target.apply_resource_transfer(plan["plan_id"])
    for identity in selected:
        info = target.resource_information(result["id_map"][identity])
        assert info["name"] == "名称 " + next(row["kind"] for row in source.manifest["documents"] if row["id"] == identity)
        assert info["summary"] == "概述：独立摘要"
        assert info["notes"] == "备注：独立注释"


@pytest.mark.parametrize("kind", ["source", "machine", "mask", "film", "expression_template", "deposition_configuration"])
def test_empty_names_are_rejected_and_stale_information_cannot_overwrite(tmp_path, kind):
    store, _external, _process, _configuration = setup_project(tmp_path)
    if kind == "film":
        identity = store.create_document("film", new_design())
    elif kind == "expression_template":
        identity = store.create_document(kind, {"schema_version": 1, "template_id": "template",
            "name": "Expression", "note": "", "expression": {"text": "A", "symbols": {"A": {"material_id": "", "optical_set_id": "",
            "thickness": {"value": 100., "unit": "nm"}, "note": ""}}}})
    else:
        identity = next(row["id"] for row in store.manifest["documents"] if row["kind"] == kind)
    api = ProjectResourceService(store)
    old = api.resource_information(identity)
    before = store.manifest
    with pytest.raises(ProjectError):
        api.save_resource_information(identity, {"name": "  "}, expected_revision=old["token"])
    assert store.manifest == before
    api.save_resource_information(identity, {"notes": "First edit"}, expected_revision=old["token"])
    with pytest.raises(ProjectError) as error:
        api.save_resource_information(identity, {"summary": "Stale edit"}, expected_revision=old["token"])
    assert error.value.code == "stale_document"
    assert api.resource_information(identity)["notes"] == "First edit"
