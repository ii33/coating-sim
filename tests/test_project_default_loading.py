"""Headless selected-root activation and explicit unconfigured states."""

from copy import deepcopy
import json
import shutil
from unittest.mock import patch

import pytest

from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_service import simulation_settings
from coat_sim.core.film_process_import import _sha256, build_film_process_import
from coat_sim.core.project_default_loading import load_project_default
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_store import ProjectStore
from tests.test_input_snapshot import write_json
from tests.test_project_core import film, material, setup_project


pytestmark = pytest.mark.contract


def test_empty_and_nonempty_unconfigured_projects_are_distinct(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Project")
    with patch.object(ProjectStore, "recover_session", side_effect=AssertionError("recovery read")):
        empty = load_project_default(store)
        assert empty.state == "empty_project"
        assert not empty.configured
        assert empty.configuration_id is None
        assert empty.graph is None
        assert empty.film_design_snapshot is None
        store.create_document("film", new_design("Saved but unselected"))
        unselected = load_project_default(store)
    assert unselected.state == "no_default"
    assert unselected.project_id == empty.project_id
    assert unselected.project_revision > empty.project_revision
    assert unselected.graph is None


def test_selected_configuration_is_only_opening_root_and_snapshot_is_detached(tmp_path):
    store, external, _, selected = setup_project(tmp_path)
    source = json.loads((external / "recipe.cproc").read_text())
    source["steps"][0]["target_nm"] = 9.0
    write_json(external / "recipe.cproc", source)
    other_process = store.import_document(external / "recipe.cproc")
    other = store.add_deposition_configuration("Not selected", other_process)
    # Explicitly saving the older configuration makes it the opening root;
    # creation order and unrelated subsequent writes do not choose it.
    store.save_document(selected, store.resolve_document(selected)["configuration"])
    store.create_document("film", new_design("Unrelated film"))
    store.record_recovery([other])

    with patch.object(ProjectStore, "recover_session", side_effect=AssertionError("recovery read")):
        plan = load_project_default(ProjectStore.open(store.project_file))
    assert plan.state == "ready" and plan.configured
    assert plan.configuration_id == selected
    graph = plan.graph
    assert graph is not None
    assert plan.process_document_id == graph["configuration"]["process_document_id"]
    assert graph["runtime"]["steps"][0]["target_nm"] == 1.0
    assert {row["id"] for row in graph["documents"]} == {
        row["id"] for row in store.resolve_default()["documents"]
    }
    assert plan.film_design_snapshot is None
    assert plan.film_materials == []

    exposed = plan.graph
    assert exposed is not None
    exposed["runtime"]["steps"][0]["target_nm"] = -1.0
    graph = plan.graph
    assert graph is not None and graph["runtime"]["steps"][0]["target_nm"] == 1.0
    store.save_document(other, store.resolve_document(other)["configuration"])
    graph = plan.graph
    assert graph is not None and graph["runtime"]["steps"][0]["target_nm"] == 1.0
    new_graph = load_project_default(store).graph
    assert new_graph is not None and new_graph["runtime"]["steps"][0]["target_nm"] == 9.0


def test_inline_film_is_returned_as_frozen_provenance_not_live_film_link(tmp_path):
    store, external, _, _ = setup_project(tmp_path)
    design = film(tmp_path / "film.cfilm")
    snapshot = {
        "kind": "coat_sim.frozen_film_design",
        "schema_version": 1,
        "design": design,
        "settings": simulation_settings({}),
        "materials": [material()],
    }
    snapshot["sha256"] = _sha256(snapshot)
    process = json.loads((external / "recipe.cproc").read_text())
    imported = build_film_process_import(
        snapshot, "front", {"film": 1}, store.resolve_default()["runtime"]["machine"]
    )
    process.update({key: value for key, value in imported.items() if key != "preview"})
    process["schema_version"] = 7
    write_json(external / "recipe.cproc", process)
    process_id = store.import_document(external / "recipe.cproc")
    config_id = store.add_deposition_configuration("With film", process_id)
    store.set_default_configuration(config_id)
    store.create_document("film", deepcopy(design), materials={"film": material()})

    plan = load_project_default(ProjectStore.open(store.project_file))
    assert plan.configuration_id == config_id
    assert plan.film_design_snapshot == snapshot
    assert plan.film_materials == snapshot["materials"]
    graph = plan.graph
    assert graph is not None
    assert all(row["kind"] != "film" for row in graph["documents"])
    exposed = plan.film_design_snapshot
    assert exposed is not None
    exposed["materials"].clear()
    assert plan.film_materials == snapshot["materials"]


def test_missing_or_corrupt_selected_dependency_fails_without_fallback(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    source = next(row for row in store.resolve_default()["documents"] if row["kind"] == "source")
    source_path = store.root / source["path"]
    source_path.write_bytes(b"{}\n")
    with pytest.raises(ProjectError) as error:
        load_project_default(store)
    assert error.value.code == "integrity_error"


def test_stale_graph_revision_is_rejected_before_activation(tmp_path):
    store, _, _, _ = setup_project(tmp_path)
    graph = store.resolve_default()
    graph["revision"] += 1
    with patch.object(store, "resolve_default", return_value=graph):
        with pytest.raises(ProjectError) as error:
            load_project_default(store)
    assert error.value.code == "stale_project"


@pytest.mark.parametrize("missing", ["configuration", "process", "runtime"])
def test_incomplete_selected_closure_is_rejected(tmp_path, missing):
    store, _, _, _ = setup_project(tmp_path)
    graph = store.resolve_default()
    if missing == "configuration":
        graph.pop("configuration")
    elif missing == "process":
        graph["documents"] = [row for row in graph["documents"] if row["kind"] != "process"]
    else:
        graph.pop("runtime")
    with patch.object(store, "resolve_default", return_value=graph):
        with pytest.raises(ProjectError) as error:
            load_project_default(store)
    assert error.value.code == "invalid_dependency_graph"


def test_matching_film_and_material_ids_in_two_projects_remain_isolated(tmp_path):
    design = film(tmp_path / "shared.cfilm")
    plans = []
    for folder, n in (("a", 2.0), ("b", 3.0)):
        root = tmp_path / folder
        root.mkdir()
        store, external, _, _ = setup_project(root)
        optical = material(n=n)
        snapshot = {
            "kind": "coat_sim.frozen_film_design",
            "schema_version": 1,
            "design": deepcopy(design),
            "settings": simulation_settings({}),
            "materials": [optical],
        }
        snapshot["sha256"] = _sha256(snapshot)
        process = json.loads((external / "recipe.cproc").read_text())
        imported = build_film_process_import(
            snapshot, "front", {"film": 1}, store.resolve_default()["runtime"]["machine"]
        )
        process.update({key: value for key, value in imported.items() if key != "preview"})
        process["schema_version"] = 7
        write_json(external / "recipe.cproc", process)
        process_id = store.import_document(external / "recipe.cproc")
        store.set_default_configuration(store.add_deposition_configuration("With film", process_id))
        plans.append(load_project_default(ProjectStore.open(store.project_file)))

    first, second = plans
    assert first.project_id != second.project_id
    first_film, second_film = first.film_design_snapshot, second.film_design_snapshot
    assert first_film is not None and second_film is not None
    assert first_film["design"]["id"] == second_film["design"]["id"]
    assert first.film_materials[0]["id"] == second.film_materials[0]["id"] == "film"
    assert first.film_materials[0]["optical_sets"][0]["n"] == [2.0, 2.0]
    assert second.film_materials[0]["optical_sets"][0]["n"] == [3.0, 3.0]


def test_project_move_keeps_selected_graph_self_contained(tmp_path):
    store, external, _, _ = setup_project(tmp_path)
    expected = load_project_default(store).graph
    shutil.rmtree(external)
    moved = tmp_path / "moved"
    shutil.move(store.root, moved)
    actual = load_project_default(ProjectStore.open(moved / store.project_file.name)).graph
    assert actual == expected
