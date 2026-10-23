"""Independent thickness-error panel using the durable local CPU scheduler.

This is not an ordinary thickness optimizer mode. It captures its own complete
request and never silently changes the source design or nominal acceptance.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QRunnable, QThreadPool, QUrl, Signal, Slot

from coat_sim.core.film_sensitivity import (
    FilmSensitivityScenario,
    FilmThicknessError,
    film_sensitivity_result_from_payload,
    freeze_film_sensitivity_request,
    generate_film_sensitivity_scenarios,
    sensitivity_request_from_optimization,
    validate_film_sensitivity_result_payload,
)
from coat_sim.core.film_thickness_error_table import FilmThicknessErrorParameter
from coat_sim.core.film_thickness_error_table import thickness_error_table_request_from_optimization
from coat_sim.core.film_ideal_sensitivity import ideal_sensitivity_request_from_optimization
from coat_sim.core.film_result_service import atomic_film_json
from coat_sim.core.film_nominal_acceptance import solver_condition_acceptable
from coat_sim.scheduler import SchedulerClient, freeze_spec
from coat_sim.scheduler.desktop import RemoteCancelEvent, observe_job, submit_local_with_receipt
from coat_sim.ui.film_synthesis_quick_adapter import synthesis_strict_upper_bound_indices


def _raw_violation(actual, target, lower, upper):
    return abs(actual - target) if target is not None else max(
        0., 0. if lower is None else lower - actual, 0. if upper is None else actual - upper)


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
    ):
        super().__init__()
        self.service, self.request, self.client = service, request, client
        self.job_id, self.cancel, self.metadata = job_id, cancel, metadata
        self.generation, self.signals = generation, signals
        self.disconnected, self.admission_check = disconnected, admission_check

    def run(self):
        try:
            from coat_sim.core.film_sensitivity_workflow_jobs import (
                freeze_workflow_request, workflow_for_request, validate_workflow_result,
                terminal_reply,
            )
            executor = workflow_for_request(self.request)
            if executor == "film_sensitivity":
                request = freeze_film_sensitivity_request(self.service, self.request)
            else:
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
            try:
                payload = observe_job(
                    self.client,
                    self.job_id,
                    lambda item: self.signals.progress.emit(self.generation, item),
                    disconnected=self.disconnected.is_set,
                )
            except (InterruptedError, RuntimeError):
                row = self.client.status(self.job_id)
                payload = row.get("result")
                if not isinstance(payload, dict) or row.get("state") not in ("failed", "cancelled"):
                    raise
                payload = terminal_reply(payload, row["state"])
            if executor == "film_sensitivity":
                validated = validate_film_sensitivity_result_payload(
                    payload, expected_request=request, replay=True
                )
            else:
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
    layerErrorsChanged = Signal()
    tableRowsChanged = Signal()
    idealRowsChanged = Signal()
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
        self._rows: list[dict[str, Any]] = []
        self._unit = "absolute_nm"
        self._error_model = ""
        self._robustness = "finite_scenario_worst"
        self._optimization_policy = "nominal_preserving"
        self._amplitude_semantics = ""
        self._distribution = ""
        self._sampling = {"sample_count": "", "seed": ""}
        self._scenario_source: dict[str, Any] | None = None
        self._prepared_source: dict[str, Any] | None = None
        self._prepared_signature: Any = None
        self._scenario_kind = ""
        self._scenarios: tuple[FilmSensitivityScenario, ...] = ()
        self._options = {"max_evaluations": "200", "max_iterations": "20"}
        self._status = "请填写工艺误差并显式选择模型、幅度语义与分布；不会推定制造精度或自动加余量。"
        self._error = ""
        self._payload: dict[str, Any] | None = None
        self._context: dict[str, Any] | None = None
        self._legacy_context: dict[str, Any] | None = None
        self._table_context: dict[str, Any] | None = None
        self._ideal_context: dict[str, Any] | None = None
        self._job_id = ""
        self._legacy_job_id = ""
        self._table_job_id = ""
        self._ideal_job_id = ""
        self._cancel: RemoteCancelEvent | None = None
        self._disconnected = threading.Event()
        self._applied = False
        self._ideal_applied = False
        # The ordinary dialog has two independent, explicitly-typed workflows.
        # Keep their drafts and frozen reports separate from the legacy process
        # editor above so switching pages cannot migrate assumptions.
        self._table_rows: list[dict[str, Any]] = []
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
        self._ideal_layer_ids: list[str] | None = None
        self._ideal_selection_explicit = False
        self._ideal_options = {"max_iterations": "", "max_evaluations": ""}
        self._ideal_payload: dict[str, Any] | None = None
        self._ideal_status = "请选择绝对或相对敏感度尺度。"
        self._ideal_error = ""
        self._active_workflow = "legacy"

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(list, notify=layerErrorsChanged)
    def layerErrors(self):
        return deepcopy(self._rows)

    @Property(str, notify=changed)
    def errorUnit(self):
        return self._unit

    @Property(str, notify=changed)
    def errorModel(self):
        return self._error_model

    @Property(str, notify=changed)
    def robustnessObjective(self):
        return self._robustness

    @Property(str, notify=changed)
    def optimizationPolicy(self):
        return self._optimization_policy

    @Property(str, notify=changed)
    def amplitudeSemantics(self):
        return self._amplitude_semantics

    @Property(str, notify=changed)
    def distribution(self):
        return self._distribution

    @Property(dict, notify=changed)
    def sampling(self):
        return dict(self._sampling)

    @Property(str, notify=changed)
    def scenarioDescription(self):
        return self._scenario_description()

    @Property(str, notify=changed)
    def activeWorkflow(self):
        return self._active_workflow

    def _scenario_description(self) -> str:
        if not self._scenarios:
            return "尚无有效场景；配置或来源设计改变后须重新生成。"
        if self._scenario_kind == "axis_probes":
            return f"{len(self._scenarios)} 个明确 ± 轴/共模探针，非随机采样、非全部联合角点。"
        return (f"{len(self._scenarios)} 个显式 {self._distribution} 场景，seed={self._sampling['seed']}；"
                "系数将完整冻结，不代表已校准工艺或良率。")

    @Property(dict, notify=changed)
    def options(self):
        return dict(self._options)

    @Property(int, notify=changed)
    def scenarioCount(self):
        return len(self._scenarios)

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(str, notify=changed)
    def jobId(self):
        if self._active_workflow == "table" and self._table_job_id:
            return self._table_job_id
        if self._active_workflow == "ideal" and self._ideal_job_id:
            return self._ideal_job_id
        if self._active_workflow == "legacy" and self._legacy_job_id:
            return self._legacy_job_id
        return self._job_id

    @Property(str, notify=changed)
    def tableJobId(self):
        return self._table_job_id

    @Property(str, notify=changed)
    def idealJobId(self):
        return self._ideal_job_id

    @Property(bool, notify=changed)
    def hasResult(self):
        return self._payload is not None

    @Property(list, notify=tableRowsChanged)
    def thicknessErrorRows(self):
        return deepcopy(self._table_rows)

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
        return ("理想敏感度优化 · " + str(result.state) + " / " + str(result.termination_reason)
                + f"\n冻结敏感度尺度：{scale or '未读取'}；列 ID：{self._summary_value(selected)}"
                + f"\n加权敏感度平方和目标：{self._summary_value(value)}；初态：{self._summary_value(getattr(baseline, 'objective_value', None))}"
                + f"；原始名义目标通过：{self._summary_value(nominal)}"
                + ("\n存在可审阅候选，但不会自动应用。" if getattr(result, "best_design", None) is not None
                   else "\n没有可应用候选。"))

    def _can_apply_workflow(self, workflow):
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
            if getattr(analysis, "nominal_targets_met", None) is not True:
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
        if type(index) is not int or index not in (0, 1, 2):
            return False
        value = ("table", "ideal", "legacy")[index]
        if value != self._active_workflow:
            self._active_workflow = value
            self.changed.emit()
        return True

    @Property(bool, notify=changed)
    def canApply(self):
        context = self._legacy_context
        return (
            not self._busy
            and not self._applied
            and self._context_is_current(context)
            and self._payload is not None
            and self._payload.get("is_final") is True
            and self._payload["state"] in {"completed", "partial"}
            and self._payload["operation"] == "optimize"
            and self._payload.get("best_design") is not None
            and (self._payload.get("frozen_request", {}).get("optimization_policy", "robustness_only")
                 != "nominal_preserving" or
                 self._payload.get("best_analysis", {}).get("nominal_targets_met") is True)
            and context is not None
            and self.owner.session is not None
        )

    def _context_is_current(self, context):
        if context is None or self.owner.session is None:
            return False
        compute = context.get("project_compute")
        if compute is not None and (compute.editing_frozen or not compute.accepts(context.get("project_operation"))):
            return False
        return (self.owner.session.snapshot().design_revision == context["revision"]
                and self.owner._source_edit_epoch() == context["epoch"])

    @Property(str, notify=changed)
    def resultSummary(self):
        if self._payload is None:
            return ""
        result = film_sensitivity_result_from_payload(self._payload)
        best, baseline = result.best_analysis, result.baseline_analysis
        request = result.frozen_request
        policy = getattr(request, "optimization_policy", "robustness_only")
        semantics = getattr(request, "error_amplitude_semantics", "bounded_amplitude")
        policy_label = "原名义可行域约束" if policy == "nominal_preserving" else "纯降敏，名义指标可能恶化"
        text = (f"策略 {policy}（{policy_label}）；误差尺度 "
                + ("标准差 1σ（非有界幅度）" if semantics == "standard_deviation" else "有界幅度")
                + f"；模型 {request.error_model}\n")
        scales = []
        for error in request.errors:
            absolute = error.absolute_nm + (error.magnitude if error.unit == "absolute_nm" else 0.)
            relative = error.relative_fraction + (error.magnitude if error.unit == "relative_fraction" else 0.)
            scales.append(f"{error.layer_id}: {absolute:g} nm + {relative * 100:g}% × d")
        text += "冻结误差尺度：" + "；".join(scales) + "\n"
        text += (
            f"{result.state} / {result.termination_reason} · "
            f"候选评价 {result.evaluations}，前向 {result.forward_evaluations}，Jacobian {result.jacobian_evaluations}"
        )
        if best is not None:
            raw_worst = max((_raw_violation(row.actual, row.target_value, row.lower_bound, row.upper_bound)
                for row in best.nominal_conditions), default=0.)
            text += (
                f"\n所选误差目标 {result.robustness_objective}: {best.robustness_value:.8g}"
                + (f"（原 {baseline.robustness_value:.8g}）" if baseline else "")
                + f"\n原始最坏违规 {raw_worst * 100:.7g} 个百分点；"
                + f"最大归一化违规 {best.nominal_max_normalized_error:.8g}；"
                + ("名义采样通过" if best.nominal_targets_met else "名义采样未通过")
                + ("；求解容差内可接受" if result.solver_acceptable is True else
                   "；求解容差内不可接受" if result.solver_acceptable is False else "；求解容差状态未知")
                + f"\n一阶敏感度范数 {best.linear_sensitivity_norm:.8g}；完整有限扰动场景 {len(best.finite_scenarios)} 个。"
                + (f"有限场景采样通过 {sum(row.finite_targets_met for row in best.finite_scenarios)}"
                   f"/{len(best.finite_scenarios)}；最坏归一化违规 "
                   f"{max(row.finite_max_normalized_error for row in best.finite_scenarios):.8g}。"
                   if best.finite_scenarios else "")
            )
        if result.termination_reason == "initial_nominal_infeasible":
            text += "\n初态不满足原名义约束：请先恢复名义可行性或更换初态；未暗改目标或增加余量。"
        return text + "\n这是局部搜索，非全局最优证明；线性估计不是有限误差上界，有限场景不是制造良率或连续谱证明。"

    def load_verified_history(self, payload, job_id):
        """Queue loader already replayed physics off the GUI thread.

        The history view has no captured source context and cannot apply.
        """
        from coat_sim.core.film_sensitivity_workflow_jobs import workflow_result_from_payload, workflow_for_request
        result = workflow_result_from_payload(payload)
        workflow = workflow_for_request(result.frozen_request)
        self._job_id = job_id
        self._context = None
        self._legacy_context = self._table_context = self._ideal_context = None
        self._applied = self._ideal_applied = False
        self._active_workflow = ("legacy" if workflow == "film_sensitivity" else
                                 "table" if workflow == "film_thickness_error_table" else "ideal")
        message = "只读历史报告；没有绑定当前设计，不可应用。"
        if workflow == "film_sensitivity":
            film_sensitivity_result_from_payload(payload)
            self._payload = deepcopy(payload)
            self._legacy_job_id = job_id
            self._status = message
            self._error = ""
        elif workflow == "film_thickness_error_table":
            self._table_payload = deepcopy(payload)
            self._table_job_id = job_id
            self._table_context = None
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

    @Property(list, notify=changed)
    def scenarioResults(self):
        if self._payload is None or self._payload.get("best_analysis") is None:
            return []
        result = film_sensitivity_result_from_payload(self._payload)
        assert result.best_analysis is not None
        request = result.frozen_request
        return [
            {
                "scenario_id": row.scenario_id,
                "max_error_percent": 100 * max((_raw_violation(actual, nominal.target_value,
                    nominal.lower_bound, nominal.upper_bound) for nominal, actual in zip(
                        result.best_analysis.nominal_conditions, row.finite_perturbation_values,
                        strict=True)), default=0.),
                "max_normalized_error": row.finite_max_normalized_error,
                "targets_met": row.finite_targets_met,
                "solver_acceptable": all(solver_condition_acceptable(request.objectives[nominal.objective_index],
                    actual, request.acceptable_normalized_error,
                    strict_upper=nominal.objective_index in request.strict_upper_bound_objective_indices)
                    for nominal, actual in zip(result.best_analysis.nominal_conditions,
                        row.finite_perturbation_values, strict=True)),
            }
            for row in result.best_analysis.finite_scenarios
        ]

    @Slot(result=bool)
    def prepare(self):
        if self._busy or self._closed:
            return False
        try:
            base = self.owner._build_request(require_variables=False, fixed_topology=False)
            if self._prepared_source != base.design_snapshot:
                self._scenarios = ()
                self._scenario_source = None
                self._scenario_kind = ""
            self._prepared_source = deepcopy(dict(base.design_snapshot))
            self._prepared_signature = self._request_signature(base)
            previous = {row["layer_id"]: row for row in self._rows}
            self._rows = [
                {
                    "layer_id": row["id"],
                    "label": f"{index + 1}. {row['material_id']} / {row['optical_set_id']}",
                    "nominal": f"{row['thickness']['value']:g} {row['thickness']['unit']}",
                    "magnitude": previous.get(row["id"], {}).get("magnitude", ""),
                    "absolute_nm": previous.get(row["id"], {}).get("absolute_nm", ""),
                    "relative_percent": previous.get(row["id"], {}).get("relative_percent", ""),
                    "enabled": previous.get(row["id"], {}).get("enabled", True),
                }
                for index, row in enumerate(base.design_snapshot["front_layers"])
            ]
            previous_table = {row["layer_id"]: row for row in self._table_rows}
            self._table_rows = [
                {
                    "layer_id": row["id"],
                    "layer_label": f"{index + 1}. {row['material_id']} / {row['optical_set_id']}",
                    "nominal": f"{row['thickness']['value']:g} {row['thickness']['unit']}",
                    "enabled": previous_table.get(row["id"], {}).get("enabled", True),
                    "bias_text": previous_table.get(row["id"], {}).get("bias_text", ""),
                    "bias_unit": previous_table.get(row["id"], {}).get("bias_unit", ""),
                    "uncertainty_kind": previous_table.get(row["id"], {}).get("uncertainty_kind", ""),
                    "uncertainty_text": previous_table.get(row["id"], {}).get("uncertainty_text", ""),
                    "uncertainty_unit": previous_table.get(row["id"], {}).get("uncertainty_unit", ""),
                }
                for index, row in enumerate(base.design_snapshot["front_layers"])
            ]
            self._refresh_table_correlation_identity_error()
            self._ideal_layer_rows = [
                {"layer_id": row["id"],
                 "layer_label": f"{index + 1}. {row['material_id']} / {row['optical_set_id']}",
                 "nominal": f"{row['thickness']['value']:g} {row['thickness']['unit']}"}
                for index, row in enumerate(base.design_snapshot["front_layers"])
            ]
            current_ids = [row["layer_id"] for row in self._ideal_layer_rows]
            if self._ideal_layer_ids is None or not self._ideal_selection_explicit:
                self._ideal_layer_ids = list(current_ids)
            else:
                current_set = set(current_ids)
                self._ideal_layer_ids = [row_id for row_id in current_ids
                                         if row_id in current_set and row_id in self._ideal_layer_ids]
            self.layerErrorsChanged.emit()
            self.tableRowsChanged.emit()
            self.idealRowsChanged.emit()
            if not self._rows:
                raise ValueError("敏感性分析需要已有膜层；先完成自动合成或添加真实膜系。")
            self._error = ""
            self.changed.emit()
            return True
        except (ValueError, TypeError, KeyError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False

    @staticmethod
    def _request_signature(base):
        return deepcopy((base.design_revision, base.design_snapshot, base.objectives, base.variables,
                         base.frozen_material_snapshot, base.acceptable_normalized_error))

    def _edit(self):
        self._error = ""
        if self._payload is not None:
            self._status = "配置已编辑；下方仍为上一次冻结请求的报告，不代表当前草稿。需重新生成场景后运行。"
        self._scenarios = ()
        self._scenario_source = None
        self._scenario_kind = ""
        self.changed.emit()

    def _typed_edit(self, workflow):
        if workflow == "table":
            self._table_error = ""
            self._table_status = ("参数已编辑；下方仍保留上次冻结结果，不代表当前草稿。"
                                 if self._table_payload is not None else "填写参数表后运行；未填写不等于零。")
        else:
            self._ideal_error = ""
            self._ideal_status = ("配置已编辑；下方仍保留上次冻结结果，不代表当前草稿。"
                                  if self._ideal_payload is not None else "请选择绝对或相对敏感度尺度。")
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

    @Slot(str, str, result=bool)
    def setChoice(self, name, value):
        if self._busy:
            return False
        choices = {
            "unit": ("absolute_nm", "relative_fraction"),
            "model": ("independent", "common_mode", "material_common_mode", "linear_drift"),
            "policy": ("robustness_only", "nominal_preserving"),
            "semantics": ("bounded_amplitude", "standard_deviation"),
            "distribution": ("bounded_uniform", "gaussian"),
            "objective": (
                "linear_sensitivity_norm",
                "finite_scenario_worst",
                "finite_scenario_expected",
            ),
        }
        if not isinstance(name, str) or not isinstance(value, str) or name not in choices or value not in choices[name]:
            return False
        if name == "unit":
            self._unit = value
        elif name == "model":
            self._error_model = value
        elif name == "objective":
            self._robustness = value
        elif name == "policy":
            self._optimization_policy = value
        elif name == "semantics":
            self._amplitude_semantics = value
        else:
            self._distribution = value
        self._edit()
        return True

    @Slot(str, str, result=bool)
    def setMagnitude(self, layer_id, value):
        if self._busy or not isinstance(layer_id, str) or not isinstance(value, str):
            return False
        row = next((item for item in self._rows if item["layer_id"] == layer_id), None)
        if row is None:
            return False
        row["magnitude"] = value
        self._edit()
        self.layerErrorsChanged.emit()
        return True

    def _set_component(self, layer_id, component, value, *, notify_rows):
        if (self._busy or not isinstance(layer_id, str) or not isinstance(value, str)
                or component not in ("absolute_nm", "relative_percent")):
            return False
        row = next((item for item in self._rows if item["layer_id"] == layer_id), None)
        if row is None:
            return False
        row[component] = value
        self._edit()
        if notify_rows:
            self.layerErrorsChanged.emit()
        return True

    @Slot(str, str, str, result=bool)
    def setErrorComponent(self, layer_id, component, value):
        return self._set_component(layer_id, component, value, notify_rows=True)

    @Slot(str, str, str, result=bool)
    def setErrorDraft(self, layer_id, component, value):
        # Text changes are captured before focus loss. Do not rebuild a Repeater
        # during typing: that would drop IME/selection/focus and partial input.
        return self._set_component(layer_id, component, value, notify_rows=False)

    @Slot(str, str, result=bool)
    def setSamplingOption(self, name, value):
        if self._busy or not isinstance(name, str) or name not in self._sampling or not isinstance(value, str):
            return False
        self._sampling[name] = value
        self._edit()
        return True

    @Slot(str, bool, result=bool)
    def setLayerEnabled(self, layer_id, enabled):
        if self._busy or not isinstance(layer_id, str) or type(enabled) is not bool:
            return False
        row = next((item for item in self._rows if item["layer_id"] == layer_id), None)
        if row is None:
            return False
        row["enabled"] = bool(enabled)
        self._edit()
        return True

    @Slot(str, str, result=bool)
    def setOption(self, name, value):
        if self._busy or not isinstance(name, str) or name not in self._options or not isinstance(value, str):
            return False
        self._options[name] = value
        self._edit()
        return True

    @staticmethod
    def _nonnegative(value, label, *, optional=False):
        if not isinstance(value, str):
            raise ValueError(f"{label} 必须是数字文本。")
        if not value.strip():
            if optional:
                return 0.
            raise ValueError(f"请填写 {label}，不能推定工艺误差。")
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError(f"{label} 必须为有限非负数。")
        return result

    def _errors(self):
        selected = [row for row in self._rows if row["enabled"]]
        if not selected:
            raise ValueError("至少选择一个误差膜层。")
        errors = []
        for row in selected:
            values = (row["magnitude"], row["absolute_nm"], row["relative_percent"])
            if not any(isinstance(value, str) and value.strip() for value in values):
                raise ValueError(f"请填写 {row['label']} 的工艺绝对 nm 或相对 % 误差；无校准默认值。")
            magnitude, absolute, relative = (
                self._nonnegative(value, label, optional=True)
                for value, label in zip(values, ("兼容误差幅度", "绝对 nm 误差", "相对 % 误差"), strict=True)
            )
            errors.append(FilmThicknessError(
                row["layer_id"], magnitude / 100. if self._unit == "relative_fraction" else magnitude,
                self._unit, absolute_nm=absolute, relative_fraction=relative / 100.,
            ))
        if not any(error.magnitude or error.absolute_nm or error.relative_fraction for error in errors):
            raise ValueError("至少需要一个明确非零工艺误差。")
        return tuple(errors)

    def _check_process_choices(self, *, sampling=False):
        if self._error_model not in ("independent", "common_mode", "material_common_mode", "linear_drift"):
            raise ValueError("请显式选择工艺误差模型。")
        if self._amplitude_semantics not in ("bounded_amplitude", "standard_deviation"):
            raise ValueError("请显式选择误差尺度是有界幅度还是标准差 1σ。")
        if sampling:
            expected = {"bounded_uniform": "bounded_amplitude", "gaussian": "standard_deviation"}
            if self._distribution not in expected:
                raise ValueError("请显式选择场景分布；不推定制造分布。")
            if expected[self._distribution] != self._amplitude_semantics:
                raise ValueError("有界均匀需选择有界幅度；高斯需选择标准差 1σ，系数不裁剪。")

    @Slot(result=bool)
    def generateProcessScenarios(self):
        if self._busy or self._closed:
            return False
        try:
            base = self.owner._build_request(require_variables=False, fixed_topology=False)
            errors = self._errors()
            self._check_process_choices(sampling=True)
            count = self._limit(self._sampling["sample_count"], "场景样本数")
            if count is None:
                raise ValueError("请填写场景样本数。")
            seed = self._integer(self._sampling["seed"], "随机 seed", minimum=0)
            scenarios = generate_film_sensitivity_scenarios(
                base.design_snapshot, errors, sample_count=count, seed=seed,
                error_model=self._error_model, distribution=self._distribution,
            )
            self._scenarios = scenarios
            self._scenario_source = deepcopy(dict(base.design_snapshot))
            self._scenario_kind = "process_samples"
            self._status = self._scenario_description()
            self._error = ""
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._scenarios = ()
            self._scenario_source = None
            self._scenario_kind = ""
            self._error = str(exc)
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    @Slot(result=bool)
    def generateScenarios(self):
        """Compatibility entry: explicit axis probes, never process Monte Carlo."""
        if self._busy or self._closed:
            return False
        try:
            base = self.owner._build_request(require_variables=False, fixed_topology=False)
            count = len(self._errors())
            self._check_process_choices()
            if self._amplitude_semantics != "bounded_amplitude":
                raise ValueError("旧 ± 轴探针仅用于有界幅度；1σ请使用显式高斯场景生成。")
            if self._error_model == "common_mode":
                scenarios = tuple(FilmSensitivityScenario(f"common_{sign:+d}", (float(sign),) * count)
                                  for sign in (-1, 1))
            elif self._error_model == "independent":
                scenarios = tuple(FilmSensitivityScenario(f"layer_{axis + 1}_{sign:+d}",
                    tuple(float(sign) if i == axis else 0. for i in range(count)))
                    for axis in range(count) for sign in (-1, 1))
            else:
                raise ValueError("旧 ± 轴探针仅支持逐层独立/全部共模；请使用显式工艺场景生成。")
            self._scenarios = scenarios
            self._scenario_source = deepcopy(dict(base.design_snapshot))
            self._scenario_kind = "axis_probes"
            self._status = self._scenario_description()
            self._error = ""
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    @staticmethod
    def _integer(value, name, *, minimum):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"请填写 {name} 的整数值。")
        result = int(value)
        if str(result) != value.strip() or result < minimum:
            raise ValueError(f"{name} 必须是大于等于 {minimum} 的整数。")
        return result

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
            strict_upper_bound_objective_indices=synthesis_strict_upper_bound_indices(
                getattr(self.owner, "_objectives", ())),
        )

    def _build_ideal_request(self, base):
        if self._ideal_mode not in ("absolute", "relative"):
            raise ValueError("请选择绝对或相对敏感度尺度；不推定工艺意义或默认单位。")
        selected = tuple(row["layer_id"] for row in self._ideal_layer_rows
                         if row["layer_id"] in (self._ideal_layer_ids or ()))
        if not selected:
            raise ValueError("至少选择一个理想敏感度厚度列。")
        return ideal_sensitivity_request_from_optimization(
            base, sensitivity_mode=self._ideal_mode, sensitivity_layer_ids=selected,
            max_iterations=self._limit(self._ideal_options["max_iterations"], "最大迭代"),
            max_evaluations=self._limit(self._ideal_options["max_evaluations"], "最大评价"),
            strict_lower_bound_objective_indices=(),
            strict_upper_bound_objective_indices=synthesis_strict_upper_bound_indices(
                getattr(self.owner, "_objectives", ())),
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
        ))
        return True

    @Slot(result=bool)
    def startThicknessErrorTableAnalysis(self):
        return self._start_typed("table")

    @Slot(result=bool)
    def startIdealSensitivityOptimization(self):
        return self._start_typed("ideal")

    @Slot(str, result=bool)
    def start(self, operation):
        if self._busy or self._closed or self.owner.busy:
            return False
        if self._unknown_submission:
            self._error = f"作业 {self._job_id} 的提交状态未知；请先在队列核对，不会自动重复提交。"
            self.changed.emit()
            return False
        try:
            base = self.owner._build_request(require_variables=False, fixed_topology=False)
            if operation not in ("analyze", "optimize"):
                raise ValueError("请选择分析或优化操作。")
            errors = self._errors()
            self._check_process_choices()
            if (operation == "optimize" and self._optimization_policy == "nominal_preserving"
                    and self._robustness == "linear_sensitivity_norm"):
                raise ValueError("保名义优化只支持有限场景最坏/加权 RMS；一阶敏感度可独立分析，或显式选择纯降敏。")
            if self._scenarios and self._scenario_source != base.design_snapshot:
                self._scenarios = ()
                self._scenario_source = None
                self._scenario_kind = ""
                raise ValueError("来源设计已变化；原场景已失效，请重新生成明确场景。")
            if self._robustness != "linear_sensitivity_norm" and not self._scenarios:
                raise ValueError("有限场景目标需要明确场景；请填写工艺配置并生成场景。")
            request = sensitivity_request_from_optimization(
                base,
                errors=tuple(errors),
                error_model=self._error_model,
                optimization_policy=self._optimization_policy,
                error_amplitude_semantics=self._amplitude_semantics,
                robustness_objective=self._robustness,
                scenarios=self._scenarios,
                operation=operation,
                max_evaluations=self._limit(self._options["max_evaluations"], "最大候选评价"),
                max_iterations=self._limit(self._options["max_iterations"], "最大迭代"),
                strict_upper_bound_objective_indices=synthesis_strict_upper_bound_indices(
                    getattr(self.owner, "_objectives", ())),
            )
            job_id = uuid4().hex
            compute = self.owner._project_compute
            project_operation = compute.capture(job_id) if compute is not None else None
            metadata = {"summary": "膜厚敏感性 " + operation, "origin": {"surface": "desktop"}}
            if self._scenario_kind == "process_samples":
                metadata["process_scenario_generation"] = {
                    "distribution": self._distribution,
                    "sample_count": len(self._scenarios),
                    "seed": self._integer(self._sampling["seed"], "随机 seed", minimum=0),
                    "note": "Generation provenance only; frozen request coefficients are authoritative.",
                }
            if compute is not None:
                metadata = compute.metadata(project_operation, metadata)
            context = {
                "workflow": "legacy",
                "revision": base.design_revision,
                "epoch": self.owner._source_edit_epoch(),
                "project_operation": project_operation,
                "project_compute": compute,
            }
            client = self.client_factory(getattr(self.owner.service, "root", Path.cwd()))
            cancel = RemoteCancelEvent(client, job_id, pending_admission=True)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self._generation += 1
        self._active_workflow = "legacy"
        self._legacy_job_id = job_id
        self._context, self._job_id, self._cancel = context, job_id, cancel
        self._payload, self._applied = None, False
        self._busy, self._error = True, ""
        self._status = "正在冻结原网格/材料并提交独立本地 CPU 作业。"
        self.changed.emit()
        revision, epoch = context["revision"], context["epoch"]
        session, source = self.owner.session, self.owner._source_controller

        def admission_check():
            project_ok = compute is None or (
                not compute.editing_frozen and compute.accepts(project_operation)
            )
            source_ok = session is None or (
                session.snapshot().design_revision == revision
                and int(getattr(source, "pendingEditEpoch", 0)) == epoch
            )
            return project_ok and source_ok

        self.pool.start(
            _SensitivityTask(
                self.owner.service,
                request,
                client,
                job_id,
                cancel,
                metadata,
                self._generation,
                self._signals,
                self._disconnected,
                admission_check,
            )
        )
        return True

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
        workflow = self._context.get("workflow", "legacy")
        message = "独立敏感性作业已进入共享队列；原设计不变。"
        if workflow == "table":
            self._table_status = message
        elif workflow == "ideal":
            self._ideal_status = message
        else:
            self._status = message
        self.changed.emit()

    @Slot(int, object)
    def _on_progress(self, generation, value):
        if self._closed or generation != self._generation:
            return
        status = f"{value.get('stage', 'running')} · 评价 {value.get('evaluations', 0)} · {value.get('message', '')}"
        workflow = (self._context or {}).get("workflow", "legacy")
        if workflow == "table":
            self._table_status = status
        elif workflow == "ideal":
            self._ideal_status = status
        else:
            self._status = status
        self.changed.emit()

    @Slot(int, object, str)
    def _on_finished(self, generation, payload, error):
        if self._closed or generation != self._generation:
            return
        self._busy = False
        self._unknown_submission = error.startswith("submission_unknown:")
        context = self._context or {}
        workflow = context.get("workflow", "legacy")
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
        else:
            self._error, self._payload = error, payload
            self._legacy_context = dict(context)
            self._status = (payload["state"] + " / " + payload["termination_reason"]
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
            workflow = (self._context or {}).get("workflow", "legacy")
            if workflow == "table":
                self._table_status = message
            elif workflow == "ideal":
                self._ideal_status = message
            else:
                self._status = message
            self.changed.emit()

    @Slot(result=bool)
    def applyBest(self):
        context = self._legacy_context
        if self._busy or self._payload is None or context is None:
            return False
        if not self.canApply:
            self._error = "来源设计、未提交编辑、材料或项目状态已变化；整个候选未应用。"
            self.changed.emit()
            return False
        try:
            result = film_sensitivity_result_from_payload(self._payload)
            accepted = self.owner._apply_sensitivity_candidate(result, context)
            if not accepted:
                raise ValueError("来源设计、未提交编辑或项目已变化；整个候选未应用。")
            self._applied = True
            self._status = "敏感性最优候选已显式应用；可在设计窗口撤销。"
            self._error = ""
        except (ValueError, TypeError, RuntimeError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    @Slot(result=bool)
    def applySelectedBest(self):
        if self._active_workflow == "legacy":
            return self.applyBest()
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
        else:
            payload = self._payload
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
            else:
                self._status, self._error = message, ""
        except (ValueError, OSError) as exc:
            if workflow == "table":
                self._table_error = str(exc)
            elif workflow == "ideal":
                self._ideal_error = str(exc)
            else:
                self._error = str(exc)
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
            else:
                self._error = message
            self.changed.emit()
            return False
        return self.saveReport(url.toLocalFile(), overwrite)

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
