"""Bounded live-worker regressions for enqueue isolation and FIFO identity."""
from collections.abc import Callable
from pathlib import Path
import time
from typing import cast
import unittest
from unittest.mock import patch

from coat_sim.core.compute_tasks import ComputeTaskStore
from coat_sim.core.desktop_compute import QueueCancelEvent
from coat_sim.ui.main import MainBridge
from tests import test_desktop_compute_queue as support
from tests.scheduler_queue_support import SchedulerHarness


class QueueSubmissionTests(unittest.TestCase):
    root: Path
    store: ComputeTaskStore
    scheduler: SchedulerHarness

    setUp = support.QueueTests.setUp
    tearDown = support.QueueTests.tearDown

    def spin(self, predicate: Callable[[], bool], timeout: float = 12) -> None:
        support.QueueTests.spin(cast(support.QueueTests, self), predicate, timeout)

    def bridge(self) -> MainBridge:
        return support.QueueTests.bridge(cast(support.QueueTests, self))

    def test_later_insert_with_older_clock_does_not_revoke_running_lease(self):
        with patch('coat_sim.core.compute_tasks._now', return_value='2026-10-05T10:00:00+00:00'):
            a = self.store.register_desktop_job('a', summary='A')
        self.assertTrue(self.store.acquire_execution('a'))
        a['state'] = 'running'; self.store.update_job(a)
        with patch('coat_sim.core.compute_tasks._now', return_value='2026-10-05T09:59:00+00:00'):
            self.store.register_desktop_job('b', summary='B, later insertion / earlier clock')
        self.assertTrue(self.store.acquire_execution('a'), 'enqueue must not revoke A heartbeat')
        self.assertFalse(self.store.acquire_execution('b'))
        self.assertEqual([r['job_id'] for r in self.store.snapshot()['jobs']], ['a', 'b'])
        a['state'] = 'succeeded'; self.store.update_job(a)
        self.store.release_execution('a')
        self.assertTrue(self.store.acquire_execution('b'))
        self.store.release_execution('b')

    def test_unregistered_or_terminal_job_cannot_acquire(self):
        self.assertFalse(self.store.acquire_execution('missing'))
        a = self.store.register_desktop_job('a', summary='A')
        a['state'] = 'failed'; self.store.update_job(a)
        self.assertFalse(self.store.acquire_execution('a'))

    def test_live_child_enqueue_other_window_and_cancel_waiter_preserves_a(self):
        a, b, c = self.bridge(), self.bridge(), self.bridge()
        b._values['grid_size'] = '23'
        c._values['grid_size'] = '25'
        gate = self.root / 'gate'; gate.mkdir()
        with self.scheduler.gated_workers(gate):
            try:
                a.calculate()
                a_id = a._job_cancel_events[a._job_id].job_id
                self.spin(lambda: (gate / 'started-21').exists(), timeout=10)
                a_row = self.store.read_job(a_id)
                older_clock = "2000-01-01T00:00:00+00:00"
                # Admission runs off the Qt thread. Keep the scheduler's clock
                # patched until B's exact durable job exists, before enqueueing C.
                with patch("coat_sim.scheduler.store._now", return_value=older_clock):
                    b.calculate()
                    b_id = b._job_cancel_events[b._job_id].job_id
                    self.spin(lambda: any(row['job_id'] == b_id
                                          for row in self.store.snapshot()['jobs']))
                    b_row = self.store.read_job(b_id)
                    self.assertEqual(b_row['created_at'], older_clock)
                    self.assertLess(b_row['created_at'], a_row['created_at'])
                c.calculate()
                c_id = c._job_cancel_events[c._job_id].job_id
                self.spin(lambda: any(row['job_id'] == c_id
                                      for row in self.store.snapshot()['jobs']))
                rows = self.store.snapshot()['jobs']
                self.assertEqual(len({a_id, b_id, c_id}), 3)
                self.assertEqual([r['job_id'] for r in rows], [a_id, b_id, c_id])
                self.assertEqual([r['state'] for r in rows], ['running', 'queued', 'queued'])
                self.scheduler.client().cancel(b_row['job_id'])
                self.spin(lambda: not b.busy)
                self.assertFalse(QueueCancelEvent(self.root, a_row['job_id']).is_set())
                self.assertFalse((gate / 'started-23').exists())
                self.assertFalse((gate / 'started-25').exists())
                # Cross a real five-second lease heartbeat while B is added/cancelled.
                deadline = time.monotonic() + 5.3
                while time.monotonic() < deadline:
                    from tests.test_desktop_compute_queue import APP
                    self.scheduler.tick(); APP.processEvents(); time.sleep(.02)
                self.assertEqual(self.store.read_job(a_row['job_id'])['state'], 'running')
                self.assertEqual(self.store.read_job(c_id)['state'], 'queued')
                self.assertFalse((gate / 'started-23').exists())
                self.assertFalse((gate / 'started-25').exists())
            finally:
                (gate / 'release').touch()
                self.spin(lambda: not a.busy and not b.busy and not c.busy, timeout=25)
        rows = {r['job_id']: r for r in self.store.snapshot()['jobs']}
        self.assertEqual(rows[a_row['job_id']]['state'], 'succeeded', rows)
        self.assertEqual(rows[b_row['job_id']]['state'], 'cancelled', rows)
        self.assertEqual(rows[c_id]['state'], 'succeeded', rows)
        self.assertEqual(c._asset_run_id, rows[c_id]['result']['run_id'])
        self.assertNotEqual(rows[a_id]['result']['run_id'], rows[c_id]['result']['run_id'])
        self.assertTrue(c.hasResult, c.status)
        self.assertTrue((gate / 'started-25').exists())


    def test_failed_live_child_does_not_poison_already_queued_successor(self):
        a, b = self.bridge(), self.bridge()
        b._values['grid_size'] = '23'
        gate = self.root / 'gate'; gate.mkdir()
        with self.scheduler.gated_workers(gate, crash_grid=21):
            try:
                a.calculate()
                a_id = a._job_cancel_events[a._job_id].job_id
                self.spin(lambda: (gate / 'started-21').exists(), timeout=10)
                b.calculate()
                b_id = b._job_cancel_events[b._job_id].job_id
                self.spin(lambda: any(row['job_id'] == b_id
                                      for row in self.store.snapshot()['jobs']))
                self.assertNotEqual(a_id, b_id)
                self.assertEqual(self.store.read_job(a_id)['state'], 'running')
                self.assertEqual(self.store.read_job(b_id)['state'], 'queued')
                self.assertFalse((gate / 'started-23').exists())
            finally:
                (gate / 'release').touch()
                self.spin(lambda: not a.busy and not b.busy, timeout=25)
        failed = self.store.read_job(a_id)
        self.assertEqual(failed['state'], 'failed', failed)
        self.assertEqual(failed['error_code'], 'worker_crashed')
        self.assertEqual(failed['exit_code'], 37)
        self.assertTrue(b.hasResult, b.status)
        self.assertEqual(self.store.read_job(b_id)['state'], 'succeeded')
        self.assertTrue((gate / 'started-23').exists())
        self.assertEqual(sum(row['state'] == 'succeeded' for row in self.store.snapshot()['jobs']), 1)
