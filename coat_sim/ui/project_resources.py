"""Current-project resource browser using existing exchange and session fences."""
from __future__ import annotations

from PySide6.QtCore import Qt, Slot

from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
from .resource_library import ResourceLibraryBridge


class ProjectResourcesBridge(ResourceLibraryBridge):
    """Own only this view's exchange tokens, never a second project store."""

    def __init__(self, workspace, parent=None):
        resources = ProjectResourceWorkspace.for_project(workspace.host, workspace.binding)
        self.project_context = workspace
        self.resource_workspace = resources
        self.owner_binding = workspace.binding
        self.window = None
        super().__init__(parent, service=resources.service.fork_editor(),
                         library_scope="project", library_path=str(resources.store.project_file))

    def _ensure_material_editor(self):
        if self._material_controller is not None:
            return True
        from .project_resource_adapter import ProjectResourceController
        resources = self.resource_workspace
        controller = resources.controllers.get("materials")
        if controller is not None and controller.window is not None and controller.window is not self.window:
            controller.window.show()
            controller.window.raise_()
            controller.window.requestActivate()
            self._message = "已有材料编辑窗口保留当前输入。请先完成当前编辑，再在资产库中打开材料。"
            self.libraryChanged.emit()
            return False
        if controller is None:
            controller = ProjectResourceController(self.project_context, resources, "materials", self.parent(),
                                                   self.window.property("projectNavigation"),
                                                   material_global_library=_ProjectOnlyMaterialCatalog())
            resources.controllers["materials"] = controller
            # A project asset browser lists only project-owned material records.
        controller.window = self.window
        controller.project_navigation = self.window.property("projectNavigation")
        resources.windows["materials"] = self.window
        self._material_controller = controller
        self._material_bridge = controller.bridge
        self._material_bridge._initial_default = False
        self._material_bridge.changed.connect(self._material_changed)
        self._material_bridge.catalogCommitted.connect(self._queue_catalog_refresh, Qt.ConnectionType.QueuedConnection)
        controller.changed.connect(self.libraryChanged)
        self.libraryChanged.emit()
        return True

    def _select_material_asset(self, identity):
        row = next((row for row in self._transfer_catalog.get("rows", []) if row["id"] == identity), None)
        if row is None:
            return False
        if row["kind"] == "optical_material" and not any(
                item["kind"] == "material_profile" and item.get("material_id") == row.get("material_id")
                for item in self._transfer_catalog.get("rows", [])):
            from coat_sim.core.materials import default_material_profile
            material = self.service.load_document(identity).data["material"]
            profile = default_material_profile(material["name"], id=material["id"], optical_only=True)
            profile.update(notes=material.get("notes", ""), optical_constants=material["optical_sets"])
            uses = row.get("material_uses", [])
            if uses:
                profile["roles"] = list(uses)
            return bool(self._material_bridge.loadAssetOpticalProjection(profile, identity))
        return super()._select_material_asset(identity)

    @Slot()
    def dispose(self):
        if self.assetApplying:
            return
        controller = self._material_controller
        super().dispose()
        if controller is not None and controller.window is self.window:
            controller.detach_view()

    def _asset_library_changed(self):
        self._transfer_applied({})
        if self._material_bridge is not None:
            self._material_bridge.reload()

    def _transfer_applied(self, result):
        for controller in tuple(self.resource_workspace.controllers.values()):
            controller.changed.emit()
        self.service.refresh_bound_film_materials()
        editor = getattr(self.project_context, "_editor", None)
        if editor is not None:
            editor.refresh_saved_designs()
        super()._transfer_applied(result)


def capture_project_resources_snapshot(controller, window, _module):
    """Project the already-loaded project view; never scan or substitute a source."""
    if controller._disposed:
        return None, "window_closed"
    if controller._transfer_busy or controller.deletionBusy or controller.assetApplying or controller.catalogRefreshPending:
        return None, "busy"
    if controller.libraryFailed:
        return None, "catalog_refresh_failed"
    catalog = controller._transfer_catalog
    if not catalog:
        return None, "initializing"
    if (catalog.get("source_type") != "project"
            or catalog.get("source_path") != str(controller.service.store.project_file)):
        return None, "external_import_source"
    rows = [{key: row.get(key) for key in ("id", "kind", "name", "summary", "notes", "dependencies", "material_uses", "material_category", "material_categories")}
            for row in catalog.get("rows", [])]
    selected = window.property("selectedIds")
    if hasattr(selected, "toVariant"):
        selected = selected.toVariant()
    summary = {"scope": "loaded_saved_project_resources", "catalog_read_only": True,
               "material_editor": {"available": controller._material_controller is not None,
                   "dirty": bool(controller._material_bridge is not None and controller._material_bridge.dirty),
                   "logical_target": "materials", "direct_asset_target_write": False,
                   "source_asset_id": getattr(controller._material_bridge, "projectionAssetId", "")
                       if controller._material_bridge is not None else ""},
               "category": window.property("categoryKey"), "material_use": window.property("materialUse"),
               "selected_ids": selected,
               "resource_count": len(rows), "categories": catalog.get("categories", [])}
    return {"summary": summary, "collections": {"resources": rows},
            "signature": {"catalog_id": catalog.get("catalog_id"), "rows": rows}}, None


class _ProjectOnlyMaterialCatalog:
    def catalog_records(self):
        return []

    def read(self, identity):
        raise ValueError("Only current-project materials are visible in this asset library: " + str(identity))
