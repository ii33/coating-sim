"""Qt Quick synthetic event-dispatch regressions; no hardware is simulated.

Run with QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software for headless CI.
Set COAT_VIEWPORT_SCREENSHOT_DIR to retain optional rendered QA evidence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import time
import urllib.request
import unittest
from unittest.mock import patch
from typing import cast

from PySide6.QtCore import QObject, QPoint, QPointF, QCoreApplication, QEvent, QSettings as QtQSettings, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QInputDevice, QNativeGestureEvent, QPointingDevice, QRawFont, QTextLayout, QWheelEvent
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from coat_sim.core.api import CoatService
from coat_sim.ui.machine_mask import MaskBridge, create_editor
from coat_sim.ui import launcher as launcher_module
from coat_sim.ui.fonts import bundled_font_family
from coat_sim.ui import window_geometry as window_geometry_module
from coat_sim.ui.theme_service import ThemeService
from tests import launcher_test_isolation as launcher_test_isolation_module
from tests.launcher_test_isolation import LauncherTestIsolation


class TemporaryOnlyQSettings:
    """Reject QSettings' organization/application constructor in these tests."""
    Format = QtQSettings.Format
    Status = QtQSettings.Status
    Scope = QtQSettings.Scope

    def __new__(cls, *args, **kwargs):
        if kwargs or len(args) != 2 or args[1] != QtQSettings.Format.IniFormat:
            raise AssertionError("mask viewport tests require an explicit IniFormat filename")
        filename = Path(args[0])
        if not filename.is_absolute():
            raise AssertionError("mask viewport test QSettings filename must be absolute")
        return QtQSettings(str(filename), QtQSettings.Format.IniFormat)


QSettings = TemporaryOnlyQSettings


def temporary_ini_settings(filename, temporary_root):
    """Open only a new IniFormat file proven to live under this temp root."""
    root = Path(temporary_root).resolve()
    settings_file = Path(filename)
    if not settings_file.is_absolute():
        raise AssertionError("temporary QSettings path must be absolute")
    resolved_file = settings_file.resolve()
    if not resolved_file.is_relative_to(root):
        raise AssertionError(f"temporary QSettings path escaped temp root: {resolved_file}")
    if resolved_file.exists():
        raise AssertionError(f"temporary QSettings file must be new: {resolved_file}")
    resolved_file.parent.mkdir(parents=True, exist_ok=True)

    # Always pass an explicit filename and IniFormat. The organization/app
    # overload selects Windows NativeFormat and can target the user registry.
    settings = QSettings(str(resolved_file), QSettings.Format.IniFormat)
    actual_file = Path(settings.fileName()).resolve()
    if (settings.format() != QSettings.Format.IniFormat
            or actual_file != resolved_file
            or not actual_file.is_relative_to(root)):
        raise AssertionError(f"QSettings did not stay in the temporary INI: {actual_file}")
    return settings


class MaskViewportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.original_app_font = cls.app.font()
        cls.addClassCleanup(cls.app.setFont, cls.original_app_font)
        cls.cjk_font_family = bundled_font_family()
        if not cls.cjk_font_family:
            raise AssertionError("bundled Noto CJK font did not register")
        cls.app.setFont(QFont(cls.cjk_font_family))
        cls.cjk_raw_font = QRawFont.fromFont(cls.app.font())
        cjk_codepoints = (0x6EDA, 0x8F6E, 0x5E73, 0x79FB, 0x7F29, 0x653E)
        if not cls.cjk_raw_font.isValid() or not all(
                cls.cjk_raw_font.supportsCharacter(codepoint) for codepoint in cjk_codepoints):
            raise AssertionError("registered test font does not provide required Chinese glyphs")
        cls.touchpad = QPointingDevice("viewport-test-pad", 471,
            QInputDevice.DeviceType.TouchPad, QPointingDevice.PointerType.Finger,
            QInputDevice.Capability.Position | QInputDevice.Capability.Scroll |
            QInputDevice.Capability.PixelScroll, 2, 0)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for module in (window_geometry_module, launcher_test_isolation_module):
            qsettings_guard = patch.object(module, "QSettings", TemporaryOnlyQSettings)
            qsettings_guard.start()
            self.addCleanup(qsettings_guard.stop)
        settings_file = self.root / "qt-settings" / "coat-sim.ini"
        self.qt_settings = temporary_ini_settings(settings_file, self.root)
        self.addCleanup(self.restore_qsettings)
        self.qt_settings.setValue("maskViewport/width", 1280)
        self.qt_settings.setValue("maskViewport/height", 900)
        self.qt_settings.sync()
        self.assertEqual(self.qt_settings.status(), QSettings.Status.NoError)

        self.network_blockers = [
            patch("socket.create_connection",
                  side_effect=AssertionError("network is disabled in mask viewport tests")),
            patch("socket.socket.connect",
                  side_effect=AssertionError("network is disabled in mask viewport tests")),
            patch("urllib.request.urlopen",
                  side_effect=AssertionError("network is disabled in mask viewport tests")),
        ]
        for blocker in self.network_blockers:
            blocker.start()
            self.addCleanup(blocker.stop)

        self.isolation = LauncherTestIsolation(
            self.root, launcher_module).apply()
        self.addCleanup(self.isolation.stop)
        self.theme = ThemeService.instance()
        self.theme.configure(mode="light", theme_package_id="")
        screenshot_directory = (os.environ.get("COAT_VIEWPORT_SCREENSHOT_DIR")
                                or str(self.root / "screenshots"))
        self.screenshot_patch = patch.dict(os.environ, {
            "COAT_VIEWPORT_SCREENSHOT_DIR": screenshot_directory})
        self.screenshot_patch.start()
        self.addCleanup(self.screenshot_patch.stop)
        self.service = CoatService(self.root)
        self.files = {
            "source.csrc": {"schema_version": 2, "kind": "point",
                            "device_type": "custom", "source_exponent": 1.0},
            "mask.cmsk": {"z_mm": 500, "outer": [[-80,-100],[80,-100],[80,100],[-80,100]],
                          "holes": [[[-20,-20],[20,-20],[20,20],[-20,20]]]},
            "machine.cmac": {"schema_version": 5, "carrier_x_mm": 0., "carrier_y_mm": 0.,
                "planet_offset_mm": 312., "planet_tilt_deg": 0., "substrate_z_mm": 1000.,
                "sun_teeth": 120, "planet_teeth": 36, "gear_module_mm": 4.,
                "sources": [{"file": "source.csrc", "x_mm": 1600., "y_mm": 200., "z_mm": 0.},
                            {"file": "source.csrc", "x_mm": -1800., "y_mm": -350., "z_mm": 50.}],
                "masks": [{"file": "mask.cmsk"}], "monitors": []},
        }
        for name, data in self.files.items():
            (self.root / name).write_text(json.dumps(data), encoding="utf-8")
        self.directory_patch = patch("coat_sim.ui.machine_mask.design_directory", return_value=self.root)
        self.directory_patch.start()
        self.addCleanup(self.directory_patch.stop)
        self.engine, self.bridge = create_editor("mask", self.service)
        self.addCleanup(self.close_engine)
        self.window = self.engine.rootObjects()[0]
        self.window.showNormal()
        self.window.resize(1280, 900)
        self.canvas = self.window.findChild(QQuickItem, "maskViewportCanvas")
        self.assertIsNotNone(self.canvas)
        self.bridge.loadDocument(str(self.root / "mask.cmsk"))
        self.bridge.loadMachine(str(self.root / "machine.cmac"))
        self.wait_for_initial_read()
        self.assertEqual(len(json.loads(self.bridge.ringsJson)), 2, self.bridge.status)

    def wait_for_initial_read(self, timeout=15.0):
        deadline = time.monotonic() + timeout
        while (not bool(self.window.property("initialReadFinished"))
               and time.monotonic() < deadline):
            self.app.processEvents()
            QTest.qWait(10)
        self.assertTrue(bool(self.window.property("initialReadFinished")),
                        f"initial read did not finish in {timeout:.1f}s; "
                        f"stage={self.window.property('initialReadStage')!r}")

    def tearDown(self):
        self.close_engine()

    def close_engine(self):
        engine = getattr(self, "engine", None)
        if engine is None:
            return
        window = getattr(self, "window", None)
        if window is not None:
            window.close()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.engine = None
        self.window = None
        self._tooltip_references = []

    def restore_qsettings(self):
        self.qt_settings = None

    def expression(self, expression):
        value = QQmlExpression(self.engine.rootContext(), self.window, expression)
        result = value.evaluate()
        self.assertFalse(value.hasError(), value.error().toString())
        if isinstance(result, tuple):
            result = result[0]
        return result.toVariant() if hasattr(result, "toVariant") else result

    def view(self):
        return tuple(float(self.window.property(key)) for key in ("panX", "panY", "zoomX", "zoomY"))

    def assert_view_almost_equal(self, expected, places=10):
        for actual, target in zip(self.view(), expected):
            self.assertAlmostEqual(actual, target, places=places)

    def assert_text_has_shaped_cjk_glyphs(self, text, font):
        """Check Qt's resolved font runs, including fallback from the theme font."""
        cjk_chars = {char for char in text if "\u3400" <= char <= "\u9fff"}
        self.assertTrue(cjk_chars)
        for sample in (text, *sorted(cjk_chars)):
            layout = QTextLayout(sample, font)
            layout.beginLayout()
            line = layout.createLine()
            self.assertTrue(line.isValid(), sample)
            line.setLineWidth(10000)
            layout.endLayout()
            runs = layout.glyphRuns()
            self.assertTrue(runs, sample)
            self.assertTrue(all(run.rawFont().isValid()
                                and run.glyphIndexes()
                                and all(index != 0 for index in run.glyphIndexes())
                                for run in runs), sample)
            if len(sample) == 1:
                self.assertTrue(any(run.rawFont().supportsCharacter(ord(sample))
                                    for run in runs), sample)

    def view_diagnostic(self):
        return {
            "autoFitView": self.window.property("autoFitView"),
            "canvas": (self.canvas.width(), self.canvas.height()),
            "bounds": self.expression("geometryBounds()"),
            "view": self.view(),
            "devicePixelRatio": self.window.devicePixelRatio(),
            "windowVisible": self.window.isVisible(),
        }

    def wheel(self, *, pixel=(0,0), angle=(0,0), modifiers=Qt.NoModifier, touchpad=False,
              point=(210.,170.), phase=Qt.ScrollUpdate, inverted=False):
        position = self.canvas.mapToScene(QPointF(*point))
        event = QWheelEvent(position, self.window.mapToGlobal(position), QPoint(*pixel), QPoint(*angle),
                            Qt.NoButton, modifiers, phase, inverted,
                            Qt.MouseEventSource.MouseEventSynthesizedByApplication,
                            self.touchpad if touchpad else QPointingDevice.primaryPointingDevice())
        QCoreApplication.sendEvent(self.window, event)
        QTest.qWait(10)
        return event

    def assert_geometry_visible(self):
        x0, y0, x1, y1 = self.expression("geometryBounds()")
        for x, y in ((x0,y0),(x1,y1)):
            px = self.expression(f"px({x})")
            py = self.expression(f"py({y})")
            plot_top = self.expression("plotContentTop()")
            self.assertGreaterEqual(px, 31.99, f"x={x}, y={y}, px={px}, {self.view_diagnostic()}")
            self.assertGreaterEqual(py, plot_top+31.99)
            self.assertLessEqual(px, self.canvas.width()-31.99)
            self.assertLessEqual(py, self.canvas.height()-31.99)
        self.assertAlmostEqual(self.window.property("zoomX"), self.window.property("zoomY"))

    def screenshot(self, name):
        output = os.environ.get("COAT_VIEWPORT_SCREENSHOT_DIR")
        if output:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            QTest.qWait(50)
            image = self.window.grabWindow()
            self.assertFalse(image.isNull())
            self.assertTrue(image.save(str(directory / name)))

    def test_initial_fit_contains_two_real_rings_mask_and_hole_after_resize(self):
        self.assertEqual(self.window.title(), "修正板")
        toolbar = self.window.findChild(QQuickItem, "maskViewportFloatingToolbar")
        self.assertIsNotNone(toolbar)
        for theme in ("light", "dark"):
            self.theme.configure(mode=theme, theme_package_id="")
            self.app.processEvents()
            for width, height in ((1280,900),(960,640)):
                self.window.resize(width, height)
                QTest.qWait(40)
                self.assert_geometry_visible()
                self.assertLessEqual(toolbar.width(), self.canvas.width())
                dpr = self.window.devicePixelRatio()
                self.screenshot(f"mask-{theme}-{width}x{height}-dpr{dpr:g}.png")
        for width, height in ((960,640),(1440,1000),(1100,760)):
            self.window.resize(width, height)
            QTest.qWait(40)
            self.assert_geometry_visible()

    def test_early_pan_survives_staged_initial_read(self):
        self.window.close()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

        self.engine, self.bridge = create_editor("mask", self.service)
        self.window = self.engine.rootObjects()[0]
        self.window.showNormal()
        self.window.resize(1280, 900)
        self.canvas = self.window.findChild(QQuickItem, "maskViewportCanvas")
        self.assertIsNotNone(self.canvas)
        self.assertEqual(str(self.window.property("viewportScrollMode")), "pan")
        mode_button = self.window.findChild(QQuickItem, "coatcontrol:viewport_scroll_mode")
        self.assertIsNotNone(mode_button)
        self.assertEqual(str(mode_button.property("text")), "滚轮：平移")
        self.bridge.loadDocument(str(self.root / "mask.cmsk"))
        self.bridge.loadMachine(str(self.root / "machine.cmac"))

        self.assertFalse(bool(self.window.property("initialReadFinished")))
        self.window.panView(29, -17)
        expected = self.view()
        self.assertFalse(bool(self.window.property("autoFitView")))
        self.wait_for_initial_read()
        self.assert_view_almost_equal(expected)

    def test_source_ring_palette_is_stable_and_distinct_in_light_and_dark_themes(self):
        palettes = {}
        for dark_surface in (False, True):
            colors = [str(self.expression(f"sourceColorForTheme({slot},{str(dark_surface).lower()})"))
                      for slot in range(1, 9)]
            self.assertTrue(all(color.startswith("#") and len(color) == 7 for color in colors))
            self.assertEqual(len(set(colors)), 8)
            palettes[dark_surface] = colors
        self.assertTrue(all(light != dark for light, dark in zip(palettes[False], palettes[True])))
        for slot in range(1, 9):
            self.assertTrue(str(self.expression(f"sourceColor({slot})")).startswith("#"))

        def luminance(color):
            channels = [int(color[index:index+2], 16)/255 for index in (1, 3, 5)]
            linear = [value/12.92 if value <= 0.04045 else ((value+0.055)/1.055)**2.4
                      for value in channels]
            return sum(value*weight for value, weight in zip(linear, (0.2126,0.7152,0.0722)))

        def contrast(first, second):
            high, low = sorted((luminance(first), luminance(second)), reverse=True)
            return (high+0.05)/(low+0.05)

        for color in palettes[False]:
            self.assertGreater(contrast(color, "#ffffff"), 3)
        for color in palettes[True]:
            self.assertGreater(contrast(color, "#14181c"), 3)

    def test_pixel_and_angle_touchpad_pan_zero_and_horizontal_wheel(self):
        x, y, zx, zy = self.view()
        self.wheel(pixel=(27,-18), touchpad=True)
        self.assert_view_almost_equal((x+27,y-18,zx,zy))
        self.wheel(angle=(0,60), touchpad=True)
        self.assert_view_almost_equal((x+27,y+2,zx,zy))
        self.wheel(angle=(120,0))
        self.assert_view_almost_equal((x+67,y+2,zx,zy))
        before = self.view()
        self.wheel(touchpad=True, phase=Qt.ScrollEnd)
        self.assertEqual(self.view(), before)
        self.assertFalse(self.window.property("autoFitView"))

    def test_cold_horizontal_pixel_angle_and_mixed_axis_delta_pan_once(self):
        revision = int(self.window.property("viewportInteractionRevision"))
        before = self.view()
        pixel_event = self.wheel(pixel=(23,0), touchpad=True, phase=Qt.NoScrollPhase)
        self.assertEqual(pixel_event.pixelDelta(), QPoint(23,0))
        self.assert_view_almost_equal((before[0]+23,before[1],before[2],before[3]))
        revision += 1
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision)

        QTest.qWait(120)
        before = self.view()
        angle_event = self.wheel(angle=(-120,0), touchpad=True, phase=Qt.NoScrollPhase)
        self.assertEqual(angle_event.angleDelta(), QPoint(-120,0))
        self.assert_view_almost_equal((before[0]-40,before[1],before[2],before[3]))
        revision += 1
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision)

        before = self.view()
        self.wheel(pixel=(11,0), angle=(0,60), touchpad=True,
                   phase=Qt.NoScrollPhase)
        self.assert_view_almost_equal((before[0]+11,before[1]+20,before[2],before[3]))
        revision += 1
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision)

    def test_horizontal_diagonal_vertical_and_scroll_end_each_route_once(self):
        revision = int(self.window.property("viewportInteractionRevision"))
        before = self.view()
        self.wheel(pixel=(15,0), touchpad=True)
        revision += 1
        self.assert_view_almost_equal((before[0]+15,before[1],before[2],before[3]))
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision)

        before = self.view()
        self.wheel(angle=(120,60), touchpad=True)
        revision += 1
        self.assert_view_almost_equal((before[0]+40,before[1]+20,before[2],before[3]))
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision)

        before = self.view()
        self.wheel(angle=(0,-60), touchpad=True)
        revision += 1
        self.assert_view_almost_equal((before[0],before[1]-20,before[2],before[3]))
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision)

        self.wheel(touchpad=True, phase=Qt.ScrollEnd)
        QTest.qWait(120)
        before = self.view()
        self.wheel(pixel=(-9,0), touchpad=True, phase=Qt.NoScrollPhase)
        revision += 1
        self.assert_view_almost_equal((before[0]-9,before[1],before[2],before[3]))
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision)

    def test_horizontal_wheel_after_native_pinch_end_and_toolbar_hit_boundaries(self):
        pinch_position = self.canvas.mapToScene(QPointF(210,170))
        global_position = self.window.mapToGlobal(pinch_position)
        for kind, value in ((Qt.BeginNativeGesture,0), (Qt.ZoomNativeGesture,0.1),
                            (Qt.EndNativeGesture,0)):
            event = QNativeGestureEvent(kind, self.touchpad, 2, pinch_position,
                                        pinch_position, global_position, value, QPointF())
            QCoreApplication.sendEvent(self.window, event)
            QTest.qWait(10)

        before = self.view()
        revision = int(self.window.property("viewportInteractionRevision"))
        self.wheel(angle=(120,0), touchpad=True, phase=Qt.NoScrollPhase)
        self.assert_view_almost_equal((before[0]+40,before[1],before[2],before[3]))
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision+1)

        def canvas_point_for(item):
            scene_point = item.mapToScene(QPointF(item.width()/2,item.height()/2))
            canvas_origin = self.canvas.mapToScene(QPointF(0,0))
            return scene_point-canvas_origin

        pan_button = self.window.findChild(QQuickItem, "coatcontrol:pan_tool")
        instructions = self.window.findChild(QQuickItem, "maskViewportInstructions")
        self.assertIsNotNone(pan_button)
        self.assertIsNotNone(instructions)
        before = self.view()
        revision = int(self.window.property("viewportInteractionRevision"))
        button_point = canvas_point_for(pan_button)
        self.wheel(angle=(120,0), touchpad=True, point=(button_point.x(),button_point.y()))
        self.assertEqual(self.view(), before)
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision)

        help_point = canvas_point_for(instructions)
        self.wheel(angle=(120,0), touchpad=True, point=(help_point.x(),help_point.y()))
        self.assert_view_almost_equal((before[0]+40,before[1],before[2],before[3]))
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision+1)

    def test_collapsible_sidebar_keeps_edit_input_and_expands_canvas(self):
        transform_button = self.window.findChild(QQuickItem, "coatcontrol:mask_sidebar_transform")
        analysis_button = self.window.findChild(QQuickItem, "coatcontrol:mask_sidebar_analysis")
        sidebar = self.window.findChild(QQuickItem, "maskEditorSidebar")
        shift_x = self.window.findChild(QQuickItem, "coatcontrol:shift_x_mm")
        self.assertIsNotNone(transform_button)
        self.assertIsNotNone(analysis_button)
        self.assertIsNotNone(sidebar)
        self.assertIsNotNone(shift_x)
        for object_name in ("coatcontrol:shift_x_mm", "coatcontrol:shift_y_mm",
                            "coatcontrol:scale_x", "coatcontrol:scale_y"):
            field = self.window.findChild(QQuickItem, object_name)
            self.assertIsNotNone(field, object_name)
            self.assertGreaterEqual(field.width(), 50, object_name)
        for button, label in ((transform_button, "变换操作"),
                              (analysis_button, "遮挡计算")):
            self.assertEqual(str(button.property("text")), label)
            font = button.property("font")
            text_width = QFontMetricsF(font).horizontalAdvance(label)
            content = button.property("contentItem")
            self.assertIsInstance(content, QQuickItem)
            self.assertGreaterEqual(content.width(), text_width - 1,
                                    f"{label} must not be elided")
        shift_x.setProperty("text", "13.5")
        self.app.processEvents()
        expanded_canvas_width = self.canvas.width()

        self.click_item("coatcontrol:mask_sidebar_transform")
        self.app.processEvents()
        self.assertFalse(bool(self.window.property("maskSidebarExpanded")))
        self.assertGreater(self.canvas.width(), expanded_canvas_width+100)
        self.assertEqual(str(shift_x.property("text")), "13.5")

        self.click_item("coatcontrol:mask_sidebar_analysis")
        self.app.processEvents()
        self.assertTrue(bool(self.window.property("maskSidebarExpanded")))
        self.assertEqual(str(self.window.property("maskSidebarPage")), "analysis")
        for object_name in ("coatcontrol:workpiece_diameter_mm", "coatcontrol:mask_slot",
                            "coatcontrol:source_slot", "coatcontrol:patch_radius_mm",
                            "coatcontrol:weight_y_min", "coatcontrol:weight_y_max"):
            field = self.window.findChild(QQuickItem, object_name)
            self.assertIsNotNone(field, object_name)
            self.assertGreaterEqual(field.width(), 50, object_name)
        self.window.resize(960, 640)
        QTest.qWait(80)
        for theme_mode in ("light", "dark"):
            self.theme.configure(mode=theme_mode, theme_package_id="")
            self.app.processEvents()
            self.screenshot(f"mask-sidebar-analysis-{theme_mode}-960x640-"
                            f"dpr{self.window.devicePixelRatio():g}.png")
        for _ in range(8):
            self.click_item("coatcontrol:mask_sidebar_transform")
            self.click_item("coatcontrol:mask_sidebar_analysis")
        self.assertEqual(str(shift_x.property("text")), "13.5")

    def test_zoom_is_proportional_cursor_anchored_and_reversible(self):
        self.window.setProperty("viewportScrollMode", "automatic")
        before = self.view()
        world = (self.expression("mmX(210)"), self.expression("mmY(170)"))
        for _ in range(8):
            self.wheel(angle=(0,15))
        self.assertAlmostEqual(self.view()[2], before[2]*1.12, places=10)
        self.assertAlmostEqual(self.expression("mmX(210)"), world[0], places=8)
        self.assertAlmostEqual(self.expression("mmY(170)"), world[1], places=8)
        self.wheel(angle=(0,-120))
        for actual, expected in zip(self.view(), before):
            self.assertAlmostEqual(actual, expected, places=8)
        self.wheel(pixel=(0,60), touchpad=True, modifiers=Qt.ControlModifier)
        self.assertAlmostEqual(self.view()[2], before[2]*1.12**0.5, places=10)
        self.assertAlmostEqual(self.expression("mmX(210)"), world[0], places=8)
        self.assertAlmostEqual(self.expression("mmY(170)"), world[1], places=8)
        self.screenshot("mask-cursor-anchored-zoom.png")

    def test_touchpad_inverted_scroll_and_ctrl_zoom_preserve_anchor_and_phase(self):
        before = self.view()
        self.wheel(pixel=(17,-9), touchpad=True, inverted=True)
        self.assert_view_almost_equal((before[0]+17,before[1]-9,before[2],before[3]))

        before = self.view()
        self.wheel(angle=(0,60), touchpad=True, inverted=True)
        self.assert_view_almost_equal((before[0],before[1]+20,before[2],before[3]))

        before = self.view()
        self.wheel(angle=(120,-60), touchpad=True)
        self.assert_view_almost_equal((before[0]+40,before[1]-20,before[2],before[3]))

        anchor = (310.,245.)
        world = (self.expression(f"mmX({anchor[0]})"), self.expression(f"mmY({anchor[1]})"))
        before = self.view()
        self.wheel(pixel=(0,60), modifiers=Qt.ControlModifier, touchpad=True,
                   point=anchor, inverted=True)
        self.assertAlmostEqual(self.view()[2], before[2]*1.12**0.5, places=10)
        self.assertAlmostEqual(self.expression(f"mmX({anchor[0]})"), world[0], places=8)
        self.assertAlmostEqual(self.expression(f"mmY({anchor[1]})"), world[1], places=8)

        before = self.view()
        self.wheel(angle=(0,60), modifiers=Qt.ControlModifier, touchpad=True,
                   point=anchor, inverted=True)
        self.assertAlmostEqual(self.view()[2], before[2]*1.12**0.5, places=10)
        self.assertAlmostEqual(self.expression(f"mmX({anchor[0]})"), world[0], places=8)
        self.assertAlmostEqual(self.expression(f"mmY({anchor[1]})"), world[1], places=8)

        manual = self.view()
        wheel_handler = self.window.findChild(QObject, "maskViewportWheel")
        self.assertIsNotNone(wheel_handler)
        for phase in (Qt.ScrollBegin, Qt.ScrollEnd):
            self.wheel(touchpad=True, phase=phase)
            self.assert_view_almost_equal(manual)
            self.assertEqual(bool(wheel_handler.property("active")), phase == Qt.ScrollBegin)

    def test_default_no_phase_angle_only_mouse_shaped_wheel_pans(self):
        # This is a synthetic event-dispatch regression for an ambiguous
        # Angle-only primary-Mouse event shape; this does not represent touchpad hardware.
        before = self.view()
        revision = int(self.window.property("viewportInteractionRevision"))
        event = self.wheel(angle=(0,120), touchpad=False,
                           phase=Qt.NoScrollPhase)
        self.assertEqual(event.device().type(), QInputDevice.DeviceType.Mouse)
        self.assertEqual(event.source(), Qt.MouseEventSource.MouseEventSynthesizedByApplication)
        self.assertEqual(event.phase(), Qt.NoScrollPhase)
        self.assertEqual(event.pixelDelta(), QPoint(0,0))
        self.assertEqual(event.angleDelta(), QPoint(0,120))
        self.assert_view_almost_equal((before[0],before[1]+40,before[2],before[3]))
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision+1)

        anchor = (310.,245.)
        world = (self.expression(f"mmX({anchor[0]})"), self.expression(f"mmY({anchor[1]})"))
        before = self.view()
        revision = int(self.window.property("viewportInteractionRevision"))
        event = self.wheel(angle=(0,120), modifiers=Qt.ControlModifier,
                           touchpad=False, point=anchor, phase=Qt.NoScrollPhase)
        self.assertAlmostEqual(self.view()[2], before[2]*1.12, places=10)
        self.assertAlmostEqual(self.view()[3], before[3]*1.12, places=10)
        self.assertAlmostEqual(self.expression(f"mmX({anchor[0]})"), world[0], places=8)
        self.assertAlmostEqual(self.expression(f"mmY({anchor[1]})"), world[1], places=8)
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision+1)

        before = self.view()
        revision = int(self.window.property("viewportInteractionRevision"))
        self.wheel(angle=(120,0), modifiers=Qt.ControlModifier, touchpad=False,
                   phase=Qt.NoScrollPhase)
        self.assert_view_almost_equal(before)
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision)

    def test_explicit_automatic_mode_keeps_mouse_shaped_wheel_zoom(self):
        # Synthetic angle-only primary-Mouse shape; this is not hardware proof.
        self.window.setProperty("viewportScrollMode", "automatic")
        before = self.view()
        anchor = (210.,170.)
        world = (self.expression(f"mmX({anchor[0]})"), self.expression(f"mmY({anchor[1]})"))
        self.wheel(angle=(0,120), touchpad=False, point=anchor,
                   phase=Qt.NoScrollPhase)
        self.assertGreater(self.view()[2], before[2])
        self.assertAlmostEqual(self.expression(f"mmX({anchor[0]})"), world[0], places=8)
        self.assertAlmostEqual(self.expression(f"mmY({anchor[1]})"), world[1], places=8)

    def test_automatic_mode_keeps_angle_only_diagonal_horizontal_compatibility(self):
        self.window.setProperty("viewportScrollMode", "automatic")
        before = self.view()
        revision = int(self.window.property("viewportInteractionRevision"))
        event = self.wheel(angle=(120,60), touchpad=False, phase=Qt.NoScrollPhase)
        self.assertEqual(event.angleDelta(), QPoint(120,60))
        self.assert_view_almost_equal((before[0]+40,before[1]+20,before[2],before[3]))
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision+1)

        anchor = (310.,245.)
        world = (self.expression(f"mmX({anchor[0]})"), self.expression(f"mmY({anchor[1]})"))
        before = self.view()
        revision = int(self.window.property("viewportInteractionRevision"))
        self.wheel(angle=(120,60), modifiers=Qt.ControlModifier, touchpad=False,
                   point=anchor, phase=Qt.NoScrollPhase)
        self.assertAlmostEqual(self.view()[2], before[2]*1.12**0.5, places=10)
        self.assertAlmostEqual(self.view()[3], before[3]*1.12**0.5, places=10)
        self.assertAlmostEqual(self.expression(f"mmX({anchor[0]})"), world[0], places=8)
        self.assertAlmostEqual(self.expression(f"mmY({anchor[1]})"), world[1], places=8)
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision+1)

    def test_scroll_mode_toggle_documents_mouse_compatibility(self):
        self.window.resize(960, 640)
        self.app.processEvents()
        mode_button = self.window.findChild(QQuickItem, "coatcontrol:viewport_scroll_mode")
        fit_button = self.window.findChild(QQuickItem, "coatcontrol:fit_all")
        toolbar = self.window.findChild(QQuickItem, "maskViewportFloatingToolbar")
        self.assertIsNotNone(mode_button)
        self.assertIsNotNone(fit_button)
        self.assertIsNotNone(toolbar)
        self.assertEqual(str(mode_button.property("text")), "滚轮：平移")
        self.assertEqual(str(self.window.property("viewportScrollMode")), "pan")
        mode_position = mode_button.mapToItem(toolbar, QPointF(0, 0))
        self.assertGreaterEqual(mode_position.x(), -0.5)
        self.assertGreaterEqual(mode_position.y(), -0.5)
        self.assertLessEqual(mode_position.x() + mode_button.width(), toolbar.width() + 0.5)
        self.assertLessEqual(mode_position.y() + mode_button.height(), toolbar.height() + 0.5)

        self.window.setProperty("toolIndex", 1)
        original_document = self.bridge.documentJson
        self.click_item("coatcontrol:viewport_scroll_mode")
        self.assertEqual(str(self.window.property("viewportScrollMode")), "automatic")
        self.assertEqual(str(mode_button.property("text")), "滚轮：自动")
        self.assertEqual(self.bridge.documentJson, original_document)
        self.assertEqual(self.expression("draftPoints.length"), 0)
        self.assertFalse(bool(self.window.property("hasAnalysisPoint")))

        anchor = (310.,245.)
        world = (self.expression(f"mmX({anchor[0]})"), self.expression(f"mmY({anchor[1]})"))
        before = self.view()
        mouse_wheel = self.wheel(angle=(0,60), touchpad=False,
                                 point=anchor, phase=Qt.NoScrollPhase)
        self.assertEqual(mouse_wheel.device().type(), QInputDevice.DeviceType.Mouse)
        self.assertAlmostEqual(self.view()[2], before[2]*1.12**0.5, places=10)
        self.assertAlmostEqual(self.expression(f"mmX({anchor[0]})"), world[0], places=8)
        self.assertAlmostEqual(self.expression(f"mmY({anchor[1]})"), world[1], places=8)

        self.click_item("coatcontrol:viewport_scroll_mode")
        self.assertEqual(str(self.window.property("viewportScrollMode")), "pan")
        self.assertEqual(str(mode_button.property("text")), "滚轮：平移")
        before = self.view()
        revision = int(self.window.property("viewportInteractionRevision"))
        pan_wheel = self.wheel(angle=(120,-60), touchpad=False,
                               phase=Qt.NoScrollPhase)
        self.assert_view_almost_equal((before[0]+40,before[1]-20,before[2],before[3]))
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision+1)
        self.assertEqual(self.bridge.documentJson, original_document)
        self.assertEqual(self.expression("draftPoints.length"), 0)
        self.assertFalse(bool(self.window.property("hasAnalysisPoint")))

        before = self.view()
        world = (self.expression(f"mmX({anchor[0]})"), self.expression(f"mmY({anchor[1]})"))
        revision = int(self.window.property("viewportInteractionRevision"))
        self.wheel(angle=(0,60), modifiers=Qt.ControlModifier, touchpad=False,
                   point=anchor, inverted=True)
        self.assertAlmostEqual(self.view()[2], before[2]*1.12**0.5, places=10)
        self.assertAlmostEqual(self.expression(f"mmX({anchor[0]})"), world[0], places=8)
        self.assertAlmostEqual(self.expression(f"mmY({anchor[1]})"), world[1], places=8)
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision+1)

        self.click_item("coatcontrol:viewport_scroll_mode")
        self.assertEqual(str(self.window.property("viewportScrollMode")), "automatic")
        before = self.view()
        self.wheel(angle=(0,60), touchpad=False, phase=Qt.NoScrollPhase)
        self.assertGreater(self.view()[2], before[2])

        # Verify the real attached Qt Quick tooltip and its rendered CJK text
        # at the narrow supported window size in each mode and theme.
        self._tooltip_references = []
        button_names = (
            "coatcontrol:select_tool", "coatcontrol:draw_outer", "coatcontrol:draw_hole",
            "coatcontrol:pan_tool", "coatcontrol:fit_all", "coatcontrol:viewport_scroll_mode",
            "coatcontrol:line_mode", "coatcontrol:curve_mode", "coatcontrol:undo_point",
            "coatcontrol:delete_point", "coatcontrol:close_contour",
        )
        glyphs = (0x6EDA, 0x8F6E, 0x5E73, 0x79FB, 0x7F29, 0x653E)
        for name in button_names:
            button = self.window.findChild(QQuickItem, name)
            self.assertIsNotNone(button, name)
            content = button.property("contentItem")
            self.assertIsInstance(content, QQuickItem, name)
            label = str(button.property("text"))
            self.assertEqual(str(content.property("text")), label, name)
            self.assertFalse(bool(content.property("truncated")), name)
            self.assertLessEqual(float(content.property("implicitWidth")),
                                 float(content.width()) + 0.5, name)
            self.assertLessEqual(float(content.property("implicitHeight")),
                                 float(content.height()) + 0.5, name)
            self.assertLessEqual(float(content.property("paintedWidth")),
                                 float(content.width()) + 0.5, name)
            self.assert_text_has_shaped_cjk_glyphs(
                label + "".join(chr(codepoint) for codepoint in glyphs),
                content.property("font"))

        for theme_mode in ("light", "dark"):
            self.theme.configure(mode=theme_mode, theme_package_id="")
            for scroll_mode in ("pan", "automatic"):
                self.window.setProperty("viewportScrollMode", scroll_mode)
                self.app.processEvents()
                button_position = mode_button.mapToScene(
                    QPointF(mode_button.width() / 2, mode_button.height() / 2))
                QTest.mouseMove(self.window, QPoint(round(button_position.x()),
                                                    round(button_position.y())))
                self.app.processEvents()
                context = QQmlEngine.contextForObject(mode_button)
                expression = QQmlExpression(context, mode_button, "ToolTip.toolTip")
                tip = expression.evaluate()
                if isinstance(tip, tuple):
                    tip = tip[0]
                self.assertFalse(expression.hasError(), expression.error().toString())
                self.assertIsNotNone(tip)
                self._tooltip_references.extend((context, expression, tip))
                deadline = time.monotonic() + 3.0
                while not bool(tip.property("visible")) and time.monotonic() < deadline:
                    self.app.processEvents()
                    QTest.qWait(50)
                self.assertTrue(bool(tip.property("visible")),
                                f"tooltip did not open: theme={theme_mode}, mode={scroll_mode}")
                tooltip_text = str(tip.property("text"))
                if scroll_mode == "pan":
                    self.assertIn("\u666e\u901a\u6eda\u8f6e", tooltip_text)
                else:
                    self.assertIn("\u663e\u5f0f\u9f20\u6807\u517c\u5bb9\u6a21\u5f0f", tooltip_text)
                self.assertIn("Ctrl+", tooltip_text)

                content = tip.property("contentItem")
                self.assertIsInstance(content, QQuickItem)
                self._tooltip_references.append(content)
                self.assertEqual(str(content.property("text")), tooltip_text)
                self.assertFalse(bool(content.property("truncated")))
                self.assertGreaterEqual(int(content.property("lineCount")), 1)
                self.assertLessEqual(float(content.property("paintedWidth")),
                                     float(content.width()) + 0.5)
                self.assertLessEqual(float(content.property("paintedHeight")),
                                     float(content.height()) + 0.5)
                context = QQmlEngine.contextForObject(content)
                wrap_expression = QQmlExpression(context, content, "Text.Wrap")
                self._tooltip_references.extend((context, wrap_expression))
                wrap_value = wrap_expression.evaluate()
                if isinstance(wrap_value, tuple):
                    wrap_value = wrap_value[0]
                self.assertFalse(wrap_expression.hasError(), wrap_expression.error().toString())
                actual_wrap_expression = QQmlExpression(context, content, "wrapMode")
                self._tooltip_references.append(actual_wrap_expression)
                actual_wrap = actual_wrap_expression.evaluate()
                if isinstance(actual_wrap, tuple):
                    actual_wrap = actual_wrap[0]
                self.assertFalse(actual_wrap_expression.hasError(),
                                 actual_wrap_expression.error().toString())
                self.assertEqual(int(actual_wrap), int(wrap_value))
                self.assert_text_has_shaped_cjk_glyphs(
                    tooltip_text, content.property("font"))

                popup_item = content.parentItem()
                self.assertIsInstance(popup_item, QQuickItem)
                self._tooltip_references.append(popup_item)
                self.assertTrue(popup_item.isVisible())
                for item in (popup_item, content):
                    origin = item.mapToScene(QPointF(0, 0))
                    self.assertGreaterEqual(origin.x(), -0.5)
                    self.assertGreaterEqual(origin.y(), -0.5)
                    self.assertLessEqual(origin.x() + item.width(), self.window.width() + 0.5)
                    self.assertLessEqual(origin.y() + item.height(), self.window.height() + 0.5)
                self.assertTrue(self.window.isVisible())
                self.screenshot(f"mask-viewport-zh-{theme_mode}-{scroll_mode}-960x640-"
                                 f"dpr{self.window.devicePixelRatio():g}.png")

    def test_native_pinch_repeated_begin_zoom_end_keeps_anchor(self):
        position = self.canvas.mapToScene(QPointF(210,170))
        global_position = self.window.mapToGlobal(position)
        before = self.view()
        expected_revision = int(self.window.property("viewportInteractionRevision"))
        world = (self.expression("mmX(210)"), self.expression("mmY(170)"))
        pinch = self.window.findChild(QObject, "maskViewportPinch")
        for _ in range(3):
            for kind, value in ((Qt.BeginNativeGesture,0), (Qt.ZoomNativeGesture,0.1),
                                (Qt.ZoomNativeGesture,-1/11), (Qt.EndNativeGesture,0)):
                event = QNativeGestureEvent(kind, self.touchpad, 2, position, position,
                                            global_position, value, QPointF())
                QCoreApplication.sendEvent(self.window, event)
                QTest.qWait(10)
                if kind == Qt.ZoomNativeGesture:
                    expected_revision += 1
                    self.assertEqual(int(self.window.property("viewportInteractionRevision")),
                                     expected_revision)
                if kind == Qt.ZoomNativeGesture and value > 0:
                    handler_state = {key: pinch.property(key) for key in ("active", "scale", "activeScale")}
                    self.assertGreater(self.view()[2], before[2],
                        f"native value={value}, before={before}, after={self.view()}, "
                        f"handler={handler_state}, dpr={self.window.devicePixelRatio()}")
                    self.assertAlmostEqual(self.expression("mmX(210)"), world[0], places=8)
                    self.assertAlmostEqual(self.expression("mmY(170)"), world[1], places=8)
            for actual, expected in zip(self.view(), before):
                self.assertAlmostEqual(actual, expected, places=7)
            self.assertEqual(int(self.window.property("viewportInteractionRevision")),
                             expected_revision)

    def test_viewport_wheel_keeps_toolbar_focus_and_is_consumed_once(self):
        pan_button = self.window.findChild(QQuickItem, "coatcontrol:pan_tool")
        self.assertIsNotNone(pan_button)
        self.click_item("coatcontrol:pan_tool")
        self.assertTrue(bool(pan_button.property("activeFocus")))
        before = self.view()
        revision = int(self.window.property("viewportInteractionRevision"))
        event = self.wheel(angle=(0,60), phase=Qt.NoScrollPhase, point=(210.,170.))
        self.assert_view_almost_equal((before[0],before[1]+20,before[2],before[3]))
        self.assertEqual(int(self.window.property("viewportInteractionRevision")), revision+1)
        self.assertTrue(bool(pan_button.property("activeFocus")))

    def test_middle_drag_and_pan_tool_do_not_edit_or_select_geometry(self):
        doc = self.bridge.documentJson
        for tool, button in ((1,Qt.MiddleButton),(3,Qt.LeftButton)):
            self.window.setProperty("toolIndex", tool)
            before = self.view()
            start_scene = self.canvas.mapToScene(QPointF(120,120))
            start = start_scene.toPoint()
            end = start + QPoint(42,33)
            QTest.mousePress(self.window, button, Qt.NoModifier, start)
            local_start = (self.window.property("lastMouseX"), self.window.property("lastMouseY"))
            QTest.mouseMove(self.window, end, 10)
            local_end = (self.window.property("lastMouseX"), self.window.property("lastMouseY"))
            QTest.mouseRelease(self.window, button, Qt.NoModifier, end)
            QTest.qWait(30)
            diagnostic = (f"tool={tool}, button={button}, start_scene={start_scene}, end_window={end}, "
                          f"local_start={local_start}, local_end={local_end}, before={before}, "
                          f"after={self.view()}, dpr={self.window.devicePixelRatio()}")
            self.assertAlmostEqual(self.view()[0], before[0]+42, msg=diagnostic)
            self.assertAlmostEqual(self.view()[1], before[1]+33, msg=diagnostic)
            self.assertEqual(self.bridge.documentJson, doc)
            self.assertFalse(self.window.property("hasAnalysisPoint"))

    def test_manual_view_survives_edits_resize_and_reset_reloads_fit(self):
        self.wheel(pixel=(130,-50), touchpad=True)
        self.wheel(angle=(0,120))
        manual = self.view()
        self.bridge.setValue("/description", '"preview edit"')
        self.window.resize(1100,700)
        QTest.qWait(40)
        self.assertEqual(self.view(), manual)
        self.bridge.setValue("/z_mm", "550")
        QTest.qWait(40)
        self.assertEqual(self.view(), manual)
        button = self.window.findChild(QQuickItem, "coatcontrol:fit_all")
        QTest.mouseClick(self.window, Qt.LeftButton, Qt.NoModifier,
                         button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint())
        QTest.qWait(40)
        self.assertTrue(self.window.property("autoFitView"))
        self.assert_geometry_visible()
        for _ in range(3):
            self.wheel(angle=(0,120))
            self.bridge.loadDocument(str(self.root/"mask.cmsk"))
            QTest.qWait(30)
            self.assertTrue(self.window.property("autoFitView"))
            self.assert_geometry_visible()

    def test_late_machine_load_refits_but_late_refresh_preserves_manual_view(self):
        self.wheel(pixel=(130,-50), touchpad=True)
        self.bridge.refreshRings()
        manual = self.view()
        QTest.qWait(30)
        self.assertEqual(self.view(), manual)
        self.bridge.loadMachine(str(self.root/"machine.cmac"))
        QTest.qWait(30)
        self.assertTrue(self.window.property("autoFitView"))
        self.assert_geometry_visible()
        # Manual input cancels an already queued automatic layout fit.
        self.expression("resetView(); panView(13,17)")
        manual = self.view()
        QTest.qWait(30)
        self.assertEqual(self.view(), manual)

    def test_new_or_imported_document_requests_fit_but_point_edit_does_not(self):
        requests = QSignalSpy(cast(MaskBridge, self.bridge).viewFitRequested)
        self.bridge.setValue("/outer/0", "[-81,-101]")
        self.assertEqual(requests.count(), 0)
        self.bridge.applyDocument(self.files["mask.cmsk"])
        self.assertEqual(requests.count(), 1)
        self.bridge.newDocument('{"z_mm": 500, "outer": [], "holes": []}')
        QTest.qWait(30)
        self.assertEqual(requests.count(), 2)
        self.assert_geometry_visible()  # Empty mask still fits the loaded rings.

    def click_item(self, object_name):
        item = self.window.findChild(QQuickItem, object_name)
        self.assertIsNotNone(item, object_name)
        position = item.mapToScene(QPointF(item.width()/2, item.height()/2)).toPoint()
        QTest.mouseClick(self.window, Qt.LeftButton, Qt.NoModifier, position)
        QTest.qWait(20)

    def click_world(self, x_mm, y_mm):
        x = float(self.expression(f"px({x_mm})"))
        y = float(self.expression(f"py({y_mm})"))
        position = self.canvas.mapToScene(QPointF(x, y)).toPoint()
        QTest.mouseClick(self.window, Qt.LeftButton, Qt.NoModifier, position)
        QTest.qWait(10)

    def test_vertex_hit_enables_safe_delete_for_outer_and_holes(self):
        delete_button = self.window.findChild(QQuickItem, "coatcontrol:delete_point")
        self.assertIsNotNone(delete_button)

        self.click_world(-80, -100)
        self.assertEqual(self.window.property("selectedPolygon"), "/outer")
        self.assertEqual(self.window.property("selectedPoint"), 0)
        self.assertTrue(delete_button.property("enabled"))
        self.screenshot("板设计器-已选中顶点.png")
        self.click_item("coatcontrol:delete_point")
        document = json.loads(self.bridge.documentJson)
        self.assertEqual(len(document["outer"]), 3)
        self.assertNotEqual(document["outer"][0], document["outer"][-1])
        self.assertTrue(self.bridge.dirty)
        self.assertTrue(self.bridge.canSave, self.bridge.validationMessage)

        # A hit still selects the vertex, while the last three vertices stay intact.
        self.click_world(80, -100)
        self.assertEqual(self.window.property("selectedPolygon"), "/outer")
        self.assertGreaterEqual(self.window.property("selectedPoint"), 0)
        self.assertFalse(delete_button.property("enabled"))
        self.click_item("coatcontrol:delete_point")
        self.assertEqual(json.loads(self.bridge.documentJson), document)

        self.click_world(-20, -20)
        self.assertEqual(self.window.property("selectedPolygon"), "/holes/0")
        self.assertEqual(self.window.property("selectedPoint"), 0)
        self.assertTrue(delete_button.property("enabled"))
        self.click_item("coatcontrol:delete_point")
        document = json.loads(self.bridge.documentJson)
        self.assertEqual(len(document["holes"][0]), 3)
        self.assertNotEqual(document["holes"][0][0], document["holes"][0][-1])
        self.assertTrue(self.bridge.canSave, self.bridge.validationMessage)

        self.bridge.saveDocument()
        self.assertFalse(self.bridge.dirty, self.bridge.status)
        self.assertEqual(json.loads((self.root / "mask.cmsk").read_text(encoding="utf-8")), document)

    def test_floating_tools_stay_inside_view_and_work_at_multiple_zooms(self):
        toolbar = self.window.findChild(QQuickItem, "maskViewportFloatingToolbar")
        self.assertIsNotNone(toolbar)
        viewport = self.canvas.parentItem()
        top_left = toolbar.mapToItem(viewport, QPointF(0, 0))
        self.assertGreaterEqual(top_left.x(), 0)
        self.assertGreaterEqual(top_left.y(), 0)
        self.assertLessEqual(top_left.x()+toolbar.width(), viewport.width())
        self.assertLessEqual(top_left.y()+toolbar.height(), viewport.height())
        names = (
            "coatcontrol:select_tool", "coatcontrol:draw_outer", "coatcontrol:draw_hole",
            "coatcontrol:pan_tool", "coatcontrol:line_mode", "coatcontrol:curve_mode",
            "coatcontrol:undo_point", "coatcontrol:delete_point", "coatcontrol:close_contour",
            "coatcontrol:fit_all",
        )
        for name in names:
            item = self.window.findChild(QQuickItem, name)
            self.assertIsNotNone(item, name)
            point = item.mapToItem(viewport, QPointF(0, 0))
            self.assertGreaterEqual(point.x(), top_left.x(), name)
            self.assertGreaterEqual(point.y(), top_left.y(), name)
            self.assertLessEqual(point.x()+item.width(), top_left.x()+toolbar.width(), name)
            self.assertLessEqual(point.y()+item.height(), top_left.y()+toolbar.height(), name)

        for zoom in (0.5, 2.0, 7.0):
            self.expression(f"autoFitView=false; zoomX={zoom}; zoomY={zoom}; panX=0; panY=0")
            for name, tool in (("coatcontrol:select_tool", 0),
                               ("coatcontrol:draw_outer", 1),
                               ("coatcontrol:draw_hole", 2),
                               ("coatcontrol:pan_tool", 3)):
                self.click_item(name)
                self.assertEqual(self.window.property("toolIndex"), tool, name)
            self.click_item("coatcontrol:line_mode")
            self.assertEqual(self.window.property("contourMode"), "line")
            self.click_item("coatcontrol:curve_mode")
            self.assertEqual(self.window.property("contourMode"), "curve")
            self.click_item("coatcontrol:fit_all")
            self.assertTrue(self.window.property("autoFitView"))

    def test_floating_toolbar_has_no_panel_and_blank_area_passes_pan_in_both_themes(self):
        toolbar = self.window.findChild(QQuickItem, "maskViewportFloatingToolbar")
        self.assertIsNotNone(toolbar)
        self.assertEqual(len(toolbar.childItems()), 1,
                         "the toolbar should contain only its button layout")
        self.window.autoFitView = False

        for mode in ("light", "dark"):
            self.theme.configure(mode=mode, theme_package_id="")
            QTest.qWait(30)
            self.assertEqual(self.window.property("themePaletteMode"), mode)
            self.assertEqual(len(toolbar.childItems()), 1)

            self.click_item("coatcontrol:select_tool")
            self.assertEqual(self.window.property("toolIndex"), 0)
            self.click_item("coatcontrol:pan_tool")
            self.assertEqual(self.window.property("toolIndex"), 3)
            self.screenshot(f"mask-toolbar-transparent-{mode}.png")

        button_names = (
            "coatcontrol:select_tool", "coatcontrol:draw_outer", "coatcontrol:draw_hole",
            "coatcontrol:pan_tool", "coatcontrol:fit_all", "coatcontrol:line_mode",
            "coatcontrol:curve_mode", "coatcontrol:undo_point", "coatcontrol:delete_point",
            "coatcontrol:close_contour",
        )
        buttons = [self.window.findChild(QQuickItem, name) for name in button_names]
        blank_point = QPointF(toolbar.width() - 8, toolbar.height() / 2)
        for button in buttons:
            self.assertIsNotNone(button)
            button_origin = button.mapToItem(toolbar, QPointF(0, 0))
            self.assertFalse(
                button_origin.x() <= blank_point.x() <= button_origin.x() + button.width()
                and button_origin.y() <= blank_point.y() <= button_origin.y() + button.height(),
                f"chosen blank-area point overlaps {button.objectName()}",
            )

        self.expression("autoFitView=false; toolIndex=3")
        start = toolbar.mapToScene(blank_point).toPoint()
        delta = QPoint(24, int(toolbar.height()) + 36)
        end = start + delta
        before = self.view()
        QTest.mousePress(self.window, Qt.LeftButton, Qt.NoModifier, start)
        QTest.mouseMove(self.window, end, 10)
        QTest.mouseRelease(self.window, Qt.LeftButton, Qt.NoModifier, end)
        QTest.qWait(30)
        self.assertAlmostEqual(self.view()[0], before[0] + delta.x())
        self.assertAlmostEqual(self.view()[1], before[1] + delta.y())
        self.assertFalse(self.bridge.dirty)

    def test_floating_toolbar_buttons_have_no_drop_shadow_or_text_outline(self):
        names = (
            "coatcontrol:select_tool", "coatcontrol:draw_outer", "coatcontrol:draw_hole",
            "coatcontrol:pan_tool", "coatcontrol:fit_all", "coatcontrol:viewport_scroll_mode",
            "coatcontrol:line_mode", "coatcontrol:curve_mode", "coatcontrol:undo_point",
            "coatcontrol:delete_point", "coatcontrol:close_contour",
        )
        for mode in ("light", "dark"):
            self.theme.configure(mode=mode, theme_package_id="")
            QTest.qWait(30)
            for name in names:
                button = self.window.findChild(QQuickItem, name)
                self.assertIsNotNone(button, name)
                self.assertIsNone(self.window.findChild(QQuickItem, name + "_shadow"), name)
                content = self.window.findChild(QQuickItem, name + "_content")
                self.assertIsNotNone(content, name)
                content_item = cast(QQuickItem, content)
                text_style = QQmlExpression(QQmlEngine.contextForObject(content_item), content_item,
                                           "style === Text.Normal")
                normal_style, _undefined = text_style.evaluate()
                self.assertFalse(text_style.hasError(), text_style.error().toString())
                self.assertTrue(normal_style, name)
                background = self.window.findChild(QQuickItem, name + "_background")
                self.assertIsNotNone(background, name)
            self.click_item("coatcontrol:pan_tool")
            self.assertEqual(self.window.property("toolIndex"), 3)
            self.click_item("coatcontrol:select_tool")
            self.assertEqual(self.window.property("toolIndex"), 0)
            self.screenshot(f"mask-toolbar-flat-{mode}.png")

    def test_draw_outer_line_and_curve_then_close_contours(self):
        self.bridge.setValue("/outer", "[]")
        self.expression("autoFitView=false; zoomX=2; zoomY=2; panX=0; panY=0")
        self.click_item("coatcontrol:line_mode")
        self.click_item("coatcontrol:draw_outer")
        self.assertEqual(self.window.property("toolIndex"), 1)
        for point in ((-30, -20), (30, -20), (0, 30)):
            self.click_world(*point)
        self.click_world(0, 0)
        self.assertEqual(self.expression("draftPoints.length"), 4)
        self.click_item("coatcontrol:undo_point")
        self.assertEqual(self.expression("draftPoints.length"), 3)
        self.click_item("coatcontrol:close_contour")
        line_points = json.loads(self.bridge.documentJson)["outer"]
        self.assertEqual(len(line_points), 3)
        self.assertNotEqual(line_points[0], line_points[-1])

        self.bridge.setValue("/outer", "[]")
        self.click_item("coatcontrol:curve_mode")
        self.click_item("coatcontrol:draw_outer")
        for point in ((-30, -20), (30, -20), (30, 20), (-30, 20)):
            self.click_world(*point)
        self.assertEqual(self.expression("draftPoints.length"), 4)
        self.assertEqual(self.expression("displayedDraft().length"), 20)
        controls = self.expression("JSON.parse(JSON.stringify(draftPoints))")
        self.click_item("coatcontrol:close_contour")
        curve_points = json.loads(self.bridge.documentJson)["outer"]
        self.assertEqual(len(curve_points), 20)
        self.assertEqual(curve_points[0], controls[0])
        self.assertNotEqual(curve_points[0], curve_points[-1])
        self.screenshot("mask-drawing-tools.png")

        self.click_item("coatcontrol:draw_hole")
        for point in ((-8, -8), (8, -8), (0, 8)):
            self.click_world(*point)
        self.click_item("coatcontrol:close_contour")
        self.assertEqual(len(json.loads(self.bridge.documentJson)["holes"]), 2)

    def test_rotate_explicitly_selected_hole_about_mm_reference_center(self):
        before = json.loads(self.bridge.documentJson)
        polygon_control = self.window.findChild(QQuickItem, "coatcontrol:rotation_polygon")
        angle_field = self.window.findChild(QQuickItem, "coatcontrol:rotation_angle_deg")
        self.assertIsNotNone(polygon_control)
        self.assertIsNotNone(angle_field)
        polygon_control.setProperty("currentIndex", 1)
        self.assertEqual(int(self.window.property("rotationContourIndex")), 1)

        # Exercise the actual ThemeField edit path: programmatically assigning
        # text skips textEdited and leaves numericValueEdited false.
        self.click_item("coatcontrol:rotation_angle_deg")
        QTest.keyClick(self.window, Qt.Key_A, Qt.ControlModifier)
        QTest.keyClick(self.window, Qt.Key_9)
        QTest.keyClick(self.window, Qt.Key_0)
        QTest.qWait(20)
        self.assertTrue(bool(angle_field.property("numericValueEdited")))
        self.assertEqual(str(angle_field.property("numericEditText")), "90")

        self.click_item("coatcontrol:rotate_polygon")
        after = json.loads(self.bridge.documentJson)
        self.assertEqual(after["outer"], before["outer"])
        self.assertEqual(after["holes"][0][0], [20.0, -20.0])
        self.assertEqual(after["holes"][0][1], [20.0, 20.0])
        self.screenshot("mask-rotated-hole.png")

    def test_ruler_ticks_are_world_mm_and_follow_pan_and_zoom(self):
        self.expression("autoFitView=false; zoomX=2; zoomY=2.5; panX=11; panY=-17")
        x_values = self.expression("rulerXValues()")
        y_values = self.expression("rulerYValues()")
        self.assertGreater(len(x_values), 2)
        self.assertGreater(len(y_values), 2)
        x_step = float(self.expression("rulerXStep()"))
        y_step = float(self.expression("rulerYStep()"))
        for value in x_values:
            self.assertAlmostEqual(value/x_step, round(value/x_step), places=7)
            self.assertAlmostEqual(self.expression(f"mmX(px({value}))"), value, places=8)
        for value in y_values:
            self.assertAlmostEqual(value/y_step, round(value/y_step), places=7)
            self.assertAlmostEqual(self.expression(f"mmY(py({value}))"), value, places=8)
        x_spacing = float(self.expression(f"px({x_values[1]})-px({x_values[0]})"))
        y_spacing = abs(float(self.expression(f"py({y_values[1]})-py({y_values[0]})")))
        self.assertGreaterEqual(x_spacing, 84)
        self.assertLessEqual(x_spacing, 210)
        self.assertGreaterEqual(y_spacing, 84)
        self.assertLessEqual(y_spacing, 210)
        self.expression("zoomX*=2")
        zoomed = self.expression("rulerXValues()")
        zoomed_spacing = float(self.expression(f"px({zoomed[1]})-px({zoomed[0]})"))
        self.assertNotEqual(zoomed_spacing, x_spacing)
        old_world = float(self.expression("mmX(200)"))
        self.expression("panX+=30")
        self.assertAlmostEqual(self.expression("mmX(200)"), old_world-5, places=8)
        self.assertNotEqual(x_values, self.expression("rulerXValues()"))

    def test_empty_degenerate_and_invalid_bounds_do_not_poison_transform(self):
        self.expression('doc=({outer:[],holes:[]}); machine=({sources:[],masks:[]}); rings=[]; resetView()')
        QTest.qWait(30)
        self.assertEqual(self.view(), (0.,0.,1.,1.))
        self.expression('doc=({outer:[[5000,-3000],[null,0],[Infinity,4]],holes:[]}); machine=({sources:[],masks:[]}); rings=[]; resetView()')
        QTest.qWait(30)
        self.assertAlmostEqual(self.expression("px(5000)"), self.canvas.width()/2)
        self.assertAlmostEqual(self.expression("py(-3000)"), self.expression("plotCenterY()"))
        self.assertEqual(self.expression("geometryBounds()"), [5000,-3000,5000,-3000])


if __name__ == "__main__":
    unittest.main()
