from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event
from typing import Any, cast

import numpy as np
import pytest

from coat_sim.core import film_thickness_error_table as table
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def problem(*, two=False, rows=None, low=550., high=550., samples=1, **kwargs):
    service, synthesis = setup_problem(low=low, high=high, samples=samples, upper=.1)
    design = deepcopy(synthesis.environment)
    design['front_layers'] = [dict(id='low-film', material_id='low', optical_set_id='nk',
        thickness=dict(value=80., unit='nm'), note='synthetic parameter-table fixture')]
    if two:
        design['front_layers'].append(dict(id='high-film', material_id='high', optical_set_id='nk',
            thickness=dict(value=40., unit='nm'), note='synthetic second layer'))
    if rows is None:
        rows = (table.FilmThicknessErrorParameter('low-film', bias=1., bias_unit='absolute_nm'),)
    base = FilmOptimizationRequest(design, 9, (), synthesis.objectives)
    request = table.thickness_error_table_request_from_optimization(base, rows=rows, **kwargs)
    return service, request


def run(*args, **kwargs):
    service, request = problem(*args, **kwargs)
    result = table.execute_film_thickness_error_table_request(service, request)
    assert result.state == 'completed', result.message
    assert result.analysis is not None
    return service, request, result


def fake_jacobian(monkeypatch, columns=(2., -3.), nominal=.2):
    def response(design, materials, variables, settings, *, wavelengths_nm):
        count = settings['samples']
        assert wavelengths_nm.shape == (count,)
        assert wavelengths_nm[0] == settings['wavelength_min_nm']
        assert wavelengths_nm[-1] == settings['wavelength_max_nm']
        jac = np.tile(columns[:len(variables)], (count, 1)) if variables else np.empty((count, 0))
        return dict(wavelength_nm=np.array(wavelengths_nm, copy=True),
                    R=np.full(count, nominal), T=np.full(count, 1 - nominal),
                    jacobian_R=jac, jacobian_T=-jac)
    monkeypatch.setattr(table, 'simulate_with_thickness_jacobian', response)


@pytest.mark.parametrize('rows', [(), (table.FilmThicknessErrorParameter('low-film'),),
    (table.FilmThicknessErrorParameter('low-film', uncertainty_kind='bounded'),),
    (table.FilmThicknessErrorParameter('low-film', enabled=False, bias=1., bias_unit='absolute_nm'),)])
def test_all_blank_or_disabled_is_not_computable(rows):
    with pytest.raises(ValueError, match='explicitly defined'):
        problem(rows=rows)


def test_none_zero_signed_bias_and_bias_only_are_distinct():
    _, request, zero = run(rows=(table.FilmThicknessErrorParameter('low-film', bias=0., bias_unit='absolute_nm'),))
    row = zero.analysis.conditions[0]
    assert row.bias_shift == 0. and row.linear_bias_value == row.nominal_value
    assert row.aggregate_sigma is None and row.bounded_first_order_l1 is None
    assert zero.analysis.coverage.defined_bias_layer_ids == ('low-film',)
    assert zero.analysis.aggregate_sigma_status == 'not_configured'
    _, _, signed = run(rows=(table.FilmThicknessErrorParameter('low-film', bias=-2., bias_unit='absolute_nm'),))
    assert signed.analysis.conditions[0].bias_shift != 0
    _, _, positive = run(rows=(replace(request.rows[0], bias=2.),))
    assert signed.analysis.conditions[0].bias_shift == pytest.approx(-positive.analysis.conditions[0].bias_shift)
    assert signed.cache_identity != positive.cache_identity != zero.cache_identity
    assert not hasattr(signed, 'candidate_design') and not hasattr(signed, 'best_design')


@pytest.mark.parametrize('row', [
    table.FilmThicknessErrorParameter('missing', bias=1., bias_unit='absolute_nm'),
    table.FilmThicknessErrorParameter('low-film', enabled=cast(Any, 1), bias=1., bias_unit='absolute_nm'),
    table.FilmThicknessErrorParameter('low-film', bias=1.),
    table.FilmThicknessErrorParameter('low-film', bias_unit='absolute_nm'),
    table.FilmThicknessErrorParameter('low-film', bias=float('nan'), bias_unit='absolute_nm'),
    table.FilmThicknessErrorParameter('low-film', bias=1., bias_unit='percent'),
    table.FilmThicknessErrorParameter('low-film', uncertainty_magnitude=1., uncertainty_unit='absolute_nm'),
    table.FilmThicknessErrorParameter('low-film', uncertainty_kind='bounded', uncertainty_magnitude=-1., uncertainty_unit='absolute_nm'),
    table.FilmThicknessErrorParameter('low-film', uncertainty_kind='stddev', uncertainty_magnitude=-1., uncertainty_unit='absolute_nm'),
    table.FilmThicknessErrorParameter('low-film', uncertainty_kind='bounded', uncertainty_magnitude=1.),
])
def test_invalid_parameters_reject(row):
    with pytest.raises(ValueError):
        problem(rows=(row,))


def test_duplicate_backside_and_unknown_layer_ids_reject():
    row = table.FilmThicknessErrorParameter('low-film', bias=1., bias_unit='absolute_nm')
    with pytest.raises(ValueError, match='uniquely'):
        problem(rows=(row, row))
    service, request = problem()
    design = deepcopy(request.design_snapshot)
    back = deepcopy(design['front_layers'][0])
    back['id'] = 'back-film'
    design['back_layers'] = [back]
    with pytest.raises(ValueError, match='single-sided'):
        table.freeze_film_thickness_error_table_request(service, replace(request, design_snapshot=design,
            rows=(replace(row, layer_id='back-film'),)))


