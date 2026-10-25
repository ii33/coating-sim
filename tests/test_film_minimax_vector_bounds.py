"""Per-layer minimax bound contracts using synthetic responses, never optics."""
from copy import deepcopy
from dataclasses import replace
import math
from types import SimpleNamespace

import numpy as np
import pytest

from coat_sim.core.film_objective_criteria import (
    FilmObjectiveConstraint, reduce_objective_responses,
)
from coat_sim.core.film_optimization_scoring import reduction_score
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_refinement import (
    SynthesisResourceGuard, SynthesisStageBudget, refine_synthesis_full_minimax,
)
from tests.test_film_synthesis_band_criteria import problem

pytestmark = pytest.mark.algorithm


def _problem(thicknesses=(3., 4.), *, minimum=.1, upper=6., total=12., layer_ids=('a', 'b')):
    request, initial, _, _ = problem()
    objective = replace(request.objectives[0],
        pointwise=FilmObjectiveConstraint(mode='value', target_value=0.,
            lower_bound=None, upper_bound=None),
        mean=FilmObjectiveConstraint(enabled=False))
    request = replace(request, objectives=(objective,), minimum_nonzero_thickness_nm=minimum,
        max_layer_thickness_nm=upper, max_total_thickness_nm=total)
    design = deepcopy(initial['design'])
    design['front_layers'] = [dict(id=identifier, material_id='m', optical_set_id='nk',
        thickness=dict(value=value, unit='nm'))
        for identifier, value in zip(layer_ids, thicknesses, strict=True)]
    calls, archived, events = [], [], []

    def evaluate(candidate, *, jacobian=True):
        ds = [row['thickness']['value'] for row in candidate['front_layers']]
        calls.append(tuple(ds))
        responses = [dict(objective_index=0, angle_deg=0.,
            wavelength_nm=[500., 550., 600.], actual_values=[.01 * sum(ds)] * 3,
            jacobian_values_by_layer_id={row['id']: [.01] * 3
                for row in candidate['front_layers']})]
        reduction = reduce_objective_responses(request.objectives, responses,
            layer_ids=tuple(row['id'] for row in candidate['front_layers']), tolerance=0.)
        return dict(design=deepcopy(candidate), responses=responses,
                    score=reduction_score(reduction, 0.))

    initial = evaluate(design)
    calls.clear()
    kwargs = dict(evaluate=evaluate,
        archive=lambda value, **kw: archived.append((value, kw)),
        progress=lambda *args: events.append(args))
    return request, initial, kwargs, calls, archived, events


def _outcome(x, *, success=True):
    return SimpleNamespace(x=np.asarray(x, float), success=success, status=0, nit=1,
                           message='synthetic endpoint')


@pytest.mark.parametrize('bounds', [[], {}, {'a': 1.}, {'a': 1., 'b': 2., 'extra': 3.},
    {'A': 1., 'b': 2.}, {0: 1., 'b': 2.}])
def test_vector_mapping_requires_exact_layer_ids_before_callbacks(monkeypatch, bounds):
    request, initial, kwargs, calls, archived, events = _problem()
    monkeypatch.setattr('scipy.optimize.minimize',
        lambda *a, **kw: pytest.fail('invalid bounds reached solver'))
    with pytest.raises(ValueError, match='exact unique current layer IDs'):
        refine_synthesis_full_minimax(request, initial, lower_bounds_nm=bounds, **kwargs)
    assert not calls and not archived and not events


@pytest.mark.parametrize('bad', [True, False, np.bool_(True), float('nan'), float('inf'),
    -float('inf'), 0., -1., '1', None, 1j, 10**1000])
def test_vector_minima_require_finite_positive_nonbool_numbers(bad):
    request, initial, kwargs, calls, archived, events = _problem()
    with pytest.raises(ValueError, match='finite positive numbers'):
        refine_synthesis_full_minimax(request, initial, lower_bounds_nm={'a': bad, 'b': 2.},
                                     **kwargs)
    assert not calls and not archived and not events


@pytest.mark.parametrize('ids', [('a', 'a'), ('a', 1)])
def test_vector_mapping_rejects_ambiguous_current_identity(ids):
    request, initial, kwargs, calls, archived, events = _problem()
    for row, identifier in zip(initial['design']['front_layers'], ids, strict=True):
        row['id'] = identifier
    with pytest.raises(ValueError, match='exact unique current layer IDs'):
        refine_synthesis_full_minimax(request, initial,
            lower_bounds_nm={identifier: 1. for identifier in ids}, **kwargs)
    assert not calls and not archived and not events


