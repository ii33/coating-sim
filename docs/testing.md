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
