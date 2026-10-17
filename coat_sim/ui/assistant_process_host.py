"""Real Qt presentation host for the independent shared assistant process."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication, QMessageBox

from coat_sim.assistant_process import ASSISTANT_OPERATIONS, ASSISTANT_PROTOCOL, ApplicationClientRegistry
from coat_sim.assistant_diagnostics import source_id, trace
from coat_sim.ui.control import PROJECT_ROOT
from coat_sim.version import BUILD_IDENTITY
from coat_sim.core.process_identity import _process_identity
from coat_sim.ui.assistant_desktop import DesktopAssistantController
from coat_sim.ui.control import UiControlError, UiControlServer
from coat_sim.ui.icons import window_icon
from coat_sim.ui.settings import apply_font_settings, configure_qt_dpi_scale
from coat_sim.ui.theme_service import configure_theme_from_settings


class AssistantProcessHost(QObject):
    exitProgress = Signal(object)
    exitFinished = Signal(object)
    externalWindowsReady = Signal(object)
    externalWindowActionReady = Signal(str)

    def __init__(self, engine: QQmlApplicationEngine, runtime_root: Path):
        super().__init__(engine)
        self.generation = uuid4().hex
        state, self.process_start = _process_identity(os.getpid())
        if state != "alive" or self.process_start is None:
            raise UiControlError("process_identity_unknown", "Cannot verify assistant process creation identity")
        self.registry = ApplicationClientRegistry()
        self.controller = DesktopAssistantController(engine)
        self.controller.desktop.set_application_exit_callback(self.request_exit)
        self.controller.desktop.set_application_exit_cancel_callback(self.cancel_exit_wait)
        self.controller.desktop.set_external_window_callbacks(self.refresh_external_windows, self.external_window_action)
        self._window_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="assistant-managed-windows")
        self._window_fetching = False
        self._window_last_fetch = 0.0
        self.externalWindowsReady.connect(self._external_windows_ready)
        self.externalWindowActionReady.connect(self._external_window_action_ready)
        self._exit: dict[str, Any] = {"state": "idle"}
        self._closed = False
        self._worker: threading.Thread | None = None
        self._chat_enabled: bool | None = None
        self._cancel_wait = threading.Event()
        self.exitProgress.connect(self._exit_progress)
        self.exitFinished.connect(self._exit_finished)
        self.server = UiControlServer(self.dispatch, root=runtime_root, role="assistant",
                                       operations=ASSISTANT_OPERATIONS, endpoint_prefix="assistant")
        self._debug = None
        try:
            from coat_sim.ui.assistant_debug import AssistantDebugServer
            self._debug = AssistantDebugServer(self.controller.bridge)
            self._debug.start()
        except Exception as exc:
            print(f"Assistant debug endpoint unavailable: {type(exc).__name__}", flush=True)

    def start(self) -> dict[str, Any]:
        return self.server.start()

    def dispatch(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation == "status":
            return {"application": "coat_sim", "pid": os.getpid(), "process_start": self.process_start,
                    "generation": self.generation, "nonce": arguments.get("nonce"), "state": "ready",
                    "assistant_protocol": ASSISTANT_PROTOCOL, "version": BUILD_IDENTITY.version,
                    "source_id": source_id(PROJECT_ROOT),
                    "capabilities": ["activate_project", "project_window"],
                    "enabled": bool(cast(Any, self.controller.bridge).enabled),
                    "bubble_created": self.controller._bubble is not None,
                    "chat_created": self.controller._chat is not None}
        if operation == "show_assistant":
            self.controller.desktop.showAssistant()
            return {"accepted": True, "enabled": bool(cast(Any, self.controller.bridge).enabled)}
        if operation == "show_queue":
            self.controller.desktop.showQueue()
            return {"accepted": True}
        if operation == "reload_configuration":
            self.controller.reload_configuration()
            return {"accepted": True}
        if operation == "register_client":
            return self.registry.register_client(arguments.get("endpoint", {}),
                generation=arguments.get("generation", ""), process_start=arguments.get("process_start", ""))
        if operation == "unregister_client":
            return {"removed": self.registry.unregister_client(arguments.get("client_id", ""), arguments.get("generation", ""))}
        if operation == "list_clients":
            return {"clients": self.registry.list_clients()}
        if operation == "activate_project":
            if self._worker is not None:
                raise UiControlError("exit_in_progress", "应用正在退出，暂不能激活工程。")
            return self.registry.activate_project(arguments)
        if operation == "project_window":
            if self._worker is not None:
                raise UiControlError("exit_in_progress", "应用正在退出，暂不能操作工程窗口。")
            if set(arguments) != {"project_target", "window_operation", "arguments"}:
                raise UiControlError("invalid_arguments", "工程窗口路由参数无效。")
            return self.registry.project_window(arguments["project_target"],
                arguments["window_operation"], arguments["arguments"])
        if operation == "project_manage":
            if self._worker is not None:
                raise UiControlError("exit_in_progress", "Application exit is in progress")
            from coat_sim.core.project_store import ProjectError
            try:
                return self.registry.manage_project(arguments.get("project_operation", ""),
                    arguments.get("arguments", {}), session_generation=arguments.get("session_generation", ""))
            except ProjectError as exc:
                raise UiControlError(exc.code, str(exc)) from exc
        if operation == "request_exit_all":
            if self._exit["state"] in {"idle", "declined", "failed", "shutdown_pending", "wait_abandoned"}:
                self._exit = {"state": "own_confirmation", "exit_id": uuid4().hex}
                QTimer.singleShot(0, self.request_exit)
            return dict(self._exit)
        if operation == "exit_status":
            return dict(self._exit)
        raise UiControlError("invalid_operation", "Unknown assistant process operation")

    def refresh_external_windows(self) -> None:
        if self._closed or self._window_fetching or time.monotonic() - self._window_last_fetch < 1.0:
            return
        self._window_fetching = True
        self._window_last_fetch = time.monotonic()
        future = self._window_pool.submit(self.registry.list_windows)

        def done(item):
            try:
                rows = item.result()
            except Exception:
                rows = []
            if not self._closed:
                self.externalWindowsReady.emit(rows)
        future.add_done_callback(done)

    @Slot(object)
    def _external_windows_ready(self, rows) -> None:
        self._window_fetching = False
        self.controller.desktop.set_external_windows(rows)

    def external_window_action(self, identity: str, operation: str) -> bool:
        if self._closed:
            return False
        if operation == "open_window":
            try:
                # The menu still owns the actual user input/foreground here.
                # Deferring this grant until the worker's IPC would lose it.
                granted = self.registry.prepare_window_activation(identity)
                trace("window_foreground_permission", operation=operation,
                      reason="granted" if granted else "denied")
            except UiControlError as exc:
                self.controller.desktop.notify("窗口操作未完成：" + str(exc), sound=False)
                return False
        future = self._window_pool.submit(self.registry.window_action, identity, operation)

        def done(item):
            try:
                item.result()
                error = ""
            except Exception as exc:
                error = str(exc)
            if not self._closed:
                self.externalWindowActionReady.emit(error)
        future.add_done_callback(done)
        return True  # Admission only; native dirty-close decisions stay host-owned.

    @Slot(str)
    def _external_window_action_ready(self, error: str) -> None:
        if error:
            self.controller.desktop.notify("窗口操作未完成：" + error, sound=False)
        self._window_last_fetch = 0.0
        self.refresh_external_windows()

    @Slot()
    def request_exit(self) -> None:
        if self._closed or self._worker is not None:
            return
        if "exit_id" not in self._exit or self._exit["state"] != "own_confirmation":
            self._exit = {"state": "own_confirmation", "exit_id": uuid4().hex}
        bridge, desktop = cast(Any, self.controller.bridge), self.controller.desktop
        active_jobs = any(row.get("state") in {"queued", "waiting_confirmation", "running", "held", "pending"}
                          for row in desktop._jobs if isinstance(row, dict))
        if bridge.busy or active_jobs:
            text = "退出将关闭本应用的窗口和助手。"
            if bridge.busy:
                text += "\n助手仍在处理对话，退出会结束当前助手任务。"
            if active_jobs:
                text += "\n计算服务持有的任务会继续运行，远程计算服务不会被停止。"
            text += "\n项目中的未保存内容会分别确认。是否继续退出？"
            decision = QMessageBox.question(None, "退出应用", text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if decision != QMessageBox.StandardButton.Yes:
                self._exit.update(state="declined", code="assistant_close_declined")
                return
        try:
            foreground_client_id = self.registry.prepare_exit_activation()
        except UiControlError as exc:
            self._exit.update(state="failed", code=exc.code)
            desktop.notify("退出尚未开始：" + str(exc), sound=False)
            return
        cast(Any, self.controller.bridge).set_application_exit_frozen(True)
        if self.controller._chat is not None:
            content = self.controller._chat.contentItem()
            self._chat_enabled = content.isEnabled()
            content.setEnabled(False)
        self._cancel_wait.clear()
        self._exit.update(state="preparing")
        exit_id = self._exit["exit_id"]

        def execute() -> None:
            result = self.registry.request_exit_all(exit_id, on_progress=self.exitProgress.emit, cancel_wait=self._cancel_wait,
                                                    foreground_client_id=foreground_client_id)
            self.exitFinished.emit(result)

        self._worker = threading.Thread(target=execute, name="assistant-graceful-exit", daemon=True)
        self._worker.start()

    @Slot(object)
    def _exit_progress(self, value: dict[str, Any]) -> None:
        self._exit.update(value)
        if value.get("state") == "waiting_for_exit":
            self.controller.desktop.set_application_exit_waiting(True)
        if value.get("code") == "client_exit_delayed":
            self.controller.desktop.notify("部分窗口进程仍在退出中。右键菜单可停止等待并保留助手；已发送的退出请求无法撤销。", sound=False)

    @Slot()
    def cancel_exit_wait(self) -> None:
        if self._exit.get("state") != "waiting_for_exit" or self._worker is None:
            return
        decision = QMessageBox.question(None, "停止等待退出", 
            "这只会停止等待并保留助手。已经收到退出请求的窗口可能继续关闭，无法恢复。是否停止等待？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if decision == QMessageBox.StandardButton.Yes:
            self._cancel_wait.set()

    @Slot(object)
    def _exit_finished(self, value: dict[str, Any]) -> None:
        self._worker = None
        self.controller.desktop.set_application_exit_waiting(False)
        self._exit.update(value)
        if value.get("state") == "completed":
            self.close()
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(0, app.quit)
        else:
            cast(Any, self.controller.bridge).set_application_exit_frozen(False)
            if self.controller._chat is not None and self._chat_enabled is not None:
                self.controller._chat.contentItem().setEnabled(self._chat_enabled)
            self._chat_enabled = None
        if value.get("state") not in {"completed", "declined", "wait_abandoned"}:
            self.controller.desktop.notify("退出尚未完成：" + str(value.get("message") or value.get("code")),
                                           sound=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._window_pool.shutdown(wait=False, cancel_futures=True)
        self.server.close()
        if self._debug is not None:
            self._debug.close()
        self.controller.shutdown()


def run_assistant(runtime_root: Path) -> int:
    configure_qt_dpi_scale()
    existing = QApplication.instance()
    if existing is not None and not isinstance(existing, QApplication):
        raise RuntimeError("Assistant requires its own QApplication")
    app = existing if isinstance(existing, QApplication) else QApplication([])
    app.setApplicationName("CoatSim Assistant")
    app.setOrganizationName("CoatSim")
    app.setWindowIcon(window_icon("launcher"))
    app.setQuitOnLastWindowClosed(False)
    apply_font_settings(app=app)
    theme = configure_theme_from_settings()
    engine = QQmlApplicationEngine()
    theme.attach_engine(engine)
    host = AssistantProcessHost(engine, runtime_root)
    app.aboutToQuit.connect(lambda: trace("assistant_qt_quit", state=str(host._exit.get("state", "unknown"))))
    app.aboutToQuit.connect(host.close)
    host.start()
    trace("assistant_service_ready", protocol=ASSISTANT_PROTOCOL, generation=host.generation)
    return app.exec()
