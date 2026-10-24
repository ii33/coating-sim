"""Hybrid pipeline contracts: real archive, mass balance and endpoint ownership."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_synthesis_archive import SynthesisDesignArchive, physical_design_hash
from coat_sim.core.film_synthesis_refinement import (
    SynthesisStageBudget, weighted_sse_loss_gradient, synthesis_window_masses,
    refine_synthesis_explorer, refine_synthesis_full_minimax,
)
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def layer(identifier, material='high', thickness=50.):
    return dict(id=identifier, material_id=material, optical_set_id='nk',
                thickness=dict(value=thickness, unit='nm'), note='test')


def scored(design, maximum, windows=None):
    errors = [dict(objective_index=i, normalized_violation=value)
              for i, value in enumerate(windows or (maximum,))]
    return dict(design=deepcopy(design), score=dict(max_error=maximum,
        weighted_rms_error=maximum, errors=tuple(errors)))


def test_shared_archive_retains_actual_deletion_design_pareto_and_lineage():
    _, request = setup_problem()
    original = deepcopy(dict(request.environment))
    original['front_layers'] = [layer('a'), layer('b', 'low')]
    deleted = deepcopy(original)
    deleted['front_layers'].pop()
    archive = SynthesisDesignArchive(2, capacity=4)
    old = archive.add(scored(original, .1, (.1, .03)), provenance=dict(family='mirror'))
    winner = archive.add(scored(deleted, .09, (.09, .04)), parent_hash=old.physical_hash,
                         operator='physical_layer_deletion')
    assert winner is not None
    assert winner.design == deleted
    assert winner.parent_hash == physical_design_hash(original)
    assert {entry.physical_hash for entry in archive.pareto_entries()} == {old.physical_hash, winner.physical_hash}
    snapshot = archive.snapshot()
    row = next(row for row in snapshot['entries'] if row['operator'] == 'physical_layer_deletion')
    assert row['design'] == deleted and row['parent_hash'] == old.physical_hash
    deleted['front_layers'][0]['thickness']['value'] = 999.
    assert winner.design['front_layers'][0]['thickness']['value'] == 50.


def test_archive_capacity_protects_strict_champion_not_a_layer_target():
    _, request = setup_problem()
    archive = SynthesisDesignArchive(1, capacity=3)
    for n in (32, 70, 104, 105, 140):
        design = deepcopy(dict(request.environment))
        design['front_layers'] = [layer(str(i)) for i in range(n)]
        archive.add(scored(design, 1 / n), provenance=dict(family=str(n)))
    assert len(archive.entries) == 3
    assert min(entry.candidate['score']['max_error'] for entry in archive.entries.values()) == pytest.approx(1 / 140)
    assert any(len(entry.design['front_layers']) > 104 for entry in archive.entries.values())
    assert all('errors' not in entry.candidate['score'] for entry in archive.entries.values())


def _guide_value(request, design, actuals, derivatives=None):
    responses, errors = [], []
    for i, objective in enumerate(request.objectives):
        for angle in objective.angles_deg:
            actual = np.broadcast_to(actuals[i], (objective.wavelength_samples,)).copy()
            row = dict(objective_index=i, angle_deg=angle,
                wavelength_nm=np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm, objective.wavelength_samples),
                actual_values=actual)
            if derivatives is not None:
                row['jacobian_values_by_layer_id'] = {'x': np.broadcast_to(derivatives[i], actual.shape).copy()}
            responses.append(row)
            for value in actual:
                if objective.pointwise.mode == 'value':
                    violation = abs(value - objective.pointwise.target_value) / objective.normalization_scale
                else:
                    violation = max(0., 0. if objective.pointwise.lower_bound is None else objective.pointwise.lower_bound - value,
                                    0. if objective.pointwise.upper_bound is None else value - objective.pointwise.upper_bound) / objective.normalization_scale
                errors.append(dict(objective_index=i, normalized_violation=float(violation)))
    maximum = max(e['normalized_violation'] for e in errors)
    return dict(design=deepcopy(design), responses=responses,
                score=dict(max_error=maximum, weighted_rms_error=float(np.sqrt(np.mean([e['normalized_violation']**2 for e in errors]))),
                           errors=tuple(errors)))


def test_equal_window_sse_does_not_let_ir_point_count_dominate_and_keeps_user_weights():
    _, base = setup_problem()
    objectives = tuple(FilmOptimizationObjective(500., 600., count, pointwise=FilmObjectiveConstraint(mode='value', target_value=0., lower_bound=None, upper_bound=None), angles_deg=(0., 30.), weight=weight)
        for count, weight in ((2, 1.), (101, 3.)))
    request = replace(base, objectives=objectives)
    design = deepcopy(base.environment)
    design['front_layers'] = [layer('x')]
    value = _guide_value(request, design, [.2, .4], [1., 2.])
    loss, gradient = weighted_sse_loss_gradient(request, value, ('x',))
    assert synthesis_window_masses(request) == pytest.approx([.25, .75])
    assert loss == pytest.approx(.25 * .2**2 + .75 * .4**2)
    assert gradient == pytest.approx([2 * (.25 * .2 + .75 * .4 * 2)])
    denser = replace(request, objectives=tuple(replace(o, wavelength_samples=count)
        for o, count in zip(objectives, (17, 501), strict=True)))
    denser_value = _guide_value(denser, design, [.2, .4], [1., 2.])
    assert synthesis_window_masses(denser) == pytest.approx([.25, .75])
    assert weighted_sse_loss_gradient(denser, denser_value, ('x',))[0] == pytest.approx(loss)
    both = replace(request, objectives=(replace(objectives[0], mean=FilmObjectiveConstraint(
        mode='value', target_value=0., lower_bound=None, upper_bound=None)), objectives[1]))
    both_value = _guide_value(both, design, [.2, .4], [1., 2.])
    both_loss, both_gradient = weighted_sse_loss_gradient(both, both_value, ('x',))
    assert both_loss == pytest.approx(loss)
    assert both_gradient == pytest.approx(gradient)
    with pytest.raises(TypeError, match='objective_weighting'):
        replace(request, objective_weighting='point_count')
    incomplete = deepcopy(value)
    incomplete['responses'].pop()
    with pytest.raises(ValueError, match='missing original'):
        weighted_sse_loss_gradient(request, incomplete, ('x',))


def test_sse_real_terminal_continues_even_when_strict_max_worsens(monkeypatch):
    _, base = setup_problem()
    request = replace(base, objectives=tuple(FilmOptimizationObjective(550., 550., 1, pointwise=FilmObjectiveConstraint(mode='value', target_value=0., lower_bound=None, upper_bound=None)) for _ in range(2)))
    design = deepcopy(dict(request.environment))
    design['front_layers'] = [layer('x', thickness=1.)]
    def evaluate(trial, **kwargs):
        d = trial['front_layers'][0]['thickness']['value']
        return _guide_value(request, trial, [.6 + .1 * (d - 1), .6 * (2 - d)], [.1, -.6])
    initial = evaluate(design)
    champion = [initial]
    def archive(value, **kwargs):
        if value['score']['max_error'] < champion[0]['score']['max_error']:
            champion[0] = value
    def minimize(fun, x0, **kwargs):
        x = np.array([.02])  # phase scale 100 nm: actual terminal d=2 nm
        fun(x)
        kwargs['callback'](x)
        return SimpleNamespace(x=x, success=True, status=0, nit=1, message='test converged')
    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    terminal, diagnostics = refine_synthesis_explorer(request, initial,
        evaluate=evaluate, archive=archive)
    assert terminal['design']['front_layers'][0]['thickness']['value'] == 2.
    assert terminal['score']['max_error'] > champion[0]['score']['max_error']
    assert weighted_sse_loss_gradient(request, terminal)[0] < weighted_sse_loss_gradient(request, initial)[0]
    assert champion[0]['design'] == initial['design']
    assert diagnostics['endpoint_role'].startswith('actual_solver_terminal')


def test_sse_stage_budget_keeps_last_accepted_not_last_line_search(monkeypatch):
    _, base = setup_problem()
    request = replace(base, objectives=(FilmOptimizationObjective(550., 550., 1, pointwise=FilmObjectiveConstraint(mode='value', target_value=0., lower_bound=None, upper_bound=None)),))
    design = deepcopy(dict(request.environment))
    design['front_layers'] = [layer('x', thickness=1.)]
    def evaluate(trial, **kwargs):
        return _guide_value(request, trial, [trial['front_layers'][0]['thickness']['value']], [1.])
    def minimize(fun, x0, **kwargs):
        fun(np.array([.005]))
        kwargs['callback'](np.array([.005]))
        fun(np.array([.001]))  # better unaccepted line-search sample
        raise SynthesisStageBudget()
    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    terminal, diagnostics = refine_synthesis_explorer(request, evaluate(design),
        evaluate=evaluate, archive=lambda *a, **k: None)
    assert terminal['design']['front_layers'][0]['thickness']['value'] == .5
    assert diagnostics['termination'] == 'stage_budget'
    assert not diagnostics['success']


def test_full_slsqp_checks_every_original_constraint_and_not_optimizer_t(monkeypatch):
    _, base = setup_problem()
    request = replace(base, objectives=(FilmOptimizationObjective(500., 600., 3, pointwise=FilmObjectiveConstraint(mode='range', target_value=None, lower_bound=.9, upper_bound=None), angles_deg=(0., 30.)),))
    design = deepcopy(dict(request.environment))
    design['front_layers'] = [layer('x', thickness=1.)]
    def evaluate(trial, **kwargs):
        return _guide_value(request, trial, [.6], [.1])
    observed = {}
    def minimize(fun, x0, **kwargs):
        assert kwargs['method'] == 'SLSQP'
        constraints = kwargs['constraints'][-1]
        observed['constraints'] = len(constraints['fun'](x0))
        assert constraints['jac'](x0).shape == (6, 2)
        x = np.array([.01, 0.])  # fake solver success but violates all inequalities
        return SimpleNamespace(x=x, success=True, status=0, nit=1, message='claimed convergence')
    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    _, diagnostics = refine_synthesis_full_minimax(request, evaluate(design),
        evaluate=evaluate, archive=lambda *a, **k: None)
    assert observed['constraints'] == 6
    assert not diagnostics['success']
    assert diagnostics['termination'] == 'solver_failure'
    assert diagnostics['terminal_signed_max'] == pytest.approx(.3)
    assert not diagnostics['terminal_full_grid_epigraph_feasible']


def test_hybrid_reserves_real_needle_and_minimax_work_after_busy_prior_prefix(monkeypatch):
    service, request = setup_problem(upper=0., low=450., high=700., samples=7)
    request = replace(request, max_cycles=1, max_evaluations=100, max_layers=20, acceptable_normalized_error=0., search_strategy="hybrid")
    result = synthesis.synthesize_film_design(service, request)
    hybrid = result.performance['search']['hybrid']
    assert result.frozen_request.search_strategy == 'hybrid'
    assert hybrid['objective_masses'] == pytest.approx([1.])
    stages = {row['stage']: row for row in hybrid['stages']}
    assert stages['engineering_prior_screen']['evaluations_used'] < 100 // 3
    assert stages['analytic_needle_scan']['evaluations_used'] > 0
    assert 'full_original_slsqp_minimax' in stages
    assert hybrid['needle_scans'] and sum(row.get('probes', 0) for row in hybrid['needle_scans']) > 0
    assert any(row.stage == 'needle_probe' for row in result.trace)
    assert hybrid['last_full_minimax']['all_original_conditions']
    assert result.performance['search']['design_archive']['entries']


def test_explicit_resource_guard_reports_not_layer_infeasibility():
    service, request = setup_problem(upper=0., low=550., high=550., samples=1)
    request = replace(request, max_cycles=1, max_evaluations=40, max_working_set_bytes=1024, search_strategy="hybrid")
    result = synthesis.synthesize_film_design(service, request)
    assert result.state != 'failed'
    skips = result.performance['search']['hybrid']['resource_skips']
    assert skips and all('not_layer_infeasibility' in row['reason'] for row in skips)
    assert result.frozen_request.max_layers is None


def test_legacy_route_is_explicit_and_remains_callable():
    service, request = setup_problem(upper=.05)
    result = synthesis.synthesize_film_design(service, replace(request, search_strategy='legacy'))
    assert result.targets_met
    assert 'hybrid' not in result.performance['search']


def test_new_request_defaults_to_hybrid_without_hidden_layer_limit():
    _, base = setup_problem()
    request = synthesis.FilmSynthesisRequest(base.environment, base.objectives, base.candidate_materials)
    assert request.search_strategy == 'hybrid'
    assert not hasattr(request, 'objective_weighting')
    assert synthesis_window_masses(request) == pytest.approx([1.])
    assert request.max_layers is None
    assert request.minimum_nonzero_thickness_nm is None


def test_real_four_window_hybrid_keeps_original_conditions_and_shared_priors():
    service, base = setup_problem()
    objectives = tuple(FilmOptimizationObjective(wave, wave, 1, pointwise=FilmObjectiveConstraint(mode='range', target_value=None, lower_bound=.99 if index < 2 else None, upper_bound=None if index < 2 else .001), angles_deg=(0., 30.))
        for index, wave in enumerate((480., 520., 600., 650.)))
    request = replace(base, objectives=objectives, search_strategy='hybrid',
                      max_cycles=1, max_evaluations=140, acceptable_normalized_error=0.)
    result = synthesis.synthesize_film_design(service, request)
    assert result.state != 'failed', result.termination_reason
    hybrid = result.performance['search']['hybrid']
    assert hybrid['objective_masses'] == pytest.approx([.25] * 4)
    assert hybrid['original_grid_points'] == 8
    assert hybrid['needle_scans']
    entries = result.performance['search']['design_archive']['entries']
    assert any(entry['provenance'].get('family') in ('multispectral_hr', 'multispectral_matched_profile')
               for entry in entries)
    assert result.training_score is not None
    assert len(result.training_score['errors']) == 8
    if 'last_full_minimax' in hybrid:
        minimax = hybrid['last_full_minimax']
        assert minimax['original_signed_constraints'] == 8
        assert minimax['initial_physical_hash'] == hybrid['exploration_endpoints'][-1]['physical_hash']


def test_real_full_original_slsqp_improves_analytic_quarter_wave_without_t_only_acceptance():
    from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
    service, base = setup_problem(upper=0., low=550., high=550., samples=1, materials=('low',))
    request = synthesis.freeze_film_synthesis_request(service, replace(base, acceptable_normalized_error=1e-12))
    design = deepcopy(dict(request.environment))
    design['front_layers'] = [layer('x', 'low', 40.)]
    proxy = synthesis._proxy(request)
    def evaluate(trial, **kwargs):
        jacobian = kwargs.get('jacobian', False)
        return evaluate_frozen_structure_candidate(proxy, trial, variable_ids=('x',) if jacobian else (),
                                                   with_jacobian=jacobian)
    initial = evaluate(design, jacobian=True)
    refined, diagnostics = refine_synthesis_full_minimax(request, initial,
        evaluate=evaluate, archive=lambda *a, **k: None)
    assert refined['score']['max_error'] < initial['score']['max_error'] * 1e-6
    assert diagnostics['success'] and diagnostics['all_original_conditions']
    assert diagnostics['terminal_signed_max'] == pytest.approx(diagnostics['terminal_max_error'])
    assert refined['design']['front_layers'][0]['thickness']['value'] == pytest.approx(
        550 / (4 * np.sqrt(1.5)), abs=.01)


def test_shared_archive_binds_audit_status_and_retains_independent_audit_champion():
    _, request = setup_problem()
    archive = SynthesisDesignArchive(1, capacity=2)
    design = deepcopy(dict(request.environment))
    design['front_layers'] = [layer('audited')]
    training = scored(design, .3)
    archive.add(training)
    archive.mark_audited(training, scored(design, .01))
    audited_hash = physical_design_hash(design)
    for index in range(4):
        competitor = deepcopy(design)
        competitor['front_layers'][0]['thickness']['value'] += index + 1
        archive.add(scored(competitor, .2 - .01 * index))
    assert audited_hash in archive.entries
    row = next(row for row in archive.snapshot()['entries'] if row['physical_hash'] == audited_hash)
    assert row['audit_status'] == 'complete' and row['audit']['max_error'] == .01
    changed = deepcopy(design)
    changed['front_layers'][0]['thickness']['value'] += 10
    with pytest.raises(ValueError, match='exact physical'):
        archive.mark_audited(training, scored(changed, .01))


def test_hybrid_wall_slice_preserves_completed_partial_needle_scan(monkeypatch):
    from coat_sim.core import film_synthesis_batch_needle as batch
    service, base = setup_problem(upper=0., low=450., high=700., samples=7)
    request = replace(base, search_strategy='hybrid', acceptable_normalized_error=0.,
        max_cycles=1, max_evaluations=100, max_seconds=1000.)
    clock = [0.]
    monkeypatch.setattr(synthesis, 'monotonic', lambda: clock[0])
    original = batch.scan_batch_needles
    def scan(design, frozen, references, **kwargs):
        evaluate = kwargs['evaluate']
        def one_then_expire(*args, **options):
            value = evaluate(*args, **options)
            clock[0] = 260.  # local scan share250s; whole1000s request remains live
            return value
        kwargs['evaluate'] = one_then_expire
        return original(design, frozen, references, **kwargs)
    monkeypatch.setattr(batch, 'scan_batch_needles', scan)
    result = synthesis.synthesize_film_design(service, request)
    report = result.performance['search']['hybrid']['needle_scans'][0]
    assert report['probes'] == 1
    assert report['complete'] is False and report['stop_reason'] == 'stage_budget'
    assert sum(row.stage == 'needle_probe' for row in result.trace) == 1
    assert result.state != 'failed'


@pytest.mark.parametrize('fixed_nm', (10., 100.))
def test_real_scipy_all_fixed_thickness_bounds_are_valid_and_not_missing_nit(fixed_nm):
    service, base = setup_problem(upper=0., low=450., high=700., samples=11)
    request = replace(base, search_strategy='hybrid', minimum_nonzero_thickness_nm=fixed_nm,
        max_layer_thickness_nm=fixed_nm, max_total_thickness_nm=2 * fixed_nm,
        max_cycles=2, max_evaluations=120, acceptable_normalized_error=0.)
    result = synthesis.synthesize_film_design(service, request)
    assert result.state != 'failed', result.termination_reason
    assert result.candidate_design is not None
    assert all(row['thickness']['value'] == fixed_nm for row in result.candidate_design['front_layers'])


def test_sse_fixed_bound_diagnostics_use_actual_zero_free_variables():
    _, base = setup_problem()
    request = replace(base, minimum_nonzero_thickness_nm=10., max_layer_thickness_nm=10.,
        objectives=(FilmOptimizationObjective(550., 550., 1, pointwise=FilmObjectiveConstraint(mode='value', target_value=0., lower_bound=None, upper_bound=None)),))
    design = deepcopy(dict(request.environment))
    design['front_layers'] = [layer('x', thickness=10.)]
    def evaluate(trial, **kwargs):
        return _guide_value(request, trial, [.6], [.1])
    _, details = refine_synthesis_explorer(request, evaluate(design), evaluate=evaluate,
                                          archive=lambda *a, **k: None)
    assert details['termination'] == 'fixed_bounds' and details['success']
    assert details['iterations'] == 0 and details['free_thickness_variables'] == 0
    assert details['fixed_layer_ids'] == ['x']


def test_auto_resource_guard_admits_original_106_layer_full_grid_on_sufficient_memory(monkeypatch):
    from coat_sim.core import compute_resources
    _, request = setup_problem()
    request = replace(request, search_strategy='hybrid', max_working_set_bytes=0,
        objectives=tuple(FilmOptimizationObjective(low, high, samples, pointwise=FilmObjectiveConstraint(mode='range', target_value=None, lower_bound=None, upper_bound=.03), angles_deg=tuple(np.linspace(39., 51., 25)))
            for low, high, samples in ((580., 920., 341), (1044., 1084., 41),
                                      (3000., 5000., 2001), (7000., 12000., 5001))))
    monkeypatch.setattr(compute_resources, 'available_memory_bytes', lambda: 9_225_568_256)
    resolution = synthesis._resolve_synthesis_resource_guard(request)
    estimate = synthesis._synthesis_working_set_estimate_bytes(request, 106, jacobian=True, epigraph=True)
    assert estimate == 1_702_579_328  # 184,600 original signed constraints; no allocated dense matrix.
    assert synthesis.SYNTHESIS_WORKING_SET_FALLBACK_BYTES < estimate < resolution['effective_bytes']
    assert resolution == dict(version=1, mode='auto', requested_bytes=0,
        effective_bytes=4_612_784_128, available_bytes=9_225_568_256,
        basis='half_available_memory_at_freeze')


@pytest.mark.parametrize('version', ('v1', 'v2'))
def test_auto_resource_guard_uses_existing_host_cgroup_minimum(monkeypatch, version):
    from coat_sim.core import compute_resources
    _, request = setup_problem()
    values = {'/proc/meminfo': 'MemAvailable: 9000000 kB\n'}
    prefix = '/sys/fs/cgroup/memory/' if version == 'v1' else '/sys/fs/cgroup/'
    values[prefix + ('memory.limit_in_bytes' if version == 'v1' else 'memory.max')] = str(2 * 1024**3)
    values[prefix + ('memory.usage_in_bytes' if version == 'v1' else 'memory.current')] = str(512 * 1024**2)

    class MemoryPath:
        def __init__(self, path):
            self.path = path

        def read_text(self):
            if self.path not in values:
                raise OSError('not present')
            return values[self.path]

    monkeypatch.setattr(compute_resources, 'Path', MemoryPath)
    monkeypatch.setattr(compute_resources.os, 'name', 'posix')
    resolution = synthesis._resolve_synthesis_resource_guard(replace(request, max_working_set_bytes=0))
    assert resolution['available_bytes'] == 1536 * 1024**2
    assert resolution['effective_bytes'] == 768 * 1024**2
    assert resolution['basis'] == 'half_available_memory_at_freeze'


@pytest.mark.parametrize('measurement', (None, -1, False, 1.5, 'unknown'))
def test_auto_resource_guard_unknown_or_invalid_memory_is_transparent_fallback(monkeypatch, measurement):
    from coat_sim.core import compute_resources
    _, request = setup_problem()
    monkeypatch.setattr(compute_resources, 'available_memory_bytes', lambda: measurement)
    resolution = synthesis._resolve_synthesis_resource_guard(replace(request, max_working_set_bytes=0))
    assert resolution['effective_bytes'] == synthesis.SYNTHESIS_WORKING_SET_FALLBACK_BYTES
    assert resolution['available_bytes'] is None
    assert resolution['basis'] == ('unknown_memory_fallback' if measurement is None else 'invalid_memory_fallback')


def test_auto_resource_guard_probe_error_and_zero_headroom_are_not_assumed_sufficient(monkeypatch):
    from coat_sim.core import compute_resources
    _, request = setup_problem()
    request = replace(request, max_working_set_bytes=0)

    def unavailable():
        raise OSError('memory statistics unavailable')

    monkeypatch.setattr(compute_resources, 'available_memory_bytes', unavailable)
    fallback = synthesis._resolve_synthesis_resource_guard(request)
    assert fallback['basis'] == 'memory_probe_error_fallback'
    assert fallback['effective_bytes'] == synthesis.SYNTHESIS_WORKING_SET_FALLBACK_BYTES
    monkeypatch.setattr(compute_resources, 'available_memory_bytes', lambda: 0)
    zero = synthesis._resolve_synthesis_resource_guard(request)
    assert zero['available_bytes'] == zero['effective_bytes'] == 0
    assert zero['basis'] == 'half_available_memory_at_freeze'


@pytest.mark.parametrize('limit', (1024, 1_610_612_736, 3_221_225_472, None))
def test_current_explicit_resource_cap_or_null_replay_is_exact_and_missing_field_is_rejected(monkeypatch, limit):
    from coat_sim.core import compute_resources
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload, film_synthesis_request_from_payload
    service, request = setup_problem()
    measurements = []
    monkeypatch.setattr(compute_resources, 'available_memory_bytes', lambda: measurements.append('unexpected'))
    frozen = synthesis.freeze_film_synthesis_request(service, replace(request, max_working_set_bytes=limit))
    payload = film_synthesis_request_to_payload('current-explicit-resource-replay', frozen)
    _, old, _ = film_synthesis_request_from_payload(payload)
    replay = synthesis.freeze_film_synthesis_request(None, old)
    assert replay.max_working_set_bytes == limit
    assert replay.resource_guard_resolution is not None
    assert replay.resource_guard_resolution['requested_bytes'] == limit
    assert replay.resource_guard_resolution['effective_bytes'] == limit
    assert replay.resource_guard_resolution['mode'] == ('disabled' if limit is None else 'explicit')
    payload['request'].pop('resource_guard_resolution')
    with pytest.raises(ValueError, match='requires every field'):
        film_synthesis_request_from_payload(payload)
    assert not measurements


def test_frozen_auto_resource_resolution_is_deterministic_replay_and_can_be_explicitly_reset(monkeypatch):
    from coat_sim.core import compute_resources
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload, film_synthesis_request_from_payload
    service, request = setup_problem(upper=0., low=450., high=700., samples=7)
    measurements = []

    def measure():
        measurements.append(1)
        return 8 * 1024**3 if len(measurements) == 1 else 2 * 1024**3

    monkeypatch.setattr(compute_resources, 'available_memory_bytes', measure)
    request = replace(request, search_strategy='hybrid', max_working_set_bytes=0,
                      max_cycles=1, max_evaluations=40, max_layers=10)
    frozen = synthesis.freeze_film_synthesis_request(service, request)
    _, decoded, _ = film_synthesis_request_from_payload(film_synthesis_request_to_payload('auto-replay', frozen))
    replay = synthesis.freeze_film_synthesis_request(None, decoded)
    first = synthesis.synthesize_film_design(None, frozen)
    second = synthesis.synthesize_film_design(None, replay)
    assert measurements == [1]
    assert frozen.resource_guard_resolution == replay.resource_guard_resolution
    assert first.state == second.state and first.termination_reason == second.termination_reason
    assert first.evaluations == second.evaluations
    assert first.candidate_design is not None and second.candidate_design is not None
    assert physical_design_hash(first.candidate_design) == physical_design_hash(second.candidate_design)
    hybrid = second.performance['search']['hybrid']
    assert hybrid['requested_max_working_set_bytes'] == 0
    assert hybrid['effective_max_working_set_bytes'] == 4 * 1024**3
    assert hybrid['resource_guard_resolution'] == frozen.resource_guard_resolution
    assert hybrid['resource_limited'] is False
    reset = synthesis.freeze_film_synthesis_request(None, replace(replay, resource_guard_resolution=None))
    assert measurements == [1, 1]
    assert reset.resource_guard_resolution is not None
    assert reset.resource_guard_resolution['effective_bytes'] == 1024**3


@pytest.mark.parametrize('new_limit', (1024, None))
def test_editing_auto_requested_resource_policy_invalidates_stale_resolution(monkeypatch, new_limit):
    from coat_sim.core import compute_resources
    service, request = setup_problem()
    monkeypatch.setattr(compute_resources, 'available_memory_bytes', lambda: 8 * 1024**3)
    frozen = synthesis.freeze_film_synthesis_request(service, replace(request, max_working_set_bytes=0))
    changed = synthesis._validated(replace(frozen, max_working_set_bytes=new_limit))
    assert changed.resource_guard_resolution is None
    replay = synthesis.freeze_film_synthesis_request(None, changed)
    assert replay.resource_guard_resolution is not None
    assert replay.resource_guard_resolution['effective_bytes'] == new_limit


@pytest.mark.parametrize('changes', ({'version': 2}, {'effective_bytes': 100}, {'available_bytes': False},
                                     {'basis': []}, {'mode': {}}))
def test_resource_resolution_rejects_unknown_version_or_inconsistent_frozen_policy(changes):
    _, request = setup_problem()
    resolution = dict(version=1, mode='auto', requested_bytes=0, effective_bytes=4000,
                      available_bytes=8000, basis='half_available_memory_at_freeze')
    resolution.update(changes)
    with pytest.raises(ValueError, match='resource_guard_resolution'):
        synthesis._validated(replace(request, max_working_set_bytes=0, resource_guard_resolution=resolution))
