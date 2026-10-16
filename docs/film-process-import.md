# Film design to process import contract

Date: 2026-10-15. This contract covers importing an in-memory FilmDesign
draft into a deposition process. It does not define an optical solver or a
double-sided deposition flip operation.

## Data contract

Process schema v7 adds two optional root values:

- `film_design_snapshot`: a complete `coat_sim.frozen_film_design` snapshot
  with the frozen design, settings, selected material and optical-set records,
  and SHA-256 digest. The digest uses UTF-8 JSON with sorted keys,
  `ensure_ascii=False`, and `allow_nan=False`, matching the FilmDesign freeze
  format. Import completion uses the typed film document dependency closure:
  already-bound datasets from both faces, substrate/material media, every
  expression symbol and version-1 optimization `structure_materials` candidate.
  Multiple required sets of one material are retained together, including
  unused symbols and candidate-only materials that the standard numerical
  freeze does not capture. Missing records or malformed typed candidates fail;
  records come only from the supplied service's material provider. The full
  expression and workflow metadata are retained without reinterpretation.
  Each referenced material retains all provider optical sets in their original
  order, including valid unused sets; every included set is validated. Used
  values already captured by the numerical producer remain authoritative.
  Unreferenced materials are not added. This preserves exact same-project
  material replay without relaxing content-conflict checks.
  An absent optional `design_ready` key remains absent rather than acquiring
  the numerical producer's default `null`; an explicit `null`, `false` or
  `true` value is preserved. Project material equality/conflict rules are unchanged.
- `film_design_import`: `{snapshot_sha256, surface, sequence_order, layer_ids,
  unexecuted_surfaces, source_slots_by_material_id}`. `surface` is `front` or
  `back`; `sequence_order` is `substrate_outward`; the other face is recorded
  as unexecuted. There is no flip event or claim that both faces are deposited
  in one recipe.

Each generated process step has `film_layer_ref: {surface, layer_id}` and an
immutable `film_design_thickness_nm`. `target_nm` remains the editable machine
target. Import initializes the target from the converted design thickness,
but subsequent target edits do not rewrite the frozen design value.

The design snapshot retains source thickness values and units (`nm`, `um`, or
`qw`), reference wavelength, `use_k`, material and optical-set IDs and full
records (including n/k data), substrate material/data/geometry/sides, and
incident/exit media. The process step is only a mapping to a stable design
layer ID; it does not copy or reinterpret that source metadata.

## Conversion and ordering

| Design thickness | `film_design_thickness_nm` |
| --- | --- |
| nm | value |
| um | `1000 × value` |
| QW | `value × reference_wavelength_nm / (4 × n(reference_wavelength_nm))` |

For QW conversion, n is evaluated from the frozen layer material and selected
optical set with out-of-domain extrapolation set to error. Missing data,
unknown/invalid n, non-finite values, non-positive thickness, or a converted
thickness above the process limit rejects the import. The original unit/value,
dataset, wavelength, k data and `use_k` remain in the snapshot.

The user selects one face explicitly. The selected design array already uses
`substrate_outward`, so generated steps preserve that order directly. A back
face is available only when the frozen substrate declares `sides=double`.
Single-sided recipes may retain a back-face draft in the full snapshot, but
cannot execute it. No legacy v6 aggregate target is divided or expanded into
per-layer values during migration.

Every distinct selected-face `material_id` requires a user-selected valid
machine source slot. No matching by material name, source name, or ID occurs.
Missing mappings remain visible in the review dialog and prevent replacement.
The map is stored in the process provenance and validated against the source
slot on each linked step. Editing a linked step's source updates the explicit
mapping and all linked steps for the same material together.

## UI state and failure boundary

`ProcessBridge` owns the candidate frozen snapshot, selected face, explicit
material/source mapping, errors, and current process document. The launcher
selects the uniquely active visible FilmDesign window, or the sole visible
FilmDesign window if none is active, and supplies its current in-memory draft;
saving a `.cfilm` first is not required. If no window is visible, multiple
windows are ambiguous, initialization/expression work is still pending, or
freezing fails, the dialog reports the error and the process document remains
unchanged.

