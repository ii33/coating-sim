from pathlib import Path
import subprocess

import pytest

from tools.dependency_manager import compute_recommendation as cr
from tools.dependency_manager.manager import catalog

ROOT = Path(__file__).resolve().parents[1]


def identity(platform="win32", python="3.12.8", machine="AMD64"):
    return {"platform": platform, "python": python, "machine": machine,
            "implementation": "CPython", "executable": "target-python"}


@pytest.fixture
def probes(monkeypatch):
    monkeypatch.setattr(cr.sys, "platform", "win32")
    monkeypatch.setattr(cr, "_visible_gpus", lambda _: ([], []))
    monkeypatch.setattr(cr, "_runtime_probe", lambda *_: False)


def test_end_user_groups_include_renderer_and_no_developer_tools():
    _, rows = catalog(ROOT, cr.END_USER_GROUPS)
    names = {row["name"] for row in rows}
    assert {"mitsuba", "drjit", "numpy", "scipy", "pyside6-essentials", "pyside6-addons",
            "mcp", "pydantic-ai-slim", "fastmcp-slim", "keyring", "ezdxf", "openpyxl"} <= names
    assert not names.intersection({"pytest", "mpmath", "ruff", "mypy", "pyinstaller", "pygal"})


def test_no_target_no_gpu_autoselection(probes):
    result = cr.recommend_environment()
    assert result["selected_backend"] == "cpu"
    assert result["install_requires_confirmation"] is True
    assert result["profile"] == "runtime"


def test_cuda_candidate_is_not_usable(probes, monkeypatch):
    monkeypatch.setattr(cr, "_visible_gpus", lambda _: ([{
        "name": "NVIDIA GPU", "vendor": "nvidia", "driver": "537.42",
        "compute_capability": "8.9", "source": "nvidia_smi"}], []))
    result = cr.recommend_environment(identity())
    assert result["recommended_backend"] == "cuda12"
    assert result["backend_usable"]["cuda12"] is False


@pytest.mark.parametrize("driver,cap", [("526.99", "8.9"), ("537.42", "3.5"),
                                         ("537.42", "N/A"), ("537.42", "12.0")])
def test_inadequate_or_unknown_cuda_keeps_cpu(probes, monkeypatch, driver, cap):
    monkeypatch.setattr(cr, "_visible_gpus", lambda _: ([{
        "vendor": "nvidia", "driver": driver, "compute_capability": cap,
        "source": "nvidia_smi"}], []))
    assert cr.recommend_environment(identity())["recommended_backend"] == "cpu"


def test_visible_amd_is_not_usable_directml(probes, monkeypatch):
    monkeypatch.setattr(cr, "_visible_gpus", lambda _: ([{
        "name": "AMD Radeon", "vendor": "amd", "source": "windows_cim"}], []))
    result = cr.recommend_environment(identity())
    assert result["selected_backend"] == "cpu"
    assert result["suggested_candidate"] == "directml"
    assert result["backend_usable"]["directml"] is False


def test_directml_actual_probe_required(probes, monkeypatch):
    monkeypatch.setattr(cr, "_runtime_probe", lambda _, backend: backend == "directml")
    assert cr.recommend_environment(identity())["recommended_backend"] == "directml"
    assert cr.recommend_environment(identity(python="3.13.0"))["recommended_backend"] == "cpu"


def test_manual_override_preserved(probes):
    result = cr.recommend_environment(identity(), manual_backend="xpu")
    assert result["selected_backend"] == "xpu"
    assert result["recommended_backend"] == "cpu"
    with pytest.raises(ValueError):
        cr.recommend_environment(manual_backend="made-up")


def test_foreign_or_arm_target_not_autoselected(probes, monkeypatch):
    calls = []
    def record_probe(*args):
        calls.append(args)
        return True
    monkeypatch.setattr(cr, "_runtime_probe", record_probe)
    assert cr.recommend_environment(identity(machine="arm64"))["recommended_backend"] == "cpu"
    assert cr.recommend_environment(identity(platform="linux"))["recommended_backend"] == "cpu"
    assert calls == []


def test_probe_timeout_is_bounded_and_non_mutating(monkeypatch):
    def run(argv, **kwargs):
        assert kwargs["timeout"] == cr.PROBE_TIMEOUT
        assert "shell" not in kwargs
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(cr.subprocess, "run", run)
    assert cr._run(["nvidia-smi", "--query-gpu=name"]) is None


def test_no_inventory_or_credentials_returned(probes):
    target = identity()
    target.update(inventory={"secret": "value"}, password="sensitive")
    result = cr.recommend_environment(target)
    assert "inventory" not in result["python"]
    assert "password" not in result["python"]


def test_cancelled_skips_probes(probes, monkeypatch):
    import threading
    event = threading.Event()
    event.set()
    def unexpected(*args):
        raise AssertionError("Cancelled probe must not run")
    monkeypatch.setattr(cr, "_visible_gpus", unexpected)
    monkeypatch.setattr(cr, "_runtime_probe", unexpected)
    result = cr.recommend_environment(identity(), cancel_event=event)
    assert result["status"] == "cancelled"


def test_standalone_probe_scripts_match_application():
    import ast
    module = ast.parse((ROOT / "coat_sim/core/compute_device_probe.py").read_text())
    scripts = {node.targets[0].id: ast.literal_eval(node.value)
               for node in module.body if isinstance(node, ast.Assign)
               and isinstance(node.targets[0], ast.Name)
               and node.targets[0].id in ("_CUDA_PROBE_SCRIPT", "_DIRECTML_PROBE_SCRIPT")}
    assert cr._CUDA_PROBE == scripts["_CUDA_PROBE_SCRIPT"]
    assert cr._DIRECTML_PROBE == scripts["_DIRECTML_PROBE_SCRIPT"]


def test_windows_cim_malformed_is_safe(probes, monkeypatch):
    # Undo the fixture's visibility replacement for this hardware parser test.
    import importlib
    module = importlib.reload(cr)
    monkeypatch.setattr(module.shutil, "which", lambda name: "powershell.exe" if name == "powershell.exe" else None)
    monkeypatch.setattr(module, "_run", lambda *_: "invalid-json")
    devices, warnings = module._visible_gpus("win32")
    assert devices == []
    assert warnings


def test_cuda_driver_baselines_and_cim_not_cuda_evidence():
    gpu = {"source": "nvidia_smi", "compute_capability": "8.9", "driver": "525.60.13"}
    assert cr._cuda_candidate(gpu, "linux")
    assert not cr._cuda_candidate(gpu, "win32")
    gpu["driver"] = "527.41"
    assert cr._cuda_candidate(gpu, "win32")
    gpu["source"] = "windows_cim"
    assert not cr._cuda_candidate(gpu, "win32")
