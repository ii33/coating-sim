# Independent dependency-manager contract

## Current delivery, scope and interface truth

The dependency manager is delivered as Python source. On Windows,
`dependency_manager.cmd` starts it by discovering a compatible full CPython with
venv/pip/Tcl/Tk, or provisioning one from the pinned official Python manifest
when none is available. The bootstrap does not install project dependencies or
start the project; those are separate, reviewable dependency-manager operations.

Executable packaging was cancelled on 2026-10-14. The manager does not
currently produce or distribute an EXE. The legacy packaging entrypoints refuse
execution, including test builds. Build catalogs, scripts, native-payload data
and licence records are retained only as historical evidence; any later binary
proposal would require its own platform review and must continue to satisfy the
existing release policy and redistribution gates. The intended current scope is
project-specific dependency inspection, plans, install/repair, runtime and
development profiles, mirror measurements, transient proxy configuration, and
optional checkout/dependency deployment for `ii33/coating-sim` (or the explicitly selected legacy development source). It is not a
generic project builder or online updater.

`tools.dependency_manager.manager` is the sole inspection/planning/execution
owner; `mirrors` owns transient proxies and bounded source measurements;
`runtime` owns the Python-accessible hash-pinned normal CPython installation flow;
`bootstrap_windows.ps1` supplies the initial source launcher before Python exists.
Both consume the same runtime manifest. Python modules use the standard library
and do not import `coat_sim`, Qt or the scientific runtime. CLI and Tk GUI call
the same functions. No numerical algorithms or optical file formats change, so
there is no additional `algorithms.html` entry.

Primary interfaces include `inspect_environment(root, venv=None,
groups=("desktop",), backend="cpu", profile="runtime")`,
`plan_environment(root, venv=None, groups=("desktop",), backend="cpu",
mirror="pypi", action="repair", python=None, proxy=None, profile="runtime")`,
and `execute_plan(plan, confirmed=False, emit=None, cancel_event=None,
proxy=None)`. Runtime provisioning uses `plan_runtime`/`execute_runtime`, mirror
measurement uses `benchmark_mirrors`, and fixed-repository checkout uses
`plan_project_deployment`/`execute_project_deployment`.
JSON-compatible results have schema_version 1, operation and explicit status.
Inspection/plan results include root, target, groups, backend, packages, warnings
and errors. Each row includes name, requirement, installed_version, status
(`satisfied|missing|conflict|unsupported`), source and dependency_issues.
Plans additionally contain mirror/proxy public identity, actions (argv arrays,
never raw shell), requires_confirmation and a SHA-256 plan_id over the full
object. Execution results include that plan ID and final inspection when possible.
Code/data structures and contract tests are the authoritative interface.

## Target identity and lifecycle

Default target: project `.venv`. Explicit `--venv` is displayed completely and
canonicalized. No implicit system-Python installation fallback. An existing
prefix must have pyvenv.cfg, disabled system site packages, matching reported
prefix, distinct base_prefix and normal CPython3.10–3.13. Symlink targets or
changed parent/interpreter identities fail closed. Installed direct versions,
requested extras and recursive metadata dependencies are checked without imports.
Pin mismatch is a conflict even when pip check would succeed.

Missing venv is inspectable/planable. Creation requires explicitly selected normal
CPython with venv/ensurepip and supported bundled pip. Frozen tool executables and
CPython embedded packages are not bootstrap interpreters. Before execution, plans
are rebuilt/compared; after acquiring the operation lock they are compared again.
Before each mutation parent/target identity is checked; immediately after creating
a venv, Python/platform/machine/base-prefix must match the selected bootstrap
before pip is allowed. Existing non-venv or corrupt interpreter directories are
not overwritten. Missing pip can be repaired with ensurepip; unsupported old pip
(<22.2 dry-run support) gives an explicit blocker instead of a misleading ready
plan. An unrecoverable existing venv should be preserved and a new target selected.

Install adds selected missing requirements; repair also reconciles selected
conflicts. Wheel dry-run precedes installation; only binary packages are accepted.
The resolver may replace relevant dependencies; the tool never explicitly removes
unrelated packages. Final inspection proves requirements, not merely pip exit=0.
Failures preserve actionable information; error is not presented as empty data.

## Group/platform/backend compatibility

Catalog reads actual requirements.txt, requirements-test.txt and
requirements-sandbox.txt, partitions desktop CPU/GUI/assistant/interchange,
appearance renderer pair, tests and sandbox pins, expands dependencies and
deduplicates declarations. Desktop covers all 15 direct runtime requirements.
Unsupported requirement syntax, URLs, pip options or arbitrary shell are rejected.
CPU needs no GPU packages. CUDA12 has an explicit Windows/Linux x86-64 CuPy profile;
DirectML requires Windows and a selected 3.10–3.12 interpreter. Managed3.13 is
blocked for DirectML before download. ROCm/XPU/vendor-specific installs remain
manual-only. Package presence is not hardware/driver/kernel validation.

## Sources, proxies and measurements

