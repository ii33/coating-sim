"""One asset window preserves the existing logical material editing owner."""
from __future__ import annotations

from types import SimpleNamespace
import time
from typing import Any, cast

import pytest
from PySide6.QtCore import QObject, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.control_errors import UiControlError
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.project_material_library import ProjectMaterialLibrary
from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
from coat_sim.ui.global_resources import GlobalResourcesBridge
from coat_sim.ui.project_editor_operations import project_editor_operation
from coat_sim.ui.project_editor_snapshot import project_editor_snapshot
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.test_launcher_information_global_resources import global_root as global_root
from tests.test_project_global_resources import profile
from tests.test_project_last_window_close import desktop as desktop, drain_events, spin_until
from tests.test_scoped_resource_libraries import open_library, qml, settled

pytestmark = [pytest.mark.contract, pytest.mark.qt]


class EmptyMaterials:
    def catalog_records(self):
        return []


def wait_for_material_idle(library, identity, label):
    deadline = time.monotonic() + 8
    stable_since = None
    while time.monotonic() < deadline:
        QTest.qWait(10)
        if not library.loading and (identity is None or library.currentId == identity):
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 0.25:
                return
        else:
            stable_since = None
    raise AssertionError(f"{label}: loading={library.loading!r}, currentId={library.currentId!r}")


def seed_project_material(desktop, project, identity="glass", *, category=None):
    resources = ProjectResourceWorkspace.for_project(desktop.adapter.host, project.session.binding)
    material = profile(identity)
    if category is not None:
        material["category"] = category
    ProjectMaterialLibrary(resources.service, global_library=EmptyMaterials()).write(material)
    return next(row for row in resources.store.manifest["documents"] if row["kind"] == "material_profile")


def test_material_route_reuses_one_asset_window_and_one_retained_owner(desktop, monkeypatch):
    project = desktop.open()
    row = seed_project_material(desktop, project, category="film")
    monkeypatch.setattr("coat_sim.core.material_business.application_material_library",
                        lambda: pytest.fail("project asset material editor must not create a global picker"))
    route_id = desktop.adapter.desktop.open_project_module(project.workspace, "materials")
    route = next(route for route in desktop.adapter.host.routes(project.session.binding.project_id)
                 if route.module_key == "project_resources")
    window, assets, engine, *_ = desktop.adapter.desktop._views[route]
    settled(assets)
    # The logical open selects an explicitly film-eligible material after inspection.
    wait_for_material_idle(assets._material_controller.bridge, "glass", "project material selection")
    assert route_id == route.route_id
    assert all(route.module_key != "materials" for route in desktop.adapter.host.routes(project.session.binding.project_id))
    editor = assets._material_controller
    assert editor is not None and editor.bridge is assets.materialLibrary
    assert editor.window is window and editor.project_context is project.workspace
    assert editor.bridge.currentId == "glass"
    assert window.property("editorInstance") is not None
    assert window.findChild(QQuickItem, "addOpticalSetButton") is not None
    assert window.findChild(QQuickItem, "materialOpticalDatasetsPage") is not None
    assert desktop.adapter.desktop.open_project_module(project.workspace, "materials") == route_id
    assert desktop.adapter.desktop.open_project_module(project.workspace, "project_resources") == route_id
    assert desktop.adapter.desktop.controller_for(project.workspace, "materials") is editor
    editor.bridge.setField("notes", "Retained exact material input")
    assert editor.dirty
    assert assets.browseLibrary()
    settled(assets)
    assert editor.bridge.draftData()["notes"] == "Retained exact material input"
    assert qml(engine, window, "informationId") == row["id"]


