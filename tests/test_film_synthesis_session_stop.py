"""One local synthesis session may finish without satisfying its optical goals."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, FilmSynthesisResult, synthesize_film_design
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint, reduce_objective_responses
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
from coat_sim.core.film_optimization_scoring import reduction_score
from coat_sim.core.film_synthesis_archive import physical_design_hash
from coat_sim.core.film_synthesis_continuation import (
    continue_synthesis_request, load_search_state, validate_search_state,
)
from tests.test_film_auto_synthesis import Service, setup_problem

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def unmet_problem(**changes: Any) -> tuple[Service, FilmSynthesisRequest]:
    """A real one-layer broadband stationary point misses this strict R bound.

    The explicit test limits are guards against the old infinite-search policy,
    not production defaults. They leave hundreds of unused physical calls.
    """
    service, request = setup_problem(upper=1e-6, materials=('low',))
    seed = deepcopy(dict(request.environment))
    seed['front_layers'] = [dict(id='seed-low', material_id='low', optical_set_id='nk',
                               thickness=dict(value=80., unit='nm'))]
    options = dict(search_strategy='hybrid', seed_designs=(seed,), cpu_workers=1,
                   parallel_batch_size=1, max_layers=1, max_layer_thickness_nm=180.,
                   max_cycles=1, max_evaluations=500)
    options.update(changes)
    return service, replace(request, **options)


def complete_evidence(result: FilmSynthesisResult) -> None:
    assert result.candidate_design is not None
    assert result.training_score is not None and result.validation_score is not None
    assert result.validation_complete
    identity = physical_design_hash(result.candidate_design)
    assert physical_design_hash(result.training_score['design']) == identity
    assert physical_design_hash(result.validation_score['design']) == identity
    assert all(row.reduction == 'pointwise' for row in result.training_score['errors'])
    assert all(row.reduction == 'pointwise' for row in result.validation_score['errors'])


def explorable_problem(seed: int) -> tuple[Service, FilmSynthesisRequest]:
    return unmet_problem(max_layers=4,
        candidate_materials=tuple(FilmNeedleMaterialReference(material, 'nk') for material in ('low', 'high')),
        randomize_template_search=True, structure_search_seed=seed)


def test_real_unmet_stationary_structure_finishes_one_session_before_the_test_limits():
    service, request = unmet_problem()
    result = synthesize_film_design(service, request)
    assert result.state == 'completed', result.termination_reason
    assert result.termination_reason == 'local_session_converged_targets_unmet'
    assert not result.targets_met
    complete_evidence(result)
    assert result.training_score is not None and result.validation_score is not None
    assert result.training_score['nominal_pass'] is False
    assert result.validation_score['nominal_pass'] is False
    assert result.validation_score['max_error'] > 0.
    assert request.objectives[0].pointwise.upper_bound == 1e-6
    assert not request.objectives[0].mean.enabled
    assert request.max_evaluations is not None
    assert result.evaluations < 100 < request.max_evaluations
    hybrid = result.performance['search']['hybrid']
    local = hybrid['local_session']
    assert hybrid['completed_rotations'] >= 1
    assert local['session_complete'] and local['local_converged']
    assert local['solver_terminal']['physical_hash'] == local['audited_selected']['physical_hash']
    assert result.local_optimum is not None
    assert result.local_optimum['validation_complete']
    stages = [row.stage for row in result.trace]
    assert stages.index('local_session') < stages.index('local_session_complete')
    assert not any(stage == 'growth_restart' for stage in stages[stages.index('local_session_complete') + 1:])


def test_unmet_post_local_thinning_never_worsens_the_audited_optical_champion():
    service, request = unmet_problem()
    result = synthesize_film_design(service, request)
    complete_evidence(result)
    assert result.local_optimum is not None and result.validation_score is not None
    before = result.local_optimum['validation_score']
    assert result.validation_score['max_error'] <= before['max_error']
    assert not result.targets_met
    reduction = result.performance['search']['hybrid']['thickness_reduction']
    assert reduction['status'] != 'skipped_no_audited_nominal_incumbent'
    stages = [row.stage for row in result.trace]
    assert stages.index('local_session_complete') < stages.index('thickness_reduction_complete')
    assert result.search_checkpoint is not None
    assert validate_search_state(result.frozen_request, result.search_checkpoint)


def test_feasible_default_still_improves_margin_then_thins_and_keeps_nominal_audit():
    from tests.test_film_synthesis_thickness_integration import request_case
    service, request, _initial = request_case('AR', 'pointwise')
    result = synthesize_film_design(service, request)
    assert result.state == 'completed' and result.targets_met
    complete_evidence(result)
    assert result.validation_score is not None and result.validation_score['nominal_pass']
    stages = [row.stage for row in result.trace]
    assert stages.index('local_margin_complete') < stages.index('thickness_reduction_complete')


def test_evaluation_budget_is_not_reported_as_local_convergence():
    service, request = unmet_problem(max_evaluations=2)
    result = synthesize_film_design(service, request)
    assert result.termination_reason == 'evaluation_limit'
    assert result.evaluations == 2 and not result.targets_met
    local = result.performance['search'].get('hybrid', {}).get('local_session', {})
    assert not local.get('local_converged', False)
    assert not local.get('session_complete', False)
    assert not any(row.stage == 'local_session_complete' for row in result.trace)


def test_cancel_at_final_local_session_keeps_evidence_without_later_optics():
    service, request = unmet_problem()
    cancel = Event()
    observed = []
    def progress(row):
        observed.append(row)
        if row.stage == 'local_session':
            cancel.set()
    result = synthesize_film_design(service, request, cancel_event=cancel, progress_callback=progress)
    assert result.state == 'cancelled'
    assert not result.targets_met
    stopped = next(i for i, row in enumerate(observed) if row.stage == 'local_session')
    assert not any(row.stage in {'validation', 'local_session_complete', 'thickness_reduction_trial'}
                   for row in observed[stopped + 1:])
    assert not result.performance['search']['hybrid']['local_session'].get('local_converged', False)
    if result.validation_complete:
        complete_evidence(result)
    assert result.search_checkpoint is not None


def test_terminal_iteration_limit_cannot_be_relabelled_as_a_converged_session(monkeypatch):
    from coat_sim.core import film_synthesis_refinement as refinement
    service, request = unmet_problem()
    original = refinement.refine_synthesis_full_minimax
    entered = []
    def limited(request, initial, **kwargs):
        if kwargs.get('terminal_callback') is None:
            return original(request, initial, **kwargs)
        entered.append(True)
        def optimizer(_fun, start, **_options):
            return SimpleNamespace(x=np.asarray(start).copy(), success=False, status=9, nit=1,
                                   message='controlled local iteration limit')
        with monkeypatch.context() as scoped:
            scoped.setattr(refinement.import_module('scipy.optimize'), 'minimize', optimizer)
            return original(request, initial, **kwargs)
    monkeypatch.setattr(refinement, 'refine_synthesis_full_minimax', limited)
    result = synthesize_film_design(service, request)
    assert entered
    local = result.performance['search']['hybrid']['local_session']
    assert not local.get('local_converged', False)
    assert not local.get('session_complete', False)
    assert result.termination_reason not in {
        'local_session_converged_targets_unmet', 'local_session_stalled_targets_unmet'}
    assert result.search_checkpoint is not None


def test_substantial_fixed_topology_work_can_stop_stalled_without_claiming_stationarity(monkeypatch):
    from coat_sim.core import film_synthesis_refinement as refinement
    service, request = unmet_problem()
    original = refinement.refine_synthesis_full_minimax
    entered = []
    def stalled(request, initial, **kwargs):
        if kwargs.get('terminal_callback') is None:
            return original(request, initial, **kwargs)
        entered.append(True)
        def optimizer(_fun, start, **options):
            for direction in (-1., 1.):
                trial = np.asarray(start).copy()
                trial[0] += direction * .01  # Real one-nm physical neighbours.
                options['constraints'][0]['fun'](trial)
            options['callback'](start)
            return SimpleNamespace(x=np.asarray(start).copy(), success=False, status=9, nit=2,
                                   message='controlled two-iteration plateau after real neighbouring work')
        with monkeypatch.context() as scoped:
            scoped.setattr(refinement.import_module('scipy.optimize'), 'minimize', optimizer)
            return original(request, initial, **kwargs)
    monkeypatch.setattr(refinement, 'refine_synthesis_full_minimax', stalled)
    result = synthesize_film_design(service, request)
    assert entered and result.state == 'completed'
    assert result.termination_reason == 'local_session_stalled_targets_unmet'
    local = result.performance['search']['hybrid']['local_session']
    assert local['session_complete'] and not local['local_converged']
    assert not local['solver_terminal']['local_stationary']
    complete_evidence(result)
    assert not result.targets_met


def test_a_converged_different_terminal_keeps_the_better_audited_trial_without_a_false_stall(monkeypatch):
    from coat_sim.core import film_synthesis_refinement as refinement
    service, request = unmet_problem()
    original = refinement.refine_synthesis_full_minimax
    entered = []
    def return_worse_terminal(request, initial, **kwargs):
        if kwargs.get('terminal_callback') is None:
            return original(request, initial, **kwargs)
        entered.append(True)
        def optimizer(_fun, start, **options):
            terminal = np.asarray(start).copy()
            terminal[0] += .01  # A different, physically evaluated one-nm endpoint.
            terminal[-1] = 1.
            residuals = options['constraints'][0]['fun'](terminal)
            terminal[-1] = max(0., 1. - float(np.min(residuals)))
            return SimpleNamespace(x=terminal, success=True, status=0, nit=3,
                                   message='controlled converged endpoint, distinct from the strict champion')
        with monkeypatch.context() as scoped:
            scoped.setattr(refinement.import_module('scipy.optimize'), 'minimize', optimizer)
            return original(request, initial, **kwargs)
    monkeypatch.setattr(refinement, 'refine_synthesis_full_minimax', return_worse_terminal)
    result = synthesize_film_design(service, request)
    assert entered and result.state == 'completed'
    assert result.termination_reason == 'local_session_terminal_converged_best_retained_targets_unmet'
    local = result.performance['search']['hybrid']['local_session']
    assert local['session_complete'] and not local['local_converged']
    assert local['solver_terminal']['local_stationary']
    assert local['solver_terminal']['physical_hash'] != local['audited_selected']['physical_hash']
    assert result.candidate_design is not None
    assert physical_design_hash(result.candidate_design) == local['audited_selected']['physical_hash']
    assert result.performance['search']['hybrid']['thickness_reduction']['promoted'] is False
    assert not any(row.stage == 'thickness_reduction_trial' for row in result.trace)
    complete_evidence(result)


def test_training_pass_with_real_dense_audit_failure_has_an_explicit_rejected_session_reason(monkeypatch):
    from coat_sim.core import film_synthesis_audit as audit
    service, request = unmet_problem()
    # An actual high-order layer fits the two training endpoints exactly, but
    # the dense audit finds a large physical peak between them.
    seed = deepcopy(dict(request.environment))
    seed['front_layers'] = [dict(id='coarse-training-high-order', material_id='low', optical_set_id='nk',
        thickness=dict(value=3500. / (4. * np.sqrt(1.5)), unit='nm'))]
    request = replace(request, objectives=(replace(request.objectives[0], wavelength_min_nm=500.,
        wavelength_max_nm=700., wavelength_samples=2),), seed_designs=(seed,), max_layer_thickness_nm=800.)
    original_audit = audit.audit_frozen_synthesis_candidate
    seen: list[tuple[str, bool]] = []
    def observe_audit(frozen, design, **kwargs):
        value = original_audit(frozen, design, **kwargs)
        seen.append((physical_design_hash(design), value['score']['nominal_pass']))
        return value
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', observe_audit)
    result = synthesize_film_design(service, request)
    assert result.state == 'completed' and not result.targets_met
    assert result.termination_reason == 'local_session_audit_rejected_targets_unmet'
    local = result.performance['search']['hybrid']['local_session']
    assert local['status'] == 'audit_rejected'
    assert local['session_complete'] and not local['local_converged']
    assert local['solver_terminal']['nominal_pass']
    assert local['terminal_audit_complete'] and local['terminal_audit_max_error'] > 0.
    assert (local['solver_terminal']['physical_hash'], False) in seen
    assert result.local_optimum is not None and result.local_optimum['termination'] == 'audit_rejected'
    assert not any(row.stage == 'thickness_reduction_trial' for row in result.trace)
    assert 'stalled' not in result.termination_reason
    complete_evidence(result)


def test_two_explicit_continues_advance_real_sources_instead_of_returning_old_champion():
    service, request = explorable_problem(317)
    current = synthesize_film_design(service, request)
    assert current.state == 'completed' and not current.targets_met
    for _ in range(2):
        assert current.candidate_design is not None and current.search_checkpoint is not None
        saved = json.loads(json.dumps(current.search_checkpoint))
        unchanged = deepcopy(saved)
        continued = continue_synthesis_request(current.frozen_request, saved,
                                               current_design=current.candidate_design)
        following = synthesize_film_design(None, continued)
        assert saved == unchanged
        assert following.search_checkpoint is not None
        assert following.state == 'completed' and not following.targets_met
        assert any(row.stage == 'structure_proposal' for row in following.trace)
        assert following.evaluations > 2
        assert sum(following.search_checkpoint['cursors'].values()) > sum(saved['cursors'].values())
        assert set(following.search_checkpoint['tried']) - set(saved['tried'])
        assert following.search_checkpoint['random_state']['root_seed'] == 317
        assert (following.search_checkpoint['random_state']['next_rotation_seed']
                >= saved['random_state']['next_rotation_seed'])
        assert validate_search_state(following.frozen_request, following.search_checkpoint)
        complete_evidence(following)
        current = following


def test_fixed_one_layer_exhausted_frontier_stops_honestly_without_false_local_completion():
    service, request = unmet_problem(randomize_template_search=True, structure_search_seed=317,
                                    minimum_nonzero_thickness_nm=80., max_layer_thickness_nm=80.)
    first = synthesize_film_design(service, request)
    assert first.state == 'completed' and first.candidate_design is not None and first.search_checkpoint is not None
    current = first
    for _ in range(8):  # The finite duplicate stream can require several bounded source quanta.
        assert current.search_checkpoint is not None and current.candidate_design is not None
        previous = current.search_checkpoint
        result = synthesize_film_design(None, continue_synthesis_request(current.frozen_request,
            previous, current_design=current.candidate_design))
        assert result.search_checkpoint is not None
        assert not any(row.stage == 'structure_proposal' for row in result.trace)
        frontier = result.search_checkpoint
        assert not result.performance['search']['hybrid'].get('local_session', {}).get('session_complete', False)
        assert not result.targets_met
        assert sum(frontier['cursors'].values()) >= sum(previous['cursors'].values())
        if not frontier['active_sources'] and not frontier['pending']:
            assert result.termination_reason == 'continuation_frontier_exhausted'
            assert result.performance['search']['hybrid']['continuation_session']['frontier_exhausted']
            return
        assert result.termination_reason == 'continuation_quantum_no_new_candidate'
        assert not result.performance['search']['hybrid']['continuation_session']['frontier_exhausted']
        current = result
    pytest.fail('finite fixed-thickness source did not finish its bounded duplicate quanta')


def test_exhausted_finite_priors_still_continue_new_physical_basins_when_thickness_is_free():
    service, request = unmet_problem(randomize_template_search=True, structure_search_seed=317)
    first = synthesize_film_design(service, request)
    assert first.state == 'completed' and first.candidate_design is not None and first.search_checkpoint is not None
    assert not first.search_checkpoint['active_sources']
    before = deepcopy(dict(first.search_checkpoint))
    result = synthesize_film_design(None, continue_synthesis_request(first.frozen_request,
        before, current_design=first.candidate_design))
    assert result.state == 'completed' and not result.targets_met
    assert result.search_checkpoint is not None
    assert result.termination_reason != 'continuation_frontier_exhausted'
    assert result.evaluations > 2
    newly_evaluated = set(result.search_checkpoint['tried']) - set(before['tried'])
    assert newly_evaluated
    assert any(row.physical_hash in newly_evaluated for row in result.trace)
    assert result.search_checkpoint['rotations'] > before['rotations']
    assert (result.search_checkpoint['random_state']['next_rotation_seed']
            > before['random_state']['next_rotation_seed'])
    assert validate_search_state(result.frozen_request, result.search_checkpoint)
    complete_evidence(result)


def test_legacy_scaled_stagnation_finishes_after_complete_cycles_without_claiming_stationarity():
    service, request = setup_problem(upper=0., low=550., high=550., samples=1, materials=('high',))
    request = replace(request, stagnation_cycles=2, growth_quarter_wave_fractions=(1.,),
                      position_samples=1, max_cycles=8, max_evaluations=300)
    result = synthesize_film_design(service, request)
    assert result.state == 'completed' and not result.targets_met
    assert result.termination_reason == 'local_session_stalled_targets_unmet'
    assert request.max_evaluations is not None
    assert result.evaluations < request.max_evaluations
    legacy = result.performance['search']['legacy_local_session']
    assert legacy['session_complete'] and not legacy['local_converged']
    assert legacy['completed_cycles'] >= legacy['required_stagnant_cycles'] >= 2
    assert result.local_optimum is not None and not result.local_optimum['local_converged']
    complete_evidence(result)


def test_legacy_audit_feedback_improvement_resets_the_completed_turn_stagnation_counter(monkeypatch):
    from coat_sim.core import film_auto_synthesis as synthesis, film_synthesis_refinement as refinement
    from coat_sim.core.film_synthesis_feedback import restore_feedback_base
    service, request = setup_problem(upper=0., materials=('low',))
    seed = deepcopy(dict(request.environment))
    seed['front_layers'] = [dict(id='controlled-quarter-wave', material_id='low', optical_set_id='nk',
        thickness=dict(value=550. / (4. * np.sqrt(1.5)), unit='nm'))]
    request = replace(request, seed_designs=(seed,), acceptable_normalized_error=0., local_step_tolerance=.5,
        relative_improvement_tolerance=.05, stagnation_cycles=2, growth_quarter_wave_fractions=(1.,),
        position_samples=1, cpu_workers=1, max_cycles=8, max_evaluations=300)
    context: dict[str, Any] = dict(cycle=0, feedback_due=None)
    observed: list[Any] = []
    repairs: list[dict[str, Any]] = []
    starts: dict[int, float] = {}
    growth_scores: dict[int, float] = {}
    original_insert = synthesis._insert_at_site
    def progress(row):
        context['cycle'] = row.cycle
        observed.append(row)
        if row.stage == 'audit_feedback_repair':
            context['feedback_due'] = row.cycle
    def nearby_growth(design, site, reference, thickness, identifier, split_id, **kwargs):
        trial = original_insert(design, site, reference, thickness, identifier, split_id, **kwargs)
        if thickness > 0. and not kwargs.get('zero', False) and context['cycle'] > 0:
            # Each completed growth turn supplies a small real improvement,
            # below the configured 5% stagnation scale. Keep ordinary search
            # controlled so only the boundary audit repair makes the large jump.
            total = 112.27 - .01 * context['cycle']
            for row in trial['front_layers']:
                row['thickness'] = dict(value=total / len(trial['front_layers']), unit='nm')
        return trial
    def controlled_interior(request, initial, *, evaluate, archive, **_kwargs):
        feedback_cycle = context.pop('feedback_due', None)
        if feedback_cycle == 2 and not repairs:
            candidate = deepcopy(initial['design'])
            assert candidate['front_layers']
            harmonic_nm = 2. / (1. / 520. + 1. / 580.)
            total_nm = harmonic_nm / (4. * np.sqrt(1.5))
            for row in candidate['front_layers']:
                row['thickness'] = dict(value=total_nm / len(candidate['front_layers']), unit='nm')
            value = evaluate(candidate, jacobian=True)
            archive(value, audit_eligible=False)
            before = restore_feedback_base(initial)['score']['max_error']
            after = restore_feedback_base(value)['score']['max_error']
            repairs.append(dict(cycle=feedback_cycle, before=before, after=after))
            return value, dict(reason='controlled physical audit-feedback improvement', iterations=1, physical_candidates=1)
        return initial, dict(reason='controlled ordinary interior hold', iterations=0, physical_candidates=0)
    monkeypatch.setattr(synthesis, '_insert_at_site', nearby_growth)
    monkeypatch.setattr(refinement, 'refine_synthesis_candidate', controlled_interior)
    result = synthesize_film_design(service, request, progress_callback=progress)
    assert len(repairs) == 1
    repaired = repairs[0]
    assert repaired['before'] - repaired['after'] > request.relative_improvement_tolerance * repaired['before']
    last_training_error: float | None = None
    for row in result.trace:
        if row.stage == 'incumbent':
            last_training_error = row.max_normalized_error
        elif row.stage == 'needle_scan':
            assert last_training_error is not None
            starts[row.cycle] = last_training_error
        elif row.stage == 'growth_restart':
            assert row.max_normalized_error is not None
            growth_scores[row.cycle] = row.max_normalized_error
    for cycle in (1, 2):
        assert starts[cycle] - growth_scores[cycle] <= request.relative_improvement_tolerance * starts[cycle]
    feedback_index = next(i for i, row in enumerate(observed)
                          if row.stage == 'audit_feedback_repair' and row.cycle == repaired['cycle'])
    assert any(row.stage == 'growth_restart' and row.cycle == repaired['cycle']
               for row in observed[:feedback_index])
    assert result.state == 'completed' and result.termination_reason == 'local_session_stalled_targets_unmet'
    assert result.cycles >= repaired['cycle'] + request.stagnation_cycles
    assert not any(row.stage == 'local_session_complete' and row.cycle == repaired['cycle'] for row in observed)
    legacy = result.performance['search']['legacy_local_session']
    assert legacy['stagnant_cycles'] >= request.stagnation_cycles
    assert legacy['completed_cycles'] > repaired['cycle']
    assert not legacy['local_converged'] and not result.targets_met
    complete_evidence(result)


def test_legacy_audit_only_pair_improvement_resets_stagnation_when_training_best_is_already_zero(monkeypatch):
    from coat_sim.core import film_auto_synthesis as synthesis, film_synthesis_refinement as refinement
    from coat_sim.core.film_synthesis_feedback import restore_feedback_base
    service, request = setup_problem(upper=.001, low=500., high=700., samples=2, materials=('low',))
    seed = deepcopy(dict(request.environment))
    seed['front_layers'] = [dict(id='audit-only-high-order', material_id='low', optical_set_id='nk',
        thickness=dict(value=3500. / (4. * np.sqrt(1.5)), unit='nm'))]
    request = replace(request, seed_designs=(seed,), acceptable_normalized_error=0., local_step_tolerance=.5,
        relative_improvement_tolerance=.05, stagnation_cycles=2, growth_quarter_wave_fractions=(1.,),
        position_samples=1, cpu_workers=1, max_cycles=8, max_evaluations=300, max_layer_thickness_nm=800.)
    context: dict[str, Any] = dict(cycle=0, feedback_due=None)
    pairs: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    original_insert = synthesis._insert_at_site
    def progress(row):
        context['cycle'] = row.cycle
        if row.stage == 'audit_feedback_repair':
            context['feedback_due'] = row.cycle
    def snapshot(design, training, validation, _audit):
        pairs.append(dict(physical_hash=physical_design_hash(design), training=training['max_error'],
                          validation=validation['max_error'], merit=max(training['max_error'], validation['max_error'])))
    def nearby_high_order_growth(design, site, reference, thickness, identifier, split_id, **kwargs):
        trial = original_insert(design, site, reference, thickness, identifier, split_id, **kwargs)
        if thickness > 0. and not kwargs.get('zero', False) and context['cycle'] > 0:
            total_nm = 714.4 - .01 * context['cycle']
            for row in trial['front_layers']:
                row['thickness'] = dict(value=total_nm / len(trial['front_layers']), unit='nm')
        return trial
    def repair_audit_only(request, initial, *, evaluate, archive, **_kwargs):
        feedback_cycle = context.pop('feedback_due', None)
        if feedback_cycle == 2 and not repairs:
            assert pairs
            candidate = deepcopy(initial['design'])
            total_nm = (2. / (1. / 500. + 1. / 700.)) / (4. * np.sqrt(1.5))
            for row in candidate['front_layers']:
                row['thickness'] = dict(value=total_nm / len(candidate['front_layers']), unit='nm')
            value = evaluate(candidate, jacobian=True)
            archive(value, audit_eligible=False)
            repairs.append(dict(cycle=feedback_cycle, previous_audited_merit=pairs[-1]['merit'],
                source_training_error=restore_feedback_base(initial)['score']['max_error'],
                training_error=restore_feedback_base(value)['score']['max_error'],
                physical_hash=physical_design_hash(candidate)))
            return value, dict(reason='controlled real audit-only improvement', iterations=1, physical_candidates=1)
        return initial, dict(reason='controlled ordinary interior hold', iterations=0, physical_candidates=0)
    monkeypatch.setattr(synthesis, '_insert_at_site', nearby_high_order_growth)
    monkeypatch.setattr(refinement, 'refine_synthesis_candidate', repair_audit_only)
    result = synthesize_film_design(service, request, progress_callback=progress, snapshot_callback=snapshot)
    assert len(repairs) == 1
    repaired = repairs[0]
    assert repaired['source_training_error'] == 0. < repaired['training_error']
    audited_repair = next(pair for pair in pairs if pair['physical_hash'] == repaired['physical_hash'])
    assert audited_repair['merit'] == max(audited_repair['training'], audited_repair['validation'])
    assert (repaired['previous_audited_merit'] - audited_repair['merit']
            > request.relative_improvement_tolerance * repaired['previous_audited_merit'])
    assert result.state == 'completed' and result.termination_reason == 'local_session_stalled_targets_unmet'
    assert result.cycles >= repaired['cycle'] + request.stagnation_cycles
    legacy = result.performance['search']['legacy_local_session']
    assert legacy['last_scaled_improvement'] == 0.
    assert legacy['completed_cycles'] > repaired['cycle']
    assert not legacy['local_converged'] and not result.targets_met
    complete_evidence(result)


def test_saved_edited_seed_replays_across_a_new_process_without_resetting_the_frontier(tmp_path):
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
    from coat_sim.scheduler.io import write_json
    service, request = explorable_problem(911)
    first = synthesize_film_design(service, request)
    assert first.state == 'completed' and first.candidate_design is not None
    assert first.search_checkpoint is not None
    path = tmp_path / 'search.json'
    write_json(path, first.search_checkpoint)
    original = path.read_bytes()
    state = load_search_state(path)
    assert validate_search_state(first.frozen_request, state) == state
    assert path.read_bytes() == original
    edited = deepcopy(dict(first.candidate_design))
    edited['front_layers'][0]['thickness']['value'] *= .97
    continued = continue_synthesis_request(first.frozen_request, state, current_design=edited)
    assert continued.seed_designs[0] == edited
    direct = synthesize_film_design(None, continued)
    assert direct.search_checkpoint is not None and direct.candidate_design is not None
    assert any(row.stage == 'structure_proposal' for row in direct.trace)
    envelope, output = tmp_path / 'request.json', tmp_path / 'result.json'
    envelope.write_text(json.dumps(film_synthesis_request_to_payload('session-reopen', continued)), encoding='utf8')
    script = '''
import json,sys
from pathlib import Path
from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload
from coat_sim.core.film_auto_synthesis import synthesize_film_design
_,request,_=film_synthesis_request_from_payload(json.loads(Path(sys.argv[1]).read_text(encoding='utf8')))
result=synthesize_film_design(None,request)
data=dict(state=result.search_checkpoint,design=result.candidate_design,reason=result.termination_reason,
          evaluations=result.evaluations,sequence=[row.physical_hash for row in result.trace if row.stage=='structure_proposal'])
Path(sys.argv[2]).write_text(json.dumps(data),encoding='utf8')
'''
    env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', PYTHONUTF8='1')
    subprocess.run([sys.executable, '-c', script, str(envelope), str(output)],
                   cwd=Path(__file__).resolve().parents[1], env=env, check=True, capture_output=True, timeout=45)
    child = json.loads(output.read_text(encoding='utf8'))
    assert child['sequence'] == [row.physical_hash for row in direct.trace if row.stage == 'structure_proposal']
    assert child['evaluations'] == direct.evaluations
    assert physical_design_hash(child['design']) == physical_design_hash(direct.candidate_design)
    assert child['reason'] == direct.termination_reason
    for key in ('cursors', 'source_cursor', 'active_sources', 'pending', 'random_state', 'feedback'):
        assert child['state'][key] == json.loads(json.dumps(direct.search_checkpoint[key]))
    assert path.read_bytes() == original
    assert direct.performance['search']['warm_start']['physical_hash'] == physical_design_hash(edited)


def test_unmet_worker_result_and_audited_sidecar_recover_without_faking_target_success(tmp_path):
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
    from coat_sim.core.film_synthesis_jobs import build_job_payload, execute_job, is_synthesis_result, validate_checkpoint
    from coat_sim.scheduler.io import write_json
    service, request = unmet_problem()
    frozen = freeze_film_synthesis_request(service, request)
    job = build_job_payload(dict(payload=film_synthesis_request_to_payload('unmet-session-recovery', frozen),
                                 physical_structure_metadata=[]))
    path = tmp_path / 'best.json'
    response = execute_job({'payload': job}, Event(), lambda *_a, **_k: None, path, write_json, str(tmp_path))
    assert is_synthesis_result(response)
    assert response['result']['state'] == 'completed'
    assert response['result']['targets_met'] is False
    assert response['result']['termination_reason'] == 'local_session_converged_targets_unmet'
    assert response['result']['validation_complete']
    saved = json.loads(path.read_text(encoding='utf8'))
    original = path.read_bytes()
    recovered = validate_checkpoint(saved)
    assert is_synthesis_result(recovered) and path.read_bytes() == original
    assert recovered['result']['targets_met'] is False
    assert recovered['result']['validation_complete']
    local = recovered['result']['local_optimum']
    assert local['local_converged'] and local['validation_score']['nominal_pass'] is False
    assert local['physical_hash'] == response['result']['local_optimum']['physical_hash']
    assert recovered['result']['search_checkpoint']['cursors'] == response['result']['search_checkpoint']['cursors']
    assert (tmp_path / 'best.search-state.json').is_file()
    tampered = deepcopy(saved)
    tampered['audit']['local_optimum']['solver_terminal']['physical_hash'] = '0' * 64
    with pytest.raises(ValueError, match='convergence identity'):
        validate_checkpoint(tampered)
    assert path.read_bytes() == original


@pytest.mark.parametrize('change', ['target', 'material', 'constraint'])
def test_unmet_session_restore_rejects_changed_physics_without_losing_saved_progress(tmp_path, change):
    from coat_sim.scheduler.io import write_json
    service, request = unmet_problem()
    first = synthesize_film_design(service, request)
    assert first.state == 'completed' and first.search_checkpoint is not None
    path = tmp_path / 'progress.json'
    write_json(path, first.search_checkpoint)
    original = path.read_bytes()
    frozen = first.frozen_request
    if change == 'target':
        frozen = replace(frozen, objectives=(replace(frozen.objectives[0], normalization_scale=.2),))
    elif change == 'material':
        assert frozen.frozen_material_snapshot is not None
        snapshot = deepcopy(dict(frozen.frozen_material_snapshot))
        snapshot['materials'][0]['optical_sets'][0]['n'][0] += .01
        frozen = replace(frozen, frozen_material_snapshot=snapshot)
    else:
        frozen = replace(frozen, max_layer_thickness_nm=179.)
    with pytest.raises(ValueError, match='fingerprint changed'):
        continue_synthesis_request(frozen, load_search_state(path))
    assert path.read_bytes() == original


def test_strict_best_trial_keeps_its_identity_separate_from_the_solver_terminal(monkeypatch):
    from coat_sim.core import film_synthesis_refinement as refinement
    objective = FilmOptimizationObjective(500., 600., 3, normalization_scale=.004,
        pointwise=FilmObjectiveConstraint(lower_bound=None, upper_bound=.0001))
    request = FilmSynthesisRequest({}, (objective,), (), max_layer_thickness_nm=10.)
    design = dict(front_layers=[dict(id='d', material_id='m', optical_set_id='nk',
                                     thickness=dict(value=4., unit='nm'))])
    def evaluate(candidate, *, jacobian=True):
        thickness = candidate['front_layers'][0]['thickness']['value']
        actual = .001 + .001 * (thickness - 5.) ** 2
        responses = [dict(objective_index=0, angle_deg=0., wavelength_nm=[500., 550., 600.],
            actual_values=[actual] * 3, jacobian_values_by_layer_id={'d': [.002 * (thickness - 5.)] * 3})]
        score = reduction_score(reduce_objective_responses(request.objectives, responses, layer_ids=('d',)), .001)
        return dict(design=deepcopy(candidate), responses=responses, score=score)
    def optimizer(_fun, _start, **kwargs):
        kwargs['constraints'][0]['fun'](np.array([.05, .225]))
        return SimpleNamespace(x=np.array([.06, .475]), success=True, status=0, nit=2,
                               message='controlled terminal, distinct from the better line-search trial')
    monkeypatch.setattr(refinement.import_module('scipy.optimize'), 'minimize', optimizer)
    terminals: list[dict[str, Any]] = []
    best, details = refinement.refine_synthesis_full_minimax(request, evaluate(design), evaluate=evaluate,
        archive=lambda *_a, **_k: None, terminal_callback=terminals.append)
    assert best['design']['front_layers'][0]['thickness']['value'] == pytest.approx(5.)
    assert terminals[-1]['design']['front_layers'][0]['thickness']['value'] == pytest.approx(6.)
    assert details['solver_terminal']['physical_hash'] == physical_design_hash(terminals[-1]['design'])
    assert details['returned_best']['physical_hash'] == physical_design_hash(best['design'])
    assert details['solver_terminal']['physical_hash'] != details['returned_best']['physical_hash']
    assert not details['returned_best']['is_solver_terminal']


def quality_value(request: FilmSynthesisRequest, actual: list[list[float]]) -> dict[str, Any]:
    responses = [dict(objective_index=i, angle_deg=angle,
        wavelength_nm=np.linspace(objective.wavelength_min_nm, objective.wavelength_max_nm,
                                  objective.wavelength_samples), actual_values=values)
        for (i, objective, angle), values in zip(
            ((i, objective, angle) for i, objective in enumerate(request.objectives)
             for angle in objective.angles_deg), actual, strict=True)]
    return dict(score=reduction_score(reduce_objective_responses(request.objectives, responses), .001))


@pytest.mark.parametrize('metric,criterion,before,better,worse', [
    ('R', FilmObjectiveConstraint(lower_bound=0., upper_bound=.004), .003, .002, .0035),
    ('R', FilmObjectiveConstraint(lower_bound=.996, upper_bound=1.), .997, .998, .9965),
])
def test_thinning_quality_uses_the_original_ar_or_hr_direction(metric, criterion, before, better, worse):
    from coat_sim.core.film_synthesis_thickness import optical_quality_by_group, preserves_optical_quality
    objective = FilmOptimizationObjective(500., 600., 3, metric=metric, normalization_scale=.004,
                                         pointwise=criterion)
    request = FilmSynthesisRequest({}, (objective,), ())
    original = quality_value(request, [[before] * 3])
    improved = quality_value(request, [[better] * 3])
    degraded = quality_value(request, [[worse] * 3])
    assert optical_quality_by_group(request, original)[(0, 0., 'pointwise')] == pytest.approx(-.25)
    assert preserves_optical_quality(request, original, improved)
    assert not preserves_optical_quality(request, original, degraded)
    assert set(optical_quality_by_group(request, original)) == {(0, 0., 'pointwise')}


@pytest.mark.parametrize('criterion', [
    FilmObjectiveConstraint(mode='value', target_value=.2, lower_bound=None, upper_bound=None),
    FilmObjectiveConstraint(lower_bound=.2, upper_bound=.2),
])
def test_value_and_zero_width_thinning_keep_original_violation_instead_of_inventing_safety(criterion):
    from coat_sim.core.film_synthesis_thickness import optical_quality_by_group, preserves_optical_quality
    objective = FilmOptimizationObjective(500., 600., 3, normalization_scale=.1, pointwise=criterion)
    request = FilmSynthesisRequest({}, (objective,), ())
    original = quality_value(request, [[.25] * 3])
    better = quality_value(request, [[.21] * 3])
    worse = quality_value(request, [[.3] * 3])
    assert optical_quality_by_group(request, original)[(0, 0., 'pointwise')] == pytest.approx(.5)
    assert preserves_optical_quality(request, original, better)
    assert not preserves_optical_quality(request, original, worse)


def test_thinning_cannot_hide_a_worse_objective_angle_or_explicit_mean_behind_an_improved_global_max():
    from coat_sim.core.film_synthesis_thickness import preserves_optical_quality
    objectives = (
        FilmOptimizationObjective(500., 600., 3, angles_deg=(0., 30.), normalization_scale=.01,
            pointwise=FilmObjectiveConstraint(lower_bound=None, upper_bound=.004)),
        FilmOptimizationObjective(650., 700., 3, metric='T', normalization_scale=.1,
            pointwise=FilmObjectiveConstraint(lower_bound=.9, upper_bound=None)),
    )
    request = FilmSynthesisRequest({}, objectives, ())
    original = quality_value(request, [[.01] * 3, [.005] * 3, [.85] * 3])
    # Global max improves .6 -> .3, but the second angle becomes worse .1 -> .2.
    wrong_angle = quality_value(request, [[.007] * 3, [.006] * 3, [.89] * 3])
    assert wrong_angle['score']['max_error'] < original['score']['max_error']
    assert not preserves_optical_quality(request, original, wrong_angle)
    # Again global max improves .6 -> .55, while the T objective gets worse.
    wrong_objective = quality_value(request, [[.007] * 3, [.0045] * 3, [.845] * 3])
    assert wrong_objective['score']['max_error'] < original['score']['max_error']
    assert not preserves_optical_quality(request, original, wrong_objective)
    mean_objective = FilmOptimizationObjective(500., 600., 3, normalization_scale=.01,
        pointwise=FilmObjectiveConstraint(lower_bound=None, upper_bound=.004),
        mean=FilmObjectiveConstraint(enabled=True, lower_bound=None, upper_bound=.002))
    mean_request = FilmSynthesisRequest({}, (mean_objective,), ())
    original = quality_value(mean_request, [[0., .006, 0.]])
    # Peak improves .2 -> .1, while the configured mean rises .1 -> .15.
    worse_mean = quality_value(mean_request, [[.005, .002, .005]])
    assert worse_mean['score']['max_error'] < original['score']['max_error']
    assert not preserves_optical_quality(mean_request, original, worse_mean)


def test_disabled_mean_is_absent_from_thinning_quality_even_with_a_stricter_unused_bound():
    from coat_sim.core.film_synthesis_thickness import optical_quality_by_group, preserves_optical_quality
    objective = FilmOptimizationObjective(500., 600., 3, normalization_scale=.004,
        pointwise=FilmObjectiveConstraint(lower_bound=None, upper_bound=.004),
        mean=FilmObjectiveConstraint(enabled=False, lower_bound=None, upper_bound=0.))
    request = FilmSynthesisRequest({}, (objective,), ())
    original, better = quality_value(request, [[.003] * 3]), quality_value(request, [[.002] * 3])
    assert set(optical_quality_by_group(request, original)) == {(0, 0., 'pointwise')}
    assert preserves_optical_quality(request, original, better)

