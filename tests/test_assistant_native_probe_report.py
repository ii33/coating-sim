"""Probe readiness is published whole, without starting a native IPC endpoint."""
import ast
import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def publish_probe_report(tmp_path):
    # Exercise the real fixture's small file publisher without importing the
    # script, whose top-level code starts a native Qt process/IPC owner.
    source = Path(__file__).parent / "fixtures" / "assistant_native_probe.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    publisher = next(node for node in module.body
                     if isinstance(node, ast.FunctionDef) and node.name == "write")
    namespace: dict[str, Any] = {
        "report": tmp_path / "report.json",
        "payload": {"pid": 123, "phase": "ready", "generation": "synthetic"},
        "json": json,
    }
    exec(compile(ast.Module(body=[publisher], type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def test_ready_report_does_not_exist_until_complete(publish_probe_report, monkeypatch):
    report = publish_probe_report["report"]
    original = Path.write_text
    observed = []

    def inspect_write(path, data, *args, **kwargs):
        assert not report.exists()
        result = original(path, data, *args, **kwargs)
        assert not report.exists()
        observed.append(json.loads(path.read_text(encoding="utf-8")))
        return result

    monkeypatch.setattr(Path, "write_text", inspect_write)
    publish_probe_report["write"]()
    assert observed == [publish_probe_report["payload"]]
    assert json.loads(report.read_text(encoding="utf-8")) == publish_probe_report["payload"]


def test_ready_report_update_keeps_complete_previous_payload_until_publication(
        publish_probe_report, monkeypatch):
    report = publish_probe_report["report"]
    previous = {"pid": 123, "phase": "starting", "generation": "synthetic"}
    report.write_text(json.dumps(previous), encoding="utf-8")
    original = Path.write_text
    observed = []

    def inspect_write(path, data, *args, **kwargs):
        assert json.loads(report.read_text(encoding="utf-8")) == previous
        result = original(path, data, *args, **kwargs)
        assert json.loads(report.read_text(encoding="utf-8")) == previous
        observed.append(json.loads(path.read_text(encoding="utf-8")))
        return result

    monkeypatch.setattr(Path, "write_text", inspect_write)
    publish_probe_report["write"]()
    assert observed == [publish_probe_report["payload"]]
    assert json.loads(report.read_text(encoding="utf-8")) == publish_probe_report["payload"]
