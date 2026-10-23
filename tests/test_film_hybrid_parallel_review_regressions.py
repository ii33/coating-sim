"""Independent-review regressions retain real idle-worker memory in admission."""
from __future__ import annotations

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_parallel as parallel
from tests.test_film_hybrid_parallel import _problem

pytestmark = pytest.mark.algorithm


def test_singleton_tail_is_not_mislabeled_as_memory_limitation(monkeypatch):
    monkeypatch.setattr(parallel, "available_cpu_workers", lambda: 2)
    executor = parallel.OrderedEvaluationExecutor(2)
    try:
        assert not executor.eligible(1, 1_000_000, True, phase="hybrid_prior", worker_limit=1)
        assert executor.selection_reasons == {"insufficient_candidates": 1}
        assert not executor.eligible(2, 1_000_000, True, phase="hybrid_prior", worker_limit=1)
        assert executor.selection_reasons == {"insufficient_candidates": 1, "resource_guard_serial": 1}
    finally:
        executor.close()


def test_serial_singleton_batches_include_retained_real_worker_baselines(monkeypatch):
    # Only the tiny regression fixture bypasses cold-start amortization. The
    # separate request-19 production-threshold test remains unchanged.
    monkeypatch.setattr(parallel, "MIN_PARALLEL_WORK", 0)
    result = synthesis.synthesize_film_design(None,
        _problem(workers=19, memory=448 * 1024**2, batch_size=2))
    assert result.performance["cleanup_complete"]
    assert result.performance["effective_workers"] == 2
    batches = result.performance["search"]["hybrid"]["parallel_batches"]
    idle = [row for row in batches if row["resource_worker_cap"] == 1
            and row["resident_workers_estimate"] > 0]
    assert idle, "exercise serial singletons while the actual two-worker pool is retained"
    for row in idle:
        baseline = row["resident_worker_baseline_estimate_bytes"]
        assert baseline == row["resident_workers_estimate"] * 128 * 1024**2
        assert row["estimated_peak_bytes"] >= row["parent_working_set_estimate_bytes"] + baseline
        assert row["estimated_peak_bytes"] <= row["frozen_budget_bytes"]
