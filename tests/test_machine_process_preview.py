"""Rendered Qt previews use original geometry, labels and fixed-frame mm units.

All fixture files are temporary. Set COAT_PREVIEW_SCREENSHOT_DIR to retain
actual Qt Quick window renders (offscreen renders are not desktop captures).
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtGui import QColor
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression, QQmlEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from coat_sim.core.api import CoatService
from coat_sim.ui.machine_mask import MachineBridge, create_editor
from coat_sim.ui.process import ProcessBridge
from coat_sim.ui.theme_service import ThemeService
from coat_sim.core.project_default_loading import load_project_default
from tests.test_mask_optimization_ui import (
    _bound_project,
    _click,
    _item,
    _load_project_window,
    _scroll_into_view,
    _type_into_quick,
)
from tests.test_project_core import setup_project
from tests.test_project_module_selector import Navigation

ROOT = Path(__file__).resolve().parents[1]


class MachineProcessPreviewTests(unittest.TestCase):
    app: QCoreApplication

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = CoatService(root=self.root)
        self.service.save("source", self.root / "source.csrc", {
            "schema_version": 2, "kind": "point",
            "device_type": "electron_beam", "source_exponent": 1.0})
        self.service.save("substrate", self.root / "substrate.csub", {
            "schema_version": 1, "name": "测试基片", "outline": {"kind": "circle", "radius_mm": 10.0},
            "surface": {"kind": "flat"}})
        self.mask = {"z_mm": 500., "outer": [[-120., -100.], [80., -100.], [80., 100.], [-120., 100.]],
                     "holes": [[[-40., -30.], [20., -30.], [20., 30.], [-40., 30.]]]}
        self.service.save("mask", self.root / "mask.cmsk", self.mask)
        self.machine = {"schema_version": 5, "carrier_x_mm": 260., "carrier_y_mm": 40.,
            "planet_offset_mm": 312., "planet_tilt_deg": 0., "substrate_z_mm": 1000.,
            "sun_teeth": 120, "planet_teeth": 36, "gear_module_mm": 4.,
            "sources": [{"file": "source.csrc", "x_mm": -230., "y_mm": 150., "z_mm": 10.},
                        {"file": "source.csrc", "x_mm": 460., "y_mm": -290., "z_mm": 35.}],
            "masks": [{"file": "mask.cmsk"}],
            "monitors": [{"name": "晶控探头 A", "kind": "crystal", "x_mm": 370., "y_mm": 260., "z_mm": 660.},
                         {"name": "光控探头 B", "kind": "optical", "x_mm": -210., "y_mm": -220., "z_mm": 800.}]}
        self.service.save("machine", self.root / "machine.cmac", self.machine)
        self.process = {"schema_version": 6, "name": "预览验证工艺", "machine_file": "machine.cmac",
                        "substrate_file": "substrate.csub", "steps": [{"source_slot": 1, "target_nm": 100.,
                        "rate_nm_s": 1., "target_mode": "monitor", "monitor_slot": 1}]}
        self.service.save("process", self.root / "process.cproc", self.process)
        self.directory_patches = [patch(f"coat_sim.ui.{module}.design_directory", return_value=self.root)
                                  for module in ("machine_mask", "process")]
        for patcher in self.directory_patches:
            patcher.start()
        self.engines = []
        self.windows = []
        self.bridges = []
        self.components = []

    def tearDown(self):
        for window in self.windows:
            if isValid(window):
                window.close()
                window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for window in self.windows:
            self.assertFalse(isValid(window), "QML root must be destroyed before its engine")
        for engine in self.engines:
            engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.bridges.clear()
        for patcher in self.directory_patches:
            patcher.stop()
        self.temporary.cleanup()

    def expression(self, target, text):
        expression = QQmlExpression(QQmlEngine.contextForObject(target), target, text)
        value, _ = expression.evaluate()
        self.assertFalse(expression.hasError(), expression.error().toString())
        return value.toVariant() if hasattr(value, "toVariant") else value

    def visual_item(self, window, name):
        pending = [window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName() == name:
                return item
            pending.extend(item.childItems())
        return None

    def assert_fit(self, canvas):
        bounds = self.expression(canvas, "geometryBounds()")
        self.assertGreater(canvas.width(), 200)
        self.assertGreater(canvas.height(), 100)
        for x, y in ((bounds[0], bounds[1]), (bounds[2], bounds[3])):
            px, py = self.expression(canvas, f"pixelX({x})"), self.expression(canvas, f"pixelY({y})")
            self.assertGreaterEqual(px, 33.99)
            self.assertLessEqual(px, canvas.width() - 33.99)
            self.assertGreaterEqual(py, 33.99)
            self.assertLessEqual(py, canvas.height() - 33.99)

    def screenshot(self, window, filename):
        QTest.qWait(80)
        image = window.grabWindow()
        self.assertFalse(image.isNull())
        output = os.environ.get("COAT_PREVIEW_SCREENSHOT_DIR")
        if output:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            self.assertTrue(image.save(str(directory / filename)))
        return image

    def test_machine_bridge_exposes_exact_outlines_holes_and_dependency_refresh(self):
        bridge = MachineBridge(self.service)
        bridge.loadDocument(str(self.root / "machine.cmac"))
        self.assertEqual(json.loads(bridge.maskPlanesJson), [])
        bridge.preparePreview()
        plane = json.loads(bridge.maskPlanesJson)[0]
        self.assertEqual(plane["outer"], self.mask["outer"])
        self.assertEqual(plane["holes"], self.mask["holes"])
        self.assertEqual(plane["z_mm"], 500.)
        self.assertEqual(plane["display_slot"], 1)
        replacement = {"z_mm": 540., "outer": [[900., 0.], [1000., 0.], [1000., 100.]], "holes": []}
        self.service.save("mask", self.root / "other.cmsk", replacement)
        bridge.setDependencyFile("/masks/0/file", str(self.root / "other.cmsk"))
        self.assertEqual(json.loads(bridge.maskPlanesJson)[0]["outer"], replacement["outer"])
        bridge.setValue("/masks/0/file", json.dumps("missing.json"))
        self.assertIn("error", json.loads(bridge.maskPlanesJson)[0])
        self.assertNotIn("outer", json.loads(bridge.maskPlanesJson)[0])

    def test_machine_render_fits_offset_orbit_masks_and_repeated_resize(self):
        engine, bridge = create_editor("machine", self.service)
        self.bridges.append(bridge)
        self.engines.append(engine)
        window = engine.rootObjects()[0]
        self.windows.append(window)
        bridge.loadDocument(str(self.root / "machine.cmac"))
        canvas = window.findChild(QQuickItem, "machinePlanPreview")
        self.assertIsNotNone(canvas)
        window.showNormal()
        deadline = time.monotonic() + 5
        while self.expression(canvas, "plan.masks.length") == 0 and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertEqual(self.expression(canvas, "plan.masks.length"), 1)
        for width, height in ((1280, 900), (920, 620), (1440, 1000)):
            window.resize(width, height)
            QTest.qWait(60)
            self.assert_fit(canvas)
            self.assertEqual(self.expression(canvas, "plan.masks[0].holes.length"), 1)
        window.resize(1280, 900)
        image = self.screenshot(window, "machine-outline-preview.png")
        # Verify that the actual Canvas painted the board outline color.
        mask_color = self.expression(canvas, "maskColors[0]")
        mask_color_name = (mask_color.name() if isinstance(mask_color, QColor)
                           else QColor(mask_color).name())
        mask_pixels = sum(1 for y in range(image.height()) for x in range(image.width())
                          if image.pixelColor(x, y).name() == mask_color_name)
        self.assertGreater(mask_pixels, 20)
        bridge.removeItem("/masks", 0)
        QTest.qWait(40)
        self.assertEqual(self.expression(canvas, "plan.masks.length"), 0)
        self.assert_fit(canvas)

    def test_process_renders_metadata_monitors_and_editable_busy_parameters(self):
        bridge = ProcessBridge(self.service, str(self.root / "process.cproc"))
        self.bridges.append(bridge)
        preview = bridge.sourcePreview
        self.assertNotIn("outer", preview["masks"][0])
        bridge.prepareSourcePreview()
        preview = bridge.sourcePreview
        self.assertEqual(preview["masks"][0]["outer"], self.mask["outer"])
        self.assertEqual(preview["masks"][0]["holes"], self.mask["holes"])
        self.assertEqual(preview["monitors"][0]["name"], "晶控探头 A")
        self.assertEqual(preview["monitors"][1]["slot"], 2)
        engine = QQmlApplicationEngine()
        self.engines.append(engine)
        ThemeService.instance().attach_engine(engine)
        engine.rootContext().setContextProperty("bridge", bridge)
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/ProcessWindow.qml")))
        self.assertFalse(component.isError(), component.errors())
        self.components.append(component)
        window = component.create()
        self.assertIsNotNone(window, component.errors())
        self.windows.append(window)
        window.showNormal()
        canvas = window.findChild(QQuickItem, "processPlanPreview")
        for width, height in ((1280, 900), (1060, 620)):
            window.resize(width, height)
            QTest.qWait(60)
            self.assert_fit(canvas)
        for slot, expected in ((1, "晶控探头 A"), (2, "光控探头 B")):
            label = self.visual_item(window, f"previewMonitorLabel{slot}")
            self.assertIsNotNone(label)
            self.assertIn(expected, label.property("text"))
            self.assertIn(f"监测点 {slot}", label.property("text"))
            position = self.visual_item(window, f"previewMonitorPosition{slot}").property("text")
            self.assertIn("mm", position)
            for axis in ("X ", "Y ", "Z "):
                self.assertIn(axis, position)
        self.assertIn("35", self.visual_item(window, "previewSourcePosition2").property("text"))
        self.screenshot(window, "process-preview-minimum-size.png")
        window.resize(1280, 900)
        self.screenshot(window, "process-structure-and-metadata.png")
        self.expression(window, "previewDetails.contentItem.contentY = Math.max(0, previewDetails.contentHeight - previewDetails.availableHeight)")
        self.screenshot(window, "process-mask-details-scrolled.png")
        bridge.newDocument()
        QTest.qWait(40)
        self.assertEqual(self.expression(canvas, "plan.sources.length"), 0)
        self.assertFalse(self.expression(canvas, "Boolean(plan.monitors && plan.monitors.length)"))

        # Optimization controls now live in the project-bound mask editor.
        # Keep the process preview assertions above, then verify that a real
        # mask optimization dialog leaves parameter fields editable mid-run.
        project_root = self.root / "project"
        project_root.mkdir()
        store, _external, _process_id, _configuration = setup_project(project_root)
        host, binding, workspace, film = _bound_project(store)
        self.assertTrue(workspace.initialize_default(load_project_default(store)))
        mask_engine = QQmlEngine()
        ThemeService.instance().attach_engine(mask_engine)
        mask_window, mask_controller, component = _load_project_window(
            mask_engine, film, workspace, "mask", Navigation("Busy mask optimization test"))
        self.engines.append(mask_engine)
        self.windows.append(mask_window)
        self.components.append(component)
        try:
            mask_window.resize(900, 700)
            mask_window.show()
            QTest.qWait(50)
            button = _item(mask_window, "maskOptimizationButton")
            self.assertIsNotNone(button)
            _click(self.app, mask_window, button)
            QTest.qWait(100)
            self.app.processEvents()
            dialog = _item(mask_window, "maskOptimizationDialog")
            self.assertIsNotNone(dialog)
            self.assertTrue(dialog.property("visible"))

            optimization_bridge = mask_controller.bridge
            optimization_bridge._optimization_coordinator._optimization["busy"] = True
            optimization_bridge.optimizationChanged.emit()
            rpm = _item(mask_window, "optimizationCarrierRpmField")
            bounds = _item(mask_window, "optimizationBoundXMinField0")
            backend = _item(mask_window, "optimizationBackendSelector")
            self.assertIsNotNone(rpm)
            self.assertIsNotNone(bounds)
            self.assertIsNotNone(backend)
            self.assertTrue(rpm.isEnabled())
            self.assertTrue(backend.isEnabled())
            self.assertFalse(_item(mask_window, "startOptimizationButton").isEnabled(),
                             "a second optimization cannot start while this one is busy")
            _type_into_quick(mask_window, rpm, "39.5")
            self.assertEqual(rpm.property("text"), "39.5")
            _scroll_into_view(self.app, bounds)
            self.assertTrue(bounds.isEnabled())
            _type_into_quick(mask_window, bounds, "-6")
            self.assertEqual(bounds.property("text"), "-6")
            optimization_bridge._optimization_coordinator._optimization["message"] = "测试中的运行状态"
            optimization_bridge.optimizationChanged.emit()
            self.assertEqual(rpm.property("text"), "39.5")
            self.assertEqual(bounds.property("text"), "-6")
        finally:
            mask_window.setProperty("projectCloseAuthorized", True)
            mask_window.close()
            from coat_sim.ui.project_resource_adapter import dispose_project_resource_controller
            dispose_project_resource_controller(mask_controller, discard=True)
            mask_window.deleteLater()


    def test_pending_mask_selection_and_overwrite_confirmation_use_requested_candidate(self):
        project_root = self.root / "project"
        project_root.mkdir()
        store, _external, _process_id, _configuration = setup_project(project_root)
        host, binding, workspace, film = _bound_project(store)
        self.assertTrue(workspace.initialize_default(load_project_default(store)))
        engine = QQmlEngine()
        ThemeService.instance().attach_engine(engine)
        window, controller, component = _load_project_window(
            engine, film, workspace, "mask", Navigation("Mask candidate selection test"))
        self.engines.append(engine)
        self.windows.append(window)
        self.components.append(component)
        try:
            window.resize(900, 700)
            window.show()
            QTest.qWait(50)
            _click(self.app, window, _item(window, "maskOptimizationButton"))
            dialog = _item(window, "maskOptimizationDialog")
            self.assertIsNotNone(dialog)
            self.assertTrue(dialog.property("visible"))

            coordinator = controller.bridge._optimization_coordinator
            self.assertIsNotNone(coordinator._inputs)
            original_rows = workspace.service.list_documents("mask")
            original_ids = {row["id"] for row in original_rows}
            original_profile = deepcopy(workspace.service.load_document(original_rows[0]["id"]).data)
            first = {**original_profile, "z_mm": float(original_profile.get("z_mm", 0)) + 10.}
            second = {**original_profile, "z_mm": 550.}
            coordinator._result_inputs = coordinator._inputs
            coordinator._pending_masks = [
                {"slot": 0, "profile": first}, {"slot": 1, "profile": second}]
            coordinator._optimization.update(
                busy=False, artifacts={}, message="", stage="complete")
            coordinator.optimizationChanged.emit()
            choice = _item(window, "pendingMaskChoice")
            self.assertIsNotNone(choice)
            self.assertEqual(choice.property("count"), 2)
            choice.setProperty("currentIndex", 1)
            button = _item(window, "savePendingMaskButton")
            self.assertTrue(button.isEnabled())
            _scroll_into_view(self.app, button)
            _click(self.app, window, button)

            saved_rows = [row for row in workspace.service.list_documents("mask")
                          if row["id"] not in original_ids]
            self.assertEqual(len(saved_rows), 1)
            saved = workspace.service.load_document(saved_rows[0]["id"]).data
            self.assertEqual(saved["z_mm"], second["z_mm"],
                             "the real mask dialog must save the selected candidate index")
            self.assertEqual(saved["outer"], second["outer"])
            self.assertEqual(saved["holes"], second["holes"])

            # ProcessBridge still exposes a legacy file-save API for callers;
            # preserve its requested-candidate overwrite guard as a separate
            # compatibility contract, without relying on removed Process UI.
            legacy_bridge = ProcessBridge(self.service, str(self.root / "process.cproc"))
            self.bridges.append(legacy_bridge)
            legacy_bridge._pending_masks = [
                {"slot": 0, "profile": first}, {"slot": 1, "profile": second}]
            target = self.root / "existing-result.cmsk"
            old_content = json.dumps(self.mask)
            target.write_text(old_content, encoding="utf-8")
            url = QUrl.fromLocalFile(str(target))
            overwrite_requests = []
            legacy_bridge.maskOverwriteRequested.connect(
                lambda requested_url, index: overwrite_requests.append(
                    (requested_url.toLocalFile(), index)))
            legacy_bridge.saveOptimizedMask(url, 1, False)
            self.assertEqual([(Path(path).resolve(), index) for path, index in overwrite_requests],
                             [(target.resolve(), 1)])
            self.assertEqual(target.read_text(), old_content)
            legacy_bridge.saveOptimizedMask(url, 1, True)
            self.assertEqual(json.loads(target.read_text())["z_mm"], 550.)
            self.assertEqual(json.loads(target.read_text())["holes"], second["holes"])
        finally:
            window.setProperty("projectCloseAuthorized", True)
            window.close()
            from coat_sim.ui.project_resource_adapter import dispose_project_resource_controller
            dispose_project_resource_controller(controller, discard=True)
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
