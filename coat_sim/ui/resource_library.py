"""Shared asynchronous resource browser, bound to an explicit library scope."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Property, QTimer, Qt, Signal, Slot

from coat_sim.core.global_resource_library import GlobalResourceLibrary
from .asset_library_actions import AssetLibraryActions


class ResourceLibraryBridge(AssetLibraryActions):
    """Scope-bound browser with the existing material editor as its draft owner."""

    libraryChanged = Signal()
    materialSelectionChanged = Signal(str)
    _contentCommitted = Signal()

    def __init__(self, parent=None, *, service=None, library_scope="global", library_path="global"):
        super().__init__(parent)
        self.service = service if service is not None else GlobalResourceLibrary()
        self._library_scope = library_scope
        self._library_path = library_path
        self.transferChanged.connect(self.libraryChanged)
        self.assetChanged.connect(self.libraryChanged)
        self._message = ""
        self._failed = False
        self._disposed = False
        self.window: Any = None
        self._material_bridge: Any = None
        self._material_controller: Any = None
        self._refractive_index_browser: Any = None
        self._material_editor_active = False
        self._material_pending_id = ""
        self._material_section_pending = False
        self._catalog_refresh_pending = False
        self._refresh_previous_catalogs: dict[int, str] = {}
        self._catalog_refresh_timer = QTimer(self)
        self._catalog_refresh_timer.setInterval(10)
        self._catalog_refresh_timer.timeout.connect(self._refresh_committed_catalog)
        self._contentCommitted.connect(self._queue_catalog_refresh, Qt.ConnectionType.QueuedConnection)
        self._unsubscribe_store = None
        if library_scope == "project":
            self._unsubscribe_store = self.service.store.observe_changes(self._store_content_committed)
        QTimer.singleShot(0, self.browseLibrary)

    @Property(bool, notify=libraryChanged)
    def dirty(self):
        if self._material_controller is not None:
            return self._material_controller.dirty
        information = getattr(self._material_bridge, "_information_controller", None)
        return bool(self._material_bridge is not None and self._material_bridge.dirty
                    or getattr(information, "_pending_information_edit", None))

    @Property(bool, notify=libraryChanged)
    def materialModalPending(self):
        editor = self.window.property("editorInstance") if self.window is not None else None
        if editor is None:
            return False
        return any(item is not None and item.property("visible")
                   for name in ("setEditorInstance", "referenceDialogInstance")
                   for item in (editor.property(name),))

    @Slot(result=bool)
    def saveMaterialForClose(self):
        if self._material_bridge is None:
            return True
        information = getattr(self._material_bridge, "_information_controller", None)
        if getattr(information, "_pending_information_edit", None):
            self._message = "资源信息仍在编辑，请确认更新或取消编辑；也可明确放弃并关闭。"
            self.libraryChanged.emit()
            return False
        return bool(self._material_bridge.save())

    @Property(bool, notify=libraryChanged)
    def assetApplying(self):
        return bool(self._deletion_applying or self._transfer_applying or (self._material_controller is not None
                    and self._material_controller._transfer_applying)
                    or (self._material_bridge is not None and self._material_bridge.operationBusy)
                    or (self._refractive_index_browser is not None and self._refractive_index_browser.applying))

    @Property(bool, notify=libraryChanged)
    def assetCancellable(self):
        return not self.assetApplying

    @Property(QObject, notify=libraryChanged)
    def materialLibrary(self):
        return self._material_bridge

    @Property(QObject, notify=libraryChanged)
    def materialResources(self):
        return self._material_controller

    @Property(QObject, notify=libraryChanged)
    def refractiveIndexBrowser(self):
        return self._refractive_index_browser

    @Slot(result=bool)
    def openRefractiveIndexBrowser(self):
        if self._disposed or self.assetApplying or self._deletion_busy or self._transfer_busy:
            return False
        if self._refractive_index_browser is None:
            from .refractiveindex_browser import RefractiveIndexBrowserBridge
            try:
                self._refractive_index_browser = RefractiveIndexBrowserBridge(self)
            except (ValueError, OSError) as exc:
                self._message = "本地折射率数据库配置无效：" + str(exc)
                self.libraryChanged.emit()
                return False
            self._refractive_index_browser.changed.connect(self.libraryChanged)
            self.libraryChanged.emit()
        return bool(self._refractive_index_browser.open())

    def refractiveIndexImportBlocker(self):
        """Admit one import without treating its own read-only popup as an edit."""
        if (not self._current_catalog_owner() or self.assetApplying or self._transfer_busy
                or self._deletion_busy or self._catalog_refresh_pending):
            return "资产库仍在处理操作或已关闭，请等待后重新预览。"
        if self._library_scope == "global":
            from coat_sim.core.data_files import data_path
            if self.service.root != data_path("data"):
                return "全局材料库位置已变化，请重新打开资产库。"
        if self.dirty or (self._material_bridge is not None and self._material_bridge.loading):
            return "材料仍有未保存修改或正在加载，请先保存或明确放弃修改。"
        resources = getattr(self, "resource_workspace", None)
        if resources is not None and any(controller.dirty for controller in tuple(resources.controllers.values())):
            return "当前工程仍有未保存的资源输入，请先处理后导入。"
        if self.window is not None:
            if self.window.property("projectPendingTextEdited"):
                return "当前工程仍有待确认输入，请先完成或取消编辑。"
            allowed = {"refractiveIndexBrowserDialog", "materialReferenceDialog",
                       "materialReferenceDetailsDialog", "projectModuleMenu"}
            for dialog in self.window.findChildren(QObject):
                if (dialog.property("visible") is True and dialog.property("modal") is True
                        and dialog.objectName() not in allowed):
                    return "请先完成或取消其他待确认对话框。"
        return ""

    @Property(bool, notify=libraryChanged)
    def materialEditorActive(self):
        return self._material_editor_active

    @Property(bool, notify=libraryChanged)
    def materialDirty(self):
        return bool(self._material_bridge is not None and self._material_bridge.dirty)

    @Slot(QObject, result=bool)
    def attachAssetWindow(self, window):
        if self.window is not None and self.window is not window:
            return False
        self.window = window
        return True

    def _ensure_material_editor(self):
        if self._material_bridge is None:
            from .source_materials import MaterialLibraryBridge
            self._material_bridge = MaterialLibraryBridge(self, self.service.root / "materials")
            self._material_bridge._initial_default = False
            self._material_bridge.changed.connect(self._material_changed)
            self._material_bridge.catalogCommitted.connect(self._queue_catalog_refresh, Qt.ConnectionType.QueuedConnection)
            self._material_bridge.informationController.changed.connect(self.libraryChanged)
            self.libraryChanged.emit()
        return True

    def _material_changed(self):
        library = self._material_bridge
        if library is None:
            return
        if self._material_pending_id and not library.loading:
            identity, self._material_pending_id = self._material_pending_id, ""
            if self._select_material_asset(identity):
                self._present_material_asset(identity)
            else:
                self._asset_action_error(
                    "未能打开所选材料：材料库已变化或该材料已被删除，请刷新后重试。")
        current = str(library.currentId or library._draft.get("id", ""))
        row = next((row for row in self._transfer_catalog.get("rows", [])
                    if row.get("material_id") == current and row["kind"] == "material_profile"), None)
        if row is None:
            row = next((row for row in self._transfer_catalog.get("rows", [])
                        if row.get("material_id") == current and row["kind"] == "optical_material"), None)
        if row is not None and self._material_editor_active:
            self.materialSelectionChanged.emit(row["id"])
        self.libraryChanged.emit()

    def _store_content_committed(self):
        # Core invokes this outside its writer lock, possibly from a worker.
        # Always marshal presentation work through a queued QObject signal.
        self._contentCommitted.emit()

    def _current_catalog_owner(self):
        if self._disposed:
            return False
        if self._library_scope != "project":
            return True
        context = getattr(self, "project_context", None)
        binding = getattr(self, "owner_binding", None)
        if context is None or binding is None:
            return False
        try:
            return (context.binding == binding
                    and context.host.store_for(binding) is self.service.store)
        except ValueError:
            return False

    @Property(bool, notify=libraryChanged)
    def transferBusy(self):
        # A scheduled scope refresh is already part of the asynchronous read,
        # not an idle gap in which a stale catalog can accept another action.
        return self._transfer_busy or self._catalog_refresh_pending

    @Property(bool, notify=libraryChanged)
    def catalogRefreshPending(self):
        return self._catalog_refresh_pending

    @Slot()
    def _queue_catalog_refresh(self):
        if not self._current_catalog_owner():
            return
        self._catalog_refresh_pending = True
        # A successful write invalidates a previously reviewed plan. Refreshing
        # never confirms or retries a destructive operation on the user's behalf.
        self.cancelAssetDeletion()
        if not self._transfer_busy:
            self._release_transfer_plan()
            self.transferChanged.emit()
        self._catalog_refresh_timer.start()
        self.libraryChanged.emit()

    @Slot()
    def _refresh_committed_catalog(self):
        if not self._current_catalog_owner():
            self._catalog_refresh_timer.stop()
            self._catalog_refresh_pending = False
            return
        if (self._transfer_busy or self._deletion_busy or self.assetApplying
                or (self._material_bridge is not None and self._material_bridge.loading)):
            return
        self._catalog_refresh_timer.stop()
        self._catalog_refresh_pending = False
        self._release_transfer_plan()
        # An explicit external-import selection wins over a queued local change.
        # Returning to the owned library always performs its own fresh read.
        if self._transfer_catalog and self._transfer_catalog.get("source_path") != self._library_path:
            self.transferChanged.emit()
            self.libraryChanged.emit()
            return
        self._failed = False
        self._transfer_mode, self._transfer_active = "export", True
        previous = self._transfer_catalog.get("catalog_id")
        if previous:
            self._refresh_previous_catalogs[self._transfer_generation + 1] = previous
        service, source = self.service, self._library_scope
        # Retain the currently rendered catalog while the replacement is read.
        # Clearing it would hide the embedded popup and close local input forms.
        self._start_transfer_task("inspect", lambda: service.inspect_resources(source))
        self.libraryChanged.emit()

    def _asset_action_admitted(self):
        if self._catalog_refresh_pending:
            return self._asset_action_error("资产目录正在更新，请等待后重新检查所选资产。")
        if self._failed:
            return self._asset_action_error("资产目录读取未完成，请先重试刷新，再检查所选资产。")
        return super()._asset_action_admitted()

    def _present_material_asset(self, identity):
        row = next((row for row in self._transfer_catalog.get("rows", []) if row["id"] == identity), None)
        if row is None:
            return
        if self.window is not None:
            self.window.setProperty("categoryKey", row["kind"])
            uses = row.get("material_uses", [])
            if uses and self.window.property("materialUse") not in uses:
                self.window.setProperty("materialUse", uses[0])
        self.materialSelectionChanged.emit(identity)

    def _select_material_asset(self, identity):
        row = next((row for row in self._transfer_catalog.get("rows", []) if row["id"] == identity), None)
        if row is None or row["kind"] not in {"material_profile", "optical_material"}:
            return False
        return bool(self._material_bridge.selectMaterial(str(row["material_id"])))

    @Slot(str, result=bool)
    def openMaterialAsset(self, identity):
        if (self._disposed or self._transfer_busy or self._deletion_busy or self.assetApplying
                or self._transfer_catalog.get("source_path") != self._library_path):
            return False
        row = next((row for row in self._transfer_catalog.get("rows", []) if row["id"] == identity), None)
        if row is None or row["kind"] not in {"material_profile", "optical_material"}:
            return False
        if not self._ensure_material_editor():
            return False
        self._material_editor_active = True
        if self._material_bridge.loading:
            self._material_pending_id = identity
            self.libraryChanged.emit()
            return True
        if self._select_material_asset(identity):
            self._present_material_asset(identity)
            self.libraryChanged.emit()
            return True
        # The editor's own cached catalog can predate an out-of-process write,
        # for example an assistant create_material, while the asset list was
        # already refreshed. Force one fresh read and resolve the click when it
        # lands instead of silently returning False.
        if not self._material_bridge.hasMaterial(row.get("material_id")):
            self._material_pending_id = identity
            self._material_bridge.reload()
            self.libraryChanged.emit()
            return True
        self.libraryChanged.emit()
        return self._asset_action_error(
            "未能打开所选材料：材料库已变化或该材料已被删除，请刷新后重试。")

    @Slot(result=bool)
    def openMaterialSection(self):
        if self.window is not None:
            self.window.setProperty("materialSectionRequested", True)
            self.window.setProperty("categoryKey", "material_profile")
        if self._disposed or self._deletion_busy or self.assetApplying or not self._ensure_material_editor():
            return False
        if self._material_bridge.dirty:
            self._material_section_pending = False
            self._material_editor_active = True
            self._material_changed()
            current = self._material_bridge.currentId or self._material_bridge._draft.get("id", "")
            row = next((row for row in self._transfer_catalog.get("rows", [])
                        if row["kind"] == "material_profile" and row.get("material_id") == current), None)
            if row is not None:
                self._present_material_asset(row["id"])
            return True
        self._material_section_pending = True
        if not self._transfer_busy and self._transfer_catalog.get("source_path") == self._library_path:
            self._open_pending_material_section()
        self.libraryChanged.emit()
        return True

    def _open_pending_material_section(self):
        if not self._material_section_pending:
            return
        rows = [row for row in self._transfer_catalog.get("rows", []) if row["kind"] == "material_profile"]
        if not rows:
            rows = [row for row in self._transfer_catalog.get("rows", []) if row["kind"] == "optical_material"]
        current = self._material_bridge.currentId
        use = self.window.property("materialUse") if self.window is not None else ""
        eligible = [row for row in rows if use in row.get("material_uses", [])] or rows
        row = next((row for row in rows if row.get("material_id") == current), eligible[0] if eligible else None)
        self._material_section_pending = False
        if row is not None:
            self.openMaterialAsset(row["id"])
        else:
            # Empty project libraries still provide the existing material editor
            # for explicit creation; no global catalog or file is substituted.
            self._material_editor_active = True
            self.libraryChanged.emit()

    @Slot(result=bool)
    def newMaterialAsset(self):
        if (self._disposed or self._transfer_busy or self._deletion_busy or self.assetApplying
                or not self._ensure_material_editor()):
            return False
        self._material_editor_active = True
        result = bool(self._material_bridge.newMaterial())
        self.libraryChanged.emit()
        return result

    @Property(str, constant=True)
    def libraryScope(self):
        return self._library_scope

    @Property(str, constant=True)
    def librarySourcePath(self):
        return self._library_path

    @Slot(result=bool)
    def browseLibrary(self):
        return self.browseSource(self._library_scope)

    @Property(str, notify=libraryChanged)
    def libraryMessage(self):
        return self._message

    @Property(bool, notify=libraryChanged)
    def libraryFailed(self):
        return self._failed

    def _transfer_failed(self, exc):
        self._failed = True
        return super()._transfer_failed(exc)

    def _on_transfer_result(self, generation, kind, result, error, elapsed):
        previous = self._refresh_previous_catalogs.pop(generation, None)
        if previous and (generation != self._transfer_generation or not error):
            self.service.release_resource_catalog(previous)
        if generation == self._transfer_generation:
            self._failed = bool(error)
        super()._on_transfer_result(generation, kind, result, error, elapsed)
        if (generation == self._transfer_generation and kind == "inspect" and not error
                and self._transfer_catalog.get("source_path") == self._library_path
                and not self._catalog_refresh_pending):
            self._open_pending_material_section()

    @Slot(str, result=bool)
    def browseSource(self, source):
        if self._disposed or self._transfer_busy or self._deletion_busy or self.assetApplying:
            return False
        self._failed = False
        self._catalog_refresh_pending = False
        self._catalog_refresh_timer.stop()
        self.cancelAssetDeletion()
        self.cancelTransfer()
        self._transfer_mode = "export" if source == self._library_scope else "import"
        self._transfer_active = True
        return self.inspectTransferSource(source)

    @Slot("QVariantList", str, result=bool)
    def previewLibraryTransfer(self, selected_ids, destination):
        if (self._disposed or self._transfer_busy or self._deletion_busy or self.assetApplying
                or self._catalog_refresh_pending or not self._transfer_catalog):
            return False
        try:
            self._release_transfer_plan()
            target = self._transfer_location(destination)
            own_source = self._transfer_catalog.get("source_path") == self._library_path
            if own_source:
                if target == "global" and self._library_scope == "project":
                    pass
                elif target in {"global", "project"} or Path(target).suffix.casefold() != ".cproject":
                    raise ValueError("请选择新的 .cproject 导出文件或允许的全局资源库。")
            elif target != self._library_scope:
                raise ValueError("导入目标必须是" + ("当前工程。" if self._library_scope == "project" else "全局资源库。"))
            ids = list(selected_ids)
            if not ids or any(not isinstance(value, str) for value in ids):
                raise ValueError("请至少选择一个资源。")
            service, catalog_id = self.service, self._transfer_catalog["catalog_id"]
            return self._start_transfer_task("preview", lambda: service.preview_resource_transfer(
                catalog_id, ids, target), catalog_id=catalog_id)
        except (ValueError, TypeError, OSError, KeyError) as exc:
            return self._transfer_failed(exc)

    def _transfer_admitted(self):
        return not (self._disposed or self._deletion_busy or self.assetApplying or self._catalog_refresh_pending)

    @Slot(str, result=bool)
    def beginTransfer(self, mode):
        return self._transfer_admitted() and super().beginTransfer(mode)

    @Slot(str, result=bool)
    def inspectTransferSource(self, source):
        return self._transfer_admitted() and super().inspectTransferSource(source)

    @Slot("QVariantList", str, result=bool)
    def previewTransfer(self, selected_ids, target):
        return self._transfer_admitted() and super().previewTransfer(selected_ids, target)

    @Slot("QVariantMap", result=bool)
    def applyTransfer(self, decisions):
        return self._transfer_admitted() and super().applyTransfer(decisions)

    @Slot()
    def cancelTransfer(self):
        if self._deletion_applying:
            return
        if not self._transfer_applying:
            for previous in self._refresh_previous_catalogs.values():
                self.service.release_resource_catalog(previous)
            self._refresh_previous_catalogs.clear()
        super().cancelTransfer()

    def _transfer_applied(self, result):
        self._message = "资源传输已完成。目标：" + str(result.get("target_path", ""))
        QTimer.singleShot(0, self.browseLibrary)

    @Slot()
    def dispose(self):
        if self._disposed:
            return
        if self.assetApplying:
            return  # The visible window rejects close while publication runs.
        self._disposed = True
        if self._refractive_index_browser is not None:
            self._refractive_index_browser.dispose()
        self._catalog_refresh_timer.stop()
        self._catalog_refresh_pending = False
        if self._unsubscribe_store is not None:
            self._unsubscribe_store()
            self._unsubscribe_store = None
        self.cancelAssetDeletion()
        self.cancelTransfer()
        close = getattr(self.service, "close", None)
        if callable(close):
            close()
        else:
            self.service.close_resource_exchange()
        if self._material_bridge is not None:
            self._material_bridge.cancelPendingLoad()
            self._material_bridge._cancel_background_work()

    def detach_observers(self):
        self.dispose()
