"""Mixed-window scheduling keeps complete physics, resumable coverage and budgets."""
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event
from typing import Any

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core.film_nominal_acceptance import nominal_targets_pass
from coat_sim.core import film_synthesis_audit as audit
from coat_sim.core import film_synthesis_global as global_search
from coat_sim.core import film_synthesis_multispectral_minimax as minimax
from coat_sim.core import film_synthesis_multispectral_priors as priors
from coat_sim.core import film_synthesis_refinement as refinement
from coat_sim.core.film_optimization_structure import evaluate_frozen_structure_candidate
from tests.test_film_synthesis_hr_priors import hr_request

pytestmark = pytest.mark.algorithm


def mixed_request(**changes):
    request = hr_request(low=550., high=550., angles=(0.,), max_layers=8, absorbing=False)
    first = replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise, lower_bound=.99999), wavelength_samples=1)
    second = replace(first, wavelength_min_nm=560., wavelength_max_nm=560.)
    transmitted = replace(first, pointwise=replace(first.pointwise, lower_bound=None, upper_bound=.05), wavelength_min_nm=570., wavelength_max_nm=570., metric="T")
    return replace(request, objectives=(first, second, transmitted), cpu_workers=1,
                   max_evaluations=None, max_cycles=3, position_samples=1,
                   validation_samples_multiplier=2, **changes)


def proposal(request, name, *, layers=2, thickness=55., source=False):
    design = deepcopy(request.environment)
    design['front_layers'] = [dict(id=f'{name}-{index}',
        material_id=request.candidate_materials[index % 2].material_id,
        optical_set_id='nk', thickness=dict(value=thickness, unit='nm'), note='scheduling fixture')
        for index in range(layers)]
    metadata: dict[str, Any] = dict(family='multispectral_hr' if source else 'periodic', schedule_fixture=name,
                    high=(request.candidate_materials[1].material_id, 'nk'))
    if source:
        metadata.update(candidate_pair_budget=1, estimated_pair_demand=4)
    return global_search.StructuralProposal(design, metadata)


def install_streams(monkeypatch, request, *, old_layers=2, old_count=5):
    old = [proposal(request, f'classic-{index}', layers=old_layers, thickness=55.+index)
           for index in range(old_count)]
    new = proposal(request, 'window-aware', layers=2, thickness=61., source=True)
    generated, factories, closed = [], [], []
    def classic(frozen, *, epoch=0, cancel_check, **kwargs):
        factories.append(epoch)
        try:
            for index, item in enumerate(old):
                cancel_check()
                generated.append(index)
                yield item
        finally:
            closed.append(epoch)
    def window_aware(frozen, *, cancel_check, **kwargs):
        cancel_check()
        yield new
    monkeypatch.setattr(global_search, 'iter_reflector_proposals', classic)
    monkeypatch.setattr(priors, 'iter_multispectral_hr_proposals', window_aware)
    return old, new, generated, factories, closed


def assert_audit_identity(result):
    if result.validation_score is not None:
        assert result.validation_score['design'] == result.training_score['design']
        assert result.validation_score['design']['front_layers'] == result.candidate_design['front_layers']
        if 'validation_matches_candidate' in result.candidate_provenance:
            assert result.candidate_provenance['validation_matches_candidate']
    else:
        assert not result.validation_complete


def test_training_failed_bare_does_not_audit_before_any_structure(monkeypatch):
    request = mixed_request()
    cancel = Event()
    reached = []
    def classic(frozen, *, cancel_check, **kwargs):
        reached.append('classic preparation')
        cancel.set()
        cancel_check()
        yield
    def unexpected_audit(*args, **kwargs):
        raise AssertionError('an obvious bare training failure must not start a pre-search audit')
    monkeypatch.setattr(global_search, 'iter_reflector_proposals', classic)
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', unexpected_audit)
    result = synthesis.synthesize_film_design(None, request, cancel_event=cancel)
    assert reached == ['classic preparation']
    assert result.termination_reason == 'cancelled'
    assert result.evaluations == 1 and result.validation_evaluations == 0
    assert result.candidate_design is not None and result.training_score is not None
    assert result.candidate_design['front_layers'] == []
    assert result.training_score['max_error'] > request.acceptable_normalized_error
    assert not result.targets_met and result.validation_score is None
    assert result.performance['cleanup_complete']


