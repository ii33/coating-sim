"""Parent-owned analytic HR soft-min continuation and sampled epigraph repair.

This optimizes only the original discrete training conditions. The parent owns
physical work accounting, budgets, cancellation and independent final audit.
"""
from __future__ import annotations

from copy import deepcopy
from importlib import import_module
import math
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Protocol, Sequence, TypedDict, cast

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from .film_auto_synthesis import FilmSynthesisRequest

Array = NDArray[np.float64]
Value = Mapping[str, Any]
BETAS = (100., 400., 1500.)


class _Constraint(TypedDict):
    type: str
    fun: Callable[[Array], float | Array]
    jac: Callable[[Array], Array]


class _Result(Protocol):
    @property
    def x(self) -> Array: ...
    @property
    def message(self) -> str: ...
    @property
    def nit(self) -> int: ...
    @property
    def success(self) -> bool: ...


class _Minimize(Protocol):
    def __call__(self, fun: Callable[[Array], float], x0: Array, *,
                 jac: Callable[[Array], Array], method: str,
                 bounds: Sequence[tuple[float | None, float | None]],
                 constraints: Sequence[_Constraint],
                 options: Mapping[str, int | float]) -> _Result: ...


def _pure_hr(request: FilmSynthesisRequest) -> bool:
    return bool(request.objectives) and all(
        o.metric == "R" and o.mode == "range" and o.weight > 0
        and o.lower_bound is not None and o.lower_bound >= .5
        and (o.upper_bound is None or o.upper_bound >= 1.)
        for o in request.objectives)


def _responses(request: FilmSynthesisRequest, value: Value,
               ids: tuple[str, ...]) -> tuple[Array, Array]:
    """Require each original objective/angle row exactly once, in any order."""
    expected = {(i, float(a)) for i, o in enumerate(request.objectives) for a in o.angles_deg}
    rows: dict[tuple[int, float], tuple[Array, Array]] = {}
    for row in value.get("responses", ()):
        index = row["objective_index"]
        if type(index) is not int or not 0 <= index < len(request.objectives):
            raise ValueError("HR response objective index is invalid")
        angle = row["angle_deg"]
        if isinstance(angle, bool) or not isinstance(angle, (int, float)) or not math.isfinite(angle):
            raise ValueError("HR response angle is invalid")
        identity = (index, float(angle))
        if identity not in expected or identity in rows:
            raise ValueError("HR response repeats or changes an original condition")
        objective = request.objectives[index]
        waves = np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm,
                            objective.wavelength_samples)
        actual = np.asarray(row["actual_values"], dtype=float)
        if (not np.array_equal(np.asarray(row["wavelength_nm"], dtype=float), waves)
                or actual.shape != waves.shape or not np.all(np.isfinite(actual))):
            raise ValueError("HR response grid/values differ from original training conditions")
        derivatives = row.get("jacobian_values_by_layer_id", {})
        if set(derivatives) != set(ids):
            raise ValueError("HR response requires exactly the optimized layer Jacobians")
        columns = [np.asarray(derivatives[identifier], dtype=float) for identifier in ids]
        if any(column.shape != waves.shape or not np.all(np.isfinite(column)) for column in columns):
            raise ValueError("HR response Jacobian shape/values are invalid")
        rows[identity] = (actual, np.column_stack(columns))
    if set(rows) != expected:
        raise ValueError("HR response is missing original training conditions")
    ordered = [rows[key] for key in sorted(expected)]
    return np.concatenate([item[0] for item in ordered]), np.vstack([item[1] for item in ordered])


def _soft_min_loss(values: Array, jacobian: Array, beta: float) -> tuple[float, Array]:
    """Stable logsumexp(-beta*R)/beta and its thickness derivative."""
    logits = -beta * values
    shift = float(np.max(logits))
    weights = np.exp(logits - shift)
    denominator = float(np.sum(weights))
    return (shift + math.log(denominator)) / beta, -(weights @ jacobian) / denominator


