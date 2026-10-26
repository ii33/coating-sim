"""Typed adapter between film synthesis and the durable project scheduler."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields, is_dataclass, replace
import json
import math
import threading
import time
from typing import Any, Mapping
from pydantic import BaseModel, ConfigDict

from .film_auto_synthesis import (FilmSynthesisResult, FilmSynthesisTrace, _feasible,
                                  freeze_film_synthesis_request,
                                  _proxy, synthesize_film_design)
from .film_nominal_acceptance import (ACCEPTANCE_SEMANTICS, complete_original_score,
    nominal_targets_pass, solver_acceptable)
from .film_optimization_structure import evaluate_frozen_structure_candidate
from .film_synthesis_io import (
    RESULT_KIND, SCHEMA_VERSION, film_synthesis_request_from_payload,
    film_synthesis_result_to_payload, film_synthesis_request_to_payload, prepare_film_synthesis_payload,
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
    # Admission owns entropy resolution. Queue construction and replay must
    # never turn an unresolved request into a different random search.
    film_synthesis_request_from_payload(parameters["payload"])
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
    nominal = (nominal_targets_pass(request, checkpoint["training_score"])
        and nominal_targets_pass(request, validation, independent_audit=True))
    state = "not_found" if terminal_state == "completed" and not nominal else terminal_state
    result = FilmSynthesisResult(
        state=state, termination_reason=("validation_targets_not_met" if state != terminal_state
            else "audited_checkpoint_recovered"),
        candidate_design=design, design_revision=request.design_revision,
        targets_met=state == "completed" and nominal,
        solver_acceptable=(solver_acceptable(request, checkpoint["training_score"])
            and solver_acceptable(request, validation, independent_audit=True)),
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
        performance=performance, search_checkpoint=deepcopy(checkpoint.get('search_checkpoint')),
        local_optimum=deepcopy(checkpoint.get('local_optimum')))
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


def _validate_objective_status(request, score):
    from .film_synthesis_audit import objective_constraint_status
    expected = objective_constraint_status(request.objectives, score["errors"],
                                           request.acceptable_normalized_error)
    if score.get("audit", {}).get("objective_status") != expected:
        raise ValueError("saved audit objective status differs from its enabled criteria")
    return expected


def _verify_saved_score(request, stored, actual, *, independent=False):
    """Validate raw coverage and all active records against physical replay."""
    from .film_design import validate_design
    if not complete_original_score(request, stored, independent_audit=independent):
        raise ValueError("saved synthesis score lacks complete raw and active evidence")
    def physical(design):
        return {k: v for k, v in validate_design(design).items() if k not in {"note", "summary"}}
    if physical(stored["design"]) != physical(actual["design"]):
        raise ValueError("saved synthesis score belongs to another design")
    replay = _json_snapshot(actual)
    def equal(a, b):
        return (not isinstance(a, bool) and isinstance(a, (int, float))
            and math.isfinite(a) and math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-12))
    for key in ("max_error", "weighted_max_error", "weighted_rms_error"):
        if not equal(stored[key], replay[key]):
            raise ValueError("saved synthesis aggregate score does not physically replay")
    def order(row):
        return (row["objective_index"], row["angle_deg"], row["reduction"],
                -1. if row["wavelength_nm"] is None else row["wavelength_nm"])
    old_rows, new_rows = sorted(stored["errors"], key=order), sorted(replay["errors"], key=order)
    if len(old_rows) != len(new_rows):
        raise ValueError("saved synthesis active condition count does not physically replay")
    numeric = {"angle_deg", "actual", "normalized_violation", "weighted_normalized_violation"}
    for old, new in zip(old_rows, new_rows, strict=True):
        if set(old) != set(new) or any(not equal(old[k], v) if k in numeric else old[k] != v
                                      for k, v in new.items()):
            raise ValueError("saved synthesis physical condition does not replay")
    for field in (("coverage", "audit_coverage") if independent else ("coverage",)):
        previous, current = stored[field], replay[field]
        if len(previous) != len(current):
            raise ValueError("saved raw spectral coverage does not physically replay")
        for old, new in zip(previous, current, strict=True):
            if (set(old) != set(new) or old["objective_index"] != new["objective_index"]
                    or old["angle_deg"] != new["angle_deg"]):
                raise ValueError("saved raw coverage identity does not physically replay")
            for key in ("wavelength_nm", "actual_values"):
                if len(old[key]) != len(new[key]) or any(not equal(a, b)
                        for a, b in zip(old[key], new[key], strict=True)):
                    raise ValueError("saved raw spectral values do not physically replay")
    for field in ("nominal_pass", "solver_acceptable", "condition_count"):
        if stored[field] != replay[field]:
            raise ValueError("saved synthesis acceptance does not physically replay")
    if independent:
        _validate_objective_status(request, stored)


def validate_checkpoint(value: Mapping[str, Any], *, terminal_state="cancelled") -> dict[str, Any]:
    if (not isinstance(value, Mapping) or type(value.get("schema_version")) is not int
            or value["schema_version"] != SCHEMA_VERSION or value.get("kind") != AUDITED_CHECKPOINT_KIND
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
    _verify_saved_score(request, stored_training, training)
    audited = audit_frozen_synthesis_candidate(request, design)
    _verify_saved_score(request, stored, audited["score"], independent=True)
    checked = {**deepcopy(dict(value)), "design": design,
               "training_score": training,
               "validation_score": audited.get("score", stored),
               "audit": audited.get("audit", value["audit"]),
               "recovery_replay_work": {"training_evaluations": 1, "validation_evaluations": 1,
                   "audit_physical_evaluations": audited.get("audit", {}).get("physical_evaluations", 0),
                   "audit_wavelength_points": audited.get("audit", {}).get("wavelength_points", 0)},
               "physical_structure_metadata": metadata}
    search_state = audit_record.get('search_checkpoint')
    if search_state is not None:
        from .film_synthesis_continuation import validate_search_state
        checked['search_checkpoint'] = validate_search_state(freeze_film_synthesis_request(None, request), search_state)
    local = deepcopy(audit_record.get('local_optimum'))
    if local is not None:
        # Historical local quality gets the same explicit physical replay as
        # the final candidate. Old scores are never promoted by mere JSON load.
        from .film_synthesis_archive import physical_design_hash
        local_design = validate_design(local['design'])
        _validate_result_physical_binding(request, dict(design_revision=request.design_revision,
            candidate_design=local_design, training_score=local['training_score'], validation_score=local['validation_score']))
        if local.get('physical_hash') != physical_design_hash(local_design) or local.get('validation_complete') is not True:
            raise ValueError('local checkpoint audit belongs to another candidate')
        if local.get('termination') == 'converged' or local.get('local_converged') is True:
            terminal, returned, selected = (local.get(k) for k in ('solver_terminal','returned_best','audited_selected'))
            if (local.get('local_converged') is not True or local.get('termination') != 'converged'
                    or any(not isinstance(r,dict) or r.get('physical_hash') != local['physical_hash']
                           for r in (terminal,returned,selected))
                    or terminal.get('local_stationary') is not True or terminal.get('nominal_pass') is not True
                    or terminal.get('epigraph_feasible') is not True or returned.get('is_solver_terminal') is not True
                    or returned.get('role') != 'solver_terminal' or selected.get('nominal_pass') is not True
                    or selected.get('topology_matches_fixed') is not True):
                raise ValueError('local checkpoint convergence identity does not match the audited candidate')
        local_training = evaluate_frozen_structure_candidate(_proxy(request), local_design,
            derived_validation_grid=False)['score']
        local_audit = audit_frozen_synthesis_candidate(request, local_design)
        _verify_saved_score(request, local['training_score'], local_training)
        _verify_saved_score(request, local['validation_score'], local_audit['score'], independent=True)
        local.update(training_score=local_training, validation_score=local_audit['score'])
        checked['local_optimum'] = local
        work = checked['recovery_replay_work']
        work.update(local_training_evaluations=1, local_validation_evaluations=1,
            local_audit_physical_evaluations=local_audit['audit']['physical_evaluations'],
            local_audit_wavelength_points=local_audit['audit']['wavelength_points'])
    return _partial_result(request, request_id, checked, terminal_state=terminal_state)


def _validate_completed_cancel_candidate(value: Mapping[str, Any]) -> None:
    _validate_completed_training_candidate(value, time_limited=False)


def _validate_completed_time_limit_candidate(value: Mapping[str, Any]) -> None:
    _validate_completed_training_candidate(value, time_limited=True)


def _validate_completed_training_candidate(value: Mapping[str, Any], *, time_limited: bool) -> None:
    """Check saved conditions/constraints only; never run TMM or an audit.

    This validates an explicitly parent-committed worker result, not an
    arbitrary historical checkpoint. The scheduler binds it to its input job.
    """
    from .film_design import validate_design
    from .film_design_service import active_material_references
    from .film_optimization import _checked_frozen_snapshot
    from .film_synthesis_absorption import sample_synthesis_optical
    from .optical_constants import prepare_optical_set

    result = value["result"]
    provenance = result["candidate_provenance"]
    frozen_flag = "frozen_at_time_limit" if time_limited else "frozen_at_cancel"
    selection = "completed_training_at_time_limit" if time_limited else "completed_training_at_cancel"
    state = "not_found" if time_limited else "cancelled"
    if (set(provenance) != {"selection", "training_complete", frozen_flag,
            "fully_scored_evaluations", "validation_matches_candidate"}
            or result["state"] != state or result["targets_met"] is not False
            or (time_limited and result["termination_reason"] != "time_limit")
            or provenance["selection"] != selection
            or provenance["training_complete"] is not True or provenance[frozen_flag] is not True
            or type(provenance["fully_scored_evaluations"]) is not int
            or provenance["fully_scored_evaluations"] < 1
            or type(provenance["validation_matches_candidate"]) is not bool):
        raise ValueError("cancel candidate lacks a complete parent-committed provenance")
    _, request, _ = film_synthesis_request_from_payload({
        "schema_version": SCHEMA_VERSION, "kind": "coat_sim.film_synthesis_request",
        "request_id": value["request_id"], "request": result["frozen_request"], "physical_structures": []})
    if time_limited and request.max_seconds is None:
        raise ValueError("time-limit candidate requires an explicit frozen max_seconds")
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
    counters = [result[name] for name in ("cycles", "validation_evaluations", "failed_evaluations")]
    if (any(type(counter) is not int or counter < 0 for counter in counters)
            or result["validation_evaluations"] + provenance["fully_scored_evaluations"] > result["evaluations"]
            or result["failed_evaluations"] > result["evaluations"]
            or (request.max_evaluations is not None and result["evaluations"] > request.max_evaluations)
            or (request.max_cycles is not None and result["cycles"] > request.max_cycles)):
        raise ValueError("completed training candidate counters exceed its frozen budgets")
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
    if not complete_original_score(request, training):
        raise ValueError("cancel candidate lacks complete raw training coverage and enabled constraints")
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
                or quality["targets_met"] is not (nominal_targets_pass(request, training)
                    and nominal_targets_pass(request, validation, independent_audit=True))):
            raise ValueError("cancel candidate audit belongs to another design or quality")
        if not complete_original_score(request, validation, independent_audit=True):
            raise ValueError("cancel candidate lacks a complete saved independent audit")
        audit = validation["audit"]
        if (type(audit.get("physical_evaluations")) is not int or audit["physical_evaluations"] < 1
                or type(audit.get("wavelength_points")) is not int
                or audit["wavelength_points"] < audit["unique_condition_points"]):
            raise ValueError("cancel candidate saved audit work counters are invalid")
        _validate_objective_status(request, validation)
    elif (validation is not None or quality["audit_max_normalized_error"] is not None
            or quality["targets_met"] is not None or quality["continuous_certificate"] is not False):
        raise ValueError("unaudited cancel candidate must not advertise audit evidence")
    layers = value["review"]["layers"]
    for layer, reviewed in zip(design["front_layers"], layers, strict=True):
        if (reviewed["id"] != layer["id"] or reviewed["material_id"] != layer["material_id"]
                or reviewed["optical_set_id"] != layer["optical_set_id"]
                or not equal(reviewed["thickness_nm"], layer["thickness"]["value"])):
            raise ValueError("cancel candidate review layer differs from the frozen candidate")


def _validate_result_physical_binding(request, result: Mapping[str, Any]) -> None:
    """Bind saved designs to real frozen inputs, without TMM or re-auditing.

    This is deliberately a cheap live/cancellation boundary. Historical
    acceptance additionally needs the opt-in background physical replay below.
    """
    from .film_design import validate_design
    if type(result["design_revision"]) is not int or result["design_revision"] != request.design_revision:
        raise ValueError("synthesis result revision differs from its frozen request")
    request = freeze_film_synthesis_request(None, request)
    candidate = result["candidate_design"]
    if candidate is None:
        if result["training_score"] is not None or result["validation_score"] is not None:
            raise ValueError("synthesis result has scores without a candidate")
        return
    candidate = validate_design(candidate)
    physical_keys = ("substrate", "incident_medium", "exit_medium", "first_surface",
        "layer_order", "reference_wavelength_nm", "use_k", "assume_lossless")
    allowed = {(ref.material_id, ref.optical_set_id) for ref in request.candidate_materials}
    allowed.update((row["material_id"], row["optical_set_id"])
        for seed in request.seed_designs for row in seed["front_layers"])
    if (any(candidate[key] != request.environment[key] for key in physical_keys)
            or candidate["back_layers"] or not _feasible(request, candidate)
            or any(row["thickness"]["unit"] != "nm"
                or (row["material_id"], row["optical_set_id"]) not in allowed
                for row in candidate["front_layers"])):
        raise ValueError("synthesis candidate violates its frozen physical request")
    def physical_identity(design):
        return {key: item for key, item in validate_design(design).items()
                if key not in {"note", "summary"}}
    expected = physical_identity(candidate)
    for name in ("training_score", "validation_score"):
        score = result[name]
        if score is not None and physical_identity(score["design"]) != expected:
            raise ValueError("synthesis score belongs to another physical design")
    if not isinstance(result["training_score"], Mapping):
        raise ValueError("synthesis candidate has no complete training evidence")


def is_synthesis_result(value: Any) -> bool:
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or value.get("kind") != RESULT_KIND
            or value.get("acceptance_semantics") != ACCEPTANCE_SEMANTICS):
        return False
    result = value.get("result")
    review = value.get("review")
    candidate_audit = value.get("best_candidate_audit")
    if not isinstance(result, dict) or not isinstance(review, dict):
        return False
    if (not isinstance(candidate_audit, dict)
            or type(candidate_audit.get("available")) is not bool
            or candidate_audit.get("continuous_certificate") is not False
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
    if result.get("termination_reason") == "time_limit":
        if (result["state"] != "not_found" or result["targets_met"] is not False
                or "cancel_snapshot" in review):
            return False
        try:
            _, timed_request, _ = film_synthesis_request_from_payload({
                "schema_version": SCHEMA_VERSION, "kind": "coat_sim.film_synthesis_request",
                "request_id": value["request_id"], "request": result["frozen_request"], "physical_structures": []})
            if timed_request.max_seconds is None:
                return False
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return False
    try:
        _, acceptance_request, _ = film_synthesis_request_from_payload({
            "schema_version": SCHEMA_VERSION, "kind": "coat_sim.film_synthesis_request",
            "request_id": value["request_id"], "request": result["frozen_request"], "physical_structures": []})
        _validate_result_physical_binding(acceptance_request, result)
        audited = result["candidate_design"] is not None and result["validation_complete"]
        if audited and (not complete_original_score(acceptance_request, result.get("training_score"))
                or not complete_original_score(acceptance_request, result.get("validation_score"), independent_audit=True)):
            return False
        nominal = (nominal_targets_pass(acceptance_request, result.get("training_score"))
            and nominal_targets_pass(acceptance_request, result.get("validation_score"), independent_audit=True)
            if audited else None)
        acceptable = (solver_acceptable(acceptance_request, result.get("training_score"))
            and solver_acceptable(acceptance_request, result.get("validation_score"), independent_audit=True)
            if audited else None)
        def same_number(left, right):
            return (not isinstance(left, bool) and isinstance(left, (float, int))
                and math.isfinite(left) and math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-12))
        if audited:
            statuses = review.get("per_objective")
            if (not isinstance(statuses, list) or len(statuses) != len(acceptance_request.objectives)
                    or {row.get("objective_index") for row in statuses if isinstance(row, Mapping)}
                        != set(range(len(acceptance_request.objectives)))):
                return False
            if statuses != _validate_objective_status(acceptance_request, result["validation_score"]):
                return False
        if (candidate_audit["available"] is not audited
                or not same_number(candidate_audit.get("acceptable_normalized_error"), acceptance_request.acceptable_normalized_error)
                or (audited and (not same_number(candidate_audit.get("training_max_normalized_error"), result["training_score"]["max_error"])
                    or not same_number(candidate_audit.get("audit_max_normalized_error"), result["validation_score"]["max_error"])))
                or candidate_audit["targets_met"] is not nominal
                or candidate_audit.get("solver_acceptable") is not acceptable
                or result.get("solver_acceptable") is not acceptable
                or review.get("targets_met") is not result["targets_met"]
                or review.get("solver_acceptable") is not acceptable
                or result["targets_met"] is not (result["state"] == "completed" and nominal is True)
                or (result["state"] == "completed" and nominal is not True)):
            return False
    except (KeyError, TypeError, ValueError, ArithmeticError):
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
    if (result.get("termination_reason") == "time_limit"
            and (not isinstance(provenance, Mapping)
                 or provenance.get("selection") != "completed_training_at_time_limit")):
        return False
    if provenance:
        if not isinstance(provenance, Mapping):
            return False
        try:
            if provenance.get("selection") == "completed_training_at_time_limit":
                _validate_completed_time_limit_candidate(value)
            else:
                _validate_completed_cancel_candidate(value)
        except (KeyError, TypeError, ValueError, ArithmeticError, StopIteration):
            return False
        return True
    # Legacy results remain audited-only; a missing provenance flag must never
    # turn an arbitrary unaudited candidate into an applicable cancellation.
    return (result["validation_complete"] is True
            and isinstance(result.get("validation_score"), dict)
            and isinstance(result.get("training_score"), dict))


def is_legacy_synthesis_result(value: Any) -> bool:
    """Previous schemas and acceptance semantics are deliberately unsupported."""
    return False


def normalize_legacy_synthesis_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Retained UI entry point; accept valid current content only."""
    if not isinstance(value, Mapping) or value.get("acceptance_semantics") != ACCEPTANCE_SEMANTICS:
        raise ValueError("unsupported legacy synthesis result semantics; recreate with current band criteria")
    if not is_synthesis_result(value):
        raise ValueError("current synthesis result is invalid")
    return deepcopy(dict(value))


