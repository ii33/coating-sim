"""Private real scheduler lifetimes; no user daemon, project or endpoint is used."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from coat_sim.scheduler.client import SchedulerClient
from coat_sim.scheduler.lifecycle import (SchedulerExitGroup, acquire_start_gate,
    registration_path, registry_directory)
from coat_sim.scheduler.store import SchedulerStore

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def private_scope(tmp_path, monkeypatch):
    scope = tmp_path / 'private-assistant'
    monkeypatch.setenv('COAT_ASSISTANT_RUNTIME_ROOT', str(scope))
    yield scope
    # Only records in this test's newly created private scope may be inspected.
    assert not list(registry_directory(scope).glob('*.json')), 'private scheduler leaked'


def wait_for(predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.04)
    raise AssertionError('private scheduler condition timed out')


def spec(delay=0.05):
    return {'protocol_version': 1, 'executor': 'probe',
            'parameters': {'delay_s': delay}, 'inputs': {}}


@contextmanager
def service(root, *, idle_seconds=60, lease_seconds=45):
    """Retain and reap only the newly created private daemon's exact Popen."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    code = ('import sys; from coat_sim.scheduler.daemon import Scheduler; '
            'Scheduler(sys.argv[1],idle_seconds=float(sys.argv[2]),'
            'prepare_lease_seconds=float(sys.argv[3])).serve()')
    process = subprocess.Popen([sys.executable, '-B', '-c', code, str(root),
                                str(idle_seconds), str(lease_seconds)], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == 'nt' else 0,
                                  start_new_session=os.name != 'nt')
    from coat_sim.agent.processes import ProcessTree
    tree = ProcessTree(process)
    client = SchedulerClient(root)
    try:
        def ready():
            if process.poll() is not None:
                raise AssertionError('private scheduler startup failed')
            try:
                return client.snapshot_if_running()
            except (OSError, RuntimeError):
                return None
        wait_for(ready)
        yield client, process
    finally:
        if process.poll() is None:
            try:
                client.stop()
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                pass
        try:
            process.wait(timeout=15)
        finally:
            tree.close()
        # A test may have intentionally restarted the service through a client.
        owned = SchedulerClient._processes.get(str(root.resolve()))
        if owned is not None:
            if owned.poll() is None:
                client.stop()
            owned.wait(timeout=15)
            SchedulerClient._processes.pop(str(root.resolve()), None)
        SchedulerClient._endpoints.pop(str(root.resolve()), None)


def running(client, job_id):
    row = client._call('status', start=False, job_id=job_id)
    return row if row['state'] == 'running' else None


def test_background_snapshot_absent_and_invalid_never_launch(tmp_path, monkeypatch):
    client = SchedulerClient(tmp_path / 'absent')
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('background read started daemon'))
    assert client.snapshot_if_running() is None
    client.endpoint.parent.mkdir(parents=True)
    client.endpoint.write_text('{bad', encoding='utf-8')
    with pytest.raises(ValueError):
        client.snapshot_if_running()


@pytest.mark.parametrize('refresh_fails', [False, True])
def test_background_snapshot_revalidates_before_read_without_starting(tmp_path, refresh_fails):
    calls = []
    class Transport:
        def request(self, action, arguments):
            calls.append((action, arguments))
            if action == 'refresh_estimates':
                if refresh_fails:
                    raise OSError('authority unavailable')
                return {}
            assert action == 'snapshot'
            return {'confirmations': []}
    client = SchedulerClient(tmp_path / 'authority', transport=Transport())
    if refresh_fails:
        with pytest.raises(OSError, match='authority unavailable'):
            client.snapshot_if_running(after_event=7, limit=20, refresh_estimates=True)
        assert calls == [('refresh_estimates', {})]
    else:
        assert client.snapshot_if_running(after_event=7, limit=20, refresh_estimates=True) == {'confirmations': []}
        assert calls == [('refresh_estimates', {}), ('snapshot', {'after_event': 7, 'limit': 20})]


