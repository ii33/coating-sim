"""Fault controls: real CPU optics, injected failures; no GPU timing claims."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_optics_gpu, film_optimization_structure, film_synthesis_audit
from coat_sim.core import film_synthesis_forward as forward
from coat_sim.core.film_performance import FrozenFilmEvaluator, PerformanceOptions
from coat_sim.core.film_synthesis_parallel import EvaluationTask, OrderedEvaluationExecutor
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


@pytest.mark.parametrize("boundary", ["training", "audit"])
def test_physical_runtime_failure_returns_failed_with_complete_best(monkeypatch, boundary):
    service, request = setup_problem(upper=.05 if boundary == "audit" else .005)
    if boundary == "training":
        original = film_optimization_structure.simulate_with_thickness_jacobian
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected physical training failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(film_optimization_structure, "simulate_with_thickness_jacobian", fail_second)
    else:
        def fail_audit(*args, **kwargs):
            raise RuntimeError("injected physical audit failure")

        monkeypatch.setattr(film_synthesis_audit, "audit_frozen_synthesis_candidate", fail_audit)
    result = synthesis.synthesize_film_design(service, request)
    assert result.state == "failed"
    assert "injected physical" in result.termination_reason
    assert result.candidate_design is not None and result.training_score is not None
    assert result.candidate_design["front_layers"] == []
    assert result.training_score["errors"][0].actual == pytest.approx(.04)
    assert result.failed_evaluations >= 1
    assert result.performance["cleanup_complete"]
    print(boundary, result.termination_reason, result.evaluations, result.failed_evaluations)


@pytest.mark.parametrize("boundary,error", [
    ("progress", RuntimeError("observer progress failed")),
    ("audit_progress", ValueError("observer audit progress failed")),
    ("snapshot", OSError("checkpoint disk write failed")),
])
def test_observer_failure_propagates_original_and_closes_owned_executor(monkeypatch, boundary, error):
    service, request = setup_problem(upper=.05)
    closed = []
    original = OrderedEvaluationExecutor.close

    def close(executor):
        original(executor)
        closed.append(executor.diagnostics()["cleanup_complete"])

    monkeypatch.setattr(OrderedEvaluationExecutor, "close", close)

    def progress(event):
        if boundary == "progress" or boundary == "audit_progress" and event.stage == "audit_progress":
            raise error

    def snapshot(*args):
        raise error

    with pytest.raises(type(error)) as caught:
        synthesis.synthesize_film_design(service, request, progress_callback=progress,
            snapshot_callback=snapshot if boundary == "snapshot" else None)
    assert caught.value is error
    assert closed and all(closed)
    print(boundary, type(error).__name__, "original exception propagated; owned executor closed")


def _failed_gate(monkeypatch):
    class FailedGate:
        """Inject a verification failure after one actual CPU oracle spectrum."""
        def __init__(self, stack, *, backend):
            self.stack = stack
            self.verified = False
            self.last_verification = {}

        def verify(self, thickness, *, chunk_bytes, cancel):
            self.stack.evaluate(thickness[:1], chunk_bytes=chunk_bytes, cancel=cancel)
            self.last_verification.update(cpu_completed=1, gpu_completed=0,
                gpu_completed_wavelength_points=0, wavelength_samples=self.stack.waves.size,
                complete=False)
            raise RuntimeError("injected gate failure after real CPU oracle")

    monkeypatch.setattr(film_optics_gpu, "TorchCoherentBatch", FailedGate)


def test_failed_gpu_gate_preserves_real_cpu_verification_work_on_fallback(monkeypatch):
    _failed_gate(monkeypatch)
    service, request = setup_problem()
    request = synthesis.freeze_film_synthesis_request(service, request)
    engine = forward.SynthesisForward(request, synthesis._proxy(request))
    evaluator = FrozenFilmEvaluator(engine._snapshot(request.environment),
        dict(wavelength_min_nm=520., wavelength_max_nm=580., samples=5),
        options=PerformanceOptions(backend="cuda", cpu_workers=1))
    response = evaluator.evaluate_thickness_batch((), np.empty((3, 0)))
    assert response["metadata"]["backend"] == "cpu"
    assert "injected gate failure" in response["metadata"]["fallback_reason"]
    verification = response["metadata"]["verification"]
    assert verification["cpu_completed"] == 1 and verification["gpu_completed"] == 0
    assert not verification["complete"]
    assert np.allclose(response["R"], .04)
    print("real CPU gate and fallback", verification, "3 candidates x 5 samples")


def test_failed_forward_batch_counts_each_unknown_candidate_once(monkeypatch):
    _failed_gate(monkeypatch)
    service, request = setup_problem()
    request = synthesis.freeze_film_synthesis_request(service, replace(request, forward_backend="cuda"))
    # Identity resolution is isolated; this is CPU fault accounting, not a GPU execution claim.
    monkeypatch.setattr(forward, "resolve_forward_device", lambda *args: {"runtime": "cuda"})
    engine = forward.SynthesisForward(request, synthesis._proxy(request))
    try:
        with pytest.raises(ValueError, match="used cpu.*injected gate failure"):
            engine._spectra([EvaluationTask(request.environment, False) for _ in range(3)])
    finally:
        engine.close()
    work = engine.diagnostics()
    assert work["candidates_submitted"] == work["unknown_candidates"] == 3
    assert work["candidates_completed"] == 0 and not work["actual_work_complete"]
    assert work["objective_spectral_calls"] is None
    assert work["known_objective_spectral_calls"] == 3
    assert work["known_wavelength_points"] == 15
    assert work["verification_cpu_calls"] == 1 and work["verification_cpu_points"] == 5
    assert work["verification_gpu_calls"] == 0
    print("failed batch actual-work diagnostics", work)
