"""Offscreen real-QML integration; no cross-process/launcher acceptance claim."""

from types import SimpleNamespace

import pytest

from PySide6.QtCore import QObject, QCoreApplication, QEvent, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.project_store import ProjectStore
from coat_sim.project_session import ProjectSessionHost
from coat_sim.ui.project_film_host import ProjectFilmDesktop
from tests.test_project_film_controller_adapter import MemorySettings
from tests.test_project_film_service import bound_design, material


pytestmark = [pytest.mark.contract, pytest.mark.qt]


def events():
    QCoreApplication.processEvents()
    QTest.qWait(20)
    QCoreApplication.processEvents()


@pytest.fixture
def desktop(tmp_path):
    stores = {}
    for name in ("First", "Second"):
        store = ProjectStore.create(tmp_path / name, name)
        stores[store.root] = store
    host = ProjectSessionHost(lambda root: stores[root])
    desktop = ProjectFilmDesktop(host, geometry_settings=MemorySettings())
    yield desktop, stores
    for route, view in tuple(desktop._views.items()):
        window, controller, _engine, _context, _component = view
        if route.module_key == "film_design":
            window.setProperty("allowClose", True)
        else:
            window.setProperty("closeAuthorized", True)
        window.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    events()
    desktop.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_real_qml_project_toolbar_and_repeated_navigation_preserve_draft(desktop):
    client, stores = desktop
    root = next(iter(stores))
    workspace = client.open_project(root)
    events()
    controller = client.controller_for(workspace, "film_design")
    window = next(view[0] for route, view in client._views.items()
                  if route.binding == workspace.binding)
    selector = window.findChild(QQuickItem, "filmDesignProjectModuleSelector")
    header = window.findChild(QQuickItem, "filmDesignHeaderFlow")
    assert selector is not None and selector.parentItem().parentItem() is header
    assert selector.property("text") == "膜系设计"
    assert window.property("title") == "膜系设计 — First"
    assert window.property("caption") == "膜系设计 — First"
    assert window.findChild(QQuickItem, "filmDesignProjectName") is None
    assert controller.setDesignValue("name", "Retain working draft")
    # Selecting this window through the real two-level menu activates its
    # existing project registration and retains the working owner/draft.
    for _ in range(2):
        selector.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Space)
        events()
        menu = window.findChild(QObject, "projectModuleMenu")
        assert menu is not None and menu.property("visible")
        if menu.property("currentIndex") == -1:
            QTest.keyClick(window, Qt.Key.Key_Down)
        QTest.keyClick(window, Qt.Key.Key_Right)
        events()
        film_menu = window.findChild(QObject, "projectModuleFilmMenu")
        assert film_menu is not None and film_menu.property("visible")
        if film_menu.property("currentIndex") == -1:
            QTest.keyClick(window, Qt.Key.Key_Down)
        QTest.keyClick(window, Qt.Key.Key_Return)
        events()
        assert len(client._views) == 1
        assert client.controller_for(workspace, "film_design") is controller
        assert controller.currentDesign()["name"] == "Retain working draft"
    identity = controller.openProjectModule("film_design")
    assert client.open_project(root) is workspace
    assert client.controller_for(workspace, "film_design") is controller
    assert controller.currentDesign()["name"] == "Retain working draft"
    assert len(client._views) == 1 and identity
    # Ordinary Save in the actual QML window does not launch a file dialog.
    window.saveOrChoosePath()
    assert not controller.dirty
    assert len(workspace.service.list_saved_designs()) == 1


