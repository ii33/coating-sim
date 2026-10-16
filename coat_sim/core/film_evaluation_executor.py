"""Qt-independent bounded execution for frozen film candidates.

This module owns scheduling mechanics only. It never scores candidates, picks a
best candidate, mutates an archive, or changes a caller's counters. A caller
must reserve its own budget here (or pass a :class:`BudgetLedger`) before work
is submitted, then consume the ordered result tuple on its owning thread.

The callable executor always runs Python callbacks on CPU threads. ``backend``
is a policy hint for integration code: requests for ``gpu`` are reported as a
CPU fallback here, because GPU work belongs in the caller's vectorized numeric
backend. The executor does not change BLAS environment variables or claim to
limit a generic callback's BLAS threads.
"""

from __future__ import annotations

import copy
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import CancelledError, FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class CandidateStatus(str, Enum):
    """Terminal state of one candidate evaluation."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CandidateCancelled(Exception):
    """Raised by a cooperative evaluator when an already-started call stops."""


class ResultMemoryLimitError(ValueError):
    """Raised before any reservations when a batch cannot fit its result cap."""


class UnsafeConcurrencyError(ValueError):
    """Raised when multiple generic workers were requested without opt-in."""


def _freeze_payload(value: Any) -> Any:
    """Copy caller data and freeze common containers and NumPy arrays."""
    if isinstance(value, Mapping):
        return MappingProxyType({copy.deepcopy(key): _freeze_payload(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_payload(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_payload(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)

    # NumPy arrays are reconstructed over immutable bytes, so callers cannot
    # re-enable writes with setflags(write=True). Object arrays contain Python
    # references and cannot safely use this byte-backed strategy.
    dtype = getattr(value, "dtype", None)
    if dtype is not None and hasattr(dtype, "hasobject") and hasattr(value, "tobytes"):
        if dtype.hasobject:
            raise TypeError("NumPy object arrays are not supported in frozen candidate payloads")
        import numpy as np

        immutable_bytes = value.tobytes(order="C")
        return np.frombuffer(immutable_bytes, dtype=dtype).reshape(value.shape)
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class CandidateInput:
    """Immutable candidate envelope with an isolated payload snapshot.

    Built-in mappings/sequences/sets and NumPy arrays are recursively frozen.
    Other custom objects are deep-copied; callers should pass immutable custom
    payload types if their evaluators must not mutate them.
    """

    candidate_id: str
    operation_generation: int
    payload: Any

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id:
            raise ValueError("candidate_id must be a non-empty string")
        if isinstance(self.operation_generation, bool) or not isinstance(self.operation_generation, int):
            raise TypeError("operation_generation must be an integer")
        object.__setattr__(self, "payload", _freeze_payload(self.payload))


@dataclass(frozen=True, slots=True)
class ExecutionOptions:
    """Validated CPU scheduling limits and the requested backend policy.

    ``blas_safe=True`` is an explicit caller assertion that concurrent calls
    will not oversubscribe BLAS (for example, the callback uses no BLAS, or the
    caller already configured a per-call limit). This module does not mutate
    global thread settings. GPU is a policy hint handled by a separate
    vectorized backend; this callable executor still reports CPU execution.
    """

    backend: str = "auto"
    cpu_workers: int = 1
    max_in_flight: int | None = None
    chunk_bytes: int = 8 * 1024 * 1024
    max_result_bytes: int | None = 64 * 1024 * 1024
    blas_safe: bool = False

    def __post_init__(self) -> None:
        if self.backend not in {"auto", "cpu", "gpu"}:
            raise ValueError("backend must be 'auto', 'cpu', or 'gpu'")
        if isinstance(self.cpu_workers, bool) or not isinstance(self.cpu_workers, int):
            raise TypeError("cpu_workers must be an integer")
        if not 1 <= self.cpu_workers <= 4:
            raise ValueError("cpu_workers must be between 1 and 4")
        if self.max_in_flight is None:
            object.__setattr__(self, "max_in_flight", self.cpu_workers)
        if isinstance(self.max_in_flight, bool) or not isinstance(self.max_in_flight, int):
            raise TypeError("max_in_flight must be an integer")
        if not 1 <= self.max_in_flight <= self.cpu_workers:
            raise ValueError("max_in_flight must be between 1 and cpu_workers")
        if isinstance(self.chunk_bytes, bool) or not isinstance(self.chunk_bytes, int):
            raise TypeError("chunk_bytes must be an integer")
        if self.chunk_bytes <= 0:
            raise ValueError("chunk_bytes must be positive")
        if self.max_result_bytes is not None:
            if isinstance(self.max_result_bytes, bool) or not isinstance(self.max_result_bytes, int):
                raise TypeError("max_result_bytes must be an integer or None")
            if self.max_result_bytes <= 0:
                raise ValueError("max_result_bytes must be positive or None")
        if not isinstance(self.blas_safe, bool):
            raise TypeError("blas_safe must be a bool")


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    max_evaluations: int
    started: int
    reserved: int

    @property
    def remaining(self) -> int:
        return max(0, self.max_evaluations - self.started - self.reserved)


@dataclass(frozen=True, slots=True)
class _BudgetReservation:
    owner: object = field(repr=False)
    token_id: int


class BudgetLedger:
    """Thread-safe local token ledger; started work is never refunded."""

    def __init__(self, max_evaluations: int) -> None:
        if isinstance(max_evaluations, bool) or not isinstance(max_evaluations, int):
            raise TypeError("max_evaluations must be an integer")
        if max_evaluations < 0:
            raise ValueError("max_evaluations must be non-negative")
        self._max = max_evaluations
        self._started = 0
        self._next_token = 1
        self._owner = object()
        self._reservations: set[int] = set()
        self._lock = threading.Lock()

    def reserve(self) -> _BudgetReservation | None:
        """Atomically reserve one future evaluation, or return ``None``."""
        with self._lock:
            if self._started + len(self._reservations) >= self._max:
                return None
            token_id = self._next_token
            self._next_token += 1
            self._reservations.add(token_id)
            return _BudgetReservation(self._owner, token_id)

    def commit(self, reservation: _BudgetReservation) -> None:
        """Count a reservation at the worker's start boundary."""
        with self._lock:
            token_id = self._validate_reservation(reservation)
            self._reservations.remove(token_id)
            self._started += 1

    def release(self, reservation: _BudgetReservation) -> bool:
        """Release work that did not start. Return False if already closed."""
        with self._lock:
            if reservation.owner is not self._owner or reservation.token_id not in self._reservations:
                return False
            self._reservations.remove(reservation.token_id)
            return True

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return BudgetSnapshot(self._max, self._started, len(self._reservations))

    def _validate_reservation(self, reservation: _BudgetReservation) -> int:
        if reservation.owner is not self._owner or reservation.token_id not in self._reservations:
            raise ValueError("reservation is not active in this ledger")
        return reservation.token_id


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """Final result for one input, in the same positional slot as that input.

    ``is_current`` includes generation and cancellation eligibility at result
    completion. Only a completed, current result is eligible for archival.
    """

    candidate: CandidateInput
    status: CandidateStatus
    value: Any = None
    started_at: float | None = None
    completed_at: float | None = None
    duration_seconds: float = 0.0
    error: str | None = None
    is_current: bool = True
    cancelled_after_compute: bool = False
    estimated_result_bytes: int = 0

    @property
    def started(self) -> bool:
        return self.started_at is not None

    @property
    def archive_eligible(self) -> bool:
        return self.status is CandidateStatus.COMPLETED and self.is_current


