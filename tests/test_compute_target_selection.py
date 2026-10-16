"""Offline, temp-path contract checks for shared calculation target selection."""
from __future__ import annotations

import json
import time
from typing import Any, cast
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication

from coat_sim.ui import compute_target as module


@pytest.fixture
def app():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def isolated_preferences(tmp_path):
    return tmp_path / "config" / "compute_target_selections.json"


class LocalClient:
    def __init__(self, root):
        self.root = root

    def capabilities(self):
        return {"protocol_version": 1, "executors": ["process_recipe", "film_optimization"]}


def _spin(app, predicate, seconds=3):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def test_local_device_probe_binding_and_algorithm_filter(app, isolated_preferences,
                                                         tmp_path, monkeypatch):
    from coat_sim.core import compute_device_probe
    rows = [
        {"id": "cpu:default", "label": "CPU · 8 逻辑核", "backend": "CPU",
         "available": True, "reason": "", "physical_id": ""},
        {"id": "cuda:default", "label": "CUDA 默认设备 · 物理 GPU ID 未指定", "backend": "GPU",
         "available": True, "reason": "", "physical_id": ""},
        {"id": "directml:default", "label": "DirectML · 物理 GPU ID 未指定", "backend": "DirectML",
         "available": False, "reason": "库不可用", "physical_id": ""},
    ]
    monkeypatch.setattr(compute_device_probe, "probe_local_compute_targets", lambda: rows)
    monkeypatch.setattr("coat_sim.ui.settings.load_ui_settings", lambda: {})
    target = module.ComputeTarget(tmp_path, "deposition", client_factory=LocalClient)
    _spin(app, lambda: len(target.devices) == 3)
    assert target.canSubmit
    assert target.hostId == "local"
    assert not next(row for row in target.devices if row["id"] == "directml:default")["available"]
    target.setDeviceId("cuda:default")
    client, device = target.client_for_submit(algorithm="fast")
    assert isinstance(client, LocalClient)
    assert device["id"] == "cuda:default" and device["backend"] == "GPU"
    assert target.deviceId == "cuda:default"
    assert json.loads(isolated_preferences.read_text())["deposition"] == {
        "host_id": "local", "device_id": "cuda:default"}

    target.setAlgorithm("reference")
    assert not target.canSubmit
    assert next(row for row in target.devices if row["id"] == "cuda:default")["reason"]
    with pytest.raises(module.ComputeTargetError, match="只支持 CPU"):
        target.client_for_submit(algorithm="reference")
    target.setAlgorithm("fast")
    assert target.canSubmit


def test_optional_gpu_probe_failure_preserves_local_cpu(app, tmp_path, monkeypatch):
    from coat_sim.core import compute_device_probe
    def failed_probe():
        raise RuntimeError("driver internal path must not enter UI")
    monkeypatch.setattr(compute_device_probe, "probe_local_compute_targets", failed_probe)
    monkeypatch.setattr("coat_sim.ui.settings.load_ui_settings", lambda: {})
    target = module.ComputeTarget(tmp_path, "deposition", client_factory=LocalClient)
    _spin(app, lambda: "可选加速设备探测失败" in target.message)
    assert target.canSubmit
    assert target.deviceId == "cpu:default"
    assert "internal path" not in target.message


