# Bound current-editor operations

Date: 2026-10-24. This is an explicit controller gateway, not arbitrary UI,
mouse/keyboard, method invocation, or document replacement.

## Input, ownership, and output

The shared assistant registry first authenticates the exact six-field
`ProjectTarget`: client ID/generation, project ID/session generation, canonical
root, and the actual descriptor file. No focus, PID, or default project supplies
a missing target. The application client forwards the authenticated owner
identity. The gateway requires the current route's `instance_id` and a complete
`expected_snapshot_id` obtained from `project_editor_snapshot` for the same
module. Snapshot hashing is shared with the read interface, including related
window instances, draft revision/content, and live optimization configuration.

The host owns routes; the existing workspace editor owns draft/dirty/revision
state; each existing controller owns its own optimization controls. The gateway
does not create a workspace/editor/window, read a saved fallback, or maintain a
second editable state. One GUI-thread turn holds host admission and editor locks
from token validation through one literal public controller operation.

Success returns `accepted`, all forwarded generations/project identity, module,
window instance, base snapshot ID, current revision/dirty state, and a truthful
per-module capability matrix. Clients must refresh the snapshot after an edit.

## Implemented matrix

- `film_design` update: exactly one of name, summary, note,
  reference_wavelength_nm, assume_lossless, use_k, first_surface.
- `film_design` actions: save, add_layer, insert_layer_after, delete_layer,
  move_layer, set_layer_value. Layers require exact side and stable layer ID;
  editable layer fields are thickness.value, thickness.unit, and note.
- `film_optimization` update: exactly one of search_mode, structure_mode, solver,
  acceptable_error_percent.
- `film_optimization` actions: save (the same workspace film/workflow draft),
  set_solver_option, set_structure_option, set_structure_material_selected.
  Exact option names and argument fields are returned by capabilities.
- Optimization record actions: set_objective, add_objective,
  duplicate_objective, remove_objective, and set_variable. Objectives use an
  exact objective ID; a record patch is merged into a detached complete record
  and normalized by the controller's existing wavelength/angle helpers before
  the real atomic configuration loader is called once. Record identity and
  derived sample fields are not writable. Variables require exact side/layer
  ID plus enabled/lower_nm/upper_nm together, avoiding invalid intermediate
  bounds. The public editor workflow setter preserves dirty/save ownership.
  The current optimization configuration is version 3; legacy version 2
  remains readable, while `automatic_repair` is valid only in version 3 and
  routes start/continue/apply through the existing `FilmManufacturingQuickController`
  and `film_manufacturing_repair` executor. This project API has no compute-start
  action; its read-only submission summary names this route rather than
  fabricating an ordinary film_optimization request. Shared band, angle,
  polarization, metric and weight patches preserve both existing constraint
  groups. `mean` and `pointwise` are independent nested records; when a patch
  includes either group, it replaces that entire group. Each supplied group
  must contain `enabled`, `mode`, `target_value_percent`, `lower_bound_percent`,
  `upper_bound_percent`, `strict_lower` and `strict_upper`. Percentages remain
  GUI percentages; the existing controller owns their calculation conversion.
  Optional `bounds_drafts` retains only disabled-group lower/upper text under
  the current editor contract. It cannot make an active uncommitted group valid.
  There is no implicit group merge, inferred legacy mode, or acceptance of old
  top-level mode/target/bound fields. Adding an objective starts with the current
  native defaults, including enabled pointwise and disabled mean groups.
  Detached record preflight uses the native group field set and existing active
  criterion validator, then the strict atomic loader commits once. Both groups
  disabled, incompatible active groups and malformed records reject without
  changing the configuration, draft/dirty/revision, selection or snapshot
  readability. Disabled groups remain editable state and contribute no
  calculation target. These synchronous edits start no asynchronous result,
  cancellation or queue operation; the existing owner and snapshot fences apply.
