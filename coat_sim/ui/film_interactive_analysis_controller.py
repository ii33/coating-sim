"""Temporary TFCalc-style layer analysis, owned by the design source session.

This controller never mutates the source during editing. It freezes each preview
through the existing optical service and delegates the single atomic commit to
FilmDesignQuickController.applyOptimizedDesign.
"""
from __future__ import annotations

from copy import copy, deepcopy
from decimal import Decimal, ROUND_HALF_UP
import math
import re
import threading
from typing import Any, Mapping, cast
from uuid import uuid4
import weakref

from PySide6.QtCore import QObject, Property, QThreadPool, QTimer, Signal, Slot


_MAX_QML_SAFE_INTEGER = (1 << 53) - 1
_QVARIANT_PROPERTY_TYPE = cast(type, "QVariant")


def _number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有限数字")
    try:
        result = float(cast(Any, value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是有限数字") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label}必须是有限数字")
    return result


def _integer(value: object, label: str, minimum: int = 1,
             maximum: int | None = _MAX_QML_SAFE_INTEGER) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是整数")
    try:
        result = int(str(value).strip())
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if str(value).strip() not in (str(result), f"+{result}"):
        raise ValueError(f"{label}必须是整数")
    if result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"{label}超出有效范围")
    return result


def _edited_thickness(value: object, label: str) -> float:
    result = _number(value, label)
    try:
        result = float(Decimal(str(result)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))
    except (ArithmeticError, OverflowError, ValueError) as exc:
        raise ValueError(f"{label}必须为非负有限数值") from exc
    if result < 0:
        raise ValueError(f"{label}不能为负；0 是有效厚度。")
    return result


def _simulate_interactive_spectrum(service, design: dict, settings: dict,
                                   cancel_event: threading.Event) -> dict:
    if cancel_event.is_set():
        raise InterruptedError("临时光谱预览已取消")
    frozen_service = copy(service)
    freeze_design = getattr(frozen_service, "freeze_design", None)
    if not callable(freeze_design):
        raise ValueError("当前光学服务不支持冻结材料快照，无法安全预览")
    frozen = freeze_design(deepcopy(design), deepcopy(settings))
    if not isinstance(frozen, dict) or not isinstance(frozen.get("materials"), list):
        raise ValueError("光学服务未返回有效的冻结材料快照")
    frozen_service.materials = _FrozenMaterials(frozen["materials"])
    if cancel_event.is_set():
        raise InterruptedError("临时光谱预览已取消")
    return frozen_service.simulate(deepcopy(design), deepcopy(settings), cancel_event=cancel_event)


class _FrozenMaterials:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = {str(row["id"]): deepcopy(row) for row in rows}

    def get(self, material_id: str) -> dict:
        if material_id not in self._rows:
            raise ValueError(f"frozen material missing: {material_id}")
        return deepcopy(self._rows[material_id])


