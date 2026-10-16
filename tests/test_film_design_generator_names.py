"""Focused contracts for stable, conservative generated design names."""

from copy import deepcopy
import unittest

from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_templates import build_preview


def _material(identifier, n):
    return {
        "id": identifier,
        "name": identifier,
        "optical_sets": [{
            "id": identifier + "_set", "name": identifier + " data",
            "kind": "tabulated", "source": "synthetic unit-test values",
            "wavelength_unit": "nm", "wavelength_min": 450.0,
            "wavelength_max": 650.0, "wavelengths": [450.0, 550.0, 650.0],
            "n": [n, n, n], "k": [0.0, 0.0, 0.0],
        }],
    }


class FilmDesignGeneratorNameTests(unittest.TestCase):
    def setUp(self):
        self.options = {
            "film": [_material("F", 1.38), _material("F1", 1.38),
                     _material("F2", 2.2), _material("F3", 1.63),
                     _material("H", 2.1), _material("L", 1.45)],
            "medium": [_material("air", 1.0)],
            "substrate": [_material("glass", 1.5)],
        }
        self.bindings = {
            role: {"material_id": role, "optical_set_id": role + "_set"}
            for role in ("F", "F1", "F2", "F3", "H", "L")
        }
        self.endpoints = {
            "incident_binding": {"material_id": "air", "optical_set_id": "air_set"},
            "substrate_binding": {"material_id": "glass", "optical_set_id": "glass_set"},
        }

    def build(self, template_id, *, base=None, **changes):
        inputs = {
            "template_id": template_id, "wavelength_nm": 550.0, "pairs": 8,
            "role_bindings": deepcopy(self.bindings), "assume_lossless": True,
            **deepcopy(self.endpoints),
        }
        inputs.update(changes)
        return build_preview(inputs=inputs, base_design=base or new_design(),
                             material_options=self.options)

    def test_default_and_repeated_generation_use_stable_actual_pair_name(self):
        first = self.build("hr_hl_qw", pairs=5, wavelength_nm=532.0)
        candidate = first["candidate_design"]
        self.assertEqual(candidate["name"], "HR (HL)^5 · 532nm")

        second = self.build("hr_hl_qw", base=candidate, pairs=5, wavelength_nm=532.0)
        self.assertEqual(second["candidate_design"]["name"], candidate["name"])
        self.assertLessEqual(len(second["candidate_design"]["name"]), 120)

    def test_template_switch_and_parameter_change_refresh_automatic_name(self):
        first = self.build("hr_hl_qw", pairs=6)
        second = self.build("fp_halfwave_defect", base=first["candidate_design"],
                            pairs=3, wavelength_nm=600.0)
        self.assertEqual(second["candidate_design"]["name"],
                         "FP (HL)^3 C (LH)^3 · 600nm")

    def test_known_legacy_generated_names_collapse_but_unrecognized_suffixes_do_not(self):
        old_name = ("未命名膜系 - (HL)^N 四分之一波 HR - "
                    "(HL)^N 四分之一波 HR")
        generated = self.build("hr_hl_qw", base=new_design(name=old_name), pairs=2)
        self.assertEqual(generated["candidate_design"]["name"], "HR (HL)^2 · 550nm")

        custom_lookalike = "未命名膜系 - (HL)^N 四分之一波 HR - my note"
        preserved = self.build("hr_hl_qw", base=new_design(name=custom_lookalike), pairs=2)
        self.assertEqual(preserved["candidate_design"]["name"], custom_lookalike)

    def test_custom_name_is_preserved_across_generations_and_within_schema_limit(self):
        chinese_custom = self.build("ar_single_qw", base=new_design(name="膜系"))
        self.assertEqual(chinese_custom["candidate_design"]["name"], "膜系")
        custom_name = "My coating recipe"
        first = self.build("ar_single_qw", base=new_design(name=custom_name))
        self.assertEqual(first["candidate_design"]["name"], custom_name)
        second = self.build("ar_vcoat_qq", base=first["candidate_design"])
        self.assertEqual(second["candidate_design"]["name"], custom_name)

        long_custom_name = "x" * 120
        long_result = self.build("ar_single_qw", base=new_design(name=long_custom_name))
        self.assertEqual(long_result["candidate_design"]["name"], long_custom_name)
        self.assertEqual(len(long_result["candidate_design"]["name"]), 120)


if __name__ == "__main__":
    unittest.main()
