"""Opt-in Qt CI evidence before unittest cleanup and pytest failure formatting.

The Qt and contract CI tiers load this plugin. JSONL owns diagnostic evidence,
not test state. Writes flush immediately; a logging failure never replaces a test error.
Exception arguments other than exact primitive types are recorded by type only.
No Qt import, object property inspection, repr, retry, or outcome change occurs.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
import json
from pathlib import Path
import sys
from typing import Any, Protocol, cast
import unittest

import pytest


class _UnittestMethodSlot(Protocol):
    _callTestMethod: Callable[[Callable[[], object]], object]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--qt-diagnostic-log", default=None,
                     help="Flush Qt CI node IDs and primitive exception evidence to JSONL")


def _argument(value: object) -> object:
    if type(value) is str:
        return value[:8000]
    if type(value) in (bool, int, float, type(None)):
        return value
    return {"type": type(value).__name__, "value": "omitted"}


def _exception_snapshot(exc: BaseException) -> dict[str, Any]:
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append({"file": code.co_filename, "line": tb.tb_lineno,
                       "function": code.co_name})
        tb = tb.tb_next
    return {"exception_type": type(exc).__name__,
            "arguments": [_argument(value) for value in exc.args], "frames": frames}


def _emit(config: pytest.Config, event: str, **data: object) -> None:
    target = config.getoption("qt_diagnostic_log")
    if target is None:
        return
    try:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf8") as stream:
            stream.write(json.dumps({"event": event, **data}, ensure_ascii=False) + "\n")
            stream.flush()
    except (OSError, TypeError, ValueError):
        sys.stderr.write("Qt CI diagnostic log write failed; original test outcome retained.\n")


_CONFIG: pytest.Config | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _CONFIG
    _CONFIG = config


def pytest_unconfigure(config: pytest.Config) -> None:
    global _CONFIG
    _CONFIG = None


def record_qt_disposal(test_id: str, phase: str, label: str) -> None:
    """Persist primitive-only teardown evidence outside pytest FD capture."""
    if _CONFIG is not None:
        _emit(_CONFIG, "qt_disposal", nodeid=test_id, phase=phase, object=label)


def pytest_runtest_logstart(nodeid: str, location: object) -> None:
    if _CONFIG is not None:
        _emit(_CONFIG, "node_start", nodeid=nodeid)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    if not isinstance(item, pytest.Function) or not isinstance(item.instance, unittest.TestCase):
        yield
        return
    case = cast(_UnittestMethodSlot, item.instance)
    original = case._callTestMethod
    had_instance_method = "_callTestMethod" in vars(case)
    previous_instance_method = vars(case).get("_callTestMethod")

    def call(method: Callable[[], object]) -> object:
        try:
            return original(method)
        except BaseException as exc:
            _emit(item.config, "exception_before_unittest_cleanup", nodeid=item.nodeid,
                  **_exception_snapshot(exc))
            raise

    case._callTestMethod = call
    try:
        yield
    finally:
        if had_instance_method:
            # Restore exact instance state without retaining a new bound-self cycle.
            vars(case)["_callTestMethod"] = previous_instance_method
        else:
            del case._callTestMethod


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    if call.excinfo is not None:
        _emit(item.config, "exception_before_pytest_report", nodeid=item.nodeid,
              when=call.when, **_exception_snapshot(call.excinfo.value))
    yield
