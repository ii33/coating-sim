"""Launcher dismissal and explicit exit keep project draft ownership intact."""

import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.ui.launcher import LauncherBridge, _ProjectQuitGuard
from coat_sim.ui.project_runtime_owner import ProjectRuntimeOwner


pytestmark = [pytest.mark.contract, pytest.mark.qt]


class PickerWindow:
    def __init__(self):
        self.hidden = 0
        self.shown = 0

    def hide(self):
        self.hidden += 1

    def windowState(self):
        return Qt.WindowState.WindowNoState

    def isVisible(self):
        return bool(self.shown > self.hidden)

    def show(self):
        self.shown += 1

    def raise_(self):
        pass

    def requestActivate(self):
        pass


def settled(bridge, timeout=5):
    deadline = time.monotonic() + timeout
    while bridge.project_runtime._busy and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert not bridge.project_runtime._busy


def _close_bridge_fixture(bridge, engine):
    if bridge.project_runtime is not None:
        for _route, view in tuple(bridge.project_runtime.desktop._views.items()):
            view[0].setProperty("allowClose", True)
            view[0].setProperty("closeAuthorized", True)
            view[0].close()
        bridge.project_runtime.stop()
    bridge._shutdown_timer.stop()
    bridge.deleteLater()
    engine.deleteLater()
    QCoreApplication.processEvents()


@pytest.fixture
def launcher():
    QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlApplicationEngine()
    bridge = LauncherBridge(engine, software_mode=True)
    bridge.project_runtime = ProjectRuntimeOwner(
        bridge, geometry_settings=bridge._window_settings)
    picker = PickerWindow()
    bridge._launcher_window = picker
    yield bridge, picker
    _close_bridge_fixture(bridge, engine)


@pytest.fixture
def launcher_without_project_owner():
    QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlApplicationEngine()
    bridge = LauncherBridge(engine, software_mode=True)
    picker = PickerWindow()
    bridge._launcher_window = picker
    yield bridge, picker
    _close_bridge_fixture(bridge, engine)


def test_closing_lightweight_launcher_exits_without_host(launcher_without_project_owner):
    bridge, _picker = launcher_without_project_owner
    assert bridge.project_runtime is None
    assert bridge.project_launcher is not None
    assert not hasattr(bridge.project_launcher, "host")
    closes = []
    legacy = SimpleNamespace(close=lambda: closes.append(True), property=lambda _name: False)
    bridge._windows["legacy"] = SimpleNamespace(
        key="settings", title_bridge=None, window=legacy)
    bridge.closeLauncher()
    assert bridge._closing
    assert closes
    assert "legacy" in bridge._windows


def test_dirty_project_exit_confirm_cancel_and_hidden_draft(launcher, tmp_path):
    bridge, picker = launcher
    projects = bridge.project_runtime
    assert projects is not None
    assert projects.createProject("Study", str(tmp_path / "Study"))
    settled(bridge)
    session = projects.host.sessions()[0]
    workspace = ProjectFilmWorkspace.for_project(projects.host, session.binding)
    assert workspace.editor.set_design_value("name", "Unsaved draft")
    route = projects.host.routes(session.binding.project_id)[0]
    window = projects.host.window_for(route)
    lease = projects._leases[session.binding.project_id]
    project_close_requests: list[str] = []
    projects.projectCloseConfirmationRequested.connect(project_close_requests.append)
    # A hidden view retains its draft and owner. Closing the last actual view
    # now has its own whole-project decision, covered by the last-window tests.
    window.hide()
    QCoreApplication.processEvents()
    assert not window.isVisible()
    assert projects.host.routes(session.binding.project_id) == (route,)
    assert projects.host.window_for(route) is window
    assert projects._leases[session.binding.project_id] is lease
    assert project_close_requests == []
    assert workspace.editor.snapshot().dirty

    requested = []
    bridge.shutdownConfirmationRequested.connect(lambda: requested.append(True))
    bridge.shutdown()
    assert picker.shown == 1
    assert requested and bridge._shutdown_confirmation_pending
    bridge.shutdown()
    assert requested == [True]
    assert not bridge._closing
    assert len(projects.host.sessions()) == 1
    bridge.cancelShutdown()
    assert not bridge._shutdown_confirmation_pending
    assert workspace.editor.snapshot().dirty
    assert not window.isVisible()
    assert projects.host.window_for(route) is window
    assert projects._leases[session.binding.project_id] is lease
    assert project_close_requests == []

    guard = _ProjectQuitGuard(bridge)
    app = QGuiApplication.instance()
    assert app is not None
    assert guard.eventFilter(app, QEvent(QEvent.Type.Quit))
    assert bridge._shutdown_confirmation_pending
    bridge.confirmShutdown()
    assert bridge._closing
    assert projects.host.sessions() == ()
    assert session.binding.project_id not in projects._leases
    assert lease._closed


def test_clean_project_exit_closes_owned_windows_without_prompt(launcher, tmp_path):
    bridge, _picker = launcher
    projects = bridge.project_runtime
    assert projects is not None
    assert projects.createProject("Clean", str(tmp_path / "Clean"))
    settled(bridge)
    project_id = projects.selectedProjectId
    assert projects.host.routes(str(project_id))
    requested = []
    bridge.shutdownConfirmationRequested.connect(lambda: requested.append(True))
    bridge.shutdown()
    assert not requested
    assert bridge._closing
    assert projects.host.sessions() == ()


def test_close_one_project_preserves_other_project_and_checks_dirty(launcher, tmp_path):
    bridge, _picker = launcher
    projects = bridge.project_runtime
    assert projects.createProject("One", str(tmp_path / "One"))
    settled(bridge)
    one = projects.host.sessions()[0]
    assert projects.createProject("Two", str(tmp_path / "Two"))
    settled(bridge)
    two = projects.host.sessions()[1]
    owner = ProjectFilmWorkspace.for_project(projects.host, one.binding)
    owner.editor.set_design_value("name", "Unsaved One")
    confirmations: list[str] = []
    projects.projectCloseConfirmationRequested.connect(confirmations.append)
    assert not projects.closeProject(one.binding.project_id)
    assert confirmations == [one.binding.project_id]
    assert len(projects.host.sessions()) == 2
    projects.cancelCloseProject(one.binding.project_id)
    assert len(projects.host.sessions()) == 2
    assert not projects.closeProject(one.binding.project_id)
    assert projects.confirmCloseProject(one.binding.project_id)
    assert [row.binding.project_id for row in projects.host.sessions()] == [two.binding.project_id]
    assert projects.host.routes(two.binding.project_id)
    assert not bridge._closing
