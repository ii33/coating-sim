"""Explicit source-development tools. These gates are independent of persona text."""
from __future__ import annotations

import ast
import base64
from datetime import datetime, timezone
import hashlib
from importlib import metadata
from itertools import islice
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Literal

from coat_sim.core.atomic_io import replace_with_retry

ROOT = Path(__file__).resolve().parents[2]
MAX_PERSONA_BYTES = 32_000
MAX_READ_BYTES = 65_536
MAX_DIRECTORY_ENTRIES = 200
_ACCESS_LOCK = threading.RLock()
_READ_ACCESS_UNTIL = 0.0
_SOURCE_DIRS = {"coat_sim": {".py", ".qml", ".md"},
                "tests": {".py"}, "tools": {".py"}, "docs": {".md"}}
_ROOT_SOURCE_FILES = {"launcher.py", "pyproject.toml", "README.md", "AGENTS.md"}
_SENSITIVE_PARTS = {".git", ".venv", ".aws", ".ssh", ".codex", ".env",
                    "credentials", "credential", "secrets", "secret", "tokens", "keys"}


def _project_read_scope(target: Path) -> tuple[bool, bool]:
    """Return (safe project path, available without an explicit grant)."""
    root = ROOT.resolve()
    if not target.is_relative_to(root):
        return False, False
    parts = target.relative_to(root).parts
    if any(part.lower() in _SENSITIVE_PARTS or part.lower().startswith(".env.")
           for part in parts):
        return False, False
    if not parts:
        return True, True
    if len(parts) == 1 and parts[0] in _ROOT_SOURCE_FILES:
        return True, True
    if parts[0] in _SOURCE_DIRS:
        if len(parts) == 1:
            return True, True
        return True, target.is_dir() or target.suffix.lower() in _SOURCE_DIRS[parts[0]]
    if parts[:2] == ("artifacts", "logs"):
        return True, (len(parts) == 2 or
                      (len(parts) == 3 and re.fullmatch(r"[\w-]+\.log(?:\.[0-9]+)?", parts[2]) is not None))
    if len(parts) == 1 and parts[0] == "artifacts":
        return True, True
    return True, False


def development_available() -> bool:
    # Only source installations can opt in; packaging always refuses.
    return (os.environ.get("COAT_RELEASE_BUILD") != "1"
            and not getattr(sys, "frozen", False)
            and "__compiled__" not in globals())


def development_enabled(_config: dict[str, Any] | None = None) -> bool:
    if not development_available():
        return False
    # launcher.py overwrites this inherited marker on every official startup;
    # persisted settings are intentionally ignored as an authority source.
    return os.environ.get("COAT_DEVELOPMENT") == "1"


def require_development() -> None:
    if not development_enabled():
        raise PermissionError("开发工具未启用；须从源码入口显式使用 --development。发行程序禁止此能力。")


def _fixed_path(relative: str) -> Path:
    path = ROOT / relative
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT) or path.is_symlink() or resolved != path.absolute():
        raise PermissionError("开发工具的固定路径不能通过链接指向项目外。")
    return resolved


def developer_status() -> dict[str, Any]:
    from coat_sim.agent.environment_issues import issue_report_status
    return {"available": development_available(), "enabled": development_enabled(),
            "enabled_by": ("source_launch" if development_enabled() and os.environ.get("COAT_DEVELOPMENT") == "1"
                           else "saved_settings" if development_enabled() else "disabled"),
            "issue_reports": issue_report_status(),
            "checks": ["environment", "syntax", "unit-tests"],
            "persona_editable": development_enabled(),
            "file_access": {"granted": development_enabled() and time.monotonic() < _READ_ACCESS_UNTIL,
                            "project_source_and_logs_default": development_enabled(),
                            "read_only": True, "administrator": False},
            "scope": "Project source and logs are readable by default; a grant permits other safe project files. No arbitrary command execution."}


