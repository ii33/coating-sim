"""Real stdio-process hybrid contracts, with no GUI or scheduler socket IPC."""
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event
import time
from typing import Any, Callable, Mapping, Sequence, TypedDict

import numpy as np
import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_parallel as parallel
from coat_sim.core.film_synthesis_batch_needle import scan_batch_needles
from coat_sim.core.film_synthesis_refinement import weighted_sse_loss_gradient
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def _problem(*, workers=1, budget=100, samples=31, memory=1024**3, batch_size=8):
    service, request = setup_problem(upper=0., low=450., high=700., samples=samples)
    return synthesis.freeze_film_synthesis_request(service, replace(request,
        search_strategy='hybrid', cpu_workers=workers, max_cycles=1,
        acceptable_normalized_error=0., max_evaluations=budget,
        max_working_set_bytes=memory, parallel_batch_size=batch_size))


def _physical_semantics(result):
    return (result.state, result.termination_reason, result.candidate_design,
        result.targets_met, result.solver_acceptable, result.evaluations,
        result.validation_evaluations, result.failed_evaluations,
        result.training_score, result.validation_score,
        [(t.stage, t.cycle, t.evaluations, t.layer_count, t.max_normalized_error,
          t.material_id, t.position_nm, t.sensitivity) for t in result.trace])


class _NeedleScanOptions(TypedDict):
    position_samples: int
    valley_refinement_rounds: int
    max_needles: int
    seed: int
    genuine_only: bool
    max_probes: int
    gradient: Callable[[Mapping[str, Any], Sequence[str]], Sequence[float]]


