"""Regression for cross-turn audited retention, using real optics.

The pending proposal and local solver termination are deliberately controlled;
these orchestration cases do not claim to reproduce a user's physical design.
"""
from copy import deepcopy

import pytest

from coat_sim.core.film_auto_synthesis import synthesize_film_design
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_continuation import continue_synthesis_request, seal_search_state
from tests.test_film_synthesis_session_stop import unmet_problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def _new_pending(state, design):
    state = deepcopy(dict(state))
    state['active_sources'] = []
    state['pending'] = [dict(design=deepcopy(design), provenance=dict(family='controlled_test'))]
    return seal_search_state(state)


def _stop_local_at_real_source(monkeypatch):
    """Simulate solver termination; each returned design is physically scored/audited."""
    from coat_sim.core import film_synthesis_refinement as refinement
    original = refinement.refine_synthesis_full_minimax

    def stop(request, initial, **kwargs):
        if kwargs.get('terminal_callback') is None:
            return original(request, initial, **kwargs)
        identity = physical_design_hash(initial['design'])
        kwargs['terminal_callback'](initial)
        return initial, dict(termination='converged', solver_terminal=dict(
            physical_hash=identity, local_stationary=True),
            returned_best=dict(physical_hash=identity, is_solver_terminal=True))

    monkeypatch.setattr(refinement, 'refine_synthesis_full_minimax', stop)


@pytest.mark.parametrize('edit_current_seed', [False, True])
@pytest.mark.parametrize('until_target', [False, True])
def test_unmet_audited_incumbent_survives_repeated_worse_continues(monkeypatch, edit_current_seed, until_target):
    service, request = unmet_problem(acceptable_normalized_error=1e-8, search_until_target=until_target)
    first = synthesize_film_design(service, request)
    assert first.state == 'completed' and not first.targets_met
    assert first.validation_complete and first.search_checkpoint is not None
    assert first.validation_score is not None and first.candidate_design is not None
    incumbent = deepcopy(first.candidate_design)
    prior_error = first.validation_score['max_error']
    assert physical_design_hash(incumbent) in {
        physical_design_hash(entry['design']) for entry in first.search_checkpoint['pool']}
    _stop_local_at_real_source(monkeypatch)
    current = first
    for thickness in (10., 20.):
        proposal = deepcopy(incumbent)
        proposal['front_layers'][0]['thickness']['value'] = thickness
        seed = deepcopy(incumbent)
        if edit_current_seed:
            seed['front_layers'][0]['thickness']['value'] = 30.
        checkpoint = _new_pending(current.search_checkpoint, proposal)
        snapshots: list[dict] = []
        result = synthesize_film_design(None, continue_synthesis_request(current.frozen_request,
            checkpoint, current_design=seed), snapshot_callback=lambda design, *_, rows=snapshots: rows.append(deepcopy(design)))
        assert any(row.stage == 'structure_proposal' and row.physical_hash == physical_design_hash(proposal)
                   for row in result.trace), result.termination_reason
        assert result.state == 'completed', result.termination_reason
        assert not result.targets_met and result.validation_complete
        assert result.validation_score is not None and result.candidate_design is not None
        assert result.validation_score['max_error'] <= prior_error + 1e-12
        assert physical_design_hash(result.candidate_design) == physical_design_hash(incumbent)
        assert snapshots and physical_design_hash(snapshots[-1]) == physical_design_hash(result.candidate_design)
        assert result.search_checkpoint is not None
        current = result


def test_genuinely_better_new_branch_is_promoted(monkeypatch):
    """Controlled checkpoint with an inferior incumbent, not a user-case replay."""
    from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate

    service, request = unmet_problem(acceptable_normalized_error=1e-8)
    reference = synthesize_film_design(service, request)
    assert reference.candidate_design is not None and reference.search_checkpoint is not None
    improved = deepcopy(reference.candidate_design)
    inferior = deepcopy(improved)
    inferior['front_layers'][0]['thickness']['value'] = 20.
    inferior_audit = audit_frozen_synthesis_candidate(reference.frozen_request, inferior)['score']
    improved_audit = audit_frozen_synthesis_candidate(reference.frozen_request, improved)['score']
    assert not inferior_audit['nominal_pass'] and not improved_audit['nominal_pass']
    assert improved_audit['max_error'] < inferior_audit['max_error']
    state = deepcopy(dict(reference.search_checkpoint))
    state['pool'] = [dict(design=deepcopy(inferior), provenance=dict(family='controlled_old'),
                          parent_hash=None, operator='evaluated', insertion_index=1)]
    state['tried'] = [physical_design_hash(inferior)]
    state = _new_pending(state, improved)
    _stop_local_at_real_source(monkeypatch)
    result = synthesize_film_design(None, continue_synthesis_request(reference.frozen_request,
        state, current_design=inferior))
    assert any(row.stage == 'structure_proposal' and row.physical_hash == physical_design_hash(improved)
               for row in result.trace), result.termination_reason
    assert result.state == 'completed' and result.validation_complete and not result.targets_met
    assert result.candidate_design is not None and result.validation_score is not None
    assert physical_design_hash(result.candidate_design) == physical_design_hash(improved)
    assert result.validation_score['max_error'] < inferior_audit['max_error']


