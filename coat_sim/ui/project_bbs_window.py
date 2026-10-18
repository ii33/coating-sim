"""Presentation-only BBS window adapter; content remains in the runtime owner."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtQml import QJSValue


class ProjectBbsWindowController(QObject):
    def __init__(self, workspace: Any, owner: Any, parent: QObject):
        super().__init__(parent)
        self.project_context = workspace
        self.owner = owner
        self.owner_binding = workspace.binding
        self.window: Any = None


def capture_bbs_snapshot(controller: Any, window: Any, module: str) -> tuple[dict | None, str | None]:
    """Read bounded existing QML state without disk access or field submission."""
    value = window.property("bbsSnapshot")
    if isinstance(value, QJSValue):
        value = value.toVariant()
    if not isinstance(value, dict):
        return None, "editor_unavailable"
    return {"summary": value, "collections": {}, "signature": value}, ""
