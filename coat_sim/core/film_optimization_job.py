"""Serializable executor contract for durable film-optimization jobs.

The scheduler owns registration and process dispatch. This module owns the
film-specific frozen request, incumbent checkpoint, progress, and result shapes.
"""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import math
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

if TYPE_CHECKING:
    from .film_reduction import FilmReductionRequest
    from .film_optimization_structure import FilmStructureOptimizationRequest

from .film_optimization import (
    FilmOptimizationObjective,
    FilmOptimizationRequest,
    FilmOptimizationResult,
    FilmThicknessVariable,
    freeze_film_optimization_request,
    optimize_film_design,
)
from .film_objective_criteria import FilmObjectiveCoverage, reduce_objective_responses, validate_objective_criteria
from .film_objective_payload import (condition_from_payload, coverage_from_payload, coverage_to_payload,
                                     objective_from_payload, objective_to_payload)


JOB_KIND = "coat_sim.film_optimization_job"
REQUEST_KIND = "coat_sim.film_optimization_request"
RESULT_KIND = "coat_sim.film_optimization_result"
STRUCTURE_REQUEST_KIND = "coat_sim.film_structure_optimization_request"
STRUCTURE_RESULT_KIND = "coat_sim.film_structure_optimization_result"
CANDIDATE_KIND = "coat_sim.film_optimization_candidate"
CHECKPOINT_KIND = "coat_sim.film_optimization_checkpoint"
CONTRACT_VERSION = 2

_REQUEST_REQUIRED_FIELDS = {
    "schema_version", "kind", "design_snapshot", "design_revision",
    "variables", "objectives", "max_iterations", "max_evaluations",
    "initial_step_fraction", "min_step_fraction",
    "acceptable_normalized_error", "frozen_material_snapshot",
    "solver", "seed", "multistart_count", "population_size",
    "validation_samples_multiplier",
}
_SOLVER_REQUEST_FIELDS = {
    "solver", "seed", "multistart_count", "population_size",
    "validation_samples_multiplier", "execution_mode", "batch_size", "execution_device",
}
_SUPPORTED_SOLVERS = {
    "baseline", "powell", "nelder_mead", "multistart",
    "differential_evolution", "simulated_annealing", "trf", "lbfgsb",
}
_SOLVER_VALIDATION_FIELDS = {
    "solver", "seed", "termination_reason", "failed_evaluations",
    "validation_samples_multiplier", "validation_evaluations",
    "validation_complete", "validation_max_normalized_error",
    "validation_weighted_rms_error", "validation_targets_met",
    "validation_per_condition_errors", "validation_message",
    "validation_solver_acceptable", "validation_raw_coverage",
}


def _validate_solver_request_fields(*, solver: Any, seed: Any,
                                    multistart_count: Any,
                                    population_size: Any,
                                    validation_samples_multiplier: Any) -> None:
    """Reject invalid solver settings at the queue boundary, never default them."""
    if not isinstance(solver, str) or solver not in _SUPPORTED_SOLVERS:
        raise ValueError(f"solver must be one of {', '.join(sorted(_SUPPORTED_SOLVERS))}")
    if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
        raise ValueError("seed must be an integer from 0 to 4294967295")
    for value, name in ((multistart_count, "multistart_count"),
                        (population_size, "population_size"),
                        (validation_samples_multiplier, "validation_samples_multiplier")):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if solver == "differential_evolution" and population_size < 5:
        raise ValueError("population_size must be at least 5 for differential_evolution")


def serialize_request(request: FilmOptimizationRequest | FilmReductionRequest) -> dict[str, Any]:
    from .film_reduction import FilmReductionRequest, film_reduction_request_to_payload
    if isinstance(request, FilmReductionRequest):
        return film_reduction_request_to_payload(request)
    if not isinstance(request, FilmOptimizationRequest):
        raise ValueError("request must be a FilmOptimizationRequest")
    _validate_solver_request_fields(
        solver=request.solver, seed=request.seed,
        multistart_count=request.multistart_count,
        population_size=request.population_size,
        validation_samples_multiplier=request.validation_samples_multiplier,
    )
    value = {
        "schema_version": CONTRACT_VERSION,
        "kind": REQUEST_KIND,
        "design_snapshot": deepcopy(dict(request.design_snapshot)),
        "design_revision": request.design_revision,
        "variables": [asdict(item) for item in request.variables],
        "objectives": [objective_to_payload(item) for item in request.objectives],
        "max_iterations": request.max_iterations,
        "max_evaluations": request.max_evaluations,
        "initial_step_fraction": request.initial_step_fraction,
        "min_step_fraction": request.min_step_fraction,
        "acceptable_normalized_error": request.acceptable_normalized_error,
        "frozen_material_snapshot": deepcopy(
            dict(request.frozen_material_snapshot)
            if request.frozen_material_snapshot is not None else None),
        "solver": request.solver,
        "seed": request.seed,
        "multistart_count": request.multistart_count,
        "population_size": request.population_size,
        "validation_samples_multiplier": request.validation_samples_multiplier,
    }
    from .film_optimization import _validate_execution_controls
    _validate_execution_controls(request)
    if request.execution_mode != "serial" or request.batch_size != 16:
        value.update(execution_mode=request.execution_mode, batch_size=request.batch_size)
    if request.execution_device is not None:
        from .film_xpu_execution import validate_binding
        value["execution_device"] = validate_binding(request.execution_device)
    json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return value


