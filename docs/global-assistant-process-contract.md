# Independent assistant, project ownership and recovery

Date: 2026-10-19. Implementation contract for this cohort.

## State owners and transport

One per-user assistant process owns assistant windows, its provider worker and a registry of authenticated application clients. Launchers are clients: closing a launcher does not stop the assistant or independently owned project presentations. Assistant failure does not close any project. Launchers re-register after restart. No process-name scans, PID kills or remote-job termination are part of this protocol.

`AssistantProcessClient` discovers or starts the assistant through the existing `UiControlClient` / `UiControlServer` transport. An OS-held singleton file lock is authoritative; stale endpoint files do not reserve ownership. Endpoint metadata is ephemeral, uses the existing transport authentication token, and is never returned in public registry results. The bubble menu aggregates only authenticated registered application windows. Each action binds client generation, project ID, session generation, module key and native instance ID; a late action cannot activate/close a replacement or another project. Window discovery/actions run off the GUI thread. A client is admitted only after a nonce challenge through its authenticated endpoint and comparison of PID, OS creation identity, role and fresh generation. An unknown creation identity fails closed.

Public client operations are status, show_assistant, show_queue, reload_configuration, register_client, unregister_client, list_clients, activate_project, request_exit_all, exit_status and project_manage. Project operations always supply a root and project ID. UI focus and an environment-wide current project are never routing inputs.

Control action breadcrumbs are best-effort diagnostics. A missing, closed or
failing stdout stream cannot skip dispatch, replace its business reply, or
prevent request completion. Both begin/end logging errors are isolated, and
the relay always signals completion after a dispatched request. This protects
retained project clients after their original console/pipe disappears; it does
not diagnose a particular older process without its runtime evidence.

## Existing project activation (2026-10-15)

The launcher presents local sessions and authenticated sessions returned by the
same per-user assistant's `list_clients`. Remote entries are presentation-only:
they never enter the launcher's `ProjectSessionHost`, acquire another owner
lease, or expose recovery/export/close commands as if they were local. Discovery
and activation run off the launcher GUI thread because the assistant challenges
that same GUI thread through `lifecycle_status`. Late discovery results cannot
replace a stopped adapter's state.

The one added public operation is `activate_project`. Its arguments are exactly
six nonempty strings: `client_id`, `client_generation`, `project_id`,
`session_generation`, `root`, and `project_file`. The root and descriptor must
identify the registered project exactly after normal descriptor validation.
The assistant verifies the exact authenticated client, OS creation identity,
fresh client/session generations and descriptor binding, then asks that owner
to open its fixed `film_design` entry. No caller-selected module, endpoint,
transport token, document content, mutation or generic window action is allowed.
The owner reuses its existing workspace and draft; a retained session without a
current designer may recreate only its own designer view.

Success is `activated: true` with nonempty `instance_id` and the matching
`project_id`, `client_generation`, and `session_generation`. A request rejected
for stale identity, a different root/descriptor, shutdown, unavailable owner or
unsupported assistant version cannot report success. The launcher displays a
Chinese explanation and keeps existing leases, windows and drafts intact. A
failed lookup is never permission to steal or delete an OS lock. If admission
races with another opener, the OS lease remains authoritative and discovery may
resolve the newly admitted owner without a second writer. Normal opening does
not restore recovery data.

`status.capabilities` advertises `activate_project` on supporting assistants.
An already running older assistant is not automatically replaced or stopped;
unsupported activation is reported so its existing owners stay protected.

## Graceful exit protocol

A launcher/project host exposes lifecycle_status, lifecycle_prepare, lifecycle_commit and lifecycle_cancel. Status echoes a nonce and reports application `coat_sim`, process identity, generation, role and project sessions. Prepare receives an exit ID and exact client generation, freezes admission, and returns pending, ready (with opaque permit ID), or declined. Repeated preparation with the same exit ID polls the same decision. Every client must be ready before any commit. Commit/cancel carry the exact exit, generation and permit identities. Dirty-document and active-job decisions belong to the host. The assistant separately confirms its active conversation and local-computation consequences as specified below.

No commit starts after a refused/failed prepare. Uncommitted permits are cancelled on failure. Partial commit and unknown shutdown outcomes are reported explicitly, never hidden by OS termination. The assistant remains available until graceful commits have been requested and registered clients have exited. After15s an unresolved exact process identity produces a visible waiting notice; observation continues. The tray/bubble exposes Stop waiting and keep assistant, with a warning that committed closes may still finish and cannot be restored. This only abandons observation and never kills a process. A crashed/dead exact process is removed; PID reuse cannot remove its successor.

