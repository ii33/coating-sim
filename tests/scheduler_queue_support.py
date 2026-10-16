"""Local protocol peer with real scheduler ownership and isolated CPU workers.

The test owns tick scheduling, so admission and worker launch can be inspected
separately. No HTTP server, background daemon, or user runtime is started.
"""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import io
import json
import sys

from coat_sim.scheduler.client import SchedulerClient
from coat_sim.scheduler.daemon import Scheduler, public_payload
from coat_sim.scheduler.transport import download_verified


class SchedulerHarness:
    def __init__(self, root):
        self.scheduler = Scheduler(root)

    def client(self, root=None, **kwargs):
        if Path(root or self.scheduler.root).resolve() != self.scheduler.root:
            raise AssertionError("test client escaped its isolated scheduler root")
        return SchedulerClient(self.scheduler.root, transport=self)

    def request(self, action, arguments):
        # Apply the same JSON isolation and path sanitization as the HTTP peer.
        arguments = json.loads(json.dumps(arguments, allow_nan=False))
        return public_payload(self.scheduler.dispatch(action, arguments))

    def download(self, artifact_id, destination):
        item = self.scheduler.resolve_artifact(artifact_id)
        content = Path(item['local_path']).read_bytes()
        class Opener:
            def open(self, url, timeout):
                return io.BytesIO(content)
        return download_verified(Opener(), 'test-artifact:' + artifact_id,
                                 destination, item)

    def tick(self):
        self.scheduler.tick()

    def close(self):
        scheduler = self.scheduler
        scheduler.stopping.set()
        if scheduler.active:
            job_id, process, _, _ = scheduler.active
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            from coat_sim.scheduler.daemon import _interrupted_job_changes
            row = scheduler.memory_status(job_id)
            scheduler._commit('transition', job_id, **_interrupted_job_changes(row))
            scheduler.active = None
        with scheduler.condition:
            scheduler.condition.notify_all()
        scheduler.guard.close()
        scheduler.lock.close()

    @contextmanager
    def gated_workers(self, gate, *, crash_grid=None):
        from coat_sim.scheduler import daemon
        original = daemon.module_command
        def command(module, *arguments):
            if module == 'coat_sim.scheduler' and 'worker' in arguments:
                return [sys.executable, '-m', 'tests.scheduler_queue_worker',
                        '--gate', str(gate), '--crash-grid', str(crash_grid or 0),
                        *arguments]
            return original(module, *arguments)
        with patch.object(daemon, 'module_command', side_effect=command):
            yield

    @contextmanager
    def resource_failure_workers(self, failure):
        """Route bounded recipe jobs through the isolated resource-fault child.

        The fixture validates the frozen job before a process is launched and
        records the real Popen objects so tests can verify their terminal exit
        status. Any worker left alive on test exit is reaped without changing
        the scheduler's cancellation or outcome path.
        """
        if type(failure) is not str or failure not in ('memory', 'crash'):
            raise ValueError("failure must be 'memory' or 'crash'")

        from coat_sim.scheduler import daemon
        from coat_sim.scheduler.jobs import JobSpec
        original_command = daemon.module_command
        original_popen = daemon.subprocess.Popen
        processes = []

        def command(module, *arguments):
            # start_job's launch contract is exactly:
            # --root ROOT worker JOB_ID OWNER_PID OWNER_INSTANCE.
            if module != 'coat_sim.scheduler' or 'worker' not in arguments:
                return original_command(module, *arguments)
            if (len(arguments) != 6 or arguments[0] != '--root'
                    or arguments[2] != 'worker'):
                raise ValueError('bounded resource fixture requires the known worker launch contract')
            root = Path(arguments[1]).resolve()
            job_id = arguments[3]
            if root != self.scheduler.root:
                raise ValueError('bounded resource fixture requires the isolated scheduler root')
            if not job_id or job_id in ('.', '..') or Path(job_id).name != job_id:
                raise ValueError('bounded resource fixture requires a safe job id')
            spec_path = root / 'artifacts/runtime/scheduler/jobs' / job_id / 'spec.json'
            frozen = JobSpec.parse_frozen_json(spec_path.read_text(encoding='utf-8'))
            from tests.scheduler_resource_worker import validate_bounded_spec
            validate_bounded_spec(frozen.to_spec())
            return [sys.executable, '-m', 'tests.scheduler_resource_worker',
                    '--failure', failure, *arguments]

        def popen(*args, **kwargs):
            # subprocess is a shared module object. Patching daemon.subprocess
            # also observes unrelated launches from UI probes/background tasks
            # in the long-lived full-suite process. Own and reap only the
            # scheduler child whose bounded spec `command` validated above.
            argv = args[0] if args else kwargs.get('args')
            owned = (isinstance(argv, (list, tuple)) and len(argv) == 11
                     and list(argv[:5]) == [sys.executable, '-m',
                                           'tests.scheduler_resource_worker',
                                           '--failure', failure]
                     and list(argv[5:8]) == ['--root', str(self.scheduler.root),
                                              'worker'])
            process = original_popen(*args, **kwargs)
            if owned:
                processes.append(process)
            return process

        try:
            with (patch.object(daemon, 'module_command', side_effect=command),
                  patch.object(daemon.subprocess, 'Popen', side_effect=popen)):
                yield processes
        finally:
            for process in processes:
                try:
                    if process.poll() is None:
                        try:
                            process.terminate()
                        except OSError:
                            pass
                    try:
                        process.wait(timeout=10)
                    except daemon.subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        finally:
                            process.wait(timeout=10)
                finally:
                    for handle in (process.stdin, process.stdout, process.stderr):
                        if handle is not None:
                            try:
                                handle.close()
                            except OSError:
                                pass