def test_training_passed_bare_still_gets_full_independent_audit_and_strict_acceptance(monkeypatch):
    request = hr_request(low=550., high=550., angles=(0.,), absorbing=False)
    objective = replace(request.objectives[0], pointwise=replace(request.objectives[0].pointwise,
        lower_bound=.01, strict_lower=True), wavelength_samples=1)
    request = replace(request, objectives=(objective,), max_evaluations=3)
    original_audit = audit.audit_frozen_synthesis_candidate
    audited = []
    def checked(frozen, design, **kwargs):
        value = original_audit(frozen, design, **kwargs)
        audited.append(deepcopy(value))
        return value
    def unexpected_priors(*args, **kwargs):
        raise AssertionError('audited passing bare substrate should finish before proposal construction')
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', checked)
    monkeypatch.setattr(global_search, 'iter_reflector_proposals', unexpected_priors)
    result = synthesis.synthesize_film_design(None, request)
    assert result.targets_met and result.validation_complete and result.state == 'completed'
    assert result.evaluations == 2 and result.validation_evaluations == 1
    assert len(audited) == 1 and audited[0]['design']['front_layers'] == []
    assert audited[0]['audit']['physical_evaluations'] > 0
    assert nominal_targets_pass(request, result.validation_score)
    assert_audit_identity(result)


def test_bare_on_strict_lower_boundary_cannot_gain_acceptance_or_presearch_audit(monkeypatch):
    request = hr_request(low=550., high=550., angles=(0.,), absorbing=False)
    objective = replace(request.objectives[0], wavelength_samples=1)
    request = replace(request, objectives=(objective,))
    raw = evaluate_frozen_structure_candidate(synthesis._proxy(request), request.environment,
                                              with_jacobian=False)
    objective = replace(objective, pointwise=replace(objective.pointwise, lower_bound=raw['score']['errors'][0].actual))
    objective = replace(objective, pointwise=replace(objective.pointwise, strict_lower=True))
    request = replace(request, objectives=(objective,))
    cancel = Event()
    def classic(frozen, *, cancel_check, **kwargs):
        cancel.set()
        cancel_check()
        yield
    monkeypatch.setattr(global_search, 'iter_reflector_proposals', classic)
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate',
                        lambda *a, **k: pytest.fail('strict boundary cannot trigger a pre-search audit'))
    result = synthesis.synthesize_film_design(None, request, cancel_event=cancel)
    assert result.training_score is not None
    assert result.training_score['max_error'] == 0.
    assert not result.targets_met and result.validation_score is None
    assert result.evaluations == 1 and result.validation_evaluations == 0


@pytest.mark.parametrize('quantum,expected_first,expected_screened', [(10, 'window-aware-', 1),
                                                                    (1000, 'classic-', 5)])
def test_classic_quantum_reaches_real_joint_refiner_and_orders_large_vs_small_work(
        monkeypatch, quantum, expected_first, expected_screened):
    assert synthesis.STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM == 1_000_000
    request = mixed_request()
    old, _, _, _, closed = install_streams(monkeypatch, request, old_layers=8)
    monkeypatch.setattr(synthesis, 'STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM', quantum)
    original_refine = minimax.refine_multispectral_minimax
    cancel = Event()
    refined = []
    stages = []
    def one_real_stage(frozen, initial, *, cancel_check, **kwargs):
        kwargs['max_iterations'] = 1
        best, details = original_refine(frozen, initial, cancel_check=cancel_check, **kwargs)
        refined.append((initial['design'], best, details))
        cancel.set()
        cancel_check()
        raise AssertionError('parent cancellation must leave the real refiner')
    monkeypatch.setattr(minimax, 'refine_multispectral_minimax', one_real_stage)
    result = synthesis.synthesize_film_design(None, request, cancel_event=cancel,
        progress_callback=lambda item: stages.append(item.stage))
    assert len(refined) == 1
    assert refined[0][0]['front_layers'][0]['id'].startswith(expected_first)
    assert refined[0][2]['physical_candidates'] > 0
    assert stages.count('structure_proposal') == expected_screened
    assert result.performance['search']['multispectral_start']['source_first'] == (quantum == 10)
    assert result.termination_reason == 'cancelled' and not result.targets_met
    assert result.validation_evaluations == 0
    assert result.performance['cleanup_complete'] and result.performance['unknown_tasks'] == 0
    if quantum == 10:
        schedule = result.performance['search']['multispectral_structure_schedule']
        assert schedule['admitted_candidates'] == 1  # One whole candidate may exceed the quantum.
        assert schedule['estimated_admitted_point_layer_work'] == 3*len(old[0].design['front_layers'])
        assert schedule['yielded'] and schedule['pending']
    assert closed == [0]
    assert_audit_identity(result)


