"""One-shot, authenticated local startup events for externally launched UI."""
from __future__ import annotations

import base64
import errno
import json
import os
from multiprocessing.connection import Client, Listener
from pathlib import Path
import queue
import secrets
import stat
import tempfile
import threading
import time
from typing import Any


ADDRESS_ENV = "COAT_UI_READY_ADDRESS"
FAMILY_ENV = "COAT_UI_READY_FAMILY"
KEY_ENV = "COAT_UI_READY_KEY"
MAX_EVENT_BYTES = 64 * 1024
READY_TIMEOUT_SECONDS = 45.0
# Application byte budget for pathname sockets, excluding the terminating NUL.
# Count filesystem-encoded bytes, not Python characters (TMPDIR may be Unicode).
MAX_UNIX_ADDRESS_BYTES = 103
SHORT_UNIX_TEMP_ROOTS = (Path("/tmp"), Path("/var/tmp"))


def _remove_ready_directory(directory: Path) -> None:
    (directory / "event.sock").unlink(missing_ok=True)
    directory.rmdir()


def _new_ready_directory(root: Path | None = None) -> Path:
    # Preserve the existing default tempfile location for ordinary paths.
    name = (tempfile.mkdtemp(prefix="coat-ui-ready-") if root is None
            else tempfile.mkdtemp(prefix="coat-ui-ready-", dir=root))
    directory = Path(name)
    try:
        os.chmod(directory, 0o700)
    except BaseException:
        _remove_ready_directory(directory)
        raise
    return directory


def _safe_short_root(candidate: Path) -> Path | None:
    # This is called only for AF_UNIX; never assume getuid exists on Windows or
    # another platform which cannot establish the directory ownership check.
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        return None
    try:
        root = candidate.resolve(strict=True)
        metadata = root.stat()
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            return None
        raise
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, getuid()}:
        return None
    shared_write = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if shared_write and not metadata.st_mode & stat.S_ISVTX:
        return None
    return root


def _unix_ready_address() -> tuple[Path, str]:
    directory = _new_ready_directory()
    address = str(directory / "event.sock")
    if len(os.fsencode(address)) <= MAX_UNIX_ADDRESS_BYTES:
        return directory, address
    _remove_ready_directory(directory)
    # The only fallback trigger is the measured pathname length. Creation,
    # chmod, root inspection or Listener errors (including EPERM) propagate;
    # none are a reason to try a different root or transport.
    seen: set[Path] = set()
    for candidate in SHORT_UNIX_TEMP_ROOTS:
        root = _safe_short_root(candidate)
        if root is None or root in seen:
            continue
        seen.add(root)
        directory = _new_ready_directory(root)
        address = str(directory / "event.sock")
        if len(os.fsencode(address)) <= MAX_UNIX_ADDRESS_BYTES:
            return directory, address
        _remove_ready_directory(directory)
    raise OSError(errno.ENAMETOOLONG,
                  f"AF_UNIX startup address exceeds {MAX_UNIX_ADDRESS_BYTES} bytes; "
                  "no safe short temporary directory is available")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _valid_endpoint(value: Any) -> bool:
    fields = {"protocol", "host", "port", "token", "pid", "root", "role",
              "window_type", "parent_pid"}
    if not isinstance(value, dict) or set(value) != fields:
        return False
    return (
        type(value["protocol"]) is int and value["protocol"] == 1
        and value["host"] == "127.0.0.1"
        and type(value["port"]) is int and 1 <= value["port"] <= 65535
        and isinstance(value["token"], str) and 32 <= len(value["token"]) <= 256
        and type(value["pid"]) is int and value["pid"] > 0
        and isinstance(value["root"], str) and 0 < len(value["root"]) <= 4096
        and isinstance(value["role"], str) and 0 < len(value["role"]) <= 64
        and (value["window_type"] is None
             or isinstance(value["window_type"], str) and len(value["window_type"]) <= 64)
        and (value["parent_pid"] is None
             or type(value["parent_pid"]) is int and value["parent_pid"] > 0)
    )


