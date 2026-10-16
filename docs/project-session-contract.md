# Project sessions and application lifetime

Date: 2026-10-16. Status: staged implementation contract; native desktop acceptance is pending.

The in-process routing foundation and the proposed desktop/process rollout are
documented in [Project desktop adapters](project-desktop-adapters.md). The
foundation is not evidence that the existing GUI already follows this contract.

## Ownership

The headless project store owns persisted documents and dependency resolution. A project
session owns its working document state, revisions, undo history and window registrations.
The launcher is a project-management and global-settings presentation client. Closing it
must not close project sessions or the shared assistant. The assistant is a separate,
per-user application process; it is not the owner of project documents or session lifetime.
A coordinator routes identities and lifecycle requests, not optical calculations or edits.

Each project session has an explicit project ID and canonical root. Window uniqueness is
(project ID, module type), not module type across the entire application. Reopening a
window activates that existing window. Separate projects never share dirty state, undo,
material bindings or automatic film/simulation synchronization. The same canonical project
cannot have two writable owners. A copied project with the same persisted ID must be
explicitly cloned/reidentified before simultaneous independent editing.

## Opening and default state

Empty projects are valid and open at the film designer. A configured project's most
recently successfully saved deposition configuration is the single dependency root for
normal loading, persisted atomically in the existing `default_configuration_id` field.
Ordinary process/machine/film saves do not create or select a configuration. Dependency
loading does not launch computations or open every dependent editor. Normal opening does
not replay the previous session. Recovery records are considered only after abnormal
termination, shown for review, and never silently overwrite saved documents.

A top-row module selector shares the existing operation-button row. It opens/activates the
selected module within the same project and does not implicitly replace a document. The
project name appears in the native and custom window titles, after the module/resource title.
Editors create their expensive views lazily. Global settings and the globally shared assistant
remain application-scoped.

The shared selector receives the owning navigation facade (`projectName`, detached
`projectModules: [{key, name}]`, `openProjectModule(key)`, and `closeProject()`) and the presenting
window's `currentModuleKey`. Its button resolves that key, with an explicit
unknown-window placeholder rather than a first-module fallback. The three nested
groups are film design/optimization, deposition editors, and simulations. Material
and compute-queue tools remain separated leaf actions. Grouping owns presentation
only; it never changes keys, backend ownership, or the current window's identity.
Only leaf activation dispatches, once, to that instance's facade. Browsing or
canceling does not dispatch; selecting the current module uses the existing
window-reuse path. Mouse and keyboard enter/leave the same nested menus and return
focus to the selector after cancellation. See the toolbar section of
[Project package contract](project-package-contract.md) for the visible groups.
The final separated Close Project action is a command, not a module; it does not
change the presenting window's selected module or title.

## AI, CLI and GUI boundary

All project operations use the same headless services. A mutating command supplies an
explicit project ID, operation ID and expected document revision where applicable. The
project binding is captured at admission, not inferred later from the foreground window.
Cross-project imports explicitly supply source and destination and copy resources rather
than establish live links. A response identifies the project, affected document/revision,
and structured error. Missing/closed projects and stale revisions fail without rerouting.
GUI widgets are not required to create, inspect, import or resolve a project.

## Calculations and asynchronous returns

Admission freezes a complete validated input snapshot and identifies project, document,
revision and task. Later edits do not change admitted calculations. Completion may be
stored even if the originating window is closed. A stale result may be inspected but does
not silently overwrite newer edits. Any optimization writeback checks the target revision
and is an undoable change; it cannot target the currently focused unrelated project.
Closing a client does not cancel an independently service-owned task.

## Exit and failure

Closing the launcher affects only its presentation. Closing one project checks that
project's unsaved documents and leaves other projects available. Assistant menu 'Exit
application' requests orderly shutdown of this application's managed local processes only,
including dirty-document decisions. It does not terminate arbitrary Python processes or a
remote compute service. A declined close keeps the affected session alive. Crash recovery
and registration use process identity/generation to reject stale endpoints and PID reuse.
An assistant crash must not close project windows or discard working documents.

Closing a project's last actual module window requests the same whole-project
close policy. Hide/minimize retain the view and do not end its session. Clean
close releases the session and OS ownership; dirty close offers Return to save,
Discard and close, and Cancel. Return to save and Cancel keep every provider's
draft and the final view. Return to save is navigation, not a claim that an
asynchronous or cross-provider save has completed. Reviews for different
projects are presented serially, so a second request cannot replace an unresolved
decision. New windows for a reviewing session are fenced, while other projects
remain available. Accepted Close cleanup is generation-bound and does not rely
on a visibility transition. The detailed bridge contract is in
[Project-manager launcher entry](project-launcher-entry.md#last-project-window-and-explicit-close-project-2026-10-15).

## Acceptance before desktop rollout

- Open two projects with identical module types; edits and results remain isolated.
- Close/reopen launcher without losing either project or assistant.
- Reopen the same project and module without a duplicate writable owner/window.
- Switch focus while an AI action or optimization runs; its target remains unchanged.
- Reject stale writeback; preserve the result and newer design.
- Import external dependency closure; move the project directory and reopen successfully.
- Recover abnormal drafts only after review; ordinary open loads the designated config.
- Verify shared assistant singleton and explicit exit with unsaved work on Windows.
- Close the final project view with clean/dirty/active-job inputs; verify release,
  Return to save/Discard/Cancel, two-project review ordering, and hide/minimize.

This contract does not claim these desktop/process behaviors are already integrated.

## Resource editor close decisions (2026-10-16)

Machine defaults bound on first presentation are a clean baseline; no-op field
writebacks do not create user edits. Resource close checks include native text buffers and unconfirmed Information
edits. Syntactically committable native text settles before the close signature
is captured, so modal focus-out is not mistaken for a newer user edit; invalid
text is retained verbatim without blocking the decision. Save validates inputs,
and a failed save keeps the window and text.
The ordinary resource close dialog offers Save, Discard changes and close, and
Cancel. Discard restores that editor's clean baseline only after its native
close is accepted, including embedded material edits; it never publishes files.
A pending local answer that becomes the final window close is instead subject
to the existing whole-project review, and its cancellation retains every editor.
The last-window and explicit project-close paths therefore also allow discarding
invalid uncommitted resource text without saving or normalizing it first. Internal
recovery records and generation/lease/atomic-publication safeguards are unchanged.
Recovery activation derives clean savepoints from the referenced persisted resource
and nested material, never from the recovered unsaved text or a different default
selection. This applies to both lazy and already-open editors; stale persisted
revisions reject activation without replacing the current editor.
