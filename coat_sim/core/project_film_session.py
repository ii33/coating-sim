"""Project-owned film draft and routing facade, with no Qt dependency."""

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from typing import Any, Mapping, cast

from coat_sim.project_modules import project_module_options

from coat_sim.project_session import ModuleReservation, ProjectBinding, ProjectSessionHost
from .film_design_session import FilmDesignEditSession
from .film_process_import import validate_frozen_design_snapshot
from .project_dependencies import ProjectError, document_material_references
from .project_film_service import ProjectFilmService, ProjectFilmStore, ProjectOpticalSnapshot


class ProjectFilmEditSession(FilmDesignEditSession):
    """Use document membership rather than standalone save-file dialogs."""

    service: ProjectFilmService

    def __init__(self, service: ProjectFilmService) -> None:
        self._pending_information_edit: dict[str, Any] | None = None
        self._frozen_draft_materials: dict[str, dict[str, Any]] | None = None
        self._retained_material_snapshots: dict[str, dict[str, Any]] = {}
        super().__init__(service)
        self._picker_catalog_source = self.service._project_materials

    def _default_design(self) -> dict[str, Any]:
        """Start project films unbound; catalog visibility is not a selection."""
        design = self.service.new_design()
        design["expression"]["symbols"] = {
            "H": self._default_symbol_record(), "L": self._default_symbol_record(),
        }
        return design

    def _capture_design_material_snapshots(self, design: Mapping[str, Any], *, preserve: bool) -> None:
        """Pin available values without rejecting an incomplete edit buffer.

        Material and optical-set selectors are separate controls, so an
        intermediate dirty draft may temporarily contain only one half of a
        reference. Strict dependency validation remains at recovery/save time.
        """
        previous = self._retained_material_snapshots if preserve else {}
        captured: dict[str, dict[str, Any]] = {}
        rows = [design.get("substrate", {})]
        rows.extend(design.get("front_layers", []))
        rows.extend(design.get("back_layers", []))
        expression = design.get("expression", {})
        rows.extend(expression.get("symbols", {}).values())
        rows.extend(design.get(key, {}) for key in ("incident_medium", "exit_medium")
                    if design.get(key, {}).get("kind") == "material")
        optimization = design.get("workflow_settings", {}).get("optimization", {})
        rows.extend(optimization.get("structure_materials", []))
        identities = {str(row.get("material_id", "")) for row in rows
                      if isinstance(row, Mapping) and row.get("material_id")}
        for material_id in identities:
            if material_id in captured:
                continue
            value = previous.get(material_id)
            if value is None:
                try:
                    value = self.service.materials.get(material_id)
                except ProjectError:
                    continue
            captured[material_id] = deepcopy(value)
        self._retained_material_snapshots = captured

    def _activate_design_material_snapshots(self) -> None:
        overlay = deepcopy(self._frozen_draft_materials or {})
        overlay.update(deepcopy(self._retained_material_snapshots))
        if overlay:
            self.service.activate_frozen_draft_materials(overlay.values())
        else:
            self.service.clear_frozen_draft_materials()

    def _check_material_snapshot_current(self) -> None:
        """Do not save a design against a same-ID value changed in another view."""
        for material_id, _set_id in document_material_references("film", self._design):
            captured = self._retained_material_snapshots.get(material_id)
            if captured is None:
                continue
            try:
                current = self.service.catalog_material(material_id)
            except ProjectError as exc:
                # Process-default film provenance may carry an explicit
                # immutable material snapshot not yet present as a project
                # optical document. Its first managed save captures that
                # closure; a conflicting same-ID project value is still caught
                # below whenever the catalog has one.
                if self._frozen_draft_materials and material_id in self._frozen_draft_materials:
                    continue
                raise ProjectError(
                    "stale_document",
                    "A material used by this film is no longer available in its captured catalog snapshot: "
                    + material_id,
                ) from exc
            if current != captured:
                raise ProjectError(
                    "stale_document",
                    "A referenced material changed after this film draft captured it: "
                    + material_id + ". Reload the design before saving.",
                )

    def _material_record(self, purpose: str, material_id: str) -> dict[str, Any] | None:
        # Picker rows may refresh from a newer catalog while the current design
        # keeps using the value it already captured for its derived QW and math.
        retained = self._retained_material_snapshots.get(str(material_id or ""))
        return retained if retained is not None else super()._material_record(purpose, material_id)

    def _mark_dirty(self, *, invalidate_expression: bool = True) -> None:
        super()._mark_dirty(invalidate_expression=invalidate_expression)
        self._capture_design_material_snapshots(self._design, preserve=True)
        self._activate_design_material_snapshots()

    def initialize_frozen_draft(self, snapshot: Mapping[str, Any]) -> bool:
        """Adopt selected process provenance once as an unsaved working film.

        A blank session is the only implicit replacement target. Neither a
        saved film nor a draft with user edits may be silently replaced by a
        repeated project activation.
        """
        # Match save/import's editor -> host -> service lock order. An
        # initializer must not keep the host lock while waiting for an editor
        # that may itself be waiting to enter a bound persistence operation.
        with self._lock:
            host = self.service._session_host
            binding = self.service.binding
            scope = (host.operation_scope(binding)
                     if host is not None and binding is not None else nullcontext())
            with scope:
                return self._initialize_frozen_draft_locked(snapshot)

    def _initialize_frozen_draft_locked(self, snapshot: Mapping[str, Any]) -> bool:
        """Initialize only while the editor and its live host admission are held."""
        if (self._design_revision != 0 or self._path is not None or self._dirty
                or self._pending_confirmation is not None or self._expression_busy):
            return False
        previous = (self._design, self._derived, self._catalog_options,
                    self._material_options, self._design_revision, self._path,
                    self._dirty, self._message, self._generated_undo_state)
        activated = False
        try:
            frozen = validate_frozen_design_snapshot(snapshot)
            design = deepcopy(frozen["design"])
            configuration = deepcopy(design.get("workflow_settings", {}))
            configuration["simulation"] = deepcopy(frozen["settings"])
            design["workflow_settings"] = configuration
            design = self.service.validate(design)
            materials = {row["id"]: deepcopy(row) for row in frozen["materials"]}
            for material_id, set_id in document_material_references("film", design):
                material = materials.get(material_id)
                if material is None or set_id not in {
                        row["id"] for row in material["optical_sets"]}:
                    raise ProjectError("missing_dependency",
                                       "冻结膜系内容缺少材料或光学数据集："
                                       + material_id + "/" + set_id
                                       + "。请从原始膜系重新导出包含全部表达式符号及优化候选材料的完整快照。"
                                       "此快照仍可用于光谱计算或沉积工艺。")
            self.service.activate_frozen_draft_materials(materials.values())
            activated = True
            self._frozen_draft_materials = materials
            self._capture_design_material_snapshots(design, preserve=False)
            self._activate_design_material_snapshots()
            self._refresh_project_materials()
            super()._replace_design(design, path=None,
                                    message="已从项目默认配置载入冻结膜系内容；尚未保存。",
                                    dirty=True)
            return True
        except (ValueError, TypeError, OSError) as exc:
            if activated:
                self.service.clear_frozen_draft_materials()
                self._frozen_draft_materials = None
                (self._design, self._derived, self._catalog_options,
                 self._material_options, self._design_revision, self._path,
                 self._dirty, self._message, self._generated_undo_state) = previous
                self._prepared_optical_cache.clear()
                self._reference_index_cache.clear()
            self._set_error(str(exc))
            return False

    def _replace_design(self, design: dict[str, Any], *, path: str | None,
                        message: str, dirty: bool = False) -> None:
        # A confirmed in-place import derives an unsaved candidate from the
        # current film. It still owns the frozen substrate/symbol/film records
        # until its first managed save. Switching documents releases them.
        derived_draft = (path is None and dirty
                         and design.get("id") == self._design.get("id"))
        if not derived_draft:
            self.service.clear_frozen_draft_materials()
            self._frozen_draft_materials = None
            self._retained_material_snapshots = {}
            self._refresh_project_materials()
        super()._replace_design(design, path=path, message=message, dirty=dirty)
        self._capture_design_material_snapshots(self._design, preserve=derived_draft)
        self._activate_design_material_snapshots()

    def _generation_material_options(self) -> Mapping[str, Any]:
        # Generated thickness and later numerics must use the same pinned
        # values, even when the picker displays a newer same-ID catalog row.
        return self.service.material_options_many(("film", "substrate", "medium"))

    def _generation_source(self) -> Any:
        return self.service.materials

    def _capture_generated_context(self) -> Any:
        return {"retained": deepcopy(self._retained_material_snapshots),
                "frozen": deepcopy(self._frozen_draft_materials),
                "draft": self.service._draft_materials}

    def _prepare_generated_context(self, design: Mapping[str, Any]) -> Any:
        retained = {}
        for material_id, optical_set_id in document_material_references("film", dict(design)):
            if material_id not in retained:
                retained[material_id] = self.service.materials.get(material_id)
            if optical_set_id not in {row["id"] for row in retained[material_id]["optical_sets"]}:
                raise ProjectError("missing_dependency", "Generated film optical set is missing: " + optical_set_id)
        # A frozen default draft's first save supplies an explicit closure.
        # Replace that closure with the generated design's complete references,
        # including newly selected materials rather than omitting them.
        frozen = deepcopy(retained) if self._frozen_draft_materials is not None else None
        return {"retained": retained, "frozen": frozen,
                "draft": ProjectOpticalSnapshot(retained.values()) if retained else None}

    def _restore_generated_context(self, context: Any) -> None:
        if context is None:
            return
        with self.service._operation():
            draft = context["draft"]
            active = self.service._material_view(draft)
            self._retained_material_snapshots = context["retained"]
            self._frozen_draft_materials = context["frozen"]
            self.service._draft_materials = draft
            self.service.materials = active
            self._refresh_project_materials()

    def _prepare_generated_undo_context(self, design: Mapping[str, Any], context: Any) -> Any:
        # Undo preserves later page settings. Their material references must
        # therefore join the restored physical design's original snapshot,
        # using already-captured values before considering the live provider.
        retained = deepcopy(context["retained"])
        frozen = deepcopy(context["frozen"])
        for material_id, optical_set_id in document_material_references("film", dict(design)):
            value = retained.get(material_id)
            if value is None:
                value = self._retained_material_snapshots.get(material_id)
                if value is None:
                    value = self.service.materials.get(material_id)
                retained[material_id] = deepcopy(value)
            if optical_set_id not in {row["id"] for row in value["optical_sets"]}:
                raise ProjectError(
                    "missing_dependency", "Restored film optical set is missing: " + optical_set_id)
            if frozen is not None:
                frozen[material_id] = deepcopy(value)
        return {"retained": retained, "frozen": frozen,
                "draft": ProjectOpticalSnapshot(retained.values()) if retained else None}

    def apply_generated_design(self, candidate_design: Mapping[str, Any],
                               base_design_revision: int) -> bool:
        with self._lock:
            with self.service._operation():
                return super().apply_generated_design(candidate_design, base_design_revision)

    def undo_generated_design(self, expected_design_revision: int) -> bool:
        with self._lock:
            with self.service._operation():
                return super().undo_generated_design(expected_design_revision)

    def request_import_film(self, path: str, *,
                            materials: Mapping[str, Any] | None = None) -> bool:
        with self._lock:
            if self._pending_confirmation is not None or self._expression_busy:
                self._set_error("请先完成当前确认或表达式操作，再导入。")
                return False
            return self._request_action(
                "project_import_film", (str(path), deepcopy(materials)),
                "放弃当前未保存的修改，并将所选膜系及依赖复制到此项目？")

    def request_close(self) -> bool:
        with self._lock:
            if not self._dirty and self._pending_information_edit is None:
                return True
            self._pending_confirmation = {
                "kind": "discard_changes", "action": "close", "payload": None,
                "message": "关闭设计窗口，并在运行中的项目会话内保留当前内容？修改尚未写入磁盘，退出程序后不会保留。",
            }
            self._notify("confirmation", "ui")
            return False

    def _refresh_project_materials(self) -> None:
        options = self.service.picker_material_options_many(("film", "substrate", "medium"))
        self._catalog_options = options
        self._material_options = self._make_picker_options(options)
        self._prepared_optical_cache.clear()
        self._reference_index_cache.clear()
        self._picker_catalog_source = self.service._project_materials

    def refresh_project_materials(self) -> None:
        """Refresh one project's live editor while retaining captured draft numerics."""
        with self._lock:
            self._capture_design_material_snapshots(self._design, preserve=True)
            self.service.refresh_materials()
            self._activate_design_material_snapshots()
            self._refresh_project_materials()
            # Refreshing catalog rows is not a design edit: preserve dirty state
            # and revision, but make the live QML picker and derived display
            # observe the refreshed catalog immediately.
            self._refresh_derived()
            self._notify("derived", "ui")

    def _adopt_document(self, document_id: str, *, message: str) -> bool:
        handle = self.service.document_handle(document_id)
        design = self.service.load(handle)
        self._refresh_project_materials()
        self._replace_design(design, path=str(handle), message=message)
        return True

    def _request_action(self, action: str, payload: Any, message: str) -> bool:
        if self._pending_information_edit is not None:
            self._set_error("资源信息仍在编辑，请先确认更新或取消编辑。")
            return False
        return super()._request_action(action, payload, message)

    def _run_action(self, action: str, payload: Any) -> bool:
        if action not in {"load_saved", "project_import_film"}:
            if action == "new" and self._frozen_draft_materials is not None:
                self.service.clear_frozen_draft_materials()
                self._frozen_draft_materials = None
                self._retained_material_snapshots = {}
                self._refresh_project_materials()
            return super()._run_action(action, payload)
        try:
            if action == "load_saved":
                return self._adopt_document(str(payload), message="已加载项目膜系。")
            path, materials = payload
            imported = self.service.import_film(path, materials=materials)
            result = self._adopt_document(imported.document_id,
                                          message="膜系及依赖已复制到此项目。")
            self.refresh_saved_designs()
            return result
        except (ValueError, TypeError, OSError) as exc:
            self._set_error(str(exc))
            return False

    def load_saved_design(self, design_id: str) -> bool:
        with self._lock:
            return self._run_action("load_saved", design_id)

    def save_current(self) -> bool:
        with self._lock:
            if self._pending_information_edit is not None:
                self._set_error("资源信息仍在编辑，请先确认更新或取消编辑。")
                return False
            try:
                # The backing service may predate a write from another editor.
                # Refresh the latest project catalog before comparing captured
                # values, while the draft overlay keeps current numerics pinned.
                previous_materials = self.service.materials
                self.service.refresh_materials()
                catalog_changed = (self.service.materials is not previous_materials
                                   or self._picker_catalog_source is not self.service._project_materials)
                if catalog_changed:
                    self._activate_design_material_snapshots()
                    self._refresh_project_materials()
                self._check_material_snapshot_current()
                if catalog_changed:
                    self._refresh_derived()
                    self._notify("derived", "ui")
            except (ValueError, TypeError, OSError) as exc:
                self._set_error(str(exc))
                return False
            if self._path:
                result = super().save_current()
                if result and self._picker_catalog_source is not self.service._project_materials:
                    # Publication can capture a newly used global material or
                    # change its picker role. Keep the visible catalog current
                    # without rebuilding it after unrelated name/thickness saves.
                    self._refresh_project_materials()
                    self._notify("derived", "ui")
                return result
            try:
                saved = self.service.create_film(
                    self._design, materials=deepcopy(self._frozen_draft_materials))
                result = self._adopt_document(saved.document_id, message="已保存项目膜系。")
                self.refresh_saved_designs()
                return result
            except (ValueError, TypeError, OSError) as exc:
                self._set_error(str(exc))
                return False

    def save_copy_current(self) -> bool:
        """Create a managed copy of the complete current film/workflow draft."""
        with self._lock:
            if self._pending_information_edit is not None:
                self._set_error("资源信息仍在编辑，请先确认更新或取消编辑。")
                return False
            try:
                self.service.refresh_materials()
                self._activate_design_material_snapshots()
                self._check_material_snapshot_current()
                saved = self.service.create_film(
                    self._design, materials=deepcopy(self._frozen_draft_materials))
                result = self._adopt_document(saved.document_id, message="已保存项目膜系副本。")
                self.refresh_saved_designs()
                return result
            except (ValueError, TypeError, OSError) as exc:
                self._set_error(str(exc))
                return False

    def save_to_library(self, name: str) -> bool:
        with self._lock:
            # This UI action has the same single managed owner as ordinary Save.
            # It never finds/replaces another document by a non-unique design ID.
            if name and str(name) != self._design["name"]:
                if not self.set_design_value("name", str(name)):
                    return False
            return self.save_current()