def test_empty_topology_still_requires_exact_supplied_keys():
    request, initial, kwargs, calls, archived, events = _problem()
    initial['design']['front_layers'] = []
    result, detail = refine_synthesis_full_minimax(request, initial, lower_bounds_nm={}, **kwargs)
    assert result is initial and detail['termination'] == 'empty_topology'
    with pytest.raises(ValueError, match='exact unique current layer IDs'):
        refine_synthesis_full_minimax(request, initial, lower_bounds_nm={'a': 1.}, **kwargs)
    assert not calls and not archived and not events


@pytest.mark.parametrize('bounds', [{'a': .09, 'b': 1.}, {'a': 1., 'b': 6.01}])
def test_vector_minima_cannot_relax_native_lower_or_exceed_native_upper(bounds):
    request, initial, kwargs, calls, archived, events = _problem()
    with pytest.raises(ValueError, match='native layer limits'):
        refine_synthesis_full_minimax(request, initial, lower_bounds_nm=bounds, **kwargs)
    assert not calls and not archived and not events


def test_vector_minimum_respects_native_positive_tiny_when_scalar_is_absent():
    request, initial, kwargs, calls, _, _ = _problem(minimum=None)
    with pytest.raises(ValueError, match='native layer limits'):
        refine_synthesis_full_minimax(request, initial,
            lower_bounds_nm={'a': np.nextafter(0., 1.), 'b': 1.}, **kwargs)
    assert not calls


def test_vector_minima_cannot_exceed_unchanged_total_cap():
    request, initial, kwargs, calls, archived, events = _problem(total=7.)
    with pytest.raises(ValueError, match='native total thickness limit'):
        refine_synthesis_full_minimax(request, initial,
            lower_bounds_nm={'a': 4., 'b': 4.}, **kwargs)
    assert not calls and not archived and not events


@pytest.mark.parametrize('thicknesses,total', [((.5, 4.), 12.), ((3., 7.), 12.),
    ((3., 4.), 6.), ((float('nan'), 4.), 12.), ((float('inf'), 4.), 12.)])
def test_vector_initial_physical_feasibility_checked_even_with_cached_jacobians(thicknesses, total):
    request, initial, kwargs, calls, archived, events = _problem(total=total)
    for row, thickness in zip(initial['design']['front_layers'], thicknesses, strict=True):
        row['thickness']['value'] = thickness
    with pytest.raises(ValueError, match='feasible initial physical thicknesses'):
        refine_synthesis_full_minimax(request, initial,
            lower_bounds_nm={'a': 1., 'b': 2.}, **kwargs)
    assert not calls and not archived and not events


@pytest.mark.parametrize('unit', ['QW', 'um', 'NM', None, 'missing'])
def test_vector_initial_requires_explicit_nm_units_before_cache_or_solver(monkeypatch, unit):
    request, initial, kwargs, calls, archived, events = _problem()
    thickness = initial['design']['front_layers'][0]['thickness']
    if unit == 'missing':
        thickness.pop('unit')
    else:
        thickness['unit'] = unit
    monkeypatch.setattr('scipy.optimize.minimize',
        lambda *a, **kw: pytest.fail('non-nm initial thickness reached solver'))
    with pytest.raises(ValueError, match='initial physical thicknesses in nm'):
        refine_synthesis_full_minimax(request, initial,
            lower_bounds_nm={'a': 1., 'b': 2.}, **kwargs)
    assert not calls and not archived and not events


@pytest.mark.parametrize('value', [True, False, np.bool_(True), np.bool_(False)])
def test_vector_initial_rejects_boolean_values_before_array_conversion(monkeypatch, value):
    request, initial, kwargs, calls, archived, events = _problem()
    initial['design']['front_layers'][0]['thickness']['value'] = value
    monkeypatch.setattr('scipy.optimize.minimize',
        lambda *a, **kw: pytest.fail('boolean initial thickness reached solver'))
    with pytest.raises(ValueError, match='nonboolean initial thicknesses'):
        refine_synthesis_full_minimax(request, initial,
            lower_bounds_nm={'a': 1., 'b': 2.}, **kwargs)
    assert not calls and not archived and not events


