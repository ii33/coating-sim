# Deposition Sim · Planetary Coating Simulation

[中文](README.md) | English

A desktop tool for optical thin-film research and engineering development, bringing thin-film design, planetary evaporation deposition, thickness maps, spectral analysis, and result assets into independent projects.

**This is the `dev.20261016.13` development snapshot, not a formal Release.** The project has not completed comprehensive physical calibration. Simulation and optimization support analysis, comparison, and research; they do not replace equipment calibration, process qualification, or optical measurements. Run from source: there is no release-qualified installer or dependency-manager EXE.

## What it does

- **Deposition modeling:** define sources, substrates, machines, correction masks, and processes; calculate thickness distributions and uniformity; explore process and mask optimization within documented limits.
- **Thin-film optics:** edit layer stacks and material n/k data, calculate spectra, inspect bands and extrema, and use thickness optimization and experimental structure search.
- **Projects and assets:** isolate inputs, unsaved changes, and results by project. Browse resources and edit materials in a unified asset library, with scoped recycling and recovery for eligible assets.
- **Results and automation:** manage calculation queues and result assets; use geometric ray tracing and appearance rendering. Some operations also have CLI, MCP, and optional AI-assistant entry points. Their capabilities and permissions differ; see the [entry-point matrix](docs/entrypoint-capabilities.md).
- **Dependency management:** a terminal-only interactive online installer and advanced standard-library CLI to inspect Python/compute environments, review plans, choose sources and mirrors, and install full end-user dependencies into an isolated virtual environment.

Models, units, sampling rules, approximations, and sources are documented in the [algorithm atlas](docs/algorithms.html). Download this self-contained HTML file and open it locally for offline reading; GitHub's source preview is not a rendered documentation site. The [software manual](docs/software_manual.html) provides fuller usage guidance. Most detailed documentation and UI text are currently in Chinese.

## Quick start

Download and extract this repository's source, or clone this repository with Git. Run the following commands from the source root. **Python 3.12** is the recommended starting point, and initial dependency installation needs network access. Start with CPU; optional compute backends require separate installation and validation.

### Recommended: interactive online installation

