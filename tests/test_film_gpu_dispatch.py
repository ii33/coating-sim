"""Dispatch-only coverage for optional multi-vendor GPU evaluation.

These tests use fake PyTorch runtime namespaces and a mocked batch evaluator;
they do not require PyTorch, a GPU driver, or accelerator hardware.
"""

from concurrent.futures import CancelledError
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from coat_sim.core import film_optics_gpu
from coat_sim.core.film_optics_batch import FrozenCoherentStack
from coat_sim.core.film_performance import CoherentBatchEvaluator, PerformanceOptions


class FakeRuntime:
    def __init__(self, available=True, index=0):
        self.available = available
        self.index = index
        self.availability_checks = 0

    def is_available(self):
        self.availability_checks += 1
        return self.available

    def current_device(self):
        return self.index


def fake_torch(*, cuda=None, hip=None, xpu_available=True, cuda_available=True):
    cuda_runtime = FakeRuntime(available=cuda_available)
    xpu_runtime = FakeRuntime(available=xpu_available, index=1)
    torch = SimpleNamespace(
        version=SimpleNamespace(cuda=cuda, hip=hip),
        cuda=cuda_runtime,
        xpu=xpu_runtime,
        device=lambda namespace, index: (namespace, index),
    )
    return torch, cuda_runtime, xpu_runtime


def test_auto_dispatch_uses_rocm_build_and_xpu_device():
    torch, cuda, _xpu = fake_torch(cuda=None, hip="6.2")

    backend, runtime, device = film_optics_gpu.select_gpu_runtime(torch, "gpu")

    assert backend == "rocm"
    assert runtime is cuda
    assert device == ("cuda", 0)
    assert cuda.availability_checks == 1


def test_explicit_cuda_never_treats_hip_runtime_as_cuda():
    torch, cuda, _xpu = fake_torch(cuda="12.4", hip="6.2")

    with pytest.raises(RuntimeError, match="NVIDIA CUDA PyTorch build"):
        film_optics_gpu.select_gpu_runtime(torch, "cuda")

    # Although PyTorch exposes its HIP runtime through torch.cuda, the explicit
    # NVIDIA request must be rejected before probing or using that namespace.
    assert cuda.availability_checks == 0


def test_explicit_cuda_selects_cuda_build():
    torch, cuda, _xpu = fake_torch(cuda="12.4", hip=None)

    backend, runtime, device = film_optics_gpu.select_gpu_runtime(torch, "cuda")

    assert (backend, runtime, device) == ("cuda", cuda, ("cuda", 0))
    assert cuda.availability_checks == 1


def test_explicit_xpu_dispatches_to_xpu_namespace():
    torch, _cuda, xpu = fake_torch(cuda=None, hip=None)

    backend, runtime, device = film_optics_gpu.select_gpu_runtime(torch, "xpu")

    assert backend == "xpu"
    assert runtime is xpu
    assert device == ("xpu", 1)
    assert xpu.availability_checks == 1


def test_generic_dispatch_uses_xpu_when_no_cuda_or_rocm_device_is_available():
    torch, unavailable_cuda, xpu = fake_torch(
        cuda="12.4", hip=None, cuda_available=False, xpu_available=True
    )

    backend, runtime, device = film_optics_gpu.select_gpu_runtime(torch, "gpu")

    assert (backend, runtime, device) == ("xpu", xpu, ("xpu", 1))
    assert unavailable_cuda.availability_checks == xpu.availability_checks == 1


@pytest.mark.parametrize(
    "requested,torch,reason",
    [
        ("cuda", fake_torch(cuda=None, hip=None)[0], "NVIDIA CUDA PyTorch build"),
        ("rocm", fake_torch(cuda="12.4", hip=None)[0], "ROCm PyTorch build"),
        ("xpu", fake_torch(cuda=None, hip=None, xpu_available=False)[0], "xpu has no available device"),
        ("gpu", fake_torch(cuda=None, hip=None, xpu_available=False)[0], "xpu has no available device"),
    ],
    ids=["cuda-build-unavailable", "rocm-build-unavailable", "xpu-unavailable", "all-gpu-unavailable"],
)
def test_runtime_selection_reports_unavailable_build_or_device(requested, torch, reason):
    with pytest.raises(RuntimeError, match=reason):
        film_optics_gpu.select_gpu_runtime(torch, requested)


