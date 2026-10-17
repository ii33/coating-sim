"""Project-local, bounded diagnostic streams shared safely across processes."""
from __future__ import annotations

from contextlib import contextmanager
import io
from pathlib import Path
import sys
import threading
import time
from typing import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 2
MAX_RECORD_BYTES = 64 * 1024
CHANNELS = frozenset({"desktop", "assistant", "assistant-client", "assistant-worker",
                      "project", "mcp-ui", "scheduler", "compute-worker", "remote-worker"})
_THREAD_LOCK = threading.RLock()


def log_path(channel: str, root: str | Path | None = None) -> Path:
    if channel not in CHANNELS:
        raise ValueError("unknown_project_log_channel")
    return Path(root if root is not None else PROJECT_ROOT).resolve() / "artifacts" / "logs" / (channel + ".log")


@contextmanager
def _rotation_lock(directory: Path) -> Iterator[None]:
    """Lock logging only; never touch application ownership/endpoint locks."""
    with _THREAD_LOCK, (directory / ".rotation.lock").open("a+b") as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + 0.25
        while True:
            lock.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class ProjectLog(io.TextIOBase):
    """Open only while writing, so another process can rotate on Windows.

    No raw file descriptor is exposed: child stdio and native crash handlers
    must not bypass the byte limit. Logging failures never fail application work.
    """
    def __init__(self, channel: str, root: str | Path | None = None, *,
                 max_bytes: int = MAX_LOG_BYTES, backups: int = LOG_BACKUPS):
        super().__init__()
        if max_bytes < 64 or not 0 <= backups <= LOG_BACKUPS:
            raise ValueError("invalid_project_log_limit")
        self.path = log_path(channel, root)
        self.max_bytes, self.backups = max_bytes, backups
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # Read-only/full installations keep their application behavior.

    encoding = "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not text or self.closed:
            return len(text)
        try:
            data = text.encode("utf-8", errors="replace")
            limit = min(MAX_RECORD_BYTES, self.max_bytes)
            if len(data) > limit:
                # Decode a sliced UTF-8 tail to avoid broken multibyte characters.
                data = ("[truncated] " + data[-(limit - 20):].decode("utf-8", errors="replace")).encode("utf-8")[-limit:]
            with _rotation_lock(self.path.parent):
                existing = [self.path] + [self.path.with_name(self.path.name + "." + str(index))
                                         for index in range(1, self.backups + 1)]
                for candidate in existing:
                    if candidate.exists() and candidate.stat().st_size > self.max_bytes:
                        with candidate.open("rb") as previous:
                            previous.seek(-self.max_bytes, 2)
                            tail = previous.read(self.max_bytes)
                        candidate.write_bytes(tail)
                if self.path.exists() and self.path.stat().st_size + len(data) > self.max_bytes:
                    for index in range(self.backups, 0, -1):
                        target = self.path.with_name(self.path.name + "." + str(index))
                        source = self.path if index == 1 else self.path.with_name(self.path.name + "." + str(index - 1))
                        if source.exists():
                            source.replace(target)
                    if self.path.exists():
                        self.path.unlink()
                with self.path.open("ab") as output:
                    output.write(data)
        except (OSError, ValueError):
            pass
        return len(text)


def install_process_log(channel: str, root: str | Path | None = None, *,
                        stderr_only: bool = False) -> ProjectLog:
    stream = ProjectLog(channel, root)
    names = ("stderr", "__stderr__") if stderr_only else ("stderr", "__stderr__", "stdout", "__stdout__")
    for name in names:
        setattr(sys, name, stream)
    return stream


def logged_module_command(module: str, *arguments: str, channel: str,
                          project_root: str | Path, stderr_only: bool = False,
                          interpreter: str | None = None, unbuffered: bool = False) -> list[str]:
    from coat_sim.process_entry import module_command
    log_path(channel, project_root)  # Validate before creating a process.
    options = ["--channel", channel, "--project-root", str(project_root)]
    if stderr_only:
        options.append("--stderr-only")
    return module_command("coat_sim.logged_process", *options, "--module", module, "--", *arguments,
                          interpreter=interpreter, unbuffered=unbuffered)
