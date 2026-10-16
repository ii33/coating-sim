"""Project-default input resolver and UI owner for correction-mask optimization.

The coordinator owns only a queued optimization's view state. Resource editing
remains with the project workspace; calculation inputs are resolved from that
workspace's explicit default graph on every submission.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot

from coat_sim.core.project_dependencies import ProjectError

if TYPE_CHECKING:
    from .process import _OptimizationWorker


@dataclass(frozen=True)
class DefaultMaskOptimizationInputs:
    project_id: str
    project_revision: int
    configuration_id: str
    process_document_id: str
    process_name: str
    process_data: dict[str, Any]
    runtime: dict[str, Any]
    input_snapshot: dict[str, Any]
    closure_ids: frozenset[str]
    configuration_parameters: dict[str, Any]
    display_time_step_s: float


def resolve_default_mask_optimization_inputs(service) -> DefaultMaskOptimizationInputs:
    """Resolve only one bound project's designated configuration and closure."""
    if not getattr(service, "is_project_service", False):
        raise ProjectError("dependency_conflict", "修正板优化需要绑定一个项目资源服务。")
    binding = getattr(service, "binding", None)
    host = getattr(service, "_session_host", None)
    if binding is None or host is None or host.store_for(binding) is not service.store:
        raise ProjectError("dependency_conflict", "项目服务与修正板窗口的项目绑定不一致。")

    from coat_sim.core.project_default_loading import load_project_default

    # Keep the identity check and read-only draft snapshot under this session's
    # operation guard. The returned snapshot is detached before any task starts.
    with service._operation():
        plan = load_project_default(service.store)
        manifest = service.store.manifest
        if plan.project_id != binding.project_id:
            raise ProjectError("dependency_conflict", "默认工艺属于其他项目。")
        if plan.state == "empty_project":
            raise ProjectError("empty_project", "当前项目为空，没有默认沉积配置可用于优化。")
        if plan.state == "no_default":
            raise ProjectError("no_default", "当前项目尚未指定默认沉积配置。")
        if plan.state != "ready" or plan.graph is None:
            raise ProjectError("invalid_dependency_graph", "当前项目默认沉积配置不可用。")
        graph = plan.graph
        if (plan.project_revision != manifest["revision"] or
                plan.configuration_id != manifest["default_configuration_id"] or
                graph.get("project_id") != binding.project_id or
                graph.get("revision") != manifest["revision"] or
                graph.get("root_document_id") != plan.configuration_id):
            raise ProjectError("stale_project", "项目默认配置在解析期间发生变化，请重试。")
        rows = {row["id"]: row for row in graph.get("documents", [])}
        process_id = plan.process_document_id
        process_row = rows.get(process_id or "")
        if process_row is None or process_row.get("kind") != "process":
            raise ProjectError("invalid_dependency_graph", "默认配置没有有效的项目工艺。")
        if (not isinstance(graph.get("runtime"), dict) or
                not isinstance(graph.get("input_snapshot"), dict)):
            raise ProjectError("invalid_dependency_graph", "默认工艺依赖闭包或输入快照缺失。")

        workspace = host.resource_for(binding, "resource_workspace")
        if workspace is None:
            drafts = {}
        else:
            if workspace.store is not service.store or workspace.binding != binding:
                raise ProjectError("dependency_conflict", "默认依赖中的未保存修改属于其他项目会话。")
            drafts = workspace.snapshot().get("drafts", {})
        closure_ids = frozenset(rows)
        dirty = []
        for module_key, draft in drafts.items():
            nested = draft.get("nested_material", {}) if isinstance(draft, dict) else {}
            if not isinstance(draft, dict):
                continue
            pending_ui = draft.get("pending_ui") is not None
            nested_pending = (isinstance(nested, dict) and
                              nested.get("pending_ui") is not None)
            dirty_draft = (bool(draft.get("dirty")) or pending_ui or
                           (isinstance(nested, dict) and
                            (bool(nested.get("dirty")) or nested_pending)))
            if not dirty_draft:
                continue
            identity = draft.get("document_id")
            if identity in closure_ids:
                name = str(draft.get("data", {}).get("name", "")) if isinstance(draft.get("data"), dict) else ""
                display = name or module_key
                dirty.append(f"{display} ({module_key})")
        if dirty:
            raise ProjectError("dirty_default_dependency",
                               "默认工艺依赖存在未保存修改，请保存或放弃后再优化：" + "、".join(dirty))

        configuration = graph.get("configuration")
        parameters = configuration.get("parameters") if isinstance(configuration, dict) else None
        if (not isinstance(configuration, dict) or configuration.get("process_document_id") != process_id or
                not isinstance(parameters, dict)):
            raise ProjectError("invalid_dependency_graph", "默认沉积配置与工艺身份不匹配。")
        configured_settings = parameters.get("settings", {})
        if not isinstance(configured_settings, dict):
            raise ProjectError("invalid_dependency_graph", "默认沉积配置的 settings 必须为对象。")
        if "time_step_s" in parameters:
            display_time_step_s = float(parameters["time_step_s"])
        elif "time_steps" in configured_settings:
            display_time_step_s = float(service.time_fields(
                process_path=service.document_handle(process_id),
                time_steps=configured_settings["time_steps"])['time_step_s'])
        else:
            display_time_step_s = 0.025
        process_data = deepcopy(process_row["data"])
        return DefaultMaskOptimizationInputs(
            project_id=binding.project_id,
            project_revision=manifest["revision"],
            configuration_id=str(plan.configuration_id),
            process_document_id=str(process_id),
            process_name=str(process_data.get("name", "默认工艺")),
            process_data=process_data,
            runtime=deepcopy(graph["runtime"]),
            input_snapshot=deepcopy(graph["input_snapshot"]),
            closure_ids=closure_ids,
            configuration_parameters=deepcopy(parameters),
            display_time_step_s=display_time_step_s,
        )


