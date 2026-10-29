"""Read-only assistant views of the existing frozen synthesis/queue contracts.

These helpers neither execute optics nor submit/apply a candidate. The scheduler
and synthesis codecs remain the sole owners of execution and validation.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import re
from typing import Any

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
from coat_sim.core.film_synthesis_defaults import ASSUME_UNKNOWN_K_ZERO
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_continuation import (
    MAX_SEARCH_STATE_BYTES, continue_synthesis_request, validate_search_state,
)
from coat_sim.core.film_synthesis_io import (
    REQUEST_KIND, SCHEMA_VERSION, film_synthesis_request_to_payload,
)
from coat_sim.core.film_synthesis_jobs import (
    AUDITED_CHECKPOINT_KIND, _partial_result, deserialize_job_payload,
    is_synthesis_result,
)
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.scheduler.worker import _film_reply_matches_payload


def request_template() -> dict[str, Any]:
    """Defaults come from the product request, not a second assistant budget."""
    defaults = asdict(FilmSynthesisRequest({}, (), ()))
    defaults["environment"] = {"assume_lossless": ASSUME_UNKNOWN_K_ZERO}
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": REQUEST_KIND,
        "request_id": "replace-with-your-request-id",
        "request": defaults,
        "physical_structures": [],
    }


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("synthesis_source_invalid: saved artifact must be a regular file")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_SEARCH_STATE_BYTES + 1)
    except OSError as exc:
        raise ValueError("synthesis_source_unavailable: required saved artifact is unreadable") from exc
    if len(raw) > MAX_SEARCH_STATE_BYTES:
        raise ValueError("synthesis_source_invalid: saved JSON exceeds the existing transport limit")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("synthesis_source_invalid: saved artifact must be a JSON object")
    return value


def _source(service: Any, job_id: str):
    if not isinstance(job_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id) is None:
        raise ValueError("invalid_job_id")
    row = SchedulerClient(service.root).status(job_id)
    if row.get("job_id") != job_id or row.get("kind") != "film_synthesis":
        raise ValueError("synthesis_source_invalid: job is not the requested synthesis task")
    if not isinstance(row.get("input_digest"), str) or re.fullmatch(r"[0-9a-f]{64}", row["input_digest"]) is None:
        raise ValueError("synthesis_source_invalid: queue input identity is missing")
    root = Path(service.root).resolve()
    jobs_root = root / "artifacts/runtime/scheduler/jobs"
    directory = jobs_root / job_id
    # Resolve no symlinked scheduler ancestors or job directories. An in-root
    # sibling link could otherwise make one queue row read another job's files.
    for path in (root / "artifacts", root / "artifacts/runtime",
                 root / "artifacts/runtime/scheduler", jobs_root, directory):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("synthesis_source_invalid: job directory escaped its workspace")
    spec_path = directory / "spec.json"
    if spec_path.is_symlink():
        raise ValueError("synthesis_source_invalid: frozen worker spec must be a regular file")
    if not spec_path.exists():
        # The queue row is committed before the daemon creates worker artifacts.
        # Only expose that already-verified status for these two pre-worker
        # states; never infer a request or candidate from missing artifacts.
        pre_worker_failure = row.get("state") == "failed" and row.get("error_code") == "worker_start_failed"
        if row.get("state") == "queued" or pre_worker_failure:
            return row, None, directory, None, None, None
        raise ValueError("synthesis_source_unavailable: frozen worker spec is not available")
    spec = JobSpec.parse_frozen(_read(spec_path))
    if spec.executor != "film_synthesis" or spec.fingerprint != row.get("input_digest"):
        raise ValueError("synthesis_source_invalid: frozen queue input identity changed")
    request_id, request, metadata = deserialize_job_payload(spec.parameters.get("payload"))
    return row, spec, directory, request_id, request, metadata


def _checkpoint(directory: Path, request: FilmSynthesisRequest, result: Any):
    # A final envelope owns its exact final frontier. A run with no viewable
    # optical candidate can still have the separately committed search state.
    state = result.get("result", {}).get("search_checkpoint") if isinstance(result, dict) else None
    path = directory / "best-candidate.search-state.json"
    if state is None:
        if not path.exists():
            return None
        state = _read(path)
    return validate_search_state(request, state)


def _bound_result(row, spec):
    result = row.get("result")
    if result is None:
        return None
    payload = spec.parameters["payload"]
    if (not is_synthesis_result(result) or not _film_reply_matches_payload(result, payload)
            or result.get("physical_structure_metadata", []) != payload["physical_structure_metadata"]):
        raise ValueError("synthesis_source_invalid: result does not match its frozen queue input")
    return result


def _candidate(envelope, *, source: str) -> dict[str, Any] | None:
    body = envelope["result"]
    design = body.get("candidate_design")
    if design is None:
        return None
    audit = envelope["best_candidate_audit"]
    training = body.get("training_score") or {}
    validation = body.get("validation_score") or {}
    return {
        "physical_hash": physical_design_hash(design),
        "source": source,
        "layer_count": len(design["front_layers"]) + len(design["back_layers"]),
        "total_thickness_nm": sum(r["thickness"]["value"] for side in ("front_layers", "back_layers")
                                  for r in design[side]),
        "validation_complete": body["validation_complete"],
        "recorded_candidate_audit": deepcopy(audit),
        "training_max_normalized_error": training.get("max_error"),
        "training_weighted_rms_error": training.get("weighted_rms_error"),
        "audit_max_normalized_error": validation.get("max_error"),
        "per_objective": deepcopy(envelope["review"]["per_objective"]),
        "replayed_by_this_read": False,
    }


def read_job(service: Any, job_id: str) -> dict[str, Any]:
    row, spec, directory, request_id, request, _metadata = _source(service, job_id)
    if spec is None:
        failure = row.get("state") == "failed"
        return {
            "job_id": job_id, "scheduler_state": row["state"],
            "execution_project_root": str(service.root),
            "core_state": None, "termination_reason": None, "targets_met": None,
            "local_stage": None, "thickness_stage": {},
            "error": deepcopy(row.get("error")),
            "error_code": row.get("error_code"),
            "cancel_requested": row.get("cancel_requested", False),
            "progress": deepcopy(row.get("progress")), "best_candidate": None,
            "source_available": False, "request_available": False,
            "checkpoint_available": False,
            "progress_evidence": (
                "Scheduler queue status only; no frozen worker spec, request, candidate, or checkpoint is available."
            ),
            "source_input_digest": None,
            "continuation": {"available": False, "checkpoint_sha256": None,
                "reason": "worker_source_not_started" if not failure else "worker_start_failed_before_source"},
            "application": "Native candidate review and source guards remain required; this tool does not apply or save.",
        }
    result = _bound_result(row, spec)
    best = _candidate(result, source="terminal_worker_result") if result is not None else None
    if result is None:
        path = directory / "best-candidate.json"
        if path.exists():
            saved = _read(path)
            if (saved.get("kind") != AUDITED_CHECKPOINT_KIND
                    or saved.get("schema_version") != SCHEMA_VERSION
                    or saved.get("checkpoint_state") != "audited"
                    or saved.get("job") != spec.parameters["payload"]):
                raise ValueError("synthesis_source_invalid: saved candidate identity changed")
            # Decode recorded complete raw coverage without starting a physical
            # replay. This read is never a candidate application capability.
            projected = _partial_result(request, request_id, saved)
            if not is_synthesis_result(projected):
                raise ValueError("synthesis_source_invalid: recorded candidate coverage is invalid")
            best = _candidate(projected, source="worker_committed_audited_checkpoint")
    state = _checkpoint(directory, request, result)
    body = {} if result is None else result["result"]
    local = body.get("local_optimum")
    hybrid = body.get("performance", {}).get("search", {}).get("hybrid", {})
    return {
        "job_id": job_id, "scheduler_state": row["state"], "execution_project_root": str(service.root),
        "core_state": body.get("state"), "termination_reason": body.get("termination_reason"),
        "targets_met": body.get("targets_met"),
        "local_stage": None if local is None else {key: deepcopy(local.get(key)) for key in
            ("local_converged", "termination", "physical_hash", "role", "validation_complete")},
        "thickness_stage": {key: deepcopy(hybrid.get("thickness_reduction", {}).get(key)) for key in
            ("status", "stop_reason", "promoted", "independent_audit_performed")},
        "error": deepcopy(row.get("error")), "cancel_requested": row.get("cancel_requested", False),
        "progress": deepcopy(row.get("progress")), "best_candidate": best,
        "source_available": True, "request_available": True,
        "checkpoint_available": state is not None,
        "progress_evidence": "Worker stage diagnostics; candidate audit identity is bound only by best_candidate.",
        "source_input_digest": spec.fingerprint,
        "continuation": {"available": state is not None and row["state"] in {"succeeded", "failed", "cancelled"},
                         "checkpoint_sha256": None if state is None else state["sha256"],
                         "reason": "job_still_active" if row["state"] not in {"succeeded", "failed", "cancelled"}
                                   else "no_committed_search_checkpoint" if state is None else None},
        "application": "Native candidate review and source guards remain required; this tool does not apply or save.",
    }


def plan_continuation(service: Any, job_id: str, request_id: str, *,
                      limits: dict[str, Any] | None = None,
                      current_design: dict[str, Any] | None = None) -> dict[str, Any]:
    row, spec, directory, _original_id, request, _metadata = _source(service, job_id)
    if spec is None:
        raise ValueError("synthesis_continuation_unavailable: frozen worker source is not available")
    if row["state"] not in {"succeeded", "failed", "cancelled"}:
        raise ValueError("synthesis_continuation_unavailable: source job is still active")
    result = _bound_result(row, spec)
    state = _checkpoint(directory, request, result)
    if state is None:
        raise ValueError("synthesis_continuation_unavailable: no committed search checkpoint")
    if limits is not None and (not isinstance(limits, dict)
            or set(limits) - {"max_cycles", "max_evaluations", "max_seconds"}):
        raise ValueError("continuation limits accept only max_cycles, max_evaluations and max_seconds")
    updated = continue_synthesis_request(request, state, current_design=current_design, **(limits or {}))
    return {
        "ok": True, "request": film_synthesis_request_to_payload(request_id, updated),
        "execution_project_root": str(service.root),
        "source": {"job_id": job_id, "input_digest": spec.fingerprint,
                   "checkpoint_sha256": state["sha256"], "scheduler_state": row["state"]},
        "limits": {name: getattr(updated, name) for name in ("max_cycles", "max_evaluations", "max_seconds")},
        "execution": "Read-only plan. Estimate this request, then submit_film_synthesis through the existing confirmed queue. No scores, audit or solver state are reused.",
    }
