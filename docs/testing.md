# Test collection and execution

The supported test runner is `tools/run_tests.py`, which invokes pytest from
the active Python interpreter. Pytest discovers both module-level `test_*`
functions and `unittest.TestCase` methods. The test directory is the source of
truth for full collection; no hand-maintained test file allowlist is used.

```bash
python tools/run_tests.py quick
python tools/run_tests.py algorithm
python tools/run_tests.py contract
python tools/run_tests.py qt
python tools/run_tests.py all
```

The specialized tiers are assigned by an explicit module manifest in
`tests/conftest.py`. A module can belong to more than one tier. Every test item
from a module outside the specialized manifests is assigned to `quick`, so a
new test module remains collected and covered by the tier union automatically.
`all` removes tier filtering and is the canonical full run. `--collect-only`
uses the same collection policy without executing tests:

```bash
python tools/run_tests.py all --collect-only
```

Put raw pytest arguments after `--` to keep runner options distinct and reject
misspelled runner flags. For example:

```bash
python tools/run_tests.py all -- -k optical --junitxml=artifacts/optical.xml
```

This forwards selection and report options to pytest. A collection JSON report is available through
`--collection-report PATH`; it records every nodeid, item tier, module coverage,
parameterized-item count, AST test-definition counts, and the
`load_tests` bridge count.

All commands set Qt to the offscreen platform and software Quick/RHI renderers.
The default network guard blocks Python socket DNS lookup and outbound
connections during collection and test execution, while allowing loopback TCP
and Unix-domain sockets for local IPC. Collection and module imports always
remain guarded. A live provider or remote opt-in temporarily opens Python
socket access only while the matching marked test body runs, then reinstates
the guard. It does not provide OS isolation and does not intercept every
native extension, Qt C++ transport, or child process.

When PySide6 is installed, pytest configuration retains one `QApplication`
before importing test modules, after headless/offline defaults and private
QSettings paths are established. Qt cannot upgrade a preexisting
`QCoreApplication` or `QGuiApplication` to the widget-capable singleton: such
a process fails with a clear usage error. This avoids module-order-dependent
font/QML/widget crashes. The tradeoff is application startup even for a
core-only selection in an environment that has PySide6. Without PySide6,
configuration leaves core-only subsets usable and GUI tests report their
normal missing dependency errors.

The same private Qt test root also owns `XDG_STATE_HOME`, including scheduler
lifecycle registration. The existing autouse setup/finally restores it after
environment contamination; each process creates its own root. Tests must not
write scheduler registry metadata into the real user's `~/.local/state`.

Use `tests.qt_test_guard.qml_item_window(item)` for a QML item's input surface.
On the verified PySide6/Shiboken 6.12.0 Linux build, calling
`QQuickItem.window()` creates a reciprocal wrapper ownership cycle while the
C++ QObject parent remains unchanged; later GC can crash in
`Shiboken::Object::removeParent`. Reading attached QML `Window.window` avoids
that ownership mutation and preserves a separate `Popup.Window` as the real
keyboard/wheel target. The helper rejects missing QML contexts and expression
errors. This is an observed version-specific binding defect, not evidence
about every supported Qt version. GC remains enabled, and teardown is not
skipped or replaced by retaining deleted objects.

