"""Combined transactional editor for film calculation and plot options."""
from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
)


class FilmSimulationAndPlotSettingsDialog(QDialog):
    """One modal, two-tab editor with a single Apply/Cancel decision."""

    def __init__(self, simulation_settings=None, plot_settings=None,
                 light_sources=(), parent=None):
        super().__init__(parent)
        self.setObjectName("simulation_and_plot_settings_dialog")
        self.setWindowTitle("仿真参数")
        self.setModal(True)

        from .film_simulation_settings import FilmSimulationSettingsDialog
        from .film_plot_settings import FilmPlotSettingsDialog

        self.simulation_form = FilmSimulationSettingsDialog(
            simulation_settings, light_sources, self)
        self.plot_form = FilmPlotSettingsDialog(
            plot_settings if isinstance(plot_settings, Mapping) else None, self)
        for form in (self.simulation_form, self.plot_form):
            form.setWindowFlags(Qt.WindowType.Widget)
            form.setModal(False)
            form.button_box.hide()
        self.simulation_form.setObjectName("simulation_settings_form")
        self.plot_form.setObjectName("plot_settings_form")

        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("simulation_plot_settings_tabs")
        self.simulation_scroll = QScrollArea()
        self.simulation_scroll.setObjectName("simulation_settings_scroll")
        self.simulation_scroll.setWidgetResizable(True)
        self.simulation_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.simulation_scroll.setWidget(self.simulation_form)
        self.plot_scroll = QScrollArea()
        self.plot_scroll.setObjectName("plot_settings_scroll")
        self.plot_scroll.setWidgetResizable(True)
        self.plot_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.plot_scroll.setWidget(self.plot_form)
        self.tabs.addTab(self.simulation_scroll, "计算参数")
        self.tabs.addTab(self.plot_scroll, "绘图参数")
        outer.addWidget(self.tabs)

        self.error_label = QLabel("")
        self.error_label.setObjectName("simulation_plot_settings_error")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #a12622")
        outer.addWidget(self.error_label)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.setObjectName("simulation_plot_settings_buttons")
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).setText("应用参数")
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        self.button_box.rejected.connect(self.reject)
        outer.addWidget(self.button_box)
        screen = (parent.screen() if parent is not None and hasattr(parent, "screen")
                  else QApplication.primaryScreen())
        available = screen.availableGeometry() if screen is not None else None
        target_width = min(920, max(260, available.width() - 48)) if available else 920
        target_height = min(720, max(220, available.height() - 48)) if available else 720
        self.setMinimumSize(min(320, target_width), min(240, target_height))
        self.resize(target_width, target_height)

    def values(self):
        """Return both validated drafts without changing application state."""
        return self.simulation_form.values(), self.plot_form.values()

    def _apply(self):
        try:
            self.values()
        except (ValueError, TypeError, OverflowError) as exc:
            self.error_label.setText(str(exc))
            return
        self.error_label.clear()
        self.simulation_form.error_label.clear()
        self.plot_form.error_label.clear()
        self.accept()
