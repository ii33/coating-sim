# Independent scheduler read-only review

Scope: `scheduler-workspace` as inspected on 2026-10-06 UTC. This was a one-pass read-only audit; no tests or calculations were run, and no files outside this review note were changed. Files were still being edited during inspection (for example `scheduler/client.py` 23:57, `daemon.py` 23:55, and `ui/launcher.py` newer), so findings below describe the observed revision and need a final recheck after integration.

## Findings requiring follow-up

1. **GUI observer calls do not match the observer API.** At inspection, `coat_sim/scheduler/desktop.py:11-21` defined `observe_job(client, job_id, progress=None)`, while `coat_sim/ui/main.py:813` and `coat_sim/ui/process.py:62` passed `disconnected=`. Python raises `TypeError` before observing a newly admitted job; the daemon job can continue, but these callers report observer failure instead of the result. The parent agent has acknowledged and is correcting this.

2. **Some queue clients still bypass the protocol for result reads.** `coat_sim/ui/compute_queue.py:214,251` constructs `ComputeTaskStore` and calls `result_asset_path` directly. This reads the service-side SQLite record and returns a service filesystem path instead of fetching by artifact ID. `coat_sim/mcp/server.py:2071-73` likewise calls `ComputeTaskStore.find_request` directly for `find_calculation_job`. The parent agent has acknowledged both paths and is moving them to `SchedulerClient`.

3. **The result/status wire shape contains server-local paths.** `coat_sim/scheduler/daemon.py:224-27` returns the stored result without sanitizing it; `coat_sim/scheduler/store.py:40-45` also stores `input_snapshot_file` as a local path. Simulation results built in `coat_sim/scheduler/worker.py:126-145` contain `result_file`, `asset`, report paths and file records. The artifact metadata action removes only `local_path` (`daemon.py:258-60`), so the general status/result responses still expose paths and cannot yet serve as a location-independent contract. The parent agent has acknowledged and is changing wire serialization to artifact IDs.

4. **Input assets are carried inline in the submit JSON.** `coat_sim/scheduler/jobs.py:57-95` embeds frozen design data and base64 attachments inside `JobSpec`; there is no asset upload/register action or server asset ID in `SchedulerClient`/`Transport` (`scheduler/client.py:28-48`, `transport.py:7-9`). This is portable for small jobs, but a large dependency makes the RPC body itself large, and the client contract does not yet express content-addressed input asset transfer. This is a remote-readiness gap; the local-only implementation has no evidence of external listening.

5. **Cancel marker and SQLite commit are not atomic.** `coat_sim/scheduler/store.py:88-103` touches the shared cancel marker before updating and committing the durable scheduler rows. If a later DB write/commit fails, the caller gets an error but the worker can still see the marker and cancel. The normal success path is correctly serialized; this is specifically a failure-path consistency issue. Prefer committing the durable request first, then signaling the worker, with a recoverable signal mechanism.

6. **The cancel-race test does not force a running-job race.** `tests/test_scheduler_service.py:145-55` submits a 60 ms probe and immediately cancels it, which can cancel while still queued. It does not coordinate completion against an accepted cancellation while `running`. Also, for a running task `SchedulerStore.cancel` only sets `cancel_requested` and leaves `state` as `running` (`store.py:91-103`); consumers must inspect the flag rather than rely on a `cancelling` state. The locking in the implementation appears to choose one terminal result consistently, but that race was not demonstrated by the tests inspected.

7. **The protocol-specific acceptance tests do not yet prove the memory-only and adapter-sharing claims.** Existing tests exercise `SchedulerClient` directly. I did not find an assertion that repeated list/status/events calls avoid SQLite/JSON reads, nor an integration assertion that GUI, MCP, and CLI observe the same admitted job. The tests cover client-process exit, duplicate-ID behavior, queued restart, worker crash, save error, frozen inputs, event cursor reset and a large file result by source inspection; they were not run during this audit.

## Behaviors supported by the inspected code

- The daemon is a separate process; its scheduler loop launches a separate worker (`daemon.py:271-89`). The worker has no client control pipe and persists progress/outcome files. Closing a client does not trigger a cancel path. The MCP shutdown hook is a no-op (`mcp/server.py:2149-57`).
- The daemon holds an OS instance lock (`daemon.py:18-32`) and one active worker slot. `SchedulerStore.claim` uses `BEGIN IMMEDIATE` and checks the existing execution lease before claiming (`store.py:105-22`), so the inspected current path has a single queue executor.
- Same job ID plus same input fingerprint returns the prior record; a different fingerprint is rejected (`store.py:25-56`).
- Cancel/claim/final transitions use SQLite write transactions, and daemon commits are serialized by a reentrant condition lock. The finish path rechecks `cancel_requested` inside that lock before writing the terminal state (`daemon.py:313-25`).
- Startup examines persisted `running` rows, validates worker PID/start identity, terminates a verifiable leftover worker and records `scheduler_interrupted`; queued rows remain claimable. If identity cannot be safely established, the daemon records `recovery_error` and does not dispatch more work (`daemon.py:71-76,97-16,327-33`).
- Result bytes use separate artifact downloads with size and SHA-256 verification (`daemon.py:339-52`, `client.py:110-31`, `transport.py:11-29`); the status RPC does not carry result arrays. The remaining issue is the path-bearing metadata noted above.

## Review limits

This was a source review, not Seed independent sign-off or 复核者 final approval. I did not execute tests, launch the daemon, or inspect/modify the original `C:\a\wk\coat\deposition_sim` tree. Recheck the line-specific findings against the final integrated revision.



## Owner resolution after review

The observer accepts `disconnected`; the result viewer and request lookup now use SchedulerClient. Public RPC payloads exclude service paths; array/report/bundle consumers use downloaded artifact IDs. Cancellation notification follows durable commit, duplicate cancellation is idempotent, and worker notification failure is retried. Added forced-running near-completion cancellation and SQLite rollback tests; the strengthened 14-test suite passed. Memory-only hot-query assertions and the real GUI/MCP/CLI integration passed. Frozen inputs currently carry content plus hashes inline; a remote blob upload stage and remote authenticated deployment remain future work. This resolution is the implementation owner report, not another independent review.
