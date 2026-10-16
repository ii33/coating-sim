"""Qt Quick bridges for the machine and correction-mask editors.

The editors keep the document in memory and delegate file I/O and schema
validation to ``CoatService``.  Field edits use JSON pointers so that the UI
does not bind to generated IDs or to a particular source ordering.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import fields
from functools import wraps
from pathlib import Path
from threading import Thread, get_ident
from time import perf_counter
from typing import Any, TYPE_CHECKING, cast

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

from coat_sim.ui.icons import window_icon

from coat_sim.core.design_files import default_design_path, validate_design_path
from coat_sim.core.api import CoatService, Document
from coat_sim.core.data_files import design_directory

if TYPE_CHECKING:
    from coat_sim.core.project_resource_service import ProjectResourceService


def _local_path(value: str) -> Path:
    value = value.strip()
    if value.startswith("file:"):
        value = QUrl(value).toLocalFile()
    if not value:
        raise ValueError("请选择文件路径")
    return Path(value).expanduser().resolve()


def _json_data(value: Any) -> str:
    def normalized(item: Any) -> Any:
        if hasattr(item, "tolist"):
            return normalized(item.tolist())
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if isinstance(item, dict):
            return {key: normalized(part) for key, part in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalized(part) for part in item]
        return item

    return json.dumps(normalized(value), ensure_ascii=False, indent=2, allow_nan=False)


def _pointer_tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("字段路径必须是 JSON 指针")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _at_pointer(data: Any, pointer: str) -> Any:
    current = data
    for part in _pointer_tokens(pointer):
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def _parent_at_pointer(data: Any, pointer: str) -> tuple[Any, str]:
    parts = _pointer_tokens(pointer)
    if not parts:
        raise ValueError("不能直接替换根文档")
    current = data
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current, parts[-1]


def _result_ok(result: Any) -> bool:
    if isinstance(result, bool):
        return result
    for name in ("ok", "valid", "is_valid", "success"):
        if hasattr(result, name):
            value = getattr(result, name)
            return bool(value() if callable(value) else value)
    errors = getattr(result, "errors", None)
    if errors is not None:
        return not bool(errors)
    return True


def _result_message(result: Any) -> str:
    errors = getattr(result, "errors", None)
    if errors:
        return "\n".join(str(item) for item in errors)
    if isinstance(result, str):
        return result
    return ""


def _project_edit(denied_result=None):
    """Block project editor writebacks while a session is frozen for exit."""
    def decorate(method):
        @wraps(method)
        def guarded(self, *args, **kwargs):
            if self._project_mutations_frozen():
                self._set_status("项目正在关闭或被冻结，当前修改未应用。")
                return denied_result
            return method(self, *args, **kwargs)
        return guarded
    return decorate


def _checked_reload_document(service: CoatService, kind: str, path: str | Path):
    """Read and check the complete graph before replacing any editor state."""
    if Path(path).suffix.lower() == ".coatasset":
        from coat_sim.core.input_snapshot import runtime_from_snapshot
        content = service.read_simulation_asset(path, detail="full")
        runtime = runtime_from_snapshot(content["configuration"]["input_snapshot"])
        matches = [row for row in content.get("designs", []) if row["kind"] == kind]
        if len(matches) != 1:
            raise ValueError("资产包中该类型设计不唯一，请选择后导出打开")
        document = Document(kind, str(path), deepcopy(matches[0]["data"]), (),
                            runtime if kind == "process" else runtime["machine"] if kind == "machine" else None)
    else:
        document = service.load(kind, path)
    if not _result_ok(document):
        raise ValueError(_result_message(document) or "依赖或参数无效")
    if not isinstance(document.data, dict):
        raise ValueError("文件根节点必须是对象")
    # Packages resolve their dependencies internally; never interpret their
    # portable references relative to the package's host directory.
    validation_path = None if Path(path).suffix.lower() == ".coatasset" else document.path
    result = service.validate(kind, document.data, path=validation_path)
    if not _result_ok(result):
        raise ValueError(_result_message(result) or "依赖或参数无效")
    return document


class DocumentBridge(QObject):

    @Property(QObject, constant=True)
    def informationController(self):
        if not hasattr(self, "_information_controller"):
            from .project_resource_adapter import EditorResourceInformationController
            self._information_controller = EditorResourceInformationController(self, self._kind, self)
        return self._information_controller

    _loaded_document: Document | None
    documentChanged = Signal()
    pathChanged = Signal()
    statusChanged = Signal()
    dirtyChanged = Signal()
    validationChanged = Signal()
    overwriteRequested = Signal(QUrl)
    reloadRequested = Signal()
    reloadStateChanged = Signal()
    documentReplaceRequested = Signal()
    editingFrozenChanged = Signal()

    @Property(bool, notify=editingFrozenChanged)
    def editingFrozen(self):
        return self._project_mutations_frozen()

    @Slot(bool)
    def setEditingFrozen(self, frozen: bool) -> None:
        self._editing_frozen = bool(frozen)
        self.editingFrozenChanged.emit()

    def _project_mutations_frozen(self) -> bool:
        if not getattr(self._service, "is_project_service", False):
            return False
        gate = getattr(self._service, "_gate", None)
        gate_frozen = getattr(gate, "frozen", False) if gate is not None else False
        if callable(gate_frozen):
            gate_frozen = gate_frozen()
        close_save_threads: set[int] = getattr(gate, "close_save_threads", set()) if gate is not None else set()
        coordinated_save = get_ident() in close_save_threads
        return bool(getattr(self, "_editing_frozen", False) or (gate_frozen and not coordinated_save))

    @Property(QObject, constant=True)
    def assetBrowser(self):
        return self._asset_browser

    @Property(QObject, constant=True)
    def projectNavigation(self):
        return self._project_navigation

    @Property(QUrl, constant=True)
    def defaultDirectory(self):
        if getattr(self._service, "is_project_service", False):
            return QUrl.fromLocalFile(str(cast("ProjectResourceService", self._service).default_directory(self._kind)))
        return QUrl.fromLocalFile(str(design_directory(self._kind)))

    @Slot(str, result=QUrl)
    def dependencyDirectory(self, pointer):
        if getattr(self._service, "is_project_service", False):
            return QUrl.fromLocalFile(str(cast("ProjectResourceService", self._service).default_directory(self._kind)))
        return QUrl.fromLocalFile(str(design_directory("machine" if pointer == "machine" else "source" if "sources" in pointer else "mask")))

    @Property(str, notify=documentChanged)
    def description(self):
        return self._data.get("description", "")

    @Property(str, notify=documentChanged)
    def notes(self):
        return self._data.get("notes", "")

    @Slot(str, str)
    @_project_edit()
    def setMetadata(self, key, value):
        if key in ("description", "notes"):
            self.setValue("/" + key, json.dumps(value, ensure_ascii=False))

    def __init__(self, kind: str, service: CoatService | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._kind = kind
        # The user explicitly allows the desktop file pickers to open/save anywhere.
        # This applies only to the desktop editor; the core service default stays bounded.
        self._service = service or CoatService(restrict_to_root=False)
        self._editing_frozen = False
        self._project_navigation = getattr(self._service, "project_navigation", None)
        from .asset_browser import AssetBrowserBridge
        import weakref
        weak_owner = weakref.ref(self)
        def open_asset(path):
            editor = weak_owner()
            if editor is not None:
                editor.loadDocument(str(path))
        self._asset_browser = AssetBrowserBridge(self._service, self._kind, open_asset, self)
        self._data: dict[str, Any] = {}
        self._path = ""
        self._status = "请打开或新建文件"
        self._dirty = False
        self._can_save = False
        self._validation = ""
        self._pending_save_as = None
        self._managed_dependency_paths: dict[str, str] = {}
        self._pending_document_replace: tuple[str, Any, str, dict[str, Any]] | None = None
        self._project_replace_bypass = False
        self._reload_origin = ""
        self._pending_reload = None
        self._reloading = False
        self._package_runtime = None
        self.documentChanged.connect(self._cancel_pending_reload)

    @Property(str, notify=documentChanged)
    def documentJson(self) -> str:
        return _json_data(self._data)

    @Property(str, notify=pathChanged)
    def documentPath(self) -> str:
        return self._path

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property(bool, notify=dirtyChanged)
    def dirty(self) -> bool:
        return self._dirty

    @Property(bool, notify=reloadStateChanged)
    def canReload(self) -> bool:
        return bool(self._reload_origin)

    def _cancel_pending_reload(self) -> None:
        self._pending_reload = None

    def _defer_project_replace(self, action: str, value=None) -> bool:
        if (getattr(self._service, "is_project_service", False)
                and self._dirty and not self._project_replace_bypass):
            self._pending_document_replace = (
                action, value, self._path, deepcopy(self._data))
            self.documentReplaceRequested.emit()
            self._set_status("当前有未保存修改；请确认后再替换设计。")
            return True
        return False

    @Slot(bool, result=bool)
    @_project_edit(False)
    def confirmDocumentReplace(self, confirmed: bool) -> bool:
        pending, self._pending_document_replace = self._pending_document_replace, None
        if not confirmed or pending is None:
            return False
        action, value, old_path, old_data = pending
        if old_path != self._path or old_data != self._data:
            self._set_status("设计已更改，请重新确认替换。")
            return False
        self._project_replace_bypass = True
        try:
            if action == "new":
                self.newDocument(value)
            elif action == "open":
                self.loadDocument(str(value))
        finally:
            self._project_replace_bypass = False
        return True

    def _reload_identity(self):
        return (self._reload_origin,)

    @Slot(result=bool)
    @_project_edit(False)
    def reloadCurrent(self) -> bool:
        if not self.canReload:
            self._set_status("请先打开或保存文件，再刷新。")
            return False
        if self._dirty and self._reload_origin:
            pending = (self._reload_identity(), deepcopy(self._data))
            if pending != self._pending_reload:
                self._pending_reload = pending
                self.reloadRequested.emit()
            return False
        self._pending_reload = None
        return self._reload_now()

    @Slot(bool, result=bool)
    @_project_edit(False)
    def confirmReload(self, confirmed: bool) -> bool:
        pending, self._pending_reload = self._pending_reload, None
        if not confirmed or pending is None:
            return False
        if pending != (self._reload_identity(), self._data):
            self._set_status("文件或设计已更改，请重新刷新。")
            return False
        return self._reload_now()

    def _reload_now(self) -> bool:
        try:
            document = _checked_reload_document(self._service, self._kind, self._reload_origin)
            self._reloading = True
            self._package_runtime = document.runtime if Path(self._reload_origin).suffix.lower() == ".coatasset" else None
            self._set_document(deepcopy(document.data), str(document.path), False)
            self._set_status(f"已刷新：{Path(self._reload_origin).name}")
            return True
        except Exception as exc:
            self._set_status(f"刷新失败，已保留当前内容：{exc}")
            return False
        finally:
            self._reloading = False

    @Property(bool, notify=validationChanged)
    def canSave(self) -> bool:
        return self._can_save

    @Property(str, notify=validationChanged)
    def validationMessage(self) -> str:
        return self._validation

    def _set_status(self, message: str) -> None:
        self._status = message
        self.statusChanged.emit()

    def applyDocument(self, data: dict[str, Any], intended_path: str = "") -> None:
        """Fill a checked automation draft through the existing editor signals."""
        if self._project_mutations_frozen():
            self._set_status("项目正在关闭或被冻结，当前修改未应用。")
            return
        self._reload_origin = ""
        self._package_runtime = None
        self._set_document(deepcopy(data), intended_path, True)
        self._set_status("已填入设计，请确认后保存。")

    def _set_document(self, data: dict[str, Any], path: str, dirty: bool) -> None:
        self._pending_save_as = None
        self._data = data
        self._path = path
        self._dirty = dirty
        self.documentChanged.emit()
        self.pathChanged.emit()
        self.dirtyChanged.emit()
        self.reloadStateChanged.emit()
        self._validate()

    def _validate(self) -> None:
        try:
            if self._path and Path(self._path).suffix.lower() == ".coatasset":
                self._can_save = False
                self._validation = "资产包设计请先在资产包选择器中导出，再保存修改。"
                self.validationChanged.emit()
                return
            result = self._service.validate(self._kind, self._data, path=self._path or None)
            self._can_save = _result_ok(result)
            self._validation = _result_message(result)
        except Exception as exc:
            self._can_save = False
            self._validation = str(exc)
        self.validationChanged.emit()

    @Slot(str)
    @_project_edit()
    def loadDocument(self, path: str) -> None:
        if self._defer_project_replace("open", path):
            return
        self._pending_save_as = None
        try:
            local = _local_path(path)
            document = (_checked_reload_document(self._service, self._kind, local)
                        if local.suffix.lower() == ".coatasset" else self._service.load(self._kind, local))
            data = document.data
            if not isinstance(data, dict):
                raise ValueError("文件根节点必须是对象")
            managed_path = str(document.path)
            self._reload_origin = managed_path
            self._package_runtime = document.runtime if local.suffix.lower() == ".coatasset" else None
            self._set_document(data, managed_path, False)
            self._set_status(f"已打开：{Path(managed_path).name}")
        except Exception as exc:
            self._set_status(f"打开失败：{exc}")

    @Slot(str)
    @_project_edit()
    def openDocument(self, path: str) -> None:
        """Open an explicit path requested by the shared window launcher."""
        self.loadDocument(path)

    @Slot(str, str, result=str)
    def dependencyPath(self, pointer: str, reference: str) -> str:
        if self._kind != "machine" or not reference:
            return ""
        child_kind = "source" if pointer.startswith("/sources/") else "mask"
        try:
            managed = self._managed_dependency_paths.get(reference)
            if managed:
                return str(validate_design_path(child_kind, Path(managed)))
            owner = (Path(self._path) if self._path else
                     (Path(cast("ProjectResourceService", self._service).default_directory("machine")) / "new_machine.cmac"
                      if getattr(self._service, "is_project_service", False)
                      else design_directory("machine") / "new_machine.cmac"))
            absolute = Path(self._service.resolve_reference(owner, reference))
            return str(validate_design_path(child_kind, absolute))
        except (OSError, TypeError, ValueError):
            return ""

    @Slot(str)
    @_project_edit()
    def initializeDocument(self, json_text: str) -> None:
        """Bind an untouched empty editor to its defaults without a user edit."""
        if self._data or self._path or self._dirty:
            return
        try:
            data = json.loads(json_text)
            if not isinstance(data, dict):
                raise ValueError("文件根节点必须是对象")
            self._set_document(data, "", False)
            self._set_status("未命名方案")
        except (ValueError, TypeError) as exc:
            self._set_status(f"初始化失败：{exc}")

    @Slot(str)
    @_project_edit()
    def newDocument(self, json_text: str) -> None:
        if self._defer_project_replace("new", json_text):
            return
        self._pending_save_as = None
        self._managed_dependency_paths.clear()
        try:
            data = json.loads(json_text)
            if not isinstance(data, dict):
                raise ValueError("文件根节点必须是对象")
            self._reload_origin = ""
            self._package_runtime = None
            self._set_document(data, "", True)
            self._set_status("未命名方案")
        except Exception as exc:
            self._set_status(f"新建失败：{exc}")

    @Slot(str, str)
    @_project_edit()
    def setValue(self, pointer: str, json_value: str) -> None:
        self._pending_save_as = None
        try:
            value = json.loads(json_value)
            updated = deepcopy(self._data)
            parent, key = _parent_at_pointer(updated, pointer)
            if isinstance(parent, list):
                parent[int(key)] = value
            else:
                parent[key] = value
            if self._kind == "machine" and pointer in (
                "/gear_module_mm", "/planet_offset_mm", "/sun_teeth", "/planet_teeth"
            ):
                sun = int(updated.get("sun_teeth", 0))
                planet = int(updated.get("planet_teeth", 0))
                module = float(updated.get("gear_module_mm", 0))
                offset = float(updated.get("planet_offset_mm", 0))
                if sun > 0 and planet > 0:
                    if pointer == "/planet_offset_mm":
                        updated["gear_module_mm"] = self._service.convert_gear(
                            sun, planet, center_distance_mm=offset)["gear_module_mm"]
                    elif module > 0:
                        updated["planet_offset_mm"] = self._service.convert_gear(
                            sun, planet, module_mm=module)["planet_offset_mm"]
            if updated == self._data:
                # Keep the setter acceptance identity used by the bounded
                # editor adapter, without marking an unchanged value dirty.
                self._data = updated
                return
            self._data = updated
            self._dirty = True
            self.dirtyChanged.emit()
            self.documentChanged.emit()
            self._validate()
        except Exception as exc:
            self._set_status(f"修改失败：{exc}")

    @Slot(str, str)
    @_project_edit()
    def setDependencyFile(self, pointer: str, path: str) -> None:
        """Store a selected machine dependency relative to the machine file."""
        try:
            if self._kind != "machine":
                raise ValueError("只有机器文件包含源和修正板依赖")
            child_kind = "source" if pointer.startswith("/sources/") else "mask"
            local = validate_design_path(child_kind, _local_path(path))
            if getattr(self._service, "is_project_service", False):
                owner = self._path or str(Path(cast("ProjectResourceService", self._service).default_directory("machine")) / "new_machine.cmac")
                reference = self._service.relative_reference(owner, local)
                self._managed_dependency_paths[reference] = str(
                    self._service.resolve_reference(owner, reference))
            else:
                reference = (self._service.relative_reference(self._path, local)
                             if self._path else str(local))
            self.setValue(pointer, _json_data(reference))
        except Exception as exc:
            self._set_status(f"选择文件失败：{exc}")

    @Slot(str, str)
    @_project_edit()
    def appendItem(self, pointer: str, json_value: str) -> None:
        self._pending_save_as = None
        try:
            collection = _at_pointer(self._data, pointer)
            if not isinstance(collection, list):
                raise ValueError("目标字段不是列表")
            collection.append(json.loads(json_value))
            self._dirty = True
            self.dirtyChanged.emit()
            self.documentChanged.emit()
            self._validate()
        except Exception as exc:
            self._set_status(f"添加失败：{exc}")

    @Slot(str, int)
    @_project_edit()
    def removeItem(self, pointer: str, index: int) -> None:
        self._pending_save_as = None
        try:
            collection = _at_pointer(self._data, pointer)
            if not isinstance(collection, list):
                raise ValueError("目标字段不是列表")
            collection.pop(index)
            self._dirty = True
            self.dirtyChanged.emit()
            self.documentChanged.emit()
            self._validate()
        except Exception as exc:
            self._set_status(f"删除失败：{exc}")

    @Slot()
    @_project_edit()
    def saveDocument(self) -> None:
        self._pending_save_as = None
        if not self._path:
            self._set_status("请先使用另存为选择路径")
            return
        self._save(Path(self._path), overwrite=True)

    @Slot(str)
    @_project_edit()
    def saveDocumentAs(self, path: str) -> None:
        self._pending_save_as = None
        try:
            local = default_design_path(self._kind, _local_path(path))
            managed_existing = bool(getattr(self._service, "is_managed_path", lambda _p: False)(local))
            existing = (managed_existing if getattr(self._service, "is_project_service", False)
                        else local.exists())
            if existing:
                self._service.load(self._kind, local)  # Reject another design type before asking.
                self._pending_save_as = local
                self.overwriteRequested.emit(QUrl.fromLocalFile(str(local)))
                return
            self._save(local, overwrite=False)
        except Exception as exc:
            self._set_status(f"另存为失败，原文件未覆盖；请使用其他文件名：{exc}")

    @Slot(bool)
    @_project_edit()
    def confirmSaveAs(self, confirmed: bool) -> None:
        target, self._pending_save_as = self._pending_save_as, None
        if confirmed and target is not None:
            self._save(target, overwrite=True)

    def _save(self, path: Path, *, overwrite: bool) -> None:
        try:
            payload = deepcopy(self._data)
            if (self._kind == "machine" and self._path != str(path)
                    and not getattr(self._service, "is_project_service", False)):
                old_owner = self._path or str(Path.cwd() / "new_machine.cmac")
                for collection in ("sources", "masks"):
                    for item in payload.get(collection, []):
                        reference = str(item.get("file", ""))
                        if not reference:
                            continue
                        absolute = self._service.resolve_reference(old_owner, reference)
                        item["file"] = self._service.relative_reference(path, absolute)
            result = self._service.validate(self._kind, payload, path=path)
            if not _result_ok(result):
                raise ValueError(_result_message(result) or "依赖或参数无效")
            document = self._service.save(self._kind, path, payload, overwrite=overwrite)
            saved = getattr(document, "data", payload)
            if self._kind == "machine":
                self._loaded_document = document
                self._loaded_runtime = getattr(document, "runtime", None)
                self._loaded_runtime_signature = None
                self._planes_key = None
            saved_path = str(getattr(document, "path", path))
            self._reload_origin = saved_path
            self._package_runtime = None
            self._set_document(saved, saved_path, False)
            self._set_status(f"已保存：{Path(saved_path).name}")
        except Exception as exc:
            self._set_status(f"保存失败：{exc}")


class MachineBridge(DocumentBridge):
    planesChanged = Signal()

    def __init__(self, service: CoatService | None = None, parent: QObject | None = None) -> None:
        super().__init__("machine", service, parent)
        self._planes: list[dict[str, Any]] = []
        self._planes_key = None
        self._preview_requested = False
        self._loaded_document = None
        self._loaded_runtime = None
        self._loaded_runtime_signature = None
        self.documentChanged.connect(self._refresh_planes)
        self.pathChanged.connect(self._refresh_planes)

    @Slot(str)
    @_project_edit()
    def loadDocument(self, path: str) -> None:
        """Load the machine and reuse that dependency validation in the editor."""
        self._pending_save_as = None
        was_requested = self._preview_requested
        self._preview_requested = False
        try:
            if self._defer_project_replace("open", path):
                self._preview_requested = was_requested
                return
            local = _local_path(path)
            document = (_checked_reload_document(self._service, self._kind, local)
                        if local.suffix.lower() == ".coatasset"
                        else self._service.load(self._kind, local))
            data = document.data
            if not isinstance(data, dict):
                raise ValueError("文件根节点必须是对象")
            managed_path = str(document.path)
            self._reload_origin = managed_path
            self._package_runtime = document.runtime if local.suffix.lower() == ".coatasset" else None
            self._loaded_document = document
            self._loaded_runtime = document.runtime
            self._planes_key = None
            self._managed_dependency_paths = {}
            self._set_document(data, managed_path, False)
            self._loaded_runtime_signature = self._plane_reference_key()
            self._preview_requested = was_requested
            if was_requested:
                self._refresh_planes()
            self._set_status(f"已打开：{Path(managed_path).name}")
        except Exception as exc:
            self._loaded_document = None
            self._loaded_runtime = None
            self._loaded_runtime_signature = None
            self._preview_requested = was_requested
            self._set_status(f"打开失败：{exc}")

    def _reload_now(self) -> bool:
        """Reload the document and retain the exact validation/runtime snapshot."""
        was_requested = self._preview_requested
        try:
            local = _local_path(self._reload_origin)
            document = _checked_reload_document(self._service, self._kind, local)
            data = deepcopy(document.data)
            self._preview_requested = False
            self._package_runtime = (
                document.runtime if local.suffix.lower() == ".coatasset" else None)
            self._loaded_document = document
            self._loaded_runtime = document.runtime
            self._planes_key = None
            self._set_document(data, str(local), False)
            self._loaded_runtime_signature = self._plane_reference_key()
            self._preview_requested = was_requested
            if was_requested:
                self._refresh_planes()
            self._set_status(f"已刷新：{local.name}")
            return True
        except Exception as exc:
            self._preview_requested = was_requested
            self._set_status(f"刷新失败，已保留当前数据：{exc}")
            return False

    def _validate(self) -> None:
        document, self._loaded_document = self._loaded_document, None
        if document is None:
            super()._validate()
            return
        if self._path and Path(self._path).suffix.lower() == ".coatasset":
            self._can_save = False
            self._validation = "资产包设计请先在资产包选择器中导出，再保存修改。"
        else:
            self._can_save = _result_ok(document)
            self._validation = _result_message(document)
        self.validationChanged.emit()

    @Property(str, notify=planesChanged)
    def maskPlanesJson(self) -> str:
        return _json_data(self._planes)

    def _plane_reference_key(self):
        machine_path = Path(self._path).resolve() if self._path else None
        base = machine_path.parent if machine_path else Path.cwd()
        references = tuple(str(item.get("file", "")) for item in self._data.get("masks", []))
        package = id(self._package_runtime) if self._package_runtime is not None else None
        if getattr(self._service, "is_project_service", False):
            managed_paths = [str(machine_path)] if machine_path is not None else []
            if machine_path is not None and package is None:
                for reference in references:
                    if not reference:
                        continue
                    try:
                        managed_paths.append(str(self._service.resolve_reference(machine_path, reference)))
                    except (OSError, TypeError, ValueError):
                        managed_paths.append(self._managed_dependency_paths.get(reference, reference))
            return str(base), references, package, tuple(managed_paths)
        paths = [machine_path] if machine_path is not None else []
        if machine_path is not None and package is None:
            for reference in references:
                if not reference:
                    continue
                try:
                    paths.append(Path(self._service.resolve_reference(machine_path, reference)))
                except (OSError, TypeError, ValueError):
                    path = Path(reference)
                    paths.append(path if path.is_absolute() else base / path)
        stamps: list[tuple[str, int | None, int | None, int | None]] = []
        for path in paths:
            try:
                resolved = path.resolve()
                info = resolved.stat()
                stamps.append((str(resolved), info.st_mtime_ns, info.st_ctime_ns,
                               info.st_size))
            except (OSError, RuntimeError, ValueError):
                stamps.append((str(path), None, None, None))
        return str(base.resolve()), references, package, tuple(stamps)

    @Slot()
    def preparePreview(self) -> None:
        """Populate the geometry view after the editor's first frame."""
        self._preview_requested = True
        self._refresh_planes()

    @Slot()
    def refreshPreview(self) -> None:
        self._preview_requested = True
        self._refresh_planes(force=True)

    def _refresh_planes(self, *, force: bool = False) -> None:
        if not self._preview_requested:
            return
        key = self._plane_reference_key()
        if self._package_runtime is not None and (force or self._loaded_runtime_signature != key):
            if not self._dirty and self._reload_now():
                return
            self._planes = [{"slot": slot, "error": "资产包已变化，请重新载入以更新几何预览。"}
                            for slot, _item in enumerate(
                                self._data.get("masks", []), start=1)]
            self._planes_key = key
            self.planesChanged.emit()
            return
        if not force and key == self._planes_key:
            return
        planes: list[dict[str, Any]] = []
        base = Path(self._path).parent if self._path else Path.cwd()
        use_loaded_runtime = not force and (
            isinstance(self._loaded_runtime, dict)
            and self._loaded_runtime_signature == key)
        for slot, item in enumerate(self._data.get("masks", []), start=1):
            reference = str(item.get("file", ""))
            if not reference:
                planes.append({"slot": slot, "error": "尚未选择修正板"})
                continue
            if getattr(self._service, "is_project_service", False):
                try:
                    managed_path = (self._managed_dependency_paths.get(reference)
                                    or (self._service.resolve_reference(self._path, reference)
                                        if self._path else None))
                    if managed_path is None:
                        managed_path = self._service.load("mask", reference).path
                    path = Path(managed_path)
                except Exception as exc:
                    planes.append({"slot": slot, "error": str(exc)})
                    continue
            else:
                path = Path(reference)
                path = path if path.is_absolute() else base / path
            try:
                if self._package_runtime is not None:
                    installed = next(entry for entry in self._package_runtime["masks"]
                                     if entry["file"] == reference)
                    profile = installed["profile"]
                elif use_loaded_runtime:
                    installed = next(entry for entry in (self._loaded_runtime or {}).get("masks", [])
                                     if entry["file"] == reference)
                    profile = installed["profile"]
                else:
                    profile = self._service.load("mask", path).data
                planes.append({"slot": slot, "display_slot": slot, "file": path.name,
                               "z_mm": float(profile["z_mm"]),
                               "outer": deepcopy(profile["outer"]),
                               "holes": deepcopy(profile.get("holes", []))})
            except Exception as exc:
                planes.append({"slot": slot, "error": str(exc)})
        self._planes = planes
        self._planes_key = key
        self.planesChanged.emit()


