"""Explicit headless Compute Service API v1 over the durable scheduler.

Run with ``python -m coat_sim.scheduler.remote_server --help``. No local-client
discovery or GUI is involved. See docs/remote-compute-service.md for boundaries.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import subprocess
from typing import Any
import tempfile
import threading
from urllib.parse import urlsplit

from .daemon import Scheduler, _interrupted_job_changes, public_payload, _process_identity
from .io import replace_with_retry, write_json, write_text

API_VERSION = 1
MAX_CONTROL_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_SPEC_BYTES = 20 * 1024 * 1024
MAX_BLOB_BYTES = 512 * 1024 * 1024
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_JOB = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


class ServiceError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ServiceError('duplicate_json_field')
        value[key] = item
    return value


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ServiceError('nonfinite_json')))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ServiceError('invalid_json') from exc


def _public(value):
    value = public_payload(value)
    def clean(item):
        if isinstance(item, dict):
            result = {}
            for key, content in item.items():
                if key in {'original_path', 'manifest_file', 'traceback'}:
                    continue
                if key in {'error', 'history_warning', 'recovery_error'} and content:
                    content = item.get('error_code') or 'service_diagnostic_available_on_server'
                result[key] = clean(content)
            return result
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item
    return clean(value)


def _digest(value):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ServiceError('invalid_sha256')
    return value


def _worker_env_without_token(environment, token):
    """Return a child-only copy without the service's in-memory bearer value."""
    if not token:
        return dict(environment)
    return {key: value for key, value in environment.items() if value != token}


def _integer(value, code, low=0, high=2**63-1):
    if type(value) is not int or not low <= value <= high:
        raise ServiceError(code)
    return value


class BlobStore:
    """Content-addressed input bytes, bounded uploads, atomic verified publish."""
    def __init__(self, root, max_bytes, quota_bytes):
        self.root = Path(root).resolve()
        self.directory = self.root / 'artifacts/runtime/remote/blobs'
        self.directory.mkdir(parents=True, exist_ok=True)
        self._check(self.directory)
        self.max_bytes = _integer(max_bytes, 'invalid_blob_limit', 1, MAX_BLOB_BYTES)
        self.quota_bytes = _integer(quota_bytes, 'invalid_blob_quota', self.max_bytes)
        self.lock = threading.RLock()
        self.reserved = 0

    def _check(self, path):
        if not path.resolve().is_relative_to(self.root):
            raise ServiceError('storage_path_invalid', 500)
        current = path
        while current != self.root:
            if current.is_symlink():
                raise ServiceError('storage_path_invalid', 500)
            current = current.parent
        return path

    def path(self, digest):
        return self._check(self.directory / _digest(digest))

    def read(self, digest):
        path = self.path(digest)
        try:
            with path.open('rb') as stream:
                data = stream.read(MAX_SPEC_BYTES + 1)
        except FileNotFoundError as exc:
            raise ServiceError('blob_not_found', 404) from exc
        if len(data) > MAX_SPEC_BYTES:
            raise ServiceError('spec_too_large', 413)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ServiceError('blob_integrity_failed')
        return data

    def receive(self, digest, length, stream):
        path = self.path(digest)
        _integer(length, 'invalid_content_length', 0, self.max_bytes)
        if path.is_file():
            existing = hashlib.sha256()
            with path.open('rb') as saved:
                for block in iter(lambda: saved.read(1024*1024), b''):
                    existing.update(block)
            if path.stat().st_size != length or existing.hexdigest() != digest:
                raise ServiceError('blob_integrity_failed')
            received = hashlib.sha256()
            remaining = length
            while remaining:
                block = stream.read(min(1024*1024, remaining))
                if not block:
                    raise ServiceError('incomplete_upload')
                received.update(block)
                remaining -= len(block)
            if received.hexdigest() != digest:
                raise ServiceError('blob_digest_mismatch')
            return {'sha256': digest, 'bytes': length}
        with self.lock:
            used = sum(p.stat().st_size for p in self.directory.iterdir()
                       if _DIGEST.fullmatch(p.name) and p.is_file())
            if used + self.reserved + length > self.quota_bytes:
                raise ServiceError('blob_quota_exceeded', 413)
            self.reserved += length
        temporary = None
        try:
            checksum = hashlib.sha256()
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as target:
                temporary = Path(target.name)
                remaining = length
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ServiceError('incomplete_upload')
                    target.write(block)
                    checksum.update(block)
                    remaining -= len(block)
                target.flush()
                os.fsync(target.fileno())
            if checksum.hexdigest() != digest:
                raise ServiceError('blob_digest_mismatch')
            with self.lock:
                # Replacing the same identity is safe only after full verification.
                self._check(path)
                replace_with_retry(temporary, path)
            return {'sha256': digest, 'bytes': length}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            with self.lock:
                self.reserved -= length


