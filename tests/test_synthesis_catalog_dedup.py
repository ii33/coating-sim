"""Per-invocation material catalog deduplication preserves canonical physics."""
from copy import deepcopy
from dataclasses import replace

import pytest

from coat_sim.core import film_synthesis_physics as physics
from coat_sim.core.film_design_service import active_material_references
from coat_sim.core.film_optimization_derivatives import _PreparedMaterialData
from coat_sim.core.film_auto_synthesis import (freeze_film_synthesis_request, _proxy,
                                               evaluate_frozen_structure_candidate)
from tests.test_film_auto_synthesis import setup_problem
from tests.test_film_synthesis_selected_surfaces_core import _row

pytestmark = pytest.mark.algorithm


def _case(sides):
    service, base = setup_problem(low=540., high=560., samples=3)
    alt = deepcopy(service.materials.records['low']['optical_sets'][0])
    alt.update(id='other', name='other', n=[1.4, 1.45, 1.5])
    service.materials.records['low']['optical_sets'].append(alt)
    design = deepcopy(base.environment)
    design['substrate']['sides'] = sides
    design['front_layers'] = [_row('f1'), _row('f2'), _row('f3')]
    design['front_layers'][1]['optical_set_id'] = 'other'
    if sides == 'double':
        design['back_layers'] = [_row('b1'), _row('b2')]
        design['back_layers'][1]['optical_set_id'] = 'other'
    settings = dict(wavelength_min_nm=540., wavelength_max_nm=560., samples=3,
                    angle_deg=27., polarization='unpolarized')
    frozen = service.freeze_design(design, settings)
    variables = [dict(side=side, layer_id=row['id'], lower_nm=0., upper_nm=1000.)
                 for side in ('front', 'back') for row in design[side + '_layers']]
    return service, base, design, frozen, variables, settings


def _old_catalog(prepared, design):
    # Original implementation intentionally copies every reference, overwriting
    # earlier entries with the same material ID.
    return {row['material_id']: prepared._material_record(row['material_id'])
            for row in active_material_references(design)}


@pytest.mark.parametrize(('sides', 'first_surface'),
                         [('single', 'front'), ('double', 'front'), ('double', 'back')])
def test_unique_detached_catalog_matches_per_reference_path(monkeypatch, sides, first_surface):
    service, base, design, frozen, variables, settings = _case(sides)
    design['first_surface'] = first_surface
    settings['first_surface'] = first_surface
    original_design, original_frozen = deepcopy(design), deepcopy(frozen)
    calls = []
    original_copy = _PreparedMaterialData._material_record
    def counted(self, identifier):
        calls.append(identifier)
        return original_copy(self, identifier)
    monkeypatch.setattr(_PreparedMaterialData, '_material_record', counted)
    actual = physics.simulate_synthesis_with_thickness_jacobian(design, frozen, variables, settings)
    expected_ids = {r['material_id'] for r in active_material_references(design)}
    assert set(calls) == expected_ids and len(calls) == len(expected_ids)
    calls.clear()
    again = physics.simulate_synthesis_with_thickness_jacobian(design, frozen, variables, settings)
    assert set(calls) == expected_ids and len(calls) == len(expected_ids)
    assert again == actual
    calls.clear()
    monkeypatch.setattr(physics, '_detached_material_catalog', _old_catalog)
    expected = physics.simulate_synthesis_with_thickness_jacobian(design, frozen, variables, settings)
    assert len(calls) == len(active_material_references(design)) > len(expected_ids)
    assert actual == expected
    old_forward = physics.simulate_synthesis_with_thickness_jacobian(design, frozen, [], settings)
    monkeypatch.undo()
    new_forward = physics.simulate_synthesis_with_thickness_jacobian(design, frozen, [], settings)
    assert new_forward == old_forward
    monkeypatch.setattr(physics, '_detached_material_catalog', _old_catalog)
    assert design == original_design and frozen == original_frozen

    # The candidate scorer consumes the same forward and Jacobian path, and
    # must retain every per-condition error as well as the gradient.
    env = deepcopy(design)
    env['front_layers'] = []
    env['back_layers'] = []
    req = replace(base, environment=env, seed_designs=(design,),
                  selected_surfaces=('front', 'back') if sides == 'double' else ('front',))
    req = freeze_film_synthesis_request(service, req)
    ids = tuple(v['layer_id'] for v in variables)
    old_score = evaluate_frozen_structure_candidate(_proxy(req), design, variable_ids=ids, with_jacobian=True)
    old_forward_score = evaluate_frozen_structure_candidate(_proxy(req), design, with_jacobian=False)
    monkeypatch.undo()
    new_score = evaluate_frozen_structure_candidate(_proxy(req), design, variable_ids=ids, with_jacobian=True)
    new_forward_score = evaluate_frozen_structure_candidate(_proxy(req), design, with_jacobian=False)
    assert new_score == old_score
    assert new_forward_score == old_forward_score


def test_catalog_contains_all_optical_sets_and_records_are_detached():
    _, _, design, frozen, _, _ = _case('double')
    prepared = _PreparedMaterialData(frozen, design)
    first = physics._detached_material_catalog(prepared, design)
    assert {row['id'] for row in first['low']['optical_sets']} == {'nk', 'other'}
    first['low']['optical_sets'][0]['n'][0] = 999.
    second = physics._detached_material_catalog(prepared, design)
    assert second['low']['optical_sets'][0]['n'][0] != 999.
    assert frozen['materials'] != list(first.values())


@pytest.mark.parametrize('sides', ['single', 'double'])
def test_invalid_source_fails_in_both_paths(monkeypatch, sides):
    _, _, design, frozen, variables, settings = _case(sides)
    # An invalid digest is rejected before any catalog is built.
    frozen['materials'][0]['optical_sets'][0]['n'][0] = -1.
    with pytest.raises(ValueError, match='digest'):
        physics.simulate_synthesis_with_thickness_jacobian(design, frozen, variables, settings)
    monkeypatch.setattr(physics, '_detached_material_catalog', _old_catalog)
    with pytest.raises(ValueError, match='digest'):
        physics.simulate_synthesis_with_thickness_jacobian(design, frozen, variables, settings)


@pytest.mark.parametrize('sides', ['single', 'double'])
def test_missing_optical_source_fails_in_both_paths(monkeypatch, sides):
    _, _, design, frozen, variables, settings = _case(sides)
    rows = deepcopy(frozen['materials'])
    low = next(row for row in rows if row['id'] == 'low')
    low['optical_sets'] = [row for row in low['optical_sets'] if row['id'] != 'other']
    with pytest.raises((ValueError, KeyError)) as actual:
        physics.simulate_synthesis_with_thickness_jacobian(design, rows, variables, settings)
    monkeypatch.setattr(physics, '_detached_material_catalog', _old_catalog)
    with pytest.raises(type(actual.value)) as expected:
        physics.simulate_synthesis_with_thickness_jacobian(design, rows, variables, settings)
    assert str(actual.value) == str(expected.value)
