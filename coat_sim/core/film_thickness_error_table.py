"""Frozen, analysis-only thickness-error parameter tables.

Missing values never mean zero. Bias, bounded amplitudes and user-supplied
standard deviations are separate quantities; no distribution, independence,
yield probability, optimization candidate or finite-error certificate is inferred.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import math
from typing import Any, Callable, Mapping

import numpy as np

from .film_design import validate_design
from .film_nominal_acceptance import nominal_condition_pass
from .film_objective_criteria import FilmConditionError, reduce_objective_responses
from .film_objective_payload import condition_from_payload, objective_from_payload
from .optical_constants import prepare_optical_set
from .film_optimization import (FilmOptimizationObjective, FilmOptimizationRequest,
                                FilmThicknessVariable, _checked_frozen_snapshot)
from .film_optimization_derivatives import _PreparedMaterialData, simulate_with_thickness_jacobian
from .film_optimization_needle import _normalize_objectives, _physical_layer_thickness

REQUEST_KIND = "coat_sim.film_thickness_error_table_request"
RESULT_KIND = "coat_sim.film_thickness_error_table_result"
SCHEMA_VERSION = 2
MAX_RETAINED_RESULT_BYTES = 256 * 1024**2
MAX_WORKING_BYTES = 256 * 1024**2
MAX_CHUNK_SAMPLES = 4096
UNITS = ("absolute_nm", "relative_fraction")
LIMITATIONS = (
    "Analysis only: frozen single-sided coherent front-coating thickness errors.",
    "Missing parameters are unknown, not zero; all results apply only to the explicitly defined subsets.",
    "Bounded L1 responses are first-order local estimates, not nonlinear finite-thickness guarantees.",
    "Standard deviations require an explicit ordered correlation matrix for aggregation; no independence is assumed.",
    "Bias, bounded amplitudes and standard deviations are separate; no total probability or yield is inferred.",
    "Finite bias responses change only explicitly defined bias layers; all other layers remain nominal.",
)


@dataclass(frozen=True)
class FilmThicknessErrorParameter:
    layer_id: str
    enabled: bool = True
    bias: float | None = None
    bias_unit: str | None = None
    uncertainty_kind: str | None = None
    uncertainty_magnitude: float | None = None
    uncertainty_unit: str | None = None


@dataclass(frozen=True)
class FilmThicknessErrorTableRequest:
    design_snapshot: Mapping[str, Any]
    design_revision: int
    objectives: tuple[FilmOptimizationObjective, ...]
    rows: tuple[FilmThicknessErrorParameter, ...]
    correlation_layer_ids: tuple[str, ...] = ()
    correlation_matrix: tuple[tuple[float, ...], ...] | None = None
    include_finite_bias: bool = False
    frozen_material_snapshot: Mapping[str, Any] | None = None
    max_evaluations: int | None = None


@dataclass(frozen=True)
class FilmThicknessErrorTableCoverage:
    front_layer_ids: tuple[str, ...]
    configured_layer_ids: tuple[str, ...]
    enabled_layer_ids: tuple[str, ...]
    disabled_layer_ids: tuple[str, ...]
    unconfigured_layer_ids: tuple[str, ...]
    enabled_undefined_layer_ids: tuple[str, ...]
    disabled_defined_layer_ids: tuple[str, ...]
    disabled_undefined_layer_ids: tuple[str, ...]
    defined_bias_layer_ids: tuple[str, ...]
    missing_bias_layer_ids: tuple[str, ...]
    bounded_layer_ids: tuple[str, ...]
    stddev_layer_ids: tuple[str, ...]
    missing_uncertainty_layer_ids: tuple[str, ...]
    bias_scope: str
    bounded_scope: str
    stddev_scope: str


@dataclass(frozen=True)
class FilmThicknessErrorCorrelation:
    status: str
    layer_ids: tuple[str, ...]
    matrix: tuple[tuple[float, ...], ...] | None
    tau: float | None
    min_eigenvalue: float | None
    matrix_unmodified: bool = True


@dataclass(frozen=True)
class FilmThicknessErrorTableCondition:
    objective_index: int
    wavelength_nm: float
    angle_deg: float
    polarization: str
    metric: str
    nominal_value: float
    nominal_condition_pass: bool | None
    bias_shift: float | None
    linear_bias_value: float | None
    bounded_first_order_l1: float | None
    aggregate_sigma: float | None
    aggregate_sigma_status: str
    finite_bias_value: float | None
    finite_bias_condition_pass: bool | None


@dataclass(frozen=True)
class FilmThicknessErrorLayerSummary:
    layer_id: str
    nominal_thickness_nm: float
    bias_nm: float | None
    uncertainty_kind: str | None
    uncertainty_nm: float | None
    max_abs_derivative_per_nm: float
    max_abs_bias_contribution: float | None
    max_bounded_contribution: float | None
    max_stddev_contribution: float | None
    rms_bias_contribution: float | None
    rms_bounded_contribution: float | None
    rms_stddev_contribution: float | None


@dataclass(frozen=True)
class FilmThicknessErrorTableAnalysis:
    coverage: FilmThicknessErrorTableCoverage
    correlation: FilmThicknessErrorCorrelation
    expected_condition_count: int
    completed_condition_count: int
    conditions_complete: bool
    nominal_targets_met: bool | None
    finite_bias_targets_met: bool | None
    finite_bias_scope: str | None
    aggregate_sigma_status: str
    conditions: tuple[FilmThicknessErrorTableCondition, ...]
    layer_summaries: tuple[FilmThicknessErrorLayerSummary, ...]
    material_k_policy: str
    nominal_conditions: tuple[FilmConditionError, ...]
    finite_bias_conditions: tuple[FilmConditionError, ...]


@dataclass(frozen=True)
class FilmThicknessErrorTableProgress:
    stage: str
    state: str
    completed_conditions: int
    expected_conditions: int
    evaluations: int
    message: str = ""


@dataclass(frozen=True)
class FilmThicknessErrorTableResult:
    state: str
    termination_reason: str
    design_revision: int
    analysis: FilmThicknessErrorTableAnalysis | None
    evaluations: int
    forward_evaluations: int
    jacobian_evaluations: int
    failed_evaluations: int
    cache_identity: str
    frozen_request: FilmThicknessErrorTableRequest
    is_final: bool = True
    message: str = ""
    limitations: tuple[str, ...] = LIMITATIONS


def _finite(value: Any, label: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite real number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _sequence(value: Any, label: str) -> tuple:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{label} must be a sequence")
    return tuple(value)


def _json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("thickness-error table data must be finite JSON") from exc


def _pointwise_pass(objective, actual):
    # A raw sample has no mean acceptance label; only its enabled local target
    # can supply a local verdict. Complete-band verdicts use the reducer below.
    return (nominal_condition_pass(objective.pointwise, actual)
            if objective.pointwise.enabled else None)


def _reduce_conditions(request, conditions, value_field):
    frames = []
    for index, objective in enumerate(request.objectives):
        for angle in objective.angles_deg:
            rows = [row for row in conditions
                    if row.objective_index == index and row.angle_deg == angle]
            frames.append(dict(objective_index=index, angle_deg=angle,
                               wavelength_nm=[row.wavelength_nm for row in rows],
                               actual_values=[getattr(row, value_field) for row in rows]))
    return reduce_objective_responses(request.objectives, frames)


def _defined(row: FilmThicknessErrorParameter) -> bool:
    return row.bias is not None or row.uncertainty_magnitude is not None


def _coverage(request: FilmThicknessErrorTableRequest) -> FilmThicknessErrorTableCoverage:
    front = tuple(layer["id"] for layer in request.design_snapshot["front_layers"])
    configured = tuple(row.layer_id for row in request.rows)
    enabled = tuple(row.layer_id for row in request.rows if row.enabled)
    disabled = tuple(row.layer_id for row in request.rows if not row.enabled)
    bias = tuple(row.layer_id for row in request.rows if row.enabled and row.bias is not None)
    bounded = tuple(row.layer_id for row in request.rows if row.enabled and
                    row.uncertainty_kind == "bounded" and row.uncertainty_magnitude is not None)
    sigma = tuple(row.layer_id for row in request.rows if row.enabled and
                  row.uncertainty_kind == "stddev" and row.uncertainty_magnitude is not None)

    def scope(ids):
        return "not_defined" if not ids else "whole_front_film" if set(ids) == set(front) else "defined_subset_only"

    return FilmThicknessErrorTableCoverage(front, configured, enabled, disabled,
        tuple(i for i in front if i not in configured),
        tuple(row.layer_id for row in request.rows if row.enabled and not _defined(row)),
        tuple(row.layer_id for row in request.rows if not row.enabled and _defined(row)),
        tuple(row.layer_id for row in request.rows if not row.enabled and not _defined(row)),
        bias, tuple(i for i in front if i not in bias), bounded, sigma,
        tuple(i for i in front if i not in bounded and i not in sigma),
        scope(bias), scope(bounded), scope(sigma))


def estimate_film_thickness_error_table_resources(request: FilmThicknessErrorTableRequest) -> dict[str, int]:
    """Conservative simultaneous-array policy, not a measured process RSS.

    At most one forward chunk is active. Its complete physical topology still
    allocates layer/index/q/interface/phase complex128 arrays, float64 flux and
    derivative arrays, even for one selected derivative column. The 128-byte
    coefficients reserve eight complex128 (or sixteen float64) simultaneous
    values per layer/column/sample, plus 4096 bytes/sample for point buffers and
    recurrence temporaries. Retained Python/JSON analysis and rho work coexist
    with that chunk; the prepared-source cache is not counted again as RSS.
    No grid, selected column, layer or chunk size is altered to pass this gate.
    """
    count = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
    columns = sum(row.enabled and _defined(row) for row in request.rows)
    sigma = sum(row.enabled and row.uncertainty_kind == "stddev" and
                row.uncertainty_magnitude is not None for row in request.rows)
    # Keep raw error rows and both nominal/finite-bias reduced target records.
    # Enabling only a mean never removes the independent per-point evidence.
    retained = count * 4096 + len(request.rows) * 4096 + sigma * sigma * 96
    physical_layers = (len(request.design_snapshot["front_layers"]) +
                       len(request.design_snapshot.get("back_layers", ())))
    chunk = max((min(o.wavelength_samples, MAX_CHUNK_SAMPLES) for o in request.objectives), default=0)
    physical_working = chunk * (128 * (physical_layers + 2) + 128 * columns + 4096)
    working = physical_working + retained + sigma * sigma * 128
    return dict(condition_count=count, parameter_row_count=len(request.rows),
        derivative_column_count=columns, stddev_column_count=sigma,
        physical_layer_count=physical_layers, max_chunk_samples=chunk,
        physical_forward_working_bytes=physical_working,
        estimated_retained_result_bytes=retained, estimated_working_bytes=working,
        max_retained_result_bytes=MAX_RETAINED_RESULT_BYTES, max_working_bytes=MAX_WORKING_BYTES)


def _correlation(request: FilmThicknessErrorTableRequest) -> FilmThicknessErrorCorrelation:
    sigma_ids = _coverage(request).stddev_layer_ids
    if request.correlation_matrix is None:
        if request.correlation_layer_ids:
            raise ValueError("correlation_layer_ids require an explicit correlation_matrix")
        return FilmThicknessErrorCorrelation("missing_information" if sigma_ids else "not_configured",
                                             (), None, None, None)
    if not sigma_ids or tuple(request.correlation_layer_ids) != sigma_ids:
        raise ValueError("correlation_layer_ids must exactly match the ordered enabled defined stddev rows")
    matrix = _sequence(request.correlation_matrix, "correlation_matrix")
    n = len(sigma_ids)
    if len(matrix) != n:
        raise ValueError("correlation_matrix dimension differs from stddev rows")
    matrix = tuple(tuple(_finite(v, "correlation coefficient")
                         for v in _sequence(row, "correlation matrix row")) for row in matrix)
    if any(len(row) != n for row in matrix):
        raise ValueError("correlation_matrix must be square")
    if any(matrix[i][i] != 1.0 for i in range(n)):
        raise ValueError("correlation_matrix diagonal must be exactly 1")
    if any(matrix[i][j] != matrix[j][i] for i in range(n) for j in range(i)):
        raise ValueError("correlation_matrix must be exactly symmetric; it is never averaged")
    rho = np.asarray(matrix, dtype=float)
    eigen = np.linalg.eigvalsh(rho)
    minimum = float(eigen[0])
    tau = float(64 * np.finfo(float).eps * n * max(1., float(np.linalg.norm(rho, 2))))
    if minimum < -tau:
        raise ValueError("correlation_matrix is not positive semidefinite within the stated numerical tolerance")
    status = "psd_numerically_uncertain" if minimum < 0 else "positive_semidefinite"
    return FilmThicknessErrorCorrelation(status, sigma_ids, matrix, tau, minimum)


def _validate(request: FilmThicknessErrorTableRequest) -> FilmThicknessErrorTableRequest:
    if not isinstance(request, FilmThicknessErrorTableRequest):
        raise ValueError("request must be a FilmThicknessErrorTableRequest")
    design = validate_design(deepcopy(request.design_snapshot))
    if (design["substrate"]["sides"] != "single" or design["back_layers"] or
            design["first_surface"] != "front"):
        raise ValueError("thickness-error tables support only single-sided coherent front coatings")
    if type(request.design_revision) is not int or request.design_revision < 0:
        raise ValueError("design_revision must be a nonnegative integer")
    if type(request.include_finite_bias) is not bool:
        raise ValueError("include_finite_bias must be a boolean")
    objectives = _sequence(request.objectives, "objectives")
    if not objectives:
        raise ValueError("at least one complete original optical objective is required")
    objectives = _normalize_objectives(objectives)
    rows = _sequence(request.rows, "rows")
    front = {layer["id"] for layer in design["front_layers"]}
    seen = set()
    normalized = []
    for row in rows:
        if not isinstance(row, FilmThicknessErrorParameter):
            raise ValueError("rows must contain FilmThicknessErrorParameter values")
        if not isinstance(row.layer_id, str) or row.layer_id not in front or row.layer_id in seen:
            raise ValueError("parameter layer IDs must uniquely identify existing front layers")
        seen.add(row.layer_id)
        if type(row.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if (row.bias is None) != (row.bias_unit is None):
            raise ValueError("bias and bias_unit must be supplied together or both null")
        if row.bias_unit is not None and row.bias_unit not in UNITS:
            raise ValueError("bias_unit must be absolute_nm or relative_fraction")
        bias = None if row.bias is None else _finite(row.bias, "bias")
        if row.uncertainty_kind not in (None, "undefined", "bounded", "stddev"):
            raise ValueError("uncertainty_kind must be undefined, bounded or stddev")
        if (row.uncertainty_magnitude is None) != (row.uncertainty_unit is None):
            raise ValueError("uncertainty magnitude and unit must be supplied together or both null")
        if row.uncertainty_unit is not None and row.uncertainty_unit not in UNITS:
            raise ValueError("uncertainty_unit must be absolute_nm or relative_fraction")
        magnitude = (None if row.uncertainty_magnitude is None else
                     _finite(row.uncertainty_magnitude, "uncertainty_magnitude"))
        if magnitude is not None and (magnitude < 0 or row.uncertainty_kind not in ("bounded", "stddev")):
            raise ValueError("defined uncertainty needs bounded/stddev and a nonnegative magnitude")
        normalized.append(replace(row, bias=bias, uncertainty_magnitude=magnitude))
    if not any(row.enabled and _defined(row) for row in rows):
        raise ValueError("at least one enabled explicitly defined parameter is required; blank is not zero")
    if request.include_finite_bias and not any(row.enabled and row.bias is not None for row in rows):
        raise ValueError("finite bias recomputation requires at least one explicitly defined enabled bias")
    if request.max_evaluations is not None and (type(request.max_evaluations) is not int or request.max_evaluations < 1):
        raise ValueError("max_evaluations must be a positive integer or null")
    ids = _sequence(request.correlation_layer_ids, "correlation_layer_ids")
    request = replace(request, design_snapshot=design, objectives=objectives,
                      rows=tuple(normalized), correlation_layer_ids=ids)
    resources = estimate_film_thickness_error_table_resources(request)
    if (resources["estimated_retained_result_bytes"] > MAX_RETAINED_RESULT_BYTES or
            resources["estimated_working_bytes"] > MAX_WORKING_BYTES):
        raise ValueError("thickness-error table resource estimate exceeds explicit memory policy; no samples or layers were dropped")
    correlation = _correlation(request)
    request = replace(request, correlation_matrix=correlation.matrix)
    if request.frozen_material_snapshot is not None:
        frozen = _checked_frozen_snapshot(request.frozen_material_snapshot, design)
        request = replace(request, frozen_material_snapshot=frozen)
    _json(asdict(request))
    return request


def thickness_error_table_request_from_optimization(base: FilmOptimizationRequest, *,
    rows: tuple[FilmThicknessErrorParameter, ...], correlation_layer_ids: tuple[str, ...] = (),
    correlation_matrix: tuple[tuple[float, ...], ...] | None = None,
    include_finite_bias: bool = False,
    max_evaluations: int | None = None,
) -> FilmThicknessErrorTableRequest:
    if not isinstance(base, FilmOptimizationRequest):
        raise ValueError("base must be a FilmOptimizationRequest")
    return _validate(FilmThicknessErrorTableRequest(deepcopy(base.design_snapshot), base.design_revision,
        tuple(base.objectives), tuple(rows), tuple(correlation_layer_ids), correlation_matrix,
        include_finite_bias, deepcopy(base.frozen_material_snapshot), max_evaluations))


def freeze_film_thickness_error_table_request(service: Any,
    request: FilmThicknessErrorTableRequest,
) -> FilmThicknessErrorTableRequest:
    request = _validate(request)
    if request.frozen_material_snapshot is None:
        if service is None or not callable(getattr(service, "freeze_design", None)):
            raise ValueError("a complete frozen snapshot or film design service is required")
        first = request.objectives[0]
        frozen = service.freeze_design(request.design_snapshot, dict(
            wavelength_min_nm=first.wavelength_min_nm, wavelength_max_nm=first.wavelength_max_nm,
            samples=first.wavelength_samples, angle_deg=first.angles_deg[0],
            polarization=first.polarization, first_surface="front"))
    else:
        frozen = request.frozen_material_snapshot
    frozen = _checked_frozen_snapshot(frozen, request.design_snapshot)
    request = replace(request, design_snapshot=deepcopy(frozen["design"]), frozen_material_snapshot=frozen)
    _check_frozen_material_domain(request)
    # Check the entire defined finite perturbation before performing any forward call.
    if request.include_finite_bias:
        _physical_parameters(request)
    return request


def _check_frozen_material_domain(request):
    """Prevent the legacy forward sampler's unknown/out-of-domain k=0 default.

    This entry point accepts only calculable source n/k on the complete original
    grid. Explicit use_k=False is preserved, never selected on the user's behalf.
    Frozen source records are not rewritten or padded with synthetic k samples.
    """
    frozen = request.frozen_material_snapshot
    assert frozen is not None
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
        optical = next((row for row in material.get("optical_sets", []) if row["id"] == key[1]), None)
        if optical is None:
            raise ValueError(f"missing frozen optical source {key[0]}/{key[1]}")
        samplers[key] = prepare_optical_set(optical, k_sampling_policy="source")
    use_k = request.design_snapshot["use_k"]
    for objective in request.objectives:
        waves = np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm, objective.wavelength_samples)
        for key, sampler in samplers.items():
            try:
                # Unknown k or non-tabulated sources need scalar inspection.
                series = sampler.evaluate_many(waves, "nm", extrapolation_policy="error", include_k=use_k)
                if series is not None and series[0] is not None:
                    continue
                for wave in waves:
                    _n, k, _details = sampler(float(wave), "nm", extrapolation_policy="error", include_k=use_k)
                    if use_k and k is None:
                        raise ValueError("missing calculable k; no k=0 assumption was made")
            except (ValueError, ArithmeticError) as exc:
                raise ValueError(f"material={key[0]} optical_set={key[1]} original band "
                    f"[{objective.wavelength_min_nm}, {objective.wavelength_max_nm}] nm: {exc}") from exc


def _physical_parameters(request):
    assert request.frozen_material_snapshot is not None
    thickness = {layer["id"]: _physical_layer_thickness(layer, request.design_snapshot,
        request.frozen_material_snapshot) for layer in request.design_snapshot["front_layers"]}
    parameters = []
    for row in request.rows:
        if row.enabled and _defined(row):
            d = thickness[row.layer_id]
            bias = None if row.bias is None else row.bias * (d if row.bias_unit == "relative_fraction" else 1.)
            amplitude = (None if row.uncertainty_magnitude is None else row.uncertainty_magnitude *
                         (d if row.uncertainty_unit == "relative_fraction" else 1.))
            if bias is not None:
                _finite(bias, "physical bias_nm")
            if amplitude is not None:
                _finite(amplitude, "physical uncertainty_nm")
            if request.include_finite_bias and bias is not None and d + bias < 0:
                raise ValueError(f"finite bias gives negative physical thickness for {row.layer_id}; no clipping was applied")
            parameters.append((row, d, bias, amplitude))
    return tuple(parameters)


def _scaled_real_product(left: float, right: float) -> tuple[float, int]:
    """Represent a product before it can overflow or underflow as m * 2**e."""
    if left == 0. or right == 0.:
        return 0., 0
    left_m, left_e = math.frexp(left)
    right_m, right_e = math.frexp(right)
    mantissa, adjustment = math.frexp(left_m * right_m)
    return mantissa, left_e + right_e + adjustment


def _restore_scaled_real(mantissa: float, exponent: int, label: str) -> float:
    try:
        value = math.ldexp(mantissa, exponent)
    except OverflowError as exc:
        raise ValueError(f"{label} is not representable as a finite real output") from exc
    if not math.isfinite(value) or value == 0. and mantissa != 0.:
        raise ValueError(f"{label} is not representable as a nonzero finite real output")
    return value


def _real_product(left: float, right: float, label: str) -> float:
    return _restore_scaled_real(*_scaled_real_product(left, right), label)


def _quadratic_form(vector, matrix) -> float:
    """Stable explicit normalized quadratic form; negative q is never clamped."""
    return math.fsum(float(vector[i]) * float(matrix[i][j]) * float(vector[j])
                     for i in range(len(vector)) for j in range(len(vector)))


def _aggregate_sigma(derivatives, amplitudes, matrix) -> tuple[float | None, str]:
    """Positive power-of-two scaling leaves rho and the sign of q unchanged.

    Neither J*sigma nor its square is formed before exponent separation. The
    normalized q uses the original matrix and stable summation; only its square
    root is rescaled. Exact q=0 remains valid, including perfect cancellation.
    Unrepresentable nonzero outputs or internal loss of a nonzero component
    fail explicitly instead of reporting a spurious zero or infinite sigma.
    """
    products = [_scaled_real_product(float(j), float(a))
                for j, a in zip(derivatives, amplitudes, strict=True)]
    exponent = max((e for m, e in products if m != 0.), default=0)
    normalized = []
    for mantissa, power in products:
        value = math.ldexp(mantissa, power - exponent)
        if value == 0. and mantissa != 0.:
            raise ValueError("sigma dynamic range is not representable without losing a nonzero component")
        normalized.append(value)
    q = _finite(_quadratic_form(normalized, matrix), "scaled sigma quadratic form")
    if q < 0.:
        return None, "numerical_indeterminate"
    return _restore_scaled_real(math.sqrt(q), exponent, "aggregate sigma"), "available"


def _identity(request):
    return hashlib.sha256(json.dumps(film_thickness_error_table_request_to_payload(request),
        ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


class _Cancelled(Exception):
    pass


class _Budget(Exception):
    pass


def execute_film_thickness_error_table_request(service: Any, request: FilmThicknessErrorTableRequest, *,
    cancel_event: Any = None,
    progress_callback: Callable[[FilmThicknessErrorTableProgress], None] | None = None,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
) -> FilmThicknessErrorTableResult:
    """Evaluate all original conditions in bounded blocks, without optimization.

    Invalid contracts raise ValueError. Runtime optical failures are failed
    results. Run cancellation/budget partiality and unavailable numerical sigma
    aggregation are separate states, so neither can be mistaken for success.
    """
    request = _validate(request)
    if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
        raise ValueError("cancel_event must provide is_set()")
    for callback in (progress_callback, checkpoint_callback):
        if callback is not None and not callable(callback):
            raise ValueError("callbacks must be callable")
    expected = estimate_film_thickness_error_table_resources(request)["condition_count"]
    conditions: list[FilmThicknessErrorTableCondition] = []
    parameters: tuple[tuple[FilmThicknessErrorParameter, float, float | None, float | None], ...] = ()
    stats: list[list[float]] = []
    evaluations = jacobians = failures = 0
    coverage = _coverage(request)
    correlation = _correlation(request)

    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled()

    def emit(stage, state="running", message=""):
        if progress_callback is not None:
            progress_callback(FilmThicknessErrorTableProgress(stage, state, len(conditions), expected,
                                                            evaluations, message))

    def analysis():
        if not conditions:
            return None
        complete = len(conditions) == expected
        summaries = []
        for (row, d, bias, amplitude), stat in zip(parameters, stats, strict=True):
            def rms(index, value, stat=stat):
                return None if value is None else stat[index] / math.sqrt(len(conditions))
            summaries.append(FilmThicknessErrorLayerSummary(row.layer_id, d, bias,
                row.uncertainty_kind, amplitude, stat[0],
                None if bias is None else stat[1],
                None if row.uncertainty_kind != "bounded" or amplitude is None else stat[2],
                None if row.uncertainty_kind != "stddev" or amplitude is None else stat[3],
                rms(4, bias), rms(5, amplitude if row.uncertainty_kind == "bounded" else None),
                rms(6, amplitude if row.uncertainty_kind == "stddev" else None)))
        statuses = {row.aggregate_sigma_status for row in conditions}
        aggregate = ("partial" if "numerical_indeterminate" in statuses else
                     "available" if statuses == {"available"} else
                     "unavailable" if "missing_information" in statuses else "not_configured")
        nominal = _reduce_conditions(request, conditions, "nominal_value") if complete else None
        finite = (_reduce_conditions(request, conditions, "finite_bias_value")
                  if complete and request.include_finite_bias else None)
        return FilmThicknessErrorTableAnalysis(coverage, correlation, expected, len(conditions), complete,
            None if nominal is None else nominal.nominal_pass,
            None if finite is None else finite.nominal_pass,
            "defined_subset_only" if request.include_finite_bias else None, aggregate,
            tuple(conditions), tuple(summaries),
            "source_k_required" if request.design_snapshot["use_k"] else "explicit_use_k_false_idealization",
            () if nominal is None else nominal.conditions, () if finite is None else finite.conditions)


    def result(state, reason, message="", is_final=True):
        return FilmThicknessErrorTableResult(state, reason, request.design_revision, analysis(),
            evaluations, evaluations, jacobians, failures, _identity(request), request,
            is_final, message)

    def response(design, materials, variables, settings, derivative, *, wavelengths_nm):
        nonlocal evaluations, jacobians, failures
        check()
        if request.max_evaluations is not None and evaluations >= request.max_evaluations:
            raise _Budget()
        evaluations += 1
        jacobians += int(derivative)
        try:
            output = simulate_with_thickness_jacobian(design, materials, variables, settings,
                                                        wavelengths_nm=wavelengths_nm)
            returned_waves = np.asarray(output["wavelength_nm"])
            if (returned_waves.shape != wavelengths_nm.shape or
                    not np.issubdtype(returned_waves.dtype, np.number) or
                    np.issubdtype(returned_waves.dtype, np.complexfloating) or
                    not np.array_equal(returned_waves, wavelengths_nm)):
                raise ValueError("physical table response wavelength grid differs from the exact original grid")
            values = np.asarray(output[objective.metric], dtype=float)
            jac = np.asarray(output["jacobian_" + objective.metric], dtype=float)
            if (values.shape != (settings["samples"],) or jac.shape != (settings["samples"], len(variables))
                    or not np.all(np.isfinite(values)) or not np.all(np.isfinite(jac))):
                raise ValueError("physical table response returned invalid values/Jacobian")
            return values, jac
        except (ValueError, ArithmeticError):
            failures += 1
            raise

    try:
        emit("freezing")
        check()
        request = freeze_film_thickness_error_table_request(service, request)
        parameters = _physical_parameters(request)
        stats = [[0.] * 7 for _ in parameters]
        materials = _PreparedMaterialData(request.frozen_material_snapshot, request.design_snapshot)
        variables = tuple(FilmThicknessVariable("front", row.layer_id, 0., 1.) for row, *_ in parameters)
        finite_design = deepcopy(request.design_snapshot)
        for row, d, bias, _amplitude in parameters:
            if request.include_finite_bias and bias is not None:
                layer = next(layer for layer in finite_design["front_layers"] if layer["id"] == row.layer_id)
                layer["thickness"] = dict(value=d + bias, unit="nm")
        sigma_columns = [i for i, (row, _d, _b, a) in enumerate(parameters)
                         if row.uncertainty_kind == "stddev" and a is not None]
        emit("analyzing")
        for index, objective in enumerate(request.objectives):
            waves = np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm,
                                objective.wavelength_samples)
            for angle in objective.angles_deg:
                for start in range(0, len(waves), MAX_CHUNK_SAMPLES):
                    chunk = waves[start:start + MAX_CHUNK_SAMPLES]
                    settings = dict(wavelength_min_nm=float(chunk[0]), wavelength_max_nm=float(chunk[-1]),
                        samples=len(chunk), angle_deg=angle, polarization=objective.polarization, first_surface="front")
                    nominal, jac = response(request.design_snapshot, materials, variables, settings, True,
                                            wavelengths_nm=chunk)
                    finite = None
                    if request.include_finite_bias:
                        finite, _ = response(finite_design, materials, (), settings, False, wavelengths_nm=chunk)
                    check()
                    for offset, wave in enumerate(chunk):
                        derivative = jac[offset]
                        bias_terms = []
                        bound_terms = []
                        next_stats = [list(stat) for stat in stats]
                        for column, (row, _d, bias, amplitude) in enumerate(parameters):
                            j = float(derivative[column])
                            b = None if bias is None else j * bias
                            bounded = (abs(j) * amplitude if row.uncertainty_kind == "bounded" and amplitude is not None else None)
                            sigma = (abs(_real_product(j, amplitude, "layer stddev contribution"))
                                     if row.uncertainty_kind == "stddev" and amplitude is not None else None)
                            if b is not None:
                                bias_terms.append(b)
                            if bounded is not None:
                                bound_terms.append(bounded)
                            stat = next_stats[column]
                            stat[0] = max(stat[0], abs(j))
                            for k, contribution in enumerate((b, bounded, sigma), start=1):
                                if contribution is not None:
                                    _finite(contribution, "layer contribution")
                                    stat[k] = max(stat[k], abs(contribution))
                                    stat[k + 3] = math.hypot(stat[k + 3], contribution)
                        bias_shift = math.fsum(bias_terms) if bias_terms else None
                        bound = math.fsum(bound_terms) if bound_terms else None
                        aggregate_sigma = None
                        sigma_status = "not_configured"
                        if sigma_columns:
                            sigma_status = "missing_information"
                            if correlation.matrix is not None:
                                sigma_amplitudes = []
                                for column in sigma_columns:
                                    sigma_amplitude = parameters[column][3]
                                    assert sigma_amplitude is not None
                                    sigma_amplitudes.append(sigma_amplitude)
                                aggregate_sigma, sigma_status = _aggregate_sigma(
                                    [float(derivative[c]) for c in sigma_columns],
                                    sigma_amplitudes, correlation.matrix)
                        actual = float(nominal[offset])
                        finite_actual = None if finite is None else float(finite[offset])
                        conditions.append(FilmThicknessErrorTableCondition(index, float(wave), float(angle),
                            objective.polarization, objective.metric, actual,
                            _pointwise_pass(objective, actual), bias_shift,
                            None if bias_shift is None else _finite(actual + bias_shift, "linear bias value"),
                            bound, aggregate_sigma, sigma_status, finite_actual,
                            None if finite_actual is None else _pointwise_pass(objective, finite_actual)))
                        stats = next_stats
                    emit("block_complete")
                    if checkpoint_callback is not None:
                        checkpoint_callback(film_thickness_error_table_result_to_payload(
                            result("partial", "checkpoint", is_final=False)))
        check()
        outcome = result("completed", "analysis_complete")
    except _Cancelled:
        outcome = result("cancelled", "cancelled", "Analysis was cancelled; only completed condition blocks are retained.")
    except _Budget:
        outcome = result("partial", "evaluation_limit", "The explicit forward budget was reached; no conditions were dropped or claimed complete.")
    except (ValueError, ArithmeticError) as exc:
        outcome = result("failed", "physical_evaluation_failed", str(exc))
    emit("finished", outcome.state, outcome.message)
    return outcome


def _record(cls, data, label):
    if not isinstance(data, Mapping) or set(data) - {f.name for f in fields(cls)}:
        raise ValueError(f"invalid {label} or unknown fields")
    try:
        return cls(**data)
    except (TypeError, KeyError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc


def _envelope(payload, cls, kind):
    if (not isinstance(payload, Mapping) or type(payload.get("schema_version")) is not int or
            payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != kind):
        raise ValueError(f"invalid {kind} envelope")
    data = deepcopy(dict(payload))
    data.pop("schema_version")
    data.pop("kind")
    if set(data) - {f.name for f in fields(cls)}:
        raise ValueError(f"unknown {kind} fields")
    _json(data)
    return data


def film_thickness_error_table_request_to_payload(request: FilmThicknessErrorTableRequest) -> dict[str, Any]:
    request = _validate(request)
    return _json(dict(schema_version=SCHEMA_VERSION, kind=REQUEST_KIND, **asdict(request)))


def film_thickness_error_table_request_from_payload(payload: Mapping[str, Any]) -> FilmThicknessErrorTableRequest:
    data = _envelope(payload, FilmThicknessErrorTableRequest, REQUEST_KIND)
    try:
        data["objectives"] = tuple(objective_from_payload(row)
                                   for row in _sequence(data["objectives"], "objectives"))
        data["rows"] = tuple(_record(FilmThicknessErrorParameter, row, "parameter row")
                             for row in _sequence(data["rows"], "rows"))
        return _validate(_record(FilmThicknessErrorTableRequest, data, "request"))
    except (TypeError, KeyError) as exc:
        raise ValueError(f"invalid thickness-error table request: {exc}") from exc


def _analysis_from_payload(payload):
    if payload is None:
        return None
    data = deepcopy(dict(payload)) if isinstance(payload, Mapping) else payload
    record = _record(FilmThicknessErrorTableAnalysis, data, "analysis")
    coverage_data = asdict(_record(FilmThicknessErrorTableCoverage, record.coverage, "coverage"))
    for name, value in coverage_data.items():
        if name.endswith("_ids"):
            coverage_data[name] = _sequence(value, name)
    correlation = _record(FilmThicknessErrorCorrelation, record.correlation, "correlation")
    correlation = replace(correlation, layer_ids=_sequence(correlation.layer_ids, "correlation layer_ids"),
        matrix=None if correlation.matrix is None else tuple(_sequence(row, "correlation row") for row in
        _sequence(correlation.matrix, "correlation matrix")))
    return replace(record, coverage=_record(FilmThicknessErrorTableCoverage, coverage_data, "coverage"),
        correlation=correlation,
        nominal_conditions=tuple(condition_from_payload(row) for row in record.nominal_conditions),
        finite_bias_conditions=tuple(condition_from_payload(row) for row in record.finite_bias_conditions),
        conditions=tuple(_record(FilmThicknessErrorTableCondition, row, "condition") for row in
                         _sequence(record.conditions, "conditions")),
        layer_summaries=tuple(_record(FilmThicknessErrorLayerSummary, row, "layer summary") for row in
                              _sequence(record.layer_summaries, "layer_summaries")))


def film_thickness_error_table_result_to_payload(result: FilmThicknessErrorTableResult) -> dict[str, Any]:
    if not isinstance(result, FilmThicknessErrorTableResult):
        raise ValueError("result must be a FilmThicknessErrorTableResult")
    data = asdict(result)
    data["frozen_request"] = film_thickness_error_table_request_to_payload(result.frozen_request)
    payload = _json(dict(schema_version=SCHEMA_VERSION, kind=RESULT_KIND, **data))
    film_thickness_error_table_result_from_payload(payload)
    return payload


def film_thickness_error_table_result_from_payload(payload: Mapping[str, Any]) -> FilmThicknessErrorTableResult:
    data = _envelope(payload, FilmThicknessErrorTableResult, RESULT_KIND)
    try:
        data["frozen_request"] = film_thickness_error_table_request_from_payload(data["frozen_request"])
        data["analysis"] = _analysis_from_payload(data["analysis"])
        data["limitations"] = _sequence(data.get("limitations", LIMITATIONS), "limitations")
        result = _record(FilmThicknessErrorTableResult, data, "result")
    except (TypeError, KeyError) as exc:
        raise ValueError(f"invalid thickness-error table result: {exc}") from exc
    _validate_result(result)
    return result


def _validate_result(result):
    request = result.frozen_request
    if type(result.design_revision) is not int or result.design_revision < 0:
        raise ValueError("result design_revision must be a nonnegative integer, not bool")
    if result.design_revision != request.design_revision or result.cache_identity != _identity(request):
        raise ValueError("result identity differs from its exact frozen request")
    if type(result.is_final) is not bool or not isinstance(result.message, str) or result.limitations != LIMITATIONS:
        raise ValueError("invalid result flags, message or limitations")
    reasons = {"completed": {"analysis_complete"}, "partial": {"evaluation_limit", "checkpoint"},
               "cancelled": {"cancelled", "scheduler_cancel_won"},
               "failed": {"physical_evaluation_failed", "scheduler_failed"}}
    if result.state not in reasons or result.termination_reason not in reasons[result.state]:
        raise ValueError("invalid result state/termination_reason")
    if (not result.is_final) != (result.state == "partial" and result.termination_reason == "checkpoint"):
        raise ValueError("nonfinal results must be partial checkpoints")
    for name in ("evaluations", "forward_evaluations", "jacobian_evaluations", "failed_evaluations"):
        if type(getattr(result, name)) is not int or getattr(result, name) < 0:
            raise ValueError("work counters must be nonnegative integers")
    if (result.forward_evaluations != result.evaluations or result.jacobian_evaluations > result.evaluations or
            result.failed_evaluations > result.evaluations or
            request.max_evaluations is not None and result.evaluations > request.max_evaluations):
        raise ValueError("inconsistent result work counters")
    if result.state == "partial" and result.termination_reason == "evaluation_limit" and (
            request.max_evaluations is None or result.evaluations != request.max_evaluations):
        raise ValueError("evaluation_limit requires an exhausted explicit forward budget")
    analysis = result.analysis
    if analysis is None:
        if result.state == "completed":
            raise ValueError("completed result requires complete analysis")
        return
    if request.frozen_material_snapshot is None:
        raise ValueError("retained analysis requires frozen materials")
    expected = estimate_film_thickness_error_table_resources(request)["condition_count"]
    if (type(analysis.expected_condition_count) is not int or analysis.expected_condition_count != expected or type(analysis.completed_condition_count) is not int or
            not 0 < analysis.completed_condition_count <= expected or
            analysis.completed_condition_count != len(analysis.conditions) or
            type(analysis.conditions_complete) is not bool or
            analysis.conditions_complete != (len(analysis.conditions) == expected) or
            result.state == "completed" and not analysis.conditions_complete):
        raise ValueError("analysis coverage count/completeness differs from original conditions")
    if (type(analysis.correlation.matrix_unmodified) is not bool or not analysis.correlation.matrix_unmodified or
            analysis.material_k_policy != ("source_k_required" if request.design_snapshot["use_k"] else "explicit_use_k_false_idealization")):
        raise ValueError("invalid correlation preservation or material-k policy metadata")
    for value in (analysis.correlation.tau, analysis.correlation.min_eigenvalue):
        if value is not None:
            _finite(value, "correlation numerical metadata")
    if analysis.correlation.matrix is not None:
        for values in analysis.correlation.matrix:
            for value in values:
                _finite(value, "correlation matrix coefficient")
    if analysis.coverage != _coverage(request) or analysis.correlation != _correlation(request):
        raise ValueError("analysis parameter coverage/correlation differs from request")
    descriptors = ((i, float(w), float(a), o) for i, o in enumerate(request.objectives)
                   for a in o.angles_deg for w in np.linspace(o.wavelength_min_nm, o.wavelength_max_nm, o.wavelength_samples))
    for row, (i, w, a, o) in zip(analysis.conditions, descriptors, strict=False):
        if (type(row.objective_index) is not int or
                (row.objective_index, row.wavelength_nm, row.angle_deg, row.polarization, row.metric) != (i, w, a, o.polarization, o.metric)):
            raise ValueError("condition identity/order differs from the complete original request")
        _finite(row.wavelength_nm, "wavelength_nm")
        _finite(row.angle_deg, "angle_deg")
        _finite(row.nominal_value, "nominal value")
        if row.nominal_condition_pass is not _pointwise_pass(o, row.nominal_value):
            raise ValueError("raw pointwise acceptance differs from its enabled constraint")
        for name in ("bias_shift", "linear_bias_value", "bounded_first_order_l1", "aggregate_sigma", "finite_bias_value"):
            if getattr(row, name) is not None:
                value = _finite(getattr(row, name), name)
                if name in ("bounded_first_order_l1", "aggregate_sigma") and value < 0:
                    raise ValueError("bound and sigma must be nonnegative")
        has_bias = bool(analysis.coverage.defined_bias_layer_ids)
        if ((row.bias_shift is not None) != has_bias or (row.linear_bias_value is not None) != has_bias or
                (row.bounded_first_order_l1 is not None) != bool(analysis.coverage.bounded_layer_ids)):
            raise ValueError("condition component presence differs from explicitly defined subsets")
        if has_bias and row.linear_bias_value != row.nominal_value + row.bias_shift:
            raise ValueError("linear bias value differs from nominal + signed bias shift")
        if request.include_finite_bias:
            if row.finite_bias_value is None or row.finite_bias_condition_pass is not _pointwise_pass(o, row.finite_bias_value):
                raise ValueError("finite bias value/acceptance is missing or inconsistent")
        elif row.finite_bias_value is not None or row.finite_bias_condition_pass is not None:
            raise ValueError("finite bias was not requested")
        if not analysis.coverage.stddev_layer_ids:
            status = "not_configured"
        elif request.correlation_matrix is None:
            status = "missing_information"
        else:
            status = row.aggregate_sigma_status
            if status not in ("available", "numerical_indeterminate"):
                raise ValueError("invalid aggregate sigma condition status")
        if row.aggregate_sigma_status != status or (row.aggregate_sigma is not None) != (status == "available"):
            raise ValueError("aggregate sigma presence/status differs from explicit correlation information")
    nominal = (_reduce_conditions(request, analysis.conditions, "nominal_value")
               if analysis.conditions_complete else None)
    finite = (_reduce_conditions(request, analysis.conditions, "finite_bias_value")
              if request.include_finite_bias and analysis.conditions_complete else None)
    if (analysis.nominal_targets_met is not (None if nominal is None else nominal.nominal_pass)
            or analysis.finite_bias_targets_met is not (None if finite is None else finite.nominal_pass)
            or analysis.nominal_conditions != (() if nominal is None else nominal.conditions)
            or analysis.finite_bias_conditions != (() if finite is None else finite.conditions)
            or analysis.finite_bias_scope != ("defined_subset_only" if request.include_finite_bias else None)):
        raise ValueError("enabled band target flags/evidence differ from full original-grid reduction")
    statuses = {row.aggregate_sigma_status for row in analysis.conditions}
    aggregate = ("partial" if "numerical_indeterminate" in statuses else "available" if statuses == {"available"}
                 else "unavailable" if "missing_information" in statuses else "not_configured")
    if analysis.aggregate_sigma_status != aggregate:
        raise ValueError("aggregate sigma summary status differs from condition statuses")
    parameters = _physical_parameters(request)
    if len(analysis.layer_summaries) != len(parameters):
        raise ValueError("layer summary count differs from defined enabled parameters")
    for summary, (row, d, bias, amplitude) in zip(analysis.layer_summaries, parameters, strict=True):
        for name in ("nominal_thickness_nm", "bias_nm", "uncertainty_nm"):
            if getattr(summary, name) is not None:
                _finite(getattr(summary, name), name)
        if (summary.layer_id, summary.nominal_thickness_nm, summary.bias_nm, summary.uncertainty_kind, summary.uncertainty_nm) != (row.layer_id, d, bias, row.uncertainty_kind, amplitude):
            raise ValueError("layer summary definitions differ from frozen physical parameters")
        for name in ("max_abs_derivative_per_nm", "max_abs_bias_contribution", "max_bounded_contribution",
                     "max_stddev_contribution", "rms_bias_contribution", "rms_bounded_contribution", "rms_stddev_contribution"):
            value = getattr(summary, name)
            present = (True if name == "max_abs_derivative_per_nm" else bias is not None if "bias" in name else
                       amplitude is not None and row.uncertainty_kind == ("bounded" if "bounded" in name else "stddev"))
            if (value is not None) != present or value is not None and _finite(value, name) < 0:
                raise ValueError("layer contribution summary presence or magnitude is invalid")


def _compare_replay(left, right, path="analysis"):
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            raise ValueError(f"{path} replay fields differ")
        for key in left:
            _compare_replay(left[key], right[key], f"{path}.{key}")
    elif isinstance(left, (tuple, list)) and isinstance(right, (tuple, list)):
        if len(left) != len(right):
            raise ValueError(f"{path} replay dimensions differ")
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            _compare_replay(a, b, f"{path}[{index}]")
    elif isinstance(left, bool) or isinstance(right, bool):
        if type(left) is not type(right) or left != right:
            raise ValueError(f"{path} replay flags differ")
    elif isinstance(left, (float, int)) and isinstance(right, (float, int)):
        if not math.isclose(_finite(left, path), _finite(right, path), rel_tol=1e-10, abs_tol=1e-13):
            raise ValueError(f"{path} frozen physical replay differs")
    elif left != right:
        raise ValueError(f"{path} replay value differs")


def validate_film_thickness_error_table_result_payload(payload: Mapping[str, Any], *,
    expected_request: FilmThicknessErrorTableRequest | Mapping[str, Any] | None = None,
    replay: bool = True,
) -> dict[str, Any]:
    """Verify exact request binding and optionally replay frozen physics only."""
    if type(replay) is not bool:
        raise ValueError("replay must be a boolean")
    result = film_thickness_error_table_result_from_payload(payload)
    request = result.frozen_request
    if expected_request is not None:
        expected = (film_thickness_error_table_request_from_payload(expected_request)
                    if isinstance(expected_request, Mapping) else _validate(expected_request))
        if film_thickness_error_table_request_to_payload(expected) != film_thickness_error_table_request_to_payload(request):
            raise ValueError("result is bound to a different expected_request")
    if replay and result.analysis is not None:
        replay_request = replace(request, max_evaluations=result.evaluations)
        repeated = execute_film_thickness_error_table_request(None, replay_request)
        if repeated.analysis is None:
            raise ValueError("frozen physical replay could not produce the retained analysis")
        _compare_replay(asdict(result.analysis), asdict(repeated.analysis))
        for name in ("evaluations", "forward_evaluations", "jacobian_evaluations", "failed_evaluations"):
            if getattr(result, name) != getattr(repeated, name):
                raise ValueError("frozen physical replay work counters differ")
    return _json(payload)
