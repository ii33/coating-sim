"""Read-only in-memory projections for project compute editor snapshots.

This module deliberately reads existing controller buffers only.  It does not
ask a controller to validate, refresh, estimate, resolve resources, or start
work; the public snapshot wrapper owns response sizing and pagination.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any


_MAIN_DRAFT_FIELDS = frozenset({
    "pending_information", "configuration_name", "configuration_description",
    "configuration_notes", "fields", "device", "algorithm", "algorithm_budget_s",
    "fast_cycle", "fast_cycle_residual_percent", "requested_time_step_s", "time_basis",
})
_REQUEST_SCALARS = ("schema", "schema_version", "units", "polarization", "detector")
_REQUEST_UNIT_FIELDS = ("length", "wavelength", "index")
_REQUEST_DETECTOR_FIELDS = ("z_mm", "aperture_radius_mm", "pixel_count")
_MAIN_VISIBLE_FIELDS = frozenset({"carrier_rpm", "carrier_phase_deg", "planet_phase_deg",
                                 "time_steps", "grid_size", "time_step_s"})
_APPEARANCE_SCALAR_FIELDS = frozenset({
    "geometry_thickness_mm", "camera_x", "camera_y", "camera_z",
    "target_x", "target_y", "target_z", "up_x", "up_y", "up_z", "fov_y_deg",
    "area_x", "area_y", "area_z", "area_target_x", "area_target_y", "area_target_z",
    "area_up_x", "area_up_y", "area_up_z", "area_size_x", "area_size_y", "area_scale",
    "environment_scale", "width", "height", "spp", "seed", "wavelength_start_nm",
    "wavelength_end_nm", "wavelength_step_nm", "area_enabled", "area_kind",
    "area_absolute_calibration", "environment_enabled", "environment_kind",
    "environment_absolute_calibration",
})
_SAMPLE_FIELDS = ("sample_id", "origin_xz_mm", "direction_xz", "weight")
_SURFACE_GEOMETRY_FIELDS = ("type", "z_mm", "center_z_mm", "radius_mm", "branch")
_LAYER_FIELDS = ("layer_id", "thickness_nm", "deposition_step_id", "global_to_grid_xy")
_QUEUE_ROW_FIELDS = ("job_id", "state", "stateLabel", "job_type", "kind", "task", "type",
                     "summary", "label", "display_name", "progress", "created_at", "submitted_at",
                     "started_at", "updated_at", "finished_at", "expected_finish_at",
                     "resultAvailable", "result_available")


def _hash_full(value: Any) -> str:
    """Hash complete JSON-like state incrementally without building a copy."""
    digest = sha256()

    def feed(item: Any) -> None:
        if item is None:
            digest.update(b"n;")
        elif item is True:
            digest.update(b"b1;")
        elif item is False:
            digest.update(b"b0;")
        elif isinstance(item, int):
            digest.update(b"i" + str(item).encode("ascii") + b";")
        elif isinstance(item, float):
            # Non-finite values are possible in an invalid draft and still need
            # a stable signature without being emitted to the public response.
            token = (repr(item) if not math.isfinite(item) else
                     json.dumps(item, allow_nan=False, separators=(",", ":")))
            digest.update(b"f" + token.encode("ascii") + b";")
        elif isinstance(item, str):
            raw = item.encode("utf-8", errors="surrogatepass")
            digest.update(b"s" + str(len(raw)).encode("ascii") + b":" + raw + b";")
        elif isinstance(item, Path):
            feed(str(item))
        elif isinstance(item, Mapping):
            digest.update(b"{")
            if any(not isinstance(key, str) for key in item):
                raise TypeError("unsupported signature mapping key")
            keys = sorted(item)
            for key in keys:
                feed(str(key))
                feed(item[key])
            digest.update(b"};")
        elif isinstance(item, (list, tuple)):
            digest.update(b"[")
            for value_item in item:
                feed(value_item)
            digest.update(b"];")
        elif isinstance(item, (bytes, bytearray, memoryview)):
            view = memoryview(item)
            digest.update(b"x" + str(view.nbytes).encode("ascii") + b":")
            digest.update(view)
            digest.update(b";")
        else:
            # A type-only token would make distinct unsupported state look equal.
            # Fail closed so callers return unavailable instead of a false signature.
            raise TypeError(f"unsupported signature value: {type(item).__name__}")

    feed(value)
    return digest.hexdigest()


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _scalar(value: Any) -> Any:
    """A public scalar, with nonfinite numeric input represented as null."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) else None


