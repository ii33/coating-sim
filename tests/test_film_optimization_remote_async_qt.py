"""Offline Qt lifecycle tests for remote film admission and cancellation."""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from coat_sim.scheduler.remote_client import RemoteSubmissionUnknown
from coat_sim.ui import compute_target as compute_target_module
from coat_sim.ui import film_optimization_quick_controller as controller_module
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from tests.film_structure_fixtures import fixture as film_fixture


DEVICE_ID = "cpu:default"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _Session:
    def __init__(self, design):
        self.design = design
        self.design_revision = 19
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


class _Source:
    def __init__(self, service, session):
        self.service = service
        self.session = session
        self.pendingEditEpoch = 3

    def schematicLayers(self, side):
        return [{"id": row["id"], "physicalNmValue": row["thickness"]["value"]}
                for row in self.session.design.get(side + "_layers", [])]


class _RemoteStore:
    def __init__(self, *, capabilities_gate=None, submit_gate=None, unknown=False,
                 device_available=True, cancel_failures=0):
        self.transport: SimpleNamespace | None = SimpleNamespace(remote_http=True, device_id=DEVICE_ID)
        self.capabilities_gate = capabilities_gate
        self.submit_gate = submit_gate
        self.unknown = unknown
        self.device_available = device_available
        self.cancel_failures = int(cancel_failures)
        self.capabilities_entered = threading.Event()
        self.submit_entered = threading.Event()
        self.cancel_called = threading.Event()
        self.cancel_succeeded = threading.Event()
        self.observer_entered = threading.Event()
        self.submit_records = []
        self.capability_threads = []
        self.submit_threads = []
        self.cancel_records = []
        self.result_calls = 0

    def capabilities(self):
        self.capability_threads.append(threading.get_ident())
        self.capabilities_entered.set()
        if self.capabilities_gate is not None:
            self.capabilities_gate.wait(5)
        return {
            "protocol_version": 1,
            "api_version": 1,
            "executor_schema_version": 1,
            "device_schema_version": 1,
            "device_binding_version": 1,
            "executors": ["film_optimization"],
            "devices": [{
                "id": DEVICE_ID,
                "name": "Stable CPU",
                "backend": "CPU",
                "available": self.device_available,
                "physical_id": "",
                "reason": "",
                "algorithms": ["fast"],
                "executors": ["film_optimization"],
            }],
        }

    def submit(self, spec, *, job_id, metadata):
        self.submit_threads.append(threading.get_ident())
        self.submit_records.append((spec, job_id, metadata))
        self.submit_entered.set()
        if self.unknown:
            raise RemoteSubmissionUnknown(
                job_id, expected_spec_sha256="a" * 64, device_id=DEVICE_ID)
        if self.submit_gate is not None:
            self.submit_gate.wait(5)
        return {"job_id": job_id, "state": "queued"}

    def cancel(self, job_id):
        self.cancel_records.append((threading.get_ident(), job_id))
        self.cancel_called.set()
        if self.cancel_failures:
            self.cancel_failures -= 1
            raise RuntimeError("controlled transient cancel failure")
        self.cancel_succeeded.set()
        return {"job_id": job_id, "cancel_requested": True}

    def wait_for_terminal(self, job_id, *, on_update=None, should_stop=None, wait_s=1.0):
        self.observer_entered.set()
        self.cancel_succeeded.wait(3)
        raise InterruptedError("controlled remote cancellation")

    def result(self, job_id):
        self.result_calls += 1
        raise RuntimeError("no replay-validated result")


@pytest.fixture
def harness(tmp_path, monkeypatch, app):
    service, _library, design = film_fixture(tmp_path)
    request = SimpleNamespace(
        design_snapshot=design,
        design_revision=19,
        objectives=(),
        variables=(),
        frozen_material_snapshot={"materials": []},
    )
    store_box: dict[str, _RemoteStore] = {}

    class _Target(compute_target_module.ComputeTarget):
        def client_for_submit(self, *, algorithm: str = "", client_factory=None):
            return store_box["store"], {"id": DEVICE_ID, "backend": "CPU", "available": True}

    monkeypatch.setattr(compute_target_module, "ComputeTarget", _Target)
    monkeypatch.setattr(controller_module, "freeze_spec", lambda *_args, **_kwargs: {"frozen": True})
    monkeypatch.setattr(controller_module, "freeze_film_optimization_request",
                        lambda _service, value: value)

    def create(store):
        store_box["store"] = store
        session = _Session(design)
        source = _Source(service, session)
        controller = FilmOptimizationQuickController(source)
        monkeypatch.setattr(controller, "_build_request", lambda **_kwargs: request)
        return controller

    return create, request