## Project mutation ownership

`ProjectOwnerLease.acquire(root, project_id, generation=None)` holds OS file locks for the writable session lifetime. The fixed per-user local runtime scope is independent of the assistant override; locks are keyed by both persisted project ID and canonical root, preserving project bytes on rejection. This is a per-user local application guarantee, not a multi-user or network-filesystem distributed lock. The same locks are required for a headless mutation; if a GUI owns it, the mutation fails with active_project_owner instead of bypassing its drafts. The lock is independent of the assistant process, so an assistant restart cannot admit a second writer. The lease validates the project manifest identity. It never replaces or deletes another writer lock.

`ProjectManagementService.execute(operation, arguments)` implements project creation, inspection, default dependency resolution, import, document creation/save, configuration/default changes, export and explicit recovery inspection using ProjectStore. Existing-project operations require explicit root and project_id. AI/MCP destinations and imported files must remain under software data or authenticated project-manager roots; the direct CLI uses its explicitly supplied paths. Mutations require operation_id and expected_revision; stale identities/revisions fail without rerouting. The persisted store's existing fingerprint/write lock remains authoritative for commit races. Active-project IPC also requires explicit session_generation from project_list. The registry challenges the exact owner and dispatches project_manage with project_operation, arguments, session_generation and client_generation. Only the trusted owning process calls execute_owned under its lifetime lease and host admission scope; no ownership assertion is accepted from a transport payload. Dirty draft replacement is rejected and the owner refreshes its local catalogs after commit. AI and MCP use this same adapter; the legacy CLI remains compatible and acquires the same ownership lock on mutations.

## Recovery boundary

The project session journal records clean/unclean lifecycle and explicitly supplied recovery checkpoints. It is not the normal loading root. Normal opening always calls the designated dependency graph and never consumes the journal. Only a previous unclean session with the same persisted project identity is offered as a recovery candidate. Candidate metadata is listed with generation, project revision, last-checkpoint time and available sections; restoration always requires an explicit section selection and confirmation, and never saves or replays calculations automatically. A clean close clears only the current session state. Before a new checkpoint, an unreviewed abnormal generation is atomically preserved in a project-local archive (up to four archived prior generations plus the previous journal generation). A second crash or a later clean close does not erase older unreviewed candidates. Explicitly archiving a selected section first retains it in the versioned `.project-session-reviewed-recovery.json` file, then removes only that section from its pending candidate; sibling sections and other generations stay available. Reviewed copies remain visible as archived candidates and can be restored only through the same explicit selection and confirmation flow. The legacy `discard_recovery` API is a compatibility alias for archiving a full generation and never deletes recovery data. The reviewed archive is bounded at 32 MiB; if it is full, the selected candidate remains unchanged and the user is asked to back up the archive before retrying. It is never automatically purged. Archive capacity/corruption blocks publication without altering saved documents or existing recovery bytes. Corrupt/stale recovery fails safely without modifying saved documents. OS ownership must be acquired before writing a journal.

## UI and errors

Assistant bubble and tray menus expose one Exit application action. While exit is pending, repeated requests reuse the same exit ID. Assistant chat/debug admissions and automatic queued-message drains are frozen after its own exit decision; cancellation restores them without discarding queued work. Cancellation leaves windows alive. Failures are displayed through the existing assistant notification/status channel. Existing shared Theme and window geometry rules apply; no new optical/numerical algorithms are introduced.


## Native window recovery and lifecycle diagnostics (2026-10-17)

Closing the conversation hides it; it neither destroys its owner nor exits the
shared service. The service keeps `quitOnLastWindowClosed=false`. The controller
owns the QML windows explicitly using C++ ownership. If either native QQuickWindow
is destroyed independently, it clears that exact generation, discards the old
pair and recreates the pair/context on the next user open. Late destruction from
an earlier pair cannot clear its replacement; desktop active-window callbacks
are rebound. Service generation and conversation storage remain unchanged.
Shutdown is idempotent and prevents queued UI callbacks from recreating windows.

