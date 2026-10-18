"""Run the product's deposition paths without a desktop: python -m coat_sim.algorithm_cli."""
import argparse
import json
from pathlib import Path
import signal
import shutil
import threading
import time
from uuid import uuid4
import numpy as np
from .core.api import CoatService
from .core.atomic_io import replace_with_retry
from .core.simulation_assets import plain, prepare_simulation_asset, save_simulation_asset


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', help='Explicit project root; recipe paths stay confined to this root')
    parser.add_argument('--process', required=True, help='Existing v6/v7 recipe in project root')
    parser.add_argument('--algorithm', choices=['fast', 'reference', 'optimize'], default='fast')
    parser.add_argument('--settings', help='JSON Settings file; omitted fields use product defaults')
    parser.add_argument('--grid-size', type=int, default=31)
    parser.add_argument('--time-steps', type=int, help='Time sample count; overrides settings count; default 120 without settings')
    parser.add_argument('--backend', default='CPU')
    parser.add_argument('--fast-cycle', action='store_true')
    parser.add_argument('--budget-s', type=float,
                        help='Optional wall-time stop limit in seconds (0 < value <= 300); omitted means no time limit')
    parser.add_argument('--max-evaluations', type=int)
    parser.add_argument('--tolerance-percent', type=float, default=.1)
    parser.add_argument('--rpm-scale-bounds', nargs=2, type=float, default=[.8, 1.2])
    parser.add_argument('--output', help='New output directory (never overwrites)')
    parser.add_argument('--time-step-s', type=float, help='Requested maximum mean timestep; count rounds up and effective mean is duration/count')
    parser.add_argument('--detach', action='store_true', help='Submit and exit; scheduler retains the job')
    parser.add_argument('--job-id', help='Stable submission ID for retry/deduplication')
    parser.add_argument('--capture-layer-fields', action='store_true',
                        help='Persist per-step thickness grids in the result asset (opt-in, capped at 256 MiB)')
    args = parser.parse_args(argv)
    cancel = threading.Event()
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    output = None
    output_initialized = False
    started = time.monotonic()
    def progress(value):
        assert output is not None
        temporary = output / 'status.tmp'
        temporary.write_text(json.dumps(plain(value), ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
        replace_with_retry(temporary, output / 'status.json')
    try:
        service = CoatService(args.root)
        output = Path(args.output) if args.output else service.root / 'artifacts' / 'algorithm-runs' / uuid4().hex
        data = json.loads(Path(args.settings).read_text(encoding='utf-8')) if args.settings else {
            'grid_size': args.grid_size}
        if not isinstance(data, dict):
            raise ValueError('Settings must be a JSON object')
        if args.time_step_s is not None and (args.time_steps is not None or 'time_steps' in data):
            raise ValueError('time_steps and time_step_s are mutually exclusive')
        timing = None
        if args.time_step_s is not None:
            timing = service.time_fields(process_path=args.process, time_step_s=args.time_step_s)
            data['time_steps'] = timing['time_steps']
        elif args.time_steps is not None:
            data['time_steps'] = args.time_steps
        elif not args.settings:
            data['time_steps'] = 120
        output.mkdir(parents=True, exist_ok=False)
        progress({'status': 'starting', 'algorithm': args.algorithm})
        output_initialized = True
        settings = service._settings(data)
        options = {'tolerance_percent': args.tolerance_percent,
                   'rpm_scale_bounds': args.rpm_scale_bounds}
        if args.budget_s is not None:
            options['budget_s'] = args.budget_s
        if args.max_evaluations is not None:
            options['max_evaluations'] = args.max_evaluations
        from dataclasses import asdict
        from .scheduler import SchedulerClient, freeze_spec
        spec = freeze_spec(service, 'process', {'settings': asdict(settings),
            'process_path': args.process, 'backend': args.backend, 'fast_cycle': args.fast_cycle,
            'include_layer_fields': args.capture_layer_fields,
            'algorithm': args.algorithm, 'algorithm_options': options})
        client = SchedulerClient(service.root)
        job = client.submit(spec, job_id=args.job_id,
            metadata={'summary': 'CLI · ' + args.algorithm, 'origin': {'surface': 'cli'}})
        progress({'status': job['state'], 'job_id': job['job_id'], 'algorithm': args.algorithm})
        if args.detach:
            print(json.dumps({'output': str(output.resolve()), 'job_id': job['job_id'], 'state': job['state'], 'timing': timing}, ensure_ascii=False))
            return 0
        def report_status(state):
            progress({'job_id': job['job_id'], 'status': state['state'], **(state.get('progress') or {})})
        state = client.wait_for_terminal(job['job_id'], on_update=report_status,
                                         should_stop=cancel.is_set, wait_s=1.0)
        if state['state'] != 'succeeded':
            if cancel.is_set():
                print(json.dumps({'job_id': job['job_id'], 'state': state['state'], 'detached': True}, ensure_ascii=False))
                return 0
            raise RuntimeError(str(state.get('error_code')) + ': ' + str(state.get('error')))
        reply = state['result'] if 'result' in state else client.result(job['job_id'])
        result_artifact = next((item for item in reply.get('result_artifacts', [])
                                if item.get('artifact_id') == reply.get('artifact_id')), None)
        client.fetch_artifact(reply['artifact_id'], output / 'result.npz', metadata=result_artifact)
        bundle = next((item for item in reply['result_artifacts'] if item['role'] == 'bundle'), None)
        if bundle: client.fetch_artifact(bundle['artifact_id'], output / 'result.coatasset', metadata=bundle)
        from .scheduler.worker import load_result
        result = load_result(reply, client)
        report = result.get('algorithm_report', {})
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        progress({**report, 'job_id': job['job_id'], 'output_saved': True,
                  'result_file': str((output / 'result.npz').resolve())})
        print(json.dumps({'output': str(output.resolve()), 'job_id': job['job_id'], **report, 'timing': timing}, ensure_ascii=False))
        return 0
    except Exception as exc:
        if output_initialized:
            try:
                progress({'status': 'cancelled' if isinstance(exc, InterruptedError) else 'failed',
                          'algorithm': args.algorithm, 'error': str(exc), 'elapsed_s': time.monotonic()-started})
            except Exception:
                pass  # Preserve the original failure if status publication also fails.
        print(json.dumps({'output': str(output.resolve()) if output is not None else None,
                          'error': str(exc)}, ensure_ascii=False))
        return 2
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == '__main__':
    raise SystemExit(main())