def _optimization_masks(runtime: dict) -> list[dict]:
    """Build all installed bounds plus the next available installation slot."""
    machine = runtime.get("machine")
    if not isinstance(machine, dict):
        return []
    masks = []
    for index, entry in enumerate(machine.get("masks", [])):
        profile = entry.get("profile") if isinstance(entry, dict) else None
        if isinstance(profile, dict):
            points = profile.get("outer", [])
            if points:
                masks.append({"slot": index, "installed": True,
                              "z_mm": profile.get("z_mm", 0),
                              "x_min_mm": min(point[0] for point in points),
                              "x_max_mm": max(point[0] for point in points),
                              "y_min_mm": min(point[1] for point in points),
                              "y_max_mm": max(point[1] for point in points)})
                continue
        masks.append({"slot": index, "installed": False, "z_mm": 0,
                      "x_min_mm": 0, "x_max_mm": 0,
                      "y_min_mm": 0, "y_max_mm": 0})
    sources = machine.get("sources", [])
    source = sources[0] if sources else {"x_mm": 0, "y_mm": 0, "z_mm": 0}
    source_z = max((float(item.get("z_mm", 0)) for item in sources), default=0.0)
    substrate_z = float(machine.get("substrate_z_mm", 1000.0))
    z = (source_z + substrate_z) / 2.0
    extent = max(50.0, 0.5 * (float(machine.get("planet_offset_mm", 100)) + 100.0))
    cx = (float(source.get("x_mm", 0)) + float(machine.get("carrier_x_mm", 0))) / 2.0
    cy = (float(source.get("y_mm", 0)) + float(machine.get("carrier_y_mm", 0))) / 2.0
    for _ in range(max(2, len(masks) + 1) - len(masks)):
        masks.append({"slot": len(masks), "installed": False, "z_mm": z,
                      "x_min_mm": cx - extent, "x_max_mm": cx + extent,
                      "y_min_mm": cy - extent, "y_max_mm": cy + extent})
    return masks


