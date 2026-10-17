import argparse
import json
from pathlib import Path

def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent local calculation scheduler (protocol v1)')
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[2]))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('serve'); sub.add_parser('start'); sub.add_parser('stop'); sub.add_parser('list')
    submit = sub.add_parser('submit'); submit.add_argument('spec'); submit.add_argument('--id')
    for action in ('status', 'cancel', 'result'):
        sub.add_parser(action).add_argument('job_id')
    worker = sub.add_parser('worker'); worker.add_argument('job_id'); worker.add_argument('owner_pid', type=int); worker.add_argument('owner_instance')
    args = parser.parse_args(argv)
    if args.command in ('serve', 'worker'):
        from coat_sim.project_logging import install_process_log
        from coat_sim.assistant_diagnostics import trace
        install_process_log('scheduler' if args.command == 'serve' else 'compute-worker', args.root)
        trace('scheduler_process_start', role=args.command)
    if args.command == 'serve':
        from .daemon import serve
        return serve(args.root)
    if args.command == 'worker':
        from .worker import run
        return run(args.root, args.job_id, args.owner_pid, args.owner_instance)
    from .client import SchedulerClient
    client = SchedulerClient(args.root)
    if args.command == 'start': value = client.ensure_started()
    elif args.command == 'stop': value = client.stop()
    elif args.command == 'submit': value = client.submit(json.loads(Path(args.spec).read_text(encoding='utf-8')), job_id=args.id)
    elif args.command == 'list': value = client.snapshot()
    else: value = getattr(client, args.command)(args.job_id)
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return 0

if __name__ == '__main__': raise SystemExit(main())
