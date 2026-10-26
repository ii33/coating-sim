"""Explicit A/B manufacturing repair UI over the durable local CPU scheduler.

Only this controller owns drafts/report selection. Numerical execution and
physical replay stay in the existing job worker, never on the Qt UI thread.
"""
from __future__ import annotations

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
from coat_sim.ui.film_manufacturing_quick_adapter import comparison_rows
from coat_sim.ui.film_optimization_run_coordinator import FilmOptimizationRunCoordinator


_EXECUTOR = "film_manufacturing_repair"
_MAX_FINITE_NUMERIC_INPUT = Decimal.from_float(sys.float_info.max)
_ROUTES = ("raise_selected", "delete_selected_merge")
_OPTION_LABELS = {"reference_nm": "参考厚度 nm", "refinement_evaluations_per_route": "每路精修评价配额",
                  "max_iterations_per_route": "每路最大迭代", "seconds_per_route": "每路秒数配额"}


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
            request = jobs.make_request_payload(
                self.service, self.synthesis, self.source, defect_layer_ids=self.selected,
                request_id=self.job_id, **self.options)
            _, frozen_synthesis, _ = film_synthesis_request_from_payload(request["synthesis"])
            closure = freeze_synthesis_editable_snapshot(
                self.service, self.source, frozen_synthesis.frozen_material_snapshot)
            spec = freeze_spec(None, _EXECUTOR, {"payload": request})
            job = spec["parameters"]["payload"]
            self._check()
            if _EXECUTOR not in self.client.capabilities().get("executors", ()):
                raise ValueError("当前本地调度器不支持工艺薄层修复；请正常关闭并重启应用后重试。")
            self._check()
            submit_local_with_receipt(self.client, spec, job_id=self.job_id, metadata=self.metadata)
            self.cancel_event.mark_admitted()
            self.signals.jobIdChanged.emit(self.operation_id, self.job_id)
            try:
                payload = observe_job(
                    self.client, self.job_id,
                    lambda item: self.signals.progress.emit(self.operation_id, item),
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
                                       {"frozen_request": request, "synthesis_request": frozen_synthesis, "editable_snapshot": closure,
                                        "comparison_rows": comparison_rows(payload)}, None)
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
        self._prepared_signature = None
        self._context: dict | None = None
        self._payload: dict | None = None
        self._comparison_rows: list[dict] = []
        self._job_id = ""
        self._error = ""
        self._status = "请选择当前前层并填写参考厚度和所有每路配额；原始设计不会自动改变。"
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
        return dict(self._options)

    @Property(str, notify=changed)
    def selectedRoute(self):
        return self._route

    @Property(bool, notify=changed)
    def hasResult(self):
        return self._payload is not None

    @Property(list, notify=changed)
    def comparisonRows(self):
        return comparison_rows(self._payload) if self._payload is not None else deepcopy(self._comparison_rows)

    @Property(str, notify=changed)
    def resultSummary(self):
        if self._payload is None:
            return "尚无验证报告。"
        payload, request = self._payload, self._payload.get("request", {})
        result = payload.get("result") or {}
        accounting = payload.get("work", payload.get("work_ledger", payload.get("budget_accounting", {})))
        return (f"终态：{payload.get('state', '未知')}；终止原因：{payload.get('termination_reason', '未知')}\n"
                "任务完成不等于两路均完成审计或原目标通过；逐路证据见下方。\n"
                f"已选择层 ID：{', '.join(request.get('defect_layer_ids', ()))}；"
                f"参考厚度：{request.get('reference_nm', '未知')} nm\n"
                f"总耗时 {result.get('elapsed_seconds', '未知')} s；估计峰值 {result.get('estimated_peak_bytes', '未知')} bytes；"
                f"资源上限 {result.get('effective_limit_bytes', '未知')} bytes\n"
                "任务账目：" + json.dumps(accounting, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _signature(base):
        return deepcopy((base.design_revision, base.design_snapshot, base.objectives,
                         base.acceptable_normalized_error, base.validation_samples_multiplier))

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
        if self._payload is None or self._route not in _ROUTES:
            return None
        result = self._payload.get("result")
        if not isinstance(result, dict):
            return None
        route: dict[str, Any] = next((r for r in result.get("routes", ()) if r.get("route") == self._route), {})
        return route.get("champion")

    @Property(str, notify=changed)
    def selectedRouteWarning(self):
        snapshot = self._selected_snapshot()
        if not isinstance(snapshot, dict):
            return "尚未选择完整 A/B 候选；不会自动选择或应用。"
        audit, training = snapshot.get("audit_score"), snapshot.get("training_score")
        if not isinstance(audit, dict) or not isinstance(training, dict):
            return "未知/未通过：所选候选缺少完整独立审计，不能应用。"
        if audit.get("nominal_pass") is True and training.get("nominal_pass") is True:
            return "所选候选的冻结训练与独立审计均通过原始目标；应用前仍会复核来源、目标、材料和项目。"
        return "警告：所选候选未通过原始光学目标。应用只替换膜系，不表示光学验收通过，也不会放宽原始目标。"

    @Property(bool, notify=changed)
    def canApply(self):
        if (self.busy or self._applied or self._cancel_intent or self._unknown_submission
                or self._payload is None or self._payload.get("state") != "completed"
                or not self._payload.get("is_final") or not self._context_is_current()):
            return False
        snapshot = self._selected_snapshot()
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("audit_score"), dict)
                or type(snapshot.get("training_score", {}).get("nominal_pass")) is not bool
                or type(snapshot["audit_score"].get("nominal_pass")) is not bool):
            return False
        check = getattr(self.owner, "_manufacturing_can_apply", None)
        try:
            return bool(callable(check) and check(self._payload, self._context, snapshot))
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            return False

    def load_verified_history(self, payload, job_id):
        """The queue loader validated frozen evidence off the UI thread; no optical replay claim."""
        if self.busy:
            raise ValueError("cannot replace an active repair with history")
        if not isinstance(payload, dict) or payload.get("kind") != "coat_sim.film_manufacturing_result":
            raise ValueError("unsupported manufacturing report")
        self._payload, self._job_id = deepcopy(payload), str(job_id)
        self._comparison_rows = comparison_rows(payload)
        self._context = None
        self._route = ""
        self._error = ""
        self._status = "只读队列历史；未绑定当前来源设计，不能应用。"
        self.changed.emit()

    @Slot(result=bool)
    def prepare(self):
        if self.busy or self._closed:
            return False
        try:
            session = self.owner.session
            if (session is not None and session.snapshot().design_revision != self.owner._source_revision):
                self.owner.refreshDesign()
            base = self.owner._build_request(require_variables=False, fixed_topology=False)
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
            self._status = ("已刷新当前来源；新层默认不勾选。填写所有显式配额后运行 A/B 比较。"
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
        self._edited()
        self.layerRowsChanged.emit()
        return True

    @Slot(str, str, result=bool)
    def setOption(self, name, text):
        if self.busy or self._closed or name not in _OPTION_LABELS or not isinstance(text, str):
            return False
        self._options[name] = text
        # Do not recreate editors or validate/replace an in-progress draft.
        self._edited()
        return True

    @Slot(str, result=bool)
    def setSelectedRoute(self, route):
        if self.busy or route not in ("", *_ROUTES):
            return False
        self._route = route
        self.changed.emit()
        return True

    def _parse_option(self, name):
        caption = _OPTION_LABELS[name]
        text = self._options[name].strip()
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
        try:
            value = float(text)
        except (ValueError, OverflowError) as exc:
            raise ValueError(caption + "必须是有限正数；原始草稿已保留。") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(caption + "必须是有限正数；原始草稿已保留。")
        return value

    @Slot(str, result=bool)
    def finishOptionEdit(self, name):
        if self.busy or name not in _OPTION_LABELS:
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

    def _validated_options(self):
        return {name: self._parse_option(name) for name in _OPTION_LABELS}

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
            base = self.owner._build_request(require_variables=False, fixed_topology=False)
            if self._prepared_signature is None or self._signature(base) != self._prepared_signature or self._stale:
                raise ValueError("来源设计或当前目标已改变；请先刷新来源，核对层 ID 后重新运行。")
            options = self._validated_options()
            selected = tuple(row["layer_id"] for row in self._rows if row["selected"])
            if not selected:
                raise ValueError("请明确勾选当前前层；不会按隐藏阈值自动选择薄层。")
            source = deepcopy(dict(base.design_snapshot))
            if any(row["thickness"]["value"] >= options["reference_nm"] for row in source["front_layers"] if row["id"] in selected and row["thickness"]["unit"] == "nm"):
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
            metadata = {"summary": "工艺薄层修复 A/B", "origin": {"surface": "desktop"}}
            if compute is not None:
                metadata = compute.metadata(operation, metadata)
            context = {"session": self.owner.session, "source": self.owner._source_controller,
                       "revision": base.design_revision, "epoch": self.owner._source_edit_epoch(),
                       "signature": self._signature(base), "draft_epoch": self._draft_epoch,
                       "project_compute": compute, "project_operation": operation,
                       "project_root": str(Path(getattr(self.owner.service, "root", Path.cwd())).resolve())}
            client = self.client_factory(getattr(self.owner.service, "root", Path.cwd()))
            invalidated = threading.Event()
            session, source_owner = context["session"], context["source"]

            def admission_check():
                return (not invalidated.is_set()
                        and (compute is None or (not compute.editing_frozen and compute.accepts(operation)))
                        and (session is None or (session.snapshot().design_revision == context["revision"]
                             and int(getattr(source_owner, "pendingEditEpoch", 0)) == context["epoch"])))

            self._context, self._source_invalidated = context, invalidated
            self._cancel_intent = self._applied = False
            self._route, self._error, self._job_id = "", "", job_id
            operation_id = self._run_coordinator.start(
                submit=None, edit_epoch=context["epoch"],
                worker_factory=lambda serial: _ManufacturingTask(
                    serial, self.owner.service, synthesis, source, options, selected,
                    client, job_id, metadata, admission_check))
            if operation_id is None:
                raise ValueError("修复操作正在运行或已关闭；没有重复提交。")
            self._payload = None
            self._comparison_rows = []
            self._status = "正在后台冻结来源、原始目标、材料与可编辑闭包，然后提交本地 CPU 队列；原设计不变。"
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
        self._status = "独立 A/B 修复作业已进入本地 CPU 共享队列；原设计保持不变。"
        self.changed.emit()

    @Slot(int, object)
    def _on_progress(self, operation_id, progress):
        if self._closed:
            return
        if isinstance(progress, dict):
            self._status = (f"{progress.get('stage', 'running')} · 评价 {progress.get('evaluations', '未知')} · "
                            f"{progress.get('message', '')}")
        self.changed.emit()

    @Slot(int, object, object, object)
    def _on_finished(self, operation_id, payload, prepared, error):
        if self._closed:
            return
        self._error = str(error or "")
        self._unknown_submission = self._error.startswith("submission_unknown:")
        if isinstance(prepared, dict) and self._context is not None:
            self._comparison_rows = deepcopy(prepared.get("comparison_rows", []))
            self._context.update({key: deepcopy(value) for key, value in prepared.items()
                                  if key != "comparison_rows"})
        self._payload = deepcopy(payload) if isinstance(payload, dict) else None
        self._status = (f"{payload.get('state')} / {payload.get('termination_reason')}；仅比较，未应用。"
                        if self._payload is not None else "没有可验证报告；原设计未改变。")
        if self._stale or self._source_invalidated.is_set():
            self._status += " 来源已改变，报告仅供只读比较。"
        if self._cancel_intent:
            self._status += " 已请求取消，本次结果不可应用。"
        compute = (self._context or {}).get("project_compute")
        if compute is not None:
            state = ("submission_unknown" if self._unknown_submission else "failed" if self._error
                     else "cancelled" if (payload or {}).get("state") == "cancelled"
                     else "failed" if (payload or {}).get("state") == "failed" else "succeeded")
            compute.update_job(self._job_id, state)
        self.changed.emit()

    @Slot()
    def cancel(self):
        if self.busy:
            self._cancel_intent = True
            error = self._run_coordinator.request_cancel()
            self._status = "已请求取消；等待服务确认终态，不把取消意图当成完成。本次结果不会应用。"
            if error is not None:
                self._error = f"取消请求未确认：{error}；可重试同一作业的取消。"
            self.changed.emit()

    @Slot()
    def sourceChanged(self):
        self._stale = True
        self._source_invalidated.set()
        if self.busy:
            self.cancel()
        self._status = "来源、原始目标或项目已改变；需刷新并重新核对。本次报告不可应用。"
        self.changed.emit()

    @Slot(result=bool)
    def applySelectedRoute(self):
        if not self.canApply:
            self._error = "候选缺少完整独立审计，或来源/目标/材料/项目已改变；整个候选未应用。"
            self.changed.emit()
            return False
        apply = getattr(self.owner, "_apply_manufacturing_candidate", None)
        try:
            if not callable(apply) or not apply(self._payload, self._context, self._selected_snapshot()):
                raise ValueError("来源修订、未提交编辑、目标或材料已改变；整个候选未应用。")
            self._applied = True
            self._status = "所选工艺修复候选已显式应用；原始光学目标未放宽，可在设计窗口撤销。"
            self._error = ""
        except (ValueError, TypeError, RuntimeError, KeyError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
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
        if getattr(self.owner, "project_context", None) is None:
            self.cancel()
        return True

    @Slot()
    def detach(self):
        if self._closed:
            return
        if getattr(self.owner, "project_context", None) is None:
            self.cancel()
        self._closed = True
        self._source_invalidated.set()
        self._run_coordinator.detach_service_observer()

    def shutdown(self, timeout_ms=1000):
        self.detach()
        return self._run_coordinator.shutdown(timeout_ms)
