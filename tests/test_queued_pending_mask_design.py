"""A real queued small mask search returns a new unsaved design."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import time
from pathlib import Path
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication
from tests.test_process_optimization_modes import fixture, options
from coat_sim.ui.process import ProcessBridge
from coat_sim.core.compute_tasks import ComputeTaskStore

APP = QApplication.instance() or QApplication([])

class QueuedPendingMaskTests(unittest.TestCase):
    def test_pending_mask_without_fake_machine_substrate_files(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as cleanup:
            root = Path(tmp)
            from coat_sim.scheduler import SchedulerClient
            cleanup.callback(lambda: SchedulerClient(root).stop())
            root_patch = patch("coat_sim.core.data_files.DATA_ROOT", root / "data")
            project_patch = patch("coat_sim.core.data_files.PROJECT_ROOT", root)
            project_patch.start()
            self.addCleanup(project_patch.stop)
            root_patch.start()
            self.addCleanup(root_patch.stop)
            service, path = fixture(root, mask_count=0, source_count=1)
            initial = {p.name: p.read_bytes() for p in root.iterdir()
                       if p.suffix in ('.csrc', '.csub', '.cmac', '.cproc', '.cmsk')}
            self.assertTrue(initial, 'input preservation must inspect real typed fixture files')
            bridge = ProcessBridge(service, str(path))
            bridge.startOptimization({'grid_size':21, 'time_steps':80, 'carrier_rpm':1.}, options())
            self.assertTrue(bridge.optimization['busy'], bridge.optimization)
            bridge.setField('name', 'Edited after submission')
            deadline = time.monotonic() + 20
            while bridge.optimization['busy'] and time.monotonic() < deadline:
                APP.processEvents(); time.sleep(.01)
            self.assertFalse(bridge.optimization['busy'], bridge.optimization)
            self.assertEqual(len(bridge.pendingMasks), 1, bridge.optimization)
            self.assertEqual(bridge.document['name'], 'Edited after submission')
            self.assertEqual({name: (root / name).read_bytes() for name in initial}, initial)
            self.assertFalse((root/'data/tmp/optimization-inputs').exists())
            self.assertEqual(list((root/'data/tmp').rglob('machine*.json')), [])
            self.assertEqual(list((root/'data/tmp').rglob('substrate*.json')), [])
            row = ComputeTaskStore(root).snapshot()['jobs'][0]
            self.assertEqual(row['state'], 'succeeded', row)
            self.assertEqual(row['kind'], 'mask_optimization')
            self.assertEqual(row['input_summary']['process']['name'], 'Test')
            self.assertTrue(row['result']['pending_masks'])
            package = row['result']['asset']['temporary_bundle_path']
            loaded = service.load_simulation_asset(package)
            retained = loaded['input_snapshot']['parameters']['options']
            self.assertEqual(retained['mode'], 'auto_design')
            self.assertEqual(retained['max_evaluations'], options()['max_evaluations'])
            self.assertEqual(retained['mask_bounds'], options()['mask_bounds'])
            target = root/'new-mask.cmsk'
            bridge.saveOptimizedMask(QUrl.fromLocalFile(str(target)), 0, False)
            self.assertTrue(target.exists())
            seen = []
            bridge.maskOverwriteRequested.connect(lambda url, index: seen.append(index))
            bridge.saveOptimizedMask(QUrl.fromLocalFile(str(target)), 0, False)
            self.assertEqual(seen, [0])
