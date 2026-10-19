"""Focused contract checks for the Qt Quick film-design controller.

These tests cover document/model behavior and offscreen Qt Quick interactions;
they do not start a visible window or run the optical spectrum solver.
"""

from __future__ import annotations

import math
import json
import os
import tempfile
import threading
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QTimer, QUrl, Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression
from PySide6.QtTest import QTest

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.core import materials as materials_module
from coat_sim.core.materials import default_material_profile
from coat_sim.ui import launcher as launcher_module
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.fonts import bundled_font_family
from coat_sim.ui.theme_service import ThemeService
from launcher_test_isolation import LauncherTestIsolation
from tests.qt_test_guard import NoNetworkAccessManagerFactory


_APP = QGuiApplication.instance() or QGuiApplication([])
_CJK_FONT_FAMILY = bundled_font_family()
if _CJK_FONT_FAMILY:
    _APP.setFont(QFont(_CJK_FONT_FAMILY))


def _visual_window_for(item: QQuickItem) -> QQuickWindow:
    """Locate the owning surface without QQuickItem.window() wrapper reparenting."""
    for window in QGuiApplication.allWindows():
        if isinstance(window, QQuickWindow):
            content = window.contentItem()
            if content is item or content.isAncestorOf(item):
                return window
    raise AssertionError(f"no Qt Quick surface contains {item.objectName()!r}")


class FilmDesignQuickControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = FilmDesignService(self.root)
        self.isolation = LauncherTestIsolation(
            self.root / "launcher-isolation", launcher_module).apply()
        # Isolation copies the shipped optical catalogue without synthetic Air.
        self.controller = FilmDesignQuickController(
            self.root, service=self.service,
            geometry_settings=self.isolation.create_settings())

    def wait_for_initialization(self, controller, timeout=8.0):
        deadline = perf_counter() + timeout
        while perf_counter() < deadline:
            QCoreApplication.processEvents()
            if controller.initializationState != "loading":
                return controller.initializationState
            QTest.qWait(10)
        return controller.initializationState

    def stable_id_for_symbol(self, symbol):
        for stable_id in self.controller.expression_symbols.ids():
            row = self.controller.expression_symbols.record_for_id(stable_id)
            if row["symbol"] == symbol:
                return stable_id
        self.fail(f"symbol {symbol!r} is not present")

    def tearDown(self):
        self.controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _APP.processEvents()
        self.isolation.stop()
        self.temporary.cleanup()

    def install_film_fixtures(self):
        def optical_set(identifier, index):
            return {
                "id": identifier,
                "name": identifier,
                "kind": "tabulated",
                "notes": "Synthetic unit-test data",
                "source": "Local unit-test fixture",
                "wavelength_unit": "nm",
                "wavelength_min": 400.0,
                "wavelength_max": 700.0,
                "wavelengths": [400.0, 550.0, 700.0],
                "n": [index, index, index],
                "k": None,
            }

        profile = default_material_profile("Quick controller fixture", id="quick_fixture")
        profile.update(
            category="film",
            roles=["film", "medium"],
            optical_constants=[optical_set("n15", 1.5), optical_set("n20", 2.0)],
        )
        fixture_path = self.root / "data" / "materials" / (profile["id"] + ".json")
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        self.controller.film_options = self.service.material_options(purpose="film")
        self.controller.medium_options = self.service.material_options(purpose="medium")
        self.controller._refresh_picker_options()

    @staticmethod
    def write_design_fixture(path, design):
        path.write_text(json.dumps(design, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    def test_dataset_picker_rows_are_built_for_selected_material_only(self):
        self.assertEqual(self.controller._dataset_picker_model_cache,
                         {"film": {}, "substrate": {}, "medium": {}})
        material = next(row for row in self.controller.film_options
                        if row["id"] == "builtin_zns_literature_20261004")
        expected_ids = [row["id"] for row in material["optical_sets"]]
        self.assertTrue(expected_ids)

        film_rows = self.controller.opticalSetsForFilm(material["id"])
        self.assertEqual([row["id"] for row in film_rows], [""] + expected_ids)
        self.assertIs(self.controller.opticalSetsForFilm(material["id"]), film_rows)
        self.assertEqual(self.controller._dataset_picker_model_cache["film"].keys(),
                         {material["id"]})
        self.assertIn("n ", film_rows[1]["value_range_text"])
        self.assertNotIn("n", film_rows[1])
        self.assertNotIn("wavelengths", film_rows[1])
        self.assertEqual(self.controller._dataset_picker_model_cache["substrate"], {})
        self.assertEqual(self.controller._dataset_picker_model_cache["medium"], {})

    def test_async_initialization_overlay_cancel_and_instances_are_isolated(self):
        original = self.service.material_options_many
        entered = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        calls = 0

        def block_first_catalog_read(purposes):
            nonlocal calls
            with lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                entered.set()
                if not release.wait(8):
                    raise TimeoutError("synthetic material read was not released")
            return original(purposes)

        controller_a = controller_b = None
        engine = root = None
        with patch.object(self.service, "material_options_many",
                          side_effect=block_first_catalog_read):
            try:
                controller_a = FilmDesignQuickController(
                    self.root, service=self.service,
                    geometry_settings=self.isolation.create_settings(),
                    defer_initialization=True)
                self.assertTrue(entered.wait(3), "catalog read did not start")
                self.assertEqual(controller_a.initializationState, "loading")
                self.assertFalse(controller_a.initializationReady)
                self.assertIsNone(controller_a.session)
                placeholder_name = controller_a._snapshot.design.get("name", "")

                engine, _component, root = self.create_quick_window(controller_a)
                overlay = root.findChild(QQuickItem, "filmDesignInitializationOverlay")
                front_tab = root.findChild(QQuickItem, "filmDesignTabFront")
                self.assertIsNotNone(overlay)
                self.assertTrue(overlay.property("visible"))
                self.assertIsNotNone(front_tab)
                self.assertFalse(front_tab.property("enabled"))

                controller_b = FilmDesignQuickController(
                    self.root, service=self.service,
                    geometry_settings=self.isolation.create_settings(),
                    defer_initialization=True)
                self.assertEqual(self.wait_for_initialization(controller_b), "ready")
                self.assertIsNotNone(controller_b.session)
                self.assertTrue(controller_b.setDesignValue("name", "second instance"))
                self.assertEqual(controller_a.initializationState, "loading")
                self.assertEqual(controller_a._snapshot.design.get("name", ""),
                                 placeholder_name)

                root.close()
                self.assertEqual(controller_a.initializationState, "cancelled")
                release.set()
                deadline = perf_counter() + 5
                while perf_counter() < deadline and controller_a._initialization_workers:
                    QCoreApplication.processEvents()
                    QTest.qWait(10)
                QCoreApplication.processEvents()
                self.assertFalse(controller_a._initialization_workers)
                self.assertEqual(controller_a.initializationState, "cancelled")
                self.assertIsNone(controller_a.session)
                self.assertTrue(controller_b.initializationReady)
                self.assertIsNot(controller_a.session, controller_b.session)
            finally:
                release.set()
                if engine is not None and root is not None:
                    self.dispose_quick_window(engine, root)
                for controller in (controller_a, controller_b):
                    if controller is not None:
                        controller.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                _APP.processEvents()

    def test_async_initialization_failure_can_be_retried(self):
        original_init = FilmDesignEditSession.__init__
        calls = 0

        def fail_first_initialization(session, service):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("synthetic catalog read failure")
            original_init(session, service)

        controller = FilmDesignQuickController(
            self.root, service=self.service,
            geometry_settings=self.isolation.create_settings(),
            defer_initialization=True)
        engine = root = None
        try:
            with patch.object(FilmDesignEditSession, "__init__",
                              new=fail_first_initialization):
                self.assertEqual(self.wait_for_initialization(controller), "failed")
                self.assertIn("synthetic catalog read failure",
                              controller.initializationError)
                self.assertIsNone(controller.session)
            engine, _component, root = self.create_quick_window(controller)
            overlay = root.findChild(QQuickItem, "filmDesignInitializationOverlay")
            retry = root.findChild(QQuickItem, "filmDesignInitializationRetry")
            self.assertIsNotNone(overlay)
            self.assertTrue(overlay.property("visible"))
            self.assertIsNotNone(retry)
            self.assertTrue(retry.isVisible())
            controller.retryInitialization()
            self.assertEqual(self.wait_for_initialization(controller), "ready")
            self.assertFalse(overlay.property("visible"))
            self.assertTrue(controller.initializationReady)
            design = controller.currentDesign()
            self.assertEqual(set(design["expression"]["symbols"]), {"H", "L"})
            self.assertEqual(design["incident_medium"]["material_id"], "builtin_ideal_air")
            self.assertEqual(design["exit_medium"]["material_id"], "builtin_ideal_air")
        finally:
            if engine is not None and root is not None:
                self.dispose_quick_window(engine, root)
            controller.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            _APP.processEvents()

    def test_new_design_has_default_h_l_symbols_without_layers_or_expression(self):
        design = self.controller.currentDesign()
        symbols = design["expression"]["symbols"]

        self.assertEqual(list(symbols), ["H", "L"])
        self.assertEqual(self.controller.expression_symbols.rowCount(), 2)
        self.assertEqual(
            [self.controller.expression_symbols.record_for_id(stable_id)["symbol"]
             for stable_id in self.controller.expression_symbols.ids()],
            ["H", "L"],
        )
        for symbol in ("H", "L"):
            self.assertEqual(symbols[symbol]["material_id"], "")
            self.assertEqual(symbols[symbol]["optical_set_id"], "")
            self.assertEqual(symbols[symbol]["thickness"], {"value": 1.0, "unit": "qw"})
            self.assertEqual(symbols[symbol]["note"], "")
        self.assertEqual(design["front_layers"], [])
        self.assertEqual(design["back_layers"], [])
        self.assertEqual(design["expression"]["text"], "")

    def test_new_design_keeps_air_as_default_incident_and_exit_material(self):
        air = {"kind": "material", "material_id": "builtin_ideal_air",
               "optical_set_id": "ideal_air_n1_k0"}
        design = self.controller.currentDesign()
        self.assertEqual(design["incident_medium"], air)
        self.assertEqual(design["exit_medium"], air)
        self.assertEqual(self.controller.mediumOptions[0]["id"], "builtin_ideal_air")
        self.assertNotIn("optical_sets", self.controller.mediumOptions[0])
        air_option = next(row for row in self.controller.medium_options
                          if row["id"] == "builtin_ideal_air")
        self.assertEqual(air_option["optical_sets"][0]["id"], "ideal_air_n1_k0")

    def test_opening_empty_symbol_table_does_not_add_default_symbols(self):
        path = self.root / "empty-expression.cfilm"
        self.service.save(path, self.service.new_design())

        self.controller.requestOpenFile(QUrl.fromLocalFile(str(path)))

        self.assertEqual(self.controller.currentDesign()["expression"]["symbols"], {})
        self.assertEqual(self.controller.expression_symbols.rowCount(), 0)

    def test_deleting_default_symbol_stays_deleted_after_save_and_reload(self):
        self.assertTrue(self.controller.deleteSymbol(self.stable_id_for_symbol("H")))
        self.assertEqual(list(self.controller.currentDesign()["expression"]["symbols"]), ["L"])

        path = self.root / "deleted-default.cfilm"
        self.assertTrue(self.controller.saveFilePath(str(path), False))
        reopened = FilmDesignQuickController(self.root, service=self.service)
        reopened.requestOpenFile(QUrl.fromLocalFile(str(path)))

        self.assertEqual(list(reopened.currentDesign()["expression"]["symbols"]), ["L"])
        self.assertEqual(reopened.expression_symbols.rowCount(), 1)

    def create_quick_window(self, controller=None):
        engine = QQmlApplicationEngine()
        self.network_factory = NoNetworkAccessManagerFactory()
        engine.setNetworkAccessManagerFactory(self.network_factory)
        ThemeService.instance().set_theme("light")
        ThemeService.instance().attach_engine(engine, "Theme")
        active_controller = controller or self.controller
        component = QQmlComponent(
            engine,
            QUrl.fromLocalFile(str(Path(__file__).parents[1] / "coat_sim/ui/qml/FilmDesignQuick.qml")),
        )
        root = component.createWithInitialProperties({"controller": active_controller})
        if root is None:
            errors = "\n".join(error.toString() for error in component.errors())
            engine.deleteLater()
            self.fail(f"FilmDesignQuick.qml failed to load: {errors}")
        root.show()
        QTest.qWait(100)
        return engine, component, root

    def dispose_quick_window(self, engine, root):
        # Test edits can leave the product's dirty-close confirmation pending.
        # Bypass only that UI prompt while reliably disposing the test window.
        try:
            root.setProperty("allowClose", True)
            root.hide()
            root.close()
            root.deleteLater()
        except RuntimeError:
            pass
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _APP.processEvents()

    def test_qml_f5_f6_commit_focused_edits_and_start_one_pending_optimizer(self):
        class FakeOptimizer:
            def __init__(self, source):
                self._source_controller = source
                self.session = source.session
                self.busy = False
                self.start_count = 0

            def startOptimizationFromShortcut(self):
                if self.busy:
                    return False
                self.busy = True
                self.start_count += 1
                return True

        class ShortcutLauncher(QObject):
            def __init__(self, source):
                super().__init__()
                self.source = source
                self.optimizer = FakeOptimizer(source)
                self._windows = {}
                self._open_requests = {}
                self._pending_qml = {}
                self._film_companion_auto_start = set()
                self.simulation_requests = []
                self.opens = []

            def open_window(self, key, *, mode, payload):
                self.opens.append((key, mode, payload))
                identity = f"shortcut-optimizer-{len(self.opens)}"
                self._open_requests[identity] = (
                    SimpleNamespace(key=key),
                    SimpleNamespace(instance_id=identity, payload=payload),
                )
                self._pending_qml[identity] = SimpleNamespace(
                    bridge=self.optimizer, on_ready=lambda *_args: None)
                return identity

            def _activate_window(self, _window):
                pass

            def open_or_reuse_film_companion(self, module_key, source, *, start=False):
                if module_key == "film_simulation":
                    snapshot = source.session.snapshot()
                    self.simulation_requests.append((start, snapshot))
                    return "shortcut-simulation"
                return launcher_module.LauncherBridge.open_or_reuse_film_companion(
                    self, module_key, source, start=start)

        self.install_film_fixtures()
        self.assertEqual(self.wait_for_initialization(self.controller), "ready")
        self.assertTrue(self.controller.addLayer("front"))
        layer_id = self.controller.front_layers.ids()[0]
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "material_id", "quick_fixture"))
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "optical_set_id", "n15"))
        launcher = ShortcutLauncher(self.controller)
        self.shortcut_launcher = launcher
        self.controller.setParent(launcher)
        engine, _component, root = self.create_quick_window()
        try:
            root.requestActivate()
            front_tab = root.findChild(QQuickItem, "filmDesignTabFront")
            self.assertIsNotNone(front_tab)
            front_tab_center = front_tab.mapToScene(
                QPointF(front_tab.width() / 2, front_tab.height() / 2))
            QTest.mouseClick(
                root,
                Qt.LeftButton,
                Qt.NoModifier,
                QPoint(round(front_tab_center.x()), round(front_tab_center.y())),
            )

            def find_visual_item(parent, object_name):
                if parent.objectName() == object_name:
                    return parent
                for child in parent.childItems():
                    found = find_visual_item(child, object_name)
                    if found is not None:
                        return found
                return None

            thickness = None
            expression = None
            for _ in range(150):
                QCoreApplication.processEvents()
                thickness = find_visual_item(
                    root.contentItem(), "filmLayerNm_" + layer_id)
                expression = find_visual_item(
                    root.contentItem(), "filmDesignExpressionArea")
                if thickness is not None and expression is not None:
                    break
                QTest.qWait(10)
            self.assertIsNotNone(thickness)
            self.assertIsNotNone(expression)

            thickness.forceActiveFocus()
            QCoreApplication.processEvents()
            QTest.keyClick(root, Qt.Key_A, Qt.ControlModifier)
            for character in "225":
                QTest.keyClick(root, Qt.Key(ord(character)))
            QCoreApplication.processEvents()
            self.assertGreater(self.controller.pendingEditEpoch, 0)
            self.assertEqual(
                self.controller.session.snapshot().design["front_layers"][0]["thickness"]["value"],
                100.0,
            )
            QTest.keyClick(root, Qt.Key_F5)
            for _ in range(100):
                QCoreApplication.processEvents()
                if launcher.simulation_requests:
                    break
                QTest.qWait(10)
            self.assertEqual(len(launcher.simulation_requests), 1)
            started, f5_snapshot = launcher.simulation_requests[0]
            self.assertTrue(started)
            self.assertEqual(f5_snapshot.design["front_layers"][0]["thickness"]["value"], 225.0)

            # The shared sidebar can start collapsed at a narrow width; the
            # shortcut test explicitly opens its expression section first.
            root.setProperty("sidebarExpanded", True)
            QTest.qWait(20)
            expression.forceActiveFocus()
            QCoreApplication.processEvents()
            QTest.keyClick(root, Qt.Key_A, Qt.ControlModifier)
            for character in "G1 H":
                key = Qt.Key_Space if character == " " else Qt.Key(ord(character.upper()))
                modifier = Qt.ShiftModifier if character.isalpha() else Qt.NoModifier
                QTest.keyClick(root, key, modifier)
            QCoreApplication.processEvents()
            self.assertEqual(self.controller.session.snapshot().design["expression"]["text"], "")
            QTest.keyClick(root, Qt.Key_F6)
            QTest.keyClick(root, Qt.Key_F6)
            QTest.keyClick(root, Qt.Key_F6)
            for _ in range(100):
                QCoreApplication.processEvents()
                if launcher.opens:
                    break
                QTest.qWait(10)
            self.assertEqual(len(launcher.opens), 1)
            self.assertEqual(launcher.opens[0][0], "film_optimization")
            self.assertTrue(launcher.optimizer.busy)
            self.assertEqual(launcher.optimizer.start_count, 1)
            self.assertEqual(
                self.controller.session.snapshot().design["expression"]["text"],
                "G1 H",
            )

            QTest.keyClick(root, Qt.Key_F4)
            for _ in range(100):
                QCoreApplication.processEvents()
                sidebar = root.findChild(QQuickItem, "filmDesignExpressionSidebar")
                if sidebar is not None and (sidebar.height() if root.property("sidebarUsesBottomLayout") else sidebar.width()) <= 65:
                    break
                QTest.qWait(10)
            self.assertIsNotNone(sidebar)
            self.assertLessEqual(
                sidebar.height() if root.property("sidebarUsesBottomLayout") else sidebar.width(), 65)
            screenshot_path = os.environ.get("FILM_SYNC_DESIGN_SHORTCUT_SCREENSHOT")
            if screenshot_path:
                image = root.grabWindow()
                self.assertFalse(image.isNull())
                self.assertTrue(image.save(screenshot_path))
        finally:
            self.dispose_quick_window(engine, root)

    def test_layer_rows_keep_identity_when_reordered_and_edited(self):
        self.assertTrue(self.controller.addLayer("front"))
        self.assertTrue(self.controller.addLayer("front"))
        first, second = self.controller.front_layers.ids()

        self.assertTrue(self.controller.moveLayer("front", second, -1))
        self.assertEqual(self.controller.front_layers.ids(), [second, first])
        self.assertTrue(self.controller.setLayerValue("front", first, "thickness.value", "225"))

        rows = self.controller.currentDesign()["front_layers"]
        self.assertEqual([row["id"] for row in rows], [second, first])
        self.assertEqual(rows[1]["thickness"], {"value": 225.0, "unit": "nm"})

    def test_substrate_thickness_is_independent_of_layer_thickness(self):
        self.controller.addLayer("front")
        layer_id = self.controller.front_layers.ids()[0]
        self.assertTrue(self.controller.setSubstrateValue("thickness.value", "1.25"))
        self.assertTrue(self.controller.setSubstrateValue("thickness.unit", "mm"))
        self.assertTrue(self.controller.setSubstrateValue("sides", "double"))

        design = self.controller.currentDesign()
        self.assertEqual(design["substrate"]["thickness"], {"value": 1.25, "unit": "mm"})
        self.assertEqual(design["substrate"]["sides"], "double")
        self.assertEqual(design["front_layers"][0]["id"], layer_id)
        self.assertEqual(design["front_layers"][0]["thickness"], {"value": 100.0, "unit": "nm"})

    def test_qml_substrate_dataset_callback_uses_controller_owned_model(self):
        self.install_film_fixtures()
        self.controller.substrate_options = self.service.material_options(purpose="substrate")
        self.controller._refresh_picker_options()
        self.assertTrue(self.controller.setSubstrateValue("material_id", "quick_fixture"))
        self.assertEqual(self.controller.designMeta["substrate"]["optical_set_id"], "n15")
        options = self.controller.opticalSetsForSubstrate("quick_fixture")
        first_index = next(index for index, row in enumerate(options) if row["id"] == "n15")
        second_index = next(index for index, row in enumerate(options) if row["id"] == "n20")
        engine, _component, root = self.create_quick_window()
        try:
            # Exercise the window callback with its real controller model before
            # loading the page. A page-local QML id cannot be used by this scope.
            def select_dataset(index):
                query = QQmlExpression(
                    QQmlEngine.contextForObject(root), root,
                    f"commitSubstrateSetSelection({index})",
                )
                query.evaluate()
                self.assertFalse(query.hasError(), query.error().toString())
                QCoreApplication.processEvents()

            select_dataset(second_index)
            self.assertEqual(self.controller.currentDesign()["substrate"]["optical_set_id"], "n20")
            revision = self.controller.session.snapshot().design_revision
            select_dataset(second_index)
            self.assertEqual(self.controller.session.snapshot().design_revision, revision)
            select_dataset(first_index)
            self.assertEqual(self.controller.currentDesign()["substrate"]["optical_set_id"], "n15")
            self.assertTrue(self.controller.setSubstrateValue("optical_set_id", ""))
            self.assertEqual(self.controller.currentDesign()["substrate"]["optical_set_id"], "")
            select_dataset(second_index)
            self.assertEqual(self.controller.currentDesign()["substrate"]["optical_set_id"], "n20")
        finally:
            self.dispose_quick_window(engine, root)

    def test_dual_thickness_fields_keep_schema_and_report_missing_n(self):
        self.assertTrue(self.controller.addLayer("front"))
        layer_id = self.controller.front_layers.ids()[0]
        row = self.controller.front_layers.record_for_id(layer_id)
        self.assertEqual(row["physicalNmText"], "100.000")
        self.assertEqual(row["physicalNmValue"], 100.0)
        self.assertEqual(row["qwText"], "n不可用")
        self.assertFalse(self.controller.setLayerValue("front", layer_id, "thickness.nm", "125"))
        self.assertIn("有效膜层材料", self.controller.state["error"])
        self.assertFalse(self.controller.setLayerValue("front", layer_id, "thickness.qw", "1.5"))
        self.assertIn("QW", self.controller.state["error"])
        stored = self.controller.currentDesign()["front_layers"][0]["thickness"]
        self.assertEqual(stored, {"value": 100.0, "unit": "nm"})

    def test_qw_and_nm_edits_recalculate_from_current_reference_and_dataset(self):
        self.install_film_fixtures()
        self.assertTrue(self.controller.addLayer("front"))
        layer_id = self.controller.front_layers.ids()[0]
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "material_id", "quick_fixture"))
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "optical_set_id", "n15"))

        self.assertTrue(self.controller.setLayerValue("front", layer_id, "thickness.qw", "1"))
        row = self.controller.front_layers.record_for_id(layer_id)
        self.assertEqual(self.controller.currentDesign()["front_layers"][0]["thickness"],
                         {"value": 1.0, "unit": "qw"})
        self.assertEqual(row["physicalNmText"], format(550.0 / 6.0, ".3f"))
        self.assertTrue(math.isclose(row["physicalNmValue"], 550.0 / 6.0, rel_tol=1e-11))

        self.assertTrue(self.controller.setLayerValue("front", layer_id, "thickness.nm", "110"))
        row = self.controller.front_layers.record_for_id(layer_id)
        self.assertEqual(self.controller.currentDesign()["front_layers"][0]["thickness"],
                         {"value": 110.0, "unit": "nm"})
        self.assertTrue(math.isclose(float(row["qwText"]), 1.2, rel_tol=1e-11))

        self.assertTrue(self.controller.setDesignValue("reference_wavelength_nm", 600))
        row = self.controller.front_layers.record_for_id(layer_id)
        self.assertTrue(math.isclose(float(row["qwText"]), 1.1, rel_tol=1e-11))
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "optical_set_id", "n20"))
        row = self.controller.front_layers.record_for_id(layer_id)
        self.assertTrue(math.isclose(float(row["qwText"]), 4.0 * 2.0 * 110.0 / 600.0,
                                     rel_tol=1e-11))

    def test_material_changes_keep_valid_datasets_and_replace_invalid_datasets(self):
        self.install_film_fixtures()

        def optical_set(identifier, index):
            return {
                "id": identifier, "name": identifier, "kind": "tabulated",
                "notes": "Synthetic unit-test data", "source": "Local unit-test fixture",
                "wavelength_unit": "nm", "wavelength_min": 400.0, "wavelength_max": 700.0,
                "wavelengths": [400.0, 550.0, 700.0], "n": [index, index, index], "k": None,
            }

        stable = default_material_profile("Quick stable fixture", id="quick_fixture_stable")
        stable.update(category="film", optical_constants=[
            optical_set("n15", 1.5), optical_set("n20", 2.0),
        ])
        only = default_material_profile("Quick only fixture", id="quick_fixture_only")
        only.update(category="film", optical_constants=[optical_set("only-set", 1.8)])
        fixture_directory = self.root / "data" / "materials"
        fixture_directory.mkdir(parents=True, exist_ok=True)
        for profile in (stable, only):
            (fixture_directory / (profile["id"] + ".json")).write_text(
                json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        self.controller.film_options = self.service.material_options(purpose="film")
        self.controller._refresh_picker_options()

        self.assertTrue(self.controller.addLayer("front"))
        layer_id = self.controller.front_layers.ids()[0]
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "material_id", "quick_fixture"))
        row = self.controller.currentDesign()["front_layers"][0]
        self.assertEqual(row["optical_set_id"], "n15")
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "optical_set_id", "n20"))
        self.assertTrue(self.controller.setLayerValue(
            "front", layer_id, "material_id", "quick_fixture_stable"
        ))
        self.assertEqual(self.controller.currentDesign()["front_layers"][0]["optical_set_id"], "n20")
        self.assertTrue(self.controller.setLayerValue(
            "front", layer_id, "material_id", "quick_fixture_only"
        ))
        self.assertEqual(self.controller.currentDesign()["front_layers"][0]["optical_set_id"], "only-set")
        self.assertNotIn(self.controller.front_layers.record_for_id(layer_id)["qwText"],
                         ("\u4e0d\u53ef\u7528", "n\u4e0d\u53ef\u7528"))

        self.assertTrue(self.controller.addSymbol())
        symbol_id = self.stable_id_for_symbol("A")
        self.assertTrue(self.controller.setLayerValue("symbol", symbol_id, "material_id", "quick_fixture"))
        symbol = self.controller.currentDesign()["expression"]["symbols"]["A"]
        self.assertEqual(symbol["optical_set_id"], "n15")
        self.assertNotIn(self.controller.expression_symbols.record_for_id(symbol_id)["qwText"],
                         ("\u4e0d\u53ef\u7528", "n\u4e0d\u53ef\u7528"))

    def test_medium_material_selection_defaults_dataset_and_preserves_loaded_legacy_constant(self):
        self.install_film_fixtures()
        original = self.controller.currentDesign()["incident_medium"]
        self.assertEqual(original, {
            "kind": "material", "material_id": "builtin_ideal_air",
            "optical_set_id": "ideal_air_n1_k0",
        })

        legacy = self.service.new_design()
        legacy["incident_medium"] = {"kind": "constant", "n": 1.37}
        path = self.root / "legacy-medium-before-selection.cfilm"
        self.write_design_fixture(path, legacy)
        self.controller.requestOpenFile(QUrl.fromLocalFile(str(path)))
        self.assertEqual(self.controller.currentDesign()["incident_medium"],
                         {"kind": "constant", "n": 1.37})

        self.assertTrue(self.controller.setMediumValue("incident_medium", "material_id", "quick_fixture"))
        selected = self.controller.currentDesign()["incident_medium"]
        self.assertEqual(selected, {
            "kind": "material", "material_id": "quick_fixture", "optical_set_id": "n15",
        })
        self.assertNotIn("n", selected)

    def test_medium_purpose_uses_roles_and_keeps_substrate_binding_path(self):
        materials = self.service.materials
        air = materials.get("builtin_ideal_air")
        self.assertIn("medium", air.get("roles", []))
        self.assertIn("builtin_ideal_air", {item["id"] for item in materials.options("medium")})
        # The pre-existing substrate predicate admits medium-role records too.
        self.assertIn("builtin_ideal_air", {item["id"] for item in materials.options("substrate")})

        materials.set_usage("builtin_ideal_air", "medium", False)
        self.assertNotIn("builtin_ideal_air", {item["id"] for item in materials.options("medium")})
        materials.set_usage("builtin_ideal_air", "medium", True)
        self.assertIn("builtin_ideal_air", {item["id"] for item in materials.options("medium")})

    def test_qtest_medium_picker_selects_existing_air_record_from_loaded_constant(self):
        air_record = self.service.materials.get("builtin_ideal_air")
        self.assertIn("medium", air_record.get("roles", []))
        design = self.service.new_design()
        design["incident_medium"] = {"kind": "constant", "n": 1.37}
        path = self.root / "medium-air-role.cfilm"
        self.write_design_fixture(path, design)
        self.controller.requestOpenFile(QUrl.fromLocalFile(str(path)))
        self.assertEqual(self.controller.currentDesign()["incident_medium"],
                         {"kind": "constant", "n": 1.37})

        engine, _component, root = self.create_quick_window()
        try:
            root.setWidth(1400)
            root.setHeight(900)
            QTest.qWait(40)
            tabs = root.findChild(QQuickItem, "filmDesignPageTabs")
            self.assertIsNotNone(tabs)
            tabs.setProperty("currentIndex", 2)
            QTest.qWait(30)

            combo = root.findChild(QQuickItem, "filmDesignMediumMaterial_incident_medium")
            self.assertIsNotNone(combo)
            self.assertTrue(combo.isVisible())
            air_index = next(
                index for index, item in enumerate(self.controller.mediumOptions)
                if item.get("id") == "builtin_ideal_air"
            )
            self.assertEqual(combo.property("currentIndex"), -1)

            def click_item(item):
                target = _visual_window_for(item)
                point = target.contentItem().mapFromItem(
                    item, QPointF(item.width() / 2, item.height() / 2)
                ).toPoint()
                self.assertGreaterEqual(point.x(), 0)
                self.assertLess(point.x(), target.width())
                self.assertGreaterEqual(point.y(), 0)
                self.assertLess(point.y(), target.height())
                QTest.mouseClick(target, Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier, point)
                QTest.qWait(35)

            def read_qml_property(scope, expression):
                item_context = QQmlEngine.contextForObject(scope) or engine.rootContext()
                query = QQmlExpression(item_context, scope, expression)
                value, undefined = query.evaluate()
                self.assertFalse(query.hasError(), query.error().toString())
                self.assertFalse(undefined, expression)
                return value

            click_item(combo)
            self.assertTrue(combo.property("popupVisible"))
            options_list = combo.property("optionsListView")
            self.assertIsNotNone(options_list)
            options_list.positionViewAtIndex(air_index, 1)
            QTest.qWait(35)
            air_option = read_qml_property(
                combo, f"optionsListView.itemAtIndex({air_index})"
            )
            self.assertIsNotNone(air_option)
            click_item(air_option)

            self.assertEqual(
                self.controller.currentDesign()["incident_medium"],
                {"kind": "material", "material_id": "builtin_ideal_air",
                 "optical_set_id": "ideal_air_n1_k0"},
            )
        finally:
            self.dispose_quick_window(engine, root)

    def test_legacy_constant_medium_survives_cfilm_open_and_save(self):
        design = self.service.new_design()
        design["incident_medium"] = {"kind": "constant", "n": 1.37}
        path = self.root / "legacy-medium.cfilm"
        self.service.save(path, design)
        self.controller.requestOpenFile(QUrl.fromLocalFile(str(path)))
        self.assertEqual(self.controller.currentDesign()["incident_medium"],
                         {"kind": "constant", "n": 1.37})
        self.assertTrue(self.controller.saveCurrent())
        stored = self.service.load(path)
        self.assertEqual(stored["incident_medium"], {"kind": "constant", "n": 1.37})

    def test_empty_material_dataset_list_stays_explicit_and_does_not_make_an_index(self):
        self.assertEqual(
            self.controller._preferred_optical_set_id({"id": "empty", "optical_sets": []}), ""
        )
        picker_models = self.controller._dataset_picker_models([
            {"id": "empty", "optical_sets": []},
        ])
        self.assertEqual([item["id"] for item in picker_models["empty"]], [""])

    def test_expression_symbol_addition_is_incremental_and_does_not_expand_expression(self):
        inserted = []
        resets = []
        self.controller.expression_symbols.rowsInserted.connect(lambda *_args: inserted.append(1))
        self.controller.expression_symbols.modelAboutToBeReset.connect(lambda: resets.append(1))
        self.controller.setExpressionText("G1 A")
        with patch.object(self.controller, "expandExpression") as expand:
            self.assertTrue(self.controller.addSymbol())
        self.assertEqual(len(inserted), 1)
        self.assertEqual(resets, [])
        expand.assert_not_called()
        self.assertEqual(self.controller.expressionText, "G1 A")

    def test_invalid_reference_index_rejects_thickness_edit_without_changing_source(self):
        self.install_film_fixtures()
        self.assertTrue(self.controller.addLayer("front"))
        layer_id = self.controller.front_layers.ids()[0]
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "material_id", "quick_fixture"))
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "optical_set_id", "n15"))
        self.assertTrue(self.controller.setDesignValue("reference_wavelength_nm", 900))
        self.assertFalse(self.controller.setLayerValue("front", layer_id, "thickness.nm", "125"))
        self.assertFalse(self.controller.setLayerValue("front", layer_id, "thickness.qw", "1.5"))
        self.assertEqual(self.controller.currentDesign()["front_layers"][0]["thickness"],
                         {"value": 100.0, "unit": "nm"})
        self.assertNotEqual(self.controller.front_layers.record_for_id(layer_id)["qwText"], "0")

    def test_cfilm_thickness_round_trip_keeps_schema_unit(self):
        legacy = self.service.new_design()
        legacy["front_layers"] = [{
            "id": "legacy-layer",
            "material_id": "old-film",
            "optical_set_id": "old-set",
            "thickness": {"value": 0.15, "unit": "um"},
            "note": "legacy record",
        }]
        path = self.root / "legacy.cfilm"
        self.service.save(path, legacy)

        self.controller.requestOpenFile(QUrl.fromLocalFile(str(path)))
        self.assertEqual(self.controller.currentDesign()["front_layers"][0]["thickness"],
                         {"value": 0.15, "unit": "um"})
        row = self.controller.front_layers.record_for_id("legacy-layer")
        self.assertEqual(row["physicalNmText"], "150.000")
        self.assertTrue(self.controller.saveCurrent())
        saved = self.service.load(path)
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["front_layers"][0]["thickness"],
                         {"value": 0.15, "unit": "um"})

    def test_existing_um_value_remains_compatible_and_symbol_case_is_normalized(self):
        self.assertTrue(self.controller.addLayer("front"))
        layer_id = self.controller.front_layers.ids()[0]
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "thickness.value", "0.15"))
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "thickness.unit", "um"))
        row = self.controller.front_layers.record_for_id(layer_id)
        self.assertEqual(row["physicalNmText"], "150.000")
        self.assertEqual(self.controller.currentDesign()["front_layers"][0]["thickness"]["unit"], "um")

        self.assertTrue(self.controller.addSymbol())
        symbol_id = self.stable_id_for_symbol("A")
        self.assertEqual(self.controller.expression_symbols.record_for_id(symbol_id)["symbol"], "A")
        self.assertTrue(self.controller.setLayerValue("symbol", symbol_id, "symbol", "b"))
        self.assertEqual(list(self.controller.currentDesign()["expression"]["symbols"]), ["H", "L", "B"])

    def test_active_editor_marks_document_dirty_before_commit(self):
        self.assertFalse(self.controller.dirty)
        self.controller.markEditingDirty()
        self.assertTrue(self.controller.dirty)

    def test_adding_a_layer_derives_only_the_new_row(self):
        for _ in range(12):
            self.assertTrue(self.controller.addLayer("front"))

        from coat_sim.core import film_design

        with patch.object(film_design, "physical_thickness_nm",
                          wraps=film_design.physical_thickness_nm) as convert:
            self.assertTrue(self.controller.addLayer("front"))

        self.assertEqual(convert.call_count, 1)

    def test_drag_reorder_keeps_model_and_document_arrays_aligned(self):
        for _ in range(4):
            self.assertTrue(self.controller.addLayer("front"))
        ids = self.controller.front_layers.ids()
        notifications = []
        unsubscribe = self.controller.session.subscribe(
            lambda change: notifications.append(change))
        base_revision = self.controller.session.snapshot().design_revision

        self.assertTrue(self.controller.moveLayerToIndex("front", ids[0], 4))
        expected = ids[1:] + ids[:1]
        self.assertEqual(self.controller.front_layers.ids(), expected)
        self.assertEqual([row["id"] for row in self.controller.currentDesign()["front_layers"]], expected)
        self.assertEqual(self.controller.session.snapshot().design_revision, base_revision + 1)
        self.assertEqual(len(notifications), 1)
        after_move = self.controller.session.snapshot()
        notifications.clear()
        self.assertFalse(self.controller.moveLayerToIndex("front", ids[0], 5))
        self.assertFalse(self.controller.moveLayerToIndex("front", ids[0], 1.5))
        self.assertFalse(self.controller.moveLayerToIndex("front", "missing", 0))
        self.assertEqual(self.controller.currentDesign()["front_layers"], after_move.design["front_layers"])
        self.assertEqual(self.controller.session.snapshot().design_revision, after_move.design_revision)
        self.assertEqual(notifications, [])
        unsubscribe()

    def test_multiline_expression_is_not_rewritten_during_editing(self):
        text = "g1 (a b)^2\nG2 0.5 A"
        self.assertTrue(self.controller.setExpressionText(text))
        self.assertEqual(self.controller.expressionText, text)
        self.assertFalse(self.controller.currentDesign()["expression"]["in_sync"])

    def test_expression_normalizes_ascii_case_after_focus_loss(self):
        text = "g1 (a b)^2\nG2 0.5 A"
        self.controller.setExpressionText(text)
        self.controller.normalizeExpressionText()
        self.assertEqual(self.controller.expressionText, "G1 (A B)^2\nG2 0.5 A")

    def test_expression_template_round_trip_uses_service_store(self):
        self.assertTrue(self.controller.addSymbol())
        symbol_id = self.stable_id_for_symbol("A")
        self.assertTrue(self.controller.setLayerValue("symbol", symbol_id, "material_id", "film-case-sensitive-ID"))
        self.assertTrue(self.controller.setLayerValue("symbol", symbol_id, "optical_set_id", "optical-set-01"))
        self.controller.setExpressionText("g1 a\nG2 (0.5 a)^2")
        self.assertTrue(self.controller.saveExpressionTemplate("Quick template", "check"))
        saved = self.service.list_expression_templates()
        self.assertEqual(saved[0]["name"], "Quick template")
        loaded = self.service.load_expression_template(saved[0]["template_id"])
        self.assertEqual(loaded["expression"]["text"], "G1 A\nG2 (0.5 A)^2")
        self.assertTrue(self.controller.applyExpressionTemplate(saved[0]["template_id"]))
        self.assertEqual(self.controller.expressionText, "G1 A\nG2 (0.5 A)^2")

    def test_qml_design_file_dialog_opens_cfilm_or_tfd_but_saves_cfilm_only(self):
        qml = Path(__file__).parents[1] / "coat_sim/ui/qml/FilmDesignQuick.qml"
        source = qml.read_text(encoding="utf-8")
        self.assertEqual(source.count("*.cfilm"), 3)
        self.assertEqual(source.count("*.tfd"), 2)
        self.assertIn('defaultSuffix: "cfilm"', source)
        self.assertNotIn("*.json", source)

    def test_cfilm_and_sqlite_paths_use_the_existing_service_contract(self):
        path = self.root / "designs" / "quick.cfilm"
        self.assertTrue(self.controller.saveFilePath(str(path), False))
        self.assertEqual(path.suffix, ".cfilm")
        self.assertEqual(self.service.load(path), self.controller.currentDesign())
        self.assertTrue(self.controller.saveToLibrary())
        self.assertEqual(len(self.service.list_saved_designs()), 1)

    def test_film_design_qml_component_loads_with_shared_theme_contract(self):
        engine = QQmlApplicationEngine()
        ThemeService.instance().set_theme("light")
        ThemeService.instance().attach_engine(engine, "Theme")
        controller = FilmDesignQuickController(self.root, service=self.service)
        component = QQmlComponent(
            engine,
            QUrl.fromLocalFile(str(Path(__file__).parents[1] / "coat_sim/ui/qml/FilmDesignQuick.qml")),
        )
        root = component.createWithInitialProperties({"controller": controller})
        if root is None:
            errors = "\n".join(error.toString() for error in component.errors())
            self.fail(f"FilmDesignQuick.qml failed to load: {errors}")
        try:
            self.assertIsNotNone(root)
            self.assertTrue(root.property("controller"))
            screen_width = root.property("screenWidthForLayout")
            self.assertGreater(screen_width, 0)
            self.assertLessEqual(root.property("width"), screen_width)
            self.assertLessEqual(root.property("minimumWidth"), screen_width)
            self.assertEqual(root.property("title"), "膜系设计")
            controller.markEditingDirty()
            QTest.qWait(10)
            self.assertEqual(root.property("title"), "膜系设计 *")
        finally:
            self.dispose_quick_window(engine, root)

    def test_material_picker_models_are_lightweight_and_choose_a_default_dataset(self):
        repository_root = Path(__file__).parents[1]
        controller = FilmDesignQuickController(repository_root)
        self.assertEqual(self.wait_for_initialization(controller), "ready")
        material = next(item for item in controller.film_options if item.get("optical_sets"))
        optical_set = material["optical_sets"][0]
        picker_material = next(item for item in controller.filmOptions
                               if item.get("id") == material["id"])
        picker_sets = controller.opticalSetsForFilm(material["id"])
        self.assertNotIn("optical_sets", picker_material)
        self.assertNotIn("wavelengths", picker_sets[1])
        self.assertNotIn("n", picker_sets[1])
        self.assertNotIn("k", picker_sets[1])
        self.assertNotIn("source", picker_sets[1])
        self.assertEqual(picker_sets[1]["id"], optical_set["id"])
        self.assertIs(picker_sets, controller.opticalSetsForFilm(material["id"]))

        self.assertTrue(controller.addLayer("front"))
        stable_id = controller.front_layers.ids()[0]
        self.assertTrue(controller.setLayerValue("front", stable_id, "material_id", material["id"]))
        row = controller.currentDesign()["front_layers"][0]
        self.assertEqual(row["optical_set_id"], optical_set["id"])
        self.assertNotIn(controller.front_layers.record_for_id(stable_id)["qwText"],
                         ("\u4e0d\u53ef\u7528", "n\u4e0d\u53ef\u7528"))
        controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _APP.processEvents()

    def test_real_zns_material_popup_selects_a_default_dataset_and_keeps_qw_valid(self):
        controller = self.controller
        material = next(item for item in controller.film_options
                        if item.get("name") == "ZnS" and item.get("optical_sets"))
        default_dataset_id = controller._preferred_optical_set_id(material)
        self.assertTrue(controller.addLayer("front"))
        stable_id = controller.front_layers.ids()[0]
        engine, _component, root = self.create_quick_window(controller)
        try:
            content = root.contentItem()

            def find_visual_item(parent, object_name):
                if parent.objectName() == object_name:
                    return parent
                for child in parent.childItems():
                    if child.objectName() == object_name:
                        return child
                    found = find_visual_item(child, object_name)
                    if found is not None:
                        return found
                return None

            def click_visual_item(item):
                target_window = _visual_window_for(item)
                target_content = target_window.contentItem()
                point = target_content.mapFromItem(
                    item, QPointF(item.width() / 2, item.height() / 2)
                ).toPoint()
                QTest.mouseClick(target_window, Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier, point)
                QTest.qWait(40)

            def read_qml_property(scope, expression):
                item_context = QQmlEngine.contextForObject(scope) or engine.rootContext()
                query = QQmlExpression(item_context, scope, expression)
                value, is_undefined = query.evaluate()
                self.assertFalse(query.hasError(), query.error().toString())
                self.assertFalse(is_undefined, expression)
                return value

            front_view = root.findChild(QQuickItem, "filmDesignFrontView")
            self.assertIsNotNone(front_view)
            material_combo = find_visual_item(
                content, "filmLayerMaterial_" + stable_id)
            self.assertIsNotNone(material_combo)
            click_visual_item(material_combo)
            self.assertTrue(material_combo.property("popupVisible"))
            material_index = next(
                index for index, option in enumerate(controller.filmOptions)
                if option.get("id") == material["id"]
            )
            material_options_list = material_combo.property("optionsListView")
            self.assertIsNotNone(material_options_list)
            material_options_list.positionViewAtIndex(material_index, 1)
            QTest.qWait(40)
            material_option = read_qml_property(
                material_combo, f"optionsListView.itemAtIndex({material_index})"
            )
            self.assertIsNotNone(material_option)
            self.assertEqual(material_option.property("text"), material["name"])
            click_visual_item(material_option)

            design_row = controller.currentDesign()["front_layers"][0]
            self.assertEqual(design_row["material_id"], material["id"])
            self.assertEqual(design_row["optical_set_id"], default_dataset_id)
            dataset_combo = find_visual_item(front_view, "filmLayerOpticalSet_" + stable_id)
            self.assertIsNotNone(dataset_combo)
            self.assertEqual(dataset_combo.property("count"), len(material["optical_sets"]) + 1)
            self.assertEqual(dataset_combo.property("currentValue"), default_dataset_id)
            self.assertNotIn(controller.front_layers.record_for_id(stable_id)["qwText"],
                             ("\u4e0d\u53ef\u7528", "n\u4e0d\u53ef\u7528"))

            chosen_dataset = next(
                (item for item in material["optical_sets"] if item["id"] != default_dataset_id),
                next(item for item in material["optical_sets"] if item["id"] == default_dataset_id),
            )
            click_visual_item(dataset_combo)
            self.assertTrue(read_qml_property(dataset_combo, "popup.visible"))
            dataset_content = read_qml_property(dataset_combo, "popup.contentItem")
            dataset_index = next(index for index, item in enumerate(material["optical_sets"])
                                 if item["id"] == chosen_dataset["id"]) + 1
            dataset_content.positionViewAtIndex(dataset_index, 1)
            QTest.qWait(40)
            dataset_option = read_qml_property(
                dataset_combo, f"popup.contentItem.itemAtIndex({dataset_index})"
            )
            self.assertIsNotNone(dataset_option)
            self.assertEqual(dataset_option.property("text"), chosen_dataset["name"])
            click_visual_item(dataset_option)
            self.assertEqual(controller.currentDesign()["front_layers"][0]["optical_set_id"],
                             chosen_dataset["id"])
            self.assertEqual(dataset_combo.property("currentValue"), chosen_dataset["id"])
            self.assertNotIn(controller.front_layers.record_for_id(stable_id)["qwText"],
                             ("\u4e0d\u53ef\u7528", "n\u4e0d\u53ef\u7528"))
        finally:
            self.dispose_quick_window(engine, root)

    def test_material_and_dataset_popups_show_hover_and_keyboard_highlights(self):
        self.install_film_fixtures()
        self.assertTrue(self.controller.addLayer("front"))
        stable_id = self.controller.front_layers.ids()[0]
        engine, _component, root = self.create_quick_window()
        try:
            front_view = root.findChild(QQuickItem, "filmDesignFrontView")

            def find_visual_item(parent, object_name):
                for child in parent.childItems():
                    if child.objectName() == object_name:
                        return child
                    found = find_visual_item(child, object_name)
                    if found is not None:
                        return found
                return None

            material_combo = find_visual_item(front_view, "filmLayerMaterial_" + stable_id)

            def read_qml_property(scope, expression):
                item_context = QQmlEngine.contextForObject(scope) or engine.rootContext()
                query = QQmlExpression(item_context, scope, expression)
                value, undefined = query.evaluate()
                self.assertFalse(query.hasError(), query.error().toString())
                self.assertFalse(undefined, expression)
                return value

            def click_visual_item(item):
                target = _visual_window_for(item)
                point = target.contentItem().mapFromItem(
                    item, QPointF(item.width() / 2, item.height() / 2)
                ).toPoint()
                QTest.mouseClick(target, Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier, point)
                QTest.qWait(20)

            def move_to_visual_item(item):
                target = _visual_window_for(item)
                point = target.contentItem().mapFromItem(
                    item, QPointF(item.width() / 2, item.height() / 2)
                ).toPoint()
                QTest.mouseMove(target, point)
                QTest.qWait(20)

            material_options = self.controller.filmOptions
            fixture_index = next(i for i, item in enumerate(material_options)
                                 if item.get("id") == "quick_fixture")
            click_visual_item(material_combo)
            self.assertTrue(material_combo.property("popupVisible"))
            material_options_list = material_combo.property("optionsListView")
            self.assertIsNotNone(material_options_list)
            material_options_list.positionViewAtIndex(fixture_index, 1)
            QTest.qWait(40)
            material_popup_item = read_qml_property(
                material_combo, f"optionsListView.itemAtIndex({fixture_index})"
            )
            self.assertIsNotNone(material_popup_item)
            move_to_visual_item(material_popup_item)
            self.assertTrue(material_popup_item.property("hovered"))

            QTest.keyClick(root, Qt.Key.Key_Escape)
            QTest.qWait(20)
            click_visual_item(material_combo)
            before = int(material_combo.property("highlightedIndex"))
            QTest.keyClick(root, Qt.Key.Key_Down)
            QTest.qWait(20)
            highlighted_index = int(material_combo.property("highlightedIndex"))
            self.assertNotEqual(highlighted_index, before)
            keyboard_item = read_qml_property(
                material_combo, f"optionsListView.itemAtIndex({highlighted_index})"
            )
            self.assertTrue(keyboard_item.property("highlighted"))
            material_options_list.positionViewAtIndex(fixture_index, 1)
            QTest.qWait(30)
            QTest.keyClick(root, Qt.Key.Key_Escape)
            QTest.qWait(20)

            click_visual_item(material_combo)
            material_options_list.positionViewAtIndex(fixture_index, 1)
            QTest.qWait(30)
            material_popup_item = read_qml_property(
                material_combo, f"optionsListView.itemAtIndex({fixture_index})"
            )
            click_visual_item(material_popup_item)
            dataset_combo = find_visual_item(front_view, "filmLayerOpticalSet_" + stable_id)
            self.assertEqual(dataset_combo.property("currentValue"), "n15")
            click_visual_item(dataset_combo)
            self.assertTrue(read_qml_property(dataset_combo, "popup.visible"))
            dataset_popup_list = read_qml_property(dataset_combo, "popup.contentItem")
            dataset_popup_list.positionViewAtIndex(1, 1)
            QTest.qWait(30)
            dataset_item = read_qml_property(
                dataset_combo, "popup.contentItem.itemAtIndex(1)")
            self.assertIsNotNone(dataset_item)
            move_to_visual_item(dataset_item)
            self.assertTrue(dataset_item.property("hovered"))
            tooltip = read_qml_property(dataset_item, "ToolTip.text")
            self.assertIn("Synthetic unit-test data", tooltip)
            self.assertIn("400", tooltip)
            self.assertNotIn("Local unit-test fixture", tooltip)

            QTest.keyClick(root, Qt.Key.Key_Escape)
            QTest.qWait(20)
            click_visual_item(dataset_combo)
            dataset_popup_list = read_qml_property(dataset_combo, "popup.contentItem")
            before = int(dataset_combo.property("highlightedIndex"))
            QTest.keyClick(root, Qt.Key.Key_Down)
            QTest.qWait(20)
            highlighted_index = int(dataset_combo.property("highlightedIndex"))
            self.assertNotEqual(highlighted_index, before)
            dataset_popup_list.positionViewAtIndex(highlighted_index, 1)
            QTest.qWait(30)
            keyboard_item = read_qml_property(
                dataset_combo, f"popup.contentItem.itemAtIndex({highlighted_index})"
            )
            self.assertTrue(keyboard_item.property("highlighted"))
        finally:
            self.dispose_quick_window(engine, root)

    def _assert_layer_insert_click_at_row_count(self, target_count):
        def find_visual_item(parent, object_name):
            for child in parent.childItems():
                if child.objectName() == object_name:
                    return child
                found = find_visual_item(child, object_name)
                if found is not None:
                    return found
            return None

        while self.controller.front_layers.rowCount() < target_count:
            self.assertTrue(self.controller.addLayer("front"))
        engine, _component, root = self.create_quick_window()
        timer = None
        try:
            # Keep this scenario's QQuickWindow alive for five repeated clicks.
            # Each size has its own test case and scene.
            self.assertTrue(root.isVisible())
            model = self.controller.front_layers
            inserted = []
            inserted_at = []
            resets = []
            model.rowsInserted.connect(
                lambda *_args: (inserted.append(1), inserted_at.append(perf_counter()))
            )
            model.modelAboutToBeReset.connect(lambda: resets.append(1))
            heartbeat = [0]
            heartbeat_last = [None]
            heartbeat_gaps_ms = []

            def on_heartbeat():
                now = perf_counter()
                if heartbeat_last[0] is not None:
                    heartbeat_gaps_ms.append((now - heartbeat_last[0]) * 1000)
                heartbeat_last[0] = now
                heartbeat[0] += 1

            timer = QTimer(_APP)
            timer.setInterval(1)
            timer.timeout.connect(on_heartbeat)

            front_view = find_visual_item(root.contentItem(), "filmDesignFrontView")
            self.assertIsNotNone(front_view)
            first_id = model.ids()[0]
            insert_button = find_visual_item(
                front_view, "filmDesignInsertAfter_" + first_id
            )
            self.assertIsNotNone(insert_button)
            point = root.contentItem().mapFromItem(
                insert_button,
                QPointF(insert_button.width() / 2, insert_button.height() / 2),
            ).toPoint()
            timer.start()
            QTest.qWait(5)
            click_ms = []
            rows_inserted_ms = []
            next_loop_ms = []
            for _ in range(5):
                click_started = perf_counter()
                QTest.mouseClick(root, Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier, point)
                click_returned = perf_counter()
                click_ms.append((click_returned - click_started) * 1000)
                self.assertEqual(len(inserted), len(click_ms))
                rows_inserted_ms.append((inserted_at[-1] - click_started) * 1000)

                response = []
                QTimer.singleShot(0, lambda: response.append(perf_counter()))
                response_deadline = perf_counter() + 0.25
                while not response and perf_counter() < response_deadline:
                    _APP.processEvents()
                self.assertTrue(response, "queued next-event-loop callback did not run")
                next_loop_ms.append((response[0] - click_returned) * 1000)
                QTest.qWait(3)

            timer.stop()
            self.assertGreater(heartbeat[0], 0)
            self.assertEqual(model.rowCount(), target_count + 5)
            self.assertEqual(len(inserted), 5)
            self.assertEqual(resets, [])
            print(
                f"film-ui layer insert: baseline_rows={target_count}, repeats=5, "
                f"mouseClick_ms={[round(value, 2) for value in click_ms]}, "
                f"rowsInserted_ms={[round(value, 2) for value in rows_inserted_ms]}, "
                f"nextLoopAfterClick_ms={[round(value, 2) for value in next_loop_ms]}, "
                f"heartbeat_count={heartbeat[0]}, "
                f"heartbeat_max_gap_ms={max(heartbeat_gaps_ms, default=0.0):.2f}"
            )
        finally:
            if timer is not None:
                timer.stop()
            self.dispose_quick_window(engine, root)

    def test_qtest_layer_insert_click_at_100_rows(self):
        self._assert_layer_insert_click_at_row_count(100)

    def test_qtest_layer_insert_click_at_500_rows(self):
        self._assert_layer_insert_click_at_row_count(500)

    def test_qtest_add_symbol_five_incremental_clicks(self):
        engine, _component, root = self.create_quick_window()
        timer = None
        try:
            root.setWidth(1400)
            root.setHeight(900)
            QTest.qWait(40)
            button = root.findChild(QQuickItem, "filmDesignAddSymbol")
            self.assertIsNotNone(button)
            self.assertTrue(button.isEnabled())
            model = self.controller.expression_symbols
            initial_row_count = model.rowCount()
            inserted = []
            inserted_at = []
            resets = []
            model.rowsInserted.connect(
                lambda *_args: (inserted.append(1), inserted_at.append(perf_counter()))
            )
            model.modelAboutToBeReset.connect(lambda: resets.append(1))

            heartbeat = [0]
            heartbeat_last = [None]
            heartbeat_gaps_ms = []

            def on_heartbeat():
                now = perf_counter()
                if heartbeat_last[0] is not None:
                    heartbeat_gaps_ms.append((now - heartbeat_last[0]) * 1000)
                heartbeat_last[0] = now
                heartbeat[0] += 1

            timer = QTimer(_APP)
            timer.setInterval(1)
            timer.timeout.connect(on_heartbeat)
            timer.start()
            QTest.qWait(5)
            point = root.contentItem().mapFromItem(
                button, QPointF(button.width() / 2, button.height() / 2)
            ).toPoint()
            self.assertGreaterEqual(point.x(), 0)
            self.assertLess(point.x(), root.width())

            click_ms = []
            rows_inserted_ms = []
            next_loop_ms = []
            for _ in range(5):
                click_started = perf_counter()
                QTest.mouseClick(root, Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier, point)
                click_returned = perf_counter()
                click_ms.append((click_returned - click_started) * 1000)
                self.assertEqual(len(inserted), len(click_ms))
                rows_inserted_ms.append((inserted_at[-1] - click_started) * 1000)

                response = []
                QTimer.singleShot(0, lambda: response.append(perf_counter()))
                response_deadline = perf_counter() + 0.25
                while not response and perf_counter() < response_deadline:
                    _APP.processEvents()
                self.assertTrue(response, "queued next-event-loop callback did not run")
                next_loop_ms.append((response[0] - click_returned) * 1000)
                QTest.qWait(3)

            timer.stop()
            self.assertEqual(model.rowCount(), initial_row_count + 5)
            self.assertEqual(len(inserted), 5)
            self.assertEqual(resets, [])
            self.assertGreater(heartbeat[0], 0)
            print(
                f"film-ui Add Symbol: repeats=5, "
                f"mouseClick_ms={[round(value, 2) for value in click_ms]}, "
                f"rowsInserted_ms={[round(value, 2) for value in rows_inserted_ms]}, "
                f"nextLoopAfterClick_ms={[round(value, 2) for value in next_loop_ms]}, "
                f"heartbeat_count={heartbeat[0]}, "
                f"heartbeat_max_gap_ms={max(heartbeat_gaps_ms, default=0.0):.2f}"
            )
        finally:
            if timer is not None:
                timer.stop()
            self.dispose_quick_window(engine, root)

    def test_add_symbol_button_routes_to_incremental_controller_slot(self):
        qml = Path(__file__).parents[1] / "coat_sim/ui/qml/FilmDesignExpressionPanel.qml"
        source = qml.read_text(encoding="utf-8")
        self.assertIn('objectName: "filmDesignAddSymbol"', source)
        self.assertIn("onClicked: if (panel.commitDraft(true)) panel.controller.addSymbol()", source)
        self.assertIn("if (expressionArea.inputMethodComposing) return false", source)
        controller_source = Path(__file__).parents[1] / "coat_sim/ui/film_quick_controller.py"
        controller = controller_source.read_text(encoding="utf-8")
        self.assertIn("@Slot(result=bool)\n    def addSymbol(self)", controller)
        self.assertIn("self.expression_symbols.append_record(dict(symbols[symbol], symbol=symbol), stable_id)", controller)
        self.assertIn("beginInsertRows", controller)
        self.assertIn("endInsertRows", controller)
        self.assertIn("model: panel.controller.expressionSymbols", source)

    def test_qtest_add_symbol_button_inserts_one_incremental_model_row(self):
        engine, _component, root = self.create_quick_window()
        try:
            root.setWidth(1400)
            root.setHeight(900)
            QTest.qWait(40)
            design = root.findChild(QQuickItem, "filmDesignDesignColumn")
            sidebar = root.findChild(QQuickItem, "filmDesignExpressionSidebar")
            button = root.findChild(QQuickItem, "filmDesignAddSymbol")
            self.assertTrue(design)
            self.assertTrue(sidebar)
            self.assertIsNotNone(button)
            design_x = design.mapToItem(root.contentItem(), QPointF(0, 0)).x()
            sidebar_pos = sidebar.mapToItem(root.contentItem(), QPointF(0, 0))
            self.assertGreater(sidebar_pos.x(), design_x)
            self.assertLessEqual(sidebar_pos.x() + sidebar.width(), root.width() + 1)
            self.assertTrue(button.isEnabled())
            self.assertTrue(button.isVisible())
            self.assertGreater(
                button.width(), 0,
                f"Add Symbol button width is {button.width()}, height is {button.height()}",
            )
            self.assertGreater(button.height(), 0)

            model = self.controller.expression_symbols
            initial_row_count = model.rowCount()
            inserted = []
            resets = []
            model.rowsInserted.connect(lambda *_args: inserted.append(1))
            model.modelAboutToBeReset.connect(lambda: resets.append(1))
            point = root.contentItem().mapFromItem(
                button,
                QPointF(button.width() / 2, button.height() / 2),
            ).toPoint()
            self.assertGreaterEqual(point.x(), 0)
            self.assertLess(
                point.x(), root.width(),
                f"Add Symbol center {point.x()},{point.y()} lies outside {root.width()}x{root.height()}",
            )
            self.assertGreaterEqual(point.y(), 0)
            self.assertLess(point.y(), root.height())
            QTest.mouseClick(
                root,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                point,
            )

            # Assert the complete mouse → QML handler → controller slot →
            # incremental model insertion path.
            self.assertEqual(model.rowCount(), initial_row_count + 1)
            self.assertEqual(inserted, [1])
            self.assertEqual(resets, [])
        finally:
            self.dispose_quick_window(engine, root)

    def test_expression_sidebar_stays_right_at_common_widths_and_collapses_to_handle(self):
        engine, _component, root = self.create_quick_window()
        try:
            def wait_for_layout(predicate):
                for _ in range(100):
                    QCoreApplication.processEvents()
                    if predicate():
                        return True
                    QTest.qWait(10)
                QCoreApplication.processEvents()
                return predicate()

            design = root.findChild(QQuickItem, "filmDesignDesignColumn")
            sidebar = root.findChild(QQuickItem, "filmDesignExpressionSidebar")
            toggle = root.findChild(QQuickItem, "filmDesignSidebarToggle")
            expression_scroll = root.findChild(QQuickItem, "filmDesignExpressionScroll")
            self.assertTrue(all((design, sidebar, toggle, expression_scroll)))

            for width, expanded in ((1100, True), (1400, True), (1000, False)):
                root.setWidth(width)
                QTest.qWait(50)
                # The flexible spacer and collapse toggle occupy distinct rows.
                # Sharing row 3 made Qt reject the toggle and recursively relayout.
                spacer = root.findChild(QQuickItem, "filmDesignSidebarSpacer")
                self.assertIsNotNone(spacer)
                layout_rows = []
                for item in (spacer, toggle):
                    query = QQmlExpression(QQmlEngine.contextForObject(item), item, "Layout.row")
                    value, undefined = query.evaluate()
                    self.assertFalse(query.hasError(), query.error().toString())
                    self.assertFalse(undefined)
                    layout_rows.append(value)
                self.assertNotEqual(*layout_rows)
                design_pos = design.mapToItem(root.contentItem(), QPointF(0, 0))
                sidebar_pos = sidebar.mapToItem(root.contentItem(), QPointF(0, 0))
                self.assertGreaterEqual(sidebar_pos.x(), design_pos.x() + design.width() - 2)
                self.assertLessEqual(sidebar_pos.x() + sidebar.width(), root.width() + 1)
                self.assertLessEqual(abs(sidebar_pos.y() - design_pos.y()), 2)
                self.assertEqual(expression_scroll.isVisible(), expanded)
                if expanded:
                    self.assertTrue(wait_for_layout(lambda: sidebar.width() > 300))
                    self.assertGreater(
                        sidebar.width(), 300,
                        f"window={root.width()}, grid={sidebar.parentItem().width()}, "
                        f"design={design.width()}, sidebar={sidebar.width()}, "
                        f"toggle={toggle.width()}, scroll={expression_scroll.width()}",
                    )
                else:
                    self.assertTrue(wait_for_layout(lambda: (sidebar.height() if root.property("sidebarUsesBottomLayout") else sidebar.width()) <= 65))
                    self.assertLessEqual(
                sidebar.height() if root.property("sidebarUsesBottomLayout") else sidebar.width(), 65)
                toggle_pos = root.contentItem().mapFromItem(
                    toggle, QPointF(toggle.width() / 2, toggle.height() / 2)
                )
                self.assertGreaterEqual(toggle_pos.x(), 0)
                self.assertLess(toggle_pos.x(), root.width())
                self.assertGreaterEqual(toggle_pos.y(), 0)
                self.assertLess(toggle_pos.y(), root.height())

            root.setWidth(1100)
            root.show()
            QTest.qWait(50)
            expression_area = root.findChild(QQuickItem, "filmDesignExpressionArea")
            self.assertIsNotNone(expression_area)
            expression_area.forceActiveFocus()
            QCoreApplication.processEvents()
            self.assertEqual(root.activeFocusItem().objectName(),
                             "filmDesignExpressionArea")

            QTest.keyClick(root, Qt.Key_F4)
            QCoreApplication.processEvents()
            self.assertFalse(expression_scroll.isVisible())
            self.assertTrue(wait_for_layout(lambda: (sidebar.height() if root.property("sidebarUsesBottomLayout") else sidebar.width()) <= 65))
            self.assertLessEqual(
                sidebar.height() if root.property("sidebarUsesBottomLayout") else sidebar.width(), 65)
            focused = root.activeFocusItem()
            while focused is not None:
                self.assertIsNot(focused, sidebar)
                focused = focused.parentItem()

            QTest.keyClick(root, Qt.Key_F4)
            QCoreApplication.processEvents()
            self.assertTrue(expression_scroll.isVisible())
            self.assertTrue(wait_for_layout(lambda: sidebar.width() > 300))
            expanded_width = sidebar.width()
            point = toggle.mapToScene(QPointF(toggle.width() / 2, toggle.height() / 2))
            QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier,
                             QPoint(round(point.x()), round(point.y())))
            QCoreApplication.processEvents()
            self.assertFalse(expression_scroll.isVisible())
            self.assertTrue(wait_for_layout(lambda: (sidebar.height() if root.property("sidebarUsesBottomLayout") else sidebar.width()) <= 65))
            QTest.keyClick(root, Qt.Key_F4)
            QCoreApplication.processEvents()
            self.assertTrue(expression_scroll.isVisible())
            self.assertTrue(wait_for_layout(lambda: sidebar.width() > expanded_width / 2))
            self.assertGreater(sidebar.width(), expanded_width / 2)
        finally:
            self.dispose_quick_window(engine, root)

    def test_qtest_plus_inserts_after_stable_id(self):
        for _ in range(3):
            self.assertTrue(self.controller.addLayer("front"))
        original_ids = self.controller.front_layers.ids()
        engine, _component, root = self.create_quick_window()
        try:
            front_view = root.findChild(QQuickItem, "filmDesignFrontView")
            self.assertIsNotNone(front_view)

            def find_visual_item(parent, object_name):
                for child in parent.childItems():
                    if child.objectName() == object_name:
                        return child
                    found = find_visual_item(child, object_name)
                    if found is not None:
                        return found
                return None

            insert_button = find_visual_item(
                front_view, "filmDesignInsertAfter_" + original_ids[0]
            )
            self.assertIsNotNone(insert_button)
            content = root.contentItem()
            point = content.mapFromItem(
                insert_button, QPointF(insert_button.width() / 2, insert_button.height() / 2)
            ).toPoint()
            QTest.mouseClick(root, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
            QTest.qWait(50)

            actual_ids = self.controller.front_layers.ids()
            self.assertEqual(actual_ids[0], original_ids[0])
            self.assertEqual(actual_ids[2:], original_ids[1:])
            self.assertNotIn(actual_ids[1], original_ids)
            self.assertEqual(
                [row["id"] for row in self.controller.currentDesign()["front_layers"]], actual_ids
            )
        finally:
            self.dispose_quick_window(engine, root)
    def test_layer_header_and_row_columns_align_at_narrow_and_wide_widths(self):
        self.assertTrue(self.controller.addLayer("front"))
        stable_id = self.controller.front_layers.ids()[0]
        engine, _component, root = self.create_quick_window()
        try:
            content = root.contentItem()
            front_view = root.findChild(QQuickItem, "filmDesignFrontView")
            self.assertIsNotNone(front_view)

            def find_visual_item(parent, object_name):
                for child in parent.childItems():
                    if child.objectName() == object_name:
                        return child
                    found = find_visual_item(child, object_name)
                    if found is not None:
                        return found
                return None

            pairs = (
                ("filmDesignHeaderLayerNumber", "filmLayerNumber_" + stable_id),
                ("filmDesignHeaderMaterial", "filmLayerMaterial_" + stable_id),
                ("filmDesignHeaderDataset", "filmLayerOpticalSet_" + stable_id),
                ("filmDesignHeaderQw", "filmLayerQw_" + stable_id),
                ("filmDesignHeaderNm", "filmLayerNm_" + stable_id),
                ("filmDesignHeaderInsert", "filmDesignInsertAfter_" + stable_id),
                ("filmDesignHeaderDelete", "filmDesignDelete_" + stable_id),
            )
            for window_width in (1100, 1400):
                root.setWidth(window_width)
                QTest.qWait(40)
                for header_name, row_name in pairs:
                    header = find_visual_item(content, header_name)
                    row_item = find_visual_item(front_view, row_name)
                    self.assertIsNotNone(header, header_name)
                    self.assertIsNotNone(row_item, row_name)
                    header_x = header.mapToItem(content, QPointF(0, 0)).x()
                    row_x = row_item.mapToItem(content, QPointF(0, 0)).x()
                    self.assertAlmostEqual(header_x, row_x, delta=1.0,
                                           msg=f"{header_name} x misaligned at {window_width}px")
                    self.assertAlmostEqual(header.width(), row_item.width(), delta=1.0,
                                           msg=f"{header_name} width differs at {window_width}px")
                    viewport_right = front_view.mapToItem(
                        content, QPointF(front_view.width(), 0)
                    ).x()
                    header_right = header.mapToItem(
                        content, QPointF(header.width(), 0)
                    ).x()
                    row_right = row_item.mapToItem(
                        content, QPointF(row_item.width(), 0)
                    ).x()
                    self.assertLessEqual(header_right, viewport_right + 1.0)
                    self.assertLessEqual(row_right, viewport_right + 1.0)
        finally:
            self.dispose_quick_window(engine, root)

    def test_layout_metrics_cover_symbol_rows_face_switch_and_responsive_panel(self):
        self.assertTrue(self.controller.addLayer("front"))
        self.assertTrue(self.controller.addLayer("back"))
        self.assertTrue(self.controller.setSubstrateValue("sides", "double"))
        self.assertTrue(self.controller.addSymbol())
        front_id = self.controller.front_layers.ids()[0]
        back_id = self.controller.back_layers.ids()[0]
        symbol_id = self.stable_id_for_symbol("A")
        symbol_ids = self.controller.expression_symbols.ids()
        engine, _component, root = self.create_quick_window()
        try:
            content = root.contentItem()
            front_view = root.findChild(QQuickItem, "filmDesignFrontView")
            back_view = root.findChild(QQuickItem, "filmDesignBackView")
            page_tabs = root.findChild(QQuickItem, "filmDesignPageTabs")
            design_column = root.findChild(QQuickItem, "filmDesignDesignColumn")
            expression_sidebar = root.findChild(QQuickItem, "filmDesignExpressionSidebar")
            expression_scroll = root.findChild(QQuickItem, "filmDesignExpressionScroll")
            symbol_list = root.findChild(QQuickItem, "filmDesignSymbolList")
            self.assertTrue(all((front_view, back_view, page_tabs, design_column, expression_sidebar,
                                 expression_scroll, symbol_list)))

            def find_visual_item(parent, object_name):
                for child in parent.childItems():
                    if child.objectName() == object_name:
                        return child
                    found = find_visual_item(child, object_name)
                    if found is not None:
                        return found
                return None

            def click_item(item):
                point = content.mapFromItem(
                    item, QPointF(item.width() / 2, item.height() / 2)
                ).toPoint()
                QTest.mouseClick(root, Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier, point)
                QTest.qWait(40)

            layer_header_row = (
                ("filmDesignHeaderLayerNumber", "filmLayerNumber_"),
                ("filmDesignHeaderMaterial", "filmLayerMaterial_"),
                ("filmDesignHeaderDataset", "filmLayerOpticalSet_"),
                ("filmDesignHeaderQw", "filmLayerQw_"),
                ("filmDesignHeaderNm", "filmLayerNm_"),
                ("filmDesignHeaderInsert", "filmDesignInsertAfter_"),
                ("filmDesignHeaderDelete", "filmDesignDelete_"),
            )
            symbol_header_row = (
                ("filmDesignSymbolHeaderIndex", "filmLayerSymbol_"),
                ("filmDesignSymbolHeaderMaterial", "filmLayerMaterial_"),
                ("filmDesignSymbolHeaderDataset", "filmLayerOpticalSet_"),
                ("filmDesignSymbolHeaderQw", "filmLayerQw_"),
                ("filmDesignSymbolHeaderNm", "filmLayerNm_"),
                ("filmDesignSymbolHeaderInsert", "filmDesignInsertPlaceholder_"),
                ("filmDesignSymbolHeaderDelete", "filmDesignDelete_"),
            )

            for window_width in (int(root.property("minimumWidth")), 1000, 1100, 1400):
                root.setWidth(window_width)
                QTest.qWait(50)
                design_x = design_column.mapToItem(content, QPointF(0, 0)).x()
                design_pos = design_column.mapToItem(content, QPointF(0, 0))
                expression_pos = expression_sidebar.mapToItem(content, QPointF(0, 0))
                bottom_layout = bool(root.property("sidebarUsesBottomLayout"))
                if bottom_layout:
                    self.assertGreaterEqual(expression_pos.y(), design_pos.y() + design_column.height() - 2)
                    self.assertGreaterEqual(expression_pos.x(), design_pos.x() - 2)
                else:
                    self.assertGreaterEqual(expression_pos.x(), design_x + design_column.width() - 2)
                    self.assertLessEqual(abs(expression_pos.y() - design_pos.y()), 2)
                self.assertLessEqual(expression_pos.x() + expression_sidebar.width(), root.width() + 1)
                self.assertEqual(expression_scroll.isVisible(), root.width() >= 1030)
                if root.width() < 1030:
                    self.assertLessEqual(
                        expression_sidebar.height() if bottom_layout else expression_sidebar.width(), 65)
                else:
                    self.assertGreater(expression_sidebar.width(), 300)
                self.assertLessEqual(expression_scroll.width(), root.width())
                expression_right = expression_scroll.mapToItem(
                    content, QPointF(expression_scroll.width(), 0)
                ).x()
                self.assertLessEqual(expression_right, root.width() + 1.0)

                for side_index, (view, stable_id) in enumerate(
                        ((front_view, front_id), (back_view, back_id))):
                    page_tabs.setProperty("currentIndex", side_index)
                    QTest.qWait(20)
                    self.assertTrue(view.isVisible())
                    for header_name, row_prefix in layer_header_row:
                        header = find_visual_item(content, header_name)
                        row_item = find_visual_item(view, row_prefix + stable_id)
                        self.assertIsNotNone(header, header_name)
                        self.assertIsNotNone(row_item, row_prefix + stable_id)
                        header_x = header.mapToItem(content, QPointF(0, 0)).x()
                        row_x = row_item.mapToItem(content, QPointF(0, 0)).x()
                        self.assertAlmostEqual(header_x, row_x, delta=1.0)
                        self.assertAlmostEqual(header.width(), row_item.width(), delta=1.0)
                        for field_name in ("filmLayerQw_" + stable_id,
                                           "filmLayerNm_" + stable_id):
                            field = find_visual_item(view, field_name)
                            self.assertIsNotNone(field, field_name)
                            implicit_height = float(field.property("implicitHeight"))
                            self.assertGreaterEqual(
                                field.height() + 1.0, implicit_height,
                                msg=(f"{field_name} height {field.height():.1f} is below "
                                     f"ThemeField implicitHeight {implicit_height:.1f}"),
                            )
                        viewport_right = view.mapToItem(
                            content, QPointF(view.width(), 0)
                        ).x()
                        header_right = header.mapToItem(
                            content, QPointF(header.width(), 0)
                        ).x()
                        row_right = row_item.mapToItem(
                            content, QPointF(row_item.width(), 0)
                        ).x()
                        self.assertLessEqual(header_right, viewport_right + 1.0)
                        self.assertLessEqual(row_right, viewport_right + 1.0)

                page_tabs.setProperty("currentIndex", 0)
                QTest.qWait(20)
                if root.width() < 1030:
                    self.assertFalse(expression_scroll.isVisible())
                    self.assertFalse(symbol_list.isVisible())
                    self.assertEqual(self.controller.expression_symbols.ids(), symbol_ids)
                    self.assertEqual(
                        self.controller.expression_symbols.record_for_id(symbol_id)["symbol"],
                        "A",
                    )
                else:
                    self.assertTrue(expression_scroll.isVisible())
                    for header_name, row_prefix in symbol_header_row:
                        header = find_visual_item(content, header_name)
                        row_item = find_visual_item(symbol_list, row_prefix + symbol_id)
                        self.assertIsNotNone(header, header_name)
                        self.assertIsNotNone(row_item, row_prefix + symbol_id)
                        header_x = header.mapToItem(content, QPointF(0, 0)).x()
                        row_x = row_item.mapToItem(content, QPointF(0, 0)).x()
                        self.assertAlmostEqual(header_x, row_x, delta=1.0)
                        self.assertAlmostEqual(header.width(), row_item.width(), delta=1.0)
                        viewport_right = symbol_list.mapToItem(
                            content, QPointF(symbol_list.width(), 0)
                        ).x()
                        header_right = header.mapToItem(
                            content, QPointF(header.width(), 0)
                        ).x()
                        row_right = row_item.mapToItem(
                            content, QPointF(row_item.width(), 0)
                        ).x()
                        self.assertLessEqual(header_right, viewport_right + 1.0)
                        self.assertLessEqual(row_right, viewport_right + 1.0)

            tab_names = (
                "filmDesignTabFront", "filmDesignTabBack",
                "filmDesignTabSubstrate",
            )
            tabs = [root.findChild(QQuickItem, name) for name in tab_names]
            self.assertTrue(all(tabs))
            self.assertEqual(page_tabs.property("count"), 3)
            tab_y = [tab.mapToItem(content, QPointF(0, 0)).y() for tab in tabs]
            self.assertTrue(all(abs(y - tab_y[0]) <= 1.0 for y in tab_y))
            back_tab = root.findChild(QQuickItem, "filmDesignTabBack")
            self.assertTrue(back_tab.property("enabled"))
            self.assertTrue(self.controller.setSubstrateValue("sides", "single"))
            QTest.qWait(30)
            self.assertFalse(back_tab.property("enabled"))
            self.assertTrue(str(root.property("title")).endswith(" *"))
            source = Path(__file__).parents[1] / "coat_sim" / "ui" / "qml" / "FilmDesignQuick.qml"
            source_text = source.read_text(encoding="utf-8")
            self.assertNotIn("uiState.path ||", source_text)
            self.assertNotIn("kindBox", source_text)
            self.assertNotIn("filmDesignTabMedia", source_text)
            self.assertNotIn("基片向外记录", source_text)

            # The substrate form is Loader-backed; activate it before looking
            # up its visual controls, then wait for the loaded item tree.
            page_tabs.setProperty("currentIndex", 2)
            self.assertEqual(page_tabs.property("currentIndex"), 2)
            substrate_loader = root.findChild(QQuickItem, "filmDesignSubstratePageLoader")
            substrate_names = (
                "filmDesignSubstratePageScroll",
                "filmDesignReferenceWavelength",
                "filmDesignSubstrateMaterial",
                "filmDesignSubstrateDataset",
                "filmDesignSubstrateSides",
                "filmDesignMediumEditor_incident_medium",
                "filmDesignMediumEditor_exit_medium",
                "filmDesignMediumMaterial_incident_medium",
                "filmDesignMediumMaterial_exit_medium",
                "filmDesignLegacyMedium_incident_medium",
            )
            substrate_controls = []
            for _ in range(150):
                QCoreApplication.processEvents()
                substrate_controls = [
                    find_visual_item(content, name) for name in substrate_names
                ]
                if all(substrate_controls):
                    break
                QTest.qWait(10)
            (substrate_scroll, reference_field, substrate_material,
             substrate_dataset, substrate_sides, incident_editor, exit_editor,
             incident_material, exit_material, legacy_incident) = substrate_controls
            missing_substrate_controls = [
                name for name, item in zip(substrate_names, substrate_controls) if item is None
            ]
            self.assertTrue(all((substrate_scroll, reference_field, substrate_material,
                                 substrate_dataset, substrate_sides, incident_editor, exit_editor,
                                 incident_material, exit_material, legacy_incident)),
                            f"missing visual controls: {missing_substrate_controls}; "
                            f"page index={page_tabs.property('currentIndex')}; "
                            f"loader active={substrate_loader.property('active') if substrate_loader else None}; "
                            f"loader source={substrate_loader.property('source') if substrate_loader else None}")
            QTest.qWait(30)
            self.assertTrue(substrate_scroll.isVisible())
            self.assertTrue(all(item.isVisible() for item in
                                (reference_field, substrate_material, substrate_dataset, substrate_sides,
                                 incident_editor, exit_editor, incident_material, exit_material)))
            self.assertFalse(legacy_incident.isVisible())
            self.assertEqual(incident_material.property("currentValue"), "builtin_ideal_air")
            incident_dataset = root.findChild(QQuickItem, "filmDesignMediumDataset_incident_medium")
            self.assertIsNotNone(incident_dataset)
            self.assertEqual(incident_dataset.property("currentValue"), "ideal_air_n1_k0")
            self.assertEqual(self.controller.currentDesign()["incident_medium"],
                             {"kind": "material", "material_id": "builtin_ideal_air",
                              "optical_set_id": "ideal_air_n1_k0"})

            def read_qml_property(scope, expression):
                item_context = QQmlEngine.contextForObject(scope) or engine.rootContext()
                query = QQmlExpression(item_context, scope, expression)
                value, undefined = query.evaluate()
                self.assertFalse(query.hasError(), query.error().toString())
                self.assertFalse(undefined, expression)
                return value

            def click_popup_item(item):
                target = _visual_window_for(item)
                point = target.contentItem().mapFromItem(
                    item, QPointF(item.width() / 2, item.height() / 2)
                ).toPoint()
                QTest.mouseClick(target, Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier, point)
                QTest.qWait(40)

            material_option = next(
                option for option in self.controller.substrate_options
                if option.get("optical_sets")
            )
            click_item(substrate_material)
            material_option_index = next(
                index for index, option in enumerate(self.controller.substrateOptions)
                if option.get("id") == material_option["id"]
            )
            self.assertTrue(substrate_material.property("popupVisible"))
            material_options = substrate_material.property("optionsListView")
            self.assertIsNotNone(material_options)
            material_options.positionViewAtIndex(material_option_index, 1)
            QTest.qWait(40)
            material_item = read_qml_property(
                substrate_material, f"optionsListView.itemAtIndex({material_option_index})"
            )
            self.assertIsNotNone(material_item)
            click_popup_item(material_item)
            self.assertEqual(
                self.controller.currentDesign()["substrate"]["material_id"],
                material_option["id"],
            )
            optical_set = material_option["optical_sets"][0]
            self.assertEqual(self.controller.currentDesign()["substrate"]["optical_set_id"],
                             optical_set["id"])
            substrate_sets = read_qml_property(substrate_dataset, "model")
            optical_set = material_option["optical_sets"][0]
            optical_set_index = next(
                index for index, option in enumerate(substrate_sets)
                if option.get("id") == optical_set["id"]
            )
            click_item(substrate_dataset)
            substrate_set_item = read_qml_property(
                substrate_dataset, f"popup.contentItem.itemAtIndex({optical_set_index})"
            )
            self.assertIsNotNone(substrate_set_item)
            click_popup_item(substrate_set_item)
            self.assertEqual(
                self.controller.currentDesign()["substrate"]["optical_set_id"],
                optical_set["id"],
            )

            use_k = root.findChild(QQuickItem, "filmDesignUseK")
            self.assertTrue(use_k)
            previous_use_k = bool(self.controller.designMeta["use_k"])
            click_item(use_k)
            self.assertEqual(bool(self.controller.designMeta["use_k"]), not previous_use_k)
            self.assertTrue(incident_editor.isVisible())
            self.assertTrue(exit_editor.isVisible())

            toggle = root.findChild(QQuickItem, "filmDesignSidebarToggle")
            sidebar = root.findChild(QQuickItem, "filmDesignExpressionSidebar")
            self.assertTrue(toggle)
            self.assertTrue(sidebar)
            root.setWidth(1400)
            QTest.qWait(40)
            expanded_width = sidebar.width()

            click_item(toggle)
            self.assertFalse(expression_scroll.isVisible())
            self.assertLess(sidebar.width(), expanded_width / 2)
            click_item(toggle)
            self.assertTrue(expression_scroll.isVisible())
        finally:
            self.dispose_quick_window(engine, root)

    def test_expression_symbol_table_and_action_row_fit_at_dpi_scale(self):
        self.assertTrue(self.controller.addSymbol())
        stable_id = self.stable_id_for_symbol("A")
        engine, _component, root = self.create_quick_window()
        try:
            scale = float(os.environ.get("QT_SCALE_FACTOR", "1"))
            screenshot_path = os.environ.get("FILM_QUICK_LAYOUT_SCREENSHOT")
            screen = _APP.primaryScreen()
            self.assertIsNotNone(screen)
            self.assertTrue(_CJK_FONT_FAMILY, "bundled Noto CJK font was not registered")
            self.assertAlmostEqual(float(screen.devicePixelRatio()), scale, delta=0.05)

            expression_panel_qml = (
                Path(__file__).parents[1] / "coat_sim/ui/qml/FilmDesignExpressionPanel.qml"
            ).read_text(encoding="utf-8")
            self.assertNotIn('text: "表达式符号"', expression_panel_qml)

            content = root.contentItem()
            sidebar = root.findChild(QQuickItem, "filmDesignExpressionSidebar")
            panel = root.findChild(QQuickItem, "filmDesignExpressionPanel")
            symbol_list = root.findChild(QQuickItem, "filmDesignSymbolList")
            actions = root.findChild(QQuickItem, "filmDesignSymbolHeader")
            action_names = (
                "filmDesignAddSymbol", "filmDesignSaveExpressionTemplate",
                "filmDesignLoadExpressionTemplate", "filmDesignApplyExpression",
            )
            action_buttons = [root.findChild(QQuickItem, name) for name in action_names]
            self.assertTrue(all((sidebar, panel, symbol_list, actions, *action_buttons)))

            def find_visual_item(parent, object_name):
                for child in parent.childItems():
                    if child.objectName() == object_name:
                        return child
                    found = find_visual_item(child, object_name)
                    if found is not None:
                        return found
                return None

            row_pairs = (
                ("filmDesignSymbolHeaderIndex", "filmLayerSymbol_" + stable_id),
                ("filmDesignSymbolHeaderMaterial", "filmLayerMaterial_" + stable_id),
                ("filmDesignSymbolHeaderDataset", "filmLayerOpticalSet_" + stable_id),
                ("filmDesignSymbolHeaderQw", "filmLayerQw_" + stable_id),
                ("filmDesignSymbolHeaderNm", "filmLayerNm_" + stable_id),
                ("filmDesignSymbolHeaderInsert", "filmDesignInsertPlaceholder_" + stable_id),
                ("filmDesignSymbolHeaderDelete", "filmDesignDelete_" + stable_id),
            )
            for width in (1100, 1400):
                root.setWidth(width)
                root.setHeight(900)
                QTest.qWait(50)
                sidebar_pos = sidebar.mapToItem(content, QPointF(0, 0))
                panel_pos = panel.mapToItem(content, QPointF(0, 0))
                self.assertTrue(symbol_list.isVisible())
                self.assertGreaterEqual(panel_pos.x(), sidebar_pos.x() + 38)
                self.assertLessEqual(sidebar_pos.x() + sidebar.width(), root.width() + 1)
                self.assertLessEqual(panel_pos.x() + panel.width(), root.width() + 1)

                # Numeric ThemeFields must retain their padded text content at
                # the actual DPI-scaled row height, not only expose a nonzero
                # header/column geometry.
                for field_name in ("filmLayerQw_" + stable_id,
                                   "filmLayerNm_" + stable_id):
                    field = find_visual_item(symbol_list, field_name)
                    self.assertIsNotNone(field, field_name)
                    implicit_height = float(field.property("implicitHeight"))
                    self.assertGreaterEqual(
                        field.height() + 1.0, implicit_height,
                        msg=(f"{field_name} height {field.height():.1f} is below "
                             f"ThemeField implicitHeight {implicit_height:.1f}"),
                    )

                action_rects = []
                for button in action_buttons:
                    self.assertTrue(button.isVisible(), button.objectName())
                    pos = button.mapToItem(content, QPointF(0, 0))
                    action_rects.append((pos.x(), pos.y(), button.width(), button.height()))
                    self.assertLessEqual(
                        pos.x() + button.width(), panel_pos.x() + panel.width() + 1,
                        msg=(f"{button.objectName()} rect={pos.x():.1f}+{button.width():.1f}, "
                             f"panel={panel_pos.x():.1f}+{panel.width():.1f}, "
                             f"actions={actions.width():.1f}, rowParent="
                             f"{actions.parentItem().width():.1f}x"
                             f"{actions.parentItem().height():.1f}"),
                    )
                for previous, current in zip(action_rects, action_rects[1:]):
                    self.assertLessEqual(abs(previous[1] - current[1]), 1.0)
                    self.assertGreaterEqual(current[0], previous[0] + previous[2] - 1.0)

                previous_right = None
                for header_name, row_name in row_pairs:
                    header = find_visual_item(content, header_name)
                    row_item = find_visual_item(symbol_list, row_name)
                    self.assertIsNotNone(header, header_name)
                    self.assertIsNotNone(row_item, row_name)
                    header_x = header.mapToItem(content, QPointF(0, 0)).x()
                    row_x = row_item.mapToItem(content, QPointF(0, 0)).x()
                    self.assertAlmostEqual(header_x, row_x, delta=1.0,
                                           msg=f"{header_name} x misaligned at {width}px")
                    self.assertAlmostEqual(header.width(), row_item.width(), delta=1.0,
                                           msg=f"{header_name} width differs at {width}px")
                    self.assertGreater(header.width(), 0)
                    if previous_right is not None:
                        self.assertGreaterEqual(header_x, previous_right - 1.0,
                                                msg=f"{header_name} overlaps preceding column")
                    previous_right = header_x + header.width()

                qw_header = root.findChild(QQuickItem, "filmDesignSymbolHeaderQw")
                nm_header = root.findChild(QQuickItem, "filmDesignSymbolHeaderNm")
                self.assertEqual(qw_header.property("text"), "光学厚度\nQW")
                self.assertEqual(nm_header.property("text"), "物理厚度\nnm")
                self.assertGreater(qw_header.height(), 0)
                self.assertGreater(nm_header.height(), 0)
                if width == 1100 and screenshot_path:
                    screenshot = Path(screenshot_path)
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    image = root.grabWindow()
                    self.assertFalse(image.isNull(), "offscreen Quick screenshot is empty")
                    self.assertTrue(image.save(str(screenshot), "PNG"))
        finally:
            self.dispose_quick_window(engine, root)

    def test_qml_expression_draft_debounces_and_survives_cfilm_save_reload(self):
        engine, component, root = self.create_quick_window()
        try:
            editor = root.findChild(QQuickItem, "filmDesignExpressionArea")
            self.assertIsNotNone(editor)
            editor.forceActiveFocus()
            QTest.qWait(20)
            editor.setProperty("text", "g1 a")
            QTest.qWait(40)
            self.assertEqual(self.controller.expressionText, "")
            QTest.qWait(220)
            self.assertEqual(self.controller.expressionText, "g1 a")

            path = self.root / "draft-save.cfilm"
            self.assertTrue(self.controller.saveFilePath(str(path), False))
            reopened = FilmDesignQuickController(self.root, service=self.service)
            reopened.requestOpenFile(QUrl.fromLocalFile(str(path)))
            self.assertEqual(reopened.currentDesign(), self.service.load(path))
            self.assertEqual(reopened.expressionText, "G1 A")
        finally:
            self.dispose_quick_window(engine, root)

    def test_physical_nm_edit_commits_three_decimal_precision(self):
        self.install_film_fixtures()
        self.assertTrue(self.controller.addLayer("front"))
        layer_id = self.controller.front_layers.ids()[0]
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "material_id", "quick_fixture"))
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "optical_set_id", "n15"))
        precise_nm = 123.123456789
        self.assertTrue(self.controller.setLayerValue("front", layer_id, "thickness.nm", precise_nm))
        row = self.controller.front_layers.record_for_id(layer_id)
        self.assertEqual(row["physicalNmText"], format(precise_nm, ".3f"))
        self.assertEqual(row["physicalNmValue"], 123.123)

        path = self.root / "physical-precision.cfilm"
        self.assertTrue(self.controller.saveFilePath(str(path), False))
        reopened = FilmDesignQuickController(self.root, service=self.service)
        reopened.requestOpenFile(QUrl.fromLocalFile(str(path)))
        thickness = reopened.currentDesign()["front_layers"][0]["thickness"]
        self.assertEqual(thickness, {"value": 123.123, "unit": "nm"})
        saved_row = reopened.front_layers.record_for_id(layer_id)
        self.assertEqual(saved_row["physicalNmText"], format(precise_nm, ".3f"))
        self.assertEqual(saved_row["physicalNmValue"], 123.123)


if __name__ == "__main__":
    unittest.main()
