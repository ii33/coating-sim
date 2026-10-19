"""Qt Quick state and stable-row models for thin-film design editing.

This module intentionally has no dependency on the legacy QWidget film window.
Document normalization, expression expansion, file I/O, and SQLite persistence
remain owned by :mod:`coat_sim.core.film_design_service`.
"""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.project_paths import project_default_directory
from coat_sim.core.film_design_templates import template_catalog, template_roles
from coat_sim.core.film_design_reference_recipes import (
    reference_recipe_catalog,
)
from coat_sim.core.film_design_session import (
    ExpressionTaskRequest,
    FilmDesignEditSession,
    SessionChange,
    SessionSnapshot,
)
import math
from pathlib import Path
import uuid
import weakref

from PySide6.QtCore import (
    QAbstractListModel,
    QCoreApplication,
    QModelIndex,
    QObject,
    Property,
    QRunnable,
    QSettings,
    QThreadPool,
    QUrl,
    Qt,
    Signal,
    Slot,
)


_ROW_ROLES = {
    "stableId": Qt.ItemDataRole.UserRole + 1,
    "symbol": Qt.ItemDataRole.UserRole + 2,
    "materialId": Qt.ItemDataRole.UserRole + 3,
    "opticalSetId": Qt.ItemDataRole.UserRole + 4,
    "thicknessValue": Qt.ItemDataRole.UserRole + 5,
    "thicknessText": Qt.ItemDataRole.UserRole + 6,
    "thicknessUnit": Qt.ItemDataRole.UserRole + 7,
    "physicalNmText": Qt.ItemDataRole.UserRole + 8,
    "qwText": Qt.ItemDataRole.UserRole + 9,
    "derivedReason": Qt.ItemDataRole.UserRole + 10,
    "note": Qt.ItemDataRole.UserRole + 11,
    "physicalNmValue": Qt.ItemDataRole.UserRole + 12,
}
_ROLE_NAME_BY_ID = {int(role): name for name, role in _ROW_ROLES.items()}
_ROLE_ID_BY_NAME = {name: int(role) for name, role in _ROW_ROLES.items()}
_AIR_MATERIAL_ID = "builtin_ideal_air"
_AIR_OPTICAL_SET_ID = "ideal_air_n1_k0"


