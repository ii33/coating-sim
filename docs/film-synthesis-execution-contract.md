# Film synthesis execution contract

当前更新（2026-10-24）：波段 mean/pointwise 两组、版本、raw coverage 与严格判定以[双约束契约](film-band-criteria-contract.md)为准。旧 flat 目标与旧协议不迁移。

Updated: 2026-10-25

## Purpose and boundary

This contract defines the GUI-independent execution boundary for producing candidate multilayer coating designs from a bare environment and from explicitly supplied physical-structure seed designs. It covers the frozen JSON request/result exchange used by the CLI and the existing persistent compute-job system. It does not apply a result to an open project or change a user design.

The wire format is versioned JSON (`schema_version: 4`). Request parsing and validation are centralized in `coat_sim/core/film_synthesis_io.py`; the CLI in `coat_sim/film_synthesis_cli.py` is a transport adapter. The synthesis core remains the sole owner of algorithm state, incumbent score, stopping conditions, and final candidate. The execution adapter owns process signals, heartbeat timestamps, progress transport, and output file creation only.

## Request

A request contains `schema_version`, `kind`, `request_id`, `request`, and `physical_structures`. The request object matches frozen `FilmSynthesisRequest`: a bare substrate `environment`, target `objectives`, explicitly selected `candidate_materials`, optional `seed_designs` containing real layer sequences, and optional physical constraints and budgets. `physical_structures` is an array of strict physical-structure spec mappings; each is expanded using the same frozen material snapshot and becomes another seed design. `null`/omitted cycle, evaluation, and time budgets mean no caller-imposed limit; the adapter must never insert a small default. The environment remains a bare substrate. Each seed design is validated by the core against the same environment, material snapshot, and objective/constraint contract.

`strict_lower_bound_objective_indices` is appended to the frozen request and defaults to empty; no new GUI toggle is introduced in this phase. Typed CLI/assistant requests can opt in. The user's strict R > 0.99 benchmark explicitly selects objective 0; a GUI range R ≥ 0.99 keeps its displayed inclusive semantics. Invalid indices fail before computation.

Inputs and outputs use nanometres for wavelengths, layer thicknesses, and geometric positions, and dimensionless refractive index, error, and fractions. Angles follow the existing design schema's degree convention. Input values are copied when frozen and are not mutable shared GUI state. Only a fully frozen request with its material snapshot is accepted by offline execution; the CLI does not read a live catalog.

## Default automatic reflector search (2026-10-21)

The GUI default bare-environment route now supplies automatic periodic/chirped/multistack and objective-derived single/coupled/matched-cavity proposals, fixed-topology thickness DE and analytic interior refinement without requiring `physical_structures` or a user prescription. `structure_search_seed` is an integer in 0..4294967295 (default 0); optional template randomization is described below. Explicit real seed designs keep their existing path. The core remains the unique owner of candidate budgets, ordered commits, cancellation and independent audit; proposal/refinement helpers may not perform separate uncounted physical work.

`performance.search` records structural proposals/skips, global population generations/box provenance, local refinement diagnostics and consumed feasible candidates by layer count. Proposal boxes, quantum counts and layer estimates are project search conventions, not manufacturing caps or global-coverage certificates. The new automatic reflector ranking prioritizes original raw violation before cost and records zero layer penalty. Range interior guidance does not rewrite saved targets. Ordinary range bounds remain inclusive with the existing normalized tolerance, for all routes and R/T metrics. Optional `strict_lower_bound_objective_indices` (default empty tuple; JSON array) explicitly requires actual > lower_bound for every corresponding training and independent audit point, in addition to ordinary tolerance. Entries are unique non-boolean integers indexing range objectives with a lower bound. No objective is implicitly made strict merely because it requests positive R. Old payloads remain inclusive. Archive selection, cancelled best-candidate handoff, per-objective review and serialized matching-audit validation share the same strict predicate. Completed audit status is never inferred from mean loss or worker heartbeat.

## Optional template randomization (2026-10-24)

`randomize_template_search` defaults to `false`. It adds a thickness start
beside an admitted deterministic template. Existing DE, low-dimensional,
needle and explicit `structure_search_seed` behavior stays unchanged when
the switch is off. Archive insertion, worker result ordering, ties, the
objective, and ordinary/sensitivity optimization are not randomized.

The sole seed field is `structure_search_seed`, an integer from 0 through
4294967295 inclusive; booleans, fractions, negatives and overflow fail. Zero
is valid. An enabled draft can have `null`: formal core freeze chooses one
seed and replaces it with the actual integer. Planning through
`prepare_film_synthesis_payload` / `plan_film_synthesis` performs that freeze.
Estimation rejects an unresolved automatic draft without drawing entropy.
Serialization, queue construction and replay require the resolved integer;
none silently chooses another seed. Frozen requests/results preserve it,
including cancellation and checkpoint recovery.

