"""Current ideal sensitivity requests, results, policies and strict schema2 I/O.

Numerical evaluation and the single current SLSQP executor live in the private
core. No historical sensitivity request format is accepted.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Mapping

import numpy as np

from .film_optimization import (FilmConditionError, FilmOptimizationObjective,
    FilmOptimizationRequest, FilmThicknessVariable)

SCHEMA_VERSION = 2
REQUEST_KIND = "coat_sim.film_ideal_sensitivity_request"
RESULT_KIND = "coat_sim.film_ideal_sensitivity_result"
POLICIES = ("strict_nominal", "bounded_baseline_degradation")

@dataclass(frozen=True)
class FilmIdealDegradationBudget:
    objective_id: str
    delta: float
    unit: str = "percentage_point"


@dataclass(frozen=True)
class FilmIdealSensitivityRequest:
    """Immutable fixed-topology request; max_evaluations counts raw condition blocks.

    sensitivity_layer_ids is independent of variables and freezes all front IDs
    when omitted. Absolute columns are J*1nm; relative columns are J*current d.
    solver_objective_ftol is a fixed numerical setting, never nominal tolerance.
    """

    design_snapshot: Mapping[str, Any]
    design_revision: int
    objectives: tuple[FilmOptimizationObjective, ...]
    variables: tuple[FilmThicknessVariable, ...]
    sensitivity_mode: str
    sensitivity_layer_ids: tuple[str, ...] | None = None
    max_iterations: int | None = None
    max_evaluations: int | None = None
    frozen_material_snapshot: Mapping[str, Any] | None = None
    strict_lower_bound_objective_indices: tuple[int, ...] = ()
    strict_upper_bound_objective_indices: tuple[int, ...] = ()
    solver_objective_ftol: float = 1e-10
    schema_version: int = SCHEMA_VERSION
    policy: str = "bounded_baseline_degradation"
    objective_ids: tuple[str, ...] = ()
    budgets: tuple[FilmIdealDegradationBudget, ...] = ()
    kernel_identity: str | None = None


@dataclass(frozen=True)
class FilmIdealSensitivityAnalysis:
    objective_value: float
    nominal_targets_met: bool
    nominal_max_normalized_error: float
    nominal_weighted_rms_error: float
    nominal_conditions: tuple[FilmConditionError, ...]


@dataclass(frozen=True)
class FilmIdealSensitivityProgress:
    stage: str
    state: str
    iterations: int
    evaluations: int
    forward_evaluations: int
    jacobian_evaluations: int
    probe_evaluations: int
    cache_hits: int
    best_objective_value: float | None = None
    message: str = ""


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
class FilmIdealSensitivityResult:
    """Feasible incumbent and solver termination are deliberately independent.

    evaluations counts cache-miss candidate/probe attempts; forward/jacobian
    count paired logical condition blocks, not canonical value-solver calls.
    Scalar probes retain the analytic r/t recurrence but omit the redundant
    coherent power solve. probe_evaluations counts FD probe attempts
    including cache hits; fd_gradient_evaluations counts complete gradients only.
    """

    state: str
    termination_reason: str
    design_revision: int
    best_design: Mapping[str, Any] | None
    best_thickness_nm: Mapping[str, float]
    frozen_request: FilmIdealSensitivityRequest
    baseline_analysis: FilmIdealSensitivityAnalysis | None
    best_analysis: FilmIdealSensitivityAnalysis | None
    iterations: int
    evaluations: int
    forward_evaluations: int
    jacobian_evaluations: int
    cache_identity: str
    probe_evaluations: int = 0
    cache_hits: int = 0
    failed_evaluations: int = 0
    fd_gradient_evaluations: int = 0
    resolved_max_iterations: int | None = None
    operation: str = "optimize"
    is_final: bool = True
    message: str = ""
    budget_unit: str = "raw_forward_jacobian_condition_blocks"
    scipy_version: str | None = None
    solver_objective_scale: float | None = None
    baseline_solver_objective_value: float | None = None
    best_solver_objective_value: float | None = None

    @property
    def candidate_design(self):
        return self.best_design

    @property
    def nominal_targets_met(self):
        return self.best_analysis is not None and self.best_analysis.nominal_targets_met

    @property
    def targets_met(self):
        return self.state == "completed" and self.nominal_targets_met

    @property
    def improved(self):
        return (
            self.best_analysis is not None
            and self.baseline_analysis is not None
            and self.best_analysis.objective_value < self.baseline_analysis.objective_value
        )


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


# Canonical records must exist before the private core imports their types.
from . import _film_ideal_sensitivity_core as _core  # noqa: E402


def _validate(request):
    if not isinstance(request, FilmIdealSensitivityRequest):
        raise ValueError("request must be a FilmIdealSensitivityRequest")
    if type(request.schema_version) is not int or request.schema_version != SCHEMA_VERSION:
        raise ValueError("ideal sensitivity requires schema_version 2")
    base = _core._validate_physical(request)
    if request.policy not in POLICIES:
        raise ValueError("unknown ideal sensitivity policy")
    ids = _core._sequence(request.objective_ids, "objective_ids")
    if (
        len(ids) != len(base.objectives)
        or any(not isinstance(i, str) or not i.strip() for i in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("objective_ids must uniquely identify every frozen objective row")
    budgets = _core._sequence(request.budgets, "budgets")
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
        delta = _core._finite(b.delta, "delta")
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
        design_snapshot=base.design_snapshot,
        objectives=base.objectives,
        variables=base.variables,
        sensitivity_layer_ids=base.sensitivity_layer_ids,
        strict_lower_bound_objective_indices=base.strict_lower_bound_objective_indices,
        strict_upper_bound_objective_indices=base.strict_upper_bound_objective_indices,
        objective_ids=ids,
        budgets=tuple(normalized),
    )
    if result.kernel_identity is not None and result.kernel_identity != _core._kernel_identity():
        raise ValueError("frozen physical kernel identity differs from the current implementation")
    return result


def ideal_sensitivity_request_from_optimization(
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
) -> FilmIdealSensitivityRequest:
    if not isinstance(base, FilmOptimizationRequest):
        raise ValueError("base must be a FilmOptimizationRequest")
    return _validate(
        FilmIdealSensitivityRequest(
            design_snapshot=deepcopy(base.design_snapshot),
            design_revision=base.design_revision,
            objectives=tuple(base.objectives),
            variables=tuple(base.variables),
            sensitivity_mode=sensitivity_mode,
            sensitivity_layer_ids=(None if sensitivity_layer_ids is None
                                   else tuple(sensitivity_layer_ids)),
            max_iterations=max_iterations,
            max_evaluations=max_evaluations,
            frozen_material_snapshot=deepcopy(base.frozen_material_snapshot),
            strict_lower_bound_objective_indices=tuple(strict_lower_bound_objective_indices),
            strict_upper_bound_objective_indices=tuple(strict_upper_bound_objective_indices),
            policy=policy,
            objective_ids=objective_ids,
            budgets=budgets,
        )
    )


def estimate_film_ideal_sensitivity_resources(request):
    request = _validate(request)
    base = _core._estimate_physical_resources(request)
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
    # remain additional to physical retained analysis/Jacobian estimates.
    constraint_cache = 8 * m * (base["free_variables"] + 1)
    baseline_bytes = 1024 * _core._point_count(request) + 1024 + 768 * len(request.objectives)
    base["estimated_constraint_cache_bytes"] = constraint_cache
    build_peak = 3 * constraint_cache + 192 * m
    base["estimated_constraint_cache_build_bytes"] = build_peak
    base["estimated_frozen_baseline_bytes"] = baseline_bytes
    base["estimated_working_bytes"] += constraint_cache + build_peak + baseline_bytes
    base["estimated_retained_result_bytes"] += baseline_bytes + len(repr(asdict(request))) * 4
    return base


def _check_resources(request):
    resources = estimate_film_ideal_sensitivity_resources(request)
    if resources["estimated_retained_result_bytes"] > _core.MAX_RETAINED_RESULT_BYTES:
        raise ValueError(
            "ideal sensitivity retained-result resource limit exceeded; no grid or columns were dropped"
        )
    if resources["estimated_working_bytes"] > _core.MAX_WORKING_BYTES:
        raise ValueError(
            "ideal sensitivity SLSQP/working resource limit exceeded; no grid or columns were dropped"
        )
    return resources


def _freeze_materials(service, request):
    request = _validate(request)
    _check_resources(request)
    base = _core._freeze_physical_inputs(service, request)
    return _validate(
        replace(
            request,
            design_snapshot=base.design_snapshot,
            frozen_material_snapshot=base.frozen_material_snapshot,
            kernel_identity=_core._kernel_identity(),
        )
    )


def freeze_film_ideal_sensitivity_request(service, request):
    """Freeze physical inputs only; y0 is locked by the run's first full F/J."""
    return _freeze_materials(service, request)


