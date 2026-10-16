# Bounded scheduler resource-failure fixtures

Updated: 2026-10-13. This is a test-harness change. No production executor,
algorithm, memory policy, remote permission, or runtime fault switch is added.

## Ownership and process boundary

`tests/test_queue_results_resources.py` reuses `tests/test_desktop_compute_queue.py`
setup. In this checkout, that setup supplies a `SchedulerHarness`: its scheduler
and public protocol transport live in the pytest process, while `Scheduler.tick()`
launches the actual worker in a new Python subprocess. There is no daemon in this
particular harness. Normal `SchedulerClient` use outside this harness can start a
separate daemon as well.

Patching `isolated_compute._compute_child` in the pytest interpreter cannot change
the module imported by that independently launched worker. Furthermore, the
scheduler worker runs the isolated calculation's `_execute` branch in its own
already-isolated process. The old queued test mistakenly combined the ineffective
parent patch with a requested 100000 by 100000 grid. That old node must not be run
unchanged: the intended injected fault did not prevent a real large allocation.

## Explicit tests-only boundary

`SchedulerHarness.resource_failure_workers(failure)` uses the existing test-owned
worker-command seam. It accepts only the fixed strings `memory` and `crash`; no
caller-supplied callable, code, import path, or production parameter selects a
fault. Before `Popen`, it reads the frozen job and requires:

- executor `process_recipe`
- integer grid size 21 and integer time steps 12
- backend `CPU` and algorithm `fast`
- the harness's own isolated root and a safe job identity

`tests.scheduler_resource_worker` validates the same frozen specification again
in the fresh child. Inside that child's interpreter only, it replaces
`algorithm_paths.run_recipe` with a fixed fault callable and calls the unchanged
scheduler `worker.run` entrypoint. The standard durable PID/lease handshake,
service preparation, outcome serialization, scheduler state transition, and GUI
observer path still run. The numerical recipe implementation does not enter.

The callable writes a small scalar JSON marker immediately before the fault. It
records the actual child PID, parent PID, job ID, 21/12 parameters, CPU/fast route,
and the pre-recipe allocation boundary. The memory fixture raises a real
`MemoryError('bounded scheduler allocation failure')`; the worker captures it as
`failed` / `algorithm_failed` and logs its traceback. The crash fixture exits the
actual subprocess with code 37; the scheduler records `failed` /
`worker_crashed` / `exit_code: 37` because no outcome envelope was published.
Neither path requests cancellation.

The context tracks actual launched `Popen` objects. Its `finally` block reaps any
still-live child and closes retained stdin/stdout/stderr handles, including when a
test assertion fails. Normal scheduler/test cleanup continues to own its lease,
observer, process guard, and disposable directory. The context does not rewrite
the scheduler's failure or cancellation state.

## Resource warning and recovery coverage

The queued fault test always requests 21 by 21 and 12 time steps. It checks child
marker identity, real process return status, failure cause, no cancellation,
finished GUI busy state, error status, and absence of a result. An advisory warning
is exercised by an explicitly synthetic one-byte available-memory estimate in
the submitting test process. This preserves queued warning metadata coverage
without increasing the requested workload or imposing a production memory cap.

The large requested-grid warning remains separately covered by
`ResourceUnitTests.test_memory_warning_precedes_any_grid_allocation_and_does_not_reject`.
That estimator-only test uses grid size 100000 with allocation functions guarded;
it does not enqueue or execute a simulation. A warning is advisory and does not
reject a valid grid in production.

After both faults, the launch seam is restored. The same GUI bridge submits a real
CPU 21 by 21, 12-step calculation through the unchanged worker. Assertions require
a `succeeded` job, full 21 by 21 result, persisted asset package, and restored GUI
result display. Success is not provided by a mocked algorithm or a probe result.

`tests/test_scheduler_resource_fixture.py` also checks malformed and oversized
scalar specifications, rejection before `Popen`, revalidation before the child's
worker entrypoint, unknown fault choice rejection, and actual child/stdio cleanup
when an assertion interrupts the fixture context. The previous 100000 queued
fault node is replaced; unrelated legacy queue-suite drift is outside this change.
