"""Version 2 ideal sensitivity with an immutable full-grid degradation guard.

The optical objective, physical Jacobian, F0 conditioning and raw-block accounting
are shared with schema 1. Only candidate acceptance/constraint bounds change.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import inspect
import json
import math
from typing import Any, Mapping

import numpy as np

from . import film_ideal_sensitivity as v1
from .film_optimization import (
    FilmOptimizationObjective,
    FilmOptimizationRequest,
    FilmThicknessVariable,
)

SCHEMA_VERSION = 2
REQUEST_KIND = v1.REQUEST_KIND
RESULT_KIND = v1.RESULT_KIND
POLICIES = ("strict_nominal", "bounded_baseline_degradation")


@dataclass(frozen=True)
class FilmIdealDegradationBudget:
    objective_id: str
    delta: float
    unit: str = "percentage_point"


@dataclass(frozen=True)
class FilmIdealSensitivityV2Request(v1.FilmIdealSensitivityRequest):
    schema_version: int = SCHEMA_VERSION
    policy: str = "bounded_baseline_degradation"
    objective_ids: tuple[str, ...] = ()
    budgets: tuple[FilmIdealDegradationBudget, ...] = ()
    kernel_identity: str | None = None


@dataclass(frozen=True)
class FilmIdealDegradationRow:
    objective_id: str
    delta_percentage_point: float
    delta_fraction: float
    consumed_fraction: float
    excess_fraction: float
    wavelength_nm: float
    angle_deg: float
    polarization: str


@dataclass(frozen=True)
class FilmIdealDegradationGuard:
    guard_pass: bool
    max_directional_degradation_fraction: float
    worst_objective_id: str | None
    wavelength_nm: float | None
    angle_deg: float | None
    polarization: str | None
    rows: tuple[FilmIdealDegradationRow, ...]


@dataclass(frozen=True)
class FilmIdealSensitivityBaselineCondition:
    objective_id: str
    objective_index: int
    wavelength_nm: float
    wavelength_bits: str
    angle_deg: float
    angle_bits: str
    polarization: str
    metric: str


@dataclass(frozen=True)
class FilmIdealSensitivityBaselineEvidence:
    request_identity: str
    kernel_identity: str
    dtype: str
    spectral_unit: str
    conditions: tuple[FilmIdealSensitivityBaselineCondition, ...]
    values: tuple[float, ...]
    digest: str


@dataclass(frozen=True)
class FilmIdealSensitivityV2Result(v1.FilmIdealSensitivityResult):
    schema_version: int = SCHEMA_VERSION
    policy: str = "bounded_baseline_degradation"
    baseline_guard: FilmIdealDegradationGuard | None = None
    best_guard: FilmIdealDegradationGuard | None = None
    baseline_evidence: FilmIdealSensitivityBaselineEvidence | None = None

    @property
    def baseline_locked(self):
        return self.baseline_evidence is not None

    @property
    def improvement_status(self):
        if self.best_analysis is None or self.baseline_analysis is None:
            return "unavailable"
        return "improved" if self.improved else "no_improvement"

    @property
    def guard_pass(self):
        return self.best_guard is not None and self.best_guard.guard_pass

    @property
    def usable_candidate(self):
        return (
            self.best_design is not None
            and self.best_analysis is not None
            and self.baseline_analysis is not None
            and self.guard_pass
            and self.best_analysis.objective_value <= self.baseline_analysis.objective_value
        )


def _as_v1(request):
    return v1.FilmIdealSensitivityRequest(
        **{f.name: getattr(request, f.name) for f in fields(v1.FilmIdealSensitivityRequest)}
    )


def _kernel_identity():
    # Fail closed across numerical implementation changes, not just materials.
    from . import film_optimization_derivatives, film_optics, optical_constants
    from . import film_design, film_design_service

    h = hashlib.sha256()
    for module in (
        film_optimization_derivatives,
        film_optics,
        optical_constants,
        film_design,
        film_design_service,
    ):
        h.update(module.__name__.encode())
        try:
            h.update(inspect.getsource(module).encode())
        except (OSError, TypeError) as exc:
            raise ValueError("cannot establish physical kernel identity") from exc
    h.update(np.__version__.encode())
    return h.hexdigest()


def _evidence_digest(evidence):
    data = asdict(evidence)
    data.pop("digest")
    data["values_bits"] = np.asarray(evidence.values, dtype="<f8").tobytes().hex()
    return hashlib.sha256(
        json.dumps(
            v1._json_native(data), sort_keys=True, ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def _baseline_evidence(request, evaluation):
    if request.kernel_identity is None:
        raise ValueError("baseline requires a frozen kernel identity")
    conditions = tuple(
        FilmIdealSensitivityBaselineCondition(
            request.objective_ids[i],
            i,
            wave,
            np.asarray(wave, dtype="<f8").tobytes().hex(),
            angle,
            np.asarray(angle, dtype="<f8").tobytes().hex(),
            o.polarization,
            o.metric,
        )
        for i, wave, angle, o in evaluation.conditions
    )
    evidence = FilmIdealSensitivityBaselineEvidence(
        v1._request_identity(request),
        request.kernel_identity,
        "<f8",
        "fraction",
        conditions,
        tuple(float(x) for x in evaluation.values),
        "",
    )
    if len(evidence.values) != _point_count(request) or not all(
        math.isfinite(x) for x in evidence.values
    ):
        raise ValueError("baseline must be complete and finite before locking")
    return replace(evidence, digest=_evidence_digest(evidence))


def _point_count(request):
    return sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)


def _validate(request):
    if not isinstance(request, FilmIdealSensitivityV2Request):
        raise ValueError("request must be a FilmIdealSensitivityV2Request")
    if type(request.schema_version) is not int or request.schema_version != SCHEMA_VERSION:
        raise ValueError("ideal sensitivity v2 requires schema_version 2")
    base = v1._validate(_as_v1(request))
    if request.policy not in POLICIES:
        raise ValueError("unknown ideal sensitivity policy")
    ids = v1._sequence(request.objective_ids, "objective_ids")
    if (
        len(ids) != len(base.objectives)
        or any(not isinstance(i, str) or not i.strip() for i in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("objective_ids must uniquely identify every frozen objective row")
    budgets = v1._sequence(request.budgets, "budgets")
    seen = set()
    normalized = []
    for b in budgets:
        if not isinstance(b, FilmIdealDegradationBudget):
            raise ValueError("budgets must contain FilmIdealDegradationBudget values")
        if (
            not isinstance(b.objective_id, str)
            or b.objective_id not in ids
            or b.objective_id in seen
        ):
            raise ValueError("budgets must uniquely match stable objective_id values")
        if b.unit != "percentage_point":
            raise ValueError("degradation budget unit must explicitly be percentage_point")
        delta = v1._finite(b.delta, "delta")
        if delta < 0:
            raise ValueError("delta must be nonnegative")
        # Reject a conversion that would silently erase a nonzero allowance.
        if delta > 0 and delta / 100.0 == 0:
            raise ValueError("percentage-point degradation budget underflows")
        normalized.append(replace(b, delta=delta))
        seen.add(b.objective_id)
    if request.policy == "bounded_baseline_degradation" and seen != set(ids):
        raise ValueError(
            "bounded_baseline_degradation requires an explicit budget for every objective_id"
        )
    if request.policy == "strict_nominal" and budgets:
        raise ValueError("strict_nominal does not consume degradation budgets")
    result = replace(
        request,
        **{f.name: getattr(base, f.name) for f in fields(base)},
        objective_ids=ids,
        budgets=tuple(normalized),
    )
    if result.kernel_identity is not None and result.kernel_identity != _kernel_identity():
        raise ValueError("frozen physical kernel identity differs from the current implementation")
    return result


def ideal_sensitivity_v2_request_from_optimization(
    base: FilmOptimizationRequest,
    *,
    sensitivity_mode: str,
    policy: str,
    objective_ids: tuple[str, ...],
    budgets: tuple[FilmIdealDegradationBudget, ...],
    sensitivity_layer_ids: tuple[str, ...] | None = None,
    max_iterations: int | None = None,
    max_evaluations: int | None = None,
    strict_lower_bound_objective_indices: tuple[int, ...] = (),
    strict_upper_bound_objective_indices: tuple[int, ...] = (),
) -> FilmIdealSensitivityV2Request:
    ordinary = v1.ideal_sensitivity_request_from_optimization(
        base,
        sensitivity_mode=sensitivity_mode,
        sensitivity_layer_ids=sensitivity_layer_ids,
        max_iterations=max_iterations,
        max_evaluations=max_evaluations,
        strict_lower_bound_objective_indices=strict_lower_bound_objective_indices,
        strict_upper_bound_objective_indices=strict_upper_bound_objective_indices,
    )
    return _validate(
        FilmIdealSensitivityV2Request(
            **{f.name: getattr(ordinary, f.name) for f in fields(ordinary)},
            policy=policy,
            objective_ids=objective_ids,
            budgets=budgets,
        )
    )


def estimate_film_ideal_sensitivity_resources(request):
    request = _validate(request)
    base = v1._v1_estimate_film_ideal_sensitivity_resources(_as_v1(request))
    if request.policy == "strict_nominal":
        m = base["equality_constraints"] + base["inequality_constraints"]
    else:
        m = sum(
            o.wavelength_samples
            * len(o.angles_deg)
            * (
                2
                if o.mode == "value" or (o.lower_bound is not None and o.upper_bound is not None)
                else 1
            )
            for i, o in zip(request.objective_ids, request.objectives, strict=True)
        )
        n = base["free_variables"]
        modern = n * (n + 1) // 2 + 3 * m * n + 9 * m + 8 * n * n + 35 * n + 28
        legacy = (3 * (n + 1) + m) * (n + 2) + (n + 2) * (m + 2 * n + 2) + 2 * m + 12 * n + 32
        workspace = 8 * (max(modern, legacy, 1) + max(1, m) * n + 3 * max(1, m + 2 * n + 2))
        base["estimated_working_bytes"] += workspace - base["estimated_slsqp_workspace_bytes"]
        base["estimated_slsqp_workspace_bytes"] = workspace
        base["equality_constraints"] = 0
        base["inequality_constraints"] = m
    # One exact candidate constraint cache plus conservative double-size build
    # temporaries/list storage. Full immutable baseline and per-row diagnostics
    # remain additional to v1 retained analysis/Jacobian estimates.
    constraint_cache = 8 * m * (base["free_variables"] + 1)
    baseline_bytes = 1024 * _point_count(request) + 1024 + 768 * len(request.objectives)
    base["estimated_constraint_cache_bytes"] = constraint_cache
    build_peak = 3 * constraint_cache + 192 * m
    base["estimated_constraint_cache_build_bytes"] = build_peak
    base["estimated_frozen_baseline_bytes"] = baseline_bytes
    base["estimated_working_bytes"] += constraint_cache + build_peak + baseline_bytes
    base["estimated_retained_result_bytes"] += baseline_bytes + len(repr(asdict(request))) * 4
    return base


def _check_resources(request):
    resources = estimate_film_ideal_sensitivity_resources(request)
    if resources["estimated_retained_result_bytes"] > v1.MAX_RETAINED_RESULT_BYTES:
        raise ValueError(
            "ideal sensitivity retained-result resource limit exceeded; no grid or columns were dropped"
        )
    if resources["estimated_working_bytes"] > v1.MAX_WORKING_BYTES:
        raise ValueError(
            "ideal sensitivity SLSQP/working resource limit exceeded; no grid or columns were dropped"
        )
    return resources


def _freeze_materials(service, request):
    request = _validate(request)
    _check_resources(request)
    base = v1._v1_freeze_film_ideal_sensitivity_request(service, _as_v1(request))
    return _validate(
        replace(
            request,
            **{f.name: getattr(base, f.name) for f in fields(base)},
            kernel_identity=_kernel_identity(),
        )
    )


def freeze_film_ideal_sensitivity_request(service, request):
    """Freeze physical inputs only; y0 is locked by the run's first full F/J."""
    return _freeze_materials(service, request)


