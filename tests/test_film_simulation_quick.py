"""Focused controller and offscreen Quick tests for the spectrum window."""
import unittest
import json
import csv
import math
import os
import struct
import tempfile
import threading
import time
import warnings
from pathlib import Path

from PySide6.QtCore import QObject, QEvent, QPoint, QPointF, QSettings, Qt, qInstallMessageHandler
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QSignalSpy, QTest

from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from coat_sim.ui.film_simulation_quick_controller import (
    FilmSimulationQuickController,
    _automatic_y_limits,
    _calculate_band_statistics,
    _display_indices,
    _result_summary,
    _simulate_with_polarizations,
    create_film_simulation_quick_window,
)


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QQuickStyle.setStyle("Fusion")
_APP = QGuiApplication.instance() or QGuiApplication([])


class FilmSimulationQuickControllerTests(unittest.TestCase):
    def setUp(self):
        self._geometry_tempdir = tempfile.TemporaryDirectory()
        self._geometry_settings = QSettings(
            str(Path(self._geometry_tempdir.name) / "geometry.ini"), QSettings.Format.IniFormat)
        self.controller = FilmSimulationQuickController()

    def tearDown(self):
        self.controller.close()
        self._geometry_settings.sync()
        self._geometry_tempdir.cleanup()

    def _create_quick_window(self, *args, **kwargs):
        kwargs.setdefault("geometry_settings", self._geometry_settings)
        return create_film_simulation_quick_window(*args, **kwargs)

    @staticmethod
    def _synthetic_glass_fixture(root):
        library = MaterialLibrary(root)
        record = default_material_profile("Synthetic n=1.5 glass", id="fixture_glass")
        record.update(category="substrate", optical_constants=[{
            "id": "glass", "name": "constant n=1.5", "kind": "tabulated",
            "notes": "Analytical test fixture, not production data",
            "source": "Fresnel reference fixture", "wavelength_unit": "nm",
            "wavelength_min": 400.0, "wavelength_max": 700.0,
            "wavelengths": [400.0, 700.0], "n": [1.5, 1.5], "k": [0.0, 0.0],
        }])
        library.write(record)
        service = FilmDesignService(root)
        design = service.new_design()
        design["substrate"].update(material_id="fixture_glass", optical_set_id="glass")
        return service, design

    @staticmethod
    def _wait_until(predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            _APP.processEvents()
            time.sleep(0.005)
        _APP.processEvents()
        return predicate()

    @staticmethod
    def _click_quick_item(window, item):
        if not window.isVisible():
            window.show()
        _APP.processEvents()
        ancestors = [item]
        while ancestors[-1].parentItem() is not None:
            ancestors.append(ancestors[-1].parentItem())
        for ancestor in reversed(ancestors):
            ancestor.ensurePolished()
        window.contentItem().ensurePolished()
        frame = window.grabWindow()
        assert not frame.isNull(), "Qt Quick window did not render while preparing a click"
        _APP.processEvents()

        scene_center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        click_point = QPoint(round(scene_center.x()), round(scene_center.y()))
        assert item.isVisible() and item.isEnabled() and item.width() > 0 and item.height() > 0, (
            f"Qt Quick target {item.objectName()} is hidden/disabled/empty")
        assert (0 <= click_point.x() < window.width()
                and 0 <= click_point.y() < window.height()), (
            f"Qt Quick target {item.objectName()} center outside window: "
            f"scene=({scene_center.x():.3f},{scene_center.y():.3f}), "
            f"click=({click_point.x()},{click_point.y()}), "
            f"window={window.width()}x{window.height()}")
        ancestor = item.parentItem()
        while ancestor is not None:
            if ancestor.clip():
                local_center = ancestor.mapFromScene(QPointF(click_point))
                assert (0 <= local_center.x() < ancestor.width()
                        and 0 <= local_center.y() < ancestor.height()), (
                    f"Qt Quick target {item.objectName()} clipped by {ancestor.objectName()}: "
                    f"local=({local_center.x():.3f},{local_center.y():.3f}), "
                    f"clip={ancestor.width()}x{ancestor.height()}")
            ancestor = ancestor.parentItem()

        QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, click_point)
        _APP.processEvents()

    def _assert_dialog_action_row(self, window, row_name, cancel_name, apply_name):
        row = window.findChild(QObject, row_name)
        cancel = window.findChild(QObject, cancel_name)
        apply = window.findChild(QObject, apply_name)
        self.assertIsNotNone(row)
        self.assertIsNotNone(cancel)
        self.assertIsNotNone(apply)
        self.assertLess(cancel.x(), apply.x())
        self.assertLess(abs(cancel.y() - apply.y()), 1.0)
        self.assertLessEqual(apply.x() + apply.width(), row.width() + 1.0)
        self.assertFalse(cancel.property("primary"))
        self.assertTrue(apply.property("primary"))

        cancel.forceActiveFocus()
        _APP.processEvents()
        focused = window.activeFocusItem()
        self.assertIsNotNone(focused)
        self.assertEqual(focused.objectName(), cancel_name)
        QTest.keyClick(window, Qt.Key_Tab)
        _APP.processEvents()
        focused = window.activeFocusItem()
        self.assertIsNotNone(focused)
        self.assertEqual(focused.objectName(), apply_name)
        return cancel, apply

    def test_default_waveband_and_step_point_synchronization(self):
        settings = self.controller.settings
        self.assertEqual(settings["wavelength_min_nm"], 400.0)
        self.assertEqual(settings["wavelength_max_nm"], 700.0)
        self.assertEqual(settings["samples"], 301)
        self.assertAlmostEqual(settings["step_nm"], 1.0)

        self.controller.setParameter("wavelength_min_nm", 500.0)
        self.assertEqual(self.controller.settings["samples"], 201)
        self.controller.setParameter("samples", 5)
        self.assertEqual(self.controller.settings["samples"], 5)
        self.assertAlmostEqual(self.controller.settings["step_nm"], 50.0)
        self.controller.setParameter("step_nm", 7.0)
        self.assertEqual(self.controller.settings["samples"], 30)
        self.assertAlmostEqual(self.controller.settings["step_nm"], 200 / 29)

    def test_large_sample_counts_stay_exact_without_allocating_a_grid(self):
        for count in (3_000_000_001, 1_000_000_000_000_000):
            self.controller.setParameter("samples", count)
            self.assertEqual(self.controller.settings["samples"], count)
            raw = self.controller._raw_settings()
            self.assertNotIn("step_nm", raw)
            self.assertEqual(self.controller._current_settings()["samples"], count)
            self.assertTrue(self.controller.setSimulationSettings(self.controller.settings))
            self.assertEqual(self.controller._current_settings()["samples"], count)
        self.assertTrue(self.controller.setSimulationSettings({
            "wavelength_min_nm": 400.0, "wavelength_max_nm": 420.0,
            "samples": 301, "step_nm": 1.0,
        }))
        self.assertEqual(self.controller._current_settings()["samples"], 21)

    def test_design_json_interface_uses_existing_model_schema(self):
        design = new_design("Quick input")
        self.controller.setDesignJson(json.dumps(design, ensure_ascii=False))
        self.assertEqual(self.controller.designName, "Quick input")
        self.assertEqual(json.loads(self.controller.designJson), design)

    def test_load_existing_v1_cfilm_and_reject_invalid_internal_json_without_replacing(self):
        design = new_design("Loaded from v1")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "film.cfilm"
            path.write_text(json.dumps(design, ensure_ascii=False), encoding="utf-8")
            self.controller.loadDesignFromPath(str(path))
        self.assertEqual(self.controller.designName, "Loaded from v1")
        previous = self.controller.designJson
        self.controller.setDesignJson("{")
        self.assertEqual(self.controller.designJson, previous)

    def test_invalid_range_is_reported_and_not_committed(self):
        original = self.controller.settings["wavelength_min_nm"]
        self.controller.setParameter("wavelength_min_nm", 701.0)
        self.assertEqual(self.controller.settings["wavelength_min_nm"], original)
        self.assertIn("小于最大值", self.controller.validationError)

    def test_display_downsampling_preserves_endpoints_and_bucket_extrema(self):
        values = [0.0] * 10001
        values[431] = -4.0
        values[8432] = 9.0
        indices = _display_indices(values, 1000)
        self.assertLessEqual(len(indices), 1000)
        self.assertEqual(indices[0], 0)
        self.assertEqual(indices[-1], len(values) - 1)
        self.assertIn(431, indices)
        self.assertIn(8432, indices)

    def test_result_summary_hides_k_zero_warning_and_keeps_n_domain_details(self):
        result = {
            "definition": "half-infinite substrate",
            "provenance": [{"evaluation": {
                "n": {
                    "source_range_nm": [500.0, 650.0],
                    "effective_input_range_nm": [400.0, 650.0],
                    "interpolation_method": "piecewise_linear",
                    "interpolated_segments_nm": [500.0, 650.0],
                    "extrapolated_ranges_nm": [[400.0, 500.0]],
                },
                "k": {
                    "enabled": True,
                    "source_range_nm": [450.0, 650.0],
                    "effective_input_range_nm": [450.0, 650.0],
                    "interpolation_method": "linear",
                    "default_zero_intervals": [{
                        "min_nm": 400.0, "max_nm": 450.0,
                        "min_inclusive": True, "max_inclusive": False,
                        "reason": "outside_source_range",
                    }],
                },
            }}],
        }
        summary = _result_summary(result)
        self.assertNotIn("\u6309 0 \u5904\u7406", summary)
        self.assertNotIn("\u8d85\u51fa k \u6765\u6e90\u8303\u56f4", summary)
        self.assertIn("500\u2013650 nm", summary)
        self.assertIn("\u5916\u63a8\u8303\u56f4 400\u2013500 nm", summary)

    def test_plot_view_zoom_pan_and_reset_leave_simulation_input_unchanged(self):
        self.controller._result = {
            "wavelength_nm": [400.0, 500.0, 600.0],
            "R": [0.1, 0.2, 0.3],
        }
        self.controller._render_plot = lambda: None
        original_inputs = self.controller.settings
        initial = self.controller.plotView

        self.controller.zoomPlot(0.5, 0.5, 0.5)
        zoomed = self.controller.plotView
        self.assertAlmostEqual(zoomed["x_max"] - zoomed["x_min"],
                               (initial["x_max"] - initial["x_min"]) / 2)
        self.controller.panPlot(0.1, 0.1)
        panned = self.controller.plotView
        self.assertNotEqual(panned, zoomed)
        self.controller.resetPlotView()
        self.assertEqual(self.controller.plotView, initial)
        self.assertEqual(self.controller.settings, original_inputs)

    def test_plot_cursor_reports_nearest_selected_sample(self):
        self.controller._result = {
            "wavelength_nm": [400.0, 500.0, 600.0],
            "R": [0.1, 0.2, 0.3],
            "T": [0.8, 0.7, 0.6],
            "A": [0.1, 0.1, 0.1],
        }
        self.controller.setPlotCursor(550.0)
        self.assertIn("500 nm", self.controller.cursorReadout)
        self.assertIn("20.000%", self.controller.cursorReadout)
        self.controller.clearPlotCursor()
        self.assertEqual(self.controller.cursorReadout, "")

    def test_percent_axis_settings_are_stored_as_fraction_values(self):
        accepted = self.controller.setPlotSettings({
            "curves": ["R"],
            "x": {"auto": True, "major_tick_auto": True},
            "y": {"unit": "percent", "auto": False, "min": -10, "max": 110,
                  "major_tick_auto": False, "major_tick": 10},
            "grid": True,
        })
        self.assertTrue(accepted)
        settings = self.controller.plotSettings["y"]
        self.assertAlmostEqual(settings["min"], -0.1)
        self.assertAlmostEqual(settings["max"], 1.1)
        self.assertAlmostEqual(settings["major_tick"], 0.1)

    def test_auto_y_uses_visible_rta_and_applying_settings_clears_old_viewport(self):
        self.controller._result = {
            "wavelength_nm": [400.0, 500.0, 600.0],
            "R": [0.2, 0.4, 0.8],
            "T": [0.0, 0.1, 0.2],
            "A": [0.9, 0.95, 1.0],
        }
        self.controller._view_bounds = {
            "x_min": 450.0, "x_max": 550.0, "y_min": 0.45, "y_max": 0.55,
        }
        self.controller._render_plot = lambda: None

        self.assertTrue(self.controller.setPlotSettings({
            "curves": ["R"],
            "x": {"auto": True},
            "y": {"unit": "percent", "auto": True},
        }))

        self.assertIsNone(self.controller._view_bounds)
        bounds = self.controller._plot_bounds()
        self.assertAlmostEqual(bounds["y_min"], 0.17)
        self.assertAlmostEqual(bounds["y_max"], 0.83)

    def test_png_export_options_validate_and_commit_atomically(self):
        options = {"width_in": 10, "height_in": 6, "dpi": 240, "transparent": True}
        self.assertTrue(self.controller.setPngExportSettings(options))
        self.assertEqual(self.controller.pngExportSettings, {
            "width_in": 10.0, "height_in": 6.0, "dpi": 240, "transparent": True,
        })
        self.assertFalse(self.controller.setPngExportSettings({
            "width_in": 10, "height_in": 6, "dpi": 301, "transparent": False,
        }))
        self.assertEqual(self.controller.pngExportSettings, {
            "width_in": 10.0, "height_in": 6.0, "dpi": 240, "transparent": True,
        })

    def test_fusion_qml_window_loads_with_theme_bridge_without_dialog_accessibility_warning(self):
        from coat_sim.ui.theme_service import ThemeService

        qt_messages = []
        previous_handler = qInstallMessageHandler(
            lambda kind, context, message: qt_messages.append((str(kind), str(message))))
        engine = window = controller = None
        try:
            engine, window, controller = self._create_quick_window()
        finally:
            qInstallMessageHandler(previous_handler)
        try:
            window.show()
            _APP.processEvents()
            self.assertIsNotNone(window)
            self.assertIsNotNone(engine.rootContext().contextProperty("Theme"))
            self.assertGreaterEqual(window.property("themeRevision"), 0)
            self.assertEqual(ThemeService.instance().current_revision,
                             window.property("themeRevision"))
            qml_dir = Path(__file__).resolve().parents[1] / "coat_sim" / "ui" / "qml"
            quick_source = (qml_dir / "FilmSimulationQuick.qml").read_text(encoding="utf-8")
            app_window_source = (qml_dir / "AppWindow.qml").read_text(encoding="utf-8")
            self.assertIn('import "."', quick_source)
            self.assertIn("\nAppWindow {", quick_source)
            self.assertIn("LocalComponents.WindowChrome {", app_window_source)
            self.assertIsNotNone(window.findChild(QObject, "windowChromeTitleDrag"))

            action_bar = window.findChild(QObject, "filmSimulationActionBar")
            action_names = (
                "filmSimulationLoadButton", "filmSimulationParametersButton",
                "filmSimulationExportButton", "filmSimulationCalculateButton",
            )
            actions = [window.findChild(QObject, name) for name in action_names]
            self.assertIsNotNone(action_bar)
            self.assertTrue(all(action is not None for action in actions))
            for removed in ("filmSimulationPlotSettingsButton", "filmSimulationCsvButton",
                            "filmSimulationPngButton"):
                self.assertIsNone(window.findChild(QObject, removed))
            quick_source_prefix = quick_source[:quick_source.index("FilmSpectrumPlot {")]
            for name in action_names:
                self.assertIn(f'objectName: "{name}"', quick_source_prefix)
            self.assertNotIn("k 缺失或超出", quick_source)
            parameter_source = (qml_dir / "FilmSimulationParameters.qml").read_text(encoding="utf-8")
            self.assertNotIn("缺失 k", parameter_source)
            self.assertNotIn("按 0 处理", parameter_source)

            original_width = window.width()
            for width, compact in ((original_width, original_width < 1000), (720, True)):
                window.setWidth(width)
                _APP.processEvents()
                self.assertEqual(bool(window.property("compactActions")), compact)
                widths = [float(action.width()) for action in actions]
                ys = [float(action.y()) for action in actions]
                right = max(float(action.x()) + float(action.width()) for action in actions)
                self.assertTrue(all(value > 0 for value in widths))
                if float(action_bar.height()) <= max(float(action.height()) for action in actions) + 1:
                    self.assertLess(max(ys) - min(ys), 1.5)
                else:
                    self.assertGreater(len(set(ys)), 1, "Narrow action bars should wrap")
                self.assertTrue(all(float(action.y()) + float(action.height())
                                    <= float(action_bar.height()) + 1 for action in actions))
                self.assertLessEqual(right, float(action_bar.width()) + 1.0)

            statistics = window.findChild(QObject, "filmSimulationCalculationItems")
            statistics_toggle = window.findChild(QObject, "filmSimulationStatisticsToggle")
            threshold = window.findChild(QObject, "filmSpectrumAnalysisThreshold")
            self.assertTrue(all((statistics, statistics_toggle, threshold)))
            window.setWidth(1100)
            _APP.processEvents()
            expanded_width = float(statistics.width())
            threshold.forceActiveFocus()
            _APP.processEvents()
            QTest.keyClick(window, Qt.Key_F4)
            _APP.processEvents()
            self.assertFalse(window.property("statisticsExpanded"))
            self.assertFalse(statistics.property("expanded"))
            self.assertTrue(self._wait_until(lambda: not statistics.isVisible()))
            self.assertFalse(threshold.isVisible())
            focused = window.activeFocusItem()
            self.assertNotEqual(
                None if focused is None else focused.objectName(),
                "filmSpectrumAnalysisThreshold")

            self._click_quick_item(window, statistics_toggle)
            self.assertTrue(window.property("statisticsExpanded"))
            self.assertTrue(statistics.property("expanded"))
            self.assertTrue(statistics.isVisible())
            self.assertTrue(self._wait_until(lambda: float(statistics.width()) > 44.0))
            self.assertGreater(float(statistics.width()), 44.0)
            QTest.keyClick(window, Qt.Key_F4)
            _APP.processEvents()
            self.assertFalse(statistics.property("expanded"))
            self.assertTrue(self._wait_until(lambda: not statistics.isVisible()))
            self._click_quick_item(window, statistics_toggle)
            self.assertTrue(window.property("statisticsExpanded"))
            self.assertTrue(statistics.property("expanded"))
            self.assertTrue(self._wait_until(lambda: float(statistics.width()) > expanded_width / 2))
            self.assertGreater(float(statistics.width()), expanded_width / 2)

            for filename in ("FilmPlotSettings.qml", "FilmPlotExportSettings.qml"):
                dialog_source = (qml_dir / filename).read_text(encoding="utf-8")
                self.assertIn("contentItem: ColumnLayout {", dialog_source)
                self.assertIn("Accessible.role: Accessible.Dialog", dialog_source)
                self.assertIn("Accessible.name: dialog.title", dialog_source)
            unsupported_dialog_accessibility = [
                message for _, message in qt_messages
                if "Accessible" in message and "Dialog" in message
            ]
            self.assertEqual(unsupported_dialog_accessibility, [])
        finally:
            controller.close()
            window.close()
            engine.deleteLater()
            _APP.sendPostedEvents(None, QEvent.DeferredDelete)
            _APP.processEvents()

    def test_parameters_button_dialog_apply_and_cancel(self):
        engine = window = controller = None
        try:
            engine, window, controller = self._create_quick_window()
            root = engine.rootObjects()[0]
            self.assertFalse(window.isVisible())
            self.assertTrue(window.property("_coat_sim_geometry_prepared"))
            self.assertGreater(window.width(), 0)
            self.assertGreater(window.height(), 0)
            button = root.findChild(QObject, "filmSimulationParametersButton")
            dialog = root.findChild(QObject, "filmSimulationParametersDialog")
            form = root.findChild(QObject, "filmSimulationParametersForm")
            apply_button = root.findChild(QObject, "filmSimulationParametersApplyButton")
            self.assertIsNotNone(button)
            self.assertIsNotNone(dialog)
            self.assertIsNotNone(form)
            self.assertIsNotNone(apply_button)

            self._click_quick_item(window, button)
            self.assertTrue(dialog.property("visible"))
            self.assertTrue(window.isVisible())
            committed = dict(controller.settings)
            draft = dict(committed)
            draft["angle_deg"] = 17.0
            form.setProperty("draftSettings", draft)
            dialog.reject()
            _APP.processEvents()
            self.assertEqual(dict(controller.settings), committed)

            self._click_quick_item(window, button)
            draft = dict(controller.settings)
            draft["angle_deg"] = 17.0
            form.setProperty("draftSettings", draft)
            self._click_quick_item(window, apply_button)
            self.assertAlmostEqual(controller.settings["angle_deg"], 17.0)
            self.assertFalse(dialog.property("visible"))
        finally:
            if controller is not None:
                controller.close()
            if window is not None:
                window.close()
            if engine is not None:
                engine.deleteLater()
                _APP.sendPostedEvents(None, QEvent.DeferredDelete)
                _APP.processEvents()

    def test_parameter_and_plot_dialog_buttons_keyboard_cancel_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = window = controller = None
            try:
                engine, window, controller = self._create_quick_window(root=folder)
                self.assertFalse(window.isVisible())
                self.assertTrue(window.property("_coat_sim_geometry_prepared"))
                root = engine.rootObjects()[0]
                parameters_button = window.findChild(QObject, "filmSimulationParametersButton")
                parameters_dialog = window.findChild(QObject, "filmSimulationParametersDialog")
                parameters_form = window.findChild(QObject, "filmSimulationParametersForm")
                self.assertIsNotNone(parameters_button)
                self.assertIsNotNone(parameters_dialog)
                self.assertIsNotNone(parameters_form)

                before_parameters = dict(controller.settings)
                self._click_quick_item(window, parameters_button)
                self.assertTrue(self._wait_until(lambda: bool(parameters_dialog.property("visible"))))
                cancel, apply = self._assert_dialog_action_row(
                    window, "filmSimulationParametersActionRow",
                    "filmSimulationParametersCancelButton",
                    "filmSimulationParametersApplyButton")

                draft = dict(before_parameters)
                draft["angle_deg"] = 12.0
                parameters_form.setProperty("draftSettings", draft)
                initial_size = (window.width(), window.height())
                initial_minimum = (window.minimumWidth(), window.minimumHeight())
                window.setMinimumWidth(280)
                window.setMinimumHeight(280)
                window.resize(420, 440)
                self.assertEqual((window.width(), window.height()), (420, 440))
                # Leave the resize event pending; the click helper must polish
                # the existing dialog against the resized window before clicking.
                self._click_quick_item(window, cancel)
                self.assertTrue(self._wait_until(lambda: not parameters_dialog.property("visible")))
                self.assertEqual(dict(controller.settings), before_parameters)
                window.setMinimumWidth(initial_minimum[0])
                window.setMinimumHeight(initial_minimum[1])
                window.resize(*initial_size)
                _APP.processEvents()

                self._click_quick_item(window, parameters_button)
                self.assertTrue(self._wait_until(lambda: bool(parameters_dialog.property("visible"))))
                draft = dict(controller.settings)
                draft["angle_deg"] = 14.0
                parameters_form.setProperty("draftSettings", draft)
                QTest.keyClick(window, Qt.Key_Escape)
                _APP.processEvents()
                self.assertTrue(self._wait_until(lambda: not parameters_dialog.property("visible")))
                self.assertEqual(dict(controller.settings), before_parameters)

                self._click_quick_item(window, parameters_button)
                self.assertTrue(self._wait_until(lambda: bool(parameters_dialog.property("visible"))))
                draft = dict(controller.settings)
                draft["angle_deg"] = 18.0
                parameters_form.setProperty("draftSettings", draft)
                self._click_quick_item(window, apply)
                self.assertTrue(self._wait_until(lambda: not parameters_dialog.property("visible")))
                self.assertAlmostEqual(controller.settings["angle_deg"], 18.0)

                plot_button = parameters_button
                plot_dialog = parameters_dialog
                plot_tab = window.findChild(QObject, "filmSimulationPlotTab")
                plot_form = window.findChild(QObject, "filmPlotSettingsForm")
                x_auto = window.findChild(QObject, "filmPlotSettingsXAuto")
                x_min = window.findChild(QObject, "filmPlotSettingsXMin")
                x_max = window.findChild(QObject, "filmPlotSettingsXMax")
                y_auto = window.findChild(QObject, "filmPlotSettingsYAuto")
                y_min = window.findChild(QObject, "filmPlotSettingsYMin")
                y_max = window.findChild(QObject, "filmPlotSettingsYMax")
                grid_check = window.findChild(QObject, "filmPlotSettingsShowGrid")
                polarization_s = window.findChild(QObject, "filmPlotSettingsPolarizationS")
                polarization_p = window.findChild(QObject, "filmPlotSettingsPolarizationP")
                self.assertIsNotNone(plot_button)
                self.assertIsNotNone(plot_dialog)
                self.assertIsNotNone(plot_tab)
                self.assertIsNotNone(plot_form)
                self.assertIsNotNone(x_auto)
                self.assertIsNotNone(x_min)
                self.assertIsNotNone(x_max)
                self.assertIsNotNone(y_auto)
                self.assertIsNotNone(y_min)
                self.assertIsNotNone(y_max)
                self.assertIsNotNone(grid_check)
                self.assertIsNotNone(polarization_s)
                self.assertIsNotNone(polarization_p)
                before_plot = json.loads(json.dumps(controller.plotSettings))

                def open_plot_dialog():
                    opened = QSignalSpy(plot_dialog.opened)
                    self._click_quick_item(window, plot_button)
                    self.assertTrue(self._wait_until(lambda: bool(plot_dialog.property("visible"))))
                    if opened.count() == 0:
                        self.assertTrue(opened.wait(1000))
                    self._click_quick_item(window, plot_tab)
                    _APP.processEvents()

                def edit_numeric(field, value):
                    self._click_quick_item(window, field)
                    _APP.processEvents()
                    QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
                    for character in value:
                        QTest.keyClick(window, Qt.Key(ord(character)))
                    _APP.processEvents()
                    self.assertTrue(field.property("numericValueEdited"))
                    self.assertEqual(field.property("numericEditText"), value)

                open_plot_dialog()
                plot_cancel, plot_apply = self._assert_dialog_action_row(
                    window, "filmSimulationParametersActionRow",
                    "filmSimulationParametersCancelButton", "filmSimulationParametersApplyButton")

                self.assertTrue(x_auto.property("checked"))
                self.assertTrue(x_min.property("enabled"))
                self.assertTrue(y_auto.property("checked"))
                self.assertTrue(y_min.property("enabled"))
                self.assertTrue(y_max.property("enabled"))
                self._click_quick_item(window, x_min)
                QTest.keyClick(window, Qt.Key.Key_Tab)
                _APP.processEvents()
                self.assertTrue(x_auto.property("checked"),
                                "focus alone must not disable the automatic X range")
                self._click_quick_item(window, grid_check)
                self.assertTrue(x_auto.property("checked"),
                                "changing the grid must not disable the automatic X range")
                self.assertTrue(y_auto.property("checked"),
                                "changing the grid must not disable the automatic Y range")
                self._click_quick_item(window, plot_cancel)
                self.assertTrue(self._wait_until(lambda: not plot_dialog.property("visible")))

                open_plot_dialog()
                self.assertTrue(x_auto.property("checked"))
                edit_numeric(x_min, "400")
                controller.stateChanged.emit()
                _APP.processEvents()
                self.assertEqual(x_min.property("text"), "400",
                                 "controller notifications must leave the focused draft visible")
                QTest.keyClick(window, Qt.Key.Key_Enter)
                _APP.processEvents()
                self.assertTrue(x_auto.property("checked"),
                                "re-entering the existing range must leave auto enabled")
                self.assertEqual(x_min.property("text"), "400")

                edit_numeric(x_min, "420")
                controller.stateChanged.emit()
                _APP.processEvents()
                self.assertEqual(x_min.property("text"), "420",
                                 "controller notifications must not overwrite current input")
                self.assertTrue(x_auto.property("checked"),
                                "the range changes only when an edited value is committed")
                self._click_quick_item(window, x_max)
                self.assertFalse(x_auto.property("checked"),
                                 "a changed X range must switch that axis to manual")
                self.assertEqual(x_min.property("text"), "420",
                                 "the unfocused field must keep its edited value visible")
                self._click_quick_item(window, plot_cancel)
                self.assertTrue(self._wait_until(lambda: not plot_dialog.property("visible")))

                open_plot_dialog()
                self.assertTrue(x_auto.property("checked"),
                                "reopening after cancel must reload the controller setting")
                self.assertEqual(x_min.property("text"), "400",
                                 "reopening after cancel must discard the local range draft")
                edit_numeric(y_min, "10")
                self._click_quick_item(window, y_max)
                self.assertFalse(y_auto.property("checked"),
                                 "a changed Y range must switch that axis to manual")
                self.assertEqual(y_min.property("text"), "10")
                self._click_quick_item(window, plot_cancel)
                self.assertTrue(self._wait_until(lambda: not plot_dialog.property("visible")))

                open_plot_dialog()
                self.assertTrue(y_auto.property("checked"))
                self.assertEqual(y_min.property("text"), "0")
                self._click_quick_item(window, plot_cancel)
                self.assertTrue(self._wait_until(lambda: not plot_dialog.property("visible")))

                open_plot_dialog()
                self._click_quick_item(window, x_auto)
                edit_numeric(x_min, "420")
                edit_numeric(x_max, "680")
                self._click_quick_item(window, plot_cancel)
                self.assertTrue(self._wait_until(lambda: not plot_dialog.property("visible")))
                self.assertEqual(controller.plotSettings, before_plot)

                open_plot_dialog()
                self._click_quick_item(window, x_auto)
                edit_numeric(x_min, "430")
                edit_numeric(x_max, "670")
                QTest.keyClick(window, Qt.Key_Escape)
                _APP.processEvents()
                self.assertTrue(self._wait_until(lambda: not plot_dialog.property("visible")))
                self.assertEqual(controller.plotSettings, before_plot)

                open_plot_dialog()
                self._click_quick_item(window, x_auto)
                edit_numeric(x_min, "700")
                edit_numeric(x_max, "400")
                self._click_quick_item(window, plot_apply)
                _APP.processEvents()
                self.assertTrue(plot_dialog.property("visible"))
                self.assertTrue(plot_dialog.property("localError"))
                self.assertEqual(controller.plotSettings, before_plot)
                self._click_quick_item(window, plot_cancel)
                self.assertTrue(self._wait_until(lambda: not plot_dialog.property("visible")))

                open_plot_dialog()
                edit_numeric(x_min, "420")
                edit_numeric(x_max, "680")
                self.assertFalse(x_auto.property("checked"),
                                 "range edits should disable auto without clicking its checkbox")
                self.assertEqual(x_min.property("text"), "420")
                self._click_quick_item(window, plot_apply)
                self.assertTrue(self._wait_until(lambda: not plot_dialog.property("visible")))
                self.assertAlmostEqual(controller.plotSettings["x"]["min"], 420.0)
                self.assertAlmostEqual(controller.plotSettings["x"]["max"], 680.0)
                self.assertFalse(controller.plotSettings["x"]["auto"])
                self.assertTrue(controller.plotSettings["y"]["auto"])

                open_plot_dialog()
                self.assertFalse(x_auto.property("checked"))
                self.assertEqual(x_min.property("text"), "420")
                self.assertEqual(x_max.property("text"), "680")
                self._click_quick_item(window, plot_cancel)
                self.assertTrue(self._wait_until(lambda: not plot_dialog.property("visible")))

                open_plot_dialog()
                polarization_s.setProperty("checked", True)
                polarization_p.setProperty("checked", True)
                self._click_quick_item(window, plot_apply)
                self.assertTrue(self._wait_until(lambda: not plot_dialog.property("visible")))
                self.assertEqual(controller.plotSettings["curves"], ["R"])
                self.assertEqual(controller.plotSettings["polarizations"], ["s", "p"])
            finally:
                if controller is not None:
                    controller.close()
                if window is not None:
                    window.close()
                if engine is not None:
                    engine.deleteLater()
                    _APP.sendPostedEvents(None, QEvent.DeferredDelete)
                    _APP.processEvents()

    def test_quick_window_cfilm_parameters_small_simulation_and_exports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service, design = self._synthetic_glass_fixture(root)
            design["name"] = "Quick cfilm fixture"
            cfilm_path = root / "synthetic.cfilm"
            service.save(str(cfilm_path), design)
            engine = window = controller = None
            try:
                engine, window, controller = self._create_quick_window(
                    root=root, service=service)
                self.assertIsNotNone(window.findChild(QObject, "windowChromeTitleDrag"))
                controller.loadDesignFromPath(str(cfilm_path))
                self.assertEqual(controller.designName, "Quick cfilm fixture")
                self.assertEqual(Path(controller.designPath), cfilm_path)

                parameters_button = window.findChild(QObject, "filmSimulationParametersButton")
                parameters_dialog = window.findChild(QObject, "filmSimulationParametersDialog")
                parameters_form = window.findChild(QObject, "filmSimulationParametersForm")
                parameters_cancel = window.findChild(QObject, "filmSimulationParametersCancelButton")
                parameters_apply = window.findChild(QObject, "filmSimulationParametersApplyButton")
                before = dict(controller.settings)

                self._click_quick_item(window, parameters_button)
                self.assertTrue(self._wait_until(lambda: bool(parameters_dialog.property("visible"))))
                draft = dict(before)
                draft["wavelength_max_nm"] = 420.0
                parameters_form.setProperty("draftSettings", draft)
                self._click_quick_item(window, parameters_cancel)
                self.assertTrue(self._wait_until(lambda: not parameters_dialog.property("visible")))
                self.assertEqual(dict(controller.settings), before)

                self._click_quick_item(window, parameters_button)
                self.assertTrue(self._wait_until(lambda: bool(parameters_dialog.property("visible"))))
                draft = dict(controller.settings)
                draft["wavelength_max_nm"] = 420.0
                parameters_form.setProperty("draftSettings", draft)
                self._click_quick_item(window, parameters_apply)
                self.assertTrue(self._wait_until(lambda: not parameters_dialog.property("visible")))
                self.assertEqual(controller.settings["wavelength_max_nm"], 420.0)
                self.assertEqual(controller.settings["samples"], 21)

                controller.startCalculation()
                self.assertTrue(self._wait_until(lambda: not controller.busy, timeout=30.0))
                result = controller._result
                self.assertIsNotNone(result)
                self.assertEqual(len(result["wavelength_nm"]), 21)

                controller._view_bounds = {
                    "x_min": 405.0, "x_max": 415.0, "y_min": 0.4, "y_max": 0.6,
                }
                self.assertTrue(controller.setPlotSettings({
                    "curves": ["R"],
                    "x": {"auto": True},
                    "y": {"unit": "percent", "auto": True},
                }))
                self.assertIsNone(controller._view_bounds)
                bounds = controller._plot_bounds()
                self.assertGreaterEqual(bounds["y_min"], 0.0)
                self.assertLess(bounds["y_max"], 0.2)

                csv_path = root / "quick-spectrum.csv"
                png_path = root / "quick-spectrum.png"
                controller.exportCsvToPath(str(csv_path))
                self.assertTrue(controller.setPngExportSettings({
                    "width_in": 4, "height_in": 3, "dpi": 72, "transparent": True,
                }))
                controller.exportPngToPath(str(png_path))
                with csv_path.open(encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), 21)
                self.assertEqual(png_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            finally:
                if controller is not None:
                    controller.close()
                if window is not None:
                    window.close()
                if engine is not None:
                    engine.deleteLater()
                    _APP.sendPostedEvents(None, QEvent.DeferredDelete)
                    _APP.processEvents()

    def test_301_point_rta_csv_png_match_existing_core_and_fresnel_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service, design = self._synthetic_glass_fixture(root)
            controller = FilmSimulationQuickController(root=root, service=service, design=design)
            try:
                settings = controller._current_settings()
                reference = service.simulate(design, settings)
                self.assertTrue(controller.setPlotSettings({
                    "curves": ["R", "T", "A"],
                    "polarizations": ["s", "p"],
                }))
                with warnings.catch_warnings(record=True) as render_warnings:
                    warnings.simplefilter("always")
                    controller.startCalculation()
                    self.assertTrue(self._wait_until(lambda: not controller.busy))
                    self.assertFalse(any("Glyph" in str(item.message)
                                         and "missing from font" in str(item.message)
                                         for item in render_warnings))
                actual = controller._result
                self.assertIsNotNone(actual)
                self.assertEqual(set(actual["polarization_curves"]), {"s", "p"})
                self.assertEqual(len(actual["wavelength_nm"]), 301)
                self.assertAlmostEqual(actual["wavelength_nm"][0], 400.0)
                self.assertAlmostEqual(actual["wavelength_nm"][-1], 700.0)
                for channel in ("R", "T", "A"):
                    self.assertEqual(len(actual[channel]), 301)
                    self.assertTrue(all(abs(a - b) <= 2e-14
                                        for a, b in zip(actual[channel], reference[channel])))
                self.assertTrue(all(abs(r - 0.04) <= 2e-14 for r in actual["R"]))
                self.assertTrue(all(abs(r + t + a - 1.0) <= 2e-14
                                    for r, t, a in zip(actual["R"], actual["T"], actual["A"])))
                for channel in ("R", "T", "A"):
                    self.assertTrue(all(abs(a - b) <= 2e-14 for a, b in zip(
                        actual["polarization_curves"]["s"][channel],
                        actual["polarization_curves"]["p"][channel])))

                self.assertTrue(controller.setCalculationBand(0, 420.0, 680.0))
                self.assertTrue(controller.addCalculationBand())
                self.assertTrue(controller.setCalculationBand(1, 500.0, 600.0))
                self.assertEqual([item["status"] for item in controller.calculationItems],
                                 ["ok", "ok"])
                self.assertAlmostEqual(controller.calculationItems[1]["mean_R"], 0.04)

                csv_path = root / "spectrum.csv"
                expected_csv_path = root / "reference.csv"
                service.export_csv(str(expected_csv_path), reference, overwrite=True)
                controller.exportCsvToPath(str(csv_path))
                with csv_path.open(encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), 301)
                self.assertEqual(csv_path.read_bytes(), expected_csv_path.read_bytes())
                for channel in ("R", "T", "A"):
                    self.assertAlmostEqual(float(rows[150][channel]), actual[channel][150], places=12)

                self.assertTrue(controller.setPngExportSettings({
                    "width_in": 4, "height_in": 3, "dpi": 72, "transparent": True,
                }))
                png_path = root / "spectrum.png"
                with warnings.catch_warnings(record=True) as export_warnings:
                    warnings.simplefilter("always")
                    controller.exportPngToPath(str(png_path))
                self.assertFalse(any("Glyph" in str(item.message)
                                     and "missing from font" in str(item.message)
                                     for item in export_warnings))
                png = png_path.read_bytes()
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", png[16:24]), (288, 216))
                self.assertEqual(png[25], 6, "transparent PNG should retain an alpha channel")

                preview = os.environ.get("FILM_SIMULATION_QUICK_PREVIEW")
                if preview:
                    self.assertTrue(controller.setPngExportSettings({
                        "width_in": 9, "height_in": 5, "dpi": 160, "transparent": False,
                    }))
                    with warnings.catch_warnings(record=True) as preview_warnings:
                        warnings.simplefilter("always")
                        controller.exportPngToPath(preview)
                    self.assertFalse(any("Glyph" in str(item.message)
                                         and "missing from font" in str(item.message)
                                         for item in preview_warnings))
                    self.assertTrue(Path(preview).is_file())
            finally:
                controller.close()

    def test_stale_worker_result_is_discarded_and_close_does_not_wait_for_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service, design = self._synthetic_glass_fixture(root)
            controller = FilmSimulationQuickController(root=root, service=service, design=design)
            real_simulate = service.simulate
            started = threading.Event()
            release = threading.Event()
            worker_finished = threading.Event()

            def delayed_simulate(current_design, settings):
                started.set()
                if not release.wait(10):
                    raise TimeoutError("test worker release timed out")
                try:
                    return real_simulate(current_design, settings)
                finally:
                    worker_finished.set()

            service.simulate = delayed_simulate
            try:
                controller.startCalculation()
                self.assertTrue(started.wait(5))
                controller.setParameter("angle_deg", 12.0)
                release.set()
                self.assertTrue(self._wait_until(lambda: not controller.busy))
                self.assertIsNone(controller._result)
                self.assertTrue(controller.stale)
                self.assertTrue(worker_finished.wait(5))

                controller.startCalculation()
                self.assertTrue(self._wait_until(lambda: not controller.busy))
                self.assertIsNotNone(controller._result)

                second_started = threading.Event()
                second_release = threading.Event()
                second_finished = threading.Event()

                def delayed_close_simulate(current_design, settings):
                    second_started.set()
                    if not second_release.wait(10):
                        raise TimeoutError("close test worker release timed out")
                    try:
                        return real_simulate(current_design, settings)
                    finally:
                        second_finished.set()

                service.simulate = delayed_close_simulate
                controller.startCalculation()
                self.assertTrue(second_started.wait(5))
                before_close = time.monotonic()
                controller.close()
                self.assertLess(time.monotonic() - before_close, 0.5)
                second_release.set()
                self.assertTrue(second_finished.wait(5))
                self.assertIsNone(controller._result)
                _APP.processEvents()
                self.assertTrue(controller._closed)
            finally:
                release.set()
                controller.close()
    def test_calculation_band_api_add_update_remove_and_show_nonuniform_statistics(self):
        controller = self.controller
        result = {
            "wavelength_nm": [400.0, 415.0, 500.0, 620.0],
            "R": [0.1, 0.115, 0.2, 0.32],
            "T": [0.9, 0.8, 0.6, 0.5],
        }
        self.assertEqual(controller.calculationItems[0]["status"], "no_result")
        controller._result = result
        controller._stale = False
        self.assertTrue(controller.setCalculationBand(0, 410.0, 600.0))
        self.assertTrue(controller.addCalculationBand())
        self.assertTrue(controller.setCalculationBand(1, 400.0, 500.0))
        items = controller.calculationItems
        self.assertEqual([item["status"] for item in items], ["ok", "ok"])
        self.assertAlmostEqual(items[0]["mean_R"], 0.205)
        self.assertAlmostEqual(items[0]["mean_T"], 0.6285087719298246)
        controller.removeCalculationBand(0)
        self.assertEqual(len(controller.calculationItems), 1)
        self.assertEqual(controller.calculationItems[0]["index"], 0)
        self.assertAlmostEqual(controller.calculationItems[0]["mean_R"], 0.15)

    def test_design_use_k_survives_simulation_parameter_apply(self):
        design = new_design("k policy fixture")
        design["use_k"] = False
        self.controller.setDesignJson(json.dumps(design, ensure_ascii=False))
        self.assertFalse(self.controller.useK)
        settings = self.controller.settings
        self.assertNotIn("use_k", settings)
        settings["angle_deg"] = 14.0
        self.assertTrue(self.controller.setSimulationSettings(settings))
        self.assertFalse(self.controller.useK)
        self.assertFalse(json.loads(self.controller.designJson)["use_k"])
        parameter_source = (
            Path(__file__).resolve().parents[1]
            / "coat_sim" / "ui" / "qml" / "FilmSimulationParameters.qml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("setUseK", parameter_source)
        self.assertNotIn("draftUseK", parameter_source)

    def test_band_statistics_interpolate_edges_and_integrate_nonuniform_grid(self):
        result = {
            "wavelength_nm": [400.0, 415.0, 500.0, 620.0],
            "R": [0.1, 0.115, 0.2, 0.32],
            "T": [0.9, 0.8, 0.6, 0.5],
        }
        stats = _calculate_band_statistics(result, 410.0, 600.0)
        self.assertEqual(stats["status"], "ok")
        self.assertAlmostEqual(stats["min_R"], 0.11)
        self.assertAlmostEqual(stats["max_R"], 0.30)
        self.assertAlmostEqual(stats["mean_R"], 0.205)
        self.assertAlmostEqual(stats["mean_T"], 0.6285087719298246)

    def test_band_statistics_report_no_result_invalid_range_outside_band_and_nan(self):
        self.assertEqual(_calculate_band_statistics(None, 400, 500)["status"], "no_result")
        result = {
            "wavelength_nm": [400.0, 500.0, 700.0],
            "R": [0.1, 0.2, 0.3],
            "T": [0.9, 0.8, 0.7],
        }
        self.assertEqual(_calculate_band_statistics(result, 500, 500)["status"], "invalid_range")
        outside = _calculate_band_statistics(result, 390, 450)
        self.assertEqual(outside["status"], "out_of_range")
        self.assertIn("不会外推", outside["message"])
        result["R"][1] = math.nan
        non_finite = _calculate_band_statistics(result, 450, 600)
        self.assertEqual(non_finite["status"], "non_finite")

    def test_polarization_overlays_keep_service_result_and_extend_auto_y_limits(self):
        class FakeService:
            def __init__(self):
                self.calls = []

            def simulate(self, design, settings):
                polarization = settings["polarization"]
                self.calls.append(polarization)
                level = {"unpolarized": 0.3, "s": 0.1, "p": 0.5}[polarization]
                return {
                    "settings": {"polarization": polarization},
                    "wavelength_nm": [400.0, 500.0],
                    "R": [level, level],
                    "T": [1 - level, 1 - level],
                    "A": [0.0, 0.0],
                }

        service = FakeService()
        result = _simulate_with_polarizations(
            service, {}, {"polarization": "unpolarized"}, ("s", "p"))
        self.assertEqual(service.calls, ["unpolarized", "s", "p"])
        self.assertEqual(result["R"], [0.3, 0.3])
        self.assertEqual(result["polarization_curves"]["s"]["R"], [0.1, 0.1])
        self.assertEqual(result["polarization_curves"]["p"]["R"], [0.5, 0.5])
        self.assertGreater(_automatic_y_limits(result, ["R"], ["s", "p"])[1], 0.5)

    def test_polarization_matching_base_channel_is_reused(self):
        class FakeService:
            def __init__(self):
                self.calls = []

            def simulate(self, design, settings):
                self.calls.append(settings["polarization"])
                return {
                    "settings": {"polarization": settings["polarization"]},
                    "wavelength_nm": [400.0, 500.0],
                    "R": [0.2, 0.3], "T": [0.8, 0.7], "A": [0.0, 0.0],
                }

        service = FakeService()
        result = _simulate_with_polarizations(service, {}, {"polarization": "s"}, ("s",))
        self.assertEqual(service.calls, ["s"])
        self.assertEqual(result["polarization_curves"]["s"]["R"], [0.2, 0.3])


if __name__ == "__main__":
    unittest.main()
