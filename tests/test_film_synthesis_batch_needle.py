"""Original-coordinate simultaneous needle and full-response regression gates."""
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_optimization_structure import (
    FilmStructureOptimizationRequest, _site_records,
    evaluate_frozen_structure_candidate, freeze_film_structure_optimization_request,
)
from coat_sim.core.film_synthesis_batch_needle import (
    BatchNeedleProposal, batch_needle_parent_fingerprint,
    build_batch_needle_design, cleanup_batch_needle_design, scan_batch_needles,
)

pytestmark = pytest.mark.algorithm


class _Materials:
    def __init__(self, records):
        self.records = {record['id']: deepcopy(record) for record in records}

    def _records(self):
        return deepcopy(list(self.records.values()))

    def get(self, identifier):
        return deepcopy(self.records[identifier])


class _Service:
    validate = staticmethod(validate_design)
    freeze_design = FilmDesignService.freeze_design

    def __init__(self, records):
        self.materials = _Materials(records)


def _row(identifier, material='host', thickness=100., optical='nk'):
    return dict(id=identifier, material_id=material, optical_set_id=optical,
                thickness=dict(value=thickness, unit='nm'), note='fixture')


def _problem(rows=None):
    records = []
    for name, n, k in [('bulk', 1.6, .002), ('host', 1.45, .001), ('needle', 2.3, .003)]:
        sets = []
        for optical_id in ('nk', 'other'):
            sets.append(dict(id=optical_id, name=name, kind='tabulated',
                source='synthetic validation fixture, not material evidence',
                wavelength_unit='nm', wavelength_min=400., wavelength_max=900.,
                wavelengths=[400., 600., 900.], n=[n]*3, k=[k]*3, notes='fixture'))
        records.append(dict(id=name, name=name, design_ready=True, optical_sets=sets))
    design = new_design(design_id='batch-test')
    design['substrate'].update(material_id='bulk', optical_set_id='nk')
    design['front_layers'] = deepcopy(rows if rows is not None else [_row('original-host')])
    service = _Service(records)
    base = FilmOptimizationRequest(design_snapshot=design, design_revision=0,
        variables=(), objectives=(FilmOptimizationObjective(450., 800., 9, pointwise=FilmObjectiveConstraint(mode='range', target_value=None, lower_bound=None, upper_bound=.01), angles_deg=(0., 37.), polarization='unpolarized'),))
    request = FilmStructureOptimizationRequest(base,
        candidate_materials=(FilmNeedleMaterialReference('needle', 'nk'),
                             FilmNeedleMaterialReference('host', 'nk')),
        min_thickness_nm=.0001, max_thickness_nm=500.,
        max_total_thickness_nm=1e6, max_total_layers=max(32, len(design['front_layers']) + 10))
    frozen = freeze_film_structure_optimization_request(service, request)
    return frozen, frozen.base_request.design_snapshot, frozen.base_request.frozen_material_snapshot


def _proposal(site, ordinal=0, material='needle', *, parent=''):
    return BatchNeedleProposal(deepcopy(site), FilmNeedleMaterialReference(material, 'nk'),
                               -1., f'needle-{ordinal}', f'split-{ordinal}',
                               parent_fingerprint=parent)


def _evaluate(request):
    def evaluate(design, *, variable_ids=(), zero_ids=()):
        return evaluate_frozen_structure_candidate(request, design,
            variable_ids=variable_ids, with_jacobian=bool(variable_ids),
            zero_thickness_layer_ids=zero_ids)
    return evaluate


def _spectra(value):
    return np.concatenate([np.asarray(row['actual_values']) for row in value['responses']])


def test_same_original_host_three_needles_and_interfaces_conserve_geometry():
    request, design, frozen = _problem([_row('first', thickness=100), _row('second', thickness=80)])
    sites = _site_records(design, frozen, 3)
    proposals = [_proposal(site, i) for i, site in enumerate(sites)
                 if site['host_layer_id'] == 'first' or site['kind'] == 'interface']
    # Deliberately not spatial/proposal order. Builder reconstructs one base.
    built = build_batch_needle_design(design, frozen, tuple(reversed(proposals)))
    by_id = {row['id']: row for row in built.design['front_layers']}
    segments = built.host_segments['first']
    assert segments[0] == 'first'
    assert [by_id[identifier]['thickness']['value'] for identifier in segments] == [25.] * 4
    assert sum(by_id[identifier]['thickness']['value'] for identifier in segments) == 100.
    assert built.host_segments['second'] == ('second',)
    assert built.layer_ids == tuple(row['id'] for row in built.design['front_layers'])
    assert built.needle_ids == tuple(identifier for identifier in built.layer_ids if identifier.startswith('needle-'))
    assert set(built.zero_thickness_layer_ids) == set(built.needle_ids)
    assert built.design['substrate'] == design['substrate']
    assert design['front_layers'][0]['thickness']['value'] == 100.
    for proposal in proposals:
        anchor = built.anchors[proposal.needle_id]
        assert anchor['original_fraction'] == proposal.site['fraction']
        assert anchor['original_host_id'] == proposal.site['host_layer_id']


