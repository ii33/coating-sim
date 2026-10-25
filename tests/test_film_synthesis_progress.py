"""Pure policy, accepted-state and current-wire contracts; no usercase optics."""

from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import numpy as np
import pytest

from coat_sim.core.film_synthesis_progress import (
    AcceptedProgressMonitor,
    HybridProgressPolicy,
    ProgressSample,
    progress_policy_from_payload,
    relative_progress,
)
from coat_sim.core.film_synthesis_refinement import refine_synthesis_explorer
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_objective_criteria import (
    FilmObjectiveConstraint,
    reduce_objective_responses,
)
from coat_sim.core.film_optimization import FilmOptimizationObjective
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def sample(i, sse=1.0, maximum=1.0, *, passed=False, thickness=1.0):
    return ProgressSample(i, sse, maximum, maximum, passed, f"physical-{i}", (thickness,))


@pytest.mark.parametrize(
    "old,new,expected",
    [
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 1.0),
        (0.0, 1.0, -1.0),
        (2.0, 1.0, 0.5),
        (1.0, 2.0, -0.5),
        (1e-320, 0.0, 1.0),
        (1e308, 5e307, 0.5),
    ],
)
def test_relative_progress_has_no_epsilon_or_overflow(old, new, expected):
    assert relative_progress(old, new) == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("accepted_window_steps", 0),
        ("accepted_window_steps", True),
        ("stagnation_windows", 0),
        ("stagnation_windows", 1.5),
        ("relative_sse_progress_floor", float("nan")),
        ("relative_sse_progress_floor", True),
        ("relative_worst_progress_floor", float("inf")),
        ("relative_worst_progress_floor", -1.0),
    ],
)
def test_policy_invalid_configuration_rejected(field, value):
    values = asdict(HybridProgressPolicy(2, 0.01, 0.01, 2))
    values[field] = value
    with pytest.raises(ValueError, match="objective_progress_policy"):
        progress_policy_from_payload(values)


def test_policy_no_threshold_defaults_or_unknown_fields():
    assert progress_policy_from_payload(None) is None
    with pytest.raises(ValueError, match="exactly"):
        progress_policy_from_payload({})
    values = asdict(HybridProgressPolicy(2, 0.01, 0.01, 2))
    values["extra"] = 1
    with pytest.raises(ValueError, match="exactly"):
        progress_policy_from_payload(values)


def test_tradeoff_requires_complete_accepted_window():
    monitor = AcceptedProgressMonitor(HybridProgressPolicy(2, 0.01, 0.01, 2), sample(0))
    assert monitor.observe(sample(1, 0.5, 1.1)) is None
    decision = monitor.observe(sample(2, 0.4, 1.2))
    assert decision is not None
    assert decision.reason == "sse_worst_tradeoff"
    assert decision.trigger.accepted_iteration == 2
    assert decision.trigger.physical_hash == "physical-2"
    with pytest.raises(ValueError, match="after a handoff"):
        monitor.observe(sample(3))


def test_two_nonoverlapping_stagnation_windows_not_single_stale_sample():
    monitor = AcceptedProgressMonitor(HybridProgressPolicy(2, 0.0, 0.0, 2), sample(0))
    assert all(monitor.observe(sample(i)) is None for i in (1, 2, 3))
    decision = monitor.observe(sample(4))
    assert decision is not None
    assert decision.reason == "declared_stagnation" and decision.completed_windows == 2
    assert decision.baseline.accepted_iteration == 2


def test_both_metrics_improve_or_nominal_pass_never_triggers():
    policy = HybridProgressPolicy(1, 0.0, 0.0, 1)
    monitor = AcceptedProgressMonitor(policy, sample(0))
    assert monitor.observe(sample(1, 0.5, 0.5)) is None
    assert monitor.observe(sample(2, 0.1, 0.0, passed=True)) is None
    assert monitor.observe(sample(3, 0.1, 0.0, passed=True)) is None