def deserialize_request(value: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping) and value.get("kind") == "coat_sim.film_reduction_request":
        from .film_reduction import film_reduction_request_from_payload
        return film_reduction_request_from_payload(value)
    if (isinstance(value, Mapping)
            and value.get("kind") == STRUCTURE_REQUEST_KIND):
        from .film_optimization_structure import film_structure_request_from_payload

        return film_structure_request_from_payload(value)
    if (not isinstance(value, Mapping)
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != CONTRACT_VERSION
            or value.get("kind") != REQUEST_KIND):
        raise ValueError("film optimization request kind/version is invalid")
    fields = set(value)
    if (not _REQUEST_REQUIRED_FIELDS.issubset(fields)
            or fields - (_REQUEST_REQUIRED_FIELDS | _SOLVER_REQUEST_FIELDS)):
        raise ValueError("film optimization request fields are invalid")
    try:
        variables = tuple(FilmThicknessVariable(**item) for item in value["variables"])
        objectives = tuple(objective_from_payload(item) for item in value["objectives"])
        request = FilmOptimizationRequest(
            design_snapshot=deepcopy(value["design_snapshot"]),
            design_revision=value["design_revision"], variables=variables,
            objectives=objectives, max_iterations=value["max_iterations"],
            max_evaluations=value["max_evaluations"],
            initial_step_fraction=value["initial_step_fraction"],
            min_step_fraction=value["min_step_fraction"],
            acceptable_normalized_error=value["acceptable_normalized_error"],
            frozen_material_snapshot=deepcopy(value["frozen_material_snapshot"]),
            solver=value["solver"],
            seed=value["seed"],
            multistart_count=value["multistart_count"],
            population_size=value["population_size"],
            validation_samples_multiplier=value["validation_samples_multiplier"],
            execution_mode=value.get("execution_mode", "serial"),
            batch_size=value.get("batch_size", 16),
            execution_device=deepcopy(value.get("execution_device")),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("film optimization request fields are invalid") from exc
    _validate_solver_request_fields(
        solver=request.solver, seed=request.seed,
        multistart_count=request.multistart_count,
        population_size=request.population_size,
        validation_samples_multiplier=request.validation_samples_multiplier,
    )
    from .film_optimization import _validate_execution_controls
    _validate_execution_controls(request)
    _validate_replay_request(request)
    return request


def build_job_payload(request: FilmOptimizationRequest | FilmReductionRequest) -> dict[str, Any]:
    frozen = serialize_request(request)
    frozen_base = frozen.get("base_request", frozen)
    if frozen_base["frozen_material_snapshot"] is None:
        raise ValueError("freeze the film optimization request before queue submission")
    return {"schema_version": CONTRACT_VERSION, "kind": JOB_KIND,
            "request": frozen}


def freeze_job_payload(service: Any, request: FilmOptimizationRequest | FilmReductionRequest
                       | FilmStructureOptimizationRequest) -> dict[str, Any]:
    """Freeze and serialize typed film requests supported by this executor.

    Structure search shares only the outer ``film_optimization`` executor. Its
    embedded request remains independently typed and has no solver ID.
    """
    from .film_reduction import FilmReductionRequest, freeze_film_reduction_request
    if isinstance(request, FilmReductionRequest):
        return build_job_payload(freeze_film_reduction_request(service, request))
    from .film_optimization_structure import (
        FilmStructureOptimizationRequest,
        film_structure_request_to_payload,
        freeze_film_structure_optimization_request,
    )

    if isinstance(request, FilmStructureOptimizationRequest):
        frozen = freeze_film_structure_optimization_request(service, request)
        payload = {
            "schema_version": CONTRACT_VERSION,
            "kind": JOB_KIND,
            "request": film_structure_request_to_payload(frozen),
        }
        json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        return payload
    return build_job_payload(freeze_film_optimization_request(service, request))


class _FrozenMaterials:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = {str(row["id"]): deepcopy(row) for row in rows}

    def _records(self, material_ids=None) -> list[dict[str, Any]]:
        if material_ids is None:
            source = self._rows.values()
        else:
            source = (self._rows[identifier] for identifier in material_ids
                      if identifier in self._rows)
        return [deepcopy(row) for row in source]

    def get(self, material_id: str) -> dict[str, Any]:
        try:
            return deepcopy(self._rows[material_id])
        except KeyError as exc:
            raise ValueError(f"frozen film optimization material missing: {material_id}") from exc


def _service_with_frozen_materials(service: Any,
                                   snapshot: Mapping[str, Any]) -> Any:
    rows = deepcopy(snapshot["materials"])
    worker_service = copy(service)
    worker_service.materials = _FrozenMaterials(rows)
    worker_service.freeze_design = (
        lambda _design, _settings=None, value=deepcopy(dict(snapshot)): deepcopy(value))
    return worker_service


def _candidate_payload(request: FilmOptimizationRequest, *, design: Mapping[str, Any],
                       thickness_nm: Mapping[str, Any], max_error: float,
                       weighted_max_error: float, weighted_rms_error: float,
                       targets_met: bool, solver_acceptable: bool,
                       per_condition_errors: list[dict[str, Any]],
                       raw_coverage: Sequence[FilmObjectiveCoverage | Mapping[str, Any]],
                       iterations: int, evaluations: int) -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_VERSION,
        "kind": CANDIDATE_KIND,
        "design_revision": request.design_revision,
        "design": deepcopy(dict(design)),
        "thickness_nm": deepcopy(dict(thickness_nm)),
        "quality": {
            "metric": "max_normalized_error",
            "value": float(max_error),
            "direction": "minimize",
            "weighted_max_normalized_error": float(weighted_max_error),
            "weighted_rms_error": float(weighted_rms_error),
            "targets_met": bool(targets_met),
            "solver_acceptable": bool(solver_acceptable),
        },
        "per_condition_errors": deepcopy(per_condition_errors),
        "raw_coverage": [coverage_to_payload(row) for row in raw_coverage],
        "iterations": int(iterations),
        "evaluations": int(evaluations),
    }


