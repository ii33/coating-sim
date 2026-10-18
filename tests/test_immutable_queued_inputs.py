"""UI jobs freeze fresh contents and remain independent from later draft edits."""
import json
import threading
from unittest.mock import patch
from tests import test_desktop_compute_queue as queue_tests


class ImmutableQueueTests(queue_tests.QueueTests):
    def test_same_window_two_jobs_refresh_same_filename(self):
        bridge = self.bridge()
        gate = self.root / 'gate'; gate.mkdir()
        with self.scheduler.gated_workers(gate):
            self.addCleanup(lambda: (gate / 'release').touch() if gate.exists() else None)
            bridge.calculate()
            self.spin(lambda: (gate / 'started-21').exists())
            path = self.root / 'examples/process_example.cproc'
            recipe = json.loads(path.read_text())
            before = recipe['steps'][0]['target_nm']
            recipe['steps'][0]['target_nm'] = before * 2
            recipe['name'] = 'Same file updated after enqueue'
            path.write_text(json.dumps(recipe))
            bridge.setField('carrier_phase_deg', '17')
            bridge.calculate()
            latest_queue_id = bridge._job_cancel_events[bridge._job_id].job_id
            self.spin(lambda: any(row['job_id'] == latest_queue_id
                                  for row in self.store.snapshot()['jobs']))
            rows = self.store.snapshot()['jobs']
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(set(row['input_summary']['snapshot_sha256'] for row in rows)), 2)
            self.assertFalse((self.root / 'data/tmp/simulation-inputs').exists())
            (gate / 'release').touch()
            self.spin(lambda: all(row['state'] in ('succeeded', 'failed') for row in self.store.snapshot()['jobs']))
            self.spin(lambda: not bridge.busy)
        rows = self.store.snapshot()['jobs']
        self.assertTrue(all(row['state'] == 'succeeded' for row in rows), rows)
        summaries = [row['input_summary']['process'] for row in rows]
        self.assertEqual({row['steps'][0]['target_nm'] for row in summaries}, {before, before * 2})
        # Read each independently executed worker's verified result bundle,
        # not just the queue's presentation summary of the submitted inputs.
        executed_targets = []
        for row in rows:
            package = self.scheduler.client().result_asset_path(row['job_id'])
            loaded = self.service.load_simulation_asset(package)
            executed_targets.append(self.service.load('process', loaded['input_snapshot']).data['steps'][0]['target_nm'])
        self.assertEqual(set(executed_targets), {before, before * 2})
        self.assertTrue(bridge.hasResult, bridge.status)
        self.assertEqual(bridge._export_inputs['process']['name'], recipe['name'])

    def test_cancel_latest_preserves_older_job_lifecycle(self):
        bridge = self.bridge()
        gate = self.root / 'gate'; gate.mkdir()
        with self.scheduler.gated_workers(gate):
            self.addCleanup(lambda: (gate / 'release').touch() if gate.exists() else None)
            bridge.calculate()
            self.spin(lambda: (gate / 'started-21').exists())
            older_id = bridge._job_id
            older = bridge._job_cancel_events[bridge._job_id]
            older_queue_id = older.job_id

            # A durable terminal state precedes observer completion and Qt's
            # queued resultReady delivery. Hold that boundary deliberately:
            # polling the store alone must not imply bridge cleanup has run.
            from coat_sim.ui.main import observe_job
            terminal_observed = threading.Event()
            observer_release = threading.Event()
            self.addCleanup(observer_release.set)
            def paused_terminal_observer(client, job_id, *args, **kwargs):
                try:
                    return observe_job(client, job_id, *args, **kwargs)
                except InterruptedError:
                    if job_id != older_queue_id:
                        terminal_observed.set()
                        if not observer_release.wait(12):
                            raise TimeoutError('test terminal observer gate expired')
                    raise

            with patch('coat_sim.ui.main.observe_job', side_effect=paused_terminal_observer):
                bridge.setField('carrier_phase_deg', '19')
                bridge.calculate()
                latest_id = bridge._job_id
                latest = bridge._job_cancel_events[latest_id]
                self.spin(lambda: any(row['job_id'] == latest.job_id
                                      for row in self.store.snapshot()['jobs']))
                latest.set()  # Queue cancellation of only newest job.
                self.spin(lambda: self.store.read_job(latest.job_id)['state'] == 'cancelled')
                self.spin(terminal_observed.is_set)
                self.assertTrue(bridge.busy)
                self.assertEqual(set(bridge._job_cancel_events), {older_id, latest_id})
                self.assertFalse(older.is_set())
                self.assertFalse(self.store.read_job(older_queue_id)['cancel_requested'])
                observer_release.set()
                self.spin(lambda: latest_id not in bridge._job_cancel_events)

            self.assertTrue(bridge.busy)
            self.assertEqual(len(bridge._job_cancel_events), 1)
            self.assertEqual(bridge._job_cancel_events, {older_id: older})
            self.assertEqual(set(bridge._queued_keys), {older_id})
            self.assertIsNone(bridge._cancel_event)
            self.assertIsNone(bridge._pending_export_inputs)
            self.assertIn('计算已取消', bridge.status)
            self.assertFalse(self.store.read_job(older_queue_id)['cancel_requested'])
            bridge.cancel()  # Explicit cancel targets every remaining window job.
            self.assertTrue(older.is_set())
            (gate / 'release').touch()
            self.spin(lambda: not bridge.busy)
        self.assertFalse(bridge._job_cancel_events)
        self.assertFalse(bridge._queued_keys)
        self.assertTrue(all(row['state'] == 'cancelled' for row in self.store.snapshot()['jobs']))

    def test_older_success_after_latest_cancel_cleans_up_without_replacing_latest_state(self):
        bridge = self.bridge()
        gate = self.root / 'gate'; gate.mkdir()
        with self.scheduler.gated_workers(gate):
            self.addCleanup(lambda: (gate / 'release').touch() if gate.exists() else None)
            bridge.calculate()
            self.spin(lambda: (gate / 'started-21').exists())
            older_id = bridge._job_id
            older = bridge._job_cancel_events[older_id]
            bridge.setField('carrier_phase_deg', '19')
            bridge.calculate()
            latest_id = bridge._job_id
            latest = bridge._job_cancel_events[latest_id]
            self.spin(lambda: any(row['job_id'] == latest.job_id
                                  for row in self.store.snapshot()['jobs']))
            latest.set()
            self.spin(lambda: latest_id not in bridge._job_cancel_events)
            self.assertEqual(self.store.read_job(latest.job_id)['state'], 'cancelled')
            self.assertEqual(bridge._job_cancel_events, {older_id: older})
            self.assertFalse(older.is_set())
            self.assertFalse(self.store.read_job(older.job_id)['cancel_requested'])
            self.assertTrue(bridge.busy)
            self.assertFalse(bridge.hasResult)
            self.assertIsNone(bridge._pending_export_inputs)
            latest_state = (bridge.status, bridge.progressValue, bridge.hasResult,
                            bridge._asset_run_id, bridge._export_inputs,
                            bridge._uniformity_result_notice)
            (gate / 'release').touch()
            self.spin(lambda: not bridge.busy)

        self.assertEqual(self.store.read_job(older.job_id)['state'], 'succeeded')
        self.assertFalse(self.store.read_job(older.job_id)['cancel_requested'])
        self.assertFalse(bridge._job_cancel_events)
        self.assertFalse(bridge._queued_keys)
        self.assertIsNone(bridge._cancel_event)
        self.assertIsNone(bridge._pending_export_inputs)
        self.assertEqual((bridge.status, bridge.progressValue, bridge.hasResult,
                          bridge._asset_run_id, bridge._export_inputs,
                          bridge._uniformity_result_notice), latest_state)
