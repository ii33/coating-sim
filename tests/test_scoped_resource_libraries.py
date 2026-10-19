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


def write_test_material(library, record):
    """Publish a validated fixture without depending on Windows hard links."""
    path = library.directory / (record["id"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(record))


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
        assert categories == [
            {"key": "material_profile", "label": "材料"},
            {"key": "source", "label": "蒸发源"},
            {"key": "mask", "label": "修正板"},
        ]
        assert all(row["key"] and "全部" not in row["label"] for row in categories)
        assert qml(engine, window, "kindLabel('material_profile')") == "材料"
        assert qml(engine, window, "visibleRows.map(row => row.kind)") == ["source"]
        source = bridge.transferCatalog["rows"][0]
        qml(engine, window, f"choose('{source['id']}', true)")
        assert qml(engine, window, "selectedIds.length") == 1
        category = cast(Any, window.findChild(QQuickItem, "globalResourcesCategory"))
        mask_index = next(index for index, row in enumerate(categories) if row["key"] == "mask")
        category.setProperty("currentIndex", mask_index)
        category.activated.emit(mask_index)
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
    assert window.property("title") == "工程资产库 — First" and window.property("librarySource")
    assert window.findChild(QQuickItem, "projectModuleSelector").property("text") == "工程资产库"
    assert desktop.adapter.desktop.open_project_module(first.workspace, "project_resources") == route_id
    assert desktop.adapter.host.window_for(route) is window
    _other_id, _other_route, other_view = open_library(desktop, second)
    assert other_view[1] is not bridge
    assert other_view[0].property("title") == "工程资产库 — Second"
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
    window.setProperty("categoryKey", "process")
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


def test_material_browser_uses_explicit_multi_membership_categories(global_root):
    from coat_sim.core.material_business import MaterialLibrary
    from coat_sim.core.materials import default_material_profile
    from tests.test_project_global_resources import profile

    library = MaterialLibrary(directory=global_root / "data/materials")
    for identity, name, roles in (
            ("film", "Film", ["film"]),
            ("bk7", "BK7", ["substrate"]),
            ("fused_silica", "熔融石英", ["substrate"]),
            ("air_film", "Air", ["film", "medium"]),
            ("air_substrate", "Air", ["substrate"]),
            ("both", "Dual use", ["film", "substrate"]),
            ("boat", "Boat", ["boat"]),
            ("component", "Component", ["component"]),
            ("medium_only", "Medium only", ["medium"])):
        record = profile(identity)
        record["name"] = name
        record["roles"] = roles
        write_test_material(library, record)

    # Neither absent purpose metadata nor optical-only data implies a browser pool.
    write_test_material(library, default_material_profile("No purpose", id="no_purpose"))
    write_test_material(library, default_material_profile("Optical only", id="optical_only", optical_only=True))
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
        assert qml(engine, window, "materialUses.map(row => row.label)") == ["膜料", "基片材料"]
        assert qml(engine, window, "materialUses.map(row => row.key)") == ["film", "substrate"]
        rows = {row.get("material_id", row["id"]): row for row in bridge.transferCatalog["rows"]
                if row["kind"] == "material_profile"}
        assert rows["air_film"]["material_categories"] == ["film"]
        assert rows["air_substrate"]["material_categories"] == ["substrate"]
        assert rows["air_film"]["material_uses"] == ["film", "medium"]
        assert rows["medium_only"]["material_categories"] == []
        assert rows["medium_only"]["material_uses"] == ["medium"]
        assert rows["boat"]["material_categories"] == []
        assert rows["boat"]["material_uses"] == ["other"]
        assert rows["component"]["material_categories"] == []
        assert rows["no_purpose"]["material_categories"] == []
        assert rows["optical_only"]["material_categories"] == []
        assert rows["both"]["material_categories"] == ["film", "substrate"]
        selector = cast(Any, window.findChild(QQuickItem, "resourceLibraryMaterialUse"))
        for width in (680, 1100):
            window.resize(width, 760)
            QTest.qWait(60)
            assert set(qml(engine, window, "visibleRows.map(row => row.material_id)")) == {"film", "air_film", "both"}
            selector.setProperty("currentIndex", 1)
            selector.activated.emit(1)
            assert set(qml(engine, window, "visibleRows.map(row => row.material_id)")) == {
                "bk7", "fused_silica", "air_substrate", "both"
            }
            selector.setProperty("currentIndex", 0)
            selector.activated.emit(0)
        assert qml(engine, window, "categories.map(row => row.label)") == ["材料"]
    finally:
        bridge.dispose()
        window.deleteLater()
        engine.deleteLater()
        drain_events()


def test_medium_picker_deduplicates_air_and_keeps_currently_bound_material():
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController

    def medium_record(identity):
        return {"id": identity, "name": "Air", "optical_sets": [{"id": identity + "_set"}]}

    film_air = medium_record("air_film")
    substrate_air = medium_record("air_substrate")
    state = FilmDesignQuickController._build_picker_state(
        {"film": [film_air], "substrate": [substrate_air], "medium": [film_air, film_air]},
        {"incident_medium": {"kind": "material", "material_id": "air_substrate"},
         "exit_medium": {"kind": "material", "material_id": "air_substrate"}})

    medium_ids = [row["id"] for row in state["medium_options"]]
    assert medium_ids == ["air_film", "air_substrate"]
    assert len(medium_ids) == len(set(medium_ids))
    assert {row["id"] for row in state["medium_picker_options"]} == set(medium_ids)


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


def test_unclassified_optical_record_stays_preserved_without_a_film_category(desktop):
    from tests.test_project_global_resources import optical_material
    project = desktop.open()
    store = desktop.adapter.host.store_for(project.session.binding)
    identity = store.create_document("optical_material", optical_material("standalone-optics"))
    _id, _route, (window, bridge, engine, *_rest) = open_library(desktop, project)
    assert qml(engine, window, "categories") == [{"key": "material_profile", "label": "材料"}]
    assert qml(engine, window, "visibleRows.map(row => row.id)") == []
    assert bridge.openMaterialAsset(identity)
    QTest.qWait(100)
    assert window.property("categoryKey") == "material_profile"
    assert qml(engine, window, "visibleRows.map(row => row.id)") == []
    editor = window.property("editorInstance")
    assert editor is not None
    toolbar = editor.findChild(QQuickItem, "materialLibraryActionToolbar")
    assert toolbar is not None and not toolbar.isVisible()
    assert window.findChild(QQuickItem, "assetLibrarySaveMaterialButton").isVisible()
    bridge.materialLibrary.setField("notes", "Synthetic optical-only edited")
    assert bridge.materialLibrary.dirty
    assert bridge.materialResources.saveCurrent()
    QTest.qWait(100)
    assert store.manifest["documents"]  # No asset was removed to merge categories.


def test_library_copy_global_project_package_and_classification_checks(desktop, global_root, tmp_path):
    seed_source(global_root)
    project = desktop.open()
    _id, _route, (window, bridge, _engine, *_rest) = open_library(desktop, project)
    store = desktop.adapter.host.store_for(project.session.binding)
    assert bridge.browseSource("global")
    settled(bridge)
    source = next(row for row in bridge.transferCatalog["rows"] if row["kind"] == "source")
    window.setProperty("categoryKey", "mask")
    assert not bridge.previewLibraryTransfer([source["id"]], "project")
    assert not store.manifest["documents"]
    window.setProperty("categoryKey", "source")
    assert not bridge.previewLibraryTransfer([], "project")
    assert bridge.previewLibraryTransfer([source["id"]], "project")
    settled(bridge)
    assert bridge.applyTransfer({})
    settled(bridge)
    QTest.qWait(50)
    settled(bridge)
    current = next(row for row in bridge.transferCatalog["rows"] if row["kind"] == "source")
    assert bridge.previewLibraryTransfer([current["id"]], "global")
    settled(bridge)
    assert bridge.transferPlan["rows"][0]["conflict"] in {"", "identical"}
    assert bridge.applyTransfer({})
    settled(bridge)
    QTest.qWait(50)
    settled(bridge)
    current = next(row for row in bridge.transferCatalog["rows"] if row["kind"] == "source")
    destination = tmp_path / "selected.cproject"
    assert bridge.previewLibraryTransfer([current["id"]], str(destination))
    settled(bridge)
    assert bridge.applyTransfer({})
    settled(bridge)
    assert destination.is_file()
    assert bridge.browseSource(str(tmp_path / "missing.cproject"))
    settled(bridge)
    assert bridge.libraryFailed and bridge.transferStatus


def test_library_conflict_requires_choice_and_failed_apply_preserves_owner(desktop, tmp_path):
    from coat_sim.core.project_store import ProjectStore
    from tests.test_project_global_resources import optical_material
    project = desktop.open()
    store = desktop.adapter.host.store_for(project.session.binding)
    store.create_document("optical_material", optical_material("shared", n=3.0))
    donor = ProjectStore.create(tmp_path / "conflict-donor", "Donor")
    identity = donor.create_document("optical_material", optical_material("shared", n=2.0))
    _id, _route, (window, bridge, engine, *_rest) = open_library(desktop, project)
    assert bridge.browseSource(str(donor.project_file))
    settled(bridge)
    assert window.property("categoryKey") == "material_profile"
    assert bridge.previewLibraryTransfer([identity], "project")
    settled(bridge)
    assert bridge.transferPlan["rows"][0]["conflict"] == "different"
    assert not qml(engine, window, "planChoicesResolved")
    before = store.manifest
    assert bridge.applyTransfer({})
    settled(bridge)
    assert bridge.libraryFailed and bridge.transferStatus
    assert store.manifest == before
    assert bridge.previewLibraryTransfer([identity], "project")
    settled(bridge)
    assert bridge.applyTransfer({identity: "copy"})
    settled(bridge)
    assert len([row for row in store.manifest["documents"] if row["kind"] == "optical_material"]) == 2
