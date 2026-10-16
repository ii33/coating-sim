"""One-shot isolated child for a frozen process-asset appearance render."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

from coat_sim.appearance.limits import MAX_APPEARANCE_CONTROL_BYTES


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _emit(kind: str, stage: str, message: str, **extra: Any) -> None:
    payload = {"type": kind, "stage": stage, "message": str(message)[:1000], **extra}
    # This is a compact status protocol, not a log channel.
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _progress_duration(value: Any) -> str | None:
    """Format finite nonnegative seconds compactly for the existing status line."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0:
        return None
    total = int(round(float(value)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes:02d} 分"
    if minutes:
        return f"{minutes} 分 {seconds:02d} 秒"
    return f"{seconds} 秒"


def _emit_render_progress(progress: Mapping[str, Any]) -> None:
    """Adapt render callback data to the worker's newline-delimited UI status."""
    stage = progress.get("stage")
    if stage not in {"preflight", "uploading", "rendering", "integrating", "complete"}:
        return
    completed = progress.get("completed_wavelengths")
    total = progress.get("total_wavelengths")
    if (isinstance(completed, bool) or not isinstance(completed, int)
            or isinstance(total, bool) or not isinstance(total, int)):
        return
    elapsed = _progress_duration(progress.get("elapsed_s")) or "未知"
    remaining = _progress_duration(progress.get("estimated_remaining_s"))
    estimate_kind = progress.get("estimate_kind")
    if stage == "preflight":
        message = "GPU 预检与 CUDA 初始化中；首帧可能需要编译，暂无法估算剩余时间。"
    elif stage == "uploading":
        message = "正在上传 GPU 几何、膜层和材料数组；首帧可能需要编译。"
    elif stage == "complete":
        message = f"波长 {completed}/{total} · 已用 {elapsed} · 正在确认结果"
    elif stage == "integrating" or estimate_kind == "finalizing":
        message = (f"波长 {completed}/{total} · 已用 {elapsed} · "
                   "预计剩余收尾中")
    else:
        eta = f"约 {remaining}" if remaining is not None else "估算中"
        message = f"波长 {completed}/{total} · 已用 {elapsed} · 预计剩余 {eta}"
    # Preserve structured progress for any future protocol consumer while the
    # current UI continues to show the established status text property.
    extra = {key: progress[key] for key in (
        "completed_wavelengths", "total_wavelengths", "elapsed_s",
        "estimated_remaining_s", "fraction", "estimate_kind") if key in progress}
    _emit("status", stage, message, **extra)


class _FileCancel:
    def __init__(self, marker: Path):
        self.marker = marker

    def is_set(self) -> bool:
        return self.marker.exists()


def _appearance_context_from_asset(path: Path, root: Path, asset_sha256: str,
                                   *, cancel_event=None) -> dict[str, Any]:
    """Resolve the exact v7 process profile without importing the Qt UI."""
    from coat_sim.core.ray_trace_job import (_asset_snapshot, _read_layer_fields,
                                             _substrate_profile,
                                             MAX_RAYTRACE_ASSET_BYTES)
    from coat_sim.core.simulation_assets import read_simulation_asset
    from coat_sim.core.process_profiles import validate_process_profile

    with _asset_snapshot(path, root=root, expected_sha256=asset_sha256,
                         cancel_event=cancel_event,
                         max_asset_bytes=MAX_RAYTRACE_ASSET_BYTES) as snapshot:
        content = read_simulation_asset(snapshot["path"], detail="full",
                                        include_arrays=False, cancel_event=cancel_event)
        configuration = content.get("configuration") or {}
        candidates = []
        process = configuration.get("process")
        if isinstance(process, Mapping):
            candidates.append(process)
        frozen_inputs = configuration.get("input_snapshot")
        rows = frozen_inputs.get("files") if isinstance(frozen_inputs, Mapping) else None
        if isinstance(rows, list):
            candidates.extend(row.get("data") for row in rows
                              if isinstance(row, Mapping) and row.get("kind") == "process"
                              and isinstance(row.get("data"), Mapping))
        process_profile = None
        for candidate in candidates:
            if candidate.get("schema_version") != 7:
                continue
            try:
                normalized = validate_process_profile(candidate)
            except (ValueError, TypeError):
                continue
            if (isinstance(normalized.get("film_design_snapshot"), Mapping) and
                    isinstance(normalized.get("film_design_import"), Mapping)):
                process_profile = normalized
                break
        if process_profile is None:
            raise ValueError("The selected process asset does not contain a complete frozen v7 FilmDesign snapshot.")
        captured = _read_layer_fields(snapshot["path"], include_arrays=False,
                                      cancel_event=cancel_event)
        if not captured.get("fields"):
            raise ValueError("The selected process asset has no captured deposition simulation per-layer thickness fields.")
        profile = _substrate_profile(configuration)
        if not isinstance(profile, Mapping):
            raise ValueError("The selected process asset has no inherited substrate profile.")
        outline = profile.get("outline")
        if not isinstance(outline, Mapping) or outline.get("kind") not in ("rectangle", "circle"):
            raise ValueError("Thin-film appearance requires a flat rectangular or circular process substrate profile.")
        _profile_for_adapter(profile)
        profile_sha256 = _sha256_bytes(_canonical(process_profile))
        return {
            "schema": "coat_sim.appearance_launch_context",
            "version": 1,
            "asset_path": str(Path(path).resolve()),
            "asset_sha256": snapshot["sha256"],
            "asset_root": str(Path(root).resolve()),
            "process_profile": process_profile,
            "process_profile_sha256": profile_sha256,
            "substrate_profile": dict(profile),
            "captured_step_ids": sorted(row["step_id"] for row in captured["fields"]
                                         if isinstance(row, Mapping) and isinstance(row.get("step_id"), str)),
            "layer_field_units": captured["manifest"].get("field_units", ""),
            "layer_field_coordinate_frame": (captured["manifest"].get("coordinates") or {}).get("frame", ""),
        }


def _profile_for_adapter(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve a circle or normalize a v1 rectangle without changing geometry."""
    surface = profile.get("surface")
    if not isinstance(surface, Mapping) or surface.get("kind") != "flat":
        raise ValueError("Thin-film appearance requires an explicitly flat process substrate surface.")
    outline = profile.get("outline")
    if not isinstance(outline, Mapping) or outline.get("kind") not in ("rectangle", "circle"):
        raise ValueError("Thin-film appearance requires a flat rectangular or circular process substrate profile.")
    result = dict(profile)
    normalized_outline = dict(outline)
    if normalized_outline["kind"] == "circle":
        radius = normalized_outline.get("radius_mm")
        if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not math.isfinite(float(radius)) or radius <= 0:
            raise ValueError("Circular process substrate requires a positive finite radius_mm.")
        result["outline"] = normalized_outline
        return result
    points = normalized_outline.get("points")
    if points is None:
        try:
            width = float(normalized_outline["width_mm"])
            height = float(normalized_outline["height_mm"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Rectangular process substrate width and height are missing.") from exc
        if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
            raise ValueError("Rectangular process substrate width and height must be positive finite millimetres.")
        normalized_outline["points"] = [
            [-width / 2, -height / 2], [width / 2, -height / 2],
            [width / 2, height / 2], [-width / 2, height / 2],
        ]
    result["outline"] = normalized_outline
    return result


def _vectorize_scalar_optical_evaluator(scalar_evaluator):
    """Adapt the core's scalar wavelength API to the v7 adapter's list contract."""
    import numpy as np

    def evaluate(record, wavelengths, wavelength_unit,
                 extrapolation_policy="error"):
        query = np.asarray(wavelengths, dtype=np.float64)
        if query.ndim == 0:
            return scalar_evaluator(record, float(query), wavelength_unit,
                                    extrapolation_policy=extrapolation_policy)
        if query.ndim != 1 or not np.isfinite(query).all() or np.any(query <= 0):
            raise ValueError("Optical evaluation wavelengths must be a positive finite vector.")
        sampled_n = []
        sampled_k = []
        k_is_unknown = None
        for wavelength_nm in query:
            n_value, k_value = scalar_evaluator(
                record, float(wavelength_nm), wavelength_unit,
                extrapolation_policy=extrapolation_policy)
            if k_is_unknown is None:
                k_is_unknown = k_value is None
            elif k_is_unknown != (k_value is None):
                raise ValueError("Optical set k availability changes across the requested wavelengths.")
            sampled_n.append(float(n_value))
            if k_value is not None:
                sampled_k.append(float(k_value))
        return sampled_n, (None if k_is_unknown else sampled_k)

    return evaluate


def _build_job_from_verified_asset(process_profile: Mapping[str, Any], asset_path: str,
                                   asset_root: str, wavelengths_nm, render_inputs: Mapping[str, Any],
                                   *, expected_sha256: str,
                                   geometry_thickness_mm: float | None,
                                   cancel_event=None) -> dict[str, Any]:
    """Use the frozen adapter with only a rectangular-profile representation bridge."""
    from coat_sim.core.ray_trace_job import (_asset_snapshot, _read_layer_fields,
                                             _substrate_profile,
                                             MAX_RAYTRACE_ASSET_BYTES)
    from coat_sim.core.simulation_assets import read_simulation_asset
    from coat_sim.appearance.process_adapter import build_job_from_v7
    from coat_sim.core import optical_constants

    with _asset_snapshot(asset_path, root=asset_root,
                         expected_sha256=expected_sha256,
                         cancel_event=cancel_event,
                         max_asset_bytes=MAX_RAYTRACE_ASSET_BYTES) as snapshot:
        _emit("status", "field_load", "Reading the captured per-step field arrays.")
        captured = _read_layer_fields(snapshot["path"], include_arrays=True,
                                      cancel_event=cancel_event)
        _emit("status", "asset_read", "Captured thickness arrays validated; reading the frozen process asset metadata.")
        content = read_simulation_asset(snapshot["path"], detail="full",
                                        include_arrays=False, cancel_event=cancel_event)
        metadata = content.get("result_metadata") or {}
        field_manifest = metadata.get("layer_field_manifest")
        if not isinstance(field_manifest, Mapping) or field_manifest.get("captured") is not True:
            raise ValueError("Verified process asset has no captured per-step field manifest.")
        source_profile = _substrate_profile(content.get("configuration") or {})
        if not isinstance(source_profile, Mapping):
            raise ValueError("Verified process asset has no inherited substrate profile.")
        render_parameters = dict(render_inputs)
        render_parameters["substrate_profile"] = _profile_for_adapter(source_profile)
        _emit("status", "film_adapter", "Joining captured fields to frozen FilmDesign layer IDs.")
        original_evaluator = optical_constants.evaluate_optical_set
        optical_constants.evaluate_optical_set = _vectorize_scalar_optical_evaluator(original_evaluator)
        try:
            job = build_job_from_v7(
                process_profile, {"manifest": dict(field_manifest), "fields": captured},
                wavelengths_nm, render_parameters,
                geometry_thickness_mm=geometry_thickness_mm)
        finally:
            optical_constants.evaluate_optical_set = original_evaluator
        _emit("status", "film_adapter_complete", "The frozen film stack passed input validation.")
        job["provenance"]["process_asset_sha256"] = snapshot["sha256"]
        return job


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run(input_path: Path, job_dir: Path, cancel_marker: Path) -> int:
    try:
        if cancel_marker.exists():
            _emit("status", "cancelled", "Cancellation arrived before preflight completed.")
            return 3
        if (not input_path.is_file()
                or input_path.stat().st_size > MAX_APPEARANCE_CONTROL_BYTES):
            raise ValueError("Worker input is missing or exceeds the 16 MB limit")
        request = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict) or request.get("schema") != "coat_sim.appearance_worker_input" or request.get("version") != 1:
            raise ValueError("Worker input schema/version is unsupported")
        job_id = request.get("job_id")
        outer_hash = request.get("input_sha256")
        if (not isinstance(job_id, str) or len(job_id) != 32
                or not isinstance(outer_hash, str) or len(outer_hash) != 64):
            raise ValueError("Worker input job ID or frozen draft digest is invalid")
        if job_dir.resolve() != input_path.parent.resolve():
            raise ValueError("Worker input must be inside its unique job directory")
        if not isinstance(request.get("render_inputs"), dict):
            raise ValueError("Frozen render parameters are missing")
        backend = request.get("backend", "cpu")
        precision = request.get("precision", "native" if backend == "cpu" else "mixed")
        if backend == "cpu":
            if precision != "native":
                raise ValueError("CPU appearance precision must be native")
        elif backend == "gpu_cuda":
            if precision not in ("float32", "mixed"):
                raise ValueError("GPU CUDA appearance precision must be float32 or mixed")
        else:
            raise ValueError("Unsupported appearance render backend")
        thickness_mode = request.get("geometry_thickness_mode")
        if thickness_mode == "film_design_snapshot":
            geometry_thickness_mm = None
        elif thickness_mode == "explicit_override":
            value = request.get("geometry_thickness_mm")
            if isinstance(value, bool):
                raise ValueError("Explicit geometry thickness must be a positive finite number of mm")
            try:
                geometry_thickness_mm = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Explicit geometry thickness must be a positive finite number of mm") from exc
            if not math.isfinite(geometry_thickness_mm) or geometry_thickness_mm <= 0.0:
                raise ValueError("Explicit geometry thickness must be a positive finite number of mm")
        else:
            raise ValueError("Frozen geometry thickness mode is unsupported")
        context = request.get("context")
        if not isinstance(context, dict) or _sha256_bytes(_canonical(context)) != request.get("context_sha256"):
            raise ValueError("Frozen process context digest does not match")
        _emit("status", "asset_preflight", "Rechecking process asset hash and frozen v7 profile.")

        # Re-read the exact asset in the child: neither the JSON payload nor the
        # geometric-raytrace request's copied optical values are authoritative.
        verified = _appearance_context_from_asset(
            Path(context["asset_path"]), Path(context["asset_root"]),
            str(context["asset_sha256"]))
        if (verified.get("process_profile_sha256") != context.get("process_profile_sha256")
                or _sha256_bytes(_canonical(verified)) != request.get("context_sha256")):
            raise ValueError("Process asset context changed since the appearance window opened")
        if cancel_marker.exists():
            _emit("status", "cancelled", "Cancellation arrived during asset preflight.")
            return 3

        _emit("status", "job_validation", "Joining captured process fields to frozen FilmDesign layer IDs.")
        _emit("status", "job_building", "Renderer module loaded; adapting verified v7 fields.")
        job = _build_job_from_verified_asset(
            verified["process_profile"], verified["asset_path"], verified["asset_root"],
            request["render_inputs"]["wavelength_nm"], request["render_inputs"],
            expected_sha256=verified["asset_sha256"],
            geometry_thickness_mm=geometry_thickness_mm,
            cancel_event=_FileCancel(cancel_marker))
        _emit("status", "job_ready", "Frozen layers and captured thickness fields passed validation.")
        if cancel_marker.exists():
            _emit("status", "cancelled", "Cancellation arrived during field validation.")
            return 3

        output_dir = job_dir / "render"
        if output_dir.exists():
            raise FileExistsError("This appearance job render directory already exists")
        if backend == "cpu":
            from coat_sim.appearance.render import render_job
            _emit("status", "rendering", "Rendering independent scalar wavelength frames.")
            result = render_job(job, output_dir,
                                cancel_event=_FileCancel(cancel_marker),
                                progress_callback=_emit_render_progress)
            render_manifest = result["manifest"]
        else:
            from coat_sim.appearance.gpu import render_preview
            from coat_sim.core.appearance_result import _gpu_manifest_fields
            _emit("status", "preflight", "Checking CUDA renderer and frozen inputs; no CPU fallback.")
            render_manifest = render_preview(
                job, output_dir, precision=precision,
                cancel_event=_FileCancel(cancel_marker),
                progress_callback=_emit_render_progress,
                warm_repeat=False)
            _gpu_manifest_fields(render_manifest, job, precision, job["wavelength_nm"])
            result = None
        if cancel_marker.exists():
            # render_job checks cancellation at frame boundaries and never writes
            # its completion manifest until all wavelength frames are complete.
            raise InterruptedError("appearance render cancelled at wavelength boundary")
        render_manifest_path = output_dir / "manifest.json"
        image_path = output_dir / "display.png"
        if not render_manifest_path.is_file() or not image_path.is_file():
            raise RuntimeError("renderer did not produce its completion manifest and display image")
        render_manifest_bytes = render_manifest_path.read_bytes()
        image_bytes = image_path.read_bytes()
        if len(image_bytes) > 64_000_000:
            raise RuntimeError("render display image exceeds its completion limit")
        completion = {
            "schema": "coat_sim.appearance_result", "job_id": job_id,
            "input_sha256": outer_hash,
            "backend": backend, "precision": precision,
            "render_input_sha256": render_manifest["input_sha256"],
            "render_manifest": "render/manifest.json",
            "render_manifest_sha256": _sha256_bytes(render_manifest_bytes),
            "image": "render/display.png", "image_sha256": _sha256_bytes(image_bytes),
            "width": int(render_manifest["resolution"]["width"]),
            "height": int(render_manifest["resolution"]["height"]),
            "wavelength_count": len(render_manifest["wavelength_nm"]),
        }
        # This wrapper is the only UI-accepted completion marker and is written
        # atomically after every render file and digest has been checked.
        _atomic_json(job_dir / "appearance_result.json", completion)
        if result is not None:
            try:
                result["radiance_spectrum"]._mmap.close()
            except (AttributeError, OSError):
                pass
        _emit("status", "complete", "Render files and completion manifest are ready.")
        return 0
    except InterruptedError as exc:
        _emit("status", "cancelled", str(exc))
        return 3
    except ModuleNotFoundError as exc:
        _emit("error", "dependency_missing", f"Local render dependency is unavailable: {exc.name}",
              failure_kind="dependency_missing")
        return 4
    except BaseException as exc:
        # Dependencies called through this one-shot boundary may raise
        # SystemExit rather than Exception. Keep every failure visible to the
        # Qt controller instead of returning silently without a completion file.
        detail = str(exc).strip() or type(exc).__name__
        _emit("error", "failed", detail, failure_kind=type(exc).__name__)
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated appearance render")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--cancel-marker", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args.input.resolve(), args.job_dir.resolve(), args.cancel_marker.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
