"""One detached GUI runtime per project, independent of launcher lifetime."""
from __future__ import annotations

import argparse
from pathlib import Path
import time
from typing import Any, cast

from PySide6.QtCore import QMetaObject, QObject, QTimer, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication

from coat_sim.assistant_process import LIFECYCLE_OPERATIONS
from coat_sim.version import BUILD_IDENTITY
from coat_sim.core.project_store import ProjectStore
from .control import OPERATIONS, UiControlServer, WindowRegistry
from .icons import window_icon
from .launcher import LauncherBridge, _ProjectQuitGuard
from .project_application_client import ProjectApplicationClient
from .project_application_lifecycle import ProjectApplicationLifecycle
from .project_runtime_owner import ProjectRuntimeOwner
from .ready_channel import report_startup_event
from .recent_projects import RecentProjects
from .recent_project_tasks import RecentProjectTasks
from .settings import apply_font_settings, configure_qt_dpi_scale
from .theme_service import configure_theme_from_settings


class ProjectRuntime(QObject):
    """Compose existing owner, routing and lifecycle adapters without a picker."""

    def __init__(self, app: QApplication, engine: QQmlApplicationEngine, project_file: Path | None,
                 *, create_root: Path | None = None, create_name: str | None = None,
                 create_summary: str | None = None, create_notes: str | None = None):
        super().__init__(engine)
        self.app, self.engine, self.project_file = app, engine, project_file
        self.create_root, self.create_name = create_root, create_name
        self.create_summary, self.create_notes = create_summary, create_notes
        self.owner = ProjectRuntimeOwner(self)
        self.bridge = LauncherBridge(engine, with_launcher=False)
        self.bridge.project_runtime = self.owner
        self.bridge._project_starting = True
        self.bridge._runtime_shutdown_callback = self.request_shutdown
        self.bridge._project_activation_callback = self._record_history
        self._history_worker = RecentProjectTasks(RecentProjects().worker_factory())
        self._history_pending: dict[str, Any] = {}
        self._history_last: dict[str, float] = {}
        self._history_draining_started: float | None = None
        self.bridge.control_registry = WindowRegistry(self.bridge)
        self.application = ProjectApplicationClient(
            self.bridge, self.bridge.control_registry, role="project_host")
        self.bridge.project_application = self.application
        self.bridge.project_lifecycle = ProjectApplicationLifecycle(
            self.bridge, self.application.generation)
        self.server = UiControlServer(self.application.dispatch,
            operations=OPERATIONS | LIFECYCLE_OPERATIONS, role="project_host", window_type="project_runtime")
        self.bridge.control_server = self.server
        self._quit_guard = _ProjectQuitGuard(self.bridge)
        app.installEventFilter(self._quit_guard)
        app.aboutToQuit.connect(self.bridge.shutdown)
        self._started_at = time.monotonic()
        self._requested = False
        self._finished = False
        self._failed = False
        self._endpoint: dict[str, Any] | None = None
        self._completion: bool | None = None
        self._startup_windows: list[tuple[Any, Any, bool, bool]] = []
        self.owner.openFinished.connect(self._open_finished)
        self.owner.projectCloseConfirmationRequested.connect(self._present_review)
        self.bridge.shutdownConfirmationRequested.connect(self._present_shutdown)
        self.owner.projectsChanged.connect(self._owner_changed)
        self.owner.desktop.managementRequested.connect(self._present_management)
        engine.rootContext().setContextProperty("runtimeProjects", self.owner)
        engine.rootContext().setContextProperty("runtimeBridge", self.bridge)
        engine.load(QUrl.fromLocalFile(str(Path(__file__).parent / "qml/ProjectRuntimeDialogs.qml")))
        if not engine.rootObjects():
            raise RuntimeError("工程确认界面未能加载。")
        self.dialog_window = cast(Any, engine.rootObjects()[0])
        # This existing management/review surface also joins global-exit freeze.
        cast(Any, self.bridge)._project_management_window = self.dialog_window
        self.dialog_window.setIcon(window_icon("launcher"))
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._endpoint = self.server.start()
        self.application.start(self._endpoint)
        self._timer.start()

    def _record_history(self) -> None:
        for session in self.owner.host.sessions():
            store = cast(ProjectStore, self.owner.host.store_for(session.binding))
            descriptor = str(store.project_file)
            if descriptor in self._history_pending or time.monotonic() - self._history_last.get(descriptor, 0) < 1:
                continue  # Startup's immediate authenticated activation is one recent event.
            self._history_last[descriptor] = time.monotonic()
            self._history_pending[descriptor] = self._history_worker.submit(
                "record_only", store.project_file, session.name)

    def _poll_history(self) -> bool:
        for descriptor, pending in tuple(self._history_pending.items()):
            if pending.done():
                self._history_pending.pop(descriptor)
                try:
                    pending.result()
                except Exception as exc:
                    self.owner._set_error(f"工程已打开，但最近工程列表未能更新：{exc}")
        return not self._history_pending

    def _open_finished(self, succeeded: bool) -> None:
        self._completion = succeeded
        if succeeded:
            # Admission creates real views synchronously. Hide/disable them in
            # this same GUI callback before input can reach an unconfirmed owner.
            for view in self.owner.desktop._views.values():
                window = view[0]
                item = window.contentItem()
                self._startup_windows.append((window, item, window.isVisible(), item.isEnabled()))
                item.setEnabled(False)
                window.hide()
            # Refresh the exact bound session before publishing ready. A new
            # owner's empty initial registration is not a readiness certificate.
            self.application._register()
        else:
            self._fail(str(self.owner.projectError) or "工程打开失败。")

    def _present_review(self, project_id: str) -> None:
        self.dialog_window.setProperty("closeProjectId", project_id)
        self.dialog_window.setProperty("reviewKind", "project")
        self.dialog_window.show()
        self.dialog_window.raise_()
        self.dialog_window.requestActivate()

    def _present_shutdown(self) -> None:
        lifecycle = self.bridge.project_lifecycle
        if lifecycle is None:
            raise RuntimeError("Project lifecycle is unavailable")
        parent = lifecycle.confirmation_owner()
        self.dialog_window.setTransientParent(parent)
        self.dialog_window.setProperty("reviewKind", "shutdown")
        LauncherBridge._activate_window(self.dialog_window)

    def _present_management(self, project_id: str) -> None:
        session = next((row for row in self.owner.host.sessions()
                        if row.binding.project_id == project_id), None)
        if session is None:
            return
        self.dialog_window.setProperty("managementProjectId", project_id)
        self.dialog_window.setProperty("managementProjectName", session.name)
        self.dialog_window.setProperty("reviewKind", "management")
        self.dialog_window.show()
        self.dialog_window.raise_()
        self.dialog_window.requestActivate()
        QMetaObject.invokeMethod(self.dialog_window, "openManagement")

    def _owner_changed(self) -> None:
        if (self._finished or self._failed) and self.owner.projectError:
            # An asynchronous warning cannot replace an outstanding close,
            # recovery or global-exit decision and strand its coordinator.
            if self.dialog_window.isVisible() and self.dialog_window.property("reviewKind") != "error":
                return
            self.dialog_window.setProperty("reviewKind",
                "releaseWarning" if getattr(self.owner, "releaseWarningPending", False) else "error")
            self.dialog_window.show()

    def _fail(self, message: str) -> None:
        if self._failed:
            return
        self._failed = True
        report_startup_event("error", error=message)
        try:
            self.owner.abort_startup()
        except Exception as exc:
            self.owner._set_error(f"工程启动清理失败：{exc}")
        finally:
            self.owner.stop()

    def request_shutdown(self) -> None:
        """Qt Quit and global commit use the same fresh project preflight."""
        if self.bridge._closing or self.bridge._runtime_shutdown_requested:
            return
        try:
            for session in self.owner.host.sessions():
                if self.bridge._project_shutdown_approved:
                    self.owner.project_closer._preflight(session)
                    if not self.owner.project_closer._close(session):
                        return
                elif not self.owner.closeProject(session.binding.project_id):
                    return
            self.bridge._runtime_shutdown_requested = True
        except Exception as exc:
            self.owner._set_error(str(exc))

    def _tick(self) -> None:
        history_settled = self._poll_history()
        client = self.application
        if not self._finished and not self._failed:
            if time.monotonic() - self._started_at > 45.0:
                self._fail("工程进程启动超时；现有工程的未保存内容未迁移或覆盖。")
            elif not self._requested and client.admission.ready:
                self._requested = True
                accepted = (self.owner._start(str(self.create_root), self.create_name or "",
                                              summary=self.create_summary, notes=self.create_notes)
                            if self.create_root is not None else self.owner.openProject(str(self.project_file)))
                if not accepted:
                    self._fail(str(self.owner.projectError))
            elif self._completion is True and client._pending is None and client.admission.ready:
                self._finished = True
                self.bridge._project_starting = False
                for window, item, visible, enabled in self._startup_windows:
                    item.setEnabled(enabled)
                    if visible:
                        window.show()
                        window.raise_()
                        window.requestActivate()
                self._startup_windows.clear()
                self._record_history()
                report_startup_event("ready", endpoint=self._endpoint)
        if self._failed or (self._finished and not self.owner.host.sessions()):
            if not history_settled:
                if self._history_draining_started is None:
                    self._history_draining_started = time.monotonic()
                # Auxiliary history must not retain an otherwise closed owner
                # indefinitely on an unavailable/network settings location.
                history_settled = time.monotonic() - self._history_draining_started >= 6.0
            try:
                if self.owner.settled_for_exit() and history_settled:
                    self._timer.stop()
                    self._history_worker.stop()
                    self.bridge._runtime_shutdown_authorized = True
                    if self._failed:
                        # The initial, noninteractive admission was aborted;
                        # all asynchronous release work has now settled.
                        self.bridge.project_runtime = None
                    self.bridge.shutdown()
            except Exception as exc:
                message = f"工程所有权释放失败，请保留此窗口：{exc}"
                if self.owner.projectError != message:
                    self.owner._set_error(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent project runtime")
    selected = parser.add_mutually_exclusive_group(required=True)
    selected.add_argument("--project", type=Path)
    selected.add_argument("--create-root", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--create-information", type=Path)
    parser.add_argument("--consume-create-information", action="store_true")
    args = parser.parse_args(argv)
    if (args.create_root is not None) != (args.name is not None):
        parser.error("--create-root and --name must be supplied together")
    information: dict[str, str] = {}
    if args.consume_create_information and args.create_information is None:
        parser.error("--consume-create-information requires --create-information")
    if args.create_information is not None:
        if args.create_root is None:
            parser.error("--create-information requires --create-root")
        from coat_sim.core.project_dependencies import read_json, reject_links
        try:
            reject_links(args.create_information)
            information, _ = read_json(args.create_information)
        finally:
            if args.consume_create_information:
                args.create_information.unlink(missing_ok=True)
        if (not isinstance(information, dict) or set(information) - {"summary", "notes"}
                or any(not isinstance(value, str) or len(value) > 20000
                       for value in information.values())):
            parser.error("Invalid project creation information")
    configure_qt_dpi_scale()
    app = cast(QApplication, QApplication.instance() or QApplication([]))
    app.setApplicationVersion(BUILD_IDENTITY.version)
    app.setQuitOnLastWindowClosed(False)
    apply_font_settings(app=app)
    engine = QQmlApplicationEngine()
    configure_theme_from_settings().attach_engine(engine)
    try:
        runtime = ProjectRuntime(app, engine, args.project, create_root=args.create_root, create_name=args.name,
                                 create_summary=information.get("summary"), create_notes=information.get("notes"))
        runtime.start()
    except Exception as exc:
        report_startup_event("error", error=str(exc))
        return 2
    result = app.exec()
    return 2 if runtime._failed else result


if __name__ == "__main__":
    raise SystemExit(main())
