"""Isolated Qt coverage for the compact process editor and its optimization sidebar."""

from __future__ import annotations

import os
import importlib
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import patch

try:
    from tests.qt_ui_test_bootstrap import QA_ROOT, ROOT
except ModuleNotFoundError as error:
    if error.name not in {"tests", "tests.qt_ui_test_bootstrap"}:
        raise
    bootstrap = importlib.import_module("qt_ui_test_bootstrap")
    QA_ROOT = Path(vars(bootstrap)["QA_ROOT"])
    ROOT = Path(vars(bootstrap)["ROOT"])

from tests import qt_test_guard

from PySide6.QtCore import (QCoreApplication, QEvent, QObject, QPoint, QPointF,
                            Property, QUrl, Qt, Signal, Slot)
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from shiboken6 import isValid

from coat_sim.core.api import CoatService
from coat_sim.ui.process import ProcessBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot


class _RecordingLauncher(QObject):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    @Slot(str, str, result=str)
    def openWindow(self, module: str, document_path: str) -> str:
        self.calls.append((module, document_path))
        return "process-sidebar-test-window"



class _ThemeStub(QObject):
    snapshotChanged = Signal()
    modeChanged = Signal()

    def __init__(self):
        super().__init__()
        self._mode = "light"

    @Property("QVariantMap", notify=snapshotChanged)
    def snapshot(self):
        return build_theme_snapshot(self._mode)

    @Property(str, notify=modeChanged)
    def mode(self):
        return self._mode

    @Property("QVariantMap", constant=True)
    def assets(self):
        return {}

    def setMode(self, mode: str) -> None:
        if mode != self._mode:
            self._mode = mode
            self.modeChanged.emit()
            self.snapshotChanged.emit()


