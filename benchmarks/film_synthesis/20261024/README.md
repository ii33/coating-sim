# Current-format representative benchmark registration

Registered against `99066e7c5d18664dadc96b3b88ca28b27b799f3e` (.04).

`representative-suite-current-pointwise.json` explicitly registers the historical
16 cases and 49 objectives in the current nested constraint contract. It is a new
benchmark input, not a runtime compatibility reader. The historical suite remains
byte-for-byte unchanged and is still rejected by the production runner.

Materials, grids, angles, polarization, bound values/directions, strict flags,
layer limits, per-layer/total thickness limits, budgets, weights and seeds 0/1/2
are preserved. All mean constraints are disabled. Historical qualification is
labelled `historical_qualification`, not inherited as evidence for a new run.

The registration stores the original checkout SHA256 and its LF-normalized
SHA256 separately: only Git CRLF/LF checkout differences are normalized. Tests
also compare every case field and every original objective against the frozen
source; normalization must not mask changes to JSON content.

## Interpretation

Pointwise attainment is equivalent. Aggregated scores are **not** equivalent to
historical equal-sample scores: current scoring uses band/angle-normalized
trapezoidal masses. Compare algorithm arms under the same current scoring rules,
not old and new score values. A successful request freeze is neither an optical
test nor a successful design.

The registration test uses the real GUI request adapter, material freeze, job
specification and durable worker decoder for all 48 case/seed pairs. It does not
call the optimizer or TMM. It fixes CPU and one worker for this preflight only.

Run the headless registration check without requiring pytest:

    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m unittest discover -s tests -p test_film_current_representative_suite.py -v

Production preflight/runner: `tools/run_film_synthesis_benchmarks.py`.
This suite's existing wall limits are not a new process-tree CPU-budget contract.
The proposed 96-run paired algorithm experiment must first add and verify its
own shared CPU/F-only/FJ accounting and independent audit reservation; it has not
been executed here. No claim of generalized algorithm improvement is made.