The source assistant module uses the same Windows native-crash-handler policy as
the desktop bootstrap: inherited PYTHONFAULTHANDLER does not enable the Windows
vectored handler. A requested native stack dump cannot obtain a raw descriptor
from the bounded log stream; that path stays disabled, and Windows crash
diagnosis uses WER/observed exit codes. This closes a policy discrepancy; it does
not prove that an unobserved remote crash was caused by that handler.

`status` adds assistant_protocol=1, application version and a source_id. source_id
hashes the canonical installation directory, not the source bytes: it helps
identify different installations and is not code-integrity verification. Legacy
services without the added fields remain accepted after existing authenticated
PID/creation-time/nonce/generation checks. An explicitly different protocol is
rejected without terminating or replacing that service. OS-held locks remain
authoritative; stale endpoint files are not locks and are not cleared to restart.

All application diagnostics are UTF-8 under the launching installation's
`artifacts/logs/`. Shared-service discovery/ownership still uses
`%LOCALAPPDATA%\CoatSim\assistant\artifacts/runtime` (or
COAT_ASSISTANT_RUNTIME_ROOT); this override no longer relocates logs.
`assistant.log` belongs to the installation that started the shared service;
`assistant-client.log` belongs to each connecting installation and records the
authenticated service PID/version/source ID, so mixed installations are visible.
Other fixed channels are desktop, assistant-worker, project, mcp-ui, scheduler,
compute-worker and remote-worker. Each `.log` is at most 2 MiB and retains only
`.log.1` and `.log.2`: nine allowed channels use at most 54 MiB, plus one small
logging-only rotation lock. No timestamp/PID/job-named logs are created.
Writes/rotation are serialized across processes with a bounded 250 ms lock wait;
an unavailable disk/lock drops diagnostics rather than blocking the operation.
Single writes retain at most 64 KiB. Detached children install their own stream
before importing the application and do not depend on a parent relay pipe.
Agent protocol stdout remains a pipe; only diagnostic stderr goes to disk.
Pre-existing oversized *active channel* logs are trimmed before rotation;
unrelated/historical files, old external runtime logs and user data are not
migrated or deleted. The 54 MiB limit applies to these managed channel files.
Native faulthandler cannot bypass this byte limit with a raw file descriptor;
Windows native crashes are investigated using WER and observed exit codes. In contrast,
assistant-debug-PID.json is under the *installation* artifacts/runtime and is a
development endpoint. Its presence alone proves neither current liveness nor
shared-service readiness. Never send raw endpoint JSON: it contains IPC tokens.
For diagnosis share filenames, sizes/times and allowlisted protocol/loopback host/
PID/role/window_type/parent_pid, with installation paths redacted if necessary.
Locks, databases, ready-channel keys, tokens and arbitrary runtime payloads are
not required diagnostic attachments.

Logs can contain both legacy Qt/plain text and bounded structured JSON event
lines. Structured lines carry UTC timestamp, PID and event; relevant milestones
include attempt ID, child PID versus authenticated service PID, version/source
identity, registration recovery/failure, sanitized exception code/type/causes,
and observed child exit code. Successful status/registration heartbeats are not
logged repeatedly. Logging failures cannot replace IPC replies or stop GUI work.
An owned daemon thread observes only the Popen handle created by that client; it
never scans or signals another process. It cannot record an exit after its own
client process has already ended. Native failure still requires the observed
exit code or Windows crash report; ordinary Python exception logs are not proof
that no native crash happened.

Desktop bootstrap writes its time/PID/interpreter header before handing off to
the branded Python host. Previously that normal handoff could leave the first
desktop log empty. The assistant module opens its own log before acquiring its
lock or importing Qt, and flushes each lifecycle event. Disk/permission failures
are reported best-effort; log delivery is not a liveness guarantee.

`tests/test_assistant_native_reopen.py` runs owned native Windows processes with
private runtime, settings, database and debug roots. It covers close/hide/reopen,
first-user enable, independent window destruction, abrupt self-exit/restart,
single-instance ownership/real PID, inherited crash-handler policy and Chinese
installation/QML paths. No existing environment is copied or installed into,
and no live user installation, endpoint or lock is touched. Normal close/hide
and OS-lock recovery passed before the fix; destroyed-chat reopening and the
Windows handler-policy check failed before it. The other machine's spontaneous
exit has not been reproduced from the supplied successful-action-only log.

## Exit confirmation foreground ownership (2026-10-17)

