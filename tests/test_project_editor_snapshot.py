"""Live film context never creates editors, submits fields or uses saved inputs."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from threading import Thread
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication, QWindow
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.assistant_process import AssistantProcessClient, LIFECYCLE_OPERATIONS
from coat_sim.core.project_film_session import ProjectFilmWorkspace
from coat_sim.core.project_store import ProjectStore
from coat_sim.mcp import server
from coat_sim.project_session import ProjectSessionHost
from coat_sim.project_ui import project_window_arguments
from coat_sim.ui.control import UiControlError
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.project_application_client import ProjectApplicationClient
from coat_sim.ui.project_editor_snapshot import MAX_RESPONSE_BYTES, project_editor_snapshot

pytestmark = [pytest.mark.contract, pytest.mark.qt]


class Bridge(QObject):
    launchError = Signal(str)
    project_launcher: SimpleNamespace
    project_lifecycle: object | None


class ProjectWindow(QWindow):
    _project_film_controller: QObject


@pytest.fixture
def live(tmp_path):
    app = QGuiApplication.instance() or QGuiApplication([])
    store = ProjectStore.create(tmp_path / "study", "Study")
    host = ProjectSessionHost(lambda _root: ProjectStore.open(store.project_file))
    session = host.open_project(store.root)
    workspace = ProjectFilmWorkspace.for_project(host, session.binding)
    editor = workspace.editor
    source = FilmDesignQuickController(service=workspace.service, edit_session=editor,
                                      project_context=workspace)
    windows, controllers = {}, {"film_design": source}
    def mount(module, controller):
        route = host.reserve_module(session.binding.project_id, module).route
        window = ProjectWindow()
        window._project_film_controller = controller
        host.attach_window(route, window)
        windows[module] = window
        controllers[module] = controller
        return window
    mount("film_design", source)
    desktop = SimpleNamespace(controller_for=lambda owner, module: controllers.get(module))
    owner = SimpleNamespace(host=host, desktop=desktop)
    bridge = Bridge()
    bridge.project_launcher, bridge.project_lifecycle = owner, None
    value = ProjectApplicationClient(bridge, SimpleNamespace(dispatch=lambda *_args: pytest.fail("global fallback")))
    args = {"project_id": session.binding.project_id, "session_generation": session.binding.generation,
            "client_generation": value.generation, "project_root": str(store.root),
            "project_file": str(store.project_file), "module_key": "film_design"}
    yield SimpleNamespace(app=app, store=store, host=host, session=session, workspace=workspace,
                          editor=editor, source=source, windows=windows, controllers=controllers,
                          owner=owner, bridge=bridge, client=value, args=args, mount=mount)
    value.stop()
    for controller in controllers.values():
        detach = getattr(controller, "detach_observers", None)
        if callable(detach):
            detach()
    for window in windows.values():
        window.close()


def read(live, **arguments):
    return live.client.dispatch("project_editor_snapshot", live.args | arguments)


def test_reads_unsaved_current_draft_without_publishing_or_side_effect(live, monkeypatch):
    assert live.editor.set_design_value("name", "Unsaved current study")
    before = deepcopy(live.store.manifest)
    monkeypatch.setattr(ProjectFilmWorkspace, "for_project", lambda *_args: pytest.fail("lazy workspace"))
    monkeypatch.setattr(live.workspace.service, "load_document", lambda *_args: pytest.fail("saved fallback"))
    monkeypatch.setattr(live.editor, "save_current", lambda *_args: pytest.fail("save"))
    result = read(live)
    assert result["available"] and result["dirty"]
    assert result["design"]["name"] == "Unsaved current study"
    assert result["revision"] == live.editor.snapshot().revision
    assert result["optimization"] == {"available": False, "source": None, "reason": "window_closed"}
    assert not result["saved_fallback"]
    assert live.store.manifest == before
    assert set(live.windows) == {"film_design"}
    assert live.workspace._editor is live.editor


def test_no_workspace_editor_or_window_never_lazily_constructed(live, monkeypatch):
    editor = live.workspace._editor
    live.workspace._editor = None
    monkeypatch.setattr(ProjectFilmWorkspace, "editor", property(lambda _self: pytest.fail("lazy editor")))
    assert read(live)["reason"] == "editor_unavailable"
    live.workspace._editor = editor
    live.windows["film_design"]._project_film_controller = None
    live.controllers.clear()
    assert read(live)["reason"] == "controller_unavailable"


def test_optimization_reads_actual_controller_changes_and_invalidates_pages(live):
    optimization = FilmOptimizationQuickController(live.source, project_context=live.workspace)
    live.mount("film_optimization", optimization)
    first = read(live, limit=1)
    assert first["optimization"]["source"] == "current_controller"
    assert first["collections"]["objectives"]["total"] == 1
    assert first["optimization"]["settings"]["solver"]["method"] == optimization._solver_id
    revision = live.editor.snapshot().design_revision
    configuration = optimization.exportOptimizationConfiguration()
    configuration["solver"]["max_iterations"] += 1
    assert optimization.loadOptimizationConfiguration(configuration)
    assert live.editor.snapshot().design_revision == revision
    with pytest.raises(UiControlError) as error:
        read(live, detail="objectives", expected_snapshot_id=first["snapshot_id"])
    assert error.value.code == "stale_snapshot"
    current = read(live, detail="objectives")
    assert current["snapshot_id"] != first["snapshot_id"]
    assert current["page"]["items"] == optimization.exportOptimizationConfiguration()["objectives"]
    assert read(live, module_key="film_optimization")["available"]


def test_layer_material_projection_uses_captured_not_new_picker_and_bounds_all_pages(live):
    with live.editor._lock:
        live.editor._design["front_layers"] = [
            {"id": f"layer-{index}", "material_id": "pinned", "optical_set_id": "nk",
             "thickness": {"value": index + 1.0, "unit": "nm"}, "note": "secret note" * 10000}
            for index in range(7)]
        live.editor._retained_material_snapshots = {"pinned": {
            "name": "Pinned value", "optical_sets": [{"id": "nk", "kind": "tabulated",
            "wavelength_min": 400.0, "wavelength_max": 700.0, "wavelength_unit": "nm",
            "n": [1.4] * 10000, "k": None, "wavelengths": list(range(10000))}]}}
        live.editor._material_options = {"film": [{"id": "pinned", "name": "Mutable NEW catalog"}]}
        live.editor._revision += 1
    result = read(live, limit=2)
    assert result["collections"]["layers"]["total"] == 7
    assert len(result["collections"]["layers"]["items"]) == 2
    assert result["collections"]["layers"]["truncated"] and result["truncated"]
    material = next(row for row in result["collections"]["materials"]["items"]
                    if row["material_id"] == "pinned" and row["optical_set_id"] == "nk")
    assert material["name"] == "Pinned value"
    assert material["optical_data"]["n_sample_count"] == 10000
    assert material["optical_data"]["k_data_present"] is None
    assert material["optical_data"]["lossless"] == "unknown"
    encoded = json.dumps(result)
    assert "secret note" not in encoded and "Mutable NEW" not in encoded
    assert "wavelengths" not in encoded and len(encoded.encode()) < MAX_RESPONSE_BYTES
    page = read(live, detail="layers", offset=2, limit=2,
                expected_snapshot_id=result["snapshot_id"])
    assert page["snapshot_id"] == result["snapshot_id"]
    assert [row["id"] for row in page["page"]["items"]] == ["layer-2", "layer-3"]
    assert page["page"]["next_offset"] == 4
    live.editor.set_design_value("name", "Changed")
    with pytest.raises(UiControlError, match="改变"):
        read(live, detail="layers", offset=4, expected_snapshot_id=result["snapshot_id"])


@pytest.mark.parametrize("change,reason", [
    (lambda live: setattr(live.bridge, "_project_starting", True), "initializing"),
    (lambda live: setattr(live.bridge, "project_lifecycle", SimpleNamespace(active=True)), "project_frozen"),
    (lambda live: setattr(live.workspace, "_resource_replacement_in_progress", True), "ownership_transfer"),
    (lambda live: setattr(live.editor, "_expression_busy", True), "expression_busy"),
    (lambda live: setattr(live.editor, "_pending_confirmation", {"kind": "overwrite"}), "pending_confirmation"),
    (lambda live: setattr(live.editor, "_pending_information_edit", {"name": "Typed"}), "pending_information_edit"),
])
def test_unavailable_fences_do_not_return_old_document(live, change, reason):
    change(live)
    result = read(live)
    assert not result["available"] and result["reason"] == reason
    assert "design" not in result and "snapshot_id" not in result


def test_compute_freeze_and_gui_thread_requirement(live):
    live.host.claim_resource(live.session.binding, "compute_workspace", SimpleNamespace(editing_frozen=True))
    assert read(live)["reason"] == "project_frozen"
    errors = []
    def attempt():
        try:
            project_editor_snapshot(live.owner, live.session, "film_design", {},
                                    client_generation=live.client.generation, bridge=live.bridge)
        except UiControlError as exc:
            errors.append(exc.code)
    worker = Thread(target=attempt)
    worker.start()
    worker.join()
    assert errors == ["gui_thread_required"]


def test_native_field_draft_is_read_only_and_unavailable(live):
    panel = QObject(live.windows["film_design"])
    panel.setObjectName("filmDesignExpressionPanel")
    panel.setProperty("expressionDraftPending", True)
    result = read(live)
    assert result["reason"] == "native_field_editing"
    assert panel.property("expressionDraftPending") is True


def test_live_client_rejects_generation_root_mismatch_and_missing_identity(live):
    for changes, code in (({"session_generation": "old"}, "stale_project"),
                          ({"client_generation": "old"}, "stale_client"),
                          ({"project_root": "/wrong"}, "project_identity_mismatch")):
        with pytest.raises(UiControlError) as error:
            read(live, **changes)
        assert error.value.code == code
    with pytest.raises(UiControlError) as error:
        live.client.dispatch("project_editor_snapshot", {"window_type": "film_design"})
    assert error.value.code == "invalid_arguments"


@pytest.mark.parametrize("arguments", [
    {"limit": 0}, {"limit": 101}, {"limit": True}, {"offset": -1}, {"offset": 1.0},
    {"detail": "all"}, {"expected_snapshot_id": "old"}, {"data": {}},
    {"window_type": "settings"},
])
def test_strict_bounded_snapshot_arguments(arguments):
    with pytest.raises(UiControlError):
        project_window_arguments("project_editor_snapshot", {"window_type": "film_design", **arguments})


def test_mcp_registration_requires_target_and_routes_without_global_fallback(monkeypatch):
    registered = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    tool = registered["project_editor_snapshot"]
    schema = getattr(tool, "input_schema", None) or tool.inputSchema
    assert set(schema["required"]) == {"project_target", "window_type"}
    assert "expected_snapshot_id" in schema["properties"]
    assert schema["properties"]["offset"]["minimum"] == 0
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == 100
    assert "project_editor_snapshot" in LIFECYCLE_OPERATIONS
    calls = []
    def record_request(_self, operation, **args):
        calls.append((operation, args))
        return {}
    monkeypatch.setattr(AssistantProcessClient, "request", record_request)
    monkeypatch.setattr(server, "_ui_call", lambda *_args, **_kwargs: pytest.fail("global fallback"))
    target = server.ProjectTarget(client_id="owner", client_generation="client", project_id="project",
        session_generation="session", root="/tmp/study", project_file="/tmp/study/study.coatproj")
    server.project_editor_snapshot(target, "film_design", detail="layers", offset=2, limit=3,
                                   expected_snapshot_id="a" * 64)
    assert calls[0][0] == "project_window"
    assert calls[0][1]["window_operation"] == "project_editor_snapshot"
    assert calls[0][1]["arguments"]["expected_snapshot_id"] == "a" * 64
    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool("project_editor_snapshot", {"window_type": "film_design"}))


@pytest.mark.parametrize("field", ["client_generation", "project_root", "project_file"])
def test_owner_snapshot_requires_complete_forwarded_identity(live, field):
    args = {key: value for key, value in live.args.items() if key != field}
    with pytest.raises(UiControlError) as error:
        live.client.dispatch("project_editor_snapshot", args)
    assert error.value.code == "invalid_arguments"


def test_owner_snapshot_rejects_unknown_arguments(live):
    with pytest.raises(UiControlError) as error:
        read(live, accidental_endpoint="http://unapproved")
    assert error.value.code == "invalid_arguments"


def test_snapshot_changes_when_window_instance_changes(live):
    before = read(live)
    assert before["instance_id"]
    live.host.release_window(next(route for route in live.host.routes(live.session.binding.project_id)
                                  if route.module_key == "film_design"))
    live.mount("film_design", live.source)
    after = read(live)
    assert after["instance_id"] != before["instance_id"]
    assert after["snapshot_id"] != before["snapshot_id"]


def test_native_project_close_and_exit_freeze_are_unavailable(live):
    observed = []
    def prepare(_binding, _routes):
        observed.append(read(live))
        return False
    assert not live.host.close_project(live.session.binding, prepare_close=prepare)
    assert observed[0]["reason"] == "project_frozen"
    assert "design" not in observed[0]
    with live.host.exit_admission_freeze():
        assert read(live)["reason"] == "project_frozen"
    assert read(live)["available"]


def test_optimizer_busy_and_stale_source_cannot_masquerade_as_current(live):
    optimization = FilmOptimizationQuickController(live.source, project_context=live.workspace)
    live.mount("film_optimization", optimization)
    optimization._busy = True
    assert read(live, module_key="film_optimization")["reason"] == "busy"
    design = read(live)
    assert design["available"] and design["optimization"]["available"] is False
    optimization._busy = False
    optimization._source_stale = True
    assert read(live, module_key="film_optimization")["reason"] == "stale_source"


def simulation(live):
    from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
    controller = FilmSimulationQuickController(service=live.workspace.service,
        source_controller=live.source, project_context=live.workspace)
    live.mount("film_simulation", controller)
    return controller


def test_simulation_current_parameters_are_real_and_changes_invalidate_snapshot(live, monkeypatch):
    controller = simulation(live)
    monkeypatch.setattr(controller, "requestCalculation", lambda: pytest.fail("calculate"))
    monkeypatch.setattr(controller, "refreshFromSource", lambda: pytest.fail("implicit refresh"))
    monkeypatch.setattr(controller.service, "simulate", lambda *_args, **_kw: pytest.fail("compute"))
    first = read(live, module_key="film_simulation")
    assert first["available"] and first["simulation"]["source"] == "current_controller"
    config = first["simulation"]["configuration"]
    exported = controller.exportSimulationConfiguration()
    assert config["parameters"] == exported["parameters"]
    assert config["sampling"]["authoritative"] == "exact_sample_count"
    assert config["sampling"]["samples"] == controller._params["samples"]
    physical_revision = live.editor.snapshot().design_revision
    controller.setParameter("angle_deg", 17.0)
    assert live.editor.snapshot().design_revision == physical_revision
    with pytest.raises(UiControlError) as error:
        read(live, module_key="film_simulation", expected_snapshot_id=first["snapshot_id"])
    assert error.value.code == "stale_snapshot"
    current = read(live, module_key="film_simulation")
    assert current["simulation"]["configuration"]["parameters"]["angle_deg"] == 17.0
    assert current["snapshot_id"] != first["snapshot_id"]
    assert controller._result is None and not controller.busy


def test_simulation_tabulated_light_source_is_compact_but_full_values_bind_hash(live):
    controller = simulation(live)
    intensities = [1.0] * 1000
    source = {"kind": "tabulated", "wavelength_nm": [400.0 + index for index in range(1000)],
              "intensity": intensities, "source": "Captured document source"}
    controller._source_payloads["document:tabulated"] = source
    controller._params["light_source_id"] = "document:tabulated"
    first = read(live, module_key="film_simulation")
    light = first["simulation"]["configuration"]["parameters"]["light_source"]
    assert light["sample_count"] == 1000 and light["samples_omitted"]
    assert light["wavelength_min_nm"] == 400.0 and light["wavelength_max_nm"] == 1399.0
    assert "wavelength_nm" not in light and "intensity" not in light
    assert first["content_truncated"] and first["truncated"]
    intensities[500] = 2.0
    with pytest.raises(UiControlError) as error:
        read(live, module_key="film_simulation", expected_snapshot_id=first["snapshot_id"])
    assert error.value.code == "stale_snapshot"


@pytest.mark.parametrize("change,reason", [
    (lambda controller: setattr(controller, "_source_session", None), "controller_unavailable"),
    (lambda controller: setattr(controller, "_source_design_revision", -1), "stale_source"),
    (lambda controller: setattr(controller, "_source_sync_error", "old source"), "stale_source"),
    (lambda controller: setattr(controller, "_busy", True), "busy"),
    (lambda controller: setattr(controller, "_pending_save_path", "/pending"), "pending_confirmation"),
])
def test_simulation_missing_stale_or_busy_source_is_unavailable(live, change, reason):
    controller = simulation(live)
    change(controller)
    result = read(live, module_key="film_simulation")
    assert not result["available"] and result["reason"] == reason
    assert "design" not in result and "simulation" not in result


def test_closed_simulation_never_creates_a_window_or_uses_persisted_settings(live, monkeypatch):
    live.editor.set_workflow_configuration("simulation", {"parameters": {"angle_deg": 33.0}})
    monkeypatch.setattr(ProjectFilmWorkspace, "for_project", lambda *_args: pytest.fail("lazy create"))
    result = read(live, module_key="film_simulation")
    assert not result["available"] and result["reason"] == "window_closed"
    summary = read(live)
    assert summary["simulation"] == {"available": False, "source": None, "reason": "window_closed"}
    assert set(live.windows) == {"film_design"}


def test_simulation_physical_mismatch_rejects_but_other_page_workflow_does_not(live):
    controller = simulation(live)
    live.editor.set_workflow_configuration("optimization", {"version": 1, "objectives": []})
    assert read(live, module_key="film_simulation")["available"]
    controller._design["reference_wavelength_nm"] += 10.0
    assert read(live, module_key="film_simulation")["reason"] == "stale_source"