def test_relative_fraction_and_qw_use_frozen_nominal_physical_thickness():
    service, request, absolute = run()
    relative = table.execute_film_thickness_error_table_request(service, replace(request,
        rows=(table.FilmThicknessErrorParameter('low-film', bias=1 / 80., bias_unit='relative_fraction'),)))
    assert relative.analysis is not None
    assert absolute.analysis is not None
    assert relative.analysis.conditions[0].bias_shift == pytest.approx(absolute.analysis.conditions[0].bias_shift)
    qw = deepcopy(request.design_snapshot)
    qw['front_layers'][0]['thickness'] = dict(value=80 * 4 * np.sqrt(1.5) / 550., unit='qw')
    converted = table.execute_film_thickness_error_table_request(service, replace(request, design_snapshot=qw,
        rows=(table.FilmThicknessErrorParameter('low-film', uncertainty_kind='bounded',
              uncertainty_magnitude=.01, uncertainty_unit='relative_fraction'),)))
    assert converted.state == 'completed', converted.message
    assert converted.analysis is not None
    assert converted.analysis.layer_summaries[0].nominal_thickness_nm == pytest.approx(80.)
    assert converted.analysis.layer_summaries[0].uncertainty_nm == pytest.approx(.8)
    # Percent is a UI conversion, never a second ambiguous core unit.
    assert converted.frozen_request.rows[0].uncertainty_magnitude == .01


def test_large_finite_bias_is_separate_from_linear_and_unselected_layers_stay_nominal():
    service, request, result = run(two=True,
        rows=(table.FilmThicknessErrorParameter('low-film', bias=30., bias_unit='absolute_nm'),),
        include_finite_bias=True)
    assert result.analysis is not None
    row = result.analysis.conditions[0]
    assert abs(row.finite_bias_value - row.linear_bias_value) > .001
    assert result.analysis.finite_bias_scope == 'defined_subset_only'
    assert result.analysis.coverage.missing_bias_layer_ids == ('high-film',)
    changed = deepcopy(request.design_snapshot)
    changed['front_layers'][0]['thickness']['value'] += 30.
    comparison = table.execute_film_thickness_error_table_request(service, replace(request,
        design_snapshot=changed, include_finite_bias=False))
    assert comparison.analysis is not None
    assert row.finite_bias_value == pytest.approx(comparison.analysis.conditions[0].nominal_value)
    assert result.frozen_request.design_snapshot['front_layers'][1]['thickness']['value'] == 40.


def test_negative_finite_thickness_rejects_without_clipping_or_forward_work():
    service, request = problem(rows=(table.FilmThicknessErrorParameter('low-film', bias=-81., bias_unit='absolute_nm'),),
                               include_finite_bias=True)
    with pytest.raises(ValueError, match='negative physical thickness'):
        table.freeze_film_thickness_error_table_request(service, request)
    result = table.execute_film_thickness_error_table_request(service, request)
    assert result.state == 'failed' and result.evaluations == 0 and result.analysis is None
    assert 'no clipping' in result.message
    # Negative linear biases remain legal when finite perturbation was not requested.
    linear = table.execute_film_thickness_error_table_request(service, replace(request, include_finite_bias=False))
    assert linear.state == 'completed'


def test_bounded_amplitudes_are_l1_not_rss_and_bias_is_not_double_counted(monkeypatch):
    fake_jacobian(monkeypatch)
    _, _, result = run(two=True, rows=(
        table.FilmThicknessErrorParameter('low-film', bias=-1., bias_unit='absolute_nm',
            uncertainty_kind='bounded', uncertainty_magnitude=4., uncertainty_unit='absolute_nm'),
        table.FilmThicknessErrorParameter('high-film', uncertainty_kind='bounded', uncertainty_magnitude=5., uncertainty_unit='absolute_nm')))
    condition = result.analysis.conditions[0]
    assert condition.bias_shift == -2.
    assert condition.bounded_first_order_l1 == 23.
    assert condition.aggregate_sigma is None
    assert condition.bounded_first_order_l1 != pytest.approx(np.hypot(8., 15.))
    assert result.analysis.layer_summaries[0].max_bounded_contribution == 8.
    assert result.analysis.layer_summaries[1].max_bounded_contribution == 15.


def sigma_rows():
    return (table.FilmThicknessErrorParameter('low-film', uncertainty_kind='stddev', uncertainty_magnitude=2., uncertainty_unit='absolute_nm'),
            table.FilmThicknessErrorParameter('high-film', uncertainty_kind='stddev', uncertainty_magnitude=1., uncertainty_unit='absolute_nm'))


def test_stddev_requires_explicit_correlation_and_identity_is_an_explicit_choice(monkeypatch):
    fake_jacobian(monkeypatch)
    _, _, unknown = run(two=True, rows=sigma_rows())
    assert unknown.analysis.conditions[0].aggregate_sigma is None
    assert unknown.analysis.conditions[0].aggregate_sigma_status == 'missing_information'
    assert unknown.analysis.aggregate_sigma_status == 'unavailable'
    assert [s.max_stddev_contribution for s in unknown.analysis.layer_summaries] == [4., 3.]
    _, _, independent = run(two=True, rows=sigma_rows(), correlation_layer_ids=('low-film', 'high-film'),
                            correlation_matrix=((1., 0.), (0., 1.)))
    assert independent.analysis.conditions[0].aggregate_sigma == 5.
    _, _, correlated = run(two=True, rows=sigma_rows(), correlation_layer_ids=('low-film', 'high-film'),
                           correlation_matrix=((1., .5), (.5, 1.)))
    assert correlated.analysis.conditions[0].aggregate_sigma == pytest.approx(np.sqrt(13.))
    assert independent.cache_identity != unknown.cache_identity != correlated.cache_identity
    assert correlated.analysis.correlation.matrix == ((1., .5), (.5, 1.))
    assert correlated.analysis.correlation.matrix_unmodified


