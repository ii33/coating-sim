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


@pytest.mark.parametrize("basis", ["step", "samples", "legacy_samples"])
def test_saved_optimization_step_or_count_overrides_new_defaults(tmp_path, monkeypatch, basis):
    with optimizer(tmp_path, monkeypatch) as (_service, controller):
        assert controller.setObjectiveField("wavelength_step_nm", 5.)
        if basis != "step":
            assert controller.setObjectiveField("wavelength_samples", 31)
        saved = json.loads(json.dumps(controller.exportOptimizationConfiguration()))
        if basis == "legacy_samples":
            saved["objectives"][0].pop("sampling_basis")
            saved["objectives"][0].pop("wavelength_step_nm")
        original = deepcopy(saved)
        assert controller.setObjectiveField("wavelength_step_nm", 1.)
        assert controller.loadOptimizationConfiguration(saved)
        row = controller.selectedObjective
        assert row["wavelength_samples"] == (61 if basis == "step" else 31)
        assert row["sampling_basis"] == ("step" if basis == "step" else "samples")
        assert row["actual_wavelength_step_nm"] == (5. if basis == "step" else 10.)
        assert saved == original


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


def test_empty_legacy_configuration_uses_new_gui_defaults(tmp_path, monkeypatch):
    with optimizer(tmp_path / "optimization", monkeypatch) as (_service, controller):
        assert controller.setObjectiveField("wavelength_step_nm", 5.)
        assert controller.loadOptimizationConfiguration({"version": 1})
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
