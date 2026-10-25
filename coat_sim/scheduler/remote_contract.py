"""Strict admission helpers for the external Compute Service API v1.

This module validates the frozen JSON boundary. It intentionally does not
claim that every physical parameter combination has been checked; executors
remain responsible for domain-level validation at execution time.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import NoReturn

from .jobs import JobSpec, canonical, MAX_RAYTRACE_SPEC_BYTES

API_VERSION = 1
EXECUTOR_SCHEMA_VERSION = 1
MAX_SPEC_BYTES = MAX_RAYTRACE_SPEC_BYTES  # 20 MiB, matching frozen ray jobs.
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ALLOWED = frozenset({"process", "process_recipe", "single_source",
    "mask_optimization", "film_optimization", "film_synthesis", "mask_weights", "mask_field",
    "point_weights", "geometric_raytrace", "appearance_render"})
REMOTE_EXECUTORS = _ALLOWED
DEVICE_BINDING_VERSION = 1
_BOUND_EXECUTORS = frozenset({
    "process_recipe", "film_optimization", "film_synthesis", "appearance_render"})


class RemoteContractError(ValueError):
    """Stable, machine-readable rejection of an external submission."""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _fail(code: str, message: str) -> NoReturn:
    raise RemoteContractError(code, message)


def _unique_object(pairs):
    result = {}
    for key, item in pairs:
        if key in result:
            _fail("duplicate_json_key", f"JSON object contains duplicate key {key!r}")
        result[key] = item
    return result


def _finite_json(value, *, path="$", depth=0, budget=None):
    if budget is None:
        budget = [200000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 64:
        _fail("json_complexity_limit", f"{path}: JSON structure exceeds supported limits")
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        if abs(value) > 2**53:
            _fail("invalid_json_number", f"{path}: integer is outside the exact JSON number range")
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail("invalid_json_number", f"{path}: number must be finite")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _finite_json(item, path=f"{path}[{i}]", depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("invalid_json_object", f"{path}: object keys must be strings")
            _finite_json(item, path=f"{path}.{key}", depth=depth + 1, budget=budget)
        return
    _fail("invalid_json_value", f"{path}: value is not JSON data")


def _absolute_path(value: str) -> bool:
    # Detect path-shaped strings, while permitting ordinary labels containing ':'
    # (for example "Al:SiO2") and URI-like text that is not used as a path.
    return (value.startswith(("\\\\", "//", "/")) or
            bool(re.match(r"^[A-Za-z]:[\\/]", value)))


def _reject_provenance_and_paths(value, *, path="$", key=""):
    if isinstance(value, dict):
        for name, child in value.items():
            folded = name.casefold()
            if folded == "original_path" or folded in {"local_path", "executable_path", "python_path"}:
                _fail("nonportable_path", f"{path}.{name}: local path/provenance is not accepted")
            _reject_provenance_and_paths(child, path=f"{path}.{name}", key=name)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _reject_provenance_and_paths(child, path=f"{path}[{i}]", key=key)
    elif isinstance(value, str) and ("path" in key.casefold() or key.casefold() in {"file", "filename"}):
        if _absolute_path(value):
            _fail("nonportable_path", f"{path}: local filesystem paths are not accepted")


def _fields(value, required, optional=(), where="parameters"):
    if not isinstance(value, dict):
        _fail("invalid_executor_schema", f"{where}: expected an object")
    missing, extra = set(required) - set(value), set(value) - set(required) - set(optional)
    if missing or extra:
        _fail("invalid_executor_schema", f"{where}: missing={sorted(missing)} unsupported={sorted(extra)}")


def _map(value, where):
    if not isinstance(value, dict):
        _fail("invalid_executor_schema", f"{where}: expected an object")


def _input_files(inputs, expected):
    if not isinstance(inputs, dict) or set(inputs) != {"files"}:
        _fail("invalid_executor_inputs", "inputs must contain only files")
    files = inputs["files"]
    if not isinstance(files, list) or len(files) > 128:
        _fail("invalid_executor_inputs", "inputs.files must be an array of at most 128 records")
    seen = set()
    for i, row in enumerate(files):
        where = f"inputs.files[{i}]"
        if not isinstance(row, dict) or set(row) != {"kind", "path", "data", "sha256"}:
            _fail("invalid_executor_inputs", f"{where}: unsupported record shape")
        kind, name, data, digest = row["kind"], row["path"], row["data"], row["sha256"]
        if not isinstance(kind, str) or kind not in expected:
            _fail("invalid_executor_inputs", f"{where}.kind: unsupported design kind")
        if (not isinstance(name, str) or not name or len(name) > 128 or
                name in {".", ".."} or "/" in name or "\\" in name or
                any(ord(c) < 32 or c in ':<>"|?*' for c in name) or name.endswith((".", " ")) or
                name.split(".", 1)[0].upper() in ({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} |
                    {f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)})):
            _fail("invalid_logical_name", f"{where}.path: expected a safe logical filename")
        folded = name.casefold()
        if folded in seen:
            _fail("duplicate_logical_name", f"{where}.path: duplicate logical filename")
        seen.add(folded)
        if not isinstance(data, dict) or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            _fail("invalid_executor_inputs", f"{where}: data or sha256 is invalid")
        actual = hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()
        if actual != digest:
            _fail("input_digest_mismatch", f"{where}: data SHA-256 does not match")
        try:
            from coat_sim.core.input_snapshot import _VALIDATORS
            from coat_sim.core.design_files import design_extension
            if not name.casefold().endswith(design_extension(kind).casefold()):
                raise ValueError("wrong extension")
            _VALIDATORS[kind](data)
        except Exception:
            _fail("invalid_design_input", f"{where}: typed design data failed validation")
    # Resolve machine references only against the submitted logical records.
    by_name = {row["path"]: row for row in files}
    for i, row in enumerate(files):
        if row["kind"] == "machine":
            for collection in ("sources", "masks"):
                entries = row["data"].get(collection, [])
                if not isinstance(entries, list):
                    _fail("invalid_executor_inputs", f"inputs.files[{i}].data.{collection}: expected array")
                for j, entry in enumerate(entries):
                    ref = entry.get("file") if isinstance(entry, dict) else None
                    expected_kind = "source" if collection == "sources" else "mask"
                    if (not isinstance(ref, str) or ref not in by_name or
                            by_name[ref]["kind"] != expected_kind):
                        _fail("unresolved_input_reference", f"inputs.files[{i}].data.{collection}[{j}].file is unresolved")


def _validate_spec(spec: JobSpec, *, allow_probe: bool, bound: bool = False):
    executor, p, inputs = spec.executor, spec.parameters, spec.inputs
    if bound:
        if executor not in _BOUND_EXECUTORS:
            _fail("unsupported_device_executor", "executor has no device-bound remote route")
        binding = inputs.get("device_binding")
        if not isinstance(binding, dict):
            raise RemoteContractError(
                "invalid_device_binding", "frozen input has no valid device binding")
        if (set(binding) != {"schema_version", "device_id"}
                or type(binding.get("schema_version")) is not int
                or binding["schema_version"] != DEVICE_BINDING_VERSION
                or not isinstance(binding.get("device_id"), str)
                or not 1 <= len(binding["device_id"]) <= 120):
            _fail("invalid_device_binding", "frozen input has no valid device binding")
        if executor == "film_synthesis" and binding["device_id"] != "cpu:default":
            _fail("unsupported_device_binding", "film synthesis supports only cpu:default")
        inputs = {key: value for key, value in inputs.items() if key != "device_binding"}
    elif "device_binding" in inputs:
        _fail("invalid_device_binding", "device binding requires the extended request envelope")
    if executor == "probe":
        if not allow_probe:
            _fail("executor_not_allowed", "probe is available only in isolated test mode")
        _fields(p, (), {"delay_s", "payload_bytes"}, "parameters")
        if set(p) - {"delay_s", "payload_bytes"}:
            _fail("executor_not_allowed", "probe crash/failure injection is forbidden")
        delay, size = p.get("delay_s", 0), p.get("payload_bytes", 0)
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 30:
            _fail("invalid_probe_parameter", "probe delay_s must be between 0 and 30")
        if type(size) is not int or not 0 <= size <= 16 * 1024 * 1024:
            _fail("invalid_probe_parameter", "probe payload_bytes must be between 0 and 16777216")
        if inputs:
            _fail("invalid_executor_inputs", "probe inputs must be empty")
        return
    if executor not in _ALLOWED:
        _fail("executor_not_allowed", f"executor {executor!r} is not enabled for remote submission")

    if executor == "appearance_render":
        if not bound:
            _fail("device_binding_required", "appearance requires an explicit bound remote device")
        _fields(p, {"request"})
        _fields(inputs, {"appearance_artifact"}, where="inputs")
        ref = inputs["appearance_artifact"]
        if (not isinstance(ref, dict) or set(ref) != {"artifact_id", "sha256"}
                or any(not isinstance(ref.get(k), str) or not _SHA256.fullmatch(ref[k])
                       for k in ("artifact_id", "sha256"))):
            _fail("invalid_artifact_reference", "appearance asset ID and SHA-256 are required")
        request = p["request"]
        allowed = {"render_inputs", "geometry_thickness_mode", "geometry_thickness_mm",
                   "display_exposure", "backend", "precision"}
        if (not isinstance(request, dict) or "render_inputs" not in request
                or set(request) - allowed
                or (request.get("backend"), request.get("precision"), binding["device_id"])
                   not in {("cpu", "native", "cpu:default"),
                           ("gpu_cuda", "mixed", "cuda:default"),
                           ("gpu_cuda", "float32", "cuda:default")}
                or not isinstance(request["render_inputs"], dict)):
            _fail("invalid_appearance_request", "appearance request must be path-free and match its device")
        return

    if executor in {"process", "process_recipe"}:
        _fields(p, {"settings", "process_path"}, {"backend", "fast_cycle", "fast_cycle_residual_percent", "algorithm", "algorithm_options", "include_layer_fields"})
        _validate_settings(p["settings"])
        if not isinstance(p["process_path"], dict) or set(inputs) != {"snapshot"}:
            _fail("invalid_executor_inputs", "process jobs require a frozen process snapshot")
        _validate_snapshot(p["process_path"])
        _validate_snapshot(inputs["snapshot"])
        if p["process_path"] != inputs["snapshot"]:
            _fail("invalid_executor_inputs", "process_path and inputs.snapshot must match")
        for k in ("backend", "algorithm"):
            if k in p and not isinstance(p[k], str): _fail("invalid_executor_schema", f"parameters.{k}: expected string")
        if bound and p.get("backend") != "CPU":
            _fail("device_backend_mismatch", "bound deposition CPU requires the explicit CPU backend")
        if "algorithm_options" in p: _validate_recipe_options(p["algorithm_options"])
        for k in ("fast_cycle", "include_layer_fields"):
            if k in p and type(p[k]) is not bool: _fail("invalid_executor_schema", f"parameters.{k}: expected boolean")
        if "fast_cycle_residual_percent" in p: _number(p["fast_cycle_residual_percent"], "parameters.fast_cycle_residual_percent")
        return
    if executor == "mask_optimization":
        _fields(p, {"settings", "process_path"}, {"options"})
        _validate_settings(p["settings"])
        if "options" in p: _validate_mask_optimization_options(p["options"])
        if not isinstance(p["process_path"], dict) or set(inputs) != {"snapshot"}:
            _fail("invalid_executor_inputs", "mask optimization requires a frozen process snapshot")
        _validate_snapshot(p["process_path"]); _validate_snapshot(inputs["snapshot"])
        if p["process_path"] != inputs["snapshot"]: _fail("invalid_executor_inputs", "process snapshots must match")
        return
    if executor == "single_source":
        _fields(p, {"settings"}, {"machine_path", "source_slot", "substrate_path", "source_path", "mask_paths", "backend"})
        _validate_settings(p["settings"])
        for k in ("machine_path", "substrate_path", "source_path"):
            if k in p and p[k] is not None and not isinstance(p[k], str): _fail("invalid_executor_schema", f"parameters.{k}: expected logical name")
        if "mask_paths" in p and (not isinstance(p["mask_paths"], list) or any(not isinstance(x, str) for x in p["mask_paths"])): _fail("invalid_executor_schema", "parameters.mask_paths: expected logical-name array")
        if "source_slot" in p and (type(p["source_slot"]) is not int or p["source_slot"] < 1): _fail("invalid_executor_schema", "parameters.source_slot: expected positive integer")
        _input_files(inputs, {"machine", "source", "mask", "substrate"})
        _ensure_logical_refs(p, inputs["files"])
        return
    if executor in {"mask_weights", "mask_field", "point_weights"}:
        allowed = {
            "mask_weights": ({"settings", "machine_path"}, {"source_slot", "mask_slot", "mask_data", "substrate_path", "radii_mm"}),
            "mask_field": ({"settings", "machine_path"}, {"source_slot", "mask_slot", "mask_data", "substrate_path", "ring_radii_mm"}),
            "point_weights": ({"settings", "machine_path", "mask_z_mm", "point_xy_mm", "patch_radius_mm"}, {"source_slot", "substrate_path", "radii_mm"}),
        }
        required, optional = allowed[executor]; _fields(p, required, optional)
        _validate_settings(p["settings"])
        _input_files(inputs, {"machine", "source", "mask", "substrate"})
        _ensure_logical_refs(p, inputs["files"])
        for key in ("source_slot", "mask_slot"):
            if key in p and (type(p[key]) is not int or p[key] < 1): _fail("invalid_executor_schema", f"parameters.{key}: expected positive integer")
        for key in ("radii_mm", "ring_radii_mm", "point_xy_mm"):
            if key in p: _number_list(p[key], f"parameters.{key}")
        for key in ("mask_z_mm", "patch_radius_mm"):
            if key in p: _number(p[key], f"parameters.{key}")
        if "mask_data" in p:
            try:
                from coat_sim.core.machine_profiles import validate_mask_profile
                validate_mask_profile(p["mask_data"])
            except Exception:
                _fail("invalid_executor_schema", "parameters.mask_data: mask profile failed validation")
        return
    if executor == "film_optimization":
        _fields(p, {"payload"})
        request = _validate_film_payload(p["payload"])
        _validate_film_inputs(inputs, request)
        return
    if executor == "film_synthesis":
        _fields(p, {"payload"})
        request = _validate_film_synthesis_payload(p["payload"])
        _validate_film_inputs(inputs, request)
        return
    if executor == "geometric_raytrace":
        _fields(p, {"request"})
        _fields(inputs, (), {"deposition_artifact"}, "inputs")
        if "deposition_artifact" in inputs:
            ref = inputs["deposition_artifact"]
            if not isinstance(ref, dict) or set(ref) != {"artifact_id", "sha256"} or not all(isinstance(ref[k], str) and _SHA256.fullmatch(ref[k]) for k in ref):
                _fail("invalid_artifact_reference", "deposition_artifact must contain artifact_id and sha256 lowercase digests")
        from coat_sim.core.geometric_raytrace import _read_request, request_limits
        try:
            _read_request(p["request"])
            request_limits(p["request"])
        except Exception as exc:
            _fail("invalid_raytrace_request", "geometric raytrace request failed schema validation")
        from coat_sim.core.ray_trace_job import request_uses_deposition_fields
        needs_artifact = request_uses_deposition_fields(p["request"])
        if needs_artifact != ("deposition_artifact" in inputs):
            _fail("invalid_artifact_reference", "deposition_artifact must be supplied exactly when layer fields are referenced")
        return


def _number(value, where):
    if type(value) not in (int, float) or not math.isfinite(value):
        _fail("invalid_executor_schema", f"{where}: expected a finite number")
    return value


def _validate_settings(value):
    if not isinstance(value, dict):
        _fail("invalid_executor_schema", "parameters.settings: expected an object")
    try:
        from coat_sim.core.simulator import Settings
        from dataclasses import fields
        for field in fields(Settings):
            if field.name not in value:
                continue
            item, default = value[field.name], field.default
            expected = type(default) if default is not None else None
            if default is None:
                if item is not None:
                    if field.name in {"gear_module_mm", "source_rate_nm_s", "beam_attenuation_m_inv"}:
                        if type(item) not in (int, float): raise TypeError(field.name)
                    elif not isinstance(item, dict):
                        raise TypeError(field.name)
            elif expected is int and type(item) is not int:
                raise TypeError(field.name)
            elif expected is float and type(item) not in (int, float):
                raise TypeError(field.name)
            elif expected is not None and expected not in (int, float) and type(item) is not expected:
                raise TypeError(field.name)
        Settings(**value).validate()
    except Exception:
        _fail("invalid_executor_schema", "parameters.settings: unsupported setting field or type")


def _number_list(value, where):
    if not isinstance(value, list) or not value or any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
        _fail("invalid_executor_schema", f"{where}: expected a non-empty array of finite numbers")


def _validate_recipe_options(value):
    try:
        from coat_sim.core.schemas import RecipeAlgorithmOptions
        RecipeAlgorithmOptions.model_validate({} if value is None else value, strict=True)
    except Exception:
        _fail("invalid_executor_schema", "parameters.algorithm_options: unknown or invalid option")


def _validate_mask_optimization_options(value):
    try:
        from coat_sim.core.schemas import MaskOptimizationOptions
        MaskOptimizationOptions.model_validate({} if value is None else value, strict=True)
    except Exception:
        _fail("invalid_executor_schema", "parameters.options: unknown or invalid optimization option")


def _ensure_logical_refs(parameters, rows):
    by_name = {row["path"]: row for row in rows}
    used = set()
    expected = {"machine_path": "machine", "substrate_path": "substrate", "source_path": "source"}
    for key in ("machine_path", "substrate_path", "source_path"):
        if parameters.get(key) is not None and not isinstance(parameters[key], str):
            _fail("invalid_executor_schema", f"parameters.{key}: expected a logical filename")
        if parameters.get(key) is not None and (parameters[key] not in by_name or by_name[parameters[key]]["kind"] != expected[key]):
            _fail("unresolved_input_reference", f"parameters.{key} does not name a submitted input")
        if parameters.get(key) is not None: used.add(parameters[key])
    mask_paths = parameters.get("mask_paths", [])
    if not isinstance(mask_paths, list) or any(not isinstance(name, str) for name in mask_paths):
        _fail("invalid_executor_schema", "parameters.mask_paths must be a logical filename array")
    if any(name not in by_name or by_name[name]["kind"] != "mask" for name in mask_paths):
        _fail("unresolved_input_reference", "parameters.mask_paths contains an unknown input")
    used.update(mask_paths)
    for row in rows:
        if row["kind"] != "machine": continue
        for collection in ("sources", "masks"):
            for entry in row["data"].get(collection, []):
                if isinstance(entry, dict) and isinstance(entry.get("file"), str):
                    used.add(by_name[entry["file"]]["path"])
    used_names = {name.casefold() for name in used}
    if used_names != {name.casefold() for name in by_name}:
        _fail("unreferenced_input", "inputs.files contains an unreferenced design record")


def _validate_snapshot(snapshot):
    try:
        from coat_sim.core.input_snapshot import runtime_from_snapshot
        runtime_from_snapshot(snapshot)
    except Exception:
        _fail("invalid_frozen_snapshot", "frozen process snapshot failed validation")


def _validate_film_payload(payload):
    from coat_sim.core.film_optimization_job import CONTRACT_VERSION, JOB_KIND, deserialize_request
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "kind", "request"} or
            type(payload.get("schema_version")) is not int or payload.get("schema_version") != CONTRACT_VERSION or
            payload.get("kind") != JOB_KIND):
        _fail("invalid_film_payload", "film payload kind, version or fields are invalid")
    request = payload["request"]
    if isinstance(request, dict) and request.get("kind") == "coat_sim.film_reduction_request":
        try:
            from coat_sim.core.film_reduction import film_reduction_request_from_payload
            reduction_request = film_reduction_request_from_payload(request)
            if reduction_request.base_request.frozen_material_snapshot is None:
                raise ValueError("reduction requires frozen materials")
            return reduction_request
        except Exception:
            _fail("invalid_film_payload", "film reduction request or frozen material snapshot is invalid")
    required = {"schema_version", "kind", "design_snapshot", "design_revision", "variables", "objectives", "max_iterations", "max_evaluations", "initial_step_fraction", "min_step_fraction", "acceptable_normalized_error", "frozen_material_snapshot"}
    if (not isinstance(request, dict) or not required <= set(request) or
            type(request.get("schema_version")) is not int or request.get("schema_version") != CONTRACT_VERSION or
            type(request.get("design_revision")) is not int or
            type(request.get("max_iterations")) is not int or type(request.get("max_evaluations")) is not int or
            not isinstance(request.get("variables"), list) or not isinstance(request.get("objectives"), list)):
        _fail("invalid_film_payload", "film optimization request fields or scalar types are invalid")
    try:
        parsed = deserialize_request(request)
        from coat_sim.core.film_optimization import FilmOptimizationRequest
        if not isinstance(parsed, FilmOptimizationRequest):
            _fail("invalid_film_payload", "film optimization requires a thickness request")
        material_snapshot = parsed.frozen_material_snapshot
        if material_snapshot is None:
            _fail("invalid_film_payload", "film optimization requires frozen material inputs")
        from coat_sim.core.film_design import validate_design
        from coat_sim.core.film_optimization import _checked_frozen_snapshot
        design = validate_design(parsed.design_snapshot)
        _checked_frozen_snapshot(material_snapshot, design)
        return parsed
    except RemoteContractError:
        raise
    except Exception:
        _fail("invalid_film_payload", "film optimization request or frozen material snapshot is invalid")


def _validate_film_synthesis_payload(payload):
    from coat_sim.core.film_synthesis_jobs import deserialize_job_payload
    from coat_sim.core.film_synthesis_io import prepare_film_synthesis_payload
    try:
        _request_id, request, _metadata = deserialize_job_payload(payload)
        canonical, _ = prepare_film_synthesis_payload(None, payload["request"])
        if canonical["request"] != payload["request"]["request"]:
            _fail("invalid_film_synthesis_payload", "film synthesis payload is not canonical")
        return request
    except RemoteContractError:
        raise
    except Exception:
        _fail("invalid_film_synthesis_payload", "film synthesis request or frozen materials are invalid")


def _validate_film_inputs(inputs, request):
    # Provenance is immutable request input, not a free-form metadata channel.
    # The existing validator is Qt-free and binds both request and materials.
    from coat_sim.ui.film_optimization_continue_source import SOURCE_INPUT_KEY, validate_source_binding
    if set(inputs) - {SOURCE_INPUT_KEY, "film_warm_restart"}:
        _fail("invalid_executor_inputs", "unsupported film optimization input")
    if SOURCE_INPUT_KEY not in inputs:
        if "film_warm_restart" in inputs:
            _fail("invalid_executor_inputs", "film warm restart requires a source binding")
        return
    try:
        binding = validate_source_binding(inputs[SOURCE_INPUT_KEY], request)
        identity = binding["managed_film"]
        if identity is not None and any(not re.fullmatch(r"[0-9a-f]{32}", identity[key])
                                        for key in ("project_id", "document_id")):
            raise ValueError("film source requires stable project document IDs")
        from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
        delta = binding["synthesis_editable_delta"]
        if type(request) is FilmSynthesisRequest:
            if delta is not None:
                from coat_sim.core.film_continuation_editable_snapshot import restore_editable_snapshot_delta
                frozen = request.frozen_material_snapshot
                if frozen is None:
                    raise ValueError("synthesis source requires frozen materials")
                restored = restore_editable_snapshot_delta(frozen, delta)
                from coat_sim.core.project_dependencies import normalize_optical_material
                for material in restored["materials"]:
                    if normalize_optical_material(material) != material:
                        raise ValueError("synthesis editable material is not canonical")
        elif delta is not None:
            raise ValueError("thickness source cannot contain synthesis editable inputs")
        if "film_warm_restart" in inputs:
            _validate_film_warm_restart(inputs["film_warm_restart"], request)
    except Exception:
        _fail("invalid_executor_inputs", "film source binding does not match the frozen request")


def _validate_film_warm_restart(value, request):
    """Check bounded provenance, not old-job authority or saved optical scores."""
    from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
    from coat_sim.core.film_synthesis_archive import physical_design_hash
    synthesis = type(request) is FilmSynthesisRequest
    required = {"schema_version", "kind", "mode", "solver_state_preserved", "source_kind",
        "original_design_revision", "original_request_sha256", "candidate_physical_hash",
        "spent", "fresh_budget", "saved_training_complete", "physical_replay_performed",
        "independent_audit_performed", "saved_scores_reused", "source_job_id",
        "source_spec_fingerprint"}
    if synthesis:
        required.add("seeds")
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("film warm restart fields are invalid")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != "coat_sim.film_warm_restart"
            or value["mode"] != "warm_restart"
            or value["source_kind"] != ("film_synthesis" if synthesis else "film_optimization")
            or value["saved_training_complete"] is not True
            or any(value[key] is not False for key in ("solver_state_preserved",
                "physical_replay_performed", "independent_audit_performed", "saved_scores_reused"))
            or type(value["original_design_revision"]) is not int
            or value["original_design_revision"] < 0
            or not isinstance(value["source_job_id"], str)
            or not _JOB_ID.fullmatch(value["source_job_id"])
            or any(not isinstance(value[key], str) or not _SHA256.fullmatch(value[key])
                for key in ("original_request_sha256", "source_spec_fingerprint", "candidate_physical_hash"))):
        raise ValueError("film warm restart provenance is invalid")
    spent_keys = ({"cycles", "evaluations", "validation_evaluations", "failed_evaluations",
        "audit_physical_evaluations", "audit_wavelength_points"} if synthesis else
        {"iterations", "evaluations", "failed_evaluations", "validation_evaluations"})
    spent = value["spent"]
    if (not isinstance(spent, dict) or set(spent) != spent_keys
            or any(not (synthesis and counter is None)
                and (type(counter) is not int or counter < 0) for counter in spent.values())):
        raise ValueError("film warm restart work counters are invalid")
    budget_keys = ({"max_cycles", "max_evaluations", "max_seconds"} if synthesis else
                   {"max_iterations", "max_evaluations"})
    budget = value["fresh_budget"]
    if (not isinstance(budget, dict) or set(budget) != budget_keys
            or any(type(budget[key]) is not type(getattr(request, key))
                or budget[key] != getattr(request, key) for key in budget_keys)):
        raise ValueError("film warm restart budget does not match its request")
    candidate = request.design_snapshot if not synthesis else (
        request.seed_designs[0] if request.seed_designs else None)
    if candidate is None or value["candidate_physical_hash"] != physical_design_hash(candidate):
        raise ValueError("film warm restart candidate does not match its request")
    if synthesis:
        seeds = value["seeds"]
        counts = {"archive_added", "original_count", "retained_count", "archive_geometry_capacity"}
        if (not isinstance(seeds, dict) or set(seeds) != counts | {"candidate_first", "saved_scores_reused"}
                or seeds["candidate_first"] is not True or seeds["saved_scores_reused"] is not False
                or any(type(seeds[key]) is not int or seeds[key] < 0 for key in counts)
                or seeds["archive_geometry_capacity"] != request.archive_capacity
                or seeds["archive_added"] >= request.archive_capacity
                or seeds["retained_count"] != len(request.seed_designs)
                or not 1 + seeds["archive_added"] <= seeds["retained_count"] <= (
                    1 + seeds["archive_added"] + seeds["original_count"])
                or type(request.warm_start_seed_index) is not int or request.warm_start_seed_index != 0):
            raise ValueError("film warm restart seeds do not match its request")


def prepare_portable_spec(spec: JobSpec) -> JobSpec:
    """Return a detached path-free spec prepared from already frozen JSON.

    This never reads a design file or otherwise accesses user data. Snapshot
    content hashes remain valid because hashes cover each record's ``data``.
    """
    if isinstance(spec, JobSpec):
        raw = spec.to_dict()
    elif isinstance(spec, Mapping):
        raw = deepcopy(dict(spec))
    else:
        _fail("invalid_job_spec", "spec must be a JobSpec or JSON object")
    def clean(obj):
        if isinstance(obj, dict):
            result = {k: clean(v) for k, v in obj.items() if k.casefold() != "original_path"}
            # Frozen design/input record digests cover canonical data only.
            # Recompute them if portability cleanup changed that data object.
            if isinstance(result.get("data"), dict) and isinstance(result.get("sha256"), str):
                result["sha256"] = hashlib.sha256(canonical(result["data"]).encode("utf-8")).hexdigest()
            return result
        if isinstance(obj, list): return [clean(v) for v in obj]
        return obj
    portable = clean(raw)
    # Embedded runtime is only a cache; it can contain reconstructed profiles.
    def strip_runtime(value):
        if isinstance(value, dict):
            from coat_sim.core.input_snapshot import SNAPSHOT_KIND
            if value.get("kind") == SNAPSHOT_KIND:
                value.pop("runtime", None)
            for child in value.values(): strip_runtime(child)
        elif isinstance(value, list):
            for child in value: strip_runtime(child)
    strip_runtime(portable)
    return JobSpec.parse(portable)


def validate_submission(value, blob_reader, *, allow_probe=False) -> tuple[str, JobSpec, dict]:
    """Validate the exact external API v1 envelope and its content-addressed spec."""
    legacy_fields = {"api_version", "executor_schema_version", "job_id", "spec_blob"}
    bound_fields = legacy_fields | {"device_binding_version", "device_id"}
    from .film_display import CONFIG_KEY, FILM_DISPLAY_EXECUTORS, prepare_display_configuration
    if not isinstance(value, dict) or set(value) - {CONFIG_KEY} not in (legacy_fields, bound_fields):
        _fail("invalid_submission", "submission has missing or unsupported fields")
    bound = set(value) - {CONFIG_KEY} == bound_fields
    if type(value["api_version"]) is not int or value["api_version"] != API_VERSION:
        _fail("unsupported_api_version", "api_version must be 1")
    if type(value["executor_schema_version"]) is not int or value["executor_schema_version"] != EXECUTOR_SCHEMA_VERSION:
        _fail("unsupported_executor_schema_version", "executor_schema_version must be 1")
    if bound and (type(value["device_binding_version"]) is not int
                  or value["device_binding_version"] != DEVICE_BINDING_VERSION):
        _fail("unsupported_device_binding_version", "device binding version must be 1")
    if bound and (not isinstance(value["device_id"], str)
                  or not 1 <= len(value["device_id"]) <= 120):
        _fail("invalid_device_binding", "device ID must be a stable nonempty identifier")
    job_id = value["job_id"]
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        _fail("invalid_job_id", "job_id must be a safe 1-128 character identifier")
    manifest = value["spec_blob"]
    if not isinstance(manifest, dict) or set(manifest) != {"sha256", "bytes"}:
        _fail("invalid_blob_manifest", "spec_blob must contain sha256 and bytes only")
    digest, size = manifest["sha256"], manifest["bytes"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        _fail("invalid_blob_manifest", "spec_blob.sha256 must be lowercase SHA-256 hex")
    if type(size) is not int or not 1 <= size <= MAX_SPEC_BYTES:
        _fail("spec_blob_too_large", "spec_blob.bytes must be between 1 and 20971520")
    try:
        blob = blob_reader(digest)
    except Exception:
        _fail("spec_blob_unavailable", "spec blob is unavailable")
    if not isinstance(blob, bytes) or len(blob) != size:
        _fail("spec_blob_size_mismatch", "spec blob length does not match manifest")
    if hashlib.sha256(blob).hexdigest() != digest:
        _fail("spec_blob_digest_mismatch", "spec blob SHA-256 does not match manifest")
    if len(blob) > MAX_SPEC_BYTES:
        _fail("spec_blob_too_large", "spec blob exceeds 20 MiB")
    try:
        text = blob.decode("utf-8")
        raw = json.loads(text, object_pairs_hook=_unique_object)
        if canonical(raw).encode("utf-8") != blob:
            _fail("noncanonical_job_spec", "spec blob must use canonical UTF-8 JSON encoding")
        frozen = JobSpec.parse_frozen(raw)
    except RemoteContractError:
        raise
    except Exception:
        _fail("invalid_job_spec", "spec blob is not a valid frozen JobSpec JSON document")
    spec = frozen.to_spec()
    _finite_json(spec.to_dict())
    _reject_provenance_and_paths(spec.to_dict())
    _validate_spec(spec, allow_probe=allow_probe, bound=bound)
    if bound and spec.inputs["device_binding"]["device_id"] != value["device_id"]:
        _fail("device_binding_mismatch", "request and frozen input device IDs differ")
    metadata = {"api_version": API_VERSION, "executor_schema_version": EXECUTOR_SCHEMA_VERSION,
                "spec_sha256": digest, "spec_bytes": size,
                "device_id": value["device_id"] if bound else None}
    if CONFIG_KEY in value:
        if spec.executor not in FILM_DISPLAY_EXECUTORS:
            _fail("film_display_not_supported", "executor does not produce optimization candidates")
        try:
            metadata[CONFIG_KEY] = prepare_display_configuration(value[CONFIG_KEY])
        except (ValueError, TypeError, OverflowError):
            _fail("invalid_film_display_configuration", "invalid optional display configuration")
    return job_id, spec, metadata