def test_near_zero_batch_adds_only_seed_and_does_not_subtract_from_host():
    request, design, frozen = _problem()
    sites = [site for site in _site_records(design, frozen, 3) if site['kind'] == 'interior']
    proposals = tuple(_proposal(site, i) for i, site in enumerate(sites))
    seeds = {proposal.needle_id: (i+1)*1e-12 for i, proposal in enumerate(proposals)}
    built = build_batch_needle_design(design, frozen, proposals, seed_thickness_nm=seeds)
    by_id = {row['id']: row for row in built.design['front_layers']}
    assert sum(by_id[i]['thickness']['value'] for i in built.host_segments['original-host']) == 100.
    assert {i: by_id[i]['thickness']['value'] for i in built.needle_ids} == seeds
    assert not built.zero_thickness_layer_ids
    assert all(by_id[i]['thickness']['value'] < .0001 for i in built.needle_ids)
    # The structural request's manufacturing .0001 nm is NOT a mathematical lower bound.
    assert np.all(np.isfinite(_spectra(_evaluate(request)(built.design))))


def test_simultaneous_zero_batch_full_spectrum_and_all_needle_jacobians_match_single():
    request, design, frozen = _problem([_row('a', thickness=93.), _row('b', 'needle', 51.)])
    chosen = [site for site in _site_records(design, frozen, 3)
              if site['host_layer_id'] == 'a' or site['kind'] == 'interface']
    proposals = tuple(_proposal(site, i) for i, site in enumerate(chosen))
    built = build_batch_needle_design(design, frozen, tuple(reversed(proposals)))
    evaluate = _evaluate(request)
    joint = evaluate(built.design, variable_ids=built.layer_ids, zero_ids=built.zero_thickness_layer_ids)
    baseline = evaluate(design)
    np.testing.assert_allclose(_spectra(joint), _spectra(baseline), atol=1e-13, rtol=0)
    for proposal in proposals:
        single = build_batch_needle_design(design, frozen, (proposal,))
        value = evaluate(single.design, variable_ids=single.layer_ids, zero_ids=single.zero_thickness_layer_ids)
        for response, single_response in zip(joint['responses'], value['responses'], strict=True):
            np.testing.assert_allclose(response['jacobian_values_by_layer_id'][proposal.needle_id],
                single_response['jacobian_values_by_layer_id'][proposal.needle_id], atol=2e-16, rtol=1e-10)


def test_joint_jacobian_columns_match_built_rows_finite_differences_not_proposal_order():
    request, design, frozen = _problem([_row('a', thickness=93.), _row('b', 'needle', 51.)])
    sites = _site_records(design, frozen, 1)
    proposals = (_proposal(sites[-1], 0), _proposal(sites[1], 1), _proposal(sites[0], 2))
    built = build_batch_needle_design(design, frozen, proposals, seed_thickness_nm=1.25)
    evaluate = _evaluate(request)
    joint = evaluate(built.design, variable_ids=built.layer_ids)
    assert built.needle_ids != tuple(proposal.needle_id for proposal in proposals)
    step = 1e-4
    for identifier in built.layer_ids:
        high, low = deepcopy(built.design), deepcopy(built.design)
        next(row for row in high['front_layers'] if row['id'] == identifier)['thickness']['value'] += step
        next(row for row in low['front_layers'] if row['id'] == identifier)['thickness']['value'] -= step
        fd = (_spectra(evaluate(high)) - _spectra(evaluate(low))) / (2*step)
        analytic = np.concatenate([response['jacobian_values_by_layer_id'][identifier]
                                   for response in joint['responses']])
        np.testing.assert_allclose(analytic, fd, atol=2e-11, rtol=2e-6)


