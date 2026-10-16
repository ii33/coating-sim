from __future__ import annotations

import threading

import numpy as np
import pytest

from coat_sim.core.film_evaluation_executor import (
    BudgetLedger,
    CandidateCancelled,
    CandidateInput,
    CandidateStatus,
    ExecutionOptions,
    ResultMemoryLimitError,
    UnsafeConcurrencyError,
    execute_batch,
)


def _candidate(candidate_id: str, payload=None, generation: int = 7) -> CandidateInput:
    return CandidateInput(candidate_id, generation, {} if payload is None else payload)


def _options(*, workers: int = 1, **kwargs) -> ExecutionOptions:
    values = {
        "cpu_workers": workers,
        "max_in_flight": workers,
        "blas_safe": workers > 1,
        "max_result_bytes": 1024,
    }
    values.update(kwargs)
    return ExecutionOptions(**values)


def _result_size(_candidate: CandidateInput) -> int:
    return 8


def test_candidate_payload_is_copied_and_common_containers_are_frozen():
    source = {"layers": [{"thickness_nm": 120.0}], "ids": ["a"]}
    candidate = _candidate("frozen", source)
    source["layers"][0]["thickness_nm"] = 999.0
    source["ids"].append("b")

    assert candidate.payload["layers"][0]["thickness_nm"] == 120.0
    assert candidate.payload["ids"] == ("a",)
    with pytest.raises(TypeError):
        candidate.payload["new"] = 1
    with pytest.raises(TypeError):
        candidate.payload["layers"][0]["thickness_nm"] = 2.0

    source_array = np.array([1.0, 2.0])
    frozen_array = _candidate("array", {"spectrum": source_array}).payload["spectrum"]
    source_array[0] = 99.0
    assert frozen_array.tolist() == [1.0, 2.0]
    assert not frozen_array.flags.writeable
    with pytest.raises(ValueError):
        frozen_array.setflags(write=True)
    with pytest.raises(TypeError, match="object arrays"):
        _candidate("object-array", np.array([object()], dtype=object))


def test_failed_started_evaluations_are_charged_and_results_keep_input_order():
    first_started = threading.Event()
    fast_finished = threading.Event()
    release_first = threading.Event()
    ledger = BudgetLedger(max_evaluations=2)

    def evaluator(candidate, _cancel_event):
        if candidate.candidate_id == "first":
            first_started.set()
            assert release_first.wait(3)
            return "slow"
        fast_finished.set()
        raise RuntimeError("evaluation failed")

    holder = {}

    def run():
        holder["batch"] = execute_batch(
            (_candidate("first"), _candidate("second")),
            evaluator,
            options=_options(workers=2),
            budget=ledger,
            estimate_result_bytes=_result_size,
        )

    runner = threading.Thread(target=run)
    runner.start()
    assert first_started.wait(3)
    assert fast_finished.wait(3)
    release_first.set()
    runner.join(3)
    assert not runner.is_alive()

    batch = holder["batch"]
    assert [result.candidate.candidate_id for result in batch.results] == ["first", "second"]
    assert [result.status for result in batch.results] == [CandidateStatus.COMPLETED, CandidateStatus.FAILED]
    assert all(result.started and result.completed_at is not None for result in batch.results)
    assert batch.results[0].value == "slow"
    assert "evaluation failed" in batch.results[1].error
    assert batch.results[1].is_current is False
    assert batch.results[1].archive_eligible is False
    assert ledger.snapshot().started == 2
    assert ledger.snapshot().reserved == 0


def test_budget_is_reserved_before_submission_and_unstarted_candidates_are_not_charged():
    ledger = BudgetLedger(max_evaluations=1)
    called = []

    def evaluator(candidate, _cancel_event):
        called.append(candidate.candidate_id)
        return candidate.candidate_id

    batch = execute_batch(
        (_candidate("one"), _candidate("two"), _candidate("three")),
        evaluator,
        options=_options(),
        budget=ledger,
        estimate_result_bytes=_result_size,
    )

    assert called == ["one"]
    assert [item.status for item in batch.results] == [
        CandidateStatus.COMPLETED,
        CandidateStatus.CANCELLED,
        CandidateStatus.CANCELLED,
    ]
    assert "budget exhausted" in batch.results[1].error
    assert ledger.snapshot().started == 1
    assert ledger.snapshot().reserved == 0


