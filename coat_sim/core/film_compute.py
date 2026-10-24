"""Typed film JSON adapters for the existing estimate and release protocol."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from pydantic import BaseModel, ConfigDict


class FilmEstimateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request: dict[str, Any]


class FilmSubmitArguments(FilmEstimateArguments):
    client_request_id: str | None = None


FILM_TOOL_ARGUMENTS = {
    "estimate_film_optimization": FilmEstimateArguments,
    "submit_film_optimization": FilmSubmitArguments,
}


def validate_tool_arguments(name, arguments):
    """Reject coercion/extra fields before the MCP SDK can filter arguments."""
    json.dumps(arguments, allow_nan=False)
    return FILM_TOOL_ARGUMENTS[name].model_validate(arguments).model_dump()


def canonical_film_parameters(service, supplied):
    if not isinstance(supplied, dict) or set(supplied) != {"request"}:
        raise ValueError("film_optimization requires exactly one serialized request")
    raw = supplied["request"]
    from .film_optimization_job import (
        REQUEST_KIND, deserialize_request, freeze_job_payload, _validate_replay_request,
    )
    from .film_optimization import _validate_optimization_controls
    from coat_sim.scheduler.result_capability import film_optimization_service
    if (not isinstance(raw, dict) or raw.get("kind") != REQUEST_KIND
            or type(raw.get("schema_version")) is not int or raw["schema_version"] != 2):
        raise ValueError("only current v2 serialized film thickness requests are supported")
    json.dumps(raw, allow_nan=False)
    request = deserialize_request(raw)
    _validate_optimization_controls(request)
    frozen = freeze_job_payload(film_optimization_service(service), request)["request"]
    _validate_replay_request(deserialize_request(frozen))
    return {"request": frozen}


def estimate_film(service, request):
    values = canonical_film_parameters(service, {"request": request})
    return service.estimate_compute_task("film_optimization", values)


def submit_film(service, request, client_request_id=None):
    from .compute_tasks import ComputeTaskStore, digest
    from coat_sim.scheduler.submission import submit_confirmed
    values = canonical_film_parameters(service, {"request": request})
    identity = digest({"kind": "film_optimization", "parameters": values, "input_files": {}})
    client_request_id = client_request_id if client_request_id is not None else "film:" + identity
    if not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 512:
        raise ValueError("client_request_id must contain 1 to 512 characters")
    store = ComputeTaskStore(service.root)
    prior = store.find_request(client_request_id)
    if prior is not None and prior["input_digest"] != identity:
        raise ValueError("request_id_conflict: film request inputs changed")
    if prior is None:
        # Only exact, unbound estimates may be reused. Approved receipts belong
        # to their original release request; this lookup cannot grant approval.
        key = store.claim_unbound_estimate(client_request_id, identity)
        if key is not None:
            prior = store.read_estimate(key)
    if prior is None:
        receipt = submit_confirmed(service, "film_optimization", values, client_request_id)
    elif prior["state"] in {"estimated", "pending"}:
        receipt = {**store.public_estimate(prior),
                   "state": "prepared" if prior["state"] == "estimated" else "awaiting_confirmation",
                   "needs_release_request": prior["state"] == "estimated",
                   "needs_user_confirmation": prior.get("release_policy") == "manual",
                   "next_action": "request_compute_confirmation"}
    else:
        receipt = submit_confirmed(service, "film_optimization", deepcopy(values),
                                   client_request_id, estimate_id=prior["estimate_id"])
    return {**receipt, "client_request_id": client_request_id,
            "estimate_id": receipt.get("estimate_id") or (prior or {}).get("estimate_id")}
