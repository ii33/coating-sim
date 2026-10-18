"""Qt Quick presentation bridge for deposition calculation.

The bridge owns only window state and a worker thread.  File rules, simulation,
runtime estimates, history, and PDF export are provided by ``CoatService``.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import threading
import time
import traceback
import json
import hashlib
from copy import deepcopy
from uuid import uuid4
from coat_sim.project_session import OperationBinding
from coat_sim.scheduler import SchedulerClient, freeze_spec
from coat_sim.scheduler.desktop import (RemoteCancelEvent, observe_job,
                                        resolve_unknown_submission)
from coat_sim.scheduler.remote_client import RemoteSubmissionUnknown

from PySide6.QtCore import QObject, Property, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from coat_sim.core.api import CoatService
from coat_sim.core.data_files import data_path, design_directory
from coat_sim.core.simulator import Settings
from coat_sim.ui.deposition_plot import sample_nearest_grid
from coat_sim.ui.surface_figure_presentation import format_surface_figure_summary
from coat_sim.ui.uniformity_presentation import format_uniformity_rows


def _assert_selected_backend_result(result: dict, requested_backend: str) -> None:
    """Never present a CPU result as the selected explicit GPU execution."""
    actual = result.get("device")
    prefix = ("GPU (CuPy)" if requested_backend == "GPU" else
              "GPU (DirectML:" if requested_backend == "DirectML" else "CPU")
    if not isinstance(actual, str) or not actual or not all(
            part.strip().startswith(prefix) for part in actual.split("、")):
        raise ValueError("计算结果与所选设备不符；拒绝发布本次结果。")


def _detach_error_tracebacks(error: BaseException | None) -> None:
    """Keep UI error identity/text without keeping worker arrays alive.

    A traceback owns its simulation frame and GPU/CPU tensors. A queued signal
    must not retain those frames, including nested causes and contexts. Preserve
    the exception objects and their chains for diagnostics, but remove frames.
    """
    pending = [error] if error is not None else []
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend(item for item in (current.__cause__, current.__context__)
                       if item is not None)
        # Clear completed frame locals too: nested exceptions or debugging
        # references may otherwise keep a frame cycle alive until a later GC.
        if current.__traceback__ is not None:
            traceback.clear_frames(current.__traceback__)
        # ServiceError is a frozen dataclass; clear BaseException storage
        # without invoking its generated __setattr__.
        object.__setattr__(current, "__traceback__", None)


class ThicknessImageProvider(QQuickImageProvider):
    """Copy the latest scalar map into a texture for the Qt Quick scene graph."""

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.Image)
        self._lock = threading.Lock()
        self._image = QImage(1, 1, QImage.Format_RGBA8888)
        self._image.fill(0)
        self._legend = QImage(1, 1, QImage.Format_RGBA8888)
        self._legend.fill(0)

    def set_field(self, field: object) -> tuple[float, float]:
        import numpy as np
        from matplotlib import colormaps

        values = np.asarray(field)
        if values.ndim != 2 or not values.size:
            raise ValueError("膜厚结果须为二维网格")
        # Extrema use bounded tiles; only the visual preview is resampled.
        lower, upper = float("inf"), float("-inf")
        for row in range(0, values.shape[0], 512):
            for column in range(0, values.shape[1], 512):
                tile = values[row:row + 512, column:column + 512]
                valid = tile[np.isfinite(tile)]
                if valid.size:
                    lower = min(lower, float(valid.min()))
                    upper = max(upper, float(valid.max()))
        if lower == float("inf"):
            raise ValueError("膜厚结果没有有效点")
        if max(values.shape) > 1024:
            scale = 1024 / max(values.shape)
            row_count = max(1, min(values.shape[0], round(values.shape[0] * scale)))
            column_count = max(1, min(values.shape[1], round(values.shape[1] * scale)))
            rows = np.linspace(0, values.shape[0] - 1, row_count, dtype=int)
            columns = np.linspace(0, values.shape[1] - 1, column_count, dtype=int)
            values = values[np.ix_(rows, columns)]
        finite = np.isfinite(values)
        spread = upper - lower
        scale = np.zeros_like(values) if spread == 0 else (values - lower) / spread
        scale[~finite] = 0
        color_map = colormaps["turbo"]
        rgba = np.ascontiguousarray(np.flipud(
            color_map(np.clip(scale, 0, 1), bytes=True)))
        rgba[:, :, 3] = np.flipud(finite).astype(np.uint8) * 255
        height, width = values.shape
        image = QImage(rgba.data, width, height, width * 4,
                       QImage.Format_RGBA8888).copy()
        levels = np.linspace(1.0, 0.0, 256).reshape(256, 1)
        legend_bytes = np.ascontiguousarray(
            color_map(levels, bytes=True))
        legend = QImage(legend_bytes.data, 1, 256, 4,
                        QImage.Format_RGBA8888).copy()
        with self._lock:
            self._image = image
            self._legend = legend
        return lower, upper

    def clear(self):
        image = QImage(1, 1, QImage.Format_RGBA8888)
        image.fill(0)
        with self._lock:
            self._image = image
            self._legend = image.copy()

    def requestImage(self, image_id: str, size: QSize,
                     requested_size: QSize) -> QImage:
        with self._lock:
            image = (self._legend if image_id.startswith("legend")
                     else self._image).copy()
        size.setWidth(image.width())
        size.setHeight(image.height())
        return image


class MainBridge(QObject):
    changed = Signal()
    inputsChanged = Signal()
    showQueueRequested = Signal()
    resultReady = Signal(int, object, float, object)
    progressReady = Signal(int, object)
    cancelTransportStatus = Signal(str, str)

    _ALGORITHM_LABELS = {
        "fast": "工艺积分",
        "reference": "时间收敛检查",
        "optimize": "转速局部优化",
    }

    _INT_FIELDS = {"time_steps", "grid_size"}
    _VISIBLE_FIELDS = {
        "carrier_rpm", "carrier_phase_deg", "planet_phase_deg",
        "time_steps", "grid_size",
    }

    def __init__(self, service: CoatService | None = None, *, project_context=None,
                 result_view_only: bool = False) -> None:
        super().__init__()
        if type(result_view_only) is not bool:
            raise TypeError("result_view_only must be a bool")
        self._result_view_only = result_view_only
        self.project_context = project_context
        self.service: CoatService
        self._project_compute = None
        if project_context is not None:
            from coat_sim.core.project_compute import ProjectComputeWorkspace
            host = getattr(project_context, "host", None)
            binding = getattr(project_context, "binding", None)
            if host is None or binding is None:
                raise ValueError("Project deposition requires an explicitly bound project context")
            self._project_compute = ProjectComputeWorkspace.for_project(host, binding)
            context_service = self._project_compute.service
            if context_service is None:
                raise ValueError("Project deposition requires a bound project compute workspace")
            if service is not None and service is not context_service:
                raise ValueError("Project deposition service must be owned by its project workspace")
            self.service = context_service
        else:
            self.service = service or CoatService(restrict_to_root=False)
        from coat_sim.ui.compute_target import ComputeTarget
        self._compute_target = ComputeTarget(self.service.root, "deposition", self,
                                             client_factory=SchedulerClient)
        self._compute_target.changed.connect(self._on_compute_target_changed)
        from .asset_browser import AssetBrowserBridge
        import weakref
        weak_owner = weakref.ref(self)
        def open_asset(path):
            editor = weak_owner()
            if editor is not None:
                editor.chooseFile("process", QUrl.fromLocalFile(str(path)))
        self._asset_browser = AssetBrowserBridge(self.service, 'process', open_asset, self)
        self.image_provider = ThicknessImageProvider()
        defaults = Settings()
        self._values = {key: str(getattr(defaults, key))
                        for key in self._VISIBLE_FIELDS}
        self._values["time_step_s"] = "0.025"
        # The visible step is the effective T/N, rounded for presentation. Keep
        # the user's requested maximum step separately: feeding the displayed
        # T/N back into ceil(T/step) can add a sample on every refresh/submit.
        self._requested_time_step_s = "0.025"
        self._time_basis = "time_step_s"
        self._paths = {"process": ""}
        self._active_configuration_parameters: dict = {}
        self._project_configuration_name = "沉积配置"
        self._project_configuration_description = ""
        self._project_configuration_notes = ""
        self._pending_information_edit = None
        self._geometry_rows: list[str] = []
        self._device = "CPU"
        self._fast_cycle = True
        self._algorithm = "fast"
        self._progress_value = -1.0
        self._algorithm_report_text = ""
        # Zero is a UI-only sentinel; it is omitted from execution options.
        self._algorithm_budget_s = 0
        self._fast_threshold_percent = 100.0
        self._fast_threshold_text = "100"
        self._fast_threshold_invalid = False
        self._busy = False
        self._queued_keys = {}
        self._job_cancel_events = {}
        self._cancel_event: threading.Event | None = None
        self._job_id = 0
        self._revision = 0
        self._started_revision = 0
        self._status = "选择工艺后计算膜厚。"
        self._estimate = ""
        self._memory_estimate = ""
        self._metrics: list[str] = []
        self._uniformity_result_notice = ""
        self._uniformity_result_notice_severity = "info"
        self._surface_figure_settings: dict | None = None
        self._history: list[str] = []
        self._result: dict | None = None
        self._project_operations: dict[int, OperationBinding] = {}
        self._project_queue_ids: dict[int, str] = {}
        self._deferred_project_finishes: dict[int, tuple] = {}
        self._display_field = None
        self._asset_run_id = ""
        self._loaded_asset_path = ""
        self._queue_result_asset_path = ""
        self._asset_name = "膜厚仿真结果"
        self._pdf_sections = ["heatmap", "thickness", "uniformity"]
        self._export_inputs: dict | None = None
        self._pending_export_inputs: dict | None = None
        self._image_revision = 0
        self._image_min = 0.0
        self._image_max = 0.0
        self._image_unit = "nm"
        self._axis_bounds = {"x_min": 0.0, "x_max": 0.0,
                             "y_min": 0.0, "y_max": 0.0}
        self.resultReady.connect(self._finish, Qt.QueuedConnection)
        self.cancelTransportStatus.connect(self._on_cancel_transport_status, Qt.ConnectionType.QueuedConnection)
        self.progressReady.connect(self._on_progress, Qt.QueuedConnection)
        self._refresh_history()
        if self._project_compute is not None and not self._result_view_only:
            restored = self._project_compute.has_pending_deposition_draft or self._project_compute.recovery_restored
            self._project_compute.register_draft_provider(
                self._project_draft_signature, restore=self._restore_project_draft)
            self._project_compute.register_thaw_callback(self._on_project_unfrozen)
            if not restored:
                self.loadDefaultConfiguration()
                self._project_compute.mark_draft_saved()
            else:
                self.refreshEstimate()
        elif not self._result_view_only:
            self.refreshEstimate()

    @Property(QObject, constant=True)
    def assetBrowser(self):
        return self._asset_browser

    @Property(QObject, constant=True)
    def computeTarget(self):
        return self._compute_target

    @Property(bool, notify=changed)
    def projectMode(self) -> bool:
        return self._project_compute is not None

    def _on_compute_target_changed(self):
        rows = self._compute_target.devices
        target = next((row for row in rows if row["id"] == self._compute_target.deviceId), None)
        if target is not None and target["available"]:
            selected = target["backend"]
            if selected != self._device:
                self._device = selected
                self._revision += 1
                self.inputsChanged.emit()
                self.changed.emit()
                self.refreshEstimate()

    @Property(QUrl, constant=True)
    def processDirectory(self):
        if self._project_compute is not None:
            return QUrl.fromLocalFile(str(self._project_directory("data", "processes")))
        return QUrl.fromLocalFile(str(design_directory("process")))

    @Property(QUrl, constant=True)
    def resultsDirectory(self):
        if self._project_compute is not None:
            return QUrl.fromLocalFile(str(self._project_directory("data", "results")))
        folder = data_path("results")
        folder.mkdir(parents=True, exist_ok=True)
        return QUrl.fromLocalFile(str(folder))

    @Property(QUrl, constant=True)
    def assetsDirectory(self):
        if self._project_compute is not None:
            return QUrl.fromLocalFile(str(self._project_directory("data", "assets")))
        folder = data_path("assets")
        folder.mkdir(parents=True, exist_ok=True)
        return QUrl.fromLocalFile(str(folder))

    def _project_directory(self, *parts: str) -> Path:
        if self._project_compute is None:
            raise RuntimeError("Project directory requested in legacy mode")
        path = self._project_compute.owned_path(
            self._project_compute.root.joinpath(*parts), allow_missing=True)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @Property("QVariantMap", notify=inputsChanged)
    def fields(self) -> dict:
        return dict(self._values)

    @Property("QVariantMap", notify=inputsChanged)
    def paths(self) -> dict:
        return dict(self._paths)

    @Property(list, notify=changed)
    def projectConfigurations(self) -> list[dict]:
        if self._project_compute is None:
            return []
        try:
            return self._project_compute.configurations()
        except (ValueError, OSError, KeyError):
            return []

    @Property(str, notify=changed)
    def projectConfigurationId(self) -> str:
        return "" if self._project_compute is None else (self._project_compute.configuration_id or "")

    @Property(bool, notify=changed)
    def projectEditingFrozen(self) -> bool:
        return bool(self._project_compute is not None and self._project_compute.editing_frozen)

    def _project_draft_signature(self) -> dict:
        """Return detached editable values for close-preflight change detection."""
        return {
            "pending_information": deepcopy(self._pending_information_edit),
            "configuration_name": self._project_configuration_name,
            "configuration_description": self._project_configuration_description,
            "configuration_notes": self._project_configuration_notes,
            "fields": dict(self._values),
            "device": self._device,
            "algorithm": self._algorithm,
            "algorithm_budget_s": self._algorithm_budget_s,
            "fast_cycle": self._fast_cycle,
            "fast_cycle_residual_percent": self._fast_threshold_percent,
            "requested_time_step_s": self._requested_time_step_s,
            "time_basis": self._time_basis,
        }

    def _restore_project_draft(self, draft: dict) -> None:
        """Restore editable controls only; persisted jobs are never resubmitted."""
        if self._project_compute is None:
            raise ValueError("A project draft requires a bound compute workspace")
        if not isinstance(draft, dict):
            raise ValueError("Project recovery draft must be an object")
        self._pending_information_edit = deepcopy(draft.get("pending_information"))
        self._project_configuration_name = str(draft.get("configuration_name", "沉积配置"))
        self._project_configuration_description = str(draft.get("configuration_description", ""))
        self._project_configuration_notes = str(draft.get("configuration_notes", ""))
        values = draft.get("fields", {})
        if not isinstance(values, dict):
            raise ValueError("Project recovery fields must be an object")
        self._values = {key: str(value) for key, value in values.items()
                        if key in self._VISIBLE_FIELDS or key == "time_step_s"}
        defaults = Settings()
        for key in self._VISIBLE_FIELDS:
            self._values.setdefault(key, str(getattr(defaults, key)))
        self._values.setdefault("time_step_s", "0.025")
        self._device = draft.get("device", self._device)
        algorithm = draft.get("algorithm", self._algorithm)
        if algorithm in self._ALGORITHM_LABELS:
            self._algorithm = algorithm
        self._algorithm_budget_s = max(0, min(300, int(draft.get("algorithm_budget_s", 0))))
        self._fast_cycle = bool(draft.get("fast_cycle", self._fast_cycle))
        self._fast_threshold_percent = float(draft.get(
            "fast_cycle_residual_percent", self._fast_threshold_percent))
        self._fast_threshold_text = str(self._fast_threshold_percent)
        self._requested_time_step_s = str(draft.get(
            "requested_time_step_s", self._values.get("time_step_s", "0.025")))
        self._time_basis = ("time_step_s" if draft.get("time_basis") == "time_step_s"
                            else "time_steps")
        process_id = self._project_compute.process_document_id
        self._paths["process"] = (str(self._project_compute.process_handle(process_id))
                                  if process_id else "")
        self._active_configuration_parameters = {}
        if process_id:
            resolved = self._project_compute.resolve_process(process_id)
            self._refresh_geometry_preview(
                self._project_compute.service.load("process", resolved["input_snapshot"]))
        else:
            self._geometry_rows = []
        self._revision += 1
        self.inputsChanged.emit()
        self.changed.emit()

    def _activate_project_configuration(self, configuration: dict,
                                        configuration_id: str | None,
                                        process_graph: dict) -> bool:
        if self._project_compute is None:
            return False
        try:
            process_id = configuration["process_document_id"]
            snapshot = process_graph["input_snapshot"]
            if not isinstance(snapshot, dict):
                raise ValueError("项目工艺输入快照缺失")
            handle = self._project_compute.process_handle(process_id)
            parameters = deepcopy(configuration.get("parameters", {}))
            settings = parameters.get("settings", {})
            if not isinstance(settings, dict):
                raise ValueError("项目配置 settings 必须是对象")
            for key in self._VISIBLE_FIELDS:
                if key in settings:
                    self._values[key] = str(settings[key])
            self._active_configuration_parameters = parameters
            self._project_configuration_name = str(configuration.get("name", "沉积配置"))
            self._project_configuration_description = str(configuration.get("description", ""))
            self._project_configuration_notes = str(configuration.get("notes", ""))
            self._paths["process"] = str(handle)
            self._fast_cycle = bool(parameters.get("fast_cycle", self._fast_cycle))
            self._fast_threshold_percent = float(parameters.get(
                "fast_cycle_residual_percent", self._fast_threshold_percent))
            self._fast_threshold_text = str(self._fast_threshold_percent)
            self._fast_threshold_invalid = False
            self._algorithm = parameters.get("algorithm", self._algorithm)
            algorithm_options = parameters.get("algorithm_options", {})
            self._algorithm_budget_s = int(algorithm_options.get("budget_s") or 0)
            backend = parameters.get("backend")
            if backend in {"CPU", "GPU", "DirectML"}:
                self._device = backend
            if "time_step_s" in parameters:
                self._requested_time_step_s = f'{float(parameters["time_step_s"]):.12g}'
                self._time_basis = "time_step_s"
            else:
                self._time_basis = "time_steps"
                self._requested_time_step_s = self._values.get("time_step_s", "0.025")
            self._project_compute.select_context(
                configuration_id=configuration_id, process_document_id=process_id)
            document = self._project_compute.service.load("process", snapshot)
            if document.issues or not (document.runtime or {}).get("machine"):
                raise ValueError("项目工艺或机器依赖无效")
            self._refresh_geometry_preview(document)
            self._update_time(self._time_basis)
            self._revision += 1
            self._project_compute.mark_draft_saved()
            self._status = f"已加载项目配置：{configuration.get('name', '沉积配置')}。"
            self.inputsChanged.emit()
            self.changed.emit()
            self.refreshEstimate()
            return True
        except (ValueError, TypeError, OSError, KeyError, OverflowError) as exc:
            self._status = f"项目配置读取失败：{exc}"
            self._uniformity_result_notice = self._status
            self._uniformity_result_notice_severity = "error"
            self.changed.emit()
            return False

    @Slot(result=bool)
    def loadDefaultConfiguration(self) -> bool:
        """Load only the manifest-selected managed project configuration."""
        if self._project_compute is None or self._result_view_only:
            self._status = "当前窗口未绑定项目。"
            self.changed.emit()
            return False
        try:
            plan = self._project_compute.load_default()
            if not plan.configured:
                self._paths["process"] = ""
                self._active_configuration_parameters = {}
                self._geometry_rows = []
                self._status = ("项目还没有沉积工艺。请选择工艺文件导入到项目，再保存配置。"
                                if plan.state == "empty_project" else
                                "项目没有默认沉积配置。请加载项目工艺并保存配置。")
                self.inputsChanged.emit()
                self.changed.emit()
                self.refreshEstimate()
                return False
            graph = plan.graph
            if not isinstance(graph, dict) or not isinstance(graph.get("configuration"), dict):
                raise ValueError("项目默认配置解析不完整")
            return self._activate_project_configuration(
                graph["configuration"], plan.configuration_id, graph)
        except (ValueError, TypeError, OSError, KeyError) as exc:
            self._status = f"项目默认配置读取失败：{exc}"
            self._uniformity_result_notice = self._status
            self._uniformity_result_notice_severity = "error"
            self.changed.emit()
            return False

    @Slot(str, result=bool)
    def loadConfiguration(self, configuration_id: str) -> bool:
        if self._project_compute is None or self._result_view_only:
            return False
        try:
            loaded = self._project_compute.load_configuration(configuration_id)
            return self._activate_project_configuration(
                loaded["configuration"], configuration_id, loaded["process_graph"])
        except (ValueError, TypeError, OSError, KeyError) as exc:
            self._status = f"项目配置读取失败：{exc}"
            self.changed.emit()
            return False

    def _configuration_parameters(self) -> dict:
        settings = asdict(self._settings())
        parameters: dict = {
            "settings": settings,
            "backend": self._device,
            "fast_cycle": self._fast_cycle,
            "fast_cycle_residual_percent": self._fast_threshold_percent,
            "algorithm": self._algorithm,
            "algorithm_options": self._algorithm_options(),
        }
        if self._time_basis == "time_step_s":
            parameters["settings"].pop("time_steps", None)
            parameters["time_step_s"] = float(self._requested_time_step_s)
        else:
            parameters.pop("time_step_s", None)
        return parameters

    @Slot(str, result=bool)
    @Slot(str, bool, result=bool)
    def saveConfiguration(self, name: str, make_default: bool = True) -> bool:
        """Persist editable settings beside their managed process in this project."""
        if self._project_compute is None or self._result_view_only:
            self._status = "保存项目配置需要绑定项目窗口。"
            self.changed.emit()
            return False
        try:
            if self._pending_information_edit is not None:
                raise ValueError("资源信息仍在编辑，请先确认更新或取消编辑。")
            process_id = self._project_compute.process_document_id
            if not process_id:
                raise ValueError("请先选择并导入项目工艺")
            if not name or not name.strip():
                raise ValueError("配置名称不能为空")
            identity = self._project_compute.save_configuration(
                process_id, self._configuration_parameters(), name.strip(),
                configuration_id=self._project_compute.configuration_id,
                make_default=bool(make_default),
                description=self._project_configuration_description,
                notes=self._project_configuration_notes)
            self._project_compute.select_context(
                configuration_id=identity, process_document_id=process_id)
            self._active_configuration_parameters = self._configuration_parameters()
            self._project_configuration_name = name.strip()
            self._project_compute.mark_draft_saved()
            self._status = f"项目配置已保存：{name.strip()}。"
            self.inputsChanged.emit()
            self.changed.emit()
            return True
        except (ValueError, TypeError, OSError, KeyError) as exc:
            self._status = f"项目配置保存失败：{exc}"
            self.changed.emit()
            return False

    @Property("QStringList", notify=changed)
    def geometryRows(self) -> list[str]:
        return list(self._geometry_rows)

    @Property(str, notify=inputsChanged)
    def device(self) -> str:
        return self._device

    @Property(str, notify=inputsChanged)
    def algorithm(self) -> str:
        return self._algorithm

    @Property(int, notify=inputsChanged)
    def algorithmBudgetSeconds(self) -> int:
        return self._algorithm_budget_s

    @Slot(int)
    def setAlgorithmBudgetSeconds(self, value: int) -> None:
        if 0 <= value <= 300:
            self._algorithm_budget_s = value
            self._revision += 1
            self.inputsChanged.emit()
            self.changed.emit()

    def _algorithm_options(self) -> dict:
        if self._algorithm != "fast" and self._algorithm_budget_s > 0:
            return {"budget_s": self._algorithm_budget_s}
        return {}

    @Property(float, notify=changed)
    def progressValue(self) -> float:
        return self._progress_value

    @Property(str, notify=changed)
    def algorithmReportText(self) -> str:
        return self._algorithm_report_text

    @Property(bool, notify=inputsChanged)
    def fastCycle(self) -> bool:
        return self._fast_cycle

    @Property(float, notify=inputsChanged)
    def fastThresholdPercent(self) -> float:
        return self._fast_threshold_percent

    @Property(str, notify=inputsChanged)
    def fastThresholdText(self) -> str:
        return self._fast_threshold_text

    @Property(bool, notify=inputsChanged)
    def fastThresholdInvalid(self) -> bool:
        return self._fast_threshold_invalid

    @Property(bool, constant=True)
    def resultViewOnly(self):
        return self._result_view_only

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=changed)
    def status(self) -> str:
        return self._status

    @Property(str, notify=changed)
    def estimate(self) -> str:
        return self._estimate

    @Property(str, notify=changed)
    def memoryEstimate(self):
        return self._memory_estimate

    @Property(QUrl, notify=changed)
    def pdfSuggestedUrl(self):
        from coat_sim.core.result_pdf import suggested_pdf_filename
        process = (self._export_inputs or {}).get("process")
        folder = (self._project_directory("data", "results") if self._project_compute is not None
                  else data_path("results"))
        return QUrl.fromLocalFile(str(folder / suggested_pdf_filename(self._result or {}, process)))

    @Property("QStringList", notify=changed)
    def metrics(self) -> list[str]:
        return list(self._metrics)

    @Property("QStringList", notify=changed)
    def uniformityMetrics(self) -> list[str]:
        labels = {"平均膜厚", "最小膜厚", "最大膜厚", "峰谷差", "变异系数"}
        rows = []
        for item in self._metrics:
            label = item.partition("：")[0]
            if (label in labels or "径向" in label or "角向" in label
                    or "均匀" in label or "uniformity" in label.lower()):
                rows.append(item)
        return rows

    @Property("QVariantList", notify=changed)
    def uniformityRows(self) -> list[dict[str, str]]:
        return format_uniformity_rows(self._result)

    @Property(str, notify=changed)
    def uniformityStateText(self) -> str:
        if self._busy:
            return ("正在计算均匀性；当前指标仍为上次有效结果。" if self._result is not None
                    else "正在计算均匀性，完成后显示本次指标。")
        if self._uniformity_result_notice:
            return self._uniformity_result_notice
        return "" if self._result is not None else "计算完成后显示本次结果的均匀性指标。"

    @Property(str, notify=changed)
    def uniformityStateSeverity(self) -> str:
        return "busy" if self._busy else self._uniformity_result_notice_severity

    @Property("QStringList", notify=changed)
    def surfaceFigureMetrics(self) -> list[str]:
        """Show only the configured substrate geometry summary for this result."""
        settings = None
        if isinstance(self._result, dict):
            requested = self._result.get("requested_settings")
            effective = self._result.get("effective_settings")
            settings = (requested if isinstance(requested, dict) else
                        effective if isinstance(effective, dict) else None)
        if settings is None and isinstance(self._surface_figure_settings, dict):
            settings = self._surface_figure_settings
        if settings is None:
            try:
                settings = asdict(self._settings())
            except (ValueError, TypeError, OverflowError):
                settings = asdict(Settings())
        return format_surface_figure_summary(settings)

    @Property("QStringList", notify=changed)
    def historyRows(self) -> list[str]:
        return list(self._history)

    @Property(int, notify=changed)
    def imageRevision(self) -> int:
        return self._image_revision

    @Property(float, notify=changed)
    def imageMinimum(self) -> float:
        return self._image_min

    @Property(float, notify=changed)
    def imageMaximum(self) -> float:
        return self._image_max

    @Property(str, notify=changed)
    def imageUnit(self) -> str:
        return self._image_unit

    @Property("QVariantMap", notify=changed)
    def axisBounds(self) -> dict[str, float]:
        return dict(self._axis_bounds)

    @Slot(float, float, float, float, float, float, result="QVariantMap")
    def sampleThicknessAt(self, point_x, point_y, image_x, image_y,
                         image_width, image_height) -> dict:
        """Return the raw value under a plot-local point, never a PNG color."""
        result = self._result
        field = self._display_field
        if result is None or field is None:
            return {"inside_image": False, "has_data": False}
        x_axis = result.get("x")
        y_axis = result.get("y", x_axis)
        try:
            sample = sample_nearest_grid(
                field, x_axis, y_axis, point_x, point_y,
                image_x, image_y, image_width, image_height)
        except (TypeError, ValueError, OverflowError):
            return {"inside_image": False, "has_data": False}
        if sample is None:
            return {"inside_image": False, "has_data": False}
        response = {
            "inside_image": True,
            "has_data": sample.thickness is not None,
            "x_mm": sample.x_mm,
            "y_mm": sample.y_mm,
            "marker_x": sample.marker_x,
            "marker_y": sample.marker_y,
            "unit": self._image_unit,
        }
        if sample.thickness is not None:
            response["value"] = sample.thickness
        return response

    @Property(bool, notify=changed)
    def hasResult(self) -> bool:
        return self._result is not None

    @Property(str, notify=changed)
    def assetName(self):
        return self._asset_name

    @Property(bool, notify=changed)
    def canSaveAsset(self):
        queue_bundle_available = bool(self._queue_result_asset_path) and Path(self._queue_result_asset_path).is_file()
        return bool(self._asset_run_id) and (not self._loaded_asset_path or queue_bundle_available)

    @Slot("QStringList")
    def setPdfSections(self, sections):
        self._pdf_sections = list(sections)

    @Slot(str)
    @Slot(str, bool)
    def saveAsset(self, name, include_results=True):
        if not self._asset_run_id:
            self._status = "没有可保存的临时结果。"
        else:
            try:
                if self._queue_result_asset_path:
                    if not Path(self._queue_result_asset_path).is_file():
                        raise ValueError("队列结果资产包已不存在，不能从任务摘要恢复计算结果")
                    saved = self.service.save_simulation_asset_copy(
                        self._queue_result_asset_path, name.strip(), confirmed=True,
                        include_results=include_results)
                else:
                    saved = self.service.save_simulation_asset(
                        self._asset_run_id, name.strip(), confirmed=True, include_results=include_results)
                self._asset_name = name.strip()
                self._status = f"资产包已保存：{saved['asset_path']}"
            except (ValueError, OSError, KeyError) as exc:
                self._status = f"资产保存失败：{exc}"
        self.changed.emit()

    @Slot(QUrl)
    def openAsset(self, url):
        if self._busy:
            return
        try:
            path = Path(url.toLocalFile())
            if self._project_compute is not None:
                path = self._project_compute.import_asset(path, "assets")
            loaded = self.service.load_simulation_asset(path)
            self._apply_loaded_asset(loaded, str(path))
            if self._project_compute is not None:
                # A result asset is project-owned after import, but its process
                # provenance is not a managed process document. Keep it view-only.
                self._result_view_only = True
        except (ValueError, OSError, KeyError, MemoryError) as exc:
            self._status = f"资产包读取失败：{exc}"
            self._uniformity_result_notice = self._status
            self._uniformity_result_notice_severity = "error"
        self.changed.emit()

    def _apply_loaded_asset(self, loaded, path, *, queue_result_copy=False):
        self._queue_result_asset_path = str(path) if queue_result_copy else ""
        requested_settings = loaded.get("requested_settings")
        self._surface_figure_settings = (
            requested_settings if isinstance(requested_settings, dict)
            else loaded.get("settings") if isinstance(loaded.get("settings"), dict)
            else None)
        input_snapshot = loaded.get("input_snapshot")
        reusable = bool(
            isinstance(input_snapshot, dict)
            and input_snapshot.get("kind") == "coat_sim.input_snapshot"
            and input_snapshot.get("process_path")
            and isinstance(input_snapshot.get("files"), list)
            and input_snapshot["files"]
        )
        if queue_result_copy:
            # Queue reopening may recalculate only from the complete portable
            # snapshot embedded in this asset. Never fall back to provenance
            # paths that may have been deleted since the original run.
            self._result_view_only = not reusable
        self._paths["process"] = path if reusable else ""
        for key in self._VISIBLE_FIELDS:
            if key in (loaded.get("requested_settings") or loaded.get("settings") or {}):
                self._values[key] = str((loaded.get("requested_settings") or loaded["settings"])[key])
        parameters = (loaded.get("input_snapshot") or {}).get("parameters", {})
        self._device = parameters.get("backend", loaded.get("requested_device") or "CPU")
        self._algorithm = parameters.get("algorithm", "fast")
        self._algorithm_budget_s = parameters.get("algorithm_options", {}).get("budget_s") or 0
        self._fast_cycle = bool(parameters.get("fast_cycle", False))
        self._fast_threshold_percent = float(parameters.get("fast_cycle_residual_percent", 5))
        self._fast_threshold_text = str(self._fast_threshold_percent)
        self._fast_threshold_invalid = False
        self._time_basis = "time_steps"
        if reusable:
            self._update_time(self._time_basis)
        self.inputsChanged.emit()
        self._refresh_geometry_preview()
        self._revision += 1
        result = loaded["result"]
        if not loaded.get("has_results", True):
            self._result = None
            self._display_field = None
            self.image_provider.clear()
            self._image_revision += 1
            self._image_min = self._image_max = 0.0
            self._axis_bounds = {"x_min": 0.0, "x_max": 0.0,
                                 "y_min": 0.0, "y_max": 0.0}
            self._metrics = []
            self._uniformity_result_notice = ""
            self._uniformity_result_notice_severity = "info"
            self._asset_run_id = ""
            self._export_inputs = None
            self._status = "已打开不含结果的资产包：参数、工艺和依赖可重新加入计算队列"
            self.changed.emit()
            return
        self._loaded_asset_path = loaded["asset_path"]
        result["_loaded_asset"] = True
        result["_asset"] = {"run_id": loaded["run_id"]}
        self._pending_export_inputs = {key: loaded.get(key) for key in
            ("settings", "machine", "process", "elapsed_s", "requested_device")}
        self._asset_name = loaded["name"]
        self._started_revision = self._revision
        if self._finish(self._job_id, result, float(loaded["elapsed_s"]), None) is False:
            return False
        self._status = f"已打开资产包：{Path(path).name}"
        if not reusable:
            self._status += " · 旧包没有完整设计依赖，可查看结果；重新计算前请选择工艺"
        self.changed.emit()


    def _settings(self) -> Settings:
        values = asdict(Settings())
        for key in self._VISIBLE_FIELDS:
            raw = self._values[key].strip()
            if key in self._INT_FIELDS:
                if not raw.isdecimal():
                    raise ValueError(f"{key} 须为正整数")
                values[key] = int(raw)
            else:
                values[key] = float(raw)
        settings = Settings(**values)
        settings.validate()
        return settings

    def _simulation_args(self) -> dict:
        if self._project_compute is not None:
            process_id = self._project_compute.process_document_id
            if not process_id:
                raise ValueError("请先导入项目内的工艺")
            # Resolve the project's validated input_snapshot by its managed
            # document ID. The logical path is UI provenance only.
            return {"process_path": self._project_compute.resolve_process(process_id)["input_snapshot"]}
        process_path = self._paths["process"]
        if not process_path:
            raise ValueError("请先选择工艺")
        return {"process_path": process_path}

    def _refresh_geometry_preview(self, document=None) -> None:
        """Show the process machine as read-only information."""
        self._geometry_rows = []
        try:
            if self._project_compute is not None:
                process_id = self._project_compute.process_document_id
                if not process_id:
                    return
                process_snapshot = self._project_compute.resolve_process(process_id)["input_snapshot"]
                document = document or self.service.load("process", process_snapshot)
            else:
                process_path = self._paths["process"]
                if not process_path:
                    return
            if document is None:
                document = self.service.load("process", process_path)
            machine = (document.runtime or {}).get("machine") or {}
            if not machine:
                raise ValueError("工艺未加载机器结构")
            labels = (
                ("carrier_x_mm", "大盘 X", "mm"),
                ("carrier_y_mm", "大盘 Y", "mm"),
                ("planet_offset_mm", "行星盘轨道半径", "mm"),
                ("substrate_z_mm", "基片高度", "mm"),
                ("sun_teeth", "太阳轮齿数", ""),
                ("planet_teeth", "行星轮齿数", ""),
                ("gear_module_mm", "齿轮模数", "mm"),
                ("substrate_radius_mm", "基片计算半径", "mm"),
            )
            self._geometry_rows = [
                f"{label}：{machine[key]}{(' ' + unit) if unit else ''}"
                for key, label, unit in labels if machine.get(key) is not None
            ]
            sources = machine.get("sources")
            if isinstance(sources, list):
                self._geometry_rows.append(f"蒸发源数：{len(sources)}")
                for slot, source in enumerate(sources, 1):
                    if isinstance(source, dict):
                        x_mm = source.get("x_mm")
                        y_mm = source.get("y_mm")
                        if x_mm is not None and y_mm is not None:
                            self._geometry_rows.append(
                                f"源位次 {slot}：X {x_mm} mm · Y {y_mm} mm")
            masks = machine.get("masks")
            if isinstance(masks, list):
                self._geometry_rows.append(f"修正板数：{len(masks)}")
        except (ValueError, TypeError, OSError, KeyError) as exc:
            self._geometry_rows = [f"机器结构无法预览：{exc}"]

    def _time_request(self, basis: str) -> dict:
        """Return authoritative user input, never the rounded effective step."""
        if basis == "time_step_s":
            return {"time_step_s": float(self._requested_time_step_s)}
        raw = self._values["time_steps"].strip()
        if not raw.isdecimal():
            raise ValueError("时间步数须为正整数")
        return {"time_steps": int(raw)}

    def _update_time(self, basis: str) -> None:
        if self._project_compute is not None:
            if not self._project_compute.process_document_id:
                return
            process_path = self._simulation_args()["process_path"]
        else:
            process_path = self._paths["process"]
        if not process_path:
            # Step duration is defined by the selected process, so there is
            # no one-second fallback in the process-only main window.
            return
        timing = self.service.time_fields(
            process_path=process_path, **self._time_request(basis))
        self._values["time_steps"] = str(timing["time_steps"])
        self._values["time_step_s"] = f'{timing["time_step_s"]:.12g}'

    @Slot(str, str)
    def setField(self, key: str, value: str) -> None:
        if key not in self._VISIBLE_FIELDS and key != "time_step_s":
            return
        self._values[key] = value.strip()
        if key == "time_step_s":
            # Retain even incomplete/invalid edits so submission rejects them
            # rather than falling back to a previous valid sampling request.
            self._requested_time_step_s = value.strip()
        self._revision += 1
        try:
            if key in ("time_steps", "time_step_s"):
                self._time_basis = key
                self._update_time(key)
            self._status = "输入已更新。"
        except (ValueError, TypeError, OSError) as exc:
            self._status = str(exc)
        self.inputsChanged.emit()
        self.changed.emit()
        self.refreshEstimate()

    @Property(bool, notify=inputsChanged)
    def canReload(self) -> bool:
        selected = (self._project_compute.process_document_id if self._project_compute is not None
                    else self._paths["process"])
        return bool(selected) and not self._result_view_only

    @Slot(result=bool)
    def reloadCurrent(self) -> bool:
        """Refresh the selected process/dependency graph, never queue snapshots.

        Asset inputs are read anew from their archive, not an old extraction.
        Editable simulation controls and the last completed result are retained.
        """
        if not self.canReload:
            self._status = "请先选择工艺，再刷新文件。"
            self.changed.emit()
            return False
        try:
            if self._project_compute is not None:
                process_id = self._project_compute.process_document_id
                if not process_id:
                    raise ValueError("请先导入项目内的工艺")
                document = self.service.load(
                    "process", self._project_compute.resolve_process(process_id)["input_snapshot"])
            else:
                document = self.service.load("process", self._paths["process"])
            if document.issues:
                raise ValueError("；".join(document.issues))
            if not (document.runtime or {}).get("machine"):
                raise ValueError("工艺未加载机器结构")
            from coat_sim.core.process_simulator import process_timing
            timing = process_timing(document.data, **self._time_request(self._time_basis))
        except (ValueError, OSError, TypeError, KeyError, OverflowError) as exc:
            self._status = f"刷新失败，当前输入和结果保持不变：{exc}"
            self.changed.emit()
            return False
        self._values["time_steps"] = str(timing["time_steps"])
        self._values["time_step_s"] = f'{timing["time_step_s"]:.12g}'
        self._refresh_geometry_preview(document)
        self._revision += 1
        self._status = "已重新载入工艺和全部依赖；计算参数保留，已提交任务不受影响。"
        if self._result is not None:
            self._status += "当前图像仍为上次计算结果。"
        self.inputsChanged.emit()
        self.changed.emit()
        self.refreshEstimate()
        return True

    @Slot(str, QUrl)
    def chooseFile(self, kind: str, url: QUrl) -> None:
        if kind not in self._paths:
            return
        selected = Path(url.toLocalFile()).expanduser()
        if self._project_compute is not None and kind == "process":
            try:
                identity = self._project_compute.import_process(selected)
                self._paths[kind] = str(self._project_compute.process_handle(identity))
                self._active_configuration_parameters = {}
                self._status = "工艺及依赖已复制到项目；可计算或保存为项目配置。"
            except (ValueError, OSError, TypeError, KeyError) as exc:
                self._status = f"导入项目工艺失败：{exc}"
                self.changed.emit()
                return
        else:
            if self._project_compute is not None:
                try:
                    selected = self._project_compute.owned_path(selected)
                except (ValueError, OSError) as exc:
                    self._status = f"项目路径无效：{exc}"
                    self.changed.emit()
                    return
            self._paths[kind] = str(selected.resolve())
        self._revision += 1
        if kind == "process":
            self._refresh_geometry_preview()
            try:
                self._update_time(self._time_basis)
            except (ValueError, TypeError, OSError) as exc:
                self._status = str(exc)
        self.inputsChanged.emit()
        self.changed.emit()
        self.refreshEstimate()

    @Slot(str)
    def openDocument(self, path: str) -> None:
        """Let the shared launcher initialize simulation from a process path."""
        if self._project_compute is not None:
            try:
                candidate = Path(path).expanduser()
                # Resource document handles use the managed document ID as
                # their filename. Re-select that ID rather than reimporting.
                identity = candidate.stem
                if len(identity) == 32 and all(ch in "0123456789abcdef" for ch in identity):
                    resolved = self._project_compute.resolve_process(identity)
                    self._project_compute.select_context(
                        configuration_id=None, process_document_id=identity)
                    self._paths["process"] = str(self._project_compute.process_handle(identity))
                    self._active_configuration_parameters = {}
                    self._refresh_geometry_preview(self.service.load("process", resolved["input_snapshot"]))
                    self._revision += 1
                    self.inputsChanged.emit()
                    self.changed.emit()
                    self.refreshEstimate()
                    return
            except (ValueError, OSError, TypeError, KeyError):
                pass
            self.chooseFile("process", QUrl.fromLocalFile(path))
            return
        local = Path(path).expanduser().resolve()
        self.chooseFile("process", QUrl.fromLocalFile(str(local)))

    @Slot(str)
    def clearFile(self, kind: str) -> None:
        if kind not in self._paths:
            return
        if self._project_compute is not None and kind == "process":
            self._project_compute.select_context(configuration_id=None, process_document_id=None)
            self._active_configuration_parameters = {}
        self._paths[kind] = ""
        self._revision += 1
        if kind == "process":
            self._refresh_geometry_preview()
            try:
                self._update_time(self._time_basis)
            except (ValueError, TypeError, OSError) as exc:
                self._status = str(exc)
        self.inputsChanged.emit()
        self.changed.emit()
        self.refreshEstimate()

    @Slot(str)
    def setDevice(self, value: str) -> None:
        # Compatibility entry point for controller/automation callers. The
        # selected backend must resolve to a discovered executable target.
        target = next((row for row in self._compute_target.devices
                       if row["backend"] == value and row["available"]), None)
        if target is None:
            self._status = "所选设备尚未探测可用，或此算法不支持；未更改仿真设备。"
            self.changed.emit()
            return
        self._compute_target.setDeviceId(target["id"])

    @Slot(str)
    def setAlgorithm(self, value: str) -> None:
        if value not in self._ALGORITHM_LABELS:
            return
        self._algorithm = value
        self._compute_target.setAlgorithm(value)
        self._revision += 1
        self._status = f"已选择{self._ALGORITHM_LABELS[value]}。"
        self.inputsChanged.emit()
        self.changed.emit()
        self.refreshEstimate()

    @Slot(bool)
    def setFastCycle(self, enabled: bool) -> None:
        self._fast_cycle = bool(enabled)
        self._revision += 1
        if self._fast_cycle and self._fast_threshold_invalid:
            self._status = "残余时长阈值须为 0–100%，修正后才能计算。"
        elif self._fast_cycle:
            self._status = ("已启用周期快速算法；残余沉积时长占比不超过 "
                            f"{self._fast_threshold_percent:g}% 的层使用完整回归周期。")
        else:
            self._status = "全时长积分将按真实时长积分（仍受时间与空间采样误差影响）。"
        self.inputsChanged.emit()
        self.changed.emit()
        self.refreshEstimate()

    @Slot(str)
    def setFastThreshold(self, value: str) -> None:
        self._fast_threshold_text = value
        try:
            threshold = float(value)
            if not 0 <= threshold <= 100:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            self._fast_threshold_invalid = True
            self._status = "残余时长阈值须为 0–100%。"
            self.inputsChanged.emit()
            self.changed.emit()
            self.refreshEstimate()
            return
        self._fast_threshold_percent = threshold
        self._fast_threshold_invalid = False
        self._revision += 1
        self._status = f"快速算法残余时长阈值已设为 {threshold:g}%。"
        self.inputsChanged.emit()
        self.changed.emit()
        self.refreshEstimate()

    @Slot()
    def refreshEstimate(self) -> None:
        process_snapshot = None
        try:
            from coat_sim.core.compute_resources import estimate_memory
            if self._project_compute is not None:
                process_id = self._project_compute.process_document_id
                if process_id:
                    process_snapshot = self._project_compute.resolve_process(process_id)["input_snapshot"]
                    process_document = self.service.load("process", process_snapshot)
                    layers = len(process_document.data.get("steps", []))
                else:
                    layers = 1
            else:
                layers = (len(self.service.load("process", self._paths["process"]).data.get("steps", []))
                          if self._paths["process"] else 1)
            self._memory_estimate = estimate_memory(self._settings(), layer_count=layers)["memory_estimate_label"]
        except (ValueError, TypeError, OSError, OverflowError) as exc:
            self._memory_estimate = f"内存估算：{exc}"
        if self._algorithm != "fast":
            self._estimate = ("CPU 时间加密检查：耗时取决于收敛层级"
                              if self._algorithm == "reference"
                              else "局部优化：默认 9 次评估，暂无可靠耗时估计")
            self.changed.emit()
            return
        if self._fast_cycle and self._fast_threshold_invalid:
            self._estimate = "预计时间：先修正残余时长阈值"
            self.changed.emit()
            return
        if (self._project_compute is not None and not self._project_compute.process_document_id) or (
                self._project_compute is None and not self._paths["process"]):
            self._estimate = "选择工艺后显示预计时间"
            self.changed.emit()
            return
        try:
            if self._project_compute is not None:
                from coat_sim.core.calculation_history import format_elapsed
                arguments = self._simulation_args()
                work_index = self.service.work_index(
                    self._settings(), fast_cycle=self._fast_cycle,
                    fast_cycle_residual_percent=self._fast_threshold_percent,
                    **arguments)
                timing = self.service.history.estimate(
                    work_index, self._device,
                    algorithm="fast_cycle" if self._fast_cycle else "precise")
                estimate = (None if timing is None else {
                    "formatted": format_elapsed(timing[0]), "sample_count": timing[2],
                    "estimated_s": timing[0]})
                if estimate is None:
                    self._estimate = "预计时间：暂无同算法、同设备历史"
                else:
                    self._estimate = (f'粗估 {estimate["formatted"]} · '
                                      f'依据最近 {estimate["sample_count"]} 条同算法、同设备记录估算')
                self.changed.emit()
                return
            estimate = self.service.estimate_runtime(
                self._settings(), requested_device=self._device,
                fast_cycle=self._fast_cycle,
                fast_cycle_residual_percent=self._fast_threshold_percent,
                **self._simulation_args())
            self._estimate = (f'粗估 {estimate["formatted"]} · '
                              f'依据最近 {estimate["sample_count"]} 条同算法、同设备记录估算'
                              if estimate["estimated_s"] is not None
                              else "预计时间：暂无同算法、同设备历史")
        except (ValueError, TypeError, OSError) as exc:
            self._estimate = f"预计时间：{exc}"
        self.changed.emit()

    @Slot()
    def showQueue(self):
        self.showQueueRequested.emit()

    def _submission_failed(self, store, job, error, *, job_id=None, previous=None):
        """Report client preparation/observer errors without rewriting admitted jobs."""
        message = str(error) or type(error).__name__
        if job is not None:
            message += f"；独立队列已接收任务 {job['job_id']}，可在队列中继续查询"
        if job_id is not None:
            self._queued_keys.pop(job_id, None)
            self._job_cancel_events.pop(job_id, None)
            if previous is not None:
                (self._job_id, self._cancel_event, self._pending_export_inputs,
                 self._started_revision) = previous
        self._busy = bool(self._job_cancel_events)
        self._status = f"无法开始计算：{message}"
        self._uniformity_result_notice = self._status
        self._uniformity_result_notice_severity = "error"
        self.changed.emit()

    def detach_observers(self):
        self._observers_closed = True
        self._job_cancel_events.clear()
        self._queued_keys.clear()
        if self._project_compute is not None:
            if not self._result_view_only:
                self._project_compute.unregister_draft_provider(self._project_draft_signature)
                self._project_compute.unregister_thaw_callback(self._on_project_unfrozen)
            for operation in self._project_operations.values():
                self._project_compute.retire_operation(operation)
        self._project_operations.clear()
        self._project_queue_ids.clear()
        self._deferred_project_finishes.clear()
        self._busy = False

    def _on_project_unfrozen(self) -> None:
        """Replay a queued Qt completion only after close preflight releases."""
        try:
            QTimer.singleShot(0, self._replay_deferred_project_finishes)
        except RuntimeError:
            pass

    def _replay_deferred_project_finishes(self) -> None:
        for job_id, deferred in tuple(self._deferred_project_finishes.items()):
            self._deferred_project_finishes.pop(job_id, None)
            result, elapsed_s, error, operation, _captured_revision = deferred
            if self._project_compute is None or not self._project_compute.accepts(operation):
                continue
            self._finish(job_id, result, elapsed_s, error)

    @Slot()
    def calculate(self) -> None:
        if self._result_view_only:
            self._status = "此窗口仅查看已完成结果；没有重新计算"
            self.changed.emit()
            return
        if (self._algorithm == "fast" and self._fast_cycle
                and self._fast_threshold_invalid):
            self._status = "无法计算：请先将残余时长阈值设为 0–100%。"
            self._uniformity_result_notice = self._status
            self._uniformity_result_notice_severity = "error"
            self.changed.emit()
            return
        if self._project_compute is not None and self._project_compute.editing_frozen:
            self._status = "项目正在进行关闭确认，暂不能开始新计算。"
            self.changed.emit()
            return
        queue_store = queue_job = None
        project_operation = None
        try:
            # Refresh even when a reused filename has changed on disk.
            self._update_time(self._time_basis)
            self._refresh_geometry_preview()
            settings = self._settings()
            self._surface_figure_settings = asdict(settings)
            args = self._simulation_args()
            if self._project_compute is not None:
                process_id = self._project_compute.process_document_id
                if not process_id:
                    raise ValueError("请先导入项目内的工艺")
                project_operation = self._project_compute.capture(uuid4().hex)
                # ProjectStore validation is the sole source of process
                # dependencies. No external path is reopened or frozen here.
                resolved = self._project_compute.resolve_process(process_id)
                snapshot = deepcopy(resolved["input_snapshot"])
                document = self.service.load("process", snapshot)
            else:
                document = self.service.load("process", args["process_path"])
                snapshot = (self.service.load_simulation_asset(document.path)["input_snapshot"]
                            if Path(document.path).suffix.lower() == ".coatasset" else
                            self.service.freeze_process(document.data, base_dir=Path(document.path).parent))
            frozen = self.service.load("process", snapshot)
            process_data, machine_data = frozen.data, frozen.runtime["machine"]
            algorithm = self._algorithm
            queue_store, target_device = self._compute_target.client_for_submit(
                algorithm=algorithm, client_factory=SchedulerClient)
            verify_admission, target_is_current = self._compute_target.admission_verifier(queue_store, target_device)
            device = target_device["backend"]
            estimate = {"work_index": None, "estimated_s": None}
            if algorithm == "fast":
                try:
                    estimate = self.service.estimate_runtime(
                        settings, requested_device=device,
                        fast_cycle=self._fast_cycle,
                        fast_cycle_residual_percent=self._fast_threshold_percent,
                        process_path=snapshot)
                except (ValueError, TypeError, OSError, KeyError):
                    # Project input snapshots remain calculable even when a
                    # historical file-based estimator has no path fingerprint.
                    estimate = {"work_index": None, "estimated_s": None}
            job_key = hashlib.sha256(json.dumps({"settings": asdict(settings),
                "files": [(f["path"], f["sha256"]) for f in snapshot["files"]],
                "algorithm": algorithm, "device": device, "budget": self._algorithm_budget_s,
                "fast_cycle": self._fast_cycle, "threshold": self._fast_threshold_percent}, sort_keys=True).encode()).hexdigest()
            if job_key in self._queued_keys.values():
                if project_operation is not None and self._project_compute is not None:
                    self._project_compute.retire_operation(project_operation)
                self._status = "相同输入已在队列中；可以继续编辑或提交不同输入。"
                self.changed.emit()
                return
            from coat_sim.core.compute_resources import estimate_memory
            memory_estimate = estimate_memory(settings, layer_count=len(process_data.get("steps", [])))
            queue_id = uuid4().hex
            algorithm_options = self._algorithm_options()
            parameters = {"settings": asdict(settings), "process_path": snapshot, "backend": device,
                "algorithm": algorithm, "algorithm_options": algorithm_options,
                "fast_cycle": self._fast_cycle, "fast_cycle_residual_percent": self._fast_threshold_percent}
            submission_spec = freeze_spec(self.service, "process_recipe", parameters)
            submission_metadata = {"summary": f"沉积仿真 · {process_data.get('name', '工艺')} · {self._ALGORITHM_LABELS[algorithm]}",
                    "origin": {"surface": "desktop"}, "estimated_s": estimate.get("estimated_s"),
                    "memory_estimate_label": memory_estimate.get("memory_estimate_label", ""),
                    "memory_warning": memory_estimate.get("memory_warning", False),
                    "input_summary": {"snapshot_sha256": job_key, "process": deepcopy(process_data),
                        "dependencies": [{"kind": f["kind"], "path": f["path"], "sha256": f["sha256"]} for f in snapshot["files"]]}}
            if self._project_compute is not None:
                if project_operation is None:
                    raise ValueError("Project submission is missing its frozen operation")
                submission_metadata = self._project_compute.metadata(
                    project_operation, submission_metadata)
            if self._project_compute is not None:
                self._project_compute.register_job(
                    queue_id, module_key="main", service_owned=False,
                    state="submitting", metadata=submission_metadata)
        except Exception as exc:
            if project_operation is not None and self._project_compute is not None:
                self._project_compute.retire_operation(project_operation)
            self._submission_failed(queue_store, queue_job, exc)
            return
        previous_submission = (self._job_id, self._cancel_event,
                               self._pending_export_inputs, self._started_revision)
        job_id = None
        try:
            self._busy = True
            self._uniformity_result_notice = ""
            self._uniformity_result_notice_severity = "info"
            self._status = f"正在提交计算任务：{self._ALGORITHM_LABELS[algorithm]}"
            self._metrics = []
            self._progress_value = 0.0
            self._algorithm_report_text = ""
            self._job_id += 1
            job_id = self._job_id
            self._queued_keys[job_id] = job_key
            if project_operation is not None:
                self._project_operations[job_id] = project_operation
                self._project_queue_ids[job_id] = queue_id
            self._started_revision = self._revision
            cancel_event = RemoteCancelEvent(queue_store, queue_id, pending_admission=True)
            cancel_event.on_cancel_status = self.cancelTransportStatus.emit
            self._cancel_event = cancel_event
            self._job_cancel_events[job_id] = cancel_event
            # All algorithm choices are captured before starting the worker.
            algorithm_options = self._algorithm_options()
            fast_cycle = self._fast_cycle
            fast_threshold_percent = self._fast_threshold_percent
            snapshot["parameters"] = {"settings": asdict(settings), "process_path": snapshot["process_path"],
                "backend": device, "algorithm": algorithm, "algorithm_options": algorithm_options,
                "fast_cycle": fast_cycle, "fast_cycle_residual_percent": fast_threshold_percent}
            self._pending_export_inputs = {
                "settings": settings, "machine": machine_data,
                "process": process_data, "requested_device": device,
                "fast_cycle": fast_cycle,
                "fast_cycle_residual_percent": fast_threshold_percent,
                "work_index": estimate["work_index"],
                "algorithm_path": algorithm,
            }
            self.changed.emit()

            admission_project = self._project_compute

            def worker():
                started = time.monotonic()
                def publish_progress(value):
                    if not getattr(self, "_observers_closed", False):
                        try:
                            self.progressReady.emit(job_id, value)
                        except RuntimeError:
                            # Qt may destroy the window after an HTTP call
                            # returns; the admitted service job still lives.
                            pass
                try:
                    observed_id = queue_id
                    verify_admission()
                    if cancel_event.is_set():
                        raise InterruptedError("已在作业接纳前取消；未创建作业。")
                    if (getattr(self, "_observers_closed", False)
                            or self._project_compute is not admission_project
                            or (admission_project is not None
                                and (project_operation is None
                                     or admission_project.editing_frozen
                                     or not admission_project.accepts(project_operation)))):
                        raise InterruptedError("项目已关闭或已改变；未创建作业。")
                    target_is_current()
                    try:
                        if getattr(getattr(queue_store, "transport", None), "remote_http", False):
                            queue_store.submit(submission_spec, job_id=queue_id,
                                               metadata=submission_metadata)
                        else:
                            from coat_sim.scheduler.desktop import submit_local_with_receipt
                            submit_local_with_receipt(queue_store, submission_spec,
                                                      job_id=queue_id, metadata=submission_metadata)
                        if admission_project is not None:
                            admission_project.update_job(
                                queue_id, "queued", service_owned=True)
                    except RemoteSubmissionUnknown as unknown:
                        if not getattr(getattr(queue_store, "transport", None), "remote_http", False):
                            raise
                        if admission_project is not None:
                            admission_project.update_job(
                                queue_id, "submission_unknown", service_owned=False)
                        cancel_event.replace_job_id_before_admission(unknown.job_id)
                        publish_progress(
                            f"提交回执未确认；正在按任务 ID {unknown.job_id} 核对服务端状态…")
                        resolve_unknown_submission(
                            queue_store, unknown,
                            should_stop=lambda: getattr(self, "_observers_closed", False))
                        observed_id = unknown.job_id
                    cancel_event.mark_admitted()
                    if admission_project is not None:
                        admission_project.update_job(queue_id, "running")
                    reply = observe_job(queue_store, observed_id, publish_progress,
                                        disconnected=lambda: getattr(self, "_observers_closed", False))
                    from coat_sim.scheduler.worker import load_result
                    result, error = load_result(reply, queue_store), None
                    _assert_selected_backend_result(result, device)
                    elapsed = reply.get("elapsed_s", time.monotonic() - started)
                except Exception as exc:
                    result, error, elapsed = None, exc, time.monotonic() - started
                if admission_project is not None:
                    state = ("submission_unknown" if isinstance(error, RemoteSubmissionUnknown) else
                             "cancelled" if isinstance(error, InterruptedError) else
                             "failed" if error is not None else "succeeded")
                    admission_project.update_job(queue_id, state)
                _detach_error_tracebacks(error)
                if not getattr(self, "_observers_closed", False):
                    try:
                        self.resultReady.emit(job_id, result, elapsed, error)
                    except RuntimeError:
                        pass
            threading.Thread(target=worker, name="scheduler-result-observer", daemon=True).start()
        except Exception as exc:
            if self._project_compute is not None:
                self._project_compute.update_job(queue_id, "observer_failed")
            self._submission_failed(queue_store, queue_job, exc,
                                    job_id=job_id, previous=previous_submission)

    @Slot(int, object)
    def _on_progress(self, job_id: int, payload: object) -> None:
        if job_id != self._job_id or job_id not in self._job_cancel_events:
            return
        if self._project_compute is not None:
            operation = self._project_operations.get(job_id)
            if operation is None or not self._project_compute.accepts(operation):
                return
        if self._cancel_event is not None and self._cancel_event.is_set():
            return
        if isinstance(payload, dict):
            algorithm = payload.get("algorithm", "fast")
            label = self._ALGORITHM_LABELS.get(algorithm, "计算")
            if "fraction" in payload:
                self._progress_value = max(self._progress_value, float(payload["fraction"]))
                detail = (f" · 第 {payload['step_index']} 层 / 共 {payload['step_count']} 层"
                          if payload.get("step_count") else "")
                self._status = f"{label} · {self._progress_value * 100:.1f}%{detail}"
            elif payload.get("status") == "running":
                self._status = f"{label}：开始评估…"
            else:
                self._status = f"{label}：正在整理结果…"
        elif payload:
            self._status = str(payload)
        self.changed.emit()

    @Slot(str, str)
    def _on_cancel_transport_status(self, queue_id: str, code: str) -> None:
        if not any(event.job_id == queue_id for event in self._job_cancel_events.values()):
            return
        if code:
            self._status = f"取消请求尚未确认（{code}）；可再次点击取消，按原作业 ID 重试。"
        else:
            self._status = "取消请求已送达；等待队列确认终态…"
        self.changed.emit()

    @Slot()
    def cancel(self) -> None:
        if self._job_cancel_events:
            for event in self._job_cancel_events.values():
                event.set()
            self._status = "正在取消本窗口任务…"
            self.changed.emit()

    @Slot(int, object, float, object)
    def _finish(self, job_id: int, result: object,
                elapsed_s: float, error: object) -> None:
        operation = self._project_operations.get(job_id)
        if self._project_compute is not None and operation is not None:
            if self._project_compute.editing_frozen:
                try:
                    self._project_compute.host.resolve_operation(operation)
                except (ValueError, RuntimeError):
                    self._project_compute.update_job(
                        self._project_queue_ids.get(job_id, ""), "stale")
                    self._project_compute.retire_operation(operation)
                    self._project_operations.pop(job_id, None)
                    self._project_queue_ids.pop(job_id, None)
                else:
                    self._deferred_project_finishes[job_id] = (
                        result, elapsed_s, error, operation, self._started_revision)
                    return
            elif not self._project_compute.accepts(operation):
                self._project_compute.update_job(
                    self._project_queue_ids.get(job_id, ""), "stale")
                self._project_compute.retire_operation(operation)
                self._project_operations.pop(job_id, None)
                self._project_queue_ids.pop(job_id, None)
                self._queued_keys.pop(job_id, None)
                self._job_cancel_events.pop(job_id, None)
                self._busy = bool(self._job_cancel_events)
                if job_id == self._job_id:
                    self._pending_export_inputs = None
                    self._status = "项目已关闭或重新打开；已忽略旧会话的迟到结果。"
                self.changed.emit()
                return
            if (job_id == self._job_id and isinstance(result, dict) and error is None
                    and self._revision != self._started_revision):
                queue_id = self._project_queue_ids.pop(job_id, "")
                self._project_compute.update_job(queue_id, "stale")
                self._project_compute.retire_operation(operation)
                self._project_operations.pop(job_id, None)
                self._queued_keys.pop(job_id, None)
                self._job_cancel_events.pop(job_id, None)
                self._busy = bool(self._job_cancel_events)
                self._cancel_event = None
                self._pending_export_inputs = None
                self._status = ("计算已完成，但本次提交后输入已更改；迟到结果未替换当前图像。"
                                "可在项目队列中查看提交时的任务结果。")
                self._uniformity_result_notice = self._status
                self._uniformity_result_notice_severity = "info"
                self.changed.emit()
                return
        if not isinstance(error, RemoteSubmissionUnknown):
            self._queued_keys.pop(job_id, None)
        finished_cancel_event = self._job_cancel_events.pop(job_id, None)
        self._busy = bool(self._job_cancel_events)
        if job_id != self._job_id:
            if self._project_compute is not None and operation is not None:
                self._project_compute.retire_operation(operation)
                self._project_operations.pop(job_id, None)
                self._project_compute.update_job(
                    self._project_queue_ids.pop(job_id, ""),
                    "submission_unknown" if isinstance(error, RemoteSubmissionUnknown) else
                    "cancelled" if isinstance(error, InterruptedError) else
                    "failed" if error is not None else "succeeded")
            self.changed.emit()
            return
        if self._project_compute is not None and operation is not None:
            self._project_compute.retire_operation(operation)
            self._project_operations.pop(job_id, None)
            self._project_compute.update_job(
                self._project_queue_ids.pop(job_id, ""),
                "submission_unknown" if isinstance(error, RemoteSubmissionUnknown) else
                    "cancelled" if isinstance(error, InterruptedError) else
                "failed" if error is not None else "succeeded")
        self._cancel_event = None
        if error is not None:
            self._pending_export_inputs = None
            if isinstance(error, RemoteSubmissionUnknown):
                self._status = (f"任务 {error.job_id} 提交状态未知；请按此 ID 核对服务端，"
                                "不要创建新任务重试。")
                if finished_cancel_event is not None and finished_cancel_event.is_set():
                    self._status += "取消请求也尚未确认。"
            else:
                self._status = ("计算已取消" if isinstance(error, InterruptedError)
                                else f"计算失败：{error}")
                self._status += " · 本次未产生新结果"
            if self._result is not None:
                self._status += "；当前图像和可导出资产仍为上次结果"
            if getattr(error, "_coat_summary_warning", ""):
                self._status += " · " + error._coat_summary_warning
            self._uniformity_result_notice = self._status
            self._uniformity_result_notice_severity = ("info" if isinstance(error, InterruptedError)
                                                       else "error")
            self.changed.emit()
            return
        assert isinstance(result, dict)
        self._uniformity_result_notice = ""
        self._uniformity_result_notice_severity = "info"
        self._progress_value = 1.0
        report = result.get("algorithm_report") or {}
        self._algorithm_report_text = self._format_algorithm_report(report)
        warnings = result.get("sampling_warnings", [])
        if warnings:
            self._algorithm_report_text += "\n" + "\n".join("采样提示：" + item for item in warnings)
        self._result = result
        self._asset_run_id = result.get("_asset", {}).get("run_id", "")
        if not result.get("_loaded_asset"):
            self._loaded_asset_path = ""
        self._export_inputs = self._pending_export_inputs
        self._pending_export_inputs = None
        if not result.get("_loaded_asset"):
            self._asset_name = result.get("_asset", {}).get("summary", {}).get("name_suggestion", "膜厚仿真结果")
        x_axis = result["x"]
        y_axis = result.get("y", x_axis)
        self._axis_bounds = {
            "x_min": float(x_axis[0]), "x_max": float(x_axis[-1]),
            "y_min": float(y_axis[0]), "y_max": float(y_axis[-1]),
        }
        field = result["field_nm"] if "field_nm" in result else result["field"]
        try:
            self._image_min, self._image_max = self.image_provider.set_field(field)
            self._display_field = field
            self._image_revision += 1
        except (ValueError, TypeError, MemoryError) as exc:
            # Never leave the previous texture labelled as this new result.
            self.image_provider.clear()
            self._display_field = None
            self._image_revision += 1
            self._image_min = self._image_max = 0.0
            self._axis_bounds = {"x_min": 0.0, "x_max": 0.0, "y_min": 0.0, "y_max": 0.0}
            self._result = None
            self._asset_run_id = self._loaded_asset_path = ""
            self._export_inputs = self._pending_export_inputs = None
            self._metrics = []
            self._algorithm_report_text = ""
            self._status = f"绘图失败：{exc}；已清空预览，请释放内存后重新打开已保存的结果资产"
            self._uniformity_result_notice = self._status
            self._uniformity_result_notice_severity = "error"
            self.changed.emit()
            return False
        else:
            self._status = "计算完成 · 结果与摘要来自提交时冻结快照"
        if isinstance(report, dict) and report.get("status"):
            self._status += " · " + self._report_status(report)
        if result.get("sampling_warnings"):
            self._status += " · 存在采样风险，详见算法报告"
        if self._revision != self._started_revision:
            self._status += " · 输入已更改，图像对应开始计算时的快照"
        if result.get("_asset_warning"):
            self._status += " · " + result["_asset_warning"]
        self._image_unit = "nm" if "field_nm" in result else "相对值"
        metrics = [f'计算设备：{result.get("device", "—")}',
                   f"计算用时：{elapsed_s:.2f} s"]
        metrics.insert(0, f'层数：{len(result.get("step_results", []))}')
        if result.get("algorithm") == "fast_cycle":
            threshold = float(result.get("fast_cycle_residual_percent", 5.0))
            metrics.append(f"积分方式：周期快算 · 残余阈值 {threshold:g}%")
            if result.get("computed_time_steps") is not None:
                metrics.append(f'实际计算采样：{result["computed_time_steps"]} 步')
        else:
            metrics.append("积分方式：真实时长逐步积分")
        if result.get("environment_model_notice"):
            metrics.append(result["environment_model_notice"])
            if result.get("minimum_direct_beam_survival") is not None:
                metrics.append(f"各层最低直达束存活率（归一化前）：{result['minimum_direct_beam_survival'] * 100:.4g}%")
            if result.get("strong_scattering_warning"):
                metrics.append("警告：直达束显著损失，未计算散射再沉积；不能据此预测完整沉积结果")
        step_results = result.get("step_results", [])
        for step in step_results[:8]:
            index = step.get("step_index", "?")
            source_slot = step.get("source_slot", "?")
            algorithm = ("周期快算" if step.get("algorithm") == "cycle"
                         else "真实时长积分")
            metrics.append(f"第 {index} 层（源 {source_slot}）：{algorithm}")
            environment = step.get("environment", {})
            if environment.get("direct_beam_survival") is not None:
                metrics.append(f"第 {index} 层直达束存活率：{environment['direct_beam_survival'] * 100:.4g}%（相同几何真空对照，目标厚度仍归一化）")
            if step.get("algorithm") == "cycle":
                cycles = step.get("return_carrier_revolutions")
                remainder = step.get("residual_fraction")
                if cycles is not None and remainder is not None:
                    metrics.append(
                        f"第 {index} 层回归：{cycles} 圈 · 残余占比 {float(remainder) * 100:.2f}%")
                metrics.append(
                    f"第 {index} 层周期场：{'复用同源结果' if step.get('reused_cycle_field') else '本层计算'}")
            if step.get("simulated_duration_s") is not None:
                metrics.append(
                    f"第 {index} 层时长：真实 {float(step['duration_s']):.3g} s · "
                    f"积分 {float(step['simulated_duration_s']):.3g} s")
        if len(step_results) > 8:
            metrics.append(f"另有 {len(step_results) - 8} 层；完整信息保留在资产包，可选入 PDF。")
        for label, key, unit in (
                ("平均膜厚", "mean_nm", " nm"),
                ("最小膜厚", "min_nm", " nm"),
                ("最大膜厚", "max_nm", " nm"),
                ("峰谷差", "pv_nm", " nm"),
                ("变异系数", "cv_percent", "%")):
            if result.get(key) is not None:
                metrics.append(f'{label}：{float(result[key]):.3f}{unit}')
        self._metrics = metrics
        if self._export_inputs is not None:
            self._export_inputs["elapsed_s"] = elapsed_s
            try:
                if (result.get("_loaded_asset")
                        or self._export_inputs.get("algorithm_path", "fast") != "fast"):
                    # Multi-run paths must not skew legacy single-run timing estimates.
                    self._refresh_history()
                    self.changed.emit()
                    return
                self.service.record_success(
                    requested_device=self._export_inputs["requested_device"],
                    result=result, work_index=self._export_inputs["work_index"],
                    elapsed_s=elapsed_s)
            except (ValueError, OSError) as exc:
                self._status += f" · 历史记录失败：{exc}"
        self._refresh_history()
        self.changed.emit()
        self.refreshEstimate()

    @staticmethod
    def _report_status(report: dict) -> str:
        status = report.get("status", "")
        if report.get("algorithm") == "optimize" and report.get("validation_status"):
            return {
                "accepted_at_finer_sampling": "加密采样复核通过，采用局部候选",
                "rejected_at_finer_sampling": "加密采样复核未通过，保留初值转速",
                "unchanged_initial": "搜索未找到更佳候选，保留初值转速",
                "inconclusive_budget": "复核预算不足，保留初值转速",
                "insufficient_evaluations": "复核评估次数不足，保留初值转速",
                "sampling_limit": "复核采样超限，保留初值转速",
            }.get(report["validation_status"], str(report["validation_status"]))
        if status == "evaluation_limit":
            return ("评估次数已用尽，未证实时间收敛"
                    if report.get("algorithm") == "reference"
                    else "评估次数已用尽，返回当前最佳局部候选")
        return {
            "completed": "工艺积分完成",
            "temporal_converged": "时间采样一致性检查通过",
            "sampling_limit": "已达采样上限，未证实时间收敛",
            "budget_exhausted": "时间预算已用尽，仅返回上一轮完整评估；未证实收敛",
            "local_step_limit": "已达局部步长下限，返回当前最佳局部候选",
        }.get(status, str(status))

    @staticmethod
    def _format_algorithm_report(report: object) -> str:
        if not isinstance(report, dict) or not report:
            return ""
        algorithm = report.get("algorithm", "")
        label = MainBridge._ALGORITHM_LABELS.get(algorithm, "算法报告")
        lines = [f"计算路径：{label}", f"状态：{MainBridge._report_status(report)}"]
        if algorithm == "optimize" and report.get("validation_status"):
            stop_reason = {
                "evaluation_limit": "搜索评估次数上限",
                "local_step_limit": "局部步长下限",
                "budget_exhausted": "时间预算已用尽",
            }.get(report.get("status"), str(report.get("status", "")))
            lines.append(f"搜索停止条件：{stop_reason}")
        if report.get("evaluations") is not None:
            lines.append(f"已完成评估：{report['evaluations']} 次")
        if report.get("scope"):
            lines.append(f"范围：{report['scope']}")
        if report.get("provisional_best_carrier_rpm") is not None:
            lines.append(f"搜索候选转速：{float(report['provisional_best_carrier_rpm']):.6g} rpm")
        if report.get("provisional_best_cv_percent") is not None:
            lines.append(f"搜索候选 CV（原采样）：{float(report['provisional_best_cv_percent']):.6g}%")
        accepted_rpm = report.get("accepted_carrier_rpm", report.get("best_carrier_rpm"))
        if accepted_rpm is not None:
            lines.append(f"采用大盘转速：{float(accepted_rpm):.6g} rpm")
        if report.get("best_rpm_scale") is not None:
            lines.append(f"采用转速倍率：{float(report['best_rpm_scale']):.6g}")
        validation = report.get("validation") or {}
        if validation.get("time_steps") is not None:
            lines.append(f"加密复核采样：{validation['time_steps']} 步")
        if (validation.get("baseline_cv_percent") is not None
                and validation.get("candidate_cv_percent") is not None):
            lines.append(f"同采样复核 CV：初值 {float(validation['baseline_cv_percent']):.6g}%"
                         f" / 候选 {float(validation['candidate_cv_percent']):.6g}%")
        accepted_cv = report.get("accepted_cv_percent", report.get("best_cv_percent"))
        if accepted_cv is not None:
            lines.append(f"返回结果 CV：{float(accepted_cv):.6g}%")
        if report.get("unverified_dimensions"):
            lines.append("尚未验证：" + "、".join(map(str, report["unverified_dimensions"])))
        if report.get("warning"):
            lines.append(f"注意：{report['warning']}")
        return "\n".join(lines)

    def _refresh_history(self) -> None:
        try:
            records = self.service.recent_history(20)
            self._history = [
                " · ".join(f"{key}: {value}" for key, value in item.items())
                for item in records]
        except (ValueError, OSError):
            self._history = []

    @Slot(QUrl)
    def exportPdf(self, url: QUrl) -> None:
        if self._result is None or self._export_inputs is None:
            self._status = "尚无可导出的计算结果。"
            self.changed.emit()
            return
        try:
            target = (self._project_compute.destination(Path(url.toLocalFile()).expanduser())
                      if self._project_compute is not None
                      else Path(url.toLocalFile()).expanduser().resolve())
            saved = self.service.export_result(
                target, result=self._result,
                settings=self._export_inputs["settings"],
                machine=self._export_inputs["machine"],
                process=self._export_inputs["process"],
                elapsed_s=self._export_inputs["elapsed_s"],
                requested_device=self._export_inputs["requested_device"],
                overwrite=True, sections=self._pdf_sections)
            self._status = f"PDF 已保存：{saved}"
        except (ValueError, OSError, KeyError) as exc:
            self._status = f"PDF 导出失败：{exc}"
        self.changed.emit()
