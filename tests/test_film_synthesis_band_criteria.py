"""Short route checks for independent physical band mean/pointwise constraints."""
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint, reduce_objective_responses
from coat_sim.core.film_optimization_scoring import reduction_score
from coat_sim.core.film_synthesis_feedback import SparseFeedbackRegistry, augment_feedback_guide, restore_feedback_base
from coat_sim.core.film_synthesis_global import reflector_objectives, bandpass_objectives
from coat_sim.core.film_synthesis_matching import matching_objectives
from coat_sim.core.film_synthesis_multispectral_minimax import _signed_bounds, refine_multispectral_minimax
from coat_sim.core.film_synthesis_hr_minimax import _responses, refine_hr_minimax
from coat_sim.core.film_synthesis_refinement import (
    SynthesisStageBudget, interior_loss_gradient, weighted_sse_loss_gradient,
    refine_synthesis_candidate, refine_synthesis_explorer, refine_synthesis_full_minimax,
)

pytestmark = pytest.mark.algorithm


def constraint(lower, *, enabled=True, strict=False):
    return FilmObjectiveConstraint(enabled=enabled, lower_bound=lower, upper_bound=None, strict_lower=strict)


def objective(*, pointwise=None, mean=None, angles=(0.,)):
    return FilmOptimizationObjective(500., 600., 3, angles_deg=angles,
        pointwise=pointwise or constraint(.9, enabled=False), mean=mean or constraint(.7))


def problem(obj=None):
    obj = obj or objective()
    request = FilmSynthesisRequest({}, (obj,), (), minimum_nonzero_thickness_nm=.1,
        max_layer_thickness_nm=6., max_total_thickness_nm=6., acceptable_normalized_error=0.)
    design = {'front_layers': [dict(id='d', material_id='m', optical_set_id='nk', thickness=dict(value=1., unit='nm'))]}
    calls = []
    def evaluate(candidate, *, jacobian=True, zero_ids=()):
        d = candidate['front_layers'][0]['thickness']['value']
        calls.append(d)
        responses = [dict(objective_index=0, angle_deg=angle, wavelength_nm=[500., 550., 600.],
            actual_values=(np.array([.1, .8, .8]) + .02 * d - angle * .001).tolist(),
            jacobian_values_by_layer_id={'d': [.02] * 3}) for angle in obj.angles_deg]
        reduced = reduce_objective_responses((obj,), responses, layer_ids=('d',), tolerance=0.)
        return dict(design=deepcopy(candidate), responses=responses, score=reduction_score(reduced, 0.))
    initial = evaluate(design)
    calls.clear()
    return request, initial, evaluate, calls


def test_mean_only_local_miss_is_absent_from_loss_jacobian_and_guards():
    obj = objective(mean=constraint(.6))
    request, initial, _, _ = problem(obj)
    assert min(initial['responses'][0]['actual_values']) < .6
    assert initial['score']['nominal_pass']
    assert len(initial['score']['errors']) == 1
    assert initial['score']['errors'][0].wavelength_nm is None
    loss, gradient = weighted_sse_loss_gradient(request, initial, ('d',))
    assert loss == 0. and gradient.tolist() == [0.]
    bounds, jacobian, _ = _signed_bounds(request, initial, ('d',))
    assert bounds.shape == (1,) and bounds[0] < 0.
    assert jacobian.shape == (1, 1)
    actual, derivative = _responses(request, initial, ('d',))
    assert actual.tolist() == pytest.approx([.645])
    assert derivative[:, 0].tolist() == pytest.approx([.02])


def test_both_distinct_bounds_and_each_angle_use_active_physical_means():
    obj = objective(pointwise=constraint(.1), mean=constraint(.64), angles=(0., 30.))
    request, initial, _, _ = problem(obj)
    errors = initial['score']['errors']
    assert len(errors) == 8  # two independent means and six pointwise values
    assert [e.angle_deg for e in errors if e.reduction == 'mean' and e.normalized_violation > 0] == [30.]
    assert all(e.normalized_violation == 0. for e in errors if e.reduction == 'pointwise' and e.angle_deg == 0.)
    loss, gradient = weighted_sse_loss_gradient(request, initial, ('d',))
    step = 1e-5
    values = []
    for sign in (-1, 1):
        trial = deepcopy(initial)
        for row in trial['responses']:
            row['actual_values'] = (np.asarray(row['actual_values']) + sign * step * .02).tolist()
        values.append(weighted_sse_loss_gradient(request, trial, ('d',))[0])
    assert loss > 0.
    assert gradient[0] == pytest.approx((values[1] - values[0]) / (2 * step), rel=1e-7)


@pytest.mark.parametrize('route', ['interior', 'sse', 'full', 'multispectral', 'hr'])
def test_real_short_search_routes_improve_active_mean_without_pointwise_veto(route):
    request, initial, evaluate, calls = problem()
    archived = []
    kwargs = dict(evaluate=evaluate, archive=lambda value, **kw: archived.append(value), max_iterations=12)
    if route == 'interior':
        result, detail = refine_synthesis_candidate(request, initial, quarter_nm={('m', 'nk'): 20.}, **kwargs)
    elif route == 'sse':
        result, detail = refine_synthesis_explorer(request, initial, **kwargs)
    elif route == 'full':
        result, detail = refine_synthesis_full_minimax(request, initial, **kwargs)
    elif route == 'multispectral':
        result, detail = refine_multispectral_minimax(request, initial, **kwargs)
    else:
        result, detail = refine_hr_minimax(request, initial, **kwargs)
    assert result['score']['max_error'] < initial['score']['max_error']
    assert min(result['responses'][0]['actual_values']) < request.objectives[0].mean.lower_bound
    assert calls and len(calls) == len(archived)
    assert detail['physical_candidates'] == len(calls)
    assert all(len(item['score']['errors']) == 1 for item in archived)


