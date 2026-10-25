"""The shipped example must open offline, relocate and calculate real results."""
from pathlib import Path
import os
import shutil
import subprocess
from unittest.mock import patch
from typing import cast

import numpy as np
import pytest

from coat_sim.core.project_default_loading import load_project_default
from coat_sim.core.project_dependencies import ProjectError
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
    film_snapshot = plan.film_design_snapshot
    assert film_snapshot is not None
    configuration = film_snapshot["design"]["workflow_settings"]["optimization"]
    assert configuration["version"] == 2
    objective = configuration["objectives"][0]
    assert objective["pointwise"]["enabled"] and not objective["mean"]["enabled"]
    assert (objective["pointwise"]["lower_bound_percent"],
            objective["pointwise"]["upper_bound_percent"]) == (0., 1.3)
    assert not {"mode", "target_value_percent", "lower_bound_percent",
                "upper_bound_percent"} & set(objective)
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
        restored = optimization.exportOptimizationConfiguration()
        # The current example omits the optional synthesis section. Restoring
        # it must retain all saved criteria and add explicit editor defaults.
        assert {key: restored[key] for key in config} == config
        assert set(restored) == set(config) | {"synthesis"}
        explicit_false = {
            **restored,
            "objectives": [{**row, "pointwise": {**row["pointwise"], "strict_upper": False}}
                           for row in restored["objectives"]],
        }
        assert optimization.loadOptimizationConfiguration(explicit_false)
        assert optimization.exportOptimizationConfiguration() == restored
        synthesis = restored["synthesis"]
        assert synthesis == {
            "options": {**{key: None for key in (
                "max_cycles", "max_evaluations", "max_layers", "max_seconds",
                "minimum_nonzero_thickness_nm", "max_layer_thickness_nm",
                "max_total_thickness_nm")},
                "randomize_template_search": False, "structure_search_seed": None,
                "enable_thickness_reduction": True, "objective_progress_policy": None},
            "objective_progress_editor": {
                "enabled": False,
                "fields": {
                    "accepted_window_steps": "",
                    "relative_sse_progress_floor": "",
                    "relative_worst_progress_floor": "",
                    "stagnation_windows": "",
                },
            },
            "cpu_workers": 0,
            "forward_backend": "cpu",
            "forward_device": None,
            "use_current_seed": False,
            "assume_unknown_k_zero": False,
            "material_rows": [{
                "id": synthesis["material_rows"][0]["id"],
                "material_id": "demo-film",
                "optical_set_id": "visible",
            }],
        }
        assert synthesis["options"]["enable_thickness_reduction"] is True
        row_id = synthesis["material_rows"][0]["id"]
        assert len(row_id) == 32 and all(character in "0123456789abcdef" for character in row_id)
        assert optimization.loadOptimizationConfiguration(restored)
        assert optimization.exportOptimizationConfiguration() == restored
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


def test_example_builder_uses_current_optimization_configuration(tmp_path):
    from example.build_example import build
    descriptor = build(tmp_path / "fresh current example")
    store = ProjectStore.open(descriptor)
    for row in store.manifest["documents"]:
        store.resolve_document(row["id"])
    plan = load_project_default(store)
    film_snapshot = plan.film_design_snapshot
    assert film_snapshot is not None
    configuration = film_snapshot["design"]["workflow_settings"]["optimization"]
    assert configuration["version"] == 2
    objective = configuration["objectives"][0]
    assert objective["pointwise"]["enabled"] and not objective["mean"]["enabled"]
    assert objective["pointwise"]["lower_bound_percent"] == 0.
    assert objective["pointwise"]["upper_bound_percent"] == 1.3
    assert (objective["wavelength_min_nm"], objective["wavelength_max_nm"],
            objective["wavelength_samples"]) == (500., 600., 11)
    assert not {"mode", "target_value_percent", "lower_bound_percent",
                "upper_bound_percent"} & set(objective)


