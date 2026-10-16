"""Offscreen interaction regressions for the film-only Quick page refinements."""

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from shiboken6 import isValid

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.theme_service import ThemeService


ROOT = Path(__file__).parents[1]
_APP = QGuiApplication.instance() or QGuiApplication([])


class FilmQuickPageUiRefinementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.temp_path = Path(cls.temp.name)
        material_service = FilmDesignService(ROOT, restrict_to_root=False)
        cls.service = FilmDesignService(cls.temp_path, restrict_to_root=False)
        cls.service.materials = material_service.materials

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.settings = QSettings(str(self.temp_path / "ui-refinement-geometry.ini"),
                                  QSettings.Format.IniFormat)
        self.controller = FilmDesignQuickController(
            self.temp_path, service=self.service, geometry_settings=self.settings)
        self.engine = QQmlApplicationEngine()
        ThemeService.instance().set_theme("light")
        ThemeService.instance().attach_engine(self.engine, "Theme")
        self.component = QQmlComponent(
            self.engine,
            QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/FilmDesignQuick.qml")))
        self.root = self.component.createWithInitialProperties({"controller": self.controller})
        if self.root is None:
            raise AssertionError("FilmDesignQuick failed to load: " +
                                 "\n".join(error.toString() for error in self.component.errors()))
        self.root.setWidth(1100)
        self.root.setHeight(760)
        self.root.show()
        self.wait_events()
        if not self.controller.initializationReady:
            raise AssertionError("Design controller did not finish initialization")

    def tearDown(self):
        if self.root is not None and isValid(self.root):
            self.root.close()
            self.root.deleteLater()
        if isValid(self.controller):
            self.controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _APP.processEvents()
        self.engine.deleteLater()
        _APP.processEvents()

    @staticmethod
    def wait_events(ms=60):
        QCoreApplication.processEvents()
        QTest.qWait(ms)
        QCoreApplication.processEvents()

    def item(self, name):
        result = self.root.findChild(QQuickItem, name)
        self.assertIsNotNone(result, f"Missing QML item {name}")
        return result

    def qml_eval(self, scope, code):
        expression = QQmlExpression(self.engine.rootContext(), scope, code)
        result = expression.evaluate()
        if expression.hasError():
            raise AssertionError(expression.error().toString())
        if isinstance(result, tuple) and result:
            result = result[0]
        if hasattr(result, "toVariant"):
            result = result.toVariant()
        QCoreApplication.processEvents()
        return result

    def generator_flickable(self):
        scroll = self.item("filmDesignGeneratorScroll")
        candidates = [scroll, *scroll.findChildren(QQuickItem)]
        flickables = [candidate for candidate in candidates
                      if candidate.metaObject().className() == "QQuickFlickable"]
        self.assertTrue(flickables, "Generator ScrollView has no Flickable")
        return flickables[0]

    def test_rapid_sidebar_switches_preserve_models_focus_commits_and_scroll(self):
        summary = str(self.root.property("statusDetail"))
        self.assertIn(self.controller.thicknessSummaryText, summary)
        self.assertIn(self.controller.thicknessSummaryDescription, summary)
        self.assertIn("膜厚合计", summary)
        self.assertRegex(summary, r"\d+\.\d{3} nm")
        self.assertNotIn("草稿", str(self.controller.thicknessSummaryText))
        self.assertNotIn("草稿", str(self.controller.thicknessSummaryDescription))
        if self.controller.currentDesign()["substrate"]["sides"] == "single":
            self.assertIn("背面设计", str(self.controller.thicknessSummaryText))
            self.assertIn("背面设计会保留但不参与计算", str(self.controller.thicknessSummaryDescription))
        self.assertIn("膜厚合计", str(self.root.property("statusText")))

        toggle = self.item("filmDesignSidebarToggle")
        self.assertLessEqual(float(toggle.width()), 24.5)
        self.assertAlmostEqual(float(self.item("filmDesignSidebarRail").width()), 64.0, delta=1.0)

        self.qml_eval(self.root, "openSidebarSection('generator')")
        panel_before = self.item("filmDesignGeneratorPanel")
        applicability = self.item("filmDesignGeneratorApplicability")
        for template in self.controller.builtinDesignTemplates:
            template_id = str(template["stable_id"])
            self.qml_eval(panel_before, f"changeTemplate('{template_id}')")
            self.assertIn(str(template["applicability"]), applicability.property("text"))
            self.assertIn("不保证达标", applicability.property("text"))
        flickable = self.generator_flickable()
        scrollbar = self.item("filmDesignGeneratorVerticalScrollBar")
        self.assertTrue(scrollbar.isVisible())
        self.assertGreater(float(scrollbar.width()), 0.0)
        scroll_max = max(0.0, float(flickable.property("contentHeight"))
                         - float(flickable.property("height")))
        self.assertGreater(scroll_max, 20.0, "Generator form should be vertically scrollable")
        flickable.setProperty("contentY", scroll_max)
        field = self.item("filmDesignGeneratorWavelength")
        field.forceActiveFocus()
        self.wait_events()
        saved_y = float(flickable.property("contentY"))

        # Call the same section switch handler repeatedly within one event turn.
        # Only the last deferred focus restoration should win.
        self.qml_eval(self.root,
                      "openSidebarSection('library');"
                      "openSidebarSection('expression');"
                      "openSidebarSection('generator');")
        self.wait_events(90)
        self.assertEqual(self.root.property("activeSidebarSection"), "generator")
        self.assertTrue(isValid(panel_before), "Switching sections destroyed the generator panel")
        self.assertEqual(float(flickable.property("contentY")), saved_y)
        focused = self.root.activeFocusItem()
        self.assertIsNotNone(focused)
        self.assertEqual(focused.objectName(), "filmDesignGeneratorWavelength")

        # Leaving the expression editor commits the pending draft before the page
        # is hidden; returning restores its field focus and its panel instance.
        self.qml_eval(self.root, "openSidebarSection('expression')")
        expression_panel_before = self.item("filmDesignExpressionPanel")
        expression_area = self.item("filmDesignExpressionArea")
        expression_area.forceActiveFocus()
        expression_area.setProperty("text", "(H L)^2")
        QCoreApplication.processEvents()
        self.assertTrue(bool(expression_panel_before.property("expressionDraftPending")))
        self.qml_eval(self.root, "openSidebarSection('generator')")
        self.wait_events()
        self.assertEqual(self.controller.expressionText, "(H L)^2")
        self.assertFalse(bool(expression_panel_before.property("expressionDraftPending")))
        self.qml_eval(self.root, "openSidebarSection('expression')")
        self.wait_events()
        self.assertTrue(isValid(expression_panel_before))
        self.assertEqual(self.item("filmDesignExpressionPanel"), expression_panel_before)
        focused = self.root.activeFocusItem()
        self.assertIsNotNone(focused)
        self.assertEqual(focused.objectName(), "filmDesignExpressionArea")

        # A rapid collapse/expand sequence does not recreate controls or reset
        # the generator's scroll position.
        self.qml_eval(self.root,
                      "toggleSidebar(); toggleSidebar();")
        self.wait_events(90)
        self.assertTrue(self.root.property("sidebarExpanded"))
        self.qml_eval(self.root, "openSidebarSection('generator')")
        self.wait_events()
        self.assertEqual(float(flickable.property("contentY")), saved_y)
        self.assertTrue(isValid(panel_before))

        # At 720 px, keep the layer editor readable and move the feature panel
        # below it instead of squeezing both columns side by side.
        self.root.setWidth(720)
        self.wait_events(90)
        self.assertTrue(self.root.property("sidebarUsesBottomLayout"))
        design_column = self.item("filmDesignDesignColumn")
        feature_sidebar = self.item("filmDesignExpressionSidebar")
        self.assertGreaterEqual(
            float(feature_sidebar.y()),
            float(design_column.y() + design_column.height()) - 1.0)
        self.assertLessEqual(
            float(feature_sidebar.y() + feature_sidebar.height()),
            float(self.root.height()) + 1.0)
        self.assertLessEqual(float(design_column.height()), 210.0)


if __name__ == "__main__":
    unittest.main()
