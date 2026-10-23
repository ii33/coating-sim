"""Ideal local derivative sensitivity, independent of manufacturing error models.

Fixed topology, captured n/k and the complete original nominal grid are retained.
The objective is a weighted *mean square*, not a norm, probability or error bound.
SLSQP uses analytic first derivatives for nominal constraints and a separately
bounded three-point scalar finite difference for the derivative-based objective.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import inspect
import json
import math
from typing import Any, Mapping

import numpy as np

from .film_design import validate_design
from .film_nominal_acceptance import nominal_condition_pass
from .optical_constants import prepare_optical_set
from .film_optimization import (
    FilmConditionError,
    FilmOptimizationObjective,
    FilmOptimizationRequest,
    FilmThicknessVariable,
    _checked_frozen_snapshot,
    _wavelength_chunks,
)
from .film_optimization_derivatives import _PreparedMaterialData, simulate_with_thickness_jacobian
from .film_optimization_needle import _normalize_objectives, _physical_layer_thickness
from .film_optimization_scoring import score_condition, weighted_rms_error

SCHEMA_VERSION = 1
REQUEST_KIND = "coat_sim.film_ideal_sensitivity_request"
RESULT_KIND = "coat_sim.film_ideal_sensitivity_result"
MAX_RETAINED_RESULT_BYTES = 256 * 1024**2
MAX_WORKING_BYTES = 1024**3
MAX_CACHE_BYTES = 64 * 1024**2
BLOCK_SAMPLES = 4096


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
class FilmIdealSensitivityResult:
    """Feasible incumbent and solver termination are deliberately independent.

    evaluations counts cache-miss candidate attempts; forward/jacobian count
    paired raw condition blocks. probe_evaluations counts FD probe attempts
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


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise ValueError(f"{label} must be a finite number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return value


def _sequence(value, label):
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{label} must be a sequence")
    return tuple(value)


def _json_native(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("ideal sensitivity data must be finite JSON data") from exc


def _validate(request):
    if not isinstance(request, FilmIdealSensitivityRequest):
        raise ValueError("request must be a FilmIdealSensitivityRequest")
    if not isinstance(request.design_snapshot, Mapping):
        raise ValueError("design_snapshot must be a mapping")
    design = validate_design(deepcopy(dict(request.design_snapshot)))
    if (
        design["substrate"]["sides"] != "single"
        or design.get("first_surface", "front") != "front"
        or design["substrate"].get("mode") not in ("auto", "coherent")
    ):
        raise ValueError("ideal sensitivity supports only single-sided coherent front coatings")
    if type(request.design_revision) is not int or request.design_revision < 0:
        raise ValueError("design_revision must be a nonnegative integer")
    if (
        type(request.solver_objective_ftol) not in (int, float)
        or request.solver_objective_ftol != 1e-10
    ):
        raise ValueError(
            "solver_objective_ftol is fixed at 1e-10 for the normalized solver objective"
        )
    if request.sensitivity_mode not in ("absolute", "relative"):
        raise ValueError("sensitivity_mode must explicitly be absolute or relative")
    objectives = _normalize_objectives(_sequence(request.objectives, "objectives"))
    if not objectives:
        raise ValueError("at least one optical objective is required")
    layer_ids = tuple(row["id"] for row in design["front_layers"])
    sensitivity_ids = (
        layer_ids
        if request.sensitivity_layer_ids is None
        else _sequence(request.sensitivity_layer_ids, "sensitivity_layer_ids")
    )
    if any(
        not isinstance(identifier, str) or identifier not in layer_ids
        for identifier in sensitivity_ids
    ) or len(set(sensitivity_ids)) != len(sensitivity_ids):
        raise ValueError("sensitivity_layer_ids must uniquely select existing front stable IDs")
    if layer_ids and not sensitivity_ids:
        raise ValueError("sensitivity_layer_ids must select at least one front layer")
    variables = _sequence(request.variables, "variables")
    seen = set()
    for variable in variables:
        if not isinstance(variable, FilmThicknessVariable):
            raise ValueError("variables must contain FilmThicknessVariable values")
        if (
            variable.side != "front"
            or variable.layer_id not in layer_ids
            or variable.layer_id in seen
        ):
            raise ValueError("variables must uniquely select existing front layer IDs")
        seen.add(variable.layer_id)
        low, high = _finite(variable.lower_nm, "lower_nm"), _finite(variable.upper_nm, "upper_nm")
        if low < 0 or high < low or not math.isfinite(high - low):
            raise ValueError("bounds must be finite nonnegative ordered nm values with finite span")
    for label in ("max_iterations", "max_evaluations"):
        value = getattr(request, label)
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(f"{label} must be a positive integer or null")
    flags = {}
    for side in ("lower", "upper"):
        label = f"strict_{side}_bound_objective_indices"
        indices = _sequence(getattr(request, label), label)
        if (
            any(type(i) is not int or not 0 <= i < len(objectives) for i in indices)
            or len(set(indices)) != len(indices)
            or any(
                objectives[i].mode != "range" or getattr(objectives[i], side + "_bound") is None
                for i in indices
            )
        ):
            raise ValueError(f"{label} must uniquely select range {side} bounds")
        flags[label] = tuple(sorted(indices))
    if request.frozen_material_snapshot is not None:
        _checked_frozen_snapshot(request.frozen_material_snapshot, design)
    return replace(
        request,
        design_snapshot=design,
        objectives=objectives,
        variables=variables,
        sensitivity_layer_ids=tuple(sensitivity_ids),
        strict_lower_bound_objective_indices=flags["strict_lower_bound_objective_indices"],
        strict_upper_bound_objective_indices=flags["strict_upper_bound_objective_indices"],
    )


def ideal_sensitivity_request_from_optimization(
    base: FilmOptimizationRequest,
    *,
    sensitivity_mode: str,
    sensitivity_layer_ids: tuple[str, ...] | None = None,
    max_iterations: int | None = None,
    max_evaluations: int | None = None,
    strict_lower_bound_objective_indices: tuple[int, ...] = (),
    strict_upper_bound_objective_indices: tuple[int, ...] = (),
) -> FilmIdealSensitivityRequest:
    """Capture only physical inputs; nominal solver/error-model options do not leak in."""
    if not isinstance(base, FilmOptimizationRequest):
        raise ValueError("base must be a FilmOptimizationRequest")
    return _validate(
        FilmIdealSensitivityRequest(
            deepcopy(base.design_snapshot),
            base.design_revision,
            tuple(base.objectives),
            tuple(base.variables),
            sensitivity_mode,
            None if sensitivity_layer_ids is None else tuple(sensitivity_layer_ids),
            max_iterations,
            max_evaluations,
            deepcopy(base.frozen_material_snapshot),
            tuple(strict_lower_bound_objective_indices),
            tuple(strict_upper_bound_objective_indices),
        )
    )


def estimate_film_ideal_sensitivity_resources(
    request: FilmIdealSensitivityRequest,
) -> dict[str, int]:
    """Conservative independent retained/Jacobian/SLSQP memory estimates, no Hessian tensor."""
    request = _validate(request)
    points = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
    blocks = sum(
        ((o.wavelength_samples + BLOCK_SAMPLES - 1) // BLOCK_SAMPLES) * len(o.angles_deg)
        for o in request.objectives
    )
    assert request.sensitivity_layer_ids is not None
    sensitivity = len(request.sensitivity_layer_ids)
    columns = len(set(request.sensitivity_layer_ids) | {v.layer_id for v in request.variables})
    free = sum(v.lower_nm < _finite(v.upper_nm, "upper_nm") for v in request.variables)
    eq = sum(
        o.wavelength_samples * len(o.angles_deg) for o in request.objectives if o.mode == "value"
    )
    iq = sum(
        o.wavelength_samples
        * len(o.angles_deg)
        * (int(o.lower_bound is not None) + int(o.upper_bound is not None))
        for o in request.objectives
        if o.mode == "range"
    )
    m, n = eq + iq, free
    # Include current SciPy's double workspace, normals/multipliers and a
    # conservative bound on historical SLSQP workspaces. This is optimizer
    # workspace, not a fabricated physical P x K x V Hessian.
    modern = max(
        1,
        n * (n + 1) // 2
        + 3 * m * n
        - (m + 5 * n + 7) * eq
        + 9 * m
        + 8 * n * n
        + 35 * n
        + eq * eq
        + 28
        + (2 * n * (n + 1) if iq == 0 else 0),
    )
    legacy = (
        (3 * (n + 1) + m) * (n + 2)
        + (n + 1 - eq + 1) * (max(0, iq) + 2 * n + 2)
        + 2 * m
        + 12 * n
        + 32
    )
    solver_bytes = 8 * (max(modern, legacy, 1) + max(1, m) * n + 3 * max(1, m + 2 * n + 2))
    analysis_bytes = points * 768 + 512
    jacobian_bytes = 8 * points * (columns + 2)
    cache_entry = analysis_bytes + jacobian_bytes
    block_points = max(min(BLOCK_SAMPLES, o.wavelength_samples) for o in request.objectives)
    # The recurrence stores transfer states for all fixed physical layers, not
    # only selected derivative columns. Account for those complex arrays and
    # the run-owned prepared optical-series cache independently of full J.
    forward_workspace = block_points * (
        256 * (len(request.design_snapshot["front_layers"]) + 2) + 256 * (columns + 1) + 1024
    )
    prepared_optical_cache = 32 * 1024**2
    working_bytes = (
        solver_bytes
        + forward_workspace
        + prepared_optical_cache
        + min(MAX_CACHE_BYTES, 32 * cache_entry)
        + 3 * cache_entry
    )
    return dict(
        condition_points=points,
        condition_blocks_per_evaluation=blocks,
        sensitivity_columns=sensitivity,
        derivative_columns=columns,
        free_variables=free,
        equality_constraints=eq,
        inequality_constraints=iq,
        estimated_analysis_bytes=analysis_bytes,
        estimated_retained_result_bytes=2 * analysis_bytes
        + len(_json_native(asdict(request)).__repr__()) * 4,
        estimated_jacobian_bytes=jacobian_bytes,
        estimated_forward_workspace_bytes=forward_workspace,
        estimated_prepared_optical_cache_bytes=prepared_optical_cache,
        estimated_slsqp_workspace_bytes=solver_bytes,
        estimated_working_bytes=working_bytes,
        objective_probes_per_gradient=2 * free,
        raw_blocks_per_gradient=2 * free * blocks,
    )


def _check_resources(request):
    resources = estimate_film_ideal_sensitivity_resources(request)
    if resources["estimated_retained_result_bytes"] > MAX_RETAINED_RESULT_BYTES:
        raise ValueError(
            "ideal sensitivity retained-result resource limit exceeded; no grid or columns were dropped"
        )
    if resources["estimated_working_bytes"] > MAX_WORKING_BYTES:
        raise ValueError(
            "ideal sensitivity SLSQP/working resource limit exceeded; no grid or columns were dropped"
        )
    return resources


def _check_frozen_material_domain(request):
    """Require captured source n/k over every original sample, never infer k=0.

    Explicit use_k=False remains a user-authored idealization. The independent
    source sampler validates the legacy kernel's complete domain before that
    kernel can enter its source-only-zero-default calculation convention.
    """
    frozen = request.frozen_material_snapshot
    if frozen is None:
        raise ValueError("complete frozen materials are required")
    records = {row["id"]: row for row in frozen["materials"]}
    references = [*request.design_snapshot["front_layers"], request.design_snapshot["substrate"]]
    incident = request.design_snapshot["incident_medium"]
    if incident.get("kind") == "material":
        references.append(incident)
    samplers = {}
    for reference in references:
        key = (reference["material_id"], reference["optical_set_id"])
        if key in samplers:
            continue
        material = records.get(key[0], {})
        optical = next(
            (row for row in material.get("optical_sets", []) if row["id"] == key[1]), None
        )
        if optical is None:
            raise ValueError(f"missing frozen optical source {key[0]}/{key[1]}")
        samplers[key] = prepare_optical_set(optical, k_sampling_policy="source")
    use_k = request.design_snapshot["use_k"]
    for objective in request.objectives:
        waves = np.linspace(
            objective.wavelength_min_nm, objective.wavelength_max_nm, objective.wavelength_samples
        )
        for key, sampler in samplers.items():
            try:
                series = sampler.evaluate_many(
                    waves, "nm", extrapolation_policy="error", include_k=use_k
                )
                if series is not None and series[0] is not None:
                    continue
                for wave in waves:
                    _n, k, _details = sampler(
                        float(wave), "nm", extrapolation_policy="error", include_k=use_k
                    )
                    if use_k and k is None:
                        raise ValueError("missing calculable source k; no k=0 assumption was made")
            except (ValueError, ArithmeticError) as exc:
                raise ValueError(
                    f"frozen optical source {key[0]}/{key[1]} cannot cover original "
                    f"{objective.wavelength_min_nm:g}-{objective.wavelength_max_nm:g} nm band: {exc}"
                ) from exc


def freeze_film_ideal_sensitivity_request(
    service: Any, request: FilmIdealSensitivityRequest
) -> FilmIdealSensitivityRequest:
    request = _validate(request)
    _check_resources(request)
    design = request.design_snapshot
    if request.frozen_material_snapshot is None:
        if service is None or not callable(getattr(service, "freeze_design", None)):
            raise ValueError("a film service is required to freeze materials")
        first = request.objectives[0]
        payload = service.freeze_design(
            design,
            dict(
                wavelength_min_nm=first.wavelength_min_nm,
                wavelength_max_nm=first.wavelength_max_nm,
                samples=first.wavelength_samples,
                angle_deg=first.angles_deg[0],
                polarization=first.polarization,
                first_surface="front",
            ),
        )
    else:
        payload = request.frozen_material_snapshot
    payload = _checked_frozen_snapshot(payload, design)
    result = _validate(
        replace(
            request,
            design_snapshot=deepcopy(payload["design"]),
            frozen_material_snapshot=deepcopy(payload),
        )
    )
    _check_frozen_material_domain(result)
    return result


class _Cancelled(Exception):
    pass


class _Budget(Exception):
    pass


class _FDDomainInsufficient(ValueError):
    pass


@dataclass
class _Evaluation:
    analysis: FilmIdealSensitivityAnalysis
    values: np.ndarray
    nominal_jacobian: np.ndarray
    conditions: tuple


def _request_identity(request):
    return hashlib.sha256(
        json.dumps(
            film_ideal_sensitivity_request_to_payload(request),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class _Evaluator:
    def __init__(self, request, cancel_event=None, *, enforce_budget=True):
        _check_resources(request)
        _check_frozen_material_domain(request)
        self.request, self.cancel = request, cancel_event
        self.enforce_budget = enforce_budget
        self.identity = _request_identity(request)
        self.snapshot = request.frozen_material_snapshot
        self.materials = _PreparedMaterialData(self.snapshot, request.design_snapshot)
        self.columns = tuple(
            dict.fromkeys(
                request.sensitivity_layer_ids + tuple(v.layer_id for v in request.variables)
            )
        )
        self.derivative_variables = tuple(
            FilmThicknessVariable("front", i, 0.0, 0.0) for i in self.columns
        )
        self.sensitivity_columns = [self.columns.index(i) for i in request.sensitivity_layer_ids]
        self.variable_columns = [self.columns.index(v.layer_id) for v in request.variables]
        self.cache = OrderedDict()
        self.entry_bytes = (
            estimate_film_ideal_sensitivity_resources(request)["estimated_analysis_bytes"]
            + estimate_film_ideal_sensitivity_resources(request)["estimated_jacobian_bytes"]
        )
        self.evaluations = self.forward = self.jacobian = self.probes = self.cache_hits = (
            self.failures
        ) = 0
        self.gradients = 0

    def check(self, *, block=False):
        if self.cancel is not None and self.cancel.is_set():
            raise _Cancelled()
        if (
            block
            and self.enforce_budget
            and self.request.max_evaluations is not None
            and self.forward >= self.request.max_evaluations
        ):
            raise _Budget()

    def physical(self, design):
        return {
            row["id"]: _physical_layer_thickness(row, design, self.snapshot)
            for row in design["front_layers"]
        }

    def evaluate(self, design, *, probe=False):
        self.check()
        physical = self.physical(design)
        if any(not math.isfinite(value) or value < 0 for value in physical.values()):
            raise ValueError("physical thicknesses must be finite and nonnegative")
        if any(
            not v.lower_nm <= physical[v.layer_id] <= v.upper_nm for v in self.request.variables
        ):
            raise ValueError("candidate violates explicit physical thickness bounds")
        key = (
            self.identity,
            np.asarray(
                [physical[row["id"]] for row in design["front_layers"]], dtype="<f8"
            ).tobytes(),
        )
        if probe:
            self.probes += 1
        if key in self.cache:
            self.cache_hits += 1
            self.cache.move_to_end(key)
            return self.cache[key]
        self.evaluations += 1
        values, jacobians = [], []
        condition_rows: list[tuple[int, float, float, FilmOptimizationObjective]] = []
        try:
            for i, objective in enumerate(self.request.objectives):
                waves = np.linspace(
                    objective.wavelength_min_nm,
                    objective.wavelength_max_nm,
                    objective.wavelength_samples,
                )
                for angle in objective.angles_deg:
                    for chunk in _wavelength_chunks(waves, BLOCK_SAMPLES):
                        self.check(block=True)
                        self.forward += 1
                        self.jacobian += 1
                        response = simulate_with_thickness_jacobian(
                            design,
                            self.materials,
                            self.derivative_variables,
                            dict(
                                wavelength_min_nm=float(chunk[0]),
                                wavelength_max_nm=float(chunk[-1]),
                                samples=len(chunk),
                                angle_deg=angle,
                                polarization=objective.polarization,
                                first_surface="front",
                            ),
                            wavelengths_nm=chunk,
                        )
                        self.check()
                        returned_waves = np.asarray(response.get("wavelength_nm"), dtype=np.float64)
                        if (
                            returned_waves.shape != chunk.shape
                            or returned_waves.tobytes()
                            != np.asarray(chunk, dtype=np.float64).tobytes()
                        ):
                            raise ValueError(
                                "physical forward changed the exact original wavelength grid"
                            )
                        actual = np.asarray(response[objective.metric], dtype=float)
                        jac = np.asarray(response["jacobian_" + objective.metric], dtype=float)
                        if (
                            actual.shape != chunk.shape
                            or jac.shape != (len(chunk), len(self.columns))
                            or not np.all(np.isfinite(actual))
                            or not np.all(np.isfinite(jac))
                        ):
                            raise ValueError("physical forward returned invalid response/Jacobian")
                        values.append(actual)
                        jacobians.append(jac)
                        condition_rows.extend((i, float(w), float(angle), objective) for w in chunk)
            nominal, jac = np.concatenate(values), np.concatenate(jacobians)
            conditions = tuple(condition_rows)
            scales = np.asarray([c[3].normalization_scale for c in conditions], dtype=np.longdouble)
            weights = np.asarray([c[3].weight for c in conditions], dtype=np.longdouble)
            # Normalize weights before multiplication to avoid overflowing sum(w).
            weights /= np.max(weights)
            weights /= np.sum(weights)
            amplitude = (
                np.ones(len(self.sensitivity_columns))
                if self.request.sensitivity_mode == "absolute"
                else np.asarray([physical[i] for i in self.request.sensitivity_layer_ids])
            )
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                normalized = (
                    np.asarray(jac[:, self.sensitivity_columns], dtype=np.longdouble)
                    * amplitude
                    / scales[:, None]
                )
                extended_value = np.sum(weights * np.sum(normalized * normalized, axis=1))
                objective_value = float(extended_value)
            if objective_value == 0.0 and np.any(normalized != 0.0):
                raise ValueError(
                    "ideal sensitivity objective underflows; a nonzero derivative cannot be claimed as zero"
                )
            if not math.isfinite(objective_value):
                raise ValueError("ideal sensitivity mean-square objective is not finite")
            errors = []
            for actual, (i, wave, angle, objective) in zip(nominal, conditions, strict=True):
                violation = (
                    abs(float(actual) - _finite(objective.target_value, "target_value"))
                    if objective.mode == "value"
                    else max(
                        0.0,
                        0.0
                        if objective.lower_bound is None
                        else objective.lower_bound - float(actual),
                        0.0
                        if objective.upper_bound is None
                        else float(actual) - objective.upper_bound,
                    )
                )
                normal, weighted = score_condition(
                    violation, objective.normalization_scale, objective.weight
                )
                errors.append(
                    FilmConditionError(
                        i,
                        wave,
                        angle,
                        objective.polarization,
                        objective.metric,
                        float(actual),
                        objective.target_value,
                        objective.lower_bound,
                        objective.upper_bound,
                        normal,
                        weighted,
                    )
                )
            analysis = FilmIdealSensitivityAnalysis(
                objective_value,
                all(
                    nominal_condition_pass(
                        o,
                        float(actual),
                        strict_lower=i in self.request.strict_lower_bound_objective_indices,
                        strict_upper=i in self.request.strict_upper_bound_objective_indices,
                    )
                    for actual, (i, _w, _a, o) in zip(nominal, conditions, strict=True)
                ),
                max(row.normalized_violation for row in errors),
                weighted_rms_error(
                    [row.normalized_violation for row in errors], [c[3].weight for c in conditions]
                ),
                tuple(errors),
            )
            result = _Evaluation(analysis, nominal, jac[:, self.variable_columns], conditions)
            self.check()
        except (ValueError, ArithmeticError, FloatingPointError):
            self.failures += 1
            raise
        if self.entry_bytes <= MAX_CACHE_BYTES:
            self.cache[key] = result
            while len(self.cache) > 32 or len(self.cache) * self.entry_bytes > MAX_CACHE_BYTES:
                self.cache.popitem(last=False)
        return result


def _bounded_stencil(value, lower, upper, *, step_scale=1.0):
    """Return two legal distinct probes; use actual float offsets in the weights."""
    value, lower, upper = float(value), float(lower), float(upper)
    if not (math.isfinite(value) and 0 <= lower <= value <= upper and math.isfinite(upper)):
        raise _FDDomainInsufficient(
            "fd_domain_insufficient: invalid physical finite-difference domain"
        )
    hbase = np.cbrt(np.finfo(float).eps) * max(1.0, abs(value)) * step_scale
    left, right = value - lower, upper - value
    h = min(hbase, left, right)
    lo, hi = value - h, value + h
    if lower <= lo < value < hi <= upper:
        return (lo, hi)
    # At/near a non-resolvable central boundary use a two-probe second-order
    # one-sided stencil. Never cross a bound or alias either probe to its base.
    directions = (1.0, -1.0) if right >= left else (-1.0, 1.0)
    for direction in directions:
        room = right if direction > 0 else left
        h = min(hbase, room / 2.0)
        first, second = value + direction * h, value + direction * (2.0 * h)
        if (
            lower <= first <= upper
            and lower <= second <= upper
            and first != value
            and second != value
            and first != second
        ):
            return first, second
    raise _FDDomainInsufficient(
        "fd_domain_insufficient: cannot fit two distinct legal physical probes"
    )


def _three_point_derivative(f0, f1, f2, x0, x1, x2):
    """Lagrange three-point derivative at x0 with true representable offsets."""
    a, b = x1 - x0, x2 - x0
    if a == 0 or b == 0 or a == b:
        raise _FDDomainInsufficient(
            "fd_domain_insufficient: repeated finite-difference coordinates"
        )
    # Difference form cancels a constant exactly and avoids the large f0 weight.
    derivative = (b * ((f1 - f0) / a) - a * ((f2 - f0) / b)) / (b - a)
    if not math.isfinite(derivative):
        raise ValueError("finite-difference objective gradient is nonfinite")
    return derivative


def _constraint_rows(evaluation, request, free_indices, spans):
    inequalities, iq_jac, equalities, eq_jac = [], [], [], []
    for actual, derivative, (i, _wave, _angle, objective) in zip(
        evaluation.values,
        evaluation.nominal_jacobian[:, free_indices],
        evaluation.conditions,
        strict=True,
    ):
        derivative = derivative * spans / objective.normalization_scale
        if objective.mode == "value":
            equalities.append((actual - objective.target_value) / objective.normalization_scale)
            eq_jac.append(derivative)
        else:
            if objective.lower_bound is not None:
                lower = (
                    np.nextafter(objective.lower_bound, np.inf)
                    if i in request.strict_lower_bound_objective_indices
                    else objective.lower_bound
                )
                inequalities.append((actual - lower) / objective.normalization_scale)
                iq_jac.append(derivative)
            if objective.upper_bound is not None:
                upper = (
                    np.nextafter(objective.upper_bound, -np.inf)
                    if i in request.strict_upper_bound_objective_indices
                    else objective.upper_bound
                )
                inequalities.append((upper - actual) / objective.normalization_scale)
                iq_jac.append(-derivative)
    n = len(free_indices)
    return (
        np.asarray(inequalities),
        np.asarray(iq_jac).reshape(-1, n),
        np.asarray(equalities),
        np.asarray(eq_jac).reshape(-1, n),
    )


def _candidate_design(design, variables, values):
    result = deepcopy(design)
    selected = {v.layer_id: float(value) for v, value in zip(variables, values, strict=True)}
    for row in result["front_layers"]:
        if row["id"] in selected:
            row["thickness"] = dict(value=selected[row["id"]], unit="nm")
    if isinstance(result.get("expression"), dict):
        result["expression"]["in_sync"] = False
    return result


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
    request = freeze_film_ideal_sensitivity_request(service, request)
    evaluator = _Evaluator(request, cancel_event)
    initial_design = deepcopy(request.design_snapshot)
    physical = evaluator.physical(initial_design)
    if any(not v.lower_nm <= physical[v.layer_id] <= v.upper_nm for v in request.variables):
        raise ValueError("initial physical thickness is outside explicit variable bounds")
    baseline = best = best_design = None
    iterations = 0
    resolved_maxiter = None
    scipy_version = None
    solver_scale = None

    def result(state, reason, message="", *, final=True):
        return FilmIdealSensitivityResult(
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
        )

    def emit(stage, state="running", message=""):
        if progress_callback is not None:
            progress_callback(
                FilmIdealSensitivityProgress(
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
        nonlocal best, best_design
        analysis = evaluation.analysis
        if analysis.nominal_targets_met and (
            best is None or analysis.objective_value < best.objective_value
        ):
            best, best_design = analysis, deepcopy(design)
            if checkpoint_callback is not None:
                checkpoint_callback(
                    film_ideal_sensitivity_result_to_payload(
                        result("partial", "checkpoint", final=False)
                    )
                )

    try:
        emit("baseline")
        base_evaluation = evaluator.evaluate(initial_design)
        baseline = base_evaluation.analysis
        if operation == "analyze":
            # An analysis is not an optimization incumbent and may explicitly
            # describe a nominal-infeasible design.
            best, best_design = baseline, initial_design
            return result("completed", "analysis_complete")
        if not baseline.nominal_targets_met:
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
            design = _candidate_design(initial_design, variables, values)
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
                    probe = _candidate_design(design, variables, changed)
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
                    _three_point_derivative(normalized_center, probes[0], probes[1], value, x1, x2)
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
            return _constraint_rows(data, request, free_indices, spans)[
                (2 if equality else 0) + int(derivative)
            ]

        first_rows = _constraint_rows(base_evaluation, request, free_indices, spans)
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
        if not final_data.analysis.nominal_targets_met:
            return result(
                "partial",
                "solver_nominal_rejected",
                "Final trial fails exact nominal bounds; the last complete feasible incumbent is retained.",
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
    except _Cancelled:
        return result(
            "cancelled",
            "cancelled",
            "Cancelled; only a complete nominal-feasible incumbent is retained.",
        )
    except _Budget:
        return result(
            "partial" if best is not None else "failed",
            "evaluation_limit",
            "Raw forward/Jacobian condition-block budget exhausted; no partial analysis or gradient is retained.",
        )
    except _FDDomainInsufficient as exc:
        return result("failed", "fd_domain_insufficient", str(exc))
    except (ValueError, ArithmeticError, FloatingPointError) as exc:
        return result("failed", "numerical_failure", str(exc))


def execute_film_ideal_sensitivity_request(
    service: Any,
    request: FilmIdealSensitivityRequest,
    *,
    cancel_event=None,
    progress_callback=None,
    checkpoint_callback=None,
) -> FilmIdealSensitivityResult:
    return _execute(
        service,
        request,
        operation="optimize",
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        checkpoint_callback=checkpoint_callback,
    )


def analyze_film_ideal_sensitivity(
    service: Any, request: FilmIdealSensitivityRequest, *, cancel_event=None, progress_callback=None
) -> FilmIdealSensitivityResult:
    return _execute(
        service,
        request,
        operation="analyze",
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )


def _record(cls, data, label):
    if not isinstance(data, Mapping) or set(data) - {f.name for f in fields(cls)}:
        raise ValueError(f"{label} has unknown fields or is not an object")
    try:
        return cls(**dict(data))
    except TypeError as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc


def _envelope(payload, cls, kind):
    payload = _json_native(payload)
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != kind
        or set(payload) - {f.name for f in fields(cls)} - {"schema_version", "kind"}
    ):
        raise ValueError(f"invalid {kind} schema/type or unknown fields")
    data = deepcopy(dict(payload))
    data.pop("schema_version")
    data.pop("kind")
    return data


def film_ideal_sensitivity_request_to_payload(
    request: FilmIdealSensitivityRequest,
) -> dict[str, Any]:
    return _json_native(
        dict(schema_version=SCHEMA_VERSION, kind=REQUEST_KIND, **asdict(_validate(request)))
    )


def film_ideal_sensitivity_request_from_payload(
    payload: Mapping[str, Any],
) -> FilmIdealSensitivityRequest:
    data = _envelope(payload, FilmIdealSensitivityRequest, REQUEST_KIND)
    try:
        data["objectives"] = tuple(
            replace(
                _record(FilmOptimizationObjective, row, "objective"),
                angles_deg=tuple(row.get("angles_deg", (0.0,))),
            )
            for row in data["objectives"]
        )
        data["variables"] = tuple(
            _record(FilmThicknessVariable, row, "variable") for row in data["variables"]
        )
        if data.get("sensitivity_layer_ids") is not None:
            data["sensitivity_layer_ids"] = tuple(data["sensitivity_layer_ids"])
        return _validate(_record(FilmIdealSensitivityRequest, data, "request"))
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid ideal sensitivity request: {exc}") from exc


def _analysis_from_payload(data):
    if data is None:
        return None
    result = _record(FilmIdealSensitivityAnalysis, data, "analysis")
    conditions = tuple(
        _record(FilmConditionError, row, "nominal condition") for row in result.nominal_conditions
    )
    if type(result.nominal_targets_met) is not bool:
        raise ValueError("nominal_targets_met must be a boolean")
    for name in ("objective_value", "nominal_max_normalized_error", "nominal_weighted_rms_error"):
        if _finite(getattr(result, name), name) < 0:
            raise ValueError(f"{name} must be nonnegative")
    return replace(result, nominal_conditions=conditions)


def film_ideal_sensitivity_result_to_payload(result: FilmIdealSensitivityResult) -> dict[str, Any]:
    if not isinstance(result, FilmIdealSensitivityResult):
        raise ValueError("result must be a FilmIdealSensitivityResult")
    payload = dict(schema_version=SCHEMA_VERSION, kind=RESULT_KIND, **asdict(result))
    payload["frozen_request"] = film_ideal_sensitivity_request_to_payload(result.frozen_request)
    return _json_native(payload)


def film_ideal_sensitivity_result_from_payload(
    payload: Mapping[str, Any],
) -> FilmIdealSensitivityResult:
    data = _envelope(payload, FilmIdealSensitivityResult, RESULT_KIND)
    try:
        data["frozen_request"] = film_ideal_sensitivity_request_from_payload(data["frozen_request"])
        data["baseline_analysis"] = _analysis_from_payload(data["baseline_analysis"])
        data["best_analysis"] = _analysis_from_payload(data["best_analysis"])
        result = _record(FilmIdealSensitivityResult, data, "result")
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid ideal sensitivity result: {exc}") from exc
    if result.state not in ("completed", "partial", "failed", "cancelled"):
        raise ValueError("invalid ideal sensitivity result state")
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
        if not isinstance(identifier, str) or _finite(value, "thickness") < 0:
            raise ValueError("invalid best physical thickness")
    if result.best_design is not None:
        result = replace(result, best_design=validate_design(result.best_design))
    return result


def _optical_invariants(design):
    result = deepcopy(dict(design))
    for row in result["front_layers"]:
        row.pop("thickness")
    if isinstance(result.get("expression"), dict):
        result["expression"].pop("in_sync", None)
    return result


def _compare(expected, actual, path="result"):
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        if set(expected) != set(actual):
            raise ValueError(f"{path} replay fields differ")
        for key in expected:
            _compare(expected[key], actual[key], path + "." + key)
    elif isinstance(expected, (tuple, list)) and isinstance(actual, (tuple, list)):
        if len(expected) != len(actual):
            raise ValueError(f"{path} replay dimensions differ")
        for i, (a, b) in enumerate(zip(expected, actual, strict=True)):
            _compare(a, b, path + f"[{i}]")
    elif isinstance(expected, bool) or isinstance(actual, bool):
        if type(expected) is not type(actual) or expected != actual:
            raise ValueError(f"{path} replay flag differs")
    elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        # Squared derivative objectives can be far below 1e-12. An optical
        # response absolute tolerance must not allow orders-of-magnitude score
        # forgery merely because both scores are small.
        absolute_tolerance = 0.0 if path.endswith("objective_value") else 1e-12
        if not math.isclose(
            _finite(expected, path), _finite(actual, path), rel_tol=1e-9, abs_tol=absolute_tolerance
        ):
            raise ValueError(f"{path} physical replay differs")
    elif expected != actual:
        raise ValueError(f"{path} replay value differs")


def _validate_analysis_shape(analysis, request):
    if analysis is None:
        return
    expected = []
    for i, o in enumerate(request.objectives):
        for angle in o.angles_deg:
            for wave in np.linspace(o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples):
                expected.append((i, float(wave), float(angle), o))
    if len(analysis.nominal_conditions) != len(expected):
        raise ValueError("analysis must retain every original nominal grid condition")
    normals, weights = [], []
    for row, (i, wave, angle, o) in zip(analysis.nominal_conditions, expected, strict=True):
        if (
            type(row.objective_index) is not int
            or row.objective_index != i
            or row.wavelength_nm != wave
            or row.angle_deg != angle
            or any(
                getattr(row, key) != getattr(o, key)
                for key in ("polarization", "metric", "target_value", "lower_bound", "upper_bound")
            )
        ):
            raise ValueError("nominal condition identity/grid/target is not bound to the request")
        actual = _finite(row.actual, "actual")
        violation = (
            abs(actual - o.target_value)
            if o.mode == "value"
            else max(
                0.0,
                0.0 if o.lower_bound is None else o.lower_bound - actual,
                0.0 if o.upper_bound is None else actual - o.upper_bound,
            )
        )
        normal, weighted = score_condition(violation, o.normalization_scale, o.weight)
        _compare(row.normalized_violation, normal, "normalized_violation")
        _compare(row.weighted_normalized_violation, weighted, "weighted_normalized_violation")
        normals.append(normal)
        weights.append(o.weight)
    expected_met = all(
        nominal_condition_pass(
            o,
            row.actual,
            strict_lower=i in request.strict_lower_bound_objective_indices,
            strict_upper=i in request.strict_upper_bound_objective_indices,
        )
        for row, (i, _w, _a, o) in zip(analysis.nominal_conditions, expected, strict=True)
    )
    if analysis.nominal_targets_met is not expected_met:
        raise ValueError("nominal acceptance flag differs from exact original checker")
    _compare(analysis.nominal_max_normalized_error, max(normals), "nominal_max_normalized_error")
    _compare(
        analysis.nominal_weighted_rms_error,
        weighted_rms_error(normals, weights),
        "nominal_weighted_rms_error",
    )


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
        or result.cache_identity != _request_identity(request)
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
        else ("slsqp_converged", "zero_sensitivity_objective")
    ):
        raise ValueError("completed result has an invalid termination reason")
    if result.operation == "optimize" and result.state == "completed":
        if not any(v.lower_nm < _finite(v.upper_nm, "upper_nm") for v in request.variables):
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
        scale = _finite(result.solver_objective_scale, "solver_objective_scale")
        if (
            scale <= 0
            or result.baseline_analysis is None
            or scale != result.baseline_analysis.objective_value
        ):
            raise ValueError("solver conditioning must use the fixed positive initial objective")
        _compare(result.baseline_solver_objective_value, 1.0, "baseline_solver_objective_value")
        if result.best_analysis is not None:
            _compare(
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
        "completed": {"analysis_complete", "slsqp_converged", "zero_sensitivity_objective"},
        "partial": {
            "checkpoint",
            "evaluation_limit",
            "solver_nominal_rejected",
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
        or any(v.lower_nm < _finite(v.upper_nm, "upper_nm") for v in request.variables)
    ):
        raise ValueError("all_variables_fixed must fail without free thickness variables")
    _validate_analysis_shape(result.baseline_analysis, request)
    _validate_analysis_shape(result.best_analysis, request)
    if (result.best_design is None) != (result.best_analysis is None):
        raise ValueError("best design and analysis must either both be present or both absent")
    if result.best_design is None and result.best_thickness_nm:
        raise ValueError("an empty result cannot claim physical thicknesses")
    if (
        result.operation == "optimize"
        and result.best_analysis is not None
        and not result.best_analysis.nominal_targets_met
    ):
        raise ValueError("optimization incumbent must satisfy exact original nominal bounds")
    if (
        result.baseline_analysis is not None
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
    evaluator = _Evaluator(request, enforce_budget=False)
    if result.best_design is not None:
        if _optical_invariants(result.best_design) != _optical_invariants(request.design_snapshot):
            raise ValueError("result changed fixed topology, material n/k policy or stable IDs")
        initial, physical = (
            evaluator.physical(request.design_snapshot),
            evaluator.physical(result.best_design),
        )
        _compare(result.best_thickness_nm, physical, "best_thickness_nm")
        variables = {v.layer_id: v for v in request.variables}
        for identifier, value in physical.items():
            if result.operation == "analyze" or identifier not in variables:
                if value != initial[identifier]:
                    raise ValueError("result changed an unselected thickness")
            elif not variables[identifier].lower_nm <= value <= variables[identifier].upper_nm:
                raise ValueError("result violates physical thickness bounds")
    if replay:
        if result.baseline_analysis is not None:
            _compare(
                asdict(result.baseline_analysis),
                asdict(evaluator.evaluate(request.design_snapshot).analysis),
                "baseline_analysis",
            )
        if result.best_design is not None:
            assert result.best_analysis is not None
            _compare(
                asdict(result.best_analysis),
                asdict(evaluator.evaluate(result.best_design).analysis),
                "best_analysis",
            )
    return film_ideal_sensitivity_result_to_payload(result)


# Schema-2 facade dispatch is additive. Keep every schema-1 dataclass, payload,
# numerical helper and digest input unchanged, including the default adapter.
_v1_estimate_film_ideal_sensitivity_resources = estimate_film_ideal_sensitivity_resources
_v1_freeze_film_ideal_sensitivity_request = freeze_film_ideal_sensitivity_request
_v1_execute_film_ideal_sensitivity_request = execute_film_ideal_sensitivity_request
_v1_analyze_film_ideal_sensitivity = analyze_film_ideal_sensitivity
_v1_film_ideal_sensitivity_request_to_payload = film_ideal_sensitivity_request_to_payload
_v1_film_ideal_sensitivity_request_from_payload = film_ideal_sensitivity_request_from_payload
_v1_film_ideal_sensitivity_result_to_payload = film_ideal_sensitivity_result_to_payload
_v1_film_ideal_sensitivity_result_from_payload = film_ideal_sensitivity_result_from_payload
_v1_validate_film_ideal_sensitivity_result_payload = validate_film_ideal_sensitivity_result_payload


def _v2_module():
    from . import film_ideal_sensitivity_v2
    return film_ideal_sensitivity_v2


def _is_v2(value):
    return (getattr(value, "schema_version", None) == 2
            or isinstance(value, Mapping) and value.get("schema_version") == 2)


def estimate_film_ideal_sensitivity_resources(request):  # type: ignore[no-redef]
    return (_v2_module().estimate_film_ideal_sensitivity_resources(request) if _is_v2(request)
            else _v1_estimate_film_ideal_sensitivity_resources(request))


def freeze_film_ideal_sensitivity_request(service, request):  # type: ignore[no-redef]
    return (_v2_module().freeze_film_ideal_sensitivity_request(service, request) if _is_v2(request)
            else _v1_freeze_film_ideal_sensitivity_request(service, request))


def execute_film_ideal_sensitivity_request(service, request, **kwargs):  # type: ignore[no-redef]
    return (_v2_module().execute_film_ideal_sensitivity_request(service, request, **kwargs)
            if _is_v2(request) else _v1_execute_film_ideal_sensitivity_request(service, request, **kwargs))


def analyze_film_ideal_sensitivity(service, request, **kwargs):  # type: ignore[no-redef]
    return (_v2_module().analyze_film_ideal_sensitivity(service, request, **kwargs)
            if _is_v2(request) else _v1_analyze_film_ideal_sensitivity(service, request, **kwargs))


def film_ideal_sensitivity_request_to_payload(request):  # type: ignore[no-redef]
    return (_v2_module().film_ideal_sensitivity_request_to_payload(request) if _is_v2(request)
            else _v1_film_ideal_sensitivity_request_to_payload(request))


def film_ideal_sensitivity_request_from_payload(payload):  # type: ignore[no-redef]
    return (_v2_module().film_ideal_sensitivity_request_from_payload(payload) if _is_v2(payload)
            else _v1_film_ideal_sensitivity_request_from_payload(payload))


def film_ideal_sensitivity_result_to_payload(result):  # type: ignore[no-redef]
    return (_v2_module().film_ideal_sensitivity_result_to_payload(result) if _is_v2(result)
            else _v1_film_ideal_sensitivity_result_to_payload(result))


def film_ideal_sensitivity_result_from_payload(payload):  # type: ignore[no-redef]
    return (_v2_module().film_ideal_sensitivity_result_from_payload(payload) if _is_v2(payload)
            else _v1_film_ideal_sensitivity_result_from_payload(payload))


def validate_film_ideal_sensitivity_result_payload(payload, **kwargs):  # type: ignore[no-redef]
    return (_v2_module().validate_film_ideal_sensitivity_result_payload(payload, **kwargs)
            if _is_v2(payload) else _v1_validate_film_ideal_sensitivity_result_payload(payload, **kwargs))


def ideal_sensitivity_v2_request_from_optimization(base, **kwargs):
    return _v2_module().ideal_sensitivity_v2_request_from_optimization(base, **kwargs)


def __getattr__(name):
    if name in {"FilmIdealSensitivityV2Request", "FilmIdealSensitivityV2Result",
                "FilmIdealDegradationBudget", "FilmIdealDegradationGuard",
                "FilmIdealDegradationRow", "FilmIdealSensitivityBaselineEvidence",
                "FilmIdealSensitivityBaselineCondition"}:
        return getattr(_v2_module(), name)
    raise AttributeError(name)
