"""Scalar FD probes preserve complete F but never become complete candidates."""
from copy import deepcopy
from dataclasses import replace
from threading import Event

import numpy as np
import pytest

from coat_sim.core import film_ideal_sensitivity as ideal
from coat_sim.core import _film_ideal_sensitivity_core as core
from coat_sim.core import film_optimization_derivatives as derivatives
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmThicknessVariable
from tests.test_film_ideal_sensitivity import problem

pytestmark = pytest.mark.algorithm


def scalar_fixture(*, layers=2, mode="absolute", metric="mixed", absorption=False,
                   polarization="unpolarized", unit="nm", samples=17):
    service, request = problem(two=True, mode=mode)
    if absorption:
        # Deliberately synthetic absorption, NOT ZnS/YbF3 process evidence.
        for identifier in ("low", "high"):
            service.materials.records[identifier]["optical_sets"][0]["k"] = [.001, .002, .003]
    design = deepcopy(request.design_snapshot)
    design["front_layers"] = [
        dict(id=f"film-{i}", material_id="low" if i % 2 == 0 else "high", optical_set_id="nk",
             thickness=dict(value=40.0 + 3 * i, unit="nm"), note="synthetic scalar-probe fixture")
        for i in range(layers)
    ]
    variables = tuple(FilmThicknessVariable("front", f"film-{i}", 0.0, 200.0)
                      for i in range(layers - 1))
    # Last layer is fixed; one free variable is not a sensitivity column.
    selected = tuple(f"film-{i}" for i in reversed(range(1, layers)))
    variables = variables + (FilmThicknessVariable("front", f"film-{layers - 1}",
                                                  40.0 + 3 * (layers - 1),
                                                  40.0 + 3 * (layers - 1)),)
    if unit != "nm":
        for i, row in enumerate(design["front_layers"]):
            value = row["thickness"]["value"]
            n = np.sqrt(1.5) if i % 2 == 0 else 2.2
            row["thickness"] = dict(value=value / 1000 if unit == "um" else value * 4 * n / 550,
                                    unit=unit)
    metrics = ("R", "T") if metric == "mixed" else (metric,)
    objectives = tuple(FilmOptimizationObjective(
        502.1 + i, 646.7 - i, samples - i, (40.0, 47.5), polarization, m, "range",
        lower_bound=0.0, weight=3.7 if i else 0.13,
        normalization_scale=0.21 if i else 0.83,
    ) for i, m in enumerate(metrics))
    request = replace(request, design_snapshot=design, variables=variables,
                      sensitivity_layer_ids=selected, objectives=objectives, max_evaluations=None,
                      objective_ids=tuple(f"objective-{i}" for i in range(len(objectives))))
    return ideal.freeze_film_ideal_sensitivity_request(service, request)


@pytest.mark.parametrize("layers", [2, 8, 16])
@pytest.mark.parametrize("mode", ["absolute", "relative"])
@pytest.mark.parametrize("metric", ["R", "T", "mixed"])
@pytest.mark.parametrize("absorption", [False, True])
def test_scalar_F_is_bit_exact_to_complete_path(layers, mode, metric, absorption, monkeypatch):
    monkeypatch.setattr(core, "BLOCK_SAMPLES", 7)
    frozen = scalar_fixture(layers=layers, mode=mode, metric=metric, absorption=absorption)
    full, scalar = core._Evaluator(frozen), core._Evaluator(frozen)
    expected = full.evaluate(frozen.design_snapshot).analysis.objective_value
    actual = scalar.scalar_objective(frozen.design_snapshot)
    assert actual.hex() == expected.hex()
    assert scalar.forward == full.forward == scalar.jacobian
    assert scalar.requested_coherent_value_solves == 0 < full.requested_coherent_value_solves
    assert len(scalar.cache) == 0


