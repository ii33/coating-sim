# Isolated GPU appearance preview contract

Date: 2026-10-13. Status: integrated opt-in CUDA backend; output remains unqualified and CPU remains the default.

## Scope and interface

`coat_sim/appearance/gpu.py` (with `tools/validation/gpu_appearance_preview.py` as a compatibility entrypoint) accepts the existing schema-version-1 frozen appearance job directly (`validate_job`). It renders full images with Mitsuba `cuda_ad_rgb`, a device-vectorized coherent multilayer delta BSDF, and the existing independent-wavelength, unpolarized intensity transport convention. This is an opt-in appearance preview; high-precision imaging simulation is deferred. The user-facing window and shared `appearance_render` request may explicitly select `gpu_cuda` with `float32` or `mixed`; CPU remains the default. The same frozen process-asset adapter is authoritative for the actual geometry, spatial thickness fields, materials and sources. No CPU fallback occurs for a selected CUDA request. A separate CUDA child probes real device execution asynchronously when the GPU selector is chosen. The frozen backend/precision and output diagnostics are verified before a UI or queue result is published.

The first real closure uses the captured 81-node grid, actual 60 ZnS/YbF3 layers, ZnS substrate, 12 mm solid thickness, bare back face, and all 86 wavelengths from 405 to 830 nm. The input is mandatory; no invented material data or nominal thickness replaces the captured job. Camera, source pose/size/SPD, environment SPD, outline, and seed are retained. Optional resolution/SPP overrides are explicit and recorded with both original and effective hashes. `validate_job` keeps its existing default aggregate budget of 20,000,000 scalar wavelength samples for production callers. The opt-in GPU tool explicitly supplies `max_wavelength_samples=None`, lifting only this execution-budget limit, and records `estimated_wavelength_samples`, the disabled budget and an explicit warning if the former default is exceeded. All material, physical, shape, finite-value, integer-type, max-pixel, max-SPP and wavelength validation is retained. GPU per-wavelength UInt32-counter overflow protection also remains enabled.

## Ownership, GPU residency and coordinates

One process owns one GPU and runs wavelengths sequentially. Geometry, material spectra, and immutable captured cell thickness arrays are uploaded once. A persistent wave-index array selects wavelength and optical constants with device gathers. Source radiance changes through opaque Mitsuba scene parameters. Quantized grid axes must remain finite and strictly increasing or the job is refused. The same scene and BSDF objects are reused; no wavelength-specific Python class or scalar constant specialization is created. JIT cache reuse is measured, not promised.

World XY is process-local, input mm becomes Mitsuba m. Front is z=0 with outward -Z; back is z=solid thickness with outward +Z. A circle uses two native analytic disks and one open native cylinder; rectangles use two flat caps and four sides. Side walls are always bare substrate/air interfaces. The back is bare unless its frozen back-layer list is coated. Surface-hit cell lookup supports nonuniform grids and executes device binary search and gather. Host preparation retains the existing captured-node cell means and circle boundary local-nearest fallback, including audit metadata. No per-ray Python float, NumPy, host branch, angular LUT, or host TMM is used.

Layer arrays are stored substrate-to-outward, matching FilmDesign. Outside incidence reverses them, inside incidence preserves them. Incident/exit ports are transparent, films may absorb; absorption terminates the path rather than being redistributed to reflection/transmission. Radiance transmission has the existing `(n_i/n_t)^2` adjoint weight, and restricted BSDF contexts retain their physical lobe weight. Directions change hemisphere consistently for both geometric normals.

## Precision and numerical refusal

`float32` means FP32 optical/material/thickness arithmetic, FP32 geometry and film. `mixed` means FP64 TMM/material/thickness arithmetic with FP32 Mitsuba geometry, directions and film. Neither is labelled equivalent to the CPU FP64 reference.

The stable candidate's passive recurrence and guarded near-critical field propagation are reused. Exact original zero thickness is retained as a per-cell mask; zero-thickness field steps are strict identity and an all-zero stack is the actual bare port interface. Bare transparent TIR has an analytic R=1,T=A=0 branch. Incident axial wavevector is computed from the transparent incident index and incoming cosine, avoiding cancellation of `n_i^2-beta^2` at grazing incidence. No critical angle is shifted to make a failing sample pass.