def test_zero_worst_is_not_inferred_nominal_pass():
    monitor = AcceptedProgressMonitor(HybridProgressPolicy(1, 0.0, 0.0, 1), sample(0, 0.0, 0.0))
    decision = monitor.observe(sample(1, 0.0, 0.0))
    assert decision is not None and decision.reason == "declared_stagnation"


def test_zero_probe_resets_window_then_positive_resume_counts_fresh_window():
    monitor = AcceptedProgressMonitor(HybridProgressPolicy(2, 0.0, 0.0, 1), sample(0))
    assert monitor.observe(sample(1)) is None
    assert monitor.observe(sample(2, thickness=0.0)) is None
    assert monitor.summary()["last_reset"]["reason"] == "minimax_receiver_zero_thickness"
    assert monitor.observe(sample(3, thickness=np.finfo(float).tiny)) is None
    assert monitor.observe(sample(4)) is None
    decision = monitor.observe(sample(5))
    assert decision is not None and decision.reason == "declared_stagnation"
    assert decision.baseline.accepted_iteration == 3


def test_missing_nominal_or_nonconsecutive_samples_rejected():
    with pytest.raises(ValueError, match="nominal_pass"):
        sample(0, passed=None)
    monitor = AcceptedProgressMonitor(HybridProgressPolicy(1, 0.0, 0.0, 1), sample(0))
    with pytest.raises(ValueError, match="consecutive"):
        monitor.observe(sample(2))


def refinement_problem():
    _, request = setup_problem()
    objective = FilmOptimizationObjective(
        500.0,
        501.0,
        2,
        angles_deg=(0.0,),
        metric="R",
        polarization="s",
        mean=FilmObjectiveConstraint(enabled=False),
        pointwise=FilmObjectiveConstraint(mode="range", lower_bound=0.9, upper_bound=None),
    )
    request = replace(request, objectives=(objective,))
    design = deepcopy(request.environment)
    design["front_layers"] = [
        dict(
            id="x",
            material_id="high",
            optical_set_id="nk",
            thickness=dict(value=100.0, unit="nm"),
            note="",
        )
    ]

    def value(d):
        responses = [
            dict(
                objective_index=0,
                angle_deg=0.0,
                wavelength_nm=np.array([500.0, 501.0]),
                actual_values=np.array([0.5, 0.5]),
                jacobian_values_by_layer_id={"x": np.array([0.001, 0.001])},
            )
        ]
        reduced = reduce_objective_responses(request.objectives, responses, layer_ids=("x",))
        return dict(
            design=deepcopy(d),
            responses=responses,
            score=dict(
                nominal_pass=reduced.nominal_pass,
                max_error=reduced.max_error,
                weighted_rms_error=reduced.weighted_rms_error,
                errors=reduced.conditions,
            ),
        )

    return request, design, value


def test_handoff_commits_trial_and_accepted_event_before_exit(monkeypatch):
    request, design, value = refinement_problem()
    events = []
    calls = []

    def evaluate(d, **kw):
        calls.append(1)
        return value(d)

    def solver(fun, x0, **kw):
        assert kw["options"]["ftol"] == 1e-13 and kw["options"]["gtol"] == 1e-10
        # Unaccepted trial alone must not trigger even with a one-step window.
        fun(np.array([0.8]))
        fun(np.array([0.9]))
        kw["callback"](np.array([0.9]))
        pytest.fail("handoff must stop further SSE steps")

    monkeypatch.setattr("scipy.optimize.minimize", solver)
    terminal, diag = refine_synthesis_explorer(
        request,
        value(design),
        evaluate=evaluate,
        archive=lambda *_a, **_k: events.append("archive"),
        progress=lambda stage, *_: events.append(stage),
        progress_policy=HybridProgressPolicy(1, 0.0, 0.0, 1),
    )
    assert len(calls) == 2 and terminal["design"]["front_layers"][0]["thickness"]["value"] == 90.0
    assert events[-1] == "hybrid_sse_accepted_iterate"
    assert events.count("archive") == 2
    assert diag["termination"] == "objective_progress_handoff"
    decision = diag["objective_progress"]["decision"]
    assert decision["trigger"]["physical_hash"] == physical_design_hash(terminal["design"])
    assert decision["trigger"]["accepted_iteration"] == 1


