# Reconstructed regression suite from historical f12 evidence and current contracts.
# This is not a byte-for-byte recovery of the missing target test.
"""Controller/session bridge contracts for the fixed reference recipe library."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_quick_controller import FilmDesignQuickController


ROOT = Path(__file__).parents[1]
ARTIFACTS = ROOT / "artifacts" / "film_design_reference_library_validation"


class FilmDesignReferenceBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=ARTIFACTS)
        cls.temp_path = Path(cls.temp.name)
        source = FilmDesignService(ROOT, restrict_to_root=False)
        cls.service = FilmDesignService(cls.temp_path, restrict_to_root=False)
        cls.service.materials = source.materials

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.controller = FilmDesignQuickController(
            self.temp_path, service=self.service)
        self.assertTrue(self.controller.initializationReady)

    def tearDown(self):
        self.controller.deleteLater()

    @staticmethod
    def binding(material_id, optical_set_id):
        return {"material_id": material_id, "optical_set_id": optical_set_id}

    def simple_reference_inputs(self):
        return {
            "recipe_id": "comsol_ideal_single_ar",
            "role_bindings": {
                "F": self.binding("builtin_zns_literature_20261004", "ri_zns_amotchkina_n"),
            },
            "incident_binding": self.binding("builtin_ideal_air", "ideal_air_n1_k0"),
            "substrate_binding": self.binding(
                "film_substrate_malitson_silica", "malitson_silica_sellmeier"),
            "mapping_confirmed": True,
            "assume_lossless": True,
        }

    def test_reference_token_is_invalidated_by_committed_revision_and_live_edit_epoch(self):
        first = self.controller.previewReferenceDesign(self.simple_reference_inputs())
        self.assertTrue(first.get("ok"), first)
        stale_revision_token = first["preview_token"]
        before_revision = self.controller.session.snapshot().design_revision
        self.assertTrue(self.controller.setDesignValue("note", "revision changed after preview"))
        self.assertGreater(self.controller.session.snapshot().design_revision, before_revision)
        self.assertFalse(self.controller.applyGeneratedDesign(stale_revision_token))
        self.assertIsNone(self.controller._generator_preview)

        second = self.controller.previewReferenceDesign(self.simple_reference_inputs())
        self.assertTrue(second.get("ok"), second)
        stale_edit_token = second["preview_token"]
        before_epoch = self.controller.pendingEditEpoch
        self.controller.markEditingDirty()
        self.assertEqual(self.controller.pendingEditEpoch, before_epoch + 1)
        self.assertFalse(self.controller.applyGeneratedDesign(stale_edit_token))
        self.assertIsNone(self.controller._generator_preview)

    def test_real_qoq_mapping_is_private_until_apply_then_undo_restores_owner(self):
        before = self.controller.session.snapshot()
        result = self.controller.previewReferenceDesign({
            "recipe_id": "comsol_qq_mgf2_cef3",
            "role_bindings": {
                "F1": self.binding("builtin_zns_literature_20261004", "ri_zns_amotchkina_n"),
                "F2": self.binding("builtin_research_YbF3_film", "ri_ybf3_amotchkina_n"),
            },
            "incident_binding": self.binding("builtin_ideal_air", "ideal_air_n1_k0"),
            "substrate_binding": self.binding("film_substrate_malitson_silica", "malitson_silica_sellmeier"),
            "mapping_confirmed": True,
            "assume_lossless": True,
        })
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["layer_count"], 2)
        self.assertNotIn("candidate_design", result)
        self.assertTrue(result["preview_token"])
        self.assertEqual(result["lambda0_nm"], 550.0)
        self.assertEqual(self.controller.session.snapshot().design, before.design)
        self.assertEqual(self.controller.session.snapshot().design_revision, before.design_revision)
        self.assertFalse(self.controller.dirty)
        self.assertFalse(any(key in result for key in ("reflectance", "transmission", "reference_results")))

        self.assertTrue(self.controller.applyGeneratedDesign(result["preview_token"]))
        applied = self.controller.session.snapshot()
        self.assertEqual(applied.design_revision, before.design_revision + 1)
        self.assertTrue(applied.dirty)
        self.assertEqual(len(applied.design["front_layers"]), 2)
        self.assertEqual(applied.design["front_layers"][0]["material_id"], "builtin_research_YbF3_film")
        self.assertTrue(self.controller.canUndoGeneratedDesign)
        self.assertTrue(self.controller.undoGeneratedDesign())
        restored = self.controller.session.snapshot()
        self.assertEqual(restored.design, before.design)
        self.assertEqual(restored.design_revision, before.design_revision + 2)
        self.assertFalse(self.controller.canUndoGeneratedDesign)

    def test_real_absorption_is_preserved_and_fixed_tfcalc_lambda_is_per_recipe(self):
        absorbing = self.controller.previewReferenceDesign({
            "recipe_id": "comsol_ideal_single_ar",
            "role_bindings": {
                "F": self.binding("builtin_research_Cu_film", "ri_cu_mcpeak"),
            },
            "incident_binding": self.binding("builtin_ideal_air", "ideal_air_n1_k0"),
            "substrate_binding": self.binding("film_substrate_malitson_silica", "malitson_silica_sellmeier"),
            "mapping_confirmed": True,
            "assume_lossless": True,
        })
        self.assertTrue(absorbing.get("ok"), absorbing)
        self.assertIn("\u534a\u65e0\u9650\u5438\u6536\u57fa\u7247", absorbing["limitations"])
        mapped = absorbing["mapped_layers_incident_order"][0]
        self.assertGreater(mapped["k_at_wavelength"], 1e-9)
        self.assertAlmostEqual(mapped["delta_k"], mapped["k_at_wavelength"])
        self.assertTrue(any("保留该 k" in assumption for assumption in absorbing["assumptions"]))
        self.assertNotIn("candidate_design", absorbing)
        self.assertTrue(self.controller.applyGeneratedDesign(absorbing["preview_token"]))
        design = self.controller.currentDesign()
        self.assertTrue(design["use_k"])
        self.assertEqual(design["front_layers"][0]["material_id"], "builtin_research_Cu_film")
        self.assertTrue(self.controller.undoGeneratedDesign())

        fixed = self.controller.previewReferenceDesign({
            "recipe_id": "tfcalc_growing_ar_13",
            "wavelength_nm": 10001,
            "role_bindings": {},
            "incident_binding": {},
            "substrate_binding": {},
            "mapping_confirmed": True,
        })
        self.assertFalse(fixed.get("ok"))
        self.assertIn("10000 nm", fixed.get("error", ""))
        self.assertIsNone(self.controller._generator_preview)

    def test_closed_source_rejects_preview_without_returning_token(self):
        self.controller.markSourceClosed()
        result = self.controller.previewReferenceDesign({
            "recipe_id": "comsol_ideal_single_ar",
        })
        self.assertFalse(result.get("ok"))
        self.assertNotIn("preview_token", result)
        self.assertIsNone(self.controller._generator_preview)

    def test_cancel_or_invalid_repreview_cannot_apply_an_earlier_mapping(self):
        original = self.controller.session.snapshot()
        inputs = self.simple_reference_inputs()
        inputs_before = deepcopy(inputs)
        first = self.controller.previewReferenceDesign(inputs)
        self.assertTrue(first.get("ok"), first)
        self.assertEqual(inputs, inputs_before)
        self.controller.clearGeneratorPreview()
        self.assertFalse(self.controller.applyGeneratedDesign(first["preview_token"]))
        self.assertEqual(self.controller.session.snapshot().design, original.design)

        second = self.controller.previewReferenceDesign(inputs)
        self.assertTrue(second.get("ok"), second)
        self.assertNotEqual(second["preview_token"], first["preview_token"])
        invalid = self.controller.previewReferenceDesign({
            **inputs, "mapping_confirmed": False,
        })
        self.assertFalse(invalid.get("ok"))
        self.assertNotIn("preview_token", invalid)
        self.assertFalse(self.controller.applyGeneratedDesign(second["preview_token"]))
        snapshot = self.controller.session.snapshot()
        self.assertEqual(snapshot.design, original.design)
        self.assertEqual(snapshot.design_revision, original.design_revision)
        self.assertFalse(snapshot.dirty)


if __name__ == "__main__":
    unittest.main()

