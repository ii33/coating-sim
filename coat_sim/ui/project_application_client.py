"""Nonblocking registration and explicitly scoped project window routing."""
from __future__ import annotations

from concurrent.futures import Future
import os
from threading import Event, Thread
from typing import Any, cast
from uuid import uuid4
from weakref import WeakKeyDictionary

from PySide6.QtCore import QObject, QTimer

from coat_sim.assistant_process import AssistantProcessClient
from coat_sim.core.process_identity import _process_identity
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_activation import canonical_path
from coat_sim.project_ui import PROJECT_WINDOW_MODULES, project_window_capabilities
from coat_sim.project_session import ProjectSessionError
from .control import UiControlError
from .project_owner_access import project_owner
from .presentation_scope import legacy_window_scope_error, presentation_scope
from .project_application_admission import ProjectApplicationAdmission


class ProjectApplicationClient(QObject):
    """Register this presentation process without owning the assistant lifetime."""

    def __init__(self, bridge: Any, registry: Any, *, role: str = "launcher"):
        super().__init__(bridge)
        self.bridge, self.registry, self.role = bridge, registry, role
        self.generation = uuid4().hex
        state, process_start = _process_identity(os.getpid())
        if state != "alive" or not process_start:
            raise RuntimeError("Cannot establish presentation process identity")
        self.process_start: str = process_start
        self.client = AssistantProcessClient()
        launcher = project_owner(self.bridge)
        self.admission = ProjectApplicationAdmission(getattr(launcher, "_set_error", None))
        install_guard = getattr(launcher, "set_admission_guard", None)
        if callable(install_guard):
            install_guard(self.admission.decision)
        self._stop_event = Event()
        self._pending: Future | None = None
        self._endpoint: dict | None = None
        self._stopped = False
        self._last_error = ""
        self._launcher_ids: WeakKeyDictionary[Any, str] = WeakKeyDictionary()
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll)
        self._retry = QTimer(self)
        self._retry.setInterval(10000)
        self._retry.timeout.connect(self._register)

    def start(self, endpoint: dict) -> None:
        self._endpoint = dict(endpoint)
        connect_projects = getattr(project_owner(self.bridge), "connect_shared_projects", None)
        if callable(connect_projects):
            connect_projects(self.client, self.generation)
        self._timer.start()
        self._retry.start()
        self._register()

    def _register(self) -> None:
        if self._stopped or self._pending is not None or self._endpoint is None:
            return
        self.admission.registration_started()
        endpoint = dict(self._endpoint)
        future: Future = Future()
        self._pending = future
        def register() -> None:
            try:
                if self._stop_event.is_set():
                    future.cancel()
                    return
                self.client.ensure_running()
                if self._stop_event.is_set():
                    future.cancel()
                    return
                result = self.client.register_client(endpoint, generation=self.generation,
                                                     process_start=self.process_start)
                future.set_result(result)
            except Exception as exc:
                future.set_exception(exc)
        Thread(target=register, name="assistant-registration", daemon=True).start()

    def _poll(self) -> None:
        if self._pending is None or not self._pending.done():
            return
        future, self._pending = self._pending, None
        try:
            future.result()
            self.admission.registration_succeeded()
            self._last_error = ""
        except Exception as exc:
            self.admission.registration_failed(getattr(exc, "code", "registration_failed"), str(exc))
            if isinstance(exc, UiControlError) and exc.code == "exit_in_progress":
                return
            message = f"共享助手连接失败，将自动重试：{exc}"
            if message != self._last_error:
                self._last_error = message
                self.bridge.launchError.emit(message)

    def stop(self) -> None:
        self._stopped = True
        self.admission.stopped()
        self._timer.stop()
        self._retry.stop()
        self._stop_event.set()
        # The registry verifies OS identity and removes this client after exit.
        # Never stop the independent assistant or another application client.

    def _sessions(self) -> tuple:
        launcher = project_owner(self.bridge)
        return launcher.host.sessions() if launcher is not None else ()

    def status(self, nonce: Any) -> dict:
        launcher = project_owner(self.bridge)
        return {"application": "coat_sim", "nonce": nonce, "pid": os.getpid(),
                "process_start": self.process_start, "generation": self.generation,
                "role": self.role,
                "scope": presentation_scope(self.bridge),
                "project_sessions": [
                    {"project_id": session.binding.project_id, "root": str(session.binding.root),
                     "project_file": str(cast(ProjectStore, launcher.host.store_for(session.binding)).project_file),
                     "generation": session.binding.generation, "name": session.name}
                    for session in self._sessions()]}

    def _project_rows(self) -> list[dict]:
        launcher = project_owner(self.bridge)
        if launcher is None:
            return []
        rows = []
        for session in self._sessions():
            for route in launcher.host.routes(session.binding.project_id):
                window = launcher.host.window_for(route)
                if window is not None:
                    rows.append({"project_id": session.binding.project_id,
                                 "session_generation": session.binding.generation,
                                 "module_key": route.module_key, "window_type": route.module_key,
                                 "instance_id": route.route_id, "open": True,
                                 "visible": window.isVisible(), "title": window.title(),
                                 "state": "ready", "capabilities": self._project_capabilities(route.module_key)})
        return rows

    @staticmethod
    def _project_capabilities(module: str) -> dict:
        from .project_editor_operations import project_editor_capabilities
        editors = project_editor_capabilities(module)
        return project_window_capabilities() | editors | {
            "unsupported_operations": [operation for operation in ("update_window", "window_action")
                                       if operation not in editors["editor_operations"]]}

    def _legacy_identity(self, module: str) -> tuple[Any, str] | None:
        if module == "launcher":
            window = getattr(self.bridge, "_launcher_window", None)
            if window is None:
                return None
            if window not in self._launcher_ids:
                self._launcher_ids[window] = uuid4().hex
            return window, self._launcher_ids[window]
        lookup = getattr(self.bridge, "_existing_window", None)
        handle = lookup(module) if callable(lookup) else next((
            row for row in getattr(self.bridge, "_windows", {}).values()
            if row.key == module), None)
        return (handle.window, handle.instance_id) if handle is not None else None

    def _enrich_legacy(self, row: dict) -> dict:
        module = row.get("window_type", "")
        identity = self._legacy_identity(module)
        if identity is None:
            return row
        window, instance_id = identity
        title = getattr(window, "title", None) or getattr(window, "windowTitle", None)
        return row | {"instance_id": instance_id, "module_key": module, "open": True,
                      "visible": window.isVisible(), "title": title() if callable(title) else module}

    def dispatch(self, operation: str, arguments: dict) -> dict:
        if operation == "lifecycle_status":
            return self.status(arguments.get("nonce"))
        if getattr(self.bridge, "_project_starting", False) and operation not in {
                "list_windows", "read_window", "project_editor_snapshot", "lifecycle_prepare", "lifecycle_cancel"}:
            raise UiControlError("project_starting", "工程窗口仍在完成身份登记，请稍后重试。")
        if operation.startswith("lifecycle_"):
            lifecycle = getattr(self.bridge, "project_lifecycle", None)
            if lifecycle is None:
                raise UiControlError("lifecycle_unavailable", "退出协调尚未就绪。")
            return lifecycle.dispatch(operation, arguments)
        lifecycle = getattr(self.bridge, "project_lifecycle", None)
        if lifecycle is not None and lifecycle.active and operation not in {"list_windows", "read_window", "project_editor_snapshot"}:
            raise UiControlError("application_exiting", "退出确认期间暂停窗口修改操作。")
        if operation == "project_editor_snapshot":
            if arguments.get("project_id") is None or set(arguments) - {
                    "project_id", "session_generation", "client_generation", "project_root", "project_file",
                    "module_key", "window_type", "instance_id", "detail", "offset", "limit", "expected_snapshot_id"}:
                raise UiControlError("invalid_arguments", "读取当前编辑内容需要完整工程身份与已声明参数。")
        if operation == "project_manage":
            return self._manage_project(arguments)
        if operation == "list_windows" and arguments.get("project_id") is None:
            value = self.registry.dispatch(operation, arguments)
            value["windows"] = [self._enrich_legacy(row) for row in value["windows"]]
            value["windows"].extend(self._project_rows())
            return value
        if arguments.get("project_id") is None:
            scope_error = legacy_window_scope_error(self.bridge, arguments.get("window_type", ""))
            if scope_error is not None:
                raise UiControlError(*scope_error)
        project_id = arguments.get("project_id")
        if project_id is None:
            instance_id = arguments.get("instance_id")
            if instance_id is not None:
                identity = self._legacy_identity(arguments.get("window_type", ""))
                if identity is None or identity[1] != instance_id:
                    raise UiControlError("stale_window", "窗口实例已关闭或替换。")
            if operation == "close_window" and arguments.get("window_type") == "launcher":
                self.bridge.closeLauncher()
                return {"accepted": True, "window_type": "launcher"}
            return self.registry.dispatch(operation, arguments)
        session = next((item for item in self._sessions() if item.binding.project_id == project_id), None)
        if session is None or arguments.get("session_generation") != session.binding.generation:
            raise UiControlError("stale_project", "工程会话已关闭或身份已失效。")
        launcher = project_owner(self.bridge)
        if ("client_generation" in arguments
                and arguments["client_generation"] != self.generation):
            raise UiControlError("stale_client", "原工程进程身份已经失效。")
        if operation == "project_editor_snapshot" and any(
                not isinstance(arguments.get(key), str) or not arguments[key].strip()
                for key in ("client_generation", "project_root", "project_file")):
            raise UiControlError("invalid_arguments", "读取当前编辑内容需要完整且精确的工程会话身份。")
        from .project_asset_materials import asset_publication_busy
        if (operation not in {"list_windows", "read_window", "project_editor_snapshot"}
                and asset_publication_busy(launcher.host, session.binding)):
            raise UiControlError("window_busy", "工程资产正在提交，请等待明确结果后再修改窗口。")
        try:
            store = launcher.host.store_for(session.binding)
        except ProjectSessionError as exc:
            if operation == "project_editor_snapshot" and exc.code in {"project_closing", "application_exiting"}:
                return {"available": False, "state": "unavailable", "reason": "project_frozen",
                        "source": "current_editor", "saved_fallback": False,
                        "project_id": project_id, "session_generation": session.binding.generation,
                        "client_generation": self.generation, "module_key": arguments.get("module_key")}
            raise
        if (("project_root" in arguments and canonical_path(arguments["project_root"]) != canonical_path(store.root))
                or ("project_file" in arguments and canonical_path(arguments["project_file"]) != canonical_path(store.project_file))):
            raise UiControlError("project_identity_mismatch", "工程文件或目录与当前会话不一致。")
        if operation == "list_windows":
            return {"project_id": project_id, "session_generation": session.binding.generation,
                    "scope": "bound_project", "available_windows": sorted(PROJECT_WINDOW_MODULES),
                    "capabilities": project_window_capabilities(),
                    "windows": [row for row in self._project_rows() if row["project_id"] == project_id]}
        module = arguments.get("module_key")
        if not isinstance(module, str) or module not in PROJECT_WINDOW_MODULES:
            raise UiControlError("invalid_window", "未知工程模块。")
        if operation == "project_editor_snapshot":
            from .project_editor_snapshot import project_editor_snapshot
            from coat_sim.project_ui import project_window_arguments
            payload = {key: arguments[key] for key in
                       ("detail", "offset", "limit", "expected_snapshot_id") if key in arguments}
            project_window_arguments(operation, {"window_type": module, **payload})
            from .project_editor_operations import project_editor_capabilities
            result = project_editor_snapshot(launcher, session, module, payload,
                                             client_generation=self.generation, bridge=self.bridge)
            instance = arguments.get("instance_id")
            if instance is not None and result.get("instance_id") != instance:
                raise UiControlError("stale_window", "窗口实例已关闭或替换。")
            return result | {"editor_capabilities": project_editor_capabilities(module)}
        rows = [row for row in self._project_rows() if row["project_id"] == project_id
                and row["module_key"] == module]
        if module == "materials" and not rows:
            from .project_asset_materials import existing_material_view
            route, window, controller = existing_material_view(launcher.host, session.binding)
            if route is not None and window is not None and controller is not None:
                rows = [{"project_id": project_id, "session_generation": session.binding.generation,
                         "module_key": "materials", "window_type": "materials", "instance_id": route.route_id,
                         "open": True, "visible": window.isVisible(), "title": window.title(), "state": "ready",
                         "physical_module": route.module_key, "capabilities": self._project_capabilities("materials")}]
        instance = arguments.get("instance_id")
        if instance is not None and (not rows or rows[0]["instance_id"] != instance):
            raise UiControlError("stale_window", "窗口实例已关闭或替换。")
        if operation in {"update_window", "window_action"}:
            from .project_editor_operations import project_editor_operation
            return project_editor_operation(launcher, session, module, operation, arguments,
                                            client_generation=self.generation, bridge=self.bridge)
        if operation == "open_window":
            if any(arguments.get(name) is not None for name in
                   ("fields", "design_path", "data", "intended_path", "pointers")):
                raise UiControlError("unsupported_operation", "工程窗口打开只支持导航，不接受文档或字段替换。")
            workspace = ProjectFilmWorkspace.for_project(launcher.host, session.binding)
            route_id = launcher.desktop.open_project_module(workspace, module)
            activated = getattr(self.bridge, "_project_activation_callback", None)
            if callable(activated) and module == "film_design":
                activated()
            return {"instance_id": route_id, "project_id": project_id,
                    "generation": session.binding.generation,
                    "capabilities": self._project_capabilities(module)} | (
                        {"history_managed": True} if callable(activated) else {})
        if operation == "read_window":
            return rows[0] if rows else {"open": False, "visible": False, "state": "closed",
                "project_id": project_id, "session_generation": session.binding.generation,
                "module_key": module, "capabilities": self._project_capabilities(module)}
        if operation == "close_window" and rows:
            route = next(route for route in launcher.host.routes(project_id)
                         if route.route_id == rows[0]["instance_id"])
            window = launcher.host.window_for(route)
            return {"accepted": bool(window.close()), "instance_id": route.route_id,
                    "project_id": project_id, "session_generation": session.binding.generation,
                    "close_review": "native_owner", "force_applied": False}
        if operation == "close_window":
            return {"accepted": False, "open": False, "project_id": project_id,
                    "session_generation": session.binding.generation, "module_key": module}
        raise UiControlError("invalid_operation", "该工程窗口不支持此操作。")

    def _manage_project(self, arguments: dict) -> dict:
        from coat_sim.project_management import ProjectManagementService, WRITE_OPERATIONS
        from coat_sim.core.project_dependencies import ProjectError
        if arguments.get("client_generation") != self.generation:
            raise UiControlError("stale_client", "项目管理请求的进程身份已经失效。")
        payload = arguments.get("arguments")
        operation = arguments.get("project_operation")
        if not isinstance(payload, dict) or not isinstance(operation, str):
            raise UiControlError("invalid_arguments", "项目管理请求格式无效。")
        session = next((row for row in self._sessions()
                        if row.binding.project_id == payload.get("project_id")), None)
        if session is None or session.binding.generation != arguments.get("session_generation"):
            raise UiControlError("stale_project", "项目会话身份已经失效。")
        launcher = project_owner(self.bridge)
        if operation in WRITE_OPERATIONS:
            from .project_asset_materials import asset_publication_busy
            if asset_publication_busy(launcher.host, session.binding):
                raise UiControlError("window_busy", "工程资产正在提交，请等待明确结果后再修改工程。")
            film_owner = launcher.host.resource_for(session.binding, "film_workspace")
            editor = getattr(film_owner, "_editor", None)
            resource_owner = launcher.host.resource_for(session.binding, "resource_workspace")
            compute_owner = launcher.host.resource_for(session.binding, "compute_workspace")
            dirty = ((editor is not None and editor.snapshot().dirty) or (
                resource_owner is not None and bool(resource_owner.dirty_summary()))
                or bool(getattr(compute_owner, "has_dirty_draft", False)))
            if self.bridge._uncommitted_project_view_draft(session.binding.project_id) or dirty:
                raise UiControlError("dirty_project", "请先保存或处理该工程的未保存修改。")
            if operation == "save":
                # Saved resources can be viewed by several retained adapters.
                # Replacing a live document requires the editor's own contract.
                raise UiControlError("editor_owned_document", "请通过该工程编辑器保存已打开的设计。")
        with launcher.host.operation_scope(session.binding):
            store = launcher.host.store_for(session.binding)
            service = ProjectManagementService(authorized_roots=[row.binding.root for row in self._sessions()])
            try:
                result = service.execute_owned(operation, payload, store=store,
                                               project_id=session.binding.project_id)
            except ProjectError as exc:
                raise UiControlError(exc.code, str(exc)) from exc
            film = launcher.host.resource_for(session.binding, "film_workspace")
            if film is not None and operation in WRITE_OPERATIONS:
                film.refresh_materials()
                if film._editor is not None:
                    film._editor.refresh_saved_designs()
        launcher.projectsChanged.emit()
        return result
