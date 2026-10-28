"""Public synthetic boundary fixtures; no user project inputs or paths."""
from copy import deepcopy
from dataclasses import asdict, replace
from threading import Event

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_feasibility as repair
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint as Constraint, validate_objective_criteria
from coat_sim.core.film_optimization import FilmOptimizationObjective, OpticalMetric, Polarization
from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
from coat_sim.core.film_synthesis_audit import audit_frozen_synthesis_candidate
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_io import film_synthesis_result_to_payload
from coat_sim.core.film_synthesis_jobs import is_synthesis_result, replay_synthesis_result
from tests.test_film_auto_synthesis import setup_problem
from tests.test_film_synthesis_thickness import physical_fixture

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


@pytest.mark.parametrize('constraint', [
    Constraint(lower_bound=0., upper_bound=.005),
    Constraint(lower_bound=.99, upper_bound=1., strict_lower=True),
    Constraint(lower_bound=.2, upper_bound=.8, strict_lower=True, strict_upper=True),
    Constraint(mode='value', target_value=.5, lower_bound=None, upper_bound=None),
    Constraint(lower_bound=.5, upper_bound=.5),
    Constraint(lower_bound=.5, upper_bound=float(np.nextafter(.5, 1.))),
    Constraint(lower_bound=0., upper_bound=1., strict_lower=True, strict_upper=True),
    Constraint(lower_bound=0., upper_bound=0., strict_upper=True),
    Constraint(lower_bound=1., upper_bound=1., strict_lower=True),
])
def test_internal_margin_respects_physical_and_narrow_interiors(constraint):
    obj = FilmOptimizationObjective(500., 600., 3, pointwise=constraint)
    original = asdict(obj)
    guides, rows = repair.interior_guides((obj,))
    if ((constraint.strict_upper and constraint.upper_bound == 0.)
            or (constraint.strict_lower and constraint.lower_bound == 1.)):
        assert guides[0] == obj
        with pytest.raises(ValueError, match='empty intersection'):
            validate_objective_criteria(guides[0])
        return
    validate_objective_criteria(guides[0])
    assert asdict(obj) == original
    c = guides[0].pointwise
    if constraint.mode == 'value' or (constraint.lower_bound is not None and constraint.upper_bound is not None
            and constraint.upper_bound - constraint.lower_bound < 1e-15):
        assert c == constraint
    if constraint.lower_bound == 0. and not constraint.strict_lower:
        assert c.lower_bound == 0.
        if constraint.upper_bound == .005:
            assert rows[0]['upper_margin'] == pytest.approx(1e-6)
    if constraint.upper_bound == 1. and not constraint.strict_upper:
        assert c.upper_bound == 1.
    if constraint.lower_bound == 0. and constraint.upper_bound == 1. and constraint.strict_lower:
        assert c.lower_bound is not None and c.upper_bound is not None
        assert c.lower_bound > 0. and c.upper_bound < 1.
    assert c.strict_lower == constraint.strict_lower and c.strict_upper == constraint.strict_upper


def test_disabled_mean_never_enters_guide_and_coupled_ranges_remain_nonempty():
    obj = FilmOptimizationObjective(500., 600., 3, pointwise=Constraint(lower_bound=.4, upper_bound=.6),
        mean=Constraint(enabled=False, lower_bound=.999, upper_bound=.001))
    guides, rows = repair.interior_guides((obj,))
    assert len(rows) == 1 and rows[0]['reduction'] == 'pointwise'
    assert not guides[0].mean.enabled
    both = replace(obj, mean=Constraint(lower_bound=.45, upper_bound=.55))
    guides, rows = repair.interior_guides((both,))
    validate_objective_criteria(guides[0])
    assert all(r['common_width'] == pytest.approx(.1) for r in rows)


