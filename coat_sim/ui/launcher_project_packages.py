"""Asynchronous saved-input package operations for the project launcher."""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QTimer, QUrl

from coat_sim.core.project_store import ProjectError, ProjectStore
from coat_sim.project_package import export_project, import_project


def local_path(value: str) -> Path:
    if not value.strip():
        raise ProjectError("invalid_path", "请选择本地文件或目录。")
    if value.startswith("file:"):
        url = QUrl(value)
        if not url.isValid() or not url.isLocalFile():
            raise ProjectError("invalid_path", "请选择本地文件或目录。")
        value = url.toLocalFile()
    return Path(value).expanduser().absolute()


class ProjectPackageCoordinator:
    def __init__(self, adapter: Any):
        self.adapter = adapter
        self.pending: Future | None = None
        self.importing = False
        self.poll = QTimer(adapter)
        self.poll.setInterval(40)
        self.poll.timeout.connect(self.complete)

    def _admit(self) -> None:
        if self.adapter._busy or self.adapter._stopped:
            raise ProjectError("project_busy", "项目管理器正在处理其他请求。")
        if self.adapter._admission_guard is not None:
            allowed, message = self.adapter._admission_guard()
            if not allowed:
                raise ProjectError("project_frozen", message)

    def _start(self, function, *args) -> bool:
        self.adapter._error = ""
        self.adapter._busy = True
        self.pending = self.adapter._executor.submit(function, *args)
        self.poll.start()
        self.adapter.projectsChanged.emit()
        return True

    def export(self, project_id: str, destination: str) -> bool:
        self._admit()
        session = self.adapter.project_closer._session(project_id)
        # Reuse the existing flush-and-dirty preflight. It never saves drafts.
        dirty, _signature = self.adapter.project_closer._preflight(session, allow_active_jobs=True)
        if dirty:
            raise ProjectError("dirty_project", "请先保存此工程的修改，再导出工程包。")
        store = cast(ProjectStore, self.adapter.host.store_for(session.binding))
        self.importing = False
        return self._start(self._export_bound, store.project_file, project_id, local_path(destination))

    @staticmethod
    def _export_bound(project_file: Path, project_id: str, destination: Path) -> dict:
        store = ProjectStore.open(project_file)
        if store.manifest["id"] != project_id:
            raise ProjectError("project_identity_mismatch", "工程目录身份已变化，请重新打开。")
        return export_project(store, destination)

    def import_package(self, source: str, destination: str) -> bool:
        self._admit()
        self.importing = True
        return self._start(import_project, local_path(source), local_path(destination))

    def complete(self) -> None:
        if self.pending is None or not self.pending.done():
            return
        pending, self.pending = self.pending, None
        self.poll.stop()
        self.adapter._busy = False
        try:
            result = pending.result()
            if self.adapter._stopped:
                return
            if self.importing:
                open_imported = getattr(self.adapter, "open_imported_project", None)
                if callable(open_imported):
                    open_imported(result.project_file)
                else:
                    self.adapter.openProject(str(result.project_file))
            else:
                self.adapter._error = "工程包已导出：" + result["destination"]
        except Exception as exc:
            self.adapter._error = str(exc)
        finally:
            self.adapter.projectsChanged.emit()

    def stop(self) -> None:
        self.poll.stop()
        if self.pending is not None:
            self.pending.cancel()
            self.pending = None