@dataclass(frozen=True, slots=True)
class ExecutionBatchResult:
    """Ordered batch output and resolved executor metadata."""

    results: tuple[CandidateResult, ...]
    requested_backend: str
    backend: str
    fallback_reason: str | None
    budget: BudgetSnapshot | None


Evaluator = Callable[[CandidateInput, threading.Event], Any]
ResultSizeEstimator = Callable[[CandidateInput], int]
GenerationSource = int | Callable[[], int]


def _payload_size(value: Any, seen: set[int] | None = None) -> int:
    """Estimate retained Python/native-buffer bytes for a payload snapshot."""
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    nbytes = getattr(value, "nbytes", None)
    if isinstance(nbytes, int) and nbytes >= 0:
        return nbytes
    size = sys.getsizeof(value, 0)
    if isinstance(value, Mapping):
        size += sum(_payload_size(key, seen) + _payload_size(item, seen) for key, item in value.items())
    elif isinstance(value, (tuple, list, set, frozenset)):
        size += sum(_payload_size(item, seen) for item in value)
    elif hasattr(value, "__dict__"):
        size += _payload_size(vars(value), seen)
    return size


def _generation_value(source: GenerationSource | None, default: int) -> int:
    value = default if source is None else source() if callable(source) else source
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("current_generation must resolve to an integer")
    return value


def _error_text(error: BaseException) -> str:
    detail = str(error)
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


