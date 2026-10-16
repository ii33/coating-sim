"""One headless scheduler per project; workers never use a control pipe for arrays."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from coat_sim.process_entry import is_frozen, module_command
import threading
import time
from uuid import uuid4
from collections import deque
from copy import deepcopy
from coat_sim.core.process_identity import _process_identity
from coat_sim.core.compute_tasks import _compact_progress
from .io import write_json, write_text
from .store import SchedulerStore, FINAL

MAX_RPC_BODY_BYTES = 64 * 1024 * 1024


def _appearance_gpu_python(root: Path) -> str:
    """Select only the project's dedicated Mitsuba environment for CUDA jobs."""
    if is_frozen():
        raise RuntimeError(
            'gpu_cuda appearance queue requires the source-installed '
            'build/mitsuba_appearance_env runtime; frozen worker cannot select it')
    module_root = Path(__file__).resolve().parents[2]
    for candidate_root in dict.fromkeys((root, module_root)):
        runtime = candidate_root / 'build' / 'mitsuba_appearance_env'
        executable = (runtime / 'Scripts' / 'python.exe' if os.name == 'nt'
                      else runtime / 'bin' / 'python')
        if executable.is_file():
            return str(executable.resolve())
    raise FileNotFoundError(
        'gpu_cuda appearance queue requires isolated build/mitsuba_appearance_env '
        'with Mitsuba/Dr.Jit CUDA; no application-interpreter fallback')


def _appearance_gpu_environment(interpreter: str) -> dict[str, str]:
    """Prevent the application/Conda interpreter from leaking into CUDA worker imports."""
    environment = dict(os.environ)
    for name in tuple(environment):
        upper = name.upper()
        if ((upper.startswith(('PYTHON', 'CONDA', 'MAMBA'))
             and upper not in {'PYTHONUTF8', 'PYTHONIOENCODING', 'PYTHONUNBUFFERED'})
                or upper in {'VIRTUAL_ENV', '__PYVENV_LAUNCHER__'}
                or upper.startswith(('QT_', 'QML_'))):
            environment.pop(name, None)

    def clean_search_path(value):
        return os.pathsep.join(part for part in value.split(os.pathsep)
                               if part and not any(token in part.casefold()
                               for token in ('conda', 'mamba', '.venv', 'site-packages')))

    executable_dir = str(Path(interpreter).parent)
    environment['PATH'] = executable_dir + os.pathsep + clean_search_path(environment.get('PATH', ''))
    for name in ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH'):
        if name in environment:
            environment[name] = clean_search_path(environment[name])
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
    environment['PYTHONNOUSERSITE'] = '1'
    environment['PYTHONSAFEPATH'] = '1'
    return environment


def _appearance_gpu_route(frozen_spec, root: Path) -> tuple[str | None, dict[str, str] | None]:
    if frozen_spec.executor != 'appearance_render':
        return None, None
    from coat_sim.core.appearance_result import _backend_precision
    parameters = frozen_spec.parameters
    if not isinstance(parameters, dict) or set(parameters) != {'frozen'}:
        raise ValueError('appearance queue has no frozen request envelope')
    envelope = parameters['frozen']
    if not isinstance(envelope, dict):
        raise ValueError('appearance queue frozen request envelope is invalid')
    backend, _precision = _backend_precision(envelope.get('request'))
    if backend != 'gpu_cuda':
        return None, None
    interpreter = _appearance_gpu_python(root)
    return interpreter, _appearance_gpu_environment(interpreter)

def public_payload(value):
    if isinstance(value, dict):
        private = {'input_snapshot_file', 'result_file', 'report_path', 'directory',
                   'temporary_bundle_path', 'local_path', 'diagnostic_file', 'discarded_result', 'files'}
        return {key: public_payload(item) for key, item in value.items() if key not in private}
    if isinstance(value, list): return [public_payload(item) for item in value]
    if isinstance(value, str):
        from pathlib import PureWindowsPath
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
            return PureWindowsPath(value).name
    return value

