"""Isolated executor registry: compute locally, publish file references only."""
from dataclasses import asdict
import json
import os
import sys
from pathlib import Path
from typing import Any
import tempfile
import time
import traceback
from copy import deepcopy
from .io import write_json
from .jobs import JobSpec, canonical

class FileCancelEvent:
    def __init__(self, root, job_id, owner_pid, owner_instance):
        self.marker = root / 'artifacts/runtime/jobs' / (job_id + '.cancel')
        self.owner_pid, self.owner_instance = owner_pid, owner_instance
        from coat_sim.core.process_identity import _process_identity
        self.owner_start = _process_identity(owner_pid)[1]
        self.last_check, self.cancelled = 0, False
    def is_set(self):
        if self.cancelled: return True
        now = time.monotonic()
        if now - self.last_check >= .1:
            from coat_sim.core.process_identity import _process_identity
            state, start = _process_identity(self.owner_pid)
            self.cancelled = self.marker.exists() or state == 'dead' or (self.owner_start and start != self.owner_start)
            self.last_check = now
        return self.cancelled

class ResultSaveError(RuntimeError): pass

def load_result(reply, client=None):
    """Array loading is explicit and local; never part of a status RPC."""
    import numpy as np
    artifact = next((item for item in reply.get('result_artifacts', [])
                     if item.get('artifact_id') == reply.get('artifact_id')), None)
    path = client.materialize_artifact(reply['artifact_id'], artifact) if client else reply['result_file']
    with np.load(path, allow_pickle=False) as saved:
        metadata = json.loads(str(saved['metadata_json'].item()))
        result = {key: saved[key] for key in ('field', 'field_nm', 'x', 'y') if key in saved}
    result.update(metadata.get('result_metadata', {}))
    result['_asset'] = reply.get('asset')
    result['_configuration'] = metadata
    return result

def _materialize(service, spec, directory):
    parameters = deepcopy(spec.parameters)
    files = spec.inputs.get('files', [])
    inputs = directory / 'inputs'
    inputs.mkdir(exist_ok=True)
    names = {}
    for record in files:
        name = record['path']
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('invalid_frozen_dependency_path')
        target = inputs / name
        if 'data' in record:
            import hashlib
            if hashlib.sha256(canonical(record['data']).encode('utf-8')).hexdigest() != record['sha256']:
                raise ValueError('frozen_dependency_digest_mismatch')
            write_json(target, record['data'])
        else:
            import base64
            import hashlib
            content = base64.b64decode(record['data_base64'], validate=True)
            if hashlib.sha256(content).hexdigest() != record['sha256']:
                raise ValueError('frozen_dependency_digest_mismatch')
            target.write_bytes(content)
        names[name] = str(target)
    for key in ('machine_path', 'substrate_path', 'source_path'):
        if parameters.get(key): parameters[key] = names[parameters[key]]
    if parameters.get('mask_paths'): parameters['mask_paths'] = [names[v] for v in parameters['mask_paths']]
    return parameters

def _probe(parameters, directory, cancel, report, job_id):
    duration = float(parameters.get('delay_s', 0))
    if duration < 0 or not __import__('math').isfinite(duration): raise ValueError('invalid_delay')
    start = time.monotonic()
    while time.monotonic() - start < duration:
        if cancel.is_set(): raise InterruptedError('cancelled')
        report({'fraction': min(.98, (time.monotonic()-start) / max(duration, .001)), 'stage': 'probe'})
        time.sleep(.02)
    if cancel.is_set(): raise InterruptedError('cancelled')
    if parameters.get('crash'): os._exit(73)
    if parameters.get('fail_save'): raise ResultSaveError('injected result save failure')
    count = int(parameters.get('payload_bytes', 0))
    if count < 0: raise ValueError('invalid_payload_bytes')
    path = directory / 'probe-result.bin'
    try:
        with path.open('xb') as stream:
            remaining = count
            block = b'x' * min(1024*1024, count)
            while remaining:
                if cancel.is_set(): raise InterruptedError('cancelled')
                chunk = block[:min(len(block), remaining)]
                stream.write(chunk); remaining -= len(chunk)
            stream.flush(); os.fsync(stream.fileno())
    except OSError as exc: raise ResultSaveError(str(exc)) from exc
    return {'result_file': str(path), 'payload_bytes': count, 'run_id': job_id}