For each admitted base, spare candidate, physical-evaluation, stage, memory,
time and cancellation budgets permit at most one extra start. Construction
uses correlated log thickness perturbations and performs no TMM. Only illegal
or duplicate draws are retried, at most three times. No spare quota or no legal
start retains the base with a reason. Positive optimizable layers move; zero
and fixed layers, materials, topology and all manufacturing bounds stay intact.
Forward/Jacobian/refinement work uses the existing production guards and
independent audit. No extra work is hidden as a free restart.

PCG64 streams derive from algorithm version, actual root seed, canonical
physical template identity, stage and restart index. Temporary layer IDs,
process IDs and completion order do not enter the stream. Diagnostics record
actual seed, algorithm version, PCG64, NumPy version, attempts and skips. Equal
seed/configuration reconstructs starts on the same numerical stack; wall-time
stopping and different numerical libraries can change final results. This
does not guarantee escape from a local optimum. Formula and illustrated limits
are in the [algorithm entry](algorithms.html#film-template-randomization-20261024).

Enabled hybrid/classic template screening currently scores BASE and its extra
sequentially so completed BASE work survives cancellation. This new screening
branch does not accelerate with more workers; other existing parallel stages
retain their behavior. No speedup claim is made for randomization.

## Frozen thickness reduction option (2026-10-24)

`FilmSynthesisRequest.enable_thickness_reduction` is a strict boolean, default
`true` in new core and GUI requests. Hybrid search consults this frozen value;
`false` disables its thickness-reduction trials. The explicit `legacy` strategy
does not use this option. This is not a runtime keyword or a compatibility flag.
The core owns trial admission, physical evaluation budgets, constraints, ordered
candidate acceptance, cancellation, and independent audit. Enabling the option
does not guarantee a thinner or passing result and never relaxes targets.

The existing synthesis options map is the sole editable GUI owner. The checkbox
`filmSynthesisThicknessReductionField` calls `setSynthesisOption`, with the same
busy/pending-edit guards as other synthesis options. Configuration save/load
preserves the boolean; formal freeze copies it into the immutable request.
Changes after a run cannot mutate that run. CLI, MCP plan/submit, durable queue,
result export/replay, and cloud-bundle execution preserve the same value.

Synthesis request/result/job/checkpoint envelopes are now schema 4. Every frozen
request field, including this boolean, is required. Schema 3 and earlier are
rejected, not decoded, migrated, or silently upgraded. Existing unrelated
optimization and editor configuration versions are unchanged. Archived research
artifacts remain byte-for-byte historical evidence and cannot be replayed as
current requests. Current tools must export a newly frozen request instead.

## Result and progress

The result JSON contains `schema_version`, `request_id`, `ok`, and either `result` or `error`. A successful result preserves the core's state, termination reason, final candidate, per-objective acceptance, training score, independent validation score/status, cycle and evaluation counts, failed evaluation count, and trace. The best audited candidate checkpoint is written atomically when configured and is labeled `audited`; any unvalidated incumbent is labeled `training` and must not be presented as accepted. Checkpoints are snapshots for recovery and review, not authority to resume hidden algorithm state.

Progress events are JSON Lines on stderr. Each event has a monotonically increasing `sequence`, `kind` (`progress` or `heartbeat`), UTC timestamp, and the latest core progress when known. Heartbeats indicate that the worker process is alive; they do not assert algorithm progress. The CLI marks `stalled` when progress counters do not change for the configured observation interval and `unresponsive` after the longer configured interval. These are diagnostic flags only: the adapter never cancels work because of them. The core owns the best-so-far score and layer count; the adapter only reports those values.

## State ownership, async work, and cancellation

A GUI must create one immutable request snapshot for a run. The worker process/thread and core own the run thereafter. The GUI may display progress and request cancellation; it must not mutate the request or candidate during execution. A run is asynchronous from the GUI perspective. Completion is delivered once as a result; late progress after completion is ignored by the consumer.

Cancellation is cooperative. SIGINT and an optional cross-platform cancel-file path set the same cancellation signal, which the core checks at its defined safe points. Cancellation may therefore wait for an in-progress indivisible optical evaluation to return. The adapter does not terminate the process or discard a checkpoint. A cancelled result reports the best available candidate, whether that candidate has passed independent audit, and the core's cancellation termination reason. Removing a cancel file does not reverse a cancellation already observed by the run.

## Output path and failures

A requested result path is created exclusively; an existing path is an error and is never truncated or replaced. An audited incumbent snapshot uses a sibling temporary file, flushes it, then atomically replaces only the adapter-owned checkpoint path; it is labeled `audited` and contains the same candidate's training score, independent audit score, and audit metadata. Parsing, validation, missing frozen data, and I/O failures produce a structured error and nonzero process exit. The CLI reserves stdout for one final result JSON when no output path is selected, and stderr for progress events. No partial result is written to the final result path. Final JSON includes each objective's worst audited wavelength/angle and result, audit domain, constraints, layer table, material source references, cost counters, stopping reason, and warnings.

## Execution entrypoints

The CLI and assistant provide two GUI-independent transports over the same frozen request and synthesis core. Batch or external callers can invoke the project CLI in the configured project Python environment as `python -m coat_sim.film_synthesis_cli --request request.json --output result.json`; `--cancel-file path` enables cooperative cancellation and `--checkpoint path` atomically keeps the best audited candidate snapshot. The assistant execution route is the registered MCP task flow: `plan_film_synthesis` prepares the request, while `submit_film_synthesis` uses the existing estimate, confirmation, and persistent scheduler lifecycle. The CLI has no PowerShell dependency, and neither route supplies an implicit evaluation or cycle budget; the frozen request carries the user's limits or `null`.

The CLI result is a review artifact; it does not write a candidate into an open or saved user design.

The read-only `plan_film_synthesis(payload)` assistant operation prepares this same envelope. It reads the selected material records needed to freeze the request when no snapshot is supplied, expands physical specs into real seed designs, and returns a canonical frozen `request` that can be passed to the CLI plus structure metadata. Its response is planning data only: it creates no job, estimate, file, session mutation, or candidate write-back. The unique state owner remains the immutable request payload. Invalid payloads, missing catalog records, or out-of-domain optical data return a structured tool error before a request is returned. Preparation is synchronous and has no cancellation boundary.

`submit_film_synthesis(payload, client_request_id)` is available only to the simulation execution mode. It submits the canonical frozen request through the existing estimate, confirmation, and persistent calculation-job lifecycle. It returns a job receipt; it does not wait for optimization or write into a user design. This release uses the same configured business `CoatService` root as existing film optimization. Estimate, submit receipt, and MCP status/result projections report `execution_project_root`; the MCP host fills it from its configured service root, identifying the calculation workspace rather than the currently selected `.coatproj`. The request schema has no root or routing override. Lower-level `SchedulerClient` responses preserve the scheduler's existing absolute-path redaction behavior; this integration does not loosen that generic disclosure rule. `calculation_job_status`, `wait_calculation_job`, and `cancel_calculation_job` expose the common job lifecycle. `calculation_job_result` returns the full synthesis result, including the audited candidate and per-layer review table. `result.targets_met` and `review.targets_met` mean the task completed successfully with accepted objectives. The top-level `best_candidate_audit` separately reports whether the retained best candidate has a complete audit, its independently measured target status, training/audit maximum normalized errors, the acceptance threshold, and `continuous_certificate` (false for the current finite-sample audit). `targets_met` is `null` when no complete audit is available. Thus a cancelled task can have `targets_met=false` while `best_candidate_audit.targets_met=true`; the recorded scores and per-objective audit values remain factual. The persistent scheduler owns job identity, queue state, event sequence, cancel marker, process liveness, and terminal receipt. The synthesis core owns algorithm state, the complete training best and the separate best audited checkpoint. Worker progress updates include the current stage, evaluation and layer counts, score scope, algorithm-progress age, worker heartbeat, `stalled`, and `unresponsive`; diagnostic flags never cancel work. Cancellation is cooperative and returns the best fully scored feasible parent-committed candidate with `state=cancelled`; audit fields are available only when that same design has a complete saved audit. Preparation errors fail before queue admission; executor or persistence errors are recorded as failed jobs. Repeating a submission with the same `client_request_id` and identical input recovers the original receipt; reusing that ID with changed input is rejected. Existing estimate/confirmation policy remains the authority for release.

## Implementation map

- Wire models and JSON conversion: `coat_sim/core/film_synthesis_io.py`.
- CLI progress, heartbeat, cancellation, checkpoint and exclusive output: `coat_sim/film_synthesis_cli.py`.
- Persistent synthesis executor and typed worker/result/checkpoint validation: `coat_sim/core/film_synthesis_jobs.py`, `coat_sim/scheduler/worker.py`, `coat_sim/scheduler/daemon.py`, and `coat_sim/scheduler/result_capability.py`.
- MCP registration, schemas, and assistant mode permissions: `coat_sim/mcp/server.py`, `coat_sim/core/mcp_contracts.py`, `coat_sim/agent/runtime.py`, and `coat_sim/agent/tool_dispatch.py`.
- Algorithm and physical seed evaluation: `coat_sim/core/film_auto_synthesis.py` and the physical-structure library integration.
- CLI contract tests: `tests/test_film_synthesis_cli.py`.
- Persistent execution and actual worker lifecycle tests: `tests/test_film_synthesis_jobs.py` and `tests/test_film_synthesis_execution_review.py`.

This execution contract documents transport and ownership only. Algorithm definitions and references remain in `docs/algorithms.html` per the repository algorithm documentation convention.

## Existing desktop integration (2026-10-20)

The existing optimization window adds an explicit `synthesis` search mode. `film_synthesis_quick_adapter.py` maps current objectives, chosen film materials and optional constraints into the same frozen request. The existing `FilmOptimizationRunCoordinator` owns queue admission, observation, cancellation and window lifecycle; no additional scheduler is created. `ComputeTarget.setTask` selects the synthesis CPU executor and preserves task-specific host preferences. Local and remote admission reject unsupported hardware and hosts without the advertised executor capability.

This desktop path uses the current project's `ProjectFilmService`, material projection and `ProjectComputeWorkspace`, and fences callbacks by project generation, source revision, pending-edit epoch and run attempt. It does not change the assistant's configured-business-root contract above. Historical queue inspection forwards the full typed envelope through `filmSynthesisResultReady` to `inspectSynthesisQueueResult`; persisted row identity and origin are checked, and history stays read-only. A presentation projection for existing plots is not a second wire format.

Completed current candidates require explicit session confirmation to apply. An explicit current user cancellation can instead apply its complete trusted candidate once under the [cancel/apply contract](film-optimization-cancel-apply-contract.md), without adding an audit or saving a file. Failed, historical, unsolicited and conflicting results do not automatically apply; available audited data may still be viewed and saved only when its frozen materials cover every candidate document reference. Live runs capture a separate editable closure at submission; historical envelopes do not carry that extra GUI snapshot and reject incomplete closure rather than consulting the later live catalog. Project-host preview means the existing-window frozen target-sample plot, not a new independent project simulation window. Material-preserving project saves and standalone reference-file drift rejection are described in [the GUI contract](film-synthesis-gui-contract.md). Targeted tests include `test_film_synthesis_quick_adapter.py`, `test_film_synthesis_quick_controller.py`, `test_film_synthesis_queue_gui.py` and the independent real-Qt `test_film_synthesis_gui_review.py`.

## Explicit unknown-k approximation (2026-10-20)

The existing typed design boolean `request.environment.assume_lossless` is the sole numerical policy. Its default remains false; `use_k` must remain true. True permits zero only when the original selected source has no k data; known nonzero k and its domain remain strict. Seed copies, frozen snapshot design, worker, independent audit, checkpoints, result and saved candidate preserve this same policy. Original material source records retain unknown k. Result `absorption_assumption` distinguishes the frozen pool/seed/environment affected references from `used_affected_materials` in the actual candidate, and labels acceptance as conditional on the assumption, never as measured absorption validation. The design schema/wire version is unchanged; malformed/non-boolean policy values fail. Details and lifecycle are in [the policy contract](film-synthesis-unknown-k-contract.md) and offline algorithms14v.

## Audited checkpoint cost recovery (2026-10-20)

The four-argument snapshot callback remains unchanged. Its copied audit metadata now includes `synthesis_cumulative_work` containing `cycle`, `evaluations`, `validation_evaluations`, `failed_evaluations`, `audit_physical_evaluations`, and `audit_wavelength_points`, captured at the saved audited incumbent. Typed jobs persist these cumulative fields in checkpoint `progress`. Recovery returns the saved worker totals, not fabricated zero failures/one validation or the candidate's single-audit work. Original-worker scope stops at the saved checkpoint; later unpersisted work is not known.

A legacy audited candidate with absent counters remains recoverable after the same physical replay checks. Missing fields in public result/review cost are `null`, with `work_counters_complete:false`, `unknown_fields`, and `scope:original_worker_through_saved_checkpoint` in `work_counters` and relevant review/audit metadata. Any additional training/audit replay cost is reported separately as `recovery_replay_work`. The GUI preserves this original envelope and renders unknown work as “未知”; presentation-only integer placeholders do not become wire statistics or replace null. This is an additive metadata/legacy interpretation rule within the existing result version, not hidden solver-state resume.

## CPU candidate workers (2026-10-20)

The optional typed request integer `cpu_workers` defaults to1 for omitted legacy wire data; 0 selects automatic sizing. It controls pure candidate CPU workers, not host admission or GPU execution. Parent-only ordered decisions and finite-budget safeguards are in [the parallel contract](film-synthesis-parallel-contract.md). Result `performance` records actual worker/speculative/fallback work independently of logical search and audit counters. Checkpoints retain only the historical execution snapshot actually saved; absent data is unrecorded, never fabricated as zero or used to manage an old PID.


## Active user cancellation candidate handoff (2026-10-20)

The new [cancel/apply contract](film-optimization-cancel-apply-contract.md) defines a complete parent-committed training candidate as distinct from an audited checkpoint. A current trusted typed cancellation reply must survive scheduler cancellation handling without fallback checkpoint replay or overwrite. No complete candidate means no automatic source change. Explicit crash/historical recovery retains its existing strict physical replay and remains outside active automatic apply. Candidate provenance, unmatched audit nulls, generation/version guards and one-shot UI application are defined by that contract; cancellation never waits for a new audit solely to make a candidate applicable.

## Reproducible suite runner (2026-10-21)

`tools/run_film_synthesis_benchmarks.py` consumes the pinned representative-suite-v2.1 manifest and reuses GUI `build_synthesis_request`, material freezing, durable JobSpec roundtrip and `execute_job`. References are not injected as seeds. Outputs are exclusively new directories below system Temp, with frozen request/source identities, progress/heartbeats, atomic checkpoints and terminal results. No live project or scheduler database is opened.

The suite wall budget starts after compilation/import/request writes and before execute_job; old HR tooling included import, so raw times across these tools are not directly comparable. Independent runner success additionally requires complete training/dense/offset conditions, metadata, unchanged source hashes, current-candidate training/audit design matching and every original raw bound. The executor's completed state alone is insufficient. Raw quality success and valid execution/cleanup are distinct summary fields. Windows descendant CPU measurements are explicitly sampled lower bounds; no unknown work is silently treated as zero.

Example: `.venv/Scripts/python.exe tools/run_film_synthesis_benchmarks.py --suite benchmarks/film_synthesis/20261021/representative-suite-v2.1.json --case ar_w_visible --seed 0 --workers 2 --output <NEW_DIRECTORY_BELOW_TEMP>`. Omit seed for frozen 0/1/2. `--preflight` compiles without optimizing; the default frozen suite budget remains 120 seconds per run, not a product default.

## Finite local-search diagnostics (2026-10-21)

Strict JSON remains mandatory. A retained initial best can have a lower original error than its phase-coordinate roundtrip re-evaluation by one ULP. Its guide diagnostic is computed from already cached initial responses without a new physical evaluation; missing compact-worker responses produce explicit unavailable/null diagnostics with a reason. An internal infinity sentinel may never enter checkpoints, progress or final results. Nonfinite physical actuals or Jacobians are errors and are not converted to null. This preserves original candidate identity, optical ordering and physical work accounting.


### Coupled-cavity scheduling and refinement guide (2026-10-21)

Positive transmission pass bands between positive reflection stops select generic cavity proposals from frozen q/n/k and the original objectives. No benchmark prescription, fixed wavelength or fabricated optical constant is injected. A persistent iterator consumes 512 proposals per rotation, then resumes its remaining sequence; this is scheduling granularity, never a lifetime cap. Separate topology/phase basins survive selection. Explicit manufacturing limits apply before scoring.

Cavity structure DE uses one absolute log(nm) coordinate for each symmetric group (temporary asymmetric seeds are projected by their group geometric mean; the original incumbent is retained). The epoch box is ±log(8)·(epoch+1), intersected with explicit thickness limits. Population max(24,min(64,4×groups)); initial plus four mutated generations per scheduling quantum. Parent evaluation alone owns physical accounting, feasibility, archive and audit. Full-thickness DE remains distinct. Exhausted insertion branches do not terminate pending structural or continuous work, and selection never loops over an entirely blocked branch set.

Range interior guidance uses min(width×0.1,max(width×0.001,8ulp(bound),acceptable_error×normalization_scale)). This changes the optimization guide only; saved bounds, ordinary inclusive/tolerant acceptance, explicit strict indices, candidate identity and physical work counts are unchanged. This project numerical convention avoids floating-point boundary stagnation under an explicitly zero acceptance tolerance. It is not a new product tolerance or manufacturing guarantee.


### AR matching family (2026-10-21)

`film_synthesis_matching.py` adds pure-reflection-ceiling/low-value AR thin buried-layer and terminal matching proposals, using real frozen material pairs and q. The helper performs no response evaluation. On the automatic no-seed route with at least two allowed materials and room for three layers, the first cycle retains ordinary growth; the second cycle starts an independent layer-count/material/profile scan. Single-material cases retain their original route. Declared strict indices and physical acceptance bounds do not change. Automatic matching ranking, like automatic reflection, prioritizes original maximum violation before legacy cost/RMS tie-breaks.

The first template sweeps every material pair, layer-pair count and representative wavelength before detailed fraction grids. Explicit manufacturing minimum is used exactly; absent minimum uses a QW/16 search proposal without creating a manufacturing floor. At epoch zero, m=1..1+ceil(log2(hi/lo)); fractions 1/32..1 and terminal quarters .5/1/1.5 are project conventions, expanded later. A persistent 512-proposal quantum resumes the same iterator. Deferred and remaining matching work participate in all-growth-blocked scheduling, with closure in the owner's finally. Ordinary physical layer/total limits remain hard. No reference qualification prescription is read or injected.


## Homogeneous-layer AR cluster integration (2026-10-21)

`FilmSynthesisRequest.ar_structure_mode` is a strict string enum: `clustered` (new automatic default, including legacy payloads which omit the field) or `v23_matching` (explicit noncluster ablation). The GUI request constructor uses the dataclass default; request freezing and durable IO carry the chosen mode without reading current UI state. It changes proposal scheduling and parameter search, not target bounds, manufacturing limits, optical materials or result application. The core remains the unique budget/archive/audit/cancellation owner. No new window or system dependency is introduced.

Only automatic no-seed AR requests without positive reflection objectives enter the new default. Non-QW alternating two-material and one/two/three-cluster real homogeneous-layer proposals share a persistent round-robin stage with the existing v23 matching stream. Both are evaluated by full joint TMM. The low-dimensional structure population is scored before analytic full-layer refinement; later fixed-topology thickness DE releases individual layer thicknesses. A directly audited raw solution can stop without running unnecessary DE. HR, BPF and mixed HR/AR retain their existing route, and explicit user seed designs retain their prior path. `v23_matching` preserves its original deferred first growth cycle and three-parameter structure mapping for the same-source control.

Cluster parameters describe phase budgets (nm of real phase index times physical nm at a declared in-domain reference wavelength/angle), high-material optical shares, relative envelope increments and real boundary-layer thicknesses. Cluster budget allocation includes boundary contributions in its proposal total. It is a search coordinate, not a manufactured effective index or a new hard optical-thickness constraint. Only user-allowed real material references are used; a third lower-index outer layer is optional only when a distinct permitted source material qualifies. All source k/domain policies remain strict, including explicit unknown-k=0 opt-in.

Parameter boxes do not guarantee feasibility of coupled internal layers. Parent feasibility checks apply to every mapped candidate before a physical call. No clipping silently alters parameters to satisfy layer/total limits. Pending cluster work participates in insertion-exhaustion decisions and all generators are closed on completion/cancellation/failure; queued results never replace the best complete candidate after cancellation. Population boxes and stage quanta are project search conventions, not total search budgets or global-optimum certificates.

The portable cloud runner accepts only frozen public requests. Export uses the existing GUI constructor/freeze/JobSpec preflight; cloud runtime intentionally invokes the same pure core without Qt. Both mode runs use identical materials/targets/caps/seeds and the frozen 120-second budget. Explicit shortened smoke runs are labeled nonconfirmatory. Worst raw per-condition bounds plus independent dense/offset/extremum audit decide acceptance. Uniform-lambda and uniform-log-lambda trapezoidal means use only the fixed dense audit grid and are reported separately; extrema points never change those means. R/T summaries stay metric-specific; mixed metrics have no top-level aggregate mean. Checkpoint first-pass timing requires matching candidate/training/audit physical identities and source hashes. One-second heartbeat records mark 30/120-second progress gaps as diagnostic stalled/unresponsive states only. SIGINT and SIGTERM request cooperative cancellation; original handlers are restored.


## Git-source execution identity (2026-10-21)

The existing cloud CLI also accepts the source-owned eight-request JSON with `--repo-root` and an exact `--expect-commit`. HEAD and every declared core/dependency/request input must match committed regular blobs; UTF-8 hashes normalize CRLF to LF only. Missing/untracked, escaped, mismatched or altered inputs reject before calculation; unrelated dirty files remain untouched. Runtime reads use Git only in this research CLI, not in product core/GUI. Git SHA and canonical hashes are recorded before/after execution; target/audit/cancel/work semantics are unchanged. See [Git handoff](film-synthesis-git-handoff-20261021.md). No Library file is read.

## Explicit elapsed-time budget (2026-10-21)

The optional frozen request field `max_seconds` is a positive finite number of seconds; absent/`null` means unlimited. GUI configuration, JSON serialization, typed-job transport and CLI use the same field. Its monotonic deadline starts on core synthesis entry, covers cycle-0 and subsequent search, and excludes earlier transport/queue/adaptor preparation. It does not change `max_cycles` or reinterpret a logical evaluation as physical work. Worker/audit code cooperatively observes the shared stop boundary; noninterruptible calls and cleanup can exceed the requested time.

Deadline expiry returns `not_found/time_limit`, retains only completed parent-committed candidate evidence, and cannot start a new final audit or trigger user-cancel auto-apply. Existing audited checkpoint files remain valid for their exact saved design. See [algorithm 14ab](algorithms.html#film-synthesis-run-budgets-20261021) and the core budget contract. No time limit is inserted by default.


## Simple-first AR initialization and run-local scheduling (2026-10-21)

The default no-seed clustered AR route scores one legal reference-quarter-wave homogeneous layer per permitted material before preparing its complex structural sweep. Each proposal uses the same complete objectives, real frozen n/k, explicit physical bounds, shared evaluation/time/cancel budget, archive and independent audit. Already-audited success exits before complex preparation, including when the remaining training budget is exhausted. No V/W wavelength names or reference prescriptions enter the production branch. Unmet targets retain the original matching, one/two/three-cluster and global/refinement route; explicit v23 control, HR and mixed positive-reflection routes are unchanged.

CPU batch diagnostics now expose cold/warm selection reasons, worker caps, observed completed serial work and cost estimates. Only completed same-kind callback samples inform warm small-batch reuse; cold accumulated-work discovery is capped at two workers. These are project scheduling heuristics, not an added work budget or a speedup guarantee. See [algorithm 14ac](algorithms.html#film-synthesis-adaptive-start-20261021). Actual process coverage still requires submitted/consumed work and observed worker PIDs; fast audited cases may correctly remain serial.


## HR boundary and fixed audit-feedback repair (2026-10-21)

Explicit max_layers contributes the largest legal even pair topology after the original reflector estimates (32 to 32, 31 to 30), still filtered by every physical thickness constraint. Unspecified layer caps retain the original schedule. This is additional candidate coverage, not a solved broad-HR claim.

Only completed original audits can register new failing conditions, keyed by original objective index, wavelength in nm, angle in degrees and polarization. Original training-grid and already recorded points use at most four ULPs for duplicate detection. Each local refinement freezes one immutable condition snapshot and owns a fresh cache. One completed failed audit may invoke one local repair followed by the original independent audit; that inner audit cannot recursively repair. Later ordinary local stages use accumulated snapshots. Global DE parent losses and original rank are unchanged in this phase.

Guide scores/responses are private: base grid errors, fixed-grid RMS and exact design identity are restored before archive, trace or transport. Normal completion keeps the best original audited candidate; cancel/time-limit contracts still retain only completed training and a matching existing audit. Additional singleton physics runs through parent accounting and cooperative stop checks; it does not count as a second logical candidate or imply GPU/parallel acceleration. A missing-response initial refresh does use the normal logical budget. See offline algorithm 14ad for formulae, limits and finite-evidence scope.

New-objective and spectrum GUI defaults were already 1 nm; new regressions lock this behavior without altering saved step/count, legacy range-only semantics or any frozen 5 nm request.

## Dual-substack HR proposals and fixed-training-grid minimax refinement (2026-10-21)

For eligible no-seed, bare single-sided HR requests, `pure_hr_prior_eligible` may admit `iter_dual_substack_hr_proposals` before the existing classical HR scan. Eligibility requires at least two candidate materials, an explicit `max_layers >= 4`, and **every** original objective to be a positive-weight R-range objective with `lower_bound >= 0.5` and no restrictive upper bound (`null` or `>= 1`). Mixed R/T objectives and value objectives are ineligible. A supported real-propagating incident medium is required; an absorbing incident medium is ineligible. These are additional initial structures, not catalog-wide coverage. Proposals use the frozen true complex n/k, target bands/angles/polarizations, the existing format layer ceiling, and every explicit minimum, per-layer maximum and total-thickness bound. Invalid or out-of-domain contrasts and crossed sub-band references are skipped; no material, wavelength or prescription is hard-coded.

The prior derives passive q and s/p optical admittance at finite witnesses `{lo, sqrt(lo*hi), hi}` for every target angle and polarization, where `lo` is the minimum lower wavelength across all objectives and `hi` is the maximum upper wavelength. It uses the minimum real high/low admittance ratio `C`, the project fractional bandwidth estimate `Delta = 4/pi * asin((C-1)/(C+1))`, and fixed offsets `f in {1/3, 1/4, 1/2}` to place two sub-stack references `c1 = lo*(1+f*Delta)` and `c2 = hi/(1+f*Delta)`. These prior witnesses construct initial proposals; they are not the minimax training grid. A user-specified layer cap contributes `P = floor(min(max_layers, MAX_LAYERS)/2)` legal layer pairs, split as `ceil(P/2)` and `floor(P/2)` pairs between the two sub-stacks (two physical layers per pair). The proposal generator expands real quarter-wave H/L designs in both HL and LH order using the midpoint of the extreme requested incidence angles. The formulae and all thresholds are search conventions; they are not a manufacturing model or a success certificate.

The source research handoff and its generic-prior bridge describe one generated-prior, no-answer-seed case; that case is not a production-software result or evidence of general feasibility. When translating an incident-to-substrate research stack into production `front_layers`, whose order is `substrate_outward`, reverse both the material sequence and the thickness sequence together. Preserve known film absorption; an explicit unknown-k assumption applies only where the frozen source lacks k. See the [source research handoff and bridge](https://github.com/ii33/coating-research-library/tree/3f670f4fbf5c2731c719e59792705bd25207beb9/manual_studies/2026-10-21_coating_sim_lowk_tio2_bulk_sio2).

`refine_hr_minimax` can locally refine an already parent-scored proposal using every original training-grid wavelength × angle × polarization condition, not just the three prior witnesses. Its guide objective maximizes raw minimum R; this is not the same as the worst normalized violation across objectives with different lower bounds or normalization. Thickness uses the fixed coordinate `x = d/(100 nm)`. It first minimizes `logsumexp(-beta*R)/beta` with L-BFGS-B for beta 100, 400 and 1500 (`ftol=2e-13`, `gtol=1e-9`, `maxls=25`), then uses SLSQP to maximize a common lower bound `t` subject to `R_i(d) >= t`, per-layer bounds and the original total-thickness bound (`ftol=1e-11`). Each phase is limited to 180 iterations as a scheduling quantum, not as a new run-wide cap. Analytic Jacobians are evaluated through the parent CPU path; this does not mean the full optimizer uses GPU execution. Only the latest thickness point's physical R/Jacobian values are cached and may be reused across phases; losses and weights are recomputed. An infeasible manufacturing trial receives an internal feasibility penalty and is not physically evaluated or archived. Local optimization has no convergence guarantee.

All real optical calls, the original complete objective score, audit, archive, `max_evaluations`, `max_seconds`, cancellation and time-limit handling remain owned by the parent synthesis evaluator. Invalid trials do not enter TMM. The minimax `t`, smoothed loss and finite witness minimum never replace original scores or independent audit and do not certify continuous-band performance. Logical candidate/audit admissions remain distinct from actual physical spectral calls and wavelength-point counts. The implementation is in `coat_sim/core/film_synthesis_hr_priors.py`, `coat_sim/core/film_synthesis_hr_minimax.py`, and the scheduling path in `coat_sim/core/film_auto_synthesis.py`; SciPy `logsumexp` and SLSQP references document numerical interfaces only, not optical performance. Existing saved sampling and frozen 5 nm requests are unchanged.

## Terminal structural-search diagnostics (2026-10-22)

A live lazy structural iterator may pause between search slices. On every normal terminal result, its owned proposal streams close before the terminal progress event and copied `performance.search` snapshot. This publishes the existing matching/cluster/cavity schedule counters without consuming more proposals, evaluating optics or changing the retained candidate. Exception paths also close the streams and preserve the original observer exception. Family schedule keys remain branch-specific: an unentered family has no fabricated schedule; `latest_consumed` counts generated proposals in the latest iterator slice for that family, while `pending` records unexhausted coverage at termination, not an active worker after cleanup. The ordinary matching control and cluster fallback remain interleaved. The regression uses both real generators and checks cancellation, deadline and observer-failure boundaries.


## Retained multispectral-incumbent statistics (2026-10-22)

An initial completed candidate can beat every thickness-coordinate roundtrip by one ULP. Its original score, identity and exact-tie ranking remain unchanged. The internal minimax sentinel must not be exported as an observed `max_signed_violation`: use the retained candidate's existing complete responses, or `null` with `max_signed_violation_source` identifying unavailable responses without additional physical evaluation. Response-only statistics keep original objective/angle/grid/duplicate/finite-value checks; gradient optimization still requires all optimized layer Jacobians. Malformed nonempty responses fail validation rather than becoming unavailable. Strict JSON and the audited-checkpoint callback are unchanged. The real CLI heartbeat/cancel/checkpoint test retains all assertions and exercises the repaired serialization path.


## Opt-in objective-progress handoff (2026-10-25, current schema 4)

Every current frozen request explicitly carries `objective_progress_policy`: `null`
(default, unchanged scheduling) or an exact object with `accepted_window_steps`,
`relative_sse_progress_floor`, `relative_worst_progress_floor`, and
`stagnation_windows`. Step/window counts are positive integers; progress floors are
finite nonnegative numbers. There are no implicit threshold defaults. This option
is valid only for hybrid search. Version 3 and earlier envelopes are rejected with
a version error, not migrated. GUI option persistence remains JSON data; the core
owns the validated frozen `HybridProgressPolicy`. CLI, MCP, queue and checkpoint
paths use this same current request contract. Historical benchmark inputs remain
historical; a new experimental registration must name its source and explicit policy.

This first slice observes only accepted iterates inside the main
`joint_needle_sse_growth` refinement, not prior screening/pre-local refinement,
line-search trials, deletion, or independent audit. A compact monitor stores
canonical SSE, original maximum normalized violation, weighted RMS, the explicit
original `nominal_pass`, physical identity and nm coordinates. It introduces no
second score or optical evaluation. Non-overlapping windows use signed relative
improvement `(old-new)/max(old,new)`, with an all-zero pair defined as zero. A
window requests handoff when SSE improves beyond its declared floor but original
worst violation does not; alternatively, neither improves for the declared number
of windows. These are project scheduling conventions, not proofs of a stationary
point, local minimum, infeasibility, or cross-target benefit. Solver `ftol`/`gtol`
and original mean/pointwise goals remain unchanged; zero violation alone does not
establish a strict nominal pass.

Observed `accepted_samples` is not the optimizer's accepted-iteration count.
The existing callback commits a matching complete endpoint before checking its
slice. If that last admitted call already exhausts the local slice, the endpoint
remains usable but is not observed by the monitor; no latent decision is inferred.
A window with that missing final observation is inconclusive, not evidence of no
stagnation. Conversely, once a decision is recorded, later local-slice expiry
cannot undo it. Global cancellation, deadline, evaluation limit and observer
exceptions still take priority.

The refinement commits the complete trial through the existing archive and emits
its accepted-iterate event before the decision. The parent verifies exact physical
identity, then emits `objective_progress_handoff` with reason, baseline, trigger and
window metrics. It skips further SSE, deletion and physical cleanup in that cycle,
including normal-exit cleanup, and passes that exact accepted design to the existing
full-original minimax only if shared admission permits. Candidate/evaluation counts,
resource guard, final original audit, current/best displays and incumbent selection
retain their existing owners. Cancellation/deadline wins; no new optics or return
materialization is admitted after cancellation. A denied minimax admission retains
already completed evidence, never claims a successful handoff optimization or a
new audited result. Original strict champion and audited incumbent are not replaced
by the SSE endpoint solely because it triggered routing.

Exactly-zero thickness is legal in SSE probes but outside the native positive-bound
minimax receiver. An accepted zero or incomplete accepted evidence resets the
eligible window and stagnation streak. The next complete positive point becomes a
new baseline; positive values within the native bound remain eligible without a manufacturing floor.
Without a user lower bound, the native lower bound is float64 tiny (about
2.225e-308 nm); smaller positive subnormals also reset the window.
Mean and pointwise criteria stay independently enabled. Layer count is neither fixed
nor minimized by this policy. Optional manufacturing repair is not implemented here.

The acceptance harness compares policy-null and explicitly configured runs in the
same source checkout, original material/optical grids and separate F/FJ ledgers.
Its declared finite work/wall caps and any derived BOTH-goal fixtures are experimental
protocol choices, not product defaults or a universal F/FJ equivalence. An untriggered
or negative comparison must be reported as such before considering a default change.
