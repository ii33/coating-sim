"""Focused unittest coverage for film simulation settings."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from coat_sim.ui.film_simulation_settings import FilmSimulationSettingsDialog

_APP = QApplication.instance() or QApplication([])


class FilmSimulationSettingsDialogTests(unittest.TestCase):
    def test_default_one_nm_and_waveband_span_changes_keep_step_priority(self):
        dialog = FilmSimulationSettingsDialog()
        self.assertEqual(dialog.wavelength_min_spin.value(), 400.0)
        self.assertEqual(dialog.wavelength_max_spin.value(), 700.0)
        self.assertEqual(dialog.step_spin.value(), 1.0)
        self.assertEqual(dialog.samples_spin.value(), 301)
        self.assertEqual(dialog.values()["extrapolation_policy"], "endpoint_hold")

        dialog.wavelength_max_spin.setValue(800.0)
        self.assertAlmostEqual(dialog.step_spin.value(), 1.0)
        self.assertEqual(dialog.samples_spin.value(), 401)
        dialog.wavelength_min_spin.setValue(450.0)
        self.assertAlmostEqual(dialog.step_spin.value(), 1.0)
        self.assertEqual(dialog.samples_spin.value(), 351)
        dialog.close()

    def test_range_change_prioritizes_step_and_sampling_inputs_synchronize(self):
        dialog = FilmSimulationSettingsDialog({
            "wavelength_min_nm": 400, "wavelength_max_nm": 700, "step_nm": 10,
        })
        self.assertEqual(dialog.samples_spin.value(), 31)
        dialog.wavelength_min_spin.setValue(500)
        self.assertEqual(dialog.samples_spin.value(), 21)
        self.assertEqual(dialog.wavelength_max_spin.value(), 700)
        self.assertAlmostEqual(dialog.step_spin.value(), 10)
        dialog.step_spin.setValue(7)
        self.assertEqual(dialog.values()["samples"], 30)
        self.assertAlmostEqual(dialog.values()["step_nm"], 200 / 29)
        dialog.samples_spin.setValue(5)
        self.assertEqual(dialog.values()["samples"], 5)
        self.assertAlmostEqual(dialog.values()["step_nm"], 50)
        self.assertEqual(dialog.values()["wavelength_min_nm"], 500)
        self.assertEqual(dialog.values()["wavelength_max_nm"], 700)
        dialog.close()

    def test_endpoints_and_large_point_counts_do_not_clip_silently(self):
        dialog = FilmSimulationSettingsDialog()
        low, high = dialog.wavelength_min_spin.value(), dialog.wavelength_max_spin.value()
        dialog.samples_spin.setValue(2)
        self.assertEqual(dialog.values()["samples"], 2)
        self.assertEqual(dialog.step_spin.value(), high - low)
        dialog.samples_spin.setValue(10001)
        self.assertEqual(dialog.values()["samples"], 10001)
        self.assertAlmostEqual(dialog.values()["step_nm"], (high - low) / 10000)
        dialog.step_spin.setValue(0.0001)
        self.assertEqual(dialog.samples_spin.value(), 3_000_001)
        self.assertEqual(dialog.values()["samples"], 3_000_001)
        dialog.step_spin.setValue(1.0e6)
        with self.assertRaisesRegex(ValueError, "少于 2"):
            dialog.values()
        dialog.samples_spin.setValue(2)
        self.assertEqual(dialog.values()["samples"], 2)

        dialog.samples_spin.setValue(3_000_000_001)
        self.assertEqual(dialog.samples_spin.value(), 3_000_000_001)
        self.assertEqual(dialog.values()["samples"], 3_000_000_001)
        self.assertGreater(dialog.values()["step_nm"], 0)

        for invalid in (1, 0):
            dialog.samples_spin.setValue(invalid)
            with self.assertRaisesRegex(ValueError, "至少 2"):
                dialog.values()
        dialog.samples_spin.setText("")
        with self.assertRaises(ValueError):
            dialog.values()
        dialog.samples_spin.setValue(301)
        self.assertEqual(dialog.values()["samples"], 301)

        dialog.step_spin.setText("")
        with self.assertRaises(ValueError):
            dialog.values()
        dialog.step_spin.setValue(1.0)
        self.assertEqual(dialog.values()["samples"], 301)

        dialog.samples_spin.setValue(2)
        dialog.wavelength_min_spin.setValue(dialog.wavelength_max_spin.value())
        with self.assertRaisesRegex(ValueError, "小于最大值"):
            dialog.values()
        dialog.close()

    def test_catalog_source_keeps_provenance_and_values_are_detached(self):
        source = {
            "kind": "tabulated", "wavelength_nm": [350.0, 550.0, 850.0],
            "intensity": [0.2, 1.0, 0.3], "source": "Lab spectrometer run 42",
        }
        expected_source = {**source, "wavelength_basis": "vacuum"}
        metadata = {"id": "lamp-42", "name": "Measured lamp", "source": source,
                    "standard_notes": "Calibration record CAL-42"}
        dialog = FilmSimulationSettingsDialog(light_sources=[metadata])
        index = dialog.light_source_combo.findText("Measured lamp")
        self.assertGreaterEqual(index, 0)
        dialog.light_source_combo.setCurrentIndex(index)
        self.assertEqual(dialog.values()["light_source"], expected_source)
        self.assertIn("Lab spectrometer run 42", dialog.source_notes_label.text())
        self.assertIn("Calibration record CAL-42", dialog.source_notes_label.text())
        output = dialog.values()
        output["light_source"]["wavelength_nm"][0] = 1
        self.assertEqual(dialog.values()["light_source"], expected_source)
        dialog.close()

    def test_loaded_tabulated_source_survives_stale_catalog_and_blackbody_round_trips(self):
        loaded = {"kind": "tabulated", "wavelength_nm": [300.0, 900.0],
                  "intensity": [0.5, 1.0], "source": "Archived spectrometer capture"}
        expected_loaded = {**loaded, "wavelength_basis": "vacuum"}
        stale = [
            {"id": "removed", "name": "Removed", "source": {"kind": "other"}},
            {"name": "Bad row", "source": {"kind": "tabulated",
                                            "wavelength_nm": [1], "intensity": [1]}},
        ]
        dialog = FilmSimulationSettingsDialog({"light_source": loaded}, stale)
        self.assertEqual(dialog.light_source_combo.currentText(), "已加载的离散光源")
        self.assertEqual(dialog.values()["light_source"], expected_loaded)
        self.assertIn("Archived spectrometer capture", dialog.source_notes_label.text())
        dialog.close()

        body = {"kind": "blackbody", "temperature_k": 3000.25}
        blackbody = FilmSimulationSettingsDialog({"light_source": body})
        self.assertEqual(blackbody.values()["light_source"], body)
        self.assertFalse(blackbody.blackbody_temperature_spin.isHidden())
        blackbody.close()

    def test_extrapolation_policy_round_trip_and_chinese_labels(self):
        dialog = FilmSimulationSettingsDialog({"extrapolation_policy": "error"})
        self.assertEqual(dialog.extrapolation_combo.currentData(), "error")
        self.assertEqual(dialog.values()["extrapolation_policy"], "error")
        self.assertEqual(dialog.windowTitle(), "仿真参数")
        self.assertEqual(dialog.button_box.button(QDialogButtonBox.Apply).text(), "应用")
        self.assertEqual(dialog.button_box.button(QDialogButtonBox.Cancel).text(), "取消")
        dialog.close()

    def test_angle_polarization_surface_and_keyboard_focus(self):
        dialog = FilmSimulationSettingsDialog({
            "angle_deg": 37.5, "polarization": "p", "first_surface": "back",
        })
        self.assertEqual(dialog.values()["angle_deg"], 37.5)
        self.assertEqual(dialog.values()["polarization"], "p")
        self.assertEqual(dialog.values()["first_surface"], "back")
        dialog.show()
        QApplication.processEvents()
        dialog.step_spin.setFocus(Qt.OtherFocusReason)
        QApplication.processEvents()
        self.assertIs(QApplication.focusWidget(), dialog.step_spin)
        editor = dialog.step_spin
        editor.selectAll()
        QTest.keyClicks(editor, "12")
        QTest.keyClick(editor, Qt.Key_Enter)
        QApplication.processEvents()
        self.assertEqual(dialog.values()["samples"], 26)
        dialog.button_box.button(QDialogButtonBox.Apply).click()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