Preview roundoff tolerances are fixed project conventions: FP32 raw powers must be within [-2e-5,1+2e-5], mixed within [-2e-9,1+2e-9], and raw balance errors at most 3 times that tolerance. Only qualified finite powers can undergo bounded probability repair: R'=clip(R,0,1), T'=min(max(T,0),1-R'), A'=1-R'-T'. Bounds are |delta R|<=tau, |delta T|<=2tau, |delta A|<=6tau including balance tolerance. Explicit device repair-distance gates also enforce those bounds after arithmetic rounding, and the manifest records their effective representable values. All repair counts and maxima are recorded separately from raw power statistics. This repair is a declared preview approximation, not a certification of arithmetic accuracy.

All active-hit nonfinite values, kernel-invalid masks, large negative/above-one powers, raw balance failures, invalid cell lookup, zero-thickness underflow, and forbidden nonzero TIR transmission are counted. Any such event rejects the task before a completed image/manifest is published. A rejected diagnostic manifest is retained. Any partial spectrum is explicitly marked incomplete and cannot be used as a completed render; memory maps are flushed and closed on success and failure. Temporary inactive/invalid-lane masking is allowed to keep a GPU trace finite but never converts a rejected task into a successful black image. Mitsuba `BSDFSample.eta` is `n_t/n_i` for path/Russian-roulette accounting; this differs from the old CPU implementation metadata while retaining its intended radiance weight. Counter buffer updates are device atomic operations; readback occurs only after synchronized wavelength rendering.

## Windows CUDA API compatibility (2026-10-13)

The installed Dr.Jit 1.4.0 CUDA runtime rejects Float64 atomic Min/Max. Diagnostic extrema therefore use ordered UInt64 keys after exact conversion of optical scalars to Float64. For IEEE-754 bits b and sign bit s=2^63, key=~b when b&s is set, else key=b xor s. The inverse rule decodes the original Float64 bit pattern. Atomic Min/Max on these keys preserves finite extrema, signed zero and infinity sentinels without Float32 narrowing. NaNs are excluded by the original optical-precision finite mask and separately counted as fatal. Extrema remain diagnostic summaries: all rejection comparisons stay in the selected optical precision and accumulate UInt32 event counters. Device wavelength/cell/gather indices also remain UInt32, separate from UInt64 statistic values. The manifest states these precisions and representation. This bit mapping is a project diagnostic implementation convention, with independent ordering and round-trip tests.

`dr.binary_search` in the installed runtime requires Python integer start/end bounds. Grid lookup uses `0, size`; its predicate produces device UInt32 indices. Failure stderr and `rejected-manifest.json` preserve chained tracebacks. Actual 81-grid/60-layer CUDA smoke and 64×48/16spp/all86-wavelength render passed after these compatibility changes, with no fatal diagnostic events. This is execution evidence and does not certify complete numerical/image accuracy. The 64×48 measured whole walltime is 20.384536 s versus the separately measured CPU baseline 51.8903845 s; workload/runtime/cache state must accompany comparisons.

Cold-JIT detection accepts both Boolean false and integer 0 for kernel-history `cache_hit`. Missing cache information is not treated as evidence of cold compilation. First-call time is always reported separately from synchronized warm and whole-job timing.

## Output and acceptance

The 2026-10-13 real HR54 product-queue smoke, its bounded full-field handoff,
and the remaining full-resolution/qualification limits are recorded in
[the product-integration evidence](gpu-product-integration-evidence-20261013.md).
For large captured fields, the verified `input-job.json` copy is now lossless
compact JSON rather than whitespace-amplified pretty JSON. Its physical-job
digest and all output acceptance gates are unchanged.

Outputs: un-denoised `radiance_spectrum.npy`, `xyz.npy`, `linear_srgb.npy`, view-only `display.png`, and a provenance/timing/diagnostic `manifest.json`. XYZ integration and view transform match production. A PNG is not the accuracy evidence.

Compare to the same-input CPU exact-per-hit renderer, including geometry, cell sampling, camera, sources, all wavelengths and integrator depths. CPU/GPU random paths need not be byte-identical. Reference resolution must match, and its SPP must be greater than or equal to preview SPP. A second independent CPU reference seed can supply Monte Carlo noise-floor evidence; the tool does not assert numerical qualification from one noisy image. Report linear XYZ/sRGB absolute error, three-channel RMSE normalized by reference luminance RMS, color averages, high-luminance highlight ROI and high-gradient edge ROI. XYZ luminance is Y; linear sRGB uses Y=0.2126R+0.7152G+0.0722B. Image metrics are descriptive with no automatic image-accuracy acceptance gate in this first prototype. Numerical runtime thresholds are declared project preview conventions, not external standards. Without a supplied reference, output status is `rendered_unqualified`, never accuracy-qualified.

Timing includes process startup separately, input validation/upload/scene build, first render (cold-first-call with cache status evidence), warm repeated first-wave render, subsequent wavelength renders, output integration/write, and whole walltime. Cold first call is not called cold JIT unless kernel history actually reports uncached compilation. The standalone validation CLI optionally warm-rerenders the first wave with identical seed/SPP, excluding it from the saved spectrum and including its time in whole walltime. UI and queue execution disable this extra measurement render; first-call and all-wave timings remain recorded. Kernel history and runtime versions/device information are saved. Instrumentation overhead is included; no kernel-only speed is advertised as full-render acceleration.

## Minimal remote commands

Use the already installed isolated Mitsuba interpreter. Do not install dependencies into the main project environment.

```sh
python tools/validation/gpu_appearance_preview.py --job /absolute/path/frozen-appearance-job.json --output /tmp/gpu-preview-first --precision float32
python tools/validation/gpu_appearance_preview.py --job /absolute/path/frozen-appearance-job.json --output /tmp/gpu-preview-mixed --precision mixed
```

First inspect diagnostics and actual complete-image output; then generate a higher-SPP CPU reference in a separate process and compare with `--reference /absolute/path/cpu-render`. Place its original schema-1 input at `cpu-render/input-job.json`; the tool verifies every physical input (only reference seed and SPP may differ), in addition to the CPU manifest. A real 86-wave job is the deliverable, not a synthetic TMM throughput probe. Failed or cancelled CUDA tasks retain rejection diagnostics when available but never publish a completed result. The integrated worker checks cancellation at each wavelength boundary. The UI may stop only its own CUDA child after a 10-second cancellation wait to escape a stuck first-wave compile; this cannot validate a partial image. First-wave timing is reported separately and remaining time is unknown until a wavelength completes. The supported geometry is a flat rectangle or origin-centred flat circle, not a curved or arbitrary solid. The rectangle area source and uniform scalar spectral environment are the supported illumination models. Other GPU brands are not yet implemented.

## Primary implementation references

- [Mitsuba custom Python plugin tutorial](https://mitsuba.readthedocs.io/en/stable/src/others/custom_plugin.html): vectorized JIT BSDFs, flags, sampling and scene reuse.
- [Dr.Jit API reference](https://drjit.readthedocs.io/en/latest/reference.html): device gather, binary search, atomic scatter-reduce, opaque residency and kernel history.
- Local `docs/gpu-tmm-boundary-candidate.md`: stable numerical candidate and its unverified CUDA status.
- Local `docs/appearance-circle-contract.md`: authoritative circle geometry and declared captured-field preview policy.

## Cross-platform frozen job serialization

The captured GPU input job is compact sorted-key UTF-8 JSON followed by one LF byte on every platform. The serializer writes bytes so Windows text translation cannot add a CR byte or change the frozen-spec byte budget. This affects storage only; the JSON values, schema, validation and optical calculations are unchanged.

The byte-level regressions in `tests/test_windows_runtime_regressions.py` simulate Windows text translation on any host, check the exact saved bytes and hash, and reject nonfinite values before overwriting existing input.
