"""Read-only adapters from frozen v7 film-process inputs to appearance jobs."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import math
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from .contract import AppearanceInputError


def _material_from_snapshot(snapshot: Mapping, material_id: str, optical_set_id: str,
                            wavelengths_nm: np.ndarray, *, assume_lossless: bool) -> dict:
    records = snapshot.get("materials")
    if not isinstance(records, list):
        raise AppearanceInputError("frozen FilmDesign snapshot has no material records")
    material = next((item for item in records if isinstance(item, Mapping) and item.get("id") == material_id), None)
    if material is None:
        raise AppearanceInputError(f"frozen snapshot is missing material {material_id}")
    optical_sets = material.get("optical_sets")
    if not isinstance(optical_sets, list):
        raise AppearanceInputError(f"frozen material {material_id} has no optical sets")
    optical = next((item for item in optical_sets if isinstance(item, Mapping) and item.get("id") == optical_set_id), None)
    if optical is None:
        raise AppearanceInputError(f"frozen material {material_id} is missing optical set {optical_set_id}")
    from ..core.optical_constants import evaluate_optical_set
    try:
        samples = [evaluate_optical_set(optical, float(wavelength), "nm", extrapolation_policy="error")
                   for wavelength in wavelengths_nm]
    except ValueError as exc:
        raise AppearanceInputError(f"{material_id}/{optical_set_id} strict optical sampling failed: {exc}") from exc
    n = np.asarray([row[0] for row in samples], dtype=np.float64)
    assumed_lossless = any(row[1] is None for row in samples)
    if assumed_lossless and not assume_lossless:
        raise AppearanceInputError(f"{material_id}/{optical_set_id} has unknown k; explicit lossless approval is required")
    k = np.asarray([0.0 if row[1] is None else row[1] for row in samples], dtype=np.float64)
    return {"wavelength_nm": wavelengths_nm.tolist(), "n": n.tolist(), "k": k.tolist(),
            "valid_wavelength_nm": [float(wavelengths_nm[0]), float(wavelengths_nm[-1])],
            **({"assume_lossless": True} if assumed_lossless else {})}


def _body_thickness_mm(
        substrate: Mapping, *, geometry_thickness_mm: float | None
) -> tuple[float, str, str | None]:
    if geometry_thickness_mm is not None:
        if isinstance(geometry_thickness_mm, (bool, np.bool_, str, bytes)):
            raise AppearanceInputError("explicit solid geometry thickness must be a positive number of millimetres")
        try:
            value = float(geometry_thickness_mm)
        except (TypeError, ValueError, OverflowError) as exc:
            raise AppearanceInputError(
                "explicit solid geometry thickness must be a positive number of millimetres"
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise AppearanceInputError("explicit solid geometry thickness must be positive millimetres")
        return value, "explicit_geometry_input", None
    thickness = substrate.get("thickness")
    if not isinstance(thickness, Mapping):
        raise AppearanceInputError("frozen FilmDesign substrate thickness is missing")
    raw_value = thickness.get("value")
    if isinstance(raw_value, (bool, np.bool_, str, bytes)) or raw_value is None:
        raise AppearanceInputError("frozen FilmDesign substrate thickness value must be a positive number")
    try:
        value = float(raw_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AppearanceInputError("frozen FilmDesign substrate thickness value must be a positive number") from exc
    unit = thickness.get("unit")
    if not math.isfinite(value) or value <= 0:
        raise AppearanceInputError("frozen FilmDesign substrate thickness must be finite and positive")
    if not isinstance(unit, str):
        raise AppearanceInputError("FilmDesign substrate thickness must use nm, um, or mm")
    factor = {"mm": 1.0, "um": 1e-3, "nm": 1e-6}.get(unit)
    if factor is None:
        raise AppearanceInputError("FilmDesign substrate thickness must use nm, um, or mm")
    millimetres = value * factor
    if not math.isfinite(millimetres) or millimetres <= 0:
        raise AppearanceInputError("FilmDesign substrate thickness conversion to millimetres is out of range")
    return (millimetres, "film_design_snapshot/design_value",
            "design_value_not_measurement; rectangular slab assumes constant thickness and a parallel flat back surface")


def build_job_from_v7(process_profile: Mapping, captured_fields: Mapping,
                      wavelengths_nm, render_inputs: Mapping, *,
                      geometry_thickness_mm: float | None = None) -> dict:
    """Join actual captured per-step fields to exact frozen film layer IDs.

    ``captured_fields`` contains a manifest mapping and a step-ID keyed fields mapping. The
    process profile and both snapshots remain read-only. A single process run
    can contribute only the surface it actually imported; a non-empty opposing
    design face requires its own captured field run.
    """
    if not isinstance(process_profile, Mapping) or process_profile.get("schema_version") != 7:
        raise AppearanceInputError("appearance adapter requires process schema version 7")
    if not isinstance(captured_fields, Mapping) or not isinstance(captured_fields.get("manifest"), Mapping):
        raise AppearanceInputError("captured process field manifest is required")
    manifest = captured_fields["manifest"]
    if (manifest.get("captured") is not True or manifest.get("field_units") != "nm" or
            manifest.get("field_dtype") != "float64" or
            manifest.get("coordinates") != {"frame": "substrate_local_xy", "x_array": "x",
                                               "y_array": "y", "unit": "mm"} or
            manifest.get("sequence_order") != "process_recipe_order" or
            manifest.get("stack_order") != "first_step_is_substrate_adjacent_then_later_steps_overcoat"):
        raise AppearanceInputError("captured process field manifest has unsupported physical coordinate or order semantics")
    snapshot = process_profile.get("film_design_snapshot")
    if not isinstance(snapshot, Mapping):
        raise AppearanceInputError("v7 process profile has no frozen FilmDesign snapshot")
    from ..core.film_process_import import validate_frozen_design_snapshot
    snapshot = validate_frozen_design_snapshot(snapshot)
    design = snapshot["design"]
    imported = process_profile.get("film_design_import")
    if not isinstance(imported, Mapping) or imported.get("snapshot_sha256") != snapshot.get("sha256"):
        raise AppearanceInputError("process import and frozen FilmDesign snapshot do not match")
    active_face = imported.get("surface")
    if active_face not in ("front", "back"):
        raise AppearanceInputError("film import surface must be front or back")
    if imported.get("sequence_order") != "substrate_outward":
        raise AppearanceInputError("frozen film layer sequence must be substrate_outward")
    waves = np.asarray(wavelengths_nm, dtype=np.float64)
    if waves.ndim != 1 or waves.size < 2 or not np.isfinite(waves).all() or np.any(np.diff(waves) <= 0):
        raise AppearanceInputError("wavelength sampling is invalid")

    fields = captured_fields.get("fields")
    if not isinstance(fields, Mapping):
        raise AppearanceInputError("captured process fields are missing")
    rows = manifest.get("fields")
    if not isinstance(rows, list):
        raise AppearanceInputError("captured process field index is missing")
    sums: dict[str, np.ndarray] = {}
    captured_grid_reference = None
    step_ids_by_layer: dict[str, list[str]] = {}
    process_stack_order: list[str] = []
    steps = process_profile.get("steps")
    if not isinstance(steps, list):
        raise AppearanceInputError("process v7 step list is missing")
    for row in rows:
        if not isinstance(row, Mapping) or row.get("included_in_total") is not True:
            continue
        step_index, step_id = row.get("step_index"), row.get("step_id")
        if type(step_index) is not int or not 1 <= step_index <= len(steps) or not isinstance(step_id, str):
            raise AppearanceInputError("captured step index cannot be joined to process v7")
        step = steps[step_index - 1]
        reference = step.get("film_layer_ref") if isinstance(step, Mapping) else None
        if not isinstance(reference, Mapping) or reference.get("surface") != active_face:
            raise AppearanceInputError(f"included process step {step_index} is not bound to the imported film surface")
        layer_id = reference.get("layer_id")
        if not isinstance(layer_id, str) or step_id not in fields:
            raise AppearanceInputError(f"captured growth field is missing for process step {step_index}")
        if layer_id not in process_stack_order:
            process_stack_order.append(layer_id)
        row_field = fields[step_id]
        values = np.asarray(row_field.get("thickness_nm"), dtype=np.float64)
        if values.shape != (np.asarray(row_field["y_mm"]).size, np.asarray(row_field["x_mm"]).size):
            raise AppearanceInputError(f"captured growth field is malformed for process step {step_index}")
        if captured_grid_reference is None:
            captured_grid_reference = row_field
        elif any(not np.array_equal(np.asarray(captured_grid_reference[key]), np.asarray(row_field[key]))
                 for key in ("x_mm", "y_mm", "valid_mask", "valid_cell_mask")):
            raise AppearanceInputError("included captured steps must share actual grid and validity masks")
        mask = np.asarray(row_field["valid_mask"], dtype=bool)
        if mask.shape != values.shape or not np.isfinite(values[mask]).all() or np.any(values[mask] < 0):
            raise AppearanceInputError(f"captured growth field has missing or invalid interior values for process step {step_index}")
        sums[layer_id] = values.copy() if layer_id not in sums else sums[layer_id] + values
        step_ids_by_layer.setdefault(layer_id, []).append(step_id)

    all_layers = {face: design[f"{face}_layers"] for face in ("front", "back")}
    active_layers = all_layers[active_face]
    expected_ids = [item["id"] for item in active_layers]
    if expected_ids != imported.get("layer_ids"):
        raise AppearanceInputError("frozen layer sequence differs from its v7 film import snapshot")
    if set(sums) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(sums))
        extra = sorted(set(sums) - set(expected_ids))
        raise AppearanceInputError(f"captured fields do not cover the imported stack (missing={missing}, extra={extra})")
    if process_stack_order != expected_ids:
        raise AppearanceInputError("captured process growth order conflicts with the frozen substrate_outward layer order")
    other_face = "back" if active_face == "front" else "front"
    if all_layers[other_face]:
        raise AppearanceInputError(f"frozen {other_face} layers need their own actual process thickness-field capture")

    materials = {}
    layer_by_id = {item["id"]: item for item in active_layers}
    for layer_id in expected_ids:
        layer = layer_by_id[layer_id]
        materials[layer_id] = _material_from_snapshot(
            snapshot, layer["material_id"], layer["optical_set_id"], waves,
            assume_lossless=bool(design.get("assume_lossless", False)))
    # If the active process is the back face, front is bare; conversely a
    # non-empty back stack was rejected above rather than silently treated bare.
    front_layers = []
    back_layers = []
    destination = front_layers if active_face == "front" else back_layers
    for layer in active_layers:  # exact substrate_outward order from FilmDesign
        layer_id = layer["id"]
        actual = fields[step_ids_by_layer[layer_id][0]]
        destination.append({
            "layer_id": layer_id,
            "material": materials[layer_id],
            "thickness_field": {
                "unit": "nm", "origin": "captured_process_step",
                "x_mm": np.asarray(actual["x_mm"], dtype=np.float64).tolist(),
                "y_mm": np.asarray(actual["y_mm"], dtype=np.float64).tolist(),
                "thickness_nm": sums[layer_id].tolist(),
                "valid_mask": np.asarray(actual["valid_mask"], dtype=bool).tolist(),
                "valid_cell_mask": np.asarray(actual["valid_cell_mask"], dtype=bool).tolist(),
            },
        })

    sub = design["substrate"]
    thickness_mm, provenance, geometry_note = _body_thickness_mm(
        sub, geometry_thickness_mm=geometry_thickness_mm)
    substrate_material = _material_from_snapshot(
        snapshot, sub["material_id"], sub["optical_set_id"], waves,
        assume_lossless=bool(design.get("assume_lossless", False)))
    substrate_profile = process_profile.get("substrate_profile")
    if not isinstance(substrate_profile, Mapping):
        # ray_trace_job's verified asset read exposes the process-local geometry
        # in the underlying asset configuration. The caller can copy that
        # already-validated profile into render_inputs; never invent an outline.
        substrate_profile = render_inputs.get("substrate_profile")
    if not isinstance(substrate_profile, Mapping):
        raise AppearanceInputError("frozen process substrate outline is required for a closed physical slab")
    outline = substrate_profile.get("outline")
    if not isinstance(outline, Mapping) or outline.get("kind") not in ("rectangle", "circle"):
        raise AppearanceInputError("appearance prototype can close flat rectangle or circle outlines only")
    kind = outline["kind"]
    circle_geometry = {}
    if kind == "circle":
        surface = substrate_profile.get("surface")
        if not isinstance(surface, Mapping) or surface.get("kind") != "flat":
            raise AppearanceInputError("appearance circle extension requires an explicitly flat frozen substrate surface")
        radius = outline.get("radius_mm")
        if (isinstance(radius, (bool, np.bool_, str, bytes)) or radius is None):
            raise AppearanceInputError("frozen circular substrate radius_mm must be positive and finite")
        try:
            radius = float(radius)
        except (TypeError, ValueError, OverflowError) as exc:
            raise AppearanceInputError("frozen circular substrate radius_mm must be positive and finite") from exc
        if not math.isfinite(radius) or radius <= 0:
            raise AppearanceInputError("frozen circular substrate radius_mm must be positive and finite")
        bounds = [-radius, radius, -radius, radius]
        circle_geometry = {"radius_mm": radius,
                           "circle_boundary_sampling": render_inputs.get("circle_boundary_sampling")}
        if geometry_note is not None:
            geometry_note = geometry_note.replace("rectangular slab", "circular slab")
        # JSON null preserves exterior invalidity without transmitting NaN or
        # pretending exterior sentinel values are captured thickness samples.
        for layer in destination:
            field = layer["thickness_field"]
            values = np.asarray(field["thickness_nm"], dtype=object)
            values[~np.asarray(field["valid_mask"], dtype=bool)] = None
            field["thickness_nm"] = values.tolist()
    else:
        points = np.asarray(outline.get("points"), dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 2 or points.shape[0] < 4 or not np.isfinite(points).all():
            raise AppearanceInputError("rectangular substrate outline points are missing or invalid")
        bounds = [float(points[:, 0].min()), float(points[:, 0].max()),
                  float(points[:, 1].min()), float(points[:, 1].max())]
    job = copy.deepcopy(dict(render_inputs))
    job.update({"schema_version": 1, "coordinate_unit": "mm", "wavelength_nm": waves.tolist(),
                "front_layers": front_layers,
                "substrate": {"outline_kind": kind, "bounds_mm": bounds,
                              **circle_geometry,
                              "solid_thickness_mm": thickness_mm,
                              "solid_thickness_provenance": provenance,
                              "material": substrate_material,
                              "back_surface": {"layers": back_layers, "outside_medium": "air"}}})
    job["provenance"] = {"film_design_snapshot_sha256": snapshot["sha256"],
                         "active_process_surface": active_face,
                         "captured_step_ids_by_layer": step_ids_by_layer,
                         "process_field_sequence": manifest["sequence_order"],
                         "geometry_note": geometry_note}
    return job


def appearance_context_from_asset(path: Path, root: Path, asset_sha256: str,
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
            for row in rows:
                if isinstance(row, Mapping) and row.get("kind") == "process":
                    process_data = row.get("data")
                    if isinstance(process_data, Mapping):
                        candidates.append(process_data)
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
        profile_for_adapter(profile)
        profile_sha256 = hashlib.sha256(json.dumps(
            process_profile, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
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


def profile_for_adapter(profile: Mapping[str, Any]) -> dict[str, Any]:
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


def build_job_from_asset(process_profile: Mapping[str, Any], asset_path,
                         asset_root, wavelengths_nm, render_inputs: Mapping[str, Any],
                         *, expected_sha256: str | None = None,
                         geometry_thickness_mm: float | None = None,
                         cancel_event=None, progress_callback=None) -> dict[str, Any]:
    """Build a job from verified captured fields and the saved flat substrate.

    The optional callback receives ``(stage, message)`` synchronously. It does
    not own job state; callers own transport/status and cancellation lifetime.
    No renderer, Qt import, or process-global optical evaluator patch is used.
    """
    from coat_sim.core.ray_trace_job import (_asset_snapshot, _read_layer_fields,
                                             _substrate_profile,
                                             MAX_RAYTRACE_ASSET_BYTES)
    from coat_sim.core.simulation_assets import read_simulation_asset
    from coat_sim.core.ray_trace_job import _check_cancel

    def progress(stage, message):
        _check_cancel(cancel_event)
        if progress_callback is not None:
            progress_callback(stage, message)
        _check_cancel(cancel_event)

    with _asset_snapshot(asset_path, root=asset_root,
                         expected_sha256=expected_sha256,
                         cancel_event=cancel_event,
                         max_asset_bytes=MAX_RAYTRACE_ASSET_BYTES) as snapshot:
        progress("field_load", "Reading the captured per-step field arrays.")
        captured = _read_layer_fields(snapshot["path"], include_arrays=True,
                                      cancel_event=cancel_event)
        progress("asset_read", "Captured thickness arrays validated; reading the frozen process asset metadata.")
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
        render_parameters["substrate_profile"] = profile_for_adapter(source_profile)
        progress("film_adapter", "Joining captured fields to frozen FilmDesign layer IDs.")
        job = build_job_from_v7(
            process_profile, {"manifest": dict(field_manifest), "fields": captured},
            wavelengths_nm, render_parameters,
            geometry_thickness_mm=geometry_thickness_mm)
        progress("film_adapter_complete", "The frozen film stack passed input validation.")
        job["provenance"]["process_asset_sha256"] = snapshot["sha256"]
        return job
