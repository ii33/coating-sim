"""Subprocess contracts for collection after pytest releases fixture owners."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[1]


def _run_child(directory: Path, source: str, *, plugin: bool) -> subprocess.CompletedProcess[str]:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pytest.ini").write_text("[pytest]\nmarkers = qt: isolated lifecycle contract\n", encoding="utf-8")
    test_file = directory / "test_child.py"
    test_file.write_text(textwrap.dedent(source), encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    command = [sys.executable, "-m", "pytest", "--confcutdir", str(directory),
        "--rootdir", str(directory), "-c", str(directory / "pytest.ini"), "-q"]
    if plugin:
        command += ["-p", "tools.pytest_qt_lifecycle"]
    command.append(str(test_file))
    return subprocess.run(command, cwd=directory, env=environment, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30., check=False)


def test_unreachable_fixture_cycle_is_collected_after_release_before_next_case(tmp_path):
    source = """
        import gc, sys, weakref
        import pytest
        gc.disable()
        reference = None
        fixture_released = False
        class Owner: pass
        @pytest.fixture
        def owner():
            global fixture_released
            value = Owner()
            value.cycle = value
            yield value
            fixture_released = True
        @pytest.mark.qt
        def test_first(owner):
            global reference
            reference = weakref.ref(owner)
            assert reference() is owner
            assert not fixture_released
        @pytest.mark.qt
        def test_second():
            assert fixture_released, "FIXTURE_MUST_RELEASE_FIRST"
            assert reference() is None, "COLLECT_AFTER_FIXTURE_RELEASE"
            assert not any(name.startswith(("PySide", "PyQt")) for name in sys.modules)
    """
    red = _run_child(tmp_path / "without-plugin", source, plugin=False)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "COLLECT_AFTER_FIXTURE_RELEASE" in red.stdout
    assert "1 failed, 1 passed" in red.stdout
    green = _run_child(tmp_path / "with-plugin", source, plugin=True)
    assert green.returncode == 0, green.stdout + green.stderr
    assert "2 passed" in green.stdout


def test_unmarked_boundary_and_reachable_strong_owners_are_preserved(tmp_path):
    result = _run_child(tmp_path, """
        import gc, weakref
        import pytest
        gc.disable()
        reference = None
        retained_reference = None
        retained = []
        class Owner: pass
        def test_unmarked():
            global reference
            value = Owner()
            value.cycle = value
            reference = weakref.ref(value)
        @pytest.mark.qt
        def test_marked_boundary():
            global retained_reference
            assert reference() is not None, "UNMARKED_CASE_MUST_NOT_COLLECT"
            value = Owner()
            value.cycle = value
            retained.append(value)
            retained_reference = weakref.ref(value)
        @pytest.mark.qt
        def test_reachable_owner_survives_collection():
            assert reference() is None, "MARKED_BOUNDARY_MUST_COLLECT_UNREACHABLE"
            assert retained_reference() is retained[0], "STRONG_OWNER_MUST_SURVIVE"
    """, plugin=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout


@pytest.mark.parametrize("failure", ["body", "fixture"])
def test_collection_does_not_change_case_or_fixture_failure_outcome(tmp_path, failure):
    source = """
        from pathlib import Path
        import pytest
        @pytest.fixture
        def owner():
            yield
            Path("fixture-released.txt").write_text("released", encoding="utf-8")
            if FAILURE == "fixture":
                pytest.fail("INTENTIONAL_FIXTURE_FAILURE")
        @pytest.mark.qt
        def test_failure(owner):
            if FAILURE == "body":
                pytest.fail("INTENTIONAL_BODY_FAILURE")
    """
    source = "FAILURE = " + repr(failure) + "\n" + textwrap.dedent(source)
    result = _run_child(tmp_path, source, plugin=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "INTENTIONAL_" + failure.upper() + "_FAILURE" in result.stdout
    assert (tmp_path / "fixture-released.txt").read_text(encoding="utf-8") == "released"
