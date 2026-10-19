# Unified asset browser and material editor

Date: 2026-10-19. No physical algorithm or persisted document format changes.

The existing global/project library window becomes the asset-library entry. Its
left-hand search, actual categories and material-use filters lead to selectable
asset cards; the right-hand pane presents resource information or the existing
material editor. Material datasets, n/k imports, dispersion models and source
models continue to use MaterialLibraryDialog and its original controller.
Embedded presentation hides that dialog's duplicate browser and destructive
legacy material-delete action. Asset deletion is a separate preview/confirmation
operation, scoped to the loaded owner, with reference blockers and recoverable
storage. Browsing never deletes or migrates user data.

A project retains exactly one materials controller in its resource workspace.
Opening the compatible materials module normally activates the asset window and
its materials section. Existing live legacy material windows are activated rather
than migrated; their pending input is not replaced. The public logical materials
target and its field/action whitelist remain unchanged. Its snapshot and guarded
operations resolve the existing controller inside the physical asset window,
with all binding/store/window/instance/revision fences retained. Physical window
lists contain one asset window; an asset-window target alone grants no material
write capability. Discussion mode and authenticated six-field routing stay intact.

Global material edits own the existing global MaterialLibraryBridge. Project
material edits use a project-only catalog provider and write only through their
existing ProjectResourceController/ProjectMaterialLibraryBridge. Asset rows do
not import or select global records implicitly. New materials are created only
by an explicit New action. Refresh does not replace dirty material input.

The window follows shared theme, operation-bar and geometry policies. At normal
width the browser and detail/editor panes are side by side; narrow windows stack
them with scrolling. The original material data editor remains responsible for
its nested dialogs, dataset validation and pending-change decisions. Project
close and application-exit preflight keep the retained material owner; local
view close offers the existing save/discard/keep/cancel boundary. Failed loads
and writes preserve input and show an error.

## Asynchronous recycling and recovery

The scope-bound bridge exposes previewAssetDeletion(single_id_list),
confirmAssetDeletion(), cancelAssetDeletion(), loadAssetDeletions() and
restoreAssetDeletion(receipt_id). Slot booleans acknowledge admission only;
delete-preview/history/finished signals report worker delivery. Preview and
history are cancellable reads; closing their view invalidates pending results
and cancels any returned plan. Apply/restore are non-cancellable after admission
and block view/project/application close until an explicit result. No operation
has a permanent-delete fallback. The displayed opaque plan fixes its owner,
catalog, selected asset and components; changing the selection does not retarget
an existing confirmation. Core revalidates references, versions and ownership.

Uncommitted material forms and same-project resource buffers block recycling.
The embedded clean material detail is registered through the exact-owner
service binding; it is not treated as another editing window. During apply and
restore, existing same-project module content is temporarily disabled, the
project material setter is frozen, and project window/management writes and new
module opens are rejected. Global library content is disabled during its own
publication; there is no new global assistant editor route. Cancellation or
failure retains the original material selection/input. A committed recycling
operation closes only its affected clean material detail, then refreshes; a
refresh error is reported separately from the completed storage transaction.

History lists only this scope's receipts. Only recoverable/active receipts offer
a separate explicit restore confirmation. Interrupted/uncertain/restored entries
remain diagnostic, not a claim that an asset was successfully removed. Recovery
never overwrites a recreated/conflicting asset. Standalone global binary inputs
are currently unsupported, with a visible explanation and unchanged files.

Project optical data without a material profile is a clean, editable projection
of that exact saved optical asset. Browsing does not create a profile or dirty
the project. Its separate source-asset identity participates in both catalog and
logical materials snapshot signatures; `_saved_id` remains unset. An actual edit
uses the original optical revision CAS, and explicit Save atomically creates the
material profile while updating that optical component. Existing recovery uses
an unsaved profile draft plus its original selected revisions, with no schema
change; restoring it strictly re-derives the optical origin from that owner.

## File imports, categories and diagnostic catalogs (2026-10-19)

`ProjectResourceTransferController.beginFileImport(str fileUrl) -> bool` admits
the explicitly chosen local file and inspects it directly. Cancelling a picker
does not start a transfer or read the global library. `transferFileFilters` names
current `.coatproj`/`.cproject` projects and the existing typed design suffixes;
`transferKinds` remains the designer's authority for visible/selectable types.
Library imports retain the one-category selection fence. The controller owns
async generations, cancellation and late-result token cleanup; QML owns only
picker/preview visibility and selection. Inspection errors preserve files/drafts.

Catalog `warnings: list[str]` contains the source path and validation failure of
unreadable entries and dependent entries excluded from the detached catalog.
These are partial-read diagnostics, not migrations: no damaged file is rewritten,
deleted, converted, or accepted under an older schema. Capacity/path safety
failures still fail the read. Publication and numerical inputs retain strict
validation. The bridge ends loading on success, partial success and failure and
sets `libraryMessage` to a count or diagnostic, never a stale reading message.

Material categories/name pools are `film` and `substrate`. Stable opaque material
IDs remain independent, including `builtin_ideal_air` (film) and
`builtin_ideal_air_substrate` (substrate); both are ordinary optical-only Air
records with their own owned data. Names compare trimmed/case-insensitive only
within the writable owner's category. Project film bindings still use project
material IDs and owned optical sets, never display names/global lookups.
Historical payloads remain unchanged. Records without explicit film/substrate
membership (including medium-only and source-container records) do not appear in
either pool. They are not renamed, moved, hidden behind a replacement category,
or deleted by this projection fix. Separately, explicit user confirmation on
2026-10-19 removed the ten builtin source-container seed records themselves;
they are not moved to another pool/library and cannot be regenerated from the
remaining seed. Original literature, independent films/substrates and user files
are preserved; see the material-library contract's confirmed removal section.

`material_categories: list["film" | "substrate"]` on material-profile catalog
rows is the browser/name-pool authority. Dual-purpose records match both filters;
an empty list matches neither. Scalar `material_category` is the first membership
or `null` and must never be treated as an implicit film classification.
`material_uses` retains declared picker usages (`medium`, historical `other`,
`unspecified` included); it is not the classification list. Each Air stays in its
own category. Film Air additionally declares medium usage; substrate Air does
not automatically enter the medium picker.

`ProjectFilmService.picker_material_options_many(purposes, *, design=None)` uses
catalog roles plus actual references in that design. Frozen numerical closures
retain complete same-ID values without automatically granting every picker
purpose. The medium picker lists declared media plus current incident/exit
bindings, preserving valid immersion bindings by ID. It does not concatenate
all film and substrate options. Empty new substrate drafts stay unbound; saved
substrate references are loaded unchanged. See
[sourced substrate presets](optical-substrate-presets-contract.md).


## Assistant-only offline reference access (2026-10-19)

The asset operation bar no longer exposes the refractive-index database action,
and the window no longer instantiates its source-browser page. Existing material
editing/import and project/global ownership remain unchanged. Offline research,
material reference datasets, packaged/installed RII and existing CIE tables are
discovered through the assistant's unified library tools. Settings has no
external-research-root field. Legacy private configuration and user research are
retained with their original authorization; the public provider never scans them.
Source-specific availability, rights, hashes, units and errors are defined in
[the unified offline contract](unified-offline-library-contract.md).
