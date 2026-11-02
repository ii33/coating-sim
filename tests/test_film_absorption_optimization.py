"""Absorption objective regressions: independent optics, Jacobians and audit gates."""
from copy import deepcopy
from dataclasses import replace
import math
from typing import Any, cast

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_design_service import simulate_frozen
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_synthesis_physics import simulate_synthesis_with_thickness_jacobian
from tests.test_film_auto_synthesis import setup_problem
from tests.test_film_optics import matrix_reference
from tests.test_film_synthesis_selected_surfaces_core import _double_environment, _row

pytestmark = pytest.mark.algorithm


def _case(mode='single', first='front', *, film_k=.06, substrate_k=.007):
    service, base = setup_problem(low=520., high=580., samples=3)
    for name, k in [('low', film_k), ('high', .035 if film_k else 0.), ('glass', substrate_k)]:
        service.materials.records[name]['optical_sets'][0]['k'] = [k] * 3
    if mode == 'single':
        env = deepcopy(base.environment)
    else:
        env = _double_environment()
        env['substrate'].update(mode=mode, thickness=dict(value=1200. if mode == 'coherent' else 6000., unit='nm'))
        env['first_surface'] = first
        env['exit_medium']['n'] = 1.15
    design = deepcopy(env)
    design['front_layers'] = [_row('f', 'low', 83.)]
    if mode != 'single':
        design['back_layers'] = [_row('b', 'high', 61.)]
    selected = ('front',) if mode == 'single' else ('front', 'back')
    frozen = synthesis.freeze_film_synthesis_request(service, replace(base, environment=env,
        selected_surfaces=selected, seed_designs=(design,)))
    variables = [dict(side=side, layer_id='f' if side == 'front' else 'b',
                      lower_nm=0., upper_nm=1000.) for side in selected]
    return service, base, frozen, design, variables


def _phase_average(wave, indices, thicknesses, angle, polarization):
    # Independent characteristic-matrix phase integral, fixed substrate loss;
    # no production optics/cascade code is used. Layer 2 is the substrate.
    n = np.asarray(indices, dtype=complex)
    beta = n[0].real * math.sin(math.radians(angle))
    q = np.sqrt(n * n - beta * beta + 0j)
    y = q if polarization == 's' else n * n / q
    result = []
    for phase in np.linspace(0., 2 * math.pi, 512, endpoint=False):
        matrix = np.eye(2, dtype=complex)
        for i, d in enumerate(thicknesses, 1):
            delta = 2 * math.pi * q[i] * d / wave
            if i == 2:
                delta = complex(phase, delta.imag)
            c, s = np.cos(delta), np.sin(delta)
            matrix = matrix @ np.array([[c, -1j * s / y[i]], [-1j * y[i] * s, c]])
        a = matrix[0, 0] + matrix[0, 1] * y[-1]
        b = matrix[1, 0] + matrix[1, 1] * y[-1]
        r = (y[0] * a - b) / (y[0] * a + b)
        t = 2 * y[0] / (y[0] * a + b)
        result.append((abs(r) ** 2, y[-1].real / y[0].real * abs(t) ** 2))
    return np.mean(result, axis=0)


@pytest.mark.parametrize('mode,first', [('single', 'front'), ('coherent', 'front'),
                                       ('coherent', 'back'), ('incoherent', 'front'),
                                       ('incoherent', 'back')])
