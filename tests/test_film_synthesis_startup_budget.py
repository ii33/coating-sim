"""Cycle-zero preparation and physical work share explicit user budgets."""
from copy import deepcopy
from dataclasses import fields, replace
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_global as global_search
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def reflector_request(**changes):
    service, request = setup_problem(low=550., high=550., samples=1)
    objective = replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, lower_bound=.999999, upper_bound=1.))
    return service, replace(request, objectives=(objective,), max_cycles=1,
                            max_evaluations=None, **changes)


def prior(request):
    design = deepcopy(request.environment)
    design["front_layers"] = [dict(id="startup-budget-prior", material_id="high",
        optical_set_id="nk", thickness=dict(value=62.5, unit="nm"), note="")]
    return global_search.StructuralProposal(design,
        dict(family="periodic", high=("high", "nk")))


@pytest.mark.parametrize("value", [True, False, 0., -1., float("nan"), float("inf"), -float("inf")])
def test_wall_budget_rejects_nonpositive_nonfinite_and_boolean_values(value):
    _, request = setup_problem()
    with pytest.raises(ValueError, match="max_seconds"):
        synthesis._validated(replace(request, max_seconds=value))


def test_wall_budget_is_optional_float_and_appended_to_request():
    _, request = setup_problem()
    assert request.max_seconds is None
    names = [item.name for item in fields(type(request))]
    assert names.index("max_seconds") == names.index("ar_structure_mode") + 1
    assert synthesis._validated(replace(request, max_seconds=2)).max_seconds == 2.


def test_cycle_zero_does_not_generate_priors_after_training_budget_is_spent(monkeypatch):
    service, request = reflector_request()
    calls = []

    def proposals(*args, **kwargs):
        calls.append("unexpected proposal preparation")
        raise AssertionError("No training slot remains before proposal preparation")
        yield

    monkeypatch.setattr(global_search, "iter_reflector_proposals", proposals)
    # One complete bare training score plus the reserved final audit fills
    # this budget; a failed bare design no longer has a pre-search audit.
    result = synthesis.synthesize_film_design(service, replace(request, max_evaluations=2))
    assert calls == []
    assert result.termination_reason == "evaluation_limit"
    assert result.state == "not_found" and not result.targets_met
    assert result.cycles == 0 and result.evaluations == 2
    assert result.validation_complete and result.validation_evaluations == 1
    assert result.performance["cleanup_complete"]


@pytest.mark.parametrize("stop", ["deadline", "cancel", "both"])
def test_all_skipped_lazy_startup_proposals_observe_stop_without_physical_work(monkeypatch, stop):
    service, request = reflector_request(max_seconds=1.)
    clock = [0.]
    monkeypatch.setattr(synthesis, "monotonic", lambda: clock[0])
    cancelled = Event()
    scans = []

    def proposals(frozen, *, cancel_check, **kwargs):
        for index in range(10):
            scans.append(index)
            if stop in ("deadline", "both"):
                clock[0] = 2.
            if stop in ("cancel", "both"):
                cancelled.set()
            cancel_check()
        yield prior(frozen)

    monkeypatch.setattr(global_search, "iter_reflector_proposals", proposals)
    result = synthesis.synthesize_film_design(service, request, cancel_event=cancelled)
    assert scans == [0]
    assert result.cycles == 0 and result.evaluations == 1
    assert result.state == ("not_found" if stop == "deadline" else "cancelled")
    assert result.termination_reason == ("time_limit" if stop == "deadline" else "cancelled")
    assert result.candidate_design is not None and result.candidate_design["front_layers"] == []
    assert not result.validation_complete and result.validation_evaluations == 0
    assert result.training_score is not None and result.validation_score is None
    assert result.training_score["design"] == result.candidate_design
    assert result.candidate_provenance["validation_matches_candidate"] is False
    assert result.candidate_provenance["selection"] == (
        "completed_training_at_time_limit" if stop == "deadline" else "completed_training_at_cancel")
    assert result.performance["cleanup_complete"]


def test_time_limit_retains_unmatched_complete_training_without_starting_an_audit(monkeypatch):
    service, request = reflector_request(max_seconds=1.)
    clock = [0.]
    monkeypatch.setattr(synthesis, "monotonic", lambda: clock[0])
    scans = []
    snapshots = []
    stages = []

    def observe(item):
        stages.append(item.stage)
        if item.stage == "structure_proposal":
            clock[0] = 2.

    # The native serial chunk has two candidates. The first complete score is
    # committed before the observer advances the clock; the second is rejected.
    def two_proposals(frozen, *, cancel_check, **kwargs):
        scans.append("generated")
        cancel_check()
        yield prior(frozen)
        yield prior(frozen)

    monkeypatch.setattr(global_search, "iter_reflector_proposals", two_proposals)
    result = synthesis.synthesize_film_design(service, request,
        progress_callback=observe, snapshot_callback=lambda *args: snapshots.append(args))
    assert scans == ["generated"]
    assert result.state == "not_found" and result.termination_reason == "time_limit"
    assert not result.targets_met and result.cycles == 0
    assert result.training_score is not None and result.candidate_design is not None
    assert len(result.candidate_design["front_layers"]) == 1
    assert result.validation_score is None and not result.validation_complete
    assert result.validation_evaluations == 0 and stages.count("validation") == 0
    assert snapshots == []  # No unqualified bare checkpoint or post-deadline audit.
    assert result.candidate_provenance["validation_matches_candidate"] is False
    assert result.candidate_provenance["frozen_at_time_limit"] is True
    assert "frozen_at_cancel" not in result.candidate_provenance
    assert result.performance["cleanup_complete"]


