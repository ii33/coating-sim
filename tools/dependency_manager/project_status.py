"""Read-only source identity; never fetch, checkout, import or rewrite a project."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from . import manager, project_deploy as deploy
from .git_runtime import find_managed_git
from .mirrors import ProxyConfig, redact


def _version(directory):
    try:
        path = directory / "coat_sim/version.txt"
        if path.stat().st_size > 128:
            return "unknown"
        value = path.read_text(encoding="utf-8").strip()
        match = re.fullmatch(r"dev\.(\d{8})\.(\d{2})", value)
        if match and int(match[2]) > 0:
            datetime.strptime(match[1], "%Y%m%d")
            return value
    except (OSError, ValueError, UnicodeError):
        pass
    return "unknown"


def check_project(directory, venv=None, groups=("desktop",), backend="cpu", profile="runtime",
                  *, check_remote=False, source="github", repository_url=None, ref="main",
                  proxy=None, cancel_event=None, timeout=30, git_path=None):
    """Comparison means commit equality, never an unproven ahead/behind claim."""
    proxy = proxy or ProxyConfig()
    cancel_event = cancel_event or threading.Event()
    root = Path(directory).resolve()
    result = {"schema_version": 1, "operation": "project_check", "status": "success",
              "directory": str(root), "internal_version": _version(root),
              "commit": None, "ref": None, "dirty": None,
              "submodule_dirty": "not_checked", "dirty_scope": "parent_worktree_excluding_submodules",
              "source": {"provider": "unknown", "url": None},
              "provenance": "unknown", "comparison": "unknown" if check_remote else "not_requested",
              "warnings": [], "errors": []}
    if not root.is_dir():
        result.update(status="failed", errors=["Local project directory does not exist"])
        return result
    result["environment"] = manager.inspect_environment(root, venv, groups, backend, profile)
    try:
        git = str(Path(git_path).resolve()) if git_path else find_managed_git() or shutil.which("git")
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3600:
            raise ValueError("Git check timeout must be between 1 and 3600 seconds")
        deadline = time.monotonic() + timeout

        def run(args):
            return deploy._run_git(git, args, cwd=root, proxy=proxy, cancel_event=cancel_event,
                                   deadline=deadline, emit=lambda event: None)

        if git and (root / ".git").exists():
            commit = run(["rev-parse", "--verify", "HEAD"]).strip()
            if not deploy._SHA.fullmatch(commit):
                raise ValueError("Local Git commit is invalid")
            result.update(commit=commit, provenance="git",
                          ref=run(["rev-parse", "--abbrev-ref", "HEAD"]).strip(),
                          dirty=bool(deploy._readonly_status(run)))
            result["warnings"].append("Git content filters are disabled during read-only status; filter-transformed files may conservatively appear modified. Submodule worktrees are not inspected; dirty describes the parent worktree only")
            # A missing/unsupported origin is honest unknown provenance, not an error.
            try:
                url = run(["config", "--get", "remote.origin.url"]).strip()
                for provider in ("github", "cnb"):
                    try:
                        result["source"] = deploy.repository_source(provider, url)
                        break
                    except ValueError:
                        continue
            except ValueError:
                pass
        else:
            result["warnings"].append("Git metadata or Git executable unavailable; commit/ref/dirty/source remain unknown (source archives are not inferred from environment variables)")
        if check_remote:
            result["comparison"] = "unknown"
            origin = deploy.repository_source(source, repository_url)
            ref = deploy._validated_ref(ref)
            proxy.validate()
            if not git:
                raise ValueError("Git is required to compare the selected remote ref")
            if run(["ls-remote", "--get-url", origin["url"]]).strip() != origin["url"]:
                raise ValueError("Git URL rewriting changes the selected HTTPS destination")
            remote = deploy._resolve_commit(run, ref, origin["url"])
            result["remote"] = {**origin, **remote}
            if deploy._SHA.fullmatch(ref):
                result["warnings"].append("Full SHA compared as supplied; remote existence is not verified without fetching")
            if result["commit"]:
                result["comparison"] = "equal" if result["commit"] == remote["commit"] else "different"
            result["warnings"].append("Comparison is commit equality only; no ahead/behind inference and no local files changed")
    except InterruptedError as exc:
        result.update(status="cancelled", errors=[redact(str(exc), proxy)])
    except TimeoutError as exc:
        result.update(status="timed_out", errors=[redact(str(exc), proxy)])
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        result.update(status="failed", errors=[redact(str(exc), proxy)])
    return result
