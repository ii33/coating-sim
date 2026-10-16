"""Real native close events use one generation-scoped project close policy."""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import cast

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QGuiApplication, QWindow
from shiboken6 import isValid

from coat_sim.core.project_compute import ProjectComputeWorkspace
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
from coat_sim.core.project_store import ProjectStore
from coat_sim.project_recovery import ProjectOwnerLease
from coat_sim.project_session import ProjectSessionError
from coat_sim.ui.project_runtime_owner import ProjectRuntimeOwner
from tests.test_project_film_controller_adapter import MemorySettings


pytestmark = [pytest.mark.contract, pytest.mark.qt]


def spin_until(read, accept, label, timeout=8):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        last = read()
        if accept(last):
            return last
        time.sleep(0.01)
    raise AssertionError(f"{label}: {last!r}")


def drain_events():
    for _ in range(4):
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    app = cast(QGuiApplication, QGuiApplication.instance() or QGuiApplication([]))
    previous_quit = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(False)
    owner_root = tmp_path / "owners"
    monkeypatch.setattr("coat_sim.project_recovery.project_owner_runtime_root", lambda: owner_root)
    adapter = ProjectRuntimeOwner(
        geometry_settings=MemorySettings(),
        default_loader=lambda _store: SimpleNamespace(state="empty_project"))
    prompts: list[str] = []
    adapter.projectCloseConfirmationRequested.connect(prompts.append)

    def open_project(name="Study"):
        store = ProjectStore.create(tmp_path / name, name)
        assert adapter.openProject(str(store.project_file))
        spin_until(lambda: adapter.projectBusy, lambda busy: not busy, "project admission")
        assert adapter.projectError == ""
        session = next(row for row in adapter.host.sessions()
                       if row.binding.project_id == store.manifest["id"])
        workspace = ProjectFilmWorkspace.for_project(adapter.host, session.binding)
        route = next(route for route in adapter.host.routes(session.binding.project_id)
                     if route.module_key == "film_design")
        window = adapter.host.window_for(route)
        assert isinstance(window, QWindow) and window.isVisible()
        assert adapter.desktop.project_closer is adapter.project_closer
        return SimpleNamespace(store=store, session=session, workspace=workspace,
                               route=route, window=window,
                               lease=adapter._leases[session.binding.project_id])

    yield SimpleNamespace(adapter=adapter, open=open_project, prompts=prompts, owner_root=owner_root)
    # Teardown goes through the same all-project authorization path, including
    # when a test leaves a dirty confirmation pending.
    for session in tuple(adapter.host.sessions()):
        identity = session.binding.project_id
        adapter.cancelCloseProject(identity)
        compute = adapter.host.resource_for(session.binding, "compute_workspace")
        if compute is not None:
            for row in compute.active_jobs():
                compute.update_job(row["job_id"], "cancelled")
        if not adapter.closeProject(identity):
            adapter.confirmCloseProject(identity)
    adapter.stop()
    adapter.deleteLater()
    drain_events()
    app.setQuitOnLastWindowClosed(previous_quit)


def assert_lease_held(project, desktop):
    with pytest.raises(ProjectError) as error:
        ProjectOwnerLease.acquire(project.store.project_file, project.session.binding.project_id,
                                  owner_runtime_root=desktop.owner_root)
    assert error.value.code == "active_project_owner"
    assert not project.lease._closed


def assert_lease_released(project, desktop):
    assert project.lease._closed
    lease = ProjectOwnerLease.acquire(project.store.project_file, project.session.binding.project_id,
                                     owner_runtime_root=desktop.owner_root)
    lease.close()


def open_source(desktop, project):
    desktop.adapter.desktop.open_project_module(project.workspace, "source")
    route = next(route for route in desktop.adapter.host.routes(project.session.binding.project_id)
                 if route.module_key == "source")
    window = desktop.adapter.host.window_for(route)
    resources = ProjectResourceWorkspace.for_project(desktop.adapter.host, project.session.binding)
    source = resources.controllers["source"]
    assert isinstance(window, QWindow) and window.isVisible()
    return route, window, resources, source