def test_example_empty_extrema_roundtrip_never_scans_full_spectrum_or_changes_integrals(example):
    from PySide6.QtGui import QGuiApplication
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
    app = QGuiApplication.instance() or QGuiApplication([])
    service = ProjectFilmService(example, global_material_library=EmptyCatalog())
    snapshot = load_project_default(example).film_design_snapshot
    assert snapshot is not None
    design = snapshot["design"]
    configuration = design["workflow_settings"]["simulation"]
    assert configuration["extrema_bands"] == []
    source = FilmDesignQuickController(service=service)
    assert source.session is not None
    assert source.session.request_replace_design(design)
    simulation = FilmSimulationQuickController(service=service, source_controller=source)
    try:
        assert simulation.exportSimulationConfiguration() == configuration
        simulation._result = service.simulate(design, configuration["parameters"])
        simulation._stale = False
        simulation._invalidate_calculation_items()
        simulation._invalidate_extrema_items()
        simulation._invalidate_spectrum_analysis()
        integrals = cast(list, simulation.calculationItems)
        assert integrals and all(row["status"] == "ok" for row in integrals)
        with patch("coat_sim.ui.film_simulation_quick_controller.calculate_spectrum_extrema",
                   side_effect=AssertionError("An empty extrema list must not scan any spectrum")):
            assert simulation.extremaItems == []
            analysis = cast(dict, simulation.spectrumAnalysis)
            assert all(analysis[key] == [] for key in
                       ("global_min", "global_max", "local_peaks", "local_valleys"))
        assert simulation.addExtremaBand()
        assert simulation.setExtremaBand(0, 500.0, 600.0)
        assert cast(list, simulation.extremaItems)[0]["status"] == "ok"
        assert cast(list, simulation.extremaItems)[0]["sample_count"] == 11
        assert simulation.calculationItems == integrals
        assert simulation.exportSimulationConfiguration()["calculation_bands"] == configuration["calculation_bands"]
        simulation.removeExtremaBand(0)
        assert simulation.extremaItems == []
        assert simulation.calculationItems == integrals
        assert simulation.exportSimulationConfiguration() == configuration
    finally:
        simulation.close()
        source.markSourceClosed()
        app.processEvents()


@pytest.mark.parametrize("preserve_project_bytes", [False, True], ids=["negative-control", "protected-checkout"])
def test_example_git_autocrlf_clone_preserves_hashed_inputs(tmp_path, preserve_project_bytes):
    """Exercise a real Git checkout; core.autocrlf=true models Windows conversion.

    The negative control proves that conversion would break the same fixture
    without the repository attributes. Git history is created only in tmp_path.
    """
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for the checkout regression")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_CONFIG_NOSYSTEM="1")

    def run(*args, cwd):
        return subprocess.run([git, *args], cwd=cwd, env=environment, check=True,
                              capture_output=True, text=True).stdout

    origin = tmp_path / "fixture-origin"
    origin.mkdir()
    relative_project = Path("example") / "synthetic-demo"
    shutil.copytree(EXAMPLE, origin / relative_project)
    if preserve_project_bytes:
        shutil.copyfile(EXAMPLE.parents[1] / ".gitattributes", origin / ".gitattributes")
    expected = {path.relative_to(EXAMPLE): path.read_bytes()
                for path in EXAMPLE.rglob("*") if path.is_file()}
    assert all(b"\r\n" not in content for content in expected.values())
    run("init", cwd=origin)
    run("config", "core.autocrlf", "false", cwd=origin)
    run("config", "core.attributesfile", os.devnull, cwd=origin)
    run("add", "--all", cwd=origin)
    run("-c", "user.name=Example checkout test", "-c", "user.email=example@invalid.test",
        "-c", "commit.gpgsign=false", "commit", "-m", "Isolated checkout fixture", cwd=origin)
    clone = tmp_path / "Windows-style checkout 工程"
    run("clone", "--no-local", "--config", "core.autocrlf=true",
        "--config", "core.attributesfile=" + os.devnull, str(origin), str(clone), cwd=tmp_path)
    assert run("config", "--get", "core.autocrlf", cwd=clone).strip() == "true"
    checked_project = clone / relative_project
    actual = {relative: (checked_project / relative).read_bytes() for relative in expected}
    descriptor = checked_project / "project.coatproj"
    if not preserve_project_bytes:
        assert all(actual[name] != content and b"\r\n" in actual[name]
                   for name, content in expected.items())
        with pytest.raises(ProjectError) as error:
            store = ProjectStore.open(descriptor)
            load_project_default(store)
        assert error.value.code == "integrity_error"
        return
    assert actual == expected  # Includes the descriptor and every managed asset.
    store = ProjectStore.open(descriptor)
    for row in store.manifest["documents"]:
        store.resolve_document(row["id"])
    plan = load_project_default(store)
    assert plan.configured and plan.film_design_snapshot is not None
