from copy import deepcopy
from dataclasses import replace
from threading import Event
import unittest
import numpy as np

from coat_sim.core.film_performance import (CoherentBatchEvaluator, FrozenFilmEvaluator,
    PerformanceOptions, optimize_film_design_accelerated)
from coat_sim.core.film_optimization import (FilmOptimizationRequest, FilmOptimizationObjective,
    FilmThicknessVariable, optimize_film_design)
from tests.film_performance_fixtures import fixture

class PerformanceTests(unittest.TestCase):
    def test_service_parity_supported_and_fallback_models(self):
        for double, sub, first, use_k in [(False, 1000., "front", True),
                (True, 1000., "front", True), (True, 1000., "back", False),
                (True, 100000., "front", True), (True, 100000., "back", True)]:
            service, design, frozen = fixture(double=double, substrate_nm=sub, use_k=use_k)
            for polarization in ("s", "p", "unpolarized"):
                settings = dict(wavelength_min_nm=400., wavelength_max_nm=800., samples=31,
                                angle_deg=43., polarization=polarization, first_surface=first)
                backend = FrozenFilmEvaluator(frozen, settings, options=PerformanceOptions(backend="cpu", cpu_workers=2))
                ids = [row["id"] for row in design["front_layers"]]
                d = np.array([[row["thickness"]["value"] for row in design["front_layers"]]])
                actual = backend.evaluate_thickness_batch(ids, np.concatenate([d, d * 1.12]))
                for i, ds in enumerate((d[0], d[0] * 1.12)):
                    candidate = deepcopy(design)
                    for row, value in zip(candidate["front_layers"], ds):
                        row["thickness"] = dict(unit="nm", value=float(value))
                    expected = service.simulate(candidate, settings)
                    for key in ("R", "T", "A"):
                        np.testing.assert_allclose(actual[key][i], expected[key], rtol=2e-12, atol=2e-12)
                if sub == 100000.:
                    self.assertIn("incoherent", actual["metadata"]["fallback_reason"])

    def test_freeze_digest_ids_zero_cancel_and_memory(self):
        _, _, frozen = fixture()
        options = PerformanceOptions(max_result_bytes=1024)
        backend = FrozenFilmEvaluator(frozen, {"samples": 3}, options=options)
        ids = ["layer-0"]
        with self.assertRaises(ValueError):
            backend.evaluate_thickness_batch(ids, [[0.]])
        backend.evaluate_thickness_batch(ids, [[0.]], zero_thickness_layer_ids=ids)
        with self.assertRaises(ValueError):
            backend.evaluate_thickness_batch(ids, np.ones((16, 1)))
        event = Event(); event.set()
        with self.assertRaises(InterruptedError):
            backend.evaluate_thickness_batch(ids, [[80.]], cancel=event)
        frozen["materials"][0]["name"] = "tampered"
        with self.assertRaises(ValueError):
            FrozenFilmEvaluator(frozen)

    def test_gpu_unavailable_or_critical_is_explicit_fallback(self):
        from coat_sim.core.film_optics_batch import FrozenCoherentStack
        stack = FrozenCoherentStack.prepare([500., 600.], [2., 1., 1.5], beta=1.)
        backend = CoherentBatchEvaluator(stack, PerformanceOptions(backend="gpu"))
        actual = backend.evaluate([[50.]])
        np.testing.assert_allclose(actual, stack.evaluate([[50.]]))
        self.assertEqual(backend.last_metadata["backend"], "cpu")
        self.assertIn("critical", backend.last_metadata["fallback_reason"])

    def test_prepared_memory_and_unbound_inactive_exit(self):
        _, design, frozen = fixture()
        with self.assertRaises(ValueError):
            FrozenFilmEvaluator(frozen, {"samples": 50001},
                                options=PerformanceOptions(max_prepared_bytes=1024))
        from tests.film_performance_fixtures import fixture as fresh
        service, design, frozen = fresh()
        design["front_layers"][0]["thickness"] = {"unit": "qw", "value": 1.2}
        design["exit_medium"] = {"kind": "material", "material_id": "unused", "optical_set_id": "unused"}
        frozen["design"] = design
        import hashlib, json
        frozen["sha256"] = hashlib.sha256(json.dumps({key: value for key, value in frozen.items() if key != "sha256"},
            sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        settings = dict(samples=7)
        expected = service.simulate(design, settings)
        evaluator = FrozenFilmEvaluator(frozen, settings)
        actual = evaluator.evaluate_thickness_batch([], np.empty((1, 0)))
        np.testing.assert_allclose(actual["R"][0], expected["R"], atol=2e-12, rtol=2e-12)

    def test_cli_uses_same_frozen_service_without_file_access(self):
        import io, json, types
        from unittest.mock import patch
        from coat_sim.film_performance_cli import main
        _, _, frozen = fixture()
        request = dict(frozen_snapshot=frozen, settings={"samples": 5},
                       layer_ids=["layer-0"], thickness_nm=[[70.]])
        output = io.StringIO()
        with patch("sys.argv", ["film-cli", "evaluate"]), patch("sys.stdin", types.SimpleNamespace(
                buffer=io.BytesIO(json.dumps(request).encode()))), patch("sys.stdout", output):
            self.assertEqual(main(), 0)
        actual = json.loads(output.getvalue())
        self.assertTrue(actual["ok"])
        self.assertEqual(len(actual["result"]["R"][0]), 5)

    def test_qw_error_policy_and_immutable_service_coordinates(self):
        import hashlib, json
        service, design, frozen = fixture()
        design["reference_wavelength_nm"] = 1200.
        design["front_layers"][0]["thickness"] = {"unit": "qw", "value": 1.}
        frozen["design"] = design
        frozen["sha256"] = hashlib.sha256(json.dumps({k: v for k, v in frozen.items() if k != "sha256"},
            sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        settings = dict(samples=3, extrapolation_policy="error")
        with self.assertRaises(ValueError):
            service.simulate(design, settings)
        with self.assertRaises(ValueError):
            FrozenFilmEvaluator(frozen, settings)
        evaluator = FrozenFilmEvaluator(frozen, dict(samples=3, extrapolation_policy="endpoint_hold"))
        with self.assertRaises(ValueError):
            evaluator.waves.setflags(write=True)
        exposed = evaluator.settings
        exposed["samples"] = 999
        self.assertEqual(evaluator.settings["samples"], 3)

    def test_all_eight_opt_in_modes_preserve_original_behavior(self):
        service, design, frozen = fixture(layers=1)
        request = FilmOptimizationRequest(design, 7,
            (FilmThicknessVariable("front", "layer-0", 30., 140.),),
            (FilmOptimizationObjective(450., 700., 9, target_value=0.),),
            max_iterations=2, max_evaluations=25, acceptable_normalized_error=0.,
            frozen_material_snapshot=frozen, validation_samples_multiplier=1)
        for mode in ("baseline", "powell", "nelder_mead", "multistart",
                     "differential_evolution", "simulated_annealing", "trf", "lbfgsb"):
            req = replace(request, solver=mode)
            expected = optimize_film_design(service, req)
            actual = optimize_film_design_accelerated(service, req)
            self.assertEqual(actual.evaluations, expected.evaluations, mode)
            self.assertAlmostEqual(actual.max_normalized_error, expected.max_normalized_error, places=11, msg=mode)
            self.assertEqual(actual.state, expected.state)
            self.assertEqual(actual.validation_complete, expected.validation_complete)
            self.assertEqual(actual.best_design["id"], expected.best_design["id"])

if __name__ == "__main__":
    unittest.main()