def _simulation(service, spec, parameters, cancel, report, job_id, started,
                checkpoint_path=None):
    if spec.executor not in ('process', 'process_recipe'):
        return _simulation_impl(service, spec, parameters, cancel, report, job_id,
                                started, checkpoint_path=checkpoint_path)
    parameters.setdefault('include_layer_fields', True)
    if parameters['include_layer_fields'] is not True:
        return _simulation_impl(service, spec, parameters, cancel, report, job_id,
                                started, checkpoint_path=checkpoint_path)
    import hashlib, re
    run_id = job_id if re.fullmatch(r'[0-9a-f]{32}', job_id) else hashlib.sha256(
        job_id.encode('utf-8')).hexdigest()[:32]
    capture_root = Path(service.root) / 'data' / 'tmp' / 'simulations' / run_id
    capture_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.layer-capture-', dir=capture_root) as capture_dir:
        parameters['layer_field_spool_path'] = str(Path(capture_dir) / 'capture.npz')
        return _simulation_impl(service, spec, parameters, cancel, report, job_id,
                                started, checkpoint_path=checkpoint_path)


def _work_index_arguments(parameters, kind, snapshot, work_index):
    """Adapt job parameters to the public workload-estimator signature."""
    args = dict(parameters)
    if kind == 'mask_optimization':
        args = {'process_path': snapshot,
                'fast_cycle': parameters.get('options', {}).get('fast_cycle', True)}
    import inspect
    accepted = {
        name for name, parameter in inspect.signature(work_index).parameters.items()
        if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                              inspect.Parameter.KEYWORD_ONLY)
        and name not in {'self', 'settings'}
    }
    return {key: value for key, value in args.items() if key in accepted}


def _invoke_work_index(service, settings, parameters, kind, snapshot):
    work_index = service.work_index
    return work_index(
        settings, **_work_index_arguments(parameters, kind, snapshot, work_index))


def _simulation_impl(service, spec, parameters, cancel, report, job_id, started,
                     checkpoint_path=None):
    from coat_sim.core.result_summary import algorithm_result_metadata
    from coat_sim.core.simulation_assets import plain
    kind = spec.executor
    import hashlib, re
    run_id = job_id if re.fullmatch(r'[0-9a-f]{32}', job_id) else hashlib.sha256(job_id.encode('utf-8')).hexdigest()[:32]
    settings = parameters.pop('settings')
    optimized = None
    if kind in ('process', 'process_recipe'):
        result = service.simulate_recipe(settings, cancel_event=cancel, progress=report, **parameters)
        document = service.load('process', parameters['process_path'])
        machine, process, snapshot = document.runtime['machine'], document.data, spec.inputs['snapshot']
        backend = parameters.get('backend', '自动')
    elif kind == 'mask_optimization':
        optimized = service.optimize_masks(settings, parameters['process_path'], options=parameters.get('options'),
                    cancel_event=cancel, progress_callback=report, pending_only=True,
                    checkpoint_path=checkpoint_path)
        result = optimized['simulation']
        machine, process, snapshot = optimized['machine'], optimized['process'], optimized['input_snapshot']
        backend = (parameters.get('options') or {}).get('backend', '自动')
    else:
        result = service.simulate_single(settings, cancel_event=cancel, **parameters)
        machine = service.load('machine', parameters['machine_path']).data if parameters.get('machine_path') else None
        process, snapshot, backend = None, None, parameters.get('backend', '自动')
    if cancel.is_set(): raise InterruptedError('cancelled')
    elapsed = time.monotonic() - started
    report({'stage': 'materialize', 'fraction': .98})
    try:
        asset = service.prepare_simulation_asset(result=result, settings=settings, machine=machine,
                    process=process, elapsed_s=elapsed, requested_device=backend, run_id=run_id,
                    input_snapshot=snapshot)
    except Exception as exc:
        raise ResultSaveError(f'Calculation finished but result persistence failed: {type(exc).__name__}: {exc}') from exc
    field_name = 'field_nm' if 'field_nm' in result else 'field'
    reply = {'run_id': run_id, 'result_file': asset['result_file'], 'field_name': field_name,
             'field_unit': 'nm' if field_name == 'field_nm' else 'relative',
             'field_shape': list(result[field_name].shape), 'elapsed_s': elapsed,
             'metadata': plain(algorithm_result_metadata(result)), 'files': asset['files'], 'asset': asset,
             'temporary_asset': {'run_id': job_id, 'directory': asset['directory'],
                                 'save_requires_user_confirmation': True}}
    if optimized:
        reply.update(optimization_report={k: plain(v) for k,v in optimized['report'].items()
                    if k not in ('response_scan','options','simulation_settings','group_reports','search_best_masks','proposed_masks','verification_candidates')},
                     report_path=optimized['report_path'], artifacts=optimized['artifacts'], pending_masks=optimized['pending_masks'])
    try:
        index = _invoke_work_index(service, settings, parameters, kind, snapshot)
        service.record_success(requested_device=backend, result=result, work_index=index, elapsed_s=elapsed)
        reply['work_index'] = index
    except Exception as exc: reply['history_warning'] = str(exc)
    return reply

