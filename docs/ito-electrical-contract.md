# ITO electrical calculation contract

Updated 2026-10-18. Contract-first addition: pure, deterministic, GUI-independent,
read-only `coat_sim.core.ito_electrical.calculate_ito_electrical(payload)`.
No project/material lookup, filesystem writes, network, job submission or UI owner.
Input/output dictionaries belong to caller; the service never mutates inputs.
Synchronous bounded scalar arithmetic; no asynchronous state or cancellation boundary.
Invalid data raises `ValueError`; insufficient independent data returns an explanatory
`insufficient_data` result. The CLI wraps errors as JSON and exits 2.

## Request

`operation` is `solve` (default). Optional `thickness`, `sheet_resistance`, `resistivity`
are quantity objects: `value`, `unit`, `provenance` (`theoretical`, `measured`, `assumed`),
`source` (nonempty human-readable provenance), optional `standard_uncertainty` in the
same unit. No silent unit or material constants. All physical values are finite and
strictly positive for this conducting-film model; uncertainties are finite/nonnegative.
Booleans and numeric strings are rejected. Unknown fields are rejected.

Length units: `m`, `cm`, `mm`, `um`, `nm`. Sheet resistance: `ohm/sq`.
Resistivity: `ohm*m`, `ohm*cm`. Resistance: `ohm`.
Optional `rectangle`: `resistance`, `length`, `width` quantity objects, plus mandatory
`electrode_boundary: full_width_opposite_edges` and
`contact_assumption: negligible_or_corrected`. Length is current-path distance,
width transverse to current. The resistance must describe the film alone after
lead/contact corrections or a justified negligible-contact approximation.
Rectangle computes Rs = R W/L; cannot coexist with supplied sheet resistance.
Point probes, arbitrary contacts, curved parts, nonuniform films and shunts are not
supported by this reduction. It does not apply a four-point-probe correction factor.

Optional `uncertainty_assumption: independent` explicitly enables first-order standard
uncertainty propagation. All contributing uncertainties must be supplied (zero must
be explicit); missing uncertainty is never treated as zero. No covariance input is
accepted. Correlated measurements require a separate covariance-aware model. Values
of standard uncertainty comparable to the estimate make linear propagation unreliable.
`consistency_relative_tolerance` is finite in [0,1], default 1e-6, a project numerical
comparison convention, not measurement acceptance or a confidence level.

## Results and model

`rho = Rs * t` with SI conversion. Any two independently provided quantities solve
the third; derived provenance is `derived` and retains dependencies and input sources.
Canonical outputs: thickness nm, sheet resistance ohm/sq, resistivity ohm*cm; each
also has `si_value`, `si_unit`. All three provided values remain unchanged and return
signed residual rho - Rs*t in ohm*m, relative residual divided by max(rho, Rs*t), and
`consistent` or `inconsistent`. This is no least-squares fit; independent uncertainties
can additionally yield standard uncertainty of the residual, not automatic acceptance.
With fewer than two knowns, `insufficient_data` explains missing information. In
particular a sheet resistance alone never determines film thickness.

The uniform, isotropic, continuous ohmic single-layer model assumes no parallel
conduction and common specimen/temperature/processing conditions. ITO resistivity
is process-dependent; inferred thickness is conditional on the supplied resistivity,
not an independent thickness measurement. No deposition recipe, optical fit, carrier
model, or literature constant is silently substituted.

CLI: `python -m coat_sim.ito_cli solve` reads one JSON object on stdin and returns
`{ok:true,result:...}` or `{ok:false,error:...}`. Intended assistant tool:
`ito_electrical_calculate`, read-only including discuss mode; CLI/MCP/live adapters
must delegate this same function and preserve validation/provenance.

## Sources

- NIST, [The Hall Effect](https://www.nist.gov/pml/nanoscale-device-characterization-division/popular-links/hall-effect/hall-effect): rho = Rs d.
- NIST, [TN 1297 Appendix A](https://www.nist.gov/pml/nist-technical-note-1297/nist-tn-1297-appendix-law-propagation-uncertainty): first-order uncertainty propagation and covariance.
- Rectangle reduction follows R = rho L/(W t) for the stated uniform geometry; its accepted boundary token and tolerance are project conventions, not universal metrology corrections.
