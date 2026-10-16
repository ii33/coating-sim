# Film GUI object lifetime and palette callback evidence

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

Date: 2026-10-15 UTC.

## Scope and remaining CI boundary

This change closes two directly measured lifetime defects: test-owned film
windows surviving their fixture, and an application-global palette lambda
retaining and calling a deleted chrome host. It does **not** establish the
root cause of the earlier CI SIGSEGV. A new candidate's complete Qt CI result
is still required; the original crash was not reproduced locally before or
after these changes.

No calculation, material, file-format, window close/reuse, or numerical
semantics change. Production `WA_DeleteOnClose=False` remains in place. GC,
test assertions, AF_UNIX transport, and failing CI exit statuses remain enabled.

## Original CI sequence

Tests run 38703028340, Qt job 116315562389, candidate
`83e104d072973fbf59da809cc97d58485c93aaef` terminated with SIGSEGV/139.
The main Python thread was in the unknown-k mouse calculation test's
`QTest.qWait`; the faulting worker was collecting garbage during material
JSON decoding. No native object identity or native backtrace was available.
Those Python frames alone do not prove the faulting object's origin.

The collection artifact's manifest reports 798 Qt items, but does not include
225 explicitly Qt-marked items outside that manifest. Actual `-m qt tests`
selection was 1023 of 3107 collected items. The original ordered collection
was verified locally against all 3107 artifact nodeids. Consequently:

- The fifth executed item, matching the first `F`, is detached application
  ownership smoke. Bubble Escape is the ninth item.
- Unknown-k mouse calculation is item 123, matching the 122 completed result
  symbols before the fatal error.
- The same smoke's separate contract-tier result reports an AF_UNIX pathname
  length error. The Qt job itself did not emit its failure traceback before
  crashing, so that separate result is not presented as a Qt traceback.

## Measured defects and bounded changes

### Test-owned Qt graphs survived fixture teardown

A weak-reference/scalar-only diagnostic followed each fixture's two windows,
their two chrome hosts/widgets/engines, and the unparented calculation signal
object: nine QObjects per fixture. A QObject destruction callback captured
only a label, never the watched object. GC callbacks recorded thread identity
without changing GC thresholds or retaining collected objects.

In the original 17-item module prefix, live C++ objects after successive
teardowns were 9, 18, ... 153. All 153 remained valid at session finish, with
no destruction receipts before application shutdown. Earlier TestCase weak
references had already cleared, so this was not merely the active test's
ordinary ownership. Worker GC ran naturally, including generation 2, but this
baseline did not demonstrate tracked Qt destruction on a worker.

The fixture now closes only its own windows, joins its own calculation
executor so completion callbacks finish, processes queued completion events,
and explicitly schedules GUI-thread deletion. It separately deletes the
unparented signal object. Exact `destroyed` receipts and weak-reference
`isValid` checks verify all nine C++ objects are gone before the temporary
material directory is removed. A bounded DeferredDelete/event drain fails
the test if any expected destruction is missing.

The real-ZnS schematic test's additional local window uses the same test-only
deletion helper, including its chrome graph and any created schematic dialog.
The palette regression's separate QWidget is also explicitly deleted. No
global top-level-window scan deletes objects belonging to another fixture.

### Deleted chrome host remained on the application palette signal

Explicit fixture deletion exposed a second defect:

`app.paletteChanged.connect(lambda *_: self.paletteChanged.emit())`

The application-owned lambda retained the deleted `_WindowChromeHost`.
After the original 21 tests passed, a process-local palette change and restore
emitted two palette signals and produced 84
`RuntimeError: Signal source has been deleted` tracebacks at this connection.
That diagnostic correctly exited 1 rather than ignoring the errors.

The sole product change connects `paletteChanged` to the host's
`@Slot(QPalette)` bound method. Qt now has the QObject receiver context and
disconnects it when the host is destroyed. Live-host forwarding and the
production close/reuse path remain unchanged; other connections were not
rewritten.

The added regression verifies live-host forwarding on the GUI thread,
deletion of the probe QWidget/host, release of the host weak reference, and
no subsequent callback or Python error. A diagnostic-only negative control
restoring just the old lambda fails this regression because the application
signal still retains the deleted host: 1 failed, 2.11 seconds. It does not
change checked-in source or disable GC.

## Verification

Host: Python 3.12.14, PySide6/Shiboken 6.12.0, pytest 9.1.1, numpy 2.5.4,
matplotlib 3.11.2, scipy 1.18.1, glibc 2.41; Qt offscreen with software Quick/RHI
rendering and pytest plugin autoload disabled. Original CI used Ubuntu
24.04.5/Python 3.12.15, so these are not identical hosts.