def test_wall_deadline_is_shared_with_freeze_and_starts_at_entry(monkeypatch):
    service, request = setup_problem(upper=.05)
    request = replace(request, max_seconds=1.)
    clock = [0.]
    monkeypatch.setattr(synthesis, "monotonic", lambda: clock[0])
    original = synthesis.freeze_film_synthesis_request
    observed = []

    def freeze(service, request, *, cancel_event):
        observed.append(cancel_event)
        clock[0] = 2.
        return original(service, request, cancel_event=cancel_event)

    monkeypatch.setattr(synthesis, "freeze_film_synthesis_request", freeze)
    result = synthesis.synthesize_film_design(service, request)
    assert len(observed) == 1 and observed[0].is_set()
    assert result.state == "not_found" and result.termination_reason == "time_limit"
    assert result.evaluations == 0 and result.candidate_design is None
    assert result.validation_score is None and not result.validation_complete
    assert result.performance["cleanup_complete"]


def test_one_cycle_keeps_startup_priors_and_respects_explicit_evaluation_budget(monkeypatch):
    service, request = reflector_request()
    clock = [0.]
    stages = []
    generated = []
    monkeypatch.setattr(synthesis, "monotonic", lambda: clock[0])

    def proposals(frozen, *, cancel_check, **kwargs):
        cancel_check()
        generated.append("prior")
        yield prior(frozen)

    from coat_sim.core import film_synthesis_refinement
    monkeypatch.setattr(global_search, "iter_reflector_proposals", proposals)
    monkeypatch.setattr(film_synthesis_refinement, "refine_synthesis_candidate",
        lambda _request, value, **kwargs: (value, {"method": "budget fixture"}))
    result = synthesis.synthesize_film_design(service, replace(request, max_evaluations=4),
        progress_callback=lambda item: stages.append(item.stage))
    assert generated == ["prior"] and "structure_proposal" in stages
    assert not any("initial_population" in stage for stage in stages)
    assert result.evaluations <= 4 and result.cycles == 0
    assert result.termination_reason == "evaluation_limit"
    assert result.performance["cleanup_complete"]



def test_global_population_preparation_observes_the_same_deadline(monkeypatch):
    service, request = reflector_request(max_seconds=1.)
    clock = [0.]
    stages = []
    monkeypatch.setattr(synthesis, "monotonic", lambda: clock[0])

    def proposals(frozen, *, cancel_check, **kwargs):
        cancel_check()
        yield prior(frozen)

    from coat_sim.core import film_synthesis_refinement
    monkeypatch.setattr(global_search, "iter_reflector_proposals", proposals)
    monkeypatch.setattr(film_synthesis_refinement, "refine_synthesis_candidate",
        lambda _request, value, **kwargs: (value, {"method": "budget fixture"}))
    class DeadlineAtPopulation(global_search.DifferentialEvolutionProposals):
        def proposals(self):
            points = super().proposals()
            clock[0] = 2.
            return points

    monkeypatch.setattr(global_search, "DifferentialEvolutionProposals", DeadlineAtPopulation)
    result = synthesis.synthesize_film_design(service, request,
        progress_callback=lambda item: stages.append(item.stage))
    assert "structure_proposal" in stages and "interior_local_complete" in stages
    assert "structure_global_initial_population" not in stages
    assert result.state == "not_found" and result.termination_reason == "time_limit"
    assert result.evaluations == 3 and result.validation_evaluations == 1
    assert result.validation_complete
    assert result.training_score is not None and result.validation_score is not None
    assert result.training_score["design"] == result.validation_score["design"]
    assert result.performance["search"]["global_search"][0]["stop_reason"] == "time_limit"
    assert result.performance["cleanup_complete"]


def test_time_spent_validating_expires_before_startup_observers(monkeypatch):
    service, request = setup_problem(upper=.05)
    request = replace(request, max_seconds=1.)
    clock = [0.]
    monkeypatch.setattr(synthesis, "monotonic", lambda: clock[0])
    validate = synthesis._validated
    stages = []

    def elapsed_validation(request):
        result = validate(request)
        clock[0] = 2.
        return result

    monkeypatch.setattr(synthesis, "_validated", elapsed_validation)
    result = synthesis.synthesize_film_design(service, request,
        progress_callback=lambda item: stages.append(item.stage))
    assert stages == ["not_found"]
    assert result.termination_reason == "time_limit" and result.evaluations == 0
    assert result.candidate_design is None and result.training_score is None


def test_deadline_reaches_owned_worker_startup_before_any_submission(monkeypatch):
    from coat_sim.core import film_synthesis_parallel

    service, request = reflector_request(max_seconds=1.)
    request = replace(request, cpu_workers=2)
    clock = [0.]
    startups = []
    monkeypatch.setattr(synthesis, "monotonic", lambda: clock[0])
    monkeypatch.setattr(film_synthesis_parallel, "MIN_PARALLEL_WORK", 0)

    def proposals(frozen, *, cancel_check, **kwargs):
        cancel_check()
        yield prior(frozen)
        yield prior(frozen)

    def expired_startup(executor, count=None):
        startups.append(count)
        clock[0] = 2.
        executor.check_cancel()
        raise AssertionError("Expired startup must not submit a worker task")

    monkeypatch.setattr(global_search, "iter_reflector_proposals", proposals)
    monkeypatch.setattr(film_synthesis_parallel.OrderedEvaluationExecutor,
                        "_ensure_pool", expired_startup)
    result = synthesis.synthesize_film_design(service, request)
    assert startups == [2]
    assert result.state == "not_found" and result.termination_reason == "time_limit"
    assert result.evaluations == 1 and not result.validation_complete
    assert result.validation_evaluations == 0 and result.validation_score is None
    assert result.candidate_provenance["validation_matches_candidate"] is False
    assert result.performance["submitted_tasks"] == 0
    assert result.performance["cleanup_complete"]