@pytest.mark.parametrize('matrix,ids', [
    (((1., .5), (.5 + 1e-15, 1.)), ('low-film', 'high-film')),
    (((1. + 1e-15, 0.), (0., 1.)), ('low-film', 'high-film')),
    (((1., 1.01), (1.01, 1.)), ('low-film', 'high-film')),
    (((1., 0.), (0., 1.)), ('high-film', 'low-film')),
    (((1., 0.),), ('low-film', 'high-film')),
    (None, ('low-film', 'high-film')),
])
def test_bad_correlation_shape_exact_symmetry_diagonal_or_column_order_reject(matrix, ids):
    with pytest.raises(ValueError):
        problem(two=True, rows=sigma_rows(), correlation_layer_ids=ids, correlation_matrix=matrix)


def test_materially_non_psd_reject_and_near_psd_keeps_original_matrix():
    service, request = problem(two=True, rows=sigma_rows())
    third = deepcopy(request.design_snapshot['front_layers'][0])
    third['id'] = 'third-film'
    design = deepcopy(request.design_snapshot)
    design['front_layers'].append(third)
    rows = sigma_rows() + (replace(sigma_rows()[0], layer_id='third-film'),)
    with pytest.raises(ValueError, match='positive semidefinite'):
        table.freeze_film_thickness_error_table_request(service, replace(request, design_snapshot=design, rows=rows,
            correlation_layer_ids=('low-film', 'high-film', 'third-film'),
            correlation_matrix=((1., -.9, -.9), (-.9, 1., -.9), (-.9, -.9, 1.))))
    r = -.5 - 1e-15
    matrix = ((1., r, r), (r, 1., r), (r, r, 1.))
    request = replace(request, design_snapshot=design, rows=rows,
        correlation_layer_ids=('low-film', 'high-film', 'third-film'), correlation_matrix=matrix)
    frozen = table.freeze_film_thickness_error_table_request(service, request)
    report = table._correlation(frozen)
    assert report.tau is not None and report.min_eigenvalue is not None
    assert -report.tau <= report.min_eigenvalue < 0
    assert report.status == 'psd_numerically_uncertain'
    assert frozen.correlation_matrix == matrix == report.matrix
    assert report.matrix_unmodified
    expected_tau = 64 * np.finfo(float).eps * 3 * max(1., np.linalg.norm(matrix, 2))
    assert report.tau == expected_tau


def test_negative_q_is_unavailable_not_clamped_and_other_points_survive(monkeypatch):
    fake_jacobian(monkeypatch, columns=(1., 1.))
    # q now uses positive scale=4 for original v=(2,1); preserve the same raw q=2.
    answers = iter((-1e-30, 2. / 16.))
    monkeypatch.setattr(table, '_quadratic_form', lambda vector, matrix: next(answers))
    _, _, result = run(two=True, rows=sigma_rows(), low=550., high=551., samples=2,
        correlation_layer_ids=('low-film', 'high-film'), correlation_matrix=((1., 0.), (0., 1.)))
    assert result.state == 'completed'
    assert result.analysis.aggregate_sigma_status == 'partial'
    assert result.analysis.conditions[0].aggregate_sigma is None
    assert result.analysis.conditions[0].aggregate_sigma_status == 'numerical_indeterminate'
    assert result.analysis.conditions[1].aggregate_sigma == pytest.approx(np.sqrt(2.))
    assert all(s.max_stddev_contribution is not None for s in result.analysis.layer_summaries)
    table.film_thickness_error_table_result_from_payload(table.film_thickness_error_table_result_to_payload(result))


def test_perfect_anticorrelation_zero_is_valid(monkeypatch):
    fake_jacobian(monkeypatch, columns=(1., 2.))
    _, _, result = run(two=True, rows=sigma_rows(), correlation_layer_ids=('low-film', 'high-film'),
                       correlation_matrix=((1., -1.), (-1., 1.)))
    assert result.analysis.conditions[0].aggregate_sigma == 0.
    assert result.analysis.conditions[0].aggregate_sigma_status == 'available'


def test_mixed_uncertainty_has_separate_columns_no_combined_probability(monkeypatch):
    fake_jacobian(monkeypatch)
    _, _, result = run(two=True, rows=(sigma_rows()[0],
        table.FilmThicknessErrorParameter('high-film', uncertainty_kind='bounded', uncertainty_magnitude=5., uncertainty_unit='absolute_nm')),
        correlation_layer_ids=('low-film',), correlation_matrix=((1.,),))
    row = result.analysis.conditions[0]
    assert row.bounded_first_order_l1 == 15. and row.aggregate_sigma == 4.
    assert row.bias_shift is None
    assert not any('probability' in key or 'total_sigma' in key for key in vars(row))


def test_missing_disabled_defined_and_undefined_sets_are_explicit():
    _, _, result = run(two=True, rows=(
        table.FilmThicknessErrorParameter('low-film', bias=0., bias_unit='absolute_nm'),
        table.FilmThicknessErrorParameter('high-film', enabled=False, uncertainty_kind='stddev',
                                          uncertainty_magnitude=0., uncertainty_unit='absolute_nm')))
    coverage = result.analysis.coverage
    assert coverage.disabled_defined_layer_ids == ('high-film',)
    assert coverage.missing_bias_layer_ids == ('high-film',)
    assert coverage.bias_scope == 'defined_subset_only'
    _, _, missing = run(two=True)
    assert missing.analysis.coverage.unconfigured_layer_ids == ('high-film',)
    _, _, blank = run(two=True, rows=(
        table.FilmThicknessErrorParameter('low-film', bias=0., bias_unit='absolute_nm'),
        table.FilmThicknessErrorParameter('high-film')))
    assert blank.analysis.coverage.enabled_undefined_layer_ids == ('high-film',)
    assert blank.analysis.coverage.missing_uncertainty_layer_ids == ('low-film', 'high-film')


