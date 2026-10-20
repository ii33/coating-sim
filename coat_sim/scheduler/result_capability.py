"""Shared queue result capabilities and verified optimization checkpoints."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re

from coat_sim.core.geometric_raytrace_result import validate_geometric_raytrace_summary


_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_ARTIFACT_ID = re.compile(r"[0-9a-f]{64}\Z")
_RAYTRACE_RESULT_KIND = "geometric_raytrace"
_RAYTRACE_RESULT_SCHEMA = "coat_sim.geometric_raytrace_result"
_FILM_RESULT_KIND = "coat_sim.film_optimization_result"
_FILM_STRUCTURE_RESULT_KIND = "coat_sim.film_structure_optimization_result"
_FILM_CANDIDATE_KIND = "coat_sim.film_optimization_candidate"
_FILM_REQUEST_KIND = "coat_sim.film_optimization_request"
_FILM_STRUCTURE_CANDIDATE_KIND = "coat_sim.film_structure_candidate"
_FILM_STRUCTURE_REQUEST_KIND = "coat_sim.film_structure_optimization_request"


def film_optimization_service(service):
    """Return the film API while preserving a worker CoatService's path policy.

    Queue workers are constructed with the generic CoatService. Embedded
    film callers may already pass a FilmDesignService-like object; keep that
    object unchanged as long as it implements the replay simulator.
    """
    from coat_sim.core.api import CoatService

    if isinstance(service, CoatService):
        service = service.film_design_service
    if not callable(getattr(service, "simulate", None)):
        raise TypeError("film optimization requires a film service with simulate")
    return service


def is_film_optimization_result(result) -> bool:
    """Check the typed film result envelope without inventing simulation fields.

    Full replay validation belongs to ``validate_film_optimization_result``;
    this inexpensive shape check is used while listing queue records.
    """
    if (isinstance(result, dict)
            and result.get("kind") == _FILM_STRUCTURE_RESULT_KIND):
        return _is_film_structure_result(result)
    if not isinstance(result, dict) or result.get("schema_version") != 1 \
            or result.get("kind") != _FILM_RESULT_KIND:
        return False
    if set(result) - {"schema_version", "kind", "request", "candidate",
                      "partial_result", "state", "message", "solver_validation", "execution"}:
        return False
    # Solver/holdout metadata is an additive terminal-result field. This
    # capability check only verifies its container shape; the frozen core's
    # validate_checkpoint replays the candidate and verifies every value.
    if "solver_validation" in result and not isinstance(result["solver_validation"], dict):
        return False
    if "execution" in result and not isinstance(result["execution"], dict):
        return False
    request = result.get("request")
    snapshot = request.get("frozen_material_snapshot") if isinstance(request, dict) else None
    if (not isinstance(request, dict) or request.get("schema_version") != 1
            or request.get("kind") != _FILM_REQUEST_KIND
            or type(request.get("design_revision")) is not int
            or not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", snapshot["sha256"])):
        return False
    candidate = result.get("candidate")
    candidate_fields = {
        "schema_version", "kind", "design_revision", "design", "thickness_nm",
        "quality", "per_condition_errors", "iterations", "evaluations",
    }
    if (not isinstance(candidate, dict) or set(candidate) != candidate_fields
            or candidate.get("schema_version") != 1
            or candidate.get("kind") != _FILM_CANDIDATE_KIND
            or type(candidate.get("design_revision")) is not int
            or candidate.get("design_revision") != request["design_revision"]
            or not isinstance(candidate.get("design"), dict)
            or not isinstance(candidate.get("thickness_nm"), dict)
            or not isinstance(candidate.get("per_condition_errors"), list)
            or type(candidate.get("iterations")) is not int
            or candidate["iterations"] < 0
            or type(candidate.get("evaluations")) is not int
            or candidate["evaluations"] < 1):
        return False
    quality = candidate.get("quality")
    if (not isinstance(quality, dict)
            or set(quality) != {"metric", "value", "direction",
                                "weighted_max_normalized_error",
                                "weighted_rms_error", "targets_met"}
            or quality.get("metric") != "max_normalized_error"
            or quality.get("direction") != "minimize"
            or type(quality.get("targets_met")) is not bool):
        return False
    for field in ("value", "weighted_max_normalized_error", "weighted_rms_error"):
        value = quality.get(field)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            return False
    for value in candidate["thickness_nm"].values():
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            return False
    partial = result.get("partial_result")
    return bool(
        isinstance(partial, dict)
        and set(partial) == {"schema_version", "kind", "best_so_far",
                             "candidate_verified", "quality", "artifact_kind",
                             "artifact_schema_version"}
        and partial.get("schema_version") == 1
        and partial.get("kind") == "best_candidate"
        and partial.get("best_so_far") is True
        and partial.get("candidate_verified") is True
        and partial.get("artifact_kind") == _FILM_CANDIDATE_KIND
        and partial.get("artifact_schema_version") == 1
        and partial.get("quality") == {"metric": quality["metric"],
                                        "value": quality["value"]}
        and ("state" not in result or isinstance(result["state"], str))
        and ("message" not in result or isinstance(result["message"], str))
    )


def _contains_solver_identifier(value) -> bool:
    if isinstance(value, dict):
        return ("solver" in value or "solver_id" in value
                or any(_contains_solver_identifier(item) for item in value.values()))
    if isinstance(value, list):
        return any(_contains_solver_identifier(item) for item in value)
    return False


def _is_film_structure_result(result) -> bool:
    """Cheap typed-envelope check; core validation replays all structure rules."""
    if (not isinstance(result, dict) or result.get("schema_version") != 1
            or result.get("kind") != _FILM_STRUCTURE_RESULT_KIND
            or set(result) - {"schema_version", "kind", "request", "candidate",
                              "structure", "partial_result", "state", "message"}
            or not {"schema_version", "kind", "request", "candidate",
                    "structure", "partial_result"}.issubset(result)):
        return False
    request = result.get("request")
    if (not isinstance(request, dict)
            or request.get("schema_version") != 1
            or request.get("kind") != _FILM_STRUCTURE_REQUEST_KIND
            or _contains_solver_identifier(request)):
        return False
    base_request = request.get("base_request")
    snapshot = (base_request.get("frozen_material_snapshot")
                if isinstance(base_request, dict) else None)
    if (not isinstance(base_request, dict) or not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", snapshot["sha256"])
            or type(base_request.get("design_revision")) is not int):
        return False
    candidate = result.get("candidate")
    if (not isinstance(candidate, dict)
            or candidate.get("design_revision") != base_request["design_revision"]
            or not isinstance(candidate.get("design"), dict)
            or not isinstance(candidate.get("thickness_nm"), dict)
            or not isinstance(candidate.get("per_condition_errors"), list)
            or not isinstance(candidate.get("validation_per_condition_errors"), list)
            or set(candidate) != {"design_revision", "design", "thickness_nm", "quality",
                                  "per_condition_errors", "validation_per_condition_errors"}):
        return False
    quality = candidate.get("quality")
    if (not isinstance(quality, dict)
            or set(quality) != {"max_normalized_error", "weighted_max_normalized_error",
                                "weighted_rms_error", "targets_met", "validation_complete",
                                "validation_max_normalized_error", "validation_weighted_rms_error",
                                "validation_targets_met", "termination_reason"}
            or type(quality.get("targets_met")) is not bool
            or type(quality.get("validation_complete")) is not bool
            or (quality.get("validation_targets_met") is not None
                and type(quality.get("validation_targets_met")) is not bool)
            or not isinstance(quality.get("termination_reason"), str)):
        return False
    for field in ("max_normalized_error", "weighted_max_normalized_error",
                  "weighted_rms_error", "validation_max_normalized_error",
                  "validation_weighted_rms_error"):
        value = quality.get(field)
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, (int, float))
                                  or not math.isfinite(float(value))):
            return False
    structure = result.get("structure")
    if (not isinstance(structure, dict)
            or structure.get("mode") not in {"needle_only", "growth_only", "combined"}
            or not isinstance(structure.get("topology_audit"), dict)
            or not isinstance(structure.get("trace"), list)
            or not isinstance(structure.get("counters"), dict)
            or set(structure) != {"mode", "added_needle_layer_ids", "topology_audit",
                                 "trace", "counters"}
            or not isinstance(structure.get("added_needle_layer_ids"), list)):
        return False
    partial = result.get("partial_result")
    complete_worker_cancel = False
    if (isinstance(partial, dict) and partial.get("candidate_verified") is False
            and result.get("state") == "cancelled"
            and structure["topology_audit"].get("cancel_snapshot")):
        from coat_sim.core.film_optimization_job import validate_completed_structure_cancel_result
        try:
            validate_completed_structure_cancel_result(result)
        except (KeyError, TypeError, ValueError, ArithmeticError, IndexError):
            return False
        complete_worker_cancel = True
    if (not isinstance(partial, dict)
            or partial.get("kind") != "best_candidate"
            or (partial.get("candidate_verified") is not True and not complete_worker_cancel)
            or type(partial.get("best_so_far")) is not bool
            or type(partial.get("validation_complete")) is not bool
            or type(partial.get("is_final")) is not bool
            # A checkpoint advertises the incumbent as best-so-far; the
            # terminal wrapper marks that same candidate as final instead.
            or partial.get("best_so_far") is not (not partial.get("is_final"))
            or set(partial) != {"kind", "best_so_far", "candidate_verified",
                                "validation_complete", "is_final"}
            or partial.get("validation_complete") is not quality.get("validation_complete")):
        return False
    if "state" in result and result["state"] not in {"completed", "cancelled", "failed"}:
        return False
    return "message" not in result or isinstance(result["message"], str)


def validate_film_optimization_result(service, result) -> dict:
    """Replay a film result using only its frozen design/material snapshot."""
    if (isinstance(result, dict)
            and result.get("kind") == _FILM_STRUCTURE_RESULT_KIND):
        from coat_sim.core.film_optimization_structure import validate_film_structure_payload

        validated = validate_film_structure_payload(film_optimization_service(service), result)
        if not _is_film_structure_result(validated) or not is_film_optimization_result(validated):
            raise ValueError("film structure result failed replay capability validation")
        return deepcopy(validated)
    if not is_film_optimization_result(result):
        raise ValueError("film optimization result kind/schema is invalid")
    from coat_sim.core.film_optimization_job import validate_checkpoint

    validated = validate_checkpoint(film_optimization_service(service), result)
    if not is_film_optimization_result(validated):
        raise ValueError("film optimization result failed capability validation")
    return deepcopy(validated)


def is_best_candidate_result(result) -> bool:
    """Return whether a terminal result explicitly advertises a verified candidate."""
    if is_film_optimization_result(result):
        return True
    from coat_sim.core.film_synthesis_jobs import is_synthesis_result
    if is_synthesis_result(result):
        body = result.get("result", {})
        provenance = body.get("candidate_provenance") or {}
        audited = (body.get("validation_complete") is True
                   and isinstance(body.get("validation_score"), dict))
        complete_cancel = (body.get("state") == "cancelled"
            and provenance.get("selection") == "completed_training_at_cancel"
            and provenance.get("training_complete") is True)
        return isinstance(body.get("candidate_design"), dict) and (audited or complete_cancel)
    if not isinstance(result, dict):
        return False
    partial = result.get("partial_result")
    if not isinstance(partial, dict):
        return False
    quality = partial.get("quality")
    if not (partial.get("schema_version") == 1
            and partial.get("kind") == "best_candidate"
            and partial.get("best_so_far") is True
            and partial.get("candidate_verified") is True
            and isinstance(quality, dict)
            and isinstance(quality.get("metric"), str)
            and quality["metric"].strip()
            and isinstance(quality.get("value"), (int, float))
            and not isinstance(quality.get("value"), bool)
            and math.isfinite(float(quality["value"]))):
        return False
    run_id = result.get("run_id")
    asset = result.get("asset")
    if not (isinstance(run_id, str) and _RUN_ID.fullmatch(run_id)
            and isinstance(asset, dict) and asset.get("run_id") == run_id):
        return False
    # Local paths are private transport metadata and are removed from public
    # payloads. A registered portable bundle is the public result capability.
    has_local_bundle = (isinstance(asset.get("temporary_bundle_path"), str)
                        and bool(asset["temporary_bundle_path"]))
    has_bundle_artifact = any(
        isinstance(item, dict) and item.get("role") == "bundle"
        and isinstance(item.get("artifact_id"), str) and item["artifact_id"]
        for item in result.get("result_artifacts", [])
    )
    return has_local_bundle or has_bundle_artifact


def _has_registered_result_artifact(result) -> bool:
    """Recognize a successful result backed by a registered, hashed artifact."""
    artifacts = result.get("result_artifacts") if isinstance(result, dict) else None
    if not isinstance(artifacts, list):
        return False
    for item in artifacts:
        if not isinstance(item, dict) or item.get("role") != "result":
            continue
        artifact_id, digest = item.get("artifact_id"), item.get("sha256")
        size, name = item.get("bytes"), item.get("name")
        if (isinstance(artifact_id, str) and _ARTIFACT_ID.fullmatch(artifact_id)
                and isinstance(digest, str) and _ARTIFACT_ID.fullmatch(digest)
                and type(size) is int and size >= 0
                and isinstance(name, str) and name.strip()):
            return True
    return False


def is_synthesis_result(result) -> bool:
    """Typed film synthesis result predicate shared by the scheduler boundary."""
    from coat_sim.core.film_synthesis_jobs import is_synthesis_result as check
    return check(result)


def is_geometric_raytrace_result(result) -> bool:
    """Recognize a complete ray result summary backed by a registered artifact."""
    try:
        validate_geometric_raytrace_summary(result)
    except (TypeError, ValueError):
        return False
    return _has_registered_result_artifact(result)


def job_has_viewable_result(job) -> bool:
    """Keep result capability independent from a job's terminal state."""
    if not isinstance(job, dict):
        return False
    result = job.get("result")
    if not isinstance(result, dict):
        return False
    if job.get("state") == "succeeded":
        from coat_sim.core.film_synthesis_jobs import is_synthesis_result
        if is_synthesis_result(result):
            return True
        if result.get("kind") == "appearance_render":
            return is_appearance_render_result(result) and _has_registered_result_artifact(result)
        if is_film_optimization_result(result):
            return True
        if (result.get("kind") == _RAYTRACE_RESULT_KIND
                or result.get("result_schema") == _RAYTRACE_RESULT_SCHEMA):
            return is_geometric_raytrace_result(result)
        run_id = result.get("run_id")
        asset = result.get("asset")
        if (isinstance(run_id, str) and _RUN_ID.fullmatch(run_id)
                and isinstance(asset, dict) and asset.get("run_id", run_id) == run_id):
            return True
        return _has_registered_result_artifact(result)
    return job.get("state") in ("failed", "cancelled") and is_best_candidate_result(result)


