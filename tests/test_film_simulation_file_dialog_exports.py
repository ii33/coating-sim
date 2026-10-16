"""Exercise native FileDialog URL values through the real spectrum QML handlers."""
from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from tests import qt_test_guard  # noqa: F401

from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QSettings, QUrl
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression
from PySide6.QtQuickControls2 import QQuickStyle

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_simulation_quick_controller import create_film_simulation_quick_window


QQuickStyle.setStyle("Fusion")
_APP = QGuiApplication.instance() or QGuiApplication([])


class FilmSimulationFileDialogExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = FilmDesignService(self.root)
        geometry = QSettings(str(self.root / "geometry.ini"), QSettings.Format.IniFormat)
        self.engine, self.window, self.controller = create_film_simulation_quick_window(
            root=self.root, service=self.service, design=self.service.new_design(),
            geometry_settings=geometry)

    def tearDown(self):
        self.controller.close()
        self.window.close()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        self.temp.cleanup()

    def _dialog(self, name):
        dialog = self.window.findChild(QObject, name)
        self.assertIsNotNone(dialog, name)
        return dialog

    def _select(self, dialog, path):
        # Round-trip the encoded representation too: literal "%20" must stay
        # literal, while spaces, Chinese text, '#' and '%' decode exactly once.
        url = QUrl.fromEncoded(QUrl.fromLocalFile(str(path)).toEncoded())
        self.assertTrue(dialog.setProperty("selectedFile", url))
        self.assertEqual(dialog.property("selectedFile"), url)

    def _accept(self, dialog, path):
        self._select(dialog, path)
        # Emit the real Qt Quick FileDialog accepted signal, which executes the
        # production QML onAccepted binding with its real url-valued property.
        # Only the platform picker interaction is simulated.
        self.assertTrue(QMetaObject.invokeMethod(dialog, "accepted"))
        QCoreApplication.processEvents()

    def _evaluate(self, target, code):
        expression = QQmlExpression(QQmlEngine.contextForObject(target), target, code)
        value, _ = expression.evaluate()
        self.assertFalse(expression.hasError(), expression.error().toString())
        return value

    def _publish_result(self):
        self.controller._result = {
            "wavelength_nm": [400.0, 550.0, 700.0],
            "R": [0.25, 0.5, 0.75], "T": [0.75, 0.5, 0.25], "A": [0.0] * 3,
            "source_spectrum": [1.0] * 3,
            "reflected_spectrum": [0.25, 0.5, 0.75],
            "transmitted_spectrum": [0.75, 0.5, 0.25], "absorbed_spectrum": [0.0] * 3,
            "settings": {"polarization": "unpolarized"},
        }
        self.controller._stale = False
        self.controller._error = ""
        self.controller._refresh_plot_data()
        self.controller._render_plot()
        self.controller.stateChanged.emit()
        self.assertTrue(self.controller.canExport)

    def test_real_qml_accepted_writes_valid_csv_and_png_at_encoded_unicode_paths(self):
        self._publish_result()
        folder = self.root / "Research 空格 25%20 #"
        csv_path = folder / "光谱 25%20 #.csv"
        png_path = folder / "光谱 25%20 #.png"
        folder.mkdir()
        # Simulate the platform's final accepted signal after an existing-file
        # overwrite was approved; it carries the same selectedFile URL.
        csv_path.write_bytes(b"old CSV")
        png_path.write_bytes(b"old PNG")
        self._accept(self._dialog("filmSimulationCsvFileDialog"), csv_path)
        self.assertTrue(csv_path.is_file(), self.controller.status)
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([float(row["wavelength_nm"]) for row in rows], [400.0, 550.0, 700.0])
        self.assertEqual([float(row["R"]) for row in rows], [0.25, 0.5, 0.75])
        self.assertEqual(json.loads(rows[0]["evaluation_report_json"])["units"]["wavelength_nm"], "nm")

        # Use the smallest supported real render, rather than mocking the PNG
        # writer or merely verifying that the controller was called.
        self.assertTrue(self.controller.setPngExportSettings({
            "width_in": 4.0, "height_in": 3.0, "dpi": 72, "transparent": False,
        }))
        self._accept(self._dialog("filmSimulationPngFileDialog"), png_path)
        self.assertTrue(png_path.is_file(), self.controller.status)
        self.assertTrue(png_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        image = QImage(str(png_path))
        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (288, 216))
        self.assertGreater(len({image.pixel(x, y) for x in range(image.width())
                                for y in range(image.height())}), 8)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), [csv_path.name, png_path.name])

    def test_export_cancel_writes_nothing_and_preserves_existing_destinations(self):
        self._publish_result()
        self.window.show()
        QCoreApplication.processEvents()
        for suffix, name in (("csv", "filmSimulationCsvFileDialog"),
                             ("png", "filmSimulationPngFileDialog")):
            with self.subTest(suffix=suffix):
                dialog = self._dialog(name)
                # Native overwrite confirmation must remain enabled for both
                # export dialogs. Tests simulate acceptance only after that UI.
                self.assertTrue(self._evaluate(
                    dialog, "!(options & FileDialog.DontConfirmOverwrite)"))
                for existing in (False, True):
                    target = self.root / f"cancel {existing}.{suffix}"
                    if existing:
                        target.write_bytes(b"keep existing contents")
                    self._select(dialog, target)
                    self.assertTrue(QMetaObject.invokeMethod(dialog, "open"))
                    QCoreApplication.processEvents()
                    self.assertTrue(dialog.property("visible"))
                    self.assertTrue(QMetaObject.invokeMethod(dialog, "reject"))
                    QCoreApplication.processEvents()
                    self.assertFalse(dialog.property("visible"))
                    if existing:
                        self.assertEqual(target.read_bytes(), b"keep existing contents")
                    else:
                        self.assertFalse(target.exists())

    def test_same_page_design_save_load_and_overwrite_confirmation_use_local_paths(self):
        target = self.root / "膜系 save 25%20 #.cfilm"
        dialog = self._dialog("filmSimulationSaveDesignFileDialog")
        self._accept(dialog, target)
        self.assertTrue(target.is_file(), self.controller.status)
        original = target.read_bytes()
        self.controller._design["name"] = "replacement name"
        self._accept(dialog, target)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.window.property("pendingConfirmationKind"), "overwrite")
        confirmation = self._dialog("filmSimulationReplacementConfirmation")
        self.assertTrue(QMetaObject.invokeMethod(confirmation, "reject"))
        QCoreApplication.processEvents()
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.window.property("pendingConfirmationKind"), "")
        self._accept(dialog, target)
        self.assertTrue(QMetaObject.invokeMethod(confirmation, "accept"))
        QCoreApplication.processEvents()
        self.assertEqual(self.service.load(target)["name"], "replacement name")

        load_target = self.root / "加载 25%20 #.cfilm"
        design = self.service.new_design()
        design["name"] = "loaded through QML url"
        self.service.save(load_target, design)
        self._accept(self._dialog("filmSimulationDesignFileDialog"), load_target)
        self.assertEqual(self.controller.designName, "loaded through QML url")

    def test_qml_url_has_no_toLocalFile_member_and_typed_bridge_keeps_windows_paths(self):
        component = QQmlComponent(self.engine)
        component.setData(b"""
            import QtQml
            QtObject {
                property url selectedFile
                readonly property string memberType: typeof selectedFile.toLocalFile
                readonly property string localPath: FilmSimulation.localFilePath(selectedFile)
            }
        """, QUrl())
        probe = component.create()
        self.assertIsNotNone(probe, component.errors())
        try:
            self.assertEqual(probe.property("memberType"), "undefined")
            for encoded, expected in (
                ("file:///C:/Research%20Data/%E5%85%89%E8%B0%B1%2025%2520%23.csv",
                 ("" if os.name == "nt" else "/") + "C:/Research Data/光谱 25%20#.csv"),
                ("file://server/share/Research%20Data/%E5%85%89%E8%B0%B1.png",
                 "//server/share/Research Data/光谱.png"),
            ):
                with self.subTest(encoded=encoded):
                    self.assertTrue(probe.setProperty("selectedFile", QUrl(encoded)))
                    # The production slot receives QUrl, never a file:// path
                    # string nor a hand-decoded/drive-letter-stripped value.
                    self.assertEqual(probe.property("localPath"), expected)
        finally:
            probe.deleteLater()

    def test_non_file_and_empty_urls_cannot_reach_file_writers(self):
        self._publish_result()
        for url in (QUrl(), QUrl("https://example.invalid/spectrum.csv"),
                    QUrl("qrc:/spectrum.csv")):
            self.assertEqual(self.controller.localFilePath(url), "")
        for name in ("filmSimulationCsvFileDialog", "filmSimulationPngFileDialog"):
            dialog = self._dialog(name)
            self.assertTrue(dialog.setProperty("selectedFile", QUrl("https://example.invalid/result.csv")))
            self.assertTrue(QMetaObject.invokeMethod(dialog, "accepted"))
            self.assertIn("本地文件", self.controller.status)
        self.assertFalse((self.root / "https:").exists())


if __name__ == "__main__":
    unittest.main()
