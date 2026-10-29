"""Execute Qt-marked modules in fresh interpreters, with native pytest reports.

Collection is authoritative in the controller. Workers collect only their exact
planned node IDs, execute once, and serialize reports using pytest's public
hooks. No Qt API, fork, retry, timeout, outcome rewrite, or test allowlist is used.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any
import warnings

import pytest
from _pytest.junitxml import xml_key


_WORKER_ENV = "COATING_PYTEST_MODULE_WORKER"
_STATE = pytest.StashKey[dict[str, Any]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--module-isolation-report", type=Path,
        help="Write the exact Qt module process plan and completion evidence as JSON",
    )
    parser.addoption("--_qt-module-worker", type=Path, help=argparse.SUPPRESS)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    worker_path = config.getoption("_qt_module_worker")
    token = os.environ.get(_WORKER_ENV)
    if worker_path is None and token is not None:
        raise pytest.UsageError(f"{_WORKER_ENV} is internal; remove it from the environment")
    state: dict[str, Any] = {"worker": worker_path is not None}
    config.stash[_STATE] = state
    if worker_path is None:
        return
    plan = json.loads(worker_path.read_text(encoding="utf-8"))
    if not token or token != plan["token"] or os.getppid() != plan["controller_pid"]:
        raise pytest.UsageError("Qt module worker must be launched by its owning pytest controller")
    state.update(plan=plan, stream=Path(plan["events"]).open("w", encoding="utf-8"))
    # The controller owns aggregate artifacts; workers must never overwrite them.
    config.option.xmlpath = plan["worker_junit"]
    if hasattr(config.option, "collection_report_json"):
        config.option.collection_report_json = None
    config.option.maxfail = plan["maxfail"]
    # Selection/order was already resolved against the complete collection.
    config.option.lf = False
    config.option.failedfirst = False
    config.option.newfirst = False
    config.option.stepwise = False


def _emit(config: pytest.Config, event: str, **data: Any) -> None:
    state = config.stash[_STATE]
    if state["worker"]:
        stream = state["stream"]
        stream.write(json.dumps(_pack({"event": event, **data}), ensure_ascii=False) + "\n")
        stream.flush()


def _pack(value: Any) -> Any:
    """Lossless JSON transport: pytest's skipped longrepr must remain a tuple."""
    if isinstance(value, dict):
        return ["dict", [[key, _pack(item)] for key, item in value.items()]]
    if isinstance(value, tuple):
        return ["tuple", [_pack(item) for item in value]]
    if isinstance(value, list):
        return ["list", [_pack(item) for item in value]]
    return ["atom", value]


def _unpack(value: Any) -> Any:
    kind, data = value
    if kind == "dict":
        return {key: _unpack(item) for key, item in data}
    if kind == "tuple":
        return tuple(_unpack(item) for item in data)
    if kind == "list":
        return [_unpack(item) for item in data]
    if kind != "atom":
        raise ValueError(f"Unknown pytest report transport kind: {kind}")
    return data


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection(session: pytest.Session) -> Iterator[Any]:
    state = session.config.stash[_STATE]
    if state["worker"]:
        # Change collection targets, not command-line parsing: plugin, capture,
        # assertion, warning, marker and reporting options retain pytest semantics.
        session.config.args = state["plan"]["targets"]
        _emit(session.config, "worker", pid=os.getpid())
    return (yield)


