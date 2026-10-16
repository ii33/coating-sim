"""Offscreen QML clicks exercise same-process design navigation."""

from __future__ import annotations

from copy import deepcopy
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from pathlib import Path
import tempfile
from typing import Any, ClassVar
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, QPoint, QPointF, Property, QUrl, Qt, Signal, Slot, QCoreApplication, QEvent
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from shiboken6 import isValid

from coat_sim.core.api import CoatService
from coat_sim.core.film_design import new_design
from coat_sim.core.project_film_service import ProjectFilmService
from coat_sim.core.project_store import ProjectStore
from coat_sim.ui.main import MainBridge
from coat_sim.ui.machine_mask import MachineBridge, MaskBridge
from coat_sim.ui.process import ProcessBridge
from coat_sim.ui.source_substrate import SubstrateBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot
from coat_sim.scheduler import SchedulerClient
from tests.test_scheduler_service import stop_service


ROOT = Path(__file__).resolve().parents[1]


class _RecordingLauncher(QObject):
    def __init__(self):
        super().__init__()
        self.calls = []

    @Slot(str, str, result=str)
    def openWindow(self, module, document_path):
        self.calls.append((module, document_path))
        return "test-window"


class _ProjectNavigation(QObject):
    projectNameChanged = Signal()

    def __init__(self, name):
        super().__init__()
        self._project_name = name

    @Property(str, notify=projectNameChanged)
    def projectName(self):
        return self._project_name

    def rename(self, name):
        if name != self._project_name:
            self._project_name = name
            self.projectNameChanged.emit()


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

    def setMode(self, mode):
        if mode != self._mode:
            self._mode = mode
            self.modeChanged.emit()
            self.snapshotChanged.emit()


