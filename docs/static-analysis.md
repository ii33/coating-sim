# Python and QML static analysis

Static analysis is a separate CI stage from pytest. It finds suspicious source
constructs and type/QML contract mistakes; it does not execute the physical
algorithms, validate their accuracy, or replace the test tiers.

## One command, reproducible dependencies

Use Python 3.12 and a separate virtual environment:

```bash
python3.12 -m venv .venv-analysis
.venv-analysis/bin/python -m pip install -r requirements-analysis.txt
.venv-analysis/bin/python tools/run_static_analysis.py check
```

On Windows use `.venv-analysis\Scripts\python.exe` for the last two commands.
The runner uses the active interpreter for Ruff and mypy; `--python-executable`
can select a separate dependency interpreter. Both environments must be the
reviewed versions. Missing tools, incomplete dependencies, wrong tool versions,
failed processes, missing Qt import metadata, incomplete file coverage, and
unparseable output exit with status **2**. Diagnostic debt is never inferred
from an analyzer failing to execute.

The tools are Ruff 0.17.0, mypy 2.4.0, and native qmllint from PySide6/Qt 6.12.0.
The analysis dependency lock is a separate reproducible Python 3.12 snapshot;
it does not narrow the application's production ranges or change the separate
sandbox's version pins. Dependency changes need review and a new verified
analysis snapshot because shipped stubs can change diagnostics.

Ruff checks E4/E7/E9/F/B: import/statement correctness, undefined names, unused
code, and common bug patterns. It makes no automatic fixes and does not run a
formatter. mypy is selected because it runs in the same Python packaging flow,
can read the dependencies of a separate interpreter, and uses the PySide6
package's shipped type descriptions. It checks untyped function bodies and
normally follows imports. There is no global `ignore_missing_imports`, blanket
Qt suppression, or automatic stub installation. The initial report includes
unresolved optional GPU imports and legacy tests/tools that manipulate their
runtime `sys.path`; those limitations remain visible as debt.

qmllint gets explicit Qt and project import paths, ignores external settings,
and checks all discovered QML files with `-W 0`. `CoatUi`'s two Python-registered
types have analysis-only `qmltypes` descriptions under
`tools/static_analysis/qml/CoatUi`. They describe the real QObject/QQuickItem
base classes and exported properties; they are never added to runtime import
paths. Context properties supplied by Python remain dynamically typed: an
`unqualified` warning is visible, but is not by itself proof of a runtime bug.
A Qt-module/import-description failure always fails the runner, even when old
diagnostic debt exists.

## Coverage and confinement

Git enumerates tracked sources and non-ignored untracked sources. The scanner
checks `.py`, `.pyi`, `.pyw`, and `.qml`; a new source file is covered before it
is added to Git. Python windowed entry points are checked separately by mypy
to avoid collisions with same-named `.py` modules; already-scanned imported
modules are not double-counted. No source file allowlist is maintained.

The runner records the discovered source-file set and a digest of its exact
contents before analysis, then verifies both again after the analyzers and
fingerprint generation. If a source is added, removed, or edited while the scan
is running, the run fails with exit **2** instead of comparing mixed-snapshot
diagnostics; baseline creation and pruning are not performed for that run.

Directories containing generated/runtime data, `other`, environments, build
outputs, configuration/credential stores, and caches are excluded explicitly.
Source symlinks and paths escaping the repository are rejected. Reports and
baselines must remain inside the repository. The scanner does not import
application modules or execute their top-level code; mypy reads dependency
stubs and qmllint reads QML import metadata.

## Visible legacy debt, blocking new debt

The first full baseline records every diagnostic individually with its tool,
path, rule, message, enclosing Python symbol, normalized source-line anchor,
and multiplicity. Its SHA-256 fingerprints omit line/column positions, so
inserting blank lines does not reset debt. Copying a faulty construct increases
its count and is blocked. A changed statement or enclosing symbol requires
review rather than silently inheriting a different statement's allowance.
Syntax diagnostics cannot be accepted into the baseline. The initial fixes
also include annotation-only `Settings` resolution and separate semantic IDs
for the front/back film-design hover handlers; the latter avoids Qt 6.12
qmllint's duplicate-ID diagnostic across the two inline delegates.

