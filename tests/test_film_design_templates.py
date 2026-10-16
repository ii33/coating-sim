"""Focused, dependency-free contracts for the classic thin-film templates."""

from copy import deepcopy
import math
import unittest

from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_templates import (
    K_TRANSPARENCY_THRESHOLD,
    build_preview,
    template_catalog,
    template_roles,
)


def _optical_set(identifier, n, k=0.0, *, band=(450.0, 650.0)):
    return {
        "id": identifier,
        "name": identifier,
        "kind": "tabulated",
        "source": "synthetic unit-test values",
        "wavelength_unit": "nm",
        "wavelength_min": band[0],
        "wavelength_max": band[1],
        "wavelengths": [band[0], 550.0, band[1]],
        "n": [n, n, n],
        "k": None if k is None else [k, k, k],
    }


def _material(identifier, purpose, n, k=0.0, *, band=(450.0, 650.0)):
    return {
        "id": identifier,
        "name": identifier,
        "optical_sets": [_optical_set(identifier + "_set", n, k, band=band)],
    }


class FilmDesignTemplatesTests(unittest.TestCase):
    def setUp(self):
        self.options = {
            "film": [
                _material("film_f", "film", 1.38),
                _material("film_f1", "film", 1.38),
                _material("film_f2", "film", 2.2),
                _material("film_f3", "film", 1.63),
                _material("film_h", "film", 2.1),
                _material("film_l", "film", 1.45),
                _material("film_absorb", "film", 2.1, k=0.01),
                _material("film_unknown_k", "film", 1.6, k=None),
            ],
            "medium": [_material("air", "medium", 1.0),
                       _material("glass", "medium", 1.5, None)],
            "substrate": [_material("glass", "substrate", 1.5, k=None)],
        }
        self.bindings = {
            "F": {"material_id": "film_f", "optical_set_id": "film_f_set"},
            "F1": {"material_id": "film_f1", "optical_set_id": "film_f1_set"},
            "F2": {"material_id": "film_f2", "optical_set_id": "film_f2_set"},
            "F3": {"material_id": "film_f3", "optical_set_id": "film_f3_set"},
            "H": {"material_id": "film_h", "optical_set_id": "film_h_set"},
            "L": {"material_id": "film_l", "optical_set_id": "film_l_set"},
        }
        self.endpoints = {
            "incident_binding": {"material_id": "air", "optical_set_id": "air_set"},
            "substrate_binding": {"material_id": "glass", "optical_set_id": "glass_set"},
        }

    def inputs(self, template_id, **changes):
        result = {
            "template_id": template_id,
            "wavelength_nm": 550.0,
            "pairs": 8,
            "role_bindings": deepcopy(self.bindings),
            "assume_lossless": True,
            **deepcopy(self.endpoints),
        }
        result.update(changes)
        return result

    def build(self, template_id, **changes):
        return build_preview(inputs=self.inputs(template_id, **changes),
                             base_design=new_design(), material_options=self.options)

    def test_catalog_has_stable_source_and_scope_metadata(self):
        rows = template_catalog()
        self.assertEqual(len(rows), 5)
        self.assertEqual(len({row["stable_id"] for row in rows}), 5)
        for row in rows:
            for key in ("family", "source_url", "reference_section", "original_material_band",
                        "formula", "license_note", "verification_status"):
                self.assertTrue(row.get(key), (row["stable_id"], key))
        self.assertEqual([row["role"] for row in template_roles("ar_qhq_seed")],
                         ["F1", "F2", "F3"])
        self.assertIn("mit.edu", next(row for row in rows if row["stable_id"] == "hr_hl_qw")["source_url"])

    def test_ar_center_formula_and_one_time_physical_thickness(self):
        preview = self.build("ar_single_qw")
        expected = ((1.0 * 1.5 - 1.38 ** 2) / (1.0 * 1.5 + 1.38 ** 2)) ** 2
        self.assertAlmostEqual(preview["reference_results"]["center_reflectance"], expected, places=14)
        self.assertEqual(preview["layer_count"], 1)
        layer = preview["layers_incident_order"][0]
        self.assertAlmostEqual(layer["physical_thickness_nm"], 550 / (4 * 1.38))
        self.assertEqual(preview["candidate_design"]["front_layers"][0]["thickness"]["unit"], "nm")
        self.assertEqual(preview["candidate_design"]["substrate"]["sides"], "single")
        self.assertEqual(preview["candidate_design"]["back_layers"], [])

    def test_vcoat_and_qhq_reference_relations(self):
        vcoat_bindings = {
            "F1": {"material_id": "film_f1", "optical_set_id": "film_f1_set"},
            "F2": {"material_id": "film_f2", "optical_set_id": "film_f2_set"},
        }
        preview = self.build("ar_vcoat_qq", role_bindings=vcoat_bindings)
        self.assertAlmostEqual(preview["reference_results"]["ideal_match_ratio_n2_over_n1"], math.sqrt(1.5))
        qhq = self.build("ar_qhq_seed")
        self.assertTrue(qhq["reference_results"]["middle_half_wave_matrix_is_minus_identity"])
        self.assertEqual([row["qw_multiplier"] for row in qhq["layers_incident_order"]], [1, 2, 1])
        self.assertAlmostEqual(qhq["reference_results"]["center_reflectance"], 0.00131190193, places=9)

    def test_hr_fp_layout_and_center_values(self):
        hr = self.build("hr_hl_qw", pairs=8)
        self.assertEqual(hr["layer_count"], 16)
        self.assertEqual([layer["role"] for layer in hr["layers_incident_order"][:4]], ["H", "L", "H", "L"])
        self.assertAlmostEqual(hr["reference_results"]["center_reflectance"], 0.99290742712, places=10)
        fp = self.build("fp_halfwave_defect", pairs=4)
        self.assertEqual(fp["layer_count"], 17)
        self.assertEqual(fp["layers_incident_order"][8]["qw_multiplier"], 2)
        self.assertAlmostEqual(fp["reference_results"]["center_transmittance"], 0.96, places=14)
        equal_port = self.build("fp_halfwave_defect", pairs=4,
                                incident_binding={"material_id": "glass", "optical_set_id": "glass_set"},
                                substrate_binding={"material_id": "glass", "optical_set_id": "glass_set"})
        self.assertAlmostEqual(equal_port["reference_results"]["center_transmittance"], 1.0, places=14)

    def test_known_absorption_cannot_be_bypassed_but_unknown_k_needs_consent(self):
        bindings = deepcopy(self.bindings)
        bindings["H"] = {"material_id": "film_absorb", "optical_set_id": "film_absorb_set"}
        with self.assertRaisesRegex(ValueError, "阈值"):
            self.build("hr_hl_qw", role_bindings=bindings, assume_lossless=True)
        bindings["H"] = {"material_id": "film_unknown_k", "optical_set_id": "film_unknown_k_set"}
        with self.assertRaisesRegex(ValueError, "未知"):
            self.build("ar_single_qw", role_bindings={"F": bindings["H"]}, assume_lossless=False)
        preview = self.build("ar_single_qw", role_bindings={"F": bindings["H"]}, assume_lossless=True)
        self.assertTrue(any("F" in text and "未提供 k" in text for text in preview["assumptions"]))
        self.assertTrue(preview["candidate_design"]["assume_lossless"])

    def test_no_dataset_out_of_band_bad_n_noninteger_pairs_and_order_errors(self):
        with self.assertRaisesRegex(ValueError, "选择材料"):
            self.build("ar_single_qw", role_bindings={})
        with self.assertRaisesRegex(ValueError, "无法在 700 nm"):
            self.build("ar_single_qw", wavelength_nm=700.0)
        with self.assertRaisesRegex(ValueError, "整数"):
            self.build("hr_hl_qw", pairs=2.5)
        inverted = deepcopy(self.bindings)
        inverted["H"], inverted["L"] = inverted["L"], inverted["H"]
        with self.assertRaisesRegex(ValueError, "nH > nL"):
            self.build("hr_hl_qw", role_bindings=inverted)
        with self.assertRaisesRegex(ValueError, "有限正数"):
            self.build("ar_single_qw", wavelength_nm=float("nan"))


    def test_incident_medium_accepts_explicit_records_from_quick_picker_catalogs(self):
        preview = self.build(
            "ar_single_qw",
            incident_binding={"material_id": "film_f", "optical_set_id": "film_f_set"})
        expected = ((1.38 * 1.5 - 1.38 ** 2) / (1.38 * 1.5 + 1.38 ** 2)) ** 2
        self.assertAlmostEqual(preview["reference_results"]["center_reflectance"], expected)
        with self.assertRaisesRegex(ValueError, "材料不在当前材料库"):
            self.build("ar_single_qw", incident_binding={
                "material_id": "not-a-library-record", "optical_set_id": "film_f_set"})

    def test_tiny_known_incident_k_is_rejected_without_zeroing_passive_layer_k(self):
        for k in (1e-15, 1e-14, K_TRANSPARENCY_THRESHOLD):
            self.options['medium'][0]['optical_sets'][0]['k'] = [k, k, k]
            before = deepcopy(self.options)
            for template_id in ('ar_single_qw', 'ar_vcoat_qq', 'ar_qhq_seed',
                                'hr_hl_qw', 'fp_halfwave_defect'):
                with self.subTest(k=k, template=template_id):
                    with self.assertRaisesRegex(ValueError, 'k=0 的入射端口'):
                        self.build(template_id)
                    self.assertEqual(self.options, before)
        self.options['medium'][0]['optical_sets'][0]['k'] = [0.0, 0.0, 0.0]
        self.options['film'][0]['optical_sets'][0]['k'] = [1e-14, 1e-14, 1e-14]
        preview = self.build('ar_single_qw')
        self.assertEqual(preview['layers_incident_order'][0]['k_at_wavelength'], 1e-14)
        self.assertTrue(preview['candidate_design']['use_k'])
        self.options['medium'][0]['optical_sets'][0]['k'] = None
        self.options['substrate'][0]['optical_sets'][0]['k'] = [0.0, 0.0, 0.0]
        with self.assertRaisesRegex(ValueError, '入射介质.*未知'):
            self.build('ar_single_qw', assume_lossless=False)
        unknown = self.build('ar_single_qw', assume_lossless=True)
        self.assertTrue(any('入射介质' in note and '未提供 k' in note for note in unknown['assumptions']))


if __name__ == "__main__":
    unittest.main()