def _film_progress_payload(value):
    """Convert typed film progress to the scheduler's scalar JSON contract."""
    from coat_sim.core.film_optimization import FilmOptimizationProgress

    if not isinstance(value, FilmOptimizationProgress):
        return value
    return {
        'stage': 'film_optimization',
        'fraction': value.fraction,
        'evaluations': value.evaluations,
        'best_score': value.best_max_normalized_error,
        'step_index': value.iteration,
        'status': value.message or value.state,
    }


def _film_optimization(service, parameters, cancel, report, checkpoint_path):
    """Execute a typed film job and persist only replay-verified incumbents."""
    from coat_sim.core.film_optimization_job import execute_film_optimization_job
    from .result_capability import (
        film_optimization_service, validate_film_optimization_result,
    )

    payload = parameters.get('payload')
    if not isinstance(payload, dict) or set(parameters) != {'payload'}:
        raise ValueError('film_optimization requires one frozen job payload')
    film_service = film_optimization_service(service)

    latest_progress: list[dict[str, Any]] = [{}]

    def publish_progress(value):
        progress = _film_progress_payload(value)
        if isinstance(progress, dict):
            latest_progress[0] = dict(progress)
        latest = latest_performance[0] if performance_enabled else None
        if isinstance(progress, dict) and isinstance(latest, dict):
            for key in (
                    'actual_backend', 'dtype', 'cpu_workers', 'capacity', 'gpu_runtime', 'device', 'device_uuid', 'output_device', 'requested_backend',
                    'forward_calls', 'cached_forward_calls', 'cache_hits',
                    'prepared_builds', 'cache_evictions', 'fallback_calls',
                    'failed_forward_calls', 'cancelled_forward_calls',
                    'forward_seconds', 'prepare_seconds'):
                if key in latest:
                    progress[key] = latest[key]
        report(progress)

    def save_candidate(candidate):
        # Validate before replacing the last good checkpoint. A bad/new candidate
        # or disk error must never erase an already viewable incumbent.
        try:
            verified = validate_film_optimization_result(film_service, candidate)
            write_json(checkpoint_path, {'result': verified})
        except Exception as exc:
            print(f'film_candidate_checkpoint_warning: {exc}', flush=True)

    forward_cache_enabled = os.environ.get('COAT_SIM_FILM_FORWARD_CACHE') == '1'
    batch_enabled = payload.get('request', {}).get('execution_mode') == 'deferred_batch'
    performance_enabled = forward_cache_enabled or batch_enabled
    performance_options = None
    if batch_enabled and payload.get('request', {}).get('execution_device') is None:
        from coat_sim.core.film_performance import PerformanceOptions
        performance_options = PerformanceOptions(
            backend=os.environ.get('COAT_SIM_FILM_BACKEND', 'auto'),
            cpu_workers=int(os.environ.get('COAT_SIM_FILM_CPU_WORKERS', '1')))
    performance_path = checkpoint_path.with_name('film-performance.json')
    latest_performance = [None]
    last_performance_write = [0.]

    def persist_performance(*, force=False):
        snapshot = latest_performance[0]
        if not performance_enabled or not isinstance(snapshot, dict):
            return
        now = time.monotonic()
        if not force and now - last_performance_write[0] < .5:
            return
        try:
            write_json(performance_path, snapshot)
            last_performance_write[0] = now
        except Exception as exc:
            print(f'film_performance_persistence_warning: {exc}', flush=True)

    def publish_performance(value, *, force_report=False):
        if not isinstance(value, dict):
            return
        latest_performance[0] = deepcopy(value)
        progress: dict[str, Any] = {key: latest_progress[0][key] for key in
                    ('fraction','step_index','evaluations','best_score') if key in latest_progress[0]}
        progress.update({
            'stage': 'film_batch' if batch_enabled else 'film_forward_cache',
            'status': f"forward cache {value.get('actual_backend', 'running')}",
        })
        for key in (
                'actual_backend', 'dtype', 'cpu_workers', 'capacity', 'gpu_runtime', 'device', 'device_uuid', 'output_device', 'requested_backend', 'forward_calls',
                'cached_forward_calls', 'cache_hits', 'prepared_builds',
                'cache_evictions', 'fallback_calls', 'failed_forward_calls',
                'cancelled_forward_calls', 'forward_seconds', 'prepare_seconds'):
            if key in value:
                progress[key] = value[key]
        try:
            report(progress, force=force_report)
        except Exception as exc:
            print(f'film_performance_progress_warning: {exc}', flush=True)
        persist_performance()

    try:
        result = execute_film_optimization_job(
            film_service, payload, cancel_event=cancel, progress_callback=publish_progress,
            checkpoint_callback=save_candidate,
            enable_forward_cache=forward_cache_enabled,
            performance_callback=(publish_performance if performance_enabled else None),
            performance_options=performance_options,
        )
    finally:
        persist_performance(force=True)
        if performance_enabled and isinstance(latest_performance[0], dict):
            publish_performance(latest_performance[0], force_report=True)
    if payload.get('request', {}).get('execution_device') is not None and result.get('state') == 'failed':
        raise RuntimeError('显式设备批量前向失败；未回退 CPU；已保留最后完整候选供查看。')
    return validate_film_optimization_result(film_service, result)

