"""Saved-input copy/export and explicitly confirmed whole-folder recycling."""
from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Callable

from PySide6.QtCore import QFile, QStandardPaths

from coat_sim.core.project_dependencies import ProjectError, reject_links
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_package import export_project, import_project
from coat_sim.project_recovery import ProjectOwnerLease


def _source(descriptor: str, project_id: str) -> ProjectStore:
    store = ProjectStore.open(descriptor)
    if not project_id or store.manifest["id"] != project_id:
        raise ProjectError("project_identity_mismatch", "工程身份已变化，请刷新最近工程列表后重试。")
    return store


def _directory_reason(root: Path) -> str:
    """Protect general roots; do not infer the contents' ownership."""
    reject_links(root)
    protected = {
        Path(root.anchor): "磁盘根目录",
        Path.home().resolve(): "用户主目录",
        Path(tempfile.gettempdir()).resolve(): "临时文件根目录",
        Path(__file__).resolve().parents[2]: "应用源码根目录",
        Path.cwd().resolve(): "应用工作目录",
    }
    for location, label in (
            (QStandardPaths.StandardLocation.DesktopLocation, "桌面根目录"),
            (QStandardPaths.StandardLocation.DocumentsLocation, "文档根目录"),
            (QStandardPaths.StandardLocation.DownloadLocation, "下载根目录")):
        value = QStandardPaths.writableLocation(location)
        if value:
            protected[Path(value).resolve()] = label
    for path, label in protected.items():
        if root == path or root in path.parents:
            return f"无法回收工程文件夹 {root}：该路径是{label}或其上级目录，请仅移出最近列表。"
    system_roots = {Path(value).resolve() for value in
                    ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/run", "/sbin", "/sys", "/usr", "/var")}
    if os.name == "nt":
        system_roots = {Path(system_value).resolve() for key in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)")
                        if (system_value := os.environ.get(key))}
    if any(root == path or path in root.parents or root in path.parents for path in system_roots):
        return f"无法回收工程文件夹 {root}：该路径位于系统目录内或包含系统目录，请仅移出最近列表。"
    return ""


def project_deletion_plan(descriptor: str, project_id: str) -> dict:
    """Preview the canonical folder, without scanning or classifying its files."""
    store = _source(descriptor, project_id)
    reason = _directory_reason(store.root)
    if reason:
        raise ProjectError("shared_project_directory", reason)
    root_info, descriptor_info = store.root.stat(), store.project_file.stat()
    token_data = [project_id, str(store.root), str(store.project_file),
                  root_info.st_dev, root_info.st_ino, descriptor_info.st_dev, descriptor_info.st_ino]
    token = hashlib.sha256(json.dumps(token_data, ensure_ascii=False).encode()).hexdigest()
    return {"descriptorFile": str(store.project_file), "projectId": project_id,
            "directoryPath": str(store.root), "token": token}


def _move_to_trash(path: str) -> tuple[bool, str]:
    # The instance API reliably exposes both Qt's error and resulting trash path.
    entry = QFile(path)
    if not entry.moveToTrash():
        raise OSError(entry.errorString())
    return True, entry.fileName()


def _error_detail(error: OSError) -> str:
    detail = error.strerror or str(error)
    winerror: object = getattr(error, "winerror", None)
    if isinstance(winerror, int):
        detail = f"[WinError {winerror}] " + detail
    if error.filename:
        detail += "\n文件：" + str(error.filename)
    return detail


def trash_project(descriptor: str, project_id: str, *, move_to_trash: Callable | None = None,
                  expected_plan: str = "") -> dict:
    """Recycle exactly the confirmed folder under the existing root/ID lease."""
    store = _source(descriptor, project_id)
    with ProjectOwnerLease.acquire(store.project_file, project_id):
        plan = project_deletion_plan(descriptor, project_id)
        if expected_plan and plan["token"] != expected_plan:
            raise ProjectError("stale_project_directory", "工程文件夹路径或身份已变化，请重新确认；尚未回收文件。")
        mover = move_to_trash if move_to_trash is not None else _move_to_trash
        try:
            success, trash_path = mover(plan["directoryPath"])
        except OSError as exc:
            raise ProjectError("trash_failed", "无法将工程文件夹 " + plan["directoryPath"]
                               + " 移入回收站：" + _error_detail(exc) + "；最近记录保留，请检查该路径。") from exc
        if not success or store.root.exists():
            raise ProjectError("trash_failed", "系统未确认工程文件夹 " + plan["directoryPath"]
                               + " 已移入回收站；最近记录保留，请检查该路径。")
    return {"descriptorFile": str(store.project_file), "trashPath": str(trash_path),
            "message": "整个工程文件夹已移入回收站：" + plan["directoryPath"]}


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
