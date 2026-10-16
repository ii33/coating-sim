# Project resource editor integration

## Ownership and document format

The project session host owns the explicit binding and one window per module. A
ProjectResourceWorkspace owns the resource service and retained per-kind GUI
drafts. The ProjectStore owns saved generations; resource services can be used
without Qt. The project material picker may read the global material catalog as
detached read-only input, but catalog browsing does not write project files.
Saving an edited global material makes a project-owned copy-on-write profile;
global originals and overrides are never changed by project editing. Project
and global records are not shared mutable objects, and one project's edits do
not alter another project's materials.

The existing project manifest remains version 1. Existing source, substrate,
mask, machine, process, deposition_configuration and optical_material documents
retain their validation and version rules. This change adds an optional
`material_profile` document kind with envelope
`{"kind":"coat_sim.project_material_profile","version":1,"material":PROFILE}`.
PROFILE is the complete existing material schema v1, including emission models,
roles, reference evidence and optical_constants. It is not an optical_material
conversion. Existing projects need no migration. Older readers reject the new
kind explicitly rather than discarding source material data. Updating optical
resources is an explicit operation; embedded source/substrate snapshots stay
immutable until the user chooses a material again.

## Persistence boundary

Logical editor handles are `project_default_directory(root, kind)/UUID.EXT`
under `project-root/data/<typed-category>`. They identify a manifest document
and do not name files to overwrite. The corresponding actual managed JSON is
`data/<category>/r00000001/<sanitized resource name>.EXT`; IDs and logical
references stay stable across name changes. Each load/save resolves the latest
manifest record by identity. Loading an external typed file copies its complete
typed dependency closure into the project before adoption and never changes
the original. Ordinary Save/Save As write managed documents; explicit standalone
export rebases the selected closure to flat UUID files.

New projects create the typed `data/sources`, `data/substrates`, `data/masks`,
`data/machines`, `data/processes`, `data/films`, `data/expression_templates`,
`data/materials`, `data/configurations`, `data/raytrace`, `data/appearance` and
`data/assets` directories, plus `objects` (private staging/retained legacy
generations), `imports`, `exports`, `assets` (large owned binary inputs), and
`results`. The full naming, collision and descriptor-last atomicity contract is
in [project-core-contract.md](project-core-contract.md). No previous object
generation is automatically deleted or migrated merely by opening a project.
Readers accept historical `documents/<UUID>.EXT` logical handles and the
current actual manifest path. Typed references remain UUID handles checked
against the manifest's kind, independent of display/storage filenames.

ProjectStore.create_document accepts validated managed resource kinds. File
references must be UUID filenames of existing project-owned documents. The
multi-document save transaction validates every draft, expected SHA revision,
typed reference and affected process runtime before publishing one complete
generation. Failed or stale transactions keep the previous manifest and GUI
drafts. Default selection can be changed in the same transaction. An external
path, unresolved filename, wrong kind or missing typed-file dependency is an
error, never a reason to consult the global library. Film and expression-template
optical-material IDs are a separate first-binding case: an explicitly selected
global optical record may be captured into that project's owned dependency
closure on save. After capture, saved dependency resolution uses only project
documents and never consults the global catalog.

## GUI lifecycle and empty projects

The existing resource editors receive a project-local service. The opening
selection is derived from the designated deposition_configuration closure,
including its process, machine, substrate, sources and masks. Initial source/mask
selection follows the first machine slot rather than UUID-sorted graph order. Empty projects
show empty/new editor state. Saving a new source/mask/substrate creates a managed
resource; saving a machine binds explicit managed children; saving a process
binds a machine and substrate. A saved process can become a named deposition
configuration and be selected as the default. The project resource bar lists
managed documents, provides import-copy/export and default configuration
selection, and supports this same workflow after reopening.

Each editor retains its working draft in its workspace when hidden. New/open
replacement, reload, overwrite and project/application close require visible
save/discard/cancel decisions for dirty content. Cancel leaves the same draft.
Reopening a module reuses its retained owner; asynchronous results and disposal
are tied to that explicit binding and cannot target the foreground project.
Global editor mode and its existing file dialogs remain available independently.
Selecting a global material for a source or substrate captures its complete
inline profile in that design; saving preserves that self-contained snapshot
and requires no later global lookup. Film and expression-template pickers may
display global optical records, but saving captures only referenced
material IDs as complete project-owned optical records, validating each
referenced set in the same document transaction. Unused global materials remain outside the project. A saved
project's resolve and export path uses only its manifest closure.