def test_complete_original_1nm_grid_all_angles_materials_and_revision_remain_frozen():
    service, request = problem(low=500., high=600., samples=101)
    objective = replace(request.objectives[0], angles_deg=(0., 42.), metric='T', polarization='p')
    request = replace(request, objectives=(objective,))
    frozen = table.freeze_film_thickness_error_table_request(service, request)
    original = table.film_thickness_error_table_request_to_payload(frozen)
    result = table.execute_film_thickness_error_table_request(None, frozen)
    assert result.state == 'completed'
    assert result.analysis is not None
    assert result.analysis.completed_condition_count == 202
    assert result.design_revision == 9
    assert [r.wavelength_nm for r in result.analysis.conditions[:101]] == list(range(500, 601))
    assert {r.angle_deg for r in result.analysis.conditions} == {0., 42.}
    assert table.film_thickness_error_table_request_to_payload(result.frozen_request) == original
    service.materials.records.clear()
    replay = table.execute_film_thickness_error_table_request(None, frozen)
    assert replay == result


@pytest.mark.parametrize('bound,strict_flag', [('upper_bound', 'strict_upper'),
                                             ('lower_bound', 'strict_lower')])
def test_strict_boundary_is_only_explicit_no_hidden_margin(bound, strict_flag):
    service, request, baseline = run()
    assert baseline.analysis is not None
    actual = baseline.analysis.conditions[0].nominal_value
    objective = FilmOptimizationObjective(550., 550., 1, pointwise=FilmObjectiveConstraint(
        mode='range', target_value=None, lower_bound=actual if bound == 'lower_bound' else None,
        upper_bound=actual if bound == 'upper_bound' else None,
        **{strict_flag: True}))
    inclusive_objective = replace(objective, pointwise=replace(
        objective.pointwise, strict_lower=False, strict_upper=False))
    inclusive = table.execute_film_thickness_error_table_request(service,
        replace(request, objectives=(inclusive_objective,)))
    assert inclusive.analysis is not None
    assert inclusive.analysis.nominal_targets_met
    strict = table.execute_film_thickness_error_table_request(service,
        replace(request, objectives=(objective,)))
    assert strict.analysis is not None
    assert not strict.analysis.nominal_targets_met
    assert inclusive.analysis.conditions[0].nominal_value == strict.analysis.conditions[0].nominal_value


def test_cancellation_budget_checkpoint_and_counts_are_separate_from_sigma_status(monkeypatch):
    monkeypatch.setattr(table, 'MAX_CHUNK_SAMPLES', 2)
    service, request = problem(low=550., high=554., samples=5)
    event = Event()
    event.set()
    cancelled = table.execute_film_thickness_error_table_request(service, request, cancel_event=event)
    assert cancelled.state == 'cancelled' and cancelled.evaluations == 0 and cancelled.analysis is None
    checkpoints: list[Mapping[str, Any]] = []
    limited = table.execute_film_thickness_error_table_request(service, replace(request, max_evaluations=1),
        checkpoint_callback=checkpoints.append)
    assert limited.state == 'partial' and limited.termination_reason == 'evaluation_limit'
    assert limited.analysis is not None
    assert limited.evaluations == limited.jacobian_evaluations == limited.forward_evaluations == 1
    assert limited.analysis.completed_condition_count == 2
    assert limited.analysis.nominal_targets_met is None
    assert limited.analysis.aggregate_sigma_status == 'not_configured'
    assert checkpoints[0]['is_final'] is False
    table.validate_film_thickness_error_table_result_payload(checkpoints[0], replay=True)
    table.validate_film_thickness_error_table_result_payload(table.film_thickness_error_table_result_to_payload(limited))
    event.clear()
    def progress(update):
        if update.stage == 'block_complete':
            event.set()
    stopped = table.execute_film_thickness_error_table_request(service, request, cancel_event=event,
                                                              progress_callback=progress)
    assert stopped.analysis is not None
    assert stopped.state == 'cancelled' and stopped.analysis.completed_condition_count == 2
    assert stopped.evaluations == 1
    table.validate_film_thickness_error_table_result_payload(table.film_thickness_error_table_result_to_payload(stopped))


def test_budget_between_nominal_and_finite_does_not_claim_condition_complete():
    service, request = problem(include_finite_bias=True)
    result = table.execute_film_thickness_error_table_request(service, replace(request, max_evaluations=1))
    assert result.state == 'partial' and result.analysis is None
    assert result.evaluations == result.jacobian_evaluations == 1


def test_resource_preflight_rejects_without_freezing_or_subsampling(monkeypatch):
    service, request = problem()
    def denied(*args, **kwargs):
        pytest.fail('resource preflight must reject before material freezing')
    monkeypatch.setattr(service, 'freeze_design', denied)
    with pytest.raises(ValueError, match='no samples or layers were dropped'):
        table.execute_film_thickness_error_table_request(service, replace(request,
            objectives=(replace(request.objectives[0], wavelength_max_nm=600., wavelength_samples=2_000_000),)))
    estimate = table.estimate_film_thickness_error_table_resources(request)
    assert estimate['condition_count'] == estimate['derivative_column_count'] == 1


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(extra=True), lambda p: p['rows'][0].update(error_model='independent'),
    lambda p: p['objectives'][0].update(extra=True), lambda p: p.update(schema_version=True),
    lambda p: p['rows'][0].update(bias=float('nan')),
])
def test_strict_request_payload_rejects_unknown_fields_and_nonfinite(mutation):
    _, request = problem()
    payload = table.film_thickness_error_table_request_to_payload(request)
    mutation(payload)
    with pytest.raises(ValueError):
        table.film_thickness_error_table_request_from_payload(payload)


