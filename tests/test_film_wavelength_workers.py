"""CPU partition parity, frozen-view safety and resource/cancellation contracts."""

from threading import Event
import numpy as np
import pytest
from coat_sim.core import film_performance
from coat_sim.core.film_optics_batch import FrozenCoherentStack
from coat_sim.core.film_performance import CoherentBatchEvaluator, PerformanceOptions


@pytest.fixture(autouse=True)
def cpu_limit(monkeypatch):
    monkeypatch.setattr(film_performance, "available_cpu_workers", lambda: 8)


def stack(polarization="unpolarized", critical=False):
    n = np.tile([2.0, 1.4 + 0.003j, 1.45, 1.52], (17, 1)).astype(complex)
    if critical:
        n[::3, 1] = 1.0
    return FrozenCoherentStack.prepare(
        np.linspace(400, 800, 17), n, polarization=polarization, beta=1.0
    )


def test_wavelength_views_share_immutable_precomputed_arrays():
    original = stack()
    part = original.wavelength_slice(3, 12)
    for key in ("waves", "indices", "transverse", "q", "critical"):
        array = getattr(part, key)
        assert np.shares_memory(array, getattr(original, key))
        with pytest.raises(ValueError):
            array.setflags(write=True)
    for p in original.polarizations:
        for old, new in zip(
            original.interfaces[p] + original.fluxes[p], part.interfaces[p] + part.fluxes[p], strict=True
        ):
            assert np.shares_memory(old, new)
            with pytest.raises(ValueError):
                new.setflags(write=True)
    for a, b in [(-1, 4), (3, 3), (0, 18), (True, 5), (1.0, 4)]:
        with pytest.raises(ValueError):
            original.wavelength_slice(a, b)


@pytest.mark.parametrize("polarization", ["s", "p", "unpolarized"])
@pytest.mark.parametrize("critical", [False, True])
@pytest.mark.parametrize("count", [1, 7])
def test_wave_workers_preserve_spectra_order_and_global_reduction(polarization, critical, count):
    frozen = stack(polarization, critical)
    d = np.random.default_rng(19).uniform(0, 150, (count, 2))
    expected = frozen.evaluate(d)
    evaluator = CoherentBatchEvaluator(
        frozen, PerformanceOptions(backend="cpu", cpu_workers=5, cpu_parallel_axis="wavelengths")
    )
    actual = evaluator.evaluate(d)
    np.testing.assert_allclose(actual, expected, rtol=2e-12, atol=2e-12)
    np.testing.assert_allclose(
        np.mean(actual[:, :, 0] ** 2, axis=1),
        np.mean(expected[:, :, 0] ** 2, axis=1),
        rtol=2e-12,
        atol=2e-12,
    )
    assert evaluator.last_metadata["cpu_workers"] == 5
    assert evaluator.last_metadata["cpu_parallel_axis"] == "wavelengths"
    parts = evaluator._wavelength_parts[5]
    evaluator.evaluate(d)
    assert evaluator._wavelength_parts[5] is parts


def test_auto_axis_uses_wave_workers_for_one_candidate_only():
    evaluator = CoherentBatchEvaluator(stack(), PerformanceOptions(backend="cpu", cpu_workers=4))
    evaluator.evaluate([[80.0, 90.0]])
    assert evaluator.last_metadata["cpu_workers"] == 4
    assert evaluator.last_metadata["cpu_parallel_axis"] == "wavelengths"
    evaluator.evaluate(np.full((8, 2), 80.0))
    assert evaluator.last_metadata["cpu_workers"] == 4
    assert evaluator.last_metadata["cpu_parallel_axis"] == "candidates"
    legacy = CoherentBatchEvaluator(
        stack(), PerformanceOptions(backend="cpu", cpu_workers=4, cpu_parallel_axis="candidates")
    )
    legacy.evaluate([[80.0, 90.0]])
    assert legacy.last_metadata["cpu_workers"] == 1


def test_wave_workers_keep_total_scratch_budget_and_cancel(monkeypatch):
    frozen = stack()
    original = FrozenCoherentStack.evaluate
    seen = []

    def tracked(self, d, *, chunk_bytes, cancel=None):
        seen.append((self.waves.size, chunk_bytes))
        return original(self, d, chunk_bytes=chunk_bytes, cancel=cancel)

    monkeypatch.setattr(FrozenCoherentStack, "evaluate", tracked)
    evaluator = CoherentBatchEvaluator(
        frozen, PerformanceOptions(backend="cpu", cpu_workers=4, chunk_bytes=4096)
    )
    evaluator.evaluate([[80.0, 90.0]])
    assert sorted(n for n, _ in seen) == [4, 4, 4, 5]
    assert sum(b for _, b in seen) == 4096
    event = Event()
    event.set()
    with pytest.raises(InterruptedError):
        evaluator.evaluate([[80.0, 90.0]], cancel=event)


def test_wave_worker_retained_output_budget_can_retain_serial_route():
    evaluator = CoherentBatchEvaluator(
        stack(),
        PerformanceOptions(
            backend="cpu", cpu_workers=8, max_result_bytes=2048, cpu_parallel_axis="wavelengths"
        ),
    )
    actual = evaluator.evaluate(np.full((4, 2), 80.0))
    assert actual.shape == (4, 17, 3)
    assert evaluator.last_metadata["cpu_workers"] == 1
    with pytest.raises(ValueError):
        PerformanceOptions(cpu_parallel_axis="layers")


def test_cancellation_from_wave_job_is_propagated(monkeypatch):
    event = Event()
    original = FrozenCoherentStack.evaluate
    entered = []

    def cancelled_job(self, d, *, chunk_bytes, cancel=None):
        entered.append(self.waves.size)
        event.set()
        return original(self, d, chunk_bytes=chunk_bytes, cancel=cancel)

    monkeypatch.setattr(FrozenCoherentStack, "evaluate", cancelled_job)
    evaluator = CoherentBatchEvaluator(stack(), PerformanceOptions(backend="cpu", cpu_workers=4))
    with pytest.raises(InterruptedError):
        evaluator.evaluate([[80.0, 90.0]], cancel=event)
    assert entered and 4 in evaluator._wavelength_parts