When a material profile is saved in an already-bound project, that project's
existing film workspace refreshes its picker immediately; another project is
not broadcast to or instantiated by that write. An open film draft keeps the
optical snapshot already captured for its selected layers and frozen numerical
inputs. If the same project-owned material ID changes, the open draft cannot
silently save against the newer values: it reports a stale-material conflict
and remains editable until reloaded. Explicit project material edits can affect
later reloads or re-resolution of saved films that reference that ID; they do
not rewrite an active draft or a numerical snapshot already frozen for a run.

## Errors and verification

Adapters expose errors through the existing status signals and resource-bar
status. Invalid default closure blocks project load without choosing another
configuration. Repeated import and navigation cannot implicitly replace a dirty
draft. Managed edit revisions detect same-document competing saves while an
unrelated project document transaction can proceed. Contract tests cover core
creation/import-copy/save, global catalog reads without project copies,
copy-on-write material editing, referenced-material capture, directory
creation, typed handle/path compatibility, old-project opening and external
resource import. Qt offscreen integration verifies actual resource QML,
two-project isolation, default opening, dirty replacement and reopening.

## Concrete adapter API and close/recovery transaction

`create_project_resource_view(workspace, module_key, engine, parent=None,
geometry_settings=None, navigation=None)` returns window/controller/context/component.
The controller exposes the original film workspace as project_context and the
explicit owner_binding. `dispose_project_resource_controller(discard=False)`
detaches only the disposable presentation; discard=True is reserved for the
accepted project close. All six resource windows use projectCloseAuthorized.

`close_preflight()` commits active editor focus and reports all retained dirty
owners, including hidden views and nested material edits. `freeze_editing(bool)`
blocks ordinary bound service mutations and bridge writebacks and restores prior
window enabled state. `save_all()` gathers detached candidates, validates their
hashes and complete graph, then publishes resource drafts, full material
profiles and corresponding optical resources in one generation. Only the
synchronous coordinated close-save thread receives an exception to the freeze.

Full material profile saves atomically synchronize their optical_material
representation using the exact material ID. Existing optical-only IDs are
explicit import conflicts with copy/overwrite/skip choices. Removing a dataset
referenced by a saved film rejects the complete transaction. Role-derived film,
substrate and medium picker visibility comes from the full project profile;
legacy optical-only picker behavior is preserved. Source/substrate embedded
material snapshots are unaffected until explicitly selected again.

`save_process_configuration` saves the current process draft, new typed
configuration and default selection in one generation. Invalid parameter JSON,
unsupported settings or broken dependencies keep both the saved project and
working draft unchanged.

`validate_recovery(snapshot)` validates all project identities, managed hashes,
module schemas and finite whitelisted editor data without changing state.
`restore_recovery(snapshot)` revalidates and stages explicit approved drafts,
including invalid numeric text separately from saved-format JSON. Normal open
never calls it. The coordinator reviews one explicit cohort at a time; it does
not claim atomic activation across film, resource and compute workspaces.
Recovery never writes or overwrites managed documents.

`workspace.snapshot_pending_edits()` returns the complete recovery snapshot,
including read-only capture of focused Process/Machine/Mask document buffers.
`controller.snapshot_pending_edits()` returns one optional typed `pending_ui`
record (stable field name, exact text and numeric edit flag). Neither moves focus,
emits editingFinished or evaluates expressions. Sources/substrates/material
fields already write raw text to their bridge on textEdited. Approved pending
text is replayed into its exact editor field on opening; it cannot be silently
headless-saved before opening/commit. Invalid pending numeric text blocks
preflight/save without erasing the buffer. Transient tool inputs and unaccepted
modal-dialog settings are outside persisted document recovery.

## Unified project resource operation bar (2026-10-16)

`ProjectResourceControls.qml` presents a same-kind managed-resource ComboBox,
independent New, project Save, Save Copy, Information and selective Import/Export. The ComboBox's unique selected-state
owner is `currentDocumentId`; activation requests `openDocument(id)` and refreshes
from that owner immediately, including after an existing asynchronous dirty
replacement confirmation is cancelled. New, Save, Save Copy and Import/Export
are inline when their measured themed widths fit; only overflowed actions enter
the single secondary menu, and expanding the bar restores them. Process-only
default-configuration actions retain their secondary-menu presentation.
Save Copy appends its suffix within the resource's name limit. An older resource
without a name receives a localized unnamed-type copy name only on the new
copy; the original stays unnamed and both overview and notes remain unchanged.
Global editor file operations are unchanged. Source, substrate, mask, machine,
process and material-profile editors share this control; unrelated application
windows do not invent a managed-resource selection.