@pytest.mark.parametrize("polarization", ["s", "p", "unpolarized"])
@pytest.mark.parametrize("unit", ["nm", "um", "qw"])
def test_scalar_shared_units_polarizations_and_absorbing_T(polarization, unit):
    frozen = scalar_fixture(polarization=polarization, unit=unit, mode="relative", absorption=True)
    full, scalar = core._Evaluator(frozen), core._Evaluator(frozen)
    expected = full.evaluate(frozen.design_snapshot).analysis.objective_value
    assert scalar.scalar_objective(frozen.design_snapshot).hex() == expected.hex()
    settings = dict(wavelength_min_nm=502.1, wavelength_max_nm=646.7, samples=17,
                    angle_deg=47.5, polarization=polarization)
    response = derivatives.simulate_with_thickness_jacobian(
        frozen.design_snapshot, full.materials, full.derivative_variables, settings)
    assert np.max(1 - np.asarray(response["R"]) - response["T"]) > .0001
    assert np.max(np.abs(np.asarray(response["jacobian_R"]) + response["jacobian_T"])) > 1e-7


def test_scalar_omits_forward_nominal_objects_and_new_cache(monkeypatch):
    frozen = scalar_fixture()
    evaluator = core._Evaluator(frozen)
    baseline = evaluator.evaluate(frozen.design_snapshot)
    cached = tuple(evaluator.cache.items())
    assert evaluator.scalar_objective(deepcopy(frozen.design_snapshot)) == baseline.analysis.objective_value
    assert evaluator.cache_hits == 1 and evaluator.evaluations == 1
    changed = core._candidate_design(frozen.design_snapshot, frozen.variables[:1], [40.01])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("scalar probe requested a complete nominal/value object")

    for name in ("simulate_with_thickness_jacobian", "FilmConditionError",
                 "FilmIdealSensitivityAnalysis", "_Evaluation"):
        monkeypatch.setattr(core, name, forbidden)
    monkeypatch.setattr(derivatives, "coherent_rta", forbidden)
    assert evaluator.scalar_objective(changed) > 0
    # Same scalar miss twice must still perform all its blocks, not use a new cache.
    blocks = evaluator.forward
    evaluator.scalar_objective(changed)
    assert evaluator.forward == 2 * blocks - 4
    assert tuple(evaluator.cache.items()) == cached
    assert evaluator.requested_coherent_value_solves == 4


@pytest.mark.parametrize("metric", ["R", "T"])
def test_both_union_Jacobians_still_checked_for_nonfinite(monkeypatch, metric):
    frozen = scalar_fixture(metric=metric)
    evaluator = core._Evaluator(frozen)
    original = core._simulate_thickness_jacobian_arrays

    def invalid(*args, **kwargs):
        waves, jac_r, jac_t = original(*args, **kwargs)
        # Corrupt an unselected variable-only column in the unused metric.
        (jac_t if metric == "R" else jac_r)[:, evaluator.columns.index("film-0")] = np.inf
        return waves, jac_r, jac_t

    monkeypatch.setattr(core, "_simulate_thickness_jacobian_arrays", invalid)
    with pytest.raises(ValueError, match="invalid response/Jacobian"):
        evaluator.scalar_objective(frozen.design_snapshot)
    assert evaluator.failures == 1 and not evaluator.cache


def test_relative_probe_recomputes_amplitude_even_with_constant_J(monkeypatch):
    frozen = scalar_fixture(mode="relative", metric="R")
    frozen = replace(frozen, sensitivity_layer_ids=("film-0",))
    evaluator = core._Evaluator(frozen)

    def constant_jac(_design, _materials, variables, _settings, *, wavelengths_nm):
        jac = np.full((len(wavelengths_nm), len(variables)), .01)
        return wavelengths_nm.copy(), jac, -jac

    monkeypatch.setattr(core, "_simulate_thickness_jacobian_arrays", constant_jac)
    center = evaluator.scalar_objective(frozen.design_snapshot)
    x1, x2 = core._bounded_stencil(40.0, 0.0, 200.0)
    fs = [evaluator.scalar_objective(core._candidate_design(
        frozen.design_snapshot, frozen.variables[:1], [x])) for x in (x1, x2)]
    actual = core._three_point_derivative(center, fs[0], fs[1], 40.0, x1, x2)
    assert actual == pytest.approx(2 * 40 * (.01 / .83) ** 2, rel=2e-10)
    assert fs[0] != fs[1]


