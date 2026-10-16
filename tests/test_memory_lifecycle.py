"""Bounded transport and worker lifetime tests; all persisted data is temporary."""
import gc
import hashlib
import json
import os
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import weakref
import zipfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication
from coat_sim.core.api import CoatService
from coat_sim.core import simulation_assets as assets
from coat_sim.core.simulator import Settings, simulate
from coat_sim.ui.main import MainBridge, ThicknessImageProvider, _detach_error_tracebacks
from tests.design_test_fixtures import copy_typed_examples

APP = QApplication.instance() or QApplication([])


class MemoryLifecycleTests(unittest.TestCase):
    def test_byte_colormap_preserves_texture_and_releases_input(self):
        from matplotlib import colormaps
        provider = ThicknessImageProvider()
        for constant in (False, True):
            field = np.ones((31, 31)) if constant else np.linspace(1, 2, 31**2).reshape(31, 31)
            field[0, 0] = np.nan
            finite = np.isfinite(field)
            low, high = float(np.min(field[finite])), float(np.max(field[finite]))
            scale = np.zeros_like(field) if high == low else (field-low)/(high-low)
            scale[~finite] = 0
            expected = np.ascontiguousarray(np.flipud(np.asarray(colormaps['turbo'](np.clip(scale, 0, 1))*255, dtype=np.uint8)))
            expected[:, :, 3] = np.flipud(finite).astype(np.uint8)*255
            self.assertEqual(provider.set_field(field), (low, high))
            image = provider._image
            actual = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(31, 31, 4)
            np.testing.assert_array_equal(actual, expected)
            reference = weakref.ref(field)
            del field
            self.assertIsNone(reference())

    def test_signature_matches_previous_bytes_for_noncontiguous_array(self):
        result = {'field': np.arange(48, dtype=float).reshape(6, 8)[:, ::2],
                  'x': np.arange(4.), 'y': np.arange(6.)}
        metadata = {'run': 'fixture'}
        digest = hashlib.sha256(json.dumps(assets.plain(metadata), ensure_ascii=False, sort_keys=True, allow_nan=False).encode())
        for key in ('field', 'x', 'y'):
            value = result[key]
            digest.update(key.encode())
            digest.update(str((value.dtype.str, value.shape)).encode())
            digest.update(np.ascontiguousarray(value).tobytes())
        self.assertEqual(assets._input_signature(metadata, result), digest.hexdigest())

    def package(self, root):
        result = {'field': np.arange(1024, dtype=float).reshape(32, 32),
                  'x': np.arange(32.), 'y': np.arange(32.)}
        with patch('coat_sim.core.result_visualization.render_result_visualizations', return_value={'files': []}):
            receipt = assets.prepare_simulation_asset(root, result=result, settings={}, machine=None,
                process=None, elapsed_s=1, requested_device='CPU')
        saved = assets.save_simulation_asset(root, receipt['run_id'], 'memory fixture', confirmed=True)
        return saved['asset_path'], result

    def test_asset_streamed_owned_arrays_and_repeated_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path, expected = self.package(folder)
            original_read = zipfile.ZipFile.read
            def guarded_read(archive, name, *args, **kwargs):
                self.assertNotEqual(name, 'result.npz', 'Nested NPZ must not be materialized as bytes')
                return original_read(archive, name, *args, **kwargs)
            with patch.object(zipfile.ZipFile, 'read', guarded_read):
                for _ in range(2):
                    loaded = assets.read_simulation_asset(path, include_arrays=True)
                    for key, value in expected.items():
                        np.testing.assert_array_equal(loaded['arrays'][key], value)
                        self.assertNotIsInstance(loaded['arrays'][key], np.memmap)
                        self.assertTrue(loaded['arrays'][key].flags.writeable)
                    reference = weakref.ref(loaded['arrays']['field'])
                    del loaded
                    self.assertIsNone(reference())

    def test_asset_spool_closed_on_load_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path, _ = self.package(folder)
            opened = []
            factory = tempfile.TemporaryFile
            def tracked(*args, **kwargs):
                stream = factory(*args, **kwargs)
                opened.append(stream)
                return stream
            with patch.object(assets.tempfile, 'TemporaryFile', tracked), patch.object(assets.np, 'load', side_effect=ValueError('injected failure')):
                with self.assertRaisesRegex(ValueError, 'injected failure'):
                    assets.read_simulation_asset(path, include_arrays=True)
            self.assertTrue(opened)
            self.assertTrue(all(stream.closed for stream in opened))

    def test_cancel_and_failure_trace_arrays_released_without_gc(self):
        class Cancel:
            count = 0
            def is_set(self):
                self.count += 1
                return self.count > 3
        def failed(cancel):
            try:
                if cancel:
                    simulate(Settings(grid_size=51, time_steps=64), backend='CPU', cancel_event=Cancel())
                else:
                    def fail_backend(*args, **kwargs):
                        raise RuntimeError('injected failure')
                    with patch('coat_sim.core.simulator._blocked', new=fail_backend):
                        simulate(Settings(grid_size=51, time_steps=64), backend='CPU',
                            mask={'z_mm': 1, 'outer': [[-1,-1],[1,-1],[1,1]]})
            except Exception as error:
                return error
            raise AssertionError('Expected injected interruption')
        def trace_arrays(error):
            references = []
            pending, seen = [error], set()
            while pending:
                current = pending.pop()
                if id(current) in seen: continue
                seen.add(id(current))
                pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
                trace = current.__traceback__
                while trace:
                    references.extend(weakref.ref(value) for value in trace.tb_frame.f_locals.values() if isinstance(value, np.ndarray))
                    trace = trace.tb_next
            return references
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            for cancel in (False, True):
                error = failed(cancel)
                self.assertIsInstance(error, InterruptedError if cancel else RuntimeError)
                references = trace_arrays(error)
                self.assertTrue(references)
                self.assertTrue(any(reference() is not None for reference in references))
                text = str(error)
                error._coat_summary_warning = 'keep warning'
                _detach_error_tracebacks(error)
                self.assertEqual(str(error), text)
                self.assertEqual(error._coat_summary_warning, 'keep warning')
                self.assertIsNone(error.__traceback__)
                self.assertTrue(all(reference() is None for reference in references))
        finally:
            if was_enabled: gc.enable()

    def test_worker_queue_releases_failure_and_cancel_payloads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            copy_typed_examples(root)
            with patch('coat_sim.core.data_files.PROJECT_ROOT', root), patch('coat_sim.core.data_files.DATA_ROOT', root / 'data'):
                service = CoatService(root)
                from tests.scheduler_queue_support import SchedulerHarness
                harness = SchedulerHarness(root)
                # MainBridge starts ComputeTarget's capability probe in its
                # constructor. Patch its client before construction so no
                # background probe can start a real daemon for this fixture.
                client_patch = patch('coat_sim.ui.main.SchedulerClient', side_effect=harness.client)
                client_patch.start()
                self.addCleanup(client_patch.stop)
                bridge = MainBridge(service)
                self.assertIs(bridge._compute_target._local_client.transport, harness)
                bridge.chooseFile('process', QUrl.fromLocalFile(str(root / 'examples/process_example.cproc')))
                bridge.setDevice('CPU')
                bridge._values.update(grid_size='21', time_steps='12')
                bridge._time_basis = 'time_steps'
                bridge._fast_cycle = False
                received = []
                bridge.resultReady.connect(lambda job, result, elapsed, error: received.append((job, error)))
                def wait_until_idle(timeout=15):
                    deadline = time.monotonic() + timeout
                    while bridge.busy and time.monotonic() < deadline:
                        harness.tick()
                        APP.processEvents()
                        time.sleep(.005)
                    APP.processEvents()
                    self.assertFalse(bridge.busy, bridge.status)

                was_enabled = gc.isenabled()
                gc.disable()
                try:
                    with harness.resource_failure_workers('memory') as processes:
                        bridge.calculate()
                        failed_job_id = bridge._cancel_event.job_id
                        wait_until_idle()
                        self.assertTrue(processes)
                        self.assertEqual(received[-1][0], bridge._job_id)
                        failure_error = received[-1][1]
                        failed_row = harness.client().status(failed_job_id)
                        fixture = json.loads((root / 'artifacts/runtime/scheduler/jobs' /
                                              failed_job_id / 'resource-failure-fixture.json').read_text())
                        self.assertEqual(failed_row['state'], 'failed', failed_row)
                        self.assertEqual(failed_row['error_code'], 'algorithm_failed', failed_row)
                        self.assertIn('bounded scheduler allocation failure', str(failure_error))
                        self.assertEqual(fixture['failure'], 'memory')
                        self.assertEqual((fixture['grid_size'], fixture['time_steps']), (21, 12))
                        self.assertEqual((fixture['backend'], fixture['algorithm']), ('CPU', 'fast'))
                        self.assertFalse(fixture['algorithm_entered'])
                        self.assertIsInstance(failure_error, RuntimeError)
                        self.assertIsNone(failure_error.__traceback__)
                        self.assertIsNone(bridge._pending_export_inputs)
                        self.assertIsNone(bridge._cancel_event)
                        self.assertFalse(bridge._job_cancel_events)

                    gate = root / 'cancel-gate'
                    gate.mkdir()
                    with harness.gated_workers(gate):
                        bridge.calculate()
                        cancelled_job_id = bridge._cancel_event.job_id
                        gate_deadline = time.monotonic() + 10
                        while not (gate / 'started-21').exists() and time.monotonic() < gate_deadline:
                            harness.tick()
                            APP.processEvents()
                            time.sleep(.005)
                        self.assertTrue((gate / 'started-21').exists(), 'bounded worker did not reach its gate')
                        self.assertEqual(harness.client().status(cancelled_job_id)['state'], 'running')
                        bridge.cancel()
                        self.assertTrue(bridge._cancel_event.is_set())
                        self.assertTrue(harness.client().status(cancelled_job_id)['cancel_requested'])
                        wait_until_idle()
                        cancelled_row = harness.client().status(cancelled_job_id)
                        cancel_error = received[-1][1]
                        self.assertEqual(cancelled_row['state'], 'cancelled', cancelled_row)
                        self.assertEqual(received[-1][0], bridge._job_id)
                        self.assertIsInstance(cancel_error, InterruptedError)
                        self.assertIsNone(cancel_error.__traceback__)
                        self.assertIsNone(bridge._pending_export_inputs)
                        self.assertIsNone(bridge._cancel_event)
                        self.assertFalse(bridge._job_cancel_events)
                finally:
                    harness.close()
                    if was_enabled: gc.enable()


if __name__ == '__main__': unittest.main()
