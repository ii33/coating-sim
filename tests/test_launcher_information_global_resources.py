"""Launcher metadata projection and project-free global library boundaries."""
from __future__ import annotations

import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtWidgets import QApplication

from coat_sim.core import data_files, material_business
from coat_sim.core.global_resource_library import GlobalResourceLibrary
from coat_sim.core.project_dependencies import ProjectError, json_bytes
from coat_sim.core.project_store import ProjectStore
from coat_sim.ui.global_resources import GlobalResourcesBridge
from coat_sim.ui.launcher_projects import ProjectLauncherAdapter
from coat_sim.ui.project_runtime_owner import ProjectRuntimeOwner
from coat_sim.ui.recent_projects import RecentProjects
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot

pytestmark = [pytest.mark.contract, pytest.mark.qt]


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def global_root(tmp_path, monkeypatch):
    root = tmp_path / "application"
    root.mkdir()
    monkeypatch.setattr(data_files, "PROJECT_ROOT", root)
    monkeypatch.setattr(data_files, "DATA_ROOT", root / "data")
    monkeypatch.setattr(material_business, "builtin_material_profiles", lambda: [])
    return root


def seed_source(root):
    path = root / "data" / "sources" / "named.csrc"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes({"schema_version": 2, "kind": "point", "device_type": "custom",
        "source_exponent": 1.0, "name": "Named source", "description": "Source summary", "notes": "Source notes"}))
    return path


def wait_bridge(app, bridge):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app.processEvents()
        bridge._poll_transfer_tasks()
        if not bridge.transferBusy:
            return
        time.sleep(0.005)
    pytest.fail("global resource task did not settle")


def test_recent_information_uses_descriptor_only_and_preserves_settings(tmp_path, app, monkeypatch):
    store = ProjectStore.create(tmp_path / "study", "Study", summary="Goal", notes="Constraints")
    history = RecentProjects(QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    history.record(store.project_file, "Old cache name")
    before = Path(history._settings.fileName()).read_bytes()
    monkeypatch.setattr(ProjectStore, "open", lambda *_args, **_kwargs: pytest.fail("history must not open a store"))
    row = history.entries()[0]
    assert (row["name"], row["summary"], row["notes"]) == ("Study", "Goal", "Constraints")
    assert row["informationAvailable"] is True
    assert Path(history._settings.fileName()).read_bytes() == before


def test_launcher_forwards_information_without_owning_store(tmp_path, app):
    calls = []
    def create(name, root, **information):
        calls.append((name, root, information))
        return SimpleNamespace(project_file=root / "project.coatproj")
    adapter = ProjectLauncherAdapter(recent_projects=RecentProjects(
        QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)), runtime_creator=create)
    try:
        destination = tmp_path / "not-created-by-launcher"
        assert adapter.createProjectWithInformation("Study", str(destination), "Goal", "Notes")
        assert adapter._pending is not None
        adapter._pending.result(timeout=2)
        adapter._complete_pending()
        assert calls == [("Study", destination, {"summary": "Goal", "notes": "Notes"})]
        assert not destination.exists()
        assert not hasattr(adapter, "host") and not hasattr(adapter, "_leases")
    finally:
        adapter.stop()


def test_runtime_creation_information_is_private_bounded_and_cleaned(tmp_path, monkeypatch):
    from coat_sim.ui import project_runtime_client as client
    shared = SimpleNamespace(ensure_running=lambda: None)
    information_paths = []
    def start(arguments, _shared, _application_root, _timeout):
        index = arguments.index("--create-information")
        path = Path(arguments[index + 1])
        information_paths.append(path)
        information = json.loads(path.read_text(encoding="utf-8"))
        assert information == {"summary": "目" * 20000, "notes": "注" * 20000}
        assert len(" ".join(arguments)) < 1000
        ProjectStore.create(Path(arguments[1]), arguments[3], **information)
    monkeypatch.setattr(client, "_start_runtime", start)
    monkeypatch.setattr(client, "_confirm_owner", lambda _shared, store: store)
    store = client.create_project_runtime("Study", tmp_path / "study", client=shared,
                                          summary="目" * 20000, notes="注" * 20000)
    assert len(store.manifest["notes"]) == 20000
    assert all(not path.exists() for path in information_paths)


def test_runtime_owner_creates_information_under_existing_admission_path(tmp_path):
    result = ProjectRuntimeOwner._read_store(tmp_path / "study", "Study", None,
        lambda current: current.manifest["summary"], summary="Goal", notes="Notes")
    assert isinstance(result, tuple)
    store, plan, lease = result
    try:
        assert plan == "Goal"
        assert store.manifest["notes"] == "Notes"
        assert lease.project_id == store.manifest["id"]
    finally:
        lease.close()


