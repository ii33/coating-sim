# Project-private compute runtime binding contract

This checkpoint adds a strict read-only schema-1 consumer and **bounded source-installation worker dispatch**. It is not a new default installer, complete CUDA/native-runtime closure, or a clean Windows-machine acceptance result. The separate installer repository exposes opt-in `runtime-roles-plan` / `runtime-roles-install` commands; its existing default GUI remains unchanged.

## Data and ownership

The installer is the only writer of `.runtime/compute-runtimes.json`. The consumer is `coat_sim.compute_runtime_manifest`; it never installs, repairs, probes hardware, executes a target Python, or imports its packages. Inputs are an existing canonical local installation root and a requested role. Outputs are immutable validated environment/role records and a project-contained private interpreter path.

Exact top-level schema keys are `schema_version: 1`, `platform: win32`, `architecture: x86_64`, `base`, `environments`, and `roles`. Unknown/duplicate JSON fields, noncanonical paths, links/reparse points, wrong file types and oversized metadata are rejected.

The base record contains fixed `root: .runtime/python`, `python: .runtime/python/python.exe`, `version: 3.12.10` and the reviewed owned-file `inventory_sha256`. The ownership marker's NuGet package hash, complete pinned inventory, and all original base file bytes are verified. An environment's `python.exe` must match a launcher hash from that independent pinned base inventory, not merely a caller-rewritten manifest hash.

Each environment has exact `root`, `python`, `backend`, `python_sha256`, `pyvenv_cfg_sha256`, `packages` and `packages_sha256` fields. Venv configuration must reference the private base and disable system site packages. Actual installed wheel METADATA name/version inventory must equal the recorded normalized PEP-503 inventory. Its digest is SHA256 over sorted UTF-8 `name + NUL + version + LF` records. Known application/CPU root requirements and reviewed role-specific pins are checked; unrelated legitimate transitive packages are not restricted to a closed global catalog.

| Environment | Fixed root | Backend / roles |
| --- | --- | --- |
| app | `.venv` | cpu; app + deposition_cpu alias |
| deposition_directml | `.runtime/envs/deposition-directml` | directml |
| deposition_cuda | `.runtime/envs/deposition-cuda` | cuda12 or cuda13 |
| appearance | `.runtime/envs/appearance` | cpu package profile; existing renderer GPU probe still required for CUDA use |

App and its CPU alias are required. Optional environment/role pairs may be absent, never internally inconsistent. CUDA and DirectML can coexist in independent environments. DML rejects competing CuPy roots; CUDA rejects competing CuPy distributions and torch-directml. Existing unrelated app packages are preserved.

## Errors and process boundaries

`RuntimeBindingMissing` distinguishes a genuinely absent manifest/optional role from an invalid existing binding (`RuntimeManifestError`). All present environments validate before any role is selected. A missing deposition GPU can fall back only when a caller explicitly requests CPU fallback. Appearance never falls back to the GUI/app runtime. The dispatch integration does not request CPU fallback.

The scheduler and remote-appearance interpreter selector consult the **source installation root**, not editable project data, before their legacy development routes. A wholly absent manifest retains existing source/development behavior. A present but invalid manifest, or missing requested role within one, stops worker launch. The scheduler remains the execution/cancellation owner; the manifest is never job state or a readiness flag.

Managed CPU and CUDA appearance queue workers use the dedicated appearance binding. Explicit `GPU` / `DirectML` deposition requests for process, recipe, single-source and mask-optimization workers use their corresponding private role. CPU/automatic deposition routes through app CPU at this checkpoint; readiness-aware automatic cross-role choice remains future work. Film optimization/synthesis/sensitivity GPU-forward routes are unchanged and receive no new GPU dispatch.

The existing isolated CUDA/DirectML numerical checks and actual renderer allocation/render checks remain the only readiness evidence. Package installation and interpreter selection do not set GPU capability to available. A renderer GPU failure does not silently switch to CPU or publish a successful GPU result.

Validation performs potentially substantial file I/O and is integrated only at non-GUI process boundaries here. The direct Qt appearance window still uses its established dedicated-environment route; asynchronous UI integration remains a separate step, rather than adding a blocking full base-inventory audit to the GUI thread.

## Failure, repair and acceptance limits

The installer publishes a new manifest only after all requested environment transactions succeed. Existing bindings are moved into a unique owned recovery directory before create-only publication; a late unrelated writer is restored or retained, never deliberately replaced. Consumers reject the installer operation lock at entry, before any missing-manifest result, and before returning validated bindings, so a publication gap does not enable the legacy development route. If app repair succeeds and a later role fails, an older manifest is preserved but its changed interpreter/config/package inventory must fail validation. Cancellation and partial installation do not refresh old evidence or silently accept external Python.

A process crash can leave `.runtime/compute-runtime-install.lock` and an owned recovery directory. The consumer reports a recoverable preparation/recovery diagnostic, never steals a lock or terminates a PID. Confirm the installer and its package processes have stopped, preserve all environments/recovery copies, remove only that stale operation lock, inspect any retained manifest-copy conflict, and review a fresh role plan before retrying installation. This is a cooperative installation guard, not a hostile-filesystem concurrency security protocol.

The owned base inventory authenticates original private CPython bytes. Environment executable hashes/configuration and name/version inventories establish local binding consistency, not full wheel-byte authentication, `.pth` behavior, native DLL closure, operating-system/driver prerequisites, physical GPU correctness, or protection from an attacker who can rewrite the whole installation. Hardware-specific end-to-end acceptance on Windows remains outstanding. No project setting, credential, driver, PATH, or registry is changed by the consumer or dispatcher.
