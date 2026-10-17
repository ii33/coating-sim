"""GUI ownership adapter for two-phase, cancellable application shutdown."""
from __future__ import annotations

from contextlib import ExitStack
from typing import Any

from PySide6.QtCore import QObject, QTimer
from shiboken6 import isValid

from .project_exit_client import Decision, ProjectExitClient
from .project_owner_access import project_owner


class ProjectApplicationLifecycle(QObject):
    def __init__(self, bridge: Any, generation: str):
        super().__init__(bridge)
        self.bridge = bridge
        self._decision: bool | None = None
        self._asked = False
        self._freeze: ExitStack | None = None
        self._windows: list[tuple[Any, bool]] = []
        self._workspaces: list[Any] = []
        self._signature: tuple = ()
        self._information_review_signature: tuple = ()
        self._legacy_dirty: list[Any] = []
        self._close_flags: list[tuple[Any, Any, Any, Any]] = []
        self.gate = ProjectExitClient(generation, prepare=self._prepare,
                                      commit=self._commit, cancel=self._cancel,
                                      validate_ready=lambda: self._draft_signature() == self._signature)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.gate.expire)
        self._timer.start()

    @property
    def active(self) -> bool:
        return self.gate.active

    def dispatch(self, operation: str, arguments: dict) -> dict:
        return self.gate.dispatch(operation, arguments)

    def decide(self, accepted: bool) -> None:
        self._decision = accepted
        self.bridge._shutdown_confirmation_pending = False

    def _resources(self) -> list[Any]:
        launcher = project_owner(self.bridge)
        if launcher is None:
            return []
        return [value for session in launcher.host.sessions()
                for key in ("resource_workspace", "compute_workspace")
                if (value := launcher.host.resource_for(session.binding, key)) is not None]

    def _project_information_signature(self) -> tuple:
        owner = project_owner(self.bridge)
        if owner is None:
            return ()
        pending: Any = getattr(owner, "pending_project_information", lambda _binding: {})
        return tuple((session.binding, repr(value)) for session in owner.host.sessions()
                     if (value := pending(session.binding)))

    def _draft_signature(self) -> tuple:
        launcher = project_owner(self.bridge)
        rows: list[Any] = []
        if launcher is not None:
            for session in launcher.host.sessions():
                pending_information: Any = getattr(launcher, "pending_project_information", lambda _binding: {})(session.binding)
                if pending_information:
                    rows.append((session.binding, "project_information", repr(pending_information)))
                film = launcher.host.resource_for(session.binding, "film_workspace")
                editor = getattr(film, "_editor", None)
                if editor is not None:
                    rows.append((session.binding, repr(editor.snapshot())))
        for handle in self.bridge._windows.values():
            rows.append((handle.instance_id if hasattr(handle, "instance_id") else id(handle),
                         repr(getattr(handle.bridge, "dirty", False)),
                         repr(getattr(handle.title_bridge, "documentDirty", False))))
        for workspace in self._resources():
            signature = getattr(workspace, "draft_signature", None)
            snapshot = getattr(workspace, "snapshot", None)
            if callable(signature):
                rows.append(repr(signature()))
            elif callable(snapshot):
                rows.append(repr(snapshot()))
        return tuple(rows)

    def _prepare(self) -> Decision:
        bridge = self.bridge
        if self._decision is False:
            return "declined"
        if getattr(bridge, "_project_starting", False):
            return "pending"
        if project_owner(bridge) is not None and project_owner(bridge)._busy:
            return "pending"
        if self._freeze is not None:
            return "ready"
        if any(handle.key == "global_resources" and (not handle.bridge.assetCancellable
                    or handle.bridge.materialModalPending)
               for handle in bridge._windows.values()):
            return "pending"
        owner = project_owner(bridge)
        closer = getattr(owner, "project_closer", None)
        if getattr(closer, "_active_review", None) is not None:
            return "pending"
        settled = getattr(owner, "settled_for_exit", None)
        if callable(settled) and not settled():
            return "pending"
        uncommitted = bridge._uncommitted_project_view_draft()
        dirty = bool(bridge._dirty_project_sessions()) or uncommitted
        if owner is not None:
            pending_information: Any = getattr(owner, "pending_project_information", lambda _binding: {})
            dirty = any(pending_information(session.binding) for session in owner.host.sessions()) or dirty
        for workspace in self._resources():
            dirty = bool(getattr(workspace, "has_dirty_draft", False)) or dirty
            preflight = getattr(workspace, "close_preflight", None)
            if callable(preflight):
                dirty = bool(preflight()) or dirty
            jobs = getattr(workspace, "active_jobs", None)
            if callable(jobs) and jobs():
                # Local render workers must finish or be cancelled in their UI.
                # Service-owned jobs remain independently managed.
                local = [job for job in jobs() if not job.get("service_owned", False)]
                if local:
                    bridge.launchError.emit("本地渲染仍在运行，请先等待完成或在对应窗口取消。")
                    return "pending"
        self._legacy_dirty = [handle for handle in bridge._windows.values()
                              if ((handle.key == "film_design" and bridge._film_is_dirty(handle))
                                  or bool(getattr(handle.bridge, "dirty", False))
                                  or bool(getattr(handle.title_bridge, "documentDirty", False)))]
        dirty = dirty or bool(self._legacy_dirty)
        information_signature = self._project_information_signature()
        if (self._decision is True and self._asked
                and information_signature != self._information_review_signature):
            # Review leaves text editable. Approval of an earlier information
            # buffer cannot silently discard newer text before the ready freeze.
            self._decision, self._asked = None, False
        if dirty and self._decision is not True:
            if not self._asked:
                self._asked = True
                self._information_review_signature = information_signature
                bridge._shutdown_confirmation_pending = True
                self.activate_confirmation_owner()
                bridge.shutdownConfirmationRequested.emit()
            return "pending"
        self._freeze = ExitStack()
        try:
            launcher = project_owner(bridge)
            if launcher is not None:
                self._freeze.enter_context(launcher.host.exit_admission_freeze())
                for view in launcher.desktop._views.values():
                    self._freeze_window(view[0])
            for handle in bridge._windows.values():
                self._freeze_window(handle.window)
            if bridge._launcher_window is not None:
                self._freeze_window(bridge._launcher_window)
            management_window = getattr(bridge, "_project_management_window", None)
            if management_window is not None:
                self._freeze_window(management_window)
            for workspace in self._resources():
                freeze = getattr(workspace, "freeze_editing", None)
                if callable(freeze):
                    freeze(True)
                    self._workspaces.append(workspace)
            self._signature = self._draft_signature()
        except Exception:
            self._cancel()
            raise
        return "ready"

    def confirmation_owner(self) -> Any:
        """Find an existing draft view; do not construct or close any window."""
        owner = project_owner(self.bridge)
        if owner is not None:
            dirty_modules: set[tuple[Any, str]] = set()
            for session in owner.host.sessions():
                binding = session.binding
                film = owner.host.resource_for(binding, "film_workspace")
                editor = getattr(film, "_editor", None)
                if editor is not None and editor.snapshot().dirty:
                    dirty_modules.add((binding, "film_design"))
                for key in ("resource_workspace", "compute_workspace"):
                    workspace = owner.host.resource_for(binding, key)
                    summary = getattr(workspace, "dirty_summary", None)
                    if not callable(summary):
                        summary = getattr(workspace, "close_preflight", None)
                    if callable(summary):
                        for row in summary():
                            module = row["module_key"]
                            dirty_modules.add((binding, "film_appearance" if module == "appearance" else module))
            for route, view in owner.desktop._views.items():
                if (route.binding, route.module_key) in dirty_modules:
                    return view[0]
        if self._legacy_dirty:
            return self._legacy_dirty[0].window
        return self.bridge._launcher_window

    def activate_confirmation_owner(self) -> Any:
        window = self.confirmation_owner()
        if window is not None:
            self.bridge._activate_window(window)
        return window

    def _freeze_window(self, window: Any) -> None:
        content = getattr(window, "contentItem", None)
        item = content() if callable(content) else window
        if (hasattr(item, "isEnabled") and hasattr(item, "setEnabled")
                and all(item is not previous for previous, _enabled in self._windows)):
            self._windows.append((item, item.isEnabled()))
            item.setEnabled(False)

    def _commit(self) -> bool:
        if self._draft_signature() != self._signature:
            self.bridge.launchError.emit("退出准备后当前内容发生变化，请重新确认。")
            return False
        for handle in self._legacy_dirty:
            self._close_flags.append((handle.window, handle.window.property("allowClose"),
                                      handle.window.property("closeAuthorized"),
                                      getattr(handle.window, "_force_close", False)))
            handle.window.setProperty("allowClose", True)
            handle.window.setProperty("closeAuthorized", True)
            handle.window._force_close = True
        self.bridge._shutdown_confirmation_pending = False
        self.bridge._project_shutdown_approved = True
        self.bridge.shutdown()
        return bool(self.bridge._closing or getattr(self.bridge, "_runtime_shutdown_requested", False))

    def _cancel(self) -> None:
        for workspace in self._resources():
            resume = getattr(workspace, "resume_close_review", None)
            if callable(resume):
                resume()
        for window, allow, authorized, forced in self._close_flags:
            if isValid(window):
                window.setProperty("allowClose", allow)
                window.setProperty("closeAuthorized", authorized)
                window._force_close = forced
        self._close_flags.clear()
        for workspace in reversed(self._workspaces):
            workspace.freeze_editing(False)
        self._workspaces.clear()
        for window, enabled in self._windows:
            if isValid(window):
                window.setEnabled(enabled)
        self._windows.clear()
        if self._freeze is not None:
            self._freeze.close()
            self._freeze = None
        self._decision, self._asked = None, False
        self._information_review_signature = ()
        self.bridge._shutdown_confirmation_pending = False
        self.bridge._project_shutdown_approved = False
