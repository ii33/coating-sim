"""Offline entrypoint budget contracts without starting a scheduler or calculation."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import Mock, patch

from coat_sim import algorithm_cli
from coat_sim.core.api import CoatService


class OfflineBudgetEntrypointTests(unittest.TestCase):
    def test_appearance_preflight_has_no_deadline_and_observes_child_exit(self):
        from coat_sim.core import appearance_compute
        for mode in ("completed", "failed_exit", "missing_output", "invalid_output"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                def child_result(command, **options):
                    self.assertNotIn("timeout", options)
                    if mode == "completed":
                        Path(command[-1]).write_text(json.dumps({"frozen": "completed"}), encoding="utf-8")
                    elif mode == "invalid_output":
                        Path(command[-1]).write_text("invalid JSON", encoding="utf-8")
                    return CompletedProcess(command, 1 if mode == "failed_exit" else 0, "", "")

                with patch.object(appearance_compute.subprocess, "run", side_effect=child_result) as run:
                    if mode == "completed":
                        self.assertEqual(appearance_compute._freeze_in_child(Path(temporary), {}),
                                         {"frozen": "completed"})
                    else:
                        with self.assertRaisesRegex(ValueError, "appearance_preflight_failed"):
                            appearance_compute._freeze_in_child(Path(temporary), {})
                run.assert_called_once()

    def test_algorithm_cli_omits_default_budget_and_preserves_explicit_budget(self):
        for algorithm in ("fast", "reference", "optimize"):
            for budget in (None, 15.0):
                with self.subTest(algorithm=algorithm, budget=budget), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    service = CoatService(root)
                    client = Mock()
                    client.submit.return_value = {"job_id": "test-budget", "state": "queued"}
                    arguments = ["--process", "recipe.cproc", "--algorithm", algorithm,
                                 "--detach", "--output", str(root / "output")]
                    if budget is not None:
                        arguments.extend(["--budget-s", str(budget)])
                    with (patch.object(algorithm_cli, "CoatService", return_value=service),
                          patch("coat_sim.scheduler.SchedulerClient", return_value=client),
                          patch("coat_sim.scheduler.freeze_spec", side_effect=lambda _service, _kind, parameters: parameters) as freeze,
                          redirect_stdout(StringIO())):
                        self.assertEqual(algorithm_cli.main(arguments), 0)
                    options = freeze.call_args.args[2]["algorithm_options"]
                    if budget is None:
                        self.assertNotIn("budget_s", options)
                    else:
                        self.assertEqual(options["budget_s"], budget)

    def test_recipe_estimate_does_not_turn_unknown_runtime_into_a_default_budget(self):
        for options in ({}, {"budget_s": None}, {"budget_s": 15.0}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary:
                service = CoatService(temporary)
                parameters = {"settings": {}, "backend": "CPU", "algorithm": "reference",
                              "algorithm_options": options}
                store = Mock()
                store.create_estimate.side_effect = lambda _kind, _parameters, calculation, _files: calculation
                with (patch("coat_sim.core.api.canonical_compute_parameters", return_value=parameters),
                      patch("coat_sim.core.compute_estimation.estimate", return_value={"summary": "recipe", "estimated_s": 10.0}),
                      patch("coat_sim.core.compute_estimation.input_file_fingerprints", return_value=[]),
                      patch("coat_sim.core.compute_tasks.ComputeTaskStore", return_value=store),
                      patch("coat_sim.scheduler.SchedulerClient")):
                    estimate = service.estimate_compute_task("process", parameters)
                self.assertIsNone(estimate["estimated_s"])
                self.assertEqual(estimate["budget_s"], options.get("budget_s"))
                self.assertIn("runtime is unknown", estimate["basis"])


if __name__ == "__main__":
    unittest.main()