def test_actual_finite_joint_response_is_not_additive_single_needles():
    request, design, frozen = _problem()
    sites = [site for site in _site_records(design, frozen, 3) if site['kind'] == 'interior']
    proposals = tuple(_proposal(site, i) for i, site in enumerate(sites))
    evaluate = _evaluate(request)
    baseline = _spectra(evaluate(design))
    joint = build_batch_needle_design(design, frozen, proposals, seed_thickness_nm=10.)
    actual = _spectra(evaluate(joint.design))
    additive = baseline.copy()
    for proposal in proposals:
        single = build_batch_needle_design(design, frozen, (proposal,), seed_thickness_nm=10.)
        additive += _spectra(evaluate(single.design)) - baseline
    assert np.max(np.abs(actual-additive)) > 1e-4


def test_exact_zero_cleanup_and_same_source_merge_are_optically_equivalent():
    request, design, frozen = _problem()
    sites = _site_records(design, frozen, 3)
    built = build_batch_needle_design(design, frozen, tuple(_proposal(site, i) for i, site in enumerate(sites)))
    cleaned = cleanup_batch_needle_design(built.design, frozen)
    assert cleaned.algebraically_equivalent and not cleaned.requires_full_reevaluation
    assert set(cleaned.removed_exact_zero_ids) == set(built.needle_ids)
    assert len(cleaned.design['front_layers']) == 1
    assert cleaned.design['front_layers'][0]['thickness']['value'] == 100.
    evaluate = _evaluate(request)
    np.testing.assert_allclose(_spectra(evaluate(cleaned.design)), _spectra(evaluate(built.design)), atol=1e-13, rtol=0)


def test_cleanup_different_optical_sets_never_merge_and_bulk_is_never_merged():
    request, design, frozen = _problem([_row('bulk-film', 'bulk', 25.),
        _row('same-a', 'host', 15.), _row('same-b', 'host', 20.),
        _row('different-set', 'host', 30., 'other')])
    cleaned = cleanup_batch_needle_design(design, frozen)
    assert [row['id'] for row in cleaned.design['front_layers']] == ['bulk-film', 'same-a', 'different-set']
    assert cleaned.merged_ids == {'same-b': 'same-a'}
    assert cleaned.design['front_layers'][0]['thickness']['value'] == 25.
    assert cleaned.design['substrate'] == design['substrate']


def test_positive_near_zero_removal_is_explicit_not_equivalence_or_manufacturing():
    request, design, frozen = _problem([_row('a', thickness=100.), _row('tiny', 'needle', .01)])
    design = deepcopy(design)
    design['front_layers'][-1]['thickness']['value'] = 1e-7
    assert len(cleanup_batch_needle_design(design, frozen).design['front_layers']) == 2
    cleaned = cleanup_batch_needle_design(design, frozen, near_zero_nm=1e-6)
    assert cleaned.removed_near_zero_ids == ('tiny',)
    assert not cleaned.algebraically_equivalent and cleaned.requires_full_reevaluation
    assert not cleaned.removed_exact_zero_ids


@pytest.mark.parametrize('layers', [70, 104])
def test_no_implicit_70_or_104_layer_cap(layers):
    request, design, frozen = _problem([_row(f'host-{i}', thickness=10.) for i in range(layers)])
    sites = [site for site in _site_records(design, frozen, 1) if site['kind'] == 'interior'][:2]
    built = build_batch_needle_design(design, frozen, tuple(_proposal(site, i) for i, site in enumerate(sites)))
    assert len(built.design['front_layers']) == layers+4


def test_stale_parent_ids_geometry_and_original_interface_neighbours_rejected():
    request, design, frozen = _problem()
    interior = next(site for site in _site_records(design, frozen, 1) if site['kind'] == 'interior')
    fingerprint = batch_needle_parent_fingerprint(design, frozen)
    proposal = _proposal(interior, parent=fingerprint)
    changed = deepcopy(design)
    changed['front_layers'][0]['thickness']['value'] += 1
    with pytest.raises(ValueError, match='different original parent'):
        build_batch_needle_design(changed, frozen, (proposal,))
    with pytest.raises(ValueError, match='thickness changed'):
        build_batch_needle_design(changed, frozen, (replace(proposal, parent_fingerprint=''),))
    interface = _site_records(design, frozen, 0)[0]
    interface['right_layer_id'] = 'wrong'
    with pytest.raises(ValueError, match='neighbour'):
        build_batch_needle_design(design, frozen, (_proposal(interface),))


