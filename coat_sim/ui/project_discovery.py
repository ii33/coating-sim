"""Background presentation-only discovery of same-user project owners."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer

from coat_sim.project_activation import remote_project_entries


class SharedProjectDiscovery(QObject):
    def __init__(self, client: Any, generation: str, changed: Callable[[], None], parent: QObject):
        super().__init__(parent)
        self.client, self.generation, self._changed = client, generation, changed
        self.entries: list[dict[str, Any]] = []
        self._stopped = False
        self._pending: Future | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="project-discovery")
        self._refresh = QTimer(self)
        self._refresh.setInterval(5000)
        self._refresh.timeout.connect(self.refresh)
        self._poll = QTimer(self)
        self._poll.setInterval(20)
        self._poll.timeout.connect(self._complete)

    def start(self) -> None:
        self._refresh.start()
        self.refresh()

    def refresh(self) -> None:
        if self._stopped or self._pending is not None:
            return
        self._pending = self._executor.submit(self.client.list_clients)
        self._poll.start()

    def _complete(self) -> None:
        if self._stopped or self._pending is None or not self._pending.done():
            return
        pending, self._pending = self._pending, None
        self._poll.stop()
        try:
            rows = remote_project_entries(pending.result(), self.generation)
        except Exception:
            # Preserve known owners on a transient outage, without claiming
            # they can currently respond or permitting another writer.
            rows = [dict(row, available=False) for row in self.entries]
        if rows != self.entries:
            self.entries = rows
            self._changed()

    def stop(self) -> None:
        self._stopped = True
        self._refresh.stop()
        self._poll.stop()
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        self._executor.shutdown(wait=False, cancel_futures=True)