def test_two_projects_simulation_sources_and_materials_never_cross(desktop):
    client, stores = desktop
    workspaces = [client.open_project(root) for root in stores]
    controllers = []
    for workspace, n in zip(workspaces, (2.0, 3.0), strict=True):
        saved = workspace.service.create_film(bound_design(), materials={"shared": material(n=n)})
        controller = client.controller_for(workspace, "film_design")
        assert controller.requestLoadSavedDesign(saved.document_id)
        design_window = next(view[0] for route, view in client._views.items()
                             if route.binding == workspace.binding and route.module_key == "film_design")
        design_window.show()
        design_window.requestActivate()
        events()
        selector = design_window.findChild(QQuickItem, "filmDesignProjectModuleSelector")
        assert selector is not None
        selector.forceActiveFocus()
        events()
        QTest.keyClick(design_window, Qt.Key.Key_Space)
        events()
        menu = design_window.findChild(QObject, "projectModuleMenu")
        assert menu is not None and menu.property("visible")
        for _ in range(3):
            if menu.property("currentIndex") == 2:
                break
            QTest.keyClick(design_window, Qt.Key.Key_Down)
        assert menu.property("currentIndex") == 2
        QTest.keyClick(design_window, Qt.Key.Key_Right)
        events()
        simulations_menu = design_window.findChild(QObject, "projectModuleSimulationMenu")
        assert simulations_menu is not None and simulations_menu.property("visible")
        if simulations_menu.property("currentIndex") == -1:
            QTest.keyClick(design_window, Qt.Key.Key_Down)
        QTest.keyClick(design_window, Qt.Key.Key_Return)
        events()
        assert client.controller_for(workspace, "film_simulation") is not None
        controllers.append(controller)
    events()
    simulations = [client.controller_for(workspace, "film_simulation") for workspace in workspaces]
    simulation_window = next(view[0] for route, view in client._views.items()
                             if route.binding == workspaces[0].binding and route.module_key == "film_simulation")
    selector = simulation_window.findChild(QQuickItem, "filmSimulationProjectModuleSelector")
    assert selector is not None
    assert selector.parentItem().parentItem().objectName() == "filmSimulationActionBar"
    assert selector.property("text") == "光谱仿真"
    assert not simulation_window.findChild(QQuickItem, "filmSimulationOptimizeButton").property("visible")
    assert simulations[0]._source_session is controllers[0].session
    assert simulations[1]._source_session is controllers[1].session
    assert simulations[0].service is not simulations[1].service
    assert controllers[0].setDesignValue("name", "Only first")
    events()
    assert simulations[0].designName == "Only first"
    assert simulations[1].designName == "Bound project film"
    assert simulations[0].service.materials.get("shared")["optical_sets"][0]["n"] == [2.0, 2.0]
    assert simulations[1].service.materials.get("shared")["optical_sets"][0]["n"] == [3.0, 3.0]
    for simulation in simulations:
        simulation.startCalculation()
    for _ in range(100):
        events()
        if all(not simulation.busy for simulation in simulations):
            break
    for simulation, expected in zip(simulations, (1.0 / 9.0, 0.25), strict=True):
        assert not simulation.busy and simulation._result is not None
        assert simulation._result["R"] == pytest.approx([expected] * len(simulation._result["R"]))
    with pytest.raises(ValueError, match="ownership differs"):
        client.open_or_reuse_film_companion("film_simulation", SimpleNamespace(
            project_context=workspaces[0], service=workspaces[1].service))


def test_dirty_close_rejection_then_reopen_reuses_workspace_owned_draft(desktop):
    client, stores = desktop
    workspace = client.open_project(next(iter(stores)))
    controller = client.controller_for(workspace, "film_design")
    route = workspace.host.routes(workspace.binding.project_id)[0]
    window = workspace.host.window_for(route)
    assert controller.setDesignValue("name", "Retained draft")
    assert not window.close()
    events()
    assert workspace.host.window_for(route) is window
    controller.resolvePendingAction(False)
    assert controller.dirty
    # Accepted presentation close unregisters the view, not the working owner.
    window.setProperty("allowClose", True)
    assert window.close()
    events()
    assert workspace.host.routes(workspace.binding.project_id) == ()
    assert workspace.editor.snapshot().design["name"] == "Retained draft"
    client.open_project_module(workspace, "film_design")
    replacement = client.controller_for(workspace, "film_design")
    assert replacement is not controller
    assert replacement.session is workspace.editor
    assert replacement.currentDesign()["name"] == "Retained draft"
    assert replacement.dirty