@pytest.hookimpl(specname="pytest_configure", trylast=True)
def pytest_configure_worker_terminal(config: pytest.Config) -> None:
    if config.stash[_STATE]["worker"]:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            config.pluginmanager.unregister(reporter)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> Iterator[Any]:
    result = yield
    state = config.stash[_STATE]
    if state["worker"]:
        expected = state["plan"]["nodeids"]
        by_id = {item.nodeid: item for item in items}
        selected = set(expected)
        deselected = [item for item in items if item.nodeid not in selected]
        items[:] = [by_id[nodeid] for nodeid in expected if nodeid in by_id]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
    return result


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session: pytest.Session) -> None:
    state = session.config.stash[_STATE]
    if state["worker"]:
        if session.testsfailed:
            return  # Original collection reports/exit status take precedence.
        actual = [item.nodeid for item in session.items]
        expected = state["plan"]["nodeids"]
        if actual != expected:
            raise pytest.UsageError(
                "Qt module worker collection differs from the controller plan: "
                f"expected={expected!r}; actual={actual!r}"
            )
        _emit(session.config, "collection", nodeids=actual)


class _WorkerReports:
    def __init__(self, session: pytest.Session) -> None:
        self.session = session
        self.config = session.config

    def pytest_runtest_logstart(self, nodeid: str, location: object) -> None:
        _emit(self.config, "start", nodeid=nodeid, location=location)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> Iterator[Any]:
        result = yield
        # pytest 9's built-in subtests finalize a parent report in this public
        # status hook. Keep that behavior even without a worker terminal UI.
        self.config.hook.pytest_report_teststatus(report=report, config=self.config)
        data = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
        if data is None:
            raise RuntimeError(f"pytest could not serialize report for {report.nodeid}")
        _emit(self.config, "report", data=data, failures=self.session.testsfailed)
        return result

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            data = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
            if data is None:
                raise RuntimeError(f"pytest could not serialize collection error for {report.nodeid}")
            _emit(self.config, "collectreport", data=data)

    def pytest_runtest_logfinish(self, nodeid: str, location: object) -> None:
        _emit(self.config, "finish", nodeid=nodeid, location=location)

    def pytest_warning_recorded(
        self, warning_message: warnings.WarningMessage, when: str,
        nodeid: str, location: object,
    ) -> None:
        _emit(self.config, "warning", message=str(warning_message.message),
              category=warning_message.category.__name__, filename=warning_message.filename,
              lineno=warning_message.lineno, line=warning_message.line,
              when=when, nodeid=nodeid, location=location)

    def pytest_internalerror(self, excrepr: object) -> None:
        _emit(self.config, "internalerror", message=str(excrepr))

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.config.stash[_STATE]["plan"]["junit"]:
            _emit(self.config, "suite_properties", properties=self.config.stash[xml_key].global_properties)
        _emit(self.config, "done", exitstatus=int(exitstatus), failures=session.testsfailed)


@pytest.fixture
def record_xml_attribute(
    request: pytest.FixtureRequest,
    record_xml_attribute: Callable[[str, object], None],
) -> Callable[[str, object], None]:
    original = record_xml_attribute
    state = request.config.stash[_STATE]
    if not state["worker"] or not state["plan"]["junit"]:
        return original

    def record(name: str, value: object) -> None:
        original(name, value)
        _emit(request.config, "xml_attribute", nodeid=request.node.nodeid, name=name, value=str(value))

    return record


def pytest_sessionstart(session: pytest.Session) -> None:
    if session.config.stash[_STATE]["worker"]:
        session.config.pluginmanager.register(_WorkerReports(session), "module-worker-reports")


def _batches(items: list[pytest.Item]) -> list[list[pytest.Item]]:
    qt_modules = {item.path for item in items if item.get_closest_marker("qt") is not None}
    batches: list[list[pytest.Item]] = []
    previous_key: object = object()
    for item in items:
        key = item.path if item.path in qt_modules else None
        if not batches or key != previous_key:
            batches.append([])
        batches[-1].append(item)
        previous_key = key
    return batches


