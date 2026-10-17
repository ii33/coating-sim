"""A disconnected launcher output stream cannot strand authenticated requests."""
from __future__ import annotations

from contextlib import redirect_stderr
from threading import Event
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication

from coat_sim.ui.control import UiControlError, UiControlServer


pytestmark = [pytest.mark.contract, pytest.mark.qt]


class FailingOutput:
    def __init__(self, error_type, phase):
        self.error_type, self.phase = error_type, phase
        self.failed = False

    def write(self, value):
        if self.phase in value:
            self.failed = True
            raise self.error_type("isolated disconnected diagnostic output")
        return len(value)

    def flush(self):
        pass


@pytest.mark.parametrize("operation", ["lifecycle_status", "open_window", "read_window", "list_windows"])
@pytest.mark.parametrize("error_type", [BrokenPipeError, OSError, ValueError])
@pytest.mark.parametrize("phase", ["begin", "end"])
def test_control_dispatch_completes_when_begin_or_end_output_fails(operation, error_type, phase):
    app = QCoreApplication.instance() or QCoreApplication([])
    calls = []
    def dispatch(op, arguments):
        calls.append((op, arguments))
        return {"accepted": True, "operation": op}
    server = UiControlServer(dispatch)
    envelope: dict[str, Any] = {"operation": operation, "arguments": {}, "done": Event(), "expired": False}
    stream = FailingOutput(error_type, phase)
    with redirect_stderr(stream):
        server._relay.receive(envelope)
    assert envelope["done"].is_set()
    assert envelope["reply"] == {"ok": True, "result": {"accepted": True, "operation": operation}}
    assert calls == [(operation, {})]
    assert stream.failed is (operation not in {"read_window", "list_windows"})
    server._relay.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("error", [UiControlError("stale_project", "keep the actual business failure"),
                                  RuntimeError("actual unexpected action failure")])
def test_end_log_failure_preserves_actual_action_error_and_done(error):
    app = QCoreApplication.instance() or QCoreApplication([])
    def dispatch(_operation, _arguments):
        raise error
    server = UiControlServer(dispatch)
    envelope: dict[str, Any] = {"operation": "open_window", "arguments": {}, "done": Event(), "expired": False}
    with redirect_stderr(FailingOutput(OSError, "end")):
        server._relay.receive(envelope)
    assert envelope["done"].is_set()
    assert envelope["reply"]["ok"] is False
    assert envelope["reply"]["error"] == {"code": getattr(error, "code", "ui_error"), "message": str(error)}
    server._relay.deleteLater()
    app.processEvents()
