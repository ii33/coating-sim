# Independent local computation service

The GUI, MCP stdio server, desktop assistant, and CLI submit immutable version 1
job specifications to one headless daemon. Closing a submitting window or stdio
client detaches its observer. It does not cancel admitted computation. A new
client can inspect, cancel, or download the result using the job ID.

## Files and ownership

`coat_sim/scheduler/` owns submission, local transport, durable transitions,
in-memory status and event indexes, executor registration, and isolated workers.
`coat_sim/core/isolated_compute.py` retains the existing algorithms and its legacy
isolated API; a scheduler worker executes those algorithms directly, so array
results do not traverse a second control pipe. GUI observer threads and QThreads
only read remote state and download artifacts. `mcp/server.py` no longer owns a
compute queue or a daemon calculation thread.

The existing `artifacts/runtime/compute-tasks.sqlite3` is the durable queue. New
tables record immutable specs and hashes, worker process identities, event
sequences, and artifact descriptors. Job input, progress, outcome, and diagnostic
files live under `artifacts/runtime/scheduler/jobs/<job-id>/`. The service instance
holds an OS file lock; Windows workers belong to a kill-on-close Job Object with
no computation or memory quota. Only one worker runs at a time, in durable FIFO
admission order. A stale lock filename is not treated as a running service.

## Start, stop, and inspect

From this project's directory in PowerShell, use the project's Python:

```powershell
$python = 'C:\a\wk\coat\deposition_sim\.conda\python.exe'
& $python -m coat_sim.scheduler start
& $python -m coat_sim.scheduler list
& $python -m coat_sim.scheduler status JOB_ID
& $python -m coat_sim.scheduler cancel JOB_ID
& $python -m coat_sim.scheduler result JOB_ID
& $python -m coat_sim.scheduler stop
```

Clients start the daemon on demand. `serve` runs it in the foreground for
diagnosis. `--root PATH` selects an independent data/runtime root and precedes
the command. Stopping the service preserves queued work and explicitly marks
active computation interrupted. It does not install a Windows service, startup
entry, firewall rule, or credentials. Logs are project-local `artifacts/logs/scheduler.log` and
`artifacts/logs/compute-worker.log` (`remote-worker.log` for remote execution).
Each channel is limited to 2 MiB plus two rotating backups; job failures return
the actual diagnostic file path. Job state/result files remain in runtime. Work continues after client exit while this application daemon is
running; machine shutdown and service termination interrupt computation.

The existing algorithm CLI accepts its previous inputs and defaults, plus
`--detach`, `--job-id`, and `--time-step-s`. `--detach` returns the admitted job ID.
An interrupt of a waiting CLI detaches observation; cancellation is an explicit
operation. Business executors include process deposition, single-source
deposition, correction-board optimization, existing weight/field analyses, and
the existing sandboxed Python operation. `probe` is a small regression executor.

## Version 1 API

The daemon binds only `127.0.0.1` on an ephemeral port. The endpoint document is
local discovery metadata and is cached by clients. The JSON envelope is:

```json
{"protocol_version":1,"action":"status","arguments":{"job_id":"example"}}
```

`POST /v1/rpc` responds with `ok`, `protocol_version`, and `result` or `error`.
Actions include health, capabilities, submit, status, snapshot, events, page,
cancel, result, run_result, find_request, artifact, and stop. Cross-origin browser
requests are rejected. This is a local application boundary, not an authenticated
remote deployment.

The JSON control request body is capped at 64 MiB and rejected before parsing when
larger. Artifact downloads remain binary `GET /v1/artifacts/<id>` responses streamed
in 1 MiB chunks; large result data must use the artifact channel rather than an RPC
response body. A successful result may expose a typed inline summary and a
registered `role: "result"` artifact; consumers verify and download that artifact
through the same artifact channel.

`JobSpec` has exactly `protocol_version`, `executor`, `parameters`, and `inputs`.
Inputs are frozen content and SHA-256 records; process dependencies are captured
before admission. Source paths may be retained as audit provenance, but workers
execute captured content and job-local materialized inputs. Closures and Qt
objects are never serialized or executed across IPC. Same ID and same spec hash
return the original task without another event; different content with that ID
is a conflict. Client retransmission retains the chosen ID.

The control response contains scalars and artifact descriptors (ID, role, size,
name, SHA-256), not NumPy arrays or paths a client must open on the server.
`GET /v1/artifacts/<artifact-id>` streams binary data separately. The client
verifies bytes and SHA-256 before atomic publication into its local cache.
`SchedulerTransport` is a dependency-free seam for later remote request/download
adapters. Remote TLS, authentication, authorization, upload staging, and actual
remote hosting remain future work; this change does not enable external access.

## Durable and in-memory state

Admission, cancellation, and state/event transitions commit to SQLite before
they are published into the in-memory index. All active tasks and the latest
100 terminal tasks are resident. Common status, queue snapshot, recent first
page, and event requests use that index; progress is coalesced to at most four
updates per second. Queries do not repeatedly deserialize job files or open
SQLite. Explicit older-history pages, historical details, or artifact lookup may
perform a lazy storage read. Approval changes explicitly refresh the cached
confirmation index.

