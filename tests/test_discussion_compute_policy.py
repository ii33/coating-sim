"""Discussion compute gates, using only receipts and in-memory probe manifests.

No scheduler daemon, worker, simulation, provider, or network is started.
"""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from typing import cast
import unittest
from unittest.mock import Mock, patch

from coat_sim.agent.storage import AssistantStore
from coat_sim.core.compute_tasks import ComputeTaskStore, compute_release_policy
from coat_sim.scheduler.store import SchedulerStore
from tests.test_scheduler_service import job_spec


class DiscussionComputePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assistant = AssistantStore(self.root / 'artifacts/assistant/state.sqlite3')
        self.session = self.assistant.new_session('discussion policy')
        self.store = ComputeTaskStore(self.root)
        self.scheduler = SchedulerStore(self.root)

    def request(self, mode='chat', *, request_id='request', seconds=1):
        self.assistant.set_ui_session_mode(self.session, mode)
        self.assistant.bind_ui_request(self.session, request_id, mode)
        key = self.store.create_estimate('single_source', {}, {'summary': 'bounded test', 'estimated_s': seconds}, {})['estimate_id']
        return self.store.request(key, session_id=self.session, request_id=request_id)

    def metadata(self, receipt):
        return {key: receipt[key] for key in ('estimate_id', 'origin', 'summary', 'estimated_s')}

    def test_all_discussion_tasks_including_short_local_are_blocked(self):
        for kind in ('single_source', 'process', 'mask_optimization', 'mask_weights', 'mask_field', 'point_weights', 'film_optimization'):
            for seconds in (0, 1, 59.999, 60, 61, None):
                with self.subTest(kind=kind, seconds=seconds):
                    self.assertEqual(compute_release_policy({'kind': kind, 'estimated_s': seconds},
                        {'mode': 'chat', 'external': False})[0], 'blocked')
        receipt = self.request()
        self.assertEqual(receipt['state'], 'pending')
        self.assertEqual(receipt['release_policy'], 'blocked')
        for automatic in (False, True):
            with self.assertRaisesRegex(ValueError, 'compute_discussion_read_only'):
                self.store.decide(receipt['estimate_id'], True, automatic=automatic)
        self.assertEqual(self.store.snapshot()['confirmations'][0]['estimate_id'], receipt['estimate_id'])

    def test_operate_countdown_switch_to_discussion_cannot_approve_or_restore(self):
        receipt = self.request('operate')
        self.assertEqual(receipt['release_policy'], 'countdown')
        self.assistant.set_ui_session_mode(self.session, 'chat')
        with self.assertRaisesRegex(ValueError, 'compute_discussion_read_only'):
            self.store.decide(receipt['estimate_id'], True, automatic=True)
        blocked = self.store.read_estimate(receipt['estimate_id'])
        self.assertEqual(blocked['state'], 'pending')
        self.assertIsNone(blocked['release_at'])
        self.assertTrue(blocked['requires_new_request'])
        self.assistant.set_ui_session_mode(self.session, 'operate')
        for automatic in (False, True):
            with self.assertRaisesRegex(ValueError, 'compute_ui_mode_changed'):
                self.store.decide(receipt['estimate_id'], True, automatic=automatic)
        self.assertEqual(self.store.read_estimate(receipt['estimate_id'])['state'], 'pending')
        fresh = self.request('operate', request_id='new-explicit-request')
        self.assertEqual(self.store.decide(fresh['estimate_id'], True)['state'], 'approved')

    def test_approved_receipt_switch_to_discussion_cannot_authorize_or_enqueue(self):
        receipt = self.request('research')
        self.store.decide(receipt['estimate_id'], True, automatic=True)
        self.assistant.set_ui_session_mode(self.session, 'chat')
        with self.assertRaisesRegex(ValueError, 'compute_discussion_read_only'):
            self.store.authorize(receipt['estimate_id'], 'single_source', {}, {})
        self.assertEqual(self.store.read_estimate(receipt['estimate_id'])['state'], 'pending')
        with self.assertRaisesRegex(ValueError, 'compute_confirmation_required'):
            self.scheduler.submit(job_spec(), job_id='blocked', metadata=self.metadata(receipt))
        self.assertEqual(self.store.read_estimate(receipt['estimate_id'])['state'], 'pending')
        self.assertEqual(self.store.snapshot()['jobs'], [])

    def test_historical_pending_and_received_mode_claims_are_not_authority(self):
        self.assistant.record_event(self.session, 'legacy', 'progress', {'stage': 'received', 'mode': 'research'})
        key = self.store.create_estimate('single_source', {}, {'summary': 'legacy', 'estimated_s': 1}, {})['estimate_id']
        receipt = self.store.request(key, session_id=self.session, request_id='legacy')
        self.assertEqual(receipt['release_policy'], 'blocked')
        self.assertIn('compute_ui_authority_missing', receipt['release_error'])
        self.assistant.set_ui_session_mode(self.session, 'research')
        with self.assertRaisesRegex(ValueError, 'compute_ui_authority_missing'):
            self.store.decide(key, True)
        # A pre-upgrade immediate receipt remains visible, without being replayed.
        with self.store.connection() as db:
            value = self.store.read_estimate(key)
            value.update(release_policy='immediate', request_mode='chat')
            value.pop('request_generation', None)
            value.pop('requires_new_request', None)
            db.execute("UPDATE estimates SET payload=? WHERE estimate_id=?", (json.dumps(value), key))
        self.assertEqual(self.store.snapshot()['confirmations'][0]['release_policy'], 'blocked')

    def test_request_mode_cannot_expand_current_discussion_selector(self):
        self.assistant.set_ui_session_mode(self.session, 'chat')
        self.assistant.bind_ui_request(self.session, 'override', 'research')
        key = self.store.create_estimate('single_source', {}, {'summary': 'override', 'estimated_s': 1}, {})['estimate_id']
        self.assertEqual(self.store.request(key, session_id=self.session, request_id='override')['release_policy'], 'blocked')
        with self.assertRaisesRegex(ValueError, 'assistant_ui_request_conflict'):
            self.assistant.bind_ui_request(self.session, 'override', 'operate')

    def test_cross_session_receipt_cannot_be_rebound(self):
        receipt = self.request('operate')
        second = self.assistant.new_session('second')
        self.assistant.set_ui_session_mode(second, 'operate')
        self.assistant.bind_ui_request(second, 'other', 'operate')
        with self.assertRaisesRegex(ValueError, 'compute_origin_conflict'):
            self.store.request(receipt['estimate_id'], session_id=second, request_id='other')

    def test_runtime_authority_helper_is_read_only_and_stop_exception_cannot_bypass_mode(self):
        unused_root = self.root / 'does-not-exist'
        self.assertFalse(ComputeTaskStore(unused_root).assistant_dispatch_authority('missing', 'request')['allowed'])
        self.assertFalse(unused_root.exists())
        # Runtime can use its actual store path without assuming its layout.
        custom = AssistantStore(self.root / 'custom-assistant-state.sqlite3')
        session = custom.new_session('custom path')
        custom.set_ui_session_mode(session, 'operate')
        custom.bind_ui_request(session, 'bound', 'operate')
        result = self.store.assistant_dispatch_authority(session, 'bound', database=custom.path)
        self.assertTrue(result['allowed'])
        self.assertEqual(result['generation'], result['current_generation'])
        custom.record_event(session, 'bound', 'cancelled', {})
        self.assertFalse(self.store.assistant_dispatch_authority(session, 'bound', database=custom.path)['allowed'])
        self.assertTrue(self.store.assistant_dispatch_authority(session, 'bound', database=custom.path,
            check_stopped=False)['allowed'])
        custom.set_ui_session_mode(session, 'chat')
        self.assertFalse(self.store.assistant_dispatch_authority(session, 'bound', database=custom.path,
            check_stopped=False)['allowed'])
        custom.set_ui_session_mode(session, 'operate')
        self.assertFalse(self.store.assistant_dispatch_authority(session, 'bound', database=custom.path,
            check_stopped=False)['allowed'])

    def test_manual_gui_jobs_and_existing_running_tasks_are_unchanged(self):
        receipt = self.request('research')
        self.store.decide(receipt['estimate_id'], True)
        job = self.scheduler.submit(job_spec(), job_id='existing', metadata=self.metadata(receipt))
        self.scheduler.transition(job['job_id'], state='running')
        self.assistant.set_ui_session_mode(self.session, 'chat')
        self.store.register_desktop_job('manual-gui', summary='explicit desktop click')
        manual = self.scheduler.submit(job_spec(), job_id='manual-scheduler', metadata={'origin': {'surface': 'desktop'}})
        self.assertEqual(manual['state'], 'queued')
        snapshot = self.store.snapshot()
        self.assertEqual({job['job_id'] for job in snapshot['jobs']}, {'existing', 'manual-gui', 'manual-scheduler'})
        self.assertEqual(self.scheduler.status('existing')['state'], 'running')
        self.assertFalse(self.scheduler.status('existing')['cancel_requested'])
        self.assertEqual(self.store.read_estimate(receipt['estimate_id'])['state'], 'submitted')
        self.assertFalse(list((self.root / 'artifacts/runtime/jobs').glob('*.cancel')))

    def test_final_enqueue_revalidates_mode_instead_of_trusting_approved_receipt(self):
        receipt = self.request('research')
        self.store.decide(receipt['estimate_id'], True)
        self.assistant.set_ui_session_mode(self.session, 'chat')
        with self.assertRaisesRegex(ValueError, 'compute_discussion_read_only'):
            self.scheduler.submit(job_spec(), job_id='late-submit', metadata=self.metadata(receipt))
        self.assertEqual(self.store.snapshot()['jobs'], [])
        self.assertEqual(self.store.read_estimate(receipt['estimate_id'])['state'], 'pending')

    def test_enqueue_before_mode_switch_is_serialized_even_with_wal(self):
        receipt = self.request('research')
        self.store.decide(receipt['estimate_id'], True)
        with closing(sqlite3.connect(self.assistant.path)) as db, db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        validating = threading.Event()
        allow_commit = threading.Event()
        switching = threading.Event()
        switched = threading.Event()
        validate = self.scheduler.queue._validate_pending
        connect = sqlite3.connect
        def guarded(value, **kwargs):
            validate(value, **kwargs)
            validating.set()
            self.assertTrue(allow_commit.wait(5))
        def traced_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            if threading.current_thread().name == 'mode-switch':
                connection.set_trace_callback(lambda sql: switching.set() if sql == 'BEGIN IMMEDIATE' else None)
            return connection
        def switch_mode():
            self.assistant.set_ui_session_mode(self.session, 'chat')
            switched.set()
        with patch.object(self.scheduler.queue, '_validate_pending', side_effect=guarded), patch('sqlite3.connect', side_effect=traced_connect):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.scheduler.submit, job_spec(), 'won-before-switch', self.metadata(receipt))
                self.assertTrue(validating.wait(5))
                thread = threading.Thread(target=switch_mode, name='mode-switch')
                thread.start()
                try:
                    self.assertTrue(switching.wait(5))
                    # A write lock, not a WAL reader, guards the enqueue boundary.
                    with closing(connect(self.assistant.path, timeout=0)) as db, db:
                        with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                            db.execute('BEGIN IMMEDIATE')
                    self.assertFalse(switched.is_set())
                finally:
                    allow_commit.set()
                job = future.result(timeout=5)
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertTrue(switched.is_set())
        self.assertEqual(job['state'], 'queued')
        self.assertEqual(self.store.read_estimate(receipt['estimate_id'])['state'], 'submitted')
        self.assertFalse(self.scheduler.status(job['job_id'])['cancel_requested'])

    def test_approval_cannot_overwrite_mode_switch_snapshot_rejection(self):
        receipt = self.request('operate')
        with closing(sqlite3.connect(self.assistant.path)) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
        validated = threading.Event()
        resume = threading.Event()
        switching = threading.Event()
        switched = threading.Event()
        validate = self.store._validate_pending
        connect = sqlite3.connect
        def guarded(value, **kwargs):
            validate(value, **kwargs)
            if threading.current_thread().name.startswith('approval'):
                validated.set()
                self.assertTrue(resume.wait(5))
        def traced_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            if threading.current_thread().name == 'mode-switch':
                connection.set_trace_callback(lambda sql: switching.set() if sql == 'BEGIN IMMEDIATE' else None)
            return connection
        def switch_mode():
            self.assistant.set_ui_session_mode(self.session, 'chat')
            switched.set()
        with patch.object(self.store, '_validate_pending', side_effect=guarded), patch('sqlite3.connect', side_effect=traced_connect):
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='approval') as pool:
                future = pool.submit(self.store.decide, receipt['estimate_id'], True)
                self.assertTrue(validated.wait(5))
                thread = threading.Thread(target=switch_mode, name='mode-switch')
                thread.start()
                try:
                    self.assertTrue(switching.wait(5))
                    self.assertFalse(switched.is_set())
                finally:
                    resume.set()
                self.assertEqual(future.result(timeout=5)['state'], 'approved')
                thread.join(5)
                self.assertFalse(thread.is_alive())
        blocked = self.store.snapshot()['confirmations'][0]
        self.assertEqual(blocked['state'], 'pending')
        self.assertEqual(blocked['release_policy'], 'blocked')
        self.assertTrue(blocked['requires_new_request'])
        self.assertIn('compute_discussion_read_only', blocked['release_error'])
        # A delayed second approval after the rejection must preserve it.
        with self.assertRaisesRegex(ValueError, 'compute_discussion_read_only'):
            self.store.decide(receipt['estimate_id'], True)
        retained = self.store.read_estimate(receipt['estimate_id'])
        for key in ('state', 'release_policy', 'requires_new_request', 'release_error'):
            self.assertEqual(retained[key], blocked[key])

    def test_block_release_cannot_revive_concurrent_explicit_decline(self):
        receipt = self.request('operate')
        reading = threading.Event()
        resume = threading.Event()
        declining = threading.Event()
        declined = threading.Event()
        loads = json.loads
        def guarded_loads(value, *args, **kwargs):
            parsed = loads(value, *args, **kwargs)
            if (threading.current_thread().name == 'block-release' and isinstance(parsed, dict)
                    and parsed.get('estimate_id') == receipt['estimate_id']):
                reading.set()
                self.assertTrue(resume.wait(5))
            return parsed
        def decline():
            declining.set()
            self.store.decide(receipt['estimate_id'], False)
            declined.set()
        with patch('coat_sim.core.compute_tasks.json.loads', side_effect=guarded_loads):
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='block-release') as pool:
                # Use an exact name so JSON parsing in other threads is untouched.
                def block():
                    threading.current_thread().name = 'block-release'
                    self.store.block_release(receipt['estimate_id'], 'compute_discussion_read_only: blocked')
                future = pool.submit(block)
                self.assertTrue(reading.wait(5))
                thread = threading.Thread(target=decline, name='explicit-decline')
                thread.start()
                try:
                    self.assertTrue(declining.wait(5))
                    with closing(sqlite3.connect(self.store.path, timeout=0)) as db, db:
                        with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                            db.execute('BEGIN IMMEDIATE')
                    self.assertFalse(declined.is_set())
                finally:
                    resume.set()
                future.result(timeout=5)
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertTrue(declined.is_set())
        self.assertEqual(self.store.read_estimate(receipt['estimate_id'])['state'], 'declined')
        self.store.block_release(receipt['estimate_id'], 'compute_discussion_read_only: later stale rejection')
        self.assertEqual(self.store.read_estimate(receipt['estimate_id'])['state'], 'declined')

    def test_desktop_replaces_old_countdown_with_visible_blocked_decision(self):
        from coat_sim.ui.assistant_desktop import DesktopAssistantBridge
        receipt = self.request('operate')
        dummy = SimpleNamespace(_closed=False, _busy_confirmation=set(), _cursor=0,
            _bootstrap=False, _decisions={}, _seen_confirmations=set(), changed=Mock(),
            decisionChanged=Mock(), _new_decision_notice=Mock(), _release_due=Mock())
        dummy._remove_decision = lambda key: dummy._decisions.pop(key, None)
        DesktopAssistantBridge._receive_snapshot(cast(DesktopAssistantBridge, dummy), {'confirmations': [receipt]}, '')
        decision = dummy._decisions['compute:' + receipt['estimate_id']]
        self.assertTrue(decision['optional'])
        self.assistant.set_ui_session_mode(self.session, 'chat')
        snapshot = self.store.snapshot()
        DesktopAssistantBridge._receive_snapshot(cast(DesktopAssistantBridge, dummy), snapshot, '')
        self.assertTrue(decision['release_blocked'])
        self.assertFalse(decision['optional'])
        self.assertIsNone(decision['deadline_ms'])
        self.assertIn('compute_discussion_read_only', decision['question'])
        dummy._release_due.assert_not_called()
        dummy._new_decision_notice.assert_called_once()
        # The pending record persists; the old receipt cannot auto-resume.
        self.assistant.set_ui_session_mode(self.session, 'operate')
        DesktopAssistantBridge._receive_snapshot(cast(DesktopAssistantBridge, dummy), self.store.snapshot(), '')
        self.assertTrue(decision['release_blocked'])

    def test_fast_mode_roundtrip_rejects_old_inflight_countdown_snapshot(self):
        from coat_sim.ui.assistant_desktop import DesktopAssistantBridge
        receipt = self.request('operate')
        dummy = SimpleNamespace(_closed=False, _busy_confirmation=set(), _cursor=0,
            _bootstrap=False, _decisions={}, _seen_confirmations=set(), changed=Mock(),
            decisionChanged=Mock(), _new_decision_notice=Mock(), _release_due=Mock(),
            _queue_authority_epoch=0, _refresh_estimates_pending=False, refreshQueue=Mock(),
            controller=SimpleNamespace(bridge=SimpleNamespace(taskMode='operate')))
        dummy._remove_decision = lambda key: dummy._decisions.pop(key, None)
        bridge = cast(DesktopAssistantBridge, dummy)
        DesktopAssistantBridge._receive_snapshot(bridge, {'confirmations': [receipt],
                                                          '_assistant_mode_epoch': 0}, '')
        decision = dummy._decisions['compute:' + receipt['estimate_id']]
        self.assertTrue(decision['optional'])
        self.assistant.set_ui_session_mode(self.session, 'chat')
        dummy.controller.bridge.taskMode = 'chat'
        DesktopAssistantBridge._assistant_mode_changed(bridge)
        self.assertTrue(decision['release_blocked'])
        self.assistant.set_ui_session_mode(self.session, 'operate')
        dummy.controller.bridge.taskMode = 'operate'
        DesktopAssistantBridge._assistant_mode_changed(bridge)
        self.assertEqual(dummy._queue_authority_epoch, 2)
        stale = {**receipt, 'release_at': '2000-01-01T00:00:00+00:00'}
        DesktopAssistantBridge._receive_snapshot(bridge, {'confirmations': [stale],
                                                          '_assistant_mode_epoch': 0}, '')
        self.assertTrue(decision['release_blocked'])
        self.assertIsNone(decision['deadline_ms'])
        self.assertFalse(decision['optional'])
        dummy._release_due.assert_not_called()
        fresh = {**self.store.snapshot(), '_assistant_mode_epoch': 2}
        DesktopAssistantBridge._receive_snapshot(bridge, fresh, '')
        self.assertTrue(decision['release_blocked'])
        self.assertIn('compute_ui_mode_changed', decision['question'])
        dummy._release_due.assert_not_called()

    def test_refresh_revalidates_authority_and_tags_ui_epoch_not_daemon_value(self):
        from coat_sim.ui.assistant_desktop import DesktopAssistantBridge
        client = Mock()
        client.snapshot.return_value = {'confirmations': [], '_assistant_mode_epoch': 999}
        def submit(function):
            future: Future = Future()
            future.set_result(function())
            return future
        dummy = SimpleNamespace(_closed=False, _fetching=False, _cursor=0,
            _queue_authority_epoch=7, _refresh_estimates_pending=True,
            _executor=SimpleNamespace(submit=submit), snapshotReady=Mock())
        with patch('coat_sim.scheduler.SchedulerClient', return_value=client):
            DesktopAssistantBridge.refreshQueue(cast(DesktopAssistantBridge, dummy))
        client.refresh_estimates.assert_called_once_with()
        client.snapshot.assert_called_once_with(after_event=0, limit=100)
        result, error = dummy.snapshotReady.emit.call_args.args
        self.assertEqual(result['_assistant_mode_epoch'], 7)
        self.assertEqual(client.snapshot.return_value['_assistant_mode_epoch'], 999)
        self.assertEqual(error, '')
        self.assertFalse(dummy._refresh_estimates_pending)

    def test_failed_authority_refresh_cannot_relabel_old_cache_as_current(self):
        from coat_sim.ui.assistant_desktop import DesktopAssistantBridge
        receipt = self.request('operate')
        self.assistant.set_ui_session_mode(self.session, 'chat')
        self.assistant.set_ui_session_mode(self.session, 'operate')
        fresh = self.store.snapshot()
        key = 'compute:' + receipt['estimate_id']
        decision = {'source': 'compute', 'estimate_id': receipt['estimate_id'],
                    'release_blocked': True, 'optional': False, 'deadline_ms': None}
        dummy = SimpleNamespace(_closed=False, _fetching=False, _busy_confirmation=set(), _cursor=0,
            _bootstrap=False, _decisions={key: decision}, _seen_confirmations={receipt['estimate_id']},
            changed=Mock(), decisionChanged=Mock(), _new_decision_notice=Mock(), _release_due=Mock(),
            _queue_authority_epoch=2, _refresh_estimates_pending=True,
            controller=SimpleNamespace(bridge=SimpleNamespace(taskMode='operate')))
        dummy._remove_decision = lambda identity: dummy._decisions.pop(identity, None)
        bridge = cast(DesktopAssistantBridge, dummy)
        dummy.refreshQueue = lambda: DesktopAssistantBridge.refreshQueue(bridge)
        dummy.snapshotReady = SimpleNamespace(emit=lambda value, error:
            DesktopAssistantBridge._receive_snapshot(bridge, value, error))
        def submit(function):
            future: Future = Future()
            try:
                future.set_result(function())
            except Exception as error:
                future.set_exception(error)
            return future
        dummy._executor = SimpleNamespace(submit=submit)
        client = Mock()
        client.snapshot.return_value = {'confirmations': [
            {**receipt, 'release_at': '2000-01-01T00:00:00+00:00'}]}
        attempts: list[bool] = []
        def revalidate():
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError('temporary refresh failure')
            client.snapshot.return_value = fresh
        client.refresh_estimates.side_effect = revalidate
        with patch('coat_sim.scheduler.SchedulerClient', return_value=client):
            dummy.refreshQueue()
            self.assertTrue(dummy._refresh_estimates_pending)
            self.assertFalse(dummy._fetching)
            self.assertEqual(len(attempts), 1)  # No immediate retry loop.
            client.snapshot.assert_not_called()
            self.assertTrue(decision['release_blocked'])
            dummy.refreshQueue()  # The next ordinary UI timer tick.
        self.assertEqual(len(attempts), 2)
        client.snapshot.assert_called_once()
        self.assertTrue(decision['release_blocked'])
        self.assertFalse(decision['optional'])
        self.assertIsNone(decision['deadline_ms'])
        dummy._release_due.assert_not_called()
        self.assertFalse(dummy._refresh_estimates_pending)

    def test_any_real_mode_change_revokes_old_local_countdown(self):
        from coat_sim.ui.assistant_desktop import DesktopAssistantBridge
        receipt = self.request('operate')
        decision = {'source': 'compute', 'origin': receipt['origin'],
                    'release_blocked': False, 'optional': True, 'deadline_ms': 1}
        dummy = SimpleNamespace(_assistant_mode_seen='operate', _queue_authority_epoch=0,
            controller=SimpleNamespace(bridge=SimpleNamespace(taskMode='research')),
            _decisions={'old': decision}, decisionChanged=Mock(), refreshQueue=Mock())
        DesktopAssistantBridge._assistant_mode_changed(cast(DesktopAssistantBridge, dummy))
        self.assertTrue(decision['release_blocked'])
        self.assertIsNone(decision['deadline_ms'])
        self.assertFalse(decision['optional'])
        self.assertIn('模式已改变', decision['error'])

    def test_desktop_local_guards_do_not_call_submit_for_blocked_receipts(self):
        from coat_sim.ui.assistant_desktop import DesktopAssistantBridge
        receipt = self.request()
        dummy = SimpleNamespace(_closed=False, _busy_confirmation=set(), _release_inflight=set(),
            _failed_auto_releases=set(), _held_countdowns=set(), _confirmations=[receipt],
            _executor=Mock(), changed=Mock(), refreshQueue=Mock())
        DesktopAssistantBridge._release_due(cast(DesktopAssistantBridge, dummy), receipt['estimate_id'])
        DesktopAssistantBridge.confirm(cast(DesktopAssistantBridge, dummy), receipt['estimate_id'], True)
        dummy._executor.submit.assert_not_called()
        self.assertEqual(dummy._busy_confirmation, set())

    def test_blocked_and_stale_approved_receipts_cannot_starve_fresh_pending(self):
        blocked_ids = {self.request('chat', request_id='blocked-' + str(index))['estimate_id']
                       for index in range(18)}
        for index in range(18):
            receipt = self.request('research', request_id='approved-' + str(index))
            self.store.decide(receipt['estimate_id'], True)
        # These have not yet been visited or marked blocked by any snapshot.
        stale_pending_ids = {self.request('research', request_id='stale-pending-' + str(index))['estimate_id']
                             for index in range(18)}
        fresh = self.request('operate', request_id='fresh-explicit-operation')
        confirmations = self.store.snapshot()['confirmations']
        self.assertEqual(confirmations[0]['estimate_id'], fresh['estimate_id'])
        self.assertEqual(confirmations[0]['release_policy'], 'countdown')
        self.assertFalse(confirmations[0].get('requires_new_request', False))
        self.assertTrue(all(self.store.read_estimate(key)['state'] == 'pending' for key in blocked_ids))
        self.assertTrue(all(self.store.read_estimate(key)['state'] == 'pending' for key in stale_pending_ids))


if __name__ == '__main__':
    unittest.main()
