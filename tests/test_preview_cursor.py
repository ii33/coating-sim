"""Real queued preview-cursor ownership, popup DPR, and deleted-delegate checks."""
from __future__ import annotations

import gc
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QUrl, Qt
from PySide6.QtGui import QCursor
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtWidgets import QApplication

from tests.qt_test_guard import NoNetworkAccessManagerFactory, qml_item_window
from coat_sim.ui import preview_cursor


class PreviewCursorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        preview_cursor.register_preview_cursor()

    def setUp(self):
        self.engine = QQmlEngine()
        self.network_factory = NoNetworkAccessManagerFactory()
        self.engine.setNetworkAccessManagerFactory(self.network_factory)
        self.component = QQmlComponent(self.engine)
        self.component.setData(b'''import QtQuick
import QtQuick.Controls
import QtQuick.Window
import CoatUi 1.0
Window {
    id: host; width: 320; height: 240; visible: true
    Item { id: target; objectName: "target"; width: 100; height: 80 }
    PreviewCursor { objectName: "cursor"; target: target; active: false }
    Popup {
        id: popup; objectName: "popup"; popupType: Popup.Window
        width: 180; height: 100; focus: true
        contentItem: Item { objectName: "popupTarget" }
    }
}''', QUrl("file:///qt-test/preview-cursor.qml"))
        self.assertFalse(self.component.isError(), self.component.errors())
        self.window = self.component.create()
        self.assertIsNotNone(self.window, self.component.errors())
        self.target = self.window.findChild(QQuickItem, "target")
        self.cursor = self.window.findChild(preview_cursor.PreviewCursor, "cursor")
        self.app.processEvents()

    def tearDown(self):
        self.cursor.active = False
        self.app.processEvents()
        self.window.close()
        self.window.deleteLater()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        gc.collect()

    def test_active_cursor_uses_surface_dpr_without_reversing_wrapper_ownership(self):
        with patch.object(self.window, "devicePixelRatio", return_value=2.25), \
                patch.object(preview_cursor, "_magnifier", return_value=QCursor(Qt.CrossCursor)) as magnifier:
            self.cursor.active = True
            self.app.processEvents()
            self.assertTrue(self.cursor._owns_cursor)
            magnifier.assert_called_once_with(2.25)
        self.assertTrue(all(value is not self.window for value in gc.get_referents(self.target)))
        self.assertIsNone(self.window.parent())
        self.cursor.active = False
        self.app.processEvents()
        self.cursor.active = True
        self.app.processEvents()
        self.assertTrue(self.cursor._owns_cursor)
        self.assertFalse(self.target.cursor().pixmap().isNull())
        self.assertTrue(all(value is not self.window for value in gc.get_referents(self.target)))

    def test_popup_surface_dpr_and_normal_cursor_are_preserved(self):
        popup = self.window.findChild(QObject, "popup")
        popup.open()
        self.app.processEvents()
        target = self.window.findChild(QQuickItem, "popupTarget")
        surface = qml_item_window(target)
        self.assertIsNot(surface, self.window)
        self.cursor.target = target
        self.cursor.normalShape = Qt.PointingHandCursor.value
        with patch.object(surface, "devicePixelRatio", return_value=3.0), \
                patch.object(preview_cursor, "_magnifier", return_value=QCursor(Qt.CrossCursor)) as magnifier:
            self.cursor.active = True
            self.app.processEvents()
            magnifier.assert_called_once_with(3.0)
        self.assertTrue(all(value is not surface for value in gc.get_referents(target)))
        self.cursor.active = False
        self.app.processEvents()
        self.assertFalse(self.cursor._owns_cursor)
        self.assertEqual(target.cursor().shape(), Qt.PointingHandCursor)

    def test_deleted_target_before_queued_apply_is_released(self):
        self.cursor.active = True
        self.target.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.assertIsNone(self.cursor.target)
        self.assertFalse(self.cursor._owns_cursor)

