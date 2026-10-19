"""Contracts for the project-defined, ideal Air optical placeholder."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from coat_sim.core.film_materials import FilmMaterialLibrary
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import (
    builtin_material_profiles,
    material_completeness,
    material_emission_model,
    source_bindable,
    validate_material_profile,
)
from coat_sim.core.optical_constants import evaluate_optical_set, prepare_optical_set


ROOT = Path(__file__).resolve().parents[1]
AIR_ID = "builtin_ideal_air"
AIR_SET_ID = "ideal_air_n1_k0"


def builtin_air():
    return next(row for row in builtin_material_profiles() if row["id"] == AIR_ID)


class IdealAirOpticalContractTests(unittest.TestCase):
    def test_catalog_declares_project_model_and_software_domain(self):
        catalog = json.loads((ROOT / "data" / "optical_catalog.json").read_text(encoding="utf-8"))
        row = next(item for item in catalog["materials"]
                   if item.get("builtin_material_id") == AIR_ID)
        optical = next(item for item in row["optical_constants"] if item["id"] == AIR_SET_ID)
        air = builtin_air()

        self.assertEqual(row["category"], "film")
        self.assertIn("project_ideal_air_20261012_v1", catalog["datasets"])
        self.assertIn("project-defined mathematical engineering model", catalog["provenance_contract"])
        self.assertEqual(air["roles"], ["film", "medium"])
        self.assertTrue(air["optical_only"])
        self.assertNotIn("research_data", air)
        self.assertIsNone(air["resistance_model"])
        self.assertIsNone(air["electron_beam_model"])
        self.assertEqual(optical["kind"], "tabulated")
        self.assertEqual(optical["wavelength_unit"], "um")
        self.assertEqual(optical["wavelength_min"], 0.001)
        self.assertEqual(optical["wavelength_max"], 1000.0)
        self.assertEqual(optical["wavelengths"], [0.001, 1000.0])
        self.assertEqual(optical["n"], [1.0, 1.0])
        self.assertEqual(optical["k"], [0.0, 0.0])
        self.assertIn("not RII data or measured air", catalog["provenance_contract"])
        self.assertIn("未找到外部依据，仅为本项目实现", optical["source"])
        self.assertIn("不是实测空气", optical["source"])

    def test_constant_model_evaluates_inside_domain_and_rejects_outside(self):
        optical = next(row for row in builtin_air()["optical_constants"]
                       if row["id"] == AIR_SET_ID)
        evaluate = prepare_optical_set(optical)
        for wavelength, unit in ((0.001, "um"), (0.55, "um"), (550, "nm"),
                                 (1000.0, "um")):
            with self.subTest(wavelength=wavelength, unit=unit):
                self.assertEqual(evaluate(wavelength, unit)[:2], (1.0, 0.0))
                self.assertEqual(evaluate_optical_set(optical, wavelength, unit), (1.0, 0.0))

        for wavelength, unit in ((0.000999, "um"), (1000.001, "um"), (0.999, "nm"),
                                 (1_000_001_000, "nm")):
            with self.subTest(outside=(wavelength, unit)), self.assertRaises(ValueError):
                evaluate_optical_set(optical, wavelength, unit)

    def test_library_uses_material_owned_dataset_and_air_is_not_a_source(self):
        air = builtin_air()
        self.assertFalse(source_bindable(air))
        self.assertFalse(material_completeness(air)["research_available"])
        with self.assertRaisesRegex(ValueError, "仅光学资料材料"):
            material_emission_model(air, "resistance_boat")

        with tempfile.TemporaryDirectory() as directory:
            library = FilmMaterialLibrary(directory)
            self.assertEqual(library.evaluate(AIR_ID, AIR_SET_ID, 550), complex(1.0, 0.0))
            with self.assertRaisesRegex(ValueError, "找不到光学常数组"):
                library.evaluate("builtin_research_Ta2O5_film", AIR_SET_ID, 550)

    def test_copy_override_and_restore_use_the_normal_material_library_path(self):
        original = builtin_air()
        with tempfile.TemporaryDirectory() as directory:
            library = MaterialLibrary(directory=Path(directory) / "materials")

            copied = library.copy(AIR_ID, "my_ideal_air_copy", "My ideal Air")
            copied_record = copied["record"]
            self.assertEqual(copied_record["id"], "my_ideal_air_copy")
            self.assertEqual(copied_record["optical_constants"], original["optical_constants"])
            self.assertEqual(copied_record["roles"], ["film", "medium"])
            self.assertTrue(copied_record["optical_only"])

            override = deepcopy(original)
            override["description"] = "User override note"
            result = library.save_builtin_override(override)
            self.assertTrue(result["overridden"])
            self.assertEqual(library.read(AIR_ID)["record"]["description"], "User override note")
            self.assertEqual(library.read(AIR_ID)["record"]["optical_constants"],
                             original["optical_constants"])

            restored = library.restore_builtin(AIR_ID)
            self.assertEqual(restored, original)
            self.assertEqual(library.read(AIR_ID)["record"], original)
            self.assertEqual(library.read("my_ideal_air_copy")["record"], copied_record)
            self.assertFalse((Path(directory) / "materials" / ".overrides" / f"{AIR_ID}.json").exists())
            self.assertEqual(validate_material_profile(original), original)


if __name__ == "__main__":
    unittest.main()
