"""Small real-optics acceptance checks independent of search score flags."""
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_reduction import reduce_film_design
from tests.test_film_reduction import Service, layer, request

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def ar_objectives():
    return (
        FilmOptimizationObjective(500, 600, 5, angles_deg=(0.0, 20.0),
            polarization="s", pointwise=FilmObjectiveConstraint(
                mode="range", lower_bound=0.0, upper_bound=0.01)),
        FilmOptimizationObjective(500, 600, 5, angles_deg=(0.0, 20.0),
            polarization="p", metric="T", pointwise=FilmObjectiveConstraint(
                mode="range", lower_bound=0.99, upper_bound=1.0)),
    )


def independent_check(design, objectives):
    """Use a new service and direct physical responses, not result pass flags."""
    service = Service()
    for objective in objectives:
        for angle in objective.angles_deg:
            response = service.simulate(design, {
                "wavelength_min_nm": objective.wavelength_min_nm,
                "wavelength_max_nm": objective.wavelength_max_nm,
                "samples": 9, "angle_deg": angle,
                "polarization": objective.polarization,
                "first_surface": design["first_surface"],
            })
            np.testing.assert_array_equal(response["wavelength_nm"], np.linspace(500, 600, 9))
            actual = np.asarray(response[objective.metric])
            assert np.all(np.isfinite(actual))
            assert np.all(actual >= objective.pointwise.lower_bound)
            assert np.all(actual <= objective.pointwise.upper_bound)


def test_real_ar_thickness_reduction_preserves_r_t_angle_polarization_constraints():
    source = request([layer("ar", 120.0)], mode="thickness", lower=20.0,
        max_iterations=20, max_evaluations=100, objective=ar_objectives()[0])
    source = replace(source, base_request=replace(source.base_request, objectives=ar_objectives()))
    before = deepcopy(source.base_request.design_snapshot)
    result = reduce_film_design(Service(), source)
    assert result.state == "completed"
    assert result.summary["source_feasible"] and result.summary["reduction_achieved"]
    assert 20.0 <= result.best_thickness_nm["ar"] < 120.0
    assert result.metrics["candidate"]["layer_count"] == 1
    assert source.base_request.design_snapshot == before
    independent_check(before, ar_objectives())
    independent_check(result.best_design, ar_objectives())


@pytest.mark.parametrize("spacer_nm", [0.0, 0.001])
def test_real_zero_or_thin_high_spacer_deletes_then_same_reference_low_layers_merge(spacer_nm):
    source = request([layer("low-a", 60.0), layer("high-spacer", spacer_nm, "high"),
        layer("low-b", 60.0)], mode="layer_count", objective=ar_objectives()[0],
        max_iterations=8, max_evaluations=80)
    source = replace(source, base_request=replace(source.base_request, objectives=ar_objectives()))
    result = reduce_film_design(Service(), source)
    assert result.state == "completed"
    assert result.summary["source_feasible"] and result.summary["candidate_feasible"]
    assert result.summary["removed_layer_count"] == 2
    assert result.metrics["candidate"]["layer_count"] == 1
    surviving = result.best_design["front_layers"][0]
    assert surviving["material_id"] == "low" and surviving["optical_set_id"] == "low"
    # A legal repaired survivor may differ from an algebraic 120 nm merge.
    # Its physical responses, bounds and lower layer count establish success.
    assert surviving["thickness"]["unit"] == "nm"
    assert 0.0 < surviving["thickness"]["value"] <= 200.0
    independent_check(source.base_request.design_snapshot, ar_objectives())
    independent_check(result.best_design, ar_objectives())
