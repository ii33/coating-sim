"""Isolated, asynchronous probes for film-synthesis GPU forward runtimes."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

_MARKER = "__COAT_FILM_SYNTHESIS_FORWARD__="
_BACKENDS = ("cuda", "rocm", "xpu")
_MAX_OUTPUT = 128 * 1024


class FilmSynthesisForwardProbe(QObject):
    """Own the probe generation and child process, independent of ComputeTarget."""

    changed = Signal()
    finished = Signal(int, object)

    def __init__(self, *, python: str | None = None, cwd: str | Path | None = None,
                 timeout: float = 15.0,
                 command_factory: Callable[[str], list[str]] | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._python = python or sys.executable
        self._cwd = str(cwd or Path(__file__).resolve().parents[2])
        self._timeout = max(0.1, min(float(timeout), 15.0))
        self._command_factory = command_factory
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="film-forward-probe")
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._poll)
        self._future: Future | None = None
        self._cancel_event: threading.Event | None = None
        self._active_process: subprocess.Popen | None = None
        self._active_lock = threading.Lock()
        self._generation = 0
        self._closed = False
        self._busy = False
        self._status = "尚未探测"
        self._results: dict[str, dict[str, Any]] = {
            name: {"backend": name, "available": False, "devices": [], "reason": "尚未探测"}
            for name in _BACKENDS
        }

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def status(self) -> str:
        return self._status

    @property
    def results(self) -> dict[str, dict[str, Any]]:
        return json.loads(json.dumps(self._results, ensure_ascii=False))

    @Slot()
    def probe(self) -> None:
        if self._closed:
            return
        self._cancel_running()
        self._generation += 1
        generation = self._generation
        cancelled = threading.Event()
        self._cancel_event = cancelled
        self._busy = True
        self._status = "正在独立子进程检查 CUDA、ROCm 和 XPU 的 FP64/complex128 能力"
        self._future = self._executor.submit(self._run, generation, cancelled)
        self._timer.start()
        self.changed.emit()

    @Slot()
    def cancel(self) -> None:
        self._generation += 1
        self._cancel_running()
        self._busy = False
        self._status = "探测已取消"
        self._timer.stop()
        self._future = None
        self._cancel_event = None
        self.changed.emit()

    def _cancel_running(self) -> None:
        event = self._cancel_event
        if event is not None:
            event.set()

    def _run(self, generation: int, cancelled: threading.Event):
        from coat_sim.core.film_synthesis_forward import validate_forward_device

        rows: dict[str, dict[str, Any]] = {}
        for backend in _BACKENDS:
            if cancelled.is_set():
                break
            command = (self._command_factory(backend) if self._command_factory is not None else
                       [self._python, "-m", "coat_sim.core.film_synthesis_forward",
                        "--probe", "--backend", backend])
            environment = os.environ.copy()
            environment.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "env": environment,
                "cwd": self._cwd,
            }
            owned_job = None
            if sys.platform == "win32":
                kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                            | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004))
            else:
                kwargs["start_new_session"] = True
            child: subprocess.Popen | None = None
            try:
                if sys.platform == "win32":
                    # The venv launcher can spawn its real interpreter immediately.
                    # Assign a suspended launcher first so cancellation owns both.
                    from coat_sim.core.film_synthesis_parallel import _WindowsWorkerJob
                    owned_job = _WindowsWorkerJob()
                child = subprocess.Popen(command, **kwargs)
                if owned_job is not None:
                    owned_job.attach_and_resume(child)
            except (OSError, ValueError) as exc:
                if owned_job is not None:
                    owned_job.close()
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait()
                rows[backend] = {"backend": backend, "available": False, "devices": [],
                                 "reason": str(exc)[:500]}
                continue
            assert child is not None
            with self._active_lock:
                self._active_process = child
            try:
                deadline = time.monotonic() + self._timeout
                while True:
                    try:
                        output, _ = child.communicate(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        if cancelled.is_set() or time.monotonic() >= deadline:
                            if owned_job is not None:
                                owned_job.close()
                            else:
                                from coat_sim.agent.processes import ProcessTree
                                ProcessTree(child).close()
                            output, _ = child.communicate()
                            if cancelled.is_set():
                                break
                            rows[backend] = {"backend": backend, "available": False, "devices": [],
                                             "reason": f"{backend} 能力探测超过 {self._timeout:g} 秒"}
                            break
                if cancelled.is_set():
                    break
                if backend in rows:
                    continue
                if len(output) > _MAX_OUTPUT:
                    raise ValueError("探测器输出超过大小限制")
                if child.returncode != 0:
                    rows[backend] = {"backend": backend, "available": False, "devices": [],
                                     "reason": f"探测进程异常退出，代码 {child.returncode}"}
                    continue
                payload = None
                for line in reversed(output.decode("utf-8", errors="replace").splitlines()):
                    if line.startswith(_MARKER):
                        payload = json.loads(line[len(_MARKER):])
                        break
                if not isinstance(payload, dict) or payload.get("backend") != backend:
                    raise ValueError("探测器未返回匹配的结构化结果")
                devices = payload.get("devices")
                if payload.get("ok") is not True or not isinstance(devices, list):
                    rows[backend] = {"backend": backend, "available": False, "devices": [],
                                     "reason": str(payload.get("error") or "设备不可用")[:500]}
                    continue
                valid = [validate_forward_device(item, backend) for item in devices]
                if any(item is None for item in valid):
                    raise ValueError("探测器设备绑定不能为空")
                bindings = [item for item in valid if item is not None]
                rows[backend] = {"backend": backend, "available": bool(bindings),
                                 "devices": bindings, "reason": "" if bindings else "未发现可用设备"}
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                rows[backend] = {"backend": backend, "available": False, "devices": [],
                                 "reason": str(exc)[:500]}
            finally:
                with self._active_lock:
                    if self._active_process is child:
                        self._active_process = None
                if owned_job is not None:
                    owned_job.close()
                elif child.poll() is None:
                    from coat_sim.agent.processes import ProcessTree
                    ProcessTree(child).close()
        return generation, rows

    @Slot()
    def _poll(self) -> None:
        future = self._future
        if future is None or not future.done():
            return
        self._future = None
        self._timer.stop()
        self._busy = False
        try:
            generation, rows = future.result()
            if self._closed or generation != self._generation:
                return
            self._results = rows
            available = [backend.upper() for backend, row in rows.items() if row["available"]]
            self._status = ("可用设备：" + "、".join(available)) if available else "未发现可用 GPU 前向设备；CPU 仍可使用"
            self.finished.emit(generation, self.results)
        except Exception as exc:
            self._status = f"GPU 探测失败：{str(exc)[:400]}"
        finally:
            self._cancel_event = None
            self.changed.emit()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self._timer.stop()
        self._cancel_running()
        self._executor.shutdown(wait=False, cancel_futures=True)
