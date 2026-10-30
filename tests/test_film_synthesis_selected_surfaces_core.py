"""Request-level and end-to-end coverage for selected synthesis surfaces."""
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_auto_synthesis import (
    freeze_film_synthesis_request, synthesize_film_design,
)
from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_service import simulate_frozen
from coat_sim.core.film_optimization_structure import _site_records
from coat_sim.core.film_synthesis_continuation import search_fingerprint
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def _row(identifier, material='low', thickness=80.):
    return dict(id=identifier, material_id=material, optical_set_id='nk',
                thickness=dict(value=thickness, unit='nm'), note='')


def _double_environment():
    env = new_design(design_id='selected-surfaces-double')
    env['substrate'].update(material_id='glass', optical_set_id='nk',
                            sides='double', mode='coherent')
    env['first_surface'] = 'back'
    return env


def test_selection_validation_and_default_equivalence():
    service, base = setup_problem()
    assert base.selected_surfaces == ('front',)
    assert synthesis._validated(base).selected_surfaces == ('front',)
    assert synthesis._validated(replace(base, selected_surfaces=('front',))).selected_surfaces == ('front',)
    for invalid in ((), ('left',), ('front', 'front')):
        with pytest.raises(ValueError, match='selected_surfaces'):
            synthesis._validated(replace(base, selected_surfaces=invalid))
    single_back = replace(base, selected_surfaces=('back',))
    with pytest.raises(ValueError, match='double-sided'):
        synthesis._validated(single_back)


def test_unselected_stack_is_retained_but_selected_environment_stack_must_be_empty():
    service, base = setup_problem()
    env = _double_environment()
    env['front_layers'] = [_row('fixed-front')]
    request = replace(base, environment=env, selected_surfaces=('back',),
                      max_cycles=1, max_evaluations=20)
    frozen = freeze_film_synthesis_request(service, request)
    assert frozen.environment['front_layers'] == env['front_layers']
    assert frozen.environment['back_layers'] == []
    active = deepcopy(env)
    active['back_layers'] = [_row('selected-back')]
    with pytest.raises(ValueError, match='empty selected surface'):
        synthesis._validated(replace(request, environment=active))


def test_back_only_run_preserves_front_rows_and_full_double_face_keeps_first_surface():
    service, base = setup_problem(upper=.05)
    env = _double_environment()
    env['front_layers'] = [_row('fixed-front', thickness=111.)]
    front_bytes = deepcopy(env['front_layers'])
    request = replace(base, environment=env, selected_surfaces=('back',),
                      max_cycles=1, max_evaluations=20, max_layers=2)
    result = synthesize_film_design(service, request)
    assert result.candidate_design is not None
    assert result.candidate_design['front_layers'] == front_bytes
    assert result.candidate_design['first_surface'] == 'back'
    # Independently send the returned complete physical design through normal
    # TMM service: selection must not turn either coating into a virtual stack.
    frozen_design = service.freeze_design(result.candidate_design, dict(
        wavelength_min_nm=520., wavelength_max_nm=580., samples=5))
    direct = simulate_frozen('.', frozen_design)
    assert len(direct['R']) == 5


def test_joint_surface_seed_jacobian_has_both_real_stack_ids():
    service, base = setup_problem()
    env = _double_environment()
    env['front_layers'] = [_row('front-seed', 'low', 70.)]
    env['back_layers'] = [_row('back-seed', 'high', 90.)]
    seed = deepcopy(env)
    env['front_layers'] = []
    env['back_layers'] = []
    joint = replace(base, environment=env, seed_designs=(seed,), selected_surfaces=('front', 'back'))
    frozen = freeze_film_synthesis_request(service, joint)
    assert frozen.frozen_material_snapshot is not None
    value = synthesis.evaluate_frozen_structure_candidate(
        synthesis._proxy(frozen), seed,
        variable_ids=('front-seed', 'back-seed'), with_jacobian=True)
    assert {'front-seed', 'back-seed'} <= set(value['gradient_by_layer_id'])
    sites = [site for side in joint.selected_surfaces for site in _site_records(seed, frozen.frozen_material_snapshot, 1, side=side)]
    assert {site.get('side', 'front') for site in sites} == {'front', 'back'}


