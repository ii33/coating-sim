"""Close one explicit project while preserving unrelated project sessions."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer

from coat_sim.core.project_dependencies import ProjectError


class ProjectCloseCoordinator:
    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._reviews: dict[str, tuple[str, str]] = {}
        self._last_window_requests: dict[str, Any] = {}
        self._active_review: str | None = None
        self._review_queue: list[str] = []

    def _show_next_review(self) -> None:
        if self.adapter._stopped or self._active_review is not None:
            return
        while self._review_queue:
            identity = self._review_queue.pop(0)
            review = self._reviews.get(identity)
            if review is None:
                continue
            try:
                session = self._session(identity)
            except ProjectError:
                self._reviews.pop(identity, None)
                continue
            if session.binding.generation != review[0]:
                self._reviews.pop(identity, None)
                continue
            self._active_review = identity
            self.adapter.projectCloseConfirmationRequested.emit(identity)
            return

    def _finish_review(self, project_id: str) -> None:
        self._review_queue = [identity for identity in self._review_queue if identity != project_id]
        if self._active_review == project_id:
            self._active_review = None
        QTimer.singleShot(0, self._show_next_review)

    def is_pending(self, binding: Any) -> bool:
        review = self._reviews.get(binding.project_id)
        route = self._last_window_requests.get(binding.project_id)
        return bool((review is not None and review[0] == binding.generation)
                    or (route is not None and route.binding == binding))

    def request_last_window(self, route: Any, window: Any) -> None:
        if self.is_pending(route.binding):
            return
        self._last_window_requests[route.binding.project_id] = route
        def request() -> None:
            identity = route.binding.project_id
            if self._last_window_requests.get(identity) != route:
                return
            try:
                session = self._session(identity)
                if (session.binding != route.binding or self.adapter._stopped
                        or self.adapter.host.window_for(route) is not window):
                    self._last_window_requests.pop(identity, None)
                    return
                self.request(identity)
            except Exception as exc:
                self._last_window_requests.pop(identity, None)
                self.adapter._set_error(str(exc))
        QTimer.singleShot(0, request)

    def _session(self, project_id: str):
        session = next((row for row in self.adapter.host.sessions()
                        if row.binding.project_id == project_id), None)
        if session is None:
            raise ProjectError("project_closed", "项目已经关闭。")
        return session

    def _preflight(self, session: Any, *, allow_active_jobs: bool = False) -> tuple[bool, str]:
        if getattr(self.adapter, "_busy", False):
            raise ProjectError("project_busy", "工程请求仍在处理，请稍后再关闭。")
        host = self.adapter.host
        values: list[Any] = []
        pending_information: Any = getattr(self.adapter, "pending_project_information", lambda _binding: {})(session.binding)
        pending_bbs: Any = getattr(self.adapter, "pending_project_bbs", lambda _binding: {})(session.binding)
        dirty = bool(pending_information or pending_bbs)
        if pending_bbs:
            values.append(("project_bbs", pending_bbs))
        if pending_information:
            values.append(("project_information", pending_information))
        for route, view in tuple(self.adapter.desktop._views.items()):
            if route.binding != session.binding:
                continue
            if route.module_key == "project_resources" and getattr(view[1], "assetApplying", getattr(view[1], "_transfer_applying", False)):
                raise ProjectError("resource_transfer_busy", "工程资源正在提交，请等待明确结果后再关闭工程。")
            if route.module_key == "project_resources" and getattr(view[1], "materialModalPending", False):
                raise ProjectError("pending_editor", "请先完成或取消材料数据组输入，再关闭工程。")
            commit = getattr(view[0], "commitExpressionDraft", None)
            if callable(commit) and not commit(True):
                raise ProjectError("pending_editor", "请先处理尚未提交的表达式输入。")
        film = host.resource_for(session.binding, "film_workspace")
        if film is not None and film._editor is not None:
            value = film._editor.snapshot()
            dirty = value.dirty or bool(getattr(film._editor, "_pending_information_edit", None)) or dirty
            values.append(repr(value))
        resources = host.resource_for(session.binding, "resource_workspace")
        if resources is not None:
            dirty = bool(resources.close_preflight()) or dirty
            values.append(repr(resources.snapshot()))
        compute = host.resource_for(session.binding, "compute_workspace")
        if compute is not None:
            dirty = bool(compute.close_preflight()) or dirty
            dirty = bool(getattr(compute, "has_dirty_draft", False)) or dirty
            if not allow_active_jobs and any(not row["service_owned"] for row in compute.active_jobs()):
                raise ProjectError("local_job_running", "请先等待此工程的本地计算结束或取消，再关闭工程。")
            values.append(compute.draft_signature())
        return dirty, repr(values)

    def request(self, project_id: str) -> bool:
        session = self._session(project_id)
        review = self._reviews.get(project_id)
        if review is not None and review[0] == session.binding.generation:
            return False
        dirty, signature = self._preflight(session)
        if dirty:
            self._reviews[project_id] = (session.binding.generation, signature)
            self._review_queue.append(project_id)
            self._show_next_review()
            return False
        return self._close(session)

    def confirm(self, project_id: str) -> bool:
        expected = self._reviews.pop(project_id)
        try:
            session = self._session(project_id)
            _dirty, signature = self._preflight(session)
            if expected != (session.binding.generation, signature):
                raise ProjectError("stale_close", "确认期间工程内容已变化，请重新关闭工程。")
            return self._close(session)
        finally:
            self._last_window_requests.pop(project_id, None)
            self._finish_review(project_id)

    def cancel(self, project_id: str) -> None:
        try:
            session = self._session(project_id)
            resources = self.adapter.host.resource_for(session.binding, "resource_workspace")
            if resources is not None:
                resources.resume_close_review()
        except ProjectError:
            pass
        self._reviews.pop(project_id, None)
        self._last_window_requests.pop(project_id, None)
        self._finish_review(project_id)

    def return_to_save(self, project_id: str) -> None:
        session = self._session(project_id)
        self.cancel(project_id)
        pending_information: Any = getattr(self.adapter, "pending_project_information", lambda _binding: {})(session.binding)
        if pending_information:
            self.adapter.desktop.managementRequested.emit(project_id)
            return
        # This is navigation, not a partial Save All. Retained sibling drafts
        # must stay intact even if their own presentations have been closed.
        modules = []
        if getattr(self.adapter, "pending_project_bbs", lambda _binding: {})(session.binding):
            modules.append("project_bbs")
        film = self.adapter.host.resource_for(session.binding, "film_workspace")
        if film is not None and film._editor is not None and film._editor.snapshot().dirty:
            modules.append("film_design")
        resources = self.adapter.host.resource_for(session.binding, "resource_workspace")
        if resources is not None:
            modules.extend(row["module_key"] for row in resources.dirty_summary())
        compute = self.adapter.host.resource_for(session.binding, "compute_workspace")
        if compute is not None:
            modules.extend("film_appearance" if row["module_key"] == "appearance" else row["module_key"]
                           for row in compute.close_preflight())
            if compute.has_dirty_deposition_draft:
                modules.append("main")
        existing = {route.module_key: self.adapter.host.window_for(route)
                    for route in self.adapter.host.routes(project_id)}
        for module in modules:
            window = existing.get(module)
            if window is not None:
                window.show()
                window.raise_()
                window.requestActivate()
                return
        from coat_sim.core.project_film_session import ProjectFilmWorkspace
        workspace = ProjectFilmWorkspace.for_project(self.adapter.host, session.binding)
        self.adapter.desktop.open_project_module(workspace, modules[0] if modules else "film_design")

    def stop(self) -> None:
        self._reviews.clear()
        self._last_window_requests.clear()
        self._review_queue.clear()
        self._active_review = None

    def _close(self, session: Any) -> bool:
        host = self.adapter.host
        resources = host.resource_for(session.binding, "resource_workspace")
        views = {route: host.window_for(route) for route in host.routes(session.binding.project_id)}
        def close_windows(_binding, routes) -> bool:
            for route in routes:
                window = views[route]
                if window is not None:
                    window.setProperty("projectCloseAuthorized", True)
                    window.setProperty("allowClose", True)
                    window.setProperty("closeAuthorized", True)
                    if not window.close():
                        return False
            return True
        with self.adapter.desktop.authorized_project_close(session.binding):
            if not host.close_project(session.binding, prepare_close=close_windows):
                return False
        self._last_window_requests.pop(session.binding.project_id, None)
        self._reviews.pop(session.binding.project_id, None)
        if resources is not None:
            resources.dispose()
        identity = session.binding.project_id
        clear_information = getattr(self.adapter, "clear_project_information", None)
        if callable(clear_information):
            clear_information(session.binding)
        clear_bbs = getattr(self.adapter, "clear_project_bbs", None)
        if callable(clear_bbs):
            clear_bbs(session.binding)
        lease = self.adapter._leases.pop(identity)
        pending = self.adapter.recovery.pending.pop(identity, None)
        release = getattr(self.adapter, "release_lease", None)
        if callable(release):
            release(lease, pending)
        elif pending is not None and not pending.done():
            pending.add_done_callback(lambda _future: lease.close())
        else:
            lease.close()
        self.adapter._plans.pop(identity, None)
        self.adapter._plans_with_snapshot.discard(identity)
        self.adapter._initialized_film_drafts.discard(identity)
        self.adapter.recovery.cancel_review(identity, generation=session.binding.generation)
        if self.adapter._selected_id == identity:
            remaining = host.sessions()
            self.adapter._selected_id = remaining[0].binding.project_id if remaining else ""
        self.adapter.projectsChanged.emit()
        return True
