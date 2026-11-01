"""Synthetic geometry and admission checks; no optical success is implied."""
from copy import deepcopy
from dataclasses import replace

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_local_restarts import LOCAL_RESTART_POLICY, relative_basin_start
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def _case(selected=('front',)):
    _service, request = setup_problem()
    design = deepcopy(request.environment)
    design['substrate']['sides'] = 'double'
    def layer(identifier, material, thickness):
        return dict(id=identifier, material_id=material, optical_set_id='nk',
                    thickness=dict(value=thickness, unit='nm'), note='fixture')
    design['front_layers'] = [layer('f1', 'low', 100.), layer('f2', 'high', 135.)]
    design['back_layers'] = [layer('b1', 'low', 75.), layer('b2', 'high', 160.)]
    return replace(request, environment=deepcopy(design), selected_surfaces=selected,
                   structure_search_seed=19), design


def test_deterministic_seed_binds_rotation_root_seed_and_physical_identity():
    request, design = _case()
    before = deepcopy((request, design))
    first, info = relative_basin_start(request, design, rotation=1)
    assert (first, info) == relative_basin_start(request, design, rotation=1)
    assert info['policy'] == LOCAL_RESTART_POLICY and info['rotation'] == 1
    assert info['relative_log_sigma'] == .03
    assert info['parent_physical_hash'] == physical_design_hash(design)
    for rotation, sigma in ((2, .08), (3, .18), (4, .03)):
        other, other_info = relative_basin_start(request, design, rotation=rotation)
        assert other_info['relative_log_sigma'] == sigma
        assert other_info['seed'] != info['seed'] and other != first
    seeded = replace(request, structure_search_seed=20)
    assert relative_basin_start(seeded, design, rotation=1)[1]['seed'] != info['seed']
    changed = deepcopy(design)
    changed['front_layers'][0]['thickness']['value'] += 1.
    assert relative_basin_start(request, changed, rotation=1)[1]['seed'] != info['seed']
    # Cosmetic labels are not part of physical identity.
    cosmetic = deepcopy(design)
    cosmetic['front_layers'][0]['note'] = 'other label'
    assert relative_basin_start(request, cosmetic, rotation=1)[1]['seed'] == info['seed']
    assert (request, design) == before
    with pytest.raises(ValueError, match='rotation'):
        relative_basin_start(request, design, rotation=0)
    with pytest.raises(ValueError, match='rotation'):
        relative_basin_start(request, design, rotation=True)


@pytest.mark.parametrize('selected', [('front',), ('back',), ('front', 'back')])
def test_only_selected_thicknesses_change_no_alias_or_environment_changes(selected):
    request, design = _case(selected)
    before = deepcopy(design)
    trial, info = relative_basin_start(request, design, rotation=2)
    assert info['original_constraints_unchanged'] is True
    for side in ('front', 'back'):
        old, new = before[side + '_layers'], trial[side + '_layers']
        if side in selected:
            assert [r['thickness']['value'] for r in old] != [r['thickness']['value'] for r in new]
            ratios = [b['thickness']['value'] / a['thickness']['value'] for a, b in zip(old, new, strict=True)]
            assert ratios[0] != pytest.approx(ratios[1])  # Independent, not one common scale.
        else:
            assert new == old
        for old_row, new_row in zip(old, new, strict=True):
            assert {k: v for k, v in new_row.items() if k != 'thickness'} == {
                k: v for k, v in old_row.items() if k != 'thickness'}
            assert new_row['thickness']['unit'] == 'nm'
    assert {k: v for k, v in trial.items() if k not in ('front_layers', 'back_layers')} == {
        k: v for k, v in before.items() if k not in ('front_layers', 'back_layers')}
    trial['substrate']['sides'] = 'single'
    trial['front_layers'][0]['thickness']['value'] = 9.
    assert design == before and request.environment == before


