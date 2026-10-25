"""Production scheduler adapter: real optics, immutable IPC and stop boundaries."""
from copy import deepcopy
from dataclasses import replace
from threading import Event
from typing import Any

import pytest

from coat_sim.core import film_manufacturing_jobs as jobs
from coat_sim.core.film_design import validate_design
from coat_sim.scheduler.jobs import JobSpec, freeze_spec
from tests.test_film_auto_synthesis import setup_problem

pytestmark = pytest.mark.algorithm


def request_fixture(*, samples=1, low=550., high=550., cap=None, max_evaluations=100):
    service, synthesis = setup_problem(upper=.1, samples=samples, low=low, high=high)
    source = deepcopy(synthesis.environment)
    source['front_layers'] = [dict(id=identifier, material_id=material, optical_set_id='nk',
        thickness=dict(value=thickness, unit='nm'), note='synthetic') for identifier, material, thickness in
        [('left', 'high', 40.), ('defect', 'low', .5), ('right', 'high', 40.)]]
    source = validate_design(source)
    synthesis = replace(synthesis, max_working_set_bytes=cap, max_evaluations=max_evaluations,
        validation_samples_multiplier=2)
    payload = jobs.make_request_payload(service, synthesis, source, defect_layer_ids=('defect',),
        reference_nm=8., refinement_evaluations_per_route=2, max_iterations_per_route=2,
        seconds_per_route=10.)
    return service, payload, jobs.build_job_payload(None, payload)


def execute(job, tmp_path, cancel=None, progress=None):
    from coat_sim.scheduler.io import write_json
    return jobs.execute_job({'payload': job}, cancel or Event(), progress or (lambda x: None),
        tmp_path / 'checkpoint.json', write_json)


def test_real_local_cpu_pair_roundtrip_and_full_band_evidence(tmp_path):
    _, request, job = request_fixture()
    result = execute(job, tmp_path)
    assert result['state'] == 'completed'
    assert result['termination_reason'] == 'complete'
    assert jobs.result_matches_job(result, job)
    body = result['result']
    assert body['source']['audit_score']['audit']['continuous_certificate'] is False
    assert len(body['routes']) == 2
    assert [r['route'] for r in body['routes']] == ['raise_selected', 'delete_selected_merge']
    assert body['routes'][0]['lower_bounds_nm']['defect'] == 8.
    assert body['routes'][1]['removed_ids'] == ['defect']
    for route in body['routes']:
        assert route['status'] == 'audited'
        assert route['champion']['training_score']['coverage']
        assert route['champion']['audit_score']['audit_coverage']
        assert route['champion']['audit_score']['audit']['objective_status']
    assert request == job['request']
    assert jobs.validate_result_payload(result, expected_job=job, replay=True) == result
    assert (tmp_path / 'checkpoint.json').is_file()


def test_queue_freeze_is_portable_and_source_revision_fenced(tmp_path):
    service, request, job = request_fixture()
    spec = freeze_spec(service, jobs.EXECUTOR, {'payload': request})
    assert JobSpec.parse(spec).parameters == {'payload': job}
    result = execute(job, tmp_path)
    changed = deepcopy(job)
    changed['request']['synthesis']['request']['design_revision'] += 1
    assert not jobs.result_matches_job(result, changed)
    changed = deepcopy(job)
    changed['request']['source_design']['front_layers'][0]['thickness']['value'] += 1.
    with pytest.raises(ValueError, match='stale'):
        jobs.deserialize_job_payload(changed)
    unknown = deepcopy(request)
    unknown['silent_default'] = 25
    with pytest.raises(ValueError):
        jobs.canonical_parameters(service, {'payload': unknown})


def test_cancel_before_source_never_dispatches(monkeypatch, tmp_path):
    _, _, job = request_fixture()
    monkeypatch.setattr(jobs, 'evaluate_frozen_structure_candidate', lambda *a, **k: pytest.fail('physical dispatch'))
    event = Event()
    event.set()
    result = execute(job, tmp_path, event)
    assert result['state'] == 'cancelled' and result['result'] is None
    assert result['work']['admitted_candidate_calls'] == 0


def test_source_and_complete_audit_share_candidate_budget(tmp_path):
    _, _, job = request_fixture(max_evaluations=2)
    result = execute(job, tmp_path)
    assert result['termination_reason'] == 'shared_budget'
    assert result['result']['source']['audit_score'] is not None
    assert not result['result']['routes'][0]['start_calls']
    assert result['work']['admitted_candidate_calls'] == 2