def is_appearance_render_result(result) -> bool:
    """Check the typed appearance envelope without opening completion files."""
    from coat_sim.core.appearance_result import is_appearance_render_result as check

    return check(result)


def validate_appearance_render_result(root, job_id, result, *,
                                      expected_input_digest=None,
                                      require_registered_artifact=False):
    """Revalidate the index, queue-spec binding, and all five render outputs."""
    from coat_sim.core.appearance_result import validate_appearance_render_result as validate

    return validate(root, job_id, result,
                    expected_input_digest=expected_input_digest,
                    require_registered_artifact=require_registered_artifact)


def load_best_candidate_checkpoint(root, job_id, *, terminal_state="cancelled"):
    """Load and validate a job-local checkpoint; return ``(result, reason)``.

    A checkpoint is usable only if its pointer is contained in the scheduler job
    directory and the product-specific simulation bundle or typed film result
    still validates against its frozen input snapshot.
    """
    root = Path(root).resolve()
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        return None, "Invalid scheduler job identity."
    job_directory = (root / "artifacts" / "runtime" / "scheduler" / "jobs" / job_id).resolve()
    if not job_directory.is_relative_to(root):
        return None, "Checkpoint location is outside the scheduler workspace."
    pointer = job_directory / "best-candidate.json"
    if not pointer.exists():
        return None, "No verified optimization candidate was saved before this job ended."
    try:
        if pointer.is_symlink() or not pointer.resolve().is_relative_to(job_directory):
            raise ValueError("checkpoint pointer escaped its job directory")
        envelope = json.loads(pointer.read_text(encoding="utf-8"))
        if isinstance(envelope, dict) and envelope.get("kind") == "coat_sim.film_synthesis_audited_checkpoint":
            from .jobs import JobSpec
            from .store import SchedulerStore
            from coat_sim.core.film_synthesis_jobs import validate_checkpoint
            spec_path = job_directory / "spec.json"
            if spec_path.is_symlink() or not spec_path.resolve().is_relative_to(job_directory):
                raise ValueError("film synthesis job spec escaped its scheduler directory")
            spec = JobSpec.parse_frozen_json(spec_path.read_text(encoding="utf-8"))
            queue_record = SchedulerStore(root).status(job_id)
            if (spec.executor != "film_synthesis"
                    or queue_record.get("input_digest") != spec.fingerprint
                    or spec.parameters.get("payload") != envelope.get("job")):
                raise ValueError("audited synthesis checkpoint is not bound to this frozen job input")
            recovered = validate_checkpoint(envelope, terminal_state=terminal_state)
            business_root = queue_record.get("execution_project_root")
            recovered["execution_project_root"] = (
                business_root if isinstance(business_root, str) and business_root else str(root))
            return recovered, ""
        result = envelope.get("result") if isinstance(envelope, dict) else None
        if is_film_optimization_result(result):
            from coat_sim.core.api import CoatService

            service = CoatService(root, restrict_to_root=False)
            return validate_film_optimization_result(service, result), ""
        if not is_best_candidate_result(result):
            raise ValueError("checkpoint does not contain a verified best candidate")
        asset = result["asset"]
        bundle_source = Path(asset["temporary_bundle_path"])
        if bundle_source.is_symlink():
            raise ValueError("candidate bundle must not be a symbolic link")
        bundle = bundle_source.resolve(strict=True)
        if not bundle.is_file() or not bundle.is_relative_to(root):
            raise ValueError("candidate bundle is missing or outside the workspace")
        from coat_sim.core.api import CoatService

        service = CoatService(root, restrict_to_root=False)
        loaded = service.load_simulation_asset(bundle)
        if loaded.get("run_id") != result["run_id"]:
            raise ValueError("candidate bundle run identity does not match its pointer")
        snapshot = loaded.get("input_snapshot")
        if not isinstance(snapshot, dict) or not snapshot.get("files"):
            raise ValueError("candidate bundle has no complete frozen input snapshot")
        document = service.load("process", snapshot)
        if document.issues or document.runtime is None:
            raise ValueError("candidate frozen inputs do not load as a complete process")
        simulation = loaded.get("result")
        if not isinstance(simulation, dict) or not any(
                key in simulation for key in ("field", "field_nm")):
            raise ValueError("candidate bundle has no simulation field")
        return deepcopy(result), ""
    except Exception as exc:
        return None, f"The best-candidate checkpoint is invalid and cannot be opened: {exc}"
