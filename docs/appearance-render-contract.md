# Thin-film appearance render contract

Date: 2026-10-13

This is a frozen-input, single-frame appearance workflow with default CPU and opt-in CUDA GPU execution. It starts from the frozen
FilmDesign plus actual per-step process fields, a spectral source, a closed
flat substrate, and a camera. It returns per-wavelength linear radiance,
linear XYZ/sRGB arrays, and a display PNG. It does not replace process
calculation, edit a design, or own substrate/layer selection.

UI-only layout and invocation boundaries are documented in
[Appearance window layout](appearance-window-layout.md). The existing window
uses a compact top action bar, central scene/result view, and persistent
right-hand parameter sidebar. F4 collapses that sidebar. Controller status is
presented in the shared title bar; the rendering and cancellation boundaries
below are unchanged.

## Boundary and state ownership

| Data | Authoritative owner | Appearance adapter behavior |
| --- | --- | --- |
| Layer IDs, material/optical-set references, front/back stack order, substrate material, and substrate actual-thickness design value | `FilmDesignService` frozen snapshot (`film_design_snapshot`) | Read and validate snapshot hash; never duplicate layer or substrate selection. “Actual thickness” is a physical length used by the optical plate calculations too; it is not an optical-only quantity or measurement provenance. |
| Which process step grows which layer | process v7 `film_layer_ref {surface, layer_id}` | Match by layer ID and one-based step index from the captured manifest; sum repeated process-step increments for that layer. |
| Actual spatial thickness | captured process asset, `layer_field_manifest`, `layer_fields` | Use float64 nm arrays, `substrate_local_xy` mm axes, validity masks, process order, and only fields included in the captured total. Never substitute `film_design_thickness_nm`. |
| Substrate outline and front surface | frozen process substrate profile | Read only; accept an explicitly flat rectangle or origin-centred circle. Preserve the actual circle radius; reject curves and arbitrary outlines. |
| Solid geometric thickness | Appearance adapter; explicit `geometry_thickness_mm` override or frozen FilmDesign substrate thickness | An explicit positive finite override in mm wins; an invalid explicit override is rejected without fallback. Otherwise inherit the snapshot's positive finite `thickness.value` after converting `nm`, `um`, or `mm` to mm. The source is `film_design_snapshot/design_value`; it does not claim the value was measured. With no override, missing or invalid snapshot thickness is rejected, with no implicit 1 mm fallback. The geometry is restricted to a flat rectangular or circular slab with constant thickness and a parallel flat back surface. The appearance form inherits this snapshot value by default, and its “使用膜系基片厚度” control restores that inherited value after an explicit override. |
| Render parameters and output | appearance job/CLI | The adapter creates a derived job; the appearance controller owns its local child process and result state. The local appearance window owns its child process. The separately exposed `appearance_render` queue/MCP path persists the same frozen job and explicitly selected backend/precision. |

`build_job_from_v7()` in `coat_sim/appearance/process_adapter.py` accepts a
validated v7 profile, the output of the read-only captured-field loader, and a
render-parameter mapping. The render-parameter mapping must include the saved
substrate profile when the asset adapter cannot provide it directly. Materials
are evaluated from the frozen snapshot with strict range checking. Unknown
`k` is rejected unless the frozen design explicitly opts into a lossless
assumption; any such assumption is recorded in warnings and provenance.

An empty `back_layers` list in the frozen design maps to a real uncoated
substrate/air interface on the closed body. Non-empty back layers require
their own actual captured thickness fields. A one-sided process import does
not erase a non-empty stack on the other side.

## Job fields

The self-contained CLI input uses `schema_version: 1` and these required
fields:

| Field | Meaning and validation |
| --- | --- |
| `coordinate_unit` | Must be `mm`; all camera, light, substrate, and process axes use the process-local XY frame. Converted to Mitsuba metres exactly once. Front outward normal is `-Z`; body extends toward `+Z`. |
| `wavelength_nm` | Strictly increasing samples within 360–830 nm; each material and source must cover the full sampled range. No extrapolation. |
| `substrate` | `outline_kind: rectangle` with bounds or `outline_kind: circle` with actual radius and bounds `[-R,R,-R,R]`, positive `solid_thickness_mm`, and `solid_thickness_provenance` equal to `explicit_geometry_input` or `film_design_snapshot/design_value`; also transparent substrate `material` and `back_surface.layers`. |
| `front_layers` / `back_surface.layers` | Arrays in FilmDesign `substrate_outward` order. Each row has a stable `layer_id`, wavelength-indexed `n` and `k`, and `thickness_field` with `unit: nm`, `origin: captured_process_step`, XY axes, thickness nodes, and validity masks. |
| `camera` | Arbitrary 3D `position_mm`, `target_mm`, `up`, and vertical field of view. Degenerate view/up vectors are rejected. |
| `area_source` | A rectangular area emitter with position, normal, up, dimensions, and scalar wavelength-indexed SPD. The normal must face the substrate. The SPD is either relative with a separate dimensionless `scale`, or absolute only with explicit radiometric calibration. |
| `environment_spectrum` | Uniform scalar spectral radiance in wavelength-indexed form. RGB triplets and RGB environment maps are not accepted. |
| `resolution` | Width, height, spp, and deterministic seed. CPU legacy execution keeps the 20 million aggregate scalar sample default. GPU explicitly lifts only that aggregate budget while retaining pixel, SPP, per-wave uint32 diagnostics and seed bounds. |

Inputs with incomplete material constants, unknown `k`, missing thickness
cells inside the actual rectangular or circular footprint, mismatched field grids, unsupported
curved outlines, missing or invalid geometric thickness, or out-of-band spectra fail
with a validation error before rendering. `k=0` is never inferred.

Each active emitter contributes in either relative units or calibrated
absolute radiance units. If both the area source and environment have any
nonzero samples, their unit kinds must match; a relative spectrum cannot be
added to an absolute one. A disabled/all-zero source does not determine the
result unit, so an absolute environment spectrum alone is reported as
absolute. The manifest records active-source flags and both emitter metadata.

## Output, ownership, and cancellation

`coat_sim/appearance/render.py` writes:

- `radiance_spectrum.npy`: float32 array shaped `[wavelength,y,x]`, linear,
  one scalar channel per sampled wavelength.
- `xyz.npy`: float32 `[y,x,XYZ]`, linear, CIE 1931 2° colour-matching functions
  and trapezoidal wavelength integration.
- `linear_srgb.npy`: float32 `[y,x,RGB]`, linear conversion of XYZ.
- `manifest.json`: engine/version, scalar-channel semantics, seed per
  wavelength, hashes, geometry/back-face model, source calibration, warnings,
  and frozen input provenance.

The renderer claims its output directory atomically by creating a new,
previously nonexistent directory. It refuses any existing directory, including
one containing an older `manifest.json`; failed/cancelled partial arrays can
therefore never be joined to an earlier completion manifest. The CLI's
`--display-exposure` is passed into the renderer, which writes the matching
view-only PNG and exposure value into that render's manifest.

The path integrator renders once per wavelength with `scalar_rgb`. Every
physical quantity and scalar uniform texture is evaluated at that wavelength;
the three film channels must agree exactly, and channel 0 is saved as
`L_lambda`. The integrator is not asked to apply CIE sensor weights. No RGB
channel is summed or multiplied by three. Display PNG tone mapping is a
separate view-only operation; it does not alter the saved spectral arrays.

At each smooth interface, project TMM powers select reflection, transmission,
or absorption. Transmission uses `eta = n_incident / n_transmitted` for its
Snell direction and chooses the outgoing local normal sign opposite to
`wi.z`, so a ray entering through the front can leave through the back. In
radiance transport its branch weight also includes `(n_incident /
n_transmitted)^2`. This applies independently to the substrate/air boundary on
an uncoated back face.

The UI launches one local Python child process per render from a unique job
directory under `data/tmp/appearance/`. It passes a frozen context descriptor
containing the process-asset path and SHA-256, resolved v7 profile identity,
and render inputs. The child rechecks the asset digest, resolves the v7 profile
from the asset's frozen input snapshot, and requires both the complete v7
FilmDesign snapshot and captured per-layer fields before it builds a render
job. It never reconstructs those from the geometric-raytrace request's optical
numbers. The process asset remains the substrate and layer-field source; the
appearance form has no second substrate selector.

