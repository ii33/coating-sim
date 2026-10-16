"""Per-process, GUI-thread state machine for authenticated coordinated exit."""
from __future__ import annotations

import time
from typing import Callable, Literal
from uuid import uuid4

from .control import UiControlError

Decision = Literal["ready", "pending", "declined"]


class ProjectExitClient:
    """Retain a prepared decision until global commit, cancellation or lease expiry.

    Callbacks run on the owning GUI thread. ``prepare`` may show a nonblocking
    confirmation and return pending; repeated calls must not show duplicate
    dialogs. A ready callback must freeze mutation admission before returning.
    ``cancel`` releases that freeze and any unconsumed confirmation. Commit
    requests graceful shutdown only; it must not kill another process.
    """
    def __init__(self, generation: str, *, prepare: Callable[[], Decision],
                 commit: Callable[[], bool], cancel: Callable[[], None],
                 clock: Callable[[], float] = time.monotonic, lease_seconds: float = 120,
                 validate_ready: Callable[[], bool] | None = None):
        if not generation or lease_seconds <= 0:
            raise ValueError("A process generation and positive exit lease are required")
        self.generation = generation
        self._validate_ready = validate_ready
        self._prepare = prepare
        self._commit = commit
        self._cancel = cancel
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._exit_id = ""
        self._permit = ""
        self._deadline = 0.0
        self._state: str = "idle"

    @property
    def active(self) -> bool:
        return self._state in {"pending", "ready"}

    def expire(self) -> bool:
        if self._state == "ready" and self._clock() >= self._deadline:
            self._cancel()
            self._state = "declined"
            self._permit = ""
            return True
        return False

    def _identity(self, arguments: dict) -> str:
        if arguments.get("client_generation") != self.generation:
            raise UiControlError("stale_client", "退出请求的进程身份已经失效。")
        exit_id = arguments.get("exit_id")
        if not isinstance(exit_id, str) or not exit_id or len(exit_id) > 256:
            raise UiControlError("invalid_exit", "退出请求标识无效。")
        return exit_id

    def dispatch(self, operation: str, arguments: dict) -> dict:
        exit_id = self._identity(arguments)
        self.expire()
        if operation == "lifecycle_prepare":
            if self.active and self._exit_id != exit_id:
                raise UiControlError("exit_in_progress", "已有退出请求正在等待处理。")
            if self._exit_id != exit_id:
                self._exit_id, self._permit, self._state = exit_id, "", "pending"
            if self._state == "pending":
                try:
                    state = self._prepare()
                except Exception:
                    self._cancel()
                    self._state = "declined"
                    raise
                if state not in {"ready", "pending", "declined"}:
                    self._cancel()
                    self._state = "declined"
                    raise UiControlError("invalid_exit_state", "退出准备未返回有效状态。")
                self._state = state
                if state == "ready":
                    self._permit = uuid4().hex
                elif state == "declined":
                    self._cancel()
            if self._state == "ready" and self._validate_ready is not None:
                try:
                    valid = self._validate_ready()
                except Exception:
                    valid = False
                if not valid:
                    self._cancel()
                    self._state, self._permit = "declined", ""
            if self._state == "ready":
                self._deadline = self._clock() + self._lease_seconds
            return {"state": self._state, "permit_id": self._permit, "generation": self.generation}
        if exit_id != self._exit_id:
            raise UiControlError("stale_permit", "退出许可不属于当前请求。")
        permit = arguments.get("permit_id", "")
        if operation == "lifecycle_cancel":
            if self._state == "committed":
                return {"cancelled": False, "generation": self.generation}
            if permit != self._permit:
                raise UiControlError("stale_permit", "退出许可已失效。")
            if self.active:
                self._cancel()
            self._permit, self._state = "", "declined"
            return {"cancelled": True, "generation": self.generation}
        if operation == "lifecycle_commit":
            if self._state == "committed" and permit == self._permit:
                return {"accepted": True, "generation": self.generation}
            if self._state != "ready" or not permit or permit != self._permit:
                raise UiControlError("stale_permit", "退出许可未准备好或已失效。")
            try:
                accepted = self._commit()
            except Exception:
                self._cancel()
                self._permit, self._state = "", "declined"
                raise
            if accepted:
                self._state = "committed"
            else:
                self._cancel()
                self._permit, self._state = "", "declined"
            return {"accepted": accepted, "generation": self.generation}
        raise UiControlError("invalid_operation", "未知退出操作。")
