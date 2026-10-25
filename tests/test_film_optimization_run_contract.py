"""Behavioral contracts for one film-optimization window's active attempt.

These tests use the real Qt controller with a held fake worker and scheduler
client. They exercise request ownership and lifecycle gates without starting a
queue daemon or running an optical optimization.
"""
from __future__ import annotations

import os
from threading import Event
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QRunnable, Signal, QTimer, QCoreApplication

from coat_sim.core.film_optimization import FilmOptimizationResult
from coat_sim.ui import film_optimization_quick_controller as module
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from tests.film_structure_fixtures import fixture, request as film_request
from tests.test_film_optimization_needle import _objective


class _Signals(QObject):
    progress = Signal(int, object)
    finished = Signal(int, object, object, object)


class _HeldWorker(QRunnable):
    instances = []

    def __init__(self, service, request, store, job_id, cancel_event, operation_id,
                 *, remote_admission=None, project_compute=None):
        super().__init__()
        self.service = service
        self.request = request
        self.store = store
        self.job_id = job_id
        self.cancel_event = cancel_event
        self.operation_id = operation_id
        self.remote_admission = remote_admission
        self.project_compute = project_compute
        self.signals = _Signals()
        self.setAutoDelete(False)
        self.__class__.instances.append(self)

    def admit(self):
        # A deterministic fake pool performs the worker admission phase here;
        # actual thread affinity is covered by the gated async Qt tests.
        try:
            self.remote_admission()
            self.admitted = True
        except Exception as exc:
            message = str(exc)
            QTimer.singleShot(0, lambda: self.finish(error={"state": "failed", "message": message}))

    def finish(self, result=None, error=None):
        self.signals.finished.emit(self.operation_id, result, [], error)


class _Store:
    def __init__(self):
        self.submissions = []
        self.cancel_attempts = []
        self.cancellations = []
        self.fail_submit = False
        self.fail_cancel_once = False

    def capabilities(self):
        return {"protocol_version": 1, "executors": ["film_optimization"]}

    def submit(self, spec, *, job_id, metadata):
        if self.fail_submit:
            raise ValueError("controlled submit failure")
        self.submissions.append((spec, job_id, metadata))

    def cancel(self, job_id):
        self.cancel_attempts.append(job_id)
        if self.fail_cancel_once:
            self.fail_cancel_once = False
            raise RuntimeError("controlled cancel transport failure")
        self.cancellations.append(job_id)
        return {"job_id": job_id, "cancel_requested": True}


class _Session:
    def __init__(self, design, revision):
        self.design = design
        self.design_revision = revision
        self.path = ""

    def snapshot(self):
        return SimpleNamespace(
            design_revision=self.design_revision,
            design=self.design,
            path=self.path,
        )

    def subscribe(self, callback):
        self._subscriber = callback
        return lambda: setattr(self, "_subscriber", None)


class _Source(QObject):
    pendingEditEpochChanged = Signal()
    closeConfirmed = Signal()

    def __init__(self, service, session):
        super().__init__()
        self.service = service
        self.session = session
        self.pendingEditEpoch = 4
        self.applied = []

    def applyOptimizedDesign(self, candidate, revision, edit_epoch):
        self.applied.append((candidate, revision, edit_epoch))
        return (revision == self.session.design_revision
                and edit_epoch == self.pendingEditEpoch)

    def schematicLayers(self, side):
        return [{"id": row["id"], "physicalNmValue": row["thickness"]["value"]}
                for row in self.session.design.get(side + "_layers", [])]


@pytest.fixture
def attempt_harness(tmp_path, monkeypatch):
    service, _library, design = fixture(tmp_path)
    request = film_request(design, [_objective()], bounds=(90.0, 110.0),
                           max_iterations=2, max_evaluations=4)
    stores = []

    def make_store(root):
        store = _Store()
        stores.append(store)
        return store

    monkeypatch.setattr(module, "SchedulerClient", make_store)
    monkeypatch.setattr(module, "_FilmOptimizationTask", _HeldWorker)
    _HeldWorker.instances.clear()

    def make_controller():
        session = _Session(design, request.design_revision)
        source = _Source(service, session)
        controller = FilmOptimizationQuickController(source)
        monkeypatch.setattr(controller, "_build_request", lambda **_kwargs: request)
        controller._pool.start = lambda worker: worker.admit()
        from coat_sim.ui import film_optimization_run_coordinator as coordinator_module
        monkeypatch.setattr(coordinator_module.QThreadPool, "globalInstance",
                            lambda: controller._pool)
        return controller, source

    return make_controller, stores, request


def _completed_result(request):
    return FilmOptimizationResult(
        state="completed",
        design_revision=request.design_revision,
        best_design=request.design_snapshot,
        best_thickness_nm={},
        max_normalized_error=0.0,
        weighted_max_normalized_error=0.0,
        weighted_rms_error=0.0,
        targets_met=True,
        per_condition_errors=(),
        iterations=1,
        evaluations=1,
        message="complete",
        validation_complete=True,
    )


