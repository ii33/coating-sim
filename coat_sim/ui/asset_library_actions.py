"""Async, explicit, recoverable asset actions for one loaded library owner."""
from __future__ import annotations

from copy import deepcopy
from threading import Event
from typing import Any

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot
from shiboken6 import isValid

from .project_resource_adapter import ProjectResourceTransferController, _TRANSFER_EXECUTOR


class AssetLibraryActions(ProjectResourceTransferController):
    assetChanged = Signal()
    deletionPreviewReady = Signal()
    deletionHistoryReady = Signal()
    deletionFinished = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        # The scope-bound subclass installs these existing view/editor owners.
        self.window: Any = None
        self._material_bridge: Any = None
        self._material_controller: Any = None
        self._disposed = False
        self._library_path = ""
        self._library_scope = ""
        self._deletion_plan = {}
        self._deletion_records = []
        self._deletion_message = ""
        self._deletion_busy = False
        self._deletion_applying = False
        self._deletion_task = None
        self._asset_frozen_content = []
        self._asset_material_was_frozen = None
        self._deletion_poll = QTimer(self)
        self._deletion_poll.setInterval(10)
        self._deletion_poll.timeout.connect(self._poll_asset_action)

    @Property(dict, notify=assetChanged)
    def deletionPlan(self):
        return deepcopy(self._deletion_plan)

    @Property(list, notify=assetChanged)
    def deletionRecords(self):
        return deepcopy(self._deletion_records)

    @Property(str, notify=assetChanged)
    def deletionMessage(self):
        return self._deletion_message

    @Property(bool, notify=assetChanged)
    def deletionBusy(self):
        return self._deletion_busy

    def _asset_action_error(self, message):
        self._deletion_message = str(message)
        self.assetChanged.emit()
        return False

    def _asset_inputs_pending(self):
        if getattr(self, "dirty", False):
            return True
        resources = getattr(self, "resource_workspace", None)
        if resources is not None and any(controller.dirty for controller in tuple(resources.controllers.values())):
            return True
        window = self.window
        if window is None:
            return False
        if window.property("projectPendingTextEdited"):
            return True
        own_decisions = {"assetDeletionPreviewDialog", "assetDeletionHistoryDialog", "assetRestoreDialog"}
        read_only = {"materialReferenceDialog", "materialReferenceDetailsDialog", "projectModuleMenu"}
        for dialog in window.findChildren(QObject):
            if dialog.property("visible") is not True or dialog.property("modal") is not True:
                continue
            name = dialog.objectName()
            if name in own_decisions or name in read_only:
                continue
            if name == "projectResourceInformationDialog" and dialog.property("editing") is not True:
                continue
            return True
        return False

    def _asset_action_admitted(self):
        if self._disposed or self._transfer_busy or self._deletion_busy or getattr(self, "assetApplying", False):
            return self._asset_action_error("资产操作仍在处理，请等待明确结果。")
        if (self._material_bridge is not None and self._material_bridge.loading) or self._asset_inputs_pending():
            return self._asset_action_error("材料仍有未保存修改或待确认输入，请先保存、明确放弃或取消编辑。")
        return True

    def _freeze_asset_presentation(self, frozen):
        if not frozen:
            for item, enabled in self._asset_frozen_content:
                if isValid(item):
                    item.setEnabled(enabled)
            self._asset_frozen_content = []
            if self._asset_material_was_frozen is not None:
                self._material_controller.set_editing_frozen(self._asset_material_was_frozen)
                self._asset_material_was_frozen = None
            return
        windows = [self.window] if self.window is not None else []
        context = getattr(self, "project_context", None)
        if context is not None:
            windows = [context.host.window_for(route) for route in context.host.routes(context.binding.project_id)]
        for window in windows:
            if window is not None:
                item = window.contentItem()
                self._asset_frozen_content.append((item, item.isEnabled()))
                item.setEnabled(False)
        if self._material_controller is not None:
            self._asset_material_was_frozen = bool(getattr(self._material_bridge, "_editing_frozen", False))
            self._material_controller.set_editing_frozen(True)

    def _start_asset_action(self, kind, operation):
        cancelled = Event()
        service = self.service

        def run():
            try:
                result, error = operation(), None
            except Exception as exc:
                result, error = None, str(exc)
            if cancelled.is_set() and kind == "preview" and result:
                service.cancel_resource_deletion(result["plan_id"])
                result = None
            return result, error

        self._deletion_busy = True
        self._deletion_applying = kind in {"apply", "restore"}
        if self._deletion_applying:
            self._freeze_asset_presentation(True)
        self._deletion_message = {"preview": "正在检查资产引用与可恢复路径…", "history": "正在读取可恢复资产…",
            "apply": "正在回收资产，开始后无法取消；请等待明确结果。",
            "restore": "正在恢复资产，开始后无法取消；请等待明确结果。"}[kind]
        try:
            self._deletion_task = (kind, cancelled, _TRANSFER_EXECUTOR.submit(run))
        except RuntimeError as exc:
            self._deletion_busy = self._deletion_applying = False
            self._freeze_asset_presentation(False)
            return self._asset_action_error(exc)
        self._deletion_poll.start()
        self.assetChanged.emit()
        return True

    @Slot()
    def _poll_asset_action(self):
        task = self._deletion_task
        if task is None or not task[2].done():
            return
        kind, cancelled, future = task
        result, error = future.result()
        self._deletion_task = None
        self._deletion_busy = self._deletion_applying = False
        self._freeze_asset_presentation(False)
        self._deletion_poll.stop()
        if cancelled.is_set() or self._disposed:
            # A result may finish just before cancellation, after its worker's
            # cancelled check. The GUI delivery still owns token cleanup.
            if kind == "preview" and result:
                self.service.cancel_resource_deletion(result["plan_id"])
            self.assetChanged.emit()
            return
        if error:
            self._asset_action_error("资产操作未完成：" + error)
            if kind in {"apply", "restore"}:
                self.deletionFinished.emit(False)
            return
        if kind == "preview":
            self._deletion_plan = result
            self._deletion_message = ("此资产不受当前格式支持；确认后删除，不能将其恢复为当前可编辑资产。"
                if result.get("unsupported") else "请审阅资产组成与引用检查结果，再明确确认删除。")
            self.deletionPreviewReady.emit()
        elif kind == "history":
            self._deletion_records = result
            self._deletion_message = "选择一项回收记录后可确认恢复；不会覆盖当前资产。"
            self.deletionHistoryReady.emit()
        else:
            self._deletion_plan = {}
            self._deletion_message = (("资产已删除；不支持的内容不作为当前资产恢复。"
                if not result.get("recoverable", True) else "资产已删除。既有恢复记录位置：")
                if kind == "apply" else "资产已恢复。位置：") + str(result.get("recovery_path", ""))
            if kind == "apply" and self._material_bridge is not None:
                affected = set(result.get("affected_ids", []))
                current = self._material_bridge.currentId
                if any(row["id"] in affected and row.get("material_id") == current
                       for row in self._transfer_catalog.get("rows", [])):
                    self._material_bridge.newMaterial()
                    self._material_editor_active = False
            try:
                self._asset_library_changed()
            except Exception as exc:
                self._deletion_message += " 列表刷新未完成，请重新打开资产库：" + str(exc)
            self.deletionFinished.emit(True)
        self.assetChanged.emit()

    @Slot("QVariantList", result=bool)
    def previewAssetDeletion(self, selected_ids):
        if not self._asset_action_admitted():
            return False
        ids = list(selected_ids)
        catalog = self._transfer_catalog
        if (len(ids) != 1 or not isinstance(ids[0], str)
                or catalog.get("source_path") != self._library_path
                or catalog.get("source_type") != self._library_scope):
            return self._asset_action_error("每次只回收一个当前资产库中的真实资产；导入来源不可删除。")
        rows = [*catalog.get("rows", []), *catalog.get("unavailable_rows", [])]
        row = next((row for row in rows if row["id"] == ids[0]), None)
        if row is None:
            return self._asset_action_error("所选资产已变化，请刷新后重新选择。")
        if self._library_scope == "global" and row["kind"] == "binary_asset":
            return self._asset_action_error("全局依赖输入暂不支持单独回收；原文件保持不变。")
        self.cancelAssetDeletion()
        if self._material_controller is not None:
            try:
                self.service.bind_asset_library_view_owner(self._material_controller)
            except (ValueError, TypeError, OSError) as exc:
                return self._asset_action_error(exc)
        service, catalog_id = self.service, catalog["catalog_id"]
        return self._start_asset_action("preview", lambda: service.preview_resource_deletion(catalog_id, ids))

    @Slot(result=bool)
    def confirmAssetDeletion(self):
        if not self._asset_action_admitted():
            return False
        plan = self._deletion_plan
        if not plan.get("plan_id") or not plan.get("can_apply") or plan.get("scope") != self._library_scope:
            return self._asset_action_error("回收计划存在阻塞或已经失效，请重新检查。")
        service, identity = self.service, plan["plan_id"]
        return self._start_asset_action("apply", lambda: service.apply_resource_deletion(identity))

    @Slot()
    def cancelAssetDeletion(self):
        if self._deletion_applying:
            return
        if self._deletion_task is not None:
            self._deletion_task[1].set()
        identity = self._deletion_plan.get("plan_id")
        self._deletion_plan = {}
        if identity:
            self.service.cancel_resource_deletion(identity)
        self.assetChanged.emit()

    @Slot(result=bool)
    def loadAssetDeletions(self):
        if not self._asset_action_admitted():
            return False
        service = self.service
        return self._start_asset_action("history", service.list_resource_deletions)

    @Slot(str, result=bool)
    def restoreAssetDeletion(self, receipt_id):
        if not self._asset_action_admitted():
            return False
        if not any(row.get("receipt_id") == receipt_id and row.get("scope") == self._library_scope
                   and row.get("recoverable", row.get("state") == "active")
                   for row in self._deletion_records):
            return self._asset_action_error("恢复记录不属于当前资产库，请重新读取。")
        service = self.service
        return self._start_asset_action("restore", lambda: service.restore_resource_deletion(receipt_id))

    def browseLibrary(self):
        raise NotImplementedError("The scope-bound library supplies catalog refresh")

    def _asset_library_changed(self):
        if self._material_bridge is not None:
            self._material_bridge.reload()
        self.browseLibrary()
