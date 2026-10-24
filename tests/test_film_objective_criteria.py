"""Band reductions conserve physical wavelength measure and analytic gradients."""
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from coat_sim.core.film_objective_criteria import (
    FilmObjectiveConstraint as Constraint,
    active_constraints,
    reduce_objective_responses,
    signed_bound_count,
    validate_objective_criteria,
    wavelength_measure,
)


def objective(*, mean=None, pointwise=None, **kwargs):
    fields = dict(wavelength_min_nm=0.0, wavelength_max_nm=1.0, wavelength_samples=3,
                  angles_deg=(0.0,), polarization="s", metric="R", weight=1.0,
                  normalization_scale=1.0,
                  mean=Constraint(enabled=False) if mean is None else mean,
                  pointwise=Constraint() if pointwise is None else pointwise)
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def value(target, *, enabled=True):
    return Constraint(enabled=enabled, mode="value", target_value=target,
                      lower_bound=None, upper_bound=None)


def frame(values, *, waves=(0.0, 0.1, 1.0), angle=0.0, index=0, derivative=None):
    result = dict(objective_index=index, angle_deg=angle, wavelength_nm=waves,
                  actual_values=values)
    if derivative is not None:
        result["jacobian_values_by_layer_id"] = {"layer": derivative}
    return result


def test_nonuniform_mean_is_physical_integral_and_pointwise_is_independent():
    np.testing.assert_allclose(wavelength_measure([0, .1, 1], 0, 1), [.05, .5, .45])
    band = objective(mean=Constraint(lower_bound=.55), pointwise=Constraint(enabled=False))
    result = reduce_objective_responses([band], [frame([.3, .9, .3])])
    assert result.conditions[0].actual == pytest.approx(.6)
    assert result.conditions[0].actual != pytest.approx(.5)
    assert result.conditions[0].wavelength_nm is None
    assert result.conditions[0].reduction == "mean"
    assert result.nominal_pass
    assert result.max_error == 0
    band.mean = Constraint(enabled=False)
    band.pointwise = Constraint(lower_bound=.55)
    result = reduce_objective_responses([band], [frame([.3, .9, .3])])
    assert not result.nominal_pass
    assert result.max_error == pytest.approx(.25)


def test_mean_is_independent_for_each_angle():
    band = objective(mean=Constraint(lower_bound=.6), pointwise=Constraint(enabled=False),
                     angles_deg=(0., 45.))
    result = reduce_objective_responses([band], [frame([.9]*3), frame([.5]*3, angle=45.)])
    assert [row.actual for row in result.conditions] == pytest.approx([.9, .5])
    assert not result.nominal_pass
    assert result.weighted_rms_error == pytest.approx(.1 / np.sqrt(2))


def test_disabled_draft_contributes_no_loss_or_derivative_or_rejection():
    invalid_disabled = Constraint(enabled=False, mode=cast(Any, "invalid"), target_value=float("nan"),
                                  lower_bound=-2, upper_bound=-3, strict_lower=True)
    band = objective(mean=invalid_disabled, pointwise=Constraint())
    result = reduce_objective_responses([band], [frame([.3, .9, .3], derivative=[1, 2, 3])], layer_ids=["layer"])
    assert len(result.conditions) == 3
    assert result.nominal_pass
    np.testing.assert_array_equal(result.residual_vector, 0)
    np.testing.assert_array_equal(result.jacobian_matrix, 0)


def test_mean_hinge_happens_after_reduction():
    band = objective(mean=Constraint(lower_bound=.55), pointwise=Constraint(enabled=False))
    result = reduce_objective_responses([band], [frame([.3, .9, .3], derivative=[1, 2, 3])], layer_ids=["layer"])
    np.testing.assert_array_equal(result.residual_vector, [0])
    np.testing.assert_array_equal(result.jacobian_matrix, [[0]])


def test_both_modes_share_fixed_band_mass_and_keep_different_bounds():
    band = objective(mean=Constraint(lower_bound=.55), pointwise=Constraint(upper_bound=.7), weight=7.)
    result = reduce_objective_responses([band], [frame([.3, .9, .3])])
    assert len(result.conditions) == 4
    assert result.condition_masses[0] == pytest.approx(.5)
    assert sum(result.condition_masses[1:]) == pytest.approx(.5)
    assert sum(result.signed_bound_masses) == pytest.approx(1)
    assert result.weighted_rms_error == pytest.approx(.1)
    assert signed_bound_count([band]) == 8
    assert tuple(name for name, _ in active_constraints(band)) == ("mean", "pointwise")


@pytest.mark.parametrize("mean,pointwise", [
    (Constraint(enabled=False), Constraint(enabled=False)),
    (Constraint(lower_bound=.8), Constraint(upper_bound=.7)),
    (Constraint(lower_bound=.5, strict_lower=True), Constraint(upper_bound=.5)),
    (value(.5), Constraint(lower_bound=.5, strict_lower=True)),
    (Constraint(lower_bound=.8, upper_bound=.7), Constraint(enabled=False)),
    (Constraint(lower_bound=-.1), Constraint(enabled=False)),
    (Constraint(mode="value", target_value=.5), Constraint(enabled=False)),
    (Constraint(target_value=.5), Constraint(enabled=False)),
    (replace(value(.5), strict_upper=True), Constraint(enabled=False)),
])
def test_invalid_or_impossible_enabled_constraints_are_rejected(mean, pointwise):
    with pytest.raises(ValueError):
        validate_objective_criteria(objective(mean=mean, pointwise=pointwise))


@pytest.mark.parametrize("constraint", [value(cast(Any, ".5")), Constraint(lower_bound=cast(Any, ".5")),
                                         Constraint(upper_bound=True)])