class FilmInteractiveAnalysisController(QObject):
    """QML adapter for one design-session-owned, disposable analysis draft."""

    changed = Signal()
    previewChanged = Signal(object)
    _sessionChange = Signal(object)

    def __init__(self, source_controller, parent: QObject | None = None) -> None:
        super().__init__(parent or source_controller)
        self._source_controller = source_controller
        self.session = getattr(source_controller, "session", None)
        self.service = getattr(source_controller, "service", None)
        self._source_closed = False
        self._busy = False
        self._shutting_down = False
        self._interactive_active = False
        self._interactive_mode = "physical_nm"
        self._interactive_base_design: dict[str, Any] = {}
        self._interactive_replacement_design: dict[str, Any] = {}
        self._interactive_base_revision: int | None = None
        self._interactive_base_epoch: int | None = None
        self._interactive_base_design_id = ""
        self._interactive_base_physical_nm: dict[tuple[str, str], float | None] = {}
        self._interactive_base_qw: dict[tuple[str, str], float | None] = {}
        self._interactive_nm_per_qw: dict[tuple[str, str], float | None] = {}
        self._interactive_values: dict[tuple[str, str], float] = {}
        self._interactive_base_group_ids: dict[tuple[str, str], int | None] = {}
        self._interactive_group_ids: dict[tuple[str, str], int | None] = {}
        self._interactive_group_factors: dict[int, float] = {}
        self._interactive_angle_deg = 0.0
        self._interactive_base_angle_deg = 0.0
        self._interactive_base_simulation_configuration: dict[str, Any] = {}
        self._interactive_stale = False
        self._interactive_applying = False
        self._interactive_preview_spectrum: list[dict[str, float]] = []
        self._interactive_preview_generation = 0
        self._interactive_last_valid_generation = -1
        self._interactive_preview_busy = False
        self._interactive_preview_error = ""
        self._interactive_preview_status = "临时分析不会改动设计；点击‘应用到设计’后才提交。"
        self._interactive_preview_event: threading.Event | None = None
        self._interactive_preview_workers: dict[str, Any] = {}
        self._interactive_preview_worker_generations: dict[str, int] = {}
        self._interactive_preview_timer = QTimer(self)
        self._interactive_preview_timer.setSingleShot(True)
        self._interactive_preview_timer.setInterval(130)
        self._interactive_preview_timer.timeout.connect(self._start_interactive_spectrum_preview)
        self._session_unsubscribe = None
        self._sessionChange.connect(self._on_session_change)
        if self.session is not None:
            controller_ref = weakref.ref(self)
            def notify(change) -> None:
                controller = controller_ref()
                if controller is not None:
                    try:
                        controller._sessionChange.emit(change)
                    except RuntimeError:
                        return
            self._session_unsubscribe = self.session.subscribe(notify)
        for signal_name, callback in (("closeConfirmed", self.markSourceClosed),
                                      ("destroyed", self.markSourceClosed),
                                      ("pendingEditEpochChanged", self._on_source_pending_edit)):
            signal = getattr(source_controller, signal_name, None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(callback)
        from .theme_service import ThemeService
        ThemeService.instance().filmThicknessDecimalsChanged.connect(self._on_film_display_precision)

    def _on_source_pending_edit(self, *_args) -> None:
        if self._interactive_active and not self._interactive_applying \
                and self._source_edit_epoch() != self._interactive_base_epoch:
            self._mark_interactive_stale("来源设计正在编辑；临时分析已失效，没有覆盖该编辑。")

    def _on_session_change(self, _change) -> None:
        if self._interactive_active and not self._interactive_applying and not self._interactive_source_is_current():
            self._mark_interactive_stale("来源膜系或仿真设置已改变；只保留预览，不会覆盖新内容。")

    def _source_edit_epoch(self) -> int | None:
        try:
            return int(getattr(self._source_controller, "pendingEditEpoch", 0))
        except (TypeError, ValueError, RuntimeError):
            return None

    @Slot()
    def markSourceClosed(self, *_args) -> None:
        self._source_closed = True
        self.clearInteractiveAnalysis()

    @Slot()
    def _on_film_display_precision(self, _decimals: int) -> None:
        self.changed.emit()

    @Property(int, notify=changed)
    def filmThicknessDecimals(self) -> int:
        from .theme_service import ThemeService
        return ThemeService.instance().film_thickness_decimals

    @Slot(result=bool)
    def openSimulationWindow(self) -> bool:
        if self._source_closed:
            return False
        opener = getattr(self._source_controller, "openInteractiveSimulation", None)
        return bool(callable(opener) and opener())

    def _publish_preview(self, result: Mapping[Any, Any] | None) -> None:
        payload = None
        if result is not None:
            try:
                design = self._interactive_preview_design()
                settings = self._interactive_preview_settings()
                payload = {"design": design, "settings": settings,
                           "result": deepcopy(dict(result)), "label": "临时交互分析预览（未写入设计）"}
            except (ValueError, TypeError, KeyError, OverflowError):
                payload = None
        self.previewChanged.emit(payload)
        publisher = getattr(self._source_controller, "setInteractivePreview", None)
        if callable(publisher):
            try:
                publisher(payload)
            except RuntimeError:
                self._source_closed = True

    @Property(bool, notify=changed)
    def interactiveAnalysisActive(self) -> bool:
        return self._interactive_active

    @Property(str, notify=changed)
    def interactiveAnalysisMode(self) -> str:
        return self._interactive_mode

    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def interactiveAnalysisModeOptions(self) -> list[dict[str, str]]:
        return [
            {"id": "physical_nm", "label": "物理厚度（nm）"},
            {"id": "optical_qw", "label": "光学厚度（QW）"},
            {"id": "incident_angle", "label": "入射角（°）"},
            {"id": "group_coefficient", "label": "组系数"},
        ]

    @Property(bool, notify=changed)
    def interactiveAnalysisCanEdit(self) -> bool:
        return bool(self._interactive_active and not self._interactive_stale
                    and not self._source_closed and not self._busy and not self._shutting_down)

    @Property(bool, notify=changed)
    def interactiveAnalysisBusy(self) -> bool:
        return self._interactive_preview_busy

    @Property(bool, notify=changed)
    def interactiveAnalysisDirty(self) -> bool:
        if not self._interactive_active:
            return False
        for key, value in self._interactive_values.items():
            base = (self._interactive_base_qw.get(key) if self._interactive_mode == "optical_qw"
                    else self._interactive_base_physical_nm.get(key))
            if base is None or value != base:
                return True
        if any(value != 1.0 for value in self._interactive_group_factors.values()):
            return True
        if self._interactive_angle_deg != self._interactive_base_angle_deg:
            return True
        if self._interactive_group_ids != self._interactive_base_group_ids:
            return True
        base_layers = {(side, str(layer.get("id", ""))): layer
                       for side in ("front", "back")
                       for layer in self._interactive_base_design.get(side + "_layers", [])}
        replacement_layers = {(side, str(layer.get("id", ""))): layer
                              for side in ("front", "back")
                              for layer in self._interactive_replacement_design.get(side + "_layers", [])}
        if any((base_layers[key].get("material_id"), base_layers[key].get("optical_set_id"))
               != (replacement_layers[key].get("material_id"), replacement_layers[key].get("optical_set_id"))
               for key in base_layers if key in replacement_layers):
            return True
        return False

    @Property(bool, notify=changed)
    def interactiveAnalysisCanApply(self) -> bool:
        return bool(self.interactiveAnalysisCanEdit and self.interactiveAnalysisDirty
                    and not self._interactive_preview_busy
                    and self._interactive_last_valid_generation == self._interactive_preview_generation
                    and not self._interactive_preview_error and self._interactive_source_is_current())

    @Property(str, notify=changed)
    def interactiveAnalysisStatus(self) -> str:
        return self._interactive_preview_status

    @Property(str, notify=changed)
    def interactiveAnalysisError(self) -> str:
        return self._interactive_preview_error

    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def interactiveAnalysisSpectrum(self) -> list[dict[str, float]]:
        return [dict(row) for row in self._interactive_preview_spectrum]

    @Property(float, notify=changed)
    def interactiveAnalysisAngle(self) -> float:
        return float(self._interactive_angle_deg)

    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def interactiveAnalysisMaterialOptions(self) -> list[dict[str, str]]:
        rows: dict[str, dict[str, str]] = {}
        for item in getattr(self._source_controller, "film_options", []):
            material_id = str(item.get("id", ""))
            if material_id:
                rows.setdefault(material_id, {"id": material_id,
                    "name": str(item.get("name") or material_id)})
        return [{"id": "", "name": "全部材料"}, *sorted(
            rows.values(), key=lambda row: (row["name"].casefold(), row["id"]))]

    def _interactive_replacement_options(self) -> list[dict[str, str]]:
        rows = []
        for material in getattr(self._source_controller, "film_options", []):
            material_id = str(material.get("id", ""))
            material_name = str(material.get("name") or material_id)
            for optical in material.get("optical_sets", []):
                optical_id = str(optical.get("id", ""))
                if material_id and optical_id:
                    rows.append({"material_id": material_id, "optical_set_id": optical_id,
                                 "label": f"{material_name} — {optical.get('name') or optical_id}"})
        return [{"material_id": "", "optical_set_id": "", "label": "不替换"}, *rows]

    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def interactiveReplacementOptions(self) -> list[dict[str, str]]:
        return self._interactive_replacement_options()

    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def interactiveAnalysisGroups(self) -> list[dict[str, Any]]:
        if not self._interactive_active:
            return []
        group_ids = sorted({value for value in self._interactive_group_ids.values()
                            if isinstance(value, int) and value > 0})
        return [{"group_id": group_id,
                 "coefficient": float(self._interactive_group_factors.get(group_id, 1.0)),
                 "members": sum(value == group_id for value in self._interactive_group_ids.values())}
                for group_id in group_ids]

    @staticmethod
    def _interactive_number_text(value: float, decimals: int) -> str:
        try:
            return format(float(value), f".{max(0, min(int(decimals), 12))}f")
        except (TypeError, ValueError, OverflowError):
            return ""

    @staticmethod
    def _interactive_digit_cells(value: float, decimals: int) -> list[dict[str, Any]]:
        text = FilmInteractiveAnalysisController._interactive_number_text(value, decimals)
        if not text:
            return []
        integer, dot, fraction = text.partition(".")
        cells = [{"digit": char, "place": len(integer) - index - 1, "decimal": False}
                 for index, char in enumerate(integer)]
        if dot:
            cells.append({"digit": ".", "place": 0, "decimal": True})
            cells.extend({"digit": char, "place": -(index + 1), "decimal": False}
                         for index, char in enumerate(fraction))
        return cells

    @Property(_QVARIANT_PROPERTY_TYPE, notify=changed)
    def interactiveAnalysisRows(self) -> list[dict[str, Any]]:
        if not self._interactive_active:
            return []
        decimals = cast(int, self.filmThicknessDecimals)
        material_names = {str(row.get("id", "")): str(row.get("name") or row.get("id", ""))
                          for row in getattr(self._source_controller, "film_options", [])}
        layers: list[dict[str, Any]] = []
        double_sided = self._interactive_base_design.get("substrate", {}).get("sides") == "double"
        for side in ("front", "back"):
            for index, layer in enumerate(self._interactive_replacement_design.get(side + "_layers", [])):
                layer_id = str(layer.get("id", ""))
                key = (side, layer_id)
                physical_nm = self._interactive_base_physical_nm.get(key)
                qw = self._interactive_base_qw.get(key)
                mode = self._interactive_mode
                editable = cast(bool, self.interactiveAnalysisCanEdit)
                if mode == "physical_nm":
                    value, original, unit = physical_nm, physical_nm, "nm"
                elif mode == "optical_qw":
                    value, original, unit = qw, qw, "QW"
                elif mode == "group_coefficient":
                    original = physical_nm
                    group_id = self._interactive_group_ids.get(key)
                    factor = self._interactive_group_factors.get(group_id, 1.0) if group_id else 1.0
                    value = None if physical_nm is None else physical_nm * factor
                    unit = "nm"
                    editable = False
                else:
                    value, original, unit = physical_nm, physical_nm, "nm"
                    editable = False
                if value is None or original is None:
                    editable = False
                if mode == "optical_qw" and self._interactive_nm_per_qw.get(key) is None:
                    editable = False
                current_value = (self._interactive_values.get(key, float(value))
                                 if value is not None and mode in {"physical_nm", "optical_qw"}
                                 else value)
                group_id = self._interactive_group_ids.get(key)
                layers.append({
                    "side": side, "side_label": "正面" if side == "front" else "背面",
                    "active": side == "front" or double_sided,
                    "layer_id": layer_id, "layer_number": index + 1,
                    "material": material_names.get(str(layer.get("material_id", "")),
                                                    str(layer.get("material_id") or "未命名材料")),
                    "material_id": str(layer.get("material_id", "")),
                    "optical_set_id": str(layer.get("optical_set_id", "")),
                    "group_id": "" if group_id is None else str(group_id),
                    "original_text": self._interactive_number_text(float(original), decimals)
                                     if original is not None else "不可用",
                    "current_text": self._interactive_number_text(float(current_value), decimals)
                                    if current_value is not None else "不可用",
                    "unit": unit, "editable": editable,
                    "digits": self._interactive_digit_cells(float(current_value), decimals)
                             if editable and current_value is not None else [],
                })
        return layers

    @Slot(result=bool)
    def startInteractiveAnalysis(self) -> bool:
        if self._source_closed or self._source_controller is None or self.session is None:
            self._interactive_preview_error = "交互式分析需要仍打开的可编辑膜系设计。"
            self._interactive_preview_status = "当前来源不可编辑；临时分析未启动。"
            self.changed.emit()
            return False
        snapshot = self.session.snapshot()
        if snapshot.pending_confirmation or snapshot.expression_busy:
            self._interactive_preview_error = "请先完成设计确认或表达式操作，再开始交互分析。"
            self._interactive_preview_status = "源设计尚未就绪。"
            self.changed.emit()
            return False
        self.clearInteractiveAnalysis()
        self._interactive_active = True
        self._interactive_mode = "physical_nm"
        self._interactive_base_design = deepcopy(dict(snapshot.design))
        self._interactive_replacement_design = deepcopy(dict(snapshot.design))
        self._interactive_base_revision = int(snapshot.design_revision)
        self._interactive_base_epoch = self._source_edit_epoch()
        self._interactive_base_design_id = str(snapshot.design.get("id", ""))
        self._interactive_base_physical_nm.clear()
        self._interactive_base_qw.clear()
        self._interactive_nm_per_qw.clear()
        self._interactive_values.clear()
        self._interactive_base_group_ids.clear()
        self._interactive_group_ids.clear()
        self._interactive_group_factors.clear()
        derived = getattr(snapshot, "derived", {})
        for side in ("front", "back"):
            for layer in self._interactive_base_design.get(side + "_layers", []):
                layer_id = str(layer.get("id", ""))
                key = (side, layer_id)
                thickness = layer.get("thickness", {})
                unit = str(thickness.get("unit", "nm"))
                try:
                    row_derived = (derived.get(side, {}).get(layer_id, {})
                                   if isinstance(derived, dict) else {})
                    physical = row_derived.get("physicalNmValue")
                    if physical is None and unit == "nm":
                        physical = _number(thickness.get("value"), "physical thickness")
                    elif physical is None and unit == "um":
                        physical = _number(thickness.get("value"), "physical thickness") * 1000.0
                    physical = None if physical is None else _number(physical, "physical thickness")
                except (ValueError, TypeError, OverflowError):
                    physical = None
                row_derived = (derived.get(side, {}).get(layer_id, {})
                               if isinstance(derived, dict) else {})
                qw = None
                try:
                    if unit == "qw":
                        qw = _number(thickness.get("value"), "optical thickness")
                    else:
                        raw_qw = row_derived.get("qwText")
                        if raw_qw not in (None, "", "n不可用"):
                            qw = _number(raw_qw, "optical thickness")
                except (ValueError, TypeError, OverflowError):
                    qw = None
                scale = (None if physical is None or qw is None else
                         (physical / qw if qw > 0 else None))
                group_id = layer.get("interactive_group_id")
                if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id <= 0:
                    group_id = None
                self._interactive_base_physical_nm[key] = physical
                self._interactive_base_qw[key] = qw
                self._interactive_nm_per_qw[key] = scale
                self._interactive_base_group_ids[key] = group_id
                self._interactive_group_ids[key] = group_id
        self._interactive_base_simulation_configuration = self.session.workflow_configuration("simulation")
        try:
            from coat_sim.core.film_design_service import simulation_settings
            raw_parameters = self._interactive_base_simulation_configuration.get("parameters", {})
            settings = simulation_settings(raw_parameters)
            if "first_surface" not in raw_parameters:
                settings["first_surface"] = str(self._interactive_base_design.get("first_surface", "front"))
            self._interactive_base_angle_deg = float(settings["angle_deg"])
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._interactive_preview_error = f"无法读取当前仿真参数：{exc}"
            self._interactive_preview_status = "临时光谱预览尚未启动。"
            self._interactive_active = False
            self.changed.emit()
            return False
        self._interactive_angle_deg = self._interactive_base_angle_deg
        self._interactive_stale = False
        self._interactive_preview_spectrum = []
        self._interactive_preview_generation = 0
        self._interactive_last_valid_generation = -1
        self._interactive_preview_error = ""
        self._interactive_preview_status = "正在计算原设计的临时光谱…"
        self._queue_interactive_spectrum_preview()
        return True

    @Slot()
    def clearInteractiveAnalysis(self) -> None:
        self._interactive_preview_timer.stop()
        self._interactive_preview_generation += 1
        if self._interactive_preview_event is not None:
            self._interactive_preview_event.set()
        self._interactive_preview_event = None
        self._interactive_active = False
        self._interactive_stale = False
        self._interactive_base_design = {}
        self._interactive_replacement_design = {}
        self._interactive_base_revision = None
        self._interactive_base_epoch = None
        self._interactive_base_design_id = ""
        self._interactive_base_physical_nm.clear()
        self._interactive_base_qw.clear()
        self._interactive_nm_per_qw.clear()
        self._interactive_values.clear()
        self._interactive_base_group_ids.clear()
        self._interactive_group_ids.clear()
        self._interactive_group_factors.clear()
        self._interactive_base_simulation_configuration = {}
        self._interactive_preview_spectrum = []
        self._interactive_preview_busy = False
        self._interactive_last_valid_generation = -1
        self._interactive_preview_error = ""
        self._interactive_preview_status = "交互式分析已关闭；原设计未改变。"
        self._publish_preview(None)
        self.changed.emit()

    @Slot()
    def resetInteractiveAnalysis(self) -> None:
        if not self._interactive_active:
            return
        if not self.interactiveAnalysisCanEdit or not self._interactive_source_is_current():
            self._mark_interactive_stale("源膜系已改变；请关闭并重新打开交互式分析。")
            return
        self._interactive_values.clear()
        self._interactive_replacement_design = deepcopy(self._interactive_base_design)
        self._interactive_group_ids = deepcopy(self._interactive_base_group_ids)
        self._interactive_group_factors = {group_id: 1.0 for group_id in set(
            value for value in self._interactive_group_ids.values() if value is not None)}
        self._interactive_angle_deg = self._interactive_base_angle_deg
        self._interactive_preview_error = ""
        self._queue_interactive_spectrum_preview()

    @Slot(str, result=bool)
    def setInteractiveAnalysisMode(self, mode: str) -> bool:
        allowed = {"physical_nm", "optical_qw", "incident_angle", "group_coefficient"}
        if not self._interactive_active or mode not in allowed or not self.interactiveAnalysisCanEdit:
            return False
        if mode == self._interactive_mode:
            return True
        if self.interactiveAnalysisDirty:
            self._interactive_preview_error = "切换分析变量会丢弃当前临时更改；请先应用、重置或关闭分析。"
            self.changed.emit()
            return False
        if mode == "optical_qw":
            self._prepare_interactive_qw_scales()
        self._interactive_mode = mode
        groups = {value for value in self._interactive_group_ids.values() if value is not None}
        self._interactive_group_factors = {group_id: 1.0 for group_id in groups}
        self._interactive_preview_error = ""
        self._queue_interactive_spectrum_preview()
        return True

    def _prepare_interactive_qw_scales(self) -> None:
        if self.session is None:
            return
        reference = float(self._interactive_base_design.get("reference_wavelength_nm", 550.0))
        for side in ("front", "back"):
            for layer in self._interactive_base_design.get(side + "_layers", []):
                key = (side, str(layer.get("id", "")))
                if self._interactive_nm_per_qw.get(key) is not None:
                    continue
                try:
                    index = self.session._reference_index(
                        str(layer.get("material_id", "")), str(layer.get("optical_set_id", "")), reference)
                    scale = reference / (4.0 * _number(index, "reference index"))
                    self._interactive_nm_per_qw[key] = scale if math.isfinite(scale) and scale > 0 else None
                    resolved_scale = self._interactive_nm_per_qw.get(key)
                    if self._interactive_base_qw.get(key) is None and resolved_scale is not None:
                        physical = self._interactive_base_physical_nm.get(key)
                        if physical is not None:
                            self._interactive_base_qw[key] = physical / resolved_scale
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    self._interactive_nm_per_qw[key] = None

    def _interactive_source_is_current(self) -> bool:
        if (not self._interactive_active or self._interactive_stale or self._source_closed
                or self.session is None or self._source_controller is None
                or self._interactive_base_revision is None):
            return False
        try:
            snapshot = self.session.snapshot()
            return bool(snapshot.design_revision == self._interactive_base_revision
                        and str(snapshot.design.get("id", "")) == self._interactive_base_design_id
                        and self._source_edit_epoch() == self._interactive_base_epoch
                        and self.session.workflow_configuration("simulation")
                            == self._interactive_base_simulation_configuration
                        and not snapshot.pending_confirmation and not snapshot.expression_busy)
        except (AttributeError, TypeError, RuntimeError):
            return False

    def _mark_interactive_stale(self, message: str) -> None:
        if not self._interactive_active:
            return
        self._interactive_stale = True
        self._interactive_preview_timer.stop()
        self._interactive_preview_generation += 1
        if self._interactive_preview_event is not None:
            self._interactive_preview_event.set()
        self._interactive_preview_event = None
        self._interactive_preview_busy = False
        self._interactive_preview_error = str(message)
        self._interactive_preview_status = "源版本已失效；只保留最近一次有效光谱供查看，不能应用。"
        self._publish_preview(None)
        self.changed.emit()

    def _interactive_current_value(self, key: tuple[str, str]) -> float | None:
        base = (self._interactive_base_qw.get(key) if self._interactive_mode == "optical_qw"
                else self._interactive_base_physical_nm.get(key))
        if base is None:
            return None
        return self._interactive_values.get(key, base)

    @Slot(str, str, int, int, result=bool)
    def adjustInteractiveDigit(self, side: str, layer_id: str, place: int, direction: int) -> bool:
        if (not self.interactiveAnalysisCanEdit or direction not in (-1, 1)
                or self._interactive_mode not in {"physical_nm", "optical_qw"}):
            return False
        key = (str(side), str(layer_id))
        current = self._interactive_current_value(key)
        if current is None:
            return False
        text = self._interactive_number_text(current, cast(int, self.filmThicknessDecimals))
        integer = text.partition(".")[0]
        index = len(integer) - 1 - place if place >= 0 else len(integer) - place
        if not 0 <= index < len(text) or text[index] == ".":
            return False
        try:
            amount = Decimal(1).scaleb(int(place)) * int(direction)
            updated = Decimal(text) + amount
            if updated < 0 or not updated.is_finite():
                self._interactive_preview_error = "物理厚度不能为负。"
                self.changed.emit()
                return False
            self._interactive_values[key] = float(updated)
        except (ArithmeticError, TypeError, ValueError, OverflowError):
            self._interactive_preview_error = "数字位调整无效。"
            self.changed.emit()
            return False
        self._queue_interactive_spectrum_preview()
        return True

    @Slot(str, str, int, str, result=bool)
    def setInteractiveDigit(self, side: str, layer_id: str, place: int, digit: str) -> bool:
        if (not self.interactiveAnalysisCanEdit or self._interactive_mode not in {"physical_nm", "optical_qw"}
                or re.fullmatch(r"[0-9]", str(digit)) is None):
            self._interactive_preview_error = "每个厚度数字位只能输入 0–9。"
            self.changed.emit()
            return False
        key = (str(side), str(layer_id))
        current = self._interactive_current_value(key)
        if current is None:
            self._interactive_preview_error = "当前层厚度不可换算，不能编辑。"
            self.changed.emit()
            return False
        text = self._interactive_number_text(current, cast(int, self.filmThicknessDecimals))
        integer = text.partition(".")[0]
        index = len(integer) - 1 - place if place >= 0 else len(integer) - place
        if not 0 <= index < len(text) or text[index] == ".":
            return False
        try:
            updated = Decimal(text) + (int(digit) - int(text[index])) * Decimal(1).scaleb(int(place))
            if updated < 0 or not updated.is_finite():
                self._interactive_preview_error = "物理厚度不能为负。"
                self.changed.emit()
                return False
            self._interactive_values[key] = float(updated)
        except (ArithmeticError, TypeError, ValueError, OverflowError):
            self._interactive_preview_error = "数字位输入无效。"
            self.changed.emit()
            return False
        self._queue_interactive_spectrum_preview()
        return True

    @Slot(str, str, result=bool)
    def setInteractiveGroupFactor(self, group_id: str, text: str) -> bool:
        if self._interactive_mode != "group_coefficient" or not self.interactiveAnalysisCanEdit:
            return False
        try:
            identifier = _integer(group_id, "组号", 1)
            factor = _number(text, "组系数")
            if factor < 0:
                raise ValueError("组系数不能为负。")
            if not any(value == identifier for value in self._interactive_group_ids.values()):
                raise ValueError("此组当前没有膜层。")
        except (ValueError, TypeError, OverflowError) as exc:
            self._interactive_preview_error = str(exc)
            self.changed.emit()
            return False
        self._interactive_group_factors[identifier] = factor
        self._queue_interactive_spectrum_preview()
        return True

    @Slot(str, float, result=bool)
    def adjustInteractiveGroupFactor(self, group_id: str, delta: float) -> bool:
        try:
            identifier = _integer(group_id, "组号", 1)
            increment = Decimal(str(_number(delta, "组系数步长")))
            current = Decimal(str(self._interactive_group_factors.get(identifier, 1.0)))
            updated = current + increment
            if updated < 0:
                self._interactive_preview_error = "组系数不能为负。"
                self.changed.emit()
                return False
            return self.setInteractiveGroupFactor(str(identifier), str(updated))
        except (ValueError, TypeError, OverflowError, ArithmeticError) as exc:
            self._interactive_preview_error = str(exc)
            self.changed.emit()
            return False

    @Slot(str, result=bool)
    def setInteractiveAngle(self, text: str) -> bool:
        if self._interactive_mode != "incident_angle" or not self.interactiveAnalysisCanEdit:
            return False
        try:
            value = _number(text, "入射角")
            if not 0 <= value < 90:
                raise ValueError("入射角必须在 [0, 90) 度。")
        except (ValueError, TypeError, OverflowError) as exc:
            self._interactive_preview_error = str(exc)
            self.changed.emit()
            return False
        self._interactive_angle_deg = value
        self._queue_interactive_spectrum_preview()
        return True

    @Slot("QVariantMap", result=bool)
    def applyInteractiveBatch(self, payload: dict) -> bool:
        if not self.interactiveAnalysisCanEdit or not isinstance(payload, Mapping):
            return False
        if self._interactive_mode == "incident_angle":
            self._interactive_preview_status = (
                "入射角模式只应用仿真角度；批量材料、厚度和分组修改不会提交。")
            self.changed.emit()
            return False
        filter_values = payload.get("filter", {})
        changes = payload.get("changes", {})
        if not isinstance(filter_values, Mapping) or not isinstance(changes, Mapping):
            return False
        side_filter = str(filter_values.get("side", "") or "")
        if side_filter not in ("", "all", "front", "back"):
            self._interactive_preview_error = "批量修改的正/背面筛选无效。"
            self.changed.emit()
            return False
        material_filter = str(filter_values.get("material_id", "") or "")
        set_filter = str(filter_values.get("optical_set_id", "") or "")
        group_filter_text = str(filter_values.get("group_id", "") or "").strip()
        try:
            group_filter = None if not group_filter_text else _integer(group_filter_text, "筛选组号", 1)
            thickness_text = str(changes.get("thickness_value", "") or "").strip()
            thickness = None if not thickness_text else _number(thickness_text, "批量厚度")
            if thickness is not None and thickness < 0:
                raise ValueError("批量厚度不能为负；0 是有效厚度。")
            replacement_material = str(changes.get("material_id", "") or "").strip()
            replacement_set = str(changes.get("optical_set_id", "") or "").strip()
            if bool(replacement_material) != bool(replacement_set):
                raise ValueError("材料替换必须同时指定材料和光学数据集。")
            if replacement_material:
                valid_pair = any(str(item.get("material_id")) == replacement_material
                                 and str(item.get("optical_set_id")) == replacement_set
                                 for item in self._interactive_replacement_options()[1:])
                if not valid_pair:
                    raise ValueError("所选材料/光学数据集不在当前膜层材料目录中。")
            group_text = str(changes.get("group_id", "") or "").strip()
            clear_group = bool(changes.get("clear_group", False))
            new_group = None if not group_text else _integer(group_text, "新组号", 1)
            if clear_group and new_group is not None:
                raise ValueError("清除组号与设置新组号不能同时选择。")
            if thickness is not None and self._interactive_mode not in {"physical_nm", "optical_qw"}:
                raise ValueError("批量厚度只适用于物理厚度或光学厚度分析模式。")
            if thickness is None and new_group is None and not clear_group and not replacement_material:
                raise ValueError("批量修改没有任何替换值；留空表示不修改。")
        except (ValueError, TypeError, OverflowError) as exc:
            self._interactive_preview_error = str(exc)
            self.changed.emit()
            return False
        matched: list[tuple[str, str]] = []
        for side in ("front", "back"):
            if side_filter not in ("", "all", side):
                continue
            for layer in self._interactive_replacement_design.get(side + "_layers", []):
                key = (side, str(layer.get("id", "")))
                if material_filter and str(layer.get("material_id", "")) != material_filter:
                    continue
                if set_filter and str(layer.get("optical_set_id", "")) != set_filter:
                    continue
                if group_filter is not None and self._interactive_group_ids.get(key) != group_filter:
                    continue
                matched.append(key)
        if not matched:
            self._interactive_preview_error = "筛选条件没有匹配的膜层；没有修改任何层。"
            self.changed.emit()
            return False
        if thickness is not None and self._interactive_mode == "optical_qw":
            missing = [f"{side}/{layer_id}" for side, layer_id in matched
                       if self._interactive_nm_per_qw.get((side, layer_id)) is None]
            if missing:
                self._interactive_preview_error = "部分匹配层缺少 QW↔nm 换算：" + "、".join(missing[:8])
                self.changed.emit()
                return False
        # Prepare every replacement before changing the temporary draft.
        next_values = dict(self._interactive_values)
        next_groups = dict(self._interactive_group_ids)
        next_factors = dict(self._interactive_group_factors)
        next_design = deepcopy(self._interactive_replacement_design)
        for key in matched:
            if thickness is not None:
                next_values[key] = thickness
            if replacement_material:
                row = next(item for item in next_design[key[0] + "_layers"]
                           if str(item.get("id", "")) == key[1])
                row["material_id"] = replacement_material
                row["optical_set_id"] = replacement_set
            old_group = next_groups.get(key)
            if clear_group:
                next_groups[key] = None
            elif new_group is not None:
                next_groups[key] = new_group
                next_factors.setdefault(new_group, 1.0)
            if old_group is not None and old_group not in next_groups.values():
                next_factors.pop(old_group, None)
        self._interactive_values = next_values
        self._interactive_replacement_design = next_design
        self._interactive_group_ids = next_groups
        self._interactive_group_factors = next_factors
        self._queue_interactive_spectrum_preview()
        return True

    def _interactive_preview_design(self) -> dict[str, Any]:
        # Clone the frozen design once. Untouched thickness records retain their
        # exact original value/unit; only explicitly edited dimensions are
        # rewritten, avoiding precision loss on open/preview alone.
        design = deepcopy(self._interactive_replacement_design)
        for side in ("front", "back"):
            for layer in design.get(side + "_layers", []):
                key = (side, str(layer.get("id", "")))
                if self._interactive_mode == "physical_nm" and key in self._interactive_values:
                    value = _number(self._interactive_values[key], "物理厚度")
                    if value < 0:
                        raise ValueError("物理厚度不能为负。")
                    layer["thickness"] = {"value": value, "unit": "nm"}
                elif self._interactive_mode == "optical_qw" and key in self._interactive_values:
                    value = _number(self._interactive_values[key], "光学厚度")
                    if value < 0:
                        raise ValueError("光学厚度不能为负。")
                    layer["thickness"] = {"value": value, "unit": "qw"}
                elif self._interactive_mode == "group_coefficient":
                    group_id = self._interactive_group_ids.get(key)
                    factor = self._interactive_group_factors.get(group_id, 1.0) if group_id else 1.0
                    original_nm = self._interactive_base_physical_nm.get(key)
                    if group_id is not None and factor != 1.0:
                        if original_nm is None:
                            raise ValueError(f"{side}/{key[1]} 的物理厚度不可用，无法应用组系数。")
                        value = _number(original_nm * factor, "组系数调整后的物理厚度")
                        if value < 0:
                            raise ValueError("物理厚度不能为负。")
                        layer["thickness"] = {"value": value, "unit": "nm"}
                group_id = self._interactive_group_ids.get(key)
                if group_id is None:
                    layer.pop("interactive_group_id", None)
                else:
                    layer["interactive_group_id"] = group_id
        return design

    def _interactive_preview_settings(self) -> dict[str, Any]:
        from coat_sim.core.film_design_service import simulation_settings
        configuration = deepcopy(self._interactive_base_simulation_configuration)
        raw_parameters = configuration.get("parameters", {})
        settings = simulation_settings(raw_parameters)
        if "first_surface" not in raw_parameters:
            settings["first_surface"] = str(self._interactive_base_design.get("first_surface", "front"))
        if self._interactive_mode == "incident_angle":
            settings["angle_deg"] = float(self._interactive_angle_deg)
        return settings

    def _queue_interactive_spectrum_preview(self) -> None:
        if not self._interactive_active or self._interactive_stale:
            return
        self._interactive_preview_generation += 1
        if self._interactive_preview_event is not None:
            self._interactive_preview_event.set()
        self._interactive_preview_event = None
        self._interactive_preview_busy = True
        self._interactive_preview_error = ""
        self._interactive_preview_status = "参数已更改；正在合并快速输入并刷新光谱…"
        self._interactive_preview_timer.start()
        self.changed.emit()

    @Slot()
    def _start_interactive_spectrum_preview(self) -> None:
        if (not self._interactive_active or self._interactive_stale
                or not self._interactive_source_is_current() or self._source_closed):
            self._mark_interactive_stale("源膜系已变化；请关闭并重新打开交互式分析。")
            return
        generation = self._interactive_preview_generation
        cancel_event = threading.Event()
        try:
            design = self._interactive_preview_design()
            settings = self._interactive_preview_settings()
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._interactive_preview_busy = False
            self._interactive_preview_error = str(exc)
            self._interactive_preview_status = "显示最近一次有效光谱；本次预览无法计算。"
            self.changed.emit()
            return
        # Reuse the film window's existing task owner/reaper: it keeps worker
        # wrappers alive after a sidebar/window closes without blocking Qt.
        from .film_quick_controller import _FilmTask, _STARTUP_WORKERS, _startup_task_reaper
        self._interactive_preview_event = cancel_event
        worker_id = uuid4().hex
        worker = _FilmTask(worker_id, lambda: _simulate_interactive_spectrum(
            self.service, design, settings, cancel_event))
        worker.signals.finished.connect(self._on_interactive_spectrum_finished)
        worker.signals.finished.connect(_startup_task_reaper().release)
        self._interactive_preview_workers[worker_id] = worker
        self._interactive_preview_worker_generations[worker_id] = generation
        _STARTUP_WORKERS[worker_id] = worker
        QThreadPool.globalInstance().start(worker)

    @Slot(str, object, object)
    def _on_interactive_spectrum_finished(self, worker_id: str, result: object, error: object) -> None:
        self._interactive_preview_workers.pop(str(worker_id), None)
        generation = self._interactive_preview_worker_generations.pop(str(worker_id), None)
        if (generation is None or generation != self._interactive_preview_generation
                or not self._interactive_active or self._interactive_stale):
            return
        self._interactive_preview_event = None
        self._interactive_preview_busy = False
        if error is not None:
            self._interactive_preview_error = (f"{error[0]}: {error[1]}"
                if isinstance(error, tuple) and len(error) == 2 else str(error))
            self._interactive_preview_status = "显示最近一次有效光谱；本次临时预览失败。"
            self.changed.emit()
            return
        try:
            if not isinstance(result, Mapping):
                raise ValueError("光学服务返回了非映射光谱。")
            waves = list(result.get("wavelength_nm", []))
            curves = [list(result.get(key, [])) for key in ("R", "T", "A")]
            if not waves or any(len(curve) != len(waves) for curve in curves):
                raise ValueError("光谱波长与 R/T/A 曲线长度不一致。")
            spectrum = []
            for wave, reflectance, transmittance, absorbance in zip(*([waves] + curves), strict=True):
                row = {"wavelength_nm": _number(wave, "波长"),
                       "R": _number(reflectance, "R"), "T": _number(transmittance, "T"),
                       "A": _number(absorbance, "A")}
                spectrum.append(row)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._interactive_preview_error = str(exc)
            self._interactive_preview_status = "显示最近一次有效光谱；本次临时预览结果无效。"
            self.changed.emit()
            return
        self._interactive_preview_spectrum = spectrum
        self._interactive_last_valid_generation = generation
        self._publish_preview(result)
        self._interactive_preview_error = ""
        self._interactive_preview_status = (
            f"当前临时光谱已更新：{len(spectrum)} 点 · "
            f"{spectrum[0]['wavelength_nm']:g}–{spectrum[-1]['wavelength_nm']:g} nm · "
            f"入射角 {self._interactive_preview_settings()['angle_deg']:g}°。"
        )
        self.changed.emit()

    @Slot(result=bool)
    def applyInteractiveAnalysis(self) -> bool:
        if not self.interactiveAnalysisCanApply:
            self._interactive_preview_error = "当前临时结果未完成、已过期或没有可应用的修改。"
            self.changed.emit()
            return False
        if self._interactive_mode == "incident_angle":
            session = self.session
            if session is None:
                self._interactive_preview_error = "来源设计已关闭，无法保存仿真角度。"
                self.changed.emit()
                return False
            current_configuration = session.workflow_configuration("simulation")
            current_parameters = deepcopy(current_configuration.get("parameters", {}))
            try:
                from coat_sim.core.film_design_service import simulation_settings
                current_angle = simulation_settings(current_parameters)["angle_deg"]
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                self._interactive_preview_error = f"无法校验当前仿真设置：{exc}"
                self.changed.emit()
                return False
            if float(current_angle) != self._interactive_base_angle_deg:
                self._mark_interactive_stale("仿真角度已由其他操作修改；未覆盖新设置。")
                return False
            current_parameters["angle_deg"] = float(self._interactive_angle_deg)
            configuration = deepcopy(current_configuration)
            configuration["schema_version"] = int(configuration.get("schema_version", 1))
            configuration["parameters"] = current_parameters
            setter = getattr(self._source_controller, "setWorkflowConfiguration", None)
            self._interactive_applying = True
            try:
                applied = bool(callable(setter) and setter("simulation", configuration))
            finally:
                self._interactive_applying = False
            if not applied:
                self._interactive_preview_error = "无法原子保存入射角仿真设置。"
                self.changed.emit()
                return False
            self.clearInteractiveAnalysis()
            return True
        try:
            candidate = self._interactive_preview_design()
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._interactive_preview_error = str(exc)
            self.changed.emit()
            return False
        revision = self._interactive_base_revision
        epoch = self._interactive_base_epoch
        apply = getattr(self._source_controller, "applyOptimizedDesign", None)
        self._interactive_applying = True
        try:
            applied = bool(callable(apply) and revision is not None and epoch is not None
                           and apply(candidate, int(revision), int(epoch)))
        finally:
            self._interactive_applying = False
        if not applied:
            self._mark_interactive_stale("源设计已变化或正在确认；未覆盖任何新内容。请重新打开分析。")
            return False
        self.clearInteractiveAnalysis()
        return True

    @Slot()
    def shutdown(self) -> None:
        self.clearInteractiveAnalysis()
        if self._session_unsubscribe is not None:
            self._session_unsubscribe()
            self._session_unsubscribe = None
