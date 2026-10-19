"""Narrow current-editor operations for the six project resource bridges.

Only the existing in-memory editor setters are called here. This module does
not open, save, import, select, calculate, export, or otherwise start work.
The project gateway owns current-editor identity and pending-edit fences.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import math
from typing import Any, NoReturn

from coat_sim.control_errors import UiControlError


RESOURCE_EDIT_MODULES = frozenset({"source", "substrate", "machine", "mask", "process", "materials"})

SOURCE_FIELDS = frozenset({"name", "description", "notes", "device_type", "kind",
    "source_exponent", "sampling_order", "length_mm", "angle_deg", "radius_mm",
    "width_mm", "inner_radius_mm", "outer_radius_mm"})
SOURCE_EMISSION_FIELDS = frozenset({"kind", "quadrature_order", "reference_rate_nm_s",
    "rate_exponent_per_decade", "active_fraction", "rate_active_fraction_per_decade",
    "center_weight", "boat_mode", "cover_height_mm", "cover_thickness_mm",
    "spot_sigma_mm",
    "rate_spot_sigma_per_decade", "scan_pattern", "scan_width_mm", "scan_height_mm",
    "scan_center_x_mm", "scan_center_y_mm", "scan_angle_deg", "dwell_center_fraction",
    "dwell_points", "valid_rate_min_nm_s", "valid_rate_max_nm_s", "provenance_status",
    "provenance_reference", "provenance_notes"})
APERTURE_FIELDS = frozenset({"kind", "length_mm", "width_mm", "radius_mm",
                             "offset_x_mm", "offset_y_mm", "angle_deg"})
SUBSTRATE_FIELDS = frozenset({"name", "description", "notes", "outline_kind", "surface_kind",
    "radius_mm", "width_mm", "height_mm", "roc_mm", "conic_k", "a4", "a6", "a8"})
MACHINE_GEOMETRY_FIELDS = frozenset({"carrier_x_mm", "carrier_y_mm", "planet_offset_mm",
    "planet_tilt_deg", "substrate_z_mm", "sun_teeth", "planet_teeth", "gear_module_mm"})
MACHINE_METADATA_FIELDS = frozenset({"name", "description", "notes"})
MONITOR_FIELDS = frozenset({"name", "kind", "x_mm", "y_mm", "z_mm"})
MASK_FIELDS = frozenset({"name", "description", "notes", "z_mm"})
PROCESS_FIELDS = frozenset({"name", "description", "notes"})
PROCESS_STEP_FIELDS = frozenset({"target_nm", "rate_nm_s", "rate_angstrom_s",
                                "target_mode", "monitor_slot"})
MATERIAL_MODEL_NUMBERS = {
    "resistance": ("source_exponent", "reference_rate_nm_s", "rate_exponent_per_decade",
                   "active_fraction", "rate_active_fraction_per_decade", "center_weight",
                   "valid_rate_min_nm_s", "valid_rate_max_nm_s"),
    "electron_beam": ("source_exponent", "reference_rate_nm_s", "rate_exponent_per_decade",
                      "spot_sigma_mm", "rate_spot_sigma_per_decade",
                      "valid_rate_min_nm_s", "valid_rate_max_nm_s"),
}
MATERIAL_RATE_KEYS = frozenset({"reference_rate_nm_s", "valid_rate_min_nm_s", "valid_rate_max_nm_s"})

_SOURCE_EMISSION_ENUMS = {
    "kind": frozenset({"uniform", "resistance_boat", "electron_beam"}),
    "boat_mode": frozenset({"open", "covered"}),
    "scan_pattern": frozenset({"point", "line", "raster", "ring", "custom"}),
    "provenance_status": frozenset({"unvalidated", "theoretical", "calibrated"}),
}


def _field(type_name: str, *, unit: str | None = None,
           enum: tuple[str, ...] | None = None, stored_unit: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": type_name}
    if type_name == "string":
        result.update(maxLength=20000, nulAllowed=False, utf8Encodable=True)
    if unit is not None:
        result["unit"] = unit
    if enum is not None:
        result["enum"] = list(enum)
    if stored_unit is not None:
        result["stored_unit"] = stored_unit
    return result


def _source_field_schema() -> dict[str, Any]:
    result = {key: _field("string") for key in SOURCE_FIELDS}
    result["kind"] = _field("string", enum=("point", "line", "disk", "annulus", "rectangle"))
    result["device_type"] = _field("string", enum=("custom", "resistance_boat", "crucible", "electron_beam"))
    for key, unit in (("source_exponent", "1"), ("sampling_order", "odd integer or blank"),
                      ("length_mm", "mm"), ("angle_deg", "deg"), ("radius_mm", "mm"),
                      ("width_mm", "mm"), ("inner_radius_mm", "mm"), ("outer_radius_mm", "mm")):
        result[key] = _field("string", unit=unit)
    return result


def _source_emission_schema() -> dict[str, Any]:
    result = {key: _field("string") for key in SOURCE_EMISSION_FIELDS}
    for key in ("kind", "boat_mode", "scan_pattern", "provenance_status"):
        result[key] = _field("string", enum=tuple(sorted(_SOURCE_EMISSION_ENUMS[key])))
    for key in ("reference_rate_nm_s", "valid_rate_min_nm_s", "valid_rate_max_nm_s"):
        result[key] = _field("string", unit="Å/s (GUI input)", stored_unit="nm/s in serialized emission_model")
    for key in ("source_exponent", "quadrature_order", "rate_exponent_per_decade",
                "active_fraction", "rate_active_fraction_per_decade", "center_weight",
                "cover_height_mm", "cover_thickness_mm",
                "spot_sigma_mm", "rate_spot_sigma_per_decade", "scan_width_mm", "scan_height_mm",
                "scan_center_x_mm", "scan_center_y_mm", "scan_angle_deg", "dwell_center_fraction"):
        unit = ("deg" if key.endswith("_deg") else "mm" if key.endswith("_mm") else
                "1/decade" if key.endswith("per_decade") else
                "fraction" if key in {"active_fraction", "dwell_center_fraction"} else
                "relative weight" if key == "center_weight" else "1")
        result[key] = _field("string", unit=unit)
    result["dwell_points"] = _field("string", unit="GUI rows: x_mm, y_mm, weight")
    return result


def _sub_field_schema() -> dict[str, Any]:
    result = {key: _field("string") for key in SUBSTRATE_FIELDS}
    result["outline_kind"] = _field("string", enum=("circle", "rectangle", "polygon", "curve"))
    result["surface_kind"] = _field("string", enum=("flat", "spherical", "asphere"))
    for key in ("radius_mm", "width_mm", "height_mm", "roc_mm"):
        result[key] = _field("string", unit="mm")
    for key in ("conic_k", "a4", "a6", "a8"):
        result[key] = _field("string", unit="surface sag coefficient")
    return result


def _materials_fields() -> frozenset[str]:
    fields = {"name", "description", "notes"}
    for prefix, keys in MATERIAL_MODEL_NUMBERS.items():
        fields.update(prefix + "_" + key for key in keys)
        fields.update(prefix + "_provenance_" + key for key in ("status", "reference", "notes"))
    return frozenset(fields)


def _materials_field_schema() -> dict[str, Any]:
    fields = {key: _field("string") for key in ("name", "description", "notes")}
    for prefix, keys in MATERIAL_MODEL_NUMBERS.items():
        for key in keys:
            unit = ("Å/s (GUI input; serialized nm/s)" if key in MATERIAL_RATE_KEYS else
                    "mm" if key.endswith("_mm") else "1/decade" if key.endswith("per_decade") else
                    "fraction" if key == "active_fraction" else
                    "relative weight" if key == "center_weight" else "1")
            fields[prefix + "_" + key] = _field("string", unit=unit)
            fields[prefix + "_" + key]["requires_model"] = prefix
        fields[prefix + "_provenance_status"] = _field(
            "string", enum=("unvalidated", "theoretical", "calibrated"))
        fields[prefix + "_provenance_reference"] = _field("string")
        fields[prefix + "_provenance_notes"] = _field("string")
        for key in ("status", "reference", "notes"):
            fields[prefix + "_provenance_" + key]["requires_model"] = prefix
    return fields


def _action(args: dict[str, Any]) -> dict[str, Any]:
    return {key: dict(value) for key, value in args.items()}


def _str_arg() -> dict[str, Any]:
    return {"type": "string", "maxLength": 20000, "nulAllowed": False,
            "utf8Encodable": True}


def _int_arg(*, minimum: int = 0) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum}


def _number_arg(*, unit: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "number", "finite": True}
    if unit is not None:
        schema["unit"] = unit
    return schema


def _point_list_arg() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "array", "minItems": 2,
            "maxItems": 2, "itemsType": "finite number"}, "minItems": 3, "maxItems": 500,
            "unit": "mm"}


def _action_schemas(module: str) -> dict[str, dict[str, Any]]:
    s = _str_arg()
    i = _int_arg()
    nmm = _number_arg(unit="mm")
    if module == "source":
        return {
            "set_emission_field": _action({"field": {"type": "string", "enum": sorted(SOURCE_EMISSION_FIELDS)},
                "value": {**s, "enumWhenField": {key: sorted(values)
                    for key, values in _SOURCE_EMISSION_ENUMS.items()},
                    "unitWhenField": {"reference_rate_nm_s": "Å/s (GUI input; serialized nm/s)",
                        "valid_rate_min_nm_s": "Å/s (GUI input; serialized nm/s)",
                        "valid_rate_max_nm_s": "Å/s (GUI input; serialized nm/s)"}}}),
            "set_aperture_field": _action({"index": {**i, "mustMatchCurrentSelection": True},
                "field": {"type": "string", "enum": sorted(APERTURE_FIELDS)},
                "value": {**s, "enumWhenField": {"kind": ["rectangle", "disk"]},
                          "unitWhenField": {"length_mm": "mm", "width_mm": "mm", "radius_mm": "mm",
                                            "offset_x_mm": "mm", "offset_y_mm": "mm", "angle_deg": "deg"}}}),
            "add_aperture": {},
            "remove_aperture": _action({"index": i}),
        }
    if module == "substrate":
        return {
            "set_point": _action({"index": {**i, "mustMatchCurrentSelection": True},
                                   "x": _number_arg(unit="mm"), "y": _number_arg(unit="mm")}),
            "add_point": _action({"index": {**i, "mustBeInsertionPositionFromCurrentSelection": True},
                                  "x": _number_arg(unit="mm"), "y": _number_arg(unit="mm")}),
            "remove_point": _action({"index": {**i, "mustMatchCurrentSelection": True}}),
        }
    if module == "machine":
        return {
            "set_source_field": _action({"index": i,
                "field": {"type": "string", "enum": ["x_mm", "y_mm", "z_mm"]}, "value": nmm}),
            "set_monitor_field": _action({"index": i,
                "field": {"type": "string", "enum": sorted(MONITOR_FIELDS)}, "value": {
                    "oneOf": [{"type": "string", "maxLength": 20000, "nulAllowed": False,
                               "whenField": ["name", "kind"]},
                              {"type": "number", "finite": True, "whenField": ["x_mm", "y_mm", "z_mm"]}],
                    "kind_enum": ["crystal", "optical"]}}),
            "add_monitor": {},
            "remove_monitor": _action({"index": i}),
        }
    if module == "mask":
        contour = {"type": "string", "enum": ["outer", "holes"]}
        return {
            "set_contour": _action({"contour": {**contour, "outerIndex": 0}, "index": i,
                                     "points": _point_list_arg()}),
            "add_hole": _action({"points": _point_list_arg()}),
            "remove_hole": _action({"index": i}),
            "rotate_contour": _action({"contour": {**contour, "outerIndex": 0}, "index": i,
                "center_x_mm": nmm, "center_y_mm": nmm, "angle_deg": _number_arg(unit="deg")}),
            "transform_mask": _action({"dx": nmm, "dy": nmm,
                "sx": _number_arg(unit="scale factor"), "sy": _number_arg(unit="scale factor")}),
        }
    if module == "process":
        env_number = {"type": "number", "finite": True, "nullable": True}
        component = {"type": "object", "additionalProperties": False, "fields": {
            "species": {"type": "string", "required": True, "maxLength": 100,
                        "nulAllowed": False, "utf8Encodable": True},
            "partial_pressure_pa": {**env_number, "unit": "Pa"},
            "effective_cross_section_m2": {**env_number, "unit": "m^2", "positiveWhenSet": True},
            "flow_sccm": {**env_number, "unit": "sccm"},
            "cross_section_source": {"type": "string", "nullable": True, "maxLength": 2000,
                                     "nulAllowed": False, "utf8Encodable": True}}}
        env_record = {"type": "object", "additionalProperties": False, "fields": {
            "transport_model": {"type": "string", "enum": ["disabled", "ballistic_loss"], "nullable": True},
            "substrate_temperature_k": {**env_number, "unit": "K", "positiveWhenSet": True},
            "source_temperature_k": {**env_number, "unit": "K", "positiveWhenSet": True},
            "gas_temperature_k": {**env_number, "unit": "K", "positiveWhenSet": True},
            "operating_pressure_pa": {**env_number, "unit": "Pa", "minimumWhenSet": 0},
            "base_pressure_pa": {**env_number, "unit": "Pa", "minimumWhenSet": 0},
            "effective_cross_section_m2": {**env_number, "unit": "m^2", "positiveWhenSet": True},
            "effective_cross_section_source": {"type": "string", "nullable": True,
                                                "maxLength": 2000, "nulAllowed": False,
                                                "utf8Encodable": True},
            "gas_components": {"type": "array", "items": component, "nullable": True, "maxItems": 32}}}
        return {
            "set_step_field": _action({"index": i,
                "field": {"type": "string", "enum": sorted(PROCESS_STEP_FIELDS)},
                "value": {**s, "enumWhenField": {"target_mode": ["mean", "center", "monitor"]},
                          "unitWhenField": {"target_nm": "nm", "rate_nm_s": "nm/s",
                                            "rate_angstrom_s": "Å/s", "monitor_slot": "1-based slot"}}}),
            "choose_step_source": _action({"index": i, "slot": _int_arg(minimum=1)}),
            "set_step_mask": _action({"index": i, "slot": i, "enabled": {"type": "boolean"}}),
            "add_step": {},
            "delete_step": _action({"index": i}),
            "move_step": _action({"index": i, "delta": {"type": "integer", "enum": [-1, 1]} }),
            "set_environment": _action({"index": {"type": "integer", "minimum": -1},
                                         "record": env_record}),
        }
    return {}


def _field_schema(module: str) -> dict[str, Any]:
    if module == "source":
        return _source_field_schema()
    if module == "substrate":
        return _sub_field_schema()
    if module == "machine":
        fields = {key: _field("number", unit="mm") for key in MACHINE_GEOMETRY_FIELDS}
        fields["planet_tilt_deg"] = _field("number", unit="deg")
        for key in ("sun_teeth", "planet_teeth"):
            fields[key] = _field("integer", unit="teeth")
        fields.update({key: _field("string") for key in MACHINE_METADATA_FIELDS})
        return fields
    if module == "mask":
        return {key: _field("string" if key != "z_mm" else "number",
                            unit="mm" if key == "z_mm" else None) for key in MASK_FIELDS}
    if module == "process":
        return {key: _field("string") for key in PROCESS_FIELDS}
    if module == "materials":
        return _materials_field_schema()
    return {}


def resource_editor_capabilities(module: str) -> dict[str, Any]:
    """Describe only bounded edits available on the exact current editor."""
    supported = module in RESOURCE_EDIT_MODULES
    schemas = _action_schemas(module) if supported else {}
    editable = _materials_fields() if module == "materials" else (
        SOURCE_FIELDS if module == "source" else SUBSTRATE_FIELDS if module == "substrate" else
        MACHINE_GEOMETRY_FIELDS | MACHINE_METADATA_FIELDS if module == "machine" else
        MASK_FIELDS if module == "mask" else PROCESS_FIELDS if module == "process" else frozenset())
    result: dict[str, Any] = {
        "editable_fields": sorted(editable),
        "actions": sorted(schemas),
        "action_arguments": {name: sorted(schema) for name, schema in schemas.items()},
        "field_schema": _field_schema(module) if supported else {},
        "action_schemas": schemas,
        "implicit_gui_effects": {
            "source": ["existing validation runs; source-preview bindings are signalled and spatial samples stay lazy"],
            "substrate": ["existing validation runs; surface and outline preview bindings are signalled"],
            "machine": ["existing validation runs; opted-in geometry preview may refresh from current dependencies"],
            "mask": ["existing edits invalidate rings, weights, point-weights and field results; height changes may refresh rings"],
            "process": ["existing recipe validation/normalization runs and updates preview bindings"],
            "materials": ["existing material-draft validation runs; reference and optical data stay read-only"],
        }.get(module, []),
        "unsupported_actions": ["save", "open", "load", "new", "replace_document", "export",
                                "import", "select_resource", "start_compute", "cancel_work"],
    }
    if module == "materials":
        result["unsupported_actions"] += ["edit_dataset", "import_optical_data", "edit_optical_set"]
    return result


def _invalid(code: str, message: str) -> NoReturn:
    raise UiControlError(code, message)


def _strict_args(arguments: Any, required: set[str]) -> dict[str, Any]:
    if not isinstance(arguments, Mapping) or set(arguments) != required:
        _invalid("invalid_arguments", "操作参数必须且只能包含声明的字段。")
    return dict(arguments)


def _index(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _invalid("invalid_arguments", "索引须为当前编辑器允许范围内的整数。")
    return value


def _finite(value: Any, *, label: str = "数值") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid("invalid_arguments", f"{label}须为有限数值。")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        _invalid("invalid_arguments", f"{label}须为有限数值。")
    if not math.isfinite(number):
        _invalid("invalid_arguments", f"{label}须为有限数值。")
    return number


def _text(value: Any, label: str = "文本", *, max_length: int = 20000) -> str:
    if not isinstance(value, str) or len(value) > max_length or "\x00" in value:
        _invalid("invalid_arguments", f"{label}须为不超过 {max_length} 字且不含 NUL 的文本。")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid("invalid_arguments", f"{label}必须可编码为 UTF-8。")
    return value


def _enum(value: str, allowed: frozenset[str] | tuple[str, ...], label: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        _invalid("invalid_arguments", f"未知{label}。")


def _single_field(fields: Any) -> tuple[str, Any]:
    if not isinstance(fields, Mapping) or len(fields) != 1:
        _invalid("invalid_arguments", "一次只能更新一个字段。")
    key, value = next(iter(fields.items()))
    if not isinstance(key, str):
        _invalid("invalid_field", "字段名须为文本。")
    return key, value


def _check_source_model(editor: Any, kind: str) -> None:
    if editor._material is not None and kind != "uniform":
        # The original setter reads the captured material after changing kind.
        # Prove this lookup succeeds before allowing that mutation.
        try:
            if not isinstance(editor._material, Mapping):
                raise ValueError("材料快照必须为对象")
            prefix = "electron_beam" if kind == "electron_beam" else "resistance"
            captured = editor._material.get(prefix + "_model")
            if not isinstance(captured, Mapping) or not isinstance(captured.get("defaults"), Mapping):
                raise ValueError("缺少 defaults 对象")
            if "source_exponent" not in captured["defaults"]:
                raise ValueError("defaults 缺少 source_exponent")
            if not isinstance(captured.get("provenance"), Mapping):
                raise ValueError("缺少 provenance 对象")
            from coat_sim.core.materials import validate_model_defaults, validate_provenance
            validate_model_defaults(captured["defaults"], kind)
            validate_provenance(captured["provenance"])
            editor._captured_material_model(kind)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            _invalid("invalid_model", f"当前材料快照不包含所需发射模型：{exc}")


def _update_source(editor: Any, key: str, value: Any) -> None:
    if key not in SOURCE_FIELDS:
        _invalid("invalid_field", "字段不在当前源编辑白名单内。")
    if key not in editor._fields:
        _invalid("invalid_field", "字段不属于当前源编辑器的可编辑内容。")
    value = _text(value)
    if key == "kind":
        _enum(value, ("point", "line", "disk", "annulus", "rectangle"), "源形状")
    elif key == "device_type":
        _enum(value, ("custom", "resistance_boat", "crucible", "electron_beam"), "蒸发设备类型")
        if value in ("resistance_boat", "electron_beam"):
            _check_source_model(editor, value)
    editor.setField(key, value)


def _update_substrate(editor: Any, key: str, value: Any) -> None:
    if key not in SUBSTRATE_FIELDS:
        _invalid("invalid_field", "字段不在当前基片编辑白名单内。")
    if key not in editor._fields:
        _invalid("invalid_field", "字段不属于当前基片编辑器的可编辑内容。")
    value = _text(value)
    if key == "outline_kind":
        _enum(value, ("circle", "rectangle", "polygon", "curve"), "基片轮廓类型")
    elif key == "surface_kind":
        _enum(value, ("flat", "spherical", "asphere"), "基片表面类型")
    editor.setField(key, value)


def _json_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        _invalid("invalid_arguments", f"数值或记录无法安全编码：{exc}")


def _machine_set_value(editor: Any, pointer: str, value: Any) -> None:
    before = editor._data
    editor.setValue(pointer, _json_value(value))
    if editor._data is before:
        _invalid("editor_rejected", "原机器编辑器未接受字段，当前修正板未改变。")


def _mask_points(value: Any) -> list[list[float | int]]:
    if not isinstance(value, list) or not 3 <= len(value) <= 500:
        _invalid("invalid_arguments", "轮廓须为 3–500 个 [X,Y] 毫米坐标。")
    points: list[list[float | int]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            _invalid("invalid_arguments", "轮廓顶点须为 [X,Y] 数组。")
        points.append([_finite(point[0], label="X 坐标"), _finite(point[1], label="Y 坐标")])
    return points


def _mask_pointer(contour: Any, index: Any, editor: Any) -> str:
    if contour not in ("outer", "holes"):
        _invalid("invalid_arguments", "轮廓须为 outer 或 holes。")
    index = _index(index)
    if contour == "outer":
        if index != 0:
            _invalid("invalid_arguments", "outer 的轮廓索引固定为 0。")
        if not isinstance(editor._data.get("outer"), list):
            _invalid("editor_rejected", "当前外轮廓不可编辑。")
        return "/outer"
    holes = editor._data.get("holes", [])
    if not isinstance(holes, list) or index >= len(holes):
        _invalid("stale_index", "指定孔洞已不存在，请重新读取当前修正板内容。")
    if not isinstance(holes[index], list):
        _invalid("invalid_arguments", "指定孔洞不是可编辑的轮廓数组。")
    return "/holes/" + str(index)


def _process_environment_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _invalid("invalid_arguments", "environment record 须为对象。")
    from coat_sim.core.process_environment import validate_environment
    allowed = {"transport_model", "substrate_temperature_k", "source_temperature_k",
               "gas_temperature_k", "operating_pressure_pa", "base_pressure_pa",
               "effective_cross_section_m2", "effective_cross_section_source", "gas_components"}
    if any(not isinstance(key, str) for key in value) or set(value) - allowed:
        _invalid("invalid_arguments", "environment record 含有不支持的字段。")
    if "transport_model" in value and value["transport_model"] is not None:
        _enum(value["transport_model"], ("disabled", "ballistic_loss"), "输运模型")
    for key in ("substrate_temperature_k", "source_temperature_k", "gas_temperature_k",
                "operating_pressure_pa", "base_pressure_pa", "effective_cross_section_m2"):
        if key in value and value[key] is not None:
            _finite(value[key], label=key)
    if "effective_cross_section_source" in value and value["effective_cross_section_source"] is not None:
        _text(value["effective_cross_section_source"], "有效截面依据", max_length=2000)
    if "gas_components" in value and value["gas_components"] is not None:
        components = value["gas_components"]
        component_fields = {"species", "partial_pressure_pa", "effective_cross_section_m2",
                            "flow_sccm", "cross_section_source"}
        if not isinstance(components, list) or len(components) > 32:
            _invalid("invalid_arguments", "gas_components 须为最多 32 项的列表。")
        for row in components:
            if not isinstance(row, Mapping) or any(not isinstance(key, str) for key in row):
                _invalid("invalid_arguments", "每个 gas_components 项须为字段受限的对象。")
            if set(row) - component_fields or "species" not in row:
                _invalid("invalid_arguments", "气体组分须包含 species，且只能包含声明字段。")
            species = _text(row["species"], "气体 species", max_length=100)
            if not 1 <= len(species.strip()) <= 100:
                _invalid("invalid_arguments", "气体 species 须为 1–100 字且不含 NUL 的文本。")
            for key in ("partial_pressure_pa", "effective_cross_section_m2", "flow_sccm"):
                if key in row and row[key] is not None:
                    _finite(row[key], label=key)
            if "cross_section_source" in row and row["cross_section_source"] is not None:
                _text(row["cross_section_source"], "截面依据", max_length=2000)
    try:
        return validate_environment(dict(value))
    except (ValueError, TypeError, OverflowError) as exc:
        _invalid("invalid_arguments", str(exc))


def apply_resource_editor_operation(module: str, editor: Any, operation: str, *,
                                    fields: dict | None = None, action: str | None = None,
                                    arguments: dict | None = None) -> None:
    """Preflight one bounded update, then call one existing bridge setter."""
    if module not in RESOURCE_EDIT_MODULES:
        _invalid("unsupported_module", "该项目资源模块没有受控编辑操作。")
    if operation == "update_window":
        if action is not None or arguments is not None:
            _invalid("invalid_arguments", "字段更新不能混入动作参数。")
        key, value = _single_field(fields)
        if module == "source":
            _update_source(editor, key, value)
            return
        if module == "substrate":
            _update_substrate(editor, key, value)
            return
        if module == "machine":
            if key in MACHINE_METADATA_FIELDS:
                value = _text(value)
                _machine_set_value(editor, "/" + key, value)
                return
            if key not in MACHINE_GEOMETRY_FIELDS:
                _invalid("invalid_field", "字段不在当前机器编辑白名单内。")
            if key in {"sun_teeth", "planet_teeth"}:
                if type(value) is not int:
                    _invalid("invalid_arguments", "齿数须为整数。")
            else:
                value = _finite(value, label=key)
            _machine_set_value(editor, "/" + key, value)
            return
        if module == "mask":
            if key not in MASK_FIELDS:
                _invalid("invalid_field", "字段不在当前修正板编辑白名单内。")
            if key == "z_mm":
                value = _finite(value, label="修正板高度")
                pointer = "/z_mm"
                _mask_set_value(editor, pointer, value)
            elif key == "name":
                value = _text(value)
                _mask_set_value(editor, "/name", value)
            else:
                value = _text(value)
                _mask_set_value(editor, "/" + key, value)
            return
        if module == "process":
            if key not in PROCESS_FIELDS:
                _invalid("invalid_field", "字段不在当前工艺编辑白名单内。")
            value = _text(value)
            if key == "name":
                editor.setField("name", value)
            else:
                editor.setMetadata(key, value)
            return
        if module == "materials":
            if key not in _materials_fields():
                _invalid("invalid_field", "字段不在当前材料记录编辑白名单内。")
            value = _text(value)
            if key.endswith("_provenance_status"):
                _enum(value, ("unvalidated", "theoretical", "calibrated"), "材料来源状态")
            if key not in editor._draft:
                _invalid("invalid_field", "字段不属于当前材料编辑器的可编辑内容。")
            prefix = "resistance" if key.startswith("resistance_") else (
                "electron_beam" if key.startswith("electron_beam_") else None)
            if prefix is not None and prefix in editor._null_models:
                _invalid("unsupported_field", "仅光学资料材料的源模型为空，该字段不可编辑。")
            if editor._loading or editor._operation_busy:
                _invalid("editor_busy", "材料记录正在加载或执行其他操作，当前字段未更新。")
            editor.setField(key, value)
            return
        _invalid("unsupported_module", "该项目资源模块没有受控编辑操作。")

    if operation != "window_action":
        _invalid("unsupported_operation", "仅支持当前编辑器字段更新和受控动作。")
    if fields is not None:
        _invalid("invalid_arguments", "窗口动作不能混入字段更新参数。")
    if not isinstance(action, str):
        _invalid("invalid_arguments", "动作名须为文本。")
    schemas = _action_schemas(module)
    if action not in schemas:
        _invalid("unsupported_action", "该动作不在当前编辑器受控动作白名单内。")
    args = _strict_args(arguments, set(schemas[action]))

    if module == "source":
        if action == "set_emission_field":
            field = _text(args["field"], "发射字段")
            value = _text(args["value"])
            if field not in SOURCE_EMISSION_FIELDS:
                _invalid("invalid_field", "发射字段不在当前源编辑白名单内。")
            if field not in editor._emission:
                _invalid("invalid_field", "发射字段不属于当前源编辑器的可编辑内容。")
            if field in _SOURCE_EMISSION_ENUMS:
                _enum(value, _SOURCE_EMISSION_ENUMS[field], "发射模型选项")
            if field == "kind":
                _check_source_model(editor, value)
            editor.setEmissionField(field, value)
            return
        if action == "set_aperture_field":
            index = _index(args["index"])
            field = _text(args["field"], "开口字段")
            value = _text(args["value"])
            if index != editor._aperture_index or index >= len(editor._apertures):
                _invalid("stale_index", "开口索引与当前选中开口不符。")
            if field not in APERTURE_FIELDS:
                _invalid("invalid_field", "开口字段不在当前编辑白名单内。")
            if not isinstance(editor._apertures[index], Mapping) or field not in editor._apertures[index]:
                _invalid("invalid_field", "开口字段不属于当前开口的可编辑内容。")
            if field == "kind":
                _enum(value, ("rectangle", "disk"), "开口形状")
            editor.setApertureField(field, value)
            return
        if action == "add_aperture":
            rows = editor._apertures
            selected = editor._aperture_index
            if (not isinstance(rows, list) or type(selected) is not int
                    or not 0 <= selected < len(rows) or not isinstance(rows[selected], Mapping)):
                _invalid("invalid_arguments", "当前选中开口结构无效。")
            if len(rows) >= 100:
                _invalid("invalid_arguments", "开口数量已达到当前编辑器上限。")
            editor.addAperture()
            return
        if action == "remove_aperture":
            index = _index(args["index"])
            rows = editor._apertures
            if (not isinstance(rows, list) or index != editor._aperture_index
                    or index >= len(rows) or not isinstance(rows[index], Mapping)):
                _invalid("stale_index", "开口索引与当前选中开口不符。")
            if len(rows) <= 1:
                _invalid("invalid_arguments", "至少保留一个开口。")
            editor.removeAperture()
            return

    if module == "substrate":
        if action in ("set_point", "add_point"):
            index = _index(args["index"])
            x, y = _finite(args["x"], label="X 坐标"), _finite(args["y"], label="Y 坐标")
            if action == "set_point":
                if index != editor._selected or not 0 <= index < len(editor._points):
                    _invalid("stale_index", "顶点索引与当前选中顶点不符。")
                if editor.updateSelectedPoint(str(x), str(y)) is not True:
                    _invalid("editor_rejected", "原基片编辑器未接受顶点更新。")
                return
            expected = editor._selected + 1 if editor._selected >= 0 else len(editor._points)
            if index != expected:
                _invalid("stale_index", "新增顶点位置须与当前选择状态一致。")
            if editor.addPoint(str(x), str(y)) is not True:
                _invalid("editor_rejected", "原基片编辑器未接受新增顶点。")
            return
        if action == "remove_point":
            index = _index(args["index"])
            if index != editor._selected or not 0 <= index < len(editor._points):
                _invalid("stale_index", "顶点索引与当前选中顶点不符。")
            editor.deleteSelectedPoint()
            return

    if module == "machine":
        if action == "set_source_field":
            index = _index(args["index"])
            field = _text(args["field"], "源位置字段")
            if field not in {"x_mm", "y_mm", "z_mm"}:
                _invalid("invalid_field", "机器源只允许编辑 x_mm、y_mm 或 z_mm。")
            value = _finite(args["value"], label=field)
            rows = editor._data.get("sources", [])
            if not isinstance(rows, list) or index >= len(rows):
                _invalid("stale_index", "指定机器源已不存在。")
            row = rows[index]
            if (not isinstance(row, Mapping) or not {"file", "x_mm", "y_mm"} <= set(row)
                    or set(row) - {"file", "x_mm", "y_mm", "z_mm"}
                    or (field != "z_mm" and field not in row)):
                _invalid("invalid_arguments", "指定机器源安装项结构无效。")
            _machine_set_value(editor, "/sources/" + str(index) + "/" + field, value)
            return
        if action == "set_monitor_field":
            index = _index(args["index"])
            field = _text(args["field"], "监测点字段")
            value = args["value"]
            if field not in MONITOR_FIELDS:
                _invalid("invalid_field", "监测点字段不在当前编辑白名单内。")
            rows = editor._data.get("monitors", [])
            if not isinstance(rows, list) or index >= len(rows):
                _invalid("stale_index", "指定监测点已不存在。")
            if not isinstance(rows[index], Mapping) or set(rows[index]) != MONITOR_FIELDS:
                _invalid("invalid_arguments", "指定监测点结构无效。")
            if field == "kind":
                value = _text(value)
                _enum(value, ("crystal", "optical"), "监测点类型")
            elif field == "name":
                value = _text(value)
            else:
                value = _finite(value, label=field)
            _machine_set_value(editor, "/monitors/" + str(index) + "/" + field, value)
            return
        if action == "add_monitor":
            rows = editor._data.get("monitors", [])
            if not isinstance(rows, list) or len(rows) >= 64:
                _invalid("invalid_arguments", "监测点数量已达到当前编辑器上限。")
            before_count = len(rows)
            editor.appendItem("/monitors", _json_value({"kind": "crystal", "name": "监测点",
                                                        "x_mm": 0, "y_mm": 0, "z_mm": 0}))
            if len(editor._data.get("monitors", [])) != before_count + 1:
                _invalid("editor_rejected", "原机器编辑器未接受新增监测点。")
            return
        if action == "remove_monitor":
            index = _index(args["index"])
            rows = editor._data.get("monitors", [])
            if not isinstance(rows, list) or index >= len(rows):
                _invalid("stale_index", "指定监测点已不存在。")
            before_count = len(rows)
            editor.removeItem("/monitors", index)
            if len(editor._data.get("monitors", [])) != before_count - 1:
                _invalid("editor_rejected", "原机器编辑器未接受删除监测点。")
            return

    if module == "mask":
        if action == "set_contour":
            pointer = _mask_pointer(args["contour"], args["index"], editor)
            points = _mask_points(args["points"])
            if pointer == "/outer":
                _mask_set_value(editor, pointer, points)
            else:
                _mask_set_value(editor, pointer, points)
            return
        if action == "add_hole":
            points = _mask_points(args["points"])
            holes = editor._data.get("holes", [])
            if not isinstance(holes, list) or len(holes) >= 30:
                _invalid("invalid_arguments", "孔洞数量已达到当前编辑器上限。")
            before_count = len(holes)
            editor.appendItem("/holes", _json_value(points))
            if len(editor._data.get("holes", [])) != before_count + 1:
                _invalid("editor_rejected", "原修正板编辑器未接受新增孔洞。")
            return
        if action == "remove_hole":
            index = _index(args["index"])
            holes = editor._data.get("holes", [])
            if not isinstance(holes, list) or index >= len(holes):
                _invalid("stale_index", "指定孔洞已不存在。")
            before_count = len(holes)
            editor.removeItem("/holes", index)
            if len(editor._data.get("holes", [])) != before_count - 1:
                _invalid("editor_rejected", "原修正板编辑器未接受删除孔洞。")
            return
        if action == "rotate_contour":
            pointer = _mask_pointer(args["contour"], args["index"], editor)
            center_x = _finite(args["center_x_mm"], label="旋转中心 X")
            center_y = _finite(args["center_y_mm"], label="旋转中心 Y")
            angle = _finite(args["angle_deg"], label="旋转角度")
            if abs(center_x) > 100000 or abs(center_y) > 100000 or abs(angle) > 360000:
                _invalid("invalid_arguments", "旋转中心或角度超出原编辑器允许范围。")
            polygon = editor._data.get("outer") if pointer == "/outer" else editor._data["holes"][int(pointer.split("/")[-1])]
            if not isinstance(polygon, list) or len(polygon) < 3:
                _invalid("invalid_arguments", "所选轮廓至少需要三个顶点。")
            _mask_points(polygon)
            before = editor._data
            editor.rotatePolygon(pointer, center_x, center_y, angle)
            if editor._data is before:
                _invalid("editor_rejected", "原修正板编辑器未接受轮廓旋转。")
            return
        if action == "transform_mask":
            dx, dy = _finite(args["dx"], label="X 平移"), _finite(args["dy"], label="Y 平移")
            sx, sy = _finite(args["sx"], label="X 缩放"), _finite(args["sy"], label="Y 缩放")
            if abs(dx) > 100000 or abs(dy) > 100000 or not 0.01 <= sx <= 100 or not 0.01 <= sy <= 100:
                _invalid("invalid_arguments", "平移或缩放超出原编辑器允许范围。")
            polygons = [editor._data.get("outer", [])] + list(editor._data.get("holes", []))
            if not any(polygons):
                _invalid("invalid_arguments", "当前修正板没有可变换的顶点。")
            for polygon in polygons:
                if polygon:
                    _mask_points(polygon)
            before = editor._data
            editor.transformMask(dx, dy, sx, sy)
            if editor._data is before:
                _invalid("editor_rejected", "原修正板编辑器未接受轮廓变换。")
            return

    if module == "process":
        if action == "set_step_field":
            index = _index(args["index"])
            field = _text(args["field"], "工艺步骤字段")
            value = _text(args["value"])
            if field not in PROCESS_STEP_FIELDS:
                _invalid("invalid_field", "工艺步骤字段不在当前编辑白名单内。")
            if not 0 <= index < len(editor._data.get("steps", [])):
                _invalid("stale_index", "指定工艺步骤已不存在。")
            if field == "target_mode":
                _enum(value, ("mean", "center", "monitor"), "膜厚控制模式")
            if field == "monitor_slot" and editor._data["steps"][index].get("target_mode") != "monitor":
                _invalid("invalid_arguments", "只有 monitor 模式的步骤可以编辑监测点位次。")
            editor.setStep(index, field, value)
            return
        if action == "choose_step_source":
            index, slot = _index(args["index"]), _index(args["slot"], minimum=1)
            if not 0 <= index < len(editor._data.get("steps", [])):
                _invalid("stale_index", "指定工艺步骤已不存在。")
            source_rows = editor._source_preview.get("sources", [])
            if not any(row.get("slot") == slot and row.get("valid") for row in source_rows):
                _invalid("invalid_arguments", "指定源位次当前没有有效的已保存源。")
            editor.chooseStepSource(index, slot)
            return
        if action == "set_step_mask":
            index, slot = _index(args["index"]), _index(args["slot"])
            enabled = args["enabled"]
            if type(enabled) is not bool:
                _invalid("invalid_arguments", "修正板开关须为布尔值。")
            if not 0 <= index < len(editor._data.get("steps", [])):
                _invalid("stale_index", "指定工艺步骤已不存在。")
            if not 0 <= slot < editor.maskCount:
                _invalid("invalid_arguments", "修正板槽位不在当前机器范围内。")
            editor.setStepMask(index, slot, enabled)
            return
        if action == "add_step":
            from coat_sim.core.process_profiles import MAX_PROCESS_STEPS
            if len(editor._data.get("steps", [])) >= MAX_PROCESS_STEPS:
                _invalid("invalid_arguments", "工艺步骤数已达到当前编辑器上限。")
            editor.addStep()
            return
        if action == "delete_step":
            index = _index(args["index"])
            if not 0 <= index < len(editor._data.get("steps", [])):
                _invalid("stale_index", "指定工艺步骤已不存在。")
            editor.deleteStep(index)
            return
        if action == "move_step":
            index, delta = _index(args["index"]), args["delta"]
            if type(delta) is not int or delta not in (-1, 1):
                _invalid("invalid_arguments", "步骤移动量须为 -1 或 1。")
            if not 0 <= index < len(editor._data.get("steps", [])) or not 0 <= index + delta < len(editor._data["steps"]):
                _invalid("invalid_arguments", "步骤不能移出当前工艺顺序。")
            editor.moveStep(index, delta)
            return
        if action == "set_environment":
            index = args["index"]
            if type(index) is not int or index < -1 or index >= len(editor._data.get("steps", [])):
                _invalid("stale_index", "工艺默认值索引须为 -1，或指向现有步骤。")
            record = _process_environment_record(args["record"])
            result = editor.applyEnvironment(index, record)
            if not isinstance(result, Mapping) or result.get("ok") is not True:
                _invalid("editor_rejected", str(result.get("message", "原工艺编辑器未接受环境参数。"))
                         if isinstance(result, Mapping) else "原工艺编辑器未接受环境参数。")
            return

    _invalid("unsupported_action", "该动作不在当前编辑器受控动作白名单内。")


def _mask_set_value(editor: Any, pointer: str, value: Any) -> None:
    before = editor._data
    editor.setValue(pointer, _json_value(value))
    if editor._data is before:
        _invalid("editor_rejected", "原修正板编辑器未接受字段，当前内容未改变。")