def test_material_snapshot_alias_keeps_identity_and_asset_target_cannot_write(desktop):
    project, other = desktop.open("First"), desktop.open("Second")
    row = seed_project_material(desktop, project)
    _identity, route, (window, assets, *_rest) = open_library(desktop, project)
    assert assets.openMaterialAsset(row["id"])
    QTest.qWait(80)
    live = SimpleNamespace()
    material = project_editor_snapshot(desktop.adapter, project.session, "materials", {},
                                      client_generation="client", bridge=live)
    assert material["available"], material
    assert material["instance_id"] == route.route_id
    catalog = project_editor_snapshot(desktop.adapter, project.session, "project_resources", {},
                                     client_generation="client", bridge=live)
    assert catalog["available"] and catalog["summary"]["material_editor"]["available"]
    assert catalog["summary"]["material_editor"]["logical_target"] == "materials"
    target = dict(project_id=project.session.binding.project_id, session_generation=project.session.binding.generation,
                  client_generation="client", project_root=str(project.session.binding.root),
                  project_file=str(project.store.project_file), instance_id=route.route_id)
    with pytest.raises(UiControlError) as blocked:
        project_editor_operation(desktop.adapter, project.session, "project_resources", "update_window",
            target | {"expected_snapshot_id": catalog["snapshot_id"], "fields": {"name": "blocked"}},
            client_generation="client", bridge=live)
    assert blocked.value.code == "unsupported_operation"
    with pytest.raises(UiControlError) as mismatched:
        project_editor_operation(desktop.adapter, project.session, "materials", "update_window",
            target | {"expected_snapshot_id": catalog["snapshot_id"], "fields": {"name": "blocked"}},
            client_generation="client", bridge=live)
    assert mismatched.value.code == "stale_snapshot"
    accepted = project_editor_operation(desktop.adapter, project.session, "materials", "update_window",
        target | {"expected_snapshot_id": material["snapshot_id"], "fields": {"name": "Edited logical material"}},
        client_generation="client", bridge=live)
    assert accepted["accepted"]
    assert assets._material_controller.bridge.draftData()["name"] == "Edited logical material"
    assert not project_editor_snapshot(desktop.adapter, other.session, "materials", {},
                                      client_generation="client", bridge=live)["available"]


def test_global_asset_material_editor_retains_optical_dataset_editing(global_root):
    MaterialLibrary(directory=global_root / "data/materials").write(profile())
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    assets: Any = GlobalResourcesBridge()
    engine.rootContext().setContextProperty("bridge", assets)
    from pathlib import Path
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path(__file__).parents[1] / "coat_sim/ui/qml/ResourceLibraryWindow.qml")))
    window = cast(Any, component.create())
    try:
        assert window is not None, component.errorString()
        window.resize(1100, 760)
        window.show()
        settled(assets)
        row = next(row for row in assets.transferCatalog["rows"] if row["kind"] == "material_profile")
        assert assets.openMaterialAsset(row["id"])
        spin_until(lambda: assets.materialLibrary.loading, lambda value: not value, "global material editor load")
        QTest.qWait(80)
        assert assets.materialLibrary.currentId == "glass"
        assert window.findChild(QQuickItem, "addOpticalSetButton") is not None
        assert assets.materialLibrary.editOpticalMetadata("set", "Edited dataset name", "Dataset notes retained")
        assert assets.materialLibrary.save()
        assert MaterialLibrary(directory=global_root / "data/materials").read("glass")["record"]["optical_constants"][0]["name"] == "Edited dataset name"
        expression = QQmlExpression(QQmlEngine.contextForObject(window), window, "[resourcePanes.width, resourceBrowser.width, resourceBrowser.implicitWidth, resourceBrowser.Layout.preferredWidth, assetDetailPane.width, assetDetailPane.Layout.fillWidth]")
        geometry = expression.evaluate()[0].toVariant()
        assert not expression.hasError(), expression.error().toString()
        assert geometry[1] < 400 and geometry[4] > 600, geometry
        assert not window.grabWindow().isNull()
        assert window.grabWindow().save("/tmp/unified-global-asset-material-editor.png")
    finally:
        assets.dispose()
        window.deleteLater()
        engine.deleteLater()
        drain_events()


def test_logical_material_rows_share_physical_asset_instance_and_reject_other_owner(desktop):
    from PySide6.QtCore import QObject
    from coat_sim.ui.project_application_client import ProjectApplicationClient
    first, second = desktop.open("First"), desktop.open("Second")
    seed_project_material(desktop, first)
    instance = desktop.adapter.desktop.open_project_module(first.workspace, "materials")
    bridge: Any = QObject()
    bridge.project_runtime = desktop.adapter
    bridge.project_lifecycle = None
    client = ProjectApplicationClient(bridge, SimpleNamespace(dispatch=lambda *_: {"windows": []}))
    try:
        args = {"project_id": first.session.binding.project_id,
                "session_generation": first.session.binding.generation, "module_key": "materials",
                "client_generation": client.generation, "instance_id": instance}
        rows = client.dispatch("list_windows", {"project_id": first.session.binding.project_id,
            "session_generation": first.session.binding.generation})["windows"]
        assert [row["module_key"] for row in rows].count("project_resources") == 1
        assert not any(row["module_key"] == "materials" for row in rows)
        logical = client.dispatch("read_window", args)
        assert logical["instance_id"] == instance and logical["physical_module"] == "project_resources"
        assert "update_window" in logical["capabilities"]["editor_operations"]
        with pytest.raises(UiControlError) as error:
            client.dispatch("read_window", args | {"project_id": second.session.binding.project_id,
                "session_generation": second.session.binding.generation})
        assert error.value.code == "stale_window"
    finally:
        client.stop()
        bridge.deleteLater()