@pytest.mark.parametrize('polarization', ['s', 'p', 'unpolarized'])
@pytest.mark.parametrize('angle', [0., 37.])
def test_absorption_physics_and_nm_jacobian_against_independent_oracle(mode, first, polarization, angle):
    service, _, frozen, design, variables = _case(mode, first)
    settings = dict(wavelength_min_nm=520., wavelength_max_nm=580., samples=3,
                    angle_deg=angle, polarization=polarization, first_surface=first)
    result = simulate_synthesis_with_thickness_jacobian(design, frozen.frozen_material_snapshot, variables, settings)
    direct = simulate_frozen('.', service.freeze_design(design, settings))
    for metric in ('R', 'T', 'A'):
        np.testing.assert_array_equal(result[metric], direct[metric])
    assert min(result['A']) > 1e-5  # safely away from the production 5e-13 cleanup boundary
    pols = ('s', 'p') if polarization == 'unpolarized' else (polarization,)
    nf = math.sqrt(1.5) + .06j
    nb = 2.2 + .035j
    ns = 1.5 + .007j
    if mode == 'single':
        indices, thickness = [1., nf, ns], [83.]
    elif first == 'front':
        indices, thickness = [1., nf, ns, nb, 1.15], [83., 1200. if mode == 'coherent' else 6000., 61.]
    else:
        indices, thickness = [1.15, nb, ns, nf, 1.], [61., 1200. if mode == 'coherent' else 6000., 83.]
    for i, wave in enumerate(result['wavelength_nm']):
        reference = np.mean([(_phase_average(wave, indices, thickness, angle, p)
                               if mode == 'incoherent' else matrix_reference(wave, indices, thickness, angle, p))
                              for p in pols], axis=0)
        np.testing.assert_allclose([result['R'][i], result['T'][i], result['A'][i]],
                                   [reference[0], reference[1], 1-reference[0]-reference[1]],
                                   rtol=2e-10, atol=2e-11)
    for column, var in enumerate(variables):
        h = 1e-3
        minus, plus = deepcopy(design), deepcopy(design)
        for trial, sign in ((minus, -1), (plus, 1)):
            trial[var['side'] + '_layers'][0]['thickness']['value'] += sign * h
        m = simulate_frozen('.', service.freeze_design(minus, settings))
        p = simulate_frozen('.', service.freeze_design(plus, settings))
        for metric in ('R', 'T', 'A'):
            fd = (np.asarray(p[metric]) - np.asarray(m[metric])) / (2 * h)
            np.testing.assert_allclose(np.asarray(result['jacobian_' + metric])[:, column], fd,
                                       rtol=4e-6, atol=3e-10)


def test_absorption_excludes_absorbing_exit_half_space_but_includes_finite_substrate():
    for mode, substrate_k in [('single', .2), ('coherent', .007), ('incoherent', .007)]:
        service, _, frozen, design, variables = _case(mode, film_k=0., substrate_k=substrate_k)
        settings = dict(wavelength_min_nm=520., wavelength_max_nm=580., samples=3,
                        angle_deg=27., polarization='s')
        result = simulate_synthesis_with_thickness_jacobian(design, frozen.frozen_material_snapshot, variables, settings)
        if mode == 'single':
            np.testing.assert_allclose(result['A'], 0., atol=1e-12)
        else:
            assert min(result['A']) > .1
        # Test zero film and substrate extinction separately, including two faces.
        service0, _, frozen0, design0, variables0 = _case(mode, film_k=0., substrate_k=0.)
        zero = simulate_synthesis_with_thickness_jacobian(design0, frozen0.frozen_material_snapshot, variables0, settings)
        np.testing.assert_allclose(zero['A'], 0., atol=1e-12)


