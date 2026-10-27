"""Independent thickness-error panel using the durable local CPU scheduler.

This is not an ordinary thickness optimizer mode. It captures its own complete
request and never silently changes the source design or nominal acceptance.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import inspect
import math
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QRunnable, QThreadPool, QUrl, Signal, Slot

from coat_sim.core.film_thickness_error_table import FilmThicknessErrorParameter
from coat_sim.core.film_thickness_error_table import thickness_error_table_request_from_optimization
from coat_sim.core.film_result_service import atomic_film_json
from coat_sim.scheduler import SchedulerClient, freeze_spec
from coat_sim.scheduler.desktop import RemoteCancelEvent, observe_job, submit_local_with_receipt


_TABLE_FIELDS = ("bias_text", "bias_unit", "uncertainty_kind", "uncertainty_text", "uncertainty_unit")
_TABLE_UNITS = ("", "absolute_nm", "relative_percent")


@dataclass(frozen=True)
class _TableRowDraft:
    enabled: bool
    override: bool
    values: dict[str, str]


def _table_values(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(_TABLE_FIELDS):
        raise ValueError("参数表字段必须包含偏差、单位及不确定度类型、数值、单位。")
    result: dict[str, str] = {}
    for field in _TABLE_FIELDS:
        item = value[field]
        if not isinstance(item, str):
            raise ValueError("参数表草稿字段必须是文本。")
        result[field] = item
    if (result["bias_unit"] not in _TABLE_UNITS or result["uncertainty_unit"] not in _TABLE_UNITS
            or result["uncertainty_kind"] not in ("", "bounded", "stddev")):
        raise ValueError("参数表单位或不确定度类型不受支持。")
    return result


@dataclass(frozen=True)
class _TableConfigurationDraft:
    defaults: dict[str, str]
    rows: dict[str, _TableRowDraft]
    text: str
    bound_ids: tuple[str, ...] | None
    invalidated: bool
    uncorrelated: bool
    finite_bias: bool


def parse_table_configuration(configuration: object) -> _TableConfigurationDraft:
    """Validate the existing draft schema without changing a live controller."""
    if not isinstance(configuration, dict):
        raise ValueError("参数表配置必须为对象。")
    if not configuration:
        configuration = {"version": 1, "defaults": {field: "" for field in _TABLE_FIELDS},
            "layers": [], "correlation_text": "", "correlation_layer_ids": None,
            "correlation_invalidated": False, "uncorrelated": False, "include_finite_bias": False}
    expected = {"version", "defaults", "layers", "correlation_text", "correlation_layer_ids",
                "correlation_invalidated", "uncorrelated", "include_finite_bias"}
    if set(configuration) != expected or type(configuration["version"]) is not int or configuration["version"] != 1:
        raise ValueError("参数表配置版本或字段不受支持。")
    defaults = _table_values(configuration["defaults"])
    rows: dict[str, _TableRowDraft] = {}
    raw_layers: object = configuration["layers"]
    if not isinstance(raw_layers, list):
        raise ValueError("参数表膜层配置必须为数组。")
    for raw in raw_layers:
        if not isinstance(raw, dict) or set(raw) != {"layer_id", "enabled", "override", "values"}:
            raise ValueError("参数表膜层配置字段不受支持。")
        key, enabled, override = raw["layer_id"], raw["enabled"], raw["override"]
        if (not isinstance(key, str) or not key or key in rows
                or type(enabled) is not bool or type(override) is not bool):
            raise ValueError("参数表膜层 ID 或选择/例外标志无效。")
        values = _table_values(raw["values"]) if override else {}
        if not override and raw["values"] != {}:
            raise ValueError("继承公共参数的膜层不能同时保存例外数值。")
        rows[key] = _TableRowDraft(enabled, override, values)
    text: object = configuration["correlation_text"]
    ids: object = configuration["correlation_layer_ids"]
    if not isinstance(text, str):
        raise ValueError("相关矩阵草稿或有序层 ID 无效。")
    bound_ids: list[str] | None = None
    if ids is not None:
        if not isinstance(ids, list):
            raise ValueError("相关矩阵有序层 ID 必须为数组。")
        bound_ids = []
        for key in ids:
            if not isinstance(key, str) or not key or key in bound_ids:
                raise ValueError("相关矩阵有序层 ID 无效或重复。")
            bound_ids.append(key)
    invalidated: object = configuration["correlation_invalidated"]
    uncorrelated: object = configuration["uncorrelated"]
    finite_bias: object = configuration["include_finite_bias"]
    if not isinstance(invalidated, bool) or not isinstance(uncorrelated, bool) or not isinstance(finite_bias, bool):
        raise ValueError("参数表配置标志必须为布尔值。")
    return _TableConfigurationDraft(defaults, rows, text, None if bound_ids is None else tuple(bound_ids),
                                    invalidated, uncorrelated, finite_bias)


class _Signals(QObject):
    progress = Signal(int, object)
    finished = Signal(int, object, str)
    admitted = Signal(int)


class _SensitivityTask(QRunnable):
    def __init__(
        self,
        service,
        request,
        client,
        job_id,
        cancel,
        metadata,
        generation,
        signals,
        disconnected,
        admission_check,
        live_display=None,
    ):
        super().__init__()
        self.service, self.request, self.client = service, request, client
        self.job_id, self.cancel, self.metadata = job_id, cancel, metadata
        self.generation, self.signals = generation, signals
        self.disconnected, self.admission_check = disconnected, admission_check
        self.live_display = live_display

    def run(self):
        try:
            from coat_sim.core.film_sensitivity_workflow_jobs import (
                freeze_workflow_request, workflow_for_request, validate_workflow_result,
                terminal_reply,
            )
            executor = workflow_for_request(self.request)
            request = freeze_workflow_request(self.service, self.request)
            spec = freeze_spec(None, executor, {"request": request})
            if self.cancel.is_set() or self.disconnected.is_set() or not self.admission_check():
                raise InterruptedError(
                    "Cancelled or source/project changed before admission; no job was submitted."
                )
            capabilities = self.client.capabilities()
            if executor not in capabilities.get("executors", ()):
                raise ValueError(
                    f"The running local scheduler does not support {executor}; close it normally and restart the application."
                )
            if self.cancel.is_set() or self.disconnected.is_set() or not self.admission_check():
                raise InterruptedError(
                    "Cancelled or source/project changed during capability discovery; no job was submitted."
                )
            submit_local_with_receipt(self.client, spec, job_id=self.job_id, metadata=self.metadata)
            self.cancel.mark_admitted()
            self.signals.admitted.emit(self.generation)
            display_options = {}
            if self.live_display is not None:
                try:
                    attempt = ("sensitivity", self.generation)
                    self.live_display.bind_admitted(attempt, self.client, self.job_id)
                    if "display" in inspect.signature(observe_job).parameters:
                        display_options["display"] = lambda packet: self.live_display.offer(attempt, packet)
                except Exception:
                    pass  # Display failures do not change the independent analysis/job.
            try:
                payload = observe_job(
                    self.client,
                    self.job_id,
                    lambda item: self.signals.progress.emit(self.generation, item),
                    disconnected=self.disconnected.is_set, **display_options,
                )
            except (InterruptedError, RuntimeError):
                row = self.client.status(self.job_id)
                payload = row.get("result")
                if not isinstance(payload, dict) or row.get("state") not in ("failed", "cancelled"):
                    raise
                payload = terminal_reply(payload, row["state"])
            from coat_sim.core.film_sensitivity_workflow_jobs import result_matches_job
            if not result_matches_job(payload, spec["parameters"]["payload"]):
                raise ValueError("Sensitivity result is not bound to this job request/operation.")
            validated = validate_workflow_result(payload, expected_request=request, replay=True)
            self.signals.finished.emit(self.generation, validated, "")
        except Exception as exc:
            from coat_sim.scheduler.remote_client import RemoteSubmissionUnknown

            prefix = "submission_unknown: " if isinstance(exc, RemoteSubmissionUnknown) else ""
            self.signals.finished.emit(
                self.generation, None, prefix + f"{type(exc).__name__}: {exc}"
            )


class FilmSensitivityQuickController(QObject):
    changed = Signal()
    tableRowsChanged = Signal()
    idealRowsChanged = Signal()
    tableConfigurationEdited = Signal()
    overwriteRequested = Signal(str)

    def __init__(self, owner: Any, *, client_factory=SchedulerClient):
        super().__init__(owner)
        self.owner = owner
        self.client_factory = client_factory
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._signals = _Signals()
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.admitted.connect(self._on_admitted)
        self._generation = 0
        self._busy = False
        self._closed = False
        self._unknown_submission = False
        self._prepared_source: dict[str, Any] | None = None
        self._prepared_signature: Any = None
        self._error = ""
        self._context: dict[str, Any] | None = None
        self._table_context: dict[str, Any] | None = None
        self._ideal_context: dict[str, Any] | None = None
        self._job_id = ""
        self._table_job_id = ""
        self._ideal_job_id = ""
        self._cancel: RemoteCancelEvent | None = None
        self._disconnected = threading.Event()
        self._ideal_applied = False
        # The ordinary dialog has two independent, explicitly-typed workflows.
        # Keep drafts and frozen reports separate so switching pages cannot
        # migrate assumptions or reset either result.
        self._table_rows: list[dict[str, Any]] = []
        self._table_defaults: dict[str, str] = {field: "" for field in _TABLE_FIELDS}
        self._table_saved_rows: dict[str, _TableRowDraft] = {}
        self._ideal_layer_rows: list[dict[str, Any]] = []
        self._table_correlation_json = ""
        self._table_correlation_bound_layer_ids: tuple[str, ...] | None = None
        self._table_correlation_invalidated = False
        self._table_uncorrelated = False
        self._table_finite_bias = False
        self._table_payload: dict[str, Any] | None = None
        self._table_status = "填写参数表后运行；未填写不等于零。"
        self._table_error = ""
        self._ideal_mode = ""
        # Policy and per-row allowances are an independent draft, keyed by the
        # owner's stable
        # objective IDs so reordering objectives never reassigns an allowance.
        self._ideal_policy = "bounded_baseline_degradation"
        self._ideal_objective_rows: list[dict[str, Any]] = []
        self._ideal_budget_texts: dict[str, str] = {}
        self._ideal_layer_ids: list[str] | None = None
        self._ideal_selection_explicit = False
        self._ideal_options = {"max_iterations": "", "max_evaluations": ""}
        self._ideal_payload: dict[str, Any] | None = None
        self._ideal_status = "默认采用受控基准退化；请选择绝对或相对尺度，并为每个目标填写百分点额度。"
        self._ideal_error = ""
        self._active_workflow = "ideal"

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(str, notify=changed)
    def activeWorkflow(self):
        return self._active_workflow

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(str, notify=changed)
    def jobId(self):
        return self._table_job_id if self._active_workflow == "table" else self._ideal_job_id

    @Property(str, notify=changed)
    def tableJobId(self):
        return self._table_job_id

    @Property(str, notify=changed)
    def idealJobId(self):
        return self._ideal_job_id

    @Property(list, notify=tableRowsChanged)
    def thicknessErrorRows(self):
        return deepcopy(self._table_rows)

    @Property(dict, notify=changed)
    def tableDefaults(self) -> dict[str, str]:
        return dict(self._table_defaults)

    @Slot(result="QVariantMap")
    def exportTableConfiguration(self) -> dict[str, object]:
        states = ({row["layer_id"]: _TableRowDraft(
            bool(row["enabled"]), bool(row["override"]),
            {field: row[field] for field in _TABLE_FIELDS} if row["override"] else {})
            for row in self._table_rows} if self._table_rows else self._table_saved_rows)
        return {"version": 1, "defaults": dict(self._table_defaults),
                "layers": [{"layer_id": key, "enabled": row.enabled, "override": row.override,
                            "values": dict(row.values)} for key, row in states.items()],
                "correlation_text": self._table_correlation_json,
                "correlation_layer_ids": (None if self._table_correlation_bound_layer_ids is None
                                           else list(self._table_correlation_bound_layer_ids)),
                "correlation_invalidated": self._table_correlation_invalidated,
                "uncorrelated": self._table_uncorrelated, "include_finite_bias": self._table_finite_bias}

    @Slot("QVariantMap", result=bool)
    def loadTableConfiguration(self, configuration: dict[str, object]) -> bool:
        if self._busy or self._closed:
            return False
        try:
            draft = parse_table_configuration(configuration)
        except (ValueError, TypeError) as exc:
            self._table_error = str(exc)
            self.changed.emit()
            return False
        self._table_defaults, self._table_saved_rows = draft.defaults, draft.rows
        self._table_rows = []
        self._prepared_source = self._prepared_signature = None
        self._table_correlation_json = draft.text
        self._table_correlation_bound_layer_ids = draft.bound_ids
        self._table_correlation_invalidated = draft.invalidated
        self._table_uncorrelated = draft.uncorrelated
        self._table_finite_bias = draft.finite_bias
        self._table_error = ""
        self._table_status = "参数表草稿已恢复；重新准备来源后使用当前名义厚度。"
        self.tableRowsChanged.emit()
        self.changed.emit()
        return True

    @Slot(str, str, result=bool)
    def setTableDefault(self, field: str, value: str) -> bool:
        if self._busy or self._closed or field not in _TABLE_FIELDS or not isinstance(value, str):
            return False
        candidate = {**self._table_defaults, field: value}
        try:
            _table_values(candidate)
        except ValueError:
            return False
        if candidate == self._table_defaults:
            return True
        self._table_defaults = candidate
        for row in self._table_rows:
            if not row["override"]:
                row[field] = value
        self._typed_edit("table")
        self._refresh_table_correlation_identity_error()
        self.tableRowsChanged.emit()
        return True

    @Slot(str, bool, result=bool)
    def setTableLayerOverride(self, layer_id: str, enabled: bool) -> bool:
        if self._busy or self._closed or type(enabled) is not bool:
            return False
        row = self._table_row(layer_id)
        if row is None:
            return False
        if row["override"] == enabled:
            return True
        row["override"] = enabled
        if not enabled:
            row.update(self._table_defaults)
        self._typed_edit("table")
        self._refresh_table_correlation_identity_error()
        self.tableRowsChanged.emit()
        return True

    @Property(str, notify=changed)
    def tableCorrelationJson(self):
        return self._table_correlation_json

    @Property(bool, notify=changed)
    def tableUncorrelated(self):
        return self._table_uncorrelated

    @Property(bool, notify=changed)
    def tableIncludeFiniteBias(self):
        return self._table_finite_bias

    @Property(list, notify=changed)
    def tableStandardDeviationLayerIds(self):
        return [row["layer_id"] for row in self._table_rows
                if row["enabled"] and row["uncertainty_kind"] == "stddev"
                and isinstance(row["uncertainty_text"], str) and row["uncertainty_text"].strip()]

    def _current_table_stddev_layer_ids(self):
        return tuple(row["layer_id"] for row in self._table_rows
                     if row["enabled"] and row["uncertainty_kind"] == "stddev"
                     and isinstance(row["uncertainty_text"], str)
                     and row["uncertainty_text"].strip())

    def _table_correlation_identity_error(self, current_ids=None):
        if not self._table_correlation_json.strip() or self._table_uncorrelated:
            return ""
        current_ids = (self._current_table_stddev_layer_ids() if current_ids is None
                       else tuple(current_ids))
        bound_ids = self._table_correlation_bound_layer_ids
        if bound_ids is None or current_ids != bound_ids:
            self._table_correlation_invalidated = True
        if not self._table_correlation_invalidated:
            return ""
        original = "未绑定" if bound_ids is None else ", ".join(bound_ids) or "无"
        current = ", ".join(current_ids) or "无"
        return ("rho 矩阵身份已失效，矩阵文本已保留但不会按新顺序解释。"
                f"录入时有序标准差层 ID：[{original}]；当前有序标准差层 ID：[{current}]。"
                "请清空 rho 矩阵文本后，按当前 ID 顺序重新输入；矩阵不会自动重排、平均或修复。")

    def _refresh_table_correlation_identity_error(self):
        previous = self._table_error
        message = self._table_correlation_identity_error()
        if message:
            self._table_error = message
        elif self._table_error.startswith("rho 矩阵身份已失效"):
            self._table_error = ""
        if self._table_error != previous:
            self.changed.emit()
        return message

    @Property(bool, notify=changed)
    def tableHasResult(self):
        return self._table_payload is not None

    @Property(str, notify=changed)
    def tableResultSummary(self):
        return self._typed_result_summary("table")

    @Property(str, notify=changed)
    def tableStatus(self):
        return self._table_status

    @Property(str, notify=changed)
    def tableError(self):
        return self._table_error

    @Property(str, notify=changed)
    def idealSensitivityMode(self):
        return self._ideal_mode

    @Property(str, notify=changed)
    def idealSensitivityPolicy(self):
        return self._ideal_policy

    @Property(list, notify=idealRowsChanged)
    def idealDegradationObjectives(self):
        return deepcopy(self._ideal_objective_rows)

    @Property(list, notify=idealRowsChanged)
    def idealSensitivityLayers(self):
        return [dict(row, selected=row["layer_id"] in (self._ideal_layer_ids or []))
                for row in self._ideal_layer_rows]

    @Property(str, notify=changed)
    def idealAssumptionSummary(self):
        if not self._prepared_source:
            return "来源尚未准备"
        layers = self._prepared_source.get("front_layers", ())
        material_count = len({row.get("material_id") for row in layers})
        return (f"{len(layers)} 个前表面层、{material_count} 种材料；冻结来源的光学常数/消光系数和原目标网格，"
                "k/use_k 保持来源值且不默认 k=0；不从参数表读取误差幅度或权重。")

    @Property(dict, notify=changed)
    def idealOptions(self):
        return dict(self._ideal_options)

    @Property(bool, notify=changed)
    def idealHasResult(self):
        return self._ideal_payload is not None

    @Property(str, notify=changed)
    def idealResultSummary(self):
        return self._typed_result_summary("ideal")

    @Property(str, notify=changed)
    def idealApplyConfirmationSummary(self):
        payload = self._ideal_payload
        if payload is None:
            return "没有可应用的完整验证候选。"
        try:
            from coat_sim.core.film_sensitivity_workflow_jobs import workflow_result_from_payload
            result = workflow_result_from_payload(payload)
        except (ValueError, TypeError, KeyError, AttributeError):
            return "候选报告无法通过验证；不会应用。"
        baseline = getattr(result, "baseline_analysis", None)
        best = getattr(result, "best_analysis", None)
        initial_met = getattr(baseline, "nominal_targets_met", None)
        final_met = getattr(best, "nominal_targets_met", None)
        nominal = ("通过" if final_met is True else "未通过" if final_met is False else "未知")
        initial = ("通过" if initial_met is True else "未通过" if initial_met is False else "未知")
        if getattr(result, "policy", None) == "strict_nominal":
            warning = ("最终原名义目标未通过；严格策略禁止应用。"
                       if final_met is False else "")
            return (f"初态原名义目标：{initial}；最终原名义目标：{nominal}。"
                    f"严格名义策略不启用退化额度。{warning} 一次应用形成一笔可撤销修改，不会写入项目文件。")
        guard = getattr(result, "best_guard", None)
        if guard is None:
            guard_line = "最大定向退化：未提供；guard 未通过。"
        else:
            maximum_pp = float(guard.max_directional_degradation_fraction) * 100.0
            guard_line = (f"最大定向退化：{maximum_pp:.7g} pp；"
                          f"所有逐目标额度{('内' if guard.guard_pass else '外')}。")
        warning = ("最终原名义目标未通过警示：guard 额度通过不代表原名义目标通过。"
                   if final_met is False else "")
        return (f"初态原名义目标：{initial}；最终原名义目标：{nominal}。{guard_line}"
                f"{warning} 一次应用形成一笔可撤销修改，不会写入项目文件。")

    @Property(str, notify=changed)
    def idealStatus(self):
        return self._ideal_status

    @Property(str, notify=changed)
    def idealError(self):
        return self._ideal_error

    @Property(bool, notify=changed)
    def idealCanApply(self):
        return self._can_apply_workflow("ideal")

    def _payload_for_workflow(self, workflow):
        return self._table_payload if workflow == "table" else self._ideal_payload

    @staticmethod
    def _summary_value(value):
        if value is None:
            return "未提供"
        try:
            if hasattr(value, "__dataclass_fields__"):
                from dataclasses import asdict
                value = asdict(value)
            return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError):
            if isinstance(value, (list, tuple)):
                return "、".join(str(item) for item in value)
            return str(value)

    def _typed_result_summary(self, workflow):
        payload = self._payload_for_workflow(workflow)
        if payload is None:
            return ""
        from coat_sim.core.film_sensitivity_workflow_jobs import workflow_result_from_payload
        result = workflow_result_from_payload(payload)
        analysis = (getattr(result, "analysis", None) if workflow == "table"
                    else getattr(result, "best_analysis", None))
        frozen = getattr(result, "frozen_request", None)
        if workflow == "table":
            coverage = getattr(analysis, "coverage", None)
            conditions = getattr(analysis, "conditions", ())
            layers = getattr(analysis, "layer_summaries", None)
            correlation = getattr(analysis, "correlation", None)
            condition_count = len(conditions or ())
            condition_preview = []
            for row in list(conditions or ())[:3]:
                condition_preview.append(
                    f"{row.wavelength_nm:g} nm/{row.angle_deg:g}° {row.metric}: "
                    f"bias={self._summary_value(row.linear_bias_value)}, "
                    f"bounded L1={self._summary_value(row.bounded_first_order_l1)}, "
                    f"σ={self._summary_value(row.aggregate_sigma)} ({row.aggregate_sigma_status})"
                )
            layer_preview = [f"{row.layer_id}: max |dM/dd|={row.max_abs_derivative_per_nm:g}/nm"
                             for row in list(layers or ())[:8]]
            return ("参数表预期误差分析 · " + str(result.state) + " / " + str(result.termination_reason)
                    + "\n覆盖范围：" + self._summary_value(coverage)
                    + "\n相关性：" + self._summary_value(getattr(correlation, "status", None))
                    + f"；合并标准差状态：{getattr(analysis, 'aggregate_sigma_status', '未提供')}"
                    + f"\n条件完成：{getattr(analysis, 'completed_condition_count', 0)}/{getattr(analysis, 'expected_condition_count', condition_count)}"
                    + f"；原始名义目标通过：{self._summary_value(getattr(analysis, 'nominal_targets_met', None))}"
                    + "\n条件预览：" + ("；".join(condition_preview) if condition_preview else "无已完成条件")
                    + "\n逐层灵敏度预览：" + ("；".join(layer_preview) if layer_preview else "无")
                    + "\n偏差与不确定度分栏呈现，不合并为总概率；此流程不生成候选设计，也不可应用。")
        value = getattr(analysis, "objective_value", None)
        nominal = getattr(analysis, "nominal_targets_met", None)
        selected = getattr(frozen, "sensitivity_layer_ids", ()) if frozen is not None else ()
        scale = getattr(frozen, "sensitivity_mode", "") if frozen is not None else ""
        baseline = getattr(result, "baseline_analysis", None)
        schema = result.schema_version
        policy = result.policy
        summary = ("理想敏感度优化 · " + str(result.state) + " / " + str(result.termination_reason)
                   + f"\n冻结敏感度尺度：{scale or '未读取'}；列 ID：{self._summary_value(selected)}"
                   + f"\n策略：{policy}（schema v{schema}）"
                   + f"\n原始加权敏感度平方和 F：best={self._summary_value(value)}；"
                   f"初态={self._summary_value(getattr(baseline, 'objective_value', None))}"
                   + f"\n归一化求解器 F：best={self._summary_value(getattr(result, 'best_solver_objective_value', None))}；"
                   f"初态={self._summary_value(getattr(result, 'baseline_solver_objective_value', None))}"
                   + f"\n初态原始名义目标通过：{self._summary_value(getattr(baseline, 'nominal_targets_met', None))}；"
                   f"最终原始名义目标通过：{self._summary_value(nominal)}")
        direction_names = {"lower": "仅下降", "upper": "仅上升", "both": "双向"}
        directions = getattr(frozen, "guard_directions", ())
        identifiers = getattr(frozen, "objective_ids", ())
        if len(directions) == len(identifiers) and directions:
            summary += "\n冻结逐点退化方向：" + "；".join(
                f"{identifier}：{direction_names.get(direction, direction)}"
                for identifier, direction in zip(identifiers, directions, strict=True))
        improvement_status = getattr(result, "improvement_status", "unavailable")
        if improvement_status not in {"improved", "no_improvement", "unavailable"}:
            improvement_status = "unavailable"
        summary += f"\n敏感度改善状态：{improvement_status}"
        for caption, guard in (("初态 guard", getattr(result, "baseline_guard", None)),
                               ("最终 guard", getattr(result, "best_guard", None))):
            if guard is None:
                summary += f"\n{caption}：未提供"
                continue
            maximum_pp = float(guard.max_directional_degradation_fraction) * 100.0
            worst = (f"{guard.worst_objective_id} / {guard.wavelength_nm:g} nm / "
                     f"{guard.angle_deg:g}° / {guard.polarization}"
                     if guard.worst_objective_id is not None else "无最坏点")
            summary += (f"\n{caption}：{'通过' if guard.guard_pass else '未通过'}；"
                        f"最大定向退化 {maximum_pp:.7g} pp；最坏点 {worst}")
            if caption == "最终 guard":
                for row in guard.rows:
                    summary += (f"\n额度行 {row.objective_id}：{row.delta_percentage_point:g} pp "
                                f"({row.delta_fraction:.7g} fraction)；"
                                f"退化消耗 {row.consumed_fraction * 100:.7g} pp；"
                                f"超额 {row.excess_fraction * 100:.7g} pp；最坏点 "
                                f"{row.wavelength_nm:g} nm/{row.angle_deg:g}°/{row.polarization}")
        summary += ("\n存在可审阅候选；必须显式确认后才会应用。"
                    if getattr(result, "best_design", None) is not None
                    else "\n没有可应用候选。")
        if nominal is False:
            summary += "\n警示：原名义目标未通过；guard 额度通过不代表原名义目标通过。"
        return summary

    def _can_apply_workflow(self, workflow):
        if workflow != "ideal":
            return False
        payload = self._payload_for_workflow(workflow)
        context = self._table_context if workflow == "table" else self._ideal_context
        if (payload is None or self._busy or self._closed or context is None
                or context.get("workflow") != workflow or self.owner.session is None
                or self.owner.busy or (workflow == "ideal" and self._ideal_applied)):
            return False
        try:
            from coat_sim.core.film_sensitivity_workflow_jobs import workflow_result_from_payload
            result = workflow_result_from_payload(payload)
            if (result.state not in {"completed", "partial"} or not result.is_final
                    or getattr(result, "operation", "") != "optimize"
                    or result.best_design is None):
                return False
            analysis = getattr(result, "best_analysis", None)
            nominal_met = getattr(analysis, "nominal_targets_met", None) is True
            if result.schema_version != 3:
                return False
            if result.policy == "strict_nominal":
                if not nominal_met:
                    return False
            elif result.policy == "bounded_baseline_degradation":
                if result.usable_candidate is not True or result.guard_pass is not True:
                    return False
            else:
                return False
            compute = context.get("project_compute")
            if compute is not None and (compute.editing_frozen or not compute.accepts(context.get("project_operation"))):
                return False
            return (self.owner.session.snapshot().design_revision == context["revision"]
                    and self.owner._source_edit_epoch() == context["epoch"]
                    and result.design_revision == context["revision"])
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            return False

    @Slot(int, result=bool)
    def selectWorkflow(self, index):
        if type(index) is not int or index not in (0, 1):
            return False
        value = ("table", "ideal")[index]
        if value != self._active_workflow:
            self._active_workflow = value
            self.changed.emit()
        return True

    def _context_is_current(self, context):
        if context is None or self.owner.session is None:
            return False
        compute = context.get("project_compute")
        if compute is not None and (compute.editing_frozen or not compute.accepts(context.get("project_operation"))):
            return False
        return (self.owner.session.snapshot().design_revision == context["revision"]
                and self.owner._source_edit_epoch() == context["epoch"])

    def _display_context_is_current(self, context, session, source):
        if (self._closed or self.owner._source_closed or self.owner._observers_detached
                or self.owner.session is not session or self.owner._source_controller is not source):
            return False
        if session is not None:
            return self._context_is_current(context)
        compute = context.get("project_compute")
        return (self.owner._source_revision == context["revision"]
                and self.owner._source_edit_epoch() == context["epoch"]
                and (compute is None or (not compute.editing_frozen
                    and compute.accepts(context.get("project_operation")))))

    def load_verified_history(self, payload, job_id):
        """Queue loader already replayed physics off the GUI thread.

        The history view has no captured source context and cannot apply.
        """
        from coat_sim.core.film_sensitivity_workflow_jobs import workflow_result_from_payload, workflow_for_request
        result = workflow_result_from_payload(payload)
        workflow = workflow_for_request(result.frozen_request)
        if workflow not in {"film_thickness_error_table", "film_ideal_sensitivity"}:
            raise ValueError("unsupported historical sensitivity workflow")
        self._job_id = job_id
        self._context = None
        self._table_context = self._ideal_context = None
        self._ideal_applied = False
        self._active_workflow = "table" if workflow == "film_thickness_error_table" else "ideal"
        message = "只读历史报告；没有绑定当前设计，不可应用。"
        if workflow == "film_thickness_error_table":
            self._table_payload = deepcopy(payload)
            self._table_job_id = job_id
            self._table_status = message
            self._table_error = ""
        elif workflow == "film_ideal_sensitivity":
            self._ideal_payload = deepcopy(payload)
            self._ideal_job_id = job_id
            self._ideal_context = None
            self._ideal_status = message
            self._ideal_error = ""
        else:
            raise ValueError("unsupported historical sensitivity workflow")
        self.changed.emit()

    @Slot(result=bool)
    def prepare(self):
        if self._busy or self._closed:
            return False
        try:
            base = self.owner._build_request(require_variables=False, fixed_topology=False)
            self._prepared_source = deepcopy(dict(base.design_snapshot))
            self._prepared_signature = self._request_signature(base)
            previous_table = {row["layer_id"]: row for row in self._table_rows}
            table_rows = []
            for index, row in enumerate(base.design_snapshot["front_layers"]):
                previous = previous_table.get(row["id"], {})
                saved = self._table_saved_rows.get(row["id"])
                enabled = saved.enabled if saved is not None else previous.get("enabled", True)
                override = saved.override if saved is not None else previous.get("override", False)
                values = (saved.values if saved is not None else previous) if override else self._table_defaults
                table_rows.append({
                    "layer_id": row["id"],
                    "layer_label": f"{index + 1}. {row['material_id']} / {row['optical_set_id']}",
                    "nominal": f"{row['thickness']['value']:g} {row['thickness']['unit']}",
                    "enabled": enabled, "override": override,
                    **{field: values.get(field, "") for field in _TABLE_FIELDS},
                })
            self._table_rows = table_rows
            self._table_saved_rows = {}
            self._refresh_table_correlation_identity_error()
            self._ideal_layer_rows = [
                {"layer_id": row["id"],
                 "layer_label": f"{index + 1}. {row['material_id']} / {row['optical_set_id']}",
                 "nominal": f"{row['thickness']['value']:g} {row['thickness']['unit']}"}
                for index, row in enumerate(base.design_snapshot["front_layers"])
            ]
            objective_source = getattr(self.owner, "_objectives", ())
            if not isinstance(objective_source, (list, tuple)):
                raise ValueError("目标配置不是可读取的有序列表。")
            objective_ids = [row.get("id") for row in objective_source
                             if isinstance(row, dict)]
            if (len(objective_ids) != len(objective_source)
                    or any(not isinstance(value, str) or not value.strip() for value in objective_ids)
                    or len(set(objective_ids)) != len(objective_ids)
                    or len(objective_ids) != len(base.objectives)):
                raise ValueError("每个当前光学目标必须具有唯一稳定 ID；不会按位置猜测退化额度。")
            objective_rows = []
            for index, row in enumerate(objective_source):
                objective_id = row["id"]
                metric = str(row.get("metric", "R"))
                low = row.get("wavelength_min_nm")
                high = row.get("wavelength_max_nm")
                try:
                    low_value, high_value = float(low), float(high)
                    band = (f"{low_value:g} nm" if low_value == high_value else
                            f"{low_value:g}–{high_value:g} nm")
                except (TypeError, ValueError, OverflowError):
                    band = f"{low}–{high} nm"
                objective = base.objectives[index]
                criteria = []
                for kind, caption in (("mean", "波段均值"), ("pointwise", "逐点")):
                    constraint = getattr(objective, kind)
                    if not constraint.enabled:
                        continue
                    if constraint.mode == "value":
                        target = f"{metric} = {constraint.target_value * 100:g}%"
                    else:
                        sides = []
                        if constraint.lower_bound is not None:
                            sign = ">" if constraint.strict_lower else "≥"
                            sides.append(f"{metric} {sign} {constraint.lower_bound * 100:g}%")
                        if constraint.upper_bound is not None:
                            sign = "<" if constraint.strict_upper else "≤"
                            sides.append(f"{metric} {sign} {constraint.upper_bound * 100:g}%")
                        target = "，".join(sides)
                    criteria.append(f"{caption}：{target}")
                direction = "；".join(criteria)
                name = str(row.get("name") or f"目标 {index + 1}")
                objective_rows.append({
                    "objective_id": objective_id,
                    "name": name,
                    "band": band,
                    "metric": metric,
                    "direction": direction,
                    "budget_text": self._ideal_budget_texts.get(objective_id, ""),
                    "display": f"{name} · {band} · {direction}",
                })
            self._ideal_objective_rows = objective_rows
            current_ids = [row["layer_id"] for row in self._ideal_layer_rows]
            if self._ideal_layer_ids is None or not self._ideal_selection_explicit:
                self._ideal_layer_ids = list(current_ids)
            else:
                current_set = set(current_ids)
                self._ideal_layer_ids = [row_id for row_id in current_ids
                                         if row_id in current_set and row_id in self._ideal_layer_ids]
            self.tableRowsChanged.emit()
            self.idealRowsChanged.emit()
            if not self._table_rows:
                raise ValueError("敏感性分析需要已有膜层；先完成自动合成或添加真实膜系。")
            self._error = ""
            self.changed.emit()
            return True
        except (ValueError, TypeError, KeyError) as exc:
            self._error = str(exc)
            if self._active_workflow == "table":
                self._table_error = self._error
            else:
                self._ideal_error = self._error
            self.changed.emit()
            return False

    @staticmethod
    def _request_signature(base):
        design = deepcopy(dict(base.design_snapshot))
        # Page drafts are not physical inputs. Persisting this table must not
        # invalidate its own prepared source; objectives/variables stay bound.
        design.pop("workflow_settings", None)
        return deepcopy((base.design_revision, design, base.objectives, base.variables,
                         base.frozen_material_snapshot, base.acceptable_normalized_error))

    def _typed_edit(self, workflow):
        if workflow == "table":
            self._table_error = ""
            self._table_status = ("参数已编辑；下方仍保留上次冻结结果，不代表当前草稿。"
                                 if self._table_payload is not None else "填写参数表后运行；未填写不等于零。")
            self._refresh_table_correlation_identity_error()
            self.tableConfigurationEdited.emit()
        else:
            self._ideal_error = ""
            self._ideal_status = ("配置已编辑；下方仍保留上次冻结结果，不代表当前草稿。"
                                  if self._ideal_payload is not None else
                                  "默认采用受控基准退化；请选择绝对或相对尺度，并为每个目标填写百分点额度。")
        self.changed.emit()

    def _table_row(self, layer_id):
        return next((row for row in self._table_rows if row["layer_id"] == layer_id), None)

    @Slot(str, str, str, result=bool)
    def setTableDraft(self, layer_id, field, value):
        if self._busy or not isinstance(layer_id, str) or not isinstance(value, str):
            return False
        if field not in ("bias", "uncertainty_magnitude"):
            return False
        row = self._table_row(layer_id)
        if row is None:
            return False
        row["bias_text" if field == "bias" else "uncertainty_text"] = value
        row["override"] = True
        # Intentionally avoid tableRowsChanged while editing, so a QML
        # Repeater never rebuilds the active editor, selection, or IME state.
        self._typed_edit("table")
        self._refresh_table_correlation_identity_error()
        return True

    @Slot(str, str, str, result=bool)
    def setTableUnit(self, layer_id, component, unit):
        if self._busy or component not in ("bias", "uncertainty") or unit not in ("", "absolute_nm", "relative_percent"):
            return False
        row = self._table_row(layer_id)
        if row is None:
            return False
        row["bias_unit" if component == "bias" else "uncertainty_unit"] = unit
        row["override"] = True
        self.tableRowsChanged.emit()
        self._typed_edit("table")
        self._refresh_table_correlation_identity_error()
        return True

    @Slot(str, str, result=bool)
    def setTableUncertaintyKind(self, layer_id, value):
        if self._busy or value not in ("", "bounded", "stddev"):
            return False
        row = self._table_row(layer_id)
        if row is None:
            return False
        row["uncertainty_kind"] = value
        row["override"] = True
        self.tableRowsChanged.emit()
        self._typed_edit("table")
        self._refresh_table_correlation_identity_error()
        return True

    @Slot(str, bool, result=bool)
    def setTableLayerEnabled(self, layer_id, enabled):
        if self._busy or type(enabled) is not bool:
            return False
        row = self._table_row(layer_id)
        if row is None:
            return False
        row["enabled"] = enabled
        self.tableRowsChanged.emit()
        self._typed_edit("table")
        self._refresh_table_correlation_identity_error()
        return True

    @Slot(str, result=bool)
    def setTableCorrelationDraft(self, value):
        if self._busy or not isinstance(value, str):
            return False
        was_defined = bool(self._table_correlation_json.strip())
        is_defined = bool(value.strip())
        if not is_defined:
            self._table_correlation_bound_layer_ids = None
            self._table_correlation_invalidated = False
        elif not was_defined:
            self._table_correlation_bound_layer_ids = self._current_table_stddev_layer_ids()
            self._table_correlation_invalidated = False
        self._table_correlation_json = value
        self._typed_edit("table")
        self._refresh_table_correlation_identity_error()
        return True

    @Slot(bool, result=bool)
    def setTableUncorrelated(self, value):
        if self._busy or type(value) is not bool:
            return False
        self._table_uncorrelated = value
        self.changed.emit()
        self._typed_edit("table")
        self._refresh_table_correlation_identity_error()
        return True

    @Slot(bool, result=bool)
    def setTableIncludeFiniteBias(self, value):
        if self._busy or type(value) is not bool:
            return False
        self._table_finite_bias = value
        self._typed_edit("table")
        return True

    @Slot(str, result=bool)
    def setIdealSensitivityMode(self, value):
        if self._busy or value not in ("", "absolute", "relative"):
            return False
        self._ideal_mode = value
        self._typed_edit("ideal")
        return True

    @Slot(str, result=bool)
    def setIdealSensitivityPolicy(self, value):
        if self._busy or value not in ("strict_nominal", "bounded_baseline_degradation"):
            return False
        self._ideal_policy = value
        self._typed_edit("ideal")
        return True

    @Slot(str, str, result=bool)
    def setIdealDegradationBudget(self, objective_id, value):
        if self._busy or not isinstance(objective_id, str) or not isinstance(value, str):
            return False
        row = next((row for row in self._ideal_objective_rows
                    if row["objective_id"] == objective_id), None)
        if row is None:
            return False
        self._ideal_budget_texts[objective_id] = value
        row["budget_text"] = value
        # Avoid idealRowsChanged while typing, preserving the active editor.
        self._typed_edit("ideal")
        return True

    @Slot(str, bool, result=bool)
    def setIdealSensitivityLayer(self, layer_id, enabled):
        if self._busy or not isinstance(layer_id, str) or type(enabled) is not bool:
            return False
        current_ids = {row["layer_id"] for row in self._ideal_layer_rows}
        if layer_id not in current_ids:
            return False
        selected = set(self._ideal_layer_ids or ())
        if enabled:
            selected.add(layer_id)
        else:
            selected.discard(layer_id)
        self._ideal_layer_ids = [row["layer_id"] for row in self._ideal_layer_rows
                                 if row["layer_id"] in selected]
        self._ideal_selection_explicit = True
        self.idealRowsChanged.emit()
        self._typed_edit("ideal")
        return True

    @Slot(str, str, result=bool)
    def setIdealOption(self, name, value):
        if self._busy or name not in self._ideal_options or not isinstance(value, str):
            return False
        self._ideal_options[name] = value
        self._typed_edit("ideal")
        return True

    @staticmethod
    def _limit(value, name):
        if not isinstance(value, str):
            raise ValueError(f"{name} 必须是整数文本。")
        if not value.strip():
            return None
        result = int(value)
        if str(result) != value.strip() or result < 1:
            raise ValueError(f"{name} 必须是正整数或留空。")
        return result

    @staticmethod
    def _optional_decimal(value, name, *, signed):
        if not isinstance(value, str):
            raise ValueError(f"{name} 必须是数字文本。")
        if not value.strip():
            return None
        try:
            result = float(value)
        except ValueError as exc:
            raise ValueError(f"{name} 必须是有限数字。") from exc
        if not math.isfinite(result) or (not signed and result < 0):
            raise ValueError(f"{name} 必须为有限{'有符号' if signed else '非负'}数。")
        return result

    def _thickness_error_parameters(self):
        parameters = []
        configured = False
        stddev_ids = []
        for row in self._table_rows:
            enabled = row["enabled"]
            bias = None
            bias_unit = None
            uncertainty = None
            uncertainty_kind = None
            uncertainty_unit = None
            raw_bias = row["bias_text"]
            if raw_bias.strip():
                if row["bias_unit"] not in ("absolute_nm", "relative_percent"):
                    raise ValueError(f"请为 {row['layer_label']} 的偏差选择 nm 或 % 单位。")
                bias = self._optional_decimal(raw_bias, f"{row['layer_label']} 偏差", signed=True)
                bias_unit = row["bias_unit"]
                if bias_unit == "relative_percent":
                    bias /= 100.0
                    bias_unit = "relative_fraction"

            raw_uncertainty = row["uncertainty_text"]
            kind = row["uncertainty_kind"]
            unit = row["uncertainty_unit"]
            if raw_uncertainty.strip() or kind or unit:
                if not raw_uncertainty.strip():
                    raise ValueError(f"请填写 {row['layer_label']} 已设置类型/单位的不确定度数值；空白不是零。")
                if kind not in ("bounded", "stddev"):
                    raise ValueError(f"请为 {row['layer_label']} 选择有界或标准差不确定度类型。")
                if unit not in ("absolute_nm", "relative_percent"):
                    raise ValueError(f"请为 {row['layer_label']} 的不确定度选择 nm 或 % 单位。")
                uncertainty = self._optional_decimal(raw_uncertainty, f"{row['layer_label']} 不确定度", signed=False)
                uncertainty_kind = kind
                uncertainty_unit = unit
                if unit == "relative_percent":
                    uncertainty /= 100.0
                    uncertainty_unit = "relative_fraction"
            if enabled and (bias is not None or uncertainty is not None):
                configured = True
            if enabled and uncertainty_kind == "stddev" and uncertainty is not None:
                stddev_ids.append(row["layer_id"])
            parameters.append(FilmThicknessErrorParameter(
                layer_id=row["layer_id"], enabled=enabled, bias=bias, bias_unit=bias_unit,
                uncertainty_kind=uncertainty_kind, uncertainty_magnitude=uncertainty,
                uncertainty_unit=uncertainty_unit,
            ))
        if not configured:
            raise ValueError("请至少填写一个启用膜层的偏差或不确定度；全空不计算，明确的 0 合法。")
        return tuple(parameters), tuple(stddev_ids)

    def _build_thickness_error_request(self, base):
        rows, stddev_ids = self._thickness_error_parameters()
        identity_error = self._table_correlation_identity_error(stddev_ids)
        if identity_error:
            self._table_error = identity_error
            self.changed.emit()
            raise ValueError(identity_error)
        correlation_ids = ()
        matrix: Any = None
        if self._table_uncorrelated:
            if not stddev_ids:
                raise ValueError("已勾选互不相关，但没有完整配置的标准差行。")
            correlation_ids = stddev_ids
            matrix = [[1.0 if i == j else 0.0 for j in range(len(stddev_ids))]
                      for i in range(len(stddev_ids))]
        elif self._table_correlation_json.strip():
            try:
                matrix = json.loads(self._table_correlation_json)
            except json.JSONDecodeError as exc:
                raise ValueError("rho 矩阵 JSON 无法解析；请修正后再计算。") from exc
            correlation_ids = stddev_ids
        return thickness_error_table_request_from_optimization(
            base, rows=rows, correlation_layer_ids=correlation_ids, correlation_matrix=matrix,
            include_finite_bias=self._table_finite_bias,
        )

    def _build_ideal_request(self, base):
        if self._ideal_mode not in ("absolute", "relative"):
            raise ValueError("请选择绝对或相对敏感度尺度；不推定工艺意义或默认单位。")
        selected = tuple(row["layer_id"] for row in self._ideal_layer_rows
                         if row["layer_id"] in (self._ideal_layer_ids or ()))
        if not selected:
            raise ValueError("至少选择一个理想敏感度厚度列。")
        if self._ideal_policy not in ("strict_nominal", "bounded_baseline_degradation"):
            raise ValueError("请选择有效的理想敏感度退化策略。")
        objective_ids = tuple(row["objective_id"] for row in self._ideal_objective_rows)
        if len(objective_ids) != len(base.objectives) or not objective_ids:
            raise ValueError("当前目标列表已变化；请刷新来源后重新核对稳定目标 ID。")
        budgets = []
        if self._ideal_policy == "bounded_baseline_degradation":
            from coat_sim.core.film_ideal_sensitivity import FilmIdealDegradationBudget
            for row in self._ideal_objective_rows:
                text = self._ideal_budget_texts.get(row["objective_id"], "")
                if not text.strip():
                    raise ValueError(
                        f"请为每个当前目标填写退化额度（百分点 pp）；{row['name']} 为空，空白不会按零处理。")
                try:
                    delta = float(text.strip())
                except (ValueError, OverflowError) as exc:
                    raise ValueError(f"{row['name']} 退化额度必须是有限非负数 pp。") from exc
                if not math.isfinite(delta) or delta < 0:
                    raise ValueError(f"{row['name']} 退化额度必须是有限非负数 pp；明确填写 0 合法。")
                budgets.append(FilmIdealDegradationBudget(
                    objective_id=row["objective_id"], delta=delta, unit="percentage_point"))
        from coat_sim.core.film_ideal_sensitivity import ideal_sensitivity_request_from_optimization
        return ideal_sensitivity_request_from_optimization(
            base, sensitivity_mode=self._ideal_mode, policy=self._ideal_policy,
            objective_ids=objective_ids, budgets=tuple(budgets),
            sensitivity_layer_ids=selected,
            max_iterations=self._limit(self._ideal_options["max_iterations"], "最大迭代"),
            max_evaluations=self._limit(self._ideal_options["max_evaluations"], "最大评价"),
        )

    def _start_typed(self, workflow):
        if self._busy or self._closed or self.owner.busy:
            return False
        if self._unknown_submission:
            message = f"作业 {self._job_id} 的提交状态未知；请先在队列核对，不会自动重复提交。"
            if workflow == "table":
                self._table_error = message
            else:
                self._ideal_error = message
            self.changed.emit()
            return False
        try:
            base = self.owner._build_request(require_variables=False, fixed_topology=False)
            if self._prepared_signature is None:
                if not self.prepare():
                    raise ValueError(self.error or "来源设计尚未准备。")
            elif self._request_signature(base) != self._prepared_signature:
                raise ValueError("来源设计、目标或材料已变化；请关闭并重新打开此页，复核刷新后的来源再运行。")
            request = (self._build_thickness_error_request(base) if workflow == "table"
                       else self._build_ideal_request(base))
            job_id = uuid4().hex
            compute = self.owner._project_compute
            project_operation = compute.capture(job_id) if compute is not None else None
            metadata = {"summary": ("参数表预期误差分析" if workflow == "table" else "理想敏感度优化"),
                        "origin": {"surface": "desktop"}}
            if compute is not None:
                metadata = compute.metadata(project_operation, metadata)
            context = {"workflow": workflow, "revision": base.design_revision,
                       "epoch": self.owner._source_edit_epoch(), "project_operation": project_operation,
                       "project_compute": compute}
            client = self.client_factory(getattr(self.owner.service, "root", Path.cwd()))
            cancel = RemoteCancelEvent(client, job_id, pending_admission=True)
        except (ValueError, TypeError, KeyError, OverflowError, json.JSONDecodeError) as exc:
            if workflow == "table":
                self._table_error = str(exc)
            else:
                self._ideal_error = str(exc)
            self.changed.emit()
            return False

        self._generation += 1
        display_session, display_source = self.owner.session, self.owner._source_controller
        if workflow == "ideal":
            self.owner._sync_live_selection()
            metadata["film_display_config"] = self.owner._live_display.begin(
                ("sensitivity", self._generation), objectives=request.objectives,
                source_current=lambda: self._display_context_is_current(context, display_session, display_source))
        else:
            self.owner._live_display.clear_analysis()
        self._active_workflow = workflow
        if workflow == "table":
            self._table_payload = None
            self._table_job_id = job_id
            self._table_error = ""
            self._table_status = "正在冻结来源设计、材料、目标网格与参数表后提交独立作业。"
        else:
            self._ideal_payload = None
            self._ideal_job_id = job_id
            self._ideal_error = ""
            self._ideal_status = "正在冻结理想敏感度请求与来源材料后提交独立作业。"
            self._ideal_applied = False
        self._context, self._job_id, self._cancel = context, job_id, cancel
        self._busy = True
        self.changed.emit()
        revision, epoch = context["revision"], context["epoch"]
        session, source = self.owner.session, self.owner._source_controller

        def admission_check():
            project_ok = compute is None or (not compute.editing_frozen and compute.accepts(project_operation))
            source_ok = session is None or (
                session.snapshot().design_revision == revision
                and int(getattr(source, "pendingEditEpoch", 0)) == epoch
            )
            return project_ok and source_ok

        self.pool.start(_SensitivityTask(
            self.owner.service, request, client, job_id, cancel, metadata,
            self._generation, self._signals, self._disconnected, admission_check,
            self.owner._live_display if workflow == "ideal" else None,
        ))
        return True

    @Slot(result=bool)
    def startThicknessErrorTableAnalysis(self):
        return self._start_typed("table")

    @Slot(result=bool)
    def startIdealSensitivityOptimization(self):
        return self._start_typed("ideal")

    @Slot(int)
    def _on_admitted(self, generation):
        if self._closed or generation != self._generation:
            return
        if self._context is None:
            return
        compute = self._context["project_compute"]
        if compute is not None:
            compute.register_job(
                self._job_id,
                module_key="film_optimization",
                state="queued",
                metadata={"search_mode": "sensitivity"},
            )
        workflow = self._context.get("workflow")
        message = "独立敏感性作业已进入共享队列；原设计不变。"
        if workflow == "table":
            self._table_status = message
        elif workflow == "ideal":
            self._ideal_status = message
        self.changed.emit()

    @Slot(int, object)
    def _on_progress(self, generation, value):
        if self._closed or generation != self._generation:
            return
        status = f"{value.get('stage', 'running')} · 评价 {value.get('evaluations', 0)} · {value.get('message', '')}"
        workflow = (self._context or {}).get("workflow")
        if workflow == "table":
            self._table_status = status
        elif workflow == "ideal":
            self._ideal_status = status
        self.changed.emit()

    @Slot(int, object, str)
    def _on_finished(self, generation, payload, error):
        if self._closed or generation != self._generation:
            return
        self._busy = False
        self.owner._live_display.end(("sensitivity", generation))
        self._unknown_submission = error.startswith("submission_unknown:")
        context = self._context or {}
        workflow = context.get("workflow")
        if workflow == "table":
            self._table_error, self._table_payload = error, payload
            self._table_context = dict(context)
            self._table_status = (payload["state"] + " / " + payload["termination_reason"]
                                  if payload is not None else "结果尚不可用；原设计未改变。")
        elif workflow == "ideal":
            self._ideal_error, self._ideal_payload = error, payload
            self._ideal_context = dict(context)
            self._ideal_status = (payload["state"] + " / " + payload["termination_reason"]
                                  if payload is not None else "结果尚不可用；原设计未改变。")
        compute = self._context["project_compute"] if self._context else None
        if compute is not None:
            state = (
                "submission_unknown"
                if self._unknown_submission
                else "failed"
                if error or payload["state"] == "failed"
                else "cancelled"
                if payload["state"] == "cancelled"
                else "succeeded"
            )
            compute.update_job(self._job_id, state)
        self.changed.emit()

    @Slot()
    def cancel(self):
        if self._busy and self._cancel is not None:
            self._cancel.set()
            message = "已请求取消；等待当前完整光学评价结束，不把取消意图当成已取消。"
            workflow = (self._context or {}).get("workflow")
            if workflow == "table":
                self._table_status = message
            elif workflow == "ideal":
                self._ideal_status = message
            self.changed.emit()

    @Slot(result=bool)
    def applySelectedBest(self):
        if self._active_workflow != "ideal" or self._ideal_payload is None:
            return False
        if not self.idealCanApply:
            self._ideal_error = "来源设计、未提交编辑、材料或项目状态已变化；整个候选未应用。"
            self.changed.emit()
            return False
        try:
            from coat_sim.core.film_sensitivity_workflow_jobs import workflow_result_from_payload
            result = workflow_result_from_payload(self._ideal_payload)
            if not self.owner._apply_sensitivity_candidate(result, self._ideal_context):
                raise ValueError("来源设计、未提交编辑或项目已变化；整个敏感度候选未应用。")
            self._ideal_applied = True
            self._ideal_status = "理想敏感度最优候选已显式应用；可在设计窗口撤销。"
            self._ideal_error = ""
        except (ValueError, TypeError, RuntimeError) as exc:
            self._ideal_error = str(exc)
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    @Slot(str, bool, result=bool)
    def saveReport(self, path, overwrite=False):
        workflow = self._active_workflow
        if workflow == "table":
            payload = self._table_payload
        elif workflow == "ideal":
            payload = self._ideal_payload
        if self._busy or payload is None:
            return False
        try:
            target = Path(path).expanduser()
            if target.suffix.lower() != ".json":
                target = target.with_suffix(".json")
            if target.exists() and not overwrite:
                self.overwriteRequested.emit(str(target))
                return False
            atomic_film_json(target, payload, overwrite=overwrite)
            message = "完整敏感性报告已保存：" + str(target)
            if workflow == "table":
                self._table_status, self._table_error = message, ""
            elif workflow == "ideal":
                self._ideal_status, self._ideal_error = message, ""
        except (ValueError, OSError) as exc:
            if workflow == "table":
                self._table_error = str(exc)
            elif workflow == "ideal":
                self._ideal_error = str(exc)
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    @Slot(QUrl, bool, result=bool)
    def saveReportUrl(self, url, overwrite=False):
        if not url.isLocalFile():
            message = "请选择本地 JSON 文件位置。"
            if self._active_workflow == "table":
                self._table_error = message
            elif self._active_workflow == "ideal":
                self._ideal_error = message
            self.changed.emit()
            return False
        return self.saveReport(url.toLocalFile(), overwrite)

    @Slot()
    def sourceChanged(self):
        """End an old source observation without disposing this reusable page."""
        if self._closed:
            return
        generation = self._generation
        self._generation += 1
        self._disconnected.set()
        if self.owner.project_context is None:
            self.cancel()
        # The old task keeps its permanently-set event and its own cancel.
        # New work gets a different gate; late callbacks keep the old generation.
        self._disconnected = threading.Event()
        self._busy = False
        self._cancel = None
        self._context = self._table_context = self._ideal_context = None
        self._prepared_source = self._prepared_signature = None
        self._unknown_submission = False
        self._error = ""
        self.owner._live_display.end(("sensitivity", generation))
        message = "来源已切换；重新准备后可运行。下方旧冻结报告仅供回看，不能应用。"
        self._table_status = self._ideal_status = message
        self.changed.emit()

    @Slot()
    def detach(self):
        self._closed = True
        self._disconnected.set()
        # Project-owned durable jobs outlive this observer; standalone owner
        # closure requests cancellation, but never blocks the GUI thread.
        if self.owner.project_context is None and self._busy:
            self.cancel()

    def shutdown(self, timeout_ms=1000):
        self.detach()
        return self.pool.waitForDone(timeout_ms)
