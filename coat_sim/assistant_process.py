"""Independent per-user assistant process and authenticated application registry.

The desktop entry is imported only when starting this process. Headless clients
and project tools can import this module without importing Qt. Process identity
checks never signal, enumerate or terminate application or remote processes.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
from threading import Event, RLock, Thread
import time
from typing import Any
from uuid import uuid4

from coat_sim.assistant_diagnostics import client_trace, lifecycle_log, source_id, trace
from coat_sim.version import BUILD_IDENTITY
from coat_sim.core.process_identity import _process_identity
from coat_sim.project_recovery import HeldFileLock
from coat_sim.ui.control import PROJECT_ROOT, UiControlClient, UiControlError

ASSISTANT_OPERATIONS = frozenset({"status", "show_assistant", "show_queue", "reload_configuration",
    "register_client", "unregister_client", "list_clients", "request_exit_all", "exit_status", "project_manage",
    "activate_project", "project_window"})
ASSISTANT_PROTOCOL = 1

LIFECYCLE_OPERATIONS = frozenset({"lifecycle_status", "lifecycle_prepare", "lifecycle_commit", "lifecycle_cancel", "lifecycle_handoff",
                                  "list_windows", "open_window", "close_window", "read_window", "project_manage",
                                  "project_editor_snapshot", "update_window", "window_action"})


def _project_validation_detail(error: BaseException) -> str:
    """Retain file/schema diagnostics across IPC, which drops exception causes."""
    details, seen = [], set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "code", type(current).__name__)
        details.append(f"{code}: {current}")
        current = current.__cause__
    return "；".join(details)


def assistant_runtime_root() -> Path:
    """One per-user scope shared across projects/installations; tests override."""
    explicit = os.environ.get("COAT_ASSISTANT_RUNTIME_ROOT")
    if explicit:
        return Path(explicit).expanduser().absolute()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "CoatSim" / "assistant"


class AssistantProcessClient:
    """Discovery/start client with no lifetime ownership of the assistant."""

    def __init__(self, runtime_root: Path | str | None = None, *,
                 application_root: Path | str = PROJECT_ROOT, timeout: float = 5.0):
        self.root = Path(runtime_root).resolve() if runtime_root is not None else assistant_runtime_root().resolve()
        self.application_root = Path(application_root).resolve()
        self.timeout = timeout
        self._compatibility_notice: tuple[Any, Any, Any] | None = None
        self._last_probe_error: UiControlError | None = None

    def trace(self, event: str, *, error: BaseException | None = None, **values: Any) -> None:
        client_trace(self.application_root, event, error=error, **values)

    def _transport(self) -> UiControlClient:
        client = UiControlClient(self.root, timeout=self.timeout, operations=ASSISTANT_OPERATIONS,
                                 endpoint_prefix="assistant")
        # Launcher targeting never selects the per-user assistant.
        client.target_pid, client._explicit_pid = None, False
        return client

    def status(self) -> dict[str, Any]:
        client = self._transport()
        for endpoint in client._endpoints():
            state, start = _process_identity(endpoint["pid"])
            if state == "dead":
                continue
            if state != "alive" or start is None:
                raise UiControlError("process_identity_unknown", "Cannot verify assistant process identity")
            nonce = secrets.token_hex(16)
            pinned = UiControlClient(self.root, endpoint["pid"], self.timeout,
                operations=ASSISTANT_OPERATIONS, endpoint_prefix="assistant", initial_endpoint=endpoint)
            try:
                value = pinned.request("status", nonce=nonce)
            except UiControlError as exc:
                if exc.code == "ui_unavailable":
                    self._last_probe_error = exc
                    continue
                raise
            if (value.get("application") != "coat_sim" or value.get("nonce") != nonce
                    or value.get("pid") != endpoint["pid"] or value.get("process_start") != start
                    or not isinstance(value.get("generation"), str) or not value["generation"]):
                raise UiControlError("process_identity_mismatch", "Assistant endpoint failed its identity challenge")
            protocol = value.get("assistant_protocol")
            if protocol is not None and (type(protocol) is not int or protocol != ASSISTANT_PROTOCOL):
                raise UiControlError("assistant_protocol_mismatch", "Shared assistant protocol is incompatible; close the original application normally before switching versions.")
            notice = (value.get("version"), value.get("source_id"), protocol)
            if notice != self._compatibility_notice:
                self._compatibility_notice = notice
                self.trace("assistant_service_identified", assistant_pid=value["pid"], protocol=protocol,
                      remote_version=value.get("version"), remote_source_id=value.get("source_id"),
                      reason="legacy_unversioned" if protocol is None else "compatible")
            return value
        raise UiControlError("assistant_unavailable", "Shared assistant is not running")

    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        if operation == "status":
            return self.status()
        status = self.status()
        transport = self._transport()
        transport.target_pid, transport._explicit_pid = status["pid"], True
        return transport.request(operation, **arguments)

    def ensure_running(self, timeout: float = 20.0) -> dict[str, Any]:
        """Blocking startup for a caller's worker thread; never stops a child."""
        try:
            return self.status()
        except UiControlError as exc:
            if exc.code != "assistant_unavailable":
                raise
        from coat_sim.project_logging import logged_module_command, log_path
        attempt = uuid4().hex
        started = time.monotonic()
        self.trace("assistant_start_requested", attempt=attempt, source_id=source_id(self.application_root), version=BUILD_IDENTITY.version)
        command = logged_module_command("coat_sim.assistant_process", "--runtime-root", str(self.root),
            channel="assistant", project_root=self.application_root)
        environment = os.environ.copy()
        for name in ("COAT_UI_PID", "COAT_UI_PARENT_PID", "COAT_UI_READY_ADDRESS", "COAT_UI_READY_FAMILY", "COAT_UI_READY_KEY"):
            environment.pop(name, None)
        environment["COAT_ASSISTANT_RUNTIME_ROOT"] = str(self.root)
        environment["PYTHONUNBUFFERED"] = "1"
        logs = log_path("assistant", self.application_root).parent
        kwargs: dict[str, Any] = {"cwd": str(self.application_root), "env": environment,
            "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
            "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        self.trace("assistant_child_started", attempt=attempt, child_pid=process.pid)
        def observe_exit() -> None:
            try:
                code = process.wait()
                self.trace("assistant_child_exited", attempt=attempt, child_pid=process.pid, exit_code=code)
            except Exception as exc:
                self.trace("assistant_child_observer_failed", error=exc, attempt=attempt, child_pid=process.pid)
        Thread(target=observe_exit, name="assistant-owned-child-exit", daemon=True).start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = self.status()
                self.trace("assistant_start_ready", attempt=attempt, child_pid=process.pid, assistant_pid=value["pid"], elapsed_s=round(time.monotonic()-started,3))
                return value
            except UiControlError as exc:
                if exc.code != "assistant_unavailable":
                    raise
            code = process.poll()
            if code not in (None, 0):
                self.trace("assistant_child_exit_before_ready", attempt=attempt, child_pid=process.pid, exit_code=code)
                raise UiControlError("assistant_start_failed", f"Assistant startup failed (exit {code}); inspect {logs / 'assistant.log'}")
            time.sleep(0.05)
        error = UiControlError("assistant_start_timeout", f"Assistant startup is still unresolved; inspect {logs / 'assistant.log'}")
        if self._last_probe_error is not None:
            self.trace("assistant_last_probe_failed", error=self._last_probe_error, attempt=attempt)
        self.trace("assistant_start_timeout", error=error, attempt=attempt, child_pid=process.pid,
              exit_code=process.poll(), elapsed_s=round(time.monotonic()-started,3))
        raise error

    def register_client(self, endpoint: dict[str, Any], *, generation: str,
                        process_start: str) -> dict[str, Any]:
        return self.request("register_client", endpoint=endpoint, generation=generation, process_start=process_start)

    def unregister_client(self, client_id: str, generation: str) -> bool:
        return bool(self.request("unregister_client", client_id=client_id, generation=generation).get("removed"))

    def list_clients(self) -> list[dict[str, Any]]:
        return self.request("list_clients")["clients"]


@dataclass
class _Client:
    client_id: str
    endpoint: dict[str, Any]
    generation: str
    process_start: str
    role: str
    projects: list[dict[str, Any]]

    def public(self, state: str = "alive") -> dict[str, Any]:
        return {"client_id": self.client_id, "pid": self.endpoint["pid"], "generation": self.generation,
                "process_start": self.process_start, "role": self.role, "state": state,
                "project_sessions": [dict(item) for item in self.projects]}


def _allow_foreground_process(pid: int) -> bool:
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    allow = vars(ctypes)["WinDLL"]("user32", use_last_error=True).AllowSetForegroundWindow
    allow.argtypes = [wintypes.DWORD]
    allow.restype = wintypes.BOOL
    return bool(allow(pid))


class ApplicationClientRegistry:
    """Authenticated participants and all-prepare-before-any-commit exit."""

    def __init__(self):
        self._clients: dict[str, _Client] = {}
        self._lock = RLock()
        self._exiting = False
        self._windows: dict[str, tuple[_Client, dict[str, Any]]] = {}

    @staticmethod
    def _call(client: _Client, operation: str, **arguments: Any) -> dict[str, Any]:
        state, start = _process_identity(client.endpoint["pid"])
        if state == "dead" or (start is not None and start != client.process_start):
            raise UiControlError("client_exited", "Registered application process has exited")
        if state != "alive" or start is None:
            raise UiControlError("process_identity_unknown", "Cannot verify registered application process")
        endpoint = client.endpoint
        transport = UiControlClient(endpoint["root"], endpoint["pid"], timeout=3.0,
                                     operations=LIFECYCLE_OPERATIONS, initial_endpoint=endpoint)
        return transport.request(operation, **arguments)

    @classmethod
    def _status(cls, client: _Client) -> dict[str, Any]:
        nonce = secrets.token_hex(16)
        value = cls._call(client, "lifecycle_status", nonce=nonce)
        if (value.get("application") != "coat_sim" or value.get("nonce") != nonce
                or value.get("pid") != client.endpoint["pid"] or value.get("generation") != client.generation
                or value.get("process_start") != client.process_start or value.get("role") != client.role):
            raise UiControlError("process_identity_mismatch", "Application client failed its identity challenge")
        projects = value.get("project_sessions", [])
        if not isinstance(projects, list) or len(projects) > 256:
            raise UiControlError("invalid_client", "Invalid project session list")
        cleaned = []
        for project in projects:
            if not isinstance(project, dict) or any(not isinstance(project.get(key), str) or not project[key]
                                                  for key in ("project_id", "root", "project_file", "generation")):
                raise UiControlError("invalid_client", "Invalid explicit project binding")
            cleaned.append({key: project[key] for key in ("project_id", "root", "project_file", "generation")}
                           | {"name": str(project.get("name", ""))})
        value["project_sessions"] = cleaned
        return value

    def register_client(self, endpoint: dict[str, Any], *, generation: str,
                        process_start: str) -> dict[str, Any]:
        if (not isinstance(endpoint, dict) or not isinstance(generation, str) or not generation
                or not isinstance(process_start, str) or not process_start):
            raise UiControlError("invalid_client", "Explicit endpoint and process generation are required")
        try:
            transport = UiControlClient(endpoint["root"], endpoint.get("pid"), initial_endpoint=endpoint)
            if not transport._endpoint_is_valid(endpoint, endpoint.get("pid")):
                raise ValueError("invalid endpoint")
            role = endpoint.get("role")
            if role not in {"launcher", "project_host"}:
                raise ValueError("invalid role")
        except (KeyError, TypeError, ValueError) as exc:
            raise UiControlError("invalid_client", "Only authenticated application endpoints may register") from exc
        candidate = _Client(uuid4().hex, dict(endpoint), generation, process_start, role, [])
        value = self._status(candidate)
        candidate.projects = value["project_sessions"]
        with self._lock:
            if self._exiting:
                raise UiControlError("exit_in_progress", "Application exit is in progress")
            # Remove only exact dead/replaced OS identities. An inaccessible
            # live endpoint cannot silently relinquish project ownership.
            self._prune_dead()
            same = next((item for item in self._clients.values()
                         if item.endpoint["pid"] == endpoint["pid"] and item.process_start == process_start
                         and item.generation == generation), None)
            for other in self._clients.values():
                if other is same:
                    continue
                for project in candidate.projects:
                    if any(item["project_id"] == project["project_id"] or
                           os.path.normcase(str(Path(item["root"]).resolve())) ==
                           os.path.normcase(str(Path(project["root"]).resolve())) for item in other.projects):
                        raise UiControlError("project_owner_conflict", "Project already has another registered owner")
            if same is not None:
                # Heartbeat registration renews the same generation, not a new
                # owner. Keep captured window targets valid for that owner.
                same.endpoint, same.projects = candidate.endpoint, candidate.projects
                return same.public()
            self._clients[candidate.client_id] = candidate
            return candidate.public()

    def _prune_dead(self) -> None:
        for identity, client in tuple(self._clients.items()):
            state, start = _process_identity(client.endpoint["pid"])
            if state == "dead" or (start is not None and start != client.process_start):
                self._clients.pop(identity, None)

    def unregister_client(self, client_id: str, generation: str) -> bool:
        with self._lock:
            if self._exiting:
                raise UiControlError("exit_in_progress", "Application exit is in progress")
            current = self._clients.get(client_id)
            if current is None or current.generation != generation:
                return False
            del self._clients[client_id]
            return True

    def list_clients(self) -> list[dict[str, Any]]:
        with self._lock:
            self._prune_dead()
            snapshot = tuple(self._clients.values())
        result = []
        for client in snapshot:
            try:
                value = self._status(client)
                client.projects = value["project_sessions"]
                result.append(client.public())
            except UiControlError as exc:
                if exc.code == "client_exited":
                    continue
                result.append(client.public("unavailable") | {"error": {"code": exc.code, "message": str(exc)}})
        return result

    def activate_project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Raise only the exact existing owner's fixed project entry window."""
        from coat_sim.project_activation import ACTIVATION_FIELDS, canonical_path
        from coat_sim.core.project_store import ProjectError, ProjectStore
        if (not isinstance(arguments, dict) or set(arguments) != ACTIVATION_FIELDS
                or any(not isinstance(value, str) or not value.strip() for value in arguments.values())):
            raise UiControlError("invalid_arguments", "工程激活需要完整且精确的会话身份。")
        with self._lock:
            if self._exiting:
                raise UiControlError("exit_in_progress", "应用正在退出，暂不能激活工程。")
            client = self._clients.get(arguments["client_id"])
            if client is None or client.generation != arguments["client_generation"]:
                raise UiControlError("stale_client", "原工程进程身份已失效，请刷新工程列表。")
        try:
            status = self._status(client)
        except UiControlError as exc:
            raise UiControlError(exc.code, "当前无法联系并核验原工程进程；原窗口和当前内容仍保留。") from exc
        project = next((row for row in status["project_sessions"]
                        if row["project_id"] == arguments["project_id"]), None)
        if project is None or project["generation"] != arguments["session_generation"]:
            raise UiControlError("stale_project", "原工程会话已关闭或更新，请刷新工程列表。")
        if (canonical_path(project["root"]) != canonical_path(arguments["root"])
                or canonical_path(project["project_file"]) != canonical_path(arguments["project_file"])):
            raise UiControlError("project_identity_mismatch", "所选工程目录或文件与原会话不一致。")
        try:
            store = ProjectStore.open(arguments["project_file"])
        except (ProjectError, OSError) as exc:
            raise UiControlError("project_identity_mismatch",
                "无法核验所选工程文件，未激活其他工程。原因：" + _project_validation_detail(exc)) from exc
        if (store.manifest["id"] != arguments["project_id"]
                or canonical_path(store.root) != canonical_path(project["root"])):
            raise UiControlError("project_identity_mismatch", "所选工程身份已改变，未激活其他工程。")
        with self._lock:
            if self._exiting:
                raise UiControlError("exit_in_progress", "应用正在退出，暂不能激活工程。")
            if self._clients.get(client.client_id) is not client:
                raise UiControlError("stale_client", "原工程进程身份已更新，请刷新工程列表。")
        try:
            result = self._call(client, "open_window", project_id=project["project_id"],
                                session_generation=project["generation"], module_key="film_design")
        except UiControlError as exc:
            message = ("原工程窗口仍在完成身份登记，请稍后重试；原会话和当前内容仍保留。"
                       if exc.code == "project_starting"
                       else "原工程窗口未能激活；原会话和当前内容仍保留。")
            raise UiControlError(exc.code, message) from exc
        if (not isinstance(result, dict) or not isinstance(result.get("instance_id"), str)
                or not result["instance_id"].strip()
                or result.get("project_id") != project["project_id"]
                or result.get("generation") != project["generation"]):
            raise UiControlError("activation_unconfirmed", "原工程窗口尚未确认激活，请稍后重试。")
        try:
            view = self._call(client, "read_window", project_id=project["project_id"],
                              session_generation=project["generation"], module_key="film_design",
                              instance_id=result["instance_id"])
        except UiControlError as exc:
            raise UiControlError(exc.code, "原工程窗口尚未确认可见，请稍后重试。") from exc
        if (not isinstance(view, dict) or view.get("open") is not True or view.get("visible") is not True
                or view.get("instance_id") != result["instance_id"]
                or view.get("project_id") != project["project_id"]
                or view.get("session_generation") != project["generation"]):
            raise UiControlError("activation_unconfirmed", "原工程窗口尚未确认可见，请稍后重试。")
        return {"activated": True, "instance_id": result["instance_id"],
                "project_id": project["project_id"], "session_generation": project["generation"],
                "client_generation": client.generation} | (
                    {"history_managed": True} if result.get("history_managed") is True else {})

    def project_window(self, target: dict[str, Any], operation: str,
                       arguments: dict[str, Any]) -> dict[str, Any]:
        """Route bounded window/context/guarded-editor calls to the exact freshly challenged owner."""
        from coat_sim.project_activation import canonical_path
        from coat_sim.project_ui import project_target_data, project_window_arguments
        from coat_sim.core.project_store import ProjectError, ProjectStore
        selected = project_target_data(target)
        payload = project_window_arguments(operation, arguments)
        with self._lock:
            if self._exiting:
                raise UiControlError("exit_in_progress", "应用正在退出，暂不能操作工程窗口。")
            client = self._clients.get(selected["client_id"])
            if client is None or client.generation != selected["client_generation"]:
                raise UiControlError("stale_client", "原工程进程身份已失效，请刷新工程列表。")
        status = self._status(client)
        project = next((row for row in status["project_sessions"]
                        if row["project_id"] == selected["project_id"]), None)
        if project is None or project["generation"] != selected["session_generation"]:
            raise UiControlError("stale_project", "原工程会话已关闭或更新，请刷新工程列表。")
        if (canonical_path(project["root"]) != canonical_path(selected["root"])
                or canonical_path(project["project_file"]) != canonical_path(selected["project_file"])):
            raise UiControlError("project_identity_mismatch", "所选工程目录或文件与原会话不一致。")
        try:
            store = ProjectStore.open(selected["project_file"])
        except (ProjectError, OSError) as exc:
            raise UiControlError("project_identity_mismatch",
                "无法核验所选工程文件，未操作其他工程。原因：" + _project_validation_detail(exc)) from exc
        if (store.manifest["id"] != selected["project_id"]
                or canonical_path(store.root) != canonical_path(project["root"])):
            raise UiControlError("project_identity_mismatch", "所选工程身份已改变，未操作其他工程。")
        with self._lock:
            if self._exiting:
                raise UiControlError("exit_in_progress", "应用正在退出，暂不能操作工程窗口。")
            if self._clients.get(client.client_id) is not client:
                raise UiControlError("stale_client", "原工程进程身份已更新，请刷新工程列表。")
        if operation == "open_window" and any(
                value is not None and (name != "force" or value is not False)
                for name, value in payload.items() if name != "window_type"):
            raise UiControlError("unsupported_operation", "工程窗口打开只支持导航；文档与字段须在原工程编辑器中处理。")
        forwarded = {"project_id": project["project_id"],
                     "session_generation": project["generation"],
                     "client_generation": client.generation,
                     "project_root": project["root"], "project_file": project["project_file"]}
        if operation != "list_windows":
            forwarded["module_key"] = payload["window_type"]
            forwarded["window_type"] = payload["window_type"]
        if operation in {"project_editor_snapshot", "update_window", "window_action"}:
            forwarded.update({key: value for key, value in payload.items() if key != "window_type"})
        # force is deliberately not forwarded: native Close and last-window
        # project review retain their complete existing approval boundaries.
        result = self._call(client, operation, **forwarded)
        if not isinstance(result, dict):
            raise UiControlError("invalid_ui_response", "工程窗口回复无效。")
        if operation == "open_window" and (
                not isinstance(result.get("instance_id"), str) or not result["instance_id"].strip()
                or result.get("project_id") != project["project_id"]
                or result.get("generation") != project["generation"]):
            raise UiControlError("window_unconfirmed", "工程窗口未确认打开，请先刷新窗口状态。")
        if operation == "open_window":
            view = self._call(client, "read_window", **forwarded,
                              instance_id=result["instance_id"])
            if (not isinstance(view, dict) or view.get("open") is not True
                    or view.get("visible") is not True
                    or view.get("instance_id") != result["instance_id"]
                    or view.get("project_id") != project["project_id"]
                    or view.get("session_generation") != project["generation"]
                    or view.get("module_key") != payload["window_type"]):
                raise UiControlError("window_unconfirmed", "工程窗口尚未确认可见，请刷新窗口状态。")
            result = result | view
        if operation in {"list_windows", "read_window", "close_window", "project_editor_snapshot", "update_window", "window_action"} and (
                result.get("project_id") != project["project_id"]
                or result.get("session_generation") != project["generation"]):
            raise UiControlError("window_unconfirmed", "工程窗口回复的会话身份不一致。")
        if operation == "list_windows":
            windows = result.get("windows")
            if (not isinstance(windows, list) or len(windows) > 512
                    or any(not isinstance(row, dict)
                           or row.get("project_id") != project["project_id"]
                           or row.get("session_generation") != project["generation"] for row in windows)):
                raise UiControlError("window_unconfirmed", "工程窗口列表包含不匹配的会话身份。")
        return result | {"project_target": selected}

    def manage_project(self, operation: str, arguments: dict[str, Any], *,
                       session_generation: str = "") -> dict[str, Any]:
        """Route to an exact active owner or use bounded headless persistence."""
        from coat_sim.project_management import ProjectManagementService
        from coat_sim.core.project_store import ProjectStore
        if not isinstance(arguments, dict):
            raise UiControlError("invalid_arguments", "Project arguments must be an explicit object")
        public = self.list_clients()
        roots = [project["root"] for client in public if client.get("state") == "alive"
                 for project in client.get("project_sessions", [])]
        management = ProjectManagementService(authorized_roots=roots)
        with self._lock:
            if self._exiting:
                raise UiControlError("exit_in_progress", "Application exit is in progress")
            clients = tuple(self._clients.values())
        project_id, target = arguments.get("project_id"), arguments.get("root")
        selected_store = None
        if operation != "create":
            if not isinstance(target, str):
                raise UiControlError("invalid_arguments", "An explicit .coatproj project file is required")
            management._authorize_path(target)
            # Full descriptor validation precedes routing by its parent root.
            # Registration roots never authorize a directory/legacy alias.
            selected_store = ProjectStore.open(target)
            if selected_store.manifest["id"] != project_id:
                raise UiControlError("project_identity_mismatch", "Explicit project ID differs from the selected file")
        selected = []
        for client in clients:
            for project in client.projects:
                if project["project_id"] == project_id:
                    if selected_store is None or selected_store.root != Path(project["root"]).resolve():
                        raise UiControlError("project_identity_mismatch", "Explicit root differs from the registered project")
                    if selected_store.project_file != Path(project["project_file"]).resolve():
                        raise UiControlError("project_identity_mismatch", "Explicit file differs from the registered project")
                    selected.append((client, project))
        if len(selected) > 1:
            raise UiControlError("project_owner_conflict", "Project has conflicting registered owners")
        if selected:
            client, project = selected[0]
            if not session_generation or session_generation != project["generation"]:
                raise UiControlError("stale_project", "Active owner requires its explicit current session_generation")
            # Challenge immediately before dispatch, not cached endpoint data.
            status = self._status(client)
            if not any(item["project_id"] == project_id and item["generation"] == session_generation
                       and selected_store is not None
                       and Path(item["root"]).resolve() == selected_store.root
                       and Path(item["project_file"]).resolve() == selected_store.project_file
                       for item in status["project_sessions"]):
                raise UiControlError("stale_project", "The selected project session has ended")
            for field in ("file", "destination"):
                if arguments.get(field):
                    management._authorize_path(arguments[field])
            return self._call(client, "project_manage", project_operation=operation, arguments=arguments,
                              session_generation=session_generation, client_generation=client.generation)
        return management.execute(operation, arguments, actor="assistant")

    def list_windows(self) -> list[dict[str, Any]]:
        """Aggregate exact app-owned windows, never scan the OS desktop."""
        result: list[dict[str, Any]] = []
        targets: dict[str, tuple[_Client, dict[str, Any]]] = {}
        self.list_clients()  # Refresh authenticated project bindings first.
        with self._lock:
            clients = tuple(self._clients.values())
        for client in clients:
            try:
                value = self._call(client, "list_windows")
            except UiControlError:
                continue
            windows = value.get("windows", [])
            if not isinstance(windows, list) or len(windows) > 512:
                continue
            for row in windows:
                # The launcher deliberately hides while retaining its live
                # picker window. Keep explicitly open hidden windows so its
                # exact instance can be reopened from the assistant menu.
                if (not isinstance(row, dict) or row.get("open") is False
                        or (row.get("visible") is False and row.get("open") is not True)):
                    continue
                instance = row.get("instance_id")
                module = row.get("module_key") or row.get("window_type")
                if not isinstance(instance, str) or not instance or not isinstance(module, str) or not module:
                    continue
                project_id = row.get("project_id", "")
                generation = row.get("session_generation", "")
                if project_id and not any(project["project_id"] == project_id and
                                          project["generation"] == generation for project in client.projects):
                    continue
                arguments = {"instance_id": instance, "module_key": module, "window_type": module}
                if project_id:
                    arguments.update(project_id=project_id, session_generation=generation)
                identity = "remote:" + hashlib.sha256(json.dumps([client.client_id, client.generation,
                    project_id, generation, module, instance]).encode("utf-8")).hexdigest()
                targets[identity] = (client, arguments)
                result.append({"id": identity, "title": str(row.get("title") or module),
                               "project_id": project_id, "session_generation": generation,
                               "module_key": module, "instance_id": instance})
        with self._lock:
            self._windows = targets
        return result

    def prepare_window_activation(self, identity: str) -> bool:
        """Transfer this user gesture's foreground permission before async IPC."""
        with self._lock:
            if self._exiting:
                raise UiControlError("exit_in_progress", "Application exit is in progress")
            target = self._windows.get(identity)
            if target is None:
                raise UiControlError("stale_window", "Managed window identity is no longer registered")
            client, _arguments = target
            if self._clients.get(client.client_id) is not client:
                raise UiControlError("stale_client", "Managed window process generation has changed")
        state, start = _process_identity(client.endpoint["pid"])
        if state != "alive" or start != client.process_start:
            raise UiControlError("process_identity_unknown", "Cannot verify activation target process")
        return _allow_foreground_process(client.endpoint["pid"])

    def prepare_exit_activation(self) -> str:
        """Grant the menu gesture to the first exact client, never every PID."""
        with self._lock:
            if self._exiting:
                raise UiControlError("exit_in_progress", "Application exit is in progress")
            self._prune_dead()
            client = next(iter(self._clients.values()), None)
            if client is None:
                return ""
            state, start = _process_identity(client.endpoint["pid"])
            if state != "alive" or start != client.process_start:
                raise UiControlError("process_identity_unknown", "Cannot verify exit activation target")
            if not _allow_foreground_process(client.endpoint["pid"]):
                raise UiControlError("foreground_denied", "请从助手菜单重新退出，以显示未保存确认。")
            return client.client_id

    def window_action(self, identity: str, operation: str) -> dict[str, Any]:
        if operation not in {"open_window", "close_window"}:
            raise UiControlError("invalid_operation", "Unknown managed window operation")
        with self._lock:
            if self._exiting:
                raise UiControlError("exit_in_progress", "Application exit is in progress")
            target = self._windows.get(identity)
            if target is None:
                raise UiControlError("stale_window", "Managed window identity is no longer registered")
            client, arguments = target
            if self._clients.get(client.client_id) is not client:
                raise UiControlError("stale_client", "Managed window process generation has changed")
        status = self._status(client)
        project_id = arguments.get("project_id")
        if project_id and not any(project["project_id"] == project_id and
                                  project["generation"] == arguments["session_generation"]
                                  for project in status["project_sessions"]):
            raise UiControlError("stale_project", "Managed window project generation has ended")
        return self._call(client, operation, **arguments)

    def request_exit_all(self, exit_id: str, *, on_progress=None, cancel_wait: Event | None = None,
                         foreground_client_id: str | None = None) -> dict[str, Any]:
        """Blocking worker operation; pending user decisions have no timeout."""
        with self._lock:
            if self._exiting:
                raise UiControlError("exit_in_progress", "Application exit is already in progress")
            self._exiting = True
            self._prune_dead()
            clients = tuple(self._clients.values())
        prepared: list[tuple[_Client, str]] = []
        committed: list[str] = []
        pending: _Client | None = None
        next_renewal = time.monotonic() + 10.0

        def renew_prepared() -> None:
            for prepared_client, prepared_permit in prepared:
                renewed = self._call(prepared_client, "lifecycle_prepare", exit_id=exit_id,
                                     client_generation=prepared_client.generation)
                if (renewed.get("state") != "ready" or renewed.get("permit_id") != prepared_permit
                        or renewed.get("generation") != prepared_client.generation):
                    raise UiControlError("stale_permit", "A prepared exit permit expired or changed")
        try:
            if (foreground_client_id is not None
                    and foreground_client_id != (clients[0].client_id if clients else "")):
                raise UiControlError("stale_client", "退出前台目标已变化，请重新退出。")
            for client in clients:
                self._status(client)
                if foreground_client_id is not None and prepared:
                    previous, permit = prepared[-1]
                    handed = self._call(previous, "lifecycle_handoff", exit_id=exit_id,
                                        client_generation=previous.generation, permit_id=permit,
                                        target_pid=client.endpoint["pid"], target_start=client.process_start)
                    if handed.get("generation") != previous.generation or handed.get("granted") is not True:
                        raise UiControlError("foreground_denied", "未能显示下一工程确认，请重新退出。")
                pending = client
                while True:
                    reply = self._call(client, "lifecycle_prepare", exit_id=exit_id,
                                       client_generation=client.generation)
                    if reply.get("generation") != client.generation:
                        raise UiControlError("stale_permit", "Exit preparation returned another generation")
                    state = reply.get("state")
                    if state == "pending":
                        if time.monotonic() >= next_renewal:
                            renew_prepared()
                            next_renewal = time.monotonic() + 10.0
                        if on_progress:
                            on_progress({"state": "awaiting_confirmation", "client_id": client.client_id})
                        time.sleep(0.1)
                        continue
                    if state != "ready" or not isinstance(reply.get("permit_id"), str) or not reply["permit_id"]:
                        return {"state": "declined", "code": "client_close_declined", "client_id": client.client_id,
                                "committed_clients": []}
                    prepared.append((client, reply["permit_id"]))
                    pending = None
                    break
            # Refresh every ready lease before the first irreversible commit.
            renew_prepared()
            for client, permit in prepared:
                reply = self._call(client, "lifecycle_commit", exit_id=exit_id,
                                   client_generation=client.generation, permit_id=permit)
                if reply.get("generation") != client.generation or reply.get("accepted") is not True:
                    raise UiControlError("shutdown_refused", "Application client refused its prepared exit")
                committed.append(client.client_id)
            if on_progress:
                on_progress({"state": "waiting_for_exit", "committed_clients": committed})
            # Keep the assistant available while exact graceful exits finish.
            # A refused/stuck process is reported, never forcibly terminated.
            deadline = time.monotonic() + 15.0
            while True:
                alive = []
                for client, _permit in prepared:
                    state, start = _process_identity(client.endpoint["pid"])
                    if state != "dead" and (start is None or start == client.process_start):
                        alive.append(client.client_id)
                if not alive:
                    return {"state": "completed", "committed_clients": committed}
                if cancel_wait is not None and cancel_wait.is_set():
                    return {"state": "wait_abandoned", "code": "exit_wait_abandoned",
                            "committed_clients": committed, "pending_clients": alive}
                if deadline and time.monotonic() >= deadline:
                    if on_progress:
                        on_progress({"state": "waiting_for_exit", "code": "client_exit_delayed",
                                     "committed_clients": committed, "pending_clients": alive})
                    deadline = 0.0  # Continue until exact exit or explicit abandon.
                time.sleep(0.1)
        except Exception as exc:
            return {"state": "failed", "code": getattr(exc, "code", "exit_failed"), "message": str(exc),
                    "committed_clients": committed, "partial_commit": bool(committed)}
        finally:
            for client, permit in reversed(prepared):
                if client.client_id in committed:
                    continue
                try:
                    self._call(client, "lifecycle_cancel", exit_id=exit_id,
                               client_generation=client.generation, permit_id=permit)
                except UiControlError:
                    pass
            if pending is not None:
                try:
                    self._call(pending, "lifecycle_cancel", exit_id=exit_id,
                               client_generation=pending.generation, permit_id="")
                except UiControlError:
                    pass
            with self._lock:
                self._exiting = False


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root")
    args = parser.parse_args(argv)
    root = Path(args.runtime_root).resolve() if args.runtime_root else assistant_runtime_root().resolve()
    try:
        with lifecycle_log(PROJECT_ROOT) as log:
            import faulthandler
            import traceback
            native_trace = os.name != "nt" or os.environ.get("COAT_NATIVE_TRACE") == "1"
            faulthandler.disable()
            if native_trace:
                try:
                    faulthandler.enable(file=log, all_threads=os.name != "nt")
                except (OSError, RuntimeError):
                    # Bounded streams deliberately expose no bypass file descriptor.
                    native_trace = False
            trace("assistant_process_start", role="assistant", version=BUILD_IDENTITY.version,
                  git_sha=BUILD_IDENTITY.git_sha, source_id=source_id(PROJECT_ROOT), protocol=ASSISTANT_PROTOCOL,
                  native_trace=native_trace)
            lock = HeldFileLock(root / "assistant.lock")
            if not lock.acquire():
                trace("assistant_lock_contended", reason="another_owner", exit_code=0)
                return 0
            code = 1
            try:
                from coat_sim.ui.assistant_process_host import run_assistant
                code = int(run_assistant(root) or 0)
                return code
            except BaseException as exc:
                code = exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 1
                trace("assistant_process_failed", error=exc, exit_code=code,
                      stack=[f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in traceback.extract_tb(exc.__traceback__)][-12:])
                return code
            finally:
                lock.close()
                trace("assistant_process_exit", exit_code=code)
    except OSError as exc:
        trace("assistant_process_bootstrap_failed", error=exc, exit_code=73)
        return 73


if __name__ == "__main__":
    raise SystemExit(main())
