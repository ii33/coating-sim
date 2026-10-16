"""Responsive project admission while authenticated process registration settles."""
from __future__ import annotations

from typing import Callable


class ProjectApplicationAdmission:
    """Gate new writable project owners, without disrupting retained sessions.

    Only the local registration coordinator changes this gate after its
    authenticated client registration completes. Transport arguments must never
    assert admission readiness. The launcher checks ``decision`` before admitting
    a new project; activating an existing lifetime owner does not need this gate.
    A retry preserves the last successful registration until it actually fails.
    """

    CONNECTING = "正在连接共享助手，连接完成后可以创建或打开新项目。"
    EXITING = "应用正在协调退出，暂不能创建或打开新项目；稍后将自动重新连接。"
    STOPPED = "项目管理器已关闭。"

    def __init__(self, on_change: Callable[[str], None] | None = None):
        self._ready = False
        self._message = self.CONNECTING
        self._stopped = False
        self._on_change = on_change
        if on_change is not None:
            on_change(self._message)

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def message(self) -> str:
        return self._message

    def decision(self) -> tuple[bool, str]:
        return self._ready, self._message

    def _set(self, ready: bool, message: str) -> None:
        if (ready, message) == (self._ready, self._message):
            return
        self._ready, self._message = ready, message
        if self._on_change is not None:
            self._on_change(message)

    def registration_started(self) -> None:
        if self._stopped:
            return
        if not self._ready:
            self._set(False, self.CONNECTING)

    def registration_succeeded(self) -> None:
        if self._stopped:
            return
        self._set(True, "")

    def registration_failed(self, code: str, message: str) -> None:
        if self._stopped:
            return
        status = self.EXITING if code == "exit_in_progress" else f"共享助手连接失败，将自动重试：{message}"
        self._set(False, status)

    def stopped(self) -> None:
        self._stopped = True
        self._set(False, self.STOPPED)
