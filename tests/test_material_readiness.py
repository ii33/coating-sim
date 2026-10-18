"""Read-only material diagnostics must not manufacture physical readiness."""
from copy import deepcopy
import json
from typing import Any, ClassVar
import unittest

from coat_sim.core.film_materials import _builtin_substrates
from coat_sim.core.material_readiness import material_readiness
from coat_sim.core.materials import builtin_material_profiles, default_material_profile


def optical(*, identifier="fixture", k=None, unit="nm", low=400.0, high=800.0):
    return {"id": identifier, "name": identifier, "kind": "tabulated",
            "wavelength_unit": unit, "wavelength_min": low, "wavelength_max": high,
            "wavelengths": [low, high], "n": [1.5, 1.6], "k": k,
            "source": "https://example.test/measurement", "notes": "Synthetic test data."}


def material(*datasets):
    row = default_material_profile("Fixture", id="fixture_material")
    row["optical_constants"] = list(datasets)
    return row


def report(row, **kwargs):
    params = {"wavelength_min_nm": 400.0, "wavelength_max_nm": 700.0}
    params.update(kwargs)
    return material_readiness(row, **params)


class MaterialReadinessTests(unittest.TestCase):
    builtins: ClassVar[dict[str, dict[str, Any]]]

    @classmethod
    def setUpClass(cls):
        cls.builtins = {row["id"]: row for row in builtin_material_profiles()}

    def test_no_implicit_selection_even_for_single_complete_dataset(self):
        result = report(material(optical(k=[0.0, 0.01])))
        self.assertEqual(result["optical_status"], "selection_required")
        self.assertFalse(result["can_evaluate_under_stated_assumptions"])
        self.assertEqual(result["datasets"][0]["status"], "data_complete")

    def test_complete_input_is_not_production_certification(self):
        result = report(material(optical(k=[0.0, 0.01])), optical_set_id="fixture")
        self.assertEqual(result["optical_status"], "data_complete")
        self.assertTrue(result["can_evaluate_under_stated_assumptions"])
        self.assertEqual(result["production_readiness"], "not_established")
        self.assertFalse(result["numerical_solver_run"])

    def test_missing_k_requires_explicit_assumption_and_stays_missing(self):
        row = material(optical())
        result = report(row, optical_set_id="fixture")
        self.assertEqual(result["optical_status"], "requires_lossless_assumption")
        self.assertFalse(result["can_evaluate_under_stated_assumptions"])
        assumed = report(row, optical_set_id="fixture", assume_lossless=True)
        self.assertEqual(assumed["optical_status"], "assumption_dependent")
        self.assertTrue(assumed["can_evaluate_under_stated_assumptions"])
        self.assertEqual(assumed["missing"][0]["code"], "k_unknown")
        self.assertIsNone(assumed["datasets"][0]["k_range_nm"])
        self.assertIsNone(row["optical_constants"][0]["k"])

    def test_assumption_does_not_override_known_k(self):
        row = material(optical(k=[0.2, 0.4]))
        before = deepcopy(row)
        result = report(row, optical_set_id="fixture", assume_lossless=True)
        self.assertEqual(result["assumptions"], [])
        self.assertEqual(row, before)

    def test_unknown_k_assumption_never_fills_missing_n_band(self):
        result = report(material(optical()), wavelength_min_nm=380.0,
                        optical_set_id="fixture", assume_lossless=True)
        self.assertEqual(result["optical_status"], "missing_n_coverage")
        self.assertFalse(result["can_evaluate_under_stated_assumptions"])
        self.assertEqual(result["datasets"][0]["n_range_nm"], [400.0, 800.0])

    def test_declared_domain_not_raw_table_extent(self):
        data = optical(k=[0.0, 0.0])
        data["wavelengths"] = [300.0, 1000.0]
        result = report(material(data), wavelength_min_nm=380.0, optical_set_id="fixture")
        self.assertEqual(result["optical_status"], "missing_n_coverage")

    def test_independent_k_domain_and_units(self):
        data = {"id": "model", "name": "Model", "kind": "cauchy",
                "wavelength_unit": "um", "wavelength_min": 0.3, "wavelength_max": 1.0,
                "parameters": {"A": 1.5, "B": 0.0, "C": 0.0},
                "source": "n reference", "notes": "",
                "k_data": {"wavelength_unit": "nm", "wavelength_min": 450.0,
                           "wavelength_max": 650.0, "wavelengths": [400.0, 700.0],
                           "k": [0.01, 0.02], "source": "k reference", "notes": ""}}
        result = report(material(data), optical_set_id="model")
        row = result["datasets"][0]
        self.assertEqual(row["n_range_nm"], [300.0, 1000.0])
        self.assertEqual(row["k_range_nm"], [450.0, 650.0])
        self.assertEqual(row["joint_nk_range_nm"], [450.0, 650.0])
        self.assertEqual(row["k_coverage"], "partial")
        self.assertEqual(row["k_source"]["text"], "k reference")
        self.assertEqual(result["optical_status"], "requires_lossless_assumption")

    def test_separate_datasets_not_stitched(self):
        result = report(material(optical(identifier="blue", low=380.0, high=500.0, k=[0, 0]),
                                 optical(identifier="red", low=500.0, high=780.0, k=[0, 0])),
                        wavelength_min_nm=380.0, wavelength_max_nm=780.0)
        self.assertTrue(all(row["status"] == "missing_n_coverage" for row in result["datasets"]))
        self.assertFalse(result["can_evaluate_under_stated_assumptions"])

    def test_nm_um_equivalence(self):
        nm = report(material(optical(k=[0, 0])), optical_set_id="fixture")
        um = report(material(optical(k=[0, 0], unit="um", low=0.4, high=0.8)),
                    optical_set_id="fixture")
        self.assertEqual(nm["datasets"], um["datasets"])

    def test_empty_optics_is_visible_even_with_lossless_assumption(self):
        result = report(material(), assume_lossless=True)
        self.assertEqual(result["optical_status"], "no_optical_data")
        self.assertEqual(result["datasets"], [])
        self.assertFalse(result["can_evaluate_under_stated_assumptions"])

    def test_source_reference_missing_does_not_hide_data_coverage(self):
        data = optical(k=[0, 0])
        data["source"] = ""
        result = report(material(data), optical_set_id="fixture")
        self.assertEqual(result["optical_status"], "data_complete")
        self.assertIn("optical_source_missing", [row["code"] for row in result["missing"]])

    def test_optical_aliases_supported_but_conflict_rejected(self):
        row = material(optical())
        row["optical_sets"] = row.pop("optical_constants")
        self.assertEqual(len(report(row)["datasets"]), 1)
        row["optical_constants"] = deepcopy(row["optical_sets"])
        self.assertEqual(len(report(row)["datasets"]), 1)
        row["optical_constants"][0]["n"][0] = 2
        with self.assertRaisesRegex(ValueError, "disagree"):
            report(row)

    def test_calibrated_is_declared_not_verified(self):
        row = material(optical(k=[0, 0]))
        row["resistance_model"]["provenance"] = {
            "status": "calibrated", "reference": "Specific witness experiment", "notes": ""}
        result = report(row)
        model = result["process"]["models"][0]
        self.assertTrue(model["calibration_declared"])
        self.assertFalse(model["calibration_verified"])
        self.assertEqual(model["source"]["text"], "Specific witness experiment")
        self.assertFalse(result["process"]["absolute_deposition_rate_prediction_available"])

    def test_calibrated_without_reference_rejected(self):
        row = material()
        row["resistance_model"]["provenance"] = {"status": "calibrated", "reference": ""}
        with self.assertRaises(ValueError):
            report(row)

    def test_builtin_zns_default_appearance_gap(self):
        row = self.builtins["builtin_zns_literature_20261004"]
        result = report(row, wavelength_min_nm=380.0, wavelength_max_nm=780.0,
                        optical_set_id="ri_zns_amotchkina_nk")
        self.assertEqual(result["optical_status"], "missing_n_coverage")
        self.assertEqual(result["datasets"][1]["n_range_nm"], [400.0, 1000.0])
        self.assertFalse(any(item["calibration_declared"] for item in result["process"]["models"]))

    def test_builtin_h4_k9_are_not_replaced_by_other_materials(self):
        for identity in ("H4_film", "K9_substrate"):
            with self.subTest(identity=identity):
                result = report(self.builtins["builtin_research_" + identity])
                self.assertEqual(result["optical_status"], "no_optical_data")
                self.assertGreater(result["research"]["property_count"], 0)

    def test_ito_sample_is_sourced_but_not_production_calibrated(self):
        row = self.builtins["builtin_research_ITO_film"]
        result = report(row, wavelength_min_nm=380.0, wavelength_max_nm=780.0,
                        optical_set_id="ri_ito_minenkov_glass_2024")
        self.assertEqual(result["optical_status"], "data_complete")
        self.assertIn("联合光学模型拟合", result["datasets"][0]["notes"]["text"])
        self.assertEqual(result["production_readiness"], "not_established")
        self.assertFalse(any(item["calibration_declared"] for item in result["process"]["models"]))

    def test_extra_builtin_glass_uses_n_only_and_has_no_process_defaults(self):
        row = _builtin_substrates()[0]
        result = report(row, optical_set_id=row["optical_sets"][0]["id"])
        self.assertEqual(result["optical_status"], "requires_lossless_assumption")
        self.assertFalse(result["process"]["source_bindable"])
        self.assertTrue(all(item["status"] == "unknown" for item in result["process"]["models"]))

    def test_ideal_air_is_optical_only_not_process_ready(self):
        result = report(self.builtins["builtin_ideal_air"], optical_set_id="ideal_air_n1_k0")
        self.assertEqual(result["optical_status"], "data_complete")
        self.assertFalse(result["process"]["source_bindable"])
        self.assertIn("理想", result["datasets"][0]["notes"]["text"])

    def test_truncated_source_is_explicit_and_output_detached(self):
        data = optical(k=[0, 0])
        data["source"] = "x" * 3000
        row = material(data)
        before = deepcopy(row)
        result = report(row, optical_set_id="fixture")
        self.assertTrue(result["datasets"][0]["source"]["truncated"])
        self.assertEqual(len(result["datasets"][0]["source"]["text"]), 2000)
        result["datasets"][0]["n_range_nm"][0] = 1
        self.assertEqual(row, before)
        json.dumps(result, allow_nan=False)

    def test_invalid_request_and_dataset_rejected(self):
        cases = [{"wavelength_min_nm": value} for value in (True, 0, -1, float("nan"), "400")]
        cases += [{"wavelength_max_nm": float("inf")}, {"wavelength_max_nm": 300},
                  {"assume_lossless": 1}, {"optical_set_id": "foreign"}, {"optical_set_id": ""}]
        for params in cases:
            with self.subTest(params=params), self.assertRaises(ValueError):
                report(material(optical()), **params)
        data = optical()
        data["n"][0] = -1
        with self.assertRaises(ValueError):
            report(material(data))


if __name__ == "__main__":
    unittest.main()
