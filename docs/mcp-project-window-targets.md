# Explicit MCP project window targets

Date: 2026-10-15. This interface adds no project format or numerical semantics.

## Inputs and state owners

Existing window tools accept optional `project_target`, containing exactly the
six nonempty strings `client_id`, `client_generation`, `project_id`,
`session_generation`, `root`, and `project_file`. `project_list` returns this
object. Module keys are the existing `PROJECT_MODULES`; global windows cannot
use a project target. No endpoint, port, token, executable or foreground selector
is accepted. Global tools and explicitly selected standalone CLI hosts retain
their previous interface. A targetless business-window request never chooses a
project owner.

`project_open(project_file)` opens an explicitly authorized exact `.coatproj`
file through `open_project_runtime`; it returns a target only after authenticated
owner discovery and activation. A detached process or successful headless
inspection alone does not establish UI success. The open path is restricted to
existing service roots and authenticated registered roots, like project management.

The shared assistant registry owns routing, not project state. Every request
challenges the exact registered owner with a fresh nonce, verifies PID creation,
client/session generations, canonical root, exact descriptor and persisted ID,
and forwards only a bounded operation to that owner. Open verifies the same
instance with a visible-window read before reporting success. Expired identity fails
without rediscovery, another owner, or a new writer. The runtime rechecks its
client/session identity and routes only through its bound desktop/controllers.

## Capabilities, mutation and close

`list_windows(project_target)` lists only that session's existing routes and
allowed modules. `read_window` returns actual supported navigation capabilities;
closed windows report closed. Open/read/close work for all existing module keys.
Read is window/identity status, not a full editor document snapshot. The separate
[project_editor_snapshot](project-editor-snapshot-contract.md) read-only tool
projects existing current film-design/simulation/optimization owners with bounded pages and
snapshot consistency checks. It never opens an editor or substitutes saved data.
The separate guarded editor gateway supports only the returned per-module
`editor_operations`/field/action whitelist for film design, simulation and optimization.
`update_window` and `window_action` require the exact current `instance_id` and
`expected_snapshot_id`; unsupported modules/actions, document/path replacement,
and force are rejected. They never fall back to a global adapter or a headless
write into a live editor. Existing
`project_manage` remains a headless managed interface with dirty/editor-owned
write restrictions; it is not a universal replacement for editor controls.

Pending native field edits, confirmation, transfer, busy and project-close
review fence mutations. `force` never authorizes project-close or sibling-draft
discard. Computational actions cannot bypass the existing estimate/confirmation flow. Close invokes the native bound window
close path, including last-window project preflight and its Save/Discard/Cancel
review. A close admission is not a claim that asynchronous user review finished.

The typed target contract and shared `control_errors.UiControlError` import no
presentation package or assistant runtime. The historical
`ui.control.UiControlError` name re-exports the same class, preserving exception
identity and code/message handling at existing boundaries.

## Verification

Contract tests use fake authenticated transports for stale identities, malformed
targets, copied IDs, module allowlists, failed opens and non-bypass routing. Qt
controller tests cover actual bound window state and native close dispatch without a launcher.
Native IPC/process acceptance is a separate gate; sandbox denial is not replaced
with another transport.


Current-context reads cover every registered project module through
`project_editor_snapshot`, with a module-specific detail whitelist. Resource and
compute adapters read existing owners even when no film editor has been created.
The queue's explicitly cached-view scope is not an authoritative scheduler query.
Uncommitted native/modal/geometry drafts remain unavailable; reads never open a
window, load a resource, refresh materials, calculate or grant mutation rights.
See the snapshot contract for the exact per-module collections and fences.
