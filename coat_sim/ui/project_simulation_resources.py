"""Thin managed-input resource bars for existing film and deposition owners.

These facades never own a second edit buffer or numerical state. Film companions
share their ProjectFilmWorkspace editor; deposition uses its compute draft.
"""
from __future__ import annotations

from typing import Any
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Property, QUrl, Signal, Slot

from coat_sim.core.project_paths import project_default_directory
from coat_sim.core.project_resource_workspace import ProjectResourceWorkspace
from .project_resource_adapter import ProjectResourceTransferController

_RETAINED_TRANSFERS: set[ProjectSimulationResources] = set()


def _information_token(document_id, values):
    return hashlib.sha256(json.dumps([document_id or "", values],
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()



class ProjectSimulationResources(ProjectResourceTransferController):
    # A notifier declared in this class is required by PySide's real QML path.
    resourceChanged = Signal()
    replaceDecisionRequested = Signal()
    closeDecisionRequested = Signal()
    resourceOperationFinished = Signal(str, str, bool, str)
    replacementFinished = Signal(str, bool, str)

    def __init__(self, workspace, module_key, parent=None):
        super().__init__(parent)
        self.changed.connect(self.resourceChanged)
        self.project_context = workspace
        self.owner_binding = workspace.binding
        self.module_key = module_key
        self.resource_workspace = ProjectResourceWorkspace.for_project(workspace.host, workspace.binding)
        self.service = self.resource_workspace.service
        self._pending_replacement = None
        self._pending_replacement_id = ""
        self._pending_replacement_save_id = ""
        self._resource_busy = False
        self._resource_operation_id = ""
        self._resource_operation_kind = ""
        self._resource_operation_error = ""
        self._status = ""
        self._resource_name = ""
        self._detached = False
        self._pending_information_edit = None

    def _documents(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _current_id(self) -> str:
        raise NotImplementedError

    def _information(self) -> dict[str, Any]:
        raise NotImplementedError

    def _update_information(self, name: str, summary: str, notes: str) -> bool:
        raise NotImplementedError

    def _can_replace(self) -> bool:
        raise NotImplementedError

    def _can_save(self) -> bool:
        raise NotImplementedError

    def _is_dirty(self) -> bool:
        raise NotImplementedError

    def _owner_status(self) -> str:
        raise NotImplementedError

    def _perform_replace(self, action: str, identity: str) -> bool:
        raise NotImplementedError

    def _save(self, copy: bool) -> bool:
        raise NotImplementedError

    def _can_export_document(self) -> bool:
        return True

    @Property(bool, notify=resourceChanged)
    def canExportDocument(self):
        return self._can_export_document()

    @Slot(QUrl, result=bool)
    def exportDocument(self, directory):
        try:
            if not self._can_export_document() or not self._can_replace():
                raise ValueError("当前输入暂不支持资源文件夹导出，请使用工程交换包。")
            identity = self._current_id()
            if not identity:
                raise ValueError("请先保存工程资源，再导出资源文件及依赖。")
            if not directory.isLocalFile():
                raise ValueError("请选择本地导出文件夹。")
            destination = Path(directory.toLocalFile()) / ("resource-export-" + uuid4().hex[:12])
            result = self.service.store.export_document(identity, destination)
            self._status = "已导出保存的工程资源及完整依赖：" + result["destination"]
            self.changed.emit()
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._status = str(exc)
            self.changed.emit()
            return False

    @Property(bool, notify=resourceChanged)
    def resourceBusy(self):
        return self._resource_busy

    @Property(bool, notify=resourceChanged)
    def resourceCancellable(self):
        return not self._resource_busy or self._resource_operation_kind in {"open", "restore"}

    @Property(str, notify=resourceChanged)
    def resourceOperationId(self):
        return self._resource_operation_id

    @Property(str, notify=resourceChanged)
    def resourceOperationKind(self):
        return self._resource_operation_kind

    @Property(str, notify=resourceChanged)
    def resourceOperationError(self):
        return self._resource_operation_error

    @Property(str, notify=resourceChanged)
    def replacementRequestId(self):
        return self._pending_replacement_id

    @Property(str, constant=True)
    def moduleKey(self):
        return self.module_key

    @Property(list, notify=resourceChanged)
    def documents(self):
        return self._documents()

    @Property(str, notify=resourceChanged)
    def currentDocumentId(self):
        return self._current_id()

    @Property(str, notify=resourceChanged)
    def resourceName(self):
        return self._resource_name_value()

    def _resource_name_value(self):
        if self._resource_name:
            return self._resource_name
        return next((row["name"] for row in self._documents()
                     if row["id"] == self._current_id()), "沉积配置")

    @Slot(str)
    def setResourceName(self, name):
        self._set_resource_name(str(name))
        self.changed.emit()

    def _set_resource_name(self, name):
        self._resource_name = name

    def _information_edit_owner(self):
        return self

    def _pending_information_changed(self):
        self.changed.emit()

    @Property(dict, notify=resourceChanged)
    def pendingInformationEdit(self):
        owner = self._information_edit_owner()
        pending = getattr(owner, "_pending_information_edit", None)
        presentation = getattr(owner, "_information_edit_presentation", None)
        if pending is None or (presentation is not None and presentation is not self):
            return {}
        owner._information_edit_presentation = self
        return deepcopy(pending)

    @Slot(str, str, str, str, result=bool)
    def stageInformationEdit(self, name, summary, notes, expected_token):
        if self._resource_busy or self._transfer_busy:
            return False
        owner = self._information_edit_owner()
        presentation = getattr(owner, "_information_edit_presentation", None)
        if self._has_information_edit() and presentation is not None and presentation is not self:
            self._status = "另一个窗口正在编辑此输入的信息。"
            self.changed.emit()
            return False
        current = self._information()
        if expected_token != current["token"]:
            self._status = "输入信息已变化，尚未确认的内容未应用。"
            self.changed.emit()
            return False
        if any(not isinstance(value, str) or len(value) > limit for value, limit in
               ((name, 200), (summary, 20000), (notes, 20000))):
            self._status = "信息编辑超过长度限制，当前输入未改变。"
            self.changed.emit()
            return False
        values = {"name": name, "summary": summary, "notes": notes}
        existing = getattr(owner, "_pending_information_edit", None)
        model_dirty = existing["model_dirty"] if existing is not None else self._is_dirty()
        owner._information_edit_presentation = self
        owner._pending_information_edit = (
            None if all(values[key] == current[key] for key in values) else
            {**values, "document_id": self._current_id() or None,
             "token": expected_token, "model_dirty": model_dirty})
        self._pending_information_changed()
        return True

    @Slot()
    def clearInformationEdit(self):
        owner = self._information_edit_owner()
        presentation = getattr(owner, "_information_edit_presentation", None)
        if presentation is not None and presentation is not self:
            return
        owner._pending_information_edit = None
        owner._information_edit_presentation = None
        self._pending_information_changed()

    def _has_information_edit(self):
        return getattr(self._information_edit_owner(), "_pending_information_edit", None) is not None

    @Property(dict, notify=resourceChanged)
    def resourceInformation(self):
        information = self._information()
        presentation = getattr(self._information_edit_owner(), "_information_edit_presentation", None)
        if self._has_information_edit() and presentation is not None and presentation is not self:
            information["editable_fields"] = []
        return information

    @Slot(str, str, str, str, result=bool)
    def updateResourceInformation(self, name, summary, notes, expected_token):
        presentation = getattr(self._information_edit_owner(), "_information_edit_presentation", None)
        if self._has_information_edit() and presentation is not None and presentation is not self:
            self._status = "另一个窗口正在编辑此输入的信息。"
            self.changed.emit()
            return False
        if not self._can_replace():
            return False
        current = self._information()
        if expected_token != current["token"]:
            self._status = "输入已变化，请重新打开信息后编辑。"
            self.changed.emit()
            return False
        try:
            result = self._update_information(str(name), str(summary), str(notes))
            if result:
                self.clearInformationEdit()
                self._status = "信息已更新到当前输入；请保存写入工程。"
            self.changed.emit()
            return bool(result)
        except (ValueError, TypeError, OSError) as exc:
            self._status = "信息没有更新：" + str(exc)
            self.changed.emit()
            return False

    @Property(bool, notify=resourceChanged)
    def dirty(self):
        return self._is_dirty() or self._has_information_edit()

    @Property(bool, notify=resourceChanged)
    def canSave(self):
        return self._can_save()

    @Property(str, notify=resourceChanged)
    def status(self):
        return self._status or self._owner_status()

    @Property(QUrl, constant=True)
    def defaultDirectory(self):
        kind = self.module_key if self.module_key in {
            "film", "raytrace_request", "appearance_configuration"} else "deposition_configuration"
        return QUrl.fromLocalFile(str(project_default_directory(self.service.root, kind)))

    @Property(list, notify=resourceChanged)
    def configurations(self):
        return []

    @Property(str, notify=resourceChanged)
    def defaultConfigurationId(self):
        return ""

    @Property(str, constant=True)
    def defaultParameters(self):
        return "{}"

    def _request_replace(self, action, identity=""):
        if self._has_information_edit():
            self._status = "资源信息仍在编辑，请先确认更新或取消编辑。"
            self.changed.emit()
            return False
        if not self._can_replace():
            self.changed.emit()
            return False
        if action == "open" and identity == self._current_id():
            self.changed.emit()
            return True
        owners = getattr(self.project_context, "_input_resource_replacements", {})
        owner = owners.get(self.module_key)
        if owner is not None and owner is not self:
            self._status = "请先完成此工程输入的现有切换确认。"
            self.changed.emit()
            return False
        if self._pending_replacement is not None:
            return False
        if self._is_dirty():
            owners[self.module_key] = self
            self.project_context._input_resource_replacements = owners
            self._pending_replacement = (action, identity)
            self._pending_replacement_id = uuid4().hex
            self._status = "当前输入有未保存修改。请选择保存、放弃修改或取消。"
            self.changed.emit()
            self.replaceDecisionRequested.emit()
            return False
        return self._perform_replace(action, identity)

    @Slot(str, result=bool)
    def openDocument(self, identity):
        # Validate membership before asking to replace a working draft.
        if not any(str(row["id"]) == str(identity) for row in self._documents()):
            self._status = "所选输入不属于当前工程或资源类型。"
            self.changed.emit()
            return False
        return self._request_replace("open", str(identity))

    @Slot(result=bool)
    def newDocument(self):
        return self._request_replace("new")

    @Slot(str, result=bool)
    def confirmReplace(self, decision):
        pending = self._pending_replacement
        if pending is None:
            return False
        if decision == "cancel":
            if self._resource_busy and self._resource_operation_kind in {"save", "copy"}:
                return False
            self._pending_replacement = None
            self._pending_replacement_id = ""
            self._pending_replacement_save_id = ""
            self._clear_replacement_owner()
            self._status = "已取消切换，当前输入保持不变。"
            self.changed.emit()
            return True
        if decision not in {"save", "discard"}:
            return False
        if not self._can_replace():
            return False
        if decision == "save":
            if not self.saveCurrent():
                return False
            if self._resource_busy:
                self._pending_replacement_save_id = self._resource_operation_id
                return False
        self._pending_replacement = None
        self._pending_replacement_id = ""
        self._clear_replacement_owner()
        return self._perform_replace(*pending)

    def _after_async_replacement_save(self, request_id, success, error=""):
        if request_id != self._pending_replacement_save_id:
            return
        self._pending_replacement_save_id = ""
        replacement_id = self._pending_replacement_id
        if not success:
            if error:
                self._status = str(error)
                self.changed.emit()
            self.replacementFinished.emit(replacement_id, False, str(error))
            return
        pending, self._pending_replacement = self._pending_replacement, None
        self._pending_replacement_id = ""
        self._clear_replacement_owner()
        result = bool(pending is not None and self._perform_replace(*pending))
        self.replacementFinished.emit(replacement_id, result, "" if result else self._owner_status())

    @Slot(result=bool)
    def saveCurrent(self):
        if self._has_information_edit():
            self._status = "资源信息仍在编辑，请先确认更新或取消编辑。"
            self.changed.emit()
            return False
        if not self._can_save():
            return False
        result = self._save(False)
        self.changed.emit()
        return bool(result)

    @Slot(result=bool)
    def saveCopyCurrent(self):
        if self._has_information_edit():
            self._status = "资源信息仍在编辑，请先确认更新或取消编辑。"
            self.changed.emit()
            return False
        if not self._can_save():
            return False
        result = self._save(True)
        self.changed.emit()
        return bool(result)

    @Slot(str, result=bool)
    def confirmClose(self, _decision):
        return False

    def _transfer_applied(self, _result):
        if not self._detached:
            self.changed.emit()

    def _clear_replacement_owner(self):
        owners = getattr(self.project_context, "_input_resource_replacements", {})
        if owners.get(self.module_key) is self:
            owners.pop(self.module_key)

    def detach(self):
        owner = self._information_edit_owner()
        if getattr(owner, "_information_edit_presentation", None) is self:
            owner._information_edit_presentation = None
        self._detached = True
        self._pending_replacement = None
        self._pending_replacement_id = ""
        self._pending_replacement_save_id = ""
        self._clear_replacement_owner()
        if self._transfer_applying:
            # Apply is already admitted and cannot be cancelled. Preserve only
            # its detached facade, independently of the disposable window owner.
            self._dispose_after_transfer = True
            self.setParent(None)
            _RETAINED_TRANSFERS.add(self)
            return
        self.cancelTransfer()

    def dispose(self):
        if self._transfer_applying:
            self.detach()
            return
        _RETAINED_TRANSFERS.discard(self)
        self.cancelTransfer()
        self.deleteLater()


class ProjectFilmResources(ProjectSimulationResources):
    """One disposable bar observing the same retained project film editor."""

    def __init__(self, workspace, source_controller, parent=None):
        super().__init__(workspace, "film", parent)
        if source_controller.project_context is not workspace or source_controller.session is not workspace.editor:
            raise ValueError("Film resource operations require this project's exact film editor")
        self.source = source_controller
        self.editor = workspace.editor
        self._unsubscribe = self.editor.subscribe(lambda _change: self.changed.emit())

    def _information_edit_owner(self):
        return self.editor

    def _pending_information_changed(self):
        self.editor._notify("information_buffer", "ui")
        self.changed.emit()

    def _information(self):
        state = self.editor.snapshot()
        return {"name": str(state.design.get("name", "")), "summary": str(state.design.get("summary", "")),
                "notes": str(state.design.get("note", "")),
                "editable_fields": ["name", "summary", "notes"], "name_limit": 120,
                "token": _information_token(self._current_id(), {
                    "name": str(state.design.get("name", "")),
                    "summary": str(state.design.get("summary", "")),
                    "notes": str(state.design.get("note", ""))})}

    def _update_information(self, name, summary, notes):
        with self.editor._lock:
            candidate = dict(self.editor.snapshot().design)
            candidate.update(name=name, summary=summary, note=notes)
            self.project_context.service.validate(candidate)
            return (self.editor.set_design_value("name", name)
                    and self.editor.set_design_value("summary", summary)
                    and self.editor.set_design_value("note", notes))

    def _documents(self):
        return [{"id": row["design_id"], "name": row["name"]}
                for row in self.project_context.service.list_saved_designs()]

    def _current_id(self):
        path = self.editor.snapshot().path
        return self.project_context.service._handle_id(path) if path else ""

    def _is_dirty(self):
        return bool(self.editor.snapshot().dirty)

    def _owner_status(self):
        state = self.editor.snapshot()
        return state.error or state.message

    def _can_save(self):
        return self._can_replace()

    def _can_replace(self):
        compute = self.service._gate.frozen
        state = self.editor.snapshot()
        return not bool(compute or state.expression_busy or state.pending_confirmation)

    def _perform_replace(self, action, identity):
        # This bar owns its dirty decision, so the designer must not open a
        # second confirmation while the session runs the already-approved action.
        workspace = self.project_context
        workspace._resource_replacement_in_progress = True
        try:
            callback = (self.editor.request_new_design if action == "new"
                        else lambda: self.editor.request_load_saved_design(identity))
            source = None
            for route in workspace.host.routes(workspace.binding.project_id):
                if route.module_key == "film_design":
                    window = workspace.host.window_for(route)
                    candidate = getattr(window, "_project_film_controller", None)
                    if candidate is not None and getattr(candidate, "session", None) is self.editor:
                        source = candidate
                        break
            execute = source._replacement_call if source is not None else lambda action: action()
            result = execute(callback)
            if not result and self.editor.snapshot().pending_confirmation:
                result = execute(lambda: self.editor.resolve_pending_action(True))
            self._status = "" if result else self._owner_status()
            self.changed.emit()
            return bool(result)
        finally:
            workspace._resource_replacement_in_progress = False

    def _save(self, copy):
        self._status = ""
        return self.editor.save_copy_current() if copy else self.editor.save_current()

    def _transfer_applied(self, _result):
        # Refresh only picker rows. Import must never adopt or revise the draft.
        if not self._detached:
            self.editor.refresh_saved_designs()

    def detach(self):
        self._unsubscribe()
        super().detach()


class ProjectDepositionResources(ProjectSimulationResources):
    """Reuse the deposition workspace's existing cdep configuration API."""

    def __init__(self, workspace, bridge, parent=None):
        super().__init__(workspace, "deposition_configuration", parent)
        if bridge.project_context is not workspace:
            raise ValueError("Deposition resources require their exact project controller")
        self.bridge = bridge
        self.compute = bridge._project_compute
        bridge.changed.connect(self.changed)
        bridge.inputsChanged.connect(self.changed)

    def _information_edit_owner(self):
        return self.bridge

    def _pending_information_changed(self):
        self.bridge.changed.emit()
        self.changed.emit()

    def _resource_name_value(self):
        return self.bridge._project_configuration_name

    def _set_resource_name(self, name):
        self.bridge._project_configuration_name = name
        self.bridge.changed.emit()

    def _information(self):
        return {"name": self._resource_name_value(),
                "summary": self.bridge._project_configuration_description,
                "notes": self.bridge._project_configuration_notes,
                "editable_fields": ["name", "summary", "notes"], "name_limit": 100,
                "token": _information_token(self._current_id(), {
                    "name": self._resource_name_value(),
                    "summary": self.bridge._project_configuration_description,
                    "notes": self.bridge._project_configuration_notes})}

    def _update_information(self, name, summary, notes):
        if len(summary) > 20000 or len(notes) > 20000:
            raise ValueError("概述和备注不能超过 20000 字符。")
        if not name.strip() or len(name) > 100:
            raise ValueError("配置名称须为 1 至 100 字符。")
        self.bridge._project_configuration_description = summary
        self.bridge._project_configuration_notes = notes
        self._set_resource_name(name.strip())
        return True

    def _documents(self):
        return self.compute.configurations()

    def _current_id(self):
        return self.compute.configuration_id or ""

    def _is_dirty(self):
        return self.compute.has_dirty_deposition_draft

    def _owner_status(self):
        return self.bridge.status

    def _can_replace(self):
        return not self.compute.editing_frozen and not self.bridge.busy

    def _can_save(self):
        return self._can_replace() and bool(self.compute.process_document_id)

    def _perform_replace(self, action, identity):
        self._status = ""
        self._resource_name = ""
        if action == "open":
            result = self.bridge.loadConfiguration(identity)
        else:
            # A new configuration retains the explicitly bound managed process,
            # while starting fresh simulation parameters. No resource is saved.
            process_id = self.compute.process_document_id
            self.compute.select_context(configuration_id=None, process_document_id=process_id)
            self.bridge._restore_project_draft({"fields": {}, "device": "CPU", "algorithm": "fast",
                                                "fast_cycle": True, "fast_cycle_residual_percent": 100.0,
                                                "time_basis": "time_step_s", "requested_time_step_s": "0.025"})
            self.bridge.refreshEstimate()
            result = True
        self.changed.emit()
        return bool(result)

    @Slot(result=bool)
    def saveAsDefault(self):
        if not self._can_save():
            return False
        return self.bridge.saveConfiguration(self._resource_name_value(), True)

    @Slot(result=bool)
    def loadDefault(self):
        identity = self.compute.store.manifest["default_configuration_id"]
        if not identity:
            self._status = "工程尚无已保存沉积配置。"
            self.changed.emit()
            return False
        return self.openDocument(identity)

    def _save(self, copy):
        name = self._resource_name_value()
        if not copy:
            self._status = ""
            return self.bridge.saveConfiguration(name, False)
        try:
            created = self.compute.save_configuration(self.compute.process_document_id,
                self.bridge._configuration_parameters(), name + " 副本", make_default=False,
                description=self.bridge._project_configuration_description,
                notes=self.bridge._project_configuration_notes)
            self.compute.select_context(configuration_id=created,
                                        process_document_id=self.compute.process_document_id)
            self.bridge._project_configuration_name = name + " 副本"
            self.compute.mark_draft_saved()
            self._status = "已保存沉积配置副本；计算结果没有包含在输入配置中。"
            return True
        except (ValueError, TypeError, OSError, KeyError) as exc:
            self._status = "配置副本保存失败：" + str(exc)
            return False
