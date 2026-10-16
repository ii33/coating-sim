"""Independent store-level regressions for insertion FIFO and lease upgrades."""
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from coat_sim.core.compute_tasks import ComputeTaskStore


def _delayed_register(root, ready, release, registered, finish):
    """A separate submitter remains alive while the parent checks its record."""
    ready.set()
    if not release.wait(10):
        raise TimeoutError('registration release timed out')
    with patch('coat_sim.core.compute_tasks._now', return_value='2000-01-01T00:00:00+00:00'):
        ComputeTaskStore(root).register_desktop_job('delayed', summary='old clock')
    registered.set()
    if not finish.wait(10):
        raise TimeoutError('parent verification timed out')


class QueueFifoReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ComputeTaskStore(self.root)

    def _lease(self, owner, expires_at):
        with self.store.connection() as connection:
            connection.execute('UPDATE execution_lease SET owner=?,expires_at=? WHERE singleton=1',
                               (owner, expires_at))

    def _lease_row(self):
        with self.store.connection() as connection:
            return dict(connection.execute('SELECT * FROM execution_lease').fetchone())

    def _legacy_inverted_owner(self):
        """Old timestamp scheduling can run row 2 while row 1 is still queued."""
        with patch('coat_sim.core.compute_tasks._now', return_value='2026-10-05T10:00:00+00:00'):
            a = self.store.register_desktop_job('a', summary='inserted first', estimated_s=20)
        with patch('coat_sim.core.compute_tasks._now', return_value='2026-10-05T09:00:00+00:00'):
            b = self.store.register_desktop_job('b', summary='legacy running owner', estimated_s=10)
        b.update(state='running', progress={'estimated_remaining_s': 10})
        self.store.update_job(b)
        self._lease('b', 2000)
        return a, b

    def test_existing_schema_keeps_rowids_and_accepts_new_fifo(self):
        # Construct only the queue table exactly as shipped before this fix.
        self.store.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.store.path) as connection:
            connection.execute('CREATE TABLE queue_jobs (job_id TEXT PRIMARY KEY, '
                               'payload TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL)')
            for rowid, job_id, created in [(7, 'a', '2026-10-05T10:00:00+00:00'),
                                         (19, 'b', '2000-01-01T00:00:00+00:00')]:
                value = {'job_id': job_id, 'state': 'queued', 'created_at': created,
                         'estimated_s': 10}
                connection.execute('INSERT INTO queue_jobs(rowid,job_id,payload,state,created_at) '
                                   'VALUES(?,?,?,?,?)', (rowid, job_id, json.dumps(value), 'queued', created))
        # sqlite3.Connection's context manager commits/rolls back but does not
        # close the handle; close before TemporaryDirectory removes the DB on Windows.
        connection.close()
        self.assertFalse(self.store.acquire_execution('b'))
        self.assertTrue(self.store.acquire_execution('a'))
        with self.store.connection() as connection:
            self.assertEqual([(r[0], r[1]) for r in connection.execute(
                'SELECT rowid,job_id FROM queue_jobs ORDER BY rowid')], [(7, 'a'), (19, 'b')])
        self.assertEqual([j['job_id'] for j in self.store.snapshot()['jobs']], ['a', 'b'])

    def test_duplicate_attach_preserves_original_position_and_event(self):
        receipt = self.store.create_estimate('single_source', {}, {'summary': 'first', 'estimated_s': 2}, {})
        estimate_id = receipt['estimate_id']
        self.store.request(estimate_id)
        self.store.decide(estimate_id, True)
        job = {'job_id': 'a', 'kind': 'single_source', 'state': 'queued',
               'created_at': '2026-10-05T10:00:00+00:00'}
        self.store.attach_job(estimate_id, job)
        self.store.register_desktop_job('b', summary='second')
        with self.store.connection() as connection:
            before = connection.execute('SELECT rowid FROM queue_jobs WHERE job_id="a"').fetchone()[0]
        job['created_at'] = '2050-01-01T00:00:00+00:00'
        self.store.attach_job(estimate_id, job)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT rowid FROM queue_jobs WHERE job_id="a"').fetchone()[0], before)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM queue_events WHERE job_id="a" AND type="enqueued"').fetchone()[0], 1)
        self.assertTrue(self.store.acquire_execution('a'))
        self.assertFalse(self.store.acquire_execution('b'))
        self.assertEqual([j['job_id'] for j in self.store.snapshot()['jobs']], ['a', 'b'])

    def _approved_estimate(self, summary):
        estimate_id = self.store.create_estimate(
            'single_source', {}, {'summary': summary, 'estimated_s': 2}, {})['estimate_id']
        self.store.request(estimate_id)
        self.store.decide(estimate_id, True)
        return estimate_id

    def test_stale_reattach_preserves_and_returns_running_then_completed_job(self):
        estimate_id = self._approved_estimate('original')
        stale = {'job_id': 'a', 'kind': 'single_source', 'state': 'queued',
                 'created_at': '2026-10-05T00:00:00+00:00'}
        self.store.attach_job(estimate_id, stale)
        self.store.update_job({'job_id': 'a', 'state': 'running',
                               'started_at': '2026-10-05T01:00:00+00:00',
                               'progress': {'fraction': .75}})
        running = self.store.read_job('a')
        self.assertEqual(self.store.attach_job(estimate_id, stale), running)
        self.assertEqual(self.store.read_job('a'), running)
        self.store.update_job({'job_id': 'a', 'state': 'succeeded',
                               'finished_at': '2026-10-05T01:01:00+00:00',
                               'progress': {'fraction': 1}, 'result': {'run_id': 'saved-result'}})
        completed = self.store.read_job('a')
        self.assertEqual(self.store.attach_job(estimate_id, stale), completed)
        self.assertEqual(self.store.read_job('a'), completed)
        with self.store.connection() as connection:
            counts = dict(connection.execute(
                'SELECT type,COUNT(*) FROM queue_events WHERE job_id="a" GROUP BY type'))
        self.assertEqual(counts, {'enqueued': 1, 'completed': 1})

    def test_same_job_id_cannot_be_attached_to_a_different_estimate(self):
        first = self._approved_estimate('first estimate')
        second = self._approved_estimate('second estimate')
        job = {'job_id': 'a', 'kind': 'single_source', 'state': 'queued',
               'created_at': '2026-10-05T00:00:00+00:00'}
        authoritative = self.store.attach_job(first, job)
        with self.assertRaises(ValueError):
            self.store.attach_job(second, job)
        self.assertEqual(self.store.read_job('a'), authoritative)
        unused = self.store.read_estimate(second)
        self.assertEqual(unused['state'], 'approved')
        self.assertIsNone(unused['job_id'])

    def test_desktop_job_id_collision_cannot_be_rebound_to_an_estimate(self):
        authoritative = self.store.register_desktop_job('a', summary='desktop owner')
        estimate_id = self._approved_estimate('MCP collision')
        job = {'job_id': 'a', 'kind': 'single_source', 'state': 'queued',
               'created_at': '2026-10-05T00:00:00+00:00'}
        with self.assertRaises(ValueError):
            self.store.attach_job(estimate_id, job)
        self.assertEqual(self.store.read_job('a'), authoritative)
        unused = self.store.read_estimate(estimate_id)
        self.assertEqual(unused['state'], 'approved')
        self.assertIsNone(unused['job_id'])

    def test_delayed_other_process_registration_cannot_revoke_lease(self):
        context = multiprocessing.get_context('spawn')
        ready, release, registered, finish = [context.Event() for _ in range(4)]
        process = context.Process(target=_delayed_register,
                                  args=(str(self.root), ready, release, registered, finish))
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            self.store.register_desktop_job('first', summary='first committed')
            self.assertTrue(self.store.acquire_execution('first'))
            release.set()
            self.assertTrue(registered.wait(10))
            self.assertTrue(self.store.acquire_execution('first'))
            self.assertFalse(self.store.acquire_execution('delayed'))
            self.assertEqual([j['job_id'] for j in self.store.snapshot()['jobs']], ['first', 'delayed'])
        finally:
            release.set()
            finish.set()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(process.exitcode, 0)
        process.close()

    def test_legacy_nonhead_owner_can_renew_valid_lease(self):
        self._legacy_inverted_owner()
        with patch('time.time', return_value=1000):
            self.assertTrue(self.store.acquire_execution('b'))
            self.assertFalse(self.store.acquire_execution('a'))
        self.assertEqual(self._lease_row()['owner'], 'b')

    def test_expired_legacy_lease_is_not_stolen_from_active_owner(self):
        self._legacy_inverted_owner()
        with patch('time.time', return_value=3000):
            self.assertFalse(self.store.acquire_execution('a'))
            self.assertTrue(self.store.acquire_execution('b'))
        self.assertEqual(self._lease_row()['owner'], 'b')

    def test_snapshot_places_legacy_running_owner_before_waiter_and_eta(self):
        self._legacy_inverted_owner()
        jobs = self.store.snapshot()['jobs']
        self.assertEqual([job['job_id'] for job in jobs], ['b', 'a'])
        self.assertLess(jobs[0]['expected_finish_at'], jobs[1]['expected_finish_at'])

    def test_terminal_owner_cannot_renew_and_successor_acquires_after_expiry(self):
        a, _ = self._legacy_inverted_owner()
        b = self.store.read_job('b')
        b['state'] = 'succeeded'
        self.store.update_job(b)
        with patch('time.time', return_value=3000):
            self.assertFalse(self.store.acquire_execution('b'))
            self.assertFalse(self.store.acquire_execution('missing'))
            self.assertTrue(self.store.acquire_execution(a['job_id']))
        self.assertEqual(self._lease_row()['owner'], 'a')

    def test_dead_or_reused_owner_is_repaired_before_unexpired_lease_handoff(self):
        for identity in [('dead', None), ('alive', 'replacement-process')]:
            with self.subTest(identity=identity):
                root = self.root / str(identity[0])
                store = ComputeTaskStore(root)
                a = store.register_desktop_job('a', summary='dead or replaced owner')
                store.register_desktop_job('b', summary='successor')
                a.update(state='running', _owner_pid=987654321, _owner_start='original-process')
                with store.connection() as connection:
                    connection.execute('UPDATE queue_jobs SET payload=?,state=? WHERE job_id=?',
                                       (json.dumps(a), 'running', 'a'))
                    connection.execute('UPDATE execution_lease SET owner="a",expires_at=99999999')
                from coat_sim.core.process_identity import _process_identity
                def identity_for(pid):
                    return identity if pid == 987654321 else _process_identity(pid)
                with patch('coat_sim.core.process_identity._process_identity', side_effect=identity_for), \
                        patch('time.time', return_value=1000):
                    self.assertTrue(store.acquire_execution('b'))
                    self.assertFalse(store.acquire_execution('a'))
                    self.assertEqual(store.read_job('a')['state'], 'failed')
                    self.assertTrue(store.read_job('a')['interrupted'])

    def test_unknown_identity_does_not_allow_expiry_only_takeover(self):
        self._legacy_inverted_owner()
        with patch('coat_sim.core.process_identity._process_identity', return_value=('unknown', None)), \
                patch('time.time', return_value=9999999):
            self.assertFalse(self.store.acquire_execution('a'))
            self.assertTrue(self.store.acquire_execution('b'))
        self.assertEqual(self.store.read_job('b')['state'], 'running')

    def test_nonowner_release_cannot_clear_renewed_lease(self):
        self.store.register_desktop_job('a', summary='head')
        self.store.register_desktop_job('b', summary='next')
        with patch('time.time', return_value=1000):
            self.assertTrue(self.store.acquire_execution('a', ttl_s=30))
        with patch('time.time', return_value=5000):
            self.assertFalse(self.store.acquire_execution('b'))
            self.assertTrue(self.store.acquire_execution('a', ttl_s=30))
        self.store.release_execution('b')
        self.assertEqual(self._lease_row(), {'singleton': 1, 'owner': 'a', 'expires_at': 5030})


if __name__ == '__main__':
    unittest.main()
