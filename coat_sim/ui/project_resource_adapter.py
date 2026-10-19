"""Actual resource editor factory and project document/default controls.

Windows are disposable presentations. Controllers and their existing bridges
remain workspace-owned until the whole project is explicitly closed.
"""
from __future__ import annotations
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
from typing import Any, cast
from threading import Event
from time import perf_counter
from PySide6.QtCore import QObject, Property, QMetaObject, Qt, QUrl, Signal, Slot, QTimer
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlContext
from shiboken6 import isValid
from coat_sim.core.project_default_loading import load_project_default
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_resource_workspace import EDITOR_STATE_FIELDS, ProjectResourceWorkspace
from .icons import window_icon
from .project_material_library import ProjectMaterialLibraryBridge

QML_ROOT = Path(__file__).parent / "qml"
QML_FILES = {"process": "ProcessWindow.qml", "machine": "MachineWindow.qml", "mask": "MaskWindow.qml",
             "source": "SourceWindow.qml", "substrate": "SubstrateWindow.qml", "materials": "MaterialLibraryHost.qml"}


def _find_text_item(window, name):
    """Repeater delegates can belong to the visual tree, not QObject children."""
    from PySide6.QtQuick import QQuickItem
    found = window.findChild(QQuickItem, name)
    if found is not None:
        return found
    pending = [window.contentItem()]
    while pending:
        item = pending.pop()
        if item.objectName() == name:
            return item
        pending.extend(item.childItems())
    return None


