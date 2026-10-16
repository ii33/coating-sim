"""Launcher-independent project open/create with bounded detached startup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from typing import Any

from coat_sim.assistant_process import AssistantProcessClient
from coat_sim.core.project_store import MANIFEST_FILENAME, ProjectStore
from coat_sim.process_entry import module_command
from coat_sim.project_activation import activate_shared_project, find_shared_project_owner
from .control import PROJECT_ROOT, UiControlError
from .ready_channel import UiReadyChannel


def _record_recent_success(store: ProjectStore) -> None:
    """Best-effort compatibility history; never changes actual open success."""
    try:
        from .recent_projects import RecentProjects
        RecentProjects().record(store.project_file, store.manifest["name"])
    except Exception as exc:
        try:
            print(f"工程已打开，但最近工程列表未能保存：{exc}", file=sys.stderr, flush=True)
        except Exception:
            pass  # A closed diagnostic stream cannot replace a confirmed result.


def _start_runtime(arguments: list[str], shared: Any, application_root: str | Path,
                   timeout: float) -> None:
    """Keep only a local startup handle; never signal or own child lifetime."""
    root = Path(application_root).resolve()
    logs = shared.root / "artifacts" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"project-{secrets.token_hex(8)}.log"
    channel = UiReadyChannel()
    environment = os.environ.copy()
    for name in ("COAT_UI_PID", "COAT_UI_PARENT_PID", "COAT_UI_READY_ADDRESS",
                 "COAT_UI_READY_FAMILY", "COAT_UI_READY_KEY"):
        environment.pop(name, None)
    environment.update(channel.child_environment())
    environment["COAT_ASSISTANT_RUNTIME_ROOT"] = str(shared.root)
    environment["PYTHONUNBUFFERED"] = "1"
    kwargs: dict[str, Any] = {"cwd": str(root), "env": environment,
        "stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        with log_path.open("ab") as stream:
            process = subprocess.Popen(module_command("coat_sim.ui.project_runtime", *arguments),
                                       stdout=stream, **kwargs)
        event = channel.wait(process, timeout=timeout)
    finally:
        channel.close()
    if event.get("status") != "ready":
        detail = event.get("error") or event.get("status", "unknown")
        raise UiControlError("project_start_failed", f"工程进程启动未确认：{detail}。日志：{log_path}")


def _confirm_owner(shared: Any, store: ProjectStore) -> ProjectStore:
    # Ready checks the spawned process tree (including Windows venv redirectors);
    # discovery challenges the actual lifetime
    # owner, which may be a different process after simultaneous open attempts.
    target = find_shared_project_owner(shared, store, "")
    if target is None:
        raise UiControlError("project_owner_unconfirmed", "工程进程未提供可验证的工程会话，请稍后重试。")
    activated = activate_shared_project(shared, target)
    if not isinstance(activated, dict) or activated.get("history_managed") is not True:
        _record_recent_success(store)
    return store


def open_project_runtime(project_file: str | Path, *, client: Any = None,
                         application_root: str | Path = PROJECT_ROOT,
                         timeout: float = 50.0) -> ProjectStore:
    """Return only after an authenticated owner confirms its actual designer."""
    store = ProjectStore.open(project_file)
    shared = client if client is not None else AssistantProcessClient(application_root=application_root)
    shared.ensure_running()
    target = find_shared_project_owner(shared, store, "")
    if target is not None:
        activated = activate_shared_project(shared, target)
        if not isinstance(activated, dict) or activated.get("history_managed") is not True:
            _record_recent_success(store)
        return store
    _start_runtime(["--project", str(store.project_file)], shared, application_root, timeout)
    return _confirm_owner(shared, store)


def create_project_runtime(name: str, root_path: str | Path, *, summary: str | None = None,
                           notes: str | None = None, client: Any = None,
                           application_root: str | Path = PROJECT_ROOT,
                           timeout: float = 50.0) -> ProjectStore:
    """Creation itself belongs to the detached runtime, never a launcher daemon."""
    name = str(name).strip()
    if not name or len(name) > 100:
        raise UiControlError("invalid_project_name", "请填写不超过 100 字的项目名称。")
    information = {}
    for field, value in (("summary", summary), ("notes", notes)):
        if value is not None:
            if not isinstance(value, str) or len(value) > 20000:
                raise UiControlError("invalid_project_information", "工程摘要和备注各须不超过 20000 字。")
            information[field] = value
    root = Path(root_path).expanduser().absolute()
    if root.exists():
        raise UiControlError("project_exists", "新工程目标目录必须尚不存在，不会覆盖已有内容。")
    shared = client if client is not None else AssistantProcessClient(application_root=application_root)
    shared.ensure_running()
    arguments = ["--create-root", str(root), "--name", name]
    # A private bounded file avoids OS command-line size limits and exposing
    # project notes in process listings. Only the runtime creates the project.
    information_file = None
    try:
        if information:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="coat-create-",
                                             suffix=".json", delete=False) as stream:
                information_file = Path(stream.name)
                json.dump(information, stream, ensure_ascii=False)
            arguments.extend(["--create-information", str(information_file), "--consume-create-information"])
        _start_runtime(arguments, shared, application_root, timeout)
    finally:
        if information_file is not None:
            information_file.unlink(missing_ok=True)
    # ProjectStore.create is authoritative inside the owner, including the
    # destination race check and atomic transaction. Read only after real ready.
    return _confirm_owner(shared, ProjectStore.open(root / MANIFEST_FILENAME))
