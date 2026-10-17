# Single-layer optical-constant inversion

`coat_sim.core.optical_inversion.fit_optical_constants(request, *, cancelled=None,
progress=None)` is a pure, unsaved estimation API. It does not edit an optical
dataset, install dependencies, write files, or run deposition calculations.
The caller preserves full fit provenance before explicitly adding a new dataset.
The current fit supports one homogeneous isotropic planar layer, a known fixed
positive thickness, a known substrate, and an uncoated substrate backside.
Roughness, gradients, anisotropy, multilayers, diffuse scattering, partial
coherence and instrumental spectral resolution are outside this first version.

## Input

The request contains these fields:

- `model`: `cauchy` or `lorentz`. Cauchy additionally requires the explicit
  `transparent_region: true` declaration for the entire measured band.
- `film_thickness_nm`: fixed positive thickness, at most 1,000,000 nm.
- `substrate`: either constant `n` and `k`, or an existing validated `optical_set`.
  Unknown `k` is rejected unless `assume_lossless: true` is explicit. It remains
  unknown in original provenance; zero is a recorded calculation assumption.
  Known n/k data must cover each measured wavelength, with no extrapolation.
  Finite substrates require positive `thickness_nm`, at most 1,000,000,000 nm.
- `geometry`: explicit `backside: incoherent | coherent | semi_infinite`, with
  transparent exterior indices `incident_n` and `exit_n` (defaults 1).
- `spectra`: 1–16 records, at most 4096 measured samples in total. Each record has
  `quantity: R | T`, strictly increasing `wavelengths`, explicit
  `wavelength_unit: nm | um`, `values`, `value_unit: fraction | %`, explicit
  `angle_deg` (0–85), `polarization: s | p | unpolarized`, and
  `measurement: absolute_specular`. Optional `id`, `source` and positive `sigma`
  (scalar or same-length array in the same value units) preserve provenance.
  For T, `transmission_definition` must be `external` for a finite plate or
  `into_substrate` for an explicitly chosen semi-infinite substrate.
- `parameter_bounds` optionally tightens the limits below;
  `initial_parameters` optionally supplies bounded starting values.
- `optimization` optionally sets `multistarts` (2–8, default 4), `max_nfev`
  (10–1000, default 250 per start), deterministic `seed` (default 0), and
  `acceptance_rmse` (absolute fraction, default 0.01, at most 0.2).
- Optional `source` and `name` label the resulting candidate.

Bare-substrate-normalized T, total hemispherical/scattered measurements,
unidentified units, missing geometry and wavelength-by-wavelength arbitrary n/k
fitting are rejected. No automatic normalization is performed. R and T may have
different wavelength grids and angles; every residual is evaluated on that
spectrum's original wavelengths, without pre-interpolating measurements.

## Parameterization and forward calculation

All conventions below follow the existing `film_optics` convention of complex
index `N=n+i k` and time dependence `exp(-i omega t)`.

| Model | Formula | Parameter bounds; initial values |
| --- | --- | --- |
| Transparent Cauchy | `n=A+B/lambda_um²+C/lambda_um⁴`, imposed `k=0` | A [1,4]; 1.8. B [0,0.3] um²; 0.005. C [0,0.03] um⁴; 0.0001. |
| One Lorentz oscillator | `epsilon=eps_inf+strength_ev2/(resonance_ev²-E²-i*damping_ev*E)`; `E=1239.8419843320026/lambda_nm` eV; passive principal `N=sqrt(epsilon)` | eps_inf [1,16]; 3. strength_ev2 [0,100] eV²; 10. resonance_ev [0.2,12] eV; 4. damping_ev [0.01,5] eV; 0.3. |

The joint objective is the sum of squared `(predicted-measured)/sigma` residuals;
omitting sigma gives equal sample weights in absolute fractions. All parameters
are shared across all spectra and scaled to [0,1] within their bounds. SciPy
bounded trust-region `least_squares` uses the declared initial point, one local
perturbation, and remaining seeded broad starts. Numerical tolerances are
`ftol=xtol=gtol=1e-10`. `max_nfev` follows SciPy semantics; finite-difference
Jacobian calls add residual evaluations, whose actual total is reported.
Each start additionally has a hard residual-call ceiling of
`(number_of_parameters+1)*(max_nfev+1)`, including numerical Jacobian calls.

