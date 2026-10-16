# Reconstructed regression suite from historical f12 evidence and current contracts.
# This is not a byte-for-byte recovery of the missing target test.
"""Bounded resource-failure, large-grid validation and persisted-result UI tests."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
import unittest
from unittest.mock import patch
import numpy as np

from coat_sim.core import isolated_compute
from coat_sim.core.api import CoatService, ServiceError
from coat_sim.core.compute_resources import estimate_memory, ensure_result_memory, constrain_worker_memory
from coat_sim.core.schemas import SimulationSettings
from coat_sim.core.simulator import Settings
from coat_sim.ui.main import ThicknessImageProvider
from tests import test_desktop_compute_queue as support


def memory_failure_child(connection, kind, arguments):
    """Exercise real child error capture without allocating any large array."""
    from unittest.mock import patch
    from coat_sim.core.isolated_compute import _compute_child
    target = ('coat_sim.core.api.CoatService._optimize_masks_worker' if kind == 'mask_optimization' else 'coat_sim.core.algorithm_paths.run_recipe')
    with patch(target, side_effect=MemoryError('bounded test allocation failure')):
        _compute_child(connection, kind, arguments)


def budget_failure_child(connection, kind, arguments):
    from coat_sim.core.algorithm_paths import BudgetExceeded
    from coat_sim.core.isolated_compute import _compute_child
    with patch('coat_sim.core.algorithm_paths.run_recipe', side_effect=BudgetExceeded('bounded deadline expiry')):
        _compute_child(connection, kind, arguments)


def crashed_child(connection, kind, arguments):
    os._exit(37)


class ResourceUnitTests(unittest.TestCase):
    def test_reselect_shown_result_invalidates_inflight_different_result(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from coat_sim.ui.compute_queue import QueueResultViewer
        # A is displayed; B is loading; user selects A before B completes.
        viewer = SimpleNamespace(_closed=False, _loading_job='B', _shown_job='A',
            _shown_kind='simulation', _shown_film_result=None, _shown_raytrace_path='',
            _generation=2, _future=Mock(), bridge=SimpleNamespace(hasResult=True),
            window=Mock(), statusChanged=Mock())
        QueueResultViewer.open(viewer, 'A')
        self.assertEqual(viewer._generation, 3)
        self.assertEqual(viewer._loading_job, '')
        viewer._future.cancel.assert_called_once()
        viewer.window.showNormal.assert_called_once()
        QueueResultViewer._receive(viewer, 2, {'name':'B'}, 'B.coatasset', '')
        self.assertEqual(viewer._shown_job, 'A')
        viewer.statusChanged.emit.assert_not_called()

    def test_grid_accepts_above_old_cap_and_requires_integer_structure(self):
        for grid in (502, 1001, 100000):
            Settings(grid_size=grid).validate()
            self.assertEqual(SimulationSettings(grid_size=grid).grid_size, grid)
        for grid in (True, 0, -3, 20, 22.5):
            with self.assertRaises((ValueError, TypeError)):
                Settings(grid_size=grid).validate()
            with self.assertRaises((ValueError, TypeError)):
                SimulationSettings(grid_size=grid)

    def test_memory_warning_precedes_any_grid_allocation_and_does_not_reject(self):
        with patch('numpy.meshgrid', side_effect=AssertionError('estimate allocated')), patch('numpy.zeros', side_effect=AssertionError('estimate allocated')):
            result = estimate_memory(Settings(grid_size=100000), available_bytes=2*1024**3)
        self.assertTrue(result['memory_warning'])
        self.assertGreater(result['estimated_peak_memory_bytes'], 2*1024**3)
        self.assertIn('可能失败', result['memory_estimate_label'])
        self.assertEqual(estimate_memory(Settings(grid_size=21), available_bytes=2*1024**3)['memory_warning'], False)

    def test_memory_error_is_not_wrapped_as_backend_error_or_auto_fallback(self):
        from coat_sim.core.simulator import simulate
        failure = MemoryError('bounded allocation failure')
        with patch('coat_sim.core.simulator._backend', return_value=(np, 'CPU')), patch('coat_sim.core.simulator._simulate_on_backend', side_effect=failure) as calculate:
            with self.assertRaises(MemoryError) as caught:
                simulate(Settings(grid_size=21), backend='自动')
        self.assertIs(caught.exception, failure)
        self.assertEqual(calculate.call_count, 1)

    def test_result_headroom_is_advisory_without_allocation(self):
        for available in (0, 100, None):
            with self.subTest(available=available):
                ensure_result_memory(10**30, copies=4, available_bytes=available)

    def test_legacy_worker_limit_never_samples_or_sets_os_caps(self):
        with patch('coat_sim.core.compute_resources.available_memory_bytes',
                   side_effect=AssertionError('must not impose a headroom gate')):
            self.assertFalse(constrain_worker_memory())

    def test_unknown_memory_estimate_still_allows_valid_grid(self):
        with patch('coat_sim.core.compute_resources.available_memory_bytes', return_value=None):
            value = estimate_memory(Settings(grid_size=100000))
        self.assertIsNone(value['available_memory_bytes'])
        self.assertFalse(value['memory_warning'])
        self.assertIn('当前可用内存未知', value['memory_estimate_label'])

    def test_child_zero_or_unknown_headroom_never_gates_compute_or_result(self):
        from unittest.mock import Mock
        for available in (0, None):
            with self.subTest(available=available):
                connection = Mock()
                with (patch('coat_sim.core.compute_resources.available_memory_bytes', return_value=available),
                      patch('coat_sim.core.compute_resources.constrain_worker_memory', side_effect=AssertionError('hard cap invoked')),
                      patch('coat_sim.core.compute_resources.ensure_result_memory', side_effect=AssertionError('result gate invoked')),
                      patch('coat_sim.core.simulator.simulate', return_value={'ok': True}) as calculate):
                    isolated_compute._compute_child(connection, 'single', {'settings': Settings(grid_size=21),
                        'mask': None, 'backend': 'CPU', 'source_profile': None})
                calculate.assert_called_once()
                message = connection.send.call_args.args[0]
                self.assertEqual(message[0], 'result')
                self.assertIs(message[1]['ok'], True)
                self.assertEqual(message[1]['memory_estimate']['available_memory_bytes'], available)

    def test_asset_format_capacity_failure_is_explicit_and_separate(self):
        from coat_sim.core import simulation_assets
        # Exercise the format branch with a tiny fixture, not a large array.
        import tempfile
        from pathlib import Path
        configuration = {'settings': {}, 'requested_settings': {}, 'machine': {},
                         'process': {}, 'input_snapshot': None}
        with tempfile.TemporaryDirectory() as folder, patch.object(simulation_assets, 'MAX_EXPANDED_BYTES', 1):
            with self.assertRaisesRegex(ValueError, '资产包格式安全容量限制'):
                simulation_assets._write_package(Path(folder) / 'tiny.coatasset', name='tiny',
                    run_id='a' * 32, summary={'created_at': '2026-10-05'},
                    configuration=configuration, overwrite=False)

    def test_preview_downsample_keeps_full_field_and_exact_extrema(self):
        # 8 MiB fixture, rather than a stress allocation.
        import shiboken6
        field = np.zeros((1025, 1025))
        field[512, 511] = 9
        provider = ThicknessImageProvider()
        # This provider is not installed in a QQmlEngine. Keep C++ teardown at
        # the test boundary rather than deferring it into a later GUI test's
        # processEvents/garbage-collection cycle.
        self.addCleanup(shiboken6.delete, provider)
        self.assertEqual(provider.set_field(field), (0, 9))
        self.assertLessEqual(provider._image.width(), 1024)
        self.assertLessEqual(provider._image.height(), 1024)
        self.assertEqual(field.shape, (1025, 1025))
        self.assertEqual(field[512, 511], 9)


class QueueResultResourceTests(unittest.TestCase):
    setUp = support.QueueTests.setUp
    spin = support.QueueTests.spin
    bridge = support.QueueTests.bridge

    def tearDown(self):
        # The harness closes its scheduler via addCleanup. First dispose the
        # standalone test bridges while no observer owns a live Qt signal.
        # QQuickImageProvider is Python-owned when no engine installed it, so
        # leave no provider wrapper for GC during a later QApplication tick.
        import shiboken6
        for bridge in self.bridges:
            bridge.detach_observers()
            shiboken6.delete(bridge.image_provider)
            shiboken6.delete(bridge)
        self.bridges.clear()

    def test_queued_bounded_oom_and_crash_fail_without_cancellation_then_recover(self):
        """The fault runs in the real scheduler child, before recipe allocation.

        Keep the queued grid at 21: a parent-only monkeypatch cannot intercept
        an independently launched worker. A synthetic one-byte headroom reading
        exercises advisory metadata without scheduling an unsafe huge grid.
        """
        bridge = self.bridge()
        for failure, error_code, return_code in (
                ('memory', 'algorithm_failed', 0),
                ('crash', 'worker_crashed', 37)):
            with self.subTest(failure=failure):
                before = {row['job_id'] for row in self.store.snapshot()['jobs']}
                with (patch('coat_sim.core.compute_resources.available_memory_bytes',
                            return_value=1),
                      self.scheduler.resource_failure_workers(failure) as processes):
                    bridge.calculate()
                    self.spin(lambda: not bridge.busy, timeout=20)
                rows = [row for row in self.store.snapshot()['jobs']
                        if row['job_id'] not in before]
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row['state'], 'failed', row)
                self.assertEqual(row['error_code'], error_code, row)
                self.assertFalse(row['cancel_requested'], row)
                self.assertTrue(row['memory_warning'], row)
                self.assertFalse(bridge.hasResult, bridge.status)
                self.assertFalse(bridge.busy)
                self.assertIn('计算失败', bridge.status)
                self.assertIsNone(row.get('result'))
                self.assertEqual(len(processes), 1)
                self.assertEqual(processes[0].returncode, return_code)
                marker_path = (self.root / 'artifacts/runtime/scheduler/jobs' /
                               row['job_id'] / 'resource-failure-fixture.json')
                marker = json.loads(marker_path.read_text(encoding='utf-8'))
                self.assertEqual(marker['job_id'], row['job_id'])
                self.assertEqual(marker['pid'], processes[0].pid)
                self.assertEqual(marker['failure'], failure)
                self.assertEqual((marker['grid_size'], marker['time_steps']), (21, 12))
                self.assertEqual((marker['backend'], marker['algorithm']), ('CPU', 'fast'))
                self.assertFalse(marker['algorithm_entered'])
                self.assertEqual(marker['allocation_boundary'], 'before run_recipe')
                if failure == 'memory':
                    self.assertIn('bounded scheduler allocation failure', row['error'])
                else:
                    self.assertEqual(row['exit_code'], 37)

        # The real physics worker, persisted asset, and UI result path still work
        # after the test-only launch route has been restored.
        bridge.calculate()
        self.spin(lambda: not bridge.busy, timeout=20)
        rows = self.store.snapshot()['jobs']
        self.assertEqual(len(rows), 3)
        success = next(row for row in rows if row['state'] == 'succeeded')
        self.assertFalse(success['cancel_requested'])
        self.assertTrue(bridge.hasResult, bridge.status)
        self.assertEqual(bridge._result['field_nm'].shape, (21, 21))
        self.assertTrue(self.store.result_asset_path(success['job_id']).is_file())

    def test_render_memory_error_clears_old_preview_and_keeps_failure_status(self):
        bridge = self.bridge()
        old = {'field_nm':np.ones((21,21)), 'x':np.arange(21.), 'y':np.arange(21.),
               '_loaded_asset':True, '_asset':{'run_id':'a'*32}}
        bridge._finish(bridge._job_id, old, 1., None)
        self.assertTrue(bridge.hasResult)
        revision = bridge.imageRevision
        loaded = {'result':{'field_nm':np.full((21,21), 2.), 'x':np.arange(21.), 'y':np.arange(21.)},
            'asset_path':'B.coatasset', 'run_id':'b'*32, 'name':'B', 'elapsed_s':1.,
            'settings':{}, 'requested_settings':{}, 'has_results':True}
        with patch.object(bridge.image_provider, 'set_field', side_effect=MemoryError('bounded render failure')):
            self.assertIs(bridge._apply_loaded_asset(loaded, 'B.coatasset'), False)
        self.assertFalse(bridge.hasResult)
        self.assertIsNone(bridge._export_inputs)
        self.assertEqual(bridge._asset_run_id, '')
        self.assertGreater(bridge.imageRevision, revision)
        self.assertEqual(bridge.image_provider._image.size().width(), 1)
        self.assertEqual(bridge.image_provider._image.pixelColor(0,0).alpha(), 0)
        self.assertIn('绘图失败', bridge.status)
        self.assertNotIn('已打开资产包', bridge.status)
        self.assertEqual(bridge.metrics, [])

    def test_real_worker_budget_expiry_preserves_timeout_type(self):
        from coat_sim.core.algorithm_paths import BudgetExceeded
        with patch.object(isolated_compute, '_compute_child', budget_failure_child):
            with self.assertRaisesRegex(BudgetExceeded, 'bounded deadline expiry'):
                self.service.simulate_recipe(Settings(grid_size=21), process_path='examples/process_example.cproc', backend='CPU')

    def test_optimization_memory_error_and_worker_exit_are_isolated(self):
        for target, reason in ((memory_failure_child, '内存不足'), (crashed_child, '退出码 37')):
            with self.subTest(reason=reason), patch.object(isolated_compute, '_compute_child', target):
                with self.assertRaises(ServiceError) as caught:
                    self.service.optimize_masks(Settings(grid_size=21), 'examples/process_example.cproc', options={'backend':'CPU'})
            self.assertIn(reason, str(caught.exception))

    def test_view_completed_asset_reopens_without_recompute_or_mutating_inputs(self):
        from PySide6.QtQml import QQmlApplicationEngine
        from coat_sim.ui.compute_queue import ComputeQueueView
        import shiboken6
        bridge = self.bridge(); bridge.calculate()
        self.spin(lambda: not bridge.busy, timeout=20)
        row = self.store.snapshot()['jobs'][0]
        self.assertEqual(row['state'], 'succeeded', row)
        path = self.store.result_asset_path(row['job_id'])
        stamp = path.stat().st_mtime_ns
        # New session/controller gets everything from SQLite + persisted assets.
        engine = QQmlApplicationEngine()
        view = ComputeQueueView(engine, self.root)
        try:
            with patch.object(CoatService, 'simulate_recipe', side_effect=AssertionError('must never recompute')), patch.object(CoatService, 'prepare_simulation_asset', side_effect=AssertionError('must not rewrite assets')):
                view.openQueueResult(row['job_id']); view.openQueueResult(row['job_id'])
                self.spin(lambda: view._result_viewer.bridge.hasResult or '无法' in view.status, timeout=20)
                self.assertTrue(view._result_viewer.bridge.hasResult, view.status)
                title_viewer = view._result_viewer
                assert title_viewer is not None
                title_window = title_viewer.window
                assert title_window is not None
                self.assertEqual(title_window.property('title'), '计算队列 · 已完成结果')
                self.assertEqual(title_window.property('caption'), '计算队列 · 已完成结果')
                self.assertEqual(view._result_viewer.bridge._asset_run_id, row['result']['run_id'])
                np.testing.assert_array_equal(view._result_viewer.bridge._result['field_nm'], bridge._result['field_nm'])
                view._result_viewer.window.close(); support.APP.processEvents()
                view.openQueueResult(row['job_id'])
                self.assertTrue(view._result_viewer.window.isVisible())
            self.assertEqual(path.stat().st_mtime_ns, stamp)
            self.assertEqual(len(self.store.snapshot()['jobs']), 1)
        finally:
            view.shutdown(); view.pool.shutdown(wait=True)
            if view._result_viewer:
                view._result_viewer.pool.shutdown(wait=True)
            support.APP.processEvents()
            shiboken6.delete(engine)

    def test_missing_or_unfinished_results_never_submit(self):
        job = self.store.register_desktop_job('pending', summary='pending')
        with self.assertRaisesRegex(ValueError, '尚未成功'):
            self.store.result_asset_path('pending')
        job.update(state='succeeded', result={'run_id':'f'*32})
        self.store.update_job(job)
        with self.assertRaisesRegex(ValueError, '已移动或删除'):
            self.store.result_asset_path('pending')
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.store.result_asset_path('unknown')
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)


if __name__ == '__main__':
    unittest.main()

