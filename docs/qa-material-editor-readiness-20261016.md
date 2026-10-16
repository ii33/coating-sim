# Material editor readiness QA, 2026-10-16

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

## Scope and cause

Test-only fix: `feb3eda94d3d4d6709fa120347bb967255547b2f`, based on
`04626ea`. Only `tests/test_project_editor_context.py` changed. Production
code, snapshot/write fences, resource state and version are unchanged; this
increment does not advance the `.01` candidate version.

The published `69f6cb48` CI contract and Qt tiers each failed the same four
material-window cases. Each failure occurred before the expected initial
snapshot was available; teardown reported 16 QML items still being created.
The CI assertion truncated the snapshot dictionary, so its exact `reason` was
not visible in that log.

`MaterialLibraryHost.qml` creates its primary editor with asynchronous
`incubateObject`. The shared `client_for` fixture previously processed events
and waited a fixed 20 ms before returning. That does not establish
`Component.Ready` or completion of the popup's `onOpened`/`prepareOpen`.

A controlled **real** `QQmlIncubationController` reproduced the state:
`editorLoading=true`, `editorOpenRequested=true`, no `editorInstance`, and
snapshot `available=false`, `reason=pending_confirmation`. After allowing the
original incubator to finish and the primary popup to open, the existing flags
cleared and the same read interface returned `available=true`. Repeated reads
did not change the draft; original GUI initialization could legitimately load
the initial material. No cross-project owner contamination was identified.

## Minimal fix and causal regression

For materials only, the test helper now waits for the existing primary editor
to exist and report `opened=true`, both host loading/open-request flags to be
false, and the original bridge's `loading=false`. The existing event loop is
processed in short steps. A monotonic five-second deadline or an explicit
load failure fails the test with all sampled readiness flags.

This does not force synchronous creation, change focus, confirm or close a
modal, alter dirty state, retry arbitrary unavailable snapshots, skip a test,
or weaken a production fence.

The regression deliberately holds the real incubator for 120 ms **after the
registered window exists**, so compilation time cannot consume the delay.
Restoring the old 20-ms-only behavior makes that regression fail with
`editorLoading=true` and unfinished creation at teardown. The corrected helper
passes it. Six additional cases verify bounded, diagnostic, nonmutating failure
for loading, an outstanding open request, a missing/closed editor, bridge
loading, and explicit creation failure.

## Results and limits

- Both affected test modules: **57 passed in 38.49 s**, including all four
  originally failing material cases.
- After tightening only the regression's deliberate-delay clock, its final
  seven new cases: **7 passed in 3.09 s**. The exact final test file is also
  covered by the complete contract run below.
- Focused mypy: no issues in the changed file. Ruff: passed.
- Independent read-only review: no blocking findings; no production changes.
- Exact CI runner/order, `tools/run_tests.py contract`: **2355 passed, 8 skipped,
  482 subtests passed, 1376 deselected, 2 failed in 679.20 s**. JUnit confirms
  all four original material cases and all seven new cases passed without skips.

The complete local tier is **not green**. Its only failures are the two known
cloud-environment EPERM cases, neither modified nor skipped by this fix:

1. `test_application_process_smoke.py::test_detached_project_runtime_ownership_across_launcher_lifetimes`:
   line 453 finds no registered runtime owner; both launcher outcomes report
   `[Errno 1] Operation not permitted`. Its assertion does not expose a deeper
   exception stack.
2. `test_launcher_last_window_activation.py::test_last_clean_project_window_close_releases_lease_and_reopens_new_generation`:
   line 163 receives the same error. The captured stack explicitly traverses
   `UiReadyChannel` / `multiprocessing.Listener` / `socket.socket` and raises
   `PermissionError: [Errno 1] Operation not permitted`.

These local environment failures do not constitute a passing full-tier result.
Remote CI for the integrated candidate remains a separate required check.
No new Windows, GPU or provider acceptance is claimed here.

Final test-file SHA-256:
`cabae3a5906d529e237c92e6e31316aaf943466b452ab74764ba2e01ecf51c6e`.

Local evidence: `/tmp/material-readiness-fix-report.json`,
`/tmp/material-readiness-contract-full.log`, and
`/tmp/material-readiness-contract-junit.xml` (temporary execution artifacts,
not committed). The isolated worktree was clean after the test-fix commit.
