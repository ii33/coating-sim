# Material library transfer contract

## Scope and owners

MaterialLibrary in coat_sim/core/material_business.py is the single owner of persisted effective records, builtin override files, package validation, import plans, and transaction rollback. MaterialLibraryBridge in coat_sim/ui/source_materials.py owns asynchronous operation state and publishes results to QML. The QML page owns the visible edit draft and bulk include/exclude selection. A file operation never mutates that draft while it runs.

The standalone/global library is the authoritative user catalog at the application data root and is read-only when viewed from a project. Project material views combine detached global records with project-owned material profiles; a project record shadows a global record with a case-insensitive matching ID. Catalog reads never populate a project. Selecting a global record for inspection, exporting it, or cancelling its edit leaves the project manifest unchanged. Saving edits creates or updates a project-owned copy with the same material ID; it never writes the global record or global builtin override. Editing a project-owned copy is copy-on-write and is isolated to that project. A second project has a separate ProjectStore and independently owned value copies, even when the same ID occurs in each project.

Project material edits atomically maintain the matching project optical-material representation. Existing source and substrate records retain complete inline material snapshots; they remain self-contained when saved or exported and do not resolve by consulting the global catalog. Film designs and expression templates may display global optical records as read-only picker inputs. When such a design is saved, only the referenced material IDs are copied into that project's typed dependency closure, with complete optical records and validation that every referenced set exists; unrelated global catalog entries are not copied. `resolve_document` and project export follow project manifest dependencies only. A later global edit or removal cannot change the captured project optical record. An already-open film draft also keeps the values it captured for its current layer and calculations; attempting to save after a same-ID project material changed is rejected until the design is reloaded. An explicit edit to a project-owned optical record can affect later reloads/re-resolution of films that depend on that ID, but never rewrites an already-open draft or a numerical snapshot already frozen for a run.

Built-in originals stay in the packaged catalog for the standalone/global library. Its historical `.overrides/<material-id>.json` behavior remains available only in standalone/global mode. Project edits of a builtin are project-local profiles and do not create or remove a global override. Previously saved source and substrate records remain inline value snapshots; changing or restoring a library profile does not rewrite them.

## Package input and output

- Single material: .cmat, JSON envelope { "kind": "coat_sim.material", "version": 1, "material": <profile> }.
- Library or selected subset: .cmatl, JSON envelope { "kind": "coat_sim.material_library", "version": 1, "materials": [<profile>, ...] }.
- Records use the existing material profile schema and unit conventions. No executable fields are interpreted.
- Import rejects packages larger than 64 MiB, more than 1000 records, duplicate JSON object keys, non-finite numeric values, repeated IDs, invalid paths/structure, unsupported versions, and profiles rejected by validate_material_profile.
- Reference text is kept as supplied. The importer neither checks a citation online nor rewrites it.

## Calls and results

- export_single_file(id, path) exports one current effective profile.
- export_library_file(path, include_ids) exports exactly the provided IDs. UI selection begins with all entries included; select all, select none, and per-row checkboxes update that one selection state. The count is shown before choosing an output path.
- parse_package_file(path) returns the parsed {kind, records} envelope after bounded strict parsing.
- preview_import(package) returns {rows, plan} without writing. Each ID conflict is shown before apply; choices are skip, copy, or explicit overwrite. Non-conflicting rows are imported as new records.
- apply_import(plan, decisions) rechecks the captured target fingerprints, stages all changes, commits them atomically, and rolls back if any replace fails. It returns created/copied/overwritten/skipped counts. Stale plans and write errors fail without partial library changes.

The MaterialLibraryDialog runs catalog reads, package parsing, export, save, restore, delete, and import apply through MaterialLibraryBridge worker threads. The global editor retains its legacy synchronous save() and confirmDelete() methods for older callers; this page does not call them. A closed import preview invalidates its generation and ignores a late parse result. Apply is not cancellable after commit begins; its Cancel action is disabled while applying. Failures stay visible in status/error text and preserve the current material draft. Successful import refreshes the selected material only when that draft was clean; an unsaved draft is retained. New, Save, import, and export live in the top action bar; refresh is adjacent to the category filter; no explicit "copy as user material" or "close and keep draft" action is shown. The enclosing window owns close/save/discard/cancel behavior. There is no explanatory status row beneath the action bar.

`MaterialLibraryBridge.requestOpticalSetCurve(id) -> int` issues a curve request and returns its generation. `opticalCurveReady(str id, int generation, object points, str error)` reports only the latest request for that same ID and the current immutable record snapshot. A newer request replaces the older request for that ID; requests for other IDs remain independent. `cancelOpticalSetCurve(str id, int generation) -> bool` cancels only the matching latest request. Switching material, replacing its draft, editing/removing an optical set, or closing the page invalidates pending curve generations across the current material.