def test_budget_exhausted_during_restoration_keeps_unvisited_pool_without_false_completion():
    service, request = unmet_problem(acceptable_normalized_error=1e-8)
    first = synthesize_film_design(service, request)
    assert first.search_checkpoint is not None and first.candidate_design is not None
    old = deepcopy(dict(first.search_checkpoint))
    edited = deepcopy(first.candidate_design)
    edited['front_layers'][0]['thickness']['value'] = 30.
    result = synthesize_film_design(None, continue_synthesis_request(first.frozen_request,
        old, current_design=edited, max_evaluations=2))
    assert result.state != 'completed' and result.termination_reason == 'evaluation_limit'
    assert not result.targets_met and result.evaluations <= 2
    assert result.search_checkpoint is not None
    original = {physical_design_hash(item['design']) for item in old['pool']}
    recovered = {physical_design_hash(item['design']) for item in result.search_checkpoint['pool']}
    assert original <= recovered
    assert not any(row.stage in {'structure_proposal', 'search_resumed'} for row in result.trace)
    if result.validation_complete:
        assert result.validation_score is not None and result.candidate_design is not None
        assert physical_design_hash(result.validation_score['design']) == physical_design_hash(result.candidate_design)


def test_cancel_during_restoration_keeps_unvisited_pool_and_no_new_search():
    from threading import Event

    service, request = unmet_problem(acceptable_normalized_error=1e-8)
    first = synthesize_film_design(service, request)
    assert first.search_checkpoint is not None and first.candidate_design is not None
    state = deepcopy(dict(first.search_checkpoint))
    edited = deepcopy(first.candidate_design)
    edited['front_layers'][0]['thickness']['value'] = 30.
    cancel = Event()
    observed = []
    def stop_after_first_audit(row):
        observed.append(row.stage)
        if row.stage == 'validation_complete':
            cancel.set()
    result = synthesize_film_design(None, continue_synthesis_request(first.frozen_request,
        state, current_design=edited), cancel_event=cancel, progress_callback=stop_after_first_audit)
    assert result.state == 'cancelled' and not result.targets_met
    assert 'validation_complete' in observed and 'search_resumed' not in observed
    assert not any(row.stage == 'structure_proposal' for row in result.trace)
    assert result.search_checkpoint is not None
    before = {physical_design_hash(item['design']) for item in state['pool']}
    after = {physical_design_hash(item['design']) for item in result.search_checkpoint['pool']}
    assert before <= after
    if result.validation_complete:
        assert result.validation_score is not None and result.candidate_design is not None
        assert physical_design_hash(result.validation_score['design']) == physical_design_hash(result.candidate_design)


def test_full_pool_cancel_between_restore_scoring_and_audit_preserves_unfinished_candidate():
    """A candidate pruned by archive admission must survive until its audit commits."""
    from threading import Event

    service, request = unmet_problem(acceptable_normalized_error=1e-8, archive_capacity=2)
    first = synthesize_film_design(service, request)
    assert first.search_checkpoint is not None and first.candidate_design is not None
    state = deepcopy(dict(first.search_checkpoint))
    originals = []
    for thickness in (20., 10.):
        design = deepcopy(first.candidate_design)
        design['front_layers'][0]['thickness']['value'] = thickness
        originals.append(design)
    state['pool'] = [dict(design=deepcopy(design), provenance=dict(family='controlled_old'),
                          parent_hash=None, operator='evaluated', insertion_index=index)
                     for index, design in enumerate(originals, start=1)]
    state = seal_search_state(state)
    cancel = Event()
    seen = []
    def stop_before_third_audit(row):
        if row.stage == 'validation':
            seen.append(row.stage)
            if len(seen) == 3:
                cancel.set()
    result = synthesize_film_design(None, continue_synthesis_request(first.frozen_request,
        state, current_design=first.candidate_design), cancel_event=cancel,
        progress_callback=stop_before_third_audit)
    assert result.state == 'cancelled' and len(seen) == 3
    assert [row.physical_hash for row in result.trace if row.stage == 'validation'][-1] == physical_design_hash(originals[-1])
    assert result.search_checkpoint is not None
    retained = {physical_design_hash(item['design']) for item in result.search_checkpoint['pool']}
    assert physical_design_hash(originals[-1]) in retained
