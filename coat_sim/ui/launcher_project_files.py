"""Bounded saved-input actions; never owns or controls a project process."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from typing import Callable

from PySide6.QtCore import QFile, QStandardPaths

from coat_sim.core.project_dependencies import ProjectError, reject_links
from coat_sim.core.project_paths import PROJECT_LAYOUT_DIRECTORIES
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_package import export_project, import_project
from coat_sim.project_recovery import ProjectOwnerLease

_ROOT_DIRECTORIES = {"objects", "data", "imports", "exports", "assets", "results"}
_RECOVERY_FILES = {"recovery.json", ".project-session.json", ".project-session-recovery.json",
                   ".project-session-reviewed-recovery.json"}
_MAX_TREE_ENTRIES = 100_000


def _source(descriptor: str, project_id: str) -> ProjectStore:
    store = ProjectStore.open(descriptor)
    if not project_id or store.manifest["id"] != project_id:
        raise ProjectError("project_identity_mismatch", "工程身份已变化，请刷新最近工程列表后重试。")
    return store


def _safe_tree(store: ProjectStore) -> tuple:
    """Fail closed unless the whole directory has the standard project scope.

    No marker is added to old projects. Unknown top-level content and nested
    projects make ownership ambiguous, so they can only be removed from history.
    This scan never follows a link and never changes project contents.
    """
    root = store.root
    reject_links(root)
    home = Path.home().resolve()
    protected = {Path(root.anchor), home, Path(tempfile.gettempdir()).resolve(),
                 Path(__file__).resolve().parents[2], Path.cwd().resolve()}
    for location in (QStandardPaths.StandardLocation.DesktopLocation,
                     QStandardPaths.StandardLocation.DocumentsLocation,
                     QStandardPaths.StandardLocation.DownloadLocation,
                     QStandardPaths.StandardLocation.HomeLocation):
        value = QStandardPaths.writableLocation(location)
        if value:
            protected.add(Path(value).resolve())
    if any(root == path or root in path.parents for path in protected):
        raise ProjectError("unsafe_project_directory", "此目录属于主目录、共享位置或程序目录，不能作为整工程删除。请仅移出最近列表。")
    system_roots = {Path(value).resolve() for value in
                    ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/run", "/sbin", "/sys", "/usr", "/var")}
    if os.name == "nt":
        system_roots = {Path(system_value).resolve() for key in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)")
                        if (system_value := os.environ.get(key))}
    if any(root == path or path in root.parents for path in system_roots):
        raise ProjectError("unsafe_project_directory", "系统目录中的内容不能整工程删除。请仅移出最近列表。")
    if os.path.ismount(root):
        raise ProjectError("unsafe_project_directory", "工程目录是挂载点，不能移入回收站。")
    for relative in PROJECT_LAYOUT_DIRECTORIES:
        path = root / relative
        reject_links(path)
        if not path.is_dir():
            raise ProjectError("unknown_project_directory", "无法确认整个目录均属于此工程。请仅移出最近列表，工程文件保留。")
    allowed = _ROOT_DIRECTORIES | _RECOVERY_FILES | {store.project_file.name}
    if any(path.name not in allowed or (path.name in _ROOT_DIRECTORIES and not path.is_dir())
           or (path.name not in _ROOT_DIRECTORIES and not path.is_file()) for path in root.iterdir()):
        raise ProjectError("unknown_project_directory", "工程目录含归属不明的其他内容，不能整目录删除。请仅移出最近列表。")
    records = []
    root_device = root.stat().st_dev
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or info.st_dev != root_device or (path != root and os.path.ismount(path))):
            raise ProjectError("unsafe_project_directory", "工程目录含链接、重解析点或挂载内容，不能整目录删除。")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ProjectError("unsafe_project_directory", "工程目录含特殊文件，不能整目录删除。")
        if path != store.project_file and path.suffix.casefold() == ".coatproj":
            raise ProjectError("nested_project", "此目录还包含其他工程，不能删除其父目录。请仅移出最近列表。")
        records.append((str(path.relative_to(root)), info.st_dev, info.st_ino, info.st_mode,
                        info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        if len(records) > _MAX_TREE_ENTRIES or len(path.relative_to(root).parts) > 64:
            raise ProjectError("project_directory_too_large", "目录范围过大，无法安全确认整工程删除。请仅移出最近列表。")
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
    return tuple(sorted(records))


def _move_to_trash(path: str) -> tuple[bool, str]:
    # PySide6 6.12's static overload can return only bool despite its stub's
    # tuple annotation. The instance API reliably updates fileName on success.
    entry = QFile(path)
    success = entry.moveToTrash()
    return success, entry.fileName() if success else ""


def trash_project(descriptor: str, project_id: str, *, move_to_trash: Callable | None = None) -> dict:
    """Hold the existing root/ID locks throughout the actual OS trash action.

    No permanent-delete fallback, recursive unlink, permission change or early
    lease release is permitted, including on Windows sharing failures.
    """
    store = _source(descriptor, project_id)
    with ProjectOwnerLease.acquire(store.project_file, project_id):
        current = _source(descriptor, project_id)
        before = _safe_tree(current)
        current._current()
        if _safe_tree(current) != before:
            raise ProjectError("stale_project_directory", "目录内容已变化，未移入回收站。请检查后重试。")
        reject_links(current.root)
        mover = move_to_trash if move_to_trash is not None else _move_to_trash
        success, trash_path = mover(str(current.root))
        if not success or current.root.exists():
            raise ProjectError("trash_failed", "无法将工程目录移入回收站，最近记录已保留；不会永久删除文件。请检查目录后重试。")
    return {"descriptorFile": str(store.project_file), "trashPath": str(trash_path),
            "message": "工程目录已移入回收站。"}


def export_saved_project(descriptor: str, project_id: str, destination: str) -> dict:
    store = _source(descriptor, project_id)
    with ProjectOwnerLease.acquire(store.project_file, project_id):
        # A running owner may have unsaved edits. Never quietly export a stale
        # saved-only replacement for the existing runtime's dirty preflight.
        result = export_project(_source(descriptor, project_id), destination)
    return {**result, "message": "工程包已导出：" + result["destination"]}


def copy_saved_project(descriptor: str, project_id: str, destination: str) -> dict:
    store = _source(descriptor, project_id)
    with ProjectOwnerLease.acquire(store.project_file, project_id):
        # The established archive validator and atomic no-replace publication
        # remain authoritative. Runtime data/unknown files are never copied.
        with tempfile.TemporaryDirectory(prefix="coat-project-copy-") as temporary:
            package = Path(temporary) / "copy.cproject"
            export_project(_source(descriptor, project_id), package)
            copied = import_project(package, destination)
    return {"descriptorFile": str(copied.project_file), "name": copied.manifest["name"],
            "message": "工程副本已创建：" + str(copied.root)}
