# Project-scoped deposition compute contract

This contract describes the existing deposition MainBridge adapted to one explicit project session. It does not change the project manifest or document schemas, calculation algorithms, or standalone-window defaults.

## Ownership and inputs

- `ProjectComputeWorkspace` is the single compute-adapter owner for one `(ProjectSessionHost, ProjectBinding)` generation. The host retains it under `compute_workspace`; a reopened project gets a new binding and a new workspace.
- The workspace exposes the project root, the bound project store/resource service, the root-restricted numerical `CoatService`, operation fencing, and checked project-owned path/import helpers. It never consults the foreground project, application-wide design folders, or a global film library.
- Project compute input is either the selected managed deposition configuration and its resolved process closure, or an explicitly selected external process copied into the project store. A managed process is resolved by document ID to the store's validated `input_snapshot`; its canonical object path is provenance only. Standalone external design paths are never reopened for project execution.
- Configuration names and calculation parameters are stored through the current managed deposition-configuration schema. Project document membership is the source of truth for configuration choices. A selected default is used only when its complete process/input-snapshot graph validates.

## State ownership and APIs

- The bound workspace owns the project store, configuration selection, and project identity. `MainBridge` owns editable deposition fields, current process/config selection, display state, and one worker-observer lifecycle. The queue service owns admitted jobs; a submitted job owns its frozen input snapshot and output artifacts. A close-preflight may temporarily freeze editing/admission; releasing the preflight unfreezes it.
- `load_default()` returns the explicit selected-root plan (including unconfigured state); `load_configuration(id)` resolves only an in-project deposition configuration; `configurations()` enumerates managed configuration membership.
- `save_configuration(process_document_id, parameters, name/id)` atomically saves a managed `.cdep` document and makes it the next session's opening root. The legacy `make_default` flag is accepted but no longer opts out. It never writes a global recipe. See [last-saved configuration policy](project-recent-configuration-contract.md) for unordered batches and import boundaries.
- `has_dirty_draft` compares editable deposition fields and selection with the successfully loaded/saved baseline. Closing its presentation window retains that draft in the workspace; reopening restores it after controller initialization. `mark_draft_saved()` is called only after successful managed load/save. Project close must explicitly review or discard an unsaved draft, including one whose window is closed.
- `capture(operation_id)`, `accepts(operation)`, and `metadata(operation, extra=None)` capture and verify the project session generation. Metadata puts project ID, generation, project root, configuration ID, and operation ID inside the queue-persisted `origin` record for each admitted operation. While editing is frozen, capture and new-job admission are rejected and result writeback is not accepted. `active_jobs()` reports registered task descriptors; `update_job(..., service_owned=...)` records verified ownership transfer from a local submitter to the queue service. `draft_signature()` deterministically identifies selected configuration and editable parameters for close coordination.
- `owned_path()` rejects paths outside the bound root, including symlink/junction escapes. `destination()` rejects any output destination outside the root. `import_asset()` copies an explicitly chosen external input below `data/imported/<category>/` using a content-derived prefix, rejects links, and does not overwrite existing content.

## Saving, loading, and errors

- Project Save/Load controls enumerate managed configuration documents, preserve process/configuration membership, and persist editable settings to the selected project configuration. Process picker imports use the managed store import API before selection.
- An empty or unconfigured project reports that state without falling back to `data/`, a standalone process, application defaults, or the global library. Missing or invalid selected dependencies fail closed; users can explicitly import a process and save a configuration.
- An invalid document, stale project revision, path escape, symlink, missing dependency, or closed/reopened session is surfaced as a failed operation. It must not alter a different project, global data, or a newer project's state.

## Asynchronous results and cancellation

- The bridge captures project operation identity and the current editable revision before submitting. Queue metadata records immutable project/configuration provenance; all queue state, history, and simulation artifacts are rooted in the project.
- Progress and completion are accepted only for the current job and still-open captured project generation. A callback from a closed/reopened generation is discarded.
- Editable settings remain enabled while work runs. If inputs change after submission, the job still represents its frozen snapshot, but a late completion may not replace the current edited inputs or publish as their result. Preserve the prior result and report that the finished result belongs to an earlier input revision.
- Cancellation stops observing/cancels according to the existing queue protocol; it never implies that an admitted job's persisted snapshot or queue history has been deleted. Detaching a window observer does not cancel a service-owned task; persisted scheduler results remain inspectable.
- Optimization uses a separate disconnect event to stop client polling, never its cancellation transport. Remote admission remains a local task until its receipt is verified. An `observer_failed` or `detached` terminal client descriptor means the local observer ended, not that the scheduler job was canceled; its recorded job identity stays available for queue reconciliation.

## Verification boundary

