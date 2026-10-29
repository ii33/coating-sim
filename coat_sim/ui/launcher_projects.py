"""Recent-project picker; writable project owners live in independent runtimes."""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from threading import Thread
from typing import Callable

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot

from .local_files import local_file_path
from .launcher_project_files import copy_saved_project, export_saved_project, trash_project, project_deletion_plan
from .project_runtime_client import create_project_runtime, open_project_runtime
from .recent_projects import RecentProjects
from .recent_project_tasks import RecentProjectTasks


class ProjectLauncherAdapter(QObject):
    """Own only recent history and one background open request, never a host."""

    projectsChanged = Signal()

    def __init__(self, parent: QObject | None = None, *, geometry_settings=None,
                 recent_projects=None, runtime_opener: Callable = open_project_runtime,
                 runtime_creator: Callable = create_project_runtime):
        super().__init__(parent)
        self._recent = recent_projects if recent_projects is not None else RecentProjects()
        self._runtime_opener = runtime_opener
        self._runtime_creator = runtime_creator
        factory = getattr(self._recent, "worker_factory", None)
        self._history_worker = RecentProjectTasks(factory() if callable(factory) else lambda: self._recent)
        self._entries: list[dict] = []
        self._history_requests: dict[int, Future] = {}
        self._history_version = 0
        self._history_error = ""
        self._busy = False
        self._error = ""
        self._selected_file = ""
        self._stopped = False
        self._pending: Future | None = None
        self._pending_operation = "open"
        self._file_message = ""
        self._deletion_plan: dict = {}
        self._poll = QTimer(self)
        self._poll.setInterval(20)
        self._poll.timeout.connect(self._complete_pending)
        self._history_poll = QTimer(self)
        self._history_poll.setInterval(20)
        self._history_poll.timeout.connect(self._complete_history)
        self._request_history("refresh")

    @Property(list, notify=projectsChanged)
    def projectEntries(self) -> list[dict]:
        return [dict(row, active=row["descriptorFile"] == self._selected_file)
                for row in self._entries]

    @Property(bool, notify=projectsChanged)
    def projectBusy(self) -> bool:
        return self._busy

    @Property(bool, notify=projectsChanged)
    def recentBusy(self) -> bool:
        return bool(self._history_requests)

    @Property(str, notify=projectsChanged)
    def projectError(self) -> str:
        return self._error or self._history_error

    @Property(str, notify=projectsChanged)
    def projectFileMessage(self) -> str:
        return self._file_message

    @Property(dict, notify=projectsChanged)
    def projectDeletionPlan(self) -> dict:
        return dict(self._deletion_plan)

    def _set_error(self, message: str) -> None:
        self._error = str(message)
        self.projectsChanged.emit()

    def _start(self, raw: str, name: str | None, *, summary: str | None = None,
               notes: str | None = None) -> bool:
        if self._stopped or self._busy:
            self._set_error("正在处理工程请求，请稍候。" if self._busy else "启动器已关闭。")
            return False
        raw = str(raw).strip()
        if raw.startswith("file:"):
            raw = local_file_path(QUrl(raw))
        if not raw or (name is not None and not name.strip()):
            self._set_error("请选择本地 .coatproj 工程文件。" if name is None else "请填写项目名称和完整目录。")
            return False
        path = Path(raw).expanduser().absolute()
        self._busy, self._error = True, ""
        future: Future = Future()
        self._pending = future
        self._pending_operation = "open"
        self._file_message = ""
        def open_request() -> None:
            try:
                if name is None:
                    result = self._runtime_opener(path)
                elif summary is None and notes is None:
                    result = self._runtime_creator(name, path)
                else:
                    result = self._runtime_creator(name, path, summary=summary, notes=notes)
                future.set_result(result)
            except Exception as exc:
                future.set_exception(exc)
        # A launcher process does not wait for an unfinished startup worker on
        # exit. The detached runtime independently bounds its own admission.
        Thread(target=open_request, name="project-open-request", daemon=True).start()
        self._poll.start()
        self.projectsChanged.emit()
        return True

    @Slot(str, result=bool)
    def openProject(self, projectFile: str) -> bool:
        return self._start(projectFile, None)

    @Slot(str, str, result=bool)
    def createProject(self, name: str, rootPath: str) -> bool:
        return self._start(rootPath, name.strip())

    @Slot(str, str, str, str, result=bool)
    def createProjectWithInformation(self, name: str, rootPath: str,
                                     summary: str, notes: str) -> bool:
        if len(name.strip()) > 100 or len(summary) > 20000 or len(notes) > 20000:
            self._set_error("工程名称须不超过 100 字，摘要和备注各不超过 20000 字。")
            return False
        return self._start(rootPath, name.strip(), summary=summary, notes=notes)

    def _complete_pending(self) -> None:
        if self._stopped or self._pending is None or not self._pending.done():
            return
        pending, self._pending = self._pending, None
        self._poll.stop()
        try:
            result = pending.result()
            if self._pending_operation == "open":
                self._request_history("refresh", force=True)
                self._selected_file = str(result.project_file)
                self._file_message = "工程窗口已打开；若未切到前台，请从任务栏选择工程窗口。"
            elif self._pending_operation == "plan":
                self._deletion_plan = result
            elif self._pending_operation == "trash":
                self._request_history("remove", result["descriptorFile"])
                if self._selected_file == result["descriptorFile"]:
                    self._selected_file = ""
                self._file_message = result["message"]
            elif self._pending_operation == "copy":
                self._request_history("record", result["descriptorFile"], result["name"])
                self._file_message = result["message"]
            else:
                self._file_message = result["message"]
            self._error = ""
        except Exception as exc:
            self._error = ("请先保存当前修改并关闭此工程，再复制、导出或移入回收站。"
                           if getattr(exc, "code", "") == "active_project_owner" else str(exc))
        self._busy = False
        self.projectsChanged.emit()

    def _request_history(self, operation: str, *arguments, force: bool = False) -> None:
        if self._stopped:
            return
        if not force and operation == "refresh" and any(not value.done() for value in self._history_requests.values()):
            return  # An already queued mutation/refresh will return a fresh snapshot.
        self._history_version += 1
        self._history_requests[self._history_version] = self._history_worker.submit(operation, *arguments)
        self._history_poll.start()

    def _complete_history(self) -> None:
        if self._stopped:
            return
        changed = False
        for version, future in tuple(self._history_requests.items()):
            if not future.done():
                continue
            self._history_requests.pop(version)
            changed = True
            if version != self._history_version:
                continue  # A late refresh cannot replace newer record/remove intent.
            try:
                self._entries = [dict(row) for row in future.result()]
                self._history_error = ""
            except Exception as exc:
                self._history_error = "最近工程列表未能更新：" + str(exc)
        if not self._history_requests:
            self._history_poll.stop()
        if changed:
            self.projectsChanged.emit()

    def _start_file_action(self, operation: str, descriptor: str, project_id: str, destination: str = "",
                           plan_token: str = "") -> bool:
        if self._stopped or self._busy:
            self._set_error("正在处理工程请求，请稍候。" if self._busy else "启动器已关闭。")
            return False
        row = next((row for row in self._entries if row["descriptorFile"] == descriptor), None)
        if row is None or not project_id or row.get("projectId") != project_id:
            self._set_error("工程信息当前不可用，请检查工程文件并刷新后重试。")
            return False
        if operation not in {"trash", "plan"}:
            if destination.startswith("file:"):
                destination = local_file_path(QUrl(destination))
            if not destination.strip():
                self._set_error("请选择新的目标文件或目录。")
                return False
        if operation == "plan":
            self._deletion_plan = {**self._deletion_plan, "token": ""}
        self._busy, self._error, self._file_message = True, "", ""
        future: Future = Future()
        self._pending, self._pending_operation = future, operation
        actions: dict[str, Callable] = {"copy": copy_saved_project, "export": export_saved_project,
                                      "trash": trash_project, "plan": project_deletion_plan}
        function = actions[operation]
        arguments = (descriptor, row["projectId"]) + ((destination,) if operation not in {"trash", "plan"} else ())
        options: dict[str, object] = {}
        if plan_token:
            options["expected_plan"] = plan_token
        def action() -> None:
            try:
                future.set_result(function(*arguments, **options))
            except Exception as exc:
                future.set_exception(exc)
        Thread(target=action, name="launcher-project-file-action", daemon=True).start()
        self._poll.start()
        self.projectsChanged.emit()
        return True

    @Slot(str, str, str, result=bool)
    def copyRecentProject(self, descriptorFile: str, projectId: str, destination: str) -> bool:
        return self._start_file_action("copy", descriptorFile, projectId, destination)

    @Slot(str, str, str, result=bool)
    def exportRecentProject(self, descriptorFile: str, projectId: str, destination: str) -> bool:
        return self._start_file_action("export", descriptorFile, projectId, destination)

    @Slot(str, str, result=bool)
    def trashRecentProject(self, descriptorFile: str, projectId: str) -> bool:
        return self._start_file_action("trash", descriptorFile, projectId)

    @Slot(str, str, result=bool)
    def prepareRecentProjectDeletion(self, descriptorFile: str, projectId: str) -> bool:
        return self._start_file_action("plan", descriptorFile, projectId)

    @Slot(str, str, str, result=bool)
    def trashRecentProjectWithPlan(self, descriptorFile: str, projectId: str, token: str) -> bool:
        plan = self._deletion_plan
        if (not token or plan.get("token") != token or plan.get("descriptorFile") != descriptorFile
                or plan.get("projectId") != projectId):
            self._set_error("请先查看本工程的实际删除范围。")
            return False
        return self._start_file_action("trash", descriptorFile, projectId, plan_token=token)

    @Slot(str)
    def removeRecentProject(self, descriptorFile: str) -> None:
        self._request_history("remove", descriptorFile)
        self.projectsChanged.emit()

    @Slot()
    def refreshProjects(self) -> None:
        self._request_history("refresh")
        self.projectsChanged.emit()

    def stop(self) -> None:
        self._stopped = True
        self._poll.stop()
        self._history_poll.stop()
        self._history_worker.stop()
        self._history_requests.clear()
        self._pending = None
        self._busy = False