def test_full_audit_admission_before_dispatch_and_no_fixed_cap(monkeypatch, tmp_path):
    _, _, job = request_fixture(samples=5, low=520., high=580., cap=200 * 1024**2)
    request, source = jobs.deserialize_job_payload(job)
    bound = jobs.estimate_audit_resources(request.synthesis, source)
    assert bound['working_set_bytes'] > 200 * 1024**2
    assert bound['physical_evaluations_upper_bound'] > 1
    monkeypatch.setattr(jobs, 'audit_frozen_synthesis_candidate', lambda *a, **k: pytest.fail('unadmitted audit'))
    result = execute(job, tmp_path)
    assert result['termination_reason'] == 'resource_guard'
    assert result['result'] is None
    assert result['work']['completed_training_calls'] == 0
    assert result['work']['admitted_candidate_calls'] == 0
    _, _, unlimited = request_fixture(cap=None)
    parsed, _ = jobs.deserialize_job_payload(unlimited)
    assert parsed.synthesis.resource_guard_resolution is not None
    assert parsed.synthesis.resource_guard_resolution['effective_bytes'] is None


def test_cancel_bridge_reaches_existing_physical_chunks(monkeypatch, tmp_path):
    _, _, job = request_fixture(samples=10002, low=520., high=580.)
    from coat_sim.core import film_optimization_structure as structure
    original = structure.simulate_with_thickness_jacobian
    event = Event()
    calls = []
    def chunk(*args, **kwargs):
        calls.append(args[3]['samples'])
        value = original(*args, **kwargs)
        event.set()
        return value
    monkeypatch.setattr(structure, 'simulate_with_thickness_jacobian', chunk)
    result = execute(job, tmp_path, event)
    assert calls == [10001]
    assert result['state'] == 'cancelled'
    assert result['result'] is None
    assert result['work']['partial_physical_work_possible'] is True
    assert not result['work']['completed_training_calls']


def test_tampered_nominal_coverage_topology_and_checkpoint_rejected(tmp_path):
    _, _, job = request_fixture()
    result = execute(job, tmp_path)
    for change in ('nominal', 'coverage', 'topology', 'incumbent'):
        invalid = deepcopy(result)
        if change == 'nominal':
            invalid['result']['source']['training_score']['nominal_pass'] = not invalid['result']['source']['training_score']['nominal_pass']
        elif change == 'coverage':
            invalid['result']['source']['audit_score']['audit_coverage'] = []
        elif change == 'topology':
            invalid['result']['routes'][0]['lower_bounds_nm']['defect'] = 0.
        else:
            invalid['result']['spectral_incumbent'] = None
        assert not jobs.result_matches_job(invalid, job)
    import json
    checkpoint = json.loads((tmp_path / 'checkpoint.json').read_text())
    assert jobs.validate_checkpoint(checkpoint, expected_job=job)['state'] == 'cancelled'
    checkpoint['job']['request']['reference_nm'] = 9.
    with pytest.raises(ValueError):
        jobs.validate_checkpoint(checkpoint, expected_job=job)


def test_terminal_cancel_wins_without_physical_replay(monkeypatch, tmp_path):
    _, _, job = request_fixture()
    result = execute(job, tmp_path)
    monkeypatch.setattr(jobs, 'evaluate_frozen_structure_candidate', lambda *a, **k: pytest.fail('replay on cancel'))
    from coat_sim.scheduler.worker import _completed_film_cancel_reply, _film_reply_matches_payload
    from coat_sim.scheduler.result_capability import job_has_viewable_result
    saved = _completed_film_cancel_reply(result, job)
    assert saved['state'] == 'cancelled'
    assert saved['termination_reason'] == 'scheduler_cancel_won'
    assert saved['result'] == result['result']
    assert _film_reply_matches_payload(saved, job)
    assert job_has_viewable_result({'kind': jobs.EXECUTOR, 'state': 'cancelled', 'result': saved})
    assert not job_has_viewable_result({'kind': 'process', 'state': 'succeeded', 'result': saved})


def test_remote_contract_explicitly_rejects_local_only_executor():
    from coat_sim.scheduler.remote_contract import RemoteContractError, _validate_spec
    _, request, _ = request_fixture()
    spec = JobSpec.parse(freeze_spec(None, jobs.EXECUTOR, {'payload': request}))
    with pytest.raises(RemoteContractError, match='executor'):
        _validate_spec(spec, allow_probe=False)


