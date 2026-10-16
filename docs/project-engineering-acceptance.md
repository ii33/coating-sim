# Complete project engineering acceptance

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

Date: 2026-10-14. Status: implementation in progress after dev.20261014.04.
This checklist is the release gate, not a claim that the remaining behavior exists.

## Ownership and boundaries

- The GUI-free project store is the persisted owner; project-scoped workspaces own drafts.
- A canonical project has one writable session owner and one window per module.
- Different projects never share mutable drafts, dependencies, undo or result writeback.
- The launcher presents project management and global settings. Closing it does not exit projects or the independent shared assistant.
- The assistant is globally singleton for this application's user context; every project action explicitly identifies its target.
- Normal opening follows exactly one designated deposition configuration. It never restores the previous session. Abnormal recovery is offered separately and requires acceptance.
- External inputs are copied into the managed project with complete typed dependencies. No live external references or implicit global-library fallback.

## Desktop adapter contract

The shared desktop host owns module reservations, existing-window activation, navigation and view lifetime. Resource and compute adapters receive the explicit ProjectFilmWorkspace (host and binding), module key and an engine; they obtain their own project-scoped workspaces through the same host. Factories return the window, controller, QML context and component. Controllers expose their binding and an explicit disposal/close policy; the host must not assume every legacy bridge implements the same close method.

Construction failure releases only its own reservation and disposes partial objects. No second owner is constructed on repeated or asynchronous opens. The module selector shares the existing top operation row. The project identity remains visible. Expensive views are lazy.

## Release evidence required

1. Create an empty project, open its film designer and save a managed design.
2. Create/import process, machine, source, mask, substrate and material resources; external dependency closures are copied atomically. Global/project transfer is explicit in both directions.
3. Select a default deposition configuration, close and reopen; its dependency graph populates the correct drafts without starting calculations or opening every editor.
4. Open two projects, including identical module types and overlapping local resource names/IDs. Verify independent edits, dirty flags, undo, materials, settings and results.
5. Film design, simulation and optimization share the same project draft; settings persist together in the film design document. Late optimization results cannot overwrite newer edits or another project.
6. Deposition, ray tracing and appearance use project-owned inputs, work directories and result identities; queued calculations freeze their admitted inputs. Closing views does not cancel service-owned tasks.
7. Reopen every module without duplicate instances; navigate between modules without silently replacing drafts.
8. Close/reopen launcher while projects and assistant remain available. Restart an assistant after a crash without discarding projects. Verify singleton across multiple launcher starts.
9. Assistant Exit All checks dirty drafts and local active jobs, supports cancellation and only requests graceful exit of registered application-owned processes. Remote services and unrelated Python processes are unaffected.
10. Recover an abnormal draft only after explicit review. A clean restart uses saved defaults; stale generations and moved/copied projects fail safely or use an explicit reidentification workflow.
11. Exercise project creation/inspection/import/export/default selection through headless CLI and AI/MCP with explicit project identity, expected revisions and structured errors.
12. Move a fully saved project directory and reopen; no original external paths are required.
13. Run static checks, collection audit, quick/contract/algorithm/Qt tiers, and Windows native multi-window/process acceptance. Record unavailable environments as unverified, never as passed.

## Historical .04 baseline

Version .04 implements the project store and film-only desktop entry, frozen default-film drafts, isolated film workspaces and headless lifecycle contracts. Its CI is green. Resource/compute adapters, independent assistant process, abnormal recovery and native process acceptance are the remaining delivery scope. Update this section only with verified integration evidence.

## Integration evidence, 2026-10-14 (not final release acceptance)

The integration branch now wires the resource/compute modules, independent assistant
process, registered application exit, and explicit section-scoped recovery. The
following results precede the final corrective commits; all affected gates must be
rerun on the immutable publication candidate:

- Quick tier: 945 passed, 9 skipped; 195 subtests.
- Algorithm tier: 528 passed, 6 skipped; 285 subtests.
- Contract tier: 1156 passed, 8 skipped; 432 subtests.
- Qt tier: 655 passed, 2 failed; 2532 subtests. The two failures are in
  `test_process_editor_navigation_qt.py` (compact layout and dependency navigation).
  They remain release blockers until corrected and rerun.
- Follow-up compute/resource/application/recovery cohort: 67 passed.
- Real isolated POSIX offscreen launcher/assistant smoke: passed, including delayed
  authenticated registration, safe admission, exact hidden-launcher activation,
  independent launcher dismissal, dirty-exit cancellation and graceful Exit All.

Windows native validation of the published `.04` baseline is separate from this
integration candidate. Passing old-baseline tests cannot establish acceptance of
new unpublished changes. No public release or repository visibility change is
authorized until the README, complete engineering acceptance and publication
audit are ready.

## Cloud candidate validation, dev.20261014.05

Final runtime candidate plus test-only type/qualified-selector corrections:

- Collection audit: 2454 items across 242 modules; no collection errors or empty test modules.
- Quick tier: 967 passed, 9 skipped; 195 subtests.
- Algorithm tier: 528 passed, 6 skipped; 285 subtests.
- Contract tier: 1184 passed, 8 skipped; 435 subtests.
- Qt tier: 672 passed; 2532 subtests. Both earlier navigation failures are fixed.
- Tiers overlap; these counts must not be summed as unique tests. Skipped hardware,
  provider or environment-specific cases are not verified by this cloud run.
- Reviewed static prune: zero new issues, zero fatal syntax; 149 resolved baseline
  diagnostics removed without adding debt. A separate final check follows pruning.

The remaining release gate is native Windows acceptance of this exact new
version, followed by publication/privacy/license review. The `.04` Windows
baseline result is not a substitute. Repository visibility and Release publication
remain deferred.

## Windows compatibility acceptance, 2026-10-14

The native Windows repair branch at `d7e2fe07f9e89596a47310715a3261a259fe120a`
completed its full suite with exit 0: 2436 passed, 27 skipped, 3310 subtests
passed, zero failures/errors; 2463 ordinary cases in 242 modules, 933.62 seconds.
Environment: Windows, PowerShell 7.6.5, Python 3.12.14, PySide6/Qt 6.12.0,
pytest 9.1.1. The inherited build-label environment variable still named the
`.05` base; the independently recorded actual source SHA above is authoritative.

The skips comprise 19 platform-specific cases, six opt-in real-GPU cases and
two cases requiring the vendored Mitsuba runtime. This is not a real GPU
rendering acceptance or a clean-machine dependency bootstrap acceptance.

Native windows covered two isolated projects, all 13 module types, per-project
single instances, default dependencies, navigation, dirty-exit cancellation,
and launcher dismissal preserving project windows and the independent assistant.
Fifteen native HWND screenshots were retained with the Windows test evidence.
Test-owned schedulers were stopped through their APIs; original configuration
and task-file hashes remained unchanged.

The `.05` candidate CI passed before promotion, but the post-promotion main
quick tier reproduced one recovery-list click failure (run 38567590481).
Independent diagnosis at 640x480 found the archived button outside the clipped
viewport; the old click produced no new call. The `.06` test fix uses actual
wheel scrolling, waits for settled layout/motion, requires the complete target
inside the viewport, and asserts one new call with the exact archived identity.
Ten fresh-process runs, including default and small-window flows, passed
(60 tests). No product QML routing change was required.

The `.06` integration also preserves all 27 reference-manifest byte sequences
across autocrlf checkouts, makes owned-process fixtures portable to Windows
venv launchers, and fixes numeric generator source values and GPU JSON newline
serialization. The native full run predates the stronger cloud recovery-click
test and the added runtime regression module; it does not verify those later
test-only additions. Final integrated Linux/static/CI results are recorded
separately. Public visibility and Release publication remain deferred pending
final acceptance and publication/privacy/license review.

## Integrated .06 cloud verification

- Full Linux run: 2471 passed, 11 skipped, 3311 subtests passed, 12 warnings,
  747.60 seconds, exit 0. The runtime was fixed at `6bfeb8e`; subsequent changes
  were documentation, test-only typing/style, and removal of resolved static debt.
- Final affected typing/style follow-up: 31 passed on the corrected test files.
- Static gate after reviewed prune: 646 sources, zero new diagnostics, zero
  unresolved baseline changes, zero fatal syntax. Eight resolved diagnostics
  were removed; no new debt was accepted. Existing baseline totals remain
  mypy 4679, qmllint 2518, Ruff 1564, so this is not a claim of zero total debt.
- The 18 added runtime regressions cover canonical UTF-8/LF serialization,
  nonfinite input refusal, locale-formatted defaults, edit/focus precision and
  invalid input preservation. Five pre-fix mutation probes failed as expected.

GitHub verification must be tied to the final remote commit, including the run
triggered by main promotion; a green candidate run alone is not final evidence.

## Cross-platform viewport fixture follow-up, .07

The exact `.06` main commit `e5e7fc57c40cf4fb2c0767dacb4bb97e83418a4c`
passed GitHub Tests and Static analysis. Its final Windows delta check passed
63 cases and skipped one Unix-socket case, but failed the newly added small-window
scroll assertion: with compact Windows metrics the target was already visible.
This was a test precondition defect; the 18 runtime regressions all passed.

The `.07` test-only correction adds three distinct preceding reviewed candidates
and asserts the target starts fully below the clipped viewport before sending
real wheel events. Selection still requires exactly one new call and exact
record, generation, revision and section identity. Ten fresh-process module runs
passed (70 tests); two recovery flows passed at four font/density combinations
(eight further cases). Native Windows and CI must confirm the published follow-up.
Runtime production files are unchanged from `.06`.
