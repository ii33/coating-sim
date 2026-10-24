"""Pure-memory optimizer regressions; SciPy remains the real installed package."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
import hashlib
import json
import math

import numpy as np
import pytest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import (
    FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable,
    _wavelength_chunks, optimize_film_design,
)
from coat_sim.core.film_optimization_job import _replay_candidate_scores
from coat_sim.core.film_optimization_needle import (
    FilmNeedleMaterialReference, FilmNeedleOptimizationRequest,
    _NeedleValidationCancelled, _score_design, _validate_request,
)
from coat_sim.core.film_optimization_scoring import (
    score_condition, weighted_rms_error,
)


class MemoryService:
    def __init__(self, *, mode="constant", cancel_after=None, event=None):
        self.mode = mode
        self.cancel_after = cancel_after
        self.event = event
        self.calls = []

    def freeze_design(self, design, _settings):
        payload = {
            "schema_version": 1,
            "kind": "coat_sim.frozen_film_design",
            "design": validate_design(deepcopy(design)),
            "materials": [{"id": "memory-glass"}, {"id": "memory-film"}],
        }
        payload["sha256"] = hashlib.sha256(json.dumps(
            payload, sort_keys=True, ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()
        return payload

    def simulate(self, design, settings):
        assert settings["samples"] <= 10001
        waves = np.linspace(settings["wavelength_min_nm"],
                            settings["wavelength_max_nm"], settings["samples"])
        if self.mode == "linear":
            thickness = float(design["front_layers"][0]["thickness"]["value"])
            value = 0.5 + 0.001 * (thickness - 100.0)
            values = np.full(waves.size, value)
        elif self.mode == "zero":
            values = np.zeros(waves.size)
        else:
            values = np.full(waves.size, 0.5)
        self.calls.append((dict(settings), waves.copy()))
        if self.event is not None and self.cancel_after == len(self.calls):
            self.event.set()
        return {"wavelength_nm": waves, "R": values, "T": 1.0 - values}


def make_design(thickness=100.0):
    design = new_design("In-memory film design", design_id="memory-film-design")
    design["substrate"].update(
        material_id="memory-glass", optical_set_id="memory-n15")
    design["front_layers"] = [{
        "id": "memory-layer", "material_id": "memory-film",
        "optical_set_id": "memory-n2",
        "thickness": {"value": float(thickness), "unit": "nm"}, "note": "",
    }]
    return design


def make_request(*, samples=3, target=0.0, scale=1.0, solver="baseline",
                 max_evaluations=1, thickness=100.0, low=50.0, high=150.0,
                 multiplier=1, acceptable=0.0, **changes):
    design = make_design(thickness)
    request = FilmOptimizationRequest(
        design_snapshot=design, design_revision=5,
        variables=(FilmThicknessVariable("front", "memory-layer", low, high),),
        objectives=(FilmOptimizationObjective(500.0, 600.0 if samples > 1 else 500.0, samples, pointwise=FilmObjectiveConstraint(mode="value", target_value=target, lower_bound=None, upper_bound=None), metric="R", normalization_scale=scale),),
        solver=solver, max_iterations=2, max_evaluations=max_evaluations,
        acceptable_normalized_error=acceptable,
        validation_samples_multiplier=multiplier,
        frozen_material_snapshot=None,
    )
    return replace(request, **changes)


def synthetic_jacobian(candidate, materials, variables, settings):
    del materials, variables
    thickness = float(candidate["front_layers"][0]["thickness"]["value"])
    waves = np.linspace(settings["wavelength_min_nm"],
                        settings["wavelength_max_nm"], settings["samples"])
    return {
        "wavelength_nm": waves,
        "R": np.full(waves.size, 0.5 + 0.001 * (thickness - 100.0)),
        "jacobian_R": np.full((waves.size, 1), 0.001),
    }


def test_weighted_rms_handles_subnormal_product_without_zeroing_result():
    single = weighted_rms_error([1e-200], [1e-300])
    pair = weighted_rms_error([1e-200, 2e-200], [1e-300, 1e-300])
    assert single > 0.0
    assert single == pytest.approx(1e-200, rel=1e-12, abs=0.0)
    assert pair > 0.0
    assert pair == pytest.approx(math.sqrt(2.5) * 1e-200,
                                 rel=1e-12, abs=0.0)
    assert weighted_rms_error([5e307], [1.0]) == pytest.approx(5e307)
    assert score_condition(0.5, 1e-308, 1.0)[0] == pytest.approx(5e307)


@pytest.mark.parametrize(("size", "expected"), [
    (10002, [10001, 1]),
    (10003, [10001, 2]),
    (20001, [10001, 10000]),
])
def test_global_chunk_slices_cover_each_sample_once(size, expected):
    waves = np.linspace(500.0, 600.0, size)
    chunks = list(_wavelength_chunks(waves))
    assert [len(chunk) for chunk in chunks] == expected
    combined = np.concatenate(chunks)
    assert np.array_equal(combined, waves)
    assert len(np.unique(combined)) == size
    one = list(_wavelength_chunks(np.array([550.0])))
    assert [chunk.tolist() for chunk in one] == [[550.0]]


@pytest.mark.parametrize("samples,multiplier,expected_chunks", [
    (5002, 2, [5002, 10001, 2]),  # 10003 validation wavelengths
    (10001, 2, [10001, 10001, 10000]),  # 20001 validation wavelengths
])
def test_main_holdout_scores_nonoverlapping_global_chunks(
        samples, multiplier, expected_chunks):
    service = MemoryService()
    request = make_request(samples=samples, target=0.0, max_evaluations=1,
                           multiplier=multiplier)
    result = optimize_film_design(service, request)
    assert result.validation_complete
    assert result.evaluations == 1
    assert [row[0]["samples"] for row in service.calls] == expected_chunks
    validation_waves = np.concatenate([waves for _settings, waves in service.calls[1:]])
    expected = np.linspace(500.0, 600.0, (samples - 1) * multiplier + 1)
    assert np.allclose(validation_waves, expected, rtol=0.0, atol=1e-9)
    assert len(np.unique(validation_waves)) == len(expected)
    assert len(result.validation_per_condition_errors) == len(expected)


def test_main_invalid_wavelength_band_has_compatibility_message():
    request = make_request(samples=2)
    invalid = replace(request.objectives[0],
                      wavelength_min_nm=600.0, wavelength_max_nm=500.0)
    request = replace(request, objectives=(invalid,))
    with pytest.raises(
            ValueError, match=r"^objective wavelength band must be positive and ordered$"):
        optimize_film_design(MemoryService(), request)


def test_gradient_baseline_and_initial_jacobian_both_spend_budget(monkeypatch):
    from coat_sim.core import film_optimization_derivatives

    derivative_calls = []
    def tracked(*args, **kwargs):
        derivative_calls.append(True)
        return synthetic_jacobian(*args, **kwargs)

    monkeypatch.setattr(
        film_optimization_derivatives, "simulate_with_thickness_jacobian", tracked)
    service = MemoryService()
    result = optimize_film_design(service, make_request(
        samples=1, target=0.0, solver="trf", max_evaluations=1))
    assert derivative_calls == []
    assert result.evaluations == 1
    assert result.iterations == 0
    assert result.termination_reason == "evaluation_budget"


def test_gradient_pair_lru_caches_failures_and_recharges_after_eviction(monkeypatch):
    from scipy import optimize as scipy_optimize
    from coat_sim.core import film_optimization_derivatives
    from coat_sim.core.film_optimization_solvers import SolverStop

    calls = []
    def derivative(candidate, materials, variables, settings):
        del materials, variables
        thickness = float(candidate["front_layers"][0]["thickness"]["value"])
        fraction = (thickness - 50.0) / 130.0
        calls.append(fraction)
        if fraction < 0.21:
            raise ArithmeticError("synthetic derivative failure")
        waves = np.array([settings["wavelength_min_nm"]])
        reflectance = 1.0 - 0.2 * (fraction - 0.6) ** 2
        return {"wavelength_nm": waves, "R": np.array([reflectance]),
                "jacobian_R": np.array([[0.4 * (fraction - 0.6) / 130.0]])}

    monkeypatch.setattr(
        film_optimization_derivatives, "simulate_with_thickness_jacobian", derivative)
    points = [1.0, 0.2, 0.2, 0.3, 0.4, 0.2, 0.5]

    def fake_least_squares(fun, x0, *, jac, bounds, method, x_scale,
                           max_nfev, ftol, xtol, gtol, callback):
        del x0, bounds, method, x_scale, max_nfev, ftol, xtol, gtol, callback
        for value in points:
            point = np.array([value])
            try:
                fun(point); jac(point); fun(point); jac(point)
            except SolverStop:
                break
        return SimpleNamespace(success=False, status=0)

    monkeypatch.setattr(scipy_optimize, "least_squares", fake_least_squares)
    service = MemoryService()
    result = optimize_film_design(service, make_request(
        samples=1, target=1.0, solver="trf", max_evaluations=6,
        thickness=180.0, high=180.0))
    assert calls == pytest.approx([1.0, 0.2, 0.3, 0.4, 0.2])
    assert result.evaluations == 6
    assert result.failed_evaluations == 2
    assert result.termination_reason == "evaluation_budget"
    assert result.best_thickness_nm["memory-layer"] == pytest.approx(102.0)
    assert result.max_normalized_error == pytest.approx(0.008)


@pytest.mark.parametrize("solver", [
    "baseline", "powell", "nelder_mead", "multistart",
    "differential_evolution", "simulated_annealing", "trf", "lbfgsb",
])
def test_all_eight_solver_entries_run_with_bounded_memory_objective(
        solver, monkeypatch):
    if solver in {"trf", "lbfgsb"}:
        from coat_sim.core import film_optimization_derivatives
        monkeypatch.setattr(
            film_optimization_derivatives, "simulate_with_thickness_jacobian",
            synthetic_jacobian)
    result = optimize_film_design(MemoryService(mode="linear"), make_request(
        samples=2, target=0.54, solver=solver, max_evaluations=8,
        max_iterations=2, acceptable=0.0, multistart_count=2,
        population_size=5, seed=19))
    assert result.solver == solver
    assert 1 <= result.evaluations <= 8
    assert math.isfinite(result.max_normalized_error)
    assert math.isfinite(result.weighted_rms_error)
    assert result.validation_complete


@pytest.mark.parametrize(("changes", "message"), [
    ({"solver": "unsupported"}, "solver must be one of"),
    ({"seed": -1}, "seed must be an integer"),
    ({"acceptable_normalized_error": math.inf}, "acceptable_normalized_error must be finite"),
    ({"initial_step_fraction": 2.0}, "step fractions"),
    ({"multistart_count": 0}, "multistart_count must be a positive integer"),
    ({"population_size": 0}, "population_size must be a positive integer"),
])
def test_needle_without_base_variables_uses_shared_controls(changes, message):
    base = make_request(samples=1, max_evaluations=4)
    base = replace(base, variables=(), **changes)
    request = FilmNeedleOptimizationRequest(
        base_request=base,
        candidate_materials=(FilmNeedleMaterialReference("memory-film", "memory-n2"),),
    )
    with pytest.raises(ValueError, match=message):
        _validate_request(request)


def test_needle_dense_score_chunks_and_cancellation_rollback():
    objective = FilmOptimizationObjective(500.0, 600.0, 20001, pointwise=FilmObjectiveConstraint(mode="value", target_value=0.0, lower_bound=None, upper_bound=None), metric="R")
    design = make_design()
    uncancelled = MemoryService()
    score = _score_design(uncancelled, design, (objective,))
    assert [row[0]["samples"] for row in uncancelled.calls] == [10001, 10000]
    assert len(score["errors"]) == 20001

    event = Event()
    cancelled_service = MemoryService(event=event, cancel_after=1)
    with pytest.raises(_NeedleValidationCancelled):
        _score_design(cancelled_service, design, (objective,), cancel_event=event)
    assert [row[0]["samples"] for row in cancelled_service.calls] == [10001]


def test_later_nonfinite_analytic_jacobian_is_charged_without_archiving(monkeypatch):
    from scipy import optimize as scipy_optimize
    from coat_sim.core import film_optimization_derivatives

    calls = []
    def derivative(candidate, materials, variables, settings):
        del materials, variables
        thickness = float(candidate["front_layers"][0]["thickness"]["value"])
        fraction = (thickness - 50.0) / 100.0
        calls.append(fraction)
        waves = np.array([settings["wavelength_min_nm"]])
        jac = np.array([[np.nan if fraction < 0.75 else 0.001]])
        return {"wavelength_nm": waves, "R": np.array([0.8 if fraction >= 0.75 else 0.95]),
                "jacobian_R": jac}

    monkeypatch.setattr(
        film_optimization_derivatives, "simulate_with_thickness_jacobian", derivative)
    def fake_least_squares(fun, x0, *, jac, bounds, method, x_scale,
                           max_nfev, ftol, xtol, gtol, callback):
        del x0, bounds, method, x_scale, max_nfev, ftol, xtol, gtol, callback
        point = np.array([0.5])
        fun(point)
        jac(point)
        return SimpleNamespace(success=False, status=0)
    monkeypatch.setattr(scipy_optimize, "least_squares", fake_least_squares)
    result = optimize_film_design(MemoryService(), make_request(
        samples=1, target=1.0, solver="trf", max_evaluations=5,
        thickness=150.0, high=150.0))
    assert calls == pytest.approx([1.0, 0.5])
    assert result.evaluations == 3
    assert result.failed_evaluations == 1
    assert result.best_thickness_nm["memory-layer"] == pytest.approx(150.0)
    assert result.max_normalized_error == pytest.approx(0.2)


@pytest.mark.parametrize("failure", ["cost", "gradient"])
def test_l_bfgs_b_rejects_nonfinite_cost_or_gradient_and_keeps_best(
        monkeypatch, failure):
    from scipy import optimize as scipy_optimize
    from coat_sim.core import film_optimization_derivatives

    calls = []
    def derivative(candidate, materials, variables, settings):
        del materials, variables
        thickness = float(candidate["front_layers"][0]["thickness"]["value"])
        fraction = thickness - 100.0
        calls.append(fraction)
        waves = np.array([settings["wavelength_min_nm"]])
        if fraction > 0.75:
            value, slope = 0.5, 0.0
        elif failure == "cost":
            value, slope = 0.0, 0.0
        else:
            value, slope = 0.0, 0.005
        return {"wavelength_nm": waves, "R": np.array([value]),
                "jacobian_R": np.array([[slope]])}

    monkeypatch.setattr(
        film_optimization_derivatives, "simulate_with_thickness_jacobian", derivative)
    def fake_minimize(fun, x0, *, jac, **kwargs):
        del x0, kwargs
        point = np.array([0.5])
        fun(point)
        jac(point)
        return SimpleNamespace(success=False, status=2)
    monkeypatch.setattr(scipy_optimize, "minimize", fake_minimize)
    result = optimize_film_design(MemoryService(mode="zero"), make_request(
        samples=1, target=0.5, scale=1e-308, solver="lbfgsb",
        max_evaluations=5, thickness=101.0, low=100.0, high=101.0))
    assert calls == pytest.approx([1.0, 0.5])
    assert result.evaluations == 3
    assert result.failed_evaluations == 1
    assert result.best_thickness_nm["memory-layer"] == pytest.approx(101.0)
    assert result.max_normalized_error == 0.0


def test_job_replay_keeps_objective_identity_with_global_chunking():
    design = make_design()
    objectives = (
        FilmOptimizationObjective(500.0, 600.0, 20001, pointwise=FilmObjectiveConstraint(mode="value", target_value=0.0, lower_bound=None, upper_bound=None), metric="R"),
        FilmOptimizationObjective(500.0, 600.0, 20001, pointwise=FilmObjectiveConstraint(mode="value", target_value=0.0, lower_bound=None, upper_bound=None), metric="R", weight=2.0),
    )
    service = MemoryService()
    result = _replay_candidate_scores(service, design, objectives)
    assert [row[0]["samples"] for row in service.calls] == [10001, 10000, 10001, 10000]
    assert len(result["per_condition_errors"]) == 40002
    assert {row["objective_index"] for row in result["per_condition_errors"]} == {0, 1}