class MaskOptimizationCoordinator(QObject):
    """Mask-window owner for immutable default-graph optimization tasks."""

    changed = Signal()
    optimizationChanged = Signal()
    maskOverwriteRequested = Signal(QUrl, int)

    def __init__(self, service, parent: QObject | None = None):
        super().__init__(parent)
        self.service = service
        self._editing_frozen = False
        self._inputs: DefaultMaskOptimizationInputs | None = None
        self._optimization: dict[str, Any] = {"busy": False, "message": "", "stage": "", "artifacts": {}}
        self._pending_masks: list[dict[str, Any]] = []
        self._workers: dict[str, _OptimizationWorker] = {}
        self._worker: _OptimizationWorker | None = None
        self._availability = "正在解析项目默认工艺与机器。"
        self._default_summary = ""
        self._pending_frozen_result: tuple[dict[str, Any], DefaultMaskOptimizationInputs | None] | None = None
        self._result_inputs: DefaultMaskOptimizationInputs | None = None
        self.refreshDefault()

    def get_optimization(self) -> dict:
        return deepcopy(self._optimization)

    def get_pendingMasks(self) -> list:
        return deepcopy(self._pending_masks)

    def get_optimizationMasks(self) -> list:
        if self._inputs is None:
            return []
        return _optimization_masks(self._inputs.runtime)

    def get_optimizationMaskCount(self) -> int:
        return len(self._inputs.runtime.get("machine", {}).get("masks", [])) if self._inputs else 0

    def get_optimizationSourceCount(self) -> int:
        return len(self._inputs.runtime.get("machine", {}).get("sources", [])) if self._inputs else 0

    def get_optimizationAvailability(self) -> str:
        if self._availability:
            return self._availability
        if self._inputs is None:
            return "项目默认工艺与机器不可用。"
        if not self.get_optimizationMaskCount():
            return "默认机器未安装修正板；可选择从零设计，并确认新板高度与 XY 包络。"
        if not any((list(range(self.get_optimizationMaskCount())) if step.get("mask_slots") is None else step["mask_slots"])
                   for step in self._inputs.process_data.get("steps", [])):
            return "默认工艺各层均未启用修正板；请为目标层选择修正板。"
        return "已解析项目默认工艺与机器；计算使用其已保存依赖闭包，结果生成独立副本。"

    def get_optimizationDefaultSummary(self) -> str:
        return self._default_summary

    def get_optimizationParameters(self) -> dict:
        if self._inputs is None:
            return {}
        parameters = deepcopy(self._inputs.configuration_parameters)
        parameters["_optimization_time_step_s"] = self._inputs.display_time_step_s
        return parameters

    def get_optimizationDefaultConfigurationId(self) -> str:
        return "" if self._inputs is None else self._inputs.configuration_id

    def get_optimizationDefaultRevision(self) -> int:
        return 0 if self._inputs is None else self._inputs.project_revision

    def get_optimizationReady(self) -> bool:
        return self._inputs is not None and not self._is_frozen()

    # Qt descriptors stay at the presentation boundary; Python callers use
    # the explicit getters above so static types reflect the returned values.
    optimization = Property(dict, get_optimization, notify=optimizationChanged)
    pendingMasks = Property(list, get_pendingMasks, notify=optimizationChanged)
    optimizationMasks = Property(list, get_optimizationMasks, notify=changed)
    optimizationMaskCount = Property(int, get_optimizationMaskCount, notify=changed)
    optimizationSourceCount = Property(int, get_optimizationSourceCount, notify=changed)
    optimizationAvailability = Property(str, get_optimizationAvailability, notify=changed)
    optimizationDefaultSummary = Property(str, get_optimizationDefaultSummary, notify=changed)
    optimizationParameters = Property(dict, get_optimizationParameters, notify=changed)
    optimizationDefaultConfigurationId = Property(str, get_optimizationDefaultConfigurationId, notify=changed)
    optimizationDefaultRevision = Property(int, get_optimizationDefaultRevision, notify=changed)
    optimizationReady = Property(bool, get_optimizationReady, notify=changed)

    def _is_frozen(self) -> bool:
        gate = getattr(self.service, "_gate", None)
        return bool(self._editing_frozen or getattr(gate, "frozen", False))

    @Slot(result=bool)
    def refreshDefault(self) -> bool:
        try:
            inputs = resolve_default_mask_optimization_inputs(self.service)
            self._inputs = inputs
            runtime = inputs.runtime
            machine = runtime.get("machine", {})
            machine_name = str(machine.get("name") or inputs.process_data.get("machine_file", "机器"))
            self._default_summary = f"默认工艺：{inputs.process_name} · 默认机器：{Path(machine_name).name}"
            self._availability = ""
            self.changed.emit()
            return True
        except Exception as exc:
            self._inputs = None
            self._default_summary = ""
            self._availability = str(exc)
            self.changed.emit()
            return False

    def _verify_inputs_current(self, inputs: DefaultMaskOptimizationInputs) -> None:
        manifest = self.service.store.manifest
        if (manifest["id"] != inputs.project_id or
                manifest["revision"] != inputs.project_revision or
                manifest["default_configuration_id"] != inputs.configuration_id or
                self.service.binding.project_id != inputs.project_id):
            raise ProjectError("stale_project", "项目默认工艺或依赖在开始前发生变化，请重新解析后再试。")

    @Slot("QVariantMap", "QVariantMap")
    def startOptimization(self, settings: dict, options: dict) -> None:
        worker = None
        job_key = None
        previous_worker = self._worker
        displayed_inputs = self._inputs
        try:
            if self._is_frozen():
                raise ProjectError("project_editing_frozen", "项目正在关闭或被冻结，不能提交优化。")
            if self._workers:
                raise ProjectError("optimization_busy", "本窗口已有优化任务运行；请等待它完成或取消后再开始。")
            inputs = resolve_default_mask_optimization_inputs(self.service)
            if (displayed_inputs is not None and
                    (displayed_inputs.project_id != inputs.project_id or
                     displayed_inputs.project_revision != inputs.project_revision or
                     displayed_inputs.configuration_id != inputs.configuration_id)):
                raise ProjectError("stale_project", "默认配置已变化。请重新解析默认工艺与机器后再开始。")
            self._verify_inputs_current(inputs)
            parameters = inputs.configuration_parameters
            configured_settings = deepcopy(parameters.get("settings", {}))
            options = dict(options)
            time_step_edited = bool(options.pop("_time_step_edited", False))
            expected_configuration = options.pop("_default_configuration_id", None)
            expected_revision = options.pop("_default_project_revision", None)
            if (expected_configuration is not None and
                    expected_configuration != inputs.configuration_id):
                raise ProjectError("stale_project", "默认配置已变化。请重新解析默认工艺与机器后再开始。")
            if expected_revision is not None and int(expected_revision) != inputs.project_revision:
                raise ProjectError("stale_project", "项目依赖已变化。请重新解析默认工艺与机器后再开始。")
            ui_settings = dict(settings)
            time_step_s = ui_settings.pop("time_step_s", None)
            settings = {**configured_settings, **ui_settings}
            # A configuration stored as a step count is exact source data. Keep
            # that count unless the user explicitly edits the displayed step.
            if time_step_edited:
                settings.pop("time_steps", None)
                if time_step_s is None:
                    raise ValueError("请输入有效时间步长")
                settings["time_steps"] = self.service.time_fields(
                    process_path=self.service.document_handle(inputs.process_document_id),
                    time_step_s=time_step_s)["time_steps"]
            elif "time_step_s" in parameters:
                settings.pop("time_steps", None)
                settings["time_steps"] = self.service.time_fields(
                    process_path=self.service.document_handle(inputs.process_document_id),
                    time_step_s=parameters["time_step_s"])["time_steps"]
            elif "time_steps" not in configured_settings:
                settings.pop("time_steps", None)
                if time_step_s is not None:
                    settings["time_steps"] = self.service.time_fields(
                        process_path=self.service.document_handle(inputs.process_document_id),
                        time_step_s=time_step_s)["time_steps"]
            for name, fallback in (("backend", "DirectML"),
                                   ("fast_cycle", True),
                                   ("fast_cycle_residual_percent", 100)):
                if name not in options:
                    options[name] = deepcopy(parameters.get(name, fallback))
            settings = _integer_controls(dict(settings))
            options = _integer_controls(dict(options))
            snapshot = deepcopy(inputs.input_snapshot)
            step_s = settings.pop("time_step_s", None)
            if step_s is not None:
                settings["time_steps"] = self.service.time_fields(
                    process_path=snapshot, time_step_s=step_s)["time_steps"]
            files = snapshot.get("files", [])
            job_key = hashlib.sha256(json.dumps({
                "project_id": inputs.project_id,
                "configuration_id": inputs.configuration_id,
                "files": [(item["path"], item["sha256"]) for item in files],
                "settings": settings, "options": options,
            }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if job_key in self._workers:
                self._optimization["message"] = "相同默认输入已在队列中；请等待现有任务完成。"
                self.optimizationChanged.emit()
                return
            estimate = self.service.estimate_mask_optimization(settings, snapshot, options=options)
            self._optimization = {
                "busy": True, "stage": "queued", "artifacts": {},
                "message": "已冻结项目默认工艺与机器输入并加入计算队列。",
                "estimated_remaining_s": estimate.get("estimated_s"),
                "estimate_basis": estimate.get("basis"),
                "evaluations": 0,
                "memory_warning": estimate.get("memory_warning", False),
                "memory_estimate_label": estimate.get("memory_estimate_label", ""),
                "max_evaluations": options.get("max_evaluations", 48),
                "project_id": inputs.project_id,
                "project_revision": inputs.project_revision,
                "configuration_id": inputs.configuration_id,
                "process_document_id": inputs.process_document_id,
            }
            if estimate.get("memory_warning"):
                self._optimization["message"] += "；" + estimate.get("memory_estimate_label", "内存可能不足，计算仍会提交")
            from .process import _OptimizationWorker
            worker = _OptimizationWorker(self.service, snapshot, dict(settings), dict(options),
                                         estimate=estimate)
            self._worker = worker
            self._workers[job_key] = worker
            worker.progress.connect(lambda value, w=worker: self._optimization_progress(value) if w is self._worker else None)
            worker.resultReady.connect(lambda value, w=worker, job_inputs=inputs:
                                       self._optimization_result(value, job_inputs) if w is self._worker else None)
            worker.failed.connect(lambda message, w=worker: self._optimization_failed(message) if w is self._worker else None)
            # A detached observer can finish after its Qt UI owner was
            # disposed. Only the still-owned observer may update UI signals.
            worker.finished.connect(lambda w=worker, key=job_key:
                                    self._optimization_finished(w, key)
                                    if self._workers.get(key) is w else None)
            self.optimizationChanged.emit()
            worker.start()
            if not worker.isRunning() and not worker.isFinished():
                raise RuntimeError("无法创建优化计算线程")
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            if worker is not None and not worker.isRunning() and not worker.isFinished():
                message = worker.fail_start(exc)
                if job_key is not None:
                    self._workers.pop(job_key, None)
                if self._worker is worker:
                    self._worker = previous_worker
                worker.deleteLater()
            self._optimization.update(busy=bool(self._workers), stage="failed",
                                      message=f"无法开始优化：{message}")
            inputs_unavailable = isinstance(exc, ProjectError) and exc.code in {
                    "empty_project", "no_default", "invalid_dependency_graph",
                    "missing_dependency", "integrity_error", "stale_project",
                    "dependency_conflict", "dirty_default_dependency",
                    "project_closed", "project_closing", "stale_session"}
            if inputs_unavailable:
                self._inputs = None
                self._default_summary = ""
                self._availability = str(exc)
                self.changed.emit()
            self.optimizationChanged.emit()

    @Slot(dict)
    def _optimization_progress(self, value: dict) -> None:
        self._optimization.update(value)
        names = {"group_complete": "当前源板组完成", "verify_full": "全工艺复算",
                 "baseline": "计算无遮板基线", "current": "计算原板基线",
                 "response": "扫描初形位置", "optimize": "调整轮廓",
                 "verify": "原网格复算", "complete": "优化完成"}
        self._optimization["message"] = names.get(str(value.get("stage") or ""), "正在优化")
        if self._optimization.get("memory_warning"):
            self._optimization["message"] += "；" + self._optimization.get("memory_estimate_label", "内存可能不足")
        self.optimizationChanged.emit()

    @Slot(object)
    def _optimization_result(self, value: dict, job_inputs: DefaultMaskOptimizationInputs | None = None) -> None:
        if self._is_frozen():
            payload = {key: deepcopy(value[key]) for key in (
                "pending_masks", "_asset", "result_path", "report", "report_path",
                "artifacts", "_asset_warning") if key in value}
            payload["simulation"] = {}
            self._pending_frozen_result = (payload, job_inputs)
            return
        try:
            if not isinstance(value.get("simulation"), dict):
                raise ValueError("优化结果未包含仿真数据")
            self._pending_masks = deepcopy(value.get("pending_masks", []))
            self._result_inputs = job_inputs
            path = Path(value["result_path"])
            report = value["report"]
            groups = report.get("group_reports", [report])
            improved = sum(bool(group.get("improved_on_search_grid")) for group in groups)
            invalid_original = sum(group.get("original_boards_search") is None for group in groups)
            comparison = f" · {len(groups)} 组中 {improved} 组搜索改善"
            if invalid_original:
                comparison += f" · {invalid_original} 组原板不可计算，返回可行候选"
            result_owner = (f"结果所属配置 {job_inputs.configuration_id} · r{job_inputs.project_revision} · "
                            f"项目 {job_inputs.project_id[:8]} · " if job_inputs is not None else "")
            message = (f"优化完成 · {result_owner}全工艺复算 CV {report['best_verified']['cv_percent']:.4f}%"
                       f"{comparison} · 新修正板设计待保存；原文件未修改")
            if value.get("_asset_warning"):
                message += "；" + value["_asset_warning"]
            self._optimization.update(artifacts=deepcopy(value.get("artifacts", {})),
                                      report_path=value.get("report_path", ""),
                                      result_path=str(path), message=message)
            if job_inputs is not None:
                self._optimization.update(result_project_id=job_inputs.project_id,
                                          result_project_revision=job_inputs.project_revision,
                                          result_configuration_id=job_inputs.configuration_id,
                                          result_process_document_id=job_inputs.process_document_id)
        except Exception as exc:
            self._optimization.update(message=f"优化结果已生成，导出信息失败：{exc}",
                                      artifacts=deepcopy(value.get("artifacts", {})))
        self.optimizationChanged.emit()

    @Slot(str)
    def _optimization_failed(self, message: str) -> None:
        self._optimization.update(message=message, stage="cancelled" if "取消" in message else "failed")
        self.optimizationChanged.emit()

    def _optimization_finished(self, worker: _OptimizationWorker | None = None, key: str | None = None) -> None:
        worker = worker or self._worker
        if key is not None:
            self._workers.pop(key, None)
        if worker is self._worker:
            self._worker = None
        self._optimization["busy"] = bool(self._workers)
        self.optimizationChanged.emit()
        if worker is not None:
            worker.deleteLater()

    @Slot()
    def cancelOptimization(self) -> None:
        if self._workers:
            for worker in self._workers.values():
                worker.cancel_event.set()
            self._optimization["message"] = "正在取消本窗口任务，等待计算检查点。"
            self.optimizationChanged.emit()

    def setEditingFrozen(self, frozen: bool) -> None:
        self._editing_frozen = bool(frozen)
        if not frozen and self._pending_frozen_result is not None:
            gate = getattr(self.service, "_gate", None)
            if not getattr(gate, "frozen", False):
                (pending, inputs), self._pending_frozen_result = self._pending_frozen_result, None
                self._optimization_result(pending, inputs)

    def detach_observers(self) -> None:
        """Detach UI observers; scheduler-owned submitted jobs keep running."""
        from .process import _reap_process_worker
        workers = list(self._workers.values())
        self._workers.clear()
        self._worker = None
        self._pending_frozen_result = None
        self._optimization["busy"] = False
        for worker in workers:
            worker.setParent(None)
            worker.requestInterruption()
            _reap_process_worker(worker)

    @Slot(QUrl, int, bool)
    def saveOptimizedMask(self, url: QUrl, index: int, overwrite: bool = False) -> None:
        try:
            if self._is_frozen():
                raise ProjectError("project_editing_frozen", "项目正在关闭或被冻结，不能保存优化结果。")
            inputs = self._result_inputs
            if inputs is None or not 0 <= index < len(self._pending_masks):
                raise ValueError("没有可保存的优化修正板")
            host = getattr(self.service, "_session_host", None)
            binding = getattr(self.service, "binding", None)
            if (host is None or binding is None or binding.project_id != inputs.project_id
                    or host.store_for(binding) is not self.service.store):
                raise ProjectError("dependency_conflict", "优化结果不属于当前项目会话，无法保存。")
            profile = deepcopy(self._pending_masks[index]["profile"])
            display_name = Path(url.toLocalFile()).stem or "优化修正板"
            # Display metadata is independent of the candidate overview.
            # Naming this new resource must preserve the original summary,
            # notes and every physical field in the generated candidate.
            profile["name"] = display_name
            identity = self.service.create_resource("mask", profile)
            saved = self.service.load_document(identity)
            self._optimization["message"] = (f"新修正板已复制到结果所属项目"
                f"（默认配置 {inputs.configuration_id}）：{saved.path}")
            self._optimization.setdefault("artifacts", {})["saved_mask_document_id"] = identity
        except Exception as exc:
            self._optimization["message"] = f"保存修正板失败：{exc}"
        self.optimizationChanged.emit()

    @Slot(str, int)
    def saveOptimizedMaskNamed(self, name: str, index: int) -> None:
        label = (Path(str(name)).name or "优化修正板").strip()
        if not label.lower().endswith(".cmsk"):
            label += ".cmsk"
        self.saveOptimizedMask(QUrl.fromLocalFile("/" + label), index, False)


def _integer_controls(value, key=""):
    integer_keys = {"time_steps", "grid_size", "search_grid_size", "max_evaluations",
                    "seed_grid_points", "control_points", "slot", "mask_slots",
                    "fixed_mask_slots", "source_slot"}
    if isinstance(value, dict):
        return {name: _integer_controls(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_integer_controls(item, key) for item in value]
    if key in integer_keys and type(value) is float and value.is_integer():
        return int(value)
    return value
