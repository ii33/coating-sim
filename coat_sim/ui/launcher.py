"""Qt Quick launcher for independent simulation and design modules."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import sys
import threading
from typing import Any, Callable, TYPE_CHECKING, cast
import uuid
import weakref

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, Property, QPropertyAnimation,
                            Qt, QTimer, QUrl, Signal, Slot)
from PySide6.QtGui import QGuiApplication, QImageReader, QWindow
from PySide6.QtQml import (QQmlApplicationEngine, QQmlComponent, QQmlContext,
                           QQmlIncubator, QQmlIncubationController)
from PySide6.QtWidgets import QApplication

from coat_sim.ui.settings import apply_font_settings, configure_qt_dpi_scale
from coat_sim.ui.theme_service import configure_theme_from_settings
from coat_sim.ui.icons import window_icon
from coat_sim.ui.local_files import local_file_path
from coat_sim.ui.window_geometry import WindowGeometryManager
from coat_sim.version import BUILD_IDENTITY, format_startup_line


if TYPE_CHECKING:
    from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
    from coat_sim.ui.global_resources import GlobalResourcesBridge
    from coat_sim.ui.project_application_client import ProjectApplicationClient
    from coat_sim.ui.project_application_lifecycle import ProjectApplicationLifecycle

UI_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = UI_DIR.parents[1]
QML_DIR = UI_DIR / "qml"
BACKGROUND = UI_DIR / "assets" / "launcher.png"

# Qt Quick is the primary film UI. ``--film-ui legacy`` keeps the established
# Widgets implementation available as an explicit rollback route.
QUICK_FILM_MODULES = {
    "film_design": (
        "coat_sim.ui.film_quick_controller",
        "FilmDesignQuickController",
        "FilmDesignQuick.qml",
        "controller",
    ),
    "film_optimization": (
        "coat_sim.ui.film_optimization_quick_controller",
        "FilmOptimizationQuickController",
        "FilmOptimizationWindow.qml",
        "controller",
    ),
    "film_simulation": (
        "coat_sim.ui.film_simulation_quick_controller",
        "FilmSimulationQuickController",
        "FilmSimulationQuick.qml",
        "FilmSimulation",
    ),
    "ray_trace": (
        "coat_sim.ui.raytrace",
        "RayTraceWindowController",
        "RayTraceWindow.qml",
        "bridge",
    ),
    "film_appearance": (
        "coat_sim.ui.appearance",
        "AppearanceWindowController",
        "FilmAppearanceWindow.qml",
        "controller",
    ),
}


@dataclass(frozen=True)
class ModuleSpec:
    key: str
    title: str
    detail: str
    python_module: str
    bridge_class: str
    qml_file: str


MODULES = (
    ModuleSpec("main", "膜厚仿真", "选择工艺并计算膜厚",
               "coat_sim.ui.main", "MainBridge", "MainWindow.qml"),
    ModuleSpec("process", "工艺", "安排镀膜步骤",
               "coat_sim.ui.process", "ProcessBridge", "ProcessWindow.qml"),
    ModuleSpec("machine", "机器", "配置机器结构",
               "coat_sim.ui.machine_mask", "MachineBridge", "MachineWindow.qml"),
    ModuleSpec("mask", "修正板", "绘制修正板",
               "coat_sim.ui.machine_mask", "MaskBridge", "MaskWindow.qml"),
    ModuleSpec("source", "蒸发源", "设计蒸发源",
               "coat_sim.ui.source_substrate", "SourceBridge", "SourceWindow.qml"),
    ModuleSpec("substrate", "基片", "设计基片",
               "coat_sim.ui.source_substrate", "SubstrateBridge", "SubstrateWindow.qml"),
    ModuleSpec("film_design", "设计", "图形编辑膜堆与光学数据组",
               "coat_sim.ui.film_design", "FilmDesignWindow", ""),
    ModuleSpec("film_optimization", "膜系优化", "优化固定材料膜层的物理厚度",
               "coat_sim.ui.film_optimization_quick_controller",
               "FilmOptimizationQuickController", "FilmOptimizationWindow.qml"),
    ModuleSpec("film_simulation", "仿真计算", "计算本征 R/T/A 与光源光谱",
               "coat_sim.ui.film_design", "FilmSimulationWindow", ""),
    ModuleSpec("ray_trace", "光线追迹", "几何成像与沉积逐层膜厚",
               "coat_sim.ui.raytrace", "RayTraceWindowController", "RayTraceWindow.qml"),
    ModuleSpec("settings", "设置", "字体与界面缩放",
               "coat_sim.ui.settings", "SettingsBridge", "SettingsWindow.qml"),
    ModuleSpec("about", "关于与帮助", "项目介绍、算法与许可",
               "coat_sim.ui.about", "AboutBridge", "AboutWindow.qml"),
    ModuleSpec("materials", "材料库", "浏览、筛选与编辑材料",
               "coat_sim.ui.source_materials", "MaterialLibraryBridge", "MaterialLibraryHost.qml"),
)


# Contextual routes without entries in the ordinary business-module catalogue.
# Global resources is exposed only by its explicit launcher global action;
# appearance is routed by RayTrace's process asset context.
_INTERNAL_MODULES = (
    ModuleSpec("global_resources", "全局资产库", "浏览和编辑全局材料与设计资产",
               "coat_sim.ui.global_resources", "GlobalResourcesBridge", "GlobalResourcesWindow.qml"),
    ModuleSpec("film_appearance", "薄膜外观", "基于沉积逐层厚度场的光谱外观",
               "coat_sim.ui.appearance", "AppearanceWindowController",
               "FilmAppearanceWindow.qml"),
)
_ROUTE_MODULES = MODULES + _INTERNAL_MODULES


@dataclass
class WindowHandle:
    instance_id: str
    key: str
    window: Any
    bridge: QObject
    context: QQmlContext | None
    component: QQmlComponent | None
    engine: Any | None = None
    title_bridge: Any | None = None
    open_payload: Any = None


@dataclass
class _PendingQmlOpen:
    spec: ModuleSpec
    request: WindowOpenRequest
    bridge: QObject
    context: QQmlContext
    component: QQmlComponent
    engine: Any
    title_bridge: WindowTitleBridge
    initial_properties: dict[str, Any]
    maximized: bool
    on_ready: Any | None = None
    incubator: Any | None = None
    cancelled: bool = False
    activate_when_ready: bool = False


class _LauncherIncubator(QQmlIncubator):
    """Creates QML trees asynchronously while staying on the GUI thread."""

    def __init__(self, launcher: "LauncherBridge", instance_id: str):
        super().__init__(QQmlIncubator.IncubationMode.Asynchronous)
        self._launcher = weakref.ref(launcher)
        self._instance_id = instance_id

    def statusChanged(self, status) -> None:
        launcher = self._launcher()
        if launcher is not None:
            launcher._qml_incubator_changed(self._instance_id, status)


class _BudgetedIncubationController(QQmlIncubationController):
    """Fallback time-slicer for engines without a QQuickWindow controller."""

    def __init__(self, parent: QObject, budget_ms: int = 4):
        super().__init__()
        self._budget_ms = max(1, int(budget_ms))
        self._timer = QTimer(parent)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._incubate_slice)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def _incubate_slice(self) -> None:
        if self.incubatingObjectCount() <= 0:
            self._timer.stop()
            return
        self.incubateFor(self._budget_ms)
        if self.incubatingObjectCount() <= 0:
            self._timer.stop()


class _ProjectQuitGuard(QObject):
    """Route ordinary QApplication.quit through the launcher's close policy."""

    def __init__(self, launcher: "LauncherBridge"):
        super().__init__(launcher)
        self.launcher = launcher

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Quit and not self.launcher._closing:
            self.launcher.shutdown()
            return True
        return super().eventFilter(watched, event)


@dataclass(frozen=True)
class WindowOpenRequest:
    instance_id: str
    key: str
    document_path: str = ""
    payload: Any = None
    mode: str = "new"


