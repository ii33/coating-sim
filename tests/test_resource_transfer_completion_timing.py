"""Worker completion and main-thread delivery are separate measured events."""
from threading import Event
import time
from typing import Any

import pytest
from PySide6.QtTest import QTest

from coat_sim.ui.project_resource_adapter import ProjectResourceTransferController
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


def test_transfer_wait_yields_worker_after_each_qt_event_pump(monkeypatch):
    calls = []

    class Controller:
        checks = 0

        @property
        def transferBusy(self):
            self.checks += 1
            return self.checks <= 2

    monkeypatch.setattr(QTest, "qWait", lambda value: calls.append(("qt", value)))
    monkeypatch.setattr(time, "sleep", lambda value: calls.append(("yield", value)))
    controller = Controller()
    wait_transfer(controller)
    assert calls == [("qt", 10), ("yield", 0), ("qt", 10), ("yield", 0)]
    assert controller.checks == 3


def test_transfer_wait_keeps_original_poll_count_and_timeout_failure(monkeypatch):
    calls = []

    class Controller:
        transferBusy = True
        transferStatus = "synthetic incomplete transfer"
        transferDiagnostics = {"synthetic": True}

    monkeypatch.setattr(QTest, "qWait", lambda value: calls.append(("qt", value)))
    monkeypatch.setattr(time, "sleep", lambda value: calls.append(("yield", value)))
    with pytest.raises(pytest.fail.Exception, match="Resource transfer did not finish") as caught:
        wait_transfer(Controller())
    assert calls == [("qt", 10), ("yield", 0)] * 1200
    assert "synthetic incomplete transfer" in str(caught.value)
    assert '"synthetic": true' in str(caught.value)
