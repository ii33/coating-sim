from __future__ import annotations

from dataclasses import asdict, replace
import os
from threading import Event, Timer
import time

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_parallel as parallel
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def semantic(result):
    value = asdict(result)
    value.pop("performance")
    value["frozen_request"].pop("cpu_workers")
    return value


@pytest.mark.parametrize("workers", [-1, True, 1.5])
def test_invalid_cpu_workers_are_rejected(workers):
    service, request = setup_problem()
    with pytest.raises(ValueError, match="cpu_workers"):
        synthesis.synthesize_film_design(service, replace(request, cpu_workers=workers))


def test_small_explicit_multicore_request_remains_lazy():
    service, request = setup_problem()
    result = synthesis.synthesize_film_design(service, replace(request, cpu_workers=4))
    assert result.targets_met
    assert result.performance["backend"] == "serial"
    assert result.performance["worker_pids"] == []
    assert result.performance["cleanup_complete"]
    assert result.validation_score is not None
    print("lazy", result.evaluations, result.validation_score["max_error"], result.performance)


@pytest.mark.parametrize("workers", [2, 4])
def test_real_headless_growth_preserves_full_semantics(monkeypatch, workers):
    monkeypatch.setattr(parallel, "MIN_PARALLEL_WORK", 0)
    service, request = setup_problem()
    request = replace(request, max_cycles=1, max_evaluations=None,
                      growth_quarter_wave_fractions=(.25, .5, 1., 1.5, 2.))
    serial = synthesis.synthesize_film_design(service, request)
    result = synthesis.synthesize_film_design(service, replace(request, cpu_workers=workers))
    assert semantic(result) == semantic(serial)
    performance = result.performance
    assert performance["backend"] == "headless_process"
    assert performance["effective_workers"] == workers
    assert performance["discarded_tasks"] > 0
    assert performance["completed_tasks"] + performance["unknown_tasks"] == performance["submitted_tasks"]
    assert performance["active_worker_pids"] == [] and performance["cleanup_complete"]
    assert all(not row["qt_imported"] for row in performance["workers"])
    assert all(set(row["thread_environment"].values()) == {"1"} for row in performance["workers"])
    assert result.validation_score is not None
    print("real ordered", workers, result.evaluations, result.validation_score["max_error"], performance)


def test_finite_growth_parallelizes_only_complete_budgeted_batch(monkeypatch):
    monkeypatch.setattr(parallel, "MIN_PARALLEL_WORK", 0)
    service, request = setup_problem()
    request = replace(request, max_cycles=1, max_evaluations=300)
    serial = synthesis.synthesize_film_design(service, replace(request, cpu_workers=1))
    result = synthesis.synthesize_film_design(service, replace(request, cpu_workers=2))
    assert semantic(result) == semantic(serial)
    decisions = [row for row in result.performance["batch_decisions"] if row["phase"] == "growth"]
    assert decisions and any(row["budget_allows"] and row["parallel_selected"] for row in decisions)
    assert result.performance["submitted_tasks"] > 0
    assert result.performance["submitted_tasks"] <= result.evaluations <= request.max_evaluations
    assert result.performance["completed_tasks"] + result.performance["unknown_tasks"] == result.performance["submitted_tasks"]
    assert result.performance["active_worker_pids"] == [] and result.performance["cleanup_complete"]
    print("finite budgeted growth", result.evaluations, result.performance)


def test_parent_exception_closes_owned_real_workers(monkeypatch):
    monkeypatch.setattr(parallel, "MIN_PARALLEL_WORK", 0)
    service, request = setup_problem()
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    executor = parallel.OrderedEvaluationExecutor(2)
    executor.configure(synthesis._proxy(frozen))
    task = parallel.EvaluationTask(frozen.environment)
    with pytest.raises(RuntimeError, match="parent callback"):
        try:
            with executor.batch([task] * 4, count=4, work=1, budget_allows=True) as values:
                _, outcome = next(values)
                executor.take(outcome)
                raise RuntimeError("parent callback")
        finally:
            executor.close()
    assert executor.diagnostics()["cleanup_complete"]
    assert executor.diagnostics()["active_worker_pids"] == []
    assert all(child.actual_dead and not child.reader.is_alive() for child in executor.children)
    assert all(outcome.value is None for outcome in executor.outcomes.values())
    print("exception cleanup", executor.diagnostics())


def test_cancel_during_backpressured_startup_reaps_writer_and_owned_tree(monkeypatch, tmp_path):
    # Only these explicitly owned workers receive this temporary startup hook.
    marker = tmp_path / "entered"
    (tmp_path / "sitecustomize.py").write_text(
        "import pathlib, time\n"
        f"pathlib.Path({str(marker)!r}).write_text('worker startup blocked')\n"
        "time.sleep(30)\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    cancel = Event()
    executor = parallel.OrderedEvaluationExecutor(2, cancel)
    # The child has not begun reading; this exceeds the pipe capacity.
    executor.configure({"large_frozen_payload": "x" * (2 * 1024**2)})
    timer = Timer(.7, cancel.set)
    before = time.monotonic()
    timer.start()
    try:
        with pytest.raises(parallel.physical._StructureCancelled):
            executor._ensure_pool()
    finally:
        executor.close()
        timer.cancel()
        timer.join()
    elapsed = time.monotonic() - before
    assert marker.exists(), "actual owned interpreter must enter the blocked startup"
    assert elapsed < 5
    assert executor.diagnostics()["cleanup_complete"]
    assert all(child.process.poll() is not None and not child.reader.is_alive()
               and child.writer is not None and not child.writer.is_alive()
               for child in executor.children)
    print("backpressured startup cancellation", elapsed, executor.diagnostics())


@pytest.mark.skipif(os.name != "nt", reason="Windows owned Job fault injection")
def test_idle_writer_is_reaped_after_worker_exits_before_close():
    executor = parallel.OrderedEvaluationExecutor(2)
    service, request = setup_problem()
    executor.configure(synthesis._proxy(synthesis.freeze_film_synthesis_request(service, request)))
    try:
        executor._ensure_pool()
        for child in executor.children:
            child.job.close()
            child.process.wait(timeout=3)
        # At this point the successful initialization writes have completed and
        # each writer is idle, while close can no longer send to a live process.
    finally:
        executor.close()
    assert executor.diagnostics()["cleanup_complete"]
    assert all(child.writer is not None and not child.writer.is_alive() for child in executor.children)
    print("idle writer fault cleanup", executor.diagnostics())


def test_owned_workers_bind_source_with_safe_path_and_foreign_pythonpath(monkeypatch, tmp_path):
    foreign = tmp_path / "coat_sim"
    foreign.mkdir()
    (foreign / "__init__.py").write_text(
        "raise RuntimeError('foreign checkout must not be imported')\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    service, request = setup_problem()
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    executor = parallel.OrderedEvaluationExecutor(2)
    executor.configure(synthesis._proxy(frozen))
    try:
        with executor.batch([parallel.EvaluationTask(frozen.environment)] * 2,
                            count=2, work=200000, budget_allows=True) as values:
            for _, outcome in values:
                executor.take(outcome)
    finally:
        executor.close()
    performance = executor.diagnostics()
    assert performance["effective_workers"] == 2
    assert performance["completed_tasks"] == 2 and performance["unknown_tasks"] == 0
    assert performance["serial_fallback_tasks"] == 0
    assert performance["cleanup_complete"] and performance["active_worker_pids"] == []
    assert all(not row["qt_imported"] for row in performance["workers"])