The bridge exposes the read-only Qt property `opticalSetDataRevision: int` with `changed` as its notify signal (`@Property(int, notify=changed)`). It advances when a material draft or its optical-set records are replaced or edited. The page uses this revision to discard completed points tied to an older record snapshot and re-request curves for still-expanded IDs. This is a data-validity boundary; a matching ID alone does not make cached points current.

The page owns `expandedOpticalSetIds` and the per-ID curve state (`points`, `loading`, and `error`), keyed by optical-set ID so completed curves and expansion survive Repeater delegate recreation. Closing cancels outstanding requests but retains expanded IDs; reopening requests the current records again. Switching material clears expansions. IDs absent from the current `opticalSets` model are dropped. New, Save, import, and both export actions share the top action bar; refresh sits beside the category filter and is disabled while the bridge is loading or applying an operation. There is no separate copy action; in project mode, saving edits to a global material creates the required project-local copy.

The QML editor entry point `openSetEditor(identity)` keeps its one-argument (`QVariant`) signature for existing `QMetaObject.invokeMethod` callers. The visible edit button calls `openSetEditorFromControl(identity, focusTarget)`. The page owns `editorFocusReturnTarget: Item` and `editorFocusGeneration: int`; opening an editor advances the generation, while starting a new optical set or closing the page clears the target and advances it again. When the editor closes, the page schedules focus restoration only if the page and target remain visible and the captured generation is still current. Cancel discards editor-local changes and leaves the bridge draft unchanged; closing the page while the editor is open cannot restore focus to its hidden controls.

## UI states

The material library is a page popup inside its existing host, not a separate window. Loading and applying states disable conflicting controls; an empty list keeps the new-material action visible; read/validation failures appear in the preview dialog. Save clears dirty only on success. Closing a dirty draft follows the existing save/discard/cancel confirmation. Restore explicitly names the affected builtin override and states that saved snapshots are unchanged.

Theme sizing comes from the existing Theme.snapshot logical dimensions. The containing host continues to own window geometry and scrolling; the import preview does not reset the edit form or current selection. The material list, editor scroll position, popup focus, and draft confirmation remain owned by the existing page/host; scrolling is reset only when a different material draft is installed, not on field edits. The action toolbar wraps by available logical width so all actions remain visible in narrow windows.


## Category-local names (2026-10-19)

The visible/name categories are `film` and `substrate`.
`material_categories(record) -> tuple[str, ...]` admits only explicit film and
substrate roles; an explicit category is used only when roles are absent.
Dual-purpose records belong to both pools. `material_category(record)` is the
first membership or `None`, never a fallback to film. Medium-only, container,
component, element and unclassified records gain neither membership by name,
optics, nor a historical `other` value. Their stored data is preserved; no new
`other` browser category, relabeling or automatic deletion is introduced.

`check_material_name(record, records)` compares trimmed, case-insensitive names
only where the same owner's explicit category memberships intersect and excludes the same opaque ID.
Global writes/overrides/restores/import application and project material writes
and imports use that fence; package/import failures preserve all existing files.
Reading pre-existing records does not reject or discard historical duplicate
names. Project material IDs and optical-set ownership remain the binding truth.

Each category contains its ordinary ideal Air record: film `builtin_ideal_air`
retains its stable ID, substrate uses `builtin_ideal_air_substrate`. Their optical
values and project-model provenance are equal but their editable value ownership,
overrides and deletion markers are independent. Both are optical-only; no
evaporation source models or physical substrate data are inferred.


Medium/film picker projection refresh captures all pending purpose rows before
notifying the edit session. The session remains the state owner; its synchronous
change callbacks may reinstall picker projections without replacing inputs that
are still pending in the same refresh. This preserves declared-medium dataset
selection while restricting newly offered media to explicit usage.


## Confirmed builtin seed removal (2026-10-19)

After explicit user confirmation, the ten `_source_container` records for W,
Mo, Ta, Pt, graphite, glassy carbon, FABMATE, Cu hearth, Al2O3 crucible and TiB2–BN
were removed from `core/resources/researched_materials.json`, the sole seed read
by `material_research.research_material_records()` and `builtin_material_profiles()`.
They are not generated, relabeled, hidden or transferred to another library.
The 18 remaining research material records, including independent `Cu_film`,
`Al2O3_film` and all five research substrates, remain unchanged. Shared original
literature/source registries and paper references remain unchanged; no deleted
material record was saved as a research-library replacement. The two ordinary
Air records and three sourced optical-substrate presets remain available.
No user project or user material file is changed by this seed deletion.