@pytest.mark.parametrize('family', ['AR', 'HR', 'mixed_RT'])
@pytest.mark.parametrize('mode', ['pointwise', 'mean', 'both'])
def test_joint_physical_evaluator_preserves_original_scores_across_families(family, mode):
    _, request, initial, _, _ = physical_fixture(family, mode)
    audited = audit_frozen_synthesis_candidate(request, initial['design'])
    evaluator = repair.PreparedFeasibilityEvaluator(request, audited)
    value = evaluator.restore(evaluator.evaluate(initial['design']))
    canonical = evaluate_frozen_structure_candidate(synthesis._proxy(request), initial['design'])
    assert value['score']['max_error'] == pytest.approx(canonical['score']['max_error'], abs=1e-14)
    assert value['score']['weighted_rms_error'] == pytest.approx(canonical['score']['weighted_rms_error'], abs=1e-14)
    assert value['score']['condition_count'] == canonical['score']['condition_count']
    assert value['score']['design'] == canonical['score']['design'] == initial['design']
    assert [r['wavelength_nm'] for r in value['responses']] == [r['wavelength_nm'] for r in canonical['responses']]
    assert not any(k.startswith('_feasibility_') for k in repair.canonical_candidate(value))


def near_case(*, strict=False, budget=100):
    service, request = setup_problem(materials=('low',), low=520., high=580., samples=5)
    seed = deepcopy(dict(request.environment))
    seed['front_layers'] = [dict(id='public-local-seed', material_id='low', optical_set_id='nk',
        thickness=dict(value=550. / (4 * np.sqrt(1.5)), unit='nm'), note='Synthetic boundary fixture')]
    request = replace(request, seed_designs=(seed,), warm_start_seed_index=0, search_strategy='hybrid',
        max_cycles=1, max_evaluations=budget, minimum_nonzero_thickness_nm=1.,
        max_layer_thickness_nm=200., cpu_workers=1, parallel_batch_size=1)
    request = synthesis.freeze_film_synthesis_request(service, request)
    value = evaluate_frozen_structure_candidate(synthesis._proxy(request), seed)
    maximum = max(e.actual for e in value['score']['errors'])
    obj = replace(request.objectives[0], pointwise=Constraint(lower_bound=0.,
        upper_bound=maximum if strict else maximum - 1e-12, strict_upper=strict))
    return service, replace(request, objectives=(obj,)), seed


def test_actual_offgrid_cavity_peak_becomes_constraint_without_rms_dilution():
    service, request = setup_problem(low=500., high=600., samples=3)
    for identifier, index in [('glass', 1.), ('low', 1.3), ('high', 2.2)]:
        service.materials.records[identifier]['optical_sets'][0]['n'] = [index] * 3
    objective = FilmOptimizationObjective(549., 551., 3, metric='T',
        pointwise=Constraint(lower_bound=None, upper_bound=.01))
    request = synthesis.freeze_film_synthesis_request(service, replace(request, objectives=(objective,)))
    center = 550.123456
    mirror = [('high', center / (4 * 2.2)), ('low', center / (4 * 1.3))] * 14
    recipe = mirror + [('low', center / (2 * 1.3))] + list(reversed(mirror))
    design = deepcopy(dict(request.environment))
    design['front_layers'] = [dict(id=f'public-cavity-{i}', material_id=m, optical_set_id='nk',
        thickness=dict(value=d, unit='nm'), note='Synthetic physical stress fixture') for i, (m, d) in enumerate(recipe)]
    audited = audit_frozen_synthesis_candidate(request, design)
    assert audited['score']['max_error'] > .9
    evaluator = repair.PreparedFeasibilityEvaluator(request, audited)
    joint = evaluator.evaluate(design)
    assert len(joint['responses'][0]['wavelength_nm']) > 3
    value = evaluator.restore(joint)
    assert value['score']['weighted_rms_error'] == 0.
    assert value['score']['max_error'] == 0.
    assert max(value['_feasibility_bounds']) > .9
    assert value['score']['condition_count'] == 3
    assert len(value['responses'][0]['wavelength_nm']) == 3


