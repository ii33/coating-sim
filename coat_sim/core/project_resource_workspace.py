"""Headless owner for explicitly bound resource adapters and retained drafts."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from coat_sim.project_session import ProjectBinding, ProjectSessionHost
from .project_resource_service import ProjectResourceService
from .project_store import ProjectStore
from .project_default_loading import ProjectDefaultPlan
from .project_dependencies import MAX_FILE_BYTES, ProjectError, json_bytes, normalize_document
import json
import hashlib


EDITOR_STATE_FIELDS = {
    "process": ("_data",), "machine": ("_data",), "mask": ("_data",),
    "source": ("_fields", "_material", "_thermochemistry", "_emission", "_emission_present", "_apertures", "_preview_rate"),
    "substrate": ("_fields", "_material", "_points", "_closed"),
    "materials": ("_draft", "_optical_sets", "_roles", "_category", "_reference_data", "_research_data",
                  "_extra_profile_data", "_null_models", "_optical_only", "_saved_id", "_selected_key", "_selected_revisions"),
}


def pending_document_fields(kind: str, state: dict) -> set[str]:
    """Only named document text controls may appear in a recovery payload."""
    data = state.get("_data", {})
    if not isinstance(data, dict):
        return set()
    def count(field):
        rows = data.get(field, [])
        if not isinstance(rows, list):
            return 0
        if len(rows) > 10000:
            raise ProjectError("capacity_exceeded", "Pending resource recovery exceeds the row safety limit")
        return len(rows)
    if kind == "process":
        result = {"processNameField", "processMachineField", "processSubstrateField"}
        for index in range(count("steps")):
            result.update(prefix + str(index) for prefix in
                          ("stepThicknessField", "stepRateField", "stepMonitorSlotField"))
        return result
    if kind == "machine":
        result = {"solarRadiusField", "planetRadiusField"}
        result.update("geometryField_" + key for key in ("carrier_x_mm", "carrier_y_mm", "planet_offset_mm",
                      "planet_tilt_deg", "substrate_z_mm", "sun_teeth", "planet_teeth", "gear_module_mm"))
        for index in range(count("sources")):
            result.update(prefix + str(index) for prefix in
                          ("sourceFileField", "sourceXField", "sourceYField", "sourceZField"))
        result.update("maskFileField" + str(index) for index in range(count("masks")))
        for index in range(count("monitors")):
            result.add("monitorNameField" + str(index))
            result.update("monitorField" + str(index) + axis for axis in ("x_mm", "y_mm", "z_mm"))
        return result
    return {"maskHeightField"} if kind == "mask" else set()


class _RecoveredResourceDraft:
    """A lazy recovered draft remains owned and saveable without constructing Qt."""
    def __init__(self, workspace, kind, record):
        self.workspace, self.kind, self.record = workspace, kind, record
        self.display_name = kind

    @property
    def dirty(self):
        return self.record["dirty"] or bool(self.record.get("nested_material", {}).get("dirty"))

    def commit_pending_edits(self):
        return True

    def snapshot(self):
        return deepcopy(self.record)

    def _set_status(self, value):
        self.workspace._recovery_error = str(value)

    def prepare_save_operations(self):
        if self.record.get("pending_information") is not None:
            raise ProjectError("unconfirmed_information", "Recovered information edits must be opened and confirmed or cancelled before saving")
        from .project_material_library import ProjectMaterialLibrary
        new, updates, revisions = [], {}, {}
        if self.record["dirty"]:
            data = self.record["data"]
            if data is None:
                raise ProjectError("invalid_document", "Recovered " + self.kind + " draft has invalid text; open it before saving")
            identity = self.record["document_id"]
            if self.kind == "materials":
                profile = data["material"]
                if identity is None:
                    new.append(("material_profile", data))
                else:
                    updates[identity] = data
                    revisions[identity] = self.record["revision"]
                revisions.update(self.record["editor_state"]["_selected_revisions"])
                ProjectMaterialLibrary(self.workspace.service)._optical_operation(
                    profile, new, updates, revisions, allow_existing=True)
            elif identity is None:
                new.append((self.kind, data))
            else:
                updates[identity] = data
                revisions[identity] = self.record["revision"]
        nested = self.record.get("nested_material")
        if nested is not None and nested["dirty"]:
            additions, changes, expected = _RecoveredResourceDraft(self.workspace, "materials", nested).prepare_save_operations()
            new.extend(additions)
            updates.update(changes)
            revisions.update(expected)
        return new, updates, revisions

    def adopt_saved_transaction(self, created):
        service = self.workspace.service
        if self.record["dirty"]:
            identity = self.record["document_id"]
            managed_kind = "material_profile" if self.kind == "materials" else self.kind
            if identity is None:
                identity = next(identity for kind, identity in created if kind == managed_kind and (
                    self.kind != "materials" or service.load_document(identity).data["material"]["id"] ==
                    self.record["data"]["material"]["id"]))
            self.record["document_id"] = identity
            self.record["revision"] = service._record(identity)["sha256"]
            self.record["data"] = service.load_document(identity).data
            if "_data" in self.record["editor_state"]:
                self.record["editor_state"]["_data"] = deepcopy(self.record["data"])
            self.record["dirty"] = False
            if self.kind == "materials":
                from .project_material_library import ProjectMaterialLibrary
                state = self.record["editor_state"]
                state["_saved_id"] = self.record["data"]["material"]["id"]
                state["_selected_key"] = "user:" + state["_saved_id"]
                state["_selected_revisions"] = ProjectMaterialLibrary(service).revisions(state["_saved_id"])
        nested = self.record.get("nested_material")
        if nested is not None:
            _RecoveredResourceDraft(self.workspace, "materials", nested).adopt_saved_transaction(created)


class ProjectResourceWorkspace:
    module_keys = frozenset({"process", "machine", "mask", "source", "substrate", "materials"})

    def __init__(self, host: ProjectSessionHost, binding: ProjectBinding):
        self.host, self.binding = host, binding
        self.store = cast(ProjectStore, host.store_for(binding))
        self.service = ProjectResourceService(self.store, session_host=host, binding=binding)
        self.controllers: dict[str, Any] = {}
        self.windows: dict[str, Any] = {}
        self._closing = False
        self._window_enabled: dict[str, bool] = {}
        self._initial_selection: dict[str, str] = {}
        self._pending_recovery: dict[str, dict] = {}
        self._recovery_error = ""
        self._default_initialized = False
        self._opening_graph: bytes | None = None
        host.claim_resource(binding, "resource_workspace", self)

    @classmethod
    def for_project(cls, host: ProjectSessionHost, binding: ProjectBinding) -> ProjectResourceWorkspace:
        with host.operation_scope(binding):
            existing = host.resource_for(binding, "resource_workspace")
            return existing if existing is not None else cls(host, binding)

    def initialize_default(self, plan: ProjectDefaultPlan) -> bool:
        """Atomically seed one detached opening graph; never replace active drafts."""
        with self.host.operation_scope(self.binding):
            if self._default_initialized:
                return False
            if plan.project_id != self.binding.project_id:
                raise ProjectError("dependency_conflict", "Opening plan belongs to another project")
            if self.controllers:
                return False
            manifest = self.store.manifest
            if plan.project_revision != manifest["revision"]:
                raise ProjectError("stale_project", "Project changed after its opening plan was captured")
            if plan.configuration_id != manifest["default_configuration_id"]:
                raise ProjectError("stale_project", "Default selection changed after its opening plan was captured")
            graph = plan.graph
            if graph is not None and (graph.get("project_id") != plan.project_id or
                                      graph.get("revision") != plan.project_revision or
                                      graph.get("root_document_id") != plan.configuration_id):
                raise ProjectError("dependency_conflict", "Opening graph differs from its default identity")
            selections = {}
            if graph is not None:
                # Normalize before changing any workspace-owned selection.
                for row in graph["documents"]:
                    normalize_document(row["kind"], row["data"])
                by_id = {row["id"]: row for row in graph["documents"]}
                def select(kind, identity):
                    row = by_id.get(identity)
                    if row is None or row["kind"] != kind:
                        raise ProjectError("dependency_conflict", "Opening graph has an invalid " + kind + " selection")
                    selections[kind] = identity
                    return row["data"]
                process = select("process", graph["configuration"]["process_document_id"])
                machine = select("machine", Path(process["machine_file"]).stem)
                select("substrate", Path(process["substrate_file"]).stem)
                # Graph rows are UUID-sorted. The visible editor starts at the
                # first machine slot, not an arbitrary dependency UUID.
                for kind, field in (("source", "sources"), ("mask", "masks")):
                    if machine[field]:
                        select(kind, Path(machine[field][0]["file"]).stem)
            captured = None if graph is None else json_bytes(graph)
            self._initial_selection = selections
            self._opening_graph = captured
            self._default_initialized = True
            return True

    @property
    def opening_graph(self) -> dict | None:
        return None if self._opening_graph is None else json.loads(self._opening_graph)

    @property
    def name(self) -> str:
        return str(self.store.manifest["name"])

    def initial_document(self, kind: str) -> str | None:
        return self._initial_selection.get(kind)

    def recovery_record(self, kind: str) -> dict | None:
        return deepcopy(self._pending_recovery.get(kind))

    def _stage_recovery(self, snapshot: dict) -> dict[str, dict]:
        """Validate a detached candidate without changing any owner or disk state."""
        with self.host.operation_scope(self.binding):
            if (not isinstance(snapshot, dict) or set(snapshot) != {
                    "kind", "version", "project_id", "opening_graph", "drafts", "dirty"} or
                    snapshot.get("kind") != "coat_sim.project_resource_recovery" or
                    type(snapshot.get("version")) is not int or snapshot["version"] != 1):
                raise ProjectError("invalid_document", "Invalid resource recovery format")
            if snapshot.get("project_id") != self.binding.project_id:
                raise ProjectError("dependency_conflict", "Recovery belongs to another project")
            if len(json_bytes(snapshot)) > MAX_FILE_BYTES:
                raise ProjectError("capacity_exceeded", "Resource recovery exceeds the JSON capacity limit")
            drafts = snapshot.get("drafts")
            if not isinstance(drafts, dict) or set(drafts) - self.module_keys:
                raise ProjectError("invalid_document", "Invalid resource recovery modules")
            if any(controller.dirty for controller in self.controllers.values()):
                raise ProjectError("dirty_project", "Recovery cannot replace existing project edits")
            staged = {}
            for kind, record in drafts.items():
                if (not isinstance(record, dict) or not {
                        "module_key", "document_id", "revision", "dirty", "data", "editor_state"}.issubset(record) or
                        set(record) - {"module_key", "document_id", "revision", "dirty", "data", "editor_state", "nested_material", "pending_ui", "pending_information"} or
                        record.get("module_key") != kind or type(record.get("dirty")) is not bool):
                    raise ProjectError("invalid_document", "Invalid resource recovery record")
                pending = record.get("pending_ui")
                if pending is not None and (not isinstance(pending, dict) or
                        set(pending) != {"object_name", "text", "numeric_value_edited"} or
                        not isinstance(pending["object_name"], str) or not 0 < len(pending["object_name"]) <= 160 or
                        not isinstance(pending["text"], str) or type(pending["numeric_value_edited"]) is not bool or
                        not record["dirty"] or record["data"] is not None):
                    raise ProjectError("invalid_document", "Invalid pending resource text recovery")
                identity = record.get("document_id")
                revision = record.get("revision")
                if identity is not None:
                    row = self.service._record(identity)
                    expected_kind = "material_profile" if kind == "materials" else kind
                    if row["kind"] != expected_kind or row["sha256"] != revision:
                        raise ProjectError("stale_document", "Saved resource differs from the approved recovery")
                elif revision is not None:
                    raise ProjectError("invalid_document", "Unsaved recovery has a persisted revision")
                data = record.get("data")
                if data is not None:
                    expected_kind = "material_profile" if kind == "materials" else kind
                    # Unsaved/invalid editor text is stored separately from saved-format data.
                    normalize_document(expected_kind, data)
                state = record.get("editor_state")
                if not isinstance(state, dict) or set(state) != set(EDITOR_STATE_FIELDS[kind]):
                    raise ProjectError("invalid_document", "Invalid resource editor state fields")
                if pending is not None and pending["object_name"] not in pending_document_fields(kind, state):
                    raise ProjectError("invalid_document", "Unknown pending resource document field")
                for key in ("_fields", "_emission", "_draft"):
                    if key in state and (not isinstance(state[key], dict) or any(
                            not isinstance(name, str) or not isinstance(value, str) for name, value in state[key].items())):
                        raise ProjectError("invalid_document", "Recovery editor text fields must be string maps")
                required_fields = {
                    "source": {"description", "notes", "device_type", "kind", "source_exponent", "sampling_order", "length_mm",
                               "angle_deg", "radius_mm", "width_mm", "inner_radius_mm", "outer_radius_mm"},
                    "substrate": {"description", "notes", "name", "outline_kind", "surface_kind", "radius_mm", "width_mm",
                                  "height_mm", "roc_mm", "conic_k", "a4", "a6", "a8"},
                }
                if kind in required_fields:
                    fields = set(state["_fields"])
                    optional = {"name"} if kind == "source" else set()
                    if (not required_fields[kind].issubset(fields)
                            or fields - required_fields[kind] - optional):
                        raise ProjectError("invalid_document", "Recovery editor fields differ from their schema")
                information = record.get("pending_information")
                if information is not None:
                    name_limit = 100 if kind == "process" else 200 if kind == "materials" else 120
                    if (not isinstance(information, dict) or set(information) != {
                            "name", "summary", "notes", "document_id", "token", "model_dirty"}
                            or information["document_id"] != identity
                            or type(information["model_dirty"]) is not bool or not record["dirty"]
                            or any(not isinstance(information[key], str) or len(information[key]) > limit
                                   for key, limit in (("name", name_limit), ("summary", 20000), ("notes", 20000)))):
                        raise ProjectError("invalid_document", "Invalid unconfirmed information recovery")
                    source = state.get("_draft", state.get("_fields", state.get("_data", {})))
                    if not isinstance(source, dict):
                        raise ProjectError("invalid_document", "Invalid information metadata state")
                    metadata = {"name": str(source.get("name", "")),
                                "summary": str(source.get("description", "")),
                                "notes": str(source.get("notes", ""))}
                    token = hashlib.sha256(json.dumps([identity or "", metadata],
                        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                    if information["token"] != token:
                        raise ProjectError("stale_document", "Unconfirmed information belongs to different resource metadata")
                if kind == "materials":
                    from .materials import RESISTANCE_DEFAULTS, ELECTRON_BEAM_DEFAULTS
                    expected_fields = {"id", "name", "description", "notes"}
                    for prefix, defaults in (("resistance", RESISTANCE_DEFAULTS), ("electron_beam", ELECTRON_BEAM_DEFAULTS)):
                        expected_fields.update(prefix + "_" + key for key in defaults)
                        expected_fields.update(prefix + "_provenance_" + key for key in ("status", "reference", "notes"))
                    if set(state["_draft"]) != expected_fields:
                        raise ProjectError("invalid_document", "Recovery material fields differ from their schema")
                    if identity is not None:
                        original = self.service.load_document(identity).data["material"]
                        if state["_saved_id"] != original["id"] or state["_draft"]["id"] != original["id"]:
                            raise ProjectError("dependency_conflict", "Recovery material identity differs from its saved document")
                for key in ("_data", "_extra_profile_data", "_selected_revisions"):
                    if key in state and not isinstance(state[key], dict):
                        raise ProjectError("invalid_document", "Invalid recovery mapping")
                for key in ("_optical_sets", "_points", "_apertures", "_null_models"):
                    if key in state and not isinstance(state[key], list):
                        raise ProjectError("invalid_document", "Invalid recovery editor list")
                for key in ("_closed", "_emission_present", "_optical_only"):
                    if key in state and type(state[key]) is not bool:
                        raise ProjectError("invalid_document", "Invalid recovery editor flag")
                if "_material" in state and state["_material"] is not None:
                    from .materials import validate_material_profile
                    validate_material_profile(state["_material"])
                if kind == "materials":
                    from .optical_constants import validate_optical_set
                    for optical in state["_optical_sets"]:
                        validate_optical_set(optical)
                    if state["_roles"] is not None and (not isinstance(state["_roles"], list) or any(
                            not isinstance(role, str) for role in state["_roles"])):
                        raise ProjectError("invalid_document", "Invalid recovery material roles")
                    for document_id, expected in state["_selected_revisions"].items():
                        if self.service._record(document_id)["sha256"] != expected:
                            raise ProjectError("stale_document", "Project material differs from recovery")
                nested = record.get("nested_material")
                if nested is not None:
                    if kind not in {"source", "substrate"}:
                        raise ProjectError("invalid_document", "Invalid nested material recovery owner")
                    candidate = {"kind": "coat_sim.project_resource_recovery", "version": 1,
                                 "project_id": self.binding.project_id, "opening_graph": None,
                                 "drafts": {"materials": nested}, "dirty": []}
                    self._stage_recovery(candidate)
                json_bytes(record)  # bounded finite JSON; no live QObject state
                staged[kind] = deepcopy(record)
            return staged

    def validate_recovery(self, snapshot: dict) -> None:
        self._stage_recovery(snapshot)

    def restore_recovery(self, snapshot: dict) -> bool:
        """Activate an explicitly approved, revalidated detached recovery."""
        with self.host.operation_scope(self.binding):
            if self.service._gate.frozen:
                raise ProjectError("project_frozen", "Resource recovery is paused while project close is prepared")
            staged = self._stage_recovery(snapshot)
            previous = {kind: controller.capture_recovery_state() for kind, controller in self.controllers.items()}
            applied = []
            try:
                for kind, controller in self.controllers.items():
                    if kind in staged:
                        controller.restore_recovery_snapshot(staged[kind])
                        applied.append(kind)
            except Exception:
                for kind in applied:
                    self.controllers[kind].rollback_recovery_state(previous[kind])
                raise
            self._pending_recovery = staged
            return True

    def freeze_editing(self, frozen: bool) -> None:
        self.service._gate.frozen = bool(frozen)
        for controller in self.controllers.values():
            if hasattr(controller, "set_editing_frozen"):
                controller.set_editing_frozen(bool(frozen))
        for key, window in self.windows.items():
            if frozen:
                self._window_enabled.setdefault(key, bool(window.property("enabled")))
                window.setProperty("enabled", False)
            elif key in self._window_enabled:
                window.setProperty("enabled", self._window_enabled.pop(key))

    def close_preflight(self) -> tuple[dict, ...]:
        # Closing must also offer Discard for invalid, uncommitted text. Settle
        # valid native focus-out text, but never require save/schema validation.
        for controller in self.controllers.values():
            controller.prepare_close_review()
        return self.dirty_summary()

    def resume_close_review(self) -> None:
        for controller in self.controllers.values():
            controller.resume_close_review()

    def dirty_summary(self) -> tuple[dict, ...]:
        owners = list(self.controllers.items())
        owners.extend((kind, _RecoveredResourceDraft(self, kind, record))
                      for kind, record in self._pending_recovery.items() if kind not in self.controllers)
        return tuple({"module_key": kind, "name": controller.display_name,
                      "dirty": True, "project_id": self.binding.project_id}
                     for kind, controller in owners if controller.dirty)

    def snapshot(self) -> dict:
        return {"kind": "coat_sim.project_resource_recovery", "version": 1,
                "project_id": self.binding.project_id,
                "opening_graph": self.opening_graph,
                "drafts": deepcopy({**self._pending_recovery,
                                    **{kind: controller.snapshot() for kind, controller in self.controllers.items()}}),
                "dirty": list(self.dirty_summary())}

    def snapshot_pending_edits(self) -> dict:
        """Journal text buffers read-only; never clear focus or normalize expressions."""
        return self.snapshot()

    def save_all(self) -> bool:
        """Save all retained resource/material drafts in one validated generation."""
        for controller in self.controllers.values():
            if controller.commit_pending_edits() is False:
                raise ProjectError("uncommitted_edit", "A resource field could not commit its current text")
        candidates = [controller for controller in self.controllers.values() if controller.dirty]
        candidates.extend(_RecoveredResourceDraft(self, kind, record)
                          for kind, record in self._pending_recovery.items()
                          if kind not in self.controllers and _RecoveredResourceDraft(self, kind, record).dirty)
        if not candidates:
            return True
        touches_materials = any(
            getattr(candidate, "module_key", getattr(candidate, "kind", None)) == "materials"
            or bool(getattr(getattr(getattr(candidate, "bridge", None),
                                    "_material_library", None), "dirty", False))
            or bool(getattr(candidate, "record", {}).get("nested_material", {}).get("dirty"))
            for candidate in candidates)
        try:
            new: list[tuple[str, dict]] = []
            updates: dict[str, dict] = {}
            revisions: dict[str, str] = {}
            material_creations: dict[tuple[str, str], dict] = {}
            for controller in candidates:
                pending, changed, expected = controller.prepare_save_operations()
                for kind, document in pending:
                    if kind in {"material_profile", "optical_material"}:
                        key = (kind, document["material"]["id"].casefold())
                        if key in material_creations:
                            if material_creations[key] != document:
                                raise ProjectError("dependency_conflict", "Retained material drafts disagree")
                            continue
                        material_creations[key] = document
                    new.append((kind, document))
                for identity, document in changed.items():
                    if identity in updates and updates[identity] != document:
                        raise ProjectError("dependency_conflict", "Retained resource drafts disagree")
                    updates[identity] = document
                for identity, revision in expected.items():
                    if identity in revisions and revisions[identity] != revision:
                        raise ProjectError("stale_document", "Retained draft revisions disagree")
                    revisions[identity] = revision
            with self.service.coordinated_save():
                created = self.store.write_documents(new, updates=updates, expected_revisions=revisions)
                typed_created = [(kind, identity) for (kind, _document), identity in zip(new, created, strict=True)]
                for controller in candidates:
                    controller.adopt_saved_transaction(typed_created)
            if touches_materials:
                try:
                    self.service.refresh_bound_film_materials()
                except Exception as exc:
                    self._recovery_error = "项目已保存，但膜系材料选择器刷新失败：" + str(exc)
            return True
        except (ValueError, TypeError, OSError, KeyError) as exc:
            for controller in candidates:
                controller._set_status("项目批量保存失败，所有未保存修改已保留：" + str(exc))
            return False

    def prepare_close(self, decision: str = "cancel") -> bool:
        if not self.close_preflight():
            return True
        if decision == "save":
            return self.save_all()
        return decision == "discard"

    def dispose(self) -> None:
        for controller in self.controllers.values():
            controller.dispose()
        self.service.close_resource_exchange()
        self.controllers.clear()
        self.windows.clear()
        self._pending_recovery.clear()