@pytest.mark.parametrize('max_evaluations, expected_state', [(100, 'succeeded'), (2, 'failed')])
def test_actual_scheduler_worker_terminal_and_readonly_result(tmp_path, max_evaluations, expected_state):
    import time
    from tests.scheduler_queue_support import SchedulerHarness
    _, request, job = request_fixture(max_evaluations=max_evaluations)
    spec = freeze_spec(None, jobs.EXECUTOR, {'payload': request})
    harness = SchedulerHarness(tmp_path / 'isolated-scheduler')
    client = harness.client()
    try:
        queued = client.submit(spec)
        deadline = time.monotonic() + 30.
        while True:
            harness.tick()
            status = client.status(queued['job_id'])
            if status['state'] in ('succeeded', 'failed', 'cancelled'):
                break
            assert time.monotonic() < deadline, status
            time.sleep(.025)
        assert status['state'] == expected_state, status
        result = client.result(queued['job_id'])
        assert jobs.result_matches_job(result, job)
        assert result['result']['source']['audit_score'] is not None
        assert result['state'] == ('completed' if expected_state == 'succeeded' else 'failed')
    finally:
        harness.close()


def test_cancel_after_first_complete_trial_keeps_raw_evidence_and_does_not_audit_route(tmp_path):
    _, _, job = request_fixture()
    event = Event()
    def progress(item):
        if item['stage'] == 'manufacturing:manufacturing_repair_trial':
            event.set()
    result = execute(job, tmp_path, event, progress)
    assert result['state'] == 'cancelled'
    assert result['result']['source']['audit_score'] is not None
    first, second = result['result']['routes']
    assert first['champion']['training_score']['coverage']
    assert first['champion']['audit_score'] is None
    assert first['completed_training_calls'] == 1 and first['audit_calls'] == 0
    assert second['start_calls'] == 0
    assert result['result']['best_repaired'] is None
    assert result['result']['spectral_incumbent'] == result['result']['source']
    assert jobs.result_matches_job(result, job)


def test_declared_worker_external_owner_can_block_before_any_optics(monkeypatch, tmp_path):
    from coat_sim.scheduler.io import write_json
    _, _, job = request_fixture(cap=512 * 1024**2)
    monkeypatch.setattr(jobs, 'evaluate_frozen_structure_candidate', lambda *a, **k: pytest.fail('external owner bypass'))
    result = jobs.execute_job({'payload': job}, Event(), lambda item: None, tmp_path / 'c.json', write_json,
        external_retained_bytes=lambda: 1024**3)
    assert result['termination_reason'] == 'resource_guard'
    assert result['result'] is None and result['work']['admitted_candidate_calls'] == 0
    assert result['work']['peak_estimated_bytes'] > 1024**3


def test_mean_only_full_audit_reserves_coverage_without_inventing_pointwise():
    from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
    _, _, job = request_fixture(samples=5, low=520., high=580.)
    request, design = jobs.deserialize_job_payload(job)
    mean_only = replace(request.synthesis, objectives=tuple(replace(o,
        mean=FilmObjectiveConstraint(enabled=True, upper_bound=.1),
        pointwise=FilmObjectiveConstraint(enabled=False)) for o in request.synthesis.objectives))
    bound = jobs.estimate_audit_resources(mean_only, design)
    assert bound['physical_evaluations_upper_bound'] == 2
    assert bound['wavelength_points_upper_bound'] == 17
    assert bound['working_set_bytes'] > 128 * 1024**2
    assert not mean_only.objectives[0].pointwise.enabled


def test_completed_work_ledger_matches_actual_existing_kernel_calls(monkeypatch, tmp_path):
    _, _, job = request_fixture(samples=3, low=520., high=580.)
    from coat_sim.core import film_optimization_structure as structure
    original = structure.simulate_with_thickness_jacobian
    calls = []
    def counted(*args, **kwargs):
        value = original(*args, **kwargs)
        calls.append((bool(args[2]), args[3]['samples'], len(args[0]['front_layers'])))
        return value
    monkeypatch.setattr(structure, 'simulate_with_thickness_jacobian', counted)
    result = execute(job, tmp_path)
    assert result['state'] == 'completed'
    work = result['work']
    assert work['completed_kernel_FJ_calls'] == sum(int(fj) for fj, points, layers in calls)
    assert work['completed_kernel_F_calls'] == sum(int(not fj) for fj, points, layers in calls)
    assert work['completed_training_wavelength_points'] + work['completed_audit_wavelength_points'] == sum(points for fj, points, layers in calls)
    assert work['completed_film_layer_wavelength_points'] == sum(points * layers for fj, points, layers in calls)
    assert work['partial_physical_work_possible'] is False


