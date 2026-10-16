"""Pinned-main integration at the CLI and desktop control boundaries.

All inputs are generated small fixtures. No user asset or credential is read.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from coat_sim.scheduler import SchedulerClient
from coat_sim.core.api import CoatService
from coat_sim.core.compute_tasks import ComputeTaskStore
from coat_sim.core.simulator import Settings
from coat_sim.agent.storage import AssistantStore
from tests.test_process_optimization_modes import fixture
from tests.test_scheduler_service import job_spec, wait_state, stop_service

ROOT = Path(__file__).resolve().parents[1]
APP = QApplication.instance() or QApplication([])

class MainSchedulerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = SchedulerClient(ROOT)
        self.addCleanup(self.stop)

    def stop(self):
        try:
            stop_service(self.client)
        except (OSError, RuntimeError, ValueError):
            pass

    def cli(self, detached):
        parent = ROOT / 'artifacts' / 'integration-fixtures'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            directory = Path(tmp)
            _, path = fixture(directory, mask_count=1, source_count=1)
            original = {file.name: file.read_bytes() for file in directory.glob('*.json')}
            output = directory / 'cli-output'
            command = [sys.executable, '-m', 'coat_sim.algorithm_cli', '--process', str(path),
                       '--grid-size', '21', '--time-steps', '80', '--backend', 'CPU',
                       '--max-evaluations', '1', '--output', str(output)]
            if detached:
                command.append('--detach')
            cli = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)
            self.assertEqual(cli.returncode, 0, cli.stderr + '\n' + cli.stdout)
            reply = json.loads(cli.stdout.strip().splitlines()[-1])
            final = wait_state(self.client, reply['job_id'], 'succeeded', 'failed', 'cancelled')
            self.assertEqual(final['state'], 'succeeded', final)
            from coat_sim.mcp import server
            with patch.object(server, 'service', CoatService(ROOT)):
                self.assertEqual(server.calculation_job_status(reply['job_id'])['state'], final['state'])
                self.assertEqual(server.calculation_job_result(reply['job_id'])['artifact_id'],
                                 self.client.result(reply['job_id'])['artifact_id'])
            self.assertEqual(original, {file.name: file.read_bytes() for file in directory.glob('*.json')})
            if not detached:
                result = self.client.result(reply['job_id'])
                artifact = self.client.artifact(result['artifact_id'])
                data = (output / 'result.npz').read_bytes()
                self.assertEqual(len(data), artifact['bytes'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), artifact['sha256'])
                self.assertTrue((output / 'result.coatasset').is_file())
                self.assertTrue((output / 'report.json').is_file())

    def test_algorithm_cli_detaches_without_cancelling_and_mcp_sees_same_job(self):
        self.cli(True)

    def test_algorithm_cli_downloads_verified_result_and_bundle(self):
        self.cli(False)

    def test_authorized_mcp_submission_is_deduplicated_and_matches_client_result(self):
        from coat_sim.mcp import server
        parent = ROOT / 'artifacts' / 'integration-fixtures'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            directory = Path(tmp)
            service, path = fixture(directory, mask_count=1, source_count=1)
            client = SchedulerClient(directory)
            store = ComputeTaskStore(directory)
            assistant = AssistantStore(directory / 'artifacts/assistant/state.sqlite3')
            session = assistant.new_session('isolated integration')
            assistant.set_ui_session_mode(session, 'operate')
            assistant.bind_ui_request(session, 'test-request', 'operate')
            assistant.record_event(session, 'test-request', 'progress', {'stage': 'received', 'mode': 'operate'})
            parameters = {'settings': asdict(Settings(grid_size=21, time_steps=80, carrier_rpm=1)),
                          'process_path': str(path), 'backend': 'CPU', 'fast_cycle': False,
                          'algorithm': 'fast', 'algorithm_options': {}}
            try:
                with patch.object(server, 'service', service):
                    prepared = server._submit_job('process', parameters, None, 'integration-stable-request')
                    self.assertEqual(prepared['state'], 'prepared')
                    estimate = prepared['estimate_id']
                    origin = store.request(estimate, session_id=session, request_id='test-request')
                    store.decide(estimate, True, automatic=origin['release_policy'] == 'immediate')
                    job = server._submit_job('process', parameters, None, 'integration-stable-request', estimate_id=estimate)
                    duplicate = server._submit_job('process', parameters, None, 'integration-stable-request', estimate_id=estimate)
                    self.assertEqual(job['job_id'], duplicate['job_id'])
                    final = wait_state(client, job['job_id'], 'succeeded', 'failed', 'cancelled')
                    self.assertEqual(final['state'], 'succeeded', final)
                    self.assertEqual(server.calculation_job_status(job['job_id'])['state'], 'succeeded')
                    self.assertEqual(server.calculation_job_result(job['job_id'])['artifact_id'],
                                     client.result(job['job_id'])['artifact_id'])
                    self.assertEqual(len(store.snapshot()['jobs']), 1)
            finally:
                stop_service(client)

    def test_assistant_cancel_and_refresh_use_shared_scheduler(self):
        from coat_sim.ui import assistant_desktop
        job = self.client.submit(job_spec(delay_s=3))
        wait_state(self.client, job['job_id'], 'running')
        received, ready = {}, threading.Event()
        def emit(value, error):
            received.update(snapshot=value, error=error)
            ready.set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            dummy = SimpleNamespace(_status='', changed=Mock(), _closed=False,
                _fetching=False, _cursor=0, _executor=executor,
                snapshotReady=SimpleNamespace(emit=emit))
            dummy.refreshQueue = lambda: assistant_desktop.DesktopAssistantBridge.refreshQueue(dummy)
            with patch.object(assistant_desktop, 'ROOT', ROOT):
                assistant_desktop.DesktopAssistantBridge.cancelQueueJob(dummy, job['job_id'])
                self.assertTrue(ready.wait(10), 'assistant snapshot did not arrive')
            self.assertEqual(received['error'], '')
            self.assertFalse(dummy._status.startswith('无法取消'), dummy._status)
            matching = [row for row in received['snapshot']['jobs'] if row['job_id'] == job['job_id']]
            self.assertEqual(len(matching), 1)
            self.assertTrue(matching[0]['cancel_requested'])
        final = wait_state(self.client, job['job_id'], 'cancelled', 'succeeded', 'failed')
        self.assertEqual(final['state'], 'cancelled', final)
        next_job = self.client.submit(job_spec(delay_s=.04))
        self.assertEqual(wait_state(self.client, next_job['job_id'], 'succeeded', 'failed')['state'], 'succeeded')

    def test_control_close_detaches_observer_and_preserves_admitted_job(self):
        from coat_sim.ui.control import WindowRegistry
        job = self.client.submit(job_spec(delay_s=.6))
        wait_state(self.client, job['job_id'], 'running')
        actions = []
        bridge = SimpleNamespace(detach_observers=lambda: actions.append('detached'),
                                 cancel=lambda: self.fail('closing a window cancelled the job'))
        adapter = SimpleNamespace(snapshot=lambda: {'dirty': False, 'busy': True},
                                  bridge=bridge, window=SimpleNamespace(close=lambda: actions.append('closed')))
        registry = SimpleNamespace(manager=SimpleNamespace(_loading=set()), _pending={},
                                   _child=lambda key: None, _adapter=lambda key: adapter)
        result = WindowRegistry.dispatch(registry, 'close_window', {'window_type': 'main', 'force': True})
        self.assertEqual(result['state'], 'closing')
        deadline = time.monotonic() + 3
        while 'closed' not in actions and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.01)
        self.assertEqual(actions, ['detached', 'closed'])
        final = wait_state(self.client, job['job_id'], 'succeeded', 'failed', 'cancelled')
        self.assertEqual(final['state'], 'succeeded', final)
        self.assertFalse(final['cancel_requested'])

    def test_synthetic_contexts_restore_client_factories_in_fresh_processes(self):
        # A fresh interpreter is essential: a pre-imported submission module
        # hides the first-import/package-patch leakage this regression covers.
        script = r'''
from contextlib import ExitStack
import importlib
from pathlib import Path
import sys
import tempfile
import coat_sim.scheduler as package
from coat_sim.scheduler.client import SchedulerClient as real_client
assert 'coat_sim.scheduler.submission' not in sys.modules
module = importlib.import_module(sys.argv[1])
from coat_sim.scheduler import submission
case = getattr(module, sys.argv[2])()
with tempfile.TemporaryDirectory() as directory:
    case.root = Path(directory)
    case.service = object()
    case.transport = object()
    assert package.SchedulerClient is real_client
    assert submission.SchedulerClient is real_client
    with ExitStack() as stack:
        context = getattr(case, sys.argv[3])()
        if isinstance(context, tuple):
            for patcher in context:
                stack.enter_context(patcher)
        else:
            stack.enter_context(context)
        assert package.SchedulerClient is not real_client
        assert submission.SchedulerClient is not real_client
    assert package.SchedulerClient is real_client
    assert submission.SchedulerClient is real_client
print('factories restored')
'''
        helpers = (
            ('tests.test_appearance_compute_entrypoints', 'AppearanceComputeEntrypointTests', '_scheduler_context'),
            ('tests.test_film_compute_entrypoints', 'FilmComputeEntrypointTests', '_scheduler_context'),
            ('tests.test_ai_appearance_operations_contract', 'RegisteredAppearanceServiceContractTests', '_server_patches'),
            ('tests.test_ai_film_operations_contract', 'RegisteredFilmServiceContractTests', '_patch_services'),
        )
        for module, case, context in helpers:
            with self.subTest(helper=module):
                process = subprocess.run([sys.executable, '-c', script, module, case, context],
                                         cwd=ROOT, text=True, capture_output=True, timeout=30)
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                self.assertIn('factories restored', process.stdout)
