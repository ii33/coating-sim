# Selective resource transfer contract

Date: 2026-10-15

## Ownership and API

The persisted schema remains `coat_sim.project`, version 1 JSON. An explicitly
selected descriptor can have any filename ending in `.coatproj`. Directories,
`project.json`, and `.cproject` archives are not descriptor-opening aliases.
`ProjectResourceService` owns one lazy `ProjectResourceExchange`; editor drafts
remain owned by their existing workspaces and controllers.

The Python signatures in `core/project_resource_service.py` and contract tests
are authoritative:

- `inspect_resources(source="project")` accepts the current saved project,
  `"global"`, an explicitly chosen descriptor/archive, or a supported standalone
  resource. It returns an opaque `catalog_id`, `source_type`, `source_path`,
  `name`, `categories`, `warnings`, and `rows`. Each row contains `id`, `kind`,
  `name`, `summary`, `notes`, `dependencies`, and `read_only=True`. Summary/notes are detached read-only information projections, using the existing per-kind metadata aliases (material envelopes use material description/notes; binary input metadata returns empty strings). Read-only describes the source;
  it does not disable root selection.
- `preview_resource_transfer(catalog_id, selected_ids, target="project")`
  captures an explicit destination and a dependency closure. Targets are the
  bound current project, `"global"`, another explicit `.coatproj`, or a new
  `.cproject` archive. The result contains `plan_id`, `rows`, `selected_ids`,
  `dependency_ids`, `target_type`, and `target_path`. Rows add `selected`,
  `conflict`, `target_id`, and `choices`. Only roots are explicitly selected;
  required descendants cannot be excluded from a retained root's closure.
- `apply_resource_transfer(plan_id, decisions=None)` applies a saved plan, with
  decisions keyed by source document UUID. It returns `created`, `copied`,
  `overwritten`, `reused`, `skipped`, the document UUID `id_map`, transferred
  target-root `selected_ids`, `target_path`, and `replaced_ids`. Destination
  backends may add publication details such as `written_files`.
- `cancel_resource_transfer(plan_id)` and
  `release_resource_catalog(catalog_id)` only discard in-memory tokens. Release
  also cancels idle plans for that catalog. Successful apply consumes the plan.
  Once apply reserves a plan, another apply/cancel/release reports
  `transfer_in_progress`; an in-flight plan is never silently discarded.

Catalog and plan results are detached dictionaries. Changing a returned list
cannot change the held source snapshot or target plan. Catalogs are bounded to
16, plans to 32; expired tokens require a fresh browse/preview. Forked editors
have separate exchange tokens and retain the project's existing mutation gate.

## Read-only browsing and confirmation boundaries

Browsing and previewing never import into a user's project, create a global
library, start Qt, run a calculation, save a draft, or change a default.
Archive browsing reuses the shared read-only `read_project_package` validation
boundary and does not extract or permanently import a project. A preview with
selected binary inputs owns only their context-managed private spool, released
on cancel, successful apply, or owner cleanup. Current archive member, byte, digest, path, reparse-point,
encryption, compression, and exact-input restrictions are preserved.

A preview does not grant an implicit overwrite. An existing output archive is
rejected, and exclusive publication also rejects a destination appearing later.
Source snapshots are reloaded before preview and twice during apply, including
immediately before target publication. A changed or unavailable source raises
`stale_source`. A project destination retains optimistic revision and writer
lock protection; a stale target requires previewing again.

Read-only catalog loading, source revalidation, target snapshot capture, and
graph preparation run outside session-host/resource locks. Short admission
checks before and after read work reject a closed owner. A private exchange
lock protects token creation and atomic apply reservation only. The Qt adapter
may run these synchronous methods in a worker without blocking another project
read/close on a long archive validation.

Final apply publication uses the existing operation scope and mutation freeze
for both the originating owner and any already-open target owner. Project-root
lock ordering is stable for opposite-direction transfers. The target retains
its descriptor CAS and writer lock; a current in-process target store is reused
instead of becoming stale through a separate writable owner. There is
no asynchronous worker owned by this core API. Cancellation is effective before
apply starts; synchronous publication cannot be interrupted midway by closing
a dialog. The UI must keep an in-flight operation owned until its result is
reported and must not report cancellation after a committed transaction.

