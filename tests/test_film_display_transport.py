"""Owned-file and in-memory display transport, without sockets or child workers."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from coat_sim.scheduler.client import SchedulerClient
from coat_sim.scheduler.daemon import Scheduler
from coat_sim.scheduler.desktop import observe_job
from coat_sim.scheduler.film_display import (
    CONFIG_KEY, MAX_DISPLAY_BYTES, FilmDisplayRegistry, WorkerDisplaySession,
    display_configuration_for_job, prepare_display_configuration, validate_live_display_packet,
)
from coat_sim.scheduler.io import write_json, write_text
from coat_sim.scheduler.jobs import JobSpec, canonical, freeze_spec
from coat_sim.scheduler.store import SchedulerStore
from tests.test_film_candidate_display import objective, record
from tests.test_film_optimization_forward_cache import _request
from tests.film_performance_fixtures import fixture


def configured(*, enabled=False, generation='a' * 32):
    return prepare_display_configuration({'schema_version': 1, 'generation': generation,
        'score_config': {}, 'enabled': enabled,
        'selection': {'objective_index': 0, 'angle_deg': 0., 'metric': 'R'}})


def row(*, enabled=False):
    return {'job_id': 'display-job', 'kind': 'film_optimization', 'state': 'running',
        'input_digest': 'b' * 64, 'cancel_requested': False, CONFIG_KEY: configured(enabled=enabled)}


def session_pair(tmp_path, *, enabled=False):
    item = row(enabled=enabled)
    directory = tmp_path / 'artifacts/runtime/scheduler/jobs' / item['job_id']
    directory.mkdir(parents=True)
    registry = FilmDisplayRegistry(tmp_path)
    registry.start(item, directory)
    now = [0.]
    session = WorkerDisplaySession(directory, item['job_id'], item['input_digest'],
        item[CONFIG_KEY], clock=lambda: now[0])
    observer = session.observer_for_request(SimpleNamespace(objectives=(objective(),)))
    assert observer is not None
    return item, directory, registry, session, observer, now


def control(registry, item, *, enabled, expected=0, selection=None):
    return registry.control(item, input_digest=item['input_digest'],
        generation=item[CONFIG_KEY]['generation'], enabled=enabled,
        selection=selection or item[CONFIG_KEY]['selection'], expected_sequence=expected)


def test_configuration_is_frozen_outside_physical_digest_and_legacy_generation_stable(tmp_path):
    service, design, frozen = fixture(layers=2)
    spec = freeze_spec(service, 'film_optimization', {'request': _request(design, frozen, budget=2)})
    digest = JobSpec.parse_frozen(spec).fingerprint
    store = SchedulerStore(tmp_path)
    config = configured()
    admitted = store.submit(spec, 'frozen-job', {CONFIG_KEY: config})
    config['enabled'] = True
    assert admitted['input_digest'] == digest
    assert store.status('frozen-job')[CONFIG_KEY]['enabled'] is False
    retried = store.submit(spec, 'frozen-job', {CONFIG_KEY: configured(enabled=True, generation='c' * 32)})
    assert retried[CONFIG_KEY] == admitted[CONFIG_KEY]
    legacy = {key: value for key, value in admitted.items() if key != CONFIG_KEY}
    assert display_configuration_for_job(legacy) == display_configuration_for_job(deepcopy(legacy))
    assert display_configuration_for_job(legacy)['enabled'] is False
    assert display_configuration_for_job(dict(legacy, job_id='other'))['generation'] != display_configuration_for_job(legacy)['generation']
    with pytest.raises(ValueError, match='not_supported'):
        store.submit(JobSpec('probe', {}, {}).to_dict(), 'probe-display', {CONFIG_KEY: configured()})


def test_off_metadata_on_dynamic_off_no_spectrum_provider_or_copy(tmp_path):
    item, directory, registry, session, observer, now = session_pair(tmp_path)
    record(observer, best=True)
    assert registry.poll(item, directory)
    packet = registry.attach_job(item)['film_display']
    assert not packet['display']['enabled'] and packet['display']['top10']
    assert observer.spectral_snapshot_builds == observer.spectral_points_copied == observer.spectral_serializations == 0
    assert observer.metadata_only_packets == 1
    control(registry, item, enabled=True)
    now[0] = 1.
    record(observer, 90., best=True)
    assert registry.poll(item, directory)
    assert registry.attach_job(item)['film_display']['display']['best']['spectrum']['actual'] == [.1] * 3
    prior = (observer.spectral_snapshot_builds, observer.spectral_points_copied, observer.spectral_serializations)
    control(registry, item, enabled=False, expected=1)
    now[0] = 2.
    observer.refresh(spectrum_provider=lambda *_args: pytest.fail('off invoked spectral provider'))
    assert registry.poll(item, directory)
    packet = registry.attach_job(item)['film_display']
    assert packet['control_sequence'] == 2 and not packet['display']['enabled']
    assert all(packet['display'][role]['spectrum'] is None for role in ('current', 'best'))
    assert prior == (observer.spectral_snapshot_builds, observer.spectral_points_copied, observer.spectral_serializations)
    assert (directory / 'film-display.json').stat().st_size <= MAX_DISPLAY_BYTES
    assert session.published_packets == 3


def test_cas_late_old_on_cannot_overwrite_new_off_and_old_equal_ack_does_not_rewrite(tmp_path):
    item, directory, registry, _session, _observer, _now = session_pair(tmp_path)
    assert control(registry, item, enabled=True)['sequence'] == 1
    assert control(registry, item, enabled=False, expected=1)['sequence'] == 2
    before = (directory / 'film-display-control.json').read_bytes()
    with pytest.raises(ValueError, match='stale'):
        control(registry, item, enabled=True, expected=0)
    ack = control(registry, item, enabled=False, expected=0)
    assert ack['sequence'] == 2 and (directory / 'film-display-control.json').read_bytes() == before
    for change in ({'state': 'succeeded'}, {'cancel_requested': True}):
        with pytest.raises(ValueError, match='not_active'):
            control(registry, dict(item, **change), enabled=True, expected=2)
    with pytest.raises(ValueError, match='identity_mismatch'):
        control(registry, dict(item, input_digest='d' * 64), enabled=True, expected=2)


def test_corrupt_oversized_stale_wrong_generation_packets_drop_without_losing_last_good(tmp_path):
    item, directory, registry, session, observer, now = session_pair(tmp_path, enabled=True)
    record(observer, best=True)
    assert registry.poll(item, directory)
    good = registry.attach_job(item)['film_display']
    for mutate in (lambda p: p.update(generation='c' * 32),
                   lambda p: p.update(control_sequence=99),
                   lambda p: p['display'].update(sequence=0),
                   lambda p: p['display'].update(extra=[]),
                   lambda p: p['display'].update(sequence=2, context_id='another-frozen-context'),
                   lambda p: p['display'].update(sequence=2, scope='audit')):
        candidate = deepcopy(good)
        mutate(candidate)
        write_json(directory / 'film-display.json', candidate)
        assert not registry.poll(item, directory)
        assert registry.attach_job(item)['film_display'] == good
    (directory / 'film-display.json').write_text('x' * (MAX_DISPLAY_BYTES + 1))
    assert not registry.poll(item, directory)
    for bad_json in ('{bad', '[' * 1500 + '0' + ']' * 1500):
        (directory / 'film-display.json').write_text(bad_json)
        assert not registry.poll(item, directory)
    session.publish({'bad': []})
    assert session.dropped_packets == 1
    control_path = directory / 'film-display-control.json'
    control_path.write_text('{bad')
    now[0] = 1.
    record(observer, 90., best=True)
    assert observer.spectral_snapshot_builds == 2  # Only the first enabled current/best.
    assert not json.loads((directory / 'film-display.json').read_text())['display']['enabled']
    registry.start(item, directory)
    now[0] = 2.
    record(observer, 80., best=True)
    assert registry.poll(item, directory)
    assert registry.attach_job(item)['film_display']['display']['best']['total_thickness_nm'] == 80.


def test_display_only_updates_never_enter_store_or_event_buffer_and_wake_observers(tmp_path):
    scheduler = Scheduler(tmp_path)
    spec = JobSpec('film_optimization', {}, {}).to_dict()
    admitted = scheduler.dispatch('submit', {'spec': spec, 'job_id': 'display-job',
        'metadata': {CONFIG_KEY: configured(enabled=True)}})
    directory = scheduler.runtime / 'jobs' / admitted['job_id']
    directory.mkdir(parents=True)
    scheduler.film_display.start(admitted, directory)
    session = WorkerDisplaySession(directory, admitted['job_id'], admitted['input_digest'], admitted[CONFIG_KEY])
    observer = session.observer_for_request(SimpleNamespace(objectives=(objective(),)))
    sequence, original_events = scheduler.sequence, deepcopy(list(scheduler.event_buffer))
    before = scheduler.store.status(admitted['job_id'])
    output, started = [], Event()
    def listen():
        started.set()
        output.append(scheduler.events(after_sequence=sequence, wait_s=2))
    thread = Thread(target=listen)
    thread.start()
    started.wait(1)
    with scheduler.condition:
        record(observer, best=True)
        assert scheduler.film_display.poll(admitted, directory)
        scheduler.condition.notify_all()
    thread.join(3)
    assert not thread.is_alive() and output[0]['film_displays']
    assert scheduler.sequence == sequence and list(scheduler.event_buffer) == original_events
    assert scheduler.store.status(admitted['job_id']) == before
    assert 'film_display' not in scheduler.index[admitted['job_id']]
    assert scheduler.memory_status(admitted['job_id'])['film_display']
    assert scheduler.memory_snapshot()['jobs'][0]['film_display']
    with scheduler.store.queue.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM scheduler_events').fetchone()[0] == 1
        assert 'wavelength_nm' not in db.execute('SELECT payload FROM scheduler_events').fetchone()[0]
    scheduler.stopping.set()
    with pytest.raises(ValueError, match='scheduler_stopping'):
        scheduler.dispatch('film_display_control', {'job_id': admitted['job_id'],
            'input_digest': admitted['input_digest'], 'generation': admitted[CONFIG_KEY]['generation'],
            'enabled': False, 'selection': admitted[CONFIG_KEY]['selection'], 'expected_sequence': 0})


def test_client_control_reads_server_sequence_but_never_blindly_retries(tmp_path):
    calls = []
    item = row()
    item['film_display_control_sequence'] = 7
    class Transport:
        def request(self, action, args):
            calls.append((action, args))
            if action == 'status':
                return deepcopy(item)
            raise ValueError('film_display_control_stale')
    client = SchedulerClient(tmp_path, transport=Transport())
    with pytest.raises(ValueError, match='stale'):
        client.film_display_control(item['job_id'], input_digest=item['input_digest'],
            generation=item[CONFIG_KEY]['generation'], enabled=True, selection=item[CONFIG_KEY]['selection'])
    assert [name for name, _args in calls] == ['status', 'film_display_control']
    assert calls[-1][1]['expected_sequence'] == 7


def test_desktop_observer_delivers_display_only_updates_once_and_final_first(tmp_path):
    item, directory, registry, _session, observer, now = session_pair(tmp_path, enabled=True)
    record(observer, best=True)
    registry.poll(item, directory)
    initial = registry.attach_job(item)
    now[0] = 1.
    observer.finish(state='succeeded')
    registry.poll(item, directory)
    final = registry.attach_job(dict(item, state='succeeded', result={'official': True}))
    class Transport:
        def __init__(self):
            self.count = 0
        def request(self, action, _args):
            if action == 'snapshot':
                return {'jobs': [initial], 'sequence': 1}
            if action == 'events':
                self.count += 1
                if self.count == 1:
                    return {'sequence': 1, 'events': [], 'film_displays': [initial['film_display']]}
                return {'sequence': 2, 'events': [dict(final, sequence=2)], 'film_displays': []}
            raise AssertionError(action)
    packets: list[dict[str, Any]] = []
    assert observe_job(SchedulerClient(tmp_path, transport=Transport()), item['job_id'],
        display=packets.append, wait_s=0) == {'official': True}
    assert len(packets) == 2 and packets[-1]['display']['terminal']
    assert validate_live_display_packet(packets[-1], generation='c' * 32) is None


@pytest.mark.parametrize('executor', ['film_optimization', 'film_synthesis', 'film_sensitivity',
                                     'film_ideal_sensitivity', 'film_thickness_error_table'])
def test_worker_dispatches_optional_runtime_observer_without_progress_arrays(tmp_path, monkeypatch, executor):
    from coat_sim.scheduler import worker
    spec = JobSpec(executor, {}, {}).to_dict()
    item = SchedulerStore(tmp_path).submit(spec, 'worker-dispatch',
        {CONFIG_KEY: configured(enabled=True)} if executor != 'film_thickness_error_table' else {})
    directory = tmp_path / 'artifacts/runtime/scheduler/jobs/worker-dispatch'
    directory.mkdir(parents=True)
    write_text(directory / 'spec.json', canonical(spec))
    write_json(directory / 'launch.json', {'instance': 'owned-launch'})
    FilmDisplayRegistry(tmp_path).start(item, directory)
    from coat_sim.core import isolated_compute, data_files
    monkeypatch.setattr(isolated_compute, '_in_scheduler_worker', isolated_compute._in_scheduler_worker)
    monkeypatch.setattr(data_files, 'PROJECT_ROOT', data_files.PROJECT_ROOT)
    monkeypatch.setattr(data_files, 'DATA_ROOT', data_files.DATA_ROOT)
    monkeypatch.setattr(worker, 'FileCancelEvent', lambda *_args: Event())
    monkeypatch.setattr(worker, '_materialize', lambda _service, current, _directory: current.parameters)
    import coat_sim.core.api
    monkeypatch.setattr(coat_sim.core.api, 'CoatService', lambda root: SimpleNamespace(root=root))
    calls = []
    def execute(*_args, **kwargs):
        factory = kwargs.get('candidate_observer_factory')
        calls.append(factory)
        if factory is not None:
            observed = factory(SimpleNamespace(objectives=(objective(),)))
            record(observed, best=True)
        return {'state': 'completed', 'official': True}
    if executor == 'film_optimization':
        monkeypatch.setattr(worker, '_film_optimization', execute)
    else:
        import importlib
        module = importlib.import_module('coat_sim.core.' + {
            'film_synthesis': 'film_synthesis_jobs', 'film_sensitivity': 'film_sensitivity_jobs',
            'film_ideal_sensitivity': 'film_sensitivity_workflow_jobs',
            'film_thickness_error_table': 'film_sensitivity_workflow_jobs'}[executor])
        monkeypatch.setattr(module, 'execute_job', execute)
    assert worker.run(tmp_path, 'worker-dispatch', 1234, 'owned-launch') == 0
    outcome = json.loads((directory / 'outcome.json').read_text())
    assert outcome['state'] == 'succeeded' and outcome['result']['official']
    assert len(calls) == 1 and (calls[0] is None) == (executor == 'film_thickness_error_table')
    if executor != 'film_thickness_error_table':
        packet = validate_live_display_packet(json.loads((directory / 'film-display.json').read_text()),
            job_id=item['job_id'], input_digest=item['input_digest'], generation=item[CONFIG_KEY]['generation'])
        assert packet is not None and packet['display']['terminal']
        assert packet['display']['top10']
    else:
        assert not (directory / 'film-display.json').exists()
    assert not (directory / 'progress.json').exists()


@pytest.mark.parametrize('kind', ['ordinary', 'structure', 'synthesis', 'sensitivity', 'constrained',
                                 'ideal', 'ideal_v2', 'analysis', 'table'])
def test_real_job_wrappers_observe_only_complete_optimization_candidates(tmp_path, kind):
    from coat_sim.core import film_optimization_job, film_synthesis_jobs, film_sensitivity_jobs
    from coat_sim.core import film_sensitivity_workflow_jobs
    from tests.test_film_sensitivity_candidate_display import case
    packet_list: list[dict[str, Any]] = []
    factories: list[Any] = []
    def factory(request):
        factories.append(request)
        from coat_sim.core.film_candidate_display import CandidateDisplayObserver
        return CandidateDisplayObserver(context_id='frozen-wrapper-test', objectives=request.objectives,
            strict_lower_bound_objective_indices=getattr(request, 'strict_lower_bound_objective_indices', ()),
            strict_upper_bound_objective_indices=getattr(request, 'strict_upper_bound_objective_indices', ()),
            control_provider=lambda: {'enabled': False}, publish_callback=packet_list.append)
    if kind in ('ordinary', 'structure'):
        if kind == 'ordinary':
            service, design, snapshot = fixture(layers=2)
            request = _request(design, snapshot, budget=3, samples=2)
        else:
            from tests.test_film_optimization_structure import _service, _request as structure_request
            service, design = _service()
            request = structure_request(design, max_evaluations=3, max_iterations=1)
        spec = freeze_spec(service, 'film_optimization', {'request': request})
        result = film_optimization_job.execute_film_optimization_job(service, spec['parameters']['payload'],
            cancel_event=Event(), candidate_observer_factory=factory)
    elif kind == 'synthesis':
        from tests.test_film_synthesis_jobs import _payload
        payload = film_synthesis_jobs.build_job_payload(film_synthesis_jobs.canonical_parameters(None, {'payload': _payload()}))
        result = film_synthesis_jobs.execute_job({'payload': payload}, Event(), lambda _item: None,
            tmp_path / 'checkpoint.json', write_json, execution_project_root=str(tmp_path),
            candidate_observer_factory=factory)
    elif kind in ('sensitivity', 'constrained', 'analysis'):
        if kind == 'analysis':
            from tests.test_film_sensitivity import problem
            service, request = problem()
            from coat_sim.core.film_sensitivity import freeze_film_sensitivity_request
            request = freeze_film_sensitivity_request(service, request)
        else:
            request, _execute = case(kind, budget=3)
        payload = film_sensitivity_jobs.build_job_payload(None, request)
        result = film_sensitivity_jobs.execute_job({'payload': payload}, Event(), lambda _item: None,
            tmp_path / 'checkpoint.json', write_json, candidate_observer_factory=factory)
    else:
        if kind == 'table':
            from tests.test_film_sensitivity_workflow_jobs import problem as workflow_problem
            service, request = workflow_problem('film_thickness_error_table')
            request = film_sensitivity_workflow_jobs.freeze_workflow_request(service, request)
        else:
            request, _execute = case(kind, budget=3)
        payload = film_sensitivity_workflow_jobs.build_job_payload(None, request)
        result = film_sensitivity_workflow_jobs.execute_job({'payload': payload}, Event(), lambda _item: None,
            tmp_path / 'checkpoint.json', write_json, candidate_observer_factory=factory)
    assert result
    if kind in ('analysis', 'table'):
        assert not factories and not packet_list
    else:
        assert len(factories) == 1 and packet_list and packet_list[-1]['terminal']
        assert packet_list[-1]['top10'] and not packet_list[-1]['enabled']
        assert all(packet[role] is None or packet[role]['spectrum'] is None
                   for packet in packet_list for role in ('current', 'best'))


@pytest.mark.parametrize('bound', [False, True])
def test_remote_display_configuration_is_optional_metadata_not_spec_or_digest(tmp_path, bound):
    from coat_sim.scheduler.remote_client import RemoteTransport
    from coat_sim.scheduler.remote_contract import validate_submission
    service, design, snapshot = fixture(layers=2)
    spec = freeze_spec(service, 'film_optimization', {'request': _request(design, snapshot, budget=2)})
    transport: Any = RemoteTransport('https://service.test', device_id='cpu:default' if bound else None,
        pending_root=tmp_path / 'pending')
    calls, blobs = [], {}
    def upload(body):
        digest = hashlib.sha256(body).hexdigest()
        blobs[digest] = body
        return {'sha256': digest, 'bytes': len(body)}
    transport.upload_blob = upload
    def respond(method, path, body):
        calls.append((method, path, body))
        return {'job_id': 'remote-display', 'input_summary': {'device_id': transport.device_id,
            'external_spec_sha256': body['spec_blob']['sha256']}}
    transport._json_call = respond
    config = configured(enabled=True)
    transport.submit(spec, 'remote-display', {CONFIG_KEY: config})
    envelope = calls[-1][2]
    _job_id, parsed, metadata = validate_submission(envelope, blobs.__getitem__)
    assert metadata[CONFIG_KEY] == config and CONFIG_KEY not in parsed.to_dict()
    plain = dict(envelope)
    plain.pop(CONFIG_KEY)
    _job_id, without, no_metadata = validate_submission(plain, blobs.__getitem__)
    assert parsed.fingerprint == without.fingerprint and CONFIG_KEY not in no_metadata
    assert envelope['spec_blob']['sha256'] == hashlib.sha256(canonical(parsed.to_dict()).encode()).hexdigest()


def test_remote_rpc_display_control_closed_fields_and_server_cas(tmp_path):
    from coat_sim.scheduler.remote_server import RemoteComputeService, ServiceError
    service: Any = RemoteComputeService.__new__(RemoteComputeService)
    service.scheduler = Scheduler(tmp_path)
    item = service.scheduler.dispatch('submit', {'spec': JobSpec('film_optimization', {}, {}).to_dict(),
        'job_id': 'remote-display', 'metadata': {CONFIG_KEY: configured()}})
    arguments = {'job_id': item['job_id'], 'input_digest': item['input_digest'],
        'generation': item[CONFIG_KEY]['generation'], 'enabled': True,
        'selection': item[CONFIG_KEY]['selection'], 'expected_sequence': 0}
    def rpc(args):
        return service._rpc({'api_version': 1, 'action': 'film_display_control', 'arguments': args})
    assert rpc(arguments)['sequence'] == 1
    assert rpc(dict(arguments, enabled=False, expected_sequence=1))['sequence'] == 2
    with pytest.raises(ValueError, match='stale'):
        rpc(arguments)
    with pytest.raises(ServiceError):
        rpc(dict(arguments, sequence=88))
    missing = dict(arguments)
    missing.pop('generation')
    with pytest.raises(ServiceError):
        rpc(missing)


def test_remote_submit_preserves_display_metadata_at_durable_admission(tmp_path):
    from coat_sim.scheduler.remote_server import RemoteComputeService
    from tests.test_remote_contract import _envelope
    service, design, snapshot = fixture(layers=2)
    spec = freeze_spec(service, 'film_optimization', {'request': _request(design, snapshot, budget=2)})
    envelope, blobs = _envelope(JobSpec.parse(spec), job_id='admitted-remote')
    envelope[CONFIG_KEY] = configured(enabled=True)
    remote: Any = RemoteComputeService.__new__(RemoteComputeService)
    remote.scheduler = Scheduler(tmp_path)
    remote.allow_probe = False
    remote.blobs = SimpleNamespace(read=blobs.__getitem__)
    admitted = remote._submit(envelope)
    assert admitted[CONFIG_KEY] == envelope[CONFIG_KEY]
    assert remote.scheduler.store.status('admitted-remote')[CONFIG_KEY] == envelope[CONFIG_KEY]
    assert admitted['input_digest'] == JobSpec.parse_frozen(spec).fingerprint
    retried = remote._submit(dict(envelope, film_display_config=configured(generation='c' * 32)))
    assert retried[CONFIG_KEY] == admitted[CONFIG_KEY]


def test_daemon_drains_final_packet_published_between_first_poll_and_process_exit(tmp_path, monkeypatch):
    scheduler = Scheduler(tmp_path)
    scheduler.dispatch('submit', {'spec': JobSpec('film_optimization', {}, {}).to_dict(),
        'job_id': 'terminal-race', 'metadata': {CONFIG_KEY: configured(enabled=True)}})
    item, _spec = scheduler._commit('claim', scheduler.owner)
    directory = scheduler.runtime / 'jobs' / item['job_id']
    directory.mkdir(parents=True)
    scheduler.film_display.start(item, directory)
    session = WorkerDisplaySession(directory, item['job_id'], item['input_digest'], item[CONFIG_KEY])
    observer = session.observer_for_request(SimpleNamespace(objectives=(objective(),)))
    record(observer, best=True)
    class FinishingProcess:
        def poll(self):
            observer.finish(state='completed')
            write_json(directory / 'outcome.json', {'state': 'succeeded', 'result': {'official': True}})
            return 0
    scheduler.active = (item['job_id'], FinishingProcess(), directory, None)
    monkeypatch.setattr(scheduler, 'register_artifacts', lambda _job_id, result: result)
    scheduler.stopping.set()  # Never claim or launch any other work in this test.
    scheduler.tick()
    final = scheduler.memory_status(item['job_id'])
    assert final['state'] == 'succeeded' and final['film_display']['display']['terminal']
    event = scheduler.events()['events'][-1]
    assert event['state'] == 'succeeded' and event['film_display']['display']['terminal']
    assert 'film_display' not in scheduler.event_buffer[-1]


def test_remote_override_publishes_control_before_owned_worker_launch(tmp_path, monkeypatch):
    from coat_sim.scheduler import remote_server
    scheduler = remote_server.RemoteScheduler(tmp_path)
    spec = JobSpec('film_optimization', {}, {}).to_dict()
    admitted = scheduler.dispatch('submit', {'spec': spec, 'job_id': 'remote-launch',
        'metadata': {CONFIG_KEY: configured(enabled=True)}})
    directory = scheduler.runtime / 'jobs' / admitted['job_id']
    launched = []
    class FakeProcess:
        pid = 12345
    def launch(*_args, **_kwargs):
        control_value = json.loads((directory / 'film-display-control.json').read_text())
        assert control_value['generation'] == admitted[CONFIG_KEY]['generation']
        assert control_value['enabled']
        launched.append(True)
        return FakeProcess()
    monkeypatch.setattr(remote_server.subprocess, 'Popen', launch)
    monkeypatch.setattr(scheduler.guard, 'attach', lambda _process: None)
    monkeypatch.setattr(scheduler.store, 'set_worker', lambda *_args: None)
    monkeypatch.setattr(remote_server, '_process_identity', lambda _pid: ('alive', 'fake-start'))
    scheduler.start_job(admitted, spec)
    assert launched and (directory / 'launch.json').is_file()
    scheduler.active = None


def test_registry_rebuild_restores_control_epoch_without_mutating_frozen_config(tmp_path):
    item, directory, original, session, observer, now = session_pair(tmp_path)
    control(original, item, enabled=True)
    now[0] = .2
    assert session.control()['enabled']
    control(original, item, enabled=False, expected=1)
    now[0] = .4
    assert not session.control()['enabled']
    rebuilt = FilmDisplayRegistry(tmp_path)
    status = rebuilt.attach_job(item)
    assert status['film_display_control_sequence'] == 2
    assert status['film_display_control_available']
    assert status[CONFIG_KEY] == item[CONFIG_KEY]  # Initial off remains frozen.
    ack = control(rebuilt, item, enabled=True, expected=2)
    assert ack['sequence'] == 3
    now[0] = 1.
    record(observer, best=True)
    assert session.control()['enabled'] and rebuilt.poll(item, directory)
    assert rebuilt.attach_job(item)['film_display']['control_sequence'] == 3
    assert rebuilt.attach_job(item)[CONFIG_KEY]['enabled'] is False


@pytest.mark.parametrize('bad', ['missing_after_launch', 'corrupt', 'recursive', 'oversized', 'wrong_generation'])
def test_rebuilt_unknown_control_epoch_fails_closed_without_false_ack_or_physics(tmp_path, bad):
    item, directory, original, session, observer, now = session_pair(tmp_path, enabled=True)
    control(original, item, enabled=False)
    now[0] = .2
    assert not session.control()['enabled']
    path = directory / 'film-display-control.json'
    if bad == 'missing_after_launch':
        write_json(directory / 'launch.json', {'instance': 'existing-worker'})
        path.unlink()
    elif bad == 'corrupt':
        path.write_text('{bad')
    elif bad == 'recursive':
        path.write_text('[' * 1100 + '0' + ']' * 1100)
    elif bad == 'oversized':
        path.write_text('x' * 4097)
    else:
        value = json.loads(path.read_text())
        value['generation'] = 'c' * 32
        write_json(path, value)
    rebuilt = FilmDisplayRegistry(tmp_path)
    status = rebuilt.attach_job(item)
    assert not status['film_display_control_available']
    with pytest.raises(ValueError, match='control_unavailable'):
        control(rebuilt, item, enabled=True)
    now[0] = 1.
    record(observer, best=True)
    assert not session.control()['enabled']
    assert observer.metadata_only_packets == 1 and observer.spectral_snapshot_builds == 0
    assert session.published_packets == 1


def test_historical_status_reads_do_not_grow_live_registry(tmp_path):
    registry = FilmDisplayRegistry(tmp_path)
    for index in range(500):
        item = dict(row(), job_id='history-' + str(index), state='succeeded')
        status = registry.attach_job(item)
        assert not status['film_display_control_available'] and 'film_display' not in status
    assert not registry._states
