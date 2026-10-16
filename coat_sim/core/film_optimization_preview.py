"""Versioned, immutable handoff from an optimization result to a new preview."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping
import uuid

from .film_design import validate_design
from .film_design_service import active_material_references, simulation_settings
from .film_optimization import (
    FilmOptimizationRequest,
    FilmOptimizationResult,
    _checked_frozen_snapshot,
)
from .film_optimization_structure import (
    FilmStructureOptimizationRequest, FilmStructureOptimizationResult,
)


PREVIEW_KIND = "coat_sim.film_optimization_preview"
PREVIEW_VERSION = 1


def _reference_keys(design: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {(str(row["material_id"]), str(row["optical_set_id"]))
            for row in active_material_references(design)}


def create_optimization_preview_handoff(
    request: FilmOptimizationRequest | FilmStructureOptimizationRequest,
    result: FilmOptimizationResult | FilmStructureOptimizationResult,
) -> dict[str, Any]:
    """Create an automatic preview payload from a completed candidate."""
    return _create_optimization_handoff(request, result, preview_state="completed")


def create_optimization_candidate_handoff(
    request: FilmOptimizationRequest | FilmStructureOptimizationRequest,
    result: FilmOptimizationResult | FilmStructureOptimizationResult,
) -> dict[str, Any]:
    """Create a user-requested preview payload from a verified queue candidate."""
    return _create_optimization_handoff(request, result, preview_state="best_candidate")


def _create_optimization_handoff(
    request: FilmOptimizationRequest,
    result: FilmOptimizationResult,
    *,
    preview_state: str,
) -> dict[str, Any]:
    structure_request = None
    structure_result = None
    if not isinstance(request, FilmOptimizationRequest):
        from .film_optimization_structure import (
            FilmStructureOptimizationRequest, FilmStructureOptimizationResult,
            film_structure_request_to_payload,
            validate_film_structure_candidate_topology,
        )
        if (not isinstance(request, FilmStructureOptimizationRequest)
                or not isinstance(result, FilmStructureOptimizationResult)):
            raise ValueError("structure previews require matching structure request/result types")
        structure_request, structure_result = request, result
        if result.candidate_verified is not True:
            raise ValueError("structure previews require a completely scored, verified candidate")
        if result.mode != request.mode:
            raise ValueError("structure result mode does not match its request")
        if (result.frozen_request is not None and
                film_structure_request_to_payload(result.frozen_request) !=
                film_structure_request_to_payload(request)):
            raise ValueError("structure result frozen request does not match the preview source")
        validate_film_structure_candidate_topology(
            request, result.base_result.best_design,
            split_groups=result.topology_audit.get("split_groups", {}),
        )
        if preview_state == "completed":
            if result.is_final is not True:
                raise ValueError("automatic structure previews require a final result")
            if result.base_result.validation_complete is not True:
                raise ValueError("automatic structure previews require completed holdout validation")
        request, result = request.base_request, result.base_result
    if not isinstance(request, FilmOptimizationRequest):
        raise ValueError("request must be a FilmOptimizationRequest")
    if not isinstance(result, FilmOptimizationResult):
        raise ValueError("result must be a FilmOptimizationResult")
    if preview_state == "completed" and result.state != "completed":
        raise ValueError("automatic previews require a completed optimization")
    if preview_state == "best_candidate" and result.state not in (
            "completed", "cancelled", "failed", "best_candidate"):
        raise ValueError("queue preview requires a verified best candidate")
    if preview_state not in ("completed", "best_candidate"):
        raise ValueError("optimization preview state is invalid")
    if result.design_revision != request.design_revision:
        raise ValueError("optimization result revision does not match its request")
    if request.frozen_material_snapshot is None or not request.objectives:
        raise ValueError("preview handoff requires frozen inputs and an objective")

    original = validate_design(deepcopy(dict(request.design_snapshot)))
    original_snapshot = _checked_frozen_snapshot(
        request.frozen_material_snapshot, original)
    candidate = validate_design(deepcopy(dict(result.best_design)))
    if structure_request is None:
        if _reference_keys(candidate) != _reference_keys(original):
            raise ValueError("optimized candidate changed a frozen material reference")
    else:
        explicit = {(ref.material_id, ref.optical_set_id)
                    for ref in structure_request.candidate_materials}
        allowed = _reference_keys(original) | explicit
        if not _reference_keys(candidate).issubset(allowed):
            raise ValueError("structure preview contains a material that was not explicitly selected")
        frozen_keys = {(row["id"], optical["id"])
                       for row in original_snapshot["materials"]
                       for optical in row.get("optical_sets", [])}
        if not _reference_keys(candidate).issubset(frozen_keys):
            raise ValueError("structure preview material is missing from the frozen snapshot")

    candidate_snapshot = deepcopy(original_snapshot)
    candidate_snapshot["design"] = deepcopy(candidate)
    candidate_identity = {
        key: value for key, value in candidate_snapshot.items() if key != "sha256"
    }
    candidate_snapshot["sha256"] = hashlib.sha256(json.dumps(
        candidate_identity, sort_keys=True, ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()
    candidate_snapshot = _checked_frozen_snapshot(candidate_snapshot, candidate)

    objective = request.objectives[0]
    settings = simulation_settings({
        "wavelength_min_nm": objective.wavelength_min_nm,
        "wavelength_max_nm": objective.wavelength_max_nm,
        "samples": objective.wavelength_samples,
        "angle_deg": objective.angles_deg[0],
        "polarization": objective.polarization,
        "first_surface": candidate.get("first_surface", "front"),
    })
    payload = {
        "schema_version": PREVIEW_VERSION,
        "kind": PREVIEW_KIND,
        "handoff_id": uuid.uuid4().hex,
        "design": candidate,
        "frozen_material_snapshot": candidate_snapshot,
        "settings": settings,
        "plot_settings": {"curves": [objective.metric]},
        "optimization_summary": {
            "state": preview_state,
            "targets_met": bool(result.targets_met),
            "objective_count": len(request.objectives),
            "max_normalized_error": float(result.max_normalized_error),
            "message": str(result.message),
            "source_design_revision": int(result.design_revision),
        },
    }
    if structure_request is not None:
        payload["optimization_summary"].update(
            structure_mode=structure_request.mode,
            validation_complete=bool(structure_result.base_result.validation_complete),
            structure_state=str(structure_result.base_result.state),
        )
    json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return payload


def validate_optimization_preview_handoff(value: Mapping[str, Any]) -> dict[str, Any]:
    if (not isinstance(value, Mapping)
            or value.get("schema_version") != PREVIEW_VERSION
            or value.get("kind") != PREVIEW_KIND):
        raise ValueError("optimization preview handoff kind/version is invalid")
    required = {
        "schema_version", "kind", "handoff_id", "design",
        "frozen_material_snapshot", "settings", "plot_settings",
        "optimization_summary",
    }
    if set(value) != required:
        raise ValueError("optimization preview handoff fields are invalid")
    handoff_id = value["handoff_id"]
    if (not isinstance(handoff_id, str) or len(handoff_id) != 32
            or any(char not in "0123456789abcdef" for char in handoff_id)):
        raise ValueError("optimization preview handoff id is invalid")
    design = validate_design(deepcopy(dict(value["design"])))
    frozen = _checked_frozen_snapshot(value["frozen_material_snapshot"], design)
    settings = simulation_settings(deepcopy(dict(value["settings"])))
    plot_settings = deepcopy(dict(value["plot_settings"]))
    summary = deepcopy(dict(value["optimization_summary"]))
    if (summary.get("state") not in ("completed", "best_candidate")
            or type(summary.get("targets_met")) is not bool):
        raise ValueError("optimization preview summary is invalid")
    if "structure_mode" in summary:
        if (summary["structure_mode"] not in ("needle_only", "growth_only", "combined")
                or type(summary.get("validation_complete")) is not bool
                or summary.get("structure_state") not in ("completed", "cancelled", "failed")):
            raise ValueError("structure preview summary is invalid")
    return {
        **deepcopy(dict(value)),
        "design": design,
        "frozen_material_snapshot": frozen,
        "settings": settings,
        "plot_settings": plot_settings,
        "optimization_summary": summary,
    }
