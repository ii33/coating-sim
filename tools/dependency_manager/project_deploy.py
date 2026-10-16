"""Fetch this project into a fresh checkout, then reuse the dependency manager.

No build recipes, arbitrary repositories, credentials, activation or publishing.
Planning checks out source without executing it. Dependencies only use .venv.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .git_runtime import find_managed_git
from . import manager
from .mirrors import MIRRORS, ProxyConfig, redact

REPOSITORY = "ii33/coating-sim"
LEGACY_REPOSITORY = "ii33/deposition-sim"
REPOSITORY_URL = "https://github.com/ii33/coating-sim.git"
LEGACY_REPOSITORY_URL = "https://github.com/ii33/deposition-sim.git"
_SHA = re.compile(r"[0-9a-fA-F]{40}")
_MAX_OUTPUT = 2 * 1024 * 1024
_STATE = "dependency-manager-deploy"


def repository_source(source="github", repository_url=None):
    """Only this project's GitHub origin or an explicitly chosen CNB mirror."""
    if source not in ("github", "cnb"):
        raise ValueError("Select github or cnb")
    url = repository_url or (REPOSITORY_URL if source == "github" else "")
    parsed = urlsplit(url)
    host = "github.com" if source == "github" else "cnb.cool"
    if (parsed.scheme != "https" or parsed.netloc != host or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", parsed.path)
            or any(part in (".", "..") for part in parsed.path.split("/"))):
        raise ValueError("Use a credential-free HTTPS repository URL on " + host)
    if source == "github" and url not in (REPOSITORY_URL, REPOSITORY_URL[:-4], LEGACY_REPOSITORY_URL, LEGACY_REPOSITORY_URL[:-4]):
        raise ValueError("GitHub deployment is limited to ii33/coating-sim or the explicit legacy ii33/deposition-sim source")
    return {"provider": source, "url": url}


def _validated_ref(ref):
    if not isinstance(ref, str) or not ref or len(ref) > 200:
        raise ValueError("Select a branch/tag or full commit SHA")
    if _SHA.fullmatch(ref):
        return ref.lower()
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", ref) or ".." in ref
            or "//" in ref or ref.endswith(("/", ".", ".lock"))
            or any(part.startswith(".") for part in ref.split("/"))
            or (ref.startswith("refs/") and not ref.startswith(("refs/heads/", "refs/tags/")))):
        raise ValueError("Unsafe Git ref; select a branch/tag or full commit SHA")
    return ref


def _identity(directory):
    stat = directory.stat()
    return {"path": str(directory), "device": stat.st_dev, "inode": stat.st_ino}


def _git_identity(path):
    stat = Path(path).stat()
    return {**_identity(Path(path)), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _plan_id(plan):
    return _digest({key: value for key, value in plan.items() if key != "plan_id"})


def _git_environment(proxy):
    # Preserve existing Git credential helpers/home. Never offer an auth prompt,
    # copy credentials into arguments/config, or inherit Git command injection.
    env = manager.clean_environment(proxy)
    for key in list(env):
        if key.upper().startswith("GIT_"):
            env.pop(key, None)
    env.update(GIT_ATTR_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=os.devnull,
               SSH_ASKPASS=os.devnull, GCM_INTERACTIVE="Never", GIT_LFS_SKIP_SMUDGE="1")
    return env


def _stop_git(process, emit):
    confirmed = True
    try:
        if os.name == "nt":
            taskkill = str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe")
            confirmed = subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"],
                                       capture_output=True, timeout=10).returncode == 0
            if process.poll() is None:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
    except ProcessLookupError:
        pass
    except (OSError, KeyError, subprocess.SubprocessError):
        confirmed = False
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass
    if not confirmed:
        emit({"event": "warning", "text": "Git process-tree termination was not confirmed; inspect descendants before retry"})
    return confirmed


