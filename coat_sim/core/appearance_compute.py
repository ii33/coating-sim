"""Strict shared preflight and scheduler adapters for appearance renders."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

from pydantic import BaseModel, ConfigDict
from coat_sim.appearance.limits import (
    MAX_APPEARANCE_CONTROL_BYTES, MAX_APPEARANCE_FROZEN_BYTES,
)


KIND = "appearance_render"
MAX_APPEARANCE_JSON_BYTES = MAX_APPEARANCE_CONTROL_BYTES
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RENDER_INPUT_FIELDS = frozenset({
    "wavelength_nm", "camera", "area_source", "environment_spectrum", "resolution",
})
_CIRCLE_POLICY = "captured_node_mean_with_local_nearest_fallback"
_REQUEST_REQUIRED = frozenset({"asset_path", "asset_sha256", "render_inputs"})
_REQUEST_ALLOWED = _REQUEST_REQUIRED | frozenset({
    "geometry_thickness_mode", "geometry_thickness_mm", "display_exposure",
    "backend", "precision",
})
_FROZEN_FIELDS = frozenset({
    "schema", "schema_version", "root", "request", "context", "context_sha256",
    "job", "job_sha256",
})


class AppearanceEstimateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request: dict[str, Any]


class AppearanceSubmitArguments(AppearanceEstimateArguments):
    client_request_id: str | None = None


class AppearanceAssetArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    asset_path: str


APPEARANCE_TOOL_ARGUMENTS = {
    "inspect_appearance_asset": AppearanceAssetArguments,
    "estimate_appearance_render": AppearanceEstimateArguments,
    "submit_appearance_render": AppearanceSubmitArguments,
}


def validate_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate before an MCP SDK can coerce values or discard extra fields."""
    model = APPEARANCE_TOOL_ARGUMENTS.get(name)
    if model is None:
        raise ValueError("unknown appearance tool")
    if type(arguments) is not dict:
        raise ValueError("appearance tool arguments must be a JSON object")
    try:
        json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        return model.model_validate(arguments).model_dump()
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid appearance tool arguments") from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_appearance_asset(service, asset_path: str) -> dict[str, Any]:
    """Report the actual digest and size of a readable project-local asset snapshot.

    This is a discovery aid only; estimate-time validation remains responsible for
    determining whether the asset contains all inputs needed for rendering.
    """
    root = _root_path(service.root)
    target = _resolve_asset(root, asset_path, service=service)
    from coat_sim.core.ray_trace_job import _asset_snapshot
    from coat_sim.core.simulation_assets import read_simulation_asset

    with _asset_snapshot(target, root=root) as snapshot:
        snapshot_path = Path(snapshot["path"])
        # Parse only the private hashed bytes, never the mutable source path.
        read_simulation_asset(snapshot_path, detail="summary", include_arrays=False)
        asset_bytes = snapshot_path.stat().st_size
        asset_sha256 = snapshot["sha256"]

    return {
        "schema_version": 1,
        "root": str(root),
        "asset_path": str(target),
        "asset_sha256": asset_sha256,
        "asset_bytes": asset_bytes,
    }


def _root_path(root: str | os.PathLike[str]) -> Path:
    try:
        resolved = Path(root).expanduser().resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("appearance project root is unavailable") from exc
    if not resolved.is_dir():
        raise ValueError("appearance project root is unavailable")
    return resolved


def _resolve_asset(root: Path, value: Any, *, service=None) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("asset_path must be a non-empty path")
    try:
        candidate = Path(service._path(value)) if service is not None else Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        target = candidate.expanduser().resolve(strict=True)
        data_root = (root / "data").resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("appearance asset path is unavailable") from exc
    if not data_root.is_dir() or not data_root.is_relative_to(root):
        raise ValueError("appearance project data directory is unavailable")
    if not target.is_relative_to(data_root):
        raise ValueError("appearance asset must resolve inside the project data directory")
    if target.suffix.lower() != ".coatasset" or not target.is_file():
        raise ValueError("asset_path must resolve to an existing .coatasset file")
    return target


