"""Offline optional Qt sound regressions; never play audio or load user settings."""
import builtins
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtQml import QQmlEngine
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NotificationSoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def module_with_sound(self, sound=None, error=None):
        real_import = builtins.__import__

        def intercepted(name, *args, **kwargs):
            if name == "PySide6.QtMultimedia":
                if error is not None:
                    raise error
                return SimpleNamespace(QSoundEffect=sound)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=intercepted):
            return load_module("sound_under_test", ROOT / "coat_sim/ui/assistant_desktop.py")

    @contextlib.contextmanager
    def bridge(self, module):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(module, "load_ui_settings", return_value={}), \
                patch.object(module, "CURSOR_PATH", Path(directory) / "cursor.json"):
            engine = QQmlEngine()
            bridge = module.DesktopAssistantBridge(engine)
            try:
                yield bridge
            finally:
                bridge.shutdown()

    def test_missing_module_warns_and_uses_system_beep(self):
        module = self.module_with_sound(error=ModuleNotFoundError(
            "missing sound extension", name="PySide6.QtMultimedia"))
        self.assertIsNone(module.QSoundEffect)
        with self.assertWarnsRegex(RuntimeWarning, "降级"), self.bridge(module) as bridge, \
                patch.object(module.QApplication, "beep") as beep:
            bridge._play_notification_sound()
            beep.assert_called_once_with()

    def test_available_module_preserves_bubble_sound(self):
        effect = Mock()
        factory = Mock(return_value=effect)
        module = self.module_with_sound(sound=factory)
        with self.bridge(module) as bridge, patch.object(module.QApplication, "beep") as beep:
            factory.assert_called_once_with(bridge)
            self.assertTrue(effect.setSource.call_args.args[0].toLocalFile().endswith("bubble_pop.wav"))
            effect.setVolume.assert_called_once_with(0.42)
            bridge._play_notification_sound()
            effect.play.assert_called_once_with()
            beep.assert_not_called()

    def test_other_missing_dependency_is_not_hidden(self):
        with self.assertRaises(ModuleNotFoundError):
            self.module_with_sound(error=ModuleNotFoundError("broken dependency", name="other_dependency"))

    def test_broken_native_import_is_not_hidden(self):
        with self.assertRaises(ImportError):
            self.module_with_sound(error=ImportError("shared library unavailable", name="PySide6.QtMultimedia"))

    def test_playback_error_is_not_hidden(self):
        effect = Mock()
        effect.play.side_effect = RuntimeError("audio backend failure")
        module = self.module_with_sound(sound=Mock(return_value=effect))
        with self.bridge(module) as bridge, self.assertRaisesRegex(RuntimeError, "audio backend"):
            bridge._play_notification_sound()

    def test_bubble_menu_targets_the_bridge_with_the_slot(self):
        module = self.module_with_sound(sound=Mock())
        self.assertIn(b"menuPosition", [module.DesktopAssistantBridge.staticMetaObject.method(i).name()
            for i in range(module.DesktopAssistantBridge.staticMetaObject.methodCount())])
        source = (ROOT / "coat_sim/ui/qml/AssistantBubble.qml").read_text()
        self.assertIn("desktop.menuPosition(", source)
        self.assertNotIn("assistant.menuPosition(", source)


class RuntimePreflightTests(unittest.TestCase):
    def preflight(self, missing=(), versions=None):
        module = load_module("preflight_under_test", ROOT / "tools/check_cloud_runtime.py")
        versions = versions or {}

        def version(name):
            if name in missing:
                raise module.metadata.PackageNotFoundError(name)
            return versions.get(name, "6.11.2" if name.startswith("PySide6") else "1.25.0")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requirements.txt").write_text("numpy>=1.24\nPySide6-Addons>=6.8,<7\n")
            output = io.StringIO()
            with patch.object(module, "ROOT", root), \
                    patch.object(module.metadata, "version", side_effect=version), \
                    patch.object(module.importlib, "import_module"), contextlib.redirect_stdout(output):
                result = module.main()
        return result, output.getvalue()

    def test_missing_sound_extension_warns_but_allows_startup(self):
        code, text = self.preflight(missing={"PySide6-Addons"})
        self.assertEqual(code, 0)
        self.assertIn("降级", text)

    def test_available_sound_extension_has_no_degradation(self):
        code, text = self.preflight()
        self.assertEqual(code, 0)
        self.assertNotIn("降级", text)

    def test_missing_required_dependency_still_fails(self):
        code, text = self.preflight(missing={"numpy"})
        self.assertEqual(code, 2)
        self.assertIn("未安装：numpy", text)

    def test_installed_unsupported_extension_version_still_fails(self):
        code, text = self.preflight(versions={"PySide6-Addons": "7.0.0"})
        self.assertEqual(code, 2)
        self.assertIn("版本不符合要求：PySide6-Addons", text)


if __name__ == "__main__":
    unittest.main()