class MaskBridge(DocumentBridge):
    optimizationChanged = Signal()
    optimizationInputsChanged = Signal()
    maskOverwriteRequested = Signal(QUrl, int)
    viewFitRequested = Signal()
    machineChanged = Signal()
    ringsChanged = Signal()
    weightsChanged = Signal()
    pointWeightsChanged = Signal()
    fieldChanged = Signal()
    workpieceDiameterChanged = Signal()
    estimateChanged = Signal()
    busyChanged = Signal()
    _weightsReady = Signal(int, str, float)
    _weightsFailed = Signal(int, str)
    _pointWeightsReady = Signal(int, str, float)
    _pointWeightsFailed = Signal(int, str)
    _fieldReady = Signal(int, str, float)
    _fieldFailed = Signal(int, str)
    cadChanged = Signal()
    cadFinished = Signal(str)
    _cadReady = Signal(int, int, str, object)
    _cadFailed = Signal(int, str)

    def __init__(self, service: CoatService | None = None, parent: QObject | None = None) -> None:
        super().__init__("mask", service, parent)
        self._machine: dict[str, Any] = {}
        self._machine_path = ""
        self._rings: list[dict[str, Any]] = []
        self._weights: dict[str, Any] = {}
        self._point_weights: dict[str, Any] = {}
        self._field: dict[str, Any] = {}
        self._workpiece_diameter_mm = 300.0
        self._source_sample_counts: list[int] = []
        self._seconds_per_work_unit: dict[str, float] = {}
        self._estimate_revision = 0
        self._radial_busy = False
        self._field_busy = False
        self._point_busy = False
        self._request_id = 0
        self._point_request_id = 0
        self._field_request_id = 0
        self._last_ring_z: float | None = None
        self._cad_busy = False
        self._cad_request_id = 0
        self._cad_revision = 0
        self._cad_warnings: list[str] = []
        self._cad_provenance: dict[str, Any] = {}
        self._cad_geometry = None
        self._cadReady.connect(self._accept_cad)
        self._cadFailed.connect(self._reject_cad)
        self.documentChanged.connect(self._document_modified)
        self._weightsReady.connect(self._accept_weights)
        self._weightsFailed.connect(self._reject_weights)
        self._pointWeightsReady.connect(self._accept_point_weights)
        self._pointWeightsFailed.connect(self._reject_point_weights)
        self._fieldReady.connect(self._accept_field)
        self._fieldFailed.connect(self._reject_field)
        self._optimization_coordinator = None
        if getattr(self._service, "is_project_service", False):
            from .mask_optimization import MaskOptimizationCoordinator
            self._optimization_coordinator = MaskOptimizationCoordinator(self._service, self)
            self._optimization_coordinator.changed.connect(self.optimizationInputsChanged)
            self._optimization_coordinator.optimizationChanged.connect(self.optimizationChanged)
            self._optimization_coordinator.maskOverwriteRequested.connect(self.maskOverwriteRequested)

    @Property(dict, notify=optimizationChanged)
    def optimization(self) -> dict:
        return {} if self._optimization_coordinator is None else self._optimization_coordinator.get_optimization()

    @Property(list, notify=optimizationChanged)
    def pendingMasks(self) -> list:
        return [] if self._optimization_coordinator is None else self._optimization_coordinator.get_pendingMasks()

    @Property(list, notify=optimizationInputsChanged)
    def optimizationMasks(self) -> list:
        return [] if self._optimization_coordinator is None else self._optimization_coordinator.get_optimizationMasks()

    @Property(int, notify=optimizationInputsChanged)
    def optimizationMaskCount(self) -> int:
        return 0 if self._optimization_coordinator is None else self._optimization_coordinator.get_optimizationMaskCount()

    @Property(int, notify=optimizationInputsChanged)
    def optimizationSourceCount(self) -> int:
        return 0 if self._optimization_coordinator is None else self._optimization_coordinator.get_optimizationSourceCount()

    @Property(str, notify=optimizationInputsChanged)
    def optimizationAvailability(self) -> str:
        return ("修正板优化需要项目模式及项目默认沉积配置。" if self._optimization_coordinator is None
                else self._optimization_coordinator.get_optimizationAvailability())

    @Property(str, notify=optimizationInputsChanged)
    def optimizationDefaultSummary(self) -> str:
        return "" if self._optimization_coordinator is None else self._optimization_coordinator.get_optimizationDefaultSummary()

    @Property(dict, notify=optimizationInputsChanged)
    def optimizationParameters(self) -> dict:
        return {} if self._optimization_coordinator is None else self._optimization_coordinator.get_optimizationParameters()

    @Property(str, notify=optimizationInputsChanged)
    def optimizationDefaultConfigurationId(self) -> str:
        return ("" if self._optimization_coordinator is None
                else self._optimization_coordinator.get_optimizationDefaultConfigurationId())

    @Property(int, notify=optimizationInputsChanged)
    def optimizationDefaultRevision(self) -> int:
        return (0 if self._optimization_coordinator is None
                else self._optimization_coordinator.get_optimizationDefaultRevision())

    @Property(bool, notify=optimizationInputsChanged)
    def optimizationReady(self) -> bool:
        return self._optimization_coordinator is not None and self._optimization_coordinator.get_optimizationReady()

    @Slot("QVariantMap", "QVariantMap")
    def startOptimization(self, settings: dict, options: dict) -> None:
        if self._optimization_coordinator is None:
            self._set_status("修正板优化需要项目模式及项目默认沉积配置。")
            return
        self._optimization_coordinator.startOptimization(settings, options)

    @Slot()
    def cancelOptimization(self) -> None:
        if self._optimization_coordinator is not None:
            self._optimization_coordinator.cancelOptimization()

    @Slot(QUrl, int, bool)
    def saveOptimizedMask(self, url: QUrl, index: int, overwrite: bool = False) -> None:
        if self._optimization_coordinator is None:
            self._set_status("修正板优化结果仅可保存到所属项目。")
            return
        self._optimization_coordinator.saveOptimizedMask(url, index, overwrite)

    @Slot(str, int)
    def saveOptimizedMaskNamed(self, name: str, index: int) -> None:
        if self._optimization_coordinator is None:
            self._set_status("修正板优化结果仅可保存到所属项目。")
            return
        self._optimization_coordinator.saveOptimizedMaskNamed(name, index)

    @Slot(result=bool)
    def refreshOptimizationDefault(self) -> bool:
        return (False if self._optimization_coordinator is None
                else self._optimization_coordinator.refreshDefault())

    @Slot(bool)
    def setEditingFrozen(self, frozen: bool) -> None:
        super().setEditingFrozen(frozen)
        if self._optimization_coordinator is not None:
            self._optimization_coordinator.setEditingFrozen(frozen)
            self.optimizationInputsChanged.emit()

    def detach_observers(self) -> None:
        if self._optimization_coordinator is not None:
            self._optimization_coordinator.detach_observers()

    def _set_document(self, data: dict[str, Any], path: str, dirty: bool) -> None:
        super()._set_document(data, path, dirty)
        # Replacement/open/import is a new view. Ordinary vertex/metadata edits
        # keep the user's current pan and zoom.
        if not self._reloading:
            self.viewFitRequested.emit()

    @Property(bool, notify=DocumentBridge.reloadStateChanged)
    def canReload(self) -> bool:
        return bool(self._reload_origin or self._machine_path)

    def _reload_identity(self):
        return (self._reload_origin, self._machine_path)

    def _reload_now(self) -> bool:
        try:
            document = (_checked_reload_document(self._service, "mask", self._reload_origin)
                        if self._reload_origin else None)
            machine = (_checked_reload_document(self._service, "machine", self._machine_path)
                       if self._machine_path else None)
            # Both reads must succeed before committing either document. A
            # machine-only refresh preserves an unsaved mask's geometry.
            self._reloading = True
            if machine is not None:
                self._machine = deepcopy(machine.data)
                self._update_source_sample_counts(machine)
                self._last_ring_z = None
                self.machineChanged.emit()
            if document is not None:
                self._set_document(deepcopy(document.data), str(document.path), False)
            else:
                self._document_modified()
            self._set_status("已刷新修正板及已加载的机器文件。" if document is not None
                             else "已刷新机器；当前修正板修改已保留。")
            return True
        except Exception as exc:
            self._set_status(f"刷新失败，已保留当前内容：{exc}")
            return False
        finally:
            self._reloading = False

    @Property(bool, notify=cadChanged)
    def cadBusy(self) -> bool:
        return self._cad_busy

    @Property("QVariantList", notify=cadChanged)
    def cadWarnings(self) -> list[str]:
        return list(self._cad_warnings)

    @Property("QVariantMap", notify=cadChanged)
    def cadProvenance(self) -> dict:
        return dict(self._cad_provenance)

    @Slot(str, str, str, str)
    @_project_edit()
    def importCad(self, path: str, units: str, curve_error: str, join_tolerance: str) -> None:
        self._start_cad("import", path, {"units": units,
            "curve_tolerance_mm": curve_error, "join_tolerance_mm": join_tolerance}, False)

    @Slot(str, str, str, bool)
    @_project_edit()
    def exportCad(self, path: str, format_name: str, options_json: str, overwrite: bool) -> None:
        try:
            options = json.loads(options_json or "{}")
            if not isinstance(options, dict):
                raise ValueError("加工选项须为对象")
            if format_name not in ("dxf", "pdf"):
                raise ValueError("仅支持DXF与PDF导出")
            self._start_cad(format_name, path, options, overwrite)
        except (ValueError, TypeError) as exc:
            self._set_status(f"CAD导出失败：{exc}")

    def _start_cad(self, operation: str, path: str, options: dict, overwrite: bool) -> None:
        if self._cad_busy:
            self._set_status("CAD操作正在进行，请等待完成。")
            return
        try:
            local = _local_path(path)
            data = deepcopy(self._data)
            if operation != "import" and not self.canSave:
                raise ValueError(self.validationMessage or "修正板轮廓无效")
            if operation == "import":
                options = {**options, "curve_tolerance_mm": float(options["curve_tolerance_mm"]),
                           "join_tolerance_mm": float(options["join_tolerance_mm"])}
        except (ValueError, TypeError, KeyError) as exc:
            self._set_status(f"CAD参数无效：{exc}")
            return
        self._cad_request_id += 1
        request, revision = self._cad_request_id, self._cad_revision
        self._cad_busy = True
        self._cad_warnings = []
        self.cadChanged.emit()
        self._set_status("正在导入二维DXF…" if operation == "import" else "正在生成CAD文件/完整加工图纸…")
        def work() -> None:
            try:
                if operation == "import":
                    result = self._service.import_mask_dxf(local, base_mask=data, **options)
                elif operation == "dxf":
                    result = self._service.export_mask_dxf(data, local, overwrite=overwrite)
                else:
                    result = self._service.export_mask_drawing(data, local, options=options, overwrite=overwrite)
                self._cadReady.emit(request, revision, operation, result)
            except Exception as exc:
                self._cadFailed.emit(request, str(exc))
        try:
            Thread(target=work, daemon=True).start()
        except Exception as exc:
            self._reject_cad(request, str(exc))

    @Slot(int, int, str, object)
    def _accept_cad(self, request: int, revision: int, operation: str, result: object) -> None:
        if request != self._cad_request_id:
            return
        self._cad_busy = False
        if operation == "import" and self._project_mutations_frozen():
            self._set_status("项目正在关闭或被冻结，DXF 导入结果未应用。")
            self.cadChanged.emit()
            self.cadFinished.emit(operation)
            return
        if not isinstance(result, dict):
            self._reject_cad(request, "CAD服务返回格式无效")
            return
        self._cad_warnings = list(result.get("warnings") or [])
        if operation == "import":
            if revision != self._cad_revision:
                self._set_status("导入期间设计已被修改，已保留当前内容；请重新导入，避免覆盖新编辑。")
                self.cadChanged.emit()
                return
            self._set_document(result["data"], self._path, True)
            self._cad_provenance = dict(result.get("provenance") or {})
            counts = self._cad_provenance.get("point_counts") or []
            self._set_status(f"DXF已填入设计：外轮廓{counts[0] if counts else 0}点，{max(0, len(counts) - 1)}孔；请预览核对并保存。")
        else:
            self._set_status(f"已导出{operation.upper()}：{result.get('path', '')}"
                             + (f"（{result['pages']}页）" if result.get("pages") else ""))
        self.cadChanged.emit()
        self.cadFinished.emit(operation)

    @Slot(int, str)
    def _reject_cad(self, request: int, message: str) -> None:
        if request != self._cad_request_id:
            return
        self._cad_busy = False
        self._cad_warnings = [message]
        self.cadChanged.emit()
        self._set_status(f"CAD操作失败：{message}")
        self.cadFinished.emit("error")

    @Property(str, notify=machineChanged)
    def machineJson(self) -> str:
        return _json_data(self._machine)

    @Property(str, notify=machineChanged)
    def machinePath(self) -> str:
        return self._machine_path

    @Property(str, notify=ringsChanged)
    def ringsJson(self) -> str:
        return _json_data(self._rings)

    @Property(str, notify=weightsChanged)
    def weightsJson(self) -> str:
        return _json_data(self._weights)

    @Property(str, notify=pointWeightsChanged)
    def pointWeightsJson(self) -> str:
        return _json_data(self._point_weights)

    @Property(str, notify=fieldChanged)
    def fieldJson(self) -> str:
        return _json_data(self._field)

    @Property(float, notify=workpieceDiameterChanged)
    def workpieceDiameterMm(self) -> float:
        return self._workpiece_diameter_mm

    @Property(int, notify=estimateChanged)
    def estimateRevision(self) -> int:
        return self._estimate_revision

    @Property(bool, notify=busyChanged)
    def radialBusy(self) -> bool:
        return self._radial_busy

    @Property(bool, notify=busyChanged)
    def fieldBusy(self) -> bool:
        return self._field_busy

    @Property(bool, notify=busyChanged)
    def pointBusy(self) -> bool:
        return self._point_busy

    def _estimate_work_units(self, kind: str, source_slot: int) -> int:
        if not self._machine_path or not 1 <= source_slot <= len(self._source_sample_counts):
            return 0
        try:
            sun = int(self._machine["sun_teeth"])
            planet = int(self._machine["planet_teeth"])
            if sun <= 0 or planet <= 0:
                return 0
            revolutions = planet // math.gcd(sun, planet)
        except (KeyError, TypeError, ValueError):
            return 0
        # Full-cycle phases × spatial samples × selected source quadrature nodes.
        spatial_and_phase = {
            "radial": 79 * 11 * 24,
            "field": 79 * (73 + 41 * 41),
            "point": 599 * 11 * 48,
        }
        return revolutions * spatial_and_phase[kind] * self._source_sample_counts[source_slot - 1]

    def _estimated_seconds(self, kind: str, source_slot: int) -> tuple[float, bool]:
        work_units = self._estimate_work_units(kind, source_slot)
        if work_units == 0:
            return 0.0, False
        measured = self._seconds_per_work_unit.get(kind)
        # First-use throughput is a project estimate, then this session's
        # measured wall time per source/phase/spatial sample takes precedence.
        seconds_per_unit = measured if measured is not None else 1 / 100_000
        return max(1.0, work_units * seconds_per_unit), measured is not None

    @staticmethod
    def _format_estimate(seconds: float) -> str:
        if seconds < 60:
            return f"约 {math.ceil(seconds)} 秒"
        if seconds < 3600:
            return f"约 {math.ceil(seconds / 60)} 分钟"
        return f"约 {math.ceil(seconds / 3600 * 10) / 10:g} 小时"

    @Slot(int, result=str)
    def estimateBoardTime(self, source_slot: int) -> str:
        radial, radial_measured = self._estimated_seconds("radial", source_slot)
        field, field_measured = self._estimated_seconds("field", source_slot)
        if radial == 0 or field == 0:
            return ""
        basis = ("按本机会话实测校准" if radial_measured and field_measured else
                 "部分或全部按本项目经验值粗估；完成后按本机会话实测校准")
        return (f"设计器 CPU 预计计算耗时：径向 {self._format_estimate(radial)}；"
                f"角向与热力图 {self._format_estimate(field)}；"
                f"两项并行约 {self._format_estimate(max(radial, field))}"
                f"至 {self._format_estimate(radial + field)}。{basis}。")

    @Slot(int, result=str)
    def estimatePointTime(self, source_slot: int) -> str:
        seconds, measured = self._estimated_seconds("point", source_slot)
        if seconds == 0:
            return ""
        basis = ("按本机会话实测校准" if measured else
                 "按本项目经验值粗估；完成后按本机会话实测校准")
        return f"设计器 CPU 预计计算耗时：{self._format_estimate(seconds)}（{basis}）。"

    def _record_compute_time(self, kind: str, result: dict[str, Any], elapsed_s: float) -> None:
        try:
            work_units = (int(result["phase_sample_count"]) *
                          int(result["point_count"]) *
                          int(result["source_sample_count"]))
        except (KeyError, TypeError, ValueError):
            return
        if work_units <= 0 or not math.isfinite(elapsed_s) or elapsed_s <= 0:
            return
        self._seconds_per_work_unit[kind] = elapsed_s / work_units
        self._estimate_revision += 1
        self.estimateChanged.emit()

    def _clear_field(self) -> None:
        self._field_request_id += 1
        self._field = {}
        self.fieldChanged.emit()

    @Slot(float)
    @_project_edit()
    def setWorkpieceDiameter(self, diameter_mm: float) -> None:
        diameter = float(diameter_mm)
        if not math.isfinite(diameter) or not 0 < diameter <= 20000:
            self._set_status("工件盘直径须在 0–20000 mm 之间")
            return
        if math.isclose(diameter, self._workpiece_diameter_mm):
            return
        self._workpiece_diameter_mm = diameter
        self.workpieceDiameterChanged.emit()
        self._request_id += 1
        self._weights = {}
        self.weightsChanged.emit()
        self._point_request_id += 1
        self._point_weights = {}
        self.pointWeightsChanged.emit()
        self._clear_field()
        self._set_status(f"分析范围已设为直径 {diameter:g} mm；请重新计算")

    def _document_modified(self) -> None:
        self._cad_revision += 1
        geometry = deepcopy((self._data.get("outer"), self._data.get("holes")))
        if geometry != self._cad_geometry:
            self._cad_provenance = {}
        self._cad_geometry = geometry
        self.cadChanged.emit()
        self._request_id += 1
        self._weights = {}
        self.weightsChanged.emit()
        self._point_request_id += 1
        self._point_weights = {}
        self.pointWeightsChanged.emit()
        self._clear_field()
        z = self._data.get("z_mm")
        if self._machine_path and z != self._last_ring_z:
            self.refreshRings()

    def _preview_settings(self):
        from coat_sim.core.simulator import Settings

        names = {field.name for field in fields(Settings)}
        params = {key: value for key, value in self._machine.items() if key in names}
        # Keep valid service defaults: the planet-center ring service forces one turn,
        # while weight analysis integrates the full point-return cycle in phase
        # space independently of these speed and duration values.
        params["carrier_rpm"] = 15.0
        params["duration_s"] = 4.0
        params["substrate_radius_mm"] = self._workpiece_diameter_mm / 2
        return Settings(**params)

    @Slot(str)
    @_project_edit()
    def loadMachine(self, path: str) -> None:
        try:
            local = _local_path(path)
            document = self._service.load("machine", local)
            self._machine = document.data
            self._machine_path = str(document.path)
            self._pending_reload = None
            self.reloadStateChanged.emit()
            self._update_source_sample_counts(document)
            self._point_request_id += 1
            self._point_weights = {}
            self.pointWeightsChanged.emit()
            self._request_id += 1
            self._weights = {}
            self.weightsChanged.emit()
            self._clear_field()
            self.machineChanged.emit()
            self._set_status(f"已加载机器：{Path(document.path).name}")
            self._validate()
            self.refreshRings()
            self.viewFitRequested.emit()
        except Exception as exc:
            self._set_status(f"加载机器失败：{exc}")

    def _update_source_sample_counts(self, document) -> None:
        source_kinds = {"point": 1, "line": 9, "disk": 12,
                        "annulus": 16, "rectangle": 16}
        runtime = getattr(document, "runtime", None) or {}
        self._source_sample_counts = [
            source_kinds.get((entry.get("profile") or {}).get("kind"), 1)
            for entry in runtime.get("sources", [])
        ]
        self._estimate_revision += 1
        self.estimateChanged.emit()

    @Slot()
    def refreshRings(self) -> None:
        self._rings = []
        self._last_ring_z = self._data.get("z_mm")
        if not self._machine_path:
            self.ringsChanged.emit()
            return
        try:
            settings = self._preview_settings()
            z = float(self._data["z_mm"])
            skipped: list[str] = []
            for slot in range(1, len(self._machine.get("sources", [])) + 1):
                try:
                    trajectory = self._service.source_trajectory(
                        settings, machine_path=self._machine_path,
                        mask_z_mm=z, source_slot=slot,
                    )
                except Exception as exc:
                    skipped.append(f"源{slot}：{exc}")
                    continue
                self._rings.append({
                    "source_slot": slot,
                    "points": trajectory["points_xy"],
                    "center_xy": trajectory.get("center_xy"),
                    "radius_mm": trajectory.get("radius_mm"),
                })
            message = f"已更新 {len(self._rings)} 个源的盘心遮挡环"
            if skipped:
                message += "；略过 " + "、".join(skipped)
            self._set_status(message)
        except Exception as exc:
            self._set_status(f"盘心遮挡环更新失败：{exc}")
        self.ringsChanged.emit()

    @Slot(int)
    def closePolygon(self, hole_index: int) -> None:
        holes = self._data.get("holes", [])
        if hole_index >= len(holes):
            self._set_status("未选择有效孔洞")
            return
        points = self._data.get("outer", []) if hole_index < 0 else holes[hole_index]
        self._set_status("轮廓已闭合（首末点自动相连）" if len(points) >= 3 else "闭合轮廓至少需要三个点")

    @Slot(str, float, float, float)
    @_project_edit()
    def rotatePolygon(self, pointer: str, center_x_mm: float, center_y_mm: float,
                      angle_deg: float) -> None:
        """Rotate one explicitly selected mask contour about an XY point in mm."""
        try:
            if not all(math.isfinite(value) for value in
                       (center_x_mm, center_y_mm, angle_deg)):
                raise ValueError("旋转中心和角度必须是有限数值")
            if abs(center_x_mm) > 100000 or abs(center_y_mm) > 100000:
                raise ValueError("旋转中心范围为 ±100000 mm")
            if abs(angle_deg) > 360000:
                raise ValueError("旋转角度范围为 ±360000°")
            if pointer == "/outer":
                polygon = self._data.get("outer")
            elif pointer.startswith("/holes/"):
                index_text = pointer.removeprefix("/holes/")
                if not index_text.isdecimal():
                    raise ValueError("请选择有效轮廓")
                holes = self._data.get("holes", [])
                index = int(index_text)
                polygon = holes[index] if index < len(holes) else None
            else:
                raise ValueError("请选择外轮廓或一个孔洞")
            if not isinstance(polygon, list) or len(polygon) < 3:
                raise ValueError("所选轮廓至少需要三个点")

            angle = math.radians(angle_deg % 360.0)
            cosine, sine = math.cos(angle), math.sin(angle)
            updated = deepcopy(self._data)
            if pointer == "/outer":
                target = updated["outer"]
            else:
                target = updated["holes"][int(pointer.removeprefix("/holes/"))]
            for point in target:
                dx, dy = float(point[0]) - center_x_mm, float(point[1]) - center_y_mm
                point[0] = round(center_x_mm + dx * cosine - dy * sine, 6)
                point[1] = round(center_y_mm + dx * sine + dy * cosine, 6)
            self._data = updated
            self._dirty = True
            self.dirtyChanged.emit()
            self.documentChanged.emit()
            self._validate()
            self._set_status(
                f"已将所选轮廓绕 ({center_x_mm:g}, {center_y_mm:g}) mm 旋转 {angle_deg:g}°"
            )
        except (ValueError, TypeError, IndexError, KeyError) as exc:
            self._set_status(f"轮廓旋转失败：{exc}")

    @Slot(float, float, float, float)
    @_project_edit()
    def transformMask(self, dx: float, dy: float, sx: float, sy: float) -> None:
        if (not all(math.isfinite(v) for v in (dx, dy, sx, sy))
                or abs(dx) > 100000 or abs(dy) > 100000
                or not 0.01 <= sx <= 100 or not 0.01 <= sy <= 100):
            self._set_status("平移范围为 ±100000 mm；缩放比例范围为 0.01–100")
            return
        try:
            transformed = deepcopy(self._data)
            polygons = [transformed.get("outer", [])] + transformed.get("holes", [])
            all_points = [point for polygon in polygons for point in polygon]
            if not all_points:
                raise ValueError("轮廓中没有可变换的点")
            cx = (min(float(p[0]) for p in all_points) + max(float(p[0]) for p in all_points)) / 2
            cy = (min(float(p[1]) for p in all_points) + max(float(p[1]) for p in all_points)) / 2
            for polygon in polygons:
                for point in polygon:
                    point[0] = cx + (float(point[0]) - cx) * sx + dx
                    point[1] = cy + (float(point[1]) - cy) * sy + dy
            self._data = transformed
            self._dirty = True
            self.dirtyChanged.emit()
            self.documentChanged.emit()
            self._validate()
            self._set_status("轮廓与孔洞已平移、缩放")
        except Exception as exc:
            self._set_status(f"轮廓变换失败：{exc}")

    @Slot(int, int)
    def computeWeights(self, mask_slot: int, source_slot: int) -> None:
        if self._radial_busy:
            self._set_status("整板径向遮挡仍在计算")
            return
        if not self._machine_path:
            self._set_status("请先加载机器")
            return
        self._validate()
        if not self._can_save:
            self._set_status("当前修正板参数无效，无法计算")
            return
        self._request_id += 1
        request_id = self._request_id
        try:
            settings = self._preview_settings()
            machine_path = self._machine_path
            mask_data = deepcopy(self._data)
        except Exception as exc:
            self._set_status(f"预览参数无效：{exc}")
            return
        self._set_status("正在计算遮挡权重…")
        self._radial_busy = True
        self.busyChanged.emit()

        def work() -> None:
            try:
                started_at = perf_counter()
                result = self._service.mask_weights(
                    settings, machine_path=machine_path,
                    mask_slot=mask_slot, source_slot=source_slot,
                    mask_data=mask_data,
                )
                result["source_slot"] = source_slot
                result["mask_slot"] = mask_slot
                self._weightsReady.emit(request_id, _json_data(result),
                                        perf_counter() - started_at)
            except Exception as exc:
                self._weightsFailed.emit(request_id, str(exc))

        try:
            Thread(target=work, daemon=True).start()
        except Exception as exc:
            self._radial_busy = False
            self.busyChanged.emit()
            self._set_status(f"无法开始遮挡权重计算：{exc}")

    @Slot(int, int, str)
    def computeField(self, mask_slot: int, source_slot: int,
                     ring_radii_text: str) -> None:
        if self._field_busy:
            self._set_status("角向与热力图仍在计算")
            return
        if not self._machine_path:
            self._set_status("请先加载机器")
            return
        self._validate()
        if not self._can_save:
            self._set_status("当前修正板参数无效，无法计算")
            return
        self._clear_field()
        request_id = self._field_request_id
        try:
            settings = self._preview_settings()
            machine_path = self._machine_path
            mask_data = deepcopy(self._data)
            ring_radii = None
            if ring_radii_text.strip():
                ring_radii = [float(part.strip()) for part in
                              ring_radii_text.replace("，", ",").split(",")]
                if (not 1 <= len(ring_radii) <= 8 or
                        any(not math.isfinite(value) or value < 0 or
                            value > settings.substrate_radius_mm
                            for value in ring_radii)):
                    raise ValueError("角向半径最多 8 个，均须位于 0 至工件盘半径内")
        except Exception as exc:
            self._set_status(f"盘面分析参数无效：{exc}")
            return
        self._set_status("正在计算角向遮挡与整盘热力图…")
        self._field_busy = True
        self.busyChanged.emit()

        def work() -> None:
            try:
                started_at = perf_counter()
                result = self._service.mask_field(
                    settings, machine_path=machine_path,
                    mask_slot=mask_slot, source_slot=source_slot,
                    mask_data=mask_data, ring_radii_mm=ring_radii)
                self._fieldReady.emit(request_id, _json_data(result),
                                      perf_counter() - started_at)
            except Exception as exc:
                self._fieldFailed.emit(request_id, str(exc))

        try:
            Thread(target=work, daemon=True).start()
        except Exception as exc:
            self._field_busy = False
            self.busyChanged.emit()
            self._set_status(f"无法开始盘面分析：{exc}")

    @Slot(int, str, float)
    def _accept_field(self, request_id: int, result_json: str, elapsed_s: float) -> None:
        self._field_busy = False
        self.busyChanged.emit()
        if request_id != self._field_request_id:
            return
        self._field = json.loads(result_json)
        self._record_compute_time("field", self._field, elapsed_s)
        self.fieldChanged.emit()
        self._set_status("角向遮挡与整盘热力图计算完成")

    @Slot(int, str)
    def _reject_field(self, request_id: int, message: str) -> None:
        self._field_busy = False
        self.busyChanged.emit()
        if request_id == self._field_request_id:
            self._set_status(f"盘面分析失败：{message}")

    @Slot(float, float, float, int)
    def computePointWeights(self, x_mm: float, y_mm: float,
                            patch_radius_mm: float, source_slot: int) -> None:
        if self._point_busy:
            self._set_status("选点遮挡权重仍在计算")
            return
        if not self._machine_path:
            self._set_status("请先加载机器")
            return
        self._point_request_id += 1
        request_id = self._point_request_id
        try:
            settings = self._preview_settings()
            machine_path = self._machine_path
            z_mm = float(self._data["z_mm"])
        except (ValueError, TypeError, KeyError) as exc:
            self._set_status(f"选点预览参数无效：{exc}")
            return
        self._set_status("正在计算选点遮挡权重…")
        self._point_busy = True
        self.busyChanged.emit()

        def work() -> None:
            try:
                started_at = perf_counter()
                result = self._service.point_weights(
                    settings, machine_path=machine_path, mask_z_mm=z_mm,
                    point_xy_mm=(x_mm, y_mm),
                    patch_radius_mm=patch_radius_mm,
                    source_slot=source_slot)
                self._pointWeightsReady.emit(request_id, _json_data(result),
                                             perf_counter() - started_at)
            except Exception as exc:
                self._pointWeightsFailed.emit(request_id, str(exc))

        try:
            Thread(target=work, daemon=True).start()
        except Exception as exc:
            self._point_busy = False
            self.busyChanged.emit()
            self._set_status(f"无法开始选点分析：{exc}")

    @Slot(int, str, float)
    def _accept_point_weights(self, request_id: int, result_json: str,
                              elapsed_s: float) -> None:
        self._point_busy = False
        self.busyChanged.emit()
        if request_id != self._point_request_id:
            return
        self._point_weights = json.loads(result_json)
        self._record_compute_time("point", self._point_weights, elapsed_s)
        self.pointWeightsChanged.emit()
        self._set_status("选点遮挡权重计算完成")

    @Slot(int, str)
    def _reject_point_weights(self, request_id: int, message: str) -> None:
        self._point_busy = False
        self.busyChanged.emit()
        if request_id == self._point_request_id:
            self._set_status(f"选点遮挡权重计算失败：{message}")

    @Slot(int, str, float)
    def _accept_weights(self, request_id: int, result_json: str, elapsed_s: float) -> None:
        self._radial_busy = False
        self.busyChanged.emit()
        if request_id != self._request_id:
            return
        self._weights = json.loads(result_json)
        self._record_compute_time("radial", self._weights, elapsed_s)
        self.weightsChanged.emit()
        self._set_status("遮挡权重计算完成")

    @Slot(int, str)
    def _reject_weights(self, request_id: int, message: str) -> None:
        self._radial_busy = False
        self.busyChanged.emit()
        if request_id == self._request_id:
            self._set_status(f"遮挡权重计算失败：{message}")


