"""Child-only temp settings and conservative installer lifecycle evidence."""
import os
import sys
import tempfile
import threading
from unittest.mock import patch
from typing import Any

import pytest

from tools.dependency_manager.manager import run_command


def test_normal_exit_reports_completion_and_child_temporary_directory():
    outcome: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    before = {key: os.environ.get(key) for key in ("TEMP", "TMP")}
    with tempfile.TemporaryDirectory() as directory:
        result = run_command([sys.executable, "-I", "-c", "import os; print(os.environ['TEMP']); print(os.environ['TMP'])"], events.append, threading.Event(), outcome=outcome, temporary_directory=directory)
    assert result == 0
    assert outcome == {"started": True, "completed": True, "stopped": False, "returncode": 0, "parent_exited": True, "tree_termination_confirmed": False}
    assert [event["text"] for event in events] == [directory, directory]
    assert {key: os.environ.get(key) for key in ("TEMP", "TMP")} == before


def test_prelaunch_cancel_reports_no_process_started():
    event = threading.Event()
    outcome: dict[str, Any] = {}
    event.set()
    with patch("tools.dependency_manager.manager.subprocess.Popen") as launch:
        assert run_command([sys.executable], lambda e: None, event, outcome=outcome) == 130
    launch.assert_not_called()
    assert outcome == {"started": False, "completed": False, "stopped": True, "returncode": None, "parent_exited": False, "tree_termination_confirmed": False}


def test_launch_failure_reports_no_process_started():
    outcome: dict[str, Any] = {}
    with patch("tools.dependency_manager.manager.subprocess.Popen", side_effect=OSError("launch failed")), pytest.raises(OSError):
        run_command([sys.executable], lambda e: None, threading.Event(), outcome=outcome)
    assert outcome == {"started": False, "completed": False, "stopped": False, "returncode": None, "parent_exited": False, "tree_termination_confirmed": False}


def test_timeout_never_claims_installer_completed():
    outcome: dict[str, Any] = {}
    result = run_command([sys.executable, "-I", "-c", "import time; time.sleep(10)"], lambda e: None, threading.Event(), timeout=0.05, outcome=outcome)
    assert result == 130
    assert outcome == {"started": True, "completed": False, "stopped": True, "returncode": None, "parent_exited": True, "tree_termination_confirmed": False}
