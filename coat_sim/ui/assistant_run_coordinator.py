"""Own worker transport and request lifecycle for one AssistantBridge."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from coat_sim.agent.processes import ProcessTree


UI_DIR = Path(__file__).resolve().parent
MAX_REPLY_BYTES = 2_000_000


class AssistantRunCoordinator(QObject):
    """Single owner for worker process, run id, cancel deadline and event gate."""

    eventAccepted = Signal(object)
    transportFailed = Signal()
    cancelTimedOut = Signal()

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._worker = None
        self._worker_tree = None
        self._request_id = ""
        self._run_active = False
        self._stop_pending = False
        self._write_lock = threading.Lock()
        self._closed = False
        self._cancel_timer = QTimer(self)
        self._cancel_timer.setSingleShot(True)
        self._cancel_timer.timeout.connect(self.cancelTimedOut)

    @property
    def worker(self):
        return self._worker

    @worker.setter
    def worker(self, value):
        self._worker = value

    @property
    def worker_tree(self):
        return self._worker_tree

    @property
    def request_id(self):
        return self._request_id

    @request_id.setter
    def request_id(self, value):
        self._request_id = str(value or "")

    @property
    def run_active(self) -> bool:
        return self._run_active

    @property
    def stop_pending(self):
        return self._stop_pending

    @property
    def cancel_timer(self):
        return self._cancel_timer

    def begin_run(self) -> str:
        self._stop_pending = False
        self._request_id = uuid.uuid4().hex
        self._run_active = True
        return self._request_id

    def finish_run(self, request_id: str | None = None) -> bool:
        """Settle one run while retaining its identity for history and late accounting."""
        if request_id is not None and request_id != self._request_id:
            return False
        if not self._run_active:
            return False
        self._cancel_timer.stop()
        self._stop_pending = False
        self._run_active = False
        return True

    def reset_run(self) -> None:
        """Forget the retained identity at an explicit progress reset or stop."""
        self._cancel_timer.stop()
        self._stop_pending = False
        self._run_active = False
        self._request_id = ""

    def begin_cancel(self, timeout_ms: int = 15000) -> bool:
        if not self._run_active or not self._request_id or self._stop_pending:
            return False
        self._stop_pending = True
        self._cancel_timer.start(timeout_ms)
        return True

    def clear_cancel(self) -> None:
        self._cancel_timer.stop()
        self._stop_pending = False

    def write_worker(self, value: dict) -> bool:
        process = self._worker
        if process is None or process.poll() is not None or process.stdin is None:
            return False
        try:
            data = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
            with self._write_lock:
                process.stdin.write(data)
                process.stdin.flush()
            return True
        except (OSError, ValueError):
            self.transportFailed.emit()
            return False

    def ensure_worker(self, event_signal) -> None:
        if self._worker is not None and self._worker.poll() is None:
            return
        if self._worker_tree is not None:
            self._worker_tree.close()
            self._worker_tree = None
        root = UI_DIR.parents[1]
        log_dir = root / "artifacts" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                            "PYTHONUNBUFFERED": "1", "COAT_UI_PID": str(os.getpid())})
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        worker_executable = None
        if sys.platform == "win32" and not getattr(sys, "frozen", False):
            worker_executable = Path(sys.executable)
            console_executable = worker_executable.with_name("python.exe")
            if console_executable.is_file():
                worker_executable = console_executable
        from coat_sim.process_entry import module_command
        command = module_command("coat_sim.agent.worker",
            interpreter=str(worker_executable) if worker_executable else None, unbuffered=True)
        with (log_dir / "assistant-worker.log").open("ab") as log:
            process = subprocess.Popen(command,
                                       cwd=str(root), env=environment, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=log, **kwargs)
        self._worker = process
        self._worker_tree = ProcessTree(process)

        def read_events() -> None:
            try:
                while process.stdout is not None:
                    line = process.stdout.readline(MAX_REPLY_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_REPLY_BYTES:
                        process.kill()
                        break
                    try:
                        value = json.loads(line.decode("utf-8"))
                    except (UnicodeError, ValueError):
                        continue
                    if isinstance(value, dict):
                        event_signal.emit({"process": process.pid, "payload": value})
            finally:
                code = process.wait()
                event_signal.emit({"process": process.pid, "payload": {
                    "event": "exited", "text": f"助手后台已退出（{code}）。"}})

        threading.Thread(target=read_events, name="assistant-events", daemon=True).start()

    @Slot(object)
    def accept_worker_event(self, envelope: dict, auxiliary_request_ids=()) -> None:
        process = self._worker
        if self._closed or process is None or envelope.get("process") != process.pid:
            return
        value = envelope.get("payload", {})
        event = value.get("event")
        if event == "background_registered":
            return
        if event in ("context", "context_compaction"):
            request_id = value.get("request_id")
            primary_match = bool(self._request_id and request_id == self._request_id)
            auxiliary_match = request_id in tuple(auxiliary_request_ids)
            if not (primary_match or auxiliary_match):
                return
            self.eventAccepted.emit(value)
            return
        if event == "usage" and not self._run_active:
            # Settled usage is useful for accounting, but must identify the
            # retained request explicitly so an untagged event cannot be
            # attributed to a later run.
            if not self._request_id or value.get("request_id") != self._request_id:
                return
            self.eventAccepted.emit(value)
            return
        if event == "exited":
            if self._worker_tree is not None:
                self._worker_tree.close()
                self._worker_tree = None
        else:
            if not self._run_active or not self._request_id:
                return
            request_id = value.get("request_id", self._request_id)
            if request_id != self._request_id:
                return
        self.eventAccepted.emit(value)

    def stop_worker(self, *, finish: bool = True) -> object:
        """Stop transport ownership and return its process for usage settlement."""
        self.clear_cancel()
        process = self._worker
        if process is not None and process.poll() is None:
            self.write_worker({"op": "shutdown"})
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if self._worker_tree is not None:
            self._worker_tree.close()
            self._worker_tree = None
        self._worker = None
        if finish:
            self.reset_run()
        return process

    def close(self) -> None:
        self._closed = True
        self.stop_worker()