class WindowTitleBridge(QObject):
    """Per-window title and document metadata shared with module owners.

    Document controllers should expose documentName, documentPath,
    documentDirty and documentChanged. Legacy properties remain readable while
    owners migrate to that common contract.
    """

    changed = Signal()

    _DOCUMENT_MODULES = frozenset({
        "main", "process", "machine", "mask", "source", "substrate",
        "film_design", "materials",
    })

    def __init__(self, launcher: "LauncherBridge", instance_id: str,
                 module_key: str, module_title: str, source: QObject,
                 initial_path: str = ""):
        super().__init__(launcher)
        self.launcher = launcher
        self.instance_id = instance_id
        self.module_key = module_key
        self.module_title = module_title
        self.source = source
        self.initial_path = initial_path
        self._window = None
        self._title = module_title
        self._snapshot = None
        for signal_name in ("documentChanged", "documentNameChanged",
                            "documentDirtyChanged", "changed", "dirtyChanged",
                            "inputsChanged", "fileChanged", "pathChanged",
                            "stateChanged", "uiStateChanged", "designMetaChanged"):
            signal = getattr(source, signal_name, None)
            if signal is not None and hasattr(signal, "connect"):
                try:
                    signal.connect(self.refresh)
                except (RuntimeError, TypeError):
                    pass
        self.refresh()

    @staticmethod
    def _value(source: Any, *names: str) -> Any:
        for name in names:
            try:
                value = getattr(source, name)
            except (AttributeError, RuntimeError):
                continue
            if callable(value):
                try:
                    value = value()
                except TypeError:
                    continue
            if value is not None:
                return value
        return None

    @staticmethod
    def _path_string(value: Any) -> str:
        if value is None:
            return ""
        to_local_file = getattr(value, "toLocalFile", None)
        if callable(to_local_file):
            value = to_local_file() or value.toString()
        try:
            return os.fspath(value).strip()
        except TypeError:
            return str(value).strip()

    def _document_path(self) -> str:
        if self.module_key == "film_design":
            state = self._value(self.source, "uiState", "state")
            if isinstance(state, dict) and "path" in state:
                # Empty is an intentional untitled document, not a reason to
                # resurrect the path from this window's first open request.
                return self._path_string(state.get("path"))
        path = ""
        has_path_owner = False
        for name in ("documentPath", "designPath", "currentPath", "filePath",
                     "_path", "path"):
            try:
                getattr(self.source, name)
            except (AttributeError, RuntimeError):
                continue
            has_path_owner = True
            path = self._path_string(self._value(self.source, name))
            if path:
                break
        if not path and self.module_key == "main":
            paths = getattr(self.source, "_paths", {})
            if isinstance(paths, dict):
                has_path_owner = True
                path = self._path_string(paths.get("process"))
        return path if has_path_owner else self.initial_path

    def _document_name(self, path: str) -> str:
        value = self._value(self.source, "documentName")
        if not value:
            value = self._value(self.source, "fileName", "designName")
        if not value and self.module_key == "film_design":
            meta = self._value(self.source, "designMeta")
            if isinstance(meta, dict):
                value = meta.get("name")
        name = str(value or "").strip()
        if name and ("/" in name or "\\" in name):
            name = Path(name).name
        if not name and path:
            name = Path(path).name
        return name

    def _document_dirty(self) -> bool:
        value = self._value(self.source, "documentDirty")
        if value is not None:
            return bool(value)
        if self.module_key not in self._DOCUMENT_MODULES:
            return False
        value = self._value(self.source, "dirty", "_dirty")
        if value is not None:
            return bool(value)
        has_dirty = getattr(self.source, "_has_dirty", None)
        if callable(has_dirty):
            try:
                return bool(has_dirty())
            except RuntimeError:
                return False
        return False

    @Property(str, constant=True)
    def instanceId(self) -> str:
        return self.instance_id

    @Property(str, constant=True)
    def moduleKey(self) -> str:
        return self.module_key

    @Property(str, notify=changed)
    def title(self) -> str:
        return self._title

    @Property(str, notify=changed)
    def documentName(self) -> str:
        return self._document_name(self._document_path())

    @Property(str, notify=changed)
    def documentPath(self) -> str:
        return self._document_path()

    @Property(bool, notify=changed)
    def documentDirty(self) -> bool:
        return self._document_dirty()

    @Property(bool, notify=changed)
    def hasDocument(self) -> bool:
        explicit = self._value(self.source, "hasDocument")
        if explicit is not None:
            return bool(explicit)
        return bool(self.documentName or self.documentPath
                    or (self.module_key in self._DOCUMENT_MODULES
                        and self.documentDirty))

    @Slot(result=bool)
    def activateForConfirmation(self) -> bool:
        return self.launcher.activateWindow(self.instance_id)

    def attach_window(self, window: Any) -> None:
        self._window = window
        window.setProperty("_coat_sim_window_instance_id", self.instance_id)
        window.setProperty("_coat_sim_window_list_id", self.instance_id)
        window.setProperty("_coat_sim_module_key", self.module_key)
        self.refresh()

    @Slot()
    def refresh(self) -> None:
        path = self._document_path()
        name = self._document_name(path)
        dirty = self._document_dirty()
        has_document = self.hasDocument
        if has_document and not name:
            name = "未命名"
        title = self.module_title
        if has_document:
            title += f" — {name}"
            if dirty:
                title += " *"
        snapshot = (title, name, path, dirty, has_document)
        metadata_changed = snapshot != self._snapshot
        self._title = title
        self._snapshot = snapshot
        window = self._window
        if window is not None:
            setter = getattr(window, "setTitle", None)
            if not callable(setter):
                setter = getattr(window, "setWindowTitle", None)
            try:
                getter = getattr(window, "title", None)
                if not callable(getter):
                    getter = getattr(window, "windowTitle", None)
                current_title = getter() if callable(getter) else None
                if callable(setter) and current_title != title:
                    setter(title)
                meta = window.metaObject()
                if (meta.indexOfProperty("caption") >= 0
                        and window.property("caption") != title):
                    window.setProperty("caption", title)
                if window.property("_coat_sim_window_title") != title:
                    window.setProperty("_coat_sim_window_title", title)
            except (AttributeError, RuntimeError):
                pass
        if metadata_changed:
            self.changed.emit()
            self.launcher.windowsChanged.emit()