def _geometric_raytrace(root, spec, directory, cancel, report):
    """Run a JSON-only ray request and persist the full ray result as an artifact."""
    from coat_sim.core.geometric_raytrace import RESULT_SCHEMA, request_limits, trace
    from coat_sim.core.geometric_raytrace_result import validate_geometric_raytrace_result
    from coat_sim.core.ray_trace_job import load_layer_fields_from_artifact

    if cancel.is_set():
        raise InterruptedError('cancelled')
    request = spec.parameters.get('request')
    if not isinstance(request, dict):
        raise ValueError('geometric_raytrace requires a request object')
    limits = request_limits(request)
    reference = spec.inputs.get('deposition_artifact')
    fields = (load_layer_fields_from_artifact(
        root, reference, cancel_event=cancel,
        max_asset_bytes=limits['max_asset_bytes']) if reference is not None else None)
    if cancel.is_set():
        raise InterruptedError('cancelled')

    def progress_update(value):
        if not isinstance(value, dict):
            return
        report({'fraction': value.get('completed_fraction', 0.0),
                'stage': value.get('stage', 'tracing')})

    result = trace(request, layer_fields=fields, cancel_event=cancel,
                   progress=progress_update)
    if cancel.is_set():
        raise InterruptedError('cancelled')
    validate_geometric_raytrace_result(result)
    if cancel.is_set():
        raise InterruptedError('cancelled')
    path = directory / 'raytrace-result.json'
    try:
        write_json(path, result, max_bytes=limits['max_result_bytes'])
    except Exception as exc:
        raise ResultSaveError(f'Ray trace completed but result persistence failed: {exc}') from exc
    detector_spectra = result.get('detector', {}).get('spectra', [])
    return {
        'kind': 'geometric_raytrace',
        'result_file': str(path),
        'result_schema': RESULT_SCHEMA,
        'path_count': len(result.get('paths', [])),
        'spectrum_count': len(result.get('spectra', [])),
        'spectra': result.get('spectra', []),
        'detector_spectra': detector_spectra,
    }

