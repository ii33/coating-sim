"""Contracts for managed, in-memory film and expression-template documents."""
from copy import deepcopy
import json

import pytest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_expression_store import _normalize_template
from coat_sim.core.project_store import ProjectError, ProjectStore

pytestmark = pytest.mark.contract


def file_snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def material(identifier="film", n=2.0):
    """Synthetic optical data only; never use a production catalog record."""
    return {"id": identifier, "name": "Synthetic " + identifier, "notes": "test fixture",
            "optical_sets": [{"id": "set", "name": "Synthetic set", "kind": "tabulated",
                              "wavelength_unit": "nm", "wavelength_min": 400., "wavelength_max": 700.,
                              "wavelengths": [400., 700.], "n": [n, n], "k": [0., 0.],
                              "source": "Synthetic analytical fixture", "notes": "Not production data"}]}


def filled_film(identifier="film"):
    design = new_design("Managed film")
    design["front_layers"] = [{"id": "layer", "material_id": identifier, "optical_set_id": "set",
                               "thickness": {"value": 100., "unit": "nm"}, "note": "test"}]
    return validate_design(design)


def expression_template(template_id="quarter-wave"):
    return {"schema_version": 1, "template_id": template_id, "name": "Quarter wave",
            "note": "synthetic template",
            "expression": {"text": "A", "symbols": {
                "A": {"material_id": "symbol_only", "optical_set_id": "set",
                      "thickness": {"value": 90., "unit": "nm"}, "note": "symbol-only layer"}}}}


def graph_row(graph, kind):
    return next(row for row in graph["documents"] if row["kind"] == kind)


def test_create_empty_and_filled_films_in_memory_without_sidecar_databases(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Film project")

    empty = new_design("Empty draft")
    empty_id = store.create_document("film", empty)
    empty["name"] = "caller mutation"
    empty_graph = ProjectStore.open(store.project_file).resolve_document(empty_id)
    assert graph_row(empty_graph, "film")["data"]["name"] == "Empty draft"
    assert empty_graph["materials"] == []
    assert graph_row(empty_graph, "film")["dependencies"] == []

    filled = filled_film()
    filled_id = store.create_document("film", filled, materials={"film": material()})
    filled["front_layers"][0]["thickness"]["value"] = 999.
    graph = ProjectStore.open(store.project_file).resolve_document(filled_id)
    assert graph_row(graph, "film")["data"]["front_layers"][0]["thickness"]["value"] == 100.
    assert graph["materials"] == [material()]
    assert {row["kind"] for row in graph["documents"]} == {"film", "optical_material"}

    # In-memory creation must not need a scratch .cfilm or a SQLite library.
    assert not list(tmp_path.glob("*.cfilm"))
    assert not [path for path in store.root.rglob("*") if path.is_file() and
                path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}]


def test_create_template_normalizes_payload_and_captures_symbol_only_material(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Template project")
    source = expression_template()
    expected = _normalize_template(source)
    identity = store.create_document("expression_template", source,
                                     materials={"symbol_only": material("symbol_only")})
    source["expression"]["symbols"]["A"]["thickness"]["value"] = 1.

    graph = ProjectStore.open(store.project_file).resolve_document(identity)
    template = graph_row(graph, "expression_template")
    assert template["data"] == expected
    assert template["data"] == {"schema_version": 1, "template_id": "quarter-wave",
                                 "name": "Quarter wave", "note": "synthetic template",
                                 "expression": {"text": "A", "symbols": {
                                     "A": {"material_id": "symbol_only", "optical_set_id": "set",
                                           "thickness": {"value": 90., "unit": "nm"},
                                           "note": "symbol-only layer"}}}}
    assert template["dependencies"] == [row["id"] for row in graph["documents"]
                                        if row["kind"] == "optical_material"]
    assert graph["materials"] == [material("symbol_only")]
    assert template["path"].endswith(".json")
    assert not list(tmp_path.glob("*.cfilm"))
    assert not [path for path in store.root.rglob("*") if path.is_file() and
                path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}]


