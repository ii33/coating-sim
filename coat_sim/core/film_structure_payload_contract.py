"""Strict JSON shape/type checks for the independent structure queue contract.

Optical replay and topology truth remain in film_optimization_structure. These
checks run before DTO coercion, so booleans, missing fields, and unknown options
cannot silently become a different supported request or claimed result.
"""
from __future__ import annotations

import json
import math
from typing import Any, Mapping

from .film_design import MAX_LAYERS


_REQUEST_FIELDS = {"schema_version", "kind", "mode", "base_request", "candidate_materials", "limits"}
_BASE_FIELDS = {
    "design_snapshot", "design_revision", "variables", "objectives", "max_iterations",
    "max_evaluations", "initial_step_fraction", "min_step_fraction",
    "acceptable_normalized_error", "frozen_material_snapshot", "validation_samples_multiplier",
}
_LIMIT_FIELDS = {
    "max_total_thickness_nm", "min_thickness_nm", "max_thickness_nm", "max_total_layers",
    "max_added_layers", "position_samples", "candidate_budget", "needle_thickness_samples",
    "growth_steps_nm", "growth_phase_fractions", "growth_cycles", "seed",
}
_OBJECTIVE_FIELDS = {
    "wavelength_min_nm", "wavelength_max_nm", "wavelength_samples", "angles_deg",
    "polarization", "metric", "mode", "target_value", "lower_bound", "upper_bound",
    "weight", "normalization_scale",
}
_COUNTER_FIELDS = {
    "objective_evaluations", "score_evaluations", "jacobian_evaluations", "validation_evaluations",
    "candidate_count", "needle_candidate_count", "needle_probe_count", "growth_proposal_count",
    "local_iterations", "failed_evaluations", "candidate_budget_exhausted", "evaluation_budget_exhausted",
}
_COUNTER_FLAGS = {"candidate_budget_exhausted", "evaluation_budget_exhausted"}
_ERROR_FIELDS = {
    "objective_index", "wavelength_nm", "angle_deg", "polarization", "metric", "actual",
    "target_value", "lower_bound", "upper_bound", "normalized_violation", "weighted_normalized_violation",
}
_TRACE_FIELDS = {
    "sequence", "stage", "candidate_kind", "candidate_id", "host_layer_id", "material_id",
    "optical_set_id", "position_nm", "position_fraction", "thickness_nm", "max_normalized_error",
    "weighted_rms_error", "accepted", "selected", "verified", "parent_candidate_id", "message",
}
_TRACE_NUMBERS = {
    "position_nm", "position_fraction", "thickness_nm", "max_normalized_error", "weighted_rms_error",
}
_TRACE_FLAGS = {"accepted", "selected", "verified"}
_QUALITY_FIELDS = {
    "max_normalized_error", "weighted_max_normalized_error", "weighted_rms_error", "targets_met",
    "validation_complete", "validation_max_normalized_error", "validation_weighted_rms_error",
    "validation_targets_met", "termination_reason",
}
_MODES = {"needle_only", "growth_only", "combined"}


