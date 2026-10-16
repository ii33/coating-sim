"""Registered non-film windows expose actual drafts without creating a film editor."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal

from coat_sim.control_errors import UiControlError
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.mcp import server
from coat_sim.project_ui import PROJECT_EDITOR_DETAILS, project_window_arguments
from coat_sim.ui.project_application_client import ProjectApplicationClient
from coat_sim.ui.project_editor_snapshot import MAX_RESPONSE_BYTES
from tests.test_project_film_desktop import desktop as desktop
from tests.test_project_film_desktop import events

pytestmark = [pytest.mark.contract, pytest.mark.qt]

REMAINING = ["source", "substrate", "machine", "mask", "process", "materials",
             "main", "ray_trace", "film_appearance", "compute_queue"]


class Bridge(QObject):
    launchError = Signal(str)
    project_runtime: Any
    project_lifecycle = None


def _wait_material_editor_ready(window, material_bridge, *, timeout_s=5.0):
    """Wait for the existing asynchronous primary editor, never bypass a fence.

    Opening the host only starts QML incubation. Component.Ready and the Popup's
    onOpened/prepareOpen must finish before tests inspect or edit its draft.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        editor = window.property("editorInstance")
        state = {key: window.property(key) for key in (
            "editorLoading", "editorOpenRequested", "editorLoadFailed")}
        state.update(editor_present=editor is not None,
                     editor_opened=editor.property("opened") if editor is not None else False,
                     bridge_loading=material_bridge.loading)
        if state["editorLoadFailed"] is True:
            pytest.fail(f"Material editor failed to load: {state}")
        if window.property("projectScoped") is True:
            # The logical materials route now presents its retained controller
            # inside the sole project asset window, without a second host.
            assets = window._project_film_controller
            host_ready = not assets.transferBusy and not assets.assetApplying
        else:
            host_ready = state["editorLoading"] is False and state["editorOpenRequested"] is False
        if (state["editor_present"] and state["editor_opened"] is True
                and host_ready and state["bridge_loading"] is False):
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"Material editor did not become ready within {timeout_s}s: {state}")
        events()


def client_for(desktop, module):
    presentation, stores = desktop
    root = next(iter(stores))
    session = presentation.host.open_project(root)
    workspace = ProjectFilmWorkspace.for_project(presentation.host, session.binding)
    presentation.open_project_module(workspace, module)
    events()
    controller = presentation.controller_for(workspace, module)
    window = (controller.window if module == "materials" else
              next(view[0] for route, view in presentation._views.items() if route.module_key == module))
    if module == "materials":
        _wait_material_editor_ready(window, controller.bridge)
    bridge = Bridge()
    bridge.project_runtime = SimpleNamespace(host=presentation.host, desktop=presentation)
    client = ProjectApplicationClient(bridge, SimpleNamespace(dispatch=lambda *_args: pytest.fail("legacy fallback")))
    args = {"project_id": session.binding.project_id, "session_generation": session.binding.generation,
            "client_generation": client.generation, "project_root": str(root),
            "project_file": str(stores[root].project_file), "module_key": module}
    return SimpleNamespace(client=client, bridge=bridge, args=args, workspace=workspace,
                           session=session, controller=controller, window=window, stores=stores)


def read(live, **arguments):
    return live.client.dispatch("project_editor_snapshot", live.args | arguments)


@pytest.mark.parametrize("module", REMAINING)
def test_real_registered_window_reads_without_film_editor_or_global_fallback(desktop, module, monkeypatch):
    from coat_sim.ui.compute_queue import ComputeQueueView
    monkeypatch.setattr(ComputeQueueView, "_fetch_page", lambda *_args, **_kw: None)
    live = client_for(desktop, module)
    try:
        if module == "compute_queue":
            live.controller._pages_loaded["recent"] = 1
        assert live.workspace._editor is None
        before = deepcopy(next(iter(live.stores.values())).manifest)
        monkeypatch.setattr(ProjectFilmWorkspace, "editor", property(lambda _self: pytest.fail("lazy editor")))
        monkeypatch.setattr(ProjectFilmWorkspace, "for_project", lambda *_args: pytest.fail("lazy workspace"))
        result = read(live)
        if module == "film_appearance":
            assert not result["available"] and "appearance" in result["reason"]
            assert live.controller._appearance is None
        else:
            assert result["available"], result
            assert result["instance_id"]
            assert result["available_details"] == sorted(PROJECT_EDITOR_DETAILS[module])
            assert result["snapshot_id"] and not result["saved_fallback"]
            assert result["data_trust"] == "user_document_data"
            assert len(json.dumps(result, ensure_ascii=False).encode()) < MAX_RESPONSE_BYTES
        assert live.workspace._editor is None
        assert next(iter(live.stores.values())).manifest == before
    finally:
        live.client.stop()


