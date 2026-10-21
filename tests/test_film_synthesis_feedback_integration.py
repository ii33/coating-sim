"""A narrow synthetic optical notch is feedback, never an acceptance shortcut."""
from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def notch_problem(*, max_evaluations=80):
    service, base = setup_problem(low=450., high=700., samples=2, materials=("high",))
    optical = service.materials.records["glass"]["optical_sets"][0]
    optical.update(wavelengths=[450., 574.8, 575., 575.2, 700.],
                   n=[3., 3., 1., 3., 3.], k=[0.] * 5)
    objective = replace(base.objectives[0], upper_bound=None, lower_bound=.05,
                        angles_deg=(0., 20.), polarization="s")
    seed = deepcopy(base.environment)
    seed["front_layers"] = [dict(id="notch-seed", material_id="high", optical_set_id="nk",
                                  thickness=dict(value=5., unit="nm"), note="synthetic seed")]
    request = replace(base, objectives=(objective, replace(objective, polarization="p")),
        seed_designs=(seed,), max_cycles=1, max_evaluations=max_evaluations, max_layers=2,
        minimum_nonzero_thickness_nm=1., max_layer_thickness_nm=200.,
        max_total_thickness_nm=300.)
    return service, request


def test_narrow_notch_repair_preserves_original_training_and_best_audited_candidate():
    service, request = notch_problem()
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    bare = audit_frozen_synthesis_candidate(frozen, frozen.environment)
    assert bare["score"]["max_error"] > .049
    result = synthesis.synthesize_film_design(service, frozen)
    assert result.state == "completed" and result.targets_met, result.termination_reason
    assert result.validation_score is not None and result.training_score is not None
    assert result.training_score["max_error"] == 0
    assert result.validation_score["max_error"] == 0
    assert len(result.training_score["errors"]) == 8  # Original 2 waves x 2 angles x 2 pols.
    assert result.frozen_request.objectives == request.objectives
    assert result.candidate_design is not None
    assert result.candidate_design["front_layers"][0]["thickness"]["value"] > 5.
    # The zero-layer training champion has smaller manufacturing cost, but
    # cannot displace the later physically audited repair at normal completion.
    assert result.trace[0].stage == "freezing"
    assert any(row.stage == "audit_feedback_repair" for row in result.trace)
    feedback = result.performance["audit_feedback"]
    assert feedback["repair_stages"] >= 1 and feedback["local_stages"] >= 1
    assert feedback["physical_evaluations"] > 0 and feedback["wavelength_points"] > 0
    assert {point["objective_index"] for point in feedback["points"]} == {0, 1}
    assert {point["angle_deg"] for point in feedback["points"]} == {0., 20.}
    assert all(450. < point["wavelength_nm"] < 700. for point in feedback["points"])
    assert result.performance["cleanup_complete"]


def test_exhausted_budget_keeps_audit_without_starting_feedback_work():
    service, request = notch_problem(max_evaluations=4)
    result = synthesis.synthesize_film_design(service, request)
    assert result.state == "not_found" and not result.targets_met
    assert result.evaluations <= 4 and result.validation_complete
    assert result.performance["audit_feedback"]["parent_evaluator_invocations"] == 0
    assert result.performance["audit_feedback"]["repair_stages"] == 0
    assert result.performance["cleanup_complete"]


def test_cancel_during_feedback_keeps_completed_training_and_no_partial_guide(monkeypatch):
    service, request = notch_problem()
    event = Event()
    original = synthesis.evaluate_frozen_structure_candidate
    def observed(proxy, design, **kwargs):
        if kwargs.get("with_jacobian") and all(
                objective.wavelength_min_nm == objective.wavelength_max_nm
                for objective in proxy.base_request.objectives):
            event.set()
        return original(proxy, design, **kwargs)
    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", observed)
    result = synthesis.synthesize_film_design(service, request, cancel_event=event)
    assert event.is_set() and result.state == "cancelled" and not result.targets_met
    assert result.training_score is not None
    assert len(result.training_score["errors"]) == 8
    assert result.candidate_provenance["selection"] == "completed_training_at_cancel"
    assert result.performance["audit_feedback"]["parent_evaluator_invocations"] == 0
    assert result.performance["serial_unknown_work"] >= 1
    assert result.performance["cleanup_complete"]