def run(root, job_id, owner_pid, owner_instance):
    root = Path(root).resolve()
    directory = root / 'artifacts/runtime/scheduler/jobs' / job_id
    started = time.monotonic()
    cancel = FileCancelEvent(root, job_id, owner_pid, owner_instance)
    # No algorithm starts before its PID and creation identity are durably leased.
    while not (directory / 'launch.json').is_file():
        if cancel.is_set(): return 75
        time.sleep(.02)
    if json.loads((directory / 'launch.json').read_text(encoding='utf-8')).get('instance') != owner_instance:
        return 75
    last_report = [0., 0.]
    def report(value, *, force=False):
        from coat_sim.core.compute_tasks import _compact_progress
        value = _compact_progress(value) or {}
        now = time.monotonic()
        if now - last_report[0] < .25 and not force: return
        if 'fraction' in value:
            value['fraction'] = max(last_report[1], min(.98, value['fraction']))
            last_report[1] = value['fraction']
        last_report[0] = now
        try: write_json(directory / 'progress.json', value)
        except OSError as exc: print(f'progress_persistence_warning: {exc}', flush=True)
    try:
        spec = None
        queue_kind = None
        from .store import SchedulerStore
        queue_record = SchedulerStore(root).status(job_id)
        queue_kind = queue_record.get('kind')
        spec_path = directory / 'spec.json'
        if queue_kind == 'appearance_render':
            from coat_sim.appearance.limits import MAX_APPEARANCE_SPEC_BYTES
            if spec_path.stat().st_size > MAX_APPEARANCE_SPEC_BYTES:
                raise ValueError('appearance_frozen_spec_exceeds_60_mb')
        frozen = JobSpec.parse_frozen_json(spec_path.read_text(encoding='utf-8'))
        spec = frozen.to_spec()
        if queue_record.get('input_digest') != frozen.fingerprint:
            raise ValueError('frozen_input_digest_mismatch')
        if queue_kind != spec.executor:
            raise ValueError('queue_executor_mismatch')
        if spec.executor == 'probe':
            reply = _probe(spec.parameters, directory, cancel, report, job_id)
        elif spec.executor == 'appearance_render':
            from coat_sim.core.appearance_execution import execute_appearance
            reply = execute_appearance(root, job_id, spec.parameters, cancel, report)
        else:
            from coat_sim.core.api import CoatService
            from coat_sim.core import isolated_compute
            isolated_compute._in_scheduler_worker = True
            from coat_sim.core import data_files
            data_files.PROJECT_ROOT, data_files.DATA_ROOT = root, root / "data"
            service = CoatService(root)
            parameters = _materialize(service, spec, directory)
            if spec.executor == 'film_optimization':
                reply = _film_optimization(
                    service, parameters, cancel, report,
                    directory / 'best-candidate.json')
            elif spec.executor == 'geometric_raytrace':
                reply = _geometric_raytrace(root, spec, directory, cancel, report)
            elif spec.executor in ('process', 'process_recipe', 'single_source', 'mask_optimization'):
                reply = _simulation(service, spec, parameters, cancel, report, job_id, started,
                                    checkpoint_path=directory / 'best-candidate.json')
            elif spec.executor in ('mask_weights', 'mask_field', 'point_weights'):
                action = {'mask_weights': service.mask_weights, 'mask_field': service.mask_field, 'point_weights': service.point_weights}[spec.executor]
                settings = parameters.pop('settings'); parameters.pop('mask_paths', None)
                result = action(settings, **parameters)
                from coat_sim.core.simulation_assets import plain
                path = directory / 'analysis-result.json'
                try: write_json(path, plain(result))
                except Exception as exc: raise ResultSaveError(str(exc)) from exc
                reply = {'kind': spec.executor, 'result_file': str(path), 'summary': {k:v for k,v in plain(result).items() if not isinstance(v, (dict,list))}}
            elif spec.executor == 'python_script':
                # Preserve the existing LPAC sandbox contract; never run a script in this worker.
                import asyncio
                from coat_sim.agent.sandbox import run_python_script
                staged = root / 'data/tmp/scheduler-inputs' / job_id
                staged.mkdir(parents=True, exist_ok=True)
                import shutil
                references = []
                for file in (directory / 'inputs').iterdir():
                    target = staged / file.name; shutil.copyfile(file, target)
                    references.append(str(target.relative_to(root / 'data')))
                async def sandbox_run():
                    task = asyncio.create_task(run_python_script(parameters['code'], references, parameters.get('timeout_s', 60)))
                    while not task.done():
                        if cancel.is_set():
                            task.cancel()
                            try: await task
                            except asyncio.CancelledError: pass
                            raise InterruptedError('sandbox task cancelled')
                        await asyncio.sleep(.2)
                    return await task
                reply = asyncio.run(sandbox_run())
            else: raise ValueError('executor_not_registered')
        if cancel.is_set(): raise InterruptedError('cancelled')
        outcome = {'state': 'succeeded', 'result': reply, 'error': None,
                   'progress': {'fraction': 1., 'stage': 'complete'}}
    except BaseException as exc:
        traceback.print_exc()
        partial_result, unavailable_reason = None, ''
        if queue_kind != 'appearance_render' and (spec is None or spec.executor != 'appearance_render'):
            try:
                from .result_capability import load_best_candidate_checkpoint
                partial_result, unavailable_reason = load_best_candidate_checkpoint(root, job_id)
            except Exception as checkpoint_error:
                unavailable_reason = f'Best-candidate checkpoint validation failed: {checkpoint_error}'
        outcome = {'state': 'cancelled' if isinstance(exc, InterruptedError) else 'failed',
                   'result': partial_result,
                   'error_code': 'cancelled' if isinstance(exc, InterruptedError) else 'result_save_failed' if isinstance(exc, ResultSaveError) else 'algorithm_failed',
                   'error': str(exc), 'diagnostic_file': str(getattr(sys.stderr, 'path', Path(root) / 'artifacts/logs/compute-worker.log')),
                   'elapsed_s': time.monotonic() - started}
        if partial_result is None:
            outcome['result_unavailable_reason'] = unavailable_reason
    try: write_json(directory / 'outcome.json', outcome)
    except Exception:
        traceback.print_exc(); return 74
    return 0
