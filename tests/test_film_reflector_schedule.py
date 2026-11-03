"""Reflector pair rotation is scheduling only, not a physical acceptance rule."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from itertools import combinations, islice
import math

import pytest

from coat_sim.core import film_synthesis_global as reflector
from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_synthesis_archive import physical_design_hash
from tests.test_film_auto_synthesis import setup_problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def three_material_request(**limits):
    service, request = setup_problem()
    middle = deepcopy(service.materials.records['low'])
    middle.update(id='middle', name='middle')
    middle['optical_sets'][0]['n'] = [1.65] * 3
    service.materials.records['middle'] = middle
    refs = tuple(FilmNeedleMaterialReference(name, 'nk') for name in ('high', 'middle', 'low'))
    objective = replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise,
        lower_bound=.9, upper_bound=None))
    request = replace(request, objectives=(objective,), candidate_materials=refs,
                      search_strategy='hybrid', max_layers=16, **limits)
    return freeze_film_synthesis_request(service, request)


def pair_id(proposal):
    p = proposal.provenance
    return frozenset((tuple(p['high']), tuple(p['low'])))


def test_three_feasible_material_pairs_are_seen_before_any_pair_repeats():
    request = three_material_request()
    prefix = list(islice(reflector.iter_reflector_proposals(request), 3))
    expected = {frozenset((a.material_id, b.material_id))
                for a, b in combinations(request.candidate_materials, 2)}
    actual = {frozenset(ref[0] for ref in pair_id(p)) for p in prefix}
    assert actual == expected and len(prefix) == 3
    assert all(p.provenance['family'] == 'periodic' for p in prefix)


def test_each_pair_visits_all_admissible_periodic_counts_and_both_terminations_first():
    request = three_material_request()
    all_proposals = list(reflector.iter_reflector_proposals(request))
    assert all_proposals
    pairs = {pair_id(p) for p in all_proposals}
    assert len(pairs) == 3
    for pair in pairs:
        sequence = [p for p in all_proposals if pair_id(p) == pair]
        first_nonperiodic = next(i for i, p in enumerate(sequence)
                                 if p.provenance['family'] != 'periodic')
        prefix = sequence[:first_nonperiodic]
        periodic = [p for p in sequence if p.provenance['family'] == 'periodic']
        assert prefix == periodic
        sample = prefix[0].provenance
        estimated = sample['estimated_pairs']
        expected = {estimated * multiplier for multiplier in {1, 2, 2 * sample['bandwidth_factor']}}
        expected.add(request.max_layers // 2)
        expected = {count for count in expected if 2 * count <= request.max_layers}
        assert {p.provenance['pairs'] for p in prefix} == expected
        for count in expected:
            assert {p.provenance['reverse'] for p in prefix if p.provenance['pairs'] == count} == {False, True}
        assert any(p.provenance['family'] == 'chirped' for p in sequence[first_nonperiodic:])


def test_full_rotated_multiset_matches_aggregated_per_pair_generators():
    request = three_material_request()
    actual = list(reflector.iter_reflector_proposals(request))
    expected = []
    for pair in combinations(request.candidate_materials, 2):
        pair_request = replace(request, candidate_materials=pair)
        for periodic_only in (True, False):
            expected.extend(reflector._iter_reflector_pair_proposals(pair_request, periodic_only=periodic_only))
    assert actual and len(actual) == len(expected)
    assert Counter(physical_design_hash(p.design) for p in actual) == Counter(
        physical_design_hash(p.design) for p in expected)


@pytest.mark.parametrize('changes', [
    dict(max_total_thickness_nm=1.), dict(max_layer_thickness_nm=1.),
    dict(minimum_nonzero_thickness_nm=1000.),
])
def test_pair_rotation_never_bypasses_explicit_thickness_bounds(changes):
    request = three_material_request(**changes)
    events: list[dict[str, object]] = []
    assert list(reflector.iter_reflector_proposals(request, report=events.append)) == []
    assert any(event.get('reason') == 'explicit thickness constraints' for event in events)


def test_yielded_designs_obey_all_limits():
    request = three_material_request(minimum_nonzero_thickness_nm=10.,
        max_layer_thickness_nm=500., max_total_thickness_nm=2500.)
    proposals = list(reflector.iter_reflector_proposals(request))
    assert proposals
    for proposal in proposals:
        rows = proposal.design['front_layers']
        thicknesses = [row['thickness']['value'] for row in rows]
        assert len(rows) <= request.max_layers
        assert all(10. <= value <= 500. for value in thicknesses)
        assert math.fsum(thicknesses) <= 2500.


@pytest.mark.parametrize('stop', ['cancel', 'close'])
def test_all_started_pair_iterators_close_on_cancel_or_consumer_close(monkeypatch, stop):
    request = three_material_request()
    started, closed = [], []
    stopped = False

    class Cancelled(Exception):
        pass

    def check():
        if stopped:
            raise Cancelled

    def source(pair_request, **_kwargs):
        pair = tuple(ref.material_id for ref in pair_request.candidate_materials)
        started.append(pair)
        try:
            while True:
                yield reflector.StructuralProposal(deepcopy(pair_request.environment), {'pair': pair})
        finally:
            closed.append(pair)

    monkeypatch.setattr(reflector, '_iter_reflector_pair_prefix', source)
    iterator = reflector.iter_reflector_proposals(request, cancel_check=check)
    assert len(list(islice(iterator, 3))) == 3 and len(started) == 3
    if stop == 'cancel':
        stopped = True
        with pytest.raises(Cancelled):
            next(iterator)
    else:
        iterator.close()
    assert Counter(closed) == Counter(started)


def test_old_reflector_schedule_checkpoint_is_rejected(monkeypatch):
    from coat_sim.core import film_synthesis_continuation as continuation
    request = three_material_request()
    monkeypatch.setattr(reflector, 'REFLECTOR_SCHEDULING_POLICY', 'obsolete-pair-major-policy')
    old_fingerprint = continuation.search_fingerprint(request)
    state = continuation.seal_search_state(dict(kind=continuation.SEARCH_STATE_KIND,
        version=continuation.SEARCH_STATE_VERSION, fingerprint=old_fingerprint,
        cursors={}, source_cursor=0, rotations=0, tried=[], pending=[], pool=[],
        feedback={}, archive_rotation=0, archive_total_admitted=0))
    monkeypatch.undo()
    assert continuation.search_fingerprint(request) != old_fingerprint
    with pytest.raises(ValueError, match='fingerprint changed'):
        continuation.validate_search_state(request, state)


def test_infeasible_first_pair_does_not_starve_other_pairs():
    request = three_material_request()
    snapshot = deepcopy(request.frozen_material_snapshot)
    materials = {m['id']: m for m in snapshot['materials']}
    materials['middle']['optical_sets'][0]['n'] = deepcopy(materials['high']['optical_sets'][0]['n'])
    request = replace(request, frozen_material_snapshot=snapshot)
    first_two = list(islice(reflector.iter_reflector_proposals(request), 2))
    assert {frozenset(ref[0] for ref in pair_id(p)) for p in first_two} == {
        frozenset(('high', 'low')), frozenset(('middle', 'low'))}


def test_rejection_proposals_remain_a_prefix_before_pair_rotation(monkeypatch):
    from coat_sim.core import film_synthesis_rejection_priors as rejection
    request = three_material_request()
    marker = reflector.StructuralProposal(deepcopy(request.environment), {'family': 'narrow_rejection'})
    monkeypatch.setattr(rejection, 'iter_rejection_proposals', lambda *_args, **_kwargs: iter((marker, marker)))
    first = list(islice(reflector.iter_reflector_proposals(request), 3))
    assert [p.provenance['family'] for p in first] == ['narrow_rejection', 'narrow_rejection', 'periodic']


def test_old_checkpoint_with_no_schedule_key_is_rejected(monkeypatch):
    from coat_sim.core import film_synthesis_continuation as continuation
    request = three_material_request()
    original_digest = continuation._digest
    with monkeypatch.context() as scoped:
        def old_digest(data):
            assert 'reflector_scheduling_policy' in data
            return original_digest({key: value for key, value in data.items()
                                    if key != 'reflector_scheduling_policy'})
        scoped.setattr(continuation, '_digest', old_digest)
        old_fingerprint = continuation.search_fingerprint(request)
    state = continuation.seal_search_state(dict(kind=continuation.SEARCH_STATE_KIND,
        version=continuation.SEARCH_STATE_VERSION, fingerprint=old_fingerprint,
        cursors={}, source_cursor=0, rotations=0, tried=[], pending=[], pool=[],
        feedback={}, archive_rotation=0, archive_total_admitted=0))
    assert continuation.search_fingerprint(request) != old_fingerprint
    with pytest.raises(ValueError, match='fingerprint changed'):
        continuation.validate_search_state(request, state)


def test_pair_source_exception_closes_raising_and_other_live_sources(monkeypatch):
    request = three_material_request()
    started, closed = [], []

    class Cancelled(Exception):
        pass

    def source(pair_request, **_kwargs):
        pair = tuple(ref.material_id for ref in pair_request.candidate_materials)
        started.append(pair)
        try:
            yield reflector.StructuralProposal(deepcopy(pair_request.environment), {'pair': pair})
            raise Cancelled('interrupted inside pair generation')
        finally:
            closed.append(pair)

    monkeypatch.setattr(reflector, '_iter_reflector_pair_prefix', source)
    iterator = reflector.iter_reflector_proposals(request)
    assert len(list(islice(iterator, 3))) == 3
    with pytest.raises(Cancelled, match='inside pair'):
        next(iterator)
    assert len(started) == 3 and Counter(closed) == Counter(started)