Only documented HTTPS PyPI/TUNA/USTC sources are selectable. TLS verification is
normal; no trusted-host, arbitrary remote URL, global pip setting or system-proxy
changes. PIP_CONFIG_FILE=os.devnull and isolated pip ignore inherited configuration.
Proxy modes: none/env/explicit HTTP CONNECT, transient only. Env HTTP/HTTPS target settings with http:// proxy endpoints take
precedence over unused ALL_PROXY; effective HTTPS-proxy/SOCKS transports are explicitly unsupported.
Authenticated proxies never enter argv or default saved state. UI/JSON/logs expose
only redacted endpoints; URL credentials/queries/common tokens are removed.

Benchmark obtains at most1MiB of simple index and at most256KiB of the fixed
pip25.2 wheel, with per-request timeout, per-mirror total deadline and cancellation.
Rows show actual final download host/origin and measurement bytes. Index latency
and download throughput including connection overhead are estimates. A mirror
using PyPI CDN cannot rank as that mirror's own package throughput; official PyPI
using its own files.pythonhosted.org CDN is eligible as PyPI. Recommendations
require the user's acceptance, never silently change sources.

## Dependency profiles and source bootstrap

Project dependency profiles are explicit: `runtime` is the default and selects
only declared runtime requirements through the chosen feature groups/backend;
`development` composes that selection with direct pytest/mpmath requirements
and direct Ruff/mypy pins from requirements-analysis.txt. Its three known local
`-r` includes are recognized but never recursively installed. Unknown options,
URLs/includes or tooling declarations fail closed. Development never silently
enables appearance renderers, sandbox pins or a GPU backend. CLI `--profile` and
the GUI profile selector call the same catalog/inspect/plan implementation;
profile is part of result schemas and confirmed-plan revalidation.

`build_catalog(root)` reads the separate historical tool-build catalog from
`requirements-dependency-manager-build.txt`, with rows marked
`group="build"`, `scope="separate-build-venv"`. These pins are not a project
install profile and are not installed into the project `.venv` or the manager's
source runtime. The packaging route was cancelled; no current build operation
consumes this catalog for a release. The scripts and native-payload evidence
remain archival only. The [historical distribution record](dependency-manager-distribution.md)
retains profile-specific provenance and the existing fail-closed release policy;
its records do not establish that a current binary has been built or accepted.

The compatibility-first Tk GUI is an approved exception to Qt Quick styling.
The source manager requires normal Python; GUI use also requires a working Tk
installation. Windows may provision full CPython through the source bootstrap.
Linux/macOS do not have an automatic runtime bootstrap in this version and need a
suitable installed Python/Tk. The previous Linux native-payload evidence is
specific to its recorded profile; Windows and other profiles remain subject to
all existing rights, notice, native-closure and platform-acceptance gates.

Managed bootstrap pins official Windows CPython3.13.16 installer URLs and SHA256,
architecture/source/license in runtime_manifest.json. Hash must match before
execution. Destination is a new canonical path inside the user's home, checked
under lock and before installer invocation. Existing registered same-minor CPython
blocks to avoid maintenance of other environments. Parameters prohibit PATH,
launcher, associations/shortcuts and machine-wide install; per-user uninstall
records and CPython license are disclosed before confirmation. Missing or outdated system UCRT blocks without elevation. VC runtime DLLs are
installed application-locally by the official Python installer. Linux/macOS automatic official runtime
provisioning is unsupported in this first version; selecting existing Python works.
3.12 project QA does not establish3.13 project numerical/GUI compatibility.

## Progress, cancellation and GUI state

One GUI owner holds one operation, launches a worker thread, and uses a queue/
Tk.after to update widgets exclusively on the main thread. Inputs/duplicate actions
are disabled while active; the visible cancel remains enabled. Prior valid package
inspection survives broken-target errors. Logs are bounded/redacted, retained in
memory, and separate from the main table. Initial, loading and error states are
visible; no unsaved document exists. Refresh preserves table selection/scroll.

CLI --json gives one final object; --events gives schema-version-1 JSON lines
(started/phase/progress/log/result). SIGINT/SIGTERM request cancellation. Subprocess
argv never uses shell=True. POSIX process groups and absolute Windows taskkill /T
best-effort stop descendants; taskkill failure falls back to killing/waiting for
main process and explicitly warns descendants may remain. Cancellation/timeout
may leave partial packages or runtime, so retry reinspects, replans and confirms.
Close while active asks to request cancellation/close without promising every
external descendant has stopped. Closed generations never update destroyed UI.

Window1100×760, min720×520, native ttk and platform DPI handling; no project Qt
Theme imports or duplicate font scaling. Top compact readable full paths; primary
tab group selection/table/plan, separate mirrors+proxy/runtime/log tabs, fixed
bottom actions. Exact native rendering/keyboard/100–200%DPI and noPython Windows
clean-VM behavior require real GUI/packaged acceptance. The current Linux validation environment has no display server, so GUI
rendering remains explicitly unverified.