def test_deadline_during_feedback_keeps_completed_base_without_new_final_audit(monkeypatch):
    service, request = notch_problem()
    clock = [0.]
    monkeypatch.setattr(synthesis, "monotonic", lambda: clock[0])
    original = synthesis.evaluate_frozen_structure_candidate
    def observed(proxy, design, **kwargs):
        if kwargs.get("with_jacobian") and all(
                objective.wavelength_min_nm == objective.wavelength_max_nm
                for objective in proxy.base_request.objectives):
            clock[0] = 2.
        return original(proxy, design, **kwargs)
    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", observed)
    result = synthesis.synthesize_film_design(service, replace(request, max_seconds=1.))
    assert result.state == "not_found" and result.termination_reason == "time_limit"
    assert not result.targets_met and result.training_score is not None
    assert len(result.training_score["errors"]) == 8
    assert result.candidate_provenance["selection"] == "completed_training_at_time_limit"
    assert result.performance["audit_feedback"]["parent_evaluator_invocations"] == 0
    assert result.performance["serial_unknown_work"] >= 1
    assert result.performance["cleanup_complete"]


def test_new_feedback_snapshot_never_reuses_previous_local_cache(monkeypatch):
    from types import SimpleNamespace
    from importlib import import_module
    from coat_sim.core.film_synthesis_feedback import (
        SparseFeedbackRegistry, augment_feedback_guide, feedback_candidate_key,
    )
    from coat_sim.core.film_synthesis_refinement import refine_synthesis_candidate
    from tests.test_film_synthesis_feedback import request_fixture, base_fixture, error_for
    request = request_fixture()
    base = base_fixture(request)
    base["design"]["front_layers"][0].update(material_id="low", optical_set_id="nk",
        thickness=dict(value=100., unit="nm"))
    registry = SparseFeedbackRegistry(request)
    calls: list[tuple[int, tuple[float, ...]]] = []
    def minimize(fun, point, **_kwargs):
        first = fun(point)
        second = fun(point)
        assert first[0] == second[0]
        assert (first[1] == second[1]).all()
        return SimpleNamespace(message="same-point cache probe", nit=0)
    monkeypatch.setattr(import_module("scipy.optimize"), "minimize", minimize)
    losses: list[float] = []
    for wave in (575., 525.):
        assert registry.record_audit({"errors": (error_for(request, 0, wave, .4),)}) == 1
        snapshot = registry.snapshot()
        before = len(calls)
        def extra(objectives, snapshot=snapshot):
            calls.append((snapshot.revision, tuple(o.wavelength_min_nm for o in objectives)))
            local = replace(request, objectives=objectives)
            errors = tuple(error_for(local, i, o.wavelength_min_nm, .4, o.angles_deg[0])
                           for i, o in enumerate(objectives))
            responses = tuple(dict(objective_index=i, angle_deg=o.angles_deg[0],
                wavelength_nm=[o.wavelength_min_nm], actual_values=[.4],
                jacobian_values_by_layer_id={"layer": [0.]}) for i, o in enumerate(objectives))
            return dict(design=base["design"], score=dict(errors=errors), responses=responses)
        def guided(_design, _snapshot=snapshot, _extra=extra, **_kwargs):
            return augment_feedback_guide(request, base, _snapshot, evaluate_points=_extra)
        initial = guided(base["design"])
        _, details = refine_synthesis_candidate(request, initial, evaluate=guided,
            archive=lambda *_args, **_kwargs: None,
            quarter_nm={("low", "nk"): 100.}, max_iterations=2,
            candidate_key=feedback_candidate_key)
        assert len(calls) - before == 2  # Initial + once per invocation, never the cache probes.
        losses.append(float(details["interior_loss"]))
    assert calls[-1][1] == (525., 575.)
    assert losses[1] > losses[0]


def test_cancel_after_completed_feedback_retains_work_without_publishing_guide(monkeypatch):
    service, request = notch_problem()
    event = Event()
    original = synthesis.evaluate_frozen_structure_candidate
    def observed(proxy, design, **kwargs):
        value = original(proxy, design, **kwargs)
        if kwargs.get("with_jacobian") and all(
                objective.wavelength_min_nm == objective.wavelength_max_nm
                for objective in proxy.base_request.objectives):
            event.set()
        return value
    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", observed)
    result = synthesis.synthesize_film_design(service, request, cancel_event=event)
    assert result.state == "cancelled" and not result.targets_met
    assert result.training_score is not None and len(result.training_score["errors"]) == 8
    work = result.performance["audit_feedback"]
    assert work["parent_evaluator_invocations"] == 1
    assert work["physical_evaluations"] > 0 and work["wavelength_points"] > 0
    assert result.performance["serial_unknown_work"] == 0
    assert result.candidate_provenance["selection"] == "completed_training_at_cancel"
    assert result.performance["cleanup_complete"]