class _BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, handler, *, limit, tls_context=None):
        self.slots = threading.BoundedSemaphore(limit)
        self.requests = set()
        self.requests_lock = threading.RLock()
        self.tls_context = tls_context
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            # TLS has not handshaken yet; close overloaded connections safely.
            self.shutdown_request(request)
            return
        try:
            with self.requests_lock:
                self.requests.add(request)
            super().process_request(request, address)
        except BaseException:
            with self.requests_lock:
                self.requests.discard(request)
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        original = request
        try:
            request.settimeout(40)
            if self.tls_context is not None:
                request = self.tls_context.wrap_socket(request, server_side=True)
                with self.requests_lock:
                    self.requests.discard(original)
                    self.requests.add(request)
            super().process_request_thread(request, address)
        except (OSError, ssl.SSLError):
            self.shutdown_request(request)
        finally:
            with self.requests_lock:
                self.requests.discard(original)
                self.requests.discard(request)
            self.slots.release()

    def close_requests(self):
        with self.requests_lock:
            for request in tuple(self.requests):
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                request.close()


class RemoteScheduler(Scheduler):
    """The same queue lifecycle, launching the source-only remote worker adapter."""
    def register_artifacts(self, job_id, result):
        from .result_capability import is_film_optimization_result
        if is_film_optimization_result(result):
            # This typed result is inline; adding an empty binary manifest would
            # violate its exact existing schema and hide a successful result.
            return result
        registered = super().register_artifacts(job_id, result)
        if result.get('kind') == 'appearance_render':
            from .remote_appearance import attach_appearance_output_artifacts
            return attach_appearance_output_artifacts(self.root, job_id, registered)
        return registered

    def _attach_best_candidate_checkpoint(self, job_id, outcome):
        if self.memory_status(job_id).get('kind') != 'film_optimization':
            return super()._attach_best_candidate_checkpoint(job_id, outcome)
        result = dict(outcome)
        result['result'] = None
        result['result_unavailable_reason'] = 'verified_film_candidate_unavailable'
        directory = self.runtime / 'jobs' / job_id
        pointer = directory / 'best-candidate.json'
        try:
            if pointer.is_symlink() or not pointer.resolve().is_relative_to(directory.resolve()):
                return result
            if not pointer.resolve().is_relative_to(self.root) or pointer.stat().st_size > MAX_RESPONSE_BYTES:
                return result
            envelope = _json(pointer.read_bytes())
            from coat_sim.core.film_design_service import FilmDesignService
            from .result_capability import validate_film_optimization_result
            result['result'] = validate_film_optimization_result(FilmDesignService(self.root), envelope['result'])
            result.pop('result_unavailable_reason', None)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return result

    def start_job(self, value, spec):
        from coat_sim.process_entry import module_command
        from .jobs import JobSpec
        job_id = value['job_id']
        directory = self.runtime / 'jobs' / job_id
        directory.mkdir(parents=True, exist_ok=True)
        frozen = JobSpec.parse_frozen(spec)
        if frozen.fingerprint != value['input_digest']:
            raise ValueError('frozen_input_digest_mismatch')
        write_text(directory / 'spec.json', frozen.canonical_json)
        options: dict[str, Any] = {'cwd': str(Path(__file__).resolve().parents[2]), 'stdin': subprocess.DEVNULL}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        else:
            options['start_new_session'] = True
        interpreter = None
        if frozen.executor == 'appearance_render':
            from .remote_appearance import appearance_runtime_environment, appearance_runtime_interpreter
            request = frozen.parameters.get('frozen', {}).get('request', {})
            backend = request.get('backend') if isinstance(request, dict) else None
            device_id = frozen.inputs.get('device_binding', {}).get('device_id')
            if (backend, device_id) not in {('cpu', 'cpu:default'), ('gpu_cuda', 'cuda:default')}:
                raise ValueError('appearance_device_binding_mismatch')
            interpreter = appearance_runtime_interpreter(self.root, backend)
            options['env'] = appearance_runtime_environment(self.root, interpreter, backend)
        # The HTTP bearer credential belongs only to the service process.
        # Never copy its value into a compute child, regardless of the
        # administrator's chosen token environment-variable name.
        options['env'] = _worker_env_without_token(
            options.get('env', os.environ), getattr(self, '_remote_bearer_token', None))
        command = module_command('coat_sim.scheduler.remote_worker', str(self.root), job_id,
                                 str(os.getpid()), self.owner, interpreter=interpreter)
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        try:
            self.guard.attach(process)
            self.store.set_worker(job_id, process.pid, _process_identity(process.pid)[1])
            write_json(directory / 'launch.json', {'instance': self.owner})
        except BaseException:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            raise
        self.active = (job_id, process, directory, None)