def test_material_alias_read_only_fence_and_retained_close_reopen_preserve_input(desktop):
    project = desktop.open()
    row = seed_project_material(desktop, project)
    _instance, route, (window, assets, *_rest) = open_library(desktop, project)
    assert assets.openMaterialAsset(row["id"])
    controller = assets._material_controller
    controller.bridge._read_only = True
    live = SimpleNamespace()
    snapshot = project_editor_snapshot(desktop.adapter, project.session, "materials", {},
                                      client_generation="readonly", bridge=live)
    with pytest.raises(UiControlError) as error:
        project_editor_operation(desktop.adapter, project.session, "materials", "update_window",
            {"project_id": project.session.binding.project_id,
             "session_generation": project.session.binding.generation, "client_generation": "readonly",
             "project_root": str(project.session.binding.root), "project_file": str(project.store.project_file),
             "instance_id": route.route_id, "expected_snapshot_id": snapshot["snapshot_id"],
             "fields": {"name": "must not apply"}}, client_generation="readonly", bridge=live)
    assert error.value.code == "read_only_window"
    controller.bridge._read_only = False
    controller.bridge.setField("notes", "Exact retained input after keep-close")
    assert not window.close()
    assert controller.confirmClose("keep")
    drain_events()
    assert controller.window is None
    reopened = desktop.adapter.desktop.open_project_module(project.workspace, "materials")
    new_route = next(item for item in desktop.adapter.host.routes(project.session.binding.project_id)
                     if item.module_key == "project_resources")
    new_window, new_assets, *_ = desktop.adapter.desktop._views[new_route]
    settled(new_assets)
    assert reopened != route.route_id
    assert new_assets._material_controller is controller
    assert controller.window is new_window
    assert controller.bridge.draftData()["notes"] == "Exact retained input after keep-close"
    assert controller.dirty
    assert controller.confirmClose("discard")
    drain_events()
    assert not controller.dirty


def test_legacy_live_material_window_is_activated_without_migrating_its_buffer(desktop):
    from coat_sim.ui.project_resource_adapter import create_project_resource_view
    project = desktop.open()
    resources = ProjectResourceWorkspace.for_project(desktop.adapter.host, project.session.binding)
    asset_instance, _route, (asset_window, assets, engine, *_rest) = open_library(desktop, project)
    legacy, controller, context, component = create_project_resource_view(project.workspace, "materials", engine,
                                                                         desktop.adapter.desktop)
    try:
        controller.bridge.setField("notes", "Legacy uncommitted material input")
        legacy.show()
        assert not assets.openMaterialSection()
        assert controller.bridge.draftData()["notes"] == "Legacy uncommitted material input"
        assert controller.window is legacy and resources.windows["materials"] is legacy
        assert legacy.isVisible() and "已有材料编辑窗口" in assets.libraryMessage
        assert asset_instance and asset_window.isVisible()
    finally:
        legacy.setProperty("projectCloseAuthorized", True)
        legacy.close()
        legacy.deleteLater()
        controller.detach_view()
        drain_events()
        assert context is not None and component is not None


