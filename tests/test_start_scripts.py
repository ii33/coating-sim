"""Offline launcher regressions: fake interpreters, no GUI, wallets, or network."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "requires POSIX and Bash")
class StartScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="coat-launch-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project with spaces"
        self.root.mkdir()
        for name in ("start.sh", "start-cloud.sh"):
            shutil.copy2(ROOT / name, self.root / name)
        self.capture = Path(self.temp.name) / "arguments.txt"
        self.env = os.environ.copy()
        for name in ("COAT_PYTHON", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                     "DBUS_SESSION_BUS_ADDRESS", "QT_QPA_PLATFORM", "COAT_TEST_EXIT"):
            self.env.pop(name, None)
        self.env["COAT_TEST_CAPTURE"] = str(self.capture)
        self.venv = self.fake_python(".venv/bin/python")
        self.conda = self.fake_python(".conda/bin/python")

    def fake_python(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nprintf "%s\\n" "$0" "$PWD" "$@" > "$COAT_TEST_CAPTURE"\n'
                        'exit "${COAT_TEST_EXIT:-0}"\n', encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_script(self, *args, script="start-cloud.sh", env=None, cwd=None):
        return subprocess.run([str(self.root / script), *args], env=env or self.env,
                              cwd=cwd or self.temp.name, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)

    def test_help_never_executes_python_or_creates_logs(self):
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("--native-render", result.stdout)
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.root / "artifacts").exists())

    def test_dry_run_prefers_venv_without_display(self):
        result = self.run_script("--dry-run", "--module", "settings")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(str(self.venv), result.stdout)
        self.assertIn("--software --module settings", result.stdout)
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.root / "artifacts").exists())

    def test_generic_wrapper_prefers_venv_and_preserves_arguments(self):
        result = self.run_script("--ready-file", "file with spaces.json", script="start.sh")
        self.assertEqual(result.returncode, 0, result.stdout)
        captured = self.capture.read_text().splitlines()
        self.assertEqual(captured[0], str(self.venv))
        self.assertEqual(captured[2:], [str(self.root / "launcher.pyw"),
                                       "--ready-file", "file with spaces.json"])

    def test_conda_fallback_for_both_wrappers(self):
        self.venv.unlink()
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(str(self.conda), result.stdout)
        result = self.run_script(script="start.sh")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.capture.read_text().splitlines()[0], str(self.conda))

    def test_relative_override_resolves_before_chdir(self):
        self.env["COAT_PYTHON"] = "./.conda/bin/python"
        result = self.run_script("--dry-run", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(str(self.conda), result.stdout)

    def test_invalid_override_fails_without_fallback(self):
        self.env["COAT_PYTHON"] = str(self.root / "missing-python")
        for script in ("start.sh", "start-cloud.sh"):
            result = self.run_script(script=script)
            self.assertEqual(result.returncode, 2, result.stdout)
        self.assertFalse(self.capture.exists())

    def test_check_without_display_exits_three_without_creating_logs(self):
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("没有 DISPLAY", result.stdout)
        self.assertFalse((self.root / "artifacts").exists())

    def test_missing_dependency_stops_before_launcher(self):
        self.env["COAT_TEST_EXIT"] = "2"
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(self.capture.read_text().splitlines()[2:],
                         [str(self.root / "tools/check_cloud_runtime.py")])

    def test_invisible_platform_is_rejected(self):
        self.env["QT_QPA_PLATFORM"] = "offscreen"
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("不会显示桌面窗口", result.stdout)

    def test_wayland_requires_runtime_dir(self):
        self.env["WAYLAND_DISPLAY"] = "test-only-no-display-connection"
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("XDG_RUNTIME_DIR", result.stdout)

    def test_fake_launch_logs_and_preserves_application_arguments(self):
        # The fake interpreter records argv only; it never connects to this display.
        self.env["DISPLAY"] = ":test-only"
        result = self.run_script("--module", "settings", "--ready-file", "with spaces.json")
        self.assertEqual(result.returncode, 0, result.stdout)
        captured = self.capture.read_text().splitlines()
        self.assertEqual(captured[1], str(self.root))
        self.assertEqual(captured[2:], ["-u", str(self.root / "launcher.py"),
                                       "--software", "--module", "settings",
                                       "--ready-file", "with spaces.json"])
        logs = list((self.root / "artifacts/logs").glob("cloud-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)

    def test_native_render_does_not_inject_software(self):
        result = self.run_script("--dry-run", "--native-render", "--graphics-info")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("--software", result.stdout)
        self.assertIn("--graphics-info", result.stdout)

    def test_explicit_software_is_not_duplicated(self):
        result = self.run_script("--dry-run", "--software")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count("--software"), 1)


if __name__ == "__main__":
    unittest.main()
