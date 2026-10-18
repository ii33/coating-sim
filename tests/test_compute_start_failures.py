"""Admission, observer launch and service-worker failures have distinct owners."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from contextlib import contextmanager
from pathlib import Path
import threading
import time
from typing import cast
from unittest.mock import patch
import numpy as np

from coat_sim.core.api import CoatService
from coat_sim.core.compute_tasks import ComputeTaskStore
from coat_sim.ui.process import ProcessBridge, _OptimizationWorker
from tests import test_desktop_compute_queue as support
from tests.scheduler_queue_support import SchedulerHarness
from tests.test_process_optimization_modes import fixture, options
from tests.test_scheduler_service import job_spec


class ComputeStartFailureTests(unittest.TestCase):
    root: Path
    service: CoatService
    store: ComputeTaskStore
    scheduler: SchedulerHarness

    setUp = support.QueueTests.setUp
    tearDown = support.QueueTests.tearDown

    def spin(self, predicate, timeout=12):
        support.QueueTests.spin(cast(support.QueueTests, self), predicate, timeout)

    def bridge(self):
        return support.QueueTests.bridge(cast(support.QueueTests, self))

    def spin_ui(self, predicate, timeout=12):
        """Deliver async admission results without launching scheduler workers."""
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            support.APP.processEvents()
            time.sleep(.01)
        support.APP.processEvents()
        self.assertTrue(predicate())

    @contextmanager
    def fail_main_admission_worker_start(self, error):
        original_start = threading.Thread.start
        def start(thread, *args, **kwargs):
            if thread.name == 'scheduler-result-observer':
                raise error
            return original_start(thread, *args, **kwargs)
        # This thread now owns admission as well as observation. Leave other
        # background probes alone instead of failing every Python thread.
        with patch('coat_sim.ui.main.threading.Thread.start', autospec=True,
                   side_effect=start) as launch:
            yield launch

    def run_main_successor(self, bridge, *, bounded_result=False):
        prior_ids = {row['job_id'] for row in self.store.snapshot()['jobs']}
        if bounded_result:
            # The unknown-headroom test submits its original huge grid, but
            # this adapter test never allocates it or launches a physics worker.
            result = {'field_nm': np.ones((21, 21)), 'x': np.arange(21.),
                      'y': np.arange(21.), 'device': 'CPU',
                      '_asset': {'run_id': 'f' * 32}}
            with patch('coat_sim.scheduler.worker.load_result', return_value=result):
                bridge.calculate()
                self.spin_ui(lambda: any(row['job_id'] not in prior_ids
                                        for row in self.store.snapshot()['jobs']))
                row = next(row for row in self.store.snapshot()['jobs'] if row['job_id'] not in prior_ids)
                self.scheduler.scheduler._commit('transition', row['job_id'],
                    state='succeeded', result={'asset': {'run_id': 'f' * 32}},
                    progress={'fraction': 1})
                self.spin(lambda: not bridge.busy)
            self.assertIsNone(self.scheduler.scheduler.active)
        else:
            bridge.calculate()
            self.spin(lambda: not bridge.busy)
        self.assertTrue(bridge.hasResult, bridge.status)
        rows = self.store.snapshot()['jobs']
        self.assertEqual(len(rows), len(prior_ids) + 1)
        row = next(row for row in rows if row['job_id'] not in prior_ids)
        self.assertEqual(row['state'], 'succeeded', row)

    def fail_next_admission_event(self, message, error_type=OSError):
        original = self.scheduler.scheduler.store.queue._event
        calls = 0
        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise error_type(message)
            return original(*args, **kwargs)
        return patch.object(self.scheduler.scheduler.store.queue, '_event', side_effect=fail_once)

    def test_main_admission_thread_start_errors_leave_no_job_and_allow_retry(self):
        for error_type in (OSError, MemoryError):
            with self.subTest(error=error_type.__name__):
                bridge = self.bridge()
                before = self.store.snapshot()
                message = 'bounded main admission worker ' + error_type.__name__
                with self.fail_main_admission_worker_start(error_type(message)):
                    bridge.calculate()
                self.assertFalse(bridge.busy, bridge.status)
                self.assertEqual(bridge._queued_keys, {})
                self.assertEqual(bridge._job_cancel_events, {})
                self.assertIsNone(bridge._cancel_event)
                self.assertIn(message, bridge.status)
                self.assertNotIn('独立队列已接收任务', bridge.status)
                self.assertEqual(self.store.snapshot(), before)
                self.assertFalse(bridge.hasResult)
                self.run_main_successor(bridge)

    def test_main_observer_errors_preserve_admitted_job_and_allow_retry(self):
        for error_type in (OSError, MemoryError):
            with self.subTest(error=error_type.__name__):
                bridge = self.bridge()
                prior_ids = {row['job_id'] for row in self.store.snapshot()['jobs']}
                message = 'bounded admitted main observer ' + error_type.__name__
                with patch('coat_sim.ui.main.observe_job', side_effect=error_type(message)) as observe:
                    bridge.calculate()
                    self.spin_ui(lambda bridge=bridge: not bridge.busy)
                self.assertEqual(bridge._queued_keys, {})
                self.assertEqual(bridge._job_cancel_events, {})
                self.assertIsNone(bridge._cancel_event)
                self.assertIn(message, bridge.status)
                self.assertNotIn('提交状态未知', bridge.status)
                row = next(row for row in self.store.snapshot()['jobs'] if row['job_id'] not in prior_ids)
                self.assertEqual(len(self.store.snapshot()['jobs']), len(prior_ids) + 1)
                observe.assert_called_once()
                self.assertEqual(observe.call_args.args[1], row['job_id'])
                self.assertEqual(row['state'], 'queued')
                self.assertFalse(row['cancel_requested'])
                self.spin(lambda row=row: self.store.read_job(row['job_id'])['state'] in ('succeeded', 'failed'))
                self.assertEqual(self.scheduler.client().status(row['job_id'])['state'], 'succeeded')
                self.assertFalse(bridge.hasResult)
                self.run_main_successor(bridge)

    def test_main_admission_metadata_failure_rolls_back_before_observation(self):
        for error_type in (OSError, MemoryError):
            with self.subTest(error=error_type.__name__):
                bridge = self.bridge()
                before = self.store.snapshot()
                message = 'bounded queue metadata allocation ' + error_type.__name__
                request = self.scheduler.request
                rejections: list[tuple[str, type[Exception], str]] = []
                def rpc_request(action, arguments, request=request, rejections=rejections):
                    try:
                        return request(action, arguments)
                    except Exception as exc:
                        # The real daemon returns {ok: false, error: ...} and
                        # SchedulerClient._call raises ValueError. A raw OS
                        # exception from this in-process harness would instead
                        # simulate a lost transport ACK, not a known rejection.
                        rejections.append((action, type(exc), str(exc)))
                        raise ValueError(f'{type(exc).__name__}: {exc}') from None
                with self.fail_next_admission_event(message, error_type), \
                        patch.object(self.scheduler, 'request', side_effect=rpc_request), \
                        patch('coat_sim.ui.main.observe_job') as observe:
                    bridge.calculate()
                    self.spin_ui(lambda bridge=bridge: not bridge.busy)
                observe.assert_not_called()
                self.assertEqual(rejections, [('submit', error_type, message)])
                self.assertIn(message, bridge.status)
                self.assertNotIn('提交状态未知', bridge.status)
                self.assertEqual(bridge._queued_keys, {})
                self.assertEqual(bridge._job_cancel_events, {})
                self.assertEqual(self.store.snapshot(), before)
                self.run_main_successor(bridge)

    def test_main_failed_new_admission_thread_preserves_running_submission_identity(self):
        bridge = self.bridge()
        gate = self.root / 'gate'
        gate.mkdir()
        with self.scheduler.gated_workers(gate):
            try:
                bridge.calculate()
                self.spin(lambda: (gate / 'started-21').exists())
                old_id = bridge._job_id
                old_inputs = bridge._pending_export_inputs
                old_queue_id = bridge._job_cancel_events[old_id].job_id
                bridge._values['grid_size'] = '23'
                with self.fail_main_admission_worker_start(OSError('new admission worker failed')):
                    bridge.calculate()
                self.assertTrue(bridge.busy)
                self.assertEqual(bridge._job_id, old_id)
                self.assertIs(bridge._pending_export_inputs, old_inputs)
                self.assertEqual(list(bridge._job_cancel_events), [old_id])
                self.assertEqual([row['job_id'] for row in self.store.snapshot()['jobs']], [old_queue_id])
            finally:
                (gate / 'release').touch()
                self.spin(lambda: not bridge.busy)
                self.spin(lambda: all(row['state'] in ('succeeded', 'failed') for row in self.store.snapshot()['jobs']))
        self.assertTrue(bridge.hasResult, bridge.status)
        self.assertEqual(bridge._export_inputs['settings'].grid_size, 21)
        self.assertEqual([row['state'] for row in self.store.snapshot()['jobs']], ['succeeded'])
        self.run_main_successor(bridge)
        self.assertEqual(bridge._export_inputs['settings'].grid_size, 23)
        self.assertEqual([row['state'] for row in self.store.snapshot()['jobs']], ['succeeded', 'succeeded'])
        self.assertFalse(any(row['cancel_requested'] for row in self.store.snapshot()['jobs']))

    def test_unknown_memory_headroom_still_submits_large_request(self):
        bridge = self.bridge()
        bridge._values['grid_size'] = '100000'
        with patch('coat_sim.core.compute_resources.available_memory_bytes', return_value=None):
            self.run_main_successor(bridge, bounded_result=True)
        row = self.store.snapshot()['jobs'][0]
        self.assertIn('当前可用内存未知', row['memory_estimate_label'])
        self.assertEqual(bridge._export_inputs['settings'].grid_size, 100000)

    def optimization_bridge(self):
        path = self.root / 'process.cproc'
        service = self.service
        if not path.is_file():
            service, path = fixture(self.root, mask_count=0, source_count=1)
        return service, ProcessBridge(service, str(path))

    def run_optimization_successor(self, service, bridge):
        prior_ids = {row['job_id'] for row in self.store.snapshot()['jobs']}
        bridge.startOptimization({'grid_size': 21, 'time_steps': 8, 'carrier_rpm': 1.}, options())
        self.spin(lambda: not bridge.optimization['busy'])
        self.assertFalse(bridge._workers)
        self.assertIsNone(bridge._worker)
        self.assertTrue(bridge.pendingMasks, bridge.optimization)
        rows = self.store.snapshot()['jobs']
        self.assertEqual(len(rows), len(prior_ids) + 1)
        row = next(row for row in rows if row['job_id'] not in prior_ids)
        self.assertEqual(row['state'], 'succeeded', row)

    def test_optimization_observer_start_errors_preserve_admitted_job(self):
        for failure in (OSError('bounded Qt observer OSError'), MemoryError('bounded Qt observer MemoryError'), None):
            with self.subTest(error=type(failure).__name__):
                service, bridge = self.optimization_bridge()
                prior_ids = {row['job_id'] for row in self.store.snapshot()['jobs']}
                with patch.object(_OptimizationWorker, 'start', side_effect=failure, return_value=None):
                    bridge.startOptimization({'grid_size': 21, 'time_steps': 8, 'carrier_rpm': 1.}, options())
                message = str(failure) if failure is not None else '无法创建优化计算线程'
                self.assertFalse(bridge.optimization['busy'], bridge.optimization)
                self.assertFalse(bridge._workers)
                self.assertIsNone(bridge._worker)
                self.assertIn(message, bridge.optimization['message'])
                row = next(row for row in self.store.snapshot()['jobs'] if row['job_id'] not in prior_ids)
                self.assertEqual(row['state'], 'queued')
                self.assertFalse(row['cancel_requested'])
                self.spin(lambda: self.store.read_job(row['job_id'])['state'] in ('succeeded', 'failed'))
                self.assertEqual(self.scheduler.client().status(row['job_id'])['state'], 'succeeded')
                self.run_optimization_successor(service, bridge)

    def test_optimization_admission_metadata_failure_rolls_back(self):
        service, bridge = self.optimization_bridge()
        message = 'bounded optimization metadata failure'
        with self.fail_next_admission_event(message), patch.object(_OptimizationWorker, 'start') as launch:
            bridge.startOptimization({'grid_size': 21, 'time_steps': 8, 'carrier_rpm': 1.}, options())
        launch.assert_not_called()
        self.assertFalse(bridge.optimization['busy'])
        self.assertIn(message, bridge.optimization['message'])
        self.assertEqual(self.store.snapshot()['jobs'], [])
        self.assertEqual(self.store.snapshot()['events'], [])
        self.run_optimization_successor(service, bridge)

    def test_optimization_memory_warning_retained_without_blocking_submission(self):
        service, bridge = self.optimization_bridge()
        gate = self.root / 'gate'; gate.mkdir()
        with patch('coat_sim.core.compute_resources.available_memory_bytes', return_value=1), self.scheduler.gated_workers(gate):
            try:
                bridge.startOptimization({'grid_size': 21, 'time_steps': 8, 'carrier_rpm': 1.}, options())
                self.spin(lambda: (gate / 'started-21').exists())
                row = self.store.snapshot()['jobs'][0]
                self.assertEqual(row['state'], 'running')
                self.assertTrue(row['memory_warning'])
                self.assertIn('可能失败', row['memory_estimate_label'])
                self.assertIn('可能失败', bridge.optimization['message'])
            finally:
                (gate / 'release').touch()
                self.spin(lambda: not bridge.optimization['busy'])

    def test_scheduler_launch_metadata_failure_reaps_child_and_releases_slot(self):
        from coat_sim.scheduler import daemon
        for stage in ('guard', 'worker_identity', 'launch_file'):
            for failure_type in (OSError, MemoryError):
                with self.subTest(stage=stage, error=failure_type.__name__):
                    scheduler = self.scheduler.scheduler
                    client = self.scheduler.client()
                    job = client.submit(job_spec())
                    message = 'bounded launch ' + stage + ' ' + failure_type.__name__
                    target, attribute = {
                        'guard': (scheduler.guard, 'attach'),
                        'worker_identity': (scheduler.store, 'set_worker'),
                        'launch_file': (daemon, 'write_json'),
                    }[stage]
                    spawned = []
                    original = daemon.subprocess.Popen
                    def spawn(*args, **kwargs):
                        process = original(*args, **kwargs)
                        spawned.append(process)
                        return process
                    try:
                        with patch.object(target, attribute, side_effect=failure_type(message)), \
                                patch.object(daemon.subprocess, 'Popen', side_effect=spawn):
                            self.scheduler.tick()
                        self.assertEqual(len(spawned), 1)
                        self.assertIsNotNone(spawned[0].poll(), 'failed admission left a live untracked worker')
                        row = self.store.read_job(job['job_id'])
                        self.assertEqual(row['state'], 'failed')
                        self.assertEqual(row['error_code'], 'worker_start_failed')
                        self.assertIn(message, row['error'])
                        self.assertTrue(row['finished_at'])
                        with self.store.connection() as db:
                            self.assertIsNone(db.execute('SELECT owner FROM execution_lease WHERE singleton=1').fetchone()[0])
                        next_job = client.submit(job_spec())
                        self.spin(lambda: self.store.read_job(next_job['job_id'])['state'] in ('succeeded', 'failed'))
                        self.assertEqual(client.status(next_job['job_id'])['state'], 'succeeded')
                        self.assertEqual(self.store.read_job(job['job_id']), row)
                    finally:
                        for process in spawned:
                            if process.poll() is None:
                                process.terminate()
                            process.wait(timeout=10)

    def test_scheduler_terminal_write_failure_preserves_original_launch_error(self):
        from coat_sim.scheduler import daemon
        scheduler = self.scheduler.scheduler
        job = self.scheduler.client().submit(job_spec())
        original_error = MemoryError('original worker identity persistence failure')
        original_spawn = daemon.subprocess.Popen
        spawned = []
        def spawn(*args, **kwargs):
            process = original_spawn(*args, **kwargs)
            spawned.append(process)
            return process
        with patch.object(scheduler.store, 'set_worker', side_effect=original_error), \
                patch.object(scheduler.store, 'transition', side_effect=OSError('terminal database write failed')), \
                patch.object(daemon.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(MemoryError) as caught:
                self.scheduler.tick()
        self.assertIs(caught.exception, original_error)
        self.assertIn('terminal database write failed', '\n'.join(original_error.__notes__))
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())
        # No terminal state or released slot is claimed without its durable write.
        row = self.store.read_job(job['job_id'])
        self.assertEqual(row['state'], 'running')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT owner FROM execution_lease WHERE singleton=1').fetchone()[0], job['job_id'])
        scheduler._commit('transition', job['job_id'], state='failed', error_code='worker_start_failed',
                          error=str(original_error))

    def test_scheduler_failed_child_cleanup_keeps_owned_slot_and_original_error(self):
        from contextlib import ExitStack
        from coat_sim.scheduler import daemon
        scheduler = self.scheduler.scheduler
        job = self.scheduler.client().submit(job_spec())
        original_error = MemoryError('original worker identity failure')
        original_spawn = daemon.subprocess.Popen
        spawned = []
        with ExitStack() as cleanup_patches:
            def spawn(*args, **kwargs):
                process = original_spawn(*args, **kwargs)
                spawned.append(process)
                cleanup_patches.enter_context(patch.object(process, 'terminate', side_effect=OSError('bounded cleanup failure')))
                return process
            with patch.object(scheduler.store, 'set_worker', side_effect=original_error), \
                    patch.object(daemon.subprocess, 'Popen', side_effect=spawn):
                with self.assertRaises(MemoryError) as caught:
                    self.scheduler.tick()
            self.assertIs(caught.exception, original_error)
            self.assertIn('bounded cleanup failure', '\n'.join(original_error.__notes__))
            assert scheduler.active is not None
            self.assertEqual(scheduler.active[0], job['job_id'])
            self.assertIs(scheduler.active[1], spawned[0])
            self.assertTrue(scheduler.recovery_error)
            self.assertEqual(self.store.read_job(job['job_id'])['state'], 'running')
        # Fixture teardown can now reap exactly this retained process handle.


if __name__ == '__main__':
    unittest.main()
