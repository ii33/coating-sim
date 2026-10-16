"""Offline credential/discovery regression tests. No OS secrets or live HTTP."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QByteArray, QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkReply
from coat_sim.ui import assistant_credentials as credentials
from coat_sim.ui import settings

BASE = "https://ark.cn-beijing.volces.com/api/plan/v3"
# Synthetic data only. Never read a real system backend.
SECRET = "test-only-secret-never-an-actual-key"
APP = QGuiApplication.instance() or QGuiApplication([])


class Reply(QObject):
    finished = Signal()
    readyRead = Signal()

    def __init__(self, status=200, payload=None):
        super().__init__()
        self.status = status
        self.data = json.dumps(payload or {}).encode()

    def attribute(self, _):
        return self.status

    def readAll(self):
        return QByteArray(self.data)

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def abort(self):
        pass


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for target in ("SETTINGS_PATH", "MODEL_CACHE_PATH"):
            p = patch.object(settings, target, root / (target + ".json"))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(settings, "_model_discovery_bus", None)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(settings, "read_api_key", return_value=SECRET)
        self.read_key = p.start()
        self.addCleanup(p.stop)
        p = patch.object(settings, "QNetworkAccessManager")
        self.network = p.start().return_value
        self.addCleanup(p.stop)
        self.reply = Reply()
        self.network.get.return_value = self.reply
        self.bridge = settings.SettingsBridge()
        self.bridge.setAssistantConfig(True, BASE, "manual-model")
        self.bridge._probe_coordinator._auto_probe.stop()
        self.addCleanup(self.bridge._shutdown_mcp)
        self.addCleanup(self.bridge._shutdown_probe_budget)

    def finish(self, status, payload=None):
        self.reply.status = status
        self.reply.data = json.dumps(payload or {}).encode()
        self.bridge._on_models_finished(self.reply)
        self.network.post.assert_not_called()

    def test_fatal_metadata_failures_stop_but_unsupported_list_uses_bounded_candidates(self):
        for status, payload in ((401, {"error": {"message": SECRET}}), (403, {}),
                                (429, {}), (500, {}), (None, {})):
            with self.subTest(status=status):
                self.bridge._begin_models_probe()
                self.finish(status, payload)
                self.assertFalse(self.bridge.modelDiscoveryBusy)
                self.assertEqual(self.bridge.assistantModel, "manual-model")
                self.assertNotIn(SECRET, self.bridge.modelDiscoveryMessage)
                self.assertEqual(self.bridge._probe_coordinator._probe_usage["requests"], 0)
                self.assertEqual(self.bridge._probe_coordinator._probe_secret, "")
        self.assertEqual(self.network.get.call_count, 5)

        self.bridge._begin_models_probe()
        with patch.object(self.bridge._probe_coordinator, "_next_probe_request") as advance:
            self.reply.status = 404
            self.reply.data = json.dumps({}).encode()
            self.bridge._on_models_finished(self.reply)
        self.assertTrue(advance.called)
        self.assertTrue(self.bridge._probe_coordinator._probe_candidate_fallback)
        self.assertLessEqual(len(self.bridge._probe_coordinator._probe_queue), settings.MODEL_REQUEST_LIMIT)
        self.assertEqual(self.bridge._probe_coordinator._probe_queue[0]["model"], "manual-model")
        self.assertEqual(self.bridge.assistantModel, "manual-model")
        self.network.post.assert_not_called()
        self.bridge.cancelModelProbe()

    def test_plan_path_header_and_success(self):
        self.bridge._begin_models_probe()
        request = self.network.get.call_args.args[0]
        self.assertEqual(request.url().toString(), BASE + "/models")
        self.assertTrue(request.hasRawHeader("Authorization"))
        self.finish(200, {"data": [{"id": "listed-model"}]})
        self.assertEqual(self.bridge.availableModels[0]["id"], "listed-model")
        self.assertIn("未验证聊天", self.bridge.modelDiscoveryMessage)

    def test_remote_errors_are_distinct(self):
        for status in (401, 403, 429):
            self.bridge._begin_models_probe()
            self.finish(status)
            self.assertIn(f"HTTP {status}", self.bridge.modelDiscoveryMessage)

    def test_missing_key_sends_nothing(self):
        self.read_key.return_value = None
        self.bridge.discoverModels()
        self.network.get.assert_not_called()
        self.assertIn("没有可用密钥", self.bridge.modelDiscoveryMessage)

    def test_secure_store_failure_sends_nothing_and_hides_exception(self):
        self.read_key.side_effect = OSError(SECRET)
        self.bridge.discoverModels()
        self.network.get.assert_not_called()
        self.assertIn("凭据库", self.bridge.modelDiscoveryMessage)
        self.assertNotIn(SECRET, self.bridge.modelDiscoveryMessage)
        self.assertIsNone(settings.ModelDiscoveryBus.credential_fingerprint(BASE))

    def test_loopback_anonymous_works_without_secure_backend(self):
        self.read_key.side_effect = OSError(SECRET)
        self.bridge.setAssistantConfig(True, "http://127.0.0.1:11434/v1", "local")
        self.bridge._begin_models_probe()
        request = self.network.get.call_args.args[0]
        self.assertFalse(request.hasRawHeader("Authorization"))
        self.finish(200, {"data": [{"id": "local"}]})
        self.assertEqual(len(self.bridge.availableModels), 1)

    def test_non_loopback_is_not_implicitly_anonymous(self):
        for host in ("192.168.1.2", "127.0.0.1.example.com", "localhost.example.com"):
            self.assertFalse(settings._local_anonymous_endpoint("http://" + host))
        for host in ("localhost", "127.0.0.1", "[::1]"):
            self.assertTrue(settings._local_anonymous_endpoint("http://" + host))

    def test_automatic_reasoning_is_blocked(self):
        with patch.object(self.bridge._discovery_bus, "models_fresh", return_value=True), \
                patch.object(self.bridge._probe_coordinator, "_begin_reasoning_probe") as reasoning:
            self.bridge._auto_discover()
            reasoning.assert_not_called()
        self.assertFalse(self.bridge._start_probe("reasoning", automatic=True))
        self.network.post.assert_not_called()

    def test_repeated_clicks_and_cancel_do_not_duplicate_requests(self):
        self.bridge._begin_models_probe()
        self.bridge._begin_models_probe()
        self.assertEqual(self.network.get.call_count, 1)
        self.bridge.cancelModelProbe()
        self.assertFalse(self.bridge.modelDiscoveryBusy)
        self.assertEqual(self.bridge._probe_coordinator._probe_secret, "")
        self.bridge._on_models_finished(self.reply)
        self.network.post.assert_not_called()

    def test_credential_change_stops_request_before_send(self):
        self.assertTrue(self.bridge._start_probe("models"))
        self.read_key.return_value = "different-test-key"
        self.bridge._send_probe_request({"kind": "metadata"})
        self.network.get.assert_not_called()
        self.assertFalse(self.bridge.modelDiscoveryBusy)
        self.assertIn("密钥已变更", self.bridge.modelDiscoveryMessage)

    def test_manual_reasoning_remains_explicit(self):
        with patch.object(self.bridge._probe_coordinator, "_next_probe_request") as send:
            self.bridge._begin_reasoning_probe()
            send.assert_called_once()
            self.assertEqual(self.bridge._probe_coordinator._probe_kind, "reasoning")
            self.assertEqual([task["kind"] for task in self.bridge._probe_coordinator._probe_queue],
                             ["effort_control", "thinking_control"])
        self.bridge.cancelModelProbe()

    def test_key_save_and_delete_feedback_is_sanitized(self):
        with patch.object(settings, "save_api_key", side_effect=OSError(SECRET)):
            self.assertFalse(self.bridge.saveAssistantApiKey(SECRET))
        self.assertIn("密钥未保存", self.bridge.assistantKeyMessage)
        self.assertNotIn(SECRET, self.bridge.assistantKeyMessage + self.bridge.statusMessage)
        with patch.object(settings, "save_api_key"):
            self.assertTrue(self.bridge.saveAssistantApiKey(SECRET))
        self.assertIn("已安全保存", self.bridge.assistantKeyMessage)
        with patch.object(settings, "delete_api_key", side_effect=OSError(SECRET)):
            self.assertFalse(self.bridge.clearAssistantApiKey())
        self.assertNotIn(SECRET, self.bridge.assistantKeyMessage + self.bridge.statusMessage)
        with patch.object(settings, "delete_api_key"):
            self.assertTrue(self.bridge.clearAssistantApiKey())
        self.assertIn("已删除", self.bridge.assistantKeyMessage)

    def test_key_save_categories_reach_near_field_and_status(self):
        for category in credentials._STORE_ERRORS:
            with self.subTest(category=category):
                error = credentials.CredentialStoreError(category)
                with patch.object(settings, "save_api_key", side_effect=error):
                    self.assertFalse(self.bridge.saveAssistantApiKey(SECRET))
                self.assertIn(f"（{category}）", self.bridge.assistantKeyMessage)
                self.assertEqual(self.bridge.assistantKeyMessage, self.bridge.statusMessage)
                self.assertNotIn(SECRET, self.bridge.assistantKeyMessage)


class CredentialTests(unittest.TestCase):
    def test_chainer_selects_only_secure_backend(self):
        secure = type("FakeSecure", (), {"__module__": "keyring.backends.SecretService", "priority": 5})()
        plaintext = type("FakePlain", (), {"__module__": "keyrings.alt.file", "priority": 10})()
        chain = MagicMock(backends=[plaintext, secure])
        with patch("keyring.get_keyring", return_value=chain):
            self.assertIs(credentials._backend(), secure)
        chain.get_password.assert_not_called()
        chain.set_password.assert_not_called()
        with patch("keyring.get_keyring", return_value=plaintext):
            with self.assertRaises(credentials.CredentialStoreError) as result:
                credentials._backend()
            self.assertEqual(result.exception.category, "unavailable")

    def test_save_error_categories_never_expose_exception_details(self):
        from keyring.errors import KeyringLocked, InitError, PasswordSetError, NoKeyringError
        PromptDismissedException = type("PromptDismissedException", (Exception,), {"__module__": "secretstorage.exceptions"})
        SecretServiceNotAvailableException = type("SecretServiceNotAvailableException", (Exception,), {"__module__": "secretstorage.exceptions"})
        DBusErrorResponse = type("DBusErrorResponse", (Exception,), {"__module__": "jeepney.wrappers"})
        cases = [(KeyringLocked(SECRET), "locked"),
                 (InitError(SECRET), "init"),
                 (PasswordSetError(SECRET), "write_failed"),
                 (NoKeyringError(SECRET), "unavailable"),
                 (PromptDismissedException(SECRET), "cancelled"),
                 (SecretServiceNotAvailableException(SECRET), "dbus"),
                 (DBusErrorResponse(SECRET), "dbus"),
                 (PermissionError(SECRET), "access_denied"),
                 (RuntimeError(SECRET), "unknown")]
        for exc, category in cases:
            with self.subTest(category=category):
                backend = MagicMock()
                backend.set_password.side_effect = exc
                with patch.object(credentials, "_backend", return_value=backend):
                    with self.assertRaises(credentials.CredentialStoreError) as result:
                        credentials.save_api_key(SECRET, BASE)
                self.assertEqual(result.exception.category, category)
                message = credentials.credential_save_error_message(result.exception)
                self.assertIn(category, message)
                self.assertNotIn(SECRET, message)
                backend.get_password.assert_not_called()

    def test_dbus_subcodes_are_allowlisted_and_survive_wrapping(self):
        from keyring.errors import InitError
        DbusError = type("DBusErrorResponse", (Exception,), {"__module__": "jeepney.wrappers"})
        for name, code in credentials._DBUS_CODES.items():
            inner = DbusError(SECRET)
            inner.name = name
            outer = InitError(SECRET)
            outer.__cause__ = inner
            result = credentials._store_error(outer)
            self.assertEqual(result.category, "dbus")
            self.assertEqual(result.dbus_code, code)
            self.assertEqual(result.dbus_error_name, name)
            self.assertIn("dbus:" + code, credentials.credential_save_error_message(result))
            self.assertNotIn(SECRET, credentials.credential_save_error_message(result))
        inner.name = SECRET
        self.assertIsNone(credentials._store_error(inner).dbus_code)
        self.assertNotIn(SECRET, credentials.credential_save_error_message(credentials._store_error(inner)))
        inner.name = object()
        self.assertIsNone(credentials._store_error(inner).dbus_code)

    def test_dbus_python_name_only_and_faulting_accessor(self):
        DbusError = type("DBusException", (Exception,), {
            "__module__": "dbus.exceptions",
            "get_dbus_name": lambda self: "org.freedesktop.DBus.Error.UnknownObject",
        })
        self.assertEqual(credentials._store_error(DbusError(SECRET)).dbus_code, "unknown_object")
        def fail(self):
            raise ValueError(SECRET)
        DbusError.get_dbus_name = fail
        result = credentials._store_error(DbusError(SECRET))
        self.assertIsNone(result.dbus_code)
        self.assertNotIn(SECRET, credentials.credential_save_error_message(result))

    def test_safe_stage_uses_code_identifiers_not_exception_details(self):
        for (suffix, function), expected in credentials._STAGE_FRAMES.items():
            namespace = {"error": RuntimeError(SECRET)}
            code = compile("def " + function + "():\n    raise error\n", "/synthetic/" + suffix, "exec")
            exec(code, namespace)
            try:
                namespace[function]()
            except RuntimeError as exc:
                result = credentials._store_error(exc)
                self.assertEqual(result.stage, expected)
                self.assertNotIn(SECRET, credentials.credential_save_error_message(result))
        result = credentials.CredentialStoreError("dbus", "no_reply", "collection")
        self.assertIn("dbus:no_reply;collection", credentials.credential_save_error_message(result))
        result = credentials.CredentialStoreError("dbus", SECRET, SECRET)
        self.assertIsNone(result.stage)
        self.assertIsNone(result.dbus_code)

    def test_selection_stage_survives_outer_write_wrapper(self):
        inner = credentials.CredentialStoreError("unavailable", stage="backend-selection")
        result = credentials._store_error(inner, stage="write")
        self.assertEqual(result.stage, "backend-selection")

    def test_nested_and_cyclic_causes_are_safe(self):
        from keyring.errors import InitError
        SecretServiceNotAvailableException = type("SecretServiceNotAvailableException", (Exception,), {"__module__": "secretstorage.exceptions"})
        inner = SecretServiceNotAvailableException(SECRET)
        outer = InitError(SECRET)
        outer.__cause__ = inner
        self.assertEqual(credentials._store_error(outer).category, "dbus")
        inner.__cause__ = outer
        self.assertNotIn(SECRET, credentials.credential_save_error_message(OSError(SECRET)))
        self.assertIn(credentials._store_error(outer).category, ("init", "dbus"))

    def test_unknown_exception_class_name_is_not_rendered(self):
        unknown = type(SECRET, (Exception,), {"__module__": SECRET})(SECRET)
        result = credentials._store_error(unknown)
        self.assertEqual(result.category, "unknown")
        self.assertNotIn(SECRET, credentials.credential_save_error_message(result))

    def test_strict_read_distinguishes_store_unavailable(self):
        with patch.object(credentials, "_backend", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                credentials.read_api_key(BASE, strict=True)
            self.assertIsNone(credentials.read_api_key(BASE))

    def test_missing_key_is_not_store_failure(self):
        backend = MagicMock()
        backend.get_password.return_value = None
        with patch.object(credentials, "_backend", return_value=backend), \
                patch.object(credentials, "_saved_endpoint", return_value=None):
            self.assertIsNone(credentials.read_api_key(BASE, strict=True))


if __name__ == "__main__":
    unittest.main()
