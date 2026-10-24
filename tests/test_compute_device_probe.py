"""Offline contract tests for local deposition device probing."""
from __future__ import annotations

import json
import subprocess

import pytest

from coat_sim.core import compute_device_probe as probe


MARKER = "__COAT_SIM_DEVICE_PROBE__="


def _completed(payload: dict, *, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["python", "-c", "probe"],
        returncode=returncode,
        stdout=MARKER + json.dumps(payload) + "\n",
        stderr="",
    )


def test_enumerates_cpu_and_reports_only_probed_gpu_names(monkeypatch):
    replies = iter((
        _completed({"available": True, "name": "Runtime CUDA Device X", "reason": ""}),
        _completed({"available": True, "name": "Runtime DirectML Device Y", "reason": ""}),
    ))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return next(replies)

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    rows = probe.probe_local_compute_targets(timeout_seconds=4)

    assert [row["id"] for row in rows] == [
        "cpu:default", "cuda:default", "directml:default"]
    assert [row["backend"] for row in rows] == ["CPU", "GPU", "DirectML"]
    assert [row["available"] for row in rows] == [True, True, True]
    assert rows[0]["name"] == "CPU"
    assert rows[1]["name"] == "Runtime CUDA Device X"
    assert rows[2]["name"] == "Runtime DirectML Device Y"
    for row in rows[1:]:
        assert "物理 GPU ID 未指定" in row["label"]
        assert row["physical_id"] == ""
    assert len(calls) == 2
    assert all(kwargs["timeout"] == 4 for _, kwargs in calls)
    assert all(command[:2] == [probe.sys.executable, "-c"] for command, _ in calls)
    assert "cupy" in calls[0][0][2]
    assert "torch_directml" in calls[1][0][2]


def test_unavailable_runtimes_still_have_stable_rows_and_reasons(monkeypatch):
    replies = iter((
        _completed({"available": False, "name": "", "reason": "ModuleNotFoundError: No module named 'cupy'"}),
        _completed({"available": False, "name": "", "reason": "RuntimeError: DirectML runtime unavailable"}),
    ))
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: next(replies))

    rows = probe.probe_local_compute_targets()

    assert rows[0]["available"] is True
    assert rows[1]["id"] == "cuda:default"
    assert rows[1]["backend"] == "GPU"
    assert rows[1]["available"] is False
    assert "cupy" in rows[1]["reason"]
    assert rows[1]["name"] == "CUDA 默认 GPU"
    assert rows[2]["id"] == "directml:default"
    assert rows[2]["available"] is False
    assert "DirectML runtime unavailable" in rows[2]["reason"]
    assert all("物理 GPU ID 未指定" in row["label"] for row in rows[1:])


def test_timed_out_probe_is_bounded_and_other_runtime_is_still_probed(monkeypatch):
    replies = iter((
        subprocess.TimeoutExpired(cmd="python -c probe", timeout=2.5),
        _completed({"available": True, "name": "", "reason": ""}),
    ))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs["timeout"])
        result = next(replies)
        if isinstance(result, subprocess.TimeoutExpired):
            raise result
        return result

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    rows = probe.probe_local_compute_targets(timeout_seconds=2.5)

    assert calls == [2.5, 2.5]
    assert rows[1]["available"] is False
    assert "超时" in rows[1]["reason"]
    assert rows[2]["available"] is True
    assert rows[2]["name"] == "DirectML 默认设备"


def test_timeout_is_hard_capped_and_invalid_task_or_timeout_fails_fast(monkeypatch):
    assert probe.DEFAULT_PROBE_TIMEOUT_SECONDS == 8.0
    assert probe.MAX_PROBE_TIMEOUT_SECONDS == 15.0
    calls = []
    monkeypatch.setattr(
        probe.subprocess, "run",
        lambda *args, **kwargs: (calls.append(kwargs["timeout"]) or
                                  _completed({"available": False, "name": "", "reason": "absent"})),
    )
    probe.probe_local_compute_targets(timeout_seconds=999)
    assert calls == [probe.MAX_PROBE_TIMEOUT_SECONDS] * 2

    calls.clear()
    with pytest.raises(ValueError, match="unsupported local compute task"):
        probe.probe_local_compute_targets("appearance")
    with pytest.raises(ValueError, match="finite positive"):
        probe.probe_local_compute_targets(timeout_seconds=float("inf"))
    with pytest.raises(ValueError, match="finite positive"):
        probe.probe_local_compute_targets(timeout_seconds=0)
    with pytest.raises(ValueError, match="finite positive"):
        probe.probe_local_compute_targets(timeout_seconds=True)
    assert calls == []


