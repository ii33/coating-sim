"""Isolated Qt checks for the floating film plot controls and diagnostics sidebar."""
from __future__ import annotations

import copy
import csv
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
QA_ROOT = ROOT / "build" / "qa_ui_film_20261011_floating_01"
QA_TEMP = QA_ROOT / "temp"
QA_TEMP.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = str(QA_TEMP)
os.environ["TMP"] = str(QA_TEMP)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QT_SCALE_FACTOR", "1")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# Install the project's temporary-QSettings and no-network guards before
# importing any application UI module or constructing a QML engine.
import qt_test_guard

from PySide6.QtCore import QObject, QPoint, QPointF, QEvent, QSettings, Qt, Signal, QUrl
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtQml import QQmlApplicationEngine

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_design_session import SessionSnapshot
from coat_sim.core.materials import default_material_profile
from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
from coat_sim.ui.theme_service import ThemeService
from coat_sim.ui.window_geometry import prepare_window_geometry


QQuickStyle.setStyle("Fusion")
APP = QGuiApplication.instance() or QGuiApplication([])
for _font_path in (Path(r"C:\Windows\Fonts\segoeui.ttf"),
                   Path(r"C:\Windows\Fonts\msyh.ttc")):
    if _font_path.is_file():
        QFontDatabase.addApplicationFont(str(_font_path))


class _MemorySession:
    def __init__(self, design):
        self.design = copy.deepcopy(design)
        self.revision = 1
        self.design_revision = 1
        self.path = None
        self.dirty = False
        self.error = ""

    def snapshot(self):
        return SessionSnapshot(
            revision=self.revision,
            design=copy.deepcopy(self.design),
            design_revision=self.design_revision,
            path=self.path,
            dirty=self.dirty,
            message="",
            error=self.error,
            derived={},
            material_options={},
            expression_text="",
            expression_busy=False,
            expression_operation="",
            pending_confirmation=None,
            saved_designs=(),
            expression_templates=(),
        )

    def replace(self, design):
        # A source draft edit retains the simulation settings committed by the
        # paired page; replacing the whole document would discard those edits.
        configuration = copy.deepcopy(self.design.get("workflow_settings"))
        self.design = copy.deepcopy(design)
        if configuration is not None:
            self.design["workflow_settings"] = configuration
        self.revision += 1
        self.design_revision += 1
        self.dirty = True

    def set_workflow_configuration(self, scope, configuration):
        from coat_sim.core.film_design import validate_workflow_settings

        if scope not in ("simulation", "optimization"):
            raise ValueError("unknown workflow configuration scope")
        try:
            normalized = validate_workflow_settings({scope: configuration})[scope]
        except (ValueError, TypeError) as exc:
            self.error = str(exc)
            return False
        if self.design.get("workflow_settings", {}).get(scope) == normalized:
            return True
        self.design.setdefault("workflow_settings", {})[scope] = normalized
        self.revision += 1
        self.dirty = True
        self.error = ""
        return True


class _MemorySourceController(QObject):
    changed = Signal()
    optimizationApplied = Signal()

    def __init__(self, session):
        super().__init__()
        self.session = session


class FilmFloatingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network_factories = []
        cls.case_root = QA_ROOT / uuid4().hex
        cls.case_root.mkdir(parents=True)
        cls.temp_root = QA_TEMP / uuid4().hex
        cls.temp_root.mkdir(parents=True)
        cls.geometry_path = cls.temp_root / "window-geometry.ini"
        cls.geometry_settings = QSettings(
            str(cls.geometry_path), QSettings.Format.IniFormat)
        cls.service = FilmDesignService(cls.case_root)
        record = default_material_profile("Isolated floating UI glass", id="ui_glass")
        record.update(category="substrate", optical_constants=[{
            "id": "glass",
            "name": "constant n=1.5",
            "kind": "tabulated",
            "notes": "Analytical test fixture, not production data",
            "source": "Fresnel reference fixture",
            "wavelength_unit": "nm",
            "wavelength_min": 400.0,
            "wavelength_max": 402.0,
            "wavelengths": [400.0, 402.0],
            "n": [1.5, 1.5],
            "k": [0.0, 0.0],
        }])
        materials_root = cls.case_root / "data" / "materials"
        materials_root.mkdir(parents=True, exist_ok=True)
        (materials_root / "ui_glass.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8")
        cls.design = cls.service.new_design()
        cls.design["name"] = "Floating UI session"
        cls.design["substrate"].update(
            material_id="ui_glass", optical_set_id="glass")

    def _load_window(self, *, source_controller=None):
        self.network_factory = qt_test_guard.NoNetworkAccessManagerFactory()
        self.network_factories.append(self.network_factory)
        engine = QQmlApplicationEngine()
        engine.setNetworkAccessManagerFactory(self.network_factory)
        controller = FilmSimulationQuickController(
            root=self.case_root,
            service=self.service,
            design=self.design,
            source_controller=source_controller,
        )
        engine.rootContext().setContextProperty("FilmSimulation", controller)
        ThemeService.instance().attach_engine(engine, "Theme")
        qml_path = ROOT / "coat_sim" / "ui" / "qml" / "FilmSimulationQuick.qml"
        engine.load(QUrl.fromLocalFile(str(qml_path)))
        roots = engine.rootObjects()
        self.assertTrue(roots, "FilmSimulationQuick.qml failed to load")
        window = roots[0]
        prepare_window_geometry(
            window, "film_floating_ui", settings=self.geometry_settings)
        window.show()
        self._process_events()
        return engine, window, controller

    @staticmethod
    def _process_events():
        APP.processEvents()
        QTest.qWait(20)
        APP.processEvents()

    def _click(self, window, item):
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(
            window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            QPoint(round(point.x()), round(point.y())))
        self._process_events()

    def _assert_floating_controls_work(self, window, controller, sidebar):
        buttons = [
            window.findChild(QObject, name)
            for name in ("filmSpectrumZoomInButton", "filmSpectrumZoomOutButton",
                         "filmSpectrumResetButton")
        ]
        self.assertTrue(all(button is not None for button in buttons))
        self.assertTrue(sidebar.property("visible"))
        sidebar_left = sidebar.mapToScene(QPointF(0, 0)).x()
        for button in buttons:
            self.assertTrue(button.property("visible"))
            self.assertTrue(button.property("enabled"))
            button_right = button.mapToScene(QPointF(button.width(), button.height())).x()
            self.assertLessEqual(button_right, sidebar_left + 1,
                                 "plot buttons must remain outside the open sidebar")

        before = controller.plotView["x_max"] - controller.plotView["x_min"]
        self._click(window, buttons[0])
        after_zoom_in = controller.plotView["x_max"] - controller.plotView["x_min"]
        self.assertLess(after_zoom_in, before)
        self._click(window, buttons[1])
        after_zoom_out = controller.plotView["x_max"] - controller.plotView["x_min"]
        self.assertGreater(after_zoom_out, after_zoom_in)
        self._click(window, buttons[2])
        self.assertAlmostEqual(controller.plotView["x_min"],
                               controller.plotData["data_x_min"])
        self.assertAlmostEqual(controller.plotView["x_max"],
                               controller.plotData["data_x_max"])

    def _assert_legend_visible_without_hud_overlap(self, window, series_keys, controller):
        viewport = window.findChild(QObject, "filmSpectrumVectorViewport")
        hud = window.findChild(QObject, "filmSpectrumFloatingControls")
        repeater = window.findChild(QObject, "filmSpectrumLegendRepeater")
        self.assertIsNotNone(viewport)
        self.assertIsNotNone(hud)
        self.assertIsNotNone(repeater)
        print(f"[film-floating-qa] legend_count={repeater.property('count')} "
              f"series={controller.plotData['series']}")
        self.assertEqual(repeater.property("count"), len(series_keys))
        viewport_bounds = self._scene_rect(viewport)
        hud_bounds = self._scene_rect(hud)
        y_title = window.findChild(QObject, "filmSpectrumYAxisTitle")
        self.assertIsNotNone(y_title)
        title_left, title_top, title_right, title_bottom = self._scene_rect(y_title)
        hud_left, hud_top, hud_right, hud_bottom = hud_bounds
        title_overlaps_hud = (title_left < hud_right and title_right > hud_left
                              and title_top < hud_bottom and title_bottom > hud_top)
        self.assertFalse(title_overlaps_hud,
                         "rotated y-axis title is obscured by plot controls")
        y_title_text = y_title.property("text")
        self.assertIn("相对光谱", y_title_text)
        self.assertLessEqual(y_title.property("implicitWidth"),
                             y_title.property("width") + 1,
                             "the complete y-axis title must fit its rotated plot span")
        x_title = window.findChild(QObject, "filmSpectrumXAxisTitle")
        plot = window.findChild(QObject, "filmSpectrumPlot")
        self.assertIsNotNone(x_title)
        self.assertIsNotNone(plot)
        self.assertTrue(x_title.property("visible"))
        self.assertIn("nm", x_title.property("text").lower())
        self.assertLessEqual(x_title.property("implicitWidth"),
                             x_title.property("width") + 1,
                             "wavelength title must fit without truncation")
        x_left, x_top, x_right, x_bottom = self._scene_rect(x_title)
        visible_right = plot.mapToScene(QPointF(
            plot.property("floatingControlsRightEdge"), 0)).x()
        self.assertLessEqual(x_right, visible_right + 1,
                             "wavelength title extends beneath the diagnostics sidebar")
        self.assertFalse(x_left < hud_right and x_right > hud_left
                         and x_top < hud_bottom and x_bottom > hud_top,
                         "wavelength title is obscured by plot controls")
        sidebar = window.findChild(QObject, "filmSimulationDiagnosticsSidebar")
        if sidebar.property("visible"):
            side_left, side_top, side_right, side_bottom = self._scene_rect(sidebar)
            self.assertFalse(x_left < side_right and x_right > side_left
                             and x_top < side_bottom and x_bottom > side_top,
                             "wavelength title is obscured by the sidebar")

        def find_visual(item, object_name):
            self.assertIsInstance(item, QQuickItem)
            if item.objectName() == object_name:
                return item
            for child in item.childItems():
                found = find_visual(child, object_name)
                if found is not None:
                    return found
            return None

        for channel, polarization in series_keys:
            token = f"{channel}_{polarization}"
            label = find_visual(window.contentItem(),
                                f"filmSpectrumLegendLabel_{token}")
            self.assertIsNotNone(label, f"missing {token} legend label")
            self.assertTrue(label.property("visible"))
            self.assertTrue(label.property("text"))
            left, top, right, bottom = self._scene_rect(label)
            print(f"[film-floating-qa] legend_{token}_rect="
                  f"{(left, top, right, bottom)} hud={hud_bounds}")
            self.assertGreaterEqual(left, viewport_bounds[0] - 1)
            self.assertLessEqual(right, viewport_bounds[2] + 1,
                                 f"{token} legend label is outside the plot")
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(bottom, window.height())
            overlaps_hud = (left < hud_right and right > hud_left
                            and top < hud_bottom and bottom > hud_top)
            self.assertFalse(overlaps_hud,
                             f"{token} legend label is obscured by plot controls")

    @staticmethod
    def _scene_rect(item):
        points = [
            item.mapToScene(QPointF(0, 0)),
            item.mapToScene(QPointF(item.width(), 0)),
            item.mapToScene(QPointF(0, item.height())),
            item.mapToScene(QPointF(item.width(), item.height())),
        ]
        return (min(point.x() for point in points),
                min(point.y() for point in points),
                max(point.x() for point in points),
                max(point.y() for point in points))

    def _capture_layout_manifest(self, window, screenshot_path):
        def bounds(name):
            item = window.findChild(QObject, name)
            self.assertIsNotNone(item, f"missing layout item {name}")
            return self._scene_rect(item)

        image = window.grabWindow()
        self.assertFalse(image.isNull())
        screen = APP.primaryScreen()
        return {
            "screenshot": str(screenshot_path),
            "logical_window": [window.width(), window.height()],
            "image_pixels": [image.width(), image.height()],
            "logical_to_physical_ratio": image.width() / max(1, window.width()),
            "pixel_ratio_method": "grabWindow image width divided by logical window width",
            "reported_image_dpr": image.devicePixelRatio(),
            "screen_device_pixel_ratio": screen.devicePixelRatio() if screen else None,
            "plot_viewport_logical_rect": bounds("filmSpectrumVectorViewport"),
            "curve_layer_logical_rect": bounds("filmSpectrumCurveTransformLayer"),
            "legend_logical_rect": bounds("filmSpectrumVectorLegend"),
            "x_title_logical_bounds": bounds("filmSpectrumXAxisTitle"),
            "rotated_y_title_logical_bounds": bounds("filmSpectrumYAxisTitle"),
            "rotated_y_title_text_width": window.findChild(
                QObject, "filmSpectrumYAxisTitle").property("implicitWidth"),
            "rotated_y_title_available_span": window.findChild(
                QObject, "filmSpectrumYAxisTitle").property("width"),
            "rotated_y_title_rotation_degrees": window.findChild(
                QObject, "filmSpectrumYAxisTitle").rotation(),
        }

    def _assert_no_orange_curve_outside_plot(self, window, controller):
        chart = window.findChild(QObject, "filmSpectrumChartSurface")
        plot = window.findChild(QObject, "filmSpectrumPlot")
        viewport = window.findChild(QObject, "filmSpectrumVectorViewport")
        legend = window.findChild(QObject, "filmSpectrumVectorLegend")
        self.assertIsNotNone(chart)
        self.assertIsNotNone(plot)
        self.assertIsNotNone(viewport)
        self.assertIsNotNone(legend)
        self.assertTrue(plot.property("softwareRendererActive"),
                        "GraphicsInfo.Software was not detected by QML")
        t_series = next((item for item in controller.plotData["series"]
                         if item.get("channel") == "T"), None)
        self.assertIsNotNone(t_series, "test needs the actual T-series color")
        target_color = t_series["color"]
        self.assertRegex(target_color, r"^#[0-9a-fA-F]{6}$")
        target_rgb = tuple(int(target_color[index:index + 2], 16)
                           for index in (1, 3, 5))
        image = window.grabWindow()
        self.assertFalse(image.isNull())
        debug_screenshot = self.case_root / "curve-clip-debug.png"
        image.save(str(debug_screenshot))
        print(f"[film-floating-qa] clip_debug_screenshot={debug_screenshot}")
        scale = image.width() / max(1.0, window.width())
        self.assertAlmostEqual(image.height() / max(1.0, window.height()), scale, places=3)
        plot_left, plot_top, plot_right, plot_bottom = self._scene_rect(viewport)
        def find_visual(item, object_name):
            self.assertIsInstance(item, QQuickItem)
            if item.objectName() == object_name:
                return item
            for child in item.childItems():
                found = find_visual(child, object_name)
                if found is not None:
                    return found
            return None

        series_keys = [
            (item.get("channel"), item.get("polarization"))
            for item in controller.plotData["series"]
        ]
        swatch_rects = []
        for channel, polarization in series_keys:
            token = f"{channel}_{polarization}"
            swatch = find_visual(window.contentItem(),
                                 f"filmSpectrumLegendSwatch_{token}")
            self.assertIsNotNone(swatch, f"missing legend swatch {token}")
            swatch_rects.append(self._scene_rect(swatch))
        print(f"[film-floating-qa] clip_scan_scale={scale} "
              f"image_dpr={image.devicePixelRatio()} viewport="
              f"{(plot_left, plot_top, plot_right, plot_bottom)} "
              f"curve_layer={self._scene_rect(window.findChild(QObject, 'filmSpectrumCurveTransformLayer'))} "
              f"logical_to_physical={scale} image={image.width()}x{image.height()} "
              f"target={target_color}")
        leaked_pixels = []
        matching_inside_pixels = 0
        for x_fraction, y_fraction in ((0.2, 0.7), (0.5, 0.82), (0.8, 0.94)):
            sample_x = round((plot_left + (plot_right - plot_left) * x_fraction) * scale)
            sample_y = round((plot_top + (plot_bottom - plot_top) * y_fraction) * scale)
            sample = image.pixelColor(sample_x, sample_y)
            self.assertGreater(sample.red() + sample.green() + sample.blue(), 30,
                               "plot viewport contains an opaque black layer artifact")
        for py in range(image.height()):
            logical_y = (py + 0.5) / scale
            for px in range(image.width()):
                logical_x = (px + 0.5) / scale
                in_swatch = any(left <= logical_x <= right
                                and top <= logical_y <= bottom
                                for left, top, right, bottom in swatch_rects)
                in_plot = (plot_left <= logical_x <= plot_right
                           and plot_top <= logical_y <= plot_bottom)
                if in_swatch:
                    continue
                color = image.pixelColor(px, py)
                matches_t = all(abs(actual - expected) <= 20
                                for actual, expected in zip(
                                    (color.red(), color.green(), color.blue()),
                                    target_rgb))
                if not matches_t:
                    continue
                if in_plot:
                    matching_inside_pixels += 1
                else:
                    leaked_pixels.append((logical_x, logical_y,
                                          color.red(), color.green(), color.blue()))
                    if len(leaked_pixels) >= 30:
                        break
            if len(leaked_pixels) >= 30:
                break
        self.assertGreaterEqual(matching_inside_pixels, 20,
                                "T-series curve was not rendered inside its viewport")
        print(f"[film-floating-qa] curve_rgb_inside={matching_inside_pixels} "
              f"outside={len(leaked_pixels)} color={target_color}")
        self.assertEqual(leaked_pixels, [],
                         "an out-of-range T curve pixel escaped the plot viewport")

    def _wait_for_calculation(self, controller, timeout=12.0):
        deadline = time.monotonic() + timeout
        while controller.busy and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.005)
        APP.processEvents()
        self.assertFalse(controller.busy, "small isolated simulation did not finish")
        self.assertFalse(controller.validationError, controller.validationError)

    def _set_small_spectrum(self, controller):
        settings = controller.settings
        settings.update({
            "wavelength_min_nm": 400.0,
            "wavelength_max_nm": 402.0,
            "samples": 3,
        })
        settings.pop("step_nm", None)
        self.assertTrue(controller.setSimulationSettings(settings))

    def test_floating_plot_diagnostics_sidebar_and_session_lifecycle(self):
        theme = ThemeService.instance()
        theme.configure(mode="light")
        source_session = _MemorySession(self.design)
        source_controller = _MemorySourceController(source_session)
        engine, window, controller = self._load_window(
            source_controller=source_controller)
        reopened = None
        reopened_engine = None
        reopened_controller = None
        try:
            self.assertEqual(controller.plotSettings["curves"], ["R"])
            self.assertEqual(window.property("contentTopMargin"), window.property("titleBarHeight"))
            self.assertFalse(window.property("diagnosticsExpanded"))
            self.assertEqual(window.property("statusText"), controller.status)

            self._set_small_spectrum(controller)
            controller.startCalculation()
            self._wait_for_calculation(controller)
            self.assertEqual(controller.plotSettings["curves"], ["R"])
            self.assertEqual([item["channel"] for item in controller.plotData["series"]], ["R"])
            for channel in ("R", "T", "A"):
                self.assertEqual(len(controller._result[channel]), 3)
            initial_image = window.grabWindow()
            default_screenshot = self.case_root / "default-reflection.png"
            self.assertFalse(initial_image.isNull())
            self.assertTrue(initial_image.save(str(default_screenshot)))
            print(f"[film-floating-qa] default_screenshot={default_screenshot}")

            self.assertTrue(controller.setPlotSettings({"curves": ["T", "A"]}))
            self.assertEqual(controller.plotSettings["curves"], ["T", "A"])
            self.assertEqual([item["channel"] for item in controller.plotData["series"]], ["T", "A"])

            changed_design = copy.deepcopy(self.design)
            changed_design["name"] = "Updated memory session design"
            source_session.replace(changed_design)
            source_controller.changed.emit()
            self._process_events()
            self.assertEqual(controller.designName, "Updated memory session design")
            self.assertEqual(controller.plotSettings["curves"], ["T", "A"])
            self.assertFalse(controller.hasResult)

            controller.startCalculation()
            self._wait_for_calculation(controller)
            self.assertEqual(controller.plotSettings["curves"], ["T", "A"])
            self.assertEqual([item["channel"] for item in controller.plotData["series"]], ["T", "A"])
            self.assertTrue(all(len(controller._result[key]) == 3 for key in ("R", "T", "A")))

            csv_path = self.case_root / "complete-spectrum.csv"
            controller.exportCsvToPath(str(csv_path))
            with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 3)
            self.assertTrue({"R", "T", "A"}.issubset(rows[0]))
            for row, index in zip(rows, range(3)):
                for key in ("R", "T", "A"):
                    self.assertAlmostEqual(float(row[key]), controller._result[key][index])

            zoom_in = window.findChild(QObject, "filmSpectrumZoomInButton")
            zoom_reset = window.findChild(QObject, "filmSpectrumResetButton")
            floating = window.findChild(QObject, "filmSpectrumFloatingControls")
            self.assertIsNotNone(zoom_in)
            self.assertIsNotNone(zoom_reset)
            self.assertIsNotNone(floating)
            self.assertTrue(zoom_in.property("enabled"))
            original_view = dict(controller.plotView)
            self._click(window, zoom_in)
            self.assertLess(controller.plotView["x_max"] - controller.plotView["x_min"],
                            original_view["x_max"] - original_view["x_min"])
            self._click(window, zoom_reset)
            self.assertAlmostEqual(controller.plotView["x_min"], controller.plotData["data_x_min"])
            self.assertAlmostEqual(controller.plotView["x_max"], controller.plotData["data_x_max"])

            # At full data extent, dragging is intentionally locked. Zooming
            # first creates room to pan without moving the spectrum off-axis.
            controller.panPlot(0.2, 0.2)
            self.assertEqual(controller.plotView, original_view)
            self._click(window, zoom_in)

            viewport = window.findChild(QObject, "filmSpectrumVectorViewport")
            self.assertIsNotNone(viewport)
            hover_point = viewport.mapToScene(QPointF(viewport.width() * 0.5,
                                                      viewport.height() * 0.5))
            QTest.mouseMove(window, QPoint(round(hover_point.x()), round(hover_point.y())))
            self._process_events()
            self.assertTrue(controller.cursorReadout)
            drag_start = viewport.mapToScene(QPointF(viewport.width() * 0.55,
                                                      viewport.height() * 0.55))
            drag_end = viewport.mapToScene(QPointF(viewport.width() * 0.68,
                                                    viewport.height() * 0.65))
            before_pan = dict(controller.plotView)
            QTest.mousePress(window, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier,
                             QPoint(round(drag_start.x()), round(drag_start.y())))
            QTest.mouseMove(window, QPoint(round(drag_end.x()), round(drag_end.y())), 60)
            QTest.mouseRelease(window, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier,
                               QPoint(round(drag_end.x()), round(drag_end.y())))
            self._process_events()
            self.assertNotEqual(controller.plotView, before_pan)
            controller.resetPlotView()

            window.resize(720, 540)
            self._process_events()
            self.assertEqual(window.width(), 720)
            action_bar = window.findChild(QObject, "filmSimulationActionBar")
            toolbar = window.findChild(QObject, "filmSimulationToolbar")
            result_host = window.findChild(QObject, "filmSimulationResultHost")
            statistics_button = window.findChild(QObject, "filmSimulationStatisticsToggle")
            diagnostics_button = window.findChild(QObject, "filmSimulationDiagnosticsButton")
            sidebar_toggle = window.findChild(QObject, "filmSimulationDiagnosticsToggle")
            diagnostics_sidebar = window.findChild(QObject, "filmSimulationDiagnosticsSidebar")
            self.assertIsNotNone(action_bar)
            self.assertIsNotNone(toolbar)
            self.assertIsNotNone(result_host)
            self.assertIsNotNone(statistics_button)
            self.assertIsNotNone(diagnostics_button)
            self.assertIsNotNone(sidebar_toggle)
            self.assertIsNotNone(diagnostics_sidebar)
            print(f"[film-floating-qa] toolbar_height={toolbar.height():.1f} "
                  f"action_row_height={action_bar.height():.1f} "
                  f"action_row_implicit={action_bar.property('implicitHeight'):.1f} "
                  f"result_y={result_host.y():.1f}")
            self.assertLess(toolbar.height(), 100)
            self.assertLess(action_bar.height(), 100)
            chart_surface = window.findChild(QObject, "filmSpectrumChartSurface")
            viewport = window.findChild(QObject, "filmSpectrumVectorViewport")
            y_title = window.findChild(QObject, "filmSpectrumYAxisTitle")
            self.assertIsNotNone(chart_surface)
            self.assertIsNotNone(viewport)
            self.assertIsNotNone(y_title)
            toolbar_bottom = toolbar.mapToScene(QPointF(0, toolbar.height())).y()
            chart_top = chart_surface.mapToScene(QPointF(0, 0)).y()
            self.assertGreaterEqual(chart_top, toolbar_bottom)
            _, y_title_top, _, y_title_bottom = self._scene_rect(y_title)
            chart_bounds = self._scene_rect(chart_surface)
            self.assertGreaterEqual(y_title_top, chart_bounds[1])
            self.assertLessEqual(y_title_bottom, chart_bounds[3])
            self.assertLessEqual(floating.x() + floating.width(),
                                 window.findChild(QObject, "filmSpectrumChartSurface").width() + 1)

            for _ in range(3):
                self._click(window, diagnostics_button)
                self.assertTrue(diagnostics_sidebar.property("visible"))
                self.assertGreater(diagnostics_sidebar.width(), 0)
                self._click(window, sidebar_toggle)
                self.assertFalse(diagnostics_sidebar.property("visible"))
            self._click(window, statistics_button)
            self.assertTrue(window.property("statisticsExpanded"))
            initial_statistics = window.property("statisticsExpanded")
            for expected in (not initial_statistics, initial_statistics, not initial_statistics):
                QTest.keyClick(window, Qt.Key.Key_F4)
                self._process_events()
                self.assertEqual(window.property("statisticsExpanded"), expected)

            diagnostics_sidebar_button = diagnostics_button
            self._click(window, diagnostics_sidebar_button)
            summary = window.findChild(QObject, "filmSimulationResultSummary")
            self.assertIsNotNone(summary)
            self.assertTrue(diagnostics_sidebar.property("visible"))
            self.assertEqual(summary.property("text"), controller.resultSummary)

            window.resize(1280, 780)
            self._process_events()
            layout_manifest = []
            self._assert_floating_controls_work(window, controller, diagnostics_sidebar)
            self._assert_legend_visible_without_hud_overlap(
                window, (("T", "unpolarized"), ("A", "unpolarized")), controller)
            plot_item = window.findChild(QObject, "filmSpectrumPlot")
            chart_item = window.findChild(QObject, "filmSpectrumChartSurface")
            viewport_item = window.findChild(QObject, "filmSpectrumVectorViewport")
            layer_item = window.findChild(QObject, "filmSpectrumCurveTransformLayer")
            print(f"[film-floating-qa] plot_rect={self._scene_rect(plot_item)} "
                  f"chart_rect={self._scene_rect(chart_item)} "
                  f"viewport_rect={self._scene_rect(viewport_item)} "
                  f"layer_rect={self._scene_rect(layer_item)} "
                  f"curve_transform=({layer_item.x():.1f},{layer_item.y():.1f},"
                  f"{layer_item.scale():.3f}) view={controller.plotView}")
            wide_diagnostics_screenshot = self.case_root / "diagnostics-ta-wide.png"
            image = window.grabWindow()
            self.assertFalse(image.isNull())
            self.assertTrue(image.save(str(wide_diagnostics_screenshot)))
            layout_manifest.append(self._capture_layout_manifest(
                window, wide_diagnostics_screenshot))
            print(f"[film-floating-qa] diagnostics_ta_wide_screenshot={wide_diagnostics_screenshot}")

            window.resize(720, 540)
            self._process_events()
            self._assert_floating_controls_work(window, controller, diagnostics_sidebar)
            self._assert_legend_visible_without_hud_overlap(
                window, (("T", "unpolarized"), ("A", "unpolarized")), controller)
            narrow_ta_screenshot = self.case_root / "diagnostics-ta-narrow.png"
            image = window.grabWindow()
            self.assertFalse(image.isNull())
            self.assertTrue(image.save(str(narrow_ta_screenshot)))
            layout_manifest.append(self._capture_layout_manifest(
                window, narrow_ta_screenshot))
            print(f"[film-floating-qa] diagnostics_ta_narrow_screenshot={narrow_ta_screenshot}")

            polarized_settings = copy.deepcopy(controller.settings)
            polarized_settings["polarization"] = "s"
            self.assertTrue(controller.setSimulationSettings(polarized_settings))
            self.assertTrue(controller.setPlotSettings({
                "curves": ["R", "T", "A"],
                "polarizations": ["s", "p"],
            }))
            controller.startCalculation()
            self._wait_for_calculation(controller)
            max_legend_keys = tuple(
                (channel, polarization)
                for channel in ("R", "T", "A")
                for polarization in ("s", "p"))
            actual_legend_keys = tuple(
                (item["channel"], item["polarization"])
                for item in controller.plotData["series"])
            self.assertEqual(actual_legend_keys, max_legend_keys)
            self.assertEqual(controller.plotSettings["curves"], ["R", "T", "A"])
            self.assertEqual(controller.plotSettings["polarizations"], ["s", "p"])
            window.resize(1280, 780)
            self._process_events()
            self._assert_floating_controls_work(window, controller, diagnostics_sidebar)
            self._assert_legend_visible_without_hud_overlap(
                window, max_legend_keys, controller)
            max_legend_wide = self.case_root / "diagnostics-six-series-wide.png"
            image = window.grabWindow()
            self.assertFalse(image.isNull())
            self.assertTrue(image.save(str(max_legend_wide)))
            layout_manifest.append(self._capture_layout_manifest(
                window, max_legend_wide))
            print(f"[film-floating-qa] six_series_wide_screenshot={max_legend_wide}")
            window.resize(720, 540)
            self._process_events()
            self._assert_floating_controls_work(window, controller, diagnostics_sidebar)
            self._assert_legend_visible_without_hud_overlap(
                window, max_legend_keys, controller)
            max_legend_narrow = self.case_root / "diagnostics-six-series-narrow.png"
            image = window.grabWindow()
            self.assertFalse(image.isNull())
            self.assertTrue(image.save(str(max_legend_narrow)))
            layout_manifest.append(self._capture_layout_manifest(
                window, max_legend_narrow))
            manifest_path = self.case_root / "screenshot-manifest.json"
            manifest_path.write_text(json.dumps(layout_manifest, indent=2), encoding="utf-8")
            print(f"[film-floating-qa] six_series_narrow_screenshot={max_legend_narrow}")
            print(f"[film-floating-qa] screenshot_manifest={manifest_path}")

            long_status = "长状态提示用于可访问性和省略显示适配。" * 16
            controller._status = long_status
            controller._error = ""
            controller.stateChanged.emit()
            self._process_events()
            status_control = window.findChild(QObject, "windowChromeStatus")
            self.assertIsNotNone(status_control)
            self.assertTrue(status_control.property("visible"))
            self.assertEqual(window.property("statusDetail"), long_status)
            self.assertEqual(window.property("statusSeverity"), "info")
            controller._error = "Synthetic validation error"
            controller.stateChanged.emit()
            self._process_events()
            self.assertEqual(window.property("statusSeverity"), "error")
            self.assertEqual(window.property("statusText"), "Synthetic validation error")
            controller._error = ""

            for mode in ("dark", "light"):
                theme.configure(mode=mode)
                self._process_events()
                self.assertEqual(window.property("themePaletteMode"), mode)

            screenshot = self.case_root / "floating-ui-review.png"
            image = window.grabWindow()
            self.assertFalse(image.isNull(), "could not capture the visible Qt window")
            self.assertTrue(image.save(str(screenshot)), "could not save QA screenshot")
            print(f"[film-floating-qa] screenshot={screenshot}")

            saved_result = copy.deepcopy(controller._result)
            saved_plot_settings = controller.plotSettings
            controller._result["T"] = [-0.35, 0.5, 1.35]
            self.assertTrue(controller.setPlotSettings({
                "curves": ["T"],
                "y": {"auto": False, "min": 0.0, "max": 1.0},
            }))
            self.assertEqual(controller.plotData["series"][0]["points"],
                             [[400.0, -0.35], [401.0, 0.5], [402.0, 1.35]])
            controller.resetPlotView()
            self._process_events()
            self._assert_no_orange_curve_outside_plot(window, controller)
            clipping_screenshot = self.case_root / "out-of-range-curve-clipping.png"
            clipping_image = window.grabWindow()
            self.assertTrue(clipping_image.save(str(clipping_screenshot)))
            print(f"[film-floating-qa] clipping_screenshot={clipping_screenshot}")
            controller._result = saved_result
            self.assertTrue(controller.setPlotSettings(saved_plot_settings))
            self._process_events()

            self._click(window, diagnostics_sidebar_button)
            window.resize(740, 550)
            self._process_events()
            saved_size = (window.width(), window.height())
            window.close()
            self._process_events()
            self.geometry_settings.sync()
            key = "ui/window_geometry/v1/film_floating_ui"
            self.assertTrue(self.geometry_settings.contains(key))

            reopened_engine, reopened, reopened_controller = self._load_window()
            self._process_events()
            self.assertEqual(reopened.property("_coat_sim_geometry_source"), "restored")
            available = APP.primaryScreen().availableGeometry()
            self.assertEqual(
                (reopened.width(), reopened.height()),
                (min(saved_size[0], available.width()),
                 min(saved_size[1], available.height())),
            )
            self.assertFalse(reopened.property("diagnosticsExpanded"))

            scale = os.environ.get("QT_SCALE_FACTOR", "1")
            screen = APP.primaryScreen()
            print(f"[film-floating-qa] scale={scale} screen_dpr={screen.devicePixelRatio()} "
                  f"logical_window={reopened.width()}x{reopened.height()} theme=light/dark")
            self.assertEqual(self.network_factory.blocked_urls, [])
            blocked_urls = [url for factory in self.network_factories
                            for url in factory.blocked_urls]
            self.assertEqual(blocked_urls, [])
            print(f"[film-floating-qa] guarded_engine_count={len(self.network_factories)} "
                  f"blocked_urls={blocked_urls}")
        finally:
            if reopened is not None:
                reopened.close()
            if reopened_controller is not None:
                reopened_controller.close()
            if reopened_engine is not None:
                reopened_engine.deleteLater()
            if window is not None:
                window.close()
            controller.close()
            engine.deleteLater()
            APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            APP.processEvents()
            theme.configure(mode="light")


if __name__ == "__main__":
    unittest.main(verbosity=2)