- `film_simulation` update: exactly one of wavelength_min_nm,
  wavelength_max_nm, samples, step_nm, angle_deg, polarization, first_surface,
  extrapolation_policy. The token covers the actual current simulation
  controller's configuration and source identity. Parameters use the existing
  setParameter method and its existing sample-count helper. Save uses the same
  workspace film/workflow owner; calculate and result export remain excluded.
- The six project resource editors and three compute-input editors have the
  finite extension described below. Queue/results remain read-only.

The design validator checks a detached prospective value before the real setter;
option inputs are type/range checked before calling existing validated public
setters. Multiple-field updates are rejected rather than partially committed.
Invalid requests preserve draft content, dirty state, and revision. Existing
controller rejection is reported as rejection, never as success.

## Resource and compute-input extension

The source of truth is `project_editor_capabilities(module)`, including exact
`field_schema`, `action_arguments`, and `action_schemas`. An update still
contains exactly one field. A structured action is one bounded original GUI
operation, not a list of edits. The gateway verifies the actual registered
resource/compute owner through the shared current-editor snapshot fences and
never obtains a lazy editor or switches the selected resource. All dispatch
calls are literal bridge/controller methods. Request strings never become
method names, arbitrary JSON pointers, file paths, or executable expressions.

| Module | Current-input fields and bounded actions | Original owner/methods |
| --- | --- | --- |
| source | Metadata, device/shape and source geometry; declared emission fields; current selected aperture fields/add/remove | `SourceBridge.setField`, `setEmissionField`, `setApertureField`, `addAperture`, `removeAperture` |
| substrate | Metadata, outline/surface type and parameters; add point or modify/remove current selected point | `SubstrateBridge.setField`, `addPoint`, `updateSelectedPoint`, `deleteSelectedPoint` |
| machine | Metadata and eight visible geometry fields; one installed source coordinate; one monitor field; add/remove monitor | `DocumentBridge.setMetadata`, `setValue`, `appendItem`, `removeItem` with internally constructed finite pointers |
| mask | Metadata, z; bounded outer/hole contour replacement/add/remove, selected contour rotation, full-mask translation/scale | Existing `setMetadata`, `setValue`, `appendItem`, `removeItem`, `rotatePolygon`, `transformMask` |
| process | Metadata; one step field, installed source choice, board toggle, add/delete/move step, complete environment defaults/override | `ProcessBridge.setField`, `setMetadata`, `setStep`, `chooseStepSource`, `setStepMask`, `addStep`, `deleteStep`, `moveStep`, atomic `applyEnvironment` |
| materials | Metadata and existing resistance/electron-beam model scalar/provenance fields | `ProjectMaterialLibraryBridge.setField` |
| main | Visible deposition text inputs, algorithm, algorithm budget, fast-cycle toggle and residual threshold | `MainBridge.setField`, `setAlgorithm`, `setAlgorithmBudgetSeconds`, `setFastCycle`, `setFastThreshold` |
| ray_trace | Polarization; one existing sample/spectrum/surface/detector field with exact row identity/index | A detached bounded JSON patch followed by one `RayTraceWindowController.setRequestText`; no raw request replacement |
| film_appearance | Existing geometry/light/camera/sampling text fields, four light/calibration toggles, relative/absolute spectrum kind, restore inherited geometry thickness when available | Existing `AppearanceWindowController.setField`, `setFlag`, `setSpectrumKind`, `resetGeometryThicknessToInherited` |

Source and material rate text controls preserve the actual GUI display unit:
the historical keys ending in `reference_rate_nm_s` and `valid_rate_*_nm_s`
hold Å/s text in these forms; their serialization converts to nm/s. The returned
schemas explicitly identify this difference. Process `rate_nm_s` and
`rate_angstrom_s` remain separate declared controls. No request may append a
unit, swap an undeclared unit/model, or supply an arbitrary pointer.

Unknown fields, extra arguments, wrong types, invalid enums/models/units,
out-of-date selected rows and malformed structures are checked before a real
setter is called. Original GUI text controls may keep incomplete or physically
invalid text. Such an accepted edit returns its actual validation result and
dirty state, rather than silently repairing, rejecting, or saving the draft.
This differs from malformed requests, which must leave content, dirty state,
revision and selection unchanged. The environment action retains the original
service's all-affected-steps validation and single commit boundary. There is no
new cross-field batching mechanism or shadow editable model.