class ProjectFilmWorkspace:
    """One explicit project binding and working film draft for lazy modules.

    A workspace is a caller-owned facade. Reuse it for a registered project;
    opening a second facade for the same binding is rejected by the host-owned
    workspace registration, preventing competing draft owners.
    """

    @property
    def module_options(self) -> list[dict[str, str]]:
        return project_module_options()

    def __init__(self, host: ProjectSessionHost, binding: ProjectBinding):
        self.host = host
        self.binding = binding
        store = host.store_for(binding)
        with host.operation_scope(binding):
            self.service = ProjectFilmService(cast(ProjectFilmStore, store),
                                              session_host=host, binding=binding)
            self._editor: ProjectFilmEditSession | None = None
            host.claim_resource(binding, "film_workspace", self)

    @classmethod
    def for_project(cls, host: ProjectSessionHost, binding: ProjectBinding) -> ProjectFilmWorkspace:
        with host.operation_scope(binding):
            existing = host.resource_for(binding, "film_workspace")
            if existing is not None:
                return existing
            return cls(host, binding)

    @property
    def name(self) -> str:
        return str(self.host.store_for(self.binding).manifest["name"])

    @property
    def editor(self) -> ProjectFilmEditSession:
        with self.host.operation_scope(self.binding):
            if self._editor is None:
                self._editor = ProjectFilmEditSession(self.service)
            return self._editor

    def initialize_frozen_draft(self, snapshot: Mapping[str, Any]) -> bool:
        """Initialize the pristine editor from the selected configuration."""
        # Lazy construction is host-protected, but release that scope before
        # the existing editor takes its lock and revalidates host admission.
        editor = self.editor
        return editor.initialize_frozen_draft(snapshot)

    def refresh_materials(self) -> bool:
        """Refresh an already-owned film picker for this binding only."""
        with self.host.operation_scope(self.binding):
            editor = self._editor
        if editor is None:
            self.service.refresh_materials()
        else:
            editor.refresh_project_materials()
        return True

    def reserve_module(self, module_key: str) -> ModuleReservation:
        if module_key not in {row["key"] for row in self.module_options}:
            raise ValueError("Unknown project module")
        with self.host.operation_scope(self.binding):
            return self.host.reserve_module(self.binding.project_id, module_key)
