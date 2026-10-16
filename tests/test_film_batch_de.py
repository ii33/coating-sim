from __future__ import annotations

import threading
import unittest
from concurrent.futures import CancelledError

import numpy as np

from coat_sim.core.film_batch_de import (
    BatchBaselineError,
    BatchEvaluation,
    MODE,
    run_batch_de,
)


INITIAL = np.array([4.0, 6.0])
LOWER = np.array([0.0, 0.0])
UPPER = np.array([10.0, 10.0])


def _result(point: np.ndarray, *, payload=None) -> BatchEvaluation:
    surrogate = float(np.sum((point - np.array([2.5, 7.5])) ** 2))
    return BatchEvaluation((surrogate, surrogate), surrogate, payload)


class BatchDifferentialEvolutionTests(unittest.TestCase):
    def _run_trace(self):
        trace: list[np.ndarray] = []

        def evaluate(points):
            trace.extend(point.copy() for point in points)
            return [_result(point) for point in points]

        outcome = run_batch_de(
            evaluate,
            INITIAL,
            LOWER,
            UPPER,
            max_iterations=4,
            max_evaluations=200,
            seed=23,
            population_size=6,
            max_batch_size=5,
        )
        return trace, outcome

    def test_same_seed_reproduces_new_deferred_batch_mode(self):
        first_trace, first = self._run_trace()
        second_trace, second = self._run_trace()

        self.assertEqual(MODE, "deferred-batch-v1")
        self.assertEqual(first.mode, MODE)
        self.assertEqual(first.seed, 23)
        self.assertEqual(first.reason, second.reason)
        self.assertEqual(first.generations, second.generations)
        np.testing.assert_array_equal(np.stack(first_trace), np.stack(second_trace))
        np.testing.assert_array_equal(first.best_thickness, second.best_thickness)
        self.assertEqual(first.best.key, second.best.key)

    def test_evaluation_quota_includes_baseline_and_only_started_chunks(self):
        sizes: list[int] = []

        def evaluate(points):
            sizes.append(len(points))
            return [_result(point) for point in points]

        outcome = run_batch_de(
            evaluate,
            INITIAL,
            LOWER,
            UPPER,
            max_iterations=20,
            max_evaluations=8,
            seed=5,
            population_size=12,
            max_batch_size=3,
        )

        self.assertEqual(outcome.evaluations, 8)
        self.assertEqual(outcome.reason, "evaluation_budget")
        self.assertEqual(sizes[0], 1)  # mandatory baseline
        self.assertEqual(sum(sizes), outcome.evaluations)
        self.assertTrue(all(1 <= size <= 3 for size in sizes))
        self.assertEqual(outcome.failed, 0)

    def test_cancel_during_batch_keeps_previous_complete_best(self):
        cancel = threading.Event()
        calls = 0

        def evaluate(points):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [BatchEvaluation((5.0, 5.0), 5.0, "baseline")]
            cancel.set()
            return [BatchEvaluation((-10.0, -10.0), -10.0, "cancelled-batch") for _ in points]

        outcome = run_batch_de(
            evaluate,
            INITIAL,
            LOWER,
            UPPER,
            max_iterations=10,
            max_evaluations=30,
            seed=2,
            population_size=5,
            max_batch_size=4,
            cancel_event=cancel,
        )

        self.assertEqual(outcome.state, "cancelled")
        self.assertEqual(outcome.reason, "cancelled")
        self.assertEqual(outcome.best.value, "baseline")
        self.assertEqual(outcome.best.key, (5.0, 5.0))
        self.assertEqual(outcome.evaluations, 5)  # baseline plus the started chunk
        self.assertEqual(outcome.failed, 0)

    def test_precancel_still_evaluates_and_returns_complete_baseline(self):
        cancel = threading.Event()
        cancel.set()
        calls = []

        def evaluate(points):
            calls.append(points.copy())
            return [BatchEvaluation((1.0, 2.0), 1.0, {"complete": True})]

        outcome = run_batch_de(
            evaluate,
            INITIAL,
            LOWER,
            UPPER,
            max_iterations=2,
            max_evaluations=10,
            cancel_event=cancel,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].shape, (1, 2))
        self.assertEqual(outcome.evaluations, 1)
        self.assertEqual(outcome.state, "cancelled")
        self.assertEqual(outcome.best.value, {"complete": True})

    def test_tied_archive_key_keeps_the_first_complete_candidate(self):
        calls = 0

        def evaluate(points):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [BatchEvaluation((1.0, 1.0), 1.0, "baseline")]
            return [BatchEvaluation((1.0, 1.0), 2.0, "later-tie") for _ in points]

        outcome = run_batch_de(
            evaluate,
            INITIAL,
            LOWER,
            UPPER,
            max_iterations=2,
            max_evaluations=40,
            seed=11,
            population_size=5,
            max_batch_size=8,
        )

        self.assertEqual(outcome.best.value, "baseline")
        np.testing.assert_array_equal(outcome.best_thickness, INITIAL)

    def test_failed_batch_is_charged_and_does_not_replace_baseline(self):
        for bad_mode in ("exception", "short", "invalid"):
            with self.subTest(bad_mode=bad_mode):
                calls = 0

                def evaluate(points):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return [BatchEvaluation((4.0, 4.0), 4.0, "baseline")]
                    if bad_mode == "exception":
                        raise RuntimeError("batch failed")
                    if bad_mode == "short":
                        return []
                    return [BatchEvaluation((0.0, 0.0), float("nan"), "invalid") for _ in points]

                outcome = run_batch_de(
                    evaluate,
                    INITIAL,
                    LOWER,
                    UPPER,
                    max_iterations=2,
                    max_evaluations=30,
                    seed=7,
                    population_size=5,
                    max_batch_size=4,
                )

                self.assertEqual(outcome.reason, "batch_evaluation_failed")
                self.assertEqual(outcome.best.value, "baseline")
                self.assertEqual(outcome.failed, outcome.evaluations - 1)
                self.assertEqual(outcome.state, "stopped")

    def test_baseline_failure_raises_with_charged_counts(self):
        with self.assertRaises(BatchBaselineError) as captured:
            run_batch_de(
                lambda _points: [],
                INITIAL,
                LOWER,
                UPPER,
                max_iterations=1,
                max_evaluations=5,
            )

        self.assertEqual(captured.exception.evaluations, 1)
        self.assertEqual(captured.exception.failed, 1)

    def test_batches_are_sequential_and_never_exceed_inflight_window(self):
        sizes = []
        active = 0
        max_active = 0

        def evaluate(points):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            sizes.append(len(points))
            try:
                return [_result(point) for point in points]
            finally:
                active -= 1

        run_batch_de(
            evaluate,
            INITIAL,
            LOWER,
            UPPER,
            max_iterations=2,
            max_evaluations=100,
            seed=3,
            population_size=7,
            max_batch_size=2,
        )

        self.assertEqual(max_active, 1)
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 2)

    def test_checkpoint_carries_complete_best_and_run_metadata(self):
        snapshots = []

        outcome = run_batch_de(
            lambda points: [_result(point) for point in points],
            INITIAL,
            LOWER,
            UPPER,
            max_iterations=0,
            max_evaluations=1,
            seed=19,
            checkpoint_callback=snapshots.append,
        )

        self.assertTrue(snapshots)
        self.assertEqual(snapshots[0].mode, MODE)
        self.assertEqual(snapshots[0].seed, 19)
        self.assertEqual(snapshots[0].best, outcome.best)
        np.testing.assert_array_equal(snapshots[0].best_thickness, outcome.best_thickness)

    def test_checkpoint_mutation_cannot_change_the_returned_archive(self):
        def mutate(snapshot):
            snapshot.best.value["owner"] = "checkpoint"

        outcome = run_batch_de(
            lambda _points: [BatchEvaluation((1.0, 2.0), 1.0, {"owner": "evaluator"})],
            INITIAL,
            LOWER,
            UPPER,
            max_iterations=0,
            max_evaluations=10,
            checkpoint_callback=mutate,
        )

        self.assertEqual(outcome.best.value, {"owner": "evaluator"})

    def test_later_evaluator_mutation_cannot_change_an_archived_payload(self):
        shared_value = {"stage": "baseline"}
        calls = 0
        cancel = threading.Event()

        def evaluate(points):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [BatchEvaluation((1.0, 1.0), 1.0, shared_value)]
            shared_value["stage"] = "later batch"
            cancel.set()
            return [BatchEvaluation((2.0, 2.0), 2.0, shared_value) for _ in points]

        outcome = run_batch_de(
            evaluate,
            INITIAL,
            LOWER,
            UPPER,
            max_iterations=5,
            max_evaluations=30,
            seed=13,
            population_size=5,
            max_batch_size=4,
            cancel_event=cancel,
        )

        self.assertEqual(outcome.best.value, {"stage": "baseline"})
        self.assertEqual(shared_value, {"stage": "later batch"})

    def test_cooperative_backend_cancellation_is_not_a_numeric_failure(self):
        for exception_type in (InterruptedError, CancelledError):
            with self.subTest(exception_type=exception_type):
                cancel = threading.Event()
                calls = 0

                def evaluate(points):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return [BatchEvaluation((3.0, 3.0), 3.0, "baseline")]
                    cancel.set()
                    raise exception_type("backend cancelled")

                outcome = run_batch_de(
                    evaluate,
                    INITIAL,
                    LOWER,
                    UPPER,
                    max_iterations=3,
                    max_evaluations=20,
                    seed=9,
                    population_size=5,
                    max_batch_size=4,
                    cancel_event=cancel,
                )

                self.assertEqual(outcome.state, "cancelled")
                self.assertEqual(outcome.reason, "cancelled")
                self.assertEqual(outcome.failed, 0)
                self.assertGreater(outcome.cancelled_evaluations, 0)
                self.assertEqual(outcome.evaluations, 1 + outcome.cancelled_evaluations)

    def test_zero_iterations_runs_only_the_mandatory_baseline(self):
        calls = []

        def evaluate(points):
            calls.append(points.copy())
            return [_result(point) for point in points]

        outcome = run_batch_de(
            evaluate,
            INITIAL,
            LOWER,
            UPPER,
            max_iterations=0,
            max_evaluations=50,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(outcome.evaluations, 1)
        self.assertEqual(outcome.generations, 0)
        self.assertEqual(outcome.reason, "iteration_limit")

    def test_max_batch_size_is_bounded(self):
        with self.assertRaises(ValueError):
            run_batch_de(
                lambda _points: [],
                INITIAL,
                LOWER,
                UPPER,
                max_iterations=1,
                max_evaluations=5,
                max_batch_size=65,
            )


if __name__ == "__main__":
    unittest.main()
