# Independent assistant, project ownership and recovery

Date: 2026-10-14. Implementation contract for this cohort.

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

A launcher/project host exposes lifecycle_status, lifecycle_prepare, lifecycle_commit and lifecycle_cancel. Status echoes a nonce and reports application `coat_sim`, process identity, generation, role and project sessions. Prepare receives an exit ID and exact client generation, freezes admission, and returns pending, ready (with opaque permit ID), or declined. Repeated preparation with the same exit ID polls the same decision. Every client must be ready before any commit. Commit/cancel carry the exact exit, generation and permit identities. Dirty-document and active-job decisions belong to the host. The assistant separately confirms its own active conversation/queue consequences.

No commit starts after a refused/failed prepare. Uncommitted permits are cancelled on failure. Partial commit and unknown shutdown outcomes are reported explicitly, never hidden by OS termination. The assistant remains available until graceful commits have been requested and registered clients have exited. After15s an unresolved exact process identity produces a visible waiting notice; observation continues. The tray/bubble exposes Stop waiting and keep assistant, with a warning that committed closes may still finish and cannot be restored. This only abandons observation and never kills a process. A crashed/dead exact process is removed; PID reuse cannot remove its successor.

## Project mutation ownership

`ProjectOwnerLease.acquire(root, project_id, generation=None)` holds OS file locks for the writable session lifetime. The fixed per-user local runtime scope is independent of the assistant override; locks are keyed by both persisted project ID and canonical root, preserving project bytes on rejection. This is a per-user local application guarantee, not a multi-user or network-filesystem distributed lock. The same locks are required for a headless mutation; if a GUI owns it, the mutation fails with active_project_owner instead of bypassing its drafts. The lock is independent of the assistant process, so an assistant restart cannot admit a second writer. The lease validates the project manifest identity. It never replaces or deletes another writer lock.

`ProjectManagementService.execute(operation, arguments)` implements project creation, inspection, default dependency resolution, import, document creation/save, configuration/default changes, export and explicit recovery inspection using ProjectStore. Existing-project operations require explicit root and project_id. AI/MCP destinations and imported files must remain under software data or authenticated project-manager roots; the direct CLI uses its explicitly supplied paths. Mutations require operation_id and expected_revision; stale identities/revisions fail without rerouting. The persisted store's existing fingerprint/write lock remains authoritative for commit races. Active-project IPC also requires explicit session_generation from project_list. The registry challenges the exact owner and dispatches project_manage with project_operation, arguments, session_generation and client_generation. Only the trusted owning process calls execute_owned under its lifetime lease and host admission scope; no ownership assertion is accepted from a transport payload. Dirty draft replacement is rejected and the owner refreshes its local catalogs after commit. AI and MCP use this same adapter; the legacy CLI remains compatible and acquires the same ownership lock on mutations.

## Recovery boundary

The project session journal records clean/unclean lifecycle and explicitly supplied recovery checkpoints. It is not the normal loading root. Normal opening always calls the designated dependency graph and never consumes the journal. Only a previous unclean session with the same persisted project identity is offered as a recovery candidate. Candidate metadata is listed with generation, project revision, last-checkpoint time and available sections; restoration always requires an explicit section selection and confirmation, and never saves or replays calculations automatically. A clean close clears only the current session state. Before a new checkpoint, an unreviewed abnormal generation is atomically preserved in a project-local archive (up to four archived prior generations plus the previous journal generation). A second crash or a later clean close does not erase older unreviewed candidates. Explicitly archiving a selected section first retains it in the versioned `.project-session-reviewed-recovery.json` file, then removes only that section from its pending candidate; sibling sections and other generations stay available. Reviewed copies remain visible as archived candidates and can be restored only through the same explicit selection and confirmation flow. The legacy `discard_recovery` API is a compatibility alias for archiving a full generation and never deletes recovery data. The reviewed archive is bounded at 32 MiB; if it is full, the selected candidate remains unchanged and the user is asked to back up the archive before retrying. It is never automatically purged. Archive capacity/corruption blocks publication without altering saved documents or existing recovery bytes. Corrupt/stale recovery fails safely without modifying saved documents. OS ownership must be acquired before writing a journal.

## UI and errors

Assistant bubble and tray menus expose one Exit application action. While exit is pending, repeated requests reuse the same exit ID. Assistant chat/debug admissions and automatic queued-message drains are frozen after its own exit decision; cancellation restores them without discarding queued work. Cancellation leaves windows alive. Failures are displayed through the existing assistant notification/status channel. Existing shared Theme and window geometry rules apply; no new optical/numerical algorithms are introduced.
