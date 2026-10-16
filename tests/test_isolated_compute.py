"""IPC regression tests, including emulated Windows errors and real spawn pipes."""
import os
import time
import unittest
from unittest.mock import Mock, patch

from coat_sim.core import isolated_compute as isolated


def winerror(code=109):
    error = OSError(f'[WinError {code}] 管道已结束')
    error.winerror = code
    return error


def spawn_fixture(connection, kind, arguments):
    if kind == 'large':
        connection.send(('progress', {'step': 1}))
        connection.send(('result', {'data': b'x' * (8 * 1024 * 1024)}))
    elif kind == 'slow_exit':
        connection.send(('result', {'ok': True}))
        connection.close()
        time.sleep(2.2)
        return
    elif kind == 'crash':
        os._exit(7)
    elif kind == 'cancel':
        if connection.poll(10) and connection.recv() == ('cancel',):
            connection.send(('error', 'InterruptedError', 'cancelled', 'fixture traceback'))
        else:
            raise AssertionError('cancel message not delivered')
    connection.close()


class PipeRegressionTests(unittest.TestCase):
    def run_fake(self, messages=(), error=None, where='poll', code=0,
                 alive=True, stuck=False, progress=None, cancel_event=None, send_error=None):
        pending = list(messages)
        parent, child, process = Mock(), Mock(), Mock()
        process.exitcode = code
        process.is_alive.return_value = alive
        def join(timeout=None):
            if not stuck:
                process.is_alive.return_value = False
        def terminate():
            process.is_alive.return_value = False
            process.exitcode = -15
        process.join.side_effect = join
        process.terminate.side_effect = terminate
        closure = error if error is not None else EOFError()
        def poll(timeout=0):
            if pending or where == 'recv':
                return True
            raise closure
        def recv():
            if pending:
                return pending.pop(0)
            raise closure
        parent.send.side_effect = send_error
        parent.poll.side_effect = poll
        parent.recv.side_effect = recv
        context = Mock()
        context.Pipe.return_value = parent, child
        context.Process.return_value = process
        self.parent, self.child, self.process = parent, child, process
        with patch.object(isolated.multiprocessing, 'get_context', return_value=context):
            return isolated._run('single', {'backend': 'DirectML'},
                                 progress=progress, cancel_event=cancel_event)

    def test_clean_result_survives_windows_closure_live_and_post_exit(self):
        for alive in (False, True):
            for where in ('poll', 'recv'):
                for code in (109, 232):
                    with self.subTest(alive=alive, where=where, code=code):
                        self.assertEqual(self.run_fake([('result', {'ok': 1})],
                            error=winerror(code), where=where, alive=alive), {'ok': 1})
                        self.process.close.assert_called_once()
                        self.parent.close.assert_called_once()

    def test_clean_result_survives_eof(self):
        self.assertEqual(self.run_fake([('result', {'ok': 1})]), {'ok': 1})

    def test_no_result_reports_exit_backend_transport(self):
        for where in ('poll', 'recv'):
            with self.subTest(where=where), self.assertRaises(RuntimeError) as caught:
                self.run_fake(error=winerror(), where=where, code=23)
            for text in ('退出码 23', 'DirectML', '109', '未返回结果'):
                self.assertIn(text, str(caught.exception))

    def test_windows_access_violation_keeps_raw_code_and_classifies_without_guessing(self):
        for code in (3221225477, -1073741819):
            with self.subTest(code=code), self.assertRaises(RuntimeError) as caught:
                self.run_fake(code=code)
            for text in (str(code), '0xC0000005', '原生访问冲突', '不能仅据此判定内存不足', 'DirectML'):
                self.assertIn(text, str(caught.exception))

    def test_nonzero_exit_invalidates_delivered_result(self):
        with self.assertRaisesRegex(RuntimeError, '退出码 7'):
            self.run_fake([('result', {'ok': 1})], code=7)

    def test_unknown_transport_error_not_silently_accepted(self):
        with self.assertRaisesRegex(RuntimeError, '123'):
            self.run_fake([('result', {'ok': 1})], error=winerror(123))

    def test_worker_error_and_traceback_not_overwritten_by_pipe_error(self):
        with self.assertRaises(MemoryError) as caught:
            self.run_fake([('error', 'MemoryError', 'GPU allocation failed', 'child trace')],
                          error=winerror())
        for text in ('GPU allocation failed', 'child trace', '109', '退出码 0'):
            self.assertIn(text, str(caught.exception))

    def test_legacy_error_tuple_and_cancel_type_preserved(self):
        with self.assertRaisesRegex(InterruptedError, 'cancelled'):
            self.run_fake([('error', 'InterruptedError', 'cancelled')])

    def test_progress_callback_oserror_not_classified_as_pipe_error(self):
        original = OSError('callback failed')
        with self.assertRaises(OSError) as caught:
            self.run_fake([('progress', {'step': 1})],
                          progress=Mock(side_effect=original))
        self.assertIs(caught.exception, original)
        self.process.terminate.assert_called_once()

    def test_closed_pipe_live_worker_terminated_and_never_success(self):
        with self.assertRaisesRegex(RuntimeError, '已终止'):
            self.run_fake([('result', {'ok': 1})], error=winerror(), stuck=True)
        self.process.join.assert_any_call(isolated._WORKER_EXIT_GRACE_SECONDS)
        self.process.terminate.assert_called_once()
        self.process.close.assert_called_once()

    def test_cancel_send_failure_still_reports_worker_error(self):
        cancel = Mock()
        cancel.is_set.return_value = True
        # Sending cancellation is advisory; receiving still determines outcome.
        with self.assertRaisesRegex(InterruptedError, 'cancelled'):
            self.run_fake([('error', 'InterruptedError', 'cancelled')],
                          cancel_event=cancel, send_error=winerror())
        self.parent.send.assert_called_once_with(('cancel',))

    def test_child_cancel_handles_poll_and_recv_oserror(self):
        for where in ('poll', 'recv'):
            connection = Mock()
            connection.poll.return_value = True
            getattr(connection, where).side_effect = winerror()
            event = isolated._PipeCancelEvent(connection)
            self.assertTrue(event.is_set())
            self.assertTrue(event.is_set())
            getattr(connection, where).assert_called_once()

    def test_child_errors_include_original_traceback(self):
        connection = Mock()
        isolated._compute_child(connection, 'invalid', {})
        message = connection.send.call_args.args[0]
        self.assertEqual(message[:2], ('error', 'ValueError'))
        self.assertIn('Traceback', message[3])
        self.assertIn('_compute_child', message[3])
        connection.close.assert_called_once()

    def test_start_failure_closes_both_endpoints(self):
        context = Mock()
        parent, child = Mock(), Mock()
        context.Pipe.return_value = parent, child
        context.Process.return_value.start.side_effect = OSError('spawn failed')
        with patch.object(isolated.multiprocessing, 'get_context', return_value=context):
            with self.assertRaisesRegex(OSError, 'spawn failed'):
                isolated._run('single', {})
        parent.close.assert_called_once()
        child.close.assert_called_once()

    def test_process_construction_failure_closes_both_endpoints(self):
        context = Mock()
        parent, child = Mock(), Mock()
        context.Pipe.return_value = parent, child
        context.Process.side_effect = MemoryError('bounded process construction')
        with patch.object(isolated.multiprocessing, 'get_context', return_value=context):
            with self.assertRaisesRegex(MemoryError, 'bounded process construction'):
                isolated._run('single', {})
        parent.close.assert_called_once()
        child.close.assert_called_once()

    def test_result_serialization_memory_error_reports_failure_and_closes(self):
        from coat_sim.core.simulator import Settings
        connection = Mock()
        def send(message):
            if message[0] == 'result':
                raise MemoryError('bounded result serialization failure')
        connection.send.side_effect = send
        with patch('coat_sim.core.simulator.simulate', return_value={'ok': True}):
            isolated._compute_child(connection, 'single', {'settings': Settings(grid_size=21),
                'mask': None, 'backend': 'CPU', 'source_profile': None})
        failure = connection.send.call_args.args[0]
        self.assertEqual(failure[:2], ('error', 'MemoryError'))
        self.assertIn('bounded result serialization failure', failure[2])
        connection.close.assert_called_once()

    def test_error_envelope_allocation_failure_still_closes_child_pipe(self):
        connection = Mock()
        connection.send.side_effect = MemoryError('bounded error envelope allocation')
        isolated._compute_child(connection, 'invalid', {})
        connection.close.assert_called_once()