def _sides(objective, delta):
    if objective.mode == "value":
        return True, True
    return objective.lower_bound is not None, objective.upper_bound is not None


def _guard(evaluation, request, baseline_evidence):
    if baseline_evidence is None:
        raise ValueError("degradation guard requires the immutable frozen baseline")
    if request.policy == "strict_nominal":
        return FilmIdealDegradationGuard(
            evaluation.analysis.nominal_targets_met, 0.0, None, None, None, None, ()
        )
    budgets = {b.objective_id: b.delta / 100.0 for b in request.budgets}
    pp = {b.objective_id: b.delta for b in request.budgets}
    rows = []
    passes = True
    for index, identifier in enumerate(request.objective_ids):
        delta = budgets[identifier]
        lower, upper = _sides(request.objectives[index], delta)
        samples = []
        for actual, baseline, (i, wave, angle, o) in zip(
            evaluation.values, baseline_evidence.values, evaluation.conditions, strict=True
        ):
            if i != index:
                continue
            # Closed comparisons, not an optimizer ftol and not a nominal epsilon.
            passed = (not lower or actual >= baseline - delta) and (
                not upper or actual <= baseline + delta
            )
            passes = passes and bool(passed)
            consumed = max(
                0.0,
                float(baseline - actual) if lower else 0.0,
                float(actual - baseline) if upper else 0.0,
            )
            excess = max(
                0.0,
                float((baseline - delta) - actual) if lower else 0.0,
                float(actual - (baseline + delta)) if upper else 0.0,
            )
            samples.append((consumed, excess, wave, angle, o.polarization))
        consumed, _excess, wave, angle, pol = max(samples, key=lambda item: item[0])
        rows.append(
            FilmIdealDegradationRow(
                identifier,
                pp[identifier],
                delta,
                consumed,
                max(item[1] for item in samples),
                wave,
                angle,
                pol,
            )
        )
    worst = max(rows, key=lambda row: row.consumed_fraction)
    return FilmIdealDegradationGuard(
        passes,
        worst.consumed_fraction,
        worst.objective_id,
        worst.wavelength_nm,
        worst.angle_deg,
        worst.polarization,
        tuple(rows),
    )


