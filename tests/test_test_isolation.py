"""Fast safety contracts for the Qt and launcher test isolation helpers."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
import os
import gc

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSettings, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from tests import qt_test_guard as package_qt_test_guard
import qt_test_guard
from tests.launcher_test_isolation import LauncherTestIsolation

from coat_sim.core import data_files
from coat_sim.scheduler import SchedulerClient
from coat_sim.ui import assistant_credentials, launcher as launcher_module, settings
from coat_sim.ui.assistant import AssistantBridge
from coat_sim.ui.control import UiControlServer
from coat_sim.ui import window_geometry


APP = QGuiApplication.instance() or QGuiApplication([])


class QtTestGuardTests(unittest.TestCase):
    def test_qml_window_lookup_preserves_wrapper_ownership_and_popup_input(self):
        engine = QQmlEngine()
        factory = qt_test_guard.NoNetworkAccessManagerFactory()
        engine.setNetworkAccessManagerFactory(factory)
        component = QQmlComponent(engine)
        component.setData(b'''import QtQuick
import QtQuick.Controls
import QtQuick.Window
Window {
    id: host; width: 320; height: 240; visible: true
    property int hostKeyCount: 0
    property int popupKeyCount: 0
    Item { objectName: "hostTarget" }
    Popup {
        id: popup; objectName: "popup"; popupType: Popup.Window
        width: 180; height: 100; focus: true
        contentItem: Item {
            objectName: "popupTarget"; focus: true
            Keys.onPressed: function(event) {
                if (event.key === Qt.Key_A) {
                    host.popupKeyCount += 1; event.accepted = true
                }
            }
        }
    }
    Component.onCompleted: popup.open()
}''', QUrl("file:///qt-test/window-lookup.qml"))
        self.assertFalse(component.isError(), component.errors())
        host = component.create()
        self.assertIsNotNone(host, component.errors())
        try:
            APP.processEvents()
            host_item = host.findChild(QQuickItem, "hostTarget")
            popup_item = host.findChild(QQuickItem, "popupTarget")
            self.assertIs(qt_test_guard.qml_item_window(host_item), host)
            popup_window = qt_test_guard.qml_item_window(popup_item)
            self.assertIsInstance(popup_window, QQuickWindow)
            self.assertIsNot(popup_window, host)
            self.assertTrue(all(value is not host for value in gc.get_referents(host_item)))
            self.assertTrue(all(value is not popup_window for value in gc.get_referents(popup_item)))
            popup_window.requestActivate()
            popup_item.forceActiveFocus()
            APP.processEvents()
            QTest.keyClick(popup_window, Qt.Key_A)
            APP.processEvents()
            self.assertEqual(host.property("popupKeyCount"), 1)
            self.assertEqual(host.property("hostKeyCount"), 0)
            self.assertEqual(factory.blocked_urls, [])
        finally:
            host.close()
            host.deleteLater()
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            APP.processEvents()
        gc.collect()

    def test_both_import_names_share_guard_and_default_settings_write_is_temporary(self):
        self.assertIs(qt_test_guard, package_qt_test_guard)
        root = qt_test_guard.QT_TEST_SETTINGS_ROOT.resolve()
        self.assertEqual(Path(os.environ["XDG_STATE_HOME"]).resolve(), root)
        value = QSettings("coat-isolation-test", "user-profile-write")
        value.setValue("probe", "private")
        value.sync()
        actual = Path(value.fileName()).resolve()
        self.assertTrue(actual.is_relative_to(root), str(actual))
        self.assertTrue(actual.is_file(), str(actual))

    def test_qt_settings_guard_restores_both_scopes_and_application_identity(self):
        root = qt_test_guard.QT_TEST_SETTINGS_ROOT.resolve()
        original_state_home = os.environ["XDG_STATE_HOME"]
        with tempfile.TemporaryDirectory(prefix="coat-settings-contamination-") as folder:
            contaminated_root = Path(folder).resolve()
            QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                              str(contaminated_root / "user"))
            QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.SystemScope,
                              str(contaminated_root / "system"))
            QCoreApplication.setOrganizationName("contaminated-organization")
            QCoreApplication.setApplicationName("contaminated-application")
            os.environ["XDG_STATE_HOME"] = str(contaminated_root / "state")

            qt_test_guard.restore_qt_test_settings()

        self.assertEqual(os.environ["XDG_STATE_HOME"], original_state_home)
        self.assertEqual(QCoreApplication.organizationName(),
                         qt_test_guard.QT_TEST_ORGANIZATION_NAME)
        self.assertEqual(QCoreApplication.applicationName(),
                         qt_test_guard.QT_TEST_APPLICATION_NAME)
        for scope, organization in (
                (QSettings.Scope.UserScope, "restored-user-scope"),
                (QSettings.Scope.SystemScope, "restored-system-scope")):
            value = QSettings(QSettings.Format.IniFormat, scope, organization,
                              "settings-path-probe")
            value.setValue("probe", "private")
            value.sync()
            actual = Path(value.fileName()).resolve()
            self.assertTrue(actual.is_relative_to(root), str(actual))
            self.assertTrue(actual.is_file(), str(actual))

    def test_all_implicit_profile_overloads_stay_temporary_and_native_is_rejected(self):
        root = qt_test_guard.QT_TEST_SETTINGS_ROOT.resolve()
        settings_rows = (
            QSettings(QSettings.Scope.UserScope, "scoped-org", "scoped-app"),
            QSettings(organization="keyword-org", application="keyword-app"),
            QSettings(),
        )
        parent = QObject()
        settings_rows += (QSettings("parent-overload-org", parent),)
        for value in settings_rows:
            value.setValue("probe", "private")
            value.sync()
            self.assertTrue(Path(value.fileName()).resolve().is_relative_to(root),
                            value.fileName())

        with tempfile.TemporaryDirectory(prefix="coat-native-guard-negative-") as folder:
            outside = Path(folder) / "native-must-not-be-created.ini"
            with self.assertRaisesRegex(AssertionError, "NativeFormat"):
                QSettings(str(outside), QSettings.Format.NativeFormat)
            with self.assertRaisesRegex(AssertionError, "NativeFormat"):
                QSettings(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          "native-org", "native-app")
            self.assertFalse(outside.exists())

        explicit_ini = root / "explicit-private.ini"
        explicit = QSettings(str(explicit_ini), QSettings.Format.IniFormat)
        explicit.setValue("probe", "allowed")
        explicit.sync()
        self.assertEqual(Path(explicit.fileName()).resolve(), explicit_ini.resolve())

    def test_qml_http_request_is_failed_by_factory_without_socket_access(self):
        engine = QQmlEngine()
        factory = qt_test_guard.NoNetworkAccessManagerFactory()
        engine.setNetworkAccessManagerFactory(factory)
        component = QQmlComponent(engine)
        component.setData(
            b'''import QtQml
QtObject {
    property string requestState: "pending"
    Component.onCompleted: {
        var xhr = new XMLHttpRequest()
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE)
                requestState = "done:" + xhr.status
        }
        xhr.open("GET", "https://example.invalid/no-network-test")
        xhr.send()
    }
}''',
            QUrl("file:///qt-test/no-network.qml"),
        )
        item = component.create()
        self.assertIsNotNone(item, "QML test object failed: " + "; ".join(
            error.toString() for error in component.errors()))
        deadline = time.monotonic() + 3
        while item.property("requestState") == "pending" and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.005)
        self.assertTrue(str(item.property("requestState")).startswith("done:"))
        self.assertEqual(factory.blocked_urls,
                         ["https://example.invalid/no-network-test"])
        item.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents()
        APP.processEvents()


class LauncherIsolationTests(unittest.TestCase):
    def test_apply_redirects_state_blocks_service_starts_and_stop_restores(self):
        old_launcher_root = launcher_module.PROJECT_ROOT
        old_data_project = data_files.PROJECT_ROOT
        old_data_root = data_files.DATA_ROOT
        old_settings_path = settings.SETTINGS_PATH
        old_cache_path = settings.MODEL_CACHE_PATH
        old_credential_path = assistant_credentials.SETTINGS_PATH
        old_scheduler_init = SchedulerClient.__init__
        old_bridge_init = AssistantBridge.__init__
        old_server_init = UiControlServer.__init__
        old_geometry_settings = window_geometry.QSettings
        old_network_manager = settings.QNetworkAccessManager
        old_credential_reader = assistant_credentials.read_api_key

        with tempfile.TemporaryDirectory(prefix="coat-launcher-isolation-test-") as folder:
            root = Path(folder).resolve()
            isolation = LauncherTestIsolation(root, launcher_module).apply()
            try:
                self.assertIs(isolation.apply(), isolation)
                self.assertEqual(launcher_module.PROJECT_ROOT, root)
                self.assertEqual(data_files.PROJECT_ROOT, root)
                self.assertEqual(data_files.DATA_ROOT, root / "data")
                self.assertEqual(settings.SETTINGS_PATH, root / "config" / "ui_settings.json")
                self.assertEqual(settings.MODEL_CACHE_PATH,
                                 root / "config" / "assistant_model_cache.json")
                self.assertEqual(assistant_credentials.SETTINGS_PATH,
                                 root / "config" / "ui_settings.json")
                real_catalog = (Path(__file__).resolve().parents[1]
                                / "data" / "optical_catalog.json")
                copied_catalog = isolation.data_root / "optical_catalog.json"
                self.assertEqual(copied_catalog.read_bytes(), real_catalog.read_bytes())

                value = isolation.create_settings()
                value.setValue("probe", "temporary")
                value.sync()
                self.assertTrue(Path(value.fileName()).resolve().is_relative_to(root))
                with self.assertRaisesRegex(AssertionError, "escaped launcher test root"):
                    window_geometry.QSettings(
                        str(root.parent / "outside.ini"), QSettings.Format.IniFormat)
                settings.save_ui_settings({"font_point_size": 17})
                self.assertTrue((root / "config" / "ui_settings.json").is_file())
                self.assertIsNone(settings.read_api_key("https://example.invalid/v1"))
                with self.assertRaisesRegex(AssertionError, "scheduler client"):
                    SchedulerClient(root)
                with self.assertRaisesRegex(AssertionError, "assistant bridge"):
                    AssistantBridge()
                with self.assertRaisesRegex(AssertionError, "UI control server"):
                    UiControlServer(lambda *_args: {})
            finally:
                isolation.stop()
                isolation.stop()
                # Reapplying the same instance starts one clean patch lifetime.
                self.assertIs(isolation.apply(), isolation)
                isolation.stop()

        self.assertEqual(launcher_module.PROJECT_ROOT, old_launcher_root)
        self.assertEqual(data_files.PROJECT_ROOT, old_data_project)
        self.assertEqual(data_files.DATA_ROOT, old_data_root)
        self.assertEqual(settings.SETTINGS_PATH, old_settings_path)
        self.assertEqual(settings.MODEL_CACHE_PATH, old_cache_path)
        self.assertEqual(assistant_credentials.SETTINGS_PATH, old_credential_path)
        self.assertIs(window_geometry.QSettings, old_geometry_settings)
        self.assertIs(settings.QNetworkAccessManager, old_network_manager)
        self.assertIs(assistant_credentials.read_api_key, old_credential_reader)
        self.assertIs(SchedulerClient.__init__, old_scheduler_init)
        self.assertIs(AssistantBridge.__init__, old_bridge_init)
        self.assertIs(UiControlServer.__init__, old_server_init)


if __name__ == "__main__":
    unittest.main()
