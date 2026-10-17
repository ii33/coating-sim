"""Numerical/operator regression without requiring an Intel GPU."""

from types import SimpleNamespace
import numpy as np
import pytest

from coat_sim.core.film_optics_gpu import TorchCoherentBatch, _squared_magnitude
from coat_sim.core.film_optics_batch import FrozenCoherentStack
from coat_sim.core import film_performance
from coat_sim.core.film_performance import CoherentBatchEvaluator, PerformanceOptions


class MultiplyOnly(np.ndarray):
    def __pow__(self, exponent, modulo=None, /):
        raise AssertionError("scalar pow must not be used")


def test_squared_magnitude_retains_fp64_without_scalar_pow():
    values = np.array([0j, 1 + 2j, 1e150 + 1e150j, 1e-150j], np.complex128)
    torch = SimpleNamespace(abs=lambda x: np.abs(x).view(MultiplyOnly))
    actual = _squared_magnitude(torch, values)
    assert actual.dtype == np.float64
    np.testing.assert_array_equal(actual, np.abs(values) ** 2)


@pytest.mark.parametrize("polarization", ["s", "p", "unpolarized"])
@pytest.mark.parametrize("layers", [0, 2, 60])
def test_real_torch_kernel_cpu_reference(polarization, layers):
    # Exercise exactly the product Torch recurrence on CPU, not a fake optical
    # implementation. Hardware acceptance is the separate native benchmark.
    torch = pytest.importorskip("torch")
    indices = [1.0] + [2.1 + 0.003j if i % 2 else 1.45 for i in range(layers)] + [1.52]
    stack = FrozenCoherentStack.prepare(np.linspace(400, 800, 41), indices, 47, polarization)
    evaluator = TorchCoherentBatch.__new__(TorchCoherentBatch)
    evaluator.torch = torch
    evaluator.waves = torch.tensor(stack.waves.copy(), dtype=torch.float64)
    evaluator.q = torch.tensor(stack.q.copy(), dtype=torch.complex128)
    evaluator.interfaces = {
        p: tuple(torch.tensor(x.copy()) for x in pair) for p, pair in stack.interfaces.items()
    }
    evaluator.fluxes = {
        p: tuple(torch.tensor(x.copy()) for x in pair) for p, pair in stack.fluxes.items()
    }
    d = np.random.default_rng(19).uniform(0, 150, (4, layers))
    actual = evaluator._compute(torch.tensor(d), 0, 41).numpy()
    np.testing.assert_allclose(actual, stack.evaluate(d), rtol=2e-12, atol=2e-12)


def test_cpu_workers_follow_available_cpus_and_keep_result_order(monkeypatch):
    monkeypatch.setattr(film_performance, "available_cpu_workers", lambda: 8)
    stack = FrozenCoherentStack.prepare([400.0, 600.0], [1.0, 2.1, 1.45, 1.52])
    d = np.random.default_rng(19).uniform(20, 150, (16, 2))
    evaluator = CoherentBatchEvaluator(stack, PerformanceOptions(backend="cpu", cpu_workers=8))
    np.testing.assert_array_equal(evaluator.evaluate(d), stack.evaluate(d))
    assert evaluator.last_metadata["cpu_workers"] == 8
    for workers in [0, 9, True]:
        with pytest.raises(ValueError):
            PerformanceOptions(cpu_workers=workers)


def test_low_cpu_limit_is_respected(monkeypatch):
    monkeypatch.setattr(film_performance, "available_cpu_workers", lambda: 1)
    assert PerformanceOptions().cpu_workers == 1
    with pytest.raises(ValueError):
        PerformanceOptions(cpu_workers=2)


def test_package_and_cpu_changes_invalidate_calibration_key(monkeypatch):
    original = film_performance.metadata.version
    before = film_performance.environment_key()
    monkeypatch.setattr(
        film_performance.metadata,
        "version",
        lambda package: "different-torch" if package == "torch" else original(package),
    )
    assert film_performance.environment_key() != before
    changed = film_performance.environment_key()
    monkeypatch.setattr(film_performance, "available_cpu_workers", lambda: 999)
    assert film_performance.environment_key() != changed


def test_xpu_without_fp64_is_rejected_before_allocating_or_downgrading():
    from coat_sim.core.film_optics_gpu import verify_gpu_dtype

    runtime = SimpleNamespace(get_device_properties=lambda _device: SimpleNamespace(has_fp64=False))
    torch = SimpleNamespace(
        tensor=lambda *_a, **_k: pytest.fail("FP64 gate must precede tensor creation")
    )
    with pytest.raises(RuntimeError, match="does not report FP64"):
        verify_gpu_dtype(torch, runtime, SimpleNamespace(type="xpu"))


def test_unknown_windows_driver_identity_cannot_reuse_cross_process_profile(monkeypatch):
    import sys

    class EmptyRegistry:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        OpenKey=lambda *_a: EmptyRegistry(),
        QueryInfoKey=lambda _key: (0, 0, 0),
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(film_performance.sys, "platform", "win32")
    monkeypatch.setattr(film_performance, "available_cpu_workers", lambda: 1)
    monkeypatch.setattr(film_performance, "_PROCESS_CALIBRATION_NONCE", "process-a")
    first = film_performance.environment_key()
    monkeypatch.setattr(film_performance, "_PROCESS_CALIBRATION_NONCE", "process-b")
    assert film_performance.environment_key() != first


def test_empty_gpu_batch_rejected_without_starting_any_forward_work():
    evaluator = TorchCoherentBatch.__new__(TorchCoherentBatch)
    evaluator.torch = SimpleNamespace()
    evaluator.stack = SimpleNamespace(indices=np.empty((2, 4)))
    with pytest.raises(ValueError, match="finite nonnegative"):
        evaluator.evaluate(np.empty((0, 2)))


@pytest.mark.parametrize("device,dtype", [("cpu", "float64"), ("xpu:0", "float32")])
def test_wrong_device_or_precision_never_reaches_readback(monkeypatch, device, dtype):
    from contextlib import nullcontext

    evaluator = TorchCoherentBatch.__new__(TorchCoherentBatch)
    evaluator.torch = SimpleNamespace(
        no_grad=nullcontext,
        float64="float64",
        tensor=lambda value, **_kwargs: SimpleNamespace(shape=value.shape),
    )
    evaluator.stack = SimpleNamespace(indices=np.empty((1, 4)))
    evaluator.waves = SimpleNamespace(numel=lambda: 1)
    evaluator.device = "xpu:0"
    evaluator.runtime = SimpleNamespace(synchronize=lambda _device: None)
    block = SimpleNamespace(
        device=device,
        dtype=dtype,
        cpu=lambda: pytest.fail("invalid backend output must never be reported/read back"),
    )
    monkeypatch.setattr(evaluator, "_compute", lambda *_a, **_kw: block)
    with pytest.raises(RuntimeError, match="changed device or FP64"):
        evaluator.evaluate([[80.0, 90.0]])