def _constraint_rows(evaluation, request, free_indices, spans, baseline_evidence):
    if request.policy == "strict_nominal":
        return v1._constraint_rows(evaluation, request, free_indices, spans)
    if baseline_evidence is None:
        raise ValueError("constraint bounds require the immutable frozen baseline")
    deltas = {b.objective_id: b.delta / 100.0 for b in request.budgets}
    inequalities, jacobians = [], []
    for actual, baseline, derivative, (i, _wave, _angle, o) in zip(
        evaluation.values,
        baseline_evidence.values,
        evaluation.nominal_jacobian[:, free_indices],
        evaluation.conditions,
        strict=True,
    ):
        delta = deltas[request.objective_ids[i]]
        lower, upper = _sides(o, delta)
        derivative = derivative * spans / o.normalization_scale
        if lower:
            inequalities.append((actual - (baseline - delta)) / o.normalization_scale)
            jacobians.append(derivative)
        if upper:
            inequalities.append(((baseline + delta) - actual) / o.normalization_scale)
            jacobians.append(-derivative)
    n = len(free_indices)
    return (
        np.asarray(inequalities),
        np.asarray(jacobians).reshape(-1, n),
        np.empty(0),
        np.empty((0, n)),
    )


class _ConstraintRowsCache:
    """One exact point only; no writable aliases and no stale failed construction."""

    def __init__(self):
        self.key = self.rows = None

    def get(self, identity, z, build):
        key = (identity, np.asarray(z, dtype="<f8").tobytes())
        if key == self.key:
            return self.rows
        # Drop the old matrices before constructing another full constraint set.
        self.key = self.rows = None
        rows = tuple(
            np.frombuffer(np.asarray(row, dtype=float).tobytes(), dtype=float).reshape(
                np.asarray(row).shape
            )
            for row in build()
        )
        self.key, self.rows = key, rows
        return rows


def _bounded_stencil(value, lower, upper, *, step_scale=1.0):
    """Use the largest legal physical 3-point template, with deterministic ties."""
    value, lower, upper = float(value), float(lower), float(upper)
    step_scale = v1._finite(step_scale, "step_scale")
    if step_scale <= 0:
        raise ValueError("step_scale must be finite and positive")
    if not (
        math.isfinite(value)
        and math.isfinite(lower)
        and math.isfinite(upper)
        and 0 <= lower <= value <= upper
    ):
        raise v1._FDDomainInsufficient(
            "fd_domain_insufficient: invalid physical finite-difference domain"
        )
    hbase = np.cbrt(np.finfo(float).eps) * max(1.0, abs(value)) * step_scale
    if not math.isfinite(hbase) or hbase <= 0:
        raise v1._FDDomainInsufficient(
            "fd_domain_insufficient: no finite positive finite-difference step"
        )
    left, right = value - lower, upper - value
    candidates = []
    for order, h, a, b in (
        (0, min(hbase, left, right), -1.0, 1.0),
        (1, min(hbase, right / 2.0), 1.0, 2.0),
        (2, min(hbase, left / 2.0), -1.0, -2.0),
    ):
        x1, x2 = value + a * h, value + b * h
        if (
            h > 0
            and math.isfinite(x1)
            and math.isfinite(x2)
            and lower <= x1 <= upper
            and lower <= x2 <= upper
            and x1 != value
            and x2 != value
            and x1 != x2
        ):
            candidates.append((h, -order, x1, x2))
    if not candidates:
        raise v1._FDDomainInsufficient(
            "fd_domain_insufficient: cannot fit two distinct legal physical probes"
        )
    _, _, x1, x2 = max(candidates)
    return x1, x2


