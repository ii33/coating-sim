"""Finite current-input edits using the compute windows' existing GUI setters.

The caller owns project, window, snapshot, lifecycle and native-buffer fences.
This adapter never opens a controller, changes resources or starts/cancels work.
Raw scalar text stays raw: the GUI may intentionally retain an invalid draft.
"""
from __future__ import annotations

import json
import math
from typing import Any, NoReturn

from coat_sim.control_errors import UiControlError

COMPUTE_EDIT_MODULES = frozenset({"main", "ray_trace", "film_appearance"})
MAIN_INPUT_FIELDS = frozenset({"carrier_rpm", "carrier_phase_deg", "planet_phase_deg",
                             "time_steps", "time_step_s", "grid_size"})
MAIN_FIELDS = MAIN_INPUT_FIELDS | {"algorithm", "algorithm_budget_seconds", "fast_cycle",
                                 "fast_threshold_percent"}
APPEARANCE_INPUT_FIELDS = frozenset({
    "geometry_thickness_mm", "camera_x", "camera_y", "camera_z", "target_x", "target_y",
    "target_z", "up_x", "up_y", "up_z", "fov_y_deg", "area_x", "area_y", "area_z",
    "area_target_x", "area_target_y", "area_target_z", "area_up_x", "area_up_y", "area_up_z",
    "area_size_x", "area_size_y", "area_scale", "environment_scale", "width", "height",
    "spp", "seed", "wavelength_start_nm", "wavelength_end_nm", "wavelength_step_nm",
})
APPEARANCE_FLAGS = frozenset({"area_enabled", "environment_enabled",
                            "area_absolute_calibration", "environment_absolute_calibration"})
APPEARANCE_FIELDS = APPEARANCE_INPUT_FIELDS | APPEARANCE_FLAGS | {"area_kind", "environment_kind"}
RAY_ACTIONS = {
    "set_sample_field": frozenset({"sample_id", "field", "value"}),
    "set_surface_field": frozenset({"surface_id", "field", "value"}),
    "set_spectrum_field": frozenset({"index", "field", "value"}),
    "set_detector_field": frozenset({"field", "value"}),
}
APPEARANCE_ACTIONS: dict[str, frozenset[str]] = {"reset_geometry_thickness_to_inherited": frozenset()}
MAX_TEXT = 20_000
MAX_REQUEST_BYTES = 2 * 1024 * 1024


def _invalid(message: str) -> NoReturn:
    raise UiControlError("invalid_arguments", message)


def _raw(value: Any) -> str:
    if isinstance(value, str):
        if len(value) > MAX_TEXT or "\x00" in value:
            _invalid("输入文本超过保护范围或包含空字符。")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            _invalid("输入须为完整Unicode文本。")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid("此控件接受原始文本或有限数值。")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        _invalid("数值须有限；无效的中间输入请使用原始文本。")
    return str(value)


def _number(value: Any) -> int | float:
    if type(value) not in {int, float}:
        _invalid("结构数值须为有限数值。")
    try:
        if not math.isfinite(value):
            _invalid("结构数值须为有限数值。")
    except OverflowError:
        _invalid("结构数值超出有限范围。")
    return value


def _index(value: Any, length: int) -> int:
    if type(value) is not int or not 0 <= value < length:
        raise UiControlError("stale_record", "指定行已不存在，请重新读取当前窗口。")
    return value