def test_serialization_roundtrip_exact_identity_and_frozen_physical_replay_reject_tampering():
    _, _, result = run(two=True, rows=(
        table.FilmThicknessErrorParameter('low-film', bias=1., bias_unit='absolute_nm',
            uncertainty_kind='bounded', uncertainty_magnitude=0., uncertainty_unit='absolute_nm'),
        table.FilmThicknessErrorParameter('high-film', uncertainty_kind='stddev', uncertainty_magnitude=2., uncertainty_unit='absolute_nm')),
        correlation_layer_ids=('high-film',), correlation_matrix=((1.,),), include_finite_bias=True)
    payload = table.film_thickness_error_table_result_to_payload(result)
    assert table.film_thickness_error_table_result_from_payload(json.loads(json.dumps(payload))) == result
    table.validate_film_thickness_error_table_result_payload(payload, expected_request=result.frozen_request)
    for mutate in (
        lambda p: p.update(extra=True),
        lambda p: p['analysis']['conditions'][0].update(extra=True),
        lambda p: p['analysis']['layer_summaries'][0].update(extra=True),
        lambda p: p['analysis']['coverage'].update(extra=True),
        lambda p: p['analysis']['correlation'].update(extra=True),
        lambda p: p['analysis']['conditions'][0].update(bias_shift=123., linear_bias_value=123. + p['analysis']['conditions'][0]['nominal_value']),
        lambda p: p['analysis']['conditions'][0].update(aggregate_sigma=123.),
        lambda p: p['analysis']['layer_summaries'][0].update(max_abs_bias_contribution=123.),
        lambda p: p.update(cache_identity='0' * 64),
        lambda p: p.update(design_revision=100),
        lambda p: p['analysis']['conditions'][0].update(wavelength_nm=551.),
    ):
        changed = deepcopy(payload)
        mutate(changed)
        with pytest.raises(ValueError):
            table.validate_film_thickness_error_table_result_payload(changed)
    wrong = replace(result.frozen_request, rows=(replace(result.frozen_request.rows[0], bias=0.), result.frozen_request.rows[1]))
    with pytest.raises(ValueError, match='different expected_request'):
        table.validate_film_thickness_error_table_result_payload(payload, expected_request=wrong)


def direct_spectrum(service, design, settings):
    # Exercise the ordinary spectrum implementation with in-memory synthetic
    # materials; no material library or user project is created on disk.
    direct = FilmDesignService.__new__(FilmDesignService)
    cast(Any, direct).materials = service.materials
    return direct.simulate(design, settings)['R']


def required_values(values: list[float | None]) -> list[float]:
    result = []
    for value in values:
        assert value is not None, 'every original condition must define the requested quantity'
        result.append(value)
    return result


@pytest.mark.parametrize('material,kind', [('low', 'tabulated'), ('glass', 'sellmeier')])
@pytest.mark.parametrize('finite_bias', [False, True])
def test_missing_source_k_matches_ordinary_spectrum_sigma_and_finite_bias(material, kind, finite_bias):
    rows = tuple(table.FilmThicknessErrorParameter(identifier, bias=.02, bias_unit='relative_fraction',
        uncertainty_kind='stddev', uncertainty_magnitude=1., uncertainty_unit='absolute_nm')
        for identifier in ('low-film', 'high-film'))
    service, request = problem(two=True, low=400., high=700., samples=301, rows=rows,
        correlation_layer_ids=('low-film', 'high-film'), correlation_matrix=((1., 0.), (0., 1.)),
        include_finite_bias=finite_bias)
    for record in service.materials.records.values():
        record['optical_sets'][0].update(wavelength_min=400., wavelengths=[400., 550., 700.])
    optical = service.materials.records[material]['optical_sets'][0]
    optical['k'] = None
    if kind == 'sellmeier':
        # Synthetic n-only glass with the same nm Sellmeier representation as
        # H-K9L. These coefficients are a test model, not material evidence.
        for key in ('wavelengths', 'n', 'k'):
            optical.pop(key)
        optical.update(kind='sellmeier', parameters=dict(B1=1.2, B2=.05, B3=.01,
            C1=6000., C2=20000., C3=1.e8))
    original = deepcopy(service.materials.records)
    result = table.execute_film_thickness_error_table_request(service, request)
    assert result.state == 'completed', result.message
    assert result.analysis is not None
    assert result.analysis.material_k_policy == 'missing_source_k_zero_known_source_k_required'
    frozen = result.frozen_request.frozen_material_snapshot
    assert frozen is not None and frozen['design']['use_k'] is True
    assert next(r for r in frozen['materials'] if r['id'] == material)['optical_sets'][0] == optical
    assert service.materials.records == original
    settings = frozen['settings']
    nominal = direct_spectrum(service, request.design_snapshot, settings)
    np.testing.assert_allclose([r.nominal_value for r in result.analysis.conditions], nominal, atol=2e-14)
    gradients = []
    for index in range(2):
        values = []
        for sign in (-1., 1.):
            changed = deepcopy(request.design_snapshot)
            changed['front_layers'][index]['thickness']['value'] += sign * .001
            values.append(np.asarray(direct_spectrum(service, changed, settings)))
        gradients.append((values[1] - values[0]) / .002)
    np.testing.assert_allclose(required_values([r.aggregate_sigma for r in result.analysis.conditions]),
        np.sqrt(np.sum(np.square(gradients), axis=0)), rtol=2e-7, atol=2e-12)
    if finite_bias:
        changed = deepcopy(request.design_snapshot)
        for layer in changed['front_layers']:
            layer['thickness']['value'] *= 1.02
        np.testing.assert_allclose(required_values([r.finite_bias_value for r in result.analysis.conditions]),
            direct_spectrum(service, changed, settings), atol=2e-14)
    payload = table.film_thickness_error_table_result_to_payload(result)
    table.validate_film_thickness_error_table_result_payload(payload, expected_request=result.frozen_request)
    payload['analysis']['material_k_policy'] = 'source_k_required'
    with pytest.raises(ValueError, match='material-k policy'):
        table.validate_film_thickness_error_table_result_payload(payload, replay=False)


