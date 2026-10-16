"""Real resource QML and project-owned end-to-end authoring/close contracts."""
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from PySide6.QtCore import QObject, QUrl, QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QAccessible, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.project_paths import project_default_directory
from coat_sim.core.project_default_loading import load_project_default
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_resource_service import ProjectResourceService
from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
from coat_sim.project_session import ProjectSessionHost
from coat_sim.ui.project_resource_adapter import create_project_resource_view, dispose_project_resource_controller
from coat_sim.ui.theme_service import ThemeService
from tests.test_project_module_selector import Navigation
from tests.test_project_film_controller_adapter import MemorySettings
from tests.test_input_snapshot import fixture
from tests.qt_test_guard import qml_item_window

pytestmark = [pytest.mark.contract, pytest.mark.qt]


@pytest.fixture
def project(tmp_path):
    app = QGuiApplication.instance() or QGuiApplication([])
    stores = {name: ProjectStore.create(tmp_path / name, name) for name in ("First", "Second")}
    host = ProjectSessionHost(lambda root: next(store for store in stores.values() if store.root == root))
    first = host.open_project(stores["First"].root)
    film = ProjectFilmWorkspace.for_project(host, first.binding)
    resource = ProjectResourceWorkspace.for_project(host, first.binding)
    resource.initialize_default(load_project_default(resource.store))
    views = []
    def view(key, owner=film):
        engine = QQmlApplicationEngine()
        ThemeService.instance().attach_engine(engine)
        navigation = Navigation(owner.name)
        window, controller, context, component = create_project_resource_view(
            owner, key, engine, navigation=navigation, geometry_settings=MemorySettings())
        views.append((window, controller, engine, context, component, navigation))
        window.show()
        app.processEvents()
        # The responsive toolbar coalesces font/visibility/width changes with
        # its owned zero-delay timer before coordinates become mouse targets.
        QTest.qWait(35)
        app.processEvents()
        if key == "materials":
            # The material dialog is incubated asynchronously. Wait for its
            # real editor before inspecting controls or changing its draft;
            # onOpened also selects the initial global material.
            for _ in range(200):
                if window.property("editorInstance") is not None:
                    break
                QTest.qWait(10)
            assert window.property("editorInstance") is not None
            assert not window.property("editorLoadFailed")
            app.processEvents()
        return window, controller
    yield app, stores, host, film, resource, view
    for window, controller, engine, *_ in views:
        assert window is not None
        if controller.window is window:
            assert window is not None
            window.setProperty("projectCloseAuthorized", True)
            window.close()
        dispose_project_resource_controller(controller, discard=False)
        window.deleteLater()
        engine.deleteLater()
    for session in host.sessions():
        owner = host.resource_for(session.binding, "resource_workspace")
        if owner:
            owner.dispose()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize("key", ["source", "substrate", "machine", "mask", "process", "materials"])
def test_real_qml_resource_window_toolbar_and_explicit_project_directory(project, key):
    _app, stores, _host, _film, _resource, view = project
    window, controller = view(key)
    assert window.property("projectResources") is controller
    assert window.findChild(QQuickItem, "projectResourceControls") is not None
    assert window.findChild(QQuickItem, "projectModuleSelector") is not None
    assert Path(controller.defaultDirectory.toLocalFile()).resolve() == project_default_directory(
        stores["First"].root, "material_profile" if key == "materials" else key).resolve()
    assert controller.owner_binding.root == stores["First"].root


