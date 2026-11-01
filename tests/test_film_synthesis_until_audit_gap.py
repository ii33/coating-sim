"""Small real optics with controlled local solvers, never a performance claim.

The synthetic dispersive substrate has a sampled independent-grid miss. The
optimizer is a routing stub; gap/returned scores use actual optical evaluations.
"""
from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_audit as audit
from coat_sim.core import film_synthesis_feasibility as repair
from coat_sim.core import film_synthesis_refinement as refinement
from coat_sim.core.film_synthesis_archive import physical_design_hash
from tests.test_film_synthesis_until_cancel_best import _request

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def _run(monkeypatch, *, until=True, scale=1., stop='start'):
    request = _request(until)
    request = replace(request, objectives=(replace(request.objectives[0], normalization_scale=scale),))
    original_objectives = deepcopy(request.objectives)
    event = Event()
    stages = []
    snapshots = []
    local_calls = []
    real_audit = audit.audit_frozen_synthesis_candidate

    def local_stub(frozen, initial, **kwargs):
        assert frozen.objectives == original_objectives
        local_calls.append(physical_design_hash(initial['design']))
        terminal_callback = kwargs.get('terminal_callback')
        if terminal_callback is not None:
            terminal_callback(deepcopy(initial))
        return deepcopy(initial), dict(termination='converged',
            solver_terminal=dict(local_stationary=False), returned_best={},
            physical_candidates=2, iterations=2, terminal_full_grid_epigraph_feasible=True)

    def audit_guard(*args, **kwargs):
        assert not event.is_set(), 'no audit may start after cancellation'
        return real_audit(*args, **kwargs)

    def repair_stub(frozen, initial, *, evaluate, **kwargs):
        assert frozen.objectives == original_objectives
        if stop == 'budget':
            while True:
                evaluate(initial['design'])
        if stop == 'worse':
            candidate = deepcopy(initial['design'])
            candidate['front_layers'][0]['thickness']['value'] *= .5
            return evaluate(candidate), dict(termination='controlled_worse_trial')
        pytest.fail('repair solver should not run in this scenario')

    def progress(value):
        stages.append(value.stage)
        if (stop == 'start' and value.stage == 'feasibility_repair_start'
                and 'until_local_audit_gap' in value.message):
            event.set()
        if stop == 'worse' and value.stage == 'feasibility_repair_round':
            event.set()
        if value.stage == 'until_target_continue' or value.evaluations > 200:
            event.set()

    # Isolate the new gap bypass from the pre-existing near-feasible path.
    monkeypatch.setattr(repair, 'near_feasible_conditions', lambda *args, **kwargs: False)
    monkeypatch.setattr(repair, 'refine_feasibility_candidate', repair_stub)
    monkeypatch.setattr(refinement, 'refine_synthesis_full_minimax', local_stub)
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', audit_guard)
    result = synthesis.synthesize_film_design(None, request, cancel_event=event,
        progress_callback=progress, snapshot_callback=lambda *values: snapshots.append(deepcopy(values)))
    assert result.frozen_request.objectives == original_objectives
    assert local_calls and snapshots and not result.targets_met
    return result, stages, snapshots, real_audit


def _assert_retained_pair(result, snapshots, real_audit):
    assert result.candidate_design is not None
    assert result.training_score is not None and result.validation_score is not None
    assert result.validation_complete
    if result.state == 'cancelled':
        assert result.candidate_provenance['validation_matches_candidate']
    identity = physical_design_hash(result.candidate_design)
    assert identity == physical_design_hash(snapshots[-1][0])
    assert identity == physical_design_hash(result.training_score['design'])
    assert identity == physical_design_hash(result.validation_score['design'])
    # This separate post-run replay is explicit verification, not part of the
    # cancellation response and not evidence of a continuous-domain optimum.
    replay = real_audit(result.frozen_request, result.candidate_design)
    assert replay['score']['max_error'] == pytest.approx(result.validation_score['max_error'])


@pytest.mark.parametrize(('scale', 'triggered'), [(2_000_000., True), (5_000_000., False)])
def test_until_gap_floor_and_audited_source_identity(monkeypatch, scale, triggered):
    result, stages, snapshots, real_audit = _run(monkeypatch, scale=scale)
    assert result.state == 'cancelled' and 'local_session_complete' in stages
    training, validation = snapshots[-1][1:3]
    gap = validation['max_error'] - training['max_error']
    threshold = max(1e-8, 1e-6 * abs(training['max_error']))
    assert (gap > threshold) is triggered and gap > 0
    attempts = result.performance['search']['hybrid'].get('feasibility_repair_attempts', [])
    gap_attempts = [a for a in attempts if a['trigger_boundary'] == 'until_local_audit_gap']
    assert bool(gap_attempts) is triggered
    if triggered:
        record = gap_attempts[0]
        assert stages.index('local_session_complete') < stages.index('feasibility_repair_start')
        assert record['initial_physical_hash'] == physical_design_hash(snapshots[-1][0])
        assert record['status'] == 'cancelled_or_deadline'
        assert record['training_evaluations_used'] == record['audit_evaluations_used'] == 0
        assert record['maximum_shared_evaluations'] == 64 and record['maximum_rounds'] == 3
        assert record['formal_target_changed'] is False
    else:
        assert 'until_target_continue' in stages
    _assert_retained_pair(result, snapshots, real_audit)


def test_ordinary_mode_does_not_run_the_until_gap_bypass(monkeypatch):
    result, stages, snapshots, real_audit = _run(monkeypatch, until=False)
    assert result.state == 'completed' and 'local_session_complete' in stages
    assert snapshots[-1][2]['max_error'] - snapshots[-1][1]['max_error'] > 1e-8
    attempts = result.performance['search']['hybrid'].get('feasibility_repair_attempts', [])
    assert not any(a['trigger_boundary'] == 'until_local_audit_gap' for a in attempts)
    _assert_retained_pair(result, snapshots, real_audit)


def test_until_gap_repair_respects_shared_quantum_and_retains_audited_best(monkeypatch):
    result, stages, snapshots, real_audit = _run(monkeypatch, stop='budget')
    assert result.state == 'cancelled' and 'until_target_continue' in stages
    record = next(a for a in result.performance['search']['hybrid']['feasibility_repair_attempts']
                  if a['trigger_boundary'] == 'until_local_audit_gap')
    assert record['status'] == 'feasibility_repair_stage_budget'
    assert record['shared_evaluations_used'] == record['maximum_shared_evaluations'] == 64
    assert result.evaluations - record['trigger_evaluations'] == 64
    assert record['audit_evaluations_used'] == 0
    assert record['initial_physical_hash'] == physical_design_hash(snapshots[-1][0])
    _assert_retained_pair(result, snapshots, real_audit)


def test_until_gap_worse_repair_is_audited_but_cannot_replace_incumbent(monkeypatch):
    result, stages, snapshots, real_audit = _run(monkeypatch, stop='worse')
    assert result.state == 'cancelled' and 'feasibility_repair_round' in stages
    record = next(a for a in result.performance['search']['hybrid']['feasibility_repair_attempts']
                  if a['trigger_boundary'] == 'until_local_audit_gap')
    assert len(record['rounds']) == 1 and record['audit_evaluations_used'] == 1
    assert record['rounds'][0]['audit_max_error'] > snapshots[-1][2]['max_error']
    assert record['initial_physical_hash'] == physical_design_hash(snapshots[-1][0])
    _assert_retained_pair(result, snapshots, real_audit)
