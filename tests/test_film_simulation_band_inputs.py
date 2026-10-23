"""Real offscreen keyboard coverage for film-statistics band editors."""

import os
import tempfile
import unittest

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QLocale, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from tests.qt_test_guard import qml_item_window
from coat_sim.ui.film_simulation_quick_controller import create_film_simulation_quick_window


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QQuickStyle.setStyle("Fusion")
_APP = QGuiApplication.instance() or QGuiApplication([])
pytestmark = pytest.mark.qt


class FilmSimulationBandInputTests(unittest.TestCase):
    @staticmethod
    def pump():
        QCoreApplication.processEvents()
        QTest.qWait(12)
        QCoreApplication.processEvents()

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        root = self._tempdir.name
        self.engine, self.window, self.controller = create_film_simulation_quick_window(
            root=root,
            geometry_settings=QSettings(
                os.path.join(root, "geometry.ini"), QSettings.Format.IniFormat),
        )
        self.window.show()
        self.pump()

    def tearDown(self):
        self.controller.close()
        self.window.close()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.pump()
        self._tempdir.cleanup()

    def add_second_band(self):
        button = self.find_item("filmSimulationAddBandButton")
        self.assertIsNotNone(button)
        self.click(button)
        self.pump()
        minimum = self.find_item("filmSimulationBandMinimumField_1")
        maximum = self.find_item("filmSimulationBandMaximumField_1")
        self.assertIsNotNone(minimum)
        self.assertIsNotNone(maximum)
        return minimum, maximum

    def find_item(self, object_name):
        pending = list(self.window.contentItem().childItems())
        while pending:
            item = pending.pop()
            if item.objectName() == object_name:
                return item
            pending.extend(item.childItems())
        return None

    def click(self, item):
        target_window = qml_item_window(item)
        self.assertIs(target_window, self.window)
        center = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(
            target_window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            QPoint(round(center.x()), round(center.y())),
        )
        self.pump()

    def enter_text(self, text):
        QTest.keyClick(self.window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        keys = {
            "0": Qt.Key.Key_0, "1": Qt.Key.Key_1, "2": Qt.Key.Key_2,
            "3": Qt.Key.Key_3, "4": Qt.Key.Key_4, "5": Qt.Key.Key_5,
            "6": Qt.Key.Key_6, "7": Qt.Key.Key_7, "8": Qt.Key.Key_8,
            "9": Qt.Key.Key_9, ".": Qt.Key.Key_Period,
        }
        for character in text:
            QTest.keyClick(self.window, keys[character])
        self.pump()

    def clear_text(self):
        QTest.keyClick(self.window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(self.window, Qt.Key.Key_Backspace)
        self.pump()

    def active_name(self):
        item = self.window.activeFocusItem()
        return "" if item is None else item.objectName()

    def test_lower_then_upper_tab_focus_and_unrelated_refresh_keep_draft(self):
        minimum, _maximum = self.add_second_band()
        self.click(minimum)
        self.enter_text("500")
        QTest.keyClick(self.window, Qt.Key.Key_Tab)
        self.pump()
        self.assertEqual(self.active_name(), "filmSimulationBandMaximumField_1")
        self.assertEqual(
            self.find_item("filmSimulationBandMinimumField_1").property("text"),
            "500",
        )

        QTest.keyClick(self.window, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
        self.pump()
        self.assertEqual(self.active_name(), "filmSimulationBandMinimumField_1")
        self.enter_text("1200")

        # Updating the other statistics row must not replace this active editor
        # or overwrite its in-progress value.
        self.assertTrue(self.controller.setCalculationBand(0, 410.0, 690.0))
        self.pump()
        self.assertEqual(self.active_name(), "filmSimulationBandMinimumField_1")
        draft = self.find_item("filmSimulationBandMinimumField_1")
        self.assertEqual(draft.property("text"), "1200")

        QTest.keyClick(self.window, Qt.Key.Key_Tab)
        self.pump()
        self.assertEqual(self.active_name(), "filmSimulationBandMaximumField_1")
        self.assertEqual(self.controller.calculationItems[1]["minimum_nm"], 1200.0)
        self.assertEqual(self.controller.calculationItems[1]["maximum_nm"], 700.0)

    def test_upper_first_keeps_temporarily_invalid_range_until_corrected(self):
        # Provide a small computed result so the existing range validator is
        # observable while exercising the actual QML editor and keyboard path.
        self.controller._result = {
            "wavelength_nm": [100.0, 1200.0],
            "R": [0.1, 0.2],
            "T": [0.9, 0.8],
        }
        self.controller._stale = False
        self.controller._invalidate_calculation_items()
        self.controller.calculationItemsChanged.emit()
        self.pump()

        _minimum, maximum = self.add_second_band()
        self.click(maximum)
        self.enter_text("300")
        QTest.keyClick(self.window, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
        self.pump()

        self.assertEqual(self.active_name(), "filmSimulationBandMinimumField_1")
        item = self.controller.calculationItems[1]
        self.assertEqual((item["minimum_nm"], item["maximum_nm"]), (400.0, 300.0))
        self.assertEqual(item["status"], "invalid_range")
        self.assertEqual(
            self.find_item("filmSimulationBandMinimumField_1").property("text"),
            "400",
        )
        self.assertEqual(
            self.find_item("filmSimulationBandMaximumField_1").property("text"),
            "300",
        )

        self.enter_text("250")
        QTest.keyClick(self.window, Qt.Key.Key_Tab)
        self.pump()
        corrected = self.controller.calculationItems[1]
        self.assertEqual((corrected["minimum_nm"], corrected["maximum_nm"]), (250.0, 300.0))
        self.assertEqual(corrected["status"], "ok")
        self.assertEqual(self.active_name(), "filmSimulationBandMaximumField_1")

    def test_editing_only_upper_preserves_an_incomplete_lower_bound(self):
        self.controller._result = {
            "wavelength_nm": [100.0, 1200.0],
            "R": [0.1, 0.2],
            "T": [0.9, 0.8],
        }
        self.controller._stale = False
        self.controller._invalidate_calculation_items()
        self.controller.calculationItemsChanged.emit()
        self.pump()

        minimum, _maximum = self.add_second_band()
        self.click(minimum)
        self.clear_text()
        self.assertEqual(
            self.find_item("filmSimulationBandMinimumField_1").property("text"), "")

        QTest.keyClick(self.window, Qt.Key.Key_Tab)
        self.pump()
        self.assertEqual(self.active_name(), "filmSimulationBandMaximumField_1")
        self.assertEqual(self.controller.calculationItems[1]["minimum_nm"], 400.0)
        self.assertEqual(self.find_item("filmSimulationBandMinimumField_1").property("text"), "")
        self.enter_text("1064")
        QTest.keyClick(self.window, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
        self.pump()

        item = self.controller.calculationItems[1]
        self.assertEqual((item["minimum_nm"], item["maximum_nm"]), (400.0, 1064.0))
        self.assertEqual(item["status"], "ok")
        self.assertEqual(self.active_name(), "filmSimulationBandMinimumField_1")
        self.assertEqual(
            self.find_item("filmSimulationBandMinimumField_1").property("text"), "")
        shown = self.find_item("filmSimulationBandMaximumField_1").property("text")
        # ThemeField formats an unfocused integer through Qt.locale(), which
        # may legitimately display a grouping separator (for example 1,064).
        locale = QLocale()
        self.assertEqual(shown, locale.toString(1064.0, "f", 0))
        parsed, ok = locale.toDouble(shown)
        self.assertTrue(ok)
        self.assertEqual(parsed, 1064.0)

        self.enter_text("900")
        QTest.keyClick(self.window, Qt.Key.Key_Tab)
        self.pump()
        corrected = self.controller.calculationItems[1]
        self.assertEqual((corrected["minimum_nm"], corrected["maximum_nm"]), (900.0, 1064.0))
        self.assertEqual(corrected["status"], "ok")
        self.assertEqual(self.active_name(), "filmSimulationBandMaximumField_1")

    def test_partial_numeric_draft_survives_refresh_from_another_band(self):
        minimum, _maximum = self.add_second_band()
        self.click(minimum)
        self.enter_text("0")
        self.pump()
        self.assertEqual(self.find_item("filmSimulationBandMinimumField_1").property("text"), "0")

        self.assertTrue(self.controller.setCalculationBand(0, 410.0, 690.0))
        self.pump()
        self.assertEqual(self.active_name(), "filmSimulationBandMinimumField_1")
        self.assertEqual(self.find_item("filmSimulationBandMinimumField_1").property("text"), "0")
        self.assertEqual((self.controller.calculationItems[1]["minimum_nm"],
                          self.controller.calculationItems[1]["maximum_nm"]), (400.0, 700.0))