def _pair(value: Any) -> list[int | float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    first, second = _number(value[0]), _number(value[1])
    if first is None or second is None:
        return None
    return [first, second]


def _get(owner: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(owner, name, default)
    except (AttributeError, RuntimeError, TypeError):
        return default


def _resolve_controller(controller: Any, module: str) -> Any:
    # The public wrapper verifies and supplies the registered controller.  Never
    # substitute an arbitrary window property or controller candidate here.
    if module == "film_appearance" and controller is not None:
        if not (isinstance(_get(controller, "_draft"), Mapping)
                and isinstance(_get(controller, "_context"), Mapping)):
            return _get(controller, "_appearance")
    return controller


def _unavailable_reason(owner: Any, window: Any, *, reject_compute_busy: bool) -> str | None:
    if owner is None:
        return "controller_unavailable"
    for name in ("_closed", "_disposed", "_detached", "_observers_closed", "_shutting_down"):
        if _get(owner, name, False):
            return "window_closed"
    resources = _get(owner, "_project_input_resources")
    if resources is None:
        resources = _get(window, "projectResources")
    for name in ("_transfer_busy", "_resource_busy"):
        if bool(_get(resources, name, False)):
            return "resource_busy"
    # These are staged decisions or metadata edits.  Reading through one would
    # make the result ambiguous about which side of a confirmation is current.
    for candidate, names in (
        (owner, ("_pending_overwrite", "_pending_film_import", "_pending_information_edit",
                 "_pending_replacement", "_pending_replacement_id", "_pending_confirmation")),
        (resources, ("_pending_replacement", "_pending_replacement_id", "_pending_information_edit",
                     "_pending_ui", "_pending_confirmation")),
    ):
        for name in names:
            value = _get(candidate, name)
            if value is not None and value is not False and value != "":
                return "pending_confirmation"
    if reject_compute_busy:
        if bool(_get(owner, "_busy", False)):
            return "busy"
        if bool(_get(owner, "_observers_closed", False)):
            return "window_closed"
    return None


def _signature(module: str, raw: Any, *, scope: str = "complete") -> dict[str, Any]:
    return {"version": 1, "module_key": module, "scope": scope,
            "sha256": _hash_full(raw)}


def _main_snapshot(controller: Any) -> tuple[dict | None, str | None]:
    reason = _unavailable_reason(controller, None, reject_compute_busy=True)
    if reason:
        return None, reason
    if bool(_get(controller, "_result_view_only", False)):
        return None, "read_only_result_view"
    capture = _get(controller, "_project_draft_signature")
    if not callable(capture):
        return None, "controller_unavailable"
    try:
        draft = capture()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None, "draft_unavailable"
    if not isinstance(draft, Mapping):
        return None, "draft_unavailable"
    if draft.get("pending_information") is not None:
        return None, "pending_confirmation"
    compute = _get(controller, "_project_compute")
    configuration_id = _string(_get(compute, "configuration_id")) if compute is not None else None
    process_document_id = _string(_get(compute, "process_document_id")) if compute is not None else None
    fields = _map(draft.get("fields"))
    allowed_fields = {key: _scalar(fields[key]) for key in _MAIN_VISIBLE_FIELDS
                      if key in fields and (fields[key] is None or isinstance(fields[key], (str, bool, int, float)))}
    allowed = {key: _scalar(draft[key]) for key in _MAIN_DRAFT_FIELDS
               if key in draft and key != "fields" and
               (draft[key] is None or isinstance(draft[key], (str, bool, int, float)))}
    allowed["fields"] = allowed_fields
    raw_identity = {"configuration_id": configuration_id,
                    "process_document_id": process_document_id}
    target = _get(controller, "_compute_target")
    target_summary = ({"host_id": _string(_get(target, "_host_id")),
                       "device_id": _string(_get(target, "_device_id"))}
                      if target is not None else {})
    baseline = _get(compute, "_draft_baseline_signature") if compute is not None else None
    # Match ProjectComputeWorkspace._deposition_draft_signature exactly:
    # flattened selection keys plus the provider's canonical parameter object.
    payload = {**raw_identity, "parameters": dict(draft)}
    dirty_signature = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    result = _get(controller, "_result")
    result_map = _map(result)
    result_meta = {
        "available": isinstance(result, Mapping),
        "schema": _string(result_map.get("result_schema") or result_map.get("schema")),
        "run_id": _string(result_map.get("run_id") or _get(controller, "_asset_run_id", "")),
        "elapsed_s": _number(result_map.get("elapsed_s")),
        "asset_name": _string(_get(controller, "_asset_name", "")),
        "image_unit": _string(_get(controller, "_image_unit", "nm")),
    }
    # Invalid threshold edits deliberately do not replace the last numerical
    # setting or advance MainBridge._revision. They are still real visible UI
    # state and must invalidate an optimistic editor token independently from
    # the original canonical dirty/signature contract.
    invalid_threshold = _get(controller, "_fast_threshold_invalid")
    raw_controls = {"fast_threshold_text": _string(_get(controller, "_fast_threshold_text")),
                    "fast_threshold_invalid": invalid_threshold if isinstance(invalid_threshold, bool) else None}
    summary = {
        "module_key": "main", "draft": allowed,
        "raw_controls": raw_controls,
        "configuration_id": configuration_id,
        "process_document_id": process_document_id,
        "compute_target": target_summary,
        "revision": _scalar(_get(controller, "_revision", 0)),
        "dirty": baseline is None or dirty_signature != baseline,
        "status": _string(_get(controller, "_status", "")),
        "error": _string(_get(controller, "_uniformity_result_notice", "")),
        "error_severity": _string(_get(controller, "_uniformity_result_notice_severity", "info")),
        "progress": _number(_get(controller, "_progress_value", -1.0)),
        "result": result_meta,
    }
    raw = {"draft": dict(draft), "identity": raw_identity,
           "raw_controls": raw_controls,
           "compute_target": target_summary,
           "revision": _get(controller, "_revision", 0),
           "status": _get(controller, "_status"),
           "error": _get(controller, "_uniformity_result_notice", ""),
           "error_severity": _get(controller, "_uniformity_result_notice_severity", "info"),
           "progress": _get(controller, "_progress_value", -1.0),
           "result": result_meta,
           "dirty_signature": dirty_signature}
    return {"summary": summary, "collections": {}, "content_truncated": True,
            "signature": _signature("main", raw)}, None


def _ray_request_projection(request: Mapping) -> tuple[dict, dict[str, list]]:
    summary: dict[str, Any] = {}
    if "schema" in request:
        summary["schema"] = _string(request.get("schema"))
    if "schema_version" in request:
        summary["schema_version"] = _scalar(request.get("schema_version"))
    if "polarization" in request:
        summary["polarization"] = _string(request.get("polarization"))
    units = _map(request.get("units"))
    if units:
        summary["units"] = {key: _string(units[key]) for key in _REQUEST_UNIT_FIELDS
                             if key in units and isinstance(units[key], str)}
    detector = _map(request.get("detector"))
    if detector:
        summary["detector"] = {key: _number(detector[key]) for key in _REQUEST_DETECTOR_FIELDS
                                if key in detector and _number(detector[key]) is not None}
    collections: dict[str, list] = {"samples": [], "surfaces": [], "spectrum": [], "layers": []}
    samples = request.get("samples")
    if isinstance(samples, (list, tuple)):
        collections["samples"] = [
            {key: (_string(row[key]) if key == "sample_id" else
                   _pair(row[key]) if key in {"origin_xz_mm", "direction_xz"} else
                   _number(row[key])) for key in _SAMPLE_FIELDS if key in row and
             (key == "sample_id" and isinstance(row[key], str) or
              key in {"origin_xz_mm", "direction_xz"} and _pair(row[key]) is not None or
              key == "weight" and _number(row[key]) is not None)}
            if isinstance(row, Mapping) else {"invalid_record": True}
            for row in samples]
    spectrum = request.get("spectrum")
    if isinstance(spectrum, (list, tuple)):
        collections["spectrum"] = [
            {key: _number(row[key]) for key in ("wavelength_nm", "weight")
             if key in row and _number(row[key]) is not None}
            if isinstance(row, Mapping) else {"invalid_record": True}
            for row in spectrum]
    surfaces = request.get("surfaces")
    if isinstance(surfaces, (list, tuple)):
        for row in surfaces:
            if not isinstance(row, Mapping):
                collections["surfaces"].append({"invalid_record": True})
                continue
            geometry = _map(row.get("geometry"))
            coating = _map(row.get("coating"))
            medium_before = row.get("medium_before")
            medium_after = row.get("medium_after")
            coating_indices = coating.get("indices")
            surface_id = _string(row.get("surface_id"))
            layers = coating.get("layers")
            layer_rows = layers if isinstance(layers, (list, tuple)) else ()
            collections["surfaces"].append({
                "surface_id": _string(row.get("surface_id")),
                "geometry": {key: (_string(geometry[key]) if key in {"type", "branch"}
                                     else _number(geometry[key]))
                             for key in _SURFACE_GEOMETRY_FIELDS if key in geometry and
                             (key in {"type", "branch"} and isinstance(geometry[key], str)
                              or key not in {"type", "branch"} and _number(geometry[key]) is not None)},
                "aperture_radius_mm": _number(row.get("aperture_radius_mm")),
                # Port/coating refractive-index tables are intentionally omitted.
                "medium_before_sample_count": (len(medium_before)
                    if isinstance(medium_before, (list, tuple)) else None),
                "medium_after_sample_count": (len(medium_after)
                    if isinstance(medium_after, (list, tuple)) else None),
                "coating_present": "coating" in row,
                "coating_layer_count": len(layer_rows),
                "coating_index_row_count": (len(coating_indices)
                    if isinstance(coating_indices, (list, tuple)) else None),
            })
            for index, layer in enumerate(layer_rows):
                if isinstance(layer, Mapping):
                    collections["layers"].append({
                        "surface_id": surface_id, "index": index,
                        **{key: (_string(layer[key]) if key in {"layer_id", "deposition_step_id"}
                                 else _number(layer[key]) if key == "thickness_nm"
                                 else {"rotation_rad": _number(_map(layer[key]).get("rotation_rad")),
                                       "translation_xy_mm": _pair(_map(layer[key]).get("translation_xy_mm"))}
                                 if key == "global_to_grid_xy" else None)
                           for key in _LAYER_FIELDS if key in layer and
                           (key in {"layer_id", "deposition_step_id"} and isinstance(layer[key], str)
                            or key == "thickness_nm" and _number(layer[key]) is not None
                            or key == "global_to_grid_xy" and isinstance(layer[key], Mapping))},
                    })
                else:
                    collections["layers"].append({"surface_id": surface_id,
                                                  "index": index, "invalid_record": True})
    return summary, collections


def _ray_snapshot(controller: Any, module: str) -> tuple[dict | None, str | None]:
    reason = _unavailable_reason(controller, None, reject_compute_busy=True)
    if reason:
        return None, reason
    if bool(_get(controller, "_pending_project_task")):
        return None, "pending_project_operation"
    request = _get(controller, "_request")
    request = request if isinstance(request, Mapping) else {}
    request_summary, collections = _ray_request_projection(request)
    text = _get(controller, "_request_text", "")
    asset = _get(controller, "_asset")
    asset_map = _map(asset)
    fields = asset_map.get("fields")
    field_rows = fields if isinstance(fields, (list, tuple)) else ()
    field_ids = [_string(_map(row).get("step_id")) for row in field_rows
                 if isinstance(_map(row).get("step_id"), str)]
    information_raw = _map(_get(controller, "_managed_input_information"))
    information = {key: _string(information_raw[key]) for key in ("name", "description", "notes")
                   if key in information_raw and isinstance(information_raw[key], str)}
    managed = {"id": _string(_get(controller, "_managed_input_id")),
               "revision": _scalar(_get(controller, "_managed_input_revision")),
               "selected": (_get(controller, "_managed_input_selected")
                            if isinstance(_get(controller, "_managed_input_selected"), bool) else None)}
    target = _get(controller, "_compute_target")
    target_summary = {"host_id": _string(_get(target, "_host_id")),
                      "device_id": _string(_get(target, "_device_id"))} if target is not None else {}
    asset_summary = None
    if asset is not None:
        context = _map(asset_map.get("appearance_context"))
        raw_asset_path = asset_map.get("path")
        asset_filename = (Path(raw_asset_path).name if isinstance(raw_asset_path, (str, Path)) and raw_asset_path
                          else "")
        asset_summary = {
            "filename": asset_filename,
            "sha256": _string(asset_map.get("sha256")),
            "field_count": len(field_rows), "captured_step_ids": field_ids,
            "appearance_context_available": bool(context),
        }
    summary = {
        "module_key": module,
        "workspace_mode": _string(_get(controller, "_workspace_mode", "")),
        "request_present": isinstance(_get(controller, "_request"), Mapping),
        "request_text_length": len(text) if isinstance(text, str) else None,
        "request_parse_error": _string(_get(controller, "_request_error", "")),
        "request_path_name": (Path(_get(controller, "_request_path")).name
                              if isinstance(_get(controller, "_request_path"), (str, Path))
                              and _get(controller, "_request_path") else ""),
        "request": request_summary,
        "selected_surface_id": _string(_get(controller, "_selected_surface", "")),
        "selected_layer_id": _string(_get(controller, "_selected_layer", "")),
        "selected_step_id": _string(_get(controller, "_selected_step", "")),
        "selected_spectrum_index": _scalar(_get(controller, "_spectrum_index", 0)),
        "managed_input": managed,
        "managed_input_information": information,
        "asset": asset_summary,
        "compute_target": target_summary,
    }
    raw = {"request_text": text, "request": _get(controller, "_request"),
           "request_path": _get(controller, "_request_path"),
           "request_error": _get(controller, "_request_error"), "asset": asset,
           "managed_input": managed,
           "managed_input_information": _get(controller, "_managed_input_information"),
           "selected_surface": _get(controller, "_selected_surface"),
           "selected_layer": _get(controller, "_selected_layer"),
           "selected_step": _get(controller, "_selected_step"),
           "spectrum_index": _get(controller, "_spectrum_index"),
           "workspace_mode": _get(controller, "_workspace_mode"),
           "compute_target": target_summary}
    return {"summary": summary, "collections": collections, "content_truncated": True,
            "signature": _signature(module, raw)}, None


def _spectrum_summary(table: Any) -> dict | None:
    if not isinstance(table, Mapping):
        return None
    waves = table.get("wavelength_nm")
    values = table.get("value")
    wave_rows = [value for value in waves if isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(float(value))] if isinstance(waves, (list, tuple)) else []
    raw_path = table.get("path")
    filename = Path(raw_path).name if isinstance(raw_path, (str, Path)) and raw_path else ""
    return {
        "filename": filename,
        "sha256": _hash_full(table),
        "sample_count": max(len(waves) if isinstance(waves, (list, tuple)) else 0,
                             len(values) if isinstance(values, (list, tuple)) else 0),
        "wavelength_domain_nm": ([min(wave_rows), max(wave_rows)] if wave_rows else None),
    }


def _appearance_context_summary(context: Mapping) -> dict:
    substrate = _map(context.get("substrate_profile"))
    outline = _map(substrate.get("outline"))
    surface = _map(substrate.get("surface"))
    profile_fields = ("width_mm", "height_mm", "radius_mm", "material_id")
    profile = {key: _number(outline[key]) if key != "material_id" else _string(outline[key])
               for key in profile_fields if key in outline and
               (key == "material_id" and isinstance(outline[key], str) or
                key != "material_id" and _number(outline[key]) is not None)}
    if isinstance(outline.get("kind"), str):
        profile["outline_kind"] = outline["kind"]
    if isinstance(surface.get("kind"), str):
        profile["surface_kind"] = surface["kind"]
    captured = context.get("captured_step_ids")
    ids = list(captured) if isinstance(captured, (list, tuple)) else []
    raw_asset_path = context.get("asset_path")
    asset_filename = (Path(raw_asset_path).name if isinstance(raw_asset_path, (str, Path)) and raw_asset_path
                      else "")
    return {
        "asset_filename": asset_filename,
        "asset_sha256": _string(context.get("asset_sha256")),
        "process_profile_sha256": _string(context.get("process_profile_sha256")),
        "context_hash": _string(context.get("context_hash")),
        "captured_step_count": len(ids),
        "layer_field_units": _string(context.get("layer_field_units")),
        "layer_field_coordinate_frame": _string(context.get("layer_field_coordinate_frame")),
        "substrate": profile,
    }


def _appearance_snapshot(owner: Any, module: str) -> tuple[dict | None, str | None]:
    reason = _unavailable_reason(owner, None, reject_compute_busy=False)
    if reason:
        return None, reason
    if (_get(owner, "_process") is not None or _get(owner, "_probe_process") is not None
            or _get(owner, "_pending_project_finish") is not None):
        return None, "busy"
    draft = _get(owner, "_draft")
    if not isinstance(draft, Mapping):
        return None, "controller_unavailable"
    context = _get(owner, "_context")
    if not isinstance(context, Mapping) or not context:
        return None, "appearance_unavailable"
    # Exact whitelist: unknown input keys could contain paths, credentials or
    # future structured payloads and are not part of this public projection.
    scalar_draft = {key: _scalar(draft[key]) for key in _APPEARANCE_SCALAR_FIELDS
                    if key in draft and isinstance(draft[key], (str, bool, int, float))}
    spectra = {key: _spectrum_summary(draft.get(key)) for key in
               ("area_spectrum", "environment_spectrum")}
    gesture_active = bool(_get(owner, "_camera_gesture_active", False))
    if gesture_active:
        return None, "native_geometry_draft"
    target = _get(owner, "_compute_target")
    target_summary = {"host_id": _string(_get(target, "_host_id")),
                      "device_id": _string(_get(target, "_device_id"))} if target is not None else {}
    raw_steps = context.get("captured_step_ids")
    step_ids = [_string(value) for value in raw_steps
                if isinstance(value, str)] if isinstance(raw_steps, (list, tuple)) else []
    summary = {
        "module_key": module,
        "context": (_appearance_context_summary(context) | {"context_hash": _string(_get(owner, "_context_hash"))}),
        "draft": scalar_draft,
        "spectra": spectra,
        "geometry_thickness_mode": ("overridden" if _get(owner, "_geometry_thickness_overridden", False)
                                    else "inherited"),
        "backend": _string(_get(owner, "_backend")),
        "precision": _string(_get(owner, "_precision")),
        "compute_target": target_summary,
        "camera_gesture_active": False,
    }
    raw = {"context": context, "context_hash": _get(owner, "_context_hash"),
           "draft": draft, "geometry_thickness_overridden": _get(owner, "_geometry_thickness_overridden"),
           "backend": _get(owner, "_backend"), "precision": _get(owner, "_precision"),
           "compute_target": target_summary, "camera_gesture_active": gesture_active,
           "phase": _get(owner, "_phase"), "status": _get(owner, "_status")}
    return {"summary": summary, "collections": {"captured_steps": [{"step_id": value} for value in step_ids]},
            "content_truncated": True, "signature": _signature(module, raw)}, None


def _queue_snapshot(controller: Any, module: str) -> tuple[dict | None, str | None]:
    reason = _unavailable_reason(controller, None, reject_compute_busy=False)
    if reason:
        return None, reason
    buckets = _get(controller, "_jobs_by_bucket")
    if not isinstance(buckets, Mapping):
        return None, "controller_unavailable"
    active = _get(controller, "_active_bucket")
    if active not in ("recent", "history"):
        return None, "controller_unavailable"
    pages_loaded = _get(controller, "_pages_loaded", {})
    has_more = _get(controller, "_has_more", {})
    if not isinstance(pages_loaded, Mapping) or not isinstance(has_more, Mapping):
        return None, "controller_unavailable"
    loaded = pages_loaded.get(active, 0)
    raw_rows = buckets.get(active)
    raw_rows = list(raw_rows) if isinstance(raw_rows, (list, tuple)) else []
    initializing = not bool(loaded)
    if initializing:
        return None, "initializing"
    owns = _get(controller, "_owns_job")
    if not callable(owns):
        return None, "controller_unavailable"
    owned_rows = []
    for row in raw_rows:
        try:
            if not owns(row):
                continue
        except (RuntimeError, TypeError, ValueError, KeyError):
            return None, "invalid_editor_state"
        owned_rows.append(row)
    visible_rows: list = []
    if not initializing:
        if active == "recent":
            from .compute_queue import _visible_queue_rows
            visible_rows = _visible_queue_rows("recent", owned_rows)
            visible_rows = visible_rows[:int(_get(controller, "PAGE_SIZE", 12))]
        else:
            visible_rows = owned_rows
    pending = _get(controller, "_pending_cancels", set())
    confirmed = _get(controller, "_confirmed_cancels", set())
    cancel_ids = {value for values in (pending, confirmed)
                  if isinstance(values, (set, frozenset, list, tuple))
                  for value in values if isinstance(value, str)}
    projected_rows: list[dict] = []
    for row in visible_rows:
        if not isinstance(row, Mapping):
            continue
        item: dict[str, Any] = {}
        for key in _QUEUE_ROW_FIELDS:
            if key not in row:
                continue
            value = row[key]
            if key == "progress":
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    item["progress"] = {"fraction": _scalar(value)}
                else:
                    progress = _map(value)
                    item["progress"] = {name: _scalar(progress[name]) for name in
                        ("fraction", "completed_fraction", "stage", "completed", "total")
                        if name in progress and isinstance(progress[name], (str, int, float, bool, type(None)))}
            elif key in {"resultAvailable", "result_available"}:
                item["result_available"] = value if isinstance(value, bool) else None
            else:
                out_key = ("id" if key == "job_id" else
                           "state_label" if key == "stateLabel" else
                           "type" if key in {"job_type", "kind", "task", "type"} else
                           "label" if key in {"summary", "label", "display_name"} else key)
                if out_key not in item and isinstance(value, (str, bool, int, float)):
                    item[out_key] = _scalar(value)
        if "result_available" not in item:
            item["result_available"] = None
        item["cancel_pending"] = row.get("job_id") in cancel_ids
        projected_rows.append(item)
    summary = {
        "module_key": module, "scope": "cached_view", "active_bucket": active,
        "loaded_total": len(projected_rows), "has_more_persisted": bool(has_more.get(active, False)),
        "initializing": initializing,
        "fetching": bool(_get(controller, "_fetching", False)),
        "loading": bool(_get(controller, "_loading", False)) or bool(pending),
        "status": _string(_get(controller, "_status", "")),
    }
    visible_cancel_ids = [row["id"] for row in projected_rows
                          if row.get("cancel_pending") and isinstance(row.get("id"), str)]
    raw = {"jobs": owned_rows, "cancel_job_ids": sorted(visible_cancel_ids),
           "summary": summary, "active_bucket": active, "pages_loaded": pages_loaded,
           "has_more": has_more, "fetching": _get(controller, "_fetching", False),
           "loading": _get(controller, "_loading", False), "cursors": _get(controller, "_cursors", {})}
    return {"summary": summary, "collections": {"jobs": projected_rows},
            "content_truncated": True, "signature": _signature(module, raw, scope="cached_view")}, None


def _window_confirmation_pending(window: Any) -> bool:
    """Some native close decisions live only in an already-created QML popup."""
    finder = getattr(window, "findChild", None)
    if not callable(finder):
        return False
    from PySide6.QtCore import QObject
    dialog = finder(QObject, "rayTraceCloseDialog")
    return dialog is not None and bool(dialog.property("visible"))


def capture_compute_snapshot(controller: Any, window: Any,
                             module: str) -> tuple[dict | None, str | None]:
    """Capture the existing editor's bounded public projection and full signature."""
    module = str(module)
    if controller is None:
        return None, "controller_unavailable"
    try:
        if module in {"ray_trace", "film_appearance"} and _window_confirmation_pending(window):
            return None, "pending_confirmation"
        if module == "film_appearance":
            outer = controller
            reason = _unavailable_reason(outer, window, reject_compute_busy=True)
            if reason:
                return None, reason
            owner = _resolve_controller(outer, module)
            if owner is None:
                return None, "appearance_unavailable"
            # The nested view must belong to precisely the same project owner and
            # compute workspace as its ray trace bridge.
            if (_get(owner, "project_context") is not _get(outer, "project_context")
                    or _get(owner, "_project_compute") is not _get(outer, "_project_compute")):
                return None, "editor_owner_mismatch"
            result = _appearance_snapshot(owner, module)
        else:
            owner = controller
            if module == "main":
                result = _main_snapshot(owner)
            elif module == "ray_trace":
                result = _ray_snapshot(owner, module)
            elif module == "compute_queue":
                result = _queue_snapshot(owner, module)
            else:
                return None, "unsupported_module"
    except (TypeError, ValueError, OverflowError, RuntimeError, KeyError, AttributeError):
        return None, "invalid_editor_state"
    state, reason = result
    if state is None or reason:
        return None, reason or "editor_unavailable"
    return state, None


__all__ = ["capture_compute_snapshot"]
