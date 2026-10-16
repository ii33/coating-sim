"""Portable, content-addressed appearance input and output adapter.

Only the external *request* crosses the service API.  The large frozen field
snapshot is made on the service host from a checked-in, hash-bound asset, so
its root and path bindings remain meaningful to the canonical queue worker.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import PureWindowsPath
from uuid import uuid4

from .jobs import JobSpec, canonical


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ASSET_DIR = "remote-appearance-assets"
_ASSET_NAME = "input.coatasset"
_OUTPUTS = {
    "display": "display.png",
    "manifest": "manifest.json",
    "radiance_spectrum": "radiance_spectrum.npy",
    "xyz": "xyz.npy",
    "linear_srgb": "linear_srgb.npy",
}
_GPU_INPUT_JOB = "input-job.json"
_MAX_TRANSFER_BYTES = 512 * 1024 * 1024
_MAX_TOTAL_TRANSFER_BYTES = 1024 * 1024 * 1024
_PRIVATE_AUDIT_KEYS = frozenset({
    "password", "credential", "credentials", "secret", "access_token",
    "refresh_token", "api_key", "private_key", "authorization",
    "executable_path", "python_path",
})


def _reject_private_audit_content(value) -> None:
    """Keep the GPU audit copy path-free and credential-free before transfer."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key.casefold() in _PRIVATE_AUDIT_KEYS:
                raise ValueError("appearance_gpu_audit_private_field")
            _reject_private_audit_content(child)
    elif isinstance(value, list):
        for child in value:
            _reject_private_audit_content(child)
    elif isinstance(value, str):
        if value.startswith(("/", "\\\\")) or PureWindowsPath(value).is_absolute():
            raise ValueError("appearance_gpu_audit_absolute_path")


def appearance_runtime_interpreter(root, backend: str) -> str:
    """Select one interpreter for both a backend probe and its queue worker."""
    if backend not in {"cpu", "gpu_cuda"}:
        raise ValueError("appearance_runtime_backend_invalid")
    root = Path(root).resolve(strict=True)
    module_root = Path(__file__).resolve().parents[2]
    for candidate in dict.fromkeys((root, module_root)):
        environment = candidate / "build" / "mitsuba_appearance_env"
        python = (environment / "Scripts" / "python.exe" if os.name == "nt"
                  else environment / "bin" / "python")
        if python.is_file():
            return str(python.resolve())
    return sys.executable


def appearance_cpu_interpreter(root) -> str:
    return appearance_runtime_interpreter(root, "cpu")


def appearance_cuda_interpreter(root) -> str:
    return appearance_runtime_interpreter(root, "gpu_cuda")