def _partial_result(candidate: Mapping[str, Any]) -> dict[str, Any]:
    quality = candidate["quality"]
    return {
        "schema_version": CONTRACT_VERSION,
        "kind": "best_candidate",
        "best_so_far": True,
        "candidate_verified": True,
        "quality": {"metric": quality["metric"], "value": quality["value"]},
        "artifact_kind": CANDIDATE_KIND,
        "artifact_schema_version": CONTRACT_VERSION,
    }


def _result_payload(request: FilmOptimizationRequest, candidate: dict[str, Any], *,
                    state: str | None = None,
                    message: str | None = None,
                    solver_validation: dict[str, Any] | None = None) -> dict[str, Any]:
    value = {
        "schema_version": CONTRACT_VERSION,
        "kind": RESULT_KIND,
        "request": serialize_request(request),
        "candidate": candidate,
        "partial_result": _partial_result(candidate),
    }
    if request.execution_device is not None:
        from .film_xpu_execution import execution_summary
        value["execution"] = execution_summary(request.execution_device)
    if state is not None:
        value["state"] = state
    if message is not None:
        value["message"] = message
    if solver_validation is not None:
        value["solver_validation"] = solver_validation
    json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return value


def _solver_validation_payload(result: FilmOptimizationResult) -> dict[str, Any]:
    """Serialize terminal solver/holdout metadata outside the training candidate."""
    return {
        "solver": result.solver,
        "seed": result.seed,
        "termination_reason": result.termination_reason,
        "failed_evaluations": result.failed_evaluations,
        "validation_samples_multiplier": result.validation_samples_multiplier,
        "validation_evaluations": result.validation_evaluations,
        "validation_complete": result.validation_complete,
        "validation_max_normalized_error": result.validation_max_normalized_error,
        "validation_weighted_rms_error": result.validation_weighted_rms_error,
        "validation_targets_met": result.validation_targets_met,
        "validation_solver_acceptable": result.validation_solver_acceptable,
        "validation_raw_coverage": [coverage_to_payload(row) for row in result.validation_raw_coverage],
        "validation_per_condition_errors": [
            asdict(item) for item in result.validation_per_condition_errors
        ],
        "validation_message": result.validation_message,
    }


def serialize_result(request: FilmOptimizationRequest,
                     result: FilmOptimizationResult) -> dict[str, Any]:
    if not isinstance(result, FilmOptimizationResult):
        raise ValueError("result must be a FilmOptimizationResult")
    candidate = _candidate_payload(
        request, design=result.best_design,
        thickness_nm=result.best_thickness_nm,
        max_error=result.max_normalized_error,
        weighted_max_error=result.weighted_max_normalized_error,
        weighted_rms_error=result.weighted_rms_error,
        targets_met=result.targets_met, solver_acceptable=result.solver_acceptable,
        raw_coverage=result.raw_coverage,
        per_condition_errors=[asdict(item) for item in result.per_condition_errors],
        iterations=result.iterations, evaluations=result.evaluations,
    )
    return _result_payload(
        request, candidate, state=result.state, message=result.message,
        solver_validation=_solver_validation_payload(result),
    )


