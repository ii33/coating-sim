"""Prepared analytic source data preserves public checking and exact semantics."""
from copy import deepcopy
import math
from typing import Any

import numpy as np
import pytest

from coat_sim.core import film_optimization_derivatives as derivatives
from coat_sim.core.film_design import physical_thickness_nm
from coat_sim.core.film_optimization import FilmThicknessVariable
from tests.test_film_optimization_structure import _service

pytestmark = pytest.mark.algorithm


def fixture(*, formula=False):
    service, design = _service()
    for material in service.materials._by_id.values():
        optical = material["optical_sets"][0]
        if material["id"] == "struct_glass":
            optical.update(wavelengths=[450., 550., 700.], n=[1.52, 1.5, 1.48], k=[0.] * 3)
        elif material["id"] == "struct_film":
            optical.update(wavelengths=[450., 550., 700.], n=[1.26, 1.23, 1.2], k=[.01, .02, .03])
        else:
            optical.update(wavelengths=[450., 550., 700.], n=[2.2, 2.1, 2.], k=[.03, .04, .06])
        if formula and material["id"] == "struct_film":
            for key in ("wavelengths", "n", "k"):
                optical.pop(key)
            optical.update(kind="cauchy", parameters={"A": 1.2, "B": .003, "C": 0.},
                k_data=dict(wavelength_unit="nm", wavelength_min=450., wavelength_max=700.,
                            wavelengths=[450., 700.], k=[.01, .03]))
    design["front_layers"].append(dict(id="top-high", material_id="struct_high", optical_set_id="high20",
        thickness=dict(value=70., unit="nm"), note="synthetic top layer"))
    settings = dict(wavelength_min_nm=500., wavelength_max_nm=650., samples=11,
                    angle_deg=25., polarization="unpolarized")
    frozen = service.freeze_design(design, settings)
    variables = tuple(FilmThicknessVariable("front", identifier, 0., 1000.)
        for identifier in ("top-high", "stable-host"))
    return design, frozen, variables, settings


def assert_same(left, right):
    assert set(left) == set(right)
    for name in left:
        np.testing.assert_allclose(left[name], right[name], rtol=5e-13, atol=5e-14)


def scalar_only(monkeypatch):
    prepare = derivatives.prepare_optical_set
    def scalar(*args, **kwargs):
        original = prepare(*args, **kwargs)
        return lambda *a, **k: original(*a, **k)
    monkeypatch.setattr(derivatives, "prepare_optical_set", scalar)


@pytest.mark.parametrize("polarization", ["s", "p", "unpolarized"])
@pytest.mark.parametrize("formula", [False, True])
def test_prepared_values_and_jacobians_match_original_scalar_sampling(monkeypatch, polarization, formula):
    design, frozen, variables, settings = fixture(formula=formula)
    settings["polarization"] = polarization
    with monkeypatch.context() as scalar_patch:
        scalar_only(scalar_patch)
        reference = derivatives.simulate_with_thickness_jacobian(design, frozen, variables, settings)
    prepared = derivatives._PreparedMaterialData(frozen, design)
    assert_same(derivatives.simulate_with_thickness_jacobian(design, prepared, variables, settings), reference)
    assert_same(derivatives.simulate_with_thickness_jacobian(design, frozen, variables, settings), reference)


def tracked_sampling(monkeypatch):
    counts = dict(prepare=0, scalar=0, many=0)
    prepare = derivatives.prepare_optical_set
    def tracked(*args, **kwargs):
        counts["prepare"] += 1
        sampler = prepare(*args, **kwargs)
        def scalar(*a, **k):
            counts["scalar"] += 1
            return sampler(*a, **k)
        def many(*a, **k):
            counts["many"] += 1
            return sampler.evaluate_many(*a, **k)
        wrapped: Any = scalar
        wrapped.evaluate_many = many
        return wrapped
    monkeypatch.setattr(derivatives, "prepare_optical_set", tracked)
    return counts


def test_static_optics_reuse_across_thickness_angle_and_polarization_keeps_variable_order(monkeypatch):
    design, frozen, variables, settings = fixture()
    counts = tracked_sampling(monkeypatch)
    prepared = derivatives._PreparedMaterialData(frozen, design)
    first = derivatives.simulate_with_thickness_jacobian(design, prepared, variables, settings)
    assert counts == dict(prepare=3, scalar=0, many=3)
    changed = deepcopy(design)
    changed["front_layers"][0]["thickness"]["value"] += 7.
    reverse_variables = tuple(reversed(variables))
    second = derivatives.simulate_with_thickness_jacobian(changed, prepared, reverse_variables,
        {**settings, "angle_deg": 35., "polarization": "p"})
    assert counts == dict(prepare=3, scalar=0, many=3)
    ordinary = derivatives.simulate_with_thickness_jacobian(changed, frozen, reverse_variables,
        {**settings, "angle_deg": 35., "polarization": "p"})
    assert_same(second, ordinary)
    assert not np.allclose(first["R"], second["R"])
    normal_order = derivatives.simulate_with_thickness_jacobian(changed, prepared, variables,
        {**settings, "angle_deg": 35., "polarization": "p"})
    np.testing.assert_array_equal(np.asarray(normal_order["jacobian_R"])[:, ::-1], second["jacobian_R"])