def _execute(
    service,
    request,
    *,
    operation,
    cancel_event=None,
    progress_callback=None,
    checkpoint_callback=None,
):
    if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
        raise ValueError("cancel_event must provide is_set()")
    request = _freeze_materials(service, request)
    evaluator = v1._Evaluator(request, cancel_event)
    initial_design = deepcopy(request.design_snapshot)
    physical = evaluator.physical(initial_design)
    if any(not v.lower_nm <= physical[v.layer_id] <= v.upper_nm for v in request.variables):
        raise ValueError("initial physical thickness is outside explicit variable bounds")
    baseline = best = best_design = baseline_guard = best_guard = baseline_evidence = None
    iterations = 0
    resolved_maxiter = None
    scipy_version = None
    solver_scale = None

    def result(state, reason, message="", *, final=True):
        return FilmIdealSensitivityV2Result(
            state,
            reason,
            request.design_revision,
            deepcopy(best_design),
            {} if best_design is None else evaluator.physical(best_design),
            request,
            baseline,
            best,
            iterations,
            evaluator.evaluations,
            evaluator.forward,
            evaluator.jacobian,
            evaluator.identity,
            evaluator.probes,
            evaluator.cache_hits,
            evaluator.failures,
            evaluator.gradients,
            resolved_maxiter,
            operation,
            final,
            message,
            "raw_forward_jacobian_condition_blocks",
            scipy_version,
            solver_scale,
            None
            if baseline is None or solver_scale is None
            else baseline.objective_value / solver_scale,
            None if best is None or solver_scale is None else best.objective_value / solver_scale,
            schema_version=SCHEMA_VERSION,
            policy=request.policy,
            baseline_guard=baseline_guard,
            best_guard=best_guard,
            baseline_evidence=baseline_evidence,
        )

    def emit(stage, state="running", message=""):
        if progress_callback is not None:
            progress_callback(
                v1.FilmIdealSensitivityProgress(
                    stage,
                    state,
                    iterations,
                    evaluator.evaluations,
                    evaluator.forward,
                    evaluator.jacobian,
                    evaluator.probes,
                    evaluator.cache_hits,
                    None if best is None else best.objective_value,
                    message,
                )
            )

    def accept(design, evaluation):
        nonlocal best, best_design, best_guard
        analysis = evaluation.analysis
        guard = _guard(evaluation, request, baseline_evidence)
        if guard.guard_pass and (best is None or analysis.objective_value < best.objective_value):
            if baseline is not None and analysis.objective_value > baseline.objective_value:
                raise ValueError("incumbent objective cannot be worse than its baseline")
            best, best_design, best_guard = analysis, deepcopy(design), guard
            if checkpoint_callback is not None:
                checkpoint_callback(
                    film_ideal_sensitivity_result_to_payload(
                        result("partial", "checkpoint", final=False)
                    )
                )

    try:
        emit("baseline")
        base_evaluation = evaluator.evaluate(initial_design)
        evaluator.check()
        candidate_evidence = _baseline_evidence(request, base_evaluation)
        evaluator.check()
        baseline_evidence = candidate_evidence
        # Cache lifetime starts after the first complete baseline lock. No
        # constraint arrays are constructed or reusable before this point.
        baseline = base_evaluation.analysis
        baseline_guard = _guard(base_evaluation, request, baseline_evidence)
        if operation == "analyze":
            # An analysis is not an optimization incumbent and may explicitly
            # describe a nominal-infeasible design.
            best, best_design, best_guard = baseline, initial_design, baseline_guard
            return result("completed", "analysis_complete")
        if request.policy == "strict_nominal" and not baseline.nominal_targets_met:
            return result(
                "failed",
                "initial_nominal_infeasible",
                "Initial design fails unchanged original nominal bounds; no feasible candidate is claimed.",
            )
        accept(initial_design, base_evaluation)
        free_indices = [i for i, v in enumerate(request.variables) if v.lower_nm < v.upper_nm]
        if not free_indices:
            return result(
                "failed",
                "all_variables_fixed",
                "No free physical thickness variable; analysis only, no optimization performed.",
            )
        import scipy  # type: ignore[import-untyped]

        scipy_version = scipy.__version__
        solver_scale = baseline.objective_value
        if solver_scale == 0.0:
            solver_scale = None
            return result(
                "completed",
                "zero_sensitivity_objective",
                "The feasible initial local derivative objective is exactly zero; this is not a finite-error or manufacturing-yield certificate.",
            )
        from scipy.optimize import minimize  # type: ignore[import-untyped]
        from scipy.optimize._slsqp_py import _minimize_slsqp  # type: ignore[import-untyped]

        resolved_maxiter = (
            request.max_iterations
            if request.max_iterations is not None
            else int(inspect.signature(_minimize_slsqp).parameters["maxiter"].default)
        )
        variables = [request.variables[i] for i in free_indices]
        lows = np.asarray([v.lower_nm for v in variables])
        highs = np.asarray([v.upper_nm for v in variables])
        spans = highs - lows
        initial_values = np.asarray([physical[v.layer_id] for v in variables])
        initial_z = (initial_values - lows) / spans
        initial_z = np.where(
            (initial_values > lows) & (initial_z == 0), np.nextafter(0.0, 1.0), initial_z
        )
        initial_z = np.where(
            (initial_values < highs) & (initial_z == 1), np.nextafter(1.0, 0.0), initial_z
        )
        last_key, last_data = None, None
        constraint_cache = _ConstraintRowsCache()

        def evaluate_z(z):
            nonlocal last_key, last_data
            evaluator.check()
            z = np.asarray(z, dtype=float)
            if (
                z.shape != lows.shape
                or not np.all(np.isfinite(z))
                or np.any(z < 0)
                or np.any(z > 1)
            ):
                raise ValueError("solver trial is outside normalized physical bounds")
            key = z.tobytes()
            if last_key == key:
                return last_data
            values = np.where(z <= 0.5, lows + z * spans, highs - (1.0 - z) * spans)
            values = np.where(z == 0, lows, np.where(z == 1, highs, values))
            values = np.where(z == initial_z, initial_values, values)
            design = v1._candidate_design(initial_design, variables, values)
            evaluation = evaluator.evaluate(design)
            accept(design, evaluation)
            last_key, last_data = key, (design, evaluation, values)
            return last_data

        def objective(z):
            value = evaluate_z(z)[1].analysis.objective_value / solver_scale
            if not math.isfinite(value):
                raise ValueError(
                    "fixed initial-objective conditioning overflows; trial rejected without clipping"
                )
            return value

        def gradient(z):
            design, evaluation, values = evaluate_z(z)
            answer = np.empty(len(variables))
            for col, (value, low, high) in enumerate(zip(values, lows, highs, strict=True)):
                evaluator.check()
                x1, x2 = _bounded_stencil(value, low, high)
                probes = []
                for probe_value in (x1, x2):
                    changed = values.copy()
                    changed[col] = probe_value
                    probe = v1._candidate_design(design, variables, changed)
                    data = evaluator.evaluate(probe, probe=True)
                    # Probes can be nominal-infeasible; they are never retained
                    # as available candidates, even if their objective is lower.
                    normalized_probe = data.analysis.objective_value / solver_scale
                    if not math.isfinite(normalized_probe):
                        raise ValueError(
                            "fixed initial-objective conditioning overflows at a finite-difference probe; no clipping"
                        )
                    probes.append(normalized_probe)
                normalized_center = evaluation.analysis.objective_value / solver_scale
                if not math.isfinite(normalized_center):
                    raise ValueError(
                        "fixed initial-objective conditioning overflows at the gradient center"
                    )
                answer[col] = (
                    v1._three_point_derivative(
                        normalized_center, probes[0], probes[1], value, x1, x2
                    )
                    * spans[col]
                )
                if not math.isfinite(answer[col]):
                    raise ValueError(
                        "fixed initial-objective gradient conditioning overflows; no partial gradient is delivered"
                    )
            evaluator.check()
            evaluator.gradients += 1
            return answer  # No half-computed gradient escapes on budget/cancel.

        def constraints(z, equality=False, derivative=False):
            data = evaluate_z(z)[1]
            rows = constraint_cache.get(
                evaluator.identity,
                z,
                lambda: _constraint_rows(data, request, free_indices, spans, baseline_evidence),
            )
            return rows[(2 if equality else 0) + int(derivative)]

        first_rows = constraint_cache.get(
            evaluator.identity,
            initial_z,
            lambda: _constraint_rows(
                base_evaluation, request, free_indices, spans, baseline_evidence
            ),
        )
        cons = []
        if len(first_rows[0]):
            cons.append(
                dict(
                    type="ineq",
                    fun=lambda z: constraints(z),
                    jac=lambda z: constraints(z, derivative=True),
                )
            )
        if len(first_rows[2]):
            cons.append(
                dict(
                    type="eq",
                    fun=lambda z: constraints(z, equality=True),
                    jac=lambda z: constraints(z, equality=True, derivative=True),
                )
            )

        del first_rows  # Do not retain the replaced baseline constraint matrices.

        def callback(z):
            nonlocal iterations
            iterations += 1
            evaluate_z(z)
            emit("search")

        options = dict(ftol=request.solver_objective_ftol)
        if request.max_iterations is not None:
            options["maxiter"] = request.max_iterations
        emit("search")
        solved = minimize(
            objective,
            initial_z,
            jac=gradient,
            bounds=[(0.0, 1.0)] * len(variables),
            constraints=cons,
            method="SLSQP",
            callback=callback,
            options=options,
        )
        evaluator.check()
        final_data = evaluate_z(solved.x)[1]
        if not _guard(final_data, request, baseline_evidence).guard_pass:
            return result(
                "partial",
                "solver_guard_rejected",
                "Final trial fails the exact policy guard; the last complete guard-feasible incumbent is retained.",
            )
        if best is not None and best.objective_value == baseline.objective_value:
            return result(
                "completed" if solved.success else "partial",
                "no_improvement",
                "No objective decrease: the unchanged full-grid guard-feasible baseline is retained.",
            )
        if solved.success:
            return result(
                "completed",
                "slsqp_converged",
                "Local derivative-sensitivity search stopped; no global or finite-error certificate.",
            )
        return result(
            "partial",
            "iteration_limit" if solved.status == 9 else "slsqp_stopped",
            str(solved.message),
        )
    except v1._Cancelled:
        return result(
            "cancelled",
            "cancelled",
            "Cancelled; only a complete policy-guard-feasible incumbent is retained.",
        )
    except v1._Budget:
        return result(
            "partial" if best is not None else "failed",
            "evaluation_limit",
            "Raw forward/Jacobian condition-block budget exhausted; no partial analysis or gradient is retained.",
        )
    except v1._FDDomainInsufficient as exc:
        return result("failed", "fd_domain_insufficient", str(exc))
    except (ValueError, ArithmeticError, FloatingPointError) as exc:
        return result("failed", "numerical_failure", str(exc))