Ordinary setter effects are intentionally retained: field validation and
lightweight source/substrate/scene previews, machine geometry refresh, mask
ring trajectories and invalidation of old analysis, and main-window work/memory
estimates. These do not enqueue a computation or create a compute approval
receipt. Device/backend/precision selection is excluded in this version because
those controls can persist compute preferences or initiate probing. Pure reads
continue to call none of these validation, preview or estimation routines.

Successful new-module writes return the actual refreshed snapshot, its ID,
original dirty state, and validation result. For ray tracing the GUI validation
scope is request JSON/schema and asset binding; full physical checks remain at
the explicit computation boundary. A validation failure is not a successful
calculation and does not authorize one. Existing per-editor undo/redo behavior
is retained; these bridges do not acquire a synthetic global undo stack.

Not supported by this extension: material selection or snapshot replacement,
optical-dataset editing (including an open nested dataset modal), material roles,
IDs, research/reference evidence, source/machine/substrate file dependencies,
film import or asset binding, arbitrary raw request JSON or raised compute
limits, new/open/reset/import/export/save, device/backend configuration, result
edits, queue changes, cancellation and execution. Native text/geometry buffers
and pending dialogs must be resolved through their original UI. This does not
claim support for every GUI control. The pre-existing three film modules retain
their already declared explicit save actions; no save action is added elsewhere.

## Fences and unsupported behavior

Missing snapshot token returns `requires_snapshot`; stale token/window/session
or mismatched root/descriptor fails closed. Initialization, project close/exit
review, compute/edit freeze, ownership transfer, related controller busy state,
expression work, pending confirmations, unconfirmed information, source reuse,
and native text buffers block writes. The adapter never commits a native field,
resolves a dialog, or replaces an editor as a side effect.

No force, path, data, pointer, file replacement, new/reset, export, arbitrary
method, or candidate-apply action is exposed. Save calls the existing managed
save action with its dependency/revision/dirty checks and no arbitrary path or
overwrite flag. Existing pending save/replacement confirmation stays in the
original UI. Computation still requires the existing estimate/confirmation/
queue contract; calculate/start and cancellation are not reopened by this
gateway. Candidate apply is excluded because the existing signal may launch
a simulation; it needs its own explicit compute-approved contract.

Chat/plan/read-only restrictions are enforced by the assistant runtime before
dispatch. Possessing a current-editor snapshot does not grant write permission.
These operations do not add a numerical algorithm, alter the film format, or
change calculation budgets.

## Complete optimistic snapshot evidence

Every declared editable scalar and bounded structural target is projected as
retained GUI state and included in the snapshot token. Raw invalid text remains
visible; MainBridge's invalid fast-threshold text/flag are bound separately from
its unchanged canonical numeric setting and dirty calculation. Material
optical-only and null-model flags bind the existing serialization/editability
semantics. Native substrate dragging is a write fence, while pure reads remain
available.

Machine, mask and process retain JSON document drafts. Their complete retained
`_data` is incrementally hashed in memory, independently from the public bounded
field whitelist. This evidence distinguishes absent, null, empty object/list,
NaN and positive/negative infinity without normalization, validation, file
access, returning arbitrary document data, or copying result/optical arrays.
Process nonfinite step scalars have JSON-safe named markers beside the null
numeric projection. The captured process source/mask preview and mask machine
context are likewise hashed; environment presence, mask count, CAD provenance
and all three analysis availability/generation markers are bound. Large optical
and result data retain their dedicated revision/content-evidence strategies.

## Verification scope

Targeted offline Qt/controller tests exercise the real setters, dirty/save
round trip, stale tokens and window generations, strict field/action arguments,
atomic invalid-input preservation, close/freeze/busy/confirmation fences, and
absence of compute admission. They do not claim complete UI automation or real
provider/GPU execution.