def test_real_unsaved_resource_fields_page_hash_and_native_fences(desktop, monkeypatch):
    live = client_for(desktop, "source")
    try:
        bridge = live.controller.bridge
        bridge.setField("name", "Unsaved source")
        bridge.setEmissionField("kind", "uniform")
        bridge._apertures = [{"kind": "rectangle", "length_mm": str(index)} for index in range(150)]
        monkeypatch.setattr(bridge, "documentData", lambda: pytest.fail("validate/export"))
        monkeypatch.setattr(live.controller, "snapshot", lambda: pytest.fail("recovery snapshot"))
        monkeypatch.setattr(live.controller, "document_data", lambda: pytest.fail("document export"))
        monkeypatch.setattr(live.controller, "save_current", lambda: pytest.fail("save"))
        monkeypatch.setattr(live.controller.service, "load_document", lambda *_args: pytest.fail("saved fallback"))
        first = read(live, limit=2)
        assert first["summary"]["fields"]["name"] == "Unsaved source"
        assert first["summary"]["dirty"]
        assert first["collections"]["apertures"]["total"] == 150
        page = read(live, detail="apertures", offset=140, limit=2, expected_snapshot_id=first["snapshot_id"])
        assert page["page"]["items"][0]["length_mm"] == "140"
        bridge._apertures[149]["length_mm"] = "changed after current page"
        with pytest.raises(UiControlError) as error:
            read(live, detail="apertures", expected_snapshot_id=first["snapshot_id"])
        assert error.value.code == "stale_snapshot"
        live.controller._pending_information_edit = {"name": "Not confirmed"}
        assert read(live)["reason"] == "pending_information_edit"
        live.controller._pending_information_edit = None
        live.controller._pending_ui = {"object_name": "uncommitted", "text": "12e"}
        assert not read(live)["available"]
        assert live.controller._pending_ui["text"] == "12e"
    finally:
        live.client.stop()


def test_owner_generation_instance_and_cross_project_fences(desktop):
    live = client_for(desktop, "machine")
    try:
        first = read(live)
        assert first["available"]
        for changed, code in (({"client_generation": "stale"}, "stale_client"),
                              ({"session_generation": "stale"}, "stale_project"),
                              ({"project_root": "/wrong"}, "project_identity_mismatch"),
                              ({"instance_id": "stale"}, "stale_window")):
            with pytest.raises(UiControlError) as error:
                read(live, **changed)
            assert error.value.code == code
        presentation = desktop[0]
        route = next(route for route in presentation.host.routes(live.session.binding.project_id)
                     if route.module_key == "machine")
        view = presentation._views.pop(route)
        presentation.host.release_window(route)
        replacement = presentation.host.reserve_module(live.session.binding.project_id, "machine").route
        presentation.host.attach_window(replacement, live.window)
        presentation._views[replacement] = view
        with pytest.raises(UiControlError) as error:
            read(live, expected_snapshot_id=first["snapshot_id"])
        assert error.value.code == "stale_snapshot"
        live.controller.owner_binding = SimpleNamespace(project_id="another")
        assert read(live)["reason"] == "editor_owner_mismatch"
    finally:
        live.client.stop()