def execute_batch(
    candidates: Iterable[CandidateInput],
    evaluator: Evaluator,
    *,
    options: ExecutionOptions | None = None,
    budget: BudgetLedger | None = None,
    cancel_event: threading.Event | None = None,
    current_generation: GenerationSource | None = None,
    estimate_result_bytes: ResultSizeEstimator | None = None,
) -> ExecutionBatchResult:
    """Evaluate candidates with bounded submission and deterministic ordering.

    The evaluator signature is ``evaluator(candidate, cancel_event)``. It must
    be a pure candidate calculation: it must not mutate shared counters,
    archives, UI/session state, or its frozen input. It may poll the event or
    raise :class:`CandidateCancelled` to stop cooperative work that already
    started.

    If ``max_result_bytes`` is set, provide ``estimate_result_bytes(candidate)``
    that returns an upper bound for that candidate's retained result value.
    The entire batch is preflighted against that cap before any budget token is
    reserved or work is submitted. The cap applies to values, not candidate
    input snapshots or small result metadata. ``chunk_bytes`` bounds the
    approximate payload bytes in a submission window; opaque custom payloads
    can only be counted by their shallow Python size.

    Cancellation stops later submissions. Each worker checks cancellation and
    generation again immediately before committing its token and entering the
    evaluator. Started calls are awaited at the batch boundary; their tokens
    remain charged even when they fail or cooperatively cancel. Stale results
    are returned with ``is_current=False`` so the owning loop can reject them;
    cancellation also makes completed results ineligible for archival. This
    executor has no archive write path.
    """
    execution_options = options or ExecutionOptions()
    if execution_options.cpu_workers > 1 and not execution_options.blas_safe:
        raise UnsafeConcurrencyError(
            "cpu_workers > 1 requires blas_safe=True; the caller must ensure BLAS does not oversubscribe"
        )
    if not callable(evaluator):
        raise TypeError("evaluator must be callable")

    inputs = tuple(candidates)
    if not all(isinstance(candidate, CandidateInput) for candidate in inputs):
        raise TypeError("candidates must contain only CandidateInput values")
    estimates: list[int] = [0] * len(inputs)
    if inputs and execution_options.max_result_bytes is not None:
        if estimate_result_bytes is None:
            raise ValueError("estimate_result_bytes is required when max_result_bytes is set")
        total_estimated = 0
        for index, candidate in enumerate(inputs):
            estimate = estimate_result_bytes(candidate)
            if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
                raise ValueError("result size estimates must be non-negative integers")
            estimates[index] = estimate
            total_estimated += estimate
        if total_estimated > execution_options.max_result_bytes:
            raise ResultMemoryLimitError(
                f"estimated result values require {total_estimated} bytes, "
                f"exceeding max_result_bytes={execution_options.max_result_bytes}"
            )

    event = cancel_event if cancel_event is not None else threading.Event()
    if not hasattr(event, "is_set"):
        raise TypeError("cancel_event must provide is_set()")
    resolved_generation = current_generation
    results: list[CandidateResult | None] = [None] * len(inputs)
    payload_sizes = [_payload_size(candidate.payload) for candidate in inputs]

    def current(candidate: CandidateInput) -> bool:
        return _generation_value(resolved_generation, candidate.operation_generation) == candidate.operation_generation

    def skipped(candidate: CandidateInput, reason: str, estimated_bytes: int) -> CandidateResult:
        now = time.perf_counter()
        return CandidateResult(
            candidate=candidate,
            status=CandidateStatus.CANCELLED,
            completed_at=now,
            error=reason,
            is_current=False,
            estimated_result_bytes=estimated_bytes,
        )

    def run_one(
        candidate: CandidateInput,
        reservation: _BudgetReservation | None,
        estimated_bytes: int,
    ) -> CandidateResult:
        # This is the queue cancellation/staleness boundary. A token is only
        # committed after both checks, immediately before evaluator entry.
        started_at: float | None = None
        try:
            if event.is_set():
                if budget is not None and reservation is not None:
                    budget.release(reservation)
                return skipped(candidate, "cancelled before start", estimated_bytes)
            if not current(candidate):
                if budget is not None and reservation is not None:
                    budget.release(reservation)
                return skipped(candidate, "stale operation generation before start", estimated_bytes)
            if budget is not None and reservation is not None:
                budget.commit(reservation)
            started_at = time.perf_counter()
            try:
                value = evaluator(candidate, event)
            except (CandidateCancelled, CancelledError) as error:
                completed_at = time.perf_counter()
                return CandidateResult(
                    candidate=candidate,
                    status=CandidateStatus.CANCELLED,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_seconds=max(0.0, completed_at - started_at),
                    error=_error_text(error),
                    is_current=False,
                    estimated_result_bytes=estimated_bytes,
                )
            completed_at = time.perf_counter()
            cancelled_after_compute = event.is_set()
            return CandidateResult(
                candidate=candidate,
                status=CandidateStatus.COMPLETED,
                value=value,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=max(0.0, completed_at - started_at),
                is_current=current(candidate) and not cancelled_after_compute,
                cancelled_after_compute=cancelled_after_compute,
                estimated_result_bytes=estimated_bytes,
            )
        except Exception as error:
            completed_at = time.perf_counter()
            # If a pre-start guard fails, the reservation remains refundable;
            # after commit, release() safely returns False and work stays billed.
            if budget is not None and reservation is not None:
                budget.release(reservation)
            return CandidateResult(
                candidate=candidate,
                status=CandidateStatus.FAILED,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=max(0.0, completed_at - started_at) if started_at is not None else 0.0,
                error=_error_text(error),
                is_current=False,
                estimated_result_bytes=estimated_bytes,
            )

    next_index = 0
    pending: dict[Future[CandidateResult], tuple[int, int]] = {}
    in_flight_payload_bytes = 0
    budget_exhausted = False
    with ThreadPoolExecutor(
        max_workers=execution_options.cpu_workers,
        thread_name_prefix="film-eval",
    ) as pool:
        while next_index < len(inputs) or pending:
            while (
                next_index < len(inputs)
                and len(pending) < execution_options.max_in_flight  # type: ignore[operator]
                and not event.is_set()
            ):
                candidate = inputs[next_index]
                result_bytes = estimates[next_index]
                if not current(candidate):
                    results[next_index] = skipped(candidate, "stale operation generation before submit", result_bytes)
                    next_index += 1
                    continue
                candidate_bytes = payload_sizes[next_index]
                if pending and in_flight_payload_bytes + candidate_bytes > execution_options.chunk_bytes:
                    break
                reservation = budget.reserve() if budget is not None else None
                if budget is not None and reservation is None:
                    budget_exhausted = True
                    break
                try:
                    future = pool.submit(run_one, candidate, reservation, result_bytes)
                except Exception as error:
                    if budget is not None and reservation is not None:
                        budget.release(reservation)
                    now = time.perf_counter()
                    results[next_index] = CandidateResult(
                        candidate=candidate,
                        status=CandidateStatus.FAILED,
                        completed_at=now,
                        error=_error_text(error),
                        is_current=current(candidate),
                        estimated_result_bytes=result_bytes,
                    )
                    next_index += 1
                    continue
                pending[future] = (next_index, candidate_bytes)
                in_flight_payload_bytes += candidate_bytes
                next_index += 1

            # Cancellation and exhausted budgets stop only not-yet-submitted
            # work. Pending tasks still cross their pre-start gate and release
            # reservations if they never enter the evaluator.
            if event.is_set() or budget_exhausted:
                reason = "cancelled before submit" if event.is_set() else "evaluation budget exhausted before start"
                while next_index < len(inputs):
                    candidate = inputs[next_index]
                    results[next_index] = skipped(candidate, reason, estimates[next_index])
                    next_index += 1

            if pending:
                completed, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in completed:
                    index, candidate_bytes = pending.pop(future)
                    in_flight_payload_bytes -= candidate_bytes
                    try:
                        results[index] = future.result()
                    except Exception as error:
                        candidate = inputs[index]
                        now = time.perf_counter()
                        results[index] = CandidateResult(
                            candidate=candidate,
                            status=CandidateStatus.FAILED,
                            completed_at=now,
                            error=_error_text(error),
                            is_current=False,
                            estimated_result_bytes=estimates[index],
                        )
            elif next_index >= len(inputs):
                break
            elif not event.is_set() and not budget_exhausted:
                # A single payload may exceed chunk_bytes; the loop permits
                # that one candidate alone, so this is only a defensive guard.
                raise RuntimeError("candidate submission window made no progress")

    finalized = tuple(
        result if result is not None else skipped(inputs[index], "cancelled before submit", estimates[index])
        for index, result in enumerate(results)
    )
    fallback_reason: str | None = None
    if execution_options.backend == "auto":
        fallback_reason = "generic candidate callback resolved to the CPU thread executor"
    elif execution_options.backend == "gpu":
        fallback_reason = "generic candidate callback ran on CPU; GPU evaluation belongs to the vectorized backend"
    return ExecutionBatchResult(
        results=finalized,
        requested_backend=execution_options.backend,
        backend="cpu",
        fallback_reason=fallback_reason,
        budget=budget.snapshot() if budget is not None else None,
    )


__all__ = [
    "BudgetLedger",
    "BudgetSnapshot",
    "CandidateCancelled",
    "CandidateInput",
    "CandidateResult",
    "CandidateStatus",
    "ExecutionBatchResult",
    "ExecutionOptions",
    "ResultMemoryLimitError",
    "UnsafeConcurrencyError",
    "execute_batch",
]