def create_editor(kind: str, service: CoatService | None = None) -> tuple[QQmlApplicationEngine, DocumentBridge]:
    """Create an editor in an existing QGuiApplication event loop."""
    if QGuiApplication.instance() is None:
        raise RuntimeError("请先创建 QGuiApplication")
    icon = window_icon(kind)  # Install identity before QML maps its window.
    bridge: DocumentBridge = MachineBridge(service) if kind == "machine" else MaskBridge(service)
    name = "MachineWindow.qml" if kind == "machine" else "MaskWindow.qml"
    engine = QQmlApplicationEngine()
    from coat_sim.ui.theme_service import ThemeService

    ThemeService.instance().attach_engine(engine)
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).with_name("qml") / name)))
    if not engine.rootObjects():
        raise RuntimeError(f"无法打开 {name}")
    engine.rootObjects()[0].setIcon(icon)
    return engine, bridge


def run_editor(kind: str, initial_path: str | None = None) -> int:
    """Standalone entry point for the machine or mask designer."""
    from coat_sim.ui.settings import apply_font_settings, configure_qt_dpi_scale
    from coat_sim.ui.theme_service import configure_theme_from_settings

    configure_qt_dpi_scale()
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setWindowIcon(window_icon(kind))
    apply_font_settings(app=app)
    configure_theme_from_settings()
    engine, bridge = create_editor(kind)
    if initial_path:
        bridge.loadDocument(initial_path)
    # Keep the Python bridge alive for as long as the QML engine owns its window.
    engine._editor_bridge = bridge  # type: ignore[attr-defined]
    return app.exec()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="机器结构或修正板设计器")
    parser.add_argument("kind", choices=("machine", "mask"))
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    raise SystemExit(run_editor(args.kind, args.path))