def _wait(predicate):
    until = time.monotonic() + 3
    while not predicate() and time.monotonic() < until:
        QCoreApplication.processEvents()
        time.sleep(.005)
    assert predicate()


def _dispose(controller):
    if controller._worker is not None:
        controller._worker.finish(error={"state": "cancelled"})
    controller.shutdown(1000)
    controller.deleteLater()


def test_busy_attempt_submits_once_and_cancel_retry_then_close_is_idempotent(
        attempt_harness):
    make_controller, stores, _request = attempt_harness
    controller, _source = make_controller()
    closed = []
    controller.closeConfirmed.connect(lambda: closed.append(True))
    try:
        assert controller._run_coordinator.parent() is controller
        assert controller._pool.parent() is controller._run_coordinator
        assert controller.startOptimization()
        assert _HeldWorker.instances[-1].signals.thread() is controller.thread()
        assert not controller.startOptimization()
        assert len(stores[0].submissions) == 1

        stores[0].fail_cancel_once = True
        controller.cancelOptimization()
        assert controller._cancel_requested
        assert controller._cancel_event is not None
        assert controller._cancel_event.is_set()
        _wait(lambda: len(stores[0].cancel_attempts) >= 1)
        result = _completed_result(_request)
        controller._result_application_eligible = True
        assert not controller._apply_completed_result(
            controller._active_operation_id, result)
        assert controller._source_controller.applied == []

        controller.cancelOptimization()
        _wait(lambda: bool(stores[0].cancellations))
        assert stores[0].cancel_attempts == [
            controller._queue_job_id, controller._queue_job_id]
        _wait(lambda: bool(stores[0].cancellations))
        assert stores[0].cancellations == [controller._queue_job_id]
        assert controller._error == ""

        controller.requestClose()
        controller.requestClose()
        assert len(stores[0].cancel_attempts) == 2
        _wait(lambda: bool(stores[0].cancellations))
        assert stores[0].cancellations == [controller._queue_job_id]
        assert controller._pending_close
        assert controller._busy
        _HeldWorker.instances[-1].finish(error={"state": "cancelled"})
        assert closed == [True]
        assert not controller._pending_close
        assert not controller._busy
    finally:
        _dispose(controller)


@pytest.mark.parametrize("invalidate", ["revision", "pending_edit", "source_closed"])
def test_late_completion_after_source_change_never_writes_back(
        attempt_harness, invalidate):
    make_controller, _stores, request = attempt_harness
    controller, source = make_controller()
    try:
        assert controller.startOptimization()
        worker = _HeldWorker.instances[-1]
        if invalidate == "revision":
            source.session.design_revision += 1
        elif invalidate == "pending_edit":
            source.pendingEditEpoch += 1
        else:
            source.closeConfirmed.emit()
            assert controller._source_closed
            assert controller._source_connections == []

        worker.finish(_completed_result(request))
        assert source.applied == []
        if invalidate != "source_closed":
            assert controller.candidateStale
    finally:
        _dispose(controller)


def test_old_attempt_result_and_other_window_attempt_cannot_cross_write(
        attempt_harness):
    make_controller, stores, request = attempt_harness
    first, first_source = make_controller()
    second, second_source = make_controller()
    try:
        assert first.startOptimization()
        first_worker = _HeldWorker.instances[-1]
        assert second.startOptimization()
        second_worker = _HeldWorker.instances[-1]
        assert first._active_operation_id == second._active_operation_id == 1
        assert len(stores) == 2
        assert len(stores[0].submissions) == len(stores[1].submissions) == 1

        # End attempt 1, then start attempt 2 in the same controller. Replaying
        # attempt 1's completion after attempt 2 starts must leave attempt 2
        # busy and its candidate state untouched.
        first_worker.finish(error={"state": "cancelled", "message": "cancelled"})
        assert not first._busy
        assert first.startOptimization()
        next_worker = _HeldWorker.instances[-1]
        assert next_worker is not first_worker
        assert first._active_operation_id == 2
        first_worker.finish(_completed_result(request))
        assert first._busy
        assert first._active_operation_id == 2
        assert first._result is None
        assert first_source.applied == []
        assert second_source.applied == []
        assert second._busy

        second_worker.finish(_completed_result(request))
        assert second_source.applied
        assert first_source.applied == []
        next_worker.finish(_completed_result(request))
        assert first_source.applied
    finally:
        _dispose(first)
        _dispose(second)