def _identity(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        _invalid("须提供当前记录的精确身份。")
    return value


def _enum(value: Any, values: set[str]) -> str:
    if not isinstance(value, str) or value not in values:
        _invalid("不支持此控件选项。")
    return value


def _pair(value: Any) -> list:
    if not isinstance(value, list) or len(value) != 2:
        _invalid("向量须恰含两个有限数值。")
    return [_number(item) for item in value]


def compute_editor_capabilities(module: str) -> dict:
    if module not in COMPUTE_EDIT_MODULES:
        return {"editable_fields": [], "actions": [], "action_arguments": {},
                "field_schema": {}, "action_schemas": {}, "unsupported_actions": []}
    fields = MAIN_FIELDS if module == "main" else APPEARANCE_FIELDS if module == "film_appearance" else {"polarization"}
    actions = APPEARANCE_ACTIONS if module == "film_appearance" else RAY_ACTIONS if module == "ray_trace" else {}
    raw_schema = {"type": ["string", "number"], "maxLength": MAX_TEXT,
                  "semantics": "raw_gui_input_invalid_draft_preserved"}
    schema = {key: dict(raw_schema) for key in fields}
    if module == "main":
        schema.update(algorithm={"type": "string", "enum": ["fast", "reference", "optimize"]},
                      algorithm_budget_seconds={"type": "integer", "minimum": 0, "maximum": 300},
                      fast_cycle={"type": "boolean"})
        for key in MAIN_INPUT_FIELDS:
            schema[key]["unit"] = ("rpm" if key == "carrier_rpm" else "deg" if key.endswith("_deg")
                                   else "s" if key == "time_step_s" else "count")
        schema["fast_threshold_percent"]["unit"] = "percent"
        schema["algorithm_budget_seconds"]["unit"] = "s"
    elif module == "film_appearance":
        schema.update({key: {"type": "boolean"} for key in APPEARANCE_FLAGS})
        schema.update({key: {"type": "string", "enum": ["relative", "absolute"]}
                       for key in ("area_kind", "environment_kind")})
        for key in APPEARANCE_INPUT_FIELDS:
            schema[key]["unit"] = ("nm" if key.startswith("wavelength_") else "deg" if key == "fov_y_deg"
                else "count" if key in {"width", "height", "spp", "seed"}
                else "dimensionless" if key in {"area_scale", "environment_scale", "up_x", "up_y", "up_z",
                                               "area_up_x", "area_up_y", "area_up_z"} else "mm")
    else:
        schema = {"polarization": {"type": "string", "enum": ["s", "p", "unpolarized"]}}
    action_schemas: dict[str, dict[str, Any]] = {
        "set_sample_field": {"sample_id": {"type": "string", "maxLength": 128},
            "field": {"enum": ["origin_xz_mm", "direction_xz", "weight"]},
            "value": {"semantics": "finite_number_for_weight_or_two_finite_numbers"}},
        "set_surface_field": {"surface_id": {"type": "string", "maxLength": 128},
            "field": {"enum": ["geometry", "aperture_radius_mm"]},
            "value": {"semantics": "finite_number_or_exact_plane_or_sphere_geometry"}},
        "set_spectrum_field": {"index": {"type": "integer", "minimum": 0},
            "field": {"enum": ["wavelength_nm", "weight"]}, "value": {"type": "number"}},
        "set_detector_field": {"field": {"enum": ["z_mm", "aperture_radius_mm", "pixel_count"]},
            "value": {"type": "number", "semantics": "integer_for_pixel_count"}},
    }
    pair_schema = {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}}
    geometry_schema = {"oneOf": [
        {"type": "object", "additionalProperties": False, "required": ["type", "z_mm"],
         "properties": {"type": {"const": "plane"}, "z_mm": {"type": "number"}}},
        {"type": "object", "additionalProperties": False,
         "required": ["type", "center_z_mm", "radius_mm", "branch"],
         "properties": {"type": {"const": "sphere"}, "center_z_mm": {"type": "number"},
            "radius_mm": {"type": "number"}, "branch": {"enum": ["front", "back"]}}},
    ]}
    action_schemas["set_sample_field"]["value"] = {"oneOf": [{"type": "number"}, pair_schema]}
    action_schemas["set_surface_field"]["value"] = {"oneOf": [{"type": "number"}, geometry_schema]}
    complete_schemas: dict[str, Any] = {}
    for name, required in actions.items():
        properties = action_schemas.get(name, {})
        complete_schemas[name] = {"type": "object", "additionalProperties": False,
            "required": sorted(required), "properties": properties}
    if module == "ray_trace":
        for name, conditions in (
            ("set_sample_field", [("weight", {"type": "number"}), ("origin_xz_mm", pair_schema),
                                  ("direction_xz", pair_schema)]),
            ("set_surface_field", [("geometry", geometry_schema), ("aperture_radius_mm", {"type": "number"})]),
            ("set_detector_field", [("pixel_count", {"type": "integer"})]),
        ):
            complete_schemas[name]["allOf"] = [
                {"if": {"properties": {"field": {"const": field}}},
                 "then": {"properties": {"value": value_schema}}}
                for field, value_schema in conditions]
    return {"editable_fields": sorted(fields), "actions": sorted(actions),
            "action_arguments": {key: sorted(value) for key, value in actions.items()},
            "field_schema": schema,
            "action_schemas": complete_schemas,
            "implicit_gui_effects": (["input_validation", "work_and_memory_estimate"] if module == "main"
                else ["input_validation", "lightweight_scene_preview", "preview_stale"] if module == "film_appearance"
                else ["request_schema_validation", "loaded_result_invalidated"]),
            "unsupported_actions": ["save", "load", "new", "import", "export", "calculate", "start_render",
                "cancel", "select_resource", "set_compute_target", "set_backend", "set_precision",
                "replace_request", "set_limits", "generate_samples"]}