def refine_hr_minimax(request: FilmSynthesisRequest, initial: Value, *,
                      evaluate: Callable[..., Value], archive: Callable[..., None],
                      cancel_check: Callable[[], None] = lambda: None,
                      progress: Callable[..., None] = lambda *args: None,
                      max_iterations: int = 180,
                      soft_method: Literal["SLSQP", "L-BFGS-B"] = "L-BFGS-B",
                      coordinate_scale_nm: float | None = 100.) -> tuple[Value, dict[str, Any]]:
    """Return the completed feasible candidate with greatest original sampled minR.

    Default variables are d/(100 nm), with L-BFGS-B soft stages followed by
    SLSQP epigraph. A None scale selects max(initial d,1 nm); SLSQP soft stages
    are available as an explicit comparison. All stages retain full physical
    layer bounds; no phase trust box or implicit upper bound is introduced.
    Only the most recent physical R/Jacobian is cached, shared across beta/t.
    Exceptions from the parent's budget/cancel/evaluation callbacks propagate.
    """
    cancel_check()
    if not _pure_hr(request):
        raise ValueError("HR minimax requires exclusively positive-weight R lower-bound objectives")
    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("HR minimax max_iterations must be a positive integer")
    if soft_method not in ("SLSQP", "L-BFGS-B"):
        raise ValueError("HR minimax soft_method is unsupported")
    if coordinate_scale_nm is not None and (isinstance(coordinate_scale_nm, bool)
            or not math.isfinite(coordinate_scale_nm) or coordinate_scale_nm <= 0):
        raise ValueError("HR minimax coordinate scale must be positive finite nm")
    design = initial["design"]
    layers = design["front_layers"]
    if not layers:
        return initial, dict(method="none", reason="empty_topology", physical_candidates=0)
    ids = tuple(row["id"] for row in layers)
    if len(set(ids)) != len(ids):
        raise ValueError("HR minimax requires unique layer ids")
    if request.max_layers is not None and len(ids) > request.max_layers:
        raise ValueError("HR minimax initial topology exceeds max_layers")
    ds = np.asarray([row["thickness"]["value"] for row in layers], dtype=float)
    if any(row["thickness"]["unit"] != "nm" for row in layers):
        raise ValueError("HR minimax requires normalized nm thicknesses")
    scales = (np.maximum(ds, 1.) if coordinate_scale_nm is None
              else np.full(ds.shape, coordinate_scale_nm, dtype=float))
    low = request.minimum_nonzero_thickness_nm or np.finfo(float).tiny
    upper = request.max_layer_thickness_nm
    total_cap = request.max_total_thickness_nm

    def feasible(d: Array) -> bool:
        return bool(np.all(np.isfinite(d)) and np.all(d >= low)
                    and (upper is None or np.all(d <= upper))
                    and (total_cap is None or float(sum(d)) <= total_cap))

    if not feasible(ds):
        raise ValueError("HR minimax requires a physically feasible initial candidate")
    bounds: list[tuple[float | None, float | None]] = [
        (low / float(s), None if upper is None else upper / float(s)) for s in scales]
    cache_x: Array | None = None
    cache_data: tuple[Array, Array] | None = None
    best: Value = initial
    best_min = -math.inf
    calls = cache_hits = rejected = 0
    stages: list[dict[str, Any]] = []

    def commit(value: Value, point: Array, *, physical_call: bool) -> tuple[Array, Array]:
        nonlocal cache_x, cache_data, best, best_min, calls
        actual, jacobian = _responses(request, value, ids)
        if physical_call:
            calls += 1
            archive(value, audit_eligible=False)
        minimum = float(np.min(actual))
        if minimum > best_min:
            best, best_min = value, minimum
        cache_x, cache_data = point.copy(), (actual, jacobian * scales)
        if physical_call:
            progress("hr_minimax_trial", value,
                     dict(physical_candidates=calls, sampled_min_reflectance=minimum))
        cancel_check()
        return cache_data

    def data(point: Array) -> tuple[Array, Array] | None:
        nonlocal cache_hits, rejected
        cancel_check()
        point = np.asarray(point, dtype=float)
        if point.shape != ds.shape or not np.all(np.isfinite(point)):
            raise ValueError("HR minimax optimizer returned invalid coordinates")
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
            raise ValueError("HR minimax evaluator returned another design")
        return commit(value, point, physical_call=True)

    initial_x = ds / scales
    responses = initial.get("responses", ())
    if responses and all("jacobian_values_by_layer_id" in row for row in responses):
        commit(initial, initial_x, physical_call=False)
    else:
        data(initial_x)
    assert cache_data is not None
    point = initial_x.copy()
    minimize = cast(_Minimize, import_module("scipy.optimize").minimize)
    conditions = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)

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
            return penalty(x)[0] if measured is None else _soft_min_loss(*measured, beta)[0]
        def derivative(x: Array, beta: float = beta) -> Array:
            measured = data(x)
            return penalty(x)[1] if measured is None else _soft_min_loss(*measured, beta)[1]
        options: dict[str, int | float] = {"maxiter": max_iterations, "ftol": 2e-13}
        if soft_method == "L-BFGS-B":
            options.update(gtol=1e-9, maxls=25)
        outcome = minimize(loss, point, jac=derivative, method=soft_method, bounds=bounds,
                           constraints=total_constraint(False) if soft_method == "SLSQP" else (),
                           options=options)
        cancel_check()
        stages.append(dict(stage="soft_min", method=soft_method, beta=beta, message=str(outcome.message),
                           iterations=int(outcome.nit), success=bool(outcome.success)))
        # Continuation may follow a legal last iterate while retaining the
        # separately best physical candidate, never an infeasible endpoint.
        endpoint = np.asarray(outcome.x, dtype=float)
        if endpoint.shape != ds.shape or not np.all(np.isfinite(endpoint)):
            raise ValueError("HR minimax solver returned invalid endpoint")
        point = (endpoint.copy() if feasible(endpoint * scales) else
                 np.asarray([row["thickness"]["value"] for row in best["design"]["front_layers"]]) / scales)

    point = np.asarray([row["thickness"]["value"] for row in best["design"]["front_layers"]]) / scales
    data(point)
    assert cache_data is not None
    start = np.concatenate((point, [float(np.min(cache_data[0]))]))

    def epigraph_loss(x: Array) -> float:
        cancel_check()
        return -float(x[-1])

    def epigraph_derivative(x: Array) -> Array:
        cancel_check()
        return np.concatenate((np.zeros(len(ids)), [-1.]))

    def epigraph_constraint(x: Array) -> Array:
        measured = data(x[:-1])
        if measured is None:
            return np.full(conditions, -penalty(x[:-1])[0])
        return measured[0] - x[-1]

    def epigraph_jacobian(x: Array) -> Array:
        measured = data(x[:-1])
        if measured is None:
            derivatives = np.tile(-penalty(x[:-1])[1], (conditions, 1))
            return np.column_stack((derivatives, np.zeros(conditions)))
        return np.column_stack((measured[1], -np.ones(conditions)))

    inequalities: tuple[_Constraint, ...] = (*total_constraint(True),
        {"type": "ineq", "fun": epigraph_constraint, "jac": epigraph_jacobian})
    outcome = minimize(epigraph_loss, start, jac=epigraph_derivative, method="SLSQP",
                       bounds=[*bounds, (None, 1.)], constraints=inequalities,
                       options={"maxiter": max_iterations, "ftol": 1e-11})
    cancel_check()
    if (np.asarray(outcome.x).shape != (len(ids) + 1,)
            or not np.all(np.isfinite(outcome.x))):
        raise ValueError("HR minimax solver returned invalid epigraph endpoint")
    stages.append(dict(stage="epigraph", method="SLSQP", message=str(outcome.message),
                       iterations=int(outcome.nit), success=bool(outcome.success),
                       optimizer_t=float(outcome.x[-1])))
    return best, dict(method="analytic_hr_softmin_epigraph", betas=list(BETAS), stages=stages,
        max_iterations_per_stage=max_iterations, physical_candidates=calls,
        cache_hits=cache_hits, rejected_infeasible_trials=rejected,
        sampled_min_reflectance=best_min, training_conditions=conditions,
        thickness_bounds_source="full explicit layer/total nm bounds; positive float lower bound if unset",
        soft_method=soft_method, coordinate_scale_nm=coordinate_scale_nm,
        scale_nm=scales.tolist(), cache_scope="one most recent thickness point shared across beta and epigraph t",
        acceptance_scope="original sampled R only; independent original audit required")