def _replay(session: pytest.Session, events: list[dict[str, Any]]) -> list[str]:
    config = session.config
    failure_base = session.testsfailed
    report_hook = config.pluginmanager.subset_hook_caller("pytest_runtest_logreport", remove_plugins=[session])
    started: list[str] = []
    for event in events:
        kind = event["event"]
        if kind == "start":
            started.append(event["nodeid"])
            config.hook.pytest_runtest_logstart(nodeid=event["nodeid"], location=tuple(event["location"]))
        elif kind in ("report", "collectreport"):
            report = config.hook.pytest_report_from_serializable(config=config, data=event["data"])
            if report is None:
                raise RuntimeError("pytest could not deserialize a module worker report")
            if kind == "report":
                # A native subtest status hook can finalize a parent outcome
                # after Session counted it. Recounting the finalized report in
                # the controller would charge a failed subtest twice. Preserve
                # the worker's authoritative native failure count instead.
                session.testsfailed = failure_base + event["failures"]
                report_hook(report=report)
                if config.option.maxfail and session.testsfailed >= config.option.maxfail:
                    session.shouldfail = f"stopping after {session.testsfailed} failures"
            else:
                config.hook.pytest_collectreport(report=report)
        elif kind == "finish":
            config.hook.pytest_runtest_logfinish(nodeid=event["nodeid"], location=tuple(event["location"]))
        elif kind == "warning":
            category = type(event["category"], (Warning,), {})
            message = warnings.WarningMessage(event["message"], category, event["filename"],
                                              event["lineno"], line=event["line"])
            config.hook.pytest_warning_recorded.call_historic(kwargs={
                "warning_message": message, "when": event["when"], "nodeid": event["nodeid"],
                "location": tuple(event["location"]) if event["location"] is not None else None,
            })
        elif kind == "internalerror":
            sys.stderr.write(event["message"] + "\n")
        elif kind == "suite_properties":
            for name, value in event["properties"]:
                config.stash[xml_key].add_global_property(name, value)
        elif kind == "xml_attribute":
            config.stash[xml_key].node_reporter(event["nodeid"]).add_attribute(event["name"], event["value"])
    return started