def test_malformed_child_output_becomes_unavailable_row(monkeypatch):
    replies = iter((
        subprocess.CompletedProcess(["python"], 7, "not-json\n", "child failed"),
        _completed({"available": False, "name": "", "reason": "not installed"}),
    ))
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: next(replies))

    rows = probe.probe_local_compute_targets()

    assert rows[1]["available"] is False
    assert "退出码 7" in rows[1]["reason"]
    assert rows[2]["reason"] == "not installed"


def _child_script(payload: dict, ending: str = "") -> str:
    return f"print({(MARKER + json.dumps(payload))!r}, flush=True)\n{ending}"


@pytest.mark.parametrize(("ending", "exit_code"), [
    ("import sys; sys.exit(7)", 7),
    ("raise RuntimeError('protocol fixture child failed')", 1),
])
def test_success_payload_from_failed_real_child_is_unavailable(ending, exit_code):
    script = _child_script({"available": True, "name": "uncommitted device claim", "reason": ""}, ending)
    control = subprocess.run([probe.sys.executable, "-c", script], capture_output=True,
                             text=True, timeout=probe.DEFAULT_PROBE_TIMEOUT_SECONDS, check=False)
    assert control.returncode == exit_code
    assert MARKER in control.stdout

    result = probe._run_probe(script, "CUDA", probe.DEFAULT_PROBE_TIMEOUT_SECONDS)

    assert result["available"] is False
    assert result["name"] == ""
    assert "CUDA" in result["reason"] and f"退出码 {exit_code}" in result["reason"]


def test_launch_exception_is_unavailable(monkeypatch):
    def cannot_launch(*args, **kwargs):
        raise OSError("protocol fixture cannot start")
    monkeypatch.setattr(probe.subprocess, "run", cannot_launch)

    result = probe._run_probe("pass", "DirectML", probe.DEFAULT_PROBE_TIMEOUT_SECONDS)

    assert result["available"] is False and result["name"] == ""
    assert "OSError" in result["reason"] and "protocol fixture cannot start" in result["reason"]


@pytest.mark.parametrize("output", [
    "native log without a result marker",
    MARKER + '{"available": true',
    MARKER + '[]',
    MARKER + '{"available": "true"}',
    MARKER + '{"name": "missing availability"}',
    MARKER + '{"available": true}\n' + MARKER + '{"available":',
])
def test_invalid_zero_exit_real_child_output_is_unavailable(output):
    result = probe._run_probe(f"print({output!r}, flush=True)", "CUDA",
                              probe.DEFAULT_PROBE_TIMEOUT_SECONDS)

    assert result["available"] is False and result["name"] == ""
    assert "未返回有效结果" in result["reason"]


def test_success_marker_before_real_child_timeout_does_not_make_it_available():
    script = _child_script({"available": True, "name": "unfinished probe", "reason": ""},
                           "import time; time.sleep(30)")

    result = probe._run_probe(script, "CUDA", .2)

    assert result["available"] is False and result["name"] == ""
    assert "超时（0.2 秒）" in result["reason"]


@pytest.mark.parametrize("cuda_exit", [0, 7])
def test_real_child_protocol_preserves_backend_identity(monkeypatch, cuda_exit):
    """Real protocol subprocesses, not evidence of GPU hardware execution."""
    monkeypatch.setattr(probe, "_CUDA_PROBE_SCRIPT", _child_script(
        {"available": True, "name": "Protocol CUDA fixture", "reason": ""},
        f"import sys; sys.exit({cuda_exit})"))
    monkeypatch.setattr(probe, "_DIRECTML_PROBE_SCRIPT", _child_script(
        {"available": True, "name": "Protocol DirectML fixture", "reason": ""}))

    rows = probe.probe_local_compute_targets()

    assert [row["id"] for row in rows] == ["cpu:default", "cuda:default", "directml:default"]
    assert [row["backend"] for row in rows] == ["CPU", "GPU", "DirectML"]
    assert rows[0]["available"] is True
    assert rows[1]["available"] is (cuda_exit == 0)
    assert rows[1]["name"] == ("Protocol CUDA fixture" if cuda_exit == 0 else "CUDA 默认 GPU")
    assert rows[2]["available"] is True and rows[2]["name"] == "Protocol DirectML fixture"
    assert all(row["physical_id"] == "" for row in rows[1:])

