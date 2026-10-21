from __future__ import annotations

from pathlib import Path
import signal
import threading
import unittest

from tools.export_film_synthesis_cloud import build_bundle, import_closure
from tools.run_film_synthesis_cloud import (
    _checkpoint_matches_candidate,
    _install_cooperative_signal_handlers,
    _mean_metrics,
    _raw_bounds,
    _restore_signal_handlers,
)

ROOT = Path(__file__).resolve().parents[1]


class FilmSynthesisCloudBundleTests(unittest.TestCase):
    def test_first_audit_checkpoint_requires_matching_physical_identities(self):
        evidence = {
            "candidate_identity_sha256": "same",
            "training_identity_sha256": "same",
            "audit_identity_sha256": "same",
            "candidate_training_audit_identity_matches": True,
            "training_raw_pass": True,
            "audit_raw_pass": True,
            "source_sha256": "source",
        }
        self.assertTrue(_checkpoint_matches_candidate(evidence, "same", "source"))
        mismatched_training = {**evidence, "training_identity_sha256": "other"}
        self.assertFalse(_checkpoint_matches_candidate(mismatched_training, "same", "source"))
        mismatched_audit = {**evidence, "audit_identity_sha256": "other"}
        self.assertFalse(_checkpoint_matches_candidate(mismatched_audit, "same", "source"))

    def test_cooperative_sigterm_handler_is_restored(self):
        cancel_event = threading.Event()
        original = signal.getsignal(signal.SIGTERM)
        previous = _install_cooperative_signal_handlers(cancel_event)
        try:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
            self.assertTrue(cancel_event.is_set())
        finally:
            _restore_signal_handlers(previous)
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_git_mode_refuses_missing_checkout_identity_before_calculation(self):
        import tempfile
        from tools.run_film_synthesis_cloud import run_bundle
        manifest = ROOT / "benchmarks/film_synthesis/20261021/cloud-requests.json"
        with tempfile.TemporaryDirectory() as output:
            with self.assertRaisesRegex(ValueError, "repo-root and --expect-commit"):
                run_bundle(manifest, output=Path(output) / "unused", preflight=True)

    def test_git_mode_rejects_a_loaded_foreign_algorithm_module(self):
        import sys
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.run_film_synthesis_cloud import _activate_git_root
        with tempfile.TemporaryDirectory() as foreign:
            module = SimpleNamespace(__file__=str(Path(foreign) / "solver.py"))
            with patch.dict(sys.modules, {"coat_sim.foreign_algorithm": module}):
                with self.assertRaisesRegex(ValueError, "Foreign algorithm"):
                    _activate_git_root(ROOT)

    def test_git_mode_moves_the_selected_checkout_to_first_import_position(self):
        import sys
        from tools.run_film_synthesis_cloud import _activate_git_root
        original = list(sys.path)
        try:
            sys.path[:] = [entry for entry in sys.path if entry != str(ROOT)] + [str(ROOT)]
            _activate_git_root(ROOT)
            self.assertEqual(sys.path[0], str(ROOT))
            self.assertEqual(sys.path.count(str(ROOT)), 1)
        finally:
            sys.path[:] = original

    def test_git_frozen_requests_match_fresh_production_optics_and_targets(self):
        import json
        manifest = json.loads((ROOT / "benchmarks/film_synthesis/20261021/cloud-requests.json").read_text(encoding="utf-8"))
        fresh = build_bundle(seed=0, workers=2, repo_root=ROOT)
        self.assertEqual(manifest["kind"], "film_synthesis_git_requests")
        self.assertEqual([entry["case"] for entry in manifest["entries"]], [entry["case"] for entry in fresh["entries"]])
        for stored, actual in zip(manifest["entries"], fresh["entries"], strict=True):
            # The historical frozen requests predate the optional deadline.
            # Assert its exact current default, then compare every original
            # field without changing the stored request or its identity hash.
            current_request = dict(actual["request"])
            self.assertIn("max_seconds", current_request)
            self.assertIsNone(current_request.pop("max_seconds"))
            self.assertEqual(stored["request"], current_request)
            self.assertEqual(stored["wall_seconds"], 120)

    def test_import_closure_is_core_only_and_contains_algorithm_branches(self):
        modules = import_closure(ROOT)
        self.assertTrue({
            "coat_sim.core.film_synthesis_global",
            "coat_sim.core.film_synthesis_matching",
            "coat_sim.core.film_synthesis_refinement",
            "coat_sim.core.film_synthesis_forward",
            "coat_sim.core.film_synthesis_ar_clusters",
        }.issubset(modules))
        self.assertFalse(any(any(part in {"ui", "qml", "agent", "scheduler"}
                                 for part in module.split(".")) for module in modules))

    def test_frozen_public_suite_exports_the_eight_phase_cases_without_search(self):
        suite = ROOT / "benchmarks" / "film_synthesis" / "20261021" / "representative-suite-v2.1.json"
        bundle = build_bundle(suite, seed=0, workers=2, repo_root=ROOT)
        self.assertEqual(bundle["kind"], "film_synthesis_pure_cpu_bundle")
        self.assertEqual(len(bundle["entries"]), 8)
        self.assertEqual(len(bundle["entries"]), bundle["suite_request_count"])
        self.assertEqual(bundle["defaults"]["wall_seconds"], 120)
        self.assertEqual(bundle["defaults"]["workers"], 2)
        for entry in bundle["entries"]:
            self.assertIsNotNone(entry["request"].get("frozen_material_snapshot"))
            self.assertEqual(entry["wall_seconds"], 120)
            self.assertIn("ar_structure_mode", entry["request"])
        self.assertIn("GUI request builder", bundle["execution_contract"].get("freeze_provenance", ""))

    def test_audit_means_use_dense_points_only_and_preserve_raw_bounds(self):
        request = {
            "validation_samples_multiplier": 2,
            "strict_lower_bound_objective_indices": [],
            "objectives": [{"wavelength_min_nm": 1.0, "wavelength_max_nm": 3.0,
                "wavelength_samples": 2, "metric": "R", "lower_bound": 0.0,
                "upper_bound": 5.0, "angles_deg": [0.0], "polarization": "s"}],
        }
        score = {"errors": [
            {"objective_index": 0, "angle_deg": 0.0, "polarization": "s", "metric": "R", "lower_bound": 0.0, "upper_bound": 5.0, "wavelength_nm": 1.0, "actual": 0.0},
            {"objective_index": 0, "angle_deg": 0.0, "polarization": "s", "metric": "R", "lower_bound": 0.0, "upper_bound": 5.0, "wavelength_nm": 2.0, "actual": 2.0},
            {"objective_index": 0, "angle_deg": 0.0, "polarization": "s", "metric": "R", "lower_bound": 0.0, "upper_bound": 5.0, "wavelength_nm": 3.0, "actual": 4.0},
            {"objective_index": 0, "angle_deg": 0.0, "polarization": "s", "metric": "R", "lower_bound": 0.0, "upper_bound": 5.0, "wavelength_nm": 1.5, "actual": 50.0},
            {"objective_index": 0, "angle_deg": 0.0, "polarization": "s", "metric": "R", "lower_bound": 0.0, "upper_bound": 5.0, "wavelength_nm": 2.5, "actual": 50.0},
        ], "audit": {"dense_points": 3, "offset_points": 2,
                   "unique_condition_points": 5, "physical_evaluations": 1,
                   "wavelength_points": 5, "extra_points": 2,
                   "completed_extrema": 0, "bracket_count": 0,
                   "objective_status": [{}], "continuous_certificate": False,
                   "rms_measure": "fixed_dense_grid_only"}}
        mean = _mean_metrics(request, score)
        self.assertEqual(mean["mean_lambda"], 2.0)
        import math
        expected_log_mean = (math.log(2) + 3 * (math.log(3) - math.log(2))) / math.log(3)
        self.assertAlmostEqual(mean["mean_loglambda"], expected_log_mean)
        self.assertFalse(mean["extrema_and_half_step_offsets_included"])
        checked = _raw_bounds(request, score, scope="audit")
        self.assertFalse(checked["passed"])
        self.assertEqual(checked["violating_conditions"], 2)

    def test_line_target_reports_the_actual_sample(self):
        request = {"validation_samples_multiplier": 5, "objectives": [{
            "wavelength_min_nm": 550, "wavelength_max_nm": 550, "wavelength_samples": 1,
            "angles_deg": [0, 30], "polarization": "unpolarized"}]}
        score = {"errors": [
            {"objective_index": 0, "angle_deg": 0, "polarization": "unpolarized", "metric": "R", "wavelength_nm": 550, "actual": 0.2},
            {"objective_index": 0, "angle_deg": 30, "polarization": "unpolarized", "metric": "R", "wavelength_nm": 550, "actual": 0.4},
        ]}
        mean = _mean_metrics(request, score)
        self.assertAlmostEqual(mean["mean_lambda"], 0.3)
        self.assertAlmostEqual(mean["mean_loglambda"], 0.3)
        self.assertTrue(all(group["measure"] == "discrete_line_actual" for group in mean["groups"]))

    def test_mixed_metrics_keep_separate_means_without_cross_metric_aggregate(self):
        objectives = []
        errors = []
        for index, (metric, actual) in enumerate((("R", 0.2), ("T", 0.8))):
            objectives.append({"metric": metric, "wavelength_min_nm": 550,
                "wavelength_max_nm": 550, "wavelength_samples": 1,
                "angles_deg": [0], "polarization": "unpolarized"})
            errors.append({"objective_index": index, "angle_deg": 0,
                "polarization": "unpolarized", "metric": metric,
                "wavelength_nm": 550, "actual": actual})
        mean = _mean_metrics({"objectives": objectives}, {"errors": errors})
        self.assertIsNone(mean["mean_lambda"])
        self.assertIsNone(mean["mean_loglambda"])
        self.assertEqual(mean["reason"], "mixed_metrics_have_no_combined_physical_mean")
        self.assertEqual(mean["by_metric"]["R"]["mean_lambda"], 0.2)
        self.assertEqual(mean["by_metric"]["T"]["mean_loglambda"], 0.8)

    def test_raw_acceptance_rejects_unknown_duplicate_and_out_of_domain_conditions(self):
        request = {"acceptable_normalized_error": 0, "strict_lower_bound_objective_indices": [],
            "validation_samples_multiplier": 2,
            "objectives": [{"wavelength_min_nm": 1.0, "wavelength_max_nm": 2.0,
                "wavelength_samples": 2, "angles_deg": [0.0], "polarization": "s",
                "metric": "R", "mode": "range", "target_value": None,
                "lower_bound": None, "upper_bound": 1.0}]}
        row = {"objective_index": 0, "angle_deg": 0.0, "polarization": "s",
            "metric": "R", "wavelength_nm": 1.0, "actual": 0.5,
            "target_value": None, "lower_bound": None, "upper_bound": 1.0}
        cases = [
            ([{**row, "objective_index": 9}], "unknown_objective_index"),
            ([row, dict(row)], "duplicate_condition"),
            ([{**row, "wavelength_nm": 0.5}], "condition_outside_frozen_objective_domain"),
            ([{**row, "polarization": "p"}], None),
        ]
        for rows, reason in cases:
            checked = _raw_bounds(request, {"errors": rows}, scope="training")
            self.assertFalse(checked["available"])
            if reason:
                self.assertIn(reason, checked["invalid_conditions"])

    def test_audit_requires_complete_metadata_and_rejects_out_of_domain_extrema(self):
        request = {"acceptable_normalized_error": 0, "strict_lower_bound_objective_indices": [],
            "validation_samples_multiplier": 1,
            "objectives": [{"wavelength_min_nm": 1.0, "wavelength_max_nm": 2.0,
                "wavelength_samples": 1, "angles_deg": [0.0], "polarization": "s",
                "metric": "R", "mode": "range", "target_value": None,
                "lower_bound": None, "upper_bound": 1.0}]}
        row = {"objective_index": 0, "angle_deg": 0.0, "polarization": "s",
            "metric": "R", "wavelength_nm": 1.5, "actual": 0.5,
            "target_value": None, "lower_bound": None, "upper_bound": 1.0}
        complete_audit = {"dense_points": 1, "offset_points": 0, "unique_condition_points": 1,
            "physical_evaluations": 1, "wavelength_points": 1, "extra_points": 0,
            "completed_extrema": 0, "bracket_count": 0, "continuous_certificate": False,
            "rms_measure": "fixed_dense_grid_only", "objective_status": [{}]}
        missing = _raw_bounds(request, {"errors": [row]}, scope="audit")
        self.assertFalse(missing["available"])
        outside = _raw_bounds(request, {"errors": [{**row, "wavelength_nm": 2.5}],
                                       "audit": complete_audit}, scope="audit")
        self.assertFalse(outside["available"])
        self.assertIn("condition_outside_frozen_objective_domain", outside["invalid_conditions"])


if __name__ == "__main__":
    unittest.main()
