"""Coordinated exit never closes a client before global commit."""

import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from coat_sim.ui.project_application_lifecycle import ProjectApplicationLifecycle

pytestmark = [pytest.mark.contract, pytest.mark.qt]


class Bridge(QObject):
    launchError = Signal(str)
    shutdownConfirmationRequested = Signal()

    def __init__(self):
        super().__init__()
        self.project_launcher = None
        self._windows = {}
        self._launcher_window = None
        self._closing = False
        self._shutdown_confirmation_pending = False
        self._project_shutdown_approved = False
        self.dirty = True

    def _uncommitted_project_view_draft(self):
        return False

    def _dirty_project_sessions(self):
        return ["draft"] if self.dirty else []

    def shutdown(self):
        self._closing = True


def test_confirm_waits_for_global_commit_and_cancel_preserves_client():
    QCoreApplication.instance() or QCoreApplication([])
    bridge = Bridge()
    coordinator = ProjectApplicationLifecycle(bridge, "generation")
    signals = []
    bridge.shutdownConfirmationRequested.connect(lambda: signals.append(True))
    args = {"client_generation": "generation", "exit_id": "one"}
    assert coordinator.dispatch("lifecycle_prepare", args)["state"] == "pending"
    assert coordinator.dispatch("lifecycle_prepare", args)["state"] == "pending"
    assert len(signals) == 1
    coordinator.decide(True)
    ready = coordinator.dispatch("lifecycle_prepare", args)
    assert ready["state"] == "ready"
    assert not bridge._closing
    coordinator.dispatch("lifecycle_cancel", args | {"permit_id": ready["permit_id"]})
    assert not bridge._closing
    assert not bridge._project_shutdown_approved
    args["exit_id"] = "two"
    assert coordinator.dispatch("lifecycle_prepare", args)["state"] == "pending"
    coordinator.decide(True)
    ready = coordinator.dispatch("lifecycle_prepare", args)
    assert coordinator.dispatch("lifecycle_commit", args | {"permit_id": ready["permit_id"]})["accepted"]
    assert bridge._closing
    coordinator._timer.stop()


def test_declined_confirmation_never_closes():
    QCoreApplication.instance() or QCoreApplication([])
    bridge = Bridge()
    coordinator = ProjectApplicationLifecycle(bridge, "generation")
    args = {"client_generation": "generation", "exit_id": "one"}
    coordinator.dispatch("lifecycle_prepare", args)
    coordinator.decide(False)
    assert coordinator.dispatch("lifecycle_prepare", args)["state"] == "declined"
    assert not bridge._closing
    coordinator._timer.stop()
