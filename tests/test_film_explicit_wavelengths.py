"""Exact original arrays reach both material/forward and analytic derivatives."""
from __future__ import annotations

import numpy as np
import pytest

from coat_sim.core.film_optimization_derivatives import (
    _PreparedMaterialData, simulate_with_thickness_jacobian,
)
from coat_sim.core.film_ideal_sensitivity import freeze_film_ideal_sensitivity_request
from tests.test_film_ideal_sensitivity import problem

pytestmark = pytest.mark.algorithm


def setup():
    service, request = problem(two=True)
    frozen = freeze_film_ideal_sensitivity_request(service, request)
    return frozen, _PreparedMaterialData(frozen.frozen_material_snapshot, frozen.design_snapshot)


def settings(waves, polarization='unpolarized'):
    return dict(wavelength_min_nm=float(waves[0]), wavelength_max_nm=float(waves[-1]),
                samples=len(waves), angle_deg=42., polarization=polarization, first_surface='front')


@pytest.mark.parametrize('polarization', ['s', 'p', 'unpolarized'])
def test_original_4098_nonbinary_grid_slices_and_default_results_are_bit_identical(polarization):
    request, materials = setup()
    waves = np.linspace(500., 600., 4098)
    full = simulate_with_thickness_jacobian(request.design_snapshot, materials, request.variables,
                                          settings(waves, polarization))
    explicit = simulate_with_thickness_jacobian(request.design_snapshot, materials, request.variables,
        settings(waves, polarization), wavelengths_nm=waves)
    for name in ('wavelength_nm', 'R', 'T', 'jacobian_R', 'jacobian_T'):
        np.testing.assert_array_equal(full[name], explicit[name])
    results = []
    for start in (0, 4096):
        chunk = waves[start:start+4096]
        results.append(simulate_with_thickness_jacobian(request.design_snapshot, materials,
            request.variables, settings(chunk, polarization), wavelengths_nm=chunk))
    for name in ('wavelength_nm', 'R', 'T', 'jacobian_R', 'jacobian_T'):
        np.testing.assert_array_equal(np.concatenate([np.asarray(row[name]) for row in results]), full[name])
    # The old default path retains its previous endpoint-based interpretation;
    # callers opt into explicit original arrays rather than silently changing it.
    old_chunk = simulate_with_thickness_jacobian(request.design_snapshot, materials, request.variables,
                                                settings(waves[:4096], polarization))
    assert np.count_nonzero(np.asarray(old_chunk['wavelength_nm']) != waves[:4096]) == 518


def test_irregular_and_repeated_exact_samples_use_one_wave_array_for_all_physics():
    request, materials = setup()
    waves = np.array([500., 503.9785208689285, 503.9785208689285, 517.13, 600.])
    response = simulate_with_thickness_jacobian(request.design_snapshot, materials, request.variables,
        settings(waves), wavelengths_nm=waves)
    np.testing.assert_array_equal(response['wavelength_nm'], waves)
    for index, wave in enumerate(waves):
        single = np.array([wave])
        one = simulate_with_thickness_jacobian(request.design_snapshot, materials, request.variables,
            settings(single), wavelengths_nm=single)
        for name in ('R', 'T', 'jacobian_R', 'jacobian_T'):
            np.testing.assert_array_equal(np.asarray(response[name])[index], np.asarray(one[name])[0])
    waves[:] = 550.
    assert response['wavelength_nm'][0] == 500.


@pytest.mark.parametrize('array', [
    '500,600', np.array([[500., 600.]]), [500., float('nan')], [500., float('inf')],
    [0., 600.], [-1., 600.], [600., 500.], [500.], [500., 550., 600.],
    [True, 600.], [500+0j, 600+0j], np.array([500., 600.], dtype=object),
    [501., 600.], [500., 599.],
])
def test_explicit_grid_rejects_invalid_domain_shape_type_length_order_or_endpoints(array):
    request, materials = setup()
    with pytest.raises(ValueError, match='explicit wavelengths_nm'):
        simulate_with_thickness_jacobian(request.design_snapshot, materials, request.variables,
            settings(np.array([500., 600.])), wavelengths_nm=array)