The shared `geometric_raytrace` queue job remains separate. The appearance
child uses only `build/mitsuba_appearance_env` (with the module's project root
as a lookup fallback when the asset/output root is temporary). If that runtime
is absent, process launch fails visibly; it never falls back to the GUI
interpreter. The child uses its own site-packages without mixing NumPy, native
extensions, or DLL search paths from the application environment. Asset
validation reaches Pydantic through
`read_simulation_asset` → archive design-graph validation → `schemas.design_data`;
this headless validation chain does not import PySide6. Pydantic and its
transitive dependencies are installed into and pinned with the isolated render
runtime. The child does not update or depend on the frozen remote v6 package.
A missing runtime dependency is reported as a failed preflight/render with its
import error. The Qt event loop owns presentation and `QProcess` signals; the
child owns computation and files in its job directory. No worker is shared
between windows.

The process asset's substrate profile remains authoritative. The worker accepts
only an explicitly flat rectangular or origin-centred circular profile. For the appearance
adapter boundary, a v1 rectangle's `width_mm` and `height_mm` are converted to
the four centered local-XY corner points; no outline, sag, or solid thickness
is inferred from that rectangle record. Solid thickness follows the FilmDesign
snapshot/explicit-override rule above.

The dedicated Windows CPython 3.12 runtime is pinned in
`requirements-appearance-win-cp312.lock` (NumPy 2.2.6, Dr.Jit 1.4.0,
Mitsuba 3.9.0, and the Pydantic 2.13.5 headless schema-validation chain).
Renderer wheels were installed from the approved TUNA mirror into the isolated
venv; `.conda` was not changed. The runtime probe imports the renderer and
`schemas.design_data` without loading PySide6.

Cancellation uses a job-local marker read by the existing wavelength-boundary
check. The page immediately enters `cancelling` and keeps the event loop
responsive while the current wavelength frame finishes. A partial output
without a valid completion manifest is never a result. A close request during
render asks whether to cancel and wait at that boundary or return to the page;
it never synchronously waits or kills an unrelated process. Closing a window
manages only its own child.

The controller keeps the last manifest-validated result visible while inputs
are edited or another run is active. It compares the result input hash with
the current draft hash and labels the result `stale` when they differ. Editing
does not mutate an in-flight job's frozen input. A new result replaces the
visible one only after its manifest, job ID, input hash, image digest, PNG
decoding, and image dimensions validate. A bad manifest or missing dependency
preserves the previous valid preview and shows an error.

The current renderer checks cancellation between complete wavelength frames
but has no progress callback. The first UI therefore shows a running stage and
indeterminate progress, followed by an explicit cancellation-wait status; it
does not display a fabricated percentage. Child stdout is a bounded JSON-line
status channel, not a logging stream. Result arrays and the display PNG are
written locally. The shared appearance_render scheduler has a separate frozen request/result validation path.

## Qt Quick window and state contract

The RayTrace route now opens an appearance-first optical workspace. Selecting
an eligible captured process asset creates the existing appearance controller
and embeds `AppearanceWorkspace.qml` in the same window; geometric tracing is
available as an advanced mode. `FilmAppearanceWindow.qml` remains an internal
standalone shell for the same reusable workspace, not a second substrate
workflow or duplicated backend. The appearance workspace uses an
`AppearanceWindowController` as the sole owner of the render draft, active
child process, phase, cancellation marker, and displayed-result reference.
`RayTraceWindowController` keeps ownership of its geometric request, selected
asset, and mode; its child appearance controller receives only an immutable
asset reference/context. The QML owns transient focus, sidebar expansion,
scroll, and selected scene/render-tab state. Mode switches retain these
controllers and drafts. Parent close waits for its own appearance child's
cancellation boundary. See [the optical workspace boundary](geometric-raytrace.md)
for mixed-mode busy, empty, failed-asset, and close behavior.