@pytest.mark.parametrize("module", REMAINING)
def test_module_details_are_strict_and_reading_does_not_expand_mutation_rights(module):
    from coat_sim.ui.project_editor_operations import project_editor_capabilities
    for detail in PROJECT_EDITOR_DETAILS[module]:
        assert project_window_arguments("project_editor_snapshot", {"window_type": module, "detail": detail})
    invalid: list[dict[str, Any]] = [{"detail": "full"}, {"detail": []}, {"detail": True},
                                     {"fields": {"name": "write"}}, {"limit": True}, {"offset": -1}]
    for arguments in invalid:
        with pytest.raises(UiControlError):
            project_window_arguments("project_editor_snapshot", {"window_type": module, **arguments})
    capability = project_editor_capabilities(module)
    if module == "compute_queue":
        assert capability["editor_operations"] == []
    else:
        assert capability["scope"] == "existing_current_editor"
        assert capability["requires"] == ["exact_project_target", "instance_id", "expected_snapshot_id"]
        assert "save" not in capability["actions"]


def test_snapshot_mcp_schema_names_all_modules_and_bounded_details():
    registered = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    tool = registered["project_editor_snapshot"]
    schema = getattr(tool, "input_schema", None) or tool.inputSchema
    assert set(REMAINING) <= set(schema["properties"]["window_type"]["enum"])
    assert schema["properties"]["limit"]["maximum"] == 100
    assert schema["properties"]["offset"]["minimum"] == 0
    assert set(schema["required"]) == {"project_target", "window_type"}


def test_real_appearance_existing_frozen_context_reads_without_reinitialization(desktop, tmp_path, monkeypatch):
    from coat_sim.ui.appearance import AppearanceWindowController
    from coat_sim.ui.raytrace import _layer_asset_summary
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset
    live = client_for(desktop, "film_appearance")
    external = tmp_path / "synthetic-input"
    external.mkdir()
    context = _layer_asset_summary(_write_synthetic_v7_asset(external), external)["appearance_context"]
    appearance = AppearanceWindowController(context, root=live.workspace.service.root,
                                             parent=live.controller, project_context=live.workspace)
    live.controller._appearance = appearance
    try:
        appearance.setField("spp", "17")
        appearance._draft["environment_spectrum"] = {
            "path": "captured.csv", "sha256": "a" * 64,
            "wavelength_nm": [400.0, 500.0, 600.0], "value": [1.0, 2.0, 1.0]}
        monkeypatch.setattr(appearance, "projectInputConfiguration", lambda: pytest.fail("full export"))
        monkeypatch.setattr(appearance, "_validate_draft", lambda: pytest.fail("numerical validation"))
        monkeypatch.setattr(live.controller, "refreshProjectAppearanceContext", lambda: pytest.fail("context refresh"))
        monkeypatch.setattr(live.controller, "_sync_appearance_context", lambda: pytest.fail("lazy controller"))
        first = read(live)
        assert first["available"], first
        assert first["summary"]["draft"]["spp"] == "17"
        assert "wavelength_nm" not in first["summary"]["spectra"]["environment_spectrum"]
        appearance._draft["environment_spectrum"]["value"][1] = 5.0
        with pytest.raises(UiControlError) as error:
            read(live, expected_snapshot_id=first["snapshot_id"])
        assert error.value.code == "stale_snapshot"
        appearance._camera_gesture_active = True
        assert read(live)["reason"] == "native_geometry_draft"
        assert appearance._camera_gesture_active
    finally:
        live.client.stop()
        appearance._camera_gesture_active = False


def test_real_mask_qml_array_draft_is_withheld_without_committing_geometry(desktop):
    live = client_for(desktop, "mask")
    engine = next(view[2] for route, view in desktop[0]._views.items() if route.module_key == "mask")
    points = engine.newArray(1)
    live.window.setProperty("draftPoints", points)
    live.window.setProperty("draftPointer", "")
    try:
        before = deepcopy(live.controller.bridge._data)
        result = read(live)
        assert not result["available"] and result["reason"] == "native_geometry_draft"
        assert live.controller.bridge._data == before
        retained = live.window.property("draftPoints")
        assert retained.isArray() and retained.property("length").toInt() == 1
    finally:
        live.window.setProperty("draftPoints", engine.newArray(0))
        live.client.stop()


