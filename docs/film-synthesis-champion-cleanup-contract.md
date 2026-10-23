# Hybrid synthesis champion cleanup contract

Updated: 2026-10-23. This is a project-defined scheduling rule, not a new
optical approximation or a guarantee of minimum layer count.

## Ownership and defect boundary

The hybrid dispatcher in `coat_sim/core/film_auto_synthesis.py` owns the
exploration endpoint, strict training champion, audited champion, and the
logical evaluation budget. These are distinct candidates. A finite-growth
stage can archive a better champion immediately before its slice expires,
without moving the exploration endpoint. Cleaning only the exploration endpoint
therefore does not clean that champion.

A two-layer candidate made from the same optical reference can be physically
valid and genuinely satisfy the target. An ungenerated single-layer merge is
not an already-scored winner. This repair adds a bounded opportunity to generate
and physically check that simpler candidate; it does not retrospectively label
the original optical evidence false.

## Bounded physical transition

- The compression phase considers the actual strict champion before the live
  exploration endpoint. It must not reset the exploration endpoint to the
  champion merely to perform this check.
- Before the final audit, a further bounded check covers a new champion created
  by later deletion or refinement. No-change candidates cause no extra physical
  evaluation. The check uses remaining training budget and at most two physical
  evaluations, retaining the existing independent-audit reservation.
- Only exact-zero removal and adjacent films with the same `material_id` and
  `optical_set_id` are eligible for algebraic cleanup. Bulk substrate is never
  absorbed into a film. Thickness is physical nm and merged thickness is the
  accurately summed total. No positive near-zero deletion is introduced.
- The proposed design must still satisfy all explicit per-layer, total-thickness
  and layer-count limits. In particular, two legal films cannot be merged when
  their combined thickness exceeds the user's per-layer limit.
- A changed candidate is evaluated on the complete original frozen constraints.
  Its original-grid optical responses must match the already-scored parent
  using the existing `atol=2e-11, rtol=2e-10` equivalence check. This numerical
  comparison is not nominal acceptance and supplies no continuous certificate.
- Promotion uses the existing strict archive ranking, with the real new design
  and new score. A score is never copied onto a different design. The selected
  candidate then needs its own independent audit and exact design/score binding;
  the parent's audit cannot be relabeled as the child's.

## Stops, diagnostics and interfaces

The same cancellation, wall deadline, total/stage evaluation budget, frozen
resource guard and observer-failure behavior apply. Cleanup cannot consume the
reserved audit slot or fabricate an unperformed evaluation. If a legal change
cannot be completed or is not promoted, retain the previously complete real
candidate and report the cleanup outcome and reason separately. Skipped cleanup
is not a numerical failure, proof of irreducibility, or cleanup-complete claim.
Cancellation/time-limit paths never admit extra final physical work.

GUI, CLI and jobs keep the existing result and nominal-acceptance contracts.
`champion_cleanup` retains attempted-change history, including real evaluation
counts and whether the rescored child was promoted to the strict champion.
`terminal_champion_cleanup` describes the latest inspection; a later
`not_needed` check can follow a completed merge, so it does not replace history.
Completing spectral-equivalence checking is distinct from promotion and
independent auditing.
The diagnostic record is descriptive only; it cannot certify target acceptance,
complete original-grid coverage, independent-audit completion or process cleanup.
The frozen request, original source design, materials, targets, sampling, worker
configuration and user files are not rewritten. Ordinary optimization and
independent sensitivity requests are unchanged.

## Verification boundary

Use real frozen request → typed `JobSpec` → `execute_job` without a scheduler to
exercise the 12-evaluation, default-hybrid single-material AR case, plus bounded,
nonmergeable and interrupted cases. Existing GUI one-layer, quarter-wave,
material-closure, save and drift/undo assertions remain unchanged. Service/IPC
GUI tests require an environment where their existing scheduler endpoints are
allowed; a direct numerical test does not claim that those GUI tests ran.
