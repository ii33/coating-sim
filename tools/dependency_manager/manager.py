"""Standard-library inspection, planning and bounded project-venv maintenance."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from .mirrors import MIRRORS, ProxyConfig, redact

SCHEMA_VERSION = 1
GROUPS = {
    "cpu": "CPU / 科学计算",
    "gui": "桌面 GUI",
    "assistant": "桌面助手",
    "interchange": "CAD / 表格 / 凭据",
    "appearance": "CPU 外观渲染",
    "test": "开发与测试",
    "sandbox": "Sandbox 精确版本",
    "desktop": "完整桌面运行依赖",
}
PROFILES = {
    "runtime": "运行时 / 分发",
    "development": "开发（运行时 + 测试 + 静态检查）",
}
BACKENDS = ("cpu", "cuda12", "directml", "rocm", "xpu")
_ASSIGN = {
    "numpy": "cpu",
    "scipy": "cpu",
    "matplotlib": "cpu",
    "pillow": "cpu",
    "pydantic": "cpu",
    "pyside6-essentials": "gui",
    "pyside6-addons": "gui",
    "keyring": "interchange",
    "ezdxf": "interchange",
    "openpyxl": "interchange",
}
_REQ = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9_,.-]+\])?((?:(?:==|>=|<=|>|<|!=)[0-9][A-Za-z0-9.*+!-]*)(?:,(?:==|>=|<=|>|<|!=)[0-9][A-Za-z0-9.*+!-]*)*)?$"
)


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_requirements(path):
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = _REQ.fullmatch(line)
        if not match:
            raise ValueError(
                f"Unsupported requirement syntax: {path.name}:{number}; URLs, options and commands are forbidden"
            )
        rows.append(
            {
                "name": normalized(match[1]),
                "requirement": line,
                "source": path.name,
                "specifier": match[3] or "",
            }
        )
    return rows


def _read_analysis_tooling(path):
    """Read only direct Ruff/mypy entries; never recurse the analysis lock."""
    allowed_includes = {
        "requirements.txt",
        "requirements-test.txt",
        "requirements-analysis-lock.txt",
    }
    direct_tools = {"ruff", "mypy"}
    rows = []
    seen = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            if line[3:].strip() not in allowed_includes:
                raise ValueError(
                    f"Unsupported analysis include: {path.name}:{number}"
                )
            continue
        match = _REQ.fullmatch(line)
        if not match or normalized(match[1]) not in direct_tools:
            raise ValueError(
                f"Only direct Ruff/mypy entries are supported: {path.name}:{number}"
            )
        name = normalized(match[1])
        if name in seen:
            raise ValueError(f"Duplicate direct tooling entry: {path.name}:{number}")
        seen.add(name)
        rows.append(
            {
                "name": name,
                "requirement": line,
                "source": path.name,
                "specifier": match[3] or "",
                "group": "static-tooling",
            }
        )
    if seen != direct_tools:
        raise ValueError("requirements-analysis.txt must declare direct Ruff and mypy")
    return rows


def build_catalog(root):
    """Tool-build tops only; this scope is never a project-install profile."""
    rows = _read_requirements(Path(root).resolve() / "requirements-dependency-manager-build.txt")
    for row in rows:
        row.update(group="build", scope="separate-build-venv")
    return rows


def catalog(root, groups=("desktop",), backend="cpu", profile="runtime"):
    root = Path(root).resolve()
    groups = list(dict.fromkeys(groups))
    if not groups or any(group not in GROUPS for group in groups):
        raise ValueError("Select at least one known dependency group")
    if backend not in BACKENDS:
        raise ValueError("Unknown compute backend")
    if profile not in PROFILES:
        raise ValueError("Unknown dependency profile")
    expanded = set(groups)
    if "desktop" in expanded:
        expanded.update(("cpu", "gui", "assistant", "interchange"))
    if expanded.intersection(("gui", "assistant", "interchange", "appearance", "test")):
        expanded.add("cpu")
    rows = []
    for row in _read_requirements(root / "requirements.txt"):
        row["group"] = _ASSIGN.get(row["name"], "assistant")
        if row["group"] in expanded:
            rows.append(row)
    if expanded.intersection(("test", "appearance")):
        for row in _read_requirements(root / "requirements-test.txt"):
            row["group"] = (
                "appearance" if row["name"] in ("mitsuba", "drjit") else "test"
            )
            if row["group"] in expanded:
                rows.append(row)
    if profile == "development":
        # Development is an orthogonal profile: include the test runner and
        # oracle, plus only the direct static tools from the dedicated list.
        # Do not turn the analysis lock's full transitive inventory into roots.
        for row in _read_requirements(root / "requirements-test.txt"):
            if row["name"] in ("pytest", "mpmath"):
                row["group"] = "development-test"
                rows.append(row)
        rows.extend(_read_analysis_tooling(root / "requirements-analysis.txt"))
    if "sandbox" in expanded:
        for row in _read_requirements(root / "requirements-sandbox.txt"):
            row["group"] = "sandbox"
            rows.append(row)
    if backend in ("cuda12", "directml"):
        requirement = (
            "cupy-cuda12x>=13,<14" if backend == "cuda12" else "torch-directml"
        )
        rows.append(
            {
                "name": normalized(requirement.split(">=")[0]),
                "requirement": requirement,
                "source": "optional-backend-catalog",
                "specifier": ">=13,<14" if backend == "cuda12" else "",
                "group": "gpu",
            }
        )
    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        previous = dedup.get(row["name"])
        if previous and previous["requirement"] != row["requirement"]:
            raise ValueError(f"Conflicting project declarations: {row['name']}")
        dedup[row["name"]] = row
    return groups, list(dedup.values())


# Requirements are stdin JSON; no shell, interpolation or imports of package code.
_PROBE = r"""
import importlib.metadata as md, json, os, platform, sys
rows = json.load(sys.stdin)
try:
 from pip._vendor.packaging.requirements import Requirement
