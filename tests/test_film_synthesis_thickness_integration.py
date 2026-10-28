"""Default hybrid thickness cost stage: parent budget, audit and opt-out."""
from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_thickness as thickness
from tests.test_film_synthesis_thickness import physical_fixture

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def request_case(family='AR', mode='pointwise', *, enabled=True, budget=300):
    service, request, initial, _, _ = physical_fixture(family, mode)
    request = replace(request, search_strategy='hybrid', seed_designs=(deepcopy(initial['design']),),
        max_cycles=1, max_evaluations=budget, cpu_workers=1, parallel_batch_size=1,
        enable_thickness_reduction=enabled)
    return service, request, initial


def run_case(service, request, **kwargs):
    return synthesis.synthesize_film_design(service, request, **kwargs)


def total(result):
    return sum(r['thickness']['value'] for r in result.candidate_design['front_layers'])


@pytest.mark.parametrize('family', ['HR', 'AR', 'mixed_RT'])
@pytest.mark.parametrize('mode', ['mean', 'pointwise', 'both'])
def test_real_default_hybrid_enabled_disabled_cross_family(family, mode):
    service, request, initial = request_case(family, mode, budget=None)
    disabled = run_case(service, replace(request, enable_thickness_reduction=False))
    enabled = run_case(service, request)
    assert disabled.targets_met and enabled.targets_met
    assert total(enabled) <= total(disabled)
    assert enabled.validation_score['nominal_pass'] and enabled.training_score['nominal_pass']
    assert len(enabled.candidate_design['front_layers']) == len(initial['design']['front_layers'])
    off = disabled.performance['search']['hybrid']
    on = enabled.performance['search']['hybrid']
    assert 'thickness_reduction' not in off
    detail = on['thickness_reduction']
    assert detail['max_iterations_per_call'] == 16  # Local numerical protection, not a total work cap.
    assert detail['solver_calls']
    stage = next(row for row in on['stages'] if row['stage'] == 'nominal_thickness_reduction')
    assert stage['admitted_evaluation_quantum'] is None and stage['wall_time_share'] is None
    assert detail['initial_physical_hash']
    assert enabled.evaluations >= disabled.evaluations
    assert enabled.cycles == disabled.cycles == 1  # Early prior success, not another search rotation.
    assert any(row.stage == 'thickness_reduction_complete' for row in enabled.trace)
    stages = [row.stage for row in enabled.trace]
    assert stages.index('local_margin_complete') < stages.index('thickness_reduction_complete')
    assert on['local_margin']['status'] in {'converged', 'no_interval_margin', 'empty_topology'}
    assert [r['id'] for r in enabled.local_optimum['design']['front_layers']] == on['local_margin']['fixed_layer_ids']
    assert not any(row.stage.startswith('thickness_reduction') for row in disabled.trace)
    if detail['promoted']:
        assert detail['independent_audit_performed'] and total(enabled) < total(disabled)
    else:
        assert total(enabled) == total(disabled)


def test_legacy_route_is_unchanged_by_new_default(monkeypatch):
    service, request, _ = request_case()
    def forbidden(*args, **kwargs):
        pytest.fail('hybrid cost stage must never enter legacy route')
    monkeypatch.setattr(thickness, 'reduce_total_thickness', forbidden)
    a = run_case(service, replace(request, search_strategy='legacy', enable_thickness_reduction=False))
    b = run_case(service, replace(request, search_strategy='legacy', enable_thickness_reduction=True))
    assert a.state == b.state and a.evaluations == b.evaluations
    assert a.candidate_design == b.candidate_design


def test_low_remaining_budget_does_not_start_optional_solver(monkeypatch):
    service, request, _ = request_case(budget=4)
    request = replace(request, enable_local_margin_refinement=False)
    monkeypatch.setattr(thickness, 'reduce_total_thickness', lambda *a, **k: pytest.fail('no training slot'))
    result = run_case(service, request)
    assert result.targets_met and result.evaluations <= 4
    # This budget only establishes success during the parent's reserved final
    # audit, not during normal hybrid search. No post-budget cost hook runs.
    assert 'thickness_reduction' not in result.performance['search']['hybrid']


def test_mean_quality_guard_keeps_incumbent_instead_of_consuming_margin():
    service, request, _ = request_case(mode='mean')
    # The old cost-only policy consumed this mean margin and reached an
    # off-grid rejection. Preserving each enabled optical group now blocks
    # that physical tradeoff before there is a thinner candidate to audit.
    request = replace(request, enable_local_margin_refinement=False)
    off = run_case(service, replace(request, enable_thickness_reduction=False))
    result = run_case(service, request)
    record = result.performance['search']['hybrid']['thickness_reduction']
    assert not record['independent_audit_performed'] and not record['promoted']
    assert record['status'] == 'no_quality_preserving_improvement'
    assert result.targets_met and result.candidate_design == off.candidate_design
    assert result.validation_score['nominal_pass']
    assert total(result) == total(off)