@pytest.mark.parametrize('all_fixed', [False, True])
def test_real_scipy_vector_bounds_preserve_fixed_free_variables(all_fixed):
    request, initial, kwargs, calls, archived, events = _problem((6., 6. if all_fixed else 4.))
    minima = {'b': 6. if all_fixed else 2., 'a': 6.}  # Deliberately not row order.
    best, detail = refine_synthesis_full_minimax(request, initial, lower_bounds_nm=minima, **kwargs)
    ds = [row['thickness']['value'] for row in best['design']['front_layers']]
    assert ds == pytest.approx([6., minima['b']])
    assert detail['success']
    assert detail['fixed_layer_ids'] == (['a', 'b'] if all_fixed else ['a'])
    assert detail['free_thickness_variables'] == (0 if all_fixed else 1)
    assert all(a == 6. and minima['b'] <= b <= 6. and a + b <= 12. for a, b in calls)
    assert len(calls) == len(archived) == len(events) == detail['physical_candidates']
    assert all(kw == {'audit_eligible': False} for _, kw in archived)
    assert all(event[0] == 'hybrid_full_minimax_trial' for event in events)


def test_vector_bounds_use_row_order_and_preserve_native_total_constraint(monkeypatch):
    request, initial, kwargs, calls, _, _ = _problem()

    def minimize(fun, x0, **options):
        assert options['bounds'] == [(.01, .06), (.02, .06), (0., None)]
        total = options['constraints'][1]
        assert total['fun'](x0) == 5.
        assert total['jac'](x0).tolist() == [-100., -100., 0.]
        return _outcome(x0)

    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    best, detail = refine_synthesis_full_minimax(request, initial,
        lower_bounds_nm={'b': np.float64(2.), 'a': 1}, **kwargs)
    assert best is initial and not calls
    assert detail['fixed_layer_ids'] == [] and detail['free_thickness_variables'] == 2


def test_vector_minima_without_native_upper_or_total_do_not_invent_limits(monkeypatch):
    request, initial, kwargs, calls, _, _ = _problem(upper=None, total=None)

    def minimize(fun, x0, **options):
        assert options['bounds'] == [(.01, None), (.02, None), (0., None)]
        assert len(options['constraints']) == 1
        return _outcome(x0)

    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    best, detail = refine_synthesis_full_minimax(request, initial,
        lower_bounds_nm={'b': 2., 'a': 1.}, **kwargs)
    assert best is initial and not calls and detail['free_thickness_variables'] == 2


@pytest.mark.parametrize('with_jacobian', [False, True])
def test_vector_initial_coordinate_retains_exact_physical_identity(monkeypatch, with_jacobian):
    lower, upper = 1.5659576579707837, 3.14
    assert (lower / 100.) * 100. < lower
    assert (upper / 100.) * 100. > upper
    request, initial, kwargs, calls, _, _ = _problem((lower, upper), upper=upper, total=lower + upper)
    if not with_jacobian:
        initial['responses'][0].pop('jacobian_values_by_layer_id')
    monkeypatch.setattr('scipy.optimize.minimize', lambda fun, x0, **kw: _outcome(x0))
    best, detail = refine_synthesis_full_minimax(request, initial,
        lower_bounds_nm={'a': lower, 'b': upper}, **kwargs)
    assert best is initial
    assert calls == ([] if with_jacobian else [(lower, upper)])
    assert detail['terminal_physical_hash'] == physical_design_hash(initial['design'])


def test_vector_encoded_roundoff_snaps_but_true_bound_violations_never_evaluate(monkeypatch):
    lower, upper = 1.5659576579707837, 3.14
    request, initial, kwargs, calls, archived, events = _problem((2., 2.), upper=upper, total=None)
    valid = np.array([lower / 100., upper / 100., 1.])

    def minimize(fun, x0, **options):
        constraint = options['constraints'][0]
        constraint['fun'](valid)
        constraint['jac'](valid)  # Cache reuse does not call physical evaluation again.
        assert calls == [(lower, upper)]
        for index, direction in ((0, -np.inf), (1, np.inf)):
            invalid = valid.copy()
            invalid[index] = np.nextafter(invalid[index], direction)
            assert np.all(constraint['fun'](invalid) <= -999_999.)
            options['callback'](invalid)
            assert calls == [(lower, upper)]
        options['callback'](valid)
        return _outcome(valid)

    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    _, detail = refine_synthesis_full_minimax(request, initial,
        lower_bounds_nm={'a': lower, 'b': .5}, **kwargs)
    assert len(calls) == len(archived) == len(events) == 1
    assert detail['rejected_infeasible_trials'] == 4
    assert detail['terminal_max_error'] == .01 * (lower + upper)


