"""Offline real-controller tests for snapshot-bound editor operations."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from coat_sim.ui.control import UiControlError
from coat_sim.ui.film_optimization_quick_controller import FilmOptimizationQuickController
from coat_sim.ui.project_editor_operations import project_editor_capabilities, project_editor_operation
from coat_sim.ui.project_editor_snapshot import project_editor_snapshot
from tests.test_project_editor_snapshot import live as live

pytestmark = [pytest.mark.contract, pytest.mark.qt]


def snapshot(live, module="film_design"):
    return project_editor_snapshot(live.owner, live.session, module, {},
        client_generation=live.client.generation, bridge=live.bridge)


def request(live, *, module="film_design", fields=None, action=None, arguments=None, **changes):
    current = snapshot(live, module)
    assert current["available"], current
    payload = live.args | {"module_key": module, "instance_id": current["instance_id"],
                          "expected_snapshot_id": current["snapshot_id"]}
    if fields is not None:
        operation = "update_window"
        payload["fields"] = fields
    else:
        operation = "window_action"
        payload.update(action=action, arguments=arguments or {})
    return operation, payload | changes


def run(live, **kwargs):
    operation, payload = request(live, **kwargs)
    return project_editor_operation(live.owner, live.session, payload["module_key"], operation,
        payload, client_generation=live.client.generation, bridge=live.bridge)


def optimizer(live):
    value = FilmOptimizationQuickController(live.source, project_context=live.workspace)
    live.mount("film_optimization", value)
    return value


def test_real_metadata_and_save_current_preserve_managed_owner(live, monkeypatch):
    monkeypatch.setattr(live.source, "requestNewDesign", lambda: pytest.fail("new"))
    monkeypatch.setattr(live.source, "requestOpenFile", lambda *_args: pytest.fail("replace"))
    before = deepcopy(live.store.manifest)
    result = run(live, fields={"name": "Unsaved edit from current context"})
    assert result["accepted"] and result["dirty"] and result["refresh_snapshot"]
    assert live.store.manifest == before
    assert live.editor.snapshot().design["name"] == "Unsaved edit from current context"
    saved = run(live, action="save")
    assert saved["accepted"] and not saved["dirty"]
    rows = [row for row in live.host.store_for(live.session.binding).manifest["documents"] if row["kind"] == "film"]
    assert len(rows) == 1
    identity = rows[0]["id"]
    assert live.workspace.service.load_document(identity).design["name"] == "Unsaved edit from current context"
    run(live, fields={"note": "Second change"})
    run(live, action="save")
    assert len([row for row in live.host.store_for(live.session.binding).manifest["documents"] if row["kind"] == "film"]) == 1
    assert live.workspace._editor is live.editor


@pytest.mark.parametrize("field,value", [
    ("summary", "Brief"), ("note", "Comment"), ("reference_wavelength_nm", 600.0),
    ("assume_lossless", True), ("use_k", False), ("first_surface", "back"),
])
def test_design_field_whitelist_uses_actual_setter(live, field, value):
    assert run(live, fields={field: value})["accepted"]
    assert live.editor.snapshot().design[field] == value


def test_layer_actions_bind_stable_ids_and_keep_normal_dirty_semantics(live):
    assert run(live, action="add_layer", arguments={"side": "front"})["accepted"]
    first = live.editor.snapshot().design["front_layers"][0]["id"]
    run(live, action="insert_layer_after", arguments={"side": "front", "layer_id": first})
    second = live.editor.snapshot().design["front_layers"][1]["id"]
    for field, value in (("thickness.value", 82.5), ("thickness.unit", "um"), ("note", "Layer note")):
        run(live, action="set_layer_value", arguments={"side": "front", "layer_id": first,
                                                       "field": field, "value": value})
    row = live.editor.snapshot().design["front_layers"][0]
    assert row["thickness"] == {"value": 82.5, "unit": "um"} and row["note"] == "Layer note"
    run(live, action="move_layer", arguments={"side": "front", "layer_id": second, "delta": -1})
    assert live.editor.snapshot().design["front_layers"][0]["id"] == second
    run(live, action="delete_layer", arguments={"side": "front", "layer_id": first})
    assert [row["id"] for row in live.editor.snapshot().design["front_layers"]] == [second]


@pytest.mark.parametrize("field,value", [
    ("name", ""), ("reference_wavelength_nm", 0), ("use_k", "false"),
    ("reference_wavelength_nm", float("nan")), ("first_surface", []), ("unknown", 4),
])
def test_invalid_design_input_is_atomic(live, field, value):
    before = live.editor.snapshot()
    with pytest.raises(UiControlError):
        run(live, fields={field: value})
    after = live.editor.snapshot()
    assert (after.design, after.revision, after.dirty) == (before.design, before.revision, before.dirty)


def test_missing_stale_tokens_and_replaced_instances_cannot_write(live):
    operation, payload = request(live, fields={"name": "Never"})
    for changes, code in (({"expected_snapshot_id": None}, "requires_snapshot"),
                          ({"instance_id": None}, "window_identity_required"),
                          ({"instance_id": "old"}, "stale_window")):
        with pytest.raises(UiControlError) as error:
            project_editor_operation(live.owner, live.session, "film_design", operation, payload | changes,
                client_generation=live.client.generation, bridge=live.bridge)
        assert error.value.code == code
    live.editor.set_design_value("note", "Changed after reading")
    with pytest.raises(UiControlError) as error:
        project_editor_operation(live.owner, live.session, "film_design", operation, payload,
            client_generation=live.client.generation, bridge=live.bridge)
    assert error.value.code == "stale_snapshot"
    assert live.editor.snapshot().design["name"] != "Never"


@pytest.mark.parametrize("change", [
    {"force": True}, {"data": {}}, {"design_path": "/tmp/foreign.cfilm"},
    {"pointers": {}}, {"intended_path": "/tmp/new.cfilm"}, {"unknown": "value"},
    {"fields": {"name": "First", "note": "Second"}},
])
def test_no_force_replacement_or_partial_multifield_update(live, change):
    before = live.editor.snapshot()
    with pytest.raises(UiControlError):
        run(live, fields={"name": "Never"}, **change) if "fields" not in change else run(live, **change)
    assert live.editor.snapshot().design == before.design


@pytest.mark.parametrize("change,code", [
    ({"client_generation": "old"}, "stale_client"),
    ({"project_id": "other"}, "stale_project"),
    ({"session_generation": "old"}, "stale_project"),
    ({"project_root": "/wrong"}, "project_identity_mismatch"),
    ({"project_file": "/wrong.coatproj"}, "project_identity_mismatch"),
])
def test_owner_identity_fences(live, change, code):
    with pytest.raises(UiControlError) as error:
        run(live, fields={"name": "Never"}, **change)
    assert error.value.code == code


@pytest.mark.parametrize("change", [
    lambda live: setattr(live.bridge, "project_lifecycle", SimpleNamespace(active=True)),
    lambda live: setattr(live.workspace, "_resource_replacement_in_progress", True),
    lambda live: setattr(live.editor, "_expression_busy", True),
    lambda live: setattr(live.editor, "_pending_confirmation", {"kind": "overwrite"}),
    lambda live: setattr(live.editor, "_pending_information_edit", {"name": "Typing"}),
    lambda live: live.host.claim_resource(live.session.binding, "compute_workspace", SimpleNamespace(editing_frozen=True)),
    lambda live: setattr(live.owner.desktop, "project_closer", SimpleNamespace(is_pending=lambda _binding: True)),
])
def test_writes_recheck_fences_after_snapshot(live, change):
    operation, payload = request(live, fields={"name": "Never"})
    before = live.editor.snapshot()
    change(live)
    with pytest.raises(UiControlError):
        project_editor_operation(live.owner, live.session, "film_design", operation, payload,
            client_generation=live.client.generation, bridge=live.bridge)
    assert live.editor.snapshot().design == before.design


def test_optimization_fields_options_material_and_save_use_real_controller(live, monkeypatch):
    value = optimizer(live)
    monkeypatch.setattr(value, "startOptimization", lambda: pytest.fail("compute start"))
    for field, new in (("search_mode", "structure"), ("structure_mode", "growth_only"),
                       ("solver", "baseline"), ("acceptable_error_percent", 2.5)):
        assert run(live, module="film_optimization", fields={field: new})["accepted"]
    for option, new in (("max_iterations", 17), ("max_evaluations", 31), ("seed", 8),
                        ("validation_samples_multiplier", 2), ("multistart_count", 3), ("population_size", 7)):
        run(live, module="film_optimization", action="set_solver_option", arguments={"name": option, "value": new})
        assert value.exportOptimizationConfiguration()["solver"][option] == new
    for option, structure_value in (("candidate_budget", 12), ("growth_steps_nm", [2.0, 5.0]),
                        ("growth_phase_fractions", [0.25, 0.5]), ("max_total_layers", 20),
                        ("max_thickness_nm", 900.0)):
        run(live, module="film_optimization", action="set_structure_option", arguments={"name": option, "value": structure_value})
        assert value.exportOptimizationConfiguration()["structure_options"][option] == structure_value
    assert live.editor.snapshot().design["workflow_settings"]["optimization"] == value.exportOptimizationConfiguration()
    assert run(live, module="film_optimization", action="save")["accepted"]
    assert not live.editor.snapshot().dirty


def test_optimization_material_selection_needs_exact_available_pair(live):
    value = optimizer(live)
    # Public setter sees a real current material-option row, without running an
    # optimizer or contacting the global material registry.
    value._structure_material_options = [{"material_id": "fixture", "optical_set_id": "nk", "selected": False}]
    run(live, module="film_optimization", action="set_structure_material_selected",
        arguments={"material_id": "fixture", "optical_set_id": "nk", "selected": True})
    assert value.structureMaterialOptions[0]["selected"] is True


@pytest.mark.parametrize("action,args", [
    ("set_solver_option", {"name": "max_iterations", "value": 0}),
    ("set_solver_option", {"name": "unknown", "value": 12}),
    ("set_structure_option", {"name": "max_added_layers", "value": 2}),
    ("set_structure_option", {"name": "growth_steps_nm", "value": [1.0, float("nan")]}),
    ("set_structure_option", {"name": "min_thickness_nm", "value": 1e10}),
])
def test_invalid_optimization_options_keep_configuration_and_revision(live, action, args):
    value = optimizer(live)
    before, editor_before = value.exportOptimizationConfiguration(), live.editor.snapshot()
    with pytest.raises(UiControlError):
        run(live, module="film_optimization", action=action, arguments=args)
    assert value.exportOptimizationConfiguration() == before
    after = live.editor.snapshot()
    assert (after.design, after.revision, after.dirty) == (editor_before.design, editor_before.revision, editor_before.dirty)


@pytest.mark.parametrize("name", ["calculate", "start_optimization", "cancel", "apply_candidate", "confirmApplyCandidate", "new", "saveFilePath", "__getattribute__"])
def test_actions_do_not_open_compute_cancel_confirmation_or_arbitrary_rpc(live, name):
    optimizer(live)
    before = live.editor.snapshot()
    with pytest.raises(UiControlError) as error:
        run(live, module="film_optimization", action=name)
    assert error.value.code == "unsupported_action"
    assert live.editor.snapshot().design == before.design


def test_other_modules_have_explicit_truthful_capability_matrix(live):
    for module in ("main", "source", "materials"):
        capability = project_editor_capabilities(module)
        assert capability["scope"] == "existing_current_editor"
        assert "update_window" in capability["editor_operations"]
        assert "save" not in capability["actions"]
        assert capability["requires"] == ["exact_project_target", "instance_id", "expected_snapshot_id"]
        with pytest.raises(UiControlError) as error:
            project_editor_operation(live.owner, live.session, module, "window_action", {},
                client_generation=live.client.generation, bridge=live.bridge)
        assert error.value.code == "requires_snapshot"
    for module in ("compute_queue", "results"):
        assert project_editor_capabilities(module)["editor_operations"] == []
        with pytest.raises(UiControlError) as error:
            project_editor_operation(live.owner, live.session, module, "update_window", {},
                client_generation=live.client.generation, bridge=live.bridge)
        assert error.value.code == "unsupported_operation"


def test_field_only_modules_do_not_advertise_window_actions():
    from coat_sim.ui.project_application_client import ProjectApplicationClient
    for module in ("main", "materials"):
        capability = ProjectApplicationClient._project_capabilities(module)
        assert capability["editor_operations"] == ["update_window"]
        assert capability["unsupported_operations"] == ["window_action"]
    assert ProjectApplicationClient._project_capabilities("compute_queue")["unsupported_operations"] == [
        "update_window", "window_action"]


def test_real_route_replacement_and_reopened_session_reject_old_writes(live):
    operation, payload = request(live, fields={"name": "Never"})
    route = next(route for route in live.host.routes(live.session.binding.project_id) if route.module_key == "film_design")
    assert live.host.release_window(route)
    replacement = live.host.reserve_module(live.session.binding.project_id, "film_design").route
    live.host.attach_window(replacement, live.windows["film_design"])
    with pytest.raises(UiControlError) as error:
        project_editor_operation(live.owner, live.session, "film_design", operation, payload,
            client_generation=live.client.generation, bridge=live.bridge)
    assert error.value.code == "stale_window"
    assert live.host.close_project(live.session.binding, prepare_close=lambda *_args: True)
    successor = live.host.open_project(live.store.root)
    assert successor.binding.generation != live.session.binding.generation
    with pytest.raises(UiControlError):
        project_editor_operation(live.owner, live.session, "film_design", operation, payload,
            client_generation=live.client.generation, bridge=live.bridge)


def test_native_field_buffer_and_related_busy_controller_block_current_write(live):
    from PySide6.QtCore import QObject
    operation, payload = request(live, fields={"name": "Never"})
    panel = QObject(live.windows["film_design"])
    panel.setObjectName("filmDesignExpressionPanel")
    panel.setProperty("expressionDraftPending", True)
    with pytest.raises(UiControlError):
        project_editor_operation(live.owner, live.session, "film_design", operation, payload,
            client_generation=live.client.generation, bridge=live.bridge)
    assert panel.property("expressionDraftPending") is True
    panel.setProperty("expressionDraftPending", False)
    # A companion's active compute is fenced without cancelling its job.
    live.controllers["film_simulation"] = SimpleNamespace(busy=True)
    with pytest.raises(UiControlError) as error:
        project_editor_operation(live.owner, live.session, "film_design", operation, payload,
            client_generation=live.client.generation, bridge=live.bridge)
    assert error.value.code == "window_busy"
    assert live.editor.snapshot().design["name"] != "Never"


def test_mcp_registry_owner_helper_round_trip_never_uses_global_fallback(live, monkeypatch):
    from coat_sim.assistant_process import ApplicationClientRegistry, AssistantProcessClient, _Client
    from coat_sim.mcp import server
    from mcp.server.mcpserver.exceptions import ToolError
    registry = ApplicationClientRegistry()
    process = _Client(client_id="real-owner", endpoint={"pid": 12345},
        generation=live.client.generation, process_start="held-process", role="project", projects=[])
    registry._clients[process.client_id] = process
    # Replace transport only. All target validation and editor dispatch remain
    # the real registry/application/helper implementations on the GUI thread.
    monkeypatch.setattr(registry, "_status", lambda _client: live.client.status("probe"))
    calls = []
    def owner_call(_client, operation, **arguments):
        calls.append((operation, arguments))
        return live.client.dispatch(operation, arguments)
    monkeypatch.setattr(registry, "_call", owner_call)
    def shared_request(_client, operation, **arguments):
        assert operation == "project_window"
        return registry.project_window(arguments["project_target"],
            arguments["window_operation"], arguments["arguments"])
    monkeypatch.setattr(AssistantProcessClient, "request", shared_request)
    monkeypatch.setattr(server, "_ui_call", lambda *_args, **_kwargs: pytest.fail("global fallback"))
    target = server.ProjectTarget(client_id=process.client_id, client_generation=process.generation,
        project_id=live.session.binding.project_id, session_generation=live.session.binding.generation,
        root=str(live.store.root), project_file=str(live.store.project_file))
    current = server.project_editor_snapshot(target, "film_design")
    result = server.update_window("film_design", fields={"name": "Full routed edit"},
        project_target=target, instance_id=current["instance_id"], expected_snapshot_id=current["snapshot_id"])
    assert result["accepted"] and result["project_target"] == target.model_dump()
    assert live.editor.snapshot().design["name"] == "Full routed edit"
    current = server.project_editor_snapshot(target, "film_design")
    result = server.window_action("film_design", "save", project_target=target,
        instance_id=current["instance_id"], expected_snapshot_id=current["snapshot_id"])
    assert result["accepted"] and not live.editor.snapshot().dirty
    forwarded = next(args for operation, args in calls if operation == "update_window")
    assert forwarded["project_id"] == target.project_id
    assert forwarded["project_file"] == target.project_file
    assert forwarded["instance_id"] == current["instance_id"]
    assert "force" not in forwarded and "data" not in forwarded
    for action in ("calculate", "start_optimization"):
        with pytest.raises(ToolError, match="compute_confirmation_required"):
            server.window_action("film_design", action, project_target=target,
                instance_id=current["instance_id"], expected_snapshot_id=current["snapshot_id"])
    with pytest.raises(ToolError, match="requires_snapshot"):
        server.update_window("film_design", fields={"name": "Guardless"}, project_target=target)


def test_objective_records_update_band_angles_targets_atomically_and_manage_ids(live):
    value = optimizer(live)
    identity = value.exportOptimizationConfiguration()["objectives"][0]["id"]
    # A band far outside the old one is submitted as one validated record, so
    # neither endpoint is temporarily incompatible with the old opposite end.
    record = {"name": "Near infrared", "wavelength_min_nm": 900.0, "wavelength_max_nm": 1200.0,
        "wavelength_samples": 31, "sampling_basis": "samples", "angle_sampling_mode": "range",
        "angle_min_deg": 10.0, "angle_max_deg": 50.0, "angle_step_deg": 10.0,
        "polarization": "s", "metric": "T", "mode": "range", "target_value_percent": 90.0,
        "lower_bound_percent": 85.0, "upper_bound_percent": 100.0, "weight": 2.0}
    run(live, module="film_optimization", action="set_objective", arguments={"objective_id": identity, "record": record})
    row = value.exportOptimizationConfiguration()["objectives"][0]
    assert row["id"] == identity and row["angle_sample_count"] == 5 and row["metric"] == "T"
    assert row["wavelength_min_nm"] == 900 and row["wavelength_max_nm"] == 1200
    assert snapshot(live, "film_optimization")["available"]
    run(live, module="film_optimization", action="duplicate_objective", arguments={"objective_id": identity})
    copied = value.exportOptimizationConfiguration()["objectives"][1]
    assert copied["id"] != identity and copied["metric"] == "T"
    run(live, module="film_optimization", action="add_objective", arguments={"record": {"name": "Extra target"}})
    assert len(value.objectives) == 3
    run(live, module="film_optimization", action="remove_objective", arguments={"objective_id": copied["id"]})
    assert len(value.objectives) == 2
    assert live.editor.workflow_configuration("optimization") == value.exportOptimizationConfiguration()


@pytest.mark.parametrize("record", [
    {"wavelength_min_nm": 950, "wavelength_max_nm": 900},
    {"angle_min_deg": 60, "angle_max_deg": 10},
    {"metric": "A"}, {"lower_bound_percent": 90, "upper_bound_percent": 10},
    {"target_value_percent": 101}, {"weight": 0}, {"id": "other"},
    {"actual_angle_step_deg": 12}, {"wavelength_samples": True}, {"name": []},
])
def test_invalid_objective_record_preserves_configuration_revision_and_readability(live, record):
    value = optimizer(live)
    before, editor_before = value.exportOptimizationConfiguration(), live.editor.snapshot()
    identity = before["objectives"][0]["id"]
    with pytest.raises(UiControlError):
        run(live, module="film_optimization", action="set_objective", arguments={"objective_id": identity, "record": record})
    assert value.exportOptimizationConfiguration() == before
    after = live.editor.snapshot()
    assert (after.design, after.revision, after.dirty) == (editor_before.design, editor_before.revision, editor_before.dirty)
    assert snapshot(live, "film_optimization")["available"]


def test_variable_record_binds_exact_layer_and_replaces_enabled_bounds_once(live):
    from tests.test_project_film_service import bound_design, material
    design = bound_design()
    design.pop("workflow_settings")
    design["front_layers"] = [{"id": "layer-current", "material_id": "shared", "optical_set_id": "visible",
        "thickness": {"value": 100.0, "unit": "nm"}, "note": ""}]
    saved = live.workspace.service.create_film(design, materials={"shared": material()})
    assert live.editor.request_load_saved_design(saved.document_id)
    value = optimizer(live)
    assert value.variables[0]["available"]
    args = {"side": "front", "layer_id": "layer-current", "enabled": True, "lower_nm": 500.0, "upper_nm": 700.0}
    run(live, module="film_optimization", action="set_variable", arguments=args)
    row = value.exportOptimizationConfiguration()["variables"][0]
    assert row == args
    before, editor_before = value.exportOptimizationConfiguration(), live.editor.snapshot()
    for invalid in (args | {"lower_nm": 800.0}, args | {"enabled": "yes"}, args | {"layer_id": "old"}):
        with pytest.raises(UiControlError):
            run(live, module="film_optimization", action="set_variable", arguments=invalid)
        assert value.exportOptimizationConfiguration() == before
        assert live.editor.snapshot().revision == editor_before.revision
        assert snapshot(live, "film_optimization")["available"]
    run(live, module="film_optimization", action="set_variable", arguments=args | {"enabled": False})
    assert not value.exportOptimizationConfiguration()["variables"][0]["enabled"]


def test_objective_sampling_basis_follows_existing_edit_semantics(live):
    value = optimizer(live)
    identity = value.exportOptimizationConfiguration()["objectives"][0]["id"]
    run(live, module="film_optimization", action="set_objective",
        arguments={"objective_id": identity, "record": {"wavelength_step_nm": 20.0}})
    row = value.exportOptimizationConfiguration()["objectives"][0]
    assert row["sampling_basis"] == "step" and row["wavelength_samples"] == 16
    run(live, module="film_optimization", action="set_objective",
        arguments={"objective_id": identity, "record": {"wavelength_samples": 11}})
    row = value.exportOptimizationConfiguration()["objectives"][0]
    assert row["sampling_basis"] == "samples" and row["wavelength_samples"] == 11
    before = value.exportOptimizationConfiguration()
    with pytest.raises(UiControlError):
        run(live, module="film_optimization", action="set_objective",
            arguments={"objective_id": identity, "record": {"wavelength_samples": 5, "wavelength_step_nm": 2}})
    assert value.exportOptimizationConfiguration() == before


def test_differential_evolution_population_cross_fields_remain_valid_and_readable(live):
    value = optimizer(live)
    if next(row["available"] for row in value.solverOptions if row["id"] == "differential_evolution"):
        run(live, module="film_optimization", fields={"solver": "differential_evolution"})
        before, editor_before = value.exportOptimizationConfiguration(), live.editor.snapshot()
        with pytest.raises(UiControlError):
            run(live, module="film_optimization", action="set_solver_option", arguments={"name": "population_size", "value": 1})
        assert value.exportOptimizationConfiguration() == before
        assert live.editor.snapshot().revision == editor_before.revision
        assert live.editor.snapshot().dirty == editor_before.dirty
        assert snapshot(live, "film_optimization")["available"]
    run(live, module="film_optimization", fields={"solver": "baseline"})
    run(live, module="film_optimization", action="set_solver_option", arguments={"name": "population_size", "value": 1})
    before, editor_before = value.exportOptimizationConfiguration(), live.editor.snapshot()
    with pytest.raises(UiControlError):
        run(live, module="film_optimization", fields={"solver": "differential_evolution"})
    assert value.exportOptimizationConfiguration() == before
    assert live.editor.snapshot().revision == editor_before.revision
    assert live.editor.snapshot().dirty == editor_before.dirty
    assert snapshot(live, "film_optimization")["available"]


def simulation(live, monkeypatch):
    from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
    monkeypatch.setattr(live.workspace.service, "simulate", lambda *_args, **_kwargs: pytest.fail("numerical simulation"))
    value = FilmSimulationQuickController(service=live.workspace.service, source_controller=live.source,
                                         project_context=live.workspace)
    live.mount("film_simulation", value)
    monkeypatch.setattr(value, "startCalculation", lambda: pytest.fail("compute start"))
    return value


@pytest.mark.parametrize("key,value", [
    ("wavelength_min_nm", 450.0), ("wavelength_max_nm", 750.0), ("samples", 35),
    ("step_nm", 3.0), ("angle_deg", 45.0), ("polarization", "p"),
    ("first_surface", "back"), ("extrapolation_policy", "error"),
])
def test_simulation_parameter_whitelist_is_actual_current_controller_and_never_computes(live, monkeypatch, key, value):
    current = simulation(live, monkeypatch)
    before = snapshot(live, "film_simulation")
    assert before["available"] and before["simulation"]["source"] == "current_controller"
    result = run(live, module="film_simulation", fields={key: value})
    assert result["accepted"] and result["dirty"]
    if key == "step_nm":
        assert current.settings["samples"] == 101
    else:
        assert current.settings[key] == value
    assert live.editor.workflow_configuration("simulation") == current.exportSimulationConfiguration()
    assert run(live, module="film_simulation", action="save")["accepted"]


@pytest.mark.parametrize("key,value", [
    ("wavelength_min_nm", 800.0), ("wavelength_max_nm", 300.0), ("samples", 1),
    ("samples", True), ("step_nm", 10000), ("angle_deg", 90.0), ("polarization", []),
    ("first_surface", "other"), ("extrapolation_policy", "other"), ("light_source", {"kind": "flat"}),
])
def test_invalid_simulation_input_is_atomic_and_still_readable(live, monkeypatch, key, value):
    current = simulation(live, monkeypatch)
    before, editor_before = current.exportSimulationConfiguration(), live.editor.snapshot()
    with pytest.raises(UiControlError):
        run(live, module="film_simulation", fields={key: value})
    assert current.exportSimulationConfiguration() == before
    assert live.editor.snapshot().revision == editor_before.revision
    assert snapshot(live, "film_simulation")["available"]