def test_cancellation_stops_new_submissions_and_waits_for_started_work():
    event = threading.Event()
    started = threading.Event()
    finish_started = threading.Event()
    calls = []
    ledger = BudgetLedger(max_evaluations=4)

    def evaluator(candidate, cancel_event):
        calls.append(candidate.candidate_id)
        started.set()
        assert finish_started.wait(3)
        assert cancel_event.is_set()
        return "finished at boundary"

    holder = {}

    def run():
        holder["batch"] = execute_batch(
            tuple(_candidate(str(index)) for index in range(4)),
            evaluator,
            options=_options(),
            budget=ledger,
            cancel_event=event,
            estimate_result_bytes=_result_size,
        )

    runner = threading.Thread(target=run)
    runner.start()
    assert started.wait(3)
    event.set()
    finish_started.set()
    runner.join(3)
    assert not runner.is_alive()

    results = holder["batch"].results
    assert results[0].status is CandidateStatus.COMPLETED
    assert all(result.status is CandidateStatus.CANCELLED for result in results[1:])
    assert calls == ["0"]
    assert ledger.snapshot().started == 1
    assert ledger.snapshot().reserved == 0


def test_result_memory_limit_is_preflighted_before_budget_reservations():
    ledger = BudgetLedger(max_evaluations=1)
    called = []

    with pytest.raises(ValueError, match="estimate_result_bytes is required"):
        execute_batch(
            (_candidate("one"),),
            lambda candidate, _event: called.append(candidate.candidate_id),
            options=_options(),
            budget=ledger,
        )

    with pytest.raises(ResultMemoryLimitError, match="exceeding max_result_bytes"):
        execute_batch(
            (_candidate("one"), _candidate("two")),
            lambda candidate, _event: called.append(candidate.candidate_id),
            options=_options(max_result_bytes=10),
            budget=ledger,
            estimate_result_bytes=lambda _candidate: 6,
        )

    assert called == []
    assert ledger.snapshot().started == 0
    assert ledger.snapshot().reserved == 0


def test_stale_generation_at_start_is_skipped_and_midflight_result_is_marked_stale():
    ledger = BudgetLedger(max_evaluations=2)
    assert_stale_before_start = execute_batch(
        (_candidate("old"),),
        lambda *_: pytest.fail("stale queued candidate must not start"),
        options=_options(),
        budget=ledger,
        current_generation=8,
        estimate_result_bytes=_result_size,
    )
    assert assert_stale_before_start.results[0].status is CandidateStatus.CANCELLED
    assert assert_stale_before_start.results[0].is_current is False
    assert ledger.snapshot().started == 0
    assert ledger.snapshot().reserved == 0

    generation = [7]
    started = threading.Event()
    release = threading.Event()
    archive = {"best": "verified"}
    holder = {}

    def evaluator(_candidate, _event):
        started.set()
        assert release.wait(3)
        return "computed old generation"

    runner = threading.Thread(
        target=lambda: holder.setdefault(
            "batch",
            execute_batch(
                (_candidate("active"),),
                evaluator,
                options=_options(),
                budget=ledger,
                current_generation=lambda: generation[0],
                estimate_result_bytes=_result_size,
            ),
        )
    )
    runner.start()
    assert started.wait(3)
    generation[0] = 8
    release.set()
    runner.join(3)
    assert not runner.is_alive()

    result = holder["batch"].results[0]
    assert result.status is CandidateStatus.COMPLETED
    assert result.is_current is False
    assert result.value == "computed old generation"
    assert archive == {"best": "verified"}
    assert ledger.snapshot().started == 1


