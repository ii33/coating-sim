"""Contract tests for unscored, material-generic adaptive structure presets.

Synthetic optical constants in these fixtures are not measured materials and
proposal creation alone is never evidence that an objective is met.
"""
from copy import deepcopy
from dataclasses import replace
from itertools import islice

import pytest

from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
from coat_sim.core.film_design import validate_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_synthesis_global import StructuralProposal
from coat_sim.core import film_synthesis_adaptive_presets as adaptive
from tests.test_film_auto_synthesis import setup_problem
from tests.test_film_synthesis_bandpass_proposals import bandpass_request

pytestmark = pytest.mark.algorithm


def reflector_request():
    service, base = setup_problem()
    # Nonzero absorption is deliberately retained. These are analytical n/k.
    service.materials.records['high']['optical_sets'][0]['k'] = [.02] * 3
    objectives = (
        FilmOptimizationObjective(510., 530., 3, metric='R',
            pointwise=FilmObjectiveConstraint(lower_bound=.8, upper_bound=None)),
        FilmOptimizationObjective(560., 600., 3, metric='R',
            pointwise=FilmObjectiveConstraint(mode='value', target_value=.9, lower_bound=None, upper_bound=None)),
    )
    return freeze_film_synthesis_request(service, replace(base, objectives=objectives,
        max_layers=32, search_strategy='hybrid'))


def factories():
    return ((adaptive.iter_apodized_reflector_proposals, reflector_request()),
            (adaptive.iter_asymmetric_cavity_proposals, bandpass_request(max_layers=32)[1]))


def physical_rows(proposal, side='front'):
    return [(r['material_id'], r['optical_set_id'], r['thickness']['value'])
            for r in proposal.design[side + '_layers']]


def test_presets_are_unscored_real_materials_and_do_not_mutate_request(monkeypatch):
    from coat_sim.core import film_auto_synthesis as synthesis
    def forbidden(*args, **kwargs):
        raise AssertionError('unscored proposal must not run physical acceptance')
    monkeypatch.setattr(synthesis, 'evaluate_frozen_structure_candidate', forbidden)
    for factory, request in factories():
        before = deepcopy(request)
        proposals = list(islice(factory(request), 8))
        assert proposals, factory.__name__
        assert request == before
        refs = {(r.material_id, r.optical_set_id) for r in request.candidate_materials}
        for p in proposals:
            assert isinstance(p, StructuralProposal)
            assert validate_design(p.design)
            assert 'symmetry_groups' not in p.provenance
            assert p.provenance['family'] in {'apodized_reflector', 'asymmetric_cavity'}
            rows = physical_rows(p)
            assert rows and all((m, o) in refs and d > 0 for m, o, d in rows)
            assert {m for m, _, _ in rows} == {'low', 'high'}
            for key in ('substrate', 'incident_medium', 'exit_medium', 'first_surface',
                        'layer_order', 'use_k', 'assume_lossless'):
                assert p.design[key] == request.environment[key]
        high = next(m for m in request.frozen_material_snapshot['materials'] if m['id'] == 'high')
        if factory is adaptive.iter_apodized_reflector_proposals:
            assert high['optical_sets'][0]['k'] == [.02] * 3


def test_objective_reordering_still_generates_both_families():
    for factory, request in factories():
        assert next(factory(request), None) is not None
        reversed_request = replace(request, objectives=tuple(reversed(request.objectives)))
        assert next(factory(reversed_request), None) is not None


def test_apodization_changes_quarter_wave_and_has_multiple_edge_profiles():
    request = reflector_request()
    proposals = list(islice(adaptive.iter_apodized_reflector_proposals(request), 16))
    assert len(proposals) >= 2
    # At normal incidence, the ratio to the quarter-wave n*d is independent
    # of material. A true apodization must vary within at least one stack.
    ns = {'low': 1.5 ** .5, 'high': 2.2}
    optical = [[ns[m] * d for m, _, d in physical_rows(p)] for p in proposals]
    assert any(max(v) - min(v) > 1e-6 for v in optical)
    assert len({tuple(round(x, 8) for x in v) for v in optical}) >= 2


def test_asymmetric_cavity_is_a_real_changed_stack_and_cavity_survives():
    request = bandpass_request(max_layers=32)[1]
    proposals = list(islice(adaptive.iter_asymmetric_cavity_proposals(request), 12))
    assert proposals
    for p in proposals:
        rows = physical_rows(p)
        assert len(rows) >= 7  # two-pair mirror seed less one outer pair
        assert rows != list(reversed(rows))
        assert p.provenance.get('cavity_count', 0) >= 1


@pytest.mark.parametrize('limits', [
    {'max_layers': 1}, {'max_layer_thickness_nm': 1.},
    {'max_total_thickness_nm': 1.}, {'minimum_nonzero_thickness_nm': 10000.},
])
def test_explicit_limits_are_never_clipped_or_ignored(limits):
    for factory, request in factories():
        events: list[dict] = []
        limited = replace(request, **limits)
        assert list(factory(limited, report=events.append)) == []
        assert events  # Rejections should explain why no structures were supplied.


