"""Parent thinning has no uneditable candidate cap; explicit limits still own work."""
from copy import deepcopy
from dataclasses import replace
from threading import Event
from typing import Any

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_thickness as thickness
from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, FilmSynthesisResult
from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective
from coat_sim.core.film_synthesis_archive import physical_design_hash
from tests.test_film_auto_synthesis import setup_problem
from tests.test_film_synthesis_thickness import physical_fixture

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def improving_seed(**changes: Any) -> tuple[Any, FilmSynthesisRequest]:
    """Production TMM with synthetic n,k; the 130-nm AR seed can improve while thinning."""
    service, request, initial, _evaluate, _calls = physical_fixture('AR', 'pointwise')
    seed = deepcopy(initial['design'])
    seed['front_layers'][0]['thickness']['value'] = 130.
    options = dict(search_strategy='hybrid', seed_designs=(seed,), cpu_workers=1, parallel_batch_size=1,
        enable_local_margin_refinement=False, max_cycles=None, max_evaluations=None, max_seconds=None)
    options.update(changes)
    return service, replace(request, **options)


def total(design: Any) -> float:
    return sum(row['thickness']['value'] for row in design['front_layers'])


def numerical(initial: dict[str, Any], candidate: dict[str, Any], *, termination: str,
              physical_candidates: int) -> dict[str, Any]:
    before, after = total(initial['design']), total(candidate['design'])
    return dict(method='controlled finite local backend over actual parent TMM evaluations',
        termination=termination, solver_success=termination == 'solver_converged', iterations=16,
        physical_candidates=physical_candidates, initial_total_thickness_nm=before,
        final_total_thickness_nm=after, reduction_nm=before - after, improved=after < before)


def assert_bound_evidence(result: FilmSynthesisResult) -> None:
    assert result.candidate_design is not None
    assert result.training_score is not None and result.validation_score is not None
    assert result.validation_complete
    identity = physical_design_hash(result.candidate_design)
    assert physical_design_hash(result.training_score['design']) == identity
    assert physical_design_hash(result.validation_score['design']) == identity
    assert not result.frozen_request.objectives[0].mean.enabled


def test_more_than_32_real_thinning_candidates_and_an_improving_local_restart_are_admitted(monkeypatch):
    service, request = improving_seed()
    starts: list[float] = []
    evaluated: list[str] = []
    def backend(request, initial, *, evaluate, archive, cancel_check, **kwargs):
        assert kwargs['max_iterations'] == 16 and kwargs['preserve_quality'] is True
        starts.append(total(initial['design']))
        assert len(starts) <= 2, 'a converged finite local solve must not restart forever'
        targets = ([130. - .001 * index for index in range(1, 36)] + [129.]
                   if len(starts) == 1 else [128.])
        candidate = initial
        for target in targets:
            cancel_check()
            trial = deepcopy(initial['design'])
            trial['front_layers'][0]['thickness']['value'] = target
            candidate = evaluate(trial, jacobian=True)
            assert candidate['score']['nominal_pass']
            assert thickness.preserves_optical_quality(request, initial, candidate)
            evaluated.append(physical_design_hash(candidate['design']))
            archive(candidate, audit_eligible=False)
        return candidate, numerical(initial, candidate, termination=(
            'iteration_limit' if len(starts) == 1 else 'solver_converged'), physical_candidates=len(targets))
    monkeypatch.setattr(thickness, 'reduce_total_thickness', backend)
    result = synthesis.synthesize_film_design(service, request)
    assert starts == [130., 129.]
    assert len(evaluated) == len(set(evaluated)) == 37
    assert result.state == 'completed' and result.targets_met
    assert_bound_evidence(result)
    assert result.candidate_design is not None and total(result.candidate_design) == 128.
    detail = result.performance['search']['hybrid']['thickness_reduction']
    assert detail['status'] == 'converged'
    assert len(detail['solver_calls']) == 2
    assert detail['training_evaluations_used'] >= 37
    assert detail['independent_audit_performed'] and detail['promoted']
    assert result.frozen_request.max_evaluations is None
    assert result.frozen_request.max_cycles is None and result.frozen_request.max_seconds is None
    assert result.frozen_request.objectives == request.objectives


