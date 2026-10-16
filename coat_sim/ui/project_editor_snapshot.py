"""Bounded, read-only projection of existing project editor owners.

This module never obtains lazy ``workspace.editor`` or calls service/catalog I/O.
Native field buffers are not submitted: while they exist the read is unavailable.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import math
from typing import Any, TYPE_CHECKING, cast

from PySide6.QtCore import QCoreApplication, QObject, QThread

from coat_sim.control_errors import UiControlError
from coat_sim.project_session import ProjectSessionError, ProjectSessionHost
from coat_sim.project_ui import PROJECT_EDITOR_DETAILS, project_window_arguments

if TYPE_CHECKING:
    from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace

FILM_MODULES = frozenset({"film_design", "film_simulation", "film_optimization"})
RESOURCE_MODULES = frozenset({"source", "substrate", "machine", "mask", "process", "materials"})

MAX_RESPONSE_BYTES = 128 * 1024
MAX_TEXT = 256


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":")).encode("utf-8")


class _Projection:
    def __init__(self) -> None:
        self.truncated = False

    def value(self, value: Any, depth: int = 0) -> Any:
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            if len(value) > MAX_TEXT:
                self.truncated = True
            return value[:MAX_TEXT]
        if depth >= 5:
            self.truncated = True
            return None
        if isinstance(value, Mapping):
            if len(value) > 64:
                self.truncated = True
            return {str(key)[:64]: self.value(item, depth + 1)
                    for key, item in list(value.items())[:64]}
        if isinstance(value, (list, tuple)):
            if len(value) > 100:
                self.truncated = True
            return [self.value(item, depth + 1) for item in value[:100]]
        return None

    def page(self, rows: list, offset: int, limit: int) -> dict:
        end = min(len(rows), offset + limit)
        return {"items": [self.value(row) for row in rows[offset:end]],
                "offset": offset, "limit": limit, "total": len(rows),
                "next_offset": end if end < len(rows) else None,
                "truncated": offset > 0 or end < len(rows)}


def _unavailable(identity: dict, reason: str) -> dict:
    return identity | {"available": False, "state": "unavailable", "reason": reason,
                       "source": "current_editor", "saved_fallback": False}


def _field_draft(window: Any) -> bool:
    """Inspect only existing native/QML flags; never commit, blur or normalize."""
    finder = getattr(window, "findChild", None)
    panel = finder(QObject, "filmDesignExpressionPanel") if callable(finder) else None
    if panel is not None and panel.property("expressionDraftPending"):
        return True
    focus = getattr(window, "activeFocusItem", lambda: None)()
    # Conservatively withhold an editable native text buffer, even when its
    # text happens to equal committed state. Moving focus is the user's choice.
    while focus is not None and focus is not window:
        prop = getattr(focus, "property", None)
        if callable(prop):
            if prop("inputMethodComposing") or prop("numericValueEdited"):
                return True
            if (prop("text") is not None and prop("readOnly") is not True
                    and hasattr(focus, "editingFinished")):
                return True
        parent = getattr(focus, "parentItem", None)
        focus = parent() if callable(parent) else None
    return False


def _context_modal_pending(window: Any, module: str) -> bool:
    """Inspect only existing modal children owned by this registered window.

    Local dialogs can hold unsubmitted edits or decisions with no Python flag.
    Unknown visible secondary modals therefore fail closed. Read-only reference
    views and resource information in view mode remain readable. Never inspect
    another window, materialize a popup, or move focus to discover its state.
    """
    finder = getattr(window, "findChildren", None)
    if not callable(finder):
        return False
    read_only = {"materialReferenceDialog", "substrateMaterialReferenceDialog",
                 "materialReferenceDetailsDialog", "historyDialog", "maskCadReportDialog",
                 "projectModuleMenu"}
    try:
        pending_open = (("editorLoading", "editorOpenRequested") if module == "materials" else
                        ("sourceReferenceOpenRequested", "materialLibraryOpenRequested")
                        if module in {"source", "substrate"} else ())
        if any(window.property(name) is True for name in pending_open):
            return True
        primary = window.property("editorInstance") if module == "materials" else None
        for dialog in finder(QObject):
            if dialog is primary:
                continue  # The materials module's own primary editor is a Popup.
            if dialog.property("modal") is not True or not dialog.property("visible"):
                continue
            name = dialog.objectName()
            if name in read_only:
                continue
            if (name == "projectResourceInformationDialog"
                    and dialog.property("editing") is not True):
                continue
            return True
    except (RuntimeError, TypeError):
        return True
    return False


def _material_refs(design: Mapping, configuration: Mapping | None) -> list[tuple[str, str]]:
    rows = [design.get("substrate", {}), design.get("incident_medium", {}),
            design.get("exit_medium", {})]
    rows.extend(design.get("front_layers", []))
    rows.extend(design.get("back_layers", []))
    rows.extend(design.get("expression", {}).get("symbols", {}).values())
    if configuration is not None:
        rows.extend(configuration.get("structure_materials", []))
    return sorted({(str(row.get("material_id", "")), str(row.get("optical_set_id", "")))
                   for row in rows if isinstance(row, Mapping) and row.get("material_id")})


def _material_metadata(editor: Any, references: list[tuple[str, str]]) -> list[dict]:
    # These are captured draft values, not the mutable picker/global catalog.
    retained = getattr(editor, "_retained_material_snapshots", {})
    frozen = getattr(editor, "_frozen_draft_materials", None) or {}
    result = []
    for material_id, optical_id in references:
        record = retained.get(material_id) or frozen.get(material_id)
        optical = next((item for item in (record or {}).get("optical_sets", [])
                        if item.get("id") == optical_id), None)
        row: dict[str, Any] = {"material_id": material_id, "optical_set_id": optical_id,
               "source": "captured_draft" if record is not None else "unknown",
               "name": (record or {}).get("name"), "available": optical is not None}
        if optical is not None:
            k = optical.get("k")
            k_data = optical.get("k_data")
            row["optical_data"] = {
                key: optical.get(key) for key in ("kind", "wavelength_min", "wavelength_max", "wavelength_unit")}
            row["optical_data"].update(
                n_sample_count=len(optical["n"]) if isinstance(optical.get("n"), (list, tuple)) else None,
                k_sample_count=len(k) if isinstance(k, (list, tuple)) else None,
                k_data_present=True if isinstance(k, (list, tuple)) and k else (
                    True if isinstance(k_data, Mapping) else None),
                lossless="unknown")
            if isinstance(k_data, Mapping):
                row["optical_data"]["k_domain"] = {key: k_data.get(key) for key in
                    ("wavelength_min", "wavelength_max", "wavelength_unit")}
        result.append(row)
    return result


def _layers(snapshot: Any) -> list[dict]:
    result = []
    for side in ("front", "back"):
        for index, row in enumerate(snapshot.design.get(side + "_layers", [])):
            identity = str(row.get("id", ""))
            physical = snapshot.derived.get(side, {}).get(identity, {}).get("physicalNmValue")
            if not isinstance(physical, (int, float)) or isinstance(physical, bool) or not math.isfinite(physical):
                physical = None
            result.append({"side": side, "index": index, "id": identity,
                           "material_id": row.get("material_id"),
                           "optical_set_id": row.get("optical_set_id"),
                           "thickness": row.get("thickness"), "physical_thickness_nm": physical})
    return result


def _optimizer_state(controller: Any, editor: Any, snapshot: Any,
                     windows: list[Any]) -> tuple[dict | None, str | None]:
    if controller is None:
        return None, "window_closed"
    if getattr(controller, "session", None) is not editor:
        return None, "editor_owner_mismatch"
    if (getattr(controller, "busy", False) or getattr(controller, "_shutting_down", False)
            or getattr(controller, "_configuration_loading", False)):
        return None, "busy"
    if (getattr(controller, "_pending_edit_error", False)
            or getattr(controller, "_configuration_load_error", "")):
        return None, "configuration_unavailable"
    if (getattr(controller, "_source_closed", False)
            or getattr(controller, "_source_stale", False)
            or getattr(controller, "_source_revision", None) != snapshot.design_revision):
        return None, "stale_source"
    if any(_field_draft(window) for window in windows):
        return None, "native_field_editing"
    export = getattr(controller, "exportOptimizationConfiguration", None)
    if not callable(export):
        return None, "controller_unavailable"
    return export(), None


def _simulation_state(controller: Any, editor: Any, snapshot: Any) -> tuple[dict | None, str | None]:
    if controller is None:
        return None, "window_closed"
    if getattr(controller, "_source_session", None) is not editor:
        return None, "editor_owner_mismatch"
    if getattr(controller, "busy", False) or getattr(controller, "_closed", False):
        return None, "busy"
    if (getattr(controller, "_pending_window_open", None) is not None
            or getattr(controller, "_pending_save_path", None) is not None
            or getattr(controller, "_pending_close", False)):
        return None, "pending_confirmation"
    physical_design = {key: value for key, value in getattr(controller, "_design", {}).items()
                       if key != "workflow_settings"}
    current_physical = {key: value for key, value in snapshot.design.items() if key != "workflow_settings"}
    if (getattr(controller, "_source_sync_error", "")
            or getattr(controller, "_source_design_revision", None) != snapshot.design_revision
            or physical_design != current_physical):
        return None, "stale_source"
    export = getattr(controller, "exportSimulationConfiguration", None)
    if not callable(export):
        return None, "controller_unavailable"
    return export(), None


def _simulation_projection(configuration: dict, raw: dict) -> tuple[dict, bool]:
    """Keep exact scalar UI settings, replace sampled light tables by metadata."""
    parameters = configuration.get("parameters", {})
    light = parameters.get("light_source", {})
    compact_light = {key: value for key, value in light.items()
                     if key not in {"wavelength_nm", "intensity"}}
    waves, intensity = light.get("wavelength_nm"), light.get("intensity")
    omitted = isinstance(waves, (list, tuple)) or isinstance(intensity, (list, tuple))
    if omitted:
        compact_light.update(sample_count=len(waves) if isinstance(waves, (list, tuple)) else None,
            wavelength_min_nm=waves[0] if isinstance(waves, (list, tuple)) and waves else None,
            wavelength_max_nm=waves[-1] if isinstance(waves, (list, tuple)) and waves else None,
            samples_omitted=True)
    result = {**configuration, "parameters": {**parameters, "light_source": compact_light}}
    result["selected_light_source_id"] = raw.get("light_source_id")
    result["sampling"] = {"authoritative": "exact_sample_count", "samples": raw.get("samples"),
                          "effective_step_nm": raw.get("step_nm"), "last_edited_basis": "not_recorded"}
    return result, omitted



def _state_digest(value: Any) -> str:
    """Hash full logical state without allocating a second serialized document.

    Nonfinite numbers are only internal signature tokens. The public projection
    still converts them to null, and never emits nonstandard JSON numbers.
    """
    digest = sha256()
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=True,
                               separators=(",", ":"))
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _registered_context_snapshot(launcher: Any, session: Any, module: str, arguments: dict,
                                 identity: dict, workspace: Any, compute: Any) -> dict:
    """Project scope is already held; only inspect existing registered owners."""
    host: ProjectSessionHost = launcher.host
    binding = session.binding
    if (getattr(workspace, "host", None) is not host
            or getattr(workspace, "binding", None) != binding):
        return _unavailable(identity, "editor_owner_mismatch")
    material_controller = None
    if module == "materials":
        from .project_asset_materials import existing_material_view
        route, window, material_controller = existing_material_view(host, binding)
    else:
        route = next((row for row in host.routes(binding.project_id) if row.module_key == module), None)
        window = host.window_for(route) if route is not None else None
    if route is None or window is None:
        return _unavailable(identity, "window_closed")
    identity = identity | {"instance_id": route.route_id}
    controller = material_controller if module == "materials" else getattr(window, "_project_film_controller", None)
    if controller is None:
        return _unavailable(identity, "controller_unavailable")
    if module == "materials" and route.module_key == "project_resources":
        assets = getattr(window, "_project_film_controller", None)
        if getattr(assets, "deletionBusy", False) or getattr(assets, "assetApplying", False):
            return _unavailable(identity, "busy")
    if getattr(controller, "project_context", None) is not workspace:
        return _unavailable(identity, "editor_owner_mismatch")
    store = host.store_for(binding)
    if module == "project_resources":
        library_resources = host.resource_for(binding, "resource_workspace")
        service = getattr(controller, "service", None)
        if (library_resources is None or getattr(controller, "resource_workspace", None) is not library_resources
                or getattr(controller, "owner_binding", None) != binding
                or getattr(controller, "window", None) is not window
                or getattr(service, "binding", None) != binding
                or getattr(service, "_session_host", None) is not host
                or getattr(service, "store", None) is not store):
            return _unavailable(identity, "editor_owner_mismatch")
        if getattr(library_resources, "_closing", False) or getattr(getattr(service, "_gate", None), "frozen", False):
            return _unavailable(identity, "project_frozen")
        from .project_resources import capture_project_resources_snapshot
        capture = capture_project_resources_snapshot
    elif module in RESOURCE_MODULES:
        resource_owner = host.resource_for(binding, "resource_workspace")
        if resource_owner is None:
            return _unavailable(identity, "editor_owner_mismatch")
        resources = cast("ProjectResourceWorkspace", resource_owner)
        if (getattr(resources, "host", None) is not host
                or getattr(resources, "binding", None) != binding
                or getattr(resources, "store", None) is not store
                or getattr(controller, "owner_binding", None) != binding
                or getattr(controller, "resource_workspace", None) is not resources
                or resources.controllers.get(module) is not controller
                or getattr(controller, "window", None) is not window
                or resources.windows.get(module) is not window):
            return _unavailable(identity, "editor_owner_mismatch")
        service = getattr(controller, "service", None)
        if (getattr(service, "binding", None) != binding
                or getattr(service, "_session_host", None) is not host
                or getattr(service, "store", None) is not store):
            return _unavailable(identity, "editor_owner_mismatch")
        resource_bridge = getattr(controller, "bridge", None)
        bridge_service = (getattr(resource_bridge, "_service", None) if module in {"machine", "mask"}
                          else getattr(resource_bridge, "project_service", None) if module == "materials"
                          else getattr(resource_bridge, "service", None))
        if (getattr(bridge_service, "binding", None) != binding
                or getattr(bridge_service, "_session_host", None) is not host
                or getattr(bridge_service, "store", None) is not store):
            return _unavailable(identity, "editor_owner_mismatch")
        if getattr(resources, "_closing", False) or getattr(getattr(service, "_gate", None), "frozen", False):
            return _unavailable(identity, "project_frozen")
        from .project_resource_snapshot import capture_resource_snapshot
        capture = capture_resource_snapshot
    else:
        if (compute is None or getattr(compute, "host", None) is not host
                or getattr(compute, "binding", None) != binding
                or getattr(compute, "store", None) is not store
                or getattr(controller, "_project_compute", None) is not compute):
            return _unavailable(identity, "editor_owner_mismatch")
        if module == "main" and getattr(controller, "service", None) is not getattr(compute, "service", None):
            return _unavailable(identity, "editor_owner_mismatch")
        if module in {"ray_trace", "film_appearance"} and getattr(controller, "root", None) != getattr(compute, "root", None):
            return _unavailable(identity, "editor_owner_mismatch")
        if module != "compute_queue":
            inputs = getattr(controller, "_project_input_resources", None)
            registered_resources = host.resource_for(binding, "resource_workspace")
            if (inputs is None or getattr(inputs, "project_context", None) is not workspace
                    or getattr(inputs, "owner_binding", None) != binding
                    or getattr(inputs, "resource_workspace", None) is not registered_resources
                    or getattr(compute, "resource_workspace", None) is not registered_resources
                    or getattr(inputs, "bridge", None) is not controller
                    or getattr(inputs, "compute", None) is not compute):
                return _unavailable(identity, "editor_owner_mismatch")
        from .project_compute_snapshot import capture_compute_snapshot
        capture = capture_compute_snapshot
    if _context_modal_pending(window, module):
        return _unavailable(identity, "pending_confirmation")
    if _field_draft(window):
        return _unavailable(identity, "native_field_editing")
    state, reason = capture(controller, window, module)
    if reason or state is None:
        return _unavailable(identity, reason or "editor_unavailable")
    summary, collections = state["summary"], state["collections"]
    try:
        snapshot_id = _state_digest({"identity": identity, "summary": summary,
                                    "collections": collections, "state": state["signature"]})
    except (TypeError, ValueError, OverflowError):
        return _unavailable(identity, "invalid_editor_state")
    expected = arguments.get("expected_snapshot_id")
    if expected is not None and expected != snapshot_id:
        raise UiControlError("stale_snapshot", "当前窗口内容或已载入视图已改变，请重新读取摘要。")
    project = _Projection()
    project.truncated = bool(state.get("content_truncated", False))
    detail, offset, limit = arguments.get("detail", "summary"), arguments.get("offset", 0), arguments.get("limit", 50)
    result = identity | {"available": True, "state": "ready", "source": "current_editor",
                         "saved_fallback": False, "snapshot_id": snapshot_id, "detail": detail,
                         "available_details": sorted(PROJECT_EDITOR_DETAILS[module]),
                         "data_trust": "user_document_data"}
    if detail == "summary":
        result["summary"] = project.value(summary)
        result["collections"] = {key: project.page(rows, offset, limit) for key, rows in collections.items()}
        pages_truncated = any(page["truncated"] for page in result["collections"].values())
    else:
        result["page"] = project.page(collections.get(detail, []), offset, limit)
        pages_truncated = result["page"]["truncated"]
    result["content_truncated"] = project.truncated
    result["truncated"] = project.truncated or pages_truncated
    if len(_json(result)) > MAX_RESPONSE_BYTES - 16 * 1024:
        return _unavailable(identity, "response_budget_exceeded") | {"max_response_bytes": MAX_RESPONSE_BYTES,
            "retry": "Use a smaller limit or a single collection detail."}
    return result


def project_editor_snapshot(launcher: Any, session: Any, module: str, arguments: dict,
                            *, client_generation: str, bridge: Any) -> dict:
    """Capture an existing editor in one GUI turn and fail closed on any fence."""
    project_window_arguments("project_editor_snapshot", {"window_type": module, **arguments})
    identity = {"project_id": session.binding.project_id,
                "session_generation": session.binding.generation,
                "client_generation": client_generation, "module_key": module}
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        raise UiControlError("gui_thread_required", "当前工程内容无法在此处读取，请在所属工程窗口重新查看。")
    if getattr(bridge, "_project_starting", False):
        return _unavailable(identity, "initializing")
    lifecycle = getattr(bridge, "project_lifecycle", None)
    if (getattr(lifecycle, "active", False) or launcher.host.admission_frozen
            or getattr(launcher, "_busy", False)):
        return _unavailable(identity, "project_frozen")
    try:
        with launcher.host.operation_scope(session.binding):
            workspace = launcher.host.resource_for(session.binding, "film_workspace")
            if workspace is None:
                return _unavailable(identity, "editor_unavailable")
            if getattr(workspace, "_resource_replacement_in_progress", False):
                return _unavailable(identity, "ownership_transfer")
            compute = launcher.host.resource_for(session.binding, "compute_workspace")
            if getattr(compute, "editing_frozen", False):
                return _unavailable(identity, "project_frozen")
            if module not in FILM_MODULES:
                return _registered_context_snapshot(launcher, session, module, arguments, identity, workspace, compute)
            editor = getattr(workspace, "_editor", None)
            if editor is None:
                return _unavailable(identity, "editor_unavailable")
            routes = {route.module_key: route for route in launcher.host.routes(session.binding.project_id)
                      if route.module_key in {"film_design", "film_optimization", "film_simulation"}}
            windows = {key: launcher.host.window_for(route) for key, route in routes.items()}
            if windows.get(module) is None:
                return _unavailable(identity, "window_closed")
            identity["instance_id"] = routes[module].route_id
            desktop = getattr(launcher, "desktop", None)
            lookup = getattr(desktop, "controller_for", None)
            controller = lookup(workspace, module) if callable(lookup) else None
            source_session = getattr(controller, "_source_session", None) if module == "film_simulation" else getattr(controller, "session", None)
            if controller is None or source_session is None or source_session is not editor:
                return _unavailable(identity, "controller_unavailable")
            if (getattr(controller, "initializationReady", True) is not True
                    or getattr(controller, "_source_closed", False)):
                return _unavailable(identity, "initializing")
            if any(_field_draft(window) for window in windows.values() if window is not None):
                return _unavailable(identity, "native_field_editing")
            if getattr(controller, "busy", False):
                return _unavailable(identity, "busy")
            # Project -> editor is the established lock order. No GUI callback,
            # event processing, provider, service or catalog lookup is invoked.
            with editor._lock:
                snapshot = editor.snapshot()
                if snapshot.expression_busy:
                    return _unavailable(identity, "expression_busy")
                if snapshot.pending_confirmation:
                    return _unavailable(identity, "pending_confirmation")
                if getattr(editor, "_pending_information_edit", None) is not None:
                    return _unavailable(identity, "pending_information_edit")
                optimization = lookup(workspace, "film_optimization") if (
                    callable(lookup) and windows.get("film_optimization") is not None) else None
                configuration, optimizer_reason = _optimizer_state(
                    optimization, editor, snapshot,
                    [window for window in windows.values() if window is not None])
                if module == "film_optimization" and optimizer_reason:
                    return _unavailable(identity, optimizer_reason)
                simulation = lookup(workspace, "film_simulation") if (
                    callable(lookup) and windows.get("film_simulation") is not None) else None
                simulation_configuration, simulation_reason = _simulation_state(simulation, editor, snapshot)
                simulation_ui_state = dict(getattr(simulation, "_params", {})) if simulation_configuration is not None else None
                if module == "film_simulation" and simulation_reason:
                    return _unavailable(identity, simulation_reason)
                materials = _material_metadata(editor, _material_refs(snapshot.design, configuration))
                layers = _layers(snapshot)
                # Export is synchronous on the same Qt thread. A background
                # session mutation is excluded by this session lock.
                if editor._revision != snapshot.revision:
                    raise UiControlError("stale_snapshot", "读取期间当前内容已改变，请重新读取。")
    except ProjectSessionError as exc:
        if exc.code in {"application_exiting", "project_closing"}:
            return _unavailable(identity, "project_frozen")
        raise UiControlError(exc.code, str(exc)) from exc

    signature = {"identity": identity, "revision": snapshot.revision,
                 "design_revision": snapshot.design_revision, "dirty": snapshot.dirty,
                 "path": snapshot.path, "design": snapshot.design,
                 "materials": materials, "configuration": configuration,
                 "optimizer_reason": optimizer_reason,
                 "simulation_configuration": simulation_configuration,
                 "simulation_reason": simulation_reason, "simulation_ui_state": simulation_ui_state,
                 "related_instances": {key: route.route_id for key, route in routes.items()}}
    snapshot_id = sha256(_json(signature)).hexdigest()
    expected = arguments.get("expected_snapshot_id")
    if expected is not None and expected != snapshot_id:
        raise UiControlError("stale_snapshot", "当前内容或优化设置已改变，请重新读取摘要。")
    project = _Projection()
    detail, offset, limit = arguments.get("detail", "summary"), arguments.get("offset", 0), arguments.get("limit", 50)
    configuration = configuration or {}
    collections = {"layers": layers, "materials": materials,
                   "objectives": configuration.get("objectives", []),
                   "variables": configuration.get("variables", [])}
    result = identity | {"available": True, "state": "ready", "source": "current_editor",
                         "saved_fallback": False, "snapshot_id": snapshot_id,
                         "revision": snapshot.revision, "design_revision": snapshot.design_revision,
                         "dirty": snapshot.dirty, "detail": detail}
    if detail == "summary":
        physical = [row["physical_thickness_nm"] for row in layers if row["physical_thickness_nm"] is not None]
        result["design"] = project.value({key: snapshot.design.get(key) for key in
            ("id", "name", "layer_order", "reference_wavelength_nm", "substrate", "incident_medium", "exit_medium", "assume_lossless", "use_k", "first_surface")})
        total = sum(physical) if len(physical) == len(layers) else None
        total_overflow = total is not None and not math.isfinite(total)
        if total_overflow:
            total = None
        result["design"].update(layer_count=len(layers),
            front_layer_count=len(snapshot.design.get("front_layers", [])),
            back_layer_count=len(snapshot.design.get("back_layers", [])),
            physical_thickness_nm={"min": min(physical) if physical else None,
                                   "max": max(physical) if physical else None,
                                   "total": total, "total_overflow": total_overflow,
                                   "unknown_count": len(layers) - len(physical)},
            expression_in_sync=snapshot.design.get("expression", {}).get("in_sync"))
        result["optimization"] = {"available": optimizer_reason is None,
                                   "source": "current_controller" if optimizer_reason is None else None,
                                   "reason": optimizer_reason}
        if optimizer_reason is None:
            result["optimization"].update(
                objective_count=len(configuration.get("objectives", [])),
                variable_count=len(configuration.get("variables", [])),
                enabled_variable_count=sum(row.get("enabled") is True for row in configuration.get("variables", [])))
            result["optimization"]["settings"] = project.value({key: configuration.get(key) for key in
                ("version", "solver", "search_mode", "structure_mode", "structure_options")})
            result["optimization"]["structure_materials"] = project.page(configuration.get("structure_materials", []), offset, limit)
        result["simulation"] = {"available": simulation_reason is None,
                                 "source": "current_controller" if simulation_reason is None else None,
                                 "reason": simulation_reason}
        if simulation_reason is None:
            # A ready simulation always has the exported configuration and its UI state.
            assert simulation_configuration is not None and simulation_ui_state is not None
            compact_simulation, samples_omitted = _simulation_projection(simulation_configuration, simulation_ui_state)
            result["simulation"]["configuration"] = project.value(compact_simulation)
            result["simulation"]["light_source_samples_omitted"] = samples_omitted
            project.truncated = project.truncated or samples_omitted
        result["collections"] = {name: project.page(rows, offset, limit) for name, rows in collections.items()}
    elif detail in {"objectives", "variables"} and optimizer_reason is not None:
        return _unavailable(identity, "optimization_" + optimizer_reason)
    else:
        result["page"] = project.page(collections[detail], offset, limit)
    result["content_truncated"] = project.truncated
    if detail == "summary":
        pages_truncated = any(page["truncated"] for page in result["collections"].values())
        pages_truncated = pages_truncated or bool(result["optimization"].get("structure_materials", {}).get("truncated"))
    else:
        pages_truncated = result["page"]["truncated"]
    result["truncated"] = project.truncated or pages_truncated
    result["data_trust"] = "user_document_data"
    # Reserve space for the registry-added six-field project_target.
    if len(_json(result)) > MAX_RESPONSE_BYTES - 16 * 1024:
        return _unavailable(identity, "response_budget_exceeded") | {"max_response_bytes": MAX_RESPONSE_BYTES,
            "retry": "Use a smaller limit or a single collection detail."}
    return result
