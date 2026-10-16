"""Bounded probes for the deposition simulator's optional local runtimes.

The probes intentionally run in short-lived Python subprocesses. Importing a
GPU runtime can hang while a native driver initializes; callers must also run
this synchronous API away from the Qt GUI thread.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from typing import Any


DEFAULT_PROBE_TIMEOUT_SECONDS = 8.0
MAX_PROBE_TIMEOUT_SECONDS = 15.0
_RESULT_MARKER = "__COAT_SIM_DEVICE_PROBE__="


# Keep these scripts self-contained so the child does not need to import the
# application or touch its files, network, settings, or package manager.
_CUDA_PROBE_SCRIPT = r'''
import json, sys
marker = "__COAT_SIM_DEVICE_PROBE__="
try:
    import cupy as cp
    count = int(cp.cuda.runtime.getDeviceCount())
    if count < 1:
        raise RuntimeError("未检测到 CUDA 设备")
    device = cp.cuda.Device(0)
    with device:
        probe = cp.asarray([1.0], dtype=cp.float64)
        value = float(cp.asnumpy(cp.sum(probe * probe)).reshape(-1)[0])
    if value != 1.0:
        raise RuntimeError("CUDA 设备计算探测结果异常")
    props = cp.cuda.runtime.getDeviceProperties(0)
    raw_name = props.get("name", "") if isinstance(props, dict) else ""
    if isinstance(raw_name, bytes):
        raw_name = raw_name.decode("utf-8", "replace")
    name = str(raw_name).replace("\x00", "").strip()[:160]
    result = {"available": True, "name": name, "reason": ""}
except Exception as exc:
    result = {"available": False, "name": "", "reason":
              (type(exc).__name__ + (": " + str(exc) if str(exc) else ""))[:240]}
print(marker + json.dumps(result, ensure_ascii=False), flush=True)
'''

_DIRECTML_PROBE_SCRIPT = r'''
import json, math
marker = "__COAT_SIM_DEVICE_PROBE__="
try:
    import torch
    import torch_directml
    device = None
    name = ""
    try:
        for index in range(int(torch_directml.device_count())):
            try:
                candidate_name = str(torch_directml.device_name(index)).replace("\x00", "").strip()
            except Exception:
                continue
            # Match the simulator's explicit DirectML target selection. The
            # default target name is left unknown if enumeration cannot prove it.
            if "amd" in candidate_name.lower() or "radeon" in candidate_name.lower():
                device = torch_directml.device(index)
                name = candidate_name[:160]
                break
    except Exception:
        pass
    if device is None:
        device = torch_directml.device()
    probe = torch.as_tensor([0.5], dtype=torch.float32).to(device)
    inside = ((probe > 0) & (probe < 1)) ^ torch.zeros_like(probe, dtype=torch.bool)
    trig = torch.sin(probe) ** 2 + torch.cos(probe) ** 2
    sample = torch.where(inside, torch.sqrt(probe ** 2) * trig, torch.zeros_like(probe))
    value = float(torch.sum(sample).detach().to("cpu").numpy())
    if not math.isclose(value, 0.5, rel_tol=1e-5):
        raise RuntimeError("DirectML 设备计算探测结果异常")
    result = {"available": True, "name": name, "reason": ""}
except Exception as exc:
    result = {"available": False, "name": "", "reason":
              (type(exc).__name__ + (": " + str(exc) if str(exc) else ""))[:240]}
print(marker + json.dumps(result, ensure_ascii=False), flush=True)
'''


def _timeout_value(timeout_seconds: float) -> float:
    if isinstance(timeout_seconds, bool):
        raise ValueError("timeout_seconds must be a finite positive number")
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timeout_seconds must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a finite positive number")
    return min(timeout, MAX_PROBE_TIMEOUT_SECONDS)


def _run_probe(script: str, runtime_name: str, timeout_seconds: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"available": False, "name": "", "reason":
                f"{runtime_name} 探测超时（{timeout_seconds:g} 秒）"}
    except OSError as exc:
        return {"available": False, "name": "", "reason":
                f"无法启动本地 Python 探测进程（{type(exc).__name__}: {exc}）"[:240]}

    payload = None
    for line in reversed((completed.stdout or "").splitlines()):
        if line.startswith(_RESULT_MARKER):
            try:
                candidate = json.loads(line[len(_RESULT_MARKER):])
            except (TypeError, ValueError):
                break
            if isinstance(candidate, dict):
                payload = candidate
            break
    if payload is None or type(payload.get("available")) is not bool:
        return {"available": False, "name": "", "reason":
                f"{runtime_name} 探测进程未返回有效结果（退出码 {completed.returncode}）"}

    name = payload.get("name")
    reason = payload.get("reason")
    return {
        "available": payload["available"],
        "name": str(name).replace("\x00", "").strip()[:160] if name else "",
        "reason": str(reason).replace("\x00", " ").strip()[:240] if reason else "",
    }


def probe_local_compute_targets(
    task: str = "deposition", *, timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS
) -> list[dict[str, Any]]:
    """Return local CPU, CuPy CUDA, and DirectML target rows for deposition.

    This function is synchronous and may take up to two bounded subprocess
    timeouts. Invoke it from a worker thread, never directly on the Qt GUI
    thread. The optional GPU rows describe only their default runtime target;
    they do not pin or identify a physical GPU ID.
    """
    if task != "deposition":
        raise ValueError(f"unsupported local compute task: {task}")
    timeout = _timeout_value(timeout_seconds)
    logical_cpus = os.cpu_count() or 1
    rows: list[dict[str, Any]] = [{
        "id": "cpu:default",
        "name": "CPU",
        "label": f"CPU · {logical_cpus} 逻辑核",
        "backend": "CPU",
        "available": True,
        "reason": "",
        "physical_id": "",
    }]

    for identifier, backend, runtime_name, script, default_name in (
        ("cuda:default", "GPU", "CUDA", _CUDA_PROBE_SCRIPT, "CUDA 默认 GPU"),
        ("directml:default", "DirectML", "DirectML", _DIRECTML_PROBE_SCRIPT,
         "DirectML 默认设备"),
    ):
        result = _run_probe(script, runtime_name, timeout)
        actual_name = result["name"] or default_name
        rows.append({
            "id": identifier,
            "name": actual_name,
            "label": f"{actual_name} · 物理 GPU ID 未指定",
            "backend": backend,
            "available": result["available"],
            "reason": result["reason"],
            "physical_id": "",
        })
    return rows


__all__ = ["DEFAULT_PROBE_TIMEOUT_SECONDS", "MAX_PROBE_TIMEOUT_SECONDS",
           "probe_local_compute_targets"]
