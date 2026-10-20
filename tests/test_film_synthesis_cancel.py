from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_audit as audit
from coat_sim.core import film_synthesis_parallel as parallel
from coat_sim.core import film_optimization_structure as physical
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


@pytest.mark.parametrize("workers", [1, 2])
def test_cancel_retains_latest_committed_training_best_without_reusing_old_audit(monkeypatch, workers):
    monkeypatch.setattr(parallel, "MIN_PARALLEL_WORK", 0)
    service, request = setup_problem(upper=.03)
    request = replace(request, max_evaluations=None, cpu_workers=workers,
                      seed_designs=(deepcopy(request.environment),))
    event = Event()
    original_audit = audit.audit_frozen_synthesis_candidate
    audit_designs = []
    snapshots = []

    def audited(*args, **kwargs):
        assert not event.is_set(), "cancel must never start another physical audit"
        audit_designs.append(deepcopy(args[1]))
        return original_audit(*args, **kwargs)

    def progress(value):
        if value.stage == "zero_layer_trial":
            event.set()  # The finite raw score was already submitted by parent.

    monkeypatch.setattr(audit, "audit_frozen_synthesis_candidate", audited)
    result = synthesis.synthesize_film_design(service, request, cancel_event=event,
        progress_callback=progress, snapshot_callback=lambda *values: snapshots.append(values))
    assert result.state == "cancelled" and not result.targets_met
    assert result.candidate_design is not None and len(result.candidate_design["front_layers"]) == 1
    assert result.training_score is not None and result.training_score["max_error"] == 0
    assert result.validation_score is None and not result.validation_complete
    assert len(audit_designs) == 1 and audit_designs[0]["front_layers"] == []
    assert snapshots and snapshots[0][0]["front_layers"] == []
    assert result.candidate_provenance == {
        "selection": "completed_training_at_cancel", "training_complete": True,
        "frozen_at_cancel": True, "fully_scored_evaluations": 3,
        "validation_matches_candidate": False}
    assert result.performance["cleanup_complete"]
    if workers == 2:
        assert result.performance["discarded_tasks"] > 0
    print("cancel committed raw", workers, result.evaluations, result.candidate_provenance,
          result.training_score["max_error"], result.performance)


def test_cancel_reuses_only_completed_audit_of_the_same_candidate():
    service, request = setup_problem(upper=.05)
    event = Event()

    def progress(value):
        if value.stage == "validation_complete":
            event.set()

    result = synthesis.synthesize_film_design(service, request, cancel_event=event, progress_callback=progress)
    assert result.state == "cancelled" and not result.targets_met
    assert result.candidate_design is not None and result.candidate_design["front_layers"] == []
    assert result.validation_complete and result.validation_score is not None
    assert result.candidate_provenance["validation_matches_candidate"] is True
    assert result.candidate_provenance["fully_scored_evaluations"] == 1
    print("same candidate audit", result.evaluations, result.candidate_provenance)


def test_cancel_before_first_complete_score_has_no_candidate():
    service, request = setup_problem()
    event = Event()
    event.set()
    result = synthesis.synthesize_film_design(service, request, cancel_event=event)
    assert result.state == "cancelled" and result.candidate_design is None
    assert result.training_score is None and result.validation_score is None
    assert result.evaluations == 0
    assert result.candidate_provenance["fully_scored_evaluations"] == 0


def test_cancel_after_physical_return_but_before_parent_commit_excludes_that_value(monkeypatch):
    service, request = setup_problem(upper=.03)
    request = replace(request, seed_designs=(deepcopy(request.environment),))
    event = Event()
    original = synthesis.evaluate_frozen_structure_candidate
    rejected = []

    def physical(*args, **kwargs):
        value = original(*args, **kwargs)
        if args[1]["front_layers"] and all(row["thickness"]["value"] > 0 for row in args[1]["front_layers"]):
            rejected.append(deepcopy(value["design"]))
            event.set()  # Cancellation wins the parent submission boundary.
        return value

    monkeypatch.setattr(synthesis, "evaluate_frozen_structure_candidate", physical)
    result = synthesis.synthesize_film_design(service, request, cancel_event=event)
    assert rejected and result.state == "cancelled"
    assert result.candidate_design is not None and result.candidate_design["front_layers"] == []
    assert result.candidate_provenance["fully_scored_evaluations"] == 2
    assert result.candidate_provenance["validation_matches_candidate"] is True
    assert result.validation_complete
    print("rejected uncommitted raw", result.evaluations, result.candidate_provenance)


def test_cancel_between_objectives_never_promotes_a_partial_score(monkeypatch):
    service, request = setup_problem(upper=.03)
    request = replace(request, objectives=(*request.objectives, replace(request.objectives[0],
        wavelength_min_nm=600., wavelength_max_nm=620.)),
        seed_designs=(deepcopy(request.environment),))
    event = Event()
    original = physical.simulate_with_thickness_jacobian
    partial_calls = []

    def helper(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0]["front_layers"] and all(row["thickness"]["value"] > 0 for row in args[0]["front_layers"]):
            partial_calls.append(result)
            event.set()  # One objective is complete; the remaining one is not.
        return result

    monkeypatch.setattr(physical, "simulate_with_thickness_jacobian", helper)
    result = synthesis.synthesize_film_design(service, request, cancel_event=event)
    assert partial_calls and result.state == "cancelled"
    assert result.candidate_design is not None and result.candidate_design["front_layers"] == []
    assert result.candidate_provenance["fully_scored_evaluations"] == 2
    assert result.candidate_provenance["validation_matches_candidate"] is True
    assert result.validation_complete
    print("partial score excluded", result.evaluations, result.candidate_provenance)