def test_clean_last_native_window_close_releases_host_views_and_real_lease(desktop):
    project = desktop.open()
    assert not project.workspace.editor.snapshot().dirty
    assert_lease_held(project, desktop)
    project.window.close()
    spin_until(desktop.adapter.host.sessions, lambda rows: rows == (), "clean last close")
    drain_events()
    assert desktop.adapter.desktop._views == {}
    assert desktop.adapter.projectEntries == []
    assert desktop.adapter.selectedProjectId == ""
    assert desktop.prompts == []
    assert_lease_released(project, desktop)


@pytest.mark.parametrize("decision", ["cancelCloseProject", "returnToSaveProject"])
def test_dirty_last_window_cancel_or_return_to_save_keeps_every_module_draft(desktop, decision):
    project = desktop.open()
    assert project.workspace.editor.set_design_value("name", "Retained film draft")
    source_route, source_window, resources, source = open_source(desktop, project)
    source.bridge.setField("source_exponent", "4")
    assert source.dirty
    # An earlier film view can close while its draft remains owned. Closing
    # the final source view must therefore review more than the source draft.
    project.window.setProperty("allowClose", True)
    assert project.window.close()
    spin_until(lambda: desktop.adapter.host.routes(project.session.binding.project_id),
               lambda routes: routes == (source_route,), "non-last film view closes")
    before_descriptor = project.store.project_file.read_bytes()
    before_film = project.workspace.editor.snapshot()
    before_resources = resources.snapshot()
    local_prompts = []
    source.closeDecisionRequested.connect(lambda: local_prompts.append(True))
    source_window.close()
    spin_until(lambda: desktop.prompts, lambda rows: len(rows) == 1, "whole-project dirty prompt")
    assert desktop.prompts == [project.session.binding.project_id]
    assert local_prompts == []
    assert source_window.isVisible()
    assert not source_window.property("projectCloseAuthorized")
    getattr(desktop.adapter, decision)(project.session.binding.project_id)
    drain_events()
    assert desktop.adapter.host.sessions() == (project.session,)
    assert source_window.isVisible()
    assert project.workspace.editor.snapshot() == before_film
    assert resources.snapshot() == before_resources
    assert project.store.project_file.read_bytes() == before_descriptor
    assert source.dirty and project.workspace.editor.snapshot().dirty
    assert_lease_held(project, desktop)
    # Both choices end this close review, so normal module admission resumes.
    desktop.adapter.desktop.open_project_module(project.workspace, "mask")
    assert any(route.module_key == "mask" for route in desktop.adapter.host.routes(project.session.binding.project_id))


def test_discard_from_last_window_closes_whole_dirty_project_once_without_saving(desktop):
    project = desktop.open()
    project.workspace.editor.set_design_value("name", "Discarded film draft")
    source_route, source_window, _resources, source = open_source(desktop, project)
    source.bridge.setField("source_exponent", "5")
    project.window.setProperty("allowClose", True)
    assert project.window.close()
    spin_until(lambda: desktop.adapter.host.routes(project.session.binding.project_id),
               lambda rows: rows == (source_route,), "only source remains")
    before = project.store.project_file.read_bytes()
    source_window.close()
    spin_until(lambda: desktop.prompts, lambda rows: len(rows) == 1, "dirty review")
    assert desktop.adapter.confirmCloseProject(project.session.binding.project_id)
    spin_until(desktop.adapter.host.sessions, lambda rows: rows == (), "discard closes whole project")
    drain_events()
    assert desktop.adapter.desktop._views == {}
    assert desktop.prompts == [project.session.binding.project_id]
    assert project.store.project_file.read_bytes() == before
    assert ProjectStore.open(project.store.project_file).manifest["documents"] == []
    assert_lease_released(project, desktop)


def test_hide_and_minimize_never_close_or_release_last_project_window(desktop):
    project = desktop.open()
    project.workspace.editor.set_design_value("name", "Still owned while hidden")
    project.window.hide()
    drain_events()
    assert desktop.adapter.host.sessions() == (project.session,)
    assert desktop.adapter.host.routes(project.session.binding.project_id) == (project.route,)
    assert desktop.adapter.host.window_for(project.route) is project.window
    assert project.route in desktop.adapter.desktop._views
    project.window.showMinimized()
    drain_events()
    assert project.window.windowState() == Qt.WindowState.WindowMinimized
    assert desktop.adapter.host.window_for(project.route) is project.window
    assert project.workspace.editor.snapshot().dirty
    assert desktop.prompts == []
    assert_lease_held(project, desktop)
    project.window.showNormal()