@pytest.mark.parametrize("requested", ["", "cpu", "directml", "CUDA"])
def test_runtime_selection_rejects_invalid_backend(requested):
    torch, _cuda, _xpu = fake_torch()

    with pytest.raises(ValueError, match="GPU runtime must be"):
        film_optics_gpu.select_gpu_runtime(torch, requested)


def test_performance_options_accept_new_explicit_runtime_modes():
    for backend in ("gpu", "cuda", "rocm", "xpu"):
        assert PerformanceOptions(backend=backend).backend == backend

    with pytest.raises(ValueError, match="backend must be"):
        PerformanceOptions(backend="directml")


@pytest.fixture
def stack():
    return FrozenCoherentStack.prepare([500.0, 600.0], [2.0, 1.4, 1.5])


def test_evaluator_success_reports_requested_and_resolved_runtime(stack, monkeypatch):
    made = []

    class SuccessfulGpu:
        def __init__(self, actual_stack, *, backend):
            assert actual_stack is stack
            self.backend = "rocm"  # generic `gpu` resolved from a HIP build
            self.device_name = "mock ROCm device"
            self.upload_seconds = 0.125
            self.last_timing = {"forward_seconds": 0.25}
            self.verified = False
            self.stack = actual_stack
            made.append(self)

        def verify(self, thickness, *, chunk_bytes, cancel=None):
            self.verified = True
            self.verification_shape = np.asarray(thickness).shape

        def evaluate(self, thickness, *, chunk_bytes, cancel=None):
            self.evaluated = np.asarray(thickness).copy()
            return self.stack.evaluate(thickness, chunk_bytes=chunk_bytes, cancel=cancel)

    monkeypatch.setattr(film_optics_gpu, "TorchCoherentBatch", SuccessfulGpu)
    evaluator = CoherentBatchEvaluator(stack, PerformanceOptions(backend="gpu"))
    thickness = np.array([[70.0], [105.0]])

    actual = evaluator.evaluate(thickness)

    np.testing.assert_allclose(actual, stack.evaluate(thickness))
    assert len(made) == 1
    assert made[0].verified is True
    assert made[0].verification_shape == (2, 1)
    assert evaluator.last_metadata["backend"] == "gpu"
    assert evaluator.last_metadata["requested_backend"] == "gpu"
    assert evaluator.last_metadata["gpu_runtime"] == "rocm"
    assert evaluator.last_metadata["device"] == "mock ROCm device"
    assert evaluator.last_metadata["resident_upload_seconds"] == 0.125
    assert evaluator.last_metadata["forward_seconds"] == 0.25


@pytest.mark.parametrize("failure", [RuntimeError("runtime init failed"), NotImplementedError("complex128 unsupported")])
def test_runtime_failure_falls_back_to_cpu_with_reason(stack, monkeypatch, failure):
    attempts = []

    class UnavailableGpu:
        def __init__(self, _stack, *, backend):
            attempts.append(backend)
            raise failure

    monkeypatch.setattr(film_optics_gpu, "TorchCoherentBatch", UnavailableGpu)
    evaluator = CoherentBatchEvaluator(stack, PerformanceOptions(backend="xpu"))
    thickness = np.array([[70.0], [105.0]])

    actual = evaluator.evaluate(thickness)

    np.testing.assert_allclose(actual, stack.evaluate(thickness))
    assert evaluator.last_metadata["backend"] == "cpu"
    assert evaluator.last_metadata["requested_backend"] == "xpu"
    assert evaluator.last_metadata["fallback_reason"] == str(failure)

    # A failed initialization/capability gate is sticky for this evaluator;
    # it must not repeatedly initialize a runtime for later batches.
    again = evaluator.evaluate(thickness * 1.1)
    np.testing.assert_allclose(again, stack.evaluate(thickness * 1.1))
    assert attempts == ["xpu"]
    assert evaluator.last_metadata["fallback_reason"] == str(failure)