def _mapping(value: Any, label: str, fields: set[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError(f"{label} must be an object with text keys")
    if "solver" in value or "solver_id" in value:
        raise ValueError(f"{label} must not include solver or solver_id")
    if fields is not None and set(value) != fields:
        raise ValueError(f"{label} fields are invalid (missing or unknown fields)")
    return value


def _integer(value: Any, label: str, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{label} must be an integer in its supported range")
    return value


def _number(value: Any, label: str, *, nullable: bool = False, nonnegative: bool = False) -> float | None:
    if value is None and nullable:
        return None
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite:
        raise ValueError(f"{label} must be a finite number")
    if nonnegative and value < 0:
        raise ValueError(f"{label} must be nonnegative")
    return float(value)


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _text(value: Any, label: str, *, nonempty: bool = False) -> str:
    if type(value) is not str or (nonempty and not value):
        raise ValueError(f"{label} must be text")
    return value


def _sequence(value: Any, label: str) -> list | tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    return value


def _json(value: Any, label: str) -> None:
    try:
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must contain finite JSON data") from exc


def validate_structure_request_payload_schema(payload: Mapping[str, Any]) -> None:
    envelope = _mapping(payload, "structure request", _REQUEST_FIELDS)
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise ValueError("structure request schema_version must be integer 1")
    if envelope["kind"] != "coat_sim.film_structure_optimization_request":
        raise ValueError("structure request kind is invalid")
    if type(envelope["mode"]) is not str or envelope["mode"] not in _MODES:
        raise ValueError("structure request mode is invalid")
    base = _mapping(envelope["base_request"], "structure base_request", _BASE_FIELDS)
    _mapping(base["design_snapshot"], "design_snapshot")
    if base["frozen_material_snapshot"] is not None:
        _mapping(base["frozen_material_snapshot"], "frozen_material_snapshot")
    _integer(base["design_revision"], "design_revision")
    _integer(base["max_iterations"], "max_iterations")
    _integer(base["max_evaluations"], "max_evaluations", 1)
    _integer(base["validation_samples_multiplier"], "validation_samples_multiplier", 1)
    for name in ("initial_step_fraction", "min_step_fraction", "acceptable_normalized_error"):
        _number(base[name], name, nonnegative=True)
    variables = _sequence(base["variables"], "variables")
    for variable in variables:
        row = _mapping(variable, "variable", {"side", "layer_id", "lower_nm", "upper_nm"})
        if row["side"] != "front":
            raise ValueError("structure variables must select front layers")
        _text(row["layer_id"], "layer_id", nonempty=True)
        _number(row["lower_nm"], "lower_nm", nonnegative=True)
        _number(row["upper_nm"], "upper_nm", nonnegative=True)
    objectives = _sequence(base["objectives"], "objectives")
    if not objectives:
        raise ValueError("at least one structure objective is required")
    for objective in objectives:
        row = _mapping(objective, "objective", _OBJECTIVE_FIELDS)
        _integer(row["wavelength_samples"], "wavelength_samples", 1)
        angles = _sequence(row["angles_deg"], "angles_deg")
        if not angles:
            raise ValueError("angles_deg cannot be empty")
        for value in angles:
            _number(value, "angle_deg")
        for name in ("wavelength_min_nm", "wavelength_max_nm", "weight", "normalization_scale"):
            _number(row[name], name)
        for name in ("target_value", "lower_bound", "upper_bound"):
            _number(row[name], name, nullable=True)
        if row["metric"] not in ("R", "T") or row["polarization"] not in ("s", "p", "unpolarized"):
            raise ValueError("objective metric/polarization is unsupported")
        if row["mode"] not in ("value", "range"):
            raise ValueError("objective mode is unsupported")
    references = _sequence(envelope["candidate_materials"], "candidate_materials")
    if len(references) > 64:
        raise ValueError("too many candidate materials")
    for reference in references:
        row = _mapping(reference, "candidate material", {"material_id", "optical_set_id"})
        for name in row:
            if len(_text(row[name], name, nonempty=True)) > 128:
                raise ValueError("candidate material IDs exceed 128 characters")
    limits = _mapping(envelope["limits"], "structure limits", _LIMIT_FIELDS)
    for name in ("max_total_thickness_nm", "min_thickness_nm", "max_thickness_nm"):
        _number(limits[name], name, nonnegative=True)
    _integer(limits["max_total_layers"], "max_total_layers", 1, MAX_LAYERS)
    for name in ("position_samples", "candidate_budget", "needle_thickness_samples"):
        _integer(limits[name], name, 1)
    for name in ("max_added_layers", "growth_cycles"):
        _integer(limits[name], name, 1, 1)
    _integer(limits["seed"], "seed", 0, 0xFFFFFFFF)
    for name in ("growth_steps_nm", "growth_phase_fractions"):
        for value in _sequence(limits[name], name):
            _number(value, name, nonnegative=True)
    _json(payload, "structure request")


def _errors(value: Any, label: str, objective_count: int) -> None:
    for error in _sequence(value, label):
        row = _mapping(error, label, _ERROR_FIELDS)
        _integer(row["objective_index"], "objective_index", 0, objective_count - 1)
        for name in ("wavelength_nm", "angle_deg", "actual"):
            _number(row[name], name)
        for name in ("target_value", "lower_bound", "upper_bound"):
            _number(row[name], name, nullable=True)
        for name in ("normalized_violation", "weighted_normalized_violation"):
            _number(row[name], name, nonnegative=True)
        if row["metric"] not in ("R", "T") or row["polarization"] not in ("s", "p", "unpolarized"):
            raise ValueError(f"{label} metric/polarization is invalid")


def validate_structure_result_payload_schema(payload: Mapping[str, Any]) -> None:
    envelope = _mapping(payload, "structure result", {
        "schema_version", "kind", "request", "state", "message", "candidate", "structure", "partial_result",
    })
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise ValueError("structure result schema_version must be integer 1")
    if envelope["kind"] != "coat_sim.film_structure_optimization_result":
        raise ValueError("structure result kind is invalid")
    if type(envelope["state"]) is not str or envelope["state"] not in ("completed", "cancelled", "failed"):
        raise ValueError("structure result state is invalid")
    _text(envelope["message"], "message")
    validate_structure_request_payload_schema(envelope["request"])
    request = envelope["request"]
    base = request["base_request"]
    candidate = _mapping(envelope["candidate"], "candidate", {
        "design_revision", "design", "thickness_nm", "quality", "per_condition_errors", "validation_per_condition_errors",
    })
    _integer(candidate["design_revision"], "candidate design_revision")
    if candidate["design_revision"] != base["design_revision"]:
        raise ValueError("candidate revision does not match the frozen request")
    _mapping(candidate["design"], "candidate design")
    for key, value in _mapping(candidate["thickness_nm"], "candidate thickness_nm").items():
        _text(key, "thickness ID", nonempty=True)
        if _number(value, "candidate thickness", nonnegative=True) <= 0:
            raise ValueError("persistent candidate thickness must be positive")
    quality = _mapping(candidate["quality"], "candidate quality", _QUALITY_FIELDS)
    for name in ("max_normalized_error", "weighted_max_normalized_error", "weighted_rms_error"):
        _number(quality[name], name, nonnegative=True)
    _boolean(quality["targets_met"], "targets_met")
    validation = _boolean(quality["validation_complete"], "validation_complete")
    for name in ("validation_max_normalized_error", "validation_weighted_rms_error"):
        _number(quality[name], name, nullable=not validation, nonnegative=True)
        if not validation and quality[name] is not None:
            raise ValueError("incomplete validation cannot carry scores")
    if validation:
        _boolean(quality["validation_targets_met"], "validation_targets_met")
    elif quality["validation_targets_met"] is not None:
        raise ValueError("incomplete validation cannot claim target status")
    _text(quality["termination_reason"], "termination_reason")
    _errors(candidate["per_condition_errors"], "training errors", len(base["objectives"]))
    _errors(candidate["validation_per_condition_errors"], "validation errors", len(base["objectives"]))
    expected_training = sum(row["wavelength_samples"] * len(row["angles_deg"]) for row in base["objectives"])
    if len(candidate["per_condition_errors"]) != expected_training:
        raise ValueError("training error table is incomplete")
    factor = base["validation_samples_multiplier"]
    expected_holdout = sum((1 if row["wavelength_samples"] == 1 else (row["wavelength_samples"] - 1) * factor + 1)
                           * len(row["angles_deg"]) for row in base["objectives"])
    if len(candidate["validation_per_condition_errors"]) != (expected_holdout if validation else 0):
        raise ValueError("validation error table is incomplete or contradicts its state")
    structure = _mapping(envelope["structure"], "structure", {
        "mode", "added_needle_layer_ids", "topology_audit", "trace", "counters",
    })
    if structure["mode"] != request["mode"]:
        raise ValueError("result mode differs from frozen request")
    identifiers = _sequence(structure["added_needle_layer_ids"], "added needle IDs")
    for identifier in identifiers:
        _text(identifier, "added needle ID", nonempty=True)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("added needle IDs must be unique")
    _mapping(structure["topology_audit"], "topology_audit")
    counters = _mapping(structure["counters"], "structure counters", _COUNTER_FIELDS)
    for name in _COUNTER_FIELDS:
        if name in _COUNTER_FLAGS:
            _boolean(counters[name], name)
        else:
            _integer(counters[name], name)
    if not 1 <= counters["objective_evaluations"] <= base["max_evaluations"]:
        raise ValueError("objective evaluation count exceeds the frozen budget or has no completed work")
    if counters["local_iterations"] > base["max_iterations"]:
        raise ValueError("local iteration count exceeds the frozen budget")
    if counters["candidate_count"] > request["limits"]["candidate_budget"]:
        raise ValueError("candidate count exceeds the frozen budget")
    for name in ("score_evaluations", "validation_evaluations", "failed_evaluations", "needle_probe_count"):
        if counters[name] > counters["objective_evaluations"]:
            raise ValueError(f"{name} exceeds total evaluations")
    if validation and counters["validation_evaluations"] < 1:
        raise ValueError("completed validation has no counted validation work")
    for name in ("needle_candidate_count", "growth_proposal_count"):
        if counters[name] > counters["candidate_count"]:
            raise ValueError(f"{name} exceeds total candidates")
    previous_sequence = -1
    for event in _sequence(structure["trace"], "trace"):
        row = _mapping(event, "trace event", _TRACE_FIELDS)
        sequence = _integer(row["sequence"], "trace sequence", 0)
        if sequence <= previous_sequence:
            raise ValueError("trace sequence must increase")
        previous_sequence = sequence
        for name in _TRACE_FLAGS:
            _boolean(row[name], name)
        for name in _TRACE_NUMBERS:
            _number(row[name], name, nullable=True)
        for name in _TRACE_FIELDS - _TRACE_FLAGS - _TRACE_NUMBERS - {"sequence"}:
            _text(row[name], name)
    partial = _mapping(envelope["partial_result"], "partial result", {
        "kind", "best_so_far", "candidate_verified", "validation_complete", "is_final",
    })
    if partial["kind"] != "best_candidate":
        raise ValueError("partial result kind is invalid")
    for name in ("best_so_far", "candidate_verified", "validation_complete", "is_final"):
        _boolean(partial[name], name)
    if partial["best_so_far"] is not (not partial["is_final"]):
        raise ValueError("partial result final/best-so-far flags contradict")
    if partial["validation_complete"] is not validation:
        raise ValueError("partial validation flag differs from candidate quality")
    _json(payload, "structure result")
