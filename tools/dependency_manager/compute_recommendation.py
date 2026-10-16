"""Read-only, bounded host evidence and conservative end-user install advice.

Run off the GUI thread. This module never installs packages, drivers or changes
settings. GPU visibility and an install candidate do not prove runtime usability.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

END_USER_GROUPS = ("desktop", "appearance")
PROBE_TIMEOUT = 4
MAX_DEVICES = 16
_BACKENDS = ("cpu", "cuda12", "directml", "rocm", "xpu")
# CUDA 12 minor-compatibility baseline, NOT a guarantee for every PTX/kernel.
# https://docs.nvidia.com/cuda/archive/12.1.0/cuda-toolkit-release-notes/index.html
_DRIVER_MIN = {"linux": (525, 60, 13), "win32": (527, 41, 0)}


# Self-contained subprocess scripts: importing the app core would import its
# services and defeat the stdlib-only standalone manager. Keep parity with
# coat_sim/core/compute_device_probe.py numerical checks.
_CUDA_PROBE = r'''
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

_DIRECTML_PROBE = r'''
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


def _run(argv, *, env=None):
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=PROBE_TIMEOUT, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0 and len(result.stdout) <= 65536:
            return result.stdout
    except (OSError, UnicodeError, subprocess.SubprocessError):
        pass
    return None


def _vendor(name):
    text = name.lower()
    if "nvidia" in text:
        return "nvidia"
    if "amd" in text or "radeon" in text or "advanced micro" in text:
        return "amd"
    if "intel" in text:
        return "intel"
    return "unknown"


def _visible_gpus(host_platform):
    devices, warnings = [], []
    if host_platform == "win32":
        powershell = shutil.which("powershell.exe")
        if powershell:
            output = _run([powershell, "-NoProfile", "-NonInteractive", "-Command",
                           "Get-CimInstance Win32_VideoController | Select-Object -First 16 Name,DriverVersion | ConvertTo-Json -Compress"])
            if output is None:
                warnings.append("Windows display adapter probe unavailable or timed out")
            try:
                rows = json.loads(output or "[]")
                if isinstance(rows, dict):
                    rows = [rows]
                for row in rows[:MAX_DEVICES]:
                    name = str(row.get("Name", ""))[:160]
                    devices.append({"name": name, "vendor": _vendor(name),
                                    "driver": str(row.get("DriverVersion", ""))[:80],
                                    "source": "windows_cim"})
            except (ValueError, TypeError, AttributeError):
                warnings.append("Windows display adapter probe returned invalid data")
        else:
            warnings.append("Windows CIM probe unavailable")
    elif host_platform == "linux":
        # PCI vendor IDs enumerate visibility only, including integrated GPUs.
        vendors = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}
        for entry in sorted(Path("/sys/class/drm").glob("card[0-9]*"))[:MAX_DEVICES]:
            if not re.fullmatch(r"card\d+", entry.name):
                continue
            try:
                vendor = vendors.get((entry / "device/vendor").read_text().strip(), "unknown")
                devices.append({"name": entry.name, "vendor": vendor, "driver": "",
                                "source": "linux_drm",
                                "kernel_driver": (entry / "device/driver").resolve().name
                                if (entry / "device/driver").exists() else ""})
            except OSError:
                continue
    elif host_platform == "darwin":
        profiler = shutil.which("system_profiler")
        if profiler:
            output = _run([profiler, "SPDisplaysDataType", "-json"])
            try:
                rows = json.loads(output or "{}").get("SPDisplaysDataType", [])
                for row in rows[:MAX_DEVICES]:
                    name = str(row.get("sppci_model", row.get("_name", "")))[:160]
                    devices.append({"name": name, "vendor": _vendor(name),
                                    "driver": "", "source": "macos_system_profiler"})
            except (ValueError, TypeError, AttributeError):
                warnings.append("macOS display adapter probe returned invalid data")
    smi = shutil.which("nvidia-smi")
    if smi and host_platform in ("linux", "win32"):
        output = _run([smi, "--query-gpu=name,driver_version,compute_cap", "--format=csv,noheader,nounits"])
        if output is not None:
            for row in list(csv.reader(io.StringIO(output)))[:MAX_DEVICES]:
                if len(row) == 3:
                    devices.append({"name": row[0].strip()[:160], "vendor": "nvidia",
                                    "driver": row[1].strip()[:80],
                                    "compute_capability": row[2].strip()[:16], "source": "nvidia_smi"})
        else:
            warnings.append("NVIDIA driver/device probe unavailable or timed out")
    return devices, warnings


def _runtime_probe(executable, backend):
    # Mirror the simulator's numerical device checks without importing Qt, NumPy,
    # or GPU packages into the dependency-manager process.
    from .manager import clean_environment
    script = _CUDA_PROBE if backend == "cuda12" else _DIRECTML_PROBE
    with tempfile.TemporaryDirectory(prefix="coat-device-probe-") as cache:
        env = clean_environment()
        env["CUPY_CACHE_DIR"] = cache
        output = _run([str(executable), "-I", "-B", "-c", script], env=env)
    marker = "__COAT_SIM_DEVICE_PROBE__="
    for line in (output or "").splitlines():
        if line.startswith(marker):
            try:
                result = json.loads(line[len(marker):])
                return result.get("available") is True
            except (ValueError, AttributeError):
                break
    return False


def _cuda_candidate(device, target_platform):
    if device.get("source") != "nvidia_smi":
        return False
    try:
        driver = tuple(int(n) for n in device["driver"].split("."))
        driver = (driver + (0, 0, 0))[:3]
        capability = float(device.get("compute_capability", ""))
        # CuPy 13 CUDA12 installation candidate, conservative known architecture
        # range; unknown/future architectures need manual verification.
        return driver >= _DRIVER_MIN[target_platform] and 5.0 <= capability <= 9.0
    except (ValueError, TypeError, KeyError):
        return False


def recommend_environment(python_identity=None, manual_backend=None, cancel_event=None):
    """Return evidence and advice; never override an explicit backend selection.

    python_identity is a verified manager.probe_python/discover_python identity.
    Without one, host evidence is returned but automatic GPU choice stays CPU.
    Up to two hardware probes and two target kernel probes, 4 seconds each.
    """
    if manual_backend is not None and manual_backend not in _BACKENDS:
        raise ValueError("Unknown compute backend")
    host = {"platform": sys.platform, "os": platform.system(),
            "release": platform.release(), "machine": platform.machine()}
    # Return only the bounded fields needed for compatibility, never inventory
    # or environment variables/credentials.
    identity = python_identity or {}
    target = {key: identity.get(key) for key in
              ("executable", "python", "platform", "machine", "implementation")}
    def cancelled():
        return cancel_event is not None and cancel_event.is_set()
    devices, warnings = ([], []) if cancelled() else _visible_gpus(host["platform"])
    backend, reason = "cpu", "CPU fallback: no verified compatible GPU backend"
    usable = {"cpu": None, "cuda12": False, "directml": False}
    target_platform = target.get("platform")
    compatible = (target.get("implementation") == "CPython"
                  and bool(re.match(r"3\.1[0-3]\.", str(target.get("python", ""))))
                  and target_platform == host["platform"])
    x64 = str(target.get("machine", "")).lower() in ("amd64", "x86_64")
    cuda_platform = compatible and x64 and target_platform in _DRIVER_MIN
    dml_platform = (compatible and x64 and target_platform == "win32"
                    and bool(re.match(r"3\.1[0-2]\.", str(target.get("python", "")))))
    executable = target.get("executable")
    if executable and cuda_platform and not cancelled():
        usable["cuda12"] = _runtime_probe(executable, "cuda12")
    if executable and dml_platform and not cancelled():
        usable["directml"] = _runtime_probe(executable, "directml")
    if usable["cuda12"]:
        backend, reason = "cuda12", "CUDA target interpreter passed a numerical device probe"
    elif usable["directml"]:
        backend, reason = "directml", "DirectML target interpreter passed a numerical device probe"
    elif cuda_platform and any(_cuda_candidate(device, target_platform) for device in devices):
        backend = "cuda12"
        reason = "CUDA12 install candidate: compatible NVIDIA device/driver evidence; runtime not yet verified"
    elif not compatible:
        reason = "CPU fallback: select a supported CPython interpreter before GPU recommendation"
    elif devices:
        reason = "CPU fallback: visible display adapters do not establish a supported working compute backend"
    if backend == "cuda12" and not usable["cuda12"]:
        warnings.append("CUDA runtime/toolkit and wheel compatibility still need install-plan resolution and a post-install numerical probe")
    warnings.append("Mitsuba/DrJit appearance dependencies are separate from deposition GPU backends; GPU support is not universal")
    if manual_backend:
        warnings.append("Manual backend selection preserved; compatibility is validated by the install planner")
    suggested = ("directml" if dml_platform and backend == "cpu"
                 and any(d.get("vendor") in ("amd", "intel", "nvidia") for d in devices)
                 else backend if backend != "cpu" else None)
    return {"schema_version": 1, "host": host, "python": target,
            "status": "cancelled" if cancelled() else "success",
            "operation": "runtime_recommendation",
            "suggested_candidate": suggested,
            "visible_gpus": devices, "recommended_backend": backend,
            "selected_backend": manual_backend or backend,
            "backend_usable": usable, "reason": reason, "warnings": warnings,
            "groups": list(END_USER_GROUPS), "profile": "runtime",
            "install_requires_confirmation": True}
