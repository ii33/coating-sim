"""Analytic mixed R/T fixtures: original scores, guide mass and parent boundaries."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import numpy as np
import pytest

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core import film_synthesis_multispectral_minimax as minimax

pytestmark = pytest.mark.algorithm


def problem(*, total_cap=None, objectives=None):
    if objectives is None:
        objectives = (
            FilmOptimizationObjective(500., 600., 2, angles_deg=(0., 30.), metric="R",
                mode="range", lower_bound=.7, weight=1., normalization_scale=2.),
            FilmOptimizationObjective(700., 900., 5, angles_deg=(0.,), metric="T",
                mode="range", upper_bound=.2, weight=7., normalization_scale=.5),
        )
    request = FilmSynthesisRequest({}, tuple(objectives), (), minimum_nonzero_thickness_nm=.5,
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
        responses, errors, weights = [], [], []
        for index, objective in enumerate(objectives):
            offset = .0025 * (d[1]-2.)**2
            y = (.2 + .06*d[0] - offset if objective.metric == "R"
                 else .05 + .04*d[0] + offset)
            derivative = ([.06, -.005*(d[1]-2.)] if objective.metric == "R"
                          else [.04, .005*(d[1]-2.)])
            for angle in objective.angles_deg:
                n = objective.wavelength_samples
                responses.append(dict(objective_index=index, angle_deg=angle,
                    wavelength_nm=np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm, n).tolist(),
                    actual_values=[y]*n,
                    jacobian_values_by_layer_id={f"layer-{j}": [derivative[j]]*n for j in range(2)}))
                error = (abs(y-objective.target_value) if objective.mode == "value" else
                         max(0., 0. if objective.lower_bound is None else objective.lower_bound-y,
                             0. if objective.upper_bound is None else y-objective.upper_bound))
                errors.extend([error/objective.normalization_scale]*n)
                weights.extend([objective.weight]*n)
        rms = float(np.sqrt(np.average(np.square(errors), weights=weights)))
        return dict(design=deepcopy(candidate), responses=responses,
                    score=dict(max_error=max(errors), weighted_rms_error=rms, marker="parent-original"))

    initial = evaluate(design)
    calls.clear()
    return request, initial, evaluate, calls


def test_signed_rt_value_bounds_scales_and_soft_analytic_derivative():
    objectives = (
        FilmOptimizationObjective(500., 600., 2, metric="R", mode="range",
            lower_bound=.3, upper_bound=.5, normalization_scale=2., weight=3.),
        FilmOptimizationObjective(700., 800., 3, metric="T", mode="value",
            target_value=.2, normalization_scale=.5, weight=8.),
    )
    request, initial, evaluate, _ = problem(objectives=objectives)
    ids = ("layer-0", "layer-1")
    q, jac, masses = minimax._signed_bounds(request, initial, ids)
    assert len(q) == 10
    np.testing.assert_allclose(jac[:2, 0], -.06/2.)
    np.testing.assert_allclose(jac[2:4, 0], .06/2.)
    np.testing.assert_allclose(jac[4:7, 0], -.04/.5)
    np.testing.assert_allclose(jac[7:, 0], .04/.5)
    np.testing.assert_allclose(np.exp(masses[:4]).sum(), 3./11.)
    np.testing.assert_allclose(np.exp(masses[4:]).sum(), 8./11.)
    assert max(0., float(max(q))) == pytest.approx(initial["score"]["max_error"])
    loss, gradient = minimax._soft_max_loss(q, jac, masses, 100.)
    assert loss != minimax._soft_max_loss(q, jac, masses, 1500.)[0]
    step = 1e-5
    for axis in range(2):
        samples, signed = [], []
        for sign in (-1, 1):
            design = deepcopy(initial["design"])
            design["front_layers"][axis]["thickness"]["value"] += sign*step
            data = minimax._signed_bounds(request, evaluate(design), ids)
            signed.append(data[0])
            samples.append(minimax._soft_max_loss(*data, 100.)[0])
        np.testing.assert_allclose(jac[:, axis], (signed[1]-signed[0])/(2*step), rtol=1e-7)
        assert gradient[axis] == pytest.approx((samples[1]-samples[0])/(2*step), rel=1e-7, abs=1e-10)


def test_window_mass_unchanged_by_unequal_sampling_and_original_rms_keeps_point_weights():
    request, initial, _, _ = problem()
    ids = ("layer-0", "layer-1")
    guide = minimax._signed_bounds(request, initial, ids)
    dense_objectives = (replace(request.objectives[0], wavelength_samples=23, angles_deg=(0., 15., 30.)),
                        replace(request.objectives[1], wavelength_samples=2))
    dense_request, dense, _, _ = problem(objectives=dense_objectives)
    dense_guide = minimax._signed_bounds(dense_request, dense, ids)
    loss, derivative = minimax._soft_max_loss(*guide, 100.)
    dense_loss, dense_derivative = minimax._soft_max_loss(*dense_guide, 100.)
    assert loss == pytest.approx(dense_loss, abs=1e-14)
    np.testing.assert_allclose(derivative, dense_derivative, atol=1e-14)
    assert dense["score"]["weighted_rms_error"] != pytest.approx(initial["score"]["weighted_rms_error"])
    assert dense["score"]["max_error"] == initial["score"]["max_error"]


@pytest.mark.parametrize("method", ["L-BFGS-B", "SLSQP"])
@pytest.mark.parametrize("cap", [None, 7.])
def test_real_solver_minimizes_original_maximum_and_preserves_complete_parent_scores(method, cap):
    request, initial, evaluate, calls = problem(total_cap=cap)
    original = deepcopy(initial)
    archived = []
    best, details = minimax.refine_multispectral_minimax(request, initial, evaluate=evaluate,
        archive=lambda value, *, audit_eligible: archived.append((value, audit_eligible)),
        max_iterations=35, soft_method=method)
    assert initial == original
    assert best["score"]["max_error"] < initial["score"]["max_error"] - .10
    assert best["score"]["max_error"] == pytest.approx(.1, abs=2e-6)
    assert best["score"]["marker"] == "parent-original"
    assert details["training_conditions"] == 9
    assert details["signed_bound_conditions"] == 9
    assert details["max_error"] == best["score"]["max_error"]
    assert details["physical_candidates"] == len(calls) == len(archived)
    assert all(flag is False and len(value["responses"]) == 3 for value, flag in archived)
    assert details["stages"][-1]["optimizer_t"] == pytest.approx(.1, abs=2e-6)


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "index", "angle", "wave", "jac_missing", "jac_shape", "jac_nan", "actual_inf"])
def test_full_original_response_contract_rejects_corruptions(corruption):
    request, initial, evaluate, _ = problem()
    row = initial["responses"][0]
    if corruption == "missing":
        initial["responses"].pop()
    elif corruption == "duplicate":
        initial["responses"].append(deepcopy(row))
    elif corruption == "index":
        row["objective_index"] = True
    elif corruption == "angle":
        row["angle_deg"] = 45.
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
    with pytest.raises(ValueError, match="multispectral response"):
        minimax.refine_multispectral_minimax(request, initial, evaluate=evaluate, archive=lambda *a, **k: None)


def test_cache_shared_between_beta_and_epigraph_and_original_constraint_jacobian(monkeypatch):
    request, initial, evaluate, calls = problem(total_cap=7.)
    seen = []
    def minimize(fun, x0, *, jac, constraints, bounds, method, options):
        seen.append((method, bounds, options, fun(x0)))
        jac(x0)
        if len(x0) == 2:
            illegal = x0.copy() + 10.
            assert fun(illegal) > 1.
            jac(illegal)
        else:
            assert bounds[-1] == (0., None)
            assert jac(x0)[-1] == 1.
            constraint = constraints[-1]
            residual = constraint["fun"](x0)
            analytic = constraint["jac"](x0)
            assert np.min(residual) == pytest.approx(0.)
            for axis in range(3):
                delta = np.zeros(3)
                delta[axis] = 1e-7
                finite = (constraint["fun"](x0+delta)-constraint["fun"](x0-delta))/(2e-7)
                np.testing.assert_allclose(analytic[:, axis], finite, rtol=1e-7, atol=1e-8)
        return SimpleNamespace(x=x0, success=True, message="probe", nit=1)
    monkeypatch.setattr(minimax.import_module("scipy.optimize"), "minimize", minimize)
    best, detail = minimax.refine_multispectral_minimax(request, initial, evaluate=evaluate,
        archive=lambda *a, **k: None)
    assert best["score"]["max_error"] <= initial["score"]["max_error"]
    assert len(set(item[-1] for item in seen[:3])) == 3
    assert [item[0] for item in seen] == ["L-BFGS-B"]*3 + ["SLSQP"]
    assert seen[0][2] == dict(maxiter=180, ftol=2e-13, gtol=1e-9, maxls=25)
    assert seen[-1][2] == dict(maxiter=180, ftol=1e-11)
    assert detail["rejected_infeasible_trials"] == 6
    assert detail["cache_hits"] >= 8
    assert detail["scale_nm"] == [100., 100.]
    assert all(np.all(d >= .5) and sum(d) <= 7. for d in calls)


@pytest.mark.parametrize("reason", ["cancel", "budget"])
def test_cached_fun_jac_checks_parent_cancellation_and_budget(monkeypatch, reason):
    request, initial, evaluate, calls = problem()
    stopped = False
    class ParentBoundary(Exception):
        pass
    def check():
        if stopped:
            raise ParentBoundary(reason)
    def minimize(fun, x0, *, jac, **kwargs):
        nonlocal stopped
        fun(x0)
        stopped = True
        jac(x0)
        raise AssertionError("cached derivative must stop")
    monkeypatch.setattr(minimax.import_module("scipy.optimize"), "minimize", minimize)
    with pytest.raises(ParentBoundary, match=reason):
        minimax.refine_multispectral_minimax(request, initial, evaluate=evaluate,
            archive=lambda *a, **k: None, cancel_check=check)
    assert calls == []


def test_parent_evaluation_failure_is_not_swallowed_or_archived():
    request, initial, _, _ = problem()
    initial.pop("responses")
    archived = []
    class ParentBoundary(Exception):
        pass
    def evaluate(*args, **kwargs):
        raise ParentBoundary("budget exhausted")
    with pytest.raises(ParentBoundary, match="budget exhausted"):
        minimax.refine_multispectral_minimax(request, initial, evaluate=evaluate,
            archive=lambda *a, **k: archived.append(a))
    assert archived == []


def test_completed_candidate_archived_before_admission_budget_stop(monkeypatch):
    request, initial, evaluate, _ = problem()
    stopped = False
    archived = []
    class ParentBoundary(Exception):
        pass
    def check():
        if stopped:
            raise ParentBoundary("budget")
    def parent_evaluate(*args, **kwargs):
        nonlocal stopped
        value = evaluate(*args, **kwargs)
        stopped = True
        return value
    def minimize(fun, x0, **kwargs):
        point = x0.copy()
        point[0] += .01
        fun(point)
        raise AssertionError("completed trial must stop after archival")
    monkeypatch.setattr(minimax.import_module("scipy.optimize"), "minimize", minimize)
    with pytest.raises(ParentBoundary):
        minimax.refine_multispectral_minimax(request, initial, evaluate=parent_evaluate,
            archive=lambda value, **kwargs: archived.append(value), cancel_check=check)
    assert len(archived) == 1
    assert archived[0]["score"]["max_error"] < initial["score"]["max_error"]


def test_best_candidate_retained_when_endpoints_worsen_and_score_tie_uses_parent_rms(monkeypatch):
    request, initial, evaluate, _ = problem()
    def parent_evaluate(*args, **kwargs):
        value = evaluate(*args, **kwargs)
        # Equal primary error makes the parent's original RMS the sole arbiter.
        value["score"]["max_error"] = initial["score"]["max_error"]
        return value
    def minimize(fun, x0, **kwargs):
        improved = x0.copy()
        improved[0] = .02
        fun(improved)
        # This completed endpoint is deliberately worse in parent RMS.
        endpoint = x0.copy()
        endpoint[0] = .09
        return SimpleNamespace(x=endpoint, success=False, message="worse endpoint", nit=1)
    monkeypatch.setattr(minimax.import_module("scipy.optimize"), "minimize", minimize)
    best, _ = minimax.refine_multispectral_minimax(request, initial, evaluate=parent_evaluate,
        archive=lambda *a, **k: None)
    assert best["design"]["front_layers"][0]["thickness"]["value"] == 2.
    assert best["score"]["max_error"] == initial["score"]["max_error"]
    assert best["score"]["weighted_rms_error"] < initial["score"]["weighted_rms_error"]


@pytest.mark.parametrize("corruption", ["unit", "zero", "upper", "total", "duplicate_id"])
def test_invalid_initial_physical_stack_rejected(corruption):
    request, initial, evaluate, _ = problem(total_cap=7.)
    layer = initial["design"]["front_layers"][0]
    if corruption == "unit":
        layer["thickness"]["unit"] = "um"
    elif corruption == "zero":
        layer["thickness"]["value"] = 0.
    elif corruption == "upper":
        layer["thickness"]["value"] = 11.
    elif corruption == "total":
        layer["thickness"]["value"] = 7.
    else:
        layer["id"] = "layer-1"
    with pytest.raises(ValueError):
        minimax.refine_multispectral_minimax(request, initial, evaluate=evaluate, archive=lambda *a, **k: None)


def test_no_implicit_upper_and_relative_nm_scales(monkeypatch):
    request, initial, evaluate, _ = problem()
    request = replace(request, max_layer_thickness_nm=None)
    def minimize(fun, x0, *, bounds, **kwargs):
        assert all(bound[1] is None for bound in bounds)
        fun(x0)
        return SimpleNamespace(x=x0, success=True, message="probe", nit=0)
    monkeypatch.setattr(minimax.import_module("scipy.optimize"), "minimize", minimize)
    _, details = minimax.refine_multispectral_minimax(request, initial, evaluate=evaluate,
        archive=lambda *a, **k: None, coordinate_scale_nm=None)
    assert details["scale_nm"] == [1., 1.]


def test_evaluator_cannot_substitute_design():
    request, initial, evaluate, _ = problem()
    initial.pop("responses")
    def foreign(*args, **kwargs):
        value = evaluate(*args, **kwargs)
        value["design"]["front_layers"][0]["thickness"]["value"] += .1
        return value
    with pytest.raises(ValueError, match="another design"):
        minimax.refine_multispectral_minimax(request, initial, evaluate=foreign, archive=lambda *a, **k: None)


def test_small_production_analytic_jacobian_smoke():
    from tests.test_film_synthesis_refinement import _physical_refinement_fixture
    request, _, _, _, evaluate, initial = _physical_refinement_fixture()
    best, details = minimax.refine_multispectral_minimax(request, initial, evaluate=evaluate,
        archive=lambda *a, **k: None, max_iterations=4)
    assert (best["score"]["max_error"], best["score"]["weighted_rms_error"]) <= (
        initial["score"]["max_error"], initial["score"]["weighted_rms_error"])
    assert details["physical_candidates"] > 0



def test_cache_retains_only_the_latest_physical_point(monkeypatch):
    request, initial, evaluate, calls = problem()
    first_stage = True
    def minimize(fun, x0, *, jac, **kwargs):
        nonlocal first_stage
        if first_stage:
            first_stage = False
            fun(x0)  # Initial complete point is cached.
            assert len(calls) == 0
            point = x0.copy()
            point[1] -= .001
            fun(point)
            jac(point)
            assert len(calls) == 1
            fun(x0)  # Revisit must evaluate because only the latest point survives.
            jac(x0)
            assert len(calls) == 2
        return SimpleNamespace(x=x0, success=True, message="probe", nit=0)
    monkeypatch.setattr(minimax.import_module("scipy.optimize"), "minimize", minimize)
    _, details = minimax.refine_multispectral_minimax(request, initial, evaluate=evaluate,
        archive=lambda *a, **k: None)
    assert len(calls) == details["physical_candidates"] == 2


@pytest.mark.parametrize("change", [dict(metric="A"), dict(mode="unknown"), dict(weight=0.),
    dict(normalization_scale=0.), dict(angles_deg=(0., 0.)), dict(wavelength_samples=0),
    dict(lower_bound=None), dict(lower_bound=.8, upper_bound=.7)])
def test_original_objective_contract_rejects_invalid_targets(change):
    request, initial, evaluate, calls = problem()
    request = replace(request, objectives=(replace(request.objectives[0], **change), request.objectives[1]))
    with pytest.raises(ValueError, match="multispectral"):
        minimax.refine_multispectral_minimax(request, initial, evaluate=evaluate,
            archive=lambda *a, **k: None)
    assert calls == []



@pytest.mark.parametrize("q", [[1e307, 1e307, 1e307], [-1e307, -1e307, -1e307],
                                [1e308, -1e308, -1e308]])
def test_soft_max_extreme_finite_q_and_convex_gradient_do_not_overflow(q):
    values = np.asarray(q)
    jacobian = np.full((3, 2), 1e308)
    jacobian[:, 1] *= -1.
    with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
        loss, gradient = minimax._soft_max_loss(values, jacobian, np.full(3, -np.log(3.)), 100.)
    assert np.isfinite(loss) and np.all(np.isfinite(gradient))
    assert loss == pytest.approx(max(q), rel=1e-15)
    np.testing.assert_allclose(gradient, [1e308, -1e308], rtol=1e-15)


def test_extreme_positive_window_weights_remain_in_log_mass_and_match_decimal():
    from decimal import Decimal, localcontext
    objectives = (
        FilmOptimizationObjective(500., 500., 1, metric="R", mode="range",
            lower_bound=.7, weight=1e300),
        FilmOptimizationObjective(700., 700., 1, metric="T", mode="range",
            upper_bound=.2, normalization_scale=.01, weight=1e-300),
    )
    request, initial, _, _ = problem(objectives=objectives)
    initial["responses"][0]["actual_values"] = [.7]
    initial["responses"][1]["actual_values"] = [.4]
    q, jacobian, log_mass = minimax._signed_bounds(request, initial, ("layer-0", "layer-1"))
    assert np.all(np.isfinite(log_mass))
    assert log_mass[1] < -1000.  # Its linear mass underflows, but its contribution survives.
    loss, gradient = minimax._soft_max_loss(q, jacobian, log_mass, 100.)
    with localcontext() as context:
        context.prec = 100
        large, small, beta = Decimal("1e300"), Decimal("1e-300"), Decimal(100)
        q0, q1 = map(Decimal.from_float, q)
        numerator = large*(beta*q0).exp() + small*(beta*q1).exp()
        expected = float((numerator/(large+small)).ln()/beta)
        alpha = small*(beta*q1).exp()/numerator
        expected_gradient = [float((1-alpha)*Decimal.from_float(jacobian[0, j])
                                   + alpha*Decimal.from_float(jacobian[1, j])) for j in range(2)]
    assert loss == pytest.approx(expected, abs=2e-14)
    np.testing.assert_allclose(gradient, expected_gradient, rtol=1e-14, atol=1e-14)
    assert loss > 6.  # Dropping the tiny-weight window would incorrectly return zero.


@pytest.mark.parametrize("worse_score", [None, (1e-8, 0.), (0., 1e-8)])
def test_original_score_ties_prefer_margin_but_never_a_worse_original_score(monkeypatch, worse_score):
    objectives = (FilmOptimizationObjective(500., 500., 1, metric="R", mode="range", lower_bound=.2),)
    request, initial, evaluate, _ = problem(objectives=objectives)
    assert initial["score"]["max_error"] == initial["score"]["weighted_rms_error"] == 0.
    def parent_evaluate(*args, **kwargs):
        value = evaluate(*args, **kwargs)
        if worse_score is not None:
            value["score"]["max_error"], value["score"]["weighted_rms_error"] = worse_score
        return value
    def minimize(fun, x0, **kwargs):
        point = x0.copy()
        point[0] = .04
        fun(point)
        return SimpleNamespace(x=x0, success=True, message="retained endpoint", nit=1)
    monkeypatch.setattr(minimax.import_module("scipy.optimize"), "minimize", minimize)
    best, detail = minimax.refine_multispectral_minimax(request, initial, evaluate=parent_evaluate,
        archive=lambda *a, **k: None)
    if worse_score is None:
        assert best["design"]["front_layers"][0]["thickness"]["value"] == 4.
        assert best["score"]["max_error"] == best["score"]["weighted_rms_error"] == 0.
        assert detail["max_signed_violation"] < -.2
    else:
        assert best is initial


@pytest.mark.parametrize("cached_responses", [False, True])
def test_retained_initial_roundtrip_has_finite_or_explicitly_unavailable_statistic(monkeypatch, cached_responses):
    request, initial, evaluate, calls = problem()
    # A suboptimal original score can still beat every coordinate roundtrip by
    # one ULP. Its physical identity and original ordering must be preserved.
    original_key = dict(initial["score"])
    expected_q = float(max(minimax._signed_bounds(request, initial, ())[0]))
    for row in initial["responses"]:
        row.pop("jacobian_values_by_layer_id")
    if not cached_responses:
        initial.pop("responses")
    def roundtrip(design, **kwargs):
        value = evaluate(design, **kwargs)
        for key in ("max_error", "weighted_rms_error"):
            value["score"][key] = float(np.nextafter(original_key[key], np.inf))
        return value
    def minimize(fun, x0, *, jac, constraints=(), **kwargs):
        fun(x0)
        jac(x0)
        for constraint in constraints:
            constraint["fun"](x0)
            constraint["jac"](x0)
        return SimpleNamespace(x=x0.copy(), message="cached roundtrip", nit=0, success=True)
    monkeypatch.setattr(minimax.import_module("scipy.optimize"), "minimize", minimize)
    best, details = minimax.refine_multispectral_minimax(request, initial, evaluate=roundtrip,
        archive=lambda *args, **kwargs: None)
    assert best is initial and best["score"] == original_key
    assert len(calls) == details["physical_candidates"] == 1
    if cached_responses:
        assert details["max_signed_violation"] == expected_q
        assert details["max_signed_violation_source"] == "retained_initial_cached_responses"
    else:
        assert details["max_signed_violation"] is None
        assert "unavailable_without_additional_physical_evaluation" in details["max_signed_violation_source"]
    json.dumps({"audit": {"synthesis_performance": {"search": {"multispectral_start": {
        "runs": [{"optimizer": details}]}}}}}, allow_nan=False)


@pytest.mark.parametrize("corruption", ["actual_inf", "duplicate", "missing", "wave"])
def test_response_only_statistic_keeps_original_condition_validation(corruption):
    request, initial, _, _ = problem()
    if corruption == "actual_inf":
        initial["responses"][0]["actual_values"][0] = float("inf")
    elif corruption == "duplicate":
        initial["responses"].append(deepcopy(initial["responses"][0]))
    elif corruption == "missing":
        initial["responses"].pop()
    else:
        initial["responses"][0]["wavelength_nm"][0] += .1
    with pytest.raises(ValueError):
        minimax._signed_bounds(request, initial, ())
