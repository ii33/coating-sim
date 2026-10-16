"""Bounded, read-only discovery of project-creation interpreters."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .manager import clean_environment, probe_python, _satisfies
from .mirrors import redact

MAX_CANDIDATES = 12
PROBE_TIMEOUT = 2


def _candidate_paths(project=None, cancel_event=None):
    # Prefer the normal interpreter underlying a manager launched from a venv.
    if not getattr(sys, "frozen", False):
        yield getattr(sys, "_base_executable", sys.executable), "manager_base"
    from .runtime import default_runtime_directory, _registered_python_paths
    try:
        directory = default_runtime_directory(project=project)
    except ValueError:
        # Frozen tools need explicit project context; discovery can still inspect
        # known installed interpreters without choosing an unrelated storage root.
        directory = None
    if directory is not None:
        yield str(directory / "python.exe"), "managed_runtime"
    for name in ("python3", "python", "python3.12", "python3.11", "python3.10", "python3.13"):
        path = shutil.which(name)
        if path:
            yield path, "path"
    if cancel_event is not None and cancel_event.is_set():
        return
    if sys.platform == "win32":
        launcher = shutil.which("py")
        if launcher:
            try:
                result = subprocess.run([launcher, "-0p"], capture_output=True, text=True,
                                        timeout=PROBE_TIMEOUT, env=clean_environment())
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        match = re.search(r"([A-Za-z]:[\\/].*?\.exe)\s*$", line)
                        if match:
                            yield match[1], "windows_launcher"
            except (OSError, subprocess.SubprocessError):
                pass
        for minor in ("3.12", "3.11", "3.10", "3.13"):
            if cancel_event is not None and cancel_event.is_set():
                return
            try:
                for path in _registered_python_paths(minor):
                    if path:
                        yield str(Path(path) / "python.exe"), "windows_registry"
            except OSError:
                continue


def _incompatibility(identity, backend):
    if not re.match(r"3\.1[0-3]\.", identity.get("python", "")):
        return "A normal CPython 3.10–3.13 interpreter is required"
    if not identity.get("has_venv") or not identity.get("has_ensurepip"):
        return "Interpreter lacks venv/ensurepip"
    if identity.get("bundled_pip") and not _satisfies(identity["bundled_pip"], ">=22.2"):
        return "Bundled pip is too old for wheel dry-run"
    if identity.get("prefix") != identity.get("base_prefix"):
        return "Select a normal base CPython, not an existing project venv"
    if backend == "directml" and (identity.get("platform") != "win32" or not re.match(r"3\.1[0-2]\.", identity.get("python", ""))):
        return "DirectML requires Windows CPython 3.10–3.12"
    if backend == "cuda12" and (identity.get("platform") not in ("win32", "linux") or identity.get("machine", "").lower() not in ("amd64", "x86_64")):
        return "CUDA12 profile requires Windows/Linux x86-64"
    if backend in ("rocm", "xpu"):
        return "This backend requires an explicitly managed vendor environment"
    return ""


def discover_python(backend="cpu", explicit=None, *, project=None, cancel_event=None):
    """Never mutate or install; a supplied explicit choice prohibits fallback.

    Call from a GUI worker: at most 12 interpreter probes (2 seconds each), plus
    a 2-second Windows launcher listing. Paths are deduplicated before probing.
    """
    if backend not in ("cpu", "cuda12", "directml", "rocm", "xpu"):
        raise ValueError("Unknown backend")
    candidates, seen = [], set()
    def cancelled():
        return cancel_event is not None and cancel_event.is_set()

    if cancelled():
        return {"status": "cancelled", "selected_python": None, "candidates": [], "errors": []}
    paths = [(str(explicit), "explicit")] if explicit else _candidate_paths(project, cancel_event)
    for path, source in paths:
        if cancelled():
            break
        try:
            key = os.path.normcase(str(Path(path).resolve()))
        except (OSError, RuntimeError):
            continue
        if "windowsapps" in {part.lower() for part in re.split(r"[\\/]", str(path))}:
            continue
        if key in seen:
            continue
        seen.add(key)
        if source != "explicit" and (not Path(path).is_file() or (os.name != "nt" and not os.access(path, os.X_OK))):
            continue
        row = {"path": str(path), "source": source, "compatible": False, "reason": "", "identity": None}
        try:
            identity = probe_python(path, timeout=PROBE_TIMEOUT, allow_unsupported=True)
            row["identity"] = {key: identity.get(key) for key in ("executable", "implementation", "python", "platform", "machine", "prefix", "base_prefix")}
            row["reason"] = _incompatibility(identity, backend)
            row["compatible"] = not row["reason"]
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            row["reason"] = redact(str(exc))
        candidates.append(row)
        if len(candidates) >= MAX_CANDIDATES:
            break
    if cancelled():
        return {"status": "cancelled", "selected_python": None, "candidates": candidates, "errors": []}
    selected = next((row["path"] for row in candidates if row["compatible"]), None)
    return {"status": "ready" if selected else "missing", "selected_python": selected,
            "candidates": candidates, "errors": [] if selected else ["No compatible creation Python found; explicitly obtain a supported runtime or select its interpreter."]}
