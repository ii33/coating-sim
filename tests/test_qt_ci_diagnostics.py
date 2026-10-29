"""Diagnostic evidence survives unittest cleanup without touching object repr."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

import pytest

from tools import pytest_qt_diagnostics as diagnostics


def test_exception_snapshot_never_formats_opaque_arguments():
    class OpaqueQtArgument:
        def __repr__(self):
            raise AssertionError("repr must not be called")

        def __str__(self):
            raise AssertionError("str must not be called")

    try:
        raise RuntimeError("Original assertion", OpaqueQtArgument())
    except RuntimeError as exc:
        evidence = diagnostics._exception_snapshot(exc)
    encoded = json.dumps(evidence)
    assert "Original assertion" in encoded and "OpaqueQtArgument" in encoded
    assert evidence["frames"][-1]["function"] == "test_exception_snapshot_never_formats_opaque_arguments"


def test_log_failure_preserves_test_error(tmp_path, capsys):
    blocked = tmp_path / "file-not-directory"
    blocked.write_text("Keep existing bytes", encoding="utf8")
    config = Mock(spec=pytest.Config)
    config.getoption.return_value = str(blocked / "events.jsonl")
    diagnostics._emit(config, "probe", message="Original test result remains authoritative")
    assert "original test outcome retained" in capsys.readouterr().err
    assert blocked.read_text(encoding="utf8") == "Keep existing bytes"


@pytest.mark.parametrize("instance_override", [False, True])
def test_call_hook_restores_descriptor_without_a_bound_self_cycle(instance_override):
    case = unittest.TestCase()
    if instance_override:
        vars(case)["_callTestMethod"] = lambda method: method()
    before = dict(vars(case))
    item = Mock(spec=pytest.Function)
    item.instance = case
    item.config = Mock(spec=pytest.Config)
    item.config.getoption.return_value = None
    item.nodeid = "diagnostic-probe"
    hook = diagnostics.pytest_runtest_call(item)
    next(hook)
    assert "_callTestMethod" in vars(case)
    with pytest.raises(StopIteration):
        next(hook)
    assert vars(case) == before


def test_unittest_failure_is_flushed_before_teardown_and_stays_failed(tmp_path):
    project = Path(__file__).resolve().parents[1]
    events = tmp_path / "events.jsonl"
    cleaned = tmp_path / "cleaned.txt"
    fixture_cleaned = tmp_path / "fixture-cleaned.txt"
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import json, unittest, pytest\nfrom pathlib import Path\n"
        f"EVENTS = Path({str(events)!r})\nCLEANED = Path({str(cleaned)!r})\n"
        f"FIXTURE_CLEANED = Path({str(fixture_cleaned)!r})\n"
        "class Probe(unittest.TestCase):\n"
        "    def test_original_assertion(self):\n"
        "        raise AssertionError('Original diagnostic assertion')\n"
        "    def tearDown(self):\n"
        "        rows = [json.loads(s) for s in EVENTS.read_text().splitlines()]\n"
        "        assert any(r['event'] == 'exception_before_unittest_cleanup' for r in rows)\n"
        "        CLEANED.write_text('cleanup completed after evidence')\n"
        "@pytest.fixture\ndef cleanup_guard():\n"
        "    yield\n"
        "    rows = [json.loads(s) for s in EVENTS.read_text().splitlines()]\n"
        "    assert any(r['event'] == 'exception_before_pytest_report' and r['nodeid'].endswith('test_function_assertion') for r in rows)\n"
        "    FIXTURE_CLEANED.write_text('fixture cleanup completed after evidence')\n"
        "def test_function_assertion(cleanup_guard):\n"
        "    raise AssertionError('Original function assertion')\n",
        encoding="utf8",
    )
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = str(project)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(project / "pytest.ini"),
         str(probe), "-p", "tools.pytest_qt_diagnostics", "--qt-diagnostic-log", str(events),
         "--tb=short", "-q", "-o", f"cache_dir={tmp_path / 'cache'}"],
        cwd=project, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Original diagnostic assertion" in result.stdout
    assert cleaned.read_text() == "cleanup completed after evidence"
    assert fixture_cleaned.read_text() == "fixture cleanup completed after evidence"
    assert "Original function assertion" in result.stdout
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert rows[0]["event"] == "node_start"
    failure = next(row for row in rows if row["event"] == "exception_before_unittest_cleanup")
    assert failure["exception_type"] == "AssertionError"
    assert failure["arguments"] == ["Original diagnostic assertion"]


def test_disposal_evidence_is_flushed_before_abrupt_process_exit(tmp_path):
    project = Path(__file__).resolve().parents[1]
    events = tmp_path / "disposal-events.jsonl"
    probe = tmp_path / "test_disposal_probe.py"
    probe.write_text(
        "import os\n"
        "from tools.pytest_qt_diagnostics import record_qt_disposal\n"
        "def test_abrupt_exit():\n"
        "    record_qt_disposal('probe', 'delete_begin', 'simulation')\n"
        "    os._exit(73)\n",
        encoding="utf8",
    )
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = str(project)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(project / "pytest.ini"),
         str(probe), "-p", "tools.pytest_qt_diagnostics", "--qt-diagnostic-log", str(events),
         "-q", "-o", f"cache_dir={tmp_path / 'cache'}"],
        cwd=project, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 73, result.stdout + result.stderr
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert rows[-1] == {"event": "qt_disposal", "nodeid": "probe",
                        "phase": "delete_begin", "object": "simulation"}


@pytest.mark.parametrize("traceback_style", ["auto", "short"])
def test_short_report_preserves_failure_without_opaque_argument_repr(tmp_path, traceback_style):
    project = Path(__file__).resolve().parents[1]
    events = tmp_path / "opaque-events.jsonl"
    represented = tmp_path / "repr-called.txt"
    probe = tmp_path / "test_opaque_probe.py"
    probe.write_text(
        "import os, pytest\nfrom pathlib import Path\n"
        f"REPRESENTED = Path({str(represented)!r})\n"
        "class OpaqueArgument:\n"
        "    def __repr__(self):\n"
        "        REPRESENTED.write_text('report touched opaque object')\n"
        "        os._exit(83)\n"
        "@pytest.fixture\ndef opaque_argument():\n"
        "    return OpaqueArgument()\n"
        "def test_original_assertion(opaque_argument):\n"
        "    raise AssertionError('Original opaque-argument assertion')\n",
        encoding="utf8",
    )
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = str(project)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(project / "pytest.ini"),
         str(probe), "-p", "tools.pytest_qt_diagnostics", "--qt-diagnostic-log", str(events),
         f"--tb={traceback_style}", "-q", "-o", f"cache_dir={tmp_path / 'cache'}"],
        cwd=project, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    # The control exits only its own child process when pytest formats a fixture
    # argument. CI's short report must retain a real failure and skip that repr.
    assert result.returncode == (83 if traceback_style == "auto" else 1), result.stdout + result.stderr
    assert represented.exists() is (traceback_style == "auto")
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    failure = next(row for row in rows if row["event"] == "exception_before_pytest_report")
    assert failure["nodeid"].endswith("test_original_assertion")
    assert failure["exception_type"] == "AssertionError"
    assert failure["arguments"] == ["Original opaque-argument assertion"]
    assert failure["when"] == "call"
    if traceback_style == "short":
        assert "Original opaque-argument assertion" in result.stdout
        assert "1 failed" in result.stdout


def test_scene_invalid_visual_root_fails_without_formatting_qt_argument(tmp_path):
    project = Path(__file__).resolve().parents[1]
    represented = tmp_path / "scene-repr-called.txt"
    events = tmp_path / "scene-events.jsonl"
    probe = tmp_path / "test_scene_lookup_probe.py"
    probe.write_text(
        "import os\nfrom pathlib import Path\nfrom types import SimpleNamespace\n"
        "from PySide6.QtGui import QGuiApplication\n"
        "from tests.test_film_synthesis_gui_review import _Scene\n"
        f"REPRESENTED = Path({str(represented)!r})\n"
        "APP = QGuiApplication.instance() or QGuiApplication([])\n"
        "class OpaqueArgument:\n"
        "    def __repr__(self):\n"
        "        REPRESENTED.write_text('lookup assertion touched opaque object')\n"
        "        os._exit(83)\n"
        "def test_original_scene_lookup_failure():\n"
        "    scene = _Scene.__new__(_Scene)\n"
        "    scene.window = SimpleNamespace(contentItem=lambda: OpaqueArgument())\n"
        "    scene.item('filmOptimizationCancelButton')\n",
        encoding="utf8",
    )
    env = os.environ.copy()
    env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(project),
               QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
               QSG_RHI_BACKEND="software")
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(project / "pytest.ini"),
         str(probe), "-p", "tools.pytest_qt_diagnostics", "--qt-diagnostic-log", str(events),
         "--tb=short", "-q", "-o", f"cache_dir={tmp_path / 'cache'}"],
        cwd=project, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert not represented.exists()
    assert "QQuickWindow has no live QQuickItem content root" in result.stdout
    assert "1 failed" in result.stdout
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    failure = next(row for row in rows if row["event"] == "exception_before_pytest_report")
    assert failure["exception_type"] == "AssertionError"
    assert failure["when"] == "call"
    assert failure["arguments"] == ["QQuickWindow has no live QQuickItem content root"]
