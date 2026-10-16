"""Independent cub real spawned-CPU contract regressions, temporary data only."""
from pathlib import Path
import tempfile
import threading
import unittest
from tests.test_cub_interfaces import fixture
from coat_sim.core.api import ServiceError
from coat_sim.mcp.server import _algorithm_metadata


class CubAlgorithmReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service, self.path, _, _ = fixture(Path(self.tmp.name))

    def run_path(self, algorithm, options, **kwargs):
        return self.service.simulate_recipe(
            {"grid_size": 21, "time_steps": 4, "carrier_rpm": 0.},
            process_path=self.path, backend="CPU", algorithm=algorithm,
            algorithm_options=options, **kwargs)

    def test_real_spawn_reference_converges_after_two_comparisons(self):
        result = self.run_path("reference", {"budget_s": 15., "max_evaluations": 4,
            "sampling_policy": "requested", "periodic_integration": False})
        report = result["algorithm_report"]
        self.assertTrue(report["converged"])
        self.assertEqual(report["status"], "temporal_converged")
        self.assertEqual(report["evaluations"], 3)
        self.assertEqual([row["time_steps"] for row in report["trace"]], [4, 8, 16])
        self.assertEqual(result["requested_settings"]["time_steps"], 4)

    def test_real_spawn_running_cancel_never_returns_success(self):
        event = threading.Event()
        updates = []
        def progress(value):
            updates.append(value)
            event.set()
        with self.assertRaises(InterruptedError):
            self.run_path("reference", {"budget_s": 15., "max_evaluations": 4},
                          cancel_event=event, progress=progress)
        self.assertTrue(updates)

    def test_fast_common_limit_retains_legacy_behavior_with_visible_warning(self):
        result = self.run_path("fast", {"max_evaluations": 3})
        self.assertEqual(result["algorithm_report"]["evaluations"], 1)
        self.assertTrue(any("one evaluation" in row for row in result["execution_option_warnings"]))
        self.assertEqual(_algorithm_metadata(result)["execution_option_warnings"],
                         result["execution_option_warnings"])

    def test_invalid_options_preserve_public_service_error(self):
        with self.assertRaises(ServiceError) as caught:
            self.run_path("reference", {"max_evaluations": True})
        self.assertEqual(caught.exception.code, "simulation_error")

    def test_legacy_common_options_are_explicitly_ignored_on_other_paths(self):
        for algorithm, options in (("fast", {"tolerance_percent": .2, "rpm_scale_bounds": [.8, 1.2]}),
                ("reference", {"max_evaluations": 1, "rpm_scale_bounds": [.8, 1.2]}),
                ("optimize", {"max_evaluations": 1, "tolerance_percent": .2})):
            with self.subTest(algorithm=algorithm):
                result = self.run_path(algorithm, options)
                self.assertTrue(result["execution_option_warnings"])
                self.assertEqual(result["algorithm_report"]["evaluations"], 1)
