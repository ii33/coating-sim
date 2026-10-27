"""Local CPU scheduling boundary for explicit current-design A/B repair.

No optics are implemented here. Complete physical calls and independent audits
use the existing frozen evaluators; the worker owns budgets and retained data.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields, is_dataclass, replace
import json
import math
import sys
from time import monotonic
from typing import Any, Mapping

from .film_auto_synthesis import FilmSynthesisRequest, _proxy, freeze_film_synthesis_request
from .film_design import validate_design
from .film_optimization_structure import _StructureCancelled, evaluate_frozen_structure_candidate
from .film_synthesis_audit import EXTREMUM_MAXITER, audit_frozen_synthesis_candidate, objective_constraint_status
from .film_synthesis_io import film_synthesis_request_from_payload, film_synthesis_request_to_payload
from .film_synthesis_manufacturing import (
    ManufacturingRepairRequest, ManufacturingRepairResult, ManufacturingRouteResult,
    ManufacturingSnapshot, ManufacturingRepairStop, _audited_key, _owned_bytes,
    manufacturing_refinement_working_set_bytes, manufacturing_snapshot,
    manufacturing_source_fingerprint, prepare_manufacturing_routes, repair_manufacturing_pair, _physical_reason,
)

EXECUTOR = "film_manufacturing_repair"
REQUEST_KIND = "coat_sim.film_manufacturing_request"
JOB_KIND = "coat_sim.film_manufacturing_job"
RESULT_KIND = "coat_sim.film_manufacturing_result"
CHECKPOINT_KIND = "coat_sim.film_manufacturing_checkpoint"
SCHEMA_VERSION = 1
_REQUEST_FIELDS = {"schema_version", "kind", "synthesis", "source_design", "source_fingerprint",
    "defect_layer_ids", "reference_nm", "refinement_evaluations_per_route",
    "max_iterations_per_route", "seconds_per_route"}
_RESULT_FIELDS = {"schema_version", "kind", "request", "state", "is_final", "termination_reason", "result", "work"}


def _json(value: Any) -> Any:
    def default(item: Any) -> Any:
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        if hasattr(item, "item"):
            return item.item()
        raise TypeError("unsupported manufacturing JSON value")
    return json.loads(json.dumps(value, default=default, allow_nan=False))


def make_request_payload(service: Any, synthesis: FilmSynthesisRequest, source_design: Mapping[str, Any], *,
        defect_layer_ids, reference_nm: float, refinement_evaluations_per_route: int,
        max_iterations_per_route: int, seconds_per_route: float,
        request_id: str = "manufacturing-repair") -> dict[str, Any]:
    """Capture the current source and frozen optical input once, off the UI thread.

    The source is included as a real seed for material capture and physical-nm
    conversion. Its seed position is deterministic and does not start synthesis.
    """
    source = validate_design(deepcopy(dict(source_design)))
    synthesis = replace(synthesis, seed_designs=(source,))
    frozen = freeze_film_synthesis_request(getattr(service, "film_design_service", service), synthesis)
    source = deepcopy(dict(frozen.seed_designs[0]))
    value = {"schema_version": SCHEMA_VERSION, "kind": REQUEST_KIND,
        "synthesis": film_synthesis_request_to_payload(request_id, frozen), "source_design": source,
        "source_fingerprint": manufacturing_source_fingerprint(frozen, source),
        "defect_layer_ids": list(defect_layer_ids), "reference_nm": reference_nm,
        "refinement_evaluations_per_route": refinement_evaluations_per_route,
        "max_iterations_per_route": max_iterations_per_route, "seconds_per_route": seconds_per_route}
    request_from_payload(value)
    return _json(value)


def request_from_payload(value: Mapping[str, Any]) -> tuple[ManufacturingRepairRequest, dict[str, Any]]:
    if (not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS
            or type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION
            or value.get("kind") != REQUEST_KIND or not isinstance(value.get("defect_layer_ids"), list)):
        raise ValueError("invalid manufacturing request envelope")
    _json(value)
    _, synthesis, structures = film_synthesis_request_from_payload(value["synthesis"])
    if (structures or synthesis.forward_backend != "cpu" or synthesis.forward_device is not None
            or synthesis.cpu_workers != 1):
        raise ValueError("manufacturing repair supports the local single CPU evaluator only")
    # Revalidate frozen content, but never consult a live catalog or draw entropy.
    checked = freeze_film_synthesis_request(None, synthesis)
    if film_synthesis_request_to_payload(value["synthesis"]["request_id"], checked) != value["synthesis"]:
        raise ValueError("manufacturing synthesis request is not canonical and fully frozen")
    source = validate_design(deepcopy(value["source_design"]))
    if source != value["source_design"]:
        raise ValueError("manufacturing source must be normalized")
    request = ManufacturingRepairRequest(checked, value["source_fingerprint"], tuple(value["defect_layer_ids"]),
        value["reference_nm"], value["refinement_evaluations_per_route"], value["max_iterations_per_route"],
        value["seconds_per_route"])
    # Geometry/fingerprint validation performs no optical work and needs no score.
    prepare_manufacturing_routes(request, ManufacturingSnapshot(source, {}))
    return request, source


def canonical_parameters(service: Any, supplied: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(supplied, Mapping) or set(supplied) != {"payload"}:
        raise ValueError("manufacturing repair requires exactly one frozen payload")
    if isinstance(supplied['payload'], Mapping) and supplied['payload'].get('schema_version') == 2:
        from .film_automatic_repair_jobs import request_from_payload as automatic_request
        automatic_request(supplied['payload'])
    else:
        request_from_payload(supplied["payload"])
    return {"payload": _json(supplied["payload"])}


def build_job_payload(service: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(request, Mapping) and request.get('schema_version') == 2:
        from .film_automatic_repair_jobs import build_job_payload as automatic_job
        return automatic_job(service, request)
    frozen = canonical_parameters(service, {"payload": request})["payload"]
    return {"schema_version": SCHEMA_VERSION, "kind": JOB_KIND, "request": frozen}


def deserialize_job_payload(value: Mapping[str, Any]) -> tuple[ManufacturingRepairRequest, dict[str, Any]]:
    if (not isinstance(value, Mapping) or set(value) != {"schema_version", "kind", "request"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION
            or value.get("kind") != JOB_KIND):
        raise ValueError("invalid manufacturing job envelope")
    return request_from_payload(value["request"])


def _physical_chunk_size(design: Mapping[str, Any]) -> int:
    chunk = min(10001, 1_000_000 // (len(design["front_layers"]) + len(design["back_layers"]) + 3))
    if chunk < 1:
        raise ValueError("film evaluation chunk limit cannot satisfy the helper sample budget")
    return chunk


def _expanded_json_sizes(value: Any) -> tuple[int, int]:
    """Nonallocating upper sizes for alias-expanded JSON objects and ASCII text.

    Unlike _owned_bytes, repeated roles/keys are intentionally counted again.
    Container bases include 128 dictionary/64 list capacity bytes, then
    64 bytes/dict entry and 16 bytes/list item; scalar
    objects use their real Python size. ASCII escaping is at most 12 characters
    per Unicode code point (a surrogate pair), plus delimiters. No JSON/asdict
    projection is allocated to make this admission estimate.
    """
    if is_dataclass(value) and not isinstance(value, type):
        members = fields(value)
        owned, encoded = sys.getsizeof({}) + 128 + 64 * len(members), 2 + 4 * len(members)
        for field in members:
            a, b = _expanded_json_sizes(field.name)
            c, d = _expanded_json_sizes(getattr(value, field.name))
            owned, encoded = owned + a + c, encoded + b + d
        return owned, encoded
    if isinstance(value, Mapping):
        owned, encoded = sys.getsizeof({}) + 128 + 64 * len(value), 2 + 4 * len(value)
        for key, item in value.items():
            a, b = _expanded_json_sizes(key)
            c, d = _expanded_json_sizes(item)
            owned, encoded = owned + a + c, encoded + b + d
        return owned, encoded
    if isinstance(value, (tuple, list)):
        owned, encoded = sys.getsizeof([]) + 64 + 16 * len(value), 2 + 2 * len(value)
        for item in value:
            a, b = _expanded_json_sizes(item)
            owned, encoded = owned + a, encoded + b
        return owned, encoded
    if isinstance(value, str):
        return sys.getsizeof(value), 12 * len(value) + 2
    if value is None or isinstance(value, (bool, int, float)):
        return sys.getsizeof(value), max(32, len(str(value)))
    if hasattr(value, "item"):
        return _expanded_json_sizes(value.item())
    raise ValueError("manufacturing admission supports JSON scalars and records only")


def estimate_result_resources(request_payload: Mapping[str, Any], synthesis: FilmSynthesisRequest,
                              design: Mapping[str, Any], *, audit_physical_call_limit: int | None = None) -> dict[str, int]:
    """Reserve the *combined* terminal serialization/validation phase.

    Up to 7 unique snapshots expand into 9 training-score/design occurrences
    and 5 audit scores: source + 6 route roles + best + incumbent. The fixed
    score format bounds each 16-field condition by 4096 bytes, each raw point
    by 512 bytes, and per-angle/per-objective status containers by 8192 bytes.
    Both Python projection and encoded score text are bounded by these values.
    Design/request strings use measured Unicode-aware bounds instead.

    Peak = 128 MiB runtime + original core <= P + live JSON P + reconstructed snapshots <= P +
    return projection P + two encoded buffers J + two frozen context copies.
    Numerical arrays do not coexist with that final phase. Admission takes the
    maximum of this phase and numerical admission, adding real external owners.
    """
    points = sum(o.wavelength_samples * len(o.angles_deg) for o in synthesis.objectives)
    angles = sum(len(o.angles_deg) for o in synthesis.objectives)
    bands = len(synthesis.objectives)
    audit = (estimate_audit_resources(synthesis, design) if audit_physical_call_limit is None else
             estimate_audit_resources(synthesis, design, physical_call_limit=audit_physical_call_limit))
    dense = sum((1 if o.wavelength_samples == 1 else
        (o.wavelength_samples - 1) * synthesis.validation_samples_multiplier + 1) * len(o.angles_deg)
        for o in synthesis.objectives)
    training = 4096 * (points + angles + 1) + 512 * points + 8192 * (angles + bands)
    audited = (4096 * (audit["wavelength_points_upper_bound"] + angles + 1)
        + 512 * (audit["wavelength_points_upper_bound"] + dense)
        + 8192 * (angles + bands) + 32768)
    request_bytes, request_json = _expanded_json_sizes(request_payload)
    design_bytes, design_json = _expanded_json_sizes(design)
    # Nine role designs plus two prepared designs and conservatively six more
    # design-sized ID/bounds/diagnostic owners. No numerical derivative arrays.
    projection = request_bytes + 17 * design_bytes + 9 * training + 5 * audited + 128 * 1024
    encoded = request_json + 17 * design_json + 9 * training + 5 * audited + 128 * 1024
    return {"expanded_projection_bytes_upper_bound": projection,
        "encoded_json_bytes_upper_bound": encoded,
        "phase_working_set_bytes": 128 * 1024**2 + 4 * projection + 2 * encoded + 2 * request_bytes,
        "expanded_training_score_occurrences": 9, "expanded_audit_score_occurrences": 5}


def estimate_audit_resources(synthesis: FilmSynthesisRequest, design: Mapping[str, Any], *,
                             physical_call_limit: int | None = None) -> dict[str, int]:
    """Full dense/offset/extremum worst case, never just one TMM chunk.

    A bracket consumes at most EXTREMUM_MAXITER scalar calls. Both signs and
    every interior dense+offset node may bracket (an intentionally loose bound).
    Dense/offset evidence, scalar cache, error maps, final canonical reduction,
    three complete evidence projections, and copied frozen optics coexist.
    This project admission model is not an OS memory isolation guarantee.
    """
    dense = offset = scalar = calls = kernel_calls = 0
    chunk = _physical_chunk_size(design)
    for objective in synthesis.objectives:
        d = 1 if objective.wavelength_samples == 1 else (objective.wavelength_samples - 1) * synthesis.validation_samples_multiplier + 1
        a = len(objective.angles_deg)
        dense += d * a
        kernel_calls += ((d + chunk - 1) // chunk) * a
        if d <= 1 or objective.wavelength_min_nm == objective.wavelength_max_nm:
            continue
        offset += (d - 1) * a
        kernel_calls += ((d - 1 + chunk - 1) // chunk) * a
        calls += 1
        c = objective.pointwise
        sides = (2 if c.mode == "value" else int(c.lower_bound is not None) + int(c.upper_bound is not None)) if c.enabled else 0
        scalar += sides * max(0, 2 * d - 3) * a * EXTREMUM_MAXITER
    if physical_call_limit is not None:
        if type(physical_call_limit) is not int or physical_call_limit < 0:
            raise ValueError('audit physical call limit must be a nonnegative integer')
        # The automatic repair owner reserves every candidate/reference call.
        # Dense and offset buffers remain fully reserved; scalar extrema cannot
        # exceed the remaining whole physical-call allowance.
        scalar = min(scalar, max(0, physical_call_limit - 1 - calls))
    points = dense + offset + scalar
    layers = len(design["front_layers"])
    # At most one pointwise condition record per objective/angle/wavelength;
    # signed bounds produce residuals, not duplicate FilmConditionError rows.
    # Nine record owners cover auditor maps/cache, returned copies, canonical
    # validation and observer/checkpoint JSON projection. Mean-only still pays
    # for complete dense/offset raw coverage even with no scalar extrema.
    working = (128 * 1024**2 + 9 * 2048 * points + 384 * dense * (layers + 1)
               + 6 * _owned_bytes(synthesis, design))
    physical_upper = 1 + calls + scalar
    if physical_call_limit is not None:
        physical_upper = min(physical_upper, physical_call_limit)
    return {"physical_evaluations_upper_bound": physical_upper,
            "kernel_calls_upper_bound": kernel_calls + scalar,
            "wavelength_points_upper_bound": points, "working_set_bytes": working}


def estimate_resources(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(request_payload, Mapping) and request_payload.get('schema_version') == 2:
        from .film_automatic_repair_jobs import estimate_resources as automatic_resources
        return automatic_resources(request_payload)
    request, source = request_from_payload(request_payload)
    return {"source_training_calls": 1, "source_audit_calls": 1,
        "routes": 2, "training_calls_per_route_upper_bound": 1 + request.refinement_evaluations_per_route,
        "audit_calls_per_route_upper_bound": 1,
        "source_audit": estimate_audit_resources(request.synthesis, source),
        "combined_result_phase": estimate_result_resources(request_payload, request.synthesis, source),
        "shared_budget_unit": "complete candidate calls; one whole independent audit is one call, not matched physical work",
        "refinement_working_set_bytes": manufacturing_refinement_working_set_bytes(request.synthesis, len(source["front_layers"])),
        "seconds_per_route": request.seconds_per_route,
        "shared_candidate_call_limit": request.synthesis.max_evaluations,
        "shared_seconds_limit": request.synthesis.max_seconds,
        "memory_policy": deepcopy(request.synthesis.resource_guard_resolution),
        "note": "Conservative complete-work admission bounds, not runtime or continuous spectral certification."}


class _CancelBridge:
    """Existing TMM/auditor is_set checks call the live route/global checker."""
    def __init__(self, check):
        self.check = check

    def is_set(self) -> bool:
        self.check()
        return False


def _envelope(job, result, reason, work, *, state=None, resources=None):
    if state is None:
        state = "cancelled" if reason == "cancelled" else "completed" if reason == "complete" else "failed"
    raw = {"schema_version": SCHEMA_VERSION, "kind": RESULT_KIND, "request": job["request"],
        "state": state, "is_final": True, "termination_reason": reason, "result": result, "work": work}
    if resources is not None:
        projection, encoded = _expanded_json_sizes(raw)
        if (projection > resources["expanded_projection_bytes_upper_bound"]
                or encoded > resources["encoded_json_bytes_upper_bound"]):
            raise ValueError("manufacturing combined output exceeded its pre-admitted format bound")
    return _json(raw)


def execute_job(parameters, cancel_event, report, checkpoint_path, write_json, *,
                external_retained_bytes=lambda: 0):
    """Execute off the UI thread; callers declare any additional retained owners.

    The durable worker passes its parsed spec/canonical JSON and queue record.
    Default is only for direct callers with no additional retained data owner.
    """
    if not isinstance(parameters, Mapping) or set(parameters) != {"payload"}:
        raise ValueError("manufacturing executor requires one frozen job")
    if isinstance(parameters['payload'], Mapping) and parameters['payload'].get('schema_version') == 2:
        from .film_automatic_repair_jobs import execute_job as automatic_execute
        return automatic_execute(parameters, cancel_event, report, checkpoint_path, write_json,
            external_retained_bytes=external_retained_bytes)
    job = deepcopy(parameters["payload"])
    request, source_design = deserialize_job_payload(job)
    synthesis = request.synthesis
    started = monotonic()
    assert synthesis.resource_guard_resolution is not None
    limit = synthesis.resource_guard_resolution["effective_bytes"]
    work: dict[str, Any] = {"admitted_candidate_calls": 0, "completed_training_calls": 0, "completed_audit_calls": 0,
        "completed_audit_physical_evaluations": 0, "completed_audit_wavelength_points": 0,
        "completed_kernel_F_calls": 0, "completed_kernel_FJ_calls": 0,
        "completed_training_wavelength_points": 0, "completed_film_layer_wavelength_points": 0,
        "reserved_kernel_calls_upper_bound": 0,
        "reserved_physical_evaluations_upper_bound": 0, "reserved_wavelength_points_upper_bound": 0,
        "peak_estimated_bytes": 0, "partial_physical_work_possible": False,
        "external_retained_ownership": "adapter parameters/job/ledger/source temporaries plus caller-declared retained bytes; no archive/parallel array owner",
        "shared_budget_unit": "complete candidate calls; one whole audit counts as one; physical work is reported separately"}
    source_training = source_audit = None
    source_snapshot = None
    pair_active = False
    output_resources = estimate_result_resources(job["request"], synthesis, source_design)
    result: ManufacturingRepairResult | None = None

    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise ManufacturingRepairStop("cancelled")
        if synthesis.max_seconds is not None and monotonic() - started >= synthesis.max_seconds:
            raise ManufacturingRepairStop("time_limit")

    def external_bytes():
        # The local IPC worker has no GUI cache, parallel worker or archive.
        # Every retained adapter-owned input/score owner is declared explicitly.
        retained = external_retained_bytes()
        if type(retained) is not int or retained < 0:
            raise ValueError("external_retained_bytes must return a nonnegative integer")
        return retained + _owned_bytes(parameters, job, work, output_resources, source_design,
            source_training, source_audit, None if pair_active else request)

    def admit(*, phase, route, design, jacobian, estimated_bytes):
        check()
        if synthesis.max_evaluations is not None and work["admitted_candidate_calls"] >= synthesis.max_evaluations:
            raise ManufacturingRepairStop("shared_budget")
        estimated_bytes = max(estimated_bytes, output_resources["phase_working_set_bytes"] + external_bytes())
        work["peak_estimated_bytes"] = max(work["peak_estimated_bytes"], estimated_bytes)
        if limit is not None and estimated_bytes > limit:
            raise ManufacturingRepairStop("resource_guard")
        if phase == "audit":
            reserve = estimate_audit_resources(synthesis, design)
        else:
            chunk = _physical_chunk_size(design)
            reserve = {"physical_evaluations_upper_bound": 1,
                "kernel_calls_upper_bound": sum(((o.wavelength_samples + chunk - 1) // chunk) * len(o.angles_deg) for o in synthesis.objectives),
                "wavelength_points_upper_bound": sum(o.wavelength_samples * len(o.angles_deg) for o in synthesis.objectives)}
        work["admitted_candidate_calls"] += 1
        work["reserved_physical_evaluations_upper_bound"] += reserve["physical_evaluations_upper_bound"]
        work["reserved_kernel_calls_upper_bound"] += reserve["kernel_calls_upper_bound"]
        work["reserved_wavelength_points_upper_bound"] += reserve["wavelength_points_upper_bound"]
        work["peak_estimated_bytes"] = max(work["peak_estimated_bytes"], estimated_bytes)
        report({"stage": "manufacturing:" + route + ":" + phase,
            "evaluations": work["admitted_candidate_calls"], "layer_count": len(design["front_layers"])})
        check()

    def evaluate(design, *, jacobian, cancel_check):
        cancel_check()
        work["partial_physical_work_possible"] = True
        value = evaluate_frozen_structure_candidate(_proxy(synthesis), design,
            variable_ids=tuple(r["id"] for r in design["front_layers"]) if jacobian else (),
            with_jacobian=jacobian, cancel_event=_CancelBridge(cancel_check))
        work["partial_physical_work_possible"] = False
        work["completed_training_calls"] += 1
        points = sum(o.wavelength_samples * len(o.angles_deg) for o in synthesis.objectives)
        work["completed_training_wavelength_points"] += points
        work["completed_film_layer_wavelength_points"] += points * len(design["front_layers"])
        chunk = _physical_chunk_size(design)
        kernel_calls = sum(((o.wavelength_samples + chunk - 1) // chunk) * len(o.angles_deg) for o in synthesis.objectives)
        work["completed_kernel_FJ_calls" if jacobian and design["front_layers"] else "completed_kernel_F_calls"] += kernel_calls
        return value

    def audit(design, *, cancel_check):
        cancel_check()
        work["partial_physical_work_possible"] = True
        last = [0, 0]
        def progress(item):
            counts = [item["physical_evaluations"], item["wavelength_points"]]
            completed = counts[0] - last[0]
            if completed:
                chunk = _physical_chunk_size(design)
                if item["stage"] == "audit_dense":
                    kernels = sum(((1 if o.wavelength_samples == 1 else (o.wavelength_samples - 1) * synthesis.validation_samples_multiplier + 1) + chunk - 1) // chunk * len(o.angles_deg) for o in synthesis.objectives)
                elif item["stage"] == "audit_offset":
                    o = synthesis.objectives[item["objective_index"]]
                    kernels = (((o.wavelength_samples - 1) * synthesis.validation_samples_multiplier + chunk - 1) // chunk) * len(o.angles_deg)
                else:
                    kernels = 1
                work["completed_kernel_F_calls"] += kernels
            work["completed_audit_physical_evaluations"] += completed
            work["completed_audit_wavelength_points"] += counts[1] - last[1]
            work["completed_film_layer_wavelength_points"] += (counts[1] - last[1]) * len(design["front_layers"])
            last[:] = counts
            report({"stage": "manufacturing:" + item["stage"],
                "physical_evaluations": counts[0], "wavelength_points": counts[1]})
        value = audit_frozen_synthesis_candidate(synthesis, design,
            cancel_event=_CancelBridge(cancel_check), progress_callback=progress)
        work["partial_physical_work_possible"] = False
        work["completed_audit_calls"] += 1
        return value

    def save(payload):
        if not result_matches_job(payload, job):
            raise ValueError("manufacturing checkpoint differs from the frozen job")
        write_json(checkpoint_path, {"schema_version": 1, "kind": CHECKPOINT_KIND, "job": job, "result": payload})

    try:
        check()
        training_bytes = manufacturing_refinement_working_set_bytes(synthesis, len(source_design["front_layers"])) + external_bytes()
        admit(phase="source", route="source", design=source_design, jacobian=False, estimated_bytes=training_bytes)
        source_training = evaluate(source_design, jacobian=False, cancel_check=check)
        source_snapshot = manufacturing_snapshot(synthesis, source_training)
        check()
        audit_bytes = estimate_audit_resources(synthesis, source_design)["working_set_bytes"] + external_bytes() + _owned_bytes(source_snapshot)
        admit(phase="audit", route="source", design=source_design, jacobian=False, estimated_bytes=audit_bytes)
        source_audit = audit(source_design, cancel_check=check)
        # Admission already includes complete score validation/copy temporaries.
        source_snapshot = manufacturing_snapshot(synthesis, source_training, audit_value=source_audit)
        source_training = source_audit = None
        interim = ManufacturingRepairResult("cancelled", source_snapshot, (), None, source_snapshot,
            work["peak_estimated_bytes"], limit, monotonic() - started)
        save(_envelope(job, interim, "source_audited_checkpoint", work, state="cancelled", resources=output_resources))
        del interim
        pair_active = True
        result = repair_manufacturing_pair(request, source_snapshot, evaluate=evaluate, audit=audit,
            audit_working_set_bytes=lambda design: estimate_audit_resources(synthesis, design)["working_set_bytes"],
            admit=admit, cancel_check=check, external_retained_bytes=external_bytes,
            progress=lambda stage, snapshot, metadata: report({"stage": "manufacturing:" + stage,
                "route": metadata["route"], "max_error": snapshot.training_score["max_error"],
                "nominal_pass": snapshot.targets_met}))
        result = replace(result, source_validated=True, estimated_peak_bytes=max(result.estimated_peak_bytes, work["peak_estimated_bytes"]),
                         elapsed_seconds=monotonic() - started)
        payload = _envelope(job, result, result.status, work, resources=output_resources)
    except (_StructureCancelled, ManufacturingRepairStop) as error:
        reason = error.reason if isinstance(error, ManufacturingRepairStop) else "cancelled"
        result = (ManufacturingRepairResult(reason, source_snapshot, (), None, source_snapshot,
            work["peak_estimated_bytes"], limit, monotonic() - started) if source_snapshot is not None else None)
        payload = _envelope(job, result, reason, work, resources=output_resources)
    if not result_matches_job(payload, job):
        raise ValueError("manufacturing result differs from the frozen job")
    if payload["result"] is not None:
        save(payload)
    return payload


def _snapshot(raw, request):
    if not isinstance(raw, Mapping) or set(raw) != {"design", "training_score", "audit_score"}:
        raise ValueError("invalid manufacturing snapshot")
    if validate_design(deepcopy(raw["design"])) != raw["design"]:
        raise ValueError("manufacturing snapshot design must be normalized")
    result = manufacturing_snapshot(request.synthesis, {"design": raw["design"], "score": raw["training_score"]},
        audit_value=None if raw["audit_score"] is None else {"design": raw["design"], "score": raw["audit_score"]})
    if raw["audit_score"] is not None:
        expected = objective_constraint_status(request.synthesis.objectives, raw["audit_score"]["errors"],
                                               request.synthesis.acceptable_normalized_error)
        if raw["audit_score"].get("audit", {}).get("objective_status") != expected:
            raise ValueError("audit band status differs from original evidence")
    return result


def validate_result_payload(value: Mapping[str, Any], *, expected_job=None, replay=False) -> dict[str, Any]:
    """Validate identity, original evidence and topology; replay is explicit only.

    A scheduler completion/cancellation never starts physical replay. Callers
    requesting replay receive a fresh independent check before applying history.
    """
    if isinstance(value, Mapping) and value.get('schema_version') == 2:
        from .film_automatic_repair_jobs import validate_result_payload as automatic_validate
        return automatic_validate(value, expected_job=expected_job, replay=replay)
    if (not isinstance(value, Mapping) or set(value) != _RESULT_FIELDS
            or type(value.get("schema_version")) is not int or value["schema_version"] not in (1, 2)
            or value.get("kind") != RESULT_KIND or value.get("is_final") is not True
            or value.get("state") not in ("completed", "failed", "cancelled")
            or not isinstance(value.get("termination_reason"), str) or not isinstance(value.get("work"), Mapping)):
        raise ValueError("invalid manufacturing result envelope")
    reasons = {"complete": "completed", "cancelled": "cancelled", "shared_budget": "failed",
        "time_limit": "failed", "resource_guard": "failed", "source_audited_checkpoint": "cancelled",
        "scheduler_cancel_won": "cancelled", "scheduler_failed": "failed"}
    if reasons.get(value["termination_reason"]) != value["state"]:
        raise ValueError("manufacturing terminal state/reason mismatch")
    ledger = value["work"]
    counters = {"admitted_candidate_calls", "completed_training_calls", "completed_audit_calls",
        "completed_audit_physical_evaluations", "completed_audit_wavelength_points",
        "completed_kernel_F_calls", "completed_kernel_FJ_calls", "reserved_kernel_calls_upper_bound",
        "completed_training_wavelength_points", "completed_film_layer_wavelength_points",
        "reserved_physical_evaluations_upper_bound", "reserved_wavelength_points_upper_bound", "peak_estimated_bytes"}
    if (set(ledger) != counters | {"partial_physical_work_possible", "external_retained_ownership", "shared_budget_unit"}
            or any(type(ledger[name]) is not int or ledger[name] < 0 for name in counters)
            or type(ledger["partial_physical_work_possible"]) is not bool
            or not isinstance(ledger["external_retained_ownership"], str)
            or ledger["shared_budget_unit"] != "complete candidate calls; one whole audit counts as one; physical work is reported separately"
            or ledger["completed_training_calls"] + ledger["completed_audit_calls"] > ledger["admitted_candidate_calls"]
            or ledger["completed_kernel_F_calls"] + ledger["completed_kernel_FJ_calls"] > ledger["reserved_kernel_calls_upper_bound"]):
        raise ValueError("invalid manufacturing whole-work ledger")
    request, source_design = request_from_payload(value["request"])
    if expected_job is not None:
        deserialize_job_payload(expected_job)
        if expected_job["request"] != value["request"]:
            raise ValueError("manufacturing result belongs to another frozen job")
    body = value["result"]
    if body is None:
        if value["state"] == "completed":
            raise ValueError("completed manufacturing result has no source evidence")
        return _json(value)
    if not isinstance(body, Mapping) or set(body) != {f.name for f in fields(ManufacturingRepairResult)}:
        raise ValueError("invalid manufacturing result body")
    assert request.synthesis.resource_guard_resolution is not None
    if (body["status"] not in ("complete", "cancelled", "shared_budget", "time_limit", "resource_guard")
            or type(body["estimated_peak_bytes"]) is not int or body["estimated_peak_bytes"] < 0
            or body["effective_limit_bytes"] != request.synthesis.resource_guard_resolution["effective_bytes"]
            or isinstance(body["elapsed_seconds"], bool) or not isinstance(body["elapsed_seconds"], (int, float))
            or not math.isfinite(body["elapsed_seconds"]) or body["elapsed_seconds"] < 0):
        raise ValueError("invalid manufacturing resource/termination diagnostics")
    if value["termination_reason"] not in ("source_audited_checkpoint", "scheduler_cancel_won", "scheduler_failed") and body["status"] != value["termination_reason"]:
        raise ValueError("manufacturing algorithm status differs from terminal reason")
    source = _snapshot(body["source"], request)
    if source.design != source_design or body["source_validated"] is not True:
        raise ValueError("manufacturing source evidence differs from frozen input")
    prepared = prepare_manufacturing_routes(request, source)
    routes = body["routes"]
    if not isinstance(routes, list) or len(routes) not in (0, 2):
        raise ValueError("manufacturing result requires ordered A/B routes or a pre-route stop")
    if request.synthesis.max_evaluations is not None and ledger["admitted_candidate_calls"] > request.synthesis.max_evaluations:
        raise ValueError("manufacturing worker exceeded shared candidate call limit")
    if source.audit_score is None and routes:
        raise ValueError("manufacturing routes require complete independent source audit")
    audited = []
    snapshots = [source]
    for raw, initial in zip(routes, prepared, strict=False):
        if not isinstance(raw, Mapping) or set(raw) != {f.name for f in fields(ManufacturingRouteResult)}:
            raise ValueError("invalid manufacturing route result")
        for name in ("route", "design", "lower_bounds_nm", "removed_ids", "merged_ids"):
            if _json(raw[name]) != _json(getattr(initial, name)):
                raise ValueError("manufacturing route topology or physical guard changed")
        if raw["status"] not in {"prepared", "audited", "route_time_limit", "cancelled", "shared_budget", "time_limit", "resource_guard", initial.status} or raw["status"] == "running":
            raise ValueError("invalid manufacturing terminal route status")
        if initial.status != "prepared" and raw["status"] != initial.status:
            raise ValueError("infeasible manufacturing route changed state")
        if raw["start"] is not None and raw["start"]["design"] != initial.design:
            raise ValueError("manufacturing start evidence differs from prepared route")
        for name in ("start_calls", "refinement_calls", "audit_calls", "completed_training_calls", "completed_audit_calls"):
            if type(raw[name]) is not int or raw[name] < 0:
                raise ValueError("manufacturing call counters must be nonnegative integers")
        if (raw["start_calls"] > 1 or raw["refinement_calls"] > request.refinement_evaluations_per_route
                or raw["audit_calls"] > 1 or raw["completed_training_calls"] > raw["start_calls"] + raw["refinement_calls"]
                or raw["completed_audit_calls"] > raw["audit_calls"]):
            raise ValueError("manufacturing route exceeds its explicit budget")
        if (raw["status"] == "audited" and (raw["champion"] is None
                or raw["champion"]["audit_score"] is None or raw["completed_audit_calls"] != 1)):
            raise ValueError("audited route has no completed independent evidence")
        if not raw["completed_training_calls"] and any(raw[role] is not None for role in ("start", "current", "champion")):
            raise ValueError("manufacturing route claims uncompleted trial evidence")
        if raw["completed_training_calls"] and any(raw[role] is None for role in ("start", "current", "champion")):
            raise ValueError("complete trial lost its retained evidence roles")
        for role in ("start", "current", "champion"):
            if raw[role] is None:
                continue
            snapshot = _snapshot(raw[role], request)
            layers = snapshot.design["front_layers"]
            if [(r["id"], r["material_id"], r["optical_set_id"]) for r in layers] != [
                    (r["id"], r["material_id"], r["optical_set_id"]) for r in initial.design["front_layers"]]:
                raise ValueError("manufacturing trial changed fixed route topology")
            if _physical_reason(request.synthesis, snapshot.design, initial.lower_bounds_nm) is not None:
                raise ValueError("manufacturing trial violates explicit physical guards")
            from .film_synthesis_archive import physical_design_hash
            actual_env, expected_env = deepcopy(dict(snapshot.design)), deepcopy(dict(initial.design))
            actual_env["front_layers"] = expected_env["front_layers"] = []
            if physical_design_hash(actual_env) != physical_design_hash(expected_env):
                raise ValueError("manufacturing trial changed the frozen physical environment")
            snapshots.append(snapshot)
            if role == "champion" and snapshot.audit_score is not None:
                audited.append(snapshot)
    if (ledger["completed_training_calls"] != 1 + sum(r["completed_training_calls"] for r in routes)
            or ledger["completed_audit_calls"] != int(source.audit_score is not None) + sum(r["completed_audit_calls"] for r in routes)):
        raise ValueError("manufacturing retained evidence differs from completed call ledger")
    best = min(audited, key=_audited_key, default=None)
    if _json(best) != body["best_repaired"]:
        raise ValueError("manufacturing best repaired selection differs from audited original score")
    incumbent = best if best is not None and source.audit_score is not None and _audited_key(best) < _audited_key(source) else source
    if _json(incumbent) != body["spectral_incumbent"]:
        raise ValueError("manufacturing spectral incumbent did not preserve the original audited champion")
    if value["state"] == "completed" and (value["termination_reason"] != "complete" or body["status"] != "complete"):
        raise ValueError("manufacturing completion reason mismatch")
    if replay:
        seen = set()
        for snapshot in snapshots:
            if snapshot.physical_hash in seen:
                continue
            seen.add(snapshot.physical_hash)
            fresh = evaluate_frozen_structure_candidate(_proxy(request.synthesis), snapshot.design)
            if _json(manufacturing_snapshot(request.synthesis, dict(fresh)).training_score) != _json(snapshot.training_score):
                raise ValueError("manufacturing historical training replay differs")
            if snapshot.audit_score is not None:
                fresh_audit = audit_frozen_synthesis_candidate(request.synthesis, snapshot.design)
                if _json(manufacturing_snapshot(request.synthesis, dict(fresh), audit_value=fresh_audit).audit_score) != _json(snapshot.audit_score):
                    raise ValueError("manufacturing historical audit replay differs")
    return _json(value)


def result_matches_job(value: Mapping[str, Any], job: Mapping[str, Any]) -> bool:
    try:
        validate_result_payload(value, expected_job=job, replay=False)
        return True
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError):
        return False


def is_manufacturing_result(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("kind") != RESULT_KIND:
        return False
    try:
        validate_result_payload(value, replay=False)
        return True
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError):
        return False


def terminal_reply(value: Mapping[str, Any], state: str) -> dict[str, Any]:
    if state not in ("failed", "cancelled"):
        raise ValueError("manufacturing terminal reply must be failed or cancelled")
    saved = validate_result_payload(value, replay=False)
    if saved["state"] != state:
        saved["state"] = state
        saved["termination_reason"] = "scheduler_cancel_won" if state == "cancelled" else "scheduler_failed"
        if saved.get('schema_version') == 2:
            saved['result']['termination'] = saved['termination_reason']
            saved['result']['routes'][0]['status'] = saved['termination_reason']
    return saved


def validate_checkpoint(value: Mapping[str, Any], *, expected_job, terminal_state="cancelled") -> dict[str, Any]:
    if (not isinstance(value, Mapping) or set(value) != {"schema_version", "kind", "job", "result"}
            or type(value.get("schema_version")) is not int or value["schema_version"] not in (1, 2)
            or value.get("kind") != CHECKPOINT_KIND or value.get("job") != expected_job):
        raise ValueError("manufacturing checkpoint is not bound to the exact frozen job")
    validated = validate_result_payload(value["result"], expected_job=expected_job, replay=False)
    return terminal_reply(validated, terminal_state)