class ProcessEditorNavigationQtTests(unittest.TestCase):
    app: ClassVar[QGuiApplication]

    @classmethod
    def setUpClass(cls):
        QQuickStyle.setStyle("Fusion")
        app = QGuiApplication.instance() or QGuiApplication([])
        if not isinstance(app, QGuiApplication):
            raise RuntimeError("Process navigation tests require a QGuiApplication singleton")
        cls.app = app
        cls.addClassCleanup(cls.app.setFont, cls.app.font())
        font_path = ROOT / "coat_sim" / "ui" / "assets" / "fonts" / "noto-cjk" / "NotoSansCJKsc-Regular.otf"
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id >= 0:
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                cls.app.setFont(QFont(families[0]))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="process-navigation-qt-")
        self.root = Path(self.temporary.name)
        self.designs = self.root / "designs"
        self.designs.mkdir()
        self.service = CoatService(root=self.root)
        self._write_designs()
        self.launcher = _RecordingLauncher()
        self.theme = _ThemeStub()
        self.engines = []
        self.components = []
        self.windows = []
        self.bridges = []
        self.path_patches = [
            patch(f"coat_sim.ui.{module}.design_directory",
                  side_effect=lambda kind: self.root / "designs" / kind)
            for module in ("main", "process", "machine_mask", "source_substrate")
        ]
        self.path_patches.append(
            patch("coat_sim.ui.main.data_path",
                  side_effect=lambda name: self.root / "data" / name))
        for patcher in self.path_patches:
            patcher.start()

    def tearDown(self):
        # Qt deletion and directory cleanup must wait for owned probe threads,
        # which can still create a scheduler after the window is closed.
        for bridge in self.bridges:
            if isinstance(bridge, ProcessBridge):
                bridge.cancel()
            target = getattr(bridge, '_compute_target', None)
            if target is not None:
                target._poll.stop()
                target._executor.shutdown(wait=True, cancel_futures=True)
        for window in self.windows:
            if isValid(window):
                window.close()
                window.deleteLater()
        self.components.clear()
        for engine in self.engines:
            if isValid(engine):
                engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for bridge in self.bridges:
            detach = getattr(bridge, 'detach_observers', None)
            if detach is not None:
                detach()
            bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.windows.clear()
        self.engines.clear()
        self.bridges.clear()
        self.launcher.deleteLater()
        self.theme.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for patcher in reversed(self.path_patches):
            patcher.stop()
        if (self.root / 'artifacts/runtime/scheduler/endpoint.json').is_file():
            stop_service(SchedulerClient(self.root))
        self.temporary.cleanup()

    def _write_designs(self):
        source = {
            "schema_version": 2, "kind": "point",
            "device_type": "electron_beam", "source_exponent": 1.0,
        }
        substrate = {
            "schema_version": 1, "name": "Navigation substrate",
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
            "monitors": [{"name": "Test monitor", "kind": "crystal",
                          "x_mm": 50.0, "y_mm": 80.0, "z_mm": 700.0}],
        }
        process = {
            "schema_version": 6, "name": "Navigation process",
            "machine_file": "machine.cmac", "substrate_file": "substrate.csub",
            "steps": [{"source_slot": 1, "target_nm": 100.0,
                       "rate_nm_s": 1.0, "target_mode": "mean"}],
        }
        self.paths = {
            "source": self.designs / "source.csrc",
            "substrate": self.designs / "substrate.csub",
            "mask": self.designs / "mask.cmsk",
            "machine": self.designs / "machine.cmac",
            "process": self.designs / "process.cproc",
        }
        for kind, path, data in (
            ("source", self.paths["source"], source),
            ("substrate", self.paths["substrate"], substrate),
            ("mask", self.paths["mask"], mask),
            ("machine", self.paths["machine"], machine),
            ("process", self.paths["process"], process),
        ):
            self.service.save(kind, path, data)

    def _load_window(self, bridge, qml_name):
        if bridge not in self.bridges:
            self.bridges.append(bridge)
        engine = QQmlEngine()
        engine.rootContext().setContextProperty("bridge", bridge)
        engine.rootContext().setContextProperty("launcher", self.launcher)
        engine.rootContext().setContextProperty("Theme", self.theme)
        if hasattr(bridge, "image_provider"):
            engine.addImageProvider("thickness", bridge.image_provider)
        self.engines.append(engine)
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml" / qml_name)))
        self.components.append(component)
        window = component.createWithInitialProperties({"initialMaximized": False})
        if window is None:
            errors = "\n".join(error.toString() for error in component.errors())
            self.fail(f"{qml_name} failed to load:\n{errors}")
        self.windows.append(window)
        window.resize(1280, 900)
        window.showNormal()
        self._settle()
        return window

    def _settle(self):
        self.app.processEvents()
        QTest.qWait(35)
        self.app.processEvents()

    def _click(self, window, object_name):
        item = window.findChild(QQuickItem, object_name)
        self.assertIsNotNone(item, f"missing QML item {object_name}")
        return self._click_item(window, item, object_name)

    def _click_item(self, window, item, description="QQuickItem"):
        # Responsive bars may defer a reflow after bridge changes; calculate
        # the input coordinates only after that layout has settled.
        self._settle()
        self.assertIsNotNone(item, f"missing QML item {description}")
        self.assertTrue(item.isVisible(), f"{description} is not visible")
        self.assertTrue(item.isEnabled(), f"{description} is disabled")
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                         QPoint(round(point.x()), round(point.y())))
        self._settle()
        return item

    def _quick_items(self, window):
        pending = [window.contentItem()]
        while pending:
            item = pending.pop()
            yield item
            pending.extend(item.childItems())

    def _quick_item_by_object_name(self, window, object_name):
        return next((item for item in self._quick_items(window)
                     if item.property("objectName") == object_name), None)

    def _scroll_item_into_view(self, item):
        ancestor = item.parentItem()
        while ancestor is not None:
            content_y = ancestor.property("contentY")
            content_height = ancestor.property("contentHeight")
            viewport_height = ancestor.property("height")
            if (content_y is not None and content_height is not None
                    and viewport_height is not None
                    and float(content_height) > float(viewport_height)):
                item_y = item.mapToItem(ancestor, QPointF(0, 0)).y()
                item_height = float(item.property("height"))
                target_y = (float(content_y) + item_y
                            - max(0.0, (float(viewport_height) - item_height) / 2))
                max_y = max(0.0, float(content_height) - float(viewport_height))
                ancestor.setProperty("contentY", max(0.0, min(target_y, max_y)))
                self._settle()
                return
            ancestor = ancestor.parentItem()

    def _qml_value(self, window, expression):
        value = QQmlExpression(QQmlEngine.contextForObject(window), window, expression)
        result, _ = value.evaluate()
        self.assertFalse(value.hasError(), value.error().toString())
        return result.toVariant() if hasattr(result, "toVariant") else result

    def _save_render(self, window, directory, name):
        image = window.grabWindow()
        self.assertFalse(image.isNull(), f"empty render for {name}")
        path = directory / name
        self.assertTrue(image.save(str(path)), f"could not save {path}")
        return path

    def _screenshot_root(self):
        configured = os.environ.get("COAT_THEME_SCREENSHOT_DIR")
        root = Path(configured) if configured else self.root / "screenshots"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def test_main_window_process_create_edit_and_uniformity_sidebar(self):
        bridge = MainBridge(self.service)
        bridge._result = {
            "mean_nm": 100.0, "min_nm": 98.0, "max_nm": 102.0,
            "pv_nm": 4.0, "pv_percent": 4.0, "cv_percent": 1.25,
            "elapsed_s": 3.0,
        }
        bridge.changed.emit()
        window = self._load_window(bridge, "MainWindow.qml")

        process_action = window.findChild(QQuickItem, "processEditorActionButton")
        self.assertIsNotNone(process_action)
        self.assertEqual(process_action.property("text"), "新建工艺")
        self._click(window, "processEditorActionButton")
        self.assertEqual(self.launcher.calls[-1], ("process", ""))

        bridge.openDocument(str(self.paths["process"]))
        self._settle()
        self.assertEqual(bridge.paths["process"], str(self.paths["process"].resolve()))
        self.assertEqual(process_action.property("text"), "编辑工艺")
        self._click(window, "processEditorActionButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("process", str(self.paths["process"].resolve())))

        bridge.clearFile("process")
        self._settle()
        self.assertEqual(process_action.property("text"), "新建工艺")

        rows = {row["key"]: row for row in bridge.uniformityRows}
        self.assertEqual(rows["rms_percent"]["value"], "1.25 %")
        self.assertEqual(rows["mean_nm"]["value"], "100 nm")
        labels = [item.property("text") for item in self._quick_items(window)
                  if item.property("text") is not None]
        self.assertIn(rows["rms_percent"]["label"], labels)
        self.assertIn(rows["rms_percent"]["value"], labels)
        self.assertNotIn("Elapsed: 3 s", labels)

    def test_process_dependency_routes_and_send_save_decision(self):
        bridge = ProcessBridge(self.service, str(self.paths["process"]))
        self.assertTrue(bridge.valid, bridge.errors)
        window = self._load_window(bridge, "ProcessWindow.qml")

        machine_action = window.findChild(QQuickItem, "machineDesignActionButton")
        substrate_action = window.findChild(QQuickItem, "substrateDesignActionButton")
        self.assertEqual(machine_action.property("text"), "编辑")
        self.assertEqual(substrate_action.property("text"), "编辑")
        self._click(window, "machineDesignActionButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("machine", str(self.paths["machine"].resolve())))
        self._click(window, "substrateDesignActionButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("substrate", str(self.paths["substrate"].resolve())))

        # The actual target bridge opens the explicit path selected by the editor.
        target = MachineBridge(self.service)
        target.openDocument(self.launcher.calls[-2][1])
        self.assertEqual(Path(target._path), self.paths["machine"].resolve())
        target.deleteLater()
        substrate_target = SubstrateBridge(self.service)
        substrate_target.openDocument(self.launcher.calls[-1][1])
        self.assertEqual(substrate_target._path, self.paths["substrate"].resolve())
        substrate_target.deleteLater()
        self._settle()

        self.launcher.calls.clear()
        self._click(window, "sendToSimulationButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("main", str(self.paths["process"].resolve())))

        original = self.service.load("process", self.paths["process"]).data["name"]
        self.launcher.calls.clear()
        bridge.setField("name", "Unsaved navigation edit")
        self._click(window, "sendToSimulationButton")
        self.assertEqual(self.launcher.calls, [])
        self.assertEqual(self.service.load("process", self.paths["process"]).data["name"],
                         original)
        self._click(window, "saveAndSendToSimulationButton")
        self.assertFalse(bridge.dirty)
        self.assertEqual(self.service.load("process", self.paths["process"]).data["name"],
                         "Unsaved navigation edit")
        self.assertEqual(self.launcher.calls[-1],
                         ("main", str(self.paths["process"].resolve())))

        bridge.setField("machine_file", "")
        bridge.setField("substrate_file", "")
        self._settle()
        self.assertEqual(machine_action.property("text"), "新建")
        self.assertEqual(substrate_action.property("text"), "新建")
        self._click(window, "machineDesignActionButton")
        self.assertEqual(self.launcher.calls[-1], ("machine", ""))
        self._click(window, "substrateDesignActionButton")
        self.assertEqual(self.launcher.calls[-1], ("substrate", ""))

    def test_process_and_substrate_dirty_titles_keep_project_suffix_separate(self):
        process_bridge = ProcessBridge(self.service, str(self.paths["process"]))
        process_window = self._load_window(process_bridge, "ProcessWindow.qml")
        process_nav = _ProjectNavigation("工程甲")
        process_window.setProperty("projectNavigation", process_nav)
        process_bridge.setMetadata("notes", "未保存更改")
        self._settle()
        process_base = "工艺 · Navigation process *"
        self.assertEqual(process_window.property("title"), process_base + " — 工程甲")
        self.assertEqual(process_window.property("caption"), process_base + " — 工程甲")

        process_nav.rename("改名工程")
        self._settle()
        self.assertEqual(process_window.property("title"), process_base + " — 改名工程")
        self.assertEqual(process_window.property("caption"), process_base + " — 改名工程")

        global_process_bridge = ProcessBridge(self.service, str(self.paths["process"]))
        global_process_window = self._load_window(global_process_bridge, "ProcessWindow.qml")
        global_process_bridge.setMetadata("notes", "全局未保存更改")
        self._settle()
        self.assertEqual(global_process_window.property("title"),
                         "工艺 · Navigation process *")
        self.assertEqual(global_process_window.property("caption"),
                         "工艺 · Navigation process *")

        substrate_bridge = SubstrateBridge(self.service, str(self.paths["substrate"]))
        substrate_window = self._load_window(substrate_bridge, "SubstrateWindow.qml")
        substrate_nav = _ProjectNavigation("工程乙")
        substrate_window.setProperty("projectNavigation", substrate_nav)
        substrate_bridge.setField("name", "未保存基片")
        self._settle()
        substrate_base = "基片 *"
        self.assertEqual(substrate_window.property("title"), substrate_base + " — 工程乙")
        self.assertEqual(substrate_window.property("caption"),
                         "基片 — 未保存基片 * — 工程乙")

        # A global/non-project editor still displays its dirty marker without
        # manufacturing a project suffix or appending the marker to the name.
        global_bridge = SubstrateBridge(self.service, str(self.paths["substrate"]))
        global_window = self._load_window(global_bridge, "SubstrateWindow.qml")
        global_bridge.setField("name", "全局未保存基片")
        self._settle()
        self.assertEqual(global_window.property("title"), "基片 *")
        self.assertEqual(global_window.property("caption"), "基片 — 全局未保存基片 *")

    def test_process_hides_batch_entry_and_keeps_compact_dependency_labels(self):
        bridge = ProcessBridge(self.service, str(self.paths["process"]))
        window = self._load_window(bridge, "ProcessWindow.qml")
        batch = self._quick_item_by_object_name(window, "batchFilmStackButton")
        self.assertIsNotNone(batch)
        self.assertFalse(batch.isVisible())
        self.assertIsNotNone(bridge.filmStack)
        self.assertTrue(callable(bridge.property("filmStack").build))
        for name in ("selectMachineFileButton", "selectSubstrateFileButton"):
            button = self._quick_item_by_object_name(window, name)
            self.assertEqual(button.property("text"), "浏览")
        for name in ("machineDesignActionButton", "substrateDesignActionButton"):
            button = self._quick_item_by_object_name(window, name)
            self.assertEqual(button.property("text"), "编辑")

    def test_process_missing_machine_import_opens_error_and_valid_retry_applies(self):
        # All optical data is synthetic and local to this fixture. No global
        # catalog, saved user design, network request or scheduler is involved.
        film_service = ProjectFilmService(ProjectStore.create(self.root / "film", "Synthetic"))
        design = new_design("Synthetic in-memory film", "process-import-fixture")
        design["substrate"].update(material_id="fixture-material", optical_set_id="visible")
        design["front_layers"] = [{
            "id": "fixture-layer", "material_id": "fixture-material", "optical_set_id": "visible",
            "thickness": {"value": 125.0, "unit": "nm"},
        }]
        record: dict[str, Any] = {
            "id": "fixture-material", "name": "Synthetic fixture", "notes": "Test only",
            "optical_sets": [{
                "id": "visible", "name": "Synthetic visible", "kind": "tabulated",
                "wavelength_unit": "nm", "wavelength_min": 400.0, "wavelength_max": 700.0,
                "wavelengths": [400.0, 700.0], "n": [2.0, 2.0], "k": [0.0, 0.0],
                "source": "Synthetic fixture",
            }],
        }
        film_service.create_film(design, materials={record["id"]: record})
        self.enterContext(patch.object(self.service, "_film_design_service", film_service))
        bridge = ProcessBridge(self.service, str(self.paths["process"]))
        bridge.setField("machine_file", "")
        window = self._load_window(bridge, "ProcessWindow.qml")
        requested, ready = [], []
        bridge.filmDesignImportRequested.connect(lambda: requested.append(True))
        bridge.filmDesignImportReady.connect(lambda: ready.append(True))
        before = deepcopy(bridge.document)
        dirty_before = bridge.dirty
        files_before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        for _attempt in range(2):
            self._click(window, "filmDesignImportButton")
            self.assertTrue(self._qml_value(window, "filmDesignImportDialog.visible"))
            self.assertIn("浏览或新建机器", bridge.property("filmDesignImportError"))
            self.assertTrue(any(item.isVisible() and item.property("text") == bridge.property("filmDesignImportError")
                                for item in self._quick_items(window)))
            self.assertEqual(bridge.status, bridge.property("filmDesignImportError"))
            self.assertFalse(bridge.hasFilmDesignImportCandidate)
            self.assertFalse(bridge.filmDesignImportReadyToApply)
            self.assertEqual(bridge.document, before)
            self.assertEqual(bridge.dirty, dirty_before)
            self.assertEqual(requested, [])
            if _attempt == 0 and os.environ.get("COAT_THEME_SCREENSHOT_DIR"):
                self._save_render(window, self._screenshot_root(), "process-import-missing-machine.png")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            QTest.qWait(200)  # Let the modal popup's exit transition release input.
            self._settle()
            self.assertFalse(self._qml_value(window, "filmDesignImportDialog.visible"))
        self.assertEqual(len(ready), 2)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()},
                         files_before)

        # Closing the missing-machine dialog and selecting a valid machine
        # resets the old error and allows the ordinary import path to finish.
        bridge.chooseDependency("machine_file", QUrl.fromLocalFile(str(self.paths["machine"])))
        bridge.filmDesignImportRequested.connect(
            lambda: bridge.receiveFilmDesignDraft(design, "Synthetic in-memory film"))
        self._click(window, "filmDesignImportButton")
        self.assertEqual(requested, [True])
        self.assertTrue(bridge.hasFilmDesignImportCandidate)
        self.assertTrue(self._qml_value(window, "filmDesignImportDialog.visible"))
        self.assertTrue(bridge.setFilmDesignImportSurface("front"), bridge.property("filmDesignImportError"))
        bridge.setFilmDesignImportSource("fixture-material", 1)
        self.assertTrue(bridge.filmDesignImportReadyToApply, bridge.property("filmDesignImportError"))
        if os.environ.get("COAT_THEME_SCREENSHOT_DIR"):
            self._save_render(window, self._screenshot_root(), "process-import-valid-review.png")
        self.assertTrue(bridge.applyFilmDesignImport(True), bridge.property("filmDesignImportError"))
        self.assertEqual(bridge.stepCount, 1)
        self.assertEqual(bridge.property("document")["steps"][0]["target_nm"], 125.0)
        self.assertEqual(bridge.property("document")["steps"][0]["film_layer_ref"],
                         {"surface": "front", "layer_id": "fixture-layer"})
        self.assertEqual(self.paths["process"].read_bytes(), files_before[self.paths["process"]])
        QTest.keyClick(window, Qt.Key.Key_Escape)
        QTest.qWait(200)
        self._settle()
        bridge.setField("machine_file", "")
        imported_document = deepcopy(bridge.document)
        self._click(window, "filmDesignImportButton")
        self.assertEqual(bridge.document, imported_document)
        self.assertFalse(bridge.hasFilmDesignImportCandidate)
        self.assertEqual(bridge.filmDesignImportSourceLabel, "")
        self.assertEqual(requested, [True])
        QTest.keyClick(window, Qt.Key.Key_Escape)
        QTest.qWait(200)
        self._settle()
        self.assertFalse(self._qml_value(window, "filmDesignImportDialog.visible"))

    def test_process_import_invalid_or_unconfigured_machine_reports_error_and_recovers(self):
        bridge = ProcessBridge(self.service, str(self.paths["process"]))
        self.bridges.append(bridge)
        requested, ready = [], []
        bridge.filmDesignImportRequested.connect(lambda: requested.append(True))
        bridge.filmDesignImportReady.connect(lambda: ready.append(True))
        unconfigured_machine = deepcopy(self.service.load("machine", self.paths["machine"]).data)
        unconfigured_machine["sources"][0]["file"] = "not-yet-configured.csrc"
        incomplete_machine = self.designs / "unconfigured-machine.cmac"
        # Deliberately simulate an externally edited/broken dependency graph;
        # normal saves correctly refuse to publish missing source references.
        incomplete_machine.write_text(json.dumps(unconfigured_machine), encoding="utf-8")
        for reference, expected in (("missing-machine.cmac", "选择并验证机器"),
                                    (incomplete_machine.name, "没有有效蒸发源")):
            bridge.setField("machine_file", reference)
            before, dirty_before = deepcopy(bridge.document), bridge.dirty
            bridge.requestFilmDesignImport()
            self.assertIn(expected, bridge.property("filmDesignImportError"))
            self.assertEqual(bridge.document, before)
            self.assertEqual(bridge.dirty, dirty_before)
            self.assertEqual(requested, [])
        self.assertEqual(len(ready), 2)
        bridge.chooseDependency("machine_file", QUrl.fromLocalFile(str(self.paths["machine"])))
        bridge.requestFilmDesignImport()
        self.assertEqual(requested, [True])
        self.assertEqual(bridge.property("filmDesignImportError"), "")

    def test_process_import_rechecks_changed_source_dependency_before_request(self):
        bridge = ProcessBridge(self.service, str(self.paths["process"]))
        self.bridges.append(bridge)
        requested, ready = [], []
        bridge.filmDesignImportRequested.connect(lambda: requested.append(True))
        bridge.filmDesignImportReady.connect(lambda: ready.append(True))
        before = deepcopy(bridge.document)
        dirty_before = bridge.dirty
        source_bytes = self.paths["source"].read_bytes()
        try:
            self.paths["source"].unlink()
            bridge.requestFilmDesignImport()
            self.assertEqual(requested, [])
            self.assertEqual(len(ready), 1)
            self.assertIn("没有有效蒸发源", bridge.property("filmDesignImportError"))
            self.assertEqual(bridge.document, before)
            self.assertEqual(bridge.dirty, dirty_before)
        finally:
            self.paths["source"].write_bytes(source_bytes)
        bridge.requestFilmDesignImport()
        self.assertEqual(requested, [True])
        self.assertEqual(bridge.property("filmDesignImportError"), "")
        self.assertEqual(bridge.document, before)

    def test_process_import_changed_package_requires_explicit_reload_without_replacement(self):
        bridge = ProcessBridge(self.service, str(self.paths["process"]))
        self.bridges.append(bridge)
        # Represent an already-open package whose on-disk version changed.
        # The import preflight must not take preview's implicit reload route.
        self.enterContext(patch.object(bridge, "_package_runtime",
                                      {"machine": deepcopy(bridge._machine_runtime)}))
        bridge._reload_origin = self.paths["process"]
        self.enterContext(patch.object(bridge, "_package_signature", ("older-package-version",)))
        before, dirty_before = deepcopy(bridge.document), bridge.dirty
        requested, ready = [], []
        bridge.filmDesignImportRequested.connect(lambda: requested.append(True))
        bridge.filmDesignImportReady.connect(lambda: ready.append(True))
        with patch.object(bridge, "_reload_now", side_effect=AssertionError("unexpected package reload")):
            bridge.requestFilmDesignImport()
        self.assertEqual(requested, [])
        self.assertEqual(len(ready), 1)
        self.assertIn("资产包已变化", bridge.property("filmDesignImportError"))
        self.assertEqual(bridge.document, before)
        self.assertEqual(bridge.dirty, dirty_before)

    def test_machine_and_mask_dependency_editors_follow_loaded_paths(self):
        machine = MachineBridge(self.service)
        machine.loadDocument(str(self.paths["machine"]))
        machine_window = self._load_window(machine, "MachineWindow.qml")

        source_action = self._quick_item_by_object_name(
            machine_window, "sourceEditorActionButton")
        mask_action = self._quick_item_by_object_name(
            machine_window, "maskEditorActionButton")
        self.assertIsNotNone(source_action)
        self.assertIsNotNone(mask_action)
        self.assertEqual(source_action.property("text"), "编辑蒸发源")
        self.assertEqual(mask_action.property("text"), "编辑修正板")
        self.assertIsNone(self._quick_item_by_object_name(machine_window, "sourceFileField0"))
        self.assertIsNone(self._quick_item_by_object_name(machine_window, "maskFileField0"))
        self._scroll_item_into_view(source_action)
        self._click_item(machine_window, source_action, "sourceEditorActionButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("source", str(self.paths["source"].resolve())))
        self._scroll_item_into_view(mask_action)
        self._click_item(machine_window, mask_action, "maskEditorActionButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("mask", str(self.paths["mask"].resolve())))

        machine.setValue("/sources/0/file", '""')
        machine.setValue("/masks/0/file", '""')
        self._settle()
        source_action = self._quick_item_by_object_name(
            machine_window, "sourceEditorActionButton")
        mask_action = self._quick_item_by_object_name(
            machine_window, "maskEditorActionButton")
        self.assertIsNotNone(source_action)
        self.assertIsNotNone(mask_action)
        self.assertEqual(source_action.property("text"), "新建")
        self.assertEqual(mask_action.property("text"), "新建")
        self._scroll_item_into_view(source_action)
        self._click_item(machine_window, source_action, "sourceEditorActionButton")
        self.assertEqual(self.launcher.calls[-1], ("source", ""))
        self._scroll_item_into_view(mask_action)
        self._click_item(machine_window, mask_action, "maskEditorActionButton")
        self.assertEqual(self.launcher.calls[-1], ("mask", ""))

        mask = MaskBridge(self.service)
        mask.loadDocument(str(self.paths["mask"]))
        mask_window = self._load_window(mask, "MaskWindow.qml")
        mask_action = mask_window.findChild(QQuickItem, "machineDependencyActionButton")
        self.assertEqual(mask_action.property("text"), "编辑机器")
        self.assertFalse(mask_action.isVisible(), "a mask without a machine has no create-machine action")
        mask._machine_path = str(self.paths["machine"].resolve())
        mask.machineChanged.emit()
        self._settle()
        self.assertTrue(mask_action.isVisible())
        self.assertEqual(mask_action.property("text"), "编辑机器")
        self._click(mask_window, "machineDependencyActionButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("machine", str(self.paths["machine"].resolve())))

    def test_dirty_document_open_stays_on_current_file_until_confirmed(self):
        bridge = ProcessBridge(self.service, str(self.paths["process"]))
        bridge.setField("name", "Unsaved current document")
        bridge.openDocument(str(self.paths["process"]))
        self.assertEqual(bridge.currentPath, str(self.paths["process"].resolve()))
        self.assertEqual(bridge.document["name"], "Unsaved current document")
        self.assertIsNotNone(bridge._pending_document_replace)
        self.assertFalse(bridge.confirmDocumentReplace(False))
        self.assertEqual(bridge.document["name"], "Unsaved current document")

    def test_fast_cycle_selector_content_width_and_text_at_dpi_scale(self):
        screenshot_root = self._screenshot_root()
        scale = os.environ.get("QT_SCALE_FACTOR", "1")
        screenshots = []

        for mode in ("light", "dark"):
            self.theme.setMode(mode)
            bridge = MainBridge(self.service)
            bridge.setFastCycle(True)
            window = self._load_window(bridge, "MainWindow.qml")
            combo = window.findChild(QQuickItem, "fastCycleSelector")
            self.assertIsNotNone(combo)
            self.assertEqual(combo.property("currentIndex"), 1)

            model = [str(item) for item in combo.property("model")]
            self.assertEqual(model, ["真实时长积分", "周期快算"])
            content_item = combo.property("contentItem")
            self.assertIsNotNone(content_item)
            control_width = float(combo.property("width"))
            self.assertGreaterEqual(control_width, 219.0)

            measurements = []
            for index, expected in enumerate(model):
                combo.setProperty("currentIndex", index)
                self._settle()
                self.assertEqual(combo.property("currentText"), expected)
                self.assertEqual(combo.property("displayText"), expected)
                self.assertEqual(content_item.property("text"), expected)

                content_width = float(content_item.property("width"))
                available_width = (
                    control_width
                    - float(combo.property("leftPadding"))
                    - float(combo.property("rightPadding")))
                self.assertAlmostEqual(content_width, available_width, delta=1.0)
                self.assertGreaterEqual(content_width, 150.0)
                self.assertFalse(content_item.property("truncated"))
                measurements.append((content_width, available_width))

            combo.setProperty("currentIndex", 1)
            self._settle()
            scroll = window.findChild(QQuickItem, "simulationInputScroll")
            combo_y = combo.mapToItem(scroll.parentItem(), QPointF(0, 0)).y() - scroll.y()
            max_scroll = max(0.0, scroll.property("contentHeight") - scroll.height())
            scroll.setProperty("contentY", max(
                0.0, min(combo_y - scroll.height() / 2, max_scroll)))
            self._settle()
            screenshots.append(self._save_render(
                window, screenshot_root,
                f"MainWindow-fast-cycle-{scale}-{mode}.png"))
            print(
                f"FAST_CYCLE_SELECTOR scale={scale} mode={mode} "
                f"control={control_width:.1f}px "
                f"content={measurements[1][0]:.1f}px "
                f"available={measurements[1][1]:.1f}px truncated=False")
            window.close()
            self._settle()

        self.assertEqual(len(screenshots), 2)
        print("FAST_CYCLE_SCREENSHOTS=" + str(screenshot_root.resolve()))

    def test_compact_process_and_machine_layout_is_responsive_and_scrollable(self):
        scale = os.environ.get("QT_SCALE_FACTOR", "1")
        scale_tag = scale.replace(".", "p")
        screenshot_root = self._screenshot_root()

        def capture_pair(window, stem):
            for mode in ("light", "dark"):
                self.theme.setMode(mode)
                self._settle()
                path = self._save_render(
                    window, screenshot_root, f"{stem}-{scale_tag}-{mode}.png")
                print(f"COMPACT_LAYOUT_SCREENSHOT {path.resolve()}")

        def viewport_bounds(view, item):
            point = item.mapToItem(view, QPointF(0, 0))
            return (point.y(), point.y() + float(item.property("height")))

        process_bridge = ProcessBridge(
            self.service, str(self.paths["process"]))
        process_window = self._load_window(process_bridge, "ProcessWindow.qml")
        name_label = self._quick_item_by_object_name(
            process_window, "processNameLabel")
        machine_label = self._quick_item_by_object_name(
            process_window, "processMachineLabel")
        substrate_label = self._quick_item_by_object_name(
            process_window, "processSubstrateLabel")
        name_field = self._quick_item_by_object_name(
            process_window, "processNameField")
        machine_field = self._quick_item_by_object_name(
            process_window, "processMachineField")
        substrate_field = self._quick_item_by_object_name(
            process_window, "processSubstrateField")
        for item in (name_label, machine_label, substrate_label,
                     name_field, machine_field, substrate_field):
            self.assertIsNotNone(item)
        label_x = [item.mapToItem(process_window.contentItem(), QPointF(0, 0)).x()
                   for item in (name_label, machine_label, substrate_label)]
        field_x = [item.mapToItem(process_window.contentItem(), QPointF(0, 0)).x()
                   for item in (name_field, machine_field, substrate_field)]
        self.assertLess(max(label_x) - min(label_x), 1.0)
        self.assertLess(max(field_x) - min(field_x), 1.0)
        send_button = self._quick_item_by_object_name(
            process_window, "sendToSimulationButton")
        self.assertIsNotNone(send_button)
        self.assertTrue(send_button.property("primary"))
        self._click(process_window, "machineDesignActionButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("machine", str(self.paths["machine"].resolve())))
        self._click(process_window, "substrateDesignActionButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("substrate", str(self.paths["substrate"].resolve())))
        self._click(process_window, "sendToSimulationButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("main", str(self.paths["process"].resolve())))
        capture_pair(process_window, "ProcessWindow-ready")
        environment_button = self._quick_item_by_object_name(
            process_window, "editEnvironmentDefaults")
        self.assertIsNotNone(environment_button)
        self._scroll_item_into_view(environment_button)
        self._click_item(process_window, environment_button,
                         "editEnvironmentDefaults")
        QTest.keyClick(process_window, Qt.Key_Escape)
        self._settle()

        process_bridge.setField("machine_file", "")
        self._settle()
        self.assertTrue(process_window.property("previewEmpty"))
        self.assertEqual(process_bridge.dependencyPath("machine_file"), "")
        self._click(process_window, "machineDesignActionButton")
        self.assertEqual(self.launcher.calls[-1], ("machine", ""))
        preview_frame = self._quick_item_by_object_name(
            process_window, "processPlanFrame")
        self.assertIsNotNone(preview_frame)
        empty_preview_height = float(preview_frame.property("height"))
        self.assertLess(empty_preview_height, 150.0)
        self.assertEqual(
            self.service.load("process", self.paths["process"]).data["machine_file"],
            "machine.cmac")
        process_window.resize(920, 680)
        self._settle()
        self.assertEqual(int(process_window.width()), 920)
        actions = self._quick_item_by_object_name(process_window, "processActions")
        batch_button = self._quick_item_by_object_name(
            process_window, "batchFilmStackButton")
        self.assertIsNotNone(actions)
        self.assertIsNotNone(batch_button)
        self.assertFalse(batch_button.isVisible())
        process_scroll = self._quick_item_by_object_name(
            process_window, "processEditorScroll")
        self.assertGreater(float(process_scroll.property("contentHeight")),
                           float(process_scroll.property("height")))
        process_scroll.setProperty("contentY", 0)
        self._settle()
        capture_pair(process_window, "ProcessWindow-empty-narrow")

        before_steps = process_bridge.stepCount
        add_step = self._quick_item_by_object_name(process_window, "addStepButton")
        self._scroll_item_into_view(add_step)
        top, bottom = viewport_bounds(process_scroll, add_step)
        self.assertGreaterEqual(top, -1.0)
        self.assertLessEqual(bottom, float(process_scroll.property("height")) + 1.0)
        self._click_item(process_window, add_step, "addStepButton")
        self.assertEqual(process_bridge.stepCount, before_steps + 1)
        # Correction-mask optimization belongs to MaskWindow. The process
        # editor keeps its existing scroll/navigation space without reserving
        # an invisible sidebar after the optimization entry moves.
        for removed in ("processOptimizationSidebar", "optimizationPanelToggle",
                        "startOptimizationButton", "optimizationGridSizeField"):
            self.assertIsNone(self._quick_item_by_object_name(process_window, removed))
        process_workspace = self._quick_item_by_object_name(
            process_window, "processWorkspace")
        self.assertIsNotNone(process_workspace)
        self.assertAlmostEqual(float(process_scroll.width()),
                               float(process_workspace.width()), delta=1.0)
        self.assertGreater(float(process_scroll.width()),
                           float(process_window.width()) - 50.0)
        self._scroll_item_into_view(add_step)
        top, bottom = viewport_bounds(process_scroll, add_step)
        self.assertGreaterEqual(top, -1.0)
        self.assertLessEqual(bottom, float(process_scroll.property("height")) + 1.0)
        capture_pair(process_window, "ProcessWindow-bottom-narrow")
        process_window.close()
        self._settle()

        machine_bridge = MachineBridge(self.service)
        machine_bridge.loadDocument(str(self.paths["machine"]))
        machine_window = self._load_window(machine_bridge, "MachineWindow.qml")
        geometry_names = [
            "geometryField_carrier_x_mm", "geometryField_carrier_y_mm",
            "geometryField_planet_offset_mm", "geometryField_planet_tilt_deg",
            "geometryField_substrate_z_mm", "geometryField_sun_teeth",
            "geometryField_planet_teeth", "geometryField_gear_module_mm",
            "solarRadiusField", "planetRadiusField",
        ]
        geometry_fields = [self._quick_item_by_object_name(machine_window, name)
                           for name in geometry_names]
        self.assertTrue(all(geometry_fields), "all machine geometry fields must render")
        geometry_widths = [float(field.property("width")) for field in geometry_fields]
        self.assertLessEqual(max(geometry_widths), 100.0)
        top_preview = self._quick_item_by_object_name(
            machine_window, "machinePlanPreview")
        side_preview = self._quick_item_by_object_name(
            machine_window, "machineSidePreview")
        self.assertIsNotNone(top_preview)
        self.assertIsNotNone(side_preview)
        self.assertGreater(float(top_preview.property("width")),
                           float(machine_window.width()) * 0.48)
        self.assertLessEqual(float(side_preview.property("height")), 230.0)
        capture_pair(machine_window, "MachineWindow-geometry-normal")

        machine_window.resize(920, 680)
        self._settle()
        self.assertEqual(int(machine_window.width()), 920)
        settings_scroll = self._quick_item_by_object_name(
            machine_window, "machineSettingsScroll")
        self.assertGreater(float(settings_scroll.property("contentHeight")),
                           float(settings_scroll.property("height")))
        source_action = self._quick_item_by_object_name(
            machine_window, "sourceEditorActionButton")
        mask_action = self._quick_item_by_object_name(
            machine_window, "maskEditorActionButton")
        self.assertIsNotNone(source_action)
        self.assertIsNotNone(mask_action)
        self.assertTrue(all(float(field.property("width")) <= 100.0
                            for field in geometry_fields))
        capture_pair(machine_window, "MachineWindow-narrow-top")
        self._scroll_item_into_view(source_action)
        top, bottom = viewport_bounds(settings_scroll, source_action)
        self.assertGreaterEqual(top, -1.0)
        self.assertLessEqual(bottom, float(settings_scroll.property("height")) + 1.0)
        self._click_item(machine_window, source_action, "sourceEditorActionButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("source", str(self.paths["source"].resolve())))
        self._scroll_item_into_view(mask_action)
        self._click_item(machine_window, mask_action, "maskEditorActionButton")
        self.assertEqual(self.launcher.calls[-1],
                         ("mask", str(self.paths["mask"].resolve())))
        add_monitor = self._quick_item_by_object_name(
            machine_window, "addMonitorButton")
        self.assertIsNotNone(add_monitor)
        self._scroll_item_into_view(add_monitor)
        top, bottom = viewport_bounds(settings_scroll, add_monitor)
        self.assertGreaterEqual(top, -1.0)
        self.assertLessEqual(bottom, float(settings_scroll.property("height")) + 1.0)
        self._click_item(machine_window, add_monitor, "addMonitorButton")
        capture_pair(machine_window, "MachineWindow-narrow-bottom")

    def test_all_four_pages_render_both_themes_and_main_controls(self):
        screenshot_root = self._screenshot_root()
        screenshots = []

        for mode in ("light", "dark"):
            self.theme.setMode(mode)
            self._settle()
            main_bridge = MainBridge(self.service)
            main_bridge.setFastCycle(True)
            pages = [
                ("MainWindow", "MainWindow.qml", main_bridge),
                ("ProcessWindow", "ProcessWindow.qml",
                 ProcessBridge(self.service, str(self.paths["process"]))),
            ]
            machine = MachineBridge(self.service)
            machine.loadDocument(str(self.paths["machine"]))
            pages.append(("MachineWindow", "MachineWindow.qml", machine))
            mask = MaskBridge(self.service)
            mask.loadDocument(str(self.paths["mask"]))
            pages.append(("MaskWindow", "MaskWindow.qml", mask))

            for page_name, qml_name, bridge in pages:
                window = self._load_window(bridge, qml_name)
                expected_window = self.theme.snapshot["colorWindow"]
                self.assertEqual(window.color().name(), expected_window.lower())
                screenshot = self._save_render(
                    window, screenshot_root, f"{page_name}-{mode}.png")
                screenshots.append(screenshot)

                if page_name == "MainWindow":
                    fast_cycle = window.findChild(QQuickItem, "fastCycleSelector")
                    self.assertIsNotNone(fast_cycle)
                    selector_width = float(fast_cycle.property("width"))
                    self.assertGreaterEqual(
                        selector_width, 219.0,
                        f"fast cycle selector width is {selector_width}px")
                    self.assertEqual(fast_cycle.property("currentIndex"), 1)
                    self.assertEqual(fast_cycle.property("currentText"), "周期快算")
                    model = [str(item) for item in fast_cycle.property("model")]
                    self.assertEqual(model, ["真实时长积分", "周期快算"])
                    content_item = fast_cycle.property("contentItem")
                    self.assertIsNotNone(content_item)
                    for index, expected_text in enumerate(model):
                        fast_cycle.setProperty("currentIndex", index)
                        self._settle()
                        self.assertEqual(fast_cycle.property("currentText"), expected_text)
                        self.assertEqual(fast_cycle.property("displayText"), expected_text)
                        self.assertEqual(content_item.property("text"), expected_text)
                        content_width = float(content_item.property("width"))
                        available_width = (
                            selector_width
                            - float(fast_cycle.property("leftPadding"))
                            - float(fast_cycle.property("rightPadding")))
                        self.assertGreaterEqual(content_width, 150.0)
                        self.assertGreaterEqual(available_width, 150.0)
                        self.assertAlmostEqual(content_width, available_width, delta=1.0)
                        self.assertFalse(content_item.property("truncated"))
                    fast_cycle.setProperty("currentIndex", 1)
                    self._settle()
                    print(
                        f"FAST_CYCLE_SELECTOR scale={os.environ.get('QT_SCALE_FACTOR', '1')} "
                        f"control={selector_width:.1f}px content={content_width:.1f}px "
                        f"available={available_width:.1f}px "
                        f"truncated={content_item.property('truncated')}")
                    scroll = window.findChild(QQuickItem, "simulationInputScroll")
                    panel = window.findChild(QQuickItem, "simulationBottomPanel")
                    self.assertIsNotNone(scroll)
                    self.assertIsNotNone(panel)
                    viewport_bottom = scroll.mapToItem(
                        panel.parentItem(), QPointF(0, scroll.height())).y()
                    self.assertLessEqual(viewport_bottom, panel.y() + 0.5)
                    max_scroll = max(0.0, scroll.property("contentHeight") - scroll.height())
                    scroll.setProperty("contentY", max_scroll)
                    self._settle()
                    self.assertAlmostEqual(scroll.property("contentY"), max_scroll, places=2)

                    disabled = window.findChild(QQuickItem, "calculateButton")
                    self.assertIsNotNone(disabled)
                    self.assertFalse(disabled.isEnabled())
                    scroll.setProperty("contentY", 0)
                    self._settle()
                    fast_cycle_y = (
                        fast_cycle.mapToItem(scroll.parentItem(), QPointF(0, 0)).y()
                        - scroll.y())
                    max_scroll = max(
                        0.0, scroll.property("contentHeight") - scroll.height())
                    scroll.setProperty("contentY", max(
                        0.0, min(fast_cycle_y - scroll.height() / 2, max_scroll)))
                    self._settle()
                    screenshots.append(self._save_render(
                        window, screenshot_root,
                        f"MainWindow-{mode}-fast-cycle.png"))
                    scroll.setProperty("contentY", 0)
                    self._settle()
                    self._click(window, "processEditorActionButton")
                    focus_button = window.findChild(QQuickItem, "processEditorActionButton")
                    self.assertTrue(focus_button.property("activeFocus"))
                    screenshots.append(self._save_render(
                        window, screenshot_root, f"MainWindow-{mode}-focus-disabled.png"))

                    combo = window.findChild(QQuickItem, "algorithmSelector")
                    input_scroll = window.findChild(QQuickItem, "simulationInputScroll")
                    combo_y = (combo.mapToItem(input_scroll.parentItem(), QPointF(0, 0)).y()
                               - input_scroll.y())
                    input_scroll.setProperty("contentY", max(
                        0.0, min(combo_y - input_scroll.height() / 2,
                                 input_scroll.property("contentHeight") - input_scroll.height())))
                    self._settle()
                    self._click(window, "algorithmSelector")
                    # QQuickComboBox exposes popup as QQuickPopup*, which
                    # PySide cannot convert directly. Its public `down`
                    # property tracks the open popup state.
                    self.assertTrue(combo.property("down"))
                    screenshots.append(self._save_render(
                        window, screenshot_root, f"MainWindow-{mode}-popup.png"))
                    self._click(window, "algorithmSelector")

                window.close()
                self._settle()

        self.assertEqual(len(screenshots), 14)
        print("THEME_SCREENSHOTS=" + str(screenshot_root.resolve()))


if __name__ == "__main__":
    unittest.main()
