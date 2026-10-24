"""Narrow, optimistic-concurrency gateway for existing project editor actions.

The authenticated application router owns the six-field target check. This
adapter additionally binds every write to an existing window instance and the
read-only editor snapshot. It never creates an editor or exposes method names.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import sys
from typing import Any, NoReturn
from uuid import uuid4

from coat_sim.control_errors import UiControlError
from coat_sim.core.film_design import MAX_LAYERS, validate_design
from coat_sim.project_activation import canonical_path
from coat_sim.project_session import ProjectSessionError

DESIGN_FIELDS = frozenset({"name", "summary", "note", "reference_wavelength_nm",
                           "assume_lossless", "use_k", "first_surface"})
OPTIMIZATION_FIELDS = frozenset({"search_mode", "structure_mode", "solver",
                                 "acceptable_error_percent"})
SIMULATION_FIELDS = frozenset({"wavelength_min_nm", "wavelength_max_nm", "samples", "step_nm",
    "angle_deg", "polarization", "first_surface", "extrapolation_policy"})
LAYER_FIELDS = frozenset({"thickness.value", "thickness.unit", "note"})
SOLVER_OPTIONS = frozenset({"max_iterations", "max_evaluations", "seed",
                            "validation_samples_multiplier", "multistart_count", "population_size"})
STRUCTURE_OPTIONS = frozenset({"max_total_layers", "max_added_layers", "position_samples",
    "candidate_budget", "needle_thickness_samples", "growth_cycles", "seed",
    "max_total_thickness_nm", "min_thickness_nm", "max_thickness_nm",
    "growth_steps_nm", "growth_phase_fractions"})
DESIGN_ACTIONS = {
    "save": frozenset(), "add_layer": frozenset({"side"}),
    "insert_layer_after": frozenset({"side", "layer_id"}),
    "delete_layer": frozenset({"side", "layer_id"}),
    "move_layer": frozenset({"side", "layer_id", "delta"}),
    "set_layer_value": frozenset({"side", "layer_id", "field", "value"}),
}
OPTIMIZATION_ACTIONS = {
    "save": frozenset(), "set_solver_option": frozenset({"name", "value"}),
    "set_structure_option": frozenset({"name", "value"}),
    "set_structure_material_selected": frozenset({"material_id", "optical_set_id", "selected"}),
    "set_objective": frozenset({"objective_id", "record"}),
    "add_objective": frozenset({"record"}),
    "duplicate_objective": frozenset({"objective_id"}),
    "remove_objective": frozenset({"objective_id"}),
    "set_variable": frozenset({"side", "layer_id", "enabled", "lower_nm", "upper_nm"}),
}
OBJECTIVE_FIELDS = frozenset({"name", "wavelength_min_nm", "wavelength_max_nm", "wavelength_samples",
    "sampling_basis", "wavelength_step_nm", "angle_sampling_mode", "angle_min_deg", "angle_max_deg",
    "angle_step_deg", "angles_text", "polarization", "metric", "mean", "pointwise", "weight"})
SIMULATION_ACTIONS: dict[str, frozenset[str]] = {"save": frozenset()}
FILM_MODULES = frozenset({"film_design", "film_optimization", "film_simulation"})
RESOURCE_EDIT_MODULES = frozenset({"source", "substrate", "machine", "mask", "process", "materials"})
COMPUTE_EDIT_MODULES = frozenset({"main", "ray_trace", "film_appearance"})
SUPPORTED_MODULES = FILM_MODULES | RESOURCE_EDIT_MODULES | COMPUTE_EDIT_MODULES


def project_editor_capabilities(module: str) -> dict[str, Any]:
    """Truthful per-module matrix; navigation remains the router's contract."""
    if module in RESOURCE_EDIT_MODULES | COMPUTE_EDIT_MODULES:
        if module in RESOURCE_EDIT_MODULES:
            from .project_resource_editor_operations import resource_editor_capabilities
            declared = resource_editor_capabilities(module)
            # The resource adapter keeps property maps for its strict local
            # preflight. Publish complete schemas consistently to all clients.
            declared["action_schemas"] = {
                name: {"type": "object", "additionalProperties": False,
                       "required": declared["action_arguments"][name], "properties": schema}
                for name, schema in declared["action_schemas"].items()}
        else:
            from .project_compute_editor_operations import compute_editor_capabilities
            declared = compute_editor_capabilities(module)
        return declared | {
            "editor_operations": ["update_window", "window_action"] if declared.get("actions") else ["update_window"],
            "requires": ["exact_project_target", "instance_id", "expected_snapshot_id"],
            "single_field_update": True, "force_bypasses_review": False,
            "compute_start": "estimate_and_confirm_only", "cancel": "existing_job_contract",
            "scope": "existing_current_editor", "automatic_save": False,
            "invalid_draft": "original_gui_semantics", "undo": "original_editor_only"}
    supported = module in SUPPORTED_MODULES
    actions = (DESIGN_ACTIONS if module == "film_design" else OPTIMIZATION_ACTIONS
               if module == "film_optimization" else SIMULATION_ACTIONS if supported else {})
    return {"editor_operations": ["update_window", "window_action"] if supported else [],
            "editable_fields": sorted(DESIGN_FIELDS if module == "film_design" else
                                      OPTIMIZATION_FIELDS if module == "film_optimization" else
                                      SIMULATION_FIELDS if supported else ()),
            "actions": sorted(actions),
            "action_arguments": {key: sorted(value) for key, value in actions.items()},
            "layer_fields": sorted(LAYER_FIELDS) if module == "film_design" else [],
            "solver_options": sorted(SOLVER_OPTIONS) if module == "film_optimization" else [],
            "structure_options": sorted(STRUCTURE_OPTIONS) if module == "film_optimization" else [],
            "objective_record_fields": sorted(OBJECTIVE_FIELDS) if module == "film_optimization" else [],
            "variable_record_fields": ["side", "layer_id", "enabled", "lower_nm", "upper_nm"]
                if module == "film_optimization" else [],
            "requires": ["exact_project_target", "instance_id", "expected_snapshot_id"],
            "single_field_update": True, "force_bypasses_review": False,
            "compute_start": "estimate_and_confirm_only", "cancel": "existing_job_contract",
            "unsupported_actions": ["calculate", "start_optimization", "apply_candidate",
                                    "new", "load", "replace_document", "export"],
            "scope": "existing_current_editor" if supported else "read_only_window"}