@pytest.mark.parametrize('withdraw_endpoint', [False, True])
@pytest.mark.parametrize('process_state', ['alive', 'unknown'])
def test_background_read_disconnect_requires_withdrawn_endpoint_for_offline(
        tmp_path, monkeypatch, withdraw_endpoint, process_state):
    from coat_sim.core import process_identity
    client = SchedulerClient(tmp_path / 'disconnect')
    client.endpoint.parent.mkdir(parents=True)
    endpoint = {'protocol_version': 1, 'host': '127.0.0.1', 'port': 1,
                'pid': 12345, 'instance': 'private-generation'}
    client.endpoint.write_text(json.dumps(endpoint), encoding='utf-8')
    identities = iter([('alive', 'private-start'),
                       (process_state, 'private-start' if process_state == 'alive' else None)])
    monkeypatch.setattr(process_identity, '_process_identity', lambda _pid: next(identities))
    monkeypatch.setattr(client, 'ensure_started',
                        lambda: pytest.fail('background read attempted daemon startup'))
    def disconnect(*_args, **_kwargs):
        if withdraw_endpoint:
            client.endpoint.unlink()
        raise OSError('listener closed')
    monkeypatch.setattr(client._opener, 'open', disconnect)
    if withdraw_endpoint:
        assert client.snapshot_if_running() is None
    else:
        with pytest.raises(RuntimeError, match='scheduler_unavailable'):
            client.snapshot_if_running()


def test_start_gate_blocks_late_child_and_client(tmp_path):
    group = SchedulerExitGroup()
    assert group.prepare('exit-empty') == 0
    assert group.prepare('exit-empty') == 0
    try:
        with pytest.raises(RuntimeError, match='scheduler_exit_in_progress'):
            SchedulerClient(tmp_path / 'late').ensure_started()
        from coat_sim.scheduler.daemon import serve
        assert serve(tmp_path / 'late-child') == 0
        assert not (tmp_path / 'late-child').exists()
    finally:
        group.cancel()
        group.cancel()
    gate = acquire_start_gate()
    gate.close()


def test_idle_daemon_exits_despite_background_reads_and_explicit_submit_restarts(tmp_path):
    with service(tmp_path / 'idle', idle_seconds=.7) as (client, process):
        original = client._call('health', start=False)
        deadline = time.monotonic() + 8
        while process.poll() is None and time.monotonic() < deadline:
            client.snapshot_if_running()
            time.sleep(.07)
        assert process.wait(timeout=3) == 0
        assert client.snapshot_if_running() is None
        assert not registration_path(client.root).exists()
        job = client.submit(spec())
        latest = client._call('health', start=False)
        assert latest['instance'] != original['instance']
        assert latest['pid'] != original['pid'] or latest['process_start'] != original['process_start']
        wait_for(lambda: client.status(job['job_id'])['state'] == 'succeeded')


def test_running_work_prevents_idle_exit(tmp_path):
    with service(tmp_path / 'busy', idle_seconds=.3) as (client, process):
        job = client.submit(spec(1.5))
        wait_for(lambda: running(client, job['job_id']))
        time.sleep(.65)
        assert process.poll() is None
        wait_for(lambda: client._call('status', start=False, job_id=job['job_id'])['state'] == 'succeeded')
        assert process.wait(timeout=8) == 0


def test_prepare_freezes_admissions_cancel_resumes_without_losing_queue(tmp_path):
    with service(tmp_path / 'cancel') as (client, process):
        first = client.submit(spec(2))
        wait_for(lambda: running(client, first['job_id']))
        second = client.submit(spec(.05))
        group = SchedulerExitGroup()
        try:
            assert group.prepare('keep-alive') == 1
            assert group.prepare('keep-alive') == 1
            group.renew()
            with pytest.raises(ValueError, match='scheduler_exit_in_progress'):
                client.submit(spec())
            assert client.status(second['job_id'])['state'] == 'queued'
        finally:
            group.cancel()
        assert process.poll() is None
        third = client.submit(spec())
        for job in (first, second, third):
            wait_for(lambda job=job: client.status(job['job_id'])['state'] == 'succeeded')


def test_commit_cancels_only_running_job_preserves_queued_and_holds_fence(tmp_path):
    with service(tmp_path / 'commit') as (client, process):
        first = client.submit(spec(20))
        wait_for(lambda: running(client, first['job_id']))
        second = client.submit(spec())
        group = SchedulerExitGroup()
        try:
            assert group.prepare('exit-all') == 1
            group.commit()
            wait_for(lambda: not group.pending())
            assert process.wait(timeout=10) == 0
            with pytest.raises(RuntimeError, match='scheduler_exit_in_progress'):
                SchedulerClient(tmp_path / 'resurrection').ensure_started()
            store = SchedulerStore(client.root)
            assert store.status(first['job_id'])['state'] == 'cancelled'
            assert store.status(second['job_id'])['state'] == 'queued'
        finally:
            group.cancel()
        wait_for(lambda: client.status(second['job_id'])['state'] == 'succeeded')


