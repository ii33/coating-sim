from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from threading import Event
from typing import Any

import numpy as np
import pytest

from coat_sim.core import film_sensitivity as sensitivity
from coat_sim.core.film_design_service import simulate_frozen
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable
from coat_sim.core.film_sensitivity import (
    FilmSensitivityRequest, FilmSensitivityScenario, FilmThicknessError,
    analyze_film_sensitivity, optimize_film_sensitivity,
    freeze_film_sensitivity_request, film_sensitivity_request_from_payload,
    film_sensitivity_request_to_payload, film_sensitivity_result_from_payload,
    film_sensitivity_result_to_payload, validate_film_sensitivity_result_payload,
    sensitivity_request_from_optimization,
)
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def problem(*, two=False, objective='linear_sensitivity_norm', error_model='independent', operation='analyze'):
    service, synthesis = setup_problem(low=550., high=550., samples=1, upper=0.)
    design = deepcopy(dict(synthesis.environment))
    design['front_layers'] = [dict(id='low-film', material_id='low', optical_set_id='nk',
        thickness=dict(value=80., unit='nm'), note='synthetic sensitivity fixture')]
    if two:
        design['front_layers'].append(dict(id='high-film', material_id='high', optical_set_id='nk',
            thickness=dict(value=40., unit='nm'), note='second fixture layer'))
    errors = tuple(FilmThicknessError(layer['id'], 1.) for layer in design['front_layers'])
    variables = tuple(FilmThicknessVariable('front', layer['id'], 60. if i == 0 else 20., 145. if i == 0 else 80.)
                      for i, layer in enumerate(design['front_layers']))
    scenarios = (FilmSensitivityScenario('minus', tuple(-1. for _ in errors)),
                 FilmSensitivityScenario('plus', tuple(1. for _ in errors)))
    request = FilmSensitivityRequest(design, 7, synthesis.objectives, errors, error_model, objective,
        variables=variables, scenarios=scenarios, operation=operation, acceptable_normalized_error=1e-8)
    return service, request


@pytest.mark.parametrize("metric,polarization",[("R","s"),("R","p"),("R","unpolarized"),
    ("T","s"),("T","p"),("T","unpolarized")])
def test_analytic_derivatives_match_central_finite_difference_and_canonical_forward(metric,polarization):
    service, request = problem(two=True)
    request = replace(request, objectives=(FilmOptimizationObjective(500., 600., 5,
        (0., 42.), polarization, metric, 'range', upper_bound=.1),))
    result = analyze_film_sensitivity(service, request)
    assert result.state == 'completed', result.message
    assert result.best_analysis is not None
    baseline = result.best_analysis
    frozen = result.frozen_request.frozen_material_snapshot
    assert frozen is not None
    # Canonical service is an independent physical forward path.
    for angle in (0., 42.):
        snapshot = deepcopy(dict(frozen))
        snapshot['settings'].update(wavelength_min_nm=500., wavelength_max_nm=600.,
            samples=5, angle_deg=angle, polarization=polarization)
        snapshot.pop('sha256')
        import hashlib
        snapshot['sha256'] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        values = simulate_frozen('.', snapshot)[metric]
        actual = [r.nominal_value for r in baseline.linear_conditions if r.angle_deg == angle]
        np.testing.assert_allclose(actual, values, atol=2e-14)
    for column, error in enumerate(request.errors):
        h = 1e-3
        samples = []
        for sign in (-1., 1.):
            changed = deepcopy(request.design_snapshot)
            layer = next(row for row in changed['front_layers'] if row['id'] == error.layer_id)
            layer['thickness']['value'] += sign*h
            perturbed = analyze_film_sensitivity(service, replace(request, design_snapshot=changed))
            assert perturbed.best_analysis is not None
            samples.append(np.array([row.nominal_value for row in perturbed.best_analysis.linear_conditions]))
        analytic = np.array([row.derivative_per_nm[column] for row in baseline.linear_conditions])
        np.testing.assert_allclose(analytic, (samples[1]-samples[0])/(2*h), rtol=2e-8, atol=2e-11)


def test_independent_and_common_mode_have_distinct_analytic_cross_terms():
    service, request = problem(two=True)
    independent = analyze_film_sensitivity(service, request).best_analysis
    common = analyze_film_sensitivity(service, replace(request, error_model='common_mode')).best_analysis
    assert independent is not None and common is not None
    derivatives = np.asarray(independent.linear_conditions[0].derivative_per_nm)
    assert independent.linear_sensitivity_norm == pytest.approx(np.linalg.norm(derivatives))
    assert common.linear_sensitivity_norm == pytest.approx(abs(derivatives.sum()))
    assert abs(common.linear_sensitivity_norm-independent.linear_sensitivity_norm) > 1e-5
    assert independent.finite_scenarios == common.finite_scenarios


