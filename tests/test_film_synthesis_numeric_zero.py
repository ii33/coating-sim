"""Numerical phase zeros are distinct from real sub-nm layers and user bounds."""
from copy import deepcopy
from dataclasses import replace
from threading import Event

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_synthesis_numeric_zero import numeric_zero_cleanup
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def prepared():
    service, base = setup_problem(upper=0., low=500., high=600., samples=5)
    req = synthesis.freeze_film_synthesis_request(service, replace(base, search_strategy='hybrid',
        max_cycles=1, max_evaluations=80, warm_start_seed_index=0,
        seed_designs=(stack(base.environment),)))
    return service, req


def stack(environment):
    design = deepcopy(dict(environment))
    design['front_layers'] = [dict(id=f'layer-{i}', material_id=material, optical_set_id='nk',
        thickness=dict(value=d, unit='nm')) for i, (material, d) in enumerate([
        ('low', 40.), ('high', np.finfo(float).tiny), ('low', 30.),
        ('high', 1e-15), ('high', .7), ('low', 60.)])]
    return design


def inject_non_equivalent_cleanup(monkeypatch):
    """Fault-inject a physically different cleaned stack; keep real TMM scoring."""
    from coat_sim.core import film_synthesis_numeric_zero as numeric
    rejected = []
    def non_equivalent(design, frozen, limits, **kwargs):
        result = numeric_zero_cleanup(design, frozen, limits, **kwargs)
        if result is None:
            return None
        cleaned, threshold = result
        changed = deepcopy(cleaned.design)
        changed['front_layers'][0]['thickness']['value'] += 20.
        rejected.append((deepcopy(design), changed))
        return replace(cleaned, design=changed), threshold
    monkeypatch.setattr(numeric, 'numeric_zero_cleanup', non_equivalent)
    return rejected


def test_phase_zero_removal_preserves_real_thin_and_exact_optical_identity():
    _service, req = prepared()
    source = req.seed_designs[0]
    result = numeric_zero_cleanup(source, req.frozen_material_snapshot,
        synthesis._phase_step_limits_nm(req))
    assert result is not None
    cleaned, threshold = result
    assert 1e-15 < threshold < 1e-9
    assert set(cleaned.removed_near_zero_ids) == {'layer-1', 'layer-3'}
    assert cleaned.design['front_layers'][0]['thickness']['value'] == pytest.approx(70.)
    assert cleaned.design['front_layers'][1]['material_id'] == 'high'
    assert cleaned.design['front_layers'][1]['thickness']['value'] == .7
    assert source['front_layers'][4]['thickness']['value'] == .7


def test_cleanup_does_not_merge_past_explicit_layer_limit():
    _service, req = prepared()
    limited = numeric_zero_cleanup(req.seed_designs[0], req.frozen_material_snapshot,
        synthesis._phase_step_limits_nm(req), max_layer_thickness_nm=65.)
    assert limited is not None
    assert not limited[0].merged_ids
    assert [r['thickness']['value'] for r in limited[0].design['front_layers']] == [40., 30., .7, 60.]


def test_cleanup_preserves_distinct_optical_set_ids_even_with_equal_optics():
    _service, req = prepared()
    frozen = deepcopy(req.frozen_material_snapshot)
    low = next(row for row in frozen['materials'] if row['id'] == 'low')
    other_set = deepcopy(low['optical_sets'][0])
    other_set['id'] = 'other-nk'
    low['optical_sets'].append(other_set)
    design = deepcopy(req.seed_designs[0])
    design['front_layers'][2]['optical_set_id'] = 'other-nk'
    limits = synthesis._phase_step_limits_nm(req)
    limits[('low', 'other-nk')] = limits[('low', 'nk')]
    result = numeric_zero_cleanup(design, frozen, limits)
    assert result is not None
    rows = result[0].design['front_layers']
    assert [r['id'] for r in rows] == ['layer-0', 'layer-2', 'layer-4', 'layer-5']
    assert [r['optical_set_id'] for r in rows[:2]] == ['nk', 'other-nk']
    assert not result[0].merged_ids


def test_user_cancel_returns_only_rescored_clean_candidate_without_new_audit():
    service, req = prepared()
    cancel = Event()
    def progress(row):
        if row.stage == 'incumbent':
            cancel.set()
    result = synthesis.synthesize_film_design(service, req, cancel_event=cancel, progress_callback=progress)
    assert result.state == 'cancelled' and result.candidate_design is not None
    assert len(result.candidate_design['front_layers']) == 3
    assert all(r['thickness']['value'] > 1e-9 for r in result.candidate_design['front_layers'])
    assert result.evaluations == 2
    assert result.validation_evaluations == 0 and not result.validation_complete
    record = result.performance['search']['numeric_zero_cleanup'][0]
    assert record['training_rescored'] and record['full_original_spectral_equivalence']
    assert not record['independent_audit_performed']


