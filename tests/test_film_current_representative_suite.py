"""Current benchmark registration, without legacy runtime conversion or TMM."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from coat_sim.core.film_objective_payload import objective_from_payload
from tools import run_film_synthesis_benchmarks as runner

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / "benchmarks/film_synthesis/20261021/representative-suite-v2.1.json"
CURRENT = ROOT / "benchmarks/film_synthesis/20261024/representative-suite-current-pointwise.json"


class CurrentRepresentativeSuiteTests(unittest.TestCase):
    def setUp(self):
        self.old = json.loads(OLD.read_text(encoding="utf-8"))
        self.current = json.loads(CURRENT.read_text(encoding="utf-8"))

    def test_source_identity_and_score_non_equivalence_are_explicit(self):
        provenance = self.current["provenance"]
        self.assertEqual(provenance["source_lf_sha256"],
                         hashlib.sha256(OLD.read_bytes().replace(b"\r\n", b"\n")).hexdigest())
        self.assertIs(provenance["score_equivalence"], False)
        self.assertEqual(self.current["catalog_sha256"], self.old["catalog_sha256"])
        self.assertEqual(self.current["defaults"], self.old["defaults"])

    def test_every_case_preserves_non_objective_physical_and_budget_inputs(self):
        self.assertEqual(len(self.current["cases"]), 16)
        self.assertEqual(sum(len(c["objectives"]) for c in self.current["cases"]), 49)
        for old, current in zip(self.old["cases"], self.current["cases"], strict=True):
            with self.subTest(case=old["id"]):
                expected, actual = deepcopy(old), deepcopy(current)
                for field in ("objectives", "strict_lower_bound_objective_indices",
                              "strict_upper_bound_objective_indices", "qualification"):
                    expected.pop(field, None)
                actual.pop("objectives")
                historical = actual.pop("historical_qualification", None)
                self.assertEqual(historical, old.get("qualification"))
                self.assertEqual(expected, actual)

    def test_all_bounds_grids_and_strict_flags_are_preserved(self):
        for old, current in zip(self.old["cases"], self.current["cases"], strict=True):
            self.assertEqual(len(old["objectives"]), len(current["objectives"]))
            for index, (before, payload) in enumerate(zip(old["objectives"], current["objectives"], strict=True)):
                with self.subTest(case=old["id"], objective=index):
                    after = objective_from_payload(payload)
                    self.assertTrue(after.pointwise.enabled)
                    self.assertFalse(after.mean.enabled)
                    self.assertEqual(after.pointwise.mode, "range")
                    self.assertIsNone(after.pointwise.target_value)
                    for key, value in before.items():
                        if key in ("lower_bound", "upper_bound"):
                            self.assertEqual(getattr(after.pointwise, key), value)
                        elif key == "angles_deg":
                            self.assertEqual(after.angles_deg, tuple(value))
                        else:
                            self.assertEqual(getattr(after, key), value)
                    self.assertEqual(after.pointwise.strict_lower,
                                     index in old.get("strict_lower_bound_objective_indices", []))
                    self.assertEqual(after.pointwise.strict_upper,
                                     index in old.get("strict_upper_bound_objective_indices", []))
                    self.assertEqual(after.weight, 1.0)
                    self.assertEqual(after.normalization_scale, 1.0)

    def test_historical_suite_remains_rejected_by_current_runner(self):
        before = OLD.read_bytes()
        with self.assertRaisesRegex(ValueError, "Unsupported legacy request format"):
            runner.load_suite(OLD)
        self.assertEqual(OLD.read_bytes(), before)

    def test_current_suite_loads_without_modifying_inputs(self):
        before = CURRENT.read_bytes()
        suite, materials, identity = runner.load_suite(CURRENT)
        self.assertEqual(suite["suite_id"], self.current["suite_id"])
        self.assertTrue(materials)
        self.assertEqual(identity["suite_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(CURRENT.read_bytes(), before)

    def test_all_case_seed_pairs_freeze_through_current_job_contract(self):
        compiled = runner.compile_preflight(CURRENT, workers=1)
        self.assertEqual(len(compiled["runs"]), 48)
        pairs = set()
        for run in compiled["runs"]:
            request = run["frozen_request"]
            self.assertEqual(request["forward_backend"], "cpu")
            self.assertEqual(request["cpu_workers"], 1)
            self.assertFalse(request["environment"]["front_layers"])
            self.assertFalse(request["seed_designs"])
            pairs.add((request["environment"]["name"], request["structure_search_seed"]))
            for objective in request["objectives"]:
                self.assertTrue(objective["pointwise"]["enabled"])
                self.assertFalse(objective["mean"]["enabled"])
        self.assertEqual(len(pairs), 48)


if __name__ == "__main__":
    unittest.main()