def test_real_hybrid_priors_global_and_needle_are_ordered_and_budgeted(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    frozen = _problem()
    serial = synthesis.synthesize_film_design(None, frozen)
    result = synthesis.synthesize_film_design(None, replace(frozen, cpu_workers=2))
    assert _physical_semantics(result) == _physical_semantics(serial)
    assert not result.targets_met and result.state == 'not_found'
    perf = result.performance
    assert perf['backend'] == 'headless_process'
    assert len(set(perf['worker_pids'])) >= 2
    assert perf['cleanup_complete'] and perf['active_worker_pids'] == []
    for phase in ('hybrid_prior', 'hybrid_global', 'hybrid_needle'):
        rows = [r for r in perf['batch_decisions'] if r['phase'] == phase]
        assert any(r['backend'] == 'headless_process' and len(r['worker_pids']) >= 2 for r in rows)
    batches = perf['search']['hybrid']['parallel_batches']
    for row in batches:
        assert row['reserved_slots'] == (row['consumed_slots'] + row['dispatched_unused_slots']
                                         + row['released_unsubmitted_slots'])
        assert row['completed_results'] <= row['consumed_slots']
    assert result.evaluations <= frozen.max_evaluations
    assert perf['submitted_tasks'] <= result.evaluations - result.validation_evaluations
    assert perf['completed_tasks'] + perf['unknown_tasks'] == perf['submitted_tasks']
    assert all(w['prepared_material_contexts'] == 1 for w in perf['workers'])
    assert all(w['frozen_request_identity'] == perf['frozen_request_identity'] for w in perf['workers'])
    assert all(set(w['thread_environment'].values()) == {'1'} for w in perf['workers'])


def test_real_analytic_probe_responses_jacobians_ids_and_refinement_order(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    frozen = _problem(samples=51)
    parent = deepcopy(frozen.environment)
    parent['front_layers'] = [dict(id=f'host-{i}', material_id=name, optical_set_id='nk',
        thickness=dict(value=thickness, unit='nm'))
        for i, (name, thickness) in enumerate((('low', 78.), ('high', 61.)))]
    request = synthesis._proxy(frozen)
    physical = parallel.physical._PreparedStructureEvaluator(request)
    observed_serial = []
    observed_parallel = []

    def evaluate(design, *, variable_ids=(), zero_ids=()):
        value = physical.evaluate(design, variable_ids=variable_ids, with_jacobian=True,
            zero_thickness_layer_ids=zero_ids, derived_validation_grid=False)
        observed_serial.append(value)
        return value

    executor = parallel.OrderedEvaluationExecutor(2)
    executor.configure(request)

    def evaluate_batch(tasks, *, evaluate):
        values = []
        planned = [parallel.EvaluationTask(t['design'], True, tuple(t['zero_ids']), retain_responses=True)
                   for t in tasks]
        with executor.batch(planned, count=len(planned), work=1, budget_allows=True,
                            phase='hybrid_needle', worker_limit=2) as outcomes:
            for _task, outcome in outcomes:
                value = executor.take(outcome)
                observed_parallel.append(value)
                values.append(value)
        return values

    def gradient(value: Mapping[str, Any], ids: Sequence[str]) -> Sequence[float]:
        return weighted_sse_loss_gradient(frozen, value, ids)[1]

    options: _NeedleScanOptions = _NeedleScanOptions(
        position_samples=2, valley_refinement_rounds=1, max_needles=4,
        seed=8, genuine_only=True, max_probes=24,
        gradient=gradient)
    serial = scan_batch_needles(parent, frozen.frozen_material_snapshot, frozen.candidate_materials,
                               evaluate=evaluate, **options)
    try:
        result = scan_batch_needles(parent, frozen.frozen_material_snapshot, frozen.candidate_materials,
            evaluate=evaluate, evaluate_batch=evaluate_batch, **options)
    finally:
        executor.close()
    assert result == serial
    assert result.refined_probe_count > 0
    assert len(observed_parallel) == len(observed_serial) == result.probe_count
    for expected, actual in zip(observed_serial, observed_parallel, strict=True):
        assert expected['design'] == actual['design']
        assert expected['score'] == actual['score']
        for a, b in zip(expected['responses'], actual['responses'], strict=True):
            assert a['wavelength_nm'] == b['wavelength_nm']
            np.testing.assert_array_equal(a['actual_values'], b['actual_values'])
            assert list(a['jacobian_values_by_layer_id']) == list(b['jacobian_values_by_layer_id'])
            for identifier in a['jacobian_values_by_layer_id']:
                np.testing.assert_array_equal(a['jacobian_values_by_layer_id'][identifier],
                                              b['jacobian_values_by_layer_id'][identifier])
    assert len(executor.diagnostics()['worker_pids']) == 2
    assert executor.diagnostics()['cleanup_complete']


def test_request_19_is_capped_by_frozen_total_memory_not_reported_as_19(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    request = _problem(workers=19, memory=448 * 1024**2, batch_size=None)
    result = synthesis.synthesize_film_design(None, request)
    perf = result.performance
    assert perf['requested_workers'] == 19
    assert perf['effective_workers'] == len(perf['worker_pids']) == 2
    batches = perf['search']['hybrid']['parallel_batches']
    assert max(r['resource_worker_cap'] for r in batches) == 2
    for row in batches:
        assert row['effective_workers'] <= row['resource_worker_cap'] <= min(19, row['available_cpu_workers'])
        assert row['estimated_peak_bytes'] <= row['frozen_budget_bytes']
    assert perf['cleanup_complete']


def test_low_memory_stays_serial_with_honest_resource_reason(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    result = synthesis.synthesize_film_design(None, _problem(workers=19, memory=256 * 1024**2))
    perf = result.performance
    assert perf['worker_pids'] == [] and perf['effective_workers'] == 1
    assert perf['backend'] == 'serial' and perf['cleanup_complete']
    assert perf['selection_reasons']['resource_guard_serial'] > 0
    assert max(row['resource_worker_cap'] for row in perf['search']['hybrid']['parallel_batches']) == 1


def test_startup_failure_has_exact_serial_fallback_and_same_physics(monkeypatch):
    request = _problem(budget=50)
    serial = synthesis.synthesize_film_design(None, request)
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    def denied_start(*args, **kwargs):
        raise OSError('test-owned stdio startup unavailable')
    monkeypatch.setattr(parallel.subprocess, 'Popen', denied_start)
    result = synthesis.synthesize_film_design(None, replace(request, cpu_workers=2))
    assert _physical_semantics(result) == _physical_semantics(serial)
    perf = result.performance
    assert perf['backend'] == 'serial' and perf['cleanup_complete']
    assert perf['serial_fallback_tasks'] > 0
    assert any('test-owned stdio startup unavailable' in s for s in perf['fallback_events'])


def test_cancel_in_real_probe_batch_charges_submitted_work_and_reaps_everything(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    cancel = Event()
    original = parallel.OrderedEvaluationExecutor._receive
    def receive(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self.batch_decisions and self.batch_decisions[-1]['phase'] == 'hybrid_needle' and self.started:
            cancel.set()
        return value
    monkeypatch.setattr(parallel.OrderedEvaluationExecutor, '_receive', receive)
    result = synthesis.synthesize_film_design(None, _problem(workers=2, samples=101), cancel_event=cancel)
    assert result.state == 'cancelled' and not result.targets_met
    perf = result.performance
    assert perf['cleanup_complete'] and perf['active_worker_pids'] == []
    assert perf['discarded_tasks'] > 0
    assert perf['completed_tasks'] + perf['unknown_tasks'] == perf['submitted_tasks']
    batches = perf['search']['hybrid']['parallel_batches']
    assert any(b['dispatched_unused_slots'] > 0 for b in batches)
    assert all(b['reserved_slots'] == b['consumed_slots'] + b['dispatched_unused_slots']
               + b['released_unsubmitted_slots'] for b in batches)
    assert result.frozen_request.max_evaluations is not None
    assert result.evaluations <= result.frozen_request.max_evaluations
    assert perf['training_evaluator_invocations'] is None or perf['unknown_tasks'] == 0


def test_formal_payload_job_and_cli_execute_real_hybrid_workers(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
    from coat_sim.core.film_synthesis_jobs import canonical_parameters, execute_job
    from coat_sim.film_synthesis_cli import main
    request = _problem(workers=2, budget=32, samples=11)
    payload = film_synthesis_request_to_payload('hybrid-real-stdio-entry', request)
    writes = []
    result = execute_job(canonical_parameters(None, {'payload': payload}), Event(),
        lambda *args, **kwargs: None, tmp_path / 'checkpoint.json',
        lambda path, value: writes.append((path, value)), str(tmp_path))
    perf = result['result']['performance']
    assert len(perf['worker_pids']) == 2 and perf['cleanup_complete']
    assert not result['result']['targets_met']
    request_path, output = tmp_path / 'request.json', tmp_path / 'result.json'
    request_path.write_text(json.dumps(payload), encoding='utf-8')
    code = main(['--request', str(request_path), '--output', str(output)])
    actual = json.loads(output.read_text(encoding='utf-8'))
    assert code in (0, 2)
    assert len(actual['result']['performance']['worker_pids']) == 2
    assert actual['result']['performance']['cleanup_complete']
    assert actual['result']['targets_met'] == result['result']['targets_met']
    assert actual['result']['candidate_design'] == result['result']['candidate_design']


def test_real_worker_crash_recomputes_logical_task_but_keeps_original_work_unknown(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    request = _problem(budget=64)
    expected = synthesis.synthesize_film_design(None, request)
    original = parallel.OrderedEvaluationExecutor._submit
    crashed: list[int] = []
    def submit(self, child, task):
        outcome = original(self, child, task)
        if (not crashed and self.batch_decisions[-1]['phase'] == 'hybrid_needle'
                and outcome.token in self.submitted):
            crashed.append(child.process.pid)
            child.process.terminate()  # Only the exact run-owned worker handle.
        return outcome
    monkeypatch.setattr(parallel.OrderedEvaluationExecutor, '_submit', submit)
    result = synthesis.synthesize_film_design(None, replace(request, cpu_workers=2))
    assert crashed and _physical_semantics(result) == _physical_semantics(expected)
    perf = result.performance
    assert perf['serial_fallback_tasks'] > 0 and perf['fallback_events']
    assert perf['unknown_tasks'] >= 1 and not perf['actual_work_complete']
    assert perf['training_physical_evaluations'] is None
    assert perf['known_training_physical_evaluations'] > 0
    assert perf['cleanup_complete'] and perf['active_worker_pids'] == []


def test_real_total_wall_deadline_closes_persistent_processes(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    request = replace(_problem(workers=2, budget=None, samples=301),
                      max_cycles=None, max_seconds=1.0)
    before = time.monotonic()
    result = synthesis.synthesize_film_design(None, request)
    elapsed = time.monotonic() - before
    assert result.state == 'not_found' and result.termination_reason == 'time_limit'
    assert not result.targets_met and elapsed < 5.0
    perf = result.performance
    assert perf['launcher_pids'], 'a real stdio pool must have started before the deadline'
    assert perf['cleanup_complete'] and perf['active_worker_pids'] == []
    assert perf['completed_tasks'] + perf['unknown_tasks'] == perf['submitted_tasks']


def test_frozen_identity_covers_full_materials_objectives_and_grid_and_cannot_rebind():
    request = _problem(samples=11)
    executor = parallel.OrderedEvaluationExecutor(2)
    executor.configure(synthesis._proxy(request))
    original_identity = executor.frozen_request_identity
    changed = replace(request, objectives=(replace(request.objectives[0], wavelength_samples=12),))
    other = parallel.OrderedEvaluationExecutor(2)
    other.configure(synthesis._proxy(changed))
    assert original_identity != other.frozen_request_identity
    altered = deepcopy(synthesis._proxy(request))
    altered.base_request.frozen_material_snapshot['materials'][0]['optical_sets'][0]['n'][0] += .01
    other.configure(altered)
    assert original_identity != other.frozen_request_identity
    assert executor.request is not None
    assert executor.request.base_request.frozen_material_snapshot != altered.base_request.frozen_material_snapshot
    try:
        executor._ensure_pool(2)
        with pytest.raises(RuntimeError, match='reconfigure'):
            executor.configure(altered)
    finally:
        executor.close()
        other.close()
    assert executor.diagnostics()['cleanup_complete']


@pytest.mark.parametrize('budget', [2, 3, 5, 9, 16, 33])
def test_small_total_budgets_reserve_final_audit_before_real_dispatch(monkeypatch, budget):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    result = synthesis.synthesize_film_design(None, _problem(workers=2, budget=budget, samples=7))
    perf = result.performance
    assert result.evaluations <= budget
    assert perf['known_training_evaluator_invocations'] + result.validation_evaluations == result.evaluations
    assert perf['cleanup_complete']
    for row in perf.get('search', {}).get('hybrid', {}).get('stages', []):
        if row['stage'] == 'engineering_prior_screen':
            assert row['evaluations_used'] <= row['admitted_evaluation_quantum']


def test_early_audited_success_reports_completed_unused_physical_candidates(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    request = _problem(workers=2, budget=40, samples=11)
    seed = deepcopy(request.environment)
    seed['front_layers'] = [dict(id='explicit-ar-seed', material_id='low', optical_set_id='nk',
        thickness=dict(value=550. / (4 * np.sqrt(1.5)), unit='nm'))]
    request = replace(request, objectives=(replace(request.objectives[0], wavelength_min_nm=520.,
        wavelength_max_nm=580., upper_bound=.005),), seed_designs=(seed,))
    result = synthesis.synthesize_film_design(None, request)
    assert result.targets_met and result.validation_complete
    perf = result.performance
    rows = perf['search']['hybrid']['parallel_batches']
    assert sum(r.get('completed_unadopted_results', 0) for r in rows) > 0
    assert perf['discarded_tasks'] > 0
    assert sum(r.get('discarded_tasks', 0) for r in perf['batch_decisions']) == perf['discarded_tasks']
    assert perf['completed_tasks'] == perf['submitted_tasks']
    assert perf['known_training_evaluator_invocations'] + result.validation_evaluations == result.evaluations
    assert perf['cleanup_complete']


def test_one_oversized_prior_does_not_discard_smaller_pending_sources(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    from coat_sim.core.film_synthesis_global import StructuralProposal
    from coat_sim.core import film_synthesis_multispectral_priors as priors
    request = _problem(workers=2, budget=40, memory=448 * 1024**2)
    small = deepcopy(request.environment)
    small['front_layers'] = [dict(id='small-pending-seed', material_id='high', optical_set_id='nk',
        thickness=dict(value=37., unit='nm'))]
    large = deepcopy(small)
    large['front_layers'] = [dict(id=f'oversized-{i}', material_id='high' if i % 2 else 'low',
        optical_set_id='nk', thickness=dict(value=40., unit='nm')) for i in range(20)]
    later = deepcopy(small)
    later['front_layers'][0]['id'] = 'small-later-source'
    later['front_layers'][0]['thickness']['value'] = 43.
    def mixed_sources(*args, **kwargs):
        for design, family in ((large, 'oversized-source'), (later, 'small-later-source')):
            yield StructuralProposal(design, dict(family=family, candidate_pair_budget=1,
                estimated_pair_demand=1, matching_end_layers=1))
    monkeypatch.setattr(priors, 'iter_multispectral_hr_proposals', mixed_sources)
    estimate = synthesis._synthesis_working_set_estimate_bytes
    def bounded(request, layers, **kwargs):
        return 2 * 1024**3 if layers >= 20 else estimate(request, layers, **kwargs)
    monkeypatch.setattr(synthesis, '_synthesis_working_set_estimate_bytes', bounded)
    result = synthesis.synthesize_film_design(None, replace(request, seed_designs=(small,)))
    proposals = [json.loads(t.message) for t in result.trace if t.stage == 'structure_proposal']
    assert any(p.get('family') == 'user_seed' for p in proposals)
    assert any(p.get('family') == 'small-later-source' for p in proposals)
    assert all(p.get('family') != 'oversized-source' for p in proposals)
    assert any(s['layer_count'] == 20 for s in result.performance['search']['hybrid']['resource_skips'])
    assert result.performance['cleanup_complete']


def test_memory_limited_needle_prefix_releases_full_jacobians_before_next_prefix(monkeypatch):
    import weakref
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    from coat_sim.core import film_synthesis_batch_needle as batch
    # Keep the physical parent thin so this isolates retained-result admission,
    # rather than rejecting a genuinely large numerical workset beforehand.
    from coat_sim.core import (film_synthesis_global as global_sources,
        film_synthesis_multispectral_priors as multi_sources,
        film_synthesis_matching as matching_sources,
        film_synthesis_ar_clusters as ar_sources, film_synthesis_hr_priors as hr_sources)
    for module, name in ((global_sources, 'iter_reflector_proposals'),
                         (global_sources, 'iter_bandpass_proposals'),
                         (multi_sources, 'iter_multispectral_hr_proposals'),
                         (matching_sources, 'iter_matching_proposals'),
                         (ar_sources, 'iter_ar_cluster_proposals'),
                         (hr_sources, 'iter_dual_substack_hr_proposals')):
        monkeypatch.setattr(module, name, lambda *args, **kwargs: iter(()))
    request = _problem(workers=19, budget=64, samples=1001, memory=129 * 1024**2)
    references = []
    alive_at_probe = []
    original = synthesis.evaluate_frozen_structure_candidate
    class ObservedValue(dict):
        pass
    def evaluate(*args, **kwargs):
        value = original(*args, **kwargs)
        if kwargs.get('zero_thickness_layer_ids'):
            value = ObservedValue(value)
            references.append(weakref.ref(value))
        return value
    monkeypatch.setattr(synthesis, 'evaluate_frozen_structure_candidate', evaluate)
    scan = batch.scan_batch_needles
    def observe_scan(*args, **kwargs):
        on_probe = kwargs['on_probe']
        def observed(proposal):
            alive_at_probe.append(sum(ref() is not None for ref in references))
            on_probe(proposal)
        kwargs['on_probe'] = observed
        return scan(*args, **kwargs)
    monkeypatch.setattr(batch, 'scan_batch_needles', observe_scan)
    result = synthesis.synthesize_film_design(None, request)
    rows = [r for r in result.performance['search']['hybrid']['parallel_batches']
            if r['phase'] == 'hybrid_needle']
    assert rows and alive_at_probe
    assert max(r['reserved_slots'] for r in rows) < request.parallel_batch_size
    assert max(alive_at_probe) <= max(r['reserved_slots'] for r in rows)
    assert all(r['retained_archive_estimate_bytes'] > 0 for r in rows)
    assert all(r['estimated_peak_bytes'] <= r['frozen_budget_bytes'] for r in rows)
    assert result.performance['cleanup_complete']


def test_full_response_transport_guard_uses_serial_without_adding_another_transport(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    request = _problem(workers=2, budget=32, samples=7)
    expected = synthesis.synthesize_film_design(None, replace(request, cpu_workers=1))
    monkeypatch.setattr(parallel, 'MAX_FRAME_BYTES', 1024)
    result = synthesis.synthesize_film_design(None, request)
    assert _physical_semantics(result) == _physical_semantics(expected)
    rows = result.performance['search']['hybrid']['parallel_batches']
    assert rows and all(r['full_response_frame_limited'] for r in rows)
    assert result.performance['worker_pids'] == []
    assert result.performance['cleanup_complete']


@pytest.mark.parametrize('invalid', [0, -1, True, 1.5, '19'])
def test_parallel_batch_size_rejects_nonpositive_noninteger_and_bool(invalid):
    with pytest.raises(ValueError, match='parallel_batch_size'):
        synthesis.synthesize_film_design(None, replace(_problem(), parallel_batch_size=invalid))


def test_auto_logical_batch_is_frozen_and_real_width_can_exceed_four(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    available = parallel.available_cpu_workers()
    expected = min(6, available)
    request = _problem(workers=6, budget=100, memory=2 * 1024**3, batch_size=None)
    assert request.parallel_batch_size == expected
    serial_request = replace(request, cpu_workers=1)
    replay = synthesis.freeze_film_synthesis_request(None, serial_request)
    assert replay.parallel_batch_size == request.parallel_batch_size
    serial = synthesis.synthesize_film_design(None, serial_request)
    result = synthesis.synthesize_film_design(None, request)
    assert _physical_semantics(result) == _physical_semantics(serial)
    perf = result.performance
    assert len(set(perf['worker_pids'])) == expected
    if available >= 5:
        assert len(set(perf['worker_pids'])) >= 5
    assert perf['effective_workers'] == expected and perf['requested_workers'] == 6
    assert len(perf['worker_cpu_by_pid']) == expected
    assert all(row['known_cpu_seconds'] > 0 and row['completed_tasks'] > 0 for row in perf['worker_cpu_by_pid'])
    rows = perf['search']['hybrid']['parallel_batches']
    assert all(r['source_prefix_batch_quantum'] == expected for r in rows)
    assert max(r['effective_workers'] for r in rows) == expected
    assert perf['cleanup_complete']


def test_legacy_auto_batch_default_remains_none_for_innocent_identity_compatibility():
    service, legacy = setup_problem()
    serial = synthesis.freeze_film_synthesis_request(service, legacy)
    parallel_request = synthesis.freeze_film_synthesis_request(service, replace(legacy, cpu_workers=6))
    assert serial.parallel_batch_size is None and parallel_request.parallel_batch_size is None


def test_cancelled_real_startup_reports_owned_processes_but_zero_dispatched_workers(monkeypatch):
    monkeypatch.setattr(parallel, 'MIN_PARALLEL_WORK', 0)
    cancel = Event()
    start = parallel.OrderedEvaluationExecutor._ensure_pool
    def cancel_after_ready(self, count=None):
        start(self, count)
        cancel.set()
        raise parallel.physical._StructureCancelled()
    monkeypatch.setattr(parallel.OrderedEvaluationExecutor, '_ensure_pool', cancel_after_ready)
    result = synthesis.synthesize_film_design(None, _problem(workers=2), cancel_event=cancel)
    assert result.state == 'cancelled' and not result.targets_met
    perf = result.performance
    assert perf['cleanup_complete'] and perf['submitted_tasks'] == 0
    decision = perf['batch_decisions'][0]
    assert decision['startup_state'] == 'cancelled'
    assert decision['backend'] == 'not_dispatched' and decision['effective_workers'] == 0
    assert len(decision['owned_worker_pids']) == 2
    batch = perf['search']['hybrid']['parallel_batches'][0]
    assert batch['consumed_slots'] == batch['dispatched_unused_slots'] == 0
    assert batch['released_unsubmitted_slots'] == batch['reserved_slots']
    assert batch['effective_workers'] == 0 and batch['startup_state'] == 'cancelled'


@pytest.mark.skipif(parallel.available_cpu_workers() < 2, reason="real multicore proof requires two available CPUs")
def test_real_request_19_auto_uses_actual_platform_cap_without_lowering_work_threshold(monkeypatch):
    """Bounded one-window physical request, not the failed four-window case."""
    from coat_sim.core import (film_synthesis_global as global_sources,
        film_synthesis_multispectral_priors as multi_sources,
        film_synthesis_matching as matching_sources,
        film_synthesis_ar_clusters as ar_sources, film_synthesis_hr_priors as hr_sources)
    # A deterministic small source scene: real supplied optical seeds and the
    # ordinary homogeneous family. Do not lower the production work threshold.
    assert parallel.MIN_PARALLEL_WORK == 200_000
    for module, name in ((global_sources, 'iter_reflector_proposals'),
                         (global_sources, 'iter_bandpass_proposals'),
                         (multi_sources, 'iter_multispectral_hr_proposals'),
                         (matching_sources, 'iter_matching_proposals'),
                         (ar_sources, 'iter_ar_cluster_proposals'),
                         (hr_sources, 'iter_dual_substack_hr_proposals')):
        monkeypatch.setattr(module, name, lambda *args, **kwargs: iter(()))
    platform = min(19, parallel.available_cpu_workers())
    # Fund a full logical batch even on 19-CPU hosts. The untouched production
    # cold threshold must also be reached on two-CPU CI (48-layer seed + film).
    request = _problem(workers=19, budget=max(64, 4 * platform + 8), samples=4097,
        memory=3 * 1024**3, batch_size=None)
    seeds = []
    for branch in range(16):
        design = deepcopy(request.environment)
        design['front_layers'] = [dict(id=f'bounded-{branch}-{i}',
            material_id='low' if i % 2 else 'high', optical_set_id='nk',
            thickness=dict(value=35. + i % 11 + (.1 * branch if i == 0 else 0.), unit='nm'))
            for i in range(48)]
        seeds.append(design)
    request = replace(request, seed_designs=tuple(seeds))
    serial = synthesis.synthesize_film_design(None, replace(request, cpu_workers=1))
    before = time.monotonic()
    result = synthesis.synthesize_film_design(None, request)
    elapsed = time.monotonic() - before
    assert _physical_semantics(result) == _physical_semantics(serial)
    perf = result.performance
    assert request.parallel_batch_size == platform
    assert perf['requested_workers'] == 19
    assert perf['effective_workers'] == len(set(perf['worker_pids'])) == platform
    if parallel.available_cpu_workers() >= 5:
        assert platform >= 5
    assert all(r['known_cpu_seconds'] > 0 and r['completed_tasks'] > 0 for r in perf['worker_cpu_by_pid'])
    assert perf['cleanup_complete'] and perf['active_worker_pids'] == []
    first = next(r for r in perf['batch_decisions'] if r.get('parallel_selected'))
    assert first['cold_work_threshold'] == 200_000
    assert first['selection_reason'] == 'cold_work_threshold'
    assert first['available_cpu_limit'] == parallel.available_cpu_workers()
    assert first['resource_worker_limit'] == platform
    print(json.dumps(dict(requested_workers=19, frozen_logical_quantum=request.parallel_batch_size,
        actual_effective_workers=perf['effective_workers'], worker_pids=perf['worker_pids'],
        available_cpu_limit=first['available_cpu_limit'], resource_worker_limit=first['resource_worker_limit'],
        selection_reason=first['selection_reason'], estimated_work=first['estimated_work'],
        full_parallel_run_seconds=elapsed, cleanup_complete=perf['cleanup_complete'],
        targets_met=result.targets_met, state=result.state,
        worker_cpu_by_pid=perf['worker_cpu_by_pid'],
        submitted_tasks=perf['submitted_tasks'], completed_tasks=perf['completed_tasks'],
        consumed_tasks=perf['consumed_tasks'], unknown_tasks=perf['unknown_tasks'],
        active_worker_pids=perf['active_worker_pids'],
        frozen_memory_budget_bytes=request.max_working_set_bytes,
        maximum_estimated_batch_bytes=max(row['estimated_peak_bytes']
            for row in perf['search']['hybrid']['parallel_batches']))))


def test_auto_resolution_is_hybrid_only_and_replay_never_resamples_logical_width(monkeypatch):
    from coat_sim.core import film_performance
    monkeypatch.setattr(film_performance, 'available_cpu_workers', lambda: 7)
    frozen = _problem(workers=0, batch_size=None)
    assert frozen.parallel_batch_size == 7
    monkeypatch.setattr(film_performance, 'available_cpu_workers', lambda: 2)
    replay = synthesis.freeze_film_synthesis_request(None, replace(frozen, cpu_workers=1))
    assert replay.parallel_batch_size == 7
    explicit = synthesis.freeze_film_synthesis_request(None, replace(frozen, parallel_batch_size=11))
    assert explicit.parallel_batch_size == 11