Information displays name, overview and notes as plain text with preserved line
breaks, then enters an explicit edit/confirm/cancel presentation. Name is distinct
from the filesystem path; saved names are reflected in the window caption and
managed-resource picker. Global editors reuse the same information presentation
through their existing bridge and ordinary file/library save behavior. Opening
Information does not import global materials. Confirming an edit to a global
material in a project creates the existing copy-on-write draft; ordinary Save
publishes that draft only into the project. An unconfirmed edit is a separately
staged recovery buffer, never implicitly saved as document metadata; matching
identity/token recovery reopens the unconfirmed editor, and Cancel clears it.

The bar's independent New action uses the same editor replacement guard as
opening another managed resource. Optional `beforeDocumentAction(action, id)`
allows a presentation to commit its own focused expression before new/open/save/
copy; returning false prevents the action. Optional `replaceDecisionRequested`
and `confirmReplace("save"|"discard"|"cancel")` present resource replacement
separately from window close. `extraTransferActions` and
`extraTransferActionRequested(key)` retain module-specific format or result
exports within the one Import/Export menu; exporting results does not imply
saving input resources or vice versa.

`ProjectResourceTransferController` is a draft-independent Qt facade over
`ProjectResourceService.inspect_resources`, `preview_resource_transfer`,
`apply_resource_transfer`, `cancel_resource_transfer` and
`release_resource_catalog`. Its presentation properties are `transferCatalog`,
`transferPlan`, `transferMode`, `transferStatus`, `transferActive`, `transferBusy`,
`transferCancellable` and `transferTimings`, notified by `transferChanged`. Editor
resource lists continue to use the independent `changed` notifier. `beginTransfer(mode)`, `inspectTransferSource(source)`,
`previewTransfer(selected_ids, target)`, `clearTransferPreview()`,
`applyTransfer(decisions)` and `cancelTransfer()` are the concrete Qt boundary.
Locations are `project`, `global`, or a local filesystem path/file URL. Imports
target the current project. Exports target the global library or an explicitly
chosen `.coatproj`/`.cproject` file. Browsing project files inspects categories and
resource entries; it does not replace the current project or import the package
wholesale. A changed source, selection or destination invalidates the prior plan.

The same Import/Export menu retains “资源文件及依赖…” via the existing
`exportDocument(directory)` slot and a folder chooser. This exports a saved
resource and its complete dependency files without changing the working draft.
Presentations with binary input assets explicitly expose
`canExportDocument == false` until that independent format can preserve the
binary closure; their complete project-package exchange remains available.
Module-specific material formats and standalone film/result exports remain
labelled secondary items in this menu.

The source catalog is read-only and cross-category selection chooses transfer
roots. The preview displays the dependency closure as automatically included,
read-only rows. Conflicting entries require explicit supported decisions and
never silently overwrite a destination. Empty catalogs, inspection failures,
invalid selections and apply failures remain visibly distinct. Only Apply
publishes the selected target transaction. Inspection, preview and Cancel do not
write the source/target or adopt, save, discard or replace the current editor
draft. Successful import refreshes available managed-resource lists, while the
active editor retains its identity, focused data and loaded revision. Catalog,
preview and apply run on pure-Python workers; an owner-bound main-thread QTimer
accepts results. Slot booleans acknowledge admission, not completion. A busy
operation disables duplicate submissions. Read/preview cancellation invalidates
its generation and releases any late read tokens without publishing a target.
Apply is non-cancellable after admission, retains its QObject owner until an
explicit `transferFinished(success)` result, and never reports an admitted or
already-published transaction as cancelled. QML owns presentation only.
Inspection, preview and apply also publish
`transferOperationFinished(operation_id, kind, success, error)` on the owning
thread; `transferOperationId` identifies the admitted foreground generation.
Observers must match that ID rather than interpreting `transferBusy == false`
alone as success. Cancel invalidates a read generation; its late tokens are
released without emitting completion for the newer operation.
Source
revalidation and merge occur outside the session-host lock; final CAS/publication
keeps the existing owner/gate protection. Small/large timing and main-loop
responsiveness evidence accompanies the directed UI tests.

### Film companions and deposition simulation inputs

`ProjectFilmResources` and `ProjectDepositionResources` are disposable
`ProjectResourceTransferController` facades, defined in
`ui/project_simulation_resources.py`. They retain their exact project binding
and reuse the existing project film editor or compute draft. They own only
transfer presentation and one pending replacement request, never a second
physical/numerical input buffer. `replaceDecisionRequested()` and
`confirmReplace("save"|"discard"|"cancel")` apply to New/Open, separately from
window close. The resource picker always reads `currentDocumentId`; Cancel
changes neither input nor selection. Save failure keeps the replacement pending.
`beforeDocumentAction(action, document_id)` commits focused editor text before
New/Open/Save/Copy. Busy/frozen input owners reject replacement.