def test_cancellation_after_compute_disables_archival_and_started_cancel_is_counted():
    event = threading.Event()
    ledger = BudgetLedger(max_evaluations=1)

    def returns_after_cancel(_candidate, cancel_event):
        cancel_event.set()
        return "computed before observing cancellation"

    batch = execute_batch(
        (_candidate("late"),),
        returns_after_cancel,
        options=_options(),
        budget=ledger,
        cancel_event=event,
        estimate_result_bytes=_result_size,
    )
    result = batch.results[0]
    assert result.status is CandidateStatus.COMPLETED
    assert result.cancelled_after_compute is True
    assert result.is_current is False
    assert result.archive_eligible is False
    assert ledger.snapshot().started == 1
    assert ledger.snapshot().reserved == 0

    cancelled_event = threading.Event()
    cancelled_ledger = BudgetLedger(max_evaluations=1)

    def raises_cancelled(_candidate, cancel_event):
        cancel_event.set()
        raise CandidateCancelled("cooperative stop")

    cancelled = execute_batch(
        (_candidate("cancelled"),),
        raises_cancelled,
        options=_options(),
        budget=cancelled_ledger,
        cancel_event=cancelled_event,
        estimate_result_bytes=_result_size,
    ).results[0]
    assert cancelled.status is CandidateStatus.CANCELLED
    assert cancelled.is_current is False
    assert cancelled.archive_eligible is False
    assert cancelled.started is True
    assert cancelled_ledger.snapshot().started == 1
    assert cancelled_ledger.snapshot().reserved == 0


def test_worker_and_chunk_limits_are_bounded_and_blase_safe_is_explicit():
    with pytest.raises(UnsafeConcurrencyError, match="blas_safe=True"):
        execute_batch(
            (_candidate("one"),),
            lambda candidate, _event: candidate.candidate_id,
            options=ExecutionOptions(cpu_workers=2),
            estimate_result_bytes=lambda _candidate: 1,
        )

    second_started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak_active = 0

    def evaluator(_candidate, _event):
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
            this_count = active
        if this_count <= 2:
            if this_count == 2:
                second_started.set()
            assert release.wait(3)
        with lock:
            active -= 1
        return 1

    holder = {}
    runner = threading.Thread(
        target=lambda: holder.setdefault(
            "batch",
            execute_batch(
                tuple(_candidate(str(index), {"bytes": b"0123456789"}) for index in range(4)),
                evaluator,
                options=ExecutionOptions(
                    cpu_workers=2,
                    max_in_flight=2,
                    chunk_bytes=1_000_000,
                    max_result_bytes=16,
                    blas_safe=True,
                ),
                estimate_result_bytes=lambda _candidate: 1,
            ),
        )
    )
    runner.start()
    assert second_started.wait(3)
    release.set()
    runner.join(3)
    assert not runner.is_alive()
    assert peak_active <= 2
    assert len(holder["batch"].results) == 4
    assert all(result.status is CandidateStatus.COMPLETED for result in holder["batch"].results)

    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    chunk_holder = {}

    def chunky_evaluator(candidate, _event):
        if candidate.candidate_id == "0":
            first_started.set()
            assert release_first.wait(3)
        else:
            second_started.set()
        return 1

    chunk_runner = threading.Thread(
        target=lambda: chunk_holder.setdefault(
            "batch",
            execute_batch(
                tuple(_candidate(str(index), {"bytes": b"payload-larger-than-chunk"}) for index in range(2)),
                chunky_evaluator,
                options=ExecutionOptions(
                    cpu_workers=2,
                    max_in_flight=2,
                    chunk_bytes=1,
                    max_result_bytes=16,
                    blas_safe=True,
                ),
                estimate_result_bytes=lambda _candidate: 1,
            ),
        )
    )
    chunk_runner.start()
    assert first_started.wait(3)
    assert not second_started.wait(0.1)
    release_first.set()
    chunk_runner.join(3)
    assert not chunk_runner.is_alive()
    assert second_started.is_set()


def test_gpu_policy_is_reported_as_cpu_callable_fallback():
    batch = execute_batch(
        (_candidate("one"),),
        lambda candidate, _event: candidate.candidate_id,
        options=_options(backend="gpu"),
        estimate_result_bytes=_result_size,
    )
    assert batch.requested_backend == "gpu"
    assert batch.backend == "cpu"
    assert "vectorized backend" in batch.fallback_reason

