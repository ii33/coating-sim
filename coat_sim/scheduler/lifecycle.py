"""Headless discovery and coordinated exit of exact local scheduler instances.

Registry records are private transport metadata. Public lifecycle results never
contain the lifecycle secret. The OS start gate prevents a late UI reader or a
previously spawned child from publishing a scheduler during application exit.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from coat_sim.core.process_identity import _process_identity
from coat_sim.project_recovery import HeldFileLock
from .io import write_json, write_text
from uuid import uuid4

PREPARE_LEASE_SECONDS = 45.0
DEFAULT_IDLE_SECONDS = 60.0


def scheduler_scope_root() -> Path:
    # Same per-user scope as assistant_runtime_root, without importing Qt.
    explicit = os.environ.get("COAT_ASSISTANT_RUNTIME_ROOT")
    if explicit:
        return Path(explicit).expanduser().absolute()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "CoatSim" / "assistant"


def registry_directory(runtime_root=None) -> Path:
    return Path(runtime_root or scheduler_scope_root()) / "artifacts/runtime/schedulers"


def canonical_root(root) -> str:
    return os.path.normcase(str(Path(root).resolve()))


def registration_path(root, runtime_root=None) -> Path:
    digest = hashlib.sha256(canonical_root(root).encode("utf-8")).hexdigest()
    return registry_directory(runtime_root) / (digest + ".json")


def acquire_start_gate(runtime_root=None, *, timeout=0.0) -> HeldFileLock:
    gate = HeldFileLock(registry_directory(runtime_root) / "start.lock")
    deadline = time.monotonic() + timeout
    while not gate.acquire():
        if time.monotonic() >= deadline:
            raise RuntimeError("scheduler_exit_in_progress")
        time.sleep(.025)
    return gate


def current_start_epoch(runtime_root=None) -> str:
    """Read/create a launch generation while the caller holds the start gate."""
    path = registry_directory(runtime_root) / "start.epoch"
    try:
        value = path.read_text(encoding="ascii")
    except FileNotFoundError:
        value = uuid4().hex
        write_text(path, value)
    if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
        raise RuntimeError("scheduler_start_epoch_invalid")
    return value


def advance_start_epoch(runtime_root=None) -> str:
    """Invalidate already spawned, not-yet-published children under the gate."""
    value = uuid4().hex
    write_text(registry_directory(runtime_root) / "start.epoch", value)
    return value


def publish_registration(root, endpoint, secret, *, runtime_root=None):
    state, start = _process_identity(os.getpid())
    if state != "alive" or not start:
        raise RuntimeError("scheduler_process_identity_unknown")
    record = {"protocol_version": 1, "root": canonical_root(root),
              "host": "127.0.0.1", "port": endpoint["port"], "pid": os.getpid(),
              "process_start": start, "instance": endpoint["instance"], "secret": secret}
    write_json(registration_path(root, runtime_root), record, max_bytes=16384)


def remove_registration(root, instance, *, runtime_root=None):
    path = registration_path(root, runtime_root)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("instance") == instance:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def _alive(record) -> bool:
    state, start = _process_identity(record["pid"])
    if state == "dead" or (start is not None and start != record["process_start"]):
        return False
    if state != "alive" or start is None:
        raise RuntimeError("scheduler_process_identity_unknown")
    return True


def _read_registration(path: Path, runtime_root=None):
    try:
        if path.stat().st_size > 16384:
            raise ValueError()
        record = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("protocol_version") != 1
                or record.get("host") != "127.0.0.1"
                or type(record.get("port")) is not int or not 0 < record["port"] < 65536
                or type(record.get("pid")) is not int or record["pid"] <= 0
                or any(not isinstance(record.get(key), str) or not record[key]
                       for key in ("root", "instance", "process_start", "secret"))
                or len(record["secret"]) < 32
                or registration_path(record["root"], runtime_root).name != path.name):
            raise ValueError()
    except FileNotFoundError:
        return None
    except (ValueError, TypeError, KeyError):
        raise RuntimeError("scheduler_registry_invalid") from None
    if not _alive(record):
        path.unlink(missing_ok=True)
        return None
    return record


class SchedulerExitGroup:
    """All live schedulers in one user scope, held behind an OS start fence.

    prepare returns the number of currently running local calculations. Queued
    work remains durable. cancel releases uncommitted preparation and the fence;
    after commit it releases only the fence and cannot undo a requested exit.
    """
    def __init__(self, runtime_root=None):
        self.runtime_root = runtime_root
        self._gate = None
        self._records = []
        self._prepared = []
        self._exit_id = ""
        self._committed = False
        self._committed_instances = set()
        self._active_job_count = 0
        self._opener = build_opener(ProxyHandler({}))

    def _call(self, record, action):
        if not _alive(record):
            return None
        payload = {"protocol_version": 1, "action": action, "arguments": {
            "instance": record["instance"], "secret": record["secret"], "exit_id": self._exit_id}}
        request = Request(f"http://127.0.0.1:{record['port']}/v1/rpc",
                          json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=3) as response:
                value = json.load(response)
        except HTTPError:
            raise RuntimeError("scheduler_lifecycle_refused") from None
        except (ValueError, TypeError):
            raise RuntimeError("scheduler_lifecycle_invalid_reply") from None
        except OSError:
            if not _alive(record):
                return None
            raise RuntimeError("scheduler_lifecycle_unavailable") from None
        if (not isinstance(value, dict) or not value.get("ok")
                or not isinstance(value.get("result"), dict)):
            raise RuntimeError("scheduler_lifecycle_invalid_reply")
        result = value["result"]
        if (result.get("instance") != record["instance"]
                or result.get("pid") != record["pid"]
                or result.get("process_start") != record["process_start"]):
            raise RuntimeError("scheduler_lifecycle_identity_mismatch")
        return result

    def prepare(self, exit_id) -> int:
        if not isinstance(exit_id, str) or not exit_id or len(exit_id) > 256:
            raise ValueError("invalid_exit_id")
        if self._gate is not None:
            if self._exit_id != exit_id or self._committed:
                raise RuntimeError("scheduler_exit_in_progress")
            self.renew()
            return self._active_job_count
        self._exit_id = exit_id
        self._gate = acquire_start_gate(self.runtime_root, timeout=3.0)
        try:
            advance_start_epoch(self.runtime_root)
            paths = sorted(registry_directory(self.runtime_root).glob("*.json"))
            self._records = [record for path in paths
                             if (record := _read_registration(path, self.runtime_root)) is not None]
            count = 0
            for record in self._records:
                result = self._call(record, "lifecycle_prepare")
                if result is None:
                    continue
                if result.get("state") != "ready" or type(result.get("active_job_count")) is not int:
                    raise RuntimeError("scheduler_lifecycle_not_ready")
                self._prepared.append(record)
                count += result["active_job_count"]
            self._active_job_count = count
            return count
        except Exception:
            self.cancel()
            raise

    def renew(self):
        if self._committed:
            return
        for record in self._prepared:
            result = self._call(record, "lifecycle_renew")
            if result is not None and result.get("state") != "ready":
                raise RuntimeError("scheduler_exit_permit_expired")

    def commit(self):
        self.renew()
        # Once any commit starts, cancel must never resume a partially committed
        # group. An unavailable instance is reported while the fence is held.
        self._committed = True
        for record in self._prepared:
            result = self._call(record, "lifecycle_commit")
            if result is not None and result.get("accepted") is not True:
                raise RuntimeError("scheduler_exit_commit_refused")
            self._committed_instances.add(record["instance"])

    def pending(self) -> list[str]:
        return [record["instance"] for record in self._prepared if _alive(record)]

    def cancel(self):
        try:
            for record in reversed(self._prepared):
                if record["instance"] in self._committed_instances:
                    continue
                try:
                    self._call(record, "lifecycle_cancel")
                except RuntimeError:
                    # The ready lease also releases itself after its TTL.
                    pass
        finally:
            self._prepared = []
            self._records = []
            self._committed_instances.clear()
            self._committed = False
            self._active_job_count = 0
            self._exit_id = ""
            if self._gate is not None:
                self._gate.close()
                self._gate = None

    close = cancel
