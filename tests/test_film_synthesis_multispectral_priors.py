"""Separated-window proposals preserve source optics and explicit constraints."""
from copy import deepcopy
from dataclasses import replace
import cmath
from itertools import islice
import math
from typing import Any

import pytest

from coat_sim.core import film_synthesis_multispectral_priors as priors
from coat_sim.core.film_design import MAX_LAYERS, validate_design
from tests.test_film_synthesis_hr_priors import hr_request

pytestmark = pytest.mark.algorithm


def problem(*, max_layers=20):
    request = hr_request(low=400., high=450., max_layers=max_layers)
    return replace(request, objectives=(*request.objectives, replace(request.objectives[0],
        wavelength_min_nm=800., wavelength_max_nm=850.)))


def proposals(request, **kwargs):
    return list(priors.iter_multispectral_hr_proposals(request, **kwargs))


def test_merge_touch_overlap_keeps_gaps_and_indices():
    request = problem()
    first = request.objectives[0]
    request = replace(request, objectives=(*request.objectives,
        replace(first, wavelength_min_nm=450., wavelength_max_nm=470.),
        replace(first, wavelength_min_nm=420., wavelength_max_nm=460.)))
    windows = priors.merged_hr_windows(request)
    assert [(w['wavelength_min_nm'], w['wavelength_max_nm']) for w in windows] == [(400., 470.), (800., 850.)]
    assert set(windows[0]['objective_indices']) == {0, 2, 3}


def test_gap_is_not_sampled_or_filled_and_request_is_unchanged(monkeypatch):
    request = problem()
    original = deepcopy(request)
    sampled = []
    sample = priors.sample_synthesis_optical
    def tracked(sampler, wave, **kwargs):
        sampled.append(wave)
        assert 400. <= wave <= 450. or 800. <= wave <= 850.
        return sample(sampler, wave, **kwargs)
    monkeypatch.setattr(priors, 'sample_synthesis_optical', tracked)
    output = proposals(request)
    assert output and sampled and request == original
    for proposal in output:
        info = proposal.provenance
        assert len(info['windows']) == 2 and len(proposal.design['front_layers']) <= 20
        assert sum(sum(w['pair_counts']) for w in info['windows']) == info['pair_budget']
        assert 2 * info['pair_budget'] + info['extra_layers'] == len(proposal.design['front_layers'])
        assert info['candidate_pair_budget'] == info['pair_budget'] <= info['available_pair_budget']
        assert info['reserved_end_layers'] + info['reserved_connection_layers'] == info['extra_layers']
        assert info['estimated_pair_demand'] == sum(
            w['estimated_pairs_per_center'] * len(w['centers_nm']) for w in info['windows'])
        assert info['layer_cap'] == info['explicit_max_layers'] == 20
        assert info['allocation_limited'] == (info['pair_budget'] < info['estimated_pair_demand'])
        assert validate_design(proposal.design) == proposal.design
        for window in info['windows']:
            assert all(window['wavelength_min_nm'] <= c <= window['wavelength_max_nm'] for c in window['centers_nm'])
    assert len(output[0].design['front_layers']) < 20
    assert max(len(p.design['front_layers']) for p in output) == 20
    assert {p.provenance['center_order'] for p in output} == {'ascending', 'descending'}
    assert {p.provenance['reverse_material_order'] for p in output} == {True, False}
    assert any(p.provenance['matching_end_layers'] for p in output)
    assert any(p.provenance['connection_layers'] for p in output)


