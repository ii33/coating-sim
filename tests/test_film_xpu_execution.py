"""Frozen execution bindings and fail-closed desktop forward dispatch."""

from copy import deepcopy
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from typing import Any
import subprocess

import numpy as np
import pytest

from coat_sim.core import film_xpu_execution as route
from coat_sim.core.film_optimization_job import (
    deserialize_request,
    serialize_request,
    execute_film_optimization_job,
    validate_checkpoint,
)
from coat_sim.core.film_performance import CoherentBatchEvaluator
from coat_sim.scheduler.jobs import freeze_spec
from coat_sim.scheduler.result_capability import is_film_optimization_result
from tests.film_performance_fixtures import fixture
from tests.test_film_optimization_batch_job import _batch_request


BINDING = dict(
    kind=route.XPU_KIND,
    index=0,
    uuid="00000000-0000-0000-0000-000000000001",
    name="Intel(R) Graphics",
    torch_version="2.10.0+xpu",
)
pytestmark = pytest.mark.contract


def test_cpu_selection_freezes_workers_axis_and_ignores_gpu_environment(monkeypatch):
    service, design, frozen = fixture()
    monkeypatch.setenv("COAT_SIM_FILM_BACKEND", "xpu")
    monkeypatch.setattr(route, "available_cpu_workers", lambda: 8)
    binding = dict(kind=route.CPU_KIND, cpu_workers=2, cpu_parallel_axis="wavelengths")
    request = replace(_batch_request(design, frozen), execution_device=binding)
    spec = freeze_spec(service, "film_optimization", {"request": request})
    binding["cpu_workers"] = 7
    payload = spec["parameters"]["payload"]
    assert payload["request"]["execution_device"]["cpu_workers"] == 2
    performance: list[dict[str, Any]] = []
    result = execute_film_optimization_job(
        service, payload, cancel_event=Event(), performance_callback=performance.append
    )
    assert result["state"] == "completed" and is_film_optimization_result(result)
    assert validate_checkpoint(service, result) == result
    assert performance and all(row["actual_backend"] == "cpu" for row in performance)
    assert all(row["cpu_parallel_axis"] == "wavelengths" for row in performance)
    assert result["execution"]["forward_backend"] == "cpu"
    result["execution"]["forward_backend"] = "xpu"
    with pytest.raises(ValueError, match="declaration"):
        validate_checkpoint(service, result)


@pytest.mark.parametrize(
    "change",
    [
        dict(index=True),
        dict(index=64),
        dict(uuid="bad"),
        dict(uuid="00000000-0000-0000-0000-00000000000A"),
        dict(name="other GPU"),
        dict(extra=1),
    ],
)
def test_identity_fields_are_strict(change):
    with pytest.raises(ValueError):
        route.validate_binding(dict(BINDING, **change))


def test_legacy_request_omits_device_and_explicit_device_changes_hash():
    service, design, frozen = fixture()
    request = _batch_request(design, frozen)
    assert "execution_device" not in serialize_request(request)
    bound = replace(request, execution_device=deepcopy(BINDING))
    assert deserialize_request(serialize_request(bound)) == bound
    old = freeze_spec(service, "film_optimization", {"request": request})
    new = freeze_spec(service, "film_optimization", {"request": bound})
    assert old != new


@pytest.mark.parametrize("domain", ["serial", "solver", "angles", "objectives", "back"])
def test_unsupported_domains_reject_before_execution(domain):
    _service, design, frozen = fixture()
    request = replace(_batch_request(design, frozen), execution_device=BINDING)
    if domain == "serial":
        request = replace(request, execution_mode="serial")
    elif domain == "solver":
        request = replace(request, solver="powell")
    elif domain == "angles":
        request = replace(
            request, objectives=(replace(request.objectives[0], angles_deg=(0.0, 20.0)),)
        )
    elif domain == "objectives":
        request = replace(request, objectives=request.objectives * 2)
    else:
        request.design_snapshot["substrate"]["sides"] = "double"
    with pytest.raises(ValueError):
        serialize_request(request)


