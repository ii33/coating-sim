"""Bounded finite-thickness, needle-like structure trials for film designs.

This is a public heuristic around the existing thickness optimizer.  It is not
a zero-thickness needle sensitivity method and it does not implement any
proprietary deposition or optimization algorithm.
"""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from typing import Any, Callable, Mapping

import numpy as np

from .film_design import MAX_LAYERS, physical_thickness_nm, validate_design
from .film_optimization import (
    FilmConditionError,
    FilmOptimizationObjective,
    FilmOptimizationProgress,
    FilmOptimizationRequest,
    FilmOptimizationResult,
    FilmThicknessVariable,
    _checked_frozen_snapshot,
    _validate_optimization_controls,
    _wavelength_chunks,
    freeze_film_optimization_request,
    optimize_film_design,
)
from .optical_constants import prepare_optical_set
from .film_optimization_scoring import candidate_key, score_condition, weighted_rms_error


@dataclass(frozen=True)
class FilmNeedleMaterialReference:
    """One explicitly selected material and optical-set pair for trial layers."""

    material_id: str
    optical_set_id: str


@dataclass(frozen=True)
class FilmNeedleOptimizationRequest:
    """Immutable structure-search request wrapped around the thickness contract.

    ``min_thickness_nm`` and ``max_thickness_nm`` constrain inserted layers
    only. Existing thickness-variable bounds remain owned by ``base_request``.
    ``max_added_layers`` bounds sequential accepted insertions, not a
    single-cycle structure-search mode; all rounds share the base budgets.
    """

    base_request: FilmOptimizationRequest
    candidate_materials: tuple[FilmNeedleMaterialReference, ...]
    max_added_layers: int = 1
    max_total_layers: int = MAX_LAYERS
    min_thickness_nm: float = 0.5
    max_thickness_nm: float = 200.0
    insertion_trials_per_interface: int = 1
    seed: int = 0