Tests use headless project stores and offscreen Qt. They cover managed default/configuration resolution, project-local imports and output roots, concurrent project isolation, close/reopen fencing, stale revisions, and editable-input late completions. No physical GPU, remote HPC service, or external film solver is required for contract tests.

## Film optimization and typed compute views

`FilmOptimizationQuickController(..., project_context=workspace)` receives the
same project-owned editor and optical service as the designer. It cannot replace
its source with another project's service. Each admitted optimization captures a
host operation binding before submission; progress, completion, and candidate
application require the original live generation. Existing design revision and
pending-edit epoch checks remain the atomic writeback boundary. A candidate can
be saved as a new managed film; optimization and simulation configuration remain
inside that film's `workflow_settings`. No global optical catalog is consulted.

`create_project_compute_view(workspace, module_key, engine, parent=None,
navigation=None, geometry_settings=None)` returns window/controller/context/
component. The project desktop owns reservations and navigation. Controllers
are disposed through `dispose_project_compute_controller`, which detaches
service-job observers without cancellation. Local spectrum/render work exposes
`localWorkRunning` and `cancelLocalWork`; the desktop resolves explicit wait or
cancel-and-wait before destroying an in-process/subprocess owner. Modules are
`main`, `film_optimization`, `ray_trace`, `film_appearance`, and `compute_queue`.
The appearance entry initially selects a project-owned deposition asset when no
frozen context is present; it never invents geometry or film defaults.

Ray tracing and appearance copy explicitly selected external assets into the
bound project's inputs before use. Saved request/result destinations must belong
to that project. Optical film import uses the managed project service and its
frozen optical records. Queue pages and result/cancel actions check the frozen
origin's project identity; an old generation's jobs remain historical but may
never update a reopened project's editor. A late result may only populate the
view which admitted it while its generation remains live.

The project queue's typed result routes re-read the selected job and verify its
project ID/root before using the captured workspace's desktop navigation.
Optimization inspection also checks the payload against that persisted row.
Historical plotting objectives are separate from editable optimization settings;
inspection cannot apply a candidate or mutate the designer. Ray-trace artifacts
must match the selected verified viewer artifact inside this project's scheduler
download cache. Both routes reuse the project's registered module window.

Reusing a film companion after its designer closes/reopens reconnects it to the
current same-project designer QObject and retained edit session. Same-session
spectrum observer rebinding preserves settings; settings still persist to that
project edit owner when its designer window is closed. Independent preview
settings and unsaved optimization candidates keep their replacement confirmation.
Activating a stale historical optimizer view does not silently clear its candidate.

Successful appearance and ray-trace view completions are retained during an exit
preflight and replayed if it is cancelled. Accepted exit detaches these clients;
completed files and admitted service jobs remain in their originating project.
Compute recovery validates first, and restores previous selection, context,
history references and the previous draft if a presentation restorer fails.

### Project spectrum result envelope

The local spectrum calculation writes
`data/results/film_simulation/<operation_id>.json` with
`kind="coat_sim.project_film_simulation_result"`, `version=1`, project ID,
generation, operation ID, frozen FilmDesign/material/settings input, public
numerical result fields, and an error string. Internal plot/phase caches are
excluded. Wavelength remains nm, R/T/A remain unitless fractions, and spectral
units remain the existing result's explicit source units. No numerical
recalculation occurs when viewing an artifact. This envelope is a project
provenance convention, not a new optical model. Failed/stale generation
callbacks never replace current display state. Local calculations use frozen
material providers across every polarization; completion is deferred while an
exit preflight freezes editing and replayed only if that exit is cancelled.

### Project film synthesis desktop route (2026-10-20)

The existing film optimizer's `synthesis` mode captures current project objectives and explicitly selected materials through `ProjectFilmService`; the same project `ProjectComputeWorkspace` admits the `film_synthesis` task. This is independent of the assistant business-root route. The run coordinator remains the sole observer/cancellation owner. Generation, revision, edit-epoch and attempt guards reject stale callbacks after edits, close/reopen or source destruction. Historical typed queue inspection checks the persisted row and originating workspace and stays read-only.

A completed candidate requires the existing replacement confirmation and atomic film-session application. The existing optimizer plot previews frozen target samples. Live submissions capture a separate editable material closure for project save/application, while historical results retain only their numerical snapshot. Frozen records must cover every actual candidate document reference; incomplete closure rejects save/application without reading the later live catalog. Undo restores the original design/provider while preserving subsequent independent workflow material additions. Standalone `.cfilm` save rejects drifted/missing catalog references because that format does not embed material snapshots. No project/document schema changes are introduced. See [GUI contract](film-synthesis-gui-contract.md) and independent Qt/project closure tests `test_film_synthesis_gui_review.py` and `test_film_physical_generator_frozen_apply.py`.
