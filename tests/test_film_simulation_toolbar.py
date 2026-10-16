"""Regressions for the combined spectrum toolbar actions and settings draft."""
from __future__ import annotations

import copy
import os
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from tests import qt_test_guard  # noqa: F401

from PySide6.QtCore import (
    QCoreApplication, QEvent, QMetaObject, QObject, QPoint, QPointF, QSettings, Qt,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_simulation_quick_controller import (
    FilmSimulationQuickController,
    create_film_simulation_quick_window,
)
from coat_sim.ui.film_simulation_plot_settings import FilmSimulationAndPlotSettingsDialog


ROOT = Path(__file__).parents[1]
QQuickStyle.setStyle("Fusion")
_APP = QGuiApplication.instance() or QGuiApplication([])


class FilmSimulationToolbarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = FilmDesignService(self.root)
        self.design = self.service.new_design()
        self.geometry = QSettings(str(self.root / "geometry.ini"), QSettings.Format.IniFormat)
        self.engine, self.window, self.controller = create_film_simulation_quick_window(
            root=self.root, service=self.service, design=self.design,
            geometry_settings=self.geometry)
        self.window.setWidth(920)
        self.window.setHeight(700)

    def tearDown(self):
        self.controller.close()
        self.window.close()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        self.temp.cleanup()

    @staticmethod
    def _item(window, name):
        return window.findChild(QQuickItem, name)

    @staticmethod
    def _object(window, name):
        return window.findChild(QObject, name)

    @staticmethod
    def _click(window, item):
        if not window.isVisible():
            window.show()
        QCoreApplication.processEvents()
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(round(point.x()), round(point.y())))
        QCoreApplication.processEvents()

    def test_toolbar_has_single_export_menu_and_combined_settings_dialog(self):
        parameter_button = self._item(self.window, "filmSimulationParametersButton")
        export_button = self._item(self.window, "filmSimulationExportButton")
        self.assertIsNotNone(parameter_button)
        self.assertIsNotNone(export_button)
        for removed_name in ("filmSimulationPlotSettingsButton",
                             "filmSimulationCsvButton", "filmSimulationPngButton"):
            self.assertIsNone(self._object(self.window, removed_name))

        menu = self._object(self.window, "filmSimulationExportMenu")
        csv_action = self._object(self.window, "filmSimulationExportCsvAction")
        png_action = self._object(self.window, "filmSimulationExportPngAction")
        self.assertIsNotNone(menu)
        self.assertIsNotNone(csv_action)
        self.assertIsNotNone(png_action)
        self.assertFalse(bool(csv_action.property("enabled")))
        self.assertFalse(bool(png_action.property("enabled")))

        self._click(self.window, parameter_button)
        dialog = self._object(self.window, "filmSimulationParametersDialog")
        self.assertTrue(bool(dialog.property("visible")))
        self.assertIsNotNone(self._object(self.window, "filmSimulationSettingsTabs"))
        self.assertIsNotNone(self._item(self.window, "filmSimulationParametersForm"))
        self.assertIsNotNone(self._item(self.window, "filmPlotSettingsForm"))
        self.assertIsNotNone(self._object(self.window, "filmSimulationCalculationTab"))
        self.assertIsNotNone(self._object(self.window, "filmSimulationPlotTab"))

    def test_export_menu_actions_open_original_dialogs_and_cancel_without_files(self):
        self.controller._result = {
            "wavelength_nm": [400.0, 700.0],
            "R": [0.25, 0.25], "T": [0.75, 0.75], "A": [0.0, 0.0],
            "settings": {"polarization": "unpolarized"},
        }
        self.controller._stale = False
        self.controller._render_plot = lambda: None
        self.controller._refresh_plot_data()
        self.controller.stateChanged.emit()
        self.assertTrue(self.controller.canExport)

        button = self._item(self.window, "filmSimulationExportButton")
        menu = self._object(self.window, "filmSimulationExportMenu")
        csv_action = self._item(self.window, "filmSimulationExportCsvAction")
        png_action = self._item(self.window, "filmSimulationExportPngAction")
        csv_dialog = self._object(self.window, "filmSimulationCsvFileDialog")
        png_dialog = self._object(self.window, "filmSimulationPngFileDialog")
        png_settings = self._object(self.window, "filmSimulationPngExportSettingsDialog")
        png_apply = self._item(self.window, "filmPlotExportApplyButton")
        self.assertTrue(bool(csv_action.property("enabled")))
        self.assertTrue(bool(png_action.property("enabled")))

        target_csv = self.root / "not-written.csv"
        self._click(self.window, button)
        self.assertTrue(bool(menu.property("visible")))
        self._click(self.window, csv_action)
        self.assertTrue(bool(csv_dialog.property("visible")))
        self.assertTrue(QMetaObject.invokeMethod(csv_dialog, "reject"))
        QCoreApplication.processEvents()
        self.assertFalse(bool(csv_dialog.property("visible")))
        self.assertFalse(target_csv.exists())

        self._click(self.window, button)
        self._click(self.window, png_action)
        self.assertTrue(bool(png_settings.property("visible")))
        # Closing the settings popup must not continue to the file picker.
        self.assertTrue(QMetaObject.invokeMethod(png_settings, "reject"))
        QCoreApplication.processEvents()
        self.assertFalse(bool(png_dialog.property("visible")))

        self._click(self.window, button)
        self._click(self.window, png_action)
        self.assertTrue(bool(png_settings.property("visible")))
        self._click(self.window, png_apply)
        self.assertTrue(bool(png_dialog.property("visible")))
        target_png = self.root / "not-written.png"
        self.assertTrue(QMetaObject.invokeMethod(png_dialog, "reject"))
        QCoreApplication.processEvents()
        self.assertFalse(bool(png_dialog.property("visible")))
        self.assertFalse(target_png.exists())
        self.assertEqual(len(self.window.findChildren(QObject, "filmSimulationExportCsvAction")), 1)
        self.assertEqual(len(self.window.findChildren(QObject, "filmSimulationExportPngAction")), 1)

    def test_combined_settings_validate_both_before_committing_and_plot_only_stays_fresh(self):
        original_simulation = copy.deepcopy(self.controller.settings)
        original_plot = copy.deepcopy(self.controller.plotSettings)
        invalid_plot = copy.deepcopy(original_plot)
        invalid_plot["x"].update(auto=False, min=800.0, max=400.0)
        changed_simulation = copy.deepcopy(original_simulation)
        changed_simulation["wavelength_max_nm"] = 650.0
        changed_simulation.pop("step_nm", None)

        cancelled = threading.Event()
        self.controller._calculation_cancel_event = cancelled
        self.controller._generation = 5
        self.assertFalse(self.controller.setSimulationAndPlotSettings(
            changed_simulation, invalid_plot))
        self.assertEqual(self.controller.settings, original_simulation)
        self.assertEqual(self.controller.plotSettings, original_plot)
        self.assertFalse(cancelled.is_set())
        self.assertEqual(self.controller._generation, 5)

        invalid_simulation = copy.deepcopy(original_simulation)
        invalid_simulation["angle_deg"] = 90.0
        changed_plot = copy.deepcopy(original_plot)
        changed_plot["grid"] = False
        self.assertFalse(self.controller.setSimulationAndPlotSettings(
            invalid_simulation, changed_plot))
        self.assertEqual(self.controller.settings, original_simulation)
        self.assertEqual(self.controller.plotSettings, original_plot)
        self.assertFalse(cancelled.is_set())
        self.assertEqual(self.controller._generation, 5)

        self.controller._calculation_cancel_event = None

        self.controller._stale = False
        plot_only = copy.deepcopy(original_plot)
        plot_only["grid"] = False
        self.assertTrue(self.controller.setSimulationAndPlotSettings(
            original_simulation, plot_only))
        self.assertFalse(self.controller.stale)
        self.assertEqual(self.controller.settings, original_simulation)
        self.assertFalse(self.controller.plotSettings["grid"])

    def test_combined_configuration_is_saved_and_reloaded(self):
        simulation = copy.deepcopy(self.controller.settings)
        simulation["wavelength_max_nm"] = 640.0
        simulation.pop("step_nm", None)
        plot = copy.deepcopy(self.controller.plotSettings)
        plot["grid"] = False
        self.assertTrue(self.controller.setSimulationAndPlotSettings(simulation, plot))
        path = self.root / "combined-settings.cfilm"
        self.assertTrue(self.controller.saveFilePath(str(path), False))
        loaded = FilmSimulationQuickController(
            root=self.root, service=self.service, design=self.service.load(str(path)))
        try:
            configuration = loaded.exportSimulationConfiguration()
            self.assertEqual(configuration["parameters"]["wavelength_max_nm"], 640.0)
            self.assertFalse(configuration["plot"]["grid"])
        finally:
            loaded.close()

    def test_dialog_cancel_discards_both_drafts_and_reopen_reloads_committed_values(self):
        button = self._item(self.window, "filmSimulationParametersButton")
        self._click(self.window, button)
        form = self._item(self.window, "filmSimulationParametersForm")
        grid = self._item(self.window, "filmPlotSettingsShowGrid")
        dialog = self._object(self.window, "filmSimulationParametersDialog")
        cancel = self._item(self.window, "filmSimulationParametersCancelButton")
        before_sim = copy.deepcopy(self.controller.settings)
        before_plot = copy.deepcopy(self.controller.plotSettings)
        draft = copy.deepcopy(self.controller.settings)
        draft["samples"] = self.controller.sampleCountText
        draft["wavelength_max_nm"] = 650.0
        form.setProperty("draftSettings", draft)
        grid.setProperty("checked", False)
        self._click(self.window, cancel)
        self.assertFalse(bool(dialog.property("visible")))
        self.assertEqual(self.controller.settings, before_sim)
        self.assertEqual(self.controller.plotSettings, before_plot)

        self._click(self.window, button)
        form = self._item(self.window, "filmSimulationParametersForm")
        grid = self._item(self.window, "filmPlotSettingsShowGrid")
        self.assertEqual(form.property("draftSettings").toVariant()["wavelength_max_nm"],
                         before_sim["wavelength_max_nm"])
        self.assertTrue(bool(grid.property("checked")))

    def test_small_quick_dialog_keeps_tabs_and_actions_reachable(self):
        self.window.setMinimumWidth(280)
        self.window.setMinimumHeight(280)
        self.window.setWidth(420)
        self.window.setHeight(440)
        self.window.show()
        QCoreApplication.processEvents()
        button = self._item(self.window, "filmSimulationParametersButton")
        self._click(self.window, button)
        dialog = self._object(self.window, "filmSimulationParametersDialog")
        cancel = self._item(self.window, "filmSimulationParametersCancelButton")
        apply = self._item(self.window, "filmSimulationParametersApplyButton")
        row = self._item(self.window, "filmSimulationParametersActionRow")
        self.assertLessEqual(float(dialog.property("width")), self.window.width())
        self.assertLessEqual(float(dialog.property("height")), self.window.height())
        # The initial visible frame must fit too, before any tab interaction.
        content = dialog.property("contentItem")
        self.assertIsInstance(content, QQuickItem)
        content.ensurePolished()
        self.assertFalse(self.window.grabWindow().isNull())
        for item in (cancel, apply):
            point = item.mapToScene(QPointF(0, 0))
            self.assertGreaterEqual(point.x(), -1)
            self.assertGreaterEqual(point.y(), -1)
            self.assertLessEqual(point.x() + item.width(), self.window.width() + 1)
            self.assertLessEqual(point.y() + item.height(), self.window.height() + 1)
        for tab_name in ("filmSimulationCalculationTab", "filmSimulationPlotTab"):
            self._click(self.window, self._item(self.window, tab_name))
            # processEvents alone can leave Qt Quick's layout polish pending.
            # Inspect the first rendered frame, not a guessed delay or a loop
            # that waits for the expected bounds to become true.
            content = dialog.property("contentItem")
            self.assertIsInstance(content, QQuickItem)
            content.ensurePolished()
            self.assertFalse(self.window.grabWindow().isNull())
            self.assertTrue(cancel.isVisible())
            self.assertTrue(apply.isVisible())
            self.assertTrue(row.isVisible())
            for item in (cancel, apply):
                point = item.mapToScene(QPointF(0, 0))
                self.assertGreaterEqual(point.x(), -1)
                self.assertGreaterEqual(point.y(), -1)
                self.assertLessEqual(point.x() + item.width(), self.window.width() + 1)
                self.assertLessEqual(point.y() + item.height(), self.window.height() + 1)
        self._click(self.window, cancel)

    def test_apply_commits_both_pages_once(self):
        self._click(self.window, self._item(self.window, "filmSimulationParametersButton"))
        form = self._item(self.window, "filmSimulationParametersForm")
        draft = copy.deepcopy(self.controller.settings)
        draft["samples"] = self.controller.sampleCountText
        draft["wavelength_max_nm"] = 650.0
        form.setProperty("draftSettings", draft)
        form.setProperty("samplingPriority", "samples")
        grid = self._item(self.window, "filmPlotSettingsShowGrid")
        grid.setProperty("checked", False)
        apply = self._item(self.window, "filmSimulationParametersApplyButton")
        self._click(self.window, apply)

        self.assertEqual(self.controller.settings["wavelength_max_nm"], 650.0)
        self.assertFalse(self.controller.plotSettings["grid"])

    def test_widget_fallback_uses_a_single_tabbed_modal_editor(self):
        dialog = FilmSimulationAndPlotSettingsDialog(
            self.controller.settings, self.controller.plotSettings, parent=None)
        try:
            self.assertEqual(dialog.tabs.count(), 2)
            self.assertEqual(dialog.tabs.tabText(0), "计算参数")
            self.assertEqual(dialog.tabs.tabText(1), "绘图参数")
            simulation, plot = dialog.values()
            self.assertEqual(simulation["samples"], self.controller.settings["samples"])
            self.assertIn("curves", plot)
            self.assertEqual(dialog.button_box.button(QDialogButtonBox.StandardButton.Cancel).text(), "取消")
            self.assertEqual(dialog.minimumWidth(), 320)
            dialog.resize(320, 300)
            dialog.show()
            QCoreApplication.processEvents()
            self.assertLessEqual(dialog.width(), 320)
            self.assertTrue(dialog.simulation_scroll.widgetResizable())
            self.assertTrue(dialog.plot_scroll.widgetResizable())
            dialog.simulation_form.angle_spin.setValue(15.0)
            dialog.plot_form.grid_check.setChecked(False)
            dialog.button_box.button(QDialogButtonBox.StandardButton.Cancel).click()
            self.assertEqual(dialog.result(), 0)
            dialog2 = FilmSimulationAndPlotSettingsDialog(
                self.controller.settings, self.controller.plotSettings, parent=None)
            dialog2.show()
            QCoreApplication.processEvents()
            dialog2.tabs.setCurrentIndex(0)
            dialog2.simulation_form.angle_spin.setFocus()
            QTest.keyClick(dialog2.simulation_form.angle_spin, Qt.Key.Key_Escape)
            QCoreApplication.processEvents()
            self.assertEqual(dialog2.result(), 0)
            self.assertFalse(dialog2.isVisible())
            dialog2.close()
            dialog3 = FilmSimulationAndPlotSettingsDialog(
                self.controller.settings, self.controller.plotSettings, parent=None)
            dialog3.show()
            QCoreApplication.processEvents()
            dialog3.tabs.setCurrentIndex(1)
            dialog3.plot_form.axis_controls["x"]["minimum"].setFocus()
            QTest.keyClick(dialog3.plot_form.axis_controls["x"]["minimum"], Qt.Key.Key_Escape)
            QCoreApplication.processEvents()
            self.assertEqual(dialog3.result(), 0)
            self.assertFalse(dialog3.isVisible())
            dialog3.close()
        finally:
            dialog.close()

        applied = FilmSimulationAndPlotSettingsDialog(
            self.controller.settings, self.controller.plotSettings, parent=None)
        try:
            applied.simulation_form.angle_spin.setValue(15.0)
            applied.plot_form.grid_check.setChecked(False)
            applied.button_box.button(QDialogButtonBox.StandardButton.Apply).click()
            self.assertEqual(applied.result(), 1)
            sim, plot = applied.values()
            self.assertEqual(sim["angle_deg"], 15.0)
            self.assertFalse(plot["grid"])
        finally:
            applied.close()


if __name__ == "__main__":
    unittest.main()