def execute_film_ideal_sensitivity_request(
    service: Any,
    request: FilmIdealSensitivityRequest,
    *,
    cancel_event=None,
    progress_callback=None,
    checkpoint_callback=None,
    candidate_observer=None,
) -> FilmIdealSensitivityResult:
    return _core._execute(
        service,
        request,
        operation="optimize",
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        checkpoint_callback=checkpoint_callback,
        candidate_observer=candidate_observer,
    )


def analyze_film_ideal_sensitivity(
    service: Any,
    request: FilmIdealSensitivityRequest,
    *,
    cancel_event=None,
    progress_callback=None,
    candidate_observer=None,
) -> FilmIdealSensitivityResult:
    return _core._execute(
        service,
        request,
        operation="analyze",
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        candidate_observer=candidate_observer,
    )


def _envelope(payload, cls, kind):
    payload = _core._json_native(payload)
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != kind
        or set(payload) - {f.name for f in fields(cls)} - {"kind"}
    ):
        raise ValueError(f"invalid {kind} schema/type or unknown fields")
    data = deepcopy(dict(payload))
    data.pop("kind")
    return data


def film_ideal_sensitivity_request_to_payload(request):
    return _core._json_native(dict(kind=REQUEST_KIND, **asdict(_validate(request))))


def film_ideal_sensitivity_request_from_payload(payload):
    data = _envelope(payload, FilmIdealSensitivityRequest, REQUEST_KIND)
    if not {"policy", "objective_ids", "budgets"} <= set(data):
        raise ValueError("request requires explicit policy, objective_ids and budgets")
    if not isinstance(data["budgets"], (tuple, list)) or any(
        not isinstance(row, Mapping) or not {"objective_id", "delta", "unit"} <= set(row)
        for row in data["budgets"]
    ):
        raise ValueError("every budget requires explicit objective_id, delta and unit")
    try:
        data["objectives"] = tuple(
            replace(
                _core._record(FilmOptimizationObjective, row, "objective"),
                angles_deg=tuple(row.get("angles_deg", (0.0,))),
            )
            for row in data["objectives"]
        )
        data["variables"] = tuple(
            _core._record(FilmThicknessVariable, row, "variable") for row in data["variables"]
        )
        data["budgets"] = tuple(
            _core._record(FilmIdealDegradationBudget, row, "budget") for row in data["budgets"]
        )
        return _validate(_core._record(FilmIdealSensitivityRequest, data, "request"))
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid ideal sensitivity request: {exc}") from exc


