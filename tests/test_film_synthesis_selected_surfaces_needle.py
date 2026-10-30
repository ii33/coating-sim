"""Surface-local needle coordinates with one unchanged physical environment."""
from copy import deepcopy
import json

import pytest

from coat_sim.core.film_design import validate_design
from coat_sim.core.film_optimization_structure import _insert_at_site, _site_records
from coat_sim.core.film_synthesis_batch_needle import (
    batch_needle_parent_fingerprint, build_batch_needle_design,
    cleanup_batch_needle_design, scan_batch_needles,
)
from coat_sim.core.film_synthesis_numeric_zero import numeric_zero_cleanup
from tests.test_film_synthesis_batch_needle import _problem, _proposal, _row

pytestmark = pytest.mark.algorithm


def _double():
    request, design, frozen = _problem([_row('fixed-front', thickness=81.)])
    design = deepcopy(design)
    design['substrate']['sides'] = 'double'
    design['substrate']['mode'] = 'coherent'
    design['back_layers'] = [_row('back-host', thickness=120.)]
    return request, validate_design(design), frozen


def _bytes(rows):
    return json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()


def test_back_insertion_uses_own_coordinates_and_keeps_front_bytes():
    _request, design, frozen = _double()
    front = _bytes(design['front_layers'])
    site = next(s for s in _site_records(design, frozen, 3, side='back')
                if s['kind'] == 'interior' and s['fraction'] == .25)
    assert site['side'] == 'back' and site['position_nm'] == 30.
    proposal = _proposal(site)
    built = build_batch_needle_design(design, frozen, (proposal,),
                                     seed_thickness_nm=.01, selected_surfaces=('back',))
    assert _bytes(built.design['front_layers']) == front
    assert built.design['first_surface'] == design['first_surface']
    assert built.design['substrate'] == design['substrate']
    assert [r['thickness']['value'] for r in built.design['back_layers']] == [30., .01, 90.]
    assert built.layer_ids == ('back-host', 'needle-0', 'split-0')
    assert built.anchors['needle-0']['side'] == 'back'
    direct = _insert_at_site(design, site, proposal.reference, .01, 'needle-0', 'split-0')
    assert direct['back_layers'] == built.design['back_layers'] or [
        r['thickness'] for r in direct['back_layers']] == [r['thickness'] for r in built.design['back_layers']]
    assert _bytes(direct['front_layers']) == front


def test_joint_batch_same_local_interface_is_distinct_on_two_surfaces():
    _request, design, frozen = _double()
    proposals = tuple(_proposal(_site_records(design, frozen, 0, side=side)[0], i)
                      for i, side in enumerate(('front', 'back')))
    built = build_batch_needle_design(design, frozen, proposals,
                                     selected_surfaces=('front', 'back'))
    assert built.design['front_layers'][0]['id'] == 'needle-0'
    assert built.design['back_layers'][0]['id'] == 'needle-1'
    assert built.layer_ids == ('needle-0', 'fixed-front', 'needle-1', 'back-host')
    assert set(built.zero_thickness_layer_ids) == {'needle-0', 'needle-1'}
    assert {a['side'] for a in built.anchors.values()} == {'front', 'back'}
    assert design['front_layers'] == [_row('fixed-front', thickness=81.)]


def test_scan_routes_probe_geometry_and_joint_gradient_ids_without_surface_aliasing():
    _request, design, frozen = _double()
    observed = []
    def evaluate(candidate, *, variable_ids, zero_ids):
        assert candidate['first_surface'] == design['first_surface']
        observed.append((deepcopy(candidate), tuple(variable_ids)))
        return {'gradient_by_layer_id': {identifier: -1. for identifier in variable_ids}}
    refs = (_proposal(_site_records(design, frozen, 0)[0]).reference,)
    back = scan_batch_needles(design, frozen, refs, evaluate=evaluate, position_samples=1,
                              valley_refinement_rounds=1, max_needles=2,
                              selected_surfaces=('back',))
    assert back.probe_count == 5
    assert all(p.site['side'] == 'back' for p in back.proposals)
    assert all(_bytes(d['front_layers']) == _bytes(design['front_layers']) for d, _ids in observed)
    observed.clear()
    joint = scan_batch_needles(design, frozen, refs, evaluate=evaluate, position_samples=0,
                               valley_refinement_rounds=0, max_needles=4,
                               selected_surfaces=('front', 'back'))
    assert joint.probe_count == 4
    assert {p.site['side'] for p in joint.selected} == {'front', 'back'}
    assert all({'fixed-front', 'back-host'} <= set(ids) for _d, ids in observed)
    built = build_batch_needle_design(design, frozen, joint.selected,
                                     selected_surfaces=('front', 'back'))
    assert len(built.needle_ids) == 4


def test_cleanup_never_merges_across_substrate_or_changes_inactive_rows():
    _request, design, frozen = _double()
    design['front_layers'].append(_row('fixed-tiny', thickness=1e-16))
    design['back_layers'] = [_row('back-a', thickness=20.), _row('back-zero', thickness=0.),
                             _row('back-b', thickness=30.)]
    cleaned = cleanup_batch_needle_design(design, frozen, selected_surfaces=('back',))
    assert cleaned.design['front_layers'] == design['front_layers']
    assert cleaned.design['back_layers'] == [_row('back-a', thickness=50.)]
    assert cleaned.merged_ids == {'back-b': 'back-a'}
    joint = cleanup_batch_needle_design(design, frozen, selected_surfaces=('front', 'back'))
    assert len(joint.design['front_layers']) == len(joint.design['back_layers']) == 1
    assert joint.design['front_layers'][0]['id'] != joint.design['back_layers'][0]['id']
    design['back_layers'] = [_row('back-a', thickness=20.), _row('back-tiny', 'needle', 1e-16),
                             _row('back-b', thickness=30.)]
    reduced, threshold = numeric_zero_cleanup(design, frozen,
        {('host', 'nk'): 1., ('needle', 'nk'): 1.}, selected_surfaces=('back',),
        max_layer_thickness_nm=40.)
    assert threshold > 1e-16
    assert reduced.design['front_layers'] == design['front_layers']
    assert [r['id'] for r in reduced.design['back_layers']] == ['back-a', 'back-b']
    assert reduced.merged_ids == {}


def test_needle_selection_and_fingerprint_bind_fixed_stack_and_normalize_order():
    _request, design, frozen = _double()
    front = batch_needle_parent_fingerprint(design, frozen)
    back = batch_needle_parent_fingerprint(design, frozen, selected_surfaces=('back',))
    assert front != back
    changed = deepcopy(design)
    changed['front_layers'][0]['thickness']['value'] += 1.
    assert back != batch_needle_parent_fingerprint(changed, frozen, selected_surfaces=('back',))
    assert batch_needle_parent_fingerprint(design, frozen, selected_surfaces=('front', 'back')) == \
        batch_needle_parent_fingerprint(design, frozen, selected_surfaces=('back', 'front'))
    for invalid in ((), ('left',), ('front', 'front')):
        with pytest.raises(ValueError, match='selected_surfaces'):
            build_batch_needle_design(design, frozen, (), selected_surfaces=invalid)
    single = deepcopy(design)
    single['substrate']['sides'] = 'single'
    with pytest.raises(ValueError, match='double-sided'):
        cleanup_batch_needle_design(single, frozen, selected_surfaces=('back',))
    with pytest.raises(ValueError, match='unselected'):
        build_batch_needle_design(design, frozen,
            (_proposal(_site_records(design, frozen, 0, side='back')[0]),))
