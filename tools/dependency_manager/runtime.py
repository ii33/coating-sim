"""Explicit, hash-pinned CPython bootstrap, separate from the tool's runtime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import (
    build_opener,
    HTTPSHandler,
    HTTPRedirectHandler,
    Request,
)
import sys

from .manager import clean_environment, probe_python, run_command
from .mirrors import ProxyConfig, redact, _UniformProxyHandler

MANIFEST_PATH = (
    Path(sys.prefix) / "runtime_manifest.json"
    if getattr(sys, "frozen", False)
    else Path(__file__).with_name("runtime_manifest.json")
)


def runtime_manifest():
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if data["schema_version"] != 1:
        raise ValueError("Unknown runtime manifest schema")
    for artifact in data["artifacts"].values():
        parsed = urlsplit(artifact["url"])
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.python.org"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Runtime must come from the fixed official HTTPS source")
        if not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]):
            raise ValueError("Invalid pinned runtime SHA-256")
    return data


def _architecture(machine):
    machine = machine.lower()
    return {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "i386": "x86",
        "i686": "x86",
        "x86": "x86",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine, "unsupported")


def default_runtime_directory():
    base = (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        if os.name == "nt"
        else Path.home() / ".local/share"
    )
    return (
        base
        / "DepositionDependencyManager"
        / ("cpython-" + runtime_manifest()["version"])
    )


def _registered_python_paths(version):
    if sys.platform != "win32":
        return []
    import winreg

    found = []
    key_name = (
        "Software\\Python\\PythonCore\\"
        + ".".join(version.split(".")[:2])
        + "\\InstallPath"
    )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                    found.append(str(winreg.QueryValueEx(key, "")[0]))
            except FileNotFoundError:
                pass
    return sorted(set(found))


def plan_runtime(
    directory=None, backend="cpu", proxy=None, *, platform_name=None, machine=None
):
    manifest = runtime_manifest()
    platform_name = platform_name or sys.platform
    machine = _architecture(machine or platform.machine())
    directory = Path(directory or default_runtime_directory()).absolute()
    proxy = proxy or ProxyConfig()
    proxy.validate()
    canonical = directory.resolve()
    ancestor = directory.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    parent_stat = ancestor.stat() if ancestor.is_dir() else None
    parent_identity = (
        {
            "path": str(ancestor.resolve()),
            "device": parent_stat.st_dev,
            "inode": parent_stat.st_ino,
        }
        if parent_stat
        else None
    )
    errors, warnings = (
        [],
        [
            "This is a separate normal CPython for creating project venvs; the bundled tool runtime is never used with -m venv",
            "Windows full installer creates per-user maintenance/uninstall records; no PATH, launcher, associations or shortcuts",
            "Project tests were verified with Python 3.12; Python 3.13 package/wheel resolution is checked before installation, not assumed",
            "If prerequisites require administrator access, stop and report the prerequisite; this tool never requests elevation",
        ],
    )
    if backend not in ("cpu", "cuda12", "directml", "rocm", "xpu"):
        errors.append("Unknown backend")
    if backend == "cuda12" and machine != "x86_64":
        errors.append("Managed CUDA12 wheel profile requires x86-64")
    registered = (
        _registered_python_paths(manifest["version"])
        if platform_name == "win32"
        else []
    )
    if registered:
        errors.append(
            "A CPython of this minor version is already registered; select its interpreter to avoid installer maintenance of another environment"
        )
    if platform_name != "win32":
        errors.append(
            "Managed runtime download is Windows-only; select an existing CPython on Linux/macOS"
        )
    if machine not in manifest["artifacts"]:
        errors.append("Unsupported Windows architecture")
    if backend == "directml":
        errors.append(
            "Managed CPython 3.13 does not support DirectML's Python 3.10–3.12 wheel profile; choose an existing compatible Python"
        )
    if backend in ("rocm", "xpu"):
        errors.append("This vendor backend requires a manual environment")
    if directory.exists():
        errors.append(
            "Managed runtime destination already exists; select its python.exe or a new empty path; no overwrite"
        )
    if (
        directory != canonical
        or directory.is_symlink()
        or not canonical.is_relative_to(Path.home().resolve())
    ):
        errors.append(
            "Managed runtime destination must be a new path inside the current user's home directory"
        )
    artifact = manifest["artifacts"].get(machine)
    plan = {
        "schema_version": 1,
        "operation": "runtime_plan",
        "status": "blocked" if errors else "ready",
        "directory": str(directory),
        "parent_identity": parent_identity,
        "platform": platform_name,
        "architecture": machine,
        "version": manifest["version"],
        "artifact": artifact,
        "source": manifest["source"],
        "license": manifest["license"],
        "proxy": proxy.public(),
        "backend": backend,
        "requires_confirmation": True,
        "warnings": warnings,
        "errors": errors,
    }
    plan["plan_id"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True).encode()
    ).hexdigest()
    return plan


class _OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.python.org"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Runtime redirect is outside the official HTTPS source")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download_runtime(
    artifact,
    destination,
    proxy,
    cancel_event,
    emit,
    *,
    timeout=10,
    max_bytes=128 * 1024 * 1024,
):
    """Bounded binary download with mandatory hash verification before execution."""
    env = clean_environment(proxy)
    proxies = {
        scheme: env.get(scheme.upper() + "_PROXY", env.get(scheme + "_proxy", ""))
        for scheme in ("http", "https")
    }
    opener = build_opener(
        _UniformProxyHandler(proxies), HTTPSHandler(), _OfficialRedirect()
    )
    digest = hashlib.sha256()
    total = 0
    start = time.monotonic()
    try:
        with (
            opener.open(
                Request(
                    artifact["url"],
                    headers={"User-Agent": "DepositionDependencyManager/1"},
                ),
                timeout=timeout,
            ) as response,
            open(destination, "xb") as output,
        ):
            while True:
                if cancel_event.is_set():
                    raise InterruptedError("Runtime download cancelled")
                if time.monotonic() - start > 180:
                    raise TimeoutError("Runtime download exceeded the 180-second bound")
                chunk = getattr(response, "read1", response.read)(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("Runtime download exceeds size bound")
                digest.update(chunk)
                output.write(chunk)
                emit(
                    {
                        "event": "progress",
                        "phase": "runtime_download",
                        "download_bytes": total,
                    }
                )
        if digest.hexdigest() != artifact["sha256"]:
            raise ValueError(
                "Runtime SHA-256 mismatch; the downloaded file will not be executed"
            )
        return total
    except Exception:
        Path(destination).unlink(missing_ok=True)
        raise


def installer_arguments(installer, directory):
    # All are documented CPython full-installer options, not shell strings.
    return [
        str(installer),
        "/quiet",
        "InstallAllUsers=0",
        f"TargetDir={directory}",
        "PrependPath=0",
        "AppendPath=0",
        "Include_launcher=0",
        "InstallLauncherAllUsers=0",
        "AssociateFiles=0",
        "Shortcuts=0",
        "Include_pip=1",
        "Include_exe=1",
        "Include_lib=1",
        "Include_dev=1",
        "Include_tcltk=0",
        "Include_doc=0",
        "Include_test=0",
        "Include_tools=0",
    ]


def _verify_runtime_destination(plan):
    directory = Path(plan["directory"])
    if (
        directory != directory.resolve()
        or not directory.resolve().is_relative_to(Path.home().resolve())
        or directory.exists()
    ):
        raise ValueError(
            "Runtime target/ancestor changed or target appeared; refusing overwrite"
        )
    parent = plan.get("parent_identity")
    if not parent:
        raise ValueError("Managed runtime parent identity is unavailable")
    ancestor = Path(parent["path"])
    stat = ancestor.stat()
    if (
        ancestor != ancestor.resolve()
        or stat.st_dev != parent["device"]
        or stat.st_ino != parent["inode"]
    ):
        raise ValueError("Managed runtime parent identity changed")


def _windows_file_version(path):
    """Read fixed PE version metadata without executing installer components."""
    import ctypes

    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise AttributeError("Windows library loader unavailable")
    library = loader(str(Path(os.environ["SystemRoot"]) / "System32/version.dll"))
    size_api = library.GetFileVersionInfoSizeW
    size_api.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    size_api.restype = ctypes.c_uint32
    read_api = library.GetFileVersionInfoW
    read_api.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    read_api.restype = ctypes.c_int
    query_api = library.VerQueryValueW
    query_api.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                         ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)]
    query_api.restype = ctypes.c_int
    size = size_api(str(path), None)
    if not size:
        raise OSError("UCRT version metadata is unavailable")
    buffer = ctypes.create_string_buffer(size)
    if not read_api(str(path), 0, size, buffer):
        raise OSError("UCRT version metadata could not be read")
    address, length = ctypes.c_void_p(), ctypes.c_uint32()
    if not query_api(buffer, "\\", ctypes.byref(address), ctypes.byref(length)) or not address.value or length.value < 52:
        raise OSError("UCRT fixed version metadata is unavailable")
    fixed = ctypes.cast(address, ctypes.POINTER(ctypes.c_uint32))
    if fixed[0] != 0xFEEF04BD:
        raise OSError("UCRT fixed version metadata is invalid")
    return (fixed[2] >> 16, fixed[2] & 0xFFFF, fixed[3] >> 16, fixed[3] & 0xFFFF)


def _verify_system_ucrt():
    """Conservative OS readiness gate; VC DLLs are installed application-locally."""
    import ctypes

    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise AttributeError("Windows library loader unavailable")
    path = Path(os.environ["SystemRoot"]) / "System32/ucrtbase.dll"
    loader(str(path))
    version = _windows_file_version(path)
    # Match the official installer's CRTInstalled condition, preserving the gate
    # that avoids its optional UCRT deployment. See the bootstrap documentation.
    if version[:2] != (10, 0) or version[2:] < (10137, 0):
        raise OSError("Windows UCRT is older than the installer readiness threshold")


def execute_runtime(plan, confirmed=False, emit=None, cancel_event=None, proxy=None):
    emit = emit or (lambda event: None)
    cancel_event = cancel_event or threading.Event()
    proxy = proxy or ProxyConfig()
    result = {
        "schema_version": 1,
        "operation": "runtime_install",
        "status": "blocked",
        "errors": [],
        "warnings": [],
        "directory": plan.get("directory"),
        "plan_id": plan.get("plan_id"),
    }
    if not confirmed:
        result["errors"].append(
            "Runtime download/installer and license require explicit confirmation"
        )
        return result
    current = plan_runtime(plan["directory"], plan["backend"], proxy)
    if current != plan or current["status"] != "ready":
        result["errors"].append(
            "Runtime plan is stale, altered or unsupported; confirm a fresh plan"
        )
        return result
    if cancel_event.is_set():
        result["status"] = "cancelled"
        return result
    # The official installer deploys vcruntime140*.dll beside python.exe, not
    # necessarily into System32. Only system UCRT readiness is an OS prerequisite
    # here; missing/old UCRT stays manual rather than deploying OS components.
    try:
        _verify_system_ucrt()
    except (AttributeError, OSError, KeyError):
        result["errors"].append(
            "Windows UCRT prerequisite is absent, too old or cannot be verified; user setup is required, no elevation attempted"
        )
        return result
    directory = Path(plan["directory"])
    lock = Path(plan["parent_identity"]["path"]) / (
        ".coat-runtime-"
        + hashlib.sha256(str(directory).encode()).hexdigest()[:16]
        + ".lock"
    )
    descriptor = None
    try:
        _verify_runtime_destination(plan)
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        descriptor = -1
        if plan_runtime(plan["directory"], plan["backend"], proxy) != plan:
            raise ValueError("Runtime destination changed while acquiring the lock")
        directory.parent.mkdir(parents=True, exist_ok=True)
        _verify_runtime_destination(plan)
        with tempfile.TemporaryDirectory(prefix="coat-runtime-") as temporary:
            installer = Path(temporary) / "python-installer.exe"
            emit({"event": "phase", "phase": "runtime_download"})
            download_runtime(plan["artifact"], installer, proxy, cancel_event, emit)
            if cancel_event.is_set():
                raise InterruptedError("Runtime download cancelled")
            _verify_runtime_destination(plan)
            emit({"event": "phase", "phase": "runtime_install"})
            code = run_command(
                installer_arguments(installer, directory),
                emit,
                cancel_event,
                proxy,
                timeout=600,
            )
            if code:
                result["status"] = "cancelled" if code == 130 else "failed"
                result["errors"].append(
                    f"CPython installer returned {code}; no elevated retry"
                )
                result["warnings"].append(
                    "Runtime may be partially installed; inspect the directory before retry"
                )
                return result
            python = directory / "python.exe"
            identity = probe_python(python)
            if (
                identity["python"] != plan["version"]
                or not identity["has_venv"]
                or not identity["has_ensurepip"]
                or Path(identity["prefix"]).resolve() != directory.resolve()
            ):
                raise ValueError(
                    "Installed runtime does not match pinned identity/venv/ensurepip requirements"
                )
            result.update(
                status="success",
                python=str(python),
                identity={
                    key: identity[key] for key in ("python", "prefix", "executable")
                },
            )
            result["warnings"].append("CPython license accepted: " + plan["license"])
            return result
    except (OSError, ValueError, TimeoutError) as exc:
        result["status"] = "cancelled" if cancel_event.is_set() else "failed"
        result["errors"].append(redact(str(exc), proxy))
        return result
    finally:
        if descriptor is not None:
            if descriptor != -1:
                os.close(descriptor)
            lock.unlink(missing_ok=True)
