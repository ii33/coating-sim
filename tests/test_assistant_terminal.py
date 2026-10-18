"""Cloud-local pipe terminal tests; never run a user's existing processes."""
import os
import subprocess
import sys
import time

import pytest

from coat_sim.agent.terminal import AssistantTerminal
from coat_sim.agent.toolkit.contracts import ToolkitError


def terminal(tmp_path, **kwargs):
    return AssistantTerminal(tmp_path, 'trusted-scope', can_execute=kwargs.pop('can_execute', lambda: True),
                             can_read=kwargs.pop('can_read', lambda: True), **kwargs)


def wait_done(manager, task_id):
    until = time.monotonic() + 8
    while time.monotonic() < until:
        result = manager.poll(task_id)
        if result['state'] != 'running':
            return result
        time.sleep(.01)
    pytest.fail('Owned test process did not settle')


def start_python(manager, code, **kwargs):
    return manager.start([sys.executable, '-c', code], **kwargs)


def test_cwd_output_utf8_and_scope(tmp_path):
    manager = terminal(tmp_path)
    other = terminal(tmp_path)
    try:
        result = start_python(manager, 'import os,sys; print(os.getcwd()); print("你好"); print("error",file=sys.stderr)')
        task_id = result['task_id']
        final = wait_done(manager, task_id)
        assert final['state'] == 'completed' and final['exit_code'] == 0
        assert str(tmp_path) in final['stdout'] and '你好' in final['stdout']
        assert final['stderr'] == 'error' + os.linesep
        assert final['isolation'] == 'none' and final['terminal_kind'] == 'pipe'
        assert manager.poll(task_id) == final  # cumulative, not destructive
        with pytest.raises(ToolkitError, match='this scope'):
            other.poll(task_id)
    finally:
        manager.close()
        other.close()


def test_authority_fail_closed_and_read_withholding(tmp_path):
    grant = {'execute': False, 'read': True}
    manager = terminal(tmp_path, can_execute=lambda: grant['execute'], can_read=lambda: grant['read'])
    try:
        with pytest.raises(ToolkitError) as exc:
            start_python(manager, 'print("no")')
        assert exc.value.code == 'authorization_denied'
        assert not manager._sessions
        grant['execute'] = True
        result = start_python(manager, 'import time; time.sleep(20)')
        grant['execute'] = False
        assert wait_done(manager, result['task_id'])['state'] == 'permission_revoked'
        grant['read'] = False
        with pytest.raises(ToolkitError) as exc:
            manager.poll(result['task_id'])
        assert exc.value.code == 'authorization_denied'
    finally:
        manager.close()


@pytest.mark.parametrize('cwd', ['../', '/', 'missing'])
def test_cwd_rejects_escape_and_missing(tmp_path, cwd):
    manager = terminal(tmp_path)
    try:
        with pytest.raises(ToolkitError):
            start_python(manager, 'pass', cwd=cwd)
        assert not manager._sessions
    finally:
        manager.close()


def test_cwd_rejects_symlink(tmp_path):
    try:
        (tmp_path / 'link').symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('Symlink privilege unavailable')
    manager = terminal(tmp_path)
    try:
        with pytest.raises(ToolkitError) as exc:
            start_python(manager, 'pass', cwd='link')
        assert exc.value.code == 'path_denied'
    finally:
        manager.close()


def test_environment_does_not_inherit_credentials(tmp_path, monkeypatch):
    for key in ['CNB_TOKEN', 'OPENAI_API_KEY', 'SSH_AUTH_SOCK', 'PYTHONPATH', 'HTTP_PROXY']:
        monkeypatch.setenv(key, 'terminal-test-secret')
    manager = terminal(tmp_path)
    try:
        result = start_python(manager, 'import os; print(dict(os.environ))')
        final = wait_done(manager, result['task_id'])
        assert 'terminal-test-secret' not in final['stdout']
        assert 'CNB_TOKEN' not in final['stdout']
    finally:
        manager.close()


def test_output_is_drained_after_shared_cap_and_invalid_utf8(tmp_path):
    manager = terminal(tmp_path)
    try:
        result = start_python(manager, 'import os; os.write(1,b"a"*2000000); os.write(2,b"b"*2000000)', max_output_bytes=100)
        final = wait_done(manager, result['task_id'])
        assert final['state'] == 'completed' and final['truncated']
        assert len((final['stdout'] + final['stderr']).encode('utf-8')) <= 100
        result = start_python(manager, 'import os; os.write(1,b"\\xff"*1000)', max_output_bytes=100)
        final = wait_done(manager, result['task_id'])
        assert len((final['stdout'] + final['stderr']).encode('utf-8')) <= 100
    finally:
        manager.close()