@pytest.mark.parametrize('quantum,initial_candidates', [(10, 1), (15, 2)])
def test_deferred_classic_iterator_resumes_without_repeating_or_dropping_candidates(
        monkeypatch, quantum, initial_candidates):
    request = mixed_request()
    _, _, generated, factories, closed = install_streams(monkeypatch, request)
    monkeypatch.setattr(synthesis, 'STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM', quantum)
    monkeypatch.setattr(minimax, 'refine_multispectral_minimax',
                        lambda frozen, value, **kwargs: (value, {'method': 'schedule isolation'}))
    monkeypatch.setattr(refinement, 'refine_synthesis_candidate',
                        lambda frozen, value, **kwargs: (value, {'method': 'schedule isolation'}))
    monkeypatch.setattr(synthesis, '_site_records', lambda *args, **kwargs: ())
    original_feasible = synthesis._feasible
    # Isolate resumption from independent insertion work; every retained prior
    # still uses the real physical feasibility check and optical evaluator.
    monkeypatch.setattr(synthesis, '_feasible', lambda frozen, design:
        original_feasible(frozen, design) and not any(
            row['id'].startswith('growth-') for row in design['front_layers']))
    class EmptyPopulation:
        def __init__(self, initial, *args, **kwargs):
            self.dimensions = len(initial)
            self.generations = 0
        def proposals(self):
            return np.empty((0, self.dimensions))
        def accept(self, losses):
            assert len(losses) == 0
            self.generations += 1
    monkeypatch.setattr(global_search, 'DifferentialEvolutionProposals', EmptyPopulation)
    cancel = Event()
    schedules, scored = [], []
    def observe(item):
        if item.stage in ('structure_scan_yield', 'structure_scan_complete'):
            schedules.append(json.loads(item.message))
        elif item.stage == 'structure_proposal':
            scored.append(json.loads(item.message)['schedule_fixture'])
            if len(scored) == 5:
                cancel.set()
    result = synthesis.synthesize_film_design(None, request, cancel_event=cancel, progress_callback=observe)
    assert result.termination_reason == 'cancelled'
    assert factories == [0] and generated == list(range(5)) and closed == [0]
    assert scored == [f'classic-{index}' for index in range(5)]
    assert len(schedules) >= 2
    assert schedules[0]['point_layer_work_quantum'] == quantum
    assert schedules[0]['admitted_candidates'] == initial_candidates
    assert schedules[0]['estimated_admitted_point_layer_work'] == 6*initial_candidates
    assert schedules[0]['yielded'] and schedules[0]['pending_candidate']
    assert schedules[-1]['requested_epoch'] == 1 and schedules[-1]['active_epoch'] == 0
    assert schedules[-1]['point_layer_work_quantum'] is None
    assert result.validation_evaluations <= 1
    assert result.performance['cleanup_complete'] and result.performance['unknown_tasks'] == 0
    assert_audit_identity(result)


@pytest.mark.parametrize('stop_mode', ['cancel', 'time_limit', 'evaluation_limit'])
def test_yielded_scan_and_joint_refiner_share_parent_stop_and_candidate_identity(monkeypatch, stop_mode):
    request = mixed_request()
    if stop_mode == 'evaluation_limit':
        request = replace(request, max_evaluations=7)
    clock = [0.]
    if stop_mode == 'time_limit':
        request = replace(request, max_seconds=1.)
        monkeypatch.setattr(synthesis, 'monotonic', lambda: clock[0])
    install_streams(monkeypatch, request, old_layers=8)
    monkeypatch.setattr(synthesis, 'STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM', 10)
    cancel = Event()
    completed, refinements = [], []
    def stop(frozen, initial, *, evaluate, archive, cancel_check, **kwargs):
        refinements.append(initial)
        for index in range(20):
            cancel_check()
            trial = deepcopy(initial['design'])
            trial['front_layers'][0]['thickness']['value'] *= 1.+.001*(index+1)
            value = evaluate(trial, jacobian=True)
            completed.append(value)
            archive(value, audit_eligible=False)
            if stop_mode == 'cancel':
                cancel.set()
            elif stop_mode == 'time_limit':
                clock[0] = 2.
            cancel_check()
        raise AssertionError('the parent boundary must stop joint refinement')
    monkeypatch.setattr(minimax, 'refine_multispectral_minimax', stop)
    result = synthesis.synthesize_film_design(None, request, cancel_event=cancel)
    assert refinements and completed
    assert result.termination_reason == ('cancelled' if stop_mode == 'cancel' else stop_mode)
    assert result.state != 'failed' and not result.targets_met
    assert result.training_score is not None and result.candidate_design is not None
    assert result.performance['cleanup_complete'] and result.performance['unknown_tasks'] == 0
    assert result.performance['search']['multispectral_structure_schedule']['pending']
    if stop_mode == 'evaluation_limit':
        assert len(completed) == 3
        assert result.evaluations == 7 and result.validation_evaluations == 1
    else:
        assert len(completed) == 1
        assert result.evaluations == 4 and result.validation_evaluations == 0
    assert_audit_identity(result)


def install_many_sources(monkeypatch, request, *, count=5, layers=2):
    sources = [proposal(request, f'source-batch-{index}', layers=layers,
                        thickness=61.+index, source=True) for index in range(count)]
    generated, closed = [], []
    def source(frozen, *, cancel_check, **kwargs):
        try:
            for index, item in enumerate(sources):
                cancel_check()
                generated.append(index)
                yield item
        finally:
            closed.append(True)
    monkeypatch.setattr(priors, 'iter_multispectral_hr_proposals', source)
    return sources, generated, closed


