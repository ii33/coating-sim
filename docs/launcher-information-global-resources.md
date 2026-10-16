# Launcher information and global resources

Date: 2026-10-16.

## Project information

The launcher remains a recent-project picker and open/create entry. It does not
own a project session, lease, draft, default configuration, or writable store.
New accepts name, optional summary and notes. `createProjectWithInformation`
passes them to `create_project_runtime`, then a private bounded metadata file referenced by startup arguments and the
independent runtime's `ProjectRuntimeOwner`; only that owner creates the store.
The temporary metadata file avoids OS command-line length limits and exposing notes in process listings; the runtime consumes it before admission, with client-side cleanup after a startup error as a fallback. The original two-argument creation entry remains compatible. The project name
is at most 100 characters and summary/notes are each at most 20,000 characters.
These display fields do not change physical inputs.

Recent history still persists only exact descriptor, display name and timestamp.
Its background worker reads a bounded, validated descriptor projection for the
latest name/summary/notes; no resource payload or project owner is opened. A
missing or invalid descriptor keeps the history row and its cached name, with a
hover unavailable-information warning. Recent rows show only name/actions and
summary; full read-only name, summary, notes and path are in hover details. The
former information foldout is removed. No metadata update is performed from this page;
future edits must use the existing project owner and its lifetime lease.

Creation errors preserve the dialog fields and show the error. Request admission
is asynchronous; successful admission closes the input dialog, while eventual
startup failure is visible on the launcher without deleting a created project.
Refresh results follow the existing generation guards and never replace newer
history mutations. Notes are selectable and scrollable, and popup dismissal
restores the launching control's focus. Geometry and theme follow the shared
Quick controls and window geometry policies.

## Global resources

`global_resources` is a launcher-routed global-library window, alongside Settings
and About; it does not expose standalone project editors. The global snapshot
and publication contracts remain `project_global_resources` and
`ProjectResourceExchange`. A lightweight global exchange owner holds only
short-lived catalog/plan tokens and an asset I/O budget, never a project store.
It permits read-only global/file/project/package inspection, import into global
libraries, and export to a new `.cproject`. It rejects project mutation targets.
No legacy `.coatasset` result browser is used.

The window browses categories, searches names, shows separate name/summary/notes
and dependencies, and opens the existing material manager. Import reads an
explicit local resource or `.cproject`/`.coatproj` and previews the selected
root's dependency closure and any conflicts before publication. Global import
retains the existing single-root-kind and storage-specific restrictions; errors
are visible and do not imply a partial success. Export uses a new `.cproject`
destination and refuses overwrite. Different-content conflicts require an explicit permitted
choice for each affected resource. New or identical inputs default to import or
reuse; the preview also exposes the permitted copy/skip alternatives. There is no destructive delete action.

All scan/preview/publication work runs off the GUI thread. Closing during a read
invalidates its generation and releases eventual tokens; publication cannot be
cancelled after admission and closes only after a result. Failed operations
retain their input/selection and permit retry; empty state is distinct from an
error. Refresh preserves category/search/selection when still applicable. The
window closes only its own global exchange owner and never affects a project.


Classification now contains only actual existing resource kinds, with an explicit
default and no all-categories option. The shared presentation and separate
current-project library are specified in
[scoped resource libraries](scoped-resource-libraries.md).