def test_source_complex_phase_and_production_order_are_preserved():
    request = problem()
    proposal = proposals(request)[0]
    incident_layers = list(reversed(proposal.design['front_layers']))
    expected = []
    for window in proposal.provenance['windows']:
        for center, count in zip(window['centers_nm'], window['pair_counts'], strict=True):
            for _ in range(count):
                for pair in (window['high'], window['low']):
                    expected.append((pair, center, window['phase_angle_deg']))
    materials = {r['id']: r for r in request.frozen_material_snapshot['materials']}
    for row, (pair, center, angle) in zip(incident_layers, expected, strict=True):
        assert (row['material_id'], row['optical_set_id']) == pair
        optical = materials[pair[0]]['optical_sets'][0]
        sampler = priors.prepare_optical_set(optical, k_sampling_policy='source')
        n, k, _ = priors.sample_synthesis_optical(sampler, center, assume_unknown_k_zero=False)
        assert k > 0
        q = cmath.sqrt(complex(n, k)**2 - math.sin(math.radians(angle))**2)
        assert row['thickness']['value'] == pytest.approx(center / (4 * q.real))
    assert all('source' in s for s in proposal.provenance['optical_sources'])


def test_normal_gui_no_layer_limit_has_finite_demand_and_epoch_expansion():
    request = problem(max_layers=None)
    first, later = proposals(request), proposals(request, epoch=1)
    assert 0 < len(first) <= 64 and 0 < len(later) <= 64
    assert all(len(p.design['front_layers']) <= MAX_LAYERS for p in (*first, *later))
    assert max(len(p.design['front_layers']) for p in later) > max(len(p.design['front_layers']) for p in first)
    assert first[0].provenance['allocation_limited'] is True
    assert any(p.provenance['allocation_limited'] is False for p in first)


def test_mixed_transmission_uses_only_hr_windows_and_retains_original_targets():
    request = hr_request(low=400., high=450., max_layers=20)
    passed = replace(request.objectives[0], wavelength_min_nm=600., wavelength_max_nm=650., metric='T')
    request = replace(request, objectives=(*request.objectives, passed))
    assert priors.multispectral_prior_eligible(request)
    proposal = proposals(request)[0]
    assert len(proposal.provenance['windows']) == 1
    assert proposal.provenance['windows'][0]['objective_indices'] == [0]
    assert proposal.provenance['original_objective_count'] == 2
    assert request.objectives[1] == passed


def test_combined_mean_and_pointwise_keeps_multispectral_prior_prefix_and_count():
    pointwise = problem(max_layers=None)
    both = replace(pointwise, objectives=tuple(replace(o, mean=replace(o.pointwise))
        for o in pointwise.objectives))

    pointwise_output = proposals(pointwise)
    both_output = proposals(both)
    assert len(pointwise_output) == len(both_output) == 48
    assert both_output[:8] == pointwise_output[:8]
    assert both_output == pointwise_output
    selected = priors._hr_objectives(both)
    assert tuple(index for index, _ in selected) == (0, 1)
    assert all(o.pointwise.enabled and o.mean.enabled for _, o in selected)
    assert both.objectives == tuple(replace(o, mean=replace(o.pointwise))
        for o in pointwise.objectives)

    mean_only = replace(both, objectives=tuple(replace(o,
        pointwise=replace(o.pointwise, enabled=False)) for o in both.objectives))
    assert not priors.multispectral_prior_eligible(mean_only)
    assert proposals(mean_only) == []


@pytest.mark.parametrize('mutation', ['single_hr', 'no_hr', 'seed', 'one_material', 'front', 'back', 'double', 'order'])
def test_route_selection(mutation):
    request = problem()
    env = deepcopy(request.environment)
    if mutation == 'single_hr':
        request = replace(request, objectives=request.objectives[:1])
    elif mutation == 'no_hr':
        request = replace(request, objectives=tuple(replace(o, metric='T') for o in request.objectives))
    elif mutation == 'seed':
        request = replace(request, seed_designs=(env,))
    elif mutation == 'one_material':
        request = replace(request, candidate_materials=request.candidate_materials[:1])
    elif mutation in {'front', 'back'}:
        env[mutation + '_layers'] = [{'id': 'existing'}]
        request = replace(request, environment=env)
    elif mutation == 'double':
        env['substrate']['sides'] = 'double'
        request = replace(request, environment=env)
    else:
        env['layer_order'] = 'incident_outward'
        request = replace(request, environment=env)
    assert priors.multispectral_prior_eligible(request) is (mutation == "double")
    assert bool(proposals(request)) is (mutation == "double")