## Copy, dependencies, and conflict semantics

Project-to-project transfers create new document UUIDs and project-owned JSON.
File references and deposition-configuration roots are rebound to copied UUIDs.
Film and expression references bring their frozen optical-material closure;
full material profiles also carry their corresponding frozen optical row when
one is present. No mutable records or source filesystem paths are shared.
Unrelated target resources and the target's default configuration are retained.
Exporting to another project does not modify the source project or drafts.
A source and target descriptor in the same project directory are rejected.

Conflicts use case-insensitive material/profile business IDs and the existing
exact film-design/template business IDs. Ordinary designs get fresh UUIDs. Identical business
records have `conflict="identical"` and default to `reuse`; different content
has `conflict="different"` and requires `copy`, `overwrite`, or `skip`. A fresh
resource defaults to `import`. Overwrite is available only as an explicit
row decision and reports replaced target UUIDs. Copy generates a fresh material
or film/template business ID; every included film/template material reference is
rewritten consistently. A full material and its frozen optical row require one
coordinated decision. Existing dependent target graphs are revalidated, so an
explicit overwrite that would invalidate them fails without publication.

Skipping a required dependency skips selected roots that need it. The retained
roots' closure is recomputed, preventing dangling references or orphan-only
partial import. Empty/all-skipped project transfers do not publish a generation.
Existing editor Save operations continue using project-managed identities and
revision checks; transfer never saves or silently replaces an active draft.

## Global library scope and support matrix

Global browsing uses only the application-defined five typed design libraries,
the existing read-only application material catalog, and the existing
`data/film_design/library.sqlite3`. It does not discover projects or search the
user's disk. A missing global database is not created merely to browse.

Project and package destinations allow roots from multiple categories. Global
exports accept roots of exactly one category per operation, with the full
required dependency closure listed in preview:

- Source, substrate, mask, machine, process: validated closure published in a new
  exclusive subdirectory of that type's existing global design library.
- Full/optical material: use the existing material-library transactional writer;
  optical-only records retain unknown process models rather than inventing them.
- Film design and expression template: use their distinct existing SQLite
  tables. A transfer requiring unsafe cross-storage publication must fail with
  `unsupported_transfer` and an actionable explanation; it must not claim a
  partial material/film operation succeeded. Existing globally available optics
  can be reused before the SQLite transaction.

There is no new cross-JSON/SQLite distributed transaction. Each supported
backend validates before publication and either uses its existing atomic
boundary or reports a failure, including recoverability information if a
rollback itself fails. Global typed filenames have stable source identities
when read again; an apply result's newly allocated target UUID is a transfer
report, not a promise of the subsequent catalog's path-derived UUID.

## Errors and verification

Errors are `ProjectError` values with stable codes, including existing safety
codes and `expired_catalog`, `expired_plan`, `invalid_selection`, `same_project`,
`stale_source`, `stale_target`, `transfer_in_progress`, and `unsupported_transfer`. The caller must show
the error and retain drafts. A failed validation, unresolved conflict, expired
or canceled plan, stale source/target, or frozen project cannot be shown as a
successful import/export. Capacity and path protections are shared with
`project_dependencies`, `ProjectStore`, and `project_package`.

Focused contract tests cover selective closure, copied reference ownership,
material and expression conflict decisions, package isolation, cancellation,
stale source/target rejection, mutation freeze, atomic failure, slow-loader
owner-close fences, opposite-direction transfers, and competing apply/cancel
reservations.

## Saved optical simulation inputs

The same API now transfers ray/appearance input envelopes and their typed binary dependencies. See [the strict asset and package contract](project-simulation-input-contract.md). Catalogs list metadata without spooling all inputs; only selected closure bytes enter a plan-owned spool. New simulation global roots are explicitly defined under data/raytrace and data/appearance. Unrelated renderer outputs are never discovered or packaged. Resource information aliases follow [the metadata contract](resource-information-contract.md).
