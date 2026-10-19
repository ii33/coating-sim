"""All enabled optical objectives contribute one weighted term per sampled condition."""

from dataclasses import replace
import math

import numpy as np
import pytest

from coat_sim.core.film_optimization import (
    FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable,
    optimize_film_design,
)
from coat_sim.core.film_optimization_scoring import candidate_key, search_scalar
from tests.film_structure_fixtures import fixture


def _objectives(*, angles=(0.0, 30.0), weight=1.0):
    return (
        FilmOptimizationObjective(500.0, 500.0, 1, target_value=0.0),
        FilmOptimizationObjective(600.0, 700.0, 2, angles_deg=angles,
                                  target_value=0.0, weight=weight),
    )


@pytest.mark.parametrize("solver", [
    "baseline", "powell", "nelder_mead", "multistart",
    "differential_evolution", "simulated_annealing", "trf", "lbfgsb",
])
def test_all_solver_entrypoints_score_both_objectives_by_condition(
        tmp_path, monkeypatch, solver):
    service, _library, design = fixture(tmp_path)

    def known_spectrum(_design, settings):
        waves = np.linspace(settings["wavelength_min_nm"],
                            settings["wavelength_max_nm"], settings["samples"])
        value = 0.1 if waves[0] == 500.0 else 0.2
        return {"wavelength_nm": waves, "R": np.full(waves.shape, value),
                "T": np.full(waves.shape, 1.0 - value)}

    monkeypatch.setattr(service, "simulate", known_spectrum, raising=False)
    layer_id = design["front_layers"][0]["id"]
    request = FilmOptimizationRequest(
        design_snapshot=design, design_revision=1,
        variables=(FilmThicknessVariable("front", layer_id, 0.0, 200.0),),
        objectives=_objectives(), solver=solver,
        max_evaluations=1, validation_samples_multiplier=1,
        acceptable_normalized_error=0.15,
    )
    result = optimize_film_design(service, request)
    assert result.state == "completed"
    assert len(result.per_condition_errors) == 5
    assert [row.objective_index for row in result.per_condition_errors] == [0, 1, 1, 1, 1]
    assert result.weighted_rms_error == pytest.approx(math.sqrt(0.17 / 5))
    assert result.max_normalized_error == pytest.approx(0.2)
    assert not result.targets_met
    assert not result.validation_targets_met


def test_sample_density_and_user_weight_multiply_point_contributions(tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)

    def known_spectrum(_design, settings):
        waves = np.linspace(settings["wavelength_min_nm"],
                            settings["wavelength_max_nm"], settings["samples"])
        value = 0.1 if waves[0] == 500.0 else 0.2
        return {"wavelength_nm": waves, "R": np.full(waves.shape, value),
                "T": np.full(waves.shape, 1.0 - value)}

    monkeypatch.setattr(service, "simulate", known_spectrum, raising=False)
    layer_id = design["front_layers"][0]["id"]
    base = FilmOptimizationRequest(
        design_snapshot=design, design_revision=1,
        variables=(FilmThicknessVariable("front", layer_id, 0.0, None),),
        objectives=_objectives(angles=(0.0,)),
        max_evaluations=1, validation_samples_multiplier=1,
        acceptable_normalized_error=0.15,
    )
    two_points = optimize_film_design(service, base)
    four_points = optimize_film_design(
        service, replace(base, objectives=_objectives()))
    doubled_weight = optimize_film_design(
        service, replace(base, objectives=_objectives(weight=2.0)))
    assert len(two_points.per_condition_errors) == 3
    assert len(four_points.per_condition_errors) == 5
    assert two_points.weighted_rms_error == pytest.approx(math.sqrt(0.09 / 3))
    assert four_points.weighted_rms_error == pytest.approx(math.sqrt(0.17 / 5))
    assert doubled_weight.weighted_rms_error == pytest.approx(math.sqrt(0.33 / 9))
    assert all(row.weighted_normalized_violation == pytest.approx(
        math.sqrt(2.0) * 0.2) for row in doubled_weight.per_condition_errors[1:])
    for result in (two_points, four_points, doubled_weight):
        assert result.max_normalized_error == pytest.approx(0.2)
        assert not result.targets_met


def test_weighted_search_keeps_unweighted_feasibility_separate():
    assert candidate_key(0.14, 0.12, 0.15) < candidate_key(0.16, 0.01, 0.15)
    assert search_scalar(0.14, 0.12, 0.15) < search_scalar(0.16, 0.01, 0.15)
    assert candidate_key(0.3, 0.11, 0.15) < candidate_key(0.2, 0.12, 0.15)


def test_baseline_ranking_uses_point_mass_and_explicit_weight(tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)

    def competing_spectrum(candidate, settings):
        waves = np.linspace(settings["wavelength_min_nm"],
                            settings["wavelength_max_nm"], settings["samples"])
        thickness = candidate["front_layers"][0]["thickness"]["value"]
        value = (0.3 - 0.001 * thickness if waves[0] == 500.0
                 else 0.0015 * thickness)
        return {"wavelength_nm": waves, "R": np.full(waves.shape, value),
                "T": np.full(waves.shape, 1.0 - value)}

    monkeypatch.setattr(service, "simulate", competing_spectrum, raising=False)
    layer_id = design["front_layers"][0]["id"]
    base = FilmOptimizationRequest(
        design_snapshot=design, design_revision=1,
        variables=(FilmThicknessVariable("front", layer_id, 0.0, 200.0),),
        objectives=_objectives(), max_iterations=1, max_evaluations=40,
        acceptable_normalized_error=0.0, validation_samples_multiplier=1,
    )
    default = optimize_film_design(service, base)
    emphasize_a = optimize_film_design(service, replace(
        base, objectives=(replace(base.objectives[0], weight=10.0),
                          base.objectives[1])))
    assert default.best_thickness_nm[layer_id] < 50.0
    assert emphasize_a.best_thickness_nm[layer_id] > 100.0
    assert default.max_normalized_error > 0.25
    assert not default.targets_met and not emphasize_a.targets_met
