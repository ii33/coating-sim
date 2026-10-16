"""Cancel-safe disk refresh for machine, mask and process designer controllers."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import QObject, QCoreApplication, QEvent, QUrl
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression, QQmlEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from coat_sim.core.api import CoatService
from coat_sim.core.design_files import DESIGN_EXTENSIONS
from coat_sim.core.simulation_assets import save_simulation_input_asset
from coat_sim.core.simulator import Settings
from coat_sim.ui.machine_mask import MachineBridge, MaskBridge
from coat_sim.ui.process import ProcessBridge

ROOT = Path(__file__).resolve().parents[1]
DESIGN_FILES = {kind: f"{kind}{suffix}" for kind, suffix in DESIGN_EXTENSIONS.items()}


class DesignerReloadFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = CoatService(self.root)
        self.files = {
            "source.csrc": {"schema_version": 2, "kind": "point", "device_type": "custom", "source_exponent": 1.},
            "substrate.csub": {"schema_version": 1, "name": "Test", "outline": {"kind": "circle", "radius_mm": 10.}, "surface": {"kind": "flat"}},
            "mask.cmsk": {"z_mm": 50., "outer": [[-5., -5.], [5., -5.], [5., 5.], [-5., 5.]], "holes": []},
            "machine.cmac": {"schema_version": 5, "carrier_x_mm": 0., "carrier_y_mm": 0., "planet_offset_mm": 0., "planet_tilt_deg": 0., "substrate_z_mm": 100., "sun_teeth": 120, "planet_teeth": 36, "gear_module_mm": 0., "sources": [{"file": "source.csrc", "x_mm": 0., "y_mm": 0., "z_mm": 0.}], "masks": [{"file": "mask.cmsk"}], "monitors": []},
            "process.cproc": {"schema_version": 6, "name": "Test", "machine_file": "machine.cmac", "substrate_file": "substrate.csub", "steps": [{"source_slot": 1, "target_nm": 1., "rate_nm_s": 1., "target_mode": "mean", "mask_slots": [0]}]},
        }
        for filename, data in self.files.items():
            self.write(filename, data)

    def write(self, filename, data):
        (self.root / filename).write_text(json.dumps(data), encoding="utf-8")

    def bridge(self, kind):
        return {"machine": MachineBridge, "mask": MaskBridge, "process": ProcessBridge}[kind](self.service)

    def open(self, bridge, kind, path=None):
        path = path or self.root / DESIGN_FILES[kind]
        if kind == "process":
            bridge.openPath(QUrl.fromLocalFile(str(path)))
        else:
            bridge.loadDocument(str(path))
        self.assertTrue(bridge.canReload, bridge.status)

    def data(self, bridge):
        return bridge.document if isinstance(bridge, ProcessBridge) else json.loads(bridge.documentJson)

    def snapshot(self, bridge):
        result = (deepcopy(self.data(bridge)), bridge.dirty, bridge.canReload)
        if isinstance(bridge, ProcessBridge):
            return result + (bridge.currentPath, bridge.valid, bridge.errors, bridge.sourcePreview, bridge.stepPage)
        result += (bridge.documentPath, bridge.canSave, bridge.validationMessage)
        if isinstance(bridge, MaskBridge):
            return result + (bridge.machineJson, bridge.ringsJson, bridge.weightsJson, bridge.pointWeightsJson, bridge.fieldJson)
        return result + (bridge.maskPlanesJson,)

    def edit(self, bridge, value="unsaved"):
        bridge.setMetadata("notes", value)

    def wait_for_geometry_preview(self, kind, window, bridge):
        def ready():
            if kind == "machine":
                return bool(window.property("previewRequested")) and bool(
                    json.loads(bridge.maskPlanesJson))
            if kind == "process":
                preview = bridge.sourcePreview
                return bool(window.property("sourcePreviewRequested")) and bool(
                    preview.get("masks")) and "outer" in preview["masks"][0]
            return True

        for _ in range(100):
            self.app.processEvents()
            if ready():
                return
            QTest.qWait(10)
        self.fail(f"{kind} geometry preview did not initialize after the first frame")


class DesignerReloadTests(DesignerReloadFixture):
    def test_no_origin_and_intended_draft_paths_do_not_enable_reload(self):
        for kind in ("machine", "mask", "process"):
            with self.subTest(kind=kind):
                bridge = self.bridge(kind)
                self.assertFalse(bridge.canReload)
                self.assertFalse(bridge.reloadCurrent())
                bridge.applyDocument(self.files[DESIGN_FILES[kind]], str(self.root / DESIGN_FILES[kind]))
                self.assertFalse(bridge.canReload)
                before = self.snapshot(bridge)
                self.assertFalse(bridge.reloadCurrent())
                self.assertEqual(self.snapshot(bridge), before)

    def test_refresh_reads_external_changes_without_writing(self):
        for kind in ("machine", "mask", "process"):
            with self.subTest(kind=kind):
                bridge = self.bridge(kind)
                self.open(bridge, kind)
                updated = deepcopy(self.files[DESIGN_FILES[kind]])
                updated["notes"] = "outside edit"
                self.write(DESIGN_FILES[kind], updated)
                original = (self.root / DESIGN_FILES[kind]).read_bytes()
                with patch.object(self.service, "save", side_effect=AssertionError("refresh must never save")):
                    self.assertTrue(bridge.reloadCurrent(), bridge.status)
                    self.assertTrue(bridge.reloadCurrent(), bridge.status)
                self.assertEqual(self.data(bridge)["notes"], "outside edit")
                self.assertFalse(bridge.dirty)
                self.assertEqual((self.root / DESIGN_FILES[kind]).read_bytes(), original)

    def test_dirty_confirm_cancel_repeat_and_stale_confirmation(self):
        for kind in ("machine", "mask", "process"):
            with self.subTest(kind=kind):
                bridge = self.bridge(kind)
                self.open(bridge, kind)
                self.edit(bridge)
                requested = []
                bridge.reloadRequested.connect(lambda: requested.append(True))
                before = self.snapshot(bridge)
                self.assertFalse(bridge.reloadCurrent())
                self.assertFalse(bridge.reloadCurrent())
                self.assertEqual(len(requested), 1)
                self.assertEqual(self.snapshot(bridge), before)
                self.assertFalse(bridge.confirmReload(False))
                self.assertEqual(self.snapshot(bridge), before)
                self.assertFalse(bridge.confirmReload(True))
                bridge.reloadCurrent()
                self.edit(bridge, "newer draft")
                newer = self.snapshot(bridge)
                self.assertFalse(bridge.confirmReload(True))
                self.assertEqual(self.snapshot(bridge), newer)
                bridge.reloadCurrent()
                self.assertTrue(bridge.confirmReload(True), bridge.status)
                self.assertFalse(bridge.dirty)
                self.assertNotEqual(self.data(bridge).get("notes"), "newer draft")

    def test_missing_malformed_and_dependency_failures_are_atomic(self):
        for kind in ("machine", "mask", "process"):
            for failure in ("missing", "malformed", "dependency"):
                with self.subTest(kind=kind, failure=failure):
                    for filename, data in self.files.items():
                        self.write(filename, data)
                    bridge = self.bridge(kind)
                    self.open(bridge, kind)
                    if kind == "mask":
                        bridge.loadMachine(str(self.root / "machine.cmac"))
                        bridge._weights = {"preserved": True}
                    self.edit(bridge)
                    before = self.snapshot(bridge)
                    target = self.root / DESIGN_FILES[kind]
                    if failure == "missing":
                        target.unlink()
                    elif failure == "malformed":
                        target.write_text("{broken", encoding="utf-8")
                    else:
                        (self.root / "source.csrc").write_text("{}", encoding="utf-8")
                    bridge.reloadCurrent()
                    self.assertFalse(bridge.confirmReload(True))
                    self.assertIn("刷新失败", bridge.status)
                    self.assertEqual(self.snapshot(bridge), before)

    def test_dependency_only_changes_refresh_all_previews_and_mask_analysis(self):
        machine = self.bridge("machine")
        mask = self.bridge("mask")
        process = self.bridge("process")
        for kind, bridge in (("machine", machine), ("mask", mask), ("process", process)):
            self.open(bridge, kind)
        machine.preparePreview()
        process.prepareSourcePreview()
        mask.loadMachine(str(self.root / "machine.cmac"))
        mask.setWorkpieceDiameter("140")
        old_request = mask._request_id
        mask._weights = {"old": True}
        mask._point_weights = {"old": True}
        mask._field = {"old": True}
        fit_requests = []
        mask.viewFitRequested.connect(lambda: fit_requests.append(True))
        updated_mask = deepcopy(self.files["mask.cmsk"])
        updated_mask["outer"][0][0] = -7.
        self.write("mask.cmsk", updated_mask)
        updated_machine = deepcopy(self.files["machine.cmac"])
        updated_machine["sources"][0]["x_mm"] = 20.
        self.write("machine.cmac", updated_machine)
        for bridge in (machine, mask, process):
            self.assertTrue(bridge.reloadCurrent(), bridge.status)
        self.assertEqual(json.loads(machine.maskPlanesJson)[0]["outer"][0][0], -7.)
        self.assertEqual(process.sourcePreview["masks"][0]["outer"][0][0], -7.)
        self.assertEqual(process.sourcePreview["sources"][0]["x_mm"], 20.)
        self.assertEqual(json.loads(mask.machineJson)["sources"][0]["x_mm"], 20.)
        self.assertEqual(mask.workpieceDiameterMm, 140.)
        self.assertEqual(fit_requests, [])
        self.assertEqual(json.loads(mask.weightsJson), {})
        self.assertEqual(json.loads(mask.pointWeightsJson), {})
        self.assertEqual(json.loads(mask.fieldJson), {})
        mask._accept_weights(old_request, '{"stale": true}', .01)
        self.assertEqual(json.loads(mask.weightsJson), {})

    def test_mask_machine_only_refresh_preserves_unsaved_mask(self):
        bridge = self.bridge("mask")
        bridge.newDocument(json.dumps(self.files["mask.cmsk"]))
        bridge.loadMachine(str(self.root / "machine.cmac"))
        self.edit(bridge)
        before = self.data(bridge)
        requested = []
        bridge.reloadRequested.connect(lambda: requested.append(True))
        self.assertTrue(bridge.canReload)
        self.assertTrue(bridge.reloadCurrent(), bridge.status)
        self.assertEqual(requested, [])
        self.assertTrue(bridge.dirty)
        self.assertEqual(self.data(bridge), before)

    def test_saved_origin_changes_on_save_as_and_is_cleared_by_new(self):
        for kind in ("machine", "mask", "process"):
            with self.subTest(kind=kind):
                bridge = self.bridge(kind)
                self.open(bridge, kind)
                target = self.root / f"another-{kind}{Path(DESIGN_FILES[kind]).suffix}"
                if kind == "process":
                    bridge.saveAsPath(QUrl.fromLocalFile(str(target)))
                else:
                    bridge.saveDocumentAs(str(target))
                self.assertTrue(target.is_file(), bridge.status)
                saved = json.loads(target.read_text())
                saved["notes"] = "saved origin"
                self.write(target.name, saved)
                self.assertTrue(bridge.reloadCurrent(), bridge.status)
                self.assertEqual(self.data(bridge)["notes"], "saved origin")
                if kind == "process":
                    bridge.newDocument()
                else:
                    bridge.newDocument(json.dumps(self.files[DESIGN_FILES[kind]]))
                self.assertFalse(bridge.canReload)

    def make_package(self, path, overwrite=False):
        snapshot = self.service.freeze_process(self.service.load("process", self.root / "process.cproc").data,
                                               base_dir=self.root)
        save_simulation_input_asset(self.root, name="Reload inputs", settings=Settings(),
            input_snapshot=snapshot, confirmed=True, path=path, overwrite=overwrite)

    def test_reloads_same_replaced_package_without_original_external_files(self):
        package = self.root / "inputs.coatasset"
        self.make_package(package)
        bridges = {kind: self.bridge(kind) for kind in ("machine", "mask", "process")}
        for kind, bridge in bridges.items():
            self.open(bridge, kind, package)
            if kind == "machine":
                bridge.preparePreview()
            elif kind == "process":
                bridge.prepareSourcePreview()
            self.edit(bridge)
        for kind in ("machine", "mask", "process"):
            updated = deepcopy(self.files[DESIGN_FILES[kind]])
            updated["notes"] = "replacement package"
            if kind == "mask":
                updated["outer"][0][0] = -7.
            if kind == "machine":
                updated["sources"][0]["x_mm"] = 20.
            self.write(DESIGN_FILES[kind], updated)
        self.make_package(package, overwrite=True)
        for filename in self.files:
            (self.root / filename).unlink()
        content = package.read_bytes()
        with patch.object(self.service, "resolve_reference", side_effect=AssertionError("package must not reroot references")), \
             patch.object(self.service, "save", side_effect=AssertionError("refresh must never save")):
            for kind, bridge in bridges.items():
                with self.subTest(kind=kind):
                    bridge.reloadCurrent()
                    before = self.snapshot(bridge)
                    self.assertFalse(bridge.confirmReload(False))
                    self.assertEqual(self.snapshot(bridge), before)
                    bridge.reloadCurrent()
                    self.assertTrue(bridge.confirmReload(True), bridge.status)
                    self.assertEqual(self.data(bridge)["notes"], "replacement package")
                    path = bridge.currentPath if kind == "process" else bridge.documentPath
                    self.assertEqual(path, str(package))
                    self.assertFalse(bridge.dirty)
        self.assertEqual(json.loads(bridges["machine"].maskPlanesJson)[0]["outer"][0][0], -7.)
        self.assertEqual(bridges["process"].sourcePreview["sources"][0]["x_mm"], 20.)
        self.assertEqual(package.read_bytes(), content)
        package.write_bytes(b"broken package")
        for bridge in bridges.values():
            before = self.snapshot(bridge)
            self.assertFalse(bridge.reloadCurrent())
            self.assertEqual(self.snapshot(bridge), before)

    def test_exported_asset_designer_reloads_exported_file_not_package(self):
        package = self.root / "inputs.coatasset"
        self.make_package(package)
        for kind in ("machine", "mask", "process"):
            with self.subTest(kind=kind):
                bridge = self.bridge(kind)
                browser = bridge.assetBrowser
                self.assertTrue(browser.inspect(QUrl.fromLocalFile(str(package))))
                index = next(i for i, row in enumerate(browser.designs) if row["kind"] == kind)
                directory = self.root / f"exported-{kind}"
                browser.export(QUrl.fromLocalFile(str(directory)), index, False, True)
                target = directory / browser.designs[index]["path"]
                self.assertTrue(target.is_file(), browser.status)
                data = json.loads(target.read_text())
                data["notes"] = "exported file edit"
                target.write_text(json.dumps(data), encoding="utf-8")
                self.assertTrue(bridge.reloadCurrent(), bridge.status)
                self.assertEqual(self.data(bridge)["notes"], "exported file edit")
                path = bridge.currentPath if kind == "process" else bridge.documentPath
                self.assertEqual(path, str(target))

    def test_process_reload_preserves_page_then_clamps_after_step_removal(self):
        recipe = deepcopy(self.files["process.cproc"])
        recipe["steps"] *= 81
        self.write("process.cproc", recipe)
        bridge = self.bridge("process")
        self.open(bridge, "process")
        bridge.setStepPage(2)
        self.assertTrue(bridge.reloadCurrent(), bridge.status)
        self.assertEqual(bridge.stepPage, 2)
        recipe["steps"] = recipe["steps"][:2]
        self.write("process.cproc", recipe)
        self.assertTrue(bridge.reloadCurrent(), bridge.status)
        self.assertEqual(bridge.stepPage, 0)


class DesignerReloadQmlTests(DesignerReloadFixture):
    def test_qml_refresh_buttons_and_discard_dialogs(self):
        for kind in ("machine", "mask", "process"):
            with self.subTest(kind=kind):
                bridge = self.bridge(kind)
                engine = QQmlApplicationEngine()
                engine.rootContext().setContextProperty("bridge", bridge)
                component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml" / f"{kind.title()}Window.qml")))
                self.assertFalse(component.isError(), component.errors())
                with patch(f"coat_sim.ui.{'process' if kind == 'process' else 'machine_mask'}.design_directory", return_value=self.root):
                    window = component.create()
                self.assertIsNotNone(window, component.errors())
                try:
                    window.showNormal()
                    window.resize(1280, 900)
                    button = window.findChild(QQuickItem, "reloadButton")
                    self.assertIsNotNone(button)
                    self.assertFalse(button.property("enabled"))
                    self.open(bridge, kind)
                    QTest.qWait(10)
                    self.assertTrue(button.property("enabled"))
                    self.wait_for_geometry_preview(kind, window, bridge)
                    self.edit(bridge)
                    before = self.snapshot(bridge)
                    button.clicked.emit()
                    QTest.qWait(10)
                    dialog = window.findChild(QObject, "reloadDialog")
                    self.assertTrue(dialog.property("visible"))
                    dialog.reject()
                    self.assertEqual(self.snapshot(bridge), before)
                    button.clicked.emit()
                    QTest.qWait(10)
                    self.assertTrue(dialog.property("visible"))
                    dialog.accept()
                    self.assertFalse(bridge.dirty)
                    if kind == "mask":
                        window.setProperty("autoFitView", False)
                        for key, value in (("panX", 17.), ("panY", -13.), ("zoomX", 1.7), ("zoomY", .6)):
                            window.setProperty(key, value)
                        view = [window.property(key) for key in ("panX", "panY", "zoomX", "zoomY")]
                        button.clicked.emit()
                        QTest.qWait(10)
                        self.assertEqual([window.property(key) for key in ("panX", "panY", "zoomX", "zoomY")], view)
                    image = window.grabWindow()
                    self.assertFalse(image.isNull())
                    output = os.environ.get("COAT_RELOAD_SCREENSHOT_DIR")
                    if output:
                        Path(output).mkdir(parents=True, exist_ok=True)
                        self.assertTrue(image.save(str(Path(output) / f"deposition-{kind}-reload.png")))
                finally:
                    window.close()
                    window.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                    self.app.processEvents()
                    self.assertFalse(isValid(window), "QML root must be destroyed before its engine")
                    engine.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                    self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
