"""Build fresh frozen searches from complete, cancelled candidate geometry.

This is a warm restart, not a solver checkpoint restore. Saved evidence is
checked for shape, arithmetic and its frozen input binding, never optically
replayed here. Every seed is evaluated again, within the new job's budget.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from typing import Any, Mapping

from .film_auto_synthesis import FilmSynthesisRequest, _feasible, freeze_film_synthesis_request
from .film_design import validate_design
from .film_design_service import active_material_references
from .film_nominal_acceptance import complete_original_score
from .film_optimization import (
    FilmOptimizationRequest, FilmOptimizationResult, _validate_optimization_controls,
    freeze_film_optimization_request,
)
from .film_optimization_job import (
    _snapshot_for_candidate, _validate_replay_request, _validation_objectives,
    deserialize_request, serialize_request,
)
from .film_synthesis_archive import physical_design_hash
from .film_synthesis_io import (
    REQUEST_KIND as SYNTHESIS_REQUEST_KIND, SCHEMA_VERSION,
    film_synthesis_request_from_payload, film_synthesis_request_to_payload,
)
from .film_synthesis_jobs import is_synthesis_result


@dataclass(frozen=True)
class WarmRestartPlan:
    """A new search input and small JSON provenance, without saved spectra."""

    request: FilmOptimizationRequest | FilmSynthesisRequest
    metadata: dict[str, Any]


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("warm restart requires finite JSON data") from exc


def canonical_request_digest(request: FilmOptimizationRequest | FilmSynthesisRequest) -> str:
    """Hash canonical current request content, independent of job/request IDs."""
    if type(request) is FilmOptimizationRequest:
        payload = serialize_request(request)
    elif type(request) is FilmSynthesisRequest:
        raw = film_synthesis_request_to_payload("warm-restart-digest", request)["request"]
        payload = {"schema_version": SCHEMA_VERSION, "kind": SYNTHESIS_REQUEST_KIND,
                   "request": raw}
    else:
        raise ValueError("unsupported warm restart request")
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _integer(value: Any, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        domain = "a nonnegative integer" if minimum == 0 else f"an integer >= {minimum}"
        raise ValueError(f"{name} must be {domain}")
    return value


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value) and (value > 0 if positive else value >= 0))
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return float(value)


def _frozen_pairs(snapshot: Mapping[str, Any]) -> set[tuple[str, str]]:
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise ValueError("frozen material snapshot schema is invalid")
    pairs: set[tuple[str, str]] = set()
    for material in snapshot["materials"]:
        if not isinstance(material.get("id"), str) or not material["id"]:
            raise ValueError("frozen material identity is invalid")
        sets = material.get("optical_sets")
        if not isinstance(sets, list) or not sets:
            raise ValueError("frozen material optical sets are invalid")
        for optical in sets:
            if (not isinstance(optical, Mapping) or not isinstance(optical.get("id"), str)
                    or not optical["id"]):
                raise ValueError("frozen material optical sets are invalid")
            pair = material["id"], optical["id"]
            if pair in pairs:
                raise ValueError("frozen material optical sets are duplicated")
            pairs.add(pair)
    return pairs


def _require_materials(design: Mapping[str, Any], pairs: set[tuple[str, str]]) -> None:
    if any((row["material_id"], row["optical_set_id"]) not in pairs
           for row in active_material_references(design)):
        raise ValueError("candidate material is outside the original frozen inputs")


def _metadata(request, candidate, *, source_kind, spent, fresh_budget):
    return {"schema_version": 1, "kind": "coat_sim.film_warm_restart",
            "mode": "warm_restart", "solver_state_preserved": False,
            "source_kind": source_kind,
            "original_design_revision": request.design_revision,
            "original_request_sha256": canonical_request_digest(request),
            "candidate_physical_hash": physical_design_hash(candidate),
            "spent": spent, "fresh_budget": fresh_budget,
            "saved_training_complete": True, "physical_replay_performed": False,
            "independent_audit_performed": False, "saved_scores_reused": False}


def _check_score(request, score, *, targets_met, solver_acceptable) -> None:
    score = {**score, "nominal_pass": targets_met, "solver_acceptable": solver_acceptable}
    if (type(targets_met) is not bool or type(solver_acceptable) is not bool
            or not complete_original_score(request, score)):
        raise ValueError("cancelled candidate lacks complete matching saved score and coverage")


def build_optimization_warm_restart(
    request: FilmOptimizationRequest,
    result: FilmOptimizationResult,
    *,
    design_revision: int,
    max_iterations: int,
    max_evaluations: int,
) -> WarmRestartPlan:
    """Restart fixed-topology thickness search, with exact original criteria."""
    _integer(design_revision, "design_revision", minimum=0)
    _integer(max_iterations, "max_iterations")
    _integer(max_evaluations, "max_evaluations")
    if type(request) is not FilmOptimizationRequest or type(result) is not FilmOptimizationResult:
        raise ValueError("warm restart requires a thickness request and typed result")
    if result.state != "cancelled":
        raise ValueError("only cancelled optimization can be continued")
    if request.frozen_material_snapshot is None:
        raise ValueError("warm restart requires the original frozen request")
    try:
        original = serialize_request(request)
        # These gates validate controls, frozen digest and canonical input only.
        checked = freeze_film_optimization_request(None, deserialize_request(original))
        _validate_optimization_controls(checked)
        _validate_replay_request(checked)
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("original frozen optimization request is invalid") from exc
    if request.solver in {"trf", "lbfgsb"} and (
            request.design_snapshot["substrate"]["sides"] != "single"
            or request.design_snapshot["first_surface"] != "front"
            or any(variable.side != "front" for variable in request.variables)):
        raise ValueError("derivative solver requires single-sided front-layer variables")
    if _json(serialize_request(checked)) != _json(original):
        raise ValueError("original frozen optimization request is not canonical")
    # Validate saved records in place below, rather than cloning complete
    # spectra into an extra result/metadata tree just to construct geometry.
    if any(not isinstance(getattr(result, name), str)
           for name in ("message", "termination_reason", "validation_message")):
        raise ValueError("cancelled result status text is invalid")
    if (type(result.design_revision) is not int or result.design_revision != request.design_revision
            or result.solver != request.solver or type(result.seed) is not int
            or result.seed != request.seed
            or type(result.validation_samples_multiplier) is not int
            or result.validation_samples_multiplier != request.validation_samples_multiplier):
        raise ValueError("cancelled result does not match its frozen request")
    for name in ("iterations", "failed_evaluations", "validation_evaluations"):
        _integer(getattr(result, name), name, minimum=0)
    _integer(result.evaluations, "evaluations")
    if (result.iterations > request.max_iterations or result.evaluations > request.max_evaluations
            or result.validation_evaluations not in (0, 1)
            # Ordinary evaluations counts training only; holdout attempts are
            # separate, while failed_evaluations may include one failed holdout.
            or result.failed_evaluations > result.evaluations - 1 + result.validation_evaluations):
        raise ValueError("cancelled result work counters are invalid")
    candidate = validate_design(deepcopy(result.best_design))
    if candidate != result.best_design or not isinstance(result.best_thickness_nm, Mapping):
        raise ValueError("cancelled candidate design is not canonical")
    if set(result.best_thickness_nm) != {row.layer_id for row in request.variables}:
        raise ValueError("cancelled candidate thickness IDs do not match the request")
    expected = deepcopy(dict(request.design_snapshot))
    for variable in request.variables:
        value = _number(result.best_thickness_nm[variable.layer_id], "candidate thickness")
        low = _number(variable.lower_nm, "original lower thickness bound")
        high = (None if variable.upper_nm is None else
                _number(variable.upper_nm, "original upper thickness bound"))
        if value < low or (high is not None and value > high):
            raise ValueError("cancelled candidate thickness violates the original bounds")
        row = next((row for row in expected[variable.side + "_layers"]
                    if row["id"] == variable.layer_id), None)
        if row is None or (variable.side == "back" and expected["substrate"]["sides"] != "double"):
            raise ValueError("selected layer is missing from the original design")
        row["thickness"] = {"value": value, "unit": "nm"}
    expression = expected["expression"]
    if expression["text"] and expression["in_sync"]:
        expression["in_sync"] = False
    if candidate != expected:
        raise ValueError("cancelled candidate changed the original fixed topology or settings")
    if checked.frozen_material_snapshot is None:
        raise ValueError("warm restart requires the original frozen request")
    pairs = _frozen_pairs(checked.frozen_material_snapshot)
    _require_materials(candidate, pairs)
    _check_score(request, {"errors": result.per_condition_errors, "coverage": result.raw_coverage,
        "max_error": result.max_normalized_error,
        "weighted_max_error": result.weighted_max_normalized_error,
        "weighted_rms_error": result.weighted_rms_error},
        targets_met=result.targets_met, solver_acceptable=result.solver_acceptable)
    if type(result.validation_complete) is not bool:
        raise ValueError("cancelled candidate validation status is invalid")
    if result.validation_complete:
        if result.validation_evaluations != 1:
            raise ValueError("cancelled candidate validation counters are invalid")
        holdout = replace(request, objectives=_validation_objectives(request))
        _check_score(holdout, {"errors": result.validation_per_condition_errors,
            "coverage": result.validation_raw_coverage,
            "max_error": result.validation_max_normalized_error,
            "weighted_rms_error": result.validation_weighted_rms_error},
            targets_met=result.validation_targets_met,
            solver_acceptable=result.validation_solver_acceptable)
    elif (result.validation_max_normalized_error is not None
            or result.validation_weighted_rms_error is not None
            or result.validation_targets_met is not None
            or result.validation_solver_acceptable is not None
            or result.validation_per_condition_errors or result.validation_raw_coverage):
        raise ValueError("incomplete validation must not claim saved scores")
    new_request = replace(deepcopy(request), design_revision=design_revision,
        design_snapshot=deepcopy(candidate), frozen_material_snapshot=_snapshot_for_candidate(request, candidate),
        max_iterations=max_iterations, max_evaluations=max_evaluations)
    # Rebinding changes only the design and digest, never the material rows.
    new_request = freeze_film_optimization_request(None, new_request)
    metadata = _metadata(request, candidate, source_kind="film_optimization",
        spent={name: getattr(result, name) for name in
            ("iterations", "evaluations", "failed_evaluations", "validation_evaluations")},
        fresh_budget={"max_iterations": max_iterations, "max_evaluations": max_evaluations})
    _json(metadata)
    return WarmRestartPlan(new_request, metadata)


_PHYSICAL_ENVIRONMENT_KEYS = ("substrate", "incident_medium", "exit_medium", "first_surface",
    "layer_order", "reference_wavelength_nm", "use_k", "assume_lossless")
_ARCHIVE_FIELDS = {"capacity", "total_admitted", "total_pruned", "retained",
    "audited_champion_hash", "policy", "entries"}
_ARCHIVE_ENTRY_FIELDS = {"physical_hash", "design", "max_error", "weighted_rms_error",
    "window_max_errors", "layer_count", "total_thickness_nm", "parent_hash", "operator",
    "provenance", "pareto", "audit_status", "audit"}


def _synthesis_seed(request, raw, allowed):
    seed = validate_design(deepcopy(raw))
    if (seed != raw or seed["back_layers"]
            or any(seed[key] != request.environment[key] for key in _PHYSICAL_ENVIRONMENT_KEYS)
            or any(row["thickness"]["unit"] != "nm"
                or (row["material_id"], row["optical_set_id"]) not in allowed
                for row in seed["front_layers"])
            or not _feasible(request, seed)):
        raise ValueError("warm restart seed violates the original frozen physical inputs or bounds")
    return seed


def _hash(value):
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _archive_seeds(request, performance, allowed):
    if not isinstance(performance, Mapping):
        raise ValueError("saved synthesis performance is invalid")
    search = performance.get("search", {})
    if not isinstance(search, Mapping):
        raise ValueError("saved synthesis search archive is invalid")
    archive = search.get("design_archive")
    if archive is None and "design_archive" not in search:
        return []
    if not isinstance(archive, Mapping) or set(archive) != _ARCHIVE_FIELDS:
        raise ValueError("saved synthesis archive is invalid")
    entries = archive["entries"]
    if (type(archive["capacity"]) is not int or archive["capacity"] != request.archive_capacity
            or not isinstance(entries, list) or len(entries) > request.archive_capacity
            or type(archive["retained"]) is not int or archive["retained"] != len(entries)
            or not isinstance(archive["policy"], str)):
        raise ValueError("saved synthesis archive capacity or entries are invalid")
    admitted = _integer(archive["total_admitted"], "archive admitted count", minimum=0)
    pruned = _integer(archive["total_pruned"], "archive pruned count", minimum=0)
    if admitted - pruned != len(entries):
        raise ValueError("saved synthesis archive counters are invalid")
    seen, ranked = set(), []
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != _ARCHIVE_ENTRY_FIELDS:
            raise ValueError("saved synthesis archive entry is invalid")
        seed = _synthesis_seed(request, entry["design"], allowed)
        identity = physical_design_hash(seed)
        if entry["physical_hash"] != identity or identity in seen:
            raise ValueError("saved synthesis archive physical identity is invalid")
        seen.add(identity)
        maximum = _number(entry["max_error"], "archive error")
        rms = _number(entry["weighted_rms_error"], "archive RMS")
        total = math.fsum(row["thickness"]["value"] for row in seed["front_layers"])
        if (type(entry["layer_count"]) is not int or entry["layer_count"] != len(seed["front_layers"])
                or _number(entry["total_thickness_nm"], "archive thickness") != total
                or not isinstance(entry["window_max_errors"], list)
                or len(entry["window_max_errors"]) != len(request.objectives)
                or type(entry["pareto"]) is not bool
                or not isinstance(entry["operator"], str) or not entry["operator"]
                or not isinstance(entry["provenance"], Mapping)
                or (entry["parent_hash"] is not None and not _hash(entry["parent_hash"]))):
            raise ValueError("saved synthesis archive geometry or provenance is invalid")
        for value in entry["window_max_errors"]:
            _number(value, "archive window error")
        audit = entry["audit"]
        if entry["audit_status"] == "not_audited":
            if audit is not None:
                raise ValueError("saved synthesis archive audit status is inconsistent")
        elif entry["audit_status"] == "complete":
            if (not isinstance(audit, Mapping) or set(audit) != {
                    "status", "max_error", "weighted_rms_error", "condition_count", "physical_hash"}
                    or audit["status"] != "complete" or audit["physical_hash"] != identity):
                raise ValueError("saved synthesis archive audit descriptor is invalid")
            _number(audit["max_error"], "archive audit error")
            _number(audit["weighted_rms_error"], "archive audit RMS")
            _integer(audit["condition_count"], "archive audit condition count")
        else:
            raise ValueError("saved synthesis archive audit status is invalid")
        cost = (len(seed["front_layers"]), total)
        # Exported scalars are sorting hints, not trusted scores or audits.
        rank = ((0, *cost, rms, maximum) if maximum <= request.acceptable_normalized_error
                else (1, maximum, rms, *cost))
        ranked.append(((*rank, identity), seed))
    audited = archive["audited_champion_hash"]
    if audited is not None and audited not in seen:
        raise ValueError("saved synthesis archive champion identity is invalid")
    return [seed for _rank, seed in sorted(ranked, key=lambda item: item[0])]


def build_synthesis_warm_restart(
    payload: Mapping[str, Any],
    *,
    design_revision: int,
    max_cycles: int | None,
    max_evaluations: int | None,
    max_seconds: float | None,
) -> WarmRestartPlan:
    """Restart hybrid/legacy synthesis from best, bounded archive and old seeds."""
    _integer(design_revision, "design_revision", minimum=0)
    if max_cycles is not None:
        _integer(max_cycles, "max_cycles")
    if max_evaluations is not None:
        _integer(max_evaluations, "max_evaluations", minimum=2)
    if max_seconds is not None:
        _number(max_seconds, "max_seconds", positive=True)
    if not isinstance(payload, Mapping):
        raise ValueError("warm restart requires a current synthesis result")
    _json(dict(payload))
    try:
        valid = (type(payload.get("schema_version")) is int
                 and payload["schema_version"] == SCHEMA_VERSION
                 and is_synthesis_result(dict(payload)))
    except (TypeError, KeyError, AttributeError, StopIteration) as exc:
        raise ValueError("saved synthesis result schema or complete candidate is invalid") from exc
    if not valid:
        raise ValueError("saved synthesis result schema or complete candidate is invalid")
    result = payload["result"]
    if result["state"] != "cancelled" or result["candidate_design"] is None:
        raise ValueError("only cancelled synthesis with a complete candidate can be continued")
    _, request, _ = film_synthesis_request_from_payload({"schema_version": SCHEMA_VERSION,
        "kind": SYNTHESIS_REQUEST_KIND, "request_id": payload["request_id"],
        "request": result["frozen_request"], "physical_structures": []})
    if request.frozen_material_snapshot is None:
        raise ValueError("warm restart requires the original frozen request")
    # Frozen source controls must not be resolved against a different host.
    if ((request.max_working_set_bytes == 0 and request.resource_guard_resolution is None)
            or (request.search_strategy == "hybrid" and request.parallel_batch_size is None)
            or (request.randomize_template_search and request.structure_search_seed is None)):
        raise ValueError("original synthesis request has unresolved execution settings")
    checked = freeze_film_synthesis_request(None, request)
    if (_json(asdict(checked)) != _json(result["frozen_request"])
            or _json(asdict(request)) != _json(result["frozen_request"])):
        raise ValueError("original frozen synthesis request is not canonical")
    pairs = _frozen_pairs(request.frozen_material_snapshot)
    allowed = {(ref.material_id, ref.optical_set_id) for ref in request.candidate_materials}
    allowed.update((row["material_id"], row["optical_set_id"])
        for seed in request.seed_designs for row in seed["front_layers"])
    if not allowed.issubset(pairs):
        raise ValueError("original synthesis materials are outside the frozen snapshot")
    candidate = _synthesis_seed(request, result["candidate_design"], allowed)
    _require_materials(candidate, pairs)
    if not complete_original_score(request, result["training_score"]):
        raise ValueError("cancelled synthesis lacks complete matching saved score and coverage")
    originals = [_synthesis_seed(request, seed, allowed) for seed in request.seed_designs]
    archived = _archive_seeds(request, result.get("performance", {}), allowed)
    # Bound only exported restart geometry. Original explicit seeds are retained
    # independently of archive capacity; physical duplicates keep first identity.
    seeds, identities = [candidate], {physical_design_hash(candidate)}
    archive_added = 0
    for seed in archived:
        identity = physical_design_hash(seed)
        if identity not in identities and len(seeds) < request.archive_capacity:
            seeds.append(seed)
            identities.add(identity)
            archive_added += 1
    for seed in originals:
        identity = physical_design_hash(seed)
        if identity not in identities:
            seeds.append(seed)
            identities.add(identity)
    new_request = replace(deepcopy(request), design_revision=design_revision,
        max_cycles=max_cycles, max_evaluations=max_evaluations, max_seconds=max_seconds,
        seed_designs=tuple(deepcopy(seeds)), warm_start_seed_index=0)
    frozen = freeze_film_synthesis_request(None, new_request)
    if _json(asdict(frozen)) != _json(asdict(new_request)):
        raise ValueError("warm restart would change frozen inputs or execution settings")
    spent = {name: deepcopy(result.get(name)) for name in ("cycles", "evaluations",
        "validation_evaluations", "failed_evaluations", "audit_physical_evaluations", "audit_wavelength_points")}
    for name, value in spent.items():
        if value is not None:
            _integer(value, name, minimum=0)
    metadata = _metadata(request, candidate, source_kind="film_synthesis", spent=spent,
        fresh_budget={"max_cycles": max_cycles, "max_evaluations": max_evaluations,
                      "max_seconds": max_seconds})
    metadata["seeds"] = {"candidate_first": True, "archive_added": archive_added,
        "original_count": len(originals), "retained_count": len(seeds),
        "archive_geometry_capacity": request.archive_capacity, "saved_scores_reused": False}
    _json(metadata)
    return WarmRestartPlan(frozen, metadata)
