"""Frozen thickness-error analysis and explicit robustness-objective optimization.

This is a separate, versioned workflow. It does not modify nominal optimization
acceptance, introduce margins, or assume a manufacturing-error distribution.
Only single-sided coherent front coatings and thickness errors are supported.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import math
from typing import Any, Callable, Mapping

import numpy as np
from numpy.typing import NDArray

from .film_design import validate_design
from .film_candidate_display import (
    CandidateDisplayObserver, finish_candidate_display, record_candidate,
)
from .film_optimization import (
    FilmConditionError, FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable,
    _checked_frozen_snapshot, _wavelength_chunks,
)
from .film_optimization_derivatives import _PreparedMaterialData, simulate_with_thickness_jacobian
from .film_optimization_needle import _normalize_objectives, _physical_layer_thickness
from .film_optimization_scoring import score_condition, weighted_rms_error
from .film_nominal_acceptance import nominal_condition_pass, solver_condition_acceptable

REQUEST_KIND = "coat_sim.film_sensitivity_request"
RESULT_KIND = "coat_sim.film_sensitivity_result"
SCHEMA_VERSION = 1
# Resource policies, not manufacturing assumptions or silently shortened grids.
# Oversized requests fail explicitly before freezing/forward work.
MAX_RETAINED_RESULT_BYTES = 256 * 1024**2
MAX_ANALYSIS_CACHE_BYTES = 64 * 1024**2
ROBUSTNESS_OBJECTIVES = (
    "linear_sensitivity_norm", "finite_scenario_worst", "finite_scenario_expected",
)
LIMITATIONS = (
    "Single-sided front coatings with coherent/auto semi-infinite substrate handling only.",
    "Thickness errors only; refractive-index, extinction, material, angle and wavelength errors are unsupported.",
    "Independent, common-mode, material-common-mode and linear-drift thickness factors only; arbitrary correlations and monitoring feedback are unsupported.",
    "Linear response scales are first-order local estimates, not finite-error bounds or yield probabilities.",
    "Finite scenario results apply only to the explicitly supplied scenarios and sampled optical conditions.",
    "Legacy coordinate search and nominal-preserving constrained scene-cloud search are local, not global certificates.",
    "Estimated retained/serialization result storage is limited to 256 MiB; oversized requests are rejected, never subsampled.",
)


@dataclass(frozen=True)
class FilmThicknessError:
    """Nonnegative thickness scales; relative_fraction=0.01 means 1 percent.

    The legacy magnitude/unit term and explicit absolute/relative terms add.
    All terms share one signed layer factor, not independent quadrature noise.
    A standard-deviation scale is therefore a perfectly correlated sum of its
    absolute and relative components, never an inferred process precision.
    """
    layer_id: str
    magnitude: float = 0.
    unit: str = "absolute_nm"
    absolute_nm: float = 0.
    relative_fraction: float = 0.


@dataclass(frozen=True)
class FilmSensitivityScenario:
    """Explicit signed amplitude multipliers, in request.errors order."""
    scenario_id: str
    coefficients: tuple[float, ...]
    weight: float = 1.0


@dataclass(frozen=True)
class FilmSensitivityRequest:
    design_snapshot: Mapping[str, Any]
    design_revision: int
    objectives: tuple[FilmOptimizationObjective, ...]
    errors: tuple[FilmThicknessError, ...]
    error_model: str
    robustness_objective: str
    variables: tuple[FilmThicknessVariable, ...] = ()
    scenarios: tuple[FilmSensitivityScenario, ...] = ()
    operation: str = "analyze"
    acceptable_normalized_error: float = 1e-3
    max_iterations: int | None = None
    max_evaluations: int | None = None
    initial_step_fraction: float = 0.25
    min_step_fraction: float = 1e-5
    frozen_material_snapshot: Mapping[str, Any] | None = None
    strict_upper_bound_objective_indices: tuple[int, ...] = ()
    optimization_policy: str = "robustness_only"
    error_amplitude_semantics: str = "bounded_amplitude"


@dataclass(frozen=True)
class FilmSensitivityCondition:
    objective_index: int
    wavelength_nm: float
    angle_deg: float
    polarization: str
    metric: str
    nominal_value: float
    derivative_per_nm: tuple[float, ...]
    error_amplitudes_nm: tuple[float, ...]
    linear_response_scale: float
    normalized_linear_response_scale: float


@dataclass(frozen=True)
class FilmSensitivityScenarioResult:
    scenario_id: str
    weight: float
    delta_thickness_nm: tuple[float, ...]
    linear_estimated_values: tuple[float, ...]
    finite_perturbation_values: tuple[float, ...]
    finite_max_normalized_error: float
    finite_weighted_rms_error: float
    finite_targets_met: bool


@dataclass(frozen=True)
class FilmSensitivityAnalysis:
    nominal_max_normalized_error: float
    nominal_weighted_rms_error: float
    nominal_targets_met: bool
    nominal_conditions: tuple[FilmConditionError, ...]
    linear_conditions: tuple[FilmSensitivityCondition, ...]
    linear_sensitivity_norm: float
    finite_scenarios: tuple[FilmSensitivityScenarioResult, ...]
    robustness_value: float


@dataclass(frozen=True)
class FilmSensitivityProgress:
    stage: str
    state: str
    iterations: int
    evaluations: int
    best_robustness_value: float | None = None
    message: str = ""


@dataclass(frozen=True)
class FilmSensitivityResult:
    state: str
    termination_reason: str
    operation: str
    design_revision: int
    robustness_objective: str
    error_model: str
    best_design: Mapping[str, Any] | None
    best_thickness_nm: Mapping[str, float]
    baseline_analysis: FilmSensitivityAnalysis | None
    best_analysis: FilmSensitivityAnalysis | None
    iterations: int
    evaluations: int
    forward_evaluations: int
    jacobian_evaluations: int
    failed_evaluations: int
    cache_identity: str
    frozen_request: FilmSensitivityRequest
    limitations: tuple[str, ...] = LIMITATIONS
    is_final: bool = True
    message: str = ""
    solver_acceptable: bool | None = None

    @property
    def candidate_design(self) -> Mapping[str, Any] | None:
        return self.best_design

    @property
    def nominal_score(self) -> Mapping[str, Any] | None:
        if self.best_analysis is None:
            return None
        return {"max_error": self.best_analysis.nominal_max_normalized_error,
                "weighted_rms_error": self.best_analysis.nominal_weighted_rms_error,
                "errors": self.best_analysis.nominal_conditions}

    @property
    def targets_met(self) -> bool:
        """Nominal sampled acceptance only, never a robustness certificate."""
        return self.state == "completed" and self.nominal_targets_met

    @property
    def nominal_targets_met(self) -> bool:
        return self.best_analysis is not None and self.best_analysis.nominal_targets_met

    @property
    def improved(self) -> bool:
        return (self.best_analysis is not None and self.baseline_analysis is not None
                and self.best_analysis.robustness_value < self.baseline_analysis.robustness_value)


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _sequence(value: Any, label: str) -> tuple:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{label} must be a sequence")
    return tuple(value)



def _error_components(error: FilmThicknessError) -> tuple[float, float]:
    return (error.absolute_nm + (error.magnitude if error.unit == "absolute_nm" else 0.),
            error.relative_fraction + (error.magnitude if error.unit == "relative_fraction" else 0.))


def _error_amplitudes(errors, thickness):
    return np.asarray([absolute + relative * thickness[error.layer_id]
        for error in errors for absolute, relative in (_error_components(error),)], dtype=float)


def _drift_factors(design, errors):
    # Full front deposition order, not the selected-variable/error subset.
    layers = design["front_layers"]
    positions = {row["id"]: index for index, row in enumerate(layers)}
    return np.asarray([1. if len(layers) == 1 else
        -1. + 2. * positions[error.layer_id] / (len(layers) - 1) for error in errors])


def _linear_scale(affected, request):
    if request.error_model == "independent":
        return np.linalg.norm(affected, axis=1)
    if request.error_model == "common_mode":
        return np.abs(np.sum(affected, axis=1))
    if request.error_model == "linear_drift":
        return np.abs(affected @ _drift_factors(request.design_snapshot, request.errors))
    layers = {row["id"]: row for row in request.design_snapshot["front_layers"]}
    groups: dict[str, list[int]] = {}
    for index, error in enumerate(request.errors):
        groups.setdefault(layers[error.layer_id]["material_id"], []).append(index)
    return np.linalg.norm(np.column_stack([affected[:, indices].sum(axis=1)
        for indices in groups.values()]), axis=1)


def generate_film_sensitivity_scenarios(
    design: Mapping[str, Any], errors: tuple[FilmThicknessError, ...], *,
    sample_count: int, seed: int, error_model: str,
    distribution: str, scenario_prefix: str = "process",
) -> tuple[FilmSensitivityScenario, ...]:
    """Reproducible explicit factor cloud, not a measured process distribution.

    bounded_uniform draws coefficients in [-1,1] and requires bounded_amplitude
    request semantics. gaussian draws standard normal coefficients (unbounded,
    never clipped) and requires standard_deviation semantics. Seed, count and
    model are explicit; callers freeze the returned coefficients in their
    request and choose disjoint training/tuning/holdout seeds themselves.
    Absolute and proportional terms in each error share the same factor.
    """
    if type(sample_count) is not int or not 1 <= sample_count <= 100_000:
        raise ValueError("sample_count must be an integer in [1, 100000]")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if error_model not in ("independent", "common_mode", "material_common_mode", "linear_drift"):
        raise ValueError("unsupported process error_model")
    if distribution not in ("bounded_uniform", "gaussian"):
        raise ValueError("distribution must be bounded_uniform or gaussian")
    if not isinstance(scenario_prefix, str) or not scenario_prefix:
        raise ValueError("scenario_prefix must be nonempty")
    design = validate_design(deepcopy(dict(design)))
    layers = {row["id"]: row for row in design["front_layers"]}
    if (not errors or any(not isinstance(e, FilmThicknessError) or e.layer_id not in layers for e in errors)
            or len({e.layer_id for e in errors}) != len(errors)):
        raise ValueError("errors must uniquely select existing front layers")
    for error in errors:
        if (error.unit not in ("absolute_nm", "relative_fraction")
                or any(_finite(getattr(error, name), name) < 0
                       for name in ("magnitude", "absolute_nm", "relative_fraction"))):
            raise ValueError("error scales must be nonnegative and use an explicit supported unit")
    groups: dict[str, int] = {}
    indices = []
    for error in errors:
        group = (error.layer_id if error_model == "independent" else
                 layers[error.layer_id]["material_id"] if error_model == "material_common_mode" else "all")
        if group not in groups:
            groups[group] = len(groups)
        indices.append(groups[group])
    if sample_count * len(errors) > 2_000_000:
        raise ValueError("explicit scenario generator exceeds 2000000 coefficients; split the cloud")
    rng = np.random.default_rng(seed)
    factors = (rng.uniform(-1., 1., (sample_count, len(groups))) if distribution == "bounded_uniform"
               else rng.standard_normal((sample_count, len(groups))))
    coefficients = factors[:, indices]
    if error_model == "linear_drift":
        coefficients = coefficients * _drift_factors(design, errors)
    return tuple(FilmSensitivityScenario(f"{scenario_prefix}-{i:06d}", tuple(float(c) for c in row))
                 for i, row in enumerate(coefficients))

def estimate_film_sensitivity_resources(request: FilmSensitivityRequest) -> dict[str, int]:
    """Conservative Python-object/JSON estimate, not a peak-memory guarantee.

    The estimate includes baseline plus incumbent and serialization working
    copies. It is a project resource policy, not a maximum scenario amplitude,
    an assumed correlation, or an implicit scenario/sample-count reduction.
    """
    count = sum((1 if o.wavelength_min_nm == o.wavelength_max_nm else o.wavelength_samples)
                * len(o.angles_deg) for o in request.objectives)
    errors, scenarios = len(request.errors), len(request.scenarios)
    analysis = count * (768 + 96 * errors + 128 * scenarios) + 512 * scenarios
    solver_bytes = (count * (scenarios + 1) * (len(request.variables) + 1) * 32
                    if request.operation == "optimize" and request.optimization_policy == "nominal_preserving" else 0)
    return {"condition_count": count, "error_count": errors, "scenario_count": scenarios,
            "estimated_analysis_bytes": analysis, "estimated_solver_working_bytes": solver_bytes,
            "estimated_retained_result_bytes": 6 * analysis + solver_bytes,
            "max_retained_result_bytes": MAX_RETAINED_RESULT_BYTES}


def _validate(request: FilmSensitivityRequest) -> FilmSensitivityRequest:
    if not isinstance(request, FilmSensitivityRequest):
        raise ValueError("request must be a FilmSensitivityRequest")
    if not isinstance(request.design_snapshot, Mapping):
        raise ValueError("design_snapshot must be a mapping")
    design = validate_design(deepcopy(dict(request.design_snapshot)))
    if type(request.design_revision) is not int or request.design_revision < 0:
        raise ValueError("design_revision must be a nonnegative integer")
    if (design["substrate"]["sides"] != "single"
            or design.get("first_surface", "front") != "front"
            or design["substrate"].get("mode") not in ("auto", "coherent")):
        raise ValueError("sensitivity supports only single-sided coherent front coatings")
    if request.operation not in ("analyze", "optimize"):
        raise ValueError("operation must be analyze or optimize")
    if request.error_model not in ("independent", "common_mode", "material_common_mode", "linear_drift"):
        raise ValueError("error_model must be independent, common_mode, material_common_mode or linear_drift; arbitrary correlation is unsupported")
    if request.optimization_policy not in ("robustness_only", "nominal_preserving"):
        raise ValueError("optimization_policy must be robustness_only or nominal_preserving")
    if request.error_amplitude_semantics not in ("bounded_amplitude", "standard_deviation"):
        raise ValueError("error_amplitude_semantics must be bounded_amplitude or standard_deviation")
    if (request.operation == "optimize" and request.optimization_policy == "nominal_preserving"
            and request.robustness_objective == "linear_sensitivity_norm"):
        raise ValueError("nominal_preserving requires an explicit finite-scenario objective; linear sensitivity analysis/search remains separate")
    if request.robustness_objective not in ROBUSTNESS_OBJECTIVES:
        raise ValueError("choose an explicit robustness_objective: " + ", ".join(ROBUSTNESS_OBJECTIVES))
    objectives = _sequence(request.objectives, "objectives")
    if not objectives:
        raise ValueError("at least one optical objective is required")
    objectives = _normalize_objectives(objectives)
    errors = _sequence(request.errors, "errors")
    layer_ids = {r["id"] for r in design["front_layers"]}
    seen = set()
    for error in errors:
        if not isinstance(error, FilmThicknessError):
            raise ValueError("errors must contain FilmThicknessError values")
        if not isinstance(error.layer_id, str) or error.layer_id not in layer_ids or error.layer_id in seen:
            raise ValueError("error layer IDs must uniquely select existing front layers")
        seen.add(error.layer_id)
        if error.unit not in ("absolute_nm", "relative_fraction"):
            raise ValueError("thickness error unit must be absolute_nm or relative_fraction")
        if any(_finite(getattr(error, name), name) < 0
               for name in ("magnitude", "absolute_nm", "relative_fraction")):
            raise ValueError("error magnitude and additive scales must be nonnegative")
    if not errors or not any(e.magnitude > 0 or e.absolute_nm > 0 or e.relative_fraction > 0 for e in errors):
        raise ValueError("at least one nonzero thickness-error amplitude is required")
    variables = _sequence(request.variables, "variables")
    seen = set()
    for variable in variables:
        if not isinstance(variable, FilmThicknessVariable):
            raise ValueError("variables must contain FilmThicknessVariable values")
        if variable.side != "front" or variable.layer_id not in layer_ids or variable.layer_id in seen:
            raise ValueError("variables must uniquely select existing front layers")
        seen.add(variable.layer_id)
        low = _finite(variable.lower_nm, "lower_nm")
        high = _finite(variable.upper_nm, "upper_nm")
        if low < 0 or high <= low:
            raise ValueError("optimization bounds must be finite, nonnegative and strictly increasing")
    if request.operation == "optimize" and not variables:
        raise ValueError("robustness optimization requires explicit bounded thickness variables")
    scenarios = _sequence(request.scenarios, "scenarios")
    seen = set()
    for scenario in scenarios:
        if not isinstance(scenario, FilmSensitivityScenario):
            raise ValueError("scenarios must contain FilmSensitivityScenario values")
        if not isinstance(scenario.scenario_id, str) or not scenario.scenario_id or scenario.scenario_id in seen:
            raise ValueError("scenario IDs must be nonempty and unique")
        seen.add(scenario.scenario_id)
        coefficients = _sequence(scenario.coefficients, "scenario coefficients")
        if len(coefficients) != len(errors):
            raise ValueError("scenario coefficients must match errors order and length")
        coefficients = tuple(_finite(c, "scenario coefficient") for c in coefficients)
        if request.error_amplitude_semantics == "bounded_amplitude" and any(abs(c) > 1 for c in coefficients):
            raise ValueError("bounded-amplitude scenario coefficients must lie in [-1, 1]; sigma scenarios require standard_deviation semantics")
        if request.error_model == "common_mode" and len(set(coefficients)) > 1:
            raise ValueError("common_mode scenarios require the same coefficient for every layer")
        if request.error_model == "material_common_mode":
            groups: dict[str, float] = {}
            layers = {r["id"]: r for r in design["front_layers"]}
            for error, coefficient in zip(errors, coefficients, strict=True):
                material = layers[error.layer_id]["material_id"]
                if material in groups and groups[material] != coefficient:
                    raise ValueError("material_common_mode scenarios require the same coefficient within each material")
                groups[material] = coefficient
        if request.error_model == "linear_drift":
            factors = _drift_factors(design, errors)
            nonzero = factors != 0
            scale = coefficients[int(np.flatnonzero(nonzero)[0])] / factors[nonzero][0] if np.any(nonzero) else 0.
            if not np.allclose(coefficients, factors * scale, rtol=1e-12, atol=1e-14):
                raise ValueError("linear_drift scenarios require one signed slope over deposition layer order")
        if _finite(scenario.weight, "scenario weight") <= 0:
            raise ValueError("scenario weights must be positive")
    if request.robustness_objective != "linear_sensitivity_norm" and not scenarios:
        raise ValueError("finite-scenario objectives require explicit user-chosen scenarios")
    acceptable = _finite(request.acceptable_normalized_error, "acceptable_normalized_error")
    if acceptable < 0:
        raise ValueError("acceptable_normalized_error must be nonnegative")
    strict_upper = _sequence(request.strict_upper_bound_objective_indices,
                             "strict_upper_bound_objective_indices")
    if (any(type(i) is not int or not 0 <= i < len(objectives) for i in strict_upper)
            or len(set(strict_upper)) != len(strict_upper)
            or any(objectives[i].mode != "range" or objectives[i].upper_bound is None for i in strict_upper)):
        raise ValueError("strict_upper_bound_objective_indices must uniquely select range upper bounds")
    for label in ("max_iterations", "max_evaluations"):
        value = getattr(request, label)
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(f"{label} must be a positive integer or null")
    step = _finite(request.initial_step_fraction, "initial_step_fraction")
    minimum = _finite(request.min_step_fraction, "min_step_fraction")
    if not 0 < minimum < step <= 1:
        raise ValueError("step fractions must satisfy 0 < min_step < initial_step <= 1")
    resources = estimate_film_sensitivity_resources(replace(request, objectives=objectives,
        errors=errors, scenarios=scenarios))
    if resources["estimated_retained_result_bytes"] > MAX_RETAINED_RESULT_BYTES:
        raise ValueError("sensitivity result resource estimate "
            f"{resources['estimated_retained_result_bytes']} bytes exceeds explicit "
            f"{MAX_RETAINED_RESULT_BYTES}-byte retention limit; reduce requested samples/scenarios "
            "or split the analysis; no scenarios or conditions were dropped")
    if request.frozen_material_snapshot is not None:
        _checked_frozen_snapshot(request.frozen_material_snapshot, design)
    return replace(request, design_snapshot=design, objectives=objectives,
                   errors=errors, variables=variables, scenarios=scenarios,
                   strict_upper_bound_objective_indices=tuple(sorted(strict_upper)))


def sensitivity_request_from_optimization(
    base: FilmOptimizationRequest, *, errors: tuple[FilmThicknessError, ...],
    error_model: str, robustness_objective: str,
    scenarios: tuple[FilmSensitivityScenario, ...] = (), operation: str = "analyze",
    max_iterations: int | None = None, max_evaluations: int | None = None,
    initial_step_fraction: float = 0.25, min_step_fraction: float = 1e-5,
    strict_upper_bound_objective_indices: tuple[int, ...] = (),
    optimization_policy: str = "robustness_only",
    error_amplitude_semantics: str = "bounded_amplitude",
) -> FilmSensitivityRequest:
    """Explicit adapter; ordinary solver settings do not choose a robust objective.

    Only nominal design/targets/variables/acceptance and frozen materials carry
    over. Search budgets belong to this independent request and are explicit.
    """
    if not isinstance(base, FilmOptimizationRequest):
        raise ValueError("base must be a FilmOptimizationRequest")
    return _validate(FilmSensitivityRequest(deepcopy(base.design_snapshot), base.design_revision,
        tuple(base.objectives), tuple(errors), error_model, robustness_objective,
        tuple(base.variables), tuple(scenarios), operation, base.acceptable_normalized_error,
        max_iterations, max_evaluations, initial_step_fraction, min_step_fraction,
        deepcopy(base.frozen_material_snapshot), tuple(strict_upper_bound_objective_indices),
        optimization_policy, error_amplitude_semantics))


def freeze_film_sensitivity_request(service: Any, request: FilmSensitivityRequest) -> FilmSensitivityRequest:
    request = _validate(request)
    design = request.design_snapshot
    if request.frozen_material_snapshot is None:
        if service is None or not callable(getattr(service, "freeze_design", None)):
            raise ValueError("a complete frozen snapshot or a film design service is required")
        first = request.objectives[0]
        frozen = service.freeze_design(design, dict(wavelength_min_nm=first.wavelength_min_nm,
            wavelength_max_nm=first.wavelength_max_nm, samples=first.wavelength_samples,
            angle_deg=first.angles_deg[0], polarization=first.polarization, first_surface="front"))
    else:
        frozen = request.frozen_material_snapshot
    frozen = _checked_frozen_snapshot(frozen, design)
    return replace(request, design_snapshot=deepcopy(frozen["design"]), frozen_material_snapshot=frozen)


class _Cancelled(Exception):
    pass


class _Budget(Exception):
    pass


class _Evaluator:
    """Run-owned physical cache; no sharing across snapshots or request settings."""
    def __init__(self, request: FilmSensitivityRequest, cancel_event: Any):
        self.request, self.cancel = request, cancel_event
        assert request.frozen_material_snapshot is not None
        self.snapshot = request.frozen_material_snapshot
        self.materials = _PreparedMaterialData(self.snapshot, request.design_snapshot)
        self.cache: OrderedDict[bytes, FilmSensitivityAnalysis] = OrderedDict()
        self.cache_entry_bytes = estimate_film_sensitivity_resources(request)["estimated_analysis_bytes"]
        self.cache_bytes = 0
        self.evaluations = self.forward = self.jacobian = self.failures = 0
        self.constrained = request.operation == "optimize" and request.optimization_policy == "nominal_preserving"
        self.solver_key: bytes | None = None
        self.solver_data: dict[str, Any] | None = None
        # Includes the full frozen design/material digest, all objectives/error
        # definitions, scenario weights and nominal acceptance, never just layer IDs.
        encoded = json.dumps(film_sensitivity_request_to_payload(request), sort_keys=True,
                             ensure_ascii=False, allow_nan=False)
        self.identity = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise _Cancelled()

    def physical(self, design):
        return {layer["id"]: _physical_layer_thickness(layer, design,
                    self.snapshot) for layer in design["front_layers"]}

    def response(self, design, derivatives, variables=None):
        self.check()
        rows: list[float] = []
        jacobians: list[NDArray[np.float64]] = []
        conditions: list[tuple[int, float, float, FilmOptimizationObjective]] = []
        if variables is None:
            variables = tuple(FilmThicknessVariable("front", e.layer_id, 0., 1.)
                              for e in self.request.errors) if derivatives else ()
        for index, objective in enumerate(self.request.objectives):
            waves = np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm,
                                objective.wavelength_samples)
            for angle in objective.angles_deg:
                for chunk in _wavelength_chunks(waves, 4096):
                    self.check()
                    self.forward += 1
                    if derivatives:
                        self.jacobian += 1
                    response = simulate_with_thickness_jacobian(design, self.materials, variables,
                        dict(wavelength_min_nm=float(chunk[0]), wavelength_max_nm=float(chunk[-1]),
                             samples=len(chunk), angle_deg=angle,
                             polarization=objective.polarization, first_surface="front"))
                    values = np.asarray(response[objective.metric], dtype=float)
                    jac = np.asarray(response["jacobian_" + objective.metric], dtype=float)
                    if (values.shape != chunk.shape or jac.shape != (len(chunk), len(variables))
                            or not np.all(np.isfinite(values)) or not np.all(np.isfinite(jac))):
                        raise ValueError("physical sensitivity forward returned invalid values/Jacobian")
                    rows.extend(values)
                    jacobians.extend(jac)
                    conditions.extend((index, float(w), float(angle), objective) for w in chunk)
        self.check()
        return np.asarray(rows), np.asarray(jacobians), conditions

    def scores(self, values, conditions):
        errors = []
        for actual, (index, wave, angle, objective) in zip(values, conditions, strict=True):
            if objective.mode == "value":
                violation = abs(float(actual) - objective.target_value)
            else:
                violation = max(0., 0. if objective.lower_bound is None else objective.lower_bound - float(actual),
                                0. if objective.upper_bound is None else float(actual) - objective.upper_bound)
            normal, weighted = score_condition(violation, objective.normalization_scale, objective.weight)
            errors.append(FilmConditionError(index, wave, angle, objective.polarization,
                objective.metric, float(actual), objective.target_value, objective.lower_bound,
                objective.upper_bound, normal, weighted))
        maximum = max(row.normalized_violation for row in errors)
        rms = weighted_rms_error([row.normalized_violation for row in errors],
                                 [c[3].weight for c in conditions])
        return tuple(errors), maximum, rms

    def evaluate(self, design):
        self.check()
        thickness = self.physical(design)
        # Ordered complete front topology is fixed for the whole run. Include
        # all physical rows, not merely optimized or error-affected columns.
        key = np.asarray([thickness[r["id"]] for r in design["front_layers"]], dtype="<f8").tobytes()
        if key in self.cache and (not self.constrained or key == self.solver_key):
            self.cache.move_to_end(key)
            return self.cache[key]
        if self.request.max_evaluations is not None and self.evaluations >= self.request.max_evaluations:
            raise _Budget()
        self.evaluations += 1
        try:
            derivative_variables = None
            if self.constrained:
                identifiers = list(dict.fromkeys([e.layer_id for e in self.request.errors]
                    + [v.layer_id for v in self.request.variables]))
                derivative_variables = tuple(FilmThicknessVariable("front", identifier, 0., 1.)
                                             for identifier in identifiers)
            nominal, full_jac, conditions = self.response(design, True, derivative_variables)
            if self.constrained:
                error_columns = [identifiers.index(e.layer_id) for e in self.request.errors]
                nominal_jac = full_jac[:, [identifiers.index(v.layer_id) for v in self.request.variables]]
                jac = full_jac[:, error_columns]
            else:
                jac = full_jac
            errors, max_error, rms = self.scores(nominal, conditions)
            amplitudes = _error_amplitudes(self.request.errors, thickness)
            affected = jac * amplitudes
            linear = _linear_scale(affected, self.request)
            scales = np.asarray([c[3].normalization_scale for c in conditions])
            weights = np.asarray([c[3].weight for c in conditions])
            normalized = linear / scales
            norm = weighted_rms_error(normalized, weights)
            linear_conditions = tuple(FilmSensitivityCondition(i, w, a, o.polarization, o.metric,
                float(n), tuple(float(x) for x in j), tuple(float(x) for x in amplitudes),
                float(response_scale), float(normalized_scale)) for (i, w, a, o), n, j, response_scale, normalized_scale in
                zip(conditions, nominal, jac, linear, normalized, strict=True))
            finite = []
            scenario_jacobians = []
            worst_scores = []
            for scenario in self.request.scenarios:
                self.check()
                delta = amplitudes * np.asarray(scenario.coefficients)
                perturbed = deepcopy(design)
                for error, change in zip(self.request.errors, delta, strict=True):
                    layer = next(r for r in perturbed["front_layers"] if r["id"] == error.layer_id)
                    value = thickness[error.layer_id] + float(change)
                    if not math.isfinite(value) or value < 0:
                        raise ValueError(f"scenario {scenario.scenario_id} produces negative/nonfinite thickness for {error.layer_id}; errors are never clipped")
                    layer["thickness"] = {"value": value, "unit": "nm"}
                values, scenario_jac, _ = self.response(perturbed, self.constrained,
                    self.request.variables if self.constrained else None)
                if self.constrained:
                    chain = np.ones(len(self.request.variables))
                    error_indices = {error.layer_id: i for i, error in enumerate(self.request.errors)}
                    for column, variable in enumerate(self.request.variables):
                        if variable.layer_id in error_indices:
                            i = error_indices[variable.layer_id]
                            chain[column] += _error_components(self.request.errors[i])[1] * scenario.coefficients[i]
                    scenario_jacobians.append(scenario_jac * chain)
                scenario_errors, scenario_max, scenario_rms = self.scores(values, conditions)
                worst_scores.append(max(r.weighted_normalized_violation for r in scenario_errors))
                finite.append(FilmSensitivityScenarioResult(scenario.scenario_id, float(scenario.weight),
                    tuple(float(d) for d in delta), tuple(float(x) for x in nominal + jac @ delta),
                    tuple(float(x) for x in values), scenario_max, scenario_rms,
                    all(nominal_condition_pass(o, float(actual),
                        strict_upper=i in self.request.strict_upper_bound_objective_indices)
                        for (i, _w, _a, o), actual in zip(conditions, values, strict=True))))
            if self.request.robustness_objective == "linear_sensitivity_norm":
                robust = norm
            elif self.request.robustness_objective == "finite_scenario_worst":
                robust = max(worst_scores)
            else:
                robust = weighted_rms_error([s.finite_weighted_rms_error for s in finite],
                                            [s.weight for s in finite])
            result = FilmSensitivityAnalysis(max_error, rms,
                all(nominal_condition_pass(o, float(actual),
                    strict_upper=i in self.request.strict_upper_bound_objective_indices)
                    for (i, _w, _a, o), actual in zip(conditions, nominal, strict=True)), errors,
                linear_conditions, float(norm), tuple(finite), float(robust))
            if not math.isfinite(result.robustness_value):
                raise ValueError("robustness objective is not finite")
            if self.constrained:
                self.solver_key = key
                self.solver_data = dict(nominal=nominal, nominal_jac=nominal_jac,
                    scenario_values=np.asarray([row.finite_perturbation_values for row in finite]),
                    scenario_jac=np.asarray(scenario_jacobians), conditions=conditions)
        except (ValueError, ArithmeticError, FloatingPointError):
            self.failures += 1
            raise
        if self.cache_entry_bytes <= MAX_ANALYSIS_CACHE_BYTES:
            if key not in self.cache:
                self.cache_bytes += self.cache_entry_bytes
            self.cache[key] = result
            while len(self.cache) > 32 or self.cache_bytes > MAX_ANALYSIS_CACHE_BYTES:
                self.cache.popitem(last=False)
                self.cache_bytes -= self.cache_entry_bytes
        return result


def execute_film_sensitivity_request(service: Any, request: FilmSensitivityRequest, *,
    cancel_event: Any = None,
    progress_callback: Callable[[FilmSensitivityProgress], None] | None = None,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    candidate_observer: Any = None,
) -> FilmSensitivityResult:
    """Analyze, or minimize exactly the requested robustness objective.

    Invalid request contracts raise ValueError. Valid requests that encounter
    physical/data failure return state=failed; cancellation is never success.
    Iteration/evaluation limits return partial with a fully evaluated incumbent.
    The nominal target flag is unmodified and is not a robustness certificate.
    """
    request = _validate(request)
    if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
        raise ValueError("cancel_event must provide is_set()")
    iteration = 0
    evaluator = None
    baseline = best = None
    best_design = None
    values = None

    def emit(stage, state="running", message=""):
        if progress_callback is not None:
            progress_callback(FilmSensitivityProgress(stage, state, iteration,
                0 if evaluator is None else evaluator.evaluations,
                None if best is None else best.robustness_value, message))

    def make_result(state, reason, message="", is_final=True):
        thickness: dict[str, float] = {} if best_design is None or evaluator is None else evaluator.physical(best_design)
        result = FilmSensitivityResult(state, reason, request.operation, request.design_revision,
            request.robustness_objective, request.error_model, deepcopy(best_design), thickness,
            baseline, best, iteration, 0 if evaluator is None else evaluator.evaluations,
            0 if evaluator is None else evaluator.forward, 0 if evaluator is None else evaluator.jacobian,
            0 if evaluator is None else evaluator.failures, "" if evaluator is None else evaluator.identity,
            request, is_final=is_final, message=message,
            solver_acceptable=None if best is None else all(solver_condition_acceptable(
                request.objectives[row.objective_index], row.actual, request.acceptable_normalized_error,
                strict_upper=row.objective_index in request.strict_upper_bound_objective_indices)
                for row in best.nominal_conditions))
        return result

    def outcome(state, reason, message=""):
        result = make_result(state, reason, message)
        if request.operation == "optimize":
            finish_candidate_display(candidate_observer, state=state)
        emit("finished", state, message)
        return result

    def observe_candidate(design, analysis):
        # Runtime display is not part of the request, identity, or acceptance.
        # In particular, scenario responses are never normal candidates.
        if (request.operation != "optimize" or evaluator is None
                or type(candidate_observer) is not CandidateDisplayObserver):
            return
        record_candidate(candidate_observer, design,
            weighted_rms_error=analysis.nominal_weighted_rms_error,
            max_normalized_error=analysis.nominal_max_normalized_error,
            condition_rows=analysis.nominal_conditions,
            physical_thickness_nm=evaluator.physical(design),
            is_best=analysis is best, iterations=iteration, evaluations=evaluator.evaluations,
            selection={"robustness_value": analysis.robustness_value},
            source="sensitivity", sensitivity_objective=analysis.robustness_value,
            usable_candidate=analysis.nominal_targets_met)

    def checkpoint():
        if checkpoint_callback is not None:
            checkpoint_callback(film_sensitivity_result_to_payload(
                make_result("partial", "checkpoint", is_final=False)))

    try:
        emit("freezing")
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled()
        request = freeze_film_sensitivity_request(service, request)
        evaluator = _Evaluator(request, cancel_event)
        evaluator.check()
        initial_design = deepcopy(dict(request.design_snapshot))
        physical = evaluator.physical(initial_design)
        for v in request.variables:
            if not v.lower_nm <= physical[v.layer_id] <= _finite(v.upper_nm, "upper_nm"):
                raise ValueError(f"initial thickness for {v.layer_id} is outside explicit nominal bounds; no clamping was applied")
        emit("baseline")
        baseline = best = evaluator.evaluate(initial_design)
        best_design = initial_design
        observe_candidate(initial_design, baseline)
        if (request.operation == "optimize" and request.optimization_policy == "nominal_preserving"
                and not baseline.nominal_targets_met):
            emit("baseline_complete")
            return outcome("failed", "initial_nominal_infeasible",
                "Initial design fails the unchanged original nominal grid; no nominal-preserving search was started and no margin was added.")
        checkpoint()
        emit("baseline_complete")
        evaluator.check()
        if request.operation == "analyze":
            return outcome("completed", "analysis_complete")
        if request.optimization_policy == "nominal_preserving":
            from .film_sensitivity_constrained import run_nominal_preserving

            def accept_feasible(candidate, analysis):
                nonlocal best, best_design
                assert best is not None
                candidate_thickness = evaluator.physical(candidate)
                if any(not variable.lower_nm <= candidate_thickness[variable.layer_id] <= variable.upper_nm
                       for variable in request.variables):
                    raise ValueError("constrained candidate violates explicit nominal thickness bounds")
                if analysis.nominal_targets_met and analysis.robustness_value < best.robustness_value:
                    best, best_design = analysis, candidate
                    checkpoint()
                    emit("improved")
                observe_candidate(candidate, analysis)

            def constrained_iteration(_values):
                nonlocal iteration
                iteration += 1
                emit("iteration")
                evaluator.check()

            state, reason, message = run_nominal_preserving(request, evaluator, initial_design, baseline,
                accept_candidate=accept_feasible, iteration_callback=constrained_iteration, emit=emit,
                observe_candidate=observe_candidate)
            return outcome(state, reason, message)
        lows = np.asarray([v.lower_nm for v in request.variables], dtype=float)
        spans = np.asarray([_finite(v.upper_nm, "upper_nm") - v.lower_nm for v in request.variables])
        values = np.asarray([physical[v.layer_id] for v in request.variables])

        def probe(candidate_values):
            nonlocal best, best_design, values
            assert evaluator is not None and best is not None
            evaluator.check()
            candidate = deepcopy(initial_design)
            for v, x in zip(request.variables, candidate_values, strict=True):
                layer = next(r for r in candidate["front_layers"] if r["id"] == v.layer_id)
                layer["thickness"] = {"value": float(x), "unit": "nm"}
            if candidate.get("expression", {}).get("in_sync"):
                candidate["expression"]["in_sync"] = False
            try:
                analysis = evaluator.evaluate(candidate)
            except (ValueError, ArithmeticError, FloatingPointError) as exc:
                emit("candidate_failed", message=str(exc))
                return False
            # Never privilege a nominal target or add a hidden nominal penalty.
            # Nominal performance remains separately reported for user judgment.
            if analysis.robustness_value < best.robustness_value:
                best, best_design, values = analysis, candidate, candidate_values.copy()
                checkpoint()
                emit("improved")
                observe_candidate(candidate, analysis)
                return True
            observe_candidate(candidate, analysis)
            return False

        # A deterministic finite-domain initialization, followed by bounded
        # coordinate pattern search. The scalar is always the robustness choice.
        emit("search")
        for axis in range(len(values)):
            for fraction in np.linspace(0., 1., 17):
                candidate_values = values.copy()
                candidate_values[axis] = lows[axis] + fraction * spans[axis]
                probe(candidate_values)
        step = request.initial_step_fraction
        while step > request.min_step_fraction:
            evaluator.check()
            if request.max_iterations is not None and iteration >= request.max_iterations:
                return outcome("partial", "iteration_limit", "A fully evaluated incumbent is retained; convergence was not established.")
            iteration += 1
            changed = False
            for axis in range(len(values)):
                for direction in (-1., 1.):
                    candidate_values = values.copy()
                    candidate_values[axis] = np.clip(values[axis] + direction * step * spans[axis],
                                                     lows[axis], lows[axis] + spans[axis])
                    changed = probe(candidate_values) or changed
            if not changed:
                step *= 0.5
            emit("iteration")
        return outcome("completed", "step_tolerance", "Local robustness search converged to its requested step tolerance, not a global certificate.")
    except _Cancelled:
        return outcome("cancelled", "cancelled", "Cancelled; any retained incumbent was fully evaluated before cancellation.")
    except _Budget:
        return outcome("partial", "evaluation_limit", "Evaluation budget reached; any retained incumbent was fully evaluated.")
    except (ValueError, ArithmeticError, FloatingPointError, KeyError, TypeError) as exc:
        return outcome("failed", "physical_evaluation_failed", str(exc))


def analyze_film_sensitivity(service: Any, request: FilmSensitivityRequest, **kwargs) -> FilmSensitivityResult:
    if request.operation != "analyze":
        raise ValueError("analyze_film_sensitivity requires operation=analyze")
    return execute_film_sensitivity_request(service, request, **kwargs)


def optimize_film_sensitivity(service: Any, request: FilmSensitivityRequest, **kwargs) -> FilmSensitivityResult:
    if request.operation != "optimize":
        raise ValueError("optimize_film_sensitivity requires operation=optimize")
    return execute_film_sensitivity_request(service, request, **kwargs)


def _record(cls, value, label):
    if not isinstance(value, Mapping) or set(value) - {f.name for f in fields(cls)}:
        raise ValueError(f"{label} has unknown fields or is not an object")
    try:
        return cls(**dict(value))
    except TypeError as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc


def film_sensitivity_request_to_payload(request: FilmSensitivityRequest) -> dict[str, Any]:
    request = _validate(request)
    payload = dict(schema_version=SCHEMA_VERSION, kind=REQUEST_KIND, **asdict(request))
    # Empty optional operator flags retain the original v1 canonical identity.
    if not request.strict_upper_bound_objective_indices:
        payload.pop("strict_upper_bound_objective_indices")
    if request.optimization_policy == "robustness_only":
        payload.pop("optimization_policy")
    if request.error_amplitude_semantics == "bounded_amplitude":
        payload.pop("error_amplitude_semantics")
    for error in payload["errors"]:
        for field in ("absolute_nm", "relative_fraction"):
            if not error[field]:
                error.pop(field)
    try:
        # Return detached, JSON-native data and reject NaN/infinity everywhere.
        return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("sensitivity request must be finite JSON data") from exc


def _envelope(payload, cls, kind):
    if (not isinstance(payload, Mapping) or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != kind
            or set(payload) - {f.name for f in fields(cls)} - {"schema_version", "kind"}):
        raise ValueError(f"invalid {kind} schema/type or unknown fields")
    data = deepcopy(dict(payload))
    data.pop("schema_version")
    data.pop("kind")
    return data


def film_sensitivity_request_from_payload(payload: Mapping[str, Any]) -> FilmSensitivityRequest:
    data = _envelope(payload, FilmSensitivityRequest, REQUEST_KIND)
    try:
        data["objectives"] = tuple(replace(_record(FilmOptimizationObjective, row, "objective"),
            angles_deg=tuple(row.get("angles_deg", (0.,)))) for row in data["objectives"])
        data["errors"] = tuple(_record(FilmThicknessError, row, "thickness error") for row in data["errors"])
        data["variables"] = tuple(_record(FilmThicknessVariable, row, "variable") for row in data.get("variables", ()))
        data["scenarios"] = tuple(replace(_record(FilmSensitivityScenario, row, "scenario"),
            coefficients=tuple(row["coefficients"])) for row in data.get("scenarios", ()))
        return _validate(_record(FilmSensitivityRequest, data, "request"))
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid sensitivity request: {exc}") from exc


def _analysis_from_payload(payload):
    if payload is None:
        return None
    data = deepcopy(dict(payload)) if isinstance(payload, Mapping) else payload
    analysis = _record(FilmSensitivityAnalysis, data, "analysis")
    try:
        nominal = tuple(_record(FilmConditionError, row, "nominal condition") for row in analysis.nominal_conditions)
        linear = tuple(replace(_record(FilmSensitivityCondition, row, "linear condition"),
            derivative_per_nm=tuple(row["derivative_per_nm"]),
            error_amplitudes_nm=tuple(row["error_amplitudes_nm"])) for row in analysis.linear_conditions)
        scenarios = tuple(replace(_record(FilmSensitivityScenarioResult, row, "scenario result"),
            delta_thickness_nm=tuple(row["delta_thickness_nm"]),
            linear_estimated_values=tuple(row["linear_estimated_values"]),
            finite_perturbation_values=tuple(row["finite_perturbation_values"])) for row in analysis.finite_scenarios)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid sensitivity analysis: {exc}") from exc
    return replace(analysis, nominal_conditions=nominal, linear_conditions=linear, finite_scenarios=scenarios)


def film_sensitivity_result_to_payload(result: FilmSensitivityResult) -> dict[str, Any]:
    if not isinstance(result, FilmSensitivityResult):
        raise ValueError("result must be a FilmSensitivityResult")
    data = asdict(result)
    data["frozen_request"] = film_sensitivity_request_to_payload(result.frozen_request)
    payload = dict(schema_version=SCHEMA_VERSION, kind=RESULT_KIND, **data)
    try:
        payload = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("sensitivity result must be finite JSON data") from exc
    film_sensitivity_result_from_payload(payload)
    return payload


def film_sensitivity_result_from_payload(payload: Mapping[str, Any]) -> FilmSensitivityResult:
    data = _envelope(payload, FilmSensitivityResult, RESULT_KIND)
    has_solver_status = "solver_acceptable" in data
    try:
        data["frozen_request"] = film_sensitivity_request_from_payload(data["frozen_request"])
        data["baseline_analysis"] = _analysis_from_payload(data["baseline_analysis"])
        data["best_analysis"] = _analysis_from_payload(data["best_analysis"])
        data["limitations"] = tuple(data.get("limitations", LIMITATIONS))
        result = _record(FilmSensitivityResult, data, "result")
        json.dumps(asdict(result), ensure_ascii=False, allow_nan=False)
    except (TypeError, KeyError) as exc:
        raise ValueError(f"invalid sensitivity result: {exc}") from exc
    if type(result.is_final) is not bool:
        raise ValueError("is_final must be a boolean")
    if result.state not in ("completed", "partial", "cancelled", "failed"):
        raise ValueError("invalid sensitivity result state")
    request = result.frozen_request
    if (result.operation != request.operation or result.design_revision != request.design_revision
            or result.error_model != request.error_model or result.robustness_objective != request.robustness_objective):
        raise ValueError("sensitivity result identity differs from its request")
    for name in ("iterations", "evaluations", "forward_evaluations", "jacobian_evaluations", "failed_evaluations"):
        if type(getattr(result, name)) is not int or getattr(result, name) < 0:
            raise ValueError(f"result {name} must be a nonnegative integer")
    if (result.best_design is None) != (result.best_analysis is None):
        raise ValueError("best design and analysis must be present together")
    if result.state == "completed" and (result.best_analysis is None or result.baseline_analysis is None):
        raise ValueError("completed results require complete baseline and best analyses")
    if result.best_design is not None:
        validate_design(result.best_design)
    for analysis in (result.baseline_analysis, result.best_analysis):
        if analysis is None:
            continue
        if type(analysis.nominal_targets_met) is not bool:
            raise ValueError("nominal_targets_met must be a boolean")
        for name in ("nominal_max_normalized_error", "nominal_weighted_rms_error", "linear_sensitivity_norm", "robustness_value"):
            if _finite(getattr(analysis, name), name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if len(analysis.nominal_conditions) != len(analysis.linear_conditions) or not analysis.linear_conditions:
            raise ValueError("analysis condition counts differ or are empty")
        descriptors = tuple((index, float(wave), float(angle), objective)
            for index, objective in enumerate(request.objectives)
            for angle in objective.angles_deg
            for wave in np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm,
                                    objective.wavelength_samples))
        if len(analysis.linear_conditions) != len(descriptors):
            raise ValueError("analysis condition count does not match the request")
        for nominal, row, (index, wave, angle, objective) in zip(
                analysis.nominal_conditions, analysis.linear_conditions, descriptors, strict=True):
            identity = (index, wave, angle, objective.polarization, objective.metric)
            if (nominal.objective_index, nominal.wavelength_nm, nominal.angle_deg,
                    nominal.polarization, nominal.metric) != identity or (
                    row.objective_index, row.wavelength_nm, row.angle_deg,
                    row.polarization, row.metric) != identity:
                raise ValueError("analysis optical condition identity does not match the request")
            if len(row.derivative_per_nm) != len(request.errors) or len(row.error_amplitudes_nm) != len(request.errors):
                raise ValueError("analysis error columns do not match the request")
            for value in (row.nominal_value, *row.derivative_per_nm):
                _finite(value, "linear optical value/derivative")
            for value in (*row.error_amplitudes_nm, row.linear_response_scale, row.normalized_linear_response_scale,
                          nominal.normalized_violation, nominal.weighted_normalized_violation):
                if _finite(value, "analysis response scale/error") < 0:
                    raise ValueError("analysis response scales and errors must be nonnegative")
            _finite(nominal.actual, "nominal actual")
            if (nominal.target_value, nominal.lower_bound, nominal.upper_bound) != (
                    objective.target_value, objective.lower_bound, objective.upper_bound):
                raise ValueError("nominal condition target differs from the request")
        if analysis.nominal_targets_met != all(nominal_condition_pass(
                request.objectives[row.objective_index], row.actual,
                strict_upper=row.objective_index in request.strict_upper_bound_objective_indices)
                for row in analysis.nominal_conditions):
            raise ValueError("nominal target acceptance does not match original actual bounds")
        if len(analysis.finite_scenarios) != len(request.scenarios):
            raise ValueError("analysis scenario count does not match the request")
        for row, scenario in zip(analysis.finite_scenarios, request.scenarios, strict=True):
            if (row.scenario_id != scenario.scenario_id or row.weight != scenario.weight
                    or len(row.delta_thickness_nm) != len(request.errors)
                    or len(row.linear_estimated_values) != len(analysis.nominal_conditions)
                    or len(row.finite_perturbation_values) != len(analysis.nominal_conditions)):
                raise ValueError("scenario result identity or dimensions are invalid")
            if type(row.finite_targets_met) is not bool or row.finite_targets_met != (
                    all(nominal_condition_pass(request.objectives[nominal.objective_index], actual,
                        strict_upper=nominal.objective_index in request.strict_upper_bound_objective_indices)
                        for nominal, actual in zip(analysis.nominal_conditions,
                            row.finite_perturbation_values, strict=True))):
                raise ValueError("finite scenario acceptance does not match original actual bounds")
            for value in (*row.delta_thickness_nm, *row.linear_estimated_values, *row.finite_perturbation_values):
                _finite(value, "scenario result value")
            for value in (row.finite_max_normalized_error, row.finite_weighted_rms_error):
                if _finite(value, "finite scenario error") < 0:
                    raise ValueError("finite scenario errors must be nonnegative")
    expected_solver = None if result.best_analysis is None else all(solver_condition_acceptable(
        request.objectives[row.objective_index], row.actual, request.acceptable_normalized_error,
        strict_upper=row.objective_index in request.strict_upper_bound_objective_indices)
        for row in result.best_analysis.nominal_conditions)
    if not has_solver_status:
        result = replace(result, solver_acceptable=expected_solver)
    elif (result.solver_acceptable is not None and type(result.solver_acceptable) is not bool
          or result.solver_acceptable is not expected_solver):
        raise ValueError("solver_acceptable differs from actual conditions and unchanged request tolerance")
    return result


def _optical_invariants(design):
    """Allow only physical-thickness edits and expression synchronization loss."""
    result = deepcopy(dict(design))
    for layer in result["front_layers"]:
        layer.pop("thickness")
    # The display expression cannot remain authoritative after explicit edits.
    if isinstance(result.get("expression"), dict):
        result["expression"].pop("in_sync", None)
    return result


def _compare_replay(expected, actual, path="analysis"):
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        if set(expected) != set(actual):
            raise ValueError(f"{path} replay fields differ")
        for key in expected:
            _compare_replay(expected[key], actual[key], path + "." + key)
    elif isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            raise ValueError(f"{path} replay dimensions differ")
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            _compare_replay(left, right, path + f"[{index}]")
    elif isinstance(expected, bool) or isinstance(actual, bool):
        if type(expected) is not type(actual) or expected != actual:
            raise ValueError(f"{path} replay flag differs")
    elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if not math.isclose(_finite(expected, path), _finite(actual, path), rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError(f"{path} physical replay differs")
    elif expected != actual:
        raise ValueError(f"{path} replay value differs")


def validate_film_sensitivity_result_payload(payload: Mapping[str, Any], *,
    expected_request: FilmSensitivityRequest | Mapping[str, Any] | None = None,
    replay: bool = True,
) -> dict[str, Any]:
    """Verify checkpoint/result identity, permitted edits and optional physics.

    Queue listing may use replay=False after its own trusted envelope check.
    Before application/recovery use replay=True with the queue's exact frozen
    expected_request. No external catalog, solver search, or state changes occur.
    """
    if type(replay) is not bool:
        raise ValueError("replay must be a boolean")
    result = film_sensitivity_result_from_payload(payload)
    request = result.frozen_request
    if expected_request is not None:
        expected = (film_sensitivity_request_from_payload(expected_request)
                    if isinstance(expected_request, Mapping) else _validate(expected_request))
        if film_sensitivity_request_to_payload(expected) != film_sensitivity_request_to_payload(request):
            raise ValueError("sensitivity result frozen request identity does not match expected_request")
    if result.forward_evaluations < result.jacobian_evaluations or result.failed_evaluations > result.evaluations:
        raise ValueError("sensitivity result work counters are inconsistent")
    if not result.is_final and (result.state != "partial" or result.termination_reason != "checkpoint"):
        raise ValueError("nonfinal results must be partial checkpoints")
    expected_reason = ("analysis_complete" if result.operation == "analyze" else
        "constrained_tolerance" if request.optimization_policy == "nominal_preserving" else "step_tolerance")
    if result.state == "completed" and result.termination_reason != expected_reason:
        raise ValueError("completed sensitivity result has an invalid termination reason")
    if result.state == "cancelled" and result.termination_reason not in (
            "cancelled", "scheduler_cancel_won", "scored_checkpoint_recovered"):
        raise ValueError("cancelled result has an invalid termination reason")
    if result.best_design is None:
        if result.best_thickness_nm or result.baseline_analysis is not None:
            raise ValueError("empty results cannot claim thicknesses or a completed baseline")
        return film_sensitivity_result_to_payload(result)
    assert result.best_analysis is not None
    if request.frozen_material_snapshot is None:
        raise ValueError("a scored result requires complete frozen materials")
    if _optical_invariants(result.best_design) != _optical_invariants(request.design_snapshot):
        raise ValueError("sensitivity result changed layer IDs, topology, material references or optical invariants")
    evaluator = _Evaluator(request, None)
    if result.cache_identity != evaluator.identity:
        raise ValueError("sensitivity result cache identity does not match the full frozen request")
    initial = evaluator.physical(request.design_snapshot)
    physical = evaluator.physical(result.best_design)
    _compare_replay(result.best_thickness_nm, physical, "best_thickness_nm")
    selected = {v.layer_id: v for v in request.variables}
    for identifier, value in physical.items():
        if request.operation == "analyze" or identifier not in selected:
            if not math.isclose(value, initial[identifier], rel_tol=0., abs_tol=1e-12):
                raise ValueError("sensitivity result changed an unselected or analysis-only thickness")
        elif not selected[identifier].lower_nm <= value <= _finite(selected[identifier].upper_nm, "upper_nm"):
            raise ValueError("sensitivity result violates explicit nominal thickness bounds")
    if result.baseline_analysis is None:
        raise ValueError("a scored result must retain its baseline analysis")
    best_analysis = result.best_analysis
    if best_analysis is None:
        raise ValueError("a scored result must retain its best analysis")
    if best_analysis.robustness_value > result.baseline_analysis.robustness_value:
        raise ValueError("best robustness score is worse than its retained baseline")
    if request.operation == "optimize" and request.optimization_policy == "nominal_preserving":
        if result.baseline_analysis.nominal_targets_met:
            if not best_analysis.nominal_targets_met:
                raise ValueError("nominal-preserving result has a nominal-infeasible incumbent")
        elif (result.state != "failed" or result.termination_reason != "initial_nominal_infeasible"
              or physical != initial):
            raise ValueError("nominal-preserving search requires an initially feasible original nominal grid")
    if replay:
        # Replay must not consume or be blocked by the original search budget.
        evaluator.request = replace(request, max_evaluations=None)
        baseline = evaluator.evaluate(request.design_snapshot)
        best = evaluator.evaluate(result.best_design)
        _compare_replay(asdict(result.baseline_analysis), asdict(baseline), "baseline_analysis")
        _compare_replay(asdict(best_analysis), asdict(best), "best_analysis")
    return film_sensitivity_result_to_payload(result)