def _guard_from_payload(data):
    if data is None:
        return None
    result = _core._record(FilmIdealDegradationGuard, data, "degradation guard")
    if type(result.guard_pass) is not bool:
        raise ValueError("guard_pass must be a boolean")
    rows = tuple(_core._record(FilmIdealDegradationRow, row, "degradation row") for row in result.rows)
    return replace(result, rows=rows)


def _evidence_from_payload(data):
    if data is None:
        return None
    result = _core._record(FilmIdealSensitivityBaselineEvidence, data, "baseline evidence")
    conditions = tuple(
        _core._record(FilmIdealSensitivityBaselineCondition, row, "baseline condition")
        for row in result.conditions
    )
    values = tuple(_core._finite(x, "baseline value") for x in result.values)
    return replace(result, values=values, conditions=conditions)


def film_ideal_sensitivity_result_to_payload(result):
    if not isinstance(result, FilmIdealSensitivityResult):
        raise ValueError("result must be a FilmIdealSensitivityResult")
    payload = dict(kind=RESULT_KIND, **asdict(result))
    payload["frozen_request"] = film_ideal_sensitivity_request_to_payload(result.frozen_request)
    return _core._json_native(payload)


def film_ideal_sensitivity_result_from_payload(payload):
    data = _envelope(payload, FilmIdealSensitivityResult, RESULT_KIND)
    if "policy" not in data:
        raise ValueError("result requires an explicit policy")
    try:
        data["frozen_request"] = film_ideal_sensitivity_request_from_payload(data["frozen_request"])
        data["baseline_analysis"] = _core._analysis_from_payload(data["baseline_analysis"])
        data["best_analysis"] = _core._analysis_from_payload(data["best_analysis"])
        data["baseline_guard"] = _guard_from_payload(data["baseline_guard"])
        data["best_guard"] = _guard_from_payload(data["best_guard"])
        data["baseline_evidence"] = _evidence_from_payload(data["baseline_evidence"])
        result = _core._record(FilmIdealSensitivityResult, data, "result")
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid ideal sensitivity result: {exc}") from exc
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
        if not isinstance(identifier, str) or _core._finite(value, "thickness") < 0:
            raise ValueError("invalid best physical thickness")
    if result.best_design is not None:
        result = replace(result, best_design=_core.validate_design(result.best_design))
    return result


