# Objective-progress handoff: isolated verification milestone

Date: 2026-10-25. Exact implementation base:
`2a18c12f05ba470710b235769e8d0f1a071dd331`.
No historical source merge, reducer migration, default-policy enablement, or optical
acceptance relaxation is included. The current synthesis protocol is deliberately
v4; unrelated protocol versions and archived benchmark files are unchanged.

## Scope

Explicit policy or null; accepted main joint-growth SSE observations; existing
full-original minimax handoff; original archive/UI/cancellation/admission ownership.
The policy is available through the typed API/frozen CLI and persisted GUI options.
There is no new GUI enable control. Manufacture-repair scheduling is not implemented.
See `film-synthesis-execution-contract.md` and algorithms.html section 14aw.

## Verification

Pinned host runtime: Python 3.12.14, NumPy 2.5.4, SciPy 1.18.1. CPU only.

- Focused core/current-contract/hybrid/GUI/benchmark registration selection:
  173 passed, 65 subtests passed (38.81s).
- Later core-boundary and producer-field checks: 79 passed (11.27s).
- Supported runner selection after final callback-boundary tests:
  67 passed, 8218 deselected, 9 subtests passed (13.58s).
  Command: `python tools/run_tests.py all -- -k 'test_film_synthesis_progress or test_film_synthesis_quick_adapter or test_film_progress_acceptance'`.
- Broader 83-module synthesis/criteria/serialization/GUI selection:
  1305 passed, 4 skipped, 74 subtests passed, 1 stale default-options assertion failed
  (428.35s). It expected omission of the newly explicit null policy.
  That expectation was corrected. Example-project export had the same stale
  expectation, found by searching outside that selection and corrected.
  Final adapter+example-project check: 16 passed (2.02s).
  The entire 83-module selection was not rerun after these assertion-only updates;
  do not report a fresh clean aggregate run.
- Independent read-only core review found two issues, both corrected and covered:
  local slice expiry must not undo a recorded trigger; receiver eligibility must
  match the existing numerical lower bound. Global stop/observer exceptions remain
  authoritative. A last accepted point stopped before observation stays usable but
  is not evidence that stagnation was absent.
- Pinned full static gate: zero new diagnostics, zero resolved baseline entries,
  zero syntax errors; no new baseline allowances. Run with
  `python tools/run_static_analysis.py check`.

## Explicit limits

An unintended broad runner invocation was interrupted, not counted as a full pass.
Its early failure was identified and reproduced in
`test_application_process_smoke.py::test_detached_project_runtime_ownership_across_launcher_lifetimes`:
both launchers returned `[Errno 1] Operation not permitted` while opening a project,
so no project host registered (line 483). No application-source workaround or access
bypass was attempted. This remains an environment-blocked lifecycle gate; no
baseline equivalence is claimed.

No real-case efficacy matrix is claimed by this milestone. The separately declared
16-cell seed 0 comparison must retain null/on controls, unchanged material data and
original goals, separate F/FJ physical-work counters, wall bounds and original audit.
Null or negative findings are valid outcomes; defaults remain off.
