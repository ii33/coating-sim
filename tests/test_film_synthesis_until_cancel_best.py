"""Cancellation regression using a tiny synthetic, but real-physics, grid.

The dispersive glass deliberately differs at the unsampled midpoint; training
and audit grids can therefore disagree. No measured/private material data and
no scoring monkeypatch are involved. This exercises selection and binding, not
whether a broad real-world optimization finds a global physical optimum.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from threading import Event
from typing import Any
import json

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_optimization_structure as physical
from coat_sim.core import film_synthesis_audit as audit
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload, film_synthesis_result_to_payload
from coat_sim.core.film_synthesis_jobs import is_synthesis_result, replay_synthesis_result
from tests.test_film_candidate_display import harness
from tests.test_film_synthesis_jobs import _cancel_payload

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def _request(until=True):
    _, request, _ = film_synthesis_request_from_payload(_cancel_payload())
    objective = replace(request.objectives[0], pointwise=FilmObjectiveConstraint(
        mode='range', target_value=None, lower_bound=None, upper_bound=.03))
    return replace(request, objectives=(objective,), search_strategy='hybrid',
                   search_until_target=until, acceptable_normalized_error=.02,
                   max_cycles=2, cpu_workers=1)


def _cancel_after_audited_then_training(monkeypatch, until=True):
    request = _request(until)
    event = Event()
    snapshots: list[tuple[dict[str, Any], ...]] = []
    observer, _packets, _clock, _control = harness(request.objectives[0])
    observed = []
    original_forward = physical.simulate_with_thickness_jacobian
    original_audit = audit.audit_frozen_synthesis_candidate
    original_evaluate = synthesis.evaluate_frozen_structure_candidate

    def evaluate(*args, **kwargs):
        assert not event.is_set(), 'no training physics after cancellation'
        return original_evaluate(*args, **kwargs)

    def forward(*args, **kwargs):
        assert not event.is_set(), 'no physical forward after cancellation'
        return original_forward(*args, **kwargs)

    def audited(*args, **kwargs):
        assert not event.is_set(), 'no new audit after cancellation'
        return original_audit(*args, **kwargs)

    def progress(value):
        # The initial bare substrate was audited; a later, fully committed
        # training incumbent is a different single-layer physical design.
        if snapshots and value.stage == 'incumbent':
            observed.append((deepcopy(observer._best), deepcopy(observer._current)))
            event.set()

    monkeypatch.setattr(synthesis, 'evaluate_frozen_structure_candidate', evaluate)
    monkeypatch.setattr(physical, 'simulate_with_thickness_jacobian', forward)
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', audited)
    result = synthesis.synthesize_film_design(None, request, cancel_event=event,
        progress_callback=progress, snapshot_callback=lambda *values: snapshots.append(deepcopy(values)),
        candidate_observer=observer)
    assert event.is_set() and snapshots and observed
    # Unhook guards before the independent replay below, which must do physics.
    monkeypatch.setattr(synthesis, 'evaluate_frozen_structure_candidate', original_evaluate)
    monkeypatch.setattr(physical, 'simulate_with_thickness_jacobian', original_forward)
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', original_audit)
    return result, snapshots, observed


def test_until_cancel_keeps_the_matching_audited_pair_and_current_preview(monkeypatch):
    result, snapshots, observed = _cancel_after_audited_then_training(monkeypatch)
    design, training, validation, _audit = snapshots[-1]
    best_at_cancel, current_at_cancel = observed[-1]
    assert validation['max_error'] > training['max_error'] > 0
    assert result.state == 'cancelled' and not result.targets_met
    assert physical_design_hash(result.candidate_design) == physical_design_hash(design)
    assert result.training_score == training
    assert result.validation_score == validation and result.validation_complete
    assert result.candidate_provenance['selection'] == 'completed_training_at_cancel'
    assert result.candidate_provenance['training_complete'] is True
    assert result.candidate_provenance['frozen_at_cancel'] is True
    assert result.candidate_provenance['validation_matches_candidate'] is True
    assert result.performance['cleanup_complete']
    assert result.performance['search']['cancel_selection'] == {
        'policy': 'prefer_current_run_audited_incumbent',
        'selected_audit_complete': True, 'post_cancel_optics': False}
    # Diagnostic only: no new candidate_provenance format.
    assert best_at_cancel is not None and current_at_cancel is not None
    assert best_at_cancel[0]['physical_hash'] != current_at_cancel[0]['physical_hash']
    assert best_at_cancel[0]['layer_count'] == 0
    assert current_at_cancel[0]['layer_count'] == 1
    # Preview selection is audit-backed, but its existing observer contract
    # explicitly displays original-grid training evidence, not dense audit rows.
    assert best_at_cancel[0]['max_normalized_error'] == pytest.approx(training['max_error'])

    # The job/result contract must round-trip real evidence and reject forgery.
    payload = json.loads(json.dumps(film_synthesis_result_to_payload('until-cancel', result)))
    assert is_synthesis_result(payload)
    assert replay_synthesis_result(payload)['result']['validation_complete']
    bad = deepcopy(payload)
    bad['result']['validation_score']['max_error'] += .1
    with pytest.raises(ValueError):
        replay_synthesis_result(bad)
    mismatch = deepcopy(payload)
    mismatch['result']['validation_score']['design']['front_layers'] = [{
        'id': 'foreign', 'material_id': 'low', 'optical_set_id': 'nk',
        'thickness': {'value': 56., 'unit': 'nm'}}]
    assert not is_synthesis_result(mismatch)
    with pytest.raises(ValueError):
        replay_synthesis_result(mismatch)


def test_ordinary_cancel_keeps_training_winner_not_prior_audit(monkeypatch):
    result, snapshots, _ = _cancel_after_audited_then_training(monkeypatch, until=False)
    assert result.state == 'cancelled'
    assert physical_design_hash(result.candidate_design) != physical_design_hash(snapshots[-1][0])
    assert result.candidate_design['front_layers']
    assert result.training_score['max_error'] == 0 < snapshots[-1][1]['max_error']
    assert not result.validation_complete and result.validation_score is None
    assert result.candidate_provenance['validation_matches_candidate'] is False


def test_until_cancel_before_any_audit_falls_back_to_completed_training(monkeypatch):
    request = _request()
    event = Event()
    snapshots = []
    original_audit = audit.audit_frozen_synthesis_candidate
    def audited(*args, **kwargs):
        assert not event.is_set(), 'no new audit after cancellation'
        return original_audit(*args, **kwargs)
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', audited)
    def progress(value):
        if value.stage == 'incumbent':
            event.set()
    result = synthesis.synthesize_film_design(None, request, cancel_event=event,
        progress_callback=progress, snapshot_callback=lambda *values: snapshots.append(values))
    assert result.state == 'cancelled' and not snapshots
    assert result.candidate_design is not None and result.training_score is not None
    assert result.validation_score is None and not result.validation_complete
    assert result.candidate_provenance['selection'] == 'completed_training_at_cancel'
    assert result.candidate_provenance['validation_matches_candidate'] is False