def test_alternating_audit_misses_accumulate_across_rounds_without_changing_original_measure():
    service, request, seed = near_case()
    audited = audit_frozen_synthesis_candidate(request, seed)
    template = audited['score']['errors'][0]
    first, second = deepcopy(audited), deepcopy(audited)
    # Two audit snapshots in which the current violating peak alternates. The
    # bookkeeping must retain both physical constraint locations thereafter.
    first['score']['errors'] = (replace(template, wavelength_nm=535.25, normalized_violation=1e-6),)
    second['score']['errors'] = (replace(template, wavelength_nm=564.75, normalized_violation=1e-6),)
    one = repair.PreparedFeasibilityEvaluator(request, first)
    two = repair.PreparedFeasibilityEvaluator(request, second, previous=one)
    three = repair.PreparedFeasibilityEvaluator(request, first, previous=two)
    assert two.points == three.points == {(0, 0.): {535.25, 564.75}}
    assert two.additional_points == three.additional_points == 2
    value = three.restore(three.evaluate(seed))
    original = evaluate_frozen_structure_candidate(synthesis._proxy(request), seed)
    assert value['score']['condition_count'] == original['score']['condition_count']
    assert value['score']['weighted_rms_error'] == pytest.approx(original['score']['weighted_rms_error'], abs=1e-15)
    assert len(value['_feasibility_bounds']) == 2 * (request.objectives[0].wavelength_samples + 2)


def test_mean_feedback_uses_whole_dense_integral_without_changing_original_measure():
    _, request, initial, _, _ = physical_fixture('AR', 'mean')
    audited = audit_frozen_synthesis_candidate(request, initial['design'])
    coarse = initial['score']['errors'][0].actual
    dense = audited['score']['errors'][0].actual
    assert coarse != dense
    limit = (coarse + dense) / 2
    constraint = Constraint(lower_bound=limit if dense < coarse else None,
                            upper_bound=limit if dense > coarse else None)
    request = replace(request, objectives=(replace(request.objectives[0], mean=constraint),))
    audited = audit_frozen_synthesis_candidate(request, initial['design'])
    evaluator = repair.PreparedFeasibilityEvaluator(request, audited)
    assert evaluator.mean_grids
    value = evaluator.restore(evaluator.evaluate(initial['design']))
    canonical = evaluate_frozen_structure_candidate(synthesis._proxy(request), initial['design'])
    assert value['score']['weighted_rms_error'] == pytest.approx(canonical['score']['weighted_rms_error'], abs=1e-15)
    assert len(value['score']['errors']) == len(canonical['score']['errors']) == 2
    assert len(value['_feasibility_bounds']) > 2
    later = deepcopy(audited)
    later['score']['errors'] = ()
    following = repair.PreparedFeasibilityEvaluator(request, later, previous=evaluator)
    assert following.mean_grids == evaluator.mean_grids


def test_multiband_bandpass_angles_weights_and_scales_keep_canonical_measure():
    _, request, initial, _, _ = physical_fixture('mixed_RT', 'pointwise')
    bands: list[tuple[float, float, int, OpticalMetric, Polarization, float, float]] = [
        (500., 520., 9, 'R', 'unpolarized', 1., .01),
        (540., 560., 7, 'T', 's', 3., .1),
        (600., 620., 5, 'R', 'p', 2., 1.)]
    objectives = tuple(FilmOptimizationObjective(low, high, count, metric=metric,
        angles_deg=(0., 15., 35.), polarization=pol, pointwise=Constraint(lower_bound=.99, upper_bound=None),
        weight=weight, normalization_scale=scale)
        for low, high, count, metric, pol, weight, scale in bands)
    request = replace(request, objectives=objectives)
    audited = audit_frozen_synthesis_candidate(request, initial['design'])
    evaluator = repair.PreparedFeasibilityEvaluator(request, audited)
    value = evaluator.restore(evaluator.evaluate(initial['design']))
    canonical = evaluate_frozen_structure_candidate(synthesis._proxy(request), initial['design'])
    assert value['score']['weighted_rms_error'] == pytest.approx(canonical['score']['weighted_rms_error'], rel=1e-13)
    assert value['score']['condition_count'] == canonical['score']['condition_count'] == 63
    assert {e.objective_index for e in value['score']['errors']} == {0, 1, 2}
    assert all(e.reduction == 'pointwise' for e in value['score']['errors'])


