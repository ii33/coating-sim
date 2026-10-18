# Current project editor snapshots

Date: 2026-10-18. Read-only interface; no numerical, document-format, or editor-write change.

## Input, ownership and routing

`project_editor_snapshot(project_target, window_type, detail="summary", offset=0,
limit=50, expected_snapshot_id=None)` requires the exact six-field authenticated
`project_list` target. All registered project modules have an existing-owner
read adapter. `summary` is accepted for every module; the additional collections are:

| Module | Detail collections |
| --- | --- |
| film_design, film_simulation, film_optimization | layers, materials, objectives, variables |
| source | apertures, materials |
| substrate | points, materials |
| machine | sources, masks, monitors |
| mask | contour_points |
| process | steps, sources |
| main | none |
| materials | catalog, optical_sets |
| ray_trace | samples, surfaces, spectrum, layers |
| film_appearance | captured_steps |
| compute_queue | jobs |
| project_resources | resources |
| project_bbs | summary only |

Unsupported module/detail pairs fail closed; a detail accepted by another module
is not an implicit generic document accessor. The returned `available_details`
lists the additional adapters' exact readable scope. No write capability is added.

offset is a nonnegative strict integer and limit is a strict integer from 1 to
100. Extra arguments and incomplete targets fail closed. Existing read_window,
global windows and standalone contracts do not change.

The session owns committed in-memory film content, dirty/revision and captured
material values. An existing bound optimization controller owns its current
objectives, variable constraints and solver/structure configuration. An existing bound simulation controller owns its real exportSimulationConfiguration parameters, light source, plot/analysis and export settings; its exact _source_session, source physical revision and design identity must match the editor. Reads use
only the registered workspace's existing `_editor` for film modules and existing
registered controllers for other modules. They never construct workspaces/editors/controllers, open windows,
commit native fields, save documents, start calculations, or refresh catalogs.
Material information comes from the current draft's retained/frozen material
snapshots, never a refreshed global picker or a saved-file substitute.

## Consistency and bounded output

The authenticated owner dispatches on the Qt GUI thread. A single session lock
captures its snapshot and compact material projections; optimization export is
read synchronously on the same GUI turn. The editor revision is checked again
before returning. `snapshot_id` hashes the exact draft identity/revisions,
physical design, captured compact material metadata, and current optimization
configuration, including the complete real simulation configuration when its window exists. A changed simulation setting invalidates every page even when the physical design revision is unchanged. It is an opaque string, distinct from integer revision fields.
Pass it as `expected_snapshot_id` on subsequent pages. A changed ID raises
`stale_snapshot`; no historical snapshot is retained and pages never mix silently.

Every top-level pageable collection, including summary previews, has `offset`, `limit`, `total`,
`next_offset`, and `truncated`. Detail pages project a single collection. Summary
includes bounded previews of each collection and scalar design/solver settings.
Layer summaries use existing derived physical thickness only; no optical lookup
or numerical evaluation runs. Missing thickness/material/n/k/domain information
remains unknown. Missing k samples never establish a lossless material.
Nested angle/solver lists are bounded projections and set content_truncated when
clipped; they do not claim a separate pagination API. Full wavelength/n/k arrays,
arbitrary extension objects, candidates and job-result payloads are not returned.
Film layer free-form notes remain outside the existing film projection; selected
resource document descriptions/notes are readable bounded user data. Strings and nested settings are bounded and truncation is explicit;
the final UTF-8 JSON response is limited to 128 KiB, otherwise unavailable.

## Unavailable, errors and cancellation

A closed requested window, absent editor/controller, initialization, current
native text/IME draft, pending confirmation/information edit, stale optimizer
source, busy controller, ownership transfer or close/exit/compute freeze returns
`available:false` with an explicit reason and no current-document payload.
Identity/generation mismatches remain errors and never rediscover another owner.
This interface has no asynchronous work or cancellation action. It does not use
saved inputs when a live snapshot is unavailable. Ordinary read_window remains
identity/status only. Editor mutation and computational confirmation boundaries
are unchanged.


## Other existing project owners