def test_enabled_constraint_numbers_are_real_values_without_string_or_bool_coercion(constraint):
    with pytest.raises(ValueError):
        validate_objective_criteria(objective(mean=constraint, pointwise=Constraint(enabled=False)))


def test_closed_intersection_is_allowed_but_strict_boundary_is_not_acceptable():
    band = objective(mean=value(.5), pointwise=Constraint(lower_bound=.5))
    assert reduce_objective_responses([band], [frame([.5]*3)]).nominal_pass
    band.mean = Constraint(enabled=False)
    band.pointwise = Constraint(lower_bound=.5, strict_lower=True)
    result = reduce_objective_responses([band], [frame([.5]*3)], tolerance=1.)
    assert result.max_error == 0
    assert not result.nominal_pass
    assert not result.solver_acceptable


def test_single_line_has_unit_measure_and_no_fabricated_mean_wavelength():
    band = objective(wavelength_min_nm=500., wavelength_max_nm=500., wavelength_samples=1,
                     mean=value(.6), pointwise=Constraint(enabled=False))
    result = reduce_objective_responses([band], [frame([.6], waves=[500.])])
    np.testing.assert_array_equal(result.condition_masses, [1.])
    assert result.conditions[0].wavelength_nm is None
    assert result.conditions[0].wavelength_min_nm == 500.
    assert result.coverage[0].wavelength_nm == (500.,)


def test_mean_jacobian_matches_finite_difference_and_signed_bounds():
    band = objective(mean=value(.4), pointwise=Constraint(enabled=False))
    values, derivative = np.array([.3, .9, .3]), np.array([.1, -.2, .4])
    result = reduce_objective_responses([band], [frame(values, derivative=derivative)], layer_ids=["layer"])
    step = 1e-6
    positive = reduce_objective_responses([band], [frame(values + step * derivative)])
    negative = reduce_objective_responses([band], [frame(values - step * derivative)])
    finite_difference = (positive.residual_vector - negative.residual_vector) / (2 * step)
    np.testing.assert_allclose(result.jacobian_matrix[:, 0], finite_difference, atol=1e-10)
    np.testing.assert_allclose(result.jacobian_matrix[:, 0], [.085])
    np.testing.assert_allclose(result.signed_bound_jacobian[:, 0], [.085, -.085])


def test_chunks_are_reduced_once_after_stitching():
    band = objective(mean=value(.4), pointwise=Constraint(lower_bound=.4))
    full = reduce_objective_responses([band], [frame([.3, .9, .3], derivative=[1, 2, 3])], layer_ids=["layer"])
    chunked = reduce_objective_responses([band], [
        frame([.3], waves=[0], derivative=[1]),
        frame([.9, .3], waves=[.1, 1], derivative=[2, 3]),
    ], layer_ids=["layer"])
    assert full.conditions == chunked.conditions
    assert full.coverage == chunked.coverage
    np.testing.assert_array_equal(full.residual_vector, chunked.residual_vector)
    np.testing.assert_array_equal(full.jacobian_matrix, chunked.jacobian_matrix)


def test_refining_constant_spectrum_does_not_increase_band_weight():
    band = objective(mean=value(.4), pointwise=value(.4))
    sparse = reduce_objective_responses([band], [frame([.6]*3)])
    waves = np.linspace(0, 1, 101)
    dense = reduce_objective_responses([band], [frame([.6]*101, waves=waves)])
    assert sparse.weighted_rms_error == pytest.approx(.2)
    assert dense.weighted_rms_error == pytest.approx(sparse.weighted_rms_error)
    assert sum(dense.condition_masses) == pytest.approx(1.)


def test_extreme_band_weights_keep_finite_log_mass_and_residuals():
    bands = [objective(pointwise=value(0.), weight=weight) for weight in (1e-300, 1e300)]
    result = reduce_objective_responses(bands, [frame([.5]*3), frame([.5]*3, index=1)])
    assert np.all(np.isfinite(result.signed_bound_log_masses))
    assert np.all(np.isfinite(result.residual_vector))
    assert result.weighted_rms_error == pytest.approx(.5)
    assert np.all(result.residual_vector[:3] > 0)
    assert np.all(result.condition_masses[:3] == 0)


@pytest.mark.parametrize("waves,values", [
    ([0, .1, .1, 1], [.5]*4), ([0, 1, .1], [.5]*3),
    ([0, float("nan"), 1], [.5]*3), ([0, .1, .999], [.5]*3),
    ([0, .1, 1], [.5]*2), ([0, .1, 1], [.5, float("nan"), .5]),
])
def test_incomplete_or_invalid_raw_samples_are_rejected(waves, values):
    with pytest.raises(ValueError):
        reduce_objective_responses([objective()], [frame(values, waves=waves)])


def test_missing_angle_duplicate_chunk_boundary_and_single_line_duplicates_rejected():
    with pytest.raises(ValueError):
        reduce_objective_responses([objective(angles_deg=(0., 45.))], [frame([.5]*3)])
    with pytest.raises(ValueError):
        reduce_objective_responses([objective()], [frame([.5, .5], waves=[0, .1]), frame([.5, .5], waves=[.1, 1])])
    with pytest.raises(ValueError):
        wavelength_measure([500, 500], 500, 500)


def test_maximum_nominal_and_guard_values_do_not_carry_quadrature_mass():
    band = objective(pointwise=Constraint(lower_bound=.5), weight=9.)
    result = reduce_objective_responses([band], [frame([.3, .9, .3])])
    assert result.max_error == pytest.approx(.2)
    assert result.weighted_max_error == pytest.approx(.6)
    np.testing.assert_allclose(result.signed_bound_values[::2], [.2, -.4, .2])
    assert not result.nominal_pass
    assert result.solver_acceptable is False