def _valid_event(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    status = value.get("status")
    if status == "handoff":
        return (set(value) == {"status", "process_pid", "host_pid"}
                and type(value["process_pid"]) is int and value["process_pid"] > 0
                and type(value["host_pid"]) is int and value["host_pid"] > 0)
    if status == "ready":
        return set(value) == {"status", "endpoint"} and _valid_endpoint(value["endpoint"])
    if status == "error":
        return (set(value) == {"status", "error"}
                and isinstance(value["error"], str) and 0 < len(value["error"]) <= 4000)
    return False


class UiReadyChannel:
    """Parent-side one-shot pipe carrying ready/error or early process exit."""

    def __init__(self) -> None:
        self.family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        self.authkey = secrets.token_bytes(32)
        self._temporary_directory: Path | None = None
        if self.family == "AF_PIPE":
            self.address = rf"\\.\pipe\coat-ui-ready-{os.getpid()}-{secrets.token_hex(12)}"
        else:
            self._temporary_directory, self.address = _unix_ready_address()
        try:
            self._listener = Listener(self.address, family=self.family, authkey=self.authkey)
        except Exception:
            if self._temporary_directory is not None:
                _remove_ready_directory(self._temporary_directory)
                self._temporary_directory = None
            raise
        self._closed = False

    def child_environment(self) -> dict[str, str]:
        return {
            ADDRESS_ENV: self.address,
            FAMILY_ENV: self.family,
            KEY_ENV: base64.urlsafe_b64encode(self.authkey).decode("ascii"),
        }

    def wait(self, process: Any, timeout: float = READY_TIMEOUT_SECONDS) -> dict[str, Any]:
        messages: queue.Queue[dict[str, Any]] = queue.Queue()

        def receive_event() -> None:
            while True:
                try:
                    with self._listener.accept() as connection:
                        raw = connection.recv_bytes(MAX_EVENT_BYTES)
                    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
                    if not _valid_event(value):
                        raise ValueError("invalid startup event schema")
                    messages.put(value)
                    if value["status"] == "handoff":
                        continue
                    return
                except Exception as exc:
                    messages.put({"status": "channel_error", "error": str(exc)[:1000]})
                    return

        def wait_for_exit() -> None:
            try:
                code = process.wait()
            except Exception as exc:
                messages.put({"status": "process_error", "error": str(exc)[:1000]})
            else:
                messages.put({"status": "process_exit", "returncode": code})

        threading.Thread(target=receive_event, name="coat-ui-ready-event", daemon=True).start()
        threading.Thread(target=wait_for_exit, name="coat-ui-process-wait", daemon=True).start()
        deadline = time.monotonic() + max(0.0, float(timeout))
        handoff_seen = False
        handoff_host_pid: int | None = None
        process_pid = getattr(process, "pid", None)

        def final_event(event: dict[str, Any]) -> dict[str, Any]:
            if event.get("status") == "ready":
                endpoint_pid = event["endpoint"]["pid"]
                expected_pid = handoff_host_pid if handoff_seen else process_pid
                if type(expected_pid) is not int or endpoint_pid != expected_pid:
                    return {"status": "channel_error", "error": "startup process identity mismatch"}
            if handoff_seen:
                return {**event, "handoff_host_pid": handoff_host_pid}
            return event

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"status": "timeout"}
                try:
                    event = messages.get(timeout=remaining)
                except queue.Empty:
                    return {"status": "timeout"}
                status = event.get("status")
                if status == "handoff":
                    if handoff_seen:
                        return {"status": "channel_error", "error": "duplicate host handoff"}
                    if event["process_pid"] != process_pid:
                        return {"status": "channel_error", "error": "bootstrap process identity mismatch"}
                    handoff_seen = True
                    handoff_host_pid = event["host_pid"]
                    continue
                if status in ("ready", "error"):
                    return final_event(event)
                if status == "process_exit":
                    if handoff_seen:
                        # The desktop bootstrap exited after handing the pipe
                        # environment to its branded host. Wait for that host's
                        # final ready/error event.
                        continue
                    # A normal UI can close just after sending ready. Give its
                    # already-sent pipe message a short event-only grace period.
                    grace = min(0.5, max(0.0, deadline - time.monotonic()))
                    if grace:
                        try:
                            follow_up = messages.get(timeout=grace)
                        except queue.Empty:
                            return event
                        if follow_up.get("status") == "handoff":
                            if handoff_seen:
                                return {"status": "channel_error", "error": "duplicate host handoff"}
                            if follow_up["process_pid"] != process_pid:
                                return {"status": "channel_error", "error": "bootstrap process identity mismatch"}
                            handoff_seen = True
                            handoff_host_pid = follow_up["host_pid"]
                            continue
                        if follow_up.get("status") in ("ready", "error"):
                            return final_event(follow_up)
                        if follow_up.get("status") == "channel_error":
                            return follow_up
                    return event
                return event
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._listener.close()
        finally:
            if self._temporary_directory is not None:
                _remove_ready_directory(self._temporary_directory)
                self._temporary_directory = None


def report_startup_event(status: str, *, endpoint: dict[str, Any] | None = None,
                         error: str | None = None, process_pid: int | None = None,
                         host_pid: int | None = None,
                         consume: bool | None = None) -> bool:
    """Send one startup event to the authenticated parent channel, if present."""
    if status not in ("handoff", "ready", "error"):
        return False
    if consume is None:
        consume = status != "handoff"
    environment = os.environ.pop if consume else os.environ.get
    address = environment(ADDRESS_ENV, None)
    family = environment(FAMILY_ENV, None)
    encoded_key = environment(KEY_ENV, None)
    if not address or family not in ("AF_PIPE", "AF_UNIX") or not encoded_key:
        return False
    try:
        authkey = base64.urlsafe_b64decode(encoded_key.encode("ascii"))
        event: dict[str, Any] = {"status": status}
        if endpoint is not None:
            event["endpoint"] = endpoint
        if error is not None:
            event["error"] = str(error)[:4000]
        if process_pid is not None:
            event["process_pid"] = int(process_pid)
        if host_pid is not None:
            event["host_pid"] = int(host_pid)
        if not _valid_event(event):
            return False
        raw = json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_EVENT_BYTES:
            raise ValueError("startup event is too large")
        with Client(address, family=family, authkey=authkey) as connection:
            connection.send_bytes(raw)
        return True
    except (OSError, EOFError, ValueError, TypeError):
        return False