def test_unsupported_kind_missing_and_conflicting_materials_roll_back_atomically(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Atomic project")
    baseline = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        store.create_document("source", new_design())
    assert error.value.code == "invalid_document"
    assert file_snapshot(store.root) == baseline

    with pytest.raises(ProjectError) as error:
        store.create_document("film", filled_film())
    assert error.value.code == "missing_dependency"
    assert file_snapshot(store.root) == baseline

    store.create_document("film", filled_film(), materials={"film": material()})
    baseline = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        store.create_document("film", filled_film("film"), materials={"film": material(n=3.)})
    assert error.value.code == "dependency_conflict"
    assert file_snapshot(store.root) == baseline


def test_save_film_atomically_adds_new_resource_closure_and_preserves_managed_id(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Save project")
    identity = store.create_document("film", new_design("Draft"))
    updated = filled_film()
    updated["name"] = "Saved film"
    store.save_document(identity, updated, materials={"film": material()})

    graph = ProjectStore.open(store.project_file).resolve_document(identity)
    row = graph_row(graph, "film")
    material_row = graph_row(graph, "optical_material")
    assert row["id"] == identity
    assert row["data"]["name"] == "Saved film"
    assert row["dependencies"] == [material_row["id"]]
    assert graph["materials"] == [material()]


def test_save_missing_or_conflicting_materials_preserves_prior_project(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Save rollback")
    identity = store.create_document("film", filled_film(), materials={"film": material()})
    # A separately managed film establishes the resource that the edited
    # document will reference, so the supplied differing value is a real clash.
    store.create_document("film", filled_film("new_material"),
                          materials={"new_material": material("new_material")})
    saved = graph_row(store.resolve_document(identity), "film")["data"]
    edited = deepcopy(saved)
    edited["name"] = "Not committed"
    edited["front_layers"][0]["material_id"] = "missing_material"
    baseline = file_snapshot(store.root)

    with pytest.raises(ProjectError) as error:
        store.save_document(identity, edited)
    assert error.value.code == "missing_dependency"
    assert file_snapshot(store.root) == baseline

    edited["front_layers"][0]["material_id"] = "new_material"
    with pytest.raises(ProjectError) as error:
        store.save_document(identity, edited, materials={
            "new_material": material("new_material", n=3.)})
    assert error.value.code == "dependency_conflict"
    assert file_snapshot(store.root) == baseline
    assert graph_row(ProjectStore.open(store.project_file).resolve_document(identity), "film")["data"] == saved


def test_template_ids_are_exact_case_sensitive_and_stable_on_update(tmp_path):
    first = ProjectStore.create(tmp_path / "first", "First")
    lower = ProjectStore.create(tmp_path / "lower", "Lower")
    resources = {"symbol_only": material("symbol_only")}
    first_id = first.create_document("expression_template", expression_template("Dose"),
                                     materials=resources)
    with pytest.raises(ProjectError) as error:
        first.create_document("expression_template", expression_template("Dose"),
                              materials=resources)
    assert error.value.code == "dependency_conflict"
    assert lower.create_document("expression_template", expression_template("Dose"),
                                 materials=resources)
    case_distinct_id = first.create_document("expression_template", expression_template("dose"),
                                             materials=resources)

    edited = expression_template("Dose")
    edited["name"] = "Updated name"
    edited["note"] = "still the same business identity"
    first.save_document(first_id, edited)
    updated = graph_row(first.resolve_document(first_id), "expression_template")["data"]
    assert updated["template_id"] == "Dose"
    assert updated["name"] == "Updated name"
    assert graph_row(first.resolve_document(case_distinct_id), "expression_template")["data"]["template_id"] == "dose"

    edited["template_id"] = "changed-id"
    baseline = file_snapshot(first.root)
    with pytest.raises(ProjectError) as error:
        first.save_document(first_id, edited)
    assert error.value.code == "dependency_conflict"
    assert file_snapshot(first.root) == baseline


def test_resolved_template_and_materials_are_detached_from_caller_mutations(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Detached values")
    input_template = expression_template("detached")
    input_material = material("symbol_only")
    identity = store.create_document("expression_template", input_template,
                                     materials={"symbol_only": input_material})
    input_material["optical_sets"][0]["n"][0] = 9.
    first = store.resolve_document(identity)
    first_row = graph_row(first, "expression_template")
    first_row["data"]["name"] = "mutated result"
    first["materials"][0]["optical_sets"][0]["n"][0] = 8.
    first["documents"].clear()

    second = store.resolve_document(identity)
    assert graph_row(second, "expression_template")["data"]["name"] == "Quarter wave"
    assert second["materials"] == [material("symbol_only")]
    assert len(second["documents"]) == 2


def test_template_export_create_round_trip_includes_explicit_resource_mapping(tmp_path):
    source = ProjectStore.create(tmp_path / "source", "Source")
    identity = source.create_document("expression_template", expression_template("roundtrip"),
                                      materials={"symbol_only": material("symbol_only")})
    exported_dir = tmp_path / "export"
    exported = source.export_document(identity, exported_dir)
    exported_template = next(row for row in exported["documents"]
                             if row["kind"] == "expression_template")
    exported_resources = [json.loads((exported_dir / row["path"]).read_text(encoding="utf-8"))["material"]
                          for row in exported["documents"] if row["kind"] == "optical_material"]
    second = ProjectStore.create(tmp_path / "second", "Second")
    copied_id = second.create_document(
        "expression_template",
        json.loads((exported_dir / exported_template["path"]).read_text(encoding="utf-8")),
        materials={row["id"]: row for row in exported_resources})

    original_graph = source.resolve_document(identity)
    copied_graph = ProjectStore.open(second.project_file).resolve_document(copied_id)
    assert graph_row(copied_graph, "expression_template")["data"] == graph_row(
        original_graph, "expression_template")["data"]
    assert copied_graph["materials"] == original_graph["materials"]
    assert len(copied_graph["documents"]) == 2


def test_unbound_template_draft_needs_no_materials_but_empty_expression_is_rejected(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Draft templates")
    empty = expression_template("empty")
    empty["expression"] = {"text": "", "symbols": {}}
    unbound = expression_template("unbound")
    unbound["expression"]["symbols"]["A"].update(material_id="", optical_set_id="")
    baseline = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        store.create_document("expression_template", empty)
    assert error.value.code == "invalid_document"
    assert file_snapshot(store.root) == baseline
    identity = store.create_document("expression_template", unbound)
    graph = store.resolve_document(identity)
    assert graph["materials"] == []
    assert graph_row(graph, "expression_template")["data"] == _normalize_template(unbound)


@pytest.mark.parametrize("binding", [
    {"material_id": "", "optical_set_id": "set"},
    {"material_id": "symbol_only", "optical_set_id": ""},
    {"material_id": "symbol_only", "optical_set_id": "missing-set"},
])
def test_partial_or_missing_template_symbol_bindings_roll_back(tmp_path, binding):
    store = ProjectStore.create(tmp_path / "project", "Rejected template")
    document = expression_template()
    document["expression"]["symbols"]["A"].update(binding)
    baseline = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        store.create_document("expression_template", document,
                              materials={"symbol_only": material("symbol_only")})
    assert error.value.code == "missing_dependency"
    assert file_snapshot(store.root) == baseline


@pytest.mark.parametrize("version", [True, 0, 2, "1"])
def test_unsupported_template_version_is_an_atomic_structured_error(tmp_path, version):
    store = ProjectStore.create(tmp_path / "project", "Version boundary")
    document = expression_template()
    document["schema_version"] = version
    baseline = file_snapshot(store.root)
    with pytest.raises(ProjectError) as error:
        store.create_document("expression_template", document,
                              materials={"symbol_only": material("symbol_only")})
    assert error.value.code == "unsupported_version"
    assert file_snapshot(store.root) == baseline


def test_noop_template_save_ignores_unused_materials_and_preserves_unrelated_members(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "No-op save")
    film_id = store.create_document("film", filled_film(), materials={"film": material()})
    document = expression_template()
    identity = store.create_document("expression_template", document,
                                     materials={"symbol_only": material("symbol_only"),
                                                "unused": material("unused")})
    baseline = file_snapshot(store.root)
    manifest = store.manifest
    saved_film = store.resolve_document(film_id)
    store.save_document(identity, document,
                        materials={"symbol_only": material("symbol_only"),
                                   "unused": material("unused")})
    assert store.manifest == manifest
    assert file_snapshot(store.root) == baseline
    assert store.resolve_document(film_id) == saved_film
    material_ids = {
        graph_row(store.resolve_document(row["id"]), "optical_material")["data"]["material"]["id"]
        for row in manifest["documents"] if row["kind"] == "optical_material"
    }
    assert material_ids == {"film", "symbol_only"}


@pytest.mark.parametrize("operation", ["create-film", "save-film", "create-material", "save-material"])
def test_in_memory_publication_enforces_reader_document_limit_atomically(tmp_path, monkeypatch, operation):
    from coat_sim.core import project_dependencies, project_store

    store = ProjectStore.create(tmp_path / "project", "Document capacity")
    draft = new_design("Small draft")
    identity = store.create_document("film", draft)
    unrelated = store.create_document("film", new_design("Unrelated film"))
    baseline = file_snapshot(store.root)
    original_manifest = store.manifest
    original_graph = store.resolve_document(unrelated)
    # A small reader limit makes the real per-document boundary deterministic
    # without allocating tens of megabytes in a contract test.
    monkeypatch.setattr(project_dependencies, "MAX_FILE_BYTES", 1500)
    monkeypatch.setattr(project_store, "MAX_FILE_BYTES", 1500)
    if operation.endswith("material"):
        document = filled_film()
        resource = material()
        resource["notes"] = "x" * 2000
        resources = {"film": resource}
    else:
        document = deepcopy(draft)
        document["note"] = "x" * 2000
        resources = None
    with pytest.raises(ProjectError) as error:
        if operation.startswith("create"):
            store.create_document("film", document, materials=resources)
        else:
            store.save_document(identity, document, materials=resources)
    assert error.value.code == "capacity_exceeded"
    assert store.manifest == original_manifest
    assert file_snapshot(store.root) == baseline
    assert ProjectStore.open(store.project_file).resolve_document(unrelated) == original_graph
    assert graph_row(store.resolve_document(identity), "film")["data"] == draft


def test_failed_manifest_publication_rolls_back_saved_template_and_new_material(tmp_path, monkeypatch):
    from coat_sim.core import project_store

    store = ProjectStore.create(tmp_path / "project", "Atomic resource save")
    document = expression_template()
    identity = store.create_document("expression_template", document,
                                     materials={"symbol_only": material("symbol_only")})
    updated = deepcopy(document)
    updated["expression"]["symbols"]["A"]["material_id"] = "new_material"
    baseline = file_snapshot(store.root)
    graph = store.resolve_document(identity)
    manifest = store.manifest

    def publication_fails(*_args):
        raise OSError("injected manifest publication failure")

    monkeypatch.setattr(project_store, "replace_with_retry", publication_fails)
    with pytest.raises(ProjectError) as error:
        store.save_document(identity, updated, materials={"new_material": material("new_material")})
    assert error.value.code == "io_error"
    assert store.manifest == manifest
    assert file_snapshot(store.root) == baseline
    assert ProjectStore.open(store.project_file).resolve_document(identity) == graph