def test_vector_total_violations_never_evaluate_or_replace_accepted_endpoint(monkeypatch):
    request, initial, kwargs, calls, archived, events = _problem(total=7.)

    def minimize(fun, x0, **options):
        invalid = np.array([.05, .05, 0.])
        assert np.all(options['constraints'][0]['fun'](invalid) < -1e6)
        options['callback'](invalid)
        return _outcome(invalid)

    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    best, detail = refine_synthesis_full_minimax(request, initial,
        lower_bounds_nm={'a': 1., 'b': 2.}, **kwargs)
    assert best is initial and not calls and not archived and not events
    assert not detail['terminal_full_grid_epigraph_feasible']
    assert detail['terminal_physical_hash'] == physical_design_hash(initial['design'])


@pytest.mark.parametrize('with_jacobian', [False, True])
def test_vector_exact_total_cap_uses_stable_sum_at_every_boundary(monkeypatch, with_jacobian):
    ds = (.1, .2, .3)
    assert float(sum(np.asarray(ds))) > .6 and math.fsum(ds) == .6
    request, initial, kwargs, calls, archived, events = _problem(ds, minimum=.01, total=.6,
                                                               layer_ids=('a', 'b', 'c'))
    if not with_jacobian:
        initial['responses'][0].pop('jacobian_values_by_layer_id')

    def minimize(fun, x0, **options):
        total_constraint = options['constraints'][1]
        assert total_constraint['fun'](x0) == 0.
        assert total_constraint['jac'](x0).tolist() == [-100., -100., -100., 0.]
        options['constraints'][0]['fun'](x0)
        options['callback'](x0)
        return _outcome(x0)

    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    best, detail = refine_synthesis_full_minimax(request, initial,
        lower_bounds_nm=dict(zip(('a', 'b', 'c'), ds, strict=True)), **kwargs)
    assert best is initial and detail['success']
    assert detail['terminal_full_grid_epigraph_feasible']
    assert detail['rejected_infeasible_trials'] == 0
    assert calls == ([] if with_jacobian else [ds])
    assert len(calls) == len(archived) == len(events)


def test_real_scipy_vector_total_at_exact_stable_cap_remains_feasible():
    request, initial, kwargs, calls, _, _ = _problem((.1, .2, .3), minimum=.01, total=.6,
                                                  layer_ids=('a', 'b', 'c'))
    best, detail = refine_synthesis_full_minimax(request, initial,
        lower_bounds_nm={'a': .1, 'b': .2, 'c': .3}, **kwargs)
    assert best is initial and detail['success']
    assert detail['terminal_full_grid_epigraph_feasible']
    assert all(math.fsum(ds) <= .6 for ds in calls)


@pytest.mark.parametrize('conflict', ['lower_bounds', 'initial'])
def test_vector_stable_total_still_rejects_real_precheck_excess(conflict):
    above = np.nextafter(.3, np.inf)
    assert math.fsum((.1, .2, above)) > .6
    ds = (.1, .2, above) if conflict == 'initial' else (.1, .2, .3)
    request, initial, kwargs, calls, archived, events = _problem(ds, minimum=.01, total=.6,
                                                               layer_ids=('a', 'b', 'c'))
    minima = {'a': .1, 'b': .2, 'c': above if conflict == 'lower_bounds' else .3}
    message = 'native total thickness limit' if conflict == 'lower_bounds' else 'feasible initial'
    with pytest.raises(ValueError, match=message):
        refine_synthesis_full_minimax(request, initial, lower_bounds_nm=minima, **kwargs)
    assert not calls and not archived and not events


def test_vector_stable_total_still_rejects_real_solver_excess(monkeypatch):
    request, initial, kwargs, calls, archived, events = _problem((.1, .2, .3),
        minimum=.01, total=.6, layer_ids=('a', 'b', 'c'))

    def minimize(fun, x0, **options):
        invalid = x0.copy()
        invalid[2] = np.nextafter(invalid[2], np.inf)
        assert math.fsum(invalid[:-1] * 100.) > .6
        assert options['constraints'][1]['fun'](invalid) < 0.
        assert np.all(options['constraints'][0]['fun'](invalid) < -999_999.)
        options['callback'](invalid)
        return _outcome(invalid)

    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    best, detail = refine_synthesis_full_minimax(request, initial,
        lower_bounds_nm={'a': .1, 'b': .2, 'c': .3}, **kwargs)
    assert best is initial and not calls and not archived and not events
    assert detail['rejected_infeasible_trials'] == 2
    assert not detail['terminal_full_grid_epigraph_feasible']
    assert detail['terminal_physical_hash'] == physical_design_hash(initial['design'])


