"""Independent paired harness contracts; no long matrix or policy tuning."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
import time
from typing import TypedDict
import unittest
from unittest.mock import patch

from tools import run_film_progress_acceptance as harness

class PolicyValues(TypedDict):
    accepted_window_steps: int
    relative_sse_progress_floor: float
    relative_worst_progress_floor: float
    stagnation_windows: int


POLICY: PolicyValues = dict(accepted_window_steps=16, relative_sse_progress_floor=.001,
                            relative_worst_progress_floor=.001, stagnation_windows=2)


class ProgressAcceptancePlanTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads(harness.SUITE.read_text(encoding="utf-8"))

    def test_preflight_cannot_call_physics_and_preserves_original_fixture(self):
        from coat_sim.core import film_optimization_structure as structure
        original = harness.SUITE.read_bytes()
        with patch.object(structure, "simulate_with_thickness_jacobian", side_effect=AssertionError("preflight optics")):
            plan = harness.make_plan(policy=POLICY)
        self.assertEqual(harness.SUITE.read_bytes(), original)
        self.assertEqual(len(plan["cases"]), 8)
        self.assertEqual(plan["seeds"], [0])
        self.assertEqual(plan["arms"], {"off": None, "on": POLICY})
        self.assertEqual(plan["search_admission_seconds"], 20.)
        self.assertEqual(plan["reserved_audit_seconds"], 10.)
        for row in plan["cases"]:
            self.assertEqual(row["training_caps"]["F"], row["training_caps"]["FJ"])
            self.assertFalse(row["case"]["assume_lossless"])

    def test_both_derivation_changes_only_identity_and_active_mean(self):
        for source in self.source["cases"]:
            if source["id"] not in harness.CASE_IDS:
                continue
            original = deepcopy(source)
            both = harness.derive_case(source, "both")
            both["id"] = source["id"]
            self.assertIsNone(both.pop("objective_progress_policy"))
            for actual, expected in zip(both["objectives"], source["objectives"], strict=True):
                self.assertEqual(actual["mean"], expected["pointwise"])
                actual["mean"] = deepcopy(expected["mean"])
            original.pop("historical_qualification", None)
            self.assertEqual(both, original)
            self.assertEqual(source, next(c for c in self.source["cases"] if c["id"] == source["id"]))

    def test_registered_grid_sizes_are_not_rounded_or_coarsened(self):
        expected = {"ar_broad_visible": 82, "hr_narrow_visible": 18,
                    "bpf_narrow_visible": 32, "mixed_multispectral_v1": 212}
        for case in self.source["cases"]:
            if case["id"] in expected:
                work = harness.grid_work(case["objectives"], case["max_layers"])
                self.assertEqual(work.wavelength_points, expected[case["id"]])
                self.assertEqual(work.layer_points, expected[case["id"]] * case["max_layers"])
        bandpass = next(c for c in self.source["cases"] if c["id"] == "bpf_narrow_visible")
        band = bandpass["objectives"][0]
        self.assertEqual((band["wavelength_min_nm"], band["wavelength_max_nm"], band["wavelength_samples"]), (549.75, 550.25, 6))

    def test_chunk_accounting_uses_complete_candidate_not_partial_allowance(self):
        self.assertEqual(harness.grid_work([dict(wavelength_samples=10002, angles_deg=[0., 30.])], 2),
                         harness.Work(4, 20004, 40008))

    def test_no_implicit_policy_or_unregistered_seeds(self):
        for value in (None, {}, {**POLICY, "extra": 1}, {**POLICY, "accepted_window_steps": True},
                      {**POLICY, "relative_sse_progress_floor": float("nan")}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                harness.validate_policy(value)
        for seeds in ((0, 0), (3,), (True,), ()):
            with self.subTest(seeds=seeds), self.assertRaises(ValueError):
                harness.make_plan(policy=POLICY, seeds=seeds)

    def test_score_metrics_keep_mean_and_worst_separate(self):
        score = dict(max_error=.2, weighted_rms_error=.1, nominal_pass=False, solver_acceptable=True,
            errors=[dict(reduction="pointwise", normalized_violation=.2),
                    dict(reduction="mean", normalized_violation=.01, actual=.54)],
            coverage=[dict(objective_index=0, angle_deg=0., wavelength_nm=[500., 550., 600.], actual_values=[.2, .8, .2])])
        result = harness.score_metrics(score)
        self.assertEqual(result["pointwise_max_error"], .2)
        self.assertAlmostEqual(result["weighted_sse"], .01)
        self.assertEqual(result["mean_constraints"][0]["actual"], .54)
        self.assertEqual(result["per_band"][0]["fixed_grid_mean"], .5)
        self.assertFalse(result["nominal_pass"])

    def test_real_request_freeze_is_physics_free_and_pair_clones_one_snapshot(self):
        from coat_sim.core import film_optimization_structure as structure
        from coat_sim.core.film_synthesis_progress import HybridProgressPolicy
        from tools import run_film_synthesis_benchmarks as runner
        plan = harness.make_plan(policy=POLICY)
        defaults, materials = harness.load_registered_materials(plan)
        with patch.object(structure, "simulate_with_thickness_jacobian", side_effect=AssertionError("preflight optics")):
            for row in plan["cases"]:
                _, baseline = runner.build_case_request(row["case"], defaults, materials, 0, 1)
                candidate = replace(baseline, objective_progress_policy=HybridProgressPolicy(**POLICY))
                self.assertEqual(replace(candidate, objective_progress_policy=None), baseline)
                self.assertEqual(harness.jsonable(baseline.objectives), row["case"]["objectives"])
                self.assertFalse(baseline.enable_thickness_reduction)
                self.assertEqual(candidate.frozen_material_snapshot, baseline.frozen_material_snapshot)


class ProgressPhysicalLedgerTests(unittest.TestCase):
    def setUp(self):
        from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest
        from coat_sim.core.film_optimization_structure import FilmStructureOptimizationRequest
        from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
        self.objective = FilmOptimizationObjective(500., 600., 3, angles_deg=(0., 30.),
            pointwise=FilmObjectiveConstraint(upper_bound=.1))
        self.request = FilmStructureOptimizationRequest(FilmOptimizationRequest(
            {}, 0, (), (self.objective,), validation_samples_multiplier=5))
        self.design = dict(front_layers=[dict(id="a"), dict(id="b")], back_layers=[])
        self.row = dict(training_caps={k: harness.jsonable(harness.Work(2, 6, 12)) for k in ("F", "FJ")},
            audit_caps={"F": harness.jsonable(harness.Work(100000, 100000, 200000)), "FJ": harness.jsonable(harness.Work())},
            case=dict(objectives=harness.jsonable((self.objective,))))

    def ledger(self):
        return harness.PhysicalLedger(self.row, time.monotonic() + 30.)

    @staticmethod
    def fake_kernel(design, payload, variables, settings, **kwargs):
        return dict(wavelength_nm=[0.] * settings["samples"])

    def fake_entry(self, request, design, **kwargs):
        from coat_sim.core import film_optimization_structure as structure
        variables = kwargs.get("variable_ids", ()) if kwargs.get("with_jacobian") else ()
        for objective in request.base_request.objectives:
            for _angle in objective.angles_deg:
                structure.simulate_with_thickness_jacobian(design, {}, variables,
                    dict(samples=objective.wavelength_samples))
        return {}

    def test_F_and_FJ_are_independent_and_next_whole_candidate_is_rejected(self):
        from coat_sim.core import film_auto_synthesis as synthesis
        from coat_sim.core import film_optimization_structure as structure
        ledger = self.ledger()
        with patch.object(structure, "evaluate_frozen_structure_candidate", self.fake_entry), \
             patch.object(structure, "simulate_with_thickness_jacobian", self.fake_kernel):
            with ledger.instrument():
                synthesis.evaluate_frozen_structure_candidate(self.request, self.design)
                synthesis.evaluate_frozen_structure_candidate(self.request, self.design, variable_ids=("a", "b"), with_jacobian=True)
                before = deepcopy(ledger.attempted_calls)
                with self.assertRaises(synthesis._Stopped):
                    synthesis.evaluate_frozen_structure_candidate(self.request, self.design, variable_ids=("a", "b"), with_jacobian=True)
                self.assertEqual(ledger.attempted_calls, before)
        report = ledger.report()
        self.assertTrue(report["complete"] and report["caps_respected"])
        self.assertEqual(report["completed"]["training"]["F"], report["completed"]["training"]["FJ"])
        self.assertEqual(report["completed"]["training"]["FJ"], dict(kernel_calls=2, wavelength_points=6, layer_points=12))
        self.assertEqual(report["stop_reason"], "physical_work_cap")

    def test_search_deadline_rejects_before_any_kernel(self):
        from coat_sim.core.film_auto_synthesis import _Stopped
        ledger = self.ledger()
        ledger.search_deadline = time.monotonic() - 1.
        with self.assertRaises(_Stopped):
            ledger.admit("training", "F", harness.Work(1, 1, 1))
        self.assertEqual(ledger.report()["attempted_kernel_calls"]["training"]["F"], 0)
        self.assertEqual(ledger.stop_reason, "search_wall_reservation")

    def test_executor_admission_stop_is_not_unknown_interrupted_physics(self):
        from threading import Event
        from coat_sim.core import film_auto_synthesis as synthesis
        from coat_sim.core import film_optimization_structure as structure
        from coat_sim.core.film_synthesis_parallel import OrderedEvaluationExecutor, EvaluationTask
        ledger = self.ledger()
        task = EvaluationTask(self.design, False, ())
        executor = OrderedEvaluationExecutor(1, Event())
        with patch.object(structure, "evaluate_frozen_structure_candidate", self.fake_entry), \
             patch.object(structure, "simulate_with_thickness_jacobian", self.fake_kernel):
            with ledger.instrument():
                def callback():
                    return structure.evaluate_frozen_structure_candidate(self.request, self.design)
                # The real executor expects complete response metadata. This
                # boundary test supplies it without performing any real optics.
                def completed():
                    callback()
                    return dict(responses=[dict(wavelength_nm=[500., 550., 600.]) for _ in range(2)])
                executor.serial(completed, task)
                with self.assertRaises(synthesis._Stopped):
                    executor.serial(completed, task)
        executor.close()
        self.assertEqual(executor.diagnostics()["serial_unknown_work"], 0)
        self.assertEqual(executor.diagnostics()["training_wavelength_points"], 6)
        self.assertEqual(len(ledger.admission_events), 2)
        self.assertFalse(ledger.serial_pre_admitted)

    def test_failed_kernel_is_not_reported_as_complete_or_free(self):
        from coat_sim.core import film_optimization_structure as structure
        ledger = self.ledger()
        with patch.object(structure, "evaluate_frozen_structure_candidate", self.fake_entry), \
             patch.object(structure, "simulate_with_thickness_jacobian", side_effect=structure._StructureCancelled()):
            with ledger.instrument(), self.assertRaises(structure._StructureCancelled):
                structure.evaluate_frozen_structure_candidate(self.request, self.design)
        report = ledger.report()
        self.assertFalse(report["complete"])
        self.assertEqual(report["incomplete_kernel_calls"], 1)
        self.assertEqual(report["attempted_kernel_calls"]["training"]["F"], 1)

    def test_complete_audit_is_reserved_before_its_first_inner_call(self):
        from coat_sim.core import film_synthesis_audit as audit
        from coat_sim.core import film_auto_synthesis as synthesis
        ledger = self.ledger()
        ledger.caps["audit"]["F"] = harness.Work()
        request = SimpleNamespace(objectives=(self.objective,), validation_samples_multiplier=5)
        with patch.object(audit, "audit_frozen_synthesis_candidate", side_effect=AssertionError("audit must not start")):
            with ledger.instrument(), self.assertRaises(synthesis._Stopped):
                audit.audit_frozen_synthesis_candidate(request, self.design)
        self.assertEqual(ledger.attempted_calls["audit"]["F"], 0)
        self.assertEqual(ledger.phase, "training")

    def test_audit_inner_physics_is_separate_and_does_not_reenter_training_admission(self):
        from coat_sim.core import film_synthesis_audit as audit
        from coat_sim.core import film_optimization_structure as structure
        ledger = self.ledger()
        request = SimpleNamespace(objectives=(self.objective,), validation_samples_multiplier=5)
        def fake_audit(request, design, **kwargs):
            return structure.evaluate_frozen_structure_candidate(self.request, design)
        with patch.object(structure, "evaluate_frozen_structure_candidate", self.fake_entry), \
             patch.object(structure, "simulate_with_thickness_jacobian", self.fake_kernel), \
             patch.object(audit, "audit_frozen_synthesis_candidate", fake_audit):
            with ledger.instrument():
                audit.audit_frozen_synthesis_candidate(request, self.design)
        self.assertEqual(len(ledger.admission_events), 1)
        self.assertEqual(ledger.admission_events[0]["phase"], "audit")
        self.assertEqual(ledger.actual["audit"]["F"], harness.Work(2, 6, 12))
        self.assertEqual(ledger.actual["training"]["F"], harness.Work())
        self.assertEqual(ledger.phase, "training")


if __name__ == "__main__":
    unittest.main()