On Windows, double-click `install.bat`; on Linux/macOS, run `sh install.sh`. Choose an installation root, source (default [CNB](https://cnb.cool/nonereason/coating-sim), or [GitHub](https://github.com/ii33/coating-sim)), package-mirror acceleration, and development mode (default: no). The installer discovers Python/compute capabilities and selects full end-user groups `desktop + appearance`; development mode adds testing and static-analysis tools. Mirror acceleration changes the PyPI package source only, not the Git repository URL.

A new installation uses `<root>/.runtime/python`, `.runtime/git`, `app/` for source, and `app/.venv` for dependencies. Selecting a legitimate existing project performs dependency-only work, preserving its source and existing venv interpreter. Unknown contents, damaged environments and incompatible backends produce explicit blockers.

Windows bootstrap does not require preinstalled Python, Git or PowerShell 7. It uses built-in Windows PowerShell 5.1 and does not bypass script security policies. The official Python installer may write maintenance caches and per-user registration/uninstall records outside the selected root; this is not a completely portable or zero-system-footprint installation. The new interactive workflow still needs native end-to-end acceptance and is not an accepted Release. See the [installer guide](docs/dependency-manager.md).

After installation, bounded GPU numerical probes run in the actual project `.venv`. Dependency installation and GPU usability are reported separately; a failed probe does not automatically change drivers or reinstall packages.

### Windows: Python already installed

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launcher.py
```

Alternatively, double-click `install.bat` for the interactive terminal installer. With suitable Python already available, run `python dependency_manager.py`. The installer needs no third-party Python packages and has no manager GUI.

**No Python installed?** The Windows bootstrap attempts to install a full CPython runtime, including pip and venv, from official sources into the selected installation root. It requires network access and system prerequisites. End-to-end validation on a clean Windows machine with no Python remains outstanding. The managed Python candidate differs from the recommended Python 3.12, and backends such as DirectML have additional limits. Read the [bootstrap guide](docs/dependency-manager-bootstrap.md); passing source checks does not guarantee successful first-time installation.

For PowerShell 7+ development environments, `start-dev.bat` / `start-dev.ps1` prefer the project's `.venv`, then `.conda`. They neither fall back to system Python nor install dependencies. `start-dev.bat -Check` checks installed dependencies only. See the [launcher guide](docs/project-launcher-entry.md).

### Linux: Python already installed

Run `sh install.sh` for interactive installation. The installer needs neither Tk nor third-party Python packages. Linux/macOS without Python currently require manual installation of compatible full Python with venv/pip from official sources; secure automatic bootstrap is not implemented there. Linux also needs the Qt system libraries required by the distribution. This is not verified from-zero installation on every platform.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python launcher.py
```

For Qt platform-plugin errors, consult the [Qt for Linux requirements](https://doc.qt.io/qt-6/linux-requirements.html). Headless tests do not replace native desktop interaction testing. Native end-to-end macOS validation has not been completed; usability on macOS is not promised here.

### Complete example project

Open `example/synthetic-demo/project.coatproj` for a self-contained synthetic project with materials, a film design, machine, source, substrate, mask, process, and default deposition configuration. Copy the example directory before editing. The [example guide](example/README.md) documents expected results, optimization steps, and the limits of its small sampling grid. It is not a measured or production-calibrated setup.

### Your first project

1. Run `launcher.py`. Create a project in a separate directory, or open an existing `.coatproj` file.
2. Start in the film designer. Use project navigation to open deposition processes, machines, sources, masks, substrates, the asset library, or calculation windows. Project and global resources are managed separately; explicit imports copy external inputs into the project.
3. Prepare materials and inputs for your task. Start with a small CPU calculation and inspect the output. Spectra and appearance depend on optical constants, wavelength coverage, and model assumptions.
4. Explicitly save a deposition simulation configuration. **The most recently successfully saved deposition configuration becomes the next opening entry**, loading its dependencies. Saving a machine, process, or film alone does not change this entry. Recovery of unsaved input is a separate, explicitly reviewed operation.
5. After saving, use the project-management menu to export a `.cproject` package and import it into a new, independent directory. Packages contain managed inputs, not calculation results, recovery content, credentials, or the global library. They are not complete result backups. See the [package guide](docs/project-package-contract.md).

Each project runs in an independent process. Opening the same project again activates its existing session. Closing the launcher leaves projects running; the shared assistant's Exit Application action checks unsaved changes and coordinates exit. Removing a recent-project entry normally removes only that entry; recycling its directory is a separate confirmed action.

## Dependency manager

See the [dependency-manager guide](docs/dependency-manager.md) for full details. With Python already available:

```sh
python dependency_manager.py
python dependency_manager.py inspect --groups desktop --json
python dependency_manager.py benchmark --mirrors pypi,tuna,ustc --proxy-mode none --json
python dependency_manager.py plan --groups desktop --profile runtime --action install --python /path/to/python3.12 --proxy-mode env --json
# Review the plan; substitute your interpreter path and the returned PLAN_ID:
python dependency_manager.py install --groups desktop --profile runtime --python /path/to/python3.12 --proxy-mode env --plan-id PLAN_ID --yes --json
```

`/path/to/python3.12` is an example absolute path to an existing interpreter; use your actual `.exe` path on Windows. The default installation target is the project's `.venv`. The `runtime` profile selects runtime dependencies; `development` adds development tools. Optional groups such as appearance rendering must be selected explicitly. Regenerate the plan if its parameters, target, or requirements change; `--yes` does not replace a matching `plan_id`.

Mirror benchmarks are network estimates. Proxy options affect only the current tool process, not system proxy settings or global pip configuration. Do not put authentication values in command lines or commit them. A virtual environment isolates dependencies; it is not a security sandbox.

### Version checks and online source deployment

Local checks are offline by default; an explicit remote check connects to the selected source. These checks report version, commit, working-tree, and environment information without automatically updating your active installation.

```sh
python dependency_manager.py project-check --project . --json
python dependency_manager.py project-check --project . --check-remote --source github --ref main --json
python dependency_manager.py project-plan --deployment-dir ../coating-sim-checkout --source github --ref main --python /path/to/python3.12 --profile runtime --json
# Review the full commit, target, and dependency plan; use its deployment PLAN_ID:
python dependency_manager.py project-deploy --deployment-dir ../coating-sim-checkout --plan-id PLAN_ID --yes --json
```

The public repository is [ii33/coating-sim](https://github.com/ii33/coating-sim), with Git source `https://github.com/ii33/coating-sim.git`. The interactive installer defaults to CNB at `https://cnb.cool/nonereason/coating-sim`. Advanced CLI CNB deployment uses `--source cnb --repository-url https://cnb.cool/nonereason/coating-sim`; the GitHub CLI examples above do not change the interactive default.

Deployment accepts only a new directory and installs dependencies into that checkout's `.venv`. It does not overwrite your active installation, automatically launch the application, or migrate user projects. Source retrieval requires Git. Windows can install private MinGit through the separate confirmation steps below; this is not a promise of one-click success on arbitrary clean machines. See the [source deployment guide](docs/dependency-manager-deployment.md) for the complete workflow and validation limits.

### Windows without Git

The interactive installer requests confirmation of a private-Git plan when Git is missing. Advanced CLI users can review and execute a plan:

```sh
python dependency_manager.py git-plan --json
python dependency_manager.py git-install --plan-id PLAN_ID --yes --json
```

The tool downloads a pinned MinGit version from official Git for Windows sources, verifies SHA-256, and installs under the selected installation root’s `.runtime/git` without changing system PATH. Source deployment automatically discovers a successfully installed default directory. For a custom private location, use the same `--git-dir` in both commands, then select `--git <directory>/cmd/git.exe` explicitly. Downloaded Git and its bundled components retain their own licenses, not the project's MIT license.

The Windows setup sequence is: start `install.bat` → select the installation root and review Python bootstrap → choose source, package mirror and development mode → review private Git installation if needed → fetch source and review the full dependency plan. The final confirmed plan also creates `app/launch-coating.cmd` (or `.sh`) without overwriting existing files; desktop shortcuts are not created by default. Check each result; confirmations are not skipped automatically. Linux still requires an existing Python, Git, and system graphics libraries. Offline automated tests do not replace native download, installation, shortcut, and launch validation on Windows with neither Python nor Git installed.

### Create launch scripts and shortcuts

After deploying and checking the project's `.venv`, create an entry tied only to that project environment:

```sh
python dependency_manager.py launch-plan --project . --shortcut none --json
# Review filenames and targets; use this launch-entry plan's PLAN_ID:
python dependency_manager.py launch-create --project . --shortcut none --plan-id PLAN_ID --yes --json
```

The default creates `Coating Simulator.cmd` on Windows or `Coating Simulator.sh` on Linux in the project root. On Windows, replace `none` in both commands with `desktop` or `start-menu` to explicitly create a shortcut using the project icon. For a custom destination, use `custom --shortcut-dir <existing-directory>`. Set a different name with `--launch-name`. Existing same-name files are never overwritten. Creation does not launch the app, register startup tasks, or fall back to system Python. Native Windows shortcuts still require validation on the target machine.

## Platform and compute boundaries

- **CPU is the default starting point.** Backend availability depends on the algorithm. The current desktop film-optimization worker path is CPU-based.
- **GPU is not a universal switch.** CUDA and DirectML depend on the OS, Python version, driver, device, and task. Successful installation or device detection is not numerical validation. The DirectML profile is restricted to Windows / Python 3.10–3.12.
- **CUDA appearance rendering is a research preview.** A low-resolution workflow has been tested on specific NVIDIA hardware. This is not a guarantee of full-resolution performance or color accuracy on all devices. That appearance backend does not implement AMD / Intel GPU support.
- **Native validation and automated tests are separate evidence.** Windows smoke tests, Linux offscreen tests, CI, network installation, and physical calibration have different scopes. Clean-system first installation, broad platform/driver coverage, and complete online deployment still require separate acceptance testing.

Further details: [compute-device selection](docs/compute-host-selection.md), [GPU appearance validation limits](docs/gpu-product-integration-evidence-20261013.md), and [testing](docs/testing.md). Historical acceptance reports describe their own snapshots; their conclusions do not automatically apply to later changes.

## Development and contributions

Reproducible bug reports, documentation improvements, and tested fixes are welcome. Include the version or commit, OS, Python/backend versions, minimal reproduction steps, and sanitized logs. Do not upload private designs, credentials, or complete runtime directories.

After installing development/test dependencies, use the project test runner:

```sh
python -m pip install -r requirements-test.txt
python tools/run_tests.py quick
python tools/run_tests.py all
```

Use `all --collect-only` to audit collection. See [testing](docs/testing.md) for algorithm, contract, and Qt tiers, and explicit opt-ins for real GPU/remote tests. Static analysis is a separate check; see the [analysis guide](docs/static-analysis.md).

Algorithm changes must update the offline `docs/algorithms.html` in the same change, documenting inputs/outputs, units, formulas, diagrams, implementation locations, approximations, and reliable sources. New or cross-module UI behavior must document state ownership and failure/cancellation boundaries and include regression coverage. See [AGENTS.md](AGENTS.md).

Do not post exploit details, keys, or private data in public security issues. If the repository enables GitHub private vulnerability reporting, use that feature. Otherwise, ask the maintainer for a private reporting channel before sharing sensitive attachments.

## Documentation

- [Algorithm atlas and references](docs/algorithms.html) · [Software manual](docs/software_manual.html)
- [Thin-film spectral simulation](docs/film-simulation-quick.md) · [Optimization methods](docs/film-optimization-methods.md)
- [Unified assets and material editing](docs/unified-asset-library-contract.md) · [Last-saved configuration policy](docs/project-recent-configuration-contract.md)
- [CLI / MCP / assistant capability matrix](docs/entrypoint-capabilities.md) · [MCP setup](docs/mcp.md)
- [Workflows](docs/workflows.md) · [Changelog](docs/CHANGELOG.md)

## Author and licensing

Author and maintainer: **ii33**. Project-owned code is licensed under the [MIT License](LICENSE), Copyright (c) 2026 ii33.

Third-party content retains its own licensing and is not relicensed by this project's MIT declaration:

- Bundled refractiveindex.info optical-data snapshots retain [CC0 1.0](data/optical_catalog_sources/sourced_optical_constants_20261006_v1/refractiveindex_LICENSE) and their individual source records.
- CIE illuminant/observer datasets and the identified adaptations use CC BY-SA 4.0. Preserve attribution, license, and modification notices; see the [data notice](data/film_design/cie-illuminants-20261006/NOTICE_DATA.md). This license does not extend to the whole application or all CIE publications.
- Bundled Noto CJK fonts use [SIL OFL 1.1](coat_sim/ui/assets/fonts/noto-cjk/LICENSE). Python, Qt, and other dependencies retain their respective licenses.
- Icons and illustrations follow their own provenance and rights statements; see the [artwork record](docs/artwork-provenance.md). Permission to include artwork publicly does not establish its provenance or grant additional redistribution rights.

Historical [packaging records](docs/dependency-manager-distribution.md) do not establish a currently qualified release package.
