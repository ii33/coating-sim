"""Bounded, deferred, vectorized differential evolution for film batches.

This module is an independent entry point.  It keeps candidate generation and
archive updates on the caller thread while sending bounded physical-thickness
chunks to a batch evaluator.  The seeded initial population matches the
construction used by :mod:`film_optimization_solvers`; deferred DE does not
promise the same trajectory as that module's immediate-update solver.
"""

from __future__ import annotations

from concurrent.futures import CancelledError
from copy import deepcopy
from dataclasses import dataclass
import math
from threading import Event
from types import MappingProxyType
from typing import Any, Callable, Sequence

import numpy as np


MODE = "deferred-batch-v1"


@dataclass(frozen=True)
class BatchEvaluation:
    """One atomic evaluated candidate: lexicographic key, DE surrogate, payload."""

    key: tuple[float, float]
    surrogate: float
    value: Any


@dataclass(frozen=True)
class BatchDEProgress:
    """Immutable-by-convention snapshot passed to ``checkpoint_callback``."""

    best: BatchEvaluation
    best_thickness: np.ndarray
    evaluations: int
    failed: int
    cancelled_evaluations: int
    generations: int
    state: str
    reason: str
    mode: str
    seed: int
    remaining_evaluations: int


@dataclass(frozen=True)
class BatchDEOutcome:
    """Best complete result and accounting for a deferred batch-DE run."""

    best: BatchEvaluation
    best_thickness: np.ndarray
    evaluations: int
    failed: int
    cancelled_evaluations: int
    generations: int
    state: str
    reason: str
    mode: str
    seed: int

    @property
    def best_evaluation(self) -> BatchEvaluation:
        """Alias that makes the atomic nature of ``best`` explicit."""
        return self.best

    @property
    def failed_evaluations(self) -> int:
        """Failed candidate slots charged to the evaluation budget."""
        return self.failed


class BatchBaselineError(RuntimeError):
    """Raised when the mandatory initial/baseline candidate cannot be scored."""

    def __init__(self, message: str, *, evaluations: int = 1, failed: int = 1):
        self.evaluations = int(evaluations)
        self.failed = int(failed)
        super().__init__(message)


class _BatchStop(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _integer(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{label} must be an integer >= {minimum}")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be an integer >= {minimum}") from exc
    try:
        exact = float(value) == number
    except (TypeError, ValueError, OverflowError):
        exact = False
    if not exact or number < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return number


def _vectors(initial: Sequence[float], lower: Sequence[float], upper: Sequence[float]):
    x0 = np.asarray(initial, dtype=float)
    lo = np.asarray(lower, dtype=float)
    hi = np.asarray(upper, dtype=float)
    if x0.ndim != 1 or lo.ndim != 1 or hi.ndim != 1:
        raise ValueError("initial, lower, and upper must be one-dimensional")
    if x0.size == 0 or lo.shape != x0.shape or hi.shape != x0.shape:
        raise ValueError("initial, lower, and upper must have the same nonzero size")
    if not (np.all(np.isfinite(x0)) and np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))):
        raise ValueError("initial values and bounds must be finite")
    if np.any(lo > hi):
        raise ValueError("lower bounds must not exceed upper bounds")
    return np.clip(x0, lo, hi), lo, hi


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{label} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _validate_evaluation(item: Any) -> BatchEvaluation:
    if not isinstance(item, BatchEvaluation):
        raise ValueError("each batch result must be a BatchEvaluation")
    if not isinstance(item.key, tuple) or len(item.key) != 2:
        raise ValueError("BatchEvaluation.key must be a pair of finite numbers")
    key = (
        _finite_number(item.key[0], "BatchEvaluation.key[0]"),
        _finite_number(item.key[1], "BatchEvaluation.key[1]"),
    )
    surrogate = _finite_number(item.surrogate, "BatchEvaluation.surrogate")
    # Detach the complete atomic result from evaluator-owned mutable payloads.
    return BatchEvaluation(key=key, surrogate=surrogate, value=deepcopy(item.value))


