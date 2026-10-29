"""Real subprocess contracts for general Qt module execution isolation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET

import pytest

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[1]


def _suite(directory: Path, modules: dict[str, str], conftest: str = "") -> None:
    directory.mkdir(parents=True)
    (directory / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    qt: fresh module process\n    chosen: selected case\n", encoding="utf-8")
    for filename, source in modules.items():
        (directory / filename).write_text(textwrap.dedent(source), encoding="utf-8")
    if conftest:
        (directory / "conftest.py").write_text(textwrap.dedent(conftest), encoding="utf-8")


def _run(directory: Path, *arguments: str, isolated: bool = True,
         environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "COATING_PYTEST_MODULE_WORKER"):
        env.pop(name, None)
    env.update(environment or {})
    command = [sys.executable, "-m", "pytest", "--rootdir", str(directory),
               "--confcutdir", str(directory), "-c", str(directory / "pytest.ini"), "-q"]
    if isolated:
        command += ["-p", "tools.pytest_qt_modules"]
    command += [*arguments, str(directory)]
    return subprocess.run(command, cwd=directory, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False, timeout=90)


def _audit(directory: Path) -> dict:
    return json.loads((directory / "module-processes.json").read_text(encoding="utf-8"))


def _junit_cases(path: Path) -> list[tuple]:
    return sorted((case.get("classname"), case.get("name"),
                   tuple(child.tag for child in case if child.tag in ("failure", "error", "skipped")))
                  for case in ET.parse(path).iter("testcase"))


def test_each_qt_module_has_fresh_pid_and_only_its_own_collection(tmp_path):
    common = """
        import os, json
        from pathlib import Path
        import pytest
        if os.environ.get('COATING_PYTEST_MODULE_WORKER'):
            with Path('imports.jsonl').open('a') as f:
                f.write(json.dumps([os.getpid(), __name__]) + '\\n')
        def record():
            with Path('executed.jsonl').open('a') as f:
                f.write(json.dumps([os.getpid(), __name__]) + '\\n')
    """
    modules = {
        "test_a_plain.py": common + "\ndef test_plain(): record()\n",
        "test_b_plain.py": common + "\ndef test_plain(): record()\n",
        "test_c_qt.py": common + "\n@pytest.mark.qt\ndef test_qt(): record()\ndef test_other(): record()\n",
        "test_d_qt.py": common + "\npytestmark = pytest.mark.qt\n@pytest.mark.parametrize('value', ['', 'a [] b'])\ndef test_qt(value): record()\n",
        "test_e_plain.py": common + "\ndef test_plain(): record()\n",
    }
    # Dedent each common fragment before concatenating zero-indented test bodies.
    modules = {name: textwrap.dedent(common) + source[len(common):] for name, source in modules.items()}
    _suite(tmp_path / "suite", modules)
    directory = tmp_path / "suite"
    result = _run(directory, "--junitxml=junit.xml")
    assert result.returncode == 0, result.stdout + result.stderr
    audit = _audit(directory)
    assert [len(batch["nodeids"]) for batch in audit["batches"]] == [2, 2, 2, 1]
    assert len({batch["pid"] for batch in audit["batches"]}) == 4
    assert sum((batch["started_nodeids"] for batch in audit["batches"]), []) == audit["selected_nodeids"]
    imports = [json.loads(line) for line in (directory / "imports.jsonl").read_text().splitlines()]
    for batch in audit["batches"]:
        actual = {name for pid, name in imports if pid == batch["pid"]}
        expected = {Path(node.split("::")[0]).stem for node in batch["nodeids"]}
        assert actual == expected
    assert len(_junit_cases(directory / "junit.xml")) == 7


def test_reports_match_native_pytest_with_subtests_parameters_and_failure_phases(tmp_path):
    modules = {"test_a.py": """
        import pytest, warnings, unittest
        pytestmark = pytest.mark.qt
        @pytest.mark.parametrize('value', ['', 'a [] b'])
        def test_parameter(value): pass
        @pytest.mark.skip(reason='INTENTIONAL_SKIP')
        def test_skip(): pass
        @pytest.mark.xfail(reason='INTENTIONAL_XFAIL')
        def test_xfail(): assert False
        @pytest.mark.xfail(reason='INTENTIONAL_XPASS')
        def test_xpass(): pass
        @pytest.mark.xfail(strict=True, reason='STRICT_XPASS')
        def test_strict_xpass(): pass
        @pytest.fixture
        def setup_failure(): pytest.fail('SETUP_FAILURE')
        @pytest.fixture
        def teardown_failure():
            yield
            pytest.fail('TEARDOWN_FAILURE')
        def test_setup(setup_failure): pass
        def test_teardown(teardown_failure): pass
        def test_body():
            print('CAPTURED_BODY_OUTPUT')
            pytest.fail('BODY_FAILURE')
        def test_subtests(subtests):
            for value in [0, 1]:
                with subtests.test(value=value): assert value == 0
        def test_warning(): warnings.warn('VISIBLE_WARNING', RuntimeWarning)
        def test_properties(record_property, record_xml_attribute, record_testsuite_property):
            record_property('case_property', 'CASE_EVIDENCE')
            record_xml_attribute('custom_attribute', 'ATTRIBUTE_EVIDENCE')
            record_testsuite_property('suite_property', 'SUITE_EVIDENCE')
        class TestUnittest(unittest.TestCase):
            def test_unittest_subtests(self):
                for value in [0, 1]:
                    with self.subTest(value=value): self.assertEqual(value, 0)
    """, "test_b.py": """
        import pytest
        pytestmark = pytest.mark.qt
        def test_final(): pass
    """}
    directory = tmp_path / "suite"
    _suite(directory, modules)
    native = _run(directory, "--junitxml=native.xml", "-o", "junit_family=xunit1", isolated=False)
    isolated = _run(directory, "--junitxml=isolated.xml", "-p", "tools.pytest_qt_diagnostics",
                    "--qt-diagnostic-log=exceptions.jsonl", "-o", "junit_family=xunit1")
    assert native.returncode == isolated.returncode == 1, isolated.stdout + isolated.stderr
    assert _junit_cases(directory / "native.xml") == _junit_cases(directory / "isolated.xml")
    for path in (directory / "native.xml", directory / "isolated.xml"):
        xml = path.read_text()
        assert 'name="suite_property" value="SUITE_EVIDENCE"' in xml
        assert 'name="case_property" value="CASE_EVIDENCE"' in xml
        assert 'custom_attribute="ATTRIBUTE_EVIDENCE"' in xml
    for message in ("SETUP_FAILURE", "TEARDOWN_FAILURE", "BODY_FAILURE", "STRICT_XPASS",
                    "CAPTURED_BODY_OUTPUT", "VISIBLE_WARNING", "RuntimeWarning", "subtests passed"):
        assert message in isolated.stdout
    audit = _audit(directory)
    expected = audit["selected_nodeids"]
    diagnostic = [json.loads(line) for line in (directory / "exceptions.jsonl").read_text().splitlines()]
    assert [entry["nodeid"] for entry in diagnostic if entry["event"] == "node_start"] == expected
    assert all(batch["exit_code"] in (0, 1) for batch in audit["batches"])


@pytest.mark.parametrize(("option", "failures"), [("-x", 1), ("--maxfail=2", 2)])
def test_global_fail_fast_budget_crosses_module_boundaries(tmp_path, option, failures):
    directory = tmp_path / "suite"
    _suite(directory, {f"test_{letter}.py": """
        import pytest
        pytestmark = pytest.mark.qt
        def test_fail(): pytest.fail('PLANNED_FAILURE')
        def test_after(): pass
    """ for letter in "abc"})
    result = _run(directory, option, "--junitxml=junit.xml")
    assert result.returncode == 1, result.stdout + result.stderr
    audit = _audit(directory)
    started: list[str] = sum((batch.get("started_nodeids", []) for batch in audit["batches"]), [])
    assert len(started) == failures * 2 - 1
    assert started == audit["selected_nodeids"][:len(started)]
    assert len([batch for batch in audit["batches"] if batch["status"] == "pending"]) == 3 - failures


@pytest.mark.parametrize(("failure", "expected"), [
    ("collection", 2), ("crash", 134), ("interrupt", 2), ("membership", 4),
])
def test_worker_terminal_failures_are_never_hidden_or_retried(tmp_path, failure, expected):
    directory = tmp_path / "suite"
    _suite(directory, {"test_a.py": """
        import os, pytest, signal
        from pathlib import Path
        pytestmark = pytest.mark.qt
        if os.environ.get('COATING_PYTEST_MODULE_WORKER') and MODE == 'collection':
            raise RuntimeError('WORKER_COLLECTION_ERROR')
        def test_once():
            with Path('attempts').open('a') as f: f.write('attempt\\n')
            if MODE == 'interrupt': raise KeyboardInterrupt()
            if MODE == 'crash':
                import resource
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                os.kill(os.getpid(), signal.SIGABRT)
    """.replace("MODE", repr(failure)), "test_b.py": """
        import pytest
        pytestmark = pytest.mark.qt
        def test_must_not_run(): pytest.fail('UNEXPECTED_LATER_MODULE')
    """}, conftest="""
        import os
        def pytest_collection_modifyitems(items):
            if os.environ.get('COATING_PYTEST_MODULE_WORKER') and MODE == 'membership':
                items.clear()
    """.replace("MODE", repr(failure)))
    result = _run(directory, "--junitxml=junit.xml")
    assert result.returncode == expected, result.stdout + result.stderr
    audit = _audit(directory)
    assert audit["batches"][0]["status"] == "process-error"
    assert audit["batches"][0]["exit_code"] == expected
    assert audit["batches"][1]["status"] == "pending"
    assert "UNEXPECTED_LATER_MODULE" not in result.stdout
    attempts = directory / "attempts"
    assert (attempts.read_text().splitlines() if attempts.exists() else []) == (
        ["attempt"] if failure in ("crash", "interrupt") else [])
    if failure == "collection":
        assert "WORKER_COLLECTION_ERROR" in result.stdout
    if failure == "membership":
        assert "collection differs" in result.stdout + result.stderr


def test_collection_only_and_marker_selection_keep_original_membership(tmp_path):
    directory = tmp_path / "suite"
    _suite(directory, {"test_a.py": """
        import pytest
        pytestmark = pytest.mark.qt
        @pytest.mark.chosen
        @pytest.mark.parametrize('value', ['', 'a [] b'])
        def test_selected(value): pass
        def test_other(): pass
    """, "test_b.py": """
        import pytest
        pytestmark = pytest.mark.qt
        @pytest.mark.chosen
        def test_selected(): pass
    """})
    native = _run(directory, "--collect-only", "-m", "chosen", isolated=False)
    isolated = _run(directory, "--collect-only", "-m", "chosen")
    assert native.returncode == isolated.returncode == 0
    assert [line for line in native.stdout.splitlines() if "::" in line] == [
        line for line in isolated.stdout.splitlines() if "::" in line]
    assert not (directory / "module-processes.json").exists()
    selected = _run(directory, "-m", "chosen", "-k", "selected", "--junitxml=junit.xml")
    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert len(_audit(directory)["selected_nodeids"]) == 3
    assert len(_junit_cases(directory / "junit.xml")) == 3


def test_external_worker_environment_cannot_silently_disable_isolation(tmp_path):
    directory = tmp_path / "suite"
    _suite(directory, {"test_a.py": "def test_case(): pass\n"})
    result = _run(directory, environment={"COATING_PYTEST_MODULE_WORKER": "fake"})
    assert result.returncode == 4, result.stdout + result.stderr
    assert "is internal; remove it" in result.stderr


def test_worker_keeps_parent_configuration_after_external_report_is_created(tmp_path):
    directory = tmp_path / "suite"
    external = tmp_path / "unrelated-project"
    external.mkdir()
    (external / "pytest.ini").write_text("[pytest]\npython_files = never_test_*.py\n", encoding="utf-8")
    report = external / "collection.json"
    _suite(directory, {"test_a.py": """
        import pytest
        pytestmark = pytest.mark.qt
        def test_case(pytestconfig):
            assert pytestconfig.rootpath.name == 'suite'
            assert pytestconfig.inipath.name == 'pytest.ini'
    """}, conftest="""
        from pathlib import Path
        def pytest_addoption(parser):
            parser.addoption('--collection-report-json')
        def pytest_collection_finish(session):
            path = session.config.getoption('collection_report_json')
            if path:
                Path(path).write_text('{}')
    """)
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(ROOT)
    environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    for name in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'COATING_PYTEST_MODULE_WORKER'):
        environment.pop(name, None)
    # No -c/--rootdir here: reproduce the worker's second early discovery only
    # after the conftest option writes a file beneath another project's ini.
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collection-report-json', str(report),
         '-p', 'tools.pytest_qt_modules', '-q', '.'],
        cwd=directory, env=environment, capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 passed' in result.stdout


def test_worker_honors_explicit_user_root_and_ini_overrides(tmp_path):
    directory = tmp_path / "suite"
    _suite(directory, {"test_a.py": """
        import pytest
        pytestmark = pytest.mark.qt
        def test_case(pytestconfig):
            assert pytestconfig.rootpath.name == 'suite'
            assert pytestconfig.inipath.name == 'custom.ini'
    """})
    custom = directory / "custom.ini"
    custom.write_text('[pytest]\nmarkers = qt: selected module\n', encoding='utf-8')
    result = _run(directory, '--rootdir', str(directory), '-c', str(custom))
    assert result.returncode == 0, result.stdout + result.stderr


def test_stepwise_execution_is_rejected_instead_of_silently_changing_stop_semantics(tmp_path):
    directory = tmp_path / "suite"
    _suite(directory, {"test_a.py": "import pytest\npytestmark = pytest.mark.qt\ndef test_case(): pass\n"})
    result = _run(directory, "--stepwise")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "does not support --stepwise execution" in result.stderr


@pytest.mark.parametrize("option", ["--lf", "--ff"])
def test_controller_cache_selection_and_order_are_not_reapplied_in_workers(tmp_path, option):
    directory = tmp_path / "suite"
    _suite(directory, {f"test_{letter}.py": """
        import pytest
        pytestmark = pytest.mark.qt
        def test_case(): pass
    """ for letter in "ab"})
    cache = directory / ".pytest_cache/v/cache"
    cache.mkdir(parents=True)
    (cache / "lastfailed").write_text(json.dumps({"test_b.py::test_case": True}))
    result = _run(directory, option, "--junitxml=junit.xml")
    assert result.returncode == 0, result.stdout + result.stderr
    audit = _audit(directory)
    expected = ["test_b.py::test_case"]
    if option == "--ff":
        expected.append("test_a.py::test_case")
    assert audit["selected_nodeids"] == expected
    assert sum((batch["started_nodeids"] for batch in audit["batches"]), []) == expected


@pytest.mark.parametrize("option", ["-x", "--maxfail=2"])
def test_subtest_fail_fast_uses_native_failure_count_not_finalized_report_count(tmp_path, option):
    directory = tmp_path / "suite"
    _suite(directory, {f"test_{letter}.py": """
        import pytest
        pytestmark = pytest.mark.qt
        def test_subtest(subtests):
            with subtests.test(value=1): assert False
        def test_after(): pass
    """ for letter in "abc"}, conftest="""
        from pathlib import Path
        def pytest_runtest_logstart(nodeid, location):
            with Path('starts').open('a') as stream: stream.write(nodeid + '\\n')
    """)
    native = _run(directory, option, isolated=False)
    expected = (directory / "starts").read_text().splitlines()
    (directory / "starts").unlink()
    isolated = _run(directory, option, "--junitxml=junit.xml")
    assert native.returncode == isolated.returncode == 1, isolated.stdout + isolated.stderr
    actual: list[str] = sum((batch.get("started_nodeids", []) for batch in _audit(directory)["batches"]), [])
    assert actual == expected


def test_real_sigint_to_controller_reaps_its_running_worker(tmp_path):
    directory = tmp_path / "suite"
    _suite(directory, {"test_a.py": """
        import os, time, pytest
        from pathlib import Path
        pytestmark = pytest.mark.qt
        def test_wait_for_cancellation():
            Path('worker-pid').write_text(str(os.getpid()))
            while True: time.sleep(1)
    """, "test_b.py": """
        import pytest
        pytestmark = pytest.mark.qt
        def test_later(): pytest.fail('LATER_MODULE_MUST_NOT_RUN')
    """})
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "COATING_PYTEST_MODULE_WORKER"):
        env.pop(name, None)
    command = [sys.executable, "-m", "pytest", "-p", "tools.pytest_qt_modules", "-q",
               "--junitxml=junit.xml", str(directory)]
    controller = subprocess.Popen(command, cwd=directory, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not (directory / "worker-pid").exists():
            assert controller.poll() is None, controller.communicate()
            assert time.monotonic() < deadline, "worker never reached its test body"
            time.sleep(.02)
        worker_pid = int((directory / "worker-pid").read_text())
        os.kill(controller.pid, signal.SIGINT)
        stdout, stderr = controller.communicate(timeout=15)
        assert controller.returncode == 2, stdout + stderr
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
        audit = _audit(directory)
        assert audit["batches"][0]["status"] == "interrupted"
        assert audit["batches"][0]["exit_code"] == 2
        assert audit["batches"][1]["status"] == "pending"
        assert "LATER_MODULE_MUST_NOT_RUN" not in stdout
    finally:
        if controller.poll() is None:
            os.kill(controller.pid, signal.SIGINT)
            controller.communicate(timeout=15)
