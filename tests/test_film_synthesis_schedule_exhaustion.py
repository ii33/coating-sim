"""Insertion exhaustion must not exhaust independent continuous/structure search."""
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_global as global_search
from coat_sim.core import film_synthesis_refinement as refinement
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_continuation import continue_synthesis_request, validate_search_state
from tests.test_film_auto_synthesis import Service, setup_problem

pytestmark = pytest.mark.algorithm


def scheduling_problem(monkeypatch: pytest.MonkeyPatch, *, strategy: str = 'legacy',
                       source_count: int = 1) -> tuple[Service, synthesis.FilmSynthesisRequest, list[int]]:
    service, request = setup_problem(low=550., high=550., samples=1)
    request = replace(request, objectives=(replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, lower_bound=.99999, upper_bound=None)),), max_layers=1, max_cycles=7,
        max_evaluations=None, cpu_workers=1, position_samples=1, search_strategy=strategy)
    scans: list[int] = []

    def proposals(frozen, *, epoch=0, **_kwargs):
        scans.append(epoch)
        for index in range(source_count):
            design = deepcopy(frozen.environment)
            design["front_layers"] = [dict(id=f"schedule-prior-{epoch}-{index}", material_id="high",
                optical_set_id="nk", thickness=dict(value=55. + index, unit="nm"), note="")]
            yield global_search.StructuralProposal(design,
                dict(family="periodic", high=("high", "nk"), sequence_index=index))

    # Keep a real population and real optical evaluation, but shrink this
    # scheduling fixture's population and omit unrelated local optimization.
    evolution = global_search.DifferentialEvolutionProposals
    monkeypatch.setattr(global_search, "iter_reflector_proposals", proposals)
    monkeypatch.setattr(global_search, "DifferentialEvolutionProposals",
        lambda initial, lower, upper, **kwargs: evolution(initial, lower, upper,
            seed=kwargs["seed"], population_size=5))
    monkeypatch.setattr(refinement, "refine_synthesis_candidate",
        lambda _request, value, **_kwargs: (value, {"method": "schedule_fixture"}))
    return service, request, scans


@pytest.mark.parametrize("termination", ["cycle_limit", "cancel"])
def test_full_layer_branches_keep_structural_search_until_explicit_boundary(monkeypatch, termination):
    service, request, scans = scheduling_problem(monkeypatch)
    # Isolate the explicitly requested boundary from the separately tested
    # default local-stagnation stop. This is a caller option, not a product cap.
    assert request.max_cycles is not None
    request = replace(request, stagnation_cycles=request.max_cycles + 1)
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
    assert result.search_checkpoint is None  # Legacy does not implement durable stream continuation.
    assert not result.performance['search'].get('legacy_local_session', {}).get('session_complete', False)
    if termination == "cancel":
        assert result.candidate_provenance["training_complete"] is True
        assert result.candidate_provenance["frozen_at_cancel"] is True


def test_default_legacy_full_layer_session_stops_without_claiming_frontier_exhaustion(monkeypatch):
    service, request, scans = scheduling_problem(monkeypatch)
    result = synthesis.synthesize_film_design(service, request)
    assert result.state == 'completed' and not result.targets_met
    assert result.termination_reason == 'local_session_stalled_targets_unmet'
    assert request.max_cycles is not None and result.cycles < request.max_cycles
    assert scans and result.performance['search']['global_search']
    assert any(row.stage == 'branch_exhausted' for row in result.trace)
    local = result.performance['search']['legacy_local_session']
    assert local['session_complete'] and not local['local_converged']
    assert local['completed_cycles'] >= local['required_stagnant_cycles']
    assert local['thickness_reduction'] == 'skipped_no_fixed_topology_stationarity_evidence'
    assert result.search_checkpoint is None
    assert result.local_optimum is not None and not result.local_optimum['local_converged']
    assert result.candidate_design is not None and result.validation_complete
    assert result.training_score is not None and result.validation_score is not None
    identity = physical_design_hash(result.candidate_design)
    assert physical_design_hash(result.training_score['design']) == identity
    assert physical_design_hash(result.validation_score['design']) == identity
    assert result.training_score['max_error'] > 0. and result.validation_score['max_error'] > 0.
    assert result.performance['cleanup_complete'] and result.performance['unknown_tasks'] == 0