def execute_film_ideal_sensitivity_request(
    service: Any,
    request: FilmIdealSensitivityV2Request,
    *,
    cancel_event=None,
    progress_callback=None,
    checkpoint_callback=None,
) -> FilmIdealSensitivityV2Result:
    return _execute(
        service,
        request,
        operation="optimize",
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        checkpoint_callback=checkpoint_callback,
    )


def analyze_film_ideal_sensitivity(
    service: Any,
    request: FilmIdealSensitivityV2Request,
    *,
    cancel_event=None,
    progress_callback=None,
) -> FilmIdealSensitivityV2Result:
    return _execute(
        service,
        request,
        operation="analyze",
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )


def _envelope(payload, cls, kind):
    payload = v1._json_native(payload)
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != kind
        or set(payload) - {f.name for f in fields(cls)} - {"kind"}
    ):
        raise ValueError(f"invalid {kind} v2 schema/type or unknown fields")
    data = deepcopy(dict(payload))
    data.pop("kind")
    return data


def film_ideal_sensitivity_request_to_payload(request):
    return v1._json_native(dict(kind=REQUEST_KIND, **asdict(_validate(request))))


def film_ideal_sensitivity_request_from_payload(payload):
    data = _envelope(payload, FilmIdealSensitivityV2Request, REQUEST_KIND)
    if not {"policy", "objective_ids", "budgets"} <= set(data):
        raise ValueError("v2 request requires explicit policy, objective_ids and budgets")
    if not isinstance(data["budgets"], (tuple, list)) or any(
        not isinstance(row, Mapping) or not {"objective_id", "delta", "unit"} <= set(row)
        for row in data["budgets"]
    ):
        raise ValueError("every budget requires explicit objective_id, delta and unit")
    try:
        data["objectives"] = tuple(
            replace(
                v1._record(FilmOptimizationObjective, row, "objective"),
                angles_deg=tuple(row.get("angles_deg", (0.0,))),
            )
            for row in data["objectives"]
        )
        data["variables"] = tuple(
            v1._record(FilmThicknessVariable, row, "variable") for row in data["variables"]
        )
        data["budgets"] = tuple(
            v1._record(FilmIdealDegradationBudget, row, "budget") for row in data["budgets"]
        )
        return _validate(v1._record(FilmIdealSensitivityV2Request, data, "v2 request"))
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid ideal sensitivity v2 request: {exc}") from exc


