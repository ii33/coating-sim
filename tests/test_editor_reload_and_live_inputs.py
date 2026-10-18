"""Atomic file refresh and actual QML draft isolation from queued progress."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QUICK_BACKEND', 'software')
from copy import deepcopy
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import patch, Mock
from PySide6.QtCore import QObject, QUrl, Qt, QMetaObject, QCoreApplication, QEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from coat_sim.core.api import CoatService
from coat_sim.core.design_files import design_extension
from coat_sim.ui.main import MainBridge
from coat_sim.ui.source_substrate import SourceBridge, SubstrateBridge
from tests.design_test_fixtures import copy_typed_examples
from tests.scheduler_queue_support import SchedulerHarness

ROOT = Path(__file__).resolve().parents[1]
APP = QApplication.instance() or QApplication([])

class ReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(ROOT / 'examples', self.root / 'examples')
        self.typed_examples = copy_typed_examples(self.root)
        for name, value in [('PROJECT_ROOT', self.root), ('DATA_ROOT', self.root / 'data')]:
            p = patch('coat_sim.core.data_files.' + name, value)
            p.start(); self.addCleanup(p.stop)
        self.service = CoatService(self.root, restrict_to_root=False)
        self.engines = []
        self.bridges = []
        self.harness = SchedulerHarness(self.root)
        self.addCleanup(self.harness.close)
        # MainBridge starts its capability probe during construction, before
        # calculate(). Keep both discovery and submissions on the owned peer.
        client_patch = patch('coat_sim.ui.main.SchedulerClient', side_effect=self.harness.client)
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def tearDown(self):
        for bridge in self.bridges:
            if isinstance(bridge, MainBridge):
                # A running capability probe must finish before its peer closes.
                bridge._compute_target._poll.stop()
                bridge._compute_target._executor.shutdown(wait=True, cancel_futures=True)
        for engine in self.engines:
            for window in engine.rootObjects():
                window.hide()
            engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        for bridge in self.bridges: bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        APP.processEvents()

    def bridge(self, cls):
        b = cls(self.service); self.bridges.append(b); return b

    def window(self, bridge, name):
        engine = QQmlApplicationEngine(); self.engines.append(engine)
        errors = []
        engine.warnings.connect(lambda rows: errors.extend(str(row) for row in rows))
        engine.rootContext().setContextProperty('bridge', bridge)
        if isinstance(bridge, MainBridge): engine.addImageProvider('thickness', bridge.image_provider)
        engine.load(QUrl.fromLocalFile(str(ROOT / 'coat_sim/ui/qml' / name)))
        self.assertTrue(engine.rootObjects(), errors)
        window = engine.rootObjects()[0]
        window.showNormal(); window.resize(1250, 1000)
        QTest.qWait(30)
        return window

    def test_source_substrate_reload_origin_cancel_and_atomic_errors(self):
        for cls in [SourceBridge, SubstrateBridge]:
            with self.subTest(kind=cls.kind):
                b = self.bridge(cls)
                self.assertFalse(b.canReload)
                path = self.root / (cls.kind + design_extension(cls.kind))
                self.service.save(cls.kind, path, b.documentData())
                b.applyDocument(b.documentData(), str(path))
                self.assertFalse(b.canReload, 'intended draft path is not a loaded origin')
                if isinstance(b, SourceBridge): b._dirty = False
                self.assertTrue(b.openPath(str(path)))
                self.assertTrue(b.canReload)
                changed = json.loads(path.read_text()); changed['description'] = 'external update'
                path.write_text(json.dumps(changed))
                b.setField('description', 'unsaved draft')
                calls = []; b.reloadRequested.connect(lambda: calls.append(True))
                self.assertFalse(b.reloadCurrent()); self.assertEqual(len(calls), 1)
                self.assertFalse(b.reloadCurrent()); self.assertEqual(len(calls), 1)
                self.assertFalse(b.confirmReload(False)); self.assertEqual(b.description, 'unsaved draft')
                self.assertTrue(b.dirty)
                b.reloadCurrent(); b.setField('description', 'newer draft')
                self.assertFalse(b.confirmReload(True)); self.assertEqual(b.description, 'newer draft')
                b.reloadCurrent(); self.assertTrue(b.confirmReload(True))
                self.assertEqual(b.description, 'external update'); self.assertFalse(b.dirty)
                b.setField('description', 'retained')
                before = deepcopy(b.documentData())
                for content in ['{broken', '{"schema_version": 9999}']:
                    path.write_text(content)
                    b.reloadCurrent(); self.assertFalse(b.confirmReload(True))
                    self.assertEqual(b.documentData(), before); self.assertTrue(b.dirty)
                path.unlink()
                b.reloadCurrent(); self.assertFalse(b.confirmReload(True))
                self.assertEqual(b.documentData(), before)

    def test_source_substrate_rendered_reload_cancel(self):
        for cls, page in [(SourceBridge, 'SourceWindow.qml'), (SubstrateBridge, 'SubstrateWindow.qml')]:
            b = self.bridge(cls)
            path = self.root / (cls.kind + design_extension(cls.kind))
            self.service.save(cls.kind, path, b.documentData()); b.openPath(str(path))
            window = self.window(b, page)
            button = window.findChild(QObject, 'reloadCurrentFile')
            self.assertTrue(button.property('enabled'))
            b.setField('description', 'draft')
            self.assertTrue(QMetaObject.invokeMethod(button, 'clicked'))
            APP.processEvents()
            dialog = window.findChild(QObject, 'reloadConfirmDialog')
            self.assertTrue(dialog.property('visible'))
            self.assertTrue(QMetaObject.invokeMethod(dialog, 'reject'))
            self.assertEqual(b.description, 'draft'); self.assertTrue(b.dirty)
            window.grabWindow().save("/tmp/deposition-" + cls.kind + "-reload.png")

    def main(self):
        b = self.bridge(MainBridge)
        b.chooseFile('process', QUrl.fromLocalFile(str(self.typed_examples / 'process_example.cproc')))
        b.setDevice('CPU'); b.setField('grid_size', '21'); b.setField('time_steps', '12')
        b.setFastCycle(False)
        return b

    def test_simulator_refresh_updated_dependencies_and_atomic_failure(self):
        b = self.main()
        path = self.typed_examples / 'machine_example.cmac'
        data = json.loads(path.read_text()); data['carrier_x_mm'] = 99
        path.write_text(json.dumps(data))
        self.assertTrue(b.reloadCurrent())
        self.assertTrue(any('99' in row for row in b.geometryRows))
        before = (b.fields, b.geometryRows, b.paths, b._revision)
        for failure in ['malformed', 'missing', 'dependency']:
            with self.subTest(failure=failure):
                original = path.read_bytes()
                if failure == 'missing': path.unlink()
                elif failure == 'malformed': path.write_text('{broken')
                else:
                    bad = deepcopy(data); bad['sources'][0]['file'] = 'missing.csrc'
                    path.write_text(json.dumps(bad))
                self.assertFalse(b.reloadCurrent())
                self.assertEqual((b.fields, b.geometryRows, b.paths, b._revision), before)
                path.write_bytes(original)

    def test_simulator_asset_refresh_rereads_archive_and_preserves_controls(self):
        from coat_sim.core import simulation_assets as assets
        from tests.test_portable_asset_bundle import fixture_snapshot
        from hashlib import sha256
        snapshot = fixture_snapshot()
        first = assets.save_simulation_input_asset(self.root, name="first", settings={},
            input_snapshot=snapshot, confirmed=True)["asset_path"]
        b = self.bridge(MainBridge)
        b.openAsset(QUrl.fromLocalFile(first))
        self.assertTrue(b.canReload, b.status)
        b.setField("carrier_rpm", "7.25")
        snapshot = fixture_snapshot()
        row = next(row for row in snapshot["files"] if row["kind"] == "machine")
        row["data"]["carrier_x_mm"] = 123
        row["sha256"] = sha256(assets._json_bytes(row["data"])).hexdigest()
        second = assets.save_simulation_input_asset(self.root, name="second", settings={},
            input_snapshot=snapshot, confirmed=True)["asset_path"]
        Path(first).write_bytes(Path(second).read_bytes())
        self.assertTrue(b.reloadCurrent(), b.status)
        self.assertEqual(b.paths["process"], first)
        self.assertEqual(b.fields["carrier_rpm"], "7.25")
        self.assertTrue(any("123" in row for row in b.geometryRows))
        before = (b.fields, b.geometryRows, b._revision)
        Path(first).write_bytes(b"broken archive")
        self.assertFalse(b.reloadCurrent())
        self.assertEqual((b.fields, b.geometryRows, b._revision), before)

    def test_timestep_request_survives_refresh_and_duplicate_submission(self):
        path = self.typed_examples / "process_example.cproc"
        data = json.loads(path.read_text())
        data["steps"] = [{"source_slot": 1, "target_nm": 10888.888888888889,
                          "rate_nm_s": 1, "target_mode": "mean"}]
        path.write_text(json.dumps(data))
        b = self.bridge(MainBridge)
        b.chooseFile("process", QUrl.fromLocalFile(str(path)))
        b.setDevice("CPU")
        self.assertEqual(b.fields["time_steps"], "435556")
        # Display rounding is intentionally not the authoritative request.
        self.assertNotEqual(b.fields["time_step_s"], "0.025")
        with patch("coat_sim.ui.main.threading.Thread") as threads:
            b.calculate()
            original_key = deepcopy(b._queued_keys)
            for _ in range(5):
                b.chooseFile("process", QUrl.fromLocalFile(str(path)))
                b.refreshEstimate()
                self.assertTrue(b.reloadCurrent(), b.status)
                b.calculate()
                self.assertEqual(b.fields["time_steps"], "435556")
                self.assertEqual(b._queued_keys, original_key)
                self.assertEqual(threads.call_count, 1, b.status)
            # Only an explicit new step edit replaces the original request.
            b.setField("time_step_s", "0.05")
            self.assertEqual(b.fields["time_steps"], "217778")
            self.assertTrue(b.reloadCurrent())
            b.calculate()
            self.assertEqual(threads.call_count, 2, b.status)
            for invalid in ["", "-", "nan", "0"]:
                b.setField("time_step_s", invalid)
                b.calculate()
                self.assertFalse(b.reloadCurrent())
                self.assertEqual(threads.call_count, 2)
            b.setField("time_steps", "123")
            for _ in range(3):
                self.assertTrue(b.reloadCurrent(), b.status)
                self.assertEqual(b.fields["time_steps"], "123")
            b.calculate()
            self.assertEqual(threads.call_count, 3)
            b.setField("time_step_s", "0.025")
            b.calculate()
            self.assertEqual(b.fields["time_steps"], "435556")
            self.assertEqual(threads.call_count, 3, "original input remains deduplicated")

    def test_asset_count_basis_replaces_previous_invalid_step_request(self):
        from coat_sim.core import simulation_assets as assets
        from tests.test_portable_asset_bundle import fixture_snapshot
        path = assets.save_simulation_input_asset(self.root, name="sampling", settings={"time_steps": 123},
            input_snapshot=fixture_snapshot(), confirmed=True)["asset_path"]
        b = self.main()
        b.setField("time_step_s", "invalid")
        b.openAsset(QUrl.fromLocalFile(path))
        self.assertEqual(b._time_basis, "time_steps")
        for _ in range(3):
            self.assertTrue(b.reloadCurrent(), b.status)
            self.assertEqual(b.fields["time_steps"], "123")
        b.setField("time_step_s", "0.025")
        self.assertEqual(b.fields["time_steps"], "400")
        for _ in range(3):
            self.assertTrue(b.reloadCurrent())
            self.assertEqual(b.fields["time_steps"], "400")

    def test_all_numeric_qml_drafts_survive_progress_and_refresh_notifications(self):
        b = self.main(); window = self.window(b, "MainWindow.qml")
        for key in ["carrier_rpm", "carrier_phase_deg", "planet_phase_deg", "time_step_s", "time_steps", "grid_size"]:
            field = window.findChild(QObject, "simulationField_" + key)
            self.assertTrue(QMetaObject.invokeMethod(field, "forceActiveFocus"))
            QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
            QTest.keyClick(window, Qt.Key_9)
            for _ in range(3):
                b.changed.emit(); b.inputsChanged.emit(); APP.processEvents()
                self.assertEqual(field.property("text"), "9", key)
        # Capture the real rendered window for visual QA, not a mock screen.
        window.grabWindow().save("/tmp/deposition-live-inputs.png")

    def test_main_capability_probe_uses_owned_scheduler_before_submission(self):
        with patch('coat_sim.scheduler.client.SchedulerClient.ensure_started',
                   side_effect=AssertionError('reload tests must not start a daemon')):
            bridge = self.main()
            target = bridge._compute_target
            self.assertIs(target._local_client.transport, self.harness)
            _, devices, _, phase = target._future.result(timeout=10)
            self.assertEqual(phase, 'local')
            self.assertTrue(any(row['id'] == 'cpu:default' for row in devices))
            self.assertFalse((self.root / 'artifacts/runtime/scheduler/endpoint.json').exists())

    def test_real_qml_typing_survives_progress_refresh_and_second_submission(self):
        b = self.main(); window = self.window(b, 'MainWindow.qml')
        field = window.findChild(QObject, 'simulationField_carrier_rpm')
        self.assertIsNotNone(field)
        from coat_sim.scheduler.jobs import JobSpec

        harness = self.harness
        submissions = []

        class CapturingClient:
            def __init__(self, root):
                self.client = harness.client(root)

            def submit(self, spec, job_id=None, metadata=None):
                receipt = self.client.submit(spec, job_id=job_id, metadata=metadata)
                submissions.append((deepcopy(spec), receipt))
                return receipt

            def __getattr__(self, name):
                return getattr(self.client, name)

        observer_entered = [threading.Event(), threading.Event()]
        observer_release = threading.Event()
        created_threads = []

        def create_thread(*args, **kwargs):
            thread = threading.Thread(*args, **kwargs)
            created_threads.append(thread)
            return thread

        def hold_observation(client, job_id, *args, **kwargs):
            index = next(i for i, (_, receipt) in enumerate(submissions)
                         if receipt['job_id'] == job_id)
            observer_entered[index].set()
            if not observer_release.wait(12):
                raise TimeoutError('test observer gate expired')
            raise RuntimeError('test observer detached; admitted job continues')

        @contextmanager
        def observers():
            try:
                yield
            finally:
                # Finish every real admission/observer thread before Qt object
                # destruction and before the owned scheduler peer is closed.
                b.detach_observers()
                observer_release.set()
                for thread in created_threads:
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())

        threads = Mock(side_effect=create_thread)
        with (observers(),
              patch('coat_sim.ui.main.SchedulerClient', CapturingClient),
              patch('coat_sim.ui.main.observe_job', side_effect=hold_observation),
              patch('coat_sim.ui.main.threading', SimpleNamespace(Thread=threads, Event=threading.Event))):
            b.calculate()
            self.assertTrue(b.busy, b.status)
            self.assertTrue(observer_entered[0].wait(5), b.status)
            self.assertEqual(len(submissions), 1)
            first_spec, first_receipt = submissions[0]
            first_snapshot = deepcopy(first_spec['inputs']['snapshot'])
            first_event = b._cancel_event
            self.assertEqual(first_spec['executor'], 'process_recipe')
            self.assertEqual(first_spec['parameters']['settings']['carrier_rpm'], float(b.fields['carrier_rpm']))
            self.assertEqual(first_receipt['state'], 'queued')
            self.assertEqual(JobSpec.parse_frozen(first_spec).fingerprint, first_receipt['input_digest'])
            self.assertTrue(QMetaObject.invokeMethod(field, 'forceActiveFocus'))
            QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
            QTest.keyClick(window, Qt.Key_Minus)
            for fraction in [.1, .2, .3]:
                b._on_progress(b._job_id, {'fraction': fraction}); APP.processEvents()
                self.assertEqual(field.property('text'), '-')
            QTest.keyClick(window, Qt.Key_8)
            QTest.keyClick(window, Qt.Key_Period)
            b._on_progress(b._job_id, {'fraction': .4}); APP.processEvents()
            self.assertEqual(field.property('text'), '-8.')
            QTest.keyClick(window, Qt.Key_5)
            self.assertEqual(b.fields['carrier_rpm'], '-8.5')
            machine_path = self.typed_examples / 'machine_example.cmac'
            machine = json.loads(machine_path.read_text())
            original_machine_x = machine['carrier_x_mm']
            machine['carrier_x_mm'] = 88
            machine_path.write_text(json.dumps(machine))
            self.assertTrue(b.reloadCurrent())
            self.assertEqual(first_spec['inputs']['snapshot'], first_snapshot)
            self.assertTrue(any('88' in row for row in b.geometryRows))
            self.assertEqual(field.property('text'), '-8.5')
            self.assertFalse(first_event.is_set())
            self.assertFalse(harness.client().status(first_receipt['job_id'])['cancel_requested'])
            b.calculate()
            self.assertTrue(observer_entered[1].wait(5), b.status)
            self.assertEqual(threads.call_count, 2, b.status)
            self.assertEqual(len(submissions), 2)
            second_spec, second_receipt = submissions[1]
            second_event = b._cancel_event
            second_snapshot = second_spec['inputs']['snapshot']
            self.assertEqual(second_receipt['state'], 'queued')
            self.assertEqual(JobSpec.parse_frozen(second_spec).fingerprint, second_receipt['input_digest'])
            self.assertEqual(second_spec['parameters']['settings']['carrier_rpm'], -8.5)
            first_machine = next(row for row in first_snapshot['files'] if row['kind'] == 'machine')
            second_machine = next(row for row in second_snapshot['files'] if row['kind'] == 'machine')
            self.assertNotEqual(first_snapshot['files'], second_snapshot['files'])
            self.assertNotEqual(first_receipt['job_id'], second_receipt['job_id'])
            self.assertEqual(first_machine['data']['carrier_x_mm'], original_machine_x)
            self.assertEqual(second_machine['data']['carrier_x_mm'], 88)
            self.assertEqual(
                next(row for row in first_snapshot['files'] if row['kind'] == 'source')['sha256'],
                next(row for row in second_snapshot['files'] if row['kind'] == 'source')['sha256'])
            self.assertEqual(first_event.job_id, first_receipt['job_id'])
            self.assertEqual(second_event.job_id, second_receipt['job_id'])
            self.assertFalse(first_event.is_set())
            self.assertFalse(second_event.is_set())
            self.assertFalse(harness.client().status(first_receipt['job_id'])['cancel_requested'])
            self.assertFalse(harness.client().status(second_receipt['job_id'])['cancel_requested'])
            b._on_progress(b._job_id, {'fraction': .8}); APP.processEvents()
            self.assertEqual(field.property('text'), '-8.5')
