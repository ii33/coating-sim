"""Thin scheduler-worker adapter for the frozen appearance renderer."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

from .appearance_result import (
    INDEX_SCHEMA, INDEX_SCHEMA_VERSION, _MAX_INDEX_BYTES,
    _assert_no_symlink_path, _canonical, _resolution, _safe_fixed_file,
    _sha256_file, _sha256_json, _validate_npy, _validate_png,
    _backend_precision, _files_for_backend, _gpu_manifest_fields, _gpu_input_job,
    _unique_object, _reject_nonfinite,
    verified_output_references,
    validate_appearance_render_result,
)


_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _cancelled(cancel, stage):
    if cancel.is_set():
        raise InterruptedError(f"appearance render cancelled {stage}")


def _report_render_progress(progress, report) -> None:
    """Keep renderer details while mapping local render progress to queue units."""
    if not callable(report) or not isinstance(progress, Mapping):
        return
    stage = progress.get("stage")
    if stage == "rendering":
        fraction = progress.get("fraction")
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            fraction = 0.0
        fraction = min(1.0, max(0.0, float(fraction))) if math.isfinite(float(fraction)) else 0.0
        queue_fraction = 0.05 + 0.85 * fraction
    elif stage == "integrating":
        queue_fraction = 0.9
    elif stage == "complete":
        queue_fraction = 0.98
    else:
        return
    report({**dict(progress), "fraction": queue_fraction})


def _prepare_output_parent(root: Path, parent: Path) -> None:
    """Create only the fixed root-relative parent chain, rejecting symlinks."""
    _assert_no_symlink_path(root, parent, include_target=False)
    current = root
    for part in parent.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("appearance output path must not contain symbolic links")
        if current.exists():
            if not current.is_dir():
                raise ValueError("appearance output parent is not a directory")
        else:
            current.mkdir()
        _assert_no_symlink_path(root, current)


def _verify_unindexed_outputs(root: Path, job_id: str, output_dir: Path,
                              files: dict, resolution: dict, wavelengths: list,
                              exposure: float, input_sha256: str,
                              input_provenance: dict, backend: str, precision: str,
                              job: dict,
                              cancel) -> None:
    """Validate renderer files before publishing the completion index."""
    _cancelled(cancel, "while verifying renderer output")
    _assert_no_symlink_path(root, output_dir)
    expected_files = _files_for_backend(backend)
    if {path.name for path in output_dir.iterdir()} != set(expected_files):
        raise ValueError("appearance renderer produced an unexpected output file set")
    paths = {}
    for name in expected_files:
        path = _safe_fixed_file(root, output_dir / name, output_dir / name, "output")
        record = files[name]
        if (path.stat().st_size != record["bytes"]
                or _sha256_file(path, cancel) != record["sha256"]):
            raise ValueError("appearance renderer output changed while it was being verified")
        paths[name] = path
    width, height = resolution["width"], resolution["height"]
    _validate_npy(paths["radiance_spectrum.npy"], (len(wavelengths), height, width), cancel)
    _validate_npy(paths["xyz.npy"], (height, width, 3), cancel)
    _validate_npy(paths["linear_srgb.npy"], (height, width, 3), cancel)
    _validate_png(paths["display.png"], width, height)
    manifest_path = paths["manifest.json"]
    if manifest_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("appearance renderer manifest exceeds its size limit")
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            **({"object_pairs_hook": _unique_object, "parse_constant": _reject_nonfinite}
               if backend == "gpu_cuda" else {}))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("appearance renderer manifest is invalid") from exc
    if (not isinstance(manifest, dict)
            or type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") != 1
            or manifest.get("input_sha256") != input_sha256
            or manifest.get("resolution") != resolution
            or manifest.get("wavelength_nm") != wavelengths
            or not isinstance(manifest.get("outputs"), dict)
            or manifest["outputs"].get("display_exposure") != exposure
            or manifest.get("input_provenance") != input_provenance):
        raise ValueError("appearance renderer manifest does not match the frozen request")
    if backend == "gpu_cuda":
        _gpu_manifest_fields(manifest, job, precision, wavelengths)
        _gpu_input_job(paths["input-job.json"], job)
    _cancelled(cancel, "after verifying renderer output")


def execute_appearance(root, job_id, parameters, cancel, report):
    """Rebuild frozen inputs, render into the one job-scoped output path, and publish.

    This runs only in the existing scheduler worker process. It never starts a
    scheduler/daemon and never accepts a render path or an alternate executor
    from the request.
    """
    root = Path(root).resolve(strict=True)
    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise ValueError("invalid appearance scheduler job identity")
    if not isinstance(parameters, dict) or set(parameters) != {"frozen"}:
        raise ValueError("appearance_render requires one frozen parameter envelope")
    envelope = parameters["frozen"]
    required = {"schema", "schema_version", "root", "request", "context",
                "context_sha256", "job", "job_sha256"}
    if (not isinstance(envelope, dict) or set(envelope) != required
            or envelope.get("schema") != "coat_sim.appearance_frozen_request"
            or type(envelope.get("schema_version")) is not int
            or envelope.get("schema_version") != 1
            or envelope.get("root") != str(root)
            or not isinstance(envelope.get("request"), dict)
            or not isinstance(envelope.get("context"), dict)
            or not isinstance(envelope.get("job"), dict)):
        raise ValueError("appearance frozen request envelope is invalid")
    _cancelled(cancel, "before rebuilding frozen input")
    if callable(report):
        report({"stage": "appearance_preflight", "fraction": 0.02})

    # A queued spec is a snapshot, not permission to trust mutable project data.
    from .appearance_compute import build_frozen_request
    rebuilt = build_frozen_request(root, envelope["request"], cancel_event=cancel)
    if _canonical(rebuilt) != _canonical(envelope):
        raise ValueError("appearance frozen inputs changed after queue submission")
    request, context, job = envelope["request"], envelope["context"], envelope["job"]
    backend, precision = _backend_precision(request)
    request_sha256 = _sha256_json(request)
    context_sha256 = _sha256_json(context)
    job_sha256 = _sha256_json(job)
    if (envelope["context_sha256"] != context_sha256
            or envelope["job_sha256"] != job_sha256):
        raise ValueError("appearance frozen input digest does not match its contents")
    _cancelled(cancel, "after rebuilding frozen input")

    from coat_sim.scheduler.store import SchedulerStore
    queue_record = SchedulerStore(root).status(job_id)
    input_digest = queue_record.get("input_digest")
    if (queue_record.get("kind") != "appearance_render"
            or not isinstance(input_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", input_digest) is None):
        raise ValueError("appearance queue record is not a frozen appearance job")

    from coat_sim.appearance.contract import validate_job
    try:
        normalized = validate_job(
            job, max_wavelength_samples=None if backend == "gpu_cuda" else 20_000_000)
    except Exception as exc:
        raise ValueError("appearance frozen render job failed contract validation") from exc
    resolution = normalized["resolution"]
    wavelengths = normalized["wavelength_nm"].tolist()
    exposure = request.get("display_exposure", 0.01)
    if (isinstance(exposure, bool) or not isinstance(exposure, (int, float))
            or not math.isfinite(float(exposure)) or exposure <= 0):
        raise ValueError("appearance display exposure is invalid")
    exposure = float(exposure)

    output_dir = root / "data" / "tmp" / "appearance" / job_id / "render"
    _prepare_output_parent(root, output_dir.parent)
    _assert_no_symlink_path(root, output_dir, include_target=False)
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError("appearance render output already exists for this scheduler job")
    _cancelled(cancel, "before rendering")
    if callable(report):
        report({"stage": "appearance_render", "fraction": 0.05})

    if backend == "gpu_cuda":
        from coat_sim.appearance.gpu import render_preview
        rendered = render_preview(
            job, output_dir, precision=precision, cancel_event=cancel,
            progress_callback=lambda progress: _report_render_progress(progress, report),
            exposure=exposure, warm_repeat=False)
    else:
        from coat_sim.appearance.render import render_job
        rendered = render_job(
            job, output_dir, cancel_event=cancel, display_exposure=exposure,
            progress_callback=lambda progress: _report_render_progress(progress, report))
    spectrum = rendered.get("radiance_spectrum") if isinstance(rendered, dict) else None
    mmap = getattr(spectrum, "_mmap", None)
    if mmap is not None:
        mmap.close()
    del rendered
    _cancelled(cancel, "after rendering")
    _assert_no_symlink_path(root, output_dir)
    file_records = {}
    for name in _files_for_backend(backend):
        path = _safe_fixed_file(root, output_dir / name, output_dir / name, "output")
        file_records[name] = {"sha256": _sha256_file(path, cancel),
                              "bytes": path.stat().st_size}
    manifest_path = output_dir / "manifest.json"
    if manifest_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("appearance renderer manifest exceeds its size limit")
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            **({"object_pairs_hook": _unique_object, "parse_constant": _reject_nonfinite}
               if backend == "gpu_cuda" else {}))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("appearance renderer manifest is invalid") from exc
    input_sha256 = manifest.get("input_sha256") if isinstance(manifest, dict) else None
    if not isinstance(input_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", input_sha256) is None:
        raise ValueError("appearance renderer manifest has no physical input digest")
    _verify_unindexed_outputs(root, job_id, output_dir, file_records,
                              resolution, wavelengths, exposure, input_sha256,
                              normalized["provenance"], backend, precision, job, cancel)

    index = {
        "schema": INDEX_SCHEMA,
        "schema_version": INDEX_SCHEMA_VERSION,
        "job_id": job_id,
        "input_digest": input_digest,
        "request_sha256": request_sha256,
        "context_sha256": context_sha256,
        "job_sha256": job_sha256,
        "input_sha256": input_sha256,
        "resolution": resolution,
        "wavelength_nm": wavelengths,
        "display_exposure": exposure,
        "files": file_records,
    }
    if "backend" in request or "precision" in request:
        index.update(backend=backend, precision=precision)
    if backend == "gpu_cuda":
        index.update(render_status="rendered_unqualified", diagnostics_pass=True)
    _cancelled(cancel, "before publishing completion")
    index_path = root / "artifacts" / "runtime" / "scheduler" / "jobs" / job_id / "appearance-result.json"
    _assert_no_symlink_path(root, index_path)
    from coat_sim.scheduler.io import write_json_new
    write_json_new(index_path, index, max_bytes=_MAX_INDEX_BYTES,
                   indent=2, final_newline=True)
    index_sha256 = _sha256_file(index_path, cancel)
    result = {
        "kind": "appearance_render",
        "schema_version": 1,
        "job_id": job_id,
        "input_digest": input_digest,
        "request_sha256": request_sha256,
        "context_sha256": context_sha256,
        "job_sha256": job_sha256,
        "input_sha256": input_sha256,
        "resolution": resolution,
        "wavelength_nm": wavelengths,
        "display_exposure": exposure,
        "result_file": str(index_path),
        "sha256": index_sha256,
        "outputs": verified_output_references(root, job_id, index),
    }
    if "backend" in request or "precision" in request:
        result.update(backend=backend, precision=precision)
    if backend == "gpu_cuda":
        result.update(render_status="rendered_unqualified", diagnostics_pass=True)
    _cancelled(cancel, "after publishing completion")
    validate_appearance_render_result(
        root, job_id, result, expected_input_digest=input_digest,
        cancel_event=cancel)
    _cancelled(cancel, "after final verification")
    if callable(report):
        report({"stage": "appearance_verified", "fraction": 0.98})
    return result
