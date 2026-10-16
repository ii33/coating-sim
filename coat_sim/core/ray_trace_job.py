"""Portable deposition-asset references and loading for geometric ray jobs.

The queue spec stores artifact identity and digest only. Layer grids remain in
the existing binary ``.coatasset`` container and are verified before use.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
from contextlib import contextmanager
from typing import Any, Mapping


_ARTIFACT_ID = re.compile(r"[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_RAYTRACE_ASSET_BYTES = 512 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("cancelled")


def _private_snapshot_parent(root: str | Path) -> Path:
    """Return the ray-trace-owned temp directory, rejecting symlink escapes."""
    project_root = Path(root).resolve()
    target = project_root / "data" / "tmp" / "raytrace-assets"
    target.mkdir(parents=True, exist_ok=True)
    if any(part.is_symlink() for part in (target, target.parent, target.parent.parent)):
        raise ValueError("raytrace_snapshot_directory_must_not_contain_symlinks")
    try:
        target.resolve().relative_to(project_root)
    except ValueError as exc:
        raise ValueError("raytrace_snapshot_directory_outside_project") from exc
    return target


@contextmanager
def _asset_snapshot(path: str | Path, *, root: str | Path,
                    expected_sha256: str | None = None,
                    cancel_event=None, max_asset_bytes: int = MAX_RAYTRACE_ASSET_BYTES):
    """Copy an input asset into a unique project-local snapshot and hash that copy.

    The caller must parse only the yielded snapshot path. The digest check and
    parsing therefore refer to the same bytes even if the source changes later.
    """
    if (isinstance(max_asset_bytes, bool) or not isinstance(max_asset_bytes, int)
            or not 1 <= max_asset_bytes <= MAX_RAYTRACE_ASSET_BYTES):
        raise ValueError("max_asset_bytes_outside_supported_range")
    if expected_sha256 is not None and (
            not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256)):
        raise ValueError("invalid_expected_asset_sha256")
    _check_cancel(cancel_event)
    source = Path(path)
    if not source.is_file():
        raise ValueError("deposition_asset_missing")
    initial_size = source.stat().st_size
    if initial_size > max_asset_bytes:
        raise ValueError("deposition_asset_too_large")
    parent = _private_snapshot_parent(root)
    with tempfile.TemporaryDirectory(prefix="raytrace-", dir=parent) as private_dir:
        snapshot = Path(private_dir) / "asset.coatasset"
        digest = hashlib.sha256()
        copied = 0
        _check_cancel(cancel_event)
        with source.open("rb") as input_stream, snapshot.open("xb") as output_stream:
            while True:
                _check_cancel(cancel_event)
                block = input_stream.read(_COPY_CHUNK_BYTES)
                if not block:
                    break
                copied += len(block)
                if copied > max_asset_bytes:
                    raise ValueError("deposition_asset_too_large")
                digest.update(block)
                output_stream.write(block)
            output_stream.flush()
        _check_cancel(cancel_event)
        if copied != initial_size or source.stat().st_size != initial_size:
            raise ValueError("deposition_asset_changed_during_snapshot")
        actual_digest = digest.hexdigest()
        if expected_sha256 is not None and actual_digest != expected_sha256:
            raise ValueError("deposition_asset_digest_mismatch")
        yield {"path": snapshot, "sha256": actual_digest}


def request_uses_deposition_fields(request: Mapping[str, Any]) -> bool:
    """Return whether any coating layer explicitly refers to a deposition step."""
    if not isinstance(request, Mapping):
        raise ValueError("request must be an object")
    surfaces = request.get("surfaces")
    if not isinstance(surfaces, list):
        raise ValueError("request.surfaces must be an array")
    for surface in surfaces:
        if not isinstance(surface, Mapping):
            continue
        coating = surface.get("coating")
        if not isinstance(coating, Mapping):
            continue
        layers = coating.get("layers")
        if not isinstance(layers, list):
            continue
        if any(isinstance(layer, Mapping) and "deposition_step_id" in layer
               for layer in layers):
            return True
    return False


def freeze_raytrace_spec(request: Mapping[str, Any], deposition_artifact: Mapping[str, Any] | None = None):
    """Build the JSON-only queue spec for one ray-trace request.

    ``deposition_artifact`` is a stable artifact reference, never a filesystem
    path or an embedded grid. The worker checks the stored artifact role and
    SHA-256 again at execution time.
    """
    from coat_sim.scheduler.jobs import JobSpec, canonical

    # Verify that the whole request is finite, JSON-serializable pure data.
    request_json = canonical(request)
    frozen_request = json.loads(request_json)
    needs_fields = request_uses_deposition_fields(frozen_request)
    inputs: dict[str, Any] = {}
    if deposition_artifact is not None:
        if not isinstance(deposition_artifact, Mapping) or set(deposition_artifact) != {
                "artifact_id", "sha256"}:
            raise ValueError("deposition_artifact must contain artifact_id and sha256 only")
        artifact_id = deposition_artifact["artifact_id"]
        digest = deposition_artifact["sha256"]
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            raise ValueError("invalid_deposition_artifact_id")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("invalid_deposition_artifact_sha256")
        inputs["deposition_artifact"] = {"artifact_id": artifact_id, "sha256": digest}
    if needs_fields and "deposition_artifact" not in inputs:
        raise ValueError("deposition_artifact_required_for_layer_fields")
    if not needs_fields and inputs:
        raise ValueError("deposition_artifact_is_unused_by_request")
    return JobSpec("geometric_raytrace", {"request": frozen_request}, inputs).to_dict()


def _verified_artifact_path(root: Path, reference: Mapping[str, Any], *,
                            max_asset_bytes: int = MAX_RAYTRACE_ASSET_BYTES,
                            cancel_event=None) -> Path:
    """Resolve and bound an artifact reference through scheduler metadata.

    Byte digest verification is performed while producing the private snapshot,
    which is the exact file subsequently parsed.
    """
    from coat_sim.scheduler.store import SchedulerStore

    if set(reference) != {"artifact_id", "sha256"}:
        raise ValueError("invalid_deposition_artifact_reference")
    artifact_id, expected_digest = reference["artifact_id"], reference["sha256"]
    if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
        raise ValueError("invalid_deposition_artifact_id")
    if not isinstance(expected_digest, str) or not _SHA256.fullmatch(expected_digest):
        raise ValueError("invalid_deposition_artifact_sha256")

    _check_cancel(cancel_event)
    store = SchedulerStore(root)
    with store.queue.connection() as db:
        row = db.execute("SELECT metadata FROM scheduler_artifacts WHERE artifact_id=?",
                         (artifact_id,)).fetchone()
    if row is None:
        raise ValueError("deposition_artifact_not_found")
    metadata = json.loads(row["metadata"])
    if metadata.get("artifact_id") != artifact_id or metadata.get("role") != "bundle":
        raise ValueError("deposition_artifact_must_be_a_simulation_bundle")
    if metadata.get("sha256") != expected_digest:
        raise ValueError("deposition_artifact_digest_reference_mismatch")
    raw_path = metadata.get("local_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("deposition_artifact_has_no_local_payload")
    path = Path(raw_path).resolve()
    project_root = root.resolve()
    try:
        path.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("deposition_artifact_path_outside_project") from exc
    if not path.is_file() or path.stat().st_size > max_asset_bytes:
        raise ValueError("deposition_artifact_missing_or_too_large")
    if metadata.get("bytes") != path.stat().st_size:
        raise ValueError("deposition_artifact_size_mismatch")
    _check_cancel(cancel_event)
    return path


def _read_layer_fields(path: str | Path, *, include_arrays: bool, cancel_event=None):
    """Read only explicitly captured, total-contributing per-step grids."""
    import numpy as np
    from .simulation_assets import read_simulation_asset

    _check_cancel(cancel_event)
    content = read_simulation_asset(path, detail="full", include_arrays=include_arrays,
                                    cancel_event=cancel_event)
    _check_cancel(cancel_event)
    metadata = content.get("result_metadata") or {}
    manifest = metadata.get("layer_field_manifest")
    arrays = content.get("arrays") or {}
    layer_arrays = arrays.get("layer_fields")
    if not isinstance(manifest, Mapping) or manifest.get("captured") is not True:
        raise ValueError("deposition_asset_has_no_per_step_fields_recompute_or_use_uniform_film")
    if manifest.get("coordinates") != {"frame": "substrate_local_xy", "x_array": "x",
                                       "y_array": "y", "unit": "mm"} or \
            manifest.get("field_units") != "nm" or manifest.get("field_dtype") != "float64":
        raise ValueError("deposition_asset_has_unsupported_layer_field_coordinates_or_units")
    if not include_arrays:
        fields = []
        for row in manifest.get("fields", []):
            if isinstance(row, Mapping) and row.get("included_in_total") is True:
                fields.append({"step_id": row.get("step_id"), "array_key": row.get("array_key")})
        if not fields:
            raise ValueError("deposition_asset_has_no_included_per_step_fields_recompute_or_use_uniform_film")
        return {"fields": fields, "manifest": dict(manifest)}
    if not isinstance(layer_arrays, Mapping):
        raise ValueError("deposition_asset_has_no_per_step_fields_recompute_or_use_uniform_film")
    axes_x = np.asarray(arrays.get("x"), dtype=np.float64)
    axes_y = np.asarray(arrays.get("y"), dtype=np.float64)
    if (axes_x.ndim != 1 or axes_y.ndim != 1 or axes_x.size < 2 or axes_y.size < 2 or
            not np.isfinite(axes_x).all() or not np.isfinite(axes_y).all() or
            np.any(np.diff(axes_x) <= 0) or np.any(np.diff(axes_y) <= 0)):
        raise ValueError("deposition_asset_has_invalid_xy_axes")
    total_key = "field_nm" if "field_nm" in arrays else "field"
    total = np.asarray(arrays.get(total_key), dtype=np.float64)
    if total.shape != (axes_y.size, axes_x.size) or np.isinf(total).any():
        raise ValueError("deposition_asset_has_invalid_total_field_domain")
    configuration = content.get("configuration") or {}
    valid_mask = _substrate_valid_mask(configuration, axes_x, axes_y, total)
    profile = _substrate_profile(configuration)
    valid_cell_mask = _substrate_valid_cell_mask(
        profile, axes_x, axes_y, valid_mask, cancel_event=cancel_event)
    fields: dict[str, dict[str, Any]] = {}
    for row in manifest.get("fields", []):
        if not isinstance(row, Mapping) or row.get("included_in_total") is not True:
            continue
        step_id, key = row.get("step_id"), row.get("array_key")
        if not isinstance(step_id, str) or not isinstance(key, str) or step_id in fields:
            raise ValueError("deposition_asset_has_invalid_step_mapping")
        values = layer_arrays.get(step_id)
        if values is None:
            values = arrays.get(key)
        values = np.asarray(values)
        if values.dtype.kind != "f" or values.dtype.itemsize != 8 or \
                values.shape != (axes_y.size, axes_x.size):
            raise ValueError(f"deposition_asset_has_invalid_grid_for_step_{step_id}")
        if np.isinf(values).any() or np.any(values[np.isfinite(values)] < 0):
            raise ValueError(f"deposition_asset_has_invalid_thickness_for_step_{step_id}")
        if not np.isfinite(values[valid_mask]).all():
            raise ValueError(f"deposition_asset_has_missing_thickness_inside_substrate_for_step_{step_id}")
        fields[step_id] = {"x_mm": axes_x, "y_mm": axes_y,
                           "thickness_nm": values, "valid_mask": valid_mask.copy(),
                           "valid_cell_mask": valid_cell_mask.copy()}
    if not fields:
        raise ValueError("deposition_asset_has_no_included_per_step_fields_recompute_or_use_uniform_film")
    return fields


def _substrate_profile(configuration: Mapping[str, Any]):
    """Return the frozen substrate profile from an asset configuration."""
    profile = None
    settings = configuration.get("settings")
    if isinstance(settings, Mapping):
        candidate = settings.get("substrate_profile")
        if isinstance(candidate, Mapping):
            profile = candidate
    machine = configuration.get("machine")
    if profile is None and isinstance(machine, Mapping):
        candidate = machine.get("substrate_profile")
        if isinstance(candidate, Mapping):
            profile = candidate
    if profile is None:
        snapshot = configuration.get("input_snapshot")
        rows = snapshot.get("files") if isinstance(snapshot, Mapping) else None
        if isinstance(rows, list):
            profile = next((row.get("data") for row in rows
                            if isinstance(row, Mapping) and row.get("kind") == "substrate"
                            and isinstance(row.get("data"), Mapping)), None)
    return profile


def _substrate_valid_mask(configuration: Mapping[str, Any], axes_x, axes_y, total):
    """Rebuild the physical XY domain and require it to match the total field.

    Older or synthetic assets without a saved outline can safely use an all-
    finite total field. If total contains NaNs and no outline is available,
    reject rather than treating unknown holes as exterior substrate.
    """
    import numpy as np
    from .substrate_profiles import substrate_mask

    profile = _substrate_profile(configuration)
    finite_total = np.isfinite(total)
    if profile is None:
        if not finite_total.all():
            raise ValueError("deposition_asset_cannot_distinguish_substrate_from_internal_total_field_holes")
        return np.ones(total.shape, dtype=bool)
    xx, yy = np.meshgrid(axes_x, axes_y)
    domain = np.asarray(substrate_mask(xx, yy, profile), dtype=bool)
    if domain.shape != total.shape or not np.array_equal(domain, finite_total):
        raise ValueError("deposition_asset_total_field_finite_domain_does_not_match_substrate_outline")
    return domain


def _substrate_valid_cell_mask(profile, axes_x, axes_y, valid_mask, *, cancel_event=None):
    """Conservatively disable cells crossed by concave/polygonal outlines.

    Convex circle and rectangle domains need only four valid corners. For
    polygon and curve outlines, an outline edge crossing a cell's open interior
    disables that cell, preventing interpolation across a sub-grid notch/hole.
    """
    import numpy as np

    ny, nx = valid_mask.shape
    cells = (valid_mask[:-1, :-1] & valid_mask[1:, :-1] &
             valid_mask[:-1, 1:] & valid_mask[1:, 1:])
    if profile is None:
        return cells
    outline = profile.get("outline") if isinstance(profile, Mapping) else None
    kind = outline.get("kind") if isinstance(outline, Mapping) else None
    if kind not in {"polygon", "curve"}:
        return cells
    from .substrate_profiles import outline_polygon_points

    points = np.asarray(outline_polygon_points(outline), dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 2 or points.shape[0] < 3 or \
            not np.isfinite(points).all():
        raise ValueError("deposition_asset_has_invalid_substrate_outline")
    x_lines = np.asarray(axes_x, dtype=np.float64)
    y_lines = np.asarray(axes_y, dtype=np.float64)
    for start, end in zip(points, np.roll(points, -1, axis=0)):
        _check_cancel(cancel_event)
        delta = end - start
        if np.all(delta == 0):
            continue
        ts = [0.0, 1.0]
        if delta[0] != 0:
            ts.extend(((x_lines - start[0]) / delta[0]).tolist())
        if delta[1] != 0:
            ts.extend(((y_lines - start[1]) / delta[1]).tolist())
        ts = sorted({float(t) for t in ts if 0.0 <= t <= 1.0})
        for interval_index, (left, right) in enumerate(zip(ts, ts[1:])):
            if interval_index % 64 == 0:
                _check_cancel(cancel_event)
            if right <= left:
                continue
            midpoint = start + (0.5 * (left + right)) * delta
            # A segment coincident with a grid line only borders cells and
            # does not pass through either cell's open interior.
            if delta[0] == 0 and np.any(x_lines == start[0]):
                continue
            if delta[1] == 0 and np.any(y_lines == start[1]):
                continue
            ix = int(np.searchsorted(x_lines, midpoint[0], side="right") - 1)
            iy = int(np.searchsorted(y_lines, midpoint[1], side="right") - 1)
            if 0 <= ix < nx - 1 and 0 <= iy < ny - 1:
                cells[iy, ix] = False
    return cells


def inspect_layer_fields_asset(path: str | Path, *, root: str | Path,
                               expected_sha256: str | None = None, cancel_event=None,
                               max_asset_bytes: int = MAX_RAYTRACE_ASSET_BYTES):
    """Inspect a captured field manifest from the same verified private snapshot."""
    with _asset_snapshot(path, root=root, expected_sha256=expected_sha256,
                         cancel_event=cancel_event, max_asset_bytes=max_asset_bytes) as snapshot:
        details = _read_layer_fields(snapshot["path"], include_arrays=False,
                                     cancel_event=cancel_event)
        return {**details, "sha256": snapshot["sha256"]}


def load_layer_fields_from_asset(path: str | Path, *, root: str | Path,
                                 expected_sha256: str | None = None, cancel_event=None,
                                 max_asset_bytes: int = MAX_RAYTRACE_ASSET_BYTES):
    """Load captured grids only after copying and validating a private snapshot."""
    with _asset_snapshot(path, root=root, expected_sha256=expected_sha256,
                         cancel_event=cancel_event, max_asset_bytes=max_asset_bytes) as snapshot:
        return _read_layer_fields(snapshot["path"], include_arrays=True,
                                  cancel_event=cancel_event)


def load_layer_fields_from_artifact(root: str | Path, reference: Mapping[str, Any], *,
                                    cancel_event=None,
                                    max_asset_bytes: int = MAX_RAYTRACE_ASSET_BYTES):
    path = _verified_artifact_path(Path(root), reference,
                                   max_asset_bytes=max_asset_bytes,
                                   cancel_event=cancel_event)
    return load_layer_fields_from_asset(path, root=root,
                                        expected_sha256=reference["sha256"],
                                        cancel_event=cancel_event,
                                        max_asset_bytes=max_asset_bytes)
