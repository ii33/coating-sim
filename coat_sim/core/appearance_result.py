"""Strict, side-effect-free validation for queued appearance render results.

The completion index is written only after the renderer's five files have been
verified. The scheduler then registers that index as its ``result`` artifact;
result reads compare the registered digest with the file before trusting the
index or any of its output records.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct

import numpy as np
from coat_sim.appearance.limits import (
    MAX_APPEARANCE_FROZEN_BYTES, MAX_APPEARANCE_SPEC_BYTES,
)


RESULT_KIND = "appearance_render"
INDEX_SCHEMA = "coat_sim.appearance_render_completion"
INDEX_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_FILES = ("radiance_spectrum.npy", "xyz.npy", "linear_srgb.npy",
          "display.png", "manifest.json")
_GPU_FILES = (*_FILES, "input-job.json")
_PUBLIC_OUTPUTS = {
    "display": ("display.png", "image/png"),
    "manifest": ("manifest.json", "application/json"),
    "radiance_spectrum": ("radiance_spectrum.npy", "application/x-npy"),
    "xyz": ("xyz.npy", "application/x-npy"),
    "linear_srgb": ("linear_srgb.npy", "application/x-npy"),
}
_MAX_INDEX_BYTES = 2 * 1024 * 1024
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_ARRAY_BYTES = 1_100_000_000


def _backend_precision(request) -> tuple[str, str]:
    """Interpret older CPU-only frozen requests without changing their digest."""
    if not isinstance(request, dict):
        raise ValueError("appearance frozen request is invalid")
    backend = request.get("backend", "cpu")
    precision = request.get("precision", "native" if backend == "cpu" else "mixed")
    if (type(backend) is not str or backend not in ("cpu", "gpu_cuda")
            or type(precision) is not str
            or precision not in (("native",) if backend == "cpu" else ("float32", "mixed"))):
        raise ValueError("appearance frozen backend or precision is invalid")
    return backend, precision


def _files_for_backend(backend: str) -> tuple[str, ...]:
    return _GPU_FILES if backend == "gpu_cuda" else _FILES


def _gpu_job_sha256(job) -> str:
    # This is the producer's exact physical-job hash, not the queue's JSON digest.
    return hashlib.sha256(json.dumps(job, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate appearance output JSON key")
        value[key] = item
    return value


def _reject_nonfinite(value):
    raise ValueError("non-finite appearance output JSON value: " + value)


def _gpu_manifest_fields(manifest, job, precision, wavelengths):
    from coat_sim.appearance.gpu import FATAL_COUNTERS

    if (manifest.get("schema") != "coat_sim.gpu_appearance_preview"
            or manifest.get("status") != "rendered_unqualified"
            or manifest.get("precision") != precision
            or manifest.get("input_sha256") != _gpu_job_sha256(job)
            or not isinstance(manifest.get("execution"), dict)
            or manifest["execution"].get("variant") != "cuda_ad_rgb"
            or not isinstance(manifest.get("runtime"), dict)
            or manifest["runtime"].get("variant") != "cuda_ad_rgb"):
        raise ValueError("GPU appearance manifest does not attest the requested CUDA render")
    records = manifest.get("per_wavelength")
    if not isinstance(records, list) or len(records) != len(wavelengths):
        raise ValueError("GPU appearance diagnostics are incomplete")
    for index, (record, wavelength) in enumerate(zip(records, wavelengths)):
        if (not isinstance(record, dict) or type(record.get("index")) is not int
                or record["index"] != index or record.get("wavelength_nm") != wavelength
                or type(record.get("seed")) is not int
                or record["seed"] != job["resolution"]["seed"] + index
                or not isinstance(record.get("diagnostics"), dict)
                or record["diagnostics"].get("pass") is not True):
            raise ValueError("GPU appearance wavelength diagnostics failed")
        if any(type(record["diagnostics"].get(name)) is not int
               or record["diagnostics"][name] != 0 for name in FATAL_COUNTERS):
            raise ValueError("GPU appearance wavelength has a fatal optics diagnostic")


def _gpu_input_job(path: Path, job) -> None:
    if path.stat().st_size > MAX_APPEARANCE_FROZEN_BYTES:
        raise ValueError("GPU appearance input-job copy exceeds its size limit")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"),
                           object_pairs_hook=_unique_object,
                           parse_constant=_reject_nonfinite)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("GPU appearance input-job copy is invalid") from exc
    if _canonical(saved) != _canonical(job):
        raise ValueError("GPU appearance input-job copy differs from the frozen job")


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha256_json(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path, cancel_event=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("appearance render cancelled while verifying output")
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_output_references(value) -> bool:
    if not isinstance(value, dict) or set(value) != set(_PUBLIC_OUTPUTS):
        return False
    for key, (_, media_type) in _PUBLIC_OUTPUTS.items():
        reference = value.get(key)
        if (not isinstance(reference, dict)
                or set(reference) != {"path", "sha256", "bytes", "media_type"}
                or not isinstance(reference.get("path"), str)
                or not Path(reference["path"]).is_absolute()
                or not _is_sha256(reference.get("sha256"))
                or type(reference.get("bytes")) is not int
                or reference["bytes"] < 1
                or reference.get("media_type") != media_type):
            return False
    return True


def verified_output_references(root, job_id, index) -> dict:
    """Build fixed-path references from a validated completion index.

    The returned metadata is not a substitute for full result validation: the
    validator hashes and inspects every output before trusting a result.
    """
    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise ValueError("invalid appearance scheduler job identity")
    if not isinstance(index, dict) or index.get("job_id") != job_id:
        raise ValueError("appearance output references are not bound to this job")
    records = index.get("files")
    if not isinstance(records, dict) or not set(_FILES) <= set(records) <= set(_GPU_FILES):
        raise ValueError("appearance completion file set is invalid")
    root = Path(root).resolve(strict=True)
    output_dir = root / "data" / "tmp" / "appearance" / job_id / "render"
    _assert_no_symlink_path(root, output_dir, include_target=False)
    references = {}
    for key, (basename, media_type) in _PUBLIC_OUTPUTS.items():
        record = records.get(basename)
        if (not isinstance(record, dict) or set(record) != {"sha256", "bytes"}
                or not _is_sha256(record.get("sha256"))
                or type(record.get("bytes")) is not int or record["bytes"] < 1):
            raise ValueError("appearance output file record is invalid")
        path = output_dir / basename
        _safe_fixed_file(root, path, path, "output")
        if path.stat().st_size != record["bytes"]:
            raise ValueError("appearance output file size does not match the completion index")
        references[key] = {
            "path": str(path),
            "sha256": record["sha256"],
            "bytes": record["bytes"],
            "media_type": media_type,
        }
    return references


def is_appearance_render_result(result) -> bool:
    """Check the small public envelope shape; never reads files or opens jobs."""
    if not isinstance(result, dict):
        return False
    required = {
        "kind", "schema_version", "job_id", "input_digest", "request_sha256",
        "context_sha256", "job_sha256", "input_sha256", "resolution",
        "wavelength_nm", "display_exposure", "result_file", "sha256",
    }
    allowed = required | {"artifact_id", "result_artifacts", "outputs",
                          "backend", "precision", "render_status", "diagnostics_pass"}
    if ("backend" in result) != ("precision" in result):
        return False
    if "backend" in result:
        try:
            _backend_precision(result)
        except ValueError:
            return False
    if "render_status" in result and result["render_status"] != "rendered_unqualified":
        return False
    if "diagnostics_pass" in result and result["diagnostics_pass"] is not True:
        return False
    if "artifact_id" in result and not _is_sha256(result.get("artifact_id")):
        return False
    if "result_artifacts" in result and not isinstance(result.get("result_artifacts"), list):
        return False
    if "outputs" in result and not _is_output_references(result.get("outputs")):
        return False
    return bool(
        isinstance(result, dict)
        and required <= set(result) <= allowed
        and result.get("kind") == RESULT_KIND
        and type(result.get("schema_version")) is int
        and result["schema_version"] == 1
        and isinstance(result.get("job_id"), str)
        and _JOB_ID.fullmatch(result["job_id"]) is not None
        and all(_is_sha256(result.get(field)) for field in (
            "input_digest", "request_sha256", "context_sha256", "job_sha256",
            "input_sha256", "sha256"))
        and isinstance(result.get("resolution"), dict)
        and isinstance(result.get("wavelength_nm"), list)
        and isinstance(result.get("result_file"), str)
        and isinstance(result.get("display_exposure"), (int, float))
        and not isinstance(result.get("display_exposure"), bool)
        and math.isfinite(float(result["display_exposure"]))
        and result["display_exposure"] > 0
    )


def _assert_no_symlink_path(root: Path, target: Path, *, include_target=True) -> None:
    """Reject symlink/junction/reparse redirection below the canonical root."""
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("appearance result path escaped project root") from exc
    current = root
    parts = relative.parts if include_target else relative.parts[:-1]
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("appearance result paths must not contain symbolic links")
        is_junction = getattr(current, "is_junction", None)
        if callable(is_junction) and is_junction():
            raise ValueError("appearance result paths must not contain junctions")
        try:
            stat_result = current.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(stat_result, "st_file_attributes", 0)
        if attributes & getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("appearance result paths must not contain reparse points")
        try:
            resolved = current.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError("appearance result path cannot be resolved safely") from exc
        lexical = Path(os.path.abspath(str(current)))
        if (not resolved.is_relative_to(root)
                or os.path.normcase(str(resolved)) != os.path.normcase(str(lexical))):
            raise ValueError("appearance result path redirects outside its fixed location")


def _safe_fixed_file(root: Path, path: Path, expected: Path, label: str) -> Path:
    if path != expected:
        raise ValueError(f"appearance {label} path does not match its fixed job location")
    _assert_no_symlink_path(root, path)
    if not path.is_file():
        raise ValueError(f"appearance {label} file is missing")
    return path


def _validate_png(path: Path, width: int, height: int) -> None:
    if path.stat().st_size < 33:
        raise ValueError("appearance display PNG is truncated")
    with path.open("rb") as stream:
        header = stream.read(33)
    if (header[:8] != b"\x89PNG\r\n\x1a\n"
            or struct.unpack(">I", header[8:12])[0] != 13
            or header[12:16] != b"IHDR"):
        raise ValueError("appearance display file is not a valid PNG header")
    png_width, png_height = struct.unpack(">II", header[16:24])
    if (png_width, png_height) != (width, height):
        raise ValueError("appearance display dimensions do not match frozen resolution")


def _validate_npy(path: Path, shape: tuple[int, ...], cancel_event=None) -> None:
    size = path.stat().st_size
    expected_payload = int(np.prod(shape, dtype=np.int64)) * np.dtype(np.float32).itemsize
    if size < expected_payload or size > expected_payload + 65536 or size > _MAX_ARRAY_BYTES:
        raise ValueError("appearance NPY size does not match its bounded expected shape")
    try:
        array = np.load(path, mmap_mode="r", allow_pickle=False)
    except Exception as exc:
        raise ValueError("appearance NPY file is invalid") from exc
    try:
        if array.dtype != np.dtype(np.float32) or array.shape != shape:
            raise ValueError("appearance NPY dtype or dimensions do not match the render contract")
        if getattr(array, "offset", -1) + array.nbytes != size:
            raise ValueError("appearance NPY has unexpected trailing or missing bytes")
        flat = array.reshape(-1)
        for offset in range(0, flat.size, 1_000_000):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("appearance render cancelled while checking NPY values")
            if not np.isfinite(flat[offset:offset + 1_000_000]).all():
                raise ValueError("appearance NPY contains non-finite values")
    finally:
        mmap = getattr(array, "_mmap", None)
        if mmap is not None:
            mmap.close()


def _resolution(value) -> dict:
    if (not isinstance(value, dict)
            or set(value) != {"width", "height", "spp", "seed"}
            or any(type(value.get(key)) is not int for key in ("width", "height", "spp", "seed"))):
        raise ValueError("appearance frozen resolution is invalid")
    return {key: value[key] for key in ("width", "height", "spp", "seed")}


def validate_appearance_render_result(root, job_id, result, *,
                                      expected_input_digest=None,
                                      require_registered_artifact=False,
                                      cancel_event=None) -> dict:
    """Verify queue/index/output binding; registered-result reads require DB anchor.

    ``require_registered_artifact`` is false for the worker's pre-registration
    self-check and true for daemon result reads after ``register_artifacts``.
    """
    if not is_appearance_render_result(result) or result.get("job_id") != job_id:
        raise ValueError("appearance result envelope is invalid for this job")
    root = Path(root).resolve(strict=True)
    if expected_input_digest is not None and result["input_digest"] != expected_input_digest:
        raise ValueError("appearance result does not match the scheduler frozen input digest")

    index_path = root / "artifacts" / "runtime" / "scheduler" / "jobs" / job_id / "appearance-result.json"
    supplied_index = Path(result["result_file"])
    _safe_fixed_file(root, supplied_index, index_path, "completion index")
    spec_path = index_path.with_name("spec.json")
    _safe_fixed_file(root, spec_path, spec_path, "frozen job spec")
    if spec_path.stat().st_size > MAX_APPEARANCE_SPEC_BYTES:
        raise ValueError("appearance frozen job spec exceeds its size limit")
    try:
        from coat_sim.scheduler.jobs import JobSpec
        frozen_spec = JobSpec.parse_frozen_json(spec_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("appearance frozen job spec is invalid") from exc
    # Remote jobs bind a logical server device in the persisted fingerprint.
    # cuda:default means the server's probed default CUDA allocation, not a
    # claimed physical UUID.  The worker independently attests the manifest.
    cpu_binding = {"device_binding": {"schema_version": 1,
                                      "device_id": "cpu:default"}}
    cuda_binding = {"device_binding": {"schema_version": 1,
                                       "device_id": "cuda:default"}}
    if (frozen_spec.executor != "appearance_render"
            or frozen_spec.inputs not in ({}, cpu_binding, cuda_binding)
            or set(frozen_spec.parameters) != {"frozen"}
            or frozen_spec.fingerprint != result["input_digest"]
            or (expected_input_digest is not None
                and frozen_spec.fingerprint != expected_input_digest)):
        raise ValueError("appearance result is not bound to its persisted scheduler spec")
    envelope = frozen_spec.parameters["frozen"]
    try:
        if (not isinstance(envelope, dict)
                or set(envelope) != {"schema", "schema_version", "root", "request",
                                     "context", "context_sha256", "job", "job_sha256"}
                or envelope.get("schema") != "coat_sim.appearance_frozen_request"
                or type(envelope.get("schema_version")) is not int
                or envelope.get("schema_version") != 1
                or Path(envelope.get("root", "")).resolve(strict=True) != root
                or not isinstance(envelope.get("request"), dict)
                or not isinstance(envelope.get("context"), dict)
                or not isinstance(envelope.get("job"), dict)
                or envelope.get("context_sha256") != _sha256_json(envelope["context"])
                or envelope.get("job_sha256") != _sha256_json(envelope["job"])
                or _sha256_json(envelope["request"]) != result["request_sha256"]
                or envelope["context_sha256"] != result["context_sha256"]
                or envelope["job_sha256"] != result["job_sha256"]):
            raise ValueError("appearance result does not match the frozen envelope")
    except (OSError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("appearance frozen envelope binding is invalid") from exc
    try:
        from coat_sim.appearance.contract import validate_job
        backend, precision = _backend_precision(envelope["request"])
        if frozen_spec.inputs == cpu_binding and (backend, precision) != ("cpu", "native"):
            raise ValueError("remote appearance device binding differs from backend")
        if frozen_spec.inputs == cuda_binding and (backend != "gpu_cuda"
                                                   or precision not in ("mixed", "float32")):
            raise ValueError("remote appearance CUDA binding differs from backend")
        normalized = validate_job(
            envelope["job"],
            max_wavelength_samples=None if backend == "gpu_cuda" else 20_000_000)
    except Exception as exc:
        raise ValueError("appearance frozen render job failed contract validation") from exc
    expected_backend_fields = ("backend" in envelope["request"]
                               or "precision" in envelope["request"])
    if expected_backend_fields:
        if (result.get("backend") != backend or result.get("precision") != precision):
            raise ValueError("appearance result backend differs from its frozen request")
    elif "backend" in result or "precision" in result:
        if (result.get("backend"), result.get("precision")) != ("cpu", "native"):
            raise ValueError("legacy appearance result cannot claim another backend")
    if backend == "gpu_cuda":
        if (result.get("render_status") != "rendered_unqualified"
                or result.get("diagnostics_pass") is not True):
            raise ValueError("GPU appearance result has no successful diagnostic status")
    elif "render_status" in result or "diagnostics_pass" in result:
        raise ValueError("CPU appearance result cannot claim GPU diagnostic status")
    expected_resolution = normalized["resolution"]
    expected_wavelengths = normalized["wavelength_nm"].tolist()
    expected_exposure = envelope["request"].get("display_exposure", 0.01)
    if (result.get("resolution") != expected_resolution
            or result.get("wavelength_nm") != expected_wavelengths
            or result.get("display_exposure") != expected_exposure):
        raise ValueError("appearance result fields differ from its normalized frozen request")
    index_size = index_path.stat().st_size
    if index_size < 2 or index_size > _MAX_INDEX_BYTES:
        raise ValueError("appearance completion index size is invalid")
    index_sha256 = _sha256_file(index_path, cancel_event)
    if result["sha256"] != index_sha256:
        raise ValueError("appearance completion index digest does not match the queue result")

    artifacts = result.get("result_artifacts")
    anchored = False
    if isinstance(artifacts, list):
        for item in artifacts:
            if (isinstance(item, dict) and item.get("role") == "result"
                    and item.get("sha256") == index_sha256
                    and item.get("bytes") == index_size
                    and item.get("name") == index_path.name
                    and item.get("local_path") == str(index_path)
                    and isinstance(item.get("artifact_id"), str)
                    and _SHA256.fullmatch(item["artifact_id"])
                    and item["artifact_id"] == hashlib.sha256(
                        (job_id + ":result:" + index_sha256).encode("utf-8")).hexdigest()):
                if result.get("artifact_id") not in (None, item["artifact_id"]):
                    continue
                anchored = True
                break
    if require_registered_artifact and not anchored:
        raise ValueError("appearance completion index is not anchored by the scheduler artifact registry")

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("appearance completion index is invalid JSON") from exc
    index_fields = {
        "schema", "schema_version", "job_id", "input_digest", "request_sha256",
        "context_sha256", "job_sha256", "input_sha256", "resolution",
        "wavelength_nm", "display_exposure", "files",
    }
    if expected_backend_fields:
        index_fields |= {"backend", "precision"}
    if backend == "gpu_cuda":
        index_fields |= {"render_status", "diagnostics_pass"}
    if (not isinstance(index, dict) or set(index) != index_fields
            or index.get("schema") != INDEX_SCHEMA
            or type(index.get("schema_version")) is not int
            or index["schema_version"] != INDEX_SCHEMA_VERSION
            or index.get("job_id") != job_id
            or index.get("input_digest") != result["input_digest"]
            or index.get("request_sha256") != result["request_sha256"]
            or index.get("context_sha256") != result["context_sha256"]
            or index.get("job_sha256") != result["job_sha256"]
            or index.get("input_sha256") != result["input_sha256"]
            or index.get("resolution") != result["resolution"]
            or index.get("wavelength_nm") != result["wavelength_nm"]
            or index.get("display_exposure") != result["display_exposure"]):
        raise ValueError("appearance completion index does not match the typed queue result")
    if expected_backend_fields and (index.get("backend") != backend
                                    or index.get("precision") != precision):
        raise ValueError("appearance completion backend differs from frozen request")
    if backend == "gpu_cuda" and (index.get("render_status") != "rendered_unqualified"
                                  or index.get("diagnostics_pass") is not True):
        raise ValueError("GPU appearance completion has no passing diagnostics")
    expected_output_references = verified_output_references(root, job_id, index)
    if ("outputs" in result
            and result["outputs"] != expected_output_references):
        raise ValueError("appearance output references do not match fixed paths and completion index")
    resolution = _resolution(index["resolution"])
    wavelengths = index["wavelength_nm"]
    if (not isinstance(wavelengths, list)
            or resolution != expected_resolution
            or wavelengths != expected_wavelengths):
        raise ValueError("appearance completion inputs differ from the normalized frozen request")
    for field in ("input_digest", "request_sha256", "context_sha256", "job_sha256", "input_sha256"):
        if not _is_sha256(index[field]):
            raise ValueError("appearance completion digest is invalid")
    exposure = index["display_exposure"]
    if (isinstance(exposure, bool) or not isinstance(exposure, (int, float))
            or not math.isfinite(float(exposure)) or exposure <= 0
            or exposure != expected_exposure):
        raise ValueError("appearance display exposure is invalid")

    output_dir = root / "data" / "tmp" / "appearance" / job_id / "render"
    _assert_no_symlink_path(root, output_dir, include_target=False)
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise ValueError("appearance output directory is missing or unsafe")
    expected_files = _files_for_backend(backend)
    if {path.name for path in output_dir.iterdir()} != set(expected_files):
        raise ValueError("appearance output directory contains an unexpected file set")
    records = index["files"]
    if not isinstance(records, dict) or set(records) != set(expected_files):
        raise ValueError("appearance completion file set is invalid")
    paths = {}
    for name in expected_files:
        record = records[name]
        if (not isinstance(record, dict) or set(record) != {"sha256", "bytes"}
                or not _is_sha256(record.get("sha256"))
                or type(record.get("bytes")) is not int or record["bytes"] < 1):
            raise ValueError("appearance output file record is invalid")
        path = _safe_fixed_file(root, output_dir / name, output_dir / name, "output")
        if (path.stat().st_size != record["bytes"]
                or _sha256_file(path, cancel_event) != record["sha256"]):
            raise ValueError("appearance output file bytes do not match the completion index")
        paths[name] = path

    width, height = resolution["width"], resolution["height"]
    _validate_npy(paths["radiance_spectrum.npy"], (len(wavelengths), height, width), cancel_event)
    _validate_npy(paths["xyz.npy"], (height, width, 3), cancel_event)
    _validate_npy(paths["linear_srgb.npy"], (height, width, 3), cancel_event)
    _validate_png(paths["display.png"], width, height)
    manifest_path = paths["manifest.json"]
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("appearance manifest exceeds its size limit")
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            **({"object_pairs_hook": _unique_object, "parse_constant": _reject_nonfinite}
               if backend == "gpu_cuda" else {}))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("appearance manifest is invalid JSON") from exc
    outputs = manifest.get("outputs") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1
            or not _is_sha256(manifest.get("input_sha256"))
            or manifest["input_sha256"] != index["input_sha256"]
            or manifest.get("resolution") != resolution
            or manifest.get("wavelength_nm") != wavelengths
            or not isinstance(outputs, dict)
            or outputs.get("display_exposure") != exposure
            or manifest.get("input_provenance") != normalized["provenance"]):
        raise ValueError("appearance manifest does not bind the frozen render inputs")
    if backend == "gpu_cuda":
        _gpu_manifest_fields(manifest, envelope["job"], precision, wavelengths)
        _gpu_input_job(paths["input-job.json"], envelope["job"])
    return index