except ImportError:
 Requirement = None
inventory = {}
for d in md.distributions():
 name = d.metadata.get('Name', '')
 if name: inventory[name.lower().replace('_','-').replace('.','-')] = d.version
checks = {}
dependency_issues = {}
if Requirement:
 for row in rows:
  r = Requirement(row['requirement'])
  version = inventory.get(r.name.lower().replace('_','-').replace('.','-'))
  checks[row['name']] = bool(version and r.specifier.contains(version, prereleases=True))
  issues, visited, pending = [], set(), [(r.name, r.extras)]
  while pending:
   parent, extras = pending.pop()
   key = (parent.lower(), tuple(sorted(extras)))
   if key in visited: continue
   visited.add(key)
   try: distribution = md.distribution(parent)
   except md.PackageNotFoundError: continue
   for declaration in distribution.requires or []:
    child = Requirement(declaration)
    if child.marker and not any(child.marker.evaluate({'extra': extra}) for extra in ('', *extras)): continue
    child_name = child.name.lower().replace('_','-').replace('.','-')
    child_version = inventory.get(child_name)
    if not child_version or not child.specifier.contains(child_version, prereleases=True):
     issues.append({'requirement': str(child), 'installed_version': child_version})
    elif (child.name.lower(), tuple(sorted(child.extras))) not in visited:
     pending.append((child.name, child.extras))
  dependency_issues[row['name']] = issues
print(json.dumps({'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
 'executable': sys.executable, 'python': platform.python_version(),
 'platform': sys.platform, 'machine': platform.machine(),
 'implementation': platform.python_implementation(),
 'has_venv': __import__('importlib.util',fromlist=['find_spec']).find_spec('venv') is not None,
 'has_ensurepip': __import__('importlib.util',fromlist=['find_spec']).find_spec('ensurepip') is not None,
 'bundled_pip': __import__('ensurepip').version() if __import__('importlib.util',fromlist=['find_spec']).find_spec('ensurepip') is not None else None,
 'inventory': inventory, 'checks': checks, 'dependency_issues': dependency_issues, 'pip': inventory.get('pip')}))