def _guard_from_payload(data):
    if data is None:
        return None
    result = v1._record(FilmIdealDegradationGuard, data, "degradation guard")
    if type(result.guard_pass) is not bool:
        raise ValueError("guard_pass must be a boolean")
    rows = tuple(v1._record(FilmIdealDegradationRow, row, "degradation row") for row in result.rows)
    return replace(result, rows=rows)


def _evidence_from_payload(data):
    if data is None:
        return None
    result = v1._record(FilmIdealSensitivityBaselineEvidence, data, "baseline evidence")
    conditions = tuple(
        v1._record(FilmIdealSensitivityBaselineCondition, row, "baseline condition")
        for row in result.conditions
    )
    values = tuple(v1._finite(x, "baseline value") for x in result.values)
    return replace(result, values=values, conditions=conditions)


def film_ideal_sensitivity_result_to_payload(result):
    if not isinstance(result, FilmIdealSensitivityV2Result):
        raise ValueError("result must be a FilmIdealSensitivityV2Result")
    payload = dict(kind=RESULT_KIND, **asdict(result))
    payload["frozen_request"] = film_ideal_sensitivity_request_to_payload(result.frozen_request)
    return v1._json_native(payload)


def film_ideal_sensitivity_result_from_payload(payload):
    data = _envelope(payload, FilmIdealSensitivityV2Result, RESULT_KIND)
    if "policy" not in data:
        raise ValueError("v2 result requires an explicit policy")
    try:
        data["frozen_request"] = film_ideal_sensitivity_request_from_payload(data["frozen_request"])
        data["baseline_analysis"] = v1._analysis_from_payload(data["baseline_analysis"])
        data["best_analysis"] = v1._analysis_from_payload(data["best_analysis"])
        data["baseline_guard"] = _guard_from_payload(data["baseline_guard"])
        data["best_guard"] = _guard_from_payload(data["best_guard"])
        data["baseline_evidence"] = _evidence_from_payload(data["baseline_evidence"])
        result = v1._record(FilmIdealSensitivityV2Result, data, "v2 result")
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid ideal sensitivity v2 result: {exc}") from exc
    if result.state not in ("completed", "partial", "failed", "cancelled"):
        raise ValueError("invalid ideal sensitivity result state")
    if result.policy != result.frozen_request.policy:
        raise ValueError("result policy differs from its frozen request")
    if result.budget_unit != "raw_forward_jacobian_condition_blocks" or (
        result.scipy_version is not None and not isinstance(result.scipy_version, str)
    ):
        raise ValueError("invalid budget unit or SciPy version")
    if result.operation not in ("optimize", "analyze"):
        raise ValueError("invalid ideal sensitivity operation")
    for name in (
        "design_revision",
        "iterations",
        "evaluations",
        "forward_evaluations",
        "jacobian_evaluations",
        "probe_evaluations",
        "cache_hits",
        "failed_evaluations",
        "fd_gradient_evaluations",
    ):
        if type(getattr(result, name)) is not int or getattr(result, name) < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    if result.resolved_max_iterations is not None and (
        type(result.resolved_max_iterations) is not int or result.resolved_max_iterations < 1
    ):
        raise ValueError("resolved_max_iterations must be a positive integer or null")
    if (
        type(result.is_final) is not bool
        or not isinstance(result.message, str)
        or not isinstance(result.termination_reason, str)
    ):
        raise ValueError("invalid result flags/message/reason")
    if not isinstance(result.best_thickness_nm, Mapping):
        raise ValueError("best_thickness_nm must be an object")
    for identifier, value in result.best_thickness_nm.items():
        if not isinstance(identifier, str) or v1._finite(value, "thickness") < 0:
            raise ValueError("invalid best physical thickness")
    if result.best_design is not None:
        result = replace(result, best_design=v1.validate_design(result.best_design))
    return result


