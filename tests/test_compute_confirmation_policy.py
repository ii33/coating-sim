"""Resource-policy regressions; all jobs are isolated and no physics/network runs."""
import asyncio
from contextlib import contextmanager, ExitStack
from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import AsyncMock, Mock, patch

from coat_sim.core.api import CoatService
from coat_sim.core.compute_tasks import ComputeTaskStore, compute_release_policy
from coat_sim.agent.storage import AssistantStore
from tests.scheduler_queue_support import SchedulerHarness
from tests.test_scheduler_service import job_spec


class ComputePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ComputeTaskStore(self.root)
        self.assistant = AssistantStore(self.root / 'artifacts/assistant/state.sqlite3')
        self.session = self.assistant.new_session('policy regression')
        self.request = 'request-1'
        self.assistant.set_ui_session_mode(self.session, 'chat')
        self.assistant.bind_ui_request(self.session, self.request, 'chat')
        self.assistant.record_event(self.session, self.request, 'progress', {'stage': 'received', 'mode': 'chat'})
        self.scheduler = SchedulerHarness(self.root)
        self.addCleanup(self.scheduler.close)

    @contextmanager
    def scheduler_context(self, service):
        from coat_sim.mcp import server
        from coat_sim.scheduler import submission
        with ExitStack() as stack:
            stack.enter_context(patch.object(server, 'service', service))
            stack.enter_context(patch('coat_sim.scheduler.SchedulerClient', self.scheduler.client))
            stack.enter_context(patch.object(submission, 'SchedulerClient', self.scheduler.client))
            # Policy inputs are intentionally empty. Freeze a tiny real probe
            # after the real receipt authorization, without invoking physics.
            stack.enter_context(patch.object(submission, 'canonical_compute_parameters',
                                              side_effect=lambda service, kind, params: params))
            stack.enter_context(patch.object(submission, 'input_file_fingerprints', return_value={}))
            stack.enter_context(patch.object(submission, 'freeze_spec', return_value=job_spec()))
            yield

    def set_request_mode(self, mode):
        self.request = mode + '-' + self.request
        self.assistant.set_ui_session_mode(self.session, mode)
        self.assistant.bind_ui_request(self.session, self.request, mode)

    def receipt(self, seconds=60, kind='single_source'):
        return self.store.create_estimate(kind, {}, {'summary': 'test', 'estimated_s': seconds}, {})['estimate_id']

    def request_receipt(self, key):
        return self.store.request(key, session_id=self.session, request_id=self.request)

    def test_discussion_blocks_short_local_boundary_and_all_safe_kinds(self):
        for kind in ('single_source', 'process', 'mask_optimization', 'mask_weights', 'mask_field', 'point_weights'):
            for seconds in (0, 1, 59.999, 60):
                with self.subTest(kind=kind, seconds=seconds):
                    key = self.receipt(seconds, kind)
                    value = self.request_receipt(key)
                    self.assertEqual(value['release_policy'], 'blocked')
                    with self.assertRaisesRegex(ValueError, 'compute_discussion_read_only'):
                        self.store.decide(key, True, automatic=True)

    def test_discussion_blocks_heavy_unknown_script_and_external_stays_manual(self):
        for seconds, kind in ((60.001, 'process'), (None, 'process'), (1, 'python_script'), (1, 'external_paid')):
            key = self.receipt(seconds, kind)
            self.assertEqual(self.request_receipt(key)['release_policy'], 'blocked')
            with self.assertRaisesRegex(ValueError, 'compute_discussion_read_only'):
                self.store.decide(key, True, automatic=True)
        self.assertEqual(self.store.request(self.receipt(1))['release_policy'], 'manual')
        for invalid in (True, False, -1, math.nan, math.inf, '1'):
            self.assertEqual(compute_release_policy({'kind':'process','estimated_s':invalid}, {'mode':'chat','external':False})[0], 'blocked')

    def test_other_modes_preserved(self):
        for mode, policy in (('operate', 'countdown'), ('research', 'immediate')):
            self.assistant.set_ui_session_mode(self.session, mode)
            self.assistant.bind_ui_request(self.session, mode, mode)
            self.assistant.record_event(self.session, mode, 'progress', {'stage':'received','mode':mode})
            value = self.store.request(self.receipt(1), session_id=self.session, request_id=mode)
            self.assertEqual(value['release_policy'], policy)

    def test_repeated_request_has_one_pending_prompt_and_decline_sticks(self):
        key = self.receipt(None)
        first = self.request_receipt(key)
        second = self.request_receipt(key)
        self.assertEqual(first['requested_at'], second['requested_at'])
        self.assertEqual(len(self.store.snapshot()['confirmations']), 1)
        self.store.decide(key, False)
        self.assertEqual(self.store.snapshot()['confirmations'], [])
        with self.assertRaisesRegex(ValueError, 'not_pending'):
            self.request_receipt(key)

    def test_stopped_and_changed_inputs_cannot_autoexecute(self):
        key = self.receipt(1); self.request_receipt(key)
        self.assistant.record_event(self.session, self.request, 'cancelled', {})
        with self.assertRaisesRegex(ValueError, 'compute_request_stopped'):
            self.store.decide(key, True, automatic=True)
        key = self.receipt(1)
        self.store.request(key); self.store.decide(key, True)
        with self.assertRaisesRegex(ValueError, 'compute_estimate_changed'):
            self.store.authorize(key, 'single_source', {'changed':True}, {})

    def test_prepare_then_policy_release_concurrent_retries_enqueue_once(self):
        self.set_request_mode('research')
        from coat_sim.mcp import server
        service = CoatService(self.root)
        def estimate(kind, params):
            return self.store.create_estimate(kind, params, {'summary':'test','estimated_s':60}, {})
        with self.scheduler_context(service), patch.object(service, 'estimate_compute_task', estimate):
            prepared = server._submit_job('single_source', {}, lambda event: None, 'stable-client')
            self.assertEqual(prepared['state'], 'prepared')
            self.assertFalse(prepared['needs_user_confirmation'])
            self.assertEqual(self.store.snapshot()['confirmations'], [])
            key = prepared['estimate_id']
            # Bind using the actual research request, then exercise the real authorization,
            # deterministic ID, file lock, queue persistence and retry paths.
            self.request_receipt(key)
            self.store.decide(key, True, automatic=True)
            def submit(_):
                return server._submit_job('single_source', {}, lambda event: None, 'stable-client', estimate_id=key)
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs = list(pool.map(submit, range(8)))
            self.assertEqual(len({job['job_id'] for job in jobs}), 1)
            with self.store.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM scheduler_specs').fetchone()[0], 1)
            self.assertEqual(len(self.store.snapshot()['jobs']), 1)
            self.assertEqual([e['type'] for e in self.store.snapshot()['events']], ['enqueued'])
            self.assertEqual(self.store.snapshot()['confirmations'], [])
            retry = server._submit_job('single_source', {}, lambda event: None, 'stable-client')
            self.assertEqual(retry['job_id'], jobs[0]['job_id'])
            with self.store.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM scheduler_specs').fetchone()[0], 1)

    def test_mcp_scheduler_worker_start_failure_is_terminal_and_successor_runs(self):
        from coat_sim.mcp import server
        service = CoatService(self.root)
        for failure in (OSError('bounded spawn failure'), MemoryError('bounded start allocation')):
            with self.subTest(failure=type(failure).__name__):
                key = self.receipt(1)
                self.store.request(key); self.store.decide(key, True)
                with self.scheduler_context(service):
                    job = server._submit_job('single_source', {}, lambda event: None, None, estimate_id=key)
                self.assertEqual(job['state'], 'queued')
                with patch('coat_sim.scheduler.daemon.subprocess.Popen', side_effect=failure) as spawn:
                    self.scheduler.tick()
                spawn.assert_called_once()
                row = self.store.read_job(job['job_id'])
                self.assertEqual(row['state'], 'failed')
                self.assertEqual(row['error_code'], 'worker_start_failed')
                self.assertIn(str(failure), row['error'])
                self.assertTrue(row['finished_at'])
                # Failed work is not replayed; the next admitted job gets the slot.
                next_key = self.receipt(1)
                self.store.request(next_key); self.store.decide(next_key, True)
                with self.scheduler_context(service):
                    next_job = server._submit_job('single_source', {}, None, None, estimate_id=next_key)
                self.assertNotEqual(next_job['job_id'], job['job_id'])
                import time
                deadline = time.monotonic() + 10
                while self.store.read_job(next_job['job_id'])['state'] not in ('succeeded', 'failed') and time.monotonic() < deadline:
                    self.scheduler.tick(); time.sleep(.01)
                self.assertEqual(self.store.read_job(next_job['job_id'])['state'], 'succeeded')
                self.assertEqual(self.store.read_job(job['job_id']), row)

    def test_service_release_short_runs_automatically_and_heavy_waits_once(self):
        self.set_request_mode('research')
        from coat_sim.mcp import server
        service = CoatService(self.root)
        def submit(**arguments):
            key = arguments['estimate_id']
            return server._submit_job('single_source', {}, lambda event: None, None, estimate_id=key)
        with self.scheduler_context(service), patch.object(server, 'submit_single_source', side_effect=submit):
            key = self.receipt(60)
            job = server.request_compute_confirmation(key, self.session, self.request)
            self.assertEqual(job['state'], 'queued')
            self.assertEqual(server.request_compute_confirmation(key, self.session, self.request)['job_id'], job['job_id'])
            self.assertEqual(self.store.snapshot()['confirmations'], [])
            heavy = self.receipt(None)
            for _ in range(2):
                pending = server.request_compute_confirmation(heavy)
                self.assertEqual(pending['state'], 'awaiting_confirmation')
                self.assertTrue(pending['needs_user_confirmation'])
            self.assertEqual(len(self.store.snapshot()['confirmations']), 1)
            released = server.confirm_and_submit_compute(heavy, True)
            self.assertEqual(released['state'], 'queued')
            self.assertEqual(self.store.snapshot()['confirmations'], [])
            self.assertEqual(len(self.store.snapshot()['jobs']), 2)

    def test_desktop_has_no_short_prompt_and_exactly_one_heavy_prompt(self):
        from coat_sim.ui.assistant_desktop import DesktopAssistantBridge
        self.set_request_mode('research')
        short = self.request_receipt(self.receipt(60))
        heavy = self.store.request(self.receipt(None))
        dummy = SimpleNamespace(_closed=False, _busy_confirmation=set(), _cursor=0,
            _bootstrap=False, _decisions={}, _seen_confirmations=set(),
            changed=Mock(), decisionChanged=Mock(), _new_decision_notice=Mock(), _release_due=Mock())
        dummy._remove_decision=lambda key: dummy._decisions.pop(key, None)
        snapshot={'confirmations':[short,heavy],'events':[],'cursor':0,'jobs':[]}
        DesktopAssistantBridge._receive_snapshot(dummy, snapshot, '')
        DesktopAssistantBridge._receive_snapshot(dummy, snapshot, '')
        self.assertEqual(list(dummy._decisions), ['compute:'+heavy['estimate_id']])
        dummy._new_decision_notice.assert_called_once()
        self.store.decide(heavy['estimate_id'], False)
        DesktopAssistantBridge._receive_snapshot(dummy, {'confirmations':[short]}, '')
        self.assertEqual(dummy._decisions,{})

    def test_design_workflow_binds_prepared_receipt_once(self):
        from coat_sim.mcp import server
        with (patch.object(server,'design_pipeline',side_effect=lambda service, **callbacks: callbacks),
              patch.object(server,'submit_process_recipe',return_value={'state':'prepared','estimate_id':'one','needs_release_request':True}),
              patch.object(server,'request_compute_confirmation',return_value={'state':'queued','job_id':'one'}) as release):
            callbacks=server._registered_design_pipeline()
            result=callbacks['submit']({}, 'recipe.json', session_id='actual-session',request_id='actual-request')
        self.assertEqual(result['state'],'queued')
        release.assert_called_once_with('one','actual-session','actual-request')


class RuntimeRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_submit_binds_origin_without_model_confirmation_roundtrip(self):
        from coat_sim.agent.runtime import BudgetAndTools
        meter = object.__new__(BudgetAndTools)
        meter.mode = 'operate'
        meter.config = {'task_mode': 'operate'}
        meter.shared = {}
        meter.limits = {'tool_calls': 10}
        meter.client = cast(Any, object())
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        meter.store = AssistantStore(Path(temporary.name) / 'state.sqlite3')
        meter.role = 'main'
        meter.session = meter.store.new_session('explicit operation')
        meter.request = 'real-request'
        meter.store.set_ui_session_mode(meter.session, 'operate')
        meter.store.bind_ui_request(meter.session, meter.request, 'operate')
        meter.tool_lock=asyncio.Lock(); meter.last_poll={}; meter.gui_calculations=set()
        meter.writer=SimpleNamespace(redact=lambda text:text)
        meter.ledger=Mock(); meter.progress=Mock(); meter._observe_tool=Mock()
        meter._register_background=Mock(); meter._bounded_result=lambda name,args,result:result
        async def await_tool(value, stage, text): return await value
        meter._await_tool=await_tool
        handler=AsyncMock(return_value={'estimate_id':'receipt','state':'prepared','needs_release_request':True})
        release=AsyncMock(return_value={'estimate_id':'receipt','job_id':'one','state':'queued'})
        with patch('coat_sim.agent.runtime.ProjectGateway') as gateway:
            gateway.return_value.execute=release
            result=await meter.wrap_tool_execute(None, call=SimpleNamespace(tool_name='project_action',tool_call_id='call'),tool_def=None,
                args={'tool_name':'submit_single_source','arguments':{}},handler=handler)
        release.assert_awaited_once_with('request_compute_confirmation',{'estimate_id':'receipt','session_id':meter.session,'request_id':'real-request'})
        self.assertEqual(result['job_id'],'one')
        meter.ledger.result.assert_called_once()

    async def test_pending_release_is_refreshed_but_submission_stays_deduplicated(self):
        from coat_sim.agent.tool_dispatch import ProjectGateway
        client = SimpleNamespace(call_tool_mcp=AsyncMock(side_effect=[
            SimpleNamespace(structured_content={'estimate_id':'one','state':'awaiting_confirmation'}, is_error=False),
            SimpleNamespace(structured_content={'estimate_id':'one','state':'queued','job_id':'job'}, is_error=False),
        ]))
        gateway = ProjectGateway(client, {'request_compute_confirmation'}, set(), lambda text:text)
        gateway._tools={'request_compute_confirmation':{'input_schema':{'type':'object'}}}
        args={'estimate_id':'one'}
        first=await gateway.execute('request_compute_confirmation',args)
        second=await gateway.execute('request_compute_confirmation',args)
        third=await gateway.execute('request_compute_confirmation',args)
        self.assertEqual(first['state'],'awaiting_confirmation')
        self.assertEqual(second['job_id'],'job')
        self.assertEqual(third['job_id'],'job')
        self.assertEqual(client.call_tool_mcp.await_count,2)


if __name__ == '__main__': unittest.main()
