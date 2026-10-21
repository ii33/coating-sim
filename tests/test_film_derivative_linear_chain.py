"""Two-sweep R/T Jacobians against dense forward AD and physical differences."""
import math

import numpy as np
import pytest

from coat_sim.core import film_optimization_derivatives as derivatives
from coat_sim.core.film_optics import _beta, _flux_many, _interface_many, _longitudinal_many, coherent_rta

pytestmark = pytest.mark.algorithm


def dense_reference(waves, indices, thickness, ids, columns, angle, polarization):
    """Small original dense forward chain, retained only as a test oracle.

    Independently propagate every selected thickness derivative through the
    quotient rule; it deliberately does not use the new scalar transfer factors.
    """
    q = _longitudinal_many(indices, _beta(None, indices[:, 0], angle))
    r, t = _interface_many(indices[:, -2], indices[:, -1], q[:, -2], q[:, -1], polarization)
    dr = np.zeros((len(waves), len(columns)), dtype=complex)
    dt = np.zeros_like(dr)
    for position in range(len(thickness) - 1, -1, -1):
        phase = np.exp(2j * math.pi * q[:, position + 1] * thickness[position] / waves)
        z = phase * phase
        ri, ti = _interface_many(indices[:, position], indices[:, position + 1],
                                q[:, position], q[:, position + 1], polarization)
        dp = np.zeros_like(dr)
        if ids[position] in columns:
            dp[:, columns[ids[position]]] = phase * 2j * math.pi * q[:, position + 1] / waves
        dz = 2 * phase[:, None] * dp
        denominator = 1 + ri * r * z
        ddenominator = ri[:, None] * (dr * z[:, None] + r[:, None] * dz)
        numerator_r = ri + r * z
        numerator_t = ti * phase * t
        dr_next = ((dr * z[:, None] + r[:, None] * dz) * denominator[:, None]
                   - numerator_r[:, None] * ddenominator) / denominator[:, None] ** 2
        dt_next = (ti[:, None] * (dp * t[:, None] + phase[:, None] * dt) * denominator[:, None]
                   - numerator_t[:, None] * ddenominator) / denominator[:, None] ** 2
        dr, dt = dr_next, dt_next
        r, t = numerator_r / denominator, numerator_t / denominator
    flux = np.maximum(0., _flux_many(indices[:, -1], q[:, -1], polarization)) / _flux_many(indices[:, 0], q[:, 0], polarization)
    return (2 * np.real(np.conjugate(r)[:, None] * dr),
            2 * flux[:, None] * np.real(np.conjugate(t)[:, None] * dt))


def case(*, layers=5, absorbing=True, incident=1., exit_index=None):
    waves = np.linspace(420., 780., 13)
    samples = np.linspace(-1., 1., len(waves))
    if exit_index is None:
        exit_index = 1.52 + (.025j if absorbing else 0j)
    indices = np.column_stack([np.full(len(waves), incident), *[
        1.3 + .23 * index + .02 * samples + (1j * (.01 + .003 * index) if absorbing else 0j)
        for index in range(layers)], np.full(len(waves), exit_index)]).astype(complex)
    thickness = np.array([0., 23., 70., 110., 160., 44., 88.][:layers])
    ids = tuple(f"physical-{index}" for index in range(layers))
    return waves, indices, thickness, ids


@pytest.mark.parametrize("polarization", ["s", "p"])
@pytest.mark.parametrize("absorbing", [False, True])
@pytest.mark.parametrize("selection", ["all", "subset_reordered", "none"])
def test_chain_matches_dense_for_all_subset_reordered_zero_and_no_variables(polarization, absorbing, selection):
    waves, indices, thickness, ids = case(absorbing=absorbing)
    selected = ids if selection == "all" else (ids[3], ids[0], ids[4]) if selection == "subset_reordered" else ()
    columns = {identifier: column for column, identifier in enumerate(selected)}
    actual = derivatives._analytic_one_polarization(waves, indices, thickness, ids, columns, 37., polarization)
    expected = dense_reference(waves, indices, thickness, ids, columns, 37., polarization)
    for value, reference in zip(actual, expected, strict=True):
        assert value.shape == (len(waves), len(selected))
        np.testing.assert_allclose(value, reference, rtol=3e-11, atol=3e-14)


@pytest.mark.parametrize("polarization", ["s", "p"])
@pytest.mark.parametrize("absorbing", [False, True])
def test_both_power_jacobians_match_existing_value_solver_finite_differences(polarization, absorbing):
    waves, indices, thickness, ids = case(absorbing=absorbing)
    columns = {identifier: i for i, identifier in enumerate(ids)}
    jacobians = derivatives._analytic_one_polarization(waves, indices, thickness, ids, columns, 28., polarization)
    baseline = coherent_rta(waves, indices, thickness, angle_deg=28., polarization=polarization)
    for i, d in enumerate(thickness):
        step = 1e-5 if d == 0 else 1e-3
        high = thickness.copy()
        high[i] += step
        plus = coherent_rta(waves, indices, high, angle_deg=28., polarization=polarization)
        if d == 0:
            minus, denominator = baseline, step
        else:
            low = thickness.copy()
            low[i] -= step
            minus = coherent_rta(waves, indices, low, angle_deg=28., polarization=polarization)
            denominator = 2 * step
        for metric, jacobian in zip(("R", "T"), jacobians, strict=True):
            numeric = (np.asarray(plus[metric]) - minus[metric]) / denominator
            np.testing.assert_allclose(jacobian[:, i], numeric, rtol=2e-5, atol=3e-9)


