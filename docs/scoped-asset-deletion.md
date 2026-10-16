# Scope-bound asset recycling

Date: 2026-10-16. No physics, numerical, or saved-input format changes.

## Authority and confirmation

Both `ProjectResourceService` and `GlobalResourceLibrary` expose:

- `preview_resource_deletion(catalog_id, selected_ids) -> dict`
- `apply_resource_deletion(plan_id) -> dict`
- `cancel_resource_deletion(plan_id) -> None`
- `list_resource_deletions() -> list[dict]`
- `restore_resource_deletion(receipt_id) -> dict`

The UI separately confirms deletion and restoration. Preview is observational.
It accepts the current service's already-loaded **own-scope** catalog only;
an external import, another project, an expired catalog, or another service's
token cannot authorize deletion. The preview contains `scope`, opaque
`plan_id`, exact `affected_ids`, composition `rows`, `action`, specific
`blockers`, and `can_apply`. Project previews also report project identity,
session generation, current revision, owner root and a snapshot digest.
Apply accepts only the opaque service-owned plan, revalidates the source,
owner, revision, references and live guards, and consumes it on success.
Cancellation, releasing the catalog and closing the browser invalidate plans.
There is no arbitrary path, generic JSON edit, cascade, or assistant-write API.

Each confirmation concerns one real asset. A material profile and its paired
optical projection are displayed together as explicit components of that
asset. A different asset is never implicitly included. Project optical data
with a profile must be recycled through the profile selection; a global
material's two projections share one physical JSON file. Incoming saved
references and the default project configuration block deletion.

## Project publication and live inputs

`asset_deletion.py` writes and fsyncs a bounded, owner-checked recovery record
under the existing `imports/.asset-recycle/<receipt_id>/receipt.json` before
calling the existing atomic `ProjectStore._publish` transaction. Only the
confirmed asset's logical membership is removed. Previous immutable JSON
generations and binary input payloads remain untouched. A failed archive write
cannot remove an asset; a failed publication can leave an inert prepared copy,
which is not advertised as a successful deletion. The optional store
`expected_fingerprint` fences even a concurrent mutation of the same store
object between preparation and publication.

Restoration preserves original document IDs, validates archived content and
its owner, and publishes a new monotonic project revision. Existing IDs are
never overwritten; missing dependencies reject restoration before publication.
The durable index works after closing and reopening a service. Recovery records
are not active inputs, saved package contents, calculation inputs, or resource
catalog fallbacks. Whole-project trash retains the normal root whitelist:
the records are inside the existing owned `imports` directory. Export remains
the manifest-only saved-input export and excludes this history.

Project guards report the specific resource module or job IDs for unsaved
resource edits, retained recovery/native buffers, related active editor
presentations, unsaved film edits, pending compute recovery and active jobs.
The worker never interrogates Qt native text fields. The UI must refuse its
own uncommitted embedded material input before both preview and apply.
`bind_asset_library_view_owner(controller)` can register only the exact
workspace-owned materials controller whose window is the current
`project_resources` route, with matching host, binding, store and window
registrations. This exempts that clean embedded detail view from the
active-editor blocker, never from dirty-state checks. Apply rechecks every
identity edge. No foreground-window inference or fake detachment is used.

## Global publication and recovery

`global_asset_recycle.py` resolves only paths and database rows already present
in the exact validated global catalog. It never accepts a destination path.

- Typed files are exclusively renamed into a same-filesystem private archive
  beside their source. Original bytes, inode metadata and hashes are checked.
- Custom material files use the established material-library lock. A user's
  builtin override is recoverable: the explicit action is
  `revert_builtin_override`, exposing the builtin original after archival.
  An original bundled builtin without an override is protected. Referenced
  overrides are blocked rather than silently changing their dependents.
- Film and expression-template rows are copied into a durable SQLite archive
  table and removed within the same `BEGIN IMMEDIATE` transaction. Restoration
  inserts the exact original row. The transactional database copy takes
  precedence over an interrupted filesystem receipt mirror. A matching
  `uncertain` mirror can only raise a recovery warning, never replace the
  authoritative archived payload or declare the operation successful.
  SQLite libraries with custom triggers are rejected as unsupported schemas:
  their side effects cannot be bounded to the confirmed asset. The check is
  repeated inside the write transaction before any row or archive mutation.

Private `.asset-recycle` trees are reserved history, excluded from typed
catalog scans and their entry budget; links are rejected before exclusion.
Restoration uses exclusive no-overwrite operations and checks dependencies
and the resulting graph. A newly occupied file or business-ID row blocks it.
Failures roll back the original membership when safe. If an intervening change
prevents rollback, `deletion_uncertain` carries the receipt and all recovery
paths; it is never a success or a permanent-delete fallback. Receipts expose
`state`: active, prepared, restoring, restored, rolled_back or uncertain.
Only active receipts should be offered as ordinary Undo; interrupted states
need visible review, and restored/rolled-back records are history.

## Deliberate limits

Global `binary_asset` deletion is explicitly blocked as
`unsupported_payload_ownership`: this version does not claim exclusive
ownership of possibly shared payloads. No metadata-only deletion is presented
as deletion of the full asset. Multiple unrelated assets require separate
confirmations. The global service proves current saved-catalog references, not
unknown external processes holding raw paths. The owning UI must block known
in-flight local consumers and dirty embedded editors; no task is cancelled or
job/result directory removed. Project frozen historical bytes are retained.

Tests use only self-created project, global, material and SQLite fixtures in
`tests/test_asset_deletion.py` and `tests/test_global_asset_recycle.py`.