def test_real_cancellation_at_trigger_wins_without_more_optics(monkeypatch):
    request, design, value = refinement_problem()
    cancel = [False]
    calls = []

    class Cancelled(Exception):
        pass

    def check():
        if cancel[0]:
            raise Cancelled()

    def progress(stage, *_):
        if stage == "hybrid_sse_accepted_iterate":
            cancel[0] = True

    def solver(fun, x0, **kw):
        fun(np.array([0.9]))
        kw["callback"](np.array([0.9]))
        pytest.fail("no return expected")

    monkeypatch.setattr("scipy.optimize.minimize", solver)

    def evaluate(d, **kwargs):
        calls.append(1)
        return value(d)

    with pytest.raises(Cancelled):
        refine_synthesis_explorer(
            request,
            value(design),
            evaluate=evaluate,
            archive=lambda *_a, **_k: None,
            cancel_check=check,
            progress=progress,
            progress_policy=HybridProgressPolicy(1, 0.0, 0.0, 1),
        )
    assert calls == [1]


def test_incomplete_penalty_callback_resets_window_without_false_acceptance(monkeypatch):
    request, design, value = refinement_problem()
    request = replace(request, max_total_thickness_nm=150.0)

    def solver(fun, x0, **kwargs):
        fun(np.array([0.9]))
        kwargs["callback"](np.array([0.9]))
        # Outside total constraint: no physical evidence, not an accepted sample.
        fun(np.array([2.0]))
        kwargs["callback"](np.array([2.0]))
        fun(np.array([0.8]))
        kwargs["callback"](np.array([0.8]))
        return SimpleNamespace(
            x=np.array([0.8]), success=False, status=9, nit=2, message="bounded test"
        )

    monkeypatch.setattr("scipy.optimize.minimize", solver)
    calls = []

    def evaluate(d, **kwargs):
        calls.append(1)
        return value(d)

    _, diagnostics = refine_synthesis_explorer(
        request,
        value(design),
        evaluate=evaluate,
        archive=lambda *a, **k: None,
        progress_policy=HybridProgressPolicy(2, 0.0, 0.0, 1),
    )
    summary = diagnostics["objective_progress"]
    assert len(calls) == 2 and summary["accepted_samples"] == 2
    assert summary["last_reset"]["reason"] == "incomplete_accepted_evidence"
    assert summary["completed_windows"] == 0 and summary["decision"] is None


def test_local_slice_expiry_after_decision_preserves_handoff_not_deletion_eligibility(monkeypatch):
    from coat_sim.core.film_synthesis_refinement import SynthesisStageBudget

    request, design, value = refinement_problem()
    expired = [False]

    def check():
        if expired[0]:
            raise SynthesisStageBudget("joint_needle_sse_growth")

    def progress(stage, *_):
        if stage == "hybrid_sse_accepted_iterate":
            expired[0] = True

    def solver(fun, x0, **kwargs):
        fun(x0)
        kwargs["callback"](x0)
        pytest.fail("decision terminates SSE")

    monkeypatch.setattr("scipy.optimize.minimize", solver)
    endpoint, diagnostics = refine_synthesis_explorer(
        request,
        value(design),
        evaluate=lambda *a, **k: pytest.fail("cached callback needs no optics"),
        archive=lambda *a, **k: None,
        cancel_check=check,
        progress=progress,
        progress_policy=HybridProgressPolicy(1, 0.0, 0.0, 1),
    )
    assert diagnostics["termination"] == "objective_progress_handoff"
    assert diagnostics["objective_progress"]["decision"]["reason"] == "declared_stagnation"
    assert endpoint["design"] == design