def test_continuation_fingerprint_binds_selected_surface_and_fixed_stack():
    service, base = setup_problem()
    front = freeze_film_synthesis_request(service, base)
    base_fingerprint = search_fingerprint(front)
    env = _double_environment()
    env['front_layers'] = [_row('fixed-front')]
    back_request = replace(base, environment=env, selected_surfaces=('back',),
                           max_cycles=1, max_evaluations=20)
    back = freeze_film_synthesis_request(service, back_request)
    fingerprint = search_fingerprint(back)
    changed = deepcopy(back.environment)
    changed['front_layers'][0]['thickness']['value'] += 1.
    changed_request = replace(back, environment=changed)
    assert fingerprint != base_fingerprint
    assert fingerprint != search_fingerprint(changed_request)


@pytest.mark.parametrize('first_surface', ['front', 'back'])
@pytest.mark.parametrize('substrate_nm', [700., 12000.])
@pytest.mark.parametrize('polarization', ['s', 'p', 'unpolarized'])
@pytest.mark.parametrize('selected', [('front',), ('back',), ('front', 'back')])
@pytest.mark.parametrize('empty_fixed', [False, True])
def test_two_face_analytic_columns_match_production_finite_differences(first_surface, substrate_nm, polarization, selected, empty_fixed):
    import numpy as np
    from coat_sim.core.film_synthesis_physics import simulate_synthesis_with_thickness_jacobian
    service, base = setup_problem(low=540., high=560., samples=3)
    # Absorbing substrate, transparent unequal outer ports, and a nonempty
    # opposite coating test actual geometry, flux and reversed illumination.
    service.materials.records['glass']['optical_sets'][0]['k'] = [.002] * 3
    env = _double_environment()
    env['substrate']['thickness'] = dict(value=substrate_nm, unit='nm')
    env['first_surface'] = first_surface
    env['exit_medium']['n'] = 1.15
    design = deepcopy(env)
    design['front_layers'] = [_row('f', 'low', 83.)]
    design['back_layers'] = [_row('b', 'high', 61.)]
    for side in ('front', 'back'):
        if side not in selected:
            if empty_fixed:
                design[side + '_layers'] = []
            env[side + '_layers'] = deepcopy(design[side + '_layers'])
    request = freeze_film_synthesis_request(service, replace(base, environment=env,
        selected_surfaces=selected, seed_designs=(design,)))
    variables = [dict(side=side, layer_id=design[side + '_layers'][0]['id'], lower_nm=0., upper_nm=1000.)
                 for side in selected]
    settings = dict(wavelength_min_nm=540., wavelength_max_nm=560., samples=3,
                    angle_deg=27., polarization=polarization, first_surface=first_surface)
    value = simulate_synthesis_with_thickness_jacobian(design, request.frozen_material_snapshot,
                                                      variables, settings)
    direct = simulate_frozen('.', service.freeze_design(design, settings))
    for metric in ('R', 'T'):
        assert np.array_equal(value[metric], direct[metric])
    for column, side in enumerate(selected):
        h = 1e-3
        minus, plus = deepcopy(design), deepcopy(design)
        minus[side + '_layers'][0]['thickness']['value'] -= h
        plus[side + '_layers'][0]['thickness']['value'] += h
        m = simulate_frozen('.', service.freeze_design(minus, settings))
        p = simulate_frozen('.', service.freeze_design(plus, settings))
        for metric in ('R', 'T'):
            fd = (np.asarray(p[metric]) - np.asarray(m[metric])) / (2 * h)
            assert np.allclose(np.asarray(value['jacobian_' + metric])[:, column], fd, rtol=3e-6, atol=2e-10)


