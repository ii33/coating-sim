"""Real Qt editor coverage for provenance, transactional scenarios and old sources."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication
from coat_sim.core.api import CoatService
from coat_sim.core.materials import builtin_material_profiles, default_material_profile, save_material
from tests.material_test_helpers import MaterialQtCase
from coat_sim.ui.source_substrate import SourceBridge
from coat_sim.ui.theme_service import ThemeService

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


class ZnSReferenceEditorTests(MaterialQtCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("PROJECT_ROOT", self.root), ("DATA_ROOT", self.root / "data")):
            patcher = patch("coat_sim.core.data_files." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = CoatService(root=self.root, restrict_to_root=False)
        self.source = self.make_source(self.service)
        self.lib = self.source.materialLibrary
        self.calc = self.source.referenceCalculator

    def use_builtin(self):
        builtin = next(row for row in self.lib.entries if row["builtin"])
        self.assertTrue(self.lib.selectMaterial(builtin["key"]))
        self.settle_materials()
        self.assertTrue(self.lib.useMaterial())
        self.assertTrue(self.calc.available)
        self.calc.begin()
        return builtin

    def set_gas(self, closure="ideal_effusion", area="10"):
        for key, value in (("model_id", "zns_ri8301_gas_v1"), ("temperature_K", "1200"), ("closure", closure), ("emitting_area_mm2", area)):
            self.calc.setField(key, value)

    def test_missing_builtin_resource_preserves_user_materials_drafts_and_legacy_sources(self):
        folder = self.root / "data/materials"
        user = default_material_profile("Existing custom", id="existing")
        save_material(folder, user)
        path = folder / "existing.json"
        before = path.read_bytes()
        self.lib.reload()
        self.settle_materials()
        self.lib.setField("name", "Keep this draft")
        draft = self.lib.fields
        with patch("coat_sim.core.material_business.MaterialLibrary.catalog_records", side_effect=FileNotFoundError("zns_reference.json unavailable")), \
                patch("coat_sim.ui.source_materials.materials.builtin_material_profiles", side_effect=FileNotFoundError("zns_reference.json unavailable")):
            self.lib.reload()
            self.settle_materials()
            self.assertEqual(self.lib.fields, draft)
            self.assertTrue(self.lib.dirty)
            # With the packaged reference unavailable, retain the independent
            # on-disk user catalog and the current draft; do not fabricate
            # builtin records from fallback data.
            self.assertEqual([entry["name"] for entry in self.lib.entries], ["Existing custom"])
            self.assertIn("文献参考数据不可用", self.lib.status)
            legacy = self.make_source(self.service)
            legacy.applyDocument({"kind": "point", "source_exponent": 2})
            self.assertTrue(legacy.valid, legacy.error)
            self.assertFalse(legacy.referenceCalculator.available)
            self.assertTrue(legacy.materialLibrary.selectMaterial("user:existing"))
            self.settle_materials()
            self.assertTrue(legacy.materialLibrary.useMaterial())
            self.assertTrue(legacy.valid, legacy.error)
            self.assertNotIn("reference_data", legacy.documentData()["material"])
            self.assertTrue(legacy.saveAsPath(str(self.root / "custom_source.csrc")), legacy.status)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.lib.fields, draft)
        self.lib.reload()
        self.settle_materials()
        self.assertTrue(any(entry["builtin"] for entry in self.lib.entries))
        self.assertNotIn("不可用", self.lib.status)

    def test_first_open_guides_to_builtin_but_respects_user_drafts_and_new(self):
        self.lib.prepareOpen()
        self.settle_materials()
        self.assertTrue(self.lib.builtin)
        self.assertTrue(self.lib.currentKey.startswith("builtin:"))
        self.assertFalse(self.lib.readOnly)
        self.assertTrue(self.lib.hasReferenceData)
        self.assertTrue(self.lib.newMaterial())
        identity = self.lib.fields["id"]
        self.assertFalse(self.lib.builtin)
        self.lib.prepareOpen()
        self.settle_materials()
        self.assertFalse(self.lib.readOnly)
        self.assertEqual(self.lib.fields["id"], identity)
        second = self.make_source(self.service).materialLibrary
        second.setField("name", "Preserve unsaved")
        second.prepareOpen()
        self.settle_materials()
        self.assertEqual(second.fields["name"], "Preserve unsaved")
        self.assertTrue(second.dirty)
        third = self.make_source(self.service).materialLibrary
        third.newMaterial()
        identity = third.fields["id"]
        third.prepareOpen()
        self.settle_materials()
        self.assertEqual(third.fields["id"], identity)
        self.assertFalse(third.hasReferenceData)

    def test_builtin_override_and_copy_are_portable_and_user_file_is_untouched(self):
        user = default_material_profile("Already mine", id="existing")
        folder = self.root / "data/materials"
        save_material(folder, user)
        before = (folder / "existing.json").read_bytes()
        self.lib.reload()
        self.settle_materials()
        self.assertEqual(len(self.lib.entries), len(builtin_material_profiles()) + 1)
        self.use_builtin()
        reference = deepcopy(self.lib.referenceData)
        self.assertGreater(len(self.lib.referenceSections), 15)
        text = json.dumps(self.lib.referenceSections, ensure_ascii=False)
        for label in ("IR01", "IR02", "1223.15", "1180", "quarantined", "未知"):
            self.assertIn(label, text)
        self.assertTrue(self.lib.builtin)
        self.assertFalse(self.lib.readOnly)
        self.assertFalse(self.lib.builtinOverridden)
        self.lib.setField("name", "Edited ZnS preset")
        self.assertTrue(self.lib.dirty)
        self.assertTrue(self.lib.save(), self.lib.status)
        self.settle_materials()
        self.assertTrue(self.lib.builtinOverridden)
        self.assertEqual(self.lib.fields["name"], "Edited ZnS preset")
        self.assertEqual(self.lib.referenceData, reference)
        self.assertFalse(self.lib.confirmDelete(True))
        self.settle_materials()
        self.assertTrue(self.lib.copyMaterial())
        self.settle_materials()
        self.assertFalse(self.lib.builtin)
        self.assertFalse(self.lib.readOnly)
        self.assertTrue(self.lib.dirty)
        self.lib.setField("name", "My ZnS copy")
        self.assertTrue(self.lib.save(), self.lib.status)
        self.settle_materials()
        self.assertEqual(self.lib.draftData()["reference_data"], reference)
        self.assertTrue(self.lib.useMaterial())
        self.assertEqual(self.source.documentData()["material"]["reference_data"], reference)
        self.assertEqual((folder / "existing.json").read_bytes(), before)
        self.assertNotEqual(self.lib.currentId, builtin_material_profiles()[0]["id"])

    def test_builtin_and_user_id_collision_are_distinct_no_overwrite(self):
        identity = builtin_material_profiles()[0]["id"]
        folder = self.root / "data/materials"
        save_material(folder, default_material_profile("Historical user record", id=identity))
        user_before = (folder / (identity + ".json")).read_bytes()
        self.lib.reload()
        self.settle_materials()
        self.assertEqual(len(self.lib.entries), len(builtin_material_profiles()) + 1)
        self.assertTrue(self.lib.selectMaterial("user:" + identity))
        self.settle_materials()
        self.assertEqual(self.lib.fields["name"], "Historical user record")
        self.assertFalse(self.lib.readOnly)
        self.assertTrue(self.lib.selectMaterial("builtin:" + identity))
        self.settle_materials()
        self.assertTrue(self.lib.builtin)
        self.assertFalse(self.lib.readOnly)
        builtin_reference = deepcopy(self.lib.referenceData)
        self.assertTrue(self.lib.copyMaterial())
        self.settle_materials()
        self.lib.setField("name", "Copied builtin")
        self.assertTrue(self.lib.save(), self.lib.status)
        self.settle_materials()
        copy_id = self.lib.currentId
        self.assertNotEqual(copy_id, identity)
        self.assertEqual(self.lib.referenceData, builtin_reference)
        self.assertEqual((folder / (identity + ".json")).read_bytes(), user_before)
        self.assertTrue(self.lib.selectMaterial("user:" + identity))
        self.settle_materials()
        self.assertEqual(self.lib.fields["name"], "Historical user record")
        self.assertTrue(self.lib.selectMaterial("builtin:" + identity))
        self.settle_materials()
        self.assertEqual(self.lib.fields["name"], builtin_material_profiles()[0]["name"])
        self.assertEqual(self.lib.referenceData, builtin_reference)
        self.lib.setField("name", "Blocked colliding override")
        self.assertFalse(self.lib.save())
        self.assertIn("custom material shadows a builtin identifier", self.lib.status)
        self.assertEqual((folder / (identity + ".json")).read_bytes(), user_before)
        saved = json.loads((folder / (copy_id + ".json")).read_text())
        self.assertEqual(saved["name"], "Copied builtin")
        self.assertEqual(saved["reference_data"], builtin_reference)

    def test_unknown_coefficients_remain_null_and_pressure_is_not_flux(self):
        self.use_builtin()
        self.set_gas("pressure_ratio", area="")
        self.calc.setField("pressure_ratio", "2")
        config = self.calc.draftData()
        self.assertEqual(config["alpha"], {"Zn": None, "S2": None})
        self.assertEqual(config["transmission"], {"Zn": None, "S2": None})
        self.assertIsNone(config["emitting_area_m2"])
        self.assertTrue(self.calc.calculate(), self.calc.error)
        self.assertEqual(self.calc.result["status"], "missing_inputs")
        self.assertIsNotNone(self.calc.result["pressure"])
        self.assertIsNone(self.calc.result["pre_transmission"])
        self.assertIn("alpha.Zn", self.calc.result["missing_inputs"])
        self.calc.setField("alpha_Zn", "0")
        self.assertEqual(self.calc.draftData()["alpha"]["Zn"], 0)
        self.assertIsNone(self.calc.draftData()["alpha"]["S2"])
        self.assertFalse(self.calc.hasResult)

    def test_vendor_pressure_domains_errors_and_no_HK(self):
        self.use_builtin()
        self.calc.setField("temperature_K", "1223.15")
        self.assertTrue(self.calc.calculate())
        self.assertAlmostEqual(self.calc.result["pressure"]["vendor_pressure_Pa"], 1)
        self.assertIsNone(self.calc.result["pressure"]["total_pressure_Pa"])
        self.assertIsNone(self.calc.result["post_transmission"])
        self.calc.setField("temperature_K", "1343.15")
        self.assertFalse(self.calc.hasResult)
        self.assertTrue(self.calc.calculate())
        self.assertAlmostEqual(self.calc.result["pressure"]["vendor_pressure_Pa"], 10)
        self.calc.setField("temperature_K", "1200")
        self.assertTrue(self.calc.calculate())
        self.assertEqual(self.calc.result["status"], "out_of_domain")
        self.assertIsNone(self.calc.result["pressure"])
        self.calc.setField("temperature_K", "bad")
        self.assertFalse(self.calc.calculate())
        self.assertFalse(self.calc.hasResult)
        self.assertTrue(self.calc.error)

    def test_ideal_explicit_roundtrip_cancel_and_snapshot_survive(self):
        self.use_builtin()
        self.set_gas()
        self.assertEqual(self.calc.fields["alpha_Zn"], "1")
        self.assertTrue(self.calc.calculate(), self.calc.error)
        self.assertEqual(self.calc.result["status"], "reference_prediction")
        self.assertAlmostEqual(self.calc.result["area"]["value_m2"], 1e-5)
        self.assertAlmostEqual(self.calc.result["post_transmission"]["s_to_zn_atomic_ratio"], 1)
        self.assertGreater(self.calc.result["post_transmission"]["mass_rate_kg_s"], 0)
        self.assertTrue(self.calc.apply(), self.calc.error)
        original = deepcopy(self.source.documentData())
        path = self.root / "source_with_reference.csrc"
        self.assertTrue(self.source.saveAsPath(str(path)), self.source.error)
        self.calc.begin()
        self.calc.setField("temperature_K", "1190")
        self.calc.cancel()
        self.assertEqual(self.source.documentData(), original)
        self.assertFalse(self.source.dirty)
        restored = self.make_source(self.service, str(path))
        self.assertEqual(restored.documentData(), original)
        self.assertTrue(restored.referenceCalculator.configured)
        self.assertTrue(restored.referenceCalculator.calculate())
        self.assertTrue(restored.editMaterialSnapshot())
        self.settle_materials()
        self.assertFalse(restored.materialLibrary.readOnly)
        self.assertNotEqual(restored.materialLibrary.fields["id"], original["material"]["id"])
        self.assertEqual(restored.materialLibrary.draftData()["reference_data"], original["material"]["reference_data"])

    def test_sealed_pressure_only_and_geometry_never_guesses_transmission(self):
        self.use_builtin()
        self.source.setField("device_type", "resistance_boat")
        self.source.setEmissionField("boat_mode", "covered")
        self.set_gas("sealed_stoichiometric_gas")
        self.assertTrue(self.calc.calculate())
        self.assertIsNone(self.calc.result["pre_transmission"])
        self.assertIsNone(self.calc.result["post_transmission"])
        self.calc.setField("closure", "pressure_ratio")
        self.calc.setField("pressure_ratio", "2")
        self.calc.setField("area_basis", "source_geometry")
        self.assertIsNone(self.calc.draftData()["emitting_area_m2"])
        self.assertTrue(self.calc.calculate(), self.calc.error)
        self.assertIsNone(self.calc.result["post_transmission"])
        self.assertIn("transmission.Zn", self.calc.result["missing_inputs"])
        for key in ("alpha_Zn", "alpha_S2", "transmission_Zn", "transmission_S2"):
            self.calc.setField(key, "0.5")
        self.assertTrue(self.calc.calculate(), self.calc.error)
        self.assertAlmostEqual(self.calc.result["area"]["value_m2"], 40 * 10 * 1e-6)
        self.source.setField("length_mm", "50")
        self.assertFalse(self.calc.hasResult)
        self.assertTrue(self.calc.calculate())
        self.assertAlmostEqual(self.calc.result["area"]["value_m2"], 50 * 10 * 1e-6)
        self.calc.setField("closure", "ideal_effusion")
        self.assertEqual(self.calc.fields["area_basis"], "explicit")
        self.calc.setField("closure", "pressure_ratio")
        self.assertEqual(self.calc.fields["alpha_Zn"], "")

    def test_mode_switch_omits_inactive_fields_and_failed_selection_keeps_readonly(self):
        self.use_builtin()
        before = self.lib.fields
        self.assertFalse(self.lib.selectMaterial("builtin:missing"))
        self.settle_materials()
        self.assertEqual(self.lib.fields, before)
        self.assertTrue(self.lib.builtin)
        self.assertEqual(self.lib.currentKey, "builtin:" + builtin_material_profiles()[0]["id"])
        self.assertFalse(self.lib.readOnly)
        self.set_gas("pressure_ratio")
        self.calc.setField("pressure_ratio", "2")
        self.calc.setField("closure", "sealed_stoichiometric_gas")
        self.assertIsNone(self.calc.draftData()["pressure_ratio"])
        self.assertTrue(self.calc.calculate(), self.calc.error)
        self.calc.setField("closure", "ideal_effusion")
        self.assertTrue(self.calc.calculate(), self.calc.error)
        self.calc.setField("model_id", "zns_vendor_pressure_v1")
        self.calc.setField("temperature_K", "1300")
        self.calc.setField("alpha_Zn", "invalid hidden value")
        self.assertIsNone(self.calc.draftData()["alpha"]["Zn"])
        self.assertTrue(self.calc.calculate(), self.calc.error)

    def test_custom_name_cannot_enable_and_removal_clears_scenario(self):
        self.assertFalse(self.calc.available)
        self.lib.setField("name", "ZnS")
        self.lib.useMaterial()
        self.assertFalse(self.calc.available)
        self.assertFalse(self.calc.calculate())
        self.assertFalse(self.lib.newMaterial())
        self.lib.confirmDiscard(True)
        self.settle_materials()
        self.use_builtin()
        self.set_gas()
        self.calc.apply()
        self.assertIn("thermochemistry", self.source.documentData())
        self.source.clearMaterial()
        self.assertFalse(self.calc.available)
        self.assertNotIn("thermochemistry", self.source.documentData())
        self.assertTrue(self.source.valid, self.source.error)

    def test_legacy_source_roundtrip_remains_without_opt_in(self):
        original = {"kind": "rectangle", "length_mm": 33, "width_mm": 7, "source_exponent": 2}
        self.source.applyDocument(original)
        before = deepcopy(self.source.documentData())
        self.assertNotIn("thermochemistry", before)
        self.assertFalse(self.calc.available)
        path = self.root / "old.csrc"
        self.assertTrue(self.source.saveAsPath(str(path)))
        self.assertEqual(self.make_source(self.service, str(path)).documentData(), before)

    def test_real_qml_calculate_cancel_apply_and_error_at_minimum_size(self):
        self.use_builtin()
        engine = QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda messages: warnings.extend(str(message.toString()) for message in messages))
        engine.rootContext().setContextProperty("bridge", self.source)
        ThemeService.instance().attach_engine(engine, "Theme")
        engine.load(QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/SourceWindow.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        try:
            window.showNormal(); window.resize(780, 540); APP.processEvents()
            self.assertTrue(QMetaObject.invokeMethod(
                window, "openSourceReferenceDialog"))
            dialog = window.findChild(QObject, "sourceReferenceDialog")
            deadline = time.monotonic() + 10.0
            while dialog is None and time.monotonic() < deadline:
                APP.processEvents()
                dialog = window.findChild(QObject, "sourceReferenceDialog")
                time.sleep(0.005)
            self.assertIsNotNone(dialog, "source reference dialog failed to load")
            self.settle_materials()
            visible_deadline = time.monotonic() + 10.0
            while not dialog.property("visible") and time.monotonic() < visible_deadline:
                APP.processEvents()
                time.sleep(0.005)
            self.assertTrue(dialog.property("visible"), "\n".join(warnings))
            self.assertLessEqual(dialog.property("width"), window.width())
            self.assertLessEqual(dialog.property("height"), window.height())
            temperature = window.findChild(QObject, "referenceTemperatureField")
            temperature.setProperty("text", "1223.15")
            self.assertTrue(QMetaObject.invokeMethod(temperature, "textEdited"))
            self.settle_materials()
            calculate = window.findChild(QObject, "calculateReferenceButton")
            self.assertTrue(QMetaObject.invokeMethod(calculate, "clicked")); APP.processEvents()
            self.settle_materials()
            self.assertAlmostEqual(self.calc.result["pressure"]["vendor_pressure_Pa"], 1)
            cancel = window.findChild(QObject, "cancelReferenceButton")
            self.assertTrue(QMetaObject.invokeMethod(cancel, "clicked")); APP.processEvents()
            self.settle_materials()
            self.assertFalse(dialog.property("visible"))
            self.assertNotIn("thermochemistry", self.source.documentData())
            self.assertTrue(QMetaObject.invokeMethod(
                window, "openSourceReferenceDialog")); APP.processEvents()
            self.settle_materials()
            visible_deadline = time.monotonic() + 10.0
            while not dialog.property("visible") and time.monotonic() < visible_deadline:
                APP.processEvents()
                time.sleep(0.005)
            self.assertTrue(dialog.property("visible"), "\n".join(warnings))
            temperature.setProperty("text", "invalid")
            QMetaObject.invokeMethod(temperature, "textEdited")
            self.settle_materials()
            QMetaObject.invokeMethod(calculate, "clicked"); APP.processEvents()
            self.settle_materials()
            self.assertFalse(self.calc.hasResult)
            self.assertTrue(self.calc.error)
            apply = window.findChild(QObject, "applyReferenceButton")
            QMetaObject.invokeMethod(apply, "clicked"); APP.processEvents()
            self.settle_materials()
            self.assertTrue(dialog.property("visible"))
            temperature.setProperty("text", "1300")
            QMetaObject.invokeMethod(temperature, "textEdited")
            self.settle_materials()
            QMetaObject.invokeMethod(apply, "clicked"); APP.processEvents()
            self.settle_materials()
            self.assertFalse(dialog.property("visible"))
            self.assertEqual(self.source.documentData()["thermochemistry"]["temperature_K"], 1300)
            self.assertFalse(warnings, "\n".join(warnings))
        finally:
            window.setProperty("allowClose", True); window.close()
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