`events(after_sequence, wait_s)` supports reconnect and long polling (up to
30 seconds). An expired/restart cursor returns `reset: true` with a current
snapshot. Clients reconcile to its sequence instead of assuming missing events
mean nothing changed. Artifact hashing and binary transfer occur outside the
state-cache lock.

Cancellation records the durable flag before notifying the worker; notification
failure is retried and cannot turn a rolled-back request into a cancellation.
An accepted cancellation wins over completion that has not yet committed.
Repeated cancel is idempotent; terminal tasks are unchanged. On service restart,
queued jobs remain queued. Previously running jobs receive
`failed`, `interrupted: true`, `scheduler_interrupted`, and worker/lease evidence.
The service verifies process creation identity before terminating an orphan;
unverifiable ownership pauses recovery rather than killing an unrelated PID or
replaying work. A saved file alone is not a verified completion.

Worker crash, absent completion envelope (`worker_channel_closed`), algorithm
failure, result-save failure, cancellation, and interruption have distinct
reasons. A failed worker releases its execution slot so later work can continue.
Startup failure after spawning a worker but before publishing `launch.json`
reaps that newly created child before recording `worker_start_failed`. This
includes guard attachment, worker-identity persistence, launch-file publication,
and closing the launch log. The child has not yet been released to execute an
algorithm. Cleanup failure retains the owned process handle and execution slot
and pauses the scheduler; it cannot release a slot while a worker might remain
alive. A terminal-write failure re-raises the original startup exception with
the persistence failure attached as diagnostic context. This startup cleanup
does not add a post-launch process-tree termination policy.
Estimates and memory warnings are retained; no new hard work-size or memory
rejection limit is introduced. Existing sandbox attachment/security policies are
unchanged. Atomic JSON/download publication uses the preserved bounded
`atomic_io.replace_with_retry` implementation for Windows transient locks.

## Verification and limits

Run lifecycle regressions with standard-library unittest:

```powershell
& $python -m unittest discover -s tests -p test_scheduler_service.py -v
& $python -m unittest discover -s tests -p test_atomic_io.py -v
& $python tests\scheduler_real_checks.py
```

The real integration harness is CPU-only with grid 21, immutable fixture inputs,
and assertions on actual deposition time steps (<= 0.05 seconds). It exercises
deposition, pending correction boards, result-save failure, the GUI calculation
and launcher shutdown path, and MCP/CLI state agreement. It does not change a
user's requested time step or run a GPU stress test. See
`scheduler-test-evidence.md`, `scheduler-independent-review.md`, and the generated
`artifacts/scheduler-validation/real-checks.json` for actual results, corrections,
and tests not run. Windows transient file release and restricted symlink creation
remain environment differences. No independent Seed reviewer was available;
the 实现者 review is evidence, not a substitute for that separate sign-off.

Existing runtime queues without frozen scheduler specs are not automatically
replayed as new service tasks. A live legacy execution lease blocks service
execution. Deployment into a workspace with such legacy tasks requires explicit
reconciliation; development and trials here use isolated roots.
# Client efficiency contract (protocol v1)

The optimizations below preserve the v1 JSON actions and payload shapes.

- A successful `status` response may include the same path-sanitized `result` returned by `result`. Observers can consume it directly and call `result(job_id)` only when it is absent.
- An observer may take an initial `snapshot`, then wait on `events(after_sequence, wait_s)` with `wait_s` bounded to 1 s. The wait is condition-notified by published events, so the bound limits idle reconnects rather than event delivery latency. It advances only over returned event sequence numbers, filters by job id, and reconciles from a new snapshot/status if the event stream reports a reset. When event actions are unsupported, it falls back to status polling. Observer shutdown does not cancel the job; its background request may take up to the wait bound to return. The process editor retains its detached QThread in a background reaper until it exits, so the UI close path does not join it synchronously. Explicit cancellation remains a separate `cancel(job_id)` request.
- The service owns durable terminal state; the Qt bridge owns its local observer and cancellation-event bookkeeping. A service record becoming terminal does not synchronously deliver the bridge's queued completion callback. Main-window lifecycle checks wait for the particular local job ID to leave `_job_cancel_events` before asserting cleanup. Cancelling the newest job leaves older window jobs active; each late completion removes its own bookkeeping, while only the newest job may update the displayed result or status. `test_cancel_latest_preserves_older_job_lifecycle` deliberately gates terminal observation to cover this boundary, and `test_older_success_after_latest_cancel_cleans_up_without_replacing_latest_state` covers late older success.
- A client may pass the artifact descriptor already obtained from `artifact` into `fetch_artifact`; it may reuse the established endpoint instead of another health/metadata round trip. Every download still validates byte length and SHA-256 and publishes atomically.
- `JobSpec.parse_frozen` is the trusted boundary helper for one canonical serialization and digest calculation. Store and worker code may reuse that parsed value, canonical JSON, and digest within a single operation. Durable input digest checks, worker/store comparison, and both pre-freeze and post-freeze input-file fingerprint checks remain required.