def serialize_checkpoint(request: FilmOptimizationRequest,
                         checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    """Create a typed queue result from one fully evaluated optimizer incumbent."""
    if not isinstance(checkpoint, Mapping):
        raise ValueError("film optimization checkpoint must be a mapping")
    if (type(checkpoint.get("schema_version")) is not int
            or checkpoint.get("schema_version") != CONTRACT_VERSION
            or checkpoint.get("kind") != CHECKPOINT_KIND):
        raise ValueError("film optimization checkpoint kind/version is invalid")
    if request.frozen_material_snapshot is None:
        raise ValueError("a checkpoint requires the frozen optimization request")
    if checkpoint.get("design_revision") != request.design_revision:
        raise ValueError("checkpoint design revision does not match its request")
    if type(checkpoint.get("evaluations")) is not int or checkpoint["evaluations"] < 1:
        raise ValueError("checkpoint must contain a fully evaluated candidate")
    candidate = _candidate_payload(
        request, design=checkpoint["best_design"],
        thickness_nm=checkpoint["best_thickness_nm"],
        max_error=checkpoint["max_normalized_error"],
        weighted_max_error=checkpoint["weighted_max_normalized_error"],
        weighted_rms_error=checkpoint["weighted_rms_error"],
        targets_met=checkpoint["targets_met"], solver_acceptable=checkpoint["solver_acceptable"],
        raw_coverage=checkpoint["raw_coverage"],
        per_condition_errors=checkpoint["per_condition_errors"],
        iterations=checkpoint["iterations"], evaluations=checkpoint["evaluations"],
    )
    return _result_payload(request, candidate)


def _snapshot_for_candidate(request: FilmOptimizationRequest,
                            candidate: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = deepcopy(dict(request.frozen_material_snapshot or {}))
    snapshot.pop("sha256", None)
    snapshot["design"] = deepcopy(dict(candidate))
    identity = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    snapshot["sha256"] = hashlib.sha256(identity).hexdigest()
    return snapshot


def _replay_candidate_scores(
    worker_service: Any,
    candidate: Mapping[str, Any],
    objectives: tuple[FilmOptimizationObjective, ...] | list[FilmOptimizationObjective],
    *, tolerance: float = 0.,
) -> dict[str, Any]:
    """Evaluate one fixed candidate on an explicitly supplied grid.

    This deliberately bypasses the optimizer and its holdout phase. It is used
    to verify persisted training scores, and separately to verify terminal
    holdout metadata when that metadata is present.
    """
    import numpy as np

    responses: list[dict[str, Any]] = []
    for objective_index, objective in enumerate(objectives):
        waves = np.linspace(
            objective.wavelength_min_nm, objective.wavelength_max_nm,
            objective.wavelength_samples,
        )
        for angle in objective.angles_deg:
            for start in range(0, len(waves), 10001):
                chunk_waves = waves[start:start + 10001]
                response = worker_service.simulate(candidate, {
                    "wavelength_min_nm": float(chunk_waves[0]),
                    "wavelength_max_nm": float(chunk_waves[-1]),
                    "samples": int(len(chunk_waves)), "angle_deg": angle,
                    "polarization": objective.polarization,
                    "first_surface": candidate.get("first_surface", "front"),
                })
                actual_values = np.asarray(response[objective.metric], dtype=float)
                actual_waves = np.asarray(response["wavelength_nm"], dtype=float)
                if (actual_values.shape != chunk_waves.shape or
                        actual_waves.shape != chunk_waves.shape or
                        not np.all(np.isfinite(actual_values)) or
                        actual_waves[0] != chunk_waves[0] or actual_waves[-1] != chunk_waves[-1] or
                        np.any(np.diff(actual_waves) <= 0) or
                        not np.allclose(actual_waves, chunk_waves, rtol=0, atol=1e-9)):
                    raise ValueError("candidate replay returned invalid objective samples")
                responses.append({
                    "objective_index": objective_index, "angle_deg": float(angle),
                    "wavelength_nm": chunk_waves, "actual_values": actual_values,
                })
    reduction = reduce_objective_responses(objectives, responses, tolerance=tolerance)
    return {
        "max_normalized_error": reduction.max_error,
        "weighted_max_normalized_error": reduction.weighted_max_error,
        "weighted_rms_error": reduction.weighted_rms_error,
        "targets_met": reduction.nominal_pass,
        "solver_acceptable": reduction.solver_acceptable,
        "per_condition_errors": [asdict(row) for row in reduction.conditions],
        "raw_coverage": [coverage_to_payload(row) for row in reduction.coverage],
    }



def _same_number(value: Any, expected: float) -> bool:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not isinstance(expected, (int, float)) or isinstance(expected, bool)):
        return False
    return (math.isfinite(float(value)) and math.isfinite(float(expected))
            and math.isclose(float(value), float(expected),
                             rel_tol=1e-10, abs_tol=1e-12))


def _validate_replay_request(request: FilmOptimizationRequest) -> None:
    """Keep candidate replay bounded without invoking full optimizer validation."""
    def finite(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{label} must be finite")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{label} must be finite") from exc
        if not math.isfinite(number):
            raise ValueError(f"{label} must be finite")
        return number

    def positive_int(value: Any, label: str) -> int:
        if type(value) is not int or value < 1:
            raise ValueError(f"{label} must be a positive integer")
        return value

    if type(request.design_revision) is not int or request.design_revision < 0:
        raise ValueError("design_revision must be a nonnegative integer")
    if not isinstance(request.variables, (tuple, list)) or not request.variables:
        raise ValueError("at least one selected film thickness variable is required")
    if not isinstance(request.objectives, (tuple, list)) or not request.objectives:
        raise ValueError("at least one optical objective is required")
    positive_int(request.max_iterations, "max_iterations")
    positive_int(request.max_evaluations, "max_evaluations")
    if finite(request.acceptable_normalized_error,
              "acceptable_normalized_error") < 0:
        raise ValueError("acceptable_normalized_error must be nonnegative")

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
        low = finite(variable.lower_nm, "lower_nm")
        if variable.upper_nm is None:
            if request.solver != "baseline":
                raise ValueError("this finite-domain optimizer requires an explicit upper_nm for each selected layer; no upper bound was assumed")
            high = None
        else:
            high = finite(variable.upper_nm, "upper_nm")
        if low < 0 or (high is not None and high <= low):
            raise ValueError("thickness bounds must be nonnegative and strictly increasing")

    for objective in request.objectives:
        if not isinstance(objective, FilmOptimizationObjective):
            raise ValueError("objectives must contain FilmOptimizationObjective values")
        low = finite(objective.wavelength_min_nm, "wavelength_min_nm")
        high = finite(objective.wavelength_max_nm, "wavelength_max_nm")
        if low <= 0 or high < low:
            raise ValueError("objective wavelengths must be positive and ordered")
        samples = positive_int(objective.wavelength_samples, "wavelength_samples")
        if high == low:
            if samples != 1:
                raise ValueError("single-wavelength objectives must have one sample")
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
        validate_objective_criteria(objective)
    _validation_objectives(request)


def _validate_replayed_errors(recorded_errors: Any,
                              replay_errors: list[dict[str, Any]],
                              label: str) -> None:
    if not isinstance(recorded_errors, list) or len(recorded_errors) != len(replay_errors):
        raise ValueError(f"{label} condition data is incomplete")
    for recorded, actual in zip(recorded_errors, replay_errors, strict=True):
        condition_from_payload(recorded)
        if not isinstance(recorded, dict) or set(recorded) != set(actual):
            raise ValueError(f"{label} condition data is invalid")
        for key, actual_value in actual.items():
            recorded_value = recorded[key]
            if isinstance(actual_value, bool):
                if type(recorded_value) is not bool or recorded_value is not actual_value:
                    raise ValueError(f"{label} condition boolean labels do not match replay")
            elif key == "objective_index":
                if type(recorded_value) is not int or recorded_value != actual_value:
                    raise ValueError(f"{label} condition objective index does not match replay")
            elif isinstance(actual_value, (int, float)):
                if not _same_number(recorded_value, float(actual_value)):
                    raise ValueError(f"{label} condition values do not match replay")
            elif recorded_value != actual_value:
                raise ValueError(f"{label} condition labels do not match replay")


def _validate_replayed_coverage(recorded_coverage: Any, replay_coverage: list[dict[str, Any]],
                                label: str) -> None:
    if not isinstance(recorded_coverage, list) or len(recorded_coverage) != len(replay_coverage):
        raise ValueError(f"{label} raw spectral coverage is incomplete")
    for recorded, expected in zip(recorded_coverage, replay_coverage, strict=True):
        row = coverage_from_payload(recorded)
        if (type(row.objective_index) is not int or
                row.objective_index != expected["objective_index"] or
                not _same_number(row.angle_deg, expected["angle_deg"]) or
                row.angle_deg != expected["angle_deg"] or
                row.wavelength_nm != tuple(expected["wavelength_nm"]) or
                any(not _same_number(wave, actual) for wave, actual in
                    zip(row.wavelength_nm, expected["wavelength_nm"], strict=True)) or
                len(row.actual_values) != len(expected["actual_values"]) or
                any(not _same_number(value, actual) for value, actual in
                    zip(row.actual_values, expected["actual_values"], strict=True))):
            raise ValueError(f"{label} raw spectral coverage does not match replay")


def _validation_objectives(request: FilmOptimizationRequest) -> tuple[FilmOptimizationObjective, ...]:
    multiplier = request.validation_samples_multiplier
    objectives = tuple(
        replace(
            objective,
            wavelength_samples=(
                1 if objective.wavelength_samples == 1 else
                (objective.wavelength_samples - 1) * multiplier + 1
            ),
        )
        for objective in request.objectives
    )
    return objectives


def _validate_solver_validation(
    request: FilmOptimizationRequest,
    candidate: Mapping[str, Any],
    metadata: Any,
    state: str,
    worker_service: Any,
) -> None:
    if not isinstance(metadata, dict) or set(metadata) != _SOLVER_VALIDATION_FIELDS:
        raise ValueError("film optimization solver-validation metadata is invalid")
    if (metadata.get("solver") != request.solver
            or type(metadata.get("seed")) is not int
            or metadata.get("seed") != request.seed
            or type(metadata.get("validation_samples_multiplier")) is not int
            or metadata.get("validation_samples_multiplier") != request.validation_samples_multiplier):
        raise ValueError("solver-validation metadata does not match its request")
    if (not isinstance(metadata.get("termination_reason"), str)
            or type(metadata.get("failed_evaluations")) is not int
            or metadata["failed_evaluations"] < 0
            or type(metadata.get("validation_evaluations")) is not int
            or metadata["validation_evaluations"] not in (0, 1)
            or type(metadata.get("validation_complete")) is not bool
            or not isinstance(metadata.get("validation_message"), str)):
        raise ValueError("solver-validation metadata values are invalid")
    if metadata["validation_complete"]:
        if (metadata["validation_evaluations"] != 1
                or not _same_number(metadata["validation_max_normalized_error"],
                                    metadata["validation_max_normalized_error"])
                or not _same_number(metadata["validation_weighted_rms_error"],
                                    metadata["validation_weighted_rms_error"])
                or type(metadata["validation_targets_met"]) is not bool
                or type(metadata["validation_solver_acceptable"]) is not bool):
            raise ValueError("completed holdout-validation metadata is incomplete")
        replay = _replay_candidate_scores(
            worker_service, candidate, _validation_objectives(request),
            tolerance=request.acceptable_normalized_error)
        if not (_same_number(metadata["validation_max_normalized_error"],
                             replay["max_normalized_error"])
                and _same_number(metadata["validation_weighted_rms_error"],
                                 replay["weighted_rms_error"])
                and metadata["validation_targets_met"] is
                replay["targets_met"]
                and metadata["validation_solver_acceptable"] is replay["solver_acceptable"]):
            raise ValueError("holdout-validation scores do not match replay")
        _validate_replayed_errors(
            metadata["validation_per_condition_errors"],
            replay["per_condition_errors"], "holdout-validation")
        _validate_replayed_coverage(metadata["validation_raw_coverage"], replay["raw_coverage"], "holdout-validation")
    else:
        if (metadata["validation_max_normalized_error"] is not None
                or metadata["validation_weighted_rms_error"] is not None
                or metadata["validation_targets_met"] is not None
                or metadata["validation_per_condition_errors"] != []
                or metadata["validation_solver_acceptable"] is not None
                or metadata["validation_raw_coverage"] != []):
            raise ValueError("incomplete holdout validation must not claim scores")
        if state == "completed" and metadata["validation_message"] == "":
            raise ValueError("completed result must explain incomplete holdout validation")


def validate_checkpoint(service: Any, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    """Replay a persisted candidate against frozen materials, never re-solve it."""
    if not isinstance(checkpoint, Mapping):
        raise ValueError("film optimization checkpoint must be a mapping")
    if checkpoint.get("kind") == STRUCTURE_RESULT_KIND:
        from .film_optimization_structure import validate_film_structure_payload

        return validate_film_structure_payload(service, checkpoint)
    required = {"schema_version", "kind", "request", "candidate", "partial_result"}
    allowed = required | {"state", "message", "solver_validation", "execution"}
    if not required.issubset(checkpoint) or set(checkpoint) - allowed:
        raise ValueError("film optimization checkpoint fields are invalid")
    state = checkpoint.get("state")
    if state is not None and state not in ("completed", "cancelled"):
        raise ValueError("film optimization result state is invalid")
    if "message" in checkpoint and not isinstance(checkpoint["message"], str):
        raise ValueError("film optimization result message is invalid")
    if "solver_validation" in checkpoint and state is None:
        raise ValueError("training-only checkpoints cannot contain holdout metadata")
    if (type(checkpoint.get("schema_version")) is not int
            or checkpoint.get("schema_version") != CONTRACT_VERSION
            or checkpoint.get("kind") != RESULT_KIND):
        raise ValueError("film optimization result kind/version is invalid")
    json.dumps(dict(checkpoint), sort_keys=True, ensure_ascii=False, allow_nan=False)
    request = deserialize_request(checkpoint["request"])
    from .film_xpu_execution import execution_summary
    expected_execution = (execution_summary(request.execution_device)
                          if request.execution_device is not None else None)
    if checkpoint.get("execution") != expected_execution:
        raise ValueError("film result execution declaration differs from its frozen request")
    if request.frozen_material_snapshot is None:
        raise ValueError("checkpoint request is missing its frozen material snapshot")
    request = freeze_film_optimization_request(service, request)
    _validate_replay_request(request)
    candidate_payload = checkpoint["candidate"]
    candidate_fields = {
        "schema_version", "kind", "design_revision", "design", "thickness_nm",
        "quality", "per_condition_errors", "raw_coverage", "iterations", "evaluations",
    }
    if (not isinstance(candidate_payload, dict)
            or set(candidate_payload) != candidate_fields
            or type(candidate_payload.get("schema_version")) is not int
            or candidate_payload.get("schema_version") != CONTRACT_VERSION
            or candidate_payload.get("kind") != CANDIDATE_KIND):
        raise ValueError("film optimization candidate kind/version is invalid")
    if candidate_payload.get("design_revision") != request.design_revision:
        raise ValueError("candidate design revision does not match its request")
    if type(candidate_payload.get("evaluations")) is not int or candidate_payload["evaluations"] < 1:
        raise ValueError("checkpoint does not contain a completed evaluation")
    if type(candidate_payload.get("iterations")) is not int or candidate_payload["iterations"] < 0:
        raise ValueError("checkpoint iteration count is invalid")
    quality = candidate_payload["quality"]
    if (not isinstance(quality, dict)
            or set(quality) != {"metric", "value", "direction",
                                "weighted_max_normalized_error",
                                "weighted_rms_error", "targets_met", "solver_acceptable"}
            or quality.get("metric") != "max_normalized_error"
            or quality.get("direction") != "minimize"
            or type(quality.get("targets_met")) is not bool
            or type(quality.get("solver_acceptable")) is not bool):
        raise ValueError("film optimization candidate quality is invalid")
    partial = checkpoint["partial_result"]
    if (not isinstance(partial, dict)
            or set(partial) != {
                "schema_version", "kind", "best_so_far", "candidate_verified",
                "quality", "artifact_kind", "artifact_schema_version",
            }
            or type(partial.get("schema_version")) is not int
            or partial.get("schema_version") != CONTRACT_VERSION
            or partial.get("kind") != "best_candidate"
            or partial.get("best_so_far") is not True
            or partial.get("candidate_verified") is not True
            or partial.get("artifact_kind") != CANDIDATE_KIND
            or type(partial.get("artifact_schema_version")) is not int
            or partial.get("artifact_schema_version") != CONTRACT_VERSION
            or partial.get("quality") != {"metric": quality["metric"],
                                           "value": quality["value"]}):
        raise ValueError("film optimization partial-result descriptor is invalid")

    from .film_design import validate_design

    candidate = validate_design(deepcopy(candidate_payload["design"]))
    thickness = candidate_payload["thickness_nm"]
    if not isinstance(thickness, dict):
        raise ValueError("checkpoint thickness data is invalid")
    expected_ids = {variable.layer_id for variable in request.variables}
    if set(thickness) != expected_ids:
        raise ValueError("checkpoint thickness IDs do not match the request")
    for variable in request.variables:
        rows = candidate[variable.side + "_layers"]
        layer = next((row for row in rows if row["id"] == variable.layer_id), None)
        if layer is None or layer["thickness"].get("unit") != "nm":
            raise ValueError("checkpoint candidate is missing a physical thickness")
        value = thickness[variable.layer_id]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not math.isclose(float(layer["thickness"]["value"]), float(value),
                                    rel_tol=1e-12, abs_tol=1e-12)
                or float(value) < variable.lower_nm
                or (variable.upper_nm is not None and float(value) > variable.upper_nm)):
            raise ValueError("checkpoint candidate thickness is invalid")

    # Candidate results produced by optimization have expression text detached
    # from parsed layer state, matching candidate_design() in the optimizer.
    replay_design = deepcopy(candidate)
    expression = replay_design.get("expression", {})
    if expression.get("text") and expression.get("in_sync"):
        expression["in_sync"] = False
    if replay_design != candidate:
        raise ValueError("checkpoint candidate did not replay to the same design")
    frozen_snapshot = _snapshot_for_candidate(request, candidate)
    worker_service = _service_with_frozen_materials(
        service, frozen_snapshot)
    training_replay = _replay_candidate_scores(
        worker_service, candidate, request.objectives,
        tolerance=request.acceptable_normalized_error)
    if not (_same_number(quality["value"], training_replay["max_normalized_error"])
            and _same_number(quality["weighted_max_normalized_error"],
                             training_replay["weighted_max_normalized_error"])
            and _same_number(quality["weighted_rms_error"],
                             training_replay["weighted_rms_error"])
            and quality.get("targets_met") is
            training_replay["targets_met"]
            and quality.get("solver_acceptable") is training_replay["solver_acceptable"]):
        raise ValueError("checkpoint quality does not match the evaluated candidate")
    _validate_replayed_errors(
        candidate_payload["per_condition_errors"],
        training_replay["per_condition_errors"], "checkpoint")
    _validate_replayed_coverage(candidate_payload["raw_coverage"], training_replay["raw_coverage"], "checkpoint")
    if "solver_validation" in checkpoint:
        _validate_solver_validation(
            request, candidate, checkpoint["solver_validation"], state,
            worker_service)
    return deepcopy(dict(checkpoint))


def validate_completed_structure_cancel_result(payload: Mapping[str, Any]) -> None:
    """Validate current saved training/topology evidence without physical replay.

    candidate_verified remains false: this path does not claim an independent
    optical replay. The scheduler separately binds the complete worker reply.
    """
    from .film_optimization_structure import (film_structure_result_from_payload,
        validate_film_structure_candidate_topology)

    result = film_structure_result_from_payload(payload)
    request = result.frozen_request
    marker = result.topology_audit.get("cancel_snapshot")
    if (request is None or result.state != "cancelled" or result.candidate_verified is not False
            or marker != {"source": "worker_completed_reply", "replay_performed": False,
                          "candidate_available": True, "training_complete": True}):
        raise ValueError("structure cancel lacks an explicit complete worker snapshot")
    topology = validate_film_structure_candidate_topology(request, result.best_design,
        split_groups=result.topology_audit.get("split_groups", {}))
    if any(result.topology_audit.get(key) != item for key, item in topology.items()):
        raise ValueError("structure cancel topology does not match frozen hard bounds")
    if (tuple(result.added_needle_layer_ids) != tuple(topology["added_needle_layer_ids"])
            or dict(result.best_thickness_nm) != topology["layer_thickness_nm"]):
        raise ValueError("structure cancel layer IDs/thickness map do not match topology")

    def check_score(errors, coverage, objectives, maximum, weighted_maximum, rms,
                    targets_met, solver_acceptable):
        from types import SimpleNamespace
        from .film_nominal_acceptance import complete_original_score

        score = {"errors": errors, "coverage": coverage, "max_error": maximum,
                 "weighted_max_error": weighted_maximum, "weighted_rms_error": rms}
        criteria_request = SimpleNamespace(
            objectives=objectives,
            acceptable_normalized_error=request.base_request.acceptable_normalized_error,
        )
        if not complete_original_score(criteria_request, score):
            raise ValueError("structure cancel saved conditions or raw grid differ from frozen objectives")
        reduced = reduce_objective_responses(
            objectives, coverage,
            tolerance=request.base_request.acceptable_normalized_error,
        )
        if (not _same_number(maximum, reduced.max_error) or
                not _same_number(weighted_maximum, reduced.weighted_max_error) or
                not _same_number(rms, reduced.weighted_rms_error) or
                targets_met is not reduced.nominal_pass or
                solver_acceptable is not reduced.solver_acceptable):
            raise ValueError("structure cancel aggregate score is inconsistent")
        _validate_replayed_errors([asdict(row) for row in errors],
                                 [asdict(row) for row in reduced.conditions], "structure cancel")

    base = result.base_result
    check_score(base.per_condition_errors, base.raw_coverage, request.base_request.objectives,
                base.max_normalized_error, base.weighted_max_normalized_error,
                base.weighted_rms_error, base.targets_met, base.solver_acceptable)
    if base.validation_complete:
        errors = base.validation_per_condition_errors
        check_score(errors, base.validation_raw_coverage, _validation_objectives(request.base_request),
                    base.validation_max_normalized_error,
                    max(error.weighted_normalized_violation for error in errors),
                    base.validation_weighted_rms_error, base.validation_targets_met,
                    base.validation_solver_acceptable)



def execute_film_optimization_job(
    service: Any,
    payload: Mapping[str, Any],
    *,
    cancel_event: Any,
    progress_callback: Callable[[Any], None] | None = None,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    enable_forward_cache: bool = False,
    performance_callback: Callable[[dict[str, Any]], None] | None = None,
    performance_options: Any = None,
    candidate_observer: Any = None,
    candidate_observer_factory: Any = None,
) -> dict[str, Any]:
    """Run the queue-side task against its frozen materials only.

    The scheduler adapter should map its cancel/progress/checkpoint hooks to
    these arguments. Checkpoints contain a complete best candidate and request
    snapshot; terminal output contains the same request and full condition data.
    """
    if type(enable_forward_cache) is not bool:
        raise TypeError("enable_forward_cache must be a bool")
    if performance_callback is not None and not callable(performance_callback):
        raise TypeError("performance_callback must be callable")
    if (not isinstance(payload, Mapping)
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != CONTRACT_VERSION
            or payload.get("kind") != JOB_KIND):
        raise ValueError("film optimization job kind/version is invalid")
    request_payload = payload.get("request", {})
    if (isinstance(request_payload, Mapping)
            and request_payload.get("kind") == "coat_sim.film_reduction_request"):
        from .film_reduction import (film_reduction_request_from_payload,
            film_reduction_result_to_payload, reduce_film_design)
        reduction_request = film_reduction_request_from_payload(request_payload)
        if reduction_request.base_request.frozen_material_snapshot is None:
            raise ValueError("queued reduction requires a frozen material snapshot")
        def save_reduction(checkpoint):
            if checkpoint_callback is not None:
                checkpoint_callback(film_reduction_result_to_payload(reduction_request, checkpoint))
        reduction_result = reduce_film_design(service, reduction_request, cancel_event=cancel_event,
            progress_callback=progress_callback,
            checkpoint_callback=save_reduction if checkpoint_callback is not None else None)
        # The core archives only complete original+independent grid evaluations.
        # A terminal replay would add optical work after cancel or failure.
        return film_reduction_result_to_payload(reduction_request, reduction_result)
    if (isinstance(request_payload, Mapping)
            and request_payload.get("kind") == STRUCTURE_REQUEST_KIND):
        from .film_optimization_structure import (
            film_structure_checkpoint_to_payload,
            film_structure_request_from_payload,
            film_structure_result_to_payload,
            freeze_film_structure_optimization_request,
            optimize_film_design_with_structure,
            validate_film_structure_payload,
        )

        request = film_structure_request_from_payload(request_payload)
        if request.base_request.frozen_material_snapshot is None:
            raise ValueError("queued structure optimization requires a frozen material snapshot")
        request = freeze_film_structure_optimization_request(service, request)
        observer = (candidate_observer_factory(request.base_request) if candidate_observer_factory is not None
                    else candidate_observer)
        frozen_snapshot = deepcopy(dict(request.base_request.frozen_material_snapshot))
        frozen_service = _service_with_frozen_materials(service, frozen_snapshot)

        def save_structure_incumbent(checkpoint: Any) -> None:
            if checkpoint_callback is None:
                return
            checkpoint_callback(film_structure_checkpoint_to_payload(request, checkpoint))

        result = optimize_film_design_with_structure(
            frozen_service, request, cancel_event=cancel_event,
            progress_callback=progress_callback,
            checkpoint_callback=(save_structure_incumbent
                                if checkpoint_callback is not None else None),
            candidate_observer=observer,
        )
        result_payload = film_structure_result_to_payload(request, result)
        if cancel_event is not None and cancel_event.is_set():
            if not result.base_result.per_condition_errors:
                raise InterruptedError("Cancelled before a complete structure candidate was scored")
            from .film_optimization_structure import validate_film_structure_candidate_topology
            # Keep the core's actual final/audit/verified flags. Only complete
            # saved scores and recomputed hard-bound topology gain this marker.
            topology = validate_film_structure_candidate_topology(request, result.best_design,
                split_groups=result.topology_audit.get("split_groups", {}))
            topology = {**topology, "cancel_snapshot": {"source": "worker_completed_reply",
                "replay_performed": False, "candidate_available": True, "training_complete": True}}
            if result.state == "completed":
                # The search genuinely completed first. Cancellation wins only
                # the task envelope; all candidate/audit/final facts stay intact.
                result_payload["state"] = "cancelled"
                result_payload["message"] = ("search completed before cancellation accepted; "
                    "complete candidate retained without physical replay")
                topology["core_terminal_state_before_cancel"] = "completed"
            result_payload["structure"]["topology_audit"] = topology
            validate_completed_structure_cancel_result(result_payload)
            return result_payload
        return validate_film_structure_payload(frozen_service, result_payload)

    request = deserialize_request(request_payload)
    if request.frozen_material_snapshot is None:
        raise ValueError("queued film optimization requires a frozen material snapshot")
    request = freeze_film_optimization_request(service, request)
    observer = (candidate_observer_factory(request) if candidate_observer_factory is not None
                else candidate_observer)
    frozen_snapshot = deepcopy(dict(request.frozen_material_snapshot))
    frozen_service = _service_with_frozen_materials(service, frozen_snapshot)
    if enable_forward_cache and request.execution_mode == "serial":
        from .film_optimization_forward_cache import create_worker_forward_service

        frozen_service = create_worker_forward_service(
            frozen_service, frozen_snapshot,
            performance_callback=performance_callback,
        )

    def save_incumbent(checkpoint: dict[str, Any]) -> None:
        if checkpoint_callback is None:
            return
        checkpoint_callback(serialize_checkpoint(request, checkpoint))

    result = optimize_film_design(
        frozen_service, request, cancel_event=cancel_event,
        progress_callback=progress_callback,
        checkpoint_callback=save_incumbent if checkpoint_callback is not None else None,
        performance_options=performance_options, performance_callback=performance_callback,
        candidate_observer=observer,
    )
    return serialize_result(request, result)
