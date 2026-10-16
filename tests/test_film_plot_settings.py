"""Focused tests for the standalone film plot settings dialog."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from coat_sim.ui.film_plot_settings import FilmPlotSettingsDialog


_APP = QApplication.instance() or QApplication([])


class FilmPlotSettingsDialogTests(unittest.TestCase):
    def test_defaults_and_plain_values(self):
        dialog = FilmPlotSettingsDialog()
        values = dialog.values()
        self.assertEqual(values["curves"], ["R", "T", "A"])
        self.assertEqual(values["x"]["auto"], True)
        self.assertEqual(values["y"]["major_tick_auto"], True)
        self.assertEqual(values["grid"], True)
        self.assertEqual(set(dialog.curve_checks), {
            "R", "T", "A", "source_spectrum", "reflected_spectrum",
            "transmitted_spectrum", "absorbed_spectrum",
        })
        self.assertIn("nm", dialog.axis_controls["x"]["group"].title())
        self.assertIn("相对光谱", dialog.axis_controls["y"]["group"].title())
        dialog.close()

    def test_percent_input_and_idempotent_legacy_migration(self):
        legacy = {'y':{'auto':False,'min':0.0,'max':1.0,'major_tick_auto':False,'major_tick':.2}}
        dialog = FilmPlotSettingsDialog(legacy)
        y = dialog.axis_controls['y']
        self.assertEqual(y['maximum'].value(),100)
        self.assertEqual(y['tick'].value(),20)
        y['maximum'].setValue(75); y['tick'].setValue(25)
        values = dialog.values(); dialog.close()
        self.assertEqual(values['y']['max'],.75)
        self.assertEqual(values['y']['major_tick'],.25)
        self.assertEqual(values['y']['unit'],'fraction')
        for _ in range(3):
            reopened = FilmPlotSettingsDialog(values)
            self.assertEqual(reopened.values(),values); reopened.close()
        imported = FilmPlotSettingsDialog({'y':{'unit':'percent','auto':False,'min':10,'max':90,'major_tick':20}})
        self.assertEqual(imported.values()['y']['min'],.1)
        self.assertEqual(imported.values()['y']['max'],.9)
        imported.close()
        self.assertNotIn('unit',legacy['y'])

    def test_existing_options_are_displayed_and_round_trip(self):
        options = {
            "curves": ["R", "source_spectrum", "absorbed_spectrum"],
            "x": {"auto": False, "min": 410, "max": 720,
                  "major_tick_auto": False, "major_tick": 50},
            "y": {"auto": False, "min": -0.1, "max": 1.2,
                  "major_tick_auto": False, "major_tick": 0.25},
            "grid": False,
        }
        dialog = FilmPlotSettingsDialog(options)
        self.assertTrue(dialog.curve_checks["R"].isChecked())
        self.assertFalse(dialog.curve_checks["T"].isChecked())
        self.assertTrue(dialog.curve_checks["source_spectrum"].isChecked())
        self.assertFalse(dialog.grid_check.isChecked())
        values = dialog.values()
        self.assertEqual(values["x"]["min"], 410)
        self.assertEqual(values["x"]["max"], 720)
        self.assertEqual(values["x"]["major_tick"], 50)
        self.assertEqual(values["y"]["min"], -0.1)
        self.assertEqual(values["y"]["major_tick"], 0.25)
        self.assertFalse(values["x"]["auto"])
        self.assertFalse(values["y"]["major_tick_auto"])
        dialog.close()

    def test_apply_accepts_valid_snapshot_and_cancel_rejects(self):
        dialog = FilmPlotSettingsDialog()
        dialog.axis_controls["x"]["range_auto"].setChecked(False)
        dialog.axis_controls["x"]["minimum"].setValue(400)
        dialog.axis_controls["x"]["maximum"].setValue(800)
        dialog.axis_controls["x"]["tick_auto"].setChecked(False)
        dialog.axis_controls["x"]["tick"].setValue(25)
        dialog.button_box.button(QDialogButtonBox.Apply).click()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertEqual(dialog.values()["x"]["major_tick"], 25)

        cancelled = FilmPlotSettingsDialog()
        cancelled.button_box.button(QDialogButtonBox.Cancel).click()
        self.assertEqual(cancelled.result(), QDialog.Rejected)

    def test_explicit_range_and_tick_validation_blocks_apply(self):
        dialog = FilmPlotSettingsDialog()
        x = dialog.axis_controls["x"]
        x["range_auto"].setChecked(False)
        x["minimum"].setValue(600)
        x["maximum"].setValue(500)
        with self.assertRaisesRegex(ValueError, "最小值必须小于最大值"):
            dialog.values()
        dialog.button_box.button(QDialogButtonBox.Apply).click()
        self.assertEqual(dialog.result(), 0)
        self.assertIn("最小值必须小于最大值", dialog.error_label.text())

        x["minimum"].setValue(400)
        x["maximum"].setValue(800)
        x["tick_auto"].setChecked(False)
        x["tick"].setValue(0)
        with self.assertRaisesRegex(ValueError, "主刻度间距必须大于"):
            dialog.values()
        dialog.close()

    def test_y_range_and_nonpositive_wavelength_are_validated(self):
        dialog = FilmPlotSettingsDialog()
        y = dialog.axis_controls["y"]
        y["range_auto"].setChecked(False)
        y["minimum"].setValue(2)
        y["maximum"].setValue(1)
        with self.assertRaisesRegex(ValueError, "最小值必须小于最大值"):
            dialog.values()
        y["minimum"].setValue(-20)
        y["maximum"].setValue(120)
        self.assertEqual(dialog.values()["y"]["min"], -0.2)

        x = dialog.axis_controls["x"]
        x["range_auto"].setChecked(False)
        x["minimum"].setValue(0)
        x["maximum"].setValue(800)
        with self.assertRaisesRegex(ValueError, "波长范围必须大于"):
            dialog.values()
        dialog.close()


if __name__ == "__main__":
    unittest.main()