@pytest.mark.parametrize("last_window", [True, False])
def test_explicit_native_close_after_hide_releases_registration_without_visibility_change(desktop, last_window):
    project = desktop.open()
    source_route = source_window = None
    if not last_window:
        source_route, source_window, _resources, _source = open_source(desktop, project)
    project.window.hide()
    drain_events()
    assert desktop.adapter.host.window_for(project.route) is project.window
    project.window.close()
    if last_window:
        spin_until(desktop.adapter.host.sessions, lambda rows: rows == (), "hidden final window closes project")
        assert_lease_released(project, desktop)
    else:
        spin_until(lambda: desktop.adapter.host.routes(project.session.binding.project_id),
                   lambda routes: routes == (source_route,), "hidden non-last window registration releases")
        assert desktop.adapter.host.sessions() == (project.session,)
        assert source_window is not None and source_window.isVisible()
        assert_lease_held(project, desktop)
    spin_until(lambda: desktop.adapter.desktop._views, lambda views: project.route not in views,
               "hidden closed view cleanup")
    assert desktop.prompts == []


def test_final_window_close_isolated_from_another_dirty_project_and_global_windows(desktop):
    first = desktop.open("First")
    second = desktop.open("Second")
    second.workspace.editor.set_design_value("name", "Second draft stays open")
    globals_ = [QWindow() for _ in range(3)]
    try:
        for window, name in zip(globals_, ("launcher", "global_ai", "global_settings"), strict=True):
            window.setObjectName(name)
            window.show()
        first.window.close()
        spin_until(desktop.adapter.host.sessions, lambda rows: rows == (second.session,), "only first project closes")
        assert desktop.adapter.host.window_for(second.route) is second.window
        assert second.window.isVisible() and second.workspace.editor.snapshot().dirty
        assert all(window.isVisible() for window in globals_)
        assert desktop.prompts == []
        assert desktop.adapter.selectedProjectId == second.session.binding.project_id
        assert_lease_released(first, desktop)
        assert_lease_held(second, desktop)
    finally:
        for window in globals_:
            window.close()
            window.deleteLater()


@pytest.mark.parametrize("first_decision", ["cancelCloseProject", "returnToSaveProject", "confirmCloseProject"])
def test_two_dirty_projects_queue_one_close_question_and_each_decision_restores_admission(desktop, first_decision):
    first = desktop.open("First")
    second = desktop.open("Second")
    first.workspace.editor.set_design_value("name", "First queued draft")
    second.workspace.editor.set_design_value("name", "Second queued draft")
    first.window.close()
    second.window.close()
    drain_events()
    first_id = first.session.binding.project_id
    second_id = second.session.binding.project_id
    assert desktop.prompts == [first_id]
    assert first.window.isVisible() and second.window.isVisible()
    assert_lease_held(first, desktop)
    assert_lease_held(second, desktop)
    with pytest.raises((ProjectError, ProjectSessionError, ValueError)):
        desktop.adapter.desktop.open_project_module(second.workspace, "source")
    getattr(desktop.adapter, first_decision)(first_id)
    spin_until(lambda: desktop.prompts, lambda rows: len(rows) == 2, "next queued project close question")
    assert desktop.prompts == [first_id, second_id]
    assert second.window.isVisible() and second.workspace.editor.snapshot().dirty
    assert desktop.adapter.host.window_for(second.route) is second.window
    if first_decision == "confirmCloseProject":
        assert [session.binding.project_id for session in desktop.adapter.host.sessions()] == [second_id]
        assert_lease_released(first, desktop)
    else:
        assert first.window.isVisible() and first.workspace.editor.snapshot().dirty
        assert desktop.adapter.host.window_for(first.route) is first.window
        assert_lease_held(first, desktop)
    desktop.adapter.cancelCloseProject(second_id)
    drain_events()
    open_source(desktop, second)
    if first_decision == "confirmCloseProject":
        assert desktop.adapter.openProject(str(first.store.project_file))
        spin_until(lambda: desktop.adapter.projectBusy, lambda busy: not busy, "discarded queued project reopens")
        assert desktop.adapter.projectError == ""
        fresh = next(session for session in desktop.adapter.host.sessions()
                     if session.binding.project_id == first_id)
        assert fresh.binding.generation != first.session.binding.generation
        assert desktop.adapter.host.routes(first_id)
    else:
        open_source(desktop, first)
    assert len(desktop.adapter.host.sessions()) == 2
    assert desktop.prompts == [first_id, second_id]


