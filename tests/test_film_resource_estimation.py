"""Resource assessment: actual TMM witnesses and counterexamples, not predictors."""
from copy import deepcopy
from dataclasses import asdict, replace
from threading import Event

import numpy as np
import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint as C
from coat_sim.core.film_optimization import FilmOptimizationObjective as O
from coat_sim.core.film_resource_estimation import (
    FilmResourceEstimateRequest, estimate_film_resources,
    freeze_resource_estimate_request, resource_estimate_source_digest, observed_resource_pareto,
)
from coat_sim.core.film_resource_estimation_bounds import (
    hr_point_layer_screen, hr_thickness_screen, normalize_hr_windows,
)
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def Request(*args, **kwargs):
    if "probe_max_evaluations" in kwargs:
        raise TypeError("probe_max_evaluations supplied twice")
    kwargs["probe_max_evaluations"] = 0
    kwargs.setdefault("audit_candidates", True)
    return FilmResourceEstimateRequest(*args, **kwargs)



def hr_request(service, request, *, lower=.999, mean=False, samples=3):
    service.materials.records['low']['optical_sets'][0]['n'] = [1.45] * 3
    service.materials.records['high']['optical_sets'][0]['n'] = [2.30] * 3
    service.materials.records['glass']['optical_sets'][0]['n'] = [1.] * 3
    return replace(request, objectives=(O(500., 600., samples,
        pointwise=C(enabled=not mean, lower_bound=lower, upper_bound=None),
        mean=C(enabled=mean, lower_bound=lower, upper_bound=None)),))


def double(request, *, first='front', thick=1.):
    env = deepcopy(request.environment)
    env['substrate'].update(sides='double', thickness=dict(value=thick, unit='mm'))
    env['first_surface'] = first
    return replace(request, environment=env)


def row(identifier, material='low', d=80):
    return dict(id=identifier, material_id=material, optical_set_id='nk', thickness=dict(value=d, unit='nm'), note='')


def test_current_zero_layer_structure_is_actual_audited_witness_and_no_mutation():
    service, request = setup_problem(upper=.05)
    before = deepcopy(asdict(request))
    result = estimate_film_resources(service, Request(request, (request.environment,)))
    assert result.state == 'completed' and result.feasibility_state == 'sampled_witness_found'
    assert result.recommended_budget is not None
    assert result.recommended_budget[0]['layer_count'] == 0
    assert result.recommended_budget[0]['total_thickness_nm'] == 0
    assert result.work['physical_evaluations'] == 2 and result.work['wavelength_points'] == 33
    assert result.work['source_freeze_passes'] == 1
    assert result.work['audits_completed'] == 1
    assert not result.continuous_certificate and result.estimate_state == 'not_calibrated'
    assert asdict(request) == before


def test_point_bounds_ideal_and_layer_window_is_not_sufficient_budget():
    service, base = setup_problem()
    request = hr_request(service, base)
    result = estimate_film_resources(service, Request(request, audit_candidates=False, layer_search_limit=8))
    assert result.state == 'completed'
    thickness, layers = result.bounds
    assert thickness['value_nm'] > 0 and thickness['claim_level'] == 'model_numerical_screening'
    assert layers['value_layers'] is None and layers['excluded_through'] == 8
    assert layers['details']['exceeds_search_window']
    assert result.recommended_budget is None and result.feasibility_state == 'unknown'
    assert request.max_layers is None and result.work['lp_solves'] == 1
    assert result.work['dp_conditions'] == 3


def test_mean_hr_is_not_a_pointwise_threshold():
    service, base = setup_problem()
    request = hr_request(service, base, mean=True)
    result = estimate_film_resources(service, Request(request, audit_candidates=False))
    assert all(b['applicability'] == 'not_applicable' for b in result.bounds)
    assert result.work['lp_solves'] == result.work['dp_conditions'] == 0


def test_dispersion_and_absorption_gates_do_not_delete_materials():
    service, base = setup_problem()
    request = hr_request(service, base)
    service.materials.records['high']['optical_sets'][0]['n'] = [2.2, 2.3, 2.4]
    result = estimate_film_resources(service, Request(request, audit_candidates=False, layer_search_limit=5))
    assert result.bounds[0]['applicability'] == 'not_applicable'
    assert result.bounds[1]['applicability'] == 'applicable'
    service.materials.records['high']['optical_sets'][0]['k'] = [.01] * 3
    result = estimate_film_resources(service, Request(request, audit_candidates=False))
    assert all(b['applicability'] == 'not_applicable' for b in result.bounds)
    assert result.feasibility_state == 'unknown'


