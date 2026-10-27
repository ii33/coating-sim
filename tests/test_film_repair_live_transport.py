"""Job-bound live repair IPC: real optics and worker, scalar queue progress."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time
from typing import Any

import pytest

from coat_sim.core import film_automatic_repair_jobs as jobs
from coat_sim.core.compute_tasks import _compact_progress
from coat_sim.scheduler.client import SchedulerClient
from coat_sim.scheduler.daemon import Scheduler, _repair_live_json, _public_repair_live_payload, public_payload
from coat_sim.scheduler.io import write_json, write_text
from coat_sim.scheduler.jobs import JobSpec, freeze_spec
from coat_sim.scheduler.worker import _RepairLivePublisher
from tests.test_film_automatic_repair import permissive_problem
from tests.test_scheduler_service import stop_service, wait_state

pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def frozen_request(*, source_note=None, request_id="transport-repair"):
    # A broad physical tolerance isolates transport from repair feasibility.
    # Every snapshot still comes from the real TMM and independent audit.
    service, synthesis, source = permissive_problem()
    if source_note is not None:
        source['note'] = source_note
        if source_note:
            source['name'] = source_note
            source['workflow_settings'] = {'simulation': {'annotation': 'C:/synthetic-label/frozen-state'}}
    payload = jobs.make_live_request_payload(service, synthesis, source,
        reference_nm=1., thickness_tolerance_nm=.1,
        optical_tolerance_percentage_points=100., request_id=request_id)
    return freeze_spec(service, 'film_manufacturing_repair', {'payload': payload}), payload


@pytest.fixture(scope='module')
def live_frames():
    spec, request = frozen_request()
    frames: list[dict[str, Any]] = []
    cancel = Event()

    def report(frame):
        frames.append(jobs.validate_live_progress(frame, request))
        if frame['champion'] is not None:
            cancel.set()

    result = jobs.execute_job(JobSpec.parse_frozen(spec).parameters, cancel, report,
        Path('unused-transport-checkpoint.json'), lambda *_args, **_kwargs: None)
    assert result['state'] == 'cancelled'
    assert frames[-1]['status'] == 'cancelled'
    assert frames[-1]['champion'] is not None
    return spec, request, frames


class DispatchTransport:
    """Exercise the real daemon dispatcher without starting its claim loop."""
    def __init__(self, scheduler):
        self.scheduler = scheduler

    def request(self, action, arguments):
        return self.scheduler.dispatch(action, arguments)


@pytest.fixture
def bound_job(tmp_path, live_frames):
    spec, request, frames = live_frames
    scheduler = Scheduler(tmp_path)
    client = SchedulerClient(tmp_path, transport=DispatchTransport(scheduler))
    row = client.submit(spec, job_id='live-bound')
    directory = scheduler.runtime / 'jobs' / row['job_id']
    directory.mkdir(parents=True)
    parsed = JobSpec.parse_frozen(spec)
    write_text(directory / 'spec.json', parsed.canonical_json)
    try:
        yield scheduler, client, row, directory, request, frames
    finally:
        scheduler.guard.close()


def test_progress_keeps_only_integer_live_marker_and_existing_scalars():
    raw = dict(stage='repair_live', repair_live_sequence=4, seq=4,
        source={'design': [1, 2]}, current={'design': [3]}, champion={'design': [4]},
        work={'physical_calls': 50}, request_hash='private-frame-identity', fraction=.25)
    assert _compact_progress(raw) == dict(stage='repair_live', fraction=.25, repair_live_sequence=4)
    for invalid in (True, 1., -1, '4', None):
        assert 'repair_live_sequence' not in _compact_progress(dict(raw, repair_live_sequence=invalid))


def test_client_uses_read_only_action_with_frozen_digest(monkeypatch):
    client = SchedulerClient(Path.cwd())
    calls = []
    monkeypatch.setattr(client, '_call', lambda action, **kwargs: calls.append((action, kwargs)))
    client.repair_live('repair-1', input_digest='a' * 64, after_sequence=9)
    assert calls == [('repair_live', dict(start=False, job_id='repair-1', input_digest='a' * 64,
                                         after_sequence=9))]


def test_publisher_coalesces_preview_but_forces_champion_and_cancel(tmp_path, live_frames):
    _spec, request, frames = live_frames
    clock = [1.]
    publisher = _RepairLivePublisher(tmp_path, request, clock=lambda: clock[0])
    source = deepcopy(frames[0])
    source['seq'] = 1
    assert publisher.publish(source)
    preview = deepcopy(source)
    preview['seq'] = 2
    assert not publisher.publish(preview)
    assert publisher.sequence == 1 and publisher.seen_sequence == 2
    best = deepcopy(next(frame for frame in frames if frame['champion'] is not None))
    best['seq'] = 3
    assert publisher.publish(best)
    assert json.loads(publisher.path.read_text(encoding='utf-8')) == best
    cancelled = deepcopy(best)
    cancelled.update(seq=4, status='cancelled')
    assert publisher.publish(cancelled)
    assert json.loads(publisher.path.read_text(encoding='utf-8'))['champion'] == best['champion']
    with pytest.raises(ValueError, match='after_terminal'):
        publisher.publish(dict(cancelled, seq=5))


def test_bad_stale_or_oversize_publication_preserves_previous_atomically(tmp_path, live_frames):
    _spec, request, frames = live_frames
    publisher = _RepairLivePublisher(tmp_path, request)
    good = deepcopy(frames[0])
    assert publisher.publish(good)
    original = publisher.path.read_bytes()
    bad = deepcopy(good)
    bad.update(seq=good['seq'] + 1, request_hash='0' * 64)
    with pytest.raises(ValueError, match='identity'):
        publisher.publish(bad)
    with pytest.raises(ValueError, match='sequence'):
        publisher.publish(good)
    publisher.MAX_BYTES = 1
    with pytest.raises(ValueError, match='byte_limit'):
        publisher.publish(dict(good, seq=good['seq'] + 2), force=True)
    assert publisher.path.read_bytes() == original
    assert not list(tmp_path.glob('*.tmp'))


def test_bound_read_is_newer_complete_static_frame_without_physical_replay(bound_job, monkeypatch):
    scheduler, client, row, directory, request, frames = bound_job
    monkeypatch.setattr(jobs, '_replay_live_result', lambda *_a, **_k: pytest.fail('replay'))
    assert client.repair_live(row['job_id'], input_digest=row['input_digest']) is None
    write_json(directory / 'repair-live.json', frames[-1])
    actual = client.repair_live(row['job_id'], input_digest=row['input_digest'])
    assert actual == frames[-1]
    assert jobs.applicable_improvement(actual['champion'], actual['source'], request)
    assert client.repair_live(row['job_id'], input_digest=row['input_digest'],
                              after_sequence=actual['seq']) is None
    # Status/events retain their compact existing contract, without geometry.
    assert 'champion' not in client.status(row['job_id'])['progress']
    assert 'repair-live.json' not in json.dumps(scheduler.memory_snapshot())


@pytest.mark.parametrize('corruption', ['truncated', 'request', 'source', 'version', 'sequence'])
def test_bound_read_rejects_bad_or_cross_request_snapshot(bound_job, corruption):
    _scheduler, client, row, directory, _request, frames = bound_job
    bad = deepcopy(frames[-1])
    if corruption == 'request':
        bad['request_hash'] = '0' * 64
    elif corruption == 'source':
        bad['source_hash'] = '0' * 64
    elif corruption == 'version':
        bad['schema_version'] = 100
    elif corruption == 'sequence':
        bad['seq'] = True
    path = directory / 'repair-live.json'
    if corruption == 'truncated':
        path.write_text('{"stage":', encoding='utf-8')
    else:
        write_json(path, bad)
    with pytest.raises(ValueError):
        client.repair_live(row['job_id'], input_digest=row['input_digest'])


@pytest.mark.parametrize('job_id,digest,sequence', [('../outside', 'a' * 64, -1),
    ('live-bound', 'x' * 64, -1), ('live-bound', 'a' * 64, True)])
def test_read_rejects_unsafe_identity_before_any_file(bound_job, monkeypatch, job_id, digest, sequence):
    _scheduler, client, _row, _directory, _request, _frames = bound_job
    monkeypatch.setattr('coat_sim.scheduler.daemon._repair_live_json',
        lambda *_a, **_k: pytest.fail('unexpected file read'))
    with pytest.raises(ValueError, match='invalid_repair_live_identity'):
        client.repair_live(job_id, input_digest=digest, after_sequence=sequence)


def test_read_binds_exact_frozen_spec_and_queue_digest(bound_job):
    _scheduler, client, row, directory, _request, _frames = bound_job
    with pytest.raises(ValueError, match='job_identity_mismatch'):
        client.repair_live(row['job_id'], input_digest='0' * 64)
    # A semantically similar rewritten spec is not the submitted byte identity.
    spec = json.loads((directory / 'spec.json').read_text(encoding='utf-8'))
    (directory / 'spec.json').write_text(json.dumps(spec, indent=2), encoding='utf-8')
    with pytest.raises(ValueError, match='frozen_job_mismatch'):
        client.repair_live(row['job_id'], input_digest=row['input_digest'])


def test_reader_bounds_json_before_parse(tmp_path, monkeypatch):
    path = tmp_path / 'repair-live.json'
    path.write_bytes(b' ' * 33)
    monkeypatch.setattr('coat_sim.scheduler.daemon.MAX_RPC_BODY_BYTES', 32)
    with pytest.raises(ValueError, match='packet_too_large'):
        _repair_live_json(path)


def test_real_worker_subprocess_persists_cancelled_champion_for_daemon_client(bound_job):
    scheduler, client, row, directory, request, _frames = bound_job
    scheduler._commit('transition', row['job_id'], state='running')
    write_json(directory / 'launch.json', {'instance': scheduler.owner})
    # Only pause report delivery at a real audited improvement so a user cancel
    # can race deterministically. No proposal, physics, audit or ranking is faked.
    script = r"""
