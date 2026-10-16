# Project desktop adapters and rollout plan

Date: 2026-10-14. The delivered code is an **in-process headless routing
foundation**. Existing desktop entry points still use the legacy application
root and launcher-owned windows/assistant. Neither multi-project GUI isolation,
cross-process project ownership, nor a globally singleton assistant is claimed
as implemented by this slice.

## Additive lifecycle coordination seam (2026-10-14)

`coat_sim/project_lifecycle.py` adds `ProjectLifecycleCoordinator(host)` as a
headless, in-process adapter contract. Its `close_launcher(dismiss)` invokes
only the supplied launcher-presentation dismissal callback. Hiding the current
launcher is the safe desktop integration until assistant and project hosts move
out of the launcher process: destroying that process still destroys its
launcher-owned assistant and project windows.

Project adapters may call `register_project(binding, prepare_close)` with the
exact active `ProjectBinding`. A prepare callback returns `PreparedClose(commit,
cancel)` only after all dirty-window decisions are accepted and their validity
is held against fresh edits; it returns `None` on a user veto. The active-job
adapter provides the same permit shape to `request_exit_all(prepare_active_jobs)`.
The coordinator prepares **every** registered active project and then the
active-job policy before invoking any commit or shutdown request. Missing
project adapters, a veto, or changed session identities block exit; all
uncommitted permits are cancelled. A commit refusal blocks process shutdown.
During this transaction `ProjectSessionHost.exit_admission_freeze()` rejects
new project opens, module reservations, and operation admissions, including
opens attempted reentrantly from a close callback and opens whose store load
began before the freeze but has not yet published its session. This freezes
**admission**, not already-running document edits; adapters must still hold
their own document and job decisions stable. The admission gate is released
when graceful shutdown requests have been sent or exit is declined. It is not
a claim that processes have already exited, or that no new project can open
after the request returns while the supervisor is still shutting down.
This is an explicit adapter responsibility, not an assertion that current
editors already implement a stable two-phase close. If one commit has already
completed before a later commit fails, the coordinator cannot roll it back;
adapters should make commit infallible after preparing, and desktop integration
must test this assumption with real dirty windows and jobs.

`register_owned_process(role, key, retained_handle, request_shutdown)` accepts
only an explicit locally owned live process handle. The returned `ProcessLease`
includes a fresh generation; stale lease removal cannot erase a replacement,
including if the OS reuses its PID. Exit calls the registered graceful
shutdown callback for live, exact handles after all close commits, in
project-host, assistant, launcher order. It does not enumerate processes,
signal a PID, terminate a process, or touch remote compute services. An
accepted `ExitResult` means graceful requests were sent, **not** that child
processes actually exited. The desktop supervisor must wait for exit and
surface refusal/failure without force killing.
`ExitResult.shutdown_requests` records exactly the requests whose callbacks
returned True. If a later shutdown callback raises or refuses, its lease is
reported separately as `uncertain_shutdown`, with the error text on exception;
the supervisor must check that process's status rather than assume it stopped.

The coordinator is not an IPC server and enforces singleton ownership only
within its own process. Cross-process registration and project routing should
extend the existing authenticated loopback `UiControlClient`/`UiControlServer`
transport in `coat_sim/ui/control.py`, with explicit project ID, canonical
root, session generation, role and verified OS process start identity. Never
trust a PID or cached endpoint alone. No second unaudited discovery socket or
credential store is introduced by this lifecycle seam. Existing launcher
shutdown and assistant ownership remain legacy behavior pending that rollout.

## Implemented identity and routing boundary

`coat_sim/project_session.py` imports neither Qt nor the pending project store.
`ProjectSessionHost(store_factory)` accepts a read-only `ProjectStoreView`
protocol with `root: Path` and a detached `manifest: dict`. Internal routing
continues to use the canonical root as identity; production persistence wiring
validates the selected `.coatproj` file and stages that captured store for root
registration. Refreshes reopen the store's actual `project_file`, including an
arbitrary filename prefix, rather than guessing a default descriptor. The
external launcher and standalone `--project` entry accept the descriptor file,
not a directory. The manifest identity is `manifest["id"]`, the
existing persistence schema; resolved dependency graphs use `project_id`.

The interface truth is the dataclasses and signatures in that module:

- `open_project(root) -> ProjectSession`; `sessions() -> tuple[ProjectSession, ...]`.
- `store_for(binding: ProjectBinding) -> ProjectStoreView`.
- `reserve_module(project_id, module_key) -> ModuleReservation`.
- `attach_window(route, window)`, `window_for(route)`, `release_window(route)`.
- `routes(project_id) -> tuple[ModuleRoute, ...]`.
- `capture_operation(project_id, operation_id) -> OperationBinding` and
  `resolve_operation(binding) -> ProjectStoreView`.
- `close_project(binding, prepare_close=callback) -> bool`.

