"""Authenticated local UI control, with Qt imported only by the UI server.

The MCP client can import this module without importing desktop GUI modules.
Only the enumerated editor contracts below are exposed over the loopback socket.
"""

from __future__ import annotations

from copy import deepcopy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from coat_sim.control_errors import UiControlError as UiControlError
from coat_sim.core.design_files import default_design_path, design_extension, validate_design_path
from coat_sim.core.api import CoatService, ServiceError
from coat_sim.core.atomic_io import replace_with_retry
from coat_sim.ui.ready_channel import UiReadyChannel
from coat_sim.ui.presentation_scope import (GLOBAL_WINDOWS, legacy_window_scope_error,
                                            legacy_window_types, presentation_scope)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WINDOW_TYPES = ("launcher", "main", "process", "machine", "mask", "source", "substrate",
                "settings", "about", "film_design", "film_simulation")
OPERATIONS = frozenset({"list_windows", "open_window", "close_window", "read_window",
                        "update_window", "window_action"})
MAX_MESSAGE_BYTES = 2 * 1024 * 1024


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _action_breadcrumb(phase: str, value: dict[str, Any]) -> None:
    """Diagnostics must not own control-request completion or GUI availability."""
    try:
        print(f"UI action {phase}:", json.dumps(value, ensure_ascii=False), flush=True)
    except Exception:
        # A console/pipe may disappear while independent project windows stay
        # alive. Logging failure cannot replace the action's actual reply.
        pass


def _endpoint_dir(root: Path) -> Path:
    return root / "artifacts" / "runtime"


class UiControlClient:
    """Call a running launcher; existing instances remain discoverable by endpoint metadata."""

    def __init__(self, root: Path | str = PROJECT_ROOT, target_pid: int | None = None,
                 timeout: float = 22.0, *, operations=OPERATIONS, endpoint_prefix="ui",
                 initial_endpoint: dict[str, Any] | None = None):
        self.root = Path(root).resolve()
        self.operations = frozenset(operations)
        self.endpoint_prefix = endpoint_prefix
        selected = target_pid if target_pid is not None else os.environ.get("COAT_UI_PID")
        try:
            self.target_pid = int(selected) if selected is not None else None
        except (ValueError, TypeError) as exc:
            raise UiControlError("invalid_ui_pid", "COAT_UI_PID 须为有效进程标识") from exc
        if self.target_pid is not None and self.target_pid <= 0:
            raise UiControlError("invalid_ui_pid", "界面进程标识须为正整数")
        self._explicit_pid = selected is not None
        self.timeout = timeout
        self._initial_endpoint = initial_endpoint
        self._spawned = None
        self._startup_pending = False
        self._startup_error = None

    def _endpoints(self) -> list[dict[str, Any]]:
        if self._initial_endpoint is not None:
            if self._endpoint_is_valid(self._initial_endpoint, self.target_pid):
                return [dict(self._initial_endpoint)]
            return []
        directory = _endpoint_dir(self.root)
        paths = ([directory / f"{self.endpoint_prefix}-{self.target_pid}.json"] if self.target_pid is not None
                 else sorted(directory.glob(f"{self.endpoint_prefix}-*.json"), reverse=True))
        endpoints = []
        for path in paths:
            try:
                if path.stat().st_size > 8192:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                if not self._endpoint_is_valid(value, self.target_pid):
                    continue
                value["_mtime"] = path.stat().st_mtime
                endpoints.append(value)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(endpoints, key=lambda value: (value.get("role") != "launcher", -value["_mtime"]))

    def _endpoint_is_valid(self, value: Any, expected_pid: int | None = None) -> bool:
        if not isinstance(value, dict):
            return False
        try:
            return (value.get("protocol") == 1 and value.get("host") == "127.0.0.1"
                    and Path(value["root"]).resolve() == self.root
                    and type(value.get("port")) is int
                    and 1 <= value["port"] <= 65535
                    and type(value.get("pid")) is int
                    and (expected_pid is None or value["pid"] == expected_pid)
                    and isinstance(value.get("token"), str)
                    and len(value["token"]) >= 32)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _start_ui(self, arguments: dict[str, Any]) -> dict[str, Any]:
        key = arguments.get("window_type")
        if key not in WINDOW_TYPES:
            raise UiControlError("invalid_window", "未知窗口类型")
        bounded = CoatService(self.root)
        for name in ("design_path", "intended_path"):
            if arguments.get(name):
                arguments[name] = str(bounded._path(arguments[name]))
        # Route desktop starts through the branded-host bootstrap on Windows;
        # other platforms use the same entry without the host restart.
        command = [sys.executable, str(self.root / "launcher.pyw")]
        if key not in {"launcher", "settings", "about"}:
            command.extend(["--module", key])
        logs = self.root / "artifacts" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / f"mcp-ui-{secrets.token_hex(6)}.log"
        channel = UiReadyChannel()
        child_environment = os.environ.copy()
        child_environment.update(channel.child_environment())
        try:
            with log_path.open("ab") as stream:
                self._spawned = subprocess.Popen(command, cwd=str(self.root), stdout=stream,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    env=child_environment)
        except Exception:
            channel.close()
            raise
        self.target_pid = self._spawned.pid
        self._startup_pending = True

        def initialize():
            try:
                event = channel.wait(self._spawned)
                status = event.get("status")
                if status == "ready":
                    endpoint = event.get("endpoint")
                    endpoint_pid = event.get("handoff_host_pid", self.target_pid)
                    if not self._endpoint_is_valid(endpoint, endpoint_pid):
                        self._startup_error = "窗口已启动，但控制端点信息无效；请检查启动日志"
                    else:
                        self.target_pid = endpoint_pid
                        client = UiControlClient(self.root, self.target_pid,
                                                 initial_endpoint=endpoint)
                        try:
                            client.request("open_window", **arguments)
                        except UiControlError as exc:
                            self._startup_error = str(exc)
                elif status == "error":
                    self._startup_error = str(event.get("error") or "窗口启动失败；请检查启动日志")
                elif status == "process_exit":
                    self._startup_error = (
                        f"窗口进程在控制服务就绪前退出（代码 {event.get('returncode')}）；"
                        f"诊断日志：{log_path}")
                elif status == "timeout":
                    self._startup_error = f"窗口启动超时；诊断日志：{log_path}"
                    if self._spawned.poll() is None:
                        self._spawned.terminate()
                else:
                    self._startup_error = (
                        "窗口启动通知通道失败："
                        f"{event.get('error', '未知错误')}；诊断日志：{log_path}")
            except Exception as exc:
                self._startup_error = f"窗口启动握手失败：{exc}；诊断日志：{log_path}"
                channel.close()
            finally:
                self._startup_pending = False

        threading.Thread(target=initialize, name="mcp-open-window", daemon=True).start()
        return {"window_type": key, "pid": self.target_pid, "state": "loading",
                "pending_parameters": True}

    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        if operation not in self.operations:
            raise UiControlError("unsupported_operation", "不支持的界面操作")
        body = _json({"operation": operation, "arguments": arguments}).encode("utf-8")
        if len(body) > MAX_MESSAGE_BYTES:
            raise UiControlError("request_too_large", "界面请求过大")
        if self._startup_pending and operation in ("read_window", "open_window"):
            return {"window_type": arguments.get("window_type"), "pid": self.target_pid,
                    "state": "loading", "pending_parameters": True}
        endpoints = self._endpoints()
        if (operation == "open_window" and self.endpoint_prefix == "ui"
                and not self._explicit_pid and arguments.get("project_id") is None):
            if arguments.get("window_type") in GLOBAL_WINDOWS:
                endpoints = [row for row in endpoints if row.get("role") == "launcher"]
            else:
                raise UiControlError("project_identity_required",
                                     "跨进程工程窗口请求必须指定工程与会话身份；独立模块请使用明确 CLI 入口。")
        for endpoint in endpoints:
            request = Request(f"http://127.0.0.1:{endpoint['port']}/call", data=body,
                              headers={"Content-Type": "application/json",
                                       "X-Coat-Token": endpoint["token"]}, method="POST")
            try:
                # Local control must not use configured network proxies.
                with build_opener(ProxyHandler({})).open(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_MESSAGE_BYTES + 1)
                if len(raw) > MAX_MESSAGE_BYTES:
                    raise UiControlError("response_too_large", "界面回复过大")
                value = json.loads(raw)
            except HTTPError as exc:
                raise UiControlError("ui_authentication_failed", "界面控制认证失败") from exc
            except (URLError, OSError, TimeoutError) as exc:
                if self._startup_pending and operation in ("read_window", "open_window"):
                    return {"window_type": arguments.get("window_type"), "pid": self.target_pid,
                            "state": "loading", "pending_parameters": True}
                if self._explicit_pid or operation != "open_window":
                    raise UiControlError("ui_unavailable", "指定界面尚未就绪或已退出") from exc
                continue
            except (ValueError, TypeError) as exc:
                raise UiControlError("invalid_ui_response", "界面控制回复无效") from exc
            if not value.get("ok"):
                error = value.get("error", {})
                raise UiControlError(error.get("code", "ui_error"),
                                     error.get("message", "界面操作失败"))
            result = value["result"]
            if self.target_pid is None:
                self.target_pid = endpoint["pid"]
            if operation in ("read_window", "open_window"):
                if self._startup_pending:
                    result.update(state="loading", pending_parameters=True)
                if self._startup_error:
                    result["initialization_error"] = self._startup_error
            return result
        if self._startup_pending and operation in ("read_window", "open_window"):
            return {"window_type": arguments.get("window_type"), "pid": self.target_pid,
                    "state": "loading", "pending_parameters": True}
        if operation == "open_window" and self.endpoint_prefix == "ui" and not self._explicit_pid:
            self._startup_error = None
            return self._start_ui(arguments)
        if self._startup_error:
            raise UiControlError("ui_start_failed", self._startup_error)
        raise UiControlError("ui_unavailable", "指定界面尚未就绪或已退出，请先打开窗口")


