"""Finite, replayable classical needle search for thin-film structures.

The zero-thickness layer is used only by the analytic sensitivity probe. Every
persisted candidate has positive layer thicknesses and is checked against the
request's per-layer, total-thickness, and layer-count limits before simulation.
The structural search mode is independent of the legacy thickness solver ID.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from typing import Any, Callable, Literal, Mapping, Sequence

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
    freeze_film_optimization_request,
)
from .film_optimization_derivatives import _PreparedMaterialData, simulate_with_thickness_jacobian
from .film_optimization_needle import (
    FilmNeedleMaterialReference,
    _candidate_payload_from_materials,
    _normalize_objectives,
    _physical_layer_thickness,
    _result_from_score,
)
from .film_optimization_scoring import candidate_key, score_condition, weighted_rms_error
from .optical_constants import prepare_optical_set


StructureMode = Literal["needle_only", "growth_only", "combined"]


@dataclass(frozen=True)
class FilmStructureOptimizationRequest:
    """Finite structure-search contract around a frozen thickness request.

    ``position_samples`` is the number of interior probe points per existing
    layer. All existing layer interfaces are also probed. A candidate needle
    inside a host layer splits that row into two same-material positive rows;
    the original layer ID stays on the left row and the right row gets a stable
    generated ID. ``max_added_layers`` counts candidate-material needle rows;
    ``max_total_layers`` counts every persisted row, including a split host.

    Local refinement moves only the explicitly selected
    ``base_request.variables`` plus the new needle or grown row. Every other row
    remains fixed. The selected rows retain the intersection of their original
    nm bounds with the structure-wide layer bounds.
    """

    base_request: FilmOptimizationRequest
    candidate_materials: tuple[FilmNeedleMaterialReference, ...] = ()
    mode: StructureMode = "combined"
    max_total_thickness_nm: float = 2000.0
    min_thickness_nm: float = 0.5
    max_thickness_nm: float = 200.0
    max_total_layers: int = 32
    max_added_layers: int = 1
    position_samples: int = 17
    candidate_budget: int = 128
    needle_thickness_samples: int = 9
    growth_steps_nm: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0, 10.0)
    growth_phase_fractions: tuple[float, ...] = (0.25, 0.5, 1.0)
    growth_cycles: int = 1
    seed: int = 0


@dataclass(frozen=True)
class FilmStructureTraceEvent:
    sequence: int
    stage: str
    candidate_kind: str
    candidate_id: str = ""
    host_layer_id: str = ""
    material_id: str = ""
    optical_set_id: str = ""
    position_nm: float | None = None
    position_fraction: float | None = None
    thickness_nm: float | None = None
    max_normalized_error: float | None = None
    weighted_rms_error: float | None = None
    accepted: bool = False
    selected: bool = False
    verified: bool = False
    parent_candidate_id: str = ""
    message: str = ""


@dataclass(frozen=True)
class FilmStructureCounters:
    objective_evaluations: int = 0
    score_evaluations: int = 0
    jacobian_evaluations: int = 0
    validation_evaluations: int = 0
    candidate_count: int = 0
    needle_candidate_count: int = 0
    needle_probe_count: int = 0
    growth_proposal_count: int = 0
    local_iterations: int = 0
    failed_evaluations: int = 0
    candidate_budget_exhausted: bool = False
    evaluation_budget_exhausted: bool = False


@dataclass(frozen=True)
class FilmStructureOptimizationResult:
    """Exact-scored best structure, its validation, trace, and work counters."""

    base_result: FilmOptimizationResult
    mode: StructureMode
    trace: tuple[FilmStructureTraceEvent, ...]
    counters: FilmStructureCounters
    topology_audit: Mapping[str, Any]
    added_needle_layer_ids: tuple[str, ...] = ()
    message: str = ""
    frozen_request: FilmStructureOptimizationRequest | None = None
    is_final: bool = True
    candidate_verified: bool = False

    @property
    def state(self) -> str:
        return self.base_result.state

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
    def weighted_rms_error(self) -> float:
        return self.base_result.weighted_rms_error

    @property
    def targets_met(self) -> bool:
        return self.base_result.targets_met


class _StructureCancelled(Exception):
    def __init__(self, jacobian_calls: int = 0) -> None:
        super().__init__("structure optimization was cancelled")
        self.jacobian_calls = jacobian_calls


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


def _request_design(request: FilmStructureOptimizationRequest) -> dict[str, Any]:
    if not isinstance(request, FilmStructureOptimizationRequest):
        raise ValueError("request must be a FilmStructureOptimizationRequest")
    base = request.base_request
    if not isinstance(base, FilmOptimizationRequest):
        raise ValueError("base_request must be a FilmOptimizationRequest")
    if base.solver != "baseline":
        raise ValueError("structure requests do not accept a thickness solver ID")
    _validate_optimization_controls(base)
    design = validate_design(deepcopy(dict(base.design_snapshot)))
    if type(base.design_revision) is not int or base.design_revision < 0:
        raise ValueError("design_revision must be a nonnegative integer")
    if design["substrate"]["sides"] != "single" or design.get("first_surface", "front") != "front":
        raise ValueError("structure search supports only a single-sided front-surface design")
    if design["substrate"].get("mode") not in ("auto", "coherent"):
        raise ValueError("structure search requires coherent or auto substrate handling")
    if not isinstance(base.objectives, (tuple, list)) or not base.objectives:
        raise ValueError("at least one optical objective is required")
    _normalize_objectives(base.objectives)
    if not isinstance(base.variables, (tuple, list)):
        raise ValueError("base variables must be a sequence")
    layer_by_id = {layer["id"]: layer for layer in design["front_layers"]}
    seen_variables: set[str] = set()
    for variable in base.variables:
        if not isinstance(variable, FilmThicknessVariable):
            raise ValueError("base variables must contain FilmThicknessVariable values")
        if variable.side != "front" or variable.layer_id not in layer_by_id:
            raise ValueError("structure variables must select existing front-layer IDs")
        if variable.layer_id in seen_variables:
            raise ValueError("base variable layer IDs must be unique")
        seen_variables.add(variable.layer_id)
        low, high = _finite(variable.lower_nm, "variable.lower_nm"), _finite(variable.upper_nm, "variable.upper_nm")
        if low < 0 or high < low:
            raise ValueError("variable bounds must be nonnegative and ordered")

    if request.mode not in ("needle_only", "growth_only", "combined"):
        raise ValueError("mode must be needle_only, growth_only, or combined")
    if request.mode != "growth_only":
        if not isinstance(request.candidate_materials, (tuple, list)) or not request.candidate_materials:
            raise ValueError("needle modes require at least one candidate material optical-set reference")
    elif not isinstance(request.candidate_materials, (tuple, list)):
        raise ValueError("candidate_materials must be a sequence")
    if len(request.candidate_materials) > 64:
        raise ValueError("at most 64 candidate material optical-set references are supported")
    refs_seen: set[tuple[str, str]] = set()
    for reference in request.candidate_materials:
        if not isinstance(reference, FilmNeedleMaterialReference):
            raise ValueError("candidate_materials must contain FilmNeedleMaterialReference values")
        if any(not isinstance(value, str) or not value or len(value) > 128
               for value in (reference.material_id, reference.optical_set_id)):
            raise ValueError("candidate material and optical-set IDs must be nonempty text up to 128 characters")
        key = (reference.material_id, reference.optical_set_id)
        if key in refs_seen:
            raise ValueError("candidate material optical-set references must be unique")
        refs_seen.add(key)

    minimum = _finite(request.min_thickness_nm, "min_thickness_nm")
    maximum = _finite(request.max_thickness_nm, "max_thickness_nm")
    total_maximum = _finite(request.max_total_thickness_nm, "max_total_thickness_nm")
    if minimum <= 0 or maximum <= minimum:
        raise ValueError("structure layer bounds must be positive and increasing")
    if total_maximum <= 0:
        raise ValueError("max_total_thickness_nm must be positive")
    max_layers = _positive_int(request.max_total_layers, "max_total_layers", MAX_LAYERS)
    if type(request.max_added_layers) is not int or request.max_added_layers != 1:
        raise ValueError("this bounded structure cycle supports exactly one needle-layer allowance")
    if len(design["front_layers"]) > max_layers:
        raise ValueError("initial design already exceeds max_total_layers")
    _positive_int(request.position_samples, "position_samples")
    _positive_int(request.candidate_budget, "candidate_budget")
    _positive_int(request.needle_thickness_samples, "needle_thickness_samples")
    if type(request.growth_cycles) is not int or request.growth_cycles != 1:
        raise ValueError("growth_cycles must be 1 for the bounded single-cycle search")
    if type(request.seed) is not int or not 0 <= request.seed <= 0xFFFFFFFF:
        raise ValueError("seed must be an integer from 0 to 4294967295")
    if not isinstance(request.growth_steps_nm, (tuple, list)) or not request.growth_steps_nm:
        raise ValueError("growth_steps_nm must contain positive step sizes")
    growth_steps = tuple(_finite(v, "growth_steps_nm") for v in request.growth_steps_nm)
    if any(v <= 0 for v in growth_steps) or len(set(growth_steps)) != len(growth_steps):
        raise ValueError("growth_steps_nm must contain unique positive values")
    if not isinstance(request.growth_phase_fractions, (tuple, list)):
        raise ValueError("growth_phase_fractions must be a sequence")
    phase_fractions = tuple(_finite(v, "growth_phase_fractions") for v in request.growth_phase_fractions)
    if any(v <= 0 or v > 2 for v in phase_fractions) or len(set(phase_fractions)) != len(phase_fractions):
        raise ValueError("growth_phase_fractions must be unique values in (0, 2]")

    physical = [
        _physical_layer_thickness(layer, design, base.frozen_material_snapshot or {})
        if base.frozen_material_snapshot is not None else
        physical_thickness_nm(layer, design["reference_wavelength_nm"], 1.0)
        for layer in design["front_layers"]
    ]
    physical_by_id = {layer["id"]: thickness for layer, thickness
                      in zip(design["front_layers"], physical)}
    # QW rows require their frozen optical record to compute their actual nm
    # thickness. If the request has not been frozen yet, this preliminary
    # check is deferred to freeze_film_structure_optimization_request.
    if base.frozen_material_snapshot is not None:
        if any(d < minimum or d > maximum for d in physical):
            raise ValueError("every existing layer must satisfy min_thickness_nm and max_thickness_nm")
        if sum(physical) > total_maximum:
            raise ValueError("initial design exceeds max_total_thickness_nm")
        for variable in base.variables:
            low = max(float(variable.lower_nm), minimum)
            high = (maximum if variable.upper_nm is None
                    else min(float(variable.upper_nm), maximum))
            current = physical_by_id[variable.layer_id]
            if low > high:
                raise ValueError(f"selected layer has no feasible structure bounds: {variable.layer_id}")
            if current < low or current > high:
                raise ValueError(
                    f"selected layer initial thickness is outside its requested bounds: {variable.layer_id}")
    if base.max_evaluations < 2:
        raise ValueError("max_evaluations must allow one baseline score and one validation pass")
    return design


def _validate_material_domains(request: FilmStructureOptimizationRequest,
                               design: Mapping[str, Any], frozen: Mapping[str, Any]) -> None:
    """Reject extrapolation at every training node and each QW reference point."""
    materials = {row["id"]: row for row in frozen.get("materials", [])}
    objectives = _normalize_objectives(request.base_request.objectives)
    validation_objectives = _validation_objectives(request)
    wavelengths = sorted({
        float(value)
        for objective in (*objectives, *validation_objectives)
        for value in np.linspace(objective.wavelength_min_nm,
                                 objective.wavelength_max_nm,
                                 objective.wavelength_samples)
    })
    references: dict[tuple[str, str], Any] = {}
    rows = [*design["front_layers"], design["substrate"]]
    incident = design.get("incident_medium", {})
    if incident.get("kind", "constant") != "constant":
        rows.append(incident)
    rows.extend(
        {"material_id": ref.material_id, "optical_set_id": ref.optical_set_id}
        for ref in request.candidate_materials
    )
    for layer in rows:
        key = (layer["material_id"], layer["optical_set_id"])
        material = materials.get(key[0])
        optical = next((row for row in (material or {}).get("optical_sets", [])
                        if row.get("id") == key[1]), None)
        if optical is None:
            raise ValueError(f"frozen optical data is missing: {key[0]}/{key[1]}")
        if key not in references:
            try:
                references[key] = prepare_optical_set(
                    optical, k_sampling_policy="source_only_zero_defaults")
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"frozen optical set is invalid: {key[0]}/{key[1]}") from exc
        sample = references[key]
        check_waves = list(wavelengths)
        if layer in design["front_layers"] and layer["thickness"]["unit"] == "qw":
            check_waves.append(float(design["reference_wavelength_nm"]))
        for wavelength in sorted(set(check_waves)):
            try:
                n, k, _details = sample(
                    wavelength, "nm", extrapolation_policy="error",
                    include_k=bool(design.get("use_k", True)),
                )
            except (TypeError, ValueError, KeyError, ArithmeticError) as exc:
                raise ValueError(
                    f"structure material domain excludes {wavelength:g} nm for {key[0]}/{key[1]}") from exc
            if not math.isfinite(float(n)) or (k is not None and not math.isfinite(float(k))):
                raise ValueError(f"nonfinite optical sample for {key[0]}/{key[1]} at {wavelength:g} nm")


def freeze_film_structure_optimization_request(
    service: Any, request: FilmStructureOptimizationRequest,
) -> FilmStructureOptimizationRequest:
    """Freeze the base and explicitly selected candidate optics in memory."""
    design = _request_design(request)
    base = replace(request.base_request, solver="baseline", seed=request.seed)
    if base.frozen_material_snapshot is None:
        base = freeze_film_optimization_request(service, base)
    else:
        frozen = _checked_frozen_snapshot(base.frozen_material_snapshot, design)
        base = replace(base, design_snapshot=deepcopy(frozen["design"]),
                       frozen_material_snapshot=frozen)
    frozen = _checked_frozen_snapshot(base.frozen_material_snapshot, design)
    if request.candidate_materials:
        frozen = _candidate_payload_from_materials(service, frozen, request.candidate_materials)
        frozen = _checked_frozen_snapshot(frozen, design)
    base = replace(base, design_snapshot=deepcopy(frozen["design"]),
                   frozen_material_snapshot=frozen)
    frozen_request = replace(request, base_request=base)
    physical = [_physical_layer_thickness(layer, design, frozen)
                for layer in design["front_layers"]]
    if any(d < request.min_thickness_nm or d > request.max_thickness_nm for d in physical):
        raise ValueError("every existing layer must satisfy min_thickness_nm and max_thickness_nm")
    if sum(physical) > request.max_total_thickness_nm:
        raise ValueError("initial design exceeds max_total_thickness_nm")
    _validate_material_domains(frozen_request, design, frozen)
    return frozen_request


def film_structure_request_to_payload(request: FilmStructureOptimizationRequest) -> dict[str, Any]:
    """Return a strict JSON-safe request payload without any solver identifier."""
    design = _request_design(request)
    base = request.base_request
    frozen = base.frozen_material_snapshot
    if frozen is not None:
        frozen = _checked_frozen_snapshot(frozen, design)
    payload = {
        "schema_version": 1,
        "kind": "coat_sim.film_structure_optimization_request",
        "mode": request.mode,
        "base_request": {
            "design_snapshot": deepcopy(dict(base.design_snapshot)),
            "design_revision": base.design_revision,
            "variables": [asdict(item) for item in base.variables],
            "objectives": [asdict(item) for item in base.objectives],
            "max_iterations": base.max_iterations,
            "max_evaluations": base.max_evaluations,
            "initial_step_fraction": base.initial_step_fraction,
            "min_step_fraction": base.min_step_fraction,
            "acceptable_normalized_error": base.acceptable_normalized_error,
            "frozen_material_snapshot": deepcopy(frozen),
            "validation_samples_multiplier": base.validation_samples_multiplier,
        },
        "candidate_materials": [asdict(item) for item in request.candidate_materials],
        "limits": {
            "max_total_thickness_nm": request.max_total_thickness_nm,
            "min_thickness_nm": request.min_thickness_nm,
            "max_thickness_nm": request.max_thickness_nm,
            "max_total_layers": request.max_total_layers,
            "max_added_layers": request.max_added_layers,
            "position_samples": request.position_samples,
            "candidate_budget": request.candidate_budget,
            "needle_thickness_samples": request.needle_thickness_samples,
            "growth_steps_nm": list(request.growth_steps_nm),
            "growth_phase_fractions": list(request.growth_phase_fractions),
            "growth_cycles": request.growth_cycles,
            "seed": request.seed,
        },
    }
    # Enforce that queue storage can encode this exact immutable request.
    try:
        json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("structure request is not valid JSON data") from exc
    return payload


def film_structure_request_from_payload(payload: Mapping[str, Any]) -> FilmStructureOptimizationRequest:
    from .film_structure_payload_contract import validate_structure_request_payload_schema
    validate_structure_request_payload_schema(payload)
    """Parse the strict structure request schema; solver fields are rejected."""
    if not isinstance(payload, Mapping):
        raise ValueError("structure request payload must be a mapping")
    if "solver" in payload or "solver_id" in payload:
        raise ValueError("structure request payload must not include solver or solver_id")
    if payload.get("schema_version") != 1 or payload.get("kind") != "coat_sim.film_structure_optimization_request":
        raise ValueError("structure request payload kind/version is invalid")
    base_data = payload.get("base_request")
    limits = payload.get("limits")
    references = payload.get("candidate_materials")
    if not isinstance(base_data, Mapping) or not isinstance(limits, Mapping) or not isinstance(references, list):
        raise ValueError("structure request payload fields are malformed")
    if "solver" in base_data or "solver_id" in base_data:
        raise ValueError("structure base_request payload must not include a solver identifier")
    variables = tuple(FilmThicknessVariable(**row) for row in base_data.get("variables", []))
    objectives = tuple(FilmOptimizationObjective(**{
        **row,
        "angles_deg": tuple(row.get("angles_deg", (0.0,))),
    }) for row in base_data.get("objectives", []))
    base = FilmOptimizationRequest(
        design_snapshot=deepcopy(base_data["design_snapshot"]),
        design_revision=base_data["design_revision"],
        variables=variables,
        objectives=objectives,
        max_iterations=base_data.get("max_iterations", 200),
        max_evaluations=base_data.get("max_evaluations", 1000),
        initial_step_fraction=base_data.get("initial_step_fraction", 0.25),
        min_step_fraction=base_data.get("min_step_fraction", 1e-4),
        acceptable_normalized_error=base_data.get("acceptable_normalized_error", 1e-3),
        frozen_material_snapshot=deepcopy(base_data.get("frozen_material_snapshot")),
        solver="baseline",
        seed=limits.get("seed", 0),
        validation_samples_multiplier=base_data.get("validation_samples_multiplier", 2),
    )
    request = FilmStructureOptimizationRequest(
        base_request=base,
        candidate_materials=tuple(FilmNeedleMaterialReference(**row) for row in references),
        mode=payload.get("mode", "combined"),
        max_total_thickness_nm=limits.get("max_total_thickness_nm", 2000.0),
        min_thickness_nm=limits.get("min_thickness_nm", 0.5),
        max_thickness_nm=limits.get("max_thickness_nm", 200.0),
        max_total_layers=limits.get("max_total_layers", 32),
        max_added_layers=limits.get("max_added_layers", 1),
        position_samples=limits.get("position_samples", 17),
        candidate_budget=limits.get("candidate_budget", 128),
        needle_thickness_samples=limits.get("needle_thickness_samples", 9),
        growth_steps_nm=tuple(limits.get("growth_steps_nm", (0.5, 1.0, 2.0, 5.0, 10.0))),
        growth_phase_fractions=tuple(limits.get("growth_phase_fractions", (0.25, 0.5, 1.0))),
        growth_cycles=limits.get("growth_cycles", 1),
        seed=limits.get("seed", 0),
    )
    _request_design(request)
    if request.base_request.frozen_material_snapshot is not None:
        _checked_frozen_snapshot(request.base_request.frozen_material_snapshot,
                                 request.base_request.design_snapshot)
    return request


class _PreparedStructureEvaluator:
    """Run-owned static optics, never a candidate/score or a public trust token.

    Construction independently copies and verifies the complete frozen input.
    Its private request is used only during this synchronous bound call. A
    caller cannot attach the context to an arbitrary public request. Normal
    public evaluations continue checking every supplied snapshot independently.
    Topologies, settings and variables are still checked for each candidate.
    """

    def __init__(self, request: FilmStructureOptimizationRequest):
        if not isinstance(request, FilmStructureOptimizationRequest):
            raise ValueError("request must be a FilmStructureOptimizationRequest")
        owned = deepcopy(request)
        frozen = owned.base_request.frozen_material_snapshot
        if frozen is None:
            raise ValueError("prepared structure evaluation requires a frozen snapshot")
        payload = _checked_frozen_snapshot(frozen, owned.base_request.design_snapshot)
        self.__request = owned
        self.__materials = _PreparedMaterialData(payload, owned.base_request.design_snapshot)
        self.__active_request: FilmStructureOptimizationRequest | None = None

    def evaluate(self, design: Mapping[str, Any], *,
                 evaluator: Callable[..., Mapping[str, Any]] | None = None,
                 objectives: Sequence[FilmOptimizationObjective] | None = None,
                 **kwargs: Any) -> Mapping[str, Any]:
        if self.__active_request is not None:
            raise RuntimeError("prepared structure evaluator cannot be shared concurrently")
        request = self.__request
        if objectives is not None:
            request = replace(request, base_request=replace(
                request.base_request, objectives=tuple(objectives)))
        self.__active_request = request
        try:
            entry = evaluate_frozen_structure_candidate if evaluator is None else evaluator
            return entry(request, design, _prepared=self, **kwargs)
        finally:
            self.__active_request = None

    def _material_data_for(self, request: FilmStructureOptimizationRequest) -> _PreparedMaterialData:
        if self.__active_request is None or request is not self.__active_request:
            raise ValueError("prepared optics can only be used by their bound private evaluation")
        return self.__materials


def evaluate_frozen_structure_candidate(
    request: FilmStructureOptimizationRequest,
    design: Mapping[str, Any],
    *,
    variable_ids: Sequence[str] = (),
    cancel_event: Any = None,
    with_jacobian: bool = False,
    zero_thickness_layer_ids: Sequence[str] = (),
    derived_validation_grid: bool = False,
    forward_responses: Sequence[Mapping[str, Any]] | None = None,
    _prepared: _PreparedStructureEvaluator | None = None,
) -> Mapping[str, Any]:
    """Pure complete-grid score/Jacobian evaluation over a frozen snapshot.

    This function never changes request state, budgets, checkpoints, or best
    candidates. A backend may replace this call later, while this serial
    orchestrator remains the only owner of cancellation and archive decisions.
    ``gradient_by_layer_id`` is the exact chain derivative of the smooth
    weighted sum of squared signed residuals. It is not a derivative of the
    feasibility-first weighted-RMS archive metric.
    """
    if not isinstance(request, FilmStructureOptimizationRequest):
        raise ValueError("request must be a FilmStructureOptimizationRequest")
    frozen = request.base_request.frozen_material_snapshot
    if frozen is None:
        raise ValueError("structure request must be frozen before candidate evaluation")
    payload: Mapping[str, Any] | _PreparedMaterialData
    if _prepared is None:
        payload = _checked_frozen_snapshot(frozen, request.base_request.design_snapshot)
    elif type(_prepared) is _PreparedStructureEvaluator:
        payload = _prepared._material_data_for(request)
    else:
        raise ValueError("invalid private prepared structure context")
    zero_ids = tuple(zero_thickness_layer_ids)
    if zero_ids:
        candidate = deepcopy(dict(design))
    else:
        candidate = validate_design(deepcopy(dict(design)))
    if type(derived_validation_grid) is not bool:
        raise ValueError("derived_validation_grid must be a boolean")
    training_objectives = _normalize_objectives(request.base_request.objectives)
    if derived_validation_grid:
        multiplier = request.base_request.validation_samples_multiplier
        if type(multiplier) is not int or multiplier < 1:
            raise ValueError("validation_samples_multiplier must be a positive integer")
        objective_rows = tuple(replace(
            objective,
            wavelength_samples=(1 if objective.wavelength_samples == 1 else
                                (objective.wavelength_samples - 1) * multiplier + 1),
        ) for objective in training_objectives)
    else:
        objective_rows = training_objectives
    variable_rows: list[FilmThicknessVariable] = []
    if with_jacobian:
        if not isinstance(variable_ids, (tuple, list)) or len(set(variable_ids)) != len(variable_ids):
            raise ValueError("variable_ids must contain unique layer IDs")
        layers_by_id = {row["id"]: row for row in candidate.get("front_layers", [])}
        for identifier in variable_ids:
            if not isinstance(identifier, str) or identifier not in layers_by_id:
                raise ValueError(f"Jacobian variable is missing from front_layers: {identifier}")
            low, high = request.min_thickness_nm, request.max_thickness_nm
            if identifier in zero_ids:
                low = 0.0
            variable_rows.append(FilmThicknessVariable("front", identifier, low, high))
    elif variable_ids:
        raise ValueError("variable_ids require with_jacobian=True")

    if forward_responses is not None:
        if with_jacobian or zero_ids:
            raise ValueError("precomputed forward responses cannot supply analytic derivatives or zero probes")
        if len(forward_responses) != sum(len(o.angles_deg) for o in objective_rows):
            raise ValueError("precomputed forward response condition count differs")
    responses: list[dict[str, Any]] = []
    jac_calls = 0
    helper_sample_limit = min(
        10001,
        1_000_000 // (len(candidate.get("front_layers", [])) +
                      len(candidate.get("back_layers", [])) + 3),
    )
    if helper_sample_limit < 1:
        raise ValueError("film evaluation chunk limit cannot satisfy the helper sample budget")
    for objective_index, objective in enumerate(objective_rows):
        for angle in objective.angles_deg:
            global_waves = np.linspace(
                objective.wavelength_min_nm,
                objective.wavelength_max_nm,
                objective.wavelength_samples,
            )
            if forward_responses is not None:
                if cancel_event is not None and cancel_event.is_set():
                    raise _StructureCancelled()
                supplied = forward_responses[len(responses)]
                supplied_waves = np.asarray(supplied["wavelength_nm"], dtype=float)
                supplied_values = np.asarray(supplied[objective.metric], dtype=float)
                if (supplied_waves.shape != global_waves.shape or
                        not np.array_equal(supplied_waves, global_waves) or
                        supplied_values.shape != global_waves.shape or
                        not np.all(np.isfinite(supplied_values))):
                    raise ValueError("precomputed forward response samples differ from the objective grid")
                responses.append({"objective_index": objective_index, "angle_deg": float(angle),
                    "wavelength_nm": global_waves.tolist(), "actual_values": supplied_values.tolist()})
                continue
            value_chunks: list[np.ndarray] = []
            jacobian_chunks: list[np.ndarray] = []
            for start in range(0, objective.wavelength_samples, helper_sample_limit):
                chunk_waves = global_waves[start:start + helper_sample_limit]
                if cancel_event is not None and callable(getattr(cancel_event, "is_set", None)) and cancel_event.is_set():
                    raise _StructureCancelled(jac_calls)
                settings = {
                    "wavelength_min_nm": float(chunk_waves[0]),
                    "wavelength_max_nm": float(chunk_waves[-1]),
                    "samples": int(chunk_waves.size),
                    "angle_deg": angle,
                    "polarization": objective.polarization,
                    "first_surface": "front",
                }
                try:
                    response = simulate_with_thickness_jacobian(
                        candidate, payload, variable_rows, settings,
                        zero_thickness_layer_ids=zero_ids,
                    )
                except _StructureCancelled as exc:
                    if variable_rows:
                        jac_calls += 1
                    raise _StructureCancelled(jac_calls) from exc
                except Exception:
                    if variable_rows:
                        jac_calls += 1
                    raise
                if variable_rows:
                    jac_calls += 1
                values = np.asarray(response[objective.metric], dtype=float).reshape(-1)
                jacobian = np.asarray(response["jacobian_" + objective.metric], dtype=float)
                if jacobian.shape != (chunk_waves.size, len(variable_rows)):
                    raise ValueError("analytic Jacobian returned an invalid shape")
                returned_waves = np.asarray(response["wavelength_nm"], dtype=float).reshape(-1)
                if (values.shape != chunk_waves.shape or returned_waves.shape != chunk_waves.shape or
                        not np.all(np.isfinite(values))):
                    raise ValueError("analytic film response contains invalid samples")
                value_chunks.append(values)
                jacobian_chunks.append(jacobian)
            values = np.concatenate(value_chunks)
            jacobian = np.vstack(jacobian_chunks)
            waves = global_waves
            response_row: dict[str, Any] = {
                "objective_index": objective_index,
                "angle_deg": float(angle),
                "wavelength_nm": waves.tolist(),
                "actual_values": values.tolist(),
            }
            if variable_rows:
                response_row["jacobian_values_by_layer_id"] = {
                    variable.layer_id: jacobian[:, column].tolist()
                    for column, variable in enumerate(variable_rows)
                }
            responses.append(response_row)

    score_errors: list[FilmConditionError] = []
    normalized: list[float] = []
    weights: list[float] = []
    maximum = weighted_maximum = smooth_loss = 0.0
    gradient = np.zeros(len(variable_rows), dtype=float)
    response_cursor = 0
    for objective_index, objective in enumerate(objective_rows):
        for _angle in objective.angles_deg:
            response = responses[response_cursor]
            response_cursor += 1
            actual_values = np.asarray(response["actual_values"], dtype=float)
            waves = np.asarray(response["wavelength_nm"], dtype=float)
            if (actual_values.shape != (objective.wavelength_samples,) or
                    waves.shape != actual_values.shape):
                raise ValueError(
                    "film simulation returned invalid objective samples "
                    f"({actual_values.size} values, {waves.size} wavelengths; "
                    f"{objective.wavelength_samples} requested)"
                )
            jacobian_by_id = response.get("jacobian_values_by_layer_id", {})
            for sample_index, (wave, actual) in enumerate(zip(waves, actual_values)):
                actual_value = float(actual)
                if objective.mode == "value":
                    signed = actual_value - float(objective.target_value)
                    active = True
                elif objective.lower_bound is not None and actual_value < objective.lower_bound:
                    signed = actual_value - float(objective.lower_bound)
                    active = True
                elif objective.upper_bound is not None and actual_value > objective.upper_bound:
                    signed = actual_value - float(objective.upper_bound)
                    active = True
                else:
                    signed = 0.0
                    active = False
                norm_error, weighted_error = score_condition(
                    abs(signed), objective.normalization_scale, objective.weight)
                maximum = max(maximum, norm_error)
                weighted_maximum = max(weighted_maximum, weighted_error)
                normalized.append(norm_error)
                weights.append(objective.weight)
                sqrt_weight = math.sqrt(objective.weight)
                residual = signed * sqrt_weight / objective.normalization_scale
                smooth_loss += residual * residual
                score_errors.append(FilmConditionError(
                    objective_index=objective_index,
                    wavelength_nm=float(wave),
                    angle_deg=float(response["angle_deg"]),
                    polarization=objective.polarization,
                    metric=objective.metric,
                    actual=actual_value,
                    target_value=objective.target_value,
                    lower_bound=objective.lower_bound,
                    upper_bound=objective.upper_bound,
                    normalized_violation=norm_error,
                    weighted_normalized_violation=weighted_error,
                ))
                if with_jacobian and active and residual != 0:
                    for column, variable in enumerate(variable_rows):
                        derivative = float(jacobian_by_id[variable.layer_id][sample_index])
                        gradient[column] += (
                            2.0 * residual * sqrt_weight * derivative /
                            objective.normalization_scale
                        )
    if not np.all(np.isfinite(gradient)) or not math.isfinite(smooth_loss):
        raise ValueError("nonfinite smooth structure objective or gradient")
    rms = weighted_rms_error(normalized, weights)
    score = {
        "design": deepcopy(candidate),
        "errors": tuple(score_errors),
        "max_error": float(maximum),
        "weighted_max_error": float(weighted_maximum),
        "weighted_rms_error": float(rms),
        "key": candidate_key(maximum, rms,
                             request.base_request.acceptable_normalized_error),
    }
    return {
        "design": deepcopy(candidate),
        "score": score,
        "smooth_loss": float(smooth_loss) if with_jacobian else None,
        "gradient_by_layer_id": ({
            variable.layer_id: float(gradient[column])
            for column, variable in enumerate(variable_rows)
        } if with_jacobian else None),
        "jacobian_calls": jac_calls,
        "responses": tuple(responses),
    }


def _physical_layers(design: Mapping[str, Any], frozen: Mapping[str, Any]) -> dict[str, float]:
    return {
        layer["id"]: float(_physical_layer_thickness(layer, design, frozen))
        for layer in design["front_layers"]
    }


def _feasible_design(request: FilmStructureOptimizationRequest,
                     design: Mapping[str, Any], frozen: Mapping[str, Any]) -> tuple[bool, str]:
    try:
        checked = validate_design(deepcopy(dict(design)))
        thicknesses = _physical_layers(checked, frozen)
    except (TypeError, ValueError, KeyError, ArithmeticError) as exc:
        return False, f"design schema or material thickness invalid: {exc}"
    if len(checked["front_layers"]) > request.max_total_layers:
        return False, "max_total_layers hard limit exceeded"
    if any(not math.isfinite(value) or value < request.min_thickness_nm or
           value > request.max_thickness_nm for value in thicknesses.values()):
        return False, "min_thickness_nm or max_thickness_nm hard limit exceeded"
    if sum(thicknesses.values()) > request.max_total_thickness_nm + 1e-9:
        return False, "max_total_thickness_nm hard limit exceeded"
    return True, ""


def _stable_identifier(prefix: str, values: Sequence[Any], existing: set[str]) -> str:
    identity = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    root = prefix + "-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    identifier = root
    suffix = 1
    while identifier in existing:
        identifier = f"{root}-{suffix}"
        suffix += 1
    return identifier


def _site_records(design: Mapping[str, Any], frozen: Mapping[str, Any],
                  position_samples: int) -> list[dict[str, Any]]:
    layers = design["front_layers"]
    physical = [_physical_layer_thickness(layer, design, frozen) for layer in layers]
    sites: list[dict[str, Any]] = []
    coordinate = 0.0
    for interface in range(len(layers) + 1):
        sites.append({
            "kind": "interface", "interface": interface, "host_index": None,
            "host_layer_id": "", "fraction": None, "position_nm": coordinate,
        })
        if interface == len(layers):
            continue
        layer = layers[interface]
        thickness = physical[interface]
        for sample_index in range(1, position_samples + 1):
            fraction = sample_index / (position_samples + 1)
            sites.append({
                "kind": "interior", "interface": interface, "host_index": interface,
                "host_layer_id": layer["id"], "fraction": fraction,
                "position_nm": coordinate + thickness * fraction,
                "host_thickness_nm": float(thickness),
            })
        coordinate += thickness
    sites.sort(key=lambda row: (row["position_nm"], 0 if row["kind"] == "interface" else 1,
                                row["host_layer_id"], row["fraction"] or 0.0))
    return sites


def _insert_at_site(
    design: Mapping[str, Any],
    site: Mapping[str, Any],
    reference: FilmNeedleMaterialReference,
    thickness_nm: float,
    needle_id: str,
    split_id: str,
    *,
    zero: bool = False,
) -> dict[str, Any]:
    candidate = deepcopy(dict(design))
    layers = candidate["front_layers"]
    if site["kind"] == "interface":
        interface = int(site["interface"])
        new_layer = {
            "id": needle_id, "material_id": reference.material_id,
            "optical_set_id": reference.optical_set_id,
            "thickness": {"value": 0.0 if zero else float(thickness_nm), "unit": "nm"},
            "note": "Temporary exact-zero classical needle sensitivity probe." if zero else
                    "Finite positive-thickness classical needle proposal.",
        }
        layers.insert(interface, new_layer)
    else:
        index = int(site["host_index"])
        fraction = float(site["fraction"])
        host = deepcopy(layers[index])
        host_total = float(site["host_thickness_nm"])
        left_nm = host_total * fraction
        right_nm = host_total - left_nm
        host["thickness"] = {"value": left_nm, "unit": "nm"}
        right = deepcopy(host)
        right["id"] = split_id
        right["thickness"] = {"value": right_nm, "unit": "nm"}
        layers[index] = host
        new_layer = {
            "id": needle_id, "material_id": reference.material_id,
            "optical_set_id": reference.optical_set_id,
            "thickness": {"value": 0.0 if zero else float(thickness_nm), "unit": "nm"},
            "note": "Temporary exact-zero classical needle sensitivity probe." if zero else
                    "Finite positive-thickness classical needle proposal.",
        }
        layers[index + 1:index + 1] = [new_layer, right]
    expression = candidate.get("expression", {})
    if expression.get("text") and expression.get("in_sync"):
        expression["in_sync"] = False
    return candidate


def _growth_design(design: Mapping[str, Any], layer_id: str, thickness_nm: float) -> dict[str, Any]:
    candidate = deepcopy(dict(design))
    layer = next(row for row in candidate["front_layers"] if row["id"] == layer_id)
    layer["thickness"] = {"value": float(thickness_nm), "unit": "nm"}
    expression = candidate.get("expression", {})
    if expression.get("text") and expression.get("in_sync"):
        expression["in_sync"] = False
    return candidate


@dataclass(frozen=True)
class _VariableGroup:
    key: str
    layer_ids: tuple[str, ...]
    fractions: tuple[float, ...]
    lower_nm: float
    upper_nm: float


def _split_group_for(site: Mapping[str, Any], split_id: str) -> dict[str, Any] | None:
    if site["kind"] != "interior":
        return None
    fraction = float(site["fraction"])
    return {
        "layer_ids": (str(site["host_layer_id"]), split_id),
        "fractions": (fraction, 1.0 - fraction),
        "fraction": fraction,
    }


def _variable_groups(
    request: FilmStructureOptimizationRequest,
    design: Mapping[str, Any],
    frozen: Mapping[str, Any],
    split_groups: Mapping[str, Mapping[str, Any]],
    active_layer_ids: Sequence[str],
) -> tuple[_VariableGroup, ...]:
    thicknesses = _physical_layers(design, frozen)
    by_id = {layer["id"]: layer for layer in design["front_layers"]}
    groups: list[_VariableGroup] = []
    seen: set[str] = set()
    selected = {variable.layer_id: variable for variable in request.base_request.variables}
    for variable in request.base_request.variables:
        if variable.layer_id not in by_id or variable.layer_id in seen:
            continue
        split = split_groups.get(variable.layer_id)
        if split is None:
            identifiers = (variable.layer_id,)
            fractions = (1.0,)
        else:
            identifiers = tuple(split["layer_ids"])
            fractions = tuple(float(v) for v in split["fractions"])
        if any(identifier not in by_id for identifier in identifiers):
            continue
        lower = max(float(variable.lower_nm), request.min_thickness_nm)
        upper = (request.max_thickness_nm if variable.upper_nm is None
                 else min(float(variable.upper_nm), request.max_thickness_nm))
        for identifier, fraction in zip(identifiers, fractions):
            lower = max(lower, request.min_thickness_nm / fraction)
            upper = min(upper, request.max_thickness_nm / fraction)
        if lower > upper:
            continue
        groups.append(_VariableGroup(variable.layer_id, identifiers, fractions, lower, upper))
        seen.update(identifiers)
    for identifier in active_layer_ids:
        if identifier in seen or identifier not in by_id:
            continue
        groups.append(_VariableGroup(
            identifier, (identifier,), (1.0,),
            request.min_thickness_nm, request.max_thickness_nm,
        ))
        seen.add(identifier)
    return tuple(groups)


def _group_value(group: _VariableGroup, thicknesses: Mapping[str, float]) -> float:
    return float(sum(thicknesses[identifier] for identifier in group.layer_ids))


def _project_group_values(values: np.ndarray, lower: np.ndarray, upper: np.ndarray,
                          max_sum: float) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    clipped = np.clip(values, lower, upper)
    if float(np.sum(clipped)) <= max_sum + 1e-10:
        return clipped
    if float(np.sum(lower)) > max_sum + 1e-10:
        raise ValueError("selected layer bounds cannot satisfy max_total_thickness_nm")
    # Euclidean projection onto box ∩ sum(x)<=max_sum is clip(values-tau).
    # Keep the original values inside the bisection: clipping first changes
    # the projection when an unconstrained component lies outside its box.
    lo = float(np.min(values - upper)) - 1.0
    hi = float(np.max(values - lower)) + 1.0
    for _ in range(80):
        middle = (lo + hi) / 2.0
        projected = np.clip(values - middle, lower, upper)
        if float(np.sum(projected)) > max_sum:
            lo = middle
        else:
            hi = middle
    return np.clip(values - hi, lower, upper)


def _design_with_group_values(
    design: Mapping[str, Any],
    groups: Sequence[_VariableGroup],
    values: np.ndarray,
    frozen: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = deepcopy(dict(design))
    by_id = {layer["id"]: layer for layer in candidate["front_layers"]}
    for group, total in zip(groups, values):
        for identifier, fraction in zip(group.layer_ids, group.fractions):
            layer = by_id[identifier]
            layer["thickness"] = {"value": float(total) * fraction, "unit": "nm"}
    expression = candidate.get("expression", {})
    if expression.get("text") and expression.get("in_sync"):
        expression["in_sync"] = False
    return candidate


def _group_gradients(groups: Sequence[_VariableGroup],
                     gradient_by_id: Mapping[str, float]) -> np.ndarray:
    return np.asarray([
        sum(float(gradient_by_id[identifier]) * fraction
            for identifier, fraction in zip(group.layer_ids, group.fractions))
        for group in groups
    ], dtype=float)


def _local_refine(
    request: FilmStructureOptimizationRequest,
    start: Mapping[str, Any],
    start_evaluation: Mapping[str, Any],
    frozen: Mapping[str, Any],
    split_groups: Mapping[str, Mapping[str, Any]],
    active_layer_ids: Sequence[str],
    counters: dict[str, Any],
    trace: list[FilmStructureTraceEvent],
    cancel_event: Any,
    max_iterations: int,
    evaluation_budget: int,
    sequence: list[int],
    emit: Callable[[str, str], None],
) -> tuple[dict[str, Any], Mapping[str, Any], bool]:
    groups = _variable_groups(request, start, frozen, split_groups, active_layer_ids)
    if not groups:
        return deepcopy(dict(start)), start_evaluation, False
    thicknesses = _physical_layers(start, frozen)
    current = np.asarray([_group_value(group, thicknesses) for group in groups], dtype=float)
    lower = np.asarray([group.lower_nm for group in groups], dtype=float)
    upper = np.asarray([group.upper_nm for group in groups], dtype=float)
    all_ids = [identifier for group in groups for identifier in group.layer_ids]
    fixed_total = sum(thicknesses.values()) - float(np.sum(current))
    current = _project_group_values(
        current, lower, upper, request.max_total_thickness_nm - fixed_total)
    current_design = _design_with_group_values(start, groups, current, frozen)
    feasible, _why = _feasible_design(request, current_design, frozen)
    if not feasible:
        return deepcopy(dict(start)), start_evaluation, False

    # The proposal's exact score/Jacobian result is reusable when no projection
    # changed its selected values; otherwise evaluate the projected point now.
    if np.allclose(current, [_group_value(group, thicknesses) for group in groups], rtol=0, atol=1e-12):
        current_evaluation = start_evaluation
    else:
        if counters["objective_evaluations"] >= evaluation_budget:
            counters["evaluation_budget_exhausted"] = True
            return deepcopy(dict(start)), start_evaluation, False
        counters["objective_evaluations"] += 1
        try:
            current_evaluation = evaluate_frozen_structure_candidate(
                request, current_design, variable_ids=all_ids,
                cancel_event=cancel_event, with_jacobian=True,
            )
            counters["score_evaluations"] += 1
            counters["jacobian_evaluations"] += int(current_evaluation["jacobian_calls"])
        except _StructureCancelled:
            return deepcopy(dict(start)), start_evaluation, True
        except (ValueError, ArithmeticError, FloatingPointError):
            counters["failed_evaluations"] += 1
            trace.append(FilmStructureTraceEvent(
                sequence[0], "refinement", "local_refinement", message="projected start evaluation failed"))
            sequence[0] += 1
            return deepcopy(dict(start)), start_evaluation, False

    best_design = deepcopy(dict(current_design))
    best_evaluation = current_evaluation
    radius = max(float(request.base_request.initial_step_fraction), 0.05)
    min_radius = max(float(request.base_request.min_step_fraction), 1e-6)
    spans = np.maximum(upper - lower, 1e-12)
    was_cancelled = False
    for _ in range(max_iterations):
        if cancel_event is not None and callable(getattr(cancel_event, "is_set", None)) and cancel_event.is_set():
            was_cancelled = True
            break
        if counters["objective_evaluations"] >= evaluation_budget:
            counters["evaluation_budget_exhausted"] = True
            break
        if radius < min_radius:
            break
        current_gradient = _group_gradients(groups, current_evaluation["gradient_by_layer_id"])
        scaled_gradient = current_gradient * spans
        norm = float(np.linalg.norm(scaled_gradient, ord=2))
        if not math.isfinite(norm) or norm <= 1e-14:
            break
        direction = -scaled_gradient / norm
        proposal = current + direction * radius * spans
        proposal = _project_group_values(
            proposal, lower, upper, request.max_total_thickness_nm - fixed_total)
        if np.allclose(proposal, current, rtol=0, atol=1e-12):
            radius *= 0.5
            counters["local_iterations"] += 1
            continue
        trial_design = _design_with_group_values(start, groups, proposal, frozen)
        feasible, why = _feasible_design(request, trial_design, frozen)
        if not feasible:
            # Every attempted point is projected before evaluation; a residual
            # violation indicates a broken physical conversion, so fail closed.
            counters["failed_evaluations"] += 1
            radius *= 0.5
            counters["local_iterations"] += 1
            continue
        counters["objective_evaluations"] += 1
        try:
            trial_evaluation = evaluate_frozen_structure_candidate(
                request, trial_design, variable_ids=all_ids,
                cancel_event=cancel_event, with_jacobian=True,
            )
            counters["score_evaluations"] += 1
            counters["jacobian_evaluations"] += int(trial_evaluation["jacobian_calls"])
        except _StructureCancelled:
            was_cancelled = True
            break
        except (ValueError, ArithmeticError, FloatingPointError):
            counters["failed_evaluations"] += 1
            radius *= 0.5
            counters["local_iterations"] += 1
            continue
        counters["local_iterations"] += 1
        trace.append(FilmStructureTraceEvent(
            sequence[0], "refinement", "local_refinement",
            max_normalized_error=trial_evaluation["score"]["max_error"],
            weighted_rms_error=trial_evaluation["score"]["weighted_rms_error"],
            message="bounded projected-gradient trial evaluated"))
        sequence[0] += 1
        if trial_evaluation["score"]["key"] < best_evaluation["score"]["key"]:
            best_design = deepcopy(trial_design)
            best_evaluation = trial_evaluation
        if trial_evaluation["smooth_loss"] < current_evaluation["smooth_loss"] - 1e-14:
            current = proposal
            current_design = trial_design
            current_evaluation = trial_evaluation
            radius = min(0.5, radius * 1.5)
        else:
            radius *= 0.5
        emit("running", "local refinement explored a projected bounded step")
    return best_design, best_evaluation, was_cancelled


def _validation_objectives(request: FilmStructureOptimizationRequest) -> tuple[FilmOptimizationObjective, ...]:
    multiplier = request.base_request.validation_samples_multiplier
    return tuple(replace(
        objective,
        wavelength_samples=(1 if objective.wavelength_samples == 1 else
                            (objective.wavelength_samples - 1) * multiplier + 1),
    ) for objective in _normalize_objectives(request.base_request.objectives))


def _all_thicknesses(design: Mapping[str, Any], frozen: Mapping[str, Any]) -> dict[str, float]:
    return _physical_layers(design, frozen)


def _topology_audit(base_design: Mapping[str, Any], best_design: Mapping[str, Any],
                    scan: Mapping[str, Any]) -> dict[str, Any]:
    base_ids = [layer["id"] for layer in base_design["front_layers"]]
    best_ids = [layer["id"] for layer in best_design["front_layers"]]
    common = [identifier for identifier in best_ids if identifier in set(base_ids)]
    needle_ids = [identifier for identifier in best_ids
                  if identifier.startswith("structure-needle-") and identifier not in base_ids]
    split_ids = [identifier for identifier in best_ids
                 if identifier.startswith("structure-split-") and identifier not in base_ids]
    physical = [layer["thickness"] for layer in best_design["front_layers"]]
    return {
        "base_layer_ids": base_ids,
        "best_layer_ids": best_ids,
        "preserved_base_layer_ids_in_order": common,
        "base_order_preserved": common == base_ids,
        "added_needle_layer_ids": needle_ids,
        "added_split_host_layer_ids": split_ids,
        "persistent_zero_thickness_layer_ids": [],
        "all_persisted_thicknesses_positive": all(
            float(row["value"]) > 0 for row in physical),
        "needle_scan": deepcopy(dict(scan)),
    }


def _cancelled(cancel_event: Any) -> bool:
    return (cancel_event is not None and
            callable(getattr(cancel_event, "is_set", None)) and
            bool(cancel_event.is_set()))


def _request_with_objectives(
    request: FilmStructureOptimizationRequest,
    objectives: Sequence[FilmOptimizationObjective],
) -> FilmStructureOptimizationRequest:
    return replace(request, base_request=replace(
        request.base_request, objectives=tuple(objectives)))


def _jacobian_layer_ids(
    request: FilmStructureOptimizationRequest,
    design: Mapping[str, Any],
    frozen: Mapping[str, Any],
    split_groups: Mapping[str, Mapping[str, Any]],
    active_layer_ids: Sequence[str],
) -> tuple[str, ...]:
    groups = _variable_groups(request, design, frozen, split_groups, active_layer_ids)
    return tuple(identifier for group in groups for identifier in group.layer_ids)


def _phase_growth_steps(
    request: FilmStructureOptimizationRequest,
    design: Mapping[str, Any],
    frozen: Mapping[str, Any],
    group: _VariableGroup,
) -> tuple[float, ...]:
    layer = next(row for row in design["front_layers"] if row["id"] == group.layer_ids[0])
    material = next(row for row in frozen["materials"] if row["id"] == layer["material_id"])
    optical = next(row for row in material["optical_sets"] if row["id"] == layer["optical_set_id"])
    sample = prepare_optical_set(optical, k_sampling_policy="source_only_zero_defaults")
    primary = _normalize_objectives(request.base_request.objectives)[0]
    wavelength = (float(primary.wavelength_min_nm) + float(primary.wavelength_max_nm)) / 2.0
    n, _k, _details = sample(wavelength, "nm", extrapolation_policy="error", include_k=False)
    n = float(n)
    if not math.isfinite(n) or n <= 0:
        raise ValueError("growth phase reference requires a finite positive refractive index")
    phase_base = wavelength / (2.0 * n)
    generated = [phase_base * fraction for fraction in request.growth_phase_fractions]
    generated.extend(float(value) for value in request.growth_steps_nm)
    result: list[float] = []
    seen: set[float] = set()
    for delta in generated:
        value = float(delta)
        # Preserve first occurrence so the phase-scaled grid is auditable.
        rounded = round(value, 12)
        if rounded > 0 and rounded not in seen:
            result.append(rounded)
            seen.add(rounded)
    return tuple(result)


def _as_structure_result(
    request: FilmStructureOptimizationRequest,
    best: Mapping[str, Any],
    counters: Mapping[str, Any],
    trace: Sequence[FilmStructureTraceEvent],
    *,
    state: str,
    message: str,
    termination_reason: str,
    is_final: bool,
) -> FilmStructureOptimizationResult:
    evaluation = best["evaluation"]
    score = evaluation["score"]
    validation = best.get("validation")
    complete = validation is not None
    base_result = _result_from_score(
        score, request.base_request,
        acceptable=float(request.base_request.acceptable_normalized_error),
        evaluations=int(counters["objective_evaluations"]),
        iterations=int(counters["local_iterations"]),
        state=state,
        message=message,
        thicknesses=_all_thicknesses(best["design"], request.base_request.frozen_material_snapshot),
    )
    base_result = replace(
        base_result,
        solver="baseline",
        seed=request.seed,
        termination_reason=termination_reason,
        failed_evaluations=int(counters["failed_evaluations"]),
        validation_samples_multiplier=request.base_request.validation_samples_multiplier,
        validation_evaluations=int(counters.get("validation_evaluations", 0)),
        validation_complete=complete,
        validation_max_normalized_error=(
            float(validation["score"]["max_error"]) if complete else None),
        validation_weighted_rms_error=(
            float(validation["score"]["weighted_rms_error"]) if complete else None),
        validation_targets_met=(
            bool(validation["score"]["max_error"] <= request.base_request.acceptable_normalized_error)
            if complete else None),
        validation_per_condition_errors=(tuple(validation["score"]["errors"]) if complete else ()),
        validation_message=("completed holdout validation" if complete else
                            str(best.get("validation_message", "validation not completed"))),
    )
    base_design = request.base_request.design_snapshot
    topology = _topology_audit(base_design, best["design"], best.get("scan", {}))
    topology["split_groups"] = deepcopy(dict(best.get("split_groups", {})))
    topology["layer_thickness_nm"] = _all_thicknesses(best["design"], request.base_request.frozen_material_snapshot)
    topology["total_thickness_nm"] = float(sum(topology["layer_thickness_nm"].values()))
    topology["limits_checked"] = {
        "max_total_thickness_nm": float(request.max_total_thickness_nm),
        "min_thickness_nm": float(request.min_thickness_nm),
        "max_thickness_nm": float(request.max_thickness_nm),
        "max_total_layers": int(request.max_total_layers),
        "all_hard_limits_satisfied": bool(_feasible_design(
            request, best["design"], request.base_request.frozen_material_snapshot)[0]),
    }
    bound_groups = _variable_groups(
        request, best["design"], request.base_request.frozen_material_snapshot,
        best.get("split_groups", {}), best.get("active_layer_ids", ()),
    )
    bounds: dict[str, Any] = {}
    actual = _physical_layers(best["design"], request.base_request.frozen_material_snapshot)
    for group in bound_groups:
        bounds[group.key] = {
            "layer_ids": list(group.layer_ids),
            "fractions": list(group.fractions),
            "value_nm": float(sum(actual[identifier] for identifier in group.layer_ids)),
            "lower_nm": float(group.lower_nm),
            "upper_nm": float(group.upper_nm),
            "satisfied": bool(group.lower_nm - 1e-8 <= sum(actual[identifier] for identifier in group.layer_ids)
                              <= group.upper_nm + 1e-8),
        }
    topology["selected_bound_groups"] = bounds
    topology["selected_bounds_satisfied"] = all(row["satisfied"] for row in bounds.values())
    structure_result = FilmStructureOptimizationResult(
        base_result=base_result,
        mode=request.mode,
        trace=tuple(trace),
        counters=FilmStructureCounters(**{
            key: value for key, value in counters.items()
            if key in FilmStructureCounters.__dataclass_fields__
        }),
        topology_audit=topology,
        added_needle_layer_ids=tuple(topology["added_needle_layer_ids"]),
        message=message,
        frozen_request=request,
        is_final=is_final,
        candidate_verified=False,
    )
    return structure_result


def optimize_film_design_with_structure(
    service: Any,
    request: FilmStructureOptimizationRequest,
    *,
    progress_callback: Callable[[FilmOptimizationProgress], None] | None = None,
    cancel_event: Any = None,
    checkpoint_callback: Callable[[FilmStructureOptimizationResult], None] | None = None,
) -> FilmStructureOptimizationResult:
    """Run one serial sensitivity scan, bounded growth stage, and local polish.

    A zero-thickness classical probe is evaluated only for its analytic needle
    sensitivity. Positive-thickness structures are independently proposed,
    projected/refined within hard nm and layer-count limits, exact-ranked by
    `(maximum normalized error, weighted RMS)`, and archived only after their
    denser validation grid completes. Validation and training metrics are kept
    separate. The total `base_request.max_evaluations` budget includes every
    complete training, Jacobian, and holdout-grid evaluation; candidate budget
    counts positive structure proposals independently.
    """
    frozen_request = freeze_film_structure_optimization_request(service, request)
    _request_design(frozen_request)
    frozen = frozen_request.base_request.frozen_material_snapshot
    base_design = deepcopy(dict(frozen_request.base_request.design_snapshot))
    feasible, reason = _feasible_design(frozen_request, base_design, frozen)
    if not feasible:
        raise ValueError(f"initial design violates a hard structure constraint: {reason}")

    counters: dict[str, Any] = asdict(FilmStructureCounters())
    trace: list[FilmStructureTraceEvent] = []
    sequence = [0]
    max_evaluations = frozen_request.base_request.max_evaluations
    best: dict[str, Any] | None = None
    termination = "completed"
    final_message = "bounded structure exploration completed"

    def trace_event(**kwargs: Any) -> None:
        trace.append(FilmStructureTraceEvent(sequence=sequence[0], **kwargs))
        sequence[0] += 1

    def notify(state: str, message: str) -> None:
        if progress_callback is None:
            return
        progress = FilmOptimizationProgress(
            state=state,
            fraction=min(1.0, counters["objective_evaluations"] / max_evaluations),
            iteration=int(counters["local_iterations"]),
            evaluations=int(counters["objective_evaluations"]),
            best_max_normalized_error=(
                float(best["evaluation"]["score"]["max_error"]) if best is not None else None),
            message=message,
        )
        try:
            progress_callback(progress)
        except Exception:
            # A UI progress observer cannot corrupt a deterministic compute run.
            pass

    def checkpoint(*, final: bool = False, state: str = "completed", message: str = "") -> None:
        if checkpoint_callback is None or best is None:
            return
        snapshot = _as_structure_result(
            frozen_request, best, counters, trace, state=state,
            message=message or "best candidate checkpoint",
            termination_reason=termination, is_final=final,
        )
        try:
            checkpoint_callback(snapshot)
        except Exception as exc:
            trace_event(stage="checkpoint", candidate_kind="best_candidate",
                        message=f"checkpoint callback failed: {type(exc).__name__}")

    def evaluate(
        design: Mapping[str, Any], *, variable_ids: Sequence[str] = (),
        with_jacobian: bool, validation: bool = False,
        eval_request: FilmStructureOptimizationRequest | None = None,
        zero_thickness_layer_ids: Sequence[str] = (),
        derived_validation_grid: bool = False,
    ) -> Mapping[str, Any] | None:
        if _cancelled(cancel_event):
            raise _StructureCancelled()
        if counters["objective_evaluations"] >= max_evaluations:
            counters["evaluation_budget_exhausted"] = True
            return None
        counters["objective_evaluations"] += 1
        if validation:
            counters["validation_evaluations"] += 1
        target_request = eval_request or frozen_request
        try:
            output = evaluate_frozen_structure_candidate(
                target_request, design, variable_ids=tuple(variable_ids),
                cancel_event=cancel_event, with_jacobian=with_jacobian,
                zero_thickness_layer_ids=zero_thickness_layer_ids,
                derived_validation_grid=derived_validation_grid,
            )
        except _StructureCancelled as exc:
            counters["jacobian_evaluations"] += int(exc.jacobian_calls)
            raise
        except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError):
            counters["failed_evaluations"] += 1
            raise
        counters["score_evaluations"] += 1
        counters["jacobian_evaluations"] += int(output["jacobian_calls"])
        return output

    def validation_for(design: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return evaluate(design, with_jacobian=False, validation=True,
                        eval_request=frozen_request, derived_validation_grid=True)

    def trace_pending_validation(
        candidate_kind: str,
        candidate_id: str,
        evaluation: Mapping[str, Any],
        outcome: str,
    ) -> None:
        trace_event(
            stage="validation", candidate_kind=candidate_kind, candidate_id=candidate_id,
            max_normalized_error=float(evaluation["score"]["max_error"]),
            weighted_rms_error=float(evaluation["score"]["weighted_rms_error"]),
            accepted=False, verified=False,
            message=(
                f"{outcome}; improved training candidate holdout remains pending; "
                "retained the previously validated incumbent"
            ),
        )

    if _cancelled(cancel_event):
        termination = "cancelled_before_first_score"
        final_message = "cancelled before any candidate was scored"
        counters["objective_evaluations"] = 0
        # Keep a finite sentinel and an explicit message; no score is claimed.
        empty_score = {
            "design": deepcopy(base_design), "errors": (),
            "max_error": float(np.finfo(float).max),
            "weighted_max_error": float(np.finfo(float).max),
            "weighted_rms_error": float(np.finfo(float).max),
            "key": (1.0, float(np.finfo(float).max)),
        }
        best = {"design": base_design, "evaluation": {"score": empty_score},
                "validation": None, "split_groups": {}, "active_layer_ids": (),
                "scan": {}, "validation_message": final_message}
        result = _as_structure_result(
            frozen_request, best, counters, trace, state="cancelled",
            message=final_message, termination_reason=termination, is_final=True)
        checkpoint(final=True, state="cancelled", message=final_message)
        return result

    try:
        initial = evaluate(base_design, variable_ids=tuple(variable.layer_id for variable in frozen_request.base_request.variables), with_jacobian=True)
        if initial is None:
            raise ValueError("evaluation budget cannot score the initial design")
        best = {
            "design": deepcopy(base_design), "evaluation": initial,
            "validation": None, "split_groups": {}, "active_layer_ids": (),
            "needle_ids": (), "scan": {},
            "validation_message": "baseline validation has not completed",
        }
        notify("running", "scored the frozen base design")
        checkpoint(state="completed", message="base design scored; validation pending")
        try:
            baseline_validation = validation_for(base_design)
            if baseline_validation is not None:
                best["validation"] = baseline_validation
                best["validation_message"] = "completed holdout validation"
            else:
                best["validation_message"] = "evaluation budget ended before base validation"
        except _StructureCancelled:
            best["validation_message"] = "cancelled during base validation"
            termination = "cancelled"
            final_message = "cancelled during base validation; retained the scored base design"
            trace_event(stage="validation", candidate_kind="base",
                        message=best["validation_message"])
        except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError) as exc:
            best["validation_message"] = f"base validation failed: {type(exc).__name__}"
            termination = "failed_after_best"
            final_message = "base holdout validation failed; retained the complete training score"
            trace_event(stage="validation", candidate_kind="base",
                        message=best["validation_message"])
        if termination != "failed_after_best":
            checkpoint(state="completed", message="base design is current history best")

        base_variable_ids = tuple(variable.layer_id for variable in frozen_request.base_request.variables)
        if termination == "failed_after_best":
            baseline_design, baseline_eval, baseline_cancelled = deepcopy(base_design), initial, False
        else:
            baseline_design, baseline_eval, baseline_cancelled = _local_refine(
                frozen_request, base_design, initial, frozen, {}, (), counters, trace, cancel_event,
                max_iterations=max(0, frozen_request.base_request.max_iterations // (4 if frozen_request.mode == "combined" else 2)),
                evaluation_budget=max_evaluations - (6 if frozen_request.mode == "combined" else 2),
                sequence=sequence, emit=notify,
            )
        if baseline_cancelled:
            termination = "cancelled"
            final_message = "cancelled during baseline local refinement; retained scored baseline"
        else:
            base_design = baseline_design
            if (termination != "failed_after_best" and
                    baseline_eval["score"]["key"] < best["evaluation"]["score"]["key"]):
                try:
                    baseline_verified = validation_for(baseline_design)
                except _StructureCancelled:
                    baseline_verified = None
                    termination = "cancelled"
                    final_message = "cancelled during baseline-polish holdout; retained the validated baseline"
                    trace_pending_validation(
                        "baseline", "baseline", baseline_eval,
                        "cancelled during polished-baseline validation",
                    )
                except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError) as exc:
                    baseline_verified = None
                    termination = "failed_after_best"
                    final_message = "baseline-polish holdout failed; retained the validated baseline"
                    trace_pending_validation(
                        "baseline", "baseline", baseline_eval,
                        f"polished-baseline validation failed: {type(exc).__name__}",
                    )
                if baseline_verified is not None:
                    best = {"design": deepcopy(baseline_design), "evaluation": baseline_eval,
                            "validation": baseline_verified, "split_groups": {}, "active_layer_ids": (),
                            "needle_ids": (), "scan": {}, "validation_message": "completed holdout validation"}
                    checkpoint(state="completed", message="validated baseline polish archived")
        baseline_branch = {"design": deepcopy(base_design), "evaluation": baseline_eval,
                           "split_groups": {}, "needle_ids": (), "candidate_id": "baseline"}
        trace_event(
            stage="baseline_polish", candidate_kind="baseline", candidate_id="baseline",
            max_normalized_error=float(baseline_eval["score"]["max_error"]),
            weighted_rms_error=float(baseline_eval["score"]["weighted_rms_error"]),
            selected=True,
            message="no-needle baseline local polish completed before structure exploration",
        )

        if termination == "cancelled" or (termination != "failed_after_best" and _cancelled(cancel_event)):
            termination = "cancelled"
            final_message = "cancelled; retained the best fully scored history candidate"
        elif termination == "failed_after_best":
            pass
        else:
            base_variable_ids = tuple(variable.layer_id for variable in frozen_request.base_request.variables)
            scan_audit: dict[str, Any] = {
                "position_samples_per_host": int(frozen_request.position_samples),
                "position_semantics": "all interfaces plus k/(position_samples+1) in every host layer",
                "requested_site_count": 0, "scanned_site_count": 0,
                "requested_probe_count": 0, "scanned_probe_count": 0,
                "unscanned_probe_count": 0, "fully_scanned": True,
                "scanned_positions": [], "unscanned_reason": "",
                "needle_thickness_samples_nm": [],
            }
            needle_stage = "needle_initial"
            needle_ranked: list[dict[str, Any]] = []
            if frozen_request.mode in ("needle_only", "combined"):
                sites = _site_records(base_design, frozen, frozen_request.position_samples)
                scan_audit["requested_site_count"] = len(sites)
                probe_pairs = [
                    (site, reference)
                    for site in sites
                    for reference in frozen_request.candidate_materials
                    if len(base_design["front_layers"]) + (2 if site["kind"] == "interior" else 1)
                    <= frozen_request.max_total_layers
                ]
                scan_audit["requested_probe_count"] = len(probe_pairs)
                remaining = max_evaluations - counters["objective_evaluations"]
                reserve = 6 if frozen_request.mode == "combined" else 3
                scan_budget = max(0, remaining - reserve)
                if len(probe_pairs) > scan_budget:
                    rng = np.random.default_rng(frozen_request.seed)
                    strata = np.linspace(0, len(probe_pairs), num=scan_budget + 1)
                    chosen: list[int] = []
                    for index in range(scan_budget):
                        low, high = int(math.floor(strata[index])), int(math.floor(strata[index + 1]))
                        high = max(high, low + 1)
                        chosen.append(int(rng.integers(low, min(high, len(probe_pairs)))))
                    probe_pairs = [probe_pairs[index] for index in chosen]
                    scan_audit["fully_scanned"] = False
                    scan_audit["unscanned_reason"] = "evaluation budget reserved for finite proposal/refinement/validation"
                scan_audit["scanned_probe_count"] = len(probe_pairs)
                scan_audit["unscanned_probe_count"] = max(
                    0, int(scan_audit["requested_probe_count"]) - len(probe_pairs))
                if not probe_pairs and scan_audit["requested_probe_count"]:
                    scan_audit["fully_scanned"] = False
                    scan_audit["unscanned_reason"] = "evaluation budget left no complete sensitivity-probe slot"
                seen_position_keys: set[str] = set()
                for probe_index, (site, reference) in enumerate(probe_pairs):
                    if _cancelled(cancel_event):
                        raise _StructureCancelled()
                    existing = {row["id"] for row in base_design["front_layers"]}
                    needle_id = _stable_identifier("structure-needle", [
                        frozen_request.base_request.design_revision, frozen_request.seed, needle_stage,
                        reference.material_id, reference.optical_set_id, site["kind"],
                        site["interface"], site.get("host_layer_id", ""),
                        site.get("fraction"), site["position_nm"],
                    ], existing)
                    split_id = _stable_identifier("structure-split", [
                        needle_id, site.get("host_layer_id", ""), site.get("fraction"),
                    ], existing | {needle_id})
                    zero_design = _insert_at_site(
                        base_design, site, reference, 0.0, needle_id, split_id, zero=True)
                    output = evaluate(
                        zero_design, variable_ids=(needle_id,), with_jacobian=True,
                        zero_thickness_layer_ids=(needle_id,),
                    )
                    if output is None:
                        scan_audit["fully_scanned"] = False
                        scan_audit["unscanned_reason"] = "evaluation budget exhausted during sensitivity scan"
                        break
                    gradient = float(output["gradient_by_layer_id"][needle_id])
                    if not math.isfinite(gradient):
                        counters["failed_evaluations"] += 1
                        continue
                    position_key = json.dumps([
                        site["kind"], site["interface"], site.get("host_layer_id", ""),
                        site.get("fraction"), site["position_nm"],
                    ], separators=(",", ":"), allow_nan=False)
                    seen_position_keys.add(position_key)
                    needle_ranked.append({
                        "site": deepcopy(dict(site)), "reference": reference,
                        "needle_id": needle_id, "split_id": split_id,
                        "smooth_loss": float(output["smooth_loss"]),
                        "gradient": gradient,
                        "score": output["score"],
                        "probe_index": probe_index,
                    })
                    trace_event(
                        stage=f"{needle_stage}_sensitivity", candidate_kind="zero_probe",
                        candidate_id=needle_id, host_layer_id=str(site.get("host_layer_id", "")),
                        material_id=reference.material_id, optical_set_id=reference.optical_set_id,
                        position_nm=float(site["position_nm"]),
                        position_fraction=(float(site["fraction"]) if site.get("fraction") is not None else None),
                        max_normalized_error=float(output["score"]["max_error"]),
                        weighted_rms_error=float(output["score"]["weighted_rms_error"]),
                        message=f"exact-zero probe; d(smooth SSE)/d(thickness_nm)={gradient:.12g}",
                    )
                scan_audit["scanned_site_count"] = len(seen_position_keys)
                scan_audit["scanned_positions"] = [
                    {"kind": row["site"]["kind"], "interface": row["site"]["interface"],
                     "host_layer_id": row["site"].get("host_layer_id", ""),
                     "fraction": row["site"].get("fraction"),
                     "position_nm": float(row["site"]["position_nm"])}
                    for row in needle_ranked
                ]
                counters["needle_probe_count"] = len(needle_ranked)

                rng = np.random.default_rng(frozen_request.seed)
                tie_order = rng.permutation(max(1, len(needle_ranked)))
                tie_rank = {int(item): rank for rank, item in enumerate(tie_order)}
                needle_ranked.sort(key=lambda row: (
                    float(row["gradient"]), tie_rank.get(int(row["probe_index"]), 0)))
                thickness_grid = np.linspace(
                    frozen_request.min_thickness_nm,
                    frozen_request.max_thickness_nm,
                    frozen_request.needle_thickness_samples,
                )
                scan_audit["needle_thickness_samples_nm"] = [float(v) for v in thickness_grid]
                needle_slot = (frozen_request.candidate_budget if frozen_request.mode == "needle_only"
                               else (max(1, frozen_request.candidate_budget // 3) if needle_stage == "needle_initial"
                                     else max(1, frozen_request.candidate_budget - 2 * max(1, frozen_request.candidate_budget // 3))))
                pair_cap = min(len(needle_ranked), max(1, int(math.ceil(needle_slot / 2))))
                finite_trials: list[dict[str, Any]] = []
                for rank, probe in enumerate(needle_ranked[:pair_cap]):
                    # A seeded cyclic grid assignment spreads a small proposal
                    # budget over both position and finite thickness.
                    start_index = (rank * 3 + frozen_request.seed) % len(thickness_grid)
                    for offset in range(len(thickness_grid)):
                        thickness_index = (start_index + offset) % len(thickness_grid)
                        thickness = float(thickness_grid[thickness_index])
                        site = probe["site"]
                        trial_design = _insert_at_site(
                            base_design, site, probe["reference"], thickness,
                            probe["needle_id"], probe["split_id"], zero=False)
                        okay, _why = _feasible_design(frozen_request, trial_design, frozen)
                        if not okay:
                            continue
                        finite_trials.append({
                            **probe, "design": trial_design,
                            "thickness": thickness,
                            "prediction": float(probe["smooth_loss"] + probe["gradient"] * thickness),
                            "split_groups": ({
                                str(site["host_layer_id"]): _split_group_for(site, probe["split_id"])
                            } if site["kind"] == "interior" else {}),
                            "thickness_index": thickness_index,
                        })
                if len(finite_trials) > needle_slot:
                    # Stride across the ranked location × thickness grid.
                    indices = np.linspace(0, len(finite_trials) - 1, needle_slot, dtype=int)
                    finite_trials = [finite_trials[int(index)] for index in indices]
                finite_trials.sort(key=lambda row: (row["prediction"], row["probe_index"], row["thickness_index"]))
                scan_audit["finite_proposal_count_available"] = len(finite_trials)
                for proposal in finite_trials:
                    if counters["candidate_count"] >= frozen_request.candidate_budget:
                        counters["candidate_budget_exhausted"] = True
                        break
                    reserve = 4 if frozen_request.mode == "combined" else 2
                    if max_evaluations - counters["objective_evaluations"] <= reserve:
                        counters["evaluation_budget_exhausted"] = True
                        break
                    candidate = proposal["design"]
                    split_groups = proposal["split_groups"]
                    needle_ids = (proposal["needle_id"],)
                    active_ids = tuple((*base_variable_ids, *needle_ids))
                    jac_ids = _jacobian_layer_ids(
                        frozen_request, candidate, frozen, split_groups, needle_ids)
                    counters["candidate_count"] += 1
                    counters["needle_candidate_count"] += 1
                    try:
                        start_eval = evaluate(candidate, variable_ids=jac_ids, with_jacobian=True)
                        if start_eval is None:
                            break
                    except _StructureCancelled:
                        raise
                    except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError) as exc:
                        trace_event(stage=f"{needle_stage}_proposal", candidate_kind="needle",
                                    candidate_id=proposal["needle_id"],
                                    message=f"finite proposal scoring failed: {type(exc).__name__}")
                        continue
                    trace_event(
                        stage=f"{needle_stage}_proposal", candidate_kind="needle",
                        candidate_id=proposal["needle_id"],
                        host_layer_id=str(proposal["site"].get("host_layer_id", "")),
                        material_id=proposal["reference"].material_id,
                        optical_set_id=proposal["reference"].optical_set_id,
                        position_nm=float(proposal["site"]["position_nm"]),
                        position_fraction=(float(proposal["site"]["fraction"])
                                           if proposal["site"].get("fraction") is not None else None),
                        thickness_nm=float(proposal["thickness"]),
                        max_normalized_error=float(start_eval["score"]["max_error"]),
                        weighted_rms_error=float(start_eval["score"]["weighted_rms_error"]),
                        parent_candidate_id=needle_stage, selected=False,
                        message="positive-thickness proposal passed all hard constraints before scoring",
                    )
                    remaining_reserve = 4 if frozen_request.mode == "combined" else 1
                    polished, polished_eval, was_cancelled = _local_refine(
                        frozen_request, candidate, start_eval, frozen, split_groups, needle_ids,
                        counters, trace, cancel_event,
                        max_iterations=max(0, frozen_request.base_request.max_iterations - counters["local_iterations"]),
                        evaluation_budget=max_evaluations - remaining_reserve,
                        sequence=sequence,
                        emit=notify,
                    )
                    if was_cancelled:
                        raise _StructureCancelled()
                    if polished_eval["score"]["key"] < best["evaluation"]["score"]["key"]:
                        if counters["objective_evaluations"] < max_evaluations:
                            try:
                                verified = validation_for(polished)
                            except _StructureCancelled:
                                trace_pending_validation(
                                    "needle", str(proposal["needle_id"]), polished_eval,
                                    "cancelled during needle-candidate validation",
                                )
                                raise
                            except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError) as exc:
                                trace_pending_validation(
                                    "needle", str(proposal["needle_id"]), polished_eval,
                                    f"needle-candidate validation failed: {type(exc).__name__}",
                                )
                                raise
                            if verified is not None:
                                best = {
                                    "design": deepcopy(polished), "evaluation": polished_eval,
                                    "validation": verified, "split_groups": deepcopy(split_groups),
                                    "active_layer_ids": needle_ids, "needle_ids": needle_ids,
                                    "scan": deepcopy(scan_audit),
                                    "validation_message": "completed holdout validation",
                                }
                                trace_event(stage="archive", candidate_kind="needle",
                                            candidate_id=proposal["needle_id"],
                                            max_normalized_error=float(polished_eval["score"]["max_error"]),
                                            weighted_rms_error=float(polished_eval["score"]["weighted_rms_error"]),
                                            accepted=True, verified=True,
                                            message="exact lexicographic training improvement passed holdout validation")
                                notify("running", "archived a validated needle structure")
                                checkpoint(state="completed", message="validated needle best archived")
                    if _cancelled(cancel_event):
                        raise _StructureCancelled()
            growth_branch: dict[str, Any] | None = None
            if frozen_request.mode in ("growth_only", "combined") and not _cancelled(cancel_event):
                best["scan"] = deepcopy(scan_audit if frozen_request.mode == "combined" else {
                    "position_samples_per_host": frozen_request.position_samples,
                    "requested_site_count": 0, "scanned_site_count": 0,
                    "requested_probe_count": 0, "scanned_probe_count": 0,
                    "unscanned_probe_count": 0, "fully_scanned": False,
                    "unscanned_reason": "growth-only mode does not run needle sensitivity",
                    "needle_thickness_samples_nm": [], "scanned_positions": [],
                })
                current_design = baseline_branch["design"]
                current_split = {}
                current_needles = ()
                groups = _variable_groups(
                    frozen_request, current_design, frozen, current_split, current_needles)
                selected_keys = {variable.layer_id for variable in frozen_request.base_request.variables}
                eligible = [group for group in groups
                            if group.key in selected_keys or group.key in current_needles]
                if not eligible:
                    trace_event(stage="growth", candidate_kind="growth",
                                message="no_eligible_layer: only selected base variables and archived needle rows may grow")
                else:
                    growth_trials: list[dict[str, Any]] = []
                    growth_branch: dict[str, Any] | None = None
                    actual = _physical_layers(current_design, frozen)
                    current_values = np.asarray([_group_value(group, actual) for group in groups], dtype=float)
                    key_index = {group.key: index for index, group in enumerate(groups)}
                    for group in eligible:
                        try:
                            deltas = _phase_growth_steps(frozen_request, current_design, frozen, group)
                        except (ValueError, ArithmeticError, KeyError):
                            deltas = tuple(float(value) for value in frozen_request.growth_steps_nm)
                        for delta_index, delta in enumerate(deltas):
                            proposed_values = current_values.copy()
                            group_index = key_index[group.key]
                            proposed_values[group_index] += float(delta)
                            proposed_group_value = float(proposed_values[group_index])
                            if (proposed_group_value < group.lower_nm or
                                    proposed_group_value > group.upper_nm):
                                trace_event(
                                    stage="growth", candidate_kind="growth",
                                    candidate_id=group.key, thickness_nm=float(delta),
                                    accepted=False,
                                    message=(
                                        "growth step exceeds the selected-layer total thickness bounds; "
                                        "candidate was rejected before scoring"
                                    ),
                                )
                                continue
                            trial_design = _design_with_group_values(
                                current_design, groups, proposed_values, frozen)
                            okay, _why = _feasible_design(frozen_request, trial_design, frozen)
                            if not okay:
                                continue
                            growth_trials.append({
                                "group": group, "delta": float(delta),
                                "delta_index": delta_index, "design": trial_design,
                                "prediction": float(delta),
                            })
                    stage_slots = frozen_request.candidate_budget - counters["candidate_count"]
                    if frozen_request.mode == "combined":
                        stage_slots = min(stage_slots, max(1, frozen_request.candidate_budget // 3))
                    if len(growth_trials) > stage_slots:
                        indices = np.linspace(0, len(growth_trials) - 1, max(0, stage_slots), dtype=int) if stage_slots else []
                        growth_trials = [growth_trials[int(index)] for index in indices]
                    for proposal in growth_trials:
                        if counters["candidate_count"] >= frozen_request.candidate_budget:
                            counters["candidate_budget_exhausted"] = True
                            break
                        if counters["objective_evaluations"] >= max_evaluations - 1:
                            counters["evaluation_budget_exhausted"] = True
                            break
                        candidate = proposal["design"]
                        group = proposal["group"]
                        needle_ids = current_needles
                        active_ids = tuple((*base_variable_ids, *needle_ids))
                        jac_ids = _jacobian_layer_ids(
                            frozen_request, candidate, frozen, current_split, needle_ids)
                        counters["candidate_count"] += 1
                        counters["growth_proposal_count"] += 1
                        try:
                            start_eval = evaluate(candidate, variable_ids=jac_ids, with_jacobian=True)
                            if start_eval is None:
                                break
                        except _StructureCancelled:
                            raise
                        except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError) as exc:
                            trace_event(stage="growth", candidate_kind="growth",
                                        candidate_id=group.key, thickness_nm=float(proposal["delta"]),
                                        message=f"growth proposal scoring failed: {type(exc).__name__}")
                            continue
                        trace_event(
                            stage="growth", candidate_kind="growth",
                            candidate_id=group.key, thickness_nm=float(proposal["delta"]),
                            max_normalized_error=float(start_eval["score"]["max_error"]),
                            weighted_rms_error=float(start_eval["score"]["weighted_rms_error"]),
                            message="positive growth was checked against every hard bound before scoring",
                        )
                        polished, polished_eval, was_cancelled = _local_refine(
                            frozen_request, candidate, start_eval, frozen, current_split, needle_ids,
                            counters, trace, cancel_event,
                            max_iterations=max(0, frozen_request.base_request.max_iterations - counters["local_iterations"]),
                            evaluation_budget=max_evaluations - 1,
                            sequence=sequence,
                            emit=notify,
                        )
                        if was_cancelled:
                            raise _StructureCancelled()
                        if growth_branch is None or polished_eval["smooth_loss"] < growth_branch["evaluation"]["smooth_loss"]:
                            growth_branch = {"design": deepcopy(polished), "evaluation": polished_eval,
                                             "split_groups": deepcopy(current_split),
                                             "needle_ids": tuple(needle_ids), "candidate_id": group.key}
                        trace_event(stage="growth_escape", candidate_kind="growth_trial",
                                    candidate_id=group.key, thickness_nm=float(proposal["delta"]),
                                    max_normalized_error=float(polished_eval["score"]["max_error"]),
                                    weighted_rms_error=float(polished_eval["score"]["weighted_rms_error"]),
                                    parent_candidate_id="baseline", selected=False,
                                    message="polished escape trial scored; exact archive test follows")
                        if polished_eval["score"]["key"] < best["evaluation"]["score"]["key"]:
                            try:
                                verified = validation_for(polished)
                            except _StructureCancelled:
                                trace_pending_validation(
                                    "growth", group.key, polished_eval,
                                    "cancelled during growth-candidate validation",
                                )
                                raise
                            except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError) as exc:
                                trace_pending_validation(
                                    "growth", group.key, polished_eval,
                                    f"growth-candidate validation failed: {type(exc).__name__}",
                                )
                                raise
                            if verified is not None:
                                best = {
                                    "design": deepcopy(polished), "evaluation": polished_eval,
                                    "validation": verified, "split_groups": deepcopy(current_split),
                                    "active_layer_ids": needle_ids, "needle_ids": needle_ids,
                                    "scan": deepcopy(best.get("scan", {})),
                                    "validation_message": "completed holdout validation",
                                }
                                trace_event(stage="archive", candidate_kind="growth",
                                            candidate_id=group.key,
                                            max_normalized_error=float(polished_eval["score"]["max_error"]),
                                            weighted_rms_error=float(polished_eval["score"]["weighted_rms_error"]),
                                            accepted=True, verified=True,
                                            message="exact lexicographic training improvement passed holdout validation")
                                checkpoint(state="completed", message="validated growth best archived")
            
            if growth_branch is not None:
                trace_event(stage="growth_escape", candidate_kind="growth_branch",
                            candidate_id=str(growth_branch["candidate_id"]),
                            max_normalized_error=float(growth_branch["evaluation"]["score"]["max_error"]),
                            weighted_rms_error=float(growth_branch["evaluation"]["score"]["weighted_rms_error"]),
                            selected=True, accepted=False, parent_candidate_id="baseline",
                            message="one growth escape branch selected by smooth SSE")
            if frozen_request.mode == "combined" and growth_branch is not None and not _cancelled(cancel_event):
                base_design = growth_branch["design"]
                current_split = growth_branch.get("split_groups", {})
                current_needles = tuple(growth_branch.get("needle_ids", ()))
                needle_stage = "needle_after_growth"
                scan_audit = {"position_samples_per_host": int(frozen_request.position_samples), "position_semantics": "all interfaces plus k/(position_samples+1) in every host layer", "requested_site_count": 0, "scanned_site_count": 0, "requested_probe_count": 0, "scanned_probe_count": 0, "unscanned_probe_count": 0, "fully_scanned": True, "scanned_positions": [], "unscanned_reason": "", "needle_thickness_samples_nm": []}
                needle_ranked: list[dict[str, Any]] = []
                if frozen_request.mode in ("needle_only", "combined"):
                    sites = _site_records(base_design, frozen, frozen_request.position_samples)
                    scan_audit["requested_site_count"] = len(sites)
                    probe_pairs = [
                        (site, reference)
                        for site in sites
                        for reference in frozen_request.candidate_materials
                        if len(base_design["front_layers"]) + (2 if site["kind"] == "interior" else 1)
                        <= frozen_request.max_total_layers
                    ]
                    scan_audit["requested_probe_count"] = len(probe_pairs)
                    remaining = max_evaluations - counters["objective_evaluations"]
                    reserve = 6 if frozen_request.mode == "combined" else 3
                    scan_budget = max(0, remaining - reserve)
                    if len(probe_pairs) > scan_budget:
                        rng = np.random.default_rng(frozen_request.seed)
                        strata = np.linspace(0, len(probe_pairs), num=scan_budget + 1)
                        chosen: list[int] = []
                        for index in range(scan_budget):
                            low, high = int(math.floor(strata[index])), int(math.floor(strata[index + 1]))
                            high = max(high, low + 1)
                            chosen.append(int(rng.integers(low, min(high, len(probe_pairs)))))
                        probe_pairs = [probe_pairs[index] for index in chosen]
                        scan_audit["fully_scanned"] = False
                        scan_audit["unscanned_reason"] = "evaluation budget reserved for finite proposal/refinement/validation"
                    scan_audit["scanned_probe_count"] = len(probe_pairs)
                    scan_audit["unscanned_probe_count"] = max(
                        0, int(scan_audit["requested_probe_count"]) - len(probe_pairs))
                    if not probe_pairs and scan_audit["requested_probe_count"]:
                        scan_audit["fully_scanned"] = False
                        scan_audit["unscanned_reason"] = "evaluation budget left no complete sensitivity-probe slot"
                    seen_position_keys: set[str] = set()
                    for probe_index, (site, reference) in enumerate(probe_pairs):
                        existing = {row["id"] for row in base_design["front_layers"]}
                        needle_id = _stable_identifier("structure-needle", [
                            frozen_request.base_request.design_revision, frozen_request.seed,
                            reference.material_id, reference.optical_set_id, site["kind"],
                            site["interface"], site.get("host_layer_id", ""),
                            site.get("fraction"), site["position_nm"],
                        ], existing)
                        split_id = _stable_identifier("structure-split", [
                            needle_id, site.get("host_layer_id", ""), site.get("fraction"),
                        ], existing | {needle_id})
                        zero_design = _insert_at_site(
                            base_design, site, reference, 0.0, needle_id, split_id, zero=True)
                        output = evaluate(
                            zero_design, variable_ids=(needle_id,), with_jacobian=True,
                            zero_thickness_layer_ids=(needle_id,),
                        )
                        if output is None:
                            scan_audit["fully_scanned"] = False
                            scan_audit["unscanned_reason"] = "evaluation budget exhausted during sensitivity scan"
                            break
                        gradient = float(output["gradient_by_layer_id"][needle_id])
                        if not math.isfinite(gradient):
                            counters["failed_evaluations"] += 1
                            continue
                        position_key = json.dumps([
                            site["kind"], site["interface"], site.get("host_layer_id", ""),
                            site.get("fraction"), site["position_nm"],
                        ], separators=(",", ":"), allow_nan=False)
                        seen_position_keys.add(position_key)
                        needle_ranked.append({
                            "site": deepcopy(dict(site)), "reference": reference,
                            "needle_id": needle_id, "split_id": split_id,
                            "smooth_loss": float(output["smooth_loss"]),
                            "gradient": gradient,
                            "score": output["score"],
                            "probe_index": probe_index,
                        })
                        trace_event(
                            stage=f"{needle_stage}_sensitivity", candidate_kind="zero_probe",
                            candidate_id=needle_id, host_layer_id=str(site.get("host_layer_id", "")),
                            material_id=reference.material_id, optical_set_id=reference.optical_set_id,
                            position_nm=float(site["position_nm"]),
                            position_fraction=(float(site["fraction"]) if site.get("fraction") is not None else None),
                            max_normalized_error=float(output["score"]["max_error"]),
                            weighted_rms_error=float(output["score"]["weighted_rms_error"]),
                            message=f"exact-zero probe; d(smooth SSE)/d(thickness_nm)={gradient:.12g}",
                        )
                    scan_audit["scanned_site_count"] = len(seen_position_keys)
                    scan_audit["scanned_positions"] = [
                        {"kind": row["site"]["kind"], "interface": row["site"]["interface"],
                         "host_layer_id": row["site"].get("host_layer_id", ""),
                         "fraction": row["site"].get("fraction"),
                         "position_nm": float(row["site"]["position_nm"])}
                        for row in needle_ranked
                    ]
                    counters["needle_probe_count"] = len(needle_ranked)

                    rng = np.random.default_rng(frozen_request.seed)
                    tie_order = rng.permutation(max(1, len(needle_ranked)))
                    tie_rank = {int(item): rank for rank, item in enumerate(tie_order)}
                    needle_ranked.sort(key=lambda row: (
                        float(row["gradient"]), tie_rank.get(int(row["probe_index"]), 0)))
                    thickness_grid = np.linspace(
                        frozen_request.min_thickness_nm,
                        frozen_request.max_thickness_nm,
                        frozen_request.needle_thickness_samples,
                    )
                    scan_audit["needle_thickness_samples_nm"] = [float(v) for v in thickness_grid]
                    needle_slot = (frozen_request.candidate_budget if frozen_request.mode == "needle_only"
                                   else max(1, (frozen_request.candidate_budget + 1) // 2))
                    pair_cap = min(len(needle_ranked), max(1, int(math.ceil(needle_slot / 2))))
                    finite_trials: list[dict[str, Any]] = []
                    for rank, probe in enumerate(needle_ranked[:pair_cap]):
                        # A seeded cyclic grid assignment spreads a small proposal
                        # budget over both position and finite thickness.
                        start_index = (rank * 3 + frozen_request.seed) % len(thickness_grid)
                        for offset in range(len(thickness_grid)):
                            thickness_index = (start_index + offset) % len(thickness_grid)
                            thickness = float(thickness_grid[thickness_index])
                            site = probe["site"]
                            trial_design = _insert_at_site(
                                base_design, site, probe["reference"], thickness,
                                probe["needle_id"], probe["split_id"], zero=False)
                            okay, _why = _feasible_design(frozen_request, trial_design, frozen)
                            if not okay:
                                continue
                            finite_trials.append({
                                **probe, "design": trial_design,
                                "thickness": thickness,
                                "prediction": float(probe["smooth_loss"] + probe["gradient"] * thickness),
                                "split_groups": ({
                                    str(site["host_layer_id"]): _split_group_for(site, probe["split_id"])
                                } if site["kind"] == "interior" else {}),
                                "thickness_index": thickness_index,
                            })
                    if len(finite_trials) > needle_slot:
                        # Stride across the ranked location × thickness grid.
                        indices = np.linspace(0, len(finite_trials) - 1, needle_slot, dtype=int)
                        finite_trials = [finite_trials[int(index)] for index in indices]
                    finite_trials.sort(key=lambda row: (row["prediction"], row["probe_index"], row["thickness_index"]))
                    scan_audit["finite_proposal_count_available"] = len(finite_trials)
                    for proposal in finite_trials:
                        if counters["candidate_count"] >= frozen_request.candidate_budget:
                            counters["candidate_budget_exhausted"] = True
                            break
                        reserve = 4 if frozen_request.mode == "combined" else 2
                        if max_evaluations - counters["objective_evaluations"] <= reserve:
                            counters["evaluation_budget_exhausted"] = True
                            break
                        candidate = proposal["design"]
                        split_groups = proposal["split_groups"]
                        needle_ids = (proposal["needle_id"],)
                        active_ids = tuple((*base_variable_ids, *needle_ids))
                        jac_ids = _jacobian_layer_ids(
                            frozen_request, candidate, frozen, split_groups, needle_ids)
                        counters["candidate_count"] += 1
                        counters["needle_candidate_count"] += 1
                        try:
                            start_eval = evaluate(candidate, variable_ids=jac_ids, with_jacobian=True)
                            if start_eval is None:
                                break
                        except _StructureCancelled:
                            raise
                        except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError) as exc:
                            trace_event(stage=f"{needle_stage}_proposal", candidate_kind="needle",
                                        candidate_id=proposal["needle_id"],
                                        message=f"finite proposal scoring failed: {type(exc).__name__}")
                            continue
                        trace_event(
                            stage=f"{needle_stage}_proposal", candidate_kind="needle",
                            candidate_id=proposal["needle_id"],
                            host_layer_id=str(proposal["site"].get("host_layer_id", "")),
                            material_id=proposal["reference"].material_id,
                            optical_set_id=proposal["reference"].optical_set_id,
                            position_nm=float(proposal["site"]["position_nm"]),
                            position_fraction=(float(proposal["site"]["fraction"])
                                               if proposal["site"].get("fraction") is not None else None),
                            thickness_nm=float(proposal["thickness"]),
                            max_normalized_error=float(start_eval["score"]["max_error"]),
                            weighted_rms_error=float(start_eval["score"]["weighted_rms_error"]),
                            parent_candidate_id=needle_stage, selected=False,
                        message="positive-thickness proposal passed all hard constraints before scoring",
                        )
                        remaining_reserve = 4 if frozen_request.mode == "combined" else 1
                        polished, polished_eval, was_cancelled = _local_refine(
                            frozen_request, candidate, start_eval, frozen, split_groups, needle_ids,
                            counters, trace, cancel_event,
                            max_iterations=max(0, frozen_request.base_request.max_iterations - counters["local_iterations"]),
                            evaluation_budget=max_evaluations - remaining_reserve,
                            sequence=sequence,
                            emit=notify,
                        )
                        if was_cancelled:
                            raise _StructureCancelled()
                        if polished_eval["score"]["key"] < best["evaluation"]["score"]["key"]:
                            if counters["objective_evaluations"] < max_evaluations:
                                try:
                                    verified = validation_for(polished)
                                except _StructureCancelled:
                                    trace_pending_validation(
                                        "needle", str(proposal["needle_id"]), polished_eval,
                                        "cancelled during needle-candidate validation",
                                    )
                                    raise
                                except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError) as exc:
                                    trace_pending_validation(
                                        "needle", str(proposal["needle_id"]), polished_eval,
                                        f"needle-candidate validation failed: {type(exc).__name__}",
                                    )
                                    raise
                                if verified is not None:
                                    best = {
                                        "design": deepcopy(polished), "evaluation": polished_eval,
                                        "validation": verified, "split_groups": deepcopy(split_groups),
                                        "active_layer_ids": needle_ids, "needle_ids": needle_ids,
                                        "scan": deepcopy(scan_audit),
                                        "validation_message": "completed holdout validation",
                                    }
                                    trace_event(stage="archive", candidate_kind="needle",
                                                candidate_id=proposal["needle_id"],
                                                max_normalized_error=float(polished_eval["score"]["max_error"]),
                                                weighted_rms_error=float(polished_eval["score"]["weighted_rms_error"]),
                                                accepted=True, verified=True,
                                                message="exact lexicographic training improvement passed holdout validation")
                                    notify("running", "archived a validated needle structure")
                                    checkpoint(state="completed", message="validated needle best archived")
            
            if _cancelled(cancel_event):
                termination = "cancelled"
                final_message = "cancelled; retained the best fully scored history candidate"
            elif counters["evaluation_budget_exhausted"]:
                termination = "evaluation_budget_exhausted"
                final_message = "evaluation budget exhausted; retained the best validated candidate"
            elif counters["candidate_budget_exhausted"]:
                termination = "candidate_budget_exhausted"
                final_message = "candidate proposal budget exhausted; retained the best validated candidate"
    except _StructureCancelled:
        termination = "cancelled"
        final_message = "cancelled; retained the best fully scored history candidate"
    except (ValueError, ArithmeticError, FloatingPointError, RuntimeError, KeyError) as exc:
        if best is None:
            raise
        termination = "failed_after_best"
        final_message = f"structure exploration failed; retained history best ({type(exc).__name__})"
        trace_event(stage="failure", candidate_kind="best_candidate", message=final_message)

    assert best is not None
    result_state = ("cancelled" if termination.startswith("cancelled") else
                    ("failed" if termination == "failed_after_best" else "completed"))
    notify(result_state, final_message)
    result = _as_structure_result(
        frozen_request, best, counters, trace, state=result_state,
        message=final_message, termination_reason=termination, is_final=True,
    )
    checkpoint(final=True, state=result_state, message=final_message)
    return result


def _condition_error_payload(error: FilmConditionError) -> dict[str, Any]:
    return asdict(error)


def film_structure_result_to_payload(
    request: FilmStructureOptimizationRequest | None,
    result: FilmStructureOptimizationResult,
) -> dict[str, Any]:
    """Serialize a typed result or partial checkpoint as strict JSON data."""
    source_request = result.frozen_request or request
    if source_request is None:
        raise ValueError("a frozen structure request is required to serialize a result")
    request_payload = film_structure_request_to_payload(source_request)
    base = result.base_result
    quality = {
        "max_normalized_error": float(base.max_normalized_error),
        "weighted_max_normalized_error": float(base.weighted_max_normalized_error),
        "weighted_rms_error": float(base.weighted_rms_error),
        "targets_met": bool(base.targets_met),
        "validation_complete": bool(base.validation_complete),
        "validation_max_normalized_error": base.validation_max_normalized_error,
        "validation_weighted_rms_error": base.validation_weighted_rms_error,
        "validation_targets_met": base.validation_targets_met,
        "termination_reason": base.termination_reason,
    }
    payload = {
        "schema_version": 1,
        "kind": "coat_sim.film_structure_optimization_result",
        "request": request_payload,
        "state": str(base.state),
        "message": result.message or base.message,
        "candidate": {
            "design_revision": int(base.design_revision),
            "design": deepcopy(dict(base.best_design)),
            "thickness_nm": {str(key): float(value) for key, value in base.best_thickness_nm.items()},
            "quality": quality,
            "per_condition_errors": [_condition_error_payload(item) for item in base.per_condition_errors],
            "validation_per_condition_errors": [
                _condition_error_payload(item) for item in base.validation_per_condition_errors],
        },
        "structure": {
            "mode": result.mode,
            "added_needle_layer_ids": list(result.added_needle_layer_ids),
            "topology_audit": deepcopy(dict(result.topology_audit)),
            "trace": [asdict(item) for item in result.trace],
            "counters": asdict(result.counters),
        },
        "partial_result": {
            "kind": "best_candidate",
            "best_so_far": not result.is_final,
            "candidate_verified": bool(result.candidate_verified),
            "validation_complete": bool(base.validation_complete),
            "is_final": bool(result.is_final),
        },
    }
    try:
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("structure result is not valid JSON data") from exc
    return payload


def film_structure_checkpoint_to_payload(
    request: FilmStructureOptimizationRequest | None,
    result: FilmStructureOptimizationResult,
) -> dict[str, Any]:
    """Serialize one durable best-so-far checkpoint."""
    if result.is_final:
        checkpoint_result = replace(result, is_final=False)
    else:
        checkpoint_result = result
    return film_structure_result_to_payload(request, checkpoint_result)


def film_structure_result_from_payload(payload: Mapping[str, Any]) -> FilmStructureOptimizationResult:
    from .film_structure_payload_contract import validate_structure_result_payload_schema
    validate_structure_result_payload_schema(payload)
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1 or payload.get("kind") != "coat_sim.film_structure_optimization_result":
        raise ValueError("structure result payload kind/version is invalid")
    request = film_structure_request_from_payload(payload.get("request"))
    candidate = payload.get("candidate")
    structure = payload.get("structure")
    partial = payload.get("partial_result")
    if not isinstance(candidate, Mapping) or not isinstance(structure, Mapping) or not isinstance(partial, Mapping):
        raise ValueError("structure result payload is malformed")
    quality = candidate.get("quality")
    if not isinstance(quality, Mapping):
        raise ValueError("candidate quality is malformed")
    errors = tuple(FilmConditionError(**row) for row in candidate.get("per_condition_errors", []))
    validation_errors = tuple(FilmConditionError(**row) for row in candidate.get("validation_per_condition_errors", []))
    base = FilmOptimizationResult(
        state=payload.get("state", "completed"),
        design_revision=int(candidate.get("design_revision", request.base_request.design_revision)),
        best_design=deepcopy(dict(candidate["design"])),
        best_thickness_nm={str(key): float(value) for key, value in candidate.get("thickness_nm", {}).items()},
        max_normalized_error=float(quality["max_normalized_error"]),
        weighted_max_normalized_error=float(quality["weighted_max_normalized_error"]),
        weighted_rms_error=float(quality["weighted_rms_error"]),
        targets_met=bool(quality["targets_met"]),
        per_condition_errors=errors,
        iterations=int(structure.get("counters", {}).get("local_iterations", 0)),
        evaluations=int(structure.get("counters", {}).get("objective_evaluations", 0)),
        message=str(payload.get("message", "")),
        solver="baseline", seed=request.seed,
        termination_reason=str(quality.get("termination_reason", "")),
        failed_evaluations=int(structure.get("counters", {}).get("failed_evaluations", 0)),
        validation_samples_multiplier=request.base_request.validation_samples_multiplier,
        validation_evaluations=int(structure.get("counters", {}).get("validation_evaluations", 0)),
        validation_complete=bool(quality.get("validation_complete", False)),
        validation_max_normalized_error=(
            None if quality.get("validation_max_normalized_error") is None
            else float(quality["validation_max_normalized_error"])),
        validation_weighted_rms_error=(
            None if quality.get("validation_weighted_rms_error") is None
            else float(quality["validation_weighted_rms_error"])),
        validation_targets_met=quality.get("validation_targets_met"),
        validation_per_condition_errors=validation_errors,
        validation_message=("completed holdout validation" if quality.get("validation_complete")
                            else "validation not completed"),
    )
    counters_data = structure.get("counters", {})
    counters = FilmStructureCounters(**{
        key: counters_data[key] for key in FilmStructureCounters.__dataclass_fields__
        if key in counters_data
    })
    trace = tuple(FilmStructureTraceEvent(**row) for row in structure.get("trace", []))
    return FilmStructureOptimizationResult(
        base_result=base,
        mode=structure.get("mode", request.mode),
        trace=trace,
        counters=counters,
        topology_audit=deepcopy(dict(structure.get("topology_audit", {}))),
        added_needle_layer_ids=tuple(structure.get("added_needle_layer_ids", [])),
        message=str(payload.get("message", "")),
        frozen_request=request,
        is_final=bool(partial.get("is_final", not partial.get("best_so_far", False))),
        candidate_verified=bool(partial.get("candidate_verified", False)),
    )


def validate_film_structure_candidate_topology(
    request: FilmStructureOptimizationRequest,
    design: Mapping[str, Any],
    *,
    split_groups: Mapping[str, Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """Recompute frozen topology and hard-bound truth without optical replay."""
    if not isinstance(request, FilmStructureOptimizationRequest):
        raise ValueError("request must be a FilmStructureOptimizationRequest")
    frozen = request.base_request.frozen_material_snapshot
    if frozen is None:
        raise ValueError("topology validation requires a frozen material snapshot")
    frozen = _checked_frozen_snapshot(frozen, request.base_request.design_snapshot)
    normalized = validate_design(deepcopy(dict(design)))
    okay, reason = _feasible_design(request, normalized, frozen)
    if not okay:
        raise ValueError(f"candidate topology violates a hard limit: {reason}")
    base = validate_design(deepcopy(dict(request.base_request.design_snapshot)))
    for key in set(base) | set(normalized):
        if key == "front_layers":
            continue
        if key == "expression" and isinstance(base.get(key), Mapping) and isinstance(normalized.get(key), Mapping):
            before, after = dict(base[key]), dict(normalized[key])
            permitted_detach = (before.get("text") and before.get("in_sync") is True and
                                after.get("in_sync") is False)
            before.pop("in_sync", None)
            after.pop("in_sync", None)
            if before == after and (base[key] == normalized[key] or permitted_detach):
                continue
        if base.get(key) != normalized.get(key):
            raise ValueError(f"non-layer design field {key!r} changed")
    base_rows = list(base["front_layers"])
    rows = list(normalized["front_layers"])
    base_ids = [row["id"] for row in base_rows]
    base_set = set(base_ids)
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("candidate layer IDs are not unique")
    projected = [identifier for identifier in ids if identifier in base_set]
    if projected != base_ids:
        raise ValueError("candidate does not preserve the stable base-layer projection/order")
    rows_by_id = {row["id"]: row for row in rows}
    base_by_id = {row["id"]: row for row in base_rows}
    thicknesses = _physical_layers(normalized, frozen)
    base_thicknesses = _physical_layers(base, frozen)
    selected_ids = {variable.layer_id for variable in request.base_request.variables}
    supplied_splits = split_groups or {}
    split_hosts = set(supplied_splits) if isinstance(supplied_splits, Mapping) else set()
    for identifier, original in base_by_id.items():
        current = rows_by_id[identifier]
        if {key: value for key, value in current.items() if key != "thickness"} != {
                key: value for key, value in original.items() if key != "thickness"}:
            raise ValueError(f"base layer {identifier!r} metadata/material changed")
        if identifier not in selected_ids and identifier not in split_hosts and not math.isclose(
                thicknesses[identifier], base_thicknesses[identifier], rel_tol=0, abs_tol=1e-10):
            raise ValueError(f"unselected base layer {identifier!r} was changed")
    if not isinstance(supplied_splits, Mapping):
        raise ValueError("split_groups must be a mapping")
    recomputed_splits: dict[str, Any] = {}
    split_ids: set[str] = set()
    positions = {identifier: index for index, identifier in enumerate(ids)}
    for host_id, group in supplied_splits.items():
        if host_id not in base_set or not isinstance(group, Mapping):
            raise ValueError("split group must identify one original host layer")
        layer_ids = tuple(group.get("layer_ids", ()))
        fractions = tuple(_finite(value, "split fraction") for value in group.get("fractions", ()))
        if len(layer_ids) != 2 or layer_ids[0] != host_id or layer_ids[1] in base_set:
            raise ValueError("split group must preserve the old host ID and add one right segment")
        if len(fractions) != 2 or any(value <= 0 for value in fractions) or not math.isclose(
                sum(fractions), 1.0, rel_tol=0, abs_tol=1e-10):
            raise ValueError("split fractions must be positive and conserve the host")
        if layer_ids[0] not in positions or layer_ids[1] not in positions:
            raise ValueError("split segment is absent")
        left_pos, right_pos = positions[layer_ids[0]], positions[layer_ids[1]]
        if right_pos != left_pos + 2:
            raise ValueError("split segments must surround exactly one needle row")
        left, needle, right = rows[left_pos], rows[left_pos + 1], rows[right_pos]
        original = base_by_id[host_id]
        for side in (left, right):
            if (side["material_id"], side["optical_set_id"]) != (
                    original["material_id"], original["optical_set_id"]):
                raise ValueError("split host material/optical identity changed")
            ignored = {"thickness"} | ({"id"} if side is right else set())
            if {key: value for key, value in side.items() if key not in ignored} != {
                    key: value for key, value in original.items() if key not in ignored}:
                raise ValueError("split host metadata changed")
        total = thicknesses[layer_ids[0]] + thicknesses[layer_ids[1]]
        if host_id not in selected_ids and not math.isclose(
                total, base_thicknesses[host_id], rel_tol=0, abs_tol=1e-10):
            raise ValueError("unselected split host total thickness changed")
        if total <= 0 or not math.isclose(
                thicknesses[layer_ids[0]] / total, fractions[0], rel_tol=0, abs_tol=1e-8):
            raise ValueError("split fractions do not match candidate physical thicknesses")
        if total < 2 * request.min_thickness_nm - 1e-10 or total > 2 * request.max_thickness_nm + 1e-10:
            raise ValueError("split host cannot satisfy the global minimum/maximum row thickness")
        recomputed_splits[str(host_id)] = {
            "layer_ids": [layer_ids[0], layer_ids[1]], "fractions": list(fractions),
            "total_nm": float(total), "needle_layer_id": needle["id"],
        }
        split_ids.add(layer_ids[1])
    extras = [identifier for identifier in ids if identifier not in base_set]
    if not split_ids <= set(extras):
        raise ValueError("split-group references a base or absent segment")
    needle_ids = [identifier for identifier in extras if identifier not in split_ids]
    if len(needle_ids) > request.max_added_layers:
        raise ValueError("candidate exceeds max_added_layers")
    references = {(ref.material_id, ref.optical_set_id) for ref in request.candidate_materials}
    for identifier in needle_ids:
        row = rows_by_id[identifier]
        if (row["material_id"], row["optical_set_id"]) not in references:
            raise ValueError("new candidate row is not an explicitly frozen needle material")
        if not request.min_thickness_nm <= thicknesses[identifier] <= request.max_thickness_nm:
            raise ValueError("needle row thickness is outside the structure-wide bounds")
    if set(split_ids) | set(needle_ids) != set(extras):
        raise ValueError("candidate contains an unclassified new row")
    for host_id, group in recomputed_splits.items():
        if group["needle_layer_id"] not in needle_ids:
            raise ValueError("split host is not separated by a candidate needle")
    active = tuple(needle_ids)
    bounds: dict[str, Any] = {}
    groups = _variable_groups(request, normalized, frozen, recomputed_splits, active)
    for group in groups:
        value = sum(thicknesses[identifier] for identifier in group.layer_ids)
        satisfied = group.lower_nm - 1e-8 <= value <= group.upper_nm + 1e-8
        bounds[group.key] = {
            "layer_ids": list(group.layer_ids), "fractions": list(group.fractions),
            "value_nm": float(value), "lower_nm": float(group.lower_nm),
            "upper_nm": float(group.upper_nm), "satisfied": bool(satisfied),
        }
        if not satisfied:
            raise ValueError(f"selected thickness group {group.key!r} is outside frozen bounds")
    return {
        "base_layer_ids": base_ids,
        "best_layer_ids": ids,
        "preserved_base_layer_ids_in_order": projected,
        "base_order_preserved": True,
        "added_needle_layer_ids": needle_ids,
        "added_split_host_layer_ids": sorted(split_ids),
        "split_groups": recomputed_splits,
        "layer_thickness_nm": {key: float(value) for key, value in thicknesses.items()},
        "total_thickness_nm": float(sum(thicknesses.values())),
        "selected_bound_groups": bounds,
        "selected_bounds_satisfied": True,
        "limits_checked": {
            "max_total_thickness_nm": float(request.max_total_thickness_nm),
            "min_thickness_nm": float(request.min_thickness_nm),
            "max_thickness_nm": float(request.max_thickness_nm),
            "max_total_layers": int(request.max_total_layers),
            "all_hard_limits_satisfied": True,
        },
    }


def validate_film_structure_payload(service: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Replay-check a worker result against its frozen optics and hard limits."""
    result = film_structure_result_from_payload(payload)
    request = result.frozen_request
    if request is None or request.base_request.frozen_material_snapshot is None:
        raise ValueError("structure result must carry frozen material snapshots")
    design = validate_design(deepcopy(dict(result.base_result.best_design)))
    frozen = _checked_frozen_snapshot(request.base_request.frozen_material_snapshot,
                                      request.base_request.design_snapshot)
    _validate_material_domains(request, request.base_request.design_snapshot, frozen)
    audit = validate_film_structure_candidate_topology(
        request, design, split_groups=result.topology_audit.get("split_groups", {}))
    base_ids = {row["id"] for row in request.base_request.design_snapshot["front_layers"]}
    added_ids = [row["id"] for row in design["front_layers"] if row["id"] not in base_ids]
    needle_ids = list(audit["added_needle_layer_ids"])
    if any(_physical_layer_thickness(row, design, frozen) <= 0 for row in design["front_layers"]):
        raise ValueError("persistent structure contains a zero or negative-thickness layer")
    split_groups = audit["split_groups"]
    if tuple(result.added_needle_layer_ids) != tuple(audit["added_needle_layer_ids"]):
        raise ValueError("added needle ID summary does not match recomputed topology")
    for host_id, group in split_groups.items():
        if not isinstance(group, Mapping):
            raise ValueError("topology split-group entry is malformed")
        layer_ids = tuple(group.get("layer_ids", ()))
        fractions = tuple(float(value) for value in group.get("fractions", ()))
        if len(layer_ids) != 2 or len(fractions) != 2 or not math.isclose(sum(fractions), 1.0, abs_tol=1e-10):
            raise ValueError("split host must record exactly two conserving fractions")
        positions = {row["id"]: index for index, row in enumerate(design["front_layers"])}
        if any(identifier not in positions for identifier in layer_ids) or positions[layer_ids[1]] != positions[layer_ids[0]] + 2:
            raise ValueError("split host rows are not ordered around one inserted needle")
        left, needle, right = [design["front_layers"][positions[layer_ids[0]] + offset]
                               for offset in (0, 1, 2)]
        if left["material_id"] != right["material_id"] or left["optical_set_id"] != right["optical_set_id"]:
            raise ValueError("split host material identity was not conserved")
        left_nm = _physical_layer_thickness(left, design, frozen)
        right_nm = _physical_layer_thickness(right, design, frozen)
        total = left_nm + right_nm
        if total <= 0 or not math.isclose(left_nm / total, fractions[0], rel_tol=0, abs_tol=1e-8):
            raise ValueError("split-host total/fraction audit does not match candidate thicknesses")
        if (needle["material_id"], needle["optical_set_id"]) not in {
                (ref.material_id, ref.optical_set_id) for ref in request.candidate_materials}:
            raise ValueError("split-host needle does not use an explicitly frozen candidate material")

    train = evaluate_frozen_structure_candidate(request, design, with_jacobian=False)
    expected = result.base_result
    for field, actual in (
        ("max_normalized_error", train["score"]["max_error"]),
        ("weighted_max_normalized_error", train["score"]["weighted_max_error"]),
        ("weighted_rms_error", train["score"]["weighted_rms_error"]),
    ):
        if not math.isclose(float(getattr(expected, field)), float(actual), rel_tol=1e-9, abs_tol=1e-10):
            raise ValueError(f"candidate {field} does not match frozen replay")
    if bool(expected.targets_met) != bool(train["score"]["max_error"] <= request.base_request.acceptable_normalized_error):
        raise ValueError("candidate targets_met flag does not match replay")
    physical = _physical_layers(design, frozen)
    if set(expected.best_thickness_nm) != set(physical) or any(
            not math.isclose(float(expected.best_thickness_nm[key]), float(value), rel_tol=1e-10, abs_tol=1e-10)
            for key, value in physical.items()):
        raise ValueError("candidate thickness map does not match replayed design")
    def errors_match(actual_errors: Sequence[FilmConditionError], expected_errors: Sequence[FilmConditionError]) -> bool:
        if len(actual_errors) != len(expected_errors):
            return False
        for actual_error, expected_error in zip(actual_errors, expected_errors):
            left, right = asdict(actual_error), asdict(expected_error)
            if left.keys() != right.keys():
                return False
            for key in left:
                if isinstance(left[key], bool) or isinstance(left[key], str) or left[key] is None:
                    if left[key] != right[key]:
                        return False
                elif not math.isclose(float(left[key]), float(right[key]), rel_tol=1e-9, abs_tol=1e-10):
                    return False
        return True
    if not errors_match(expected.per_condition_errors, train["score"]["errors"]):
        raise ValueError("per-condition training errors do not match frozen replay")
    if expected.validation_complete:
        validation = evaluate_frozen_structure_candidate(
            request, design, with_jacobian=False, derived_validation_grid=True)
        if expected.validation_max_normalized_error is None or not math.isclose(
                float(expected.validation_max_normalized_error),
                float(validation["score"]["max_error"]), rel_tol=1e-9, abs_tol=1e-10):
            raise ValueError("validation maximum error does not match frozen replay")
        if expected.validation_weighted_rms_error is None or not math.isclose(
                float(expected.validation_weighted_rms_error),
                float(validation["score"]["weighted_rms_error"]), rel_tol=1e-9, abs_tol=1e-10):
            raise ValueError("validation RMS does not match frozen replay")
        validation_met = validation["score"]["max_error"] <= request.base_request.acceptable_normalized_error
        if expected.validation_targets_met is not validation_met:
            raise ValueError("validation target flag does not match frozen replay")
        if not errors_match(expected.validation_per_condition_errors, validation["score"]["errors"]):
            raise ValueError("per-condition validation errors do not match frozen replay")
    elif (expected.validation_max_normalized_error is not None or
          expected.validation_weighted_rms_error is not None or
          expected.validation_targets_met is not None or expected.validation_per_condition_errors):
        raise ValueError("incomplete holdout must not carry validation metrics")
    if payload.get("state") != expected.state:
        raise ValueError("top-level state does not match candidate result state")
    partial = payload.get("partial_result", {})
    if not isinstance(partial, Mapping) or bool(partial.get("validation_complete")) != bool(expected.validation_complete):
        raise ValueError("partial validation state does not match replayed result")
    if bool(partial.get("is_final")) != bool(result.is_final):
        raise ValueError("partial final-state flag does not match result")
    if result.is_final and expected.state not in ("completed", "cancelled", "failed"):
        raise ValueError("a final result must have a terminal state")
    canonical = film_structure_result_to_payload(
        request, replace(result, topology_audit=audit, candidate_verified=True))
    canonical["partial_result"]["candidate_verified"] = True
    return canonical


def validate_film_structure_result(
    service: Any,
    request: FilmStructureOptimizationRequest,
    result: FilmStructureOptimizationResult,
) -> FilmStructureOptimizationResult:
    """Replay-validate a typed result and return its canonical verified form."""
    payload = film_structure_result_to_payload(request, result)
    canonical = validate_film_structure_payload(service, payload)
    return film_structure_result_from_payload(canonical)