def test_explicit_per_layer_bounds_zero_and_parent_total_admission():
    request, design = _case()
    request = replace(request, minimum_nonzero_thickness_nm=110., max_layer_thickness_nm=120.)
    trial, _ = relative_basin_start(request, design, rotation=1)
    assert all(110. <= r['thickness']['value'] <= 120. for r in trial['front_layers'])
    zero = deepcopy(design)
    zero['front_layers'][0]['thickness']['value'] = 0.
    zero_trial, _ = relative_basin_start(request, zero, rotation=1)
    assert zero_trial['front_layers'][0]['thickness']['value'] == 0.
    assert not synthesis._feasible(request, zero_trial)
    # The helper does not project total thickness; the parent admits or rejects
    # the actual generated candidate without silently relaxing the request.
    total_limited = replace(request, max_total_thickness_nm=219.)
    assert not synthesis._feasible(total_limited, trial)
    assert total_limited.max_total_thickness_nm == 219.


@pytest.mark.parametrize('until', [False, True])
def test_new_until_stages_are_wired_to_real_scored_candidates_without_changing_ordinary(monkeypatch, until):
    # The small dispersive substrate fixture runs real training and audit
    # physics; only the long local optimizer is replaced by a no-op recorder.
    # This verifies routing, provenance, and cancellation, not convergence or
    # any improvement from SLSQP on a realistic coating.
    from threading import Event
    from coat_sim.core import film_synthesis_audit as audit
    from coat_sim.core import film_synthesis_refinement as refinement
    from tests.test_film_synthesis_until_cancel_best import _request

    request = _request(until)
    event = Event()
    stages = []
    snapshots = []
    refined_sources = []
    original_audit = audit.audit_frozen_synthesis_candidate

    def recorded_refinement(frozen, initial, **kwargs):
        assert frozen.objectives == request.objectives
        assert initial['score']['coverage'] and initial['score']['errors']
        refined_sources.append(deepcopy(initial))
        if until and len(refined_sources) == 2:
            event.set()  # Cancel after the restart's scored local chunk.
        return deepcopy(initial), dict(termination='test_noop', all_original_conditions=True)

    def audited(*args, **kwargs):
        assert not event.is_set(), 'cancellation must not start another audit'
        return original_audit(*args, **kwargs)

    def progress(value):
        stages.append(value.stage)
        if not until and value.stage == 'hybrid_global_trial':
            event.set()
        # A broken routing path must fail rather than launch an unbounded run.
        if value.evaluations > 200:
            event.set()

    monkeypatch.setattr(refinement, 'refine_synthesis_full_minimax', recorded_refinement)
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', audited)
    result = synthesis.synthesize_film_design(None, request, cancel_event=event,
        progress_callback=progress, snapshot_callback=lambda *values: snapshots.append(deepcopy(values)))
    assert result.state == 'cancelled' and event.is_set()
    records = result.performance['search']['hybrid']['stages']
    stage_names = [item['stage'] for item in records]
    if until:
        assert stages.index('prior_local_polish') < stages.index('basin_restart')
        assert stage_names.index('diverse_prior_local_polish') < stage_names.index('independent_layer_basin_restart')
        assert len(refined_sources) == 2 and snapshots
        prior_trace = next(item for item in result.trace if item.stage == 'prior_local_polish')
        restart_trace = next(item for item in result.trace if item.stage == 'basin_restart')
        assert prior_trace.physical_hash == physical_design_hash(refined_sources[0]['design'])
        assert restart_trace.physical_hash == physical_design_hash(refined_sources[1]['design'])
        restart = next(item['restart'] for item in result.performance['search']['local_refinement']
                       if 'restart' in item)
        assert restart['parent_physical_hash'] == physical_design_hash(snapshots[-1][0])
        assert restart_trace.physical_hash != restart['parent_physical_hash']
        assert result.validation_complete and result.candidate_provenance['validation_matches_candidate']
        assert result.candidate_design is not None
        assert physical_design_hash(result.candidate_design) == restart['parent_physical_hash']
    else:
        assert 'hybrid_global_trial' in stages
        assert 'prior_local_polish' not in stages and 'basin_restart' not in stages
        assert 'diverse_prior_local_polish' not in stage_names
        assert 'independent_layer_basin_restart' not in stage_names
        assert not refined_sources
