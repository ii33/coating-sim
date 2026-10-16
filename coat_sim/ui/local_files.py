"""Helpers for safely converting Qt local-file URLs to filesystem paths."""

from __future__ import annotations

from PySide6.QtCore import QUrl


def local_file_path(url: QUrl) -> str:
    """Return a decoded local filesystem path, or an empty string otherwise."""
    try:
        if not isinstance(url, QUrl) or not url.isValid() or url.isEmpty() or not url.isLocalFile():
            return ""
        return url.toLocalFile() or ""
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""
