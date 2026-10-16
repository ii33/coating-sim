import json
import math
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from coat_sim.core.film_design import (
    normalize_expression,
    expand_expression, load_design, new_design, physical_thickness_nm,
    save_design, validate_design,
)


def symbols():
    return {
        key: {"material_id": f"mat-{key}", "optical_set_id": f"nk-{key}",
              "thickness": {"value": 100.0, "unit": "nm"}, "note": "test"}
        for key in "ABCHLG"
    }


class FilmDesignTests(unittest.TestCase):
    def test_official_simple_formula_is_substrate_outward(self):
        layers = expand_expression("ABCCCBA", symbols())
        self.assertEqual([layer["symbol"] for layer in layers], list("ABCCCBA"))
        self.assertEqual([layer["material_id"] for layer in layers], [f"mat-{c}" for c in "ABCCCBA"])
        self.assertEqual(len({layer["id"] for layer in layers}), 7)

    def test_repetition_nesting_and_multipliers(self):
        repeated = expand_expression("(ABCBA)^10", symbols())
        self.assertEqual(len(repeated), 50)
        self.assertEqual("".join(layer["symbol"] for layer in repeated), "ABCBA" * 10)
        nested = expand_expression("1.2 (0.5 A B 0.5 C)^2", symbols())
        self.assertEqual([layer["symbol"] for layer in nested], ["A", "B", "C"] * 2)
        self.assertEqual([layer["thickness"]["value"] for layer in nested], [60, 120, 60] * 2)

    def test_group_labels_are_preserved(self):
        layers = expand_expression("G1 (HL)^5 G2 (HL)^5", symbols())
        self.assertEqual(len(layers), 20)
        self.assertEqual({layer["group"] for layer in layers[:10]}, {"G1"})
        self.assertEqual({layer["group"] for layer in layers[10:]}, {"G2"})

    def test_invalid_expressions(self):
        for formula in ("(AB)", "(AB)^1.5", "A^2", "Z", "A$", "2", "()^2", "(AB", "G3 A", "G12 A"):
            with self.subTest(formula=formula), self.assertRaises(ValueError):
                expand_expression(formula, symbols())

    def test_repeat_budget_and_zero_layer_guard(self):
        with self.assertRaisesRegex(ValueError, "limit"):
            expand_expression("(AB)^10", symbols(), max_layers=10)
        with self.assertRaisesRegex(ValueError, "limit"):
            expand_expression("(AB)^1000000000000000000000", symbols())
        with self.assertRaisesRegex(ValueError, "at least one layer"):
            expand_expression("(G1)^1000000000", symbols())
        with self.assertRaisesRegex(ValueError, "at least one layer"):
            expand_expression("A (G2)^1000000000", symbols())

    def test_nesting_depth_limit(self):
        accepted = "(" * 64 + "A" + ")^1" * 64
        self.assertEqual(len(expand_expression(accepted, symbols())), 1)
        rejected = "(" * 65 + "A" + ")^1" * 65
        with self.assertRaisesRegex(ValueError, "nesting exceeds 64"):
            expand_expression(rejected, symbols())

    def test_thickness_unit_conversion(self):
        self.assertEqual(physical_thickness_nm({"thickness": {"value": 2, "unit": "um"}}, 550, 1.5), 2000)
        self.assertAlmostEqual(physical_thickness_nm({"thickness": {"value": 1, "unit": "qw"}}, 550, 1.5), 550 / 6)

    def test_design_roundtrip_and_forward_version_rejection(self):
        design = new_design("AR coating", "design-1")
        design["front_layers"] = expand_expression("AB", symbols())
        design["expression"] = {"text": "AB", "symbols": symbols(), "in_sync": True}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ar.cfilm"
            save_design(path, design)
            self.assertEqual(load_design(path), validate_design(design))
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["schema_version"] = 2
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported"):
                load_design(path)

    def test_default_save_refuses_overwrite_and_explicit_overwrite_replaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "design.cfilm"
            first, second = new_design("first", "one"), new_design("second", "two")
            save_design(path, first)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                save_design(path, second)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaisesRegex(ValueError, "explicit boolean"):
                save_design(path, second, overwrite=1)
            self.assertEqual(path.read_bytes(), original)
            save_design(path, second, overwrite=True)
            self.assertEqual(load_design(path)["name"], "second")

    def test_concurrent_no_clobber_save_has_one_winner(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "race.cfilm"
            designs = [new_design(f"writer-{i}", f"writer-{i}") for i in range(6)]

            def attempt(design):
                try:
                    save_design(path, design)
                    return True
                except FileExistsError:
                    return False

            with ThreadPoolExecutor(max_workers=6) as pool:
                outcomes = list(pool.map(attempt, designs))
            self.assertEqual(sum(outcomes), 1)
            self.assertIn(load_design(path)["name"], {design["name"] for design in designs})

    def test_draft_refs_and_explicit_lossless_assumption(self):
        design = new_design()
        self.assertEqual(design["substrate"]["material_id"], "")
        self.assertFalse(design["assume_lossless"])
        design["assume_lossless"] = True
        self.assertTrue(validate_design(design)["assume_lossless"])

    def test_nonfinite_thickness_and_invalid_unit_rejected(self):
        design = new_design()
        design["front_layers"] = [{"material_id": "m", "optical_set_id": "o",
                                   "thickness": {"value": math.nan, "unit": "nm"}}]
        with self.assertRaises(ValueError):
            validate_design(design)
        for invalid_unit in ([], {}, None):
            layer = {"thickness": {"value": 1, "unit": invalid_unit}}
            with self.subTest(unit=invalid_unit), self.assertRaises(ValueError):
                physical_thickness_nm(layer, 550, 1.5)


    def test_expression_is_multiline_case_normalized_without_changing_ids(self):
        design = new_design()
        design["expression"]["text"] = "a\n(a)^2"
        design["expression"]["symbols"] = {
            "A": {"material_id": "Material_MixedCase", "optical_set_id": "Set_Case",
                  "thickness": {"value": 75, "unit": "nm"}}
        }
        normalized = validate_design(design)
        self.assertEqual(normalized["expression"]["text"], "A\n(A)^2")
        self.assertEqual(normalized["expression"]["symbols"]["A"]["material_id"], "Material_MixedCase")
        self.assertEqual(normalized["expression"]["symbols"]["A"]["optical_set_id"], "Set_Case")
        self.assertEqual(normalize_expression("a\n(b)^3"), "A\n(B)^3")

    def test_new_design_air_and_legacy_use_k_metadata_defaults(self):
        design = new_design()
        self.assertEqual(design["incident_medium"], {"kind": "constant", "n": 1})
        self.assertEqual(design["note"], "")
        self.assertTrue(design["use_k"])
        legacy = dict(design)
        legacy.pop("note")
        legacy.pop("use_k")
        loaded = validate_design(legacy)
        self.assertEqual(loaded["note"], "")
        self.assertTrue(loaded["use_k"])



if __name__ == "__main__":
    unittest.main()
