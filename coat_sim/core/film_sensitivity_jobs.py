"""Independent frozen sensitivity jobs, checkpoints and confirmed submission.

The v1 ordinary optimization request is deliberately not extended. This module
owns the durable adapter; numerical semantics belong to ``film_sensitivity``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any, Mapping

from .film_sensitivity import (
    FilmSensitivityRequest,
    execute_film_sensitivity_request,
    film_sensitivity_request_from_payload,
    film_sensitivity_request_to_payload,
    film_sensitivity_result_from_payload,
    film_sensitivity_result_to_payload,
    freeze_film_sensitivity_request,
)

JOB_KIND = "coat_sim.film_sensitivity_job"
CHECKPOINT_KIND = "coat_sim.film_sensitivity_checkpoint"


def prepare_sensitivity_payload(service: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    request = film_sensitivity_request_from_payload(payload)
    frozen = freeze_film_sensitivity_request(
        getattr(service, "film_design_service", service), request
    )
    return film_sensitivity_request_to_payload(frozen)


def canonical_parameters(service: Any, supplied: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(supplied, Mapping) or set(supplied) != {"payload"}:
        raise ValueError("film_sensitivity requires exactly one independent request payload")
    return {"payload": prepare_sensitivity_payload(service, supplied["payload"])}


def build_job_payload(
    service: Any, request: FilmSensitivityRequest | Mapping[str, Any]
) -> dict[str, Any]:
    if isinstance(request, FilmSensitivityRequest):
        payload = film_sensitivity_request_to_payload(
            freeze_film_sensitivity_request(service, request)
        )
    else:
        payload = prepare_sensitivity_payload(service, request)
    return {"schema_version": 1, "kind": JOB_KIND, "request": payload}


def deserialize_job_payload(value: Mapping[str, Any]) -> FilmSensitivityRequest:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema_version", "kind", "request"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["kind"] != JOB_KIND
    ):
        raise ValueError("invalid film sensitivity job envelope")
    request = film_sensitivity_request_from_payload(value["request"])
    if request.frozen_material_snapshot is None:
        raise ValueError("sensitivity worker requires frozen material data")
    return freeze_film_sensitivity_request(None, request)


def is_sensitivity_result(value: Any) -> bool:
    """Strict shape parsing only. Physical replay is required before applying."""
    try:
        result = film_sensitivity_result_from_payload(value)
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return False
    return result.is_final is True


def result_matches_job(value: Mapping[str, Any], job: Mapping[str, Any]) -> bool:
    try:
        result = film_sensitivity_result_from_payload(value)
        request = deserialize_job_payload(job)
        return film_sensitivity_request_to_payload(
            result.frozen_request
        ) == film_sensitivity_request_to_payload(request)
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return False


def cancelled_reply(value: Mapping[str, Any]) -> dict[str, Any]:
    result = film_sensitivity_result_from_payload(value)
    result = replace(
        result,
        state="cancelled",
        termination_reason="scheduler_cancel_won",
        is_final=True,
        message="Cancellation was accepted before job completion; a complete scored candidate is retained without applying it.",
    )
    return film_sensitivity_result_to_payload(result)


def terminal_reply(value: Mapping[str, Any], state: str) -> dict[str, Any]:
    """Keep a scored candidate read-only when the scheduler did not succeed."""
    if state == "cancelled":
        return cancelled_reply(value)
    if state != "failed":
        raise ValueError("sensitivity terminal reply requires failed or cancelled")
    result = film_sensitivity_result_from_payload(value)
    if result.state != "failed":
        result = replace(result, state="failed", is_final=True,
            termination_reason="scheduler_failed",
            message="Scheduler reported failure; the retained complete candidate is read-only.")
    return film_sensitivity_result_to_payload(result)


def validate_checkpoint(
    value: Mapping[str, Any], *, expected_job: Mapping[str, Any], terminal_state: str = "cancelled"
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema_version", "kind", "job", "result"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["kind"] != CHECKPOINT_KIND
        or value["job"] != expected_job
        or not result_matches_job(value["result"], expected_job)
    ):
        raise ValueError("sensitivity checkpoint is not bound to the frozen job input")
    from .film_sensitivity import validate_film_sensitivity_result_payload

    validated = validate_film_sensitivity_result_payload(
        value["result"], expected_request=deserialize_job_payload(expected_job), replay=True
    )
    result = film_sensitivity_result_from_payload(validated)
    return film_sensitivity_result_to_payload(
        replace(
            result,
            state=terminal_state,
            is_final=True,
            termination_reason="scored_checkpoint_recovered",
            message="Recovered and replayed a complete sensitivity candidate; original worker did not complete.",
        )
    )


def execute_job(
    parameters: Mapping[str, Any],
    cancel_event: Any,
    report: Any,
    checkpoint_path: Any,
    write_json: Any,
    *, candidate_observer=None, candidate_observer_factory=None,
) -> dict[str, Any]:
    if not isinstance(parameters, Mapping) or set(parameters) != {"payload"}:
        raise ValueError("film_sensitivity executor requires one frozen job")
    job = deepcopy(parameters["payload"])
    request = deserialize_job_payload(job)
    observer = ((candidate_observer_factory(request) if candidate_observer_factory is not None
                 else candidate_observer) if request.operation == 'optimize' else None)

    def progress(item):
        body = asdict(item)
        body["stage"] = "film_sensitivity:" + str(body["stage"])
        body["best_score"] = body.pop("best_robustness_value", None)
        report(body)

    def checkpoint(payload):
        if not result_matches_job(payload, job):
            raise ValueError("sensitivity checkpoint request changed during execution")
        write_json(
            checkpoint_path,
            {"schema_version": 1, "kind": CHECKPOINT_KIND, "job": job, "result": payload},
        )

    result = execute_film_sensitivity_request(
        None,
        request,
        cancel_event=cancel_event,
        progress_callback=progress,
        checkpoint_callback=checkpoint,
        candidate_observer=observer,
    )
    payload = film_sensitivity_result_to_payload(result)
    if not result_matches_job(payload, job):
        raise ValueError("sensitivity result does not match its frozen job")
    return payload


def submit_sensitivity(
    service: Any, payload: Mapping[str, Any], client_request_id: str | None = None
):
    from .compute_tasks import ComputeTaskStore, digest
    from coat_sim.scheduler.submission import submit_confirmed

    values = canonical_parameters(service, {"payload": payload})
    identity = digest({"kind": "film_sensitivity", "parameters": values, "input_files": {}})
    if client_request_id is None:
        client_request_id = "film-sensitivity:" + identity
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
        return submit_confirmed(service, "film_sensitivity", values, client_request_id)
    if prior["state"] in {"estimated", "pending"}:
        return {
            **store.public_estimate(prior),
            "state": "prepared" if prior["state"] == "estimated" else "awaiting_confirmation",
            "needs_release_request": prior["state"] == "estimated",
            "needs_user_confirmation": prior.get("release_policy") == "manual",
            "next_action": "request_compute_confirmation",
        }
    return submit_confirmed(
        service, "film_sensitivity", values, client_request_id, estimate_id=prior["estimate_id"]
    )