Resource adapters read only the registered `ProjectResourceController` and its
existing bridge. The host/binding/store, resource controller registry, registered
window and controller window must agree. They do not require or construct a film
editor. Source/substrate raw field strings, machine installation rows, mask
contour points, process steps/environment and selected material model fields
come from current in-memory state, including invalid strings and unsaved changes.
Source emission/preview rate strings and material-editor model-rate strings retain
the actual UI display unit, angstrom/second, even where legacy field names end in
`nm_s`; `field_units` explicitly labels these values. Captured embedded material
model defaults retain their persisted nm/second unit. Reads never run the export
boundary's conversion or silently mix these two representations.
The projection scope is explicit: primary editor fields and named controls,
not every transient tool panel or viewport decoration. Existing mask transform,
rotation/analysis controls and substrate point-entry controls are projected
separately from applied document geometry; reading never applies them.
Cached validation/status fields describe the editor; no validation or geometry
sampling runs during a read. Cached resource handles contribute a lexical
filename and a full-path digest to document identity; same-named identical
resources remain distinct without path resolution or currentDocumentId lookups.
Names, descriptions and notes are user document
data, bounded like other text. Credentials, network configuration and arbitrary
extension objects are outside these explicit projections.

Materials come from already-captured source/substrate snapshots or the existing
material editor's `_draft`, `_entry_cache` and `_optical_sets`; no catalog refresh,
material normalization, curve request or optical-preview cache write is allowed.
Optical datasets expose metadata and sample counts, not full n/k tables. Unknown
k remains unknown. Complete retained optical values bind the internal signature,
so same-length changes to an omitted sample invalidate later pages too.

Main reads the existing pure deposition draft provider and current input IDs;
it does not resolve the selected process or recalculate timing/estimates.
Ray-trace reads its raw request/cached parse and projects only known request
fields; it never calls parsing properties that modify error state. Its samples,
spectrum, surfaces and coating layers have independent pageable collections.
Appearance reads only the already-existing renderer controller and its immutable
launch context/current draft. The frozen upstream context is identified by its
asset/process hashes; it is never presented as a live design. Light spectra expose
sample counts/domain and content-sensitive hashes. Missing appearance context is
unavailable and cannot trigger controller construction or asset import.

Queue reads only the existing view's loaded cache and active tab. It never
refreshes the queue, queries the scheduler, moves expired cached rows, opens a
result or downloads artifacts. The `cached_view` scope and loaded-count/has-more
metadata distinguish the UI's loaded rows from the persistent queue. Recent
rows follow the same pure time-cutoff filter and visible-row cap as the view.
No loaded page means initializing; existing cached rows can explicitly report a
refresh in flight. Job payloads, results, artifacts and other projects' rows are
not emitted. Changing displayed rows, tabs, visible cancellation states or status invalidates the snapshot.

Open native text/IME edits, staged information and pending file/replace decisions
remain unavailable without committing focus. The dispatcher inspects only
already-existing modal descendants of the target window: secondary edit,
replace/import/delete/close and transfer/configuration decisions are unavailable
even when their controller has no pending marker. Read-only resource information,
material reference details, history and CAD reports remain readable; the materials
window's primary editor Popup is not a secondary dialog. Unclosed mask drawing contours,
visible optical-data subeditors and appearance camera gestures are distinct
uncommitted UI owners and return explicit unavailable reasons. Busy transfers,
loads and computations are never silently represented by an older saved draft.
The read has no cancellation, mutation or asynchronous completion callback.

New adapters hash the complete logical state using incremental hashing; pagination
is applied only after the state signature is captured. Public strings/nested
values remain bounded, omitted arrays set truncation metadata, and the same
128 KiB response ceiling applies. An unavailable response contains no prior
snapshot payload. Existing film behavior and editor-write whitelists are unchanged.


The project resource library reads only its already-loaded catalog of saved
current-project resources, with category/selection and bounded resource
information. Its scope is `loaded_saved_project_resources`, not unsaved editor
content or an automatically refreshed store. An external/global import source
returns `external_import_source`; no other source is substituted for the current
project. The window has navigation and read-only context capabilities, with no
editor update/action whitelist. See [scoped resource libraries](scoped-resource-libraries.md).

## Persistent BBS window summary

`project_bbs` is a managed per-project window. Its summary reads only the existing QML view: selected topic ID/revision/title, type/search filter, page position, history visibility, and unsaved composition mode/target/revision/title/body length. Titles and queries are bounded; body content, local paths and attachment bytes are not returned. This status-only projection remains readable while typing without committing or moving focus. It has no generic editor mutations. The existing exact-binding, registered-controller, lifecycle and snapshot-id checks apply. Closed windows do not create or load a BBS view. The state remains owned by the runtime BBS coordinator and the managed project service.