def test_colliding_positions_and_ids_are_rejected():
    request, design, frozen = _problem()
    site = _site_records(design, frozen, 1)[1]
    a, b = _proposal(site, 0), _proposal(site, 1, material='host')
    with pytest.raises(ValueError, match='same original position'):
        build_batch_needle_design(design, frozen, (a, b))
    with pytest.raises(ValueError, match='unique'):
        build_batch_needle_design(design, frozen, (replace(a, needle_id='original-host'),))


def _valley_evaluator(calls):
    def evaluate(design, *, variable_ids=(), zero_ids=()):
        assert tuple(variable_ids) == tuple(row['id'] for row in design['front_layers'])
        assert len(zero_ids) == 1
        identifier = zero_ids[0]
        rows = design['front_layers']
        needle_index = next(i for i, row in enumerate(rows) if row['id'] == identifier)
        fraction = sum(row['thickness']['value'] for row in rows[:needle_index])/100.
        reference = rows[needle_index]['material_id']
        # A material-dependent narrow valley between the initial 0 and .5 nodes.
        derivative = (fraction-.25)**2 - (.04 if reference == 'needle' else .01)
        calls.append((reference, fraction))
        return dict(design=deepcopy(design), gradient_by_layer_id={i: (derivative if i == identifier else 0.) for i in variable_ids})
    return evaluate


def test_all_materials_positions_and_adaptive_derivative_valleys_are_scanned():
    request, design, frozen = _problem()
    calls: list[tuple[str, float]] = []
    refs = request.candidate_materials
    scan = scan_batch_needles(design, frozen, refs, evaluate=_valley_evaluator(calls),
        position_samples=1, valley_refinement_rounds=2, max_needles=3)
    assert set((material, position) for material, position in calls if position in (0., .5, 1.)) == {
        (ref.material_id, p) for ref in refs for p in (0., .5, 1.)}
    assert ('needle', .25) in calls
    assert scan.refined_probe_count > 0 and scan.complete
    assert scan.selected and scan.selected[0].site['fraction'] == .25
    assert all(proposal.sensitivity < 0 for proposal in scan.selected)
    assert len({_anchor(proposal.site) for proposal in scan.selected}) == len(scan.selected)
    assert scan.probe_count == len(calls)


def _anchor(site):
    return (site['kind'], site['host_layer_id'], site['fraction'], site['interface'])


def test_custom_equal_window_gradient_uses_actual_built_column_order():
    request, design, frozen = _problem()
    calls: list[tuple[str, float]] = []
    def custom_gradient(value, identifiers):
        assert tuple(identifiers) == tuple(row['id'] for row in value['design']['front_layers'])
        return [-7. if identifier.startswith('batch-needle') else 100.
                for identifier in identifiers]
    scan = scan_batch_needles(design, frozen, request.candidate_materials,
        evaluate=_valley_evaluator(calls), gradient=custom_gradient,
        position_samples=1, valley_refinement_rounds=0, max_needles=2)
    assert {proposal.sensitivity for proposal in scan.proposals} == {-7.}
    assert len(scan.selected) == 2


def test_stage_budget_keeps_accumulated_proposals_and_rotation_changes_start():
    request, design, frozen = _problem()
    calls: list[tuple[str, float]] = []
    later: list[tuple[str, float]] = []
    first = scan_batch_needles(design, frozen, request.candidate_materials,
        evaluate=_valley_evaluator(calls), position_samples=5,
        max_probes=3, max_needles=2, seed=0)
    second = scan_batch_needles(design, frozen, request.candidate_materials,
        evaluate=_valley_evaluator(later), position_samples=5,
        max_probes=3, max_needles=2, seed=1)
    assert first.probe_count == second.probe_count == 3
    assert not first.complete and first.stop_reason == 'stage_budget'
    assert first.proposals
    assert calls[0] != later[0]


def test_resource_skips_are_incomplete_and_do_not_claim_stationarity():
    request, design, frozen = _problem()
    calls: list[tuple[str, float]] = []
    scan = scan_batch_needles(design, frozen, request.candidate_materials,
        evaluate=_valley_evaluator(calls), admit=lambda design: False)
    assert not scan.complete and scan.skipped_probe_count > 0
    assert not scan.proposals and not scan.selected and not calls