- Standalone Bubble module: 11 passed, 3.87 seconds.
- Original full-import Qt prefix through item 123: 122 passed, one detached
  smoke failure due to this sandbox's AF_UNIX `EPERM`, 18 subtests passed,
  84.38 seconds; unknown-k passed, no SIGSEGV.
- Fixture-only revision of that same prefix: 122 passed, the same sandbox
  smoke failure, 18 subtests passed, 88.27 seconds; no SIGSEGV.
- Final full module: original 21 plus one palette regression, 22 passed,
  16.50 seconds, exit 0. Palette change/restore emitted two signals with
  zero bytes of Python callback stderr.
- After explicit QApplication type narrowing, qualified DeferredDelete enums,
  and a nullable weak-reference local-variable correction requested by the
  whole-repository static check, the full 22 tests and palette change/restore
  passed again: 16.70 seconds, exit 0, two palette signals, zero callback
  stderr bytes. No ignore or static-baseline change was used for these fixes.
- Final full-module lifecycle trace: 198/198 tracked fixture QObjects emitted
  `destroyed` on MainThread; all 22 teardown snapshots had zero live tracked
  C++ objects. GC stayed enabled at thresholds `(700, 10, 10)`; the worker
  still naturally ran 108 generation-0, eight generation-1, and one
  generation-2 collection. Three Python wrappers were later released on a
  worker, approximately 666 ms **after** their corresponding C++ objects had
  already been destroyed on MainThread.
- Final original-Qt-prefix verification: 122 passed, the same sandbox AF_UNIX
  `EPERM` smoke failure, 18 subtests passed, 88.19 seconds, exit 1; unknown-k
  passed and no SIGSEGV occurred. The full collection contained 3127 items,
  with 1024 selected for Qt; the original first 123 nodeids were identical.
  Two pytest cache-write warnings reported host disk exhaustion after test
  execution; JUnit and the test log were written. These warnings are retained
  in the evidence and are not counted as successful cache writes.

The final prefix limiter retains full module collection/import, then verifies
the first 123 selected Qt nodeids exactly match the original CI sequence.
New regression/ready-channel nodes after that boundary are recorded rather
than silently substituted into the original prefix. Raw execution logs and
temporary data are not software-source deliverables.

The permanent module regression can be run with the repository's normal
headless defaults:

```sh
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software QSG_RHI_BACKEND=software \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_film_design_gui.py -vv
```

Passing these bounded checks confirms the measured cleanup and palette
defects were addressed. It does not close the original native-crash
investigation or replace complete Qt CI on the next frozen candidate.

## Windows synthetic drag and real-input coverage

The .10 Windows acceptance on Windows 原生环境 retained its historical four-module
result: **40 passed, one failed, 17 skipped**. The sole failure was
`test_shared_project_chrome_drag_controls_and_close_cancel`; an independent
rerun reproduced the synthetic QTest drag's unchanged `(80, 80)` position.
This result is not relabeled as all passing.

Native diagnostics confirmed that the synthetic press hit
`windowChromeTitleDrag` and `startSystemMove()` returned true, while
`GetAsyncKeyState` reported that the OS left mouse button was not down.
A separate real-input `SendInput` check on the visible foreground,
non-maximized window moved it from `(80, 80)` to `(195, 138)`, with matching
Win32 rectangle movement. The mouse button was released and the original
cursor position restored. All three owned diagnostic processes retired;
the real-input process exited 0. This establishes real Windows movement for
that checked path, without treating a QTest event as an OS-level button press.

The platform adaptation changes tests only:

- The existing deterministic QTest geometry check temporarily makes only
  that window's native `startSystemMove()` return false. The actual production
  fallback performs the movement, and the original changed-position assertion
  remains. Native-capable platforms also assert one attempted delegation;
  offscreen/minimal assert that no OS delegation was attempted.
- The original titlebar hit, theme/chrome, double-click maximize/restore,
  minimize/restore, caption-button maximize/restore, unsaved-close cancel, and
  final-close assertions remain in place. No skip was added.
- A separate true-return unit check uses the real window with controlled
  press/move/release data. It controls only the Python native-capable branch
  selector and native method result, verifies one delegation across two move
  events, and asserts that `window.move` fallback is never called. It also
  checks accepted events and release-state reset. This mock delegation is
  **not** evidence of real OS movement.
- Both tests verify that the patched native method and its instance-override
  state are fully restored. Production drag, native/window state, and normal
  close/reuse behavior are not modified by this adaptation.

After this test-only change, the cloud Linux offscreen full module reports
**23 passed, 18.03 seconds, exit 0**, including the retained comprehensive
control test and new true-return unit branch. This is a separate result from
the historical Windows 40/1/17 run and from the real-input Windows check.