def test_mixed_rta_value_upper_and_mean_scores_and_gradients_use_actual_channels():
    service, base, _, design, _ = _case()
    def upper(n):
        return FilmObjectiveConstraint(lower_bound=None, upper_bound=n)
    def absolute(n):
        return FilmObjectiveConstraint(mode='value', target_value=n, lower_bound=None, upper_bound=None)
    objectives = (
        replace(base.objectives[0], metric='R', angles_deg=(0., 37.), pointwise=upper(.01)),
        replace(base.objectives[0], metric='T', angles_deg=(0., 37.), pointwise=absolute(.6)),
        replace(base.objectives[0], metric='A', angles_deg=(0., 37.), pointwise=upper(.02), mean=absolute(.01)),
    )
    frozen = synthesis.freeze_film_synthesis_request(service, replace(base, objectives=objectives,
                                        seed_designs=(design,)))
    def evaluate(candidate):
        return synthesis.evaluate_frozen_structure_candidate(synthesis._proxy(frozen), candidate,
                                          variable_ids=('f',), with_jacobian=True)
    value = evaluate(design)
    for row in value['responses']:
        obj = objectives[row['objective_index']]
        settings = dict(wavelength_min_nm=520., wavelength_max_nm=580., samples=3,
                        angle_deg=row['angle_deg'], polarization=obj.polarization)
        direct = simulate_frozen('.', service.freeze_design(design, settings))
        np.testing.assert_allclose(row['actual_values'], direct[obj.metric], atol=2e-14)
        errors = [e for e in value['score']['errors'] if e.objective_index == row['objective_index']
                  and e.angle_deg == row['angle_deg']]
        for e in errors:
            expected = np.dot([.25, .5, .25], direct[obj.metric]) if e.reduction == 'mean' else direct[obj.metric][[520., 550., 580.].index(e.wavelength_nm)]
            assert e.actual == pytest.approx(expected, abs=2e-14)
            assert e.metric == obj.metric
            bound = .01 if e.reduction == 'mean' else {'R': .01, 'T': .6, 'A': .02}[obj.metric]
            violation = abs(expected - bound) if e.reduction == 'mean' or obj.metric == 'T' else max(0., expected - bound)
            assert e.normalized_violation == pytest.approx(violation, abs=2e-14)
    assert value['score']['max_error'] > 0.
    h = 1e-3
    minus, plus = deepcopy(design), deepcopy(design)
    minus['front_layers'][0]['thickness']['value'] -= h
    plus['front_layers'][0]['thickness']['value'] += h
    fd = (evaluate(plus)['smooth_loss'] - evaluate(minus)['smooth_loss']) / (2*h)
    assert value['gradient_by_layer_id']['f'] == pytest.approx(fd, rel=4e-6, abs=2e-10)


def test_synthesis_absorption_upper_bound_is_audited_and_not_transmission():
    service, base = setup_problem(low=550., high=550., samples=1)
    obj = replace(base.objectives[0], metric='A', pointwise=FilmObjectiveConstraint(lower_bound=None, upper_bound=.001))
    result = synthesis.synthesize_film_design(service, replace(base, objectives=(obj,), max_cycles=1, max_evaluations=10))
    assert result.state == 'completed' and result.targets_met and result.validation_complete
    assert result.validation_score and all(e.metric == 'A' and e.actual <= .001 for e in result.validation_score['errors'])
    assert result.candidate_design is not None
    response = simulate_frozen('.', service.freeze_design(result.candidate_design, dict(
        wavelength_min_nm=550., wavelength_max_nm=550., samples=1)))
    assert response['A'][0] <= .001 and response['T'][0] > .5


def test_synthesis_audit_rejects_fixed_absorbing_coating_against_a_upper_bound():
    service, base = setup_problem(low=550., high=550., samples=1)
    service.materials.records['low']['optical_sets'][0]['k'] = [.2] * 3
    env = _double_environment()
    env['front_layers'] = [_row('fixed', 'low', 100.)]
    obj = replace(base.objectives[0], metric='A', pointwise=FilmObjectiveConstraint(lower_bound=None, upper_bound=.001))
    request = replace(base, environment=env, selected_surfaces=('back',), objectives=(obj,),
                      max_cycles=1, max_evaluations=2)
    result = synthesis.synthesize_film_design(service, request)
    assert result.validation_complete and not result.targets_met
    assert result.validation_score and any(e.metric == 'A' and e.actual > .001 for e in result.validation_score['errors'])
    assert result.candidate_design is not None
    assert result.candidate_design['front_layers'] == env['front_layers']