def _geometry(value: Any) -> dict:
    if not isinstance(value, dict):
        _invalid("几何须为明确的plane或sphere记录。")
    kind = _enum(value.get("type"), {"plane", "sphere"})
    expected = {"type", "z_mm"} if kind == "plane" else {"type", "center_z_mm", "radius_mm", "branch"}
    if set(value) != expected:
        _invalid("几何记录只接受当前模型的精确字段。")
    result: dict[str, Any] = {key: _number(item) for key, item in value.items() if key not in {"type", "branch"}}
    result["type"] = kind
    if kind == "sphere":
        result["branch"] = _enum(value["branch"], {"front", "back"})
    return result


def _ray_edit(controller: Any, fields: dict | None, action: str | None, args: dict) -> None:
    raw = controller.requestText
    try:
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else b""
    except UnicodeEncodeError as exc:
        raise UiControlError("editor_unavailable", "当前请求含不完整Unicode文本。") from exc
    if not isinstance(raw, str) or len(raw_bytes) > MAX_REQUEST_BYTES:
        raise UiControlError("editor_unavailable", "当前光线请求为空、无效或超过编辑保护范围。")
    def unique_object(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("重复JSON字段")
            result[key] = value
        return result
    try:
        candidate = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, TypeError, RecursionError) as exc:
        raise UiControlError("editor_unavailable", "请先在原窗口完成有效JSON结构。") from exc
    if (not isinstance(candidate, dict) or candidate.get("schema") != "coat_sim.geometric_raytrace"
            or type(candidate.get("schema_version")) is not int or candidate["schema_version"] != 1):
        _invalid("当前请求不属于受支持的光线请求模型。")
    if fields is not None:
        candidate["polarization"] = _enum(fields["polarization"], {"s", "p", "unpolarized"})
    else:
        field, value = args["field"], args["value"]
        if not isinstance(field, str):
            _invalid("字段须为已声明的文本名称。")
        if action == "set_detector_field":
            if field not in {"z_mm", "aperture_radius_mm", "pixel_count"}:
                _invalid("探测器字段未开放。")
            row = candidate.get("detector")
            if not isinstance(row, dict):
                raise UiControlError("stale_record", "当前请求没有探测器记录。")
            if field == "pixel_count" and type(value) is not int:
                _invalid("探测器像素数须为整数。")
            row[field] = _number(value)
        else:
            assert action is not None  # The public action schema was checked before parsing.
            collection = {"set_sample_field": "samples", "set_surface_field": "surfaces",
                          "set_spectrum_field": "spectrum"}[action]
            rows = candidate.get(collection)
            if not isinstance(rows, list):
                raise UiControlError("stale_record", "当前请求缺少所选集合。")
            if action == "set_spectrum_field":
                row = rows[_index(args["index"], len(rows))]
                allowed = {"wavelength_nm", "weight"}
            else:
                identity_key = "sample_id" if action == "set_sample_field" else "surface_id"
                identity = _identity(args[identity_key])
                matches = [row for row in rows if isinstance(row, dict) and row.get(identity_key) == identity]
                if len(matches) != 1:
                    raise UiControlError("stale_record", "记录身份不存在或不唯一。")
                row = matches[0]
                allowed = {"origin_xz_mm", "direction_xz", "weight"} if action == "set_sample_field" else {"geometry", "aperture_radius_mm"}
            if field not in allowed or not isinstance(row, dict):
                _invalid("此记录字段未开放。")
            row[field] = (_pair(value) if field in {"origin_xz_mm", "direction_xz"}
                          else _geometry(value) if field == "geometry" else _number(value))
    try:
        text = json.dumps(candidate, ensure_ascii=False, allow_nan=False, indent=2)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise UiControlError("invalid_arguments", "当前请求含不能安全编辑的非JSON数值。") from exc
    if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
        _invalid("编辑后的请求超过保护范围。")
    controller.setRequestText(text)
    if controller.requestText != text:
        raise UiControlError("editor_rejected", "原光线窗口未接受请求修改。")