def test_chunk_domain_guard_matches_existing_evaluator_without_layer_cap():
    class TooMany:
        def __len__(self):
            return 1_000_001
    with pytest.raises(ValueError, match='chunk limit cannot satisfy'):
        jobs._physical_chunk_size({'front_layers': TooMany(), 'back_layers': []})


def test_combined_output_role_alias_lifetimes_are_reserved_without_optics(monkeypatch):
    import json
    import tracemalloc
    from coat_sim.core.film_objective_criteria import reduce_objective_responses
    from coat_sim.core.film_optimization_scoring import reduction_score
    from coat_sim.core.film_synthesis_audit import objective_constraint_status
    from coat_sim.core.film_synthesis_manufacturing import (
        ManufacturingRepairResult, _owned_bytes, manufacturing_snapshot, prepare_manufacturing_routes,
    )
    _, request_payload, job = request_fixture()
    request, source_design = jobs.deserialize_job_payload(job)
    monkeypatch.setattr(jobs, 'evaluate_frozen_structure_candidate', lambda *a, **k: pytest.fail('ownership test dispatched optics'))
    monkeypatch.setattr(jobs, 'audit_frozen_synthesis_candidate', lambda *a, **k: pytest.fail('ownership test dispatched audit'))
    def snapshot(design, actual, audited=False):
        responses = [dict(objective_index=0, angle_deg=0., wavelength_nm=[550.], actual_values=[actual])]
        reduction = reduce_objective_responses(request.synthesis.objectives, responses,
            tolerance=request.synthesis.acceptable_normalized_error)
        score = reduction_score(reduction, request.synthesis.acceptable_normalized_error)
        audit_value = None
        if audited:
            audit_score = deepcopy(score)
            audit_score['audit_coverage'] = deepcopy(score['coverage'])
            audit_score['audit'] = dict(physical_evaluations=1, wavelength_points=1,
                dense_points=1, offset_points=0, extra_points=0, unique_condition_points=1,
                bracket_count=0, completed_extrema=0, continuous_certificate=False,
                rms_measure='fixed_dense_grid_only', mean_measure='fixed_dense_grid_only',
                objective_status=objective_constraint_status(request.synthesis.objectives,
                    audit_score['errors'], request.synthesis.acceptable_normalized_error))
            audit_value = dict(design=design, score=audit_score)
        return manufacturing_snapshot(request.synthesis, dict(design=design, score=score), audit_value=audit_value)
    source = snapshot(source_design, .5, True)
    routes = []
    for prepared in prepare_manufacturing_routes(request, source):
        current_design, best_design = deepcopy(prepared.design), deepcopy(prepared.design)
        current_design['front_layers'][0]['thickness']['value'] += 1.
        best_design['front_layers'][0]['thickness']['value'] += 2.
        routes.append(replace(prepared, status='audited', start=snapshot(prepared.design, .4),
            current=snapshot(current_design, .3), champion=snapshot(best_design, .2, True),
            start_calls=1, refinement_calls=2, audit_calls=1, completed_training_calls=3, completed_audit_calls=1))
    assert routes[0].champion is not None
    core = ManufacturingRepairResult('complete', source, tuple(routes), routes[0].champion,
        routes[0].champion, 0, None, .1)
    work: dict[str, Any] = {name: 0 for name in ('admitted_candidate_calls', 'completed_training_calls', 'completed_audit_calls',
        'completed_audit_physical_evaluations', 'completed_audit_wavelength_points', 'completed_kernel_F_calls',
        'completed_kernel_FJ_calls', 'reserved_kernel_calls_upper_bound', 'completed_training_wavelength_points',
        'completed_film_layer_wavelength_points', 'reserved_physical_evaluations_upper_bound',
        'reserved_wavelength_points_upper_bound', 'peak_estimated_bytes')}
    work.update(admitted_candidate_calls=10, completed_training_calls=7, completed_audit_calls=3,
        shared_budget_unit='complete candidate calls; one whole audit counts as one; physical work is reported separately',
        partial_physical_work_possible=False,
        external_retained_ownership='synthetic identity/lifetime evidence only; no OS isolation claim')
    resources = jobs.estimate_result_resources(request_payload, request.synthesis, source_design)
    assert resources['expanded_training_score_occurrences'] == 9
    assert resources['expanded_audit_score_occurrences'] == 5
    distinct = [source] + [getattr(route, role) for route in routes for role in ('start', 'current', 'champion')]
    assert len({id(value) for value in distinct}) == 7
    assert core.best_repaired is core.spectral_incumbent is routes[0].champion
    original_bytes = _owned_bytes(core, job, work)
    tracemalloc.start()
    payload = jobs._envelope(job, core, 'complete', work, resources=resources)
    normalized = jobs.validate_result_payload(payload, expected_job=job, replay=False)
    _, observed_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    expanded, encoded_bound = jobs._expanded_json_sizes(payload)
    assert expanded >= _owned_bytes(payload)
    assert encoded_bound >= len(json.dumps(payload, allow_nan=False).encode())
    assert expanded <= resources['expanded_projection_bytes_upper_bound']
    assert encoded_bound <= resources['encoded_json_bytes_upper_bound']
    # Existing original result, payload and normalized return remain live here;
    # tracemalloc covers allocations in envelope + complete validator only.
    assert _owned_bytes(core, payload, normalized, job, work) <= resources['phase_working_set_bytes']
    assert observed_peak + original_bytes < resources['phase_working_set_bytes']
    print({'observed_envelope_and_validation_peak': observed_peak, 'original_owned_bytes': original_bytes,
           'reserved_combined_output_bytes': resources['phase_working_set_bytes']})