class InstanceLock:
    """An OS lock, held until process exit; a stale filename is never a lease."""
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open('a+b')
        self.stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            if not path.stat().st_size:
                self.stream.write(b'0'); self.stream.flush(); self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def close(self): self.stream.close()

class WorkerGuard:
    """Windows job object: native calculations die if their scheduler crashes."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt': return
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', w.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount','WriteOperationCount',
                'OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IO), ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = Extended(); info.BasicLimitInformation.LimitFlags = 0x2000
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), 'worker_guard_unavailable')
    def attach(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            import ctypes
            process.terminate(); process.wait()
            raise OSError(ctypes.get_last_error(), 'worker_guard_attach_failed')
    def close(self):
        if self.handle: self.kernel.CloseHandle(self.handle); self.handle = None

def _terminate_verified(pid, expected_start):
    if not pid: return
    state, actual_start = _process_identity(pid)
    if state == 'dead' or (expected_start and actual_start and expected_start != actual_start): return
    if state != 'alive' or not expected_start or not actual_start:
        raise RuntimeError(f'recovery_worker_identity_unknown: PID {pid}; queue remains paused')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; kernel.OpenProcess.restype = w.HANDLE
        kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x100001, False, pid)
        if not handle: raise OSError(ctypes.get_last_error(), 'recovery_worker_open_failed')
        try:
            if not kernel.TerminateProcess(handle, 1):
                if _process_identity(pid)[0] != 'dead': raise OSError(ctypes.get_last_error(), 'recovery_worker_stop_failed')
            if kernel.WaitForSingleObject(handle, 10000) != 0:
                raise RuntimeError('recovery_worker_still_alive')
        finally: kernel.CloseHandle(handle)
    else:
        import signal
        os.kill(pid, signal.SIGKILL)

def _interrupted_job_changes(row, message='Previous scheduler exited; computation is not replayed'):
    """Keep an accepted cancellation terminal when its worker is recovered."""
    cancelled = bool(row.get('cancel_requested'))
    return {
        'state': 'cancelled' if cancelled else 'failed',
        'interrupted': True,
        'error_code': 'cancelled' if cancelled else 'scheduler_interrupted',
        'error': None if cancelled else message,
        'recovery_evidence': {
            'worker_pid': row.get('worker_pid'),
            'worker_start': row.get('worker_start'),
            'previous_lease_owner': row.get('lease_owner'),
            'result_completion_verified': False,
            'cancel_requested': cancelled,
        },
    }

class Scheduler:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.runtime = self.root / 'artifacts/runtime/scheduler'
        self.lock = InstanceLock(self.runtime / 'instance.lock')
        self.owner = uuid4().hex
        self.store = SchedulerStore(self.root)
        self.stopping = threading.Event()
        self.guard = WorkerGuard()
        self.active = None
        self.recovery_error = None
        try:
            for row in self.store.running():
                _terminate_verified(row['worker_pid'], row['worker_start'])
                changes = _interrupted_job_changes(row)
                self.store.transition(row['job_id'], **changes)
                if changes['state'] == 'cancelled':
                    self._cleanup_cancel_marker(row['job_id'])
        except Exception as exc:
            self.recovery_error = str(exc)
        self.condition = threading.Condition(threading.RLock())
        initial = self.store.queue.snapshot(limit=100)
        self.index = {job['job_id']: job for job in initial['jobs']}
        self.confirmations = initial['confirmations']
        with self.store.queue.connection() as db:
            self.sequence = db.execute('SELECT COALESCE(MAX(event_id),0) FROM scheduler_events').fetchone()[0]
        self.event_buffer = deque(maxlen=1024)
        self.artifact_index = {}
        for job in self.index.values():
            for item in (job.get('result') or {}).get('result_artifacts', []):
                self.artifact_index[item['artifact_id']] = item

    def _publish(self, value, kind):
        if value is None: return
        self.sequence = getattr(self.store, 'last_sequence', self.sequence)
        self.index[value['job_id']] = deepcopy(value)
        for item in (value.get('result') or {}).get('result_artifacts', []):
            self.artifact_index[item['artifact_id']] = item
        self.event_buffer.append({**deepcopy(value), 'type': kind, 'sequence': self.sequence, 'event_id': self.sequence})
        # Active jobs always stay resident; keep a bounded recent terminal index.
        terminal = [key for key, job in self.index.items() if job['state'] in FINAL]
        for key in terminal[:-100]:
            old = self.index.pop(key, None)
            for item in (old.get('result') or {}).get('result_artifacts', []):
                self.artifact_index.pop(item['artifact_id'], None)
        self.condition.notify_all()

    def _commit(self, method, *args, **kwargs):
        with self.condition:
            previous_sequence = self.sequence
            self.store.last_sequence = self.sequence
            value = getattr(self.store, method)(*args, **kwargs)
            record = value[0] if method == 'claim' and value else value
            if record:
                kind = 'enqueued' if method == 'submit' else 'cancel_requested' if method == 'cancel' else 'completed' if record['state'] in FINAL else 'running' if method == 'claim' else 'progress'
                # Deduplicated submit does not manufacture duplicate events.
                if getattr(self.store, 'last_sequence', self.sequence) > previous_sequence:
                    self._publish(record, kind)
            if method == 'cancel' and record and record['state'] not in FINAL:
                self._notify_cancel(record['job_id'])
            return value

    def _notify_cancel(self, job_id):
        # The durable decision precedes the best-effort worker wake-up.
        marker = self.root / 'artifacts/runtime/jobs' / (job_id + '.cancel')
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)
            return True
        except OSError as exc:
            print(f'cancel_notification_warning {job_id}: {exc}', flush=True)
            return False

    def _cleanup_cancel_marker(self, job_id):
        """Remove only this scheduler-owned wake-up marker after the worker exits."""
        marker = self.root / 'artifacts/runtime/jobs' / (job_id + '.cancel')
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:
            print(f'cancel_marker_cleanup_warning {job_id}: {exc}', flush=True)

    def memory_snapshot(self, after_event=0, limit=100):
        with self.condition:
            jobs = list(self.index.values())
            active = [job for job in jobs if job['state'] not in FINAL]
            recent = sorted((job for job in jobs if job['state'] in FINAL),
                            key=lambda job: job.get('queue_order', 0), reverse=True)
            events = [value for value in self.event_buffer if value['sequence'] > after_event]
            return deepcopy({'jobs': active + recent[:max(0, int(limit)-len(active))],
                'confirmations': self.confirmations, 'events': events[:max(1,int(limit))],
                'latest_event_id': self.sequence, 'sequence': self.sequence,
                'cursor': events[min(len(events), max(1,int(limit)))-1]['sequence'] if events else after_event})

    def memory_status(self, job_id):
        with self.condition:
            if job_id in self.index: return deepcopy(self.index[job_id])
        # Explicit historical detail is a lazy read, separate from common queue refresh.
        return self.store.status(job_id)

    def events(self, after_sequence=0, wait_s=0):
        after_sequence = int(after_sequence)
        wait_s = min(30, max(0, float(wait_s)))
        with self.condition:
            if after_sequence == self.sequence and wait_s:
                self.condition.wait_for(lambda: self.sequence != after_sequence or self.stopping.is_set(), timeout=wait_s)
            oldest = self.event_buffer[0]['sequence'] if self.event_buffer else self.sequence+1
            if after_sequence < oldest-1 or after_sequence > self.sequence:
                return {'sequence': self.sequence, 'reset': True, 'events': [], 'snapshot': self.memory_snapshot()}
            return deepcopy({'sequence': self.sequence, 'reset': False,
                             'events': [event for event in self.event_buffer if event['sequence'] > after_sequence]})

    def register_artifacts(self, job_id, result):
        import hashlib
        paths = [('result', result.get('result_file')), ('report', result.get('report_path')),
                 ('bundle', (result.get('asset') or {}).get('temporary_bundle_path'))]
        records = []
        for role, raw in paths:
            if not raw: continue
            path = Path(raw).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                raise ValueError('invalid_result_artifact')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024*1024), b''): digest.update(block)
            artifact_id = hashlib.sha256((job_id+':'+role+':'+digest.hexdigest()).encode()).hexdigest()
            record = {'artifact_id': artifact_id, 'role': role, 'sha256': digest.hexdigest(),
                      'bytes': path.stat().st_size, 'name': path.name, 'local_path': str(path)}
            records.append(record)
            if role == 'result': result['artifact_id'] = artifact_id
        result['result_artifacts'] = records
        return result

    def resolve_artifact(self, artifact_id):
        item = self.artifact_index.get(artifact_id)
        if item: return item
        # Historical artifact retrieval explicitly permits a lazy storage read.
        with self.store.queue.connection() as db:
            row = db.execute('SELECT metadata FROM scheduler_artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
        if not row: raise ValueError('artifact_not_found')
        return json.loads(row['metadata'])

    def _attach_best_candidate_checkpoint(self, job_id, outcome):
        if self.memory_status(job_id).get('kind') == 'appearance_render':
            # Appearance output is readable only after a successful terminal
            # transition; it has no best-candidate or partial-result path.
            outcome = dict(outcome)
            outcome['result'] = None
            outcome.pop('result_unavailable_reason', None)
            return outcome
        from .result_capability import load_best_candidate_checkpoint

        candidate, reason = load_best_candidate_checkpoint(self.root, job_id)
        outcome = dict(outcome)
        outcome['result'] = candidate
        if candidate is None:
            outcome['result_unavailable_reason'] = reason
        else:
            outcome.pop('result_unavailable_reason', None)
        return outcome

    def _cancelled_outcome(self, job_id, previous):
        outcome = {'state': 'cancelled', 'error_code': 'cancelled',
                   'error': (previous or {}).get('error') or 'Cancelled by user'}
        if self.memory_status(job_id).get('kind') == 'appearance_render':
            outcome['result'] = None
            return outcome
        return self._attach_best_candidate_checkpoint(job_id, outcome)

    def dispatch(self, action, arguments):
        if action == 'health': return {'protocol_version': 1, 'pid': os.getpid(), 'instance': self.owner,
                                      'recovery_error': self.recovery_error, 'stopping': self.stopping.is_set()}
        if action == 'submit':
            if self.stopping.is_set(): raise ValueError('scheduler_stopping')
            return self._commit('submit', **arguments)
        if action == 'status': return self.memory_status(**arguments)
        if action == 'cancel': return self._commit('cancel', **arguments)
        if action == 'result':
            value = self.memory_status(**arguments)
            state = value.get('state')
            result = value.get('result')
            from .result_capability import (
                is_best_candidate_result, is_film_optimization_result,
                job_has_viewable_result,
            )
            if not job_has_viewable_result(value):
                if state in ('failed', 'cancelled'):
                    detail = value.get('result_unavailable_reason') or (
                        'No successful verified appearance render is available.'
                        if value.get('kind') == 'appearance_render'
                        else 'No verified best candidate is available.')
                    raise ValueError('job_result_unavailable: ' + str(detail))
                if state == 'succeeded' and 'result' not in value:
                    raise ValueError('job_result_missing: successful queue record has no result payload')
                if state == 'succeeded':
                    raise ValueError('job_result_invalid_shape: expected a result object')
                raise ValueError('job_not_complete: ' + str(state))
            if result.get('kind') == 'appearance_render':
                from .result_capability import validate_appearance_render_result
                try:
                    validate_appearance_render_result(
                        self.root, value['job_id'], result,
                        expected_input_digest=value.get('input_digest'),
                        require_registered_artifact=True)
                except Exception as exc:
                    raise ValueError('job_result_invalid_appearance: ' + str(exc)) from exc
            if state in ('failed', 'cancelled'):
                artifacts = result.get('result_artifacts') or []
                if not is_best_candidate_result(result):
                    raise ValueError('job_result_unavailable: no verified candidate is available')
                if (not is_film_optimization_result(result) and not any(
                        isinstance(item, dict) and item.get('role') == 'bundle'
                        and item.get('artifact_id') for item in artifacts)):
                    raise ValueError('job_result_unavailable: candidate bundle is not registered')
            return result
        if action == 'run_result':
            with self.condition:
                for value in self.index.values():
                    if value['state'] == 'succeeded' and (value.get('result') or {}).get('run_id') == arguments['run_id']:
                        return deepcopy(value['result'])
            with self.store.queue.connection() as db:
                row = db.execute("SELECT payload FROM queue_jobs WHERE state='succeeded' AND json_extract(payload,'$.result.run_id')=?",
                                 (arguments['run_id'],)).fetchone()
            if not row: raise ValueError('run_not_found')
            return json.loads(row['payload'])['result']
        if action == 'snapshot': return self.memory_snapshot(**arguments)
        if action == 'events': return self.events(**arguments)
        if action == 'refresh_estimates':
            value = self.store.queue.snapshot(limit=1)
            with self.condition: self.confirmations = value['confirmations']
            return {'refreshed': True}
        if action == 'page':
            return {**self.store.queue.queue_jobs_page(**arguments), 'source': 'persistent_queue'}
        if action == 'artifact':
            item = self.resolve_artifact(arguments['artifact_id'])
            return {k:v for k,v in item.items() if k != 'local_path'}
        if action == 'capabilities':
            return {'protocol_version': 1, 'executors': sorted(__import__('coat_sim.scheduler.jobs', fromlist=['EXECUTORS']).EXECUTORS),
                    'execution': 'single_local_worker', 'device_selection': 'parameters.backend',
                    'transport': 'loopback_http', 'authentication': 'local_only', 'artifacts': 'download_by_id'}
        if action == 'find_request':
            receipt = self.store.queue.find_request(**arguments)
            return {'job': self.memory_status(receipt['job_id']) if receipt and receipt.get('job_id') else None}
        if action == 'stop':
            self.stopping.set()
            return {'stopping': True, 'queued_preserved': True}
        raise ValueError('unknown_scheduler_action')

    def start_job(self, value, spec):
        job_id = value['job_id']
        directory = self.runtime / 'jobs' / job_id
        directory.mkdir(parents=True, exist_ok=True)
        from .jobs import JobSpec
        frozen = JobSpec.parse_frozen(spec)
        if frozen.fingerprint != value['input_digest']: raise ValueError('frozen_input_digest_mismatch')
        write_text(directory / 'spec.json', frozen.canonical_json)
        options = {'cwd': str(Path(__file__).resolve().parents[2]), 'stdin': subprocess.DEVNULL}
        if os.name == 'nt': options['creationflags'] = subprocess.CREATE_NO_WINDOW
        else: options['start_new_session'] = True
        process = None
        try:
            interpreter, environment = _appearance_gpu_route(frozen, self.root)
            if environment is not None:
                options['env'] = environment
            with (directory / 'worker.log').open('ab') as log:
                command = module_command('coat_sim.scheduler', '--root', str(self.root),
                    'worker', job_id, str(os.getpid()), self.owner,
                    **({'interpreter': interpreter} if interpreter is not None else {}))
                process = subprocess.Popen(command, stdout=log, stderr=log, **options)
            self.guard.attach(process)
            self.store.set_worker(job_id, process.pid, _process_identity(process.pid)[1])
            write_json(directory / 'launch.json', {'instance': self.owner})
        except BaseException as error:
            if process is None:
                raise
            # Until launch.json is published this newly created child cannot
            # start an algorithm. Reap it even if guard/identity persistence
            # failed, before the queue may release its execution slot.
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
            except BaseException as cleanup_error:
                self.active = (job_id, process, directory, None)
                error.add_note(f'worker_start_cleanup_failed: {cleanup_error}')
                raise error from cleanup_error
            raise
        self.active = (job_id, process, directory, None)

    def tick(self):
        if self.active:
            job_id, process, directory, last_progress = self.active
            if self.memory_status(job_id).get('cancel_requested') and not self._notify_cancel(job_id):
                if process.poll() is None: process.terminate()
            try:
                progress = json.loads((directory / 'progress.json').read_text(encoding='utf-8'))
                if progress != last_progress:
                    self._commit('transition', job_id, progress=_compact_progress(progress))
                    self.active = (job_id, process, directory, progress)
            except (OSError, ValueError): pass
            code = process.poll()
            if code is None: return
            try:
                outcome = json.loads((directory / 'outcome.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                outcome = {'state': 'failed', 'error_code': 'worker_crashed' if code else 'worker_channel_closed',
                           'error': f'Worker did not publish a completion envelope; exit={code}', 'exit_code': code}
            if code == 74:
                outcome = {'state': 'failed', 'error_code': 'result_save_failed',
                           'error': 'Worker completion envelope could not be saved; inspect worker.log', 'exit_code': code}
            if code and outcome.get('state') == 'succeeded':
                outcome = {'state': 'failed', 'error_code': 'worker_crashed',
                           'error': f'Worker exit={code}; saved output not accepted as success', 'exit_code': code}
            # A cancel accepted before completion wins, while a verified
            # best-so-far asset remains independently viewable.
            if self.memory_status(job_id).get('cancel_requested'):
                outcome = self._cancelled_outcome(job_id, outcome)
            elif outcome.get('state') in ('failed', 'cancelled'):
                outcome = self._attach_best_candidate_checkpoint(job_id, outcome)
            if outcome.get('state') == 'succeeded':
                try: outcome['result'] = self.register_artifacts(job_id, outcome['result'])
                except Exception as exc:
                    outcome = {'state': 'failed', 'error_code': 'result_save_failed', 'error': str(exc)}
                    outcome = self._attach_best_candidate_checkpoint(job_id, outcome)
            elif outcome.get('state') in ('failed', 'cancelled'):
                from .result_capability import is_best_candidate_result, is_film_optimization_result
                if is_best_candidate_result(outcome.get('result')):
                    if not is_film_optimization_result(outcome.get('result')):
                        try: outcome['result'] = self.register_artifacts(job_id, outcome['result'])
                        except Exception as exc:
                            outcome['result'] = None
                            outcome['result_unavailable_reason'] = f'Candidate artifact registration failed: {exc}'
            from .result_capability import is_best_candidate_result, is_film_optimization_result
            if (outcome.get('state') in ('failed', 'cancelled')
                    and is_best_candidate_result(outcome.get('result'))
                    and not is_film_optimization_result(outcome.get('result'))
                    and not any(isinstance(item, dict) and item.get('role') == 'bundle'
                                and item.get('artifact_id')
                                for item in outcome['result'].get('result_artifacts', []))):
                try: outcome['result'] = self.register_artifacts(job_id, outcome['result'])
                except Exception as exc:
                    outcome['result'] = None
                    outcome['result_unavailable_reason'] = f'Candidate artifact registration failed: {exc}'
            # Cancellation and completion serialize under one transaction boundary.
            with self.condition:
                if self.memory_status(job_id).get('cancel_requested'):
                    outcome = self._cancelled_outcome(job_id, outcome)
                    from .result_capability import is_best_candidate_result, is_film_optimization_result
                    if (is_best_candidate_result(outcome.get('result'))
                            and not is_film_optimization_result(outcome.get('result'))):
                        try: outcome['result'] = self.register_artifacts(job_id, outcome['result'])
                        except Exception as exc:
                            outcome['result'] = None
                            outcome['result_unavailable_reason'] = f'Candidate artifact registration failed: {exc}'
                self._commit('transition', job_id, **outcome)
            self.active = None
            if outcome.get('state') == 'cancelled':
                self._cleanup_cancel_marker(job_id)
        if self.stopping.is_set() or self.recovery_error: return
        claimed = self._commit('claim', self.owner)
        if claimed:
            value, spec = claimed
            try: self.start_job(value, spec)
            except Exception as exc:
                if self.active is not None:
                    # Cleanup failed: retain ownership and pause instead of
                    # releasing a slot while its worker may still be alive.
                    self.recovery_error = f'worker_start_cleanup_failed: {exc}'
                    raise
                try:
                    self._commit('transition', value['job_id'], state='failed', error_code='worker_start_failed', error=str(exc))
                except Exception as terminal_error:
                    exc.add_note(f'worker_start_terminal_persistence_failed: {terminal_error}')
                    raise exc from terminal_error

    def serve(self):
        scheduler = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                prefix = '/v1/artifacts/'
                if not self.path.startswith(prefix) or self.headers.get('Origin'):
                    self.send_error(403); return
                try: item = scheduler.resolve_artifact(self.path[len(prefix):])
                except ValueError:
                    self.send_error(404); return
                try:
                    with Path(item['local_path']).open('rb') as stream:
                        self.send_response(200); self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Length', str(item['bytes'])); self.end_headers()
                        import shutil
                        shutil.copyfileobj(stream, self.wfile, length=1024*1024)
                except (OSError, BrokenPipeError): pass
            def do_POST(self):
                # Browser pages cannot submit cross-origin requests to this local control API.
                if self.path != '/v1/rpc' or self.headers.get('Origin') or self.headers.get('Content-Type') != 'application/json':
                    self.send_error(403); return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if length < 1: raise ValueError('empty_request')
                    if length > MAX_RPC_BODY_BYTES: raise ValueError('request_body_too_large')
                    request = json.loads(self.rfile.read(length))
                    if request.get('protocol_version') != 1: raise ValueError('unsupported_protocol_version')
                    result = scheduler.dispatch(request['action'], request.get('arguments', {}))
                    body = json.dumps({'ok': True, 'protocol_version': 1, 'result': public_payload(result)}, ensure_ascii=False, allow_nan=False).encode('utf-8')
                    self.send_response(200)
                except Exception as exc:
                    body = json.dumps({'ok': False, 'protocol_version': 1, 'error': f'{type(exc).__name__}: {exc}'}).encode('utf-8')
                    self.send_response(400)
                self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError): pass
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        http.daemon_threads = True
        write_json(self.runtime / 'endpoint.json', {'protocol_version': 1, 'host': '127.0.0.1',
            'port': http.server_port, 'pid': os.getpid(), 'instance': self.owner})
        thread = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        thread.start()
        try:
            while not self.stopping.wait(.1): self.tick()
        finally:
            if self.active:
                job_id, process, _, _ = self.active
                process.terminate(); process.wait(timeout=10)
                with self.condition:
                    current = self.memory_status(job_id)
                    changes = _interrupted_job_changes(
                        current, 'Scheduler explicitly stopped while worker was running; no automatic replay')
                    self._commit('transition', job_id, **changes)
                if changes['state'] == 'cancelled':
                    self._cleanup_cancel_marker(job_id)
            self.guard.close()
            http.shutdown(); http.server_close(); thread.join(timeout=2)
            try:
                endpoint = json.loads((self.runtime / 'endpoint.json').read_text(encoding='utf-8'))
                if endpoint.get('instance') == self.owner: (self.runtime / 'endpoint.json').unlink(missing_ok=True)
            finally: self.lock.close()

def serve(root):
    try: scheduler = Scheduler(root)
    except OSError as exc:
        if getattr(exc, 'errno', None) in (13, 11, 36): return 0
        raise
    scheduler.serve()
    return 0
