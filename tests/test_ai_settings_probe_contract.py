"""Offline behavior contract for automatic model discovery.

The fixture calls the real SettingsBridge methods on SimpleNamespace objects;
it never constructs the QObject bridge, opens the system keyring, or contacts a
provider. All credential material and network replies are local fakes.
"""
from __future__ import annotations

import time
import unittest
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import coat_sim.ui.settings as settings
from coat_sim.ui.settings import SettingsBridge
from coat_sim.ui.settings_coordinator import SettingsProbeCoordinator, SettingsProbeDependencies

# This module is collected before the GUI suites. Qt cannot upgrade an existing
# QCoreApplication; keep one GUI/widget-capable application for later modules.
APP = QApplication.instance() or QApplication([])


API_URL = "https://settings-probe-fixture.invalid/v1"
FINGERPRINT = "fixture-fingerprint-only"
FAKE_SECRET = "fixture-secret-never-sent"


class _Signal:
    def __init__(self):
        self.emitted = 0
        self.callbacks = []

    def emit(self, *args):
        self.emitted += 1

    def connect(self, callback):
        self.callbacks.append(callback)


class _Timer:
    def __init__(self):
        self.active = False
        self.started = []
        self.stopped = 0

    def start(self, milliseconds=None):
        self.active = True
        self.started.append(milliseconds)

    def stop(self):
        self.active = False
        self.stopped += 1

    def isActive(self):
        return self.active


class _Future:
    def __init__(self, value):
        self.value = value

    def done(self):
        return True

    def result(self):
        return self.value


class _DiscoveryBus:
    def __init__(self, *, fresh=False):
        self.fresh = fresh
        self.snapshots = []
        self.claims = []
        self.published = []
        self.model_reads = []
        self.freshness_checks = []

    def install_cache_snapshot(self, entries, signature):
        self.snapshots.append((entries, signature))

    def models_for_fingerprint(self, api_url, fingerprint, *, reload_cache=False):
        self.model_reads.append((api_url, fingerprint, reload_cache))
        return [{"id": "fixture-model"}]

    def models_fresh_for_fingerprint(self, api_url, fingerprint, *, reload_cache=False):
        self.freshness_checks.append((api_url, fingerprint, reload_cache))
        return self.fresh

    def claim_probe(self, base_url, owner):
        self.claims.append((base_url, owner))
        return True

    def publish(self, api_url, models, *, completed, expected_fingerprint):
        self.published.append((api_url, models, completed, expected_fingerprint))


class _Reply:
    def __init__(self):
        self.finished = _Signal()
        self.readyRead = _Signal()


class _Network:
    """Capture QNetwork calls without sending anything to a socket."""

    def __init__(self):
        self.calls = []

    def get(self, request):
        self.calls.append(("GET", request))
        return _Reply()

    def post(self, request, body):
        self.calls.append(("POST", request, body))
        return _Reply()


def _credential_result(*, has_key=True, fingerprint=FINGERPRINT):
    # This is deliberately recognizable dummy data, not a system credential.
    return has_key, False, fingerprint, {"fixture": []}, "fixture-cache-signature", FAKE_SECRET if has_key else ""


def _callback_bridge(*, fresh=False, has_key=True, fingerprint=FINGERPRINT,
                     enabled=True, closed=False, paused_until=0.0, busy=False,
                     request_generation=7, active_generation=7,
                     request_url=API_URL, current_url=API_URL):
    bus = _DiscoveryBus(fresh=fresh)
    network = _Network()
    bridge = SimpleNamespace(
        assistantApiUrl=current_url,
        assistantModel="fixture-model",
        assistantEnabled=enabled,
        modelDiscoveryBusy=busy,
        _credential_future=_Future(_credential_result(has_key=has_key, fingerprint=fingerprint)),
        _credential_request=(request_generation, request_url),
        _credential_request_reason="auto_probe",
        _credential_generation=active_generation,
        _credential_poll=_Timer(),
        _discovery_bus=bus,
        _assistant_settings_requested=True,
        _assistant_key_status_ready=False,
        _assistant_credential_api_url=None,
        _assistant_credential_secret="",
        _assistant_credential_fingerprint=None,
        _has_assistant_key=False,
        _settings={},
        assistantStateChanged=_Signal(),
        modelDiscoveryChanged=_Signal(),
        _set_key_message=lambda message: None,
        _set_model_discovery_message=lambda message: None,
        _schedule_auto_probe=lambda: None,
        _refresh_bound_key=lambda **kwargs: None,
    )
    bridge._probe_coordinator = SettingsProbeCoordinator(
        bridge,
        SettingsProbeDependencies(
            validate_assistant_url=settings.validate_assistant_url,
            local_anonymous_endpoint=settings._local_anonymous_endpoint,
            read_api_key=settings.read_api_key,
            provider_profile_for_url=settings.provider_profile_for_url,
            network_manager_factory=lambda _parent: network,
            auto_probe_debounce_ms=settings.AUTO_PROBE_DEBOUNCE_MS,
            model_cache_retry_seconds=settings.MODEL_CACHE_RETRY_SECONDS,
            max_model_reply_bytes=settings.MAX_MODEL_REPLY_BYTES,
        ),
    )
    coordinator = bridge._probe_coordinator
    coordinator._probe_closed = closed
    coordinator._auto_paused_until = paused_until
    coordinator._model_discovery_busy = busy
    coordinator._probe_credentials_unchanged = lambda: True
    bridge._begin_models_probe = coordinator._begin_models_probe
    return bridge