class RemoteComputeService:
    def __init__(self, data_root, *, host='127.0.0.1', port=0, token=None,
                 tls_cert=None, tls_key=None, insecure_loopback=False,
                 allow_insecure_http=False,
                 allow_probe=False, max_connections=16,
                 max_blob_bytes=MAX_BLOB_BYTES, blob_quota_bytes=2*1024**3):
        if type(allow_insecure_http) is not bool or type(insecure_loopback) is not bool:
            raise ServiceError('invalid_insecure_http_option')
        if type(allow_probe) is not bool:
            raise ServiceError('invalid_probe_option')
        if allow_insecure_http and (insecure_loopback or tls_cert is not None or tls_key is not None):
            raise ServiceError('insecure_http_mode_conflict')
        address = ipaddress.ip_address(host)
        if address.version != 4:
            raise ServiceError('ipv4_listen_address_required')
        if insecure_loopback and not address.is_loopback:
            raise ServiceError('insecure_listening_requires_loopback')
        if allow_probe and not (insecure_loopback and address.is_loopback):
            raise ServiceError('probe_requires_insecure_loopback_test_mode')
        _integer(port, 'invalid_port', 0, 65535)
        _integer(max_connections, 'invalid_connection_limit', 1, 128)
        if token is not None and (not isinstance(token, str) or len(token) < 16
                                  or not token.isascii()
                                  or any(not 33 <= ord(c) <= 126 for c in token)):
            raise ServiceError('invalid_service_token')
        if allow_insecure_http and token is None:
            raise ServiceError('insecure_http_requires_authentication')
        tls_context = None
        if bool(tls_cert) != bool(tls_key):
            raise ServiceError('tls_certificate_and_key_required')
        if tls_cert:
            tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
            tls_context.load_cert_chain(tls_cert, tls_key)
        if not insecure_loopback and not allow_insecure_http and (not tls_context or token is None):
            raise ServiceError('tls_and_authentication_required')
        # Initialize native numeric libraries on the startup thread. Loading the
        # first NumPy extension inside an HTTP request thread can stall DLL
        # initialization on Windows; readiness must follow dependency loading.
        import numpy  # noqa: F401
        self.root = Path(data_root).resolve()
        self.token, self.allow_probe = token, allow_probe
        self.allow_insecure_http = allow_insecure_http
        self.blobs = BlobStore(self.root, max_blob_bytes, blob_quota_bytes)
        self.scheduler = RemoteScheduler(self.root)
        self.scheduler._remote_bearer_token = token
        self.http = None
        self.host, self.port = host, port
        self.tls_context = tls_context
        self.max_connections = max_connections
        from .remote_appearance import probe_appearance_cpu_runtime, probe_appearance_cuda_runtime
        if self.root.is_dir():
            self.appearance_cpu_available, self.appearance_cpu_reason = probe_appearance_cpu_runtime(self.root)
            self.appearance_cuda_available, self.appearance_cuda_reason = probe_appearance_cuda_runtime(self.root)
        else:
            # A substituted scheduler in policy-only tests need not create a
            # project root; it must never advertise an unprobed renderer.
            self.appearance_cpu_available, self.appearance_cpu_reason = False, 'appearance_cpu_runtime_missing'
            self.appearance_cuda_available, self.appearance_cuda_reason = False, 'appearance_cuda_runtime_missing'

    def _authorize(self, handler):
        if handler.headers.get('Origin') is not None:
            raise ServiceError('browser_origin_rejected', 403)
        if self.token is not None:
            presented = handler.headers.get('Authorization', '')
            if not hmac.compare_digest(presented.encode('utf-8'), ('Bearer ' + self.token).encode('utf-8')):
                raise ServiceError('authentication_required', 401)

    def _artifact(self, artifact_id):
        _digest(artifact_id)
        try:
            item = self.scheduler.resolve_artifact(artifact_id)
        except ValueError as exc:
            raise ServiceError('artifact_not_found', 404) from exc
        path = Path(item['local_path'])
        self.blobs._check(path)
        if not path.is_file():
            raise ServiceError('artifact_not_found', 404)
        return item, path

    def _register_asset(self, value):
        if (not isinstance(value, dict) or set(value) != {'api_version', 'kind', 'blob'}
                or type(value['api_version']) is not int or value['api_version'] != 1
                or value['kind'] not in {'deposition_bundle', 'appearance_bundle'}):
            raise ServiceError('invalid_asset_registration')
        record = value['blob']
        if not isinstance(record, dict) or set(record) != {'sha256', 'bytes'}:
            raise ServiceError('invalid_blob_manifest')
        digest = _digest(record['sha256'])
        size = _integer(record['bytes'], 'invalid_blob_bytes', 1, self.blobs.max_bytes)
        if value['kind'] == 'appearance_bundle':
            from .remote_appearance import register_appearance_bundle
            return register_appearance_bundle(self, digest, size)
        source = self.blobs.path(digest)
        if not source.is_file():
            raise ServiceError('blob_not_found', 404)
        checksum = hashlib.sha256()
        with source.open('rb') as stream:
            for block in iter(lambda: stream.read(1024*1024), b''):
                checksum.update(block)
        if source.stat().st_size != size or checksum.hexdigest() != digest:
            raise ServiceError('blob_integrity_failed')
        # Validate the same immutable content. Extension is intentionally fixed.
        target = self.blobs.directory.parent / 'input-assets' / digest / 'input.coatasset'
        target.parent.mkdir(parents=True, exist_ok=True)
        self.blobs._check(target)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                import shutil
                with source.open('rb') as stream:
                    shutil.copyfileobj(stream, output, 1024*1024)
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                output.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            from coat_sim.core.simulation_assets import read_simulation_asset
            validated = read_simulation_asset(temporary, detail='full', include_arrays=False)
            if not validated.get('has_results'):
                raise ServiceError('deposition_bundle_has_no_results')
            replace_with_retry(temporary, target)
        except ValueError as exc:
            raise ServiceError('invalid_deposition_bundle') from exc
        finally:
            temporary.unlink(missing_ok=True)
        artifact_id = hashlib.sha256(('remote-input:' + digest).encode()).hexdigest()
        item = {'artifact_id': artifact_id, 'sha256': digest, 'bytes': size,
                'role': 'bundle', 'name': 'input.coatasset', 'local_path': str(target)}
        with self.scheduler.condition:
            if self.scheduler.stopping.is_set():
                raise ServiceError('scheduler_stopping', 503)
            with self.scheduler.store.queue.connection() as db:
                db.execute('INSERT OR IGNORE INTO scheduler_artifacts(artifact_id,job_id,metadata) VALUES(?,?,?)',
                           (artifact_id, 'remote-input', json.dumps(item)))
            self.scheduler.artifact_index[artifact_id] = item
        return _public(item)

    def _submit(self, value):
        from .remote_contract import validate_submission
        job_id, spec, metadata = validate_submission(value, self.blobs.read, allow_probe=self.allow_probe)
        device_id = metadata.get('device_id')
        external_spec_sha256 = metadata['spec_sha256'] if device_id is not None else None
        try:
            prior = self.scheduler.memory_status(job_id)
        except ValueError:
            prior = None
        if prior is not None:
            saved = prior.get('input_summary') or {}
            previous_device = saved.get('device_id') if isinstance(saved, dict) else None
            previous_spec = saved.get('external_spec_sha256') if isinstance(saved, dict) else None
            if previous_device != device_id or (device_id is not None and previous_spec != external_spec_sha256):
                raise ServiceError('request_id_conflict', 409)
            if device_id is not None:
                # A retry after a lost HTTP acknowledgement must read the
                # durable admission, even when the device has since vanished.
                return self._with_requested_device(prior)
        if device_id is not None:
            if device_id == 'cuda:default':
                # Discovery is not an admission guarantee: verify the GPU is
                # still allocatable immediately before accepting the job.
                from .remote_appearance import probe_appearance_cuda_runtime
                self.appearance_cuda_available, self.appearance_cuda_reason = probe_appearance_cuda_runtime(self.root)
            supported = next((row for row in self._capabilities()['devices']
                              if row['id'] == device_id), None)
            if supported is None:
                raise ServiceError('unsupported_device')
            if not supported['available'] or spec.executor not in supported['executors']:
                raise ServiceError('device_unavailable')
            if spec.executor == 'process_recipe' and spec.parameters.get('algorithm', 'fast') not in supported['algorithms']:
                raise ServiceError('unsupported_device_algorithm')
            if spec.executor == 'appearance_render':
                from .remote_appearance import prepare_appearance_submission
                spec = prepare_appearance_submission(self, spec, job_id)
        spec = spec.to_dict() if hasattr(spec, 'to_dict') else spec
        reference = spec['inputs'].get('deposition_artifact')
        if reference:
            item, _ = self._artifact(reference['artifact_id'])
            if item['sha256'] != reference['sha256'] or item['role'] != 'bundle':
                raise ServiceError('deposition_artifact_mismatch')
        receipt = self.scheduler.dispatch('submit', {'spec': spec, 'job_id': job_id,
            'metadata': {'input_summary': {'device_id': device_id,
                                           'external_spec_sha256': external_spec_sha256}}
                        if device_id else {}})
        if device_id is not None:
            receipt['requested_device_id'] = device_id
        return receipt

    @staticmethod
    def _with_requested_device(record):
        if not isinstance(record, dict):
            return record
        summary = record.get('input_summary')
        device_id = summary.get('device_id') if isinstance(summary, dict) else None
        if isinstance(device_id, str) and device_id:
            return {**record, 'requested_device_id': device_id}
        return record

    def _rpc(self, value):
        if (not isinstance(value, dict) or set(value) != {'api_version', 'action', 'arguments'}
                or type(value['api_version']) is not int or value['api_version'] != 1
                or not isinstance(value['arguments'], dict)):
            raise ServiceError('invalid_rpc')
        action, args = value['action'], value['arguments']
        schemas = {'status': {'job_id'}, 'cancel': {'job_id'}, 'result': {'job_id'},
                   'snapshot': {'after_event', 'limit'}, 'events': {'after_sequence', 'wait_s'},
                   'page': {'bucket', 'before', 'limit'}, 'artifact': {'artifact_id'},
                   'run_result': {'run_id'}, 'find_request': {'client_request_id'},
                   'refresh_estimates': set()}
        if not isinstance(action, str) or action not in schemas or set(args) - schemas[action]:
            raise ServiceError('unsupported_action')
        required = {'status': 'job_id', 'cancel': 'job_id', 'result': 'job_id', 'artifact': 'artifact_id',
                    'run_result': 'run_id', 'find_request': 'client_request_id', 'page': 'bucket'}
        if action in required and required[action] not in args:
            raise ServiceError('missing_argument')
        for key in ('job_id', 'run_id', 'client_request_id'):
            if key in args and (not isinstance(args[key], str) or not _JOB.fullmatch(args[key])):
                raise ServiceError('invalid_identifier')
        for key in ('after_event', 'after_sequence'):
            if key in args:
                _integer(args[key], 'invalid_cursor')
        if 'limit' in args:
            _integer(args['limit'], 'invalid_limit', 1, 1000)
        if args.get('before') is not None:
            _integer(args['before'], 'invalid_cursor', 1)
        if 'wait_s' in args:
            import math
            wait = args['wait_s']
            if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or not 0 <= wait <= 30:
                raise ServiceError('invalid_wait')
        if action == 'artifact':
            item, _ = self._artifact(args['artifact_id'])
            return item
        if action in {'status', 'cancel', 'result'}:
            try:
                current = self.scheduler.memory_status(args['job_id'])
            except ValueError as exc:
                raise ServiceError('job_not_found', 404) from exc
            if action == 'status':
                return self._with_requested_device(current)
        if action == 'page' and args['bucket'] not in ('recent', 'history'):
            raise ServiceError('invalid_bucket')
        if action in {'cancel', 'refresh_estimates'}:
            with self.scheduler.condition:
                if self.scheduler.stopping.is_set():
                    raise ServiceError('scheduler_stopping', 503)
                return self._with_requested_device(self.scheduler.dispatch(action, args))
        return self.scheduler.dispatch(action, args)

    def _capabilities(self):
        from .remote_contract import REMOTE_EXECUTORS
        selected = ['process_recipe', 'film_optimization']
        if self.appearance_cpu_available:
            selected.append('appearance_render')
        gpu = {'id': 'cuda:default', 'name': 'NVIDIA CUDA 默认设备 · 未指定物理 GPU',
               'backend': 'gpu_cuda', 'available': self.appearance_cuda_available,
               'physical_id': '', 'binding': 'default',
               'runtime_probe': 'passed' if self.appearance_cuda_available else 'failed',
               'reason': '' if self.appearance_cuda_available else self.appearance_cuda_reason,
               'algorithms': [], 'executors': ['appearance_render']}
        return {'api_version': 1, 'protocol_version': 1,
                'executor_schema_version': 1, 'device_schema_version': 1,
                'device_binding_version': 1,
                'devices': [{'id': 'cpu:default', 'name': 'CPU 默认设备',
                             'backend': 'CPU', 'available': True, 'physical_id': '',
                             'logical_cpu_count': os.cpu_count(),
                             'reason': '', 'algorithms': ['fast', 'reference', 'optimize'],
                             'executors': selected}, gpu],
                'executors': sorted(REMOTE_EXECUTORS - {'appearance_render'}
                                    if not (self.appearance_cpu_available or self.appearance_cuda_available)
                                    else REMOTE_EXECUTORS)
                             + (['probe'] if self.allow_probe else []),
                'execution': 'single_worker', 'authorization': 'single_operator_namespace',
                'authentication': 'bearer_token' if self.token else 'explicit_loopback_test',
                'max_spec_bytes': MAX_SPEC_BYTES, 'max_blob_bytes': self.blobs.max_bytes,
                'max_json_nodes': 200000, 'max_json_depth': 64,
                'blob_quota_bytes': self.blobs.quota_bytes, 'max_connections': self.max_connections}

    def handler(self):
        service = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _length(self, limit):
                values = self.headers.get_all('Content-Length', [])
                if len(values) != 1 or not values[0].isdigit() or self.headers.get('Transfer-Encoding'):
                    raise ServiceError('content_length_required')
                length = int(values[0])
                if length > limit:
                    raise ServiceError('request_too_large', 413)
                return length

            def _body(self):
                if self.headers.get('Content-Type') != 'application/json':
                    raise ServiceError('json_content_type_required')
                length = self._length(MAX_CONTROL_BYTES)
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ServiceError('incomplete_request')
                return _json(data)

            def _respond(self, value, status=200):
                body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ServiceError('response_too_large', 413)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, verb):
                try:
                    service._authorize(self)
                    parsed = urlsplit(self.path)
                    if parsed.query or parsed.fragment or parsed.scheme or parsed.netloc:
                        raise ServiceError('invalid_route')
                    path = parsed.path
                    if verb == 'GET' and path == '/api/v1/health':
                        result = service.scheduler.dispatch('health', {})
                        result.update(api_version=1, ready=not result.get('recovery_error') and not result['stopping'])
                    elif verb == 'GET' and path == '/api/v1/capabilities':
                        result = service._capabilities()
                    elif verb == 'PUT' and path.startswith('/api/v1/blobs/'):
                        digest = _digest(path[len('/api/v1/blobs/'):])
                        result = service.blobs.receive(digest, self._length(service.blobs.max_bytes), self.rfile)
                    elif verb == 'POST' and path == '/api/v1/jobs':
                        result = service._submit(self._body())
                    elif verb == 'POST' and path == '/api/v1/assets':
                        result = service._register_asset(self._body())
                    elif verb == 'POST' and path == '/api/v1/rpc':
                        result = service._rpc(self._body())
                    elif verb == 'GET' and path.startswith('/api/v1/artifacts/'):
                        identifier = path[len('/api/v1/artifacts/'):]
                        descriptor = identifier.endswith('/metadata')
                        item, target = service._artifact(identifier[:-9] if descriptor else identifier)
                        if descriptor:
                            result = item
                        else:
                            with target.open('rb') as stream:
                                self.send_response(200)
                                self.send_header('Content-Type', 'application/octet-stream')
                                self.send_header('Content-Length', str(item['bytes']))
                                self.end_headers()
                                import shutil
                                shutil.copyfileobj(stream, self.wfile, 1024*1024)
                            return
                    else:
                        raise ServiceError('route_not_found', 404)
                    self._respond({'api_version': 1, 'ok': True, 'result': _public(result)})
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass
                except Exception as exc:
                    if isinstance(exc, ServiceError):
                        code, status = exc.code, exc.status
                    elif isinstance(exc, ValueError):
                        raw = str(exc).split(':', 1)[0]
                        code = getattr(exc, 'code', raw if re.fullmatch(r'[a-z][a-z0-9_]{1,80}', raw) else 'invalid_request')
                        status = 409 if code == 'request_id_conflict' else 404 if code.endswith('_not_found') or code == 'spec_blob_unavailable' else 503 if code == 'scheduler_stopping' else 400
                    else:
                        code, status = 'internal_service_error', 500
                    try:
                        self._respond({'api_version': 1, 'ok': False, 'error': {'code': code, 'message': code}}, status)
                    except (OSError, ServiceError):
                        pass

            def do_GET(self): self._handle('GET')
            def do_POST(self): self._handle('POST')
            def do_PUT(self): self._handle('PUT')
        return Handler

    def run(self):
        thread = None
        try:
            if self.allow_insecure_http:
                logging.warning('WARNING: HTTP is enabled without TLS; Bearer credentials and request data are unencrypted. Use only on a trusted network or through an existing encrypted tunnel.')
            self.http = _BoundedHTTPServer((self.host, self.port), self.handler(),
                                           limit=self.max_connections, tls_context=self.tls_context)
            scheme = 'https' if self.tls_context else 'http'
            write_json(self.scheduler.runtime / 'remote-endpoint.json',
                       {'api_version': 1, 'base_url': f'{scheme}://{self.host}:{self.http.server_port}',
                        'pid': os.getpid(), 'instance': self.scheduler.owner})
            thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
            thread.start()
            while not self.scheduler.stopping.wait(.1):
                self.scheduler.tick()
        finally:
            self.scheduler.stopping.set()
            if self.http is not None and thread is not None:
                self.http.shutdown()
                self.http.close_requests()
                self.http.server_close()
                thread.join(timeout=2)
            if self.scheduler.active:
                job_id, process, _, _ = self.scheduler.active
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
                with self.scheduler.condition:
                    current = self.scheduler.memory_status(job_id)
                    self.scheduler._commit('transition', job_id, **_interrupted_job_changes(current))
                if current.get('cancel_requested'):
                    self.scheduler._cleanup_cancel_marker(job_id)
            self.scheduler.guard.close()
            self.scheduler.lock.close()
            (self.scheduler.runtime / 'remote-endpoint.json').unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Headless remote compute service API v1')
    parser.add_argument('--config')
    parser.add_argument('--data-root')
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--tls-cert')
    parser.add_argument('--tls-key')
    parser.add_argument('--token-env')
    parser.add_argument('--insecure-loopback', action='store_true')
    parser.add_argument('--allow-insecure-http', action='store_true', default=None,
                        help='explicitly allow authenticated plaintext HTTP (trusted networks or encrypted tunnels only)')
    parser.add_argument('--allow-probe', action='store_true', help='loopback integration tests only')
    args = parser.parse_args(argv)
    configuration = {}
    if args.config:
        configuration = _json(Path(args.config).read_bytes())
        allowed = {'data_root', 'host', 'port', 'tls_cert', 'tls_key', 'token_env',
                   'allow_insecure_http', 'max_connections', 'max_blob_bytes', 'blob_quota_bytes'}
        if not isinstance(configuration, dict) or set(configuration) - allowed:
            parser.error('invalid service configuration fields')
    for key in ('data_root', 'host', 'port', 'tls_cert', 'tls_key', 'token_env'):
        value = getattr(args, key)
        if value is not None:
            configuration[key] = value
    if args.allow_insecure_http is not None:
        configuration['allow_insecure_http'] = args.allow_insecure_http
    if not configuration.get('data_root'):
        parser.error('--data-root is required')
    token_env = configuration.pop('token_env', None)
    if token_env is not None:
        if not isinstance(token_env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', token_env):
            parser.error('invalid token environment variable name')
        # Consume the one-time process environment handoff before any probe or
        # worker child is launched.  The long-lived service keeps it in memory.
        configuration['token'] = os.environ.pop(token_env, None)
        if configuration['token'] is None:
            parser.error('configured token environment variable is absent')
    from coat_sim.project_logging import install_process_log
    from coat_sim.assistant_diagnostics import trace
    install_process_log('scheduler', configuration['data_root'])
    trace('remote_service_start', role='remote-service')
    service = RemoteComputeService(**configuration, insecure_loopback=args.insecure_loopback, allow_probe=args.allow_probe)
    def stop(*_): service.scheduler.stopping.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, stop)
    service.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
