"""Plot display settings for thin-film spectrum views."""
from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
)


_CURVES = (
    ("R", "R — 反射率"),
    ("T", "T — 透射率"),
    ("A", "A — 吸收率"),
    ("source_spectrum", "光源光谱"),
    ("reflected_spectrum", "反射光谱"),
    ("transmitted_spectrum", "透射光谱"),
    ("absorbed_spectrum", "吸收光谱"),
)
_DEFAULTS = {
    "curves": ["R", "T", "A"],
    "x": {
        "auto": True,
        "min": 350.0,
        "max": 850.0,
        "major_tick_auto": True,
        "major_tick": 100.0,
    },
    "y": {
        "unit": "fraction",
        "auto": True,
        "min": 0.0,
        "max": 1.0,
        "major_tick_auto": True,
        "major_tick": 0.2,
    },
    "grid": True,
}


class FilmPlotSettingsDialog(QDialog):
    """Edit plot curves, axis ranges/ticks, and grid visibility.

    ``values()`` returns a plain dictionary with ``curves`` (a list of selected
    result keys), ``x`` and ``y`` axis dictionaries, and ``grid``. Each axis has
    ``auto``, ``min``, ``max``, ``major_tick_auto``, and ``major_tick`` fields.
    Auto flags are independent for x and y ranges and for their major ticks.
    """

    def __init__(self, options=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("绘图参数")
        self.setModal(True)

        settings = self._merge_options(options)
        outer = QVBoxLayout(self)

        curves_box = QGroupBox("绘制曲线")
        curves_layout = QGridLayout(curves_box)
        self.curve_checks = {}
        for row, (key, label) in enumerate(_CURVES):
            check = QCheckBox(label)
            check.setObjectName("curve_" + key)
            check.setChecked(key in settings["curves"])
            self.curve_checks[key] = check
            curves_layout.addWidget(check, row // 2, row % 2)
        outer.addWidget(curves_box)

        axes_row = QGridLayout()
        self.axis_controls = {}
        self.axis_controls["x"] = self._axis_group(
            "x", "X 轴 — 波长（nm）", settings["x"]
        )
        self.axis_controls["y"] = self._axis_group(
            "y", "Y 轴 — R / T / A（%）或相对光谱（%）", settings["y"]
        )
        axes_row.addWidget(self.axis_controls["x"]["group"], 0, 0)
        axes_row.addWidget(self.axis_controls["y"]["group"], 0, 1)
        outer.addLayout(axes_row)

        self.grid_check = QCheckBox("显示网格辅助线（主刻度）")
        self.grid_check.setObjectName("plot_grid")
        self.grid_check.setChecked(settings["grid"])
        outer.addWidget(self.grid_check)

        self.error_label = QLabel("")
        self.error_label.setObjectName("plot_settings_error")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #a12622")
        outer.addWidget(self.error_label)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.setObjectName("plot_settings_buttons")
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).setText("应用")
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        self.button_box.rejected.connect(self.reject)
        outer.addWidget(self.button_box)
        self.setMinimumWidth(620)

    @staticmethod
    def _merge_options(options):
        """Copy supported settings over defaults without mutating the caller."""
        merged = deepcopy(_DEFAULTS)
        if not isinstance(options, dict):
            return merged

        allowed_curves = {key for key, _label in _CURVES}
        curves = options.get("curves")
        if isinstance(curves, (list, tuple, set)):
            merged["curves"] = [key for key in curves if key in allowed_curves]

        for axis_name in ("x", "y"):
            axis = options.get(axis_name)
            if not isinstance(axis, dict):
                continue
            target = merged[axis_name]
            for name in ("auto", "major_tick_auto"):
                if name in axis:
                    target[name] = bool(axis[name])
            for name in ("min", "max", "major_tick"):
                if name in axis:
                    try:
                        target[name] = float(axis[name]) / (100.0 if axis_name == 'y' and axis.get('unit') == 'percent' else 1.0)
                    except (TypeError, ValueError, OverflowError):
                        pass

        if "grid" in options:
            merged["grid"] = bool(options["grid"])
        return merged

    @staticmethod
    def _number(value, minimum, maximum):
        spin = QDoubleSpinBox()
        spin.setDecimals(6)
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setKeyboardTracking(False)
        return spin

    def _axis_group(self, name, title, settings):
        group = QGroupBox(title)
        form = QFormLayout(group)

        range_auto = QCheckBox("自动范围")
        range_auto.setObjectName(name + "_range_auto")
        range_auto.setChecked(settings["auto"])
        minimum = self._number(350.0 if name == "x" else -1.0e6,
                               0.0 if name == "x" else -1.0e9, 1.0e9)
        maximum = self._number(850.0 if name == "x" else 1.0,
                               0.0 if name == "x" else -1.0e9, 1.0e9)
        minimum.setObjectName(name + "_minimum")
        maximum.setObjectName(name + "_maximum")
        factor = 100.0 if name == 'y' else 1.0
        minimum.setValue(settings["min"] * factor)
        maximum.setValue(settings["max"] * factor)
        if name == 'y':
            minimum.setSuffix(' %'); maximum.setSuffix(' %')
        form.addRow(range_auto)
        form.addRow("最小值", minimum)
        form.addRow("最大值", maximum)

        tick_auto = QCheckBox("自动主刻度间距")
        tick_auto.setObjectName(name + "_major_tick_auto")
        tick_auto.setChecked(settings["major_tick_auto"])
        tick_maximum = 1.0e9
        tick = self._number(settings["major_tick"] * factor, 0.0, tick_maximum)
        if name == 'y': tick.setSuffix(' %')
        tick.setObjectName(name + "_major_tick")
        form.addRow(tick_auto)
        form.addRow("主刻度间距", tick)

        range_auto.toggled.connect(minimum.setDisabled)
        range_auto.toggled.connect(maximum.setDisabled)
        tick_auto.toggled.connect(tick.setDisabled)
        minimum.setDisabled(range_auto.isChecked())
        maximum.setDisabled(range_auto.isChecked())
        tick.setDisabled(tick_auto.isChecked())
        return {
            "group": group,
            "range_auto": range_auto,
            "minimum": minimum,
            "maximum": maximum,
            "tick_auto": tick_auto,
            "tick": tick,
        }

    def values(self):
        """Return validated settings as a plain dictionary.

        Raises ``ValueError`` if an explicitly configured range or tick
        interval is invalid. Automatic values are included for editing but are
        ignored by the plot while their corresponding auto flag is enabled.
        """
        curves = [key for key, _label in _CURVES if self.curve_checks[key].isChecked()]
        axes = {}
        for name in ("x", "y"):
            controls = self.axis_controls[name]
            auto = controls["range_auto"].isChecked()
            minimum = controls["minimum"].value()
            maximum = controls["maximum"].value()
            if not auto:
                if minimum >= maximum:
                    raise ValueError(f"{name.upper()} 轴最小值必须小于最大值。")
                if name == "x" and minimum <= 0:
                    raise ValueError("X 轴波长范围必须大于 0 nm。")

            tick_auto = controls["tick_auto"].isChecked()
            tick = controls["tick"].value()
            if not tick_auto and tick <= 0:
                raise ValueError(f"{name.upper()} 轴主刻度间距必须大于 0。")
            axes[name] = {
                "auto": auto,
                "min": minimum,
                "max": maximum,
                "major_tick_auto": tick_auto,
                "major_tick": tick,
            }
            if name == 'y':
                axes[name]['unit'] = 'fraction'
                for key in ('min','max','major_tick'): axes[name][key] /= 100.0
        return {"curves": curves, "x": axes["x"], "y": axes["y"],
                "grid": self.grid_check.isChecked()}

    def _apply(self):
        try:
            self.values()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self.error_label.clear()
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.objectName() == "plot_settings_form":
            parent = self.parentWidget()
            while parent is not None:
                if parent.objectName() == "simulation_and_plot_settings_dialog":
                    if isinstance(parent, QDialog):
                        parent.reject()
                        event.accept()
                        return
                parent = parent.parentWidget()
        super().keyPressEvent(event)