class AutomaticModelProbeContractTests(unittest.TestCase):
    def test_collection_application_supports_later_gui_modules(self):
        # instance() on a Qt subclass can return a weaker base-class singleton.
        # The probe fixture must never poison later font/QML/widget collection.
        self.assertIsInstance(APP, QApplication)

    def test_auto_discover_refreshes_stale_bound_key_then_only_gets_models(self):
        bridge = _callback_bridge(fresh=False)
        refreshes = []
        records = []
        finished = []
        bridge._credential_future = None
        bridge._credential_request = None
        bridge._assistant_key_status_ready = True
        bridge._assistant_credential_api_url = API_URL
        bridge._assistant_credential_secret = FAKE_SECRET
        bridge._assistant_credential_fingerprint = FINGERPRINT
        bridge._has_assistant_key = True
        bridge.modelDiscoveryBusy = False
        bridge._refresh_bound_key = lambda **kwargs: refreshes.append(kwargs)

        bridge._probe_coordinator._auto_discover()

        self.assertEqual(refreshes, [{"force": True, "reason": "auto_probe"}])
        self.assertEqual(bridge._probe_coordinator._network.calls, [])

        # Complete the off-thread lookup with fake data and run the real callback.
        bridge._credential_future = _Future(_credential_result())
        bridge._credential_request = (7, API_URL)
        bridge._credential_request_reason = "auto_probe"
        bridge._credential_generation = 7
        SettingsBridge._on_bound_key_lookup_ready(bridge)

        self.assertEqual(len(bridge._discovery_bus.claims), 1)
        self.assertEqual([call[0] for call in bridge._probe_coordinator._network.calls], ["GET"])
        request_url = bridge._probe_coordinator._network.calls[0][1].url().toString()
        self.assertTrue(request_url.endswith("/models"), request_url)
        self.assertEqual(bridge._probe_coordinator._probe_queue, [])
        self.assertTrue(bridge._probe_coordinator._probe_automatic)

        bridge._probe_coordinator._record_probe = lambda *args: records.append(args)
        bridge._probe_coordinator._finish_probe = lambda *args, **kwargs: finished.append((args, kwargs))
        bridge._probe_coordinator._queue_model_candidate_fallback( "fixture list response")

        self.assertEqual([call[0] for call in bridge._probe_coordinator._network.calls], ["GET"])
        self.assertFalse(bridge._probe_coordinator._probe_candidate_fallback)
        self.assertTrue(any("candidate POSTs are manual-only" in row[3] for row in records))
        self.assertTrue(finished)

    def test_auto_discover_does_not_refresh_or_probe_without_current_key(self):
        bridge = _callback_bridge()
        refreshes = []
        bridge._credential_future = None
        bridge._credential_request = None
        bridge._assistant_key_status_ready = True
        bridge._assistant_credential_api_url = API_URL
        bridge._has_assistant_key = False
        bridge._assistant_credential_fingerprint = None
        bridge._refresh_bound_key = lambda **kwargs: refreshes.append(kwargs)
        bridge.modelDiscoveryBusy = False

        bridge._probe_coordinator._auto_discover()

        self.assertEqual(refreshes, [])
        self.assertEqual(bridge._discovery_bus.freshness_checks, [])
        self.assertEqual(bridge._probe_coordinator._network.calls, [])

    def test_callback_discards_stale_generation_and_endpoint_results(self):
        cases = (
            {"request_generation": 6, "active_generation": 7},
            {"request_url": "https://old-endpoint.invalid/v1", "current_url": API_URL},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                bridge = _callback_bridge(**overrides)
                SettingsBridge._on_bound_key_lookup_ready(bridge)
                self.assertEqual(bridge._discovery_bus.snapshots, [])
                self.assertEqual(bridge._discovery_bus.claims, [])
                self.assertEqual(bridge._probe_coordinator._network.calls, [])

    def test_callback_gates_prevent_probe_when_any_required_condition_fails(self):
        cases = (
            ("assistant disabled", {"enabled": False}, 0),
            ("probe closed", {"closed": True}, 0),
            ("automatic probe paused", {"paused_until": time.time() + 600}, 0),
            ("key missing", {"has_key": False, "fingerprint": None}, 0),
            ("fingerprint missing", {"fingerprint": None}, 0),
            ("probe busy", {"busy": True}, 0),
            ("fresh models", {"fresh": True}, 1),
        )
        for label, overrides, expected_freshness_checks in cases:
            with self.subTest(gate=label):
                bridge = _callback_bridge(**overrides)
                SettingsBridge._on_bound_key_lookup_ready(bridge)
                self.assertEqual(len(bridge._discovery_bus.freshness_checks),
                                 expected_freshness_checks)
                self.assertEqual(bridge._probe_coordinator._network.calls, [])
                self.assertEqual(bridge._discovery_bus.claims, [])


if __name__ == "__main__":
    unittest.main()
