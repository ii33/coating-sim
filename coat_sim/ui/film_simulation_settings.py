"""Standalone Qt dialog for film spectral simulation parameters."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math
import re
import sys

from PySide6.QtCore import QRegularExpression, QSignalBlocker, Signal, Qt
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QVBoxLayout,
)


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _count_for_step(span, step):
    if not math.isfinite(span) or span <= 0:
        raise ValueError("波长范围必须为有限正数。")
    if not math.isfinite(step) or step <= 0:
        raise ValueError("采样步长须为有限正数（nm）。")
    intervals = span / step
    if not math.isfinite(intervals):
        raise ValueError("采样点数超出当前运行时可寻址的数组长度")
    count = math.floor(intervals + 0.5) + 1
    if count < 2:
        raise ValueError("采样步长过大，结果少于 2 个波长点。")
    if count > sys.maxsize:
        raise ValueError("采样点数超出当前运行时可寻址的数组长度")
    return int(count)


class _IntegerInput(QLineEdit):
    """Decimal integer editor without QSpinBox's signed-32-bit ceiling."""

    valueChanged = Signal(object)

    def __init__(self, value=0, parent=None):
        super().__init__(parent)
        self.validationError = ""
        self.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9]*"), self))
        self.textChanged.connect(self._emit_value_changed)
        self.setValue(value)

    def value(self):
        text = self.text().strip()
        if not text or re.fullmatch(r"[0-9]+", text) is None:
            raise ValueError("采样点必须为整数")
        try:
            count = int(text, 10)
        except (ValueError, OverflowError) as exc:
            raise ValueError("当前运行时无法表示此采样点数") from exc
        if count > sys.maxsize:
            raise ValueError("采样点数超出当前运行时可寻址的数组长度")
        return count

    def setValue(self, value):
        self.setText(str(value))

    def _emit_value_changed(self, _text):
        try:
            value = self.value()
        except ValueError as exc:
            self.validationError = str(exc)
            self.valueChanged.emit(None)
            return
        self.validationError = ""
        self.valueChanged.emit(value)


class _ScientificInput(QLineEdit):
    """Float editor that keeps very small positive steps in scientific form."""

    valueChanged = Signal(object)

    def __init__(self, value=0.0, parent=None):
        super().__init__(parent)
        self.validationError = ""
        self.textChanged.connect(self._emit_value_changed)
        self.setValue(value)

    def value(self):
        try:
            number = float(self.text().strip())
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("采样步长必须为有限数值") from exc
        if not math.isfinite(number):
            raise ValueError("采样步长必须为有限数值")
        return number

    def setValue(self, value):
        self.setText(format(float(value), ".17g"))

    def _emit_value_changed(self, _text):
        try:
            value = self.value()
        except ValueError as exc:
            self.validationError = str(exc)
            self.valueChanged.emit(None)
            return
        self.validationError = ""
        self.valueChanged.emit(value)


def _tabulated(value):
    if not isinstance(value, Mapping) or value.get("kind") != "tabulated":
        return None
    waves, levels = value.get("wavelength_nm"), value.get("intensity")
    if not isinstance(waves, (list, tuple)) or not isinstance(levels, (list, tuple)):
        return None
    if not 2 <= len(waves) <= 100000 or len(waves) != len(levels):
        return None
    waves, levels = [_finite(x) for x in waves], [_finite(x) for x in levels]
    if any(x is None for x in waves + levels):
        return None
    if waves[0] <= 0 or any(b <= a for a, b in zip(waves, waves[1:])):
        return None
    if min(levels) < 0 or max(levels) <= 0:
        return None
    try:
        source = value.get("source", "")
        source = "" if source is None else str(source)
    except Exception:
        return None
    payload = {"kind": "tabulated", "wavelength_nm": waves, "intensity": levels,
               "source": source}
    basis = value.get("wavelength_basis", "vacuum")
    if basis not in ("vacuum", "standard_air"):
        return None
    payload["wavelength_basis"] = basis
    for key in ("dataset_id", "snapshot_date", "data_license", "source_page", "doi", "catalog_version"):
        item = value.get(key)
        if item is not None:
            if not isinstance(item, str): return None
            payload[key] = item
    source_warnings = value.get("source_warnings", [])
    if not isinstance(source_warnings, (list, tuple)) or not all(isinstance(x, str) for x in source_warnings):
        return None
    if source_warnings: payload["source_warnings"] = list(source_warnings)
    return payload