def cost_stage_with_real_improving_trials():
    service, request, _ = request_case()
    seed = deepcopy(request.seed_designs[0])
    # A thicker, still nominally passing real AR seed can become thinner while
    # improving R. Disabling the preceding margin solve keeps that work for
    # the cost stage, so cancel/deadline tests reach actual physical trials.
    seed['front_layers'][0]['thickness']['value'] = 130.
    return service, replace(request, seed_designs=(seed,), enable_local_margin_refinement=False)


def test_explicit_budget_limits_cost_work_without_an_internal_stage_cap(monkeypatch):
    service, request, _ = request_case(budget=60)
    calls = []
    def consume(request, initial, *, evaluate, archive, cancel_check, **kwargs):
        assert kwargs['max_iterations'] == 16
        for _ in range(100):
            cancel_check()
            value = evaluate(initial['design'], jacobian=True)
            calls.append(value)
            archive(value, audit_eligible=False)
        pytest.fail('explicit total evaluation budget did not stop repeated requests')
    monkeypatch.setattr(thickness, 'reduce_total_thickness', consume)
    result = run_case(service, request)
    detail = result.performance['search']['hybrid']['thickness_reduction']
    assert result.targets_met and detail['status'] == 'evaluation_limit'
    assert detail['stop_reason'] == 'evaluation_limit'
    assert 32 < len(calls) < 60  # The former private stage cap cannot stop this work.
    # The parent reserves one original-audit slot. Once stopped, an unapproved
    # cost trial must not spend that slot on a new terminal audit.
    assert result.evaluations == request.max_evaluations - 1 == 59
    assert detail['promoted'] is False


@pytest.mark.parametrize('at', ['before', 'during'])
def test_cancel_never_launches_later_optics_or_audit(monkeypatch, at):
    service, request = cost_stage_with_real_improving_trials()
    cancel = Event()
    entered = []
    actual = thickness.reduce_total_thickness
    def wrapper(*args, **kwargs):
        entered.append(True)
        return actual(*args, **kwargs)
    monkeypatch.setattr(thickness, 'reduce_total_thickness', wrapper)
    progress = []
    def observe(row):
        progress.append(row)
        if (at == 'before' and row.stage == 'validation_complete' and row.layer_count > 0
                or at == 'during' and row.stage == 'thickness_reduction_trial'):
            cancel.set()
    result = run_case(service, request, cancel_event=cancel, progress_callback=observe)
    assert result.state == 'cancelled'
    assert bool(entered) is (at == 'during')
    cancel_index = next(i for i, row in enumerate(progress) if
        (at == 'before' and row.stage == 'validation_complete' and row.layer_count > 0
         or at == 'during' and row.stage == 'thickness_reduction_trial'))
    assert not any(row.stage in ('validation', 'thickness_reduction_trial') for row in progress[cancel_index + 1:])


def test_time_deadline_during_cost_stage_does_not_trigger_terminal_audit(monkeypatch):
    service, request = cost_stage_with_real_improving_trials()
    now = [0.]
    monkeypatch.setattr(synthesis, 'monotonic', lambda: now[0])
    events = []
    def observe(row):
        events.append(row)
        if row.stage == 'thickness_reduction_trial':
            now[0] = 100.
    result = run_case(service, replace(request, max_seconds=10.), progress_callback=observe)
    assert result.termination_reason == 'time_limit' and not result.targets_met
    stop = next(i for i, row in enumerate(events) if row.stage == 'thickness_reduction_trial')
    assert not any(row.stage in ('validation', 'thickness_reduction_trial') for row in events[stop + 1:])


def test_combined_resource_estimate_blocks_even_when_components_each_fit(monkeypatch):
    service, request, _ = request_case()
    # Parent prior/TMM work plus its small archive fits 160 MiB; the staged
    # numerical estimate alone also fits. Their sum deliberately does not.
    limit = 160 * 1024**2
    request = replace(request, max_working_set_bytes=limit, resource_guard_resolution=None,
                      enable_local_margin_refinement=False)
    monkeypatch.setattr(thickness, 'thickness_reduction_working_set_bytes', lambda request, layers: limit)
    monkeypatch.setattr(thickness, 'reduce_total_thickness', lambda *a, **k: pytest.fail('combined work not admitted'))
    result = run_case(service, request)
    assert result.targets_met
    record = result.performance['search']['hybrid']['thickness_reduction']
    assert record['status'] == 'resource_guard'
    assert record['numerical_estimate_bytes'] <= limit
    assert record['retained_archive_estimate_bytes'] <= limit
    assert record['estimated_peak_bytes'] > limit
    assert record['training_evaluations_used'] == 0
    assert not record['independent_audit_performed'] and not record['promoted']