@pytest.mark.parametrize('stage', ['interior', 'explorer', 'minimax', 'thickness', 'margin', 'hr', 'multispectral', 'feasibility'])
@pytest.mark.parametrize('selected', [('back',), ('front', 'back')])
def test_all_local_algorithm_stages_keep_real_side_variables_and_fixed_rows(stage, selected):
    from coat_sim.core.film_synthesis_surfaces import selected_rows
    from coat_sim.core.film_synthesis_refinement import (refine_synthesis_candidate,
        refine_synthesis_explorer, refine_synthesis_full_minimax)
    from coat_sim.core.film_synthesis_thickness import reduce_total_thickness
    from coat_sim.core.film_synthesis_margin import refine_worst_margin
    from coat_sim.core.film_synthesis_hr_minimax import refine_hr_minimax
    from coat_sim.core.film_synthesis_multispectral_minimax import refine_multispectral_minimax
    from coat_sim.core.film_synthesis_feasibility import PreparedFeasibilityEvaluator, refine_feasibility_candidate
    from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
    service, base = setup_problem(low=550., high=550., samples=1)
    objective = replace(base.objectives[0], pointwise=replace(base.objectives[0].pointwise,
        lower_bound=.001 if stage == "margin" else .8, upper_bound=None))
    env = _double_environment()
    env['substrate']['thickness'] = dict(value=1000., unit='nm')
    design = deepcopy(env)
    design['front_layers'], design['back_layers'] = [_row('f', 'low', 82.)], [_row('b', 'high', 62.)]
    for side in ('front', 'back'):
        if side not in selected:
            env[side + '_layers'] = deepcopy(design[side + '_layers'])
    request = freeze_film_synthesis_request(service, replace(base, environment=env,
        objectives=(objective,), selected_surfaces=selected, seed_designs=(design,)))
    design = deepcopy(request.seed_designs[0])
    calls = []
    def evaluate(candidate, **kwargs):
        assert candidate['first_surface'] == 'back'
        assert candidate['substrate'] == design['substrate']
        for side in ('front', 'back'):
            if side not in selected:
                assert candidate[side + '_layers'] == design[side + '_layers']
        ids = tuple(row['id'] for row in selected_rows(request, candidate))
        calls.append(ids)
        return synthesis.evaluate_frozen_structure_candidate(synthesis._proxy(request), candidate,
            variable_ids=ids, with_jacobian=True, zero_thickness_layer_ids=kwargs.get('zero_ids', ()))
    initial = evaluate(design)
    options: dict[str, Any] = dict(evaluate=evaluate, archive=lambda *args, **kwargs: None, max_iterations=1)
    if stage == 'interior':
        result, _ = refine_synthesis_candidate(request, initial, quarter_nm={('low', 'nk'):112., ('high', 'nk'):62.}, **options)
    elif stage == 'explorer':
        result, _ = refine_synthesis_explorer(request, initial, **options)
    elif stage == 'minimax':
        result, _ = refine_synthesis_full_minimax(request, initial, **options)
    elif stage == 'thickness':
        result, _ = reduce_total_thickness(request, initial, preserve_quality=True, **options)
    elif stage == 'margin':
        result, _ = refine_worst_margin(request, initial, **options)
    elif stage == 'hr':
        result, _ = refine_hr_minimax(request, initial, **options)
    elif stage == 'multispectral':
        result, _ = refine_multispectral_minimax(request, initial, **options)
    else:
        audit = audit_frozen_synthesis_candidate(request, design)
        prepared = PreparedFeasibilityEvaluator(request, audit)
        initial = prepared.evaluate(design)
        options['evaluate'] = lambda candidate, **kwargs: prepared.evaluate(candidate)
        result, _ = refine_feasibility_candidate(request, initial, **options)
    assert calls and all(ids == (('b',) if selected == ('back',) else ('f', 'b')) for ids in calls)
    for side in ('front', 'back'):
        if side not in selected:
            assert result['design'][side + '_layers'] == design[side + '_layers']