@pytest.mark.parametrize('boundary', ['evaluation_limit', 'time_limit', 'cancelled'])
def test_only_explicit_global_limits_or_cancel_stop_a_running_thinning_backend(monkeypatch, boundary):
    service, request = improving_seed(**({'max_evaluations': 12} if boundary == 'evaluation_limit'
        else {'max_seconds': 10.} if boundary == 'time_limit' else {}))
    now = [0.]
    monkeypatch.setattr(synthesis, 'monotonic', lambda: now[0])
    cancel = Event()
    observed: list[Any] = []
    evaluated: list[str] = []
    starts: list[int] = []
    def backend(request, initial, *, evaluate, archive, cancel_check, progress, **_kwargs):
        assert not starts, 'a stopped local solve must not be silently restarted'
        starts.append(observed[-1].evaluations)
        for index in range(1, 101):
            cancel_check()
            trial = deepcopy(initial['design'])
            trial['front_layers'][0]['thickness']['value'] = (
                95. - .001 * index if boundary == 'evaluation_limit' else total(initial['design']) - .001 * index)
            value = evaluate(trial, jacobian=True)
            assert value['score']['nominal_pass']
            assert thickness.preserves_optical_quality(request, initial, value) is (boundary != 'evaluation_limit')
            archive(value, audit_eligible=False)
            evaluated.append(physical_design_hash(value['design']))
            progress('thickness_reduction_trial', value, dict(physical_candidates=len(evaluated)))
            if boundary == 'time_limit':
                now[0] = 11.
            elif boundary == 'cancelled':
                cancel.set()
            cancel_check()
        pytest.fail('the explicit boundary did not stop the finite test backend')
    monkeypatch.setattr(thickness, 'reduce_total_thickness', backend)
    result = synthesis.synthesize_film_design(service, request, cancel_event=cancel,
                                             progress_callback=observed.append)
    assert starts and evaluated
    assert result.termination_reason == boundary
    detail = result.performance['search']['hybrid']['thickness_reduction']
    assert detail['stop_reason'] == boundary and detail['status'] != 'converged'
    assert not detail['promoted']
    if boundary == 'evaluation_limit':
        # Parent admission keeps the existing final independent-audit reserve.
        assert request.max_evaluations is not None
        assert len(evaluated) == request.max_evaluations - 1 - starts[0]
        assert request.max_evaluations - 1 <= result.evaluations <= request.max_evaluations
    else:
        assert len(evaluated) == 1
        stopped = next(i for i, row in enumerate(observed) if row.stage == 'thickness_reduction_trial')
        assert not any(row.stage in {'validation', 'thickness_reduction_trial'} for row in observed[stopped + 1:])
    if boundary == 'cancelled':
        assert result.state == 'cancelled'
    assert_bound_evidence(result)
    assert result.candidate_design is not None and total(result.candidate_design) == 130.


@pytest.mark.parametrize('termination', ['iteration_limit', 'solver_failure'])
def test_a_finite_slice_without_quality_preserving_thickness_gain_does_not_restart(monkeypatch, termination):
    service, request = improving_seed()
    calls: list[str] = []
    def backend(request, initial, *, evaluate, archive, cancel_check, **_kwargs):
        assert not calls, 'a finite unsuccessful local slice must end the thinning session'
        cancel_check()
        trial = deepcopy(initial['design'])
        trial['front_layers'][0]['thickness']['value'] = 80.
        value = evaluate(trial, jacobian=True)
        assert not thickness.preserves_optical_quality(request, initial, value)
        archive(value, audit_eligible=False)
        calls.append(physical_design_hash(value['design']))
        # The backend contract retains the actually admissible incumbent;
        # a merely thinner physical trial cannot replace its optical quality.
        return initial, numerical(initial, initial, termination=termination, physical_candidates=1)
    monkeypatch.setattr(thickness, 'reduce_total_thickness', backend)
    result = synthesis.synthesize_film_design(service, request)
    assert len(calls) == 1
    assert result.state == 'completed' and result.targets_met
    assert_bound_evidence(result)
    assert result.candidate_design is not None and total(result.candidate_design) == 130.
    detail = result.performance['search']['hybrid']['thickness_reduction']
    assert detail['status'] == ('solver_failure' if termination == 'solver_failure'
                                else 'no_quality_preserving_improvement')
    assert len(detail['solver_calls']) == 1 and detail['numerical']['termination'] == termination
    assert detail['training_evaluations_used'] == 1
    assert not detail['promoted'] and not detail['independent_audit_performed']