@pytest.mark.parametrize("polarization", ["s", "p"])
@pytest.mark.parametrize("absorbing", [False, True])
def test_total_internal_reflection_exit_retains_flux_and_absorption_derivatives(polarization, absorbing):
    waves, indices, thickness, ids = case(absorbing=absorbing, incident=1.5, exit_index=1.)
    # Keep internal layers away from the exact-critical branch.
    indices[:, 1:-1] += .5
    columns = {ids[4]: 0, ids[0]: 1}
    actual = derivatives._analytic_one_polarization(waves, indices, thickness, ids, columns, 62., polarization)
    expected = dense_reference(waves, indices, thickness, ids, columns, 62., polarization)
    np.testing.assert_allclose(actual[0], expected[0], rtol=2e-10, atol=3e-14)
    np.testing.assert_array_equal(actual[1], np.zeros_like(actual[1]))
    if absorbing:
        assert np.max(np.abs(actual[0])) > 1e-6
    else:
        assert np.max(np.abs(actual[0])) < 1e-12
    high, low = thickness.copy(), thickness.copy()
    high[4] += 1e-3
    low[4] -= 1e-3
    plus = coherent_rta(waves, indices, high, angle_deg=62., polarization=polarization)
    minus = coherent_rta(waves, indices, low, angle_deg=62., polarization=polarization)
    for metric, jacobian in zip(("R", "T"), actual, strict=True):
        numeric = (np.asarray(plus[metric]) - minus[metric]) / .002
        np.testing.assert_allclose(jacobian[:, 0], numeric, rtol=2e-5, atol=2e-10)


@pytest.mark.parametrize("side", [-1., 1.])
@pytest.mark.parametrize("polarization", ["s", "p"])
def test_evanescent_and_propagating_sides_near_critical_match_dense(side, polarization):
    waves, indices, thickness, ids = case(incident=1.5, absorbing=False)
    beta = 1.5 * math.sin(math.radians(45.))
    indices[:, 1] = beta * (1 + side * 1e-6)
    thickness[0] = 45.
    columns = {ids[0]: 0, ids[3]: 1}
    actual = derivatives._analytic_one_polarization(waves, indices, thickness, ids, columns, 45., polarization)
    expected = dense_reference(waves, indices, thickness, ids, columns, 45., polarization)
    for value, reference in zip(actual, expected, strict=True):
        assert np.all(np.isfinite(value))
        np.testing.assert_allclose(value, reference, rtol=1e-8, atol=2e-12)
    high, low = thickness.copy(), thickness.copy()
    high[0] += .01
    low[0] -= .01
    plus = coherent_rta(waves, indices, high, angle_deg=45., polarization=polarization)
    minus = coherent_rta(waves, indices, low, angle_deg=45., polarization=polarization)
    for metric, jacobian in zip(("R", "T"), actual, strict=True):
        numeric = (np.asarray(plus[metric]) - minus[metric]) / .02
        np.testing.assert_allclose(jacobian[:, 0], numeric, rtol=2e-5, atol=2e-9)


@pytest.mark.parametrize("layers", [32, 64])
@pytest.mark.parametrize("polarization", ["s", "p"])
def test_many_layer_passive_chain_matches_dense_with_all_columns(layers, polarization):
    waves = np.linspace(430., 770., 7)
    indices = np.column_stack([np.ones(len(waves)), *[
        np.full(len(waves), (2.4 + .025j) if i % 2 else (1.35 + .003j))
        for i in range(layers)], np.full(len(waves), 1.5 + .01j)])
    thickness = 40. + np.arange(layers) * 3.
    ids = tuple(f"layer-{i}" for i in range(layers))
    columns = {identifier: i for i, identifier in enumerate(reversed(ids))}
    actual = derivatives._analytic_one_polarization(waves, indices, thickness, ids, columns, 33., polarization)
    expected = dense_reference(waves, indices, thickness, ids, columns, 33., polarization)
    for value, reference in zip(actual, expected, strict=True):
        np.testing.assert_allclose(value, reference, rtol=1e-8, atol=3e-15)


def test_empty_stack_shapes_and_original_critical_incident_singular_guards(monkeypatch):
    waves, indices, thickness, ids = case(layers=0)
    empty = derivatives._analytic_one_polarization(waves, indices, thickness, ids, {}, 0., "p")
    assert all(value.shape == (len(waves), 0) for value in empty)
    waves, indices, thickness, ids = case(layers=1, incident=1.5, absorbing=False)
    indices[:, 1] = 1.5 * math.sin(math.radians(45.))
    with pytest.raises(ValueError, match="critical layer"):
        derivatives._analytic_one_polarization(waves, indices, thickness, ids, {ids[0]: 0}, 45., "s")
    indices[:, 0] += .01j
    with pytest.raises(ValueError, match="incident port"):
        derivatives._analytic_one_polarization(waves, indices, thickness, ids, {ids[0]: 0}, 0., "s")
    indices[:] = 1.
    interface_calls = []
    def singular_interface(*args):
        interface_calls.append(1)
        return (np.full(len(waves), 1. if len(interface_calls) == 1 else -1., dtype=complex),
                np.ones(len(waves), dtype=complex))
    monkeypatch.setattr(derivatives, "_interface_many", singular_interface)
    with pytest.raises(ValueError, match="singular coherent stack"):
        derivatives._analytic_one_polarization(waves, indices, thickness, ids, {ids[0]: 0}, 0., "s")
