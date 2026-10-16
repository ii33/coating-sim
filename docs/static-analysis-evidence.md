# Static analysis verification evidence

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

Date: 2026-10-14 UTC. Source base: `b490ce87665221e04cd3da221b646c54e560e86c`.

This is an initial legacy-debt scan, not a clean-source or algorithm-accuracy claim. All listed tools executed successfully and their raw diagnostic return codes remain in the local evidence.

## Frozen analysis environment

- Python 3.12.14
- Ruff 0.17.0; mypy 2.4.0; native qmllint 6.12.0
- Installation: `python -m pip install -r requirements-analysis.txt`
- Exact 100-package dependency snapshot: `requirements-analysis-lock.txt`
- `python -m pip check`: no broken requirements
- Production and sandbox requirements were left unchanged

## Full historical source inventory

- 533 sources: 465 `.py`, two `.pyw`, 66 `.qml`
- Python type checking includes untyped function bodies; QML has explicit Qt/CoatUi import metadata
- Excluded: generated/runtime data, environments, caches, `other`, config/credential stores
- New non-ignored, untracked source files are included

Per-tool diagnostic counts:

- mypy: 4757
- qmllint: 2888
- ruff: 1590

Full per-rule counts (no rule-level blanket suppression):

- mypy:arg-type: 831
- mypy:assignment: 242
- mypy:attr-defined: 1617
- mypy:call-arg: 11
- mypy:call-overload: 143
- mypy:dict-item: 36
- mypy:func-returns-value: 13
- mypy:has-type: 38
- mypy:import-not-found: 62
- mypy:import-untyped: 9
- mypy:index: 378
- mypy:list-item: 9
- mypy:method-assign: 49
- mypy:misc: 64
- mypy:name-defined: 14
- mypy:no-redef: 17
- mypy:operator: 138
- mypy:override: 8
- mypy:return-value: 35
- mypy:str-unpack: 2
- mypy:type-var: 2
- mypy:typeddict-unknown-key: 6
- mypy:union-attr: 787
- mypy:unused-ignore: 3
- mypy:var-annotated: 243
- qmllint:block-scope-var-declaration: 3
- qmllint:missing-property: 81
- qmllint:renamed-type: 1
- qmllint:unqualified: 2796
- qmllint:unused-imports: 7
- ruff:B007: 14
- ruff:B008: 5
- ruff:B009: 7
- ruff:B017: 2
- ruff:B018: 3
- ruff:B023: 125
- ruff:B039: 1
- ruff:B904: 12
- ruff:B905: 170
- ruff:E401: 7
- ruff:E402: 128
- ruff:E701: 287
- ruff:E702: 604
- ruff:E731: 17
- ruff:E741: 1
- ruff:F401: 151
- ruff:F811: 1
- ruff:F821: 13
- ruff:F841: 42

## Interpretation and targeted corrections

- Removed 13 Ruff F821 annotation diagnostics in `core/api.py` with a TYPE_CHECKING-only import of the real `simulator.Settings`; future annotations were already enabled. This does not import the simulation engine at runtime.
- Renamed the two film-design hover handlers and each matching Tooltip reference to front/back semantic IDs. Qt 6.12 qmllint reported duplicate IDs across the two inline delegates; this avoids its syntax diagnostic without claiming a proven runtime defect.
- `CoatUi` metadata was checked against the actual PySide static metaobjects: PreviewCursor exports target QQuickItem*, active bool, normalShape int; AppearanceSceneCanvas exports sceneData QVariantMap, surfaceColor QColor, gridColor QColor, with matching QObject/QQuickItem bases.
- QML unqualified/context-property debt remains visible; Python-fed context properties cannot be fully proven by qmllint.
- Many mypy attr-defined/union/arg-type items involve legacy dynamically assigned Qt properties. Missing imports include optional GPU modules and legacy runtime sys.path aliases. They were not globally ignored.
- Ruff B023 closures are review candidates, not all proven bugs: sampled API/GPU callbacks are consumed synchronously inside the owning loop. B018 URL port expressions intentionally trigger urllib validation. The mutable ContextVar default still merits focused ownership review; this tooling change does not silently alter that behavior.

## Executed verification

- Runner and new runner test source: Ruff and mypy both returned 0, with no diagnostic debt for either file.
- Offline runner contracts: 45 tests and 87 subtests passed. They cover line-position drift, duplicate budgets, new diagnostics, resolved/pruned debt, CI write refusal, complete source coverage, excluded and escaping/symlink paths, missing tools, process/JSON failures, QML import coverage and fatal syntax, and windowed Python entrypoint handling.
- Targeted application regressions: 19 tests and two subtests passed for film design Qt/reference pages, source overwrite confirmation, and saved source film stacks.
- A plain interpreter without Ruff/mypy returned 2 and wrote failure.json, rather than reporting a pass.
- CI baseline-write attempt returned 2; baseline SHA-256 before/after was identical.
- Full clean check returned 0: zero new/resolved/fatal diagnostics.
- Two real untracked source probes (Python assignment + undefined name, QML unknown property) were discovered and returned 1 with exactly four new diagnostics: mypy assignment, mypy name-defined, Ruff F821, qmllint missing-property. No allowance was added.
- Removing both probes and repeating the full check returned 0 with the original counts.
- CI wrapper compatibility: with analysis.log still open for writing and an existing exit-code.txt, repeated failure→success runs preserved both wrapper-owned files, and the log stream could continue appending.
- Actual output-directory reuse: a missing-tool run returned 2, followed by a complete check returning 0 in that same directory; failure.json was absent, and the published report carried its producer and unique run ID.
- Failure↔success replacement, unknown user-file protection, legacy-product migration, symlink/directory refusal, cleanup errors, atomic partial-write/replace failures, and baseline/prune publication failures are covered by the 45 offline contracts. Failed writes return 2 without publishing a success report or destroying the previous baseline.

Canonical commands:

```bash
python tools/run_static_analysis.py check --output artifacts/static-analysis/clean-check
python tools/run_tests.py contract -- -k static_analysis_runner
python -m pip check
```

Raw reports live in ignored `artifacts/static-analysis/` directories. CI uploads the corresponding analyzer outputs, exit codes, full JSON inventory, and summary. Do not upload generated evidence or credentials into source-control commits.

## Integration-budget interpretation example

The separate `c8f286a` integration audit after TFCalc's static repairs had 541
sources, `new_count=125`, `resolved_count=45`, and no fatal syntax. Its exact
new budgets were 55 for the pending layout repairs and 70 for the spectrum
change. FilmSpectrumPlot had 26 excess diagnostics but 50 candidate locations
marked `is_new`, because groups also contained pre-existing allowed matching
occurrences. This is a multiplicity budget, not a count of every marked row.
No feature diagnostics were admitted to the initial baseline; owning changes
must fix the excess and integration may explicitly prune only resolved debt.