def test_remote_without_saved_token_never_connects(app, isolated_preferences,
                                                   tmp_path, monkeypatch):
    url = "https://compute.example.invalid:8765"
    monkeypatch.setattr("coat_sim.ui.settings.load_ui_settings", lambda: {
        "compute_server_url": url, "compute_server_tls": True,
        "compute_server_ca_file": ""})
    monkeypatch.setattr("coat_sim.ui.assistant_credentials.read_compute_token",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr("coat_sim.scheduler.remote_client.make_remote_client",
                        lambda *_args, **_kwargs: pytest.fail("remote network client constructed"))
    target = module.ComputeTarget(tmp_path, "film_optimization", client_factory=LocalClient)
    target.setHostId(module.remote_host_id(url))
    _spin(app, lambda: "令牌" in target.message)
    assert not target.canSubmit
    assert target.devices == []
    with pytest.raises(module.ComputeTargetError):
        target.client_for_submit()
    assert url not in isolated_preferences.read_text()


def test_remote_capabilities_require_versions_and_bound_devices():
    old = {"protocol_version": 1, "executors": ["film_optimization"]}
    with pytest.raises(module.ComputeTargetError) as missing:
        module.validate_capabilities(old, "film_optimization", remote=True)
    assert missing.value.code == "incompatible_protocol"
    with pytest.raises(module.ComputeTargetError) as mismatch:
        module.validate_capabilities({**old, "protocol_version": 2}, "film_optimization", remote=True)
    assert mismatch.value.code == "incompatible_protocol"
    rows = module.validate_capabilities({**old, "api_version": 1, "executor_schema_version": 1,
        "device_schema_version": 1, "device_binding_version": 1,
        "devices": [{"id": "cpu:node-a", "name": "CPU node A",
            "backend": "CPU", "available": True, "executors": ["film_optimization"],
            "algorithms": []}]},
        "film_optimization", remote=True)
    assert rows[0]["id"] == "cpu:node-a"


def test_remote_cuda_default_is_visible_but_unavailable_until_real_probe():
    base = {"protocol_version": 1, "api_version": 1, "executor_schema_version": 1,
            "device_schema_version": 1, "device_binding_version": 1,
            "executors": ["appearance_render"],
            "devices": [{"id": "cuda:default", "name": "NVIDIA CUDA 默认设备 · 未指定物理 GPU",
                         "backend": "gpu_cuda", "available": True, "physical_id": "",
                         "binding": "default", "runtime_probe": "failed",
                         "executors": ["appearance_render"], "algorithms": []}]}
    rows = module.validate_capabilities(base, "appearance_render", remote=True)
    assert len(rows) == 1 and rows[0]["available"] is False
    assert "CUDA" in rows[0]["reason"]
    base["devices"][0]["runtime_probe"] = "passed"
    rows = module.validate_capabilities(base, "appearance_render", remote=True)
    assert rows[0]["available"] is True and rows[0]["physical_id"] == ""


def test_appearance_cuda_requires_positive_runtime_probe(app, isolated_preferences,
                                                         tmp_path, monkeypatch):
    monkeypatch.setattr("coat_sim.ui.settings.load_ui_settings", lambda: {})
    target = module.ComputeTarget(tmp_path, "appearance")
    assert target.canSubmit
    target.set_appearance_backend("gpu_cuda")
    assert not target.canSubmit
    target.publish_appearance_cuda("checking", "正在独立进程检测")
    assert not target.canSubmit
    target.publish_appearance_cuda("available", "已完成真实分配")
    assert target.canSubmit
    assert next(row for row in target.devices if row["id"] == "cuda:default")["physical_id"] == ""
    target.publish_appearance_cuda("unavailable", "设备不可用")
    assert not target.canSubmit


def test_deposition_result_attests_selected_backend():
    from coat_sim.ui.main import _assert_selected_backend_result
    _assert_selected_backend_result({"device": "CPU"}, "CPU")
    _assert_selected_backend_result({"device": "GPU (CuPy)"}, "GPU")
    _assert_selected_backend_result({"device": "GPU (DirectML: AMD)"}, "DirectML")
    with pytest.raises(ValueError, match="所选设备不符"):
        _assert_selected_backend_result({"device": "CPU"}, "GPU")
    with pytest.raises(ValueError, match="所选设备不符"):
        _assert_selected_backend_result({"device": "GPU (DirectML: AMD)"}, "GPU")


@pytest.mark.parametrize("saved_device", ("", "cuda:physical-0"))
def test_optimizer_gpu_enumeration_does_not_imply_gpu_solver_support(
        app, isolated_preferences, tmp_path, monkeypatch, saved_device):
    from concurrent.futures import Future
    import shiboken6

    monkeypatch.setattr("coat_sim.ui.settings.load_ui_settings", lambda: {})
    target = module.ComputeTarget(tmp_path, "film_optimization", client_factory=LocalClient)
    try:
        target._host_id = "remote:test"
        target._device_id = saved_device
        future: Future[Any] = Future()
        # Deliberately put the advertised GPU first. Auto-selection must use
        # the task-filtered catalog, rather than choosing it and failing later.
        future.set_result((target._generation, [
            {"id": "cuda:physical-0", "backend": "gpu_cuda", "available": True,
             "label": "CUDA GPU", "reason": "", "physical_id": "GPU-0"},
            {"id": "cpu:node", "backend": "CPU", "available": True,
             "label": "CPU", "reason": "", "physical_id": ""},
        ], object(), "remote"))
        with patch.object(target, "_future", future):
            target._finish_probe()
        devices = cast(list[dict[str, Any]], target.devices)
        gpu = devices[0]
        assert not gpu["available"]
        assert "CPU 求解" in gpu["reason"]
        if saved_device:
            assert target.deviceId == saved_device
            assert not target.canSubmit
            assert "CPU 求解" in cast(str, target.message)
        else:
            assert target.deviceId == "cpu:node"
            assert target.canSubmit
        target.setDeviceId("cuda:physical-0")
        assert target.deviceId == (saved_device or "cpu:node")
    finally:
        target._executor.shutdown(wait=True, cancel_futures=True)
        shiboken6.delete(target)
