"""Typed scheduling adapters for independent ideal and error-table workflows.

The legacy explicit-scenario request retains its own interpretation. New
workflows never invent a process model or derive optimizer weights from a table.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
from importlib import import_module
from typing import Any, Mapping


WORKFLOW_KINDS = ("film_ideal_sensitivity", "film_thickness_error_table")
_ALL_KINDS = (*WORKFLOW_KINDS, "film_sensitivity")


def _api(kind: str):
    if kind not in _ALL_KINDS:
        raise ValueError("unknown independent sensitivity workflow")
    return import_module("coat_sim.core." + kind)


def _for_payload(payload: Mapping[str, Any], suffix: str) -> str:
    if not isinstance(payload, Mapping):
        raise ValueError("workflow payload must be an object")
    for kind in _ALL_KINDS:
        if payload.get("kind") == "coat_sim." + kind + "_" + suffix:
            return kind
    raise ValueError("unknown independent sensitivity " + suffix + " kind")


def workflow_for_request(request: Any) -> str:
    if isinstance(request, Mapping):
        return _for_payload(request, "request")
    for kind in _ALL_KINDS:
        # Exact class ownership is a dispatch hint only; the typed freeze below
        # is the validation boundary and rejects unknown/inconsistent fields.
        if type(request).__module__ == "coat_sim.core." + kind:
            return kind
    raise ValueError("request is not an independent sensitivity request")


def workflow_request_from_payload(payload: Mapping[str, Any]):
    kind = _for_payload(payload, "request")
    return getattr(_api(kind), kind + "_request_from_payload")(payload)


def workflow_request_to_payload(request: Any) -> dict[str, Any]:
    kind = workflow_for_request(request)
    return getattr(_api(kind), kind + "_request_to_payload")(request)


def freeze_workflow_request(service: Any, request: Any):
    if isinstance(request, Mapping):
        request = workflow_request_from_payload(request)
    kind = workflow_for_request(request)
    return getattr(_api(kind), "freeze_" + kind + "_request")(
        getattr(service, "film_design_service", service), request)


def workflow_result_from_payload(payload: Mapping[str, Any]):
    kind = _for_payload(payload, "result")
    module = _api(kind)
    if kind in WORKFLOW_KINDS:
        # Product routing must enforce semantic state/identity gates, not only
        # reconstruct a dataclass. Full physical replay still occurs before
        # displaying/applying a historical result.
        payload = getattr(module, "validate_" + kind + "_result_payload")(payload, replay=False)
    return getattr(module, kind + "_result_from_payload")(payload)


def workflow_for_result(payload: Mapping[str, Any]) -> str:
    return _for_payload(payload, "result")


def workflow_result_to_payload(result: Any) -> dict[str, Any]:
    kind = workflow_for_request(result.frozen_request)
    return getattr(_api(kind), kind + "_result_to_payload")(result)


def validate_workflow_result(payload: Mapping[str, Any], *, expected_request=None,
                             replay: bool = True) -> dict[str, Any]:
    kind = _for_payload(payload, "result")
    if expected_request is not None and workflow_for_request(expected_request) != kind:
        raise ValueError("result workflow differs from its frozen request")
    return getattr(_api(kind), "validate_" + kind + "_result_payload")(
        payload, expected_request=expected_request, replay=replay)


def estimate_workflow_resources(request: Any) -> dict[str, Any]:
    kind = workflow_for_request(request)
    return getattr(_api(kind), "estimate_" + kind + "_resources")(request)


def prepare_workflow_payload(service: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    return workflow_request_to_payload(freeze_workflow_request(service, payload))


def canonical_parameters(service: Any, supplied: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(supplied, Mapping) or set(supplied) != {"payload"}:
        raise ValueError("independent sensitivity requires exactly one typed payload")
    return {"payload": prepare_workflow_payload(service, supplied["payload"])}


def build_job_payload(service: Any, request: Any) -> dict[str, Any]:
    frozen = freeze_workflow_request(service, request)
    kind = workflow_for_request(frozen)
    if kind not in WORKFLOW_KINDS:
        raise ValueError("legacy scenario jobs retain their original adapter")
    return {"schema_version": 1, "kind": "coat_sim." + kind + "_job",
            "request": workflow_request_to_payload(frozen)}


def deserialize_job_payload(value: Mapping[str, Any]):
    if (not isinstance(value, Mapping) or set(value) != {"schema_version", "kind", "request"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
        raise ValueError("invalid independent sensitivity job envelope")
    request = workflow_request_from_payload(value["request"])
    kind = workflow_for_request(request)
    if kind not in WORKFLOW_KINDS or value["kind"] != "coat_sim." + kind + "_job":
        raise ValueError("independent sensitivity job kind differs from request")
    if request.frozen_material_snapshot is None:
        raise ValueError("sensitivity worker requires frozen material data")
    return freeze_workflow_request(None, request)


def result_matches_job(value: Mapping[str, Any], job: Mapping[str, Any]) -> bool:
    try:
        result = workflow_result_from_payload(value)
        request = deserialize_job_payload(job)
        # This executor runs optimization; the independent core analysis API
        # shares its request type but cannot stand in for that operation.
        if workflow_for_request(request) == "film_ideal_sensitivity" and result.operation != "optimize":
            return False
        return workflow_request_to_payload(result.frozen_request) == workflow_request_to_payload(request)
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return False


def is_workflow_result(value: Any) -> bool:
    try:
        return _for_payload(value, "result") in WORKFLOW_KINDS and workflow_result_from_payload(value).is_final is True
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return False


def terminal_reply(value: Mapping[str, Any], state: str) -> dict[str, Any]:
    if _for_payload(value, "result") == "film_sensitivity":
        from .film_sensitivity_jobs import terminal_reply as legacy_terminal_reply
        return legacy_terminal_reply(value, state)
    if state not in ("failed", "cancelled"):
        raise ValueError("terminal sensitivity reply requires failed or cancelled")
    result = workflow_result_from_payload(value)
    if result.state == state and result.is_final:
        # Preserve the algorithm's actual failure/cancellation reason. A queue
        # transition must not relabel an initial nominal rejection as an IPC or
        # scheduler failure merely because both states are called "failed".
        return workflow_result_to_payload(result)
    return workflow_result_to_payload(replace(result, state=state, is_final=True,
        termination_reason="scheduler_cancel_won" if state == "cancelled" else "scheduler_failed",
        message="Scheduler reported " + state + "; any retained complete analysis is read-only."))


def validate_checkpoint(value: Mapping[str, Any], *, expected_job: Mapping[str, Any],
                        terminal_state: str = "cancelled") -> dict[str, Any]:
    request = deserialize_job_payload(expected_job)
    kind = workflow_for_request(request)
    if (not isinstance(value, Mapping) or set(value) != {"schema_version", "kind", "job", "result"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value["kind"] != "coat_sim." + kind + "_checkpoint"
            or value["job"] != expected_job or not result_matches_job(value["result"], expected_job)):
        raise ValueError("sensitivity checkpoint is not bound to the exact frozen job")
    validated = validate_workflow_result(value["result"], expected_request=request, replay=True)
    return terminal_reply(validated, terminal_state)


def execute_job(parameters, cancel_event, report, checkpoint_path, write_json):
    if not isinstance(parameters, Mapping) or set(parameters) != {"payload"}:
        raise ValueError("sensitivity executor requires one frozen job")
    job = deepcopy(parameters["payload"])
    request = deserialize_job_payload(job)
    kind = workflow_for_request(request)

    def progress(item):
        body = asdict(item) if is_dataclass(item) and not isinstance(item, type) else dict(item)
        body["stage"] = kind + ":" + str(body.get("stage", "running"))
        report(body)

    def checkpoint(payload):
        if not result_matches_job(payload, job):
            raise ValueError("sensitivity checkpoint request changed during execution")
        write_json(checkpoint_path, {"schema_version": 1,
            "kind": "coat_sim." + kind + "_checkpoint", "job": job, "result": payload})

    result = getattr(_api(kind), "execute_" + kind + "_request")(
        None, request, cancel_event=cancel_event, progress_callback=progress,
        checkpoint_callback=checkpoint)
    payload = workflow_result_to_payload(result)
    if not result_matches_job(payload, job):
        raise ValueError("sensitivity result differs from its frozen job")
    return payload


def submit_workflow(service: Any, payload: Mapping[str, Any], client_request_id: str | None = None):
    from .compute_tasks import ComputeTaskStore, digest
    from coat_sim.scheduler.submission import submit_confirmed

    kind = workflow_for_request(payload)
    if kind not in WORKFLOW_KINDS:
        raise ValueError("legacy scenario submission retains its original adapter")
    values = canonical_parameters(service, {"payload": payload})
    identity = digest({"kind": kind, "parameters": values, "input_files": {}})
    if client_request_id is None:
        client_request_id = kind + ":" + identity
    if not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 512:
        raise ValueError("client_request_id must contain 1 to 512 characters")
    store = ComputeTaskStore(service.root)
    prior = store.find_request(client_request_id)
    if prior is not None and prior["input_digest"] != identity:
        raise ValueError("request_id_conflict: sensitivity inputs changed")
    if prior is None:
        key = store.claim_unbound_estimate(client_request_id, identity)
        prior = store.read_estimate(key) if key is not None else None
    if prior is None:
        return submit_confirmed(service, kind, values, client_request_id)
    if prior["state"] in {"estimated", "pending"}:
        return {**store.public_estimate(prior),
            "state": "prepared" if prior["state"] == "estimated" else "awaiting_confirmation",
            "needs_release_request": prior["state"] == "estimated",
            "needs_user_confirmation": prior.get("release_policy") == "manual",
            "next_action": "request_compute_confirmation"}
    return submit_confirmed(service, kind, values, client_request_id, estimate_id=prior["estimate_id"])
