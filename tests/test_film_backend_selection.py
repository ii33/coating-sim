"""CPU-only regression tests for measured coherent-batch backend selection."""

import pytest

from coat_sim.core import film_performance
from coat_sim.core.film_optics_batch import FrozenCoherentStack
from coat_sim.core.film_performance import CoherentBatchEvaluator, PerformanceOptions


ENVIRONMENT = "test-environment-key"


@pytest.fixture
def stack():
    # One variable layer and two wavelengths keep every calibration dimension
    # explicit while avoiding any CUDA/runtime dependency.
    return FrozenCoherentStack.prepare([500.0, 600.0], [2.0, 1.4, 1.5])


@pytest.fixture(autouse=True)
def fixed_environment_key(monkeypatch):
    monkeypatch.setattr(film_performance, "environment_key", lambda: ENVIRONMENT)


def calibration_region(**overrides):
    region = {
        "min_candidates": 1,
        "max_candidates": 8,
        "min_waves": 2,
        "max_waves": 2,
        "layers": 1,
        "polarization": "unpolarized",
        "chunk_bytes": 64 * 1024**2,
        "repetitions": 3,
        "verified": True,
        "backend": "gpu",
        "cpu_workers": 1,
    }
    region.update(overrides)
    return region


def calibration(**profile_overrides):
    profile = {
        "environment_key": ENVIRONMENT,
        "kernel_version": film_performance.KERNEL_VERSION,
        "regions": [calibration_region()],
    }
    profile.update(profile_overrides)
    return profile


def test_auto_without_calibration_keeps_cpu(stack):
    evaluator = CoherentBatchEvaluator(stack, PerformanceOptions(backend="auto"))

    selected, workers, reason = evaluator._selection(4)

    assert (selected, workers) == ("cpu", 1)
    assert "no matching measured calibration" in reason


def test_verified_matching_region_selects_gpu_without_initializing_it(stack):
    evaluator = CoherentBatchEvaluator(
        stack, PerformanceOptions(backend="auto", calibration=calibration())
    )

    selected, workers, reason = evaluator._selection(4)

    assert (selected, workers) == ("gpu", 1)
    assert reason == "matching bounded measured calibration region"
    # Selection is a CPU-only decision; evaluating a batch is what could load
    # or initialize CUDA.
    assert evaluator._gpu is None


@pytest.mark.parametrize(
    "profile_changes,region_changes",
    [
        ({"environment_key": "stale-environment"}, {}),
        ({"kernel_version": "stale-kernel"}, {}),
        ({}, {"verified": False}),
        ({}, {"repetitions": 2}),
        ({}, {"min_candidates": 5}),
        ({}, {"min_waves": 3}),
        ({}, {"layers": 2}),
        ({}, {"polarization": "s"}),
        ({}, {"chunk_bytes": 1024}),
    ],
    ids=[
        "stale-environment",
        "stale-kernel",
        "unverified",
        "too-few-repetitions",
        "candidate-shape-mismatch",
        "wavelength-shape-mismatch",
        "layer-shape-mismatch",
        "polarization-mismatch",
        "chunk-shape-mismatch",
    ],
)
def test_ineligible_calibration_cannot_select_gpu(stack, profile_changes, region_changes):
    profile = calibration(**profile_changes)
    profile["regions"] = [calibration_region(**region_changes)]
    evaluator = CoherentBatchEvaluator(
        stack, PerformanceOptions(backend="auto", calibration=profile)
    )

    selected, workers, _reason = evaluator._selection(4)

    assert (selected, workers) == ("cpu", 1)
    assert evaluator._gpu is None


@pytest.mark.parametrize("requested", ["cpu", "gpu"])
def test_explicit_backend_is_honored_over_calibration(stack, requested):
    # Explicit CPU ignores a matching GPU profile; explicit GPU is selectable
    # even when no measured profile exists.
    profile = calibration() if requested == "cpu" else None
    evaluator = CoherentBatchEvaluator(
        stack, PerformanceOptions(backend=requested, calibration=profile)
    )

    selected, workers, reason = evaluator._selection(4)

    assert selected == requested
    assert workers == 1
    assert reason == "explicit requested backend"
    assert evaluator._gpu is None
