# Scoped libraries and project information: focused acceptance

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

Date: 2026-10-16. Implementation base: `26f0c6f`.

## Verified final focused run

Qt 6.12 / Python 3.12, offscreen software rendering:

```
python -m pytest -q tests/test_scoped_resource_libraries.py \
  tests/test_project_runtime_information.py tests/test_project_runtime_management.py \
  tests/test_project_application_lifecycle.py --disable-warnings
```

Result: **34 passed**, 14.96 s.

- Real global category switching, no all-categories option, actual material-use
  filters, multi-role/legacy profiles and unclassified optical-only records.
- Real optical-only export to the global library preserves unspecified use.
- Lazy per-project library route reuse, current-project-only catalogs, two-project
  isolation, explicit import/export dependency closure and unaffected editor input.
- Exact-instance current-context reads, distinct project snapshot identities,
  stale-snapshot rejection, read-only capability/schema and editor-write refusal.
- Library last-window close preserves retained dirty content; publication blocks
  native/project close and application-exit readiness without partial view loss.
- Project name/summary/notes persistence, cancel and failure retention; stale
  generation/revision and unavailable ownership rejection; descriptor-only writes.
- Unsaved management information participates in project/last-window/global-exit
  review. Cancel retains text, Return to save identifies the exact management
  form, explicit discard closes without writing. Switching management projects
  requires an explicit discard-or-return decision.
- New text during application-exit review invalidates earlier approval. Ready
  freezes management input and rejects stage/clear setters; cancel restores input
  and retains descriptor bytes and pending information.

An earlier resource/navigation/global-exchange batch passed 93 tests. Independent
coordinator verification also passed 21 new-library/information tests and 60
existing management/navigation/last-window tests before the final exit hardening;
these overlapping runs are not added into a single test count.

## Static and visual checks

Ruff and targeted mypy passed for 19 changed/new Python sources excluding the
pre-existing-debt `mcp/server.py` and `film_quick_controller.py`. The four changed
resource/management QML files have zero qmllint diagnostics. `git diff --check`
passed. Existing baseline debt and later base-branch static cleanup are preserved.

The actual project-library render was inspected at 680 and 1100 logical pixels,
with the shared operation bar and resource/information panes. Existing global
large-font/dark-theme focused regressions passed in the earlier batch.

Full aggregate CI, native Windows rendering/process acceptance and the user's
interactive preview are separate checks; this focused report does not claim
those passed. No algorithm, numerical semantics or persisted resource format changed.