def test_nonallocating_projection_bound_handles_unicode_and_alias_expansion():
    import json
    from coat_sim.core.film_synthesis_manufacturing import _owned_bytes
    shared = {'notes': ('😀\x00中' * 1000), 'values': [1., -1e308, None, True]}
    raw = {'source': shared, 'current': shared, 'champion': shared}
    projection, encoded = jobs._expanded_json_sizes(raw)
    decoded = json.loads(json.dumps(raw, allow_nan=False))
    assert projection > _owned_bytes(raw)
    assert projection >= _owned_bytes(decoded)
    assert encoded >= len(json.dumps(raw, allow_nan=False).encode())


@pytest.mark.parametrize('value', [{}, [], {'': ''}, [''], ['', '', '', ''],
    {'nested': {'unicode': ['😀']}}, [[[[1]]]], {'nested': {'tiny': [{'x': [None]}]}}])
def test_projection_bound_covers_small_decoded_container_capacities(value):
    import json
    from coat_sim.core.film_synthesis_manufacturing import _owned_bytes
    projection, encoded = jobs._expanded_json_sizes(value)
    decoded = json.loads(json.dumps(value, allow_nan=False))
    assert projection >= _owned_bytes(decoded)
    assert encoded >= len(json.dumps(value, allow_nan=False).encode())


def test_cancel_at_source_audit_keeps_complete_training_only_evidence(tmp_path):
    _, _, job = request_fixture()
    event = Event()
    def progress(item):
        if item['stage'] == 'manufacturing:source:audit':
            event.set()
    result = execute(job, tmp_path, event, progress)
    assert result['state'] == 'cancelled'
    assert result['result']['source']['training_score']['coverage']
    assert result['result']['source']['audit_score'] is None
    assert result['result']['routes'] == []
    assert result['work']['completed_training_calls'] == 1
    assert result['work']['completed_audit_calls'] == 0
    assert jobs.result_matches_job(result, job)


def test_malformed_manufacturing_discriminator_cannot_claim_generic_best_asset():
    from coat_sim.scheduler.result_capability import is_best_candidate_result, job_has_viewable_result
    forged = {'kind': jobs.RESULT_KIND,
        'partial_result': {'schema_version': 1, 'kind': 'best_candidate', 'best_so_far': True,
                          'candidate_verified': True, 'quality': {'metric': 'error', 'value': 0.}},
        'asset': {'run_id': 'a' * 32, 'temporary_bundle_path': '/tmp/not-authority'}}
    assert not is_best_candidate_result(forged)
    assert not job_has_viewable_result({'kind': jobs.EXECUTOR, 'state': 'failed', 'result': forged})


def test_combined_output_dominates_cap_before_first_physical_dispatch(monkeypatch, tmp_path):
    from coat_sim.core.film_synthesis_manufacturing import manufacturing_refinement_working_set_bytes
    cap = 130 * 1024**2
    _, request_payload, job = request_fixture(cap=cap)
    request, source = jobs.deserialize_job_payload(job)
    numerical = manufacturing_refinement_working_set_bytes(request.synthesis, len(source['front_layers']))
    output = jobs.estimate_result_resources(request_payload, request.synthesis, source)['phase_working_set_bytes']
    assert numerical < cap < output
    monkeypatch.setattr(jobs, 'evaluate_frozen_structure_candidate', lambda *a, **k: pytest.fail('output admission bypass'))
    result = execute(job, tmp_path)
    assert result['termination_reason'] == 'resource_guard'
    assert result['work']['admitted_candidate_calls'] == 0
    assert result['work']['completed_training_calls'] == 0
    assert result['work']['peak_estimated_bytes'] >= output
    assert result['result'] is None