def test_relative_error_scales_with_candidate_physical_thickness_and_qw_conversion():
    service, request = problem()
    absolute = analyze_film_sensitivity(service, request)
    relative = analyze_film_sensitivity(service, replace(request,
        errors=(FilmThicknessError('low-film', 1/80., 'relative_fraction'),)))
    assert absolute.best_analysis is not None and relative.best_analysis is not None
    assert relative.best_analysis.linear_sensitivity_norm == pytest.approx(absolute.best_analysis.linear_sensitivity_norm)
    assert relative.best_analysis.linear_conditions[0].error_amplitudes_nm == (1.,)
    qw = deepcopy(request.design_snapshot)
    qw['front_layers'][0]['thickness'] = dict(value=80.*4*np.sqrt(1.5)/550., unit='qw')
    converted = analyze_film_sensitivity(service, replace(request, design_snapshot=qw))
    assert converted.best_analysis is not None
    assert converted.best_thickness_nm['low-film'] == pytest.approx(80.)
    assert converted.best_analysis.linear_sensitivity_norm == pytest.approx(absolute.best_analysis.linear_sensitivity_norm)


def test_linear_estimates_are_separate_from_exact_large_finite_perturbations():
    service, request = problem()
    request = replace(request, errors=(FilmThicknessError('low-film', 30.),))
    result = analyze_film_sensitivity(service, request)
    assert result.best_analysis is not None
    scenario = result.best_analysis.finite_scenarios[1]
    row = result.best_analysis.linear_conditions[0]
    assert scenario.linear_estimated_values[0] == pytest.approx(row.nominal_value+30*row.derivative_per_nm[0])
    assert abs(scenario.linear_estimated_values[0]-scenario.finite_perturbation_values[0]) > .001
    assert result.best_analysis.nominal_targets_met is False


@pytest.mark.parametrize('objective', ['linear_sensitivity_norm', 'finite_scenario_worst', 'finite_scenario_expected'])
def test_real_robustness_optimization_improves_selected_objective_not_nominal_button(objective):
    service, request = problem(objective=objective, operation='optimize')
    result = optimize_film_sensitivity(service, request)
    assert result.state == 'completed', result.message
    assert result.improved
    assert result.best_analysis is not None and result.baseline_analysis is not None
    assert result.best_analysis.robustness_value < result.baseline_analysis.robustness_value / 100.
    assert 100. < result.best_thickness_nm['low-film'] < 125.
    assert result.evaluations > 17 and result.jacobian_evaluations > 17
    assert result.best_analysis.nominal_targets_met == all(
        row.actual <= 0. for row in result.best_analysis.nominal_conditions)
    assert result.solver_acceptable == (
        result.best_analysis.nominal_max_normalized_error <= request.acceptable_normalized_error)
    validate_film_sensitivity_result_payload(film_sensitivity_result_to_payload(result), expected_request=result.frozen_request)


def test_finite_expected_uses_explicit_scenario_weights_and_worst_uses_weighted_maximum():
    service, request = problem(objective='finite_scenario_expected')
    request = replace(request, scenarios=(FilmSensitivityScenario('small', (-.1,), 9.),
        FilmSensitivityScenario('large', (1.,), 1.)))
    result = analyze_film_sensitivity(service, request)
    assert result.best_analysis is not None
    rows = result.best_analysis.finite_scenarios
    expected = np.sqrt(sum(r.weight*r.finite_weighted_rms_error**2 for r in rows)/10.)
    assert result.best_analysis.robustness_value == pytest.approx(expected)
    worst = analyze_film_sensitivity(service, replace(request, robustness_objective='finite_scenario_worst'))
    assert worst.best_analysis is not None
    assert worst.best_analysis.robustness_value == max(r.finite_max_normalized_error for r in rows)


def test_nominal_acceptance_is_unmodified_even_when_robustness_degrades_nominal():
    service, request = problem(operation='optimize')
    # d=80 gives a known exact nominal target, but its derivative is not minimal.
    preliminary = analyze_film_sensitivity(service, replace(request, operation='analyze'))
    assert preliminary.best_analysis is not None
    target = preliminary.best_analysis.linear_conditions[0].nominal_value
    request = replace(request, objectives=(FilmOptimizationObjective(550.,550.,1,
        target_value=target),), acceptable_normalized_error=1e-10)
    result = optimize_film_sensitivity(service, request)
    assert result.baseline_analysis is not None and result.best_analysis is not None
    assert result.baseline_analysis.nominal_targets_met
    assert result.improved and not result.nominal_targets_met
    assert result.best_analysis.nominal_max_normalized_error > request.acceptable_normalized_error


