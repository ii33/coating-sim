"""Headless Tk controller contracts; deliberately not a rendering acceptance test."""

from contextlib import ExitStack
import queue
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from tools.dependency_manager.gui import DependencyManagerWindow
from tools.dependency_manager.mirrors import ProxyConfig


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class GuiControllerTests(unittest.TestCase):
    def fixture(self):
        ui = DependencyManagerWindow.__new__(DependencyManagerWindow)
        ui.window = MagicMock()
        ui.events = queue.Queue()
        ui.cancel_event = threading.Event()
        ui.busy = False
        ui.closing = False
        ui.generation = 0
        ui.active_proxy = None
        ui.pending_plan = None
        ui.last_result = None
        ui.controls = [MagicMock(), MagicMock()]
        ui.cancel_button = MagicMock()
        # Dynamic replacement is deliberate: no Tk interpreter/display is created.
        ui.packages = MagicMock()
        for name, replacement in {
            "status": Value(), "identity": Value(),
            "_append_log": MagicMock(), "_finish": MagicMock(),
        }.items():
            setattr(ui, name, replacement)
        return ui

    def await_events(self, ui):
        deadline = time.monotonic() + 2
        while not ui.events.qsize() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreater(ui.events.qsize(), 0)

    def test_build_exposes_source_git_and_launch_controls_without_display(self):
        from tools.dependency_manager import gui
        with ExitStack() as stack:
            for name in ("Frame", "Label", "Entry", "Button", "Combobox", "Checkbutton", "Notebook", "Treeview", "Scrollbar"):
                stack.enter_context(patch.object(gui.ttk, name))
            stack.enter_context(patch.object(gui.tk, "Text"))
            stack.enter_context(patch.object(gui.tk, "StringVar", side_effect=lambda value="": Value(value)))
            stack.enter_context(patch.object(gui.tk, "BooleanVar", side_effect=lambda value=False: Value(value)))
            ui = DependencyManagerWindow(MagicMock(), "/project")
        self.assertEqual(ui.deployment_source.get(), "github")
        self.assertEqual(ui.shortcut_location.get(), "none")
        self.assertTrue(hasattr(ui, "git_runtime_text"))
        self.assertTrue(hasattr(ui, "launch_text"))
        self.assertTrue(hasattr(ui, "deployment_text"))

    def test_worker_does_not_block_ui_and_cancel_remains_available(self):
        ui = self.fixture()
        entered = threading.Event()
        release = threading.Event()

        def work(emit, cancel):
            entered.set()
            release.wait(2)
            return {"status": "cancelled" if cancel.is_set() else "success"}

        ui._start(work, "inspect", ProxyConfig("none"))
        self.assertTrue(entered.wait(1))
        self.assertTrue(ui.busy)
        ui.cancel_button.state.assert_called_with(["!disabled"])
        ui.cancel()
        self.assertTrue(ui.cancel_event.is_set())
        release.set()
        self.await_events(ui)
        ui._drain()
        self.assertFalse(ui.busy)
        ui._finish.assert_called_once()

    def test_repeated_action_is_ignored_while_busy(self):
        ui = self.fixture()
        ui.busy = True
        work = MagicMock()
        ui._start(work, "repair")
        work.assert_not_called()
        self.assertTrue(ui.events.empty())

    def test_worker_failure_is_redacted_and_inputs_reenabled(self):
        ui = self.fixture()

        def fail(emit, cancel):
            raise ValueError("https://user:secret@example.test/a?token=private")

        ui._start(fail, "repair", ProxyConfig("none"))
        self.await_events(ui)
        ui._drain()
        result = ui._finish.call_args[0][1]
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret", str(result))
        self.assertNotIn("private", str(result))
        for control in ui.controls:
            control.state.assert_called_with(["!disabled"])

    def test_close_running_requires_confirmation_and_waits_for_result(self):
        ui = self.fixture()
        ui.busy = True
        with patch(
            "tools.dependency_manager.gui.messagebox.askyesno", return_value=False
        ):
            ui.close()
        self.assertFalse(ui.closing)
        self.assertFalse(ui.cancel_event.is_set())
        with patch(
            "tools.dependency_manager.gui.messagebox.askyesno", return_value=True
        ):
            ui.close()
        self.assertTrue(ui.closing)
        self.assertTrue(ui.cancel_event.is_set())
        ui.window.destroy.assert_not_called()
        ui.events.put((0, "done", ("repair", {"status": "cancelled"})))
        ui._drain()
        ui.window.destroy.assert_called_once()
        ui._finish.assert_not_called()

    def test_stale_closed_generation_never_updates_widgets(self):
        ui = self.fixture()
        ui.generation = 1
        ui.events.put((0, "event", {"event": "log", "text": "late"}))
        ui.events.put((0, "done", ("inspect", {"status": "success"})))
        ui._drain()
        ui._append_log.assert_not_called()
        ui._finish.assert_not_called()

    def test_broken_target_error_preserves_prior_package_inspection(self):
        ui = self.fixture()
        ui._show_packages(
            {
                "status": "failed",
                "target": {"exists": True, "valid": False},
                "packages": [{"status": "missing"}],
            }
        )
        ui.packages.delete.assert_not_called()
        ui.packages.insert.assert_not_called()

    def test_inspection_does_not_require_a_working_network_proxy(self):
        ui = self.fixture()
        ui.project = Value("/project")
        ui.venv = Value("/project/.venv")
        ui.profile = Value("development")
        ui.backend = Value("cpu")
        ui.selected = {"desktop": Value(True)}
        ui.proxy_mode = Value("http")
        ui.proxy_url = Value("socks5://localhost:10808")
        inputs, proxy = ui._inputs(validate_proxy=False)
        self.assertEqual(inputs["groups"], ("desktop",))
        self.assertEqual(inputs["profile"], "development")
        with self.assertRaises(ValueError):
            proxy.validate()


    def test_project_fetch_uses_shared_inputs_in_worker(self):
        ui = self.fixture()
        ui.deployment_dir = Value("/new-project")
        ui.deployment_ref = Value("main")
        ui.deployment_source = Value("github")
        ui.repository_url = Value("")
        ui.git_path = Value("")
        ui.mirror = Value("pypi")
        ui.python = Value("/python")
        proxy = ProxyConfig("none")
        for name, replacement in {
            "_inputs": MagicMock(return_value=({"groups": ("desktop",), "backend": "cpu", "profile": "development"}, proxy)),
            "_start": MagicMock(),
        }.items():
            setattr(ui, name, replacement)
        with patch("tools.dependency_manager.gui.plan_project_deployment") as prepare:
            ui.prepare_deployment()
            prepare.assert_not_called()
            work, purpose, captured_proxy = ui._start.call_args.args
            self.assertEqual(purpose, "project_plan")
            self.assertIs(captured_proxy, proxy)
            work(MagicMock(), threading.Event())
            self.assertEqual(prepare.call_args.kwargs["profile"], "development")
            self.assertEqual(prepare.call_args.args[0], "/new-project")

    def test_project_check_routes_offline_and_remote_without_install(self):
        for remote in (False, True):
            ui = self.fixture()
            ui.deployment_ref = Value("main")
            ui.deployment_source = Value("cnb")
            ui.repository_url = Value("https://cnb.cool/example/mirror")
            ui.git_path = Value("")
            inputs = {"root": "/local-project", "venv": "/local-project/.venv", "groups": ("cpu",), "backend": "cpu", "profile": "runtime"}
            for name, replacement in {"_inputs": MagicMock(return_value=(inputs, ProxyConfig("none"))), "_start": MagicMock()}.items():
                setattr(ui, name, replacement)
            with patch("tools.dependency_manager.gui.check_project") as check:
                ui.check_project(remote)
                ui._inputs.assert_called_once_with(validate_proxy=remote)
                work, purpose, proxy = ui._start.call_args.args
                self.assertEqual(purpose, "project_check")
                work(MagicMock(), threading.Event())
                self.assertEqual(check.call_args.args[0], "/local-project")
                self.assertEqual(check.call_args.kwargs["check_remote"], remote)
                self.assertEqual(check.call_args.kwargs["source"], "cnb")

    def test_launch_short_transaction_has_no_false_cancel_or_close(self):
        ui = self.fixture()
        ui.busy = True
        ui.active_purpose = "launch_create"
        ui.cancel()
        ui.close()
        self.assertFalse(ui.cancel_event.is_set())
        ui.window.destroy.assert_not_called()
        self.assertIn("短事务", ui.status.get())

    def test_project_install_waits_for_confirmation(self):
        ui = self.fixture()
        ui.deployment_text = MagicMock()
        for name, replacement in {
            "_show_packages": MagicMock(), "_text": MagicMock(),
            "_confirm_plan": MagicMock(return_value=False), "_start": MagicMock(),
        }.items():
            setattr(ui, name, replacement)
        result = {"status": "ready", "directory": "/new-project"}
        DependencyManagerWindow._finish(ui, "project_plan", result)
        ui._confirm_plan.assert_called_once()
        ui._start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
