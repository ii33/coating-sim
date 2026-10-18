# Local refractive-index browser

Date: 2026-10-18.

## Entry and state ownership

The existing global and project `ResourceLibraryWindow` operation bar opens one
lazy, page-owned `RefractiveIndexBrowserDialog`. This is a read-only source
browser, not another material catalog or project module. The existing resource
bridge owns its lifetime and fixes the import destination: the current project
for a project window, or the global material library for a global window.

`RefractiveIndexLocalStore` is the only owner of the local source snapshot and
derived index. The UI holds only bounded search pages, one selected source,
preview points, and an ephemeral import plan. Source IDs are the core's stable
hashes of normalized relative source paths, not shelf/book/page tuples. All
classifications and source identities come from the core. Opening, searching,
reading and previewing never create a material or change an editor draft.
The full upstream snapshot and derived local index are not software Git or
static-distribution resources.

## Input and output

The source browser accepts a local store directory, text query, catalog and
classification filters, a bounded page offset, an exact source entry ID and an
explicit data-block selection. It displays the actual snapshot/version and
source provenance, original type and domain, conditions, references, original
YAML, and core-generated preview series. Source text is bounded plain text;
neither embedded HTML nor remote resources execute or load automatically.

The UI never evaluates dispersion formulas or synthesizes optical data. Each
preview keeps its original block and units, reports downsampling, and separates
unknown values from zero. A visible curve is not proof of convertibility.
Unsupported blocks remain readable, with a reason and disabled import. Nonlinear
n2 data never becomes a linear n/k dataset. Converting one explicitly selected
block produces a validated optical-set candidate; it does not join independent
blocks, fill missing k, infer film/substrate suitability, or replace a design.

## Explicit import

The first implementation imports one source/block as a separate `optical_only`
material with null source models and no inferred material role. It preserves
the converted source, snapshot, SHA256, block selection and citation in existing
source/notes fields. Its deterministic material identity prevents repeated
clicks from silently creating duplicates. It uses the existing
`MaterialLibrary.preview_import/apply_import` or scope-bound
`ProjectMaterialLibrary.preview_import/apply_import`, with an in-memory material
package. No intermediate file, hidden project store, or replacement persistence
format is introduced.

The preview freezes the source selection, source snapshot/hash, candidate and
target's existing import fingerprints. A later selection, filter, source root,
block or candidate-name change invalidates it. The visible confirmation names
the fixed target. Existing conflicts default to skip; copy and overwrite require
explicit choices. Core rechecks source and target validity before publication.
Only a successful explicit import refreshes the destination's catalog and
existing material picker. Saved source/substrate/film snapshots and any retained
material draft remain unchanged.

## Asynchronous and close boundaries

Source status/search, detail/preview, conversion and import-plan creation run in
workers. Search and selection use distinct generations; newer navigation and
Close invalidate late results. Read cancellation does not wait for the worker
on the GUI thread. Source, query and selection are never retargeted by a late
response. Loading, empty, failure and retry are distinct states, and read errors
do not masquerade as an empty successful query.

Import publication also runs in a worker, using the original project service
mutation gate or global material lock. After admission it is non-cancellable:
repeat apply, source changes, popup/window/project close and conflicting asset
actions are blocked until an explicit result. Existing unsaved material input,
pending dialogs or same-project editing buffers block import. The source read
browser itself remains available without saving or discarding those drafts.

Successful storage publication is reported separately from catalog/picker
refresh. A refresh failure never changes success into a failed import or retries
the write. A failed write preserves the current source and reports the error.
Closing the resource window disposes the browser and invalidates all reads;
it never closes another project's store or destroys a retained material draft.

## Presentation and verification

The popup uses existing theme controls, logical sizes and responsive layouts.
Search results are paged, detail/raw text is scrollable, and narrow/wide and
large-font layouts retain reachable actions. Reopening preserves the current
query and selection; dismissing returns focus to the original entry button.

Tests cover real offline store integration, empty/damaged sources, unsupported
and n-only data, read-result reordering, repeated requests, close/reopen,
cancelled previews, stale source/target, conflict decisions, cross-project and
global isolation, dirty draft preservation, non-cancellable apply, refresh
failure after successful publication, and actual QML rendering and screenshots.
Existing scoped-resource, material-editor and catalog-refresh contracts remain
applicable. Physical conversion semantics remain in the core contract and
algorithm documentation; this UI adds no numerical algorithm.

## Explicit vendor catalogs

The same popup can open one explicitly selected local vendor AGF file. The user
provides the manufacturer, catalog version and optional source URL/license note
and legacy text encoding. These are source declarations, not verified identity
or a redistribution grant. Vendor metadata remains separate from RII snapshot,
upstream commit and CC0 metadata; switching sources invalidates pending reads
and import plans. No vendor file is copied into the RII store or software Git.

The vendor adapter projects `VendorOpticalCatalog` status/search/detail/preview
and `prepare_import` into the shared view. AGF formula numbers retain their AGF
namespace. Only core-supported entries offer a candidate. Original AGF, IT and
TD remain reference material; IT never fills k and TD never silently enables a
temperature correction. Source warnings and exact provenance remain in the
candidate. The same scoped material import plan/conflict/write boundary applies.
Before preview and apply the selected file is reread and its hash checked against
the displayed source. PDF/BGF and unimplemented formulas remain reference-only;
in particular Merck product PDFs do not produce fabricated n/k curves.

Vendor catalogs exist only as bounded, read-only in-memory sessions owned by the
resource window. Closing/reopening the popup retains that session; disposing the
window requires explicitly reopening the original AGF. This is not an installed
vendor database. Only confirmed single-entry imports persist in the existing
material library. Manufacturer/version declarations, relative-air conventions,
reference temperature, uncorrected conditions and data-quality warnings are
visible. `source_inconsistent` is independent of supported conversion.

The shared `reference_sources` API supplies source metadata (including Merck)
without installing data or following URLs. Reference details are plain text.
Nonlinear n2 currently has no verified unit in the core preview contract; the UI
therefore shows original bounded numeric rows and source text only, with an
explicit unknown-unit/reference-only label, rather than a physical-quantity plot.

AGF air-relative n and air-wavelength convention is not silently treated as the
application's absolute-index/vacuum-wavelength convention. Source preview remains
read-only. Import additionally requires an initially unchecked, explicit user
acknowledgment of using the source numbers as an uncorrected reference-air
approximation. This consent is reset for every source selection, invalidates the
plan when changed, and is frozen into preview/apply and permanent provenance.
Without that acknowledgment no AGF import candidate can be prepared.