def test_strict_unknown_k_and_explicit_conditional_assumption():
    service, base = setup_problem()
    request = hr_request(service, base)
    service.materials.records['high']['optical_sets'][0]['k'] = None
    strict = estimate_film_resources(service, Request(request, audit_candidates=False))
    assert strict.state == 'failed' and strict.input_status == 'invalid'
    env = deepcopy(request.environment)
    env['assume_lossless'] = True
    conditional = estimate_film_resources(service, Request(replace(request, environment=env), audit_candidates=False))
    assert conditional.state == 'completed'
    assert all(b['conditional_on_assumption'] for b in conditional.bounds)


def test_single_spectral_line_skips_thickness_but_keeps_dp():
    service, base = setup_problem()
    request = hr_request(service, base)
    request = replace(request, objectives=(replace(request.objectives[0], wavelength_min_nm=550, wavelength_max_nm=550, wavelength_samples=1),))
    result = estimate_film_resources(service, Request(request, audit_candidates=False))
    assert result.bounds[0]['applicability'] == 'not_applicable'
    assert result.bounds[1]['applicability'] == 'applicable'


@pytest.mark.parametrize('first,selected', [('front', ('front',)), ('back', ('back',)), ('front', ('back',)), ('back', ('front',))])
def test_fixed_uncoated_opposite_face_limits_incoherent_whole_transmission(first, selected):
    service, base = setup_problem()
    request = double(base, first=first)
    request = replace(request, selected_surfaces=selected, objectives=(O(550, 550, 1, metric='T', pointwise=C(lower_bound=.98, upper_bound=None)),))
    result = estimate_film_resources(service, Request(request, (request.environment,)))
    assert result.state == 'completed'
    screen = result.screenings[0]
    assert screen['state'] == 'conflict'
    assert screen['details']['conflicts'][0]['transmission_upper'] == pytest.approx(.96)
    assert result.recommended_budget is None
    assert result.candidate_reports[0]['targets_met'] is False
    assert result.work['physical_evaluations'] == 3  # two pol fixed face + one audit


def test_coherent_substrate_counterexample_must_not_use_fixed_face_gate():
    service, base = setup_problem()
    request = double(base, thick=.001)
    request = replace(request, objectives=(O(500, 500, 1, metric='T', pointwise=C(lower_bound=.98, upper_bound=None)),))
    result = estimate_film_resources(service, Request(request, (request.environment,)))
    assert result.screenings[0]['state'] == 'not_applicable'
    assert result.candidate_reports[0]['targets_met']
    assert result.recommended_budget is not None
    assert result.recommended_budget[0]['layer_count'] == 0


def test_fixed_face_gate_not_for_mean_or_absorbing_substrate_or_both_free():
    service, base = setup_problem()
    request = double(base)
    mean = O(520, 580, 3, metric='T', pointwise=C(enabled=False), mean=C(lower_bound=.98, upper_bound=None))
    assert not estimate_film_resources(service, Request(replace(request, objectives=(mean,)), audit_candidates=False)).screenings
    target = replace(mean, pointwise=C(lower_bound=.98, upper_bound=None), mean=C(enabled=False))
    service.materials.records['glass']['optical_sets'][0]['k'] = [.001] * 3
    result = estimate_film_resources(service, Request(replace(request, objectives=(target,)), audit_candidates=False))
    assert result.screenings[0]['state'] == 'not_applicable'
    both = replace(request, selected_surfaces=('front', 'back'), objectives=(target,))
    assert not estimate_film_resources(service, Request(both, audit_candidates=False)).screenings


def test_nominal_strict_and_solver_tolerance_are_not_interchangeable():
    service, base = setup_problem()
    request = replace(base, objectives=(O(550, 550, 1, pointwise=C(lower_bound=.04001, upper_bound=None)),), acceptable_normalized_error=.001)
    result = estimate_film_resources(service, Request(request, (request.environment,)))
    report = result.candidate_reports[0]
    assert report['solver_acceptable'] and not report['targets_met']
    assert result.recommended_budget is None


