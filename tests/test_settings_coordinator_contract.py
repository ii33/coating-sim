"""Behavior contracts for SettingsBridge model-probe coordination.

All provider, credential, settings, cache and budget dependencies are local
fakes or temporary files. These tests exercise real Qt bridge instances.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QObject, QCoreApplication, QEvent, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkReply
import shiboken6

from coat_sim.ui import settings


APP = QGuiApplication.instance() or QGuiApplication([])
API_URL = "https://settings-coordinator.fixture.invalid/v1"
SECRET = "settings-coordinator-fixture-secret"


class Reply(QObject):
    finished = Signal()
    readyRead = Signal()

    def __init__(self, status=200, payload=None):
        super().__init__()
        self.status = status
        self.data = json.dumps(payload or {}).encode()
        self.aborted = False
        self.deleted = False

    def attribute(self, _):
        return self.status

    def readAll(self):
        return QByteArray(self.data)

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def abort(self):
        self.aborted = True

    def deleteLater(self):
        self.deleted = True


class FakeNetwork:
    def __init__(self):
        self.calls = []
        self.replies = []

    def get(self, request):
        reply = Reply()
        self.calls.append(("GET", request))
        self.replies.append(reply)
        return reply

    def post(self, request, body):
        reply = Reply()
        self.calls.append(("POST", request, body))
        self.replies.append(reply)
        return reply


class ReadyFuture:
    def __init__(self, value):
        self.value = value

    def done(self):
        return True

    def result(self):
        return self.value

    def cancel(self):
        return False


class CapturingExecutor:
    def __init__(self):
        self.submissions = []

    def submit(self, operation, *args):
        self.submissions.append((operation, args))
        return object()


class SettingsCoordinatorContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.patches = [
            patch.object(settings, "SETTINGS_PATH", root / "settings.json"),
            patch.object(settings, "MODEL_CACHE_PATH", root / "cache.json"),
            patch.object(settings, "_model_discovery_bus", None),
            patch.object(settings, "read_api_key", return_value=SECRET),
            patch.object(settings, "QNetworkAccessManager", side_effect=self._new_network),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.networks = []
        self.network_parents = []
        self.bridge = self._bridge()
        self.bridge.setAssistantConfig(True, API_URL, "fixture-model")
        self.bridge._probe_coordinator._auto_probe.stop()

    def _new_network(self, *args, **_kwargs):
        if args:
            self.network_parents.append(args[0])
        network = FakeNetwork()
        self.networks.append(network)
        return network

    def _bridge(self):
        bridge = settings.SettingsBridge()
        self.addCleanup(self._shutdown_bridge, bridge)
        return bridge

    @staticmethod
    def _shutdown_bridge(bridge):
        if shiboken6.isValid(bridge):
            bridge._shutdown_mcp()
            bridge._shutdown_key_lookup()
            bridge._shutdown_probe_budget()

    @property
    def network(self):
        return self.networks[-1]

    def test_probe_resources_are_owned_per_bridge(self):
        first = self.bridge._probe_coordinator
        self.assertIs(first.parent(), self.bridge)
        self.assertIs(self.network_parents[-1], first)
        for timer in (first._model_timeout, first._probe_overall_timeout,
                      first._auto_probe, first._budget_poll):
            self.assertIs(timer.parent(), first)

        second_bridge = self._bridge()
        second = second_bridge._probe_coordinator
        self.assertIsNot(second, first)
        self.assertIs(second.parent(), second_bridge)
        self.assertIsNot(second._budget_executor, first._budget_executor)
        self.assertIsNot(second._network, first._network)
        self.assertIs(self.network_parents[-1], second)

    def test_probe_facing_qt_properties_and_commands_remain_on_bridge(self):
        meta = settings.SettingsBridge.staticMetaObject
        expected_properties = {
            "availableModels": ("QVariantList", "modelDiscoveryChanged()"),
            "assistantNativeReasoningOptions": ("QStringList", "assistantStateChanged()"),
            "modelDiscoveryBusy": ("bool", "modelDiscoveryChanged()"),
            "modelDiscoveryKind": ("QString", "modelDiscoveryChanged()"),
            "modelDiscoveryMessage": ("QString", "modelDiscoveryChanged()"),
            "probeResults": ("QVariantList", "modelDiscoveryChanged()"),
            "probeUsage": ("QString", "modelDiscoveryChanged()"),
        }
        for name, (type_name, notify_signature) in expected_properties.items():
            index = meta.indexOfProperty(name)
            self.assertGreaterEqual(index, 0, name)
            prop = meta.property(index)
            self.assertEqual(prop.typeName(), type_name, name)
            self.assertFalse(prop.isWritable(), name)
            self.assertEqual(prop.notifySignal().methodSignature().data().decode(),
                             notify_signature, name)

        for signature in ("discoverModels()", "discoverReasoning()",
                          "cancelModelProbe()", "closeModelProbe()",
                          "closeAssistantSettingsBootstrap()"):
            self.assertGreaterEqual(meta.indexOfSlot(signature), 0, signature)

    def test_late_reply_from_cancelled_run_cannot_write_into_new_run(self):
        self.bridge._begin_models_probe()
        old_reply = self.network.replies[-1]
        old_run = self.bridge._probe_coordinator._probe_request_id
        self.bridge.cancelModelProbe()
        self.assertTrue(old_reply.aborted)

        self.bridge._begin_models_probe()
        new_reply = self.network.replies[-1]
        new_run = self.bridge._probe_coordinator._probe_request_id
        self.assertNotEqual(old_run, new_run)
        before = (self.bridge._probe_coordinator._probe_results[:], self.bridge._probe_coordinator._available_models[:],
                  self.bridge.modelDiscoveryMessage)

        old_reply.status = 200
        old_reply.data = json.dumps({"data": [{"id": "stale-model"}]}).encode()
        self.bridge._on_models_finished(old_reply)

        self.assertIs(self.bridge._probe_coordinator._model_reply, new_reply)
        self.assertEqual((self.bridge._probe_coordinator._probe_results, self.bridge._probe_coordinator._available_models,
                          self.bridge.modelDiscoveryMessage), before)
        self.assertEqual(len(self.network.calls), 2)
        self.bridge.cancelModelProbe()

    def test_native_reasoning_options_property_returns_shared_bus_values(self):
        meta_property = settings.SettingsBridge.staticMetaObject.property(
            settings.SettingsBridge.staticMetaObject.indexOfProperty(
                "assistantNativeReasoningOptions"
            )
        )
        self.assertEqual(meta_property.typeName(), "QStringList")
        self.assertFalse(meta_property.isWritable())
        self.assertEqual(meta_property.notifySignal().name(), b"assistantStateChanged")

        fingerprint = "fixture-credential-fingerprint"
        self.bridge._assistant_settings_requested = True
        self.bridge._assistant_credential_fingerprint = fingerprint
        bus = self.bridge._discovery_bus
        with patch.object(
            bus, "reasoning_for_fingerprint",
            return_value={"native_options": ["low", "high"],
                          "thinking_options": ["enabled"]},
        ) as read_reasoning:
            self.assertEqual(
                self.bridge.assistantNativeReasoningOptions,
                ["low", "high", "thinking:enabled"],
            )
        read_reasoning.assert_called_once_with(
            self.bridge.assistantApiUrl, self.bridge.assistantModel, fingerprint
        )

    def test_stale_credential_lookup_does_not_install_or_claim(self):
        self.bridge._assistant_settings_requested = True
        self.bridge._credential_future = ReadyFuture(
            (True, False, "old-fingerprint", {}, "old-signature", SECRET))
        self.bridge._credential_request = (self.bridge._credential_generation - 1, API_URL)

        self.bridge._on_bound_key_lookup_ready()

        self.assertIsNone(self.bridge._assistant_credential_fingerprint)
        self.assertFalse(self.bridge._assistant_key_status_ready)
        self.assertEqual(self.bridge._probe_coordinator._network.calls, [])
        self.assertFalse(self.bridge._discovery_bus._active)

    def test_stale_budget_reservation_callback_only_settles_unsent_reservation(self):
        self.bridge._probe_coordinator._model_discovery_busy = True
        self.bridge._probe_coordinator._probe_kind = "models"
        self.bridge._probe_coordinator._probe_request_id = "old-run"
        self.bridge._probe_coordinator._probe_model = "fixture-model"
        self.bridge._probe_coordinator._probe_queue = [{"kind": "model", "model": "fixture-model"}]
        self.bridge._probe_coordinator._probe_usage = {"requests": 0, "input_tokens": 0,
                                    "output_tokens": 0, "unreported_requests": 0}
        self.bridge._probe_coordinator._probe_credentials_unchanged = lambda: True
        submitted = []
        self.bridge._probe_coordinator._submit_probe_budget = lambda operation, *args, callback=None: submitted.append(
            (operation, args, callback))

        self.bridge._next_probe_request()
        _reserve, _args, callback = submitted.pop()
        self.bridge._probe_coordinator._probe_request_id = "new-run"
        self.bridge._probe_coordinator._probe_queue = [{"kind": "metadata"}]
        self.bridge._probe_coordinator._network = self.network
        callback(("reservation-fixture", 100))

        self.assertEqual(self.network.calls, [])
        self.assertEqual(len(submitted), 1)
        operation, args, _ = submitted[0]
        self.assertEqual(operation.__name__, "_settle_probe_budget")
        self.assertIs(operation.__self__, self.bridge._probe_coordinator)
        self.assertEqual(args, ("reservation-fixture", "probe:old-run", "fixture-model",
                                None, None, False))

    def test_two_bridges_share_one_endpoint_claim_and_release_allows_retry(self):
        first_network = self.network
        second = self._bridge()
        second.setAssistantConfig(True, API_URL, "fixture-model")
        second._probe_coordinator._auto_probe.stop()
        shared_bus = self.bridge._discovery_bus
        self.assertIs(second._discovery_bus, shared_bus)

        self.bridge._begin_models_probe()
        first_reply = first_network.replies[-1]
        second._begin_models_probe()
        self.assertEqual(len(first_network.calls), 1)
        self.assertEqual(len(self.networks[-1].calls), 0)
        self.assertIn("另一窗口", second.modelDiscoveryMessage)

        self.bridge.cancelModelProbe()
        second._begin_models_probe()
        self.assertEqual(len(self.networks[-1].calls), 1)
        second.cancelModelProbe()
        self.assertTrue(first_reply.aborted)

    def test_draft_defaults_and_failed_save_preserve_edit_until_reset(self):
        original = settings.normalized_ui_settings()
        settings.save_ui_settings(original)
        self.bridge.setFontPointSize(17)
        self.assertEqual(self.bridge.fontPointSize, 17)
        with patch.object(settings, "save_ui_settings", side_effect=OSError("fixture disk failure")):
            self.assertFalse(self.bridge.save())
        self.assertEqual(self.bridge.fontPointSize, 17)
        self.assertIn("保存失败", self.bridge.statusMessage)
        self.assertEqual(json.loads(settings.SETTINGS_PATH.read_text()), original)

        self.bridge.restoreDefaults()
        self.assertEqual(self.bridge.fontPointSize, settings.DEFAULT_FONT_POINT_SIZE)
        self.assertEqual(json.loads(settings.SETTINGS_PATH.read_text()), original)

    def test_restore_defaults_cancels_inflight_probe_and_releases_endpoint(self):
        self.bridge._begin_models_probe()
        reply = self.network.replies[-1]
        bus = self.bridge._discovery_bus
        self.assertTrue(bus._active)

        self.bridge.restoreDefaults()

        self.assertTrue(reply.aborted)
        self.assertFalse(self.bridge.modelDiscoveryBusy)
        self.assertFalse(bus._active)
        self.assertEqual(self.bridge.assistantApiUrl, "")

    def test_close_cancels_probe_and_invalidates_credential_delivery(self):
        self.bridge._begin_models_probe()
        reply = self.network.replies[-1]
        self.bridge.closeModelProbe()
        self.assertTrue(reply.aborted)
        self.assertFalse(self.bridge.modelDiscoveryBusy)
        self.assertTrue(self.bridge._probe_coordinator._probe_cancel_event.is_set())

        self.bridge._credential_future = ReadyFuture(
            (True, False, "late-fingerprint", {}, "late-signature", SECRET))
        self.bridge._credential_request = (self.bridge._credential_generation, API_URL)
        self.bridge.closeAssistantSettingsBootstrap()
        self.bridge._on_bound_key_lookup_ready()
        self.assertIsNone(self.bridge._assistant_credential_fingerprint)
        self.assertFalse(self.bridge._assistant_key_status_ready)

    def test_destroy_disconnects_reply_and_settles_each_reservation_once(self):
        self.bridge._begin_models_probe()
        reply = self.network.replies[-1]
        self.bridge._probe_coordinator._budget_reservations = {
            "reservation-fixture": {"session": "probe:destroyed-run",
                                     "model": "fixture-model", "posted": True}
        }
        self.bridge._probe_coordinator._probe_request_id = "destroyed-run"
        executor = CapturingExecutor()
        self.bridge._probe_coordinator._budget_executor = executor
        bus = self.bridge._discovery_bus
        published = []
        original_publish = bus.publish
        bus.publish = lambda *args, **kwargs: published.append((args, kwargs))

        self.bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        APP.processEvents()
        self.assertFalse(shiboken6.isValid(self.bridge))
        self.assertEqual(len(executor.submissions), 1)
        operation, args = executor.submissions[0]
        self.assertEqual(operation.__name__, "_settle_probe_budget")
        self.assertEqual(args, ("reservation-fixture", "probe:destroyed-run",
                                "fixture-model", None, None, True))
        self.assertFalse(bus._active)

        reply.status = 200
        reply.data = json.dumps({"data": [{"id": "after-destroy"}]}).encode()
        reply.finished.emit()
        self.assertEqual(published, [])
        bus.publish = original_publish


if __name__ == "__main__":
    unittest.main()
