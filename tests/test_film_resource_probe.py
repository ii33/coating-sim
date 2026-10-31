"""General resource probes: fresh native physics, not HR extrapolation.

Synthetic constant-index oracles exercise objective families; they do not
calibrate effort defaults or claim universal feasibility/minimum resources.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from threading import Event

import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint as C
from coat_sim.core.film_optimization import FilmOptimizationObjective as O
from coat_sim.core.film_resource_estimation import (
    FilmResourceEstimateRequest, estimate_film_resources,
)
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def Request(*args, **kwargs):
    # These tests explicitly exercise optional synthesis verification.
    kwargs.setdefault('probe_max_evaluations', 256)
    kwargs.setdefault('audit_candidates', True)
    return FilmResourceEstimateRequest(*args, **kwargs)


def objective(lo, hi, *, low=None, up=None, metric='R', strict=False):
    return O(lo, hi, 3, metric=metric, pointwise=C(lower_bound=low, upper_bound=up,
                                                strict_upper=strict))


FAMILIES = [
    ('AR', (objective(520, 580, up=.005),)),
    ('HR', (objective(520, 580, low=.8),)),
    ('splitter', (objective(520, 580, low=.45, up=.55),)),
    ('bandpass', (objective(460, 480, low=.6), objective(540, 560, low=.5, metric='T'),
                  objective(640, 660, low=.6))),
    ('multispectral', (objective(480, 500, low=.7), objective(620, 640, low=.7))),
]


@pytest.mark.parametrize('family,objectives', FAMILIES, ids=[f[0] for f in FAMILIES])
def test_opt_in_probe_executes_general_objectives_with_truthful_joint_resources(family, objectives):
    service, base = setup_problem()
    base = replace(base, objectives=objectives, max_layers=0, max_total_thickness_nm=.01)
    before = deepcopy(asdict(base))
    result = estimate_film_resources(service, Request(base, (base.environment,), layer_search_limit=0))
    assert result.state == 'completed', result.message
    assert result.work['probe_runs'] == 2
    assert [r['seed'] for r in result.work['probe_outcomes']] == [0, 1]
    assert all(r['state'] != 'running' for r in result.work['probe_outcomes'])
    assert result.work['probe_evaluations'] == sum(r['evaluations'] for r in result.work['probe_outcomes'])
    assert result.work['probe_physical_evaluations'] == sum(r['physical_evaluations'] for r in result.work['probe_outcomes'])
    assert result.work['probe_wavelength_points'] == sum(r['wavelength_points'] for r in result.work['probe_outcomes'])
    assert result.work['physical_evaluations'] >= result.work['probe_physical_evaluations'] > 0
    assert result.work['actual_work_complete']
    assert result.candidate_reports
    if family in ('AR', 'HR'):
        assert result.pareto_witnesses
        assert any(r['layer_count'] > 0 for r in result.pareto_witnesses)
    for witness in result.pareto_witnesses:
        assert witness['state'] == 'audited' and witness['targets_met'] and witness['within_constraints']
        assert witness['audit'] is not None
        assert witness['layer_count'] > 0 and witness['total_thickness_nm'] > .01
        assert not witness['within_search_budgets']  # reference only, not physics caps
        assert witness['physical_hash'] in {r['physical_hash'] for r in result.candidate_reports}
    assert result.feasibility_state in ('unknown', 'sampled_witness_found')
    assert result.estimate_state == 'not_calibrated' and not result.continuous_certificate
    assert asdict(base) == before


def test_probe_zero_means_off_and_finite_budget_is_per_seed_not_physical_bound():
    service, base = setup_problem()
    off = estimate_film_resources(service, Request(base, probe_max_evaluations=0, layer_search_limit=0))
    assert off.state == 'completed' and off.work['probe_runs'] == 0
    assert off.work['probe_evaluations'] == 0 and not off.pareto_witnesses
    limited = estimate_film_resources(service, Request(base, probe_max_evaluations=2, layer_search_limit=0))
    assert limited.state == 'completed', limited.message
    assert limited.work['probe_runs'] == 2
    for outcome in limited.work['probe_outcomes']:
        assert outcome['requested_max_evaluations'] == 2
        assert outcome['evaluation_window_exceeded'] == (outcome['evaluations'] > 2)
    assert limited.feasibility_state == 'unknown'  # no-solution-found != impossible
    assert not limited.continuous_certificate


def test_probe_deterministic_replay_and_frozen_sources_do_not_reread_live_materials():
    service, base = setup_problem()
    baseline = estimate_film_resources(service, Request(base, layer_search_limit=0))
    changed = False
    def progress(value):
        nonlocal changed
        if value.stage == 'probe' and not changed:
            for record in service.materials.records.values():
                record['optical_sets'][0]['n'] = [7.] * 3
            changed = True
    replay = estimate_film_resources(service, Request(base, layer_search_limit=0), progress_callback=progress)
    assert changed and replay.state == 'completed', replay.message
    assert replay.source_digest == baseline.source_digest
    def summary(result):
        return [(r['physical_hash'], r['targets_met'], r['max_normalized_error'],
                 r['layer_count'], r['total_thickness_nm']) for r in result.candidate_reports]
    assert summary(replay) == summary(baseline)
    assert replay.work['probe_evaluations'] == baseline.work['probe_evaluations']


def test_cancel_after_completed_seed_preserves_complete_witness_and_exact_work():
    service, base = setup_problem()
    event = Event()
    def progress(value):
        if value.stage == 'probe_complete':
            event.set()
    result = estimate_film_resources(service, Request(base, layer_search_limit=0),
                                     cancel_event=event, progress_callback=progress)
    assert result.state == 'cancelled'
    assert result.work['probe_runs'] == 1
    assert result.pareto_witnesses and result.recommended_budget
    assert result.feasibility_state == 'sampled_witness_found'
    assert result.work['probe_outcomes'][0]['state'] == 'completed'
    assert result.work['probe_evaluations'] == result.work['probe_outcomes'][0]['evaluations']


@pytest.mark.parametrize('changes', [dict(probe_max_evaluations=-1), dict(probe_max_evaluations=1), dict(probe_max_evaluations=True),
    dict(probe_max_evaluations=1.5), dict(probe_seeds=()), dict(probe_seeds=(0, 0)),
    dict(probe_seeds=(True,)), dict(probe_seeds=(-1,))])
def test_probe_controls_reject_invalid_inputs_without_physics(changes):
    service, base = setup_problem()
    result = estimate_film_resources(service, Request(base, **changes))
    assert result.state == 'failed' and result.input_status == 'invalid'
    assert result.work['physical_evaluations'] == 0


@pytest.mark.parametrize('damage', ['physical_hash', 'source_digest', 'missing_validation',
                                   'incomplete_validation', 'incomplete_training', 'audit_metadata'])
def test_probe_never_promotes_missing_wrong_source_or_incomplete_snapshot(monkeypatch, damage):
    from coat_sim.core import film_resource_estimation as module
    native = module.synthesize_film_design
    def synthesize(service, request, **kwargs):
        callback = kwargs['snapshot_callback']
        def damaged(design, training, validation, audit):
            training, validation, audit = deepcopy(training), deepcopy(validation), deepcopy(audit)
            if damage in ('physical_hash', 'source_digest'):
                audit[damage] = 'wrong-this-must-not-match'
            elif damage == 'missing_validation':
                validation = {}
            elif damage == 'incomplete_validation':
                validation['errors'] = validation['errors'][:-1]
            elif damage == 'incomplete_training':
                training['errors'] = training['errors'][:-1]
            else:
                audit['dense_points'] = -1
            callback(design, training, validation, audit)
        kwargs['snapshot_callback'] = damaged
        return native(service, request, **kwargs)
    monkeypatch.setattr(module, 'synthesize_film_design', synthesize)
    service, base = setup_problem()
    result = estimate_film_resources(service, Request(base, layer_search_limit=0, probe_seeds=(0,)))
    assert result.state == 'completed', result.message
    assert result.work['probe_outcomes'][0]['invalid_audit_snapshots'] > 0
    assert not result.pareto_witnesses and result.recommended_budget is None
    assert result.feasibility_state == 'unknown'
    assert all(not r['targets_met'] and r['state'] == 'not_audited' for r in result.candidate_reports)
    assert result.work['physical_evaluations'] > 0  # invalid evidence is not zero work


def test_probe_solver_tolerance_cannot_replace_strict_original_nominal_acceptance():
    service, base = setup_problem()
    base = replace(base, objectives=(objective(520, 580, up=.03, strict=True),),
                   acceptable_normalized_error=.1)
    result = estimate_film_resources(service, Request(base, layer_search_limit=0,
                                    probe_max_evaluations=2, probe_seeds=(0,)))
    assert result.state == 'completed', result.message
    assert result.candidate_reports
    assert not result.pareto_witnesses and result.feasibility_state == 'unknown'
    assert all(not r['targets_met'] for r in result.candidate_reports)
    # Non-strict nominal failure can still be inside the separate solver tolerance.
    ordinary = replace(base, objectives=(objective(520, 580, up=.03),))
    approximate = estimate_film_resources(service, Request(ordinary, layer_search_limit=0,
                                          probe_max_evaluations=2, probe_seeds=(0,)))
    assert not approximate.pareto_witnesses
    assert any(r['solver_acceptable'] is True and not r['targets_met']
               for r in approximate.candidate_reports)


def test_probe_lossless_unknown_k_opt_in_remains_conditional_provenance():
    service, base = setup_problem()
    service.materials.records['low']['optical_sets'][0]['k'] = None
    strict = estimate_film_resources(service, Request(base, layer_search_limit=0))
    assert strict.state == 'failed' and not strict.pareto_witnesses
    environment = deepcopy(base.environment)
    environment['assume_lossless'] = True
    result = estimate_film_resources(service, Request(replace(base, environment=environment),
                                    layer_search_limit=0, probe_seeds=(0,)))
    assert result.state == 'completed', result.message
    assert result.pareto_witnesses
    assert all(r['conditional_on_assumption'] for r in result.pareto_witnesses)
    assert all(r['absorption_assumption']['conditional_on_assumption'] for r in result.pareto_witnesses)


def test_hard_broadband_ar_budget_exhaustion_retains_actual_diagnostics_not_impossibility():
    service, base = setup_problem(low=400, high=700, upper=.001, samples=9)
    for name, n in (('low', 1.45), ('high', 2.1), ('glass', 1.52)):
        optical = service.materials.records[name]['optical_sets'][0]
        optical.update(wavelength_min=400., wavelengths=[400., 550., 700.], n=[n] * 3)
    result = estimate_film_resources(service, Request(base, layer_search_limit=0))
    assert result.state == 'completed', result.message
    assert result.work['probe_runs'] == 2
    assert result.candidate_reports
    audited = [r for r in result.candidate_reports if r['state'] == 'audited']
    assert audited
    assert all(r['layer_count'] is not None and r['total_thickness_nm'] is not None
               and r['max_normalized_error'] is not None for r in audited)
    assert result.feasibility_state in ('unknown', 'sampled_witness_found')
    assert result.estimate_state == 'not_calibrated' and not result.continuous_certificate


def test_probe_selected_front_keeps_fixed_back_surface_and_process_constraints():
    service, base = setup_problem()
    env = deepcopy(base.environment)
    env['substrate'].update(sides='double', thickness=dict(value=1., unit='mm'))
    fixed = dict(id='fixed-back', material_id='high', optical_set_id='nk',
                 thickness=dict(value=20., unit='nm'), note='must remain fixed')
    env['back_layers'] = [fixed]
    base = replace(base, environment=env, selected_surfaces=('front',),
                   minimum_nonzero_thickness_nm=2., max_layer_thickness_nm=200.,
                   objectives=(objective(520, 580, up=.2),))
    result = estimate_film_resources(service, Request(base, layer_search_limit=0, probe_seeds=(0,)))
    assert result.state == 'completed', result.message
    assert result.candidate_reports
    for row in result.candidate_reports:
        if row['state'] != 'audited':
            continue
        assert row['candidate_design']['back_layers'] == [fixed]
        assert row['surfaces']['back']['layer_count'] == 1
        assert row['surfaces']['back']['total_thickness_nm'] == 20
        assert row['layer_count'] == len(row['candidate_design']['front_layers'])
        assert row['within_constraints']


def test_bad_current_resource_candidate_does_not_block_independent_constructive_probe(monkeypatch):
    import coat_sim.core.film_resource_estimation as module

    service, base = setup_problem()
    current = deepcopy(base.environment)
    current['front_layers'] = [dict(id='bad-current', material_id='low', optical_set_id='nk',
        thickness=dict(value=80., unit='nm'), note='candidate-specific invalid physical conversion')]
    actual_resources = module._resources

    def candidate_specific_error(request, design, sources, reference):
        if any(row['id'] == 'bad-current' for row in design['front_layers']):
            raise ValueError('candidate-specific physical thickness conversion failed')
        return actual_resources(request, design, sources, reference)

    monkeypatch.setattr(module, '_resources', candidate_specific_error)
    result = estimate_film_resources(service, Request(base, (current,), layer_search_limit=0,
                                    probe_seeds=(0,)))
    assert result.state == 'completed', result.message
    assert result.candidate_reports[0]['state'] == 'error'
    assert result.work['probe_skipped_starts'][0]['reason'] == 'candidate-specific physical thickness conversion failed'
    assert result.work['probe_runs'] == 1
    assert result.pareto_witnesses
    assert all(r['source'] == 'probe' for r in result.pareto_witnesses)


def test_probe_preserves_constructive_branch_and_real_seed_diversity_with_ui_current(monkeypatch):
    import coat_sim.core.film_resource_estimation as module

    service, base = setup_problem()
    current = deepcopy(base.environment)
    current['front_layers'] = [dict(id='current', material_id='high', optical_set_id='nk',
                                  thickness=dict(value=40., unit='nm'), note='warm branch')]
    native = module.synthesize_film_design
    captured = []

    def observe(service, request, **kwargs):
        captured.append(request)
        return native(service, request, **kwargs)

    monkeypatch.setattr(module, 'synthesize_film_design', observe)
    result = estimate_film_resources(service, Request(base, (current,), layer_search_limit=0))
    assert result.state == 'completed', result.message
    assert captured[0].seed_designs == ()
    assert captured[1].seed_designs
    assert [r.randomize_template_search for r in captured] == [False, True]
    assert result.work['probe_outcomes'][0]['start_mode'] == 'constructive'
    assert result.work['probe_outcomes'][1]['start_mode'] == 'current_and_explicit_starts'

    # The actual UI's empty current candidate must not disable native priors.
    empty = estimate_film_resources(service, Request(base, (base.environment,), layer_search_limit=0))
    a, b = empty.work['probe_outcomes']
    assert a['start_mode'] == b['start_mode'] == 'constructive'
    assert a['audited_candidate_hashes'] != b['audited_candidate_hashes']
    assert empty.pareto_witnesses


def test_audit_identity_does_not_confuse_transformed_or_simulation_policy_evidence():
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.film_synthesis_audit import frozen_synthesis_audit_identity

    service, base = setup_problem()
    frozen = freeze_film_synthesis_request(service, base)
    ordinary = frozen_synthesis_audit_identity(frozen, frozen.environment)
    transformed = frozen_synthesis_audit_identity(
        frozen, frozen.environment, response_transformed=True)
    simulation = frozen_synthesis_audit_identity(
        frozen, frozen.environment, optical_settings={'extrapolation_policy': 'nearest'})
    assert ordinary['physical_hash'] == transformed['physical_hash'] == simulation['physical_hash']
    assert len({ordinary['source_digest'], transformed['source_digest'], simulation['source_digest']}) == 3


def test_probe_keeps_known_absorption_and_dispersion_for_joint_r_t_targets():
    service, base = setup_problem()
    service.materials.records['high']['optical_sets'][0].update(
        n=[2.3, 2.2, 2.05], k=[.18, .15, .12])
    base = replace(base, objectives=(objective(520, 580, up=.2),
                                     objective(520, 580, up=.85, metric='T')))
    result = estimate_film_resources(
        service, Request(base, (base.environment,), layer_search_limit=0))
    assert result.state == 'completed', result.message
    assert result.pareto_witnesses
    for witness in result.pareto_witnesses:
        assert witness['targets_met'] and witness['within_constraints']
        assert not witness['conditional_on_assumption']
        assert all(row['targets_met'] for row in witness['audit']['objective_status'])