def test_full_double_sided_selected_cost_and_explicit_caps_not_changed():
    service, base = setup_problem(upper=1.)
    request = double(base)
    candidate = deepcopy(request.environment)
    candidate['front_layers'] = [row('f', d=30)]
    candidate['back_layers'] = [row('b', d=50)]
    request = replace(request, selected_surfaces=('front', 'back'), max_layers=1)
    before = deepcopy(request.environment)
    result = estimate_film_resources(service, Request(request, (candidate,)))
    report = result.candidate_reports[0]
    assert report['layer_count'] == 2 and report['total_thickness_nm'] == 80
    assert report['targets_met'] and report['within_constraints'] and not report['within_search_budgets']
    assert result.recommended_budget and request.max_layers == 1
    assert result.search_budget_reference['max_layers'] == 1
    assert request.environment == before


def test_frozen_source_identity_and_live_change_detection():
    service, base = setup_problem(upper=.05)
    original = Request(base, (base.environment,))
    frozen = freeze_resource_estimate_request(service, original)
    digest = resource_estimate_source_digest(frozen)
    service.materials.records['glass']['optical_sets'][0]['n'] = [1.6] * 3
    worker = estimate_film_resources(service, frozen)
    assert worker.source_digest == digest and worker.candidate_reports[0]['targets_met']
    updated = freeze_resource_estimate_request(service, original)
    assert resource_estimate_source_digest(updated) != digest
    assert resource_estimate_source_digest(replace(frozen, layer_search_limit=3)) == digest


def test_cancel_and_deadline_keep_prior_complete_witness_and_completed_work():
    service, base = setup_problem(upper=.05)
    event = Event()
    def progress(p):
        if p.stage == 'candidate_complete':
            event.set()
    result = estimate_film_resources(service, Request(base, (base.environment, base.environment)),
                                    cancel_event=event, progress_callback=progress)
    assert result.state == 'cancelled' and len(result.pareto_witnesses) == 1
    assert result.work['audits_completed'] == 1 and result.work['physical_evaluations'] == 2
    deadline = estimate_film_resources(service, Request(base, max_seconds=1e-30))
    assert deadline.state == 'deadline' and deadline.work['physical_evaluations'] == 0


def test_cancel_during_completed_physical_call_is_counted_but_not_a_witness():
    service, base = setup_problem(upper=.05)
    event = Event()
    def progress(p):
        if p.physical_evaluations:
            event.set()
    result = estimate_film_resources(service, Request(base, (base.environment,)), cancel_event=event, progress_callback=progress)
    assert result.state == 'cancelled'
    assert result.work['physical_evaluations'] == 1
    assert result.work['audits_started'] == 1 and result.work['audits_completed'] == 0
    assert not result.pareto_witnesses


def test_target_conflicts_require_same_conditions_and_reduction():
    service, base = setup_problem()
    a = O(520, 580, 3, pointwise=C(lower_bound=.7, upper_bound=None))
    b = replace(a, metric='T')
    result = estimate_film_resources(service, Request(replace(base, objectives=(a, b)), audit_candidates=False))
    assert result.screenings[0]['state'] == 'conflict'
    b = replace(b, angles_deg=(45.,))
    assert not estimate_film_resources(service, Request(replace(base, objectives=(a, b)), audit_candidates=False)).screenings


def test_pareto_does_not_invent_combined_minimum_corner():
    def report(n, d, h):
        return dict(state='audited', targets_met=True, within_constraints=True,
                    layer_count=n, total_thickness_nm=d, physical_hash=h)
    points = observed_resource_pareto([report(5, 100, 'a'), report(7, 80, 'b'), report(8, 120, 'c')])
    assert [(p['layer_count'], p['total_thickness_nm']) for p in points] == [(5, 100), (7, 80)]


def test_independent_17_layer_center_success_is_not_a_broadband_budget():
    from coat_sim.core.film_optics import _coherent_many
    n = [2.30 if i % 2 == 0 else 1.45 for i in range(17)]
    ds = np.asarray([550 / (4 * v) for v in n])
    waves = np.linspace(400, 700, 3001)
    raw = _coherent_many(waves, np.tile([1., *n, 1.], (len(waves), 1)), ds, np.zeros(len(waves)), 's')
    assert raw[1500, 0] == pytest.approx(.9995292994027226)
    assert raw[:, 0].min() < 2e-6
    lb = hr_thickness_screen([1.45, 2.30], [(400, 700, .999)], 1., 1.)
    assert lb['value_nm'] == pytest.approx(1783.328603, rel=1e-8)
    assert ds.sum() < lb['value_nm']
    assert hr_point_layer_screen([1.45, 2.30], 1., 1., 0., 's', .999, 20)['value_layers'] == 17


