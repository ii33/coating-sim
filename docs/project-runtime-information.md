# In-project information editing

Date: 2026-10-16.

The existing `ProjectRuntimeManagement.qml` page edits the current project's
name, summary and notes. These fields describe the project itself, independently
of resource names or resource-information editors. The launcher remains a
read-only recent-project client and owns no store, lease or runtime.

`ProjectRuntimeOwner.projectInformation(projectId)` returns a detached map with
`projectId`, `sessionGeneration`, `revision`, `name`, `summary` and `notes`.
`saveProjectInformation(projectId, sessionGeneration, expectedRevision, name,
summary, notes) -> bool` admits only the exact local session and its current
project revision, under `ProjectSessionHost.operation_scope`. Pending project
close, application exit, a closed/replaced session or an unavailable owner rejects
the operation. It never uses selected-project or foreground-window state.
`ProjectStore.update_information` is the only persistence writer; it atomically
updates the selected descriptor and retains project ID, managed document IDs,
resource names and bytes, dependency edges, and default configuration.

The page owns its uncommitted text and captured identity/revision. The runtime
retains a generation-bound, memory-only pending-information marker containing
that text through `stageProjectInformationEdit` / `clearProjectInformationEdit`.
The unified project/last-window close preflight and application-exit signature
include this marker, independently of all physical/resource recovery formats.
Cancel close leaves the information and text intact; Return to save navigates
to this exact project's management form. Explicit Discard and close clears the
marker only after actual project close succeeds, without writing the descriptor.
Edits made during a close review invalidate its captured signature.
Save validates
and publishes all three fields as one descriptor update; failure keeps the page,
text and original revision, with a visible error. Cancel explicitly reloads the
current persisted information without writing. Closing with changed information
requires an explicit discard-or-return decision; dismissing for a higher-priority
project review keeps the local text, and reopening the same management form
retains it. Switching to another project with changed fields first reuses the explicit
discard-or-return decision; a different project never inherits that buffer. Reopening a clean
form reads fresh information. There is no asynchronous writeback or deferred
publication after Cancel. Name is limited to 100 characters, summary and notes
to 20,000 each, following the existing store contract.

After successful publication the runtime refreshes the session's display name
without replacing its binding or generation, emits `projectInformationChanged`
and `projectsChanged`, and bound project-name views refresh. Recent-project
metadata can be reread by its existing background descriptor-only projection;
editing does not make the launcher a store owner or start another process.

The existing themed management dialog is reused; no new top-level window or
geometry policy is introduced. The form uses the existing logical Theme sizes,
plain text, native focus/editing and scrolling. Focus returns to the management
form after cancelling a discard decision; errors do not replace its fields.
Focused headless and real Qt tests live in
`tests/test_project_runtime_information.py` and
`tests/test_project_runtime_management.py`.


During application-exit review, information remains editable and updates its
pending signature. Accepting an earlier signature requests a fresh confirmation
if the text changed. Once the global exit is ready, the existing runtime
management window joins the same content-item input freeze as project windows;
cancelling restores its previous enabled state and retains the exact text. The
ready/commit signature still rejects any later pending-information change.