def test_real_ray_close_confirmation_is_unavailable_until_user_returns(desktop):
    from PySide6.QtCore import QMetaObject, Qt
    live = client_for(desktop, "ray_trace")
    try:
        live.controller.newRequest()
        assert read(live)["available"]
        live.controller.requestClose()
        events()
        dialog = live.window.findChild(QObject, "rayTraceCloseDialog")
        assert dialog is not None and dialog.property("visible")
        result = read(live)
        assert not result["available"] and result["reason"] == "pending_confirmation"
        assert dialog.property("visible")
        assert QMetaObject.invokeMethod(dialog, "close", Qt.ConnectionType.DirectConnection)
        live.controller.resolveClose(False)
        assert read(live)["available"]
    finally:
        live.client.stop()


def test_owned_modal_inspection_allows_read_only_views_and_excludes_primary_editor():
    from coat_sim.ui.project_editor_snapshot import _context_modal_pending
    window = QObject()
    other_window = QObject()
    foreign = QObject(other_window)
    foreign.setProperty("modal", True)
    foreign.setProperty("visible", True)
    assert not _context_modal_pending(window, "source")
    dialog = QObject(window)
    dialog.setProperty("modal", True)
    dialog.setProperty("visible", True)
    assert _context_modal_pending(window, "source")
    for name in ("materialReferenceDialog", "substrateMaterialReferenceDialog",
                 "materialReferenceDetailsDialog", "historyDialog", "maskCadReportDialog"):
        dialog.setObjectName(name)
        assert not _context_modal_pending(window, "source")
    dialog.setObjectName("projectResourceInformationDialog")
    dialog.setProperty("editing", False)
    assert not _context_modal_pending(window, "source")
    dialog.setProperty("editing", True)
    assert _context_modal_pending(window, "source")
    window.setProperty("editorInstance", dialog)
    assert not _context_modal_pending(window, "materials")
    window.setProperty("editorLoading", True)
    assert _context_modal_pending(window, "materials")
    dialog.setProperty("visible", False)
    window.setProperty("sourceReferenceOpenRequested", True)
    assert _context_modal_pending(window, "source")
    assert foreign.property("visible") and dialog.property("visible") is False


@pytest.mark.parametrize("module, object_name", [
    ("process", "saveBeforeSendDialog"), ("mask", "maskImportOptionsDialog"),
    ("materials", "materialLibraryDeleteDialog"), ("source", "projectResourceCloseDialog"),
])
def test_real_local_pending_modals_withhold_draft_without_focus_or_commit(desktop, module, object_name):
    from PySide6.QtCore import QMetaObject, Qt
    live = client_for(desktop, module)
    try:
        assert read(live)["available"]
        dialog = live.window.findChild(QObject, object_name)
        assert dialog is not None
        assert QMetaObject.invokeMethod(dialog, "open", Qt.ConnectionType.DirectConnection)
        events()
        assert dialog.property("modal") is True and dialog.property("visible")
        result = read(live)
        assert not result["available"] and result["reason"] == "pending_confirmation"
        assert dialog.property("visible")
        assert QMetaObject.invokeMethod(dialog, "close", Qt.ConnectionType.DirectConnection)
        events()
        assert read(live)["available"]
    finally:
        live.client.stop()


def test_real_information_view_is_readable_but_unsubmitted_edit_mode_is_not(desktop):
    from PySide6.QtCore import QMetaObject, Qt
    live = client_for(desktop, "source")
    try:
        before = deepcopy(live.controller.bridge._fields)
        dialog = live.window.findChild(QObject, "projectResourceInformationDialog")
        assert dialog is not None
        assert QMetaObject.invokeMethod(dialog, "open", Qt.ConnectionType.DirectConnection)
        events()
        assert dialog.property("editing") is False
        assert read(live)["available"]
        dialog.setProperty("editing", True)
        result = read(live)
        assert not result["available"] and result["reason"] == "pending_confirmation"
        assert live.controller._pending_information_edit is None
        assert live.controller.bridge._fields == before
        dialog.setProperty("editing", False)
        assert QMetaObject.invokeMethod(dialog, "close", Qt.ConnectionType.DirectConnection)
        events()
        assert read(live)["available"]
    finally:
        live.client.stop()