The workspace has a central live geometry/completed-render preview and a
collapsible right-hand asset/light/camera/compute sidebar. Empty and loaded
states share those top-aligned category controls, with one visible parameter
page and four persistently mounted per-category scroll views. At narrow widths,
the preview remains beside the sidebar, which can collapse to a narrow rail;
run and cancel actions stay reachable outside the input scroll area. The window uses shared theme
controls and Theme logical-size tokens and the launcher geometry key
`film_appearance`. XYZ camera/light fields are compact X/Y/Z rows in
substrate-local mm coordinates: front outward normal `-Z`, body toward `+Z`.
The source controls select a rectangular area emitter and/or a uniform
environment, each with an imported scalar spectral table; the area source has
position, aim/up, dimensions, and explicit intensity/calibration. It accepts
relative SPD plus scale, or absolute SPD with calibration. It never supplies
an implicit white SPD or labels an RGB map as measured spectrum. Camera
controls include position, target, up, vertical field of view, image width and
height, spp, wavelength range/step, and seed.

The live geometry view is `AppearanceScenePreview.qml` with the registered
`AppearanceSceneCanvas` in `coat_sim/ui/appearance.py`. The canvas is a
`QQuickItem` that batches a neutral slab face, grid, axes, and area-source
wireframe into reusable `QSGGeometryNode`/`QSGGeometry` nodes in
`updatePaintNode()`. It uses Qt Quick's scene graph and public `QtQuick`
extension API; it does not use Qt Quick 3D, Qt 3D, Canvas Painter, a framebuffer
object, or a software full-image painter. Camera drags update a few line and
triangle vertices rather than rasterizing a complete image on the CPU.

The `previewScene` `QVariantMap` comes from the frozen rectangular or circular substrate,
current source settings, render dimensions, and controller-owned camera pose.
The draft's process-local XY coordinates remain in mm; scene points are
converted to metres once. The viewport is letterboxed to the render width /
height ratio. For each scene point `P`, the preview uses `forward =
normalize(target - position)`, `right = normalize(forward × up)`, and
`camera_up = normalize(right × forward)`. It projects view-space `(x, y, z)` to
`screen_x = width/2 + x·height/(2·z·tan(FOV_y/2))` and
`screen_y = height/2 - y·height/(2·z·tan(FOV_y/2))`. Thus horizontal FOV follows
the actual letterboxed aspect ratio while the user-entered `FOV_y` remains
vertical. The right basis is intentionally unchanged: an asymmetric
`sensor.sample_ray()` check confirms that Mitsuba's film X mapping compensates
its look-at frame so the preview's left, centre, and right points match final
render pixels.

Segments and filled polygons are clipped in view space against a near plane and
the four perspective sides before projection. The near distance is
`max(1e-9 m, min(substrate_extent, camera_target_distance) × 1e-5)`; it scales
with this small scene instead of using a fixed 1e-6 m renderer default. The
current controller accepts `0.001° ≤ FOV_y < 179°`, rejects a coincident
position/target and an up vector parallel to the view direction, and reports
invalid camera input instead of substituting an old pose or a 35° FOV. While
invalid, the scene canvas is hidden and render submission is disabled. Reset
updates `previewChanged` immediately. Numeric camera fields are compared by
numeric value so a no-motion gesture or equivalent text formatting does not
make a completed render stale.

The preview shows a neutral flat rectangular or circular slab, its XY grid and XYZ axes, a
valid rectangular area-source wireframe and aim line, and an environment state
label. When the physical substrate thickness comes from the frozen FilmDesign
snapshot, the displayed slab uses that value; an explicit geometry override
replaces it. Both choices create a flat, parallel back face for this prototype
and are marked as design or override geometry rather than a measured back-face
map. `1 mm` is treated as an ordinary valid FilmDesign value, never as a
sentinel. The preview never fabricates coating color, illumination, or spectral
appearance. Invalid light pose hides its marker without inventing a source.
Unsupported curved or non-rectangle/non-circle outlines show an explicit bounded message. Circular scene geometry uses 64 view-only chords; the physical render uses analytic disk and cylinder primitives.

`AppearanceScenePreview` sends `beginCameraGesture()`,
`orbitCamera(delta_x: float, delta_y: float)`,
`panCamera(delta_x: float, delta_y: float, viewport_height: float)`,
`zoomCamera(distance_factor: float)`, `commitCameraGesture()`, and
`resetCamera()` to the same controller that owns the render draft. Left drag
orbits; Control+left, right, or middle drag pans. Pinch translation pans and
pinch scale zooms. Vertical wheel / touchpad scrolling zooms and horizontal
scrolling pans. The current camera pose is previewed during a gesture and
position/target/up are committed to the draft when it ends. The “Use FilmDesign
value” control resets the substrate geometry field to the inherited snapshot
thickness; editing it switches to an explicit override. A changed frozen
context refreshes the inherited value only while that field remains in
inheritance mode; an explicit override is retained. Context replacement is
rejected while a render is active.