The import button remains enabled before a machine is selected. Its bridge
entry point first checks the selected machine and refreshes its source
dependencies. A missing machine, failed machine load, or machine with no valid
source opens the existing import dialog with an actionable error; it does not
request a film draft or change process steps, dirty state, or saved files.
Close the dialog, browse/create the machine and configure a valid source, then
click import again. Each request resets the previous candidate and error, so
a failed attempt does not block a later valid import. The request and review
use the existing synchronous launcher delivery; there is no background task or
late completion to cancel. Cancel/close continues to discard only the review,
without applying its candidate.
An already-open asset package uses its captured machine runtime; if the
package file changed, import reports that an explicit refresh/reload is needed
and never implicitly replaces the process through the preview reload path.

The process toolbar temporarily hides its batch-film entry while preserving
`FilmStackController`, the dialog and their APIs. Machine and substrate rows
use compact “浏览” / “新建” / “编辑” labels with full accessible names; dependency
selection and editor routing are unchanged. This is a presentation change,
not a recipe-format or numerical change.

The review dialog keeps the face choice and table heading fixed, scrolls only
the layer rows, and keeps cancel/import controls fixed at the bottom. Each row
shows the stable layer ID, material and optical set, original thickness and
QW conversion, source-slot mapping, and differences from current steps.
Import opens a second explicit replacement confirmation. Until that
confirmation succeeds, the process steps and snapshot are unchanged. Failure
keeps the review dialog and candidate input available for correction.

The process JSON is the source for save/load, process input snapshots,
portable simulation assets and queued requests. Each boundary must carry the
same normalized snapshot and digest; consumers must not replace it with a
current library lookup. Schema v6 is readable and normalizes to v7 without
adding design references or changing its total `target_nm`.

Older valid snapshots may lack unused symbol/candidate records. Numerical and
process validation remains compatible with these schema-version-1 snapshots;
it still verifies the digest and the expanded two-face/substrate/media closure.
Restoring a snapshot as an editable project draft additionally requires every
typed film reference inside that snapshot. If one is absent, restoration fails
with the missing material/optical-set pair and leaves the draft/project unchanged,
even if a matching ID exists in a current project or global catalog. Re-export
from the complete original film with its original referenced records to recover
editable provenance. No metadata is removed and no current record is guessed.

## Implementation and limits

- `coat_sim/core/film_process_import.py`: snapshot completion, digest
  validation, thickness conversion, preview and source mapping.
- `coat_sim/core/process_profiles.py`: v6 read/v7 normalization and process
  provenance validation.
- `coat_sim/core/schemas.py`: strict typed v7 process, step-reference, snapshot
  and import-provenance contracts. `design_data("process", ...)` normalizes v6
  before applying the v7 typed model.
- `coat_sim/ui/process.py`: candidate, mapping, difference, replacement, and
  dirty-state handling.
- `coat_sim/ui/launcher.py`: request routing from ProcessBridge to the
  selected FilmDesign controller/window, without opening, saving, or mutating
  another document.
- `coat_sim/ui/qml/FilmDesignImportDialog.qml` and
  `ProcessWindow.qml`: review, explicit mapping, difference display, and
  replacement confirmation.

This feature only creates a deposition recipe. It does not optimize an
optical stack, infer a machine recipe, add physical flip mechanics, or make an
unvalidated source/material compatibility claim. Editing source rates and
machine targets remains a user decision.

## Validation examples

The isolated test `tests/test_film_process_import.py` uses synthetic in-memory
material records, not production catalog values. At `lambda0=550 nm`, `n=2`
and `1 QW`, the converted thickness is `68.75 nm`. Tests also verify complete
front/back dataset retention, digest tamper rejection, explicit source
mapping, out-of-band QW rejection, strict typed v7 fields, v6 migration without
per-layer guessing, and queue snapshot preservation. The archive round-trip
test exercises actual `.coatasset` publication and reading; it reports a skip
if the host denies the package's atomic hard-link publication.

`tests/test_film_process_editable_closure.py` uses synthetic local records to
cover complete editable closure, multiple sets per material, metadata retention,
pathless project initialization and explicit managed save, including same-project
replay with absent/explicit `design_ready`, provider dataset order and unused
datasets. It also covers older numerical/process compatibility, nonmutating
editable rejection and unavailable producer dependencies. These are
dependency/serialization contracts; numerical formulas and persisted schema
versions are unchanged.