@pytest.mark.parametrize('strict', [False, True])
def test_real_parent_repairs_before_rotation_and_enters_existing_cost_stage(strict):
    service, request, seed = near_case(strict=strict)
    result = synthesis.synthesize_film_design(service, request)
    assert result.targets_met, result.termination_reason
    details = result.performance['search']['hybrid']
    phase = details['feasibility_repair']
    assert phase['trigger_evaluations'] == 2 and phase['trigger_cycle'] == 0
    assert phase['training_evaluations_used'] > 0 and phase['audit_physical_evaluations'] > 0
    assert phase['status'] == 'original_training_and_independent_audit_pass'
    assert result.training_score is not None and result.validation_score is not None
    assert result.candidate_design is not None
    assert result.training_score['nominal_pass'] and result.validation_score['nominal_pass']
    assert len(result.candidate_design['front_layers']) == 1
    assert result.candidate_design['front_layers'][0]['material_id'] == seed['front_layers'][0]['material_id']
    assert 'thickness_reduction' in details
    assert any(t.stage == 'thickness_reduction_complete' for t in result.trace)
    assert not any(t.stage == 'hybrid_global_trial' for t in result.trace)
    payload = film_synthesis_result_to_payload('public-repair-contract', result)
    assert is_synthesis_result(payload)
    replayed = replay_synthesis_result(payload)
    assert replayed['result']['targets_met'] and is_synthesis_result(replayed)


def test_fixed_infeasible_topology_finishes_local_session_without_false_target_success():
    service, request, seed = near_case()
    fixed = seed['front_layers'][0]['thickness']['value']
    request = replace(request, minimum_nonzero_thickness_nm=fixed, max_layer_thickness_nm=fixed)
    result = synthesis.synthesize_film_design(service, request)
    assert not result.targets_met and result.state == 'completed'
    assert result.termination_reason == 'local_session_converged_targets_unmet'
    assert result.candidate_design is not None
    assert result.candidate_design['front_layers'] == seed['front_layers']
    assert result.frozen_request.minimum_nonzero_thickness_nm == fixed
    assert result.frozen_request.max_layer_thickness_nm == fixed
    assert result.frozen_request.objectives == request.objectives
    assert not result.frozen_request.objectives[0].mean.enabled
    assert result.training_score is not None and result.validation_score is not None
    assert result.validation_complete
    identity = physical_design_hash(result.candidate_design)
    original = evaluate_frozen_structure_candidate(synthesis._proxy(result.frozen_request), result.candidate_design)
    independent = audit_frozen_synthesis_candidate(result.frozen_request, result.candidate_design)
    for actual, repeated in ((result.training_score, original['score']),
                             (result.validation_score, independent['score'])):
        assert physical_design_hash(actual['design']) == identity
        assert not actual['nominal_pass'] and not repeated['nominal_pass']
        assert actual['max_error'] > 0.
        assert actual['max_error'] == pytest.approx(repeated['max_error'], rel=1e-12, abs=1e-16)
    local = result.performance['search']['hybrid']['local_session']
    assert local['session_complete'] and local['local_converged']
    assert local['solver_terminal']['physical_hash'] == local['audited_selected']['physical_hash'] == identity
    terminal = local['attempts'][-1]
    assert terminal['termination'] == 'converged' and terminal['success']
    assert terminal['free_thickness_variables'] == 0
    assert terminal['fixed_layer_ids'] == [seed['front_layers'][0]['id']]
    assert terminal['solver_terminal']['physical_valid']
    assert terminal['solver_terminal']['local_stationary']
    assert terminal['terminal_full_grid_epigraph_feasible']
    assert result.local_optimum is not None and result.local_optimum['physical_hash'] == identity
    assert result.local_optimum['validation_complete'] and result.local_optimum['local_converged']
    assert any(t.stage == 'hybrid_rotation' for t in result.trace)
    assert result.performance['search']['hybrid']['feasibility_repair']['rounds']
    assert result.performance['search']['hybrid']['feasibility_repair_deduplicated_checks'] > 0
    completed = next(i for i, row in enumerate(result.trace) if row.stage == 'local_session_complete')
    assert not any(row.stage in {'hybrid_rotation', 'growth_restart'} for row in result.trace[completed + 1:])
    assert is_synthesis_result(film_synthesis_result_to_payload('fixed-unmet-local-session', result))