@pytest.mark.parametrize("failure", [InterruptedError("cancelled"), CancelledError("cancelled")])
def test_cancellation_from_gpu_is_not_swallowed_as_cpu_fallback(stack, monkeypatch, failure):
    cpu_calls = []

    class CancellingGpu:
        def __init__(self, actual_stack, *, backend):
            self.backend = backend
            self.verified = False
            self.stack = actual_stack
            self.device_name = "mock CUDA device"
            self.upload_seconds = 0.0
            self.last_timing = {}
            self.verify_calls = 0

        def verify(self, *_args, **_kwargs):
            self.verify_calls += 1
            if self.verify_calls == 1:
                raise failure
            self.verified = True

        def evaluate(self, thickness, *, chunk_bytes, cancel=None):
            return self.stack.evaluate(thickness, chunk_bytes=chunk_bytes, cancel=cancel)

    monkeypatch.setattr(film_optics_gpu, "TorchCoherentBatch", CancellingGpu)
    evaluator = CoherentBatchEvaluator(stack, PerformanceOptions(backend="cuda"))

    def forbidden_cpu(*args, **kwargs):
        cpu_calls.append((args, kwargs))
        return stack.evaluate(*args, **kwargs)

    monkeypatch.setattr(evaluator, "_cpu", forbidden_cpu)

    with pytest.raises(InterruptedError, match="film batch cancelled"):
        evaluator.evaluate([[70.0]])

    assert cpu_calls == []
    assert evaluator._gpu_failure is None

    # A later call can resume the same runtime after a transient cancellation.
    resumed = evaluator.evaluate([[70.0]])
    np.testing.assert_allclose(resumed, stack.evaluate([[70.0]]))
    assert evaluator.last_metadata["backend"] == "gpu"
    assert evaluator.last_metadata["gpu_runtime"] == "cuda"
    assert evaluator._gpu_failure is None
    assert cpu_calls == []


def test_capability_cli_reports_build_tags_without_live_probe(monkeypatch):
    from coat_sim import film_performance_cli

    monkeypatch.setattr(
        film_optics_gpu,
        "existing_torch_build_info",
        lambda: {"__version__": "2.7.0", "cuda": None, "hip": "6.2", "xpu": None},
    )
    output = io.StringIO()
    with patch("sys.argv", ["film-performance", "capability"]), patch("sys.stdout", output):
        assert film_performance_cli.main() == 0

    payload = json.loads(output.getvalue())
    assert payload["ok"] is True
    assert payload["result"]["torch_build"] == "2.7.0"
    assert payload["result"]["cuda_build"] is None
    assert payload["result"]["hip_build"] == "6.2"
    assert payload["result"]["xpu_build"] is None
    assert payload["result"]["supported_gpu_runtimes"] == ["cuda", "rocm", "xpu"]
    assert payload["result"]["gpu_live_verified"] is False
    assert "gpu_probe" not in payload["result"]


def test_capability_cli_explicit_live_probe_forwards_backend(monkeypatch):
    from coat_sim import film_performance_cli

    monkeypatch.setattr(
        film_optics_gpu,
        "existing_torch_build_info",
        lambda: {"__version__": "2.7.0", "cuda": None, "hip": None, "xpu": "2.7.0"},
    )
    probes = []

    def fake_probe(backend):
        probes.append(backend)
        return {"available": False, "requested_backend": backend, "reason": "mock unavailable"}

    monkeypatch.setattr(film_optics_gpu, "gpu_capability", fake_probe)
    output = io.StringIO()
    with patch("sys.argv", ["film-performance", "capability", "--live-probe", "--backend", "xpu"]), \
            patch("sys.stdout", output):
        assert film_performance_cli.main() == 0

    payload = json.loads(output.getvalue())
    assert probes == ["xpu"]
    assert payload["result"]["xpu_build"] == "2.7.0"
    assert payload["result"]["gpu_live_verified"] is False
    assert payload["result"]["gpu_probe"] == {
        "available": False, "requested_backend": "xpu", "reason": "mock unavailable"
    }