def test_nonlast_native_window_close_keeps_session_and_other_window_without_project_prompt(desktop):
    project = desktop.open()
    source_route, source_window, _resources, _source = open_source(desktop, project)
    assert project.window.close()
    spin_until(lambda: desktop.adapter.host.routes(project.session.binding.project_id),
               lambda routes: routes == (source_route,), "ordinary non-last close")
    assert desktop.adapter.host.sessions() == (project.session,)
    assert source_window.isVisible()
    assert desktop.prompts == []
    assert_lease_held(project, desktop)


@pytest.mark.parametrize("local_decision", ["keep", "save", "discard"])
def test_delayed_local_close_decision_rechecks_when_view_becomes_last(desktop, local_decision):
    project = desktop.open()
    project.workspace.editor.set_design_value("name", "A sibling's retained dirty film")
    source_route, source_window, _resources, source = open_source(desktop, project)
    source.bridge.setField("source_exponent", "4")
    local_prompts = []
    source.closeDecisionRequested.connect(lambda: local_prompts.append(True))
    assert not source_window.close()
    assert local_prompts == [True] and desktop.prompts == []
    # The local resource question was opened while two actual views existed.
    # Its later answer must re-evaluate the live project view count.
    project.window.setProperty("allowClose", True)
    assert project.window.close()
    spin_until(lambda: desktop.adapter.host.routes(project.session.binding.project_id),
               lambda rows: rows == (source_route,), "source becomes final view during local review")
    assert source.confirmClose(local_decision)
    spin_until(lambda: (desktop.prompts, desktop.adapter.host.routes(project.session.binding.project_id)),
               lambda result: bool(result[0]) or not result[1], "delayed local answer reaches project policy")
    assert desktop.prompts == [project.session.binding.project_id]
    assert desktop.adapter.host.sessions() == (project.session,)
    assert source_window.isVisible()
    assert project.workspace.editor.snapshot().dirty
    assert desktop.adapter.host.window_for(source_route) is source_window
    assert_lease_held(project, desktop)
    desktop.adapter.cancelCloseProject(project.session.binding.project_id)
    source.bridge.setField("source_exponent", "6")
    desktop.adapter.desktop.open_project_module(project.workspace, "mask")
    # Cancelling the project question must not leave a one-time local answer
    # as a permanent bypass for later edits when this is no longer the last.
    assert not source_window.close()
    assert source_window.isVisible() and source.dirty
    assert local_prompts == [True, True]


def test_repeated_last_close_events_deduplicate_one_dirty_review(desktop):
    project = desktop.open()
    project.workspace.editor.set_design_value("name", "One review only")
    for _ in range(5):
        project.window.close()
    spin_until(lambda: desktop.prompts, lambda rows: len(rows) >= 1, "dirty review")
    for _ in range(5):
        project.window.close()
        QCoreApplication.processEvents()
    assert desktop.prompts == [project.session.binding.project_id]
    assert project.window.isVisible()
    assert desktop.adapter.host.window_for(project.route) is project.window
    assert project.workspace.editor.snapshot().pending_confirmation is None
    desktop.adapter.cancelCloseProject(project.session.binding.project_id)
    project.window.close()
    spin_until(lambda: desktop.prompts, lambda rows: len(rows) == 2, "new review after cancellation")
    assert project.workspace.editor.snapshot().dirty