The explicit assistant menu gesture grants foreground permission to the first
registered, OS-verified client before starting asynchronous exit work. The worker
requires that exact first client to remain current. After each ready decision,
`lifecycle_handoff` carries the same exit/generation/permit plus the next client's
exact PID and process creation identity. Only a ready, unexpired lease can grant
the next process foreground permission; malformed or reused identities fail before
any commit. A denied handoff cancels all uncommitted permits and retains drafts.
An older client without this operation fails safely; no process is replaced.

The project lifecycle restores an existing dirty view, then the runtime restores
and activates its existing confirmation window, transient to that view. Cancel
keeps the project/window/draft identities and releases the existing exit freeze.
No topmost flag, global foreground policy, or forced process exit is used. Native
coverage must click the real exit menu, check exact foreground HWNDs for normal,
minimized and covered windows, and exercise sequential distinct client processes
with real project drafts and cancellation.

## Launcher foreground handoff (2026-10-19)

`activate_shared_project(client, target)` calls the local
`AssistantProcessClient.prepare_project_activation(target)` before the existing
`activate_project` IPC. The input is the unchanged six-field activation target.
There is no new wire operation or registry state. On Windows, the local helper
reads the authenticated client listing, matches the complete client/session/root/
descriptor target, rechecks the owner's OS process creation identity, and grants
foreground eligibility only to that owner's PID with `AllowSetForegroundWindow`.
The launcher open-request worker runs inside the process receiving the user click;
the broker does not need to become foreground. Other platforms require no grant.

A stale target, unknown/reused process or denied Windows grant raises the existing
`UiControlError` path before activation IPC. It never creates another project
owner, changes system settings or retries by forcing focus. The owner still
restores/raises the exact registered window through the shared Qt activation
helper. The owner alone holds the window/session, and the adapter owns only its
existing pending request. Closing the adapter prevents late UI delivery without
cancelling an already admitted remote operation; there is no new asynchronous
lifetime. A later user gesture can supersede foreground eligibility.

The focused identity/denial tests are in
`tests/test_project_activation_foreground.py`; native desktop foreground checks
are distinct from mocked/offscreen tests.


## Local computation and complete exit (2026-10-19)

The assistant coordinates local scheduler services as well as registered GUI
clients. `SchedulerExitGroup` discovers only per-user registered scheduler
endpoints in the same assistant runtime scope, verifies the exact OS creation
identity and instance, and authenticates lifecycle requests. This registry is
ephemeral service ownership metadata, never project state or recent history.
Its secret is not returned through assistant status, public queue results or
diagnostics. It does not enumerate or signal processes by executable name.

The service group's input is one exit ID. Prepare takes the shared start gate,
freezes new local submissions and obtains renewable service permits. Existing
calculations continue during review. The assistant asks before cancelling any
local calculations; every dirty project still owns its existing save/discard/
cancel decision and foreground activation. No service or GUI close is committed
until all preparations succeed. Service commit requests durable cancellation
of running local calculations, waits for owned workers to settle and preserves
queued records. Remote schedulers remain outside this local-process contract.
Completion includes observing the exact scheduler process identities exit.
Failures and partial commits remain visible; there is no process-name kill.

Cancel restores service admission and queued assistant decisions without
discarding drafts or jobs. The assistant's desktop countdown/manual submission
paths are frozen during exit review. The desktop executor barrier settles previously admitted queue reads and
submissions before service discovery. The service start gate stays held through
GUI exit observation and the assistant provider/executor cleanup, preventing
late requests from restarting a daemon. Launches carry a start epoch captured
before process creation; exit advances that epoch, so a delayed pre-exit child
cannot publish after the fence is released, even when exit was cancelled.
Prepared service permits expire if their coordinator disappears; explicit
cancel releases them immediately. Only the assistant owns global exit intent.

Empty schedulers may close after their idle grace, releasing only their own
endpoint, registration and OS lock. Running or queued work and uncertain worker
recovery prevent idle disposal. Background assistant queue reads connect only
to a running service, keeping their last projection when offline; opening the
queue explicitly can start it again. A lightweight launcher close and ordinary
project close do not request cancellation of service-owned work. The shared
assistant continues independently until explicit application exit.

These are lifecycle changes only; project formats, physical algorithms and
numerical results are unchanged. Interface signatures and lifecycle tests are
the source of truth for request identity and cancellation boundaries.
