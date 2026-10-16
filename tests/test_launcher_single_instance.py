"""Launcher routing contracts use test-owned windows and temporary settings."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import unittest

from PySide6.QtCore import QObject, QEvent, QCoreApplication, Qt, Signal
from PySide6.QtGui import QWindow
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication
import shiboken6

from coat_sim.ui import launcher as launcher_module
from coat_sim.ui.launcher import LauncherBridge, MODULES, WindowOpenRequest
from coat_sim.ui.control import UiControlError, WindowRegistry
from tests.launcher_test_isolation import LauncherTestIsolation

APP = QApplication.instance() or QApplication([])


class DocumentBridge(QObject):
    documentChanged = Signal()

    def __init__(self, path="", dirty=False):
        super().__init__()
        self.documentPath = path
        self.documentDirty = dirty
        self.opens = []
        self.busy = False

    def openDocument(self, path):
        self.opens.append(path)
        self.documentPath = path
        self.documentChanged.emit()


class ReuseBridge(DocumentBridge):
    def __init__(self):
        super().__init__()
        self.intents = []
        self.accept = True
        self.start_count = 0

    def reuseWindowOpen(self, document_path="", payload=None):
        self.intents.append((document_path, payload))
        return self.accept

    def requestCalculation(self):
        self.start_count += 1


class LauncherSingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="coat-singleton-")
        self.isolation = LauncherTestIsolation(self.directory.name, launcher_module).apply()
        self.engine = QQmlApplicationEngine()
        self.launcher = LauncherBridge(
            self.engine, with_launcher=False, software_mode=True,
            window_settings=self.isolation.create_settings())
        self.errors = []
        self.launcher.launchError.connect(self.errors.append)
        self.created_windows = []
        self.registry = None

    def tearDown(self):
        if self.registry is not None:
            self.registry.close()
        for identity in list(self.launcher._open_requests):
            self.launcher._cancel_open_request(identity)
        for identity in list(self.launcher._windows):
            handle = self.launcher._windows.get(identity)
            if handle is not None and shiboken6.isValid(handle.window):
                handle.window.close()
            self.launcher._forget_window(identity)
        for window in self.created_windows:
            if shiboken6.isValid(window):
                window.close()
                window.deleteLater()
        self.engine.deleteLater()
        self.launcher.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        self.isolation.stop()
        self.directory.cleanup()

    def spec(self, key):
        return next(spec for spec in MODULES if spec.key == key)

    def register(self, key="source", bridge=None, path="", identity="instance", payload=None):
        bridge = bridge or DocumentBridge(path)
        window = QWindow()
        self.created_windows.append(window)
        handle = self.launcher._register_window(
            self.spec(key), WindowOpenRequest(identity, key, path, payload),
            window, bridge, None, None, None)
        window.show()
        APP.processEvents()
        return handle

    def pending(self, key="source", path="", payload=None):
        with patch.object(self.launcher, "_import_module"), patch.object(
                launcher_module.threading, "Thread") as thread:
            identity = self.launcher.open_window(key, path, payload=payload)
            self.assertEqual(thread.call_count, 1)
        return identity

    def test_all_module_routes_reuse_one_instance_without_clearing_document(self):
        for spec in MODULES:
            with self.subTest(key=spec.key):
                handle = self.register(spec.key, identity=f"one-{spec.key}")
                self.assertEqual(self.launcher.open_window(spec.key), handle.instance_id)
                self.assertEqual(self.launcher.openWindow(spec.key), handle.instance_id)
                self.assertEqual(self.launcher.openOrActivate(spec.key), handle.instance_id)
                self.assertEqual(self.launcher.launch(MODULES.index(spec)), handle.instance_id)
                self.assertEqual(len(self.launcher._handles_for(spec.key)), 1)
                self.assertEqual(handle.bridge.opens, [])

    def test_pending_import_deduplicates_and_preserves_latest_explicit_intent(self):
        first = self.pending(path="/first.csrc")
        with patch.object(launcher_module.threading, "Thread") as thread:
            self.assertEqual(self.launcher.open_window("source"), first)
            self.assertEqual(self.launcher.open_window("source", "/second.csrc"), first)
            self.assertEqual(self.launcher.open_window("source"), first)
            thread.assert_not_called()
        self.assertEqual(len(self.launcher._open_requests), 1)
        self.assertEqual(len(self.launcher._pending_opens["source"]), 1)
        self.assertEqual(self.launcher._open_requests[first][1].document_path, "/second.csrc")
        self.assertEqual(self.launcher._pending_opens["source"][0].document_path, "/second.csrc")

    def test_qml_creation_defers_latest_payload_then_routes_to_same_controller(self):
        first = self.pending("film_simulation", payload={"original": True})
        # An already constructed controller keeps owning state until registration.
        self.launcher._pending_qml[first] = SimpleNamespace(activate_when_ready=False)
        payload = {"latest": True}
        self.assertEqual(self.launcher.open_window("film_simulation", payload=payload), first)
        self.assertTrue(self.launcher._pending_qml[first].activate_when_ready)
        self.launcher._pending_qml.pop(first)
        bridge = ReuseBridge()
        handle = self.register("film_simulation", bridge=bridge, identity=first)
        APP.processEvents()
        self.assertEqual(bridge.intents, [("", payload)])
        self.assertIs(self.launcher._existing_window("film_simulation"), handle)
        self.assertFalse(self.launcher._deferred_open_intents)

    def test_explicit_return_to_original_pending_source_cancels_deferred_replacement(self):
        original = object()
        first = self.pending("film_simulation", payload=original)
        self.launcher._pending_qml[first] = SimpleNamespace(activate_when_ready=False)
        self.launcher.open_window("film_simulation", payload=object())
        self.launcher._deferred_companion_starts.add(first)
        self.assertIn(first, self.launcher._deferred_open_intents)
        self.assertEqual(self.launcher.open_window("film_simulation", payload=original), first)
        self.assertNotIn(first, self.launcher._deferred_open_intents)
        self.assertNotIn(first, self.launcher._deferred_companion_starts)
        self.launcher._pending_qml.pop(first)

    def test_newer_pending_source_suppresses_old_ready_calculation(self):
        from PySide6.QtQml import QQmlIncubator
        from coat_sim.ui.launcher import WindowTitleBridge
        first = self.pending("film_simulation", payload={"original": True})
        request = self.launcher._open_requests[first][1]
        bridge = ReuseBridge()
        window = QWindow()
        self.created_windows.append(window)
        old_ready = Mock()
        pending = SimpleNamespace(
            spec=self.spec("film_simulation"), request=request, bridge=bridge,
            window=window, context=None, component=None, engine=self.engine,
            title_bridge=WindowTitleBridge(self.launcher, first, "film_simulation", "仿真", bridge),
            incubator=SimpleNamespace(object=lambda: window), on_ready=old_ready,
            cancelled=False, maximized=False)
        self.launcher._pending_qml[first] = pending
        payload = {"latest": True}
        self.launcher.open_window("film_simulation", payload=payload)
        self.launcher._deferred_companion_starts.add(first)
        with patch.object(self.launcher._geometry_manager, "prepare"), patch.object(
                self.launcher, "_window_animations_enabled", return_value=False):
            self.launcher._qml_incubator_changed(first, QQmlIncubator.Status.Ready)
        APP.processEvents()
        old_ready.assert_not_called()
        self.assertEqual(bridge.intents, [("", payload)])
        self.assertEqual(bridge.start_count, 1)
        self.assertEqual(len(self.launcher._handles_for("film_simulation")), 1)

    def test_same_path_keeps_dirty_edits_and_different_path_is_not_silently_loaded(self):
        path = str(Path(self.directory.name) / "active.csrc")
        bridge = DocumentBridge(path, dirty=True)
        handle = self.register(bridge=bridge, path=path)
        self.assertEqual(self.launcher.open_window("source", path), handle.instance_id)
        self.assertEqual(bridge.opens, [])
        self.assertEqual(self.launcher.open_window("source", path + ".other"), "")
        self.assertEqual(bridge.opens, [])
        self.assertTrue(bridge.documentDirty)
        self.assertIn("未保存", self.errors[-1])

    def test_title_path_follows_public_state_and_cleared_paths_stay_untitled(self):
        bridge = DocumentBridge("/old.csrc")
        handle = self.register(bridge=bridge, path="/first.csrc")
        bridge.documentPath = ""
        bridge.documentChanged.emit()
        self.assertEqual(handle.title_bridge.documentPath, "")
        source = QObject()
        source.uiState = {"path": "/current.cfilm", "dirty": False}
        source.designMeta = {"name": "Current design"}
        film = self.register("film_design", bridge=source, path="/old.cfilm", identity="metadata")
        self.assertEqual(film.title_bridge.documentPath, "/current.cfilm")
        self.assertEqual(film.title_bridge.documentName, "Current design")
        source.uiState = {"path": "", "dirty": False}
        self.assertEqual(film.title_bridge.documentPath, "")

    def test_clean_new_path_is_forwarded_into_existing_controller(self):
        bridge = DocumentBridge("/old.csrc")
        handle = self.register(bridge=bridge)
        self.assertEqual(self.launcher.open_window("source", "/new.csrc"), handle.instance_id)
        self.assertEqual(bridge.opens, ["/new.csrc"])
        self.assertEqual(len(self.launcher._windows), 1)

    def test_controller_owns_replacement_confirmation_and_repeated_payload(self):
        bridge = ReuseBridge()
        bridge.documentDirty = True
        handle = self.register("film_simulation", bridge=bridge)
        source = object()
        self.assertEqual(self.launcher.open_window("film_simulation", payload=source), handle.instance_id)
        # True can mean pending confirmation. A later cancel must not suppress retry.
        self.assertEqual(self.launcher.open_window("film_simulation", payload=source), handle.instance_id)
        self.assertEqual(bridge.intents, [("", source), ("", source)])
        bridge.accept = False
        self.assertEqual(self.launcher.open_window("film_simulation", payload=object()), "")
        self.assertTrue(bridge.documentDirty)
        self.assertEqual(len(self.launcher._windows), 1)

    def test_minimize_restore_and_close_recreate(self):
        handle = self.register()
        handle.window.showMinimized()
        APP.processEvents()
        self.assertTrue(handle.window.windowState() & Qt.WindowState.WindowMinimized)
        self.assertEqual(self.launcher.open_window("source"), handle.instance_id)
        self.assertFalse(handle.window.windowState() & Qt.WindowState.WindowMinimized)
        self.assertTrue(handle.window.isVisible())
        self.assertTrue(self.launcher.closeWindow(handle.instance_id))
        APP.processEvents()
        self.assertNotIn(handle.instance_id, self.launcher._windows)
        new = self.pending()
        self.assertNotEqual(new, handle.instance_id)

    def test_native_close_while_minimized_forgets_hidden_instance(self):
        handle = self.register()
        handle.window.showMinimized()
        APP.processEvents()
        self.assertIn(handle.instance_id, self.launcher._windows)
        # Qt keeps windowState=Minimized after Close, while visibility becomes
        # Hidden. This path is used by IPC/native closes without prior activation.
        self.assertTrue(handle.window.close())
        APP.processEvents()
        self.assertNotIn(handle.instance_id, self.launcher._windows)
        self.assertIsNone(self.launcher._existing_window("source"))

    def test_destroyed_window_releases_registry_and_reopen_gets_new_id(self):
        handle = self.register()
        shiboken6.delete(handle.window)
        APP.processEvents()
        self.assertIsNone(self.launcher._existing_window("source"))
        self.assertNotEqual(self.pending(), handle.instance_id)

    def test_cancel_creation_removes_deferred_payload_and_late_import_cannot_open_it(self):
        first = self.pending()
        self.launcher._deferred_open_intents[first] = WindowOpenRequest(first, "source", "/late.csrc")
        self.assertTrue(self.launcher.closeWindow(first))
        self.assertFalse(self.launcher._deferred_open_intents)
        with patch.object(self.launcher, "_open") as opener:
            self.launcher._finish_import("source", object(), "")
            APP.processEvents()
            opener.assert_not_called()
        self.assertFalse(self.launcher._windows)

    def test_registry_keeps_pending_parameters_during_qml_creation_and_force_cancels(self):
        first = self.pending()
        self.launcher._loading.clear()  # import done; component creation still pending
        self.registry = WindowRegistry(self.launcher, root=self.directory.name)
        self.registry._pending["source"] = {"fields": {"source_exponent": "2"}}
        with patch.object(self.registry, "_adapter", return_value=None):
            self.assertEqual(self.registry._read("source")["state"], "loading")
            self.registry._apply_pending()
            self.assertIn("source", self.registry._pending)
            self.registry.dispatch("open_window", {
                "window_type": "source", "design_path": None, "fields": None, "force": False})
            self.assertIn("source", self.registry._pending)
            with self.assertRaises(UiControlError) as conflict:
                self.registry.dispatch("open_window", {
                    "window_type": "source", "fields": {"source_exponent": "3"}})
            self.assertEqual(conflict.exception.code, "window_loading")
            with self.assertRaises(UiControlError) as error:
                self.registry.dispatch("close_window", {"window_type": "source"})
            self.assertEqual(error.exception.code, "window_loading")
            result = self.registry.dispatch("close_window", {"window_type": "source", "force": True})
            self.assertEqual(result["state"], "closed")
            self.assertNotIn(first, self.launcher._open_requests)
            self.assertFalse(self.registry._pending)

    def test_rejected_close_keeps_existing_instance_and_unsaved_document(self):
        bridge = DocumentBridge("/unsaved.csrc", dirty=True)
        handle = self.register(bridge=bridge)
        with patch.object(handle.window, "close", return_value=False):
            self.assertFalse(self.launcher.closeWindow(handle.instance_id))
        self.assertIs(self.launcher._existing_window("source"), handle)
        self.assertTrue(bridge.documentDirty)
        self.assertEqual(self.launcher.open_window("source"), handle.instance_id)

    def test_actual_qml_direct_route_repeated_open_close_and_recreate(self):
        import importlib
        import time
        from PySide6.QtTest import QTest
        from coat_sim.ui.theme_service import ThemeEngineBridge
        from coat_sim.ui.theme_tokens import build_theme_snapshot
        theme = ThemeEngineBridge(
            build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=self.engine)
        self.engine.rootContext().setContextProperty("Theme", theme)
        self.engine.rootContext().setContextProperty("launcher", self.launcher)
        self.launcher._modules["about"] = importlib.import_module("coat_sim.ui.about")

        def ready(identity):
            deadline = time.monotonic() + 6
            while identity not in self.launcher._windows and time.monotonic() < deadline:
                APP.processEvents()
                QTest.qWait(10)
            self.assertIn(identity, self.launcher._windows, self.errors)
            return self.launcher._windows[identity]

        first = self.launcher.open_window("about")
        self.assertEqual(self.launcher.open_window("about"), first)
        handle = ready(first)
        self.assertTrue(handle.window.isVisible())
        handle.window.showMinimized()
        APP.processEvents()
        self.assertEqual(self.launcher.open_window("about"), first)
        self.assertFalse(handle.window.windowState() & Qt.WindowState.WindowMinimized)
        self.assertTrue(self.launcher.closeWindow(first))
        APP.processEvents()
        self.assertNotIn(first, self.launcher._windows)
        second = self.launcher.open_window("about")
        self.assertNotEqual(second, first)
        ready(second)
        self.assertEqual(len(self.launcher._handles_for("about")), 1)

    def test_real_quick_controllers_list_read_and_allowlisted_updates(self):
        import copy
        import time
        from PySide6.QtTest import QTest
        from coat_sim.ui.film_quick_controller import FilmDesignQuickController
        from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController
        from tests.test_film_simulation_quick import FilmSimulationQuickControllerTests

        service, design = FilmSimulationQuickControllerTests._synthetic_glass_fixture(self.directory.name)
        source = FilmDesignQuickController(
            self.directory.name, service=service, parent=self.launcher,
            geometry_settings=self.isolation.create_settings())
        deadline = time.monotonic() + 8
        while not source.initializationReady and time.monotonic() < deadline:
            APP.processEvents()
            QTest.qWait(10)
        self.assertTrue(source.initializationReady, source.initializationError)
        self.assertTrue(source.reuseWindowOpen("", design))
        if source.session.snapshot().pending_confirmation:
            source.resolvePendingAction(True)
        simulation = FilmSimulationQuickController(
            root=self.directory.name, service=service, design=design, parent=self.launcher)
        self.register("film_design", bridge=source, identity="real-design")
        self.register("film_simulation", bridge=simulation, identity="real-simulation")
        self.registry = WindowRegistry(self.launcher, root=self.directory.name)
        result = self.registry.dispatch("list_windows", {})
        films = {row["window_type"]: row for row in result["windows"]
                 if row["window_type"] in ("film_design", "film_simulation")}
        self.assertEqual(films["film_design"]["state"], "ready")
        self.assertEqual(films["film_design"]["document"], source.state["design"])
        self.assertEqual(films["film_simulation"]["document"], design)
        self.assertEqual(films["film_simulation"]["fields"]["angle_deg"], 0.0)
        self.assertIn("simulation_configuration", films["film_simulation"])
        self.assertTrue(source.setDesignValue("name", "Local unsaved design"))
        self.assertTrue(source.dirty)
        before = copy.deepcopy(source.state["design"])
        with self.assertRaises(UiControlError) as error:
            self.registry.dispatch("update_window", {
                "window_type": "film_design", "fields": {"_source_session": "forbidden"}})
        self.assertEqual(error.exception.code, "invalid_field")
        self.assertEqual(source.state["design"], before)
        with self.assertRaises(UiControlError) as error:
            self.registry.dispatch("update_window", {
                "window_type": "film_simulation", "fields": {"arbitraryMethod": "forbidden"}})
        self.assertEqual(error.exception.code, "invalid_field")
        self.registry.dispatch("update_window", {
            "window_type": "film_simulation", "fields": {"angle_deg": 30.0}})
        self.assertEqual(simulation.settings["angle_deg"], 30.0)
        self.assertTrue(self.registry.dispatch("read_window", {
            "window_type": "film_simulation"})["dirty"])
        replacement = copy.deepcopy(design)
        replacement["name"] = "IPC replacement"
        with self.assertRaises(UiControlError) as error:
            self.registry.dispatch("update_window", {
                "window_type": "film_design", "data": replacement})
        self.assertEqual(error.exception.code, "unsaved_changes")
        pending = self.registry.dispatch("update_window", {
            "window_type": "film_design", "data": replacement, "force": True})
        self.assertTrue(pending["pending_confirmation"])
        self.assertEqual(source.state["design"], before)
        source.resolvePendingAction(False)
        self.assertEqual(source.state["design"], before)
        with self.assertRaises(UiControlError) as error:
            self.registry.dispatch("window_action", {
                "window_type": "film_simulation", "action": "calculate", "arguments": {}})
        self.assertEqual(error.exception.code, "compute_confirmation_required")

    def test_dispatch_does_not_start_a_second_tree_while_creating_or_presenting(self):
        first = self.pending()
        self.launcher._modules["source"] = object()
        self.launcher._pending_qml[first] = SimpleNamespace()
        with patch.object(self.launcher, "_open") as opener:
            self.launcher._dispatch_open(first)
            opener.assert_not_called()
        self.launcher._pending_qml.pop(first)
        handle = self.register(identity=first)
        self.launcher._open_requests[first] = (self.spec("source"), WindowOpenRequest(first, "source"))
        with patch.object(self.launcher, "_open") as opener:
            self.launcher._dispatch_open(first)
            opener.assert_not_called()
        self.assertIs(self.launcher._existing_window("source"), handle)


if __name__ == "__main__":
    unittest.main()
