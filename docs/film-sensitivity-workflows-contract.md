# Independent ideal sensitivity and thickness-parameter analysis

当前更新（2026-10-24）：波段 mean/pointwise 两组、版本、raw coverage 与严格判定以[双约束契约](film-band-criteria-contract.md)为准。旧 flat 目标与旧协议不迁移。

Updated 2026-10-31. Only the current ideal schema 3 and parameter-table schema 1
are accepted. Removed process/scenario requests and ideal schema 1 are explicitly
unsupported; there is no conversion, compatibility page or fallback parser.
Ordinary optimization targets are unchanged.
The implementation and offline diagrams are in [algorithms.html](algorithms.html).

## Common parameter-table draft and per-layer exceptions (2026-10-27)

The parameter-table page starts with one common set of the existing five fields:
signed bias text/unit and uncertainty kind/magnitude text/unit. Every new front
layer is enabled and inherits that set. Values remain undefined until supplied;
neither zero nor an uncertainty kind, unit or independence declaration is invented.
The collapsible per-layer section retains explicit exclusions and whole-row
exceptions. Editing an individual row creates an exception; turning its exception
off restores all five current common fields without changing its enabled flag.
Folding the section does not reset anything. Common edits never overwrite an
exception or enable an excluded layer.

`FilmSensitivityQuickController` is the sole draft owner. Its
`tableConfigurationEdited()` signal has no arguments and is emitted after an
edit and correlation-identity refresh, never by loading/preparing a draft.
The shared optimization-page bridge stores `exportTableConfiguration()` under
its optional `sensitivity_table` UI field and restores it through
`loadTableConfiguration(configuration)` before normal `prepare()`. The UI-only
version-1 dictionary contains `defaults`, stable-ID `layers` (enabled, override,
and exception values), correlation text/ordered IDs/invalidation, the explicit
uncorrelated flag and finite-bias flag. Malformed/duplicate IDs, invalid types,
units or schema fields reject the load atomically. Incomplete numeric text is a
draft, not a valid analysis value. Loading resets the prepared capture so the
next prepare/start uses current physical data; it creates no job or storage of
its own. Previously frozen reports remain separate from the restored draft.

Preparing preserves only matching stable layer IDs, follows current layer
order, gives added layers the common defaults, and drops removed IDs rather
than transferring their choices by position or material. Percent values are
stored as percent text and converted by the existing request builder/core using
each current nominal physical thickness and frozen optical set, including QW
conversion after thickness/material changes. Persisted `workflow_settings` are
UI metadata, not part of this page's physical source signature; design revision,
physical design, objectives, variables and frozen materials remain bound.

The analysis request still contains the same flat per-layer parameters; no
common/override fields, new solver knobs or perturbation scan steps are added.
Bounded amplitudes and standard deviations keep their different meanings.
Changing the ordered defined standard-deviation ID set retains and invalidates
an existing rho draft exactly as before, without silently reordering it or
assuming independence. Editing is disabled while a job is running.

## Two entry points and sole state owner

The optimization algorithm page exposes ideal sensitivity optimization as
`ideal_sensitivity`; the separate parameter-table dialog remains analysis only.
Both use the existing `FilmSensitivityQuickController` as sole draft, submission,
generation, revision/edit-epoch, project capture, cancellation and observation
owner. The shared Start dispatches the existing typed ideal worker, Cancel routes
to the active sensitivity job, and Apply requires the ideal-specific confirmation
before the existing guarded single undo transaction. Closing the table restores
the ideal workflow selection when that algorithm is active. Switching algorithms
is disabled while any job is busy. The table and ideal drafts remain independent;
no process/scenario page, new executor or fallback is introduced.

The table does not provide an apply-candidate operation. Its values, uncertainty
kinds and correlation declarations never populate the ideal objective. Changing
either draft leaves a prior report available but does not make it a result for
the new draft. Unsupported stored requests cannot be rerun, applied or opened
through a generic simulation fallback; task metadata can still explain rejection.

## Frozen inputs and physical domain

