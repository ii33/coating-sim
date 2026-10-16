"""Hash-pinned, private MinGit deployment. Never changes PATH or registry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPSHandler, HTTPRedirectHandler, Request
import zipfile

from .runtime_paths import project_root, contained_runtime_path
from .manager import clean_environment
from .mirrors import ProxyConfig, redact, _UniformProxyHandler

MANIFEST_PATH = (
    Path(sys.prefix) / "git_runtime_manifest.json"
    if getattr(sys, "frozen", False)
    else Path(__file__).with_name("git_runtime_manifest.json")
)
MARKER = ".deposition-managed-git.json"
MAX_DOWNLOAD = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024


def git_runtime_manifest():
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if data["schema_version"] != 1:
        raise ValueError("Unknown Git runtime manifest schema")
    for artifact in data["artifacts"].values():
        p = urlsplit(artifact["url"])
        if (
            p.scheme != "https"
            or p.netloc != "github.com"
            or p.query
            or p.fragment
            or not p.path.startswith("/git-for-windows/git/releases/download/")
        ):
            raise ValueError("Git must come from the fixed official HTTPS source")
        if not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]):
            raise ValueError("Invalid pinned Git SHA-256")
    return data


def default_git_directory(project=None):
    return project_root(project) / ".runtime" / "git"


def _unlinked(path):
    """Reject symbolic links, Windows junctions and reparse-point ancestors."""
    for entry in (path, *path.parents):
        if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
            return False
        if entry.exists() and getattr(entry.lstat(), "st_file_attributes", 0) & 0x400:
            return False
    return path.absolute() == path.resolve()


def find_managed_git(directory=None, *, project=None):
    directory = Path(directory or default_git_directory(project)).absolute()
    executable = directory / "cmd/git.exe"
    if not _unlinked(executable) or not executable.is_file():
        return None
    try:
        marker = json.loads((directory / MARKER).read_text(encoding="utf-8"))
        manifest = git_runtime_manifest()
        artifact = manifest["artifacts"][marker["architecture"]]
        if marker["version"] != manifest["version"] or marker["sha256"] != artifact["sha256"]:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return str(executable)


def discover_git(explicit=None, directory=None, *, project=None):
    if explicit:
        path = Path(explicit).absolute()
        if not path.is_file():
            raise ValueError("Explicit Git executable does not exist")
        return str(path)
    return find_managed_git(directory, project=project) or shutil.which("git")


def _identity(path):
    info = path.stat()
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


def _plan_id(plan):
    return hashlib.sha256(
        json.dumps({k: v for k, v in plan.items() if k != "plan_id"}, sort_keys=True).encode()
    ).hexdigest()


def plan_git_runtime(directory=None, proxy=None, *, platform_name=None, machine=None, project=None):
    manifest = git_runtime_manifest()
    proxy = proxy or ProxyConfig()
    proxy.validate()
    selected_project = project_root(project) if project is not None or directory is None else None
    boundary = selected_project if selected_project is not None else Path.home().resolve()
    directory = Path(directory or default_git_directory(selected_project)).absolute()
    machine = (machine or platform.machine()).lower()
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, "unsupported")
    ancestor = directory.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    errors = []
    if (platform_name or sys.platform) != "win32":
        errors.append("Managed Git download is Windows-only; select an existing Git")
    if architecture not in manifest["artifacts"]:
        errors.append("Unsupported Windows architecture")
    if directory.exists() or directory.is_symlink():
        errors.append("Git destination already exists; no overwrite")
    if not contained_runtime_path(directory, boundary):
        errors.append("Git destination must be an unlinked local path inside the selected project (or user home for an explicit legacy path)")
    plan = {
        "schema_version": 1,
        "operation": "git_runtime_plan",
        "status": "blocked" if errors else "ready",
        "directory": str(directory),
        "project": str(selected_project) if selected_project is not None else None,
        "parent_identity": _identity(ancestor) if ancestor.is_dir() else None,
        "platform": platform_name or sys.platform,
        "architecture": architecture,
        "version": manifest["version"],
        "artifact": manifest["artifacts"].get(architecture),
        "source": manifest["source"],
        "license": manifest["license"],
        "signature_evidence": manifest["signature_evidence"],
        "proxy": proxy.public(),
        "requires_confirmation": True,
        "errors": errors,
        "warnings": [
            "Private MinGit only; no administrator, PATH, registry or security-setting changes",
            "Git and its supporting tools are not licensed under the application's MIT license",
        ],
    }
    plan["plan_id"] = _plan_id(plan)
    return plan


def _official_url(url):
    p = urlsplit(url)
    if (
        p.scheme != "https"
        or p.hostname not in ("github.com", "release-assets.githubusercontent.com")
        or p.username
        or p.password
        or p.port not in (None, 443)
        or p.fragment
    ):
        raise ValueError("Git download redirect outside official HTTPS hosts")


class _OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        _official_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download_git(artifact, destination, proxy, cancel_event, emit, *, max_bytes=MAX_DOWNLOAD):
    env = clean_environment(proxy)
    proxies = {
        s: env.get(s.upper() + "_PROXY", env.get(s + "_proxy", "")) for s in ("http", "https")
    }
    opener = build_opener(_UniformProxyHandler(proxies), HTTPSHandler(), _OfficialRedirect())
    digest, total, start = hashlib.sha256(), 0, time.monotonic()
    created = False
    try:
        with opener.open(
            Request(artifact["url"], headers={"User-Agent": "DepositionDependencyManager/1"}),
            timeout=10,
        ) as response:
            _official_url(response.geturl())
            with open(destination, "xb") as output:
                created = True
                while True:
                    if cancel_event.is_set():
                        raise InterruptedError("Git download cancelled")
                    if time.monotonic() - start > 180:
                        raise TimeoutError("Git download exceeded 180 seconds")
                    chunk = getattr(response, "read1", response.read)(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Git download exceeds size bound")
                    digest.update(chunk)
                    output.write(chunk)
                    emit({"event": "progress", "phase": "git_download", "download_bytes": total})
        if digest.hexdigest() != artifact["sha256"]:
            raise ValueError("Git SHA-256 mismatch; archive will not be used")
    except Exception:
        if created:
            Path(destination).unlink(missing_ok=True)
        raise


def _extract_zip(archive, destination, cancel_event, *, max_bytes=MAX_EXPANDED):
    """Preflight every member before writing, including Windows filename aliases."""
    with zipfile.ZipFile(archive) as source:
        members, seen, total = [], set(), 0
        if len(source.infolist()) > 20000:
            raise ValueError("Git archive has too many members")
        for info in source.infolist():
            # ZipInfo.filename is already sanitized (NUL truncation and, on
            # Windows, backslash conversion). Validate the original archive name
            # before accepting any normalized alias or constructing a target.
            name = info.orig_filename
            parts = PurePosixPath(name).parts
            mode = info.external_attr >> 16
            if (
                not parts
                or name != info.filename
                or name.startswith("/")
                or "\\" in name
                or "\x00" in name
                or any(
                    p in (".", "..")
                    or ":" in p
                    or p.endswith((".", " "))
                    or re.match(r"(?i)^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)", p)
                    for p in parts
                )
                or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                or info.flag_bits & 1
                or info.external_attr & 0x400
            ):
                raise ValueError("Unsafe Git archive member")
            key = "/".join(parts).casefold()
            if key in seen or key == MARKER.casefold():
                raise ValueError("Duplicate or reserved Git archive member")
            seen.add(key)
            total += info.file_size
            if total > max_bytes:
                raise ValueError("Git archive exceeds expanded size bound")
            members.append((info, destination.joinpath(*parts)))
        actual = 0
        for info, target in members:
            if cancel_event.is_set():
                raise InterruptedError("Git extraction cancelled")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(info) as src, target.open("xb") as dst:
                while True:
                    if cancel_event.is_set():
                        raise InterruptedError("Git extraction cancelled")
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    actual += len(chunk)
                    if actual > max_bytes:
                        raise ValueError("Git archive exceeds expanded size bound")
                    dst.write(chunk)


def _verify_target(plan):
    target = Path(plan["directory"])
    if target.exists() or not contained_runtime_path(target, plan.get("project") or Path.home().resolve()):
        raise ValueError("Git target changed or already exists; refusing overwrite")
    identity = plan["parent_identity"]
    if not identity or _identity(Path(identity["path"])) != identity:
        raise ValueError("Git parent identity changed")


def execute_git_runtime(plan, confirmed=False, emit=None, cancel_event=None, proxy=None):
    emit, cancel_event, proxy = (
        emit or (lambda event: None),
        cancel_event or threading.Event(),
        proxy or ProxyConfig(),
    )
    result = {"schema_version": 1, "operation": "git_runtime_install", "status": "failed", "errors": [], "warnings": [], "plan_id": plan.get("plan_id")}
    try:
        if not confirmed:
            raise ValueError("Git runtime plan must be explicitly confirmed")
        proxy.validate()
        manifest = git_runtime_manifest()
        if (
            plan.get("plan_id") != _plan_id(plan)
            or plan.get("status") != "ready"
            or plan.get("operation") != "git_runtime_plan"
            or plan.get("platform") != "win32"
            or plan.get("artifact") != manifest["artifacts"].get(plan.get("architecture"))
            or plan.get("version") != manifest["version"]
            or plan.get("proxy") != proxy.public()
        ):
            raise ValueError("Git plan changed; generate and confirm a new plan")
        _verify_target(plan)
        if cancel_event.is_set():
            raise InterruptedError("Git installation cancelled")
        target = Path(plan["directory"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".mingit-stage-", dir=target.parent) as temporary:
            stage = Path(temporary)
            archive, unpacked = stage / "git.zip", stage / "unpacked"
            unpacked.mkdir()
            download_git(plan["artifact"], archive, proxy, cancel_event, emit)
            _extract_zip(archive, unpacked, cancel_event)
            if not (unpacked / "cmd/git.exe").is_file():
                raise ValueError("Git archive is missing cmd/git.exe")
            (unpacked / MARKER).write_text(
                json.dumps(
                    {
                        "version": plan["version"],
                        "architecture": plan["architecture"],
                        "sha256": plan["artifact"]["sha256"],
                    }
                ),
                encoding="utf-8",
            )
            _verify_target(plan)
            if cancel_event.is_set():
                raise InterruptedError("Git installation cancelled")
            # Windows directory rename is atomic and fails if destination exists.
            unpacked.rename(target)
        result.update(status="completed", git=str(target / "cmd/git.exe"), directory=str(target))
    except InterruptedError as exc:
        result.update(status="cancelled", errors=[redact(str(exc), proxy)])
    except Exception as exc:
        result["errors"] = [redact(str(exc), proxy)]
    return result