def test_expired_preparation_resumes_admission(tmp_path):
    with service(tmp_path / 'expiry', lease_seconds=.25) as (client, process):
        group = SchedulerExitGroup()
        assert group.prepare('expired') == 0
        time.sleep(.5)
        # A lost coordinator cannot leave the existing service frozen forever.
        job = client.submit(spec())
        wait_for(lambda: client.status(job['job_id'])['state'] == 'succeeded')
        group.cancel()
        assert process.poll() is None


def test_lifecycle_secret_and_instance_required_and_never_public(tmp_path):
    with service(tmp_path / 'identity') as (client, _process):
        health = client._call('health', start=False)
        record = json.loads(registration_path(client.root).read_text(encoding='utf-8'))
        assert 'secret' not in health and 'token' not in health
        assert record['secret'] not in json.dumps(health)
        for arguments in ({'instance': health['instance'], 'secret': 'wrong', 'exit_id': 'x'},
                          {'instance': 'replaced', 'secret': record['secret'], 'exit_id': 'x'}):
            with pytest.raises(ValueError, match='scheduler_lifecycle_identity_mismatch'):
                client._call('lifecycle_prepare', start=False, **arguments)
        assert client.snapshot_if_running() is not None


def test_group_skips_dead_exact_registry_without_touching_live_replacement(tmp_path, monkeypatch):
    from coat_sim.scheduler import lifecycle
    root = tmp_path / 'dead'
    path = registration_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'protocol_version': 1, 'root': str(root.resolve()),
        'host': '127.0.0.1', 'port': 1, 'pid': 11001, 'process_start': 'old',
        'instance': 'old-instance', 'secret': 'a' * 64}), encoding='utf-8')
    monkeypatch.setattr(lifecycle, '_process_identity', lambda pid: ('alive', 'replacement'))
    group = SchedulerExitGroup()
    try:
        assert group.prepare('dead-registry') == 0
        assert not path.exists()
        assert group.pending() == []
    finally:
        group.cancel()


def test_partial_commit_cancels_remaining_prepared_instances(tmp_path, monkeypatch):
    group = SchedulerExitGroup()
    first = {'instance': 'first'}
    second = {'instance': 'second'}
    group._gate = acquire_start_gate()
    group._prepared = [first, second]
    calls = []
    def call(record, action):
        calls.append((record['instance'], action))
        if action == 'lifecycle_commit' and record is second:
            raise RuntimeError('scheduler_lifecycle_unavailable')
        return {'state': 'ready', 'accepted': True}
    monkeypatch.setattr(group, '_call', call)
    with pytest.raises(RuntimeError, match='scheduler_lifecycle_unavailable'):
        group.commit()
    group.cancel()
    assert ('first', 'lifecycle_cancel') not in calls
    assert ('second', 'lifecycle_cancel') in calls
    gate = acquire_start_gate()
    gate.close()


def test_queued_and_recovery_uncertainty_prevent_idle_stop(tmp_path):
    from coat_sim.scheduler.daemon import Scheduler
    scheduler = Scheduler(tmp_path / 'idle-conditions', idle_seconds=.01)
    try:
        scheduler._last_activity = time.monotonic() - 100
        scheduler.recovery_error = 'unverified old worker'
        scheduler._idle_stop()
        assert not scheduler.stopping.is_set()
        scheduler.recovery_error = None
        scheduler.dispatch('submit', {'spec': spec(), 'job_id': 'queued'})
        scheduler._last_activity = time.monotonic() - 100
        scheduler._idle_stop()
        assert not scheduler.stopping.is_set()
        with scheduler.request_scope():
            scheduler.dispatch('cancel', {'job_id': 'queued'})
            scheduler._last_activity = time.monotonic() - 100
            scheduler._idle_stop()
            assert not scheduler.stopping.is_set()
        scheduler._idle_stop()
        assert scheduler.stopping.is_set()
        with pytest.raises(ValueError, match='scheduler_stopping'):
            scheduler.dispatch('submit', {'spec': spec(), 'job_id': 'after-stop'})
    finally:
        scheduler.guard.close()
        scheduler.lock.close()