def test_global_manager_browses_information_exports_package_and_rejects_project_write(global_root, tmp_path):
    seed_source(global_root)
    library = GlobalResourceLibrary()
    try:
        before = {path: path.read_bytes() for path in global_root.rglob("*") if path.is_file()}
        catalog = library.inspect_resources()
        row = next(row for row in catalog["rows"] if row["kind"] == "source")
        assert (row["name"], row["summary"], row["notes"]) == ("Named source", "Source summary", "Source notes")
        assert {path: path.read_bytes() for path in global_root.rglob("*") if path.is_file()} == before
        with pytest.raises(ProjectError, match="new .cproject only"):
            library.preview_resource_transfer(catalog["catalog_id"], [row["id"]], str(tmp_path / "target.coatproj"))
        destination = tmp_path / "resources.cproject"
        preview = library.preview_resource_transfer(catalog["catalog_id"], [row["id"]], str(destination))
        result = library.apply_resource_transfer(preview["plan_id"])
        assert destination.is_file() and result["target_path"] == str(destination)
        from coat_sim.project_package import read_project_package
        snapshot = read_project_package(destination)
        assert next(iter(snapshot["documents"].values()))["notes"] == "Source notes"
    finally:
        library.close()
    with pytest.raises(ProjectError, match="closed"):
        library.inspect_resources()


def test_global_manager_imports_standalone_resource_without_project(global_root, tmp_path):
    source = tmp_path / "source.csrc"
    source.write_bytes(json_bytes({"schema_version": 2, "kind": "point", "device_type": "custom",
        "source_exponent": 1.0, "name": "Import", "description": "Goal", "notes": "Context"}))
    library = GlobalResourceLibrary()
    try:
        catalog = library.inspect_resources(str(source))
        preview = library.preview_resource_transfer(catalog["catalog_id"], [catalog["rows"][0]["id"]])
        result = library.apply_resource_transfer(preview["plan_id"])
        assert result["target_path"] == "global"
        assert not list(global_root.rglob("*.coatproj"))
        current = library.inspect_resources()
        assert any(row["name"] == "Import" and row["summary"] == "Goal" for row in current["rows"])
    finally:
        library.close()


def test_global_window_loads_real_bridge_and_async_catalog(app, global_root):
    seed_source(global_root)
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    bridge = GlobalResourcesBridge()
    engine.rootContext().setContextProperty("bridge", bridge)
    qml = Path(__file__).parents[1] / "coat_sim/ui/qml/GlobalResourcesWindow.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(qml)))
    window = component.create()
    try:
        assert window is not None, "\n".join(str(error) for error in component.errors())
        app.processEvents()
        wait_bridge(app, bridge)
        assert bridge.property("transferCatalog")["source_type"] == "global"
        assert bridge.property("transferCatalog")["rows"][0]["name"] == "Named source"
        assert bridge.property("libraryMessage") == ""
        assert window.property("globalSource") is True
    finally:
        bridge.dispose()
        if window is not None:
            window.deleteLater()
        engine.deleteLater()
        app.processEvents()