def test_cold_seed_discovery_repairs_after_screening_before_global_work():
    service, request, _ = near_case(budget=120)
    request = replace(request, warm_start_seed_index=None, max_layers=1)
    result = synthesis.synthesize_film_design(service, request)
    assert result.targets_met, result.termination_reason
    details = result.performance['search']['hybrid']
    assert details['feasibility_repair']['trigger_cycle'] == 1
    assert details['feasibility_repair']['trigger_boundary'] == 'engineering_prior_screen'
    assert details['feasibility_repair']['trigger_evaluations'] < request.max_evaluations / 2
    assert any(t.stage == 'hybrid_rotation' for t in result.trace)
    assert not any(t.stage == 'hybrid_global_trial' for t in result.trace)
    assert not any(s['stage'] == 'full_original_slsqp_minimax' for s in details['stages'])


def test_failed_fixed_high_index_topology_still_discovers_feasible_low_index_topology():
    service, request = setup_problem(low=550., high=550., samples=1)
    seed = deepcopy(dict(request.environment))
    seed['front_layers'] = [dict(id='public-infeasible-high', material_id='high', optical_set_id='nk',
        thickness=dict(value=550. / (2 * 2.2), unit='nm'), note='Synthetic high-index half-wave')]
    request = synthesis.freeze_film_synthesis_request(service, replace(request,
        seed_designs=(seed,), warm_start_seed_index=0, search_strategy='hybrid',
        max_evaluations=60, max_cycles=1, minimum_nonzero_thickness_nm=1.,
        cpu_workers=1, parallel_batch_size=1))
    value = evaluate_frozen_structure_candidate(synthesis._proxy(request), seed)
    obj = replace(request.objectives[0], pointwise=Constraint(lower_bound=0.,
        upper_bound=max(e.actual for e in value['score']['errors']) - 1e-12))
    result = synthesis.synthesize_film_design(service, replace(request, objectives=(obj,)))
    assert result.targets_met, result.termination_reason
    assert result.candidate_design is not None
    assert result.training_score is not None and result.validation_score is not None
    assert len(result.candidate_design['front_layers']) == 1
    assert result.candidate_design['front_layers'][0]['material_id'] == 'low'
    record = result.performance['search']['hybrid']['feasibility_repair_attempts'][0]
    assert record['trigger_cycle'] == 0 and record['status'] != 'original_training_and_independent_audit_pass'
    assert any(t.stage == 'hybrid_rotation' for t in result.trace)
    assert result.training_score['nominal_pass'] and result.validation_score['nominal_pass']


def test_loose_solver_tolerance_alone_does_not_trigger_final_repair():
    service, request, seed = near_case(budget=4)
    constraint = replace(request.objectives[0].pointwise,
                         upper_bound=request.objectives[0].pointwise.upper_bound / 2.)
    request = replace(request, objectives=(replace(request.objectives[0], pointwise=constraint),))
    result = synthesis.synthesize_film_design(service, request)
    assert not any(t.stage == 'feasibility_repair_start' for t in result.trace)
    assert any(t.stage == 'hybrid_rotation' for t in result.trace)


