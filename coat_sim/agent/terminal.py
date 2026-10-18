"""Scoped, bounded host pipe terminal. This is NOT an OS sandbox.

The host owns scope and live approval callbacks; no model-selected ownership,
credential environment, shell=True, or arbitrary process IDs are accepted.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid
from types import ModuleType
from typing import Any

from .toolkit.contracts import ToolContext, ToolkitError
from .toolkit.execution import validate_argv
from .toolkit.policy import resolve_path

MAX_OUTPUT = 1024 * 1024
# Deliberately exclude HOME/USERPROFILE, proxy settings, SSH agents, credentials,
# interpreter/loader injection settings and arbitrary host application variables.
_ENV_KEYS = ('PATH', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
             'LANG', 'LC_ALL', 'LC_CTYPE')


def _environment():
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    return env


def _allowed(callback):
    try:
        return callback() is True
    except Exception:
        return False


def _require(callback):
    if not _allowed(callback):
        raise ToolkitError('authorization_denied', 'Host terminal permission is not available')


def _platform_attribute(module: ModuleType, name: str) -> Any:
    """Resolve Windows-only APIs absent from POSIX modules and typing stubs."""
    value = getattr(module, name, None)
    if value is None:
        raise OSError('Required platform API is unavailable')
    return value


class _WindowsJob:
    """Kill-on-close Job assigned while the process is suspended.

    NtResumeProcess is used because Popen closes the primary thread handle.
    If unavailable, fail closed before any program instructions are executed.
    """
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes = ctypes
        self.kernel = _platform_attribute(ctypes, 'WinDLL')('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, w.LPCWSTR)
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD)
        self.kernel.SetInformationJobObject.restype = w.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = (w.HANDLE, w.HANDLE)
        self.kernel.AssignProcessToJobObject.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = (w.HANDLE,)
        self.kernel.CloseHandle.restype = w.BOOL
        self.resume = _platform_attribute(ctypes, 'WinDLL')('ntdll').NtResumeProcess
        self.resume.argtypes = (w.HANDLE,)
        self.resume.restype = w.LONG

        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                        ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', w.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', w.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', w.DWORD),
                        ('SchedulingClass', w.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                         'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', Io),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError('Job unavailable')
        settings = Extended()
        settings.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(settings), ctypes.sizeof(settings)):
            self.close()
            raise OSError('Job configuration unavailable')

    def attach_and_resume(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError('Job assignment unavailable')
        if self.resume(int(process._handle)) < 0:
            raise OSError('Process resume unavailable')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


@dataclass
class _Session:
    task_id: str
    process: subprocess.Popen
    cwd: str
    max_output: int
    deadline: float
    job: object = None
    state: str = 'running'
    exit_code: int | None = None
    reason: str | None = None
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    stop: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    drain_stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)


class AssistantTerminal:
    """One trusted project/global scope's process state; never accept model scope."""
    def __init__(self, workspace_root: Path, scope_id: str, *, can_execute, can_read,
                 max_sessions: int = 16):
        if not isinstance(scope_id, str) or not scope_id or type(max_sessions) is not int or not 1 <= max_sessions <= 128:
            raise ValueError('Invalid host terminal scope or capacity')
        root = Path(workspace_root)
        if not root.is_absolute() or root == Path(root.anchor):
            raise ValueError('Terminal requires an absolute non-root workspace')
        self._context = ToolContext(allowed_roots=(root,))
        self._root = resolve_path(self._context, root)
        self._scope_id = scope_id
        self._can_execute, self._can_read = can_execute, can_read
        self._max_sessions = max_sessions
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._closed = False

    def start(self, argv, *, cwd='.', timeout_ms=120000, max_output_bytes=MAX_OUTPUT):
        _require(self._can_execute)
        _require(self._can_read)
        argv = validate_argv(argv)
        if (any(len(arg) > 65536 for arg in argv) or sum(map(len, argv)) > 262144
                or not isinstance(cwd, str) or not 1 <= len(cwd) <= 4096
                or type(timeout_ms) is not int or not 1 <= timeout_ms <= 600000
                or type(max_output_bytes) is not int or not 1 <= max_output_bytes <= MAX_OUTPUT):
            raise ToolkitError('invalid_arguments', 'Invalid terminal input or limits')
        target = resolve_path(self._context, cwd)
        if not target.is_dir():
            raise ToolkitError('path_denied', 'Terminal cwd must be an existing workspace directory')
        with self._lock:
            if self._closed:
                raise ToolkitError('terminal_unavailable', 'Terminal scope is closed')
            while len(self._sessions) >= self._max_sessions:
                expired = next((key for key, item in self._sessions.items() if item.finished.is_set()), None)
                if expired is None:
                    raise ToolkitError('terminal_busy', 'Terminal active-session limit reached')
                del self._sessions[expired]
            _require(self._can_execute)
            process, job = None, None
            try:
                options = {'start_new_session': True} if os.name != 'nt' else {
                    'creationflags': _platform_attribute(subprocess, 'CREATE_NO_WINDOW') | 0x4}  # CREATE_SUSPENDED
                if os.name == 'nt':
                    job = _WindowsJob()
                process = subprocess.Popen(argv, cwd=str(target), env=_environment(),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    shell=False, close_fds=True, **options)
                if job is not None:
                    job.attach_and_resume(process)
            except Exception:
                if process is not None:
                    process.kill()
                    process.wait()
                    for pipe in (process.stdout, process.stderr):
                        if pipe is not None:
                            pipe.close()
                if job is not None:
                    job.close()
                raise ToolkitError('terminal_unavailable', 'Host process could not be started safely') from None
            session = _Session(uuid.uuid4().hex, process, str(target), max_output_bytes,
                               time.monotonic() + timeout_ms / 1000, job)
            self._sessions[session.task_id] = session
            try:
                threading.Thread(target=self._monitor, args=(session,), daemon=True,
                                 name='assistant-terminal-' + session.task_id).start()
            except Exception:
                self._terminate(session)
                process.wait()
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
                del self._sessions[session.task_id]
                raise ToolkitError('terminal_unavailable', 'Terminal monitor could not start') from None
            data = self._snapshot(session)
            _require(self._can_read)
            return data

    def _get(self, task_id):
        if not isinstance(task_id, str):
            raise ToolkitError('invalid_arguments', 'Invalid terminal task identifier')
        with self._lock:
            session = self._sessions.get(task_id)
        if session is None:
            raise ToolkitError('task_not_found', 'Terminal task is unavailable in this scope')
        return session

    def poll(self, task_id):
        _require(self._can_read)
        data = self._snapshot(self._get(task_id))
        _require(self._can_read)
        return data

    def cancel(self, task_id):
        _require(self._can_execute)
        session = self._get(task_id)
        with session.lock:
            if session.state == 'running':
                session.reason = session.reason or 'cancelled'
                session.stop.set()
        _require(self._can_read)
        return self._snapshot(session)

    def close(self):
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
        for session in sessions:
            with session.lock:
                if session.state == 'running':
                    session.reason = session.reason or 'cancelled'
                    session.stop.set()
        # Bounded host shutdown; monitors remain responsible for reaping.
        deadline = time.monotonic() + 5
        for session in sessions:
            session.finished.wait(max(0, deadline - time.monotonic()))

    @staticmethod
    def _snapshot(session):
        with session.lock:
            def decode(data):
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                return decoder.decode(bytes(data), final=session.state != 'running')
            budget = session.max_output
            output = {}
            truncated = session.truncated
            for name in ('stdout', 'stderr'):
                encoded = decode(getattr(session, name)).encode('utf-8')
                output[name] = encoded[:budget].decode('utf-8', errors='ignore')
                truncated |= len(encoded) > budget
                budget -= len(output[name].encode('utf-8'))
            return {'task_id': session.task_id, 'state': session.state,
                    'exit_code': session.exit_code, **output, 'truncated': truncated,
                    'cwd': session.cwd, 'isolation': 'none', 'terminal_kind': 'pipe'}

    @staticmethod
    def _drain(session, pipe, name):
        # Never block forever on a pipe held by a deliberately detached child.
        # PeekNamedPipe avoids a blocking Windows ReadFile; POSIX uses O_NONBLOCK.
        stop_deadline = None
        try:
            fd = pipe.fileno()
            if os.name == 'nt':
                import ctypes
                import msvcrt
                from ctypes import wintypes as w
                peek = _platform_attribute(ctypes, 'WinDLL')('kernel32', use_last_error=True).PeekNamedPipe
                peek.argtypes = (w.HANDLE, ctypes.c_void_p, w.DWORD,
                                 ctypes.c_void_p, ctypes.POINTER(w.DWORD), ctypes.c_void_p)
                peek.restype = w.BOOL
                handle = _platform_attribute(msvcrt, 'get_osfhandle')(fd)
            else:
                os.set_blocking(fd, False)
            while True:
                if session.drain_stop.is_set():
                    stop_deadline = stop_deadline or time.monotonic() + .2
                    if time.monotonic() >= stop_deadline:
                        break
                if os.name == 'nt':
                    available = w.DWORD()
                    if not peek(handle, None, 0, None, ctypes.byref(available), None):
                        break
                    if not available.value:
                        if session.drain_stop.is_set():
                            break
                        time.sleep(.01)
                        continue
                    chunk = os.read(fd, min(8192, available.value))
                else:
                    try:
                        chunk = os.read(fd, 8192)
                    except BlockingIOError:
                        if session.drain_stop.is_set():
                            break
                        time.sleep(.01)
                        continue
                if not chunk:
                    break
                with session.lock:
                    left = session.max_output - len(session.stdout) - len(session.stderr)
                    getattr(session, name).extend(chunk[:left])
                    session.truncated |= len(chunk) > left
        except (OSError, ValueError):
            pass
        finally:
            pipe.close()

    @staticmethod
    def _terminate(session):
        if session.job is not None:
            session.job.close()
        else:
            try:
                os.killpg(session.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _monitor(self, session):
        readers = [threading.Thread(target=self._drain, args=(session, pipe, name), daemon=True,
                                   name='assistant-terminal-' + session.task_id + '-' + name)
                   for pipe, name in ((session.process.stdout, 'stdout'), (session.process.stderr, 'stderr'))]
        try:
            for thread in readers:
                thread.start()
            while session.process.poll() is None:
                with session.lock:
                    if not _allowed(self._can_execute):
                        session.reason = session.reason or 'permission_revoked'
                    if time.monotonic() >= session.deadline:
                        session.reason = session.reason or 'timed_out'
                    if session.reason or session.stop.is_set():
                        break
                session.stop.wait(0.025)
            # Also clean up same-group descendants after the direct child exits.
            self._terminate(session)
            code = session.process.wait()
            session.drain_stop.set()
            for thread in readers:
                thread.join()
            with session.lock:
                session.exit_code = code
                session.state = session.reason or 'completed'
        except Exception:
            try:
                self._terminate(session)
                session.process.wait(timeout=5)
            except Exception:
                pass
            with session.lock:
                session.state = 'failed'
        finally:
            session.drain_stop.set()
            for thread in readers:
                if thread.ident is not None:
                    thread.join()
            for pipe in (session.process.stdout, session.process.stderr):
                pipe.close()
            session.finished.set()