def test_launcher_qml_exposes_summary_notes_and_submits_information(app, tmp_path):
    from PySide6.QtCore import QObject
    from PySide6.QtQml import QQmlExpression
    store = ProjectStore.create(tmp_path / "existing", "Existing", summary="Existing goal", notes="Existing notes")
    history = RecentProjects(QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    history.record(store.project_file, "Existing")
    calls = []
    def create(name, root, **information):
        calls.append((name, root, information))
        return SimpleNamespace(project_file=root / "project.coatproj")
    adapter = ProjectLauncherAdapter(recent_projects=history, runtime_creator=create)
    for pending in adapter._history_requests.values():
        pending.result(timeout=2)
    adapter._complete_history()
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    engine.rootContext().setContextProperty("testProjects", adapter)
    component = QQmlComponent(engine)
    component.setData(b'''import QtQuick
import QtQuick.Controls
import "../coat_sim/ui/qml" as Pages
ApplicationWindow {
    width: 1000; height: 700
    Pages.LauncherProjectManager {
        objectName: "manager"; anchors.fill: parent
        projectBridge: testProjects; themeTokens: Theme.snapshot
    }
}''', QUrl.fromLocalFile(str(Path(__file__))))
    window = component.create()
    try:
        assert window is not None, "\n".join(str(error) for error in component.errors())
        manager = window.findChild(QObject, "manager")
        assert manager is not None
        assert window.findChild(QObject, "launcherProjectInformationSection") is None
        expression = QQmlExpression(engine.rootContext(), manager,
                                    "rowDetails(projectBridge.projectEntries[0])")
        details = expression.evaluate()[0]
        assert not expression.hasError(), expression.error().toString()
        assert "Existing goal" in details and "Existing notes" in details
        for name, value in (("launcherProjectNameField", "New study"),
                            ("launcherProjectSummaryField", "New goal"),
                            ("launcherProjectNotesField", "New notes"),
                            ("launcherProjectParentPathField", str(tmp_path))):
            field = window.findChild(QObject, name)
            assert field is not None
            assert field.setProperty("text", value)
        expression = QQmlExpression(engine.rootContext(), manager, "submitCreate()")
        assert expression.evaluate()[0] is True
        assert not expression.hasError(), expression.error().toString()
        assert adapter._pending is not None
        adapter._pending.result(timeout=2)
        adapter._complete_pending()
        assert calls == [("New study", tmp_path / "New study", {"summary": "New goal", "notes": "New notes"})]
        assert not (tmp_path / "New study").exists()
        assert ProjectStore.open(store.project_file).manifest["notes"] == "Existing notes"
    finally:
        adapter.stop()
        if window is not None:
            window.deleteLater()
        engine.deleteLater()
        app.processEvents()


def test_global_bridge_failure_is_distinct_from_empty_state(app, global_root, tmp_path):
    bridge = GlobalResourcesBridge()
    try:
        app.processEvents()
        wait_bridge(app, bridge)
        assert bridge.property("transferCatalog")["rows"] == [] and not bridge.libraryFailed
        assert bridge.browseSource(str(tmp_path / "missing.cproject"))
        wait_bridge(app, bridge)
        assert bridge.libraryFailed and "未完成" in bridge.property("transferStatus")
        assert bridge.property("transferCatalog") == {}
        assert bridge.browseGlobal()
        wait_bridge(app, bridge)
        assert not bridge.libraryFailed and bridge.property("transferCatalog")["rows"] == []
    finally:
        bridge.dispose()


def test_global_window_preview_and_apply_preserve_publication_until_finished(app, global_root, tmp_path):
    from threading import Event
    from PySide6.QtCore import QObject
    seed_source(global_root)
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    bridge = GlobalResourcesBridge()
    engine.rootContext().setContextProperty("bridge", bridge)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path(__file__).parents[1] / "coat_sim/ui/qml/GlobalResourcesWindow.qml")))
    window = component.create()
    assert isinstance(window, QQuickWindow)
    window.show()
    app.processEvents()
    entered, release = Event(), Event()
    original_apply = bridge.service.apply_resource_transfer
    def delayed_apply(*args):
        entered.set()
        assert release.wait(5)
        return original_apply(*args)
    bridge.service.apply_resource_transfer = delayed_apply
    try:
        app.processEvents()
        wait_bridge(app, bridge)
        destination = tmp_path / "export.cproject"
        identity = bridge.property("transferCatalog")["rows"][0]["id"]
        assert bridge.previewGlobalTransfer([identity], str(destination))
        wait_bridge(app, bridge)
        dialog = window.findChild(QObject, "globalResourcesPreviewDialog")
        assert dialog is not None and dialog.property("visible")
        assert bridge.applyTransfer({})
        assert entered.wait(2)
        assert not bridge.transferCancellable
        assert not window.close()
        assert not bridge._disposed
        release.set()
        wait_bridge(app, bridge)
        assert destination.is_file()
        assert not dialog.property("visible")
        assert window.close()
        assert bridge._disposed
    finally:
        release.set()
        wait_bridge(app, bridge)
        bridge.dispose()
        window.deleteLater()
        engine.deleteLater()
        app.processEvents()


def test_failed_runtime_startup_cleans_information_file(tmp_path, monkeypatch):
    from coat_sim.ui import project_runtime_client as client
    from coat_sim.ui.control import UiControlError
    seen = []
    def fail(arguments, *_args):
        seen.append(Path(arguments[arguments.index("--create-information") + 1]))
        assert seen[-1].is_file()
        assert "--consume-create-information" in arguments
        raise UiControlError("project_start_failed", "simulated startup failure")
    monkeypatch.setattr(client, "_start_runtime", fail)
    with pytest.raises(UiControlError, match="simulated"):
        client.create_project_runtime("Study", tmp_path / "study", summary="Goal", notes="Notes",
                                      client=SimpleNamespace(ensure_running=lambda: None))
    assert seen and not seen[0].exists()
    assert not (tmp_path / "study").exists()


