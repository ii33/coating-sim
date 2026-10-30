"""Transport adapter. Starting a daemon never ties its lifetime to this client."""
import json
import math
import os
from pathlib import Path
import re
import subprocess
from typing import Any
import sys
import time
import threading
from urllib.request import Request, urlopen, ProxyHandler, build_opener
from urllib.error import URLError, HTTPError
from coat_sim.process_entry import is_frozen, module_command
from coat_sim.project_logging import log_path

class SchedulerClient:
    _endpoints = {}
    _downloads = {}
    _processes = {}
    _start_lock = threading.RLock()
    def __init__(self, root=None, *, transport=None):
        self.transport = transport
        self.root = Path(root or Path(__file__).resolve().parents[2]).resolve()
        self.endpoint = self.root / 'artifacts/runtime/scheduler/endpoint.json'
        self._opener = build_opener(ProxyHandler({}))

    def _endpoint(self):
        key = str(self.root)
        if key not in self._endpoints:
            self._endpoints[key] = json.loads(self.endpoint.read_text(encoding='utf-8'))
        return self._endpoints[key]

    def _call(self, action, *, start=True, **arguments):
        if self.transport is not None:
            return self.transport.request(action, arguments)
        body = json.dumps({'protocol_version': 1, 'action': action, 'arguments': arguments},
                          ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
        from .daemon import MAX_RPC_BODY_BYTES
        if len(body) > MAX_RPC_BODY_BYTES:
            raise ValueError('request_body_too_large')
        if start and (action == 'submit' or str(self.root) not in self._endpoints):
            # A background read may have cached an older live endpoint. Never
            # enqueue new work in a daemon absent from coordinated shutdown.
            self.ensure_started()
        endpoint = self._endpoint()
        if endpoint.get('protocol_version') != 1 or endpoint.get('host') != '127.0.0.1':
            raise ValueError('invalid_scheduler_endpoint')
        request = Request(f"http://127.0.0.1:{int(endpoint['port'])}/v1/rpc", body,
                          {'Content-Type': 'application/json'})
        try:
            with self._opener.open(request, timeout=40 if action == 'events' else 30) as response:
                value = json.load(response)
        except HTTPError as exc:
            value = json.load(exc)
        except (OSError, URLError):
            self._endpoints.pop(str(self.root), None)
            if start:
                # Requests are idempotent by job ID; one reconnect handles daemon restarts.
                self.ensure_started()
                return self._call(action, start=False, **arguments)
            raise
        if not value.get('ok'):
            raise ValueError(value.get('error', 'scheduler_request_failed'))
        return value['result']

    def ensure_started(self):
        with self._start_lock:
            return self._ensure_started()

    def _ensure_started(self):
        if self.transport is not None: return self.transport.request('health', {})
        try:
            value = self._call('health', start=False)
            if not value.get('stopping'):
                self._require_lifecycle(value)
                return value
        except (OSError, ValueError, URLError):
            pass
        self._endpoints.pop(str(self.root), None)
        runtime = self.endpoint.parent
        runtime.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {'cwd': str(Path(__file__).resolve().parents[2]), 'stdin': subprocess.DEVNULL}
        if os.name == 'nt':
            # Also select pythonw below: Windows' venv redirector can allocate
            # a console for its real interpreter child despite these flags.
            options['creationflags'] = vars(subprocess)["CREATE_NO_WINDOW"] | vars(subprocess)["CREATE_NEW_PROCESS_GROUP"]
        else:
            options['start_new_session'] = True
        # Do not inherit a parent's stdio pipe: a closed MCP host cannot kill/block us.
        interpreter = None
        if os.name == 'nt' and not is_frozen():
            executable = Path(sys.executable)
            gui_interpreter = executable.with_name('pythonw.exe')
            if executable.name.casefold() == 'python.exe' and gui_interpreter.is_file():
                # Same environment and ABI; never search PATH or another install.
                interpreter = str(gui_interpreter)
        from .lifecycle import acquire_start_gate, current_start_epoch
        gate = acquire_start_gate(timeout=3.0)
        try:
            launch_epoch = current_start_epoch()
            command = module_command('coat_sim.scheduler', '--root', str(self.root),
                                     'serve', '--launch-epoch', launch_epoch, interpreter=interpreter)
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        finally:
            gate.close()
        self._processes[str(self.root)] = process
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                value = self._call('health', start=False)
                if not value.get('stopping'):
                    self._require_lifecycle(value)
                    return value
                self._endpoints.pop(str(self.root), None)
            except (OSError, ValueError, URLError):
                # A global exit may have fenced publication after this child
                # was spawned. Do not wait 90s or start another child.
                # The child itself briefly holds the gate while publishing.
                # A changed durable epoch, rather than lock contention, is the
                # authoritative signal that this pending launch was cancelled.
                if current_start_epoch() != launch_epoch:
                    raise RuntimeError('scheduler_start_cancelled') from None
                if process.poll() not in (None, 0):
                    raise RuntimeError(f'scheduler_start_failed: exit={process.returncode}; {log_path("scheduler", self.root)}') from None
                time.sleep(.1)
        raise RuntimeError('scheduler_start_timeout: inspect ' + str(log_path("scheduler", self.root)))

    @staticmethod
    def _require_lifecycle(health):
        if health.get('lifecycle_protocol') != 1:
            raise RuntimeError('scheduler_lifecycle_upgrade_required: existing scheduler must be closed normally before using this version')

    def snapshot_if_running(self, after_event=0, limit=100, refresh_estimates=False):
        """Read an existing exact daemon; background polling never starts one."""
        if self.transport is not None:
            if refresh_estimates:
                self._call('refresh_estimates', start=False)
            return self._call('snapshot', start=False, after_event=after_event, limit=limit)
        try:
            endpoint = json.loads(self.endpoint.read_text(encoding='utf-8'))
        except FileNotFoundError:
            self._endpoints.pop(str(self.root), None)
            return None
        if (not isinstance(endpoint, dict) or endpoint.get('protocol_version') != 1
                or endpoint.get('host') != '127.0.0.1'
                or type(endpoint.get('port')) is not int or not 0 < endpoint['port'] < 65536
                or type(endpoint.get('pid')) is not int or endpoint['pid'] <= 0
                or not isinstance(endpoint.get('instance'), str) or not endpoint['instance']):
            raise ValueError('invalid_scheduler_endpoint')
        from coat_sim.core.process_identity import _process_identity
        state, start = _process_identity(endpoint['pid'])
        if state == 'dead':
            self._endpoints.pop(str(self.root), None)
            return None
        if state != 'alive' or not start:
            raise RuntimeError('scheduler_process_identity_unknown')
        self._endpoints[str(self.root)] = endpoint
        try:
            health = self._call('health', start=False)
            if (health.get('pid') != endpoint['pid'] or health.get('instance') != endpoint['instance']
                    or (health.get('process_start') is not None and health['process_start'] != start)):
                raise RuntimeError('scheduler_process_identity_mismatch')
            if health.get('stopping'):
                return None
            if refresh_estimates:
                self._call('refresh_estimates', start=False)
            return self._call('snapshot', start=False, after_event=after_event, limit=limit)
        except (OSError, URLError):
            state, current = _process_identity(endpoint['pid'])
            if state == 'dead' or (current is not None and current != start):
                return None
            # Shutdown withdraws discovery before closing HTTP. The process may
            # still be alive while its interpreter and owned resources unwind.
            try:
                self.endpoint.stat()
            except FileNotFoundError:
                return None
            raise RuntimeError('scheduler_unavailable') from None

    def submit(self, spec, job_id=None, metadata=None):
        from uuid import uuid4
        job_id = job_id or uuid4().hex
        return self._call('submit', spec=spec, job_id=job_id, metadata=metadata or {})
    def status(self, job_id): return self._call('status', job_id=job_id)
    def read_job(self, job_id): return self.status(job_id)
    def repair_live(self, job_id, *, input_digest, after_sequence=-1):
        """Read one bound repair snapshot; never start or replay computation."""
        return self._call('repair_live', start=False, job_id=job_id,
                          input_digest=input_digest, after_sequence=after_sequence)
    def reduction_live(self, job_id, *, input_digest, after_sequence=-1):
        """Read one bound reduction feedback frame; never start or replay computation."""
        return self._call('reduction_live', start=False, job_id=job_id,
                          input_digest=input_digest, after_sequence=after_sequence)
    def cancel(self, job_id): return self._call('cancel', job_id=job_id)
    def film_display_control(self, job_id, *, input_digest, generation, enabled, selection):
        # Read/CAS only in the caller's existing background control pump. Never
        # blindly retry a stale desired value after another UI intent arrived.
        row = self._call('status', start=False, job_id=job_id)
        if (row.get('input_digest') != input_digest
                or row.get('film_display_config', {}).get('generation') != generation):
            raise ValueError('film_display_control_identity_mismatch')
        return self._call('film_display_control', start=False, job_id=job_id,
            input_digest=input_digest, generation=generation, enabled=enabled,
            selection=selection, expected_sequence=row.get('film_display_control_sequence', 0))
    def request_cancel(self, job_id): return self.cancel(job_id)['cancel_requested']
    def result(self, job_id): return self._call('result', job_id=job_id)
    def snapshot(self, after_event=0, limit=100): return self._call('snapshot', after_event=after_event, limit=limit)
    def events(self, after_sequence=0, wait_s=0): return self._call('events', after_sequence=after_sequence, wait_s=wait_s)
    def wait_for_terminal(self, job_id, on_update=None, should_stop=None, wait_s=1.0):
        """Wait on the event stream, reconciling resets and legacy servers."""
        wait_s = float(wait_s)
        if not math.isfinite(wait_s): raise ValueError('invalid_event_wait')
        wait_s = min(1.0, max(0., wait_s))
        initial = self.snapshot(limit=1)
        cursor = int(initial.get('sequence', initial.get('latest_event_id', 0)))
        current = next((job for job in initial.get('jobs', [])
                        if job.get('job_id') == job_id), None)
        if current is None: current = self.status(job_id)
        if on_update: on_update(current)

        event_actions_supported = True
        while current.get('state') not in ('succeeded', 'failed', 'cancelled'):
            if should_stop and should_stop(): return current
            if event_actions_supported:
                try:
                    response = self.events(after_sequence=cursor, wait_s=wait_s)
                except ValueError as exc:
                    if 'unknown_scheduler_action' not in str(exc): raise
                    event_actions_supported = False
                    continue
                if response.get('reset'):
                    snapshot = response.get('snapshot')
                    if not isinstance(snapshot, dict): snapshot = self.snapshot(limit=1)
                    cursor = int(snapshot.get('sequence', snapshot.get('latest_event_id', 0)))
                    current = self.status(job_id)
                    if on_update: on_update(current)
                    continue
                for event in response.get('events') or []:
                    cursor = max(cursor, int(event.get('sequence', event.get('event_id', cursor))))
                    if event.get('job_id') == job_id:
                        current = event
                        if on_update: on_update(current)
                        if current.get('state') in ('succeeded', 'failed', 'cancelled'):
                            break
                # Drain the latest side-channel even beside a terminal event.
                # It cannot affect the official terminal state or result.
                for packet in response.get('film_displays') or []:
                    if isinstance(packet, dict) and packet.get('job_id') == job_id:
                        current = {**current, 'film_display': packet}
                        if on_update:
                            on_update(current)
                cursor = max(cursor, int(response.get('sequence', cursor)))
            else:
                if wait_s: time.sleep(wait_s)
                if should_stop and should_stop(): return current
                current = self.status(job_id)
                if on_update: on_update(current)
        return current
    def refresh_estimates(self): return self._call('refresh_estimates')
    def capabilities(self): return self._call('capabilities')
    def artifact(self, artifact_id): return self._call('artifact', artifact_id=artifact_id)
    def run_result(self, run_id): return self._call('run_result', run_id=run_id)
    def find_request(self, client_request_id): return self._call('find_request', client_request_id=client_request_id)
    def materialize_artifact(self, artifact_id, metadata=None):
        """Return a verified client-side file, independent of server filesystem paths."""
        if not isinstance(artifact_id, str) or not re.fullmatch(r'[0-9a-f]{64}', artifact_id):
            raise ValueError('invalid_artifact_id')

        # A cache hit is only useful when its expected identity is available.
        # Fetch metadata before consulting the cache when callers only have an ID.
        metadata = metadata if metadata is not None else self.artifact(artifact_id)
        if not isinstance(metadata, dict):
            raise ValueError('invalid_artifact_metadata')
        if metadata.get('artifact_id') not in (None, artifact_id):
            raise ValueError('artifact_metadata_mismatch')
        name = metadata.get('name')
        from pathlib import PureWindowsPath
        if (not isinstance(name, str) or not name or name in ('.', '..')
                or '\x00' in name or Path(name).name != name
                or PureWindowsPath(name).name != name):
            raise ValueError('invalid_artifact_name')
        expected_bytes = metadata.get('bytes')
        expected_sha256 = metadata.get('sha256')
        if (type(expected_bytes) is not int or expected_bytes < 0
                or not isinstance(expected_sha256, str)
                or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)):
            raise ValueError('invalid_artifact_metadata')

        cache_key = (str(self.root), artifact_id)
        cached = self._downloads.get(cache_key)
        path = self.root / 'artifacts' / 'runtime' / 'scheduler' / 'downloads' / artifact_id / name

        def matches_descriptor(candidate):
            import hashlib
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    return False
                if candidate.stat().st_size != expected_bytes:
                    return False
                digest = hashlib.sha256()
                with candidate.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                return digest.hexdigest() == expected_sha256
            except OSError:
                return False

        if cached is not None:
            try:
                cached_path = Path(cached)
            except (TypeError, ValueError):
                cached_path = None
            if cached_path == path and matches_descriptor(cached_path):
                return cached_path

            # Discard the in-memory pointer. Remove only the expected cache file;
            # a stale mapping must never authorize deleting an arbitrary path.
            self._downloads.pop(cache_key, None)
            if cached_path == path:
                try:
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                except OSError as exc:
                    raise ValueError('artifact_cache_invalid') from exc

        path = self.fetch_artifact(artifact_id, path, metadata=metadata)
        self._downloads[cache_key] = path
        return path
    def fetch_artifact(self, artifact_id, destination, *, metadata=None):
        """Transport-neutral retrieval. No client reads service-side SQLite/JSON."""
        if self.transport is not None: return self.transport.download(artifact_id, destination)
        if metadata is None:
            self.ensure_started()
        elif str(self.root) not in self._endpoints:
            self.ensure_started()
        endpoint = self._endpoint()
        metadata = metadata or self.artifact(artifact_id)
        if metadata.get('artifact_id') not in (None, artifact_id):
            raise ValueError('artifact_metadata_mismatch')
        from .transport import download_verified
        return download_verified(self._opener, f"http://127.0.0.1:{endpoint['port']}/v1/artifacts/{artifact_id}", destination, metadata)
    def queue_jobs_page(self, bucket, before=None, limit=50): return self._call('page', bucket=bucket, before=before, limit=limit)
    def result_asset_path(self, job_id):
        result = self.result(job_id)
        if not isinstance(result, dict):
            raise ValueError('scheduler_result_invalid_shape: expected a result object')
        if 'result_artifacts' not in result or not isinstance(result['result_artifacts'], list):
            raise ValueError('scheduler_result_missing_artifact_manifest: expected result_artifacts list')
        bundle = next((item for item in result['result_artifacts']
                       if isinstance(item, dict) and item.get('role') == 'bundle'), None)
        if not bundle or not bundle.get('artifact_id'):
            raise ValueError('result_has_no_bundle')
        return self.materialize_artifact(bundle['artifact_id'], bundle)
    def stop(self):
        value = self._call('stop', start=False)
        self._endpoints.pop(str(self.root), None)
        process = self._processes.pop(str(self.root), None)
        if process is not None:
            process.wait(timeout=30)
        return value