@pytest.mark.parametrize('at_stage', ['feasibility_repair_start', 'feasibility_repair_trial'])
def test_cancel_during_local_repair_does_not_admit_later_audit_or_cost_work(at_stage):
    service, request, seed = near_case()
    event, observed = Event(), []
    def observe(row):
        observed.append(row)
        if row.stage == at_stage:
            event.set()
    result = synthesis.synthesize_film_design(service, request, cancel_event=event, progress_callback=observe)
    assert result.state == 'cancelled' and not result.targets_met
    at = next(i for i, row in enumerate(observed) if row.stage == at_stage)
    assert not any(row.stage in ('validation', 'thickness_reduction_trial') for row in observed[at + 1:])
    entries = result.performance['search']['design_archive']['entries']
    complete = next(entry for entry in entries if entry['physical_hash'] == physical_design_hash(seed))
    assert complete['audit_status'] == 'complete'
    assert complete['audit']['physical_hash'] == physical_design_hash(complete['design']) == physical_design_hash(seed)
    assert result.candidate_design is not None
    final_matches = physical_design_hash(result.candidate_design) == complete['physical_hash']
    assert result.validation_complete is final_matches
    assert result.candidate_provenance['validation_matches_candidate'] is final_matches
    if final_matches:
        assert result.validation_score is not None
        assert physical_design_hash(result.validation_score['design']) == complete['physical_hash']
    else:
        assert result.validation_score is None
    payload = film_synthesis_result_to_payload('public-repair-cancel', result)
    assert is_synthesis_result(payload)
    assert payload['best_candidate_audit']['available'] is final_matches
    assert not any(t.stage == 'thickness_reduction_trial' for t in result.trace)


def test_extra_feedback_memory_guard_runs_before_local_optics(monkeypatch):
    service, request, seed = near_case(budget=4)
    request = replace(request, max_working_set_bytes=160 * 1024**2, resource_guard_resolution=None)
    original = repair.PreparedFeasibilityEvaluator
    def excessive(*args, **kwargs):
        evaluator = original(*args, **kwargs)
        evaluator.additional_points = 2_000_000
        return evaluator
    monkeypatch.setattr(repair, 'PreparedFeasibilityEvaluator', excessive)
    result = synthesis.synthesize_film_design(service, request)
    assert result.state != 'failed' and not result.targets_met
    assert result.candidate_design is not None
    assert result.candidate_design['front_layers'] == seed['front_layers']
    record = result.performance['search']['hybrid']['feasibility_repair']
    assert record['status'] == 'resource_guard' and record['training_evaluations_used'] == 0


def test_shared_budget_stops_local_repair_and_keeps_original_candidate(monkeypatch):
    service, request, seed = near_case(budget=8)
    def consume(request, initial, *, evaluate, **kwargs):
        while True:
            evaluate(initial['design'])
    monkeypatch.setattr(repair, 'refine_feasibility_candidate', consume)
    result = synthesis.synthesize_film_design(service, request)
    assert not result.targets_met and result.evaluations <= 8 and result.state != 'failed'
    assert result.candidate_design is not None
    assert result.candidate_design['front_layers'] == seed['front_layers']
    assert any(t.stage == 'hybrid_rotation' for t in result.trace)


def test_missing_analytic_gradient_retains_audited_candidate_without_false_pass(monkeypatch):
    service, request, seed = near_case(budget=4)
    original = repair.simulate_with_thickness_jacobian
    def unavailable(*args, **kwargs):
        value = original(*args, **kwargs)
        value['jacobian_R'] = np.full(np.asarray(value['jacobian_R']).shape, np.nan).tolist()
        return value
    monkeypatch.setattr(repair, 'simulate_with_thickness_jacobian', unavailable)
    result = synthesis.synthesize_film_design(service, request)
    assert not result.targets_met and result.state == 'not_found'
    assert result.candidate_design is not None
    assert result.performance['search']['hybrid']['feasibility_repair']['status'] == 'feasibility_repair_unavailable'
    assert result.candidate_design['front_layers'] == seed['front_layers']
    assert result.validation_complete
