"""Default multispectral routing retains parent budgets and original acceptance."""
from copy import deepcopy
from dataclasses import replace
from itertools import islice
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_multispectral_priors as priors
from coat_sim.core import film_synthesis_multispectral_minimax as minimax
from tests.test_film_synthesis_multispectral_priors import problem

pytestmark = pytest.mark.algorithm


def request_for_prefix():
    return replace(problem(max_layers=8), max_evaluations=20, max_cycles=1,
                   validation_samples_multiplier=2)


@pytest.mark.parametrize("stop_mode", ["cancel", "time_limit", "evaluation_limit"])
def test_prefix_shares_parent_stop_and_preserves_complete_candidate(monkeypatch, stop_mode):
    request = request_for_prefix()
    original = deepcopy(request)
    proposals = list(islice(priors.iter_multispectral_hr_proposals(request), 1))
    assert proposals
    monkeypatch.setattr(priors, 'iter_multispectral_hr_proposals', lambda *a, **kw: iter(proposals))
    clock = [0.]
    cancel = Event()
    if stop_mode == 'time_limit':
        request = replace(request, max_seconds=1.)
        monkeypatch.setattr(synthesis, 'monotonic', lambda: clock[0])
    if stop_mode == 'evaluation_limit':
        request = replace(request, max_evaluations=5)
    seen = []
    completed = []
    def stop(request, initial, *, evaluate, archive, cancel_check, **options):
        seen.append(options)
        for index in range(20):
            trial = deepcopy(initial['design'])
            trial['front_layers'][0]['thickness']['value'] *= 1 + .001 * (index + 1)
            value = evaluate(trial, jacobian=True)
            completed.append(value['design'])
            archive(value, audit_eligible=False)
            if stop_mode == 'cancel':
                cancel.set()
            elif stop_mode == 'time_limit':
                clock[0] = 2.
            cancel_check()
        raise AssertionError('parent stop must propagate')
    monkeypatch.setattr(minimax, 'refine_multispectral_minimax', stop)
    result = synthesis.synthesize_film_design(None, request, cancel_event=cancel)
    assert len(seen) == 1 and completed
    assert seen[0]['max_iterations'] == 48
    assert seen[0]['coordinate_scale_nm'] == 100.
    assert result.termination_reason == ('cancelled' if stop_mode == 'cancel' else
                                        'time_limit' if stop_mode == 'time_limit' else 'evaluation_limit')
    assert not result.targets_met and result.state != 'failed'
    assert result.training_score is not None and result.candidate_design is not None
    assert result.performance['cleanup_complete']
    assert any(row.stage == 'multispectral_prior' for row in result.trace)
    if stop_mode in ('cancel', 'time_limit'):
        assert result.validation_evaluations == 1
        assert result.validation_score is None
    else:
        assert result.evaluations == 5
    assert original == request_for_prefix()


def test_single_hr_request_keeps_original_route(monkeypatch):
    from tests.test_film_synthesis_hr_pipeline import pipeline_request
    def unexpected(*a, **kw):
        raise AssertionError('single pure HR must retain existing validated prefix')
    monkeypatch.setattr(minimax, 'refine_multispectral_minimax', unexpected)
    request = replace(pipeline_request(), max_evaluations=3)
    result = synthesis.synthesize_film_design(None, request)
    assert result.state != 'failed'
    assert not any(row.stage == 'multispectral_prior' for row in result.trace)


def test_parent_strict_rank_champion_is_audited_when_helper_returns_equal_initial(monkeypatch):
    from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
    from tests.test_film_synthesis_hr_priors import hr_request
    import numpy as np
    request = hr_request(low=550., high=550., angles=(0.,), max_layers=12, absorbing=False)
    first = replace(request.objectives[0], wavelength_samples=1)
    request = replace(request, objectives=(first, replace(first,
        wavelength_min_nm=560., wavelength_max_nm=560.)))
    proposal = next(priors.iter_multispectral_hr_proposals(request))
    ids = tuple(row['id'] for row in proposal.design['front_layers'])
    raw = evaluate_frozen_structure_candidate(synthesis._proxy(request), proposal.design,
        variable_ids=ids, with_jacobian=True)
    actuals = [row['actual_values'][0] for row in raw['responses']]
    assert min(actuals) >= .5
    request = replace(request, objectives=tuple(replace(o, lower_bound=float(y))
        for o, y in zip(request.objectives, actuals, strict=True)),
        strict_lower_bound_objective_indices=(0, 1), max_evaluations=30,
        validation_samples_multiplier=2)
    monkeypatch.setattr(priors, 'iter_multispectral_hr_proposals', lambda *a, **kw: iter([proposal]))
    observed = []
    def equal_initial(request, initial, *, evaluate, archive, **kw):
        jac = np.array([[row['jacobian_values_by_layer_id'][i][0] for i in ids]
                        for row in raw['responses']])
        direction = jac.sum(axis=0)
        assert np.all(jac @ direction > 0)
        trial = deepcopy(initial['design'])
        for row, delta in zip(trial['front_layers'], direction, strict=True):
            row['thickness']['value'] += float(delta)
        value = evaluate(trial, jacobian=True)
        assert synthesis.strict_lower_bounds_pass(request, value['score'])
        archive(value, audit_eligible=False)
        observed.append(value['design'])
        return initial, {'method': 'strict champion regression stub'}
    monkeypatch.setattr(minimax, 'refine_multispectral_minimax', equal_initial)
    result = synthesis.synthesize_film_design(None, request)
    assert observed and result.targets_met and result.validation_complete
    assert result.candidate_design == observed[0]
    assert synthesis.strict_lower_bounds_pass(request, result.validation_score)