def test_pending_last_close_rejects_new_view_until_review_resolved(desktop):
    project = desktop.open()
    project.workspace.editor.set_design_value("name", "Close review blocks admission")
    project.window.close()
    # The guard starts with close admission, before the queued preflight can
    # emit its prompt. There must be no route reserved during this gap.
    with pytest.raises((ProjectError, ProjectSessionError, ValueError)):
        desktop.adapter.desktop.open_project_module(project.workspace, "source")
    assert desktop.adapter.host.routes(project.session.binding.project_id) == (project.route,)
    spin_until(lambda: desktop.prompts, lambda rows: len(rows) == 1, "dirty close review")
    with pytest.raises((ProjectError, ProjectSessionError, ValueError)):
        desktop.adapter.desktop.open_project_module(project.workspace, "source")
    assert not desktop.adapter.activateProject(project.session.binding.project_id)
    desktop.adapter.cancelCloseProject(project.session.binding.project_id)
    source_route, source_window, _resources, _source = open_source(desktop, project)
    assert source_window.isVisible() and source_route != project.route


def test_last_window_close_runs_real_local_active_job_preflight(desktop):
    project = desktop.open()
    compute = ProjectComputeWorkspace.for_project(desktop.adapter.host, project.session.binding)
    compute.register_job("local-still-running", service_owned=False, state="running")
    project.window.close()
    spin_until(lambda: desktop.adapter.projectError, lambda message: bool(message), "active local job close rejection")
    assert "计算" in desktop.adapter.projectError
    assert desktop.adapter.host.sessions() == (project.session,)
    assert desktop.adapter.host.window_for(project.route) is project.window
    assert project.window.isVisible() and compute.active_jobs()
    assert desktop.prompts == []
    assert_lease_held(project, desktop)
    assert compute.update_job("local-still-running", "cancelled")
    project.window.close()
    spin_until(desktop.adapter.host.sessions, lambda rows: rows == (), "close after local job ends")
    assert_lease_released(project, desktop)


def test_authorized_project_close_does_not_reenter_last_window_confirmation(desktop):
    project = desktop.open()
    project.workspace.editor.set_design_value("name", "One coordinated discard")
    open_source(desktop, project)
    assert not desktop.adapter.closeProject(project.session.binding.project_id)
    assert desktop.prompts == [project.session.binding.project_id]
    assert desktop.adapter.confirmCloseProject(project.session.binding.project_id)
    drain_events()
    assert desktop.adapter.host.sessions() == ()
    assert desktop.adapter.desktop._views == {}
    assert desktop.prompts == [project.session.binding.project_id]
    assert_lease_released(project, desktop)


def test_destroyed_old_route_callback_cannot_close_fresh_generation(desktop):
    project = desktop.open()
    project.window.close()
    spin_until(desktop.adapter.host.sessions, lambda rows: rows == (), "old generation closes")
    assert desktop.adapter.openProject(str(project.store.project_file))
    spin_until(lambda: desktop.adapter.projectBusy, lambda busy: not busy, "new generation opens")
    assert desktop.adapter.projectError == ""
    session = desktop.adapter.host.sessions()[0]
    assert session.binding.generation != project.session.binding.generation
    route = desktop.adapter.host.routes(session.binding.project_id)[0]
    window = desktop.adapter.host.window_for(route)
    fresh_lease = desktop.adapter._leases[session.binding.project_id]
    # Destruction of an old QObject can arrive after the same project has
    # reopened. Exercise the exact callback with its captured old route.
    desktop.adapter.desktop._release_destroyed(project.route)
    if isValid(project.window):
        project.window.deleteLater()
    drain_events()
    assert desktop.adapter.host.sessions() == (session,)
    assert desktop.adapter.host.routes(session.binding.project_id) == (route,)
    assert desktop.adapter.host.window_for(route) is window and window.isVisible()
    assert desktop.adapter._leases[session.binding.project_id] is fresh_lease
    assert not fresh_lease._closed
    assert route in desktop.adapter.desktop._views
    assert desktop.prompts == []


def open_machine(desktop, project):
    desktop.adapter.desktop.open_project_module(project.workspace, "machine")
    route = next(route for route in desktop.adapter.host.routes(project.session.binding.project_id)
                 if route.module_key == "machine")
    window = desktop.adapter.host.window_for(route)
    resources = ProjectResourceWorkspace.for_project(desktop.adapter.host, project.session.binding)
    return route, window, resources, resources.controllers["machine"]


