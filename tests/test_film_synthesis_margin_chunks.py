"""Feasible margin work quanta resume without an immutable stage cutoff."""
from copy import deepcopy
from threading import Event
from typing import Any

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_margin as margin
from coat_sim.core.film_synthesis_archive import physical_design_hash
from tests.test_film_synthesis_thickness_no_hidden_budget import improving_seed

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def test_improving_margin_quantum_restarts_from_audited_point_then_converges(monkeypatch):
    service, request = improving_seed(enable_local_margin_refinement=True, enable_thickness_reduction=False)
    original = margin.refine_worst_margin
    starts: list[dict[str, Any]] = []
    improved: list[str] = []

    def backend(request, initial, *, evaluate, archive, cancel_check, **kwargs):
        cancel_check()
        starts.append(deepcopy(initial['design']))
        assert len(starts) <= 2, 'stationary second quantum must finish this session'
        if len(starts) == 2:
            assert physical_design_hash(initial['design']) == improved[0]
            return original(request, initial, evaluate=evaluate, archive=archive,
                            cancel_check=cancel_check, **kwargs)
        trial = deepcopy(initial['design'])
        trial['front_layers'][0]['thickness']['value'] -= 1.
        value = evaluate(trial, jacobian=True)
        archive(value, audit_eligible=False)
        assert value['score']['nominal_pass']
        assert margin.worst_signed_margin(request, value) < margin.worst_signed_margin(request, initial)
        improved.append(physical_design_hash(value['design']))
        return value, dict(termination='iteration_limit', success=False, iterations=256,
                           physical_candidates=1)

    monkeypatch.setattr(margin, 'refine_worst_margin', backend)
    result = synthesis.synthesize_film_design(service, request)
    assert len(starts) == 2 and starts[0] != starts[1]
    assert [row['id'] for row in starts[0]['front_layers']] == [row['id'] for row in starts[1]['front_layers']]
    assert result.targets_met and result.validation_complete
    assert result.local_optimum is not None and result.local_optimum['local_converged']
    detail = result.performance['search']['hybrid']['local_margin']
    assert detail['status'] == 'converged' and len(detail['solver_calls']) == 2
    assert detail['solver_calls'][0]['iterations'] == 256
    assert detail['total_evaluations_used'] > sum(row['evaluations_used'] for row in detail['solver_calls'])
    assert request.max_evaluations is None and request.max_seconds is None
    assert result.candidate_design is not None and result.validation_score is not None
    assert physical_design_hash(result.candidate_design) == physical_design_hash(result.validation_score['design'])


def test_nonimproving_margin_quantum_ends_as_stalled_without_thinning(monkeypatch):
    service, request = improving_seed(enable_local_margin_refinement=True)
    calls: list[str] = []

    def backend(request, initial, **kwargs):
        assert not calls, 'a nonimproving finite solve is genuine stagnation, not a reason to spin'
        calls.append(physical_design_hash(initial['design']))
        return initial, dict(termination='iteration_limit', success=False, iterations=256,
                             physical_candidates=0)

    monkeypatch.setattr(margin, 'refine_worst_margin', backend)
    result = synthesis.synthesize_film_design(service, request)
    assert result.targets_met and result.validation_complete
    assert result.termination_reason == 'local_margin_scaled_stagnation'
    assert result.local_optimum is not None and not result.local_optimum['local_converged']
    assert 'thickness_reduction' not in result.performance['search']['hybrid']


@pytest.mark.parametrize('boundary', ['cancelled', 'evaluation_limit'])
def test_shared_cancel_and_explicit_budget_interrupt_resumed_margin_without_false_convergence(monkeypatch, boundary):
    service, request = improving_seed(enable_local_margin_refinement=True,
        enable_thickness_reduction=False, **({'max_evaluations': 15} if boundary == 'evaluation_limit' else {}))
    cancel = Event()
    starts = []

    def backend(request, initial, *, evaluate, archive, cancel_check, **kwargs):
        starts.append(physical_design_hash(initial['design']))
        assert len(starts) <= 2
        if len(starts) == 2:
            if boundary == 'cancelled':
                cancel.set()
            while True:
                cancel_check()
                evaluate(initial['design'], jacobian=True)
        trial = deepcopy(initial['design'])
        trial['front_layers'][0]['thickness']['value'] -= 1.
        value = evaluate(trial, jacobian=True)
        archive(value, audit_eligible=False)
        return value, dict(termination='iteration_limit', success=False, iterations=256,
                           physical_candidates=1)

    monkeypatch.setattr(margin, 'refine_worst_margin', backend)
    result = synthesis.synthesize_film_design(service, request, cancel_event=cancel)
    assert len(starts) == 2 and starts[0] != starts[1]
    assert result.termination_reason == boundary
    assert result.local_optimum is None
    assert not result.performance['search']['hybrid']['local_margin']['local_converged']
    assert result.candidate_design is not None and result.training_score is not None
    assert physical_design_hash(result.candidate_design) == physical_design_hash(result.training_score['design'])
    if boundary == 'evaluation_limit':
        assert result.evaluations <= 15
        assert result.validation_complete and result.targets_met