@dataclass(frozen=True)
class FilmNeedleOptimizationResult:
    """Best exact-scored result plus aggregate structure-search metadata."""

    base_result: FilmOptimizationResult
    added_layer_ids: tuple[str, ...]
    accepted_insertions: int
    rolled_back_insertions: int
    failed_evaluations: int
    structure_changed: bool
    message: str

    @property
    def state(self) -> str:
        return self.base_result.state

    @property
    def design_revision(self) -> int:
        return self.base_result.design_revision

    @property
    def best_design(self) -> Mapping[str, Any]:
        return self.base_result.best_design

    @property
    def best_thickness_nm(self) -> Mapping[str, float]:
        return self.base_result.best_thickness_nm

    @property
    def max_normalized_error(self) -> float:
        return self.base_result.max_normalized_error

    @property
    def weighted_max_normalized_error(self) -> float:
        return self.base_result.weighted_max_normalized_error

    @property
    def weighted_rms_error(self) -> float:
        return self.base_result.weighted_rms_error

    @property
    def targets_met(self) -> bool:
        return self.base_result.targets_met

    @property
    def per_condition_errors(self) -> tuple[FilmConditionError, ...]:
        return self.base_result.per_condition_errors

    @property
    def iterations(self) -> int:
        return self.base_result.iterations

    @property
    def evaluations(self) -> int:
        return self.base_result.evaluations

    @property
    def solver(self) -> str:
        return self.base_result.solver

    @property
    def seed(self) -> int:
        return self.base_result.seed

    @property
    def termination_reason(self) -> str:
        return self.base_result.termination_reason

    @property
    def validation_complete(self) -> bool:
        return self.base_result.validation_complete

    @property
    def validation_evaluations(self) -> int:
        return self.base_result.validation_evaluations

    @property
    def validation_max_normalized_error(self) -> float | None:
        return self.base_result.validation_max_normalized_error

    @property
    def validation_weighted_rms_error(self) -> float | None:
        return self.base_result.validation_weighted_rms_error

    @property
    def validation_targets_met(self) -> bool | None:
        return self.base_result.validation_targets_met

    @property
    def validation_per_condition_errors(self) -> tuple[FilmConditionError, ...]:
        return self.base_result.validation_per_condition_errors

    @property
    def validation_message(self) -> str:
        return self.base_result.validation_message


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _positive_int(value: Any, label: str, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        suffix = f" from 1 to {maximum}" if maximum is not None else ""
        raise ValueError(f"{label} must be a positive integer{suffix}")
    return value


def _validate_request(request: FilmNeedleOptimizationRequest) -> tuple[dict[str, Any], int]:
    if not isinstance(request, FilmNeedleOptimizationRequest):
        raise ValueError("request must be a FilmNeedleOptimizationRequest")
    base = request.base_request
    if not isinstance(base, FilmOptimizationRequest):
        raise ValueError("base_request must be a FilmOptimizationRequest")
    if not isinstance(base.design_snapshot, Mapping):
        raise ValueError("design_snapshot must be a design mapping")
    _validate_optimization_controls(base)
    design = validate_design(deepcopy(dict(base.design_snapshot)))
    if type(base.design_revision) is not int or base.design_revision < 0:
        raise ValueError("design_revision must be a nonnegative integer")
    if design["substrate"]["sides"] != "single" or design.get("first_surface", "front") != "front":
        raise ValueError("needle-like trials support only a single-sided front-surface design")
    if not isinstance(base.objectives, (tuple, list)) or not base.objectives:
        raise ValueError("at least one optical objective is required")
    if not isinstance(base.variables, (tuple, list)):
        raise ValueError("base variables must be a sequence")

    max_added = _positive_int(request.max_added_layers, "max_added_layers", MAX_LAYERS)
    max_total = _positive_int(request.max_total_layers, "max_total_layers", MAX_LAYERS)
    current_count = len(design["front_layers"])
    if current_count >= max_total:
        raise ValueError("max_total_layers leaves no room for an inserted layer")
    effective_added = min(max_added, max_total - current_count,
                          MAX_LAYERS - current_count)

    if not isinstance(request.candidate_materials, (tuple, list)) or not request.candidate_materials:
        raise ValueError("at least one candidate material optical-set reference is required")
    if len(request.candidate_materials) > 64:
        raise ValueError("at most 64 candidate material optical-set references are supported")
    seen_refs: set[tuple[str, str]] = set()
    for reference in request.candidate_materials:
        if not isinstance(reference, FilmNeedleMaterialReference):
            raise ValueError("candidate_materials must contain FilmNeedleMaterialReference values")
        for value, label in ((reference.material_id, "material_id"),
                             (reference.optical_set_id, "optical_set_id")):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError(f"candidate {label} must be nonempty text no longer than 128 characters")
        key = (reference.material_id, reference.optical_set_id)
        if key in seen_refs:
            raise ValueError("candidate material optical-set references must be unique")
        seen_refs.add(key)

    minimum = _finite(request.min_thickness_nm, "min_thickness_nm")
    maximum = _finite(request.max_thickness_nm, "max_thickness_nm")
    if minimum <= 0 or maximum <= minimum:
        raise ValueError("inserted-layer thickness bounds must be positive and increasing")
    _positive_int(request.insertion_trials_per_interface,
                  "insertion_trials_per_interface")
    if type(request.seed) is not int or not 0 <= request.seed <= 0xFFFFFFFF:
        raise ValueError("seed must be an integer from 0 to 4294967295")
    budget = _positive_int(base.max_evaluations, "max_evaluations")
    _positive_int(base.max_iterations, "max_iterations")
    _positive_int(
        base.validation_samples_multiplier, "validation_samples_multiplier")
    _normalize_objectives(base.objectives)

    # One exact baseline, every first-round interface/material/trial probe, and
    # at least one optimizer evaluation must fit in the shared budget.
    first_round_probes = ((current_count + 1) * len(request.candidate_materials) *
                          request.insertion_trials_per_interface)
    if budget < first_round_probes + 2:
        raise ValueError("max_evaluations is too small for a baseline and every first-round insertion probe")
    return design, effective_added


def _candidate_payload_from_materials(
    service: Any,
    payload: Mapping[str, Any],
    references: tuple[FilmNeedleMaterialReference, ...] | list[FilmNeedleMaterialReference],
) -> dict[str, Any]:
    """Bind explicitly selected candidate sets into the frozen material payload."""
    result = deepcopy(dict(payload))
    rows = result.get("materials")
    if not isinstance(rows, list):
        raise ValueError("frozen material snapshot list is invalid")
    by_id = {row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    for reference in references:
        current = by_id.get(reference.material_id)
        if current is not None and current.get("design_ready") is False:
            raise ValueError(f"candidate material is not marked design-ready: {reference.material_id}")
        optical = next((row for row in (current or {}).get("optical_sets", [])
                        if isinstance(row, dict) and row.get("id") == reference.optical_set_id), None)
        if optical is None:
            material = _get_material(service, reference.material_id)
            if material.get("design_ready") is False:
                raise ValueError(f"candidate material is not marked design-ready: {reference.material_id}")
            optical = next((row for row in material.get("optical_sets", [])
                            if isinstance(row, dict) and row.get("id") == reference.optical_set_id), None)
            if optical is None:
                raise ValueError(
                    f"candidate optical set is missing: {reference.material_id}/{reference.optical_set_id}")
            try:
                prepare_optical_set(optical)
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(
                    f"candidate optical set is invalid: {reference.material_id}/{reference.optical_set_id}") from exc
            if current is None:
                current = {
                    "id": reference.material_id,
                    "name": material.get("name", reference.material_id),
                    "optical_sets": [],
                    "design_ready": material.get("design_ready"),
                    "notes": material.get("notes", ""),
                }
                rows.append(current)
                by_id[reference.material_id] = current
            current.setdefault("optical_sets", []).append(deepcopy(optical))
        else:
            # A pre-frozen pair still needs a capability parse check. It avoids
            # any live catalog access when this request is reused by a worker.
            try:
                prepare_optical_set(optical)
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(
                    f"candidate optical set is invalid: {reference.material_id}/{reference.optical_set_id}") from exc
    # A candidate may share an existing material row. Normalize set ordering
    # and protect against duplicate records before computing the frozen digest.
    for material in rows:
        sets = material.get("optical_sets", [])
        deduplicated = {row["id"]: row for row in sets}
        material["optical_sets"] = [deduplicated[key] for key in sorted(deduplicated)]
    result.pop("sha256", None)
    identity = json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    result["sha256"] = hashlib.sha256(identity).hexdigest()
    return result


def _get_material(service: Any, material_id: str) -> dict[str, Any]:
    materials = getattr(service, "materials", None)
    if materials is None:
        raise ValueError("service does not expose a material catalog")
    if callable(getattr(materials, "get", None)):
        value = materials.get(material_id)
    elif callable(getattr(materials, "_records", None)):
        value = next((row for row in materials._records() if row.get("id") == material_id), None)
    else:
        raise ValueError("material catalog does not support lookup")
    if not isinstance(value, dict) or value.get("id") != material_id:
        raise ValueError(f"candidate material is missing: {material_id}")
    if not isinstance(value.get("optical_sets"), list):
        raise ValueError(f"candidate material optical-set list is invalid: {material_id}")
    return deepcopy(value)


def freeze_film_optimization_needle_request(
    service: Any,
    request: FilmNeedleOptimizationRequest,
) -> FilmNeedleOptimizationRequest:
    """Freeze the base design and all candidate material sets before submission.

    A returned request can be serialized and reused without reading the live
    material catalog, provided its digest remains valid.
    """
    design, _effective_added = _validate_request(request)
    base = freeze_film_optimization_request(service, request.base_request)
    payload = _checked_frozen_snapshot(base.frozen_material_snapshot, design)
    payload = _candidate_payload_from_materials(service, payload, request.candidate_materials)
    # Recheck the full digest and exact design after extending the materials.
    payload = _checked_frozen_snapshot(payload, design)
    return replace(request, base_request=replace(
        base, design_snapshot=deepcopy(payload["design"]), frozen_material_snapshot=payload))


def _worker_for_snapshot(service: Any, frozen: Mapping[str, Any]) -> Any:
    class FrozenMaterials:
        def __init__(self, records: list[dict[str, Any]]) -> None:
            self._records_by_id = {row["id"]: deepcopy(row) for row in records}

        def get(self, identifier: str) -> dict[str, Any]:
            if identifier not in self._records_by_id:
                raise ValueError(f"frozen needle materials missing: {identifier}")
            return deepcopy(self._records_by_id[identifier])

    worker = copy(service)
    worker.materials = FrozenMaterials(list(frozen["materials"]))
    return worker


def _normalize_objectives(
    objectives: tuple[FilmOptimizationObjective, ...] | list[FilmOptimizationObjective],
) -> tuple[FilmOptimizationObjective, ...]:
    normalized: list[FilmOptimizationObjective] = []
    for objective in objectives:
        if not isinstance(objective, FilmOptimizationObjective):
            raise ValueError("objectives must contain FilmOptimizationObjective values")
        low = _finite(objective.wavelength_min_nm, "wavelength_min_nm")
        high = _finite(objective.wavelength_max_nm, "wavelength_max_nm")
        if low <= 0 or high < low:
            raise ValueError("objective wavelengths must be positive and ordered")
        samples = _positive_int(objective.wavelength_samples, "wavelength_samples")
        if high == low:
            samples = 1
        elif samples < 2:
            raise ValueError("wavelength_samples must be at least 2")
        if not isinstance(objective.angles_deg, (tuple, list)) or not objective.angles_deg:
            raise ValueError("each objective needs at least one angle sample")
        angles = tuple(_finite(angle, "angle_deg") for angle in objective.angles_deg)
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
        weight = _finite(objective.weight, "weight")
        scale = _finite(objective.normalization_scale, "normalization_scale")
        if weight <= 0 or scale <= 0:
            raise ValueError("objective weight and normalization_scale must be positive")
        if objective.mode == "value":
            if objective.target_value is None or objective.lower_bound is not None or objective.upper_bound is not None:
                raise ValueError("value objectives require target_value and no bounds")
            target = _finite(objective.target_value, "target_value")
            if not 0 <= target <= 1:
                raise ValueError("R/T target_value must be in [0, 1]")
            normalized.append(FilmOptimizationObjective(
                low, high, samples, angles, objective.polarization, objective.metric,
                "value", target, None, None, weight, scale))
        else:
            if objective.target_value is not None or (
                    objective.lower_bound is None and objective.upper_bound is None):
                raise ValueError("range objectives require one or both bounds and no target_value")
            lower = None if objective.lower_bound is None else _finite(objective.lower_bound, "lower_bound")
            upper = None if objective.upper_bound is None else _finite(objective.upper_bound, "upper_bound")
            if ((lower is not None and not 0 <= lower <= 1) or
                    (upper is not None and not 0 <= upper <= 1) or
                    (lower is not None and upper is not None and lower > upper)):
                raise ValueError("R/T range bounds must lie in [0, 1] and be ordered")
            normalized.append(FilmOptimizationObjective(
                low, high, samples, angles, objective.polarization, objective.metric,
                "range", None, lower, upper, weight, scale))
    return tuple(normalized)


def _score_design(
    worker: Any,
    design: dict[str, Any],
    objectives: tuple[FilmOptimizationObjective, ...],
    *,
    acceptable: float = 1e-3,
    cancel_event: Any = None,
) -> dict[str, Any]:
    errors: list[FilmConditionError] = []
    normalized_errors: list[float] = []
    condition_weights: list[float] = []
    max_error = 0.0
    weighted_max_error = 0.0
    for objective_index, objective in enumerate(objectives):
        waves = np.linspace(objective.wavelength_min_nm,
                            objective.wavelength_max_nm,
                            objective.wavelength_samples)
        for angle in objective.angles_deg:
            for chunk_waves in _wavelength_chunks(waves):
                if cancel_event is not None and cancel_event.is_set():
                    raise _NeedleValidationCancelled()
                response = worker.simulate(design, {
                    "wavelength_min_nm": float(chunk_waves[0]),
                    "wavelength_max_nm": float(chunk_waves[-1]),
                    "samples": int(len(chunk_waves)),
                    "angle_deg": angle,
                    "polarization": objective.polarization,
                    "first_surface": "front",
                })
                actual_values = np.asarray(response[objective.metric], dtype=float)
                actual_waves = np.asarray(response["wavelength_nm"], dtype=float)
                if (actual_values.shape != chunk_waves.shape or actual_waves.shape != chunk_waves.shape or
                        not np.all(np.isfinite(actual_values)) or
                        not np.allclose(actual_waves, chunk_waves, rtol=0, atol=1e-9)):
                    raise ValueError("film simulation returned invalid objective samples")
                for wavelength, actual_value in zip(chunk_waves, actual_values):
                    actual = float(actual_value)
                    if objective.mode == "value":
                        violation = abs(actual - float(objective.target_value))
                    else:
                        violation = max(
                            0.0,
                            (float(objective.lower_bound) - actual
                             if objective.lower_bound is not None else 0.0),
                            (actual - float(objective.upper_bound)
                             if objective.upper_bound is not None else 0.0),
                        )
                    normalized_error, weighted_error = score_condition(
                        violation, objective.normalization_scale, objective.weight)
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
                if cancel_event is not None and cancel_event.is_set():
                    raise _NeedleValidationCancelled()
    weighted_rms = weighted_rms_error(normalized_errors, condition_weights)
    return {
        "design": deepcopy(design),
        "errors": tuple(errors),
        "max_error": max_error,
        "weighted_max_error": weighted_max_error,
        "weighted_rms_error": weighted_rms,
        "key": candidate_key(max_error, weighted_rms, acceptable),
    }


class _NeedleValidationCancelled(Exception):
    """Internal signal that a complete holdout condition was interrupted."""


def _result_from_score(
    score: Mapping[str, Any],
    request: FilmOptimizationRequest,
    *,
    acceptable: float,
    evaluations: int,
    iterations: int,
    state: str = "completed",
    message: str = "",
    thicknesses: Mapping[str, float] | None = None,
) -> FilmOptimizationResult:
    max_error = float(score["max_error"])
    if not message:
        message = ("all sampled conditions meet the requested normalized error"
                   if max_error <= acceptable else
                   "bounded structure trial retained the exact-scored candidate")
    return FilmOptimizationResult(
        state=state, design_revision=request.design_revision,
        best_design=deepcopy(score["design"]),
        best_thickness_nm=dict(thicknesses or {}),
        max_normalized_error=max_error,
        weighted_max_normalized_error=float(score["weighted_max_error"]),
        weighted_rms_error=float(score["weighted_rms_error"]),
        targets_met=bool(max_error <= acceptable),
        per_condition_errors=tuple(score["errors"]),
        iterations=iterations, evaluations=evaluations, message=message,
    )


def _score_key(result: FilmOptimizationResult) -> tuple[float, float]:
    return (0.0 if result.targets_met else 1.0, result.weighted_rms_error)


def _deterministic_layer_id(
    design_id: str,
    revision: int,
    added_index: int,
    material: FilmNeedleMaterialReference,
    interface: int,
    trial: int,
    seed: int,
    existing_ids: set[str],
) -> str:
    token = json.dumps([
        design_id, revision, added_index, material.material_id,
        material.optical_set_id, interface, trial, seed,
    ], ensure_ascii=False, separators=(",", ":"))
    base = "needle-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:20]
    identifier = base
    suffix = 1
    while identifier in existing_ids:
        identifier = f"{base}-{suffix}"
        suffix += 1
    return identifier


def _insert_trial(
    design: Mapping[str, Any],
    reference: FilmNeedleMaterialReference,
    interface: int,
    thickness_nm: float,
    layer_id: str,
) -> dict[str, Any]:
    candidate = deepcopy(dict(design))
    layer = {
        "id": layer_id,
        "material_id": reference.material_id,
        "optical_set_id": reference.optical_set_id,
        "thickness": {"value": float(thickness_nm), "unit": "nm"},
        "note": "Finite-thickness needle-like structure-search trial.",
    }
    candidate["front_layers"].insert(interface, layer)
    expression = candidate.get("expression", {})
    if expression.get("text") and expression.get("in_sync"):
        expression["in_sync"] = False
    return validate_design(candidate)


def _physical_layer_thickness(
    layer: Mapping[str, Any], design: Mapping[str, Any], frozen: Mapping[str, Any],
) -> float:
    thickness = layer["thickness"]
    if thickness["unit"] != "qw":
        return physical_thickness_nm(layer, design["reference_wavelength_nm"], 1.0)
    material = next((row for row in frozen["materials"]
                     if row["id"] == layer["material_id"]), None)
    optical = next((row for row in (material or {}).get("optical_sets", [])
                    if row["id"] == layer["optical_set_id"]), None)
    if optical is None:
        raise ValueError(f"layer optical data set is missing: {layer['id']}")
    n_value, _k, _details = prepare_optical_set(optical)(
        design["reference_wavelength_nm"], "nm", include_k=False)
    return physical_thickness_nm(layer, design["reference_wavelength_nm"], float(n_value))


def _thicknesses_for_request(
    request: FilmOptimizationRequest,
    design: Mapping[str, Any],
    frozen: Mapping[str, Any],
) -> dict[str, float]:
    result = {}
    for variable in request.variables:
        layer = next((row for row in design[variable.side + "_layers"]
                      if row["id"] == variable.layer_id), None)
        if layer is not None:
            result[layer["id"]] = _physical_layer_thickness(layer, design, frozen)
    return result


def optimize_film_design_with_needle(
    service: Any,
    request: FilmNeedleOptimizationRequest,
    *,
    cancel_event: Any = None,
    progress_callback: Callable[[FilmOptimizationProgress], None] | None = None,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
) -> FilmNeedleOptimizationResult:
    """Search bounded, finite-thickness insertions and refine the best proposal.

    All evaluations use a frozen design/material snapshot. The shared limits
    are ``base_request.max_evaluations`` and ``base_request.max_iterations``;
    cancellation is checked between complete candidates. A rejected or failed
    trial leaves the retained incumbent unchanged.
    """
    design, effective_added = _validate_request(request)
    if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
        raise ValueError("cancel_event must provide is_set()")
    frozen_request = freeze_film_optimization_needle_request(service, request)
    base_request = frozen_request.base_request
    frozen = _checked_frozen_snapshot(base_request.frozen_material_snapshot, design)
    objectives = _normalize_objectives(base_request.objectives)
    worker = _worker_for_snapshot(service, frozen)
    total_evaluations_limit = base_request.max_evaluations
    total_iterations_limit = base_request.max_iterations
    candidate_count = len(request.candidate_materials)
    trial_count = request.insertion_trials_per_interface

    def cancelled() -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    total_evaluations = 0
    total_iterations = 0
    failed_evaluations = 0
    validation_evaluations_total = 0
    accepted_ids: list[str] = []
    rolled_back = 0
    state = "completed"
    terminal_reason = ""
    incumbent: FilmOptimizationResult | None = None

    def emit(progress_state: str, message: str = "") -> None:
        if progress_callback is None:
            return
        fraction = min(1.0, total_evaluations / total_evaluations_limit)
        progress_callback(FilmOptimizationProgress(
            state=progress_state,
            fraction=(1.0 if progress_state in ("completed", "cancelled") else fraction),
            iteration=total_iterations,
            evaluations=total_evaluations,
            best_max_normalized_error=(None if incumbent is None else
                                       float(incumbent.max_normalized_error)),
            message=message,
        ))

    emit("validating", "validating front-surface scope and frozen candidate materials")

    # Reserve enough evaluations for every first-round proposal plus one
    # refinement. The base optimizer may use the remainder for a baseline fit.
    initial_probe_count = (len(design["front_layers"]) + 1) * candidate_count * trial_count
    baseline_evaluation_allowance = max(1, total_evaluations_limit - initial_probe_count - 1)
    baseline_iteration_allowance = max(1, total_iterations_limit - 1)
    if base_request.variables:
        baseline_request = replace(
            base_request,
            max_evaluations=min(base_request.max_evaluations, baseline_evaluation_allowance),
            max_iterations=min(base_request.max_iterations, baseline_iteration_allowance),
        )

        def relay_baseline(item: FilmOptimizationProgress) -> None:
            if progress_callback is not None:
                progress_callback(replace(
                    item,
                    evaluations=total_evaluations + item.evaluations,
                    iteration=total_iterations + item.iteration,
                    fraction=min(1.0, (total_evaluations + item.evaluations) /
                                 total_evaluations_limit),
                ))

        baseline_result = optimize_film_design(
            service, baseline_request, cancel_event=cancel_event,
            progress_callback=relay_baseline,
        )
        total_evaluations += baseline_result.evaluations
        total_iterations += baseline_result.iterations
        failed_evaluations += int(getattr(baseline_result, "failed_evaluations", 0) or 0)
        validation_evaluations_total += int(
            getattr(baseline_result, "validation_evaluations", 0) or 0)
        incumbent = replace(
            baseline_result,
            evaluations=total_evaluations,
            iterations=total_iterations,
        )
        state = baseline_result.state
        validation_source: FilmOptimizationResult | None = baseline_result
        if state == "cancelled":
            terminal_reason = "cancelled after a complete baseline evaluation; best candidate retained"
    else:
        try:
            baseline_score = _score_design(
                worker, design, objectives,
                acceptable=base_request.acceptable_normalized_error)
        except (ValueError, ArithmeticError, FloatingPointError):
            raise
        total_evaluations += 1
        incumbent = _result_from_score(
            baseline_score, base_request,
            acceptable=base_request.acceptable_normalized_error,
            evaluations=total_evaluations, iterations=0,
            message="exact baseline evaluated; no base thickness variables were selected",
        )
        validation_source = None
        if cancelled():
            state = "cancelled"
            terminal_reason = "cancelled after a complete baseline evaluation; best candidate retained"

    assert incumbent is not None

    rng = np.random.default_rng(request.seed)
    for added_index in range(effective_added):
        if state == "cancelled" or cancelled():
            state = "cancelled"
            terminal_reason = terminal_reason or "cancelled between complete candidate evaluations; best candidate retained"
            break
        current_design = validate_design(deepcopy(dict(incumbent.best_design)))
        layer_count = len(current_design["front_layers"])
        round_probe_count = (layer_count + 1) * candidate_count * trial_count
        remaining = total_evaluations_limit - total_evaluations
        reserve_for_refinement = 1
        if round_probe_count + reserve_for_refinement > remaining:
            terminal_reason = "evaluation budget reached before all interfaces in the next insertion round could be probed"
            break

        trials: list[dict[str, Any]] = []
        trial_position = 0
        existing_ids = {layer["id"] for layer in current_design["front_layers"]}
        for interface in range(layer_count + 1):
            for reference in request.candidate_materials:
                for trial_index in range(trial_count):
                    if cancelled():
                        state = "cancelled"
                        terminal_reason = "cancelled between complete candidate evaluations; best candidate retained"
                        break
                    if trial_index == 0:
                        thickness_nm = request.min_thickness_nm
                    else:
                        thickness_nm = float(rng.uniform(
                            request.min_thickness_nm, request.max_thickness_nm))
                    layer_id = _deterministic_layer_id(
                        design["id"], base_request.design_revision, added_index,
                        reference, interface, trial_index, request.seed, existing_ids,
                    )
                    trial_design = _insert_trial(
                        current_design, reference, interface, thickness_nm, layer_id)
                    try:
                        score = _score_design(
                            worker, trial_design, objectives,
                            acceptable=base_request.acceptable_normalized_error)
                    except (ValueError, ArithmeticError, FloatingPointError):
                        total_evaluations += 1
                        failed_evaluations += 1
                        rolled_back += 1
                        trial_position += 1
                        emit("running", "an invalid insertion trial was rolled back")
                        if cancelled():
                            state = "cancelled"
                            terminal_reason = "cancelled after a complete candidate evaluation; best candidate retained"
                            break
                        continue
                    total_evaluations += 1
                    trial = {
                        "score": score,
                        "design": trial_design,
                        "layer_id": layer_id,
                        "reference": reference,
                        "thickness_nm": thickness_nm,
                    }
                    trials.append(trial)
                    trial_position += 1
                    emit("running", f"probed insertion interface {interface + 1}/{layer_count + 1}")
                    if cancelled():
                        state = "cancelled"
                        terminal_reason = "cancelled after a complete candidate evaluation; best candidate retained"
                        break
                if state == "cancelled":
                    break
            if state == "cancelled":
                break

        if state == "cancelled":
            rolled_back += len(trials)
            break

        improving = []
        for trial in trials:
            score_key = trial["score"]["key"]
            if score_key < _score_key(incumbent):
                improving.append(trial)
        if not improving:
            rolled_back += len(trials)
            if state != "cancelled":
                terminal_reason = "no finite-thickness insertion improved the exact baseline objective; proposals rolled back"
            break
        best_trial = min(improving, key=lambda trial: (
            *trial["score"]["key"],
            trial["layer_id"],
        ))
        rolled_back += max(0, len(trials) - 1)
        probe_result = _result_from_score(
            best_trial["score"], base_request,
            acceptable=base_request.acceptable_normalized_error,
            evaluations=total_evaluations,
            iterations=total_iterations,
            message="finite-thickness insertion probe improved the exact objective",
            thicknesses={**dict(incumbent.best_thickness_nm), best_trial["layer_id"]:
                         float(best_trial["thickness_nm"])},
        )

        selected_new_variables = tuple(
            FilmThicknessVariable("front", identifier,
                                  request.min_thickness_nm, request.max_thickness_nm)
            for identifier in [*accepted_ids, best_trial["layer_id"]]
        )
        selected_variables = tuple(base_request.variables) + selected_new_variables
        refined_result: FilmOptimizationResult | None = None
        remaining_evaluations = total_evaluations_limit - total_evaluations
        remaining_iterations = total_iterations_limit - total_iterations
        next_round_probes = 0
        if added_index + 1 < effective_added:
            next_round_probes = (len(best_trial["design"]["front_layers"]) + 1) * candidate_count * trial_count
        refinement_cap = remaining_evaluations - next_round_probes
        if refinement_cap >= 1 and remaining_iterations > 0 and not cancelled():
            refinement_base = replace(
                base_request,
                design_snapshot=deepcopy(best_trial["design"]),
                variables=selected_variables,
                max_evaluations=refinement_cap,
                max_iterations=remaining_iterations,
                frozen_material_snapshot=deepcopy(frozen),
            )
            refinement_base = replace(
                refinement_base,
                frozen_material_snapshot=_retarget_frozen_snapshot(
                    frozen, best_trial["design"]),
            )

            def relay_refinement(item: FilmOptimizationProgress) -> None:
                if progress_callback is not None:
                    progress_callback(replace(
                        item,
                        evaluations=total_evaluations + item.evaluations,
                        iteration=total_iterations + item.iteration,
                        fraction=min(1.0, (total_evaluations + item.evaluations) /
                                     total_evaluations_limit),
                    ))

            try:
                refined_result = optimize_film_design(
                    service, refinement_base, cancel_event=cancel_event,
                    progress_callback=relay_refinement,
                )
                total_evaluations += refined_result.evaluations
                total_iterations += refined_result.iterations
                failed_evaluations += int(getattr(refined_result, "failed_evaluations", 0) or 0)
                validation_evaluations_total += int(
                    getattr(refined_result, "validation_evaluations", 0) or 0)
                refined_result = replace(
                    refined_result,
                    evaluations=total_evaluations,
                    iterations=total_iterations,
                )
                if refined_result.state == "cancelled":
                    state = "cancelled"
                    terminal_reason = "cancelled after the current complete refinement evaluation; best candidate retained"
            except (ValueError, ArithmeticError, FloatingPointError):
                total_evaluations += 1
                failed_evaluations += 1
                rolled_back += 1
                refined_result = None

        if state == "cancelled":
            rolled_back += 1
            break

        if refined_result is not None and _score_key(refined_result) < _score_key(probe_result):
            accepted = replace(
                refined_result,
                evaluations=total_evaluations,
                iterations=total_iterations,
            )
        else:
            accepted = replace(
                probe_result,
                evaluations=total_evaluations,
                iterations=total_iterations,
                state=state,
            )
        if _score_key(accepted) < _score_key(incumbent):
            incumbent = accepted
            accepted_ids.append(best_trial["layer_id"])
            validation_source = (
                refined_result if refined_result is not None and
                refined_result.best_design == accepted.best_design and
                refined_result.validation_complete else None
            )
            if checkpoint_callback is not None:
                checkpoint_callback({
                    "schema_version": 1,
                    "kind": "coat_sim.film_optimization_checkpoint",
                    "design_revision": base_request.design_revision,
                    "best_design": deepcopy(incumbent.best_design),
                    "best_thickness_nm": dict(incumbent.best_thickness_nm),
                    "max_normalized_error": incumbent.max_normalized_error,
                    "weighted_max_normalized_error": incumbent.weighted_max_normalized_error,
                    "weighted_rms_error": incumbent.weighted_rms_error,
                    "targets_met": incumbent.targets_met,
                    "per_condition_errors": [asdict(row) for row in incumbent.per_condition_errors],
                    "iterations": total_iterations,
                    "evaluations": total_evaluations,
                    "failed_evaluations": failed_evaluations,
                    "needle_added_layer_ids": list(accepted_ids),
                })
            emit("running", "an improving insertion was accepted")
        if state == "cancelled" or cancelled():
            state = "cancelled"
            terminal_reason = terminal_reason or "cancelled after a complete candidate evaluation; best candidate retained"
            break

    validation_complete = False
    validation_max_error: float | None = None
    validation_weighted_rms: float | None = None
    validation_targets_met: bool | None = None
    validation_errors: tuple[FilmConditionError, ...] = ()
    validation_message = ""
    can_reuse_validation = bool(
        validation_source is not None and validation_source.validation_complete and
        validation_source.best_design == incumbent.best_design
    )
    if can_reuse_validation:
        validation_complete = True
        validation_max_error = validation_source.validation_max_normalized_error
        validation_weighted_rms = validation_source.validation_weighted_rms_error
        validation_targets_met = validation_source.validation_targets_met
        validation_errors = validation_source.validation_per_condition_errors
        validation_message = validation_source.validation_message
    elif state == "cancelled":
        validation_message = "holdout validation skipped because optimization was cancelled"
    else:
        multiplier = base_request.validation_samples_multiplier
        validation_objectives = tuple(
            replace(
                objective,
                wavelength_samples=(
                    1 if objective.wavelength_samples == 1 else
                    (objective.wavelength_samples - 1) * multiplier + 1
                ),
            )
            for objective in objectives
        )
        try:
            validation_score = _score_design(
                worker, deepcopy(dict(incumbent.best_design)), validation_objectives,
                acceptable=base_request.acceptable_normalized_error,
                cancel_event=cancel_event,
            )
        except _NeedleValidationCancelled:
            validation_evaluations_total += 1
            state = "cancelled"
            terminal_reason = "cancelled during final holdout validation; last complete candidate retained"
            validation_message = "holdout validation was cancelled before a complete grid was evaluated"
        except (ValueError, ArithmeticError, FloatingPointError) as exc:
            validation_evaluations_total += 1
            failed_evaluations += 1
            validation_message = f"holdout validation failed: {exc}"
        else:
            validation_evaluations_total += 1
            validation_complete = True
            validation_max_error = float(validation_score["max_error"])
            validation_weighted_rms = float(validation_score["weighted_rms_error"])
            validation_targets_met = bool(
                validation_max_error <= base_request.acceptable_normalized_error)
            validation_errors = tuple(validation_score["errors"])
            validation_message = (
                "all denser holdout conditions meet the requested normalized error"
                if validation_targets_met else
                "the denser holdout grid contains a condition above the requested normalized error"
            )

    if state != "cancelled" and not terminal_reason:
        if incumbent.targets_met:
            terminal_reason = "all sampled conditions meet the requested normalized error"
        elif total_evaluations >= total_evaluations_limit:
            terminal_reason = "shared evaluation budget reached; target attainment is not guaranteed"
        elif total_iterations >= total_iterations_limit:
            terminal_reason = "shared iteration budget reached; target attainment is not guaranteed"
        else:
            terminal_reason = "bounded needle-like structure search completed; target attainment is not guaranteed"

    termination_reason = (
        "cancelled" if state == "cancelled" else
        "evaluation_budget" if total_evaluations >= total_evaluations_limit else
        "iteration_limit" if total_iterations >= total_iterations_limit else
        "validation_failed" if not validation_complete else
        "target_met" if incumbent.targets_met else
        "no_improvement" if not accepted_ids else "converged"
    )
    incumbent = replace(
        incumbent,
        state=state,
        evaluations=total_evaluations,
        iterations=total_iterations,
        message=terminal_reason,
        solver=base_request.solver,
        seed=base_request.seed,
        termination_reason=termination_reason,
        failed_evaluations=failed_evaluations,
        validation_samples_multiplier=base_request.validation_samples_multiplier,
        validation_evaluations=validation_evaluations_total,
        validation_complete=validation_complete,
        validation_max_normalized_error=validation_max_error,
        validation_weighted_rms_error=validation_weighted_rms,
        validation_targets_met=validation_targets_met,
        validation_per_condition_errors=validation_errors,
        validation_message=validation_message,
    )
    emit(state, terminal_reason)
    return FilmNeedleOptimizationResult(
        base_result=incumbent,
        added_layer_ids=tuple(accepted_ids),
        accepted_insertions=len(accepted_ids),
        rolled_back_insertions=rolled_back,
        failed_evaluations=failed_evaluations,
        structure_changed=bool(accepted_ids),
        message=terminal_reason,
    )


def _retarget_frozen_snapshot(
    frozen: Mapping[str, Any],
    design: Mapping[str, Any],
) -> dict[str, Any]:
    payload = deepcopy(dict(frozen))
    payload.pop("sha256", None)
    payload["design"] = validate_design(deepcopy(dict(design)))
    identity = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    payload["sha256"] = hashlib.sha256(identity).hexdigest()
    return payload
