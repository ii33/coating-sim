"""Phase-diverse proposals are geometry seeds, not feasibility evidence.

These synthetic n/k fixtures check construction and isolation, not attainment
of any real specification or optical performance of a generated starting stack.
"""
from copy import deepcopy
from dataclasses import replace
from itertools import islice
import math

import pytest

from coat_sim.core import film_synthesis_rejection_priors as priors
from tests.test_film_synthesis_rejection_priors import normal_hint_request

pytestmark = pytest.mark.algorithm


def _harmonic_request():
    request = normal_hint_request()
    objectives = list(request.objectives)
    # The unbracketed long-wave stop has a third-order Bragg harmonic at
    # 500 nm for these synthetic constant-index materials.
    objectives[0] = replace(objectives[0], wavelength_min_nm=500., wavelength_max_nm=500.)
    objectives[3] = replace(objectives[3], wavelength_min_nm=1499., wavelength_max_nm=1501.)
    return replace(request, objectives=tuple(objectives))


def _assert_cells_match_metadata(proposal):
    rows = list(reversed(proposal.design['front_layers']))
    if proposal.provenance['matching_cap']:
        cap = rows.pop(0)
        assert cap['id'].endswith('-cap')
        assert cap['thickness']['unit'] == 'nm'
    for window in proposal.provenance['windows']:
        center, fraction = window['center_nm'], window['high_phase_fraction']
        for _ in range(window['pairs']):
            high, low = rows[:2]
            del rows[:2]
            for row, role, portion in ((high, 'high', fraction), (low, 'low', 1 - fraction)):
                assert (row['material_id'], row['optical_set_id']) == tuple(window[role])
                expected = center * portion / (2 * window['source_nk_at_center'][role][0])
                assert row['thickness']['unit'] == 'nm'
                assert row['thickness']['value'] == pytest.approx(expected)
    assert not rows


def test_complementary_families_use_distinct_real_phase_fractions_and_cap():
    request = normal_hint_request()
    before = deepcopy(request)
    generated = list(islice(priors.iter_rejection_proposals(request), 12))
    original = next(p for p in generated if p.provenance['family'] == 'narrow_rejection'
                    and not p.provenance['matching_cap'])
    complement = next(p for p in generated if p.provenance['family'] == 'narrow_rejection_complement'
                      and not p.provenance['matching_cap'])
    capped = next(p for p in generated if p.provenance['matching_cap'])
    a, b = original.provenance['windows'][0], complement.provenance['windows'][0]
    assert a['phase_family'] == 'narrow' and b['phase_family'] == 'complementary_narrow'
    assert 0 < a['high_phase_fraction'] < .5 < b['high_phase_fraction'] < 1
    assert a['high_phase_fraction'] + b['high_phase_fraction'] == pytest.approx(1.)
    assert a['pairs'] == b['pairs']
    for proposal in (original, complement, capped):
        assert proposal.provenance['acceptance_equivalence'] is False
        assert proposal.provenance['search_convention'] is True
        _assert_cells_match_metadata(proposal)
    assert capped.provenance['cap_reference_nm'] is not None
    assert request == before


def test_harmonic_family_uses_nonquarter_phase_without_claiming_validation():
    request = _harmonic_request()
    proposals = list(islice(priors.iter_rejection_proposals(request), 32))
    harmonic = next(p for p in proposals if p.provenance['family'] == 'narrow_rejection_harmonic')
    quarter = next(p for p in proposals if any(w['phase_family'] == 'quarter_wave'
                                              for w in p.provenance['windows']))
    window = next(w for w in harmonic.provenance['windows'] if w['phase_family'] == 'harmonic_phase')
    assert 0 < window['high_phase_fraction'] < 1
    assert window['high_phase_fraction'] != pytest.approx(.5)
    assert math.isfinite(window['bulk_attenuation_proxy']) and window['bulk_attenuation_proxy'] >= 0
    assert harmonic.provenance['acceptance_equivalence'] is False
    assert harmonic.design != quarter.design
    _assert_cells_match_metadata(harmonic)
    _assert_cells_match_metadata(quarter)


def test_proposals_and_future_generation_are_not_aliased():
    request = normal_hint_request()
    before = deepcopy(request)
    stream = priors.iter_rejection_proposals(request)
    first, second = next(stream), next(stream)
    expected_second = deepcopy(second)
    baseline_third = list(islice(priors.iter_rejection_proposals(request), 3))[-1]
    first.provenance['windows'][0]['objective_indices'].append(999)
    first.provenance['windows'][0]['source_nk_at_center']['high'] = (-999., -999.)
    first.design['front_layers'][0]['thickness']['value'] = 999.
    third = next(stream)
    assert second == expected_second
    assert third == baseline_third
    assert request == before


def test_v4_order_is_deterministic_and_all_explicit_limits_hold():
    request = _harmonic_request()
    baseline = list(islice(priors.iter_rejection_proposals(request), 16))
    assert baseline == list(islice(priors.iter_rejection_proposals(request), 16))
    first = baseline[0]
    values = [r['thickness']['value'] for r in first.design['front_layers']]
    bounded = replace(request, max_layers=len(values), max_total_thickness_nm=sum(values) + 1e-6,
                      max_layer_thickness_nm=max(values) + 1e-6,
                      minimum_nonzero_thickness_nm=min(values) - 1e-6)
    proposals = list(islice(priors.iter_rejection_proposals(bounded), 100))
    assert proposals
    for proposal in proposals:
        values = [r['thickness']['value'] for r in proposal.design['front_layers']]
        assert len(values) <= bounded.max_layers
        assert sum(values) <= bounded.max_total_thickness_nm
        assert min(values) >= bounded.minimum_nonzero_thickness_nm
        assert max(values) <= bounded.max_layer_thickness_nm
        assert proposal.provenance['layer_count'] == len(values)
        _assert_cells_match_metadata(proposal)


