"""Insertion exhaustion must not exhaust independent continuous/structure search."""
from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_global as global_search
from coat_sim.core import film_synthesis_refinement as refinement
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


@pytest.mark.parametrize("termination", ["cycle_limit", "cancel"])
def test_full_layer_branches_keep_structural_search_until_explicit_boundary(monkeypatch, termination):
    service, request = setup_problem(low=550., high=550., samples=1)
    request = replace(request, objectives=(replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, lower_bound=.99999, upper_bound=None)),), max_layers=1, max_cycles=7,
        max_evaluations=None, cpu_workers=1, position_samples=1)
    scans = []

    def proposals(frozen, *, epoch=0, **_kwargs):
        scans.append(epoch)
        design = deepcopy(frozen.environment)
        design["front_layers"] = [dict(id=f"schedule-prior-{epoch}", material_id="high",
            optical_set_id="nk", thickness=dict(value=55., unit="nm"), note="")]
        yield global_search.StructuralProposal(design,
            dict(family="periodic", high=("high", "nk")))

    # Keep a real population and real optical evaluation, but shrink this
    # scheduling fixture's population and omit unrelated local optimization.
    evolution = global_search.DifferentialEvolutionProposals
    monkeypatch.setattr(global_search, "iter_reflector_proposals", proposals)
    monkeypatch.setattr(global_search, "DifferentialEvolutionProposals",
        lambda initial, lower, upper, **kwargs: evolution(initial, lower, upper,
            seed=kwargs["seed"], population_size=5))
    monkeypatch.setattr(refinement, "refine_synthesis_candidate",
        lambda _request, value, **_kwargs: (value, {"method": "schedule_fixture"}))
    cancelled = Event()
    progress = []

    def observe(item):
        progress.append(item)
        if termination == "cancel" and item.stage == "search_rotation" and item.cycle >= 6:
            cancelled.set()

    result = synthesis.synthesize_film_design(service, request,
        cancel_event=cancelled, progress_callback=observe)
    # By cycle 5 both the one-layer prior and the initially bare growth branch
    # cannot accept any layer. A later rotation proves independent search lived.
    assert any(item.stage == "search_rotation" and item.cycle >= 6 for item in progress)
    assert len([item for item in progress if item.stage == "branch_exhausted" and item.cycle < 6]) == 2
    assert max(scans) >= 3
    assert any(row["epoch"] >= 3 and row["completed_generations"] == 4
               for row in result.performance["search"]["global_search"])
    assert result.termination_reason == ("cancelled" if termination == "cancel" else "cycle_limit")
    assert result.state == ("cancelled" if termination == "cancel" else "not_found")
    assert not result.targets_met
    assert result.candidate_design is not None
    assert len(result.candidate_design["front_layers"]) <= 1
    assert result.training_score is not None
    assert result.performance["cleanup_complete"]
    assert result.performance["unknown_tasks"] == 0
    if termination == "cancel":
        assert result.candidate_provenance["training_complete"] is True
        assert result.candidate_provenance["frozen_at_cancel"] is True
