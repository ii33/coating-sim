"""Controlled scores verify chunk scheduling, not optical convergence."""
from copy import deepcopy
from dataclasses import replace

import pytest

from coat_sim.core.film_optimization_structure import _StructureCancelled
from coat_sim.core.film_synthesis_local_restarts import refine_basin_within_quantum
from coat_sim.core.film_synthesis_refinement import SynthesisResourceGuard, SynthesisStageBudget
from tests.test_film_synthesis_local_restarts import _case

pytestmark = pytest.mark.algorithm


def _candidate(error, marker=0):
    _request, design = _case()
    design['front_layers'][0]['thickness']['value'] += marker
    return dict(design=design, score=dict(max_error=error, weighted_rms_error=error / 2,
        errors=('original-condition',), coverage=('original-optical-grid',)), marker=marker)


def test_improving_iteration_chunks_continue_within_the_same_parent_quantum():
    request, _ = _case()
    request = replace(request, relative_improvement_tolerance=.01)
    initial = _candidate(1.)
    original = deepcopy(initial)
    outputs = [_candidate(.8, 1), _candidate(.6, 2), _candidate(.55, 3)]
    outcomes = ['iteration_limit', 'iteration_limit', 'converged']
    starts = []
    checks = []

    def refine(current):
        starts.append(current)
        index = len(starts) - 1
        return outputs[index], dict(termination=outcomes[index], diagnostic=index)

    final, report = refine_basin_within_quantum(request, initial, refine=refine,
                                               cancel_check=lambda: checks.append('check'))
    assert starts == [initial, outputs[0], outputs[1]]
    assert len(checks) == 3 and final is outputs[-1]
    assert final == outputs[-1] and initial == original
    assert report['basin_policy'] == 'continue_improving_iteration_chunks_within_parent_quantum'
    assert report['termination'] == 'converged'
    assert [row['diagnostic'] for row in report['basin_attempts']] == [0, 1, 2]
    assert final['score']['coverage'] == ('original-optical-grid',)
    assert final['score']['errors'] == ('original-condition',)


def test_relative_stagnation_stops_before_another_chunk():
    request, _ = _case()
    request = replace(request, relative_improvement_tolerance=.01)
    initial = _candidate(10.)
    outputs = [_candidate(9.8, 1), _candidate(9.72, 2)]
    calls = []
    def refine(current):
        calls.append(current)
        return outputs[len(calls) - 1], dict(termination='iteration_limit')
    final, report = refine_basin_within_quantum(request, initial, refine=refine, cancel_check=lambda: None)
    assert calls == [initial, outputs[0]]
    assert final is outputs[1] and len(report['basin_attempts']) == 2
    assert final['score']['max_error'] == 9.72


@pytest.mark.parametrize('termination', ['converged', 'solver_failure'])
def test_non_iteration_limit_termination_never_starts_another_chunk(termination):
    request, _ = _case()
    initial, final = _candidate(2.), _candidate(.5, 1)
    calls = []
    def refine(value):
        calls.append(value)
        return final, dict(termination=termination)
    returned, report = refine_basin_within_quantum(request, initial, refine=refine, cancel_check=lambda: None)
    assert calls == [initial] and returned is final
    assert report['termination'] == termination and len(report['basin_attempts']) == 1


@pytest.mark.parametrize('exception_type', [SynthesisStageBudget, SynthesisResourceGuard, _StructureCancelled])
def test_parent_stop_between_chunks_propagates_and_starts_no_more_work(exception_type):
    request, _ = _case()
    request = replace(request, relative_improvement_tolerance=.01)
    initial = _candidate(1.)
    original = deepcopy(initial)
    calls = []
    checks = []
    stop = exception_type('parent stop')
    def refine(value):
        calls.append(value)
        return _candidate(.5, 1), dict(termination='iteration_limit')
    def check():
        checks.append(1)
        if len(checks) == 2:
            raise stop
    with pytest.raises(exception_type) as raised:
        refine_basin_within_quantum(request, initial, refine=refine, cancel_check=check)
    assert raised.value is stop
    assert calls == [initial] and len(checks) == 2 and initial == original


def test_stop_raised_inside_refine_propagates_without_hidden_retry():
    request, _ = _case()
    calls = []
    def refine(value):
        calls.append(value)
        raise SynthesisStageBudget('inner work exhausted')
    with pytest.raises(SynthesisStageBudget, match='inner work exhausted'):
        refine_basin_within_quantum(request, _candidate(1.), refine=refine, cancel_check=lambda: None)
    assert len(calls) == 1


def test_small_absolute_scale_uses_the_relative_tolerance_floor():
    request, _ = _case()
    request = replace(request, relative_improvement_tolerance=.1)
    initial, final = _candidate(1e-14), _candidate(0., 1)
    calls = []
    def refine(value):
        calls.append(value)
        return final, dict(termination='iteration_limit')
    returned, report = refine_basin_within_quantum(request, initial, refine=refine, cancel_check=lambda: None)
    # 1e-14 is below 0.1 * 1e-12; it does not admit another chunk.
    assert calls == [initial] and returned is final and len(report['basin_attempts']) == 1