The runner loads `tools.pytest_qt_lifecycle` for every tier. After a Qt-marked
case has completed its entire pytest protocol (including fixture teardown and
release of pytest's case arguments), the plugin calls Python `gc.collect()`.
It does not delete Qt objects, invalidate wrappers, drain Qt events, change
selection, suppress exceptions or retry tests. Existing window/engine ownership,
destruction spies and business assertions remain the fixture's responsibility.

An isolated PySide6/Shiboken 6.12.0 reproduction showed a borrowed dynamic QML
`QMetaObject` wrapper surviving destruction of its C++ owner while an invalid
QObject wrapper remained in a Python reference cycle. A later `childItems()`
returned that exact old Python metadata object when the C++ address was reused.
Collecting the now unreachable cycle at the completed-case boundary released
the metadata wrapper before the next scene. This is a test isolation measure,
not a repair of PySide's native pointer cache: reachable objects and multiple
engines inside one case still require correct explicit ownership. Python GC
stays enabled; stderr and pytest's unraisable-exception warnings remain visible.
The local raw reproduction and controls are retained under
`artifacts/film_synthesis_20261020/gpu-forward-20261020/delivery-closeout-20261020/publication/ci-followup/closure-r4/`.
See the [pytest protocol implementation](https://github.com/pytest-dev/pytest/blob/9.1.0/src/_pytest/runner.py)
and [Python cyclic garbage collection](https://docs.python.org/3/library/gc.html).

### Qt module process boundaries

Case-boundary GC is not a native-wrapper-cache reset. The 2026-10-29 contract
run still returned live `QMetaObject` wrappers from `QQuickItem.childItems()` in
two different modules after earlier cases had completed. This does not prove
which owner/reference caused those particular cache entries, and no upstream
fix or safe binding-level invalidation has been established. In particular,
`shiboken6.isValid()` is not proof that the wrapper has the requested Qt type.
The official [Shiboken ownership documentation](https://doc.qt.io/qtforpython-6/shiboken6/typesystem_ownership.html)
describes the separate native/wrapper lifetimes. Its
[6.12.0 binding manager](https://github.com/pyside/pyside-setup/blob/v6.12.0/sources/shiboken6/libshiboken/bindingmanager.cpp)
contains address-based wrapper lookup (including an untyped overload); that
is consistent with the hypothesis, not attribution of these CI failures to
a particular upstream issue.

`run_tests.py` also loads `tools.pytest_qt_modules`. If the selected collection
contains Qt-marked cases, the controller executes each Qt-marked module in a
fresh Python interpreter. All selected items from that module use that process,
including items without an individual Qt marker. Contiguous non-Qt modules can
share one process. Collection order is retained, so a custom ordering that
revisits a Qt module creates another fresh process at that boundary. No child
inherits initialized Qt state through `fork`; each runs `python -m pytest`.
Fixtures scoped to a session therefore have a session per process, while
within-module lifecycle and repeated-operation tests remain intact.

The controller first performs ordinary full pytest collection and selection.
Workers import only their planned test modules (and normal dependencies), apply
that exact selected node-ID plan, and reject missing membership. They do not
retry failed cases or apply a failure-specific allowlist. `--collect-only` and
the existing collection-audit/tier assignment are unchanged. Ordinary test,
fixture and subtest reports use pytest's public serialization hooks and are
replayed to the controller's existing terminal, duration and JUnit reporters.
The transport retains tuples as well as lists, including skip report metadata.
Skip, xfail, xpass, setup/teardown errors and subtest outcomes remain visible.
JUnit suite properties and per-case properties/attributes are retained through
private temporary worker journals. That bridge uses pytest's internal
`_pytest.junitxml.xml_key` interface, verified with pytest 9.1.1 (the repository
requires pytest 9.1 or newer). An incompatible internal API is an explicit error,
not a silently selected compatibility path.
Workers alone append original exception/disposal diagnostics to the requested
JSONL path; controller replay does not duplicate those events or overwrite the
aggregate JUnit/collection files.

`-x` and `--maxfail` use one remaining-failure budget across process boundaries.
Last-failed/failed-first selection and ordering are decided by the controller's
canonical collection, not reapplied from a changing cache in each worker.
`--stepwise` execution is explicitly unsupported and raises a usage error;
it is not silently downgraded to module-level stopping. These contracts cover
this repository's supported runner, not arbitrary external pytest plugins.
Without fail-fast, ordinary assertion failures do not prevent later modules
from running. Collection errors, internal errors, interrupts and process crashes
terminate the run with their nonzero status; Unix signals map to `128 + signal`.
If SIGINT reaches only the controller, it kills and reaps its active worker;
on POSIX the worker has a dedicated process group, whose other members are
terminated too. The interrupted batch is recorded as interrupted, never passed.
No timeout or hidden runtime budget is introduced. A malformed internal worker
launch fails explicitly rather than disabling isolation through an inherited
environment flag.

With `--junitxml=DIR/junit.xml`, `DIR/module-processes.json` records the original
selected node IDs, every batch, child PID, elapsed time, started node IDs and
exit code. A pending batch after explicit fail-fast/interrupt/crash is visible,
not reported as passed. Alternatively pass `--module-isolation-report=PATH`
after the runner's `--` separator. Startup/import work is repeated once per
process: this is a deliberate finite overhead, not a guarantee about overall
suite duration. Process isolation prevents cross-module native-state reuse; it
does not repair PySide itself or excuse unsafe ownership within a test/module.
Terminal test reports are replayed at each batch's end rather than continuously.
While a batch is active, its banner and process audit show that it is running;
the requested exception/disposal JSONL still flushes worker evidence immediately.
An unchanged terminal test name alone is not evidence that a batch has hung.

Provider, remote-service, and actual-GPU checks must carry the corresponding
`live_provider`, `live_remote`, or `real_gpu` marker. Such checks are skipped
unless explicitly enabled:

```bash
python tools/run_tests.py contract --run-provider
python tools/run_tests.py contract --run-remote
python tools/run_tests.py algorithm --run-gpu
```

Use live network flags only in a controlled environment with the intended
credentials, endpoint, data, and spending limits. Fake providers, local `MockTransport`
tests, CPU algorithms, and GPU-fallback behavior do not use these markers and
remain in the ordinary tiers.

The CI workflow runs a full collection audit and the four named tiers. Any
collection or test failure remains a failing CI job; the runner does not
ignore import errors, rewrite test results, or silently narrow full
collection.

## Initial collection evidence

At the initial baseline, `pytest --collect-only` found 1003 items but exited
with status 2 because 13 existing test modules could not import test support
modules absent from the checkout. The standard-library `unittest` loader
discovered 786 runnable cases and produced 13 `_FailedTest` placeholders for
the same import errors. It does not collect the module-level test functions;
pytest does. In particular, `tests/test_scheduler_service.py` defines a
`load_tests` hook that creates 14 `FunctionTestCase` cases for unittest
discovery. Pytest directly collects those same top-level functions once, so
the project runner does not invoke unittest discovery in parallel.

The raw collection output, nodeid JSON, and unittest discovery summary are
stored locally under the ignored `artifacts/test-baseline/` directory. Those
logs predate the test-only support helpers subsequently added to this branch;
they preserve the original import failures and are not a current suite status.
They are audit evidence, not a claim that the test suite passed. The initial
shared-capability tracking counts were a historical snapshot,
not a current completion report. The restored tracking file explicitly retains
its old states; current bounded findings and still-required evidence are linked
from the residual audit register（历史验证记录，未随公开源码发布）.
Collection success or an offline tier pass cannot close hardware, provider,
publication, retirement or missing historical-evidence gates.