def test_failed_endpoint_publication_releases_gate_and_owned_resources(tmp_path, monkeypatch):
    from coat_sim.scheduler import daemon
    scheduler = daemon.Scheduler(tmp_path / 'failed-publish')
    closed = []
    real_close = scheduler.guard.close
    def close_guard():
        closed.append(True)
        real_close()
    monkeypatch.setattr(scheduler.guard, 'close', close_guard)
    def fail(*args, **kwargs):
        raise OSError('injected endpoint publication failure')
    monkeypatch.setattr(daemon, 'publish_registration', fail)
    with pytest.raises(OSError, match='injected endpoint'):
        scheduler.serve()
    assert closed
    gate = acquire_start_gate()
    gate.close()
    replacement = daemon.Scheduler(scheduler.root)
    replacement.guard.close()
    replacement.lock.close()


def test_legacy_daemon_remains_readable_but_no_new_work_or_replacement(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    client = SchedulerClient(tmp_path / 'legacy')
    client.endpoint.parent.mkdir(parents=True)
    endpoint = {'protocol_version': 1, 'host': '127.0.0.1', 'port': 1,
                'pid': os.getpid(), 'instance': 'legacy-private-fixture'}
    client.endpoint.write_text(json.dumps(endpoint), encoding='utf-8')
    calls = []
    def open_request(request, timeout):
        action = json.loads(request.data)['action']
        calls.append(action)
        if action == 'health':
            result = {'protocol_version': 1, 'pid': os.getpid(),
                      'instance': endpoint['instance'], 'stopping': False}
        elif action == 'snapshot':
            result = {'jobs': [], 'confirmations': []}
        else:
            pytest.fail('legacy fixture received a mutating request')
        return io.BytesIO(json.dumps({'ok': True, 'result': result}).encode())
    monkeypatch.setattr(client, '_opener', SimpleNamespace(open=open_request))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('legacy service replaced'))
    assert client.snapshot_if_running() == {'jobs': [], 'confirmations': []}
    with pytest.raises(RuntimeError, match='scheduler_lifecycle_upgrade_required'):
        client.ensure_started()
    with pytest.raises(RuntimeError, match='scheduler_lifecycle_upgrade_required'):
        client.submit(spec())
    assert calls == ['health', 'snapshot', 'health', 'health']
    SchedulerClient._endpoints.pop(str(client.root), None)


@pytest.mark.parametrize('commit', [False, True])
def test_delayed_pre_exit_child_cannot_publish_after_fence_released(tmp_path, commit):
    from coat_sim.scheduler.lifecycle import current_start_epoch
    from coat_sim.agent.processes import ProcessTree
    gate = acquire_start_gate()
    try:
        old_epoch = current_start_epoch()
    finally:
        gate.close()
    target = tmp_path / 'delayed-daemon'
    ready, release = tmp_path / 'child-ready', tmp_path / 'release-child'
    code = '\n'.join(['from pathlib import Path', 'import sys,time', 'Path(sys.argv[3]).touch()', 'while not Path(sys.argv[4]).exists():', '    time.sleep(.01)', 'from coat_sim.scheduler.daemon import serve', 'raise SystemExit(serve(sys.argv[1],launch_epoch=sys.argv[2]))'])
    child = subprocess.Popen([sys.executable, '-B', '-c', code, str(target), old_epoch,
                              str(ready), str(release)], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == 'nt' else 0,
                                  start_new_session=os.name != 'nt')
    tree = ProcessTree(child)
    group = SchedulerExitGroup()
    try:
        wait_for(ready.exists)
        assert group.prepare('delayed-child') == 0
        if commit:
            group.commit()
        group.cancel()
        # The gate is now available; an OS lock alone would let this old child
        # publish. The durable epoch must still reject its pre-exit ticket.
        release.touch()
        assert child.wait(timeout=10) == 0
        assert not target.exists()
        assert not registration_path(target).exists()
        gate = acquire_start_gate()
        try:
            assert current_start_epoch() != old_epoch
        finally:
            gate.close()
    finally:
        group.cancel()
        release.touch()
        tree.close()