@pytest.mark.parametrize("change", ["uuid", "name", "torch", "fp64", "index", "current"])
def test_stale_hardware_or_runtime_never_runs_forward(monkeypatch, change):
    properties = SimpleNamespace(uuid=BINDING["uuid"], name=BINDING["name"], has_fp64=True)
    runtime = SimpleNamespace(
        is_available=lambda: True,
        device_count=lambda: 1,
        get_device_properties=lambda _index: properties,
        current_device=lambda: 0,
    )
    torch = SimpleNamespace(xpu=runtime, __version__=BINDING["torch_version"])
    if change == "uuid":
        properties.uuid = "00000000-0000-0000-0000-000000000002"
    elif change == "name":
        properties.name = "Intel changed"
    elif change == "torch":
        torch.__version__ = "changed"
    elif change == "fp64":
        properties.has_fp64 = False
    elif change == "index":
        runtime.device_count = lambda: 0
    elif change == "current":
        runtime.current_device = lambda: 1
    monkeypatch.setattr(route, "import_module", lambda _name: torch)
    with pytest.raises(RuntimeError, match="未回退 CPU"):
        route.verify_live_binding(BINDING)


def test_gpu_failure_cannot_call_base_cpu_forward(monkeypatch):
    from coat_sim.core.film_optics_batch import FrozenCoherentStack
    from coat_sim.core import film_optics_gpu

    stack = FrozenCoherentStack.prepare([500.0, 550.0], np.tile([1.0, 1.5, 1.52], (2, 1)))
    monkeypatch.setattr(route, "verify_live_binding", lambda *_args, **_kwargs: None)

    def failed(*_args, **_kwargs):
        raise RuntimeError("controlled XPU failure")

    monkeypatch.setattr(film_optics_gpu, "TorchCoherentBatch", failed)
    monkeypatch.setattr(
        CoherentBatchEvaluator, "_cpu", lambda *_a, **_k: pytest.fail("CPU masquerade")
    )
    evaluator = route._StrictXpuBatch(
        stack, route.StrictXpuOptions(backend="xpu", execution_device=BINDING)
    )
    for _ in range(2):
        with pytest.raises(RuntimeError, match="未回退 CPU"):
            evaluator.evaluate([[80.0]])


def test_probe_timeout_reaps_only_owned_child_and_keeps_protocol_stdout(monkeypatch):
    from coat_sim.agent import processes

    captured: dict[str, Any] = {}

    class Child:
        def communicate(self, *, timeout):
            captured["timeout"] = timeout
            raise subprocess.TimeoutExpired("owned probe", timeout)

    class Tree:
        def __init__(self, child):
            captured["child"] = child

        def close(self):
            captured["closed"] = True

    def start(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return Child()

    monkeypatch.setattr(route.subprocess, "Popen", start)
    monkeypatch.setattr(processes, "ProcessTree", Tree)
    with pytest.raises(RuntimeError, match="探测超时"):
        route.probe_devices(timeout=100.0)
    assert captured["closed"] and captured["timeout"] == 30.0
    assert "--stderr-only" in captured["command"]
    assert "coat_sim.film_xpu_probe" in captured["command"]
    assert captured["kwargs"]["stdout"] == subprocess.PIPE


def test_large_batch_spectrum_scoring_keeps_exact_global_grid():
    service, design, frozen = fixture(layers=1)
    request = _batch_request(design, frozen, budget=6, batch_size=3, iterations=1)
    request = replace(
        request,
        objectives=(replace(request.objectives[0], wavelength_samples=10002),),
        execution_device={"kind": route.CPU_KIND},
    )
    payload = freeze_spec(service, "film_optimization", {"request": request})["parameters"][
        "payload"
    ]
    result = execute_film_optimization_job(service, payload, cancel_event=Event())
    assert result["state"] == "completed"
    assert len(result["candidate"]["per_condition_errors"]) == 10002
    assert validate_checkpoint(service, result) == result
