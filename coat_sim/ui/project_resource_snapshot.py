"""Pure in-memory snapshots of the existing project resource editors.

This adapter intentionally reads retained bridge/controller state only.  It
does not load, validate, refresh, sample, or otherwise ask a service to derive
document data.  The caller owns identity checks and paging of the complete
collections returned here.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any


MODULES = frozenset({"source", "substrate", "machine", "mask", "process", "materials"})

_SOURCE_FIELDS = frozenset({
    "name", "description", "notes", "device_type", "kind", "source_exponent",
    "sampling_order", "length_mm", "angle_deg", "radius_mm", "width_mm",
    "inner_radius_mm", "outer_radius_mm",
})
_SOURCE_EMISSION_FIELDS = frozenset({
    "kind", "quadrature_order", "reference_rate_nm_s", "rate_exponent_per_decade",
    "active_fraction", "rate_active_fraction_per_decade", "center_weight", "boat_mode",
    "cover_height_mm", "cover_thickness_mm", "aperture_kind", "aperture_length_mm",
    "aperture_width_mm", "aperture_radius_mm", "aperture_offset_x_mm",
    "aperture_offset_y_mm", "spot_sigma_mm", "rate_spot_sigma_per_decade",
    "scan_pattern", "scan_width_mm", "scan_height_mm", "scan_center_x_mm",
    "scan_center_y_mm", "scan_angle_deg", "dwell_center_fraction", "dwell_points",
    "valid_rate_min_nm_s", "valid_rate_max_nm_s", "provenance_status",
    "provenance_reference", "provenance_notes",
})
_APERTURE_FIELDS = frozenset({
    "kind", "length_mm", "width_mm", "radius_mm", "offset_x_mm", "offset_y_mm", "angle_deg",
})
_MACHINE_SCALARS = frozenset({
    "schema_version", "name", "description", "notes", "carrier_x_mm", "carrier_y_mm",
    "planet_offset_mm", "planet_tilt_deg", "substrate_z_mm", "sun_teeth", "planet_teeth",
    "gear_module_mm",
})
_PROCESS_SCALARS = frozenset({
    "schema_version", "name", "description", "notes", "machine_file", "substrate_file",
})
_ENVIRONMENT_FIELDS = frozenset({
    "transport_model", "substrate_temperature_k", "source_temperature_k", "gas_temperature_k",
    "operating_pressure_pa", "base_pressure_pa", "effective_cross_section_m2",
    "effective_cross_section_source",
})
_GAS_COMPONENT_FIELDS = frozenset({
    "species", "partial_pressure_pa", "effective_cross_section_m2", "flow_sccm", "cross_section_source",
})
_PROCESS_STEP_FIELDS = frozenset({
    "source_slot", "target_nm", "rate_nm_s", "target_mode", "monitor_slot", "mask_slots",
    "film_layer_ref", "film_design_thickness_nm",
})
_MASK_SCALARS = frozenset({"schema_version", "name", "description", "notes", "z_mm"})
_MONITOR_FIELDS = frozenset({"name", "kind", "x_mm", "y_mm", "z_mm"})
_SOURCE_INSTALL_FIELDS = frozenset({"file", "x_mm", "y_mm", "z_mm"})
_MASK_INSTALL_FIELDS = frozenset({"file"})
_MASK_QML_CONTROLS = (
    ("tool_index", "currentIndex"),
    ("shift_x_mm", "text"), ("shift_y_mm", "text"),
    ("scale_x", "text"), ("scale_y", "text"),
    ("mask_slot", "value"), ("source_slot", "value"),
    ("angular_radius_mm", "value"), ("patch_radius_mm", "text"),
    ("auto_weight_y", "checked"),
    ("weight_y_min", "text"), ("weight_y_max", "text"),
    ("heat_weight_mode", "currentIndex"),
    ("rotation_polygon", "currentIndex"),
    ("rotation_center_x_mm", "text"), ("rotation_center_y_mm", "text"),
    ("rotation_angle_deg", "text"),
)
_SUBSTRATE_QML_CONTROLS = (
    ("tool_index", "currentIndex"), ("selected_point_index", "currentIndex"),
    ("point_x_mm", "text"), ("point_y_mm", "text"),
)
_MATERIAL_DRAFT_PREFIXES = ("resistance", "electron_beam")
_MATERIAL_CATALOG_FIELDS = frozenset({
    "id", "key", "name", "description", "builtin", "overridden", "opticalOnly", "roles",
    "category", "roleLabel", "sourceBindable", "substrateBindable", "formula", "origin", "globalReadOnly",
})


def _copy_scalar(value: Any) -> Any:
    """Copy finite JSON scalars only; arbitrary objects are deliberately omitted."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _fields(mapping: Any, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    return {key: _copy_scalar(mapping[key]) for key in allowed if key in mapping}


