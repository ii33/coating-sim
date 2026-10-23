"""Worker completion and main-thread delivery are separate measured events."""
from threading import Event
from typing import Any

import pytest

from coat_sim.ui.project_resource_adapter import ProjectResourceTransferController
from tests import test_project_resource_transfer_adapter as transfer_tests
from tests.test_project_resource_transfer_adapter import wait_transfer


def test_done_future_before_qt_poll_is_not_reported_as_delivered():
    controller: Any = ProjectResourceTransferController()
    completed = Event()
    assert controller._start_transfer_task("inspect", lambda: {"catalog_id": "synthetic", "rows": []})
    task = controller._transfer_tasks[controller._transfer_generation]
    task.future.add_done_callback(lambda _future: completed.set())
    assert completed.wait(3)
    before = controller.transferDiagnostics
    row = before["operations"][-1]
    assert controller.transferBusy
    assert row["future_callback_s"] >= row["worker_returned_s"] >= row["operation_finished_s"] >= row["worker_started_s"] >= 0
    assert "done_poll_s" not in row and "terminal_emit_started_s" not in row
    assert before["timer_active"] and before["pending_count"] == 1
    wait_transfer(controller)
    after = controller.transferDiagnostics
    row = after["operations"][-1]
    assert not controller.transferBusy and not after["timer_active"] and after["pending_count"] == 0
    assert row["terminal_emit_returned_s"] >= row["terminal_emit_started_s"] >= row["delivery_started_s"] >= row["done_poll_s"] >= row["future_callback_s"]
    assert row["snapshot_s"] >= row["terminal_emit_returned_s"]
    assert row["reply_type"] == "tuple" and row["reply_length"] == 3
    assert row["poll_count"] >= 1 and row["result_type"] == "dict"
    assert row["error_present"] is False
    controller.deleteLater()


def test_timing_history_is_bounded_and_contains_no_worker_payload():
    controller: Any = ProjectResourceTransferController()
    private = object()
    for _ in range(20):
        assert controller._start_transfer_task("preview", lambda: {"private_payload": private})
        wait_transfer(controller)
    diagnostics = controller.transferDiagnostics
    assert diagnostics["capacity"] == 16 and len(diagnostics["operations"]) == 16
    assert [row["generation"] for row in diagnostics["operations"]] == list(range(5, 21))
    for row in diagnostics["operations"]:
        assert "private_payload" not in row
        assert all(isinstance(v, (str, int, float, bool, type(None))) for v in row.values())
        assert row["terminal_emit_returned_s"] >= row["worker_started_s"]
    controller.deleteLater()


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self):
        for slot in tuple(self.slots):
            slot(7, "apply", True, "")


def _normal_wait_mock(monkeypatch, *, behavior="success", elapsed=.1, admitted_elapsed=0.):
    from types import SimpleNamespace

    events = []
    controller = SimpleNamespace(transferBusy=True, transferOperationFinished=_Signal(),
        transferStatus="synthetic incomplete transfer",
        transferDiagnostics={"operations": [{"generation": 7, "snapshot_s": admitted_elapsed}]})
    timers = []

    class Timer:
        def __init__(self):
            self.timeout = _Signal()
            timers.append(self)

        def setSingleShot(self, value):
            events.append(("single_shot", value))

        def setTimerType(self, value):
            events.append(("timer_type", value))

        def start(self, milliseconds):
            events.append(("deadline_ms", milliseconds))

        def stop(self):
            events.append(("stop", True))

    class Loop:
        def exec(self):
            events.append(("normal_exec", True))
            if behavior == "success":
                controller.transferBusy = False
                controller.transferOperationFinished.emit()
                controller.transferDiagnostics["operations"][0]["terminal_emit_returned_s"] = admitted_elapsed + elapsed
            elif behavior == "timeout":
                for callback in tuple(timers[0].timeout.slots):
                    callback()
            else:
                raise RuntimeError("synthetic loop exception")

        def quit(self):
            events.append(("quit", True))

    times = iter([10., 10. + elapsed])
    monkeypatch.setattr(transfer_tests, "QEventLoop", Loop)
    monkeypatch.setattr(transfer_tests, "QTimer", Timer)
    monkeypatch.setattr(transfer_tests.time, "perf_counter", lambda: next(times))
    return controller, events


def test_transfer_wait_uses_normal_exec_and_precise_twelve_second_deadline(monkeypatch):
    controller, events = _normal_wait_mock(monkeypatch)
    wait_transfer(controller)
    assert ("deadline_ms", 12000) in events
    assert ("timer_type", transfer_tests.Qt.TimerType.PreciseTimer) in events
    assert events.count(("normal_exec", True)) == 1
    assert ("stop", True) in events
    assert controller.transferOperationFinished.slots == []
    assert not controller.transferBusy


def test_transfer_wait_deadline_includes_already_elapsed_admission_time(monkeypatch):
    controller, events = _normal_wait_mock(monkeypatch, admitted_elapsed=2.)
    wait_transfer(controller)
    assert ("deadline_ms", 10000) in events
    assert controller.transferOperationFinished.slots == []


def test_transfer_wait_timeout_keeps_failure_and_timing_evidence(monkeypatch):
    controller, events = _normal_wait_mock(monkeypatch, behavior="timeout", elapsed=12.)
    with pytest.raises(pytest.fail.Exception, match="Resource transfer did not finish") as caught:
        wait_transfer(controller)
    assert ("deadline_ms", 12000) in events and ("stop", True) in events
    assert controller.transferBusy  # Do not cancel/resubmit or privately deliver.
    assert controller.transferOperationFinished.slots == []
    assert "synthetic incomplete transfer" in str(caught.value)
    assert '\"generation\": 7' in str(caught.value)


def test_transfer_wait_late_success_never_passes_after_busy_cleared(monkeypatch):
    controller, events = _normal_wait_mock(monkeypatch, elapsed=12.1)
    with pytest.raises(pytest.fail.Exception, match="within 12 seconds"):
        wait_transfer(controller)
    assert not controller.transferBusy and ("stop", True) in events
    assert controller.transferOperationFinished.slots == []


def test_transfer_wait_already_delivered_late_operation_still_fails(monkeypatch):
    controller, events = _normal_wait_mock(monkeypatch)
    controller.transferBusy = False
    controller.transferDiagnostics["operations"][0]["terminal_emit_returned_s"] = 12.1
    with pytest.raises(pytest.fail.Exception, match="within 12 seconds"):
        wait_transfer(controller)
    assert events == []


def test_transfer_wait_exhausted_admission_deadline_does_not_start_fresh_loop(monkeypatch):
    controller, events = _normal_wait_mock(monkeypatch, admitted_elapsed=12.1)
    with pytest.raises(pytest.fail.Exception, match="within 12 seconds"):
        wait_transfer(controller)
    assert events == [] and controller.transferBusy


def test_transfer_wait_loop_exception_disconnects_and_stops_watchdog(monkeypatch):
    controller, events = _normal_wait_mock(monkeypatch, behavior="exception")
    with pytest.raises(RuntimeError, match="synthetic loop exception"):
        wait_transfer(controller)
    assert ("stop", True) in events
    assert controller.transferOperationFinished.slots == []


def test_transfer_wait_exact_twelve_second_terminal_is_not_treated_as_late(monkeypatch):
    controller, events = _normal_wait_mock(monkeypatch, elapsed=12.)
    wait_transfer(controller)
    assert not controller.transferBusy and ("stop", True) in events
    assert controller.transferOperationFinished.slots == []