def test_full_layer_hybrid_sessions_continue_saved_streams_for_two_explicit_requests(monkeypatch):
    service, request, _scans = scheduling_problem(monkeypatch, strategy='hybrid', source_count=128)
    request = replace(request, max_cycles=1, max_evaluations=500, structure_search_seed=251,
        parallel_batch_size=2, enable_thickness_reduction=False)
    current = synthesis.synthesize_film_design(service, request)
    assert current.state == 'completed' and not current.targets_met
    assert current.search_checkpoint is not None and current.candidate_design is not None
    # Interrupt one real source draw before it can be physically evaluated.
    # The engine, not this test, must create/seal the pending record and cursor.
    cancel = Event()
    pause_at = current.search_checkpoint['cursors']['reflector']
    original = global_search.iter_reflector_proposals
    interrupted = False

    def pause_new_proposal(*args, **kwargs):
        nonlocal interrupted
        for proposal in original(*args, **kwargs):
            if not interrupted and proposal.provenance['sequence_index'] == pause_at:
                interrupted = True
                cancel.set()
            yield proposal

    monkeypatch.setattr(global_search, 'iter_reflector_proposals', pause_new_proposal)
    paused = synthesis.synthesize_film_design(None,
        continue_synthesis_request(current.frozen_request, current.search_checkpoint,
                                   current_design=current.candidate_design), cancel_event=cancel)
    assert interrupted and cancel.is_set()
    assert paused.state == 'cancelled' and paused.termination_reason == 'cancelled'
    assert not paused.performance['search']['hybrid'].get('local_session', {}).get('session_complete', False)
    assert paused.search_checkpoint is not None and paused.search_checkpoint['pending']
    assert validate_search_state(paused.frozen_request, paused.search_checkpoint)
    current = paused
    pending_consumed = False
    for _ in range(2):
        assert current.search_checkpoint is not None and current.candidate_design is not None
        saved = json.loads(json.dumps(current.search_checkpoint))
        unchanged = deepcopy(saved)
        pending_before = {physical_design_hash(row['design']) for row in saved['pending']}
        continued = continue_synthesis_request(current.frozen_request, saved,
                                               current_design=current.candidate_design)
        following = synthesis.synthesize_film_design(None, continued)
        assert saved == unchanged
        assert following.state == 'completed' and not following.targets_met
        assert following.search_checkpoint is not None and following.candidate_design is not None
        checkpoint = following.search_checkpoint
        assert validate_search_state(following.frozen_request, checkpoint)
        assert checkpoint['cursors']['reflector'] > saved['cursors']['reflector']
        assert checkpoint['rotations'] > saved['rotations']
        assert checkpoint['random_state']['root_seed'] == 251
        assert checkpoint['random_state']['next_rotation_seed'] > saved['random_state']['next_rotation_seed']
        newly_tried = set(checkpoint['tried']) - set(saved['tried'])
        assert newly_tried
        assert any(row.stage == 'structure_proposal' and row.physical_hash in newly_tried
                   for row in following.trace)
        pending_after = {physical_design_hash(row['design']) for row in checkpoint['pending']}
        assert not pending_after.intersection(checkpoint['tried'])
        assert pending_before <= set(checkpoint['tried']) | pending_after
        if pending_before:
            assert pending_before.intersection(newly_tried)
            pending_consumed = True
        assert 'continuation_frontier_exhausted' != following.termination_reason
        assert len(following.candidate_design['front_layers']) <= 1
        assert following.validation_complete and following.evaluations > 2
        local = following.performance['search']['hybrid']['local_session']
        assert local['session_complete']
        assert following.training_score is not None and following.validation_score is not None
        identity = physical_design_hash(following.candidate_design)
        assert physical_design_hash(following.training_score['design']) == identity
        assert physical_design_hash(following.validation_score['design']) == identity
        assert following.training_score['max_error'] > 0. and following.validation_score['max_error'] > 0.
        current = following
    assert pending_consumed
