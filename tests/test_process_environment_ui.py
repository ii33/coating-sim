"""Cancel-safe process defaults, SI units, inheritance and gas input controls."""
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

from coat_sim.ui.process import ProcessBridge
from tests.test_process_optimization_modes import fixture

ROOT = Path(__file__).resolve().parents[1]


def mixture():
    return {"transport_model": "ballistic_loss", "base_pressure_pa": 0.00001,
            "operating_pressure_pa": 0.001, "gas_temperature_k": 300.,
            "substrate_temperature_k": 373.15, "source_temperature_k": 1200.,
            "effective_cross_section_m2": 1e-19,
            "effective_cross_section_source": "Explicit test assumption, not measured"}


class ProcessEnvironmentUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service, self.path = fixture(self.root)
        self.bridge = ProcessBridge(self.service, str(self.path))
        self.directory_patch = patch("coat_sim.ui.process.design_directory", return_value=self.root)
        self.directory_patch.start()
        self.engines = []
        self.components = []
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
        for engine in self.engines:
            engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.bridge.cancel()
        self.directory_patch.stop()
        self.tmp.cleanup()

    def expression(self, target, text):
        expression = QQmlExpression(QQmlEngine.contextForObject(target), target, text)
        value, _ = expression.evaluate()
        self.assertFalse(expression.hasError(), expression.error().toString())
        return value.toVariant() if hasattr(value, "toVariant") else value

    def window(self):
        engine = QQmlApplicationEngine()
        self.engines.append(engine)
        engine.rootContext().setContextProperty("bridge", self.bridge)
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/ProcessWindow.qml")))
        self.components.append(component)
        self.assertFalse(component.isError(), component.errors())
        window = component.create()
        self.assertIsNotNone(window, component.errors())
        self.windows.append(window)
        window.showNormal()
        window.resize(1060, 620)
        QTest.qWait(40)
        return window

    def visual_item(self, window, name):
        pending = [window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName() == name:
                return item
            pending.extend(item.childItems())
        self.fail(f"Missing rendered item: {name}")

    def apply(self, index, data):
        result = self.bridge.applyEnvironment(index, data)
        self.assertTrue(result["ok"], result["message"])

    def test_legacy_read_edit_other_fields_and_empty_reset_do_not_add_environment(self):
        original = self.bridge.document
        state = self.bridge.environmentEditor(-1)
        self.assertEqual(state["raw"], {})
        self.assertEqual(self.bridge.environmentEditor(0)["effective"], {})
        self.assertIn("兼容旧工艺", self.bridge.environmentDefaultsSummary)
        self.apply(-1, {})
        self.apply(0, {})
        self.assertFalse(self.bridge.dirty)
        self.assertEqual(self.bridge.document, original)
        self.bridge.setStep(0, "rate_angstrom_s", "15")
        self.assertNotIn("environment", self.bridge.document)
        self.assertNotIn("environment", self.bridge.steps[0])
        self.bridge.save()
        self.assertNotIn("environment", json.loads(self.path.read_text()))

    def test_si_defaults_override_clear_and_saved_reopen_preserve_source_and_masks(self):
        original_steps = self.bridge.steps
        self.apply(-1, mixture())
        self.apply(0, {"substrate_temperature_k": None, "source_temperature_k": "1250", "transport_model": "disabled"})
        state = self.bridge.environmentEditor(0)
        self.assertNotIn("substrate_temperature_k", state["effective"])
        self.assertEqual(state["inherited"]["substrate_temperature_k"], 373.15)
        self.assertEqual(state["effective"]["source_temperature_k"], 1250.)
        self.assertEqual(state["effective"]["operating_pressure_pa"], .001)
        self.assertEqual(self.bridge.environmentEditor(1)["effective"], mixture())
        self.bridge.save()
        reopened = ProcessBridge(self.service, str(self.path))
        self.assertEqual(reopened.document, self.bridge.document)
        self.assertIsNone(reopened.steps[0]["environment"]["substrate_temperature_k"])
        for index, step in enumerate(original_steps):
            for key, value in step.items():
                self.assertEqual(reopened.steps[index][key], value)
        self.apply(0, {})
        self.assertNotIn("environment", self.bridge.steps[0])
        self.assertEqual(self.bridge.environmentEditor(0)["effective"], mixture())
        self.apply(0, {"transport_model": None})
        self.assertNotIn("transport_model", self.bridge.environmentEditor(0)["effective"])

    def test_gas_components_replace_whole_array_without_mutation_and_roundtrip(self):
        defaults = mixture()
        defaults.pop("effective_cross_section_m2")
        defaults.pop("effective_cross_section_source")
        defaults["gas_components"] = [
            {"species": "Ar", "partial_pressure_pa": .0008, "effective_cross_section_m2": 2e-19,
             "flow_sccm": 10., "cross_section_source": "Test assumption Ar"},
            {"species": "O2", "partial_pressure_pa": .0002, "effective_cross_section_m2": 3e-19,
             "cross_section_source": "Test assumption O2"}]
        before = deepcopy(defaults)
        self.apply(-1, defaults)
        overrides = {"gas_components": [{"species": "N2", "partial_pressure_pa": .001,
                     "effective_cross_section_m2": 4e-19, "flow_sccm": 3.,
                     "cross_section_source": "Test assumption N2"}]}
        self.apply(0, overrides)
        self.assertEqual(defaults, before)
        self.assertEqual(len(self.bridge.environmentEditor(0)["effective"]["gas_components"]), 1)
        self.assertEqual(self.bridge.environmentEditor(1)["effective"]["gas_components"], defaults["gas_components"])
        detached = self.bridge.environmentEditor(0)
        detached["raw"]["gas_components"][0]["species"] = "wrong"
        self.assertEqual(self.bridge.steps[0]["environment"]["gas_components"][0]["species"], "N2")
        self.bridge.save()
        self.bridge.newDocument()
        self.bridge.openPath(QUrl.fromLocalFile(str(self.path)))
        self.assertEqual(self.bridge.steps[0]["environment"], overrides)
        self.apply(0, {"transport_model": "disabled", "gas_components": None})
        self.assertNotIn("gas_components", self.bridge.environmentEditor(0)["effective"])

    def test_invalid_physics_inputs_reject_atomically_and_do_not_block_new_recipe_metadata(self):
        original = self.bridge.document
        invalid = [
            {"operating_pressure_pa": -1}, {"gas_temperature_k": 0},
            {"effective_cross_section_m2": "nan"}, {"base_pressure_pa": "bad"},
            {"transport_model": "ballistic_loss", "operating_pressure_pa": .001},
            {**mixture(), "effective_cross_section_m2": None},
            {**mixture(), "effective_cross_section_m2": None,
             "gas_components": [{"species": "Ar", "partial_pressure_pa": .002, "effective_cross_section_m2": 1e-19}]},
        ]
        for values in invalid:
            with self.subTest(values=values):
                result = self.bridge.applyEnvironment(-1, values)
                self.assertFalse(result["ok"])
                self.assertTrue(result["message"])
                self.assertEqual(self.bridge.document, original)
                self.assertFalse(self.bridge.dirty)
        self.bridge.newDocument()
        self.apply(-1, {"transport_model": "disabled", "source_temperature_k": "500",
                       "gas_components": [{"species": "Ar", "flow_sccm": "20"}]})
        self.assertFalse(self.bridge.valid)  # Missing dependencies are independent of input editing.
        self.assertEqual(self.bridge.document["environment"]["source_temperature_k"], 500.)
        self.assertNotIn("operating_pressure_pa", self.bridge.document["environment"])
        self.assertNotIn("partial_pressure_pa", self.bridge.document["environment"]["gas_components"][0])

    def test_defaults_update_checks_all_layers_and_step_selection_crosses_pages(self):
        document = self.bridge.document
        document["steps"] = [deepcopy(document["steps"][0]) for _ in range(42)]
        self.bridge.applyDocument(document, str(self.path))
        self.apply(-1, mixture())
        self.apply(41, {"gas_temperature_k": 400.})
        before = self.bridge.document
        result = self.bridge.applyEnvironment(-1, {**mixture(), "gas_temperature_k": None})
        self.assertFalse(result["ok"])
        self.assertIn("第 1 层", result["message"])
        self.assertEqual(self.bridge.document, before)
        window = self.window()
        self.expression(window, "environmentStep.value = 42")
        window.findChild(QObject, "editStepEnvironment").clicked.emit()
        self.assertEqual(self.expression(window, "environmentDialog.stepIndex"), 41)
        self.expression(window, "environmentDialog.reject()")
        self.assertEqual(self.bridge.steps[41]["environment"], {"gas_temperature_k": 400.})

    def test_modal_cancel_failed_apply_reset_and_reopen_are_cancel_safe(self):
        window = self.window()
        dialog = window.findChild(QObject, "processEnvironmentDialog")
        before = self.bridge.document
        window.findChild(QObject, "editEnvironmentDefaults").clicked.emit()
        self.assertTrue(dialog.property("opened"))
        # Change an actual field and its explicit state without committing.
        field = self.visual_item(window, "environmentValue_substrate_temperature_k")
        mode = self.visual_item(window, "environmentState_substrate_temperature_k")
        mode.setProperty("currentIndex", 1)
        mode.activated.emit(1)
        field.setProperty("text", "373.15")
        field.textEdited.emit()
        dialog.findChild(QObject, "cancelEnvironment").clicked.emit()
        self.assertEqual(self.bridge.document, before)
        self.assertFalse(self.bridge.dirty)
        self.expression(window, "environmentDialog.prepare(-1)")
        self.assertEqual(self.expression(dialog, "collect()"), {})
        self.expression(dialog, 'fill({transport_model: "ballistic_loss", operating_pressure_pa: 0.001})')
        self.assertFalse(self.expression(dialog, "applyDraft()"))
        self.assertTrue(dialog.property("visible"))
        self.assertTrue(dialog.property("errorMessage"))
        self.assertEqual(self.bridge.document, before)
        self.expression(dialog, "fill(" + json.dumps(mixture()) + ")")
        self.assertTrue(self.expression(dialog, "applyDraft()"))
        self.assertEqual(self.bridge.document["environment"], mixture())
        self.expression(window, "environmentDialog.prepare(0)")
        self.expression(dialog, 'fill({substrate_temperature_k: null, source_temperature_k: 1400})')
        self.assertTrue(self.expression(dialog, "applyDraft()"))
        self.expression(window, "environmentDialog.prepare(0)")
        self.assertIsNone(self.expression(dialog, "collect().substrate_temperature_k"))
        dialog.findChild(QObject, "resetEnvironment").clicked.emit()
        self.assertEqual(self.expression(dialog, "collect()"), {})
        dialog.findChild(QObject, "cancelEnvironment").clicked.emit()
        self.assertIn("environment", self.bridge.steps[0])
        self.expression(window, "environmentDialog.prepare(0)")
        dialog.findChild(QObject, "resetEnvironment").clicked.emit()
        self.assertTrue(self.expression(dialog, "applyDraft()"))
        self.assertNotIn("environment", self.bridge.steps[0])
        self.expression(window, "environmentDialog.prepare(-1)")
        self.assertEqual(self.expression(dialog, "collect().source_temperature_k"), "1200")
        self.assertIn("归一化", dialog.findChild(QObject, "environmentModelLimitations").property("text"))

    def test_transport_visibility_respects_inheritance_and_keeps_hidden_values(self):
        self.apply(-1, mixture())
        window = self.window()
        dialog = window.findChild(QObject, "processEnvironmentDialog")
        self.expression(window, "environmentDialog.prepare(0)")
        self.assertTrue(dialog.property("transportEnabled"))
        self.assertTrue(self.visual_item(window, "environmentRow_gas_temperature_k").isVisible())
        self.expression(dialog, 'fill({transport_model:"disabled", gas_temperature_k:350, operating_pressure_pa:0.002})')
        self.app.processEvents()
        self.assertFalse(dialog.property("transportEnabled"))
        self.assertFalse(self.visual_item(window, "environmentRow_gas_temperature_k").isVisible())
        self.assertTrue(self.visual_item(window, "environmentRow_source_temperature_k").isVisible())
        self.assertFalse(dialog.findChild(QObject, "environmentGasGroup").property("visible"))
        self.assertEqual(self.expression(dialog, "collect().gas_temperature_k"), "350")
        self.assertTrue(self.expression(dialog, "applyDraft()"))
        self.assertEqual(self.bridge.steps[0]["environment"]["gas_temperature_k"], 350.)
        self.expression(window, "environmentDialog.prepare(0)")
        self.assertFalse(dialog.property("transportEnabled"))
        self.expression(dialog, 'fill({})')
        self.app.processEvents()
        self.assertTrue(dialog.property("transportEnabled"))
        self.expression(dialog, 'fill({transport_model:null})')
        self.app.processEvents()
        self.assertFalse(dialog.property("transportEnabled"))
        self.assertIsNone(self.expression(dialog, "collect().transport_model"))

    def test_gas_controls_roundtrip_unknown_pressure_and_fit_minimum_window(self):
        window = self.window()
        dialog = window.findChild(QObject, "processEnvironmentDialog")
        self.expression(window, "environmentDialog.prepare(-1)")
        self.expression(dialog, 'fill({transport_model:"disabled", gas_components:[{species:"Ar", flow_sccm:15}]})')
        self.assertEqual(self.expression(dialog, "collect().gas_components[0]"), {"species": "Ar", "flow_sccm": "15"})
        self.assertTrue(self.expression(dialog, "applyDraft()"))
        self.expression(window, "environmentDialog.prepare(-1)")
        dialog.findChild(QObject, "addEnvironmentGas").clicked.emit()
        self.assertEqual(len(self.expression(dialog, "collect().gas_components")), 2)
        self.visual_item(window, "removeEnvironmentGas1").clicked.emit()
        self.assertEqual(len(self.expression(dialog, "collect().gas_components")), 1)
        for width, height in ((1060, 620), (1280, 900), (1060, 620)):
            window.resize(width, height)
            QTest.qWait(40)
            self.assertLessEqual(dialog.property("width"), width - 32)
            self.assertLessEqual(dialog.property("height"), height - 32)
            self.assertGreater(dialog.property("height"), 400)
            scroll = dialog.findChild(QObject, "environmentScroll")
            self.assertLessEqual(scroll.property("contentWidth"), scroll.property("availableWidth"))
            self.assertGreater(scroll.property("height"), 150)
            image = window.grabWindow()
            self.assertFalse(image.isNull())
        output = os.environ.get("COAT_PREVIEW_SCREENSHOT_DIR")
        if output:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            self.assertTrue(image.save(str(directory / "process-environment-minimum-size.png")))
        scroll.property("contentItem").setProperty("contentY", scroll.property("contentHeight") - scroll.property("availableHeight"))
        QTest.qWait(40)
        if output:
            self.assertTrue(window.grabWindow().save(str(directory / "process-environment-gas-controls.png")))


if __name__ == "__main__":
    unittest.main()