def test_ordinary_public_payloads_recheck_digest_but_owned_context_is_isolated():
    design, frozen, variables, settings = fixture()
    original = deepcopy(frozen)
    prepared = derivatives._PreparedMaterialData(frozen, design)
    reference = derivatives.simulate_with_thickness_jacobian(design, prepared, variables, settings)
    frozen["materials"][0]["optical_sets"][0]["n"][0] += .5
    with pytest.raises(ValueError, match="digest does not match"):
        derivatives.simulate_with_thickness_jacobian(design, frozen, variables, settings)
    with pytest.raises(ValueError, match="digest does not match"):
        derivatives._PreparedMaterialData(frozen, design)
    assert_same(derivatives.simulate_with_thickness_jacobian(design, prepared, variables, settings), reference)
    # A boolean marker on an ordinary mapping cannot authorize reuse.
    forged = deepcopy(original)
    forged["_prepared_materials"] = True
    with pytest.raises(ValueError, match="digest does not match"):
        derivatives.simulate_with_thickness_jacobian(design, forged, variables, settings)


def test_constructor_copy_isolated_before_any_sampler_or_vector_is_created():
    design, frozen, variables, settings = fixture()
    prepared = derivatives._PreparedMaterialData(frozen, design)
    reference = derivatives.simulate_with_thickness_jacobian(design, frozen, variables, settings)
    frozen["materials"].clear()
    assert_same(derivatives.simulate_with_thickness_jacobian(design, prepared, variables, settings), reference)


def test_unknown_k_calculation_policy_and_same_id_contexts_do_not_share_sources(monkeypatch):
    design, frozen, variables, settings = fixture()
    rows = deepcopy(frozen["materials"])
    for row in rows:
        if row["id"] == "struct_film":
            row["optical_sets"][0]["k"] = None
    unknown = derivatives._PreparedMaterialData(rows, design)
    with monkeypatch.context() as scalar_patch:
        scalar_only(scalar_patch)
        expected = derivatives.simulate_with_thickness_jacobian(design, rows, variables, settings)
    assert_same(derivatives.simulate_with_thickness_jacobian(design, unknown, variables, settings), expected)
    for row in rows:
        if row["id"] == "struct_film":
            row["optical_sets"][0]["n"] = [1.8] * 3
    changed = derivatives._PreparedMaterialData(rows, design)
    changed_result = derivatives.simulate_with_thickness_jacobian(design, changed, variables, settings)
    assert not np.allclose(changed_result["R"], expected["R"])
    assert_same(derivatives.simulate_with_thickness_jacobian(design, unknown, variables, settings), expected)


def test_cache_key_has_exact_grid_use_k_and_extrapolation_and_arrays_cannot_be_mutated(monkeypatch):
    design, frozen, _, _ = fixture()
    counts = tracked_sampling(monkeypatch)
    prepared = derivatives._PreparedMaterialData(frozen, design)
    reference = design["front_layers"][0]
    waves = np.array([500., 575., 650.])
    def sample(grid=waves, *, use_k=True, policy="error"):
        return prepared._index_series(reference, grid, use_k=use_k, extrapolation_policy=policy)
    first = sample()
    assert sample(waves.copy()) is first
    with pytest.raises(ValueError):
        first[0] = 0j
    with pytest.raises(ValueError):
        first.flags.writeable = True
    changed = sample(np.array([500., 580., 650.]))
    assert changed is not first and changed[1] != first[1]
    no_k = sample(use_k=False)
    assert np.all(no_k.imag == 0) and np.all(first.imag > 0)
    assert sample() is first
    assert sample(policy="endpoint") is not first
    assert counts == dict(prepare=1, scalar=0, many=4)