def _invalid(message: str) -> NoReturn:
    raise UiControlError("invalid_arguments", message)


def _finite(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        _invalid("数值参数须为有限数值。")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        _invalid("数值参数须为有限数值。")
    if not math.isfinite(result) or positive and result <= 0:
        _invalid("数值参数须为有限正数。" if positive else "数值参数须为有限数值。")
    return result


def _integer(value: Any, *, low: int = 1, high: int = 2 ** 53 - 1) -> int:
    if type(value) is not int or not low <= value <= high:
        _invalid("整数参数超出当前控件允许范围。")
    return value


def _ensure_accepted(accepted: Any, controller: Any) -> None:
    if accepted is not True:
        raise UiControlError("editor_rejected", str(getattr(controller, "errorText", None)
            or getattr(controller, "error", None) or "原编辑器未接受操作，未报告成功。"))


def _design_update(controller: Any, fields: dict, editor: Any) -> None:
    key, value = next(iter(fields.items()))
    if key not in DESIGN_FIELDS:
        raise UiControlError("invalid_field", "字段不在当前工程膜系编辑白名单内。")
    if key in {"name", "summary", "note"} and not isinstance(value, str):
        _invalid("膜系文本字段须为文本。")
    if key in {"assume_lossless", "use_k"} and type(value) is not bool:
        _invalid("膜系开关须为布尔值。")
    if key == "reference_wavelength_nm":
        _finite(value, positive=True)
    candidate = deepcopy(dict(editor.snapshot().design))
    candidate[key] = value
    # Pure normalization before the real UI action: invalid input cannot partly
    # mutate the draft or dirty/revision counters.
    try:
        validate_design(candidate)
    except (ValueError, TypeError, OverflowError) as exc:
        raise UiControlError("invalid_field", str(exc)) from exc
    _ensure_accepted(controller.setDesignValue(key, value), controller)


def _optimization_update(controller: Any, fields: dict) -> None:
    key, value = next(iter(fields.items()))
    if key not in OPTIMIZATION_FIELDS:
        raise UiControlError("invalid_field", "字段不在当前工程优化编辑白名单内。")
    if key == "search_mode":
        if not isinstance(value, str) or value not in {"fixed_topology", "structure"}:
            _invalid("未知优化模式。")
        accepted = controller.setSearchMode(value)
    elif key == "structure_mode":
        if not isinstance(value, str) or value not in {"combined", "needle_only", "growth_only"}:
            _invalid("未知结构搜索模式。")
        accepted = controller.setStructureMode(value)
    elif key == "solver":
        if not isinstance(value, str) or value not in {row["id"] for row in controller.solverOptions}:
            _invalid("未知优化方法。")
        if value == "differential_evolution" and controller.exportOptimizationConfiguration()["solver"]["population_size"] < 5:
            _invalid("差分进化群体数至少为5，请先更新群体数。")
        accepted = controller.setSolver(value)
    else:
        number = _finite(value)
        if not 0 <= number <= 100:
            _invalid("误差容限须为0至100个百分点。")
        accepted = controller.setAcceptableErrorPercent(number)
    _ensure_accepted(accepted, controller)


def _simulation_update(controller: Any, fields: dict) -> None:
    from .film_simulation_quick_controller import _count_for_step
    key, value = next(iter(fields.items()))
    if key not in SIMULATION_FIELDS:
        raise UiControlError("invalid_field", "字段不在当前光谱仿真参数白名单内。")
    current = controller.settings
    expected_samples = None
    try:
        if key in {"wavelength_min_nm", "wavelength_max_nm", "step_nm"}:
            number = _finite(value, positive=True)
            prospective = current | {key: number}
            if prospective["wavelength_max_nm"] <= prospective["wavelength_min_nm"]:
                _invalid("光谱仿真波长上限必须大于下限。")
            expected_samples = _count_for_step(
                prospective["wavelength_max_nm"] - prospective["wavelength_min_nm"], prospective["step_nm"])
        elif key == "samples":
            expected_samples = _integer(value, low=2, high=sys.maxsize)
        elif key == "angle_deg":
            if not 0 <= _finite(value) < 90:
                _invalid("入射角须为[0,90)度。")
        else:
            allowed = {"polarization": {"s", "p", "unpolarized"},
                       "first_surface": {"front", "back"},
                       "extrapolation_policy": {"endpoint_hold", "error"}}[key]
            if not isinstance(value, str) or value not in allowed:
                _invalid("未知光谱仿真选项。")
    except (ValueError, TypeError, OverflowError) as exc:
        if isinstance(exc, UiControlError):
            raise
        raise UiControlError("invalid_field", str(exc)) from exc
    controller.setParameter(key, value)
    updated = controller.settings
    if (expected_samples is not None and updated["samples"] != expected_samples
            or key != "step_nm" and updated[key] != value):
        raise UiControlError("editor_rejected", "原光谱仿真控制器未接受参数，未报告成功。")


def _design_action(controller: Any, editor: Any, name: str, args: dict) -> None:
    if name == "save":
        _ensure_accepted(controller.saveCurrent(), controller)
        return
    side = args["side"]
    if not isinstance(side, str) or side not in {"front", "back"}:
        _invalid("膜面须为front或back。")
    design = editor.snapshot().design
    rows = design[side + "_layers"]
    identity = args.get("layer_id")
    row = next((row for row in rows if row["id"] == identity), None)
    if name != "add_layer" and (not isinstance(identity, str) or row is None):
        raise UiControlError("stale_layer", "指定膜层已不存在，请重新读取当前设计。")
    if name in {"add_layer", "insert_layer_after"} and (
            len(design["front_layers"]) + len(design["back_layers"]) >= MAX_LAYERS):
        _invalid("膜层数量已达到当前文档上限。")
    if name == "add_layer":
        accepted = controller.addLayer(side)
    elif name == "insert_layer_after":
        accepted = controller.insertLayerAfter(side, identity)
    elif name == "delete_layer":
        accepted = controller.deleteLayer(side, identity)
    elif name == "move_layer":
        delta = args["delta"]
        if type(delta) is not int or delta not in {-1, 1}:
            _invalid("膜层移动步长须为-1或1。")
        index = next(index for index, item in enumerate(rows) if item["id"] == identity)
        if not 0 <= index + delta < len(rows):
            _invalid("膜层不能移出当前膜面。")
        accepted = controller.moveLayer(side, identity, delta)
    else:
        field, value = args["field"], args["value"]
        if not isinstance(field, str) or field not in LAYER_FIELDS:
            raise UiControlError("invalid_field", "膜层字段不在受控编辑白名单内。")
        candidate = deepcopy(dict(design))
        target = next(item for item in candidate[side + "_layers"] if item["id"] == identity)
        if field == "thickness.value":
            _finite(value, positive=True)
            target["thickness"]["value"] = value
        elif field == "thickness.unit":
            target["thickness"]["unit"] = value
        else:
            target["note"] = value
        try:
            validate_design(candidate)
        except (ValueError, TypeError, OverflowError) as exc:
            raise UiControlError("invalid_field", str(exc)) from exc
        accepted = controller.setLayerValue(side, identity, field, value)
    _ensure_accepted(accepted, controller)


def _optimization_action(controller: Any, editor: Any, name: str, args: dict) -> None:
    if name == "save":
        _ensure_accepted(editor.save_current(), controller)
    elif name in {"set_objective", "add_objective", "duplicate_objective", "remove_objective", "set_variable"}:
        _optimization_record_action(controller, editor, name, args)
    elif name == "set_solver_option":
        option, value = args["name"], args["value"]
        if not isinstance(option, str) or option not in SOLVER_OPTIONS:
            raise UiControlError("invalid_field", "未知优化器参数。")
        _integer(value, low=0 if option == "seed" else 1,
                 high=0xFFFFFFFF if option == "seed" else 2 ** 53 - 1)
        if (option == "population_size" and value < 5
                and controller.exportOptimizationConfiguration()["solver"]["method"] == "differential_evolution"):
            _invalid("差分进化群体数至少为5。")
        _ensure_accepted(controller.setSolverOption(option, value), controller)
    elif name == "set_structure_option":
        option, value = args["name"], args["value"]
        if not isinstance(option, str) or option not in STRUCTURE_OPTIONS:
            raise UiControlError("invalid_field", "未知结构搜索参数。")
        if option in {"growth_steps_nm", "growth_phase_fractions"}:
            if not isinstance(value, list) or not value:
                _invalid("增长序列须为非空有限正数数组。")
            values = [_finite(item, positive=True) for item in value]
            if len(set(values)) != len(values) or any(item > (
                    2 if option == "growth_phase_fractions" else 1e6) for item in values):
                _invalid("增长序列重复或超出当前控件范围。")
            value = ",".join(format(item, ".17g") for item in values)
        elif option in {"max_total_thickness_nm", "min_thickness_nm", "max_thickness_nm"}:
            number = _finite(value, positive=True)
            current = controller.structureOptions
            if (number > 1e9 or option == "min_thickness_nm" and number >= current["max_thickness_nm"]
                    or option == "max_thickness_nm" and number <= current["min_thickness_nm"]):
                _invalid("结构厚度边界无效。")
        else:
            _integer(value, low=0 if option == "seed" else 1,
                high={"max_total_layers": 4096, "max_added_layers": 1, "growth_cycles": 1,
                      "seed": 0xFFFFFFFF}.get(option, 2 ** 53 - 1))
        _ensure_accepted(controller.setStructureOption(option, value), controller)
    else:
        material, optical, selected = args["material_id"], args["optical_set_id"], args["selected"]
        if not isinstance(material, str) or not isinstance(optical, str) or type(selected) is not bool:
            _invalid("候选材料须有精确材料/数据集身份和布尔选中状态。")
        if not any((row["material_id"], row["optical_set_id"]) == (material, optical)
                   for row in controller.structureMaterialOptions):
            raise UiControlError("stale_material", "候选材料已不在当前目录。")
        _ensure_accepted(controller.setStructureMaterialSelected(material, optical, selected), controller)


def _validate_objective_constraint_records(candidate: dict) -> None:
    """Preflight detached native groups before the controller's atomic load.

    Failed public loads retain a diagnostic that fences editor snapshots. Reject
    malformed records here so an invalid gateway call preserves readability too.
    The controller remains the owner of active-criterion validation and storage.
    """
    from .film_optimization_quick_controller import (
        _constraint_editor_default, _validate_editor_criteria)

    checked = deepcopy(candidate)
    required = set(_constraint_editor_default(True))
    for group in ("mean", "pointwise"):
        constraint = checked[group]
        if (not isinstance(constraint, dict) or not required <= set(constraint)
                or set(constraint) - required - {"bounds_drafts"}):
            _invalid("目标约束必须为完整的当前版本组；不支持组内局部合并。")
        if any(type(constraint[field]) is not bool for field in ("enabled", "strict_lower", "strict_upper")):
            _invalid("目标约束开关必须为布尔值。")
        if constraint["mode"] not in {"value", "range"}:
            _invalid("目标约束模式必须为value或range。")
        for field in ("lower_bound_percent", "upper_bound_percent", "target_value_percent"):
            value = constraint[field]
            if field == "target_value_percent" and value is None:
                continue
            if not 0 <= _finite(value) <= 100:
                _invalid("目标R/T百分比须为0到100。")
        if constraint["mode"] == "value":
            if constraint["target_value_percent"] is None:
                _invalid("精确值模式需要目标值。")
            # Mirror the native editor's exact-value representation for the
            # existing criterion validator; keep the caller's record untouched.
            constraint["lower_bound_percent"] = constraint["upper_bound_percent"] = constraint["target_value_percent"]
        drafts = constraint.get("bounds_drafts", {})
        if (not isinstance(drafts, dict) or set(drafts) - {"lower_bound_percent", "upper_bound_percent"}
                or any(not isinstance(value, str) for value in drafts.values())
                or constraint["enabled"] and drafts):
            _invalid("启用的约束不能含未提交草稿；关闭组只接受当前上下限文本草稿。")
    _validate_editor_criteria(checked)


def _optimization_record_action(controller: Any, editor: Any, name: str, args: dict) -> None:
    """Merge only an explicit record, validate once, and use the atomic loader.

    Sampling normalization is the controller's existing implementation. No grid
    is materialized and no optimizer/queue operation is admitted here.
    """
    from .film_optimization_quick_controller import (
        _objective_default, _resolve_objective_sampling, _resolve_angle_sampling)
    configuration = controller.exportOptimizationConfiguration()
    if name == "set_variable":
        side, layer_id, enabled = args["side"], args["layer_id"], args["enabled"]
        if not isinstance(side, str) or side not in {"front", "back"} or not isinstance(layer_id, str):
            _invalid("厚度变量须有精确膜面和膜层身份。")
        if type(enabled) is not bool:
            _invalid("变量enabled须为布尔值。")
        lower, upper = _finite(args["lower_nm"]), _finite(args["upper_nm"])
        if enabled and not 0 < lower < upper:
            _invalid("启用变量的厚度界须为正且严格递增。")
        current = next((row for row in controller.variables
                        if (row["side"], row["layer_id"]) == (side, layer_id)), None)
        if current is None or enabled and not current["available"]:
            raise UiControlError("stale_layer", "变量膜层不存在或当前不可优化。")
        row = next(row for row in configuration["variables"]
                   if (row["side"], row["layer_id"]) == (side, layer_id))
        row.update(enabled=enabled, lower_nm=lower, upper_nm=upper)
    else:
        rows = configuration["objectives"]
        identity = args.get("objective_id")
        row = next((row for row in rows if row["id"] == identity), None)
        if name != "add_objective" and (not isinstance(identity, str) or row is None):
            raise UiControlError("stale_objective", "目标已不存在，请重新读取当前优化设置。")
        if name == "remove_objective":
            rows.remove(row)
        elif name == "duplicate_objective":
            assert row is not None  # The non-add action identity check above established this.
            copied = deepcopy(row)
            copied.update(id=uuid4().hex, name=str(row["name"]) + " 副本")
            rows.insert(rows.index(row) + 1, copied)
        else:
            record = args["record"]
            if not isinstance(record, dict) or set(record) - OBJECTIVE_FIELDS:
                _invalid("目标record只接受已公布字段，不能替换身份或派生采样值。")
            if name == "set_objective" and not record:
                _invalid("目标record须包含明确修改值。")
            candidate = deepcopy(row) if row is not None else _objective_default(len(rows))
            candidate.update(deepcopy(record))
            try:
                for field in {"name", "angles_text", "sampling_basis", "angle_sampling_mode", "polarization", "metric"}:
                    if not isinstance(candidate[field], str):
                        _invalid("目标文本与选项字段须为文本。")
                if len(candidate["name"]) > 20000 or len(candidate["angles_text"]) > 20000:
                    _invalid("目标文本超出当前输入保护范围。")
                for field, allowed in (("polarization", {"s", "p", "unpolarized"}),
                                       ("metric", {"R", "T"})):
                    if candidate[field] not in allowed:
                        _invalid("未知目标偏振、指标或目标类型。")
                _finite(candidate["weight"], positive=True)
                _validate_objective_constraint_records(candidate)
                edited_field = None
                if "sampling_basis" not in record:
                    sampling_fields = {"wavelength_samples", "wavelength_step_nm"} & set(record)
                    if len(sampling_fields) > 1:
                        _invalid("同时提供波长点数与步长时须明确sampling_basis。")
                    edited_field = next(iter(sampling_fields), None)
                candidate = _resolve_angle_sampling(_resolve_objective_sampling(candidate, edited_field=edited_field))
            except (ValueError, TypeError, OverflowError) as exc:
                if isinstance(exc, UiControlError):
                    raise
                raise UiControlError("invalid_field", str(exc)) from exc
            if row is None:
                rows.append(candidate)
            else:
                rows[rows.index(row)] = candidate
    _ensure_accepted(controller.loadOptimizationConfiguration(configuration), controller)
    # The existing loader intentionally suppresses persistence during restore.
    # A user-requested record edit publishes that validated configuration to the
    # existing draft through its public workflow setter, retaining dirty/save.
    _ensure_accepted(editor.set_workflow_configuration("optimization", controller.exportOptimizationConfiguration()), controller)


def _write_editor_fence(window: Any, controller: Any, owner: Any, module: str) -> None:
    """Write eligibility is stricter than a truthful read-only projection.

    In particular a view-only/reference modal can remain readable while the
    original editor behind it must not be changed. Do not make the reader
    reject those legitimate views just to satisfy this write boundary.
    """
    from PySide6.QtCore import QObject
    for candidate in (controller, owner):
        if (getattr(candidate, "readOnly", False) or getattr(candidate, "_result_view_only", False)
                or getattr(candidate, "_read_only", False)):
            raise UiControlError("read_only_window", "当前窗口或编辑器处于只读状态。")
        if (getattr(candidate, "_editing_frozen", False)
                or getattr(candidate, "editingFrozen", False)
                or getattr(candidate, "projectEditingFrozen", False)):
            raise UiControlError("project_frozen", "当前编辑器已冻结，不能修改输入。")
        if any(bool(getattr(candidate, name, False)) for name in (
                "busy", "running", "_busy", "_loading", "_operation_busy",
                "_opening", "_transfer_busy", "_resource_busy", "_radial_busy",
                "_field_busy", "_point_busy", "_cad_busy")):
            raise UiControlError("window_busy", "当前窗口正在处理任务，不能修改输入。")
        if module in {"mask", "process"}:
            optimization = getattr(candidate, "optimization", None)
            if isinstance(optimization, dict) and optimization.get("busy") is True:
                raise UiControlError("window_busy", "当前优化任务尚未完成，不能修改输入。")
        if any(bool(getattr(candidate, name, False)) for name in (
                "_pending_close", "_close_after_finish", "_close_after_queue", "_close_after_appearance",
                "_close_after_save", "_managed_close_save_id", "_shutting_down", "_detached", "_disposed")):
            raise UiControlError("pending_confirmation", "当前窗口正在关闭或等待原操作完成。")
        # SubstrateBridge._closed describes its polygon, not window lifetime.
        if module in COMPUTE_EDIT_MODULES and getattr(candidate, "_closed", False):
            raise UiControlError("stale_window", "当前计算输入窗口已经关闭。")
        service = (getattr(candidate, "_service", None) if module in {"machine", "mask"}
                   else getattr(candidate, "project_service", None) if module == "materials"
                   else getattr(candidate, "service", None))
        if bool(getattr(getattr(service, "_gate", None), "frozen", False)):
            raise UiControlError("project_frozen", "当前资源服务已冻结。")
        if bool(getattr(getattr(candidate, "_project_compute", None), "editing_frozen", False)):
            raise UiControlError("project_frozen", "当前计算输入已冻结。")
    if module in {"source", "substrate"}:
        nested = getattr(owner, "_material_library", None)
        if (getattr(nested, "_editing_frozen", False) or getattr(nested, "loading", False)
                or getattr(nested, "operationBusy", False)):
            raise UiControlError("window_busy", "原材料编辑器正在处理任务。")
    if module == "substrate" and getattr(owner, "_drag_index", -1) != -1:
        raise UiControlError("native_geometry_draft", "请先结束原基片窗口的几何拖拽。")
    finder = getattr(window, "findChildren", None)
    if not callable(finder):
        raise UiControlError("editor_unavailable", "无法核实原窗口的模态编辑状态。")
    try:
        primary = window.property("editorInstance") if module == "materials" else None
        for dialog in finder(QObject):
            if dialog is primary:
                continue  # The materials module's own existing editor is a Popup.
            if dialog.property("modal") is True and bool(dialog.property("visible")):
                raise UiControlError("pending_confirmation", "请先返回原窗口，再修改其内容。")
    except (RuntimeError, TypeError) as exc:
        if isinstance(exc, UiControlError):
            raise
        raise UiControlError("editor_unavailable", "无法核实原窗口的模态编辑状态。") from exc


def _registered_editor_operation(launcher: Any, session: Any, module: str, operation: str,
                                 arguments: dict, *, client_generation: str, bridge: Any) -> dict:
    """Edit an already registered resource/compute owner in one GUI turn.

    The snapshot wrapper is the common exact-owner and pending/native fence.
    There is no event processing or lazy owner lookup between that check and
    the literal bridge setter. The outer application router owns write-mode
    authorization independently from this owner/snapshot validation.
    """
    from .project_editor_snapshot import project_editor_snapshot
    expected, instance = arguments["expected_snapshot_id"], arguments["instance_id"]
    before = project_editor_snapshot(launcher, session, module,
        {"detail": "summary", "limit": 1, "expected_snapshot_id": expected},
        client_generation=client_generation, bridge=bridge)
    if not before.get("available"):
        raise UiControlError("editor_unavailable", "当前内容暂不可操作：" + str(before.get("reason")))
    if before.get("instance_id") != instance:
        raise UiControlError("stale_window", "快照不属于此窗口实例。")
    if module == "materials":
        from .project_asset_materials import existing_material_view
        route, window, controller = existing_material_view(launcher.host, session.binding)
        if route is None or route.route_id != instance:
            raise UiControlError("stale_window", "窗口实例已关闭或替换。")
    else:
        routes = [route for route in launcher.host.routes(session.binding.project_id) if route.module_key == module]
        if len(routes) != 1 or routes[0].route_id != instance:
            raise UiControlError("stale_window", "窗口实例已关闭或替换。")
        window = launcher.host.window_for(routes[0])
        controller = getattr(window, "_project_film_controller", None) if window is not None else None
    if controller is None:
        raise UiControlError("editor_owner_mismatch", "窗口当前没有已注册控制器。")
    fields = arguments.get("fields") if operation == "update_window" else None
    action = arguments.get("action") if operation == "window_action" else None
    args = arguments.get("arguments", {}) if operation == "window_action" else None
    dirty: bool | None
    if module in RESOURCE_EDIT_MODULES:
        from .project_resource_editor_operations import apply_resource_editor_operation
        editor_bridge = getattr(controller, "bridge", None)
        if editor_bridge is None:
            raise UiControlError("editor_owner_mismatch", "资源窗口没有当前编辑桥。")
        _write_editor_fence(window, controller, editor_bridge, module)
        apply_resource_editor_operation(module, editor_bridge, operation,
            fields=fields, action=action, arguments=args)
        if module in {"machine", "mask"}:
            validation = {"valid": bool(editor_bridge.canSave), "message": editor_bridge.validationMessage}
        elif module == "process":
            validation = {"valid": bool(editor_bridge.valid), "errors": list(editor_bridge.errors)}
        else:
            validation = {"valid": bool(editor_bridge.valid), "message": editor_bridge.error}
        dirty = bool(editor_bridge.dirty)
    else:
        from .project_compute_editor_operations import apply_compute_editor_operation, compute_editor_validation
        # Appearance may be presented by an existing optical workspace. Never
        # call openAppearance/refreshProjectAppearanceContext to obtain it.
        owner = (getattr(controller, "_appearance", None) if module == "film_appearance"
                 and not isinstance(getattr(controller, "_draft", None), dict) else controller)
        if owner is None:
            raise UiControlError("editor_unavailable", "当前外观输入尚未初始化。")
        _write_editor_fence(window, controller, owner, module)
        apply_compute_editor_operation(module, owner, operation,
            fields=fields, action=action, arguments=args)
        validation = compute_editor_validation(module, owner)
        resources = getattr(controller, "_project_input_resources", None)
        # These registered resource facades compare their existing in-memory
        # draft signature to the saved baseline. They never save or resolve.
        dirty = bool(resources._is_dirty()) if resources is not None else None
    after = project_editor_snapshot(launcher, session, module,
        {"detail": "summary", "limit": 1}, client_generation=client_generation, bridge=bridge)
    summary = after.get("summary", {})
    return {"accepted": True, "project_id": session.binding.project_id,
        "session_generation": session.binding.generation, "client_generation": client_generation,
        "module_key": module, "instance_id": instance, "based_on_snapshot_id": expected,
        "snapshot_id": after.get("snapshot_id"), "snapshot": after,
        "revision": summary.get("revision"), "dirty": summary.get("dirty", dirty), "validation": validation,
        "capabilities": project_editor_capabilities(module), "refresh_snapshot": True}


def project_editor_operation(launcher: Any, session: Any, module: str, operation: str,
                             arguments: dict, *, client_generation: str, bridge: Any) -> dict:
    """Run exactly one real editor action after all current-owner fences pass."""
    from PySide6.QtCore import QCoreApplication, QThread
    from .project_editor_snapshot import project_editor_snapshot
    if module not in SUPPORTED_MODULES:
        raise UiControlError("unsupported_operation", "此模块尚无经验证的当前内容操作接口：" +
                             json.dumps(project_editor_capabilities(module), ensure_ascii=False))
    if operation not in {"update_window", "window_action"}:
        raise UiControlError("unsupported_operation", "此入口只接受受控字段或窗口动作。")
    expected = arguments.get("expected_snapshot_id")
    if not isinstance(expected, str) or len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise UiControlError("requires_snapshot", "请先读取当前编辑器快照，再提交其完整expected_snapshot_id。")
    instance = arguments.get("instance_id")
    if not isinstance(instance, str) or not instance:
        raise UiControlError("window_identity_required", "请显式提交当前窗口的instance_id。")
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        raise UiControlError("gui_thread_required", "工程编辑只能在所属GUI线程执行。")
    # Endpoints accept only authenticated forwarding, never a focus-derived or
    # partially supplied target. client_id was checked by the shared registry.
    if arguments.get("client_generation") != client_generation:
        raise UiControlError("stale_client", "工程进程身份已经失效。")
    if (arguments.get("project_id") != session.binding.project_id
            or arguments.get("session_generation") != session.binding.generation):
        raise UiControlError("stale_project", "工程会话身份已经失效。")
    if any(arguments.get(key, module) != module for key in ("module_key", "window_type")):
        raise UiControlError("invalid_window", "请求模块与当前窗口不一致。")
    permitted = {"project_id", "session_generation", "client_generation", "project_root", "project_file",
                 "module_key", "window_type", "instance_id", "expected_snapshot_id"}
    permitted |= {"fields"} if operation == "update_window" else {"action", "arguments"}
    if set(arguments) - permitted:
        _invalid("不接受文档/路径替换、force或未知操作参数。")
    if operation == "update_window":
        requested_fields = arguments.get("fields")
        if not isinstance(requested_fields, dict) or len(requested_fields) != 1:
            _invalid("每次受控更新须明确一个字段，以保留原子提交边界。")
        fields = requested_fields
    else:
        requested_action, args = arguments.get("action"), arguments.get("arguments", {})
        actions = (DESIGN_ACTIONS if module == "film_design" else OPTIMIZATION_ACTIONS
                   if module == "film_optimization" else SIMULATION_ACTIONS if module == "film_simulation"
                   else {key: frozenset(value) for key, value in project_editor_capabilities(module)["action_arguments"].items()})
        if not isinstance(requested_action, str) or requested_action not in actions:
            raise UiControlError("unsupported_action", "当前窗口动作尚未开放：" +
                                 json.dumps(project_editor_capabilities(module), ensure_ascii=False))
        name = requested_action
        if not isinstance(args, dict) or set(args) != actions[name]:
            _invalid("窗口动作只接受能力矩阵中列出的精确参数。")
    try:
        with launcher.host.operation_scope(session.binding) as store:
            if (not isinstance(arguments.get("project_root"), str)
                    or not isinstance(arguments.get("project_file"), str)
                    or canonical_path(arguments["project_root"]) != canonical_path(store.root)
                    or canonical_path(arguments["project_file"]) != canonical_path(store.project_file)):
                raise UiControlError("project_identity_mismatch", "工程目录或描述文件与当前会话不一致。")
            closer = getattr(launcher.desktop, "project_closer", None)
            if closer is not None and closer.is_pending(session.binding):
                raise UiControlError("project_closing", "工程正在等待原窗口关闭审核。")
            if module not in FILM_MODULES:
                return _registered_editor_operation(launcher, session, module, operation, arguments,
                    client_generation=client_generation, bridge=bridge)
            workspace = launcher.host.resource_for(session.binding, "film_workspace")
            editor = getattr(workspace, "_editor", None)
            if workspace is None or editor is None:
                raise UiControlError("requires_snapshot", "当前工程编辑器尚未就绪。")
            with editor._lock:
                routes = [route for route in launcher.host.routes(session.binding.project_id)
                          if route.module_key == module]
                if len(routes) != 1 or routes[0].route_id != instance:
                    raise UiControlError("stale_window", "窗口实例已关闭或替换。")
                route = routes[0]
                window = launcher.host.window_for(route)
                if window is None:
                    raise UiControlError("stale_window", "窗口实例已关闭或替换。")
                closer = getattr(launcher.desktop, "project_closer", None)
                if closer is not None and closer.is_pending(session.binding):
                    raise UiControlError("project_closing", "工程正在等待原窗口关闭审核。")
                before = project_editor_snapshot(launcher, session, module,
                    {"detail": "summary", "limit": 1, "expected_snapshot_id": expected},
                    client_generation=client_generation, bridge=bridge)
                if not before.get("available"):
                    raise UiControlError("editor_unavailable", "当前内容暂不可操作：" + str(before.get("reason")))
                if before.get("instance_id") != instance:
                    raise UiControlError("stale_window", "快照不属于此窗口实例。")
                controller = launcher.desktop.controller_for(workspace, module)
                controller_editor = getattr(controller, "_source_session", None) if module == "film_simulation" else getattr(controller, "session", None)
                if (controller is None or getattr(controller, "project_context", None) is not workspace
                        or controller_editor is None or controller_editor is not editor
                        or getattr(window, "_project_film_controller", None) is not controller):
                    raise UiControlError("editor_owner_mismatch", "当前窗口与工程编辑器所有者不一致。")
                for related_module in FILM_MODULES:
                    related = launcher.desktop.controller_for(workspace, related_module)
                    if related is not None and (getattr(related, "busy", False)
                            or getattr(related, "expressionBusy", False)):
                        raise UiControlError("window_busy", "相关膜系窗口正在处理任务，请等待完成或按原任务合同取消。")
                if (getattr(controller, "_pending_close", False)
                        or getattr(controller, "_shutting_down", False)
                        or getattr(controller, "_pending_reuse_source", None) is not None
                        or getattr(controller, "hasPendingOpen", False)):
                    raise UiControlError("pending_confirmation", "请先完成原窗口的关闭或来源切换审核。")
                # All dispatch targets below are literal public controller
                # calls. No request string ever becomes getattr/invokeMethod.
                if operation == "update_window":
                    if module == "film_design":
                        _design_update(controller, fields, editor)
                    elif module == "film_optimization":
                        _optimization_update(controller, fields)
                    else:
                        _simulation_update(controller, fields)
                elif module == "film_design":
                    _design_action(controller, editor, name, args)
                elif module == "film_optimization":
                    _optimization_action(controller, editor, name, args)
                else:
                    _ensure_accepted(editor.save_current(), controller)
                return {"accepted": True, "project_id": session.binding.project_id,
                    "session_generation": session.binding.generation, "client_generation": client_generation,
                    "module_key": module, "instance_id": instance,
                    "based_on_snapshot_id": expected, "revision": editor.snapshot().revision,
                    "design_revision": editor.snapshot().design_revision, "dirty": editor.snapshot().dirty,
                    "capabilities": project_editor_capabilities(module), "refresh_snapshot": True}
    except ProjectSessionError as exc:
        raise UiControlError(exc.code, str(exc)) from exc