def test_cancel_or_parent_budget_exception_propagates_unchanged():
    request, design, frozen = _problem()
    class Cancelled(Exception):
        pass
    def fail(*args, **kwargs):
        raise Cancelled('caller cancellation')
    with pytest.raises(Cancelled, match='caller cancellation'):
        scan_batch_needles(design, frozen, request.candidate_materials, evaluate=fail)


@pytest.mark.parametrize('kwargs', [dict(position_samples=-1), dict(max_needles=0),
    dict(valley_refinement_rounds=-1), dict(max_probes=0), dict(seed=True)])
def test_scan_controls_reject_invalid_values(kwargs):
    request, design, frozen = _problem()
    with pytest.raises(ValueError):
        scan_batch_needles(design, frozen, request.candidate_materials,
                           evaluate=_valley_evaluator([]), **kwargs)


def test_parent_fingerprint_hashes_actual_nk_not_a_reused_metadata_digest():
    request, design, frozen = _problem()
    before = batch_needle_parent_fingerprint(design, frozen)
    site = next(site for site in _site_records(design, frozen, 1) if site['kind'] == 'interior')
    proposal = _proposal(site, parent=before)
    changed = deepcopy(frozen)
    changed['materials'][0]['optical_sets'][0]['n'][0] += .01
    assert changed['sha256'] == frozen['sha256']
    assert batch_needle_parent_fingerprint(design, changed) != before
    with pytest.raises(ValueError, match='different original parent'):
        build_batch_needle_design(design, changed, (proposal,))


def test_bounded_scan_reserves_real_valley_refinement_and_reports_partial_grid():
    request, design, frozen = _problem()
    calls: list[tuple[str, float]] = []
    scan = scan_batch_needles(design, frozen, request.candidate_materials,
        evaluate=_valley_evaluator(calls), position_samples=15, max_probes=16,
        max_needles=2, valley_refinement_rounds=1)
    assert scan.probe_count <= 16
    assert scan.refined_probe_count > 0
    assert not scan.complete and scan.stop_reason == 'stage_budget'


def test_batch_only_resource_failure_is_incomplete_even_if_individual_probes_fit():
    request, design, frozen = _problem()
    calls: list[tuple[str, float]] = []
    scan = scan_batch_needles(design, frozen, request.candidate_materials,
        evaluate=_valley_evaluator(calls), gradient=lambda value, ids: [-1.] * len(ids),
        admit=lambda design: len(design['front_layers']) <= 3,
        position_samples=1, valley_refinement_rounds=0, max_needles=3)
    assert len(scan.selected) == 2
    assert not scan.complete and scan.skipped_batch_count > 0
    assert scan.stop_reason == 'batch_resource_skips'


def test_genuine_only_excludes_same_source_growth_but_reports_complete_scan():
    request, design, frozen = _problem()
    calls: list[tuple[str, float]] = []
    scan = scan_batch_needles(design, frozen, request.candidate_materials,
        evaluate=_valley_evaluator(calls), genuine_only=True,
        position_samples=1, valley_refinement_rounds=0)
    assert scan.complete
    assert scan.topology_equivalent_skip_count == 3
    assert {material for material, _ in calls} == {'needle'}
    assert scan.probe_count == 3 and scan.skipped_probe_count == 0


def test_genuine_only_never_treats_bulk_as_mergeable_film_neighbour():
    request, design, frozen = _problem()
    bare = deepcopy(design)
    bare['front_layers'] = []
    calls: list[tuple[str, float]] = []
    scan = scan_batch_needles(bare, frozen, (FilmNeedleMaterialReference('bulk', 'nk'),),
        evaluate=_valley_evaluator(calls), genuine_only=True,
        position_samples=1, valley_refinement_rounds=0)
    assert scan.complete and scan.probe_count == 1
    assert scan.topology_equivalent_skip_count == 0
    assert calls == [('bulk', 0.)]


def test_genuine_only_compares_both_material_and_optical_set_at_interfaces():
    request, design, frozen = _problem([_row('host-a'), _row('host-b', 'needle', 50., 'other')])
    calls: list[tuple[str, float]] = []
    scan = scan_batch_needles(design, frozen, (FilmNeedleMaterialReference('needle', 'nk'),),
        evaluate=_valley_evaluator(calls), genuine_only=True,
        position_samples=0, valley_refinement_rounds=0)
    assert scan.complete and scan.probe_count == 3
    assert scan.topology_equivalent_skip_count == 0