Camera gestures only update the local scene graph and camera draft. They do not
submit TMM work, call the render child, or create an asynchronous result; there
is no separate preview cancellation boundary.

### Scene graph modules and license obligations

The preview uses the already-installed `PySide6.QtQuick` bindings for
`QQuickItem`, `QSGGeometry`, `QSGGeometryNode`, and `QSGFlatColorMaterial`, plus
the existing Qt Quick / Qt Quick Controls imports. No package or Qt module is
installed for this change. Qt's Qt 6.11 licensing page offers LGPL v3 and
commercial options for eligible modules and identifies Qt Quick 3D as GPL v3
only in its open-source offering; this implementation removes `QtQuick3D` and
does not add a GPL-only module. The exact deployed PySide6 / Qt binary build and
its third-party components still determine the notices and license files that
must ship, so verify the packaged `LICENSES` and SBOM rather than inferring
license status from a Python import.

For an LGPL v3 distribution path, provide the applicable copyright notices and
license text, corresponding library source or the required valid source offer,
and the relinking / installation information that applies to the delivery
format; preserve users' ability to replace or relink the LGPL library. If those
conditions cannot be met, use an appropriately licensed commercial Qt build.
This paragraph records the dependency boundary, not a legal compliance
determination. See [Qt 6.11 licensing](https://doc.qt.io/qt-6.11/licensing.html),
[Qt for Python license inventory](https://doc.qt.io/qtforpython-6.11/licenses.html),
[Qt's LGPL / GPL obligations](https://www.qt.io/development/open-source-lgpl-obligations),
and the [Qt Quick scene graph custom geometry example](https://doc.qt.io/qtforpython-6.11/examples/example_quick_scenegraph_scenegraph_customgeometry.html).
Existing render start/cancel, manifest validation, and last-valid-image
ownership remain unchanged.

The context strip is read-only: selected process asset and its short SHA-256,
imported v7 surface, frozen FilmDesign layer IDs, captured-step count, and the
inherited substrate outline. Layer material and optical-set references remain
owned by the frozen FilmDesign snapshot. Each thickness row is labelled `沉积仿真逐层厚度场`.
The frozen FilmDesign physical substrate thickness is inherited as
`design_value` unless the user enters an explicit positive-mm override. A
reset control restores the inherited value; a new frozen context refreshes the
inheritance only when no explicit override is active. A back face with
no design layers is shown as bare substrate/air; non-empty opposite-face
layers without their own captured fields block submission. No substrate or
stack is selected a second time.

The visible phases are `ready`, `starting`, `running`, `cancelling`, `complete`,
and `failed`. The launch context is resolved before the window opens. Missing
v7 snapshot, missing captured fields, unknown `k`, out-of-range SPD, invalid
camera/light pose, missing inherited or overridden geometry thickness, bad
child output, bad manifest, and missing local Mitsuba runtime are explicit
errors; they never fall back to guessed spectra or nominal process thickness.
Invalid camera input hides the geometry canvas and disables render submission
until corrected or reset. When asset loading fails, the RayTrace action remains
disabled and its availability text shows the asset error, including the missing
per-step-field reason. If context loading or rendering fails, the input draft
remains editable and the last completed render remains visible. Focus and scroll
position persist across dialogs and panel reflow.

## Exact per-hit angular optics (2026-10-12)

Production BSDF evaluation uses `coherent_stack_rta` at each actual ray angle
and captured cell stack. Fixed 0.5-degree interpolation is not an accepted
accuracy contract for thick multilayers; its helper is diagnostic only.
No angular resampling or LUT table construction occurs in the production
BSDF. Exact grazing incidence retains the existing 89.999-degree cap.
Manifest `angular_optics` records this rule and the cap. Scalar TMM evaluation
uses the existing stable scalar recurrence, with the same input validation,
critical-layer limit and float64/complex128 arithmetic; wavelength arrays
retain the vectorized implementation. Errors still fail the job; no silent
coarse-angle fallback. Cancellation and output ownership are unchanged.

## Independent-wavelength CPU processes (2026-10-13)

`render_job(..., workers=1)` keeps the serial compatibility path. CLI
`--workers N` opts into N spawned CPU processes (1–64, further bounded by
wavelength count). Each worker receives the immutable normalized job once,
initializes its own scalar Mitsuba/BSDF, and caps the Dr.Jit CPU thread pool at
one to avoid nested oversubscription. The worker computes one wavelength at a
time with the unchanged seed `base_seed + wavelength_index`, exact angular
TMM and original spp. It returns one float32 plane plus index, channel-equality
error and seed. At most N tasks are outstanding. The parent alone writes the
indexed spectral memmap, performs ordered wavelength integration and publishes
final output. Completion order cannot change wavelength order or seeds.

Cancellation is polled while waiting for each bounded set of jobs. Cancellation,
worker errors, or caller errors terminate and join only the owned pool; no
final manifest is published. No coarse-angle/precision fallback is allowed.
Worker copies cost memory proportional to N times the normalized scene;
parallelism is explicit until measured real-runtime acceptance establishes a
safe automatic policy. This CPU execution option does not claim GPU support.

## Progress and cancellation (2026-10-13)

`progress_callback` receives stage, completed/total wavelengths, elapsed_s,
estimated_remaining_s, fraction and estimate_kind. Initial ETA is null; early
values are rough, then update from mean completed-wave throughput. Integration
and output use finalizing with null ETA, not a false zero. Elapsed/remaining
values are informational only and never set a deadline. The existing UI status
and scheduler report carry these values without creating a new output file.
All post-memmap failures close the spectral mapping. Cancellation is checked
after CMF generation, before color output and immediately before the completion
manifest; no completed manifest is published for observed cancellation.

Real Windows parity found Mitsuba sample blocking depends on thread count. Both
serial and spawned paths now use Dr.Jit thread count 1. Matching seeds alone
is insufficient; the same 81-grid 86-wavelength fixture is byte-identical in
1/4/8-process spectrum, XYZ, linear sRGB and PNG after this correction.
The manifest records threads_per_process=1. This changes the old default
thread-pool sample arrangement, not the estimator or optical equations.

## CUDA integration (2026-10-13)

The window persists `backend=cpu|gpu_cuda` and `precision=native` for CPU or
`float32|mixed` for CUDA in its job-local frozen request. GPU selection starts
a task-local asynchronous isolated-runtime probe that executes a real CUDA
array; a missing runtime/device disables submission with its error. No GPU
request silently executes on CPU. `mixed` computes optical TMM/material/film
thickness in float64 while Mitsuba geometry/directions/film remain float32.
Neither GPU precision is CPU-equivalent or image-accuracy-qualified. The CUDA
manifest therefore retains `rendered_unqualified` with numerical refusal
counters and independent input/output digests. The UI and shared queue only
publish complete, diagnostic-passing assets.

The frozen v7 adapter keeps actual captured thickness fields, both optical
faces, material constants, source spectra, and the process-local flat circle
radius where applicable. A coated circle must explicitly record
`circle_boundary_sampling=captured_node_mean_with_local_nearest_fallback`.
This is a preview edge-thickness convention, not a measured edge. Other
shapes and source models are rejected.

The control request, local UI worker input, and freeze-child input remain
bounded to 16,000,000 UTF-8 bytes. Full captured layer fields in the frozen
envelope and scheduler job spec have a separate 60,000,000-byte bound; the
loopback RPC retains its 64 MiB body limit and checks the actual compact
serialized wire bytes before starting a daemon. Freeze output, persisted spec,
and GPU input-job readers check their respective byte bounds before loading.
The larger frozen-data budget preserves measured fields rather than sampling
or omitting them; it is not a rendering-time limit.

GPU cancellation is checked between synchronized wavelength frames. During
a long first-frame JIT the remaining time is unknown; the UI may kill only
its own child after a 10-second cancellation wait. Fraction and remaining
time are informational, based on completed wavelengths, and do not cap work.
A rejected/cancelled partial spectrum cannot become a completion marker.
The GPU kernel is in `coat_sim/appearance/gpu.py`; the former research script
is a compatibility entrypoint to the same module. See
[gpu-preview-contract.md](gpu-preview-contract.md) and
[appearance-circle-contract.md](appearance-circle-contract.md).