def test_legacy_material_host_waits_for_real_delayed_incubation_and_popup_open(desktop):
    from PySide6.QtCore import QTimer
    from PySide6.QtQml import QQmlIncubationController
    from coat_sim.ui import project_film_host
    from coat_sim.ui.project_editor_snapshot import _context_modal_pending
    from coat_sim.ui.project_resource_adapter import create_project_resource_view
    from coat_sim.ui.theme_service import ThemeService

    original_engine = project_film_host.QQmlApplicationEngine
    retained = []
    pending_observations = []
    legacy_windows: list[Any] = []

    def delayed_engine(*args, **kwargs):
        engine = original_engine(*args, **kwargs)
        incubation = QQmlIncubationController()
        engine.setIncubationController(incubation)
        timer = QTimer(engine)
        timer.setInterval(10)
        first_pending_at: float | None = None

        def advance():
            nonlocal first_pending_at
            window = legacy_windows[0] if legacy_windows else None
            if window is not None and window.property("editorLoading"):
                pending_observations.append(_context_modal_pending(window, "materials"))
                if first_pending_at is None:
                    first_pending_at = time.monotonic()
            # Start the hold when the registered window actually exists, so
            # slow QML compilation cannot consume this deliberate delay.
            if first_pending_at is not None and time.monotonic() - first_pending_at >= 0.12:
                incubation.incubateFor(10)
                if incubation.incubatingObjectCount() == 0:
                    timer.stop()

        timer.timeout.connect(advance)
        timer.start()
        retained.append((engine, incubation, timer))
        return engine

    # The compatibility factory still owns existing live legacy hosts. New
    # logical materials routes use the asset window and are covered separately.
    presentation, stores = desktop
    session = presentation.host.open_project(next(iter(stores)))
    workspace = ProjectFilmWorkspace.for_project(presentation.host, session.binding)
    engine = delayed_engine(presentation)
    ThemeService.instance().attach_engine(engine, "Theme")
    window, controller, _context, _component = create_project_resource_view(
        workspace, "materials", engine, presentation)
    legacy_windows.append(window)
    window.show()
    try:
        _wait_material_editor_ready(window, controller.bridge)
        assert pending_observations and all(pending_observations)
        assert window.property("editorLoading") is False
        assert window.property("editorOpenRequested") is False
        assert window.property("editorLoadFailed") is False
        assert window.property("editorInstance").property("opened") is True
        assert controller.bridge._initial_default is False  # Original onOpened ran.
        assert not _context_modal_pending(window, "materials")
    finally:
        window.setProperty("projectCloseAuthorized", True)
        window.close()
        controller.detach_view()
        window.deleteLater()
        engine.deleteLater()
        events()


@pytest.mark.parametrize("blocked", ["editorLoading", "editorOpenRequested", "missing_editor",
                                     "closed_editor", "bridge_loading", "editorLoadFailed"])
def test_material_readiness_failure_is_bounded_diagnostic_and_does_not_change_state(blocked):
    window, editor = QObject(), QObject()
    editor.setProperty("opened", blocked != "closed_editor")
    window.setProperty("editorInstance", None if blocked == "missing_editor" else editor)
    for key in ("editorLoading", "editorOpenRequested", "editorLoadFailed"):
        window.setProperty(key, key == blocked)
    bridge = SimpleNamespace(loading=blocked == "bridge_loading")
    before = {key: window.property(key) for key in (
        "editorInstance", "editorLoading", "editorOpenRequested", "editorLoadFailed")}
    message = "failed to load" if blocked == "editorLoadFailed" else "did not become ready"
    with pytest.raises(pytest.fail.Exception, match=message) as error:
        _wait_material_editor_ready(window, bridge, timeout_s=0)
    assert "editorLoading" in str(error.value) and "bridge_loading" in str(error.value)
    assert before == {key: window.property(key) for key in before}
    assert editor.property("opened") == (blocked != "closed_editor")