"""


def clean_environment(proxy=None):
    env = dict(os.environ)
    for name in list(env):
        if name.upper().startswith("PIP_") or name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
        ):
            env.pop(name, None)
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PYTHONUNBUFFERED"] = "1"
    if proxy:
        changes = proxy.environment()
        for name, value in changes.items():
            if value:
                env[name] = value
            else:
                env.pop(name, None)
    return env


def probe_python(path, rows=()):
    result = subprocess.run(
        [str(path), "-I", "-c", _PROBE],
        input=json.dumps(list(rows)),
        text=True,
        capture_output=True,
        timeout=20,
        env=clean_environment(),
    )
    if result.returncode:
        raise ValueError("Interpreter probe failed: " + redact(result.stderr[-1500:]))
    data = json.loads(result.stdout)
    if data.get("implementation") != "CPython" or not re.match(
        r"3\.(1[0-3])\.", data.get("python", "")
    ):
        raise ValueError("A normal CPython 3.10–3.13 interpreter is required")
    return data


def _satisfies(version, specifier):
    """Fail-closed numeric fallback when target pip's PEP-440 parser is absent."""
    if not specifier:
        return True
    if not re.fullmatch(r"\d+(?:\.\d+)*", version):
        return False
    for clause in specifier.split(","):
        match = re.fullmatch(r"(==|!=|>=|<=|>|<)(\d+(?:\.\d+)*)", clause)
        if not match:
            return False
        a, b = tuple(map(int, version.split("."))), tuple(map(int, match[2].split(".")))
        size = max(len(a), len(b))
        a += (0,) * (size - len(a))
        b += (0,) * (size - len(b))
        if not {
            "==": a == b,
            "!=": a != b,
            ">=": a >= b,
            "<=": a <= b,
            ">": a > b,
            "<": a < b,
        }[match[1]]:
            return False
    return True


def inspect_environment(
    root, venv=None, groups=("desktop",), backend="cpu", profile="runtime"
):
    root = Path(root).resolve()
    groups, rows = catalog(root, groups, backend, profile)
    requested = Path(venv or root / ".venv").absolute()
    was_symlink = requested.is_symlink()
    venv = requested.resolve()
    interpreter = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    target = {
        "venv": str(venv),
        "interpreter": str(interpreter),
        "exists": venv.exists(),
        "valid": False,
        "prefix": "",
        "base_prefix": "",
        "python": "",
        "platform": sys.platform,
        "machine": platform.machine(),
        "pip": None,
        "bundled_pip": None,
    }
    errors, warnings, data = [], [], {}
    parent = venv.parent
    if parent.is_dir():
        stat = parent.stat()
        target["parent_identity"] = {
            "path": str(parent),
            "device": stat.st_dev,
            "inode": stat.st_ino,
        }
    else:
        target["parent_identity"] = None
    if was_symlink:
        errors.append("Target venv directory must not be a symlink")
    elif venv.exists():
        config = venv / "pyvenv.cfg"
        if not config.is_file():
            errors.append(
                "Existing target has no pyvenv.cfg; refusing system/non-venv target"
            )
        elif re.search(
            r"include-system-site-packages\s*=\s*true",
            config.read_text(encoding="utf-8").lower(),
        ):
            errors.append("System site packages must be disabled")
        elif not interpreter.is_file():
            errors.append(
                "Target venv interpreter is missing; explicit repair/bootstrap is required"
            )
        else:
            try:
                data = probe_python(interpreter, rows)
                target.update({key: data[key] for key in target if key in data})
                target["valid"] = (
                    Path(data["prefix"]).resolve() == venv.resolve()
                    and Path(data["base_prefix"]).resolve() != venv.resolve()
                )
                if not target["valid"]:
                    errors.append(
                        "Interpreter identity does not match the selected isolated venv"
                    )
                if not data["pip"]:
                    warnings.append(
                        "Target pip is missing; repair uses bundled ensurepip"
                    )
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                errors.append(redact(str(exc)))
    else:
        warnings.append(
            "Target venv does not exist; creation requires a compatible normal CPython"
        )
    target_python = target["python"] or "unknown"
    unsupported = ""
    if backend == "directml" and (
        target["platform"] != "win32" or not re.match(r"3\.1[0-2]\.", target_python)
    ):
        unsupported = "DirectML requires Windows and an explicitly selected CPython 3.10–3.12; no automatic downgrade"
    elif backend == "cuda12" and (
        target["platform"] not in ("linux", "win32")
        or target["machine"].lower() not in ("amd64", "x86_64")
    ):
        unsupported = "This CUDA12 wheel profile requires Windows/Linux x86-64; driver/hardware validation remains manual"
    elif backend in ("rocm", "xpu"):
        unsupported = "ROCm/XPU vendor-specific builds are manual-only in this version"
    if unsupported:
        errors.append(unsupported)
    inventory = data.get("inventory", {})
    checks = data.get("checks", {})
    for row in rows:
        version = inventory.get(row["name"])
        row["installed_version"] = version
        row["dependency_issues"] = data.get("dependency_issues", {}).get(
            row["name"], []
        )
        row["status"] = (
            "unsupported"
            if unsupported and row["group"] == "gpu"
            else "missing"
            if not version
            else "satisfied"
            if checks.get(row["name"], _satisfies(version, row["specifier"]))
            and not row["dependency_issues"]
            else "conflict"
        )
    if backend != "cpu":
        warnings.append(
            "Installed packages do not prove GPU device, driver or numerical backend availability"
        )
    return {
        "schema_version": 1,
        "operation": "inspect",
        "status": "failed" if errors else "success",
        "root": str(root),
        "target": target,
        "groups": groups,
        "profile": profile,
        "backend": backend,
        "packages": rows,
        "warnings": warnings,
        "errors": errors,
    }


