"""Offline help and visible author credits match the project workflow."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from pathlib import Path
import unittest
from typing import ClassVar, cast
import pytest


from PySide6.QtCore import QCoreApplication, QEvent, QObject, QUrl
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import shiboken6

from coat_sim.ui.about import AboutBridge

pytestmark = pytest.mark.qt
ROOT = Path(__file__).resolve().parents[1]


class AboutCurrentWorkflowTests(unittest.TestCase):
    app: ClassVar[QApplication]
    @classmethod
    def setUpClass(cls):
        cls.app = cast(QApplication, QApplication.instance() or QApplication([]))

    def test_help_anchors_exist_and_unknown_fragment_is_rejected(self):
        bridge = AboutBridge()
        try:
            manual = (ROOT / "docs/software_manual.html").read_text(encoding="utf-8")
            for section in ("projects", "compute_devices", "film_design", "workflow",
                            "algorithm_selection", "film_optimization", "film_optimization_parameters"):
                self.assertIn(f'id="{section}"', manual)
                url = bridge.manualSectionUrl(section)
                self.assertEqual(url.fragment(), section)
                self.assertTrue(Path(url.toLocalFile()).is_file())
            self.assertEqual(bridge.manualSectionUrl("../../other").fragment(), "")
            self.assertIn("CPU", manual)
            self.assertIn(".coatproj", manual)
        finally:
            bridge.deleteLater()
            QCoreApplication.sendPostedEvents(bridge, QEvent.Type.DeferredDelete)

    def test_visible_credit_and_license_match_repository(self):
        engine = QQmlApplicationEngine()
        bridge = AboutBridge(engine)
        engine.rootContext().setContextProperty("bridge", bridge)
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/AboutWindow.qml")))
        window = None
        try:
            self.assertFalse(component.isError(), component.errors())
            window = cast(QQuickWindow, component.createWithInitialProperties({"initialMaximized": False}))
            self.assertIsInstance(window, QQuickWindow)
            title = window.findChild(QObject, "aboutProductTitle")
            subtitle = window.findChild(QObject, "aboutProductSubtitle")
            assert title is not None and subtitle is not None
            self.assertEqual(title.property("text"), "光学镀膜")
            self.assertEqual(subtitle.property("text"), "设计与仿真")
            window.show()
            QTest.qWait(50)
            author = window.findChild(QObject, "aboutAuthorCredit")
            build = window.findChild(QObject, "aboutBuildCredit")
            assert author is not None and build is not None
            self.assertEqual(author.property("text"), "Designed by ii33")
            self.assertLess(build.property("font").pixelSize(), author.property("font").pixelSize())
            window.setProperty("page", 4)
            QTest.qWait(30)
            texts = [str(item.property("text")) for item in window.findChildren(QObject)]
            self.assertTrue(any("MIT License" in text and "ii33" in text for text in texts))
            self.assertFalse(any("没有 LICENSE" in text for text in texts))
            self.assertIn("Copyright (c) 2026 ii33", (ROOT / "LICENSE").read_text())
        finally:
            if window is not None and shiboken6.isValid(window):
                window.close()
                window.deleteLater()
                QCoreApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)
            for owned in (component, engine):
                if shiboken6.isValid(owned):
                    owned.deleteLater()
                    QCoreApplication.sendPostedEvents(owned, QEvent.Type.DeferredDelete)

    def test_visible_author_only_changed_not_settings_namespace(self):
        for name in ("AboutWindow.qml", "LauncherWindow.qml"):
            text = (ROOT / "coat_sim/ui/qml" / name).read_text()
            self.assertNotIn("LYX", text)
            self.assertIn("Designed by ii33", text)
        source = (ROOT / "coat_sim/ui/qml/LauncherWindow.qml").read_text()
        self.assertRegex(source, r"font.pointSize: authorCredit.font.pointSize \* 0\.8")
        self.assertIn('QSettings("LYX", "coat-sim")', (ROOT / "coat_sim/ui/recent_projects.py").read_text())
