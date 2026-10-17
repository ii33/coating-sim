"""A single, fail-closed host/device draft shared by calculation windows.

The scheduler remains the job state owner. This model only holds a user's
selection and discovers capabilities away from the Qt GUI thread.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
import hashlib
import json
import os
import re
import threading
import uuid

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

from coat_sim.core.atomic_io import replace_with_retry
from coat_sim.scheduler.jobs import PROTOCOL_VERSION


PREFS_NAME = "compute_target_selections.json"
TASK_EXECUTORS = {
    "deposition": "process_recipe",
    "film_optimization": "film_optimization",
    "appearance": "appearance_render",
}
_PREFS_LOCK = threading.RLock()
_TASK_SUPPORT_SUMMARIES = {
    "deposition": (
        "已支持：CPU 沉积。Intel 核显 DirectML 平面数值已有限验证；遮板边界为实验范围。"
        "默认 DirectML 不保证选中 Intel，需核对实际设备与结果后端。"
    ),
    "film_optimization": (
        "已支持：CPU 膜系优化。Intel XPU 仅基础 FP64/complex128 探测通过；"
        "实际批评估两次超时，尚未通过任务验收，桌面优化保持 CPU。"
    ),
    "appearance": (
        "已支持：CPU 外观。GPU 为 NVIDIA CUDA 实验路径，需单独检测。"
        "Intel 核显外观 GPU 不可用：当前没有 Intel 渲染后端；CPU 渲染不代表 GPU 支持。"
    ),
}


class ComputeTargetError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(detail or code)


def remote_host_id(url: str) -> str:
    """Address-derived ID never includes the endpoint or credential itself."""
    return "remote:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def validate_capabilities(value: object, executor: str, *, remote: bool) -> list[dict]:
    """Only advertised, executable targets may become selectable devices.

    The old local daemon has no device catalog, so its CPU backend is derived
    from the known local worker. A remote host must explicitly advertise v1
    device binding; executor names alone cannot establish it.
    """
    if not isinstance(value, dict) or type(value.get("protocol_version")) is not int or value["protocol_version"] != PROTOCOL_VERSION:
        raise ComputeTargetError("incompatible_protocol")
    if remote and (type(value.get("api_version")) is not int or value["api_version"] != 1
                   or type(value.get("executor_schema_version")) is not int
                   or value["executor_schema_version"] != 1):
        raise ComputeTargetError("incompatible_protocol")
    if remote and (type(value.get("device_schema_version")) is not int
                   or value["device_schema_version"] != 1
                   or type(value.get("device_binding_version")) is not int
                   or value["device_binding_version"] != 1):
        raise ComputeTargetError("device_contract_unavailable")
    executors = value.get("executors")
    if not isinstance(executors, (list, tuple)) or executor not in executors:
        raise ComputeTargetError("unsupported_executor")
    if not remote:
        return [{"id": "cpu:default", "label": f"CPU · {os.cpu_count() or '?'} 逻辑核", "backend": "CPU",
                 "available": True, "reason": "", "physical_id": ""}]
    devices = value.get("devices")
    if not isinstance(devices, list):
        raise ComputeTargetError("device_contract_unavailable")
    rows = []
    for entry in devices:
        if not isinstance(entry, dict):
            continue
        identifier, backend = entry.get("id"), entry.get("backend")
        if (not isinstance(identifier, str) or not identifier or len(identifier) > 120
                or not isinstance(backend, str) or backend not in ("CPU", "gpu_cuda")):
            continue
        if not isinstance(entry.get("available"), bool) or not isinstance(entry.get("executors"), list):
            continue
        if executor not in entry["executors"]:
            continue
        # An individual GPU must be pinned by its server-side physical ID.
        physical_id = entry.get("physical_id", "")
        if backend == "gpu_cuda" and (not isinstance(physical_id, str)
                or (not physical_id and not (identifier == "cuda:default"
                    and entry.get("binding") == "default"))):
            continue
        algorithms = entry.get("algorithms", [])
        if (not isinstance(algorithms, list)
                or any(not isinstance(item, str) for item in algorithms)):
            continue
        name = entry.get("name")
        probe_passed = backend != "gpu_cuda" or entry.get("runtime_probe") == "passed"
        rows.append({"id": identifier, "label": str(name)[:100] if name else identifier,
                     "backend": backend, "available": entry["available"] and probe_passed,
                     "reason": str(entry.get("reason") or (
                         "服务端尚未通过真实 CUDA 检测。" if not probe_passed else ""))[:160],
                     "physical_id": physical_id, "algorithms": algorithms})
    if len({row["id"] for row in rows}) != len(rows):
        raise ComputeTargetError("duplicate_device_id")
    if not rows:
        raise ComputeTargetError("no_compatible_devices")
    return rows


def _read_prefs(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_preference(path: Path, task: str, host_id: str, device_id: str) -> None:
    with _PREFS_LOCK:
        value = _read_prefs(path)
        value[task] = {"host_id": host_id, "device_id": device_id}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            replace_with_retry(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class ComputeTarget(QObject):
    """Qt-facing selection; the submitter calls `client_for_submit` once."""

    changed = Signal()
    appearanceCudaProbeRequested = Signal()

    def __init__(self, root: Path, task: str, parent: QObject | None = None,
                 *, client_factory=None):
        super().__init__(parent)
        if task not in TASK_EXECUTORS:
            raise ValueError("unknown_compute_task")
        self.root, self.task = Path(root), task
        self._prefs_path = self.root / "config" / PREFS_NAME
        self._algorithm = "fast"
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="compute-host-probe")
        self._future = None
        self._generation = 0
        self._remote_client = None
        self._appearance_cuda_state = "unchecked"
        self._appearance_cuda_reason = "选择 CUDA 后执行独立进程检测。"
        if client_factory is None and task != "appearance":
            from coat_sim.scheduler.client import SchedulerClient
            client_factory = SchedulerClient
        self._client_factory = client_factory
        self._local_client = (client_factory(self.root) if task == "deposition" else None)
        self._message = "本机 CPU 可用；计算前会核对队列协议。"
        self._host_rows = []
        self._devices = []
        self._all_devices = []
        self._ready = False
        saved = _read_prefs(self._prefs_path).get(task, {})
        self._host_id = str(saved.get("host_id") or "local") if isinstance(saved, dict) else "local"
        self._device_id = str(saved.get("device_id") or "cpu:default") if isinstance(saved, dict) else "cpu:default"
        self._poll = QTimer(self)
        self._poll.setInterval(60)
        self._poll.timeout.connect(self._finish_probe)
        self.destroyed.connect(lambda *_: self._executor.shutdown(wait=False, cancel_futures=True))
        self.reloadSettings()

    @Property("QVariantList", notify=changed)
    def hosts(self):
        return list(self._host_rows)

    @Property("QVariantList", notify=changed)
    def devices(self):
        return list(self._devices)

    @Property(str, notify=changed)
    def hostId(self):
        return self._host_id

    @Property(str, notify=changed)
    def deviceId(self):
        return self._device_id

    @Property(str, notify=changed)
    def message(self):
        return self._message

    @Property(str, constant=True)
    def supportSummary(self):
        """Task acceptance scope is separate from transient runtime availability."""
        return _TASK_SUPPORT_SUMMARIES[self.task]

    @Property(bool, notify=changed)
    def canSubmit(self):
        return self._ready and any(row["id"] == self._device_id and row["available"] for row in self._devices)

    @Property(bool, constant=True)
    def supportsCudaProbe(self):
        return self.task == "appearance"

    @Slot()
    def requestAppearanceCudaProbe(self):
        if self.task == "appearance" and self._host_id == "local":
            self.appearanceCudaProbeRequested.emit()

    @Slot()
    def reloadSettings(self):
        from coat_sim.ui.settings import load_ui_settings
        settings = load_ui_settings()
        url = str(settings.get("compute_server_url") or "").strip()
        self._remote_url = url
        self._remote_tls = settings.get("compute_server_tls") is not False
        self._remote_ca_file = str(settings.get("compute_server_ca_file") or "")
        self._host_rows = [{"id": "local", "label": "本机", "available": True}]
        if url:
            self._host_rows.append({"id": remote_host_id(url), "label": url, "available": False})
        if self._host_id not in {row["id"] for row in self._host_rows}:
            # A deleted host cannot be silently replaced for a saved selection.
            self._message = "已保存的计算主机不再配置；请重新选择主机。"
            self._devices = []
            self._ready = False
            self.changed.emit()
            return
        self.refresh()

    @Slot(str)
    def setHostId(self, host_id: str):
        if host_id not in {row["id"] for row in self._host_rows}:
            return
        self._host_id = host_id
        self._device_id = "cpu:default" if host_id == "local" else ""
        self._devices = []
        self._all_devices = []
        self._ready = False
        _save_preference(self._prefs_path, self.task, self._host_id, self._device_id)
        self.changed.emit()
        self.refresh()

    @Slot(str)
    def setDeviceId(self, device_id: str):
        if any(row["id"] == device_id and row["available"] for row in self._devices):
            self._device_id = device_id
            selected = next(row for row in self._devices if row["id"] == device_id)
            self._message = selected.get("support_note") or "所选设备已就绪。"
            _save_preference(self._prefs_path, self.task, self._host_id, device_id)
            self.changed.emit()

    @Slot(str)
    def setAlgorithm(self, algorithm: str):
        self._algorithm = algorithm
        self._devices = self._filter_devices(self._all_devices)
        selected = next((row for row in self._devices if row["id"] == self._device_id), None)
        self._message = (selected.get("reason") or "所选设备不可用。"
                         if selected is not None and not selected["available"]
                         else (selected.get("support_note") if selected else "") or "所选设备已就绪。")
        self.changed.emit()

    def set_appearance_backend(self, backend: str):
        if self.task != "appearance":
            raise ValueError("not_appearance_target")
        self._device_id = "cuda:default" if backend == "gpu_cuda" else "cpu:default"
        _save_preference(self._prefs_path, self.task, self._host_id, self._device_id)
        self.changed.emit()

    def publish_appearance_cuda(self, state: str, reason: str):
        """Accept only the appearance controller's real isolated CUDA probe."""
        if self.task != "appearance" or state not in ("unchecked", "checking", "available", "unavailable"):
            return
        self._appearance_cuda_state = state
        self._appearance_cuda_reason = reason[:240]
        if self._host_id == "local" and self._ready:
            self._all_devices = self._appearance_local_devices()
            self._devices = list(self._all_devices)
            if self._device_id == "cuda:default":
                self._message = reason[:240]
            self.changed.emit()

    def _appearance_local_devices(self):
        return [
            {"id": "cpu:default", "label": f"CPU · {os.cpu_count() or '?'} 逻辑核", "backend": "cpu",
             "available": True, "reason": "", "physical_id": ""},
            {"id": "cuda:default", "label": "NVIDIA CUDA 默认设备 · 物理 GPU ID 未指定",
             "backend": "gpu_cuda", "available": self._appearance_cuda_state == "available",
             "reason": self._appearance_cuda_reason, "physical_id": ""},
        ]

    def _filter_devices(self, devices):
        if self.task == "film_optimization":
            # Enumeration is not executor support: current production solvers
            # and the forward worker are CPU-only, even on a CUDA-capable host.
            return [{**row, "available": False,
                     "reason": "膜系优化当前使用 CPU 求解；Intel XPU 批评估尚未通过任务验收。"}
                    if row["backend"] != "CPU" else row for row in devices]
        if self.task == "deposition":
            filtered = []
            for row in devices:
                if self._algorithm != "fast" and row["backend"] != "CPU":
                    filtered.append({**row, "available": False, "reason": "此算法只支持 CPU。"})
                elif row.get("algorithms") and self._algorithm not in row["algorithms"]:
                    filtered.append({**row, "available": False, "reason": "此主机设备不支持所选算法。"})
                else:
                    filtered.append(row)
            return filtered
        return list(devices)

    @Slot()
    def refresh(self):
        self._generation += 1
        generation = self._generation
        self._devices = []
        self._all_devices = []
        self._ready = False
        self._remote_client = None
        self._message = "正在检查计算主机…"
        self.changed.emit()
        if self._host_id == "local":
            if self.task == "appearance":
                self._all_devices = self._appearance_local_devices()
                self._devices = list(self._all_devices)
                self._ready = True
                self._message = "外观计算使用本机独立进程；CUDA 需单独检测。"
                self.changed.emit()
                return
            if self.task == "film_optimization":
                self._all_devices = [{"id": "cpu:default", "label": f"CPU · {os.cpu_count() or '?'} 逻辑核",
                                      "backend": "CPU", "available": True, "reason": "", "physical_id": ""}]
                self._devices = list(self._all_devices)
                self._ready = True
                self._message = "膜系优化当前使用 CPU 求解；GPU 尚未接入此任务。"
                self.changed.emit()
                return
            self._future = self._executor.submit(self._probe_local, generation)
        elif self._remote_url and self._host_id == remote_host_id(self._remote_url):
            self._future = self._executor.submit(self._probe_remote, generation)
        else:
            self._message = "计算主机未配置。"
            self.changed.emit()
            return
        self._poll.start()

    def _probe_local(self, generation: int):
        if self.task == "appearance":
            # The current appearance UI owns an isolated QProcess worker, not
            # the scheduler. Its CUDA availability is published by that
            # controller's real on-demand allocation probe below.
            return generation, self._appearance_local_devices(), None, "local"
        devices = validate_capabilities(
            self._local_client.capabilities(), TASK_EXECUTORS[self.task], remote=False)
        return generation, devices, None, "local"

    @staticmethod
    def _probe_local_optional(generation: int):
        from coat_sim.core.compute_device_probe import probe_local_compute_targets
        try:
            return generation, probe_local_compute_targets(), None, "optional"
        except Exception:
            # An optional accelerator probe must not disable the known CPU
            # worker. Keep the reason generic; driver errors may expose paths.
            cpu = {"id": "cpu:default", "label": f"CPU · {os.cpu_count() or '?'} 逻辑核",
                   "backend": "CPU", "available": True, "reason": "", "physical_id": ""}
            return generation, [cpu], None, "optional-error"

    def _probe_remote(self, generation: int):
        from coat_sim.scheduler.remote_client import make_remote_client
        from coat_sim.ui.assistant_credentials import read_compute_token
        token = read_compute_token(self._remote_url, strict=True)
        if not token:
            raise ComputeTargetError("missing_remote_token")
        parts = urlsplit(self._remote_url)
        if (self._remote_tls and parts.scheme != "https") or (not self._remote_tls and parts.scheme != "http"):
            raise ComputeTargetError("tls_scheme_mismatch")
        client = make_remote_client(
            self._remote_url, self.root, token=token,
            ca_file=self._remote_ca_file or None if self._remote_tls else None,
            allow_insecure_http=not self._remote_tls)
        health = client.ensure_started()  # injected transport: health only, never local daemon
        if (not isinstance(health, dict) or type(health.get("api_version")) is not int
                or health["api_version"] != 1 or health.get("protocol_version") != 1
                or health.get("ready") is not True):
            raise ComputeTargetError("incompatible_protocol")
        devices = validate_capabilities(client.capabilities(), TASK_EXECUTORS[self.task], remote=True)
        return generation, devices, client, "remote"

    def _finish_probe(self):
        future = self._future
        if future is None or not future.done():
            return
        self._future = None
        self._poll.stop()
        try:
            generation, devices, client, phase = future.result()
            if generation != self._generation:
                return
            self._all_devices = devices
            self._devices = self._filter_devices(devices)
            if client is not None:
                self._remote_client = client
            if not self._device_id and self._devices:
                self._device_id = next((row["id"] for row in self._devices if row["available"]), "")
                if self._device_id:
                    _save_preference(self._prefs_path, self.task, self._host_id, self._device_id)
            self._ready = True
            selected = next((row for row in self._devices if row["id"] == self._device_id), None)
            if phase == "optional-error" and self.canSubmit:
                self._message = "本机 CPU 可用；可选加速设备探测失败。"
            elif self.canSubmit:
                self._message = (selected.get("support_note") if selected else "") or "主机能力已核对。"
            else:
                self._message = (selected.get("reason") if selected else "") or "所选设备不可用；请选择可用设备。"
            if phase == "local" and self.task == "deposition":
                self._future = self._executor.submit(self._probe_local_optional, generation)
                self._poll.start()
        except Exception as exc:
            # Endpoint and keyring details never enter UI/logs.
            code = getattr(exc, "code", "host_probe_failed")
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                code = "host_probe_failed"
            self._devices = []
            self._ready = False
            self._message = {
                "missing_remote_token": "未保存服务端令牌；无法连接。",
                "tls_scheme_mismatch": "地址与 TLS 设置不匹配；无法连接。",
                "incompatible_protocol": "服务端协议版本不兼容。",
                "unsupported_executor": "该主机不支持此计算任务。",
                "device_contract_unavailable": "服务端未报告可绑定设备；拒绝提交。",
                "no_compatible_devices": "该主机没有支持此任务的可用设备。",
                "remote_device_binding_unsupported": "当前远程提交协议未绑定设备 ID；未提交任务。",
            }.get(code, f"计算主机探测失败（{code}）；未提交任务。")
        self.changed.emit()

    def client_for_submit(self, *, algorithm: str = "", client_factory=None):
        """Return the exact route/backend after an admission-time compatibility check."""
        local_client = ((client_factory or self._client_factory)(self.root)
                        if self._host_id == "local" and self.task != "appearance" else None)
        if self._host_id == "local" and not self._ready:
            # Preserve direct Python entry points while discovery is still
            # running. These already perform synchronous queue admission.
            self._devices = (self._appearance_local_devices() if self.task == "appearance" else
                             validate_capabilities(local_client.capabilities(), TASK_EXECUTORS[self.task], remote=False))
            self._ready = True
            self.changed.emit()
        if not self.canSubmit:
            raise ComputeTargetError("target_not_ready", self._message)
        device = next(row for row in self._devices if row["id"] == self._device_id)
        if self.task == "film_optimization" and device["backend"] != "CPU":
            raise ComputeTargetError("unsupported_algorithm_device")
        if self.task == "deposition" and algorithm != "fast" and device["backend"] != "CPU":
            raise ComputeTargetError("unsupported_algorithm_device")
        if self.task == "appearance":
            raise ComputeTargetError("appearance_uses_isolated_process")
        if self._host_id == "local":
            # Status can change between discovery and submission. Existing local
            # capabilities are inexpensive once the daemon is warm.
            validate_capabilities(local_client.capabilities(), TASK_EXECUTORS[self.task], remote=False)
            return local_client, device
        if self._remote_client is None:
            raise ComputeTargetError("target_not_ready")
        from coat_sim.scheduler.remote_client import make_remote_client
        from coat_sim.ui.settings import load_ui_settings
        settings = load_ui_settings()
        if (str(settings.get("compute_server_url") or "").strip() != self._remote_url
                or (settings.get("compute_server_tls") is not False) != self._remote_tls
                or str(settings.get("compute_server_ca_file") or "") != self._remote_ca_file):
            raise ComputeTargetError("host_settings_changed", "服务端设置已更改；请重新检查设备。")
        transport = self._remote_client.transport
        return make_remote_client(
            self._remote_url, self.root, token=transport.token,
            ca_file=self._remote_ca_file or None if self._remote_tls else None,
            allow_insecure_http=not self._remote_tls, device_id=device["id"]), device