def test_frozen_replay_ignores_live_material_edits_and_cache_identity_includes_every_input():
    service, request = problem()
    request = freeze_film_sensitivity_request(service, request)
    first = analyze_film_sensitivity(None, request)
    service.materials.records.clear()
    second = analyze_film_sensitivity(None, film_sensitivity_request_from_payload(film_sensitivity_request_to_payload(request)))
    assert first == second
    changed = analyze_film_sensitivity(None, replace(request, errors=(FilmThicknessError('low-film', 2.),)))
    assert changed.best_analysis is not None and first.best_analysis is not None
    assert changed.cache_identity != first.cache_identity
    assert changed.best_analysis.linear_sensitivity_norm == pytest.approx(2*first.best_analysis.linear_sensitivity_norm)


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(extra=True),
    lambda p: p['errors'][0].update(material_error=.01),
    lambda p: p['variables'][0].update(ignored=True),
    lambda p: p['objectives'][0].update(ignored=True),
    lambda p: p['scenarios'][0].update(ignored=True),
    lambda p: p.update(schema_version=True),
    lambda p: p.update(robustness_objective='nominal'),
    lambda p: p.update(error_model='correlated'),
])
def test_request_serialization_rejects_unknown_fields_and_unsupported_semantics(mutation):
    _, request = problem()
    payload = film_sensitivity_request_to_payload(request)
    mutation(payload)
    with pytest.raises(ValueError):
        film_sensitivity_request_from_payload(payload)


def test_strict_result_roundtrip_replay_rejects_tampering_and_changed_snapshot_identity():
    service, request = problem()
    result = analyze_film_sensitivity(service, request)
    payload = film_sensitivity_result_to_payload(result)
    assert film_sensitivity_result_from_payload(json.loads(json.dumps(payload))) == result
    validate_film_sensitivity_result_payload(payload, expected_request=result.frozen_request)
    extra = deepcopy(payload)
    extra['best_analysis']['linear_conditions'][0]['extra'] = True
    with pytest.raises(ValueError):
        film_sensitivity_result_from_payload(extra)
    tampered = deepcopy(payload)
    tampered['best_analysis']['robustness_value'] += .001
    with pytest.raises(ValueError, match='replay|baseline'):
        validate_film_sensitivity_result_payload(tampered)
    material = deepcopy(payload)
    material['best_design']['front_layers'][0]['material_id']='high'
    with pytest.raises(ValueError, match='invariants'):
        validate_film_sensitivity_result_payload(material)
    changed = replace(result.frozen_request, acceptable_normalized_error=.01)
    with pytest.raises(ValueError, match='identity'):
        validate_film_sensitivity_result_payload(payload, expected_request=changed)


@pytest.mark.parametrize('stage', ['freezing', 'baseline_complete', 'search', 'improved', 'iteration'])
def test_cancellation_preserves_true_state_and_only_complete_incumbents(stage):
    service, request = problem(operation='optimize')
    cancel = Event()
    checkpoints: list[dict[str, Any]] = []
    def progress(value):
        if value.stage == stage:
            cancel.set()
    result = optimize_film_sensitivity(service, request, cancel_event=cancel,
        progress_callback=progress, checkpoint_callback=checkpoints.append)
    assert result.state == 'cancelled' and result.termination_reason == 'cancelled'
    assert result.is_final
    for checkpoint in checkpoints:
        checked = film_sensitivity_result_from_payload(validate_film_sensitivity_result_payload(checkpoint, expected_request=result.frozen_request))
        assert checked.state == 'partial' and not checked.is_final
    if stage != 'freezing':
        assert result.best_analysis is not None and checkpoints


def test_budget_returns_partial_not_false_convergence():
    service, request = problem(operation='optimize')
    result = optimize_film_sensitivity(service, replace(request,max_evaluations=2))
    assert result.state == 'partial' and result.termination_reason == 'evaluation_limit'
    assert result.evaluations == 2 and result.best_analysis is not None
    limited = optimize_film_sensitivity(service, replace(request, max_iterations=1))
    assert limited.state == 'partial' and limited.termination_reason == 'iteration_limit'


def test_negative_finite_thickness_is_failure_without_clipping_and_invalid_contract_raises():
    service, request = problem()
    failed = analyze_film_sensitivity(service, replace(request, errors=(FilmThicknessError('low-film', 90.),)))
    assert failed.state == 'failed' and failed.best_analysis is None
    assert 'never clipped' in failed.message
    film_sensitivity_result_to_payload(failed)
    with pytest.raises(ValueError, match='same coefficient'):
        _, two = problem(two=True,error_model='common_mode')
        analyze_film_sensitivity(service, replace(two,scenarios=(FilmSensitivityScenario('bad',(-1.,1.)),)))
    with pytest.raises(ValueError, match='explicit user-chosen'):
        analyze_film_sensitivity(service, replace(request, robustness_objective='finite_scenario_worst', scenarios=()))


