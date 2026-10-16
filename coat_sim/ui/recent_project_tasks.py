"""Serial background history I/O; QML reads only detached cached results."""
from __future__ import annotations

from concurrent.futures import Future
from queue import Empty, Full, Queue
from threading import Thread
from typing import Any, Callable


class RecentProjectTasks:
    """One daemon queue, independent settings objects and ordered mutations."""

    def __init__(self, factory: Callable[[], Any]):
        self._queue: Queue = Queue(maxsize=16)
        self._stopped = False
        self._thread = Thread(target=self._run, args=(self._queue, factory),
                              name="recent-project-history", daemon=True)
        self._thread.start()

    @staticmethod
    def _run(queue: Queue, factory: Callable[[], Any]) -> None:
        while True:
            task = queue.get()
            if task is None:
                return
            future, operation, arguments = task
            if not future.set_running_or_notify_cancel():
                continue
            try:
                history = factory()  # Create QSettings on this worker, never share it.
                if operation == "record_only":
                    history.record(*arguments)
                    future.set_result(None)
                else:
                    getattr(history, operation)(*arguments)
                    future.set_result(history.entries())
            except Exception as exc:
                future.set_exception(exc)

    def submit(self, operation: str, *arguments: Any) -> Future:
        future: Future = Future()
        if self._stopped:
            future.set_exception(RuntimeError("最近工程任务已关闭。"))
        else:
            try:
                self._queue.put_nowait((future, operation, arguments))
            except Full:
                future.set_exception(RuntimeError("最近工程操作过多，请稍后刷新重试。"))
        return future

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        # Drop only unstarted auxiliary metadata work. Never wait on GUI, and
        # always leave a sentinel even when the bounded queue was full.
        while True:
            try:
                task = self._queue.get_nowait()
            except Empty:
                break
            if task is not None:
                task[0].cancel()
        self._queue.put_nowait(None)