def test_known_absorption_is_used_in_both_sigma_and_finite_bias():
    service, request = problem(low=500., high=600., samples=7, include_finite_bias=True,
        rows=(table.FilmThicknessErrorParameter('low-film', bias=.02, bias_unit='relative_fraction',
            uncertainty_kind='stddev', uncertainty_magnitude=1., uncertainty_unit='absolute_nm'),),
        correlation_layer_ids=('low-film',), correlation_matrix=((1.,),))
    service.materials.records['low']['optical_sets'][0]['k'] = [.01, .03, .05]
    result = table.execute_film_thickness_error_table_request(service, request)
    assert result.state == 'completed', result.message
    assert result.analysis is not None
    assert result.analysis.material_k_policy == 'source_k_required'
    frozen = result.frozen_request.frozen_material_snapshot
    assert frozen is not None
    settings = frozen['settings']
    np.testing.assert_allclose([r.nominal_value for r in result.analysis.conditions],
        direct_spectrum(service, request.design_snapshot, settings), atol=2e-14)
    changed = deepcopy(request.design_snapshot)
    changed['front_layers'][0]['thickness']['value'] *= 1.02
    np.testing.assert_allclose(required_values([r.finite_bias_value for r in result.analysis.conditions]),
        direct_spectrum(service, changed, settings), atol=2e-14)
    values = []
    for sign in (-1., 1.):
        changed = deepcopy(request.design_snapshot)
        changed['front_layers'][0]['thickness']['value'] += sign * .001
        values.append(np.asarray(direct_spectrum(service, changed, settings)))
    np.testing.assert_allclose(required_values([r.aggregate_sigma for r in result.analysis.conditions]),
        np.abs((values[1] - values[0]) / .002), rtol=2e-7, atol=2e-12)
    ignored = table.execute_film_thickness_error_table_request(service,
        replace(request, design_snapshot={**request.design_snapshot, 'use_k': False}))
    assert ignored.analysis is not None
    assert max(abs(a.nominal_value - b.nominal_value) for a, b in
        zip(result.analysis.conditions, ignored.analysis.conditions, strict=True)) > .001
    assert ignored.analysis.material_k_policy == 'explicit_use_k_false_idealization'
    payload = table.film_thickness_error_table_result_to_payload(result)
    table.validate_film_thickness_error_table_result_payload(payload)
    payload['analysis']['material_k_policy'] = 'missing_source_k_zero_known_source_k_required'
    with pytest.raises(ValueError, match='material-k policy'):
        table.validate_film_thickness_error_table_result_payload(payload, replay=False)


@pytest.mark.parametrize('defect', ['k_domain', 'negative_k', 'nan_k', 'bad_k_data', 'n_domain', 'invalid_n'])
@pytest.mark.parametrize('finite_bias', [False, True])
def test_invalid_or_partial_sources_still_fail_before_forward_work(defect, finite_bias, monkeypatch):
    service, request = problem(low=500., high=600., samples=5, include_finite_bias=finite_bias)
    optical = service.materials.records['low']['optical_sets'][0]
    if defect in ('k_domain', 'bad_k_data'):
        for key in ('n', 'k', 'wavelengths'):
            optical.pop(key)
        optical.update(kind='cauchy', parameters=dict(A=float(np.sqrt(1.5)), B=0., C=0.))
        optical['k_data'] = dict(wavelength_unit='nm', wavelength_min=520., wavelength_max=580.,
            wavelengths=[520., 580.], k=[0., -.01] if defect == 'bad_k_data' else [.01, .02])
    elif defect in ('negative_k', 'nan_k'):
        optical['k'][1] = -.01 if defect == 'negative_k' else float('nan')
    elif defect == 'n_domain':
        optical.update(wavelength_min=520., wavelength_max=580.)
        optical['k'] = None
    else:
        optical['n'][1] = -1.
        optical['k'] = None
    def forbidden(*args, **kwargs):
        pytest.fail('invalid source must fail before nominal Jacobian or finite-bias work')
    monkeypatch.setattr(table, 'simulate_with_thickness_jacobian', forbidden)
    with pytest.raises(ValueError):
        table.freeze_film_thickness_error_table_request(service, request)


def test_physical_working_set_counts_full_topology_before_any_allocation(monkeypatch):
    service, request = problem(low=550., high=551., samples=4096)
    design = deepcopy(request.design_snapshot)
    template = design['front_layers'][0]
    design['front_layers'] = [{**deepcopy(template), 'id': f'layer-{index}'} for index in range(4096)]
    request = replace(request, design_snapshot=design,
        rows=(table.FilmThicknessErrorParameter('layer-0', bias=1., bias_unit='absolute_nm'),))
    estimate = table.estimate_film_thickness_error_table_resources(request)
    # Even one derivative column cannot hide the full physical layer arrays.
    assert estimate['estimated_working_bytes'] >= 4096 * 4096 * 80
    assert estimate['estimated_working_bytes'] > table.MAX_WORKING_BYTES
    def forbidden(*args, **kwargs):
        pytest.fail('oversized physical working set must reject before freeze or kernel allocation')
    monkeypatch.setattr(service, 'freeze_design', forbidden)
    monkeypatch.setattr(table, 'simulate_with_thickness_jacobian', forbidden)
    with pytest.raises(ValueError, match='no samples or layers were dropped'):
        table.execute_film_thickness_error_table_request(service, request)