def test_selection_wire_default_normalization_and_legacy_checkpoint_identity():
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload, film_synthesis_request_from_payload
    service, base = setup_problem()
    frozen = freeze_film_synthesis_request(service, base)
    payload = film_synthesis_request_to_payload('legacy-selected-front', frozen)
    assert payload['request']['selected_surfaces'] == ['front']
    del payload['request']['selected_surfaces']
    _, legacy, _ = film_synthesis_request_from_payload(payload)
    assert legacy.selected_surfaces == ('front',)
    assert search_fingerprint(legacy) == search_fingerprint(frozen)
    env = _double_environment()
    forward = freeze_film_synthesis_request(service, replace(base, environment=env, selected_surfaces=['front', 'back']))
    reversed_request = freeze_film_synthesis_request(service, replace(base, environment=env, selected_surfaces=['back', 'front']))
    assert reversed_request.selected_surfaces == ('front', 'back')
    assert search_fingerprint(forward) == search_fingerprint(reversed_request)


@pytest.mark.parametrize('selected', [('front',), ('back',), ('front', 'back')])
def test_selected_job_review_and_audited_checkpoint_roundtrip(tmp_path, selected):
    from threading import Event
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
    from coat_sim.core.film_synthesis_jobs import (canonical_parameters, build_job_payload,
        execute_job, is_synthesis_result, validate_checkpoint, AUDITED_CHECKPOINT_KIND)
    service, base = setup_problem(upper=1., low=550., high=550., samples=1)
    env = _double_environment()
    seed = deepcopy(env)
    seed['front_layers'], seed['back_layers'] = [_row('f', 'low', 81.)], [_row('b', 'high', 63.)]
    for side in ('front', 'back'):
        if side not in selected:
            env[side + '_layers'] = deepcopy(seed[side + '_layers'])
    request = freeze_film_synthesis_request(service, replace(base, environment=env,
        selected_surfaces=selected, seed_designs=(seed,), warm_start_seed_index=0,
        max_evaluations=6, max_cycles=1))
    payload = film_synthesis_request_to_payload('selected-checkpoint', request)
    job = build_job_payload(canonical_parameters(None, {'payload': payload}))
    checkpoints = []
    result = execute_job({'payload': job}, Event(), lambda *args, **kwargs: None,
        tmp_path / 'best.json', lambda path, value: checkpoints.append(deepcopy(value)), str(tmp_path))
    assert is_synthesis_result(result)
    assert result['review']['selected_surfaces'] == list(selected)
    assert [row['side'] for row in result['review']['layers']] == list(selected)
    candidate = result['result']['candidate_design']
    for side in ('front', 'back'):
        if side not in selected:
            assert candidate[side + '_layers'] == request.environment[side + '_layers']
    checkpoint = next(item for item in checkpoints if item.get('kind') == AUDITED_CHECKPOINT_KIND)
    recovered = validate_checkpoint(checkpoint)
    assert is_synthesis_result(recovered)
    assert recovered['result']['candidate_design'] == checkpoint['design']


