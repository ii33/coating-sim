"""Typed adapter between film synthesis and the durable project scheduler."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
import json
import math
import threading
import time
from typing import Any, Mapping
from pydantic import BaseModel, ConfigDict

from .film_auto_synthesis import (FilmSynthesisResult, FilmSynthesisTrace, _feasible,
                                  _proxy, synthesize_film_design)
from .film_optimization_structure import evaluate_frozen_structure_candidate
from .film_synthesis_io import (
    RESULT_KIND, SCHEMA_VERSION, film_synthesis_request_from_payload,
    film_synthesis_result_to_payload, prepare_film_synthesis_payload,
)

JOB_KIND = "coat_sim.film_synthesis_job"
AUDITED_CHECKPOINT_KIND = "coat_sim.film_synthesis_audited_checkpoint"
_PERFORMANCE_COUNTERS = (
    "parallel_batches", "submitted_tasks", "completed_tasks", "consumed_tasks",
    "discarded_tasks", "error_tasks", "cancelled_tasks", "unknown_tasks",
    "serial_fallback_tasks", "training_physical_evaluations", "training_wavelength_points",
    "worker_training_physical_evaluations", "worker_training_wavelength_points",
)


class SynthesisSubmitArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    payload: dict[str, Any]
    client_request_id: str | None = None


def validate_tool_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    json.dumps(arguments, allow_nan=False)
    return SynthesisSubmitArguments.model_validate(arguments).model_dump()


def canonical_parameters(service: Any, supplied: Mapping[str, Any]) -> dict[str, Any]:
    if (not isinstance(supplied, Mapping) or set(supplied) not in
            ({"payload"}, {"payload", "physical_structure_metadata"})):
        raise ValueError("film_synthesis requires exactly one request payload")
    if "physical_structure_metadata" in supplied:
        canonical, _ = prepare_film_synthesis_payload(None, supplied["payload"])
        metadata = supplied["physical_structure_metadata"]
        if not isinstance(metadata, list):
            raise ValueError("physical structure metadata must be an array")
        json.dumps(metadata, allow_nan=False)
        _validate_physical_structure_metadata(canonical, metadata)
        return {"payload": canonical, "physical_structure_metadata": deepcopy(metadata)}
    canonical, metadata = prepare_film_synthesis_payload(service, supplied["payload"])
    # The request itself is frozen and portable; metadata is provenance for the
    # result review and was generated from the same structure-library call.
    return {"payload": canonical, "physical_structure_metadata": list(metadata)}


def _validate_physical_structure_metadata(canonical: Mapping[str, Any], metadata: list[Any]) -> None:
    """Bind provenance annotations to reproducible structure-library outputs."""
    from .film_physical_structures import generate_physical_structure, structure_spec_from_mapping

    _, request, _ = film_synthesis_request_from_payload(canonical)
    snapshot = request.frozen_material_snapshot
    if not isinstance(snapshot, Mapping):
        raise ValueError("physical structure metadata requires a frozen material snapshot")
    def physical_identity(design: Mapping[str, Any]) -> str:
        # Top-level presentation text may gain a persisted assumption annotation.
        # Every physical field and the separately checked provenance remain exact.
        physical = {key: value for key, value in design.items() if key not in {"note", "summary"}}
        return json.dumps(physical, sort_keys=True, separators=(",", ":"), allow_nan=False)

    seeds = [physical_identity(item) for item in request.seed_designs]
    if len(metadata) > len(seeds):
        raise ValueError("physical structure metadata has no matching seed design")
    for index, item in enumerate(metadata):
        if not isinstance(item, Mapping) or set(item) != {"index", "mechanism", "name", "metadata"}:
            raise ValueError("physical structure metadata entry is invalid")
        details = item.get("metadata")
        if not isinstance(details, Mapping) or not isinstance(details.get("parameters"), Mapping):
            raise ValueError("physical structure metadata lacks reproducible parameters")
        spec_mapping = details["parameters"]
        spec = structure_spec_from_mapping(spec_mapping)
        regenerated = generate_physical_structure(request.environment, spec, snapshot,
            assume_unknown_k_zero=request.environment["assume_lossless"])
        expected = {"index": index, "mechanism": spec.mechanism,
                    "name": spec.name, "metadata": regenerated.metadata}
        if dict(item) != expected:
            raise ValueError("physical structure metadata does not match the structure library")
        regenerated_seed = physical_identity(regenerated.candidate_design)
        if regenerated_seed not in seeds:
            raise ValueError("physical structure metadata has no matching generated seed")


def build_job_payload(parameters: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(parameters, Mapping) or set(parameters) != {
            "payload", "physical_structure_metadata"}:
        raise ValueError("film_synthesis job parameters are invalid")
    canonical, _ = prepare_film_synthesis_payload(None, parameters["payload"])
    request_id, request, structures = film_synthesis_request_from_payload(canonical)
    if structures:
        raise ValueError("physical structures must be expanded into real seed designs before queueing")
    if not isinstance(parameters["physical_structure_metadata"], list):
        raise ValueError("physical structure metadata must be an array")
    json.dumps(parameters["physical_structure_metadata"], allow_nan=False)
    _validate_physical_structure_metadata(canonical, parameters["physical_structure_metadata"])
    return {"schema_version": SCHEMA_VERSION, "kind": JOB_KIND,
            "request_id": request_id, "request": canonical,
            "physical_structure_metadata": deepcopy(parameters["physical_structure_metadata"])}


def deserialize_job_payload(value: Mapping[str, Any]):
    if (not isinstance(value, Mapping) or set(value) != {
            "schema_version", "kind", "request_id", "request", "physical_structure_metadata"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION
            or value.get("kind") != JOB_KIND or not isinstance(value.get("physical_structure_metadata"), list)):
        raise ValueError("film synthesis job envelope is invalid")
    request_id, request, structures = film_synthesis_request_from_payload(value["request"])
    if structures or request_id != value["request_id"]:
        raise ValueError("film synthesis job request identity or structure state is invalid")
    _validate_physical_structure_metadata(value["request"], value["physical_structure_metadata"])
    return request_id, request, deepcopy(value["physical_structure_metadata"])


def _partial_result(request, request_id, checkpoint, *, terminal_state="cancelled"):
    design = deepcopy(checkpoint["design"])
    progress = checkpoint.get("progress", {})
    validation = deepcopy(checkpoint["validation_score"])
    counter_sources = {"cycles": "cycle", "evaluations": "evaluations",
        "validation_evaluations": "validation_evaluations", "failed_evaluations": "failed_evaluations",
        "audit_physical_evaluations": "audit_physical_evaluations",
        "audit_wavelength_points": "audit_wavelength_points"}
    counters = {}
    unknown_fields = []
    for field, source in counter_sources.items():
        number = progress.get(source)
        if number is None:
            unknown_fields.append(field)
            counters[field] = 0  # Internal typed placeholder; public output below is null.
        elif type(number) is not int or number < 0:
            raise ValueError("checkpoint work counters must be nonnegative integers")
        else:
            counters[field] = number
    work_metadata = {"work_counters_complete": not unknown_fields,
        "unknown_fields": unknown_fields, "scope": "original_worker_through_saved_checkpoint",
        "recovery_replay_work": deepcopy(checkpoint.get("recovery_replay_work", {}))}
    saved_performance = checkpoint.get("synthesis_performance")
    if saved_performance is None:
        saved_audit = checkpoint.get("audit")
        audit_performance = saved_audit.get("synthesis_performance") if isinstance(saved_audit, Mapping) else None
        saved_performance = audit_performance if isinstance(audit_performance, Mapping) else None
    performance = _historical_checkpoint_performance(saved_performance)
    result = FilmSynthesisResult(
        state=terminal_state, termination_reason="audited_checkpoint_recovered",
        candidate_design=design, design_revision=request.design_revision,
        targets_met=(terminal_state == "completed" and bool(validation.get("targets_met", False))),
        training_score=deepcopy(checkpoint["training_score"]), validation_score=validation,
        validation_complete=True, cycles=counters["cycles"],
        evaluations=counters["evaluations"],
        validation_evaluations=counters["validation_evaluations"], failed_evaluations=counters["failed_evaluations"],
        trace=(FilmSynthesisTrace("audit_incumbent", counters["cycles"], counters["evaluations"],
              len(design.get("front_layers", ())), validation.get("max_error"),
              message="Recovered independently audited checkpoint"),),
        message="Recovered the best independently audited candidate saved before worker exit.",
        frozen_request=request,
        audit_physical_evaluations=counters["audit_physical_evaluations"],
        audit_wavelength_points=counters["audit_wavelength_points"],
        performance=performance)
    payload = film_synthesis_result_to_payload(request_id, result,
        audit={"physical_evaluations": result.audit_physical_evaluations,
               "wavelength_points": result.audit_wavelength_points, **work_metadata},
        physical_structure_metadata=checkpoint.get("physical_structure_metadata", ()))
    payload["work_counters"] = deepcopy(work_metadata)
    payload["review"]["cost"].update(deepcopy(work_metadata))
    for field in unknown_fields:
        payload["result"][field] = None
        payload["review"]["cost"][field] = None
    if "cycles" in unknown_fields:
        payload["result"]["trace"][0]["cycle"] = None
    if "evaluations" in unknown_fields:
        payload["result"]["trace"][0]["evaluations"] = None
    if "audit_physical_evaluations" in unknown_fields:
        payload["audit"]["physical_evaluations"] = None
    if "audit_wavelength_points" in unknown_fields:
        payload["audit"]["wavelength_points"] = None
    return payload


def _historical_checkpoint_performance(value: Any) -> dict[str, Any]:
    """Return saved execution diagnostics without claiming old workers are live."""
    recorded = isinstance(value, Mapping) and value.get("recorded") is True
    result = deepcopy(dict(value)) if isinstance(value, Mapping) else {}
    unknown_fields = result.get("unknown_fields", [])
    if not isinstance(unknown_fields, list) or any(not isinstance(item, str) for item in unknown_fields):
        unknown_fields = []
    else:
        unknown_fields = list(unknown_fields)
    for key in _PERFORMANCE_COUNTERS:
        count = result.get(key)
        if (type(count) is not int or count < 0) and key not in unknown_fields:
            unknown_fields.append(key)
        if key not in result:
            result[key] = None
    result.update({"recorded": recorded, "historical": True,
                   "execution_state": "historical_checkpoint",
                   "active_worker_pids": [], "actual_work_complete": False,
                   "unknown_fields": unknown_fields})
    return result


def _json_snapshot(value: Any) -> Any:
    """Convert frozen score records to strict JSON before scheduler persistence."""
    def default(item):
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        if hasattr(item, "item"):
            return item.item()
        raise TypeError(f"unsupported checkpoint value: {type(item).__name__}")
    return json.loads(json.dumps(value, default=default, allow_nan=False))


def validate_checkpoint(value: Mapping[str, Any], *, terminal_state="cancelled") -> dict[str, Any]:
    if (not isinstance(value, Mapping) or value.get("kind") != AUDITED_CHECKPOINT_KIND
            or value.get("checkpoint_state") != "audited"):
        raise ValueError("not an audited film synthesis checkpoint")
    request_id, request, metadata = deserialize_job_payload(value["job"])
    from .film_synthesis_audit import audit_frozen_synthesis_candidate
    from .film_design import validate_design
    design = validate_design(deepcopy(value["design"]))
    physical_keys = ("substrate", "incident_medium", "exit_medium", "first_surface",
                     "layer_order", "reference_wavelength_nm", "use_k", "assume_lossless")
    if (any(design.get(key) != request.environment.get(key) for key in physical_keys)
            or design.get("back_layers")
            or any(row["thickness"].get("unit") != "nm" for row in design["front_layers"])
            or not _feasible(request, design)):
        raise ValueError("audited synthesis checkpoint violates frozen physical constraints")
    training = evaluate_frozen_structure_candidate(_proxy(request), design,
        derived_validation_grid=False)["score"]
    stored_training = value["training_score"]
    stored = value["validation_score"]
    audit_record = value["audit"]
    if not isinstance(stored_training, Mapping) or not isinstance(stored, Mapping) or not isinstance(audit_record, Mapping):
        raise ValueError("audited synthesis checkpoint score or audit record is invalid")
    for key in ("max_error", "weighted_rms_error"):
        if (key not in stored_training or not math.isfinite(float(stored_training[key]))
                or abs(float(stored_training[key]) - float(training[key])) > 1e-10 * max(1.0, abs(float(training[key])))):
            raise ValueError("audited synthesis checkpoint training score does not replay")
    audited = audit_frozen_synthesis_candidate(request, design)
    actual = audited.get("score", {})
    for key in ("max_error", "weighted_rms_error"):
        if (key not in stored or not math.isfinite(float(stored[key]))
                or abs(float(stored[key]) - float(actual[key])) > 1e-10 * max(1.0, abs(float(actual[key])))):
            raise ValueError("audited synthesis checkpoint score does not replay")
    checked = {**deepcopy(dict(value)), "design": design,
               "training_score": training,
               "validation_score": audited.get("score", stored),
               "audit": audited.get("audit", value["audit"]),
               "recovery_replay_work": {"training_evaluations": 1, "validation_evaluations": 1,
                   "audit_physical_evaluations": audited.get("audit", {}).get("physical_evaluations", 0),
                   "audit_wavelength_points": audited.get("audit", {}).get("wavelength_points", 0)},
               "physical_structure_metadata": metadata}
    return _partial_result(request, request_id, checked, terminal_state=terminal_state)


def _validate_completed_cancel_candidate(value: Mapping[str, Any]) -> None:
    """Check saved conditions/constraints only; never run TMM or an audit.

    This validates an explicitly parent-committed worker result, not an
    arbitrary historical checkpoint. The scheduler binds it to its input job.
    """
    from .film_design import validate_design
    from .film_design_service import active_material_references
    from .film_optimization import _checked_frozen_snapshot
    from .film_optimization_scoring import score_condition, weighted_rms_error
    from .film_synthesis_absorption import sample_synthesis_optical
    from .optical_constants import prepare_optical_set

    result = value["result"]
    provenance = result["candidate_provenance"]
    if (set(provenance) != {"selection", "training_complete", "frozen_at_cancel",
            "fully_scored_evaluations", "validation_matches_candidate"}
            or result["state"] != "cancelled" or result["targets_met"] is not False
            or provenance["selection"] != "completed_training_at_cancel"
            or provenance["training_complete"] is not True or provenance["frozen_at_cancel"] is not True
            or type(provenance["fully_scored_evaluations"]) is not int
            or provenance["fully_scored_evaluations"] < 1
            or type(provenance["validation_matches_candidate"]) is not bool):
        raise ValueError("cancel candidate lacks a complete parent-committed provenance")
    _, request, _ = film_synthesis_request_from_payload({
        "schema_version": SCHEMA_VERSION, "kind": "coat_sim.film_synthesis_request",
        "request_id": value["request_id"], "request": result["frozen_request"], "physical_structures": []})
    if request.frozen_material_snapshot is None:
        raise ValueError("cancel candidate requires frozen material provenance")
    snapshot = _checked_frozen_snapshot(request.frozen_material_snapshot, request.environment)
    design = validate_design(result["candidate_design"])
    physical_keys = ("substrate", "incident_medium", "exit_medium", "first_surface",
                     "layer_order", "reference_wavelength_nm", "use_k", "assume_lossless")
    if (result["design_revision"] != request.design_revision
            or any(design[key] != request.environment[key] for key in physical_keys)
            or design["back_layers"] or not _feasible(request, design)
            or any(row["thickness"]["unit"] != "nm" for row in design["front_layers"])):
        raise ValueError("cancel candidate violates its frozen physical constraints")
    if (type(result["evaluations"]) is not int
            or result["evaluations"] < provenance["fully_scored_evaluations"]):
        raise ValueError("cancel candidate complete-score count exceeds logical work")
    allowed = {(ref.material_id, ref.optical_set_id) for ref in request.candidate_materials}
    allowed.update((row["material_id"], row["optical_set_id"])
        for seed in request.seed_designs for row in seed["front_layers"])
    if any((row["material_id"], row["optical_set_id"]) not in allowed for row in design["front_layers"]):
        raise ValueError("cancel candidate introduces material outside the frozen search inputs")
    materials = {row["id"]: row for row in snapshot["materials"]}
    # The complete frozen request was already source-checked before execution.
    # Recheck active endpoints/policy without a spectral simulation or dense audit.
    for reference in active_material_references(design):
        material = materials[reference["material_id"]]
        optical = next(row for row in material["optical_sets"] if row["id"] == reference["optical_set_id"])
        sampler = prepare_optical_set(optical, k_sampling_policy="source")
        for objective in request.objectives:
            for wave in (objective.wavelength_min_nm, objective.wavelength_max_nm):
                sample_synthesis_optical(sampler, wave,
                    assume_unknown_k_zero=request.environment["assume_lossless"])

    def same_design(scored):
        checked = validate_design(scored)
        return ({key: item for key, item in checked.items() if key not in {"note", "summary"}}
                == {key: item for key, item in design.items() if key not in {"note", "summary"}})

    def number(item):
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ValueError("cancel candidate score contains a nonfinite/non-numeric value")
        return float(item)

    def equal(actual, expected):
        return math.isclose(number(actual), float(expected), rel_tol=1e-10, abs_tol=1e-12)

    training = result["training_score"]
    if not isinstance(training, Mapping) or not same_design(training["design"]):
        raise ValueError("cancel candidate training score belongs to another design")
    errors = training["errors"]
    expected_count = sum(o.wavelength_samples * len(o.angles_deg) for o in request.objectives)
    if not isinstance(errors, (list, tuple)) or len(errors) != expected_count:
        raise ValueError("cancel candidate does not contain every training condition")
    cursor = 0
    normalized, weighted, weights = [], [], []
    for index, objective in enumerate(request.objectives):
        for angle in objective.angles_deg:
            for sample in range(objective.wavelength_samples):
                wave = (objective.wavelength_min_nm if objective.wavelength_samples == 1 else
                    objective.wavelength_min_nm + (objective.wavelength_max_nm - objective.wavelength_min_nm)
                    * sample / (objective.wavelength_samples - 1))
                row = errors[cursor]
                cursor += 1
                if (not isinstance(row, Mapping) or type(row.get("objective_index")) is not int
                        or row["objective_index"] != index or not equal(row["wavelength_nm"], wave)
                        or not equal(row["angle_deg"], angle) or row["metric"] != objective.metric
                        or row["polarization"] != objective.polarization
                        or row["target_value"] != objective.target_value
                        or row["lower_bound"] != objective.lower_bound
                        or row["upper_bound"] != objective.upper_bound):
                    raise ValueError("cancel candidate training condition differs from the frozen objective")
                actual = number(row["actual"])
                violation = (abs(actual - objective.target_value) if objective.mode == "value" else
                    max(0., 0. if objective.lower_bound is None else objective.lower_bound - actual,
                        0. if objective.upper_bound is None else actual - objective.upper_bound))
                norm, weight = score_condition(violation, objective.normalization_scale, objective.weight)
                if not equal(row["normalized_violation"], norm) or not equal(row["weighted_normalized_violation"], weight):
                    raise ValueError("cancel candidate saved condition scores are inconsistent")
                normalized.append(norm)
                weighted.append(weight)
                weights.append(objective.weight)
    if (not equal(training["max_error"], max(normalized))
            or not equal(training["weighted_max_error"], max(weighted))
            or not equal(training["weighted_rms_error"], weighted_rms_error(normalized, weights))):
        raise ValueError("cancel candidate aggregate training scores are inconsistent")
    quality = value["best_candidate_audit"]
    if (not equal(quality["training_max_normalized_error"], training["max_error"])
            or not equal(quality["acceptable_normalized_error"], request.acceptable_normalized_error)):
        raise ValueError("cancel candidate review training score does not match")
    matches = provenance["validation_matches_candidate"]
    if result["validation_complete"] is not matches or quality["available"] is not matches:
        raise ValueError("cancel candidate audit availability is inconsistent")
    validation = result["validation_score"]
    if matches:
        if (not isinstance(validation, Mapping) or not same_design(validation["design"])
                or number(validation["max_error"]) < 0 or number(validation["weighted_rms_error"]) < 0
                or not equal(quality["audit_max_normalized_error"], validation["max_error"])
                or quality["targets_met"] is not (max(training["max_error"], validation["max_error"])
                                                   <= request.acceptable_normalized_error)):
            raise ValueError("cancel candidate audit belongs to another design or quality")
        import numpy as np
        audit = validation.get("audit")
        audited_errors = validation.get("errors")
        dense_count = sum((1 if o.wavelength_samples == 1 else
            (o.wavelength_samples - 1) * request.validation_samples_multiplier + 1) * len(o.angles_deg)
            for o in request.objectives)
        if (not isinstance(audit, Mapping) or not isinstance(audited_errors, (list, tuple))
                or type(audit.get("dense_points")) is not int or audit["dense_points"] != dense_count
                or type(audit.get("unique_condition_points")) is not int
                or audit["unique_condition_points"] != len(audited_errors)
                or type(audit.get("physical_evaluations")) is not int or audit["physical_evaluations"] < 1
                or type(audit.get("wavelength_points")) is not int or audit["wavelength_points"] < dense_count
                or type(audit.get("completed_extrema")) is not int or audit["completed_extrema"] < 0
                or audit.get("continuous_certificate") is not False
                or audit.get("rms_measure") != "fixed_dense_grid_only"
                or audit.get("completed_extrema") != audit.get("bracket_count")
                or not isinstance(audit.get("objective_status"), list)
                or len(audit["objective_status"]) != len(request.objectives)):
            raise ValueError("cancel candidate lacks a complete saved independent audit")
        points = {}
        audit_normalized, audit_weighted = [], []
        for row in audited_errors:
            audit_index = row.get("objective_index") if isinstance(row, Mapping) else None
            if type(audit_index) is not int or not 0 <= audit_index < len(request.objectives):
                raise ValueError("cancel candidate audit objective index is invalid")
            objective = request.objectives[audit_index]
            wave, angle = number(row["wavelength_nm"]), number(row["angle_deg"])
            if (not objective.wavelength_min_nm <= wave <= objective.wavelength_max_nm
                    or angle not in objective.angles_deg or row["metric"] != objective.metric
                    or row["polarization"] != objective.polarization
                    or row["target_value"] != objective.target_value
                    or row["lower_bound"] != objective.lower_bound or row["upper_bound"] != objective.upper_bound):
                raise ValueError("cancel candidate saved audit condition differs from its objective")
            actual = number(row["actual"])
            violation = (abs(actual - objective.target_value) if objective.mode == "value" else
                max(0., 0. if objective.lower_bound is None else objective.lower_bound - actual,
                    0. if objective.upper_bound is None else actual - objective.upper_bound))
            norm, weighted_error = score_condition(violation, objective.normalization_scale, objective.weight)
            if not equal(row["normalized_violation"], norm) or not equal(row["weighted_normalized_violation"], weighted_error):
                raise ValueError("cancel candidate saved audit scores are inconsistent")
            point = (audit_index, angle, wave)
            if point in points:
                raise ValueError("cancel candidate audit repeats a condition")
            points[point] = norm
            audit_normalized.append(norm)
            audit_weighted.append(weighted_error)
        dense_errors, dense_weights = [], []
        for index, objective in enumerate(request.objectives):
            count = 1 if objective.wavelength_samples == 1 else (
                (objective.wavelength_samples - 1) * request.validation_samples_multiplier + 1)
            for angle in objective.angles_deg:
                for wave in np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm, count):
                    dense_errors.append(points[(index, angle, float(wave))])
                    dense_weights.append(objective.weight)
        if (not equal(validation["max_error"], max(audit_normalized))
                or not equal(validation["weighted_max_error"], max(audit_weighted))
                or not equal(validation["weighted_rms_error"], weighted_rms_error(dense_errors, dense_weights))):
            raise ValueError("cancel candidate saved audit aggregate scores are inconsistent")
    elif (validation is not None or quality["audit_max_normalized_error"] is not None
            or quality["targets_met"] is not None or quality["continuous_certificate"] is not False):
        raise ValueError("unaudited cancel candidate must not advertise audit evidence")
    layers = value["review"]["layers"]
    for layer, reviewed in zip(design["front_layers"], layers, strict=True):
        if (reviewed["id"] != layer["id"] or reviewed["material_id"] != layer["material_id"]
                or reviewed["optical_set_id"] != layer["optical_set_id"]
                or not equal(reviewed["thickness_nm"], layer["thickness"]["value"])):
            raise ValueError("cancel candidate review layer differs from the frozen candidate")


def is_synthesis_result(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION or value.get("kind") != RESULT_KIND:
        return False
    result = value.get("result")
    review = value.get("review")
    candidate_audit = value.get("best_candidate_audit")
    if not isinstance(result, dict) or not isinstance(review, dict):
        return False
    if (not isinstance(candidate_audit, dict)
            or type(candidate_audit.get("available")) is not bool
            or type(candidate_audit.get("continuous_certificate")) is not bool
            or (candidate_audit.get("targets_met") is not None
                and type(candidate_audit.get("targets_met")) is not bool)):
        return False
    for key in ("training_max_normalized_error", "audit_max_normalized_error",
                "acceptable_normalized_error"):
        number = candidate_audit.get(key)
        if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float))
                                   or not math.isfinite(number)):
            return False
    if result.get("state") not in {"completed", "cancelled", "failed", "not_found"}:
        return False
    if (type(result.get("validation_complete")) is not bool
            or type(result.get("targets_met")) is not bool):
        return False
    candidate = result.get("candidate_design")
    if candidate is None:
        return (result.get("state") in {"not_found", "cancelled", "failed"}
                and not result["validation_complete"] and not result["targets_met"]
                and candidate_audit["available"] is False
                and candidate_audit["targets_met"] is None
                and review.get("layers") == [])
    if not (isinstance(candidate, dict) and isinstance(candidate.get("front_layers"), list)
            and isinstance(review.get("layers"), list)
            and len(review["layers"]) == len(candidate["front_layers"])
            and isinstance(review.get("per_objective"), list)):
        return False
    provenance = result.get("candidate_provenance")
    if provenance:
        try:
            _validate_completed_cancel_candidate(value)
        except (KeyError, TypeError, ValueError, ArithmeticError, StopIteration):
            return False
        return True
    # Legacy results remain audited-only; a missing provenance flag must never
    # turn an arbitrary unaudited candidate into an applicable cancellation.
    return (result["validation_complete"] is True
            and isinstance(result.get("validation_score"), dict)
            and isinstance(result.get("training_score"), dict))


def submit_synthesis(service, payload, client_request_id=None):
    from .compute_tasks import ComputeTaskStore, digest
    from coat_sim.scheduler.submission import submit_confirmed
    values = canonical_parameters(service, {"payload": payload})
    identity = digest({"kind": "film_synthesis", "parameters": values, "input_files": {}})
    client_request_id = client_request_id if client_request_id is not None else "film-synthesis:" + identity
    if not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 512:
        raise ValueError("client_request_id must contain 1 to 512 characters")
    store = ComputeTaskStore(service.root)
    prior = store.find_request(client_request_id)
    if prior is not None and prior["input_digest"] != identity:
        raise ValueError("request_id_conflict: film synthesis request inputs changed")
    if prior is None:
        key = store.claim_unbound_estimate(client_request_id, identity)
        if key is not None:
            prior = store.read_estimate(key)
    if prior is None:
        receipt = submit_confirmed(service, "film_synthesis", values, client_request_id)
    elif prior["state"] in {"estimated", "pending"}:
        receipt = {**store.public_estimate(prior),
            "state": "prepared" if prior["state"] == "estimated" else "awaiting_confirmation",
            "needs_release_request": prior["state"] == "estimated",
            "needs_user_confirmation": prior.get("release_policy") == "manual",
            "next_action": "request_compute_confirmation"}
    else:
        receipt = submit_confirmed(service, "film_synthesis", deepcopy(values),
                                   client_request_id, estimate_id=prior["estimate_id"])
    return {**receipt, "execution_project_root": str(service.root),
            "client_request_id": client_request_id,
            "estimate_id": receipt.get("estimate_id") or (prior or {}).get("estimate_id")}


def execute_job(value: Mapping[str, Any], cancel_event, report, checkpoint_path, write_json,
                execution_project_root: str):
    if isinstance(value, Mapping) and set(value) == {"payload"}:
        job = deepcopy(value["payload"])
        deserialize_job_payload(job)
    else:
        job = build_job_payload(value)
    request_id, request, metadata = deserialize_job_payload(job)
    latest: dict[str, Any] = {"stage": "starting", "cycle": 0, "evaluations": 0,
              "layer_count": 0, "best_max_normalized_error": None,
              "heartbeat_sequence": 0}
    have_audited_best = [False]
    last_algorithm_activity = [time.monotonic()]
    stop_heartbeat = threading.Event()
    report_lock = threading.Lock()

    def publish(payload, *, force=False):
        with report_lock:
            report(payload, force=force)

    def progress(item):
        with report_lock:
            latest.update(asdict(item))
            if item.stage == "audit_incumbent":
                have_audited_best[0] = True
            last_algorithm_activity[0] = time.monotonic()
            report({"stage": latest["stage"], "status": item.message,
                "cycle": item.cycle, "evaluations": item.evaluations,
                "layer_count": item.layer_count,
                "best_max_normalized_error": item.best_max_normalized_error,
                "heartbeat_sequence": latest["heartbeat_sequence"],
                "score_scope": "audited" if have_audited_best[0] else "training",
                "stalled": False, "unresponsive": False})

    def heartbeat():
        while not stop_heartbeat.wait(1.0):
            with report_lock:
                age = max(0.0, time.monotonic() - last_algorithm_activity[0])
                latest["heartbeat_sequence"] = int(latest.get("heartbeat_sequence", 0)) + 1
                current: dict[str, Any] = deepcopy(latest)
                report({"stage": current["stage"], "status": "worker heartbeat",
                "cycle": current["cycle"], "evaluations": current["evaluations"],
                "layer_count": current["layer_count"],
                "best_max_normalized_error": current["best_max_normalized_error"],
                "heartbeat_sequence": current["heartbeat_sequence"],
                "score_scope": "audited" if have_audited_best[0] else "training",
                "algorithm_progress_age_seconds": round(age, 2),
                "worker_heartbeat_age_seconds": 0.0,
                "stalled": age >= 30.0, "unresponsive": age >= 120.0,
                "heartbeat": True}, force=True)

    def checkpoint(design, training_score, validation_score, audit):
        synthesis_performance = audit.get("synthesis_performance")
        record = {"schema_version": SCHEMA_VERSION, "kind": AUDITED_CHECKPOINT_KIND,
            "checkpoint_state": "audited", "job": deepcopy(job),
            "design": deepcopy(design), "training_score": _json_snapshot(training_score),
            "validation_score": _json_snapshot(validation_score), "audit": _json_snapshot(audit),
            "progress": {**{key: latest.get(key) for key in ("cycle", "evaluations", "layer_count")},
                         **deepcopy(audit.get("synthesis_cumulative_work", {}))},
            "synthesis_performance": _json_snapshot(synthesis_performance),
            "physical_structure_metadata": deepcopy(metadata)}
        write_json(checkpoint_path, _json_snapshot(record))

    monitor = threading.Thread(target=heartbeat, name="film-synthesis-heartbeat", daemon=True)
    monitor.start()
    try:
        canonical, generated_metadata = prepare_film_synthesis_payload(None, job["request"], progress_callback=lambda p: publish(p))
        request_id, request, _ = film_synthesis_request_from_payload(canonical)
        result = synthesize_film_design(None, request, cancel_event=cancel_event,
                                        progress_callback=progress, snapshot_callback=checkpoint)
        if not result.validation_complete and not (result.state == "cancelled"
                and result.candidate_provenance.get("selection") == "completed_training_at_cancel"):
            result = replace(result, candidate_design=None, targets_met=False,
                             training_score=None, validation_score=None)
        response = film_synthesis_result_to_payload(request_id, result,
            audit={"physical_evaluations": result.audit_physical_evaluations,
                   "wavelength_points": result.audit_wavelength_points},
            physical_structure_metadata=generated_metadata or metadata)
        if result.state == "cancelled":
            response["review"]["cancel_snapshot"] = {"source": "worker_completed_reply",
                "replay_performed": False, "candidate_available": result.candidate_design is not None}
        response["execution_project_root"] = execution_project_root
        return response
    finally:
        stop_heartbeat.set()
        monitor.join(timeout=2.0)
