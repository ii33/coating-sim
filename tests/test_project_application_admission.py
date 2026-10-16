"""New project owners wait for registration; retained owners stay independent."""
from __future__ import annotations

import pytest

from coat_sim.ui.project_application_admission import ProjectApplicationAdmission

pytestmark = pytest.mark.contract


def test_initial_registration_blocks_new_admission_with_visible_status() -> None:
    messages: list[str] = []
    gate = ProjectApplicationAdmission(messages.append)
    assert gate.decision() == (False, gate.CONNECTING)
    gate.registration_started()
    assert messages == [gate.CONNECTING]
    gate.registration_succeeded()
    assert gate.decision() == (True, "")
    assert messages == [gate.CONNECTING, ""]


def test_successful_heartbeat_does_not_transiently_close_admission() -> None:
    messages: list[str] = []
    gate = ProjectApplicationAdmission(messages.append)
    gate.registration_succeeded()
    gate.registration_started()
    gate.registration_succeeded()
    assert gate.decision() == (True, "")
    assert messages == [gate.CONNECTING, ""]


def test_registration_failure_and_exit_race_are_visible_and_retryable() -> None:
    gate = ProjectApplicationAdmission()
    gate.registration_succeeded()
    gate.registration_failed("assistant_unavailable", "offline")
    assert gate.ready is False and "offline" in gate.message
    gate.registration_started()
    assert gate.decision() == (False, gate.CONNECTING)
    gate.registration_failed("exit_in_progress", "exit is pending")
    assert gate.decision() == (False, gate.EXITING)
    gate.registration_succeeded()
    assert gate.decision() == (True, "")
    gate.stopped()
    gate.registration_started()
    gate.registration_succeeded()
    gate.registration_failed("assistant_unavailable", "late failure")
    assert gate.decision() == (False, gate.STOPPED)
