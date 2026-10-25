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


def current_objective(low=550., high=550., samples=1, *, metric="R", angles=(0.,),
                      pointwise=True, mean=False, upper=1.):
    from coat_sim.core.film_objective_payload import objective_to_payload
    from coat_sim.core.film_optimization import FilmObjectiveConstraint, FilmOptimizationObjective
    return objective_to_payload(FilmOptimizationObjective(low, high, samples,
        angles_deg=angles, metric=metric, polarization="s",
        pointwise=FilmObjectiveConstraint(enabled=pointwise, lower_bound=None, upper_bound=upper),
        mean=FilmObjectiveConstraint(enabled=mean, lower_bound=None, upper_bound=upper)))


def score_evidence(request, dense, audit=None):
    from dataclasses import asdict
    from coat_sim.core.film_objective_payload import objective_from_payload
    from coat_sim.core.film_objective_criteria import reduce_objective_responses
    objectives = tuple(objective_from_payload(row) for row in request["objectives"])
    reduced = reduce_objective_responses(objectives, dense)
    rows = reduced.conditions
    audit_reduced = None
    if audit is not None:
        audit_reduced = reduce_objective_responses(objectives, audit)
        rows = tuple(row for row in rows if row.reduction == "mean") + tuple(
            row for row in audit_reduced.conditions if row.reduction == "pointwise")
    score = {"errors": [asdict(row) for row in rows],
        "coverage": [asdict(row) for row in reduced.coverage],
        "max_error": max(row.normalized_violation for row in rows),
        "weighted_max_error": max(row.weighted_normalized_violation for row in rows),
        "weighted_rms_error": reduced.weighted_rms_error}
    if audit_reduced is not None:
        dense_count = sum(len(row.wavelength_nm) for row in reduced.coverage)
        raw_count = sum(len(row.wavelength_nm) for row in audit_reduced.coverage)
        offsets = sum(max(0, len(row.wavelength_nm) - 1) for row in reduced.coverage)
        score["audit_coverage"] = [asdict(row) for row in audit_reduced.coverage]
        score["audit"] = dict(dense_points=dense_count, offset_points=offsets,
            unique_condition_points=raw_count, physical_evaluations=1,
            wavelength_points=raw_count, extra_points=raw_count - dense_count,
            completed_extrema=0, bracket_count=0, continuous_certificate=False,
            rms_measure="fixed_dense_grid_only", objective_status=[{} for _ in objectives])
    import json
    return json.loads(json.dumps(score))