def test_canonical_layer_deletion_is_promoted_and_restarts_on_its_own_topology_even_after_raw_convergence(monkeypatch):
    service, base = setup_problem(low=550., high=550., samples=1, materials=('high',))
    seed = deepcopy(dict(base.environment))
    seed['front_layers'] = [
        dict(id='substrate-padding', material_id='glass', optical_set_id='nk', thickness=dict(value=10., unit='nm')),
        dict(id='high-quarter', material_id='high', optical_set_id='nk', thickness=dict(value=62.5, unit='nm')),
    ]
    target = FilmOptimizationObjective(550., 550., 1,
        pointwise=FilmObjectiveConstraint(lower_bound=.2, upper_bound=None))
    request = replace(base, search_strategy='hybrid', objectives=(target,), seed_designs=(seed,),
        warm_start_seed_index=0, cpu_workers=1, parallel_batch_size=1, max_layers=2,
        max_cycles=1, max_evaluations=100, max_seconds=20., enable_local_margin_refinement=False)
    starts: list[list[str]] = []
    observations: list[dict[str, Any]] = []
    def backend(request, initial, *, evaluate, archive, cancel_check, **_kwargs):
        cancel_check()
        starts.append([row['id'] for row in initial['design']['front_layers']])
        assert len(starts) <= 2, 'canonical no-improvement convergence must end this cost session'
        if len(starts) == 1:
            assert starts[-1] == ['substrate-padding', 'high-quarter']
            trial = deepcopy(initial['design'])
            trial['front_layers'][0]['thickness']['value'] = float(np.finfo(float).tiny)
            value = evaluate(trial, jacobian=True)
            canonical = value.get('_numeric_zero_canonical')
            assert canonical is not None
            assert len(value['design']['front_layers']) == 2
            assert [row['id'] for row in canonical['design']['front_layers']] == ['high-quarter']
            assert thickness.preserves_optical_quality(request, initial, value)
            assert thickness.preserves_optical_quality(request, initial, canonical)
            observations.append(dict(raw_hash=physical_design_hash(value['design']),
                canonical_hash=physical_design_hash(canonical['design'])))
        else:
            assert starts[-1] == ['high-quarter']
            assert physical_design_hash(initial['design']) == observations[0]['canonical_hash']
            value = evaluate(initial['design'], jacobian=True)
            assert [row['id'] for row in value['design']['front_layers']] == ['high-quarter']
        archive(value, audit_eligible=False)
        # Even a successful endpoint for the old two-variable solve cannot
        # supply stationarity for the new canonical one-layer topology.
        return value, numerical(initial, value, termination='solver_converged', physical_candidates=1)
    monkeypatch.setattr(thickness, 'reduce_total_thickness', backend)
    result = synthesis.synthesize_film_design(service, request)
    assert starts == [['substrate-padding', 'high-quarter'], ['high-quarter']]
    assert result.state == 'completed' and result.targets_met
    assert_bound_evidence(result)
    assert result.candidate_design is not None
    assert [row['id'] for row in result.candidate_design['front_layers']] == ['high-quarter']
    assert total(result.candidate_design) == 62.5
    identity = physical_design_hash(result.candidate_design)
    assert identity == observations[0]['canonical_hash'] != observations[0]['raw_hash']
    detail = result.performance['search']['hybrid']['thickness_reduction']
    assert detail['promoted'] and detail['status'] != 'audit_rejected'
    assert detail['independent_audit_performed']
    assert len(detail['solver_calls']) == 2
    first, second = detail['solver_calls']
    assert first['termination'] == 'solver_converged' and first['topology_changed']
    assert first['raw_proposal_hash'] == observations[0]['raw_hash']
    assert first['canonical_proposal_hash'] == identity
    assert first['raw_layer_ids'] == ['substrate-padding', 'high-quarter']
    assert first['canonical_layer_ids'] == ['high-quarter']
    assert not second['topology_changed']
    assert second['raw_proposal_hash'] == second['canonical_proposal_hash'] == identity
    assert result.local_optimum is not None
    assert result.local_optimum['physical_hash'] == physical_design_hash(seed)
    assert not result.local_optimum['local_converged']