def appearance_runtime_environment(root, interpreter: str, backend: str) -> dict[str, str]:
    """Use one explicit module/runtime path for both probe and worker launch."""
    if backend not in {"cpu", "gpu_cuda"}:
        raise ValueError("appearance_runtime_backend_invalid")
    root = Path(root).resolve(strict=True)
    module_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    for key in tuple(env):
        if key.upper().startswith(("PYTHON", "CONDA", "MAMBA")) or key.upper() == "VIRTUAL_ENV":
            env.pop(key, None)
    paths = [str(module_root)]
    # The cloud acceptance workspace uses official pinned Mitsuba/Dr.Jit
    # wheels in a separate venv while keeping application dependencies in the
    # existing test interpreter. This is not an implicit production fallback.
    if Path(interpreter).resolve() == Path(sys.executable).resolve():
        for candidate in dict.fromkeys((root, module_root)):
            isolated = (candidate / ".appearance-runtime-venv" /
                        ("Lib/site-packages" if os.name == "nt" else
                         f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"))
            if isolated.is_dir():
                paths.insert(0, str(isolated))
                break
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONNOUSERSITE"] = "1"
    # The probe's final JSON line is an ASCII protocol even if native libraries
    # emit locale-encoded diagnostics on stderr (not decoded by the parent).
    env["PYTHONIOENCODING"] = "utf-8"
    env["PATH"] = str(Path(interpreter).parent) + os.pathsep + env.get("PATH", "")
    home = root / "artifacts/runtime/appearance-home"
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    return env


def appearance_cpu_environment(root, interpreter: str) -> dict[str, str]:
    return appearance_runtime_environment(root, interpreter, "cpu")


def appearance_cuda_environment(root, interpreter: str) -> dict[str, str]:
    return appearance_runtime_environment(root, interpreter, "gpu_cuda")


def _probe_json_stdout(completed: subprocess.CompletedProcess[bytes]) -> object:
    """Parse only the child-owned final JSON line, never native stderr text."""
    stdout = completed.stdout or b""
    if completed.returncode != 0 or len(stdout) > 8192:
        raise ValueError("appearance_probe_child_failed")
    payload = json.loads(stdout.strip().splitlines()[-1].decode("ascii"))
    if not isinstance(payload, dict):
        raise ValueError("appearance_probe_payload_invalid")
    return payload


def probe_appearance_cpu_runtime(root, *, interpreter: str | None = None) -> tuple[bool, str]:
    """Exercise Mitsuba's actual scalar renderer in a bounded child process.

    A module import or CPU-count estimate is not an appearance capability.
    This returns only a short safe code; interpreter stderr is never surfaced.
    """
    root = Path(root).resolve(strict=True)
    interpreter = interpreter or appearance_cpu_interpreter(root)
    if not Path(interpreter).is_file():
        return False, "appearance_cpu_runtime_missing"
    code = '''import json, math
import numpy, pydantic
import mitsuba as mi
mi.set_variant("scalar_rgb")
scene = mi.load_dict({"type":"scene", "integrator":{"type":"path"},
    "sensor":{"type":"perspective", "film":{"type":"hdrfilm", "width":1, "height":1},
              "sampler":{"type":"independent", "sample_count":1}},
    "environment":{"type":"constant", "radiance":1.0}})
image = mi.render(scene, spp=1)
value = float(numpy.asarray(image, dtype=float).reshape(-1)[0])
if not math.isfinite(value): raise RuntimeError("nonfinite_scalar_render")
mi.set_variant("scalar_spectral")
print(json.dumps({"scalar_render":True,"spectral_variant":mi.variant()}))
'''
    env = appearance_cpu_environment(root, interpreter)
    try:
        completed = subprocess.run(
            [interpreter, "-s", "-P", "-c", code], cwd=str(Path(__file__).resolve().parents[2]),
            env=env, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=30, check=False)
        payload = _probe_json_stdout(completed)
        if payload != {"scalar_render": True, "spectral_variant": "scalar_spectral"}:
            return False, "appearance_cpu_probe_mismatch"
        return True, "appearance_cpu_scalar_verified"
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError, IndexError):
        return False, "appearance_cpu_probe_failed"


def probe_appearance_cuda_runtime(root, *, interpreter: str | None = None) -> tuple[bool, str]:
    """Isolate a real CUDA allocation; an import or driver name is insufficient."""
    root = Path(root).resolve(strict=True)
    interpreter = interpreter or appearance_cuda_interpreter(root)
    if not Path(interpreter).is_file():
        return False, "appearance_cuda_runtime_missing"
    code = '''import json
from coat_sim.appearance.gpu import probe_cuda
value = probe_cuda()
print(json.dumps(value, separators=(",", ":"), allow_nan=False))
'''
    try:
        completed = subprocess.run(
            [interpreter, "-s", "-P", "-c", code],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=appearance_cuda_environment(root, interpreter),
            stdin=subprocess.DEVNULL, capture_output=True,
            timeout=120, check=False)
        payload = _probe_json_stdout(completed)
        if (payload.get("available") is not True
                or payload.get("backend") != "gpu_cuda"
                or payload.get("variant") != "cuda_ad_rgb"):
            return False, "appearance_cuda_probe_mismatch"
        return True, "appearance_cuda_allocation_verified"
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError, IndexError):
        return False, "appearance_cuda_probe_failed"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(code)
    return value