@pytest.mark.parametrize('guard', [SynthesisStageBudget, SynthesisResourceGuard])
def test_vector_bounds_preserve_last_accepted_cache_on_stage_or_resource_interrupt(monkeypatch, guard):
    request, initial, kwargs, calls, archived, events = _problem()

    def check():
        if calls:
            raise guard()

    def minimize(fun, x0, **options):
        accepted = np.array([.01, .02, .03])
        options['constraints'][0]['fun'](accepted)
        options['callback'](accepted)
        pytest.fail('guard should stop after retaining the accepted cached candidate')

    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    best, detail = refine_synthesis_full_minimax(request, initial,
        lower_bounds_nm={'a': 1., 'b': 2.}, cancel_check=check, **kwargs)
    assert calls == [(1., 2.)] and len(archived) == len(events) == 1
    assert detail['termination'] == ('stage_budget' if guard is SynthesisStageBudget else 'resource_guard')
    assert detail['terminal_physical_hash'] == physical_design_hash(best['design'])
    assert detail['optimizer_t'] == .03


def test_vector_bounds_do_not_swallow_caller_cancellation(monkeypatch):
    request, initial, kwargs, calls, archived, events = _problem()

    class Cancelled(Exception):
        pass

    def check():
        if calls:
            raise Cancelled()

    def minimize(fun, x0, **options):
        accepted = np.array([.01, .02, .03])
        options['constraints'][0]['fun'](accepted)
        options['callback'](accepted)
        pytest.fail('caller cancellation must propagate')

    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    with pytest.raises(Cancelled):
        refine_synthesis_full_minimax(request, initial,
            lower_bounds_nm={'a': 1., 'b': 2.}, cancel_check=check, **kwargs)
    assert calls == [(1., 2.)] and len(archived) == len(events) == 1


def test_default_none_preserves_solver_arguments_numeric_results_and_event_order(monkeypatch):
    solver_traces = []

    def minimize(fun, x0, **options):
        trial = np.array([.02, .03, .05])
        constraint = options['constraints'][0]
        solver_traces.append((x0.tolist(), options['bounds'], options['method'], options['options'],
            fun(trial), options['jac'](trial).tolist(),
            constraint['fun'](trial).tolist(), constraint['jac'](trial).tolist()))
        constraint['fun'](trial)
        options['callback'](trial)
        return _outcome(trial)

    monkeypatch.setattr('scipy.optimize.minimize', minimize)
    traces = []
    for extra in ({}, {'lower_bounds_nm': None}):
        request, initial, kwargs, calls, archived, events = _problem()
        timeline: list[tuple[str, bool] | tuple[str]] = []
        original_evaluate = kwargs['evaluate']
        original_archive = kwargs['archive']
        original_progress = kwargs['progress']

        def evaluate(candidate, *, jacobian=True, timeline=timeline, original_evaluate=original_evaluate):
            timeline.append(('evaluate', jacobian))
            return original_evaluate(candidate, jacobian=jacobian)

        def archive(value, *, timeline=timeline, original_archive=original_archive, **options):
            timeline.append(('archive',))
            original_archive(value, **options)

        def progress(*args, timeline=timeline, original_progress=original_progress):
            timeline.append(('progress',))
            original_progress(*args)

        kwargs['evaluate'] = evaluate
        kwargs['archive'] = archive
        kwargs['progress'] = progress
        kwargs['cancel_check'] = lambda timeline=timeline: timeline.append(('cancel',))
        result, detail = refine_synthesis_full_minimax(request, initial, **extra, **kwargs)
        traces.append((result['design'], result['score'], detail, calls,
            [(value['design'], kw) for value, kw in archived],
            [(event[0], event[1]['design'], event[2]) for event in events], timeline))
    assert solver_traces[0] == solver_traces[1]
    assert solver_traces[0][1] == [(.001, .06), (.001, .06), (0., None)]
    assert traces[0] == traces[1]
    assert traces[0][3] == [(2., 3.)]
    assert traces[0][-1] == [('cancel',), ('evaluate', True), ('archive',), ('progress',), ('cancel',),
        ('cancel',), ('cancel',), ('cancel',)]
