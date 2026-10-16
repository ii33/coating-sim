# Unified asset browser and material editor

Date: 2026-10-16. No physical algorithm or persisted document format changes.

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
