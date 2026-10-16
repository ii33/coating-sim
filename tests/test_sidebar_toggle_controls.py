"""Real-QML regressions for adjacent, vector-drawn sidebar toggle controls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import cast

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scale", ("1", "1.5"))
def test_sidebar_toggle_controls_at_two_dpi_profiles(tmp_path, scale):
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                       QT_QUICK_BACKEND="software", QT_QUICK_CONTROLS_STYLE="Fusion",
                       QT_SCALE_FACTOR=scale, COAT_SIDEBAR_QA_ROOT=str(tmp_path))
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--sidebar-probe"],
        cwd=ROOT, env=environment, text=True, capture_output=True,
        errors="replace", timeout=90, check=False)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["dpr"] == pytest.approx(float(scale), abs=.02)
    assert report["controls"] == ["film_design", "film_simulation", "uniformity",
                                  "raytrace_geometry", "raytrace_empty", "appearance"]
    assert report["selected_label_contrast"]
    assert min(report["selected_label_contrast"].values()) >= 4.5


def _sidebar_probe():
    sys.path.insert(0, str(ROOT))
    from tests import qt_test_guard
    from PySide6.QtCore import (QCoreApplication, QEvent, QObject, QPoint, QPointF,
                               QSettings, Qt, QUrl, Slot)
    from PySide6.QtGui import QAccessible, QFont
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
    from PySide6.QtQuick import QQuickItem, QQuickWindow
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from coat_sim.scheduler import SchedulerClient
    from coat_sim.core.api import CoatService
    from coat_sim.core.film_design_service import FilmDesignService
    from coat_sim.ui import main as main_module
    from coat_sim.ui.film_quick_controller import FilmDesignQuickController
    from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
    from coat_sim.ui.fonts import bundled_font_family
    from coat_sim.ui.raytrace import RayTraceWindowController
    from coat_sim.ui.theme_service import ThemeService
    from tests.test_appearance_compute_entrypoints import _write_synthetic_v7_asset

    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    app.setQuitOnLastWindowClosed(False)
    family = bundled_font_family()
    if family:
        app.setFont(QFont(family))
    folder = Path(os.environ["COAT_SIDEBAR_QA_ROOT"])
    theme = ThemeService.instance()
    theme.configure(mode="light", font_scale=1, density_scale=1)
    runtimes = []
    checked = []
    warnings: list[str] = []
    contrast_ratios = {}
    accessible_names = {
        "film_design": ("折叠当前侧栏功能（F4）", "展开当前侧栏功能（F4）"),
        "film_simulation": ("折叠当前侧栏", "展开当前侧栏"),
        "uniformity": ("折叠均匀性侧栏（F4）", "展开均匀性侧栏（F4）"),
        "raytrace_geometry": ("收起几何设置侧栏", "展开几何设置侧栏"),
        "raytrace_empty": ("收起外观工作流侧栏", "展开外观工作流侧栏"),
        "appearance": ("收起外观参数栏", "展开外观参数栏"),
    }

    class NoQueue:
        def __getattr__(self, name):
            raise AssertionError("Sidebar-only test accessed scheduler: " + name)

    class Launcher(QObject):
        @Slot(str, str)
        def openWindow(self, _module, _path):
            raise AssertionError("Sidebar-only test opened another window")

    launcher = Launcher()

    def flush():
        app.processEvents()
        QTest.qWait(45)
        # QTest.qWait does not consistently release Python's GIL. Give the
        # real local asset-validation worker time to deliver its Qt signal.
        time.sleep(.005)
        app.processEvents()

    def load(filename, controller, context_name=None):
        engine = QQmlApplicationEngine()
        factory = qt_test_guard.NoNetworkAccessManagerFactory()
        engine.setNetworkAccessManagerFactory(factory)
        engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
        theme.attach_engine(engine, "Theme")
        if context_name:
            engine.rootContext().setContextProperty(context_name, controller)
        engine.rootContext().setContextProperty("launcher", launcher)
        if filename == "MainWindow.qml":
            engine.addImageProvider("thickness", controller.image_provider)
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(
            ROOT / "coat_sim/ui/qml" / filename)))
        window = component.createWithInitialProperties(
            {"controller": controller} if filename == "FilmDesignQuick.qml" else {})
        assert isinstance(window, QQuickWindow), component.errorString()
        runtimes.append((engine, factory, component, window, controller))
        window.showNormal()
        window.requestActivate()
        flush()
        return window

    def find(window, name):
        item = window.findChild(QQuickItem, name)
        assert item is not None, name
        return item

    def rect(item):
        point = item.mapToScene(QPointF(0, 0))
        return (point.x(), point.y(), point.x() + item.width(), point.y() + item.height())

    def bounds(window, item):
        left, top, right, bottom = rect(item)
        assert item.isVisible() and item.width() > 0 and item.height() > 0, item.objectName()
        assert -.5 <= left < right <= window.width() + .5, (item.objectName(), rect(item))
        assert -.5 <= top < bottom <= window.height() + .5, (item.objectName(), rect(item))

    def click(window, item):
        bounds(window, item)
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(round(point.x()), round(point.y())))
        flush()

    def name(item):
        interface = QAccessible.queryAccessibleInterface(item)
        assert interface is not None
        return interface.text(QAccessible.Text.Name)

    def glyph(button, direction):
        content = button.findChild(QQuickItem, button.objectName() + "Glyph")
        assert isinstance(content, QQuickItem), button.objectName()
        assert button.property("direction") == direction, (button.objectName(), direction)
        assert "Shape" in content.metaObject().className(), "Double arrows must be vector Shapes"
        paths = [item for item in content.findChildren(QObject)
                 if "ShapePath" in item.metaObject().className()]
        assert len(paths) == 2, "Both chevrons must be independent vector paths"
        assert content.width() >= 14 and content.height() >= 14, (
            button.objectName(), content.width(), content.height())
        return content

    def adjacent(window, button, label, horizontal=False):
        bounds(window, button)
        bounds(window, label)
        button_rect, label_rect = rect(button), rect(label)
        gap = (button_rect[0] - label_rect[2] if horizontal
               else button_rect[1] - label_rect[3])
        assert -.5 <= gap <= float(theme.current_snapshot["spaceSm"]) + 1, (
            button.objectName(), "label gap", gap)
        if horizontal:
            assert max(button_rect[1], label_rect[1]) < min(
                button_rect[3], label_rect[3]), "Toggle left the label row"
        else:
            assert max(button_rect[0], label_rect[0]) < min(
                button_rect[2], label_rect[2]), "Toggle left the label rail"

    def snapshot(window, stem):
        image = window.grabWindow()
        assert not image.isNull()
        assert image.save(str(folder / (stem + ".png")))

    def selected_contrast(window, key, mode, width):
        groups = {
            "film_design": ("filmDesignSidebarExpression", "filmDesignSidebarGenerator",
                            "filmDesignSidebarLibrary"),
            "film_simulation": ("filmSimulationStatisticsToggle", "filmSimulationDiagnosticsButton"),
            "uniformity": ("uniformitySidebarButton",),
        }
        buttons = [find(window, item_name) for item_name in groups[key]]
        selected_property = "selected" if key == "uniformity" else "checked"
        previous = next((button for button in buttons if button.property(selected_property)), None)

        def luminance(color):
            def linear(channel):
                return channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
            return sum(weight * linear(channel) for weight, channel in zip(
                (.2126, .7152, .0722), (color.redF(), color.greenF(), color.blueF()), strict=True))

        for button in buttons:
            if not button.property(selected_property):
                click(window, button)
            assert button.property(selected_property), button.objectName()
            foreground = button.property("contentItem").property("color")
            background = button.property("background").property("color")
            assert foreground.name().lower() == str(theme.current_snapshot["colorTextOnAccent"]).lower()
            assert background.name().lower() == str(theme.current_snapshot["colorAccent"]).lower()
            light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
            ratio = (light + .05) / (dark + .05)
            assert ratio >= 4.5, (button.objectName(), mode, ratio)
            contrast_ratios[f"{key}/{button.objectName()}/{mode}"] = round(ratio, 3)
            snapshot(window, f"{key}_{width}_{mode}_{button.objectName()}_selected")
        if previous is not None and not previous.property(selected_property):
            click(window, previous)

    def rail_case(window, state, toggle_name, label_name, key, sizes, *, bottom=False,
                  selected=None):
        for width, height in sizes:
            window.resize(width, height)
            flush()
            for mode in ("light", "high-contrast-dark"):
                theme.set_theme(mode)
                flush()
                window.setProperty(state, True)
                flush()
                selected_contrast(window, key, mode, width)
                button, label = find(window, toggle_name), find(window, label_name)
                selected_before = window.property(selected) if selected else None
                horizontal = bottom and bool(window.property("sidebarUsesBottomLayout"))
                expanded_direction = "down" if horizontal else "right"
                collapsed_direction = "up" if horizontal else "left"
                adjacent(window, button, label, horizontal)
                if not horizontal:
                    trailing = [item for item in button.parentItem().childItems()
                                if item is not button and item.isVisible()
                                and item.y() >= button.y() + button.height() - .5]
                    assert trailing, "The rail stretch must follow the toggle"
                elif bottom:
                    assert not find(window, "filmDesignSidebarSpacer").isVisible()
                QTest.mouseMove(window, QPoint(0, 0))
                flush()
                glyph(button, expanded_direction)
                assert button.property("glyphColor").name().lower() == str(
                    theme.current_snapshot["colorText"]).lower()
                button.setEnabled(False)
                flush()
                assert button.property("glyphColor").name().lower() == str(
                    theme.current_snapshot["colorDisabledText"]).lower()
                button.setEnabled(True)
                flush()
                original_name = name(button)
                assert original_name == accessible_names[key][0]
                # Native input must still drive the existing state owner.
                click(window, button)
                assert not window.property(state), key
                adjacent(window, button, label, horizontal)
                glyph(button, collapsed_direction)
                assert name(button) == accessible_names[key][1]
                button.forceActiveFocus()
                window.requestActivate()
                flush()
                QTest.keyClick(window, Qt.Key.Key_F4)
                flush()
                assert window.property(state), key + " F4"
                glyph(button, expanded_direction)
                if selected:
                    assert window.property(selected) == selected_before
                snapshot(window, f"{key}_{width}_{mode}_expanded")
                click(window, button)
                snapshot(window, f"{key}_{width}_{mode}_collapsed")
                # Repeated mouse expansion uses the exact same visible hit area.
                click(window, button)
                assert window.property(state)
        checked.append(key)

    def title_label(button, title):
        result = next((item for item in button.parentItem().childItems()
                       if item.property("text") == title), None)
        assert result is not None, title
        return result

    def header_case(window, owner, state, toggle_name, title, key, *, collapsed_name=None):
        for width, height in ((1160, 820), (720, 520)):
            window.resize(width, height)
            flush()
            for mode in ("light", "high-contrast-dark"):
                theme.set_theme(mode)
                owner.setProperty(state, True)
                flush()
                button = find(window, toggle_name)
                label = title_label(button, title)
                adjacent(window, button, label, True)
                glyph(button, "right")
                expanded_name = name(button)
                assert expanded_name == accessible_names[key][0]
                click(window, button)
                assert not owner.property(state), key
                collapsed_button = find(window, collapsed_name or toggle_name)
                bounds(window, collapsed_button)
                glyph(collapsed_button, "left")
                assert name(collapsed_button) == accessible_names[key][1]
                snapshot(window, f"{key}_{width}_{mode}_collapsed")
                click(window, collapsed_button)
                assert owner.property(state), key
                button.forceActiveFocus()
                window.requestActivate()
                flush()
                QTest.keyClick(window, Qt.Key.Key_F4)
                flush()
                assert not owner.property(state), key + " F4"
                QTest.keyClick(window, Qt.Key.Key_F4)
                flush()
                assert owner.property(state), key + " second F4"
                adjacent(window, button, label, True)
                snapshot(window, f"{key}_{width}_{mode}_expanded")
        checked.append(key)

    try:
        design_root = folder / "design"
        design_root.mkdir()
        design_service = FilmDesignService(design_root, restrict_to_root=False)
        design = FilmDesignQuickController(
            design_root, service=design_service,
            geometry_settings=QSettings(str(folder / "design.ini"), QSettings.Format.IniFormat))
        window = load("FilmDesignQuick.qml", design)
        rail_case(window, "sidebarExpanded", "filmDesignSidebarToggle", "filmDesignSidebarLibrary",
                  "film_design", ((1320, 820), (720, 560)), bottom=True,
                  selected="activeSidebarSection")
        window.hide()

        simulation = FilmSimulationQuickController(root=design_root, service=design_service,
                                                   design=design_service.new_design())
        window = load("FilmSimulationQuick.qml", simulation, "FilmSimulation")
        rail_case(window, "sidebarExpanded", "filmSimulationDiagnosticsToggle",
                  "filmSimulationDiagnosticsButton", "film_simulation", ((1100, 760), (720, 560)),
                  selected="activeSidebarSection")
        window.hide()

        def isolated_data_path(value: str, *, root_allowed: bool = True) -> Path:
            return folder / "main_data" / value

        def isolated_design_directory(kind: str) -> Path:
            return folder / "main_designs" / kind

        main_module.data_path = isolated_data_path
        main_module.design_directory = isolated_design_directory
        main = main_module.MainBridge(CoatService(root=folder / "main", restrict_to_root=False))
        window = load("MainWindow.qml", main, "bridge")
        rail_case(window, "uniformitySidebarExpanded", "uniformitySidebarToggle",
                  "uniformitySidebarButton", "uniformity", ((1280, 760), (1080, 680)))
        window.hide()

        raytrace = RayTraceWindowController(root=folder / "raytrace", queue_client=cast(SchedulerClient, NoQueue()))
        window = load("RayTraceWindow.qml", raytrace, "bridge")
        raytrace.setWorkspaceMode("geometric")
        flush()
        header_case(window, window, "sidebarExpanded", "rayTraceSidebarToggle", "几何输入",
                    "raytrace_geometry")
        raytrace.setWorkspaceMode("appearance")
        flush()
        header_case(window, window, "appearanceSidebarExpanded", "rayTraceAppearanceSidebarToggle",
                    "外观设置", "raytrace_empty")
        asset = _write_synthetic_v7_asset(raytrace.root)
        raytrace.loadLayerAsset(str(asset))
        deadline = time.monotonic() + 10
        while raytrace.geometricBusy and time.monotonic() < deadline:
            flush()
        assert not raytrace.geometricBusy, "Synthetic asset validation did not finish"
        assert raytrace.appearanceController is not None and not raytrace.error, (
            raytrace.error, raytrace.status)
        workspace = find(window, "rayTraceAppearanceWorkspace")
        header_case(window, workspace, "parametersExpanded", "appearanceParametersCollapseButton",
                    "外观设置", "appearance", collapsed_name="appearanceParametersExpandButton")
        assert not any("ThemeDoubleChevron.qml" in warning for warning in warnings), warnings
        print(json.dumps({"dpr": float(window.devicePixelRatio()), "controls": checked,
                          "selected_label_contrast": contrast_ratios}))
    finally:
        for engine, _factory, _component, window, controller in reversed(runtimes):
            if hasattr(controller, "close"):
                controller.close()
            if hasattr(controller, "detach_observers"):
                controller.detach_observers()
            window.setProperty("allowClose", True)
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


if __name__ == "__main__" and "--sidebar-probe" in sys.argv:
    _sidebar_probe()