def _safe_asset_path(root: Path, digest: str) -> Path:
    from coat_sim.core.appearance_result import _assert_no_symlink_path

    root = root.resolve(strict=True)
    path = root / "data" / _ASSET_DIR / digest / _ASSET_NAME
    _assert_no_symlink_path(root, path)
    return path


def register_appearance_bundle(service, digest: str, size: int) -> dict:
    """Stage an uploaded .coatasset in data/ and register its immutable ID.

    The caller has already admitted the small /assets control envelope.  We
    still independently verify the bytes, path, size, and asset type here.
    """
    from coat_sim.core.appearance_result import _assert_no_symlink_path
    from coat_sim.core.simulation_assets import read_simulation_asset

    digest = _sha(digest, "invalid_sha256")
    if type(size) is not int or not 1 <= size <= service.blobs.max_bytes:
        raise ValueError("invalid_blob_bytes")
    root = Path(service.root).resolve(strict=True)
    source = service.blobs.path(digest)
    if not source.is_file() or source.stat().st_size != size or _hash_file(source) != digest:
        raise ValueError("blob_integrity_failed")
    target = _safe_asset_path(root, digest)
    _assert_no_symlink_path(root, target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    if target.exists():
        if not target.is_file() or target.stat().st_size != size or _hash_file(target) != digest:
            raise ValueError("appearance_asset_integrity_failed")
    else:
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".coatasset",
                                             delete=False) as output:
                temporary = Path(output.name)
                with source.open("rb") as stream:
                    shutil.copyfileobj(stream, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if temporary.stat().st_size != size or _hash_file(temporary) != digest:
                raise ValueError("appearance_asset_integrity_failed")
            validated = read_simulation_asset(temporary, detail="full", include_arrays=False)
            if not isinstance(validated, dict) or not validated.get("has_results"):
                raise ValueError("appearance_asset_has_no_results")
            # Never replace a previously published digest identity.  If another
            # request won the race, accept it only if its bytes are identical.
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.stat().st_size != size or _hash_file(target) != digest:
                    raise ValueError("appearance_asset_integrity_failed")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    artifact_id = hashlib.sha256(("remote-appearance-input:" + digest).encode()).hexdigest()
    item = {"artifact_id": artifact_id, "sha256": digest, "bytes": size,
            "role": "appearance_bundle", "name": _ASSET_NAME,
            "local_path": str(target)}
    with service.scheduler.condition:
        if service.scheduler.stopping.is_set():
            raise ValueError("scheduler_stopping")
        with service.scheduler.store.queue.connection() as db:
            prior = db.execute("SELECT metadata FROM scheduler_artifacts WHERE artifact_id=?",
                               (artifact_id,)).fetchone()
            if prior is not None:
                if json.loads(prior["metadata"]) != item:
                    raise ValueError("appearance_artifact_identity_conflict")
            else:
                db.execute("INSERT INTO scheduler_artifacts(artifact_id,job_id,metadata) "
                           "VALUES(?,?,?)", (artifact_id, "remote-appearance-input", canonical(item)))
        service.scheduler.artifact_index[artifact_id] = item
    return item


def _portable_request(spec: JobSpec) -> tuple[dict, dict, dict]:
    if (spec.executor != "appearance_render" or set(spec.parameters) != {"request"}
            or type(spec.parameters["request"]) is not dict
            or set(spec.inputs) != {"appearance_artifact", "device_binding"}):
        raise ValueError("invalid_appearance_portable_spec")
    request = spec.parameters["request"]
    if "asset_path" in request or "asset_sha256" in request:
        raise ValueError("appearance_remote_device_unavailable")
    reference, binding = spec.inputs["appearance_artifact"], spec.inputs["device_binding"]
    if (type(reference) is not dict or set(reference) != {"artifact_id", "sha256"}
            or type(binding) is not dict or set(binding) != {"schema_version", "device_id"}
            or binding.get("schema_version") != 1):
        raise ValueError("invalid_appearance_portable_spec")
    supported = {
        ("cpu", "native", "cpu:default"),
        ("gpu_cuda", "mixed", "cuda:default"),
        ("gpu_cuda", "float32", "cuda:default"),
    }
    if (request.get("backend"), request.get("precision"), binding["device_id"]) not in supported:
        raise ValueError("appearance_remote_device_unavailable")
    _sha(reference["artifact_id"], "invalid_artifact_id")
    _sha(reference["sha256"], "invalid_sha256")
    return request, reference, binding


def prepare_appearance_submission(service, spec: JobSpec, job_id: str) -> dict:
    """Resolve a small portable request into the canonical server-local spec.

    A replay with the same ID compares the persisted *logical* request before
    doing another expensive freeze.  The queue's ordinary fingerprint check
    remains the final idempotency gate.
    """
    from coat_sim.core.api import CoatService
    from coat_sim.core.appearance_compute import canonical_appearance_parameters

    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise ValueError("invalid_job_id")
    request, reference, binding = _portable_request(spec)
    root = Path(service.root).resolve(strict=True)
    item, path = service._artifact(reference["artifact_id"])
    expected = _safe_asset_path(root, reference["sha256"])
    if (item.get("role") != "appearance_bundle" or item.get("name") != _ASSET_NAME
            or item.get("sha256") != reference["sha256"]
            or Path(path) != expected or not expected.is_file()
            or type(item.get("bytes")) is not int or item["bytes"] != expected.stat().st_size
            or _hash_file(expected) != reference["sha256"]):
        raise ValueError("appearance_artifact_mismatch")
    server_request = {**deepcopy(request), "asset_path": str(expected),
                      "asset_sha256": reference["sha256"]}
    service_api = CoatService(root)
    # Cheap input validation precedes the potentially long isolated freeze.
    from coat_sim.core.appearance_compute import _normalize_public_request
    normalized = _normalize_public_request(root, server_request, service=service_api)
    if canonical(normalized) != canonical(server_request):
        raise ValueError("noncanonical_appearance_request")

    with service.scheduler.store.queue.connection() as db:
        prior = db.execute("SELECT spec FROM scheduler_specs WHERE job_id=?", (job_id,)).fetchone()
    if prior is not None:
        existing = JobSpec.parse_frozen_json(prior["spec"]).to_spec()
        envelope = existing.parameters.get("frozen") if isinstance(existing.parameters, dict) else None
        if (existing.executor != "appearance_render"
                or existing.inputs != {"device_binding": binding}
                or not isinstance(envelope, dict)
                or envelope.get("request") != normalized):
            raise ValueError("request_id_conflict")
        # Validate the still-present content as the worker will do; this does
        # not rebuild the 54-layer snapshot on an idempotent HTTP retry.
        from coat_sim.core.appearance_compute import validate_frozen
        validate_frozen(service_api, envelope)
        return existing.to_dict()

    parameters = canonical_appearance_parameters(service_api, {"request": normalized})
    frozen = JobSpec("appearance_render", parameters, {"device_binding": binding})
    return JobSpec.parse_frozen(frozen.to_dict()).to_spec().to_dict()


def attach_appearance_output_artifacts(root, job_id: str, result: dict) -> dict:
    """Add fixed verified render files and the CUDA-only input audit copy.

    The caller first registers the ordinary result index with the base
    scheduler. The terminal transition publishes the complete artifact set
    atomically; a failed or cancelled render publishes none of them.
    """
    from coat_sim.core.appearance_result import validate_appearance_render_result

    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise ValueError("invalid_job_id")
    if not isinstance(result, dict) or result.get("kind") != "appearance_render":
        raise ValueError("invalid_appearance_result")
    root = Path(root).resolve(strict=True)
    validate_appearance_render_result(root, job_id, result,
                                      expected_input_digest=result.get("input_digest"),
                                      require_registered_artifact=True)
    existing = result.get("result_artifacts")
    if (not isinstance(existing, list) or len(existing) != 1
            or existing[0].get("role") != "result"):
        raise ValueError("invalid_appearance_artifact_registry")
    references = result.get("outputs")
    if not isinstance(references, dict) or set(references) != set(_OUTPUTS):
        raise ValueError("invalid_appearance_output_references")
    records = []
    for role, name in _OUTPUTS.items():
        reference = references[role]
        path = Path(reference["path"])
        digest, size = reference["sha256"], reference["bytes"]
        if (path.name != name or not 1 <= size <= _MAX_TRANSFER_BYTES
                or _hash_file(path) != digest):
            raise ValueError("appearance_output_transfer_limit_or_digest")
        artifact_role = "appearance_" + role
        records.append({
            "artifact_id": hashlib.sha256(
                (job_id + ":" + artifact_role + ":" + digest).encode()).hexdigest(),
            "role": artifact_role, "sha256": digest, "bytes": size,
            "name": name, "local_path": str(path),
        })
    if result.get("backend") == "gpu_cuda":
        from coat_sim.core.appearance_result import _assert_no_symlink_path

        index_path = Path(result["result_file"])
        index = json.loads(index_path.read_text(encoding="utf-8"))
        record = index.get("files", {}).get(_GPU_INPUT_JOB)
        path = root / "data/tmp/appearance" / job_id / "render" / _GPU_INPUT_JOB
        _assert_no_symlink_path(root, path)
        if (not isinstance(record, dict) or set(record) != {"sha256", "bytes"}
                or type(record.get("bytes")) is not int
                or not 1 <= record["bytes"] <= _MAX_TRANSFER_BYTES
                or not path.is_file() or path.stat().st_size != record["bytes"]
                or _hash_file(path) != record.get("sha256")):
            raise ValueError("appearance_gpu_input_job_transfer_invalid")
        from coat_sim.core.appearance_result import _unique_object, _reject_nonfinite

        audit_job = json.loads(path.read_text(encoding="utf-8"),
                               object_pairs_hook=_unique_object,
                               parse_constant=_reject_nonfinite)
        _reject_private_audit_content(audit_job)
        digest = record["sha256"]
        records.append({
            "artifact_id": hashlib.sha256(
                (job_id + ":appearance_input_job:" + digest).encode()).hexdigest(),
            "role": "appearance_input_job", "sha256": digest,
            "bytes": record["bytes"], "name": _GPU_INPUT_JOB,
            "local_path": str(path),
        })
    if sum(item["bytes"] for item in records) > _MAX_TOTAL_TRANSFER_BYTES:
        raise ValueError("appearance_output_transfer_total_too_large")
    published = dict(result)
    published["result_artifacts"] = list(existing) + records
    return published


def _verify_downloaded_appearance(output_dir: Path, job_id: str, result: dict) -> dict:
    """Independently check the portable six-file package before display."""
    from coat_sim.core.appearance_result import (
        INDEX_SCHEMA, INDEX_SCHEMA_VERSION, _validate_npy, _validate_png,
        _unique_object, _reject_nonfinite,
    )

    index_path = output_dir / "appearance-result.json"
    if index_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("appearance_remote_index_too_large")
    if _hash_file(index_path) != result.get("sha256"):
        raise ValueError("appearance_remote_index_digest_mismatch")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"),
                           object_pairs_hook=_unique_object,
                           parse_constant=_reject_nonfinite)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("appearance_remote_index_invalid") from exc
    expected_backend = result.get("backend")
    expected_precision = result.get("precision")
    if (expected_backend, expected_precision) not in {
            ("cpu", "native"), ("gpu_cuda", "mixed"), ("gpu_cuda", "float32")}:
        raise ValueError("appearance_remote_backend_invalid")
    if (not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA
            or index.get("schema_version") != INDEX_SCHEMA_VERSION
            or index.get("job_id") != job_id
            or result.get("job_id") != job_id
            or index.get("backend") != expected_backend
            or index.get("precision") != expected_precision
            or any(index.get(key) != result.get(key) for key in (
                "input_digest", "request_sha256", "context_sha256", "job_sha256",
                "input_sha256", "resolution", "wavelength_nm", "display_exposure"))):
        raise ValueError("appearance_remote_index_binding_mismatch")
    resolution, waves, records = (index.get("resolution"), index.get("wavelength_nm"),
                                  index.get("files"))
    expected_files = set(_OUTPUTS.values())
    if expected_backend == "gpu_cuda":
        expected_files.add(_GPU_INPUT_JOB)
    if (not isinstance(resolution, dict) or set(resolution) != {"width", "height", "spp", "seed"}
            or any(type(resolution[key]) is not int for key in resolution)
            or resolution["width"] < 1 or resolution["height"] < 1
            or not isinstance(waves, list) or len(waves) < 2
            or not isinstance(records, dict) or set(records) != expected_files):
        raise ValueError("appearance_remote_index_shape_invalid")
    for name in expected_files:
        record = records[name]
        file = output_dir / name
        if (not isinstance(record, dict) or set(record) != {"sha256", "bytes"}
                or _sha(record["sha256"], "invalid_sha256") != _hash_file(file)
                or type(record["bytes"]) is not int or record["bytes"] != file.stat().st_size):
            raise ValueError("appearance_remote_file_digest_mismatch")
    width, height = resolution["width"], resolution["height"]
    _validate_npy(output_dir / "radiance_spectrum.npy", (len(waves), height, width))
    _validate_npy(output_dir / "xyz.npy", (height, width, 3))
    _validate_npy(output_dir / "linear_srgb.npy", (height, width, 3))
    _validate_png(output_dir / "display.png", width, height)
    if expected_backend == "gpu_cuda":
        from coat_sim.core.appearance_result import _unique_object, _reject_nonfinite

        input_job = json.loads((output_dir / _GPU_INPUT_JOB).read_text(encoding="utf-8"),
                               object_pairs_hook=_unique_object,
                               parse_constant=_reject_nonfinite)
        _reject_private_audit_content(input_job)
        if hashlib.sha256(canonical(input_job).encode()).hexdigest() != result["job_sha256"]:
            raise ValueError("appearance_remote_input_job_binding_mismatch")
    manifest_path = output_dir / "manifest.json"
    if manifest_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("appearance_remote_manifest_too_large")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"),
                              object_pairs_hook=_unique_object,
                              parse_constant=_reject_nonfinite)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("appearance_remote_manifest_invalid") from exc
    if (not isinstance(manifest, dict) or manifest.get("input_sha256") != index["input_sha256"]
            or manifest.get("resolution") != resolution
            or manifest.get("wavelength_nm") != waves
            or not isinstance(manifest.get("outputs"), dict)
            or manifest["outputs"].get("display_exposure") != index["display_exposure"]):
        raise ValueError("appearance_remote_manifest_binding_mismatch")
    if expected_backend == "gpu_cuda":
        from coat_sim.appearance.gpu import FATAL_COUNTERS
        wavelengths = manifest.get("per_wavelength")
        if (index.get("render_status") != "rendered_unqualified"
                or index.get("diagnostics_pass") is not True
                or result.get("render_status") != "rendered_unqualified"
                or result.get("diagnostics_pass") is not True
                or manifest.get("status") != "rendered_unqualified"
                or manifest.get("precision") != expected_precision
                or manifest.get("execution", {}).get("variant") != "cuda_ad_rgb"
                or manifest.get("runtime", {}).get("variant") != "cuda_ad_rgb"
                or not isinstance(wavelengths, list) or len(wavelengths) != len(waves)
                or any(not isinstance(row, dict)
                       or row.get("diagnostics", {}).get("pass") is not True
                       or any(row["diagnostics"].get(key) != 0 for key in FATAL_COUNTERS)
                       for row in wavelengths)):
            raise ValueError("appearance_remote_cuda_diagnostics_invalid")
    elif manifest.get("execution", {}).get("backend") != "cpu_scalar":
        raise ValueError("appearance_remote_cpu_execution_invalid")
    return index