def test_source_project_save_button_saves_reopens_and_stays_visible_at_minimum_size(project):
    app, stores, _host, _film, resources, view = project
    window, source = view("source")
    window.resize(640, 540)
    app.processEvents()

    save = window.findChild(QQuickItem, "sourceSaveButton")
    assert save is not None and save.isVisible() and save.isEnabled()
    surface = qml_item_window(save)
    assert surface is window
    top_left = save.mapToItem(surface.contentItem(), QPointF(0, 0))
    assert top_left.x() >= 0 and top_left.y() >= 0
    assert top_left.x() + save.width() <= window.width() + 1
    assert top_left.y() + save.height() <= window.height() + 1

    def click(item):
        target_window = qml_item_window(item)
        assert target_window is not None
        point = item.mapToItem(target_window.contentItem(), QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(target_window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, point)
        app.processEvents()

    # Metadata now belongs to the shared Information editor. Use actual mouse
    # and keyboard input, confirm its local buffer, then use ordinary Save.
    info_button = window.findChild(QQuickItem, "projectResourceInformationButton")
    assert info_button is not None and info_button.isVisible()
    click(info_button)
    edit = window.findChild(QQuickItem, "projectResourceInformationEditButton")
    assert edit is not None and edit.isVisible()
    click(edit)

    def enter_text(item, text):
        assert item is not None and item.isVisible() and item.isEnabled()
        target_window = qml_item_window(item)
        assert target_window is not None
        click(item)
        item.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
        QTest.qWait(20)
        assert item.hasActiveFocus()
        QTest.keyClick(target_window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(target_window, Qt.Key.Key_Backspace)
        for character in text:
            key = Qt.Key.Key_Space if character == " " else getattr(Qt.Key, "Key_" + character.upper())
            QTest.keyClick(target_window, key)
        app.processEvents()
        assert item.property("text") == text

    enter_text(window.findChild(QQuickItem, "projectResourceInformationName"), "saved source")
    description = window.findChild(QQuickItem, "projectResourceInformationSummary")
    enter_text(description, "saved through focused field")
    assert source.bridge.description == ""  # Unconfirmed UI text stays local.
    assert source.pendingInformationEdit["summary"] == "saved through focused field"
    assert not source.saveCurrent()
    confirm_information = window.findChild(QQuickItem, "projectResourceInformationSaveButton")
    click(confirm_information)
    assert source.pendingInformationEdit == {}
    assert source.bridge.description == "saved through focused field"
    assert source.resourceInformation["name"] == "saved source"
    assert source.dirty

    # Keep a visual artifact at the declared minimum logical geometry.
    evidence = Path("artifacts/source-save-review")
    evidence.mkdir(parents=True, exist_ok=True)
    image = window.grabWindow()
    assert not image.isNull()
    screenshot = evidence / "source-project-save-640x540.png"
    assert image.save(str(screenshot))
    assert screenshot.stat().st_size > 0

    click(save)
    identity = source.currentDocumentId
    assert identity
    assert not source.dirty
    reopened_service = ProjectResourceService(ProjectStore.open(stores["First"].project_file))
    saved = reopened_service.load_document(identity)
    assert saved.data["name"] == "saved source"
    assert saved.data["description"] == "saved through focused field"
    assert "saved source" in str(window.property("caption"))
    assert window.findChild(QQuickItem, "sourceResourceDisplayName").property("text") == "saved source"
    picker = window.findChild(QQuickItem, "projectManagedDocumentPicker")
    assert picker.property("currentText") == "saved source"
    assert "saved source" in str(picker.property("displayText"))
    named_image = window.grabWindow()
    assert not named_image.isNull()
    assert named_image.save(str(evidence / "source-project-saved-name-640x540.png"))
    assert source.openDocument(identity)
    assert source.bridge.description == "saved through focused field"
    assert not source.dirty

    # Updating an existing managed source must keep its identity and count.
    source.bridge.setField("source_exponent", "2.5")
    assert source.dirty
    click(save)
    assert source.currentDocumentId == identity
    assert len(resources.service.list_documents("source")) == 1
    assert resources.service.load_document(identity).data["source_exponent"] == 2.5
    assert not source.dirty

    # A write failure preserves the current dirty draft and the previous
    # managed file contents.
    source.bridge.setMetadata("description", "unsaved after write failure")
    assert source.dirty
    with patch.object(source.service, "save", side_effect=OSError("disk full")):
        click(save)
    assert source.dirty
    assert "保存失败" in source.status
    unchanged = ProjectResourceService(ProjectStore.open(stores["First"].project_file)).load_document(identity)
    assert unchanged.data["description"] == "saved through focused field"
    assert unchanged.data["source_exponent"] == 2.5

    # Invalid source input remains visible and cannot mutate the saved resource.
    source.bridge.setMetadata("description", "saved through focused field")
    source.bridge.setField("source_exponent", "-1")
    assert not source.bridge.valid
    assert not save.isEnabled()
    assert window.property("statusSeverity") == "error"
    assert window.property("statusText") == source.bridge.error
    assert resources.service.load_document(identity).data["source_exponent"] == 2.5
    assert source.dirty

    # The optional material-library action appears for a non-uniform source;
    # ensure it and the save entry still fit the operation bar at 640 px.
    source.bridge.setField("source_exponent", "2.5")
    source.bridge.setEmissionField("kind", "resistance_boat")
    app.processEvents()
    material_button = window.findChild(QQuickItem, "materialLibraryButton")
    assert material_button is not None and material_button.isVisible()
    for action in (save, material_button):
        point = action.mapToItem(surface.contentItem(), QPointF(0, 0))
        assert point.x() >= 0 and point.y() >= 0
        assert point.x() + action.width() <= window.width() + 1
        assert point.y() + action.height() <= window.height() + 1


def test_empty_project_to_complete_default_graph_then_reopen(project):
    app, stores, host, film, resources, view = project
    _source_window, source = view("source")
    assert source.saveCurrent(), source.status
    _substrate_window, substrate = view("substrate")
    assert substrate.saveCurrent(), substrate.status
    _mask_window, mask = view("mask")
    mask.bridge.newDocument(json.dumps({"z_mm": 50., "outer": [[-10., -10.], [10., -10.], [10., 10.], [-10., 10.]], "holes": []}))
    assert mask.saveCurrent(), mask.status
    _machine_window, machine = view("machine")
    data = {"schema_version": 5, "carrier_x_mm": 0., "carrier_y_mm": 0., "planet_offset_mm": 312.,
            "planet_tilt_deg": 0., "substrate_z_mm": 580., "sun_teeth": 120, "planet_teeth": 36,
            "gear_module_mm": 4., "sources": [{"file": resources.service.document_handle(source.currentDocumentId).name,
                "x_mm": 0., "y_mm": 0., "z_mm": 0.}],
            "masks": [{"file": resources.service.document_handle(mask.currentDocumentId).name}], "monitors": []}
    machine.bridge.applyDocument(data)
    assert machine.saveCurrent(), machine.status
    _process_window, process = view("process")
    process.bridge.chooseDependency("machine_file", QUrl.fromLocalFile(str(resources.service.document_handle(machine.currentDocumentId))))
    process.bridge.chooseDependency("substrate_file", QUrl.fromLocalFile(str(resources.service.document_handle(substrate.currentDocumentId))))
    process.bridge.setField("name", "Complete GUI graph")
    revision = stores["First"].manifest["revision"]
    assert process.saveDefaultConfiguration("GUI default", '{"backend":"CPU","settings":{"grid_size":21,"time_steps":2}}'), process.status
    assert stores["First"].manifest["revision"] == revision + 1
    default = resources.service.resolve_default()
    assert {row["kind"] for row in default["documents"]} == {"process", "machine", "source", "mask", "substrate", "deposition_configuration"}
    assert default["runtime"]["profile"]["name"] == "Complete GUI graph"
    root = stores["First"].root
    reopened_store = ProjectStore.open(stores["First"].project_file)
    reopened_host = ProjectSessionHost(lambda _root: reopened_store)
    session = reopened_host.open_project(root)
    reopened = ProjectResourceWorkspace.for_project(reopened_host, session.binding)
    assert reopened.initialize_default(load_project_default(reopened_store))
    assert reopened.opening_graph is not None
    assert reopened.opening_graph["runtime"]["profile"]["name"] == "Complete GUI graph"
    for kind in ("process", "machine", "source", "mask", "substrate"):
        assert reopened.initial_document(kind)
    app.processEvents()


def test_dirty_close_cancel_keep_and_reopen_retains_same_editor(project):
    app, _stores, _host, _film, resources, view = project
    window, controller = view("source")
    controller.bridge.setField("source_exponent", "3")
    assert controller.dirty
    assert not window.close()
    app.processEvents()
    close_dialog = window.findChild(QObject, "projectResourceCloseDialog")
    assert close_dialog.property("visible")
    assert not controller.confirmClose("cancel")
    assert controller.bridge._fields["source_exponent"] == "3"
    assert controller.confirmClose("keep")
    dispose_project_resource_controller(controller, discard=False)
    second_window, second = view("source")
    assert second is controller
    assert second.bridge._fields["source_exponent"] == "3" and second.dirty
    assert resources.dirty_summary()[0]["module_key"] == "source"
    second_window.setProperty("projectCloseAuthorized", True)


def test_two_projects_material_libraries_and_resource_drafts_are_isolated(project, tmp_path, monkeypatch):
    from coat_sim.core.material_business import ApplicationMaterialCatalog, MaterialLibrary
    from coat_sim.core.materials import default_material_profile

    _app, stores, host, _film, resources, view = project
    global_dir = tmp_path / "global-materials"
    global_profile = default_material_profile("Shared global material", id="shared-global-material")
    MaterialLibrary(directory=global_dir).write(global_profile)
    global_lock = global_dir / ".material-library.lock"
    global_lock.unlink(missing_ok=True)
    global_library = ApplicationMaterialCatalog(global_dir)
    monkeypatch.setattr("coat_sim.core.project_material_library.application_material_library",
                        lambda: global_library)

    def global_files():
        return {path.relative_to(global_dir).as_posix():
                (path.read_bytes(), path.stat().st_mtime_ns)
                for path in global_dir.rglob("*") if path.is_file()}

    global_before = global_files()
    assert global_before
    _first_window, one = view("source")
    one.bridge.setField("source_exponent", "4")
    second_session = host.open_project(stores["Second"].root)
    second_film = ProjectFilmWorkspace.for_project(host, second_session.binding)
    _second_window, two = view("source", second_film)
    assert two.bridge._fields["source_exponent"] == "1"
    first_builtins = one.bridge._material_library._builtins
    second_builtins = two.bridge._material_library._builtins
    assert first_builtins and [row["id"] for row in first_builtins] == [
        row["id"] for row in second_builtins]
    assert all(first is not second for first, second in zip(
        first_builtins, second_builtins, strict=True))
    other = ProjectResourceWorkspace.for_project(host, second_session.binding)
    second_before = other.service.material_library().catalog_records()
    _material_window, material = view("materials")
    identity = global_profile["id"]
    assert material.bridge.selectMaterial(material.bridge._global_material_keys[identity])
    assert material.bridge.currentId == identity and material.bridge.globalSourceSelected
    assert resources.service.material_library().read(identity)["readonly"]
    assert stores["First"].manifest["documents"] == []
    assert stores["Second"].manifest["documents"] == []
    assert global_files() == global_before
    information = material.resourceInformation
    assert set(information["editable_fields"]) == {"name", "summary", "notes"}
    assert material.updateResourceInformation("Only first project", information["summary"],
                                              information["notes"], information["token"])
    assert material.dirty
    # Editing remains an in-memory draft until the explicit project save.
    assert stores["First"].manifest["documents"] == []
    assert global_files() == global_before
    assert material.saveCurrent(), material.status
    assert material.bridge.currentId == identity and not material.bridge.globalSourceSelected
    first_catalog = resources.service.material_library().catalog_records()
    first_owned = [row for row in first_catalog if row["origin"] == "project"]
    assert len(first_owned) == 1
    assert first_owned[0]["record"]["id"] == identity
    assert first_owned[0]["record"]["name"] == "Only first project"
    assert not first_owned[0]["readonly"]
    assert {row["kind"] for row in stores["First"].manifest["documents"]} == {
        "material_profile", "optical_material"}
    owned_documents = {row["kind"]: row["id"]
                       for row in stores["First"].manifest["documents"]}
    material.bridge.setField("name", "Only first project revised")
    assert material.saveCurrent(), material.status
    assert material.bridge.currentId == identity
    assert resources.service.material_library().read(identity)["record"]["name"] == (
        "Only first project revised")
    assert {row["kind"]: row["id"] for row in stores["First"].manifest["documents"]} == (
        owned_documents)
    second_catalog = other.service.material_library().catalog_records()
    assert second_catalog == second_before
    assert second_catalog and all(row["origin"] == "global" and row["readonly"]
                                  for row in second_catalog)
    second_same_id = next(row for row in second_catalog
                          if row["record"]["id"] == identity)
    assert second_same_id["record"] == global_profile
    assert global_library.read(identity)["record"] == global_profile
    assert global_files() == global_before
    assert not global_lock.exists()
    assert stores["Second"].manifest["documents"] == []
    assert one.bridge._fields["source_exponent"] == "4"
    assert two.bridge._fields["source_exponent"] == "1"


def test_frozen_retained_drafts_save_as_one_transaction_and_keep_prior_disabled_state(project):
    _app, stores, _host, _film, resources, view = project
    window, source = view("source")
    _other_window, substrate = view("substrate")
    source.bridge.setField("source_exponent", "3")
    substrate.bridge.setField("name", "Dirty substrate")
    before = stores["First"].manifest["revision"]
    window.setProperty("enabled", False)
    assert len(resources.close_preflight()) == 2
    resources.freeze_editing(True)
    source.bridge.setField("source_exponent", "5")
    assert source.bridge._fields["source_exponent"] == "3"
    assert resources.save_all()
    assert stores["First"].manifest["revision"] == before + 1
    assert not resources.dirty_summary()
    resources.freeze_editing(False)
    assert not window.property("enabled")


def test_recovery_stages_invalid_text_without_saving_and_rejects_stale_documents(project):
    _app, stores, host, film, resources, view = project
    _window, source = view("source")
    assert source.saveCurrent()
    source.bridge.setField("source_exponent", "invalid retained text")
    snapshot = resources.snapshot()
    assert snapshot["drafts"]["source"]["data"] is None
    before = stores["First"].manifest
    source.bridge._dirty = False
    source.bridge.setField("source_exponent", "1")
    source.bridge._dirty = False
    assert resources.restore_recovery(snapshot)
    assert source.bridge._fields["source_exponent"] == "invalid retained text"
    assert source.dirty
    assert stores["First"].manifest == before
    source.bridge._dirty = False
    identity = source.currentDocumentId
    saved = resources.service.load_document(identity)
    saved.data["source_exponent"] = 2
    resources.service.save_resource(identity, saved.data)
    with pytest.raises(ValueError, match="differs"):
        resources.restore_recovery(snapshot)


def test_invalid_default_parameters_do_not_save_or_clear_current_process_draft(project, tmp_path):
    _app, stores, _host, _film, resources, view = project
    external = tmp_path / "outside"
    external.mkdir()
    fixture(external)
    _window, process = view("process")
    process.importDocument(QUrl.fromLocalFile(str(external / "recipe.cproc")))
    process.bridge.setField("name", "Unsaved process rename")
    before = stores["First"].manifest
    assert not process.saveDefaultConfiguration("Bad", '{"unexpected":true}')
    assert stores["First"].manifest == before
    assert process.dirty


def test_recovery_validation_is_read_only_and_rejects_missing_editor_fields(project):
    from copy import deepcopy
    _app, stores, _host, _film, resources, view = project
    _window, source = view("source")
    assert source.saveCurrent()
    snapshot = resources.snapshot()
    before = resources.snapshot()
    resources.validate_recovery(snapshot)
    assert resources.snapshot() == before
    malformed = deepcopy(snapshot)
    del malformed["drafts"]["source"]["editor_state"]["_fields"]["kind"]
    with pytest.raises(ValueError, match="fields differ"):
        resources.validate_recovery(malformed)
    assert resources.snapshot() == before


def test_managed_dependency_picker_rejects_wrong_kind_and_does_not_mutate_draft(project):
    _app, _stores, _host, _film, resources, view = project
    _source_window, source = view("source")
    assert source.saveCurrent()
    _process_window, process = view("process")
    before = process.bridge._data.copy()
    assert len(process.resourcesForKind("source")) == 1
    assert not process.chooseDependency("machine_file", source.currentDocumentId)
    assert process.bridge._data == before


def test_invalid_nested_material_draft_is_retained_in_recovery(project):
    _app, stores, _host, _film, resources, view = project
    _window, source = view("source")
    material = source.bridge._material_library
    material.setField("resistance_source_exponent", "invalid nested value")
    assert material.dirty and not material.valid
    snapshot = resources.snapshot()
    nested = snapshot["drafts"]["source"]["nested_material"]
    assert nested["data"] is None
    material._dirty = False
    before = stores["First"].manifest
    assert resources.restore_recovery(snapshot)
    assert material.dirty and material._draft["resistance_source_exponent"] == "invalid nested value"
    assert stores["First"].manifest == before


def test_unopened_recovered_drafts_stay_dirty_and_save_without_constructing_gui(project):
    _app, stores, _host, _film, resources, view = project
    _window, source = view("source")
    assert source.saveCurrent()
    source.bridge.setField("source_exponent", "6")
    snapshot = resources.snapshot()
    restored_store = ProjectStore.open(stores["First"].project_file)
    restored_host = ProjectSessionHost(lambda _root: restored_store)
    session = restored_host.open_project(restored_store.root)
    restored = ProjectResourceWorkspace.for_project(restored_host, session.binding)
    restored.initialize_default(load_project_default(restored_store))
    assert restored.restore_recovery(snapshot)
    assert restored.controllers == {}
    assert restored.dirty_summary()[0]["module_key"] == "source"
    assert restored.snapshot()["drafts"]["source"]["editor_state"]["_fields"]["source_exponent"] == "6"
    before = restored_store.manifest["revision"]
    assert restored.save_all()
    assert restored_store.manifest["revision"] == before + 1
    assert not restored.dirty_summary()
    identity = snapshot["drafts"]["source"]["document_id"]
    assert restored.service.load_document(identity).data["source_exponent"] == 6


def test_material_host_toolbar_remains_above_editor_and_export_is_global_importable(project, tmp_path):
    from coat_sim.core.material_business import MaterialLibrary
    _app, _stores, _host, _film, resources, view = project
    window, material = view("materials")
    editor = window.property("editorInstance")
    assert editor is not None
    controls = window.findChild(QQuickItem, "projectResourceControls")
    toolbar = controls.parentItem()
    assert toolbar.objectName() == "materialLibraryActionToolbar"
    accessible = QAccessible.queryAccessibleInterface(toolbar)
    assert accessible is not None
    assert accessible.text(QAccessible.Text.Name) == "操作栏"
    assert window.findChild(QQuickItem, "projectModuleSelector").parentItem().parentItem() == toolbar
    for name in ("newMaterialButton", "saveMaterialButton", "importMaterialButton",
                 "exportLibraryButton", "exportSingleMaterialButton"):
        action = window.findChild(QQuickItem, name)
        assert action is not None and not action.property("visible")
        assert action.parentItem() == toolbar
    for name in ("projectManagedDocumentPicker", "projectResourceNewButton",
                 "projectResourceSaveButton", "projectResourceTransferMenuButton"):
        action = window.findChild(QQuickItem, name)
        assert action is not None and action.isVisible()
        assert action.parentItem() == controls
    category = window.findChild(QQuickItem, "materialRoleFilter")
    refresh = window.findChild(QQuickItem, "refreshMaterialLibraryButton")
    assert category is not None and refresh is not None
    assert refresh.property("visible") and refresh.parentItem() == category.parentItem()
    assert refresh.parentItem() != toolbar
    assert window.findChild(QQuickItem, "copyMaterialButton") is None
    assert window.findChild(QQuickItem, "materialLibraryCloseKeepDraftButton") is None
    assert toolbar.height() > 0
    material.bridge.setField("name", "Export full project material")
    assert material.saveCurrent()
    assert material.exportDocument(QUrl.fromLocalFile(str(tmp_path)))
    exported = list(tmp_path.glob("material-export-*.json"))
    assert len(exported) == 1
    package = MaterialLibrary.parse_package_file(exported[0])
    assert package["records"][0]["name"] == "Export full project material"


@pytest.mark.parametrize(('key', 'field_name', 'text'), [
    ('process', 'processNameField', '  unfinished expression + 1  '),
    ('machine', 'geometryField_carrier_x_mm', '2 * 1e2'),
])
def test_pending_text_checkpoint_preserves_geometry_focus_and_migrates_metadata(project, key, field_name, text):
    from PySide6.QtCore import QMetaObject, Qt
    from coat_sim.ui.project_resource_adapter import _find_text_item
    app, _stores, _host, _film, resources, view = project
    window, controller = view(key)
    if key == "process":
        # The old visible name field moved into Information. An approved older
        # recovery keeps its exact text as an unsaved name, without a duplicate
        # main-form input or forcing focus onto a removed control.
        snapshot = resources.snapshot_pending_edits()
        snapshot["drafts"][key].update(dirty=True, data=None, pending_ui={
            "object_name": field_name, "text": text, "numeric_value_edited": False})
        fresh_host = ProjectSessionHost(lambda _root: resources.store)
        binding = fresh_host.open_project(resources.store.root).binding
        recovered = ProjectResourceWorkspace.for_project(fresh_host, binding)
        assert recovered.restore_recovery(snapshot)
        _restored_window, restored = view(key, ProjectFilmWorkspace.for_project(fresh_host, binding))
        assert restored.bridge.document["name"] == text
        assert restored.resourceInformation["name"] == text
        assert restored.dirty and restored.snapshot_pending_edits() is None
        assert restored.snapshot()["editor_state"]["_data"]["name"] == text
        recovered.dispose()
        return
    field = _find_text_item(window, field_name)
    assert field is not None
    field.forceActiveFocus()
    app.processEvents()
    original = controller.document_data()
    field.setProperty('text', text)
    assert QMetaObject.invokeMethod(field, 'textEdited', Qt.ConnectionType.DirectConnection)
    before_focus = window.property('activeFocusItem')
    snapshot = resources.snapshot_pending_edits()
    assert window.property('activeFocusItem') is before_focus
    assert field.property('text') == text
    assert controller.document_data() == original
    assert snapshot['drafts'][key]['pending_ui']['text'] == text
    assert snapshot['drafts'][key]['data'] is None
    assert snapshot['dirty'][0]['module_key'] == key

    # A fresh generation restores explicitly approved raw text, never disk.
    fresh_host = ProjectSessionHost(lambda _root: resources.store)
    fresh_binding = fresh_host.open_project(resources.store.root).binding
    fresh_resources = ProjectResourceWorkspace.for_project(fresh_host, fresh_binding)
    assert fresh_resources.restore_recovery(snapshot)
    fresh_film = ProjectFilmWorkspace.for_project(fresh_host, fresh_binding)
    restored_window, restored = view(key, fresh_film)
    app.processEvents()
    restored_field = _find_text_item(restored_window, field_name)
    assert restored_field is not None
    assert restored_field.property('text') == text
    assert restored.snapshot_pending_edits()['text'] == text
    if key == 'machine':
        assert restored.commit_pending_edits() is False
        assert restored_field.property('text') == text
        assert restored_window.property('activeFocusItem') is restored_field
    else:
        assert restored.commit_pending_edits()
        assert restored.bridge.document['name'] == text
    fresh_resources.dispose()


def test_pending_recovery_field_identity_is_typed_and_frozen_restore_rejected(project):
    from copy import deepcopy
    from coat_sim.core.project_dependencies import ProjectError
    _app, _stores, _host, _film, resources, view = project
    _window, controller = view('process')
    snapshot = resources.snapshot()
    record = snapshot['drafts']['process']
    record.update(dirty=True, data=None, pending_ui={
        'object_name': 'not_a_document_field', 'text': 'raw', 'numeric_value_edited': False})
    with pytest.raises(ProjectError, match='Unknown pending'):
        resources.validate_recovery(snapshot)
    valid = deepcopy(snapshot)
    valid['drafts']['process']['pending_ui']['object_name'] = 'processNameField'
    resources.validate_recovery(valid)
    resources.freeze_editing(True)
    before = controller.snapshot()
    with pytest.raises(ProjectError, match='paused'):
        resources.restore_recovery(valid)
    assert controller.snapshot() == before
    resources.freeze_editing(False)


def test_pending_machine_source_height_text_is_recovered_into_exact_second_slot(project, tmp_path):
    from PySide6.QtCore import QMetaObject, Qt
    from coat_sim.ui.project_resource_adapter import _find_text_item
    app, _stores, _host, _film, resources, view = project
    external = tmp_path / 'external'
    external.mkdir()
    fixture(external)
    process = resources.service.load('process', external / 'recipe.cproc')
    machine_path = resources.service.resolve_reference(process.path, process.data['machine_file'])
    machine_id = resources.service._identity_for_path(machine_path)
    window, controller = view('machine')
    assert controller.openDocument(machine_id)
    assert controller.currentDocumentId == machine_id  # Untouched defaults do not ask to replace.
    app.processEvents()
    field = _find_text_item(window, 'sourceZField1')
    assert field is not None, (controller.bridge._data, controller.bridge.status, controller.currentDocumentId)
    field.forceActiveFocus()
    app.processEvents()
    text = '  47.125e  '
    field.setProperty('text', text)
    assert QMetaObject.invokeMethod(field, 'textEdited', Qt.ConnectionType.DirectConnection)
    snapshot = resources.snapshot_pending_edits()
    assert snapshot['drafts']['machine']['pending_ui']['object_name'] == 'sourceZField1'
    fresh_host = ProjectSessionHost(lambda _root: resources.store)
    binding = fresh_host.open_project(resources.store.root).binding
    recovered = ProjectResourceWorkspace.for_project(fresh_host, binding)
    assert recovered.restore_recovery(snapshot)
    recovered_window, restored = view('machine', ProjectFilmWorkspace.for_project(fresh_host, binding))
    app.processEvents()
    target = _find_text_item(recovered_window, 'sourceZField1')
    assert target is not None
    assert target.property('text') == text
    first = _find_text_item(recovered_window, 'sourceZField0')
    assert first is not None and first.property('text') != text
    assert restored.snapshot_pending_edits()['object_name'] == 'sourceZField1'
    recovered.dispose()


def _machine_input(window, app, text):
    from coat_sim.ui.project_resource_adapter import _find_text_item
    field = _find_text_item(window, "geometryField_carrier_x_mm")
    assert field is not None
    field.forceActiveFocus()
    QTest.qWait(25)
    surface = qml_item_window(field)
    QTest.keyClick(surface, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    for character in text:
        QTest.keyClick(surface, getattr(Qt.Key, "Key_" + character.upper()))
    app.processEvents()
    assert field.property("text") == text
    return field


def _close_action(window, app, name):
    item = window.findChild(QQuickItem, name)
    assert item is not None and item.isVisible() and item.isEnabled()
    surface = qml_item_window(item)
    point = item.mapToItem(surface.contentItem(), QPointF(item.width()/2, item.height()/2)).toPoint()
    QTest.mouseClick(surface, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    app.processEvents()


def test_machine_initial_defaults_and_noop_bindings_close_cleanly(project):
    app, _stores, _host, _film, resources, view = project
    window, machine = view("machine")
    assert machine.bridge._data["sources"] == []
    assert not machine.dirty and not resources.dirty_summary()
    machine.bridge.setValue("/carrier_x_mm", "0")
    assert not machine.dirty
    field = _machine_input(window, app, "0")
    from coat_sim.ui.project_resource_adapter import _find_text_item
    _find_text_item(window, "geometryField_carrier_y_mm").forceActiveFocus()
    app.processEvents()
    assert not field.hasActiveFocus() and not machine.dirty
    assert window.close()
    app.processEvents()
    assert not window.isVisible() and not resources.dirty_summary()


@pytest.mark.parametrize("decision", ["save", "discard", "cancel"])
def test_machine_real_close_actions_save_discard_cancel(project, decision):
    app, stores, _host, _film, resources, view = project
    _source_window, source = view("source")
    assert source.saveCurrent()
    window, machine = view("machine")
    machine.bridge.setValue("/sources", json.dumps([{"file": resources.service.document_handle(source.currentDocumentId).name,
                            "x_mm": 0., "y_mm": 0., "z_mm": 0.}]))
    assert machine.saveCurrent(), machine.status
    identity = machine.currentDocumentId
    before_revision = stores["First"].manifest["revision"]
    _machine_input(window, app, "23")
    assert not window.close()
    app.processEvents()
    assert window.findChild(QObject, "projectResourceCloseDialog").property("visible")
    _close_action(window, app, "projectResource" + decision.title() + "CloseButton")
    assert window.isVisible() == (decision == "cancel")
    assert resources.service.load_document(identity).data["carrier_x_mm"] == (23 if decision == "save" else 0)
    assert stores["First"].manifest["revision"] == before_revision + (decision == "save")
    assert machine.dirty == (decision == "cancel")
    if decision == "discard":
        dispose_project_resource_controller(machine, discard=False)
        reopened, same = view("machine")
        assert same is machine and same.bridge._data["carrier_x_mm"] == 0
        assert not same.dirty and reopened.isVisible()


def test_machine_invalid_native_text_cancel_then_discard_without_saving(project):
    app, stores, _host, _film, resources, view = project
    window, machine = view("machine")
    original = machine.bridge.documentJson
    before = stores["First"].project_file.read_bytes()
    field = _machine_input(window, app, "invalid")
    assert machine.dirty
    assert not window.close()
    app.processEvents()
    _close_action(window, app, "projectResourceCancelCloseButton")
    QTest.qWait(50)
    app.processEvents()
    assert window.isVisible() and field.property("text") == "invalid"
    assert machine.dirty
    assert not window.close()
    app.processEvents()
    _close_action(window, app, "projectResourceSaveCloseButton")
    assert window.isVisible() and machine.dirty
    _close_action(window, app, "projectResourceDiscardCloseButton")
    assert not window.isVisible() and not machine.dirty
    assert machine.bridge.documentJson == original
    assert stores["First"].project_file.read_bytes() == before


@pytest.mark.parametrize("save_kind", ["current", "copy", "all"])
def test_discard_material_edits_returns_to_last_successful_save(project, save_kind):
    app, _stores, _host, _film, resources, view = project
    window, material = view("materials")
    material.bridge.setField("notes", "saved-note")
    if save_kind == "current":
        assert material.saveCurrent()
    elif save_kind == "copy":
        assert material.saveCopyCurrent()
    else:
        assert resources.save_all()
    app.processEvents()
    identity = material.currentDocumentId
    assert identity and material.bridge._draft["notes"] == "saved-note"
    material.bridge.setField("notes", "later-unsaved-note")
    assert material.dirty and material.confirmClose("discard")
    assert not window.isVisible() and not material.dirty
    assert material.currentDocumentId == identity
    assert material.bridge._draft["notes"] == "saved-note"
    assert resources.service.load_document(identity).data["material"]["notes"] == "saved-note"


def test_rejected_native_discard_retains_content_and_does_not_leave_close_bypass(project):
    app, _stores, _host, _film, _resources, view = project
    window, machine = view("machine")
    machine.bridge.setValue("/carrier_x_mm", "12")
    before = machine.snapshot()

    class RejectClose(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Close:
                event.ignore()
                return True
            return False

    reject = RejectClose()
    window.installEventFilter(reject)
    assert machine.confirmClose("discard")
    assert window.isVisible() and machine.snapshot() == before and machine.dirty
    assert not window.property("projectCloseAuthorized")
    window.removeEventFilter(reject)
    assert not window.close()
    app.processEvents()
    assert window.findChild(QObject, "projectResourceCloseDialog").property("visible")


def test_parent_dirty_discard_preserves_independently_saved_nested_material(project):
    app, _stores, _host, _film, _resources, view = project
    window, source = view("source")
    source.bridge.setField("source_exponent", "3")
    nested = source.bridge._material_library
    nested.prepareOpen()
    app.processEvents()
    nested.setField("notes", "nested-saved-note")
    assert nested.save()
    identity = nested.currentId
    assert source.bridge.dirty and not nested.dirty
    nested.setField("notes", "nested-unsaved-note")
    assert source.confirmClose("discard")
    assert not window.isVisible() and not source.dirty and not nested.dirty
    assert nested.currentId == identity and nested._draft["notes"] == "nested-saved-note"


@pytest.mark.parametrize("already_open", [False, True])
def test_recovered_saved_resource_discard_uses_its_persisted_parent_and_nested_baselines(project, already_open):
    app, stores, _host, _film, resources, view = project
    _window, source = view("source")
    source.bridge.setField("source_exponent", "2")
    assert source.saveCurrent()
    source_id = source.currentDocumentId
    nested = source.bridge._material_library
    nested.prepareOpen()
    app.processEvents()
    nested.setField("notes", "persisted nested note")
    assert nested.save()
    material_id = nested.currentId
    source.bridge.setField("source_exponent", "5")
    nested.setField("notes", "recovered unsaved note")
    snapshot = resources.snapshot()

    fresh_store = ProjectStore.open(stores["First"].project_file)
    fresh_host = ProjectSessionHost(lambda _root: fresh_store)
    binding = fresh_host.open_project(fresh_store.root).binding
    recovered = ProjectResourceWorkspace.for_project(fresh_host, binding)
    if already_open:
        recovered_window, restored = view("source", ProjectFilmWorkspace.for_project(fresh_host, binding))
        previous, clean = restored.snapshot(), json.loads(json.dumps(restored._clean_snapshot))
        with patch.object(restored.bridge._material_library, "_schedule_load", return_value=False):
            with pytest.raises(ValueError, match="已保存内容"):
                recovered.restore_recovery(snapshot)
        assert restored.snapshot() == previous and restored._clean_snapshot == clean
        _sibling_window, sibling = view("substrate", ProjectFilmWorkspace.for_project(fresh_host, binding))
        snapshot["drafts"]["substrate"] = sibling.snapshot()
        previous_state = restored.capture_recovery_state()
        with patch.object(sibling, "restore_recovery_snapshot", side_effect=ValueError("later editor failure")):
            with pytest.raises(ValueError, match="later editor failure"):
                recovered.restore_recovery(snapshot)
        assert restored.capture_recovery_state() == previous_state
    assert recovered.restore_recovery(snapshot)
    if not already_open:
        recovered_window, restored = view("source", ProjectFilmWorkspace.for_project(fresh_host, binding))
    assert restored.currentDocumentId == source_id and restored.dirty
    assert restored.confirmClose("discard")
    assert not recovered_window.isVisible() and not restored.dirty
    assert restored.currentDocumentId == source_id and float(restored.bridge._fields["source_exponent"]) == 2
    restored_nested = restored.bridge._material_library
    assert restored_nested.currentId == material_id
    assert restored_nested._draft["notes"] == "persisted nested note" and not restored_nested.dirty
    recovered.dispose()