def test_unavailable_materials_and_inapplicable_targets_do_not_generate():
    for factory, request in factories():
        assert list(factory(replace(request, candidate_materials=()))) == []
        no_positive_goal = tuple(replace(o, weight=0.) for o in request.objectives)
        assert list(factory(replace(request, objectives=no_positive_goal))) == []


def test_cancellation_propagates():
    class Cancelled(Exception):
        pass
    def cancel():
        raise Cancelled('caller cancellation')
    for factory, request in factories():
        with pytest.raises(Cancelled, match='caller cancellation'):
            next(factory(request, cancel_check=cancel))


@pytest.mark.parametrize('selected', [('back',), ('front', 'back')])
def test_double_surface_real_environment_and_inactive_stack_are_preserved(selected):
    for factory, request in factories():
        env = deepcopy(request.environment)
        env['substrate']['sides'] = 'double'
        env['first_surface'] = 'back'
        if selected == ('back',):
            env['front_layers'] = [dict(id='fixed-front', material_id='low', optical_set_id='nk',
                thickness=dict(value=77., unit='nm'), note='unchanged fixed stack')]
        two_sided = replace(request, environment=env, selected_surfaces=selected,
                            max_layers=64, max_total_thickness_nm=20000.)
        original = deepcopy(two_sided)
        p = next(factory(two_sided), None)
        assert p is not None
        assert two_sided == original
        assert p.design['substrate'] == env['substrate']
        assert p.design['first_surface'] == 'back'
        assert 'symmetry_groups' not in p.provenance
        for side in ('front', 'back'):
            if side in selected:
                assert physical_rows(p, side)
            else:
                assert p.design[side + '_layers'] == env[side + '_layers']
        assert sum(len(p.design[s + '_layers']) for s in selected) <= 64


def test_scaled_wavelengths_and_renamed_materials_remain_generic():
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    factor = 1.7
    names = {'low': 'synthetic_A', 'high': 'synthetic_B'}
    for factory, request in factories():
        first = next(factory(request))
        snapshot = deepcopy(request.frozen_material_snapshot)
        for material in snapshot['materials']:
            material['id'] = names.get(material['id'], material['id'])
            for optical in material['optical_sets']:
                optical['wavelengths'] = [factor * w for w in optical['wavelengths']]
                optical['wavelength_min'] *= factor
                optical['wavelength_max'] *= factor
        scaled = replace(request, frozen_material_snapshot=snapshot,
            candidate_materials=tuple(FilmNeedleMaterialReference(names[r.material_id], r.optical_set_id)
                                      for r in request.candidate_materials),
            objectives=tuple(replace(o, wavelength_min_nm=factor * o.wavelength_min_nm,
                                     wavelength_max_nm=factor * o.wavelength_max_nm)
                             for o in request.objectives))
        changed = next(factory(scaled))
        a, b = physical_rows(first), physical_rows(changed)
        assert [(names[m], o) for m, o, _ in a] == [(m, o) for m, o, _ in b]
        assert [d for _, _, d in b] == pytest.approx([factor * d for _, _, d in a])


def test_unregistered_adaptive_policy_does_not_change_default_fingerprint(monkeypatch):
    from coat_sim.core.film_synthesis_continuation import search_fingerprint
    request = reflector_request()
    old = search_fingerprint(request)
    monkeypatch.setattr(adaptive, 'ADAPTIVE_PRESET_POLICY', 'test-incompatible-future-policy')
    assert search_fingerprint(request) == old


def test_apodization_matches_source_qw_pair_phase_envelope():
    import math
    from coat_sim.core import film_synthesis_global as global_search
    request = reflector_request()
    source = next(p for p in global_search.iter_reflector_proposals(request)
                  if p.provenance['family'] == 'periodic' and len(physical_rows(p)) >= 4)
    variants = list(islice(adaptive.iter_apodized_reflector_proposals(request), 4))
    assert {(p.provenance['edge_high_phase_fraction'], p.provenance['inverted_phase_duty'])
            for p in variants} == {(.1, False), (.1, True), (.2, False), (.2, True)}
    original = physical_rows(source)
    high = source.provenance['high']
    pairs = len(original) // 2
    for p in variants:
        current = physical_rows(p)
        assert [(m, o) for m, o, _ in current] == [(m, o) for m, o, _ in original]
        assert current != original
        edge = p.provenance['edge_high_phase_fraction']
        for i in range(pairs):
            duty = edge + (.5 - edge) * math.sin(math.pi * i / (pairs - 1)) ** 2
            if p.provenance['inverted_phase_duty']:
                duty = 1 - duty
            for j in (2 * i, 2 * i + 1):
                multiplier = 2 * (duty if original[j][:2] == high else 1 - duty)
                assert current[j][2] == pytest.approx(original[j][2] * multiplier)


