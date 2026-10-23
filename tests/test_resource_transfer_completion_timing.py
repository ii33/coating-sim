"""Worker completion and main-thread delivery are separate measured events."""
from threading import Event
from typing import Any

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
