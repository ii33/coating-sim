"""Offscreen visual-interaction checks for the film spectrum plot overlays."""

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Keep child-process probes isolated from the user's Qt profile as well.
from tests import qt_test_guard  # noqa: F401
from tests.qt_test_guard import qml_item_window

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtQuick import QQuickItem
from PySide6.QtQml import QQmlComponent
from PySide6.QtTest import QSignalSpy, QTest

from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from coat_sim.ui.film_simulation_quick_controller import create_film_simulation_quick_window
from coat_sim.ui.theme_service import ThemeService


ROOT = Path(__file__).parents[1]
QQuickStyle.setStyle("Fusion")
_APP = QGuiApplication.instance() or QGuiApplication([])


class FilmSimulationQuickUiRefinementTests(unittest.TestCase):
    @staticmethod
    def wait_until(predicate, timeout=15.0):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.005)
        QCoreApplication.processEvents()
        return predicate()

    @staticmethod
    def synthetic_glass_fixture(root):
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
        design["name"] = "UI overlay fixture"
        design["substrate"].update(material_id="fixture_glass", optical_set_id="glass")
        return service, design

    @staticmethod
    def rect_in(item, parent):
        point = item.mapToItem(parent, QPointF(0, 0))
        return (float(point.x()), float(point.y()),
                float(item.property("width")), float(item.property("height")))

    @staticmethod
    def visual_items(root):
        pending = list(root.childItems())
        while pending:
            item = pending.pop()
            yield item
            pending.extend(item.childItems())

    @staticmethod
    def click(window, item):
        window = qml_item_window(item)
        assert window is not None
        scene = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(round(scene.x()), round(scene.y())))
        QCoreApplication.processEvents()

    def test_curve_pixels_follow_axes_at_fractional_and_integer_dpi(self):
        for scale in ("1", "1.5", "2"):
            with self.subTest(scale=scale):
                environment = dict(os.environ, QT_SCALE_FACTOR=scale,
                                   QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
                result = subprocess.run(
                    [sys.executable, "-m", "tests.test_film_simulation_quick_ui_refinements", "--pixel-probe"],
                    cwd=ROOT, env=environment, text=True, capture_output=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                manifest = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(manifest["phases"], ["flat_25_percent", "zoomed", "panned"])
                self.assertAlmostEqual(manifest["pixel_ratio"], float(scale), places=3)
                self.assertEqual(manifest["outside_curve_pixels"], 0)

    def test_plot_theme_input_is_required_and_remains_reactive(self):
        theme = ThemeService.instance()
        previous_mode = theme.current_preference_mode
        with tempfile.TemporaryDirectory() as folder:
            root_path = Path(folder)
            service, design = self.synthetic_glass_fixture(root_path)
            engine, window, controller = create_film_simulation_quick_window(
                root=root_path, service=service, design=design,
                geometry_settings=QSettings(str(root_path / "geometry.ini"),
                                            QSettings.Format.IniFormat))
            try:
                component = QQmlComponent(engine, QUrl.fromLocalFile(str(
                    ROOT / "coat_sim" / "ui" / "qml" / "FilmSpectrumPlot.qml")))
                self.assertTrue(component.isReady(), component.errorString())
                missing_tokens = component.createWithInitialProperties({"controller": controller})
                self.assertIsNone(missing_tokens)
                self.assertIn("Required property themeColors", component.errorString())
                plot = window.findChild(QQuickItem, "filmSpectrumPlot")
                footer = window.findChild(QQuickItem, "filmSpectrumFloatingResultSummary")
                summary = window.findChild(QQuickItem, "filmSpectrumFloatingResultSummaryText")
                # set_theme reports a change, not success. Earlier Qt tests
                # may leave light selected; start from a distinct baseline so
                # every asserted switch must really replace the engine tokens.
                theme.set_theme("high-contrast-dark")
                for mode in ("light", "dark", "light"):
                    revision = theme.current_revision
                    self.assertTrue(theme.set_theme(mode))
                    self.assertGreater(theme.current_revision, revision)
                    self.assertEqual(theme.current_preference_mode, mode)
                    QCoreApplication.processEvents()
                    expected = theme.current_snapshot
                    self.assertEqual(plot.property("color").name().lower(),
                                     str(expected["colorSurface"]).lower())
                    self.assertEqual(footer.property("color").name().lower(),
                                     str(expected["colorSurfaceRaised"]).lower())
                    self.assertEqual(summary.property("color").name().lower(),
                                     str(expected["colorTextMuted"]).lower())
            finally:
                theme.set_theme(previous_mode)
                controller.close()
                window.close()
                engine.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                QCoreApplication.processEvents()

    def test_action_bar_overflows_without_losing_buttons_or_result_footer(self):
        with tempfile.TemporaryDirectory() as folder:
            root_path = Path(folder)
            service, design = self.synthetic_glass_fixture(root_path)
            engine, window, controller = create_film_simulation_quick_window(
                root=root_path, service=service, design=design,
                geometry_settings=QSettings(str(root_path / "geometry.ini"), QSettings.Format.IniFormat))
            try:
                # Small available work areas may legitimately lower the normal
                # window minimum. Exercise that route without enlarging it.
                window.setMinimumWidth(280)
                window.setMinimumHeight(280)
                window.setProperty("statisticsExpanded", False)
                window.setProperty("diagnosticsExpanded", False)
                controller._render_plot = lambda: None
                controller._stale = False
                controller._result = {"wavelength_nm": [400.0, 700.0], "R": [0.25, 0.25]}
                controller._refresh_plot_data()
                controller.plotDataChanged.emit()
                controller.plotViewChanged.emit()
                controller.stateChanged.emit()
                window.show()
                names = ("Load", "Save", "Parameters", "Export", "Optimize", "Calculate")
                for width, height in ((533, 533), (400, 400), (280, 400), (720, 760), (1100, 760)):
                    window.setWidth(width)
                    window.setHeight(height)
                    QCoreApplication.processEvents()
                    QTest.qWait(20)
                    bar = window.findChild(QQuickItem, "filmSimulationActionBar")
                    buttons = [window.findChild(QQuickItem, "filmSimulation" + name + "Button")
                               for name in names]
                    self.assertTrue(all(buttons))
                    self.assertIsNone(window.findChild(QObject, "filmSimulationDesignSummary"))
                    for button in buttons:
                        rect = self.rect_in(button, bar)
                        self.assertTrue(button.isVisible())
                        self.assertGreater(rect[2], 0)
                        self.assertGreater(rect[3], 0)
                        if rect[0] < 0:
                            more = window.findChild(QQuickItem, "operationBarOverflowButton")
                            self.assertTrue(more.isVisible())
                            self.click(window, more)
                            entry = window.findChild(QQuickItem, "operationBarOverflow_" + button.objectName())
                            self.assertIsNotNone(entry)
                            self.assertTrue(entry.isVisible())
                            self.assertEqual(entry.isEnabled(), button.isEnabled())
                            self.assertEqual(entry.property("text"), button.property("text"))
                            QTest.keyClick(window, Qt.Key.Key_Escape)
                            QCoreApplication.processEvents()
                            continue
                        self.assertGreaterEqual(rect[1], 0)
                        self.assertLessEqual(rect[0] + rect[2], bar.width() + 1)
                        self.assertLessEqual(rect[1] + rect[3], bar.height() + 1)
                    # Overflow retains one row at every supported width.
                    inline = [button for button in buttons if button.x() >= 0]
                    self.assertEqual(len({button.y() for button in inline}), 1)
                    if width == 280:
                        calculate = buttons[-1]
                        self.assertLess(calculate.x(), 0)
                        clicked = QSignalSpy(calculate.clicked)
                        self.click(window, window.findChild(QQuickItem, "operationBarOverflowButton"))
                        entry = window.findChild(QQuickItem, "operationBarOverflow_filmSimulationCalculateButton")
                        self.assertTrue(calculate.isEnabled())
                        self.assertTrue(entry.isEnabled())
                        self.assertTrue(entry.isVisible())
                        QTest.qWait(40)
                        self.click(window, entry)
                        self.assertEqual(clicked.count(), 1)
                        self.assertTrue(self.wait_until(lambda: not controller.busy))
                    host = window.findChild(QQuickItem, "filmSimulationResultHost")
                    footer = window.findChild(QQuickItem, "filmSpectrumFloatingResultSummary")
                    viewport = window.findChild(QQuickItem, "filmSpectrumVectorViewport")
                    host_origin = host.mapToScene(QPointF(0, 0))
                    self.assertLessEqual(host_origin.y() + host.height(), window.height() + 1)
                    self.assertGreater(viewport.height(), 0)
                    self.assertTrue(footer.isVisible())
                    self.assertGreater(footer.height(), 0)
                    self.assertLessEqual(self.rect_in(footer, host)[1] + footer.height(), host.height() + 1)
            finally:
                controller.close()
                window.close()
                engine.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                QCoreApplication.processEvents()

    def test_plot_controls_legend_bottom_summary_and_cancel_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            root_path = Path(folder)
            service, design = self.synthetic_glass_fixture(root_path)
            cfilm_path = root_path / "ui-overlay-fixture.cfilm"
            service.save(str(cfilm_path), design)
            geometry = QSettings(str(root_path / "simulation-geometry.ini"),
                                 QSettings.Format.IniFormat)
            engine = window = controller = None
            try:
                engine, window, controller = create_film_simulation_quick_window(
                    root=root_path, service=service,
                    geometry_settings=geometry)
                window.setWidth(1100)
                window.setHeight(760)
                window.show()
                controller.loadDesignFromPath(str(cfilm_path))
                controller.setParameter("wavelength_max_nm", 420.0)
                controller.startCalculation()
                self.assertTrue(self.wait_until(lambda: not controller.busy, timeout=30.0))
                self.assertTrue(controller.hasResult)

                plot = window.findChild(QQuickItem, "filmSpectrumPlot")
                viewport = window.findChild(QQuickItem, "filmSpectrumVectorViewport")
                controls = window.findChild(QQuickItem, "filmSpectrumFloatingControls")
                legend = window.findChild(QQuickItem, "filmSpectrumVectorLegend")
                legend_surface = window.findChild(QQuickItem, "filmSpectrumLegendSurface")
                summary = window.findChild(QQuickItem, "filmSpectrumFloatingResultSummary")
                self.assertTrue(all((plot, viewport, controls, legend, legend_surface, summary)))
                self.assertTrue(legend.property("visible"))
                self.assertTrue(summary.property("visible"))
                self.assertEqual(float(controls.property("z")), 5.0)
                self.assertGreater(float(legend.property("z")), 0.0)
                self.assertGreater(float(summary.property("z")), 0.0)

                viewport_rect = self.rect_in(viewport, plot)
                controls_rect = self.rect_in(controls, plot)
                legend_rect = self.rect_in(legend, plot)
                summary_rect = self.rect_in(summary, plot)
                # Actions and a content-sized legend float over the axes. The
                # result note uses the footer and never covers the curves.
                self.assertGreaterEqual(controls_rect[1], viewport_rect[1] - 1)
                self.assertLess(controls_rect[1], viewport_rect[1] + viewport_rect[3])
                self.assertGreaterEqual(legend_rect[1], viewport_rect[1] - 1)
                self.assertLess(legend_rect[0], viewport_rect[0] + viewport_rect[2])
                self.assertGreaterEqual(summary_rect[1], viewport_rect[1] + viewport_rect[3])
                summary_bottom = summary_rect[1] + summary_rect[3]
                viewport_bottom = viewport_rect[1] + viewport_rect[3]
                self.assertGreater(summary_bottom, viewport_bottom)
                self.assertLessEqual(summary_bottom, plot.height() + 1)
                self.assertGreater(controls_rect[0] + controls_rect[2],
                                   viewport_rect[0])
                self.assertLess(legend_rect[2], viewport_rect[2] / 2,
                                "A single R legend must not stretch across the plot")

                zoom_in = window.findChild(QObject, "filmSpectrumZoomInButton")
                zoom_out = window.findChild(QObject, "filmSpectrumZoomOutButton")
                reset = window.findChild(QObject, "filmSpectrumResetButton")
                self.assertTrue(all((zoom_in, zoom_out, reset)))
                self.assertEqual(zoom_in.property("text"), "放大")
                self.assertEqual(zoom_out.property("text"), "缩小")
                self.assertEqual(reset.property("text"), "复位")
                zoom_in_content = window.findChild(QObject, "filmSpectrumZoomInContent")
                zoom_out_content = window.findChild(QObject, "filmSpectrumZoomOutContent")
                reset_content = window.findChild(QObject, "filmSpectrumResetContent")
                summary_text = window.findChild(QObject, "filmSpectrumFloatingResultSummaryText")
                self.assertTrue(all((zoom_in_content, zoom_out_content, reset_content, summary_text)))
                self.assertEqual(zoom_in_content.property("mark"), "+")
                self.assertEqual(zoom_out_content.property("mark"), "−")
                self.assertEqual(reset_content.property("mark"), "↺")
                self.assertIn("滚轮缩放", summary_text.property("text"))
                self.assertNotIn("数值诊断", summary_text.property("text"))
                for name in ("ZoomIn", "ZoomOut", "Reset"):
                    button = window.findChild(QQuickItem, "filmSpectrum" + name + "Button")
                    glyph = window.findChild(QQuickItem, "filmSpectrum" + name + "ContentGlyph")
                    center = glyph.mapToItem(button, QPointF(glyph.width() / 2, glyph.height() / 2))
                    self.assertAlmostEqual(center.x(), button.width() / 2, delta=0.5)
                    self.assertAlmostEqual(center.y(), button.height() / 2, delta=0.5)
                interactions = window.findChild(QQuickItem, "filmSpectrumInteractions")
                self.assertEqual(self.rect_in(interactions, plot), viewport_rect)

                # Only the axes receive panning. A legend press is consumed by
                # its presentation surface, and footer/axis labels are outside.
                controller.zoomPlot(0.5, 0.5, 0.5)
                QCoreApplication.processEvents()
                for item in (legend, summary, window.findChild(QQuickItem, "filmSpectrumXAxisTitle")):
                    start = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
                    before = controller.plotView
                    QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start.toPoint())
                    QTest.mouseMove(window, (start + QPointF(10, 0)).toPoint())
                    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, (start + QPointF(10, 0)).toPoint())
                    QCoreApplication.processEvents()
                    self.assertEqual(before, controller.plotView)
                center = viewport.mapToScene(QPointF(viewport.width() * 0.5, viewport.height() * 0.5))
                before = controller.plotView
                QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center.toPoint())
                QTest.mouseMove(window, (center + QPointF(25, 0)).toPoint())
                QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, (center + QPointF(25, 0)).toPoint())
                QCoreApplication.processEvents()
                self.assertNotEqual(before["x_min"], controller.plotView["x_min"])
                # The mouse grab may continue outside the area, but those
                # position changes must not cause off-axis panning.
                before = controller.plotView
                QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center.toPoint())
                outside = viewport.mapToScene(QPointF(viewport.width() + 10, viewport.height() / 2))
                QTest.mouseMove(window, outside.toPoint())
                QCoreApplication.processEvents()
                self.assertEqual(before, controller.plotView)
                QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, outside.toPoint())
                controller.resetPlotView()

                # Force one missing sample into a plotted result to confirm the
                # UI creates two independent vector paths for that single series.
                reflectance = list(controller._result["R"])
                reflectance[len(reflectance) // 2] = float("nan")
                controller._result["R"] = reflectance
                controller._refresh_plot_data()
                controller.plotDataChanged.emit()
                QCoreApplication.processEvents()
                r_series = next(item for item in controller.plotData["series"]
                                if item["channel"] == "R")
                self.assertEqual(len(r_series["segments"]), 2)
                status_text = str(controller.plotData.get("dataStatusText", ""))
                self.assertTrue(status_text)
                self.assertIn("部分采样无效 · 曲线已分段",
                              summary_text.property("text"))
                self.assertNotIn(status_text, summary_text.property("text"))
                self.assertEqual(status_text, summary_text.property("accessibleDataStatus"))
                curve_layer = window.findChild(QQuickItem, "filmSpectrumCurveTransformLayer")
                curve_names = [str(item.objectName()) for item in self.visual_items(curve_layer)
                               if "filmSpectrumVectorCurve_" in str(item.objectName())]
                self.assertIsNotNone(curve_layer)
                self.assertTrue(any(name == "filmSpectrumVectorCurve_0" for name in curve_names), curve_names)
                self.assertTrue(any(name == "filmSpectrumVectorCurve_0_segment_1" for name in curve_names), curve_names)
                r_legends = [item for item in self.visual_items(legend)
                             if str(item.objectName()).startswith("filmSpectrumLegendItem_R_")]
                self.assertEqual(len(r_legends), 1, "Curve gaps must not duplicate legend entries")

                diagnostics = window.findChild(QQuickItem, "filmSimulationDiagnosticsSidebar")
                window.setProperty("diagnosticsExpanded", True)
                QCoreApplication.processEvents()
                self.assertTrue(diagnostics.property("visible"))
                sidebar_width = float(diagnostics.property("sidebarWidth"))
                self.assertGreaterEqual(sidebar_width, 384.0)
                self.assertLessEqual(sidebar_width, 520.0)
                controls_rect = self.rect_in(controls, plot)
                diagnostic_x = diagnostics.mapToItem(plot, QPointF(0, 0)).x()
                self.assertLessEqual(controls_rect[0] + controls_rect[2],
                                     float(diagnostic_x) + 1.0)

                # Narrow windows keep all plot actions within the vector area
                # and move the legend below them, so neither overlay is clipped.
                window.setWidth(720)
                window.setHeight(760)
                window.setProperty("diagnosticsExpanded", False)
                window.setProperty("statisticsExpanded", True)
                QCoreApplication.processEvents()
                controls_rect = self.rect_in(controls, plot)
                viewport_rect = self.rect_in(viewport, plot)
                legend_rect = self.rect_in(legend, plot)
                self.assertLess(viewport_rect[2], 400.0)
                self.assertGreaterEqual(controls_rect[0], viewport_rect[0] - 1.0)
                self.assertLessEqual(controls_rect[0] + controls_rect[2],
                                     viewport_rect[0] + viewport_rect[2] + 1.0)
                if viewport_rect[2] < 400.0:
                    self.assertGreaterEqual(legend_rect[1],
                                            controls_rect[1] + controls_rect[3] - 1.0)
                else:
                    self.assertLessEqual(legend_rect[1],
                                         controls_rect[1] + 1.0)
                self.assertGreater(legend_rect[2], 40.0)
                summary_rect = self.rect_in(summary, plot)
                summary_text_item = window.findChild(QObject,
                                                     "filmSpectrumFloatingResultSummaryText")
                self.assertIsNotNone(summary_text_item)
                self.assertGreaterEqual(summary_rect[1], viewport_rect[1] + viewport_rect[3])
                self.assertLessEqual(float(summary_rect[1] + summary_rect[3]), plot.height() + 1.0)
                self.assertLessEqual(float(summary_text_item.property("contentHeight")),
                                     float(summary_text_item.property("height")) + 1.0)

                original_name = controller.designName
                original_summary = controller.resultSummary
                replacement = new_design("Replacement that must be cancelled")
                self.assertTrue(controller.reuseWindowOpen("", replacement))
                dialog = window.findChild(QObject, "filmSimulationReplacementConfirmation")
                self.assertIsNotNone(dialog)
                self.assertTrue(self.wait_until(lambda: bool(dialog.property("visible"))))
                dialog.reject()
                QCoreApplication.processEvents()
                self.assertFalse(controller.hasPendingOpen)
                self.assertEqual(controller.designName, original_name)
                self.assertTrue(controller.hasResult)
                self.assertEqual(controller.resultSummary, original_summary)

                save_button = window.findChild(QObject, "filmSimulationSaveButton")
                self.assertIsNotNone(save_button)
                self.click(window, save_button)
                self.assertFalse(controller.documentDirty)
                self.assertTrue(Path(controller.designPath).is_file())

                # A dirty independent simulation asks before closing; rejection
                # keeps it open and acceptance closes through the guarded path.
                controller.setParameter("angle_deg", 3.0)
                self.assertTrue(controller.documentDirty)
                self.assertFalse(controller.requestClose())
                self.assertTrue(self.wait_until(lambda: bool(dialog.property("visible"))))
                self.assertEqual(dialog.property("title"), "确认关闭仿真？")
                dialog.reject()
                QCoreApplication.processEvents()
                self.assertTrue(window.isVisible())
                self.assertTrue(controller.documentDirty)
                self.assertFalse(controller.requestClose())
                self.assertTrue(self.wait_until(lambda: bool(dialog.property("visible"))))
                dialog.accept()
                self.assertTrue(self.wait_until(lambda: not window.isVisible()))
                self.assertFalse(controller.documentDirty)
            finally:
                if controller is not None:
                    controller.close()
                if window is not None:
                    window.close()
                if engine is not None:
                    engine.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    QCoreApplication.processEvents()


def _run_pixel_probe():
    """Child process: verify actual curve pixels, not just item geometry."""
    import numpy as np
    from PySide6.QtGui import QImage
    test = FilmSimulationQuickUiRefinementTests()
    with tempfile.TemporaryDirectory() as folder:
        root_path = Path(folder)
        service, design = test.synthetic_glass_fixture(root_path)
        engine, window, controller = create_film_simulation_quick_window(
            root=root_path, service=service, design=design,
            geometry_settings=QSettings(str(root_path / "geometry.ini"), QSettings.Format.IniFormat))
        try:
            controller._render_plot = lambda: None
            controller._stale = False
            controller._result = {"wavelength_nm": [400.0, 500.0, 600.0],
                                  "T": [0.25, 0.25, 0.25]}
            test.assertTrue(controller.setPlotSettings({
                "curves": ["T"], "x": {"auto": True},
                "y": {"unit": "fraction", "auto": False, "min": 0.0, "max": 1.0},
            }))
            controller.stateChanged.emit()
            window.setWidth(1100)
            window.setHeight(760)
            window.setProperty("statisticsExpanded", False)
            window.setProperty("diagnosticsExpanded", False)
            window.show()
            phases = []
            for phase in ("flat_25_percent", "zoomed", "panned"):
                if phase == "zoomed":
                    controller._result["T"] = [-0.35, 0.5, 1.35]
                    controller._refresh_plot_data()
                    controller.plotDataChanged.emit()
                    controller.zoomPlot(0.5, 0.5, 0.5)
                elif phase == "panned":
                    controller.panPlot(0.1, -0.1)
                for _ in range(8):
                    QCoreApplication.processEvents()
                    QTest.qWait(10)
                viewport = window.findChild(QQuickItem, "filmSpectrumVectorViewport")
                image = window.grabWindow()
                test.assertFalse(image.isNull())
                ratio = image.width() / window.width()
                origin = viewport.mapToScene(QPointF(0, 0))
                left, top = origin.x(), origin.y()
                right, bottom = left + viewport.width(), top + viewport.height()
                view = controller.plotView
                x = (view["x_min"] + view["x_max"]) / 2
                q = 0.25 if phase == "flat_25_percent" else -0.35 + (x - 400.0) / 200.0 * 1.7
                px = round((left + viewport.width() / 2) * ratio)
                py = round((top + (view["y_max"] - q) / (view["y_max"] - view["y_min"])
                            * viewport.height()) * ratio)
                rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
                pixels = np.frombuffer(rgba.constBits(), dtype=np.uint8).reshape(
                    rgba.height(), rgba.bytesPerLine())[:, :rgba.width() * 4].reshape(
                    rgba.height(), rgba.width(), 4)
                matches = np.all(np.abs(pixels[:, :, :3].astype(np.int16)
                                       - np.array([217, 119, 6])) <= 25, axis=2)
                test.assertTrue(matches[py - 3:py + 4, px - 2:px + 3].any(),
                                f"{phase}: expected curve near physical pixel {(px, py)} at DPR {ratio}")
                xs = (np.arange(image.width()) + 0.5) / ratio
                ys = (np.arange(image.height()) + 0.5) / ratio
                inside = ((ys >= top) & (ys <= bottom))[:, None] & ((xs >= left) & (xs <= right))[None, :]
                outside = int(np.count_nonzero(matches & ~inside))
                test.assertEqual(outside, 0, f"{phase}: curve leaked outside the axes at DPR {ratio}")
                phases.append(phase)
            print(json.dumps({"phases": phases, "pixel_ratio": ratio, "outside_curve_pixels": outside}))
        finally:
            controller.close()
            window.close()
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QCoreApplication.processEvents()


if __name__ == "__main__":
    if "--pixel-probe" in sys.argv:
        _run_pixel_probe()
    else:
        unittest.main()