def test_unequal_mirrors_remove_one_pair_from_each_side_of_real_source():
    from coat_sim.core import film_synthesis_global as global_search
    request = bandpass_request(max_layers=32)[1]
    source = next(p for p in global_search.iter_bandpass_proposals(request)
                  if p.provenance['mirror_pairs'] >= 2)
    variants = list(islice(adaptive.iter_asymmetric_cavity_proposals(request), 2))
    assert {p.provenance['reduced_mirror_side'] for p in variants} == {'substrate', 'incident'}
    before = physical_rows(source)
    pad = int(bool(source.provenance['matching_quarters']))
    for p in variants:
        side = p.provenance['reduced_mirror_side']
        start = pad if side == 'substrate' else len(before) - pad - 2
        assert physical_rows(p) == before[:start] + before[start + 2:]
        assert p.provenance['source_mirror_pairs'] == source.provenance['mirror_pairs']
        assert p.provenance['substrate_mirror_pairs'] == source.provenance['mirror_pairs'] - (side == 'substrate')
        assert p.provenance['incident_mirror_pairs'] == source.provenance['mirror_pairs'] - (side == 'incident')


def test_ten_source_experimental_checkpoint_is_rejected():
    from coat_sim.core import film_synthesis_continuation as continuation
    request = reflector_request()
    experimental_sources = continuation.SOURCE_NAMES + ('apodized', 'asymmetric_cavity')
    assert len(continuation.SOURCE_NAMES) == 8
    # Isolate cursor incompatibility with the current fingerprint; an authentic
    # old experimental-policy fingerprint would be rejected even earlier.
    state = continuation.seal_search_state(dict(kind=continuation.SEARCH_STATE_KIND,
        version=continuation.SEARCH_STATE_VERSION, fingerprint=continuation.search_fingerprint(request),
        cursors=dict.fromkeys(experimental_sources, 0), source_cursor=0, rotations=0,
        tried=[], pending=[], pool=[], feedback={}, archive_rotation=0, archive_total_admitted=0,
        active_sources=list(experimental_sources)))
    with pytest.raises(ValueError, match='invalid search checkpoint cursors'):
        continuation.validate_search_state(request, state)


def test_default_synthesis_does_not_access_experimental_generators(monkeypatch):
    from coat_sim.core import film_auto_synthesis as synthesis
    from tests.test_film_synthesis_transmission_proposal_hints import synthetic_reference_request

    def forbidden(*_args, **_kwargs):
        raise AssertionError('experimental generator accessed by default synthesis')

    monkeypatch.setattr(adaptive, 'iter_apodized_reflector_proposals', forbidden)
    monkeypatch.setattr(adaptive, 'iter_asymmetric_cavity_proposals', forbidden)
    # Also guard aliases already imported into the synthesis module.
    monkeypatch.setattr(synthesis, 'iter_apodized_reflector_proposals', forbidden, raising=False)
    monkeypatch.setattr(synthesis, 'iter_asymmetric_cavity_proposals', forbidden, raising=False)
    request = synthetic_reference_request(max_evaluations=60)
    original_request = deepcopy(request)
    result = synthesis.synthesize_film_design(None, request)
    assert request == original_request
    assert result.frozen_request.objectives == request.objectives
    assert any(row.stage == 'structure_proposal' for row in result.trace)
    assert result.search_checkpoint is not None
    assert 'apodized' not in result.search_checkpoint['cursors']
    assert 'asymmetric_cavity' not in result.search_checkpoint['cursors']


def test_experimental_proposals_are_directly_scored_against_all_original_targets():
    from coat_sim.core import film_auto_synthesis as synthesis
    from coat_sim.core.film_synthesis_proposal_objectives import reflective_proposal_request
    from tests.test_film_synthesis_transmission_proposal_hints import synthetic_reference_request

    request = synthetic_reference_request(max_evaluations=60)
    original_request = deepcopy(request)
    hinted, hints = reflective_proposal_request(request)
    assert [o.metric for o in hinted.objectives] == ['T', 'R', 'T', 'R']
    assert hints and all(not hint['acceptance_equivalence'] for hint in hints)
    for factory in (adaptive.iter_apodized_reflector_proposals,
                    adaptive.iter_asymmetric_cavity_proposals):
        proposal = next(factory(hinted), None)
        assert proposal is not None
        result = synthesis.evaluate_frozen_structure_candidate(synthesis._proxy(request), proposal.design)
        errors = result['score']['errors']
        expected = {(index, angle) for index, objective in enumerate(request.objectives)
                    for angle in objective.angles_deg}
        assert {(row['objective_index'], row['angle_deg']) for row in result['responses']} == expected
        assert {(row.objective_index, row.angle_deg) for row in errors} == expected
        for row in errors:
            objective = request.objectives[row.objective_index]
            assert row.metric == objective.metric == 'T'
            assert row.lower_bound == objective.pointwise.lower_bound
            assert row.upper_bound == objective.pointwise.upper_bound
            assert row.strict_lower == objective.pointwise.strict_lower
            assert row.strict_upper == objective.pointwise.strict_upper
            assert objective.wavelength_min_nm <= row.wavelength_nm <= objective.wavelength_max_nm
        assert len(errors) == sum(objective.wavelength_samples * len(objective.angles_deg)
                                  for objective in request.objectives)
    assert request == original_request