These are client/storage implementation rules, not new wire semantics. The event wait bound and serialization reuse are project implementation choices, not externally validated performance guarantees.

## Opening a queued simulation result

The queue page owns job state and artifact availability. Opening a successful
simulation creates an independent normal simulation workspace from the verified
`.coatasset` bundle. The bundle's embedded `input_snapshot` is the source of
recalculation inputs; its portable design members are loaded from the archive,
not from provenance paths that may since have moved or been deleted. A new
calculation is submitted as a new queue job and does not modify the original job
or result.

If the artifact is missing, corrupt, or cannot be associated with the successful
job, opening fails with an explicit unavailable-result message. If the bundle
does not contain a complete frozen input snapshot, the result remains viewable
but recalculation is disabled and the missing snapshot is explained; current
design files are never substituted. Result loading and artifact transfer run
off the UI thread. Closing the viewer stops observation and releases its window;
it does not cancel or change the original job. Saving a copy continues to use
the existing confirmation flow. The page uses the shared Theme controls and
window geometry policy.

Process and process-recipe jobs capture each completed layer's actual incremental
thickness field into a per-run spool while the worker is running. The spool is
temporary worker state; it becomes part of the result only after all requested
steps finish and the asset passes its field and thickness-closure checks. The
queue owns job state and completion: cancellation or failure discards the spool
and cannot publish a complete layer-field manifest. Each run has an isolated
spool path, so retries cannot combine fields from earlier attempts. The frozen
job's `include_layer_fields: false` option can disable capture; otherwise these
job kinds include fields by default. Captured fields retain the simulator's
float64 values, nanometre units, per-step coordinate axes, finite-node validity
mask semantics, and the source layer/material mapping from the frozen design.

### Terminal optimization candidates

A failed or cancelled optimization may still expose its best-so-far candidate.
The durable job keeps its original terminal `state`, `error_code`, and `error`;
result availability is a separate capability. The ordinary `result` object may
include this shared partial-result descriptor:

```json
{
  "partial_result": {
    "schema_version": 1,
    "kind": "best_candidate",
    "best_so_far": true,
    "candidate_verified": true,
    "quality": {"metric": "full_recipe_uniformity_score", "value": 0.12}
  }
}
```

The result also carries the normal `run_id`, asset, and artifact manifest. A
consumer may open a `failed` or `cancelled` result only when this descriptor is
present and the portable bundle, run identity, and frozen `input_snapshot` all
validate. A missing or corrupt checkpoint does not become a viewable empty
result; the terminal reason remains visible and the queue explains that no
verified candidate is available. Saving uses the ordinary result asset flow.
Checkpoint publication writes a new bundle first and atomically changes the
job-local pointer, so a later failure cannot replace an earlier valid candidate
with an incomplete or corrupt one. This descriptor is a result capability, not
a new scheduler state.

### Typed film optimization results

Film optimization uses a different product kind from simulation assets. Submit
it with `freeze_spec(service, "film_optimization", {"request": request})`;
this calls `freeze_job_payload` before the `JobSpec` is persisted. The worker
dispatches `coat_sim.film_optimization_job` through
`execute_film_optimization_job`, mapping the scheduler cancel and progress
callbacks directly and writing incumbent results to the job-local
`best-candidate.json` checkpoint.

The result and checkpoint use schema version 1 and kind
`coat_sim.film_optimization_result`. They contain the frozen `request`, a
`candidate` object of kind `coat_sim.film_optimization_candidate`, and the
shared `partial_result` descriptor. The candidate records the proposed design,
`thickness_nm` keyed by stable layer ID, the full condition error table, and
quality metrics. `max_normalized_error` is the primary quality value and lower
is better; `targets_met` remains separate. The descriptor additionally carries
`artifact_kind: "coat_sim.film_optimization_candidate"` and
`artifact_schema_version: 1`.

Before publishing an incumbent, the scheduler adapter calls the executor's
`validate_checkpoint(service, result)`. Only a candidate whose frozen material
snapshot replays and whose finite quality values and complete condition table
match is atomically written. A rejected candidate or write error leaves the
previous checkpoint intact. On failure or cancellation the worker reloads and
revalidates that checkpoint; if none validates, the terminal record has no
viewable result and includes an unavailable reason. The job's original
`state`, `error_code`, and `error` remain unchanged.

Film results have no deposition `run_id`, simulation `asset`, `process`
snapshot, `field`, or simulation artifact manifest. The result capability and
queue reader branch on the typed film result kind and replay it from its frozen
request; they do not load it as a `.coatasset`. The shared queue emits
`ComputeQueueController.filmOptimizationResultReady(job_id, result)` after
validation so the film UI owner can display the candidate. Viewing never
applies it to the source design.
