"""Small analytic objectives and a production analytic-Jacobian smoke."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core import film_synthesis_hr_minimax as minimax

pytestmark = pytest.mark.algorithm


def problem(*, total_cap=None):
    objectives = tuple(FilmOptimizationObjective(500., 600., 2, angles_deg=(0., 30.),
        polarization=pol, metric="R", mode="range", lower_bound=.9)
        for pol in ("s", "p"))
    request = FilmSynthesisRequest({}, objectives, (), minimum_nonzero_thickness_nm=.5,
        max_layer_thickness_nm=10., max_total_thickness_nm=total_cap)
    design = {"front_layers": [dict(id=f"layer-{i}", thickness=dict(value=1., unit="nm"))
                                for i in range(2)]}
    calls = []
    def evaluate(candidate, *, jacobian=True):
        assert jacobian
        d = np.array([row["thickness"]["value"] for row in candidate["front_layers"]])
        assert np.all(d >= .5) and np.all(d <= 10.)
        if total_cap is not None:
            assert sum(d) <= total_cap
        calls.append(d.copy())
        actual = np.array([.95 - .01*(d[0]-3)**2 - .003*(d[1]-2)**2,
                           .95 - .003*(d[0]-2)**2 - .01*(d[1]-3)**2])
        jac = np.array([[-.02*(d[0]-3), -.006*(d[1]-2)],
                        [-.006*(d[0]-2), -.02*(d[1]-3)]])
        responses = [dict(objective_index=i, angle_deg=angle, wavelength_nm=[500., 600.],
            actual_values=actual.tolist(), jacobian_values_by_layer_id={
                f"layer-{j}": jac[:, j].tolist() for j in range(2)})
            for i, objective in enumerate(objectives) for angle in objective.angles_deg]
        return dict(design=deepcopy(candidate), responses=responses,
                    score=dict(max_error=max(0., .9-float(min(actual))), marker="original"))
    initial = evaluate(design)
    calls.clear()
    return request, initial, evaluate, calls


def test_soft_min_analytic_derivative_and_beta_are_not_cached_as_loss():
    request, initial, evaluate, _ = problem()
    values, jac = minimax._responses(request, initial, ("layer-0", "layer-1"))
    loss100, derivative = minimax._soft_min_loss(values, jac, 100.)
    assert loss100 != minimax._soft_min_loss(values, jac, 1500.)[0]
    step = 1e-5
    for index in range(2):
        samples = []
        for direction in (-1, 1):
            design = deepcopy(initial["design"])
            design["front_layers"][index]["thickness"]["value"] += direction * step
            v, j = minimax._responses(request, evaluate(design), ("layer-0", "layer-1"))
            samples.append(minimax._soft_min_loss(v, j, 100.)[0])
        assert derivative[index] == pytest.approx((samples[1]-samples[0])/(2*step), rel=1e-7)


@pytest.mark.parametrize("cap", [None, 4.])
@pytest.mark.parametrize("method", ["SLSQP", "L-BFGS-B"])
def test_real_solver_improves_actual_minimum_preserves_scores_and_total(cap, method):
    request, initial, evaluate, calls = problem(total_cap=cap)
    original = deepcopy(initial)
    archived = []
    best, detail = minimax.refine_hr_minimax(request, initial, evaluate=evaluate,
        archive=lambda value, *, audit_eligible: archived.append((value, audit_eligible)),
        max_iterations=40, soft_method=method)
    assert initial == original
    assert best["score"]["marker"] == "original"
    assert detail["sampled_min_reflectance"] > min(initial["responses"][0]["actual_values"]) + .015
    assert detail["sampled_min_reflectance"] == min(best["responses"][0]["actual_values"])
    assert detail["betas"] == [100., 400., 1500.]
    assert detail["training_conditions"] == 8
    assert detail["physical_candidates"] == len(calls) == len(archived)
    assert all(flag is False for _, flag in archived)
    assert detail["cache_hits"] > 0
    assert detail["stages"][-1]["optimizer_t"] == pytest.approx(detail["sampled_min_reflectance"], abs=1e-6)
    if cap is not None:
        assert all(sum(d) <= cap for d in calls)


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "angle", "wave", "jac_missing", "jac_shape", "jac_nan", "actual_inf"])
def test_invalid_complete_responses_are_rejected(corruption):
    request, initial, evaluate, _ = problem()
    bad = deepcopy(initial)
    row = bad["responses"][0]
    if corruption == "missing":
        bad["responses"].pop()
    elif corruption == "duplicate":
        bad["responses"].append(deepcopy(row))
    elif corruption == "angle":
        row["angle_deg"] = 15.
    elif corruption == "wave":
        row["wavelength_nm"][0] += 1.
    elif corruption == "jac_missing":
        row["jacobian_values_by_layer_id"].pop("layer-0")
    elif corruption == "jac_shape":
        row["jacobian_values_by_layer_id"]["layer-0"] = [0.]
    elif corruption == "jac_nan":
        row["jacobian_values_by_layer_id"]["layer-0"][0] = float("nan")
    else:
        row["actual_values"][0] = float("inf")
    with pytest.raises(ValueError, match="HR response"):
        minimax.refine_hr_minimax(request, bad, evaluate=evaluate, archive=lambda *a, **k: None)


def test_missing_initial_responses_refresh_through_parent_and_stop_propagates():
    request, initial, evaluate, calls = problem()
    initial.pop("responses")
    class Stop(Exception):
        pass
    def archive(value, **kwargs):
        raise Stop("parent budget")
    with pytest.raises(Stop, match="parent budget"):
        minimax.refine_hr_minimax(request, initial, evaluate=evaluate, archive=archive)
    assert len(calls) == 1


def test_cache_shared_across_fun_jac_t_and_beta_and_rejects_infeasible(monkeypatch):
    request, initial, evaluate, calls = problem(total_cap=4.)
    seen_losses = []
    def minimize(fun, x0, *, jac, constraints, **kwargs):
        x = x0.copy()
        seen_losses.append(fun(x))
        jac(x)
        if len(x) == 2:
            illegal = x.copy() + 10.
            fun(illegal)
            jac(illegal)
        else:
            for constraint in constraints:
                constraint["fun"](x)
                constraint["jac"](x)
            x[-1] -= .1
            constraints[-1]["fun"](x)
            constraints[-1]["jac"](x)
        return SimpleNamespace(x=x0, success=True, message="probe", nit=1)
    monkeypatch.setattr(minimax.import_module("scipy.optimize"), "minimize", minimize)
    _, detail = minimax.refine_hr_minimax(request, initial, evaluate=evaluate,
        archive=lambda *a, **k: None)
    assert calls == []  # All optimizer requests use the original completed point.
    assert len(set(seen_losses[:3])) == 3
    assert detail["rejected_infeasible_trials"] == 6
    assert detail["cache_hits"] >= 10


def test_each_cache_access_checks_cancellation(monkeypatch):
    request, initial, evaluate, calls = problem()
    stopped = False
    class Stop(Exception):
        pass
    def check():
        if stopped:
            raise Stop
    def minimize(fun, x0, *, jac, **kwargs):
        nonlocal stopped
        fun(x0)
        stopped = True
        jac(x0)
        raise AssertionError("cancel must escape cached Jacobian")
    monkeypatch.setattr(minimax.import_module("scipy.optimize"), "minimize", minimize)
    with pytest.raises(Stop):
        minimax.refine_hr_minimax(request, initial, evaluate=evaluate,
            archive=lambda *a, **k: None, cancel_check=check)
    assert calls == []


def test_mixed_objectives_do_not_enter_hr_minimax():
    request, initial, evaluate, _ = problem()
    request = replace(request, objectives=(*request.objectives, replace(request.objectives[0], metric="T")))
    with pytest.raises(ValueError, match="exclusively"):
        minimax.refine_hr_minimax(request, initial, evaluate=evaluate, archive=lambda *a, **k: None)


def test_small_production_analytic_jacobian_improves_without_extra_solver():
    from tests.test_film_synthesis_refinement import _physical_refinement_fixture
    request, _, _, _, evaluate, initial = _physical_refinement_fixture()
    best, detail = minimax.refine_hr_minimax(request, initial, evaluate=evaluate,
        archive=lambda *a, **k: None, max_iterations=8)
    assert detail["sampled_min_reflectance"] >= min(initial["responses"][0]["actual_values"])
    assert best["design"]["front_layers"]
    assert detail["physical_candidates"] > 0


def test_default_recipe_full_bounds_and_missing_upper_bound(monkeypatch):
    request, initial, evaluate, _ = problem()
    request = replace(request, max_layer_thickness_nm=None)
    seen = []
    def minimize(fun, x0, *, method, bounds, jac, options, **kwargs):
        seen.append((method, bounds, dict(options)))
        assert all(bound[1] is None for bound in bounds[:2])
        if len(x0) == 2:
            np.testing.assert_array_equal(x0, [.01, .01])
        fun(x0)
        jac(x0)
        return SimpleNamespace(x=x0, message="probe", nit=0, success=True)
    monkeypatch.setattr(minimax.import_module("scipy.optimize"), "minimize", minimize)
    best, detail = minimax.refine_hr_minimax(request, initial, evaluate=evaluate,
        archive=lambda *a, **k: None)
    assert best is initial
    assert [item[0] for item in seen] == ["L-BFGS-B"]*3 + ["SLSQP"]
    assert seen[0][2] == dict(maxiter=180, ftol=2e-13, gtol=1e-9, maxls=25)
    assert seen[-1][2] == dict(maxiter=180, ftol=1e-11)
    assert detail["scale_nm"] == [100., 100.]


def test_evaluator_must_return_same_physical_candidate():
    request, initial, evaluate, _ = problem()
    initial.pop("responses")
    def foreign(design, **kwargs):
        value = evaluate(design, **kwargs)
        value["design"]["front_layers"][0]["thickness"]["value"] += .1
        return value
    with pytest.raises(ValueError, match="another design"):
        minimax.refine_hr_minimax(request, initial, evaluate=foreign,
            archive=lambda *a, **k: None)