def test_insufficient_budget_never_labels_raw_tiny_as_complete_candidate():
    service, req = prepared()
    result = synthesis.synthesize_film_design(service, replace(req, max_evaluations=2))
    assert result.candidate_design is None
    assert not result.validation_complete and not result.targets_met
    assert result.evaluations == 1
    assert result.performance['search']['numeric_zero_cleanup'][0]['status'].startswith('unpublished_')
    fallback = result.performance['search']['numeric_zero_fallback']
    assert not fallback['returned_training_complete']
    assert fallback['returned_physical_hash'] is None
    assert not fallback['new_independent_audit_claimed']


def test_cancel_reports_better_unpublished_trial_and_exact_clean_fallback():
    from coat_sim.core.film_synthesis_archive import physical_design_hash
    service, req = prepared()
    cancel = Event()
    def progress(row):
        if row.stage == 'structure_proposal' and row.layer_count:
            cancel.set()
    result = synthesis.synthesize_film_design(service,
        replace(req, warm_start_seed_index=None, max_evaluations=4),
        cancel_event=cancel, progress_callback=progress)
    assert result.state == 'cancelled'
    assert result.candidate_design is not None
    assert result.candidate_design['front_layers'] == []
    fallback = result.performance['search']['numeric_zero_fallback']
    assert fallback['unpublished_better_trials'] >= 1
    assert fallback['returned_physical_hash'] == physical_design_hash(result.candidate_design)
    assert fallback['returned_training_complete']
    assert not fallback['validation_matches_returned_candidate']
    assert not result.validation_complete and not result.targets_met
    assert result.validation_evaluations == 0
    assert '未完成清理复算' in result.message
    assert result.evaluations == 2


def test_positive_minimum_is_preserved():
    service, base = setup_problem(upper=.001)
    result = synthesis.synthesize_film_design(service, replace(base, search_strategy='hybrid',
        minimum_nonzero_thickness_nm=20., max_cycles=1, max_evaluations=80))
    assert result.candidate_design is not None
    assert all(r['thickness']['value'] >= 20. for r in result.candidate_design['front_layers'])


def test_normal_completion_audits_the_cleaned_candidate_it_returns():
    service, req = prepared()
    target = replace(req.objectives[0], pointwise=replace(req.objectives[0].pointwise, upper_bound=1.))
    result = synthesis.synthesize_film_design(service,
        replace(req, objectives=(target,), enable_thickness_reduction=False))
    assert result.targets_met and result.validation_complete
    assert result.candidate_design is not None
    assert all(r['thickness']['value'] > 1e-9 for r in result.candidate_design['front_layers'])
    assert result.training_score is not None and result.validation_score is not None
    assert result.training_score['design'] == result.candidate_design
    assert result.validation_score['design'] == result.candidate_design


@pytest.mark.parametrize('warm_start', [False, True])
def test_non_equivalent_cleanup_is_rejected_and_other_topology_still_completes(monkeypatch, warm_start):
    from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    service, req = prepared()
    good = deepcopy(dict(req.environment))
    good['front_layers'] = [dict(id='valid-clean-seed', material_id='low', optical_set_id='nk',
        thickness=dict(value=550. / (4 * np.sqrt(1.5)), unit='nm'))]
    target = replace(req.objectives[0], pointwise=replace(req.objectives[0].pointwise, upper_bound=.004))
    req = synthesis.freeze_film_synthesis_request(service, replace(req, objectives=(target,),
        candidate_materials=(FilmNeedleMaterialReference('high', 'nk'),),
        seed_designs=(req.seed_designs[0], good), warm_start_seed_index=0 if warm_start else None,
        enable_thickness_reduction=False))
    rejected = inject_non_equivalent_cleanup(monkeypatch)
    result = synthesis.synthesize_film_design(service, req)
    assert result.state == 'completed' and result.targets_met and result.validation_complete
    assert result.candidate_design is not None
    assert [r['id'] for r in result.candidate_design['front_layers']] == ['valid-clean-seed']
    assert result.failed_evaluations == 0
    assert req.max_evaluations is not None
    assert result.evaluations <= req.max_evaluations
    records = result.performance['search']['numeric_zero_cleanup']
    assert records and all(row['status'] == 'unpublished_spectral_mismatch' for row in records)
    assert all(row['training_rescored'] and not row['full_original_spectral_equivalence'] for row in records)
    assert rejected
    original = evaluate_frozen_structure_candidate(synthesis._proxy(req), rejected[0][0])
    changed = evaluate_frozen_structure_candidate(synthesis._proxy(req), rejected[0][1])
    a, b = original['responses'][0], changed['responses'][0]
    assert np.array_equal(a['wavelength_nm'], b['wavelength_nm'])
    assert len(a['actual_values']) == 5
    assert np.max(np.abs(np.asarray(a['actual_values']) - np.asarray(b['actual_values']))) > 1e-4
    assert result.training_score is not None and result.validation_score is not None
    assert result.training_score['design'] == result.validation_score['design'] == result.candidate_design
    # Explicit seed references outside the insertion pool are frozen and have
    # physical phase scales. Unsupported layered environments are tested below.
    assert ('low', 'nk') in synthesis._phase_step_limits_nm(req)