def test_lp_union_scaling_dp_brewster_and_critical_gates():
    assert normalize_hr_windows([(400, 700, .99), (400, 700, .99), (500, 600, .98)]) == [(400, 500, .99), (500, 600, .99), (600, 700, .99)]
    a = hr_thickness_screen([1.45, 2.3], [(400, 700, .99)], 1., 1.)
    b = hr_thickness_screen([1.45, 2.3], [(4000, 7000, .99)], 1., 1.)
    assert b['value_nm'] == pytest.approx(a['value_nm'] * 10)
    assert max(a['dual_material_usage']) <= 1
    bare = hr_point_layer_screen([1.5], 1., 1.5, np.degrees(np.arctan(1.5)), 'p', .1, 0)
    assert bare['ceiling_at_search_limit'] < 1e-20
    with pytest.raises(ValueError, match='critical'):
        hr_point_layer_screen([1.], 1.5, 1.5, 60., 's', .99, 20)


@pytest.mark.parametrize('changes', [dict(layer_search_limit=True), dict(layer_search_limit=-1), dict(max_seconds=True), dict(max_seconds=float('nan'))])
def test_invalid_controls_never_return_plausible_budget(changes):
    service, base = setup_problem()
    result = estimate_film_resources(service, Request(base, **changes))
    assert result.state == 'failed' and result.input_status == 'invalid'
    assert result.recommended_budget is None


def test_observation_only_material_does_not_enlarge_pool_or_become_task_witness():
    service, base = setup_problem(upper=1., materials=('low',))
    candidate = deepcopy(base.environment)
    candidate['front_layers'] = [row('foreign', 'high')]
    result = estimate_film_resources(service, Request(base, (candidate,)))
    assert result.candidate_reports[0]['targets_met']
    assert not result.candidate_reports[0]['within_material_pool']
    assert not result.candidate_reports[0]['within_constraints']
    assert result.recommended_budget is None
    # An explicit product seed is an authorized retained material, unlike observation-only input.
    seeded = replace(base, seed_designs=(candidate,))
    result = estimate_film_resources(service, Request(seeded, (candidate,)))
    assert result.candidate_reports[0]['within_material_pool'] and result.recommended_budget


def test_frozen_snapshot_never_supplements_missing_audit_source_from_live_catalog():
    service, base = setup_problem(upper=1., materials=('low',))
    frozen = freeze_resource_estimate_request(service, Request(base))
    candidate = deepcopy(base.environment)
    candidate['front_layers'] = [row('foreign', 'high')]
    result = estimate_film_resources(service, replace(frozen, candidate_designs=(candidate,)))
    assert result.state == 'failed'
    assert 'explicitly refreeze' in result.message


def test_dp_transition_work_survives_cancellation_inside_condition():
    recorded: list[int] = []
    def stop():
        if recorded:
            raise RuntimeError('stop')
    with pytest.raises(RuntimeError, match='stop'):
        hr_point_layer_screen([1.45, 2.3], 1., 1., 0., 's', .99, 30,
                              check=stop, record_transitions=recorded.append)
    assert recorded == [4]


def test_process_limits_are_hard_for_witnesses_but_search_hats_are_only_comparison():
    service, base = setup_problem(upper=1.)
    candidate = deepcopy(base.environment)
    candidate['front_layers'] = [row('thin', d=30)]
    request = replace(base, minimum_nonzero_thickness_nm=40., max_layer_thickness_nm=100., max_layers=1, max_total_thickness_nm=20.)
    result = estimate_film_resources(service, Request(request, (candidate,)))
    report = result.candidate_reports[0]
    assert report['targets_met'] and not report['within_constraints'] and not report['within_search_budgets']
    assert result.recommended_budget is None
    request = replace(request, minimum_nonzero_thickness_nm=None)
    result = estimate_film_resources(service, Request(request, (candidate,)))
    assert result.recommended_budget and not result.candidate_reports[0]['within_search_budgets']
    assert request.max_total_thickness_nm == 20.


def test_inactive_back_draft_never_demands_missing_optical_source():
    service, base = setup_problem(upper=.05)
    env = deepcopy(base.environment)
    env['back_layers'] = [dict(id='inactive', material_id='unavailable', optical_set_id='absent', thickness=dict(value=1., unit='qw'), note='')]
    request = replace(base, environment=env)
    result = estimate_film_resources(service, Request(request, (env,)))
    assert result.state == 'completed' and result.recommended_budget
    assert result.candidate_reports[0]['surfaces']['back']['active'] is False