def plan_environment(
    root,
    venv=None,
    groups=("desktop",),
    backend="cpu",
    mirror="pypi",
    action="repair",
    python=None,
    proxy=None,
    profile="runtime",
):
    if action not in ("install", "repair") or mirror not in MIRRORS:
        raise ValueError("Unknown action or registry mirror")
    proxy = proxy or ProxyConfig()
    proxy.validate()
    result = inspect_environment(root, venv, groups, backend, profile)
    result.update(
        operation="plan",
        action=action,
        mirror=mirror,
        proxy=proxy.public(),
        actions=[],
        requires_confirmation=True,
        bootstrap_python=str(python or ""),
    )
    target = result["target"]
    if not target["exists"] and python:
        # The selected bootstrap establishes the prospective target identity.
        # Never infer a GPU-compatible Python version from this tool's runtime.
        try:
            prospective = probe_python(python)
            if (
                backend == "directml"
                and prospective["platform"] == "win32"
                and re.match(r"3\.1[0-2]\.", prospective["python"])
            ):
                result["errors"] = [
                    error
                    for error in result["errors"]
                    if not error.startswith("DirectML requires")
                ]
                for row in result["packages"]:
                    if row["group"] == "gpu":
                        row["status"] = "missing"
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    if not target["exists"]:
        if not python:
            result["errors"].append(
                "Choose an existing CPython or obtain the managed runtime before creating the target venv"
            )
        else:
            try:
                bootstrap = probe_python(python)
                if not bootstrap["has_venv"] or not bootstrap["has_ensurepip"]:
                    raise ValueError(
                        "Chosen Python lacks venv/ensurepip; frozen/embeddable runtimes cannot bootstrap"
                    )
                if bootstrap.get("bundled_pip") and not _satisfies(
                    bootstrap["bundled_pip"], ">=22.2"
                ):
                    raise ValueError(
                        "Chosen Python bundles pip older than 22.2; choose a maintained CPython with supported wheel dry-run"
                    )
                result["bootstrap_identity"] = {
                    key: bootstrap[key]
                    for key in (
                        "executable",
                        "prefix",
                        "base_prefix",
                        "python",
                        "platform",
                        "machine",
                    )
                }
                result["actions"].append(
                    {
                        "kind": "create_venv",
                        "argv": [str(python), "-I", "-m", "venv", target["venv"]],
                    }
                )
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                result["errors"].append(redact(str(exc)))
    elif target["valid"] and not target["pip"]:
        if target.get("bundled_pip") and not _satisfies(
            target["bundled_pip"], ">=22.2"
        ):
            result["errors"].append(
                "Bundled ensurepip is older than 22.2; use a maintained Python or explicitly update pip before retry"
            )
        if action == "repair":
            result["actions"].append(
                {
                    "kind": "ensurepip",
                    "argv": [
                        target["interpreter"],
                        "-I",
                        "-m",
                        "ensurepip",
                        "--upgrade",
                    ],
                }
            )
        else:
            result["errors"].append("Target pip is missing; select repair")
    selected = [
        row["requirement"]
        for row in result["packages"]
        if row["status"] == "missing"
        or (action == "repair" and row["status"] == "conflict")
    ]
    if action == "install" and any(
        row["status"] == "conflict" for row in result["packages"]
    ):
        result["warnings"].append(
            "Install preserves version conflicts; use repair to reconcile selected requirements"
        )
    if target["valid"] and target["pip"] and not _satisfies(target["pip"], ">=22.2"):
        result["errors"].append(
            "Target pip is older than 22.2 and lacks wheel dry-run; explicitly update pip with its official installer before retry"
        )
    if selected:
        result["actions"].append(
            {
                "kind": "resolve_wheels",
                "argv": [
                    target["interpreter"],
                    "-I",
                    "-m",
                    "pip",
                    "--isolated",
                    "install",
                    "--dry-run",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--only-binary=:all:",
                    "--index-url",
                    MIRRORS[mirror]["url"],
                    *selected,
                ],
            }
        )
        result["actions"].append(
            {
                "kind": "pip_install",
                "argv": [
                    target["interpreter"],
                    "-I",
                    "-m",
                    "pip",
                    "--isolated",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--only-binary=:all:",
                    "--index-url",
                    MIRRORS[mirror]["url"],
                    *selected,
                ],
            }
        )
    result["status"] = "blocked" if result["errors"] else "ready"
    result["warnings"].append(
        "Resolver may replace selected packages and their dependencies; no explicit unrelated-package removal"
    )
    result["plan_id"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()
    return result


def run_command(argv, emit, cancel_event, proxy=None, timeout=900):
    """Process-group cancellation with streamed redacted output; shell is never used."""
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise ValueError("Only argv lists are accepted")
    if cancel_event.is_set():
        return 130
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=clean_environment(proxy),
        start_new_session=os.name != "nt",
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if os.name == "nt"
        else 0,
    )
    logs: queue.Queue[str] = queue.Queue()

    def reader():
        assert process.stdout is not None
        for line in process.stdout:
            logs.put(line)
        process.stdout.close()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    start = time.monotonic()
    stopped = False
    while process.poll() is None or not logs.empty():
        try:
            line = logs.get(timeout=0.05)
            emit({"event": "log", "text": redact(line.rstrip(), proxy)})
        except queue.Empty:
            pass
        if (
            cancel_event.is_set() or time.monotonic() - start > timeout
        ) and not stopped:
            stopped = True
            if os.name == "nt":
                # Absolute system tool avoids PATH shadowing; failure still stops
                # the main process. Descendants may need manual inspection.
                try:
                    taskkill = str(
                        Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"
                    )
                    killed = subprocess.run(
                        [taskkill, "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                    )
                    if killed.returncode:
                        emit(
                            {
                                "event": "log",
                                "text": "Process-tree termination could not be confirmed; descendants may still be running.",
                            }
                        )
                except (OSError, KeyError, subprocess.SubprocessError):
                    emit(
                        {
                            "event": "log",
                            "text": "Process-tree termination failed; descendants may still be running.",
                        }
                    )
                if process.poll() is None:
                    process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    emit(
                        {
                            "event": "log",
                            "text": "Process stop remains unconfirmed; inspect the process tree before retry.",
                        }
                    )
                    break
            emit(
                {
                    "event": "log",
                    "text": "Cancelled/timed out. Changes may be partial; reinspect before retry.",
                }
            )
    thread.join(timeout=1)
    while not logs.empty():
        emit({"event": "log", "text": redact(logs.get_nowait().rstrip(), proxy)})
    return 130 if stopped else process.wait()


def _verify_mutation_target(plan, *, allow_missing=False):
    target = plan["target"]
    directory = Path(target["venv"])
    if directory.resolve() != directory or directory.is_symlink():
        raise ValueError("Target/ancestor became a symlink; refusing mutation")
    parent = target.get("parent_identity")
    if not parent:
        raise ValueError("Target parent identity is unavailable")
    stat = directory.parent.stat()
    if (
        str(directory.parent) != parent["path"]
        or stat.st_dev != parent["device"]
        or stat.st_ino != parent["inode"]
    ):
        raise ValueError("Target parent identity changed; replan before mutation")
    if allow_missing and not directory.exists():
        return
    inspected = inspect_environment(
        plan["root"],
        directory,
        plan["groups"],
        plan["backend"],
        plan.get("profile", "runtime"),
    )
    if not inspected["target"]["valid"]:
        raise ValueError("Target is not the expected isolated venv; refusing mutation")
    old = target
    new = inspected["target"]
    bootstrap = plan.get("bootstrap_identity")
    if (
        not old["exists"]
        and bootstrap
        and any(
            bootstrap[key] != new[key]
            for key in ("base_prefix", "python", "platform", "machine")
        )
    ):
        raise ValueError(
            "Created venv identity differs from the explicitly selected bootstrap; refusing pip mutation"
        )
    if old["valid"] and any(
        old[key] != new[key]
        for key in ("prefix", "base_prefix", "python", "platform", "machine")
    ):
        raise ValueError("Target interpreter identity changed; refusing mutation")


def execute_plan(plan, confirmed=False, emit=None, cancel_event=None, proxy=None):
    emit = emit or (lambda event: None)
    cancel_event = cancel_event or threading.Event()
    proxy = proxy or ProxyConfig()
    result = {
        "schema_version": 1,
        "operation": plan.get("action", "repair"),
        "status": "blocked",
        "plan_id": plan.get("plan_id", ""),
        "target": plan.get("target", {}),
        "groups": plan.get("groups", []),
        "profile": plan.get("profile", "runtime"),
        "backend": plan.get("backend", "cpu"),
        "packages": plan.get("packages", []),
        "warnings": [],
        "errors": [],
    }
    if not confirmed:
        result["errors"].append("Explicit execution confirmation is required")
        return result
    current = plan_environment(
        plan["root"],
        plan["target"]["venv"],
        plan["groups"],
        plan["backend"],
        plan["mirror"],
        plan["action"],
        plan.get("bootstrap_python") or None,
        proxy,
        plan.get("profile", "runtime"),
    )
    # Compare the complete object, not just a caller-supplied hash.
    if plan != current or current["status"] != "ready":
        result["errors"].append(
            "Plan is stale, altered or blocked; inspect and confirm a new plan"
        )
        return result
    lock = Path(plan["target"]["venv"]).parent / (
        Path(plan["target"]["venv"]).name + ".dependency-manager.lock"
    )
    if not lock.parent.is_dir():
        result["errors"].append("Target parent directory must exist")
        return result
    descriptor = None
    try:
        _verify_mutation_target(plan, allow_missing=True)
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        descriptor = -1
        current = plan_environment(
            plan["root"],
            plan["target"]["venv"],
            plan["groups"],
            plan["backend"],
            plan["mirror"],
            plan["action"],
            plan.get("bootstrap_python") or None,
            proxy,
            plan.get("profile", "runtime"),
        )
        if plan != current:
            raise ValueError(
                "Environment changed while acquiring the lock; inspect and confirm a fresh plan"
            )
        emit({"event": "started", "plan_id": plan["plan_id"]})
        for action in plan["actions"]:
            _verify_mutation_target(plan, allow_missing=action["kind"] == "create_venv")
            if (
                action["kind"] == "create_venv"
                and Path(plan["target"]["venv"]).exists()
            ):
                raise ValueError(
                    "Target appeared after confirmation; refusing venv overwrite"
                )
            emit({"event": "phase", "phase": action["kind"]})
            code = run_command(action["argv"], emit, cancel_event, proxy)
            if code:
                result["status"] = (
                    "cancelled" if cancel_event.is_set() or code == 130 else "failed"
                )
                result["errors"].append(f"{action['kind']} returned {code}")
                result["warnings"].append(
                    "Environment may be partially changed; reinspect and confirm a fresh retry plan"
                )
                return result
            if action["kind"] == "create_venv":
                _verify_mutation_target(plan)
        inspected = inspect_environment(
            plan["root"],
            plan["target"]["venv"],
            plan["groups"],
            plan["backend"],
            plan.get("profile", "runtime"),
        )
        result["inspection"] = inspected
        result["packages"] = inspected["packages"]
        remaining = [
            row for row in inspected["packages"] if row["status"] != "satisfied"
        ]
        result["status"] = (
            "success" if not inspected["errors"] and not remaining else "failed"
        )
        if remaining:
            result["errors"].append(
                "Post-install inspection still has missing/conflicting requirements"
            )
        result["errors"].extend(inspected["errors"])
        return result
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["errors"].append(redact(str(exc), proxy))
        result["status"] = "failed"
        return result
    finally:
        if descriptor is not None:
            if descriptor != -1:
                os.close(descriptor)
            lock.unlink(missing_ok=True)