def test_positive_subnormal_outside_native_receiver_resets_but_tiny_supported_positive_does_not():
    monitor = AcceptedProgressMonitor(HybridProgressPolicy(1, 0.0, 0.0, 1), sample(0))
    assert monitor.observe(sample(1, thickness=1e-310)) is None
    assert monitor.summary()["last_reset"]["reason"] == "minimax_receiver_lower_bound"
    assert monitor.observe(sample(2, thickness=1e-12)) is None
    decision = monitor.observe(sample(3, thickness=1e-12))
    assert decision is not None and decision.reason == "declared_stagnation"


def test_global_evaluation_limit_after_trigger_is_not_swallowed_as_local_handoff(monkeypatch):
    from coat_sim.core.film_auto_synthesis import _Stopped

    request, design, value = refinement_problem()
    exhausted = [False]

    def check():
        if exhausted[0]:
            raise _Stopped("evaluation_limit")

    def progress(stage, *_):
        if stage == "hybrid_sse_accepted_iterate":
            exhausted[0] = True

    def solver(fun, x0, **kwargs):
        fun(x0)
        kwargs["callback"](x0)
        pytest.fail("global stop must propagate")

    monkeypatch.setattr("scipy.optimize.minimize", solver)
    with pytest.raises(_Stopped, match="evaluation_limit"):
        refine_synthesis_explorer(
            request,
            value(design),
            evaluate=lambda *a, **k: pytest.fail("no extra optics"),
            archive=lambda *a, **k: None,
            cancel_check=check,
            progress=progress,
            progress_policy=HybridProgressPolicy(1, 0.0, 0.0, 1),
        )


def test_observer_exception_has_priority_over_routing(monkeypatch):
    request, design, value = refinement_problem()

    class ObserverError(Exception):
        pass

    def progress(stage, *_):
        if stage == "hybrid_sse_accepted_iterate":
            raise ObserverError("consumer failed")

    def solver(fun, x0, **kwargs):
        fun(x0)
        kwargs["callback"](x0)
        pytest.fail("observer error must propagate")

    monkeypatch.setattr("scipy.optimize.minimize", solver)
    with pytest.raises(ObserverError, match="consumer failed"):
        refine_synthesis_explorer(
            request,
            value(design),
            evaluate=lambda *a, **k: pytest.fail("no extra optics"),
            archive=lambda *a, **k: None,
            progress=progress,
            progress_policy=HybridProgressPolicy(1, 0.0, 0.0, 1),
        )


def test_last_admitted_accepted_point_is_preserved_but_not_observed_after_local_expiry(monkeypatch):
    from coat_sim.core.film_synthesis_refinement import SynthesisStageBudget

    request, design, value = refinement_problem()
    expired = [False]
    events = []

    def check():
        if expired[0]:
            raise SynthesisStageBudget("joint_needle_sse_growth")

    def progress(stage, *_):
        events.append(stage)
        if stage == "hybrid_sse_trial":
            expired[0] = True

    def solver(fun, x0, **kwargs):
        fun(np.array([0.9]))
        kwargs["callback"](np.array([0.9]))
        pytest.fail("local expiry stops SSE")

    monkeypatch.setattr("scipy.optimize.minimize", solver)
    archived = []
    endpoint, diagnostics = refine_synthesis_explorer(
        request,
        value(design),
        evaluate=lambda design, **kwargs: value(design),
        archive=lambda candidate, **kwargs: archived.append(candidate),
        cancel_check=check,
        progress=progress,
        progress_policy=HybridProgressPolicy(1, 0.0, 0.0, 1),
    )
    assert endpoint["design"]["front_layers"][0]["thickness"]["value"] == 90.0
    assert archived and endpoint is archived[-1]
    assert diagnostics["termination"] == "stage_budget" and diagnostics["iterations"] == 1
    summary = diagnostics["objective_progress"]
    assert summary["accepted_samples"] == 0 and summary["decision"] is None
    assert summary["completed_windows"] == 0
    assert "hybrid_sse_accepted_iterate" not in events
