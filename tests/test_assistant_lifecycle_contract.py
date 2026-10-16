"""Real AssistantBridge run lifecycle with an isolated fake worker."""
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from coat_sim.agent.storage import AssistantStore
from coat_sim.ui import assistant


class FakeDiscoveryBus(QObject):
    modelsChanged = Signal(str, object)
    reasoningChanged = Signal(str, str, object)

    def models_for(self, _url):
        return []

    def reasoning_for(self, _url, _model):
        return None

    def api_key(self, value):
        return value


class FakeSettings(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)


class FakeInput:
    def __init__(self):
        self.lines = []

    def write(self, value):
        self.lines.append(value.decode("utf-8"))

    def flush(self):
        pass

    def commands(self):
        return [json.loads(line) for line in self.lines]


class FakeWorker:
    def __init__(self, pid):
        self.pid = pid
        self.stdin = FakeInput()
        self.return_code = None

    def poll(self):
        return self.return_code

    def wait(self, timeout=None):
        self.return_code = 0
        return 0


class AssistantLifecycleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.paths = iter((root / "first.sqlite3", root / "second.sqlite3", root / "third.sqlite3"))
        self.workers = []

        def store_factory():
            return AssistantStore(next(self.paths))

        config = {
            "assistant_enabled": True,
            "assistant_api_url": "https://example.invalid/v1",
            "assistant_model": "fake-model",
            "assistant_task_mode": "chat",
            "assistant_reasoning_strength": "auto",
            "assistant_total_token_limit": 0,
            "assistant_daily_token_limit": 0,
            "assistant_context_budget_version": 2,
            "assistant_model_limits": {
                "https://example.invalid/v1": {
                    "fake-model": {
                        "context_budget_version": 2,
                        "context_window_tokens": 32768,
                        "reserved_output_tokens": 0,
                        "compression_reserve_tokens": 0,
                        "history_keep_tokens": 0,
                        "compaction_threshold_percent": 0,
                        "max_input_tokens": 0,
                        "legacy_values": {},
                    },
                },
            },
        }
        self.patches = [
            patch.object(assistant, "load_ui_settings", lambda: deepcopy(config)),
            patch.object(assistant, "model_discovery_bus", FakeDiscoveryBus),
            patch.object(assistant, "SettingsBridge", FakeSettings),
            patch.object(assistant, "AssistantStore", store_factory),
            patch.object(assistant, "reasoning_capabilities", lambda *_: {
                "supported_strengths": ["auto"], "reasoning_supported": False,
            }),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def make_bridge(self):
        bridge = assistant.AssistantBridge()
        worker = FakeWorker(7000 + len(self.workers))
        self.workers.append(worker)
        bridge._run_coordinator.worker = worker
        bridge._ensure_worker = lambda: None
        return bridge, worker

    def dispose_bridge(self, bridge):
        bridge.shutdown()
        bridge._storage_pool.shutdown(wait=True, cancel_futures=True)

    def start_run(self, bridge, prompt="first"):
        self.assertTrue(bridge.sendMessage(prompt), bridge.statusMessage)
        self.assertTrue(bridge.busy)
        return bridge._request_id

    def deliver(self, bridge, worker, payload):
        bridge._on_worker_event({"process": worker.pid, "payload": payload})

    def test_busy_submit_queues_and_steering_targets_the_active_request(self):
        bridge, worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        request = self.start_run(bridge)

        self.assertTrue(bridge.sendMessage("queued message"))
        self.assertEqual(bridge.queuedCount, 1)
        self.assertTrue(bridge.steerMessage("steer current run"))
        commands = worker.stdin.commands()
        self.assertEqual([item["op"] for item in commands], ["run", "steer"])
        self.assertEqual(commands[0]["request_id"], request)
        self.assertEqual(commands[1]["request_id"], request)
        self.assertEqual(bridge._pending_messages[0]["prompt"], "queued message")

    def test_cancel_is_sent_once_and_timeout_settles_the_run(self):
        bridge, worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        request = self.start_run(bridge)

        bridge.cancelTask()
        bridge.cancelTask()
        cancel_commands = [item for item in worker.stdin.commands() if item["op"] == "cancel"]
        self.assertEqual(cancel_commands, [{"op": "cancel", "request_id": request}])
        self.assertTrue(bridge.cancelling)

        bridge._cancel_timeout()
        self.assertFalse(bridge.busy)
        self.assertEqual(bridge._request_id, "")
        self.assertEqual([item["op"] for item in worker.stdin.commands()],
                         ["run", "cancel", "shutdown"])

    def test_stale_request_events_are_ignored_and_terminal_event_settles(self):
        bridge, worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        request = self.start_run(bridge)
        settlement_state = []
        bridge.taskSettled.connect(lambda *_args: settlement_state.append(
            (bridge.busy, bridge._run_coordinator.run_active)))
        self.deliver(bridge, worker, {"event": "token", "request_id": "old-request", "text": "stale"})
        self.assertEqual(bridge._streaming_text, "")
        self.deliver(bridge, worker, {"event": "completed", "request_id": "old-request"})
        self.assertTrue(bridge.busy)

        self.deliver(bridge, worker, {"event": "completed", "request_id": request, "text": "done"})
        self.assertFalse(bridge.busy)
        self.assertEqual(bridge._request_id, request)
        self.assertEqual(bridge.debug_status()["request_id"], request)
        self.assertFalse(bridge._run_coordinator.run_active)
        self.assertEqual(settlement_state, [(False, False)])
        self.assertEqual(bridge.statusMessage, "done")
        self.assertIn("done", [row["content"] for row in bridge._store.messages(bridge._session)])
        self.assertFalse(bridge._run_coordinator.begin_cancel())
        self.deliver(bridge, worker, {"event": "token", "request_id": request, "text": "too late"})
        self.assertEqual(bridge._streaming_text, "")
        self.deliver(bridge, worker, {"event": "completed", "request_id": request, "text": "duplicate"})
        self.assertEqual(bridge.statusMessage, "done")

        self.deliver(bridge, worker, {
            "event": "usage", "request_id": request,
            "round_usage": {"input_tokens": 7, "output_tokens": 3},
        })
        self.deliver(bridge, worker, {
            "event": "context", "request_id": request, "source": "main",
            "used_tokens": 19,
        })
        self.assertEqual(bridge._round_usage["input_tokens"], 7)
        self.assertEqual(bridge._context["used_tokens"], 19)

    def test_old_usage_and_context_are_rejected_after_a_new_run_starts(self):
        bridge, worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        first_request = self.start_run(bridge, "first")
        self.deliver(bridge, worker, {"event": "completed", "request_id": first_request})
        second_request = self.start_run(bridge, "second")
        self.assertNotEqual(second_request, first_request)

        self.deliver(bridge, worker, {
            "event": "usage", "request_id": first_request,
            "round_usage": {"input_tokens": 101},
        })
        self.deliver(bridge, worker, {
            "event": "context", "request_id": first_request, "source": "main",
            "used_tokens": 101,
        })
        self.assertEqual(bridge._round_usage, {})
        self.assertEqual(bridge._context, {})

        self.deliver(bridge, worker, {
            "event": "usage", "request_id": second_request,
            "round_usage": {"input_tokens": 5},
        })
        self.deliver(bridge, worker, {
            "event": "context", "request_id": second_request, "source": "main",
            "used_tokens": 6,
        })
        self.assertEqual(bridge._round_usage["input_tokens"], 5)
        self.assertEqual(bridge._context["used_tokens"], 6)

    def test_cancelled_and_error_runs_keep_identity_for_late_accounting(self):
        for terminal in ("cancelled", "error"):
            with self.subTest(terminal=terminal):
                bridge, worker = self.make_bridge()
                self.addCleanup(self.dispose_bridge, bridge)
                request = self.start_run(bridge, terminal)
                self.deliver(bridge, worker, {"event": terminal, "request_id": request})
                self.assertFalse(bridge.busy)
                self.assertEqual(bridge._request_id, request)
                self.assertEqual(bridge.debug_status()["request_id"], request)
                self.assertFalse(bridge._run_coordinator.run_active)
                self.deliver(bridge, worker, {
                    "event": "usage", "request_id": request,
                    "round_usage": {"input_tokens": 11},
                })
                self.deliver(bridge, worker, {
                    "event": "context", "request_id": request, "source": "main",
                    "used_tokens": 23,
                })
                self.assertEqual(bridge._round_usage["input_tokens"], 11)
                self.assertEqual(bridge._context["used_tokens"], 23)
                self.deliver(bridge, worker, {
                    "event": "completed", "request_id": request, "text": "late terminal",
                })
                self.assertNotEqual(bridge.statusMessage, "late terminal")

    def test_progress_reset_and_explicit_stop_clear_retained_identity(self):
        bridge, worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        request = self.start_run(bridge)
        self.deliver(bridge, worker, {"event": "completed", "request_id": request})
        self.assertEqual(bridge._request_id, request)
        bridge._load_progress()
        self.assertEqual(bridge._request_id, "")
        self.assertFalse(bridge._run_coordinator.run_active)

        stopped_request = self.start_run(bridge, "stopped")
        self.assertEqual(bridge._request_id, stopped_request)
        bridge._stop_worker()
        self.assertEqual(bridge._request_id, "")
        self.assertFalse(bridge._run_coordinator.run_active)

    def test_start_write_failure_settles_but_retains_request_identity(self):
        bridge, _worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        settlement_state = []
        bridge.taskSettled.connect(lambda *_args: settlement_state.append(
            (bridge.busy, bridge._run_coordinator.run_active)))
        bridge._write_worker = lambda _value: False

        self.assertFalse(bridge.sendMessage("write failure"))
        self.assertFalse(bridge.busy)
        self.assertTrue(bridge._request_id)
        self.assertEqual(bridge.debug_status()["request_id"], bridge._request_id)
        self.assertFalse(bridge._run_coordinator.run_active)
        self.assertFalse(bridge._run_coordinator.begin_cancel())
        self.assertEqual(settlement_state, [(False, False)])

    def test_terminal_reentrancy_does_not_finish_the_new_run(self):
        bridge, worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        first_request = self.start_run(bridge, "first")
        started = []

        def start_on_idle():
            if not bridge.busy and not started:
                started.append(bridge.sendMessage("started reentrantly"))

        bridge.busyChanged.connect(start_on_idle)
        self.deliver(bridge, worker, {"event": "completed", "request_id": first_request})

        self.assertEqual(started, [True])
        self.assertTrue(bridge.busy)
        self.assertTrue(bridge._run_coordinator.run_active)
        self.assertNotEqual(bridge._request_id, first_request)
        self.assertEqual(bridge.debug_status()["request_id"], bridge._request_id)

    def test_wrong_worker_process_is_rejected_even_with_current_request_id(self):
        bridge, worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        request = self.start_run(bridge)

        self.deliver(bridge, FakeWorker(worker.pid + 1), {
            "event": "token", "request_id": request, "text": "wrong process",
        })
        self.assertEqual(bridge._streaming_text, "")
        self.assertTrue(bridge.busy)

    def test_auxiliary_compaction_context_survives_primary_run_terminal(self):
        bridge, worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        request = self.start_run(bridge)
        bridge._compaction_request = "aux-compaction"

        self.deliver(bridge, worker, {"event": "completed", "request_id": request})
        self.assertFalse(bridge.busy)
        self.assertEqual(bridge._request_id, request)

        self.deliver(bridge, worker, {
            "event": "context", "request_id": "aux-compaction", "source": "main",
            "used_tokens": 42,
        })
        self.assertEqual(bridge._context.get("used_tokens"), 42)

        self.deliver(bridge, worker, {
            "event": "context_compaction", "request_id": "aux-compaction",
            "state": "completed", "text": "context compacted",
        })
        self.assertEqual(bridge._compaction_state.get("state"), "completed")
        self.assertEqual(bridge._compaction_request, "")

    def test_idle_auxiliary_compaction_is_routed_without_a_primary_run(self):
        bridge, worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        bridge._compaction_request = "idle-compaction"

        self.deliver(bridge, worker, {
            "event": "context", "request_id": "idle-compaction", "source": "main",
            "used_tokens": 42,
        })
        self.assertEqual(bridge._context.get("used_tokens"), 42)
        self.deliver(bridge, worker, {
            "event": "context_compaction", "request_id": "idle-compaction",
            "state": "completed", "text": "context compacted",
        })
        self.assertEqual(bridge._compaction_state.get("state"), "completed")
        self.assertEqual(bridge._compaction_request, "")

    def test_worker_exit_is_terminal_for_the_current_run(self):
        bridge, worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        self.start_run(bridge)

        self.deliver(bridge, worker, {"event": "exited", "text": "worker exited"})
        self.assertFalse(bridge.busy)
        self.assertTrue(bridge._request_id)
        self.assertFalse(bridge._run_coordinator.run_active)
        self.assertEqual(bridge.statusMessage, "worker exited")

    def test_current_worker_exit_after_settlement_still_settles_process_owner(self):
        bridge, worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, bridge)
        request = self.start_run(bridge)
        self.deliver(bridge, worker, {"event": "completed", "request_id": request})
        settled = []
        bridge._store.settle_owner = settled.append

        self.deliver(bridge, worker, {"event": "exited", "text": "worker exited"})

        self.assertTrue(settled)
        self.assertTrue(all(owner == worker.pid for owner in settled))
        self.assertFalse(bridge.busy)
        self.assertEqual(bridge._request_id, request)
        self.assertFalse(bridge._run_coordinator.run_active)
        self.assertEqual(bridge.statusMessage, "worker exited")
        bridge._set_status("unchanged")
        self.deliver(bridge, worker, {
            "event": "completed", "request_id": request, "text": "late run terminal",
        })
        self.assertEqual(bridge.statusMessage, "unchanged")

    def test_shutdown_rejects_late_worker_writeback(self):
        bridge, worker = self.make_bridge()
        request = self.start_run(bridge)
        bridge.shutdown()
        bridge._storage_pool.shutdown(wait=True, cancel_futures=True)
        before = bridge._store.messages(bridge._session)

        self.deliver(bridge, worker, {"event": "token", "request_id": request, "text": "late"})
        self.deliver(bridge, worker, {"event": "completed", "request_id": request, "text": "late"})
        self.assertEqual(bridge._store.messages(bridge._session), before)
        self.assertEqual(bridge._streaming_text, "")

    def test_bridge_instances_keep_independent_sessions_and_queues(self):
        first, first_worker = self.make_bridge()
        second, second_worker = self.make_bridge()
        self.addCleanup(self.dispose_bridge, first)
        self.addCleanup(self.dispose_bridge, second)
        first_session = first.currentSessionId
        second_session = second.currentSessionId
        self.assertNotEqual(first_session, second_session)

        self.start_run(first, "first bridge")
        self.assertTrue(first.sendMessage("first queue"))
        self.start_run(second, "second bridge")
        self.assertEqual(first.queuedCount, 1)
        self.assertEqual(second.queuedCount, 0)
        self.assertEqual(first_worker.stdin.commands()[0]["session_id"], first_session)
        self.assertEqual(second_worker.stdin.commands()[0]["session_id"], second_session)


if __name__ == "__main__":
    unittest.main()