def _wait(app, predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    app.processEvents()
    assert predicate(), "timed out waiting for Qt worker delivery"


def _dispose(controller, app):
    if controller._run_coordinator.busy:
        controller.shutdown(1000)
    controller.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("local", [False, True])
def test_capabilities_get_is_off_gui_and_cancel_before_post_never_cancels_or_submits(
        harness, app, local):
    create, _request = harness
    release_capabilities = threading.Event()
    store = _RemoteStore(capabilities_gate=release_capabilities)
    if local:
        store.transport = None
    controller = create(store)
    gui_thread = threading.get_ident()
    closed = []
    controller.closeConfirmed.connect(lambda: closed.append(True))
    try:
        started = time.monotonic()
        assert controller.startOptimization()
        assert time.monotonic() - started < 0.5
        assert store.capabilities_entered.wait(1)
        assert store.capability_threads and store.capability_threads[0] != gui_thread
        assert controller.busy
        assert not controller.startOptimization()
        controller.requestClose()
        assert not store.submit_records
        assert not store.cancel_records
        release_capabilities.set()
        _wait(app, lambda: not controller.busy)
        assert closed == [True]
        assert not store.submit_records
        assert not store.cancel_records
        assert controller.queueJobId == ""
    finally:
        release_capabilities.set()
        _dispose(controller, app)


@pytest.mark.parametrize("local", [False, True])
def test_post_and_cancel_are_off_gui_and_cancel_waits_for_admission(harness, app, local):
    create, _request = harness
    release_submit = threading.Event()
    store = _RemoteStore(submit_gate=release_submit)
    if local:
        store.transport = None
    controller = create(store)
    gui_thread = threading.get_ident()
    closed = []
    controller.closeConfirmed.connect(lambda: closed.append(True))
    try:
        assert controller.startOptimization()
        assert store.submit_entered.wait(1)
        assert store.submit_threads and store.submit_threads[0] != gui_thread
        assert controller.queueJobId == store.submit_records[0][1]
        assert not controller.startOptimization()
        controller.requestClose()
        assert not store.cancel_records, "remote cancel must wait for POST admission"
        release_submit.set()
        assert store.cancel_called.wait(2)
        assert store.cancel_records[0][0] != gui_thread
        assert store.cancel_records[0][1] == store.submit_records[0][1]
        _wait(app, lambda: not controller.busy)
        assert closed == [True]
        assert len(store.submit_records) == 1
    finally:
        release_submit.set()
        _dispose(controller, app)


def test_lost_ack_keeps_unknown_id_visible_and_never_reposts_or_cancels(
        harness, app, monkeypatch):
    create, _request = harness
    store = _RemoteStore(unknown=True)

    def cannot_verify(client, error, **_kwargs):
        raise error

    monkeypatch.setattr(controller_module, "resolve_unknown_submission", cannot_verify)
    controller = create(store)
    gui_thread = threading.get_ident()
    try:
        assert controller.startOptimization()
        _wait(app, lambda: not controller.busy)
        assert len(store.submit_records) == 1
        job_id = store.submit_records[0][1]
        assert store.submit_threads[0] != gui_thread
        assert controller.queueJobId == job_id
        assert controller.resultTerminalState == "提交状态未知"
        assert job_id in controller.error
        assert "不会自动重复提交" in controller.error
        assert store.cancel_records == []
        assert store.result_calls == 0
    finally:
        _dispose(controller, app)


def test_changed_device_availability_blocks_post_after_capability_recheck(harness, app):
    create, _request = harness
    store = _RemoteStore(device_available=False)
    controller = create(store)
    gui_thread = threading.get_ident()
    try:
        assert controller.startOptimization()
        _wait(app, lambda: not controller.busy)
        assert store.capability_threads[0] != gui_thread
        assert not store.submit_records
        assert not store.cancel_records
        assert controller.queueJobId == ""
        assert "设备当前不可用" in controller.error
    finally:
        _dispose(controller, app)


@pytest.mark.parametrize("local", [False, True])
def test_remote_cancel_retry_can_be_retriggered_after_transient_failures(harness, app, local):
    create, _request = harness
    store = _RemoteStore(cancel_failures=3)
    if local:
        store.transport = None
    controller = create(store)
    gui_thread = threading.get_ident()
    try:
        assert controller.startOptimization()
        assert store.submit_entered.wait(1)
        controller.cancelOptimization()
        _wait(app, lambda: len(store.cancel_records) == 3, timeout=3)
        _wait(app, lambda: not controller._cancel_event._cancel_started, timeout=2)
        assert not store.cancel_succeeded.is_set()
        _wait(app, lambda: "cancel_request_unknown" in controller.error)
        assert "取消请求尚未确认" in controller.progressText
        # The coordinator must allow a second user click through to the event
        # after the first off-thread three-attempt retry budget is exhausted.
        controller.cancelOptimization()
        assert store.cancel_succeeded.wait(2)
        _wait(app, lambda: not controller.busy)
        assert [job_id for _thread_id, job_id in store.cancel_records] == [
            store.submit_records[0][1]] * 4
        assert all(thread_id != gui_thread for thread_id, _job_id in store.cancel_records)
    finally:
        _dispose(controller, app)


def test_remote_shutdown_does_not_block_on_inflight_post_or_destroy_live_worker(harness, app):
    create, _request = harness
    release_submit = threading.Event()
    store = _RemoteStore(submit_gate=release_submit)
    controller = create(store)
    try:
        assert controller.startOptimization()
        assert store.submit_entered.wait(1)
        pool = controller._run_coordinator._active_pool
        assert pool is type(pool).globalInstance()
        started = time.monotonic()
        assert controller.shutdown(5000) is False
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        assert controller._pool.parent() is controller._run_coordinator
        assert store.cancel_records == []
        # Drop the owning window while POST is still running. The orphaned
        # worker and pool must remain alive long enough to reconcile it.
        controller.deleteLater()
        app.processEvents()
        release_submit.set()
        assert pool.waitForDone(2000)
        assert store.cancel_called.is_set()
        assert store.cancel_records[0][0] != threading.get_ident()
    finally:
        release_submit.set()
        app.processEvents()


@pytest.mark.parametrize("change", ["target", "detach"])
def test_local_pending_admission_rejects_stale_binding(harness, app, change):
    create, _request = harness
    gate = threading.Event()
    store = _RemoteStore(capabilities_gate=gate)
    store.transport = None
    controller = create(store)
    try:
        assert controller.startOptimization()
        assert store.capabilities_entered.wait(1)
        if change == "target":
            controller._compute_target._device_id = "different-device"
        else:
            controller.detach_observers()
        gate.set()
        _wait(app, lambda: not controller.busy)
        assert not store.submit_records
        assert not store.cancel_records
    finally:
        gate.set()
        _dispose(controller, app)
