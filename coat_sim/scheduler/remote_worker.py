"""Remote worker bootstrap: retain the worker protocol, select the film service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _check_bound_worker(root, job_id):
    """Fail closed before a bound job can enter any algorithm code."""
    from .jobs import JobSpec
    from coat_sim.appearance.limits import MAX_APPEARANCE_SPEC_BYTES

    path = Path(root).resolve(strict=True) / 'artifacts/runtime/scheduler/jobs' / job_id / 'spec.json'
    if path.stat().st_size > MAX_APPEARANCE_SPEC_BYTES:
        raise ValueError('remote_worker_spec_too_large')
    spec = JobSpec.parse_frozen_json(path.read_text(encoding='utf-8')).to_spec()
    binding = spec.inputs.get('device_binding')
    if binding is None:
        return spec
    if binding not in ({'schema_version': 1, 'device_id': 'cpu:default'},
                       {'schema_version': 1, 'device_id': 'cuda:default'}):
        raise ValueError('remote_device_binding_unavailable')
    device_id = binding['device_id']
    if spec.executor == 'appearance_render':
        from coat_sim.core.appearance_result import _backend_precision
        envelope = spec.parameters.get('frozen')
        backend_precision = (_backend_precision(envelope.get('request'))
                             if isinstance(envelope, dict) else None)
        expected = ({('cpu', 'native')} if device_id == 'cpu:default' else
                    {('gpu_cuda', 'mixed'), ('gpu_cuda', 'float32')})
        if backend_precision not in expected:
            raise ValueError('remote_appearance_device_mismatch')
    elif spec.executor == 'process_recipe':
        if device_id != 'cpu:default' or spec.parameters.get('backend') != 'CPU':
            raise ValueError('remote_process_device_mismatch')
    elif spec.executor == 'film_optimization':
        if device_id != 'cpu:default':
            raise ValueError('remote_film_device_mismatch')
        os.environ['COAT_SIM_FILM_BACKEND'] = 'cpu'
    elif spec.executor == 'film_synthesis':
        if device_id != 'cpu:default':
            raise ValueError('remote_film_synthesis_device_mismatch')
    else:
        raise ValueError('remote_device_executor_unsupported')
    return spec


def _attest_completed_device(root, job_id, spec) -> None:
    """Record an independently observed outcome, never a copy of the request."""
    binding = spec.inputs.get('device_binding')
    if binding not in ({'schema_version': 1, 'device_id': 'cpu:default'},
                       {'schema_version': 1, 'device_id': 'cuda:default'}):
        return
    from coat_sim.scheduler.io import write_json

    root = Path(root).resolve(strict=True)
    directory = root / 'artifacts/runtime/scheduler/jobs' / job_id
    outcome_path = directory / 'outcome.json'
    if not outcome_path.is_file() or outcome_path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('remote_worker_outcome_invalid')
    outcome = json.loads(outcome_path.read_text(encoding='utf-8'))
    if not isinstance(outcome, dict) or outcome.get('state') != 'succeeded':
        return
    result = outcome.get('result')
    actual = None
    if spec.executor == 'appearance_render':
        from coat_sim.core.appearance_result import validate_appearance_render_result

        validate_appearance_render_result(root, job_id, result,
                                          expected_input_digest=spec.fingerprint)
        manifest_path = root / 'data/tmp/appearance' / job_id / 'render/manifest.json'
        if manifest_path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('remote_appearance_manifest_invalid')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if (result.get('backend'), result.get('precision')) == ('cpu', 'native'):
            if (binding['device_id'] != 'cpu:default'
                    or manifest.get('execution', {}).get('backend') != 'cpu_scalar'):
                raise ValueError('remote_appearance_cpu_not_attested')
            actual = 'cpu:default'
        elif (result.get('backend') == 'gpu_cuda'
              and result.get('precision') in ('mixed', 'float32')):
            if (binding['device_id'] != 'cuda:default'
                    or result.get('render_status') != 'rendered_unqualified'
                    or result.get('diagnostics_pass') is not True
                    or manifest.get('status') != 'rendered_unqualified'
                    or manifest.get('precision') != result['precision']
                    or manifest.get('execution', {}).get('variant') != 'cuda_ad_rgb'
                    or manifest.get('runtime', {}).get('variant') != 'cuda_ad_rgb'):
                raise ValueError('remote_appearance_cuda_not_attested')
            actual = 'cuda:default'
        else:
            raise ValueError('remote_appearance_execution_not_attested')
    elif spec.executor == 'process_recipe' and isinstance(result, dict):
        asset = result.get('asset')
        summary = asset.get('summary') if isinstance(asset, dict) else None
        metadata = result.get('metadata')
        reported = summary.get('actual_device') if isinstance(summary, dict) else None
        scalar = metadata.get('device') if isinstance(metadata, dict) else None
        if (isinstance(reported, str) and reported == scalar
                and all(part.strip() == 'CPU' for part in reported.split('、'))):
            actual = 'cpu:default'
    elif spec.executor == 'film_optimization':
        # The typed film result does not currently include an actual backend.
        # A requested CPU flag is not sufficient evidence of execution.
        actual = None
    if actual is not None:
        outcome['executed_device_id'] = actual
        write_json(outcome_path, outcome, max_bytes=64 * 1024 * 1024)


def run(root, job_id, owner_pid, owner_instance):
    from . import worker
    spec = _check_bound_worker(root, job_id)
    original_film = worker._film_optimization
    def film_executor(service, parameters, cancel, report, checkpoint_path, *,
                      candidate_observer_factory=None, job_id=None, input_digest=None):
        from coat_sim.core.film_design_service import FilmDesignService
        return original_film(FilmDesignService(service.root), parameters, cancel, report, checkpoint_path,
                             candidate_observer_factory=candidate_observer_factory,
                             job_id=job_id, input_digest=input_digest)
    worker._film_optimization = film_executor
    try:
        code = worker.run(root, job_id, owner_pid, owner_instance)
        if code == 0:
            _attest_completed_device(root, job_id, spec)
        return code
    finally:
        worker._film_optimization = original_film


def main(argv=None):
    parser = argparse.ArgumentParser(description='Internal remote compute worker')
    parser.add_argument('root')
    parser.add_argument('job_id')
    parser.add_argument('owner_pid', type=int)
    parser.add_argument('owner_instance')
    args = parser.parse_args(argv)
    from coat_sim.project_logging import install_process_log
    from coat_sim.assistant_diagnostics import trace
    install_process_log('remote-worker', args.root)
    trace('remote_worker_start', role='remote-worker')
    return run(args.root, args.job_id, args.owner_pid, args.owner_instance)


if __name__ == '__main__':
    raise SystemExit(main())
