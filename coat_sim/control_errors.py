"""Shared control errors without importing a presentation package or runtime."""
from __future__ import annotations


class UiControlError(ValueError):
    """Stable code/message contract re-exported by the historical UI module."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