def _copy_evaluation(item: BatchEvaluation) -> BatchEvaluation:
    """Give consumers independent ownership of a mutable result payload."""
    return BatchEvaluation(item.key, item.surrogate, deepcopy(item.value))


def run_batch_de(
    evaluate_batch: Callable[[np.ndarray], Sequence[BatchEvaluation]],
    initial: Sequence[float],
    lower: Sequence[float],
    upper: Sequence[float],
    *,
    max_iterations: int,
    max_evaluations: int,
    seed: int = 0,
    population_size: int = 12,
    max_batch_size: int = 16,
    cancel_event: Event | None = None,
    checkpoint_callback: Callable[[BatchDEProgress], None] | None = None,
    accepted_callback: Callable[[Sequence[BatchEvaluation], np.ndarray, int, int], None] | None = None,
) -> BatchDEOutcome:
    """Run bounded differential evolution with deferred, vectorized updates.

    ``evaluate_batch`` receives a copied ``float64`` array with shape ``(C,V)``
    in physical nanometers and must return exactly C complete evaluations.
    The initial point is evaluated first as the mandatory baseline.  The
    baseline's duplicate in SciPy's initial population is served from that
    complete result without another model evaluation.

    Budget is reserved for each submitted chunk before calling the evaluator;
    a started call is charged for its full C slots even if it raises or returns
    incomplete/invalid data.  An unsuccessful non-baseline batch stops the run
    and leaves the last complete best intact.  If the baseline fails, this
    function raises :class:`BatchBaselineError` because no valid outcome exists.
    Interrupted/Cancelled evaluator calls are counted in
    ``cancelled_evaluations`` rather than ``failed``.  Mutable payloads are
    detached from evaluator ownership and copied again for checkpoints/results.
    """
    x0, lo, hi = _vectors(initial, lower, upper)
    max_iterations = _integer(max_iterations, "max_iterations", minimum=0)
    max_evaluations = _integer(max_evaluations, "max_evaluations", minimum=1)
    seed = _integer(seed, "seed", minimum=0)
    if seed > 2**32 - 1:
        raise ValueError("seed must be between 0 and 2**32 - 1")
    population_size = _integer(population_size, "population_size", minimum=1)
    max_batch_size = _integer(max_batch_size, "max_batch_size", minimum=1)
    if max_batch_size > 64:
        raise ValueError("max_batch_size must be between 1 and 64")
    if not callable(evaluate_batch):
        raise TypeError("evaluate_batch must be callable")
    if checkpoint_callback is not None and not callable(checkpoint_callback):
        raise TypeError("checkpoint_callback must be callable or None")
    if accepted_callback is not None and not callable(accepted_callback):
        raise TypeError("accepted_callback must be callable or None")

    evaluations = 0
    failed = 0
    cancelled_evaluations = 0
    generations = 0
    proposal_index = 0
    best: BatchEvaluation | None = None
    best_thickness: np.ndarray | None = None
    best_proposal_index = -1
    remaining = max_evaluations

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def publish(reason: str, state: str) -> None:
        if checkpoint_callback is None or best is None or best_thickness is None:
            return
        checkpoint_callback(BatchDEProgress(
            best=_copy_evaluation(best),
            best_thickness=best_thickness.copy(),
            evaluations=evaluations,
            failed=failed,
            cancelled_evaluations=cancelled_evaluations,
            generations=generations,
            state=state,
            reason=reason,
            mode=MODE,
            seed=seed,
            remaining_evaluations=remaining,
        ))

    def archive(items: Sequence[tuple[BatchEvaluation, np.ndarray, int]]) -> None:
        nonlocal best, best_thickness, best_proposal_index
        # The sequence is already in proposal order; strict comparison keeps
        # the earlier candidate when lexicographic keys tie.
        for result, thickness, index in items:
            if best is None or result.key < best.key:
                best = result
                best_thickness = thickness.copy()
                best_proposal_index = index
            elif result.key == best.key and index < best_proposal_index:
                best = result
                best_thickness = thickness.copy()
                best_proposal_index = index

    def start_batch(points: np.ndarray, *, baseline: bool = False) -> list[BatchEvaluation]:
        """Reserve and run exactly one all-or-nothing evaluator call."""
        nonlocal evaluations, failed, remaining, cancelled_evaluations
        count = int(points.shape[0])
        if count == 0:
            return []
        if not baseline and cancelled():
            raise _BatchStop("cancelled")
        if count > remaining:
            raise _BatchStop("evaluation_budget")
        # Reservation happens before the call.  If cancellation wins before
        # invocation, refund the unstarted reservation.
        remaining -= count
        if not baseline and cancelled():
            remaining += count
            raise _BatchStop("cancelled")
        evaluations += count
        try:
            raw = evaluate_batch(np.array(points, dtype=float, copy=True))
            values = tuple(raw)
            if len(values) != count:
                raise ValueError(
                    f"evaluate_batch returned {len(values)} results for {count} candidates"
                )
            validated = tuple(_validate_evaluation(item) for item in values)
        except (InterruptedError, CancelledError) as exc:
            if baseline:
                failed += count
                raise BatchBaselineError(
                    f"baseline batch cancelled before a complete result: {exc}",
                    evaluations=evaluations,
                    failed=failed,
                ) from exc
            cancelled_evaluations += count
            raise _BatchStop("cancelled") from exc
        except Exception as exc:
            # A begun, unidentifiable batch is charged and failed atomically.
            failed += count
            if baseline:
                raise BatchBaselineError(
                    f"baseline batch failed: {exc}",
                    evaluations=evaluations,
                    failed=failed,
                ) from exc
            raise _BatchStop("batch_evaluation_failed") from exc
        return list(validated)

    # The baseline always runs, including when cancellation was already set.
    baseline_result = start_batch(x0.reshape(1, -1), baseline=True)[0]
    archive([(baseline_result, x0, proposal_index)])
    proposal_index += 1
    publish("baseline_complete", "running")

    outcome_reason: str | None = None
    outcome_state = "completed"
    if cancelled():
        outcome_reason = "cancelled"
        outcome_state = "cancelled"
    elif max_iterations == 0:
        outcome_reason = "iteration_limit"
    elif max_evaluations <= evaluations:
        outcome_reason = "evaluation_budget"
        outcome_state = "stopped"
    else:
        # Match the legacy solver's explicit seed and population construction.
        actual_population_size = max(5, population_size)
        rng = np.random.default_rng(seed)
        population = lo + rng.random((actual_population_size, x0.size)) * (hi - lo)
        population[0] = x0

        def objective(columns: np.ndarray) -> np.ndarray:
            nonlocal proposal_index, cancelled_evaluations
            candidates = np.asarray(columns, dtype=float)
            if candidates.ndim != 2 or candidates.shape[0] != x0.size:
                raise ValueError("SciPy vectorized candidates must have shape (V,C)")
            if cancelled():
                raise _BatchStop("cancelled")
            candidates = np.clip(candidates.T, lo, hi)
            count = int(candidates.shape[0])
            scores = np.empty(count, dtype=float)
            pending_points: list[np.ndarray] = []
            pending_positions: list[int] = []
            pending_indexes: list[int] = []
            for position, point in enumerate(candidates):
                current_index = proposal_index
                proposal_index += 1
                if np.array_equal(point, x0):
                    scores[position] = baseline_result.surrogate
                else:
                    pending_points.append(point.copy())
                    pending_positions.append(position)
                    pending_indexes.append(current_index)

            cursor = 0
            while cursor < len(pending_points):
                if cancelled():
                    raise _BatchStop("cancelled")
                available = remaining
                if available <= 0:
                    raise _BatchStop("evaluation_budget")
                chunk_count = min(max_batch_size, available, len(pending_points) - cursor)
                chunk_points = np.stack(pending_points[cursor:cursor + chunk_count])
                chunk_positions = pending_positions[cursor:cursor + chunk_count]
                chunk_indexes = pending_indexes[cursor:cursor + chunk_count]
                batch = start_batch(chunk_points)
                # Cancellation while evaluate_batch was running discards this
                # entire batch from the archive; its started slots stay charged.
                if cancelled():
                    cancelled_evaluations += len(batch)
                    raise _BatchStop("cancelled")
                archive([
                    (result, point, index)
                    for result, point, index in zip(batch, chunk_points, chunk_indexes)
                ])
                # Internal display adapter observes only the atomically accepted
                # chunk, never a partial/failed/cancel-discarded evaluation.
                if accepted_callback is not None:
                    try:
                        observations = []
                        for item in batch:
                            metrics = (item.value.get("display_metrics")
                                       if isinstance(item.value, dict) else None)
                            if not (type(metrics) is tuple and len(metrics) == 4 and
                                    all(type(value) in (float, int, bool) for value in metrics)):
                                metrics = None
                            observations.append(BatchEvaluation(item.key, item.surrogate,
                                MappingProxyType({"display_metrics": metrics})))
                        # Only small coordinate/scalar observations are detached;
                        # never duplicate raw spectral or Jacobian payloads.
                        coordinates = np.frombuffer(chunk_points.tobytes(), dtype=float).reshape(chunk_points.shape)
                        accepted_callback(tuple(observations), coordinates, evaluations, generations)
                    except Exception:
                        pass  # Display observation cannot affect DE state.
                for position, result in zip(chunk_positions, batch):
                    scores[position] = result.surrogate
                cursor += chunk_count
                publish("batch_complete", "running")
            return scores

        def callback(_xk: np.ndarray, _convergence: float) -> bool:
            nonlocal generations
            generations += 1
            if cancelled():
                raise _BatchStop("cancelled")
            publish("generation_complete", "running")
            if cancelled():
                raise _BatchStop("cancelled")
            return False

        from scipy.optimize import differential_evolution

        try:
            result = differential_evolution(
                objective,
                bounds=list(zip(lo, hi)),
                strategy="best1bin",
                maxiter=max_iterations,
                popsize=actual_population_size,
                tol=1e-6,
                atol=0.0,
                mutation=(0.5, 1.0),
                recombination=0.7,
                seed=seed,
                callback=callback,
                init=population,
                polish=False,
                updating="deferred",
                workers=1,
                vectorized=True,
            )
            if outcome_reason is None:
                if cancelled():
                    outcome_reason = "cancelled"
                    outcome_state = "cancelled"
                elif bool(result.success):
                    outcome_reason = "converged"
                elif generations >= max_iterations:
                    outcome_reason = "iteration_limit"
                elif evaluations >= max_evaluations:
                    outcome_reason = "evaluation_budget"
                    outcome_state = "stopped"
                else:
                    outcome_reason = "solver_stopped"
        except _BatchStop as stop:
            outcome_reason = stop.reason
            outcome_state = "cancelled" if stop.reason == "cancelled" else "stopped"

    assert best is not None and best_thickness is not None
    if outcome_reason is None:
        outcome_reason = "solver_stopped"
    if outcome_reason in {"cancelled", "evaluation_budget", "batch_evaluation_failed"}:
        publish(outcome_reason, outcome_state)
    return BatchDEOutcome(
        best=_copy_evaluation(best),
        best_thickness=best_thickness.copy(),
        evaluations=evaluations,
        failed=failed,
        cancelled_evaluations=cancelled_evaluations,
        generations=generations,
        state=outcome_state,
        reason=outcome_reason,
        mode=MODE,
        seed=seed,
    )


__all__ = [
    "BatchBaselineError",
    "BatchDEOutcome",
    "BatchDEProgress",
    "BatchEvaluation",
    "MODE",
    "run_batch_de",
]
