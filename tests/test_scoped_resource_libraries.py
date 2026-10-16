"""Actual classified libraries retain explicit current-project ownership."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression, QJSValue
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.project_dependencies import json_bytes
from coat_sim.control_errors import UiControlError
from coat_sim.project_ui import project_window_arguments
from coat_sim.ui.global_resources import GlobalResourcesBridge
from coat_sim.ui.project_editor_operations import project_editor_capabilities, project_editor_operation
from coat_sim.ui.project_editor_snapshot import project_editor_snapshot
from coat_sim.ui.project_resources import capture_project_resources_snapshot
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.test_input_snapshot import fixture
from tests.test_launcher_information_global_resources import global_root as global_root, seed_source
from tests.test_project_last_window_close import desktop as desktop, drain_events, spin_until

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def settled(bridge):
    QTest.qWait(20)
    spin_until(lambda: bridge.transferBusy, lambda value: not value, "resource task")


def qml(engine, scope, expression):
    expr = QQmlExpression(engine.rootContext(), scope, expression)
    result = expr.evaluate()
    assert not expr.hasError(), expr.error().toString()
    value = result[0] if isinstance(result, tuple) else result
    return value.toVariant() if isinstance(value, QJSValue) else value


def open_library(desktop, project):
    route_id = desktop.adapter.desktop.open_project_module(project.workspace, "project_resources")
    route = next(row for row in desktop.adapter.host.routes(project.session.binding.project_id)
                 if row.module_key == "project_resources")
    view = desktop.adapter.desktop._views[route]
    settled(view[1])
    return route_id, route, view


def test_classification_has_no_all_entry_switches_actual_rows_and_clears_hidden_selection(global_root):
    seed_source(global_root)
    mask = global_root / "data/masks/other.cmsk"
    mask.parent.mkdir(parents=True)
    mask.write_bytes(json_bytes({"z_mm": 50., "outer": [[0., 0.], [1., 0.], [0., 1.]], "holes": []}))
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    bridge = cast(Any, GlobalResourcesBridge())
    engine.rootContext().setContextProperty("bridge", bridge)
    path = Path(__file__).parents[1] / "coat_sim/ui/qml/GlobalResourcesWindow.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
    window = cast(Any, component.create())
    try:
        assert window is not None, component.errorString()
        window.show()
        settled(bridge)
        categories = qml(engine, window, "categories")
        assert categories == [{"key": "source", "label": "蒸发源"}, {"key": "mask", "label": "修正板"}]
        assert all(row["key"] and "全部" not in row["label"] for row in categories)
        assert qml(engine, window, "kindLabel('material_profile')") == "材料"
        assert qml(engine, window, "visibleRows.map(row => row.kind)") == ["source"]
        source = bridge.transferCatalog["rows"][0]
        qml(engine, window, f"choose('{source['id']}', true)")
        assert qml(engine, window, "selectedIds.length") == 1
        category = cast(Any, window.findChild(QQuickItem, "globalResourcesCategory"))
        category.setProperty("currentIndex", 1)
        category.activated.emit(1)
        assert qml(engine, window, "visibleRows.map(row => row.kind)") == ["mask"]
        assert qml(engine, window, "selectedIds") == []
        assert window.property("informationId") == ""
        assert "全部分类" not in path.with_name("ResourceLibraryWindow.qml").read_text()
    finally:
        bridge.dispose()
        window.deleteLater()
        engine.deleteLater()
        drain_events()


def test_project_window_lazy_single_instance_and_cross_project_isolation(desktop, monkeypatch):
    first, second = desktop.open("First"), desktop.open("Second")
    first_store = desktop.adapter.host.store_for(first.session.binding)
    second_store = desktop.adapter.host.store_for(second.session.binding)
    first_id = first_store.create_document("source", {"schema_version": 2, "kind": "point",
        "device_type": "custom", "source_exponent": 1., "name": "Only First"})
    second_store.create_document("source", {"schema_version": 2, "kind": "point",
        "device_type": "custom", "source_exponent": 2., "name": "Only Second"})
    assert all(row.module_key != "project_resources" for row in desktop.adapter.host.routes(first.session.binding.project_id))
    monkeypatch.setattr("coat_sim.core.project_global_resources.read_global_resources",
                        lambda: pytest.fail("default project browse must not read global resources"))
    route_id, route, (window, bridge, engine, *_rest) = open_library(desktop, first)
    assert bridge.libraryScope == "project" and bridge.owner_binding == first.session.binding
    assert bridge.service.store is first_store
    assert [row["name"] for row in bridge.transferCatalog["rows"]] == ["Only First"]
    assert window.property("title") == "工程资产库" and window.property("librarySource")
    assert window.findChild(QQuickItem, "projectModuleSelector").property("text") == "工程资产库"
    assert desktop.adapter.desktop.open_project_module(first.workspace, "project_resources") == route_id
    assert desktop.adapter.host.window_for(route) is window
    _other_id, _other_route, other_view = open_library(desktop, second)
    assert other_view[1] is not bridge
    assert [row["name"] for row in other_view[1].transferCatalog["rows"]] == ["Only Second"]
    qml(engine, window, f"choose('{first_id}', true)")
    state, reason = capture_project_resources_snapshot(bridge, window, "project_resources")
    assert reason is None and state["summary"]["selected_ids"] == [first_id]
    assert [row["name"] for row in state["collections"]["resources"]] == ["Only First"]
    capabilities = project_editor_capabilities("project_resources")
    assert capabilities["editor_operations"] == [] and capabilities["scope"] == "read_only_window"
    project_window_arguments("project_editor_snapshot", {"window_type": "project_resources", "detail": "resources"})
    snapshot = project_editor_snapshot(desktop.adapter, first.session, "project_resources", {"detail": "resources"},
        client_generation="test", bridge=SimpleNamespace())
    assert snapshot["available"] and snapshot["page"]["items"][0]["name"] == "Only First"
    other_snapshot = project_editor_snapshot(desktop.adapter, second.session, "project_resources", {"detail": "resources"},
        client_generation="test", bridge=SimpleNamespace())
    assert other_snapshot["snapshot_id"] != snapshot["snapshot_id"]
    with pytest.raises(UiControlError, match="陈旧|已改变|重新读取"):
        project_editor_snapshot(desktop.adapter, first.session, "project_resources",
            {"detail": "resources", "expected_snapshot_id": other_snapshot["snapshot_id"]},
            client_generation="test", bridge=SimpleNamespace())
    before_write = first_store.manifest
    for operation, fields in (("update_window", {"fields": {"name": "blocked"}}),
                              ("window_action", {"action": "save", "arguments": {}})):
        with pytest.raises(UiControlError) as rejected:
            project_editor_operation(desktop.adapter, first.session, "project_resources", operation,
                {"instance_id": route_id, "expected_snapshot_id": snapshot["snapshot_id"], **fields},
                client_generation="test", bridge=SimpleNamespace())
        assert rejected.value.code == "unsupported_operation"
    assert first_store.manifest == before_write
    # Render the real project page at minimum and restored width.
    for width in (680, 1100):
        window.resize(width, 760)
        QTest.qWait(60)
        bar = window.findChild(QQuickItem, "resourceLibraryOperationBar")
        assert bar is not None and bar.width() <= window.width()
    image = window.grabWindow()
    assert not image.isNull()
    assert image.save("/tmp/scoped-resources-preview.png")


def test_project_import_transfers_selected_dependency_closure_only_to_current_owner(desktop, tmp_path):
    first, second = desktop.open("Target"), desktop.open("Unrelated")
    external = tmp_path / "external"
    fixture(external)
    _identity, _route, (window, bridge, *_rest) = open_library(desktop, first)
    first_store = desktop.adapter.host.store_for(first.session.binding)
    second_store = desktop.adapter.host.store_for(second.session.binding)
    before_second = second_store.manifest
    before_film = first.workspace.editor.snapshot()
    assert bridge.browseSource(str(external / "recipe.cproc"))
    settled(bridge)
    process = next(row for row in bridge.transferCatalog["rows"] if row["kind"] == "process")
    state, reason = capture_project_resources_snapshot(bridge, window, "project_resources")
    assert state is None and reason == "external_import_source"
    assert not bridge.previewLibraryTransfer([process["id"]], "global")
    assert bridge.previewLibraryTransfer([process["id"]], "project")
    settled(bridge)
    assert bridge.transferPlan["dependency_ids"]
    assert bridge.applyTransfer({})
    settled(bridge)
    assert first_store.manifest["documents"]
    assert second_store.manifest == before_second
    after_film = first.workspace.editor.snapshot()
    assert after_film.design == before_film.design
    assert after_film.design_revision == before_film.design_revision
    assert after_film.dirty == before_film.dirty
    spin_until(lambda: bridge.transferCatalog.get("source_path"),
               lambda value: value == str(first.store.project_file), "post-import current project refresh")
    settled(bridge)
    assert len(bridge.transferCatalog["rows"]) == 6
    destination = tmp_path / "selected.cproject"
    process_id = next(row["id"] for row in bridge.transferCatalog["rows"] if row["kind"] == "process")
    assert bridge.previewLibraryTransfer([process_id], str(destination))
    settled(bridge)
    assert bridge.applyTransfer({})
    settled(bridge)
    assert destination.is_file()


def test_library_last_window_preserves_retained_dirty_editor_and_close_review(desktop):
    project = desktop.open()
    assert project.workspace.editor.set_design_value("name", "Retained unsaved change")
    _identity, route, (window, bridge, *_rest) = open_library(desktop, project)
    project.window.setProperty("allowClose", True)
    assert project.window.close()
    spin_until(lambda: desktop.adapter.host.routes(project.session.binding.project_id),
               lambda routes: routes == (route,), "library is last project window")
    assert not window.close()
    spin_until(lambda: desktop.prompts, lambda prompts: prompts == [project.session.binding.project_id], "retained editor close review")
    assert not project.lease._closed and not bridge._disposed
    desktop.adapter.cancelCloseProject(project.session.binding.project_id)
    assert window.isVisible() and project.workspace.editor.snapshot().dirty
    assert not window.close()
    spin_until(lambda: len(desktop.prompts), lambda count: count == 2, "second close review")
    assert desktop.adapter.confirmCloseProject(project.session.binding.project_id)
    drain_events()
    assert project.lease._closed


def test_material_usage_keeps_film_substrate_medium_and_unknown_reachable(global_root):
    from coat_sim.core.material_business import MaterialLibrary
    from coat_sim.core.materials import default_material_profile
    from tests.test_project_global_resources import profile
    library = MaterialLibrary(directory=global_root / "data/materials")
    for identity, roles in (("film", ["film"]), ("glass", ["substrate"]),
                            ("air", ["medium"]), ("both", ["film", "substrate"]),
                            ("boat", ["boat"])):
        record = profile(identity)
        record["name"] = identity
        record["roles"] = roles
        library.write(record)
    # A legacy unclassified profile retains the existing library's film default.
    library.write(default_material_profile("Legacy", id="legacy"))
    library.write(default_material_profile("Optical only", id="optical_only", optical_only=True))
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    bridge = cast(Any, GlobalResourcesBridge())
    engine.rootContext().setContextProperty("bridge", bridge)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path(__file__).parents[1] / "coat_sim/ui/qml/ResourceLibraryWindow.qml")))
    window = cast(Any, component.create())
    try:
        assert window is not None, component.errorString()
        window.show()
        settled(bridge)
        assert window.property("categoryKey") == "material_profile", (bridge.transferStatus, bridge.transferCatalog)
        assert window.property("materialUse") == "film"
        assert qml(engine, window, "materialUses.map(row => row.label)") == ["膜料", "基片材料", "介质", "其他材料", "用途未标注"]
        assert set(qml(engine, window, "visibleRows.map(row => row.name)")) == {"film", "both", "Legacy"}
        selector = cast(Any, window.findChild(QQuickItem, "resourceLibraryMaterialUse"))
        for index, names in ((1, {"glass", "both"}), (2, {"air"}), (3, {"boat"}), (4, {"Optical only"})):
            selector.setProperty("currentIndex", index)
            selector.activated.emit(index)
            assert set(qml(engine, window, "visibleRows.map(row => row.name)")) == names
        assert qml(engine, window, "categories.map(row => row.label)") == ["材料", "光学材料数据"]
    finally:
        bridge.dispose()
        window.deleteLater()
        engine.deleteLater()
        drain_events()


def test_resource_library_is_exposed_in_mcp_read_schema_without_write_capabilities():
    import asyncio
    from coat_sim.mcp import server
    from coat_sim.project_modules import PROJECT_MODULES
    registered = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    tool = registered["project_editor_snapshot"]
    schema = getattr(tool, "input_schema", None) or tool.inputSchema
    assert {key for key, _ in PROJECT_MODULES} <= set(schema["properties"]["window_type"]["enum"])
    assert "resources" in schema["properties"]["detail"]["enum"]
    assert project_editor_capabilities("project_resources")["actions"] == []


def test_project_resource_publication_fences_whole_project_close_and_exit(desktop):
    project = desktop.open()
    _identity, route, (window, bridge, *_rest) = open_library(desktop, project)
    before_routes = desktop.adapter.host.routes(project.session.binding.project_id)
    bridge._transfer_applying = True
    bridge.transferChanged.emit()
    try:
        assert not window.close()
        assert not desktop.adapter.settled_for_exit()
        assert not desktop.adapter.closeProject(project.session.binding.project_id)
        assert "资源正在提交" in desktop.adapter.projectError
        assert desktop.adapter.host.routes(project.session.binding.project_id) == before_routes
        assert project.window.isVisible() and window.isVisible()
        assert not project.lease._closed and not bridge._disposed
    finally:
        bridge._transfer_applying = False
        bridge.transferChanged.emit()
    assert desktop.adapter.settled_for_exit()
    assert desktop.adapter.closeProject(project.session.binding.project_id)
    drain_events()
    assert project.lease._closed


def test_exported_optical_only_material_does_not_invent_film_suitability(global_root, tmp_path):
    from coat_sim.core.global_resource_library import GlobalResourceLibrary
    from coat_sim.core.project_resource_service import ProjectResourceService
    from coat_sim.core.project_store import ProjectStore
    from tests.test_project_global_resources import optical_material
    store = ProjectStore.create(tmp_path / "optical-donor", "Optical donor")
    identity = store.create_document("optical_material", optical_material("unclassified"))
    donor = ProjectResourceService(store)
    global_library = GlobalResourceLibrary()
    try:
        catalog = donor.inspect_resources("project")
        plan = donor.preview_resource_transfer(catalog["catalog_id"], [identity], "global")
        donor.apply_resource_transfer(plan["plan_id"])
        rows = global_library.inspect_resources()["rows"]
        profile_row = next(row for row in rows if row["kind"] == "material_profile")
        assert profile_row["material_uses"] == ["unspecified"]
        assert any(row["kind"] == "optical_material" for row in rows)
    finally:
        donor.close_resource_exchange()
        global_library.close()