@pytest.mark.parametrize('selected', [('back',), ('front', 'back')])
def test_structural_template_priors_are_true_full_candidates_and_global_parameters_keep_sides(selected):
    from coat_sim.core.film_synthesis_matching import iter_matching_proposals
    from coat_sim.core.film_synthesis_ar_clusters import iter_ar_cluster_proposals, ar_cluster_parameters, ar_cluster_design
    from coat_sim.core.film_synthesis_global import iter_reflector_proposals, iter_bandpass_proposals, structured_thickness_design, symmetric_thickness_parameters, symmetric_thickness_design
    from coat_sim.core.film_synthesis_multispectral_priors import iter_multispectral_hr_proposals
    from coat_sim.core.film_synthesis_hr_priors import iter_dual_substack_hr_proposals
    from tests.test_film_synthesis_matching import matching_request
    from tests.test_film_synthesis_bandpass_proposals import bandpass_request
    from tests.test_film_synthesis_multispectral_priors import problem
    from tests.test_film_synthesis_hr_priors import hr_request
    _, ar = matching_request(max_layers=24)
    _, bandpass = bandpass_request(max_layers=24)
    for source, generator in [(ar, iter_matching_proposals), (ar, iter_ar_cluster_proposals),
            (bandpass, iter_bandpass_proposals), (hr_request(), iter_reflector_proposals),
            (problem(), iter_multispectral_hr_proposals), (hr_request(), iter_dual_substack_hr_proposals)]:
        env = deepcopy(source.environment)
        env['substrate']['sides'] = 'double'
        env['first_surface'] = 'back'
        if selected == ('back',):
            env['front_layers'] = [_row('fixed-front', 'high', 93.)]
        request = replace(source, environment=env, selected_surfaces=selected)
        proposal = next(generator(request))
        assert synthesis._feasible(request, proposal.design)
        assert proposal.design['first_surface'] == 'back'
        assert all(proposal.design[side + '_layers'] for side in selected)
        if selected == ('back',):
            assert proposal.design['front_layers'] == env['front_layers']
        if generator is iter_ar_cluster_proposals:
            point, _, _ = ar_cluster_parameters(proposal.design, proposal.provenance)
            mapped = ar_cluster_design(proposal.design, point, proposal.provenance)
        elif generator is iter_bandpass_proposals:
            groups = proposal.provenance['symmetry_groups']
            point, _, _ = symmetric_thickness_parameters(proposal.design, groups, selected_surfaces=selected)
            mapped = symmetric_thickness_design(proposal.design, point, symmetry_groups=groups, selected_surfaces=selected)
        else:
            mapped = structured_thickness_design(proposal.design, [0., .01, -.01], selected_surfaces=selected)
        assert mapped['first_surface'] == 'back'
        if selected == ('back',):
            assert mapped['front_layers'] == env['front_layers']


@pytest.mark.parametrize('strategy', ['legacy', 'hybrid'])
def test_joint_selection_one_layer_cap_still_scores_real_one_side_birth(strategy):
    service, base = setup_problem(upper=.000001, low=550., high=550., samples=1)
    env = _double_environment()
    env['substrate']['thickness'] = dict(value=1000., unit='nm')
    result = synthesize_film_design(service, replace(base, environment=env,
        selected_surfaces=('front', 'back'), max_layers=1, max_cycles=1,
        max_evaluations=20, search_strategy=strategy))
    assert result.state != 'failed', result.termination_reason
    assert any(item.layer_count == 1 for item in result.trace)
    assert result.candidate_design is not None
    assert sum(len(result.candidate_design[side + '_layers']) for side in ('front', 'back')) <= 1


def test_continuation_checks_fixed_row_identity_and_rejects_selection_change():
    from coat_sim.core.film_synthesis_continuation import continue_synthesis_request, validate_search_state
    service, base = setup_problem(upper=.000001, low=550., high=550., samples=1)
    env = _double_environment()
    env['front_layers'] = [_row('fixed-front', 'low', 80.)]
    result = synthesize_film_design(service, replace(base, environment=env, selected_surfaces=('back',),
        max_layers=2, max_cycles=1, max_evaluations=10, search_strategy='hybrid'))
    assert result.search_checkpoint is not None
    frozen = result.frozen_request
    validate_search_state(frozen, result.search_checkpoint)
    resumed = continue_synthesis_request(frozen, result.search_checkpoint, max_evaluations=12)
    assert resumed.selected_surfaces == ('back',)
    changed = deepcopy(frozen.environment)
    changed['front_layers'][0]['note'] = 'fixed-row-identity-changed'
    assert search_fingerprint(replace(frozen, environment=changed)) != search_fingerprint(frozen)
    with pytest.raises(ValueError, match='fingerprint'):
        validate_search_state(replace(frozen, selected_surfaces=('front', 'back')), result.search_checkpoint)