def test_ideal_sensitivity_scalar_path_uses_absorption_not_transmission():
    from coat_sim.core import film_ideal_sensitivity as ideal
    from coat_sim.core import _film_ideal_sensitivity_core as core
    from tests.test_film_ideal_sensitivity import problem
    service, request = problem(two=True)
    service.materials.records['low']['optical_sets'][0]['k'] = [.06] * 3
    service.materials.records['high']['optical_sets'][0]['k'] = [.03] * 3
    obj = replace(request.objectives[0], metric='A', wavelength_min_nm=520., wavelength_max_nm=580.,
                  wavelength_samples=3, angles_deg=(0., 37.))
    frozen = ideal.freeze_film_ideal_sensitivity_request(service, replace(request, objectives=(obj,)))
    data = core._Evaluator(frozen).evaluate(frozen.design_snapshot)
    # New evaluator guarantees scalar_objective takes its streaming path, not a cached full result.
    scalar = core._Evaluator(frozen).scalar_objective(frozen.design_snapshot)
    assert scalar == pytest.approx(data.analysis.objective_value, rel=3e-12, abs=1e-20)
    assert scalar > 1e-10
    for col, variable in enumerate(frozen.variables):
        values = []
        for sign in (-1., 1.):
            design = deepcopy(frozen.design_snapshot)
            row = next(x for x in design['front_layers'] if x['id'] == variable.layer_id)
            row['thickness']['value'] += sign * .001
            values.append(core._Evaluator(frozen).evaluate(design).values)
        np.testing.assert_allclose(data.nominal_jacobian[:, col], (values[1]-values[0])/.002,
                                   rtol=4e-6, atol=3e-10)


def test_mixed_metric_minimax_signed_bounds_accept_a_but_reject_unknown_metric():
    from coat_sim.core import film_synthesis_multispectral_minimax as minimax
    from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
    from coat_sim.core.film_optimization import FilmOptimizationObjective
    def upper(n):
        return FilmObjectiveConstraint(lower_bound=None, upper_bound=n)
    r = FilmOptimizationObjective(500., 600., 3, metric='R', pointwise=upper(.2))
    a = FilmOptimizationObjective(500., 600., 3, metric='A', pointwise=upper(.1), mean=upper(.09))
    request = FilmSynthesisRequest({}, (r, a), ())
    minimax._validate_objectives(request)
    with pytest.raises(ValueError, match='objective'):
        minimax._validate_objectives(replace(request, objectives=(r, replace(a, metric=cast(Any, 'X')))))
    responses = [dict(objective_index=0, angle_deg=0., wavelength_nm=[500., 550., 600.],
                      actual_values=[.1, .3, .2], jacobian_values_by_layer_id={'d': [.01, .02, .03]}),
                 dict(objective_index=1, angle_deg=0., wavelength_nm=[500., 550., 600.],
                      actual_values=[.02, .10, .14], jacobian_values_by_layer_id={'d': [.001, .002, .004]})]
    bounds, jac, masses = minimax._signed_bounds(request, dict(responses=responses), ('d',))
    np.testing.assert_allclose(bounds, [-.1, .1, 0., 0., -.08, 0., .04], atol=1e-14)
    np.testing.assert_allclose(jac[:, 0], [.01, .02, .03, .00225, .001, .002, .004], atol=1e-14)
    assert np.isfinite(masses).all()


def test_needle_search_adds_absorber_and_independently_validates_a_lower_bound():
    from coat_sim.core.film_optimization import FilmOptimizationRequest
    from coat_sim.core.film_optimization_needle import (FilmNeedleMaterialReference,
        FilmNeedleOptimizationRequest, optimize_film_design_with_needle)
    service, base = setup_problem(low=550., high=550., samples=1)
    service.materials.records['high']['optical_sets'][0]['k'] = [.2] * 3
    service.simulate = lambda design, settings: simulate_frozen('.', service.freeze_design(design, settings))
    design = deepcopy(base.environment)
    design['front_layers'] = [_row('initial', 'low', 80.)]
    obj = replace(base.objectives[0], metric='A', pointwise=FilmObjectiveConstraint(lower_bound=.03, upper_bound=None))
    request = FilmNeedleOptimizationRequest(FilmOptimizationRequest(
        design, 1, (), (obj,), max_evaluations=30, max_iterations=4),
        (FilmNeedleMaterialReference('high', 'nk'),), min_thickness_nm=20., max_thickness_nm=80.)
    result = optimize_film_design_with_needle(service, request)
    assert result.state == 'completed' and result.structure_changed and result.accepted_insertions
    assert result.base_result.validation_complete and result.validation_targets_met
    assert all(e.metric == 'A' and e.actual >= .03 for e in result.validation_per_condition_errors)
    direct = simulate_frozen('.', service.freeze_design(result.best_design, dict(
        wavelength_min_nm=550., wavelength_max_nm=550., samples=1)))
    assert direct['A'][0] >= .03
