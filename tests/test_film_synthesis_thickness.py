"""Canonical fixed-topology cost reduction: no margin or tolerance acceptance."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import numpy as np
import pytest

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint, reduce_objective_responses
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_scoring import reduction_score
from coat_sim.core import film_synthesis_thickness as thickness
from coat_sim.core.film_synthesis_refinement import SynthesisStageBudget, SynthesisResourceGuard

pytestmark = pytest.mark.algorithm


def fixture(mode='both', *, strict=False, initial_nm=8., total_cap=None):
    objective = FilmOptimizationObjective(500., 600., 3,
        mean=FilmObjectiveConstraint(enabled=mode != 'pointwise', lower_bound=.56 if mode == 'both' else .4,
                                     upper_bound=None, strict_lower=strict),
        pointwise=FilmObjectiveConstraint(enabled=mode != 'mean', lower_bound=.12, upper_bound=None,
                                          strict_lower=strict))
    request = FilmSynthesisRequest({}, (objective,), (), minimum_nonzero_thickness_nm=.1,
        max_layer_thickness_nm=10., max_total_thickness_nm=total_cap, acceptable_normalized_error=1.)
    design = dict(front_layers=[dict(id='d', material_id='m', optical_set_id='nk',
                                    thickness=dict(value=initial_nm, unit='nm'))])
    calls = []
    def evaluate(candidate, *, jacobian):
        assert jacobian
        d = candidate['front_layers'][0]['thickness']['value']
        assert .1 <= d <= 10.
        assert total_cap is None or d <= total_cap
        calls.append(d)
        responses = [dict(objective_index=0, angle_deg=0., wavelength_nm=[500., 550., 600.],
            actual_values=(d * np.array([.02, .1, .1])).tolist(),
            jacobian_values_by_layer_id={'d': [.02, .1, .1]})]
        reduced = reduce_objective_responses(request.objectives, responses, layer_ids=('d',), tolerance=1.)
        return dict(design=deepcopy(candidate), responses=responses, score=reduction_score(reduced, 1.))
    initial = evaluate(design, jacobian=True)
    calls.clear()
    return request, initial, evaluate, calls


def invoke(request, initial, evaluate, **kwargs):
    return thickness.reduce_total_thickness(request, initial, evaluate=evaluate, archive=lambda *a, **k: None, **kwargs)


@pytest.mark.parametrize('mode, expected', [('mean', 5.), ('pointwise', 6.), ('both', 7.)])
@pytest.mark.parametrize('strict', [False, True])
def test_real_slsqp_preserves_independent_canonical_goals(mode, expected, strict):
    request, initial, evaluate, calls = fixture(mode, strict=strict)
    original = deepcopy(initial)
    archived = []
    best, details = thickness.reduce_total_thickness(request, initial, evaluate=evaluate,
        archive=lambda value, *, audit_eligible: archived.append((value, audit_eligible)))
    assert initial == original and best['score']['nominal_pass']
    assert best['design']['front_layers'][0]['thickness']['value'] == pytest.approx(expected, abs=(8. - expected) / 256. + 1e-12)
    assert details['final_total_thickness_nm'] < details['initial_total_thickness_nm']
    assert details['physical_candidates'] == len(calls) == len(archived)
    assert all(flag is False for _, flag in archived)
    assert details['boundary_nextafter_evaluations'] <= 4 and details['boundary_bisection_evaluations'] <= 8
    assert details['max_signed_violation'] <= 0.
    assert details['optical_extrema'][0]['sampled_min'] == min(best['responses'][0]['actual_values'])
    json.dumps(details, allow_nan=False)
    if mode == 'mean':
        assert min(best['responses'][0]['actual_values']) < request.objectives[0].mean.lower_bound


def test_tolerated_initial_miss_cannot_enter_reduction():
    request, initial, evaluate, calls = fixture(initial_nm=6.9)
    assert initial['score']['solver_acceptable'] and not initial['score']['nominal_pass']
    initial['score']['nominal_pass'] = True  # Even a forged flag cannot replace raw spectra.
    with pytest.raises(ValueError, match='nominally passing'):
        invoke(request, initial, evaluate)
    assert not calls


def test_nominal_fallback_does_not_trust_optimizer_success(monkeypatch):
    request, initial, evaluate, calls = fixture()
    def minimize(fun, x0, **kwargs):
        return SimpleNamespace(x=np.array([.069]), success=True, message='loose optimum', nit=1)
    monkeypatch.setattr(thickness.import_module('scipy.optimize'), 'minimize', minimize)
    best, details = invoke(request, initial, evaluate)
    assert details['solver_success'] and details['terminal_nominal_pass'] is False
    assert best['score']['nominal_pass'] and 7. <= details['final_total_thickness_nm'] < 8.
    assert details['boundary_nextafter_evaluations'] == 4
    assert details['boundary_bisection_evaluations'] == 8
    assert len(calls) == 13


def test_infeasible_trials_never_reach_parent_optics(monkeypatch):
    request, initial, evaluate, calls = fixture(total_cap=8.)
    def minimize(fun, x0, *, constraints, **kwargs):
        for x in (np.array([.11]), np.array([.09]), np.array([-.01])):
            assert np.all(constraints[0]['fun'](x) < 0.)
            assert np.all(np.isfinite(constraints[0]['jac'](x)))
        return SimpleNamespace(x=np.array([.09]), success=False, message='illegal endpoint', nit=1)
    monkeypatch.setattr(thickness.import_module('scipy.optimize'), 'minimize', minimize)
    best, details = invoke(request, initial, evaluate)
    assert best is initial and not calls
    assert details['rejected_infeasible_trials'] == 6
    assert details['terminal_physically_feasible'] is False
    assert details['terminal_nominal_pass'] is None


def test_one_point_cache_and_exact_initial_nm_roundtrip(monkeypatch):
    request, initial, evaluate, calls = fixture(initial_nm=7.200000000000001)
    def minimize(fun, x0, *, jac, constraints, **kwargs):
        assert fun(x0) == initial['design']['front_layers'][0]['thickness']['value'] / 100.
        jac(x0)
        for c in constraints:
            c['fun'](x0)
            c['jac'](x0)
        trial = x0 - .0001
        constraints[0]['fun'](trial)
        constraints[0]['jac'](trial)
        assert len(calls) == 1
        constraints[0]['fun'](x0)
        assert len(calls) == 2  # No unbounded history cache.
        return SimpleNamespace(x=x0, success=True, message='probe', nit=0)
    monkeypatch.setattr(thickness.import_module('scipy.optimize'), 'minimize', minimize)
    _, details = invoke(request, initial, evaluate)
    assert details['cache_hits'] >= 4


@pytest.mark.parametrize('exception', [SynthesisStageBudget, SynthesisResourceGuard, KeyboardInterrupt])
def test_parent_boundaries_propagate_before_any_work(exception):
    request, initial, evaluate, calls = fixture()
    def stop():
        raise exception('parent boundary')
    with pytest.raises(exception, match='parent boundary'):
        invoke(request, initial, evaluate, cancel_check=stop)
    assert not calls


def test_cached_callbacks_remain_cancellable(monkeypatch):
    request, initial, evaluate, calls = fixture()
    stopped = False
    def check():
        if stopped:
            raise SynthesisStageBudget('cached boundary')
    def minimize(fun, x0, *, constraints, **kwargs):
        nonlocal stopped
        constraints[0]['fun'](x0)
        stopped = True
        constraints[0]['jac'](x0)
    monkeypatch.setattr(thickness.import_module('scipy.optimize'), 'minimize', minimize)
    with pytest.raises(SynthesisStageBudget, match='cached boundary'):
        invoke(request, initial, evaluate, cancel_check=check)
    assert not calls


@pytest.mark.parametrize('missing', ['responses', 'jacobian'])
def test_missing_initial_derivatives_refresh_exact_design_through_parent(missing, monkeypatch):
    request, initial, evaluate, calls = fixture()
    if missing == 'responses':
        initial.pop('responses')
    else:
        initial['responses'][0].pop('jacobian_values_by_layer_id')
    monkeypatch.setattr(thickness.import_module('scipy.optimize'), 'minimize',
        lambda fun, x0, **kwargs: SimpleNamespace(x=x0, success=True, message='probe'))
    best, details = invoke(request, initial, evaluate)
    assert calls == [8.] and details['physical_candidates'] == 1
    assert best['design'] == initial['design'] and best['score']['nominal_pass']


def test_foreign_parent_design_rejected():
    request, initial, evaluate, calls = fixture()
    initial.pop('responses')
    def foreign(candidate, **kwargs):
        value = evaluate(candidate, **kwargs)
        value['design']['front_layers'][0]['material_id'] = 'foreign'
        return value
    with pytest.raises(ValueError, match='another design'):
        invoke(request, initial, foreign)
    assert calls == [8.]


def test_resource_guard_before_optics_and_dense_solver(monkeypatch):
    request, initial, evaluate, calls = fixture()
    request = replace(request, max_working_set_bytes=1)
    monkeypatch.setattr(thickness.import_module('scipy.optimize'), 'minimize',
                        lambda *a, **k: pytest.fail('not admitted'))
    with pytest.raises(SynthesisResourceGuard, match='estimated working set'):
        invoke(request, initial, evaluate)
    assert not calls


def test_parent_archive_storage_guard_is_not_bypassed():
    request, initial, evaluate, calls = fixture()
    def resource(candidate, *, jacobian, epigraph):
        assert jacobian and epigraph
        raise SynthesisResourceGuard('retained archive')
    with pytest.raises(SynthesisResourceGuard, match='retained archive'):
        invoke(request, initial, evaluate, resource_check=resource)
    assert not calls


def test_fixed_bounds_and_empty_topology_have_no_optical_work():
    request, initial, evaluate, calls = fixture()
    best, details = invoke(replace(request, minimum_nonzero_thickness_nm=8., max_layer_thickness_nm=8.), initial, evaluate)
    assert best is initial and details['termination'] == 'fixed_bounds' and not calls
    initial = deepcopy(initial)
    initial['design']['front_layers'] = []
    for row in initial['responses']:
        row['jacobian_values_by_layer_id'] = {}
    best, details = invoke(request, initial, evaluate)
    assert best is initial and details['termination'] == 'empty_topology'
    assert details['final_total_thickness_nm'] == 0. and not calls


@pytest.mark.parametrize('field,value', [('max_iterations', 0), ('max_iterations', True)])
def test_iteration_contract(field, value):
    request, initial, evaluate, _ = fixture()
    with pytest.raises(ValueError, match='positive integer'):
        invoke(request, initial, evaluate, **{field: value})


def test_exact_target_does_not_acquire_hidden_tolerance(monkeypatch):
    request, initial, evaluate, _ = fixture('mean')
    obj = replace(request.objectives[0], mean=FilmObjectiveConstraint(mode='value', target_value=.64,
                                                                  lower_bound=None, upper_bound=None))
    request = replace(request, objectives=(obj,))
    def evaluate_exact(candidate, **kwargs):
        value = evaluate(candidate, **kwargs)
        reduced = reduce_objective_responses(request.objectives, value['responses'], layer_ids=('d',), tolerance=1.)
        value['score'] = reduction_score(reduced, 1.)
        return value
    initial = evaluate_exact(initial['design'], jacobian=True)
    best, details = invoke(request, initial, evaluate_exact)
    assert best is initial and details['reduction_nm'] == 0.
    assert best['score']['nominal_pass']


def physical_fixture(family, mode):
    """Real production TMM, synthetic nondispersive materials, no measured claims."""
    from tests.test_film_auto_synthesis import setup_problem
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request, _proxy
    from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
    service, base = setup_problem()
    def band(low, high, metric, lower=None, upper=None):
        c = FilmObjectiveConstraint(lower_bound=lower, upper_bound=upper)
        return FilmOptimizationObjective(low, high, 9, metric=metric, angles_deg=(0., 15.),
            polarization='unpolarized', mean=replace(c, enabled=mode != 'pointwise'),
            pointwise=replace(c, enabled=mode != 'mean'))
    objectives: tuple[FilmOptimizationObjective, ...]
    if family == 'HR':
        objectives = (band(530., 570., 'R', lower=.95),)
        materials = ['high', 'low'] * 6
    elif family == 'AR':
        objectives = (band(520., 580., 'R', upper=.01),)
        materials = ['low']
    else:
        objectives = (band(500., 550., 'R', lower=.12), band(600., 650., 'T', lower=.6))
        materials = ['high']
    request = freeze_film_synthesis_request(service, replace(base, objectives=objectives,
        minimum_nonzero_thickness_nm=1., max_layer_thickness_nm=150.,
        max_layers=None, max_total_thickness_nm=None, acceptable_normalized_error=.5))
    design = deepcopy(dict(request.environment))
    indices = {'high': 2.2, 'low': np.sqrt(1.5)}
    design['front_layers'] = [dict(id=f'layer-{i}', material_id=m, optical_set_id='nk',
        thickness=dict(value=550. / (4. * indices[m]), unit='nm'), note='synthetic TMM test')
        for i, m in enumerate(materials)]
    ids = tuple(row['id'] for row in design['front_layers'])
    proxy = _proxy(request)
    calls = []
    def evaluate(candidate, *, jacobian):
        calls.append(deepcopy(candidate))
        return evaluate_frozen_structure_candidate(proxy, candidate, variable_ids=ids if jacobian else (),
                                                   with_jacobian=jacobian)
    initial = evaluate(design, jacobian=True)
    calls.clear()
    return service, request, initial, evaluate, calls


@pytest.mark.parametrize('family', ['HR', 'AR', 'mixed_RT'])
@pytest.mark.parametrize('mode', ['mean', 'pointwise', 'both'])
def test_real_tmm_cross_family_fixed_topology_reduction(family, mode):
    service, request, initial, evaluate, calls = physical_fixture(family, mode)
    assert initial['score']['nominal_pass'], (family, mode, initial['score']['max_error'])
    original = deepcopy(initial)
    best, details = invoke(request, initial, evaluate, max_iterations=24)
    assert initial == original and best['score']['nominal_pass']
    assert details['reduction_nm'] > 0., (family, mode, details)
    assert details['physical_candidates'] == len(calls) > 0
    before = [(r['id'], r['material_id'], r['optical_set_id']) for r in initial['design']['front_layers']]
    after = [(r['id'], r['material_id'], r['optical_set_id']) for r in best['design']['front_layers']]
    assert before == after
    # Independent direct forward on exactly the original training conditions,
    # rather than a second use of the optimizer's saved score/Jacobian.
    from coat_sim.core.film_design_service import simulate_frozen
    responses = []
    for i, objective in enumerate(request.objectives):
        for angle in objective.angles_deg:
            frozen = service.freeze_design(best['design'], dict(wavelength_min_nm=objective.wavelength_min_nm,
                wavelength_max_nm=objective.wavelength_max_nm, samples=objective.wavelength_samples,
                angle_deg=angle, polarization=objective.polarization))
            direct = simulate_frozen('.', frozen)
            responses.append(dict(objective_index=i, angle_deg=angle, wavelength_nm=direct['wavelength_nm'],
                                  actual_values=direct[objective.metric]))
    checked = reduce_objective_responses(request.objectives, responses)
    assert checked.nominal_pass, (family, mode, checked.max_error)


def test_initial_cost_sublevel_excludes_no_improving_candidate_and_rejects_thicker_branch(monkeypatch):
    request, initial, evaluate, calls = fixture()
    def minimize(fun, x0, *, constraints, **kwargs):
        # No user total cap exists. This is the feasible incumbent's objective
        # sublevel, not an invented manufacturing constraint.
        sublevel = constraints[-1]
        assert sublevel['fun'](x0) == 0.
        assert sublevel['fun'](np.array([.09])) < 0.
        assert sublevel['fun'](np.array([.07])) > 0.
        np.testing.assert_array_equal(sublevel['jac'](x0), [-1.])
        return SimpleNamespace(x=np.array([.09]), success=True, message='ignored constraint', nit=1)
    monkeypatch.setattr(thickness.import_module('scipy.optimize'), 'minimize', minimize)
    best, detail = invoke(request, initial, evaluate)
    assert best is initial and detail['reduction_nm'] == 0.
    assert detail['cost_sublevel_total_nm'] == 8.
    assert detail['terminal_total_thickness_nm'] == 9.
    assert request.max_total_thickness_nm is None and calls == [9.]


def test_parent_archive_failure_after_completed_physics_propagates():
    request, initial, evaluate, calls = fixture()
    archived = []
    def archive(value, *, audit_eligible):
        archived.append(value)
        raise SynthesisStageBudget('archive boundary')
    with pytest.raises(SynthesisStageBudget, match='archive boundary'):
        thickness.reduce_total_thickness(request, initial, evaluate=evaluate, archive=archive)
    assert len(archived) == len(calls) == 1


@pytest.mark.parametrize('bad', [np.array([np.nan]), np.array([np.inf]), np.array([1., 2.]), np.array([1e308])])
def test_invalid_solver_endpoint_rejected_without_physics(monkeypatch, bad):
    request, initial, evaluate, calls = fixture()
    monkeypatch.setattr(thickness.import_module('scipy.optimize'), 'minimize',
        lambda *a, **k: SimpleNamespace(x=bad, success=True, message='invalid'))
    with pytest.raises(ValueError, match='invalid coordinates|nonfinite physical'):
        invoke(request, initial, evaluate)
    assert not calls


def test_dense_mean_audit_remains_independent_and_can_reject_training_pass():
    from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
    _, request, initial, evaluate, _ = physical_fixture('AR', 'mean')
    best, details = invoke(request, initial, evaluate, max_iterations=24)
    audit = audit_frozen_synthesis_candidate(request, best['design'])
    assert best['score']['nominal_pass'] and details['nominal_pass']
    assert not audit['score']['nominal_pass'] and audit['score']['max_error'] > 0.
    assert 'independent original audit required' in details['acceptance_scope']
    # The independent audit is not allowed to mutate or relax input goals.
    assert request.objectives[0].mean.upper_bound == .01
    assert not request.objectives[0].pointwise.enabled
