"""Qt Quick bridges for the independent source and substrate editors.

The bridges own document state and file operations. QML owns only presentation;
all profile validation and physical preview sampling go through CoatService.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import math
from pathlib import Path
import sys
import weakref
from threading import get_ident
from typing import Any, TYPE_CHECKING, cast

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot, QRunnable, QThreadPool
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlContext

from coat_sim.ui.icons import window_icon

from coat_sim.core.api import CoatService
from coat_sim.core.data_files import design_directory

if TYPE_CHECKING:
    from coat_sim.core.project_resource_service import ProjectResourceService


def _number(value: Any, label: str) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}须为数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}须为有限数值")
    return number


def _file_path(url: QUrl | str) -> Path:
    qurl = url if isinstance(url, QUrl) else QUrl(str(url))
    path = qurl.toLocalFile() if qurl.isLocalFile() else str(url)
    if not path:
        raise ValueError("请选择本地 JSON 文件")
    return Path(path).expanduser().resolve()


def _document_data(document: Any) -> dict:
    data = getattr(document, "data", document)
    if not isinstance(data, dict):
        raise ValueError("文件内容必须是 JSON 对象")
    return data


def _validation_error(result: Any) -> str | None:
    if result is None:
        return None
    valid = getattr(result, "valid", getattr(result, "ok", True))
    if valid:
        return None
    errors = getattr(result, "errors", None)
    if errors:
        return "；".join(str(item) for item in errors)
    return str(getattr(result, "message", "参数无效"))


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


def _connect_material_library(editor, library):
    """Qt owns callbacks without retaining its editor through Python cycles."""
    weak_editor = weakref.ref(editor)
    def select_prepared(material):
        owner = weak_editor()
        if owner is not None:
            owner._select_prepared_material(material)
    def changed():
        owner = weak_editor()
        if owner is not None:
            owner.changed.emit()
    library.materialSelected.connect(select_prepared)
    library.changed.connect(changed)


class _EditorBridge(QObject):

    @Property(QObject, constant=True)
    def informationController(self):
        if not hasattr(self, "_information_controller"):
            from .project_resource_adapter import EditorResourceInformationController
            self._information_controller = EditorResourceInformationController(self, self.kind, self)
        return self._information_controller

    changed = Signal()
    documentChanged = Signal()
    overwriteRequested = Signal(QUrl)
    reloadRequested = Signal()
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
        if not getattr(self.service, "is_project_service", False):
            return False
        gate = getattr(self.service, "_gate", None)
        gate_frozen = getattr(gate, "frozen", False) if gate is not None else False
        if callable(gate_frozen):
            gate_frozen = gate_frozen()
        close_save_threads: set[int] = getattr(gate, "close_save_threads", set()) if gate is not None else set()
        coordinated_save = get_ident() in close_save_threads
        return bool(getattr(self, "_editing_frozen", False) or (gate_frozen and not coordinated_save))

    kind = ""

    def __init__(self, service: CoatService | None = None, initial_path: str | None = None):
        super().__init__()
        # These bridges are invoked by desktop file pickers; the user may
        # explicitly choose a JSON file outside the project directory.
        self.service = service or CoatService(restrict_to_root=False)
        self._project_navigation = getattr(self.service, "project_navigation", None)
        self._editing_frozen = False
        # Keep the legacy .coatasset reader API for old projects/files. The
        # ordinary source/substrate windows no longer expose this dialog.
        self._asset_browser = None
        self._path: Path | None = None
        self._reload_origin: Path | None = None
        self._pending_reload: Path | None = None
        self._pending_save_as: Path | None = None
        self._pending_document_replace: tuple[str, Any, Path | None, int] | None = None
        self._project_replace_bypass = False
        self._draft_revision = 0
        self._dirty = False
        self._valid = False
        self._error = ""
        self._status = ""
        self._reference_generation = 0
        self._reference_tasks = {}
        self._reset_document()
        self._refresh_validation()
        if initial_path:
            self.openPath(initial_path)

    def _refresh_material_reference(self):
        from copy import deepcopy
        self._reference_generation += 1
        self._material_reference_sections = []
        self._reference_loading = bool(self._material)
        if not self._material:
            return
        task = _SourceReferenceTask(self._reference_generation, deepcopy(self._material))
        task.signals.finished.connect(self._material_reference_ready)
        self._reference_tasks[self._reference_generation] = task
        QThreadPool.globalInstance().start(task)

    @Slot(int, list, str)
    def _material_reference_ready(self, generation, rows, error):
        self._reference_tasks.pop(generation, None)
        if generation != self._reference_generation:
            return
        self._reference_loading = False
        self._material_reference_sections = rows
        if error:
            self._set_status("材料参考详情准备失败：" + error)
        self.changed.emit()

    @Property(bool, notify=changed)
    def materialReferenceLoading(self):
        return self._reference_loading

    @Property("QVariantList", notify=changed)
    def materialReferenceSections(self):
        return self._material_reference_sections

    def _reset_document(self) -> None:
        raise NotImplementedError

    def _load_document(self, data: dict) -> None:
        raise NotImplementedError

    @Property(QObject, constant=True)
    def assetBrowser(self):
        if self._asset_browser is None:
            from .asset_browser import AssetBrowserBridge
            weak_editor = weakref.ref(self)
            def open_asset(path):
                editor = weak_editor()
                if editor is not None:
                    editor.openPath(QUrl.fromLocalFile(str(path)))
            self._asset_browser = AssetBrowserBridge(
                self.service, self.kind, open_asset, self)
        return self._asset_browser

    @Property(QObject, constant=True)
    def projectNavigation(self):
        return self._project_navigation

    @Property(QUrl, constant=True)
    def defaultDirectory(self):
        if getattr(self.service, "is_project_service", False):
            return QUrl.fromLocalFile(str(cast("ProjectResourceService", self.service).default_directory(self.kind)))
        return QUrl.fromLocalFile(str(design_directory(self.kind)))

    @Property(str, notify=changed)
    def description(self):
        return self._fields.get("description", "")

    @Property(str, notify=changed)
    def notes(self):
        return self._fields.get("notes", "")

    @Slot(str, str)
    @_project_edit()
    def setMetadata(self, key, value):
        if key in ("description", "notes"):
            self.setField(key, value)

    def _compose_document(self) -> dict:
        raise NotImplementedError

    def documentData(self) -> dict:
        """Return the same draft represented by the visible editor controls."""
        if getattr(self, "_material_validated", False) and self._material is not None:
            # Export is a strict boundary even though routine UI edits can reuse
            # the already-validated, privately owned immutable evidence.
            from coat_sim.core.materials import validate_material_profile
            validate_material_profile(self._material)
        return self._compose_document()

    def _defer_project_replace(self, action: str, value=None) -> bool:
        if (self.kind in ("source", "substrate") and getattr(self.service, "is_project_service", False)
                and self._dirty and not self._project_replace_bypass):
            self._pending_document_replace = (
                action, value, self._path, self._draft_revision)
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
        action, value, old_path, old_revision = pending
        if old_path != self._path or old_revision != self._draft_revision:
            self._set_status("当前设计已更改，请重新确认替换。")
            return False
        self._project_replace_bypass = True
        try:
            if action == "new":
                self.newDocument()
            elif action == "open":
                self.openPath(value)
        finally:
            self._project_replace_bypass = False
        return True

    def applyDocument(self, data: dict, intended_path: str = "") -> None:
        """Apply an already checked automation draft and notify the existing UI."""
        if self._project_mutations_frozen():
            self._set_status("项目正在关闭或被冻结，当前修改未应用。")
            return
        self._pending_save_as = None
        self._load_document(data)
        self._reload_origin = self._pending_reload = None
        self._path = Path(intended_path).resolve() if intended_path else None
        self._dirty = True
        self._draft_revision += 1
        self._refresh_validation()
        self._set_status("已填入当前设计，尚未保存；请确认后保存。")
        self.documentChanged.emit()

    def _set_status(self, value: str) -> None:
        self._status = value
        self.changed.emit()

    def _refresh_validation(self) -> None:
        try:
            data = self._validation_document()
            result = self.service.validate(self.kind, data, path=self._path)
            error = _validation_error(result)
            if error:
                raise ValueError(error)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self._valid, self._error = False, str(exc)
        else:
            self._valid, self._error = True, ""
        self.changed.emit()

    def _validation_document(self):
        return self._compose_document()

    def _edited(self) -> None:
        self._pending_reload = None
        self._pending_save_as = None
        self._dirty = True
        self._draft_revision += 1
        self._refresh_validation()

    @Property(str, notify=changed)
    def currentPath(self) -> str:
        return str(self._path) if self._path else ""

    @Property(str, notify=changed)
    def pendingSaveAs(self) -> str:
        return str(self._pending_save_as) if self._pending_save_as else ""

    @Property(str, notify=changed)
    def fileName(self) -> str:
        return self._path.name if self._path else "未命名"

    @Property(str, notify=changed)
    def displayName(self) -> str:
        return self._document_name() or "未命名"

    def _document_name(self) -> str:
        """Concrete editors expose their draft name without composing payloads."""
        raise NotImplementedError

    @Property(bool, notify=changed)
    def dirty(self) -> bool:
        return self._dirty

    @Property(bool, notify=changed)
    def valid(self) -> bool:
        return self._valid

    @Property(str, notify=changed)
    def error(self) -> str:
        return self._error

    @Property(str, notify=changed)
    def status(self) -> str:
        return self._status

    @Slot()
    @_project_edit()
    def newDocument(self) -> None:
        if self._defer_project_replace("new"):
            return
        self._pending_save_as = None
        self._path = None
        self._reload_origin = self._pending_reload = None
        self._dirty = False
        self._reset_document()
        self._refresh_validation()
        self._set_status("已新建；首次保存请选择路径。")
        self.documentChanged.emit()

    @Slot(QUrl, result=bool)
    @Slot(str, result=bool)
    @_project_edit(False)
    def openPath(self, url: QUrl | str) -> bool:
        if self._defer_project_replace("open", url):
            return False
        self._pending_save_as = None
        try:
            path = _file_path(url)
            document = self.service.load(self.kind, path)
            data = _document_data(document)
            self._load_document(data)
        except (ValueError, OSError, TypeError) as exc:
            self._set_status(f"打开失败：{exc}")
            return False
        self._path = Path(document.path)
        self._reload_origin = self._path
        self._pending_reload = None
        self._dirty = False
        self._refresh_validation()
        self._set_status(f"已打开：{self._path}")
        self.documentChanged.emit()
        return True

    @Slot(str)
    @_project_edit()
    def openDocument(self, path: str) -> None:
        """Open an explicit path requested by the shared window launcher."""
        self.openPath(path)

    @Property(bool, notify=changed)
    def canReload(self) -> bool:
        return self._reload_origin is not None and self._path == self._reload_origin

    @Slot(result=bool)
    @_project_edit()
    def reloadCurrent(self) -> bool:
        if not self.canReload:
            self._set_status("请先打开或保存设计，再刷新文件。")
            return False
        if self._dirty:
            if self._pending_reload != self._reload_origin:
                self._pending_reload = self._reload_origin
                self.reloadRequested.emit()
            return False
        return self._reload_current()

    @Slot(bool, result=bool)
    @_project_edit(False)
    def confirmReload(self, confirmed: bool) -> bool:
        path, self._pending_reload = self._pending_reload, None
        if not confirmed or path is None or path != self._reload_origin or path != self._path:
            return False
        return self._reload_current()

    def _reload_current(self) -> bool:
        # Load and validate before touching the editor, including its dirty flag.
        # The origin is the actual file (or explicitly exported package member),
        # never a temporary dependency extraction directory.
        try:
            document = self.service.load(self.kind, self._reload_origin)
            if document.issues:
                raise ValueError("；".join(document.issues))
            data = _document_data(document)
            error = _validation_error(self.service.validate(self.kind, data, path=self._reload_origin))
            if error:
                raise ValueError(error)
            self._load_document(data)
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
            self._set_status(f"刷新失败，当前设计保持不变：{exc}")
            return False
        self._pending_save_as = None
        self._pending_reload = None
        self._dirty = False
        self._refresh_validation()
        self._set_status(f"已重新载入：{self._path}")
        self.documentChanged.emit()
        return True

    def _save_to(self, path: Path, *, overwrite: bool) -> bool:
        try:
            data = self._compose_document()
            result = self.service.validate(self.kind, data, path=path)
            error = _validation_error(result)
            if error:
                raise ValueError(error)
            document = self.service.save(self.kind, path, data, overwrite=overwrite)
            self._load_document(_document_data(document))
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
            self._set_status(f"保存失败：{exc}")
            self._refresh_validation()
            return False
        self._path = Path(getattr(document, "path", path))
        self._reload_origin = self._path
        self._pending_reload = None
        self._dirty = False
        self._refresh_validation()
        self._set_status(f"已保存：{self._path}")
        self.documentChanged.emit()
        return True

    @Slot(result=bool)
    @_project_edit()
    def save(self) -> bool:
        self._pending_save_as = None
        if self._path is None:
            self._set_status("请选择保存路径。")
            return False
        return self._save_to(self._path, overwrite=True)

    @Slot(QUrl, result=bool)
    @Slot(str, result=bool)
    @_project_edit(False)
    def saveAsPath(self, url: QUrl | str) -> bool:
        self._pending_save_as = None
        try:
            from coat_sim.core.design_files import default_design_path, validate_design_path
            path = validate_design_path(self.kind, default_design_path(self.kind, _file_path(url)))
            if self._path is not None and path == self._path:
                raise ValueError("另存为须选择与当前文件不同的路径")
            result = self.service.validate(self.kind, self._compose_document(), path=path)
            error = _validation_error(result)
            if error:
                raise ValueError(error)
            existing = (bool(getattr(self.service, "is_managed_path", lambda _p: False)(path))
                        if getattr(self.service, "is_project_service", False)
                        else path.exists())
            if existing:
                # Preflight both suffix and payload before showing any overwrite
                # prompt; the service rechecks again at the actual write.
                try:
                    self.service.load(self.kind, path)
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    raise ValueError("已有文件损坏或与当前设计类型不符，原文件保持不变；"
                                     "请选择其他文件名。详情：" + str(exc)) from exc
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
            self._set_status(str(exc))
            return False
        if existing:
            self._pending_save_as = path
            self._set_status("目标文件已存在；请确认是否覆盖")
            self.overwriteRequested.emit(QUrl.fromLocalFile(str(path)))
            return False
        return self._save_to(path, overwrite=False)

    @Slot(bool, result=bool)
    @_project_edit(False)
    def confirmSaveAs(self, confirmed: bool) -> bool:
        path, self._pending_save_as = self._pending_save_as, None
        if not confirmed or path is None:
            if path is not None:
                self._set_status("已取消覆盖；原文件和当前设计保持不变")
            else:
                self.changed.emit()
            return False
        return self._save_to(path, overwrite=True)


class _SourceReferenceSignals(QObject):
    finished = Signal(int, list, str)


class _SourceReferenceTask(QRunnable):
    def __init__(self, generation, material):
        super().__init__()
        self.generation, self.material = generation, material
        self.signals = _SourceReferenceSignals()

    def run(self):
        from .material_research import prepare_reference_sections
        try:
            rows = prepare_reference_sections(self.material.get("reference_data"),
                                              self.material.get("research_data"))
        except (ValueError, TypeError, KeyError) as exc:
            self.signals.finished.emit(self.generation, [], str(exc))
        else:
            self.signals.finished.emit(self.generation, rows, "")


class SourceBridge(_EditorBridge):
    """Location-free source, embedded material snapshot and editable emission model."""

    sourceDataChanged = Signal()
    discardRequested = Signal()
    closeApproved = Signal()
    kind = "source"

    def _document_name(self) -> str:
        return self._fields.get("name", "")

    def __init__(self, service: CoatService | None = None,
                 initial_path: str | None = None):
        self._pending_action = None
        super().__init__(service, initial_path)
        factory = getattr(self.service, "material_library_bridge", None)
        if callable(factory):
            self._material_library = factory(self, "film")
        else:
            from .source_materials import MaterialLibraryBridge
            self._material_library = MaterialLibraryBridge(self)
        _connect_material_library(self, self._material_library)
        self._reference_calculator = None
        self.changed.connect(self.sourceDataChanged.emit)

    def _reset_document(self) -> None:
        self._fields = {"name": "", "description": "", "notes": "",
            "device_type": "custom", "kind": "point", "source_exponent": "1", "sampling_order": "",
            "length_mm": "40", "angle_deg": "0", "radius_mm": "12",
            "width_mm": "20", "inner_radius_mm": "6", "outer_radius_mm": "15",
        }
        self._material = None
        self._material_validated = False
        self._material_reference_sections = []
        self._reference_loading = False
        self._reference_generation += 1
        self._thermochemistry = None
        self._emission_present = False
        self._preview_rate = "10"
        self._sample_cache = None
        self._emission = {
            "kind": "uniform", "quadrature_order": "9", "reference_rate_nm_s": "10", "rate_exponent_per_decade": "0",
            "active_fraction": "1", "rate_active_fraction_per_decade": "0", "center_weight": "0",
            "boat_mode": "open", "cover_height_mm": "10", "cover_thickness_mm": "0",
            "spot_sigma_mm": "3",
            "rate_spot_sigma_per_decade": "0", "scan_pattern": "point", "scan_width_mm": "0",
            "scan_height_mm": "0", "scan_center_x_mm": "0", "scan_center_y_mm": "0", "scan_angle_deg": "0", "dwell_center_fraction": "0", "dwell_points": "",
            "valid_rate_min_nm_s": "", "valid_rate_max_nm_s": "",
            "provenance_status": "unvalidated", "provenance_reference": "", "provenance_notes": "",
        }
        self._apertures = [{"kind": "rectangle", "length_mm": "20", "width_mm": "10",
                            "radius_mm": "5", "offset_x_mm": "0", "offset_y_mm": "0", "angle_deg": "0"}]
        self._aperture_index = 0

    def _read_emission(self, model):
        from .source_materials import RATE_KEYS, _format
        for key in self._emission:
            if key in model:
                value = model[key]
                if key == "dwell_points":
                    value = "\n".join(f'{_format(row["x_mm"])}, {_format(row["y_mm"])}, {_format(row["weight"])}' for row in value)
                elif key in RATE_KEYS and value is not None:
                    value = value * 10
                self._emission[key] = _format(value)
        for key in ("status", "reference", "notes"):
            self._emission["provenance_" + key] = str(model.get("provenance", {}).get(key, "unvalidated" if key == "status" else ""))
        if "apertures" in model:
            self._apertures = [{key: _format(value) for key, value in row.items()} for row in model["apertures"]]
            self._aperture_index = min(self._aperture_index, len(self._apertures) - 1)

    def _load_document(self, data: dict) -> None:
        from copy import deepcopy
        from coat_sim.core.source_profiles import validate_source_profile
        normalized = validate_source_profile(data)
        self._reset_document()
        for key in self._fields:
            if key in normalized:
                self._fields[key] = str(normalized[key])
        self._material = deepcopy(normalized.get("material"))
        self._material_validated = self._material is not None
        self._refresh_material_reference()
        self._thermochemistry = deepcopy(normalized.get("thermochemistry"))
        if "emission_model" in normalized:
            self._emission_present = True
            self._read_emission(normalized["emission_model"])
            self._preview_rate = self._emission["reference_rate_nm_s"]

    @Property(QObject, constant=True)
    def materialLibrary(self):
        return self._material_library

    @Property(QObject, constant=True)
    def referenceCalculator(self):
        if self._reference_calculator is None:
            from .source_reference import ReferenceCalculatorBridge
            self._reference_calculator = ReferenceCalculatorBridge(self)
            self.sourceDataChanged.connect(self._reference_calculator.sourceChanged)
        return self._reference_calculator

    @Property("QVariantMap", notify=sourceDataChanged)
    def fields(self) -> dict:
        return dict(self._fields)

    @Property("QVariantMap", notify=sourceDataChanged)
    def emissionFields(self) -> dict:
        return dict(self._emission)

    @Property("QVariantList", notify=sourceDataChanged)
    def apertures(self):
        return [dict(row) for row in self._apertures]

    @Property(int, notify=sourceDataChanged)
    def apertureIndex(self):
        return self._aperture_index

    @Property("QVariantMap", notify=sourceDataChanged)
    def apertureFields(self):
        return dict(self._apertures[self._aperture_index])

    @Slot(int)
    @_project_edit()
    def selectAperture(self, index):
        if 0 <= index < len(self._apertures):
            self._aperture_index = index
            self.sourceDataChanged.emit()

    @Slot()
    @_project_edit()
    def addAperture(self):
        if len(self._apertures) >= 100:
            return
        self._apertures.append(dict(self._apertures[self._aperture_index]))
        self._aperture_index = len(self._apertures) - 1
        self._emission_present = True
        self._edited()

    @Slot()
    @_project_edit()
    def removeAperture(self):
        if len(self._apertures) <= 1:
            return
        self._apertures.pop(self._aperture_index)
        self._aperture_index = min(self._aperture_index, len(self._apertures) - 1)
        self._edited()

    @Slot(str, str)
    @_project_edit()
    def setApertureField(self, key, value):
        row = self._apertures[self._aperture_index]
        if key not in row or row[key] == value:
            return
        row[key] = value
        self._emission_present = True
        self._edited()

    @Property(str, notify=sourceDataChanged)
    def materialName(self):
        return self._material["name"] if self._material else "未选择材料（可使用简单快速模型）"

    @Property(bool, notify=sourceDataChanged)
    def hasMaterial(self):
        return self._material is not None

    @Property(str, notify=sourceDataChanged)
    def previewRate(self):
        return self._preview_rate

    @Property(str, notify=sourceDataChanged)
    def pendingAction(self):
        return self._pending_action[0] if self._pending_action else ""

    def _guard(self, action, value=None):
        self._pending_action = (action, value)
        self.changed.emit()
        self.discardRequested.emit()

    @Slot()
    @_project_edit()
    def newDocument(self):
        self._pending_save_as = None
        if self._defer_project_replace("new"):
            return
        if self._dirty and not (getattr(self.service, "is_project_service", False)
                                and self._project_replace_bypass):
            self._guard("new")
        else:
            super().newDocument()

    @Slot(QUrl, result=bool)
    @Slot(str, result=bool)
    @_project_edit(False)
    def openPath(self, url):
        self._pending_save_as = None
        if self._defer_project_replace("open", url):
            return False
        if self._dirty and not (getattr(self.service, "is_project_service", False)
                                and self._project_replace_bypass):
            self._guard("open", url)
            return False
        return super().openPath(url)

    @Slot(result=bool)
    def requestClose(self):
        self._pending_save_as = None
        if self._dirty or self._material_library.dirty:
            self._guard("close")
            return False
        return True

    @Slot(bool)
    @_project_edit()
    def confirmDiscard(self, confirmed):
        if getattr(self.service, "is_project_service", False):
            return
        pending, self._pending_action = self._pending_action, None
        self.changed.emit()
        if not confirmed or not pending:
            return
        action, value = pending
        if action == "new":
            super().newDocument()
        elif action == "open":
            # Failed loading leaves both document and dirty flag untouched.
            super().openPath(value)
        elif action == "close":
            self.closeApproved.emit()

    @Slot(result=bool)
    @_project_edit(False)
    def editMaterialSnapshot(self):
        return self._material_library.loadSnapshot(self._material) if self._material else False

    @Slot()
    @_project_edit()
    def clearMaterial(self):
        if self._material is not None:
            self._material = None
            self._material_validated = False
            self._material_reference_sections = []
            self._reference_loading = False
            self._reference_generation += 1
            self._thermochemistry = None
            self._edited()
            self._set_status("已移除材料快照及其文献参考场景；当前发射参数保持不变")

    def _select_material(self, material):
        from copy import deepcopy
        from coat_sim.core.materials import validate_material_profile
        # Public/direct inputs stay strict and independent of their caller.
        material = deepcopy(validate_material_profile(material))
        self._adopt_material(material)

    @Slot(object)
    @_project_edit()
    def _select_prepared_material(self, material):
        # Internal library signal only: the worker or draftData has normalized
        # this owned record. Never expose or mutate its nested evidence in UI.
        self._adopt_material(material, prepared_sections=self._material_library._sections)

    def _adopt_material(self, material, prepared_sections=None):
        from coat_sim.core.materials import source_bindable
        if not source_bindable(material):
            self._set_status("此记录不是蒸发薄膜材料；源与当前内容保持不变")
            return
        self._material = material
        self._material_validated = True
        if prepared_sections is None:
            self._refresh_material_reference()
        else:
            self._reference_generation += 1
            self._reference_loading = False
            self._material_reference_sections = prepared_sections
        if material.get("reference_data", {}).get("material_id") != "ZnS":
            self._thermochemistry = None
        kind = self._emission["kind"]
        if kind != "uniform":
            model = self._captured_material_model(kind)
            self._read_emission(model)
            prefix = "electron_beam" if kind == "electron_beam" else "resistance"
            self._fields["source_exponent"] = str(material[prefix + "_model"]["defaults"]["source_exponent"])
            self._emission_present = True
        self._preview_rate = self._emission["reference_rate_nm_s"]
        self._status = "已嵌入完整材料快照；请检查当前模型、源尺寸与标定范围"
        self._edited()
        self.documentChanged.emit()

    def _captured_material_model(self, kind):
        from copy import deepcopy
        if kind not in ("resistance_boat", "electron_beam"):
            raise ValueError("材料模型须选择电阻舟或电子束")
        field = "resistance_model" if kind == "resistance_boat" else "electron_beam_model"
        captured = self._material[field]
        model = dict(captured["defaults"])
        model.pop("source_exponent")
        model.update(kind=kind, provenance=deepcopy(captured["provenance"]))
        return model

    def _validation_document(self):
        if not self._material_validated:
            return self._compose_document()
        data = self._compose_document(include_material=False)
        if self._thermochemistry is not None:
            from coat_sim.core.material_physics import validate_thermochemistry
            if (self._material or {}).get("reference_data", {}).get("material_id") != "ZnS":
                raise ValueError("ZnS文献参考场景须绑定相应材料快照")
            validate_thermochemistry(self._thermochemistry)
        return data

    @Slot(str, str)
    @_project_edit()
    def setEmissionField(self, key, value):
        if key not in self._emission or self._emission[key] == value:
            return
        self._emission[key] = value
        self._emission_present = True
        if key == "kind":
            self._fields["device_type"] = "custom" if value == "uniform" else value
        if key == "kind" and self._material is not None and value != "uniform":
            self._read_emission(self._captured_material_model(value))
            prefix = "electron_beam" if value == "electron_beam" else "resistance"
            self._fields["source_exponent"] = str(self._material[prefix + "_model"]["defaults"]["source_exponent"])
            self.documentChanged.emit()
        self._edited()

    @Slot(str)
    @_project_edit()
    def setPreviewRate(self, value):
        self._preview_rate = value
        self.sourceDataChanged.emit()

    def _sample_result(self) -> dict:
        cache_key = (tuple(self._fields.items()), tuple(self._emission.items()),
                     tuple(tuple(row.items()) for row in self._apertures),
                     self._emission_present, self._preview_rate)
        if self._sample_cache is not None and self._sample_cache[0] == cache_key:
            return self._sample_cache[1]
        try:
            from coat_sim.core.simulator import Settings
            from coat_sim.core.source_profiles import source_samples, effective_source_exponent
            # The editor already owns explicit captured coefficients. Immutable
            # research/optical records do not affect this spatial preview and
            # must not be recopied/revalidated for each canvas property read.
            data = self._compose_document(include_material=False)
            rate = _number(self._preview_rate, "预览速率") / 10
            settings = Settings(source_x_mm=0, source_y_mm=0, source_exponent=data["source_exponent"])
            positions, weights = source_samples(settings, data, rate_nm_s=rate)
            exponent = effective_source_exponent(settings, data, rate_nm_s=rate)
            result = {"points_xy": positions[:, :2].tolist(), "weights": weights.tolist(),
                      "count": len(weights), "exponent": exponent, "error": ""}
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            result = {"points_xy": [], "weights": [], "count": 0, "error": str(exc)}
        self._sample_cache = (cache_key, result)
        return result

    @Property("QVariantList", notify=sourceDataChanged)
    def samplePoints(self) -> list[dict]:
        result = self._sample_result()
        return [{"x": float(x), "y": float(y), "weight": float(weight)}
                for (x, y), weight in zip(result["points_xy"], result["weights"], strict=False)]

    @Property(int, notify=sourceDataChanged)
    def sampleCount(self) -> int:
        return int(self._sample_result()["count"])

    @Property(str, notify=sourceDataChanged)
    def previewSummary(self):
        result = self._sample_result()
        if result["error"]:
            return "预览不可用：" + result["error"]
        return f'{result["count"]} 个源元 · 有效角指数 {result["exponent"]:.3g} · 点大小表示相对权重'

    @Slot(str, str)
    @_project_edit()
    def setField(self, key: str, value: str) -> None:
        if key not in self._fields or self._fields[key] == value:
            return
        self._fields[key] = value
        if key == "device_type":
            presets = {
                "resistance_boat": ("rectangle", {"length_mm": "40", "width_mm": "10", "angle_deg": "0"}),
                "crucible": ("disk", {"radius_mm": "12"}),
                "electron_beam": ("disk", {"radius_mm": "8"}),
            }
            if value in presets:
                shape, parameters = presets[value]
                self._fields.update(kind=shape, **parameters)
                if value in ("resistance_boat", "electron_beam"):
                    self.setEmissionField("kind", value)
                self.documentChanged.emit()
                self._set_status("已填入示例尺寸；请按实际设备调整，示例未经标定")
        self._edited()

    def _compose_emission(self):
        from .source_materials import RATE_KEYS
        fields = dict(self._emission)
        kind = fields["kind"]
        order = _number(fields["quadrature_order"], "采样阶数")
        if not order.is_integer():
            raise ValueError("采样阶数须为整数")
        model = {"kind": kind, "quadrature_order": int(order)}
        keys = ["reference_rate_nm_s", "rate_exponent_per_decade", "valid_rate_min_nm_s", "valid_rate_max_nm_s"]
        if kind == "resistance_boat":
            keys += ["active_fraction", "rate_active_fraction_per_decade", "center_weight", "cover_height_mm", "cover_thickness_mm"]
            model["boat_mode"] = fields["boat_mode"]
            model["apertures"] = [{key: value if key == "kind" else _number(value, "开口 " + key)
                                    for key, value in row.items()} for row in self._apertures]
        elif kind == "electron_beam":
            keys += ["spot_sigma_mm", "rate_spot_sigma_per_decade", "scan_width_mm", "scan_height_mm", "scan_center_x_mm", "scan_center_y_mm", "scan_angle_deg", "dwell_center_fraction"]
            model["scan_pattern"] = fields["scan_pattern"]
            points = []
            if fields["dwell_points"].strip():
                for line in fields["dwell_points"].splitlines():
                    if not line.strip():
                        continue
                    values = line.replace("，", ",").split(",")
                    if len(values) != 3:
                        raise ValueError("驻留点每行须为 X, Y, 权重")
                    points.append(dict(zip(("x_mm", "y_mm", "weight"),
                                           (_number(v, "驻留点") for v in values), strict=False)))
            model["dwell_points"] = points
        for key in keys:
            value = fields[key]
            model[key] = None if not value.strip() and key.startswith("valid_rate_") else _number(value, key) / (10 if key in RATE_KEYS else 1)
        model["provenance"] = {key: fields["provenance_" + key] for key in ("status", "reference", "notes")}
        return model

    def _compose_document(self, *, include_material=True) -> dict:
        from copy import deepcopy
        fields = self._fields
        shape = fields["kind"]
        data = {"schema_version": 2, "kind": shape,
                "device_type": fields["device_type"],
                "source_exponent": _number(fields["source_exponent"], "源角指数")}
        if fields["sampling_order"].strip():
            order = _number(fields["sampling_order"], "源空间采样阶数")
            if not order.is_integer():
                raise ValueError("源空间采样阶数须为整数")
            data["sampling_order"] = int(order)
        if shape in ("line", "rectangle"):
            data.update(length_mm=_number(fields["length_mm"],
                                          "矩形长" if shape == "rectangle" else "线长"),
                        angle_deg=_number(fields["angle_deg"], "源方向"))
            if shape == "rectangle":
                data["width_mm"] = _number(fields["width_mm"], "矩形宽")
        elif shape == "disk":
            data["radius_mm"] = _number(fields["radius_mm"], "圆半径")
        elif shape == "annulus":
            data.update(inner_radius_mm=_number(fields["inner_radius_mm"], "内半径"),
                        outer_radius_mm=_number(fields["outer_radius_mm"], "外半径"))
        elif shape != "point":
            raise ValueError("请选择有效的源形状")
        if include_material and self._material is not None:
            data["material"] = deepcopy(self._material)
        if self._emission_present:
            data["emission_model"] = self._compose_emission()
        if include_material and self._thermochemistry is not None:
            data["thermochemistry"] = deepcopy(self._thermochemistry)
        data.update(description=self.description, notes=self.notes)
        # Optional display metadata is independent of geometry and emission.
        # Keep an older unnamed source unnamed until a user supplies a name.
        if fields.get("name", ""):
            data["name"] = fields["name"]
        return data

class SubstrateBridge(_EditorBridge):
    """Substrate profile and editable outline; service supplies curve/sag samples."""

    substrateDataChanged = Signal()
    kind = "substrate"

    def _document_name(self) -> str:
        return self._fields.get("name", "")

    def __init__(self, service: CoatService | None = None,
                 initial_path: str | None = None):
        super().__init__(service, initial_path)
        self.changed.connect(self.substrateDataChanged.emit)
        factory = getattr(self.service, "material_library_bridge", None)
        if callable(factory):
            self._material_library = factory(self, "substrate")
        else:
            from .source_materials import MaterialLibraryBridge
            self._material_library = MaterialLibraryBridge(self, purpose="substrate")
        _connect_material_library(self, self._material_library)

    @Property(QObject, constant=True)
    def materialLibrary(self):
        return self._material_library

    @Property(str, notify=substrateDataChanged)
    def materialName(self):
        return self._material["name"] if self._material else "未选择基片材料"

    @Property(bool, notify=substrateDataChanged)
    def hasMaterial(self):
        return self._material is not None

    @Slot(result=bool)
    @_project_edit(False)
    def editMaterialSnapshot(self):
        return self._material_library.loadSnapshot(self._material) if self._material else False

    @Slot()
    @_project_edit()
    def clearMaterial(self):
        if self._material is not None:
            self._material = None
            self._material_validated = False
            self._material_reference_sections = []
            self._reference_loading = False
            self._reference_generation += 1
            self._edited()
            self._set_status("已移除基片材料快照；几何保持不变")

    def _select_material(self, material):
        from copy import deepcopy
        from coat_sim.core.materials import validate_material_profile
        self._adopt_material(deepcopy(validate_material_profile(material)))

    @Slot(object)
    @_project_edit()
    def _select_prepared_material(self, material):
        self._adopt_material(material, prepared_sections=self._material_library._sections)

    def _adopt_material(self, material, prepared_sections=None):
        from coat_sim.core.materials import substrate_bindable
        if not substrate_bindable(material):
            self._set_status("此材料没有基片用途；当前基片设计保持不变")
            return
        self._material = material
        self._material_validated = True
        if prepared_sections is None:
            self._refresh_material_reference()
        else:
            self._reference_generation += 1
            self._reference_loading = False
            self._material_reference_sections = prepared_sections
        self._status = "已嵌入基片材料与光学常数快照；请核对适用波段和实验条件"
        self._edited()
        self.documentChanged.emit()

    def _validation_document(self):
        return self._compose_document(include_material=not self._material_validated)

    def _reset_document(self) -> None:
        self._material = None
        self._material_validated = False
        self._material_reference_sections = []
        self._reference_loading = False
        self._reference_generation += 1
        self._fields = {"description": "", "notes": "",
            "name": "新建基片", "outline_kind": "circle", "surface_kind": "flat",
            "radius_mm": "150", "width_mm": "300", "height_mm": "300",
            "roc_mm": "600", "conic_k": "0", "a4": "0", "a6": "0", "a8": "0",
        }
        self._points = [[-100.0, -100.0], [100.0, -100.0],
                        [100.0, 100.0], [-100.0, 100.0]]
        self._closed = True
        self._selected = -1
        self._drag_index = -1

    def _load_document(self, data: dict) -> None:
        from copy import deepcopy
        from coat_sim.core.substrate_profiles import validate_substrate_profile
        data = validate_substrate_profile(data)
        self._reset_document()
        self._material = deepcopy(data.get("material"))
        self._material_validated = self._material is not None
        self._refresh_material_reference()
        self._fields["name"] = str(data.get("name", "新建基片"))
        for key in ("description", "notes"):
            self._fields[key] = str(data.get(key, ""))
        outline = data.get("outline", {})
        surface = data.get("surface", {})
        self._fields["outline_kind"] = str(outline.get("kind", "circle"))
        self._fields["surface_kind"] = str(surface.get("kind", "flat"))
        for key in ("radius_mm", "width_mm", "height_mm"):
            if key in outline:
                self._fields[key] = str(outline[key])
        for key in ("conic_k", "a4", "a6", "a8"):
            if key in surface:
                self._fields[key] = str(surface[key])
        if "radius_mm" in surface:
            self._fields["roc_mm"] = str(surface["radius_mm"])
        points = outline.get("points_mm", outline.get("control_points_mm"))
        if isinstance(points, list):
            self._points = [[float(x), float(y)] for x, y in points]

    @Property("QVariantMap", notify=substrateDataChanged)
    def fields(self) -> dict:
        return dict(self._fields)

    @Property("QVariantList", notify=substrateDataChanged)
    def points(self) -> list[dict]:
        return [{"x": x, "y": y} for x, y in self._points]

    @Property("QVariantList", notify=substrateDataChanged)
    def outlinePoints(self) -> list[dict]:
        points = self._points
        if (self._fields["outline_kind"] == "curve" and self._closed
                and len(points) >= 3):
            try:
                points = self.service.sample_substrate_curve(points)
            except (ValueError, TypeError, AttributeError):
                pass
        return [{"x": float(x), "y": float(y)} for x, y in points]

    @Property("QVariantList", notify=substrateDataChanged)
    def sectionPoints(self) -> list[dict]:
        try:
            surface = self._surface()
            radius = self._preview_radius()
            points = self.service.surface_profile(surface, radius, 181)
            return [{"x": float(x), "y": float(y)} for x, y in points]
        except (ValueError, TypeError, AttributeError):
            return []

    @Property(float, notify=substrateDataChanged)
    def previewRadius(self) -> float:
        return self._preview_radius()

    @Property(bool, notify=substrateDataChanged)
    def closed(self) -> bool:
        return self._closed

    @Property(int, notify=substrateDataChanged)
    def selectedIndex(self) -> int:
        return self._selected

    @Slot(str, str)
    @_project_edit()
    def setField(self, key: str, value: str) -> None:
        if key not in self._fields or self._fields[key] == value:
            return
        self._fields[key] = value
        if key == "outline_kind":
            self._selected = -1
            self._drag_index = -1
        self._edited()

    def _surface(self) -> dict:
        kind = self._fields["surface_kind"]
        surface = {"kind": kind}
        if kind in ("spherical", "asphere"):
            surface["radius_mm"] = _number(self._fields["roc_mm"], "曲率半径")
        if kind == "asphere":
            for key, label in (("conic_k", "圆锥常数"), ("a4", "A4"),
                               ("a6", "A6"), ("a8", "A8")):
                surface[key] = _number(self._fields[key], label)
        return surface

    def _compose_document(self, *, include_material=True) -> dict:
        kind = self._fields["outline_kind"]
        if kind == "circle":
            outline = {"kind": kind,
                       "radius_mm": _number(self._fields["radius_mm"], "圆半径")}
        elif kind == "rectangle":
            outline = {"kind": kind,
                       "width_mm": _number(self._fields["width_mm"], "矩形长"),
                       "height_mm": _number(self._fields["height_mm"], "矩形宽")}
        elif kind in ("polygon", "curve"):
            if not self._closed:
                raise ValueError("轮廓尚未闭合；点击闭合轮廓后才能保存")
            outline = {"kind": kind,
                       ("points_mm" if kind == "polygon" else "control_points_mm"):
                       [point[:] for point in self._points]}
        else:
            raise ValueError("请选择有效的轮廓类型")
        from copy import deepcopy
        data = {"schema_version": 1, "name": self._fields["name"],
                "outline": outline, "surface": self._surface(),
                "description": self.description, "notes": self.notes}
        if include_material and self._material is not None:
            data["material"] = deepcopy(self._material)
        return data

    def _preview_radius(self) -> float:
        try:
            kind = self._fields["outline_kind"]
            if kind == "circle":
                radius = _number(self._fields["radius_mm"], "圆半径")
            elif kind == "rectangle":
                radius = math.hypot(_number(self._fields["width_mm"], "矩形长") / 2,
                                    _number(self._fields["height_mm"], "矩形宽") / 2)
            else:
                radius = max((math.hypot(x, y) for x, y in self._points), default=1)
            return max(radius, 1.0)
        except ValueError:
            return 150.0

    @Slot(int)
    @_project_edit()
    def selectPoint(self, index: int) -> None:
        self._selected = index if 0 <= index < len(self._points) else -1
        self.changed.emit()

    @Slot(str, str, result=bool)
    @_project_edit()
    def addPoint(self, x: str, y: str) -> bool:
        try:
            point = [_number(x, "顶点 X"), _number(y, "顶点 Y")]
        except ValueError as exc:
            self._set_status(str(exc))
            return False
        index = self._selected + 1 if self._selected >= 0 else len(self._points)
        self._points.insert(index, point)
        self._selected = index
        self._edited()
        return True

    @Slot(str, str, result=bool)
    @_project_edit()
    def updateSelectedPoint(self, x: str, y: str) -> bool:
        if not 0 <= self._selected < len(self._points):
            self._set_status("请先选择一个顶点。")
            return False
        try:
            self._points[self._selected] = [_number(x, "顶点 X"),
                                            _number(y, "顶点 Y")]
        except ValueError as exc:
            self._set_status(str(exc))
            return False
        self._edited()
        return True

    @Slot()
    @_project_edit()
    def deleteSelectedPoint(self) -> None:
        if 0 <= self._selected < len(self._points):
            self._points.pop(self._selected)
            self._selected = min(self._selected, len(self._points) - 1)
            self._edited()

    @Slot()
    @_project_edit()
    def closeOutline(self) -> None:
        if len(self._points) < 3:
            self._set_status("至少需要 3 个控制点才能闭合轮廓。")
            return
        self._closed = True
        self._edited()
        self._set_status("轮廓已闭合；保存时会检查面积和交叉。")

    def _view(self, width: float, height: float) -> tuple[float, float, float]:
        top = height * 0.64
        radius = self._preview_radius()
        scale = min((width - 75) / (2.2 * radius), (top - 70) / (2.2 * radius))
        return width / 2, top / 2 + 5, max(scale, 1e-9)

    def _nearest_point(self, x: float, y: float, view: tuple[float, float, float]) -> int:
        cx, cy, scale = view
        if not self._points:
            return -1
        distances = [math.hypot(x - (cx + px * scale), y - (cy - py * scale))
                     for px, py in self._points]
        index = min(range(len(distances)), key=distances.__getitem__)
        return index if distances[index] <= 13 else -1

    def _nearest_edge(self, x: float, y: float,
                      view: tuple[float, float, float]) -> int:
        if not self._closed or len(self._points) < 3:
            return -1
        cx, cy, scale = view
        best_index, best_distance = -1, 13.0
        for index, (ax, ay) in enumerate(self._points):
            bx, by = self._points[(index + 1) % len(self._points)]
            ax, ay = cx + ax * scale, cy - ay * scale
            bx, by = cx + bx * scale, cy - by * scale
            dx, dy = bx - ax, by - ay
            fraction = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) /
                                     max(dx * dx + dy * dy, 1e-12)))
            distance = math.hypot(x - (ax + fraction * dx),
                                  y - (ay + fraction * dy))
            if distance < best_distance:
                best_index, best_distance = index, distance
        return best_index

    @Slot(float, float, float, float, str)
    @_project_edit()
    def canvasPress(self, x: float, y: float, width: float, height: float,
                    tool: str) -> None:
        if self._fields["outline_kind"] not in ("polygon", "curve"):
            return
        if y < 0 or y > height * 0.64 or width < 120 or height < 120:
            return
        view = self._view(width, height)
        if tool == "close":
            self.closeOutline()
            return
        if tool == "add":
            cx, cy, scale = view
            self.addPoint(str((x - cx) / scale), str((cy - y) / scale))
            return
        if tool == "delete_line":
            edge = self._nearest_edge(x, y, view)
            if edge < 0:
                self._set_status("请在闭合轮廓的一条控制边上单击。")
                return
            self._points = self._points[edge + 1:] + self._points[:edge + 1]
            self._closed = False
            self._selected = -1
            self._edited()
            self._set_status("所选线段已删除；编辑后请重新闭合轮廓。")
            return
        index = self._nearest_point(x, y, view)
        if tool == "delete_point":
            self.selectPoint(index)
            self.deleteSelectedPoint()
        elif tool == "select":
            self.selectPoint(index)
            self._drag_index = index

    @Slot(float, float, float, float)
    @_project_edit()
    def canvasDrag(self, x: float, y: float, width: float, height: float) -> None:
        if self._drag_index < 0 or y < 0 or y > height * 0.64:
            return
        cx, cy, scale = self._view(width, height)
        self._points[self._drag_index] = [(x - cx) / scale, (cy - y) / scale]
        self._selected = self._drag_index
        self._edited()

    @Slot()
    @_project_edit()
    def canvasRelease(self) -> None:
        self._drag_index = -1


@dataclass
class EditorHandle:
    """Retain QML context, component and bridge for a visible editor window."""

    window: Any
    bridge: _EditorBridge
    context: QQmlContext
    component: QQmlComponent
    engine: QQmlApplicationEngine
    application: QGuiApplication


def _open_window(qml_name: str, bridge: _EditorBridge,
                 engine: QQmlApplicationEngine | None) -> EditorHandle:
    from coat_sim.ui.settings import apply_font_settings, configure_qt_dpi_scale
    from coat_sim.ui.theme_service import ThemeService, configure_theme_from_settings

    configure_qt_dpi_scale()
    application = QGuiApplication.instance() or QGuiApplication(sys.argv)
    icon_key = "source" if qml_name == "SourceWindow.qml" else "substrate"
    application.setWindowIcon(window_icon(icon_key))
    apply_font_settings(app=application)
    if engine is None:
        configure_theme_from_settings()
        engine = QQmlApplicationEngine()
    ThemeService.instance().attach_engine(engine)
    context = QQmlContext(engine.rootContext())
    context.setContextProperty("bridge", bridge)
    path = Path(__file__).resolve().parent / "qml" / qml_name
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
    if component.isError():
        raise RuntimeError("\n".join(str(error) for error in component.errors()))
    window = component.create(context)
    if window is None:
        raise RuntimeError("\n".join(str(error) for error in component.errors()))
    window.setIcon(window_icon(icon_key))
    window.show()
    return EditorHandle(window, bridge, context, component, engine, application)


def launch_source_designer(engine: QQmlApplicationEngine | None = None,
                           service: CoatService | None = None,
                           initial_path: str | None = None) -> EditorHandle:
    return _open_window("SourceWindow.qml", SourceBridge(service, initial_path), engine)


def launch_substrate_designer(engine: QQmlApplicationEngine | None = None,
                              service: CoatService | None = None,
                              initial_path: str | None = None) -> EditorHandle:
    return _open_window("SubstrateWindow.qml", SubstrateBridge(service, initial_path), engine)


def run_source_designer(initial_path: str | None = None) -> int:
    """Standalone source editor entry point."""
    handle = launch_source_designer(initial_path=initial_path)
    return handle.application.exec()


def run_substrate_designer(initial_path: str | None = None) -> int:
    """Standalone substrate editor entry point."""
    handle = launch_substrate_designer(initial_path=initial_path)
    return handle.application.exec()