class LauncherBridge(QObject):
    launchError = Signal(str)
    fontSettingsChanged = Signal()
    loadingChanged = Signal()
    windowsChanged = Signal()
    moduleImported = Signal(str, object, str)
    shutdownConfirmationRequested = Signal()

    def __init__(self, engine: QQmlApplicationEngine, *, software_mode: bool = False,
                 with_launcher: bool = True, film_ui_backend: str = "quick",
                 window_settings=None):
        super().__init__()
        if film_ui_backend not in ("quick", "legacy"):
            raise ValueError("film_ui_backend must be 'quick' or 'legacy'")
        self.engine = engine
        self._window_settings = window_settings
        self._software_mode = software_mode
        self._film_ui_backend = film_ui_backend
        self._closing = False
        self._shutdown_confirmation_pending = False
        self._project_shutdown_approved = False
        self.project_lifecycle: ProjectApplicationLifecycle | None = None
        self.project_application: ProjectApplicationClient | None = None
        self._quit_guard: _ProjectQuitGuard | None = None
        self._windows: dict[str, WindowHandle] = {}
        self._loading: set[str] = set()
        self._pending_opens: dict[str, list[WindowOpenRequest]] = {}
        self._open_requests: dict[str, tuple[ModuleSpec, WindowOpenRequest]] = {}
        self._asset_material_open_requests: set[str] = set()
        self._deferred_open_intents: dict[str, WindowOpenRequest] = {}
        self._deferred_companion_starts: set[str] = set()
        self._film_companion_auto_start: set[str] = set()
        self._film_detached_latest_snapshots: dict[object, Any] = {}
        self._pending_qml: dict[str, _PendingQmlOpen] = {}
        self._incubation_controllers: dict[int, _BudgetedIncubationController] = {}
        self._previous_incubation_controllers: dict[int, Any] = {}
        self._incubation_engine_connections: set[int] = set()
        self._window_animations: dict[str, QPropertyAnimation] = {}
        self._presentation_timers: dict[str, QTimer] = {}
        self._presentation_connections: dict[str, tuple[Any, Any]] = {}
        self._geometry_manager = WindowGeometryManager(self, settings=window_settings)
        self._modules: dict[str, Any] = {}
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(100)
        self._shutdown_timer.timeout.connect(self._finish_shutdown)
        self.moduleImported.connect(self._finish_import)
        self._legacy_thickness_provider_registered = False
        self._queue_view = None
        self.assistant_controller: Any | None = None
        self.assistant_debug: Any | None = None
        self._assistant_queue_result_source = None
        self.control_server: Any | None = None
        self.control_registry: Any | None = None
        self._launcher_window: Any | None = None
        self.project_runtime: Any | None = None
        self._project_starting = False
        self._project_activation_callback: Callable[[], None] | None = None
        self._runtime_shutdown_callback: Callable[[], None] | None = None
        self._runtime_shutdown_authorized = False
        self._runtime_shutdown_requested = False
        self.project_launcher = None
        if with_launcher:
            from .launcher_projects import ProjectLauncherAdapter
            self.project_launcher = ProjectLauncherAdapter(
                self, geometry_settings=window_settings)
        self._image_width = self._image_height = 1
        if with_launcher:
            reader = QImageReader(str(BACKGROUND))
            size = reader.size()
            if not size.isValid():
                raise RuntimeError(f"无法读取启动器背景图：{BACKGROUND}\n{reader.errorString()}")
            self._image_width = size.width()
            self._image_height = size.height()

    @Slot(str)
    def _present_project_close(self, _project_id: str) -> None:
        if self._launcher_window is not None:
            self._activate_window(self._launcher_window)

    @Property("QVariantList", constant=True)
    def modules(self) -> list[dict[str, str]]:
        return [{"key": item.key, "title": item.title, "detail": item.detail}
                for item in MODULES if item.key != "materials"]

    @Slot(QUrl, result=str)
    def localFilePath(self, url: QUrl) -> str:
        """Decode file-dialog URLs at the typed Python/Qt boundary."""
        return local_file_path(url)

    @Property(QUrl, constant=True)
    def backgroundUrl(self) -> QUrl:
        return QUrl.fromLocalFile(str(BACKGROUND))

    @Property(int, constant=True)
    def backgroundWidth(self) -> int:
        return self._image_width

    @Property(int, constant=True)
    def backgroundHeight(self) -> int:
        return self._image_height

    @Property(bool, constant=True)
    def softwareMode(self) -> bool:
        return self._software_mode

    @Property(float, notify=fontSettingsChanged)
    def fontScale(self) -> float:
        app = QGuiApplication.instance()
        return max(0.5, app.font().pointSizeF() / 12.0) if app else 1.0

    @Property("QVariantList", notify=loadingChanged)
    def loadingKeys(self) -> list[str]:
        return sorted(self._loading)

    @Property("QVariantList", notify=loadingChanged)
    def openingKeys(self) -> list[str]:
        return sorted({request.key for _, request in self._open_requests.values()})

    @Property("QVariantList", notify=windowsChanged)
    def windowInstances(self) -> list[dict[str, Any]]:
        return [{"id": handle.instance_id, "key": handle.key,
                 "title": handle.title_bridge.title if handle.title_bridge else handle.key,
                 "documentPath": handle.title_bridge.documentPath if handle.title_bridge else "",
                 "dirty": handle.title_bridge.documentDirty if handle.title_bridge else False}
                for handle in self._windows.values()]

    def _set_loading(self, key: str, loading: bool) -> None:
        if loading:
            if key in self._loading:
                return
            self._loading.add(key)
        else:
            if key not in self._loading:
                return
            self._loading.remove(key)
        self.loadingChanged.emit()

    @staticmethod
    def _activate_window(window: Any) -> None:
        if window.windowState() & Qt.WindowMinimized:
            window.showNormal()
        elif not window.isVisible():
            window.show()
        window.raise_()
        if hasattr(window, "requestActivate"):
            window.requestActivate()
        else:
            window.activateWindow()

    def _handles_for(self, key: str) -> list[WindowHandle]:
        handles = []
        for handle in list(self._windows.values()):
            if handle.key != key:
                continue
            try:
                handle.window.isVisible()
            except RuntimeError:
                self._forget_window(handle.instance_id, delete_window=False)
                continue
            handles.append(handle)
        return handles

    def _existing_window(self, key: str) -> WindowHandle | None:
        """The control API reads the same sole instance as launcher routes."""
        handles = self._handles_for(key)
        return handles[0] if handles else None

    @staticmethod
    def _same_document_path(first: str, second: str) -> bool:
        return bool(first and second and os.path.normcase(os.path.abspath(first))
                    == os.path.normcase(os.path.abspath(second)))

    @staticmethod
    def _same_payload(first: Any, second: Any) -> bool:
        if first is second:
            return True
        if isinstance(first, dict) and isinstance(second, dict):
            try:
                return bool(first == second)
            except (TypeError, ValueError):
                return False
        return False

    def _reuse_window_intent(self, handle: WindowHandle,
                             request: WindowOpenRequest) -> bool:
        """Route explicit new inputs without taking ownership of editor state."""
        self._activate_window(handle.window)
        if not request.document_path and request.payload is None:
            return True
        title = handle.title_bridge
        if (request.payload is None and title is not None
                and self._same_document_path(request.document_path, title.documentPath)):
            return True
        bridge = handle.bridge
        reuse = getattr(bridge, "reuseWindowOpen", None)
        if (not callable(reuse) and not request.document_path
                and request.payload is not None
                and self._same_payload(request.payload, handle.open_payload)):
            return True
        try:
            if callable(reuse):
                accepted = bool(reuse(request.document_path, request.payload))
                if not accepted:
                    self.launchError.emit("窗口未接受新的打开意图；当前内容已保留。")
                elif request.payload is not None:
                    handle.open_payload = request.payload
                return accepted
            if bool(WindowTitleBridge._value(bridge, "busy", "_busy")):
                raise ValueError("窗口正在处理任务；请等待完成或取消后再切换输入。")
            if request.document_path:
                guarded_open = getattr(bridge, "requestOpenFile", None)
                if callable(guarded_open):
                    guarded_open(QUrl.fromLocalFile(request.document_path))
                    return True
                dirty = (bool(title.documentDirty) if title is not None else
                         bool(WindowTitleBridge._value(bridge, "documentDirty", "dirty", "_dirty")))
                if dirty and not callable(getattr(bridge, "confirmDocumentReplace", None)):
                    raise ValueError("窗口有未保存修改；请先保存，或在该窗口内打开文件并确认放弃修改。")
                opener = (getattr(bridge, "openDocument", None)
                          or getattr(bridge, "open_document", None))
                if not callable(opener):
                    raise ValueError("此窗口不支持在现有实例中打开文件。")
                opener(request.document_path)
                return True
            if request.payload is not None:
                if title is not None and bool(title.documentDirty):
                    raise ValueError("窗口有未保存修改；请先保存后再切换设计。")
                if handle.key == "film_simulation" and isinstance(request.payload, dict):
                    setter = getattr(bridge, "setDesign", None) or getattr(bridge, "set_design", None)
                    if callable(setter):
                        setter(request.payload)
                        handle.open_payload = request.payload
                        return True
                if handle.key == "ray_trace":
                    if not isinstance(request.payload, dict) or set(request.payload) != {"result_path"}:
                        raise ValueError("ray-trace result window received an invalid queue payload")
                    downloads = (PROJECT_ROOT / "artifacts/runtime/scheduler/downloads").resolve()
                    path = Path(request.payload["result_path"]).resolve()
                    if downloads not in path.parents or not path.is_file():
                        raise ValueError("ray-trace result is outside the scheduler download cache")
                    bridge.loadResult(str(path))
                    handle.open_payload = request.payload
                    return True
                raise ValueError("此窗口不能安全切换设计来源；请先关闭当前窗口再打开。")
        except (RuntimeError, ValueError, TypeError, OSError) as exc:
            self.launchError.emit(f"无法复用 {handle.key} 窗口：{exc}")
            return False
        return True

    def _handle_for_instance(self, instance_id: str) -> WindowHandle | None:
        return self._windows.get(str(instance_id))

    @Slot(str, result=bool)
    def activateWindow(self, instance_id: str) -> bool:
        handle = self._handle_for_instance(instance_id)
        if handle is None:
            return str(instance_id) in self._open_requests
        self._activate_window(handle.window)
        return True

    @Slot(str, result=bool)
    def closeWindow(self, instance_id: str) -> bool:
        instance_id = str(instance_id)
        handle = self._handle_for_instance(instance_id)
        if handle is None:
            return self._cancel_open_request(instance_id)
        self._activate_window(handle.window)
        try:
            return bool(handle.window.close())
        except RuntimeError:
            return False

    @Slot(QObject, str, bool, result=bool)
    def prepareWindowGeometry(self, window: QObject, module_key: str,
                              preserve_existing_geometry: bool = False) -> bool:
        """Apply the shared first-open or restored geometry policy to a window."""
        try:
            screen = self._launcher_screen(window)
            self._geometry_manager.prepare(
                window, module_key, screen=screen,
                preserve_existing=preserve_existing_geometry)
            return True
        except (TypeError, RuntimeError, ValueError) as exc:
            self.launchError.emit(f"Unable to initialize window geometry: {exc}")
            return False

    def _launcher_screen(self, window: Any | None = None):
        launcher_window = self._launcher_window
        if launcher_window is not None:
            try:
                screen = launcher_window.screen()
                if screen is not None:
                    return screen
            except (AttributeError, RuntimeError):
                pass
        if window is not None:
            try:
                screen = window.screen()
                if screen is not None:
                    return screen
            except (AttributeError, RuntimeError):
                pass
        app = QGuiApplication.instance()
        return app.primaryScreen() if app is not None else None

    @Slot(str, str, result=bool)
    def hasOtherOpenDocument(self, document_path: str,
                             excluding_instance_id: str = "") -> bool:
        normalized = os.path.normcase(os.path.abspath(document_path))
        if not document_path:
            return False
        return any(
            handle.instance_id != excluding_instance_id
            and handle.title_bridge is not None
            and handle.title_bridge.documentPath
            and os.path.normcase(os.path.abspath(handle.title_bridge.documentPath)) == normalized
            for handle in self._windows.values()
        )

    def open_window(self, module_key: str, document_path: str = "",
                    *, mode: str = "", payload: Any = None) -> str:
        if self._closing:
            return ""
        if (module_key == "materials" and self._existing_window("materials") is None
                and not any(request.key == "materials" for _, request in self._open_requests.values())):
            if document_path or payload is not None:
                self.launchError.emit("请在资产库材料区明确导入材料文件。")
                return ""
            identity = self.open_window("global_resources")
            handle = self._handle_for_instance(identity)
            if handle is not None:
                cast("GlobalResourcesBridge", handle.bridge).openMaterialSection()
            elif identity:
                self._asset_material_open_requests.add(identity)
            return identity
        spec = next((item for item in _ROUTE_MODULES if item.key == module_key), None)
        if spec is None:
            self.launchError.emit(f"Unknown window module: {module_key}")
            return ""
        if module_key == "film_optimization" and self._film_ui_backend != "quick":
            self.launchError.emit("膜系厚度优化需要 Qt Quick 膜系设计器")
            return ""
        mode = str(mode or ("open" if document_path else "new")).strip().lower()
        if mode not in ("new", "open") or (mode == "open" and not document_path):
            self.launchError.emit("Window open mode must be 'new' or 'open' with a document path")
            return ""
        existing = self._existing_window(module_key)
        if existing is not None:
            request = WindowOpenRequest(
                existing.instance_id, module_key, str(document_path or ""), payload, mode)
            return existing.instance_id if self._reuse_window_intent(existing, request) else ""
        pending = next((request for _, request in self._open_requests.values()
                        if request.key == module_key), None)
        if pending is not None:
            if document_path or payload is not None:
                request = WindowOpenRequest(
                    pending.instance_id, module_key, str(document_path or ""), payload, mode)
                changed = (request.document_path != pending.document_path
                           or not self._same_payload(request.payload, pending.payload))
                if pending.instance_id in self._pending_qml and not changed:
                    # An explicit return to the constructor's original source
                    # supersedes an earlier deferred different-source request.
                    self._deferred_open_intents.pop(pending.instance_id, None)
                    self._deferred_companion_starts.discard(pending.instance_id)
                elif changed:
                    if pending.instance_id in self._pending_qml:
                        self._deferred_open_intents[pending.instance_id] = request
                    else:
                        self._open_requests[pending.instance_id] = (spec, request)
                        queued = self._pending_opens.get(module_key, [])
                        self._pending_opens[module_key] = [
                            request if item.instance_id == pending.instance_id else item
                            for item in queued]
            pending_qml = self._pending_qml.get(pending.instance_id)
            if pending_qml is not None:
                pending_qml.activate_when_ready = True
            return pending.instance_id
        request = WindowOpenRequest(
            uuid.uuid4().hex, module_key, str(document_path or ""), payload, mode)
        self._queue_open(spec, request)
        return request.instance_id

    @Slot(str, result=str)
    @Slot(str, str, result=str)
    @Slot(str, str, str, result=str)
    def openWindow(self, module_key: str, document_path: str = "",
                   mode: str = "") -> str:
        """Open or activate the sole same-process instance of this module."""
        return self.open_window(module_key, document_path, mode=mode)

    @Slot(str, result=str)
    def openOrActivate(self, module_key: str) -> str:
        """Use the same type-singleton route for utility and module callers."""
        return self.open_window(module_key)

    def _queue_open(self, spec: ModuleSpec, request: WindowOpenRequest) -> None:
        if self._closing:
            return
        self._open_requests[request.instance_id] = (spec, request)
        self.loadingChanged.emit()
        module = self._modules.get(spec.key)
        if module is not None:
            QTimer.singleShot(0, lambda identity=request.instance_id:
                              self._dispatch_open(identity))
            return
        self._pending_opens.setdefault(spec.key, []).append(request)
        if spec.key in self._loading:
            return
        self._set_loading(spec.key, True)
        try:
            threading.Thread(target=self._import_module, args=(spec,),
                             name=f"load-{spec.key}", daemon=True).start()
        except Exception as exc:
            requests = self._pending_opens.pop(spec.key, [])
            self._set_loading(spec.key, False)
            for item in requests:
                self._finish_open_request(item.instance_id)
            self.launchError.emit(f"Unable to open {spec.title}: {exc}")

    def _dispatch_open(self, instance_id: str) -> None:
        entry = self._open_requests.get(str(instance_id))
        if entry is None or self._closing:
            return
        spec, request = entry
        if instance_id in self._windows or instance_id in self._pending_qml:
            return
        module = self._modules.get(spec.key)
        if module is None:
            return
        self._open_one(spec, module, request)

    def _finish_open_request(self, instance_id: str) -> None:
        instance_id = str(instance_id)
        self._clear_presentation_wait(instance_id)
        self._film_companion_auto_start.discard(instance_id)
        self._deferred_open_intents.pop(instance_id, None)
        self._deferred_companion_starts.discard(instance_id)
        entry = self._open_requests.get(instance_id)
        if entry is not None:
            _spec, request = entry
            owner_token = getattr(request.payload, "owner_token", None)
            if owner_token is not None:
                self._film_detached_latest_snapshots.pop(owner_token, None)
        if self._open_requests.pop(instance_id, None) is not None:
            self.loadingChanged.emit()

    def _await_window_presentation(self, instance_id: str, window: Any) -> None:
        """Keep the menu busy until a first frame or bounded display fallback."""
        instance_id = str(instance_id)
        if instance_id not in self._open_requests:
            return
        frame_swapped = getattr(window, "frameSwapped", None)
        if frame_swapped is not None and hasattr(frame_swapped, "connect"):
            callback = lambda *_args, identity=instance_id: self._finish_open_request(identity)
            frame_swapped.connect(callback)
            self._presentation_connections[instance_id] = (frame_swapped, callback)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(1500)
        timer.timeout.connect(
            lambda identity=instance_id: self._finish_open_request(identity))
        self._presentation_timers[instance_id] = timer
        timer.start()

    def _clear_presentation_wait(self, instance_id: str) -> None:
        timer = self._presentation_timers.pop(str(instance_id), None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        connection = self._presentation_connections.pop(str(instance_id), None)
        if connection is not None:
            signal, callback = connection
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass

    def _cancel_open_request(self, instance_id: str) -> bool:
        instance_id = str(instance_id)
        self._asset_material_open_requests.discard(instance_id)
        pending = self._pending_qml.get(instance_id)
        if pending is not None:
            pending.cancelled = True
            if pending.incubator is not None:
                try:
                    pending.incubator.clear()
                except RuntimeError:
                    pass
            self._pending_qml.pop(instance_id, None)
            self._release_pending_qml(pending)
            self._finish_open_request(instance_id)
            return True
        entry = self._open_requests.get(instance_id)
        if entry is None:
            return False
        spec, request = entry
        queued = self._pending_opens.get(spec.key, [])
        self._pending_opens[spec.key] = [
            item for item in queued if item.instance_id != request.instance_id]
        if not self._pending_opens[spec.key]:
            self._pending_opens.pop(spec.key, None)
        self._finish_open_request(instance_id)
        return True

    def _set_instance_context(self, context: QQmlContext,
                              request: WindowOpenRequest,
                              title_bridge: WindowTitleBridge) -> None:
        context.setContextProperty("windowInstanceId", request.instance_id)
        context.setContextProperty("initialDocumentPath", request.document_path)
        context.setContextProperty("documentOpenMode", request.mode)
        context.setContextProperty("windowTitleBridge", title_bridge)

    @staticmethod
    def _open_initial_document(bridge: Any, request: WindowOpenRequest) -> None:
        if not request.document_path:
            return
        opener = (getattr(bridge, "openDocument", None)
                  or getattr(bridge, "open_document", None))
        if callable(opener):
            opener(request.document_path)

    def _register_window(self, spec: ModuleSpec, request: WindowOpenRequest,
                         window: Any, bridge: QObject,
                         context: QQmlContext | None,
                         component: QQmlComponent | None,
                         engine: Any | None,
                         title_bridge: WindowTitleBridge | None = None,
                         *, complete_open_request: bool = True) -> WindowHandle:
        if title_bridge is None:
            title_bridge = WindowTitleBridge(
                self, request.instance_id, spec.key, spec.title, bridge,
                request.document_path)
        if context is not None:
            self._set_instance_context(context, request, title_bridge)
        if self._existing_window(spec.key) is not None:
            raise RuntimeError(f"A {spec.key} window is already registered")
        deferred = self._deferred_open_intents.pop(request.instance_id, None)
        deferred_start = request.instance_id in self._deferred_companion_starts
        self._deferred_companion_starts.discard(request.instance_id)
        handle = WindowHandle(request.instance_id, spec.key, window, bridge,
                              context, component, engine, title_bridge, request.payload)
        self._windows[request.instance_id] = handle
        if complete_open_request:
            self._finish_open_request(request.instance_id)
        title_bridge.attach_window(window)
        visible_changed = getattr(window, "visibleChanged", None)
        if visible_changed is not None and hasattr(visible_changed, "connect"):
            visible_changed.connect(
                lambda visible, identity=request.instance_id:
                self._window_visibility_changed(identity, visible))
        destroyed = getattr(window, "destroyed", None)
        if destroyed is not None and hasattr(destroyed, "connect"):
            destroyed.connect(
                lambda _obj=None, identity=request.instance_id:
                self._forget_window(identity, delete_window=False))
        self.windowsChanged.emit()
        if spec.key == "global_resources" and request.instance_id in self._asset_material_open_requests:
            self._asset_material_open_requests.discard(request.instance_id)
            cast("GlobalResourcesBridge", bridge).openMaterialSection()
        if deferred is not None:
            QTimer.singleShot(0, lambda identity=request.instance_id, intent=deferred,
                              start=deferred_start:
                              self._apply_deferred_open_intent(identity, intent, start=start))
        return handle

    def _apply_deferred_open_intent(self, instance_id: str,
                                    request: WindowOpenRequest, *, start: bool = False) -> None:
        handle = self._handle_for_instance(instance_id)
        if handle is not None and not self._closing:
            if self._reuse_window_intent(handle, request) and start:
                self._start_reused_film_companion(handle)

    @staticmethod
    def _start_reused_film_companion(handle: WindowHandle) -> None:
        """Controller queues starts behind its own replacement confirmation."""
        name = ("requestSourceRefresh" if handle.key == "film_simulation"
                else "requestOptimization")
        starter = getattr(handle.bridge, name, None)
        if not callable(starter) and handle.key == "film_simulation":
            starter = getattr(handle.bridge, "requestCalculation", None)
        if not callable(starter) and handle.key == "film_optimization":
            # Optimization's public shortcut queues behind source replacement.
            starter = getattr(handle.bridge, "startOptimizationFromShortcut", None)
        if callable(starter):
            starter()
        # Never start an old simulation source without its safe request API.

    def _window_visibility_changed(self, instance_id: str, visible: bool) -> None:
        # QWindow stays visible when minimized. A false notification means the
        # accepted close/hide, even if its old windowState still says Minimized.
        # Do not inspect visibility here: Qt emits before that enum is updated.
        if not visible:
            self._forget_window(instance_id)

    def _forget_window(self, instance_id: str, *, delete_window: bool = True) -> None:
        handle = self._windows.pop(instance_id, None)
        if handle is None:
            return
        if handle.key == "film_design":
            mark_closed = getattr(handle.bridge, "markSourceClosed", None)
            if callable(mark_closed):
                try:
                    mark_closed()
                except RuntimeError:
                    pass
        self._finish_open_request(instance_id)
        animation = self._window_animations.pop(instance_id, None)
        if animation is not None:
            animation.stop()
            animation.deleteLater()
        if delete_window:
            delete_later = getattr(handle.window, "deleteLater", None)
            if callable(delete_later):
                delete_later()
        for owned in (handle.context, handle.component):
            if owned is not None:
                try:
                    owned.deleteLater()
                except RuntimeError:
                    pass
        if handle.bridge is not handle.window:
            detach = getattr(handle.bridge, "detach_observers", None)
            if callable(detach):
                try:
                    detach()
                except RuntimeError:
                    pass
            delete_bridge = getattr(handle.bridge, "deleteLater", None)
            if callable(delete_bridge):
                try:
                    delete_bridge()
                except RuntimeError:
                    pass
        for owned in (handle.title_bridge,
                      handle.engine if handle.engine is not self.engine else None):
            if owned is not None:
                try:
                    owned.deleteLater()
                except RuntimeError:
                    pass
        self.windowsChanged.emit()

    def _provide_active_film_design_draft(self, process_instance_id: str) -> None:
        """Deliver the sole open FilmDesign draft to a requesting process editor."""
        process = self._handle_for_instance(process_instance_id)
        if process is None or process.key != "process":
            return
        fail = getattr(process.bridge, "failFilmDesignImport", None)
        receive = getattr(process.bridge, "receiveFilmDesignDraft", None)
        if not callable(fail) or not callable(receive):
            return

        candidates = [handle for handle in self._handles_for("film_design")
                      if handle.window is not None and
                      getattr(handle.window, "isVisible", lambda: False)()]
        active = [handle for handle in candidates
                  if getattr(handle.window, "isActive", lambda: False)()]
        if len(active) == 1:
            candidates = active
        if len(candidates) != 1:
            fail("请保持一个可见的膜系设计窗口后再导入；当前没有唯一可读取的膜系设计。")
            return

        handle = candidates[0]
        bridge = handle.bridge
        design = None
        try:
            if hasattr(bridge, "initializationReady"):
                if not bridge.initializationReady:
                    raise ValueError("膜系设计仍在初始化，请稍后重试。")
                session = getattr(bridge, "session", None)
                if session is None:
                    raise ValueError("当前膜系设计会话尚未就绪。")
                snapshot = session.snapshot()
                if getattr(snapshot, "expression_busy", False):
                    raise ValueError("膜系表达式仍在处理，请完成后重试。")
                design = getattr(snapshot, "design", None)
            else:
                getter = getattr(bridge, "current_design", None)
                if callable(getter):
                    design = getter()
                else:
                    snapshot_getter = getattr(bridge, "snapshot", None)
                    state = snapshot_getter() if callable(snapshot_getter) else None
                    if isinstance(state, dict):
                        design = state.get("document", state.get("data"))
            if not isinstance(design, dict):
                raise ValueError("当前膜系窗口没有可读取的设计内容。")
            label = (str(handle.title_bridge.title) if handle.title_bridge is not None
                     else "当前内存膜系")
            receive(design, label)
        except Exception as exc:
            fail(f"无法读取当前膜系设计：{exc}")

    def _open(self, spec: ModuleSpec, module: Any,
              request: WindowOpenRequest) -> WindowHandle | None:
        if spec.key in ("source", "substrate"):
            # launcher.main() initializes process-wide DPI/font/theme before
            # routed opens; independent helpers bootstrap themselves separately.
            bridge_type = getattr(module, "SourceBridge" if spec.key == "source"
                                   else "SubstrateBridge")
            bridge = bridge_type()
            self._open_initial_document(bridge, request)
            context = QQmlContext(self.engine.rootContext())
            context.setContextProperty("bridge", bridge)
            title_bridge = WindowTitleBridge(
                self, request.instance_id, spec.key, spec.title, bridge,
                request.document_path)
            self._set_instance_context(context, request, title_bridge)
            self._start_async_qml_open(
                spec, request, bridge, context, self.engine,
                QML_DIR / spec.qml_file, title_bridge=title_bridge,
                maximized=False)
            return None
        if spec.key in QUICK_FILM_MODULES and (
                self._film_ui_backend == "quick" or spec.key == "film_appearance"):
            self._open_quick_film(spec, module, request)
            return None
        if spec.key in ("film_design", "film_simulation"):
            window = getattr(module, spec.bridge_class)(PROJECT_ROOT)
            self._open_initial_document(window, request)
            if request.payload is not None and spec.key == "film_simulation":
                setter = getattr(window, "set_design", None) or getattr(window, "setDesign", None)
                if callable(setter):
                    setter(request.payload)
            self._geometry_manager.prepare(
                window, spec.key, screen=self._launcher_screen(window))
            window.setWindowIcon(window_icon(spec.key))
            handle = self._register_window(spec, request, window, window,
                                           None, None, None)
            closed = getattr(window, "closed", None)
            if closed is not None and hasattr(closed, "connect"):
                closed.connect(lambda identity=request.instance_id:
                               self._forget_window(identity))
            if spec.key == "film_design":
                window.simulationRequested.connect(
                    lambda design, source_id=request.instance_id:
                    self._show_film_simulation(source_id, design))
            window.show()
            self._activate_window(window)
            return handle
        bridge_type = getattr(module, spec.bridge_class)
        bridge = bridge_type()
        if spec.key == "process":
            bridge.filmDesignImportRequested.connect(
                lambda identity=request.instance_id:
                self._provide_active_film_design_draft(identity))
        window_engine: Any = self.engine
        if spec.key == "main":
            # A per-instance engine keeps each mutable thickness provider isolated
            # while retaining MainWindow.qml's existing provider identifier.
            window_engine = QQmlApplicationEngine()
            from coat_sim.ui.theme_service import ThemeService
            ThemeService.instance().attach_engine(window_engine)
            window_engine.addImageProvider("thickness", bridge.image_provider)
            window_engine.rootContext().setContextProperty("launcher", self)
            bridge.showQueueRequested.connect(self._show_compute_queue)
        elif spec.key == "settings":
            bridge.changed.connect(self.fontSettingsChanged.emit)
            if self.assistant_controller is not None:
                bridge.assistantSettingsSaved.connect(
                    self.assistant_controller.reload_configuration)
        initial_properties: dict[str, Any] = {}
        if spec.key == "about":
            initial_properties = {
                "internalVersion": bridge.property("internalVersion"),
                "gitCommitLabel": bridge.property("gitCommitLabel"),
            }
        self._open_initial_document(bridge, request)
        context = QQmlContext(window_engine.rootContext())
        context.setContextProperty("bridge", bridge)
        title_bridge = WindowTitleBridge(
            self, request.instance_id, spec.key, spec.title, bridge,
            request.document_path)
        self._set_instance_context(context, request, title_bridge)
        qml_path = QML_DIR / spec.qml_file
        if not qml_path.is_file():
            raise FileNotFoundError(f"模块界面不存在：{qml_path}")
        self._start_async_qml_open(
            spec, request, bridge, context, window_engine, qml_path,
            title_bridge=title_bridge, initial_properties=initial_properties,
            maximized=False)
        return None

    def _module_path(self, spec: ModuleSpec) -> str:
        quick = QUICK_FILM_MODULES.get(spec.key)
        if quick is not None and (
                self._film_ui_backend == "quick" or spec.key == "film_appearance"):
            return quick[0]
        return spec.python_module

    def _open_quick_film(self, spec: ModuleSpec, module: Any,
                         request: WindowOpenRequest) -> None:
        _, controller_name, qml_file, context_name = QUICK_FILM_MODULES[spec.key]
        controller_type = getattr(module, controller_name)
        auto_start = request.instance_id in self._film_companion_auto_start
        self._film_companion_auto_start.discard(request.instance_id)
        payload = request.payload
        if spec.key == "film_optimization":
            owner_token = getattr(payload, "owner_token", None)
            if owner_token is not None:
                payload = self._film_detached_latest_snapshots.pop(owner_token, payload)
        paired_simulation_source = (
            spec.key == "film_simulation"
            and getattr(payload, "session", None) is not None)
        if spec.key == "film_design":
            controller = controller_type(
                PROJECT_ROOT, parent=self,
                geometry_settings=self._window_settings)
        elif spec.key == "film_optimization":
            if request.payload is None:
                raise ValueError("膜系优化需要来自膜系设计窗口的会话载荷")
            controller = controller_type(payload, parent=self, defer_expensive_load=True)
        elif paired_simulation_source:
            controller = controller_type(
                root=PROJECT_ROOT, parent=self,
                source_controller=payload)
        elif spec.key == "ray_trace":
            controller = controller_type(root=PROJECT_ROOT, parent=self)
            if request.payload is not None:
                if not isinstance(request.payload, dict) or set(request.payload) != {"result_path"}:
                    raise ValueError("ray-trace result window received an invalid queue payload")
                downloads = (PROJECT_ROOT / "artifacts/runtime/scheduler/downloads").resolve()
                path = Path(request.payload["result_path"]).resolve()
                if downloads not in path.parents or not path.is_file():
                    raise ValueError("ray-trace result is outside the scheduler download cache")
                controller.loadResult(str(path))
        elif spec.key == "film_appearance":
            if not isinstance(request.payload, dict):
                raise ValueError("appearance window requires a frozen process-asset context")
            controller = controller_type(
                request.payload, root=PROJECT_ROOT, parent=self)
        else:
            controller = controller_type(root=PROJECT_ROOT, parent=self)
        if spec.key != "film_optimization":
            self._open_initial_document(controller, request)
        if (payload is not None and spec.key == "film_simulation"
                and not paired_simulation_source):
            setter = getattr(controller, "set_design", None) or getattr(controller, "setDesign", None)
            if callable(setter):
                setter(payload)
        context = QQmlContext(self.engine.rootContext())
        title_bridge = WindowTitleBridge(
            self, request.instance_id, spec.key, spec.title, controller,
            request.document_path)
        self._set_instance_context(context, request, title_bridge)
        qml_path = QML_DIR / qml_file
        if not qml_path.is_file():
            title_bridge.deleteLater()
            controller.deleteLater()
            raise FileNotFoundError(f"module UI is missing: {qml_path}")
        initial_properties: dict[str, Any] = {}
        if spec.key in ("film_design", "film_optimization", "film_appearance"):
            # Required QML properties are not initialized by context properties.
            initial_properties[context_name] = controller
        else:
            context.setContextProperty(context_name, controller)
        on_ready = None
        if spec.key == "film_design":
            on_ready = lambda window, identity=request.instance_id: window.simulationRequested.connect(
                lambda design, source_id=identity:
                self._show_film_simulation(source_id, design))
        elif spec.key == "film_simulation" and auto_start:
            on_ready = lambda _window, source=controller: QTimer.singleShot(
                0, source.startCalculation)
        elif spec.key == "film_optimization" and auto_start:
            on_ready = lambda _window, source=controller: QTimer.singleShot(
                0, source.startOptimizationFromShortcut)
        elif spec.key == "ray_trace":
            on_ready = lambda _window, source=controller: source.appearanceRequested.connect(
                self._open_film_appearance_from_raytrace)
        elif (spec.key == "film_simulation"
              and controller.hasOptimizationPreview):
            on_ready = lambda _window, preview_controller=controller: QTimer.singleShot(
                0, preview_controller.startOptimizationPreviewCalculation)
        self._start_async_qml_open(
            spec, request, controller, context, self.engine, qml_path,
            title_bridge=title_bridge, initial_properties=initial_properties,
            maximized=False, on_ready=on_ready)

    @Slot(object)
    def _open_film_appearance_from_raytrace(self, context: Any) -> None:
        if not isinstance(context, dict):
            self.launchError.emit("The selected process asset has no valid appearance context")
            return
        self.open_window("film_appearance", mode="new", payload=context)

    def _ensure_incubation_controller(self, engine: Any) -> None:
        key = id(engine)
        controller = self._incubation_controllers.get(key)
        if controller is None:
            controller = _BudgetedIncubationController(self)
            self._incubation_controllers[key] = controller
        current = engine.incubationController()
        if current != controller:
            self._previous_incubation_controllers.setdefault(key, current)
            engine.setIncubationController(controller)
        if key not in self._incubation_engine_connections:
            engine.destroyed.connect(
                lambda *_args, identity=key:
                self._forget_incubation_engine(identity))
            self._incubation_engine_connections.add(key)
        controller.start()

    def _forget_incubation_engine(self, identity: int) -> None:
        self._incubation_controllers.pop(identity, None)
        self._previous_incubation_controllers.pop(identity, None)
        self._incubation_engine_connections.discard(identity)

    def _start_async_qml_open(
            self, spec: ModuleSpec, request: WindowOpenRequest, bridge: QObject,
            context: QQmlContext, engine: Any, qml_path: Path, *,
            title_bridge: WindowTitleBridge,
            initial_properties: dict[str, Any] | None = None,
            maximized: bool, on_ready: Any | None = None) -> None:
        if not qml_path.is_file():
            raise FileNotFoundError(f"module UI is missing: {qml_path}")
        component = QQmlComponent(engine)
        pending = _PendingQmlOpen(
            spec, request, bridge, context, component, engine, title_bridge,
            dict(initial_properties or {}), maximized, on_ready)
        self._pending_qml[request.instance_id] = pending
        component.statusChanged.connect(
            lambda status, identity=request.instance_id:
            self._qml_component_changed(identity, status))
        self._ensure_incubation_controller(engine)
        component.loadUrl(
            QUrl.fromLocalFile(str(qml_path)),
            QQmlComponent.CompilationMode.Asynchronous)
        if component.status() != QQmlComponent.Status.Loading:
            QTimer.singleShot(
                0, lambda identity=request.instance_id:
                self._qml_component_changed(identity, component.status()))

    def _qml_component_changed(self, instance_id: str, status: Any) -> None:
        pending = self._pending_qml.get(str(instance_id))
        if pending is None or pending.cancelled:
            return
        if status == QQmlComponent.Status.Error:
            details = "\n".join(str(error) for error in pending.component.errors())
            self._fail_pending_qml(pending, details or "QML compilation failed")
            return
        if status != QQmlComponent.Status.Ready or pending.incubator is not None:
            return
        fallback = self._incubation_controllers.get(id(pending.engine))
        if fallback is not None:
            fallback.start()
        incubator = _LauncherIncubator(self, pending.request.instance_id)
        pending.incubator = incubator
        if pending.initial_properties:
            incubator.setInitialProperties(pending.initial_properties)
        pending.component.create(incubator, pending.context)
        if incubator.status() == QQmlIncubator.Status.Ready:
            self._qml_incubator_changed(
                pending.request.instance_id, QQmlIncubator.Status.Ready)
        elif incubator.status() == QQmlIncubator.Status.Error:
            details = "\n".join(str(error) for error in incubator.errors())
            self._fail_pending_qml(pending, details or "QML object creation failed")

    def _qml_incubator_changed(self, instance_id: str, status: Any) -> None:
        pending = self._pending_qml.get(str(instance_id))
        if pending is None or pending.cancelled:
            return
        if status == QQmlIncubator.Status.Error:
            details = "\n".join(str(error) for error in pending.incubator.errors())
            self._fail_pending_qml(pending, details or "QML object creation failed")
            return
        if status != QQmlIncubator.Status.Ready:
            return
        window = pending.incubator.object()
        if window is None:
            self._fail_pending_qml(pending, "QML object creation returned no window")
            return
        if (pending.spec.key in ("film_simulation", "film_optimization")
                and str(instance_id) in self._deferred_open_intents):
            # A newer source/file intent supersedes the constructor's queued
            # calculation. Its own accepted start runs after source confirmation.
            pending.on_ready = None
        if pending.on_ready is not None:
            pending.on_ready(window)
        page_manages_geometry = bool(window.property("pageManagesWindowGeometry"))
        self._geometry_manager.prepare(
            window, pending.spec.key, screen=self._launcher_screen(window),
            preserve_existing=page_manages_geometry)
        window.setIcon(window_icon(pending.spec.key))
        animate = (pending.spec.key not in {"about", "help"}
                   and hasattr(window, "setOpacity")
                   and self._window_animations_enabled())
        if animate:
            window.setOpacity(0.0)
        self._pending_qml.pop(str(instance_id), None)
        self._register_window(
            pending.spec, pending.request, window, pending.bridge,
            pending.context, pending.component, pending.engine,
            pending.title_bridge, complete_open_request=False)
        self._await_window_presentation(str(instance_id), window)
        if pending.maximized or bool(window.property("initialMaximized")):
            window.showMaximized()
        elif not window.isVisible():
            window.show()
        self._activate_window(window)
        if pending.spec.key == "film_optimization":
            cast("FilmOptimizationQuickController", pending.bridge).startLoading()
        if animate:
            self._start_window_transition(str(instance_id), window)

    def _release_pending_qml(self, pending: _PendingQmlOpen) -> None:
        if pending.incubator is not None:
            try:
                pending.incubator.clear()
            except RuntimeError:
                pass
        for obj in (pending.component, pending.context, pending.title_bridge):
            try:
                obj.deleteLater()
            except (AttributeError, RuntimeError):
                pass
        if pending.bridge is not pending.title_bridge:
            detach = getattr(pending.bridge, "detach_observers", None)
            if callable(detach):
                detach()
            try:
                pending.bridge.deleteLater()
            except RuntimeError:
                pass
        if pending.engine is not self.engine:
            pending.engine.deleteLater()

    def _fail_pending_qml(self, pending: _PendingQmlOpen, message: str) -> None:
        identity = pending.request.instance_id
        self._pending_qml.pop(identity, None)
        pending.cancelled = True
        self._release_pending_qml(pending)
        self._finish_open_request(identity)
        if not self._closing:
            self.launchError.emit(
                f"Unable to open {pending.spec.title}: {message}")

    def _window_animations_enabled(self) -> bool:
        app = QGuiApplication.instance()
        if app is not None:
            hints = app.styleHints()
            value = getattr(hints, "animationsEnabled", None)
            if value is not None:
                try:
                    return bool(value() if callable(value) else value)
                except (RuntimeError, TypeError):
                    pass
        if sys.platform == "win32":
            try:
                import ctypes
                enabled = ctypes.c_int()
                if ctypes.windll.user32.SystemParametersInfoW(
                        0x1042, 0, ctypes.byref(enabled), 0):
                    return bool(enabled.value)
            except (AttributeError, OSError, ValueError):
                pass
        return True

    def _start_window_transition(self, instance_id: str, window: Any) -> None:
        animation = QPropertyAnimation(window, b"opacity", self)
        animation.setDuration(170)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(
            lambda identity=instance_id:
            self._finish_window_transition(identity))
        self._window_animations[instance_id] = animation
        animation.start()

    def _finish_window_transition(self, instance_id: str) -> None:
        animation = self._window_animations.pop(instance_id, None)
        if animation is not None:
            animation.deleteLater()

    def _import_module(self, spec: ModuleSpec) -> None:
        try:
            module = importlib.import_module(self._module_path(spec))
            self.moduleImported.emit(spec.key, module, "")
        except Exception as exc:
            self.moduleImported.emit(spec.key, None, str(exc))

    @Slot(str, object, str)
    def _finish_import(self, key: str, module: Any, error: str) -> None:
        if key not in self._loading:
            return
        spec = next(item for item in _ROUTE_MODULES if item.key == key)
        try:
            requests = self._pending_opens.pop(key, [])
            if self._closing:
                for request in requests:
                    self._finish_open_request(request.instance_id)
                return
            if error:
                for request in requests:
                    self._finish_open_request(request.instance_id)
                self.launchError.emit(f"Unable to open {spec.title}: {error}")
                return
            self._modules[key] = module
            for request in requests:
                QTimer.singleShot(
                    0, lambda identity=request.instance_id:
                    self._dispatch_open(identity))
        except Exception as exc:
            for request in self._pending_opens.pop(key, []):
                self._finish_open_request(request.instance_id)
            self.launchError.emit(f"Unable to open {spec.title}: {exc}")
        finally:
            self._set_loading(key, False)

    def _open_one(self, spec: ModuleSpec, module: Any,
                  request: WindowOpenRequest) -> WindowHandle | None:
        if self._closing or request.instance_id not in self._open_requests:
            return None
        try:
            return self._open(spec, module, request)
        except Exception as exc:
            if request.instance_id in self._pending_qml:
                self._cancel_open_request(request.instance_id)
            else:
                self._finish_open_request(request.instance_id)
            self.launchError.emit(f"Unable to open {spec.title}: {exc}")
            return None

    @Slot(int, result=str)
    def launch(self, index: int) -> str:
        if self._closing or not 0 <= index < len(MODULES):
            return ""
        return self.open_window(MODULES[index].key, mode="new")

    def _show_film_simulation(self, source_instance_id: str, design: Any) -> None:
        handle = self._handle_for_instance(source_instance_id)
        if handle is None or handle.key != "film_design":
            return
        source = handle.bridge
        self.open_or_reuse_film_companion("film_simulation", source, start=True)

    def open_or_reuse_film_companion(self, module_key: str, source_controller: Any,
                                     *, start: bool = False) -> str:
        """Reuse the type singleton, keeping exact source-session ownership."""
        if module_key not in ("film_simulation", "film_optimization"):
            return ""
        session = getattr(source_controller, "session", None)
        if session is None or getattr(source_controller, "_source_closed", False):
            return ""

        def matches(controller: Any) -> bool:
            if module_key == "film_optimization":
                return (getattr(controller, "_source_controller", None) is source_controller
                        and getattr(controller, "session", None) is session)
            ref = getattr(controller, "_source_controller_ref", None)
            return (callable(ref) and ref() is source_controller
                    and getattr(controller, "_source_session", None) is session)

        existing = next((handle for handle in self._windows.values()
                         if handle.key == module_key and matches(handle.bridge)), None)
        if existing is not None:
            self._activate_window(existing.window)
            if start:
                if module_key == "film_simulation":
                    requester = (getattr(existing.bridge, "requestSourceRefresh", None)
                                 or getattr(existing.bridge, "requestCalculation", None))
                    if callable(requester):
                        requester()
                    else:
                        existing.bridge.refreshFromSource()
                        existing.bridge.startCalculation()
                else:
                    existing.bridge.startOptimizationFromShortcut()
            return existing.instance_id

        pending = next((request.instance_id for spec, request in reversed(
            list(self._open_requests.values()))
            if spec.key == module_key and request.payload is source_controller), None)
        if pending is not None:
            if pending in getattr(self, "_deferred_open_intents", {}):
                self.open_window(module_key, mode="new", payload=source_controller)
            if start:
                pending_qml = self._pending_qml.get(pending)
                if pending_qml is not None and matches(pending_qml.bridge):
                    pending_qml.on_ready = None
                    if module_key == "film_simulation":
                        requester = getattr(pending_qml.bridge, "requestSourceRefresh", None)
                        if callable(requester):
                            requester()
                        else:
                            pending_qml.bridge.refreshFromSource()
                            pending_qml.bridge.startCalculation()
                    else:
                        pending_qml.bridge.startOptimizationFromShortcut()
                else:
                    self._film_companion_auto_start.add(pending)
            return pending

        instance_id = self.open_window(module_key, mode="new", payload=source_controller)
        if instance_id and start:
            handle = self._windows.get(instance_id)
            if handle is not None:
                LauncherBridge._start_reused_film_companion(handle)
            elif instance_id in getattr(self, "_deferred_open_intents", {}):
                self._deferred_companion_starts.add(instance_id)
            else:
                self._film_companion_auto_start.add(instance_id)
        return str(instance_id or "")

    def open_or_reuse_detached_film_companion(
            self, module_key: str, source_snapshot: Any, *, owner_token: object,
            start: bool = False) -> str:
        """Reuse a snapshot optimizer owned by one detached simulation window."""
        if (module_key != "film_optimization" or owner_token is None
                or getattr(source_snapshot, "owner_token", None) is not owner_token):
            return ""
        def matches(controller: Any) -> bool:
            return (bool(getattr(controller, "_detached_source", False))
                    and getattr(controller, "_snapshot_owner_token", None) is owner_token)

        def refresh_and_start(controller: Any) -> None:
            refresh = getattr(controller, "refreshDetachedSource", None)
            if callable(refresh):
                refresh(source_snapshot)
            if start:
                starter = getattr(controller, "startOptimizationFromShortcut", None)
                if callable(starter):
                    starter()

        existing = next((handle for handle in self._windows.values()
                         if handle.key == module_key and matches(handle.bridge)), None)
        if existing is not None:
            self._activate_window(existing.window)
            refresh_and_start(existing.bridge)
            return existing.instance_id

        pending = next((request.instance_id for spec, request in reversed(
            list(self._open_requests.values()))
            if spec.key == module_key
            and getattr(request.payload, "owner_token", None) is owner_token), None)
        if pending is not None:
            pending_qml = self._pending_qml.get(pending)
            if pending_qml is not None and matches(pending_qml.bridge):
                self._film_detached_latest_snapshots.pop(owner_token, None)
                self._deferred_open_intents.pop(pending, None)
                self._deferred_companion_starts.discard(pending)
                pending_qml.on_ready = None
                refresh_and_start(pending_qml.bridge)
            else:
                self._film_detached_latest_snapshots[owner_token] = source_snapshot
                if start:
                    self._film_companion_auto_start.add(pending)
            return pending

        instance_id = self.open_window(
            module_key, mode="new", payload=source_snapshot)
        if instance_id:
            handle = self._windows.get(instance_id)
            if handle is not None:
                if start:
                    LauncherBridge._start_reused_film_companion(handle)
            elif instance_id in self._deferred_open_intents:
                if start:
                    self._deferred_companion_starts.add(instance_id)
            else:
                self._film_detached_latest_snapshots[owner_token] = source_snapshot
                if start:
                    self._film_companion_auto_start.add(instance_id)
        return str(instance_id or "")

    @Slot(str, object, result=str)
    def openQueuedFilmOptimizationCandidate(self, job_id: str, result: Any) -> str:
        """Open an explicitly selected, queue-verified film candidate in isolation."""
        try:
            from coat_sim.core.film_optimization_job import deserialize_request
            from coat_sim.core.film_optimization_preview import (
                create_optimization_candidate_handoff,
            )
            from coat_sim.ui.film_optimization_quick_controller import (
                _film_result_from_queue,
            )

            if not isinstance(result, dict) or not result.get("request"):
                raise ValueError("queue result has no frozen film request")
            request = deserialize_request(result["request"])
            candidate = _film_result_from_queue(result)
            payload = create_optimization_candidate_handoff(request, candidate)
            instance_id = self.open_window(
                "film_simulation", mode="new", payload=payload)
            if not instance_id:
                raise RuntimeError("independent film preview could not be opened")
            return str(instance_id)
        except Exception as exc:
            self.launchError.emit(
                f"Unable to open queue film candidate {job_id}: {exc}")
            return ""

    @Slot(str, str, result=str)
    def openQueuedRayTraceResult(self, job_id: str, result_path: str) -> str:
        """Open a verified queue artifact in the ray-trace result window."""
        try:
            downloads = (PROJECT_ROOT / "artifacts/runtime/scheduler/downloads").resolve()
            path = Path(result_path).resolve()
            if downloads not in path.parents or not path.is_file():
                raise ValueError("ray-trace result is outside the scheduler download cache")
            instance_id = self.open_window(
                "ray_trace", mode="new", payload={"result_path": str(path)})
            if not instance_id:
                raise RuntimeError("ray-trace result window could not be opened")
            return str(instance_id)
        except Exception as exc:
            self.launchError.emit(
                f"Unable to open queue ray-trace result {job_id}: {exc}")
            return ""

    def attach_assistant_controller(self, assistant_controller: Any) -> None:
        """Attach the desktop assistant and route its typed queue results here."""
        desktop = getattr(assistant_controller, "desktop", None)
        signal = getattr(desktop, "filmOptimizationResultReady", None)
        if signal is None:
            raise TypeError("desktop assistant queue has no film result signal")
        previous = self._assistant_queue_result_source
        if previous is desktop:
            self.assistant_controller = assistant_controller
            return
        if previous is not None:
            try:
                previous.filmOptimizationResultReady.disconnect(
                    self.openQueuedFilmOptimizationCandidate)
            except (RuntimeError, TypeError):
                pass
            try:
                previous.rayTraceResultReady.disconnect(
                    self.openQueuedRayTraceResult)
            except (AttributeError, RuntimeError, TypeError):
                pass
        signal.connect(self.openQueuedFilmOptimizationCandidate)
        ray_trace_signal = getattr(desktop, "rayTraceResultReady", None)
        if ray_trace_signal is not None:
            ray_trace_signal.connect(self.openQueuedRayTraceResult)
        self._assistant_queue_result_source = desktop
        self.assistant_controller = assistant_controller

    def _reload_assistant_settings(self) -> None:
        for handle in self._handles_for("settings"):
            reload_settings = getattr(handle.bridge, "reloadAssistantSettings", None)
            if callable(reload_settings):
                reload_settings()

    def _show_compute_queue(self) -> None:
        if self.assistant_controller is not None:
            self.assistant_controller.desktop.showQueue()
            return
        if self._queue_view is None:
            from coat_sim.ui.compute_queue import ComputeQueueView
            self._queue_view = ComputeQueueView(self.engine, PROJECT_ROOT)
            self._queue_view.filmOptimizationResultReady.connect(
                self.openQueuedFilmOptimizationCandidate)
            self._queue_view.rayTraceResultReady.connect(
                self.openQueuedRayTraceResult)
        self._queue_view.show()

    @Slot()
    def closeLauncher(self) -> None:
        """Close only this presentation process; project owners are independent."""
        self.shutdown()

    def _dirty_project_sessions(self) -> list[str]:
        if self.project_runtime is None:
            return []
        dirty = []
        host = self.project_runtime.host
        for session in host.sessions():
            workspace = host.resource_for(session.binding, "film_workspace")
            editor = getattr(workspace, "_editor", None)
            session_dirty = editor is not None and editor.snapshot().dirty
            resources = host.resource_for(session.binding, "resource_workspace")
            if resources is not None:
                session_dirty = bool(resources.dirty_summary()) or session_dirty
            if session_dirty:
                dirty.append(session.name)
        return dirty

    def _uncommitted_project_view_draft(self, project_id: str | None = None) -> bool:
        if self.project_runtime is None:
            return False
        for session in self.project_runtime.host.sessions():
            if project_id is not None and session.binding.project_id != project_id:
                continue
            resources = self.project_runtime.host.resource_for(session.binding, "resource_workspace")
            if resources is not None:
                try:
                    resources.close_preflight()
                except Exception:
                    return True
        for route, view in tuple(self.project_runtime.desktop._views.items()):
            if project_id is not None and route.binding.project_id != project_id:
                continue
            if route.module_key != "film_design":
                continue
            commit = getattr(view[0], "commitExpressionDraft", None)
            if callable(commit) and not commit(True):
                return True
        return False

    def _close_project_sessions(self) -> bool:
        if self.project_runtime is None:
            return True
        host = self.project_runtime.host
        for session in host.sessions():
            resources = host.resource_for(session.binding, "resource_workspace")
            windows = {route: host.window_for(route)
                       for route in host.routes(session.binding.project_id)}

            def close_windows(_binding, routes, windows=windows) -> bool:
                for route in routes:
                    window = windows.get(route)
                    if window is None:
                        continue
                    window.setProperty("projectCloseAuthorized", True)
                    window.setProperty("allowClose" if route.module_key == "film_design"
                                       else "closeAuthorized", True)
                    if not window.close():
                        return False
                return True

            try:
                with self.project_runtime.desktop.authorized_project_close(session.binding):
                    if not host.close_project(session.binding, prepare_close=close_windows):
                        return False
                if resources is not None:
                    resources.dispose()
            except Exception as exc:
                self.launchError.emit(f"项目关闭失败：{exc}")
                return False
        return True

    @Slot()
    def shutdown(self) -> None:
        if self._runtime_shutdown_callback is not None and not self._runtime_shutdown_authorized:
            self._runtime_shutdown_callback()
            return
        if self._closing or self._shutdown_confirmation_pending:
            return
        global_publishers = [handle for handle in self._windows.values()
            if handle.key == "global_resources"
            and (not cast("GlobalResourcesBridge", handle.bridge).assetCancellable
                 or cast("GlobalResourcesBridge", handle.bridge).materialModalPending)]
        if global_publishers:
            self.launchError.emit("全局资源正在提交，请等待结果后再退出启动器。")
            self._activate_window(global_publishers[0].window)
            return
        if self.project_runtime is not None and self.project_runtime._busy:
            self.launchError.emit("项目请求仍在处理，请稍后再退出。")
            if self._launcher_window is not None:
                self._activate_window(self._launcher_window)
            return
        dirty_documents = [handle for handle in self._windows.values()
                           if ((handle.key == "film_design" and self._film_is_dirty(handle))
                               or (handle.key == "ray_trace" and handle.title_bridge is not None
                                   and handle.title_bridge.documentDirty)
                               or (handle.key == "global_resources" and bool(getattr(handle.bridge, "dirty", False))))
                           and not getattr(handle.window, "_force_close", False)
                           and not bool(handle.window.property("allowClose"))]
        project_dirty = (not self._project_shutdown_approved
                         and (self._uncommitted_project_view_draft()
                              or self._dirty_project_sessions()))
        if dirty_documents or project_dirty:
            self._shutdown_confirmation_pending = True
            if self._launcher_window is not None:
                self._activate_window(self._launcher_window)
            self.shutdownConfirmationRequested.emit()
            return
        if not self._close_project_sessions():
            self._project_shutdown_approved = False
            return
        self._closing = True
        for instance_id in list(self._open_requests):
            self._cancel_open_request(instance_id)
        self._pending_opens.clear()
        app = QGuiApplication.instance()
        if isinstance(app, QGuiApplication):
            app.setQuitOnLastWindowClosed(False)
        if self.project_application is not None:
            self.project_application.stop()
        if self.control_registry is not None:
            self.control_registry.close()
        if self.control_server is not None:
            self.control_server.close()
        if self._queue_view is not None:
            self._queue_view.shutdown()
        if self.assistant_controller is not None:
            self.assistant_controller.shutdown()
        if self.project_runtime is not None:
            self.project_runtime.stop()
        if self.project_launcher is not None:
            self.project_launcher.stop()
        # Closing a presentation client does not revoke admitted service-owned jobs.
        for handle in list(self._windows.values()):
            handle.window.close()
        self._shutdown_timer.start()
        self._finish_shutdown()

    @Slot()
    def confirmShutdown(self) -> None:
        if self._closing or not self._shutdown_confirmation_pending:
            return
        if self.project_lifecycle is not None and self.project_lifecycle.active:
            self.project_lifecycle.decide(True)
            return
        self._shutdown_confirmation_pending = False
        self._project_shutdown_approved = True
        for handle in self._windows.values():
            if handle.key == "film_design" and self._film_is_dirty(handle):
                if self._film_ui_backend == "quick":
                    handle.window.setProperty("allowClose", True)
                else:
                    handle.window._force_close = True
            elif (handle.key == "ray_trace" and handle.title_bridge is not None
                  and handle.title_bridge.documentDirty):
                handle.window.setProperty("allowClose", True)
            elif handle.key == "global_resources" and bool(getattr(handle.bridge, "dirty", False)):
                handle.window.setProperty("allowClose", True)
        self.shutdown()

    @staticmethod
    def _film_is_dirty(handle: WindowHandle) -> bool:
        legacy_check = getattr(handle.bridge, "_has_dirty", None)
        if callable(legacy_check):
            return bool(legacy_check())
        return bool(getattr(handle.bridge, "dirty", False))

    @Slot()
    def cancelShutdown(self) -> None:
        if self.project_lifecycle is not None and self.project_lifecycle.active:
            self.project_lifecycle.decide(False)
            return
        if not self._closing:
            self._shutdown_confirmation_pending = False
            self._project_shutdown_approved = False

    def _finish_shutdown(self) -> None:
        """Presentation clients can exit while independent calculations continue."""
        self._shutdown_timer.stop()
        for handle in list(self._windows.values()):
            handle.window.close()
        app = QGuiApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)