class RealSpawnTests(unittest.TestCase):
    def test_large_result_drained_without_join_deadlock(self):
        updates = []
        with patch.object(isolated, '_compute_child', spawn_fixture):
            result = isolated._run('large', {}, progress=updates.append)
        self.assertEqual(len(result['data']), 8 * 1024 * 1024)
        self.assertEqual(updates, [{'step': 1}])

    def test_clean_result_allows_native_teardown_grace(self):
        with patch.object(isolated, '_compute_child', spawn_fixture):
            self.assertEqual(isolated._run('slow_exit', {}), {'ok': True})

    def test_native_style_exit_has_status_not_raw_eof(self):
        with patch.object(isolated, '_compute_child', spawn_fixture):
            with self.assertRaisesRegex(RuntimeError, '退出码 7'):
                isolated._run('crash', {'backend': 'DirectML'})

    def test_cooperative_cancellation(self):
        cancel = Mock()
        cancel.is_set.return_value = True
        with patch.object(isolated, '_compute_child', spawn_fixture):
            with self.assertRaisesRegex(InterruptedError, 'cancelled'):
                isolated._run('cancel', {}, cancel_event=cancel)

    def test_actual_worker_python_error_retains_trace(self):
        with self.assertRaises(ValueError) as caught:
            isolated._run('invalid', {'backend': 'DirectML'})
        self.assertIn('_compute_child', str(caught.exception))
        self.assertIn('ValueError', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