def test_conditional_current_witness_labels_unknown_k_and_keeps_known_k():
    service, base = setup_problem(upper=1.)
    env = deepcopy(base.environment)
    env['assume_lossless'] = True
    env['front_layers'] = []
    service.materials.records['glass']['optical_sets'][0]['k'] = None
    request = replace(base, environment=env)
    result = estimate_film_resources(service, Request(request, (env,)))
    report = result.candidate_reports[0]
    assert report['conditional_on_assumption']
    assert report['absorption_assumption']['known_k_preserved']
    assert report['audit']['continuous_certificate'] is False


def test_absorptive_external_port_rejected_before_energy_balance_screen():
    service, base = setup_problem()
    env = deepcopy(base.environment)
    env['incident_medium'] = dict(kind='material', material_id='high', optical_set_id='nk')
    service.materials.records['high']['optical_sets'][0]['k'] = [.1] * 3
    result = estimate_film_resources(service, Request(replace(base, environment=env)))
    assert result.state == 'failed' and result.input_status == 'invalid'
    assert not result.screenings and result.recommended_budget is None


def test_started_freeze_failure_is_not_a_completed_validation_pass():
    service, request = setup_problem()
    service.materials.records['high']['optical_sets'][0]['k'] = None
    result = estimate_film_resources(service, Request(request))
    assert result.state == 'failed'
    assert result.work['source_freeze_attempts'] == 1
    assert result.work['source_freeze_passes'] == 0


def test_compressed_recommendation_carries_source_and_conditional_scope():
    service, request = setup_problem(upper=1.)
    env = deepcopy(request.environment)
    env['assume_lossless'] = True
    service.materials.records['glass']['optical_sets'][0]['k'] = None
    result = estimate_film_resources(service, Request(replace(request, environment=env), (env,)))
    assert result.recommended_budget is not None
    node = result.recommended_budget[0]
    assert node['conditional_on_assumption'] and node['absorption_assumption']['conditional_on_assumption']
    assert node['source_digest'] == result.source_digest
    assert node['claim_level'] == 'sampled_witness' and node['continuous_certificate'] is False
    assert node['scope'] == 'selected_coating_surfaces_aggregate'


def test_small_positive_indices_do_not_turn_float_underflow_into_infinite_budget():
    result = hr_thickness_screen([1e-200, 2e-200], [(500, 600, .99)], 1e-200, 1e-200)
    assert result['no_finite_thickness_in_ideal_model'] is False
    assert np.isfinite(result['value_nm']) and result['value_nm'] > 0
    # Scaling every n and external n by s at fixed lambda scales physical d by 1/s.
    ordinary = hr_thickness_screen([1., 2.], [(500, 600, .99)], 1., 1.)
    assert result['value_nm'] == pytest.approx(ordinary['value_nm'] * 1e200, rel=1e-8)
    with pytest.raises(ValueError, match='numerical_unknown'):
        hr_thickness_screen([1e-320, 1.0005e-320], [(500, 600, .99)], 1e-320, 1e-320)


def test_structurally_zero_budget_only_reports_screen_not_float_infinite_claim():
    with pytest.raises(ValueError, match='numerical_unknown_zero_budget_comparison'):
        hr_thickness_screen([1.], [(500, 600, .99)], 1., 1.)
    perfect = hr_thickness_screen([1., 2.], [(500, 600, 1.)], 1., 1.)
    assert perfect['no_finite_thickness_in_ideal_model'] is True


def test_attainable_single_layer_peak_roundoff_is_not_an_infinite_thickness_claim():
    n = 2.0464452863279563
    target = .37759845706659817
    with pytest.raises(ValueError, match='numerical_unknown_zero_budget_comparison'):
        hr_thickness_screen([n], [(550., float(np.nextafter(550., np.inf)), target)], 1., 1.)
    service, base = setup_problem(materials=('high',))
    service.materials.records['glass']['optical_sets'][0]['n'] = [1.] * 3
    service.materials.records['high']['optical_sets'][0]['n'] = [n] * 3
    objective = O(550., float(np.nextafter(550., np.inf)), 2, pointwise=C(lower_bound=target, upper_bound=None))
    result = estimate_film_resources(service, Request(replace(base, objectives=(objective,)), audit_candidates=False))
    assert result.state == 'completed'
    assert result.bounds[0]['applicability'] == 'not_applicable'
    assert result.bounds[0]['reason'] == 'numerical_unknown_zero_budget_comparison'
    assert result.feasibility_state == 'unknown'