def test_global_material_route_uses_one_asset_window_and_dirty_shutdown_review(tmp_path):
    import time
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication
    from coat_sim.ui import launcher as launcher_module
    from tests.launcher_test_isolation import LauncherTestIsolation
    app = QApplication.instance() or QApplication([])
    isolation = LauncherTestIsolation(tmp_path / "isolated", launcher_module).apply()
    global_profile = profile()
    global_profile["category"] = "film"
    MaterialLibrary(directory=isolation.data_root / "materials").write(global_profile)
    engine = QQmlApplicationEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    launcher = launcher_module.LauncherBridge(engine, with_launcher=False,
                                              window_settings=isolation.create_settings())
    engine.rootContext().setContextProperty("launcher", launcher)
    errors: list[str] = []
    launcher.launchError.connect(errors.append)
    try:
        identity = launcher.open_window("materials")
        deadline = time.monotonic() + 5
        while identity not in launcher._windows and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert identity in launcher._windows, errors
        handle = launcher._windows[identity]
        assets = cast(Any, handle.bridge)
        settled(assets)
        wait_for_material_idle(assets.materialLibrary, None, "global alias load")
        assert handle.key == "global_resources" and assets.materialLibrary.currentId
        material_row = next(row for row in assets.transferCatalog["rows"]
                            if row["kind"] == "material_profile" and row.get("material_id") == "glass")
        assert assets.openMaterialAsset(material_row["id"])
        wait_for_material_idle(assets.materialLibrary, "glass", "selected global material")
        assert assets.materialLibrary.currentId == "glass"
        assert launcher.open_window("materials") == launcher.open_window("global_resources") == identity
        wait_for_material_idle(assets.materialLibrary, "glass", "reused global material")
        assert not any(option["key"] == "materials" for option in launcher.property("modules"))
        assert not any(window.key == "materials" for window in launcher._windows.values())
        assets.materialLibrary.setField("notes", "Preserve global unsaved input")
        assert assets.dirty
        assert not handle.window.property("allowClose")
        assert launcher._runtime_shutdown_callback is None
        launcher.shutdown()
        assert not launcher._closing and launcher._shutdown_confirmation_pending
        launcher.cancelShutdown()
        assert assets.materialLibrary.draftData()["notes"] == "Preserve global unsaved input"
        assets.materialLibrary._operation_busy = True
        launcher.shutdown()
        assert not launcher._closing and errors and "正在提交" in errors[-1]
        assets.materialLibrary._operation_busy = False
    finally:
        for handle in list(launcher._windows.values()):
            handle.window.setProperty("allowClose", True)
            handle.window.close()
        launcher.shutdown()
        launcher.deleteLater()
        engine.deleteLater()
        app.processEvents()
        isolation.stop()


def test_optical_only_asset_browses_clean_and_first_edit_saves_existing_optics_with_new_profile(desktop):
    from copy import deepcopy
    from tests.test_project_global_resources import optical_material
    project = desktop.open()
    store = desktop.adapter.host.store_for(project.session.binding)
    identity = store.create_document("optical_material", optical_material("optics-only"))
    before = deepcopy(store.manifest)
    _instance, route, (window, assets, *_rest) = open_library(desktop, project)
    assert assets.openMaterialAsset(identity)
    library, controller = assets.materialLibrary, assets._material_controller
    assert not library.dirty and not controller.dirty and store.manifest == before
    assert library.currentId == "optics-only" and library._saved_id is None
    assert controller.currentDocumentId == ""  # Recovery continues to refer only to persisted profiles.
    assert library.projectionAssetId == identity
    assert window.property("informationId") == identity
    snapshot = project_editor_snapshot(desktop.adapter, project.session, "materials", {},
                                      client_generation="optical", bridge=SimpleNamespace())
    assert snapshot["available"] and snapshot["summary"]["source_asset_id"] == identity
    assert snapshot["summary"]["project"] and not snapshot["summary"]["profile_persisted"]
    assert assets.previewAssetDeletion([identity])
    spin_until(lambda: assets.deletionBusy, lambda busy: not busy, "clean optical preview")
    assert assets.deletionPlan["can_apply"], assets.deletionPlan
    assets.cancelAssetDeletion()
    dialog = window.findChild(QObject, "assetDeletionPreviewDialog")
    dialog.close()
    library.setField("notes", "First real optical edit")
    assert library.dirty
    recovered = controller.snapshot()
    assert recovered["document_id"] is None and recovered["editor_state"]["_saved_id"] is None
    assert identity in recovered["editor_state"]["_selected_revisions"]
    # Existing recovery fields re-establish exact optical provenance without a
    # new recovery schema or a fabricated material profile id.
    library._asset_projection_document_id = library._asset_projection_material_id = ""
    controller.restore_snapshot(recovered)
    assert library.projectionAssetId == identity and library.currentId == "optics-only"
    assert library.save()
    rows = store.manifest["documents"]
    assert len([row for row in rows if row["kind"] == "material_profile"]) == 1
    assert [row["id"] for row in rows if row["kind"] == "optical_material"] == [identity]
    assert assets.service.load_document(identity).data["material"]["notes"] == "First real optical edit"
