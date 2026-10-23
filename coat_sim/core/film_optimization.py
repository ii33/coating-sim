"""Qt-independent contracts and bounded optimization for thin-film designs.

The optimizer works on a captured design mapping and never writes back to an
editing session. See ``docs/film-optimization.md`` for the worker contract.
"""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from typing import Any, Callable, Literal, Mapping

import numpy as np

from .film_design import physical_thickness_nm
from .optical_constants import prepare_optical_set
from .film_nominal_acceptance import nominal_condition_pass
from .film_optimization_scoring import (candidate_key, score_condition, search_scalar,
                                        weighted_rms_error, weighted_scaled_value)


Polarization = Literal["s", "p", "unpolarized"]
OpticalMetric = Literal["R", "T"]
ObjectiveMode = Literal["value", "range"]
OptimizationState = Literal["completed", "cancelled", "failed"]


@dataclass(frozen=True)
class FilmThicknessVariable:
    """One selected stable film-layer ID and its inclusive thickness bounds."""

    side: Literal["front", "back"]
    layer_id: str
    lower_nm: float
    upper_nm: float | None


@dataclass(frozen=True)
class FilmOptimizationObjective:
    """A per-condition optical target over a uniformly sampled wavelength band."""

    wavelength_min_nm: float
    wavelength_max_nm: float
    wavelength_samples: int
    angles_deg: tuple[float, ...] = (0.0,)
    polarization: Polarization = "unpolarized"
    metric: OpticalMetric = "R"
    mode: ObjectiveMode = "value"
    target_value: float | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    weight: float = 1.0
    normalization_scale: float = 1.0


@dataclass(frozen=True)
class FilmOptimizationRequest:
    """Immutable input contract; design_snapshot is copied before evaluation."""

    design_snapshot: Mapping[str, Any]
    design_revision: int
    variables: tuple[FilmThicknessVariable, ...]
    objectives: tuple[FilmOptimizationObjective, ...]
    max_iterations: int = 200
    max_evaluations: int = 1000
    initial_step_fraction: float = 0.25
    min_step_fraction: float = 1e-4
    acceptable_normalized_error: float = 1e-3
    frozen_material_snapshot: Mapping[str, Any] | None = None
    # Appended defaults preserve the original positional request contract.
    solver: str = "baseline"
    seed: int = 0
    multistart_count: int = 4
    population_size: int = 12
    validation_samples_multiplier: int = 2
    execution_mode: str = "serial"
    batch_size: int = 16
    execution_device: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FilmOptimizationProgress:
    state: Literal["validating", "running", "completed", "cancelled", "failed"]
    fraction: float
    iteration: int
    evaluations: int
    best_max_normalized_error: float | None
    message: str = ""


@dataclass(frozen=True)
class FilmConditionError:
    objective_index: int
    wavelength_nm: float
    angle_deg: float
    polarization: Polarization
    metric: OpticalMetric
    actual: float
    target_value: float | None
    lower_bound: float | None
    upper_bound: float | None
    normalized_violation: float
    weighted_normalized_violation: float


@dataclass(frozen=True)
class FilmOptimizationResult:
    state: OptimizationState
    design_revision: int
    best_design: Mapping[str, Any]
    best_thickness_nm: Mapping[str, float]
    max_normalized_error: float
    weighted_max_normalized_error: float
    weighted_rms_error: float
    targets_met: bool
    per_condition_errors: tuple[FilmConditionError, ...]
    iterations: int
    evaluations: int
    message: str = ""
    # Method and validation metadata are additive; the original scores and
    # ``targets_met`` continue to describe the requested training grid.
    solver: str = "baseline"
    seed: int = 0
    termination_reason: str = ""
    failed_evaluations: int = 0
    validation_samples_multiplier: int = 2
    validation_evaluations: int = 0
    validation_complete: bool = False
    validation_max_normalized_error: float | None = None
    validation_weighted_rms_error: float | None = None
    validation_targets_met: bool | None = None
    validation_per_condition_errors: tuple[FilmConditionError, ...] = ()
    validation_message: str = ""


def _freeze_settings(request: FilmOptimizationRequest, design: Mapping[str, Any]) -> dict[str, Any]:
    if not request.objectives:
        raise ValueError("at least one optical objective is required before freezing materials")
    first = request.objectives[0]
    if not isinstance(first, FilmOptimizationObjective):
        raise ValueError("objectives must contain FilmOptimizationObjective values")
    if not isinstance(first.angles_deg, (tuple, list)) or not first.angles_deg:
        raise ValueError("the first objective needs at least one angle sample")
    return {
        "wavelength_min_nm": first.wavelength_min_nm,
        "wavelength_max_nm": first.wavelength_max_nm,
        "samples": first.wavelength_samples,
        "angle_deg": first.angles_deg[0],
        "polarization": first.polarization,
        "first_surface": design.get("first_surface", "front"),
    }


