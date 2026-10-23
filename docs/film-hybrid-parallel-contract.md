# Hybrid physical evaluation parallel contract

This contract is written before the implementation. It extends only independent
hybrid physical work; it does not parallelize archive mutation, ranking, audits,
SLSQP, joint finite needle optimization, or adaptive refinement dependencies.
The existing private persistent framed-stdio executor is the only transport.
No socket, GUI IPC, or alternate transport is introduced.

## Ownership and ordering

The parent freezes the complete validated optical request once. Each owned
worker independently prepares that frozen content once and reuses only static
material optics. There is no score or topology cache. Every task carries the
complete design, original stable layer IDs, zero IDs, and Jacobian mode. Hybrid
tasks retain original full-grid response arrays and all analytic Jacobian columns;
legacy tasks keep their existing compact response contract. Input content, not
user-supplied digest metadata, defines the frozen request. Reconfiguration after
worker startup is forbidden.

Independent engineering-prior candidates and each already determined coarse or
valley-refinement round are evaluated in bounded source-order batches. A round
cannot use its own partial results to invent a deeper refinement level. Serial
and parallel use the same batching, original grids, physical evaluator, analytic
derivatives, proposal IDs and adoption order. Worker completion order cannot
mutate the archive or choose an explorer. Finite all-thickness optimization stays
serial. There is no new implicit layer limit or relaxed nominal acceptance.

## Budgets and completed work

Before dispatch, the parent reserves the entire bounded batch from the current
stage and global training allowance, leaving the existing final independent audit
slot untouched. Reserved slots cannot be used by nested work. All batch results
are physically evaluated and consumed in source order before archive adoption;
early target detection therefore cannot hide already completed eager work.
A stage deadline gates new batch admission; admitted calls may finish after that
local slice deadline. Whole-request deadlines and user cancellation remain active
through startup, evaluation and IPC waits. Records expose reservations, actual
consumption, released unsubmitted slots and dispatched-but-unused slots. Submitted
unused calls consume logical slots and remain discarded/unknown in executor work
counters; physical work without a received reply is never reported as known zero.

## Resource policy

The frozen configured working-set resource budget is not resampled. Concurrency
is capped by requested/available CPU limits, bounded batch size, and the remaining
budget after a conservative parent workset and retained result estimate. The
existing 128 MiB + points*layers*(64 Jacobian / 32 forward) workset estimate is an
advisory numerical admission model, not an OS isolation quota or measured RSS.
Retained response/Jacobian storage is estimated separately. If only one worker
fits, evaluation stays serial with an explicit reason. Diagnostics report the
requested limit, actual owned PIDs, effective batch width, estimate and frozen
budget. Requesting 19 CPUs does not promise 19 workers. Existing live workers are
idle outside the bounded active width and their baseline residency is included.

## Failure and cancellation

Workers/readers/writers belong to one run and close through the existing bounded
cancel-file/stdio/terminate/kill/join path. Failed startup disables the pool and
uses a recorded serial fallback. Worker crashes have an uncertain physical-work
outcome; fallback recomputes only the logical physical task, while original work
stays unknown. Parent exceptions, total time limits and cancellation close owned
processes and release reservations. No worker outcome can claim nominal success;
strict targets_met, independent solver_acceptable, failed/not_found/cancelled
and no-auto-margin behavior remain owned by the unchanged parent/audit boundary.

Implementation locations: film_auto_synthesis.py, film_synthesis_batch_needle.py,
and film_synthesis_parallel.py. Limits and estimates are project conventions,
not external memory or speedup guarantees. The parent integration must update
algorithms.html in the same release.

## Current width and resource details

The synthesis-only parallel_batch_size is a positive integer or None (AUTO).
Hybrid freeze resolves AUTO once to min(requested positive cpu_workers,
available CPUs), or available CPUs for cpu_workers=0, with a minimum of one.
The resolved positive logical quantum is stored in the frozen request and is
preserved on replay even when execution cpu_workers changes. Thus serial1 and
parallelN equivalence runs must start from the same frozen logical quantum.
Legacy AUTO remains None and does not change legacy behavior or identity.
Explicit positive quanta are preserved. There is no hard four-worker limit.