def apply_compute_editor_operation(module: str, controller: Any, operation: str, *,
                                   fields: dict | None = None, action: str | None = None,
                                   arguments: dict | None = None) -> None:
    if module not in COMPUTE_EDIT_MODULES:
        raise UiControlError("unsupported_operation", "此窗口没有计算输入编辑接口。")
    capabilities = compute_editor_capabilities(module)
    args = arguments if arguments is not None else {}
    if operation == "update_window":
        if not isinstance(fields, dict) or len(fields) != 1:
            _invalid("每次更新须明确一个字段。")
        key, value = next(iter(fields.items()))
        if key not in capabilities["editable_fields"]:
            raise UiControlError("invalid_field", "字段不在当前窗口编辑白名单内。")
    elif operation == "window_action":
        if not isinstance(action, str) or action not in capabilities["action_arguments"]:
            raise UiControlError("unsupported_action", "此动作尚未开放。")
        if not isinstance(args, dict) or set(args) != set(capabilities["action_arguments"][action]):
            _invalid("动作须提供能力矩阵列出的精确参数。")
    else:
        raise UiControlError("unsupported_operation", "此入口只接受受控字段或窗口动作。")
    if module == "ray_trace":
        _ray_edit(controller, fields if operation == "update_window" else None, action, args)
    elif module == "main":
        if getattr(controller, "_result_view_only", False):
            raise UiControlError("read_only_window", "结果视图不能编辑计算输入。")
        if key in MAIN_INPUT_FIELDS:
            controller.setField(key, _raw(value))
        elif key == "algorithm":
            controller.setAlgorithm(_enum(value, {"fast", "reference", "optimize"}))
        elif key == "algorithm_budget_seconds":
            if type(value) is not int or not 0 <= value <= 300:
                _invalid("算法预算须为0至300秒整数。")
            controller.setAlgorithmBudgetSeconds(value)
        elif key == "fast_cycle":
            if type(value) is not bool:
                _invalid("快速周期开关须为布尔值。")
            controller.setFastCycle(value)
        else:
            controller.setFastThreshold(_raw(value))
    elif operation == "window_action":
        if getattr(controller, "_inherited_geometry_thickness_mm", None) is None:
            _invalid("当前来源没有可恢复的继承几何厚度。")
        controller.resetGeometryThicknessToInherited()
    elif key in APPEARANCE_INPUT_FIELDS:
        controller.setField(key, _raw(value))
    elif key in APPEARANCE_FLAGS:
        if type(value) is not bool:
            _invalid("外观开关须为布尔值。")
        controller.setFlag(key, value)
    else:
        kind = _enum(value, {"relative", "absolute"})
        controller.setSpectrumKind("area" if key == "area_kind" else "environment", kind)


def compute_editor_validation(module: str, controller: Any) -> dict:
    """Read original validation after a write; never used by the pure reader."""
    if module == "film_appearance":
        message = controller._validate_draft()
        return {"valid": not bool(message), "message": message, "scope": "original_appearance_draft"}
    if module == "ray_trace":
        message = controller.validationMessage
        return {"valid": not bool(message), "message": message,
                "scope": "original_request_schema_and_asset_binding",
                "physical_validation": "at_explicit_compute_admission"}
    try:
        controller._settings()
        if controller._time_basis == "time_step_s":
            step = float(controller._requested_time_step_s)
            if not math.isfinite(step) or step <= 0:
                raise ValueError("时间步长须为有限正数。")
        if controller._fast_cycle and controller._fast_threshold_invalid:
            raise ValueError("残余时长阈值须为0至100%。")
    except (ValueError, TypeError, OverflowError) as exc:
        return {"valid": False, "message": str(exc), "scope": "original_main_input_fields"}
    return {"valid": True, "message": "", "scope": "original_main_input_fields"}