def test_launcher_routes_global_manager_and_blocks_exit_during_publication(app, tmp_path):
    from PySide6.QtQml import QQmlApplicationEngine
    from coat_sim.ui import launcher as launcher_module
    from tests.launcher_test_isolation import LauncherTestIsolation
    isolation = LauncherTestIsolation(tmp_path / "isolated", launcher_module).apply()
    engine = QQmlApplicationEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    launcher = launcher_module.LauncherBridge(engine, with_launcher=False,
                                              window_settings=isolation.create_settings())
    engine.rootContext().setContextProperty("launcher", launcher)
    errors: list[str] = []
    launcher.launchError.connect(errors.append)
    try:
        identity = launcher.open_window("global_resources")
        assert identity
        deadline = time.monotonic() + 5
        while identity not in launcher._windows and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert identity in launcher._windows, errors
        handle = launcher._windows[identity]
        assert isinstance(handle.bridge, GlobalResourcesBridge)
        wait_bridge(app, handle.bridge)
        assert handle.window.isVisible()
        assert handle.bridge.property("transferCatalog")["source_type"] == "global"
        handle.bridge._transfer_applying = True
        launcher.shutdown()
        assert not launcher._closing
        assert handle.window.isVisible()
        assert errors and "全局资源正在提交" in errors[-1]
        handle.bridge._transfer_applying = False
        assert launcher.closeWindow(identity)
        assert handle.bridge._disposed
    finally:
        for handle in list(launcher._windows.values()):
            if isinstance(handle.bridge, GlobalResourcesBridge):
                handle.bridge._transfer_applying = False
            handle.window.close()
        launcher.shutdown()
        launcher.deleteLater()
        engine.deleteLater()
        app.processEvents()
        isolation.stop()


@pytest.mark.parametrize("font_scale", [8 / 3, 1.25])
def test_global_minimum_window_keeps_large_font_resources_reachable(app, global_root, font_scale):
    from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
    from PySide6.QtTest import QTest
    source = seed_source(global_root)
    data = json.loads(source.read_text(encoding="utf-8"))
    data.update(name="N" * 100, description="Long summary " * 1000, notes="Long notes " * 1000)
    source.write_bytes(json_bytes(data))
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("dark", font_scale=font_scale),
        0, "dark", "dark", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    bridge = GlobalResourcesBridge()
    engine.rootContext().setContextProperty("bridge", bridge)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path(__file__).parents[1] / "coat_sim/ui/qml/GlobalResourcesWindow.qml")))
    window = component.create()
    assert isinstance(window, QQuickWindow)
    def item(name):
        pending = [window.contentItem()]
        while pending:
            candidate = pending.pop()
            if candidate.objectName() == name:
                return candidate
            pending.extend(candidate.childItems())
        pytest.fail("missing visual item: " + name)
    try:
        window.resize(680, 500)
        window.show()
        app.processEvents()
        wait_bridge(app, bridge)
        QTest.mouseMove(window, QPoint(650, 470))
        QTest.qWait(40)
        scroll = item("globalResourcesPageScroll")
        flick = scroll.property("contentItem")
        content_height = float(flick.property("contentHeight"))
        # Narrow asset libraries stack browser and details; both remain
        # reachable by the shared page scroll at either accessibility scale.
        assert content_height > flick.height()
        QTest.qWait(40)
        resource_list = item("globalResourcesList")
        information = item("globalResourceInformation")
        assert resource_list.height() >= 100
        assert information.height() >= 100
        select = item("globalResourceSelect_0")
        action = item("globalResourceInformation_0")
        viewport = scroll.mapRectToScene(QRectF(0, 0, scroll.width(), scroll.height()))
        initial = select.mapRectToScene(QRectF(0, 0, select.width(), select.height()))
        flick.setProperty("contentY", max(0, initial.center().y() - viewport.center().y()))
        QTest.qWait(40)
        for control in (select, action):
            bounds = control.mapRectToScene(QRectF(0, 0, control.width(), control.height()))
            assert viewport.contains(bounds), (bounds, viewport)
        assert action.property("font").pointSizeF() == pytest.approx(12 * font_scale)
        center = select.mapToScene(QPointF(select.width() / 2, select.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(round(center.x()), round(center.y())))
        selected = window.property("selectedIds")
        if hasattr(selected, "toVariant"):
            selected = selected.toVariant()
        assert bridge.property("transferCatalog")["rows"][0]["id"] in selected
        # The information pane has its own bounded text scroll and remains
        # reachable independently rather than forcing both panes on screen.
        bounds = information.mapRectToScene(QRectF(0, 0, information.width(), information.height()))
        flick.setProperty("contentY", min(content_height - flick.height(),
                          float(flick.property("contentY")) + bounds.y() - viewport.y()))
        QTest.qWait(40)
        visible = information.mapRectToScene(QRectF(0, 0, information.width(), information.height()))
        assert viewport.intersects(visible)
        assert window.width() == 680 and window.height() == 500
    finally:
        bridge.dispose()
        window.close()
        window.deleteLater()
        engine.deleteLater()
        app.processEvents()