def test_timeout_cancel_capacity_and_close(tmp_path):
    manager = terminal(tmp_path, max_sessions=1)
    try:
        first = start_python(manager, 'import time; time.sleep(20)', timeout_ms=100)
        with pytest.raises(ToolkitError) as exc:
            start_python(manager, 'pass')
        assert exc.value.code == 'terminal_busy'
        assert wait_done(manager, first['task_id'])['state'] == 'timed_out'
        second = start_python(manager, 'import time; time.sleep(20)')
        with pytest.raises(ToolkitError):
            manager.poll(first['task_id'])
        manager.cancel(second['task_id'])
        assert wait_done(manager, second['task_id'])['state'] == 'cancelled'
        manager.close()
        manager.close()
        with pytest.raises(ToolkitError):
            start_python(manager, 'pass')
    finally:
        manager.close()


@pytest.mark.parametrize('kwargs', [{'timeout_ms': True}, {'timeout_ms': 0}, {'max_output_bytes': 0},
                                    {'max_output_bytes': 1048577}, {'cwd': 1}])
def test_invalid_limits_rejected(tmp_path, kwargs):
    manager = terminal(tmp_path)
    try:
        with pytest.raises(ToolkitError) as exc:
            start_python(manager, 'pass', **kwargs)
        assert exc.value.code == 'invalid_arguments'
    finally:
        manager.close()


def test_owned_descendant_cleanup_does_not_kill_other_process(tmp_path):
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
    manager = terminal(tmp_path)
    try:
        marker = tmp_path / 'child-survived'
        code = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c",' + repr(
            'import time; from pathlib import Path; time.sleep(.7); Path(' + repr(str(marker)) + ').write_text("bad")') + '])'
        result = start_python(manager, code)
        assert wait_done(manager, result['task_id'])['state'] == 'completed'
        time.sleep(.8)
        assert not marker.exists()
        assert unrelated.poll() is None
    finally:
        manager.close()
        unrelated.kill()
        unrelated.wait()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX detached-pipe regression')
def test_detached_pipe_holder_does_not_leak_readers(tmp_path):
    import threading
    manager = terminal(tmp_path, max_sessions=1)
    try:
        # This short-lived test child deliberately escapes the process group;
        # it exits by itself. It must not keep terminal reader threads alive.
        result = start_python(manager, 'import subprocess,sys; subprocess.Popen([sys.executable,"-c",'
                              '"import time; time.sleep(.5)"], start_new_session=True)')
        final = wait_done(manager, result['task_id'])
        assert final['state'] == 'completed'
        session = manager._sessions[result['task_id']]
        assert session.finished.wait(1)
        assert not any(thread.name.startswith('assistant-terminal-' + result['task_id'])
                       for thread in threading.enumerate())
        assert wait_done(manager, start_python(manager, 'print("next")')['task_id'])['stdout'] == 'next\n'
    finally:
        manager.close()
        time.sleep(.6)


def test_monitor_start_failure_reaps_child(tmp_path, monkeypatch):
    import coat_sim.agent.terminal as module
    original_popen = module.subprocess.Popen
    processes = []
    def tracked_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process
    def failed_start(self):
        raise RuntimeError('No thread capacity')
    monkeypatch.setattr(module.subprocess, 'Popen', tracked_popen)
    monkeypatch.setattr(module.threading.Thread, 'start', failed_start)
    manager = terminal(tmp_path)
    try:
        with pytest.raises(ToolkitError) as exc:
            start_python(manager, 'import time; time.sleep(20)')
        assert exc.value.code == 'terminal_unavailable'
        assert processes[0].poll() is not None
        assert processes[0].stdout is not None and processes[0].stderr is not None
        assert processes[0].stdout.closed and processes[0].stderr.closed
        assert not manager._sessions
    finally:
        manager.close()


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows Job and no-window contract')
def test_windows_suspended_job_and_no_window(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes as w
    import coat_sim.agent.terminal as module
    original_popen = module.subprocess.Popen
    creation_flags = []
    def tracked_popen(*args, **kwargs):
        creation_flags.append(kwargs.get('creationflags', 0))
        return original_popen(*args, **kwargs)
    monkeypatch.setattr(module.subprocess, 'Popen', tracked_popen)
    manager = terminal(tmp_path)
    try:
        result = start_python(manager, 'import time; time.sleep(20)')
        session = manager._sessions[result['task_id']]
        no_window = getattr(subprocess, 'CREATE_NO_WINDOW', None)
        assert isinstance(no_window, int)
        assert creation_flags[0] & no_window
        assert creation_flags[0] & 0x4  # CREATE_SUSPENDED before Job assignment
        assert session.job is not None and session.job.handle
        in_job = w.BOOL()
        windll = getattr(ctypes, 'WinDLL', None)
        assert callable(windll)
        query = windll('kernel32', use_last_error=True).IsProcessInJob
        query.argtypes = (w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL))
        query.restype = w.BOOL
        assert query(int(session.process._handle), session.job.handle, ctypes.byref(in_job))
        assert in_job.value
        manager.cancel(result['task_id'])
        assert wait_done(manager, result['task_id'])['state'] == 'cancelled'
        assert session.job.handle is None
    finally:
        manager.close()