The project store owns saved document membership and the normal dependency
root. The host owns routing identities and opaque window references only.
`ProjectBinding` contains the explicit project ID, canonical root and fresh
session generation. `ModuleRoute` adds module key and fresh reservation ID.
Identity dataclasses are immutable. A canonical root has one registered owner;
two active roots with the same persisted ID are rejected until the copied
project is explicitly cloned/reidentified. Opening the already registered root
preserves its working owner instead of rereading/replacing it. Requested linked
paths still go through the store factory's validation; canonicalization does
not conceal a linked root from that validator.

Empty projects have film designer as their entry module, but registration does
not create a window, load recovery, resolve a second dependency root, or start
calculations. The injected persistence factory is responsible for validating
and resolving the designated configuration. Settings/about are application
scoped and rejected by project-module routing.

### Pending opens, cancellation and asynchronous returns

A reservation is published before the adapter starts expensive UI construction.
Repeated/reentrant requests return the same route, including during loading.
Only the first caller (`created=True`) constructs the module. Later callers
activate the attached window, or record activation intent in the UI adapter
while pending. A repeated module selection carries no implicit replacement
document or payload; explicit import/load is a separate operation.

Failed/cancelled construction calls `release_window(route)`. Accepted native
close does the same. Attachment requires the exact live generation/reservation,
so a late result after cancel, close or reopen cannot attach to its successor.
An old destruction callback returns `False` and cannot erase the new slot.
The adapter cleans up rejected newly constructed windows on the GUI thread;
the headless host never destroys native objects. No computation is cancelled
by releasing a presentation registration.

Project operations explicitly capture their target at admission. Later focus
changes do not affect that binding. Missing, closing or reopened sessions fail
with structured `ProjectSessionError(code, message, project_id)`, never fall
back to another project. This is target routing, not operation deduplication,
execution, revision validation or frozen-input storage. Those belong to the
headless document/task services and remain required before mutating writeback.
Resolving returns the bound store at that instant, not a revocable write lease.
The document service must revalidate binding/revision and serialize its commit
against close; retaining an earlier store reference cannot bypass that check.
The registry lock alone does not make project close and persistence atomic.
Closed-session results may be retained by their service; reopening does not
authorize an old operation to write into the new working session.

### Closing presentation versus closing a project

Launcher presentation close has no host mutation. A project close requires an
explicit `prepare_close(binding, routes) -> bool` callback implementing the
complete adapter policy: inspect every dirty document, ask/save/discard/cancel
as appropriate, and perform accepted native-window cleanup. While this callback
runs, new admissions for that project are blocked; other projects remain usable.
`False`, an exception, or a non-boolean decision keeps the registered project
alive. The callback must decide before destructive cleanup; any slots it
explicitly releases cannot be reconstructed by the registry on rejection.
An accepted close unregisters only that project. No method kills processes,
closes the launcher/assistant, or signals a remote compute service.

The required callback is an adapter contract, not evidence that the registry
can independently detect dirty editors. Dirty state/undo is still owned by
document sessions such as `FilmDesignSession`; this host does not invent a
second editable document model.

## Existing coupling verified before UI edits

| Boundary | Current code and behavior | Required adapter |
| --- | --- | --- |
| Resource versus data root | `ui/launcher.py` derives `PROJECT_ROOT` from installation and passes it to Quick controllers; `core/api.py` derives data paths from its root | Separate immutable runtime paths and inject project services |
| Module singleton | `LauncherBridge._existing_window/open_window` use instance-local module keys; `_open_requests/_pending_qml` are instance-local | Keep one bridge per project host; bind its host identity at construction |
| UI control | `WindowRegistry._adapter`, pending/error dictionaries and dispatch use module keys; `UiControlClient` discovers installation-root runtime metadata | Coordinator must resolve an explicit project binding before calling that host's registry |
| Launcher lifetime | `LauncherWindow.qml` intercepts close and calls `shutdown`; `main()` wires last-window close to shutdown | A project-manager launcher accepts presentation close; explicit application exit is a different command |
| Assistant lifetime | `main()` creates `DesktopAssistantController`; launcher shutdown invokes its shutdown; `AssistantRunCoordinator` owns provider worker tree | One independently supervised assistant process, separate from launcher and project hosts |
| Film material/storage root | `FilmDesignService` constructs `FilmMaterialLibrary(root)` and lazy SQLite at `data/film_design/library.sqlite3`; `FilmMaterialLibrary` reads `data/materials`, `.overrides`, and usage JSON | Read explicit project optical records; project documents save through `ProjectStore`, without global-catalog mirroring |
| Saved document root | The old service uses standalone typed-file paths; project core uses immutable `objects/<generation>` plus manifest dependencies | A document-ID/session adapter, not a path substitution |

Detailed current locations: `ui/launcher.py:408` (bridge), `:714` (opening),
`:1161` (Quick controller creation), `:1722` (shutdown), `:1833` (entry point);
`ui/control.py:1003` (window registry); `core/film_design_service.py:338`
(service construction); `core/film_materials.py:46` (material library);
`ui/film_quick_controller.py:372` (service injection already supported).

All paths above are beneath `coat_sim/`. Line numbers describe the inspected
2026-10-14 base and may shift after edits; class/function names are authoritative.

