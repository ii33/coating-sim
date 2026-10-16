"""Real Qt geometry regressions for the film designer's responsive workspace."""

import os
from pathlib import Path
import tempfile
import unittest
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from tests import qt_test_guard
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QSettings, QUrl, Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
from shiboken6 import isValid

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.fonts import bundled_font_family
from coat_sim.ui.theme_service import ThemeService


ROOT = Path(__file__).parents[1]
_application = QGuiApplication.instance() or QGuiApplication([])
if not isinstance(_application, QGuiApplication):
    raise RuntimeError("Film geometry tests require a QGuiApplication")
APP: QGuiApplication = _application


class FilmDesignLayoutGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp.name)
        material_service = FilmDesignService(ROOT, restrict_to_root=False)
        self.service = FilmDesignService(self.temp_path, restrict_to_root=False)
        self.service.materials = material_service.materials
        self.controller = FilmDesignQuickController(
            self.temp_path, service=self.service,
            geometry_settings=QSettings(str(self.temp_path / "geometry.ini"),
                                        QSettings.Format.IniFormat))
        self.engine = QQmlApplicationEngine()
        self.network_factory = qt_test_guard.NoNetworkAccessManagerFactory()
        self.engine.setNetworkAccessManagerFactory(self.network_factory)
        self.warnings: list[str] = []
        self.engine.warnings.connect(
            lambda warnings: self.warnings.extend(warning.toString() for warning in warnings))
        self.theme = ThemeService.instance()
        self.original_font = APP.font()
        self.original_theme = self.theme.current_snapshot
        self.original_mode = self.theme.current_preference_mode
        self.theme.configure(mode="light", font_scale=1, density_scale=1)
        self.theme.attach_engine(self.engine, "Theme")
        family = bundled_font_family()
        if family:
            APP.setFont(QFont(family))
        self.component = QQmlComponent(
            self.engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/FilmDesignQuick.qml")))
        window = self.component.createWithInitialProperties({"controller": self.controller})
        if not isinstance(window, QQuickWindow):
            self.fail("FilmDesignQuick did not create a QQuickWindow: "
                      + "\n".join(error.toString() for error in self.component.errors()))
        self.window: QQuickWindow = window
        self.window.setWidth(1100)
        self.window.setHeight(760)
        self.window.show()
        self.wait_events()
        self.assertTrue(self.controller.initializationReady)
        while self.controller.expression_symbols.rowCount() < 4:
            self.assertTrue(self.controller.addSymbol())
        self.wait_events()

    def tearDown(self) -> None:
        self.theme.configure(mode=self.original_mode,
                             font_scale=float(self.original_theme["fontScale"]),
                             density_scale=float(self.original_theme["densityScale"]))
        APP.setFont(self.original_font)
        if isValid(self.window):
            self.window.setProperty("allowClose", True)
            self.window.close()
            self.window.deleteLater()
        self.controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        self.temp.cleanup()

    @staticmethod
    def wait_events() -> None:
        QCoreApplication.processEvents()
        QTest.qWait(80)
        QCoreApplication.processEvents()

    def child_item(self, parent: QObject, name: str) -> QQuickItem:
        item = parent.findChild(QQuickItem, name)
        if not isinstance(item, QQuickItem):
            self.fail(f"Missing QQuickItem: {name}")
        return item

    def item(self, name: str) -> QQuickItem:
        return self.child_item(self.window, name)

    def evaluate(self, scope: QObject, source: str) -> Any:
        # QQmlExpression returns QVariant-backed values across the QML boundary.
        expression = QQmlExpression(self.engine.rootContext(), scope, source)
        value = expression.evaluate()
        self.assertFalse(expression.hasError(), expression.error().toString())
        if isinstance(value, tuple):
            value = value[0]
        return value

    def row_item(self, view: QQuickItem, index: int) -> QQuickItem:
        row = self.evaluate(view, f"itemAtIndex({index})")
        if not isinstance(row, QQuickItem):
            self.fail(f"Missing row {index} in {view.objectName()}")
        return row

    def assert_horizontal_containment(self, child: QQuickItem, parent: QQuickItem) -> None:
        position = child.mapToItem(parent, QPointF(0, 0))
        self.assertGreater(child.width(), 0, child.objectName())
        self.assertGreaterEqual(position.x(), -1, child.objectName())
        self.assertLessEqual(position.x() + child.width(), parent.width() + 1,
                             child.objectName())

    def assert_form_and_expression_fit(self) -> None:
        scroll = self.item("filmDesignSubstratePageScroll")
        for name in ("filmDesignReferenceWavelength", "filmDesignUseK",
                     "filmDesignSubstrateMaterial", "filmDesignSubstrateDataset",
                     "filmDesignSubstrateThickness", "filmDesignSubstrateThicknessUnit",
                     "filmDesignSubstrateSides",
                     "filmDesignMediumMaterial_incident_medium",
                     "filmDesignMediumDataset_incident_medium",
                     "filmDesignMediumMaterial_exit_medium",
                     "filmDesignMediumDataset_exit_medium"):
            field = self.item(name)
            self.assert_horizontal_containment(field, scroll)
            self.assertGreaterEqual(field.height() + 1,
                                    float(field.property("implicitHeight")), name)
        panel = self.item("filmDesignExpressionPanel")
        for name in ("filmDesignExpressionArea", "filmDesignSymbolHeader",
                     "filmDesignSymbolColumns", "filmDesignSymbolList",
                     "filmDesignAddSymbol", "filmDesignApplyExpression"):
            self.assert_horizontal_containment(self.item(name), panel)
        symbol_list = self.item("filmDesignSymbolList")
        for name in ("filmDesignSymbolHeaderIndex", "filmDesignSymbolHeaderMaterial",
                     "filmDesignSymbolHeaderDataset", "filmDesignSymbolHeaderQw",
                     "filmDesignSymbolHeaderNm", "filmDesignSymbolHeaderInsert",
                     "filmDesignSymbolHeaderDelete"):
            header = self.item(name)
            self.assert_horizontal_containment(header, symbol_list)
            self.assertGreaterEqual(header.height() + 1,
                                    float(header.property("implicitHeight")), name)

    def test_resize_expand_and_page_switch_keep_fields_inside_their_viewports(self) -> None:
        tabs = self.item("filmDesignPageTabs")
        tabs.setProperty("currentIndex", 2)
        self.evaluate(self.window, "openSidebarSection('expression')")
        self.wait_events()
        panel = self.item("filmDesignExpressionPanel")
        substrate = self.item("filmDesignSubstratePageScroll")
        for width in (1100, 960, 720, 1400, 1996, 1100):
            with self.subTest(width=width):
                self.window.setWidth(width)
                self.wait_events()
                grid = self.item("filmDesignWorkspaceGrid")
                design = self.item("filmDesignDesignColumn")
                self.assertAlmostEqual(grid.width(), self.window.width() - 24, delta=1)
                if self.window.property("sidebarUsesBottomLayout"):
                    self.assertAlmostEqual(design.width(), grid.width(), delta=1)
                self.assert_form_and_expression_fit()
                for _ in range(2):
                    self.evaluate(self.window, "toggleSidebar(); toggleSidebar()")
                    self.wait_events()
                tabs.setProperty("currentIndex", 0)
                tabs.setProperty("currentIndex", 2)
                self.wait_events()
                self.assertEqual(self.item("filmDesignExpressionPanel"), panel)
                self.assertEqual(self.item("filmDesignSubstratePageScroll"), substrate)
                self.assert_form_and_expression_fit()
        self.assertFalse([warning for warning in self.warnings
                          if "recursive rearrange" in warning.lower()], self.warnings)

    def test_scaled_and_dark_forms_fit_and_four_symbol_rows_have_real_height(self) -> None:
        self.item("filmDesignPageTabs").setProperty("currentIndex", 2)
        self.evaluate(self.window, "openSidebarSection('expression')")
        for mode, font_scale, density_scale in (("light", 1, 1), ("dark", 1.25, 1.25)):
            with self.subTest(mode=mode):
                self.theme.configure(mode=mode, font_scale=font_scale,
                                     density_scale=density_scale)
                self.wait_events()
                self.assert_form_and_expression_fit()
                symbol_list = self.item("filmDesignSymbolList")
                last_row = self.row_item(symbol_list, 3)
                self.assertLessEqual(last_row.y() + last_row.height(),
                                     symbol_list.height() + 1)
                for name in ("filmDesignSubstrateDataset", "filmDesignSubstrateSides",
                             "filmDesignMediumDataset_incident_medium"):
                    background = self.item(name + "_background")
                    self.assertEqual(background.property("color").name(),
                                     str(self.evaluate(background, "Theme.snapshot.colorSurface")).lower())

    def test_first_keyboard_expand_in_narrow_window_preserves_loaded_forms(self) -> None:
        self.window.setWidth(960)
        self.window.setProperty("sidebarExpanded", False)
        self.item("filmDesignPageTabs").setProperty("currentIndex", 2)
        self.wait_events()
        substrate = self.item("filmDesignSubstratePageScroll")
        symbols = self.controller.expression_symbols.ids()
        for _ in range(3):
            QTest.keyClick(self.window, Qt.Key.Key_F4)
            self.wait_events()
            self.assertTrue(self.window.property("sidebarExpanded"))
            self.assertTrue(self.window.property("sidebarUsesBottomLayout"))
            self.assert_form_and_expression_fit()
            QTest.keyClick(self.window, Qt.Key.Key_F4)
            self.wait_events()
            self.assertFalse(self.window.property("sidebarExpanded"))
        self.assertEqual(self.item("filmDesignSubstratePageScroll"), substrate)
        self.assertEqual(self.controller.expression_symbols.ids(), symbols)

    def test_layer_and_symbol_same_column_keyboard_navigation_survives_resize(self) -> None:
        for _ in range(3):
            self.assertTrue(self.controller.addLayer("front"))
        self.window.setWidth(720)
        self.evaluate(self.window, "openSidebarSection('expression')")
        self.wait_events()
        self.window.setWidth(1100)
        self.wait_events()
        for view_name in ("filmDesignFrontView", "filmDesignSymbolList"):
            view = self.item(view_name)
            rows = [self.row_item(view, index) for index in range(3)]
            self.assertTrue(all(rows))
            for prefix in ("filmLayerQw_", "filmLayerNm_"):
                fields = [self.child_item(row, prefix + str(row.property("stableId")))
                          for row in rows]
                self.assertTrue(all(fields))
                fields[0].forceActiveFocus()
                for key, modifier, expected in (
                        (Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier, 1),
                        (Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, 2),
                        (Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier, 1),
                        (Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier, 0),
                        (Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier, 0)):
                    QTest.keyClick(self.window, key, modifier)
                    self.wait_events()
                    self.assertEqual(self.window.activeFocusItem(), fields[expected])
                    self.assertEqual(fields[expected].property("selectedText"),
                                     fields[expected].property("text"))


if __name__ == "__main__":
    unittest.main()