import sys, time
from pathlib import Path
from coat_sim.core import film_manufacturing_jobs as jobs
from coat_sim.scheduler.worker import run
original = jobs.execute_job
def execute(parameters, cancel, report, *args, **kwargs):
    held = False
    def progress(frame):
        nonlocal held
        report(frame)
        if not held and frame.get('champion') is not None and frame.get('status') == 'running':
            held = True
            deadline = time.monotonic() + 15
            while not cancel.is_set() and time.monotonic() < deadline:
                time.sleep(.01)
            if not cancel.is_set():
                raise RuntimeError('test_user_cancel_not_received')
    return original(parameters, cancel, progress, *args, **kwargs)
jobs.execute_job = execute
raise SystemExit(run(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]))
"""
    options: dict[str, Any] = dict(cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding='utf-8')
    if os.name == 'nt':
        options['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    process = subprocess.Popen([sys.executable, '-c', script, str(scheduler.root), row['job_id'],
        str(os.getpid()), scheduler.owner], **options)
    try:
        deadline = time.monotonic() + 30
        best = None
        while time.monotonic() < deadline and process.poll() is None:
            frame = client.repair_live(row['job_id'], input_digest=row['input_digest'])
            if frame is not None and frame['champion'] is not None:
                best = frame
                break
            time.sleep(.02)
        assert best is not None, 'real worker did not publish an audited improvement'
        progress = {}
        while time.monotonic() < deadline:
            progress = json.loads((directory / 'progress.json').read_text(encoding='utf-8'))
            if progress.get('repair_live_sequence') == best['seq']:
                break
            time.sleep(.01)
        assert progress['repair_live_sequence'] == best['seq']
        assert set(progress) <= {'stage', 'status', 'repair_live_sequence'}
        assert client.cancel(row['job_id'])['cancel_requested']
        output, _ = process.communicate(timeout=20)
        assert process.returncode == 0, output
        terminal = client.repair_live(row['job_id'], input_digest=row['input_digest'],
                                    after_sequence=best['seq'])
        assert terminal is not None and terminal['status'] == 'cancelled'
        assert terminal['champion'] == best['champion']
        assert jobs.applicable_improvement(terminal['champion'], terminal['source'], request)
        outcome = json.loads((directory / 'outcome.json').read_text(encoding='utf-8'))
        assert outcome['state'] == 'cancelled'
        assert outcome['result']['result']['best'] == terminal['champion']
        assert outcome['cancel_snapshot']['replay_performed'] is False
        checkpoint = json.loads((directory / 'best-candidate.json').read_text(encoding='utf-8'))
        assert checkpoint['result']['result']['best'] == terminal['champion']
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)


@pytest.mark.parametrize('source_note,request_id', [('', 'transport-repair'),
    ('C:/synthetic-label/this-is-user-text', 'C:/synthetic-label/request-id')],
                         ids=['ordinary-metadata', 'path-like-metadata'])
def test_actual_daemon_http_reads_real_worker_frame_after_completion(tmp_path, source_note, request_id):
    spec, request = frozen_request(source_note=source_note, request_id=request_id)
    client = SchedulerClient(tmp_path)
    try:
        row = client.submit(spec, job_id='live-http', metadata={'summary': source_note})
        terminal = wait_state(client, row['job_id'], 'succeeded', 'failed', 'cancelled', timeout=45)
        assert terminal['state'] == 'succeeded', terminal
        frame = client.repair_live(row['job_id'], input_digest=row['input_digest'])
        assert frame is not None and frame['status'] == 'completed'
        assert frame['request_id'] == request_id
        assert frame['source']['design']['note'] == source_note
        assert frame['source']['design']['name'] == request['source_design']['name']
        for slot in ('current', 'source', 'champion'):
            assert frame[slot]['design'].get('workflow_settings') == request['source_design'].get('workflow_settings')
        assert client.status(row['job_id'])['summary'] == public_payload(source_note)
        assert jobs.validate_live_progress(frame, request) == frame
        assert jobs.applicable_improvement(frame['champion'], frame['source'], request)
        assert frame['champion']['layers']
        assert client.result(row['job_id'])['result']['best'] == public_payload(frame['champion'])
        assert client.repair_live(row['job_id'], input_digest=row['input_digest'],
                                  after_sequence=frame['seq']) is None
        if source_note:
            # The HTTP exception is confined to verified design/lineage and
            # the frozen request ID. Score/guard extensions remain private.
            path = tmp_path / 'artifacts/runtime/scheduler/jobs/live-http/repair-live.json'
            saved = json.loads(path.read_text(encoding='utf-8'))
            saved['seq'] += 1
            saved['source']['provider_private_path'] = 'C:/synthetic-provider/private-file'
            saved['source']['local_path'] = 'C:/synthetic-provider/private-file'
            write_json(path, saved)
            projected = client.repair_live(row['job_id'], input_digest=row['input_digest'],
                                          after_sequence=frame['seq'])
            assert 'local_path' not in projected['source']
            assert projected['source']['provider_private_path'] == 'private-file'
            assert jobs.validate_live_progress(projected, request) == projected
            assert jobs.applicable_improvement(projected['champion'], projected['source'], request)
            saved['source']['design']['provider_private_path'] = 'C:/synthetic-provider/private-file'
            write_json(path, saved)
            with pytest.raises(ValueError, match='unknown film design fields'):
                client.repair_live(row['job_id'], input_digest=row['input_digest'])
    finally:
        stop_service(client)


def test_job_directory_link_cannot_redirect_a_bound_read(bound_job, tmp_path):
    scheduler, client, row, directory, _request, frames = bound_job
    write_json(directory / 'repair-live.json', frames[-1])
    linked_id = 'live-link'
    # Even another job with identical frozen inputs must use its own directory.
    spec = JobSpec.parse_frozen_json((directory / 'spec.json').read_text(encoding='utf-8'))
    linked = client.submit(spec.to_spec().to_dict(), job_id=linked_id)
    target = scheduler.runtime / 'jobs' / linked_id
    try:
        target.symlink_to(directory, target_is_directory=True)
    except OSError as exc:
        pytest.skip('directory symlinks unavailable: ' + str(exc))
    with pytest.raises(ValueError, match='job_directory_not_owned'):
        client.repair_live(linked_id, input_digest=linked['input_digest'])
    assert directory.is_dir()
    target.unlink()


def test_live_projection_preserves_only_validated_design_and_lineage(bound_job):
    _scheduler, client, row, directory, request, frames = bound_job
    frame = deepcopy(frames[-1])
    # Existing compatibility permits score extensions; they do not gain a raw
    # transport bypass just because a valid design also contains user text.
    frame['source']['provider_private_path'] = 'C:/synthetic-provider/private-file'
    frame['source']['local_path'] = 'C:/synthetic-provider/private-file'
    checked = jobs.validate_live_progress(frame, request)
    projected = _public_repair_live_payload(checked)
    assert 'local_path' not in projected['source']
    assert projected['source']['provider_private_path'] == 'private-file'
    assert projected['source']['design'] == checked['source']['design']
    assert projected['source']['layers'] == checked['source']['layers']
    assert jobs.validate_live_progress(projected, request) == projected
    assert jobs.applicable_improvement(projected['champion'], projected['source'], request)
    # Design fields themselves are a closed, validated type.
    frame['source']['design']['provider_private_path'] = 'C:/synthetic-provider/private-file'
    write_json(directory / 'repair-live.json', frame)
    with pytest.raises(ValueError, match='unknown film design fields'):
        client.repair_live(row['job_id'], input_digest=row['input_digest'])


@pytest.mark.parametrize('slot', ['current', 'champion'])
def test_new_opaque_workflow_cannot_bypass_projection(bound_job, slot):
    _scheduler, client, row, directory, _request, frames = bound_job
    frame = deepcopy(frames[-1])
    frame[slot]['design']['workflow_settings'] = {
        'optimization': {'provider_private_path': 'C:/synthetic-provider/private-file'}}
    write_json(directory / 'repair-live.json', frame)
    with pytest.raises(ValueError, match='workflow|complete protected improvement evidence') as rejected:
        client.repair_live(row['job_id'], input_digest=row['input_digest'])
    assert 'synthetic-provider' not in str(rejected.value)