def test_untouched_machine_last_window_closes_without_forced_save(desktop):
    project = desktop.open()
    route, window, resources, machine = open_machine(desktop, project)
    assert not machine.dirty
    assert project.window.close()
    spin_until(lambda: desktop.adapter.host.routes(project.session.binding.project_id),
               lambda rows: rows == (route,), "machine is last untouched view")
    window.close()
    spin_until(desktop.adapter.host.sessions, lambda rows: rows == (), "untouched machine last close")
    assert desktop.prompts == [] and not resources.dirty_summary()
    assert_lease_released(project, desktop)


@pytest.mark.parametrize("entry", ["last", "explicit"])
@pytest.mark.parametrize("decision", ["cancelCloseProject", "returnToSaveProject", "confirmCloseProject"])
def test_invalid_native_machine_text_project_close_never_forces_save(desktop, entry, decision):
    from PySide6.QtWidgets import QApplication
    from tests.test_project_resource_adapter import _machine_input
    project = desktop.open()
    route, window, resources, machine = open_machine(desktop, project)
    if entry == "last":
        assert project.window.close()
        spin_until(lambda: desktop.adapter.host.routes(project.session.binding.project_id),
                   lambda rows: rows == (route,), "only machine remains")
    field = _machine_input(window, QApplication.instance(), "invalid")
    before = project.store.project_file.read_bytes()
    original = machine.bridge.documentJson
    identity = project.session.binding.project_id
    if entry == "last":
        assert not window.close()
    else:
        assert not desktop.adapter.closeProject(identity)
    spin_until(lambda: desktop.prompts, lambda rows: rows == [identity], "project reviews invalid text")
    assert machine.dirty and machine.snapshot_pending_edits()["text"] == "invalid"
    assert machine.bridge.documentJson == original
    getattr(desktop.adapter, decision)(identity)
    if decision == "confirmCloseProject":
        spin_until(desktop.adapter.host.sessions, lambda rows: rows == (), "discard invalid project input")
        assert_lease_released(project, desktop)
    else:
        spin_until(lambda: field.property("text"), lambda text: text == "invalid", "cancel restores invalid native text")
        assert window.isVisible() and machine.dirty
        assert desktop.adapter.host.sessions() == (project.session,)
        assert machine.bridge.documentJson == original
        assert_lease_held(project, desktop)
    assert project.store.project_file.read_bytes() == before


def test_project_discard_valid_native_machine_text_survives_confirmation_focus_change(desktop):
    from PySide6.QtWidgets import QApplication
    from tests.test_project_resource_adapter import _machine_input
    project = desktop.open()
    _route, window, _resources, machine = open_machine(desktop, project)
    _machine_input(window, QApplication.instance(), "24")
    identity = project.session.binding.project_id
    assert not desktop.adapter.closeProject(identity)
    confirmation = QWindow()
    confirmation.setModality(Qt.WindowModality.ApplicationModal)
    confirmation.show()
    confirmation.requestActivate()
    drain_events()
    try:
        assert desktop.adapter.confirmCloseProject(identity), desktop.adapter.projectError
        spin_until(desktop.adapter.host.sessions, lambda rows: rows == (), "valid native text discard")
        assert_lease_released(project, desktop)
    finally:
        confirmation.close()
        confirmation.deleteLater()


@pytest.mark.parametrize("decision", ["cancel", "stale_discard"])
def test_project_review_preserves_newer_native_machine_input(desktop, decision):
    from PySide6.QtWidgets import QApplication
    from tests.test_project_resource_adapter import _machine_input
    project = desktop.open()
    _route, window, _resources, machine = open_machine(desktop, project)
    _machine_input(window, QApplication.instance(), "invalid")
    identity = project.session.binding.project_id
    assert not desktop.adapter.closeProject(identity)
    field = _machine_input(window, QApplication.instance(), "newer")
    if decision == "cancel":
        desktop.adapter.cancelCloseProject(identity)
    else:
        assert not desktop.adapter.confirmCloseProject(identity)
        assert "变化" in desktop.adapter.projectError
    drain_events()
    assert window.isVisible() and field.property("text") == "newer"
    assert machine.snapshot_pending_edits()["text"] == "newer"
    assert machine.dirty
    assert_lease_held(project, desktop)