def _run_git(git, args, *, cwd, proxy, cancel_event, deadline, emit, log=False):
    if cancel_event.is_set():
        raise InterruptedError("Project fetch cancelled")
    if time.monotonic() >= deadline:
        raise TimeoutError("Project fetch timed out")
    hooks = Path(cwd) / ".git" / _STATE / "disabled-hooks"
    argv = [git, "-c", "core.hooksPath=" + str(hooks), "-c", "core.fsmonitor=false",
            "-c", "core.worktree=" + str(cwd), "-c", "core.bare=false",
            "-c", "core.sparseCheckout=false",
            "-c", "core.attributesfile=" + os.devnull, "-c", "submodule.recurse=false",
            "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
            "-c", "http.sslVerify=true", "-c", "http.followRedirects=false",
            "-c", "filter.lfs.process=", "-c", "filter.lfs.smudge=",
            "-c", "filter.lfs.required=false", "-c", "core.protectNTFS=true", *args]
    # Spool into temporary files rather than unbounded communicate() buffers.
    # These files are removed at exit and are not added to the project checkout.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        process = subprocess.Popen(argv, cwd=cwd, env=_git_environment(proxy),
                                   stdout=output, stderr=error, shell=False,
                                   start_new_session=os.name != "nt",
                                   creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0)
        while True:
            if os.fstat(output.fileno()).st_size + os.fstat(error.fileno()).st_size > _MAX_OUTPUT:
                _stop_git(process, emit)
                raise ValueError("Git output exceeds the metadata/log bound")
            try:
                process.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancel_event.is_set() or time.monotonic() >= deadline:
                    stopped = _stop_git(process, emit)
                    if cancel_event.is_set():
                        raise InterruptedError("Project fetch cancelled; process stop confirmed=" + str(stopped)) from None
                    raise TimeoutError("Project fetch timed out; process stop confirmed=" + str(stopped)) from None
        output.seek(0)
        error.seek(0)
        raw_stdout, raw_stderr = output.read(_MAX_OUTPUT + 1), error.read(_MAX_OUTPUT + 1)
    if len(raw_stdout) + len(raw_stderr) > _MAX_OUTPUT:
        raise ValueError("Git output exceeds the metadata/log bound")
    stdout, stderr = raw_stdout.decode("utf-8", "replace"), raw_stderr.decode("utf-8", "replace")
    if log and stderr:
        emit({"event": "log", "text": redact(stderr.rstrip(), proxy)})
    if process.returncode:
        raise ValueError("Git failed; existing authentication may be unavailable: " + redact(stderr[-1500:], proxy))
    return stdout


def _resolve_commit(run, ref, repository_url=REPOSITORY_URL):
    if _SHA.fullmatch(ref):
        return {"requested_ref": ref, "resolved_ref": ref, "commit": ref}
    refs = ([ref, ref + "^{}"] if ref.startswith("refs/tags/") else [ref]
            if ref == "HEAD" or ref.startswith("refs/heads/") else
            ["refs/heads/" + ref, "refs/tags/" + ref, "refs/tags/" + ref + "^{}"])
    found = {}
    for line in run(["ls-remote", "--", repository_url, *refs]).splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or not _SHA.fullmatch(parts[0]) or parts[1] not in refs:
            raise ValueError("Unexpected Git ref response")
        found[parts[1]] = parts[0].lower()
    if ref == "HEAD" or ref.startswith("refs/"):
        selected = ref
    else:
        head, tag = "refs/heads/" + ref, "refs/tags/" + ref
        if head in found and tag in found:
            raise ValueError("Ambiguous branch/tag; select refs/heads/name or refs/tags/name")
        selected = head if head in found else tag
    commit = found.get(selected + "^{}", found.get(selected))
    if not commit:
        raise ValueError("Requested project ref was not found")
    return {"requested_ref": ref, "resolved_ref": selected, "commit": commit}