## Recommended process architecture

Choose **one project-host process per open project**, plus independent
project-manager launcher and globally shared AI processes. Each project host
owns one `LauncherBridge`/Qt engine as module presentation machinery, with no
launcher or assistant created inside it. This preserves the existing
per-module singleton naturally within one project while isolating Qt state,
controller workers, process globals and failure boundaries across projects.
Existing module keys need not acquire a project-ID parameter in every internal
helper at once. The external coordinator and command boundary still require
explicit project identity.

A single process with one bridge/engine per project would reduce interpreter
and Qt overhead, but its process-wide settings, module-level task reapers,
assistant ownership and service/global path assumptions would remain shared.
It would require a broader isolation audit before two writable projects are
safe. It is not the recommended first integration path.

This choice has an unmeasured startup/RSS cost. Before enabling it by default,
measure cold first-project and warm second-project readiness, film first-view
readiness, and incremental RSS on Windows and Linux. Record interpreter/Qt
versions and the actual process count. Lazy editor creation must remain; do
not preload every dependent editor to hide this cost. No timing or memory
claim is made by the headless tests.

Explicit path domains must precede process rollout:

- Installation resources: code, QML, icons, bundled read-only references.
- Global state: UI/global settings, assistant preferences and existing secure
  credential access. Project hosts do not create wider persistent access.
- Project inputs: manifest, managed document/material objects and reviewed
  draft recovery. Ordinary open uses only the designated deposition config.
- Task/result state: service-owned frozen inputs, queues and result assets;
  no automatic copy into input manifests or global catalogs.

Do not implement these domains with `os.chdir`, environment-wide active-project
variables, or changing an imported module's `PROJECT_ROOT` at runtime.

## Next implementation slices, in order

1. **Project film adapter, headless first.** Add `core/project_film_service.py`
   after the core store merges, and define a document-session service protocol
   from the real `FilmDesignSession`/Quick controller calls. Inject project
   optical resources rather than constructing the global catalog at a new
   root. Use explicit document IDs plus expected working revision for saves
   and undoable optimization writeback. Loading/saving a managed film goes
   through the store; immutable object files are never edited directly.
   Cover empty material lists, material-ID conflicts, two projects using the
   same ID with different data, frozen optical snapshots and workflow settings.
   Built-in defaults needed by a new film must be explicitly available as
   validated project resources before saving/computing; missing bindings fail
   visibly. Define expression/saved-design storage ownership explicitly rather
   than silently opening the old installation SQLite database.

2. **One project host, opt-in GUI.** Add `ui/project_host.py` with explicit
   runtime paths and `--project <root>` entry. Construct the store/session and
   injected project film service; use the existing
   `FilmDesignQuickController(service=...)` seam. Route initial entry to film
   designer, attach only after a successful async open, and release on accepted
   close. Keep the old standalone launch path as an explicit compatibility
   route until project modules are adapted. Inject equivalent document services
   for deposition/geometry before declaring those modules project-enabled.

3. **Shared module header and project-manager launcher.** Add a reusable Qt
   Quick module selector to the existing operation-button row with project
   name and the host-bound open/activate callback. It opens no replacement
   document and displays pending/failure state. Validate repeated clicks,
   cancelled construction, focus/scroll restoration and dirty-close rejection.
   Replace launcher module buttons with project management/global settings
   only, and make its close dismiss only that presentation.

4. **Coordinator and independent AI.** Extend the existing audited local
   control transport with explicit project ID/generation routing; do not add
   a parallel unaudited IPC credential/discovery path or expand access. A
   coordinator maintains project root/ID ownership, launcher and assistant
   registrations, and verified process identity/generation. Enforce the
   cross-process singleton there; the in-process host and the store's short
   mutation lock do not establish it. AI project management selects an
   explicit target, captures it before dispatch, and returns project/document/
   revision metadata. Ordinary focus changes never supply a missing target.
   Assistant crash/launcher close leaves project processes running. Explicit
   Exit application requests dirty decisions from known application-owned
   local hosts, respects rejection, and closes only verified managed local
   processes. It must not kill arbitrary Python processes or remote jobs.

5. **Desktop acceptance and enablement.** Run the acceptance flows in
   `project-session-contract.md` in actual Windows GUI/process environments:
   two projects/same modules, repeated project open, launcher close/reopen,
   global AI singleton/crash recovery, changed focus during AI/optimization,
   late stale writeback, explicit exit with dirty work and project relocation.
   Enable the project-manager entry only once those observations pass.

## Verification for this slice

`tests/test_project_session_host.py` covers pending/reentrant/threaded dedup,
two projects with identical module types, root/ID conflict, explicit target
capture, cancellation/retry, stale open completion and destruction callbacks,
old session results after close/reopen, strict close policy/rejection/error,
other-project availability, requested-link validation and imports without Qt
or the project store. These are registry/adapter-contract tests, not GUI or
cross-process acceptance. Existing singleton, film initialization and assistant
lifecycle tests remain useful regression suites when the actual adapters land.