def frame(index, waves, values, angle=0.):
    return dict(objective_index=index, angle_deg=angle, wavelength_nm=waves, actual_values=values)


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
        import json
        import tempfile
        from tools.run_film_synthesis_cloud import run_bundle
        with tempfile.TemporaryDirectory() as output:
            manifest = Path(output) / "current-manifest.json"
            manifest.write_text(json.dumps({"kind": "film_synthesis_git_requests", "schema_version": 1,
                "smoke_bundle": True, "entries": [{"request": {"enable_thickness_reduction": True, "objective_progress_policy": None, "objectives": [current_objective()]}}]}), encoding="utf-8")
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

    def test_historical_git_requests_are_unsupported_and_keep_original_bytes(self):
        import json
        from tools.run_film_synthesis_acceptance import sha256_json
        from tools.run_film_synthesis_cloud import _load_manifest
        path = ROOT / "benchmarks/film_synthesis/20261021/cloud-requests.json"
        original = path.read_bytes()
        manifest = json.loads(original)
        for stored in manifest["entries"]:
            self.assertEqual(stored["request_sha256"], sha256_json(stored["request"]))
        with self.assertRaisesRegex(ValueError, "Unsupported legacy request format"):
            _load_manifest(path)
        self.assertEqual(path.read_bytes(), original)

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

    def test_current_explicit_fixture_exports_nested_requests_without_search(self):
        import tempfile
        from tests.test_film_synthesis_benchmark_runner import small_suite
        with tempfile.TemporaryDirectory() as output:
            suite = small_suite(Path(output))
            bundle = build_bundle(suite, case="tiny_production_contract", seed=0, workers=2, repo_root=ROOT)
        self.assertEqual(bundle["kind"], "film_synthesis_pure_cpu_bundle")
        self.assertEqual(len(bundle["entries"]), 1)
        objective = bundle["entries"][0]["request"]["objectives"][0]
        self.assertTrue(objective["pointwise"]["enabled"])
        self.assertFalse(objective["mean"]["enabled"])
        self.assertNotIn("mode", objective)
        self.assertNotIn("strict_lower_bound_objective_indices", bundle["entries"][0]["request"])

    def test_current_cloud_preflight_uses_schema_four_decoder_without_search(self):
        import json
        import tempfile
        from unittest.mock import patch
        from tests.test_film_synthesis_benchmark_runner import small_suite
        from tools.run_film_synthesis_cloud import run_bundle
        from coat_sim.core import film_synthesis_io
        decoded_versions = []
        original = film_synthesis_io.film_synthesis_request_from_payload
        def decode(payload):
            decoded_versions.append(payload["schema_version"])
            return original(payload)
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            bundle = build_bundle(small_suite(root), case="tiny_production_contract", seed=0, workers=1, repo_root=ROOT)
            manifest = root / "current-manifest.json"
            manifest.write_text(json.dumps(bundle), encoding="utf-8")
            with patch("tools.run_film_synthesis_cloud._source_fingerprint",
                       return_value={"bundle_source_sha256": "unit-source-identity"}), \
                 patch.object(film_synthesis_io, "film_synthesis_request_from_payload", side_effect=decode):
                checked = run_bundle(manifest, output=root / "not-created", preflight=True)
            self.assertFalse((root / "not-created").exists())
        self.assertEqual(decoded_versions, [4])
        self.assertTrue(checked["preflight_only"])

    def test_historical_suite_is_not_exported_under_current_acceptance(self):
        suite = ROOT / "benchmarks/film_synthesis/20261021/representative-suite-v2.1.json"
        original = suite.read_bytes()
        with self.assertRaisesRegex(ValueError, "Unsupported legacy request format"):
            build_bundle(suite, seed=0, workers=2, repo_root=ROOT)
        self.assertEqual(suite.read_bytes(), original)

    def test_audit_means_use_raw_dense_coverage_and_preserve_nested_pointwise_bounds(self):
        request = {"enable_thickness_reduction": True, "objective_progress_policy": None, "validation_samples_multiplier": 2, "acceptable_normalized_error": 0.,
            "objectives": [current_objective(1., 3., 2, upper=.5)]}
        score = score_evidence(request, [frame(0, [1., 2., 3.], [0., .2, .4])],
                               [frame(0, [1., 1.5, 2., 2.5, 3.], [0., .9, .2, .9, .4])])
        mean = _mean_metrics(request, score)
        self.assertAlmostEqual(mean["mean_lambda"], .2)
        import math
        expected_log = (.1 * math.log(2) + .3 * (math.log(3) - math.log(2))) / math.log(3)
        self.assertAlmostEqual(mean["mean_loglambda"], expected_log)
        self.assertFalse(mean["extrema_and_half_step_offsets_included"])
        checked = _raw_bounds(request, score, scope="audit")
        self.assertTrue(checked["available"])
        self.assertFalse(checked["passed"])
        self.assertEqual(checked["violating_conditions"], 2)

    def test_meanonly_audit_retains_raw_offsets_without_activating_pointwise_acceptance(self):
        request = {"enable_thickness_reduction": True, "objective_progress_policy": None, "validation_samples_multiplier": 2, "acceptable_normalized_error": 0.,
            "objectives": [current_objective(1., 3., 2, pointwise=False, mean=True, upper=.3)]}
        score = score_evidence(request, [frame(0, [1., 2., 3.], [0., .2, .4])],
                               [frame(0, [1., 1.5, 2., 2.5, 3.], [0., .9, .2, .9, .4])])
        self.assertIsNone(score["errors"][0]["wavelength_nm"])
        self.assertAlmostEqual(_mean_metrics(request, score)["mean_lambda"], .2)
        checked = _raw_bounds(request, score, scope="audit")
        self.assertTrue(checked["passed"])
        self.assertEqual(checked["required_condition_count"], 1)
        self.assertEqual(checked["required_raw_point_count"], 5)
        score["audit_coverage"][0]["wavelength_nm"].pop(1)
        score["audit_coverage"][0]["actual_values"].pop(1)
        self.assertFalse(_raw_bounds(request, score, scope="audit")["passed"])

    def test_line_target_reports_complete_raw_sample_for_each_angle(self):
        request = {"enable_thickness_reduction": True, "objective_progress_policy": None, "validation_samples_multiplier": 5,
            "objectives": [current_objective(angles=(0., 30.))]}
        score = score_evidence(request, [frame(0, [550.], [.2]), frame(0, [550.], [.4], angle=30.)])
        mean = _mean_metrics(request, score)
        self.assertAlmostEqual(mean["mean_lambda"], .3)
        self.assertAlmostEqual(mean["mean_loglambda"], .3)
        self.assertTrue(all(group["measure"] == "discrete_line_actual" for group in mean["groups"]))

    def test_mixed_metrics_keep_separate_raw_means(self):
        request = {"enable_thickness_reduction": True, "objective_progress_policy": None, "objectives": [current_objective(metric="R"), current_objective(metric="T")]}
        score = score_evidence(request, [frame(0, [550.], [.2]), frame(1, [550.], [.8])])
        mean = _mean_metrics(request, score)
        self.assertIsNone(mean["mean_lambda"])
        self.assertEqual(mean["reason"], "mixed_metrics_have_no_combined_physical_mean")
        self.assertEqual(mean["by_metric"]["R"]["mean_lambda"], .2)
        self.assertEqual(mean["by_metric"]["T"]["mean_loglambda"], .8)

    def test_current_raw_acceptance_rejects_unknown_duplicate_and_outside_conditions(self):
        from copy import deepcopy
        request = {"enable_thickness_reduction": True, "objective_progress_policy": None, "acceptable_normalized_error": 0., "objectives": [current_objective(1., 2., 2)]}
        score = score_evidence(request, [frame(0, [1., 2.], [.2, .4])])
        self.assertTrue(_raw_bounds(request, score, scope="training")["passed"])
        for corruption in ("unknown", "duplicate", "outside", "polarization"):
            broken = deepcopy(score)
            if corruption == "duplicate":
                broken["errors"].append(dict(broken["errors"][0]))
            else:
                key, value = {"unknown": ("objective_index", 9), "outside": ("wavelength_nm", .5),
                              "polarization": ("polarization", "p")}[corruption]
                broken["errors"][0][key] = value
            self.assertFalse(_raw_bounds(request, broken, scope="training")["available"])

    def test_legacy_raw_objective_is_explicitly_unsupported(self):
        request = {"enable_thickness_reduction": True, "objective_progress_policy": None, "objectives": [{"mode": "range", "upper_bound": .5}]}
        with self.assertRaisesRegex(ValueError, "Unsupported legacy request format"):
            _raw_bounds(request, {"errors": []}, scope="training")
        with self.assertRaisesRegex(ValueError, "Unsupported legacy request format"):
            _mean_metrics(request, {})

    def test_audit_rejects_incomplete_metadata_and_raw_coverage(self):
        from copy import deepcopy
        request = {"enable_thickness_reduction": True, "objective_progress_policy": None, "validation_samples_multiplier": 2, "acceptable_normalized_error": 0.,
            "objectives": [current_objective(1., 2., 2)]}
        score = score_evidence(request, [frame(0, [1., 1.5, 2.], [.2, .3, .4])],
                               [frame(0, [1., 1.25, 1.5, 1.75, 2.], [.2, .25, .3, .35, .4])])
        self.assertTrue(_raw_bounds(request, score, scope="audit")["passed"])
        for field, value in (("completed_extrema", 1), ("dense_points", 0), ("offset_points", 0),
                             ("continuous_certificate", True), ("unique_condition_points", 1)):
            broken = deepcopy(score)
            broken["audit"][field] = value
            self.assertFalse(_raw_bounds(request, broken, scope="audit")["available"])
        score["audit_coverage"][0]["wavelength_nm"][-1] = 2.5
        self.assertFalse(_raw_bounds(request, score, scope="audit")["available"])


if __name__ == "__main__":
    unittest.main()