def test_adapter_is_independent_and_ordinary_nominal_serialization_is_unchanged():
    _, request = problem()
    base = FilmOptimizationRequest(request.design_snapshot,request.design_revision,request.variables,request.objectives,
        max_iterations=123,max_evaluations=456,acceptable_normalized_error=.017)
    adapted = sensitivity_request_from_optimization(base, errors=request.errors,error_model='independent',
        robustness_objective='linear_sensitivity_norm')
    assert adapted.acceptable_normalized_error == .017
    assert adapted.max_iterations is None and adapted.max_evaluations is None
    assert not hasattr(base,'errors') and not hasattr(base,'robustness_objective')


@pytest.mark.parametrize('field,value', [('robustness_objective','linear_sensitivity_norm'),
    ('errors',[]),('error_model','independent'),('scenarios',[]),('operation','analyze')])
def test_ordinary_v1_request_rejects_all_sensitivity_fields(field,value):
    from coat_sim.core.film_optimization_job import serialize_request,deserialize_request
    _,request = problem()
    base = FilmOptimizationRequest(request.design_snapshot,request.design_revision,request.variables,request.objectives)
    payload = serialize_request(base)
    payload[field] = value
    with pytest.raises(ValueError,match='fields'):
        deserialize_request(payload)


def test_no_unselected_nominal_thickness_changes_and_explicit_search_budget_counts():
    service,request = problem(two=True,operation='optimize')
    request = replace(request,variables=(request.variables[0],),max_evaluations=2)
    result = optimize_film_sensitivity(service,request)
    assert result.state == 'partial' and result.evaluations == 2
    assert result.best_thickness_nm['high-film'] == 40.
    payload = film_sensitivity_result_to_payload(result)
    validate_film_sensitivity_result_payload(payload,expected_request=result.frozen_request)
    tampered = deepcopy(payload)
    tampered['best_design']['front_layers'][1]['thickness']['value'] += 1.
    tampered['best_thickness_nm']['high-film'] += 1.
    with pytest.raises(ValueError,match='unselected'):
        validate_film_sensitivity_result_payload(tampered,replay=False)


def test_resource_limit_is_explicit_and_never_silently_drops_scenarios_or_grid():
    _,request = problem()
    large = replace(request,objectives=(replace(request.objectives[0],
        wavelength_min_nm=450.,wavelength_max_nm=700.,wavelength_samples=1_000_000),))
    estimate = sensitivity.estimate_film_sensitivity_resources(large)
    assert estimate['condition_count'] == 1_000_000 and estimate['scenario_count'] == 2
    assert estimate['estimated_retained_result_bytes'] > estimate['max_retained_result_bytes']
    with pytest.raises(ValueError,match='no scenarios or conditions were dropped'):
        film_sensitivity_request_to_payload(large)


def test_worst_and_expected_use_distinct_condition_and_scenario_weight_semantics():
    service,request = problem(objective='finite_scenario_worst')
    objective = replace(request.objectives[0],weight=9.,normalization_scale=.1)
    request = replace(request,objectives=(objective,))
    worst = analyze_film_sensitivity(service,request).best_analysis
    expected = analyze_film_sensitivity(service,replace(request,
        robustness_objective='finite_scenario_expected')).best_analysis
    assert worst is not None and expected is not None
    assert worst.robustness_value == pytest.approx(3*max(s.finite_max_normalized_error for s in worst.finite_scenarios))
    assert expected.robustness_value == pytest.approx(np.sqrt(np.mean([
        s.finite_weighted_rms_error**2 for s in expected.finite_scenarios])))


def test_full_frozen_cache_identity_changes_with_material_content_and_optical_conditions():
    service,request = problem()
    original = analyze_film_sensitivity(service,request)
    service.materials.records['low']['optical_sets'][0]['n'] = [1.8]*3
    changed = analyze_film_sensitivity(service,request)
    assert changed.best_analysis is not None and original.best_analysis is not None
    assert changed.cache_identity != original.cache_identity
    assert changed.best_analysis.linear_sensitivity_norm != original.best_analysis.linear_sensitivity_norm
    third = analyze_film_sensitivity(service,replace(request,objectives=(replace(
        request.objectives[0],angles_deg=(30.,)),)))
    assert third.cache_identity not in (original.cache_identity,changed.cache_identity)