def materialize_appearance_result(client, job_id: str, result: dict,
                                  output_dir: Path) -> dict:
    """Download every verified artifact, then publish one complete folder."""
    from coat_sim.core.appearance_result import _assert_no_symlink_path

    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise ValueError("invalid_job_id")
    if not isinstance(result, dict) or result.get("kind") != "appearance_render":
        raise ValueError("invalid_appearance_result")
    status = client.status(job_id)
    if (not isinstance(status, dict) or status.get("state") != "succeeded"
            or status.get("input_digest") != result.get("input_digest")):
        raise ValueError("appearance_remote_job_not_succeeded")
    output_dir = Path(output_dir).absolute()
    root = Path(client.root).resolve(strict=True)
    _assert_no_symlink_path(root, output_dir, include_target=False)
    if not output_dir.is_relative_to(root / "data"):
        raise ValueError("appearance_remote_destination_outside_data")
    if output_dir.exists():
        _verify_downloaded_appearance(output_dir, job_id, result)
        return {"display_path": str(output_dir / "display.png"),
                "index_path": str(output_dir / "appearance-result.json"),
                "output_dir": str(output_dir), "sha256": result["sha256"]}
    artifacts = result.get("result_artifacts")
    expected_roles = {"result": "appearance-result.json", **{
        "appearance_" + role: name for role, name in _OUTPUTS.items()}}
    if result.get("backend") == "gpu_cuda":
        expected_roles["appearance_input_job"] = _GPU_INPUT_JOB
    if (not isinstance(artifacts, list) or len(artifacts) != len(expected_roles)
            or {item.get("role") for item in artifacts if isinstance(item, dict)} != set(expected_roles)):
        raise ValueError("appearance_remote_artifacts_incomplete")
    by_role = {item["role"]: item for item in artifacts}
    if result.get("artifact_id") != by_role["result"].get("artifact_id"):
        raise ValueError("appearance_remote_index_artifact_mismatch")
    if (any(type(item.get("bytes")) is not int for item in artifacts)
            or sum(item["bytes"] for item in artifacts) > _MAX_TOTAL_TRANSFER_BYTES):
        raise ValueError("appearance_remote_transfer_total_too_large")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_path(root, output_dir.parent)
    temporary = output_dir.parent / ("." + output_dir.name + "." + uuid4().hex + ".tmp")
    temporary.mkdir()
    try:
        for role, name in expected_roles.items():
            descriptor = by_role[role]
            digest = _sha(descriptor.get("sha256"), "invalid_sha256")
            artifact_id = _sha(descriptor.get("artifact_id"), "invalid_artifact_id")
            size = descriptor.get("bytes")
            if (descriptor.get("name") != name or type(size) is not int
                    or not 1 <= size <= _MAX_TRANSFER_BYTES):
                raise ValueError("appearance_remote_artifact_descriptor_invalid")
            cached = Path(client.materialize_artifact(artifact_id, descriptor))
            destination = temporary / name
            with cached.open("rb") as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if destination.stat().st_size != size or _hash_file(destination) != digest:
                raise ValueError("appearance_remote_artifact_digest_mismatch")
        _verify_downloaded_appearance(temporary, job_id, result)
        os.replace(temporary, output_dir)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"display_path": str(output_dir / "display.png"),
            "index_path": str(output_dir / "appearance-result.json"),
            "output_dir": str(output_dir), "sha256": result["sha256"]}
