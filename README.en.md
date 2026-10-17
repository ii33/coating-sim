# Deposition Sim · Planetary Coating Simulation

[中文](README.md) | English

A desktop tool for optical thin-film research and engineering development, bringing thin-film design, planetary evaporation deposition, thickness maps, spectral analysis, and result assets into independent projects.

**This is the `dev.20261016.19` development snapshot, not a formal Release.** The project has not completed comprehensive physical calibration. Simulation and optimization support analysis, comparison, and research; they do not replace equipment calibration, process qualification, or optical measurements. Application and independent-installer versions and acceptance scopes are recorded separately.

## What it does

- **Deposition modeling:** define sources, substrates, machines, correction masks, and processes; calculate thickness distributions and uniformity; explore process and mask optimization within documented limits.
- **Thin-film optics:** edit layer stacks and material n/k data, calculate spectra, inspect bands and extrema, and use thickness optimization and experimental structure search.
- **Projects and assets:** isolate inputs, unsaved changes, and results by project. Browse resources and edit materials in a unified asset library, with scoped recycling and recovery for eligible assets.
- **Results and automation:** manage calculation queues and result assets; use geometric ray tracing and appearance rendering. Some operations also have CLI, MCP, and optional AI-assistant entry points. Their capabilities and permissions differ; see the [entry-point matrix](docs/entrypoint-capabilities.md).
- **Installation:** the [independent installer](https://github.com/ii33/coating-sim-installer) owns deployment; the application supplies declarative dependencies.

Models, units, sampling rules, approximations, and sources are documented in the [algorithm atlas](docs/algorithms.html). Download this self-contained HTML file and open it locally for offline reading; GitHub's source preview is not a rendered documentation site. The [software manual](docs/software_manual.html) provides fuller usage guidance. Most detailed documentation and UI text are currently in Chinese.

## Quick start

### Independent installer

Use [ii33/coating-sim-installer](https://github.com/ii33/coating-sim-installer) or its [CNB mirror](https://cnb.cool/nonereason/coating-sim-installer). Windows EXE downloads and installer commands are maintained in that project's [Releases](https://github.com/ii33/coating-sim-installer/releases) and [guide](https://github.com/ii33/coating-sim-installer/blob/main/README.en.md). This application repository contains no installer copy.

### Run application source with existing Python

Use a full compatible Python (3.12 recommended) and a project-local environment:

```sh
python -m venv .venv
# Windows: .venv\Scripts\python.exe; Unix: .venv/bin/python
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python launcher.py
```

On Windows substitute `.venv\Scripts\python.exe` in those commands. Appearance rendering also needs Mitsuba/DrJit, currently declared in `requirements-test.txt`; the independent installer selects `desktop + appearance`. Linux needs the distribution's Qt system libraries. On Windows use `start-dev.bat` (PowerShell 7+); `start.sh` and `start-cloud.sh` remain cross-platform application entry points. See the [project-side installation contract](docs/installation.md).

### Complete example project

Open `example/synthetic-demo/project.coatproj` for a self-contained synthetic project with materials, a film design, machine, source, substrate, mask, process, and default deposition configuration. Copy the example directory before editing. The [example guide](example/README.md) documents expected results, optimization steps, and the limits of its small sampling grid. It is not a measured or production-calibrated setup.

### Your first project

1. Run `launcher.py`. Create a project in a separate directory, or open an existing `.coatproj` file.
2. Start in the film designer. Use project navigation to open deposition processes, machines, sources, masks, substrates, the asset library, or calculation windows. Project and global resources are managed separately; explicit imports copy external inputs into the project.
3. Prepare materials and inputs for your task. Start with a small CPU calculation and inspect the output. Spectra and appearance depend on optical constants, wavelength coverage, and model assumptions.
4. Explicitly save a deposition simulation configuration. **The most recently successfully saved deposition configuration becomes the next opening entry**, loading its dependencies. Saving a machine, process, or film alone does not change this entry. Recovery of unsaved input is a separate, explicitly reviewed operation.
5. After saving, use the project-management menu to export a `.cproject` package and import it into a new, independent directory. Packages contain managed inputs, not calculation results, recovery content, credentials, or the global library. They are not complete result backups. See the [package guide](docs/project-package-contract.md).

Each project runs in an independent process. Opening the same project again activates its existing session. Closing the launcher leaves projects running; the shared assistant's Exit Application action checks unsaved changes and coordinates exit. Removing a recent-project entry normally removes only that entry; recycling its directory is a separate confirmed action.

## Installation and dependency declarations

Installer implementation, updates, runtime provisioning and packaging belong to the [independent repository](https://github.com/ii33/coating-sim-installer) and [CNB mirror](https://cnb.cool/nonereason/coating-sim-installer). The application retains its requirements, locks, environment specification and runtime capabilities. The [consumption contract](docs/installation.md) documents alpha.1 compatibility.

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

- [Independent installer and dependency contract](docs/installation.md) · [Existing runtime ZIP guide](docs/portable-bundle.md) · [Project and result assets](docs/portable-assets.md)

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

Installation notices and release evidence are maintained in the [independent installer](https://github.com/ii33/coating-sim-installer).

[Project layout and historical-tool cleanup](docs/development/root-layout.md).

[Research evidence and ordinary MCP services](docs/research-integration-contract.md) · [研究证据格式](docs/algorithms.html#project-research-evidence)

[New optical dataset from measured R/T: guide and limits](docs/optical-inversion-guide.md) · [Inversion algorithm and ambiguity](docs/algorithms.html#optical-constant-inversion)