def _text(value):
    return value.strip() if isinstance(value, str) else ""


class FilmSimulationSettingsDialog(QDialog):
    """Edit parameters; values() returns a detached film-service settings dict."""

    def __init__(self, settings=None, light_sources=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("仿真参数")
        self.setModal(True)
        raw = deepcopy(settings) if isinstance(settings, Mapping) else {}

        low = _finite(raw.get("wavelength_min_nm", 400.0))
        high = _finite(raw.get("wavelength_max_nm", 700.0))
        if low is None or high is None or low <= 0 or high <= low:
            low, high = 400.0, 700.0
        count = raw.get("samples", 301)
        if type(count) is not int or count < 2:
            count = 301
        loaded_step = _finite(raw.get("step_nm"))
        if loaded_step is not None and loaded_step > 0:
            try:
                count = _count_for_step(high - low, loaded_step)
            except ValueError:
                pass
        initial_sampling_error = (
            "采样点数超出当前运行时可寻址的数组长度"
            if count > sys.maxsize else "")
        try:
            initial_step = (high - low) / (count - 1)
        except OverflowError:
            initial_step = 0.0
            initial_sampling_error = "当前运行时无法表示此采样点数"
        angle = _finite(raw.get("angle_deg", 0.0))
        if angle is None or not 0 <= angle < 90:
            angle = 0.0
        polarization = raw.get("polarization", "unpolarized")
        if polarization not in ("s", "p", "unpolarized"):
            polarization = "unpolarized"
        first = raw.get("first_surface", "front")
        if first not in ("front", "back"):
            first = "front"
        extrapolation_policy = raw.get("extrapolation_policy", "endpoint_hold")
        if extrapolation_policy == "endpoint":
            extrapolation_policy = "endpoint_hold"
        if extrapolation_policy not in ("error", "endpoint_hold"):
            extrapolation_policy = "endpoint_hold"

        outer = QVBoxLayout(self)
        spectral = QGroupBox("计算波段与采样")
        form = QFormLayout(spectral)
        self.wavelength_min_spin = self._double(low, "wavelength_min_nm")
        self.wavelength_max_spin = self._double(high, "wavelength_max_nm")
        self.step_spin = _ScientificInput(initial_step)
        self.step_spin.setObjectName("step_nm")
        self.samples_spin = _IntegerInput()
        self.samples_spin.setObjectName("samples")
        self.samples_spin.setValue(count)
        form.addRow("最小波长（nm）", self.wavelength_min_spin)
        form.addRow("最大波长（nm）", self.wavelength_max_spin)
        form.addRow("步长（nm）", self.step_spin)
        form.addRow("采样点数", self.samples_spin)
        outer.addWidget(spectral)

        geometry = QGroupBox("入射条件")
        form = QFormLayout(geometry)
        self.angle_spin = self._double(angle, "angle_deg")
        self.angle_spin.setRange(0.0, 90.0)
        self.polarization_combo = QComboBox()
        self.polarization_combo.setObjectName("polarization")
        for key, label in (("s", "s"), ("p", "p"), ("unpolarized", "非偏振")):
            self.polarization_combo.addItem(label, key)
        self.polarization_combo.setCurrentIndex(self.polarization_combo.findData(polarization))
        self.first_surface_combo = QComboBox()
        self.first_surface_combo.setObjectName("first_surface")
        self.first_surface_combo.addItem("正面", "front")
        self.first_surface_combo.addItem("背面", "back")
        self.first_surface_combo.setCurrentIndex(self.first_surface_combo.findData(first))
        form.addRow("入射角（°）", self.angle_spin)
        form.addRow("首个入射面", self.first_surface_combo)
        form.addRow("偏振方式", self.polarization_combo)
        outer.addWidget(geometry)

        coverage = QGroupBox("材料数据有效波段")
        form = QFormLayout(coverage)
        self.extrapolation_combo = QComboBox()
        self.extrapolation_combo.setObjectName("extrapolation_policy")
        self.extrapolation_combo.addItem("最近有效端点保持（默认）", "endpoint_hold")
        self.extrapolation_combo.addItem("超出有效波段时报错", "error")
        self.extrapolation_combo.setCurrentIndex(self.extrapolation_combo.findData(extrapolation_policy))
        form.addRow("超出 n 数据范围", self.extrapolation_combo)
        outer.addWidget(coverage)

        illumination = QGroupBox("光源")
        form = QFormLayout(illumination)
        self.light_source_combo = QComboBox()
        self.light_source_combo.setObjectName("light_source")
        self.light_source_combo.addItem("平坦光源", "flat")
        self.light_source_combo.addItem("黑体", "blackbody")
        self._tabulated_sources, self._source_metadata = {}, {}
        self._add_light_sources(light_sources)
        self.blackbody_temperature_label = QLabel("黑体温度（K）")
        self.blackbody_temperature_spin = self._double(
            self._temperature(raw.get("light_source")), "blackbody_temperature_k")
        self.blackbody_temperature_spin.setRange(1.0, 1.0e7)
        self.blackbody_temperature_spin.setDecimals(6)
        self.source_notes_label = QLabel("")
        self.source_notes_label.setObjectName("light_source_notes")
        self.source_notes_label.setWordWrap(True)
        form.addRow("光源", self.light_source_combo)
        form.addRow(self.blackbody_temperature_label, self.blackbody_temperature_spin)
        form.addRow("光源来源与标准说明", self.source_notes_label)
        outer.addWidget(illumination)

        self.error_label = QLabel("")
        self.error_label.setObjectName("simulation_settings_error")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #a12622")
        outer.addWidget(self.error_label)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.setObjectName("simulation_settings_buttons")
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).setText("应用")
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        self.button_box.rejected.connect(self.reject)
        outer.addWidget(self.button_box)

        self._range_error, self._sampling_error = "", initial_sampling_error
        self.wavelength_min_spin.valueChanged.connect(self._range_changed)
        self.wavelength_max_spin.valueChanged.connect(self._range_changed)
        self.step_spin.valueChanged.connect(self._step_changed)
        self.samples_spin.valueChanged.connect(self._samples_changed)
        self.light_source_combo.currentIndexChanged.connect(self._source_changed)
        self.setMinimumWidth(500)
        self._select_source(raw.get("light_source"))
        self._source_changed()

    @staticmethod
    def _double(value, name):
        spin = QDoubleSpinBox()
        spin.setObjectName(name)
        spin.setDecimals(12)
        spin.setRange(0.0, 1.0e12)
        spin.setKeyboardTracking(False)
        spin.setValue(value)
        return spin

    @staticmethod
    def _temperature(source):
        if isinstance(source, Mapping) and source.get("kind") == "blackbody":
            value = _finite(source.get("temperature_k", 5778.0))
            if value is not None and 1 <= value <= 1.0e7:
                return value
        return 5778.0

    def _add_light_sources(self, light_sources):
        try:
            rows = list(light_sources or ())
        except (TypeError, ValueError):
            rows = []
        used = set()
        for row_index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            payload = _tabulated(row.get("source"))
            if payload is None:
                continue
            identity = _text(row.get("id"))
            name = _text(row.get("name")) or identity or "离散光源"
            label = name if name.casefold() not in used else f"{name} ({identity or row_index + 1})"
            used.add(label.casefold())
            token = f"tabulated:catalog:{row_index}"
            self.light_source_combo.addItem(label, token)
            self._tabulated_sources[token] = payload
            try:
                self._source_metadata[token] = deepcopy(dict(row))
            except Exception:
                self._source_metadata[token] = {}

    def _select_source(self, source):
        if not isinstance(source, Mapping):
            source = {"kind": "flat"}
        kind = source.get("kind", "flat")
        target = kind if kind in ("flat", "blackbody") else "flat"
        if kind == "tabulated":
            payload = _tabulated(source)
            if payload is not None:
                target = next((key for key, value in self._tabulated_sources.items()
                               if value == payload), "")
                if not target:
                    target = "tabulated:loaded"
                    self.light_source_combo.addItem("已加载的离散光源", target)
                    self._tabulated_sources[target] = payload
                    self._source_metadata[target] = {}
        self.light_source_combo.setCurrentIndex(max(0, self.light_source_combo.findData(target)))

    @staticmethod
    def _set_blocked(widget, value):
        blocker = QSignalBlocker(widget)
        try:
            widget.setValue(value)
        finally:
            del blocker

    def _range(self):
        low, high = self.wavelength_min_spin.value(), self.wavelength_max_spin.value()
        if not math.isfinite(low) or not math.isfinite(high) or low <= 0 or high <= low:
            raise ValueError("波长范围须为正数，且最小值必须小于最大值。")
        return low, high

    def _range_changed(self, _value=None):
        try:
            self._range()
        except ValueError as exc:
            self._range_error = str(exc)
            self._show_error()
            return
        self._range_error = ""
        try:
            step = self.step_spin.value()
        except ValueError as exc:
            self._sampling_error = str(exc)
            self._show_error()
            return
        self._from_step(step)

    def _step_changed(self, step):
        try:
            self._range()
        except ValueError as exc:
            self._range_error = str(exc)
            self._show_error()
            return
        self._range_error = ""
        if step is None:
            self._sampling_error = self.step_spin.validationError or "采样步长必须为有限正数。"
            self._show_error()
            return
        self._from_step(step)

    def _samples_changed(self, samples):
        if samples is None:
            self._sampling_error = self.samples_spin.validationError or "采样点数须为至少 2 的整数。"
            self._show_error()
            return
        if samples < 2:
            self._sampling_error = "采样点数须为至少 2 的整数。"
            self._show_error()
            return
        try:
            low, high = self._range()
        except ValueError as exc:
            self._range_error = str(exc)
            self._show_error()
            return
        self._range_error = ""
        try:
            step = (high - low) / (samples - 1)
        except (OverflowError, ZeroDivisionError):
            step = 0.0
        if not math.isfinite(step) or step <= 0:
            self._sampling_error = "采样步长须为有限正数。"
        else:
            self._sampling_error = ""
            self._set_blocked(self.step_spin, step)
        self._show_error()

    def _from_step(self, step):
        try:
            low, high = self._range()
            count = _count_for_step(high - low, step)
        except ValueError as exc:
            self._sampling_error = str(exc)
            self._show_error()
            return
        actual = (high - low) / (count - 1)
        if not math.isfinite(actual) or actual <= 0:
            self._sampling_error = "采样步长须为有限正数。"
        else:
            self._sampling_error = ""
            self._set_blocked(self.samples_spin, count)
            self._set_blocked(self.step_spin, actual)
        self._show_error()

    def _kind(self):
        return self.light_source_combo.currentData()

    def _selected_tabulated(self):
        key = self._kind()
        return self._tabulated_sources.get(key) if isinstance(key, str) else None

    def _source_range_error(self):
        source = self._selected_tabulated()
        if source is None:
            return ""
        try:
            low, high = self._range()
        except ValueError:
            return ""
        source_low, source_high = source["wavelength_nm"][0], source["wavelength_nm"][-1]
        if low < source_low or high > source_high:
            return f"所选光源覆盖 {source_low:g}–{source_high:g} nm；计算范围须处于该区间内。"
        return ""

    def _source_notes(self):
        kind = self._kind()
        if kind == "flat":
            return "未提供来源或标准说明。"
        if kind == "blackbody":
            return "黑体模型；未提供外部来源或标准说明。"
        payload = self._selected_tabulated()
        metadata = self._source_metadata.get(kind, {})
        notes = []
        provenance = payload.get("source", "") if payload else ""
        if provenance:
            notes.append(f"来源：{provenance}")
        fields = (("standard", "标准"), ("standard_note", "标准说明"),
                  ("standard_notes", "标准说明"), ("provenance", "来源"),
                  ("notes", "备注"))
        for key, label in fields:
            note = _text(metadata.get(key))
            if note and note.casefold() not in {text.casefold() for text in notes}:
                notes.append(f"{label}：{note}")
        return "\n".join(notes) if notes else "未提供来源或标准说明。"

    def _source_changed(self, _index=None):
        visible = self._kind() == "blackbody"
        self.blackbody_temperature_label.setVisible(visible)
        self.blackbody_temperature_spin.setVisible(visible)
        self.source_notes_label.setText(self._source_notes())
        self._show_error()

    def _show_error(self):
        self.error_label.setText(self._range_error or self._sampling_error or self._source_range_error())

    def values(self):
        """Return a validated, detached dictionary matching the film service schema."""
        low, high = self._range()
        # Validate both text editors even if the last valid value is still cached.
        self.samples_spin.value()
        self.step_spin.value()
        if self._sampling_error:
            raise ValueError(self._sampling_error)
        samples = self.samples_spin.value()
        if samples < 2:
            raise ValueError("采样点数须为至少 2 的整数。")
        step = (high - low) / (samples - 1)
        if not math.isfinite(step) or step <= 0:
            raise ValueError("采样步长须为有限正数。")
        kind = self._kind()
        if kind == "flat":
            source = {"kind": "flat"}
        elif kind == "blackbody":
            temperature = self.blackbody_temperature_spin.value()
            if not math.isfinite(temperature) or not 1 <= temperature <= 1.0e7:
                raise ValueError("黑体温度须介于 1 至 1e7 K。")
            source = {"kind": "blackbody", "temperature_k": temperature}
        else:
            payload = self._selected_tabulated()
            if payload is None:
                raise ValueError("所选离散光源已不可用。")
            source = deepcopy(payload)
            if source.get("wavelength_basis", "vacuum") == "vacuum" and (
                low < source["wavelength_nm"][0] or high > source["wavelength_nm"][-1]):
                raise ValueError("计算波段须处于所选光源的覆盖范围内。")
        angle = self.angle_spin.value()
        if not math.isfinite(angle) or not 0 <= angle < 90:
            raise ValueError("入射角须处于 [0, 90)°。")
        polarization = self.polarization_combo.currentData()
        first = self.first_surface_combo.currentData()
        if polarization not in ("s", "p", "unpolarized"):
            raise ValueError("请选择 s、p 或非偏振。")
        if first not in ("front", "back"):
            raise ValueError("请选择正面或背面作为首个入射面。")
        return {
            "wavelength_min_nm": low, "wavelength_max_nm": high,
            "samples": samples, "step_nm": step, "angle_deg": angle,
            "polarization": polarization, "first_surface": first,
            "extrapolation_policy": self.extrapolation_combo.currentData(),
            "light_source": source,
        }

    def _apply(self):
        try:
            self.values()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self.error_label.clear()
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.objectName() == "simulation_settings_form":
            parent = self.parentWidget()
            while parent is not None:
                if parent.objectName() == "simulation_and_plot_settings_dialog":
                    if isinstance(parent, QDialog):
                        parent.reject()
                        event.accept()
                        return
                parent = parent.parentWidget()
        super().keyPressEvent(event)
