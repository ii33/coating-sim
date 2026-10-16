"""Bounded test-only gate around the unchanged scheduler worker entrypoint."""
import argparse
import json
import os
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gate', type=Path, required=True)
    parser.add_argument('--crash-grid', type=int, default=0)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('command', choices=('worker',))
    parser.add_argument('job_id')
    parser.add_argument('owner_pid', type=int)
    parser.add_argument('owner_instance')
    args = parser.parse_args()
    directory = args.root / 'artifacts/runtime/scheduler/jobs' / args.job_id
    spec = json.loads((directory / 'spec.json').read_text(encoding='utf-8'))
    grid = spec['parameters']['settings']['grid_size']
    (args.gate / f'started-{grid}').touch()
    if grid == 21:
        from coat_sim.scheduler.store import SchedulerStore
        store = SchedulerStore(args.root)
        deadline = time.monotonic() + 20
        while not (args.gate / 'release').exists():
            if store.status(args.job_id)['cancel_requested']:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('test scheduler gate expired')
            time.sleep(.025)
    if grid == args.crash_grid:
        os._exit(37)
    from coat_sim.scheduler.worker import run
    return run(args.root, args.job_id, args.owner_pid, args.owner_instance)


if __name__ == '__main__':
    raise SystemExit(main())