def _fatal(message: str) -> None:
    try:
        from coat_sim.ui.ready_channel import report_startup_event
        report_startup_event("error", error=message)
    except Exception:
        pass
    print(message, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "光学镀膜 · 启动器", 0x10)
        except (AttributeError, OSError):
            pass


def _extract_film_ui_backend(args: list[str]) -> tuple[str, list[str]]:
    remaining = list(args)
    backend = "quick"
    positions = [i for i, arg in enumerate(remaining)
                 if arg == "--film-ui" or arg.startswith("--film-ui=")]
    if len(positions) > 1:
        raise ValueError("use --film-ui quick or --film-ui legacy once")
    if positions:
        position = positions[0]
        option = remaining[position]
        if option == "--film-ui":
            if position + 1 >= len(remaining):
                raise ValueError("--film-ui must be followed by quick or legacy")
            backend = remaining[position + 1].strip().lower()
            del remaining[position:position + 2]
        else:
            backend = option.partition("=")[2].strip().lower()
            del remaining[position]
        if backend not in ("quick", "legacy"):
            raise ValueError("--film-ui must be quick or legacy")
    return backend, remaining


def main(argv: list[str] | None = None) -> int:
    # tools/launch_desktop.pyw redirects this line into its persistent startup
    # log; a source-console launch also shows the same human-readable version.
    if sys.stderr is not None:
        print(format_startup_line(BUILD_IDENTITY), file=sys.stderr, flush=True)
    args = list(sys.argv if argv is None else argv)
    try:
        film_ui_backend, args = _extract_film_ui_backend(args)
    except ValueError as exc:
        _fatal(str(exc))
        return 2
    software_mode = "--software" in args
    graphics_info = "--graphics-info" in args
    direct_key: str | None = None
    if "--module" in args:
        position = args.index("--module")
        if position + 1 >= len(args):
            _fatal("--module 后须填写 main、process、machine、mask、source、substrate、settings 或 about")
            return 2
        direct_key = args[position + 1].strip().lower()
        del args[position:position + 2]
        if direct_key not in {item.key for item in MODULES}:
            _fatal(f"未知模块：{direct_key}")
            return 2
    if software_mode:
        os.environ["QT_QUICK_BACKEND"] = "software"
    if graphics_info:
        os.environ["QSG_INFO"] = "1"
    qt_args = [arg for arg in args if arg not in ("--software", "--graphics-info")]

    if direct_key is None and not BACKGROUND.is_file():
        _fatal(f"缺少启动器背景图：{BACKGROUND}")
        return 2
    configure_qt_dpi_scale()
    # The desktop tray and its native context menu need QApplication. The UI
    # remains Qt Quick; no separate platform-specific presentation layer.
    app = cast(QGuiApplication, QGuiApplication.instance() or QApplication(qt_args))
    app.setApplicationVersion(BUILD_IDENTITY.version)
    app.setWindowIcon(window_icon(direct_key or "launcher"))
    apply_font_settings(app=app)
    theme_service = configure_theme_from_settings()
    engine = QQmlApplicationEngine()
    theme_service.attach_engine(engine)
    try:
        bridge = LauncherBridge(engine, software_mode=software_mode,
                                with_launcher=direct_key is None,
                                film_ui_backend=film_ui_backend)
    except RuntimeError as exc:
        _fatal(str(exc))
        return 2
    app.setQuitOnLastWindowClosed(False)
    app.lastWindowClosed.connect(bridge.closeLauncher)
    app.aboutToQuit.connect(bridge.shutdown)
    if direct_key is None:
        quit_guard = _ProjectQuitGuard(bridge)
        bridge._quit_guard = quit_guard
        app.installEventFilter(quit_guard)
    try:
        from coat_sim.ui.control import UiControlServer, WindowRegistry, OPERATIONS
        from coat_sim.assistant_process import LIFECYCLE_OPERATIONS
        from coat_sim.ui.project_application_client import ProjectApplicationClient
        from coat_sim.ui.project_application_lifecycle import ProjectApplicationLifecycle
        bridge.control_registry = WindowRegistry(bridge, direct_key=direct_key)
        role = "launcher" if direct_key is None else "project_host"
        bridge.project_application = ProjectApplicationClient(bridge, bridge.control_registry, role=role)
        bridge.project_lifecycle = ProjectApplicationLifecycle(bridge, bridge.project_application.generation)
        control_server = UiControlServer(bridge.project_application.dispatch,
            operations=OPERATIONS | LIFECYCLE_OPERATIONS,
            role=role, window_type=direct_key,
            parent_pid=int(os.environ["COAT_UI_PARENT_PID"]) if os.environ.get("COAT_UI_PARENT_PID") else None)
        bridge.control_server = control_server
        def start_control_service() -> None:
            try:
                endpoint = control_server.start()
                if bridge.project_application is not None:
                    bridge.project_application.start(endpoint)
                from coat_sim.ui.ready_channel import report_startup_event
                report_startup_event("ready", endpoint=endpoint)
            except Exception as exc:
                bridge.shutdown()
                _fatal(f"界面控制服务无法启动：{exc}")
                app.exit(2)
        QTimer.singleShot(0, start_control_service)
    except Exception as exc:
        bridge.shutdown()
        _fatal(f"界面控制服务无法启动：{exc}")
        return 2
    if direct_key is None:
        os.environ["COAT_UI_PID"] = str(os.getpid())
    if direct_key is None:
        engine.rootContext().setContextProperty("launcher", bridge)
        engine.rootContext().setContextProperty("launcherProjects", bridge.project_launcher)
        engine.load(QUrl.fromLocalFile(str(QML_DIR / "LauncherWindow.qml")))
        if not engine.rootObjects():
            bridge.shutdown()
            _fatal("启动器界面未能加载。请检查 PySide6 安装与图形驱动。")
            return 2
        launcher_window = cast(QWindow, engine.rootObjects()[0])
        launcher_window.setIcon(window_icon("launcher"))
        bridge._launcher_window = launcher_window
        launcher_window.show()

    else:
        spec = next(item for item in MODULES if item.key == direct_key)
        try:
            module = importlib.import_module(bridge._module_path(spec))
            bridge._modules[spec.key] = module
            request = WindowOpenRequest(uuid.uuid4().hex, spec.key)
            bridge._open(spec, module, request)
        except Exception as exc:
            bridge.shutdown()
            _fatal(f"无法打开{spec.title}：{exc}")
            return 2
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