def _finite_nonnegative(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite nonnegative number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite nonnegative number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a finite nonnegative number")
    return number


class FilmDesignRowsModel(QAbstractListModel):
    """Editable film-layer rows addressed by persistent IDs, never row numbers."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []
        self._row_by_id: dict[str, int] = {}

    def roleNames(self) -> dict[int, bytes]:
        return {int(role): name.encode("ascii") for name, role in _ROW_ROLES.items()}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        role_name = _ROLE_NAME_BY_ID.get(int(role))
        if role_name is None:
            return None
        return row.get(role_name)

    @staticmethod
    def _as_row(record: dict, stable_id: str | None = None) -> dict:
        thickness = record.get("thickness", {})
        value = thickness.get("value", 100)
        try:
            number = float(value)
            thickness_text = format(number, ".12g")
        except (TypeError, ValueError):
            number, thickness_text = 100.0, str(value)
        return {
            "stableId": stable_id or str(record.get("id") or uuid.uuid4().hex),
            "symbol": str(record.get("symbol", "")),
            "materialId": str(record.get("material_id", "")),
            "opticalSetId": str(record.get("optical_set_id", "")),
            "thicknessValue": number,
            "thicknessText": thickness_text,
            "thicknessUnit": str(thickness.get("unit", "nm")),
            "physicalNmValue": None,
            "physicalNmText": "不可用",
            "qwText": "不可用",
            "derivedReason": "",
            "note": str(record.get("note", "")),
        }

    def replace(self, records: list[dict], *, stable_ids: dict[str, str] | None = None,
                derived_values: dict[str, tuple[float | None, str, str]] | None = None) -> None:
        self.beginResetModel()
        self._rows = []
        for record in records:
            key = str(record.get("symbol", ""))
            row = self._as_row(record, (stable_ids or {}).get(key))
            derived = (derived_values or {}).get(str(record.get("id") or key))
            if derived is not None:
                row["physicalNmValue"] = derived[0]
                row["physicalNmText"] = "n不可用" if derived[0] is None else format(derived[0], ".3f")
                row["qwText"] = derived[1]
                row["derivedReason"] = derived[2]
            self._rows.append(row)
        self._row_by_id = {row["stableId"]: i for i, row in enumerate(self._rows)}
        self.endResetModel()

    def append_record(self, record: dict, stable_id: str | None = None) -> str:
        row = self._as_row(record, stable_id)
        position = len(self._rows)
        self.beginInsertRows(QModelIndex(), position, position)
        self._rows.append(row)
        self._row_by_id[row["stableId"]] = position
        self.endInsertRows()
        return row["stableId"]

    def insert_record(self, position: int, record: dict, stable_id: str | None = None) -> str:
        position = max(0, min(int(position), len(self._rows)))
        row = self._as_row(record, stable_id)
        self.beginInsertRows(QModelIndex(), position, position)
        self._rows.insert(position, row)
        for index in range(position, len(self._rows)):
            self._row_by_id[self._rows[index]["stableId"]] = index
        self.endInsertRows()
        return row["stableId"]

    def row_for_id(self, stable_id: str) -> int:
        return self._row_by_id.get(stable_id, -1)

    def record_for_id(self, stable_id: str) -> dict | None:
        row = self.row_for_id(stable_id)
        return deepcopy(self._rows[row]) if row >= 0 else None

    def update_field(self, stable_id: str, field: str, value: object) -> bool:
        row_number = self.row_for_id(stable_id)
        if row_number < 0:
            return False
        row = self._rows[row_number]
        if field == "material_id":
            row["materialId"] = str(value or "")
            roles = [_ROLE_ID_BY_NAME["materialId"]]
        elif field == "optical_set_id":
            row["opticalSetId"] = str(value or "")
            roles = [_ROLE_ID_BY_NAME["opticalSetId"]]
        elif field == "thickness.value":
            number = _finite_nonnegative(value, "thickness")
            row["thicknessValue"] = number
            row["thicknessText"] = format(number, ".12g")
            roles = [_ROLE_ID_BY_NAME["thicknessValue"], _ROLE_ID_BY_NAME["thicknessText"]]
        elif field == "thickness.unit":
            if value not in ("nm", "um", "qw"):
                raise ValueError("layer thickness unit must be nm, um, or qw")
            row["thicknessUnit"] = str(value)
            roles = [_ROLE_ID_BY_NAME["thicknessUnit"]]
        elif field == "note":
            row["note"] = str(value)
            roles = [_ROLE_ID_BY_NAME["note"]]
        elif field == "symbol":
            row["symbol"] = str(value)
            roles = [_ROLE_ID_BY_NAME["symbol"]]
        else:
            raise ValueError(f"unsupported layer field: {field}")
        index = self.index(row_number, 0)
        self.dataChanged.emit(index, index, roles)
        return True

    def update_derived(self, stable_id: str, *, physical_nm: float | None, qw: str,
                       reason: str) -> None:
        row_number = self.row_for_id(stable_id)
        if row_number < 0:
            return
        row = self._rows[row_number]
        physical_text = "n不可用" if physical_nm is None else format(physical_nm, ".3f")
        if (row["physicalNmValue"], row["physicalNmText"], row["qwText"], row["derivedReason"]) == (
            physical_nm, physical_text, qw, reason
        ):
            return
        row["physicalNmValue"] = physical_nm
        row["physicalNmText"], row["qwText"], row["derivedReason"] = physical_text, qw, reason
        index = self.index(row_number, 0)
        self.dataChanged.emit(index, index, [
            _ROW_ROLES["physicalNmText"], _ROW_ROLES["qwText"], _ROW_ROLES["derivedReason"],
            _ROW_ROLES["physicalNmValue"],
        ])

    def update_thickness_source(self, stable_id: str, value: float, unit: str) -> bool:
        row_number = self.row_for_id(stable_id)
        if row_number < 0:
            return False
        row = self._rows[row_number]
        row["thicknessValue"] = value
        row["thicknessText"] = format(value, ".12g")
        row["thicknessUnit"] = unit
        index = self.index(row_number, 0)
        self.dataChanged.emit(index, index, [
            _ROW_ROLES["thicknessValue"], _ROW_ROLES["thicknessText"], _ROW_ROLES["thicknessUnit"]
        ])
        return True

    def remove_id(self, stable_id: str) -> bool:
        row = self.row_for_id(stable_id)
        if row < 0:
            return False
        self.beginRemoveRows(QModelIndex(), row, row)
        self._rows.pop(row)
        self._row_by_id.pop(stable_id, None)
        for index in range(row, len(self._rows)):
            self._row_by_id[self._rows[index]["stableId"]] = index
        self.endRemoveRows()
        return True

    def move_id(self, stable_id: str, step: int) -> bool:
        source = self.row_for_id(stable_id)
        if source < 0 or step not in (-1, 1):
            return False
        return self.move_id_to(stable_id, source + step)

    def move_id_to(self, stable_id: str, target: int) -> bool:
        source = self.row_for_id(stable_id)
        if source < 0 or not 0 <= target < len(self._rows) or source == target:
            return False
        destination_child = target if target < source else target + 1
        if not self.beginMoveRows(QModelIndex(), source, source, QModelIndex(), destination_child):
            return False
        row = self._rows.pop(source)
        self._rows.insert(target, row)
        self._row_by_id[row["stableId"]] = target
        for index in range(min(source, target), len(self._rows)):
            self._row_by_id[self._rows[index]["stableId"]] = index
        self.endMoveRows()
        return True

    def ids(self) -> list[str]:
        return [row["stableId"] for row in self._rows]


class _FilmTaskSignals(QObject):
    finished = Signal(str, object, object)


class _FilmTask(QRunnable):
    def __init__(self, task_id: str, callback) -> None:
        super().__init__()
        self.task_id = task_id
        self.callback = callback
        self.signals = _FilmTaskSignals()
        self.setAutoDelete(False)

    @Slot()
    def run(self) -> None:
        try:
            self.signals.finished.emit(self.task_id, self.callback(), None)
        except Exception as exc:
            self.signals.finished.emit(self.task_id, None, (type(exc).__name__, str(exc)))


_STARTUP_WORKERS: dict[str, _FilmTask] = {}


class _FilmTaskReaper(QObject):
    """Keep startup runnables alive until queued completion is delivered."""

    @Slot(str, object, object)
    def release(self, task_id: str, _result: object, _error: object) -> None:
        _STARTUP_WORKERS.pop(task_id, None)


_STARTUP_TASK_REAPER: _FilmTaskReaper | None = None


def _startup_task_reaper() -> _FilmTaskReaper:
    global _STARTUP_TASK_REAPER
    if _STARTUP_TASK_REAPER is None:
        _STARTUP_TASK_REAPER = _FilmTaskReaper(QCoreApplication.instance())
    return _STARTUP_TASK_REAPER


class FilmQuickGeometrySettings(QObject):
    """QML-facing adapter for the film window's persisted rectangle."""

    changed = Signal()
    _PREFIX = "FilmDesignQuickWindow"

    def __init__(self, settings=None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings if settings is not None else QSettings("LYX", "coat-sim")

    def _read(self, name: str) -> float:
        try:
            return float(self._settings.value(f"{self._PREFIX}/{name}", -1))
        except (TypeError, ValueError, OverflowError):
            return -1.0

    def _write(self, name: str, value: float) -> None:
        self._settings.setValue(f"{self._PREFIX}/{name}", float(value))

    savedX = Property(float, lambda self: self._read("savedX"), notify=changed)
    savedY = Property(float, lambda self: self._read("savedY"), notify=changed)
    savedWidth = Property(float, lambda self: self._read("savedWidth"), notify=changed)
    savedHeight = Property(float, lambda self: self._read("savedHeight"), notify=changed)

    @Slot(float, float, float, float)
    def save(self, x: float, y: float, width: float, height: float) -> None:
        for name, value in (("savedX", x), ("savedY", y),
                            ("savedWidth", width), ("savedHeight", height)):
            self._write(name, value)
        self._settings.sync()
        self.changed.emit()


class FilmDesignQuickController(QObject):
    """Qt adapter for the UI-independent film-design edit session."""

    changed = Signal()
    projectNameChanged = Signal()
    uiStateChanged = Signal()
    designMetaChanged = Signal()
    workflowConfigurationChanged = Signal()
    thicknessSummaryChanged = Signal()
    errorChanged = Signal()
    savedDesignsChanged = Signal()
    expressionTemplatesChanged = Signal()
    expressionTextChanged = Signal()
    expressionStatusChanged = Signal()
    expressionBusyChanged = Signal()
    confirmationRequested = Signal(str, str)
    overwriteRequested = Signal(str)
    closeConfirmed = Signal()
    initializationChanged = Signal()
    pendingEditEpochChanged = Signal()
    optimizationApplied = Signal()
    generatorPreviewInvalidated = Signal()
    generatorUndoChanged = Signal()
    tfcalcImportChanged = Signal()
    tfcalcImportRequested = Signal()
    tfcalcImportApplied = Signal()

    _sessionChange = Signal(object)

    def __init__(self, root=None, service=None, parent: QObject | None = None,
                 geometry_settings=None, defer_initialization: bool | None = None,
                 edit_session: FilmDesignEditSession | None = None,
                 project_context=None) -> None:
        super().__init__(parent)
        if edit_session is not None:
            if service is not None and edit_session.service is not service:
                raise ValueError("Injected film session and service must have the same owner")
            service = edit_session.service
            if defer_initialization:
                raise ValueError("An owned edit session is already initialized")
        self.project_context = project_context
        project_information_changed = getattr(parent, "projectInformationChanged", None)
        if project_context is not None and project_information_changed is not None:
            project_information_changed.connect(self._project_information_changed)
        if project_context is not None:
            if edit_session is not project_context.editor or service is not project_context.service:
                raise ValueError("Project controller must observe its workspace-owned session")
        should_defer = service is None if defer_initialization is None else bool(defer_initialization)
        if service is None:
            service = FilmDesignService(root, restrict_to_root=False)
        self.service = service
        self._catalog_warning = str(getattr(service, "global_catalog_error", ""))
        self.session: FilmDesignEditSession | None = None
        self._initialization_state = "loading" if should_defer else "ready"
        self._initialization_error = ""
        self._initialization_generation = 0
        self._initialization_workers: dict[str, object] = {}
        self._initialization_task_generations: dict[str, int] = {}
        self._session_unsubscribe = None
        self._pending_edit_epoch = 0
        self._source_closed = False
        self._pending_window_open = None
        self._generator_preview: dict | None = None
        self._generator_undo_revision: int | None = None
        self._generator_undo_edit_epoch: int | None = None
        self._tfcalc_document = None
        self._tfcalc_base_design: dict | None = None
        self._tfcalc_preview = None
        self._tfcalc_confirmation_epoch = None
        self._tfcalc_state = {"loaded": False, "error": "", "ready": False}
        self._geometry_settings = FilmQuickGeometrySettings(geometry_settings, self)

        self._snapshot = SessionSnapshot(
            revision=0, design_revision=0, design=service.new_design(), path=None,
            dirty=False, message="", error="",
            derived={"front": {}, "back": {}, "symbol": {}},
            material_options={"film": [], "substrate": [], "medium": []},
            expression_text="", expression_busy=False, expression_operation="",
            pending_confirmation=None, saved_designs=(), expression_templates=(),
        )
        self._install_picker_state(self._build_picker_state(self._snapshot.material_options))

        self.front_layers = FilmDesignRowsModel(self)
        self.back_layers = FilmDesignRowsModel(self)
        self.expression_symbols = FilmDesignRowsModel(self)
        self._symbol_stable_ids: dict[str, str] = {}
        self._force_model_reset = False
        self._async_workers: dict[str, object] = {}
        self._async_meta: dict[str, ExpressionTaskRequest] = {}

        self._sessionChange.connect(self._apply_session_change)
        self._sync_models(force_reset=True)
        if should_defer:
            self._start_initialization()
        else:
            if edit_session is None:
                session, snapshot, picker_state = self._prepare_initialization(service)
            else:
                session = edit_session
                snapshot = session.snapshot()
                picker_state = self._build_picker_state(snapshot.material_options)
            self._apply_initialized_session(session, snapshot, picker_state)

    @Property(bool, constant=True)
    def projectMode(self) -> bool:
        return self.project_context is not None

    @Slot(str)
    def _project_information_changed(self, project_id: str) -> None:
        if self.project_context is None or project_id != self.project_context.binding.project_id:
            return
        try:
            self.project_context.host.store_for(self.project_context.binding)
        except ValueError:
            return
        self.projectNameChanged.emit()

    @Property(str, notify=projectNameChanged)
    def projectName(self) -> str:
        return self.project_context.name if self.project_context is not None else ""

    @Property(list, constant=True)
    def projectModules(self) -> list[dict]:
        return [dict(row) for row in self.project_context.module_options] if self.projectMode else []

    @Slot(str, result=str)
    def openProjectModule(self, module_key: str) -> str:
        opener = getattr(self.parent(), "open_project_module", None)
        if self.project_context is None or not callable(opener) or self._source_closed:
            return ""
        return str(opener(self.project_context, str(module_key)) or "")

    @Slot(result=bool)
    def closeProject(self) -> bool:
        closer = getattr(self.parent(), "close_project", None)
        if self.project_context is None or not callable(closer) or self._source_closed:
            return False
        return bool(closer(self.project_context))

    @Slot(QUrl, result=bool)
    def requestImportFile(self, url: QUrl) -> bool:
        if self.project_context is None or self.session is None or self._source_closed:
            return False
        path = url.toLocalFile() if isinstance(url, QUrl) else str(url)
        if Path(path).suffix.lower() == ".tfd":
            self.requestOpenFile(url)
            return True
        importer = getattr(self.session, "request_import_film", None)
        if not callable(importer):
            return False
        return self._replacement_call(lambda: importer(path))

    @staticmethod
    def _prepare_initialization(service):
        session = FilmDesignEditSession(service)
        snapshot = session.snapshot()
        picker_state = FilmDesignQuickController._build_picker_state(
            snapshot.material_options)
        return session, snapshot, picker_state

    @classmethod
    def _build_picker_state(cls, materials: object) -> dict:
        materials = materials if isinstance(materials, dict) else {}
        film_options = list(materials.get("film", []))
        substrate_options = list(materials.get("substrate", []))
        medium_options = list(materials.get("medium", []))
        by_id = {}
        for row in (*medium_options, *film_options, *substrate_options):
            identity = str(row.get("id", ""))
            if identity and row.get("optical_sets") and identity not in by_id:
                by_id[identity] = row
        medium_options = list(by_id.values())
        return {
            "film_options": film_options,
            "substrate_options": substrate_options,
            "medium_options": medium_options,
            "film_picker_options": [
                {"id": "", "name": "请选择材料"},
                *[cls._picker_material(row) for row in film_options],
            ],
            "substrate_picker_options": [
                {"id": "", "name": "请选择基片"},
                *[cls._picker_material(row) for row in substrate_options],
            ],
            "medium_picker_options": [cls._picker_material(row) for row in medium_options],
            "dataset_picker_sources": {
                "film": {str(row.get("id", "")): row for row in film_options},
                "substrate": {str(row.get("id", "")): row for row in substrate_options},
                "medium": {str(row.get("id", "")): row for row in medium_options},
            },
        }

    def _install_picker_state(self, state: dict) -> None:
        self.film_options = state["film_options"]
        self.substrate_options = state["substrate_options"]
        self.medium_options = state["medium_options"]
        self._film_picker_options = state["film_picker_options"]
        self._substrate_picker_options = state["substrate_picker_options"]
        self._medium_picker_options = state["medium_picker_options"]
        self._dataset_picker_sources = state["dataset_picker_sources"]
        self._dataset_picker_model_cache = {"film": {}, "substrate": {}, "medium": {}}
        self._empty_dataset_picker_model = [{"id": "", "name": "请选择光学数据集"}]
        self._no_dataset_picker_model = []
        self._picker_option_sources = {
            "film": self.film_options,
            "substrate": self.substrate_options,
            "medium": self.medium_options,
        }

    def _apply_initialized_session(self, session, snapshot, picker_state) -> None:
        self.session = session
        self._snapshot = snapshot
        self._install_picker_state(picker_state)
        self._sync_models(force_reset=True)
        controller_ref = weakref.ref(self)

        def notify_adapter(change: SessionChange) -> None:
            controller = controller_ref()
            if controller is not None and not controller._source_closed:
                controller._sessionChange.emit(change)

        unsubscribe = session.subscribe(notify_adapter)
        self._session_unsubscribe = unsubscribe
        self.destroyed.connect(lambda *_args, callback=unsubscribe: callback())
        self.changed.emit()
        self.uiStateChanged.emit()
        self.designMetaChanged.emit()
        self.thicknessSummaryChanged.emit()
        self.errorChanged.emit()
        self.savedDesignsChanged.emit()
        self.expressionTemplatesChanged.emit()
        self.expressionTextChanged.emit()
        self.expressionStatusChanged.emit()
        self.expressionBusyChanged.emit()

    def _start_initialization(self) -> None:
        self._initialization_generation += 1
        generation = self._initialization_generation
        self._initialization_error = ""
        self._initialization_state = "loading"
        self.initializationChanged.emit()
        task_id = "startup:" + uuid.uuid4().hex
        worker = _FilmTask(
            task_id, lambda service=self.service: FilmDesignQuickController._prepare_initialization(service))
        worker.signals.finished.connect(
            self._on_initialization_finished, Qt.ConnectionType.QueuedConnection)
        worker.signals.finished.connect(
            _startup_task_reaper().release, Qt.ConnectionType.QueuedConnection)
        self._initialization_workers[task_id] = worker
        self._initialization_task_generations[task_id] = generation
        _STARTUP_WORKERS[task_id] = worker
        QThreadPool.globalInstance().start(worker)

    @Slot(str, object, object)
    def _on_initialization_finished(self, task_id: str, result: object,
                                    error: object) -> None:
        self._initialization_workers.pop(task_id, None)
        generation = self._initialization_task_generations.pop(task_id, None)
        if (generation is None or generation != self._initialization_generation
                or self._initialization_state != "loading"):
            return
        if error is not None:
            self._initialization_error = (
                f"{error[0]}: {error[1]}" if isinstance(error, tuple) and len(error) == 2
                else str(error))
            self._initialization_state = "failed"
            self.initializationChanged.emit()
            return
        try:
            session, snapshot, picker_state = result
        except (TypeError, ValueError) as exc:
            self._initialization_error = f"Invalid initialization result: {exc}"
            self._initialization_state = "failed"
            self.initializationChanged.emit()
            return
        self._apply_initialized_session(session, snapshot, picker_state)
        self._initialization_error = ""
        self._initialization_state = "ready"
        self.initializationChanged.emit()
        pending, self._pending_window_open = self._pending_window_open, None
        if pending is not None:
            self.reuseWindowOpen(*pending)

    @Property(str, notify=initializationChanged)
    def initializationState(self) -> str:
        return self._initialization_state

    @Property(str, notify=initializationChanged)
    def initializationError(self) -> str:
        return self._initialization_error

    @Property(bool, notify=initializationChanged)
    def initializationReady(self) -> bool:
        return self._initialization_state == "ready"

    @Property(bool, notify=initializationChanged)
    def initializationInProgress(self) -> bool:
        return self._initialization_state == "loading"

    @Slot()
    def cancelInitialization(self) -> None:
        if self._initialization_state != "loading":
            return
        self._initialization_generation += 1
        self._initialization_state = "cancelled"
        self._initialization_error = "初始化已取消"
        self.initializationChanged.emit()

    @Slot()
    def retryInitialization(self) -> None:
        if self._initialization_state not in ("failed", "cancelled"):
            return
        self._start_initialization()

    @Property(QObject, constant=True)
    def geometrySettings(self):
        return self._geometry_settings

    @staticmethod
    def _range_text(values: object) -> str:
        if not isinstance(values, (list, tuple)) or not values:
            return ""
        finite = []
        for value in values:
            if isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number):
                finite.append(number)
        if not finite:
            return ""
        low, high = min(finite), max(finite)
        if low == high:
            return format(low, ".6g")
        return format(low, ".6g") + "–" + format(high, ".6g")

    @classmethod
    def _picker_optical_set(cls, optical: dict) -> dict:
        """Return the session's compact, array-free picker row."""
        if "value_range_text" in optical:
            return {
                "id": str(optical.get("id", "")),
                "name": str(optical.get("name") or optical.get("id") or ""),
                "kind": str(optical.get("kind", "")),
                "wavelength_min": optical.get("wavelength_min"),
                "wavelength_max": optical.get("wavelength_max"),
                "wavelength_unit": str(optical.get("wavelength_unit", "nm")),
                "value_range_text": str(optical.get("value_range_text", "")),
                "notes": str(optical.get("notes", "")),
            }
        n_range = cls._range_text(optical.get("n"))
        k_range = cls._range_text(optical.get("k"))
        values = []
        if n_range:
            values.append("n " + n_range)
        if k_range:
            values.append("k " + k_range)
        return {
            "id": str(optical.get("id", "")),
            "name": str(optical.get("name") or optical.get("id") or ""),
            "kind": str(optical.get("kind", "")),
            "wavelength_min": optical.get("wavelength_min"),
            "wavelength_max": optical.get("wavelength_max"),
            "wavelength_unit": str(optical.get("wavelength_unit", "nm")),
            "value_range_text": "; ".join(values),
            "notes": str(optical.get("notes", "")),
        }

    @staticmethod
    def _picker_material(material: dict) -> dict:
        return {
            "id": str(material.get("id", "")),
            "name": str(material.get("name") or material.get("id") or ""),
            "design_ready": material.get("design_ready"),
            "readonly": bool(material.get("readonly", False)),
        }

    @classmethod
    def _dataset_picker_models(cls, materials: list[dict], *,
                               include_placeholder: bool = True) -> dict[str, list[dict]]:
        placeholder = {"id": "", "name": "请选择光学数据集"}
        return {
            str(material.get("id", "")): (
                ([dict(placeholder)] if include_placeholder else [])
                + [cls._picker_optical_set(row)
                   for row in material.get("optical_sets", [])
                   if str(row.get("id", ""))]
            )
            for material in materials
        }

    @staticmethod
    def _preferred_optical_set_id(material: dict | None, existing_id: str = "") -> str:
        # Compatibility helper; selection is owned by the core session.
        from coat_sim.core.film_design_session import _preferred_optical_set_id
        return _preferred_optical_set_id(material, existing_id)

    def _refresh_picker_options(self) -> None:
        changed_purposes = set()
        for purpose, attribute in (("film", "film_options"),
                                   ("substrate", "substrate_options"),
                                   ("medium", "medium_options")):
            if getattr(self, attribute) is not self._picker_option_sources.get(purpose):
                changed_purposes.add(purpose)
        by_id = {}
        for row in (*self.medium_options, *self.film_options, *self.substrate_options):
            identity = str(row.get("id", ""))
            if identity and row.get("optical_sets") and identity not in by_id:
                by_id[identity] = row
        self.medium_options = list(by_id.values())
        self._film_picker_options = [
            {"id": "", "name": "请选择材料"},
            *[self._picker_material(row) for row in self.film_options],
        ]
        self._substrate_picker_options = [
            {"id": "", "name": "请选择基片"},
            *[self._picker_material(row) for row in self.substrate_options],
        ]
        self._medium_picker_options = [self._picker_material(row) for row in self.medium_options]
        self._dataset_picker_sources = {
            "film": {str(row.get("id", "")): row for row in self.film_options},
            "substrate": {str(row.get("id", "")): row for row in self.substrate_options},
            "medium": {str(row.get("id", "")): row for row in self.medium_options},
        }
        # Dataset rows are needed only for a currently selected material. Do not
        # allocate another copy of every dataset while opening the editor.
        self._dataset_picker_model_cache = {"film": {}, "substrate": {}, "medium": {}}
        self._empty_dataset_picker_model = [{"id": "", "name": "请选择光学数据集"}]
        self._no_dataset_picker_model = []
        if self.session is not None:
            for purpose in changed_purposes:
                attribute = f"{purpose}_options"
                self.session.update_material_options(purpose, getattr(self, attribute))
        self._picker_option_sources = {
            "film": self.film_options,
            "substrate": self.substrate_options,
            "medium": self.medium_options,
        }

    @staticmethod
    def _design_meta_for(snapshot) -> dict:
        design = snapshot.design
        return {
            "design_id": design.get("id", ""),
            "name": design.get("name", ""),
            "note": design.get("note", ""),
            "first_surface": design.get("first_surface", "front"),
            "reference_wavelength_nm": design.get("reference_wavelength_nm", 550.0),
            "use_k": design.get("use_k", False),
            "assume_lossless": design.get("assume_lossless", False),
            "substrate": deepcopy(design.get("substrate", {})),
            "incident_medium": deepcopy(design.get("incident_medium", {})),
            "exit_medium": deepcopy(design.get("exit_medium", {})),
            "front_layer_count": len(design.get("front_layers", [])),
            "back_layer_count": len(design.get("back_layers", [])),
            "expression_symbol_count": len(design.get("expression", {}).get("symbols", {})),
        }

    @staticmethod
    def _ui_state_for(snapshot, catalog_warning="") -> dict:
        return {
            "path": snapshot.path or "",
            "dirty": snapshot.dirty,
            "message": snapshot.message,
            "error": snapshot.error,
            "catalogWarning": str(catalog_warning or ""),
        }

    @Property("QVariantMap", notify=changed)
    def state(self) -> dict:
        snapshot = self._snapshot
        return {
            "design": deepcopy(snapshot.design),
            "path": snapshot.path or "",
            "dirty": snapshot.dirty,
            "message": snapshot.message,
            "error": snapshot.error,
            "savedDesigns": deepcopy(list(snapshot.saved_designs)),
            "expressionTemplates": deepcopy(list(snapshot.expression_templates)),
        }

    @Property("QVariantMap", notify=uiStateChanged)
    def uiState(self) -> dict:
        state = self._ui_state_for(self._snapshot, self._catalog_warning)
        state["dirty"] = state["dirty"] or bool(getattr(self.session, "_pending_information_edit", None))
        return state

    @Property("QVariantMap", notify=designMetaChanged)
    def designMeta(self) -> dict:
        return self._design_meta_for(self._snapshot)

    @Property(str, notify=errorChanged)
    def error(self) -> str:
        return self._snapshot.error

    @Property(bool, notify=uiStateChanged)
    def dirty(self) -> bool:
        return self._snapshot.dirty or bool(getattr(self.session, "_pending_information_edit", None))

    @Property(str, notify=expressionTextChanged)
    def expressionText(self) -> str:
        return self._snapshot.expression_text

    @Property(bool, notify=expressionStatusChanged)
    def expressionInSync(self) -> bool:
        return bool(self._snapshot.design.get("expression", {}).get("in_sync", False))

    @Property(bool, notify=expressionBusyChanged)
    def expressionBusy(self) -> bool:
        return self._snapshot.expression_busy

    @Property(str, notify=expressionBusyChanged)
    def expressionOperation(self) -> str:
        return self._snapshot.expression_operation

    @Property(QUrl, constant=True)
    def defaultDirectory(self) -> QUrl:
        folder = (project_default_directory(self.service.root, "film")
                  if self.projectMode else Path(self.service.root) / "data")
        return QUrl.fromLocalFile(str(folder))

    @Property(QObject, constant=True)
    def frontLayers(self) -> QObject:
        return self.front_layers

    @Property(QObject, constant=True)
    def backLayers(self) -> QObject:
        return self.back_layers

    @Property(QObject, constant=True)
    def expressionSymbols(self) -> QObject:
        return self.expression_symbols

    @Property("QVariantList", notify=initializationChanged)
    def filmOptions(self) -> list[dict]:
        return self._film_picker_options

    @Property("QVariantList", notify=initializationChanged)
    def substrateOptions(self) -> list[dict]:
        return self._substrate_picker_options

    @Property("QVariantList", notify=initializationChanged)
    def mediumOptions(self) -> list[dict]:
        return self._medium_picker_options

    @Property("QVariantList", constant=True)
    def builtinDesignTemplates(self) -> list[dict]:
        return template_catalog()

    @Property("QVariantList", constant=True)
    def builtinReferenceDesigns(self) -> list[dict]:
        return reference_recipe_catalog()

    @Property(bool, notify=generatorUndoChanged)
    def canUndoGeneratedDesign(self) -> bool:
        if (self.session is None or self._source_closed
                or self._generator_undo_revision is None):
            return False
        return (self._pending_edit_epoch == self._generator_undo_edit_epoch
                and self.session.can_undo_generated_design(self._generator_undo_revision))

    @Slot(str, result="QVariantList")
    def generatorRoles(self, template_id: str) -> list[dict]:
        try:
            return template_roles(template_id)
        except ValueError:
            return []

    @Slot()
    def clearGeneratorPreview(self) -> None:
        if self._generator_preview is not None:
            self._generator_preview = None
            self.generatorPreviewInvalidated.emit()

    @Slot("QVariantMap", result="QVariantMap")
    def generateDesignPreview(self, inputs: dict) -> dict:
        self.clearGeneratorPreview()
        if self._source_closed:
            # A closed editor must never mint a token that could later replace
            # the document. Also notify any QML preview surface to clear stale
            # display state, even when no token was present in the controller.
            self.generatorPreviewInvalidated.emit()
            return {"ok": False, "error": "当前设计页已关闭，不能生成预览。"}
        if self.session is None or self._initialization_state != "ready":
            return {"ok": False, "error": "材料目录尚未准备好，请等待膜系设计器初始化完成。"}
        if not isinstance(inputs, Mapping):
            return {"ok": False, "error": "生成参数必须是对象。"}
        try:
            normalized_inputs = dict(inputs)
            # QML JavaScript has a single numeric type, so an integral value
            # such as 8 arrives over QVariantMap as Python float 8.0. Keep the
            # core contract strict and normalize only that bridge artifact.
            pairs = normalized_inputs.get("pairs")
            if (type(pairs) is float and math.isfinite(pairs)
                    and pairs.is_integer()):
                normalized_inputs["pairs"] = int(pairs)
            preview = self.session.preview_generated_design(normalized_inputs)
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"生成预览失败：{exc}"}
        candidate = preview.pop("candidate_design")
        base_revision = int(preview.pop("base_design_revision"))
        token = uuid.uuid4().hex
        self._generator_preview = {
            "token": token,
            "candidate_design": candidate,
            "base_design_revision": base_revision,
            "base_edit_epoch": self._pending_edit_epoch,
            "material_source": getattr(self.service, "materials", None),
        }
        preview["ok"] = True
        preview["preview_token"] = token
        return preview

    @Slot("QVariantMap", result="QVariantMap")
    def previewReferenceDesign(self, inputs: dict) -> dict:
        """Create an opaque, revision-guarded preview from a public recipe."""
        self.clearGeneratorPreview()
        if self._source_closed:
            self.generatorPreviewInvalidated.emit()
            return {"ok": False, "error": "当前设计页已关闭，不能预览参考配方。"}
        if self.session is None or self._initialization_state != "ready":
            return {"ok": False, "error": "设计与材料库尚未准备好，请等待初始化完成。"}
        if not isinstance(inputs, Mapping):
            return {"ok": False, "error": "参考配方映射输入必须是对象。"}
        current = self.session.snapshot()
        if current.pending_confirmation:
            return {"ok": False, "error": "请先处理当前设计的确认操作，再预览参考配方。"}
        if current.expression_busy:
            return {"ok": False, "error": "表达式操作尚未结束，暂时不能预览参考配方。"}
        base_revision = int(current.design_revision)
        base_edit_epoch = self._pending_edit_epoch
        try:
            # Use the public service records, not the session's private catalog.
            material_source = getattr(self.service, "materials", None)
            preview = self.session.preview_reference_design(dict(inputs))
            preview_revision = int(preview.pop("base_design_revision"))
            latest = self.session.snapshot()
            if (preview_revision != base_revision or latest.design_revision != base_revision
                    or self._pending_edit_epoch != base_edit_epoch
                    or latest.pending_confirmation or latest.expression_busy
                    or material_source is not getattr(self.service, "materials", None)):
                return {"ok": False, "error": "设计状态已变化，请重新选择映射并预览。"}
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"参考配方预览失败：{exc}"}
        candidate = preview.pop("candidate_design")
        token = uuid.uuid4().hex
        self._generator_preview = {
            "token": token,
            "candidate_design": candidate,
            "base_design_revision": base_revision,
            "base_edit_epoch": base_edit_epoch,
            "kind": "reference_recipe",
            "material_source": material_source,
        }
        preview["ok"] = True
        preview["preview_token"] = token
        return preview

    @Slot(str, result=bool)
    def applyGeneratedDesign(self, preview_token: str) -> bool:
        preview = self._generator_preview
        if (self.session is None or self._source_closed or preview is None
                or str(preview.get("token", "")) != str(preview_token or "")):
            return False
        current = self.session.snapshot()
        if (current.design_revision != preview["base_design_revision"]
                or self._pending_edit_epoch != preview["base_edit_epoch"]
                or preview.get("material_source") is not getattr(self.service, "materials", None)):
            self.clearGeneratorPreview()
            return False
        if not self.session.apply_generated_design(
                preview["candidate_design"], preview["base_design_revision"]):
            self.clearGeneratorPreview()
            return False
        self._generator_preview = None
        self._generator_undo_revision = self.session.snapshot().design_revision
        self._generator_undo_edit_epoch = self._pending_edit_epoch
        self.generatorPreviewInvalidated.emit()
        self.generatorUndoChanged.emit()
        return True

    @Slot(result=bool)
    def undoGeneratedDesign(self) -> bool:
        if (self.session is None or self._generator_undo_revision is None
                or self._pending_edit_epoch != self._generator_undo_edit_epoch):
            return False
        if not self.session.undo_generated_design(self._generator_undo_revision):
            if not self.session.can_undo_generated_design(self._generator_undo_revision):
                self._generator_undo_revision = None
                self._generator_undo_edit_epoch = None
            self.generatorUndoChanged.emit()
            return False
        self._generator_undo_revision = None
        self._generator_undo_edit_epoch = None
        self.generatorUndoChanged.emit()
        return True

    @Property("QVariantList", notify=savedDesignsChanged)
    def savedDesigns(self) -> list[dict]:
        return deepcopy(list(self._snapshot.saved_designs))

    @Property("QVariantList", notify=expressionTemplatesChanged)
    def expressionTemplates(self) -> list[dict]:
        return deepcopy(list(self._snapshot.expression_templates))

    def _sync_models(self, *, force_reset: bool = False) -> None:
        design = self._snapshot.design
        self._sync_record_model(
            self.front_layers, list(design.get("front_layers", [])), force_reset=force_reset)
        self._sync_record_model(
            self.back_layers, list(design.get("back_layers", [])), force_reset=force_reset)

        symbols = design.get("expression", {}).get("symbols", {})
        if force_reset:
            self._symbol_stable_ids = {str(name): uuid.uuid4().hex for name in symbols}
            self.expression_symbols.replace(
                [dict(record, symbol=name) for name, record in symbols.items()],
                stable_ids=self._symbol_stable_ids,
            )
        else:
            target_names = set(symbols)
            for name in set(self._symbol_stable_ids) - target_names:
                self._symbol_stable_ids.pop(name, None)
            for name in target_names - set(self._symbol_stable_ids):
                self._symbol_stable_ids[name] = uuid.uuid4().hex
            entries = [(self._symbol_stable_ids[name], dict(record, symbol=name))
                       for name, record in symbols.items()]
            self._reconcile_rows(self.expression_symbols, entries)
        self._apply_derived()

    def _sync_record_model(self, model: FilmDesignRowsModel, records: list[dict],
                           *, force_reset: bool = False) -> None:
        entries = [(str(record.get("id") or ""), record) for record in records]
        if force_reset:
            model.replace(records)
        else:
            self._reconcile_rows(model, entries)

    def _reconcile_rows(self, model: FilmDesignRowsModel,
                        entries: list[tuple[str, dict]]) -> None:
        target_ids = [identity for identity, _record in entries if identity]
        target_set = set(target_ids)
        for identity in reversed(model.ids()):
            if identity not in target_set:
                model.remove_id(identity)
        for position, (identity, record) in enumerate(entries):
            if not identity:
                continue
            existing = model.row_for_id(identity)
            if existing < 0:
                model.insert_record(position, record, identity)
            elif existing != position:
                model.move_id_to(identity, position)
            current = model.record_for_id(identity) or {}
            desired = {
                "symbol": str(record.get("symbol", "")),
                "material_id": str(record.get("material_id", "")),
                "optical_set_id": str(record.get("optical_set_id", "")),
                "thickness.value": record.get("thickness", {}).get("value", 100.0),
                "thickness.unit": str(record.get("thickness", {}).get("unit", "nm")),
                "note": str(record.get("note", "")),
            }
            current_values = {
                "symbol": current.get("symbol", ""),
                "material_id": current.get("materialId", ""),
                "optical_set_id": current.get("opticalSetId", ""),
                "thickness.value": current.get("thicknessValue", 100.0),
                "thickness.unit": current.get("thicknessUnit", "nm"),
                "note": current.get("note", ""),
            }
            for field, value in desired.items():
                if current_values.get(field) != value:
                    model.update_field(identity, field, value)

    def _apply_derived(self) -> None:
        derived = self._snapshot.derived
        for side, model in (("front", self.front_layers), ("back", self.back_layers)):
            values = derived.get(side, {})
            for identity in model.ids():
                row = values.get(identity)
                if not isinstance(row, dict):
                    continue
                model.update_derived(
                    identity,
                    physical_nm=row.get("physicalNmValue"),
                    qw=str(row.get("qwText", "n不可用")),
                    reason=str(row.get("derivedReason", "")),
                )
        symbol_values = derived.get("symbol", {})
        for name, identity in self._symbol_stable_ids.items():
            row = symbol_values.get(name)
            if not isinstance(row, dict):
                continue
            self.expression_symbols.update_derived(
                identity,
                physical_nm=row.get("physicalNmValue"),
                qw=str(row.get("qwText", "n不可用")),
                reason=str(row.get("derivedReason", "")),
            )

    def _apply_session_change(self, change: SessionChange) -> None:
        previous = self._snapshot
        previous_catalog_warning = self._catalog_warning
        current = self.session.snapshot()
        self._snapshot = current
        self._catalog_warning = str(getattr(self.service, "global_catalog_error", ""))
        if self.projectMode and previous.material_options != current.material_options:
            self._install_picker_state(self._build_picker_state(current.material_options))
            self.initializationChanged.emit()
        if (self._tfcalc_document is not None and self._tfcalc_base_design != current.design):
            self._tfcalc_preview = None
            self._tfcalc_state.update(ready=False, error="设计或设置已改变，请重新打开导入文件。")
            self.tfcalcImportChanged.emit()
        if (self._generator_preview is not None
                and (current.design_revision != self._generator_preview.get("base_design_revision")
                     or self._generator_preview.get("material_source")
                     is not getattr(self.service, "materials", None))):
            self.clearGeneratorPreview()
        self.generatorUndoChanged.emit()
        categories = set(change.categories)
        if "information_buffer" in categories:
            self.uiStateChanged.emit()
        if "design" in categories or "derived" in categories:
            self._sync_models(force_reset=self._force_model_reset)
            self._force_model_reset = False
            self.thicknessSummaryChanged.emit()

        self.changed.emit()
        if previous.design.get("workflow_settings", {}) != current.design.get("workflow_settings", {}):
            self.workflowConfigurationChanged.emit()
        if (self._ui_state_for(previous, previous_catalog_warning)
                != self._ui_state_for(current, self._catalog_warning)):
            self.uiStateChanged.emit()
        if self._design_meta_for(previous) != self._design_meta_for(current):
            self.designMetaChanged.emit()
        if previous.error != current.error:
            self.errorChanged.emit()
        if tuple(previous.saved_designs) != tuple(current.saved_designs):
            self.savedDesignsChanged.emit()
        if tuple(previous.expression_templates) != tuple(current.expression_templates):
            self.expressionTemplatesChanged.emit()
        if previous.expression_text != current.expression_text:
            self.expressionTextChanged.emit()
        if (bool(previous.design.get("expression", {}).get("in_sync", False))
                != bool(current.design.get("expression", {}).get("in_sync", False))):
            self.expressionStatusChanged.emit()
        if (previous.expression_busy != current.expression_busy
                or previous.expression_operation != current.expression_operation):
            self.expressionBusyChanged.emit()
        if previous.pending_confirmation != current.pending_confirmation:
            self._publish_confirmation(current.pending_confirmation)

    def _publish_confirmation(self, pending: dict | None) -> None:
        if getattr(self.project_context, "_resource_replacement_in_progress", False):
            return
        if not pending:
            return
        if pending.get("kind") == "overwrite":
            self.overwriteRequested.emit(str(pending.get("path", "")))
            return
        self.confirmationRequested.emit(
            str(pending.get("action", "")), str(pending.get("message", "")))

    @Slot(str, bool, result=bool)
    @Slot(str, str, result=bool)
    @Slot(str, object, result=bool)
    def setDesignValue(self, field: str, value: object) -> bool:
        if self.session is None:
            return False
        return self.session.set_design_value(field, value)

    @Slot(str, str, result=bool)
    def setSubstrateValue(self, field: str, value: object) -> bool:
        if self.session is None:
            return False
        return self.session.set_substrate_value(field, value)

    @Slot(str, str, str, result=bool)
    def setMediumValue(self, medium: str, field: str, value: object) -> bool:
        if self.session is None:
            return False
        return self.session.set_medium_value(f"{medium}.{field}", value)

    @Slot(str, str, str, float, result=bool)
    @Slot(str, str, str, str, result=bool)
    @Slot(str, str, str, object, result=bool)
    def setLayerValue(self, side: str, stable_id: str, field: str, value: object) -> bool:
        if self.session is None:
            return False
        row_id = stable_id
        renamed_to = None
        if side == "symbol":
            row = self.expression_symbols.record_for_id(stable_id)
            if row is None:
                return False
            row_id = str(row.get("symbol", ""))
            if field == "symbol":
                renamed_to = str(value).strip()
                if len(renamed_to) == 1:
                    renamed_to = renamed_to.upper()
                self._symbol_stable_ids.pop(row_id, None)
                self._symbol_stable_ids[renamed_to] = stable_id
        result = self.session.set_layer_value(side, row_id, field, value)
        if side == "symbol" and not result and renamed_to is not None:
            self._symbol_stable_ids.pop(renamed_to, None)
            self._symbol_stable_ids[row_id] = stable_id
        return result

    def _dataset_picker_model_for(self, purpose: str, material_id: str) -> list[dict]:
        identity = str(material_id or "")
        cache = self._dataset_picker_model_cache[purpose]
        if identity in cache:
            return cache[identity]
        material = self._dataset_picker_sources[purpose].get(identity)
        if material is None:
            return self._empty_dataset_picker_model if purpose == "film" else self._no_dataset_picker_model
        model = ([dict(self._empty_dataset_picker_model[0])] if purpose == "film" else [])
        model.extend(
            self._picker_optical_set(row)
            for row in material.get("optical_sets", [])
            if str(row.get("id", ""))
        )
        cache[identity] = model
        return model

    @Slot(str, result="QVariantList")
    def opticalSetsFor(self, material_id: str) -> list[dict]:
        return self._dataset_picker_model_for("medium", material_id)

    @Slot(str, result="QVariantList")
    def opticalSetsForSubstrate(self, material_id: str) -> list[dict]:
        return self._dataset_picker_model_for("substrate", material_id)

    @Slot(str, result="QVariantList")
    def opticalSetsForFilm(self, material_id: str) -> list[dict]:
        return self._dataset_picker_model_for("film", material_id)

    @Slot(str, result=bool)
    def addLayer(self, side: str) -> bool:
        if self.session is None:
            return False
        return self.session.add_layer(side)

    @Slot(str, str, result=bool)
    def insertLayerAfter(self, side: str, stable_id: str) -> bool:
        if self.session is None:
            return False
        return self.session.insert_layer_after(side, stable_id)

    @Slot(str, str, result=bool)
    def deleteLayer(self, side: str, stable_id: str) -> bool:
        if self.session is None:
            return False
        return self.session.delete_layer(side, stable_id)

    @Slot(str, str, int, result=bool)
    def moveLayer(self, side: str, stable_id: str, step: int) -> bool:
        if self.session is None:
            return False
        return self.session.move_layer(side, stable_id, step)

    @Slot(str, str, int, result=bool)
    def moveLayerToIndex(self, side: str, stable_id: str, insertion_index: int) -> bool:
        if self.session is None:
            return False
        if side not in ("front", "back"):
            return False
        model = self.front_layers if side == "front" else self.back_layers
        source = model.row_for_id(stable_id)
        if source < 0 or not 0 <= insertion_index <= model.rowCount():
            return False
        return self.session.move_layer_to_index(side, stable_id, insertion_index)

    @Slot(result=bool)
    def addSymbol(self) -> bool:
        if self.session is None:
            return False
        before = set(self._snapshot.design.get("expression", {}).get("symbols", {}))
        if not self.session.add_symbol():
            return False
        symbols = self._snapshot.design.get("expression", {}).get("symbols", {})
        added = [name for name in symbols if name not in before]
        if len(added) == 1:
            symbol = added[0]
            stable_id = self._symbol_stable_ids.get(symbol)
            if stable_id and self.expression_symbols.row_for_id(stable_id) < 0:
                self.expression_symbols.append_record(dict(symbols[symbol], symbol=symbol), stable_id)
        return True

    def _symbol_name_for_id(self, stable_id: str) -> str | None:
        row = self.expression_symbols.record_for_id(stable_id)
        return str(row.get("symbol")) if row is not None else None

    @Slot(str, result=bool)
    def deleteSymbol(self, stable_id: str) -> bool:
        if self.session is None:
            return False
        symbol = self._symbol_name_for_id(stable_id)
        return False if symbol is None else self.session.delete_symbol(symbol)

    @Slot(str, result=bool)
    def setExpressionText(self, text: str) -> bool:
        if self.session is None:
            return False
        return self.session.set_expression_text(text)

    @Slot()
    def markEditingDirty(self) -> None:
        # Text fields call this as soon as the user types, before editingFinished
        # commits the new value into the session. Optimizers use this monotonic
        # epoch to reject a result that would otherwise overwrite that live draft.
        self._pending_edit_epoch += 1
        self.invalidateTfcalcPreview()
        self.pendingEditEpochChanged.emit()
        self.clearGeneratorPreview()
        self.generatorUndoChanged.emit()
        if self.session is None:
            return None
        self.session.mark_editing_dirty()

    @Property(int, notify=pendingEditEpochChanged)
    def pendingEditEpoch(self) -> int:
        return self._pending_edit_epoch

    @Slot(object, int, int, result=bool)
    def applyOptimizedDesign(self, candidate_design: object,
                             base_design_revision: int,
                             base_edit_epoch: int) -> bool:
        """Apply only if both committed and still-editing source state is current."""
        if self.session is None or self._source_closed:
            return False
        if int(base_edit_epoch) != self._pending_edit_epoch:
            return False
        apply = getattr(self.session, "apply_optimized_design", None)
        if not callable(apply):
            return False
        try:
            accepted = bool(apply(candidate_design, int(base_design_revision)))
        except (TypeError, ValueError, OverflowError):
            return False
        if accepted:
            self.optimizationApplied.emit()
        return accepted

    @Slot(result=bool)
    def normalizeExpressionText(self) -> bool:
        if self.session is None:
            return False
        request = self.session.normalize_expression()
        return True if request is None else self._start_expression_task(request)

    def _start_expression_task(self, request: ExpressionTaskRequest) -> bool:
        if self.session is None:
            return False
        task_id = str(request.token)
        if task_id in self._async_workers:
            return False
        worker = _FilmTask(task_id, lambda: self.session.execute_expression_task(request))
        worker.signals.finished.connect(self._on_expression_task_finished)
        self._async_workers[task_id] = worker
        self._async_meta[task_id] = request
        QThreadPool.globalInstance().start(worker)
        return True

    @Slot(str, object, object)
    def _on_expression_task_finished(self, task_id: str, result: object,
                                     error: object) -> None:
        if self.session is None:
            return None
        self._async_workers.pop(task_id, None)
        request = self._async_meta.pop(task_id, None)
        if request is None:
            return
        if error is not None:
            error_text = (f"{error[0]}: {error[1]}"
                          if isinstance(error, tuple) and len(error) == 2 else str(error))
            self.session.complete_expression_task(request, error=error_text)
        else:
            self.session.complete_expression_task(request, result=result)

    @Slot(str, result=bool)
    def expandExpression(self, side: str) -> bool:
        if self.session is None:
            return False
        request = self.session.expand_expression(side)
        if request is None:
            return False
        try:
            result = self.session.execute_expression_task(request)
        except Exception as exc:
            return self.session.complete_expression_task(request, error=str(exc))
        return self.session.complete_expression_task(request, result=result)

    @Slot(str, result=bool)
    def expandExpressionAsync(self, side: str) -> bool:
        if self.session is None:
            return False
        request = self.session.expand_expression(side)
        return False if request is None else self._start_expression_task(request)

    @Slot(str, str, result=bool)
    def saveExpressionTemplate(self, name: str, note: str) -> bool:
        if self.session is None:
            return False
        return self.session.save_expression_template(name, note)

    @Slot(result=bool)
    def refreshExpressionTemplatesAsync(self) -> bool:
        if self.session is None:
            return False
        request = self.session.request_refresh_expression_templates()
        return False if request is None else self._start_expression_task(request)

    @Slot(str, str, result=bool)
    def saveExpressionTemplateAsync(self, name: str, note: str) -> bool:
        if self.session is None:
            return False
        request = self.session.request_save_expression_template(name, note)
        return False if request is None else self._start_expression_task(request)

    @Slot(str, result=bool)
    def applyExpressionTemplateAsync(self, template_id: str) -> bool:
        if self.session is None:
            return False
        request = self.session.request_apply_expression_template(template_id)
        return False if request is None else self._start_expression_task(request)

    @Slot(result=bool)
    def refreshExpressionTemplates(self) -> bool:
        if self.session is None:
            return False
        return self.session.refresh_expression_templates()

    @Slot(str, result=bool)
    def applyExpressionTemplate(self, template_id: str) -> bool:
        if self.session is None:
            return False
        return self.session.apply_expression_template(template_id)

    @Slot(result=bool)
    def refreshSavedDesigns(self) -> bool:
        if self.session is None:
            return False
        return self.session.refresh_saved_designs()

    @Slot(result=bool)
    def saveToLibrary(self) -> bool:
        if self.session is None:
            return False
        return self.session.save_to_library(str(self._snapshot.design.get("name", "")))

    def _replacement_call(self, callback) -> bool:
        self._force_model_reset = True
        result = callback()
        self._force_model_reset = False
        return bool(result)

    @Slot(str, result=bool)
    def requestLoadSavedDesign(self, design_id: str) -> bool:
        if self.session is None:
            return False
        return self._replacement_call(
            lambda: self.session.request_load_saved_design(design_id))

    @Slot()
    def requestNewDesign(self) -> None:
        if self.session is None:
            return None
        self._replacement_call(self.session.request_new_design)

    @Slot()
    def requestClose(self) -> None:
        if self.session is None:
            return None
        if self.session.request_close():
            self.closeConfirmed.emit()

    @Slot(QUrl)
    def requestOpenFile(self, url: QUrl) -> None:
        if self.session is None:
            return None
        path = url.toLocalFile() if isinstance(url, QUrl) else str(url)
        if Path(path).suffix.lower() == ".tfd":
            self.inspectTfcalcFile(url)
            self.tfcalcImportRequested.emit()
            return
        self.clearTfcalcImport()
        self._replacement_call(lambda: self.session.request_open_file(path))

    @Property(dict, notify=tfcalcImportChanged)
    def tfcalcImportState(self) -> dict:
        return deepcopy(self._tfcalc_state)

    @Property(bool, notify=tfcalcImportChanged)
    def tfcalcPluginEnabled(self) -> bool:
        from coat_sim.core.tfcalc_import import PLUGIN_NAME
        registry = getattr(self.service, "plugins", None)
        if registry is None:
            return False
        try:
            return registry.is_enabled("film_importer", PLUGIN_NAME)
        except (KeyError, ValueError):
            return False

    @Slot(bool)
    def setTfcalcPluginEnabled(self, enabled: bool) -> None:
        from coat_sim.core.tfcalc_import import PLUGIN_NAME
        self.clearTfcalcImport()
        registry = getattr(self.service, "plugins", None)
        if registry is not None:
            try:
                registry.set_enabled("film_importer", PLUGIN_NAME, enabled)
            except (KeyError, ValueError, TypeError) as exc:
                self._tfcalc_state["error"] = str(exc)
        self.tfcalcImportChanged.emit()

    @Slot()
    def clearTfcalcImport(self) -> None:
        if self.session is not None:
            self.session.cancel_pending_import()
        self._tfcalc_document = self._tfcalc_base_design = self._tfcalc_preview = None
        self._tfcalc_confirmation_epoch = None
        self._tfcalc_state = {"loaded": False, "error": "", "ready": False}
        self.tfcalcImportChanged.emit()

    @Slot(QUrl, result=bool)
    def inspectTfcalcFile(self, url: QUrl) -> bool:
        self.clearTfcalcImport()
        path = url.toLocalFile() if isinstance(url, QUrl) else str(url)
        try:
            if self.session is None or self._source_closed or not self.initializationReady:
                raise ValueError("设计器尚未就绪或已经关闭。")
            document = self.service.read_tfcalc(path)
            self._tfcalc_document = document
            self._tfcalc_base_design = self.currentDesign()
            self._tfcalc_base_revision = self._snapshot.design_revision
            self._tfcalc_base_epoch = self._pending_edit_epoch
            self._tfcalc_state = {
                "loaded": True, "error": "", "ready": False, "path": path,
                "primary_count": len(document.layers), "secondary_count": len(document.secondary_layers),
                "primary_aliases": list(dict.fromkeys(row.material for row in document.layers)),
                "secondary_aliases": list(dict.fromkeys(row.material for row in document.secondary_layers)),
                "report": "\n".join(document.report),
                "environment": "\n".join("*".join(row) + "*" for row in document.environment_records),
                "formula": "\n".join(document.formulas),
                "reference_wavelength_nm": self._snapshot.design["reference_wavelength_nm"],
                "primary_rows": [{"number": row.number, "material": row.material,
                                  "value3": row.values[0], "value4": row.values[1]} for row in document.layers[:8]],
                "secondary_rows": [{"number": row.number, "material": row.material,
                                    "value3": row.values[0], "value4": row.values[1]} for row in document.secondary_layers[:8]],
            }
        except (ValueError, TypeError, OSError, PermissionError, KeyError) as exc:
            self._tfcalc_state["error"] = str(exc)
            self.tfcalcImportChanged.emit()
            return False
        self.tfcalcImportChanged.emit()
        return True

    @Slot("QVariantMap", result="QVariantMap")
    def previewTfcalcImport(self, inputs: dict) -> dict:
        self.invalidateTfcalcPreview()
        try:
            if (self.session is None or self._source_closed or self._tfcalc_document is None
                    or self._tfcalc_base_design != self.currentDesign()
                    or self._tfcalc_base_epoch != self._pending_edit_epoch):
                raise ValueError("导入预览已失效，请提交当前编辑后重新打开文件。")
            candidate = self.service.build_tfcalc_import(
                self._tfcalc_document, inputs, self._tfcalc_base_design)
            token = uuid.uuid4().hex
            self._tfcalc_preview = {"token": token, "design": candidate,
                                    "options": deepcopy(inputs)}
            side = inputs["target_side"]
            total = math.fsum(row["thickness"]["value"] * (1000 if row["thickness"]["unit"] == "um" else 1)
                              for row in candidate[side + "_layers"])
            self._tfcalc_state.update(ready=True, error="", token=token,
                                     summary=f"{side} 面 {len(candidate[side + '_layers'])} 层；总物理厚度 {total:.9g} nm。确认后替换所选面，沿用已确认的当前环境。")
        except (ValueError, TypeError, PermissionError, KeyError) as exc:
            self._tfcalc_state["error"] = str(exc)
            self.tfcalcImportChanged.emit()
            return {"ok": False, "error": str(exc)}
        self.tfcalcImportChanged.emit()
        return {"ok": True, "token": token, "summary": self._tfcalc_state["summary"]}

    @Slot()
    def invalidateTfcalcPreview(self) -> None:
        if self.session is not None:
            self.session.cancel_pending_import()
        self._tfcalc_confirmation_epoch = None
        self._tfcalc_preview = None
        self._tfcalc_state["ready"] = False
        self.tfcalcImportChanged.emit()

    @Slot(str, result=bool)
    def requestApplyTfcalcImport(self, token: str) -> bool:
        preview = self._tfcalc_preview
        if (self.session is None or self._source_closed or not self.tfcalcPluginEnabled
                or preview is None or token != preview["token"]
                or self._tfcalc_base_design != self.currentDesign()
                or self._tfcalc_base_epoch != self._pending_edit_epoch):
            self._tfcalc_state.update(ready=False, error="导入预览已失效，请重新核对。")
            self.tfcalcImportChanged.emit()
            return False
        self._tfcalc_confirmation_epoch = self._pending_edit_epoch
        return self.session.request_imported_design(preview["design"], self._tfcalc_base_revision)

    @Slot(bool)
    def resolvePendingAction(self, accepted: bool) -> None:
        if self.session is None:
            return None
        pending = self._snapshot.pending_confirmation
        action = str((pending or {}).get("action", ""))
        if action == "import_tfcalc":
            if (self._source_closed or not self.tfcalcPluginEnabled
                    or self._tfcalc_confirmation_epoch != self._pending_edit_epoch):
                accepted = False
            result = self.session.resolve_pending_action(accepted)
            if result:
                self.clearTfcalcImport()
                self.tfcalcImportApplied.emit()
            return
        replacement = accepted and action in ("new", "open_file", "load_saved", "replace_design", "project_import_film")
        if replacement:
            self._force_model_reset = True
        result = self.session.resolve_pending_action(accepted)
        if replacement:
            self._force_model_reset = False
        if accepted and result and action == "close":
            self.closeConfirmed.emit()

    @Slot(QUrl, bool, result=bool)
    def saveFile(self, url: QUrl, overwrite: bool = False) -> bool:
        if self.session is None:
            return False
        path = url.toLocalFile() if isinstance(url, QUrl) else str(url)
        return self.session.save_file(path, overwrite=bool(overwrite))

    @Slot(str, bool, result=bool)
    def saveFilePath(self, path: str, overwrite: bool = False) -> bool:
        if self.session is None:
            return False
        return self.session.save_file(path, overwrite=bool(overwrite))

    @Slot(result=bool)
    def saveCurrent(self) -> bool:
        if self.session is None:
            return False
        return self.session.save_current()

    @Slot(result="QVariantMap")
    def currentDesign(self) -> dict:
        return deepcopy(self._snapshot.design)

    @Slot(str, result="QVariantMap")
    def getWorkflowConfiguration(self, scope: str) -> dict:
        return self.session.workflow_configuration(scope) if self.session is not None else {}

    @Slot(str, "QVariantMap", result=bool)
    def setWorkflowConfiguration(self, scope: str, configuration: dict) -> bool:
        return bool(self.session is not None and
                    self.session.set_workflow_configuration(scope, configuration))

    @Property(str, notify=thicknessSummaryChanged)
    def thicknessSummaryText(self) -> str:
        values = []
        materials = {}
        face_values = {"front": [], "back": []}
        unavailable = 0
        names = {row["id"]: row.get("name", row["id"]) for row in self.film_options}
        for face in ("front", "back"):
            for layer in self._snapshot.design.get(face + "_layers", []):
                value = self._snapshot.derived.get(face, {}).get(layer.get("id"), {}).get("physicalNmValue")
                if not self._is_number(value):
                    unavailable += 1
                    continue
                value = float(value)
                values.append(value)
                face_values[face].append(value)
                material_id = layer.get("material_id", "")
                materials.setdefault(material_id, []).append(value)
        prefix = "已知膜厚合计" if unavailable else "膜厚合计"
        lines = [f"{prefix} {math.fsum(values):.3f} nm"]
        back_label = "背面设计" if self._snapshot.design.get("substrate", {}).get("sides") == "single" else "背面"
        lines.extend(f"{names.get(key, key or '未选择材料')} {math.fsum(group):.3f} nm"
                     for key, group in materials.items())
        lines.append(f"正面 {math.fsum(face_values['front']):.3f} / {back_label} {math.fsum(face_values['back']):.3f} nm")
        if unavailable:
            lines.append(f"{unavailable} 层厚度无法换算")
        return " · ".join(lines)

    @Property(str, constant=True)
    def thicknessSummaryDescription(self) -> str:
        return ("膜厚合计包含当前正/背面设计，不含基片；单面模式下背面设计会保留但不参与计算。"
                "三位小数仅用于显示，计算与文件保留完整厚度精度。")

    def reuseWindowOpen(self, document_path: str = "", payload: object = None) -> bool:
        if self.session is None:
            self._pending_window_open = (str(document_path), payload)
            return True
        if document_path:
            if (self._snapshot.path and
                    Path(str(document_path)).resolve() == Path(self._snapshot.path).resolve()):
                return True
            self.requestOpenFile(QUrl.fromLocalFile(str(document_path)))
            return bool(self._snapshot.pending_confirmation or not self.error)
        if payload is None or payload is self or getattr(payload, "session", None) is self.session:
            return True
        if isinstance(payload, dict):
            return bool(self._replacement_call(lambda: self.session.request_replace_design(payload))
                        or self._snapshot.pending_confirmation)
        return False

    @Slot(result=str)
    def openOptimization(self) -> str:
        launcher = self.parent()
        opener = getattr(launcher, "open_or_reuse_film_companion", None)
        if callable(opener) and self.session is not None:
            return str(opener("film_optimization", self) or "")
        opener = getattr(launcher, "open_window", None)
        if not callable(opener):
            return ""
        return str(opener("film_optimization", mode="new", payload=self) or "")

    @Slot(result=str)
    def openOptimizationAndStart(self) -> str:
        launcher = self.parent()
        opener = getattr(launcher, "open_or_reuse_film_companion", None)
        if callable(opener) and self.session is not None:
            return str(opener("film_optimization", self, start=True) or "")
        return self.openOptimization()

    @Slot(result=str)
    def openSimulationAndCalculate(self) -> str:
        launcher = self.parent()
        opener = getattr(launcher, "open_or_reuse_film_companion", None)
        if callable(opener) and self.session is not None:
            return str(opener("film_simulation", self, start=True) or "")
        return ""

    @Slot()
    def markSourceClosed(self) -> None:
        self._source_closed = True
        if self._session_unsubscribe is not None:
            self._session_unsubscribe()
            self._session_unsubscribe = None
        self.clearTfcalcImport()
        self._generator_preview = None
        self._generator_undo_revision = None
        self._generator_undo_edit_epoch = None
        self.generatorPreviewInvalidated.emit()
        self.generatorUndoChanged.emit()

    @Slot(str, result="QVariantList")
    def schematicLayers(self, side: str) -> list[dict]:
        if side not in ("front", "back"):
            return []
        layers = self._snapshot.design.get(side + "_layers", [])
        model = self.front_layers if side == "front" else self.back_layers
        display_by_id = {row["stableId"]: row for row in model._rows}
        by_id = {item.get("id"): item for item in self.film_options}
        result = []
        for layer in layers:
            row = display_by_id.get(layer.get("id"), {})
            material = by_id.get(layer.get("material_id"), {})
            optical = next((item for item in material.get("optical_sets", [])
                            if item.get("id") == layer.get("optical_set_id")), {})
            physical_nm_value = row.get("physicalNmValue")
            result.append({
                "id": layer.get("id", ""),
                "name": material.get("name", layer.get("material_id") or "未选择材料"),
                "setName": optical.get("name", layer.get("optical_set_id") or "未选择数据集"),
                "physicalNm": row.get("physicalNmText", "n不可用"),
                "sourceRange": self._range_label(optical),
                "unit": layer.get("thickness", {}).get("unit", "nm"),
                "physicalNmValue": physical_nm_value,
            })
        totals = []
        for face in ("front", "back"):
            face_model = self.front_layers if face == "front" else self.back_layers
            values = [float(row["physicalNmValue"]) for row in face_model._rows
                      if self._is_number(row.get("physicalNmValue"))]
            totals.append(sum(values))
        total = max(totals, default=0.0)
        for item in result:
            item["displayWidth"] = (
                280.0 * item["physicalNmValue"] / total
                if item["physicalNmValue"] is not None and total > 0 else 24.0
            )
        return result

    @staticmethod
    def _is_number(value: object) -> bool:
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            return False

    @Slot(result=str)
    def substrateLabel(self) -> str:
        substrate = self._snapshot.design.get("substrate", {})
        material = next((item for item in self.substrate_options
                         if item.get("id") == substrate.get("material_id")), {})
        optical = next((item for item in material.get("optical_sets", [])
                        if item.get("id") == substrate.get("optical_set_id")), {})
        name = material.get("name", substrate.get("material_id") or "基片未选")
        dataset = optical.get("name", substrate.get("optical_set_id") or "数据集未选")
        range_text = self._range_label(optical)
        return f"{name} / {dataset}" + (f"\n{range_text}" if range_text else "")

    @Slot(result=str)
    def substrateThicknessLabel(self) -> str:
        thickness = self._snapshot.design.get("substrate", {}).get("thickness", {})
        return f"实际厚度：{float(thickness.get('value', 1.0)):g} {thickness.get('unit', 'mm')}"

    @Slot(str, result=str)
    def mediumLabel(self, medium: str) -> str:
        value = self._snapshot.design.get(medium, {})
        if value.get("kind", "constant") == "constant":
            number = value.get("n", 1.0)
            return "Air 空气（n=1）" if float(number) == 1.0 else f"常数 n={float(number):.6g}"
        material = next((item for item in self.medium_options
                         if item.get("id") == value.get("material_id")), {})
        optical = next((item for item in material.get("optical_sets", [])
                        if item.get("id") == value.get("optical_set_id")), {})
        return (material.get("name", value.get("material_id", "未选材料")) + " / "
                + optical.get("name", value.get("optical_set_id", "未选数据集")))

    @staticmethod
    def _range_label(optical: dict) -> str:
        low, high = optical.get("wavelength_min"), optical.get("wavelength_max")
        unit = optical.get("wavelength_unit")
        if low is None or high is None or unit not in ("nm", "um"):
            return ""
        try:
            low_number, high_number = float(low), float(high)
        except (TypeError, ValueError, OverflowError):
            return ""
        if not math.isfinite(low_number) or not math.isfinite(high_number):
            return ""
        return f"{low_number:g}–{high_number:g} {unit}"

    @Slot()
    def clearError(self) -> None:
        if self.session is None:
            return None
        self.session.clear_error()

