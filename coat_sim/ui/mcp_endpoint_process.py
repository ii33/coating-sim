"""A hidden, pipe-capable MCP child process with nonblocking Qt status updates."""
from __future__ import annotations

import queue
import subprocess
import atexit
from threading import Thread

from PySide6.QtCore import QObject, QProcess, QTimer, Signal


class McpEndpointProcess(QObject):
    readyReadStandardOutput = Signal()
    errorOccurred = Signal(object)
    finished = Signal(int, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._process = None
        self._cwd = None
        self._env = None
        self._output = queue.SimpleQueue()
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._check)
        # Also cover normal Python shutdown or bridge destruction without a
        # window-closing event; kill() does not access a deleted Qt object.
        atexit.register(self.kill)
        self.destroyed.connect(lambda *_: self.kill())

    def state(self):
        return QProcess.Running if self._process is not None else QProcess.NotRunning

    def setProcessEnvironment(self, environment) -> None:
        self._env = dict(item.split("=", 1) for item in environment.toStringList())

    def setWorkingDirectory(self, directory: str) -> None:
        self._cwd = directory

    def start(self, program: str, args: list[str]) -> bool:
        self._output = queue.SimpleQueue()
        try:
            # CREATE_NO_WINDOW preserves stdout/stderr pipes for python.exe.
            self._process = subprocess.Popen([program, *args], cwd=self._cwd, env=self._env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self._process = None
            self.errorOccurred.emit(exc)
            return False
        stream, output = self._process.stdout, self._output
        def consume() -> None:
            try:
                for chunk in iter(stream.readline, b""):
                    output.put(chunk)
            finally:
                stream.close()
        Thread(target=consume, name="mcp-http-output", daemon=True).start()
        self._poll.start()
        return True

    def readAllStandardOutput(self) -> bytes:
        chunks = []
        while True:
            try:
                chunks.append(self._output.get_nowait())
            except queue.Empty:
                return b"".join(chunks)

    def _check(self) -> None:
        if not self._output.empty():
            self.readyReadStandardOutput.emit()
        if self._process is not None:
            code = self._process.poll()
            if code is not None:
                self._process = None
                self._poll.stop()
                self.finished.emit(code, None)

    def terminate(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
            except OSError:
                pass

    def kill(self) -> None:
        if self._process is not None:
            try:
                self._process.kill()
            except OSError:
                pass
