"""Independent CPU-only checks for typed material/source integration."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import TypeAdapter

from coat_sim.core.api import CoatService
from coat_sim.core.design_workflow import DesignWorkflowSpec, prepare_design_bundle
from coat_sim.core.materials import (default_material_profile, save_material,
                                    read_material_profile, list_materials)
from coat_sim.core.schemas import (SourceDesign, MaterialRecord,
                                   ElectronBeamEmission, design_data)
from coat_sim.core.source_profiles import validate_source_profile
from coat_sim.core.simulator import Settings
from tests.test_input_snapshot import fixture


def material_source(*, explicit_model=False):
    material = default_material_profile("QA material", "qa-material")
    material["electron_beam_model"]["defaults"].update(
        source_exponent=4., reference_rate_nm_s=.25, spot_sigma_mm=8.)
    source = {"kind": "disk", "radius_mm": 10.,
              "device_type": "electron_beam", "material": material}
    if explicit_model:
        source["emission_model"] = {"kind": "electron_beam"}
    return source


class MaterialSourceTypedIntegrationTests(unittest.TestCase):
    def test_typed_source_does_not_replace_omitted_material_defaults(self):
        for explicit_model in (False, True):
            with self.subTest(explicit_model=explicit_model):
                source = material_source(explicit_model=explicit_model)
                before = deepcopy(source)
                expected = validate_source_profile(source)
                typed = TypeAdapter(SourceDesign).validate_python(source)
                self.assertEqual(design_data("source", typed), expected)
                self.assertEqual(source, before)
                self.assertEqual(typed.source_exponent, 4.)
                self.assertEqual(typed.emission_model.reference_rate_nm_s, .25)
                self.assertEqual(typed.emission_model.spot_sigma_mm, 8.)

    def test_nested_typed_material_and_model_preserve_omitted_fields(self):
        source = material_source(explicit_model=True)
        expected = validate_source_profile(source)
        source["material"] = MaterialRecord.model_validate(source["material"])
        source["emission_model"] = ElectronBeamEmission(kind="electron_beam")
        actual = TypeAdapter(SourceDesign).validate_python(source)
        self.assertEqual(design_data("source", actual), expected)

    def test_explicit_overrides_and_legacy_sources_remain_authoritative(self):
        source = material_source(explicit_model=True)
        source["source_exponent"] = 2.
        source["emission_model"].update(reference_rate_nm_s=.5, spot_sigma_mm=2.)
        expected = validate_source_profile(source)
        self.assertEqual(design_data("source", TypeAdapter(SourceDesign).validate_python(source)), expected)
        legacy = {"schema_version": 2, "kind": "disk", "radius_mm": 10.,
                  "device_type": "electron_beam", "source_exponent": 3.}
        actual = design_data("source", TypeAdapter(SourceDesign).validate_python(legacy))
        self.assertEqual(actual, validate_source_profile(legacy))
        self.assertNotIn("material", actual)
        self.assertNotIn("emission_model", actual)

    def test_workflow_generation_and_persistence_use_same_material_defaults(self):
        source = material_source(explicit_model=True)
        spec = {"name": "Typed material workflow",
                "sources": [{"design": source, "x_mm": 0., "y_mm": 0., "z_mm": 0.}],
                "substrate": {"schema_version": 1, "name": "QA substrate",
                              "outline": {"kind": "circle", "radius_mm": 10.},
                              "surface": {"kind": "flat"}},
                "film_stack": {"mode": "equal", "sources": [{"source_slot": 1, "mask_slots": []}],
                               "sequence": ["QA material"], "layer_count": 2,
                               "total_thickness_nm": 10.},
                "settings": {"grid_size": 21, "time_steps": 2}, "backend": "CPU"}
        typed = DesignWorkflowSpec.model_validate(spec)
        self.assertEqual(typed.sources[0].design.source_exponent, 4.)
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            data = project / "data"
            data.mkdir()
            service = CoatService(data)
            with patch("coat_sim.core.data_files.PROJECT_ROOT", project), \
                    patch("coat_sim.core.data_files.DATA_ROOT", data):
                manifest = prepare_design_bundle(service, "qa-material-defaults", spec)
            saved = service.load("source", manifest["paths"]["sources"][0])
            process = service.load("process", manifest["paths"]["process"])
            self.assertTrue(process.valid, process.issues)
            self.assertEqual(saved.data, validate_source_profile(source))
            self.assertEqual([step["rate_nm_s"] for step in process.data["steps"]], [.25, .25])
            self.assertEqual([step["mask_slots"] for step in process.data["steps"]], [[], []])


class MaterialSourceEstimateIntegrationTests(unittest.TestCase):
    def test_cycle_work_estimate_keys_explicit_rate_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, recipe = fixture(root, mask_count=0)
            recipe["steps"] = [dict(source_slot=1, target_nm=120., rate_nm_s=rate,
                                    target_mode="mean", mask_slots=[]) for rate in (1., 10.)]
            service.save("process", root / "recipe.cproc", recipe, overwrite=True)
            for response, expected_calls in ((1., 2), (0., 1)):
                source = {"schema_version": 2, "kind": "point",
                          "emission_model": {"kind": "resistance_boat",
                                             "apertures": [{"kind": "rectangle"}],
                                             "rate_exponent_per_decade": response}}
                service.save("source", root / "parts/source.csrc", source, overwrite=True)
                with self.subTest(response=response), \
                        patch("coat_sim.core.simulator.workload", return_value=1.) as work:
                    total = service.work_index(Settings(grid_size=21, time_steps=240),
                                               process_path=root / "recipe.cproc", fast_cycle=True)
                    self.assertEqual(work.call_count, expected_calls)
                    self.assertEqual(total, expected_calls)
                    self.assertEqual([call.args[0].source_rate_nm_s for call in work.call_args_list],
                                     [1., 10.] if response else [1.])


class MaterialLibraryPersistenceIntegrationTests(unittest.TestCase):
    def test_failed_new_material_flush_publishes_nothing_and_retry_works(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = save_material(root, default_material_profile("Existing", "existing"))
            candidate = default_material_profile("Candidate", "candidate")
            with patch("coat_sim.core.materials.os.fsync", side_effect=OSError("injected disk failure")):
                with self.assertRaises(OSError):
                    save_material(root, candidate)
            self.assertCountEqual(list(root.iterdir()), [root / "existing.json", root / ".material-library.lock"])
            self.assertEqual(list_materials(root), [existing])
            self.assertEqual(save_material(root, candidate), candidate)

    def test_failed_overwrite_retains_complete_old_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = save_material(root, default_material_profile("Existing", "existing"))
            revised = deepcopy(old)
            revised["name"] = "Revised"
            with patch("coat_sim.core.materials.os.fsync", side_effect=OSError("injected disk failure")):
                with self.assertRaises(OSError):
                    save_material(root, revised, overwrite=True)
            self.assertEqual(read_material_profile(root / "existing.json"), old)
            self.assertCountEqual(list(root.iterdir()), [root / "existing.json", root / ".material-library.lock"])


class SourceMaterialQmlInterruptionIntegrationTests(unittest.TestCase):
    def test_save_and_continue_cancel_write_failure_and_retry(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
        from PySide6.QtCore import QUrl, QMetaObject, QCoreApplication, QEvent
        from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression, QQmlEngine
        from PySide6.QtWidgets import QApplication
        from coat_sim.ui.source_substrate import SourceBridge
        from coat_sim.ui.theme_service import ThemeService

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("coat_sim.core.data_files.PROJECT_ROOT", root), \
                    patch("coat_sim.core.data_files.DATA_ROOT", root / "data"):
                bridge = SourceBridge(CoatService(root))
                engine = QQmlApplicationEngine()
                warnings = []
                engine.warnings.connect(lambda messages: warnings.extend(message.toString() for message in messages))
                engine.rootContext().setContextProperty("bridge", bridge)
                ThemeService.instance().attach_engine(engine, "Theme")
                qml = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml/SourceWindow.qml"
                engine.load(QUrl.fromLocalFile(str(qml)))
                self.assertTrue(engine.rootObjects())
                window = engine.rootObjects()[0]
                context = QQmlEngine.contextForObject(window)

                def expression(text):
                    expr = QQmlExpression(context, window, text)
                    result, _ = expr.evaluate()
                    self.assertFalse(expr.hasError(), expr.error())
                    return result

                def signal(object_name, method):
                    self.assertTrue(QMetaObject.invokeMethod(expression(object_name), method))
                    app.processEvents()

                try:
                    bridge.setField("source_exponent", "2")
                    before = deepcopy(bridge.documentData())
                    bridge.newDocument()
                    self.assertEqual(bridge.pendingAction, "new")
                    self.assertTrue(QMetaObject.invokeMethod(window, "saveAndContinue"))
                    app.processEvents()
                    self.assertTrue(expression("saveThenContinue"))
                    signal("saveDialog", "rejected")
                    self.assertEqual(bridge.documentData(), before)
                    self.assertTrue(bridge.dirty)
                    self.assertEqual(bridge.pendingAction, "")

                    bridge.newDocument()
                    self.assertTrue(QMetaObject.invokeMethod(window, "saveAndContinue"))
                    app.processEvents()
                    target = root / "new.csrc"
                    expression("saveDialog").setProperty("selectedFile", QUrl.fromLocalFile(str(target)))
                    with patch.object(bridge.service, "save", side_effect=OSError("injected full disk")):
                        signal("saveDialog", "accepted")
                    self.assertEqual(bridge.documentData(), before)
                    self.assertTrue(bridge.dirty)
                    self.assertEqual(bridge.pendingAction, "new")
                    self.assertTrue(expression("discardDialog.visible"))
                    self.assertFalse(target.exists())

                    self.assertTrue(QMetaObject.invokeMethod(window, "saveAndContinue"))
                    app.processEvents()
                    expression("saveDialog").setProperty("selectedFile", QUrl.fromLocalFile(str(target)))
                    signal("saveDialog", "accepted")
                    self.assertEqual(bridge.pendingAction, "")
                    self.assertFalse(bridge.dirty)
                    self.assertEqual(bridge.documentData()["source_exponent"], 1.)
                    self.assertEqual(bridge.service.load("source", target).data["source_exponent"], 2.)
                    self.assertFalse(warnings, "\n".join(warnings))
                finally:
                    window.setProperty("allowClose", True)
                    window.close()
                    engine.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                    app.processEvents()


if __name__ == "__main__":
    unittest.main()