Actual source-prefix size and active workers are independently bounded by this
frozen logical quantum, currently available source candidates, stage/global
allowances, requested/available workers, frozen memory budget and frame-size
estimate. Hybrid cpu_workers=0 follows the frozen AUTO quantum, rather than the
legacy executor's default-eight selection. A short stage, insufficient memory,
small workload, or transport-size estimate can reduce actual width further.
Every record reports the frozen logical size and actual owned PIDs. These are
scheduling/resource bounds, never physical layer bounds.

Previously persisted queued requests may omit the new logical quantum or the
optional resource-resolution record. Execution resolves these controls but
preserves the original JobSpec's frozen request in the result. Actual resolved
controls are recorded separately in performance.resolved_execution_controls.
Only those two execution controls may differ during normalization; any other
request change is rejected. Worker/queue identity comparison stays exact,
including negative cases for injected resolved metadata, targets and optics.

Top-level owned/ready PID records describe processes which completed a startup
handshake, possibly without receiving work before cancellation. The GUI prefers
workers_with_submitted_tasks for the actual working-process count, keeping the
owned PID count separate; missing historical fields never imply requested
parallelism. Per-stage records continue to identify dispatched physical tasks.

The parent estimate includes the existing numerical-workset formula plus the
retained compact archive's deterministic conservative Python-container proxy,
computed without copying its data. This includes complete designs, summary scores and provenance, but no full
response/Jacobian matrices because the archive deliberately strips those. Shared
values are counted by occurrence, independent of Python interning/IPC aliases,
so serial/process replay uses the same numerical admission estimate. Python allocator overhead, unrelated interpreter state,
trace storage, OS page accounting and RSS are not measured or guaranteed by this
advisory model. Retained full responses use the separate conservative estimate
points * (320 + 40 * Jacobian_columns) + 4096 * layers bytes. Archive containers
use 192+72N bytes for mappings, 64+8N for sequences, 96+4N for text, 128 plus
attribute storage for records, and 32 bytes for scalars, recursively by occurrence.
These deterministic conservative constants are project conventions. Idle owned worker
baselines remain included when sizing the active width. Oversized full-response
frames stay serial instead of discovering the existing transport limit by a
worker crash. No alternate transport is attempted.

Per-candidate resource admission precedes source batching. An oversized prior is
reported and skipped independently, while smaller pending source-prefix work is
still physically scored. A memory-limited analytic batch returns a bounded
prefix; the scanner commits scalar derivatives and releases the prefix's full
response/Jacobian values before requesting its remaining independent probes.
The engineering-prior stage reserves one of its stage slots for audit where the
quantum permits; additional audits are deferred if that slice is exhausted. The
whole-request final-audit reservation always remains independent.

Explicit GPU-forward requests retain their existing sequential GPU-forward path
for non-derivative candidates; this CPU extension does not silently replace that
request with headless CPU forward work. Analytic derivatives remain CPU work.

## Bounded physical timing, 2026-10-22

On this executor, 12 independent 48-layer analytic-Jacobian evaluations on the
original 1001-wavelength grid, retaining full responses, took 0.605 s serial.
Two real stdio workers took 0.452 s for the first physical batch and 0.372 s on
warm reuse; startup was 0.135 s, so cold startup plus first batch was 0.587 s.
The warm evaluator-stage ratio was 1.63x. Exact score agreement and complete
worker/reader/writer cleanup were checked. This synthetic-material timing used
the same persistent evaluator backend directly with a bounded rolling task
stream; it is not a whole-hybrid-pipeline or 19-worker speedup claim. Performance
varies with optical work, response transfer, serial optimizers and startup cost.
The original cold four-window 184600-constraint/108-layer 1200-second failure
remains an unchanged failed nominal baseline and was not rerun or relabeled here.
