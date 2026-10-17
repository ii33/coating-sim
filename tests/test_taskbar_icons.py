"""Qt-native desktop identity regressions; no global desktop registration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from coat_sim.ui.icons import TASKBAR_ICON_SIZES, window_icon
from coat_sim.ui.taskbar import APP_ID, APP_NAME, MODULE_NAMES, application_id
from coat_sim.ui import desktop_identity

ROOT = Path(__file__).resolve().parents[1]


class TaskbarIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_bounded_multisize_icon_fits_baseline_x11_request(self):
        icon = window_icon("launcher")
        self.assertFalse(icon.isNull())
        sizes = icon.availableSizes()
        self.assertEqual([(s.width(),s.height()) for s in sizes], [(s,s) for s in TASKBAR_ICON_SIZES])
        # X11's baseline maximum is 65535 four-byte units; leave room for headers.
        self.assertLess(sum(2+s.width()*s.height() for s in sizes)+32, 65535)
        for size in sizes:
            self.assertFalse(icon.pixmap(size).isNull())

    def test_independent_modules_have_stable_desktop_identity(self):
        code = '''import json
from PySide6.QtWidgets import QApplication
from coat_sim.ui.icons import window_icon
app=QApplication([])
window_icon(PRIMARY)
before=app.desktopFileName()
window_icon("settings")
window_icon("launcher")
print(json.dumps({"before":before,"after":app.desktopFileName(),"name":app.applicationName(),"displayName":app.applicationDisplayName(),"platform":app.platformName()}))
'''
        for module in ("launcher","main","mask","process"):
            result = subprocess.run([sys.executable,"-c",code.replace("PRIMARY",repr(module))],
                cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=20, check=True)
            data = json.loads(result.stdout)
            expected = application_id(module)
            self.assertEqual(data["before"], expected)
            self.assertEqual(data["after"], data["before"])
            self.assertEqual(data["name"], expected if sys.platform == "linux" else "coat-sim")
            self.assertEqual(data["displayName"], "")
            self.assertEqual(APP_NAME, "光学镀膜")
            self.assertEqual(MODULE_NAMES["launcher"], APP_NAME)
            self.assertEqual(MODULE_NAMES["main"], "沉积仿真")
        self.assertEqual(application_id("launcher"), APP_ID)

    def test_independent_qt_windows_inherit_icon_before_show_and_reopen(self):
        icon = window_icon("mask")
        for _ in range(3):
            window = QWindow()
            window.resize(300,200)
            window.show()
            QTest.qWait(10)
            self.assertEqual(window.icon().cacheKey(), icon.cacheKey())
            window.hide()
            window.destroy()
            window.show()
            QTest.qWait(10)
            self.assertEqual(window.icon().cacheKey(), icon.cacheKey())
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


class ProjectDesktopMetadataTests(unittest.TestCase):
    def test_project_entry_is_local_idempotent_and_has_correct_relaunch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout with spaces"
            root.mkdir()
            icon = root / "logolite.png"
            icon.write_bytes(b"icon bytes are not parsed by metadata generator")
            with patch.object(sys, "argv", ["launcher.py", "--software"]):
                path = desktop_identity.write_project_desktop_entry("mask", icon, root=root)
                contents = path.read_text()
                modified = path.stat().st_mtime_ns
                self.assertEqual(desktop_identity.write_project_desktop_entry("mask",icon,root=root),path)
                self.assertEqual(path.stat().st_mtime_ns,modified)
            self.assertEqual(path.parent, root / "artifacts" / "desktop")
            self.assertIn("Name=修正板",contents)
            self.assertIn("Icon=" + str(icon).replace("\\", "\\\\"),contents)
            self.assertIn('"--module" "mask" "--software"',contents)
            self.assertIn('"'+str(root / "launcher.py").replace("\\", "\\\\\\\\")+'"',contents)
            self.assertIn("StartupWMClass="+application_id("mask"),contents)
            self.assertEqual(list(root.glob("**/*.desktop")),[path])

    def test_explicit_registration_preserves_existing_and_rolls_back_only_its_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            data_home = Path(directory) / "user-data"
            applications = data_home / "applications"
            applications.mkdir(parents=True)
            previous = applications / (application_id("main") + ".desktop")
            previous.write_text("previous project launcher")
            unrelated = applications / "unrelated.desktop"
            unrelated.write_text("unrelated")
            manifest = desktop_identity.install_desktop_entries(["main","mask"],root=root,data_home=data_home)
            record = json.loads(manifest.read_text())
            self.assertEqual(len(record["entries"]),2)
            self.assertEqual(Path(record["entries"][0]["backup"]).read_text(),"previous project launcher")
            self.assertEqual(unrelated.read_text(),"unrelated")
            self.assertIn("Name=膜厚仿真",previous.read_text())
            self.assertTrue((applications / (application_id("mask")+".desktop")).exists())
            desktop_identity.restore_desktop_entries(manifest)
            self.assertEqual(previous.read_text(),"previous project launcher")
            self.assertFalse((applications / (application_id("mask")+".desktop")).exists())
            self.assertEqual(unrelated.read_text(),"unrelated")

    def test_rollback_does_not_overwrite_a_registration_modified_after_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            data_home = Path(directory) / "user-data"
            manifest = desktop_identity.install_desktop_entries(["main"],root=root,data_home=data_home)
            destination = data_home / "applications" / (application_id("main")+".desktop")
            destination.write_text("new user change")
            with self.assertRaisesRegex(ValueError,"changed since install"):
                desktop_identity.restore_desktop_entries(manifest)
            self.assertEqual(destination.read_text(),"new user change")

    def test_explicit_registration_refuses_symlinked_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            data_home = Path(directory) / "user-data"
            applications = data_home / "applications"
            applications.mkdir(parents=True)
            other = Path(directory) / "other.desktop"
            other.write_text("keep")
            (applications / (application_id("main")+".desktop")).symlink_to(other)
            with self.assertRaisesRegex(ValueError,"symlink"):
                desktop_identity.install_desktop_entries(["main"],root=root,data_home=data_home)
            self.assertEqual(other.read_text(),"keep")

    def test_invalid_module_cannot_escape_project_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,"Unknown application module"):
                desktop_identity.write_project_desktop_entry("../../escape",Path("icon.png"),root=Path(directory))
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_exec_percent_and_shell_metacharacters_are_escaped(self):
        self.assertEqual(desktop_identity._exec_argument("a%name"),'"a%%name"')
        # Each Exec quote/backslash is escaped again for desktop-entry storage.
        self.assertEqual(desktop_identity._exec_argument('a"b'), '"a\\\\"b"')
        self.assertEqual(desktop_identity._value("a\nb"),"a\\nb")


if __name__ == "__main__":
    unittest.main()
