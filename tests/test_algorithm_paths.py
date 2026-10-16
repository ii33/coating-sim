"""Small, real product-model checks for the bounded recipe execution paths.

The clock is replaced in deadline tests, and heatmap rendering is omitted in
isolated persistence tests. The service, dependency resolver, numerical solver,
NPZ writer, and asset reader remain the production versions. Persisted products
live in a temporary root, never the project/user data.
"""

from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coat_sim.core.algorithm_paths import BudgetExceeded
from coat_sim.core.api import CoatService, ServiceError, recipe_algorithm_options
from coat_sim.core.process_simulator import simulate_process
from coat_sim.core.schemas import RecipeAlgorithmOptions
from coat_sim.core.simulation_assets import (
    prepare_simulation_asset,
    read_simulation_asset,
    save_simulation_asset,
)
from coat_sim.core.simulator import Settings


class AlgorithmPathTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(grid_size=21, time_steps=12)
        from tests.design_test_fixtures import copy_typed_examples
        self.temp_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_root.cleanup)
        copy_typed_examples(Path(self.temp_root.name))
        self.service = CoatService(self.temp_root.name)
        self.process_path = "examples/process_example.cproc"

    def run_path(self, algorithm, *, settings=None, options=None, **kwargs):
        # Retain explicit coverage of the original full-time reference mode.
        if algorithm == "reference":
            options = {"sampling_policy": "requested", "periodic_integration": False, **(options or {})}
        # These algorithm unit tests deliberately inject a local clock and
        # callbacks. Execute the production numerical function behind the
        # service boundary locally; real spawn/isolation is tested separately
        # by test_isolated_compute and test_queue_memory_refinements.
        from coat_sim.core.algorithm_paths import run_recipe
        with patch("coat_sim.core.isolated_compute.simulate_recipe_isolated", side_effect=run_recipe):
            return self.service.simulate_recipe(
                self.settings if settings is None else settings,
                process_path=self.process_path,
                backend="CPU",
                algorithm=algorithm,
                algorithm_options=options,
                **kwargs,
            )

    def assert_same_arrays(self, left, right):
        for key in ("field_nm", "x", "y"):
            np.testing.assert_array_equal(left[key], right[key], err_msg=key)

    def test_fast_matches_legacy_exactly_with_and_without_cycle(self):
        runtime = self.service.load("process", self.process_path).runtime
        for cycle in (False, True):
            with self.subTest(fast_cycle=cycle):
                legacy = simulate_process(
                    self.settings, runtime, backend="CPU", fast_cycle=cycle,
                    fast_cycle_residual_percent=5.0,
                )
                actual = self.run_path("fast", fast_cycle=cycle)
                self.assert_same_arrays(actual, legacy)
                for key in ("device", "algorithm", "time_steps", "computed_time_steps",
                            "requested_settings", "effective_settings", "step_results"):
                    self.assertEqual(actual[key], legacy[key], key)
                report = actual["algorithm_report"]
                self.assertEqual(report["algorithm"], "fast")
                self.assertEqual(report["status"], "completed")
                self.assertEqual(report["evaluations"], 1)
                self.assertIsNone(report["converged"])
                self.assertIsNone(report["budget_s"])

    def test_non_search_paths_accept_valid_maximum_carrier_speed(self):
        # Search bounds cannot restrict a legacy call that does not change rpm.
        for algorithm in ("fast", "reference"):
            with self.subTest(algorithm=algorithm):
                result = self.run_path(
                    algorithm, settings=replace(self.settings, carrier_rpm=300),
                    options={"max_evaluations": 1},
                )
                self.assertEqual(result["algorithm_report"]["evaluations"], 1)

    def test_reference_reports_unconverged_when_evaluation_limit_is_reached(self):
        for limit in (1, 2):
            with self.subTest(limit=limit):
                result = self.run_path("reference", options={
                    "max_evaluations": limit, "tolerance_percent": 1e-12,
                }, fast_cycle=True)
                report = result["algorithm_report"]
                self.assertIs(report["converged"], False)
                self.assertEqual(report["status"], "evaluation_limit")
                self.assertEqual(report["evaluations"], limit)
                self.assertEqual([row["time_steps"] for row in report["trace"]],
                                 [12 * 2 ** index for index in range(limit)])
                self.assertEqual(result["field_nm"].dtype, np.dtype("float64"))
                self.assertIn("CPU", result["device"])
                self.assertTrue(all(row["algorithm"] != "cycle"
                                    for row in result["step_results"]))
                self.assertTrue(report["unverified_dimensions"])
                self.assertTrue(report["warning"])

    def test_reference_requires_two_consecutive_passes_for_stationary_case(self):
        stationary = replace(self.settings, carrier_rpm=0)
        options = {"max_evaluations": 4, "tolerance_percent": 1e-9}
        result = self.run_path("reference", settings=stationary, options=options)
        report = result["algorithm_report"]
        self.assertIs(report["converged"], True)
        self.assertEqual(report["status"], "temporal_converged")
        self.assertEqual(report["evaluations"], 3)
        self.assertEqual(report["required_consecutive_passes"], 2)
        self.assertEqual([row["time_steps"] for row in report["trace"]], [12, 24, 48])
        self.assertTrue(all(row["max_field_change_percent"] <= options["tolerance_percent"]
                            for row in report["trace"][1:]))
        baseline = self.run_path("fast", settings=stationary)
        np.testing.assert_allclose(result["field_nm"], baseline["field_nm"],
                                   rtol=1e-12, atol=1e-12, equal_nan=True)

    def test_optimize_keeps_validated_result_inside_bounds(self):
        baseline = self.run_path("fast")
        for limit in (1, 2, 5, 11):
            with self.subTest(limit=limit):
                result = self.run_path("optimize", options={
                    "budget_s": 30, "max_evaluations": limit,
                    "rpm_scale_bounds": [0.8, 1.2],
                }, fast_cycle=True)
                report = result["algorithm_report"]
                trace = report["trace"]
                self.assertGreaterEqual(report["evaluations"], 1)
                self.assertLessEqual(report["evaluations"], limit)
                self.assertEqual(len(trace), report["evaluations"])
                self.assertEqual(trace[0]["rpm_scale"], 1.0)
                self.assertAlmostEqual(report["initial_cv_percent"], baseline["cv_percent"], places=12)
                self.assertAlmostEqual(report["best_cv_percent"], result["cv_percent"], places=12)
                self.assertEqual(report["accepted_cv_percent"], report["best_cv_percent"])
                self.assertEqual(report["accepted_carrier_rpm"], report["best_carrier_rpm"])
                search_trace = [row for row in trace if not row.get("phase")]
                self.assertEqual(report["provisional_best_cv_percent"],
                                 min(row["cv_percent"] for row in search_trace))
                self.assertTrue(all(0.8 <= row["rpm_scale"] <= 1.2 for row in trace))
                self.assertTrue(all(abs(row["carrier_rpm"]) <= 300 for row in trace))
                self.assertEqual(report["best_carrier_rpm"],
                                 self.settings.carrier_rpm * report["best_rpm_scale"])
                accepted_steps = result["effective_settings"]["time_steps"]
                comparable_baseline = self.run_path("fast", settings=replace(
                    self.settings, time_steps=accepted_steps))
                self.assertLessEqual(result["cv_percent"], comparable_baseline["cv_percent"])
                reproduced = self.run_path("fast", settings=replace(
                    self.settings, carrier_rpm=report["best_carrier_rpm"],
                    time_steps=accepted_steps))
                self.assert_same_arrays(result, reproduced)
                if report["accepted_carrier_rpm"] != self.settings.carrier_rpm:
                    self.assertEqual(report["validation_status"], "accepted_at_finer_sampling")
                    self.assertEqual(accepted_steps, self.settings.time_steps * 8)
                if limit < 3:
                    self.assert_same_arrays(result, baseline)
                    self.assertEqual(report["accepted_carrier_rpm"], self.settings.carrier_rpm)
                self.assertTrue(all(row["algorithm"] != "cycle"
                                    for row in result["step_results"]))
                self.assertTrue(report["warning"])

    def test_optimize_rejects_real_low_sampling_false_improvement(self):
        settings = Settings(grid_size=31, time_steps=120)
        result = self.run_path("optimize", settings=settings,
                               options={"max_evaluations": 11})
        report = result["algorithm_report"]
        self.assertEqual(report["evaluations"], 11)
        self.assertAlmostEqual(report["provisional_best_carrier_rpm"], 17.25)
        self.assertLess(report["provisional_best_cv_percent"], report["initial_cv_percent"])
        self.assertEqual(report["validation_status"], "rejected_at_finer_sampling")
        self.assertEqual(report["validation"]["time_steps"], 960)
        self.assertGreater(report["validation"]["candidate_cv_percent"],
                           report["validation"]["baseline_cv_percent"])
        self.assertEqual(report["accepted_carrier_rpm"], settings.carrier_rpm)
        self.assertEqual(result["effective_settings"]["time_steps"], 960)
        self.assertEqual(result["requested_settings"], asdict(settings))
        finer_baseline = self.run_path("fast", settings=replace(settings, time_steps=960))
        self.assert_same_arrays(result, finer_baseline)
        self.assertEqual([row.get("phase") for row in report["trace"][-2:]],
                         ["validation_baseline", "validation_candidate"])

    def test_optimize_accepts_only_after_completed_finer_pair(self):
        settings = replace(self.settings, time_steps=120, carrier_rpm=12)
        result = self.run_path("optimize", settings=settings,
                               options={"max_evaluations": 11})
        report = result["algorithm_report"]
        self.assertEqual(report["validation_status"], "accepted_at_finer_sampling")
        self.assertEqual(report["evaluations"], 11)
        self.assertAlmostEqual(report["accepted_carrier_rpm"], 13.8)
        self.assertEqual(report["accepted_carrier_rpm"], report["provisional_best_carrier_rpm"])
        self.assertLess(report["validation"]["candidate_cv_percent"],
                        report["validation"]["baseline_cv_percent"])
        self.assertEqual(result["effective_settings"]["time_steps"], 960)
        self.assertEqual(result["requested_settings"], asdict(settings))
        reproduced = self.run_path("fast", settings=replace(
            settings, time_steps=960, carrier_rpm=report["accepted_carrier_rpm"]))
        self.assert_same_arrays(result, reproduced)

    def test_progress_reports_real_evaluation_counts_and_final_report(self):
        for algorithm in ("fast", "reference", "optimize"):
            with self.subTest(algorithm=algorithm):
                updates = []
                result = self.run_path(algorithm, options={"max_evaluations": 3},
                                       progress=lambda event: updates.append(dict(event)))
                report = result["algorithm_report"]
                self.assertEqual(updates[-1], report)
                running = [item for item in updates[:-1] if "fraction" not in item]
                self.assertEqual([item["evaluations"] for item in running],
                                 list(range(report["evaluations"])))
                self.assertTrue(all(item["algorithm"] == algorithm for item in updates))
                self.assertTrue(all(item["status"] == "running" for item in running))
                self.assertTrue(all(item["max_evaluations"] == 3 for item in running))

    def test_requested_settings_remain_original_after_refinement_or_search(self):
        for algorithm in ("reference", "optimize"):
            with self.subTest(algorithm=algorithm):
                result = self.run_path(algorithm, options={"max_evaluations": 3})
                self.assertEqual(result["requested_settings"], asdict(self.settings))
                if algorithm == "reference":
                    self.assertEqual(result["effective_settings"]["time_steps"], 48)
                else:
                    self.assertEqual(result["effective_settings"]["carrier_rpm"],
                                     result["algorithm_report"]["best_carrier_rpm"])

    def test_pre_cancelled_runs_never_report_completion(self):
        for algorithm in ("fast", "reference", "optimize"):
            with self.subTest(algorithm=algorithm):
                cancellation = threading.Event()
                cancellation.set()
                updates = []
                with self.assertRaises(InterruptedError):
                    self.run_path(algorithm, cancel_event=cancellation, progress=updates.append)
                self.assertEqual(updates, [])

    def test_cancellation_from_progress_reaches_real_solver(self):
        for algorithm in ("fast", "reference", "optimize"):
            with self.subTest(algorithm=algorithm):
                cancellation = threading.Event()
                updates = []

                def cancel(event):
                    updates.append(dict(event))
                    cancellation.set()

                with self.assertRaises(InterruptedError):
                    self.run_path(algorithm, cancel_event=cancellation, progress=cancel)
                self.assertEqual([item["status"] for item in updates], ["running"])

    def test_omitted_and_null_recipe_budgets_are_unlimited_after_clock_jump(self):
        # Public validation and the MCP schema both preserve the nullable,
        # omitted-by-default budget. Exercise the real recipe path too, with
        # a fake clock that jumps beyond both the historical 60-second
        # default and the maximum supported explicit budget before work starts.
        for algorithm in ("fast", "reference", "optimize"):
            with self.subTest(algorithm=algorithm):
                for options in (None, {"budget_s": None}):
                    with self.subTest(options=options):
                        validated = recipe_algorithm_options(
                            algorithm, options, "CPU", self.settings.carrier_rpm)
                        self.assertEqual(validated, options or {})
                        schema_input = {} if options is None else options
                        self.assertIsNone(
                            RecipeAlgorithmOptions.model_validate(schema_input).budget_s)

                        clock = {"started": False}

                        def jump_past_all_default_budgets():
                            if not clock["started"]:
                                clock["started"] = True
                                return 0.0
                            return 301.0

                        run_options = {"max_evaluations": 1}
                        if options is not None:
                            run_options.update(options)
                        with patch("coat_sim.core.algorithm_paths.time",
                                   SimpleNamespace(monotonic=jump_past_all_default_budgets)):
                            result = self.run_path(algorithm, options=run_options)
                        report = result["algorithm_report"]
                        self.assertIsNone(report["budget_s"])
                        self.assertGreater(report["elapsed_s"], 300.0)
                        self.assertEqual(report["status"], "completed" if algorithm == "fast"
                                         else "evaluation_limit")

    def test_initial_budget_expiry_raises_instead_of_returning_success(self):
        for algorithm in ("fast", "reference", "optimize"):
            with self.subTest(algorithm=algorithm):
                moments = iter((0.0, 2.0))
                fake_time = SimpleNamespace(monotonic=lambda: next(moments))
                with patch("coat_sim.core.algorithm_paths.time", fake_time):
                    with self.assertRaises(BudgetExceeded):
                        self.run_path(algorithm, options={"budget_s": 1})

    def test_budget_exhaustion_preserves_only_completed_baseline(self):
        baseline = self.run_path("fast")
        for algorithm in ("reference", "optimize"):
            with self.subTest(algorithm=algorithm):
                clock = {"now": 0.0}
                updates = []

                def exhaust_on_second_evaluation(event):
                    updates.append(dict(event))
                    if event["status"] == "running" and event["evaluations"] == 1:
                        clock["now"] = 2.0

                fake_time = SimpleNamespace(monotonic=lambda: clock["now"])
                with patch("coat_sim.core.algorithm_paths.time", fake_time):
                    result = self.run_path(algorithm, options={
                        "budget_s": 1, "max_evaluations": 5,
                    }, progress=exhaust_on_second_evaluation)
                self.assert_same_arrays(result, baseline)
                report = result["algorithm_report"]
                self.assertEqual(report["status"], "budget_exhausted")
                self.assertEqual(report["evaluations"], 1)
                self.assertEqual(len(report["trace"]), 1)
                self.assertEqual(updates[-1], report)
                self.assertIsNot(report["converged"], True)

    def test_backend_wrapped_budget_expiry_retains_completed_baseline(self):
        baseline = self.run_path("fast")
        for algorithm in ("reference", "optimize"):
            with self.subTest(algorithm=algorithm):
                clock = {"armed": False, "checks": 0}

                def monotonic():
                    if clock["armed"]:
                        clock["checks"] += 1
                    # After the second evaluation starts, the first two
                    # checks are process and simulator entry. The third is
                    # inside _simulate_on_backend's RuntimeError wrapper.
                    return 2.0 if clock["checks"] >= 3 else 0.0

                def arm_second_evaluation(event):
                    if event["status"] == "running" and event["evaluations"] == 1:
                        clock["armed"] = True

                with patch("coat_sim.core.algorithm_paths.time",
                           SimpleNamespace(monotonic=monotonic)):
                    result = self.run_path(algorithm, options={
                        "budget_s": 1, "max_evaluations": 5,
                    }, progress=arm_second_evaluation)
                self.assertGreaterEqual(clock["checks"], 3)
                self.assertEqual(result["algorithm_report"]["status"], "budget_exhausted")
                self.assertEqual(result["algorithm_report"]["evaluations"], 1)
                self.assert_same_arrays(result, baseline)

    def test_optimize_incomplete_validation_never_accepts_provisional_candidate(self):
        settings = replace(self.settings, grid_size=31, time_steps=120)
        for interrupted_evaluation in (9, 10):
            with self.subTest(interrupted_evaluation=interrupted_evaluation):
                clock = {"now": 0.0}

                def expire_during_validation(event):
                    if (event["status"] == "running" and
                            event["evaluations"] == interrupted_evaluation):
                        clock["now"] = 2.0

                with patch("coat_sim.core.algorithm_paths.time",
                           SimpleNamespace(monotonic=lambda: clock["now"])):
                    result = self.run_path("optimize", settings=settings, options={
                        "budget_s": 1, "max_evaluations": 11,
                    }, progress=expire_during_validation)
                report = result["algorithm_report"]
                self.assertEqual(report["status"], "budget_exhausted")
                self.assertEqual(report["validation_status"], "inconclusive_budget")
                self.assertEqual(report["evaluations"], interrupted_evaluation)
                self.assertEqual(report["accepted_carrier_rpm"], settings.carrier_rpm)
                self.assertNotEqual(report["provisional_best_carrier_rpm"], settings.carrier_rpm)
                expected_steps = 120 if interrupted_evaluation == 9 else 960
                self.assertEqual(result["effective_settings"]["time_steps"], expected_steps)
                expected = self.run_path("fast", settings=replace(settings, time_steps=expected_steps))
                self.assert_same_arrays(result, expected)

    def test_invalid_algorithm_and_options_are_service_errors(self):
        with self.assertRaises(ServiceError) as caught:
            self.run_path("unknown")
        self.assertEqual(caught.exception.code, "simulation_error")
        invalid = [{"unknown_option": 1}]
        for value in (0, -1, 301, True, float("nan"), float("inf"), "bad"):
            invalid.append({"budget_s": value})
        for value in (0, -1, True, float("nan"), float("inf"), "bad"):
            invalid.append({"tolerance_percent": value})
        for value in (0, 101, True, 1.5, "2"):
            invalid.append({"max_evaluations": value})
        for value in ([], [1], [1, 1, 1], "0.8,1.2", [1, 1], [0, 1.2],
                      [1.1, 1.2], [0.5, 0.9], [True, 1.2], [0.8, float("inf")]):
            invalid.append({"rpm_scale_bounds": value})
        for options in invalid:
            with self.subTest(options=options):
                with self.assertRaises(ServiceError) as caught:
                    self.run_path("optimize", options=options)
                self.assertEqual(caught.exception.code, "simulation_error")
        with self.assertRaises(ServiceError):
            self.run_path("optimize", settings=replace(self.settings, carrier_rpm=290),
                          options={"rpm_scale_bounds": [0.8, 1.2]})

    def test_npz_and_coatasset_roundtrip_retain_algorithm_report_and_arrays(self):
        process = self.service.load("process", self.process_path)
        for algorithm in ("fast", "reference", "optimize"):
            with self.subTest(algorithm=algorithm), tempfile.TemporaryDirectory() as root:
                settings = (replace(self.settings, time_steps=120, carrier_rpm=12)
                            if algorithm == "optimize" else self.settings)
                limit = 11 if algorithm == "optimize" else 2
                result = self.run_path(algorithm, settings=settings,
                                       options={"max_evaluations": limit})
                report = result["algorithm_report"]
                if algorithm == "optimize":
                    self.assertEqual(report["validation_status"], "accepted_at_finer_sampling")
                # Rendering separately enforces the installed project data
                # root. Isolate this metadata/array roundtrip from that UI
                # policy and avoid touching the user's assets or font cache.
                renderer = SimpleNamespace(render_result_visualizations=
                                           lambda *args, **kwargs: {"files": []})
                with patch.dict(sys.modules, {
                    "coat_sim.core.result_visualization": renderer,
                }):
                    receipt = prepare_simulation_asset(
                        root, result=result, settings=settings, machine=None,
                        process=process.data, elapsed_s=report["elapsed_s"],
                        requested_device="CPU",
                    )
                self.assertTrue(Path(receipt["result_file"]).is_relative_to(root))
                with np.load(receipt["result_file"], allow_pickle=False) as archived:
                    metadata = json.loads(archived["metadata_json"].item())
                    self.assertEqual(metadata["result_metadata"]["algorithm_report"], report)
                    self.assertEqual(metadata["requested_settings"], asdict(settings))
                    self.assert_same_arrays(archived, result)
                saved = save_simulation_asset(root, receipt["run_id"],
                                              "test-" + algorithm, confirmed=True)
                self.assertTrue(Path(saved["asset_path"]).is_relative_to(root))
                self.assertEqual(Path(saved["asset_path"]).suffix, ".coatasset")
                restored = read_simulation_asset(saved["asset_path"], detail="full",
                                                 include_arrays=True)
                self.assertEqual(restored["result_metadata"]["algorithm_report"], report)
                self.assert_same_arrays(restored["arrays"], result)
                loaded = CoatService(root).load_simulation_asset(saved["asset_path"])
                self.assertEqual(loaded["result"]["algorithm_report"], report)
                self.assert_same_arrays(loaded["result"], result)


if __name__ == "__main__":
    unittest.main()
