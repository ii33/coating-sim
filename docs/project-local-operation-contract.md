# Current-format local asset operations

The current schema is the only editable, computable and exportable format.
This boundary does not translate obsolete fields, migrate versions, infer a
missing aperture, or make an unsupported document valid.

## Operation ownership and validation

`ProjectStore` remains the sole publisher of its manifest. A local mutation
captures the complete manifest and each ordinary managed file's original byte
digest, including rows omitted from the validated browser catalog. The capture
is evidence, not a validated simulation snapshot. Unparseable content has an
explicit `OpaqueDocument` marker; its display metadata cannot be published as
input data.

Only actually changed/current targets, their complete forward dependencies,
and affected saved consumers enter strict current-schema validation. Relevant
process/configuration closures also undergo the existing runtime checks. An
unchanged explicitly selected default is still a required strict root. Unknown
or unreadable possible references fail with `reference_unconfirmed`; they are
not treated as absent edges. Typed reference capabilities are a closed table;
adding a managed kind without updating that contract fails closed.

All incoming manifest dependencies are considered, including unsupported or
hidden rows. Removal never cascades, even when a caller supplies both a parent
and its child. Logical material/template identities remain unique. Embedded
frozen film values inside a process are not live references to a managed film
asset.

Unchanged rows retain their exact paths, hashes and file bytes. Retained files
are checked again at the existing writer boundary, alongside the manifest
fingerprint. Only changed documents receive a new immutable generation. A
publication error must leave the active descriptor unchanged. JSON capacities,
binary input quotas, ordinary-file checks, owner/generation gates and explicit
confirmation are not waived.

## Unsupported entries and deletion

The project browser exposes unsupported entries as separate diagnostic
metadata, never as validated resource contents. They cannot open a material
editor or be exported/copied. The user may explicitly select one for deletion.
Deletion does not require normalizing the unsupported target; it does require
the same complete incoming-reference and active-owner checks. Unknown relevant
references block it with a reason, without deleting another asset.

Ordinary current-format deletions retain their existing transaction records.
An unsupported target has no new restoration/migration protocol and is not
advertised as recoverable current data. Removing an active manifest entry does
not unlink old immutable file generations. This is not a permanent filesystem
purge or an automatic cleanup of other assets.

## Scope of export and recovery

Selected-document export and a new selected-resource package validate exactly
their dependency closure. A full project export/import/clone must validate all
included inputs and fails on an unsupported document, naming its kind/path.
It must not silently omit content and claim a complete export. Existing session
recovery metadata and frozen film snapshots do not change these rules.

The local store create/save/import/batch/template/material/simulation-asset
paths share `project_operation_scope.py` and the common publisher. Selected
resource exchange into an existing `.coatproj` now uses the same complete
captured target, keeping unchanged rows, paths and raw byte digests intact.
New, overwritten and explicitly reused roots, their dependencies and affected
consumers must satisfy the current schema. Relevant unknown identities or
references fail closed; a preview never guesses a conflicting target. Related
binary resources retain envelope and payload validation, staging and aggregate
quota checks. Preview cancellation, target changes and failed publication do
not change the active target. Global-library and new-package exchange retain
their separate strict target contracts.

Tests: `test_project_mutation_capture.py`,
`test_project_scoped_asset_operations.py`, `test_project_asset_scope_gui.py`,
`test_project_scoped_resource_exchange.py`, and the unchanged
project/session/reference/transaction regression suites.

Template business IDs are exact, case-sensitive symbols, in both ordinary
publication and selected transfer. Thus `Dose` and `dose` are separate IDs; only
an exact ID match offers reuse/overwrite/copy conflict decisions. Material-ID
normalization is unchanged. The generic operation scope does not invent a new
case-insensitive template identity.
