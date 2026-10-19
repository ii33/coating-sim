"""Material editor UI over a read-only global catalog and project COW profiles."""
from copy import deepcopy
from PySide6.QtCore import Property, Signal, Slot
from coat_sim.core.project_material_library import ProjectMaterialLibrary
from .source_materials import MaterialLibraryBridge, _entry_rows, _prepare_material


class ProjectMaterialLibraryBridge(MaterialLibraryBridge):
    # PySide's metaclass can crash when a Python subclass property's notify
    # references an inherited signal. Keep a subclass-owned notifier while
    # forwarding all base-draft changes into it.
    selectionChanged = Signal()

    def __init__(self, service, parent=None, purpose="film", *, global_library=None):
        self.project_service = service.fork_editor()
        self.project_library = ProjectMaterialLibrary(
            self.project_service, global_library=global_library)
        self._selected_revisions = {}
        self._project_material_ids = set()
        self._global_material_ids = set()
        self._global_material_keys = {}
        self._global_selected_id = None
        self._editing_frozen = False
        self._asset_projection_document_id = ""
        self._asset_projection_material_id = ""
        super().__init__(parent=parent, directory=service.root, purpose=purpose)
        self.changed.connect(self.selectionChanged)

    def reload(self):
        try:
            catalog = self.project_library.catalog_records()
            global_error = self.project_library.global_catalog_error
            origins = {row["record"]["id"]: row for row in catalog}
            self._project_material_ids = {identity for identity, item in origins.items()
                                          if item["origin"] == "project"}
            self._global_material_ids = {identity for identity, item in origins.items()
                                         if item["origin"] == "global"}
            self._builtins = [item["record"] for item in catalog
                             if item["origin"] == "global" and item["builtin"]]
            self._entries = [item["record"] for item in catalog
                             if item["origin"] == "project" or
                             (item["origin"] == "global" and not item["builtin"])]
            self._overridden_ids = set()
            self._entry_cache = _entry_rows(self._builtins, self._entries)
            self._global_material_keys = {}
            for row in self._entry_cache:
                item = origins[row["id"]]
                row["origin"] = item["origin"]
                row["globalReadOnly"] = item["origin"] == "global"
                if item["origin"] == "global":
                    self._global_material_keys[row["id"]] = row["key"]
            if global_error:
                self._status = ("全局材料库读取失败；项目材料仍可用，保存编辑只写入当前项目："
                                + global_error)
            else:
                self._status = ("全局材料可读；保存编辑只写入当前项目。" if catalog else
                                "全局与项目材料库均为空；请新建或明确导入材料。")
        except (ValueError, TypeError, OSError) as exc:
            self._status = "全局/项目材料读取失败，当前内容已保留：" + str(exc)
        self.changed.emit()

    def _schedule_load(self, action, row, builtin=False):
        if self._editing_frozen:
            return False
        try:
            record, sections = _prepare_material(deepcopy(row))
            identity = record["id"]
            is_project = identity in self._project_material_ids
            is_global = identity in self._global_material_ids and not is_project
            self._builtin = self._builtin_overridden = False
            self._saved_id = identity if is_project and action not in {"copy", "snapshot"} else None
            self._global_selected_id = identity if is_global and action not in {"copy", "snapshot"} else None
            self._selected_key = ("user:" + identity if is_project else
                                  self._global_material_keys.get(identity, "") if is_global else "")
            self._selected_revisions = (self.project_library.revisions(identity)
                                        if action not in {"copy", "snapshot"} else {})
            self._load_draft(record, sections, prepared=True)
            self._dirty = action in {"copy", "snapshot"}
            self._status = ("已载入全局材料来源；编辑后保存只创建当前项目副本，原件不变。"
                            if is_global else "已载入项目材料；已有源和基片快照保持不变。")
            if action in {"source_select", "substrate_select"}:
                self.materialSelected.emit(deepcopy(record))
            self.changed.emit()
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._status = "项目材料载入失败：" + str(exc)
            self.changed.emit()
            return False

    def _replace(self, action, value=None):
        if self._editing_frozen:
            return False
        if action == "asset_projection":
            if self._dirty:
                self._pending = (action, value)
                self.discardRequested.emit()
                return False
            profile, document_id = value
            self._asset_projection_document_id = document_id
            self._asset_projection_material_id = profile["id"]
            return self._schedule_load("select", profile)
        # Let the base owner establish its dirty-draft confirmation first. A
        # cancelled/new request must not lose the selected global export ID.
        result = super()._replace(action, value)
        if result and action == "new":
            self._global_selected_id = None
            self._selected_revisions = {}
            self.changed.emit()
        return result

    def _refresh_bound_film_picker(self):
        """Refresh a film editor only when this binding already owns one."""
        try:
            return self.project_service.refresh_bound_film_materials()
        except Exception as exc:
            # The material transaction is already committed. Keep its success
            # result and report the separate picker-refresh failure accurately.
            self._status += " 已保存，但项目膜系材料选择器刷新失败：" + str(exc)
            return False

    def loadAssetOpticalProjection(self, profile, document_id):
        if (self.projectionAssetId == document_id
                and self._draft.get("id") == profile["id"]):
            return True
        return self._replace("asset_projection", (profile, document_id))

    def restoreAssetOpticalProjectionOrigin(self):
        self._asset_projection_document_id = self._asset_projection_material_id = ""
        if self._saved_id is not None:
            return
        material_id = self._draft.get("id")
        for identity, revision in self._selected_revisions.items():
            try:
                row = self.project_service._record(identity)
                if row["kind"] != "optical_material" or row["sha256"] != revision:
                    continue
                material = self.project_service.load_document(identity).data["material"]
                if material["id"] == material_id:
                    self._asset_projection_document_id = identity
                    self._asset_projection_material_id = material_id
                    return
            except (ValueError, TypeError, OSError):
                continue

    @Property(str, notify=selectionChanged)
    def projectionAssetId(self):
        if self._saved_id is None and self._draft.get("id") == self._asset_projection_material_id:
            return self._asset_projection_document_id
        return ""

    @Property(bool, notify=selectionChanged)
    def globalSourceSelected(self):
        return self._global_selected_id is not None

    @Property(str, notify=selectionChanged)
    def currentId(self):
        projected = self._asset_projection_material_id if self.projectionAssetId else ""
        return self._global_selected_id or self._saved_id or projected

    @Property(bool, notify=selectionChanged)
    def canDelete(self):
        return self._saved_id in self._project_material_ids and not self.loading

    @Slot(result=bool)
    def restoreBuiltinInBackground(self):
        # Project edits never create, restore, or write global builtin overrides.
        return False

    @Slot(str, result=bool)
    def restoreDeletedBuiltin(self, identifier):
        # Restoring a globally deleted builtin is a global-library action; a
        # project view must not resurrect or edit global builtins.
        return False

    def _scope_rows(self):
        """Only project-owned records share a project draft's writable scope.

        Global read-only sources keep their own names; saving one as a project
        copy must not be rejected as a cross-scope duplicate.
        """
        return [row for row in self._entries
                if str(row.get("id", "")) in self._project_material_ids]

    @Slot(str, str)
    def setField(self, key, value):
        if not self._editing_frozen:
            super().setField(key, value)

    @Slot(str)
    def setCategory(self, value):
        if not self._editing_frozen:
            return super().setCategory(value)

    @Slot(str, str, str, str, result=bool)
    def importOpticalFile(self, path, name, unit, notes):
        return False if self._editing_frozen else super().importOpticalFile(path, name, unit, notes)

    @Slot(str, str, str, float, float, str, str, result=bool)
    def addDispersionModel(self, name, kind, unit, low, high, parameters, notes):
        return False if self._editing_frozen else super().addDispersionModel(name, kind, unit, low, high, parameters, notes)

    @Slot(str, str, str, result=bool)
    def editOpticalMetadata(self, identity, name, notes):
        return False if self._editing_frozen else super().editOpticalMetadata(identity, name, notes)

    @Slot(str, result=bool)
    def addOpticalSet(self, form_json):
        return False if self._editing_frozen else super().addOpticalSet(form_json)

    @Slot(str, str, result=bool)
    def updateOpticalSet(self, identity, form_json):
        return False if self._editing_frozen else super().updateOpticalSet(identity, form_json)

    @Slot(str, result=bool)
    def removeOpticalSet(self, identity):
        return False if self._editing_frozen else super().removeOpticalSet(identity)

    def set_editing_frozen(self, frozen):
        self._editing_frozen = bool(frozen)

    @Slot(result=bool)
    def save(self):
        if self._editing_frozen or self.loading:
            return False
        try:
            draft = self.draftData()
            conflict = self._name_conflict_error(draft)
            if conflict:
                self._status = conflict
                self.changed.emit()
                return False
            result = self.project_library.write(draft, update=self._saved_id is not None,
                                                expected_revisions=self._selected_revisions)
            record = result["record"]
            self._saved_id, self._selected_key = record["id"], "user:" + record["id"]
            self._global_selected_id = None
            self._selected_revisions = self.project_library.revisions(record["id"])
            self._load_draft(record, self._sections)
            self._dirty = False
            self.reload()
            self._status = "已保存项目材料与光学资源；已有源和基片快照保持不变。"
            self._refresh_bound_film_picker()
            self.catalogCommitted.emit()
            self.changed.emit()
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._status = "项目材料保存失败，未保存的修改已保留：" + str(exc)
            self.changed.emit()
            return False

    def _begin_save(self, discard_pending=False):
        if not self.save():
            return False
        if discard_pending:
            self.confirmDiscard(True)
        return True

    @Slot(str, str, result=bool)
    def exportMaterial(self, path, identifier):
        return self._run_project_operation("export_single", lambda: self.project_library.export_single_file(identifier, path))

    @Slot(str, "QVariantList", result=bool)
    def exportLibrary(self, path, identifiers):
        return self._run_project_operation("export_library", lambda: self.project_library.export_library_file(path, list(identifiers)))

    @Slot(str, result=bool)
    def previewImport(self, path):
        try:
            result = self.project_library.preview_import(self.project_library.parse_package_file(path))
            self._import_preview_plan, self._import_preview_rows = result["plan"], result["rows"]
            self._import_error = ""
            self.importPreviewReady.emit()
            self.changed.emit()
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._import_error = str(exc)
            self.importPreviewReady.emit()
            self.changed.emit()
            return False

    @Slot("QVariantMap", result=bool)
    def applyImport(self, decisions):
        if self._editing_frozen or self._import_preview_plan is None:
            return False
        try:
            self.project_library.apply_import(self._import_preview_plan, dict(decisions))
            self._import_preview_plan, self._import_preview_rows, self._import_error = None, [], ""
            self.reload()
            self._status = "材料及光学资源已原子复制到当前项目。"
            self._refresh_bound_film_picker()
            self.importApplyFinished.emit(True)
            self.catalogCommitted.emit()
            self.changed.emit()
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._import_error = str(exc)
            self.importApplyFinished.emit(False)
            self.changed.emit()
            return False

    def _run_project_operation(self, kind, operation):
        try:
            result = operation()
            self._status = f"已导出：{result['path']}"
            self.changed.emit()
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._status = "项目材料操作失败：" + str(exc)
            self.changed.emit()
            return False

    @Slot(result=bool)
    def deleteInBackground(self):
        return self.confirmDelete(True)

    @Slot(bool, result=bool)
    def confirmDelete(self, confirmed):
        if not confirmed or self._editing_frozen or not self.canDelete:
            return False
        try:
            self.project_library.delete(self._saved_id)
            self._saved_id, self._selected_key = None, ""
            self._dirty = True
            self.reload()
            self._refresh_bound_film_picker()
            self.changed.emit()
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._status = "项目材料删除失败：" + str(exc)
            self.changed.emit()
            return False