def _checked_frozen_snapshot(snapshot: Mapping[str, Any], design: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise ValueError("frozen_material_snapshot must be a mapping")
    payload = deepcopy(dict(snapshot))
    if (payload.get("schema_version") != 1 or
            payload.get("kind") != "coat_sim.frozen_film_design"):
        raise ValueError("frozen material snapshot version/type is invalid")
    digest = payload.get("sha256")
    original = {key: value for key, value in payload.items() if key != "sha256"}
    try:
        identity = hashlib.sha256(json.dumps(
            original, sort_keys=True, ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ValueError("frozen material snapshot is not valid JSON data") from exc
    if digest != identity:
        raise ValueError("frozen material snapshot digest does not match")
    from .film_design import validate_design
    frozen_design = validate_design(payload.get("design"))
    if frozen_design != design:
        raise ValueError("frozen material snapshot does not match design_snapshot")
    materials = payload.get("materials")
    if not isinstance(materials, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("id"), str)
            for row in materials):
        raise ValueError("frozen material snapshot list is invalid")
    if len({row["id"] for row in materials}) != len(materials):
        raise ValueError("frozen material snapshot contains duplicate material IDs")
    return payload


def freeze_film_optimization_request(
    service: Any,
    request: FilmOptimizationRequest,
) -> FilmOptimizationRequest:
    """Freeze the complete design/material input before queue submission.

    Call this on the submit path. The returned request can be serialized with
    its ``frozen_material_snapshot`` and passed to a later worker; the worker
    verifies and reuses that payload without rescanning the live catalog.
    """
    if not isinstance(request, FilmOptimizationRequest):
        raise ValueError("request must be a FilmOptimizationRequest")
    objectives = []
    for item in request.objectives:
        if (isinstance(item, FilmOptimizationObjective) and
                item.wavelength_min_nm == item.wavelength_max_nm):
            if type(item.wavelength_samples) is not int or item.wavelength_samples < 1:
                raise ValueError("wavelength_samples must be a positive integer")
            item = replace(item, wavelength_samples=1)
        objectives.append(item)
    objectives = tuple(objectives)
    if objectives != request.objectives:
        request = replace(request, objectives=objectives)
    from .film_design import validate_design
    design = validate_design(deepcopy(dict(request.design_snapshot)))
    if request.frozen_material_snapshot is not None:
        payload = _checked_frozen_snapshot(request.frozen_material_snapshot, design)
    else:
        payload = service.freeze_design(design, _freeze_settings(request, design))
        payload = _checked_frozen_snapshot(payload, design)
    return replace(request, design_snapshot=deepcopy(payload["design"]),
                   frozen_material_snapshot=payload,
                   execution_device=deepcopy(request.execution_device))


def _validate_execution_controls(request: FilmOptimizationRequest) -> None:
    if request.execution_mode not in ("serial", "deferred_batch"):
        raise ValueError("execution_mode must be serial or deferred_batch")
    if request.execution_mode == "deferred_batch" and request.solver != "differential_evolution":
        raise ValueError("deferred_batch requires differential_evolution")
    if type(request.batch_size) is not int or request.batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    if request.execution_device is not None:
        from .film_xpu_execution import validate_request_domain
        validate_request_domain(request)


def _validate_optimization_controls(request: FilmOptimizationRequest) -> dict[str, Any]:
    """Validate shared controls for thickness and needle optimization."""
    def finite_control(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{label} must be finite")
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{label} must be finite") from exc
        if not math.isfinite(result):
            raise ValueError(f"{label} must be finite")
        return result

    def positive_control(value: Any, label: str) -> int:
        if type(value) is not int or value < 1:
            raise ValueError(f"{label} must be a positive integer")
        return value

    if not isinstance(request, FilmOptimizationRequest):
        raise ValueError("request must be a FilmOptimizationRequest")
    _validate_execution_controls(request)
    max_iterations = positive_control(request.max_iterations, "max_iterations")
    max_evaluations = positive_control(request.max_evaluations, "max_evaluations")
    solver = request.solver
    supported = {"baseline", "powell", "nelder_mead", "multistart",
                 "differential_evolution", "simulated_annealing", "trf", "lbfgsb"}
    if not isinstance(solver, str) or solver not in supported:
        raise ValueError(f"solver must be one of {', '.join(sorted(supported))}")
    if type(request.seed) is not int or not 0 <= request.seed <= 0xFFFFFFFF:
        raise ValueError("seed must be an integer from 0 to 4294967295")
    multistart_count = positive_control(request.multistart_count, "multistart_count")
    population_size = positive_control(request.population_size, "population_size")
    if solver == "differential_evolution" and population_size < 5:
        raise ValueError("population_size must be at least 5 for differential_evolution")
    validation_multiplier = positive_control(
        request.validation_samples_multiplier, "validation_samples_multiplier")
    initial_step = finite_control(request.initial_step_fraction, "initial_step_fraction")
    min_step = finite_control(request.min_step_fraction, "min_step_fraction")
    acceptable = finite_control(request.acceptable_normalized_error,
                                "acceptable_normalized_error")
    if not 0 < min_step < initial_step <= 1:
        raise ValueError("step fractions must satisfy 0 < min_step < initial_step <= 1")
    if acceptable < 0:
        raise ValueError("acceptable_normalized_error must be nonnegative")
    return {"max_iterations": max_iterations, "max_evaluations": max_evaluations,
            "solver": solver, "seed": request.seed, "multistart_count": multistart_count,
            "population_size": population_size,
            "validation_multiplier": validation_multiplier, "initial_step": initial_step,
            "min_step": min_step, "acceptable": acceptable}


def _wavelength_chunks(waves: np.ndarray, maximum_samples: int = 10001):
    """Slice one global grid into adjacent, nonoverlapping service calls."""
    for start in range(0, len(waves), maximum_samples):
        yield waves[start:start + maximum_samples]


def optimize_film_design(
    service: Any,
    request: FilmOptimizationRequest,
    *,
    cancel_event: Any = None,
    progress_callback: Callable[[FilmOptimizationProgress], None] | None = None,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    performance_options: Any = None,
    performance_callback: Callable[[dict[str, Any]], None] | None = None,
    candidate_observer: Any = None,
) -> FilmOptimizationResult:
    """Optimize only requested layer thicknesses through the film service.

    ``service`` is a ``FilmDesignService``-compatible instance. Cancellation
    accepts any object with ``is_set()`` (including the desktop queue event).
    Progress is delivered synchronously from the worker thread.
    ``checkpoint_callback`` receives only a new incumbent, after its complete
    evaluation, so an external durable queue can retain a validated partial.
    """
    def finite(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{label} must be finite")
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{label} must be finite") from exc
        if not math.isfinite(result):
            raise ValueError(f"{label} must be finite")
        return result

    def positive_int(value: Any, label: str) -> int:
        if type(value) is not int or value < 1:
            raise ValueError(f"{label} must be a positive integer")
        return value

    def emit(state: str, fraction: float, iteration: int, evaluations: int,
             best_error: float | None, message: str = "") -> None:
        if progress_callback is not None:
            progress_callback(FilmOptimizationProgress(
                state=state, fraction=min(1.0, max(0.0, float(fraction))),
                iteration=iteration, evaluations=evaluations,
                best_max_normalized_error=best_error, message=message))

    if not isinstance(request, FilmOptimizationRequest):
        raise ValueError("request must be a FilmOptimizationRequest")
    if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
        raise ValueError("cancel_event must provide is_set()")
    if not isinstance(request.design_snapshot, Mapping):
        raise ValueError("design_snapshot must be a design mapping")
    if type(request.design_revision) is not int or request.design_revision < 0:
        raise ValueError("design_revision must be a nonnegative integer")
    if not isinstance(request.variables, (tuple, list)) or not request.variables:
        raise ValueError("at least one selected film thickness variable is required")
    if not isinstance(request.objectives, (tuple, list)) or not request.objectives:
        raise ValueError("at least one optical objective is required")
    controls = _validate_optimization_controls(request)
    max_iterations = controls["max_iterations"]
    max_evaluations = controls["max_evaluations"]
    solver = controls["solver"]
    seed = controls["seed"]
    multistart_count = controls["multistart_count"]
    population_size = controls["population_size"]
    validation_multiplier = controls["validation_multiplier"]
    initial_step = controls["initial_step"]
    min_step = controls["min_step"]
    acceptable = controls["acceptable"]

    # Validate and normalize all fields before creating a worker or evaluating.
    from .film_design import validate_design
    base_design = validate_design(deepcopy(dict(request.design_snapshot)))
    if solver in {"trf", "lbfgsb"}:
        if base_design["substrate"]["sides"] != "single":
            raise ValueError(f"{solver} supports only single-sided substrate designs")
        if base_design.get("first_surface", "front") != "front":
            raise ValueError(f"{solver} supports only the front first surface")
        if any(getattr(variable, "side", None) != "front"
               for variable in request.variables):
            raise ValueError(f"{solver} supports only front-side thickness variables")
    variable_rows: list[tuple[str, str, float, float, float]] = []
    seen_ids: set[str] = set()
    for variable in request.variables:
        if not isinstance(variable, FilmThicknessVariable):
            raise ValueError("variables must contain FilmThicknessVariable values")
        if variable.side not in ("front", "back"):
            raise ValueError("variable side must be front or back")
        if not isinstance(variable.layer_id, str) or not variable.layer_id:
            raise ValueError("variable layer_id must be a nonempty stable layer id")
        if variable.layer_id in seen_ids:
            raise ValueError("each layer id may be optimized only once")
        seen_ids.add(variable.layer_id)
        row = next((item for item in base_design[variable.side + "_layers"]
                    if item["id"] == variable.layer_id), None)
        if row is None:
            raise ValueError(f"selected {variable.side} layer id is not present: {variable.layer_id}")
        if variable.side == "back" and base_design["substrate"]["sides"] != "double":
            raise ValueError("back-layer variables require a double-sided substrate design")
        low = finite(variable.lower_nm, "lower_nm")
        if variable.upper_nm is None:
            if solver != "baseline":
                raise ValueError("this finite-domain optimizer requires an explicit upper_nm for each selected layer; no upper bound was assumed")
            high = math.inf
        else:
            high = finite(variable.upper_nm, "upper_nm")
        if low < 0 or high <= low:
            raise ValueError("thickness bounds must be nonnegative and strictly increasing")
        variable_rows.append((variable.side, variable.layer_id, low, high, 0.0))
    objectives: list[FilmOptimizationObjective] = []
    for objective in request.objectives:
        if not isinstance(objective, FilmOptimizationObjective):
            raise ValueError("objectives must contain FilmOptimizationObjective values")
        low = finite(objective.wavelength_min_nm, "wavelength_min_nm")
        high = finite(objective.wavelength_max_nm, "wavelength_max_nm")
        if low <= 0 or high < low:
            raise ValueError(
                "objective wavelength band must be positive and ordered")
        samples = positive_int(objective.wavelength_samples, "wavelength_samples")
        if high == low:
            samples = 1
        elif samples < 2:
            raise ValueError("wavelength_samples must be at least 2")
        if not isinstance(objective.angles_deg, (tuple, list)) or not objective.angles_deg:
            raise ValueError("each objective needs at least one angle sample")
        angles = tuple(finite(angle, "angle_deg") for angle in objective.angles_deg)
        if any(angle < 0 or angle >= 90 for angle in angles):
            raise ValueError("angles_deg samples must be in [0, 90)")
        if len(set(angles)) != len(angles):
            raise ValueError("angles_deg must not contain duplicate samples")
        if objective.polarization not in ("s", "p", "unpolarized"):
            raise ValueError("polarization must be s, p or unpolarized")
        if objective.metric not in ("R", "T"):
            raise ValueError("metric must be R or T")
        if objective.mode not in ("value", "range"):
            raise ValueError("objective mode must be value or range")
        weight = finite(objective.weight, "weight")
        scale = finite(objective.normalization_scale, "normalization_scale")
        if weight <= 0 or scale <= 0:
            raise ValueError("objective weight and normalization_scale must be positive")
        if objective.mode == "value":
            if objective.target_value is None or objective.lower_bound is not None or objective.upper_bound is not None:
                raise ValueError("value objectives require target_value and no bounds")
            target = finite(objective.target_value, "target_value")
            if not 0 <= target <= 1:
                raise ValueError("R/T target_value must be in [0, 1]")
            normalized = FilmOptimizationObjective(
                low, high, samples, angles, objective.polarization, objective.metric,
                "value", target, None, None, weight, scale)
        else:
            if objective.target_value is not None or (objective.lower_bound is None and objective.upper_bound is None):
                raise ValueError("range objectives require one or both bounds and no target_value")
            lower = (None if objective.lower_bound is None else
                     finite(objective.lower_bound, "lower_bound"))
            upper = (None if objective.upper_bound is None else
                     finite(objective.upper_bound, "upper_bound"))
            if ((lower is not None and not 0 <= lower <= 1) or
                    (upper is not None and not 0 <= upper <= 1) or
                    (lower is not None and upper is not None and lower > upper)):
                raise ValueError("R/T range bounds must lie in [0, 1] and be ordered")
            normalized = FilmOptimizationObjective(
                low, high, samples, angles, objective.polarization, objective.metric,
                "range", None, lower, upper, weight, scale)
        objectives.append(normalized)

    emit("validating", 0.0, 0, 0, None, "validating design, variables, targets and material data")
    first = objectives[0]
    if request.frozen_material_snapshot is None:
        frozen = service.freeze_design(base_design, _freeze_settings(request, base_design))
        frozen = _checked_frozen_snapshot(frozen, base_design)
    else:
        frozen = _checked_frozen_snapshot(request.frozen_material_snapshot, base_design)
    base_design = deepcopy(frozen["design"])

    class FrozenMaterials:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = {row["id"]: deepcopy(row) for row in rows}

        def get(self, material_id: str) -> dict[str, Any]:
            if material_id not in self._rows:
                raise ValueError(f"frozen film optimization materials missing: {material_id}")
            return deepcopy(self._rows[material_id])

    # Run against a private material snapshot so catalog edits during work cannot
    # change candidate scores and the editing session/service remain untouched.
    worker = copy(service)
    worker.materials = FrozenMaterials(frozen["materials"])
    frozen_by_id = {row["id"]: row for row in frozen["materials"]}
    normalized_variables: list[tuple[str, str, float, float, float]] = []
    for side, layer_id, low, high, _ in variable_rows:
        row = next(item for item in base_design[side + "_layers"] if item["id"] == layer_id)
        if row["thickness"]["unit"] == "qw":
            material = frozen_by_id.get(row["material_id"])
            optical = next((item for item in (material or {}).get("optical_sets", [])
                            if item["id"] == row["optical_set_id"]), None)
            if optical is None:
                raise ValueError(f"selected layer optical data set is missing: {layer_id}")
            n_value, _k, _details = prepare_optical_set(optical)(
                base_design["reference_wavelength_nm"], "nm", include_k=False)
            original_nm = physical_thickness_nm(
                row, base_design["reference_wavelength_nm"], float(n_value))
        else:
            original_nm = physical_thickness_nm(
                row, base_design["reference_wavelength_nm"], 1.0)
        if not math.isfinite(original_nm) or original_nm < 0:
            raise ValueError(f"selected layer has invalid physical thickness: {layer_id}")
        normalized_variables.append((side, layer_id, low, high, original_nm))

    def candidate_design(values: np.ndarray) -> dict[str, Any]:
        candidate = deepcopy(base_design)
        for value, (side, layer_id, _low, _high, _initial) in zip(values, normalized_variables):
            layer = next(item for item in candidate[side + "_layers"]
                         if item["id"] == layer_id)
            layer["thickness"] = {"value": float(value), "unit": "nm"}
        expression = candidate.get("expression", {})
        if expression.get("text") and expression.get("in_sync"):
            expression["in_sync"] = False
        return candidate

    from .film_optimization_scoring import PreparedConditionScore, PreparedRmsWeights
    prepared_scores = {}

    class _ValidationCancelled(Exception):
        pass

    strict_forward = None

    def evaluate(
        values: np.ndarray,
        evaluation_objectives: list[FilmOptimizationObjective] | None = None,
        *,
        check_cancellation: bool = False,
        supplied_responses: Any = None,
        include_details: bool = True,
    ) -> dict[str, Any]:
        candidate = candidate_design(values)
        if strict_forward is not None and supplied_responses is None and evaluation_objectives is None:
            response = strict_forward.responses(np.asarray(values).reshape(1, -1))
            supplied_responses = strict_forward.candidate_responses(response, 0)
        rows_to_evaluate = objectives if evaluation_objectives is None else evaluation_objectives
        score_key = tuple(rows_to_evaluate)
        if score_key not in prepared_scores:
            prepared_scores[score_key] = (
                tuple(PreparedConditionScore(o.normalization_scale, o.weight) for o in rows_to_evaluate),
                PreparedRmsWeights(o.weight for o in rows_to_evaluate
                                   for _ in range(o.wavelength_samples * len(o.angles_deg))))
        point_scorers, rms_scorer = prepared_scores[score_key]
        errors: list[FilmConditionError] = []
        normalized_errors: list[float] = []
        max_error = 0.0
        weighted_max_error = 0.0
        original_nominal_pass = True
        for objective_index, objective in enumerate(rows_to_evaluate):
            waves = np.linspace(objective.wavelength_min_nm,
                                objective.wavelength_max_nm,
                                objective.wavelength_samples)
            for angle in objective.angles_deg:
                # Evaluate each point from the global grid exactly once.
                for chunk_waves in _wavelength_chunks(waves):
                    if check_cancellation and cancelled():
                        raise _ValidationCancelled()
                    settings = {
                        "wavelength_min_nm": float(chunk_waves[0]),
                        "wavelength_max_nm": float(chunk_waves[-1]),
                        "samples": int(len(chunk_waves)),
                        "polarization": objective.polarization,
                        "first_surface": candidate.get("first_surface", "front"),
                    }
                    response = (worker.simulate(candidate, {**settings, "angle_deg": angle})
                                if supplied_responses is None else next(supplied_responses))
                    actual_values = np.asarray(response[objective.metric], dtype=float)
                    actual_waves = np.asarray(response["wavelength_nm"], dtype=float)
                    if (actual_values.shape != chunk_waves.shape or
                            actual_waves.shape != chunk_waves.shape or
                            not np.all(np.isfinite(actual_values)) or
                            not np.allclose(actual_waves, chunk_waves, rtol=0, atol=1e-9)):
                        raise ValueError("film simulation returned invalid objective samples")
                    for wavelength, actual in zip(chunk_waves, actual_values):
                        if display_physical is not None:
                            original_nominal_pass &= nominal_condition_pass(objective, float(actual))
                        if objective.mode == "value":
                            violation = abs(float(actual) - float(objective.target_value))
                        else:
                            violation = max(
                                0.0,
                                (float(objective.lower_bound) - float(actual)
                                 if objective.lower_bound is not None else 0.0),
                                (float(actual) - float(objective.upper_bound)
                                 if objective.upper_bound is not None else 0.0),
                            )
                        normalized_error, weighted_error = point_scorers[objective_index](violation)
                        max_error = max(max_error, normalized_error)
                        weighted_max_error = max(weighted_max_error, weighted_error)
                        normalized_errors.append(normalized_error)
                        if include_details:
                            errors.append(FilmConditionError(
                                objective_index=objective_index,
                                wavelength_nm=float(wavelength), angle_deg=float(angle),
                                polarization=objective.polarization, metric=objective.metric,
                                actual=float(actual), target_value=objective.target_value,
                                lower_bound=objective.lower_bound, upper_bound=objective.upper_bound,
                                normalized_violation=normalized_error,
                                weighted_normalized_violation=weighted_error,
                            ))
        if check_cancellation and cancelled():
            raise _ValidationCancelled()
        weighted_rms = rms_scorer(normalized_errors)
        return {
            "values": np.asarray(values, dtype=float), "design": candidate,
            "errors": tuple(errors), "max_error": max_error,
            "weighted_max_error": weighted_max_error,
            "weighted_rms_error": weighted_rms,
            "nominal_pass": bool(original_nominal_pass),
            "condition_count": len(normalized_errors),
            "key": candidate_key(max_error, weighted_rms, acceptable),
        }

    initial = np.asarray([
        min(high, max(low, current)) for _side, _id, low, high, current in normalized_variables
    ], dtype=float)
    lows = np.asarray([item[2] for item in normalized_variables], dtype=float)
    highs = np.asarray([item[3] for item in normalized_variables], dtype=float)
    # Only finite-domain solvers normalize by spans. The baseline's half-unbounded
    # path below works in physical nm and never sends infinity to a candidate.
    has_unbounded_axis = bool(np.any(np.isinf(highs)))
    spans = np.where(np.isfinite(highs), highs - lows, 0.0)
    best: dict[str, Any] | None = None
    evaluations = 0
    failed_evaluations = 0
    iteration = 0
    cancel_after_evaluation = False
    from .film_candidate_display import (
        CandidateDisplayObserver, finish_candidate_display, physical_layer_thicknesses, record_candidate,
        refresh_candidate_display,
    )
    display_physical = (physical_layer_thicknesses(base_design, frozen)
                        if type(candidate_observer) is CandidateDisplayObserver else None)

    def display_candidate(current, *, is_best=False, is_current=True, publish=True):
        if display_physical is None:
            return
        physical = dict(display_physical)
        for value, (side, identifier, _low, _high, _current) in zip(current["values"], normalized_variables, strict=True):
            physical[(side + "_layers", identifier)] = float(value)
        record_candidate(candidate_observer, current["design"],
            weighted_rms_error=current["weighted_rms_error"], max_normalized_error=current["max_error"],
            condition_rows=current["errors"], physical_thickness_nm=physical,
            is_best=is_best, is_current=is_current, iterations=iteration, evaluations=evaluations,
            source="film_optimization", scope="training", publish=publish)

    def cancelled() -> bool:
        return cancel_event is not None and callable(getattr(cancel_event, "is_set", None)) and cancel_event.is_set()

    def archive(current: dict[str, Any], *, is_current=True, publish_display=True) -> None:
        nonlocal best
        incumbent_changed = False
        if best is None:
            best = current
            incumbent_changed = True
        elif best["max_error"] <= acceptable and current["max_error"] > acceptable:
            # Keep an already feasible incumbent even if a heavily weighted
            # condition would make the infeasible candidate look attractive.
            pass
        elif current["key"] < best["key"]:
            best = current
            incumbent_changed = True
        display_candidate(current, is_best=incumbent_changed, is_current=is_current, publish=publish_display)
        if incumbent_changed and checkpoint_callback is not None:
            thickness = {
                layer_id: float(value)
                for value, (_side, layer_id, _low, _high, _current)
                in zip(best["values"], normalized_variables)
            }
            checkpoint_callback({
                "schema_version": 1,
                "kind": "coat_sim.film_optimization_checkpoint",
                "design_revision": request.design_revision,
                "best_design": deepcopy(best["design"]),
                "best_thickness_nm": thickness,
                "max_normalized_error": float(best["max_error"]),
                "weighted_max_normalized_error": float(best["weighted_max_error"]),
                "weighted_rms_error": float(best["weighted_rms_error"]),
                "targets_met": bool(best["max_error"] <= acceptable),
                "per_condition_errors": [asdict(row) for row in best["errors"]],
                "iterations": int(iteration),
                "evaluations": int(evaluations),
            })
        if progress_callback is not None:
            emit("running", 0.05 + 0.85 * min(1.0, evaluations / max_evaluations),
                 iteration, evaluations,
                 None if best is None else float(best["max_error"]))

    def consider(values: np.ndarray) -> dict[str, Any] | None:
        nonlocal evaluations, failed_evaluations, cancel_after_evaluation
        if evaluations >= max_evaluations:
            return None
        try:
            current = evaluate(values)
        except (ValueError, ArithmeticError, FloatingPointError):
            if best is None:
                raise
            evaluations += 1
            failed_evaluations += 1
            cancel_after_evaluation = cancelled()
            return None
        evaluations += 1
        archive(current)
        cancel_after_evaluation = cancelled()
        return current

    # Always evaluate a complete baseline candidate first. This provides a
    # usable return value even when cancellation arrives before the search.
    if request.execution_device is not None:
        from .film_optimization_batch_forward import OptimizationBatchForward
        strict_forward = OptimizationBatchForward(
            frozen, objectives, tuple(row[1] for row in normalized_variables),
            execution_device=request.execution_device, callback=performance_callback)
    consider(initial)
    assert best is not None
    termination_reason = "target_met" if best["max_error"] <= acceptable else ""
    if (solver == "baseline" and not has_unbounded_axis and
            best["max_error"] > acceptable and not cancel_after_evaluation):
        # Deterministic coarse bounded sweep: sample each variable across its
        # inclusive range while holding the others at the best known point.
        # This is useful for oscillatory thin-film objectives but is not a
        # global-optimality guarantee, especially with coupled variables.
        for axis, span in enumerate(spans):
            for fraction in np.linspace(0.0, 1.0, 17):
                if evaluations >= max_evaluations or cancel_after_evaluation:
                    break
                probe = best["values"].copy()
                probe[axis] = lows[axis] + float(fraction) * span
                if np.isclose(probe[axis], best["values"][axis], rtol=0, atol=1e-12):
                    continue
                consider(probe)
                if best["max_error"] <= acceptable:
                    break
            if best["max_error"] <= acceptable or evaluations >= max_evaluations or cancel_after_evaluation:
                break

    if solver == "baseline" and has_unbounded_axis:
        # A local coordinate search in nm permits d >= low without imposing a
        # fabricated high bound. The reference wavelength sets an initial trial
        # scale, not a domain limit; successful directions expand their step.
        reference_scale = float(base_design["reference_wavelength_nm"]) / 4.0
        physical_spans = np.where(np.isfinite(highs), spans,
                                  np.maximum(initial - lows, reference_scale))
        step_nm = physical_spans * initial_step
        minimum_step_nm = physical_spans * min_step
        while (best["max_error"] > acceptable and iteration < max_iterations and
               evaluations < max_evaluations and not cancel_after_evaluation and
               np.any(step_nm > minimum_step_nm)):
            iteration += 1
            for axis in range(len(normalized_variables)):
                if evaluations >= max_evaluations or cancel_after_evaluation:
                    break
                improved = False
                for direction in (-1.0, 1.0):
                    if evaluations >= max_evaluations or cancel_after_evaluation:
                        break
                    probe = best["values"].copy()
                    trial = float(probe[axis]) + direction * float(step_nm[axis])
                    if not math.isfinite(trial):
                        continue
                    probe[axis] = min(highs[axis], max(lows[axis], trial))
                    if np.isclose(probe[axis], best["values"][axis], rtol=0, atol=1e-12):
                        continue
                    previous = best["key"]
                    consider(probe)
                    improved = best["key"] < previous
                    if improved or best["max_error"] <= acceptable:
                        break
                if improved:
                    step_nm[axis] *= 2.0
                else:
                    step_nm[axis] *= 0.5
                if best["max_error"] <= acceptable:
                    break

    elif solver == "baseline":
        step = np.full(len(normalized_variables), initial_step, dtype=float)
        while (best["max_error"] > acceptable and iteration < max_iterations and
               evaluations < max_evaluations and not cancel_after_evaluation and
               np.any(step > min_step)):
            iteration += 1
            improved = False
            for axis in range(len(normalized_variables)):
                if evaluations >= max_evaluations or cancel_after_evaluation:
                    break
                for direction in (-1.0, 1.0):
                    if evaluations >= max_evaluations or cancel_after_evaluation:
                        break
                    probe = best["values"].copy()
                    probe[axis] = min(highs[axis], max(
                        lows[axis], probe[axis] + direction * step[axis] * spans[axis]))
                    if np.isclose(probe[axis], best["values"][axis], rtol=0, atol=1e-12):
                        continue
                    old_key = best["key"]
                    consider(probe)
                    improved |= best["key"] < old_key
                    if best["max_error"] <= acceptable:
                        break
                if best["max_error"] <= acceptable:
                    break
            if not improved:
                step *= 0.5

    elif (request.execution_mode == "deferred_batch" and
          best["max_error"] > acceptable and not cancel_after_evaluation):
        from .film_batch_de import BatchEvaluation, run_batch_de
        from .film_optimization_batch_forward import OptimizationBatchForward
        from .film_design_service import FilmDesignService

        if getattr(service.simulate, "__func__", None) is not FilmDesignService.simulate:
            raise ValueError("deferred_batch requires the canonical FilmDesignService")
        forward = strict_forward or OptimizationBatchForward(
            frozen, objectives, tuple(row[1] for row in normalized_variables),
            options=performance_options, callback=performance_callback,
            execution_device=request.execution_device)
        baseline = best
        baseline_pending = True
        display_pending: list[Any] = [None]

        def batch_evaluate(points):
            nonlocal baseline_pending
            if baseline_pending:
                baseline_pending = False
                # run_batch_de owns counting this already-complete baseline.
                return [BatchEvaluation(baseline["key"], search_scalar(
                    baseline["max_error"], baseline["weighted_rms_error"], acceptable), baseline)]
            responses = forward.responses(points, cancel=cancel_event)
            results = []
            chunk_best = None
            chunk_best_index = None
            for index, point in enumerate(points):
                if cancelled():
                    raise InterruptedError("optimization batch cancelled during scoring")
                current = evaluate(point, supplied_responses=forward.candidate_responses(responses, index),
                                   include_details=False)
                results.append(BatchEvaluation(current["key"], search_scalar(
                    current["max_error"], current["weighted_rms_error"], acceptable),
                    {"display_metrics": (current["max_error"], current["weighted_rms_error"],
                                         current["nominal_pass"], current["condition_count"])} if display_physical is not None else None))
                if chunk_best is None or current["key"] < chunk_best["key"]:
                    chunk_best, chunk_best_index = current, index
            # Only a chunk's best can replace the global best after atomic
            # batch acceptance. Do not retain full condition objects for losers.
            assert chunk_best is not None and chunk_best_index is not None
            winner_value = {
                "values": np.array(points[chunk_best_index], copy=True),
                "responses": tuple(forward.candidate_responses(responses, chunk_best_index)),
            }
            if display_physical is not None:
                winner_value["display_metrics"] = (
                    chunk_best["max_error"], chunk_best["weighted_rms_error"],
                    chunk_best["nominal_pass"], chunk_best["condition_count"])
            results[chunk_best_index] = BatchEvaluation(
                chunk_best["key"], search_scalar(
                    chunk_best["max_error"], chunk_best["weighted_rms_error"], acceptable), winner_value)
            if display_physical is not None:
                # Existing raw batch memory lives only through atomic acceptance
                # and its following progress callback. No display array copy.
                display_pending[0] = (responses, len(points) - 1)
            return results

        def batch_accepted(items, points, accepted_evaluations, accepted_generations):
            if display_physical is None:
                return
            for item, point in zip(items, points, strict=True):
                maximum, rms, passed, condition_count = item.value["display_metrics"]
                physical = dict(display_physical)
                for value, (side, identifier, _low, _high, _current) in zip(point, normalized_variables, strict=True):
                    physical[(side + "_layers", identifier)] = float(value)
                record_candidate(candidate_observer, candidate_design(point), weighted_rms_error=rms,
                    max_normalized_error=maximum, condition_rows=None, nominal_pass=passed,
                    condition_count=condition_count,
                    physical_thickness_nm=physical, iterations=accepted_generations,
                    evaluations=accepted_evaluations,
                    source="film_optimization_batch", scope="training", publish=False)

        def batch_spectrum(selection, limit):
            pending = display_pending[0]
            if pending is None:
                return None
            raw, index = pending
            objective_index = selection["objective_index"]
            objective = objectives[objective_index]
            if selection["angle_deg"] not in objective.angles_deg or selection["metric"] not in ("R", "T"):
                return None
            condition_index = sum(len(o.angles_deg) for o in objectives[:objective_index])
            condition_index += objective.angles_deg.index(selection["angle_deg"])
            response = raw[condition_index]
            waves, values = response["wavelength_nm"], response[selection["metric"]][index]
            count = len(waves)
            indices = (list(range(count)) if count <= limit else
                       [i * (count - 1) // (limit - 1) for i in range(limit)])
            return {"wavelength_nm": [float(waves[i]) for i in indices],
                    "actual": [float(values[i]) for i in indices], "indices": indices,
                    "original_point_count": count}

        def batch_progress(progress):
            nonlocal evaluations, failed_evaluations, iteration
            evaluations = progress.evaluations
            failed_evaluations = progress.failed
            iteration = progress.generations
            if progress.best.key < best["key"]:
                value = progress.best.value
                current = evaluate(value["values"], supplied_responses=iter(value["responses"]))
                archive(current, is_current=False, publish_display=False)
            else:
                archive(best, is_current=False, publish_display=False)
            try:
                refresh_candidate_display(candidate_observer, spectrum_provider=batch_spectrum)
            finally:
                display_pending[0] = None

        outcome = run_batch_de(
            batch_evaluate, initial, lows, highs,
            max_iterations=max_iterations, max_evaluations=max_evaluations,
            seed=seed, population_size=population_size,
            max_batch_size=min(request.batch_size, forward.batch_limit),
            cancel_event=cancel_event, checkpoint_callback=batch_progress,
            accepted_callback=batch_accepted if display_physical is not None else None)
        display_pending[0] = None
        evaluations, failed_evaluations = outcome.evaluations, outcome.failed
        iteration, termination_reason = outcome.generations, outcome.reason
        cancel_after_evaluation = outcome.state == "cancelled"

    elif solver != "baseline" and best["max_error"] > acceptable and not cancel_after_evaluation:
        if solver in {"trf", "lbfgsb"}:
            from .film_optimization_derivatives import simulate_with_thickness_jacobian

            derivative_variables = [
                FilmThicknessVariable(side, layer_id, low, high)
                for side, layer_id, low, high, _current in normalized_variables
            ]
            initial_fraction = (initial - lows) / spans
            from collections import OrderedDict

            # Size failed analytic residuals to the complete validated grid.
            # This statistic is not a computation quota.
            total_conditions = sum(item.wavelength_samples * len(item.angles_deg)
                                   for item in objectives)

            candidate_cache: OrderedDict[
                tuple[float, ...], tuple[dict[str, Any], np.ndarray, np.ndarray]
            ] = OrderedDict()
            initial_key = tuple(float(value) for value in initial_fraction)

            def evaluate_with_derivatives(
                fraction_values: np.ndarray,
            ) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
                physical_values = lows + np.asarray(fraction_values, dtype=float) * spans
                candidate = candidate_design(physical_values)
                errors: list[FilmConditionError] = []
                residuals: list[float] = []
                jacobian_rows: list[np.ndarray] = []
                normalized_errors: list[float] = []
                condition_weights: list[float] = []
                max_error = 0.0
                weighted_max_error = 0.0
                for objective_index, objective in enumerate(objectives):
                    waves = np.linspace(
                        objective.wavelength_min_nm, objective.wavelength_max_nm,
                        objective.wavelength_samples,
                    )
                    for angle in objective.angles_deg:
                        response = simulate_with_thickness_jacobian(
                            candidate, frozen["materials"], derivative_variables, {
                                "wavelength_min_nm": objective.wavelength_min_nm,
                                "wavelength_max_nm": objective.wavelength_max_nm,
                                "samples": objective.wavelength_samples,
                                "angle_deg": angle,
                                "polarization": objective.polarization,
                                "first_surface": candidate.get("first_surface", "front"),
                            },
                        )
                        actual_values = np.asarray(response[objective.metric], dtype=float)
                        actual_waves = np.asarray(response["wavelength_nm"], dtype=float)
                        actual_jacobian = np.asarray(
                            response[f"jacobian_{objective.metric}"], dtype=float)
                        expected_shape = (objective.wavelength_samples, len(normalized_variables))
                        if (actual_values.shape != waves.shape or actual_waves.shape != waves.shape or
                                actual_jacobian.shape != expected_shape or
                                not np.all(np.isfinite(actual_values)) or
                                not np.all(np.isfinite(actual_jacobian)) or
                                not np.allclose(actual_waves, waves, rtol=0, atol=1e-9)):
                            raise ValueError("analytic derivative returned invalid objective samples")
                        for index, (wavelength, actual) in enumerate(zip(waves, actual_values)):
                            actual = float(actual)
                            if objective.mode == "value":
                                signed_error = actual - float(objective.target_value)
                                residual_derivative = actual_jacobian[index]
                                violation = abs(signed_error)
                            elif (objective.lower_bound is not None and
                                  actual < objective.lower_bound):
                                signed_error = float(objective.lower_bound) - actual
                                residual_derivative = -actual_jacobian[index]
                                violation = signed_error
                            elif (objective.upper_bound is not None and
                                  actual > objective.upper_bound):
                                signed_error = actual - float(objective.upper_bound)
                                residual_derivative = actual_jacobian[index]
                                violation = signed_error
                            else:
                                signed_error = 0.0
                                residual_derivative = np.zeros(len(normalized_variables))
                                violation = 0.0
                            normalized_error, weighted_error = score_condition(
                                violation, objective.normalization_scale, objective.weight)
                            residuals.append(weighted_scaled_value(
                                signed_error, objective.normalization_scale, objective.weight))
                            jacobian_rows.append(np.asarray([
                                weighted_scaled_value(float(value),
                                                      objective.normalization_scale,
                                                      objective.weight)
                                for value in residual_derivative], dtype=float) * spans)
                            max_error = max(max_error, normalized_error)
                            weighted_max_error = max(weighted_max_error, weighted_error)
                            normalized_errors.append(normalized_error)
                            condition_weights.append(objective.weight)
                            errors.append(FilmConditionError(
                                objective_index=objective_index,
                                wavelength_nm=float(wavelength), angle_deg=float(angle),
                                polarization=objective.polarization, metric=objective.metric,
                                actual=actual, target_value=objective.target_value,
                                lower_bound=objective.lower_bound, upper_bound=objective.upper_bound,
                                normalized_violation=normalized_error,
                                weighted_normalized_violation=weighted_error,
                            ))
                weighted_rms = weighted_rms_error(normalized_errors, condition_weights)
                current = {
                    "values": physical_values, "design": candidate,
                    "errors": tuple(errors), "max_error": max_error,
                    "weighted_max_error": weighted_max_error,
                    "weighted_rms_error": weighted_rms,
                    "key": candidate_key(max_error, weighted_rms, acceptable),
                }
                residual_vector = np.asarray(residuals, dtype=float)
                jacobian_matrix = np.asarray(jacobian_rows, dtype=float)
                if (not np.all(np.isfinite(residual_vector)) or
                        not np.all(np.isfinite(jacobian_matrix))):
                    raise ValueError("analytic residual or Jacobian is not finite")
                with np.errstate(over="ignore", invalid="ignore"):
                    gradient = jacobian_matrix.T @ residual_vector
                if not np.all(np.isfinite(gradient)):
                    raise ValueError("analytic cost gradient is not finite")
                if solver == "lbfgsb":
                    residual_norm = math.hypot(*residual_vector.tolist())
                    max_cost_norm = math.sqrt(float.fromhex(
                        "0x1.fffffffffffffp+1023")) * math.sqrt(2.0)
                    if not math.isfinite(residual_norm) or residual_norm > max_cost_norm:
                        raise ValueError("L-BFGS-B cost is not finite")
                    if not math.isfinite(0.5 * residual_norm * residual_norm):
                        raise ValueError("L-BFGS-B cost is not finite")
                return current, residual_vector, jacobian_matrix

            def remember_candidate(
                key: tuple[float, ...],
                value: tuple[dict[str, Any], np.ndarray, np.ndarray],
            ) -> None:
                candidate_cache[key] = value
                candidate_cache.move_to_end(key)
                while len(candidate_cache) > 2:
                    candidate_cache.popitem(last=False)

            def derivative_pair(
                point: np.ndarray,
            ) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
                nonlocal evaluations, failed_evaluations, cancel_after_evaluation
                key = tuple(float(value) for value in point)
                if key in candidate_cache:
                    candidate_cache.move_to_end(key)
                    return candidate_cache[key]
                if cancelled() or cancel_after_evaluation:
                    from .film_optimization_solvers import SolverStop
                    raise SolverStop("cancelled")
                if evaluations >= max_evaluations:
                    from .film_optimization_solvers import SolverStop
                    raise SolverStop("evaluation_budget")
                try:
                    computed = evaluate_with_derivatives(point)
                except (ValueError, ArithmeticError, FloatingPointError):
                    evaluations += 1
                    failed_evaluations += 1
                    cancel_after_evaluation = cancelled()
                    if key == initial_key:
                        raise
                    failed = (
                        {}, np.full(total_conditions, 1e6, dtype=float),
                        np.zeros((total_conditions, len(normalized_variables)), dtype=float),
                    )
                    remember_candidate(key, failed)
                    return failed
                current, residual_vector, jacobian_matrix = computed
                evaluations += 1
                archive(current)
                cancel_after_evaluation = cancelled()
                remember_candidate(key, computed)
                if cancel_after_evaluation:
                    from .film_optimization_solvers import SolverStop
                    raise SolverStop("cancelled")
                return computed

            # This preflights the declared analytic support domain and seeds
            # the residual/Jacobian cache. A cache miss is a separate exact
            # candidate evaluation and consumes the shared budget.
            if evaluations < max_evaluations and not cancel_after_evaluation:
                derivative_pair(initial_fraction)
            if evaluations < max_evaluations and not cancel_after_evaluation:
                from scipy.optimize import least_squares, minimize
                from .film_optimization_solvers import SolverStop

                zero = np.zeros(len(normalized_variables), dtype=float)
                one = np.ones(len(normalized_variables), dtype=float)
                iteration = 0
                gradient_stop_reason = ""

                def residual_function(point: np.ndarray) -> np.ndarray:
                    return derivative_pair(point)[1]

                if solver == "trf":
                    remaining = max(1, max_evaluations - evaluations + 1)

                    def trf_callback(intermediate_result: Any) -> None:
                        nonlocal iteration, gradient_stop_reason
                        iteration += 1
                        if progress_callback is not None:
                            emit("running", 0.05 + 0.85 * min(
                                1.0, evaluations / max_evaluations), iteration,
                                evaluations, float(best["max_error"]))
                        if iteration >= max_iterations:
                            gradient_stop_reason = "iteration_limit"
                            raise StopIteration

                    scipy_parameters = __import__("inspect").signature(least_squares).parameters
                    if "callback" not in scipy_parameters:
                        raise ValueError("TRF requires SciPy 1.16 or newer for iteration accounting")
                    try:
                        result = least_squares(
                            residual_function,
                            initial_fraction,
                            jac=lambda point: derivative_pair(point)[2],
                            bounds=(zero, one), method="trf", x_scale="jac",
                            max_nfev=remaining, ftol=max(min_step, 1e-12),
                            xtol=max(min_step, 1e-12), gtol=max(min_step, 1e-12),
                            callback=trf_callback,
                        )
                    except SolverStop as stop:
                        termination_reason = stop.reason
                    else:
                        if gradient_stop_reason:
                            termination_reason = gradient_stop_reason
                        elif result.success:
                            termination_reason = "converged"
                        elif evaluations >= max_evaluations or result.status == 0:
                            termination_reason = "evaluation_budget"
                        else:
                            termination_reason = "solver_stopped"
                else:
                    remaining = max(1, max_evaluations - evaluations + 1)

                    def cost_function(point: np.ndarray) -> float:
                        residual_vector = residual_function(point)
                        residual_norm = math.hypot(*residual_vector.tolist())
                        cost = 0.5 * residual_norm * residual_norm
                        if not math.isfinite(cost):
                            raise ValueError("L-BFGS-B cost is not finite")
                        return cost

                    def cost_jacobian(point: np.ndarray) -> np.ndarray:
                        _current, residual_vector, jacobian_matrix = derivative_pair(point)
                        with np.errstate(over="ignore", invalid="ignore"):
                            gradient = jacobian_matrix.T @ residual_vector
                        if not np.all(np.isfinite(gradient)):
                            raise ValueError("L-BFGS-B cost gradient is not finite")
                        return gradient

                    def lbfgsb_callback(_point: np.ndarray) -> bool:
                        nonlocal iteration, gradient_stop_reason
                        iteration += 1
                        if progress_callback is not None:
                            emit("running", 0.05 + 0.85 * min(
                                1.0, evaluations / max_evaluations), iteration,
                                evaluations, float(best["max_error"]))
                        if iteration >= max_iterations:
                            gradient_stop_reason = "iteration_limit"
                            return True
                        return False

                    try:
                        result = minimize(
                            cost_function, initial_fraction, method="L-BFGS-B",
                            jac=cost_jacobian, bounds=list(zip(zero, one)),
                            callback=lbfgsb_callback,
                            options={"maxiter": max_iterations, "maxfun": remaining,
                                     "ftol": max(min_step, 1e-12),
                                     "gtol": max(min_step, 1e-12), "maxls": 20},
                        )
                    except SolverStop as stop:
                        termination_reason = stop.reason
                    else:
                        if gradient_stop_reason:
                            termination_reason = gradient_stop_reason
                        elif result.success:
                            termination_reason = "converged"
                        elif iteration >= max_iterations:
                            termination_reason = "iteration_limit"
                        elif evaluations >= max_evaluations or result.status == 1:
                            termination_reason = "evaluation_budget"
                        else:
                            termination_reason = "solver_stopped"
            else:
                termination_reason = "evaluation_budget"
        else:
            from .film_optimization_solvers import SolverStop, run_solver

            initial_fraction = (initial - lows) / spans
            scalar_cache: dict[tuple[float, ...], float] = {
                tuple(float(value) for value in initial_fraction): search_scalar(
                    best["max_error"], best["weighted_rms_error"], acceptable)
            }

            def scalar_objective(point: np.ndarray) -> float:
                nonlocal cancel_after_evaluation
                key = tuple(float(value) for value in point)
                if key in scalar_cache:
                    return scalar_cache[key]
                if cancelled() or cancel_after_evaluation:
                    raise SolverStop("cancelled")
                if evaluations >= max_evaluations:
                    raise SolverStop("evaluation_budget")
                physical_values = lows + point * spans
                current = consider(physical_values)
                if current is None:
                    failed_score = max(1e6, scalar_cache.get(
                        tuple(float(value) for value in initial_fraction), 0.0) + 1.0)
                    scalar_cache[key] = failed_score
                    if cancel_after_evaluation:
                        raise SolverStop("cancelled")
                    return failed_score
                scalar_cache[key] = search_scalar(
                    current["max_error"], current["weighted_rms_error"], acceptable)
                if cancel_after_evaluation:
                    raise SolverStop("cancelled")
                return scalar_cache[key]

            outcome = run_solver(
                solver, initial_fraction, np.zeros(len(initial_fraction)),
                np.ones(len(initial_fraction)), scalar_objective,
                max_iterations=max_iterations, seed=seed,
                multistart_count=multistart_count, population_size=population_size,
                initial_step_fraction=initial_step, min_step_fraction=min_step,
            )
            iteration = outcome.iterations
            termination_reason = outcome.reason

    assert best is not None
    validation_evaluations = 0
    validation_complete = False
    validation_max_error: float | None = None
    validation_weighted_rms: float | None = None
    validation_targets_met: bool | None = None
    validation_errors: tuple[FilmConditionError, ...] = ()
    validation_message = ""
    was_cancelled = cancel_after_evaluation or cancelled() or termination_reason == "cancelled"
    if not was_cancelled:
        validation_objectives = [
            replace(
                objective,
                wavelength_samples=(
                    1 if objective.wavelength_samples == 1 else
                    (objective.wavelength_samples - 1) * validation_multiplier + 1
                ),
            )
            for objective in objectives
        ]
        if progress_callback is not None:
            emit("running", 0.93, iteration, evaluations, float(best["max_error"]),
                 "validating the selected candidate on the denser wavelength grid")
        try:
            validation = evaluate(
                best["values"], validation_objectives, check_cancellation=True)
        except _ValidationCancelled:
            was_cancelled = True
            validation_evaluations = 1
            validation_message = "holdout validation was cancelled before a complete grid was evaluated"
        except (ValueError, ArithmeticError, FloatingPointError) as exc:
            validation_evaluations = 1
            failed_evaluations += 1
            validation_message = f"holdout validation failed: {exc}"
            if not termination_reason or termination_reason == "target_met":
                termination_reason = "validation_failed"
        else:
            validation_evaluations = 1
            validation_complete = True
            validation_max_error = float(validation["max_error"])
            validation_weighted_rms = float(validation["weighted_rms_error"])
            validation_targets_met = bool(validation_max_error <= acceptable)
            validation_errors = validation["errors"]
            validation_message = (
                "all denser holdout conditions meet the requested normalized error"
                if validation_targets_met else
                "the denser holdout grid contains a condition above the requested normalized error"
            )
    else:
        validation_message = "holdout validation skipped because optimization was cancelled"

    state: OptimizationState = "cancelled" if was_cancelled else "completed"
    if state == "cancelled":
        termination_reason = "cancelled"
        message = ("cancelled during holdout validation; best training candidate retained"
                   if validation_evaluations else
                   "cancelled after the current complete candidate evaluation; best candidate retained")
    elif termination_reason == "batch_evaluation_failed":
        state = "failed"
        message = "batch evaluation failed; last complete best candidate retained"
    elif best["max_error"] <= acceptable:
        message = "all sampled training conditions meet the requested normalized error"
    elif termination_reason == "evaluation_budget" or evaluations >= max_evaluations:
        termination_reason = termination_reason or "evaluation_budget"
        message = "maximum evaluation budget reached; target attainment is not guaranteed"
    elif termination_reason == "iteration_limit" or iteration >= max_iterations:
        termination_reason = termination_reason or "iteration_limit"
        message = "maximum iteration count reached; target attainment is not guaranteed"
    else:
        termination_reason = termination_reason or "converged"
        message = ("half-unbounded local coordinate search converged; target attainment is not guaranteed"
                   if has_unbounded_axis else
                   "bounded local search converged; target attainment is not guaranteed")
    if not termination_reason:
        termination_reason = "target_met" if best["max_error"] <= acceptable else "converged"
    emit(state, 1.0, iteration, evaluations, float(best["max_error"]), message)
    display_candidate(best, is_best=True, is_current=False)
    finish_candidate_display(candidate_observer, state=state)
    thickness = {layer_id: float(value) for value, (_side, layer_id, _low, _high, _current)
                 in zip(best["values"], normalized_variables)}
    return FilmOptimizationResult(
        state=state, design_revision=request.design_revision,
        best_design=deepcopy(best["design"]), best_thickness_nm=thickness,
        max_normalized_error=float(best["max_error"]),
        weighted_max_normalized_error=float(best["weighted_max_error"]),
        weighted_rms_error=float(best["weighted_rms_error"]),
        targets_met=bool(best["max_error"] <= acceptable),
        per_condition_errors=best["errors"], iterations=iteration,
        evaluations=evaluations, message=message,
        solver=solver, seed=seed, termination_reason=termination_reason,
        failed_evaluations=failed_evaluations,
        validation_samples_multiplier=validation_multiplier,
        validation_evaluations=validation_evaluations,
        validation_complete=validation_complete,
        validation_max_normalized_error=validation_max_error,
        validation_weighted_rms_error=validation_weighted_rms,
        validation_targets_met=validation_targets_met,
        validation_per_condition_errors=validation_errors,
        validation_message=validation_message,
    )