The new kinds are `coat_sim.film_ideal_sensitivity_request` and
`coat_sim.film_thickness_error_table_request`. Ideal request/result schema 3
is the sole ideal format; table request/result schema 1 remains its independent
current format. Workflow job/checkpoint envelope version 1 is a different protocol
and is not rejected merely for its version number. Typed parsers reject unknown fields, nonfinite values,
invalid identities and unsupported domains. Frozen inputs bind design revision,
complete ordered layer topology, materials and optical-set data, original
objectives/grid/angles/polarization, strict operators, and workflow-specific
columns/rows. The physical limitations remain single front coherent/auto
semi-infinite-substrate R/T. No refractive-index/temperature-error feature is
implied. Stored nm/um/QW thicknesses are converted using frozen physical data.

`use_k=True` requires calculable source n/k over the complete original grid,
including the referenced incident/substrate materials. Missing/out-of-domain k
cannot be silently replaced with zero. A valid source explicitly defining k=0
is accepted. Only explicit `use_k=False` ignores k and is labelled idealization;
neither new workflow silently changes material data or that flag.

Default sampling remains the existing 1 nm behavior. Explicit user grids are
preserved, not resampled. The additive `wavelengths_nm` argument of
`simulate_with_thickness_jacobian` carries exact slices of the original array to
material sampling, phases, forward R/T and analytic J alike. Its default keeps
the legacy linspace implementation. The array must be real, finite, positive,
one-dimensional, nondecreasing, and match the declared length/endpoints.

## Ideal derivative objective and versioned acceptance

The user must explicitly choose absolute or relative sensitivity. Derivative
column IDs default to all current front-layer IDs and are frozen separately from
optimization variables/bounds. Empty/unknown/duplicate column selections fail.
For physical thickness d in nm and condition i:

- absolute: A_ij = J_ij times 1 nm, a unit derivative scale, not process precision;
- relative: A_ij = J_ij times the current probe thickness d_j;
- F = sum_i w_i sum_j (A_ij / normalization_i)^2 / sum_i w_i.

F is a deterministic local squared norm, not yield, probability or a finite
manufacturing-error bound. Showing a relative 1% effect multiplies A by .01 and
its squared objective by .0001; the internal definition is unchanged.

For current policy `strict_nominal`, SLSQP retains every original nominal constraint. Analytic constraint J does not
use sensitivity-column scaling. Strict internal slack uses one inward nextafter;
final acceptance still uses the original physical threshold and >/< operator.
HR >= remains >=, and IR < remains <. No margin/tolerance is substituted.
Initial nominal infeasibility is an explicit failure, not implicit restoration.
Intermediate optical infeasibility is permitted for search/FD, but only complete
strictly feasible candidates can enter the usable incumbent archive.

Because F depends on J, its gradient needs second-order physical information.
The shared implementation uses bounded three-point scalar differences instead
of misusing J as that gradient. h_base = eps^(1/3) max(1 nm, |d_j|); select the
largest legal central/forward/backward stencil as specified below for both
current policies. Actual floating coordinates determine Lagrange weights when spacing differs. A missing
valid stencil fails with fd_domain_insufficient, never a fabricated zero.
Fixed variables stay in the design/sensitivity columns but are removed from the
optimizer freedom. All-fixed optimize fails; independent analyze can complete.
Normalized optimizer coordinates multiply both objective/constraint gradients by
the physical span. Relative scaling is recomputed at every probe.

For finite positive initial F0, only the solver sees fixed F/F0 and gradient/F0;
reported F and candidate ordering remain unscaled. Frozen solver ftol=1e-10 is
not optical accuracy. Feasible exact F0=0 reaches the nonnegative objective's
lower bound, not a process certificate. Unrepresentable arithmetic fails rather
than clipping. None max_iterations leaves SciPy's default in effect, which is
recorded with the actual SciPy version, not called unlimited.

## Controlled initial-spectrum degradation: ideal schema 3

The new ideal GUI defaults to `bounded_baseline_degradation`, not a process-error
model. The user must still choose absolute/relative derivative scale and provide
one explicit nonnegative finite allowance for every original target row. Blank is
not zero. The alternative `strict_nominal` retains the original hard nominal gate;
its budgets are empty. Ideal schemas 1 and 2 and removed process requests are rejected,
not upgraded, rehashed or interpreted through a second executor.

Each stable objective ID has a `percentage_point` allowance: 0.1 percentage point
is 0.001 of R/T power fraction, not a relative 0.1% change. Allowances bind row IDs,
not a display position. Reordering rows preserves that identity. Every original
wavelength, angle and polarization condition remains present, including overlaps.
For the initial spectrum y0 and candidate spectrum y:

- lower-bound-only row: y >= y0 - delta;
- upper-bound-only row: y <= y0 + delta;
- range with both bounds or value row: y0 - delta <= y <= y0 + delta.

These guard comparisons are closed and exact, with no solver tolerance or added
margin. A one-sided zero allowance still permits improvement in the other
spectral direction; a two-sided/value zero allowance freezes that row's spectrum.
Value-mode original nominal tolerance is unchanged. Original strict >/< operators
remain separate nominal checks. A nominal-failing initial design can start if its
physical inputs and explicit bounds are legal. This is not target relaxation:
original `nominal_targets_met` remains independently reported and can stay false.

The request freezes the initial design, material data, full grid, physical-kernel
identity, objective IDs, policy and budgets. Freezing/planning performs no hidden
optical baseline evaluation. Execution's first complete forward/J evaluation
consumes the ordinary raw-block budget and cancellation checks, then locks y0
once. Its result evidence records ordered condition identities, exact wavelength
and angle bits, little-endian float64/fraction units, values, request/kernel
binding and digest. Partial, cancelled or nonfinite baselines cannot lock or claim
a guard. The request digest does not change during execution. Replay recomputes
that original baseline and each candidate, verifies the binding, and independently
checks the exact physical guard and F <= F0. Self-reported analyses or approximate
comparison tolerances cannot make an out-of-guard candidate applicable.

The deterministic F, relative probe scaling, fixed F/F0 conditioning, analytic
constraint Jacobian and ftol remain unchanged. Only the constraint bounds depend
on policy. Incumbents require complete guard success and F <= F0; only a strict
F decrease is called improved. An unchanged fallback is `no_improvement`, even
when another stop reason describes budget/solver termination. Finite-difference
probes need physical bounds, not nominal/guard feasibility, and never become
incumbents. The guard is always against the fixed launch spectrum, never against
the last accepted candidate or the current edited design.

Both current policies select the largest legal three-point finite-difference
stencil: central h=min(h_base,left,right), forward h=min(h_base,right/2), backward
h=min(h_base,left/2), descending h with central/forward/backward tie order. Invalid,
repeated or out-of-bound points are discarded; actual floating coordinates still
determine the nonuniform Lagrange weights. This avoids a tiny near-boundary side
squeezing an otherwise well-scaled one-sided stencil. Each evaluator caches only
the most recent constraint tuple after its baseline lock,
keyed by request identity and exact little-endian z bytes. Immutable arrays have
no writable aliases; different bits/identity or incomplete construction cannot
reuse old rows. Baseline evidence, cache storage and construction peak are included
in the resource estimate; no grid/column reduction bypasses the existing limits.

Reports separate initial/final nominal status, guard status, raw/normalized F,
per-row allowance/consumption/excess, and maximum directional degradation/location.
A replay-valid guard-passing improved candidate may be applied while nominal is
false, only after the existing explicit confirmation now stating that fact and
the degradation. Source revision/edit epoch, stable layer IDs, request/operation,
material and project bindings still gate apply, followed by one undo transaction.
Parameter-table analysis is unaffected and never supplies these budgets or an
apply candidate. This policy is a user-selected deterministic allowance, not a
manufacturing-precision recommendation, yield claim or global-optimum guarantee.

## Parameter table and separate outputs

Each stable layer ID has an enabled flag, nullable signed bias plus unit, and a
separate nullable uncertainty kind/magnitude/unit. Undefined is not zero; explicit
zero is valid; all enabled parameters blank cannot start. UI percentages are real
percent numbers converted to internal relative_fraction by /100. Relative values
use frozen nominal physical thickness, not an evolving optimization candidate.

Bias is independent of uncertainty and is counted only once. Per-condition
outputs remain separate:

- bias: J*b; optional real bias-only forward recomputation changes only explicitly
  configured selected layers, keeping others nominal and reporting the subset;
- bounded: sum |J|a, a local first-order conservative sum, not RSS/probability or
  a nonlinear finite-thickness certificate;
- stddev: per-layer |J|sigma. Aggregate sqrt(J Sigma J^T) is available only with
  explicit user rho and Sigma_jk=sigma_j rho_jk sigma_k.

