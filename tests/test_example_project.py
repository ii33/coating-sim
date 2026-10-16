"""The shipped example must open offline, relocate and calculate real results."""
from pathlib import Path
import shutil
from unittest.mock import patch

import numpy as np
import pytest

from coat_sim.core.project_default_loading import load_project_default
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.process_simulator import simulate_process
from coat_sim.core.simulator import Settings

pytestmark = pytest.mark.contract
EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic-demo"


class EmptyCatalog:
    def catalog_records(self):
        return []


@pytest.fixture
def example(tmp_path):
    destination = tmp_path / "moved example 工程"
    shutil.copytree(EXAMPLE, destination)
    return ProjectStore.open(destination / "project.coatproj")


def test_example_has_only_current_inputs_and_complete_default(example):
    expected = {example.project_file} | {example.root / row["path"] for row in example.manifest["documents"]}
    assert {path for path in example.root.rglob("*") if path.is_file()} == expected
    assert not any(path.is_symlink() for path in EXAMPLE.rglob("*"))
    assert {row["kind"] for row in example.manifest["documents"]} == {
        "film", "optical_material", "source", "substrate", "mask", "machine", "process",
        "deposition_configuration"}
    before = {path: path.read_bytes() for path in expected}
    with patch.object(ProjectStore, "recover_session", side_effect=AssertionError("No recovery fallback")):
        plan = load_project_default(example)
        assert plan.configured and plan.configuration_id == example.manifest["default_configuration_id"]
        assert len(plan.film_materials) == 2
        for row in example.manifest["documents"]:
            example.resolve_document(row["id"])
    assert before == {path: path.read_bytes() for path in expected}
    for content in before.values():
        assert b"/tmp/" not in content and b"/workspace/" not in content
    graph = plan.graph
    assert graph is not None
    assert graph["runtime"]["errors"] == []
    assert len(graph["runtime"]["machine"]["masks"]) == 1
    for row in graph["documents"]:
        if row["kind"] in {"machine", "source", "mask"}:
            assert row["data"]["name"].startswith("Synthetic ")
            assert row["data"]["description"].strip()
            assert row["data"]["notes"].strip()


def test_example_cpu_deposition_and_optical_energy(example):
    plan = load_project_default(example)
    graph = plan.graph
    assert graph is not None
    parameters = graph["configuration"]["parameters"]
    result = simulate_process(Settings(**parameters["settings"]), graph["runtime"],
                              backend=parameters["backend"])
    values = result["field_nm"][np.isfinite(result["field_nm"])]
    assert result["field_nm"].shape == (21, 21)
    assert result["computed_time_steps"] == 12
    assert values.size > 0 and np.all(values >= 0.)
    assert result["mean_nm"] == pytest.approx(70.)
    assert result["min_nm"] == 0.  # The static plate casts an observable shadow.
    assert result["max_nm"] > result["mean_nm"]
    service = ProjectFilmService(example, global_material_library=EmptyCatalog())
    snapshot = plan.film_design_snapshot
    assert snapshot is not None
    design = snapshot["design"]
    result = service.simulate(design, design["workflow_settings"]["simulation"]["parameters"])
    assert len(result["R"]) == 31
    assert np.isfinite(result["R"]).all()
    assert min(result["R"]) == pytest.approx(0.01268888623, abs=1e-9)
    assert max(result["R"]) == pytest.approx(0.02537804302, abs=1e-9)
    np.testing.assert_allclose(np.array(result["R"]) + result["T"], 1., atol=1e-12)


def test_example_page_configurations_restore_without_guessing(example):
    from PySide6.QtGui import QGuiApplication
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    app = QGuiApplication.instance() or QGuiApplication([])
    service = ProjectFilmService(example, global_material_library=EmptyCatalog())
    source = FilmDesignQuickController(service=service)
    snapshot = load_project_default(example).film_design_snapshot
    assert snapshot is not None
    design = snapshot["design"]
    assert source.session is not None
    assert source.session.request_replace_design(design)
    simulation = FilmSimulationQuickController(service=service, source_controller=source)
    optimization = FilmOptimizationQuickController(source)
    try:
        assert simulation.exportSimulationConfiguration() == design["workflow_settings"]["simulation"]
        config = design["workflow_settings"]["optimization"]
        assert optimization.loadOptimizationConfiguration(config)
        assert optimization.exportOptimizationConfiguration() == config
        assert config["solver"]["max_evaluations"] == 40
        assert config["variables"] and config["objectives"]
        assert {row["layer_id"] for row in config["variables"]} == {"demo-layer"}
        from coat_sim.core.film_optimization import optimize_film_design
        request = optimization._build_request()
        result = optimize_film_design(service, request)
        assert result.state == "completed"
        assert 0 < result.evaluations <= 40
        assert 35. <= result.best_thickness_nm["demo-layer"] <= 105.
        print("Example optimization:", result.evaluations, result.best_thickness_nm,
              "targets_met=", result.targets_met)
    finally:
        optimization.markSourceClosed()
        simulation.close()
        source.markSourceClosed()
        app.processEvents()
