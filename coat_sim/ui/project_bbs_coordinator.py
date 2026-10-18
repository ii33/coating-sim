"""Bound, asynchronous engineering BBS and session-only composition buffers."""
from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

from coat_sim.core.project_bbs import ProjectBbsService
from coat_sim.core.project_store import ProjectError


class ProjectBbsCoordinator(QObject):
    completed = Signal(str, str, str, dict, str)
    stateChanged = Signal()

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.pending: Future[dict] | None = None
        self._request: tuple[str, Any, str, dict] | None = None
        self._drafts: dict[tuple[str, str], dict] = {}
        self._bindings: dict[tuple[str, str], Any] = {}
        self._error = ""
        self.session_snapshot: list[dict] = []
        from .project_bbs_attachments import ProjectBbsAttachmentBridge
        self._attachments = ProjectBbsAttachmentBridge(self)
        self.poll = QTimer(self)
        self.poll.setInterval(20)
        self.poll.timeout.connect(self.complete)

    @Property(QObject, constant=True)
    def attachmentPreview(self):
        return self._attachments

    @Slot(str, result=str)
    def renderMarkdown(self, text: str) -> str:
        from .project_bbs_markdown import render_markdown
        return render_markdown(text)

    @Property(str, notify=stateChanged)
    def error(self):
        return self._error

    @Property(bool, notify=stateChanged)
    def busy(self):
        return self.pending is not None

    def pending_draft(self, binding):
        return deepcopy(self._drafts.get((binding.project_id, binding.generation), {}))

    def clear_binding(self, binding):
        self._drafts.pop((binding.project_id, binding.generation), None)

    def forget_binding(self, binding):
        self.clear_binding(binding)
        self._bindings.pop((binding.project_id, binding.generation), None)
        self._attachments.forget(binding)

    @Slot(str, str, result="QVariantMap")
    def draft(self, project_id, generation):
        key = (project_id, generation)
        if key not in self._bindings:
            try:
                session = self.owner._information_session(project_id, generation)
                self._bindings[key] = session.binding
            except Exception as exc:
                self._error = str(exc)
                self.stateChanged.emit()
                return {}
        return deepcopy(self._drafts.get(key, {}))

    @Slot(str, str, "QVariantMap", result=bool)
    def stageDraft(self, project_id, generation, value):
        try:
            key = (project_id, generation)
            binding = self._bindings.get(key)
            if binding is None:
                # First entry is admitted before any worker starts. Later text
                # retention never waits on the worker's host registry lock.
                if self.owner._busy:
                    raise ProjectError("project_busy", "工程正在处理请求。")
                binding = self.owner._information_session(project_id, generation, retain_edit=True).binding
                self._bindings[key] = binding
            lease = self.owner._leases.get(project_id)
            if self.owner._stopped or lease is None or lease.root != binding.root:
                raise ProjectError("project_closed", "工程会话已关闭。")
            lease._require_open()
            # Preserve oversized text exactly; publication enforces the 64 KiB
            # format limit. Rejecting retention would silently lose user input.
            if value:
                self._drafts[key] = deepcopy(value)
            else:
                self._drafts.pop(key, None)
            return True
        except Exception as exc:
            self._error = str(exc)
            self.stateChanged.emit()
            return False

    @Slot(str, str, str, "QVariantMap", result=str)
    def request(self, project_id, generation, operation, payload):
        try:
            if self.pending is not None:
                raise ProjectError("project_busy", "工程 BBS 正在处理请求，请稍候。")
            session = self.owner._information_session(project_id, generation)
            if self.owner._admission_guard is not None:
                allowed, message = self.owner._admission_guard()
                if not allowed:
                    raise ProjectError("project_frozen", message)
            if operation not in {"list", "read", "history", "create", "reply", "update", "archive",
                                 "attachment_import", "attachment_preview", "attachment_export"}:
                raise ProjectError("bbs_invalid_arguments", "未知工程 BBS 操作。")
            token = uuid4().hex
            self._request = (token, session.binding, operation, self.pending_draft(session.binding))
            self._error = ""
            self._capture_sessions()
            self.owner._busy = True
            # Notify before the worker takes the host lock; views can render
            # their cached state without waiting for a disk operation.
            self.owner.projectsChanged.emit()
            self.pending = self.owner._executor.submit(self._execute, session.binding, operation, deepcopy(payload))
            self.poll.start()
            self.stateChanged.emit()
            return token
        except Exception as exc:
            self._error = str(exc)
            self.stateChanged.emit()
            return ""

    def _capture_sessions(self):
        self.session_snapshot = [
            {"project_id": session.binding.project_id, "root": str(session.binding.root),
             "project_file": str(self.owner.host.store_for(session.binding).project_file),
             "generation": session.binding.generation, "name": session.name}
            for session in self.owner.host.sessions()]

    def execute_owned_async(self, binding, operation: str, payload: dict) -> Future[dict]:
        """Use the same serial owner for trusted assistant IPC publication."""
        if self.pending is not None or self.owner._busy or self.owner._stopped:
            raise ProjectError("project_busy", "工程 BBS 正在处理请求，请稍候。")
        self.owner._information_session(binding.project_id, binding.generation)
        self._request = (uuid4().hex, binding, "external", {})
        self._error = ""
        self._capture_sessions()
        self.owner._busy = True
        self.owner.projectsChanged.emit()
        self.pending = self.owner._executor.submit(self._execute_owned, binding, operation, deepcopy(payload))
        self.poll.start()
        self.stateChanged.emit()
        return self.pending

    def _execute_owned(self, binding, operation, payload):
        from coat_sim.project_management import ProjectManagementService
        from .control import UiControlError
        try:
            with self.owner.host.operation_scope(binding):
                store = self.owner.host.store_for(binding)
                lease = self.owner._leases.get(binding.project_id)
                if lease is None:
                    raise ProjectError("project_closed", "工程可写所有权已结束。")
                lease._require_open()
                service = ProjectManagementService(authorized_roots=[binding.root])
                return service.execute_owned(operation, payload, store=store,
                    project_id=binding.project_id, actor="assistant")
        except ProjectError as exc:
            raise UiControlError(exc.code, str(exc)) from exc

    def _execute(self, binding, operation, payload):
        with self.owner.host.operation_scope(binding):
            store = self.owner.host.store_for(binding)
            lease = self.owner._leases.get(binding.project_id)
            if lease is None:
                raise ProjectError("project_closed", "工程可写所有权已结束。")
            lease._require_open()
            if store.manifest["id"] != binding.project_id or lease.root != binding.root:
                raise ProjectError("project_identity_mismatch", "工程 BBS 身份不匹配。")
            if operation.startswith("attachment_"):
                return self._attachments.execute(store, binding, operation, payload)
            service = ProjectBbsService(store, actor="user")
            if operation == "list":
                return service.list_posts(post_type=payload.get("post_type") or None,
                    parent_id=payload.get("parent_id") or None,
                    include_archived=payload.get("include_archived", False),
                    query=payload.get("query", ""), offset=payload.get("offset", 0), limit=20)
            if operation == "read":
                return service.read_post(payload["post_id"])
            if operation == "history":
                return service.history(payload["post_id"], offset=payload.get("offset", 0), limit=10)
            if operation == "create":
                return service.create_post(payload["post_type"], payload["title"], payload["body"],
                    body_format=payload.get("body_format", "plain"), attachments=payload.get("attachments"))
            expected = payload["expected_post_revision"]
            if operation == "archive":
                return service.archive_post(payload["post_id"], payload["archived"], expected_post_revision=expected)
            function = service.reply if operation == "reply" else service.update_post
            return function(payload["post_id"], payload["title"], payload["body"], expected_post_revision=expected,
                body_format=payload.get("body_format", "plain" if operation == "reply" else None),
                attachments=payload.get("attachments"))

    def complete(self):
        if self.pending is None or not self.pending.done():
            return
        future, self.pending = self.pending, None
        assert self._request is not None
        token, binding, operation, submitted_draft = self._request
        self._request = None
        self.poll.stop()
        self.owner._busy = False
        result, error = {}, ""
        try:
            result = future.result()
            # The captured binding remains authoritative even when the user
            # selected another project while this worker was finishing.
            self.owner._information_session(binding.project_id, binding.generation, retain_edit=True)
            if operation in {"create", "reply", "update"} and self.pending_draft(binding) == submitted_draft:
                self.clear_binding(binding)
        except Exception as exc:
            error = str(exc)
        self._error = error
        self.stateChanged.emit()
        self.owner.projectsChanged.emit()
        self.completed.emit(token, binding.project_id, binding.generation, result, error)

    def stop(self):
        self.poll.stop()
        pending = self.pending
        if pending is not None:
            pending.cancel()
        self._drafts.clear()
        self._bindings.clear()
        self._attachments.clear()
        return pending
