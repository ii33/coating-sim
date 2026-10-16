# Geometric ray-trace job contract

This document records the initial JSON and queue boundary for geometric optical
tracing. It is a sequential, forward-only XZ meridional model; see the detailed
equations, inline diagram, references, and numerical limits in
[the offline algorithm atlas](algorithms.html#geometric-raytrace).

## Inputs and outputs

- `coat_sim.geometric_raytrace` schema version 1 request: explicit weighted
  ray samples, spectrum, polarization, ordered plane/spherical surfaces,
  transparent adjacent bulk media, and optional local thin-film stacks. All
  lengths are mm, wavelengths and layer thicknesses are nm, and optical
  constants are `n+i*k`.
- A coating layer is either uniform (`thickness_nm`) or explicitly mapped to one
  stable process `deposition_step_id` and an optical-global XY to substrate-local
  grid transform. A total-only legacy result has no per-step layer data.
- Queue input is a version 1 `JobSpec` with executor `geometric_raytrace`, the
  request in `parameters.request`, and, when needed, only an artifact ID and
  SHA-256 under `inputs.deposition_artifact`. It never stores thickness grids or
  machine-local file paths in the JSON spec.
- Large deposition arrays live in the normal verified `.coatasset` bundle. The
  worker checks artifact identity, role, path containment, size, and digest,
  then validates the asset manifest and NPZ array headers before loading the
  arrays.
- The worker saves one full result JSON artifact containing per-ray paths and
  events, optical energy totals, and optional detector bin edges and ray-energy
  histograms. Detector misses are a subset of optical transmitted energy.
  Queue summaries are viewable only when their spectrum rows, detector rows,
  and path/spectrum counts are internally consistent and backed by one
  registered result artifact. Before opening a queue result, the client
  validates the full version-1 JSON row structure, energy totals, detector
  histograms, and agreement between the downloaded artifact and compact queue
  summary.

## State, errors, and cancellation

The existing scheduler owns queue states, cancellation requests, result
artifact metadata, and repeat-submission idempotency. A ray job owns an
immutable frozen request; the worker owns its in-progress numerical result until
the JSON result artifact has been written. Clients read the queue status first
and download the result artifact through `SchedulerClient` after success.

Malformed requests, unmatched coating ports, missing/invalid layer fields,
unavailable artifacts, digest mismatches, invalid geometry, and result-write
errors fail the job with a specific error. Old deposition bundles report that
per-step fields are unavailable; they are never decomposed from `field_nm`.
Cancellation is checked between sample/wavelength paths and at surface
boundaries. A queued job can be cancelled before computation; a worker reports
cancelled only after the current bounded geometry operation reaches a check
boundary. Re-submitting the same job ID and same frozen input returns the
existing job; the same ID with changed input is rejected.

## Independent entry

`python -m coat_sim.ray_trace_cli --request examples/geometric_raytrace_uniform.json --output ray-result.json`
executes without Qt. Add `--layer-asset path.coatasset` for a captured process
asset. The output path is new-only to avoid replacing a prior result. CLI output
and queue-worker output both enforce `limits.max_result_bytes` against the actual
UTF-8 JSON bytes. The CLI count includes pretty-print indentation and its trailing
newline; an oversized result is rejected before the requested output path is
created, and an existing file remains unchanged.

This job contract uses the existing computation queue and state store. The
separate [remote compute service](remote-compute-service.md) now exchanges the
same JSON request and artifact reference through its authenticated asset
registration/download boundary. Explicit remote connection is available through
the Python client API; the current Qt pages still use the local scheduler.

## Qt Quick page contract

### Appearance-first optical workspace (2026-10-13)

The `ray_trace` route now starts in appearance mode. Its first action selects a
captured process `.coatasset`; no geometric request JSON is required. A valid
frozen appearance context creates one child `AppearanceWindowController`,
reused by `AppearanceWorkspace.qml` in the same window. Substrate, FilmDesign
layer IDs, and captured thickness fields are inherited from that asset. The
asset/light/camera/compute sidebar uses only that controller's existing typed
fields and scalar SPD import actions. The separate `FilmAppearanceWindow.qml`
shell reuses the same workspace, not a second renderer implementation.

`RayTraceWindowController` owns the selected asset, mode, geometric document,
and geometric queue lifecycle. Its child exclusively owns appearance draft,
camera, local QProcess, cancellation marker, and validated appearance result.
QML owns sidebar expansion, section expansion, and central-view tab state.
Changing mode preserves both drafts and never cancels a job. While appearance
is running, asset replacement and geometric submission are blocked. While a
geometric operation runs, appearance submission is disabled. Closing during
appearance asks once, requests that child's cancellation, and waits for its
normal frame-boundary completion before closing the parent. Rejecting the
close decision leaves the child and draft intact.

Empty request text is ordinary empty state, including after capability-property
reads. Malformed nonempty JSON remains a validation error in geometric mode;
explicitly running without a request reports that a request must first be
created or opened. A failed replacement asset preserves the previous usable
asset and appearance controller and displays the error. A readable geometric
asset without a supported appearance context stays available to geometric mode
and explains its appearance capability limit.

`几何光追（高级）` retains the original sequential XZ model, film import,
deterministic sample controls, layer binding, JSON editing, shared queue, and
result export. Its path, spectrum-energy, and detector views share the central
preview instead of reserving a permanently tall bottom table. Raw JSON is a
collapsed advanced section. The backend numerical precision and file schemas
are unchanged; the label does not introduce or promise a new precision setting.

The empty and loaded appearance rails use the same top-aligned four-category
navigation: asset, light, camera, and compute. A mounted StackLayout exposes
one parameter page at a time, with a separate persistent scroll position per
category. The shell owns the transient selected category; choosing a category
does not replace any controller or source data.

The integrated appearance entry remains the existing local scalar Mitsuba
renderer and scene-graph geometry preview. The isolated custom GPU validation
tools/HPC runs are not connected to this route. The window offers no unsupported
GPU or precision selector. Missing `build/mitsuba_appearance_env` fails visibly;
UI fixture tests are not physical-render or GPU proof.

The launcher route `ray_trace` opens `coat_sim/ui/qml/RayTraceWindow.qml` with
`RayTraceWindowController` as the sole owner of the request draft, selected
deposition asset reference, run state, cancellation token, and loaded result.
The QML page owns only transient control state such as selected tabs and
viewport selection. Its inputs are a v1 JSON request or saved UI document, an
existing film-design file, and an optional captured `.coatasset`; outputs are a
saved UI request document and a result-schema JSON document. Film import samples
the existing film-design/material-library implementation and replaces one
surface coating only after the page confirms the replacement.
The queue's `View result` action downloads the registered result artifact and
opens it in a new `ray_trace` window.

The advanced geometric view starts empty until a request is created or loaded. File reads,
scheduler RPCs, and artifact downloads run on the controller's single
background I/O worker; numerical work is submitted as a normal
`geometric_raytrace` scheduler job and is owned/cancelled by the scheduler
worker. The visible phases are empty, loading, ready, submitting,
queued/running/cancelling, complete, and failed. Cancellation during submission
is sent as soon as the accepted job ID is known; running cancellation uses the
shared queue cancel request and waits for the worker's terminal boundary.
Asset selection hashes a unique project-local snapshot. For a field-backed
queue run, that digest must match a bundle registered in queue history so the
serialized job contains only its stable artifact ID and SHA-256. The worker
makes a fresh private snapshot, verifies the digest, and parses those exact
bytes. Errors remain attached to the page and preserve the last valid
document/result.

Creating or opening a request updates the single controller-owned draft. Edits
invalidate the prior result. Saving to a new path never replaces an existing
file; replacing a document or result requires the page's explicit overwrite
dialog. Closing a dirty request offers save, discard, or cancel. Closing during
a worker operation asks whether to cancel and waits for the worker's safe
boundary before the launcher closes. The request editor and result area remain
mounted while dialogs are open, so their ScrollView positions persist; file and
confirmation dialogs return focus to their invoking controls. Theme spacing,
control heights, colors, typography, and split-pane margins use shared Theme
tokens as logical Qt Quick units. The central path/energy/detector tabs remain
beside a responsive right sidebar; F4 collapses that sidebar to a narrow rail.

The page does not introduce a new queue state owner or modify the shared theme
service. Launcher shutdown includes the dirty ray-trace document in its existing
save/close decision so application shutdown cannot silently discard this page's
request draft.