Each residual evaluation rebuilds the film indices. It calls the existing
`coherent_rta` for a semi-infinite substrate or a fully coherent finite plate,
and `incoherent_plate_rta` for a finite incoherent substrate with all backside
round trips and known substrate absorption. It never uses a frozen-film-index
cache. The incoherent case requires propagating substrate and exit ports.
Choosing semi-infinite geometry explicitly excludes backside propagation and
does not reinterpret externally measured T as substrate-entering flux.

The model families, units, numerical bounds, start generation, sample budgets,
tolerances and warning thresholds are **project implementation choices**, not
universal material constraints or proof that experimental n/k is identified.

## Output and cancellation

Successful results return `ok`, `status: completed`, `converged`, model/formula,
`parameters`, `parameter_units`, `bounds`, `structure`, the complete finite JSON
`request`, and a `spectra` list with original inputs, measured/predicted absolute
fractions, signed residuals (`predicted-measured`) and per-spectrum RMSE. Global
`rmse` is the unweighted root mean square across measured samples, while
`weighted_rmse` reports the optimization residual convention. `nk` samples the
fitted model at the sorted union of raw measurement wavelengths.

`optical_set` is a valid **candidate** tabulated dataset, not an automatic save.
Its source/notes summarize model and warnings. Subsequent table interpolation
is linear and differs from evaluating the analytic fit; the complete original
spectra, structure, model, bounds, source, warnings and diagnostics must be kept
in a separate fit provenance record. Interpolation across unmeasured gaps and
extrapolation outside the measured band have no independent validation.

Warnings always state that this is a model estimate with non-uniqueness and
fixed-thickness/substrate uncertainty. Additional warnings cover R-only/T-only,
normal-incidence ambiguity, model restrictions, poorly conditioned/rank-deficient
weighted Jacobians, boundary parameters, nonconvergence, high residuals, and
different similar-cost starts. The near-best start heuristic uses weighted RMSE
within `max(1e-6, 5% of best RMSE)` and flags normalized parameter spread above
2% of its allowed interval. Bound detection uses 1e-5 of the parameter range;
poor conditioning uses a local Jacobian condition number above 1e8. These are
diagnostics, not statistical confidence intervals or global uniqueness tests.
In particular, R+T never suppresses the non-uniqueness warning.

Cancellation is checked during validation, every residual evaluation and every
forward spectrum. It returns `ok: false, status: cancelled` without an optical
candidate. Progress callbacks receive plain dictionaries on the caller's worker
thread; the UI must marshal updates. Input errors return `invalid_request`, and
numerical failure returns `failed`. All outputs are finite JSON; a singular
condition number is represented by null. `saved` remains false.

## Sources and verification

[J. A. Woollam's optical-constants tutorial](https://www.jawoollam.com/resources/ellipsometry-tutorial/optical-constants)
supports shared dispersion fitting, Cauchy use in transparent regions and
oscillator treatment of absorption; it also describes thickness/index coupling.
[Byrnes, Multilayer optical calculations](https://arxiv.org/abs/1603.02720)
provides coherent/incoherent multilayer and complex-index flux conventions.
[Lamprecht et al., Applied Optics 36, 6364–6371 (1997)](https://doi.org/10.1364/AO.36.006364)
reports practical ambiguity from normal-incidence spectroscopic R/T. The latter
was checked through its accessible abstract; this implementation does not claim
to reproduce that paper's error-analysis procedure.

`tests/test_optical_inversion.py` uses independent scalar characteristic matrices
and an analytic quarter-wave benchmark in addition to synthetic fit recovery.
It covers transparent/absorbing fits, original grids, noise, wrong thickness and
model, finite backside propagation, substrate absorption, normalization/units,
unknown k and extrapolation refusal, invalid values and in-residual cancellation.