class ProcessCompactSidebarTests(unittest.TestCase):
    app: ClassVar[QGuiApplication]

    @classmethod
    def setUpClass(cls):
        QQuickStyle.setStyle("Fusion")
        existing_app = QGuiApplication.instance()
        cls.app = QGuiApplication([]) if existing_app is None else cast(QGuiApplication, existing_app)
        font_path = ROOT / "coat_sim/ui/assets/fonts/noto-cjk/NotoSansCJKsc-Regular.otf"
        if font_path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            if font_id >= 0:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    cls.app.setFont(QFont(families[0]))

    def setUp(self):
        self.qa_root = QA_ROOT
        self.temporary = tempfile.TemporaryDirectory(
            prefix="process-compact-sidebar-", dir=self.qa_root)
        self.root = Path(self.temporary.name)
        self.designs = self.root / "designs"
        self.designs.mkdir()
        self.service = CoatService(root=self.root)
        self.paths = self._write_designs()
        self.launcher = _RecordingLauncher()
        self.theme = _ThemeStub()
        self.engines = []
        self.components = []
        self.windows = []
        self.bridges = []
        self.path_patches = [
            patch(f"coat_sim.ui.{module}.design_directory",
                  side_effect=lambda kind: self.designs / kind)
            for module in ("process",)
        ]
        for patcher in self.path_patches:
            patcher.start()

    def tearDown(self):
        for window in self.windows:
            if isValid(window):
                window.close()
                window.deleteLater()
        self.components.clear()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for engine in self.engines:
            if isValid(engine):
                engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.bridges.clear()
        for patcher in reversed(self.path_patches):
            patcher.stop()
        self.temporary.cleanup()

    def _write_designs(self):
        source = {
            "schema_version": 2, "kind": "point",
            "device_type": "electron_beam", "source_exponent": 1.0,
        }
        substrate = {
            "schema_version": 1, "name": "Sidebar substrate",
            "outline": {"kind": "circle", "radius_mm": 10.0},
            "surface": {"kind": "flat"},
        }
        mask = {
            "z_mm": 500.0,
            "outer": [[-80.0, -100.0], [80.0, -100.0],
                      [80.0, 100.0], [-80.0, 100.0]],
            "holes": [[[-20.0, -20.0], [20.0, -20.0],
                       [20.0, 20.0], [-20.0, 20.0]]],
        }
        machine = {
            "schema_version": 5, "carrier_x_mm": 260.0,
            "carrier_y_mm": 40.0, "planet_offset_mm": 312.0,
            "planet_tilt_deg": 0.0, "substrate_z_mm": 1000.0,
            "sun_teeth": 120, "planet_teeth": 36, "gear_module_mm": 4.0,
            "sources": [{"file": "source.csrc", "x_mm": -230.0,
                         "y_mm": 150.0, "z_mm": 10.0}],
            "masks": [{"file": "mask.cmsk"}],
            "monitors": [{"name": "Sidebar monitor", "kind": "crystal",
                          "x_mm": 50.0, "y_mm": 80.0, "z_mm": 700.0}],
        }
        process = {
            "schema_version": 6, "name": "Sidebar process",
            "machine_file": "machine.cmac", "substrate_file": "substrate.csub",
            "steps": [{"source_slot": 1, "target_nm": 100.0,
                       "rate_nm_s": 1.0, "target_mode": "mean"}],
        }
        paths = {
            "source": self.designs / "source.csrc",
            "substrate": self.designs / "substrate.csub",
            "mask": self.designs / "mask.cmsk",
            "machine": self.designs / "machine.cmac",
            "process": self.designs / "process.cproc",
        }
        for kind, path, data in (
            ("source", paths["source"], source),
            ("substrate", paths["substrate"], substrate),
            ("mask", paths["mask"], mask),
            ("machine", paths["machine"], machine),
            ("process", paths["process"], process),
        ):
            self.service.save(kind, path, data)
        return paths

    def _load_process_window(self):
        bridge = ProcessBridge(self.service, str(self.paths["process"]))
        self.assertTrue(bridge.valid, bridge.errors)
        engine = QQmlEngine()
        engine.rootContext().setContextProperty("bridge", bridge)
        engine.rootContext().setContextProperty("launcher", self.launcher)
        engine.rootContext().setContextProperty("Theme", self.theme)
        if hasattr(bridge, "image_provider"):
            engine.addImageProvider("thickness", bridge.image_provider)
        network_factory = qt_test_guard.NoNetworkAccessManagerFactory()
        engine.setNetworkAccessManagerFactory(network_factory)
        self.engines.append(engine)
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/ProcessWindow.qml")))
        self.components.append(component)
        window = component.createWithInitialProperties({"initialMaximized": False})
        if window is None:
            errors = "\n".join(error.toString() for error in component.errors())
            self.fail(f"ProcessWindow.qml failed to load:\n{errors}")
        assert isinstance(window, QQuickWindow)
        window.resize(1280, 900)
        window.showNormal()
        self.bridges.append(bridge)
        self.windows.append(window)
        self.network_factory = network_factory
        self._settle()
        return window, bridge

    def _settle(self):
        self.app.processEvents()
        QTest.qWait(25)
        self.app.processEvents()

    def _quick_items(self, window):
        pending = [window.contentItem()]
        while pending:
            item = pending.pop()
            yield item
            pending.extend(item.childItems())

    def _item(self, window, object_name):
        direct = window.findChild(QQuickItem, object_name)
        if direct is not None:
            return direct
        return next((item for item in self._quick_items(window)
                     if item.property("objectName") == object_name), None)

    def _click(self, window, item_or_name):
        item = (self._item(window, item_or_name)
                if isinstance(item_or_name, str) else item_or_name)
        self.assertIsNotNone(item, f"missing QML item {item_or_name}")
        self.assertTrue(item.isVisible(), f"{item_or_name} is not visible")
        self.assertTrue(item.isEnabled(), f"{item_or_name} is disabled")
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier,
                         QPoint(round(point.x()), round(point.y())))
        self._settle()
        return item

    def _header(self, window, title):
        return next((item for item in self._quick_items(window)
                     if item.property("objectName") == "themeSectionHeader"
                     and item.property("text") == title), None)

    def _scroll_item_into_view(self, window, item):
        ancestor = item.parentItem()
        while ancestor is not None:
            content_y = ancestor.property("contentY")
            content_height = ancestor.property("contentHeight")
            viewport_height = ancestor.property("height")
            if (content_y is not None and content_height is not None
                    and viewport_height is not None
                    and float(content_height) > float(viewport_height)):
                item_y = item.mapToItem(ancestor, QPointF(0, 0)).y()
                target_y = (float(content_y) + item_y
                            - max(0.0, (float(viewport_height) - item.height()) / 2))
                max_y = max(0.0, float(content_height) - float(viewport_height))
                ancestor.setProperty("contentY", max(0.0, min(target_y, max_y)))
                self._settle()
                return
            ancestor = ancestor.parentItem()

    def _scrollable_ancestor(self, item):
        ancestor = item.parentItem()
        while ancestor is not None:
            content_height = ancestor.property("contentHeight")
            viewport_height = ancestor.property("height")
            content_y = ancestor.property("contentY")
            if (content_height is not None and viewport_height is not None
                    and content_y is not None
                    and float(content_height) > float(viewport_height)):
                return ancestor
            ancestor = ancestor.parentItem()
        return None

    def _save_render(self, window, name):
        image = window.grabWindow()
        self.assertFalse(image.isNull(), f"empty render for {name}")
        path = self.qa_root / name
        self.assertTrue(image.save(str(path)), f"could not save {path}")
        return path

    def _assert_within(self, item, container, *, label):
        point = item.mapToItem(container, QPointF(0, 0))
        self.assertGreaterEqual(point.x(), -1.0, f"{label} overflows left")
        self.assertLessEqual(point.x() + item.width(), container.width() + 1.0,
                             f"{label} overflows right")

    def test_compact_process_has_no_mask_optimizer_sidebar_and_keeps_width(self):
        window, bridge = self._load_process_window()
        scale = os.environ.get("QT_SCALE_FACTOR", "1")
        scale_tag = scale.replace(".", "p")
        actions = self._item(window, "processActions")
        workspace = self._item(window, "processWorkspace")
        scroll = self._item(window, "processEditorScroll")
        self.assertIsNotNone(actions)
        self.assertIsNotNone(workspace)
        self.assertIsNotNone(scroll)
        from PySide6.QtGui import QAccessible
        accessible = QAccessible.queryAccessibleInterface(actions)
        self.assertIsNotNone(accessible)
        self.assertEqual(accessible.text(QAccessible.Text.Name), "操作栏")
        self.assertIsNone(self._item(window, "operationBarLabel"),
                          "the action bar is named for accessibility without a visible label")
        self.assertIsNone(self._item(window, "processOptimizationSidebar"))
        self.assertIsNone(self._item(window, "optimizationPanelToggle"))
        self.assertIsNone(self._item(window, "startOptimizationButton"))
        self.assertIsNone(self._item(window, "assetBundleDialog"))
        self.assertAlmostEqual(float(scroll.width()), float(workspace.width()), delta=1.0,
                               msg="removing the optimizer must not leave a reserved sidebar")
        self.assertAlmostEqual(float(workspace.y()),
                               float(actions.y() + actions.height() + 8.0), delta=1.0)
        self.assertGreater(float(scroll.width()), 900.0)
        self._save_render(window, f"process-wide-{scale_tag}-light.png")

        self.theme.setMode("dark")
        self._settle()
        self._save_render(window, f"process-wide-{scale_tag}-dark.png")
        window.resize(720, 560)
        self._settle()
        self.assertEqual(int(window.width()), 720)
        self.assertAlmostEqual(float(scroll.width()), float(workspace.width()), delta=1.0)
        self.assertGreater(float(scroll.width()), 650.0,
                           "the narrow process editor should use the full workspace width")
        self.assertGreater(float(scroll.property("contentHeight")), float(scroll.height()))
        self.assertTrue(self._item(window, "sendToSimulationButton").isVisible())
        self._save_render(window, f"process-narrow-{scale_tag}-dark.png")
        self.theme.setMode("light")
        self._settle()
        self._save_render(window, f"process-narrow-{scale_tag}-light.png")
        self.assertEqual(self.network_factory.blocked_urls, [])

        # The existing explicit save-before-send flow remains intact after the
        # optimization controls move to MaskWindow.
        bridge.setField("name", "Saved process without sidebar")
        self._settle()
        self.assertTrue(bridge.dirty)
        self._click(window, "sendToSimulationButton")
        confirmation = window.findChild(QObject, "saveBeforeSendDialog")
        self.assertIsNotNone(confirmation)
        self.assertTrue(confirmation.property("visible"))
        self.assertEqual(self.launcher.calls, [])
        self._click(window, "cancelSendToSimulationButton")
        self.assertTrue(bridge.dirty)
        self._click(window, "sendToSimulationButton")
        self._click(window, "saveAndSendToSimulationButton")
        self.assertFalse(bridge.dirty)
        self.assertEqual(self.service.load("process", self.paths["process"]).data["name"],
                         "Saved process without sidebar")
        self.assertEqual(self.launcher.calls[-1],
                         ("main", str(self.paths["process"].resolve())))
        if float(scale) > 1.0:
            screen = self.app.primaryScreen()
            self.assertGreater(screen.devicePixelRatio() if screen else 1.0, 1.0)


if __name__ == "__main__":
    unittest.main()