def test_lru_capacity_and_oversized_series_change_only_retention(monkeypatch):
    design, frozen, _, _ = fixture()
    counts = tracked_sampling(monkeypatch)
    monkeypatch.setattr(derivatives, "_MAX_PREPARED_SERIES_ENTRIES", 2)
    monkeypatch.setattr(derivatives, "_MAX_PREPARED_SERIES_BYTES", 200)
    prepared = derivatives._PreparedMaterialData(frozen, design)
    reference = design["front_layers"][0]
    def sample(offset):
        return prepared._index_series(reference, np.array([500., 575. + offset, 650.]),
            use_k=True, extrapolation_policy="error")
    first = sample(0.)
    sample(1.)
    sample(2.)
    refreshed = sample(0.)
    assert refreshed is not first
    np.testing.assert_array_equal(first, refreshed)
    assert counts["many"] == 4
    monkeypatch.setattr(derivatives, "_MAX_PREPARED_SERIES_BYTES", 1)
    oversized1, oversized2 = sample(3.), sample(3.)
    assert oversized1 is not oversized2
    np.testing.assert_array_equal(oversized1, oversized2)
    assert counts["many"] == 6


def test_formula_sampling_falls_back_to_scalar_and_qw_reference_stays_scalar(monkeypatch):
    design, frozen, variables, settings = fixture(formula=True)
    counts = tracked_sampling(monkeypatch)
    prepared = derivatives._PreparedMaterialData(frozen, design)
    design["front_layers"][0]["thickness"] = dict(value=1., unit="qw")
    result = derivatives.simulate_with_thickness_jacobian(design, prepared, variables, settings)
    assert counts["scalar"] == settings["samples"] + 1
    sampler = prepared._sampler(design["front_layers"][0])
    n = sampler(design["reference_wavelength_nm"], "nm", include_k=True)[0]
    normalized = deepcopy(design)
    normalized["front_layers"][0]["thickness"] = dict(value=physical_thickness_nm(
        design["front_layers"][0], design["reference_wavelength_nm"], n), unit="nm")
    assert_same(result, derivatives.simulate_with_thickness_jacobian(normalized, frozen, variables, settings))


def test_vector_domain_failure_keeps_scalar_error_and_does_not_cache_failures(monkeypatch):
    design, frozen, _, _ = fixture()
    prepared = derivatives._PreparedMaterialData(frozen, design)
    reference = design["front_layers"][0]
    waves = np.array([500., 800.])
    sampler = prepared._sampler(reference)
    with pytest.raises(ValueError) as scalar:
        sampler(800., "nm", extrapolation_policy="error", include_k=True)
    for _ in range(2):
        with pytest.raises(ValueError) as vector:
            prepared._index_series(reference, waves, use_k=True, extrapolation_policy="error")
        assert str(vector.value) == str(scalar.value)
    # Same vector may be evaluated under a different explicit endpoint policy.
    result = prepared._index_series(reference, waves, use_k=True, extrapolation_policy="endpoint")
    assert np.all(np.isfinite(result))


@pytest.mark.parametrize("unit", ["nm", "um"])
def test_zero_probes_units_and_jacobian_finite_difference_are_unchanged(unit):
    design, frozen, variables, settings = fixture()
    prepared = derivatives._PreparedMaterialData(frozen, design)
    host = design["front_layers"][0]
    if unit == "um":
        host["thickness"] = dict(value=.1, unit="um")
    response = derivatives.simulate_with_thickness_jacobian(design, prepared, variables, settings)
    step = 1e-4
    shifted = deepcopy(design)
    shifted["front_layers"][1]["thickness"]["value"] += step
    moved = derivatives.simulate_with_thickness_jacobian(shifted, prepared, variables, settings)
    np.testing.assert_allclose((np.asarray(moved["R"]) - response["R"]) / step,
        np.asarray(response["jacobian_R"])[:, 0], rtol=1e-4, atol=1e-7)
    probe = deepcopy(design)
    probe["front_layers"][1]["thickness"]["value"] = 0.
    explicit = derivatives.simulate_with_thickness_jacobian(probe, prepared, variables, settings,
        zero_thickness_layer_ids=("top-high",))
    assert_same(explicit, derivatives.simulate_with_thickness_jacobian(probe, frozen, variables,
        settings, zero_thickness_layer_ids=("top-high",)))
    assert probe["front_layers"][1]["thickness"]["value"] == 0.


def test_critical_branch_rejection_and_variable_validation_are_not_cached_away():
    design, frozen, variables, settings = fixture()
    rows = deepcopy(frozen["materials"])
    for row in rows:
        if row["id"] == "struct_film":
            optical = row["optical_sets"][0]
            optical["n"] = [1.5 * math.sin(math.radians(45.))] * 3
            optical["k"] = [0.] * 3
    prepared = derivatives._PreparedMaterialData(rows, design)
    design["incident_medium"] = dict(kind="constant", n=1.5)
    for material_input in (rows, prepared):
        with pytest.raises(ValueError, match="critical layer"):
            derivatives.simulate_with_thickness_jacobian(design, material_input, variables,
                {**settings, "angle_deg": 45.})
        with pytest.raises(ValueError, match="unique"):
            derivatives.simulate_with_thickness_jacobian(design, material_input, (variables[0], variables[0]), settings)
