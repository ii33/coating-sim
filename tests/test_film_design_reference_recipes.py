"""Contracts for the fixed public reference-recipe catalog and mappings."""
from copy import deepcopy
import json
import math
import unittest

from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_reference_recipes import (
    build_reference_recipe_preview,
    reference_recipe_catalog,
)


def _dataset(identifier, n, k=0.0, band=(450.0, 650.0)):
    return {
        "id": identifier + "_set", "name": identifier + " dataset",
        "kind": "tabulated", "source": "reference recipe unit test",
        "wavelength_unit": "nm", "wavelength_min": band[0],
        "wavelength_max": band[1], "wavelengths": [band[0], 550.0, band[1]],
        "n": [n, n, n], "k": None if k is None else [k, k, k],
    }


def _material(identifier, n, k=0.0, band=(450.0, 650.0)):
    return {"id": identifier, "name": identifier,
            "optical_sets": [_dataset(identifier, n, k, band)]}


class FilmDesignReferenceRecipeTests(unittest.TestCase):
    def setUp(self):
        self.options = {
            "film": [
                _material("mapped_f", 1.42), _material("mapped_f1", 1.4),
                _material("mapped_f2", 2.25), _material("mapped_f3", 1.6),
                _material("unknown_k", 1.7, None), _material("absorbing", 2.1, 0.01),
                _material("gain", 1.8, -0.01),
                _material("out_of_band", 1.5, band=(600.0, 700.0)),
            ],
            "medium": [_material("air", 1.0), _material("water", 1.33),
                       _material("absorbing_port", 1.2, 1e-14)],
            "substrate": [_material("glass", 1.5), _material("quartz", 1.46)],
        }
        self.base = new_design()
        self.ports = {
            "incident_binding": {"material_id": "air", "optical_set_id": "air_set"},
            "substrate_binding": {"material_id": "glass", "optical_set_id": "glass_set"},
        }

    def inputs(self, recipe_id="comsol_qhq_mgf2_zro2_cef3", **changes):
        roles = {
            "F1": {"material_id": "mapped_f1", "optical_set_id": "mapped_f1_set"},
            "F2": {"material_id": "mapped_f2", "optical_set_id": "mapped_f2_set"},
            "F3": {"material_id": "mapped_f3", "optical_set_id": "mapped_f3_set"},
            "F": {"material_id": "mapped_f", "optical_set_id": "mapped_f_set"},
        }
        data = {"recipe_id": recipe_id,
                "role_bindings": {key: roles[key] for key in {
                    "comsol_ideal_single_ar": ("F",),
                    "comsol_qq_mgf2_cef3": ("F1", "F2"),
                    "comsol_qhq_mgf2_zro2_cef3": ("F1", "F2", "F3"),
                }[recipe_id]},
                "mapping_confirmed": True, "assume_lossless": False,
                **deepcopy(self.ports)}
        data.update(changes)
        return data

    def build(self, **changes):
        return build_reference_recipe_preview(
            inputs=self.inputs(**changes), base_design=self.base,
            material_options=self.options)

    def test_catalog_is_detached_json_and_contains_four_nominal_recipes(self):
        rows = reference_recipe_catalog()
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({row["stable_id"] for row in rows}), 4)
        self.assertEqual(json.loads(json.dumps(rows, ensure_ascii=False, allow_nan=False)), rows)
        for row in rows[:3]:
            self.assertEqual(row["lambda0_nm"], 550.0)
            self.assertIsNone(row["publication_date"])
            self.assertEqual(row["checked_date"], "2026-10-11")
            self.assertEqual(row["version"], "6.4")
            self.assertIn("400–800", row["original_material_band"])
            self.assertEqual(row["nominal_endpoints"]["incident"]["n"], 1.0)
            self.assertEqual(row["nominal_endpoints"]["substrate"]["n"], 1.5)
            self.assertTrue(all("material_id" not in layer and "optical_set_id" not in layer
                                for layer in row["layers"]))
        self.assertEqual([layer["role"] for layer in rows[1]["layers"]], ["F1", "F2"])
        self.assertEqual([layer["role"] for layer in rows[2]["layers"]], ["F1", "F2", "F3"])
        self.assertAlmostEqual(rows[0]["layers"][0]["nominal_n"], math.sqrt(1.5))
        self.assertAlmostEqual(rows[2]["layers"][1]["thickness_nm"], 125.0)
        rows[1]["layers"][0]["thickness_nm"] = -1
        self.assertGreater(reference_recipe_catalog()[1]["layers"][0]["thickness_nm"], 0)
        tfcalc = reference_recipe_catalog()[3]
        self.assertEqual(tfcalc["stable_id"], "tfcalc_growing_ar_13")
        self.assertEqual(tfcalc["lambda0_nm"], 10000.0)
        self.assertEqual(tfcalc["version"], "未标注（官方公开示例）")
        self.assertIsNone(tfcalc["publication_date"])
        self.assertEqual([role["role"] for role in tfcalc["roles"]], ["H", "L"])
        self.assertEqual([layer["role"] for layer in tfcalc["layers"]],
                         ["L", "H", "L", "H", "L", "H", "L", "H", "L", "H", "L", "H", "L"])
        self.assertEqual([layer["thickness_nm"] for layer in tfcalc["layers"]],
                         [1076.97, 629.03, 457.49, 131.55, 1234.92, 1142.59,
                          2590.54, 81.75, 1827.50, 84.60, 563.34, 285.97, 157.14])
        self.assertTrue(all(layer["qw_multiplier"] is None for layer in tfcalc["layers"]))

    def test_preview_preserves_nominal_thickness_and_reverses_schema_storage_order(self):
        before = deepcopy(self.base)
        preview = self.build()
        self.assertEqual(self.base, before)
        self.assertEqual([row["role"] for row in preview["nominal_layers_incident_order"]],
                         ["F1", "F2", "F3"])
        mapped = preview["mapped_layers_incident_order"]
        self.assertAlmostEqual(mapped[0]["n_at_wavelength"], 1.4)
        self.assertAlmostEqual(mapped[0]["delta_n"], 1.4 - 1.38)
        self.assertAlmostEqual(mapped[1]["n_at_wavelength"], 2.25)
        self.assertAlmostEqual(mapped[2]["n_at_wavelength"], 1.6)
        for actual, expected in zip(
                [row["fixed_thickness_nm"] for row in mapped],
                [550 / (4 * 1.38), 125.0, 550 / (4 * 1.63)]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual([row["material_id"] for row in preview["candidate_design"]["front_layers"]],
                         ["mapped_f3", "mapped_f2", "mapped_f1"])
        self.assertEqual(preview["candidate_design"]["back_layers"], [])
        self.assertEqual(preview["candidate_design"]["substrate"]["sides"], "single")
        self.assertNotIn("reference_results", preview)
        self.assertNotIn("reflectance", preview)
        json.dumps({key: value for key, value in preview.items() if key != "candidate_design"})
        self.assertIn("参考配方映射", preview["candidate_design"]["note"])
        self.assertIn("mapped_f1_set", preview["candidate_design"]["note"])

    def test_single_layer_is_project_formula_and_qoq_uses_fixed_layer_table(self):
        single = self.build(recipe_id="comsol_ideal_single_ar")
        qq = self.build(recipe_id="comsol_qq_mgf2_cef3")
        self.assertEqual(single["recipe"]["origin"], "project_formula_fixed_reference")
        self.assertAlmostEqual(single["nominal_layers_incident_order"][0]["nominal_n"], math.sqrt(1.5))
        self.assertEqual(len(qq["mapped_layers_incident_order"]), 2)
        self.assertAlmostEqual(qq["mapped_layers_incident_order"][0]["fixed_thickness_nm"],
                               550 / (4 * 1.38))
        self.assertAlmostEqual(qq["mapped_layers_incident_order"][1]["fixed_thickness_nm"],
                               550 / (4 * 1.63))

    def test_confirmation_fixed_lambda_mapping_and_material_dataset_are_required(self):
        with self.assertRaisesRegex(ValueError, "Confirm"):
            self.build(mapping_confirmed=False)
        with self.assertRaisesRegex(ValueError, "固定为 550 nm"):
            self.build(wavelength_nm=551.0)
        with self.assertRaisesRegex(ValueError, "入射端口 k=0"):
            self.build(recipe_id="comsol_ideal_single_ar",
                       role_bindings={"F": {"material_id": "mapped_f", "optical_set_id": "mapped_f_set"}},
                       incident_binding={"material_id": "absorbing_port",
                                         "optical_set_id": "absorbing_port_set"})
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.build(role_bindings={})
        bad_roles = self.inputs()["role_bindings"]
        bad_roles["F1"] = {"material_id": "unknown", "optical_set_id": "mapped_f1_set"}
        with self.assertRaises(ValueError):
            self.build(role_bindings=bad_roles)
        with self.assertRaises(ValueError):
            self.build(role_bindings={
                "F1": {"material_id": "out_of_band", "optical_set_id": "out_of_band_set"},
                "F2": {"material_id": "mapped_f2", "optical_set_id": "mapped_f2_set"},
                "F3": {"material_id": "mapped_f3", "optical_set_id": "mapped_f3_set"},
            })

    def test_unknown_k_requires_explicit_assumption_and_known_absorption_is_preserved(self):
        unknown_roles = self.inputs()["role_bindings"]
        unknown_roles["F1"] = {"material_id": "unknown_k", "optical_set_id": "unknown_k_set"}
        with self.assertRaises(ValueError):
            self.build(role_bindings=unknown_roles, assume_lossless=False)
        preview = self.build(role_bindings=unknown_roles, assume_lossless=True)
        self.assertIsNone(preview["mapped_layers_incident_order"][0]["k_at_wavelength"])
        self.assertTrue(any("已明确对未知 k 假设无损" in text
                            for text in preview["assumptions"]))

        absorbing_roles = self.inputs(recipe_id="comsol_ideal_single_ar")["role_bindings"]
        absorbing_roles["F"] = {"material_id": "absorbing", "optical_set_id": "absorbing_set"}
        absorbed = self.build(recipe_id="comsol_ideal_single_ar",
                              role_bindings=absorbing_roles, assume_lossless=True)
        self.assertAlmostEqual(absorbed["mapped_layers_incident_order"][0]["k_at_wavelength"], 0.01)
        self.assertAlmostEqual(absorbed["mapped_layers_incident_order"][0]["delta_k"], 0.01)
        self.assertTrue(any("保留该 k" in text for text in absorbed["assumptions"]))
        self.assertIn("k=0.01", absorbed["candidate_design"]["note"])

        gain_roles = self.inputs(recipe_id="comsol_ideal_single_ar")["role_bindings"]
        gain_roles["F"] = {"material_id": "gain", "optical_set_id": "gain_set"}
        with self.assertRaisesRegex(ValueError, "k 必须非负"):
            self.build(recipe_id="comsol_ideal_single_ar",
                       role_bindings=gain_roles, assume_lossless=True)

    def test_tfcalc_keeps_source_fixed_nm_and_maps_only_unique_h_l_roles(self):
        def long_dataset(identifier, n):
            return {
                "id": identifier + "_set", "name": identifier + " dataset",
                "kind": "tabulated", "source": "reference recipe unit test",
                "wavelength_unit": "nm", "wavelength_min": 7000.0,
                "wavelength_max": 13000.0,
                "wavelengths": [7000.0, 10000.0, 13000.0],
                "n": [n, n, n], "k": [0.0, 0.0, 0.0],
            }

        options = {
            "film": [
                {"id": "mapped_H", "name": "Mapped H", "optical_sets": [long_dataset("mapped_H", 3.9)]},
                {"id": "mapped_L", "name": "Mapped L", "optical_sets": [long_dataset("mapped_L", 2.25)]},
            ],
            "medium": [{"id": "air", "name": "Air", "optical_sets": [long_dataset("air", 1.0)]}],
            "substrate": [{"id": "ge_sub", "name": "Substrate", "optical_sets": [long_dataset("ge_sub", 4.0)]}],
        }
        preview = build_reference_recipe_preview(
            inputs={
                "recipe_id": "tfcalc_growing_ar_13",
                "role_bindings": {
                    "H": {"material_id": "mapped_H", "optical_set_id": "mapped_H_set"},
                    "L": {"material_id": "mapped_L", "optical_set_id": "mapped_L_set"},
                },
                "incident_binding": {"material_id": "air", "optical_set_id": "air_set"},
                "substrate_binding": {"material_id": "ge_sub", "optical_set_id": "ge_sub_set"},
                "mapping_confirmed": True,
                "assume_lossless": False,
            },
            base_design=self.base,
            material_options=options,
        )
        expected_d = [1076.97, 629.03, 457.49, 131.55, 1234.92, 1142.59,
                      2590.54, 81.75, 1827.50, 84.60, 563.34, 285.97, 157.14]
        self.assertEqual(preview["lambda0_nm"], 10000.0)
        self.assertEqual(preview["layer_count"], 13)
        self.assertEqual([row["fixed_thickness_nm"] for row in preview["mapped_layers_incident_order"]], expected_d)
        self.assertEqual([row["n_at_wavelength"] for row in preview["mapped_layers_incident_order"]],
                         [2.25 if row["role"] == "L" else 3.9
                          for row in preview["mapped_layers_incident_order"]])
        self.assertTrue(all(row["qw_multiplier"] is None
                            for row in preview["mapped_layers_incident_order"]))
        self.assertEqual([row["material_id"] for row in preview["candidate_design"]["front_layers"]],
                         ["mapped_L" if role == "L" else "mapped_H"
                          for role in reversed([row["role"] for row in preview["mapped_layers_incident_order"]])])
        self.assertAlmostEqual(preview["total_physical_thickness_nm"], 10263.39)
        self.assertNotIn("reflectance", preview)
        self.assertIn("10000 nm", preview["candidate_design"]["note"])


if __name__ == "__main__":
    unittest.main()
