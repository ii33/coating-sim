import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication
from coat_sim.core.api import CoatService
from coat_sim.core.compute_tasks import ComputeTaskStore, _now
from coat_sim.core.desktop_compute import QueueCancelEvent, execution_slot
from coat_sim.core.process_simulator import process_timing, process_sampling_plan
from coat_sim.core.schemas import SimulationSettings
from coat_sim.core.simulator import Settings
from coat_sim.ui.main import MainBridge
from tests.design_test_fixtures import copy_typed_examples
from tests.scheduler_queue_support import SchedulerHarness
ROOT = Path(__file__).resolve().parents[1]
APP = QApplication.instance() or QApplication([])

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridges = []
        self.patches = []
        self.scheduler = None
        self.addCleanup(lambda: QueueTests._cleanup(self))
        self.root = Path(self.tmp.name)
        copy_typed_examples(self.root)
        self.patches = [patch('coat_sim.core.data_files.PROJECT_ROOT', self.root), patch('coat_sim.core.data_files.DATA_ROOT', self.root / 'data')]
        for item in self.patches: item.start()
        self.service = CoatService(self.root)
        self.store = ComputeTaskStore(self.root)
        self.scheduler = SchedulerHarness(self.root)
        # Import the consumers before replacing factories. A module imported
        # under a package-level patch can otherwise retain that test's factory.
        from coat_sim.ui import main, process, compute_queue
        self.patches.extend(patch.object(module, 'SchedulerClient', self.scheduler.client)
                            for module in (main, process, compute_queue))
        self.patches.append(patch('coat_sim.scheduler.SchedulerClient', self.scheduler.client))
        for item in self.patches[2:]: item.start()
    def tearDown(self):
        pass  # addCleanup also runs after an interrupted setUp.
    def _cleanup(self):
        try:
            for bridge in self.bridges:
                bridge.detach_observers()
        finally:
            try:
                if self.scheduler is not None:
                    self.scheduler.close()
            finally:
                APP.processEvents()
                for item in reversed(self.patches): item.stop()
                self.tmp.cleanup()
    def spin(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.scheduler.tick()
            APP.processEvents()
            time.sleep(.01)
        APP.processEvents()
        self.assertTrue(predicate())
    def bridge(self):
        bridge = MainBridge(self.service)
        bridge.chooseFile('process', QUrl.fromLocalFile(str(self.root / 'examples/process_example.cproc')))
        bridge.setDevice('CPU')
        bridge._values.update(grid_size='21', time_steps='12')
        bridge._time_basis = 'time_steps'
        bridge._fast_cycle = False
        self.bridges.append(bridge)
        return bridge
    def test_no_sampling_cap_validation_and_plan(self):
        Settings(time_steps=200001).validate()
        SimulationSettings(time_steps=200001)
        runtime = self.service.load('process', 'examples/process_example.cproc').runtime
        timing = process_timing(runtime, time_steps=250001)
        self.assertEqual(timing['time_steps'], 250001)
        plan = process_sampling_plan(Settings(time_steps=250001), runtime)
        self.assertEqual(sum(row['time_steps'] for row in plan['steps']), 250001)
        with self.assertRaises(ValueError): Settings(time_steps=1.5).validate()
    def test_persistent_fifo_monotonic_and_cancellation_marker(self):
        a = self.store.register_desktop_job('a', summary='A')
        b = self.store.register_desktop_job('b', summary='B')
        self.assertFalse(self.store.acquire_execution('b'))
        self.assertTrue(self.store.acquire_execution('a'))
        a.update(state='running', started_at=_now(), progress={'fraction':.6})
        self.store.update_job(a)
        a['progress']['fraction'] = .2
        self.store.update_job(a)
        self.assertEqual(self.store.snapshot()['jobs'][0]['progress']['fraction'], .6)
        self.store.request_cancel('a')
        self.assertTrue(QueueCancelEvent(self.root, 'a').is_set())
        a.update(state='cancelled', finished_at=_now())
        self.store.update_job(a)
        self.store.release_execution('a')
        self.assertTrue(self.store.acquire_execution('b'))
        self.store.release_execution('b')
        self.assertEqual(ComputeTaskStore(self.root).snapshot()['jobs'][0]['job_id'], 'b')
    def test_bridge_double_click_real_result_persistence_and_stale_progress(self):
        bridge = self.bridge()
        bridge.calculate(); bridge.calculate()
        self.spin(lambda: bool(self.store.snapshot()["jobs"]))
        self.assertEqual(len(self.store.snapshot()['jobs']), 1)
        self.spin(lambda: not bridge.busy)
        self.assertTrue(bridge.hasResult, bridge.status)
        row = self.store.snapshot()['jobs'][0]
        self.assertEqual(row['state'], 'succeeded', row)
        self.assertEqual(row['progress']['fraction'], 1)
        self.assertTrue(row['result']['asset']['run_id'])
        self.assertEqual(row['result']['run_id'], bridge._asset_run_id)
        old = bridge.status
        bridge._on_progress(bridge._job_id - 1, {'fraction': .1})
        self.assertEqual(bridge.status, old)
    def test_cancel_waiting_job_and_repeated_cancel(self):
        scheduler = self.scheduler
        assert scheduler is not None
        gate = self.root / 'worker-gate'
        gate.mkdir()
        first = self.bridge()
        bridge = self.bridge()
        bridge._values['grid_size'] = '23'
        with scheduler.gated_workers(gate):
            try:
                first.calculate()
                self.spin(lambda: (gate / 'started-21').exists())
                occupied = scheduler.scheduler.active
                self.assertIsNotNone(occupied)
                assert occupied is not None
                occupied_id = occupied[0]
                self.assertEqual(scheduler.client().status(occupied_id)['state'], 'running')
                bridge.calculate()
                self.spin(lambda: any(r['job_id'] != occupied_id
                                     for r in self.store.snapshot()['jobs']))
                row = next(r for r in self.store.snapshot()['jobs'] if r['job_id'] != occupied_id)
                self.assertEqual(row['state'], 'queued')
                bridge.cancel(); bridge.cancel()
                self.spin(lambda: not bridge.busy)
                row = scheduler.client().status(row['job_id'])
                self.assertEqual(row['state'], 'cancelled')
                self.assertFalse((gate / 'started-23').exists())
                self.assertFalse(bridge.hasResult)
                self.assertEqual(scheduler.client().status(occupied_id)['state'], 'running')
                (gate / 'release').touch()
                self.spin(lambda: not first.busy)
            finally:
                (gate / 'release').touch()
    def test_queue_qml_reopen_percentage_and_cancel_button(self):
        from PySide6.QtQml import QQmlApplicationEngine
        from coat_sim.ui.compute_queue import ComputeQueueView
        import shiboken6
        from tests.test_scheduler_service import job_spec
        job = self.scheduler.client().submit(job_spec(delay_s=1), job_id='qmljob',
                                            metadata={'summary': 'Qt queue test'})
        self.scheduler.scheduler._commit('transition', 'qmljob', state='running',
                                        progress={'fraction': .425, 'stage': 'integrate'})
        engine = QQmlApplicationEngine()
        from coat_sim.ui.theme_service import ThemeEngineBridge
        from coat_sim.ui.theme_tokens import build_theme_snapshot
        theme = ThemeEngineBridge(build_theme_snapshot('light'), 0, 'light', 'light',
                                  'qa', 'QA', {}, parent=engine)
        engine.rootContext().setContextProperty('Theme', theme)
        view = ComputeQueueView(engine, self.root)
        try:
            view.show(); view.show()
            self.spin(lambda: bool(view.jobs))
            self.assertEqual(view.jobs[0]['progress']['fraction'], .425)
            self.assertEqual(view.jobs[0]['stateLabel'], '计算中')
            view.cancelQueueJob('qmljob')
            self.spin(lambda: self.store.read_job('qmljob')['cancel_requested'])
            self.assertTrue(QueueCancelEvent(self.root, 'qmljob').is_set())
            view.window.close(); APP.processEvents(); view.show()
            self.assertTrue(view.window.isVisible())
        finally:
            view.shutdown()
            view.pool.shutdown(wait=True)
            APP.processEvents()
            shiboken6.delete(engine)

    def test_launcher_shutdown_detaches_main_observer_without_cancelling_job(self):
        from types import SimpleNamespace
        from coat_sim.ui.launcher import LauncherBridge
        bridge = self.bridge()
        bridge.calculate()
        self.spin(lambda: bool(self.store.snapshot()['jobs']))
        job = self.store.snapshot()['jobs'][0]
        window = Mock()
        handle = SimpleNamespace(key='main', bridge=bridge, window=window,
                                 context=None, component=None, title_bridge=None, engine=None)
        dummy = SimpleNamespace(_windows={'main-instance': handle}, _shutdown_timer=Mock(),
                                _window_animations={}, _finish_open_request=Mock(),
                                engine=None, windowsChanged=Mock())
        dummy._forget_window = lambda identity: LauncherBridge._forget_window(dummy, identity)
        # The real visibility/registry teardown owns observer detachment.
        window.close.side_effect = lambda: LauncherBridge._window_visibility_changed(dummy, 'main-instance', False)
        with patch('coat_sim.ui.launcher.QTimer.singleShot') as quit_later:
            LauncherBridge._finish_shutdown(dummy)
        dummy._shutdown_timer.stop.assert_called_once()
        quit_later.assert_called_once()
        self.assertFalse(bridge.busy)
        self.assertEqual(dummy._windows, {})
        self.assertTrue(bridge._observers_closed)
        self.spin(lambda: self.store.read_job(job['job_id'])['state'] in ('succeeded', 'failed'))
        row = self.store.read_job(job['job_id'])
        self.assertEqual(row['state'], 'succeeded', row)
        self.assertFalse(row['cancel_requested'])

    def test_default_cycle_threshold_and_progress_monotonic(self):
        bridge = MainBridge(self.service)
        self.assertTrue(bridge.fastCycle)
        self.assertEqual(bridge.fastThresholdPercent, 100)
        bridge._busy = True; bridge._job_id = 5
        bridge._job_cancel_events[5] = threading.Event()
        bridge._on_progress(5, {'fraction':.7, 'step_index':1, 'step_count':2})
        bridge._on_progress(5, {'fraction':.3})
        self.assertEqual(bridge.progressValue, .7)
        bridge._busy = False

if __name__ == '__main__': unittest.main()