@pytest.mark.parametrize('selected_count', [1, 89])
def test_89_layer_resource_policy_remains_usable_without_changing_grid_or_columns(selected_count):
    _, request = problem(low=550., high=551., samples=4096)
    design = deepcopy(request.design_snapshot)
    template = design['front_layers'][0]
    design['front_layers'] = [{**deepcopy(template), 'id': f'layer-{index}'} for index in range(89)]
    request = replace(request, design_snapshot=design,
        rows=tuple(table.FilmThicknessErrorParameter(f'layer-{index}', bias=1., bias_unit='absolute_nm')
                   for index in range(selected_count)))
    estimate = table.estimate_film_thickness_error_table_resources(request)
    assert estimate['physical_layer_count'] == 89
    assert estimate['max_chunk_samples'] == 4096
    assert estimate['condition_count'] == 4096
    assert estimate['derivative_column_count'] == selected_count
    expected_physical = 4096 * (128 * (89 + 2) + 128 * selected_count + 4096)
    assert estimate['physical_forward_working_bytes'] == expected_physical
    assert estimate['estimated_working_bytes'] >= expected_physical
    assert estimate['estimated_working_bytes'] <= table.MAX_WORKING_BYTES
    assert estimate['estimated_retained_result_bytes'] <= table.MAX_RETAINED_RESULT_BYTES


def test_resource_max_chunk_is_per_forward_not_sum_of_point_objectives():
    _, request = problem()
    request = replace(request, objectives=request.objectives * 10)
    estimate = table.estimate_film_thickness_error_table_resources(request)
    assert estimate['condition_count'] == 10
    assert estimate['max_chunk_samples'] == 1


def test_result_revision_bool_cannot_impersonate_integer_one_even_with_physical_replay():
    service, request = problem()
    result = table.execute_film_thickness_error_table_request(service, replace(request, design_revision=1))
    payload = table.film_thickness_error_table_result_to_payload(result)
    payload['design_revision'] = True
    with pytest.raises(ValueError, match='revision'):
        table.validate_film_thickness_error_table_result_payload(payload, replay=True)


@pytest.mark.parametrize('value', [complex(1., 0.), complex(1., 2.),
    np.complex64(1.), np.complex128(1.), np.complex128(1. + 2.j)])
def test_parameters_reject_all_complex_numbers_without_discarding_imaginary_parts(value):
    import warnings
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter('always')
        with pytest.raises(ValueError, match='finite'):
            problem(rows=(table.FilmThicknessErrorParameter('low-film', bias=value, bias_unit='absolute_nm'),))
    assert not captured, 'invalid complex input must reject, not warn and discard its imaginary component'


@pytest.mark.parametrize('magnitude', [1e-200, 1e200])
def test_sigma_scale_avoids_false_zero_and_overflow_for_representable_results(monkeypatch, magnitude):
    import math
    fake_jacobian(monkeypatch, columns=(1.,))
    _, _, result = run(rows=(table.FilmThicknessErrorParameter('low-film', uncertainty_kind='stddev',
        uncertainty_magnitude=magnitude, uncertainty_unit='absolute_nm'),),
        correlation_layer_ids=('low-film',), correlation_matrix=((1.,),))
    sigma = result.analysis.conditions[0].aggregate_sigma
    assert sigma is not None and sigma > 0
    assert math.isclose(sigma, magnitude, rel_tol=2e-15, abs_tol=0.)
    assert result.analysis.layer_summaries[0].max_stddev_contribution == magnitude


@pytest.mark.parametrize('magnitude', [1e-200, 1e200])
@pytest.mark.parametrize('correlation,expected_multiplier', [(1., 2.), (-1., 0.)])
def test_sigma_extreme_scale_full_correlation_and_exact_anticorrelation(monkeypatch, magnitude, correlation, expected_multiplier):
    import math
    fake_jacobian(monkeypatch, columns=(1., 1.))
    rows = tuple(replace(row, uncertainty_magnitude=magnitude) for row in sigma_rows())
    _, _, result = run(two=True, rows=rows, correlation_layer_ids=('low-film', 'high-film'),
        correlation_matrix=((1., correlation), (correlation, 1.)))
    sigma = result.analysis.conditions[0].aggregate_sigma
    assert sigma is not None
    assert result.analysis.conditions[0].aggregate_sigma_status == 'available'
    assert math.isclose(sigma, expected_multiplier * magnitude, rel_tol=2e-15, abs_tol=0.)


@pytest.mark.parametrize('magnitude', [1e-200, 1e200])
def test_scaled_sigma_matches_high_precision_matrix_oracle(monkeypatch, magnitude):
    from decimal import Decimal, localcontext
    import math
    fake_jacobian(monkeypatch, columns=(2., -3.))
    rows = tuple(replace(row, uncertainty_magnitude=magnitude) for row in sigma_rows())
    matrix = ((1., .25), (.25, 1.))
    _, _, result = run(two=True, rows=rows, correlation_layer_ids=('low-film', 'high-film'),
                       correlation_matrix=matrix)
    with localcontext() as context:
        context.prec = 100
        vector = [Decimal.from_float(j) * Decimal.from_float(magnitude) for j in (2., -3.)]
        q = sum((vector[i] * Decimal.from_float(matrix[i][j]) * vector[j]
                 for i in range(2) for j in range(2)), Decimal(0))
        oracle = float(q.sqrt())
    sigma = result.analysis.conditions[0].aggregate_sigma
    assert sigma is not None and sigma > 0
    assert math.isclose(sigma, oracle, rel_tol=3e-15, abs_tol=0.)