@pytest.mark.parametrize("boundary", ["lower", "center", "upper"])
def test_scalar_existing_bounded_FD_gradient_matches_full(boundary):
    frozen = scalar_fixture(mode="relative", metric="mixed")
    value = {"lower": 0.0, "center": 40.0, "upper": 200.0}[boundary]
    design = core._candidate_design(frozen.design_snapshot, frozen.variables[:1], [value])
    scalar, full = core._Evaluator(frozen), core._Evaluator(frozen)
    x1, x2 = core._bounded_stencil(value, 0.0, 200.0)
    scalar_f, full_f = [], []
    for x in (value, x1, x2):
        changed = core._candidate_design(design, frozen.variables[:1], [x])
        scalar_f.append(scalar.scalar_objective(changed))
        full_f.append(full.evaluate(changed, probe=True).analysis.objective_value)
    assert scalar_f == full_f
    assert core._three_point_derivative(scalar_f[0], scalar_f[1], scalar_f[2], value, x1, x2) == core._three_point_derivative(
        full_f[0], full_f[1], full_f[2], value, x1, x2)


@pytest.mark.parametrize("failure", ["grid", "cancel", "budget"])
def test_partial_scalar_has_no_cache_or_returned_result(monkeypatch, failure):
    monkeypatch.setattr(core, "BLOCK_SAMPLES", 7)
    frozen = scalar_fixture(metric="R")
    if failure == "budget":
        frozen = replace(frozen, max_evaluations=1)
    event = Event()
    evaluator = core._Evaluator(frozen, event)
    original = core._simulate_thickness_jacobian_arrays
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        waves, jr, jt = original(*args, **kwargs)
        if failure == "grid":
            waves[1] = np.nextafter(waves[1], np.inf)
        if failure == "cancel":
            event.set()
        return waves, jr, jt

    monkeypatch.setattr(core, "_simulate_thickness_jacobian_arrays", interrupted)
    exception = {"grid": ValueError, "cancel": core._Cancelled, "budget": core._Budget}[failure]
    with pytest.raises(exception):
        evaluator.scalar_objective(frozen.design_snapshot)
    assert calls == 1 and evaluator.forward == evaluator.jacobian == 1
    assert evaluator.gradients == 0 and not evaluator.cache
    assert evaluator.requested_coherent_value_solves == 0


def test_scalar_finishes_on_exact_last_allowed_block(monkeypatch):
    monkeypatch.setattr(core, "BLOCK_SAMPLES", 7)
    frozen = scalar_fixture(metric="R")
    blocks = ideal.estimate_film_ideal_sensitivity_resources(frozen)["condition_blocks_per_evaluation"]
    frozen = replace(frozen, max_evaluations=blocks)
    evaluator = core._Evaluator(frozen)
    expected = core._Evaluator(frozen).evaluate(frozen.design_snapshot).analysis.objective_value
    assert evaluator.scalar_objective(frozen.design_snapshot) == expected
    assert evaluator.forward == blocks
    with pytest.raises(core._Budget):
        evaluator.scalar_objective(frozen.design_snapshot)
    assert evaluator.forward == blocks and not evaluator.cache


@pytest.mark.parametrize("jac_value,scale,error", [(1e-200, 1.0, "underflows"),
                                                   (1.0, 1e-300, "not finite")])
def test_scalar_preserves_objective_underflow_and_overflow(monkeypatch, jac_value, scale, error):
    frozen = scalar_fixture(metric="R")
    frozen = replace(frozen, objectives=tuple(replace(o, normalization_scale=scale)
                                             for o in frozen.objectives))
    evaluator = core._Evaluator(frozen)

    def constant_jac(_design, _materials, variables, _settings, *, wavelengths_nm):
        jac = np.full((len(wavelengths_nm), len(variables)), jac_value)
        return wavelengths_nm.copy(), jac, -jac

    monkeypatch.setattr(core, "_simulate_thickness_jacobian_arrays", constant_jac)
    with pytest.raises(ValueError, match=error):
        evaluator.scalar_objective(frozen.design_snapshot)
    assert evaluator.failures == 1 and not evaluator.cache
