"""Keep new GUI wavelength defaults at 1 nm without reinterpreting saved grids."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json

import pytest
import shiboken6

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_design_service import simulation_settings
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
from coat_sim.ui.film_simulation_settings import FilmSimulationSettingsDialog
from tests.film_structure_fixtures import fixture
from tests.test_film_optimization_target_sampling import _snapshot

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@contextmanager
def optimizer(tmp_path, monkeypatch):
    service, library, design = fixture(tmp_path)
    for material_id in ("acceptance-glass", "acceptance-film"):
        record = library.read(material_id)["record"]
        optical = record["optical_constants"][0]
        optical["wavelength_min"] = 400.
        optical["wavelengths"][0] = 400.
        library.write(record, update=True)
    controller = FilmOptimizationQuickController(_snapshot(service, design))
    monkeypatch.setattr(controller._synthesis_forward_probe, "probe", lambda: None)
    try:
        yield service, controller
    finally:
        assert controller.shutdown(1000)
        shiboken6.delete(controller)


@contextmanager
def simulation(tmp_path, configuration=None):
    service, _library, design = fixture(tmp_path)
    if configuration is not None:
        design["workflow_settings"] = {"simulation": deepcopy(configuration)}
    before = deepcopy(design)
    controller = FilmSimulationQuickController(
        root=tmp_path, service=service, design=design)
    try:
        assert design == before
        yield controller
    finally:
        controller.close()
        shiboken6.delete(controller)


@pytest.mark.parametrize(("upper", "count"), [(650., 251), (650.5, 252)])
def test_new_and_added_optimization_targets_keep_editable_one_nm_basis(
        tmp_path, monkeypatch, upper, count):
    with optimizer(tmp_path, monkeypatch) as (_service, controller):
        row = controller.selectedObjective
        assert (row["wavelength_min_nm"], row["wavelength_max_nm"]) == (400., 700.)
        assert (row["sampling_basis"], row["wavelength_step_nm"],
                row["actual_wavelength_step_nm"], row["wavelength_samples"]) == ("step", 1., 1., 301)
        assert controller.setObjectiveField("wavelength_max_nm", upper)
        assert controller.selectedObjective["wavelength_samples"] == count
        assert controller.selectedObjective["wavelength_step_nm"] == 1.
        assert controller.selectedObjective["actual_wavelength_step_nm"] == (upper - 400.) / (count - 1)
        controller.addObjective()
        assert controller.selectedObjective["wavelength_samples"] == 301
        assert controller.selectedObjective["wavelength_step_nm"] == 1.
        assert controller.setObjectiveField("wavelength_step_nm", 5.)
        assert controller.selectedObjective["wavelength_samples"] == 61
        assert controller.selectedObjective["wavelength_step_nm"] == 5.


@pytest.mark.parametrize(("field", "value", "samples"), [
    (None, None, 301),
    ("wavelength_step_nm", 5., 61),
    ("wavelength_samples", 31, 31),
    ("wavelength_max_nm", 400., 1),
])
def test_synthesis_freezes_the_exact_editable_ui_grid(tmp_path, monkeypatch, field, value, samples):
    with optimizer(tmp_path, monkeypatch) as (service, controller):
        pair = next(row for row in controller.structureMaterialOptions
                    if row["material_id"] == "acceptance-film")
        assert controller.setStructureMaterialSelected(
            pair["material_id"], pair["optical_set_id"], True)
        assert controller.setSearchMode("synthesis")
        if field is not None:
            assert controller.setObjectiveField(field, value)
        visible = deepcopy(controller.selectedObjective)
        frozen = freeze_film_synthesis_request(service, controller._build_synthesis_request())
        assert visible["wavelength_samples"] == samples
        objective = frozen.objectives[0]
        assert objective.wavelength_samples == samples
        assert objective.wavelength_min_nm == float(visible["wavelength_min_nm"])
        assert objective.wavelength_max_nm == float(visible["wavelength_max_nm"])
        if samples > 1:
            actual_step = (objective.wavelength_max_nm - objective.wavelength_min_nm) / (samples - 1)
            assert actual_step == visible["actual_wavelength_step_nm"]


@pytest.mark.parametrize("basis", ["step", "samples", "samples_with_inactive_step"])
def test_saved_optimization_step_or_count_overrides_new_defaults(tmp_path, monkeypatch, basis):
    with optimizer(tmp_path, monkeypatch) as (_service, controller):
        assert controller.setObjectiveField("wavelength_step_nm", 5.)
        if basis != "step":
            assert controller.setObjectiveField("wavelength_samples", 31)
        saved = controller.exportOptimizationConfiguration()
        assert saved["version"] == 3
        saved_row = saved["objectives"][0]
        assert {"pointwise", "mean"} <= set(saved_row)
        objective_id = saved_row["id"]
        if basis == "step":
            # The explicitly selected step owns this grid, even if a stored
            # derived point count disagrees with it.
            saved_row["wavelength_samples"] = 31
        elif basis == "samples_with_inactive_step":
            # A saved sample-count basis owns the grid; an inactive step value
            # cannot replace its explicit 31 points.
            saved_row["wavelength_step_nm"] = 5.
        raw_path = tmp_path / "native-optimization.json"
        raw_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        saved = json.loads(raw_path.read_text(encoding="utf-8"))
        original = deepcopy(saved)
        assert controller.setObjectiveField("wavelength_step_nm", 1.)
        assert controller.selectedObjective["wavelength_samples"] == 301
        assert controller.loadOptimizationConfiguration(saved)
        row = controller.selectedObjective
        assert row["id"] == objective_id
        assert row["wavelength_samples"] == (61 if basis == "step" else 31)
        assert row["sampling_basis"] == ("step" if basis == "step" else "samples")
        assert row["wavelength_step_nm"] == row["actual_wavelength_step_nm"] == (5. if basis == "step" else 10.)
        assert row["pointwise"] == original["objectives"][0]["pointwise"]
        assert row["mean"] == original["objectives"][0]["mean"]
        assert saved == original
        (tmp_path / "sampling-restore-evidence.json").write_text(json.dumps({
            "version": saved["version"], "objective_id": objective_id,
            "saved": {name: original["objectives"][0][name] for name in (
                "sampling_basis", "wavelength_samples", "wavelength_step_nm")},
            "restored": {name: row[name] for name in (
                "id", "sampling_basis", "wavelength_samples", "wavelength_step_nm", "actual_wavelength_step_nm")},
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def test_new_simulation_controller_preserves_one_nm_when_user_edits_the_band(tmp_path):
    with simulation(tmp_path) as controller:
        assert controller.settings["samples"] == 301
        assert controller.settings["step_nm"] == 1.
        controller.setParameter("wavelength_max_nm", 800.)
        assert controller.settings["samples"] == 401
        assert controller.settings["step_nm"] == 1.
        controller.setParameter("step_nm", 5.)
        assert controller.settings["samples"] == 81
        assert controller.settings["step_nm"] == 5.


@pytest.mark.parametrize(("sampling", "count", "step"), [
    ({}, 301, 400 / 300),
    ({"samples": 81}, 81, 5.),
    ({"step_nm": 5.}, 81, 5.),
])
def test_saved_simulation_explicit_grid_and_range_only_legacy_fallback_stay_intact(
        tmp_path, sampling, count, step):
    configuration = {"schema_version": 1, "parameters": {
        "wavelength_min_nm": 400., "wavelength_max_nm": 800., **sampling}}
    original = deepcopy(configuration)
    with simulation(tmp_path, configuration) as controller:
        assert controller.settings["samples"] == count
        assert controller.settings["step_nm"] == step
        saved = json.loads(json.dumps(controller.exportSimulationConfiguration()))
        assert controller.loadSimulationConfiguration(saved)
        assert controller.settings["samples"] == count
        assert controller.settings["step_nm"] == step
        assert configuration == original
    # The pure service uses the same legacy interpretation; no global default
    # rewrites a saved point count or a frozen benchmark request.
    normalized = simulation_settings(configuration["parameters"])
    assert normalized["samples"] == count and normalized["step_nm"] == step


def test_legacy_optimization_rejects_atomically_and_current_defaults_restore(tmp_path, monkeypatch):
    with optimizer(tmp_path / "optimization", monkeypatch) as (_service, controller):
        defaults = json.loads(json.dumps(controller.exportOptimizationConfiguration()))
        assert defaults["version"] == 3
        assert controller.setObjectiveField("wavelength_step_nm", 5.)
        edited = deepcopy(controller.exportOptimizationConfiguration())
        assert not controller.loadOptimizationConfiguration({"version": 1})
        assert controller.exportOptimizationConfiguration() == edited
        assert controller.selectedObjective["wavelength_samples"] == 61
        assert controller.selectedObjective["wavelength_step_nm"] == 5.
        assert controller.loadOptimizationConfiguration(defaults)
        assert controller.selectedObjective["id"] == defaults["objectives"][0]["id"]
        assert controller.selectedObjective["wavelength_samples"] == 301
        assert controller.selectedObjective["wavelength_step_nm"] == 1.
    with simulation(tmp_path / "simulation", {}) as controller:
        assert controller.settings["samples"] == 301
        assert controller.settings["step_nm"] == 1.


def test_widget_dialog_default_is_editable_but_range_only_saved_input_keeps_301_points():
    fresh = FilmSimulationSettingsDialog()
    legacy = FilmSimulationSettingsDialog({
        "wavelength_min_nm": 400., "wavelength_max_nm": 800.})
    try:
        assert fresh.step_spin.value() == 1. and fresh.samples_spin.value() == 301
        fresh.wavelength_max_spin.setValue(800.)
        assert fresh.step_spin.value() == 1. and fresh.samples_spin.value() == 401
        fresh.step_spin.setValue(5.)
        assert fresh.values()["samples"] == 81 and fresh.values()["step_nm"] == 5.
        assert legacy.values()["samples"] == 301
        assert legacy.values()["step_nm"] == 400 / 300
    finally:
        fresh.close()
        legacy.close()
        shiboken6.delete(fresh)
        shiboken6.delete(legacy)
