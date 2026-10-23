# Project film/session integration

Date: 2026-10-15. This opt-in slice connects managed films, one project-owned
working designer, and the existing Qt Quick film view. It does not replace the
application launcher or implement a cross-process coordinator/shared assistant.

## Ownership and interface

`ProjectFilmService` owns managed film/template persistence, immutable optical
snapshots and expected saved revisions. Its optional session binding is checked
inside `ProjectSessionHost.operation_scope(binding)`, which serializes admitted
service operations against project close. Old generations and ordinary new
admissions while closing fail rather than writing through a retained store.
The bound film service permits the active close-policy thread to finish its
explicit saves; another thread cannot use that exception. Numerical inputs are frozen by the existing
film service; no numerical algorithm or file format changes in this slice.

`ProjectFilmWorkspace` owns one `ProjectFilmEditSession` and service for an explicit
project binding. `FilmDesignQuickController(edit_session=..., project_context=...)`
observes that same session; creating another presentation does not recreate the
draft. A project save creates a managed document for a new draft, then updates
that document with a loaded revision. Saved-film selection retains its managed
handle. External `.cfilm` import is explicit and copies the dependency closure
through the project store; absent/conflicting optical resources fail visibly.
An explicit `materials` map can be supplied to the headless import API. No
fallback to an installation catalog or SQLite is permitted.

When the selected deposition configuration contains an inline frozen film,
the caller may pass its validated `film_design_snapshot` to
`workspace.initialize_frozen_draft(snapshot)`. Only a pristine, never-edited
workspace editor adopts it. The result is a dirty, pathless film draft; its
frozen calculation settings become the simulation page configuration. The
film and optical records are detached from the opening plan. The draft's
immutable optical provider is local to this workspace service, so editing and
simulation use the captured values, including where an existing project
material has the same ID. Other project materials remain selectable where
their IDs do not collide. Repeated activation returns false without replacing
the draft, and the adapter never infers a link to a saved film from matching
names or IDs. No disk write occurs until an explicit save. The first save
passes the frozen material closure through managed `create_film` in one project
transaction; a same-ID, different-content material fails visibly without
changing the manifest or overwriting either material. Selecting another film
or making a new draft releases the frozen provider.

`ProjectFilmDesktop` is an opt-in Qt adapter. The headless host publishes a
(project, module) reservation before construction, deduplicates repeated opens,
attaches only successful windows and releases accepted closes/failed opens. It
supports film designer and film simulation only. Project-mode simulation saves
through the same working designer owner and does not expose standalone load or
optimizer actions. The project name appears in both windows' native and custom
title bars; their toolbar rows retain module navigation without reserving title space.
Repeated navigation only
activates a window, preserving its design and scroll/focus state. Simulation is
bound to the same project's designer; foreign sources are rejected. Other
application modules remain unavailable through this adapter.

## Confirmations, errors and asynchronous boundaries

The edit session owns discard/import/close decisions. Dirty replacement is
confirmed before importing or changing selection; cancellation preserves both the
saved manifest and draft. Import material errors leave the active draft unchanged.
An accepted designer presentation close releases its window registration and
marks its controller closed; the workspace keeps its in-memory working session
while that host is running. The project-mode close prompt states that unsaved
work is not persisted across program exit. Reopening inside the same live host
observes the same draft. Explicit project close must supply a complete close
policy to the headless host; the desktop adapter does not invent that policy.
The normal old standalone entry is unchanged. The optional
`python -m coat_sim.ui.project_film_host --project PATH` entry uses Qt's normal
last-window process exit; it does not implement the planned independent project
host/launcher lifetime. Use the adapter inside a live host to preserve drafts
across presentation close/reopen.

Service operations revalidate generation on entry. The host lock is held through
persistence so a concurrently accepted project close cannot precede a queued
write. Work admitted earlier may complete; project close waits for that operation
boundary. Simulation uses existing frozen-input execution and stale-result rules.
This adapter creates neither independent background service tasks nor recovery.

## Save work and generated-design identity

An ordinary managed Save re-reads current project and global material sources
before checking the draft's captured records. A same-ID content change remains
an error and preserves the dirty draft. Unchanged immutable-owned optical
records can be shared internally between providers; public material reads and
picker arrays remain detached. A no-op material refresh preserves provider
identity, so Save does not rebuild the complete picker or derived rows before
writing an unchanged material view. After publication the service refreshes
project-owned resources and role usage using the global snapshot already read
for the operation; the next Save or explicit refresh reads the global source
again. No optical sample, dataset, workflow setting, or precision is dropped.
The project store still owns full validation, stale-write checks, and atomic
publication. These changes do not introduce a background save or an early
success notification.

“Apply and replace current design” keeps the editor's existing internal design
ID and managed handle, including a pathless draft's identity. It replaces the
layer stack with new layer IDs. Only New, Import, or Save Copy creates a new
managed document. A generated preview is invalidated when its material provider
changes, even if the physical design revision did not change. Generated QW
thickness and subsequent numerical work use the same pinned material values;
selected dataset IDs are never substituted. Applying captures every newly
referenced material. Its single-step undo restores the prior design, path,
dirty state and material overlay, including a frozen default draft's complete
first-save closure. An updated external material after apply is rejected on
Save rather than silently changing the generated design's source data.

## Verification scope

Contract tests cover independent same-ID optical records in two projects,
relocation/import copy, revision rejection, project session admission, managed
save/new/selection/import cancellation, retained drafts, and singleton routing.
Offscreen Qt tests load the real film QML, exercise toolbar navigation and
same-project simulation isolation, and reject foreign companion sources.
They do not establish Windows desktop acceptance, cross-process singleton,
launcher/assistant lifetime or application exit behavior.

## Scoped film saves (2026-10-23)

`ProjectStore.save_film_document` is the dedicated atomic boundary used by
`ProjectFilmService.create_film` and `save_document`. It strictly validates the
changed film and every referenced project optical material/set. A save cannot
change the film's internal ID, bypass its expected revision, modify an existing
material, or ignore a broken dependency in the film's own closure. Explicit new
materials are captured in the same transaction.

Unrelated source/process/configuration documents are not re-normalized or
migrated. Their existing manifest records, paths, hashes and bytes are retained;
retained JSON is still subject to aggregate capacity, ordinary-path and digest
checks. A historical source rejected by the current source schema therefore
cannot block an unrelated film edit, but remains invalid for source/process
calculation. Unknown fields in the changed film or its required material records
still fail. Publication uses the existing exclusive project writer and
immutable-generation transaction, with the descriptor committed last. Failed
publication preserves the saved film, original input bytes and editing draft.

This is not a tolerant project opener or a source migration. Opening a project
still validates its selected default process closure. If an obsolete source is
part of that closure, a successful film save does not make restarting/opening
that process valid. Keep the active editor and independently verified current
recovery checkpoint until the source/opening issue is resolved explicitly;
never assume an older checkpoint includes subsequent edits.
