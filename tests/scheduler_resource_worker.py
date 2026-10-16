"""Bounded test-only worker for scheduler resource failure regressions.

This module accepts no executable payload or target override. It only allows
the two fixed failures below, after validating the immutable recipe spec.
"""
import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path


_BOUNDED_FAILURE = 'bounded resource fixture requires process_recipe, grid_size=21, time_steps=12, backend=CPU, algorithm=fast'


def _bounded_settings_values(settings):
    if isinstance(settings, Mapping):
        grid_size = settings.get('grid_size')
        time_steps = settings.get('time_steps')
    else:
        grid_size = getattr(settings, 'grid_size', None)
        time_steps = getattr(settings, 'time_steps', None)
    if (type(grid_size) is not int or grid_size != 21
            or type(time_steps) is not int or time_steps != 12):
        raise ValueError(_BOUNDED_FAILURE)
    return grid_size, time_steps


def validate_bounded_spec(spec):
    """Require the single small CPU recipe accepted by this fault fixture."""
    executor = getattr(spec, 'executor', None)
    parameters = getattr(spec, 'parameters', None)
    if executor != 'process_recipe' or not isinstance(parameters, dict):
        raise ValueError(_BOUNDED_FAILURE)
    settings = parameters.get('settings')
    if not isinstance(settings, dict):
        raise ValueError(_BOUNDED_FAILURE)
    _bounded_settings_values(settings)
    if parameters.get('backend') != 'CPU' or type(parameters.get('backend')) is not str:
        raise ValueError(_BOUNDED_FAILURE)
    if parameters.get('algorithm') != 'fast' or type(parameters.get('algorithm')) is not str:
        raise ValueError(_BOUNDED_FAILURE)
    return dict(settings)


def _validated_child_spec(root, job_id):
    from coat_sim.scheduler.jobs import JobSpec

    if not job_id or job_id in ('.', '..') or Path(job_id).name != job_id:
        raise ValueError(_BOUNDED_FAILURE)
    path = (root / 'artifacts/runtime/scheduler/jobs' / job_id / 'spec.json')
    frozen = JobSpec.parse_frozen_json(path.read_text(encoding='utf-8'))
    spec = frozen.to_spec()
    settings = validate_bounded_spec(spec)
    return spec, settings


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--failure', choices=('memory', 'crash'), required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('command', choices=('worker',))
    parser.add_argument('job_id')
    parser.add_argument('owner_pid', type=int)
    parser.add_argument('owner_instance')
    args = parser.parse_args(argv)

    root = args.root.resolve()
    _, frozen_settings = _validated_child_spec(root, args.job_id)
    directory = root / 'artifacts/runtime/scheduler/jobs' / args.job_id
    from coat_sim.scheduler.worker import run

    def fixed_fault(settings, runtime, *, algorithm, options, backend,
                    cancel_event=None, fast_cycle=False,
                    fast_cycle_residual_percent=5., progress=None,
                    capture_layer_fields=False, layer_field_spool_path=None):
        grid_size, time_steps = _bounded_settings_values(settings)
        if backend != 'CPU' or algorithm != 'fast':
            raise ValueError(_BOUNDED_FAILURE)
        record = {
            'failure': args.failure,
            'job_id': args.job_id,
            'pid': os.getpid(),
            'parent_pid': os.getppid(),
            'grid_size': grid_size,
            'time_steps': time_steps,
            'backend': backend,
            'algorithm': algorithm,
            'algorithm_entered': False,
            'allocation_boundary': 'before run_recipe',
        }
        if args.failure == 'memory':
            record['exception_type'] = 'MemoryError'
        else:
            record['exit_code'] = 37
        (directory / 'resource-failure-fixture.json').write_text(
            json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n',
            encoding='utf-8')
        if args.failure == 'memory':
            raise MemoryError('bounded scheduler allocation failure')
        os._exit(37)

    # Guard against drift between the child-side frozen spec and the settings
    # that reach the callable, before any simulation implementation can run.
    if _bounded_settings_values(frozen_settings) != (21, 12):
        raise ValueError(_BOUNDED_FAILURE)
    from unittest.mock import patch
    with patch('coat_sim.core.algorithm_paths.run_recipe', side_effect=fixed_fault):
        return run(root, args.job_id, args.owner_pid, args.owner_instance)


if __name__ == '__main__':
    raise SystemExit(main())
