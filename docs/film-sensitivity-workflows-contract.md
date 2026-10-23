# Independent ideal sensitivity and thickness-parameter analysis

Updated 2026-10-23. These two typed workflows do not reinterpret legacy
`coat_sim.film_sensitivity_request` v1 or modify ordinary optimization targets.
The implementation and offline diagrams are in [algorithms.html](algorithms.html).

## Two entry points and sole state owner

The existing sensitivity dialog exposes parameter-table analysis and ideal
sensitivity optimization as separate primary pages. Explicit scenario generation
is retained only in the advanced compatibility page. The first opening selects
ideal sensitivity optimization, with no selected absolute/relative scale. Closing
and reopening the same dialog preserves an explicitly selected page and its
independent drafts/results. The existing `FilmSensitivityQuickController` owns drafts, pending submission, generations,
revision/edit-epoch checks, project capture, cancellation and result observation.
The pages do not create their own executors or a second controller.

The table does not provide an apply-candidate operation. Its values, uncertainty
kinds and correlation declarations never populate the ideal objective. Changing
either draft leaves a prior report available but does not make it a result for
the new draft. Legacy stored requests keep their original semantics.

## Frozen inputs and physical domain

The new kinds are `coat_sim.film_ideal_sensitivity_request` and
`coat_sim.film_thickness_error_table_request`, each with independent version 1
request/result envelopes. Typed parsers reject unknown fields, nonfinite values,
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

## Ideal derivative objective

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

SLSQP retains every original nominal constraint. Analytic constraint J does not
use sensitivity-column scaling. Strict internal slack uses one inward nextafter;
final acceptance still uses the original physical threshold and >/< operator.
HR >= remains >=, and IR < remains <. No margin/tolerance is substituted.
Initial nominal infeasibility is an explicit failure, not implicit restoration.
Intermediate optical infeasibility is permitted for search/FD, but only complete
strictly feasible candidates can enter the usable incumbent archive.

Because F depends on J, its gradient needs second-order physical information.
The first implementation uses bounded three-point scalar differences instead of
misusing J as that gradient. h_base = eps^(1/3) max(1 nm, |d_j|); central samples
use the available distance on both sides. If distinct central points cannot be
represented, use two legal one-sided samples with h=min(h_base,room/2). Actual
floating coordinates determine Lagrange weights when spacing differs. A missing
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
MCP tools are separate from legacy tools. Plan is read-only; submit retains normal
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