def test_close_during_run_keeps_completed_candidate_from_auto_apply(attempt_harness):
    make_controller, stores, request = attempt_harness
    controller, source = make_controller()
    close_prompts = []
    controller.closeConfirmationRequested.connect(lambda: close_prompts.append(True))
    try:
        assert controller.startOptimization()
        controller.requestClose()
        assert controller._pending_close
        _wait(lambda: bool(stores[0].cancellations))
        assert stores[0].cancellations == [controller._queue_job_id]

        _HeldWorker.instances[-1].finish(_completed_result(request))

        assert not controller._busy
        assert not controller._pending_close
        assert source.applied == []
        assert controller.hasCandidate
        assert not controller._candidate_applied
        assert len(close_prompts) == 1
    finally:
        _dispose(controller)


def test_shutdown_ignores_late_worker_completion(attempt_harness):
    make_controller, _stores, request = attempt_harness
    controller, source = make_controller()
    try:
        assert controller.startOptimization()
        worker = _HeldWorker.instances[-1]

        assert not controller.shutdown(1000), "an admitted observer may still be in flight"
        assert controller._shutting_down
        assert not controller._busy
        assert controller._active_operation_id is None

        worker.finish(_completed_result(request))

        assert controller._result is None
        assert source.applied == []
    finally:
        _dispose(controller)


def test_duplicate_terminal_signal_is_one_shot(attempt_harness):
    make_controller, _stores, request = attempt_harness
    controller, source = make_controller()
    try:
        assert controller.startOptimization()
        worker = _HeldWorker.instances[-1]
        result = _completed_result(request)

        worker.finish(result)
        assert source.applied
        assert controller._result is result
        assert controller._result_terminal_state == "已完成"

        worker.finish(error={"state": "cancelled", "message": "late duplicate"})

        assert controller._result is result
        assert controller._result_terminal_state == "已完成"
        assert len(source.applied) == 1
    finally:
        _dispose(controller)


@pytest.mark.parametrize("failure", ["factory", "connect"])
def test_worker_preparation_failure_never_submits_or_installs_attempt(
        attempt_harness, monkeypatch, failure):
    make_controller, stores, _request = attempt_harness
    controller, _source = make_controller()

    if failure == "factory":
        def fail_factory(*_args, **_kwargs):
            raise RuntimeError("controlled worker factory failure")

        monkeypatch.setattr(module, "_FilmOptimizationTask", fail_factory)
    else:
        class BadSignals:
            def connect(self, _slot):
                raise RuntimeError("controlled signal connection failure")

        class BadWorker:
            def __init__(self, *_args, **_kwargs):
                self.cancel_event = Event()
                self.signals = SimpleNamespace(
                    progress=BadSignals(), finished=BadSignals())

        monkeypatch.setattr(module, "_FilmOptimizationTask", BadWorker)
    try:
        assert not controller.startOptimization()
        assert stores[0].submissions == []
        assert not controller._busy
        assert controller._worker is None
        assert controller._active_operation_id is None
        assert "prepare film optimization worker" in controller._error
        expected_cause = ("controlled worker factory failure" if failure == "factory"
                          else "controlled signal connection failure")
        assert expected_cause in controller._error
    finally:
        _dispose(controller)


def test_async_submit_failure_retires_busy_attempt(attempt_harness, monkeypatch):
    make_controller, stores, _request = attempt_harness
    controller, _source = make_controller()
    # The factory's first store is created only by startOptimization.
    # Make its submit fail at the scheduler boundary.
    original_factory = module.SchedulerClient

    def failing_factory(root):
        store = original_factory(root)
        assert isinstance(store, _Store)
        store.fail_submit = True
        return store

    monkeypatch.setattr(module, "SchedulerClient", failing_factory)
    try:
        assert controller.startOptimization()
        _wait(lambda: not controller._busy)
        assert controller._worker is None
        assert controller._active_operation_id is None
        assert "controlled submit failure" in controller._error
    finally:
        _dispose(controller)


@pytest.mark.parametrize("cancel_fails", [False, True])
def test_worker_start_failure_never_submits_or_cancels(
        attempt_harness, monkeypatch, cancel_fails):
    make_controller, stores, _request = attempt_harness
    controller, _source = make_controller()
    original_factory = module.SchedulerClient

    def configured_store(root):
        store = original_factory(root)
        assert isinstance(store, _Store)
        store.fail_cancel_once = cancel_fails
        return store

    monkeypatch.setattr(module, "SchedulerClient", configured_store)
    controller._pool.start = lambda _worker: (_ for _ in ()).throw(
        RuntimeError("controlled worker start failure"))
    try:
        assert not controller.startOptimization()
        assert stores[0].submissions == []
        assert stores[0].cancel_attempts == []
        assert stores[0].cancellations == []
        assert not controller._busy
        assert controller._worker is None
        assert controller._active_operation_id is None
        assert "worker start failed" in controller._error
        assert "controlled worker start failure" in controller._error
    finally:
        _dispose(controller)