def validate_film_ideal_sensitivity_result_payload(
    payload: Mapping[str, Any],
    *,
    expected_request: FilmIdealSensitivityRequest | Mapping[str, Any] | None = None,
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
        or result.cache_identity != _core._request_identity(request)
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
        if not any(v.lower_nm < _core._finite(v.upper_nm, "upper_nm") for v in request.variables):
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
        scale = _core._finite(result.solver_objective_scale, "solver_objective_scale")
        if (
            scale <= 0
            or result.baseline_analysis is None
            or scale != result.baseline_analysis.objective_value
        ):
            raise ValueError("solver conditioning must use the fixed positive initial objective")
        _core._compare(result.baseline_solver_objective_value, 1.0, "baseline_solver_objective_value")
        if result.best_analysis is not None:
            _core._compare(
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
        or any(v.lower_nm < _core._finite(v.upper_nm, "upper_nm") for v in request.variables)
    ):
        raise ValueError("all_variables_fixed must fail without free thickness variables")
    _core._validate_analysis_shape(result.baseline_analysis, request)
    _core._validate_analysis_shape(result.best_analysis, request)
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
            or evidence.digest != _core._evidence_digest(evidence)
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
        synthetic = _core._Evaluation(
            result.baseline_analysis,
            baseline_values,
            np.empty((len(conditions), len(request.variables))),
            conditions,
        )
        expected_evidence = _core._baseline_evidence(request, synthetic)
        if evidence != expected_evidence:
            raise ValueError(
                "baseline evidence differs from complete original condition identity/values"
            )
        if result.baseline_guard != _core._guard(synthetic, request, evidence):
            raise ValueError("baseline guard differs from its exact locked evidence")
        if result.best_analysis is not None:
            synthetic_best = _core._Evaluation(
                result.best_analysis,
                np.asarray([row.actual for row in result.best_analysis.nominal_conditions]),
                synthetic.nominal_jacobian,
                conditions,
            )
            if result.best_guard != _core._guard(synthetic_best, request, evidence):
                raise ValueError("candidate guard differs from the immutable original baseline")
    if result.termination_reason == "no_improvement" and (
        result.operation != "optimize"
        or result.best_analysis is None
        or result.baseline_analysis is None
        or result.best_analysis.objective_value != result.baseline_analysis.objective_value
    ):
        raise ValueError("no_improvement must retain an equal-objective baseline incumbent")
    evaluator = _core._Evaluator(request, enforce_budget=False)
    if result.best_design is not None:
        if _core._optical_invariants(result.best_design) != _core._optical_invariants(
            request.design_snapshot
        ):
            raise ValueError("result changed fixed topology, material n/k policy or stable IDs")
        initial, physical = (
            evaluator.physical(request.design_snapshot),
            evaluator.physical(result.best_design),
        )
        _core._compare(result.best_thickness_nm, physical, "best_thickness_nm")
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
            if _core._baseline_evidence(request, replay_baseline) != result.baseline_evidence:
                raise ValueError(
                    "physical replay differs from the immutable full-grid baseline evidence"
                )
            _core._compare(
                asdict(result.baseline_analysis),
                asdict(replay_baseline.analysis),
                "baseline_analysis",
            )
        if result.best_design is not None:
            assert result.best_analysis is not None
            replay_best = evaluator.evaluate(result.best_design)
            if _core._guard(replay_best, request, result.baseline_evidence) != result.best_guard:
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
            _core._compare(asdict(result.best_analysis), asdict(replay_best.analysis), "best_analysis")
    return film_ideal_sensitivity_result_to_payload(result)
