# Independent dependency-manager contract

## Current delivery, scope and interface truth

The current entry is a terminal-only interactive online installer: `install.bat` on Windows, `sh install.sh` on Unix, or `python dependency_manager.py` with suitable Python. The explicit equivalent is `python dependency_manager.py interactive --project /path/to/install-root`. The manager GUI is cancelled. Frozen EXE packaging is retired; historical records are not current release evidence. Advanced non-GUI CLI operations remain supported.

The installer itself uses only the Python standard library and imports neither `coat_sim`, Qt nor the scientific runtime. Windows uses built-in Windows PowerShell 5.1 to bootstrap full CPython when absent; preinstalled Python, Git and PowerShell 7 are not prerequisites. Script-policy/signature blocks are not bypassed. Unix checks local managed Python then PATH; Linux/macOS without suitable full Python receive official manual-install guidance, not an unimplemented automatic bootstrap promise.

Interaction selects installation root, CNB (default `https://cnb.cool/nonereason/coating-sim`) or GitHub (`ii33/coating-sim`), mirror acceleration (default no; TUNA PyPI only), and development mode (default no). It discovers Python/compute evidence, offers project-local Python, and confirms required runtime/Git and complete dependency plans. End-user groups are `desktop + appearance`; development adds testing/static-analysis tools. Discovery/recommendation is not GPU numerical acceptance. The final confirmed plan includes safe creation of `app/launch-coating.cmd` or `.sh`, with no overwrite or default desktop shortcut. Bounded GPU numerical probes then use the actual project venv; dependency success and GPU usability are separate outcomes, and failure does not silently change drivers or reinstall. Human-readable summaries accompany detailed redacted JSON under root/.dependency-manager/logs.

New installations use `<root>/.runtime/python`, `.runtime/git`, `app/`, and `app/.venv`. A valid project already at root or root/app receives dependency-only installation: source and existing venv interpreter are preserved. Unknown contents are not overwritten. Official Python installers may write maintenance caches and per-user registration/uninstall records outside root; this is not a zero-system-footprint portable installation.

`manager` owns dependency inspection/planning/execution; `interactive` orchestrates terminal choices; `mirrors` owns transient proxies and bounded measurements; `runtime` owns hash-pinned full CPython installation; `bootstrap_windows.ps1` runs before Python exists. Bootstrap and runtime share the pinned manifest. No numerical algorithms or optical file formats change. The new source workflow awaits native end-to-end acceptance; no Release is established by source checks.

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

Missing venv is inspectable/planable. Creation requires a verified normal
CPython with venv/ensurepip and supported bundled pip. If no creation interpreter
is explicitly supplied, `python_discovery.discover_python(backend, explicit=None, project=root)`
checks bounded known locations and recommends the first compatible interpreter.
An explicit selection remains pinned and never falls back. The resulting plan
records the chosen interpreter for confirmation and identity revalidation. Frozen tool executables and
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
enables appearance renderers, sandbox pins or a GPU backend. CLI `--profile` and the interactive development choice call the same catalog/inspect/plan implementation;
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

The terminal manager requires normal full Python, but neither Tk nor any third-party Python package. Windows may provision full CPython through its native source bootstrap. Linux/macOS automatic runtime bootstrap is not implemented; official manual guidance is required when Python is absent. Prior GUI/native-payload evidence does not validate this new terminal workflow.

Managed bootstrap pins official Windows CPython3.13.16 installer URLs and SHA256,
architecture/source/license in runtime_manifest.json. Hash must match before
execution. Destination is a new canonical path under the selected installation root's `.runtime/python`, checked
under lock and before installer invocation. Existing registered same-minor CPython
blocks to avoid maintenance of other environments. Parameters prohibit PATH,
launcher, associations/shortcuts and machine-wide install; possible external maintenance-cache and per-user registration/uninstall records, and the CPython license, are disclosed before confirmation. Missing or outdated system UCRT blocks without elevation. VC runtime DLLs are
installed application-locally by the official Python installer. Linux/macOS automatic official runtime
provisioning is unsupported in this first version; selecting existing Python works.
3.12 project QA does not establish3.13 project numerical/GUI compatibility.

## Progress and cancellation

Interactive prompts display redacted plans and require affirmative confirmation before execution. CLI `--json` gives one final object; `--events` gives schema-version-1 JSON lines (started/phase/progress/log/result). SIGINT/SIGTERM request cancellation where supported; interactive Ctrl+C routes cancellation into operation cleanup.

Subprocess argv never uses shell=True. POSIX process groups and absolute Windows taskkill /T best-effort stop descendants; fallback killing/waiting for the main process warns when descendants may remain. Cancellation/timeout may leave partial packages or runtime, so retry requires inspection, replanning and confirmation. A started but interrupted or uncertain official installer retains lock/download/recovery resources for manual inspection, with no automatic cleanup/retry. Bootstrap logs use `<root>/.dependency-manager/logs`; persistent runtime installation logs and unique download/temp directories stay under `<root>/.runtime`. Child-only TEMP/TMP/TMPDIR overrides do not change global environment settings.

Native Windows zero-start deployment, terminal interaction, downloaded runtime execution, process termination, launch entries and actual application startup require matching end-to-end acceptance. Previous GUI testing and offline automated checks do not substitute for it.

## Interpreter discovery and DirectML diagnostics (2026-10-16)

`python_discovery` owns read-only candidate discovery; `manager` remains the only
plan/execution owner. `discover_python(backend="cpu", explicit=None, *, project=None, cancel_event=None)` returns
`status` (`ready|missing|cancelled`), `selected_python` (path or null), `candidates` and
`errors`. Each candidate has path, source, compatible, reason and a minimal
identity including CPython implementation (or null after failed probing).
The managed candidate uses the selected project's runtime directory; a frozen
tool without project context skips that candidate rather than guessing a storage
location. Discovery does not install anything or
change settings. Sources are the manager's normal base interpreter, managed
runtime path, bounded PATH names, Windows `py -0p`, and Python registry entries.
No whole-disk search. Probes use isolated Python and a 2-second timeout, capped
at 12 candidates; the optional Windows launcher list has a 2-second timeout.
Cancellation is checked before discovery, between probes and before returning
a selection; a cancelled result has no selected interpreter. A running probe is
bounded by its timeout rather than force-interrupted. Discovery must not replace an explicitly selected path.

An existing target venv is authoritative: the creation selector does not replace,
downgrade or recreate it. Automatic selection applies only to a missing target
with no explicit creation interpreter. The final plan binds the selected path,
so revalidation does not rediscover a different interpreter.

Blocked DirectML inspect/plan results include optional `backend_diagnostic`
(null for no DirectML compatibility blocker), with backend, reason,
identity_source (`target_venv|creation_python`), python, platform, interpreter,
probe_error and remediation. Unknown/unprobeable identity is reported as unknown,
not inferred from the manager runtime. Existing-target probe failures remain in
errors. A compatible prospective creation identity clears this diagnostic without
relaxing any existing target identity or wheel-resolution checks.

### Child process lifecycle evidence

`run_command(..., *, outcome=None, temporary_directory=None)` retains its integer
return. Optional outcome reports started, completed, stopped, returncode,
parent_exited and tree_termination_confirmed. Only a normal exit sets completed;
a timeout/cancellation does not, even after the parent is observed to exit.
Current process-group termination cannot prove that detached installer children
are gone, so tree_termination_confirmed remains false. Runtime callers retain
recovery resources after a started but interrupted/uncertain execution. The
optional temporary_directory overrides TEMP/TMP/TMPDIR only in the child environment;
it does not mutate global environment variables or create that directory.