def validate_film_ideal_sensitivity_result_payload(
    payload: Mapping[str, Any],
    *,
    expected_request: FilmIdealSensitivityV2Request | Mapping[str, Any] | None = None,
    replay: bool = True,
) -> dict[str, Any]:
    if type(replay) is not bool:
        raise ValueError("replay must be a boolean")
    result = film_ideal_sensitivity_result_from_payload(payload)
    request = result.frozen_request
    if expected_request is not None:
        expected_request = (
            film_ideal_sensitivity_request_from_payload(expected_request)
            if isinstance(expected_request, Mapping)
            else _validate(expected_request)
        )
        if film_ideal_sensitivity_request_to_payload(
            expected_request
        ) != film_ideal_sensitivity_request_to_payload(request):
            raise ValueError(
                "ideal sensitivity result request identity does not match expected_request"
            )
    if (
        result.design_revision != request.design_revision
        or result.cache_identity != v1._request_identity(request)
    ):
        raise ValueError(
            "ideal sensitivity result revision/cache identity is not bound to the request"
        )
    if (
        result.forward_evaluations != result.jacobian_evaluations
        or result.failed_evaluations > result.evaluations
        or result.evaluations < result.failed_evaluations
        or (
            request.max_evaluations is not None
            and result.forward_evaluations > request.max_evaluations
        )
    ):
        raise ValueError("ideal sensitivity work counters are inconsistent")
    if not result.is_final and (
        result.state != "partial" or result.termination_reason != "checkpoint"
    ):
        raise ValueError("nonfinal results must be partial checkpoints")
    if result.state == "completed" and result.termination_reason not in (
        ("analysis_complete",)
        if result.operation == "analyze"
        else ("slsqp_converged", "zero_sensitivity_objective", "no_improvement")
    ):
        raise ValueError("completed result has an invalid termination reason")
    if result.operation == "optimize" and result.state == "completed":
        if not any(v.lower_nm < v1._finite(v.upper_nm, "upper_nm") for v in request.variables):
            raise ValueError("completed optimization requires a free physical thickness variable")
        if (
            result.baseline_analysis is not None
            and result.baseline_analysis.objective_value > 0.0
            and (
                result.solver_objective_scale is None
                or result.resolved_max_iterations is None
                or not result.scipy_version
            )
        ):
            raise ValueError(
                "positive-objective optimization completion requires fixed F0 conditioning and solver metadata"
            )
    if result.solver_objective_scale is not None:
        scale = v1._finite(result.solver_objective_scale, "solver_objective_scale")
        if (
            scale <= 0
            or result.baseline_analysis is None
            or scale != result.baseline_analysis.objective_value
        ):
            raise ValueError("solver conditioning must use the fixed positive initial objective")
        v1._compare(result.baseline_solver_objective_value, 1.0, "baseline_solver_objective_value")
        if result.best_analysis is not None:
            v1._compare(
                result.best_solver_objective_value,
                result.best_analysis.objective_value / scale,
                "best_solver_objective_value",
            )
    elif (
        result.baseline_solver_objective_value is not None
        or result.best_solver_objective_value is not None
    ):
        raise ValueError("normalized solver objective requires its fixed scale")
    if result.termination_reason == "zero_sensitivity_objective" and (
        result.operation != "optimize"
        or result.baseline_analysis is None
        or result.baseline_analysis.objective_value != 0.0
        or result.best_analysis is None
        or result.best_analysis.objective_value != 0.0
    ):
        raise ValueError("zero objective stopping requires a complete exact-zero feasible baseline")
    allowed_reasons = {
        "completed": {
            "analysis_complete",
            "slsqp_converged",
            "zero_sensitivity_objective",
            "no_improvement",
        },
        "partial": {
            "checkpoint",
            "evaluation_limit",
            "solver_guard_rejected",
            "no_improvement",
            "iteration_limit",
            "slsqp_stopped",
        },
        "failed": {
            "initial_nominal_infeasible",
            "all_variables_fixed",
            "evaluation_limit",
            "fd_domain_insufficient",
            "numerical_failure",
            "scheduler_failed",
        },
        "cancelled": {"cancelled", "scheduler_cancel_won"},
    }
    if result.termination_reason not in allowed_reasons[result.state]:
        raise ValueError("result has an invalid termination reason for its state")
    if result.termination_reason == "all_variables_fixed" and (
        result.state != "failed"
        or any(v.lower_nm < v1._finite(v.upper_nm, "upper_nm") for v in request.variables)
    ):
        raise ValueError("all_variables_fixed must fail without free thickness variables")
    v1._validate_analysis_shape(result.baseline_analysis, request)
    v1._validate_analysis_shape(result.best_analysis, request)
    if (result.best_design is None) != (result.best_analysis is None):
        raise ValueError("best design and analysis must either both be present or both absent")
    if result.best_design is None and result.best_thickness_nm:
        raise ValueError("an empty result cannot claim physical thicknesses")
    if (
        result.best_analysis is not None
        and result.operation == "optimize"
        and (result.best_guard is None or not result.best_guard.guard_pass)
    ):
        raise ValueError("optimization incumbent must satisfy its exact full-grid policy guard")
    if (
        request.policy == "strict_nominal"
        and result.baseline_analysis is not None
        and result.operation == "optimize"
        and not result.baseline_analysis.nominal_targets_met
    ):
        if (
            result.state != "failed"
            or result.termination_reason != "initial_nominal_infeasible"
            or result.best_design is not None
        ):
            raise ValueError(
                "initial nominal-infeasible optimization must fail without a candidate"
            )
    if result.state == "completed" and (
        result.best_design is None or result.baseline_analysis is None
    ):
        raise ValueError("completed result requires baseline and best evidence")
    if result.best_analysis is not None and result.baseline_analysis is None:
        raise ValueError("a candidate requires a complete baseline")
    if (
        result.best_analysis is not None
        and result.baseline_analysis is not None
        and result.best_analysis.objective_value > result.baseline_analysis.objective_value
    ):
        raise ValueError("incumbent objective cannot be worse than its feasible baseline")
    if request.frozen_material_snapshot is None:
        raise ValueError("results require frozen material inputs")
    if request.kernel_identity is None:
        raise ValueError("results require a frozen physical kernel identity")
    if (result.baseline_analysis is None) != (result.baseline_evidence is None):
        raise ValueError("a complete baseline analysis and its locked evidence must coexist")
    if (result.baseline_analysis is None) != (result.baseline_guard is None):
        raise ValueError("a partial or absent baseline cannot claim a guard")
    if (result.best_analysis is None) != (result.best_guard is None):
        raise ValueError("candidate analysis and full-grid guard evidence must coexist")
    if result.baseline_analysis is not None:
        evidence = result.baseline_evidence
        assert evidence is not None
        if (
            evidence.request_identity != result.cache_identity
            or evidence.kernel_identity != request.kernel_identity
            or evidence.dtype != "<f8"
            or evidence.spectral_unit != "fraction"
            or evidence.digest != _evidence_digest(evidence)
        ):
            raise ValueError("baseline digest/identity/dtype/unit binding differs")
        # Derive the exact ordered condition identity from the frozen request,
        # not from self-reported evidence or a current editable design.
        conditions = tuple(
            (i, float(w), float(a), o)
            for i, o in enumerate(request.objectives)
            for a in o.angles_deg
            for w in np.linspace(o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples)
        )
        baseline_values = np.asarray(
            [row.actual for row in result.baseline_analysis.nominal_conditions]
        )
        synthetic = v1._Evaluation(
            result.baseline_analysis,
            baseline_values,
            np.empty((len(conditions), len(request.variables))),
            conditions,
        )
        expected_evidence = _baseline_evidence(request, synthetic)
        if evidence != expected_evidence:
            raise ValueError(
                "baseline evidence differs from complete original condition identity/values"
            )
        if result.baseline_guard != _guard(synthetic, request, evidence):
            raise ValueError("baseline guard differs from its exact locked evidence")
        if result.best_analysis is not None:
            synthetic_best = v1._Evaluation(
                result.best_analysis,
                np.asarray([row.actual for row in result.best_analysis.nominal_conditions]),
                synthetic.nominal_jacobian,
                conditions,
            )
            if result.best_guard != _guard(synthetic_best, request, evidence):
                raise ValueError("candidate guard differs from the immutable original baseline")
    if result.termination_reason == "no_improvement" and (
        result.operation != "optimize"
        or result.best_analysis is None
        or result.baseline_analysis is None
        or result.best_analysis.objective_value != result.baseline_analysis.objective_value
    ):
        raise ValueError("no_improvement must retain an equal-objective baseline incumbent")
    evaluator = v1._Evaluator(request, enforce_budget=False)
    if result.best_design is not None:
        if v1._optical_invariants(result.best_design) != v1._optical_invariants(
            request.design_snapshot
        ):
            raise ValueError("result changed fixed topology, material n/k policy or stable IDs")
        initial, physical = (
            evaluator.physical(request.design_snapshot),
            evaluator.physical(result.best_design),
        )
        v1._compare(result.best_thickness_nm, physical, "best_thickness_nm")
        variables = {v.layer_id: v for v in request.variables}
        for identifier, value in physical.items():
            if result.operation == "analyze" or identifier not in variables:
                if value != initial[identifier]:
                    raise ValueError("result changed an unselected thickness")
            elif not variables[identifier].lower_nm <= value <= variables[identifier].upper_nm:
                raise ValueError("result violates physical thickness bounds")
    if replay:
        if result.baseline_analysis is not None:
            replay_baseline = evaluator.evaluate(request.design_snapshot)
            if _baseline_evidence(request, replay_baseline) != result.baseline_evidence:
                raise ValueError(
                    "physical replay differs from the immutable full-grid baseline evidence"
                )
            v1._compare(
                asdict(result.baseline_analysis),
                asdict(replay_baseline.analysis),
                "baseline_analysis",
            )
        if result.best_design is not None:
            assert result.best_analysis is not None
            replay_best = evaluator.evaluate(result.best_design)
            if _guard(replay_best, request, result.baseline_evidence) != result.best_guard:
                raise ValueError("physical candidate replay differs from its exact full-grid guard")
            if (
                result.operation == "optimize"
                and replay_best.analysis.objective_value > replay_baseline.analysis.objective_value
            ):
                raise ValueError("physical incumbent objective is worse than its original baseline")
            if (
                result.operation == "optimize"
                and result.improved
                and not replay_best.analysis.objective_value
                < replay_baseline.analysis.objective_value
            ):
                raise ValueError("claimed improvement is not a strict physical objective decrease")
            v1._compare(asdict(result.best_analysis), asdict(replay_best.analysis), "best_analysis")
    return film_ideal_sensitivity_result_to_payload(result)
