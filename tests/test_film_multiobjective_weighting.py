"""Independent band/angle/quadrature mass checks across optical solvers."""

from dataclasses import replace
import math

import numpy as np
import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable,
    optimize_film_design,
)
from coat_sim.core.film_optimization_scoring import candidate_key, search_scalar
from tests.film_structure_fixtures import fixture


def _objectives(*, angles=(0.0, 30.0), weight=1.0):
    return (
        FilmOptimizationObjective(500.0, 500.0, 1, pointwise=FilmObjectiveConstraint(mode="value", target_value=0.0, lower_bound=None, upper_bound=None)),
        FilmOptimizationObjective(600.0, 700.0, 2, pointwise=FilmObjectiveConstraint(mode="value", target_value=0.0, lower_bound=None, upper_bound=None), angles_deg=angles, weight=weight),
    )


@pytest.mark.parametrize("solver", [
    "baseline", "powell", "nelder_mead", "multistart",
    "differential_evolution", "simulated_annealing", "trf", "lbfgsb",
])
def test_all_solver_entrypoints_use_band_mass_and_keep_all_physical_conditions(
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
    # Equal user weights give each band half of the total mass. The second
    # band's two angles each have half of that band's mass; both endpoint
    # trapezoid masses are 1/2. Constant spectra therefore reduce to this RMS.
    assert result.weighted_rms_error == pytest.approx(math.sqrt((0.1**2 + 0.2**2) / 2))
    assert result.max_normalized_error == pytest.approx(0.2)
    assert not result.targets_met
    assert not result.validation_targets_met


def test_sampling_density_and_angle_count_preserve_band_mass_while_user_weight_changes_it(tmp_path, monkeypatch):
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
    dense_objectives = _objectives()
    dense_grid = optimize_film_design(service, replace(base, objectives=(dense_objectives[0],
        replace(dense_objectives[1], wavelength_samples=17))))
    assert len(two_points.per_condition_errors) == 3
    assert len(four_points.per_condition_errors) == 5
    assert len(dense_grid.per_condition_errors) == 35
    expected = math.sqrt((0.1**2 + 0.2**2) / 2)
    assert two_points.weighted_rms_error == pytest.approx(expected)
    assert four_points.weighted_rms_error == pytest.approx(expected)
    assert dense_grid.weighted_rms_error == pytest.approx(expected)
    assert doubled_weight.weighted_rms_error == pytest.approx(math.sqrt((0.1**2 + 2 * 0.2**2) / 3))
    assert all(row.weighted_normalized_violation == pytest.approx(
        math.sqrt(2.0) * 0.2) for row in doubled_weight.per_condition_errors[1:])
    for result in (two_points, four_points, doubled_weight, dense_grid):
        assert result.max_normalized_error == pytest.approx(0.2)
        assert not result.targets_met


def test_weighted_search_keeps_unweighted_feasibility_separate():
    assert candidate_key(0.14, 0.12, 0.15) < candidate_key(0.16, 0.01, 0.15)
    assert search_scalar(0.14, 0.12, 0.15) < search_scalar(0.16, 0.01, 0.15)
    assert candidate_key(0.3, 0.11, 0.15) < candidate_key(0.2, 0.12, 0.15)


def test_baseline_ranking_uses_equal_band_mass_and_explicit_user_weight(tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)

    def competing_spectrum(candidate, settings):
        waves = np.linspace(settings["wavelength_min_nm"],
                            settings["wavelength_max_nm"], settings["samples"])
        thickness = candidate["front_layers"][0]["thickness"]["value"]
        value = (0.3 - 0.001 * thickness if waves[0] == 500.0
                 else 0.003 * thickness)
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
    def independent_rms(thickness, weight_a=1.):
        # Each spectrum is constant, so every angle's normalized trapezoid
        # integral is exactly its squared response. Only the user weights enter.
        a, b = 0.3 - 0.001 * thickness, 0.003 * thickness
        return math.sqrt((weight_a * a**2 + b**2) / (weight_a + 1.))

    # Two candidates on the baseline's 12.5 nm screening grid reverse order.
    # Equal weights have their continuous minimum at 30 nm; A's tenfold weight
    # moves it to 10*.3*.001 / (10*.001**2 + .003**2) = 157.8947... nm.
    assert independent_rms(25.) < independent_rms(162.5)
    assert independent_rms(25., 10.) > independent_rms(162.5, 10.)
    default = optimize_film_design(service, base)
    emphasize_a = optimize_film_design(service, replace(
        base, objectives=(replace(base.objectives[0], weight=10.0),
                          base.objectives[1])))
    assert default.best_thickness_nm[layer_id] < 50.0
    assert emphasize_a.best_thickness_nm[layer_id] > 100.0
    assert default.max_normalized_error > 0.25
    for result, weight_a in ((default, 1.), (emphasize_a, 10.)):
        thickness = result.best_thickness_nm[layer_id]
        assert result.weighted_rms_error == pytest.approx(independent_rms(thickness, weight_a))
        assert result.max_normalized_error == pytest.approx(max(
            0.3 - 0.001 * thickness, 0.003 * thickness))
    assert not default.targets_met and not emphasize_a.targets_met


def test_nonconstant_spectrum_uses_trapezoid_endpoint_mass_within_each_band(tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    def known_spectrum(_design, settings):
        waves = np.linspace(settings['wavelength_min_nm'], settings['wavelength_max_nm'], settings['samples'])
        reflectance = np.full(waves.shape, .1) if waves[0] == 500. else np.array([.1, .3, .2])
        return dict(wavelength_nm=waves, R=reflectance, T=1. - reflectance)
    monkeypatch.setattr(service, 'simulate', known_spectrum, raising=False)
    layer_id = design['front_layers'][0]['id']
    objectives = _objectives(angles=(0.,), weight=3.)
    objectives = (objectives[0], replace(objectives[1], wavelength_samples=3))
    result = optimize_film_design(service, FilmOptimizationRequest(
        design_snapshot=design, design_revision=1,
        variables=(FilmThicknessVariable('front', layer_id, 0., 200.),),
        objectives=objectives, max_evaluations=1, validation_samples_multiplier=1,
        acceptable_normalized_error=.15))
    # q=(1/4,1/2,1/4), normalized band weights=(1/4,3/4).
    b_loss = .25 * .1**2 + .5 * .3**2 + .25 * .2**2
    assert result.weighted_rms_error == pytest.approx(math.sqrt(.25 * .1**2 + .75 * b_loss))
    assert len(result.per_condition_errors) == 4
    assert result.max_normalized_error == pytest.approx(.3)
    assert not result.targets_met and not result.validation_targets_met
