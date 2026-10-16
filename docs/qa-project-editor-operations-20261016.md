# Bound editor operations extension QA, 2026-10-16

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

Verification performed 2026-10-15 UTC in the original isolated worktree after
its execution environment recovered. No alternative machine, reset, package
installation, user-resource/runtime-data mutation or external publication was used.

## Increment and ownership

Implementation base: `d6a5572`; the single read-context increment was
cherry-picked as `bf9d805b675052ede3da07f44f80d4c83b3e536a` before this local
write delta. The original runtime and read-only worktrees were not edited.
Version and changelog remain for integration. This report records only the
local extension, not release/native-provider acceptance.

Added finite editing for source, substrate, machine, mask, process, materials,
main, ray_trace and film_appearance. Queue/results remain read-only. Exact
per-module fields, argument schemas, original GUI methods and exclusions are
in [the current-editor operations contract](project-editor-operations-contract.md).
Each update contains one field; each action is one bounded original GUI
operation with complete preflight. Existing film-module contracts are retained.

The existing runtime owns Discussion/Plan read-only authorization; a readable
snapshot is not write permission. Writes additionally check exact six-field
project identity, current registered owner/window instance, expected snapshot,
lifecycle/edit freeze, readable-but-noneditable modal state, native text/geometry
buffers and bridge-specific eligibility. Substrate outline `_closed` is not a
window-lifecycle flag; live substrate dragging blocks only writes. Materials
builtin/global/read-only and nested optical dataset state remain protected.

No new save/import/export/open/resource-switch/material-select/device/backend/
precision/whole-document/request-replacement/compute/cancel entry is exposed.
Only the original three film modules retain their existing explicit save action.
Ordinary GUI validation, dirty/revision/undo semantics, allowed invalid scalar
text, lightweight previews and work/memory estimates are preserved. An accepted
invalid draft returns its actual validation and usable refreshed snapshot;
malformed/unsupported requests leave content, dirty, revision and selection
unchanged. Task estimates/approval receipts/submission/cancellation remain
separate explicit contracts. No numerical algorithm or file format changed.

## Complete optimistic snapshot contract

All declared writable raw scalars and bounded row/geometry action targets are
readable and content-signed, independently from revision/dirty/status changes.
Main invalid threshold text/flag, material optical-only/null-model semantics,
CAD provenance and each of the three analysis generation/availability markers
are included. The process preview/mask count and mask machine context are bound.

Machine/mask/process retained JSON `_data` is incrementally hashed in memory,
including hidden provenance and absent/null/empty/NaN/positive and negative
infinity distinctions. Public projection remains a bounded whitelist; whole
raw documents are not returned. Process nonfinite numeric drafts have JSON-safe
named markers. No document normalization, I/O, validation or geometry sampling
is introduced into pure reads. Large optical/result data retain dedicated
content/revision evidence without copying them into the response.

## Verification

- Broad offline final feature regression: **403 passed in 88.54 s**, ten files
  listed below. Tested source digest:
  `2f95e7eb89ff792f940cc1632add14798449903ca9de019a2e724b468d92d8f8`.
- The full static scan then identified a reused local variable's inferred type
  and missing fake-controller attribute annotations. Only process local
  `masks` was renamed to `preview_masks` and two uninitialized fake-class type
  annotations were added. In-memory reversal of just those changes exactly
  reconstructs the complete broad-tested 758-source digest above, including
  reviewed resource adapter hash `94ea78e4...`. The independent reviewer also
  confirmed this exact byte reconstruction and the unchanged acceptance
  conclusion without repeating unrelated tests.
- Final affected snapshot/gateway regression: **193 passed in 18.73 s**
  (`test_project_resource_editor_snapshot`, `test_project_compute_editor_snapshot`,
  `test_project_editor_operations_context`). No second whole broad run is
  claimed after this typing-only delta.
- Independent final signature review: **177 passed in 3.41 s** on the same
  production behavior, with frozen adapter/gateway hashes verified before and
  after. Earlier independent real-gateway malformed-input attacks (22 cases)
  and source/material/process rate conversion checks also passed; resource and
  compute operation adapter bytes are unchanged from that review.
- Existing assistant/manual contract: **7 passed in 2.48 s**. Four current
  capability documents now describe the nine-module finite extension and
  restrictions; the resident overview (707 chars) and limitations (815 chars)
  remain untruncated at the existing 2400-character limit. Historical QA was
  not rewritten.
- Final complete static check: **758 sources; 0 new, 0 resolved, 0 fatal**.
  Existing baseline counts: mypy 4362, qmllint 1872, ruff 1516. No baseline
  modification. Run ID `6569b826c92046e8b9853476fc73aeb5`. Final source digest:
  `c4db19f402778aa67f459f124678c4e5bbe352106a9d13010cce3087d8c98af2`. Own regenerable mypy cache was removed after successful report
  verification; raw static report remains local under artifacts.

Commands used existing QA/static interpreters, with `PYTHONDONTWRITEBYTECODE=1`,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `QT_QPA_PLATFORM=offscreen`,
`QT_QUICK_BACKEND=software`, `pytest -p no:cacheprovider`; test logs/caches were
kept in temporary storage where the existing tools support it. Project writes,
analysis artifacts and the local commit use the approved escalated executor
because shared was outside the resumed default writable roots.

Broad selection:

- tests/test_project_editor_operations.py
- tests/test_project_resource_editor_operations.py
- tests/test_project_compute_editor_operations.py
- tests/test_project_editor_operations_context.py
- tests/test_project_editor_snapshot.py
- tests/test_project_resource_editor_snapshot.py
- tests/test_project_compute_editor_snapshot.py
- tests/test_project_editor_context.py
- tests/test_assistant_discussion_ui.py
- tests/test_project_application_client.py

Tests cover real registered owners/setters, pending and read-only modal fences,
wrong/stale project/window/token identity, malformed/superset inputs,
zero-mutation rejection, raw invalid drafts, Unicode, units, complete hidden
content evidence and absence of lazy film editors/persistence/compute admission.
They do not claim every arbitrary UI action, complete native GUI automation,
actual GPU/provider execution or end-to-end publication acceptance.

## Final production SHA-256

- `coat_sim/ui/project_editor_operations.py`: `b788fe65fbccc9abce0a5a38a7b65427e1d022b7b120b0cd901a414b07bde93f`
- `coat_sim/ui/project_resource_editor_operations.py`: `2ae2c397bd62a95104492ecf176e7485ec5b2f3e190a57bfd5aafd75400a580e`
- `coat_sim/ui/project_compute_editor_operations.py`: `1e43f36fd5c5f33a67af81ed33b48476af97cd787bbf2c276e21ef218aed61e3`
- `coat_sim/ui/project_resource_snapshot.py`: `dc7734b8585c37daddca6fa25c399f7d16a7f4421e87bb153b12188cbd30657d`
- `coat_sim/ui/project_compute_snapshot.py`: `6c6e53f602940add3a8a43bc982c70f36160f80a51937148a1f16591f0fa3ad1`
- `coat_sim/ui/project_application_client.py`: `206d04d62a6d1550bea7f2e9f7759a36634608918ff5b1f1e81e56f834ee65a4`
- `coat_sim/mcp/server.py`: `2ee1153b1073b6be384040b1b171d1a40eef5bfb4c3b39da3d107ec872108b7e`

Reviewed baseline SHA-256:
`18dbbb0c3a531409da410c8d44e51c6735b6b7a80b5e845ae70914a576cabb64`.
