"""Explicit, reviewable launch entries for an already deployed project.

No startup registration, application launch, downloads, or environment fallback.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from . import manager

ICON_RELATIVE = "coat_sim/ui/assets/icons/launcher.ico"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _identity(path):
    stat = path.stat()
    return {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino,
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _known_folder(shortcut):
    # Per-user, redirected desktop/programs locations; never common/system folders.
    buffer = ctypes.create_unicode_buffer(32768)
    loader = getattr(ctypes, "windll", None)
    if loader is None:
        raise OSError("Windows shell folders are unavailable on this platform")
    code = loader.shell32.SHGetFolderPathW(None, 0 if shortcut == "desktop" else 2,
                                               None, 0, buffer)
    if code:
        raise OSError("Cannot resolve the selected Windows user folder")
    return Path(buffer.value)


def _script(windows):
    if windows:
        return ('@echo off\r\nsetlocal DisableDelayedExpansion\r\n'
                'pushd "%~dp0" || exit /b 2\r\n'
                'if not exist ".venv\\pyvenv.cfg" goto missing\r\n'
                'if not exist ".venv\\Scripts\\python.exe" goto missing\r\n'
                '".venv\\Scripts\\python.exe" -E "launcher.py" %*\r\n'
                'set "COAT_EXIT=%ERRORLEVEL%"\r\npopd\r\nexit /b %COAT_EXIT%\r\n'
                ':missing\r\necho Project .venv is missing. Repair it with the dependency manager. 1>&2\r\n'
                'popd\r\nexit /b 2\r\n')
    return ('#!/bin/sh\n'
            'ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2\n'
            'cd -- "$ROOT" || exit 2\n'
            'if [ ! -f "$ROOT/.venv/pyvenv.cfg" ] || [ ! -x "$ROOT/.venv/bin/python" ]; then\n'
            '  echo "Project .venv is missing. Repair it with the dependency manager." >&2\n'
            '  exit 2\nfi\n'
            'exec "$ROOT/.venv/bin/python" -E "$ROOT/launcher.py" "$@"\n')


def plan_launch_entries(root, *, shortcut="none", output_dir=None, name="Coating Simulator"):
    """Plan a root-relative script and optional Windows .lnk; never overwrite.

    shortcut: none, desktop, start-menu, or custom (requires existing output_dir).
    A conflicting name is blocked: choose a new name or explicitly manage the old
    file outside this operation. Even entries made by this tool are preserved.
    """
    plan = {"schema_version": 1, "operation": "launch_entries", "status": "blocked",
            "root": str(Path(root).absolute()), "shortcut": shortcut,
            "output_dir": str(Path(output_dir).absolute()) if output_dir else None,
            "name": name, "files": [], "errors": [], "warnings": [],
            "requires_confirmation": True}
    try:
        root = Path(root).absolute()
        if root.resolve() != root or not root.is_dir():
            raise ValueError("Select an existing canonical project directory without symlink parents")
        if (not isinstance(name, str) or not name or len(name) > 100
                or re.search(r'[<>:"/\\|?*\x00-\x1f%]', name) or name.endswith((" ", "."))
                or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}):
            raise ValueError("Use a plain non-reserved launch entry name (no path separators or percent signs)")
        if shortcut not in ("none", "desktop", "start-menu", "custom"):
            raise ValueError("Unknown shortcut destination")
        if output_dir and shortcut != "custom":
            raise ValueError("output_dir is only accepted for a custom shortcut destination")
        windows = sys.platform == "win32"
        if sys.platform not in ("win32", "linux"):
            raise ValueError("Launch entry creation supports Windows and Linux scripts only")
        if shortcut != "none" and not windows:
            raise ValueError("Native shortcut creation is Windows-only; select script only")
        venv = root / ".venv"
        config = venv / "pyvenv.cfg"
        python = venv / ("Scripts/python.exe" if windows else "bin/python")
        entry = root / "launcher.py"
        icon = root / ICON_RELATIVE
        if venv.is_symlink() or not config.is_file() or config.is_symlink():
            raise ValueError("The deployment must have its own isolated .venv with pyvenv.cfg")
        if not re.search(r"(?im)^include-system-site-packages\s*=\s*false\s*$", config.read_text(encoding="utf-8")):
            raise ValueError("System site packages must be explicitly disabled")
        for path in (python, entry, icon):
            if not path.is_file():
                raise ValueError("Missing deployment file: " + str(path))
        for path in (entry, icon):
            if path.resolve() != path:
                raise ValueError("Deployment entry and icon must not be symlinked")
        probe = manager.probe_python(python)
        if Path(probe["prefix"]).resolve() != venv or Path(probe["base_prefix"]).resolve() == venv:
            raise ValueError("Interpreter does not belong to the deployment .venv")
        plan["inputs"] = [_identity(path) for path in (root, venv, config, python, entry, icon)]
        plan["icon"] = str(icon)
        plan["icon_sha256"] = hashlib.sha256(icon.read_bytes()).hexdigest()
        plan["python"] = str(python)
        plan["script"] = _script(windows)
        script = root / (name + (".cmd" if windows else ".sh"))
        plan["files"].append({"path": str(script), "kind": "script"})
        if shortcut != "none":
            if shortcut == "custom":
                if not output_dir:
                    raise ValueError("Select an explicit custom output directory")
                folder = Path(output_dir).absolute()
            else:
                folder = _known_folder(shortcut)
            if not folder.is_dir() or folder.resolve() != folder:
                raise ValueError("Shortcut destination must be an existing canonical directory")
            plan["output_identity"] = _identity(folder)
            plan["files"].append({"path": str(folder / (name + ".lnk")), "kind": "shortcut"})
        for item in plan["files"]:
            if os.path.lexists(item["path"]):
                raise ValueError("Existing launch entry is preserved; choose another name: " + item["path"])
        plan["status"] = "ready"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        plan["errors"].append(str(exc))
    plan["plan_id"] = _digest(plan)
    return plan


_LINK_SCRIPT = '''param([string]$Spec)
$ErrorActionPreference = 'Stop'
$s = Get-Content -LiteralPath $Spec -Raw -Encoding UTF8 | ConvertFrom-Json
$w = New-Object -ComObject WScript.Shell
$l = $w.CreateShortcut($s.path)
$l.TargetPath = $s.python
$l.Arguments = $s.arguments
$l.WorkingDirectory = $s.root
$l.IconLocation = $s.icon + ',0'
$l.Description = 'Coating Simulator'
$l.Save()
if (-not (Test-Path -LiteralPath $s.path -PathType Leaf)) { throw 'Shortcut was not created' }
'''


def _write_shortcut(path, plan):
    """Create real Shell Link with stock Windows PowerShell 5.1, no bypass."""
    system = Path(os.environ["SystemRoot"])
    powershell = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not powershell.is_file():
        raise OSError("Windows PowerShell 5.1 is unavailable; no shortcut was created")
    with tempfile.TemporaryDirectory(prefix="coat-link-") as temporary:
        temp = Path(temporary)
        script, spec = temp / "create.ps1", temp / "spec.json"
        script.write_text(_LINK_SCRIPT, encoding="utf-8-sig")
        spec.write_text(json.dumps({"path": str(path), "python": plan["python"],
                                   "arguments": subprocess.list2cmdline(["-E", str(Path(plan["root"]) / "launcher.py")]),
                                   "root": plan["root"], "icon": plan["icon"]}), encoding="utf-8")
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-File", str(script),
                                 "-Spec", str(spec)], capture_output=True, timeout=30, shell=False)
        if result.returncode or not path.is_file() or path.stat().st_size < 76:
            raise OSError("Windows shortcut creation failed; check Windows script policy/COM availability")


def execute_launch_entries(plan, *, confirmed=False):
    """Revalidate the exact reviewed plan, then exclusively create entries.

    Partial failures list created files; no existing file is replaced or removed.
    """
    result = {"schema_version": 1, "operation": "launch_entries", "status": "blocked",
              "plan_id": plan.get("plan_id"), "files": [], "errors": [], "warnings": []}
    if not confirmed:
        result["errors"].append("Review and confirm the launch entry plan first")
        return result
    fresh = plan_launch_entries(plan.get("root", ""), shortcut=plan.get("shortcut", "none"),
                                output_dir=plan.get("output_dir"), name=plan.get("name", ""))
    if fresh["status"] != "ready" or fresh != plan:
        result["errors"].append("Launch plan changed or is blocked; review a fresh plan")
        return result
    try:
        # Stage the .lnk first so policy/COM failures do not leave a script behind.
        with tempfile.TemporaryDirectory(prefix="coat-launch-") as temp:
            staged = Path(temp) / "entry.lnk"
            if len(plan["files"]) > 1:
                _write_shortcut(staged, plan)
            for item in plan["files"]:
                path = Path(item["path"])
                data = plan["script"].encode("utf-8") if item["kind"] == "script" else staged.read_bytes()
                with path.open("xb") as output:
                    output.write(data)
                result["files"].append(item)
                if item["kind"] == "script" and sys.platform != "win32":
                    path.chmod(0o700)
        result["status"] = "success"
    except (OSError, KeyError, subprocess.SubprocessError) as exc:
        result["status"] = "failed"
        result["errors"].append(str(exc))
        if result["files"]:
            result["warnings"].append("Some entries were created; inspect listed files before retrying")
    return result