def _rows(value: Any, allowed: frozenset[str]) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for row in value:
        if isinstance(row, dict):
            result.append(_fields(row, allowed))
        else:
            result.append({})
    return result


def _points(value: Any) -> list[list[Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    points = []
    for point in value:
        if isinstance(point, (list, tuple)) and len(point) == 2:
            points.append([_copy_scalar(point[0]), _copy_scalar(point[1])])
        else:
            points.append([None, None])
    return points


def _canonical_digest(value: Any) -> str:
    digest = sha256()

    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=True,
                               separators=(",", ":"))
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _material_draft_fields(mapping: Any) -> dict[str, Any]:
    """Read only keys declared by the existing material bridge schema."""
    if not isinstance(mapping, dict):
        return {}
    allowed = {"id", "name", "description", "notes"}
    try:
        from .source_materials import MODEL_NUMBERS
    except (ImportError, AttributeError):
        MODEL_NUMBERS = {}
    for prefix in _MATERIAL_DRAFT_PREFIXES:
        for key in MODEL_NUMBERS.get(prefix, ()):
            allowed.add(prefix + "_" + key)
        for key in ("status", "reference", "notes"):
            allowed.add(prefix + "_provenance_" + key)
    return _fields(mapping, frozenset(allowed))


def _environment_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    projected = _fields(value, _ENVIRONMENT_FIELDS)
    components = value.get("gas_components")
    if isinstance(components, (list, tuple)):
        projected["gas_components"] = []
        for row in components:
            projected["gas_components"].append(
                _fields(row, _GAS_COMPONENT_FIELDS) if isinstance(row, dict) else {})
    return projected


def _optical_parameters(row: dict[str, Any]) -> dict[str, Any] | None:
    kind = row.get("kind")
    raw = row.get("parameters")
    if kind == "cauchy":
        allowed = {"A", "B", "C"}
    elif kind == "sellmeier":
        allowed = {f"{key}{index}" for key in ("B", "C") for index in (1, 2, 3)}
    else:
        return None
    return _fields(raw, frozenset(allowed)) if isinstance(raw, dict) else None


def _material_projection(material: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return safe embedded-material metadata and content-sensitive evidence.

    Optical sample vectors are never copied into output. Their content is
    included in a digest so same-length edits still invalidate a page.
    """
    if not isinstance(material, dict):
        return None, None
    identity = _fields(material, frozenset({
        "id", "name", "description", "notes", "category", "roles", "optical_only",
    }))
    # Roles are known small categorical metadata, not an arbitrary object.
    roles = material.get("roles")
    if isinstance(roles, (list, tuple)):
        identity["roles"] = [item for item in roles if isinstance(item, str)]
    sets = material.get("optical_constants", [])
    projected_sets: list[dict[str, Any]] = []
    optical_evidence: list[dict[str, Any]] = []
    content_truncated = False
    if isinstance(sets, (list, tuple)):
        for item in sets:
            if not isinstance(item, dict):
                continue
            row = _fields(item, frozenset({
                "id", "name", "notes", "source", "kind", "wavelength_min", "wavelength_max",
                "wavelength_unit", "formula_number",
            }))
            waves = item.get("wavelengths", item.get("wavelength_nm"))
            n_values, k_values = item.get("n"), item.get("k")
            n_count = len(n_values) if isinstance(n_values, (list, tuple)) else None
            k_count = len(k_values) if isinstance(k_values, (list, tuple)) else None
            if isinstance(waves, (list, tuple)):
                row["wavelength_count"] = len(waves)
            if n_count is not None:
                row["n_sample_count"] = n_count
            if k_count is not None:
                row["k_sample_count"] = k_count
            row["k_status"] = "available" if (k_count is not None or item.get("k_data") is not None) else "unknown"
            if "k_data" in item:
                row["k_data_present"] = bool(item.get("k_data"))
            if isinstance(item.get("k_data"), dict):
                row["k_domain"] = _fields(item["k_data"], frozenset({
                    "wavelength_min", "wavelength_max", "wavelength_unit",
                }))
                k_waves, k_samples = item["k_data"].get("wavelengths"), item["k_data"].get("k")
                row["k_sample_count"] = len(k_samples) if isinstance(k_samples, (list, tuple)) else None
                row["k_wavelength_count"] = len(k_waves) if isinstance(k_waves, (list, tuple)) else None
            parameters = _optical_parameters(item)
            if parameters is not None:
                row["parameters"] = parameters
            if isinstance(item.get("coefficients"), (list, tuple)):
                row["coefficients"] = [_copy_scalar(value) for value in item["coefficients"]]
            if isinstance(item.get("process_parameters"), (list, tuple)):
                row["process_parameters"] = [
                    _fields(value, frozenset({"name", "value", "unit"}))
                    for value in item["process_parameters"] if isinstance(value, dict)
                ]
            projected_sets.append(row)
            content_truncated |= any(isinstance(value, (list, tuple)) for value in
                                     (waves, n_values, k_values))
            if isinstance(item.get("k_data"), dict):
                content_truncated |= any(isinstance(item["k_data"].get(key), (list, tuple))
                                         for key in ("wavelengths", "k"))
            optical_evidence.append({
                "id": _copy_scalar(item.get("id")),
                "wavelength_digest": _canonical_digest(waves) if waves is not None else None,
                "n_digest": _canonical_digest(n_values) if n_values is not None else None,
                "k_digest": _canonical_digest(k_values) if k_values is not None else None,
                "k_data_digest": _canonical_digest(item.get("k_data")) if item.get("k_data") is not None else None,
            })
    projected = {**identity, "optical_sets": projected_sets,
                 "model_rate_unit": "nm_per_second"}
    # Include only model defaults and provenance metadata known to the
    # material schema; research_data and extension objects stay private.
    models = {}
    try:
        from .source_materials import MODEL_NUMBERS
    except (ImportError, AttributeError):
        MODEL_NUMBERS = {}
    for model_name in ("electron_beam_model", "resistance_model"):
        model = material.get(model_name)
        if not isinstance(model, dict):
            continue
        defaults = model.get("defaults")
        provenance = model.get("provenance")
        prefix = model_name.removesuffix("_model")
        models[model_name] = {
            "defaults": _fields(defaults, frozenset(MODEL_NUMBERS.get(prefix, ()))),
            "provenance": _fields(provenance, frozenset({"status", "reference", "notes"})),
        }
    if models:
        projected["models"] = models
    evidence = {"identity": identity, "optical": optical_evidence, "models": models,
                "content_truncated": content_truncated}
    return projected, evidence


def _material_bridge_projection(bridge: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    draft = _material_draft_fields(getattr(bridge, "_draft", None))
    catalog = []
    for row in getattr(bridge, "_entry_cache", ()) or ():
        if isinstance(row, dict):
            item = _fields(row, _MATERIAL_CATALOG_FIELDS)
            roles = row.get("roles")
            if isinstance(roles, (list, tuple)):
                item["roles"] = [role for role in roles if isinstance(role, str)]
            catalog.append(item)
    optical = []
    optical_evidence = []
    content_truncated = False
    for row in getattr(bridge, "_optical_sets", ()) or ():
        if not isinstance(row, dict):
            continue
        item = _fields(row, frozenset({"id", "name", "notes", "source", "kind", "wavelength_min",
                                      "wavelength_max", "wavelength_unit", "formula_number"}))
        waves = row.get("wavelengths", row.get("wavelength_nm"))
        n_values, k_values = row.get("n"), row.get("k")
        if isinstance(waves, (list, tuple)):
            item["wavelength_count"] = len(waves)
        if isinstance(n_values, (list, tuple)):
            item["n_sample_count"] = len(n_values)
        if isinstance(k_values, (list, tuple)):
            item["k_sample_count"] = len(k_values)
        k_data = row.get("k_data")
        item["k_status"] = "available" if (isinstance(k_values, (list, tuple)) or k_data is not None) else "unknown"
        if k_data is not None:
            item["k_data_present"] = True
        if isinstance(k_data, dict):
            item["k_domain"] = _fields(k_data, frozenset({"wavelength_min", "wavelength_max", "wavelength_unit"}))
            k_waves, k_samples = k_data.get("wavelengths"), k_data.get("k")
            item["k_sample_count"] = len(k_samples) if isinstance(k_samples, (list, tuple)) else None
            item["k_wavelength_count"] = len(k_waves) if isinstance(k_waves, (list, tuple)) else None
            content_truncated |= any(isinstance(k_data.get(key), (list, tuple)) for key in ("wavelengths", "k"))
        parameters = _optical_parameters(row)
        if parameters is not None:
            item["parameters"] = parameters
        if isinstance(row.get("coefficients"), (list, tuple)):
            item["coefficients"] = [_copy_scalar(value) for value in row["coefficients"]]
        if isinstance(row.get("process_parameters"), (list, tuple)):
            item["process_parameters"] = [
                _fields(value, frozenset({"name", "value", "unit"}))
                for value in row["process_parameters"] if isinstance(value, dict)
            ]
        optical.append(item)
        content_truncated |= any(isinstance(value, (list, tuple)) for value in (waves, n_values, k_values))
        optical_evidence.append({
            "id": _copy_scalar(row.get("id")),
            "wavelength_digest": _canonical_digest(waves) if waves is not None else None,
            "n_digest": _canonical_digest(n_values) if n_values is not None else None,
            "k_digest": _canonical_digest(k_values) if k_values is not None else None,
            "k_data_digest": _canonical_digest(k_data) if k_data is not None else None,
        })
    revision = _copy_scalar(getattr(bridge, "_optical_set_data_revision", None))
    collection = {"catalog": catalog, "optical_sets": optical}
    evidence = {"optical_sets": optical_evidence, "optical_set_data_revision": revision,
                "content_truncated": content_truncated}
    is_global = bool(getattr(bridge, "_global_selected_id", None))
    is_builtin = bool(getattr(bridge, "_builtin", False))
    raw_draft = getattr(bridge, "_draft", None)
    draft_id = raw_draft.get("id") if isinstance(raw_draft, dict) else None
    source_asset_id = _copy_scalar(getattr(bridge, "projectionAssetId", ""))
    is_project = bool(getattr(bridge, "_saved_id", None)) or bool(source_asset_id) or bool(
        draft_id in (getattr(bridge, "_project_material_ids", set()) or set()))
    summary = {"draft": draft, "source_asset_id": source_asset_id,
               "profile_persisted": bool(getattr(bridge, "_saved_id", None)), "optical_set_data_revision": revision,
               "optical_only": bool(getattr(bridge, "_optical_only", False)),
               "null_models": [prefix for prefix in _MATERIAL_DRAFT_PREFIXES
                               if prefix in (getattr(bridge, "_null_models", ()) or ())],
               "field_value_representation": "raw_ui_text",
               "field_units": {"draft." + key: "angstrom_per_second" for key in draft
                   if key in {prefix + "_" + rate for prefix in _MATERIAL_DRAFT_PREFIXES
                              for rate in ("reference_rate_nm_s", "valid_rate_min_nm_s", "valid_rate_max_nm_s")}},
               "origin": "global" if is_global else "builtin" if is_builtin else "project" if is_project else "draft",
               "global": is_global,
               "project": is_project,
               "builtin": bool(getattr(bridge, "_builtin", False)),
               "selected_key": _copy_scalar(getattr(bridge, "_selected_key", "")),
               "saved_id": _copy_scalar(getattr(bridge, "_saved_id", None)),
               "global_selected_id": _copy_scalar(getattr(bridge, "_global_selected_id", None)),
               "purpose": _copy_scalar(getattr(bridge, "_purpose", None)),
               "category": _copy_scalar(getattr(bridge, "_category", None)),
               "roles": [role for role in (getattr(bridge, "_roles", None) or []) if isinstance(role, str)]}
    return summary, {**evidence, "collections": collection}


def _property(obj: Any, name: str, default: Any = None) -> Any:
    getter = getattr(obj, "property", None)
    if callable(getter):
        try:
            value = getter(name)
            return default if value is None else value
        except (RuntimeError, TypeError, AttributeError):
            return default
    return getattr(obj, name, default)


def _visible_material_editor(window: Any) -> bool:
    finder = getattr(window, "findChild", None)
    if callable(finder):
        try:
            from PySide6.QtCore import QObject
            editor = finder(QObject, "materialEditor")
        except (ImportError, RuntimeError, TypeError):
            editor = None
        if editor is not None:
            visible = _property(editor, "visible", None)
            if visible is None:
                is_visible = getattr(editor, "isVisible", None)
                visible = is_visible() if callable(is_visible) else False
            if bool(visible):
                return True
    # QML incubators can temporarily hold an existing editor instance outside
    # the QObject child tree; inspect it without creating or opening anything.
    for holder_name in ("materialLibraryDialogInstance", "editorInstance", "setEditorInstance"):
        holder = _property(window, holder_name)
        if holder is None:
            continue
        nested_finder = getattr(holder, "findChild", None)
        if callable(nested_finder):
            try:
                from PySide6.QtCore import QObject
                editor = nested_finder(QObject, "materialEditor")
            except (ImportError, RuntimeError, TypeError):
                editor = None
            if editor is not None and bool(_property(editor, "visible", False)):
                return True
        editor = _property(holder, "setEditorInstance")
        if editor is not None and bool(_property(editor, "visible", False)):
            return True
    return False


def _find_child(window: Any, name: str) -> Any:
    finder = getattr(window, "findChild", None)
    if not callable(finder):
        return None
    try:
        from PySide6.QtCore import QObject
        return finder(QObject, name)
    except (ImportError, RuntimeError, TypeError):
        return None


def _visible_named_dialog(window: Any, name: str) -> Any:
    dialog = _find_child(window, name)
    if dialog is None:
        return None
    visible = _property(dialog, "visible", None)
    if visible is None:
        opened = _property(dialog, "opened", False)
        visible = opened
    return dialog if bool(visible) else None


def _nested_library_has_draft(window: Any) -> bool:
    dialogs = [_visible_named_dialog(window, name) for name in (
        "materialLibraryDialog", "substrateMaterialLibraryDialog")]
    dialogs.append(_property(window, "materialLibraryDialogInstance"))
    for dialog in dialogs:
        if dialog is None:
            continue
        visible = _property(dialog, "visible", None)
        if visible is None:
            visible = _property(dialog, "opened", False)
        if not bool(visible):
            continue
        library = _property(dialog, "library")
        if library is not None and bool(getattr(library, "_dirty", False)):
            return True
    return False


def _pending_confirmation(bridge: Any) -> bool:
    # Inspect only an already-created information facade, never its lazy property.
    information = getattr(bridge, "_information_controller", None)
    if getattr(information, "_pending_information_edit", None) is not None:
        return True
    # Even an empty import plan is a real, unapplied decision owned by the dialog.
    if getattr(bridge, "_import_preview_plan", None) is not None:
        return True
    for name in ("_pending_reload", "_pending_save_as", "_pending_document_replace",
                 "_pending_action", "_pending"):
        value = getattr(bridge, name, None)
        if value not in (None, "", False, (), [], {}):
            return True
    return False


def _common_fence(controller: Any, window: Any, module: str) -> tuple[Any, str | None]:
    if module not in MODULES:
        return None, "unsupported_module"
    if controller is None:
        return None, "window_closed"
    if getattr(controller, "module_key", module) != module:
        return None, "module_mismatch"
    bridge = getattr(controller, "bridge", None)
    if bridge is None:
        return None, "bridge_unavailable"
    if getattr(controller, "_disposed", False):
        return None, "disposed"
    if getattr(controller, "_opening", False):
        return None, "opening"
    if getattr(controller, "_transfer_busy", False):
        return None, "transfer_busy"
    if getattr(bridge, "loading", False):
        return None, "loading"
    if (_pending_confirmation(bridge)
            or _visible_named_dialog(window, "materialLibraryImportPreviewDialog") is not None
            or _visible_named_dialog(window, "restoreBuiltinDialog") is not None
            or _visible_named_dialog(window, "materialLibraryDeleteDialog") is not None):
        return None, "pending_confirmation"
    if getattr(controller, "_pending_information_edit", None) is not None:
        return None, "pending_information_edit"
    if getattr(controller, "_pending_ui", None) is not None:
        return None, "pending_ui_edit"
    pending_reader = getattr(controller, "snapshot_pending_edits", None)
    if callable(pending_reader):
        try:
            if pending_reader() is not None:
                return None, "native_field_editing"
        except Exception:
            return None, "native_field_state_unavailable"
    if module in {"source", "substrate"}:
        library = getattr(bridge, "_material_library", None)
        if library is not None:
            if getattr(library, "loading", False):
                return None, "loading"
            if _pending_confirmation(library):
                return None, "pending_confirmation"
    if module == "mask":
        pointer = _property(window, "draftPointer", "")
        points = _property(window, "draftPoints", [])
        from PySide6.QtQml import QJSValue
        pending_points = (bool(points) if isinstance(points, (list, tuple)) else
                          points.isArray() and points.property("length").toInt() > 0
                          if isinstance(points, QJSValue) else False)
        if pointer or pending_points:
            return None, "native_geometry_draft"
        if any(_visible_named_dialog(window, name) is not None for name in (
                "maskOptimizationDialog", "maskImportReplaceConfirm", "maskImportOptionsDialog",
                "maskCadImportFileDialog")):
            return None, "modal_editor_draft"
    if module == "process" and any(_visible_named_dialog(window, name) is not None for name in (
            "processEnvironmentDialog", "filmDesignImportDialog", "filmStackDialog")):
        return None, "modal_editor_draft"
    if module in {"source", "substrate"} and _nested_library_has_draft(window):
        return None, "nested_material_draft"
    if module in {"materials", "source", "substrate"} and _visible_material_editor(window):
        return None, "modal_editor_draft"
    return bridge, None


def _status_fields(bridge: Any) -> dict[str, Any]:
    """Read already-computed editor status without invoking bridge properties."""
    valid = getattr(bridge, "_valid", None)
    if valid is None:
        valid = getattr(bridge, "_ok", getattr(bridge, "_can_save", None))
    error = getattr(bridge, "_error", None)
    if not error:
        error = getattr(bridge, "_validation", None)
    if not error:
        errors = getattr(bridge, "_errors", None)
        if isinstance(errors, (list, tuple)):
            error = "\n".join(value for value in errors if isinstance(value, str))
    return {"valid": _copy_scalar(valid), "error": _copy_scalar(error),
            "status": _copy_scalar(getattr(bridge, "_status", None))}


def _view_scalar(window: Any, name: str) -> Any:
    value = _property(window, name)
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _control_values(window: Any, controls: tuple[tuple[str, str], ...]) -> dict[str, dict[str, Any]]:
    """Read only the explicit QML control contract from existing children."""
    finder = getattr(window, "findChild", None)
    if not callable(finder):
        return {}
    try:
        from PySide6.QtCore import QObject
    except ImportError:
        return {}
    values: dict[str, dict[str, Any]] = {}
    for key, prop in controls:
        try:
            control = finder(QObject, "coatcontrol:" + key)
            if control is None:
                continue
            value = control.property(prop)
            if isinstance(value, float) and not math.isfinite(value):
                value = None
            elif not isinstance(value, (str, bool, int, float, type(None))):
                continue
            enabled = control.property("enabled")
            values[key] = {"property": prop, "value": value,
                           "enabled": enabled if isinstance(enabled, bool) else None}
        except (RuntimeError, TypeError, AttributeError):
            continue
    return values


def _resource_busy(bridge: Any, module: str) -> bool:
    if module == "mask":
        if any(getattr(bridge, name, False) is True for name in (
                "_radial_busy", "_field_busy", "_point_busy", "_cad_busy")):
            return True
        coordinator = getattr(bridge, "_optimization_coordinator", None)
        optimization = getattr(coordinator, "_optimization", None)
        return isinstance(optimization, dict) and optimization.get("busy") is True
    if module == "process":
        optimization = getattr(bridge, "_optimization", None)
        return isinstance(optimization, dict) and optimization.get("busy") is True
    return False


def _capture_resource_snapshot(controller: Any, window: Any, module: str) -> tuple[dict | None, str | None]:
    """Copy an existing project resource draft into a safe, fully pageable view.

    All collections are returned in full (not just the editor's visible page).
    The signature carries the complete projected state plus content digests for
    optical evidence whose sample arrays must not be exposed.
    """
    bridge, reason = _common_fence(controller, window, module)
    if reason is not None:
        return None, reason
    if _resource_busy(bridge, module):
        return None, "busy"

    summary: dict[str, Any] = {"module": module, "projection_scope": "current_editor_fields_and_named_controls"}
    collections: dict[str, list[dict[str, Any]]] = {}
    evidence: dict[str, Any] = {}

    if module == "source":
        fields = _fields(getattr(bridge, "_fields", None), _SOURCE_FIELDS)
        emission = _fields(getattr(bridge, "_emission", None), _SOURCE_EMISSION_FIELDS)
        apertures = _rows(getattr(bridge, "_apertures", None), _APERTURE_FIELDS)
        material, material_evidence = _material_projection(getattr(bridge, "_material", None))
        summary.update({"fields": fields, "emission": emission,
                        "emission_present": bool(getattr(bridge, "_emission_present", False)),
                        "aperture_index": _copy_scalar(getattr(bridge, "_aperture_index", None)),
                        "preview_rate": _copy_scalar(getattr(bridge, "_preview_rate", None)),
                        "field_value_representation": "raw_ui_text",
                        "field_units": {"emission." + key: "angstrom_per_second" for key in
                            ("reference_rate_nm_s", "valid_rate_min_nm_s", "valid_rate_max_nm_s")}
                            | {"preview_rate": "angstrom_per_second"},
                        "material_validated": bool(getattr(bridge, "_material_validated", False)),
                        "reference_loading": bool(getattr(bridge, "_reference_loading", False)),
                        "material": material})
        collections = {"apertures": apertures, "materials": [] if material is None else [material]}
        evidence = {"material": material_evidence}
    elif module == "substrate":
        fields = _fields(getattr(bridge, "_fields", None), frozenset({
            "name", "description", "notes", "outline_kind", "surface_kind", "radius_mm",
            "width_mm", "height_mm", "roc_mm", "conic_k", "a4", "a6", "a8",
        }))
        points = _points(getattr(bridge, "_points", None))
        material, material_evidence = _material_projection(getattr(bridge, "_material", None))
        summary.update({"fields": fields, "closed": bool(getattr(bridge, "_closed", False)),
                        "selected_index": _copy_scalar(getattr(bridge, "_selected", None)),
                        "material_validated": bool(getattr(bridge, "_material_validated", False)),
                        "reference_loading": bool(getattr(bridge, "_reference_loading", False)),
                        "material": material})
        summary["controls"] = _control_values(window, _SUBSTRATE_QML_CONTROLS)
        collections = {"points": [{"point_index": i, "x": point[0], "y": point[1]}
                                   for i, point in enumerate(points)],
                       "materials": [] if material is None else [material]}
        evidence = {"material": material_evidence}
    elif module == "machine":
        data = getattr(bridge, "_data", None)
        scalars = _fields(data, _MACHINE_SCALARS)
        sources = _rows(data.get("sources") if isinstance(data, dict) else None, _SOURCE_INSTALL_FIELDS)
        masks = _rows(data.get("masks") if isinstance(data, dict) else None, _MASK_INSTALL_FIELDS)
        monitors = _rows(data.get("monitors") if isinstance(data, dict) else None, _MONITOR_FIELDS)
        summary.update(scalars)
        summary.update(_status_fields(bridge))
        collections = {"sources": sources, "masks": masks, "monitors": monitors}
    elif module == "mask":
        data = getattr(bridge, "_data", None)
        scalars = _fields(data, _MASK_SCALARS)
        contours: list[dict[str, Any]] = []
        if isinstance(data, dict):
            for index, point in enumerate(_points(data.get("outer"))):
                contours.append({"contour": "outer", "hole_index": -1,
                                 "point_index": index, "x": point[0], "y": point[1]})
            holes = data.get("holes", [])
            if isinstance(holes, (list, tuple)):
                for hole_index, hole in enumerate(holes):
                    for point_index, point in enumerate(_points(hole)):
                        contours.append({"contour": "hole", "hole_index": hole_index,
                                         "point_index": point_index, "x": point[0], "y": point[1]})
        summary.update(scalars)
        summary.update(_status_fields(bridge))
        machine = getattr(bridge, "_machine", None)
        machine_data = _fields(machine, _MACHINE_SCALARS)
        summary["parameters"] = {
            "workpiece_diameter_mm": _copy_scalar(getattr(bridge, "_workpiece_diameter_mm", None)),
            "last_ring_z": _copy_scalar(getattr(bridge, "_last_ring_z", None)),
            "source_sample_counts": [value for value in (getattr(bridge, "_source_sample_counts", []) or [])
                                     if isinstance(value, int) and not isinstance(value, bool)],
            "estimate_revision": _copy_scalar(getattr(bridge, "_estimate_revision", None)),
            "seconds_per_work_unit": _fields(getattr(bridge, "_seconds_per_work_unit", None),
                                              frozenset({"radial", "field", "point"})),
        }
        summary["machine_identity"] = {
            "path": _copy_scalar(getattr(bridge, "_machine_path", None)),
            "name": machine_data.get("name"),
            "source_count": len(machine.get("sources", [])) if isinstance(machine, dict) and isinstance(machine.get("sources"), list) else None,
            "mask_count": len(machine.get("masks", [])) if isinstance(machine, dict) and isinstance(machine.get("masks"), list) else None,
        }
        evidence["preview_machine_sha256"] = _canonical_digest(machine)
        summary["view_state"] = {key: _view_scalar(window, key) for key in (
            "maskSidebarPage", "toolIndex", "contourMode", "selectedHole", "selectedPoint",
            "selectedPolygon", "rotationContourIndex", "hasAnalysisPoint", "analysisX", "analysisY",
        )}
        summary["view_state"] = {key: value for key, value in summary["view_state"].items() if value is not None}
        summary["controls"] = _control_values(window, _MASK_QML_CONTROLS)
        summary["cad_revision"] = _copy_scalar(getattr(bridge, "_cad_revision", None))
        provenance = getattr(bridge, "_cad_provenance", None)
        summary["cad_provenance_present"] = bool(provenance)
        evidence["cad_provenance_sha256"] = _canonical_digest(provenance)
        summary["analysis_state"] = {
            "radial_available": bool(getattr(bridge, "_weights", None)),
            "point_available": bool(getattr(bridge, "_point_weights", None)),
            "field_available": bool(getattr(bridge, "_field", None)),
            "radial_generation": _copy_scalar(getattr(bridge, "_request_id", None)),
            "point_generation": _copy_scalar(getattr(bridge, "_point_request_id", None)),
            "field_generation": _copy_scalar(getattr(bridge, "_field_request_id", None)),
        }
        collections = {"contour_points": contours}
    elif module == "process":
        data = getattr(bridge, "_data", None)
        scalars = _fields(data, _PROCESS_SCALARS)
        step_rows: list[dict[str, Any]] = []
        if isinstance(data, dict) and isinstance(data.get("steps"), (list, tuple)):
            for row in data["steps"]:
                if not isinstance(row, dict):
                    step_rows.append({})
                    continue
                step = _fields(row, _PROCESS_STEP_FIELDS - {"mask_slots", "film_layer_ref"})
                nonfinite = {
                    key: ("NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity")
                    for key, value in row.items() if key in _PROCESS_STEP_FIELDS
                    and isinstance(value, float) and not math.isfinite(value)
                }
                if nonfinite:
                    step["nonfinite_numbers"] = nonfinite
                if "environment" in row:
                    step["environment"] = _environment_projection(row.get("environment"))
                mask_slots = row.get("mask_slots")
                if isinstance(mask_slots, (list, tuple)):
                    step["mask_slots"] = [slot for slot in mask_slots if isinstance(slot, int) and not isinstance(slot, bool)]
                layer_ref = row.get("film_layer_ref")
                if isinstance(layer_ref, dict):
                    step["film_layer_ref"] = _fields(layer_ref, frozenset({"surface", "layer_id"}))
                step_rows.append(step)
        source_rows: list[dict[str, Any]] = []
        preview = getattr(bridge, "_source_preview", None)
        preview_sources = preview.get("sources") if isinstance(preview, dict) else None
        if isinstance(preview_sources, (list, tuple)):
            allowed = frozenset({"slot", "file", "name", "material_name", "valid", "default_rate_nm_s", "message"})
            source_rows = _rows(preview_sources, allowed)
        summary.update(scalars)
        summary.update(_status_fields(bridge))
        environment = _environment_projection(data.get("environment")) if isinstance(data, dict) else {}
        summary["environment"] = environment
        summary["environment_present"] = isinstance(data, dict) and "environment" in data
        preview_masks = preview.get("masks") if isinstance(preview, dict) else None
        summary["mask_count"] = len(preview_masks) if isinstance(preview_masks, (list, tuple)) else None
        evidence["source_preview_sha256"] = _canonical_digest(preview)
        summary["step_page"] = _copy_scalar(getattr(bridge, "_step_page", None))
        collections = {"steps": step_rows, "sources": source_rows}
    else:  # materials
        projected, material_evidence = _material_bridge_projection(bridge)
        summary.update(projected)
        collections = material_evidence.pop("collections")
        evidence = material_evidence

    # These editors retain JSON document drafts. Bind every retained member,
    # including absent/null/empty and nonfinite invalid states, without exposing
    # arbitrary document keys or copying optical/result sample arrays. This is
    # pure incremental encoding, not document normalization or service access.
    if module in {"machine", "mask", "process"}:
        evidence["document_sha256"] = _canonical_digest(getattr(bridge, "_data", None))

    # Bind the retained document handle without asking the service to resolve
    # it (currentDocumentId can load documents).  Only a lexical filename and
    # digest escape; same-named, identical documents still have distinct IDs.
    if module != "materials":
        cached_path = getattr(bridge, "_path", None)
        if cached_path is not None and not isinstance(cached_path, (str, Path)):
            raise TypeError("unsupported retained document path")
        path_text = str(cached_path) if cached_path is not None else ""
        summary["document_identity"] = {
            "filename": Path(path_text).name if path_text else "",
            "path_sha256": sha256(path_text.encode("utf-8")).hexdigest() if path_text else None,
        }

    # Text and collection values are existing committed bridge state. Leave
    # them intact for the public read projector to truncate as appropriate.
    summary["dirty"] = bool(getattr(bridge, "_dirty", False))
    summary["revision"] = _copy_scalar(getattr(bridge, "_draft_revision", None))
    summary.update({key: value for key, value in _status_fields(bridge).items() if value is not None})
    if module == "materials":
        summary["dirty"] = bool(getattr(bridge, "_dirty", False))
        summary["revision"] = _copy_scalar(getattr(bridge, "_optical_set_data_revision", 0))
    signature_state = {"summary": summary, "collections": collections}
    signature: dict[str, Any] = {
        "module": module,
        "sha256": _canonical_digest({"state": signature_state, "evidence": evidence}),
    }
    content_truncated = bool(evidence.get("content_truncated", False))
    if isinstance(evidence.get("material"), dict):
        content_truncated |= bool(evidence["material"].get("content_truncated", False))
    result: dict[str, Any] = {"summary": summary, "collections": collections, "signature": signature}
    if content_truncated:
        result["content_truncated"] = True
    return result, None


def capture_resource_snapshot(controller: Any, window: Any, module: str) -> tuple[dict | None, str | None]:
    """Capture a snapshot, failing closed when retained state is not JSON-safe."""
    try:
        return _capture_resource_snapshot(controller, window, module)
    except (TypeError, ValueError, RuntimeError):
        return None, "invalid_editor_state"


__all__ = ["capture_resource_snapshot"]
