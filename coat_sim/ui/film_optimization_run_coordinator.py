"""Per-window lifecycle for one admitted film-optimization queue attempt."""
from __future__ import annotations

from threading import Lock
from typing import Callable
import weakref

from PySide6.QtCore import Qt, QObject, QThreadPool, Signal, Slot


_REMOTE_WORKER_KEEPALIVE = []
_REMOTE_KEEPALIVE_LOCK = Lock()


def _release_remote_worker(worker) -> None:
    with _REMOTE_KEEPALIVE_LOCK:
        _REMOTE_WORKER_KEEPALIVE[:] = [
            item for item in _REMOTE_WORKER_KEEPALIVE if item is not worker]


def _retain_remote_worker(worker) -> None:
    with _REMOTE_KEEPALIVE_LOCK:
        if any(item is worker for item in _REMOTE_WORKER_KEEPALIVE):
            return
        _REMOTE_WORKER_KEEPALIVE.append(worker)
    worker_ref = weakref.ref(worker)

    def release_after_run(*_args):
        current = worker_ref()
        if current is not None:
            _release_remote_worker(current)

    # The global thread pool outlives this window. Keep the QRunnable alive if
    # the controller is destroyed while it is queued/running, then release it
    # directly from its own terminal signal without depending on the window.
    worker.signals.finished.connect(release_after_run, Qt.ConnectionType.DirectConnection)


class FilmOptimizationRunStartError(RuntimeError):
    """A worker could not start after its queue submission was accepted."""


class FilmOptimizationRunPreparationError(RuntimeError):
    """A worker could not be prepared before queue submission."""


class FilmOptimizationRunCoordinator(QObject):
    """Own attempt identity, cancellation, worker delivery, and close lifecycle."""

    progress = Signal(int, object)
    jobIdChanged = Signal(int, str)
    finished = Signal(int, object, object, object)

    def __init__(self, parent: QObject | None = None, *,
                 service_owned: bool = False) -> None:
        super().__init__(parent)
        self._service_owned = bool(service_owned)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._active_pool = self._pool
        self._worker = None
        self._cancel_event = None
        self._busy = False
        self._operation_serial = 0
        self._active_operation_id: int | None = None
        self._operation_edit_epoch: int | None = None
        self._cancel_requested = False
        self._pending_close = False
        self._shutting_down = False
        self._remote_attempt = False

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def operation_serial(self) -> int:
        return self._operation_serial

    @property
    def active_operation_id(self) -> int | None:
        return self._active_operation_id

    @property
    def operation_edit_epoch(self) -> int | None:
        return self._operation_edit_epoch

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    @property
    def pending_close(self) -> bool:
        return self._pending_close

    @property
    def worker(self):
        return self._worker

    @property
    def cancel_event(self):
        return self._cancel_event

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    def start(self, *, submit: Callable[[], None] | None,
              worker_factory: Callable[[int], object],
              edit_epoch: int | None) -> int | None:
        """Prepare an attempt; local admission may stay synchronous, remote admission may run in the worker."""
        if self._busy or self._shutting_down:
            return None
        operation_id = self._operation_serial + 1
        try:
            worker = worker_factory(operation_id)
            worker.signals.progress.connect(self._on_worker_progress)
            worker.signals.finished.connect(self._on_worker_finished)
            job_id_signal = getattr(worker.signals, "jobIdChanged", None)
            if job_id_signal is not None:
                job_id_signal.connect(self._on_worker_job_id_changed)
        except Exception as exc:
            raise FilmOptimizationRunPreparationError(
                f"worker preparation failed before queue submission: {exc}") from exc
        if submit is not None:
            submit()
        self._operation_serial = operation_id
        self._active_operation_id = operation_id
        self._operation_edit_epoch = edit_epoch
        self._cancel_requested = False
        self._pending_close = False
        self._cancel_event = getattr(worker, "cancel_event", None)
        self._remote_attempt = bool(getattr(self._cancel_event, "_remote", False))
        self._active_pool = (QThreadPool.globalInstance()
                             if self._remote_attempt or self._service_owned else self._pool)
        if self._remote_attempt or self._service_owned:
            _retain_remote_worker(worker)
        self._worker = worker
        self._busy = True
        try:
            self._active_pool.start(worker)
        except Exception as exc:
            self._cancel_requested = True
            cancel_error = self._request_cancel_transport()
            self._active_operation_id = None
            self._worker = None
            self._cancel_event = None
            self._remote_attempt = False
            self._active_pool = self._pool
            if getattr(getattr(worker, "cancel_event", None), "_remote", False):
                _release_remote_worker(worker)
            self._busy = False
            detail = f"worker start failed after queue submission: {exc}"
            if cancel_error is not None:
                detail += f"; queue cancellation also failed: {cancel_error}"
            raise FilmOptimizationRunStartError(detail) from exc
        return operation_id

    @Slot(int, object)
    def _on_worker_progress(self, operation_id: int, progress: object) -> None:
        if self._shutting_down or not self._busy:
            return
        if operation_id != self._active_operation_id:
            return
        self.progress.emit(operation_id, progress)

    @Slot(int, str)
    def _on_worker_job_id_changed(self, operation_id: int, job_id: str) -> None:
        if self._shutting_down or not self._busy:
            return
        if operation_id != self._active_operation_id:
            return
        self.jobIdChanged.emit(operation_id, job_id)

    @Slot(int, object, object, object)
    def _on_worker_finished(self, operation_id: int, result, baseline_rows, error) -> None:
        if self._shutting_down or not self._busy:
            return
        if operation_id != self._active_operation_id:
            return
        # This accepted terminal delivery is one-shot: state is cleared before
        # the controller handles the result, and repeated/old worker signals
        # fail the active ID + busy gates above.
        self._active_operation_id = None
        self._worker = None
        self._busy = False
        self._cancel_event = None
        self._remote_attempt = False
        self._active_pool = self._pool
        self.finished.emit(operation_id, result, baseline_rows, error)

    def _request_cancel_transport(self) -> Exception | None:
        event = self._cancel_event
        if event is None:
            return None
        # Local cancel is synchronous and remains one-shot. A remote event
        # de-duplicates in-flight requests but must be called again after its
        # retry budget is exhausted so the user can explicitly retry.
        if event.is_set() and not bool(getattr(event, "_remote", False)):
            return None
        try:
            event.set()
        except Exception as exc:
            return exc
        return None

    def request_cancel(self) -> Exception | None:
        """Record intent now and retry transport until its first success."""
        if not self._busy or self._cancel_event is None:
            return None
        self._cancel_requested = True
        return self._request_cancel_transport()

    def request_close(self) -> Exception | None:
        if not self._busy:
            return None
        self._pending_close = True
        return self.request_cancel()

    def take_pending_close(self) -> bool:
        pending, self._pending_close = self._pending_close, False
        return pending

    def detach_service_observer(self) -> None:
        """Dispose a client without cancelling the scheduler's admitted job."""
        detach = getattr(self._worker, "detach_observer", None)
        if callable(detach):
            detach()
        self._shutting_down = True
        self._active_operation_id = None
        self._busy = False

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        if self._service_owned:
            self.detach_service_observer()
            return True
        self._shutting_down = True
        self._cancel_requested = True
        self._request_cancel_transport()
        self._active_operation_id = None
        self._busy = False
        if self._remote_attempt:
            # Remote work uses Qt's process-wide pool, so closing its window
            # never destroys a pool with an in-flight HTTP call or waits here.
            return self._worker is None
        return self._pool.waitForDone(max(0, int(timeout_ms)))