def _editor_resource_information(owner):
    if owner.module_key == "materials":
        data = owner.bridge._draft
    else:
        data = getattr(owner.bridge, "_fields", getattr(owner.bridge, "_data", {}))
    values = {"name": str(data.get("name", "")),
              "summary": str(data.get("description", "")),
              "notes": str(data.get("notes", ""))}
    editable = [] if bool(getattr(owner.bridge, "readOnly", False)) else ["name", "summary", "notes"]
    token = hashlib.sha256(json.dumps([owner.currentDocumentId, values],
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    name_limit = 100 if owner.module_key == "process" else 200 if owner.module_key == "materials" else 120
    return {**values, "editable_fields": editable, "name_limit": name_limit, "token": token}


def _update_editor_resource_information(owner, name, summary, notes, expected_token):
    try:
        if owner._transfer_busy:
            raise ValueError("请等待资源传输结束后编辑信息。")
        current = owner.resourceInformation
        if expected_token != current["token"]:
            raise ValueError("当前资源信息已改变，请关闭后重新打开信息。")
        values = {"name": name, "summary": summary, "notes": notes}
        name_limit = 100 if owner.module_key == "process" else 200 if owner.module_key == "materials" else 120
        for key, value in values.items():
            if not isinstance(value, str) or len(value) > (name_limit if key == "name" else 20000):
                raise ValueError("名称须不超过 " + str(name_limit) + " 字，概述和备注须不超过 20000 字。")
            if key not in current["editable_fields"] and value != current[key]:
                raise ValueError("当前资源格式暂不支持独立编辑此字段：" + key)
        if "name" in current["editable_fields"] and not name.strip():
            raise ValueError("名称不能为空。")
        for key, value in values.items():
            if value == current[key]:
                continue
            field = "description" if key == "summary" else key
            if field == "name" and owner.module_key in {"machine", "mask"}:
                owner.bridge.setValue("/name", json.dumps(value, ensure_ascii=False))
            elif owner.module_key == "materials" or field == "name":
                owner.bridge.setField(field, value)
            else:
                owner.bridge.setMetadata(field, value)
        owner._set_status("信息已更新，请保存。")
        return True
    except (ValueError, TypeError, KeyError) as exc:
        owner._set_status("信息未更新：" + str(exc))
        return False


def _stage_information_edit(owner, name, summary, notes, expected_token):
    current = owner.resourceInformation
    if expected_token != current["token"]:
        owner._set_status("当前资源信息已改变，未确认的编辑未应用。")
        return False
    if any(not isinstance(value, str) or len(value) > limit for value, limit in
           ((name, current["name_limit"]), (summary, 20000), (notes, 20000))):
        owner._set_status("信息编辑超过长度限制，当前内容未改变。")
        return False
    values = {"name": name, "summary": summary, "notes": notes}
    owner._pending_information_edit = (None if all(values[key] == current[key] for key in values)
        else {**values, "document_id": owner.currentDocumentId or None, "token": expected_token,
              "model_dirty": bool(getattr(owner.bridge, "dirty", False))})
    owner.changed.emit()
    return True


_TRANSFER_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="project-resource-transfer")


class _ResourceTransferTask:
    """A pure-Python worker result, polled by the owner's main-thread QTimer.

    No QObject is created, signalled or destroyed in a worker thread. This also
    keeps cancelled reads independent of a view's Qt lifetime.
    """

    def __init__(self, generation, kind, operation, service, catalog_id=None):
        self.generation, self.kind = generation, kind
        self.operation, self.service = operation, service
        self.catalog_id = catalog_id
        self.cancelled = Event()
        self.future: Any = None

    def run(self):
        started = perf_counter()
        result, error = None, ""
        try:
            result = self.operation()
        except Exception as exc:
            error = str(exc)
        if self.cancelled.is_set():
            # Only inspection/preview can be cancelled. Release detached read
            # tokens after the running read, never underneath that read.
            try:
                if isinstance(result, dict):
                    if self.kind == "inspect" and result.get("catalog_id"):
                        self.service.release_resource_catalog(result["catalog_id"])
                    elif self.kind == "preview" and result.get("plan_id"):
                        self.service.cancel_resource_transfer(result["plan_id"])
                if self.catalog_id:
                    self.service.release_resource_catalog(self.catalog_id)
            except Exception:
                pass
            result = None
        return result, error, perf_counter() - started


class ProjectResourceTransferController(QObject):
    """Asynchronous, draft-independent facade for staged resource transfers.

    Subclasses provide ``service``. Opaque catalogs/plans belong to that service.
    Slot booleans acknowledge admission; transferChanged/transferFinished report
    the eventual result. Read/preview cancellation invalidates a generation.
    Apply cannot be cancelled after admission and retains its QObject owner.
    """

    module_key: str

    changed = Signal()
    transferChanged = Signal()
    transferFinished = Signal(bool)
    transferOperationFinished = Signal(int, str, bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.service: Any = None
        self._transfer_catalog: dict[str, Any] = {}
        self._transfer_plan: dict[str, Any] = {}
        self._transfer_mode = ""
        self._transfer_status = ""
        self._transfer_active = False
        self._transfer_applying = False
        self._transfer_busy = False
        self._transfer_generation = 0
        self._transfer_tasks: dict[int, _ResourceTransferTask] = {}
        self._transfer_timings: dict[str, float] = {}
        self._dispose_after_transfer = False
        self._transfer_poll = QTimer(self)
        self._transfer_poll.setInterval(10)
        self._transfer_poll.timeout.connect(self._poll_transfer_tasks)

    def _transfer_kinds(self) -> list[str]:
        """Typed internal view of the ``transferKinds`` QML property."""
        return cast(list[str], self.transferKinds)

    @Property(list, constant=True)
    def transferKinds(self) -> list[str]:
        module = getattr(self, "module_key", "")
        if not module:
            return []  # General library/standalone exchange owns no designer type.
        if module == "materials":
            return ["material_profile", "optical_material"]
        return [{"film_design": "film", "film_optimization": "film", "spectrum": "film",
                 "deposition": "deposition_configuration", "raytrace": "raytrace_request",
                 "appearance": "appearance_configuration"}.get(module, module)]

    @Property(list, constant=True)
    def transferFileFilters(self) -> list[str]:
        from coat_sim.core.project_dependencies import EXTENSIONS
        kinds = self._transfer_kinds()
        suffixes = sorted({EXTENSIONS[kind] for kind in kinds if kind in EXTENSIONS})
        if getattr(self, "module_key", "") == "materials":
            suffixes = sorted({*suffixes, ".cmat", ".cmatl"})
        if not kinds:
            suffixes = sorted(set(EXTENSIONS.values()))
        return ["工程与目标资源 (*.coatproj *.cproject " + " ".join("*" + value for value in suffixes) + ")"]

    def _scoped_transfer_catalog(self, catalog):
        kinds = self._transfer_kinds()
        if not kinds:
            return catalog
        rows = [row for row in catalog.get("rows", []) if row["kind"] in kinds]
        return {**catalog, "rows": rows, "categories": sorted({row["kind"] for row in rows})}

    def _validate_transfer_selection(self, ids):
        rows = {row["id"]: row for row in self._transfer_catalog.get("rows", [])}
        kinds = self._transfer_kinds()
        if any(identity not in rows or (kinds and rows[identity]["kind"] not in kinds)
               for identity in ids):
            raise ValueError("所选资源类型不属于当前窗口，或资源已不在当前目录。")

    @Property(dict, notify=transferChanged)
    def transferCatalog(self):
        return deepcopy(self._transfer_catalog)

    @Property(dict, notify=transferChanged)
    def transferPlan(self):
        return deepcopy(self._transfer_plan)

    @Property(str, notify=transferChanged)
    def transferMode(self):
        return self._transfer_mode

    @Property(str, notify=transferChanged)
    def transferStatus(self):
        return self._transfer_status

    @Property(bool, notify=transferChanged)
    def transferActive(self):
        return self._transfer_active

    @Property(bool, notify=transferChanged)
    def transferBusy(self):
        return self._transfer_busy

    @Property(int, notify=transferChanged)
    def transferOperationId(self):
        return self._transfer_generation

    @Property(bool, notify=transferChanged)
    def transferCancellable(self):
        return not self._transfer_applying

    @Property(dict, notify=transferChanged)
    def transferTimings(self):
        return dict(self._transfer_timings)

    @staticmethod
    def _transfer_location(value):
        value = str(value)
        if value in {"project", "global"} or Path(value).is_absolute():
            return value
        url = QUrl(value)
        if url.isLocalFile():
            return url.toLocalFile()
        if url.scheme():
            raise ValueError("请选择本地资源或工程文件。")
        if not value:
            raise ValueError("请选择资源位置。")
        return value

    def _release_transfer_plan(self):
        identity = self._transfer_plan.get("plan_id")
        self._transfer_plan = {}
        if identity:
            self.service.cancel_resource_transfer(identity)

    def _release_transfer_catalog(self):
        identity = self._transfer_catalog.get("catalog_id")
        self._transfer_catalog = {}
        if identity:
            self.service.release_resource_catalog(identity)

    def _transfer_failed(self, exc):
        self._transfer_status = "资源传输未完成：" + str(exc)
        self.transferChanged.emit()
        return False

    def _start_transfer_task(self, kind, operation, *, catalog_id=None):
        self._transfer_generation += 1
        generation = self._transfer_generation
        task = _ResourceTransferTask(generation, kind, operation, self.service, catalog_id)
        self._transfer_tasks[generation] = task
        self._transfer_busy = True
        self._transfer_applying = kind == "apply"
        self._transfer_status = {"inspect": "正在读取并校验资源…", "preview": "正在校验依赖与冲突…",
                                 "apply": "正在提交所选资源，开始后无法取消；请等待结果。"}[kind]
        self.transferChanged.emit()
        task.future = _TRANSFER_EXECUTOR.submit(task.run)
        self._transfer_poll.start()
        return True

    @Slot()
    def _poll_transfer_tasks(self):
        for generation, task in tuple(self._transfer_tasks.items()):
            if task.future is not None and task.future.done():
                result, error, elapsed = task.future.result()
                self._on_transfer_result(generation, task.kind, result, error, elapsed)
        if not self._transfer_tasks:
            self._transfer_poll.stop()

    def _on_transfer_result(self, generation, kind, result, error, elapsed):
        task = self._transfer_tasks.pop(generation, None)
        if generation != self._transfer_generation:
            # Cancel can race the gap between worker completion and delivery
            # before its main-thread poll. Such a late token must still be released.
            if isinstance(result, dict):
                if kind == "inspect" and result.get("catalog_id"):
                    self.service.release_resource_catalog(result["catalog_id"])
                elif kind == "preview" and result.get("plan_id"):
                    self.service.cancel_resource_transfer(result["plan_id"])
                    if task is not None and task.catalog_id:
                        self.service.release_resource_catalog(task.catalog_id)
            return
        self._transfer_busy = False
        self._transfer_applying = False
        self._transfer_timings[kind] = elapsed
        if error:
            self._transfer_failed(error)
            if kind == "apply":
                self.transferFinished.emit(False)
        elif kind == "inspect":
            self._transfer_catalog = self._scoped_transfer_catalog(result)
            self._transfer_status = ("请选择要传输的资源；依赖将在预览中自动包含。"
                if self._transfer_catalog.get("rows") else "此位置没有当前窗口类型的可传输资源，请选择其他来源。")
            self.transferChanged.emit()
        elif kind == "preview":
            self._transfer_plan = result
            self._transfer_status = "预览已就绪。依赖自动包含；有冲突时须逐项明确处理。"
            self.transferChanged.emit()
        else:
            self._transfer_plan = {}
            self._release_transfer_catalog()
            self._transfer_active = False
            self._transfer_status = "所选资源及依赖已传输；当前编辑内容未替换。"
            try:
                self._transfer_applied(result)
            except Exception as exc:
                self._transfer_status += " 列表刷新失败，请重新打开资源列表：" + str(exc)
            set_status = getattr(self, "_set_status", None)
            if callable(set_status):
                set_status(self._transfer_status)
            self.transferChanged.emit()
            self.transferFinished.emit(True)
        # Report a delivered foreground operation, not merely a cleared busy
        # flag. Invalidated read generations return above after token cleanup;
        # they cannot complete a newer inspection or preview for the UI.
        self.transferOperationFinished.emit(generation, kind, not bool(error), error)
        if self._dispose_after_transfer:
            dispose = getattr(self, "dispose", None)
            if callable(dispose):
                dispose()
            else:
                self.deleteLater()

    @Slot()
    def clearTransferPreview(self):
        if self._transfer_busy:
            return
        self._release_transfer_plan()
        self._transfer_status = "请选择资源，然后重新预览。"
        self.transferChanged.emit()

    @Slot(str, result=bool)
    def beginTransfer(self, mode):
        if mode not in {"import", "export"} or self._transfer_busy:
            return False
        self.cancelTransfer()
        self._transfer_mode = mode
        self._transfer_active = True
        return self.inspectTransferSource("global" if mode == "import" else "project")

    @Slot(str, result=bool)
    def inspectTransferSource(self, source):
        if not self._transfer_active or self._transfer_busy:
            return False
        try:
            self._release_transfer_plan()
            self._release_transfer_catalog()
            location = self._transfer_location(source)
            service = self.service
            return self._start_transfer_task("inspect", lambda: service.inspect_resources(location))
        except (ValueError, TypeError, OSError, KeyError) as exc:
            return self._transfer_failed(exc)

    @Slot("QVariantList", str, result=bool)
    def previewTransfer(self, selected_ids, target):
        if not self._transfer_active or self._transfer_busy:
            return False
        try:
            self._release_transfer_plan()
            if not self._transfer_catalog:
                raise ValueError("请先选择资源来源。")
            ids = list(selected_ids)
            if not ids or any(not isinstance(value, str) for value in ids):
                raise ValueError("请至少选择一个资源。")
            self._validate_transfer_selection(ids)
            target = self._transfer_location(target)
            if self._transfer_mode == "import" and target != "project":
                raise ValueError("导入的目标必须是当前工程。")
            if self._transfer_mode == "export" and target == "project":
                raise ValueError("请选择全局资源库或工程文件作为导出目标。")
            service, catalog_id = self.service, self._transfer_catalog["catalog_id"]
            return self._start_transfer_task("preview", lambda: service.preview_resource_transfer(
                catalog_id, ids, target), catalog_id=catalog_id)
        except (ValueError, TypeError, OSError, KeyError) as exc:
            return self._transfer_failed(exc)

    @Slot("QVariantMap", result=bool)
    def applyTransfer(self, decisions):
        if not self._transfer_active or self._transfer_busy:
            return False
        try:
            identity = self._transfer_plan.get("plan_id")
            if not identity:
                raise ValueError("请先预览所选资源和依赖。")
            service, choices = self.service, dict(decisions)
            return self._start_transfer_task("apply", lambda: service.apply_resource_transfer(identity, choices))
        except (ValueError, TypeError, OSError, KeyError) as exc:
            return self._transfer_failed(exc)

    def _transfer_applied(self, result):
        """Refresh presentation lists after publish, without adopting a draft."""

    @Slot()
    def cancelTransfer(self):
        if self._transfer_applying:
            self._transfer_status = "正在提交资源，无法取消；请等待明确结果。"
            self.transferChanged.emit()
            return
        if self._transfer_busy:
            task = self._transfer_tasks.get(self._transfer_generation)
            if task is not None:
                task.cancelled.set()
            self._transfer_generation += 1
            self._transfer_catalog = {}
            self._transfer_plan = {}
        else:
            self._release_transfer_plan()
            self._release_transfer_catalog()
        self._transfer_busy = False
        self._transfer_active = False
        self._transfer_mode = ""
        self._transfer_status = ""
        self.transferChanged.emit()
        self._poll_transfer_tasks()


class ProjectResourceController(ProjectResourceTransferController):
    # PySide needs subclass-owned property notifiers when inheriting QObject.
    resourceChanged = Signal()
    projectNameChanged = Signal()
    closeDecisionRequested = Signal()

    def __init__(self, film_workspace, resource_workspace, module_key, parent=None, navigation=None,
                 *, material_global_library=None):
        super().__init__(parent)
        self.changed.connect(self.resourceChanged)
        self.project_context = film_workspace
        self.resource_workspace = resource_workspace
        self.owner_binding = resource_workspace.binding
        project_information_changed = getattr(parent, "projectInformationChanged", None)
        if project_information_changed is not None:
            project_information_changed.connect(self._project_information_changed)
        self.module_key = module_key
        self.project_navigation = navigation
        self.service = resource_workspace.service.fork_editor()
        self.window = None
        self._status = ""
        self._disposed = False
        self._opening = False
        self._pending_ui = None
        self._close_pending_ui = None
        self._local_close_review = False
        self._pending_information_edit = None
        self.bridge: Any
        if module_key == "process":
            from .process import ProcessBridge
            self.bridge = ProcessBridge(self.service)
        elif module_key in {"machine", "mask"}:
            from .machine_mask import MachineBridge, MaskBridge
            self.bridge = MachineBridge(self.service) if module_key == "machine" else MaskBridge(self.service)
        elif module_key in {"source", "substrate"}:
            from .source_substrate import SourceBridge, SubstrateBridge
            self.bridge = SourceBridge(self.service) if module_key == "source" else SubstrateBridge(self.service)
        else:
            self.bridge = ProjectMaterialLibraryBridge(self.service, global_library=material_global_library)
        self.bridge.setParent(self)
        for name in ("documentChanged", "changed", "dirtyChanged", "validationChanged"):
            signal = getattr(self.bridge, name, None)
            if signal is not None:
                signal.connect(self.changed)
        if module_key == "mask" and hasattr(self.bridge, "optimizationChanged"):
            self.bridge.optimizationChanged.connect(self.changed)
        initial = resource_workspace.initial_document(module_key)
        if initial is not None:
            self._open_managed(initial)
        self._remember_clean_state()
        restored = resource_workspace.recovery_record(module_key)
        if restored is not None:
            self.restore_recovery_snapshot(restored)
        self.changed.connect(self._track_clean_state)

    @Property(str, constant=True)
    def moduleKey(self):
        return self.module_key

    @Slot(str)
    def _project_information_changed(self, project_id: str) -> None:
        if project_id != self.owner_binding.project_id:
            return
        try:
            self.resource_workspace.host.store_for(self.owner_binding)
        except ValueError:
            return
        self.projectNameChanged.emit()

    @Property(str, notify=projectNameChanged)
    def projectName(self):
        return self.resource_workspace.name

    @property
    def display_name(self):
        return self.module_key

    @Property(str, notify=resourceChanged)
    def displayName(self):
        return _editor_resource_information(self)["name"] or "未命名"

    @Property(bool, notify=resourceChanged)
    def dirty(self):
        own = bool(getattr(self.bridge, "dirty", False))
        material = getattr(self.bridge, "_material_library", None)
        return (own or self._pending_information_edit is not None
                or self.snapshot_pending_edits() is not None
                or (material is not None and bool(material.dirty)))

    @Property(bool, notify=resourceChanged)
    def canSave(self):
        return bool(getattr(self.bridge, "valid", getattr(self.bridge, "canSave", True))) and not bool(
            getattr(self.bridge, "loading", False))

    @Property(dict, notify=resourceChanged)
    def resourceInformation(self):
        return _editor_resource_information(self)

    @Property(dict, notify=resourceChanged)
    def pendingInformationEdit(self):
        return deepcopy(self._pending_information_edit or {})

    @Slot(str, str, str, str, result=bool)
    def stageInformationEdit(self, name, summary, notes, expected_token):
        return _stage_information_edit(self, name, summary, notes, expected_token)

    @Slot()
    def clearInformationEdit(self):
        self._pending_information_edit = None
        self.changed.emit()

    @Slot(str, str, str, str, result=bool)
    def updateResourceInformation(self, name, summary, notes, expected_token):
        result = _update_editor_resource_information(self, name, summary, notes, expected_token)
        if result:
            self.clearInformationEdit()
        return result

    def _transfer_applied(self, result):
        for controller in tuple(self.resource_workspace.controllers.values()):
            controller.changed.emit()
        self.service.refresh_bound_film_materials()
        editor = getattr(self.project_context, "editor", None)
        if editor is not None:
            editor.refresh_saved_designs()

    @Property(str, notify=resourceChanged)
    def status(self):
        return self._status

    @Property(QUrl, constant=True)
    def defaultDirectory(self):
        kind = "material_profile" if self.module_key == "materials" else self.module_key
        return QUrl.fromLocalFile(str(self.service.default_directory(kind)))

    @Property(list, notify=resourceChanged)
    def documents(self):
        kind = "material_profile" if self.module_key == "materials" else self.module_key
        return self.service.list_documents(kind)

    @Property(list, notify=resourceChanged)
    def configurations(self):
        return self.service.list_documents("deposition_configuration")

    @Property(str, notify=resourceChanged)
    def currentDocumentId(self):
        if self.module_key == "materials":
            current = self.bridge.currentId
            for row in self.service.list_documents("material_profile"):
                if self.service.load_document(row["id"]).data["material"]["id"] == current:
                    return row["id"]
            return ""
        path = getattr(self.bridge, "_path", None)
        return self.service._identity_for_path(path) or "" if path else ""

    @Property(str, notify=resourceChanged)
    def defaultConfigurationId(self):
        return self.service.store.manifest["default_configuration_id"] or ""

    @Property(str, notify=resourceChanged)
    def defaultParameters(self):
        graph = self.service.resolve_default()
        return json.dumps({} if graph is None else graph["configuration"]["parameters"], ensure_ascii=False, indent=2)

    @Property(str, constant=True)
    def fileFilter(self):
        from coat_sim.core.project_dependencies import EXTENSIONS
        return "项目资源 (*" + EXTENSIONS.get(self.module_key, ".json") + ")"

    def _set_status(self, value):
        self._status = str(value)
        if hasattr(self.bridge, "_set_status"):
            self.bridge._set_status(self._status)
        else:
            self.bridge._status = self._status
            self.bridge.changed.emit()
        self.changed.emit()

    def _open_managed(self, document_id):
        path = str(self.service.document_handle(document_id))
        if self.module_key in {"machine", "mask"}:
            self.bridge.loadDocument(path)
        elif self.module_key == "materials":
            record = self.service.load_document(document_id).data["material"]
            self.bridge.selectMaterial(record["id"])
        else:
            self.bridge.openPath(QUrl.fromLocalFile(path))
        if not bool(getattr(self.bridge, "dirty", False)):
            self._remember_clean_state()

    def _track_clean_state(self):
        if (not bool(getattr(self.bridge, "dirty", False))
                and not bool(getattr(self.bridge, "loading", False))
                and self._pending_information_edit is None and self.snapshot_pending_edits() is None):
            self._remember_clean_state()
        else:
            self._remember_clean_material()

    def _remember_clean_material(self):
        # Embedded materials have an independent Save boundary, even while the
        # parent source/substrate still contains unsaved geometry or metadata.
        nested = getattr(self.bridge, "_material_library", None)
        if nested is not None and not nested.dirty and not nested.loading:
            state = {name: (sorted(value) if isinstance(value, set) else deepcopy(value))
                     for name in EDITOR_STATE_FIELDS["materials"]
                     for value in (getattr(nested, name),)}
            self._clean_snapshot["nested_material"] = {"dirty": False, "editor_state": state}

    def _remember_clean_state(self):
        previous_nested = getattr(self, "_clean_snapshot", {}).get("nested_material")
        self._clean_snapshot = self.snapshot()
        self._clean_snapshot.pop("pending_ui", None)
        self._clean_snapshot.pop("pending_information", None)
        self._clean_snapshot.pop("nested_material", None)
        self._clean_snapshot["dirty"] = False
        if previous_nested is not None:
            self._clean_snapshot["nested_material"] = previous_nested
        self._remember_clean_material()

    def _discard_closed_edits(self):
        # Called only after a real accepted native close. A last-window review
        # can reject that close and must retain every module until its decision.
        if self.window is not None and isValid(self.window):
            field = self.window.property("activeFocusItem")
            if field is not None:
                field.setProperty("numericValueEdited", False)
            self.window.setProperty("projectPendingTextEdited", False)
        self._close_pending_ui = None
        self.restore_snapshot(self._clean_snapshot)
        self.resource_workspace._pending_recovery.pop(self.module_key, None)

    @Slot(str, result=list)
    def resourcesForKind(self, kind):
        return self.service.list_documents(kind)

    @Slot(str, str, result=str)
    def dependencyDocumentId(self, kind, reference):
        """Map a serialized project reference or managed handle to its ID."""
        if not reference or kind not in {"process", "machine", "source", "mask", "substrate"}:
            return ""
        try:
            value = str(reference)
            if Path(value).name == value and "\\" not in value:
                return self.service._reference_identity(value, kind)
            identity = self.service._identity_for_path(value, expected_kind=kind)
            return identity or ""
        except (ValueError, TypeError, OSError):
            return ""

    @Slot(str, result=bool)
    def openDependencyEditor(self, pointer):
        """Route the exact dependency from this retained draft in this project."""
        try:
            navigation = self.project_navigation
            if navigation is None or getattr(navigation, "workspace", None) is not self.project_context:
                raise ProjectError("dependency_conflict", "没有同项目资源编辑器路由。")
            if self.project_context is None or self.owner_binding != self.project_context.binding:
                raise ProjectError("dependency_conflict", "资源窗口与项目导航绑定不一致。")
            if self.module_key == "machine":
                if not isinstance(pointer, str):
                    raise ValueError("机器依赖指针无效")
                parts = pointer.split("/")
                if (len(parts) != 4 or parts[0] or parts[1] not in {"sources", "masks"}
                        or parts[3] != "file"):
                    raise ValueError("机器依赖指针无效")
                index = int(parts[2])
                collection = self.bridge._data.get(parts[1], [])
                if not 0 <= index < len(collection):
                    raise ValueError("机器依赖槽位不存在")
                module = "source" if parts[1] == "sources" else "mask"
                reference = str(collection[index].get("file", ""))
            elif self.module_key == "process" and pointer in {"machine_file", "substrate_file"}:
                module = "machine" if pointer == "machine_file" else "substrate"
                reference = str(self.bridge._data.get(pointer, ""))
            elif self.module_key == "mask" and pointer == "machine":
                module = "machine"
                reference = str(getattr(self.bridge, "_machine_path", ""))
            else:
                raise ValueError("当前编辑器不支持此依赖路由")

            identity = None
            if reference:
                if self.module_key == "mask":
                    identity = self.service._identity_for_path(reference, expected_kind=module)
                else:
                    identity = self.service._reference_identity(reference, module)
                if not identity:
                    raise ProjectError("missing_dependency", "此依赖尚未复制到当前项目。请先导入项目资源。")
            if not navigation.openResourceDocument(module, identity or ""):
                raise ProjectError("open_dependency_failed", "同项目资源编辑器未能打开目标文档。")
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._set_status(exc)
            return False

    @Slot(str, result=bool)
    def openOptimizedProcess(self, path):
        """Import an optimizer-produced process closure, then route within owner."""
        try:
            navigation = self.project_navigation
            if self.module_key != "mask" or navigation is None:
                raise ProjectError("dependency_conflict", "优化产物只能由同项目修正板窗口打开。")
            if getattr(navigation, "workspace", None) is not self.project_context:
                raise ProjectError("dependency_conflict", "优化产物路由属于其他项目。")
            target = Path(str(path)).expanduser().absolute()
            result = self.bridge.optimization
            expected_path = str((result.get("artifacts") or {}).get("process_path") or "")
            if (result.get("project_id") != self.owner_binding.project_id or not expected_path
                    or target != Path(expected_path).expanduser().absolute()):
                raise ProjectError("dependency_conflict", "打开路径与当前项目优化任务的工艺结果不匹配。")
            identity = self.service._identity_for_path(target, expected_kind="process")
            if identity is None:
                identity = self.service.import_resource(target, expected_kind="process")
            if not navigation.openResourceDocument("process", identity):
                raise ProjectError("open_dependency_failed", "优化工艺副本已导入，但工艺窗口未能打开。")
            self._set_status("优化工艺及其依赖已复制到当前项目并打开；项目默认配置未改变。")
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._set_status(exc)
            return False

    @Slot(str, str, result=bool)
    def chooseDependency(self, pointer, document_id):
        try:
            path = self.service.document_handle(document_id)
            expected_kind = ("machine" if pointer == "machine_file" else "substrate" if pointer == "substrate_file"
                             else "source" if pointer.startswith("/sources/") else "mask")
            if self.service._record(document_id)["kind"] != expected_kind:
                raise ProjectError("dependency_conflict", "Chosen dependency has the wrong project resource kind")
            if self.module_key == "process" and pointer in {"machine_file", "substrate_file"}:
                self.bridge.chooseDependency(pointer, QUrl.fromLocalFile(str(path)))
            elif self.module_key == "machine" and pointer.startswith(("/sources/", "/masks/")):
                self.bridge.setDependencyFile(pointer, str(path))
            else:
                raise ProjectError("dependency_conflict", "Unsupported resource dependency field")
            self.changed.emit()
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._set_status(exc)
            return False

    @Slot(str, result=bool)
    def openDocument(self, document_id):
        try:
            if self._pending_information_edit is not None:
                raise ValueError("信息仍在编辑，请先确认更新或取消编辑后切换资源。")
            if document_id == self.currentDocumentId:
                self.changed.emit()
                return True
            if self.commit_pending_edits() is False:
                return False
            self._open_managed(document_id)
            self.changed.emit()
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._set_status(exc)
            return False

    @Slot(QUrl, result=bool)
    def importDocument(self, url):
        try:
            if self.commit_pending_edits() is False:
                return False
            path = Path(url.toLocalFile())
            if self.module_key == "materials":
                return self.bridge.previewImport(str(path))
            if self.module_key in {"machine", "mask"}:
                self.bridge.loadDocument(str(path))
            else:
                self.bridge.openPath(QUrl.fromLocalFile(str(path)))
            self.changed.emit()
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._set_status(exc)
            return False

    @Slot(result=bool)
    def newDocument(self):
        if self._pending_information_edit is not None:
            self._set_status("信息仍在编辑，请先确认更新或取消编辑后新建资源。")
            return False
        if self.commit_pending_edits() is False:
            return False
        if self.module_key == "materials":
            return self.bridge.newMaterial()
        if self.module_key == "machine":
            self.bridge.newDocument(json.dumps({"schema_version": 5, "carrier_x_mm": 0., "carrier_y_mm": 0.,
                "planet_offset_mm": 312., "planet_tilt_deg": 0., "substrate_z_mm": 580., "sun_teeth": 120,
                "planet_teeth": 36, "gear_module_mm": 4., "sources": [], "masks": [], "monitors": []}))
        elif self.module_key == "mask":
            self.bridge.newDocument(json.dumps({"z_mm": 300., "outer": [], "holes": []}))
        else:
            self.bridge.newDocument()
        self.changed.emit()
        return True

    def document_data(self):
        if self.module_key == "materials":
            return {"kind": "coat_sim.project_material_profile", "version": 1, "material": self.bridge.draftData()}
        if self.module_key in {"source", "substrate"}:
            return self.bridge.documentData()
        return deepcopy(self.bridge._data)

    def commit_pending_edits(self):
        pending = self.snapshot_pending_edits()
        if pending is not None and pending["numeric_value_edited"]:
            try:
                if not math.isfinite(float(pending["text"].strip())):
                    raise ValueError("non-finite numeric text")
            except (ValueError, OverflowError):
                self._set_status("数值输入无效，请修改后保存，或选择放弃修改。")
                return False
        if self._pending_ui is not None:
            return False  # Unopened recovery must be replayed into its editor first.
        if self.window is not None and isValid(self.window):
            if pending is not None:
                field = _find_text_item(self.window, pending["object_name"])
                if field is not None:
                    field.setProperty("text", pending["text"])
                    field.setProperty("numericValueEdited", pending["numeric_value_edited"])
                # Replayed text is assigned programmatically. Qt may not mark
                # its native editing buffer modified, so blur alone is not a
                # reliable commit for an approved recovered field.
                if field is None or not QMetaObject.invokeMethod(field, "editingFinished", Qt.ConnectionType.DirectConnection):
                    return False
            if not QMetaObject.invokeMethod(self.window, "commitPendingEdits", Qt.ConnectionType.DirectConnection):
                return False
        self._pending_ui = None
        self._close_pending_ui = None
        return True

    def snapshot_pending_edits(self):
        """Read the active text buffer without changing focus or emitting commits."""
        if self._pending_ui is not None:
            return deepcopy(self._pending_ui)
        retained = deepcopy(self._close_pending_ui)
        if self.window is None or not isValid(self.window):
            return retained
        if not self.window.property("projectPendingTextEdited"):
            return retained
        field = self.window.property("activeFocusItem")
        if field is None or not isinstance(field.property("text"), str):
            return retained
        # Other resource fields commit on textEdited. These fields instead own
        # a document buffer until editingFinished; transient tool inputs are
        # deliberately excluded from a saved resource's recovery state.
        names = {"process": ("processNameField", "processMachineField", "processSubstrateField", "stepThicknessField", "stepRateField", "stepMonitorSlotField"),
                 "machine": ("geometryField_", "solarRadiusField", "planetRadiusField", "sourceFileField", "sourceXField", "sourceYField", "sourceZField", "maskFileField", "monitorNameField", "monitorField"),
                 "mask": ("maskHeightField",)}
        if not field.objectName().startswith(names.get(self.module_key, ())):
            return retained
        return {"object_name": field.objectName(), "text": field.property("text"),
                "numeric_value_edited": bool(field.property("numericValueEdited"))}

    def restore_pending_edits(self):
        """Replay explicitly approved text into a view, leaving it uncommitted."""
        if self._pending_ui is None or self.window is None:
            return
        if not self.window.isVisible():
            return
        field = _find_text_item(self.window, self._pending_ui["object_name"])
        if field is None or not isinstance(field.property("text"), str):
            raise ProjectError("stale_recovery", "Recovered resource field is unavailable in this editor")
        self.window.requestActivate()
        field.forceActiveFocus()
        if not field.hasActiveFocus():
            QTimer.singleShot(0, self.restore_pending_edits)
            return
        field.setProperty("text", self._pending_ui["text"])
        field.setProperty("numericValueEdited", self._pending_ui["numeric_value_edited"])
        self.window.setProperty("projectPendingTextEdited", True)
        self._pending_ui = None

    @Slot(result=bool)
    def saveCurrent(self):
        return self.save_current()

    def save_current(self):
        try:
            if self._pending_information_edit is not None:
                raise ProjectError("unconfirmed_information", "信息仍在编辑，请先确认更新或取消编辑。")
            if self.commit_pending_edits() is False:
                raise ProjectError("uncommitted_edit", "Resource field could not be committed")
            if self.module_key == "materials":
                saved = self.bridge.save()
                if saved:
                    self._remember_clean_state()
                return saved
            nested = getattr(self.bridge, "_material_library", None)
            if nested is not None and nested.dirty and not nested.save():
                raise ProjectError("invalid_document", "Embedded material editor has unsaved invalid content")
            data = self.document_data()
            from coat_sim.core.project_dependencies import EXTENSIONS
            path = getattr(self.bridge, "_path", None) or self.service.root / ("new" + EXTENSIONS[self.module_key])
            saved = self.service.save(self.module_key, path, data, overwrite=bool(self.currentDocumentId))
            self.adopt_saved(saved)
            self._set_status("已保存到当前项目。")
            return True
        except (ValueError, TypeError, OSError, KeyError) as exc:
            self._set_status("保存失败，当前内容已保留：" + str(exc))
            return False

    @Slot(result=bool)
    def saveCopyCurrent(self):
        try:
            if self._pending_information_edit is not None:
                raise ValueError("信息仍在编辑，请先确认更新或取消编辑后保存副本。")
            if self.commit_pending_edits() is False:
                raise ProjectError("uncommitted_edit", "Resource field could not be committed")
            if self.module_key == "materials":
                from uuid import uuid4
                record = self.bridge.draftData()
                record["id"] = "material_" + uuid4().hex
                record["name"] = record["name"][:110] + " 副本"
                saved = self.bridge.project_library.write(record)["record"]
                self.bridge.reload()
                self.bridge._schedule_load("select", saved)
                self._remember_clean_state()
                self._set_status("已保存独立项目材料副本。")
                self.bridge._refresh_bound_film_picker()
                return True
            data = self.document_data()
            from coat_sim.core.resource_information import resource_display_name
            suffix = " 副本"
            prefix_limit = int(_editor_resource_information(self)["name_limit"]) - len(suffix)
            data["name"] = resource_display_name(self.module_key, data)[:prefix_limit] + suffix
            identity = self.service.create_resource(self.module_key, data)
            self.adopt_saved(self.service.load_document(identity))
            self._set_status("已保存独立项目资源副本。")
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._set_status("保存副本失败，当前内容已保留：" + str(exc))
            return False

    def prepare_save_operations(self):
        """Detached batch candidate; do not change dirty state before publish."""
        if self._pending_information_edit is not None:
            raise ProjectError("unconfirmed_information", "信息仍在编辑，请先确认更新或取消编辑。")
        from coat_sim.core.project_dependencies import normalize_document
        new, updates, revisions = [], {}, {}
        material_bridge = self.bridge if self.module_key == "materials" else getattr(self.bridge, "_material_library", None)
        if material_bridge is not None and bool(material_bridge.dirty):
            record = material_bridge.draftData()
            library = material_bridge.project_library
            matching = next(((row, profile) for row, profile in library._rows()
                             if profile["id"] == material_bridge._saved_id), None)
            revisions.update(material_bridge._selected_revisions)
            if matching is not None:
                row = matching[0]
                updates[row["id"]] = {"kind": "coat_sim.project_material_profile", "version": 1, "material": record}
                revisions.setdefault(row["id"], row["revision"])
            else:
                new.append(("material_profile", {"kind": "coat_sim.project_material_profile", "version": 1, "material": record}))
            # A user's explicit save may promote a global profile whose optical
            # snapshot is already owned by this project (for example via a
            # saved film). Revision checks still prevent overwriting a stale copy.
            library._optical_operation(record, new, updates, revisions, allow_existing=True)
        if self.module_key != "materials" and bool(getattr(self.bridge, "dirty", False)):
            data = normalize_document(self.module_key, self.document_data())
            identity = self.currentDocumentId
            if identity:
                updates[identity] = data
                revisions[identity] = self.service._loaded_revisions.get(identity, self.service._record(identity)["sha256"])
            else:
                new.append((self.module_key, data))
        return new, updates, revisions

    def adopt_saved_transaction(self, created):
        material_bridge = self.bridge if self.module_key == "materials" else getattr(self.bridge, "_material_library", None)
        if material_bridge is not None and bool(material_bridge.dirty):
            record = material_bridge.draftData()
            material_bridge._saved_id = record["id"]
            material_bridge._selected_key = "user:" + record["id"]
            if hasattr(material_bridge, "_global_selected_id"):
                material_bridge._global_selected_id = None
            material_bridge._selected_revisions = material_bridge.project_library.revisions(record["id"])
            material_bridge._dirty = False
            material_bridge.reload()
        if self.module_key != "materials" and bool(getattr(self.bridge, "dirty", False)):
            identity = self.currentDocumentId
            if not identity:
                identity = next(identity for kind, identity in created if kind == self.module_key)
            saved = self.service.load_document(identity)
            self.adopt_saved(saved)
        self._remember_clean_state()
        self.changed.emit()

    def adopt_saved(self, saved):
        # A coordinated transaction replaces only the same owner after publish.
        was_frozen = bool(getattr(self.bridge, "_editing_frozen", False))
        self.set_editing_frozen(False)
        try:
            self.bridge._dirty = False
            self._open_managed(self.service._identity_for_path(saved.path))
        finally:
            self.set_editing_frozen(was_frozen)
        self.changed.emit()

    @Slot(QUrl, result=bool)
    def exportDocument(self, directory):
        try:
            identity = self.currentDocumentId
            if not identity:
                raise ProjectError("missing_document", "请先保存项目资源，再导出完整依赖。")
            from uuid import uuid4
            if self.module_key == "materials":
                destination = Path(directory.toLocalFile()) / ("material-export-" + uuid4().hex[:12] + ".json")
                result = self.bridge.project_library.export_single_file(self.bridge.currentId, destination)
                self._set_status("已导出完整材料包，可从全局或其他项目材料库明确导入：" + result["path"])
                return True
            destination = Path(directory.toLocalFile()) / ("resource-export-" + uuid4().hex[:12])
            result = self.service.store.export_document(identity, destination)
            self._set_status("已导出项目资源及完整依赖：" + result["destination"])
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._set_status(exc)
            return False

    @Slot(str, str, result=bool)
    def saveDefaultConfiguration(self, name, parameters_json):
        if self.module_key != "process":
            return False
        try:
            if self.commit_pending_edits() is False:
                raise ProjectError("uncommitted_edit", "Resource field could not be committed")
            from coat_sim.core.project_dependencies import parse_json
            parameters = parse_json(parameters_json.encode("utf-8"))
            if not isinstance(parameters, dict):
                raise ValueError("沉积计算参数须为 JSON 对象")
            identity, process_id = self.service.save_process_configuration(
                name, self.document_data(), parameters, process_id=self.currentDocumentId or None, make_default=True)
            self.adopt_saved(self.service.load_document(process_id))
            self._set_status("已保存工艺与沉积配置；下次打开工程将自动加载此配置：" + identity)
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._set_status(exc)
            return False

    @Slot(str, result=bool)
    def selectDefaultConfiguration(self, identity):
        try:
            self.service.select_default(identity or None)
            self._set_status("已更新项目默认沉积配置。")
            return True
        except (ValueError, TypeError, OSError) as exc:
            self._set_status(exc)
            return False

    def prepare_close_review(self):
        # A modal review blurs native fields and their display formatter may
        # replace invalid text. Keep the exact buffer until Save/Discard/Cancel.
        pending = self.snapshot_pending_edits()
        self._close_pending_ui = pending
        if pending is None or self._pending_ui is not None or self.window is None:
            return
        if pending["numeric_value_edited"]:
            try:
                if not math.isfinite(float(pending["text"].strip())):
                    return
            except (ValueError, OverflowError):
                return
        # Settle only syntactically committable text before the close signature
        # is captured. A modal's later focus-out must not look like a new edit.
        # Invalid input still reaches Discard/Cancel without save validation.
        field = _find_text_item(self.window, pending["object_name"])
        if field is not None:
            field.setProperty("text", pending["text"])
            field.setProperty("numericValueEdited", pending["numeric_value_edited"])
            QMetaObject.invokeMethod(field, "editingFinished", Qt.ConnectionType.DirectConnection)
        if not bool(getattr(self.bridge, "dirty", False)) and self._pending_information_edit is None:
            self._close_pending_ui = None  # A same-value commit has no unsaved change.

    def resume_close_review(self):
        if self._close_pending_ui is not None:
            self._pending_ui = self.snapshot_pending_edits()
            self._close_pending_ui = None
            QTimer.singleShot(0, self.restore_pending_edits)

    @Slot()
    def cancelCloseReview(self):
        if self._local_close_review:
            self._local_close_review = False
            self.resume_close_review()

    @Slot(result=bool)
    def requestClose(self):
        if self._transfer_applying:
            self._set_status("资源正在提交，请等待结果后关闭窗口。")
            return False
        self.prepare_close_review()
        if self.dirty:
            self._local_close_review = True
            self.closeDecisionRequested.emit()
            return False
        return True

    @Slot(str, result=bool)
    def confirmClose(self, decision):
        if decision == "cancel":
            self.cancelCloseReview()
            return False
        if self.window is None:
            return False
        if decision == "save" and not self.save_current():
            return False
        if decision not in {"save", "discard", "keep"}:
            return False
        self._local_close_review = False
        self.window.setProperty("projectCloseAuthorized", True)
        accepted = self.window.close()
        if accepted and decision == "discard":
            self._discard_closed_edits()
        elif not accepted:
            self.window.setProperty("projectCloseAuthorized", False)
        return True

    def set_editing_frozen(self, frozen):
        method = getattr(self.bridge, "setEditingFrozen", None) or getattr(self.bridge, "set_editing_frozen", None)
        if method is not None:
            method(frozen)
        nested = getattr(self.bridge, "_material_library", None)
        if nested is not None and hasattr(nested, "set_editing_frozen"):
            nested.set_editing_frozen(frozen)

    def snapshot(self):
        identity = self.currentDocumentId or None
        revision = None if identity is None else self.service._loaded_revisions.get(identity, self.service._record(identity)["sha256"])
        try:
            data = self.document_data()
            from coat_sim.core.project_dependencies import normalize_document
            normalize_document("material_profile" if self.module_key == "materials" else self.module_key, data)
        except (ValueError, TypeError, KeyError):
            data = None
        state = {}
        for name in EDITOR_STATE_FIELDS[self.module_key]:
            value = getattr(self.bridge, name)
            state[name] = sorted(value) if isinstance(value, set) else deepcopy(value)
        result = {"module_key": self.module_key, "document_id": identity, "revision": revision,
                  "dirty": bool(getattr(self.bridge, "dirty", False)), "data": deepcopy(data), "editor_state": state}
        if self._pending_information_edit is not None:
            information = deepcopy(self._pending_information_edit)
            information["model_dirty"] = bool(getattr(self.bridge, "dirty", False))
            result.update(pending_information=information, dirty=True)
        pending = self.snapshot_pending_edits()
        if pending is not None:
            result.update(pending_ui=pending, dirty=True, data=None)
        nested = getattr(self.bridge, "_material_library", None)
        if nested is not None and nested.dirty:
            material_state = {}
            for name in EDITOR_STATE_FIELDS["materials"]:
                value = getattr(nested, name)
                material_state[name] = sorted(value) if isinstance(value, set) else deepcopy(value)
            try:
                record = {"kind": "coat_sim.project_material_profile", "version": 1, "material": nested.draftData()}
            except (ValueError, TypeError, KeyError):
                record = None
            matching = next((row for row in self.service.list_documents("material_profile")
                             if self.service.load_document(row["id"]).data["material"]["id"] == nested._saved_id), None)
            result["nested_material"] = {"module_key": "materials", "document_id": None if matching is None else matching["id"],
                "revision": None if matching is None else nested._selected_revisions.get(matching["id"], matching["revision"]),
                "dirty": True, "data": record, "editor_state": material_state}
        return result

    def restore_recovery_snapshot(self, snapshot):
        """Keep persisted selection savepoints separate from recovered edits."""
        def saved_document(record):
            identity = record.get("document_id")
            if not identity:
                return None
            if self.service._record(identity)["sha256"] != record["revision"]:
                raise ProjectError("stale_document", "已保存资源已变化，请重新检查恢复内容。")
            return self.service.load_document(identity)

        saved = saved_document(snapshot)
        nested_record = snapshot.get("nested_material")
        nested_saved = saved_document(nested_record) if nested_record is not None else None
        previous = self.capture_recovery_state()
        try:
            if saved is not None:
                self._open_managed(snapshot["document_id"])
                if self.currentDocumentId != snapshot["document_id"]:
                    raise ProjectError("invalid_document", "无法读取恢复设计的已保存内容。")
            if nested_saved is not None:
                # ProjectMaterialLibraryBridge overrides this with a synchronous
                # checked loader; no late global-library worker can replace edits.
                nested = self.bridge._material_library
                if not nested._schedule_load("select", nested_saved.data["material"]):
                    raise ProjectError("invalid_document", "无法读取恢复材料的已保存内容。")
            self._remember_clean_state()
            self.restore_snapshot(snapshot)
        except Exception:
            self.rollback_recovery_state(previous)
            raise

    def capture_recovery_state(self):
        snapshot = self.snapshot()
        if "nested_material" not in snapshot and "nested_material" in self._clean_snapshot:
            snapshot["nested_material"] = deepcopy(self._clean_snapshot["nested_material"])
        return snapshot, deepcopy(self._clean_snapshot)

    def rollback_recovery_state(self, state):
        snapshot, clean = state
        self.restore_snapshot(snapshot)
        self._clean_snapshot = clean

    def restore_snapshot(self, snapshot):
        self._pending_ui = deepcopy(snapshot.get("pending_ui"))
        self._pending_information_edit = deepcopy(snapshot.get("pending_information"))
        for name, value in snapshot["editor_state"].items():
            setattr(self.bridge, name, set(value) if name == "_null_models" else deepcopy(value))
        if self.module_key == "source":
            self.bridge._fields.setdefault("name", "")
        # Older recovery records refer to a main-form metadata input that has
        # moved into Information. Preserve its exact text as an unsaved draft.
        if (self.module_key == "process" and self._pending_ui is not None
                and self._pending_ui["object_name"] == "processNameField"):
            self.bridge._data["name"] = self._pending_ui["text"]
            self._pending_ui = None
        self._restore_global_material_selection(self.bridge)
        if self.module_key == "materials":
            self.bridge.restoreAssetOpticalProjectionOrigin()
        identity = snapshot["document_id"]
        self.bridge._path = str(self.service.document_handle(identity)) if identity and self.module_key in {"machine", "mask"} else (
            self.service.document_handle(identity) if identity else None)
        if identity:
            self.service._loaded_revisions[identity] = snapshot["revision"]
        if hasattr(self.bridge, "_reload_origin"):
            self.bridge._reload_origin = self.bridge._path
        for pending_name in ("_pending_reload", "_pending_save_as", "_pending_document_replace", "_pending_action"):
            if hasattr(self.bridge, pending_name):
                setattr(self.bridge, pending_name, None)
        self.bridge._dirty = (snapshot["dirty"] if self._pending_information_edit is None
                              else self._pending_information_edit["model_dirty"])
        if self.module_key in {"source", "substrate"}:
            self.bridge._material_validated = self.bridge._material is not None
            if self.module_key == "source":
                self.bridge._sample_cache = None
            self.bridge._refresh_material_reference()
            self.bridge._refresh_validation()
        elif self.module_key == "process":
            self.bridge._machine_runtime = None
            self.bridge._machine_runtime_path = None
            self.bridge._machine_runtime_signature = None
            self.bridge._dependency_paths = {}
            self.bridge._refresh()
        else:
            self.bridge._validate()
        nested = snapshot.get("nested_material")
        if nested is not None:
            material_bridge = self.bridge._material_library
            for name, value in nested["editor_state"].items():
                setattr(material_bridge, name, set(value) if name == "_null_models" else deepcopy(value))
            self._restore_global_material_selection(material_bridge)
            material_bridge._dirty = nested["dirty"]
            material_bridge._validate()
            material_bridge.draftChanged.emit()
        signal = getattr(self.bridge, "documentChanged", None)
        if signal is not None:
            signal.emit()
        self.changed.emit()
        QTimer.singleShot(0, self.restore_pending_edits)

    @staticmethod
    def _restore_global_material_selection(bridge):
        """Rehydrate global selection from stable keys, including old recovery files."""
        keys = getattr(bridge, "_global_material_keys", {})
        if not hasattr(bridge, "_global_selected_id"):
            return
        selected = getattr(bridge, "_selected_key", "")
        bridge._global_selected_id = next(
            (identity for identity, key in keys.items() if key == selected), None)

    def detach_view(self):
        self.cancelTransfer()
        self.window = None
        self.resource_workspace.windows.pop(self.module_key, None)

    def dispose(self):
        if self._transfer_applying:
            self._dispose_after_transfer = True
            return
        if self._disposed:
            return
        self._disposed = True
        self.cancelTransfer()
        self.service.close_resource_exchange()
        if hasattr(self.bridge, "detach_observers"):
            self.bridge.detach_observers()
        if hasattr(self.bridge, "_cancel_background_work"):
            self.bridge._cancel_background_work()
        self.detach_view()
        self.deleteLater()



class EditorResourceInformationController(QObject):
    """The same information dialog for independent, non-project editors.

    The bridge remains the unique draft/file owner. This facade never saves or
    manufactures a project identity, and follows the bridge's edit capabilities
    including its existing editable built-in material override behavior.
    """

    changed = Signal()

    def __init__(self, bridge, module_key, parent=None):
        super().__init__(parent or bridge)
        self.bridge, self.module_key = bridge, module_key
        self._transfer_busy = False
        self._pending_information_edit = None
        for name in ("changed", "documentChanged", "draftChanged", "dirtyChanged"):
            signal = getattr(bridge, name, None)
            if signal is not None:
                signal.connect(self.changed)

    @property
    def currentDocumentId(self):
        return str(getattr(self.bridge, "_path", None) or getattr(self.bridge, "currentId", ""))

    @Property(dict, notify=changed)
    def resourceInformation(self):
        return _editor_resource_information(self)

    @Property(dict, notify=changed)
    def pendingInformationEdit(self):
        return deepcopy(self._pending_information_edit or {})

    @Slot(str, str, str, str, result=bool)
    def stageInformationEdit(self, name, summary, notes, expected_token):
        return _stage_information_edit(self, name, summary, notes, expected_token)

    @Slot()
    def clearInformationEdit(self):
        self._pending_information_edit = None
        self.changed.emit()

    @Property(str, notify=changed)
    def status(self):
        return str(getattr(self.bridge, "status", ""))

    @Property(bool, constant=True)
    def transferBusy(self):
        return False

    def _set_status(self, value):
        method = getattr(self.bridge, "_set_status", None)
        if callable(method):
            method(value)
        else:
            self.bridge._status = str(value)
            self.changed.emit()

    @Slot(str, str, str, str, result=bool)
    def updateResourceInformation(self, name, summary, notes, expected_token):
        result = _update_editor_resource_information(self, name, summary, notes, expected_token)
        if result:
            self.clearInformationEdit()
        return result


def dispose_project_resource_controller(controller, *, discard=False):
    if discard:
        controller.dispose()
    else:
        controller.detach_view()


def create_project_resource_view(workspace, module_key: str, engine: QQmlApplicationEngine,
                                 parent: QObject | None = None, *, geometry_settings=None,
                                 navigation=None) -> tuple[Any, ProjectResourceController, QQmlContext, QQmlComponent]:
    if module_key not in QML_FILES:
        raise ValueError("Unsupported project resource module")
    resources = ProjectResourceWorkspace.for_project(workspace.host, workspace.binding)
    if not resources._default_initialized:
        resources.initialize_default(load_project_default(resources.store))
    controller = resources.controllers.get(module_key)
    if controller is None:
        controller = ProjectResourceController(workspace, resources, module_key, parent, navigation)
        resources.controllers[module_key] = controller
    else:
        controller.project_navigation = navigation
    context = QQmlContext(engine.rootContext(), engine)
    context.setContextProperty("bridge", controller.bridge)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML_ROOT / QML_FILES[module_key])))
    window: Any = component.createWithInitialProperties({"projectNavigation": navigation, "projectResources": controller}, context)
    if window is None:
        raise RuntimeError("\n".join(error.toString() for error in component.errors()))
    controller.window = window
    if not controller.dirty:
        controller._remember_clean_state()
    window.visibleChanged.connect(lambda: QTimer.singleShot(0, controller.restore_pending_edits))
    resources.windows[module_key] = window
    controller.restore_pending_edits()
    window.setIcon(window_icon(module_key if module_key != "materials" else "source"))
    from .window_geometry import prepare_window_geometry
    prepare_window_geometry(window, module_key, settings=geometry_settings)
    return window, controller, context, component