The film designer, thickness optimizer and spectrum simulator show the same
project-owned `film` documents, using `ProjectFilmWorkspace.editor` as their
only state owner. Opening in any of these views replaces that single editor,
with the normal dirty decision, and does not create another window. Save writes
the complete `.cfilm`, including accepted optimization and simulation workflow
settings. Save Copy creates a new managed document and adopts it only after a
successful transaction. Imported resources only refresh choices and do not
replace or save the working film. Optimizer candidates remain explicit result
actions; the ordinary Save button saves the current film input. Spectrum
CSV/PNG exports are labelled result actions in Import/Export.

The deposition simulation bar reuses the existing managed
`deposition_configuration` list and its bound process. New starts an unsaved
configuration with default simulation settings while retaining the explicitly
selected managed process; an empty project still requires selecting/creating a
process before Save is enabled. Save and Save Copy use the existing cdep
validation, revision and process closure APIs. Naming, default selection and
result PDF export remain explicit. Saving a cdep does not include simulation
result arrays or artifacts.

The geometric ray-trace and appearance bars use the managed `raytrace_request`
and `appearance_configuration` kinds. `ProjectManagedSimulationResources` in
`ui/project_raytrace_resources.py` delegates asset capture and validation to
`save_simulation_resource`; `.cray` and `.cappearance` payloads reference owned
`binary_asset` dependencies rather than embedding result arrays. Ray requests
retain the existing window-document schema. Appearance stores only its frozen
input context, render draft, backend, precision and thickness-mode choice; a
new configuration without an asset is explicitly `context: null, draft: {}`
and cannot render until an input is selected. Result JSON/PNG actions remain
separately labelled. The saved input never implies a saved result.

Large input Open/Save/Copy/Restore run on workers. A returned boolean means
admission only; `resourceOperationId` and
`resourceOperationFinished(request_id, kind, success, error)` identify actual
completion. Only the exact pending save request can resume a dirty replacement.
Failure, cancellation, newer edits and reentrant completion handlers preserve
the current draft and picker. Open/Restore may be cancelled; an admitted Save
or Copy retains its owner through publication, including forced window teardown.
The compute workspace registers the disposable facade through the Qt-free
optical-draft provider protocol; held-window reopen retains its identity,
whereas abnormal recovery creates the established unsaved copy. Staged,
unconfirmed Information buffers are recovered separately and never silently
applied to input metadata.

Confirming an external deposition-asset or SPD file picker is an explicit
input import. The worker calls `capture_simulation_asset` to publish/reuse an
owned `binary_asset` before adopting its path in the draft; the configuration
remains unsaved. Removing the original file then cannot break first Save or
recovery. Cancel before publication leaves no input; cancellation that loses
the publication race reports the completed import and retains the reusable
project asset. Cancelling configuration edits does not garbage-collect an
already imported input. SPD parsing and byte capture also run off the GUI thread.

Film/deposition resource-file export uses the existing complete dependency
export service. Optical inputs use the full selective `.cproject` exchange
route until their separate folder-export action has the same asynchronous
owned-binary closure guarantee. Queue, settings, about and launcher windows own
no design input and do not show an invented resource selector.


## Unified resource Information editor (2026-10-15)

The shared bar's Information action is available in project mode and, as an
information-only bar, in independent global editors. `resourceInformation`
contains name, summary, notes, editable_fields, name_limit and a token tied to
the exact resource identity/current metadata. `updateResourceInformation(name,
summary, notes, token)` updates the existing editor draft only. Name is distinct
from the filename; summary maps to description and notes to notes for the six
resource editors. Film uses name/summary/note, as described below. No information
edit implicitly saves a project or a global file. Material editability delegates to the existing bridge: built-in global records
retain reversible overrides, and a project editor retains its existing
copy-on-write draft/save behavior without a preliminary copy action.

The dialog opens in viewing mode, then edits local buffers. Explicit Confirm
updates the model; Cancel/Escape clears the unconfirmed buffer. A metadata-token
conflict preserves the current model and requires a fresh review. New inline
metadata controls are not duplicated in the design form. Older approved pending
`processNameField` recovery text is migrated intact into the dirty name draft;
other geometry/dependency pending fields retain exact focus/text replay.

`stageInformationEdit` and `pendingInformationEdit` retain an unconfirmed
name/summary/notes buffer separately from the model. Project recovery journals
this optional `pending_information` with resource identity, metadata token and
original model dirty state. Recovery validates its identity/token and limits;
only a matching resource restores it into the Information editor in unconfirmed
editing mode. Ordinary Save and headless Save All are blocked until that buffer
is explicitly confirmed or cancelled. Cancelling a recovered buffer restores
the pre-buffer model dirty state and never writes the buffer to a saved resource.
