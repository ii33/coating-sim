"""Remote desktop HTTP boundaries remain off the Qt event thread."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
import unittest
from unittest.mock import patch
from typing import Callable, cast

from coat_sim.scheduler.desktop import RemoteCancelEvent, resolve_unknown_submission
from coat_sim.scheduler.remote_client import RemoteServiceError, RemoteSubmissionUnknown
from tests import test_desktop_compute_queue as support


class _SlowRemoteClient:
    transport = type("RemoteHTTP", (), {"remote_http": True})()

    def __init__(self):
        self.submit_entered = threading.Event()
        self.submit_release = threading.Event()
        self.cancel_entered = threading.Event()
        self.cancel_release = threading.Event()
        self.submissions = []
        self.cancel_calls = []
        self.network_threads = []
        self.terminal = threading.Event()

    def submit(self, spec, job_id=None, metadata=None):
        self.network_threads.append(threading.get_ident())
        self.submissions.append(job_id)
        self.submit_entered.set()
        if not self.submit_release.wait(8):
            raise TimeoutError("test_submit_gate_timeout")
        return {"job_id": job_id, "state": "queued"}

    def cancel(self, job_id):
        self.network_threads.append(threading.get_ident())
        self.cancel_calls.append(job_id)
        self.cancel_entered.set()
        if not self.cancel_release.wait(8):
            raise TimeoutError("test_cancel_gate_timeout")
        self.terminal.set()
        return {"job_id": job_id, "state": "cancelling", "cancel_requested": True}

    def wait_for_terminal(self, job_id, on_update=None, should_stop=None, wait_s=1.0):
        self.network_threads.append(threading.get_ident())
        if not self.terminal.wait(8):
            raise TimeoutError("test_terminal_gate_timeout")
        return {"job_id": job_id, "state": "cancelled"}


class RemoteCancelEventTests(unittest.TestCase):
    def test_cancel_before_admission_is_deferred_and_off_thread(self):
        remote = _SlowRemoteClient()
        event = RemoteCancelEvent(remote, "same-job-id")
        main_thread = threading.get_ident()
        start = time.monotonic()
        event.set()
        event.set()
        self.assertLess(time.monotonic() - start, .3)
        self.assertTrue(event.is_set())
        self.assertFalse(remote.cancel_entered.is_set())
        event.mark_admitted()
        self.assertTrue(remote.cancel_entered.wait(2))
        self.assertEqual(remote.cancel_calls, ["same-job-id"])
        self.assertTrue(all(item != main_thread for item in remote.network_threads))
        self.assertFalse(event.cancel_acknowledged)
        remote.cancel_release.set()
        deadline = time.monotonic() + 2
        while not event.cancel_acknowledged and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(event.cancel_acknowledged)

    def test_lost_ack_recovery_requires_exact_digest_and_device(self):
        class StatusClient:
            def __init__(self, digest):
                self.digest = digest
                self.calls = []
            def status(self, job_id):
                self.calls.append(job_id)
                return {"job_id": job_id, "state": "queued", "input_summary": {
                    "external_spec_sha256": self.digest, "device_id": "cpu:default"}}
        unknown = RemoteSubmissionUnknown("saved-id", expected_spec_sha256="a" * 64,
                                          device_id="cpu:default")
        good = StatusClient("a" * 64)
        self.assertEqual(resolve_unknown_submission(good, unknown, timeout_s=.1)["job_id"],
                         "saved-id")
        self.assertEqual(good.calls, ["saved-id"])
        with self.assertRaisesRegex(RemoteServiceError, "submission_receipt_mismatch"):
            resolve_unknown_submission(StatusClient("b" * 64), unknown, timeout_s=.1)

    def test_failed_cancel_can_be_retried_without_new_job_id(self):
        class FailingClient:
            transport = type("RemoteHTTP", (), {"remote_http": True})()
            def __init__(self):
                self.ids = []
            def cancel(self, job_id):
                self.ids.append(job_id)
                raise RemoteServiceError("remote_transport_error")
        client = FailingClient()
        event = RemoteCancelEvent(client, "one-job")
        event.mark_admitted()
        event.set()
        deadline = time.monotonic() + 3
        while event._cancel_started and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(event._cancel_started)
        self.assertEqual(client.ids, ["one-job"] * 3)
        self.assertFalse(event.cancel_acknowledged)
        event.set()
        deadline = time.monotonic() + 3
        while event._cancel_started and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(client.ids, ["one-job"] * 6)


class RemoteMainAsyncTests(unittest.TestCase):
    """Reuse the temp-root main bridge fixture; remote calls are gated fakes."""
    tearDown = support.QueueTests.tearDown
    spin = support.QueueTests.spin
    bridge = support.QueueTests.bridge

    def setUp(self):
        support.QueueTests.setUp(self)
        from coat_sim.ui import settings
        isolated_settings = patch.object(settings, "SETTINGS_PATH",
                                         self.root / "config" / "ui_settings.json")
        self.patches.append(isolated_settings)
        isolated_settings.start()

    def test_remote_submit_and_cancel_do_not_block_gui_or_duplicate(self):
        bridge = self.bridge()
        remote = _SlowRemoteClient()
        main_thread = threading.get_ident()
        target = {"id": "cpu:default", "backend": "CPU", "available": True}
        try:
            with patch.object(bridge._compute_target, "client_for_submit", return_value=(remote, target)):
                started = time.monotonic()
                bridge.calculate()
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertTrue(remote.submit_entered.wait(2))
                bridge.calculate()
            self.assertEqual(len(remote.submissions), 1)
            self.assertEqual(len(bridge._queued_keys), 1)
            started = time.monotonic()
            bridge.cancel()
            self.assertLess(time.monotonic() - started, .3)
            self.assertFalse(remote.cancel_entered.is_set())
            remote.submit_release.set()
            self.assertTrue(remote.cancel_entered.wait(2))
            self.assertFalse(bridge.hasResult)
            remote.cancel_release.set()
            self.spin(lambda: not bridge.busy, timeout=8)
            self.assertIn("计算已取消", bridge.status)
            self.assertEqual(len(remote.cancel_calls), 1)
            self.assertTrue(all(item != main_thread for item in remote.network_threads))
        finally:
            remote.submit_release.set()
            remote.cancel_release.set()
            remote.terminal.set()

    def test_close_during_remote_submit_detaches_without_network_on_gui(self):
        bridge = self.bridge()
        remote = _SlowRemoteClient()
        target = {"id": "cpu:default", "backend": "CPU", "available": True}
        with patch.object(bridge._compute_target, "client_for_submit", return_value=(remote, target)):
            bridge.calculate()
        self.assertTrue(remote.submit_entered.wait(2))
        started = time.monotonic()
        bridge.detach_observers()
        self.assertLess(time.monotonic() - started, .3)
        self.assertEqual(remote.cancel_calls, [])
        remote.submit_release.set()
        remote.terminal.set()
        time.sleep(.05)
        self.assertEqual(remote.cancel_calls, [])

    def test_lost_ack_reuses_prior_id_and_defers_cancel_until_status_matches(self):
        bridge = self.bridge()
        remote = _SlowRemoteClient()
        target = {"id": "cpu:default", "backend": "CPU", "available": True}
        remote.status_calls = []
        def lost_ack(spec, job_id=None, metadata=None):
            remote.submissions.append(job_id)
            remote.submit_entered.set()
            assert remote.submit_release.wait(8)
            raise RemoteSubmissionUnknown("prior-persisted-id",
                                          expected_spec_sha256="a" * 64,
                                          device_id="cpu:default")
        def status(job_id):
            remote.status_calls.append(job_id)
            return {"job_id": job_id, "state": "queued", "input_summary": {
                "external_spec_sha256": "a" * 64, "device_id": "cpu:default"}}
        remote.submit = lost_ack
        remote.status = status
        try:
            with patch.object(bridge._compute_target, "client_for_submit", return_value=(remote, target)):
                bridge.calculate()
            self.assertTrue(remote.submit_entered.wait(2))
            bridge.cancel()
            self.assertFalse(remote.cancel_entered.is_set())
            remote.submit_release.set()
            self.assertTrue(remote.cancel_entered.wait(2))
            self.assertEqual(remote.status_calls, ["prior-persisted-id"])
            self.assertEqual(remote.cancel_calls, ["prior-persisted-id"])
            self.assertEqual(len(remote.submissions), 1)
            remote.cancel_release.set()
            self.spin(lambda: not bridge.busy, timeout=8)
        finally:
            remote.submit_release.set()
            remote.cancel_release.set()
            remote.terminal.set()


if __name__ == "__main__":
    unittest.main()


class _SlowLocalClient(_SlowRemoteClient):
    def __init__(self):
        super().__init__()
        self.transport = None
        self.capabilities: Callable[[], dict[str, object]] = lambda: {
            "protocol_version": 1, "executors": ["process_recipe"]}
        self.status: Callable[[str], dict[str, str]] = lambda job_id: {
            "job_id": job_id, "input_digest": ""}
        self.submit_override: Callable[..., object] | None = None

    def submit(self, spec, job_id=None, metadata=None):
        if self.submit_override is not None:
            return self.submit_override(spec, job_id=job_id, metadata=metadata)
        return super().submit(spec, job_id=job_id, metadata=metadata)


class LocalMainAsyncTests(RemoteMainAsyncTests):
    """Local capability startup, POST and cancellation use gated offline fakes."""

    def bridge(self):
        return support.QueueTests.bridge(cast(support.QueueTests, self))

    def spin(self, predicate: Callable[[], bool], timeout: float = 12) -> None:
        support.QueueTests.spin(cast(support.QueueTests, self), predicate, timeout)

    def test_local_admission_capabilities_stays_responsive_and_cancel_prevents_post(self):
        from PySide6.QtCore import QTimer
        bridge = self.bridge()
        client = _SlowLocalClient()
        client.transport = None
        entered, release = threading.Event(), threading.Event()
        threads = []
        def capabilities():
            threads.append(threading.get_ident())
            entered.set()
            assert release.wait(5)
            return {"protocol_version": 1, "executors": ["process_recipe"]}
        client.capabilities = capabilities
        target = {"id": "cpu:default", "backend": "CPU", "available": True}
        beats = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: beats.append(True))
        timer.start()
        try:
            with patch.object(bridge._compute_target, "client_for_submit", return_value=(client, target)):
                start = time.monotonic()
                bridge.calculate()
                assert time.monotonic() - start < .5
                assert entered.wait(1)
                assert bridge.busy
                bridge.calculate()
            self.spin(lambda: len(beats) >= 2, timeout=1)
            start = time.monotonic()
            bridge.cancel()
            assert time.monotonic() - start < .3
            release.set()
            self.spin(lambda: not bridge.busy)
            assert client.submissions == []
            assert client.cancel_calls == []
            assert threads == [threads[0]] and threads[0] != threading.get_ident()
        finally:
            timer.stop()
            release.set()
            client.submit_release.set()
            client.cancel_release.set()
            client.terminal.set()

    def test_local_submit_and_cancel_are_off_gui(self):
        bridge = self.bridge()
        client = _SlowLocalClient()
        client.transport = None
        client.capabilities = lambda: {"protocol_version": 1, "executors": ["process_recipe"]}
        target = {"id": "cpu:default", "backend": "CPU", "available": True}
        try:
            with patch.object(bridge._compute_target, "client_for_submit", return_value=(client, target)):
                bridge.calculate()
                assert client.submit_entered.wait(1)
                bridge.calculate()
            bridge.cancel()
            assert not client.cancel_calls
            client.submit_release.set()
            assert client.cancel_entered.wait(1)
            assert len(client.submissions) == 1
            assert client.cancel_calls == client.submissions
            assert all(t != threading.get_ident() for t in client.network_threads)
            client.cancel_release.set()
            self.spin(lambda: not bridge.busy)
        finally:
            client.submit_release.set()
            client.cancel_release.set()
            client.terminal.set()

    def test_local_unknown_receipt_preserves_deduplication(self):
        bridge = self.bridge()
        client = _SlowLocalClient()
        client.transport = None
        client.capabilities = lambda: {"protocol_version": 1, "executors": ["process_recipe"]}
        def submit(spec, *, job_id, metadata):
            client.submissions.append(job_id)
            raise OSError("ambiguous local acknowledgment")
        client.submit_override = submit
        client.status = lambda job_id: {"job_id": job_id, "input_digest": "mismatched"}
        target = {"id": "cpu:default", "backend": "CPU", "available": True}
        with patch.object(bridge._compute_target, "client_for_submit", return_value=(client, target)):
            bridge.calculate()
            self.spin(lambda: not bridge.busy)
            assert "提交状态未知" in bridge.status
            bridge.calculate()
        assert len(client.submissions) == 1
        assert len(bridge._queued_keys) == 1

    def test_local_lost_ack_releases_pending_cancel_for_matching_receipt(self):
        from coat_sim.scheduler.jobs import JobSpec
        bridge = self.bridge()
        client = _SlowLocalClient()
        client.transport = None
        client.capabilities = lambda: {"protocol_version": 1, "executors": ["process_recipe"]}
        receipt: dict[str, str] = {}
        original_submit = super(_SlowLocalClient, client).submit
        def submit(spec, *, job_id, metadata):
            receipt.update(job_id=job_id, input_digest=JobSpec.parse_frozen(spec).fingerprint)
            original_submit(spec, job_id=job_id, metadata=metadata)
            raise OSError("accepted then response lost")
        client.submit_override = submit
        client.status = lambda job_id: dict(receipt)
        target = {"id": "cpu:default", "backend": "CPU", "available": True}
        try:
            with patch.object(bridge._compute_target, "client_for_submit", return_value=(client, target)):
                bridge.calculate()
            assert client.submit_entered.wait(1)
            bridge.cancel()
            assert not client.cancel_calls
            client.submit_release.set()
            assert client.cancel_entered.wait(1)
            client.cancel_release.set()
            self.spin(lambda: not bridge.busy)
            assert client.cancel_calls == client.submissions
            assert len(client.submissions) == 1
        finally:
            client.submit_release.set()
            client.cancel_release.set()
            client.terminal.set()