def test_source_screen_yields_to_real_joint_refinement_before_enumeration_finishes(monkeypatch):
    request = mixed_request()
    old, _, _, _, _ = install_streams(monkeypatch, request, old_layers=8)
    for item in old:
        for layer in item.design['front_layers']:
            layer['thickness']['value'] = 1.  # Keep the old control below the source topology.
    sources, generated, closed = install_many_sources(monkeypatch, request, layers=8)
    monkeypatch.setattr(synthesis, 'STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM', 10)
    original_refine = minimax.refine_multispectral_minimax
    cancel = Event()
    completed, entered = [], []
    def actual_joint(frozen, initial, *, evaluate, cancel_check, **kwargs):
        entered.append(initial['design'])
        assert generated == [0] and len(generated) < len(sources)
        assert initial['design'] == sources[0].design
        def physical(*args, **options):
            value = evaluate(*args, **options)
            completed.append(value)
            return value
        kwargs['max_iterations'] = 1
        best, details = original_refine(frozen, initial, evaluate=physical,
                                       cancel_check=cancel_check, **kwargs)
        assert details['physical_candidates'] > 0 and completed
        completed.append(best)
        cancel.set()
        cancel_check()
        raise AssertionError('the parent must preserve the completed joint trial at cancellation')
    monkeypatch.setattr(minimax, 'refine_multispectral_minimax', actual_joint)
    result = synthesis.synthesize_film_design(None, request, cancel_event=cancel)
    assert len(entered) == 1 and generated == [0] and closed == [True]
    assert result.termination_reason == 'cancelled' and result.validation_evaluations == 0
    assert result.training_score is not None and result.candidate_design is not None
    assert result.training_score['design'] in [value['design'] for value in completed]
    assert result.candidate_provenance['training_complete']
    assert not result.targets_met and not result.validation_complete
    batches = result.performance['search']['multispectral_start']['screening_batches']
    assert len(batches) == 1 and batches[0]['admitted_candidates'] == 1
    assert not batches[0]['source_exhausted']
    assert result.performance['cleanup_complete']
    assert_audit_identity(result)


def test_source_pending_candidates_resume_across_batches_without_loss_or_duplicates(monkeypatch):
    request = mixed_request()
    install_streams(monkeypatch, request, old_layers=8)
    sources, generated, closed = install_many_sources(monkeypatch, request)
    monkeypatch.setattr(synthesis, 'STRUCTURAL_PREFIX_POINT_LAYER_QUANTUM', 10)
    cancel = Event()
    refined, observed_generation, scored, batch_records = [], [], [], []
    def refine_one(frozen, initial, *, cancel_check, **kwargs):
        refined.append(initial['design']['front_layers'][0]['id'])
        observed_generation.append(list(generated))
        return initial, {'method': 'source resumption isolation'}
    def observe(item):
        if item.stage == 'multispectral_prior':
            # The parent completed and archived this exact source candidate.
            scored.append(item.evaluations)
        elif item.stage in ('multispectral_screen_yield', 'multispectral_screen_complete'):
            batch_records.append(json.loads(item.message))
    def stop_after_source_control(frozen, initial, *, cancel_check, **kwargs):
        if initial['design']['front_layers'][0]['id'].startswith('classic-'):
            cancel.set()
            cancel_check()
        return refine_one(frozen, initial, cancel_check=cancel_check, **kwargs)
    monkeypatch.setattr(minimax, 'refine_multispectral_minimax', stop_after_source_control)
    result = synthesis.synthesize_film_design(None, request, cancel_event=cancel, progress_callback=observe)
    assert result.termination_reason == 'cancelled'
    assert generated == list(range(5)) and closed == [True]
    assert refined == [item.design['front_layers'][0]['id'] for item in sources]
    assert len(scored) == len(sources) and len(set(scored)) == len(sources)
    # Every source costs six points*layers. Each ten-unit batch keeps its
    # prefetched next source, refines one complete candidate, then resumes it.
    assert observed_generation[0] == [0, 1]
    assert len(observed_generation[0]) < len(sources)
    assert [row['admitted_candidates'] for row in batch_records] == [1]*5
    assert all(row['estimated_admitted_point_layer_work'] == 6 for row in batch_records)
    assert all(row['pending_candidate'] for row in batch_records[:-1])
    assert batch_records[-1]['source_exhausted'] and not batch_records[-1]['pending_candidate']
    assert result.performance['search']['multispectral_start']['screened'] == len(sources)
    assert result.validation_evaluations == 0 and not result.targets_met
    assert result.performance['cleanup_complete']
    assert_audit_identity(result)