@pytest.mark.parametrize('derivative,magnitude', [(1e200, 1e200), (1e-200, 1e-200)])
def test_unrepresentable_nonzero_sigma_output_fails_explicitly(monkeypatch, derivative, magnitude):
    fake_jacobian(monkeypatch, columns=(derivative,))
    service, request = problem(rows=(table.FilmThicknessErrorParameter('low-film', uncertainty_kind='stddev',
        uncertainty_magnitude=magnitude, uncertainty_unit='absolute_nm'),),
        correlation_layer_ids=('low-film',), correlation_matrix=((1.,),))
    result = table.execute_film_thickness_error_table_request(service, request)
    assert result.state == 'failed'
    assert result.analysis is None
    assert 'represent' in result.message


def test_exact_nonbinary_original_grid_reaches_kernel_and_strict_boundary(monkeypatch):
    service, request = problem(low=500., high=600., samples=4098)
    frozen = table.freeze_film_thickness_error_table_request(service, request)
    original = np.linspace(500., 600., 4098)
    wave = original[163]
    physical = table.simulate_with_thickness_jacobian(frozen.design_snapshot, frozen.frozen_material_snapshot, (),
        dict(wavelength_min_nm=float(wave), wavelength_max_nm=float(wave), samples=1,
             angle_deg=0., polarization='unpolarized', first_surface='front'),
        wavelengths_nm=original[163:164])
    exact_value = float(physical['R'][0])
    # The supported legacy default reconstructs a different non-binary grid.
    # It genuinely falls below this strict upper target, unlike the exact wave.
    rebuilt = table.simulate_with_thickness_jacobian(frozen.design_snapshot, frozen.frozen_material_snapshot, (),
        dict(wavelength_min_nm=float(original[0]), wavelength_max_nm=float(original[4095]),
             samples=4096, angle_deg=0., polarization='unpolarized', first_surface='front'))
    assert rebuilt['wavelength_nm'][163] == 503.97852086892846
    assert rebuilt['R'][163] < exact_value
    request = replace(frozen, objectives=(replace(frozen.objectives[0], pointwise=replace(
        frozen.objectives[0].pointwise, upper_bound=exact_value, strict_upper=True)),))
    kernel = table.simulate_with_thickness_jacobian
    arrays = []
    def spy(design, materials, variables, settings, **kwargs):
        assert 'wavelengths_nm' in kwargs, 'full original grid slices must reach the physical kernel'
        arrays.append(np.array(kwargs['wavelengths_nm'], copy=True))
        return kernel(design, materials, variables, settings, **kwargs)
    monkeypatch.setattr(table, 'simulate_with_thickness_jacobian', spy)
    result = table.execute_film_thickness_error_table_request(None, request)
    assert result.state == 'completed', result.message
    assert result.analysis is not None
    np.testing.assert_array_equal(np.concatenate(arrays), original)
    condition = result.analysis.conditions[163]
    assert condition.wavelength_nm == 503.9785208689285
    assert condition.nominal_value == exact_value
    assert condition.nominal_condition_pass is False
    table.validate_film_thickness_error_table_result_payload(table.film_thickness_error_table_result_to_payload(result))


def test_kernel_returned_grid_must_exactly_match_requested_grid(monkeypatch):
    service, request = problem()
    def wrong_grid(design, materials, variables, settings, **kwargs):
        return dict(R=np.array([.2]), T=np.array([.8]), jacobian_R=np.array([[1.]]),
                    jacobian_T=np.array([[-1.]]), wavelength_nm=np.array([np.nextafter(550., np.inf)]))
    monkeypatch.setattr(table, 'simulate_with_thickness_jacobian', wrong_grid)
    result = table.execute_film_thickness_error_table_request(service, request)
    assert result.state == 'failed'
    assert result.analysis is None
    assert result.failed_evaluations == 1
    assert 'wavelength' in result.message


@pytest.mark.parametrize('offset,expected_status', [(0., 'available'), (-1e-15, 'numerical_indeterminate')])
def test_scaled_sigma_preserves_psd_boundary_and_negative_q_without_matrix_repair(monkeypatch, offset, expected_status):
    from decimal import Decimal, localcontext
    fake_jacobian(monkeypatch, columns=(1., 1., 1.))
    service, request = problem(two=True, rows=sigma_rows())
    design = deepcopy(request.design_snapshot)
    third = deepcopy(design['front_layers'][0])
    third['id'] = 'third-film'
    design['front_layers'].append(third)
    rows = tuple(replace(row, uncertainty_magnitude=1e200) for row in sigma_rows()) + (
        replace(sigma_rows()[0], layer_id='third-film', uncertainty_magnitude=1e200),)
    r = -.5 + offset
    matrix = ((1., r, r), (r, 1., r), (r, r, 1.))
    request = replace(request, design_snapshot=design, rows=rows,
        correlation_layer_ids=('low-film', 'high-film', 'third-film'), correlation_matrix=matrix)
    result = table.execute_film_thickness_error_table_request(service, request)
    assert result.state == 'completed', result.message
    assert result.analysis is not None
    correlation = result.analysis.correlation
    assert correlation.matrix == matrix and correlation.matrix_unmodified is True
    condition = result.analysis.conditions[0]
    assert condition.aggregate_sigma_status == expected_status
    with localcontext() as context:
        context.prec = 1000
        vector = [Decimal.from_float(1e200)] * 3
        oracle_q = sum((vector[i] * Decimal.from_float(matrix[i][j]) * vector[j]
                        for i in range(3) for j in range(3)), Decimal(0))
    if offset == 0.:
        assert oracle_q == 0
        assert condition.aggregate_sigma == 0.
    else:
        assert oracle_q < 0
        assert condition.aggregate_sigma is None
        assert result.analysis.aggregate_sigma_status == 'partial'
        assert correlation.status == 'psd_numerically_uncertain'