def _readonly_status(run):
    # status can invoke clean/process filters when refreshing working-tree blobs.
    # Read names only, never credential-bearing config values, and override every
    # configured filter for this command without changing any config or index.
    keys = run(["config", "--null", "--name-only", "--list"]).split("\0")
    overrides = []
    for key in sorted(set(keys)):
        if key.startswith("filter.") and key.rsplit(".", 1)[-1] in ("clean", "smudge", "process", "required"):
            if len(key) > 4096 or any(ord(char) < 32 for char in key):
                raise ValueError("Unsafe Git filter configuration key; status was not run")
            overrides.extend(["-c", key + ("=false" if key.endswith(".required") else "=")])
    return run([*overrides, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"])


def _source_inputs(directory):
    # Complete declarations used by the canonical runtime/development catalog.
    return [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(directory.glob("requirements*.txt")) if path.is_file() and not path.is_symlink()]


def _validate_tree(run, commit):
    # Check before checkout, so submodules/symlink payloads and user-configured
    # attribute filters never get a chance to execute or escape the checkout.
    for line in run(["ls-tree", "-r", "-z", commit]).split("\x00"):
        if not line:
            continue
        header, relative = line.split("\t", 1)
        mode = header.split(" ", 1)[0]
        path = PurePosixPath(relative)
        if (mode not in ("100644", "100755") or path.is_absolute() or ".." in path.parts
                or "\\" in relative or any(char in relative for char in ("\n", "\r"))):
            raise ValueError("Source symlinks/submodules or unsafe paths are unsupported")
        if path.name == ".gitattributes":
            attributes = run(["show", commit + ":" + relative])
            if re.search(r"(?:^|\s)filter(?:=|\s|$)", attributes):
                raise ValueError("Repository attribute filters cannot execute during project deployment")


def _state_directory(directory):
    return directory / ".git" / _STATE


def _save_new(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def plan_project_deployment(directory, ref="main", groups=("desktop",), backend="cpu",
                            profile="runtime", mirror="pypi", python=None, proxy=None, *,
                            emit=None, cancel_event=None, timeout=300,
                            source="github", repository_url=None, git_path=None):
    """Prepare a new fixed-project checkout and its ordinary dependency plan."""
    proxy, emit, cancel_event = proxy or ProxyConfig(), emit or (lambda event: None), cancel_event or threading.Event()
    plan: dict[str, Any] = {
        "schema_version": 1, "operation": "project_deploy_plan", "status": "blocked",
        "repository": REPOSITORY, "repository_url": REPOSITORY_URL,
        "requires_confirmation": True, "warnings": [], "errors": [],
    }
    state = None
    try:
        proxy.validate()
        origin = repository_source(source, repository_url)
        repository_url = origin["url"]
        plan.update(repository_url=repository_url, provider=origin["provider"])
        ref = _validated_ref(ref)
        if profile not in manager.PROFILES or backend not in manager.BACKENDS:
            raise ValueError("Select a supported runtime/development profile and compute backend")
        groups = tuple(groups)
        if not groups or any(group not in manager.GROUPS for group in groups):
            raise ValueError("Select at least one known dependency group")
        if mirror not in MIRRORS:
            raise ValueError("Unknown registry mirror")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or not 1 <= timeout <= 3600:
            raise ValueError("Git fetch timeout must be between 1 and 3600 seconds")
        if not directory:
            raise ValueError("Choose a new project checkout directory")
        destination = Path(directory).absolute()
        if destination.exists() or destination.is_symlink() or destination != destination.resolve():
            raise ValueError("Deployment requires a fresh directory without symlink ancestors; existing content is never overwritten")
        if not destination.parent.is_dir():
            raise ValueError("Choose a new directory inside an existing parent directory")
        git = str(Path(git_path).resolve()) if git_path else find_managed_git() or shutil.which("git")
        if not git or not Path(git).is_file():
            raise ValueError("Git is unavailable; select a verified Git executable with --git")
        git = str(Path(git).resolve())
        deadline = time.monotonic() + timeout
        if cancel_event.is_set():
            raise InterruptedError("Project fetch cancelled before creating the checkout")
        destination.mkdir(mode=0o700)
        plan.update(directory=str(destination), directory_identity=_identity(destination),
                    git=git, git_identity=_git_identity(git), git_explicit=bool(git_path))

        def run(args, log=False):
            return _run_git(git, args, cwd=destination, proxy=proxy, cancel_event=cancel_event,
                            deadline=deadline, emit=emit, log=log)

        # URL rewriting can change the recipient, including credential-bearing
        # user aliases. Inspect without network and never expose the rewritten URL.
        if run(["ls-remote", "--get-url", repository_url]).strip() != repository_url:
            raise ValueError("Git URL rewriting changes the fixed project HTTPS destination; deployment is blocked")
        emit({"event": "phase", "phase": "resolve-project-commit"})
        source = _resolve_commit(run, ref, repository_url)
        plan["source"] = {"repository": REPOSITORY, **origin, **source}
        run(["init", "--quiet", "--template=", "."])
        state = _state_directory(destination)
        state.mkdir(mode=0o700)
        (state / "disabled-hooks").mkdir(mode=0o700)
        run(["remote", "add", "origin", repository_url])
        emit({"event": "phase", "phase": "fetch-pinned-project", "commit": source["commit"]})
        run(["fetch", "--depth=1", "--no-tags", "--no-recurse-submodules", "origin", source["commit"]], log=True)
        _validate_tree(run, source["commit"])
        run(["checkout", "--quiet", "--detach", source["commit"]])
        if run(["rev-parse", "HEAD"]).strip() != source["commit"]:
            raise ValueError("Fetched project commit differs from the planned immutable commit")
        if _readonly_status(run).strip():
            raise ValueError("New source checkout is not clean")
        if not (destination / "coat_sim").is_dir() or not (destination / "requirements.txt").is_file():
            raise ValueError("Selected ref does not contain the supported deposition-sim project")
        if (destination / ".venv").exists() or (destination / ".venv").is_symlink():
            raise ValueError("Source ref contains a target environment; no existing environment will be overwritten")
        dependency = manager.plan_environment(destination, destination / ".venv", groups,
                                              backend, mirror, "install", python, proxy, profile)
        plan.update(dependency_plan=dependency, source_inputs=_source_inputs(destination),
                    proxy=proxy.public(), git_timeout=timeout, profile=profile,
                    groups=dependency["groups"], backend=backend,
                    status="ready" if dependency["status"] == "ready" else "blocked")
        plan["warnings"].extend(dependency["warnings"])
        plan["warnings"].append("Source checkout is prepared; dependency execution still requires this plan's confirmation. No application or tests have been started")
        plan["errors"].extend(dependency["errors"])
    except InterruptedError as exc:
        plan["status"] = "cancelled"
        plan["errors"].append(redact(str(exc), proxy))
    except TimeoutError as exc:
        plan["status"] = "timed_out"
        plan["errors"].append(redact(str(exc), proxy))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        plan["errors"].append(redact(str(exc), proxy))
    plan["plan_id"] = _plan_id(plan)
    if state:
        try:
            _save_new(state / "plan.json", plan)
        except OSError as exc:
            plan["status"] = "failed"
            plan["errors"].append("Could not save the deployment plan: " + redact(str(exc), proxy))
            plan["plan_id"] = _plan_id(plan)
    return plan


def read_project_deployment_plan(directory):
    destination = Path(directory).absolute()
    path = _state_directory(destination) / "plan.json"
    if destination != destination.resolve() or path.is_symlink() or path != path.resolve():
        raise ValueError("Deployment plan directory must not use symlinks")
    plan = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(plan, dict) or plan.get("schema_version") != 1
            or plan.get("repository") not in (REPOSITORY, LEGACY_REPOSITORY)
            or repository_source(plan.get("provider", "github"), plan.get("repository_url"))["url"] != plan.get("repository_url")
            or plan.get("directory") != str(destination) or plan.get("plan_id") != _plan_id(plan)):
        raise ValueError("Stored deployment plan is invalid or changed")
    return plan


def execute_project_deployment(plan, confirmed=False, emit=None, cancel_event=None, proxy=None):
    """Verify prepared source, then let the existing manager own all mutations."""
    result: dict[str, Any] = {
        "schema_version": 1, "operation": "project_deploy", "status": "blocked",
        "plan_id": plan.get("plan_id", ""), "repository": REPOSITORY,
        "warnings": [], "errors": [], "application_started": False,
    }
    if not confirmed:
        result["errors"].append("Explicit confirmation of this project's current deployment plan is required")
        return result
    proxy, cancel_event, emit = proxy or ProxyConfig(), cancel_event or threading.Event(), emit or (lambda event: None)
    state, locked = None, False
    try:
        proxy.validate()
        if plan.get("status") != "ready" or plan.get("plan_id") != _plan_id(plan):
            raise ValueError("Deployment plan is blocked or altered; prepare a fresh deployment")
        directory = Path(plan["directory"])
        if read_project_deployment_plan(directory) != plan:
            raise ValueError("Deployment plan does not match the reviewed stored plan")
        if _identity(directory) != plan["directory_identity"]:
            raise ValueError("Checkout directory identity changed")
        if proxy.public() != plan["proxy"]:
            raise ValueError("Proxy settings changed; prepare a fresh deployment")
        current_git = plan["git"] if plan.get("git_explicit") else find_managed_git() or shutil.which("git")
        if (not current_git or str(Path(current_git).resolve()) != plan["git"]
                or _git_identity(plan["git"]) != plan["git_identity"]):
            raise ValueError("Selected Git executable changed; prepare a fresh deployment")
        state = _state_directory(directory)
        if state.is_symlink() or state.resolve() != state or (directory / ".git").is_symlink():
            raise ValueError("Deployment state/Git directory identity is unsafe")
        lock = os.open(state / "execute.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        locked = True
        os.close(lock)
        deadline = time.monotonic() + plan["git_timeout"]

        def run(args):
            return _run_git(plan["git"], args, cwd=directory, proxy=proxy, cancel_event=cancel_event,
                            deadline=deadline, emit=emit)

        if run(["rev-parse", "HEAD"]).strip() != plan["source"]["commit"]:
            raise ValueError("Source commit changed; refusing dependency deployment")
        if _readonly_status(run).strip():
            raise ValueError("Source checkout is dirty; existing changes will not be overwritten")
        if _source_inputs(directory) != plan["source_inputs"]:
            raise ValueError("Project dependency declarations changed")
        expected_venv = directory / ".venv"
        dependency = plan["dependency_plan"]
        if dependency["root"] != str(directory) or dependency["target"]["venv"] != str(expected_venv):
            raise ValueError("Deployment dependencies must target only this checkout's .venv")
        if expected_venv.exists() or expected_venv.is_symlink():
            raise ValueError("Target environment appeared after planning; preserve it and use a fresh deployment")
        emit({"event": "phase", "phase": "deploy-project-dependencies", "commit": plan["source"]["commit"]})
        deployed = manager.execute_plan(dependency, True, emit, cancel_event, proxy)
        result.update(status=deployed["status"], source=plan["source"], directory=str(directory),
                      dependency_result=deployed, target=deployed["target"], profile=plan["profile"],
                      groups=plan["groups"], backend=plan["backend"])
        result["warnings"].extend(deployed["warnings"])
        result["errors"].extend(deployed["errors"])
        if deployed["status"] == "success":
            interpreter = str(expected_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            commands = []
            if (directory / "launcher.py").is_file():
                commands.append({"purpose": "launch", "cwd": str(directory), "argv": [interpreter, "launcher.py"]})
            if plan["profile"] == "development" and (directory / "tools/run_tests.py").is_file():
                commands.append({"purpose": "quick-tests", "cwd": str(directory), "argv": [interpreter, "tools/run_tests.py", "quick"]})
            result["next_commands"] = commands
    except InterruptedError as exc:
        result["status"] = "cancelled"
        result["errors"].append(redact(str(exc), proxy))
    except TimeoutError as exc:
        result["status"] = "timed_out"
        result["errors"].append(redact(str(exc), proxy))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        result["errors"].append(redact(str(exc), proxy))
    finally:
        if locked and state:
            try:
                _save_new(state / "result.json", result)
            except OSError:
                result["warnings"].append("Deployment result could not be saved; checkout/partial environment are preserved")
            (state / "execute.lock").unlink(missing_ok=True)
    return result