@pytest.mark.parametrize('constraint,value', [('max_layers', 2), ('minimum_nonzero_thickness_nm', 1000.),
    ('max_layer_thickness_nm', 1.), ('max_total_thickness_nm', 1.)])
def test_explicit_constraints_skip_without_clamping_or_hidden_layers(constraint, value):
    request = replace(problem(), **{constraint: value})
    reports: list[dict[str, Any]] = []
    assert proposals(request, report=reports.append) == []
    assert reports and all(r['stage'] == 'structure_skipped' for r in reports)


def test_tight_valid_pair_budget_reserves_optional_layers_instead_of_exceeding_cap():
    output = proposals(problem(max_layers=5))
    assert output and all(len(p.design['front_layers']) <= 5 for p in output)
    assert any(p.provenance['connection_layers'] for p in output)
    assert not any(p.provenance['matching_end_layers'] for p in output)


@pytest.mark.parametrize('invalid', ['domain', 'unknown_k', 'crossed_contrast', 'absorbing_incident', 'mixed_domain'])
def test_material_domain_and_passive_source_requirements(invalid):
    request = problem()
    snapshot = deepcopy(request.frozen_material_snapshot)
    rows = {r['id']: r for r in snapshot['materials']}
    if invalid == 'domain':
        request = replace(request, objectives=(*request.objectives,
            replace(request.objectives[0], wavelength_min_nm=300., wavelength_max_nm=320.)))
    elif invalid == 'mixed_domain':
        request = replace(request, objectives=(*request.objectives,
            replace(request.objectives[0], metric='T', wavelength_min_nm=1100., wavelength_max_nm=1200.)))
    elif invalid == 'unknown_k':
        rows['beta-source']['optical_sets'][0]['k'] = None
    elif invalid == 'crossed_contrast':
        rows['beta-source']['optical_sets'][0]['n'] = [1.1, 2.5, 1.1]
    else:
        env = deepcopy(request.environment)
        env['incident_medium'] = dict(kind='material', material_id='alpha-source', optical_set_id='nk')
        request = replace(request, environment=env)
    request = replace(request, frozen_material_snapshot=snapshot)
    reports: list[dict[str, Any]] = []
    assert proposals(request, report=reports.append) == []
    assert reports


def test_unknown_k_uses_only_explicit_assumption_and_keeps_known_k():
    request = problem()
    snapshot = deepcopy(request.frozen_material_snapshot)
    rows = {r['id']: r for r in snapshot['materials']}
    rows['beta-source']['optical_sets'][0]['k'] = None
    env = deepcopy(request.environment)
    env['assume_lossless'] = True
    request = replace(request, environment=env, frozen_material_snapshot=snapshot)
    output = proposals(request)
    assert output and all(p.provenance['assume_unknown_k_zero'] for p in output)
    assert rows['alpha-source']['optical_sets'][0]['k'][0] > 0
    assert rows['beta-source']['optical_sets'][0]['k'] is None


def test_cancellation_is_lazy_and_valueerror_is_not_swallowed(monkeypatch):
    request = problem()
    sample_count = []
    original = priors.prepare_optical_set
    def tracked(*args, **kwargs):
        sample_count.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(priors, 'prepare_optical_set', tracked)
    stream = priors.iter_multispectral_hr_proposals(request)
    assert sample_count == []
    assert len(list(islice(stream, 1))) == 1 and len(sample_count) == 2
    stream.close()
    stop = ValueError('caller cancellation')
    calls: list[int] = []
    reports: list[dict[str, Any]] = []
    def cancel():
        calls.append(1)
        if len(calls) == 8:
            raise stop
    with pytest.raises(ValueError) as failure:
        next(priors.iter_multispectral_hr_proposals(request, cancel_check=cancel, report=reports.append))
    assert failure.value is stop and reports == []
    with pytest.raises(ValueError, match='epoch'):
        next(priors.iter_multispectral_hr_proposals(request, epoch=-1))