class UiControlServer:
    """Dispatch authenticated socket requests on the owning Qt GUI thread."""

    def __init__(self, dispatch: Callable[[str, dict[str, Any]], dict[str, Any]], *,
                 root: Path | str = PROJECT_ROOT, role: str = "launcher",
                 window_type: str | None = None, parent_pid: int | None = None,
                 operations=OPERATIONS, endpoint_prefix="ui"):
        from PySide6.QtCore import QObject, Qt, Signal, Slot

        class Relay(QObject):
            requested = Signal(object)

            def __init__(self):
                super().__init__()
                self.requested.connect(self.receive, Qt.QueuedConnection)

            @Slot(object)
            def receive(self, envelope):
                if envelope.get("expired"):
                    envelope["done"].set()
                    return
                started = time.monotonic()
                operation = str(envelope.get("operation", ""))
                arguments = envelope.get("arguments", {})
                # No paths, values, user text, credentials or tool payloads.
                breadcrumb = {"operation": operation,
                              "window_type": str(arguments.get("window_type", "")),
                              "action": str(arguments.get("action", ""))}
                trace_action = operation not in ("read_window", "list_windows")
                if trace_action:
                    _action_breadcrumb("begin", breadcrumb)
                try:
                    envelope["reply"] = {"ok": True, "result": dispatch(
                        envelope["operation"], envelope["arguments"])}
                except (UiControlError, ServiceError) as exc:
                    envelope["reply"] = {"ok": False, "error": {
                        "code": exc.code, "message": str(exc)}}
                except Exception as exc:
                    envelope["reply"] = {"ok": False, "error": {
                        "code": "ui_error", "message": str(exc)}}
                finally:
                    try:
                        if trace_action:
                            _action_breadcrumb("end", {**breadcrumb,
                                "ok": bool(envelope.get("reply", {}).get("ok")),
                                "elapsed_s": round(time.monotonic() - started, 3)})
                    finally:
                        envelope["done"].set()

        self.root = Path(root).resolve()
        self.operations = frozenset(operations)
        self.role, self.window_type, self.parent_pid = role, window_type, parent_pid
        self._relay = Relay()
        self._token = secrets.token_urlsafe(32)
        self._http: ThreadingHTTPServer | None = None
        self._path = _endpoint_dir(self.root) / f"{endpoint_prefix}-{os.getpid()}.json"
        self.endpoint: dict[str, Any] | None = None

    def start(self) -> dict[str, Any]:
        if self._http is not None:
            return dict(self.endpoint or {})
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                pass

            def do_POST(self):
                if (self.path != "/call" or not hmac.compare_digest(
                        self.headers.get("X-Coat-Token", ""), owner._token)):
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_MESSAGE_BYTES:
                        raise UiControlError("request_too_large", "界面请求大小无效")
                    self.connection.settimeout(5)
                    request = json.loads(self.rfile.read(size))
                    operation, arguments = request["operation"], request.get("arguments", {})
                    if operation not in owner.operations or not isinstance(arguments, dict):
                        raise UiControlError("invalid_request", "界面操作或参数无效")
                    envelope = {"operation": operation, "arguments": arguments,
                                "done": threading.Event(), "expired": False}
                    owner._relay.requested.emit(envelope)
                    if not envelope["done"].wait(20):
                        envelope["expired"] = True
                        raise UiControlError("ui_timeout", "界面操作超时，请读取窗口确认状态")
                    reply = envelope["reply"]
                except Exception as exc:
                    reply = {"ok": False, "error": {
                        "code": getattr(exc, "code", "invalid_request"), "message": str(exc)}}
                data = _json(reply).encode("utf-8")
                if len(data) > MAX_MESSAGE_BYTES:
                    data = _json({"ok": False, "error": {
                        "code": "response_too_large", "message": "界面回复过大"}}).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._http.daemon_threads = True
        self._http.block_on_close = False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        endpoint = {"protocol": 1, "host": "127.0.0.1", "port": self._http.server_port,
                    "token": self._token, "pid": os.getpid(), "root": str(self.root),
                    "role": self.role, "window_type": self.window_type,
                    "parent_pid": self.parent_pid}
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(_json(endpoint), encoding="utf-8")
        replace_with_retry(temporary, self._path)
        threading.Thread(target=self._http.serve_forever, kwargs={"poll_interval": 0.1},
                         name="coat-ui-control", daemon=True).start()
        self.endpoint = endpoint
        return dict(endpoint)

    def close(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
        self._path.unlink(missing_ok=True)
        self.endpoint = None


_ACTIONS = {
    "launcher": (),
    "main": ("calculate", "cancel", "clear_process", "refresh_estimate", "export_pdf"),
    "source": ("new", "save"),
    "substrate": ("new", "save", "select_point", "add_point", "update_point",
                  "delete_point", "close_outline"),
    "process": ("new", "save", "add_step", "delete_step", "move_step", "set_step", "set_step_mask", "refresh_preview"),
    "machine": ("new", "save", "append_item", "remove_item", "set_dependency"),
    "mask": ("new", "save", "append_item", "remove_item", "load_machine", "close_polygon",
             "transform_mask", "refresh_rings", "compute_weights", "compute_field", "compute_point"),
    "settings": ("save", "restore_defaults", "discover_models"),
    "about": (),
    "film_design": ("new", "save", "add_layer", "delete_layer", "move_layer", "expand_expression", "send_to_simulation"),
    "film_simulation": ("calculate", "export_csv", "export_png"),
}



_QML_CONTROLS = {
    "mask": {
        "tool_index": ("currentIndex", "activated"),
        "shift_x_mm": ("text", None), "shift_y_mm": ("text", None),
        "scale_x": ("text", None), "scale_y": ("text", None),
        "mask_slot": ("value", None), "source_slot": ("value", None),
        "angular_radius_mm": ("value", "moved"), "patch_radius_mm": ("text", None),
        "auto_weight_y": ("checked", None), "weight_y_min": ("text", "textEdited"),
        "weight_y_max": ("text", "textEdited"), "heat_weight_mode": ("currentIndex", None),
    },
    "substrate": {
        "tool_index": ("currentIndex", None), "selected_point_index": ("currentIndex", "activated"),
        "point_x_mm": ("text", None), "point_y_mm": ("text", None),
    },
}

_ACTION_ARGUMENTS = {
    "save": ["path (optional for an existing draft path)", "overwrite (required, boolean)"],
    "new": ["force (boolean)", "data (required for machine/mask)", "intended_path (optional)"],
    "export_pdf": ["path", "overwrite (required, boolean)"],
    "select_point": ["index"], "add_point": ["x_mm", "y_mm"],
    "update_point": ["index", "x_mm", "y_mm"], "delete_point": ["index"],
    "delete_step": ["index"], "move_step": ["index", "delta"],
    "set_step": ["index", "key", "value"], "append_item": ["pointer", "value"],
    "remove_item": ["pointer", "index"], "set_dependency": ["pointer", "path"],
    "load_machine": ["path"], "close_polygon": ["hole_index (default -1)"],
    "transform_mask": ["dx_mm (default 0)", "dy_mm (default 0)", "sx (default 1)", "sy (default 1)"],
    "set_step_mask": ["index", "slot (0-based)", "enabled (boolean)"],
    "compute_weights": ["mask_slot (default current control)", "source_slot (default current control)"],
    "compute_field": ["mask_slot (default current control)", "source_slot (default current control)",
                      "ring_radii_mm (default current angular radius)"],
    "compute_point": ["x_mm", "y_mm", "patch_radius_mm (default current control)",
                      "source_slot (default current control)"],
}

def _window_contract(kind: str) -> dict[str, Any]:
    return {"actions": list(_ACTIONS[kind]),
            "action_arguments": {name: _ACTION_ARGUMENTS.get(name, []) for name in _ACTIONS[kind]},
            "qml_controls": {key: {"property": spec[0], "signal": spec[1]}
                             for key, spec in _QML_CONTROLS.get(kind, {}).items()},
            "document_editing": kind in ("source", "substrate", "machine", "mask", "process", "film_design", "film_simulation"),
            "json_pointer_editing": kind in ("machine", "mask")}

def _document_pointers(value: Any, prefix: str = "") -> list[str]:
    paths = []
    if isinstance(value, dict):
        for key, child in value.items():
            pointer = prefix + "/" + str(key).replace("~", "~0").replace("/", "~1")
            paths.append(pointer)
            paths.extend(_document_pointers(child, pointer))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            pointer = prefix + "/" + str(index)
            paths.append(pointer)
            paths.extend(_document_pointers(child, pointer))
    return paths


class WindowAdapter:
    """Explicit editor operations; no arbitrary object/method access from clients."""

    def __init__(self, window_type: str, window: Any, bridge: Any,
                 root: Path | str = PROJECT_ROOT):
        if window_type not in WINDOW_TYPES:
            raise UiControlError("invalid_window", "未知窗口类型")
        self.kind, self.window, self.bridge = window_type, window, bridge
        self.service = CoatService(root)

    def _validated_dependency(self, kind: str, path: str) -> None:
        if not path:
            raise UiControlError("dependency_error", "请先加载依赖设计文件")
        document = self.service.load(kind, path)
        if not document.valid:
            raise UiControlError("dependency_error", "依赖无效：" + "；".join(document.errors))

    def snapshot(self) -> dict[str, Any]:
        bridge, kind = self.bridge, self.kind
        value = {"window_type": kind, "pid": os.getpid(), "state": "ready",
                 "visible": bool(self.window.isVisible()), "actions": list(_ACTIONS[kind])}
        if kind == "launcher":
            value.update(modules=bridge.modules, loading_windows=bridge.loadingKeys)
        if kind in ("source", "substrate", "main"):
            value.update(fields=bridge.fields, status=bridge.status)
        if kind in ("source", "substrate", "process"):
            value.update(path=bridge.currentPath, dirty=bridge.dirty, valid=bridge.valid)
        if kind in ("source", "substrate"):
            value["error"] = bridge.error
            try:
                value["document"] = bridge.documentData()
            except (ValueError, TypeError, KeyError) as exc:
                value["document"] = None
                value["error"] = str(exc)
        if kind == "substrate":
            value.update(points=bridge.points, closed=bridge.closed,
                         selected_index=bridge.selectedIndex)
        elif kind == "process":
            value.update(document=bridge.document, fields={key: bridge.document[key]
                         for key in ("name", "machine_file", "substrate_file")},
                         errors=bridge.errors, status=bridge.status,
                         busy=bool(bridge.optimization.get("busy")), optimization=bridge.optimization)
        elif kind in ("machine", "mask"):
            value.update(document=json.loads(bridge.documentJson), path=bridge.documentPath,
                         dirty=bridge.dirty, valid=bridge.canSave,
                         error=bridge.validationMessage, status=bridge.status)
            if kind == "mask":
                value.update(machine_path=bridge.machinePath,
                             workpiece_diameter_mm=bridge.workpieceDiameterMm,
                             busy=bridge.radialBusy or bridge.fieldBusy or bridge.pointBusy or bridge.cadBusy,
                             cad_busy=bridge.cadBusy)
        elif kind == "main":
            value.update(paths=bridge.paths, backend=bridge.device, busy=bridge.busy,
                         fast_cycle=bridge.fastCycle,
                         fast_cycle_residual_percent=bridge.fastThresholdPercent,
                         has_result=bridge.hasResult, metrics=bridge.metrics)
        elif kind == "settings":
            value["fields"] = {
                "font_point_size": bridge.fontPointSize, "dpi_scale_percent": bridge.dpiScalePercent,
                "assistant_enabled": bridge.assistantEnabled, "assistant_api_url": bridge.assistantApiUrl,
                "assistant_model": bridge.assistantModel,
                "assistant_task_mode": bridge.assistantTaskMode,
                "assistant_reasoning_strength": bridge.assistantReasoningStrength,
                "daily_token_limit": bridge.assistantDailyTokenLimit,
                "period_token_limit": bridge.assistantPeriodTokenLimit,
                "period_minutes": bridge.assistantPeriodMinutes,
                "icon_size": bridge.assistantIconSize}
            value.update(available_models=bridge.availableModels,
                         has_api_key=bridge.hasAssistantApiKey, status=bridge.statusMessage)
        value.update(_window_contract(kind))
        value["controls"] = self._control_values()
        fields = set(value.get("fields", {})) | set(_QML_CONTROLS.get(kind, {}))
        if kind == "main":
            fields.update(("backend", "fast_cycle", "fast_cycle_residual_percent"))
        elif kind == "mask":
            fields.add("workpiece_diameter_mm")
        value["editable_fields"] = sorted(fields)
        if kind in ("machine", "mask"):
            value["editable_pointers"] = _document_pointers(value.get("document", {}))
        return value

    def _control_values(self) -> dict[str, Any]:
        from PySide6.QtCore import QObject
        values = {}
        for key, (prop, _signal) in _QML_CONTROLS.get(self.kind, {}).items():
            control = self.window.findChild(QObject, "coatcontrol:" + key)
            if control is None:
                continue
            entry = {"value": control.property(prop), "enabled": bool(control.property("enabled")),
                     "property": prop}
            if prop == "currentIndex":
                entry["count"] = int(control.property("count") or 0)
            elif prop == "value":
                entry.update(minimum=control.property("from"), maximum=control.property("to"))
            values[key] = entry
        return values

    def _set_controls(self, fields: dict[str, Any]) -> None:
        from PySide6.QtCore import QObject, QMetaObject, Qt, Q_ARG
        # Mode/selection changes can initialize dependent text fields, so apply
        # those before explicit coordinate/range values.
        if self.kind == "mask" and "angular_radius_mm" in fields and self.bridge.machinePath:
            self._validated_dependency("machine", self.bridge.machinePath)
        ordered = sorted(fields, key=lambda key: key not in ("auto_weight_y", "selected_point_index"))
        for key in ordered:
            prop, signal = _QML_CONTROLS[self.kind][key]
            control = self.window.findChild(QObject, "coatcontrol:" + key)
            if control is None:
                raise UiControlError("control_unavailable", f"控件尚未就绪：{key}")
            value = fields[key]
            if prop == "text":
                value = str(value)
            elif prop == "checked":
                if type(value) is not bool:
                    raise UiControlError("invalid_field", f"{key} 须为布尔值")
            elif prop == "currentIndex":
                count = int(control.property("count") or 0)
                lower = -1 if key == "selected_point_index" else 0
                if type(value) is not int or not lower <= value < count:
                    raise UiControlError("invalid_field", f"{key} 须为有效选项索引")
            else:
                if isinstance(value, bool):
                    raise UiControlError("invalid_field", f"{key} 须为数值")
                value = float(value)
                if not math.isfinite(value) or not float(control.property("from")) <= value <= float(control.property("to")):
                    raise UiControlError("invalid_field", f"{key} 超出控件允许范围")
                if key in ("mask_slot", "source_slot"):
                    if not value.is_integer():
                        raise UiControlError("invalid_field", f"{key} 须为整数")
                    value = int(value)
            if not control.setProperty(prop, value):
                raise UiControlError("control_error", f"无法修改控件：{key}")
            if signal == "activated":
                success = QMetaObject.invokeMethod(control, "activated", Qt.DirectConnection, Q_ARG(int, value))
            elif signal:
                success = QMetaObject.invokeMethod(control, signal, Qt.DirectConnection)
            else:
                success = True  # Existing property-change handlers run automatically.
            if not success:
                raise UiControlError("control_error", f"控件语义事件未能执行：{key}")

    def _owner_path(self) -> str:
        state = self.snapshot()
        return str(self.service._path(state.get("path") or f"new_{self.kind}{design_extension(self.kind)}"))

    def _guard_document(self, data: dict[str, Any], owner: str) -> None:
        if self.kind == "machine":
            for item in data.get("sources", []) + data.get("masks", []):
                if item.get("file"):
                    self.service.resolve_reference(owner, item["file"])
        elif self.kind == "process":
            for key in ("machine_file", "substrate_file"):
                if data.get(key):
                    self.service.resolve_reference(owner, data[key])

    def update(self, *, fields: dict[str, Any] | None = None,
               design_path: str | None = None, data: dict[str, Any] | None = None,
               intended_path: str | None = None, pointers: dict[str, Any] | None = None,
               force: bool = False) -> dict[str, Any]:
        from PySide6.QtCore import QUrl

        if type(force) is not bool:
            raise UiControlError("invalid_force", "force 须为布尔值")
        if design_path is not None and data is not None:
            raise UiControlError("invalid_request", "design_path 与 data 只能指定一项")
        bridge, kind = self.bridge, self.kind
        before = self.snapshot()
        if before.get("dirty") and (design_path is not None or data is not None) and not force:
            raise UiControlError("unsaved_changes", "窗口有未保存修改；替换文档须明确 force=true")
        if design_path is not None:
            path = str(self.service._path(design_path))
            file_kind = "process" if kind == "main" else kind
            document = self.service.load(file_kind, path)
            if not document.valid:
                raise UiControlError("dependency_error", "加载方案的依赖无效：" + "；".join(document.errors))
            if kind == "main":
                bridge.chooseFile("process", QUrl.fromLocalFile(path))
            elif kind in ("machine", "mask"):
                bridge.loadDocument(path)
            else:
                bridge.openPath(QUrl.fromLocalFile(path))
        if data is not None:
            if kind not in ("source", "substrate", "process", "machine", "mask"):
                raise UiControlError("unsupported_operation", "此窗口不接受设计文档")
            owner = str(self.service._path(intended_path)) if intended_path else self._owner_path()
            self._guard_document(data, owner)
            result = self.service.validate(kind, data, path=owner)
            if not result.valid or result.data is None:
                raise UiControlError("invalid_design", "；".join(result.errors))
            bridge.applyDocument(result.data, owner)
        if pointers:
            if kind not in ("machine", "mask"):
                raise UiControlError("unsupported_operation", "JSON pointer 修改仅用于机器和修正板")
            from coat_sim.ui.machine_mask import _parent_at_pointer
            prospective = deepcopy(json.loads(bridge.documentJson))
            for pointer, value in pointers.items():
                parent, key = _parent_at_pointer(prospective, pointer)
                if isinstance(parent, list):
                    parent[int(key)] = value
                else:
                    if key not in parent:
                        raise UiControlError("invalid_field", "只能修改已有文档字段")
                    parent[key] = value
            self._guard_document(prospective, self._owner_path())
            for pointer, value in pointers.items():
                bridge.setValue(pointer, _json(value))
        if fields:
            qml_fields = {key: value for key, value in fields.items()
                          if key in _QML_CONTROLS.get(kind, {})}
            model_fields = {key: value for key, value in fields.items() if key not in qml_fields}
            if model_fields:
                self._set_fields(model_fields)
            if qml_fields:
                self._set_controls(qml_fields)
        return self.snapshot()

    def _set_fields(self, fields: dict[str, Any]) -> None:
        bridge, kind = self.bridge, self.kind
        if kind == "main" and bridge.paths.get("process"):
            self._validated_dependency("process", bridge.paths["process"])
        if kind in ("source", "substrate", "main", "process"):
            allowed = (set(bridge.fields) if kind != "process"
                       else {"name", "machine_file", "substrate_file"})
            special = {"backend", "fast_cycle", "fast_cycle_residual_percent"} if kind == "main" else set()
            if set(fields) - allowed - special:
                raise UiControlError("invalid_field", "字段不在此窗口的可编辑控件中")
            if kind == "process":
                candidate = deepcopy(bridge.document)
                candidate.update(fields)
                self._guard_document(candidate, self._owner_path())
            if kind == "main" and fields.get("backend", bridge.device) not in ("自动", "CPU", "GPU", "DirectML"):
                raise UiControlError("invalid_backend", "计算设备须为 自动/CPU/GPU/DirectML")
            for key, value in fields.items():
                if kind == "main" and key == "backend":
                    bridge.setDevice(str(value))
                elif kind == "main" and key == "fast_cycle":
                    if type(value) is not bool:
                        raise UiControlError("invalid_field", "fast_cycle 须为布尔值")
                    bridge.setFastCycle(value)
                elif kind == "main" and key == "fast_cycle_residual_percent":
                    bridge.setFastThreshold(str(value))
                else:
                    bridge.setField(key, str(value))
        elif kind == "mask":
            if set(fields) != {"workpiece_diameter_mm"}:
                raise UiControlError("invalid_field", "修正板字段仅接受 workpiece_diameter_mm；文档使用 pointers")
            diameter = float(fields["workpiece_diameter_mm"])
            if not math.isfinite(diameter) or not 0 < diameter <= 20000:
                raise UiControlError("invalid_field", "工件盘直径须在 0–20000 mm 之间")
            bridge.setWorkpieceDiameter(diameter)
        elif kind == "settings":
            current = self.snapshot()["fields"]
            if set(fields) - set(current):
                raise UiControlError("invalid_field", "未知设置字段；凭证不通过此接口读取或写入")
            current.update(fields)
            if type(current["assistant_enabled"]) is not bool:
                raise UiControlError("invalid_field", "assistant_enabled 须为布尔值")
            bridge.setFontPointSize(int(current["font_point_size"]))
            bridge.setDpiScalePercent(int(current["dpi_scale_percent"]))
            bridge.setAssistantConfig(bool(current["assistant_enabled"]), str(current["assistant_api_url"]),
                                      str(current["assistant_model"]))
            bridge.setAssistantTokenLimits(int(current["daily_token_limit"]),
                                           int(current["period_token_limit"]), int(current["period_minutes"]))
            bridge.setAssistantIconSize(int(current["icon_size"]))
            bridge.setAssistantTaskOptions(str(current["assistant_task_mode"]), str(current["assistant_reasoning_strength"]))
        else:
            raise UiControlError("unsupported_operation", "此窗口没有可修改的输入框")

    def action(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        from PySide6.QtCore import QUrl

        args, kind, bridge = arguments or {}, self.kind, self.bridge
        if "force" in args and type(args["force"]) is not bool:
            raise UiControlError("invalid_force", "force 须为布尔值")
        if name not in _ACTIONS[kind]:
            raise UiControlError("unsupported_action", "此窗口不支持该动作")
        if name == "save" and kind != "settings":
            state = self.snapshot()
            target = default_design_path(kind, self.service._path(args.get("path") or state.get("path") or ""))
            if "overwrite" not in args or type(args["overwrite"]) is not bool:
                raise UiControlError("overwrite_required", "保存须显式指定 overwrite 布尔值")
            data = state.get("document")
            if data is None:
                raise UiControlError("invalid_design", "当前文档无效")
            # Rebase dependencies before a Save As, using the restricted service.
            old_owner = self._owner_path()
            data = deepcopy(data)
            references = ([(item, "file") for item in data.get("sources", []) + data.get("masks", [])]
                          if kind == "machine" else [(data, key) for key in ("machine_file", "substrate_file")]
                          if kind == "process" else [])
            for item, key in references:
                dependency = self.service.resolve_reference(old_owner, item[key])
                item[key] = self.service.relative_reference(target, dependency)
            saved = self.service.save(kind, target, data, overwrite=args["overwrite"])
            self.update(design_path=saved.path, force=True)
        elif name == "new":
            if self.snapshot().get("dirty") and not args.get("force", False):
                raise UiControlError("unsaved_changes", "新建前有未保存修改；须明确 force=true")
            if kind in ("machine", "mask"):
                if "data" not in args:
                    raise UiControlError("invalid_request", "机器/修正板新建须提供设计 data")
                return self.update(data=args["data"], intended_path=args.get("intended_path"), force=True)
            bridge.newDocument()
        elif name == "calculate":
            path = bridge.paths.get("process")
            if not path:
                raise UiControlError("invalid_design", "请先加载工艺文件")
            document = self.service.load("process", path)
            if not document.valid:
                raise UiControlError("dependency_error", "工艺依赖无效")
            bridge.calculate()
        elif name == "cancel":
            bridge.cancel()
        elif name == "clear_process":
            bridge.clearFile("process")
        elif name == "refresh_estimate":
            if bridge.paths.get("process"):
                self._validated_dependency("process", bridge.paths["process"])
            bridge.refreshEstimate()
        elif name == "export_pdf":
            path = self.service._path(args["path"])
            if "overwrite" not in args or type(args["overwrite"]) is not bool:
                raise UiControlError("overwrite_required", "导出须显式指定 overwrite 布尔值")
            if path.exists() and not args["overwrite"]:
                raise UiControlError("already_exists", "PDF 已存在")
            if path.suffix.lower() != ".pdf":
                raise UiControlError("invalid_path", "报告须使用 .pdf 扩展名")
            bridge.exportPdf(QUrl.fromLocalFile(str(path)))
        elif name == "select_point":
            bridge.selectPoint(int(args["index"]))
        elif name == "add_point":
            bridge.addPoint(str(args["x_mm"]), str(args["y_mm"]))
        elif name == "update_point":
            bridge.selectPoint(int(args["index"]))
            bridge.updateSelectedPoint(str(args["x_mm"]), str(args["y_mm"]))
        elif name == "delete_point":
            bridge.selectPoint(int(args["index"]))
            bridge.deleteSelectedPoint()
        elif name == "close_outline":
            bridge.closeOutline()
        elif name == "add_step":
            bridge.addStep()
        elif name == "delete_step":
            bridge.deleteStep(int(args["index"]))
        elif name == "move_step":
            bridge.moveStep(int(args["index"]), int(args["delta"]))
        elif name == "set_step":
            bridge.setStep(int(args["index"]), str(args["key"]), str(args["value"]))
        elif name == "set_step_mask":
            if type(args.get("enabled")) is not bool:
                raise UiControlError("invalid_field", "enabled 须为布尔值")
            bridge.setStepMask(int(args["index"]), int(args["slot"]), args["enabled"])
        elif name == "refresh_preview":
            self._guard_document(bridge.document, self._owner_path())
            machine = self.service.resolve_reference(self._owner_path(), bridge.document["machine_file"])
            self._validated_dependency("machine", str(machine))
            bridge.refreshSourcePreview()
        elif name == "append_item":
            allowed = {"/sources", "/masks", "/monitors"} if kind == "machine" else {"/outer", "/holes"}
            pointer = args["pointer"]
            if pointer not in allowed:
                raise UiControlError("invalid_field", "不支持此列表字段")
            item = args["value"]
            if kind == "machine" and pointer in ("/sources", "/masks"):
                self.service.resolve_reference(self._owner_path(), item["file"])
            bridge.appendItem(pointer, _json(item))
        elif name == "remove_item":
            allowed = {"/sources", "/masks", "/monitors"} if kind == "machine" else {"/outer", "/holes"}
            if args["pointer"] not in allowed:
                raise UiControlError("invalid_field", "不支持此列表字段")
            bridge.removeItem(args["pointer"], int(args["index"]))
        elif name == "set_dependency":
            import re
            pointer = args["pointer"]
            if not re.fullmatch(r"/(sources|masks)/[0-9]+/file", pointer):
                raise UiControlError("invalid_field", "依赖字段须指向源或修正板的 file")
            bridge.setDependencyFile(pointer, str(self.service._path(args["path"])))
        elif name == "load_machine":
            path = self.service._path(args["path"])
            document = self.service.load("machine", path)
            if not document.valid:
                raise UiControlError("dependency_error", "机器依赖无效")
            bridge.loadMachine(str(path))
        elif name == "close_polygon":
            bridge.closePolygon(int(args.get("hole_index", -1)))
        elif name == "transform_mask":
            bridge.transformMask(float(args.get("dx_mm", 0)), float(args.get("dy_mm", 0)),
                                 float(args.get("sx", 1)), float(args.get("sy", 1)))
        elif name == "refresh_rings":
            if bridge.machinePath:
                self._validated_dependency("machine", bridge.machinePath)
            bridge.refreshRings()
        elif name in ("compute_weights", "compute_field", "compute_point"):
            self._validated_dependency("machine", bridge.machinePath)
            controls = self._control_values()
            source_slot = int(args.get("source_slot", controls["source_slot"]["value"]))
            mask_slot = int(args.get("mask_slot", controls["mask_slot"]["value"]))
            if name == "compute_weights":
                bridge.computeWeights(mask_slot, source_slot)
            elif name == "compute_field":
                radii = args.get("ring_radii_mm", [float(controls["angular_radius_mm"]["value"])])
                bridge.computeField(mask_slot, source_slot, _json(radii))
            else:
                patch = float(args.get("patch_radius_mm", controls["patch_radius_mm"]["value"]))
                bridge.computePointWeights(float(args["x_mm"]), float(args["y_mm"]), patch, source_slot)
        elif name == "save":
            bridge.save()
        elif name == "restore_defaults":
            bridge.restoreDefaults()
        elif name == "discover_models":
            bridge.discoverModels()

        return self.snapshot()


class QuickFilmWindowAdapter:
    """Explicit public Quick-controller contracts; no Widgets/private fields."""

    DESIGN_FIELDS = frozenset({"name", "note", "reference_wavelength_nm", "assume_lossless", "use_k"})
    SIMULATION_FIELDS = frozenset({
        "wavelength_min_nm", "wavelength_max_nm", "samples", "step_nm",
        "angle_deg", "polarization", "first_surface", "extrapolation_policy",
    })

    def __init__(self, window_type: str, window: Any, bridge: Any,
                 root: Path | str = PROJECT_ROOT):
        self.kind, self.window, self.bridge = window_type, window, bridge
        self.service = CoatService(root)

    def snapshot(self) -> dict[str, Any]:
        bridge = self.bridge
        value = {"window_type": self.kind, "pid": os.getpid(), "state": "ready",
                 "visible": bool(self.window.isVisible()), **_window_contract(self.kind)}
        if self.kind == "film_design":
            state = deepcopy(bridge.state)
            document = state.get("design", {})
            initialization = bridge.initializationState
            pending = bool(bridge.session is not None
                           and bridge.session.snapshot().pending_confirmation)
            value.update(document=document, path=state.get("path", ""),
                         dirty=bool(state.get("dirty")), error=state.get("error", ""),
                         status=state.get("error") or state.get("message", ""),
                         busy=bool(bridge.expressionBusy),
                         fields={key: deepcopy(document.get(key)) for key in self.DESIGN_FIELDS},
                         initialization_state=initialization,
                         pending_confirmation=pending,
                         actions=["new", "save"])
            if initialization != "ready":
                value["state"] = "loading" if initialization == "loading" else "failed"
                value["initialization_error"] = bridge.initializationError
        else:
            value.update(document=json.loads(bridge.designJson), path=bridge.designPath,
                         dirty=bool(getattr(bridge, "documentDirty", False)),
                         fields=deepcopy(bridge.settings), busy=bool(bridge.busy),
                         status=bridge.status, error=bridge.validationError,
                         has_result=bool(bridge.hasResult), result_stale=bool(bridge.stale),
                         pending_confirmation=bool(bridge.hasPendingOpen),
                         simulation_configuration=deepcopy(bridge.exportSimulationConfiguration()),
                         actions=["calculate", "export_csv", "export_png"])
        value["editable_fields"] = sorted(self.DESIGN_FIELDS if self.kind == "film_design"
                                          else self.SIMULATION_FIELDS)
        value["action_arguments"] = {
            "new": ["force"], "save": ["path", "overwrite"],
            "calculate": [], "export_csv": ["path", "overwrite"],
            "export_png": ["path", "overwrite"],
        }
        value["action_arguments"] = {key: value["action_arguments"][key]
                                     for key in value["actions"]}
        return value

    def update(self, *, fields=None, design_path=None, data=None,
               intended_path=None, pointers=None, force=False) -> dict[str, Any]:
        if type(force) is not bool:
            raise UiControlError("invalid_force", "force 须为布尔值")
        if design_path is not None and data is not None:
            raise UiControlError("invalid_request", "design_path 与 data 只能指定一项")
        if pointers or intended_path is not None:
            raise UiControlError("unsupported_operation", "Quick 膜系不支持 pointer 操作，也不能通过路径指定当前内容")
        allowed = self.DESIGN_FIELDS if self.kind == "film_design" else self.SIMULATION_FIELDS
        if fields is not None and (not isinstance(fields, dict) or set(fields) - allowed):
            raise UiControlError("invalid_field", "未知 Quick 膜系字段")
        before = self.snapshot()
        if before["state"] != "ready":
            code = "ui_unavailable" if before["state"] == "loading" else "window_error"
            raise UiControlError(code, "膜系窗口尚未就绪")
        if before.get("pending_confirmation"):
            raise UiControlError("pending_confirmation", "请先处理窗口内待确认的打开操作")
        replacing = design_path is not None or data is not None
        if before.get("dirty") and replacing and not force:
            raise UiControlError("unsaved_changes", "窗口有未保存修改；替换文档须明确 force=true")
        if before.get("busy"):
            raise UiControlError("window_busy", "窗口正在处理任务；请等待完成或取消")
        if replacing and fields:
            raise UiControlError("invalid_request", "替换文档与字段编辑须分开请求，以保留确认边界")
        if design_path is not None:
            path = str(self.service._path(design_path))
            if not self.bridge.reuseWindowOpen(path, None):
                raise UiControlError("invalid_design", "窗口未接受该膜系文件，当前内容已保留")
        elif data is not None:
            normalized = self.bridge.service.validate(deepcopy(data))
            if not self.bridge.reuseWindowOpen("", normalized):
                raise UiControlError("invalid_design", "窗口未接受该膜系数据，当前内容已保留")
        if fields:
            for key, value in fields.items():
                if self.kind == "film_design":
                    if not self.bridge.setDesignValue(key, value):
                        raise UiControlError("invalid_field", self.bridge.error or "膜系字段无效")
                else:
                    self.bridge.setParameter(key, value)
        return self.snapshot()

    def action(self, name: str, arguments=None) -> dict[str, Any]:
        args = arguments or {}
        if not isinstance(args, dict):
            raise UiControlError("invalid_arguments", "操作参数须为对象")
        state = self.snapshot()
        if name not in state["actions"]:
            raise UiControlError("unsupported_operation", "Quick 膜系不支持此控制动作")
        allowed = {"new": {"force"}, "save": {"path", "overwrite"},
                   "calculate": set(), "export_csv": {"path", "overwrite"},
                   "export_png": {"path", "overwrite"}}[name]
        if set(args) - allowed:
            raise UiControlError("invalid_arguments", "未知操作参数")
        if name == "calculate":
            raise UiControlError("compute_confirmation_required", "计算须走估算与计算确认入口")
        if state["state"] != "ready" or state.get("pending_confirmation") or state.get("busy"):
            raise UiControlError("ui_unavailable", "请等待窗口就绪并处理待确认操作")
        if name == "new":
            if "force" in args and type(args["force"]) is not bool:
                raise UiControlError("invalid_force", "force 须为布尔值")
            if state.get("dirty") and not args.get("force", False):
                raise UiControlError("unsaved_changes", "新建须明确 force=true")
            self.bridge.requestNewDesign()
        else:
            if type(args.get("overwrite")) is not bool:
                raise UiControlError("overwrite_required", "保存或导出须显式指定 overwrite 布尔值")
            path_value = args.get("path") or (state.get("path") if name == "save" else None)
            if not path_value:
                raise UiControlError("invalid_path", "请提供保存或导出路径")
            path = self.service._path(path_value)
            if path.exists() and not args["overwrite"]:
                raise UiControlError("already_exists", "文件已存在")
            if name == "save":
                if not self.bridge.saveFilePath(str(path), args["overwrite"]):
                    raise UiControlError("save_failed", self.bridge.error or "保存失败")
            else:
                if path.suffix.lower() != (".csv" if name == "export_csv" else ".png"):
                    raise UiControlError("invalid_path", "导出文件扩展名无效")
                if not self.bridge.canExport:
                    raise UiControlError("stale_result", "请先完成当前参数计算后再导出")
                if name == "export_csv":
                    self.bridge.exportCsvToPath(str(path))
                else:
                    self.bridge.exportPngToPath(str(path))
                if self.bridge.validationError:
                    raise UiControlError("export_failed", self.bridge.validationError)
        return self.snapshot()


class WindowRegistry:
    """Expose the launcher's in-process top-level windows to the control API."""

    def __init__(self, manager: Any, *, root: Path | str = PROJECT_ROOT,
                 direct_key: str | None = None):
        from PySide6.QtCore import QTimer
        self.manager, self.root, self.direct_key = manager, Path(root).resolve(), direct_key
        self._pending: dict[str, dict[str, Any]] = {}
        self._errors: dict[str, str] = {}
        self._timer = QTimer(manager)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._apply_pending)
        self._timer.start()

    def close(self) -> None:
        self._timer.stop()
        self._pending.clear()

    def _adapter(self, key: str) -> WindowAdapter | None:
        if key == "launcher":
            window = getattr(self.manager, "_launcher_window", None)
            return WindowAdapter(key, window, self.manager, self.root) if window else None
        handle = self.manager._existing_window(key)
        if handle and key in ("film_design", "film_simulation"):
            if getattr(self.manager, "_film_ui_backend", "legacy") == "quick":
                return QuickFilmWindowAdapter(key, handle.window, handle.bridge, self.root)
            from coat_sim.ui.film_design import FilmWindowAdapter
            return FilmWindowAdapter(key, handle.window, handle.bridge, self.root)
        return WindowAdapter(key, handle.window, handle.bridge, self.root) if handle else None

    def _is_opening(self, key: str) -> bool:
        return (key in self.manager._loading
                or key in getattr(self.manager, "openingKeys", []))

    def _read(self, key: str) -> dict[str, Any]:
        adapter = self._adapter(key)
        if adapter:
            state = adapter.snapshot()
            state["pending_parameters"] = key in self._pending
            if key in self._errors:
                state["initialization_error"] = self._errors[key]
            return state
        return {"window_type": key, "state": "loading" if self._is_opening(key)
                else "closed", "pid": os.getpid(),
                "pending_parameters": key in self._pending, "error": self._errors.get(key),
                **_window_contract(key)}

    def _apply_pending(self) -> None:
        for key, parameters in list(self._pending.items()):
            adapter = self._adapter(key)
            if adapter is None:
                if not self._is_opening(key):
                    self._errors[key] = "窗口未能打开，请重新打开"
                    del self._pending[key]
                continue
            try:
                adapter.update(**parameters)
            except UiControlError as exc:
                if exc.code == "ui_unavailable":
                    continue
                self._errors[key] = str(exc)
            except Exception as exc:
                self._errors[key] = str(exc)
            else:
                self._errors.pop(key, None)
            del self._pending[key]

    def dispatch(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if "force" in arguments and type(arguments["force"]) is not bool:
            raise UiControlError("invalid_force", "force 须为布尔值")
        scope = presentation_scope(self.manager)
        available = legacy_window_types(self.manager, WINDOW_TYPES)
        if operation == "list_windows":
            return {"pid": os.getpid(), "scope": scope,
                    "launcher_pid": os.getpid() if scope == "launcher" else None,
                    "available_windows": available,
                    "windows": [self._read(key) for key in available],
                    "isolated_windows": [],
                    "same_process_windows": ["source", "substrate"] if scope == "standalone" else []}
        key = arguments.get("window_type")
        if key not in WINDOW_TYPES:
            raise UiControlError("invalid_window", "未知窗口类型")
        scope_error = legacy_window_scope_error(self.manager, key)
        if scope_error is not None:
            raise UiControlError(*scope_error)
        if operation == "read_window":
            state = self._read(key)
            if key in self._errors:
                state["initialization_error"] = self._errors[key]
            return state
        if operation == "open_window":
            parameters = {name: arguments[name] for name in (
                "fields", "design_path", "data", "intended_path", "pointers", "force")
                if name in arguments and arguments[name] is not None}
            if not any(name != "force" for name in parameters):
                parameters = {}
            if key in self._pending and parameters:
                if self._pending[key] == parameters:
                    parameters = {}  # The identical pending request is already owned here.
                else:
                    raise UiControlError("window_loading", "窗口已有待应用的打开参数；请等待完成后再打开")
            service = CoatService(self.root)
            for name in ("design_path", "intended_path"):
                if parameters.get(name):
                    parameters[name] = str(service._path(parameters[name]))
            state = self._read(key)
            if state.get("dirty") and any(parameters.get(name) is not None for name in ("design_path", "data")):
                if not parameters.get("force", False):
                    raise UiControlError("unsaved_changes", "窗口有未保存修改；替换文档须明确 force=true")
            if key == "launcher":
                if self._adapter(key) is None:
                    from PySide6.QtCore import QUrl
                    from PySide6.QtGui import QImageReader
                    from PySide6.QtQml import QQmlComponent, QQmlContext
                    from coat_sim.ui.launcher import QML_DIR, BACKGROUND
                    size = QImageReader(str(BACKGROUND)).size()
                    self.manager._image_width, self.manager._image_height = size.width(), size.height()
                    context = QQmlContext(self.manager.engine.rootContext())
                    context.setContextProperty("launcher", self.manager)
                    component = QQmlComponent(self.manager.engine, QUrl.fromLocalFile(str(QML_DIR / "LauncherWindow.qml")))
                    window = component.create(context)
                    if window is None:
                        raise UiControlError("window_error", "启动器窗口创建失败")
                    self.manager._launcher_window = window
                    self.manager._launcher_context, self.manager._launcher_component = context, component
                    window.show()
                self.manager._activate_window(self._adapter(key).window)
            else:
                modules = self.manager.modules
                self.manager.launch(next(index for index, item in enumerate(modules) if item["key"] == key))
            if parameters:
                self._pending[key] = parameters
                self._apply_pending()
            return self._read(key)
        if operation == "close_window":
            if key == "launcher":
                states = [self._read(item) for item in WINDOW_TYPES if item != "launcher"]
                if any(item.get("dirty") or item.get("busy") for item in states) and not arguments.get("force", False):
                    raise UiControlError("unsaved_changes", "有未保存修改或正在计算，关闭启动器须明确 force=true")
                from PySide6.QtCore import QTimer
                QTimer.singleShot(100, self.manager.shutdown)
                return {"window_type": key, "state": "closing"}
            if key in self._pending:
                if not arguments.get("force", False):
                    raise UiControlError("window_loading", "窗口正在加载；取消待加载参数须明确 force=true")
                self._pending.pop(key)
            adapter = self._adapter(key)
            if adapter is None and self._is_opening(key):
                if not arguments.get("force", False):
                    raise UiControlError("window_loading", "窗口正在创建；取消须明确 force=true")
                cancel = getattr(self.manager, "closeWindow", None)
                requests = list(getattr(self.manager, "_open_requests", {}).values())
                for _spec, request in requests:
                    if request.key == key and callable(cancel):
                        cancel(request.instance_id)
                # Compatibility managers without asynchronous requests retain
                # their old loading cancellation seam.
                if not requests and key in self.manager._loading:
                    self.manager._set_loading(key, False)
                self._errors.pop(key, None)
                return {"window_type": key, "state": "closed"}
            if adapter:
                state = adapter.snapshot()
                if (state.get("dirty") or state.get("busy")) and not arguments.get("force", False):
                    raise UiControlError("unsaved_changes", "窗口有未保存修改或正在计算；关闭须明确 force=true")
                detach = getattr(adapter.bridge, 'detach_observers', None)
                if callable(detach): detach()
                from PySide6.QtCore import QTimer
                if key in ("film_design", "film_simulation") and arguments.get("force", False):
                    if isinstance(adapter, QuickFilmWindowAdapter):
                        if key == "film_design":
                            adapter.window.setProperty("allowClose", True)
                    else:
                        adapter.window._force_close = True
                QTimer.singleShot(100, adapter.window.close)
                return {"window_type": key, "state": "closing"}
            return {"window_type": key, "state": "closed"}
        adapter = self._adapter(key)
        if adapter is None:
            raise UiControlError("window_not_open", "请先打开窗口并等待 state=ready")
        if operation == "update_window":
            parameters = {name: value for name, value in arguments.items() if name != "window_type"}
            return adapter.update(**parameters)
        if operation == "window_action":
            return adapter.action(arguments["action"], arguments.get("arguments"))
        raise UiControlError("unsupported_operation", "不支持的界面操作")
