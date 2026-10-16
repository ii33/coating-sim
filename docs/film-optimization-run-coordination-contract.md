# Film optimization run coordination contract

Date: 2026-10-12. Status: implementation boundary for the progressive UI
refactor; no change to queue protocol or optimization numerics.

## Owners

`FilmOptimizationQuickController` remains the QML facade and owns design
configuration, the frozen request input, source session/revision adaptation,
candidate presentation, and the decision to apply or save a result.
`FilmOptimizationRunCoordinator` is a QObject child of that controller and is
the only owner of one window's active attempt: attempt sequence and active ID,
queue cancellation event and cancellation intent, worker, private thread pool,
busy state, and pending-close lifecycle. The controller reads attempt state
from the coordinator and handles its progress and terminal signals.

The scheduler store/daemon remains the only queue claim and lease owner. A
window attempt observes a submitted job; it does not claim, release, or recreate
scheduler ownership. One coordinator is created per optimizer controller, so
independent windows cannot replace each other's worker or attempt state.

## Start input and outcome

The controller validates the source and user configuration, freezes the
request, verifies queue capabilities, and prepares the job metadata. It passes
the prepared submission and worker construction inputs to the coordinator.
Starting while an attempt is active returns `False` before a second queue
submission. The coordinator constructs the worker and connects its
progress/finish signals before submitting, so worker preparation failure does
not admit an unobserved queue job. A successful start submits once, creates
one monotonically identified attempt, marks it busy, and starts the worker in
the coordinator's private single-thread pool.

If capability lookup, request freezing, worker construction/signal connection,
or queue submission fails, no attempt becomes busy and no worker is installed.
The caller keeps the existing user-facing diagnostic. If the queue accepted a
job but the thread pool cannot start its observer, the coordinator requests
cancellation, clears local attempt state, and reports both startup and
cancellation transport errors when applicable.

Progress and completion carry the attempt ID. The coordinator delivers events
only for its current active attempt. Accepted completion clears the active
worker and busy state before its one-shot signal to the controller. The controller still validates the
source design revision and pending-edit epoch captured for that attempt before
any result can write back. A source close or destruction disconnects source
observers, requests cancellation, and makes later results ineligible for
writeback. Results from a previous attempt or another window never change the
current attempt or its candidate.

## Cancellation and close

Cancellation has two distinct facts: the user requested cancellation, and the
queue accepted the cancellation request. User intent is recorded immediately,
so a result that races with cancellation cannot auto-apply. The queue cancel
transport is sent until one call succeeds; a transport exception is reported
and may be retried. Once the cancellation event records a successful send,
repeated cancel and close requests do not send another queue cancel.

Closing during an active attempt sets one pending-close state and requests
cancellation. Repeated close requests do not create another attempt or another
successful queue cancellation. On terminal delivery the controller follows
the existing candidate confirmation/close behavior. Shutdown invalidates the
active attempt, requests cancellation through the same idempotent path, waits
for the private pool up to the supplied timeout, and ignores late signals.

## Thread and QObject boundary

The coordinator and controller are created on the GUI thread. The coordinator
owns a `QThreadPool` QObject child with one worker slot and retains each
`QRunnable` until terminal delivery. Worker progress and finish signals are
accepted on the coordinator/controller GUI thread; only the queue observation
and optimization task run in the worker. QML properties and source session
updates stay on the GUI thread. Source Qt signals are disconnected on close,
destruction, and shutdown.

## Errors and compatibility

Existing QML property names, progress text, queue job IDs, candidate payloads,
source revision checks, cancellation result behavior, and save/apply policy
remain unchanged. Invalid request/configuration, unavailable queue capability,
submit failure, worker failure, cancellation, and stale source are surfaced
through the current controller error/result state. This extraction does not
change `FilmOptimizationRequest`, queue payload schemas, scheduler claim/lease
logic, solver selection, numerical budgets, or result validation.
