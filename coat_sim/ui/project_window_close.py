"""Observe native project Close events without confusing hide with close."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer


class ProjectWindowCloseFilter(QObject):
    def __init__(self, desktop: Any, route: Any, window: Any):
        super().__init__(desktop)
        self.desktop, self.route, self.window = desktop, route, window
        self._replaying = False
        window.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self.window or event.type() != QEvent.Type.Close or self._replaying:
            return False
        desktop, route = self.desktop, self.route
        view = desktop._views.get(route)
        if view is None or view[0] is not watched:
            return False
        closer = desktop.project_closer
        if route.binding in desktop._authorized_project_closes or closer is None:
            return self._forward_close(watched, event)
        if closer.is_pending(route.binding):
            event.ignore()
            return True
        peers = [other for other in desktop._views if other.binding == route.binding]
        if len(peers) == 1:
            # Let the current native event unwind before an authorized close
            # calls window.close() again; cancellation keeps this view alive.
            event.ignore()
            # A resource's local Save/Keep decision may suppress its own
            # dialog. It is not authorization to discard sibling project drafts.
            watched.setProperty("projectCloseAuthorized", False)
            closer.request_last_window(route, watched)
            return True
        return self._forward_close(watched, event)

    def _forward_close(self, watched: QObject, event: QEvent) -> bool:
        desktop, route = self.desktop, self.route
        desktop._close_events.add(route)
        self._replaying = True
        try:
            # Dispatch once while the original event is still alive, so QML's
            # actual accepted/rejected decision is observable without retaining
            # an expired C++ event or waiting for a visibility transition.
            QCoreApplication.sendEvent(watched, event)
        finally:
            self._replaying = False
        if event.isAccepted():
            def finish() -> None:
                view = desktop._views.get(route)
                if view is not None and view[0] is watched:
                    desktop._release_hidden(route)
                desktop._close_events.discard(route)
            QTimer.singleShot(0, finish)
        else:
            desktop._close_events.discard(route)
        return True
