"""Automatic repair UI and read-only legacy A/B compatibility over the queue.

Only this controller owns drafts/report selection. Numerical execution and
physical replay stay in the existing job worker, never on the Qt UI thread.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from .film_display_format import format_film_thickness
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import sys
import threading
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QRunnable, QUrl, Signal, Slot

from coat_sim.scheduler import SchedulerClient, freeze_spec
from coat_sim.scheduler.desktop import RemoteCancelEvent, observe_job, submit_local_with_receipt
from coat_sim.ui.film_manufacturing_quick_adapter import (
    comparison_rows, comparison_scope_text, layer_change_rows, layer_total_nm,
)
from coat_sim.ui.film_optimization_run_coordinator import FilmOptimizationRunCoordinator


_EXECUTOR = "film_manufacturing_repair"
_MAX_FINITE_NUMERIC_INPUT = Decimal.from_float(sys.float_info.max)
_ROUTES = ("raise_selected", "delete_selected_merge", "automatic")
_LEGACY_LABELS = {"reference_nm": "参考厚度 nm", "refinement_evaluations_per_route": "每路精修评价配额",
                  "max_iterations_per_route": "每路最大迭代", "seconds_per_route": "每路秒数配额"}
_OPTION_LABELS = {'reference_nm': '单层目标厚度（nm）', 'thickness_tolerance_nm': '厚度容差（nm）',
                  'optical_tolerance_percentage_points': '光学指标容差（百分点）'}


def _automatic_terminal_summary(payload):
    body = payload.get('result') or {}
    reason = str(payload.get('termination_reason', ''))
    if payload.get("schema_version") == 3:
        if payload.get("state") == "cancelled":
            return "本轮已停止；保留最后完整核验的改进，取消写回仍须通过当前来源保护。"
        if payload.get("state") == "failed":
            return "本轮未正常完成；保留已完成证据，未自动写回。"
        ending = {"thickness_goal_reached": "保留层厚度下限已达到",
                  "thickness_goal_reached_original_targets_unmet": "厚度下限已达到，原光学目标仍未全部通过",
                  "no_thin_layers": "当前没有需要修复的薄层"}.get(reason, "本轮自然结束")
        return ending + ("；已有可应用阶段改进，等待手动确认。" if body.get("best") is not None else
                         "；没有严格改善且通过光学保护的候选，原设计未改变。")
    if payload.get('state') == 'cancelled':
        return '本轮已取消；保留已经完成的证据供回看，未应用。'
    if payload.get('state') == 'failed':
        return '本轮修复未完成；保留可用证据供回看，原设计未改变。'
    if reason == 'no_thin_layers':
        return '当前没有低于单层接受下限的薄层，无需修复。'
    ending = {'search_quantum_complete': '本轮结构比较完成',
              'proposal_quantum_limit': '本轮结构搜索结束',
              'evaluation_limit': '本轮计算预算已用完',
              'time_limit': '本轮时间已用完'}.get(reason, '本轮已结束')
    if body.get('best') is not None:
        return ending + '；已保留通过厚度、光学变化及适用原目标联合验收的结果，未自动应用。'
    return ending + '；本轮尚未找到通过联合验收的候选，不表示不存在可行修复。'


class _Signals(QObject):
    progress = Signal(int, object)
    jobIdChanged = Signal(int, str)
    finished = Signal(int, object, object, object)


class _ManufacturingTask(QRunnable):
    def __init__(self, operation_id, service, synthesis, source, options, selected,
                 client, job_id, metadata, admission_check):
        super().__init__()
        self.operation_id, self.service, self.synthesis = operation_id, service, synthesis
        self.source, self.options, self.selected = source, options, selected
        self.client, self.job_id, self.metadata = client, job_id, metadata
        self.admission_check = admission_check
        self.cancel_event = RemoteCancelEvent(client, job_id, pending_admission=True)
        self.signals = _Signals()
        self.disconnected = threading.Event()

    def detach_observer(self):
        self.disconnected.set()

    def _check(self):
        if self.cancel_event.is_set() or self.disconnected.is_set() or not self.admission_check():
            raise InterruptedError("已取消或来源/项目改变；不会继续提交或应用候选。")

    def run(self):
        try:
            from coat_sim.core import film_manufacturing_jobs as jobs
            from coat_sim.core.film_physical_generator import freeze_synthesis_editable_snapshot
            from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload

            self._check()
            from coat_sim.core.film_automatic_repair_jobs import make_live_request_payload as automatic_request
            make_request = automatic_request if 'thickness_tolerance_nm' in self.options else jobs.make_request_payload
            selection = {} if make_request is automatic_request else {'defect_layer_ids': self.selected}
            request = make_request(
                self.service, self.synthesis, self.source, request_id=self.job_id, **selection, **self.options)
            _, frozen_synthesis, _ = film_synthesis_request_from_payload(request["synthesis"])
            closure = freeze_synthesis_editable_snapshot(
                self.service, self.source, frozen_synthesis.frozen_material_snapshot)
            spec = freeze_spec(None, _EXECUTOR, {"payload": request})
            from coat_sim.scheduler.jobs import JobSpec
            input_digest = JobSpec.parse_frozen(spec).fingerprint
            job = spec["parameters"]["payload"]
            prepared = {"frozen_request": job.get("request", request), "frozen_job": job, "synthesis_request": frozen_synthesis,
                        "editable_snapshot": closure}
            self.signals.progress.emit(self.operation_id, {"stage": "repair_prepared", "prepared": prepared})
            self._check()
            if _EXECUTOR not in self.client.capabilities().get("executors", ()):
                raise ValueError("当前本地调度器不支持工艺薄层修复；请正常关闭并重启应用后重试。")
            self._check()
            submit_local_with_receipt(self.client, spec, job_id=self.job_id, metadata=self.metadata)
            self.cancel_event.mark_admitted()
            self.signals.jobIdChanged.emit(self.operation_id, self.job_id)
            last_sequence = -1

            def observe_progress(item):
                nonlocal last_sequence
                if isinstance(item, dict) and item.get("stage") == "repair_live":
                    from coat_sim.core.film_automatic_repair_jobs import validate_live_progress
                    try:
                        if "repair_live_sequence" in item:
                            item = self.client.repair_live(self.job_id, input_digest=input_digest,
                                                          after_sequence=last_sequence)
                            if item is None:
                                return
                        if type(item.get("seq")) is int and item["seq"] <= last_sequence:
                            return
                        item = validate_live_progress(item, expected_request=prepared["frozen_request"], after_seq=last_sequence)
                    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
                        self.signals.progress.emit(self.operation_id,
                            {"stage": "repair_live_rejected", "message": str(exc)})
                        return
                    last_sequence = item["seq"]
                self.signals.progress.emit(self.operation_id, item)

            try:
                payload = observe_job(
                    self.client, self.job_id, observe_progress,
                    disconnected=self.disconnected.is_set)
            except (InterruptedError, RuntimeError):
                row = self.client.status(self.job_id)
                payload = row.get("result")
                if not isinstance(payload, dict) or row.get("state") not in ("failed", "cancelled"):
                    raise
                payload = jobs.terminal_reply(payload, row["state"])
            if not jobs.result_matches_job(payload, job):
                raise ValueError("工艺修复报告未绑定此冻结来源、目标与作业，结果已拒绝。")
            payload = jobs.validate_result_payload(payload, expected_job=job, replay=False)
            self.signals.finished.emit(self.operation_id, payload,
                                       dict(prepared, comparison_rows=comparison_rows(payload)), None)
        except Exception as exc:
            from coat_sim.scheduler.remote_client import RemoteSubmissionUnknown
            prefix = "submission_unknown: " if isinstance(exc, RemoteSubmissionUnknown) else ""
            self.signals.finished.emit(self.operation_id, None, None,
                                       prefix + f"{type(exc).__name__}: {exc}")


class FilmManufacturingQuickController(QObject):
    changed = Signal()
    layerRowsChanged = Signal()
    overwriteRequested = Signal(str)

    def __init__(self, owner: Any, *, client_factory=SchedulerClient):
        super().__init__(owner)
        self.owner, self.client_factory = owner, client_factory
        self._run_coordinator = FilmOptimizationRunCoordinator(
            self, service_owned=getattr(owner, "project_context", None) is not None)
        self._run_coordinator.progress.connect(self._on_progress)
        self._run_coordinator.jobIdChanged.connect(self._on_admitted)
        self._run_coordinator.finished.connect(self._on_finished)
        self._rows: list[dict] = []
        self._options = dict.fromkeys(_OPTION_LABELS, "")
        self._legacy_options = dict.fromkeys(_LEGACY_LABELS, '')
        self._legacy_mode = False  # Old programmatic/manual jobs only; never selected by the new QML.
        self._has_original_targets = True
        self._comparison_settings: dict[str, Any] | None = None
        self._prepared_signature = None
        self._context: dict | None = None
        self._payload: dict | None = None
        self._live_frame: dict | None = None
        self._live_seq = -1
        self._live_bar_scale_nm = 0.0
        self._stop_reason = ""
        self._auto_apply_attempted = False
        self._apply_in_progress = False
        self._pending_application: dict | None = None
        self._applied_snapshot: dict | None = None
        self._applied_payload: dict | None = None
        self._applied_source_revision: int | None = None
        self._completed_operation_id: int | None = None
        self._eligibility_cache: dict | None = None
        self._comparison_rows: list[dict] = []
        self._job_id = ""
        self._error = ""
        self._status = "填写三个参数后自动识别薄层；实时显示当前计算和已核验改进。用户取消时应用当前改进，无改进则保持原设计。"
        self._route = ""
        self._closed = self._stale = self._cancel_intent = self._applied = False
        self._unknown_submission = False
        self._draft_epoch = 0
        self._source_invalidated = threading.Event()
        from .theme_service import ThemeService
        ThemeService.instance().filmThicknessDecimalsChanged.connect(self._on_film_display_precision)

    @Slot(int)
    def _on_film_display_precision(self, _decimals: int) -> None:
        self.layerRowsChanged.emit()
        self.changed.emit()

    @Property(bool, notify=changed)
    def busy(self):
        return self._run_coordinator.busy

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(str, notify=changed)
    def statusText(self):
        return self._status

    @Property(str, notify=changed)
    def jobId(self):
        return self._job_id

    @Property(list, notify=layerRowsChanged)
    def layerRows(self):
        return [dict(row, thickness=f"{format_film_thickness(row['thickness_value'])} {row['thickness_unit']}")
                for row in self._rows]

    @Property(dict, notify=changed)
    def options(self):
        return dict(self._legacy_options if self._legacy_mode else self._options)

    @Property(dict, notify=changed)
    def automaticOptions(self):
        """Parameters exposed by the shared optimization algorithm selector.

        The worker request historically encoded its floor as target minus
        tolerance. This selector intentionally exposes the actual accepted
        lower bound; the setter stores it losslessly as target with zero
        tolerance so the existing repair executor receives the same floor.
        """
        reference = self._options.get("reference_nm", "")
        tolerance = self._options.get("thickness_tolerance_nm", "")
        floor = reference
        if reference.strip() and tolerance.strip():
            try:
                target, delta = float(reference), float(tolerance)
                if math.isfinite(target) and math.isfinite(delta):
                    floor = reference if delta == 0 else format_film_thickness(max(0.0, target - delta))
            except (TypeError, ValueError, OverflowError):
                pass  # Preserve invalid in-progress text instead of replacing it.
        return {"lower_limit_nm": floor,
                "optical_tolerance_percentage_points": self._options.get(
                    "optical_tolerance_percentage_points", "")}

    @Property(bool, notify=changed)
    def canStartAutomatic(self):
        if (self.busy or self._closed or self._unknown_submission or self.owner.busy
                or self.owner._shutting_down or self.owner._source_closed or self.owner._source_stale
                or self._stale or self._prepared_signature is None or not self._rows):
            return False
        # This event fences the previous report/attempt after a source change.
        # A successful idle prepare has already refreshed the current signature;
        # keeping the old event in this readiness gate would permanently disable
        # a reused controller. start() installs a fresh event before submission,
        # while the old event remains set for any prior result/context.
        try:
            self._validated_options()
            return True
        except (ValueError, TypeError, OverflowError):
            return False

    @Property(str, notify=changed)
    def comparisonScopeSummary(self):
        pairing = ("按当前启用目标的波段、角度、偏振和 R/T 配对。" if self._has_original_targets else
                   "没有启用目标，按当前仿真配置比较 "
                   + comparison_scope_text(self._comparison_settings) + "。")
        return ("光学容差逐点比较本轮开始时原曲线与修复后的对应曲线，不用均值抵消。" + pairing
                + "原目标达标状态另列；须守住原光学保护，已有违规不冒充达标，曲线变化容差不放宽。")

    @Property(str, notify=changed)
    def selectedRoute(self):
        return self._route

    @Property(bool, notify=changed)
    def hasResult(self):
        return self._payload is not None

    @Property(bool, notify=changed)
    def hasLivePreview(self):
        return self._live_frame is not None or (self._payload or {}).get("schema_version") == 3

    def _application_payload(self):
        if self._applied_payload is not None:
            return self._applied_payload
        if self._pending_application is not None:
            return self._pending_application["payload"]
        return self._payload if self._payload is not None else self._live_frame

    def _application_source(self, payload=None):
        payload = self._application_payload() if payload is None else payload
        if not isinstance(payload, dict):
            return None
        return payload.get("source") if payload.get("stage") == "repair_live" else (payload.get("result") or {}).get("source")

    def _application_request(self, payload=None):
        payload = self._application_payload() if payload is None else payload
        if isinstance(payload, dict) and payload.get("stage") != "repair_live":
            return payload.get("request")
        return (self._context or {}).get("frozen_request")

    def _live_display_snapshots(self):
        payload = self._application_payload()
        source = self._application_source(payload)
        if source is None:
            request = (self._context or {}).get("frozen_request") or {}
            source = {"design": request.get("source_design", self.owner._design)}
        best = self._selected_snapshot()
        sequence = payload.get("seq", self._live_seq) if isinstance(payload, Mapping) else self._live_seq
        if self._applied_snapshot is not None:
            return source, self._applied_snapshot, self._applied_snapshot, "applied", sequence
        if isinstance(payload, Mapping) and payload.get("schema_version") == 3 and payload.get("stage") != "repair_live":
            return source, best if best is not None else source, best, "terminal_best" if best is not None else "source_unchanged", sequence
        current = (payload or {}).get("current")
        if current is None:
            current = ((self._payload or {}).get("result") or {}).get("reviewed")
        mode = "frozen_preview" if self._stale or self._source_invalidated.is_set() else "preview"
        return source, current, best, mode, sequence

    def _expand_live_bar_scale(self):
        source, current, best, _mode, _seq = self._live_display_snapshots()
        rows = layer_change_rows(source, current, best)
        self._live_bar_scale_nm = max(self._live_bar_scale_nm, *(float(row[key])
            for row in rows for key in ("original_nm", "current_nm", "best_nm")
            if isinstance(row[key], (float, int)) and math.isfinite(row[key])), 0.0)

    @Property(float, notify=changed)
    def liveBarScaleNm(self):
        return self._live_bar_scale_nm

    @Property(list, notify=changed)
    def liveLayerRows(self):
        source, current, best, _mode, _seq = self._live_display_snapshots()
        return layer_change_rows(source, current, best, bar_scale_nm=self._live_bar_scale_nm)

    @Property(dict, notify=changed)
    def liveTotals(self):
        source, current, best, mode, sequence = self._live_display_snapshots()
        source_nm = layer_total_nm(source)
        current_nm, best_nm = layer_total_nm(current), layer_total_nm(best)
        def layer_count(snapshot):
            if not isinstance(snapshot, Mapping):
                return None
            layers = snapshot.get("layers")
            if isinstance(layers, list):
                return len(layers)
            design = snapshot.get("design")
            if isinstance(design, Mapping):
                return sum(len(design.get(side + "_layers", ())) for side in ("front", "back"))
            return None
        def show(value):
            return "未知" if value is None else format_film_thickness(value) + " nm"
        def identity(snapshot):
            return "" if snapshot is None else str(snapshot.get("physical_hash", ""))
        current_label = {"applied": "已应用候选总厚度（回看）", "terminal_best": "本轮最佳候选总厚度",
                         "source_unchanged": "原设计总厚度", "frozen_preview": "冻结试探总厚度（非当前编辑）"}.get(mode, "当前试探总厚度")
        if mode in {"terminal_best", "source_unchanged"} and (self._stale or self._source_invalidated.is_set()):
            current_label += "（冻结回看，非当前编辑）"
        return dict(original_nm=source_nm, current_nm=current_nm, best_nm=best_nm,
            original=show(source_nm), current=show(current_nm), best=show(best_nm),
            original_layers=layer_count(source), current_layers=layer_count(current),
            best_layers=layer_count(best),
            current_hash=identity(current), best_hash=identity(best), seq=sequence, mode=mode,
            current_label=current_label,
            scale=format_film_thickness(self._live_bar_scale_nm) + " nm")

    @Property(str, notify=changed)
    def liveSummary(self):
        if self._payload is not None and self._context is None:
            return "只读冻结报告；未绑定当前设计，不能应用。"
        candidate = self._selected_snapshot()
        if candidate is None:
            return "当前厚度仅为计算预览；尚无通过光学保护且严格改善薄层问题的候选，原设计未改变。"
        if not self._applied and not self._automatic_candidate_accepted(candidate):
            return "候选尚无完整的光学保护与改进证据，只可预览，不能应用。"
        assessment = candidate.get("assessment") or {}
        completion = "全部保留层已达到厚度下限。" if assessment.get("thickness_complete") else "仍有膜层未达到厚度下限；这是可应用的阶段改进，不冒充修复完成。"
        if assessment.get("original_nominal_pass") is False:
            completion += "原光学目标仍未通过，真实违规另列；当前只通过光学不恶化保护。"
        return ("已应用的改进保留供回看。" if self._applied else "最佳改进已完成光学核验，无需等待求解收敛。") + completion

    @Property(str, notify=changed)
    def liveOpticalSummary(self):
        snapshot = self._selected_snapshot()
        if not isinstance(snapshot, Mapping):
            return "最佳光学误差：尚无完整独立核验的改进候选。"
        training, audit = snapshot.get("training_score"), snapshot.get("audit_score")
        guard = snapshot.get("optical_guard")
        if not isinstance(training, Mapping) or not isinstance(audit, Mapping):
            return "最佳光学误差：证据不完整，候选不可应用。"
        training_error, audit_error = training.get("max_error"), audit.get("max_error")
        def number(value):
            return ("未知" if not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) else f"{value:.6g}")
        change = (guard.get("maximum_absolute_change")
                  if isinstance(guard, Mapping) else None)
        change_text = "未知" if not isinstance(change, (int, float)) else f"{change * 100:.6g} 个百分点"
        passed = guard.get("passed") is True if isinstance(guard, Mapping) else False
        return (f"最佳最大归一化误差：训练 {number(training_error)} · 独立审计 {number(audit_error)}；"
                f"相对原曲线最大变化 {change_text} · 光学保护{'通过' if passed else '未通过/未知'}。")

    @Property(dict, notify=changed)
    def appliedPlotData(self):
        return self._applied_plot_data()

    def _applied_plot_data(self) -> dict[str, Any]:
        snapshot = self._applied_snapshot
        request = self._application_request(self._applied_payload)
        if (snapshot is None or not isinstance(request, Mapping)
                or self._applied_source_revision != self.owner._source_revision):
            return {}
        objectives = request.get("synthesis", {}).get("request", {}).get("objectives", ())
        index = int(getattr(self.owner, "_plot_objective", 0))
        if not 0 <= index < len(objectives):
            return {}
        coverage = snapshot.get("training_score", {}).get("coverage", ())
        candidates = [row for row in coverage if row.get("objective_index") == index]
        angle = float(getattr(self.owner, "_plot_angle", 0.0))
        raw = next((row for row in candidates if math.isclose(float(row["angle_deg"]), angle, abs_tol=1e-10)), None)
        if raw is None and not request.get("original_targets_present") and candidates:
            raw = candidates[0]
        if raw is None:
            return {}
        source = self._application_source(self._applied_payload) or {}
        baseline: dict[str, Any] = next((row for row in source.get("training_score", {}).get("coverage", ())
                         if row.get("objective_index") == index and row.get("angle_deg") == raw["angle_deg"]
                         and row.get("wavelength_nm") == raw["wavelength_nm"]), {})
        objective = objectives[index]
        criterion = objective["pointwise"]
        return dict(wavelength_nm=list(raw["wavelength_nm"]), candidate=list(raw["actual_values"]),
            baseline=list(baseline.get("actual_values", ())), baseline_label="修复前原设计",
            metric=objective["metric"], mode=criterion["mode"], angle_deg=raw["angle_deg"],
            target_value=criterion.get("target_value") if criterion["enabled"] else None,
            lower_bound=criterion.get("lower_bound") if criterion["enabled"] else None,
            upper_bound=criterion.get("upper_bound") if criterion["enabled"] else None,
            source="manufacturing_applied")

    @Property(str, notify=changed)
    def pendingApplicationHash(self):
        return "" if self._pending_application is None else self._pending_application["physical_hash"]

    @Property(list, notify=changed)
    def comparisonRows(self):
        return comparison_rows(self._payload) if self._payload is not None else deepcopy(self._comparison_rows)

    @Property(str, notify=changed)
    def resultSummary(self):
        if self._payload is None:
            return "尚无验证报告。"
        payload, request = self._payload, self._payload.get("request", {})
        if payload.get('schema_version') in (2, 3):
            body = payload.get('result') or {}
            if not request.get("original_targets_present"):
                scope = ('无启用目标：本轮冻结比较范围为 '
                         + comparison_scope_text(request.get('comparison_settings')) + '；原目标验收不适用。')
            elif payload.get("schema_version") == 3:
                scope = "可应用改进与全部条件满足分别判定；原光学目标仍显示真实训练和独立审计结果。"
            else:
                scope = "厚度、逐点光学变化及全部启用的原光学目标须联合通过；原目标训练与独立审计另列证据。"
            return (_automatic_terminal_summary(payload) + '\n'
                f"单层接受下限 {body.get('lower_limit_nm', '未知')} nm；自动识别 {len(body.get('detected_layer_ids', []))} 层。\n"
                f"光学变化上限 {request.get('optical_tolerance_percentage_points')} 个百分点；逐点最大绝对变化，不用均值抵消。\n"
                + scope)
        result = payload.get("result") or {}
        accounting = payload.get("work", payload.get("work_ledger", payload.get("budget_accounting", {})))
        return (f"终态：{payload.get('state', '未知')}；终止原因：{payload.get('termination_reason', '未知')}\n"
                "任务完成不等于两路均完成审计或原目标通过；逐路证据见下方。\n"
                f"已选择层 ID：{', '.join(request.get('defect_layer_ids', ()))}；"
                f"参考厚度：{request.get('reference_nm', '未知')} nm\n"
                f"总耗时 {result.get('elapsed_seconds', '未知')} s；估计峰值 {result.get('estimated_peak_bytes', '未知')} bytes；"
                f"资源上限 {result.get('effective_limit_bytes', '未知')} bytes\n"
                "任务账目：" + json.dumps(accounting, ensure_ascii=False, sort_keys=True))

    def _signature(self, base):
        design = deepcopy(dict(base.design_snapshot))
        optimization = design.get("workflow_settings", {}).get("optimization")
        if isinstance(optimization, dict):
            optimization.pop("sensitivity_table", None)  # UI-only drafts; no physical or goal change.
            optimization.pop("sensitivity_ideal", None)
        return deepcopy((base.design_revision, design, base.objectives,
                         base.acceptable_normalized_error, base.validation_samples_multiplier,
                         None if self._legacy_mode else self._comparison_settings))

    def _current_comparison_settings(self) -> dict[str, Any]:
        """Reuse the current simulator configuration, including its material policy."""
        from coat_sim.core.film_design_service import simulation_settings
        session = self.owner.session
        design = (session.snapshot().design if session is not None else self.owner._design)
        configuration = session.workflow_configuration("simulation") if session is not None else {}
        if not configuration:
            source = self.owner._source_controller
            current = getattr(source, "settings", None)
            if isinstance(current, Mapping):
                return simulation_settings(deepcopy(dict(current)))
            configuration = design.get("workflow_settings", {}).get("simulation", {})
        parameters = deepcopy(configuration.get("parameters", {}))
        parameters.setdefault("first_surface", design.get("first_surface", "front"))
        return simulation_settings(parameters)

    def _build_base_request(self):
        if self._legacy_mode:
            self._has_original_targets = True
            return self.owner._build_request(require_variables=False, fixed_topology=False)
        self._comparison_settings = self._current_comparison_settings()
        if any(row[group].get('enabled') for row in self.owner._objectives for group in ('mean', 'pointwise')):
            self._has_original_targets = True
            return self.owner._build_request(require_variables=False, fixed_topology=False)
        from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest
        from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
        snapshot = self.owner.session.snapshot() if self.owner.session is not None else None
        self._has_original_targets = False
        design = deepcopy(self.owner._design if snapshot is None else snapshot.design)
        settings = self._comparison_settings
        objectives = tuple(FilmOptimizationObjective(
            settings['wavelength_min_nm'], settings['wavelength_max_nm'], settings['samples'],
            angles_deg=(settings['angle_deg'],), polarization=settings['polarization'], metric=metric,
            pointwise=FilmObjectiveConstraint(lower_bound=0., upper_bound=1.)) for metric in ('R','T'))
        return FilmOptimizationRequest(design, self.owner._source_revision if snapshot is None else snapshot.design_revision,
            (), objectives, validation_samples_multiplier=4)

    def _context_is_current(self):
        context, owner = self._context, self.owner
        if (self._closed or self._stale or context is None or self._source_invalidated.is_set()
                or getattr(owner, "_source_closed", False) or getattr(owner, "_observers_detached", False)
                or owner.session is not context["session"]
                or owner._source_controller is not context["source"]):
            return False
        try:
            compute = context["project_compute"]
            if (compute is not getattr(owner, "_project_compute", None)
                    or (compute is not None and (compute.editing_frozen
                        or not compute.accepts(context["project_operation"])))):
                return False
            if (str(Path(getattr(owner.service, "root", Path.cwd())).resolve()) != context["project_root"]
                    or owner.session is None
                    or owner.session.snapshot().design_revision != context["revision"]
                    or owner._source_edit_epoch() != context["epoch"]
                    or self._draft_epoch != context["draft_epoch"]):
                return False
            # Owner edit/source signals mark _stale and the admission event.
            # Re-expanding large angle grids/catalogs is reserved for explicit
            # Apply's final owner fence, never a repeatedly read QML property.
            return True
        except (AttributeError, ValueError, TypeError, RuntimeError, OSError):
            return False

    def _selected_snapshot(self):
        if self._applied_snapshot is not None:
            return self._applied_snapshot
        if self._pending_application is not None:
            return self._pending_application["snapshot"]
        if self._payload is not None and self._payload.get("schema_version") == 3:
            return (self._payload.get("result") or {}).get("best")
        if self._payload is None and self._live_frame is not None:
            return self._live_frame.get("champion")
        if self._payload is None or self._route not in _ROUTES:
            return None
        result = self._payload.get("result")
        if not isinstance(result, dict):
            return None
        route: dict[str, Any] = next((r for r in result.get("routes", ()) if r.get("route") == self._route), {})
        return route.get("champion")

    def _automatic_candidate_accepted(self, snapshot, *, force=False):
        payload = self._application_payload()
        request = self._application_request(payload)
        if not isinstance(request, Mapping) or type(request.get("original_targets_present")) is not bool:
            return False
        if request.get("schema_version") == 3:
            source = self._application_source(payload)
            cached = self._eligibility_cache
            if not force:
                return bool(cached is not None and cached["payload"] is payload
                    and cached["snapshot"] is snapshot and cached["source"] is source
                    and cached["request"] is request and cached["eligible"] and self._context_is_current())
            from coat_sim.core.film_automatic_repair_jobs import applicable_improvement
            eligible = applicable_improvement(snapshot, source, request)
            self._eligibility_cache = dict(payload=payload, snapshot=snapshot, source=source,
                request=request, eligible=eligible, seq=self._live_seq,
                physical_hash=None if not isinstance(snapshot, Mapping) else snapshot.get("physical_hash"),
                assessment={} if not isinstance(snapshot, Mapping) else deepcopy(snapshot.get("assessment", {})))
            return eligible
        from coat_sim.core.film_automatic_repair import candidate_accepted
        return candidate_accepted(snapshot, request["original_targets_present"])

    def _prime_application_validation(self):
        cached = self._eligibility_cache
        if (cached is not None and cached["payload"] is self._application_payload()
                and cached["snapshot"] is self._selected_snapshot()):
            return
        try:
            self._automatic_candidate_accepted(self._selected_snapshot(), force=True)
        except (ValueError, TypeError, KeyError):
            self._eligibility_cache = None

    def _live_application(self, payload=None):
        payload = self._application_payload() if payload is None else payload
        return isinstance(payload, dict) and (payload.get("stage") == "repair_live"
                                              or payload.get("schema_version") == 3)

    def _application_matches(self, payload, snapshot):
        if payload is not self._application_payload() or snapshot is not self._selected_snapshot():
            return False
        if self._pending_application is not None and self._pending_application["context"] is not self._context:
            return False
        if self._live_application(payload):
            return self._automatic_candidate_accepted(snapshot)
        return any(route.get("champion") is snapshot for route in (payload.get("result") or {}).get("routes", ()))

    @Property(str, notify=changed)
    def selectedRouteWarning(self):
        if self._payload is not None and self._context is None:
            return "只读冻结报告，未绑定当前来源；可以回看或另存，不能应用。"
        if self._applied:
            return "本次确认的完整改进已应用；以下保留冻结证据供回看，不会重复写回。"
        snapshot = self._selected_snapshot()
        if not isinstance(snapshot, dict):
            if self._payload is not None and self._context is None:
                return '历史报告只读，未绑定当前来源；可以回看或另存，不能应用。'
            if self.hasLivePreview:
                return "尚无完整核验且严格改善薄层问题的候选；当前试探只作预览。"
            if not self._legacy_mode:
                return '本轮尚无完整、通过容差的修复候选；不完整或超出容差的结果不能应用。'
            return "尚未选择完整 A/B 候选；不会自动选择或应用。"
        audit, training = snapshot.get("audit_score"), snapshot.get("training_score")
        if not isinstance(audit, dict) or not isinstance(training, dict):
            return "未知/未通过：所选候选缺少完整独立审计，不能应用。"
        if self._live_application():
            if not self._automatic_candidate_accepted(snapshot):
                return "候选未通过光学保护或没有严格改善薄层问题，不能应用。"
            assessment = snapshot.get("assessment") or {}
            thickness = ("厚度下限已全部达到。" if assessment.get("thickness_complete") else
                         "厚度下限尚未全部达到，可应用当前阶段改进。")
            nominal = assessment.get("original_nominal_pass")
            goals = ("原光学目标不适用。" if nominal is None else "原光学目标通过。" if nominal else
                     "原光学目标仍未通过；保留真实违规，当前候选仅通过不恶化保护。")
            return "已完整核验光学变化和原光学保护；" + thickness + goals
        if self._payload is not None and self._payload.get('schema_version') == 2:
            if not self._automatic_candidate_accepted(snapshot):
                return '所选候选未通过完整联合验收，不能应用；请查看厚度、逐点光学变化及原启用目标的真实失败或未知证据。'
            if not self._payload['request']['original_targets_present']:
                return '单层下限和相对原曲线的变化均通过；没有启用的原光学目标，原目标验收不适用。'
            return '单层下限、逐点光学变化及全部启用原目标的训练与独立审计均通过；应用前仍会复核来源、目标、材料和项目。'
        if audit.get("nominal_pass") is True and training.get("nominal_pass") is True:
            return "所选候选的冻结训练与独立审计均通过原始目标；应用前仍会复核来源、目标、材料和项目。"
        return "警告：所选候选未通过原始光学目标。应用只替换膜系，不表示光学验收通过，也不会放宽原始目标。"

    @Property(bool, notify=changed)
    def canApply(self):
        payload = self._application_payload()
        live = self._live_application(payload)
        if (self._applied or self._apply_in_progress or self._unknown_submission
                or payload is None or not self._context_is_current()):
            return False
        if live:
            if self._stop_reason in {"source_changed", "owner_close", "shutdown"} or (self._cancel_intent and self.busy):
                return False
        elif (self.busy or self._cancel_intent or payload.get("state") != "completed"
                or not payload.get("is_final")):
            return False
        snapshot = self._selected_snapshot()
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("audit_score"), dict)
                or type(snapshot.get("training_score", {}).get("nominal_pass")) is not bool
                or type(snapshot["audit_score"].get("nominal_pass")) is not bool):
            return False
        if (live or payload.get('schema_version') == 2) and not self._automatic_candidate_accepted(snapshot):
            return False
        check = getattr(self.owner, "_manufacturing_can_apply", None)
        try:
            return bool(callable(check) and check(payload, self._context, snapshot))
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            return False

    def load_verified_history(self, payload, job_id):
        """The queue loader validated frozen evidence off the UI thread; no optical replay claim."""
        if self.busy:
            raise ValueError("cannot replace an active repair with history")
        if not isinstance(payload, dict) or payload.get("kind") != "coat_sim.film_manufacturing_result":
            raise ValueError("unsupported manufacturing report")
        self._payload, self._job_id = deepcopy(payload), str(job_id)
        self._live_frame = None
        self._live_seq = -1
        self._live_bar_scale_nm = 0.0
        self._pending_application = self._applied_snapshot = self._applied_payload = None
        self._comparison_rows = comparison_rows(payload)
        self._context = None
        self._route = ""
        self._error = ""
        self._status = "只读队列历史；未绑定当前来源设计，不能应用。"
        self._prime_application_validation()
        self._expand_live_bar_scale()
        self.changed.emit()

    @Slot(result=bool)
    def prepare(self):
        if self.busy or self._closed:
            return False
        try:
            session = self.owner.session
            if (session is not None and session.snapshot().design_revision != self.owner._source_revision):
                self.owner.refreshDesign()
            base = self._build_base_request()
            old = {row["layer_id"]: row["selected"] for row in self._rows}
            self._rows = [{"layer_id": row["id"], "label": f"{index + 1}. {row['material_id']} / {row['optical_set_id']}",
                           "thickness_value": row['thickness']['value'],
                           "thickness_unit": row['thickness']['unit'],
                           "selected": old.get(row["id"], False)}
                          for index, row in enumerate(base.design_snapshot["front_layers"])]
            self._prepared_signature = self._signature(base)
            self._stale = False
            self._error = ""
            if not self._rows:
                raise ValueError("没有当前前层；请先添加实际膜系。")
            self._status = ("已刷新当前来源；自动识别低于“单层目标厚度−厚度容差”的层，无需勾选。"
                            if self._payload is None else "来源已刷新；下方仍为上次冻结报告，不代表当前草稿。")
            self.layerRowsChanged.emit()
            self.changed.emit()
            return True
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            self._error = str(exc)
            self.layerRowsChanged.emit()
            self.changed.emit()
            return False

    def _edited(self):
        self._draft_epoch += 1
        self._error = ""
        self._status = ("参数已编辑；下方保留上次冻结报告，不代表当前草稿且不可应用。" if self.hasResult else
                        "参数已记录；提交时验证，空白不等于零或无限。")
        self.changed.emit()

    @Slot(str, bool, result=bool)
    def setLayerSelected(self, layer_id, selected):
        if self.busy or self._closed or type(selected) is not bool:
            return False
        row = next((row for row in self._rows if row["layer_id"] == layer_id), None)
        if row is None:
            return False
        row["selected"] = selected
        self._legacy_mode = True
        self._edited()
        self.layerRowsChanged.emit()
        return True

    @Slot(str, str, result=bool)
    def setOption(self, name, text):
        if self.busy or self._closed or name not in (_OPTION_LABELS | _LEGACY_LABELS) or not isinstance(text, str):
            return False
        if name in _LEGACY_LABELS and name != 'reference_nm':
            self._legacy_mode = True
        elif name in ('thickness_tolerance_nm', 'optical_tolerance_percentage_points'):
            self._legacy_mode = False
        if name in _OPTION_LABELS:
            self._options[name] = text
        if name in _LEGACY_LABELS:
            self._legacy_options[name] = text
        # Do not recreate editors or validate/replace an in-progress draft.
        self._edited()
        return True

    @Slot(str, str, result=bool)
    def setAutomaticOption(self, name, text):
        if (self.busy or self._closed or name not in (set(_OPTION_LABELS) | {
                "lower_limit_nm"}) or not isinstance(text, str)):
            return False
        switched_from_legacy = self._legacy_mode
        self._legacy_mode = False
        if name == "lower_limit_nm":
            # Preserve the user's exact draft while compiling the physical
            # lower bound into the legacy target-minus-tolerance request.
            self._options["reference_nm"] = text
            self._options["thickness_tolerance_nm"] = "0"
            self._edited()
        else:
            self._options[name] = text
            self._edited()
        if switched_from_legacy:
            # A prepared v1 A/B request does not contain the automatic mode's
            # comparison scope. Re-capture it once on the explicit UI-mode
            # transition, after preserving the first typed value.
            self.prepare()
        return True

    @Slot(str, result=bool)
    def setSelectedRoute(self, route):
        if self.busy or route not in ("", *_ROUTES):
            return False
        self._route = route
        self.changed.emit()
        return True

    def _parse_option(self, name):
        legacy = name in _LEGACY_LABELS and (self._legacy_mode or name not in _OPTION_LABELS)
        caption = (_LEGACY_LABELS if legacy else _OPTION_LABELS)[name]
        text = (self._legacy_options if legacy else self._options)[name].strip()
        if not text:
            raise ValueError(caption + "必须明确填写；空白不等于零或无限。")
        if name in ("refinement_evaluations_per_route", "max_iterations_per_route"):
            try:
                exact = Decimal(text)
            except InvalidOperation as exc:
                raise ValueError(caption + "必须是有限正数；原始草稿已保留。") from exc
            # Keep the existing finite numeric-input domain, but never use a
            # rounded binary float to decide integrality or construct a quota.
            if not exact.is_finite() or exact <= 0 or exact > _MAX_FINITE_NUMERIC_INPUT:
                raise ValueError(caption + "必须是有限正数；原始草稿已保留。")
            if exact != exact.to_integral_value():
                raise ValueError(caption + "必须是正整数；不会自动舍入。")
            return int(exact)
        domain = '非负有限数' if name in ('thickness_tolerance_nm','optical_tolerance_percentage_points') else '有限正数'
        try:
            value = float(text)
        except (ValueError, OverflowError) as exc:
            raise ValueError(caption + '必须是' + domain + '；原始草稿已保留。') from exc
        if not math.isfinite(value) or (value < 0 if name in ('thickness_tolerance_nm','optical_tolerance_percentage_points') else value <= 0):
            raise ValueError(caption + '必须是' + domain + '；原始草稿已保留。')
        return value

    @Slot(str, result=bool)
    def finishOptionEdit(self, name):
        if self.busy or name not in (_OPTION_LABELS | _LEGACY_LABELS):
            return False
        try:
            self._parse_option(name)
            self._error = ""
        except ValueError as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    @Slot(str, result=bool)
    def finishAutomaticOptionEdit(self, name):
        if self.busy or self._closed or name not in (set(_OPTION_LABELS) | {
                "lower_limit_nm"}):
            return False
        self._legacy_mode = False
        return self.finishOptionEdit(
            "reference_nm" if name == "lower_limit_nm" else name)

    def _validated_options(self):
        return {name: self._parse_option(name) for name in (_LEGACY_LABELS if self._legacy_mode else _OPTION_LABELS)}

    @Slot(result=bool)
    def startAutomatic(self):
        """New UI entry; old API drafts cannot switch its algorithm or budget."""
        if self.busy or self._closed or self.owner.busy:
            return False
        self._legacy_mode = False
        self.changed.emit()
        return self.start()

    @Slot(result=bool)
    def start(self):
        if self.busy or self._closed or self.owner.busy:
            return False
        if self._unknown_submission:
            self._error = f"作业 {self._job_id} 提交状态未知；请先在共享队列核对，不会重复提交。"
            self.changed.emit()
            return False
        try:
            from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
            from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
            base = self._build_base_request()
            signature, prepared_signature = self._signature(base), self._prepared_signature
            if self._legacy_mode and prepared_signature is not None:
                # Legacy selection/quota edits switch modes after Prepare. Its
                # v1 worker does not consume the automatic comparison scope.
                signature, prepared_signature = signature[:-1], prepared_signature[:-1]
            if prepared_signature is None or signature != prepared_signature or self._stale:
                raise ValueError("来源设计或当前目标已改变；请先刷新来源，核对层 ID 后重新运行。")
            options = self._validated_options()
            if not self._legacy_mode:
                options['original_targets_present'] = self._has_original_targets
                options['comparison_settings'] = deepcopy(self._comparison_settings)
            selected = tuple(row["layer_id"] for row in self._rows if row["selected"])
            if self._legacy_mode and not selected:
                raise ValueError("请明确勾选当前前层；不会按隐藏阈值自动选择薄层。")
            source = deepcopy(dict(base.design_snapshot))
            if self._legacy_mode and any(row["thickness"]["value"] >= options["reference_nm"] for row in source["front_layers"] if row["id"] in selected and row["thickness"]["unit"] == "nm"):
                raise ValueError("勾选层必须小于明确填写的参考厚度；不会自动删除选择或提高参考值。")
            environment = deepcopy(source)
            environment["front_layers"], environment["back_layers"] = [], []
            refs = tuple(dict.fromkeys(FilmNeedleMaterialReference(row["material_id"], row["optical_set_id"])
                                       for row in source["front_layers"]))
            synthesis = FilmSynthesisRequest(
                environment=environment, objectives=base.objectives, candidate_materials=refs,
                design_revision=base.design_revision, seed_designs=(deepcopy(source),),
                acceptable_normalized_error=base.acceptable_normalized_error,
                validation_samples_multiplier=base.validation_samples_multiplier,
                frozen_material_snapshot=base.frozen_material_snapshot, cpu_workers=1, forward_backend="cpu")
            job_id = uuid4().hex
            compute = getattr(self.owner, "_project_compute", None)
            operation = compute.capture(job_id) if compute is not None else None
            metadata = {"summary": "工艺薄层修复 A/B" if self._legacy_mode else "自动薄层修复", "origin": {"surface": "desktop"}}
            if compute is not None:
                metadata = compute.metadata(operation, metadata)
            context = {"session": self.owner.session, "source": self.owner._source_controller,
                       "revision": base.design_revision, "epoch": self.owner._source_edit_epoch(),
                       "signature": self._signature(base), "draft_epoch": self._draft_epoch,
                       "comparison_settings": deepcopy(self._comparison_settings) if not self._legacy_mode else None,
                       "project_compute": compute, "project_operation": operation,
                       "project_root": str(Path(getattr(self.owner.service, "root", Path.cwd())).resolve()),
                       "operation_id": self._run_coordinator.operation_serial + 1}
            client = self.client_factory(getattr(self.owner.service, "root", Path.cwd()))
            invalidated = threading.Event()
            session, source_owner = context["session"], context["source"]
            comparison_settings = deepcopy(self._comparison_settings) if not self._legacy_mode else None

            def admission_check():
                return (not invalidated.is_set()
                        and (compute is None or (not compute.editing_frozen and compute.accepts(operation)))
                        and (session is None or (session.snapshot().design_revision == context["revision"]
                              and int(getattr(source_owner, "pendingEditEpoch", 0)) == context["epoch"]))
                         and (comparison_settings is None
                              or self._current_comparison_settings() == comparison_settings))

            self._context, self._source_invalidated = context, invalidated
            self._cancel_intent = self._applied = False
            self._live_frame, self._live_seq = None, -1
            self._live_bar_scale_nm = 0.0
            self._stop_reason = ""
            self._auto_apply_attempted = self._apply_in_progress = False
            self._pending_application = self._applied_snapshot = self._applied_payload = None
            self._completed_operation_id = None
            self._eligibility_cache = None
            self._payload = None
            self._route, self._error, self._job_id = "", "", job_id
            operation_id = self._run_coordinator.start(
                submit=None, edit_epoch=context["epoch"],
                worker_factory=lambda serial: _ManufacturingTask(
                    serial, self.owner.service, synthesis, source, options, selected,
                    client, job_id, metadata, admission_check))
            if operation_id is None:
                raise ValueError("修复操作正在运行或已关闭；没有重复提交。")
            self._comparison_rows = []
            self._status = "正在后台冻结来源、目标、材料与可编辑闭包；核验后的阶段改进可应用，用户取消时自动应用当前改进。"
            self.changed.emit()
            return True
        except (ValueError, TypeError, KeyError, RuntimeError, OverflowError, OSError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False

    @Slot(int, str)
    def _on_admitted(self, operation_id, job_id):
        if self._closed:
            return
        self._job_id = job_id
        context = self._context or {}
        compute = context.get("project_compute")
        if compute is not None:
            compute.register_job(job_id, module_key="film_optimization", state="queued",
                                 metadata={"search_mode": "manufacturing_repair"})
        self._status = ("独立 A/B 修复作业已进入本地 CPU 共享队列；原设计保持不变。" if self._legacy_mode else
                        "自动薄层修复已进入本地 CPU 共享队列；原设计保持不变。")
        self.changed.emit()

    @Slot(int, object)
    def _on_progress(self, operation_id, progress):
        if self._closed or (self._context is not None and operation_id != self._context.get("operation_id", operation_id)):
            return
        if isinstance(progress, dict):
            if progress.get("stage") == "repair_prepared":
                prepared = progress.get("prepared")
                if isinstance(prepared, dict) and self._context is not None:
                    self._context.update(prepared)
                self._expand_live_bar_scale()
                self.changed.emit()
                return
            if progress.get("stage") == "repair_live":
                if self._stale or self._applied or self._source_invalidated.is_set() or self._context is None:
                    return
                if type(progress.get("seq")) is int and progress["seq"] <= self._live_seq:
                    return
                from coat_sim.core.film_automatic_repair_jobs import validate_live_progress
                try:
                    frame = validate_live_progress(progress, expected_request=self._context.get("frozen_request"),
                                                   after_seq=self._live_seq)
                except (ValueError, TypeError, KeyError) as exc:
                    self._error = "实时帧未通过身份或证据检查，已保留上一有效帧：" + str(exc)
                    self.changed.emit()
                    return  # A late, mismatched or incomplete frame never replaces the verified champion.
                self._live_frame, self._live_seq = frame, frame["seq"]
                self._expand_live_bar_scale()
                self._prime_application_validation()
                self._status = "正在修复；当前厚度是计算预览，最佳列只显示完成光学核验的改进。"
                self.changed.emit()
                return
            if progress.get("stage") == "repair_live_rejected":
                self._error = "实时帧身份或证据不完整，已忽略：" + str(progress.get("message", ""))
                self.changed.emit()
                return
            stage = {'repair_local':'正在联动优化各保留层', 'repair_audit':'正在独立核验逐点光学变化',
                     'repair_audited_best':'已保留通过联合验收的候选，继续比较结构'}.get(str(progress.get('stage')), progress.get('stage', 'running'))
            self._status = (f"{stage} · 计算 {progress.get('physical_calls', progress.get('evaluations', '未知'))} · "
                            f"{progress.get('message', '')}")
        self.changed.emit()

    @Slot(int, object, object, object)
    def _on_finished(self, operation_id, payload, prepared, error):
        if (self._closed or self._completed_operation_id == operation_id
                or (self._context is not None and operation_id != self._context.get("operation_id", operation_id))):
            return
        self._completed_operation_id = operation_id
        self._error = str(error or "")
        self._unknown_submission = self._error.startswith("submission_unknown:")
        if isinstance(prepared, dict) and self._context is not None:
            self._comparison_rows = deepcopy(prepared.get("comparison_rows", []))
            self._context.update({key: deepcopy(value) for key, value in prepared.items()
                                  if key != "comparison_rows"})
        self._payload = deepcopy(payload) if isinstance(payload, dict) else None
        if self._payload is not None and self._payload.get('schema_version') in (2, 3):
            self._route = 'automatic'
        self._prime_application_validation()
        self._expand_live_bar_scale()
        self._status = (f"{payload.get('state')} / {payload.get('termination_reason')}；仅比较，未应用。"
                        if self._payload is not None else "没有可验证报告；原设计未改变。")
        if self._payload is not None and self._payload.get('schema_version') in (2, 3):
            self._status = _automatic_terminal_summary(self._payload)
        if self._stale or self._source_invalidated.is_set():
            self._status += " 来源已改变，报告仅供只读比较。"
        if self._cancel_intent and not self._live_application():
            self._status += " 已请求取消，本次结果不可应用。"
        compute = (self._context or {}).get("project_compute")
        if compute is not None:
            state = ("submission_unknown" if self._unknown_submission else "failed" if self._error
                     else "cancelled" if (payload or {}).get("state") == "cancelled"
                     else "failed" if (payload or {}).get("state") == "failed" else "succeeded")
            compute.update_job(self._job_id, state)
        if (self._payload is not None and self._payload.get("schema_version") == 3
                and self._stop_reason == "user_cancel" and not self._auto_apply_attempted
                and not self._applied and not self._error):
            self._auto_apply_attempted = True
            if self.canApply:
                self._apply_candidate(automatic=True)
            else:
                self._status = "已取消；没有可安全写回的完整改进，原设计未改变。"
        elif self._applied:
            self._status = "本轮已结束，已应用先前确认的改进；后续报告仅供回看，不会再次写回。"
        self.changed.emit()

    @Slot()
    def cancel(self):
        self._request_cancel("user_cancel")

    def _request_cancel(self, reason):
        invalidation = {"source_changed", "owner_close", "shutdown"}
        if (self._stop_reason != "shutdown"
                and not (self._stop_reason in invalidation and reason not in invalidation)
                and not (self._stop_reason == "manual_apply" and reason == "user_cancel")):
            self._stop_reason = reason
        if reason == "user_cancel" and not self._apply_in_progress:
            self._pending_application = None
        if self.busy:
            self._cancel_intent = True
            error = self._run_coordinator.request_cancel()
            self._status = ("已请求取消；等待服务终态后自动应用最后完整核验的改进，无改进则保持原设计。"
                            if self._stop_reason == "user_cancel" and not self._legacy_mode else
                            "已请求停止；来源或窗口失效时只保留报告，不自动写回。")
            if error is not None:
                self._error = f"取消请求未确认：{error}；可重试同一作业的取消。"
            self.changed.emit()

    @Slot()
    def simulationSettingsChanged(self):
        """Invalidate on configuration events without recomputing on QML reads."""
        if self._legacy_mode or self._comparison_settings is None:
            return
        try:
            if self._current_comparison_settings() == self._comparison_settings:
                return
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError):
            pass
        self.sourceChanged()

    @Slot()
    def sourceChanged(self):
        self._stale = True
        self._source_invalidated.set()
        self._eligibility_cache = None
        if not self._apply_in_progress:
            self._pending_application = None
        if self.busy:
            self._request_cancel("manual_apply" if self._apply_in_progress else "source_changed")
        elif not self._apply_in_progress:
            self._stop_reason = "source_changed"
        self._status = "来源、原始目标或项目已改变；需刷新并重新核对。本次报告不可应用。"
        self.changed.emit()

    @Slot(result=bool)
    def prepareApplication(self):
        if not self.canApply:
            self._error = "候选缺少完整独立审计，或来源/目标/材料/项目已改变；整个候选未应用。"
            self.changed.emit()
            return False
        from coat_sim.core.film_synthesis_archive import physical_design_hash
        payload = deepcopy(self._application_payload())
        if payload.get("stage") == "repair_live":
            snapshot = payload["champion"]
        elif payload.get("schema_version") == 3:
            snapshot = payload["result"]["best"]
        else:
            snapshot = next(route["champion"] for route in payload["result"]["routes"] if route["route"] == self._route)
        self._pending_application = dict(payload=payload, snapshot=snapshot, context=self._context,
                                         physical_hash=physical_design_hash(snapshot["design"]))
        self._prime_application_validation()
        self.changed.emit()
        return True

    @Slot()
    def discardPreparedApplication(self):
        if not self._apply_in_progress:
            self._pending_application = None
            self._prime_application_validation()
            self.changed.emit()

    @Slot(str, result=bool)
    def applyPreparedCandidate(self, physical_hash):
        if self._pending_application is None or self._pending_application["physical_hash"] != physical_hash:
            self._error = "确认候选已失效，未改动来源；请重新核对当前改进。"
            self.changed.emit()
            return False
        return self._apply_candidate(automatic=False)

    @Slot(result=bool)
    def applySelectedRoute(self):
        if self._live_application() and self._pending_application is None and not self.prepareApplication():
            return False
        return self._apply_candidate(automatic=False)

    def _apply_candidate(self, *, automatic):
        if not self.canApply:
            self._error = "候选缺少完整核验改进证据，或来源已变化；原设计未改变。"
            self.changed.emit()
            return False
        payload, context, snapshot = self._application_payload(), self._context, self._selected_snapshot()
        apply = getattr(self.owner, "_apply_manufacturing_candidate", None)
        self._apply_in_progress = True
        try:
            if self.busy:
                self._request_cancel("manual_apply")
            if not callable(apply) or not apply(payload, context, snapshot):
                raise ValueError("来源修订、未提交编辑、目标或材料已改变；整个候选未应用。")
            self._applied = True
            self._applied_payload, self._applied_snapshot = payload, snapshot
            self._applied_source_revision = self.owner._source_revision
            self._pending_application = None
            self._status = ("已取消并自动应用最后完整核验的改进；可在设计窗口撤销。" if automatic else
                            "已应用本次确认的完整改进并结束本轮；可在设计窗口撤销。")
            self._error = ""
            self.owner.plotDataChanged.emit()
        except (ValueError, TypeError, RuntimeError, KeyError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        finally:
            self._apply_in_progress = False
        self.changed.emit()
        return True

    @Slot(str, bool, result=bool)
    def saveReport(self, path, overwrite=False):
        if self.busy or self._payload is None:
            return False
        try:
            from coat_sim.core.film_result_service import atomic_film_json
            target = Path(path).expanduser()
            if target.suffix.lower() != ".json":
                target = target.with_suffix(".json")
            if target.exists() and not overwrite:
                self.overwriteRequested.emit(str(target))
                return False
            atomic_film_json(target, self._payload, overwrite=overwrite)
            self._status = "完整冻结证据与预算报告已保存：" + str(target)
            self._error = ""
        except (ValueError, TypeError, OSError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self.changed.emit()
        return True

    @Slot(QUrl, bool, result=bool)
    def saveReportUrl(self, url, overwrite=False):
        if not url.isLocalFile():
            self._error = "请选择本地 JSON 报告位置。"
            self.changed.emit()
            return False
        return self.saveReport(url.toLocalFile(), overwrite)

    @Slot(result=bool)
    def requestClose(self):
        """Do not detach until the owner confirms its own document close.

        A rejected unsaved-document confirmation leaves a usable controller.
        The durable worker is independently retained, so closure never blocks.
        """
        if not self._legacy_mode or getattr(self.owner, "project_context", None) is None:
            self._request_cancel("owner_close")
        else:
            self._stop_reason = "owner_close"
        return True

    @Slot()
    def detach(self):
        if self._closed:
            return
        if not self._legacy_mode or getattr(self.owner, "project_context", None) is None:
            self._request_cancel("owner_close")
        self._closed = True
        self._source_invalidated.set()
        self._run_coordinator.detach_service_observer()

    def shutdown(self, timeout_ms=1000):
        self._stop_reason = "shutdown"
        self.detach()
        return self._run_coordinator.shutdown(timeout_ms)
