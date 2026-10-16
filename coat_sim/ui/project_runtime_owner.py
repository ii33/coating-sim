"""Independent runtime owner, reusing existing project/session desktop adapters."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Callable, cast

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot

from coat_sim.core.project_store import ProjectError, ProjectStore, project_file_root
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_default_loading import ProjectDefaultPlan
from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
from coat_sim.project_recovery import ProjectOwnerLease
from coat_sim.project_session import ProjectSessionHost, ProjectStoreView
from .project_film_host import ProjectFilmDesktop
from .local_files import local_file_path
from .project_discovery import SharedProjectDiscovery


@dataclass(frozen=True)
class _ActivatedProject:
    target: dict[str, str]


ProjectOpenResult = tuple[ProjectStore, Any, ProjectOwnerLease] | _ActivatedProject


class ProjectRuntimeOwner(QObject):
    """Own runtime admission, host, lifetime leases and project presentation.

    Store I/O runs in one worker. A QTimer transfers completed values back to
    the GUI thread before registering the session or touching any Qt window.
    """

    projectsChanged = Signal()
    projectInformationChanged = Signal(str)
    openFinished = Signal(bool)
    recoveryConfirmationRequested = Signal(str, str, str, int, str)
    projectCloseConfirmationRequested = Signal(str)

    def __init__(self, parent: QObject | None = None, *, geometry_settings=None,
                 store_loader: Callable[[Path], ProjectStore] | None = None,
                 default_loader: Callable[[ProjectStore], Any] | None = None):
        super().__init__(parent)
        self._admission_guard: Callable[[], tuple[bool, str]] | None = None
        self._staged: dict[Path, ProjectStore] = {}
        self.host = ProjectSessionHost(cast(Callable[[Path], ProjectStoreView], self._store_factory))
        self.desktop = ProjectFilmDesktop(self.host, self, geometry_settings=geometry_settings)
        self.projectInformationChanged.connect(self.desktop.projectInformationChanged.emit)
        self._store_loader = store_loader or ProjectStore.open
        self._default_loader = default_loader or self._load_default
        self._selected_id = ""
        self._information_edits: dict[str, dict[str, Any]] = {}
        self._plans: dict[str, Any] = {}
        self._plans_with_snapshot: set[str] = set()
        self._initialized_film_drafts: set[str] = set()
        self._leases: dict[str, ProjectOwnerLease] = {}
        self._pending_lease_releases: list[Future] = []
        self._draining_transactions: list[Future] = []
        self._external_pending: Future | None = None
        self._unclean_stop = False
        self._busy = False
        self._error = ""
        self._pending: Future[ProjectOpenResult] | None = None
        self._shared: SharedProjectDiscovery | None = None
        self._stopped = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="project-runtime")
        self._poll = QTimer(self)
        self._poll.setInterval(20)
        self._poll.timeout.connect(self._complete_pending)
        self._external_poll = QTimer(self)
        self._external_poll.setInterval(20)
        self._external_poll.timeout.connect(self._complete_external_open)
        self.desktop.moduleError.connect(self._set_error)
        from .project_recovery_coordinator import ProjectRecoveryCoordinator
        self.recovery = ProjectRecoveryCoordinator(self)
        from .project_close_coordinator import ProjectCloseCoordinator
        self.project_closer = ProjectCloseCoordinator(self)
        self.desktop.project_closer = self.project_closer
        from .launcher_project_packages import ProjectPackageCoordinator
        self.packages = ProjectPackageCoordinator(self)

    def _store_factory(self, root: Path) -> ProjectStore:
        # ProjectSessionHost invokes this on the GUI thread after the worker's
        # validated store has been staged. Do not reopen a stale store here.
        try:
            return self._staged.pop(root)
        except KeyError as exc:
            raise RuntimeError("Project admission has no validated store") from exc

    @Property(list, notify=projectsChanged)
    def projectEntries(self) -> list[dict[str, Any]]:
        rows = [dict(projectId=session.binding.project_id,
                     name=session.name, root=str(session.binding.root),
                     descriptorFile=str(cast(ProjectStore, self.host.store_for(session.binding)).project_file),
                     active=session.binding.project_id == self._selected_id,
                     defaultState=getattr(self._plans.get(session.binding.project_id), "state", "unknown"),
                     defaultFilmSnapshot=session.binding.project_id in self._plans_with_snapshot,
                     defaultFilmDraft=session.binding.project_id in self._initialized_film_drafts)
                for session in self.host.sessions()]
        local_ids = {row["projectId"] for row in rows}
        if self._shared is not None:
            rows.extend(dict(row, active=row["projectId"] == self._selected_id)
                        for row in self._shared.entries if row["projectId"] not in local_ids)
        return rows

    def connect_shared_projects(self, client: Any, generation: str) -> None:
        if self._shared is not None or self._stopped:
            return
        self._shared = SharedProjectDiscovery(client, generation, self.projectsChanged.emit, self)
        self._shared.start()

    @Property(str, notify=projectsChanged)
    def selectedProjectId(self) -> str:
        return self._selected_id

    @Property(bool, notify=projectsChanged)
    def projectBusy(self) -> bool:
        return self._busy

    @Property(str, notify=projectsChanged)
    def projectError(self) -> str:
        return self._error

    def _set_error(self, message: str) -> None:
        self._error = str(message)
        self.projectsChanged.emit()

    @staticmethod
    def _load_default(store: ProjectStore) -> Any:
        from coat_sim.core.project_default_loading import load_project_default
        return load_project_default(store)

    @staticmethod
    def _read_store(path: Path, name: str | None, loader, default_loader,
                    shared_client=None, own_generation: str = "",
                    summary: str | None = None, notes: str | None = None) -> ProjectOpenResult:
        if name is None:
            store = loader(path)
        else:
            store = ProjectStore.create(path, name, summary=summary, notes=notes)
        from coat_sim.project_activation import find_shared_project_owner
        if name is None and shared_client is not None:
            target = find_shared_project_owner(shared_client, store, own_generation)
            if target is not None:
                return ProjectRuntimeOwner._activate_remote(shared_client, target)
        try:
            lease = ProjectOwnerLease.acquire(store.project_file, store.manifest["id"])
        except Exception as exc:
            if getattr(exc, "code", "") != "active_project_owner" or shared_client is None:
                raise
            # A first opener may have registered between discovery and lease
            # admission. Resolve it once; never replace its lock or its draft.
            target = find_shared_project_owner(shared_client, store, own_generation)
            if target is not None:
                return ProjectRuntimeOwner._activate_remote(shared_client, target)
            from .control import UiControlError
            raise UiControlError("active_project_owner", "此工程已由其他会话持有，但当前未找到可激活窗口；请保留原窗口并刷新重试。") from exc
        try:
            # Refresh under lifetime ownership: an earlier owner may have
            # committed between the initial read and lock acquisition.
            store = ProjectStore.open(store.project_file)
            return store, default_loader(store), lease
        except BaseException:
            lease.close(clean=False)
            raise

    def set_admission_guard(self, guard: Callable[[], tuple[bool, str]]) -> None:
        self._admission_guard = guard

    def _start(self, selected_path: str, name: str | None, *,
               summary: str | None = None, notes: str | None = None) -> bool:
        if self._stopped:
            self._set_error("项目管理器已关闭。")
            return False
        if self._busy:
            self._set_error("正在打开另一个项目，请稍候。")
            return False
        raw = str(selected_path).strip()
        if not raw or (name is not None and not str(name).strip()):
            self._set_error("请填写项目名称和完整目录。" if name is not None else "请选择 .coatproj 工程文件。")
            return False
        if raw.startswith("file:"):
            url = QUrl(raw)
            raw = local_file_path(url)
            if not raw:
                self._set_error("请选择本地工程目录。" if name is not None else "请选择本地 .coatproj 工程文件。")
                return False
        path = Path(raw).expanduser().absolute()
        if name is None:
            try:
                root = project_file_root(path)
            except Exception as exc:
                self._set_error(str(exc))
                return False
            canonical_file = root / path.name
            for session in self.host.sessions():
                if os.path.normcase(str(session.binding.root)) == os.path.normcase(str(root)):
                    current = cast(ProjectStore, self.host.store_for(session.binding)).project_file
                    if os.path.normcase(str(current)) != os.path.normcase(str(canonical_file)):
                        self._set_error("此目录已由另一个工程文件打开，请先关闭该工程。")
                        return False
                    return self.activateProject(session.binding.project_id)
        if self._admission_guard is not None:
            allowed, message = self._admission_guard()
            if not allowed:
                self._set_error(message)
                return False
        self._error = ""
        self._busy = True
        self.projectsChanged.emit()
        self._pending = self._executor.submit(
            self._read_store, path, name, self._store_loader, self._default_loader,
            self._shared.client if self._shared is not None else None,
            self._shared.generation if self._shared is not None else "", summary, notes)
        self._poll.start()
        return True

    @Slot(str, str, result=bool)
    def createProject(self, name: str, rootPath: str) -> bool:
        return self._start(rootPath, name.strip())

    @Slot(str, str, str, str, result=bool)
    def createProjectWithInformation(self, name: str, rootPath: str,
                                     summary: str, notes: str) -> bool:
        return self._start(rootPath, name.strip(), summary=summary, notes=notes)

    @Slot(str, result=bool)
    def openProject(self, projectFile: str) -> bool:
        return self._start(projectFile, None)

    @Slot(str, str, result=bool)
    def exportProjectPackage(self, projectId: str, destination: str) -> bool:
        try:
            return self.packages.export(projectId, destination)
        except Exception as exc:
            self._set_error(str(exc))
            return False

    def _information_session(self, project_id: str, generation: str | None = None, *,
                             retain_edit: bool = False):
        if self._stopped or (self._busy and not retain_edit):
            raise ProjectError("project_busy", "工程当前无法编辑信息，请稍后重试。")
        session = next((row for row in self.host.sessions()
                        if row.binding.project_id == project_id), None)
        if session is None:
            raise ProjectError("project_closed", "此工程会话已经关闭。")
        if generation is not None and session.binding.generation != generation:
            raise ProjectError("stale_session", "原工程会话已经更新；请取消修改后重新打开工程管理。")
        if not retain_edit and self.project_closer.is_pending(session.binding):
            raise ProjectError("project_closing", "工程正在等待关闭确认，请先取消关闭。")
        lease = self._leases.get(project_id)
        if lease is None:
            raise ProjectError("project_closed", "工程的可写会话所有权已经结束。")
        lease._require_open()
        if lease.project_id != project_id or lease.root != session.binding.root:
            raise ProjectError("project_identity_mismatch", "工程信息与当前会话所有权不匹配。")
        return session

    def pending_project_information(self, binding) -> dict[str, Any]:
        """In-memory close-review marker; never a resource/recovery snapshot."""
        pending = self._information_edits.get(binding.project_id, {})
        return dict(pending) if pending.get("sessionGeneration") == binding.generation else {}

    def clear_project_information(self, binding) -> None:
        if self.pending_project_information(binding):
            self._information_edits.pop(binding.project_id, None)

    @Slot(str, str, int, str, str, str, result=bool)
    def stageProjectInformationEdit(self, projectId: str, sessionGeneration: str,
                                    expectedRevision: int, name: str, summary: str,
                                    notes: str) -> bool:
        try:
            session = self._information_session(projectId, sessionGeneration, retain_edit=True)
            # Retaining text is permitted during close/exit review. A changed
            # marker invalidates that review's signature; it publishes no data.
            with self.host.operation_scope(session.binding):
                self._information_edits[projectId] = {
                    "sessionGeneration": sessionGeneration, "revision": expectedRevision,
                    "name": name, "summary": summary, "notes": notes}
            return True
        except Exception as exc:
            self._set_error(str(exc))
            return False

    @Slot(str, str)
    def clearProjectInformationEdit(self, projectId: str, sessionGeneration: str) -> None:
        try:
            session = self._information_session(projectId, sessionGeneration, retain_edit=True)
            with self.host.operation_scope(session.binding):
                self.clear_project_information(session.binding)
        except Exception:
            # A late dismissal cannot clear a replacement session's edit.
            return

    @Slot(str, result="QVariantMap")
    def projectInformation(self, projectId: str) -> dict[str, Any]:
        """Read only this local owner's exact bound project display metadata."""
        try:
            session = self._information_session(projectId)
            with self.host.operation_scope(session.binding) as view:
                store = cast(ProjectStore, view)
                store._current()
                manifest = store.manifest
                if manifest["id"] != projectId:
                    raise ProjectError("project_identity_mismatch", "工程信息身份不匹配。")
                result = {"projectId": projectId,
                          "sessionGeneration": session.binding.generation,
                          "revision": manifest["revision"], "name": manifest["name"],
                          "summary": manifest.get("summary", ""), "notes": manifest.get("notes", "")}
            self._error = ""
            return result
        except Exception as exc:
            self._set_error(str(exc))
            return {}

    @Slot(str, str, int, str, str, str, result=bool)
    def saveProjectInformation(self, projectId: str, sessionGeneration: str,
                               expectedRevision: int, name: str, summary: str,
                               notes: str) -> bool:
        """Publish a descriptor-only edit with captured session/revision fencing."""
        try:
            session = self._information_session(projectId, sessionGeneration)
            with self.host.operation_scope(session.binding) as view:
                store = cast(ProjectStore, view)
                if store.manifest["id"] != projectId:
                    raise ProjectError("project_identity_mismatch", "工程信息身份不匹配。")
                store.update_information({"name": name, "summary": summary, "notes": notes},
                                         expected_revision=expectedRevision)
                self.host.refresh_project_information(session.binding)
                self.clear_project_information(session.binding)
            self._error = ""
            self.projectInformationChanged.emit(projectId)
            self.projectsChanged.emit()
            return True
        except Exception as exc:
            self._set_error(str(exc))
            return False

    @Slot(str, str, result=bool)
    def importProjectPackage(self, source: str, destination: str) -> bool:
        try:
            return self.packages.import_package(source, destination)
        except Exception as exc:
            self._set_error(str(exc))
            return False

    def _complete_pending(self) -> None:
        if self._stopped:
            return
        pending = self._pending
        if pending is None or not pending.done():
            return
        self._poll.stop()
        self._pending = None
        lease: ProjectOwnerLease | None = None
        admitted = False
        succeeded = False
        try:
            result = pending.result()
            if isinstance(result, _ActivatedProject):
                self._selected_id = result.target["project_id"]
                self._error = ""
                if self._shared is not None:
                    self._shared.refresh()
                succeeded = True
                return
            store, plan, lease = result
            if self._admission_guard is not None:
                allowed, message = self._admission_guard()
                if not allowed:
                    raise RuntimeError(message)
            root = Path(store.root)
            self._staged[root] = store
            try:
                session = self.host.open_project(root)
            finally:
                self._staged.pop(root, None)
            self._selected_id = session.binding.project_id
            self._leases[self._selected_id] = lease
            admitted = True
            self._plans[self._selected_id] = plan
            if isinstance(plan, ProjectDefaultPlan):
                resources = ProjectResourceWorkspace.for_project(self.host, session.binding)
                resources.initialize_default(plan)
            snapshot = getattr(plan, "film_design_snapshot", None)
            if snapshot is not None:
                self._plans_with_snapshot.add(self._selected_id)
            self._ensure_default_film_draft(session, snapshot=snapshot)
            self.desktop.open_project(root)
            self._error = ""
            succeeded = True
        except Exception as exc:
            if admitted:
                self.abort_startup()
            elif lease is not None:
                lease.close(clean=False)
            self._error = str(exc)
        finally:
            self._busy = False
            self.projectsChanged.emit()
            self.openFinished.emit(succeeded)

    def _ensure_default_film_draft(self, session, *, snapshot=None) -> None:
        project_id = session.binding.project_id
        if project_id in self._initialized_film_drafts:
            return
        if snapshot is None:
            snapshot = getattr(self._plans.get(project_id), "film_design_snapshot", None)
        if snapshot is None:
            return
        workspace = ProjectFilmWorkspace.for_project(self.host, session.binding)
        if not workspace.initialize_frozen_draft(snapshot):
            message = workspace.editor.snapshot().error or "无法载入默认工艺的冻结膜系内容。"
            raise ValueError(message)
        self._initialized_film_drafts.add(project_id)

    @Slot(str, result=bool)
    def activateProject(self, projectId: str) -> bool:
        if self._busy:
            self._set_error("正在打开另一个项目，请稍候。")
            return False
        session = next((item for item in self.host.sessions()
                        if item.binding.project_id == projectId), None)
        if session is None:
            if self._shared is not None:
                entry = next((row for row in self._shared.entries if row["projectId"] == projectId), None)
                if entry is not None:
                    return self.activateSharedProject(projectId, entry["sessionGeneration"])
            self._set_error("项目已关闭；请重新打开。")
            return False
        try:
            self._ensure_default_film_draft(session)
            self.desktop.open_project(session.binding.root)
            self._selected_id = projectId
            self._error = ""
            self.projectsChanged.emit()
            return True
        except Exception as exc:
            self._set_error(str(exc))
            return False

    @staticmethod
    def _activate_remote(client: Any, target: dict[str, str]) -> _ActivatedProject:
        from coat_sim.project_activation import activate_shared_project
        activate_shared_project(client, target)
        return _ActivatedProject(target)

    @Slot(str, str, result=bool)
    def activateSharedProject(self, projectId: str, sessionGeneration: str) -> bool:
        if self._stopped or self._busy or self._shared is None:
            return False
        entry = next((row for row in self._shared.entries if row["projectId"] == projectId
                      and row["sessionGeneration"] == sessionGeneration), None)
        if entry is None:
            self._set_error("原工程会话已更新，请刷新工程列表。")
            return False
        if not entry["available"]:
            self._set_error("当前无法联系原工程窗口，请稍后刷新重试；原会话和未保存的修改仍保留。")
            return False
        self._error = ""
        self._busy = True
        self.projectsChanged.emit()
        self._pending = self._executor.submit(
            self._activate_remote, self._shared.client, dict(entry["activationTarget"]))
        self._poll.start()
        return True

    @Slot(str, result=bool)
    def closeProject(self, projectId: str) -> bool:
        try:
            return self.project_closer.request(projectId)
        except Exception as exc:
            self._set_error(str(exc))
            return False

    @Slot(str, result=bool)
    def confirmCloseProject(self, projectId: str) -> bool:
        try:
            return self.project_closer.confirm(projectId)
        except Exception as exc:
            self._set_error(str(exc))
            return False

    @Slot(str)
    def cancelCloseProject(self, projectId: str) -> None:
        self.project_closer.cancel(projectId)

    @Slot(str, result=bool)
    def returnToSaveProject(self, projectId: str) -> bool:
        try:
            self.project_closer.return_to_save(projectId)
            self._set_error("工程仍然打开，所有未保存修改均已保留。请保存工程信息与相关模块的修改后再关闭工程。")
            return True
        except Exception as exc:
            self._set_error(str(exc))
            return False

    @Slot(str, result=list)
    def recoveryCandidates(self, projectId: str) -> list[dict[str, Any]]:
        if self._busy or self._stopped:
            return []
        try:
            return self.recovery.candidates(projectId)
        except Exception as exc:
            self._set_error(str(exc))
            return []

    @Slot(str, str, str, int, str, result=bool)
    def reviewRecovery(self, projectId: str, generation: str, section: str,
                       expectedRevision: int, recordId: str = "") -> bool:
        if self._busy or self._stopped:
            return False
        try:
            self.recovery.review(projectId, generation, section, expectedRevision, recordId)
            self.recoveryConfirmationRequested.emit(
                projectId, generation, section, expectedRevision, recordId)
            return True
        except Exception as exc:
            self._set_error(str(exc))
            return False

    @Slot(str, result=bool)
    def restoreRecovery(self, projectId: str) -> bool:
        try:
            result = self.recovery.restore(projectId)
            self._error = (
                "所选类别的异常退出内容已恢复，尚未保存；其他类别未改变，计算任务不会自动重启。" if result else
                "正在校验并恢复光学输入，请等待实际完成；当前内容和恢复候选仍保留。"
                if self.recovery.restore_pending(projectId) else self._error)
            self.projectsChanged.emit()
            return result
        except Exception as exc:
            self._set_error(str(exc))
            return False

    @Slot(str)
    def cancelRecovery(self, projectId: str) -> None:
        self.recovery.cancel_review(projectId)

    @Slot(str, str, str, int, result=bool)
    def archiveReviewedRecovery(self, projectId: str, generation: str, section: str,
                                expectedRevision: int) -> bool:
        if self._busy or self._stopped:
            return False
        try:
            self.recovery.archive_reviewed(projectId, generation, section, expectedRevision)
            self._error = "已将所选类别移入可恢复的已审阅归档；同一会话的其他类别未改变。"
            self.projectsChanged.emit()
            return True
        except Exception as exc:
            self._set_error(str(exc))
            return False

    @Slot()
    def refreshProjects(self) -> None:
        if self._shared is not None:
            self._shared.refresh()
        self.projectsChanged.emit()

    def open_imported_project(self, project_file: Path) -> None:
        """A restored package follows the same independent runtime open path."""
        from .project_runtime_client import open_project_runtime
        self._busy = True
        self._external_pending = self._executor.submit(open_project_runtime, project_file)
        self._external_poll.start()

    def _complete_external_open(self) -> None:
        if self._external_pending is None or not self._external_pending.done():
            return
        pending, self._external_pending = self._external_pending, None
        self._external_poll.stop()
        self._busy = False
        try:
            pending.result()
            self._error = "工程包已恢复并在独立工程窗口打开。"
        except Exception as exc:
            self._error = str(exc)
        self.projectsChanged.emit()

    def release_lease(self, lease: ProjectOwnerLease, checkpoint: Future | None = None,
                      *, clean: bool = True) -> None:
        """Track completion and errors even after the session leaves the host."""
        released: Future = Future()
        self._pending_lease_releases.append(released)
        def finish(_future=None) -> None:
            try:
                lease.close(clean=clean)
                released.set_result(None)
            except Exception as exc:
                released.set_exception(exc)
        if checkpoint is not None and not checkpoint.done():
            checkpoint.add_done_callback(finish)
        else:
            finish()

    def settled_for_exit(self) -> bool:
        if self._busy or self._pending is not None:
            return False
        if any(route.module_key == "project_resources" and (getattr(view[1], "assetApplying", getattr(view[1], "_transfer_applying", False))
                or getattr(view[1], "materialModalPending", False))
               for route, view in self.desktop._views.items()):
            return False
        pending = [self.packages.pending, *self.recovery.pending.values(),
                   *self._pending_lease_releases, *self._draining_transactions, self._external_pending]
        if any(value is not None and not value.done() for value in pending):
            return False
        for release in self._pending_lease_releases:
            release.result()  # A release failure is a visible blocker, not success.
        return True

    def abort_startup(self) -> None:
        """Unsuccessful first presentation cannot mark recovery as clean."""
        self._unclean_stop = True
        for session in self.host.sessions():
            views = {route: self.host.window_for(route)
                     for route in self.host.routes(session.binding.project_id)}
            def close_windows(_binding, routes, views=views):
                for route in routes:
                    window = views.get(route)
                    if window is not None:
                        window.setProperty("projectCloseAuthorized", True)
                        window.setProperty("allowClose", True)
                        window.setProperty("closeAuthorized", True)
                        window.close()
                return True
            with self.desktop.authorized_project_close(session.binding):
                self.host.close_project(session.binding, prepare_close=close_windows)
            lease = self._leases.pop(session.binding.project_id, None)
            if lease is not None:
                self.release_lease(lease, self.recovery.pending.pop(session.binding.project_id, None), clean=False)

    def stop(self) -> None:
        self._stopped = True
        self._information_edits.clear()
        self._poll.stop()
        if self._shared is not None:
            self._shared.stop()
        self.project_closer.stop()
        self.recovery.stop()
        package = self.packages.pending
        if package is not None and not package.done() and not package.cancel():
            self._draining_transactions.append(package)
        self.packages.stop()
        self._external_poll.stop()
        if self._external_pending is not None:
            self._draining_transactions.append(self._external_pending)
            self._external_pending = None
        if self._pending is not None:
            pending = self._pending
            if not pending.cancel():
                drained: Future = Future()
                self._pending_lease_releases.append(drained)
                def release_unadmitted(future: Future) -> None:
                    try:
                        result = future.result()
                        if not isinstance(result, _ActivatedProject):
                            _store, _plan, lease = result
                            lease.close(clean=False)
                        drained.set_result(None)
                    except Exception as exc:
                        # A loader failure has already released its lease. A
                        # late successful result's release error stays visible.
                        if future.exception() is exc:
                            drained.set_result(None)
                        else:
                            drained.set_exception(exc)
                pending.add_done_callback(release_unadmitted)
            self._pending = None
        for project_id, lease in self._leases.items():
            self.release_lease(lease, self.recovery.pending.get(project_id), clean=not self._unclean_stop)
        self._leases.clear()
        self._busy = False
        self.projectsChanged.emit()
        self._executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _release_unadmitted(pending: Future) -> None:
        if pending.cancelled():
            return
        try:
            result = pending.result()
            if not isinstance(result, _ActivatedProject):
                _store, _plan, lease = result
                lease.close(clean=False)
        except Exception:
            # A failed worker releases its lease before propagating the error.
            return
