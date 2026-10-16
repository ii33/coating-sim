"""Exercise Qt Quick fallback FileDialog handlers; validate Windows native pickers separately."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QSG_RHI_BACKEND", "software")
from tests import qt_test_guard  # noqa: F401 -- isolate settings before Qt imports

from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QSignalSpy, QTest

from coat_sim.core import materials
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.ui import settings
from coat_sim.ui.local_files import local_file_path
from coat_sim.ui.source_materials import MaterialLibraryBridge
from coat_sim.ui.theme_service import ThemeService


QQuickStyle.setStyle("Fusion")
_APP = QGuiApplication.instance() or QGuiApplication([])


class LocalFileDialogPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="coat-local-file-dialog-")
        self.root = Path(self.temp.name)
        self.old_package_root = ThemeService.PACKAGE_ROOT
        self.old_theme_service = ThemeService._instance
        self.old_settings_path = settings.SETTINGS_PATH
        self.old_palette = QGuiApplication.palette()
        self.old_font = QGuiApplication.font()
        ThemeService.PACKAGE_ROOT = self.root / "themes"
        ThemeService._instance = None
        settings.SETTINGS_PATH = self.root / "ui_settings.json"
        self.theme_service = ThemeService.instance()
        self.engines = []
        self.bridges = []
        self.network_factories = []
        self.file_dialog_names = []

    def tearDown(self):
        for engine in self.engines:
            for window in engine.rootObjects():
                for name in self.file_dialog_names:
                    picker = window.findChild(QObject, name)
                    if picker is None or not bool(picker.property("visible")):
                        continue
                    self.assertTrue(QMetaObject.invokeMethod(picker, "reject"))
                    self._wait_until(
                        lambda engine=engine, name=name: self._file_dialog_hidden(engine, name),
                        f"{name} did not close during teardown")
        for bridge in self.bridges:
            if isinstance(bridge, settings.SettingsBridge):
                bridge.closeModelProbe()
                bridge.closeAssistantSettingsBootstrap()
                bridge.closeMcpConnections()
                bridge._mcp_executor.shutdown(wait=False, cancel_futures=True)
            else:
                bridge.waitForBackgroundTasks()
        for engine in self.engines:
            for window in engine.rootObjects():
                if hasattr(window, "close"):
                    window.close()
            engine.deleteLater()
        self.theme_service.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        QGuiApplication.setPalette(self.old_palette)
        QGuiApplication.setFont(self.old_font)
        settings.SETTINGS_PATH = self.old_settings_path
        ThemeService.PACKAGE_ROOT = self.old_package_root
        ThemeService._instance = self.old_theme_service
        self.temp.cleanup()

    def _engine(self, bridge, qml_file):
        from tests.qt_test_guard import NoNetworkAccessManagerFactory

        engine = QQmlApplicationEngine()
        factory = NoNetworkAccessManagerFactory()
        self.network_factories.append(factory)
        engine.setNetworkAccessManagerFactory(factory)
        if bridge.parent() is None:
            bridge.setParent(engine)
        self.theme_service.attach_engine(engine)
        engine.rootContext().setContextProperty("bridge", bridge)
        warnings: list[str] = []
        engine.warnings.connect(lambda errors: warnings.extend(str(error) for error in errors))
        engine.load(QUrl.fromLocalFile(str(Path(__file__).parents[1] / "coat_sim/ui/qml" / qml_file)))
        self.assertEqual(len(engine.rootObjects()), 1, "QML failed: " + "\n".join(warnings))
        self.engines.append(engine)
        self.bridges.append(bridge)
        return engine, engine.rootObjects()[0]

    def _dialog(self, root, name):
        item = root.findChild(QObject, name)
        self.assertIsNotNone(item, name)
        return item

    def _file_dialog_hidden(self, engine, name):
        for window in engine.rootObjects():
            picker = window.findChild(QObject, name)
            if picker is not None and bool(picker.property("visible")):
                return False
        return True

    def _track_file_dialog(self, picker, enum_expression):
        self.assertIsNotNone(picker)
        self.assertFalse(bool(picker.property("visible")), "FileDialog fallback must be set before open")
        option = int(self._evaluate(picker, enum_expression))
        original = int(self._evaluate(picker, "options"))
        expected = original | option
        self._evaluate(picker, f"options = options | {enum_expression}")
        actual = int(self._evaluate(picker, "options"))
        self.assertEqual(actual, expected)
        self.assertTrue(actual & option, "DontUseNativeDialog option was not enabled")
        self.file_dialog_names.append(picker.objectName())
        return picker

    def _find(self, root, name):
        return root.findChild(QObject, name)

    def _text_property(self, target: QObject, name: str) -> str:
        value = target.property(name)
        assert isinstance(value, str), f"{name} must expose text"
        return value

    def _import_preview(self, bridge: MaterialLibraryBridge) -> list[dict[str, object]]:
        value = bridge.property("importPreview")
        assert isinstance(value, list), "importPreview must expose a list"
        rows: list[dict[str, object]] = []
        for row in value:
            assert isinstance(row, dict), "importPreview entries must be maps"
            assert all(isinstance(key, str) for key in row), "preview map keys must be text"
            rows.append(dict(row))
        return rows

    def _encoded_url(self, path):
        return QUrl.fromEncoded(QUrl.fromLocalFile(str(path)).toEncoded())

    def _select(self, dialog, path_or_url):
        url = path_or_url if isinstance(path_or_url, QUrl) else self._encoded_url(path_or_url)
        self.assertTrue(dialog.setProperty("selectedFile", url))
        self.assertEqual(dialog.property("selectedFile"), url)

    def _assert_selected(self, dialog, path_or_url):
        expected = path_or_url if isinstance(path_or_url, QUrl) else self._encoded_url(path_or_url)
        self.assertEqual(dialog.property("selectedFile"), expected)

    def _accept(self, dialog, path_or_url):
        self.assertTrue(bool(dialog.property("visible")), "FileDialog must be open before accept")
        accepted = self._signal_spy(dialog, "accepted()")
        self._assert_selected(dialog, path_or_url)
        self.assertTrue(QMetaObject.invokeMethod(dialog, "accept"))
        self._wait_until(lambda: not bool(dialog.property("visible")),
                         f"{dialog.objectName()} did not hide after accept")
        self._assert_selected(dialog, path_or_url)
        self.assertEqual(accepted.count(), 1, "accept() did not emit accepted() exactly once")

    def _reject(self, dialog, path):
        self.assertTrue(bool(dialog.property("visible")), "FileDialog must be open before reject")
        rejected = self._signal_spy(dialog, "rejected()")
        self._assert_selected(dialog, path)
        self.assertTrue(QMetaObject.invokeMethod(dialog, "reject"))
        self._wait_until(lambda: not bool(dialog.property("visible")),
                         f"{dialog.objectName()} did not hide after reject")
        self._assert_selected(dialog, path)
        self.assertEqual(rejected.count(), 1, "reject() did not emit rejected() exactly once")

    def _signal_spy(self, target, signature):
        index = target.metaObject().indexOfSignal(signature)
        self.assertGreaterEqual(index, 0, f"{target.objectName()} has no {signature} signal")
        method = target.metaObject().method(index)
        spy = QSignalSpy(target, method)
        self.assertTrue(spy.isValid(), f"Could not watch {target.objectName()}.{signature}")
        return spy

    def _trigger_missing_selection_guard(self, picker, no_output_path=None,
                                        expect_empty_url=True):
        # Invoke the handler on a hidden dialog to cover its missing/non-local URL
        # guard only; this intentionally does not model an opened picker lifecycle.
        self.assertFalse(bool(picker.property("visible")), "Empty-URL guard starts hidden")
        self.assertTrue(picker.setProperty("selectedFile", QUrl()))
        selected_file = picker.property("selectedFile")
        self.assertIsInstance(selected_file, QUrl)
        if expect_empty_url:
            self.assertTrue(selected_file.isEmpty(), "Expected empty selectedFile")
        else:
            # SaveFile dialogs may apply their configured suffix to an empty URL.
            self.assertFalse(selected_file.isLocalFile())
            self.assertEqual(local_file_path(selected_file), "")
        accepted = self._signal_spy(picker, "accepted()")
        self.assertTrue(QMetaObject.invokeMethod(picker, "accepted"))
        QCoreApplication.processEvents()
        self.assertEqual(accepted.count(), 1)
        self.assertFalse(bool(picker.property("visible")), "Missing-selection handler must remain hidden")
        if no_output_path is not None:
            self.assertFalse(no_output_path.exists())

    def _open_picker(self, picker, target=None, method="open", path_or_url=None):
        self.assertFalse(bool(picker.property("visible")), "FileDialog must be hidden before open")
        if path_or_url is not None:
            self._select(picker, path_or_url)
        receiver = picker if target is None else target
        self.assertTrue(QMetaObject.invokeMethod(receiver, method))
        self._wait_until(
            lambda: bool(picker.property("visible")) and (
                path_or_url is None or
                picker.property("selectedFile") == (
                    path_or_url if isinstance(path_or_url, QUrl)
                    else self._encoded_url(path_or_url))),
            f"{picker.objectName()} did not become visible with the selected file after {method}()")

    def _evaluate(self, target, code):
        expression = QQmlExpression(QQmlEngine.contextForObject(target), target, code)
        value, _ = expression.evaluate()
        self.assertFalse(expression.hasError(), expression.error().toString())
        return value

    def _wait_until(self, predicate, message, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            QCoreApplication.processEvents()
            if predicate():
                return
            QTest.qWait(20)
        self.fail(message)

    def test_settings_qml_installs_local_theme_and_ignores_cancel_or_remote_url(self):
        bridge = settings.SettingsBridge()
        bridge.setAssistantConfig(False, "", "")
        engine, window = self._engine(bridge, "SettingsWindow.qml")
        window.show()
        QCoreApplication.processEvents()
        picker = self._track_file_dialog(self._dialog(window, "themePackagePicker"),
                                         "FileDialog.DontUseNativeDialog")
        self.assertEqual(self._evaluate(picker, "typeof selectedFile.toLocalFile"), "undefined")
        self._trigger_missing_selection_guard(picker)
        self.assertEqual(list(self.theme_service.PACKAGE_ROOT.iterdir()), [])
        self.assertIn("请选择本地文件", self._text_property(bridge, "themePackageStatus"))

        package_path = self.root / "主题 空格 25%20 #" / "主题 包 25%20.zip"
        package_path.parent.mkdir(parents=True)
        manifest = {"formatVersion": 1, "id": "dialog-path-test",
                    "name": "Dialog Path Test", "modes": {"light": {}}}
        with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("theme.json", json.dumps(manifest, ensure_ascii=False))

        cancelled = self.root / "取消主题.zip"
        cancelled.write_bytes(b"cancelled theme selection")
        self._open_picker(picker, path_or_url=cancelled)
        self._reject(picker, cancelled)
        self.assertEqual(cancelled.read_bytes(), b"cancelled theme selection")
        self.assertFalse((self.theme_service.PACKAGE_ROOT / "dialog-path-test").exists())

        # Qt Quick's real open-file picker rejects non-file selectedFile values;
        # exercise the same QUrl bridge input directly and verify no install.
        self.assertEqual(bridge.localFilePath(QUrl("https://example.invalid/remote-theme.zip")), "")
        self.assertEqual(self._evaluate(window,
                                        'window.settingsBridge.localFilePath(Qt.resolvedUrl("https://example.invalid/remote-theme.zip"))'),
                         "")
        self.assertFalse((self.theme_service.PACKAGE_ROOT / "dialog-path-test").exists())
        self.assertIn("请选择本地文件", self._text_property(bridge, "themePackageStatus"))

        self._open_picker(picker, path_or_url=package_path)
        self._accept(picker, package_path)
        installed = self.theme_service.PACKAGE_ROOT / "dialog-path-test" / "theme.json"
        self.assertTrue(installed.is_file(), bridge.themePackageStatus)
        self.assertEqual(json.loads(installed.read_text(encoding="utf-8"))["id"], "dialog-path-test")
        self.assertEqual(bridge.themePackageId, "dialog-path-test")

    def test_material_qml_accepted_handlers_export_import_and_save_real_files(self):
        target_dir = self.root / "material library 空格 25%20 #"
        target_profile = materials.default_material_profile("导出材料")
        materials.save_material(target_dir, target_profile)
        bridge = MaterialLibraryBridge(directory=target_dir)
        engine, host = self._engine(bridge, "MaterialLibraryHost.qml")
        host.show()
        self.assertTrue(QMetaObject.invokeMethod(host, "openEditor"))
        self._wait_until(lambda: host.property("editorInstance") is not None,
                         "MaterialLibraryDialog incubation did not finish")
        self._wait_until(lambda: self._find(host, "materialLibraryDialog") is not None,
                         "MaterialLibraryDialog was not created")
        dialog = self._dialog(host, "materialLibraryDialog")
        self._wait_until(lambda: not bridge.loading,
                         "material catalog did not finish loading")
        self.assertTrue(bridge.selectMaterial("user:" + target_profile["id"]))
        self._wait_until(lambda: not bridge.loading and bridge.currentId == target_profile["id"],
                         "custom material was not selected")

        single_picker = self._track_file_dialog(
            self._dialog(dialog, "singleMaterialExportPicker"),
            "FileDialogs.FileDialog.DontUseNativeDialog")
        library_picker = self._track_file_dialog(
            self._dialog(dialog, "materialLibraryExportPicker"),
            "FileDialogs.FileDialog.DontUseNativeDialog")
        import_picker = self._track_file_dialog(
            self._dialog(dialog, "materialImportFilePicker"),
            "FileDialogs.FileDialog.DontUseNativeDialog")
        self.assertEqual(self._evaluate(single_picker, "typeof selectedFile.toLocalFile"), "undefined")
        self._trigger_missing_selection_guard(single_picker,
                                              self.root / "empty selection.cmat",
                                              expect_empty_url=False)
        self._trigger_missing_selection_guard(library_picker,
                                              self.root / "empty library selection.cmatl",
                                              expect_empty_url=False)
        self._trigger_missing_selection_guard(import_picker,
                                              target_dir / "empty import.json")
        self.assertFalse(bridge.loading)
        cancelled = self.root / "cancelled output.cmat"
        self._open_picker(single_picker, target=dialog, method="openSingleExport",
                          path_or_url=cancelled)
        self._reject(single_picker, cancelled)
        self.assertFalse(cancelled.exists())
        self.assertEqual(bridge.localFilePath(QUrl("https://example.invalid/remote.cmat")), "")
        self.assertFalse((self.root / "remote.cmat").exists())
        self.assertIn("请选择本地文件", self._text_property(bridge, "status"))

        single_path = self.root / "单项 导出 25%20 #.cmat"
        self._open_picker(single_picker, target=dialog, method="openSingleExport",
                          path_or_url=single_path)
        self._accept(single_picker, single_path)
        self._wait_until(lambda: not bridge.loading and single_path.is_file(),
                         "single material export did not write its package")
        self.assertEqual(json.loads(single_path.read_text(encoding="utf-8"))["kind"],
                         "coat_sim.material")

        library_path = self.root / "批量 导出 25%20 #.cmatl"
        selection = self._dialog(dialog, "materialLibraryExportSelectionDialog")
        self.assertTrue(QMetaObject.invokeMethod(selection, "open"))
        self._wait_until(lambda: bool(selection.property("visible")),
                         "material library export selection dialog did not open")
        confirm = self._dialog(selection, "confirmMaterialLibraryExportSelection")
        self._select(library_picker, library_path)
        self.assertTrue(QMetaObject.invokeMethod(confirm, "click"))
        self._wait_until(lambda: not bool(selection.property("visible")),
                         "material library export selection dialog did not close")
        self._wait_until(lambda: bool(library_picker.property("visible")),
                         "material library export picker did not open")
        self._reject(library_picker, library_path)
        self.assertFalse(library_path.exists())
        self.assertTrue(QMetaObject.invokeMethod(selection, "open"))
        self._wait_until(lambda: bool(selection.property("visible")),
                         "material library export selection dialog did not reopen")
        self._select(library_picker, library_path)
        self.assertTrue(QMetaObject.invokeMethod(confirm, "click"))
        self._wait_until(lambda: not bool(selection.property("visible")),
                         "material library export selection dialog did not close after confirmation")
        self._wait_until(lambda: bool(library_picker.property("visible")),
                         "material library export picker did not reopen")
        self._accept(library_picker, library_path)
        self._wait_until(lambda: not bridge.loading and library_path.is_file(),
                         "batch material export did not write its package")
        self.assertEqual(json.loads(library_path.read_text(encoding="utf-8"))["kind"],
                         "coat_sim.material_library")

        source_dir = self.root / "incoming library"
        incoming = materials.default_material_profile("导入材料", id="qml-import-material")
        materials.save_material(source_dir, incoming)
        import_path = self.root / "导入 包 25%20 #.cmat"
        MaterialLibrary(directory=source_dir).export_single_file(incoming["id"], import_path)
        self.assertFalse(bridge.loading)
        self.assertFalse((target_dir / (incoming["id"] + ".json")).exists())
        cancelled_import = self.root / "cancelled import.cmat"
        cancelled_import.write_bytes(b"cancelled import selection")
        self._open_picker(import_picker, path_or_url=cancelled_import)
        self._reject(import_picker, cancelled_import)
        self.assertEqual(cancelled_import.read_bytes(), b"cancelled import selection")
        self.assertFalse((target_dir / (incoming["id"] + ".json")).exists())
        remote_url = QUrl("https://example.invalid/remote-import.cmat")
        self.assertEqual(bridge.localFilePath(remote_url), "")
        self.assertEqual(self._evaluate(dialog, 'localPath(Qt.resolvedUrl("https://example.invalid/remote-import.cmat"))'), "")
        self.assertFalse((target_dir / (incoming["id"] + ".json")).exists())
        self.assertIn("请选择本地文件", self._text_property(bridge, "status"))
        self._open_picker(import_picker, path_or_url=import_path)
        self._accept(import_picker, import_path)

        preview = self._dialog(dialog, "materialLibraryImportPreviewDialog")
        self._wait_until(lambda: len(self._import_preview(bridge)) == 1 and bool(preview.property("visible")),
                         "import preview was not produced from the selected package")
        self.assertEqual(self._import_preview(bridge)[0]["id"], incoming["id"])
        cancel_preview = self._dialog(preview, "cancelMaterialImportPreviewButton")
        self.assertTrue(QMetaObject.invokeMethod(cancel_preview, "click"))
        self._wait_until(lambda: len(self._import_preview(bridge)) == 0 and not bool(preview.property("visible")),
                         "cancelling the import preview did not discard it")
        self.assertFalse((target_dir / (incoming["id"] + ".json")).exists())

        self._open_picker(import_picker, path_or_url=import_path)
        self._accept(import_picker, import_path)
        self._wait_until(lambda: len(self._import_preview(bridge)) == 1 and bool(preview.property("visible")),
                         "reopening the import package did not produce a fresh preview")
        self.assertEqual(self._import_preview(bridge)[0]["id"], incoming["id"])
        apply_button = self._dialog(preview, "applyMaterialImportButton")
        self.assertTrue(QMetaObject.invokeMethod(apply_button, "click"))
        saved = target_dir / (incoming["id"] + ".json")
        self._wait_until(lambda: not bridge.loading and saved.is_file(),
                         "confirming the QML import did not persist the material")
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["name"], "导入材料")

    def test_local_file_path_uses_qt_decoding_and_rejects_nonlocal_urls(self):
        path = self.root / "路径 空格 25%20 #.txt"
        encoded = QUrl.fromEncoded(QUrl.fromLocalFile(str(path)).toEncoded())
        # QUrl::toLocalFile() returns forward slashes on every platform.
        self.assertEqual(local_file_path(encoded), path.as_posix())
        for url in (QUrl(), QUrl("https://example.invalid/a"), QUrl("file://")):
            self.assertEqual(local_file_path(url), "")


if __name__ == "__main__":
    unittest.main()