@pytest.mark.parametrize('route', ['sse', 'full'])
def test_local_stage_budget_preserves_original_candidate_and_cancellation_propagates(route):
    request, initial, evaluate, calls = problem()
    refiner = refine_synthesis_explorer if route == 'sse' else refine_synthesis_full_minimax
    def exhausted():
        raise SynthesisStageBudget()
    result, detail = refiner(request, initial, evaluate=evaluate, archive=lambda *a, **k: None,
                             cancel_check=exhausted, max_iterations=2)
    assert result is initial and detail['termination'] == 'stage_budget' and not calls
    class Cancelled(Exception):
        pass
    def cancelled():
        raise Cancelled()
    with pytest.raises(Cancelled):
        refiner(request, initial, evaluate=evaluate, archive=lambda *a, **k: None,
                cancel_check=cancelled, max_iterations=2)
    assert not calls


def test_classic_prior_selectors_do_not_read_disabled_pointwise_targets():
    obj = objective()
    request = FilmSynthesisRequest({}, (obj,), ())
    assert not reflector_objectives(request)
    assert not bandpass_objectives(request)
    assert not matching_objectives(request)
    ar = replace(obj, pointwise=FilmObjectiveConstraint(enabled=False, upper_bound=.01),
                 mean=FilmObjectiveConstraint(upper_bound=.2))
    assert not matching_objectives(replace(request, objectives=(ar,)))


def test_mean_feedback_uses_complete_dense_integral_and_preserves_base_identity():
    obj = objective(mean=constraint(.7))
    request, initial, _, _ = problem(obj)
    dense_waves = [500., 525., 550., 575., 600.]
    dense_values = [.2, .4, .8, .8, .8]
    response = dict(objective_index=0, angle_deg=0., wavelength_nm=dense_waves, actual_values=dense_values,
                    jacobian_values_by_layer_id={'d': [.02] * 5})
    audit = reduce_objective_responses((obj,), (response,))
    registry = SparseFeedbackRegistry(request)
    assert registry.record_audit(reduction_score(audit, 0.)) == 1
    point = registry.snapshot().points[0]
    assert point.reduction == 'mean' and point.wavelength_nm is None
    assert point.wavelength_grid_nm == tuple(dense_waves)
    called = []
    def extra(objectives):
        called.append(objectives)
        assert not objectives[0].pointwise.enabled and objectives[0].mean.enabled
        return dict(design=deepcopy(initial['design']), responses=[response])
    guide = augment_feedback_guide(request, initial, registry.snapshot(), evaluate_points=extra)
    assert guide['responses'] == initial['responses']
    assert restore_feedback_base(guide) is initial
    loss, gradient = interior_loss_gradient(request, guide, ('d',))
    assert called and loss > 0. and gradient[0] < 0.
    assert initial['score']['max_error'] == pytest.approx(.055)


@pytest.mark.parametrize('strategy', ['hybrid', 'legacy'])
def test_physical_synthesis_keeps_mean_only_scores_across_global_and_needle_routes(strategy, monkeypatch):
    from coat_sim.core import film_auto_synthesis as synthesis
    from coat_sim.core.film_design import new_design
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    from tests.test_film_auto_synthesis import Service
    records = []
    for name, index in [('glass', 1.5), ('low', 1.23), ('high', 2.2)]:
        optical = dict(id='nk', name=name, kind='tabulated', notes='synthetic route fixture',
            source='synthetic route fixture; not material evidence', wavelength_unit='nm',
            wavelength_min=450., wavelength_max=700., wavelengths=[450., 550., 700.],
            n=[index] * 3, k=[0.] * 3)
        records.append(dict(id=name, name=name, design_ready=True, optical_sets=[optical]))
    environment = new_design(design_id='mean-synthesis-routes')
    environment['substrate'].update(material_id='glass', optical_set_id='nk')
    seed = deepcopy(environment)
    seed['front_layers'] = [dict(id='seed', material_id='low', optical_set_id='nk',
                                thickness=dict(value=20., unit='nm'), note='fixture')]
    obj = FilmOptimizationObjective(500., 600., 3,
        pointwise=constraint(.99, enabled=False), mean=FilmObjectiveConstraint(lower_bound=None, upper_bound=0.))
    request = FilmSynthesisRequest(environment, (obj,),
        tuple(FilmNeedleMaterialReference(name, 'nk') for name in ('low', 'high')),
        seed_designs=(seed,), search_strategy=strategy, ar_structure_mode='v23_matching',
        max_cycles=1, max_evaluations=120, max_layers=2,
        max_layer_thickness_nm=200., minimum_nonzero_thickness_nm=.01,
        max_working_set_bytes=None, position_samples=1, needle_position_refinement_rounds=0)
    original = synthesis.evaluate_frozen_structure_candidate
    seen = []
    def evaluate(*args, **kwargs):
        value = original(*args, **kwargs)
        seen.append(value['score'])
        return value
    monkeypatch.setattr(synthesis, 'evaluate_frozen_structure_candidate', evaluate)
    result = synthesis.synthesize_film_design(Service(records), request)
    assert result.state != 'failed', result.message
    assert seen and all(score['errors'] and all(error.reduction == 'mean' and error.wavelength_nm is None
                                               for error in score['errors']) for score in seen)
    assert result.training_score and all(error.reduction == 'mean' for error in result.training_score['errors'])
    assert result.evaluations <= 120
    stages = {row.stage for row in result.trace}
    if strategy == 'hybrid':
        assert 'hybrid_global_trial' in stages
        assert result.performance['search']['hybrid']['needle_scans']
    else:
        assert 'sensitivity' in stages or 'needle_scan' in stages
