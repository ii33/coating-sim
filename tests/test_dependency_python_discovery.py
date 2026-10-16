"""Offline, non-mutating interpreter discovery contracts."""
import subprocess
import sys
import threading
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.dependency_manager import manager, python_discovery as discovery


def identity(version="3.12.10", platform="win32"):
    return {"implementation": "CPython", "python": version, "platform": platform, "machine": "AMD64", "executable": "python.exe", "prefix": "base", "base_prefix": "base", "has_venv": True, "has_ensurepip": True, "bundled_pip": "25.0"}


class DiscoveryTests(unittest.TestCase):
    def discover(self, identities, backend="cpu"):
        paths = [(str(i), "fixture") for i in range(len(identities))]
        with patch.object(discovery, "_candidate_paths", return_value=paths), patch.object(Path, "is_file", return_value=True), patch.object(discovery.os, "access", return_value=True), patch.object(discovery, "probe_python", side_effect=identities) as probe:
            result = discovery.discover_python(backend)
        for call in probe.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 2)
        return result

    def test_identity_retains_implementation_for_hardware_recommendation(self):
        result = self.discover([identity()])
        self.assertEqual(result["candidates"][0]["identity"]["implementation"], "CPython")

    def test_cancelled_before_start_does_not_probe(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(discovery, "probe_python") as probe:
            result = discovery.discover_python(cancel_event=cancel)
        probe.assert_not_called()
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["selected_python"])

    def test_cancel_during_probe_never_returns_selected_candidate(self):
        cancel = threading.Event()
        def probe(*args, **kwargs):
            cancel.set()
            return identity()
        with patch.object(discovery, "probe_python", side_effect=probe):
            result = discovery.discover_python(explicit="chosen.exe", cancel_event=cancel)
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["selected_python"])

    def test_project_context_is_used_for_managed_candidate(self):
        with patch("tools.dependency_manager.runtime.default_runtime_directory", return_value=Path("selected-project/runtime")) as default, patch.object(discovery.shutil, "which", return_value=None), patch.object(discovery.sys, "platform", "linux"):
            paths = list(discovery._candidate_paths(project="selected-project"))
        default.assert_called_once_with(project="selected-project")
        self.assertIn((str(Path("selected-project/runtime/python.exe")), "managed_runtime"), paths)

    def test_frozen_missing_project_does_not_break_other_discovery(self):
        with patch("tools.dependency_manager.runtime.default_runtime_directory", side_effect=ValueError("Project required")), patch.object(discovery.sys, "frozen", True, create=True), patch.object(discovery.sys, "platform", "linux"), patch.object(discovery.shutil, "which", return_value="installed-python"):
            paths = list(discovery._candidate_paths())
        self.assertTrue(paths)
        self.assertTrue(all(source == "path" for _, source in paths))

    def test_priority_is_first_compatible(self):
        self.assertEqual(self.discover([identity(), identity()])["selected_python"], "0")

    def test_directml_excludes_313_and_wrong_platform(self):
        result = self.discover([identity("3.13.16"), identity(platform="linux"), identity()], "directml")
        self.assertEqual(result["selected_python"], "2")

    def test_probe_failure_does_not_hide_later_candidate(self):
        result = self.discover([subprocess.TimeoutExpired("python", 2), identity()])
        self.assertEqual(result["selected_python"], "1")
        self.assertFalse(result["candidates"][0]["compatible"])

    def test_explicit_incompatible_path_never_falls_back(self):
        with patch.object(discovery, "_candidate_paths") as paths, patch.object(discovery, "probe_python", return_value=identity("3.13.16")):
            result = discovery.discover_python("directml", explicit="chosen.exe")
        paths.assert_not_called()
        self.assertIsNone(result["selected_python"])
        self.assertEqual(len(result["candidates"]), 1)

    def test_venv_and_old_pip_are_not_recommended(self):
        venv, old = identity(), identity()
        venv["prefix"], old["bundled_pip"] = "venv", "21.0"
        self.assertEqual(self.discover([venv, old, identity()])["selected_python"], "2")

    def test_unsupported_version_identity_remains_visible(self):
        result = self.discover([identity("3.14.0")])
        self.assertIsNone(result["selected_python"])
        self.assertEqual(result["candidates"][0]["identity"]["python"], "3.14.0")

    def test_store_alias_is_not_probed(self):
        with patch.object(discovery, "_candidate_paths", return_value=[(r"C:\Users\user\AppData\Local\Microsoft\WindowsApps\python.exe", "path")]), patch.object(discovery, "probe_python") as probe:
            result = discovery.discover_python()
        probe.assert_not_called()
        self.assertIsNone(result["selected_python"])

    def test_duplicate_canonical_paths_are_only_probed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "python")
            alias = str(Path(tmp) / "." / "python")
            with patch.object(discovery, "_candidate_paths", return_value=[(path, "path"), (alias, "registry")]), patch.object(Path, "is_file", return_value=True), patch.object(discovery.os, "access", return_value=True), patch.object(discovery, "probe_python", return_value=identity()) as probe:
                result = discovery.discover_python()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(len(result["candidates"]), 1)

    def test_probe_count_bounded(self):
        result = self.discover([identity()] * 20)
        self.assertEqual(len(result["candidates"]), discovery.MAX_CANDIDATES)

    def test_existing_target_never_triggers_discovery_or_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("numpy>=1.24\n")
            (root / ".venv").mkdir()
            with patch.object(discovery, "discover_python") as discover:
                plan = manager.plan_environment(root, groups=("cpu",))
            discover.assert_not_called()
            self.assertEqual(plan["status"], "blocked")
            self.assertFalse(any(action["kind"] == "create_venv" for action in plan["actions"]))

    def test_selected_plan_is_reproducible_without_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("numpy>=1.24\n")
            with patch.object(discovery, "discover_python", return_value={"selected_python": sys.executable}) as discover:
                plan = manager.plan_environment(root, groups=("cpu",))
                replanned = manager.plan_environment(root, groups=("cpu",), python=plan["bootstrap_python"])
            self.assertEqual(discover.call_count, 1)
            self.assertEqual(plan, replanned)


if __name__ == "__main__":
    unittest.main()
