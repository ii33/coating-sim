"""Original-condition R/T minimax continuation using parent optical evaluation.

Signed bounds and normalized window masses guide the smooth search only. Candidate
selection and the epigraph use the parent's original unweighted maximum violation;
parent ownership includes budgets, cancellation, archival and independent audit.
"""
from __future__ import annotations

from copy import deepcopy
from importlib import import_module
import math
from typing import TYPE_CHECKING, Any, Callable, Literal, cast

import numpy as np

from .film_synthesis_hr_minimax import Array, Value, BETAS, _Constraint, _Minimize
from .film_objective_criteria import reduce_objective_responses, validate_objective_criteria

if TYPE_CHECKING:
    from .film_auto_synthesis import FilmSynthesisRequest


def _validate_objectives(request: FilmSynthesisRequest) -> None:
    if not request.objectives:
        raise ValueError("multispectral minimax requires original objectives")
    for objective in request.objectives:
        validate_objective_criteria(objective)
        if (objective.metric not in ("R", "T")
                or type(objective.wavelength_samples) is not int or objective.wavelength_samples < 1
                or not math.isfinite(objective.wavelength_min_nm) or objective.wavelength_min_nm <= 0
                or not math.isfinite(objective.wavelength_max_nm)
                or objective.wavelength_max_nm < objective.wavelength_min_nm
                or not objective.angles_deg or len(set(objective.angles_deg)) != len(objective.angles_deg)
                or any(isinstance(angle, bool) or not isinstance(angle, (int, float))
                       or not math.isfinite(angle) for angle in objective.angles_deg)):
            raise ValueError("multispectral minimax original objective is invalid")


def _original_reduction(request: FilmSynthesisRequest, value: Value, ids: tuple[str, ...]):
    """Return signed normalized violations, nm Jacobians and log window masses.

    Each original objective/angle row must occur once; wavelength vectors and
    layer IDs must exactly match. Each window's total guide mass is its weight,
    regardless of the number of wavelengths, angles or bounds in that window.
    Empty ids requests response-only statistics, with an (N, 0) derivative;
    no unavailable layer derivative is invented or used by the optimizer.
    """
    expected = {(i, float(angle)) for i, objective in enumerate(request.objectives)
                for angle in objective.angles_deg}
    rows: dict[tuple[int, float], tuple[Array, Array]] = {}
    for row in value.get("responses", ()):
        index = row["objective_index"]
        if type(index) is not int or not 0 <= index < len(request.objectives):
            raise ValueError("multispectral response objective index is invalid")
        angle = row["angle_deg"]
        if isinstance(angle, bool) or not isinstance(angle, (int, float)) or not math.isfinite(angle):
            raise ValueError("multispectral response angle is invalid")
        key = index, float(angle)
        if key not in expected or key in rows:
            raise ValueError("multispectral response repeats or changes an original condition")
        objective = request.objectives[index]
        waves = np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm,
                            objective.wavelength_samples)
        actual = np.asarray(row["actual_values"], dtype=float)
        if (not np.array_equal(np.asarray(row["wavelength_nm"], dtype=float), waves)
                or actual.shape != waves.shape or not np.all(np.isfinite(actual))):
            raise ValueError("multispectral response grid/values differ from original conditions")
        if ids:
            derivatives = row.get("jacobian_values_by_layer_id", {})
            if set(derivatives) != set(ids):
                raise ValueError("multispectral response requires exactly the optimized layer Jacobians")
            columns = [np.asarray(derivatives[identifier], dtype=float) for identifier in ids]
            if any(column.shape != waves.shape or not np.all(np.isfinite(column)) for column in columns):
                raise ValueError("multispectral response Jacobian shape/values are invalid")
            jacobian = np.column_stack(columns)
        else:
            jacobian = np.empty((len(waves), 0))
        rows[key] = actual, jacobian
    if set(rows) != expected:
        raise ValueError("multispectral response is missing original conditions")
    return reduce_objective_responses(request.objectives, value['responses'], layer_ids=ids,
                                      tolerance=request.acceptable_normalized_error)


def _signed_bounds(request: FilmSynthesisRequest, value: Value,
                   ids: tuple[str, ...]) -> tuple[Array, Array, Array]:
    """Canonical unweighted bounds/J and shared normalized log guide masses."""
    reduced = _original_reduction(request, value, ids)
    return reduced.signed_bound_values, reduced.signed_bound_jacobian, reduced.signed_bound_log_masses