def developer_access(action: Literal["status", "grant", "revoke"] = "status",
                     duration_s: int = 900) -> dict[str, Any]:
    """Grant/revoke temporary read-only access to other safe files in this project.

    The source-build launch gate is mandatory and cannot be opened by a model.
    Grant only when the user's debugging request authorizes the required read.
    This is an application permission, not OS administrator elevation.
    """
    global _READ_ACCESS_UNTIL
    require_development()
    if action not in ("status", "grant", "revoke"):
        raise ValueError("不支持的开发访问操作。")
    if not isinstance(duration_s, int) or not 1 <= duration_s <= 3600:
        raise ValueError("开发只读授权时长须为 1–3600 秒。")
    with _ACCESS_LOCK:
        if action == "grant":
            _READ_ACCESS_UNTIL = time.monotonic() + duration_s
        elif action == "revoke":
            _READ_ACCESS_UNTIL = 0.0
        remaining = max(0.0, _READ_ACCESS_UNTIL - time.monotonic())
    return {"granted": remaining > 0, "remaining_s": round(remaining, 1),
            "scope": "本项目内非敏感文件与目录", "read_only": True,
            "administrator": False, "sandbox_permissions_changed": False}


def developer_files(path: str, action: Literal["stat", "list", "read"] = "stat",
                    offset: int = 0, limit: int = 32768) -> dict[str, Any]:
    """Read project source/logs by default; grant covers other safe project files."""
    require_development()
    if not isinstance(path, str) or not path.strip() or len(path) > 4096:
        raise ValueError("请输入有效本机路径。")
    if action not in ("stat", "list", "read"):
        raise ValueError("不支持的文件操作；开发文件访问只有 stat/list/read。")
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset 须为非负整数。")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_READ_BYTES:
        raise ValueError(f"limit 须为 1–{MAX_READ_BYTES}。")
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = ROOT / target
    target = target.resolve(strict=True)
    in_project, default_readable = _project_read_scope(target)
    with _ACCESS_LOCK:
        granted = time.monotonic() < _READ_ACCESS_UNTIL
    if not in_project or not (default_readable or granted):
        raise PermissionError('Only project source/logs are readable by default; other safe project files require grant')
    info = target.stat()
    result = {"path": str(target), "size": info.st_size, "modified_s": info.st_mtime,
              "directory": target.is_dir(), "read_only": True}
    if action == "stat":
        return result
    if action == "list":
        if not target.is_dir():
            raise ValueError("list 只接受目录。")
        count = min(limit, MAX_DIRECTORY_ENTRIES)
        visible = (entry for entry in target.iterdir()
                   if not entry.is_symlink() and (scope := _project_read_scope(entry.resolve()))[0]
                   and (scope[1] or granted))
        entries = list(islice(visible, offset, offset + count + 1))
        result.update(offset=offset, entries=[{"name": entry.name, "path": str(entry)}
                                              for entry in entries[:count]],
                      more=len(entries) > count, next_offset=offset + min(count, len(entries)))
        return result
    # Reject devices/pipes/sockets; reading them could block or consume a stream.
    if not target.is_file():
        raise ValueError("read 只接受普通文件。")
    with target.open("rb") as stream:
        stream.seek(offset)
        payload = stream.read(limit)
    try:
        content, encoding = payload.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        content, encoding = base64.b64encode(payload).decode("ascii"), "base64"
    result.update(offset=offset, bytes_read=len(payload), content=content, encoding=encoding,
                  mime=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                  more=offset + len(payload) < info.st_size, next_offset=offset + len(payload))
    return result