def _phase_key(proposal):
    return tuple((w['center_nm'], w['high_phase_fraction'], w['phase_family'])
                 for w in proposal.provenance['windows'])


def test_v4_nominal_phase_menu_precedes_single_window_neighbors_and_is_deterministic():
    request = normal_hint_request()
    proposals = list(islice(priors.iter_rejection_proposals(request, epoch=0), 20))
    assert proposals == list(islice(priors.iter_rejection_proposals(request, epoch=0), 20))
    base = proposals[0]
    assert base.provenance['boundary_pair_adjustment'] is None
    assert base.provenance['matching_cap'] is False
    assert proposals[1].provenance['boundary_pair_adjustment'] is None
    assert proposals[1].provenance['matching_cap'] is True
    nominal = { _phase_key(p): p for p in proposals
               if p.provenance['boundary_pair_adjustment'] is None and not p.provenance['matching_cap'] }
    assert len(nominal) == 2  # Narrow and complementary phases are both screened first.
    assert {p.provenance['family'] for p in nominal.values()} == {
        'narrow_rejection', 'narrow_rejection_complement'}
    order = [0 if p.provenance['boundary_pair_adjustment'] is None else
             (1 if p.provenance['boundary_pair_adjustment']['pair_delta'] == -1 else 2)
             for p in proposals]
    assert order == sorted(order) and set(order) == {0, 1, 2}
    for phase, reference in nominal.items():
        uncapped = [p for p in proposals if _phase_key(p) == phase
                    and p.provenance['boundary_pair_adjustment'] is not None
                    and not p.provenance['matching_cap']]
        assert {(p.provenance['boundary_pair_adjustment']['window_index'],
                 p.provenance['boundary_pair_adjustment']['pair_delta']) for p in uncapped} == {
                     (0, -1), (0, 1), (1, -1), (1, 1)}
        previous_windows = reference.provenance['windows']
        for proposal in uncapped:
            adjustment = proposal.provenance['boundary_pair_adjustment']
            index, delta = adjustment['window_index'], adjustment['pair_delta']
            windows = proposal.provenance['windows']
            assert adjustment['nominal_pairs'] == previous_windows[index]['pairs']
            assert [w['pairs'] - r['pairs'] for w, r in zip(windows, previous_windows, strict=True)] == [
                delta if i == index else 0 for i in range(len(previous_windows))]
            for window, previous in zip(windows, previous_windows, strict=True):
                assert window['source_nk_at_center'] == previous['source_nk_at_center']
                expected_proxy = previous['bulk_attenuation_proxy'] * window['pairs'] / previous['pairs']
                assert window['bulk_attenuation_proxy'] == pytest.approx(expected_proxy)
                assert window['pairs'] >= 1
    for proposal in proposals:
        _assert_cells_match_metadata(proposal)
    later = list(islice(priors.iter_rejection_proposals(request, epoch=1), 20))
    assert later == list(islice(priors.iter_rejection_proposals(request, epoch=1), 20))
    for before, after in zip(proposals, later, strict=True):
        assert after.provenance['epoch'] == 1
        assert [w['pairs'] for w in after.provenance['windows']] == [
            w['pairs'] + 1 for w in before.provenance['windows']]
        assert after.provenance['matching_cap'] == before.provenance['matching_cap']


def test_v4_nominal_harmonic_and_complementary_breadth_within_32_draw_prefix():
    proposals = list(islice(priors.iter_rejection_proposals(_harmonic_request()), 32))
    nominal = [p for p in proposals if p.provenance['boundary_pair_adjustment'] is None]
    assert len(nominal) == 28
    assert all(p.provenance['boundary_pair_adjustment'] is None for p in proposals[:28])
    assert proposals[28].provenance['boundary_pair_adjustment']['pair_delta'] == -1
    assert len({_phase_key(p) for p in nominal}) == 14
    assert {w['phase_family'] for p in nominal for w in p.provenance['windows']} >= {
        'narrow', 'complementary_narrow', 'quarter_wave', 'harmonic_phase'}
    # Prefix breadth is a construction property, not proof of optical quality.


def test_v4_layer_limit_can_reject_base_but_admit_a_single_pair_decrement():
    request = normal_hint_request()
    base = next(priors.iter_rejection_proposals(request))
    base_count = len(base.design['front_layers'])
    limited = replace(request, max_layers=base_count - 2)
    proposals = list(islice(priors.iter_rejection_proposals(limited), 16))
    assert proposals
    first = proposals[0]
    assert first.provenance['boundary_pair_adjustment'] == {
        'window_index': 0, 'pair_delta': -1,
        'nominal_pairs': base.provenance['windows'][0]['pairs']}
    for proposal in proposals:
        assert len(proposal.design['front_layers']) <= limited.max_layers
        assert all(w['pairs'] >= 1 for w in proposal.provenance['windows'])
        _assert_cells_match_metadata(proposal)


def test_v4_neighbor_provenance_is_not_shared_between_cap_siblings():
    proposals = list(islice(priors.iter_rejection_proposals(normal_hint_request()), 20))
    uncapped_index = next(i for i, p in enumerate(proposals)
                          if p.provenance['boundary_pair_adjustment'] is not None
                          and not p.provenance['matching_cap'])
    uncapped, capped = proposals[uncapped_index:uncapped_index + 2]
    assert capped.provenance['matching_cap'] is True
    expected = deepcopy(capped.provenance['boundary_pair_adjustment'])
    assert expected == uncapped.provenance['boundary_pair_adjustment']
    uncapped.provenance['boundary_pair_adjustment']['pair_delta'] = 99
    assert capped.provenance['boundary_pair_adjustment'] == expected