def _soft_max_loss(q: Array, jacobian: Array, log_masses: Array, beta: float) -> tuple[float, Array]:
    """Stable log-mass logsumexp(beta*q)/beta; a search guide only.

    Shift q before multiplying by beta. Finite opposite extremes may produce
    -inf differences, whose exponential is safely zero. Normalize soft weights
    before their Jacobian product to keep finite convex-combination gradients.
    """
    qmax = float(np.max(q))
    with np.errstate(over="ignore"):
        logits = beta * (q - qmax) + log_masses
    shift = float(np.max(logits))
    weights = np.exp(logits - shift)
    denominator = float(np.sum(weights))
    weights /= denominator
    return qmax + (shift + math.log(denominator)) / beta, weights @ jacobian


def _score_key(value: Value) -> tuple[int, float, float]:
    score = value["score"]
    key = (int(not score.get("nominal_pass", score["max_error"] == 0.)),
           float(score["max_error"]), float(score["weighted_rms_error"]))
    if any(not math.isfinite(number) or number < 0 for number in key):
        raise ValueError("multispectral parent score must be finite and nonnegative")
    return key


def refine_multispectral_minimax(request: FilmSynthesisRequest, initial: Value, *,
                                 evaluate: Callable[..., Value], archive: Callable[..., None],
                                 cancel_check: Callable[[], None] = lambda: None,
                                 progress: Callable[..., None] = lambda *args: None,
                                 max_iterations: int = 180,
                                 soft_method: Literal["SLSQP", "L-BFGS-B"] = "L-BFGS-B",
                                 coordinate_scale_nm: float | None = 100.) -> tuple[Value, dict[str, Any]]:
    """Refine full original R/T conditions without modifying original scores.

    Three smooth continuation stages precede exact sampled min(t), t>=0 and
    t-q>=0. This is local nonlinear optimization, not a global guarantee. Bounds
    are physical nm bounds without a trust box; max_iterations is per stage.
    Pass the parent's budget admission callback as cancel_check: it runs on
    cached requests too. Parent callback exceptions propagate without wrapping.
    """
    cancel_check()
    _validate_objectives(request)
    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("multispectral minimax max_iterations must be a positive integer")
    if soft_method not in ("SLSQP", "L-BFGS-B"):
        raise ValueError("multispectral minimax soft_method is unsupported")
    if coordinate_scale_nm is not None and (isinstance(coordinate_scale_nm, bool)
            or not math.isfinite(coordinate_scale_nm) or coordinate_scale_nm <= 0):
        raise ValueError("multispectral minimax coordinate scale must be positive finite nm")
    design = initial["design"]
    layers = design["front_layers"]
    if not layers:
        return initial, dict(method="none", reason="empty_topology", physical_candidates=0)
    ids = tuple(row["id"] for row in layers)
    if len(set(ids)) != len(ids) or any(not isinstance(identifier, str) or not identifier for identifier in ids):
        raise ValueError("multispectral minimax requires unique nonempty layer ids")
    if request.max_layers is not None and len(ids) > request.max_layers:
        raise ValueError("multispectral minimax initial topology exceeds max_layers")
    if any(row["thickness"]["unit"] != "nm" or isinstance(row["thickness"]["value"], bool) for row in layers):
        raise ValueError("multispectral minimax requires normalized nm thicknesses")
    ds = np.asarray([row["thickness"]["value"] for row in layers], dtype=float)
    scales = (np.maximum(ds, 1.) if coordinate_scale_nm is None
              else np.full(ds.shape, coordinate_scale_nm, dtype=float))
    low = request.minimum_nonzero_thickness_nm or np.finfo(float).tiny
    upper, total_cap = request.max_layer_thickness_nm, request.max_total_thickness_nm

    def feasible(d: Array) -> bool:
        return bool(np.all(np.isfinite(d)) and np.all(d >= low)
                    and (upper is None or np.all(d <= upper))
                    and (total_cap is None or float(sum(d)) <= total_cap))

    if not feasible(ds):
        raise ValueError("multispectral minimax requires a physically feasible initial candidate")
    bounds: list[tuple[float | None, float | None]] = [
        (low / float(scale), None if upper is None else upper / float(scale)) for scale in scales]
    best, best_key = initial, _score_key(initial)
    best_worst_signed = math.inf
    cache_x: Array | None = None
    cache_data: tuple[Array, Array, Array] | None = None
    calls = cache_hits = rejected = 0
    stages: list[dict[str, Any]] = []

    def commit(value: Value, point: Array, *, physical_call: bool) -> tuple[Array, Array, Array]:
        nonlocal cache_x, cache_data, best, best_key, best_worst_signed, calls
        q, jacobian, log_masses = _signed_bounds(request, value, ids)
        key, worst_signed = _score_key(value), float(np.max(q))
        # Only an exact original-score tie may use guide margin. Parent scoring
        # and strict acceptance remain authoritative, including zero-error ties.
        if key < best_key or (key == best_key and worst_signed < best_worst_signed):
            best, best_key, best_worst_signed = value, key, worst_signed
        cache_x, cache_data = point.copy(), (q, jacobian * scales, log_masses)
        if physical_call:
            calls += 1
            archive(value, audit_eligible=False)
            progress("multispectral_minimax_trial", value,
                     dict(physical_candidates=calls, max_error=key[1], weighted_rms_error=key[2]))
        cancel_check()
        return cache_data

    def data(point: Array) -> tuple[Array, Array, Array] | None:
        nonlocal cache_hits, rejected
        cancel_check()
        point = np.asarray(point, dtype=float)
        if point.shape != ds.shape or not np.all(np.isfinite(point)):
            raise ValueError("multispectral minimax optimizer returned invalid coordinates")
        d = point * scales
        if not feasible(d):
            rejected += 1
            return None
        if cache_x is not None and np.array_equal(point, cache_x):
            cache_hits += 1
            assert cache_data is not None
            return cache_data
        trial = deepcopy(design)
        for row, thickness in zip(trial["front_layers"], d, strict=True):
            row["thickness"] = dict(value=float(thickness), unit="nm")
        value = evaluate(trial, jacobian=True)
        if value["design"] != trial:
            raise ValueError("multispectral minimax evaluator returned another design")
        return commit(value, point, physical_call=True)

    initial_x = ds / scales
    responses = initial.get("responses", ())
    if responses and all("jacobian_values_by_layer_id" in row for row in responses):
        commit(initial, initial_x, physical_call=False)
    else:
        data(initial_x)
    assert cache_data is not None
    signed_conditions = len(cache_data[0])
    point = initial_x.copy()
    minimize = cast(_Minimize, import_module("scipy.optimize").minimize)

    def best_point() -> Array:
        return np.asarray([row["thickness"]["value"] for row in best["design"]["front_layers"]]) / scales

    def penalty(x: Array) -> tuple[float, Array]:
        d = x * scales
        below = np.minimum(d - low, 0.)
        above = np.zeros_like(d) if upper is None else np.maximum(d - upper, 0.)
        excess = 0. if total_cap is None else max(0., float(sum(d)) - total_cap)
        return (1. + float(below @ below + above @ above) + excess**2,
                2 * (below + above + excess) * scales)

    def total_constraint(epigraph: bool) -> tuple[_Constraint, ...]:
        if total_cap is None:
            return ()
        def fun(x: Array) -> float:
            cancel_check()
            return total_cap - float(sum(x[:len(ids)] * scales))
        def jac(x: Array) -> Array:
            cancel_check()
            return np.concatenate((-scales, [0.])) if epigraph else -scales.copy()
        return ({"type": "ineq", "fun": fun, "jac": jac},)

    for beta in BETAS:
        cancel_check()
        def loss(x: Array, beta: float = beta) -> float:
            measured = data(x)
            return penalty(x)[0] if measured is None else _soft_max_loss(*measured, beta)[0]
        def derivative(x: Array, beta: float = beta) -> Array:
            measured = data(x)
            return penalty(x)[1] if measured is None else _soft_max_loss(*measured, beta)[1]
        options: dict[str, int | float] = {"maxiter": max_iterations, "ftol": 2e-13}
        if soft_method == "L-BFGS-B":
            options.update(gtol=1e-9, maxls=25)
        outcome = minimize(loss, point, jac=derivative, method=soft_method, bounds=bounds,
                           constraints=total_constraint(False) if soft_method == "SLSQP" else (), options=options)
        cancel_check()
        stages.append(dict(stage="soft_max", method=soft_method, beta=beta, message=str(outcome.message),
                           iterations=int(getattr(outcome, "nit", 0)), success=bool(outcome.success)))
        endpoint = np.asarray(outcome.x, dtype=float)
        if endpoint.shape != ds.shape or not np.all(np.isfinite(endpoint)):
            raise ValueError("multispectral minimax solver returned invalid endpoint")
        point = endpoint.copy() if feasible(endpoint * scales) else best_point()
        data(point)

    point = best_point()
    data(point)
    assert cache_data is not None
    start = np.concatenate((point, [max(0., float(np.max(cache_data[0])))]))

    def epigraph_loss(x: Array) -> float:
        cancel_check()
        return float(x[-1])

    def epigraph_derivative(x: Array) -> Array:
        cancel_check()
        return np.concatenate((np.zeros(len(ids)), [1.]))

    def epigraph_constraint(x: Array) -> Array:
        measured = data(x[:-1])
        if measured is None:
            return np.full(signed_conditions, -penalty(x[:-1])[0])
        return x[-1] - measured[0]

    def epigraph_jacobian(x: Array) -> Array:
        measured = data(x[:-1])
        if measured is None:
            derivatives = np.tile(-penalty(x[:-1])[1], (signed_conditions, 1))
            return np.column_stack((derivatives, np.zeros(signed_conditions)))
        return np.column_stack((-measured[1], np.ones(signed_conditions)))

    inequalities: tuple[_Constraint, ...] = (*total_constraint(True),
        {"type": "ineq", "fun": epigraph_constraint, "jac": epigraph_jacobian})
    outcome = minimize(epigraph_loss, start, jac=epigraph_derivative, method="SLSQP",
                       bounds=[*bounds, (0., None)], constraints=inequalities,
                       options={"maxiter": max_iterations, "ftol": 1e-11})
    cancel_check()
    endpoint = np.asarray(outcome.x, dtype=float)
    if endpoint.shape != (len(ids) + 1,) or not np.all(np.isfinite(endpoint)):
        raise ValueError("multispectral minimax solver returned invalid epigraph endpoint")
    data(endpoint[:-1])
    stages.append(dict(stage="epigraph", method="SLSQP", message=str(outcome.message),
                       iterations=int(getattr(outcome, "nit", 0)), success=bool(outcome.success), optimizer_t=float(endpoint[-1])))
    # Coordinate roundtrips can score one ULP worse than the retained initial
    # value. Keep the original ranking/tie behavior, but never export its
    # internal infinity sentinel as if it were an observed optical statistic.
    diagnostic_signed: float | None = best_worst_signed
    diagnostic_source = "parent_evaluated_candidate_responses"
    if not math.isfinite(best_worst_signed):
        if best.get("responses"):
            q, _, _ = _signed_bounds(request, best, ())
            diagnostic_signed = float(np.max(q))
            diagnostic_source = "retained_initial_cached_responses"
        else:
            diagnostic_signed = None
            diagnostic_source = "retained_initial_responses_unavailable_without_additional_physical_evaluation"
    return best, dict(method="analytic_multispectral_softmax_epigraph", betas=list(BETAS), stages=stages,
        max_iterations_per_stage=max_iterations, physical_candidates=calls, cache_hits=cache_hits,
        rejected_infeasible_trials=rejected, max_error=best_key[1], weighted_rms_error=best_key[2],
        max_signed_violation=diagnostic_signed, max_signed_violation_source=diagnostic_source,
        training_conditions=len(_original_reduction(request, best, ()).conditions) if best.get("responses") else None,
        raw_wavelength_points=sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives),
        signed_bound_conditions=signed_conditions,
        thickness_bounds_source="full explicit layer/total nm bounds; positive float lower bound if unset",
        soft_method=soft_method, coordinate_scale_nm=coordinate_scale_nm, scale_nm=scales.tolist(),
        guide_weighting="normalized band weight/(angles*active reductions) times wavelength quadrature; signed sides split each condition mass",
        cache_scope="one most recent thickness point shared across beta and epigraph t",
        acceptance_scope="parent active nominal pass, max_error then weighted_rms_error; exact ties prefer lower max signed q; independent original audit required")