def test_non_equivalent_cleanup_keeps_complete_champion_when_user_cancels(monkeypatch):
    from coat_sim.core.film_synthesis_archive import physical_design_hash
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    service, req = prepared()
    inject_non_equivalent_cleanup(monkeypatch)
    cancel = Event()
    cancellation_boundary = []
    def progress(row):
        if row.stage == 'structure_proposal' and row.layer_count == 6:
            cancellation_boundary.append(row.evaluations)
            cancel.set()
    result = synthesis.synthesize_film_design(service,
        replace(req, warm_start_seed_index=None,
            candidate_materials=(FilmNeedleMaterialReference('high', 'nk'),)),
        cancel_event=cancel, progress_callback=progress)
    assert result.state == 'cancelled' and result.failed_evaluations == 0
    assert result.candidate_design is not None and result.candidate_design['front_layers'] == []
    assert cancellation_boundary and result.evaluations == cancellation_boundary[0]
    records = result.performance['search']['numeric_zero_cleanup']
    assert records[-1]['status'] == 'unpublished_spectral_mismatch', records
    assert result.evaluations >= 3 and result.validation_evaluations == 0
    batches = result.performance['search']['hybrid']['parallel_batches']
    assert sum(row['extra_unreserved_evaluations'] for row in batches) == 1
    assert all(0 <= row['consumed_slots'] <= row['reserved_slots'] for row in batches)
    assert all(row['dispatched_unused_slots'] >= 0 and row['released_unsubmitted_slots'] >= 0 for row in batches)
    assert not result.validation_complete and not result.targets_met
    fallback = result.performance['search']['numeric_zero_fallback']
    assert fallback['spectral_equivalence_rejections'] == 1
    assert fallback['returned_physical_hash'] == physical_design_hash(result.candidate_design)
    assert not fallback['validation_matches_returned_candidate']
    assert '未通过光谱等价检查' in result.message


def test_rejected_warm_seed_without_budget_reports_no_complete_candidate(monkeypatch):
    service, req = prepared()
    inject_non_equivalent_cleanup(monkeypatch)
    result = synthesis.synthesize_film_design(service, replace(req, max_evaluations=3))
    assert result.state == 'not_found' and result.termination_reason == 'evaluation_limit'
    assert result.evaluations == 2 and result.failed_evaluations == 0
    assert result.candidate_design is None and result.training_score is None
    assert not result.validation_complete and not result.targets_met
    fallback = result.performance['search']['numeric_zero_fallback']
    assert fallback['spectral_equivalence_rejections'] == 1
    assert fallback['returned_physical_hash'] is None


def test_cleanup_configuration_error_stays_an_explicit_numerical_failure(monkeypatch):
    from coat_sim.core import film_synthesis_numeric_zero as numeric
    service, req = prepared()
    def invalid_configuration(*_args, **_kwargs):
        raise ValueError('invalid cleanup configuration fixture')
    monkeypatch.setattr(numeric, 'numeric_zero_cleanup', invalid_configuration)
    result = synthesis.synthesize_film_design(service, req)
    assert result.state == 'failed'
    assert result.termination_reason == 'numerical_failure: invalid cleanup configuration fixture'
    assert result.candidate_design is None and not result.validation_complete


@pytest.mark.parametrize('error_type', [ValueError, ArithmeticError, RuntimeError])
def test_nested_cleanup_physical_failure_is_counted_once(monkeypatch, error_type):
    service, req = prepared()
    original = synthesis.evaluate_frozen_structure_candidate
    calls = []
    def physical(request, design, **kwargs):
        layers = len(design['front_layers'])
        calls.append(layers)
        if layers == 3:
            raise error_type('injected cleaned physical failure')
        return original(request, design, **kwargs)
    monkeypatch.setattr(synthesis, 'evaluate_frozen_structure_candidate', physical)
    result = synthesis.synthesize_film_design(service, req)
    assert calls == [6, 3]  # One successful raw call and one failed cleanup call.
    assert result.state == 'failed'
    assert result.termination_reason == 'numerical_failure: injected cleaned physical failure'
    assert result.evaluations == 2 and result.failed_evaluations == 1
    assert result.candidate_design is None and not result.validation_complete


@pytest.mark.parametrize('side', ['front_layers', 'back_layers'])
def test_layered_environment_is_rejected_before_freeze_or_phase_lookup(side, monkeypatch):
    service, req = prepared()
    environment = deepcopy(dict(req.environment))
    environment[side] = [deepcopy(req.seed_designs[0]['front_layers'][0])]
    request = replace(req, environment=environment, seed_designs=(), warm_start_seed_index=None)
    def unexpected_phase_lookup(*_args, **_kwargs):
        pytest.fail('invalid environment reached phase lookup')
    monkeypatch.setattr(synthesis, '_phase_step_limits_nm', unexpected_phase_lookup)
    for operation in (synthesis.freeze_film_synthesis_request, synthesis.synthesize_film_design):
        with pytest.raises(ValueError, match='bare substrate environment'):
            operation(service, request)