def _normalize_public_request(root: Path, raw: Any, *, service=None,
                              verify_asset: bool = True,
                              preserve_legacy_backend: bool = False) -> dict[str, Any]:
    if type(raw) is not dict:
        raise ValueError("appearance request must be a JSON object")
    try:
        serialized = _canonical_json(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("appearance request must contain finite JSON values") from exc
    if len(serialized.encode("utf-8")) > MAX_APPEARANCE_JSON_BYTES:
        raise ValueError("appearance request exceeds the 16 MB limit")
    keys = set(raw)
    if not _REQUEST_REQUIRED <= keys or keys - _REQUEST_ALLOWED:
        raise ValueError("appearance request has missing or unknown fields")

    expected_sha256 = raw.get("asset_sha256")
    if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
        raise ValueError("asset_sha256 must be a lowercase SHA-256 digest")
    asset_path = _resolve_asset(root, raw.get("asset_path"), service=service)
    if verify_asset and _sha256_file(asset_path) != expected_sha256:
        raise ValueError("appearance asset SHA-256 does not match asset_sha256")

    render_inputs = raw.get("render_inputs")
    if (type(render_inputs) is not dict
            or not _RENDER_INPUT_FIELDS <= set(render_inputs)
            or set(render_inputs) - (_RENDER_INPUT_FIELDS | {"circle_boundary_sampling"})):
        raise ValueError("render_inputs has missing or unknown fields")
    if ("circle_boundary_sampling" in render_inputs
            and render_inputs["circle_boundary_sampling"] != _CIRCLE_POLICY):
        raise ValueError("unsupported circle_boundary_sampling policy")

    backend = raw.get("backend", "cpu")
    if type(backend) is not str or backend not in ("cpu", "gpu_cuda"):
        raise ValueError("appearance backend must be cpu or gpu_cuda")
    precision = raw.get("precision", "native" if backend == "cpu" else "mixed")
    if type(precision) is not str or precision not in (
            ("native",) if backend == "cpu" else ("float32", "mixed")):
        raise ValueError("appearance precision is invalid for the requested backend")

    mode = raw.get("geometry_thickness_mode", "film_design_snapshot")
    if not isinstance(mode, str) or mode not in ("film_design_snapshot", "explicit_override"):
        raise ValueError("geometry_thickness_mode must be film_design_snapshot or explicit_override")
    normalized: dict[str, Any] = {
        "asset_path": str(asset_path),
        "asset_sha256": expected_sha256,
        "render_inputs": json.loads(_canonical_json(render_inputs)),
        "geometry_thickness_mode": mode,
    }
    if not (preserve_legacy_backend and "backend" not in raw and "precision" not in raw):
        normalized.update(backend=backend, precision=precision)
    if mode == "film_design_snapshot":
        if "geometry_thickness_mm" in raw:
            raise ValueError("geometry_thickness_mm is not accepted with film_design_snapshot")
    else:
        if "geometry_thickness_mm" not in raw:
            raise ValueError("explicit_override requires geometry_thickness_mm")
        value = raw["geometry_thickness_mm"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("geometry_thickness_mm must be a positive finite number")
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("geometry_thickness_mm must be a positive finite number")
        normalized["geometry_thickness_mm"] = value

    exposure = raw.get("display_exposure", 0.01)
    if isinstance(exposure, bool) or not isinstance(exposure, (int, float)):
        raise ValueError("display_exposure must be a positive finite number")
    exposure = float(exposure)
    if not math.isfinite(exposure) or exposure <= 0:
        raise ValueError("display_exposure must be a positive finite number")
    normalized["display_exposure"] = exposure
    return normalized


def _guard_local_scheduler(service) -> None:
    """Reject any injected scheduler; this executor only supports the local client."""
    for field in ("_scheduler", "scheduler"):
        try:
            transport = getattr(service, field, None)
        except Exception as exc:
            raise ValueError("appearance_render cannot inspect the configured scheduler") from exc
        if transport is not None:
            raise ValueError("appearance_render requires the project-local SchedulerClient")


def _hidden_child_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "cwd": str(Path(__file__).resolve().parents[2]),
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "check": False,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return options


def _freeze_in_child(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    from coat_sim.process_entry import module_command

    input_payload = {
        "schema": "coat_sim.appearance_freeze_input",
        "schema_version": 1,
        "root": str(root),
        "request": request,
    }
    input_bytes = (_canonical_json(input_payload) + "\n").encode("utf-8")
    if len(input_bytes) > MAX_APPEARANCE_JSON_BYTES:
        raise ValueError("appearance freeze input exceeds the 16 MB limit")

    with tempfile.TemporaryDirectory(prefix="coat-appearance-freeze-") as directory:
        temporary = Path(directory)
        input_path = temporary / "input.json"
        output_path = temporary / "output.json"
        input_path.write_bytes(input_bytes)
        command = module_command(
            "coat_sim.core.appearance_freeze_worker",
            "--input", str(input_path), "--output", str(output_path),
        )
        # Asset verification/freezing is offline work too. A live fixed child
        # has no wall-time deadline; subprocess.run still observes its exit and
        # the return code/output checks below preserve real failure detection.
        result = subprocess.run(command, **_hidden_child_options())
        if result.returncode != 0:
            raise ValueError("appearance_preflight_failed: isolated freeze worker rejected the request")
        try:
            output_size = output_path.stat().st_size
            if output_size > MAX_APPEARANCE_FROZEN_BYTES:
                raise ValueError("appearance freeze output exceeds the 60 MB limit")
            envelope = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("appearance_preflight_failed: freeze worker output is invalid") from exc
        if not isinstance(envelope, dict):
            raise ValueError("appearance_preflight_failed: freeze worker output is not an object")
        return envelope


def validate_frozen(service, envelope: Any) -> dict[str, Any]:
    """Validate an internal frozen envelope without accepting it as public input."""
    _guard_local_scheduler(service)
    if type(envelope) is not dict or set(envelope) != _FROZEN_FIELDS:
        raise ValueError("appearance frozen request has missing or unknown fields")
    if (envelope.get("schema") != "coat_sim.appearance_frozen_request"
            or type(envelope.get("schema_version")) is not int
            or envelope["schema_version"] != 1):
        raise ValueError("unsupported appearance frozen request schema")

    root = _root_path(service.root)
    if envelope.get("root") != str(root):
        raise ValueError("appearance frozen request belongs to a different project root")
    frozen_request = envelope.get("request")
    request = _normalize_public_request(
        root, frozen_request, service=service,
        preserve_legacy_backend=(type(frozen_request) is dict
                                 and "backend" not in frozen_request
                                 and "precision" not in frozen_request))
    if _canonical_json(request) != _canonical_json(envelope["request"]):
        raise ValueError("appearance frozen request is not in canonical form")

    context = envelope.get("context")
    context_fields = {
        "schema", "version", "asset_path", "asset_sha256", "asset_root",
        "process_profile", "process_profile_sha256", "substrate_profile",
        "captured_step_ids", "layer_field_units", "layer_field_coordinate_frame",
    }
    if (type(context) is not dict or set(context) != context_fields
            or context.get("schema") != "coat_sim.appearance_launch_context"
            or type(context.get("version")) is not int or context["version"] != 1):
        raise ValueError("appearance frozen context is invalid")
    if (context.get("asset_path") != request["asset_path"]
            or context.get("asset_root") != str(root)
            or context.get("asset_sha256") != request["asset_sha256"]):
        raise ValueError("appearance frozen context does not bind the requested asset and root")
    process_profile = context.get("process_profile")
    if (not isinstance(process_profile, dict)
            or context.get("process_profile_sha256") != _sha256_json(process_profile)):
        raise ValueError("appearance frozen process-profile digest does not match")
    if envelope.get("context_sha256") != _sha256_json(context):
        raise ValueError("appearance frozen context digest does not match")
    if not isinstance(context.get("substrate_profile"), dict):
        raise ValueError("appearance frozen substrate profile is invalid")
    step_ids = context.get("captured_step_ids")
    if not isinstance(step_ids, list) or any(not isinstance(item, str) for item in step_ids):
        raise ValueError("appearance frozen captured step list is invalid")

    job = envelope.get("job")
    if type(job) is not dict or envelope.get("job_sha256") != _sha256_json(job):
        raise ValueError("appearance frozen render-job digest does not match")
    _validate_job_binding(request, job)

    encoded = _canonical_json(envelope).encode("utf-8")
    if len(encoded) > MAX_APPEARANCE_FROZEN_BYTES:
        raise ValueError("appearance frozen request exceeds the 60 MB limit")
    return json.loads(encoded)


def _validate_job_binding(request: dict[str, Any], job: dict[str, Any]) -> None:
    from coat_sim.appearance.contract import validate_job

    backend = request.get("backend", "cpu")
    try:
        validate_job(job, max_wavelength_samples=None if backend == "gpu_cuda" else 20_000_000)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("appearance frozen render job failed contract validation") from exc
    inputs = request["render_inputs"]
    for key in ("camera", "area_source", "environment_spectrum", "resolution"):
        if _canonical_json(job.get(key)) != _canonical_json(inputs[key]):
            raise ValueError("appearance frozen render job differs from the canonical request")
    try:
        expected_wavelengths = [float(value) for value in inputs["wavelength_nm"]]
        actual_wavelengths = [float(value) for value in job["wavelength_nm"]]
    except (TypeError, ValueError, OverflowError, KeyError) as exc:
        raise ValueError("appearance frozen wavelength binding is invalid") from exc
    if actual_wavelengths != expected_wavelengths:
        raise ValueError("appearance frozen wavelengths differ from the canonical request")

    substrate = job.get("substrate")
    outline = substrate.get("outline_kind") if isinstance(substrate, dict) else None
    requested_circle_policy = inputs.get("circle_boundary_sampling")
    if outline == "circle":
        coated = bool(job.get("front_layers") or substrate.get("back_surface", {}).get("layers"))
        if coated and requested_circle_policy != _CIRCLE_POLICY:
            raise ValueError("coated circle requires explicit circle_boundary_sampling")
        if substrate.get("circle_boundary_sampling") != requested_circle_policy:
            raise ValueError("appearance frozen circle sampling differs from the canonical request")
    elif requested_circle_policy is not None:
        raise ValueError("circle_boundary_sampling is only valid for a circle")

    provenance = job.get("provenance")
    if (not isinstance(provenance, dict)
            or provenance.get("process_asset_sha256") != request["asset_sha256"]):
        raise ValueError("appearance frozen job does not bind the requested process asset")
    substrate = job.get("substrate")
    if not isinstance(substrate, dict):
        raise ValueError("appearance frozen job has no substrate")
    if request["geometry_thickness_mode"] == "explicit_override":
        if (substrate.get("solid_thickness_provenance") != "explicit_geometry_input"
                or substrate.get("solid_thickness_mm") != request["geometry_thickness_mm"]):
            raise ValueError("appearance frozen job does not bind the explicit geometry override")
    elif substrate.get("solid_thickness_provenance") != "film_design_snapshot/design_value":
        raise ValueError("appearance frozen job does not use the frozen FilmDesign geometry snapshot")


def canonical_appearance_parameters(service, supplied: dict[str, Any]) -> dict[str, Any]:
    """Freeze a public request or validate a previously frozen internal package."""
    _guard_local_scheduler(service)
    if type(supplied) is not dict:
        raise ValueError("appearance_render parameters must be an object")
    if set(supplied) == {"request"}:
        root = _root_path(service.root)
        request = _normalize_public_request(root, supplied["request"], service=service)
        envelope = _freeze_in_child(root, request)
        return {"frozen": validate_frozen(service, envelope)}
    if set(supplied) == {"frozen"}:
        return {"frozen": validate_frozen(service, supplied["frozen"])}
    raise ValueError("appearance_render requires exactly request or frozen")


def build_frozen_request(root: str | os.PathLike[str], request: dict[str, Any],
                         cancel_event=None) -> dict[str, Any]:
    """Re-read the asset and reconstruct the frozen job inside the isolated executor."""
    root_path = _root_path(root)
    canonical_request = _normalize_public_request(
        root_path, request,
        preserve_legacy_backend=(type(request) is dict and "backend" not in request
                                 and "precision" not in request))
    if _canonical_json(canonical_request) != _canonical_json(request):
        raise ValueError("executor appearance request must be canonical")
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("appearance render cancelled before frozen-input rebuild")
    from .appearance_freeze_worker import build_frozen_envelope

    rebuilt = build_frozen_envelope(root_path, canonical_request, cancel_event=cancel_event)
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("appearance render cancelled after frozen-input rebuild")
    return rebuilt


def estimate_appearance(service, request: dict[str, Any]) -> dict[str, Any]:
    """Create the standard persisted estimate; runtime remains unknown until measured."""
    _guard_local_scheduler(service)
    parameters = canonical_appearance_parameters(service, {"request": request})
    return service.estimate_compute_task(KIND, parameters)


def submit_appearance(service, request: dict[str, Any],
                      client_request_id: str | None = None) -> dict[str, Any]:
    """Bind or release an appearance estimate through the shared compute protocol."""
    _guard_local_scheduler(service)
    from .compute_tasks import ComputeTaskStore, digest
    from coat_sim.scheduler.submission import submit_confirmed

    # Reject a malformed retry key before paying for an isolated asset freeze.
    if client_request_id is not None and (
            not isinstance(client_request_id, str)
            or not 1 <= len(client_request_id) <= 512):
        raise ValueError("client_request_id must contain 1 to 512 characters")
    parameters = canonical_appearance_parameters(service, {"request": request})
    from .compute_estimation import input_file_fingerprints
    input_files = input_file_fingerprints(service, KIND, parameters)
    identity = digest({"kind": KIND, "parameters": parameters, "input_files": input_files})
    if client_request_id is None:
        client_request_id = "appearance:" + identity

    store = ComputeTaskStore(service.root)
    prior = store.find_request(client_request_id)
    if prior is not None and prior["input_digest"] != identity:
        raise ValueError("request_id_conflict: appearance request inputs changed")
    if prior is None:
        key = store.claim_unbound_estimate(client_request_id, identity)
        if key is not None:
            prior = store.read_estimate(key)
    if prior is None:
        receipt = submit_confirmed(service, KIND, parameters, client_request_id)
    elif prior["state"] in {"estimated", "pending"}:
        receipt = {
            **store.public_estimate(prior),
            "state": "prepared" if prior["state"] == "estimated" else "awaiting_confirmation",
            "needs_release_request": prior["state"] == "estimated",
            "needs_user_confirmation": prior.get("release_policy") == "manual",
            "next_action": "request_compute_confirmation",
        }
    else:
        receipt = submit_confirmed(
            service, KIND, deepcopy(parameters), client_request_id,
            estimate_id=prior["estimate_id"],
        )
    return {
        **receipt,
        "client_request_id": client_request_id,
        "estimate_id": receipt.get("estimate_id") or (prior or {}).get("estimate_id"),
    }