def developer_checks(check: Literal["environment", "syntax", "unit-tests"] = "environment") -> dict[str, Any]:
    """Run a fixed development check; no arbitrary commands, paths or arguments."""
    require_development()
    if check == "environment":
        packages = {}
        for name in ("PySide6-Essentials", "pydantic", "pydantic-ai-slim",
                     "pydantic-ai-harness", "fastmcp-slim", "numpy", "matplotlib", "keyring", "ezdxf"):
            try:
                packages[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                packages[name] = None
        return {"check": check, "python": sys.version.split()[0], "platform": sys.platform,
                "packages": packages}
    if check == "syntax":
        source = _fixed_path("coat_sim")
        files = sorted(source.rglob("*.py"))[:4000] + [_fixed_path("launcher.py")]
        errors = []
        for path in files:
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                raise PermissionError("源码检查拒绝链接到项目外的文件。")
            try:
                if path.stat().st_size > 4_000_000:
                    raise ValueError("源码文件过大")
                ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path.relative_to(ROOT)))
            except (SyntaxError, OSError, ValueError) as exc:
                errors.append({"file": str(path.relative_to(ROOT)), "line": getattr(exc, "lineno", None),
                               "error": str(exc)[:1000]})
        return {"check": check, "files": len(files), "passed": not errors, "errors": errors[:100]}
    if check != "unit-tests":
        raise ValueError("不支持的开发检查。")
    directory = _fixed_path("tests")
    if not directory.is_dir():
        return {"check": check, "available": False, "reason": "项目没有 tests 目录。"}
    # The model cannot write project code. Only trusted, existing project tests
    # are executable here; arbitrary assistant scripts still require the sandbox.
    for path in directory.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise PermissionError("项目测试目录存在外部链接，拒绝执行。")
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
                   "HOME", "USERPROFILE", "LANG", "LC_ALL") if key in os.environ}
    environment["COAT_RELEASE_BUILD"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        with tempfile.TemporaryDirectory(prefix="coat-developer-check-") as scratch:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--basetemp", scratch,
                 str(directory / "test_development_project_scope.py"),
                 str(directory / "test_assistant_discussion_mode.py")],
                cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=60, check=False, **options)
        output = result.stdout.decode("utf-8", errors="replace")
        counts = re.findall(r"\b(\d+) (?:passed|failed|error|errors)\b", output)
        tests_run = sum(map(int, counts)) if counts else 0
        return {"check": check, "exit_code": result.returncode,
                "tests_run": tests_run, "passed": result.returncode == 0 and tests_run > 0,
                "reason": "No pytest tests completed" if tests_run == 0 else None,
                "output": output[-16_000:], "truncated": len(output) > 16_000}
    except subprocess.TimeoutExpired:
        return {"check": check, "passed": False, "timed_out": True, "timeout_s": 60}


def developer_persona(action: Literal["read", "update"] = "read", text: str = "",
                      expected_sha256: str = "") -> dict[str, Any]:
    """Read/update the fixed persona with conflict checking and a recoverable copy."""
    require_development()
    path = _fixed_path("agent.md")
    current = path.read_bytes()
    fingerprint = hashlib.sha256(current).hexdigest()
    if action == "read":
        if len(current) > MAX_PERSONA_BYTES:
            raise ValueError("人设文件超过读取限制。")
        return {"text": current.decode("utf-8"), "sha256": fingerprint}
    if action != "update":
        raise ValueError("不支持的人设操作。")
    if not expected_sha256 or expected_sha256 != fingerprint:
        raise ValueError("人设已变化；请先读取最新 sha256 后更新。")
    payload = text.encode("utf-8")
    if not text.strip() or len(payload) > MAX_PERSONA_BYTES:
        raise ValueError("人设不能为空，最多 32 KB。")
    directory = _fixed_path("artifacts/development/persona")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = directory / f"{stamp}-{fingerprint[:12]}.md"
    with backup.open("xb") as stream:
        stream.write(current)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".persona-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        if hashlib.sha256(path.read_bytes()).hexdigest() != fingerprint:
            raise ValueError("人设在保存期间变化，已取消更新。")
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"saved": True, "sha256": hashlib.sha256(payload).hexdigest(),
            "backup": str(backup.relative_to(ROOT)), "effective": "下一轮任务",
            "permissions_changed": False}
