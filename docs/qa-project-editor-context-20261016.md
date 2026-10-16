# Project editor read-context completion QA, 2026-10-16

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

## Scope and provenance

Incremental implementation on isolated base `d086353`, for cherry-pick onto the
main candidate. The original runtime worktree was not edited. This increment
adds real current-owner read snapshots for source, substrate, machine, mask,
process, materials, main, ray_trace, film_appearance and compute_queue, alongside
the existing three film modules. It adds no editor-write capability and leaves
Discussion/Plan mode read-only.

The strict six-field project target, window instance, GUI-thread boundary and
registered owner/service checks remain mandatory. Reads never create an editor,
material library or appearance controller; save, validate, normalize, refresh,
resource loading, geometry sampling, calculation, result opening and scheduler
queries are not fallback paths. The snapshot is unavailable when there is no
reliably readable current owner.

## Projection and expiry

Known primary fields retain unsaved UI values. Complete retained collection data
binds the snapshot before bounded pages are emitted. Same-length edits outside
an emitted page or inside omitted optical tables invalidate the next page.
Cached resource-handle digests and ray request paths distinguish same-named,
identical documents; current compute host/device IDs bind the read without
exposing endpoints or credentials. Notes and descriptions are bounded user
content, not automatically treated as secrets.

Source emission/preview and material-model editor rate text uses Å/s despite
legacy `*_nm_s` keys; explicit unit metadata prevents confusion with embedded
saved models in nm/s. Three source form labels were corrected to match the
existing conversion. Numeric conversions and physics algorithms are unchanged.

A complete ten-window QML/pending inventory led to a target-window-only modal
fence. Unsubmitted secondary editors, import/replace/delete/close decisions,
transfer/configuration forms, native drawing buffers and camera gestures return
unavailable rather than an older state. Read-only information/reference/history/
CAD-report views remain readable. The materials primary Popup is the editor,
not a secondary modal. Nested material editors are conservatively unavailable
while open, even if clean. Queue reads describe the loaded active cached view,
including cancellation indicators/status; they do not claim the entire persisted
queue or include task payloads/results/assets.

## Frozen-source verification

753 discovered Python/QML sources, source digest:

`ad752a019de4a61132b4125e30a6314d198f72271c13e261793fc3c06b43fc4e`

- Dispatcher: `3630746caa43adb28aa4000b5abcc2a16d5a7f03d54197e059980d454ad31c29`
- Resource adapter: `355d831e7741b62d148315ac66466d302ab3325e605a35b9114a57bb7b1705eb`
- Compute adapter: `82ede53f9d02bdace66d957037e4f624bae2199929ce12697232f4f67c283348`
- Public argument contract: `670ffccd3f704af5e316398257a2fdabdc272b42090800e89fefcd61f4708e20`
- MCP schema: `2e33c9752948fef16a2229d9292f249ee23e6a7865193b61381de68bf563b32f`

## Acceptance results

The production-frozen 19-file broad regression passed: **404 passed, 1 deselected,
42 subtests passed in 192.05 s**. Its post-run source digest was
`52c7cd30ea257fab9356740fab6491d18dcf0c26932516cce2d88365e5697ebf`.
The subsequent full static scan found two type errors in the new ray-path test
fixture, which assigned Path objects to the fake controller's string path field.
Only three test lines changed to use the real controller's string representation.
An in-memory reconstruction verifies every other one of the 752 sources is byte
identical to the broad-tested snapshot. The final affected compute test file
passed **16 tests in 0.31 s**; the three adapters and three new test files passed
focused mypy (zero findings) and ruff. Full final-source static analysis passed:
**753 sources; 0 new diagnostics, 0 resolved baseline entries, 0 fatal syntax**.
Inherited diagnostics remain: mypy 4362, qmllint 1872, ruff 1516 (7750 total).
The baseline was not modified. Its run ID is `4237073d2d06448690cc3917757f5e03`;
post-run verification matched the final source manifest and all listed hashes.
No second 404-test run is claimed after that fixture-only correction.

The broad selection is 19 contract/Qt/resource/compute/discussion test files.
One inherited symlink-attack fixture is deselected because its separate parent
fix is not in this isolated base; this increment does not modify that fixture or
the other parent runtime-contract fixture. Integrating the parent fixes requires
an integration recheck. The full application suite is owned by the parent run,
not represented as having been rerun for this increment.

## Evidence and limitations

Evidence is preserved in `artifacts/qa/editor-read-context-20261016/`, including
JUnit, raw/static reports, exact frozen hashes, the complete modal inventory and
superseded-run status. An earlier mypy process received SIGKILL (exit -9) with
empty stderr; its cause was not established and it is not a pass. An earlier
static scan overlapped edits and is also not a pass.

The verification uses real Qt/QML windows with offscreen/software rendering. It
is not native desktop end-to-end evidence, external-provider evidence, or GPU
execution evidence. Optical sample arrays remain intentionally omitted from the
public projection; metadata and internal full-content hashes are retained.
Native text/IME buffers are never committed to make a read succeed. The final
full static result must distinguish inherited baseline diagnostics from new
findings rather than claim the codebase has zero diagnostics.
