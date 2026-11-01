# Transmission rejection starting structures

## Scope

The hybrid synthesis route now recognizes positive-weight, pointwise transmission
ceilings at or below 0.5 as hints for reflective starting structures. It creates a
private proposal-only request with corresponding reflectance floors. This is not
an assertion that R = 1 - T in absorbing optics. The original request, source n/k,
T scoring, gradients, local convergence, strict bounds and independent audit are
unchanged. Mean-only and value targets do not use this hint.

For normal incidence, a rejection window bracketed by transmission pass windows
also enables binary, non-quarter-wave optical duty-cycle starting stacks. The
nearest pass-window edges guide the initial bandwidth; cell attenuation guides a
finite period-count ladder. The phase fractions, interior seed reflectivity and
count multipliers are search conventions, not lower bounds or feasibility claims.
No feasible design is embedded. All candidates undergo the existing full-objective
optimization and audit, including complex source optics and actual substrate.

Explicit layer/thickness limits and the existing format ceiling are respected.
The family is adapted through the existing selected-surface template machinery.
Pure high-reflector requests and tasks without a bracketed rejection window retain
their existing starting families. Oblique-incidence narrow-duty seeds are not
implemented by this change.

## Continuation

Applicable hybrid tasks include the proposal policies in their search fingerprint.
Old source cursors must not silently skip newly enabled families. Incompatible
saved search progress is rejected; start a new synthesis task rather than merging
old progress or accepting historical scores as new evidence. Physical data and
previously saved results are not deleted or rewritten.

## Validation

Focused tests cover original-target immutability, known absorption preservation,
source-family admission, actual hybrid scoring/gradient use of original T targets,
constraint and cancellation handling, normal-incidence scope, epoch expansion,
selected surfaces and policy-specific checkpoint invalidation.

The four-window diagnostic is 400–500 nm T > 80%, 510–550 nm T < 1%,
560–700 nm T > 80%, and 1040–1080 nm T < 1%, at normal incidence. Reference results
must identify their optical datasets and unknown-k assumptions. A sampled audit
pass is neither a continuous-spectrum proof nor a manufacturing-tolerance claim;
one successful design is not a minimum-layer or minimum-thickness result.