No rho means no presumed independence. The user can explicitly declare mutually
uncorrelated rows, producing a frozen identity matrix. Rho binds the exact ordered
enabled, defined stddev IDs. A manually entered matrix draft retains those exact
ordered IDs. Changing their order or membership invalidates the draft without
clearing its text; explicit clearing and re-entry is required to bind new IDs.
It must be exactly symmetric with diagonal 1; no automatic averaging/normalization occurs. PSD uncertainty uses the project rule
tau=64 eps n max(1,||rho||2). Eigenvalues below -tau fail; values in [-tau,0) remain
unchanged and are labelled numerically uncertain, not certified mathematical PSD.
Positive arithmetic scaling does not change rho. A negative quadratic form is
numerically indeterminate: sigma is null for that condition, aggregate status is
partial, and no square-root/zero clipping occurs. Exact zero is valid. Bounded and
stddev rows are not combined into a fabricated total sigma or probability.

Coverage separately lists enabled/disabled, defined/undefined and unconfigured
layers. Subset outputs do not describe full-stack error. Numerical aggregate
partiality is separate from runtime cancellation/budget partiality. Negative real
perturbed thickness is rejected, not clipped.

## Budget, result, application and transport boundaries

Each raw forward/Jacobian condition block consumes the explicit evaluation
budget. Thickness probes, complete gradients and cache hits have separate counts;
2V describes gradient probe count, not raw block work. Cancel/budget interruption
never returns half a gradient or claims a half-grid incumbent. Caches bind exact
complete physical thickness bytes plus the full request identity. Resource
estimates distinguish retained data, full-topology physical working arrays,
prepared/cache data, and SLSQP workspaces; they are conservative project estimates,
not measured RSS. Oversized requests fail, never shrink the grid/column set.

`film_sensitivity_workflow_jobs` freezes typed jobs through the existing durable
scheduler, confirmation/release and deduplication. CLI:
`python -m coat_sim.film_sensitivity_workflow_cli --request frozen.json`.
CLI requires frozen materials and does not consult a live catalog. New plan/submit
MCP tools are the only current sensitivity tools; removed process plan/submit
tools are not registered. Plan is read-only; submit retains normal
compute confirmation. The formal ideal job performs optimize; an independently
valid core analyze result cannot substitute for that operation. Worker/checkpoint/
history results match the exact persisted JobSpec and input digest before physical
replay. Recovery is a scored-result
recovery, not a resumed optimizer state. Executor or raw typed-result claims
select a typed-only route: invalid schema, mismatched kind or input cannot fall
back to a generic simulation asset. Failed/cancelled retained reports remain
read-only; valid bound analysis reports do not require an applicable candidate. Ideal apply additionally requires current source revision/edit epoch,
materials and explicit confirmation, then one undo transaction. Table analysis
never uses that path. Existing atomic report/overwrite confirmation behavior is
retained; a failed save cannot be claimed successful.

Small physical and contract regressions, not research warm designs or a cold
89-layer search, establish this implementation's initial evidence. Large-task
parallel throughput and the unrelated experimental v2 artifact backend are not
certified by these workflows.

## One implementation, no product compatibility layer

The public API is `coat_sim/core/film_ideal_sensitivity.py`; shared physical,
analysis, scalar-probe and finite-difference helpers live only in the private
`_film_ideal_sensitivity_core.py`. Both policies use the same current SLSQP
executor. The physical identity binds both actual files and the actual optical
kernel. There is no `_v2` forwarding module, schema-1 executor, public class alias,
request adapter into the withdrawn format, or dynamic compatibility attribute.
The parameter table remains independent. Historical research evidence is not an
accepted product format and is not migrated.

## Embedded ideal draft persistence (2026-10-31)

The same owner exports an optional `sensitivity_ideal` UI-only draft with scale,
policy, stable objective-ID allowance texts, explicit layer selection and search
option texts. Loading is atomic and rejects malformed data; incomplete numeric
texts remain drafts and are validated by the existing typed request builder. The
draft is separate from `sensitivity_table` and does not alter the frozen physics
or error model. Reopening and switching algorithms preserve it; current source
IDs determine which rows can be selected or used. Manufacturing candidate
application preserves the current draft instead of restoring an older capture.