def _write_audit(path: Path | None, audit: dict[str, Any]) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_worker(command: list[str], config: pytest.Config, environment: dict[str, str],
                entry: dict[str, Any], audit: dict[str, Any], destination: Path | None) -> int:
    # Own the process handle through cancellation too. subprocess.run alone can
    # leave a worker running when SIGINT reaches only the controller.
    with subprocess.Popen(command, cwd=config.invocation_params.dir, env=environment,
                          start_new_session=os.name == "posix") as process:
        try:
            entry["pid"] = process.pid
            _write_audit(destination, audit)
            return process.wait()
        except KeyboardInterrupt:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The owned process group already exited in this race.
            else:
                process.kill()
            process.wait()
            entry["status"] = "interrupted"
            entry["exit_code"] = 2
            _write_audit(destination, audit)
            raise


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session: pytest.Session) -> bool | None:
    config = session.config
    if (config.stash[_STATE]["worker"] or config.option.collectonly
            or (session.testsfailed and not config.option.continue_on_collection_errors)
            or not any(item.get_closest_marker("qt") is not None for item in session.items)):
        return None
    if config.option.stepwise:
        raise pytest.UsageError(
            "Qt module isolation does not support --stepwise execution; use -x or --maxfail"
        )
    destination = config.getoption("module_isolation_report")
    if destination is None and config.option.xmlpath:
        destination = Path(config.option.xmlpath).with_name("module-processes.json")
    if destination is not None and not destination.is_absolute():
        destination = Path(config.invocation_params.dir) / destination
    batches = _batches(session.items)
    # Original exceptions/disposal events belong to workers, not report replay.
    # Workers still parse the unchanged CLI diagnostic path and append to it.
    if hasattr(config.option, "qt_diagnostic_log"):
        config.option.qt_diagnostic_log = None
    audit: dict[str, Any] = {
        "selected_nodeids": [item.nodeid for item in session.items],
        "batches": [{"nodeids": [item.nodeid for item in batch], "status": "pending"}
                    for batch in batches],
    }
    _write_audit(destination, audit)
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    with tempfile.TemporaryDirectory(prefix="coating-pytest-modules-") as temporary:
        root = Path(temporary)
        for index, batch in enumerate(batches):
            entry = audit["batches"][index]
            nodeids = entry["nodeids"]
            events_path = root / f"{index}.jsonl"
            plan_path = root / f"{index}.json"
            maxfail = config.option.maxfail
            token = os.urandom(24).hex()
            plan_path.write_text(json.dumps({
                "nodeids": nodeids, "events": str(events_path),
                "targets": list(dict.fromkeys(str(item.path) for item in batch)),
                "maxfail": max(0, maxfail - session.testsfailed) if maxfail else 0,
                "controller_pid": os.getpid(), "token": token,
                "junit": bool(config.option.xmlpath),
                "worker_junit": str(root / f"{index}.xml") if config.option.xmlpath else None,
            }), encoding="utf-8")
            environment = os.environ.copy()
            environment[_WORKER_ENV] = token
            # Reuse the parent's parsed configuration, including explicit user
            # overrides. A report created after parent discovery must not make
            # the worker rediscover a different root/ini from that output path.
            command = [sys.executable, "-m", "pytest", *config.invocation_params.args,
                       "--rootdir", str(config.rootpath)]
            if config.inipath is not None:
                command.extend(["-c", str(config.inipath)])
            command.append(f"--_qt-module-worker={plan_path}")
            if reporter is not None:
                reporter.ensure_newline()
                reporter.write_sep("=", f"fresh test process {index + 1}/{len(batches)}: "
                                   f"{batch[0].path.name} ({len(batch)} items)")
            entry["status"] = "running"
            _write_audit(destination, audit)
            begin = time.monotonic()
            code = _run_worker(command, config, environment, entry, audit, destination)
            entry["elapsed_seconds"] = time.monotonic() - begin
            if code < 0:
                number = -code
                code = 128 + number
                sys.stderr.write(f"Module test process terminated by {signal.Signals(number).name}\n")
            entry["exit_code"] = code
            events = ([_unpack(json.loads(line)) for line in events_path.read_text(encoding="utf-8").splitlines()]
                      if events_path.exists() else [])
            entry["started_nodeids"] = _replay(session, events)
            entry["status"] = "finished" if code in (0, 1) else "process-error"
            entry["pid"] = next((event["pid"] for event in events if event["event"] == "worker"), None)
            _write_audit(destination, audit)
            done = [event for event in events if event["event"] == "done"]
            if code not in (0, 1):
                pytest.exit(f"Module test process failed with status {code}: {batch[0].path}",
                            returncode=code)
            collected = [event["nodeids"] for event in events if event["event"] == "collection"]
            if code and not done:
                entry["status"] = "process-error"
                _write_audit(destination, audit)
                pytest.exit(f"Module process exited without final pytest evidence: {batch[0].path}",
                            returncode=code)
            if collected != [nodeids] or len(done) != 1 or done[0]["exitstatus"] != code:
                raise pytest.UsageError(f"Incomplete/contradictory module process evidence: {batch[0].path}")
            started = entry["started_nodeids"]
            if started != nodeids and not (maxfail and session.shouldfail and started == nodeids[:len(started)]):
                raise pytest.UsageError(f"Module process did not execute its exact plan: {batch[0].path}")
            finished = [event["nodeid"] for event in events if event["event"] == "finish"]
            if finished != started:
                raise pytest.UsageError(f"Module process omitted protocol completion: {batch[0].path}")
            if code == 1 and not done[0]["failures"]:
                pytest.exit(f"Module process returned failure without a failing test: {batch[0].path}",
                            returncode=1)
            if session.shouldfail:
                raise session.Failed(session.shouldfail)
            if session.shouldstop:
                raise session.Interrupted(session.shouldstop)
    return True


def pytest_unconfigure(config: pytest.Config) -> None:
    state = config.stash.get(_STATE, {})
    if state.get("worker") and "stream" in state:
        state["stream"].close()