def replay_synthesis_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Physically replay current-schema frozen spectra in a background loader."""
    if not isinstance(value, Mapping) or not is_synthesis_result(value):
        raise ValueError("current synthesis result has invalid schema, semantics or physical identity")
    return _replay_synthesis_result(value)

def _replay_synthesis_result(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        body = value["result"]
        if not isinstance(body, Mapping) or set(body) - {item.name for item in fields(FilmSynthesisResult)}:
            raise ValueError("saved synthesis result body is invalid")
        if (body["state"] not in {"completed", "not_found", "cancelled", "failed"}
                or type(body["targets_met"]) is not bool or type(body["validation_complete"]) is not bool):
            raise ValueError("saved synthesis result state is invalid")
        request_id, request, _ = film_synthesis_request_from_payload({
            "schema_version": SCHEMA_VERSION, "kind": "coat_sim.film_synthesis_request",
            "request_id": value["request_id"], "request": body["frozen_request"], "physical_structures": []})
        # Preserve the parsed historical request as routing/source identity.
        # Resolving optional execution metadata on this host must not rewrite
        # old inputs (for example an absent resource_guard_resolution).
        execution_request = freeze_film_synthesis_request(None, request)
        _validate_result_physical_binding(execution_request, body)
        metadata = value.get("physical_structure_metadata", [])
        if not isinstance(metadata, list):
            raise ValueError("saved synthesis structure metadata is invalid")
        _validate_physical_structure_metadata(film_synthesis_request_to_payload(request_id, request), metadata)
        candidate = body["candidate_design"]
        training = validation = None
        replay_work = {"training_evaluations": 0, "validation_evaluations": 0,
                       "audit_physical_evaluations": 0, "audit_wavelength_points": 0}
        if candidate is not None:
            from .film_design import validate_design
            from .film_synthesis_audit import audit_frozen_synthesis_candidate
            candidate = validate_design(deepcopy(candidate))
            def physical_identity(design):
                return {key: item for key, item in validate_design(design).items()
                        if key not in {"note", "summary"}}

            training = evaluate_frozen_structure_candidate(_proxy(execution_request), candidate,
                derived_validation_grid=False)["score"]
            replay_work["training_evaluations"] = 1
            _verify_saved_score(request, body["training_score"], training)
            if body["validation_complete"]:
                audited = audit_frozen_synthesis_candidate(execution_request, candidate)
                validation = audited["score"]
                _verify_saved_score(request, body["validation_score"], validation, independent=True)
                replay_work.update(validation_evaluations=1,
                    audit_physical_evaluations=audited["audit"]["physical_evaluations"],
                    audit_wavelength_points=audited["audit"]["wavelength_points"])
            elif body["validation_score"] is not None:
                raise ValueError("saved synthesis incomplete audit has a score")
        elif (body["training_score"] is not None or body["validation_score"] is not None
                or body["validation_complete"] or body["targets_met"]):
            raise ValueError("saved synthesis result claims evidence without a candidate")
        nominal = (training is not None and validation is not None
            and nominal_targets_pass(request, training)
            and nominal_targets_pass(request, validation, independent_audit=True))
        data = deepcopy(dict(body))
        data.update(candidate_design=candidate, training_score=training, validation_score=validation,
            frozen_request=request, targets_met=body["state"] == "completed" and nominal,
            trace=tuple(FilmSynthesisTrace(**row) for row in body["trace"]))
        if body["state"] == "completed" and not nominal:
            data.update(state="not_found", termination_reason="replay_nominal_targets_not_met",
                message="Frozen physical replay did not satisfy the original nominal bounds.")
        upgraded = film_synthesis_result_to_payload(request_id, FilmSynthesisResult(**data),
            audit=value.get("audit"), physical_structure_metadata=metadata)
        # Retain routing/counter evidence, never a stale quality or approval flag.
        for key in ("execution_project_root", "work_counters"):
            if key in value:
                upgraded[key] = deepcopy(value[key])
        if "cancel_snapshot" in value.get("review", {}):
            upgraded["review"]["cancel_snapshot"] = deepcopy(value["review"]["cancel_snapshot"])
        upgraded["physical_replay_verification"] = {
            "read_only": True, "physical_replay_performed": True,
            "original_targets_met": body["targets_met"], "replay_work": replay_work}
        upgraded["review"]["warnings"].append(
            "Read-only history: "
            + "nominal and solver acceptance were recomputed by frozen physical replay; the original file was not changed.")
        if not is_synthesis_result(upgraded):
            raise ValueError("saved synthesis result cannot be safely normalized")
        return upgraded
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError) as exc:
        raise ValueError(f"saved synthesis result cannot be safely replayed: {exc}") from exc


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
                execution_project_root: str, *, candidate_observer=None, candidate_observer_factory=None):
    if isinstance(value, Mapping) and set(value) == {"payload"}:
        job = deepcopy(value["payload"])
        deserialize_job_payload(job)
    else:
        job = build_job_payload(value)
    request_id, request, metadata = deserialize_job_payload(job)
    bound_request = request  # Immutable persisted spec identity, before execution AUTO resolution.
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

    def search_checkpoint(state):
        # Separate exploration durability from quality evidence. Even a run
        # cancelled before any passing audit can resume its structural frontier.
        path = checkpoint_path.with_name(checkpoint_path.stem + '.search-state.json')
        write_json(path, _json_snapshot(state))

    monitor = threading.Thread(target=heartbeat, name="film-synthesis-heartbeat", daemon=True)
    monitor.start()
    try:
        canonical, generated_metadata = prepare_film_synthesis_payload(None, job["request"], progress_callback=lambda p: publish(p))
        request_id, request, _ = film_synthesis_request_from_payload(canonical)
        observer = (candidate_observer_factory(request) if candidate_observer_factory is not None
                    else candidate_observer)
        result = synthesize_film_design(None, request, cancel_event=cancel_event,
                                        progress_callback=progress, snapshot_callback=checkpoint,
                                        candidate_observer=observer,
                                        search_checkpoint_callback=search_checkpoint)
        # Older queued specs legitimately omit newly added execution controls.
        # Resolve them only for execution, never rewrite the original identity
        # or relax the worker/queue's exact frozen-request comparison.
        normalized = replace(result.frozen_request,
            parallel_batch_size=bound_request.parallel_batch_size,
            resource_guard_resolution=bound_request.resource_guard_resolution)
        if normalized != bound_request:
            raise ValueError("synthesis execution changed immutable job request inputs")
        resolved_controls = {
            "parallel_batch_size": result.frozen_request.parallel_batch_size,
            "resource_guard_resolution": deepcopy(result.frozen_request.resource_guard_resolution),
        }
        result = replace(result, frozen_request=bound_request,
            performance={**result.performance, "resolved_execution_controls": resolved_controls})
        retained_training = (
            result.candidate_provenance.get("training_complete") is True
            and ((result.state == "cancelled"
                  and result.candidate_provenance.get("selection") == "completed_training_at_cancel")
                 or (result.state == "not_found" and result.termination_reason == "time_limit"
                     and request.max_seconds is not None
                     and result.candidate_provenance.get("selection") == "completed_training_at_time_limit")))
        if not result.validation_complete and not retained_training:
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