```bash
# Full diagnostics and counts, without comparing to historical debt:
python tools/run_static_analysis.py scan
# Gate: fails on new diagnostics, syntax defects, or stale resolved allowances:
python tools/run_static_analysis.py check
# Remove resolved entries only. It cannot add/increase an allowance:
python tools/run_static_analysis.py prune --reason "Reviewed resolved debt"
# Create/replace debt only after explicitly reviewing a full scan:
python tools/run_static_analysis.py baseline --reason "Specific review justification"
```

`baseline` and `prune` are rejected when `CI` is set. CI never updates the
baseline. Check exits **1** for new diagnostics, unresolved syntax defects, or
resolved allowances awaiting explicit pruning; it exits **0** only for an
executed full scan that matches the reviewed debt. Pruning still exits **1**
when new diagnostics remain. Never baseline in-progress feature code to make
CI green. Fix new diagnostics in the owning change and prune only resolved
historical entries during integration.

## Evidence

The default output is `artifacts/static-analysis/latest/` (ignored runtime
evidence), including readable stdout/stderr, each analyzer's exit status,
qmllint's native JSON, and `report.json` with the full source list, per-tool and
per-rule counts, locations, fingerprints, new/resolved counts, and gate status.
Reports carry a producer tag and a per-run ID. Reusing an output directory clears only recognized `report.json`/`failure.json` products; other files are preserved, including the CI wrapper’s open `analysis.log` and its `exit-code.txt`. The wrapper remains responsible for updating its exit-code file after the runner returns. Unrecognized files with those reserved names, symlinks, failed cleanup, or failed atomic report writes fail closed. A failed run never inherits an earlier success report, and a successful run does not retain an earlier failure report.

### Multiplicity and `is_new` in JSON

`new_count` is the exact excess over the old fingerprint-group budgets:
`sum(max(current_count - baseline_count, 0))`. `resolved_count` is the converse.
The full diagnostic location list is retained. A row's `is_new: true` means its
**fingerprint group** exceeds the old budget, not that this particular location
can be uniquely identified as the newly introduced occurrence. Therefore,
counting all rows with `is_new` can exceed `new_count` when a group contains both
previously allowed and additional matching occurrences. Those rows are
candidate locations to inspect; no allowance is increased by this labeling.

For an exact per-file/per-rule new count, group current diagnostics by
`fingerprint`, subtract the corresponding tracked baseline entry's `count`,
clamp at zero, and aggregate those excesses. Every fingerprint includes path
and rule, so that aggregation is unambiguous. For example, a group with two
allowed occurrences and three current occurrences has one new diagnostic
budget excess; all three locations are shown as candidates. Line/column
positions still do not enter the fingerprint or expand the budget.

Execution/configuration failures produce `failure.json` when the output directory is recognized and writable, and exit 2. An unsafe or unwritable output path still fails closed without overwriting unrelated user files. CI uploads
this evidence even when the gate fails. Tool diagnostic exit statuses remain
visible: Ruff/mypy normally return 1 for findings; native Qt can return 255
for warnings under `-W 0`. A valid, complete diagnostic payload is required
before those codes can be classified as findings.

The reviewed initial counts and reproducible verification commands are in
[static-analysis-evidence.md](static-analysis-evidence.md). The tracked baseline
is diagnostic debt, not a statement that these legacy sources are clean.

Official references:
- [Ruff lint rules and diagnostic exit codes](https://docs.astral.sh/ruff/linter/)
- [Ruff configuration](https://docs.astral.sh/ruff/configuration/)
- [mypy imports, shipped stubs, and dependency interpreters](https://mypy.readthedocs.io/en/stable/running_mypy.html)
- [mypy command-line options](https://mypy.readthedocs.io/en/stable/command_line.html)
- [Qt qmllint JSON, import paths, and warning categories](https://doc.qt.io/qt-6/qtqml-tooling-qmllint.html)

## Pinned third-party reference data

The scanner excludes upstream source files only inside the immutable packaged
`coat_sim/core/resources/refractiveindex/versions/<commit>-<hash>-v1/source/`
snapshot. They are retained as provenance data and are never imported or run by
the application. The manifest identity, canonical file-list hash, individual
file size and SHA-256 must match; missing, unlisted or changed source fails
closed. Application/provider code and neighboring project sources remain scanned.
This boundary adds no diagnostic allowance to the baseline.