def test_failed_qml_construction_releases_reservation_and_retry_keeps_editor(desktop, monkeypatch, tmp_path):
    from coat_sim.ui import project_film_host
    from coat_sim.core.project_film_session import ProjectFilmWorkspace

    client, stores = desktop
    session = client.host.open_project(next(iter(stores)))
    workspace = ProjectFilmWorkspace.for_project(client.host, session.binding)
    editor = workspace.editor
    assert editor.set_design_value("name", "Survives failed view")
    with monkeypatch.context() as patch:
        patch.setattr(project_film_host, "QML_ROOT", tmp_path / "missing-qml")
        with pytest.raises(RuntimeError):
            client.open_project_module(workspace, "film_design")
    assert client.host.routes(workspace.binding.project_id) == ()
    client.open_project_module(workspace, "film_design")
    assert client.controller_for(workspace, "film_design").session is editor
    assert editor.snapshot().design["name"] == "Survives failed view"


def test_second_desktop_facade_uses_same_registered_designer_for_companion(desktop):
    client, stores = desktop
    workspace = client.open_project(next(iter(stores)))
    other = ProjectFilmDesktop(client.host, geometry_settings=MemorySettings())
    try:
        assert other.open_project(next(iter(stores))) is workspace
        source = other.controller_for(workspace, "film_design")
        assert source is client.controller_for(workspace, "film_design")
        other.open_project_module(workspace, "film_simulation")
        simulation = other.controller_for(workspace, "film_simulation")
        assert simulation._source_session is workspace.editor
        for _route, view in tuple(other._views.items()):
            view[0].setProperty("closeAuthorized", True)
            view[0].close()
        events()
    finally:
        other.deleteLater()


@pytest.mark.parametrize("module_key", ["film_simulation", "film_optimization"])
def test_companion_reuse_rebinds_current_designer_after_presentation_reopen(desktop, module_key):
    from shiboken6 import isValid
    client, stores = desktop
    workspace = client.open_project(next(iter(stores)))
    # This branch's older workspace catalog has two film entries; the integrated
    # desktop uses the shared complete module catalog. Keep the fixture explicit.
    if module_key not in {row["key"] for row in workspace.module_options}:
        workspace.module_options = (*workspace.module_options,
                                    {"key": module_key, "name": "膜系优化"})
    original = client.controller_for(workspace, "film_design")
    saved = workspace.service.create_film(bound_design(), materials={"shared": material()})
    assert original.requestLoadSavedDesign(saved.document_id)
    identity = client.open_project_module(workspace, module_key)
    companion = client.controller_for(workspace, module_key)
    source_window = next(view[0] for route, view in client._views.items()
                         if route.module_key == "film_design" and route.binding == workspace.binding)
    source_window.setProperty("allowClose", True)
    assert source_window.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    events()
    assert not isValid(original)
    if module_key == "film_simulation":
        companion.setParameter("samples", "73")
        assert workspace.editor.workflow_configuration("simulation")["parameters"]["samples"] == 73
    client.open_project_module(workspace, "film_design")
    replacement = client.controller_for(workspace, "film_design")
    assert replacement is not original and replacement.session is workspace.editor
    assert client.open_project_module(workspace, module_key) == identity
    assert client.controller_for(workspace, module_key) is companion
    if module_key == "film_simulation":
        assert companion._source_controller_ref() is replacement
        assert companion._source_alive and companion.sampleCountText == "73"
        assert replacement.setDesignValue("name", "Current designer")
        events()
        assert companion.designName == "Current designer"
    else:
        assert companion._source_controller is replacement
        assert not companion._source_closed
        assert companion._source_edit_epoch() == replacement.pendingEditEpoch


def test_direct_window_destruction_cleans_presentation_and_preserves_draft(desktop):
    client, stores = desktop
    workspace = client.open_project(next(iter(stores)))
    controller = client.controller_for(workspace, "film_design")
    route = workspace.host.routes(workspace.binding.project_id)[0]
    window = workspace.host.window_for(route)
    assert controller.setDesignValue("name", "Retained after native destruction")
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    events()
    assert workspace.host.routes(workspace.binding.project_id) == ()
    assert not client._views
    assert workspace.editor.snapshot().design["name"] == "Retained after native destruction"
