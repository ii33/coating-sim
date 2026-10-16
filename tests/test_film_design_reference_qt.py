"""Real Qt Quick flow for explicit selection, mapping, preview, and replacement."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import QPointF, QCoreApplication, QEvent, Qt, QUrl
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.theme_service import ThemeService


ROOT = Path(__file__).parents[1]
ARTIFACTS = ROOT / "artifacts" / "film_design_generator_validation"
REFERENCE_ARTIFACTS = ROOT / "artifacts" / "film_design_reference_library_validation"
APP = QGuiApplication.instance() or QGuiApplication([])
for _font in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\segoeui.ttf"):
    _font_id = QFontDatabase.addApplicationFont(_font)
    if _font_id >= 0:
        APP.setFont(QFont(QFontDatabase.applicationFontFamilies(_font_id)[0], 10))


class FilmDesignReferenceQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        REFERENCE_ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=REFERENCE_ARTIFACTS)
        cls.temp_path = Path(cls.temp.name)
        source = FilmDesignService(ROOT, restrict_to_root=False)
        cls.service = FilmDesignService(cls.temp_path, restrict_to_root=False)
        cls.service.materials = source.materials

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.settings_path = self.temp_path / "reference-geometry.ini"
        from PySide6.QtCore import QSettings
        self.controller = FilmDesignQuickController(
            self.temp_path, service=self.service,
            geometry_settings=QSettings(str(self.settings_path), QSettings.Format.IniFormat))
        self.engine = QQmlApplicationEngine()
        ThemeService.instance().set_theme("light")
        ThemeService.instance().attach_engine(self.engine, "Theme")
        self.component = __import__("PySide6.QtQml", fromlist=["QQmlComponent"]).QQmlComponent(
            self.engine,
            QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/FilmDesignQuick.qml")))
        self.root = self.component.createWithInitialProperties({"controller": self.controller})
        if self.root is None:
            errors = "\n".join(error.toString() for error in self.component.errors())
            raise AssertionError("FilmDesignQuick failed to load: " + errors)
        self.root.setWidth(1100)
        self.root.setHeight(760)
        self.root.show()
        self.wait_events(100)
        self.assertTrue(self.controller.initializationReady)

    def tearDown(self):
        if self.root is not None:
            self.root.close()
            self.root.deleteLater()
        self.controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        APP.processEvents()
        self.engine.deleteLater()
        APP.processEvents()

    @staticmethod
    def wait_events(ms=50):
        QCoreApplication.processEvents()
        QTest.qWait(ms)
        QCoreApplication.processEvents()

    def item(self, name):
        result = self.root.findChild(QQuickItem, name)
        if result is None:
            # Repeater delegates live in the visual child tree, not always in
            # QQmlApplicationEngine's QObject ownership tree.
            stack = [self.root.contentItem()]
            while stack and result is None:
                parent = stack.pop()
                for child in parent.childItems():
                    if child.objectName() == name:
                        result = child
                        break
                    stack.append(child)
        self.assertIsNotNone(result, f"Missing QML item {name}")
        return result

    def click(self, item):
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(self.root, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, point)
        self.wait_events()

    def qml_value(self, scope, source):
        expression = QQmlExpression(self.engine.rootContext(), scope, source)
        value = expression.evaluate()
        if expression.hasError():
            raise AssertionError(expression.error().toString())
        if isinstance(value, tuple) and value:
            value = value[0]
        if hasattr(value, "toVariant"):
            value = value.toVariant()
        return value

    def library_flickable(self):
        scroll = self.item("filmDesignLibraryScroll")
        candidates = [scroll, *scroll.findChildren(__import__("PySide6.QtCore", fromlist=["QObject"]).QObject)]
        matches = [candidate for candidate in candidates
                   if candidate.metaObject().className() == "QQuickFlickable"]
        self.assertTrue(matches, "library ScrollView did not expose a Flickable")
        return matches[0]

    def reveal(self, item):
        flickable = self.library_flickable()
        content = flickable.property("contentItem")
        self.assertIsInstance(content, QQuickItem)
        point = item.mapToItem(content, QPointF(0, 0))
        max_y = max(0.0, content.height() - flickable.height())
        target = max(0.0, min(max_y, point.y() + item.height() - flickable.height() + 8))
        flickable.setProperty("contentY", target)
        self.wait_events(80)
        rect = item.mapToItem(flickable, QPointF(0, 0))
        self.assertGreaterEqual(rect.y(), -1.0,
                                f"{item.objectName()} is above library viewport: {rect.y()}")
        self.assertLessEqual(rect.y() + item.height(), flickable.height() + 1.0,
                             f"{item.objectName()} is below library viewport: {rect.y()}")
        self.assertGreaterEqual(rect.x(), -1.0,
                                f"{item.objectName()} is left of library viewport: {rect.x()}")
        self.assertLessEqual(rect.x() + item.width(), flickable.width() - 14.0 + 1.0,
                             f"{item.objectName()} is right of library viewport: "
                             f"{rect.x() + item.width()} > {flickable.width()}")
        return flickable

    def item_rect(self, item):
        point = item.mapToItem(self.root.contentItem(), QPointF(0, 0))
        return {
            "x": round(point.x(), 3), "y": round(point.y(), 3),
            "width": round(item.width(), 3), "height": round(item.height(), 3),
            "right": round(point.x() + item.width(), 3),
            "bottom": round(point.y() + item.height(), 3),
        }

    def assert_workspace_geometry(self):
        design = self.item_rect(self.item("filmDesignDesignColumn"))
        sidebar = self.item_rect(self.item("filmDesignExpressionSidebar"))
        width, height = float(self.root.width()), float(self.root.height())
        self.assertGreaterEqual(design["x"], -1.0)
        self.assertLessEqual(design["right"], width + 1.0)
        self.assertGreaterEqual(sidebar["x"], -1.0)
        self.assertGreaterEqual(sidebar["y"], -1.0)
        self.assertLessEqual(sidebar["right"], width + 1.0)
        self.assertLessEqual(sidebar["bottom"], height + 1.0)
        if width >= 720:
            self.assertLessEqual(design["right"], sidebar["x"] + 1.0,
                                 f"side-by-side overlap: design={design}, sidebar={sidebar}")
        else:
            self.assertLessEqual(design["bottom"], sidebar["y"] + 1.0,
                                 f"bottom-layout overlap: design={design}, sidebar={sidebar}")
        return {"width": width, "height": height, "design": design, "sidebar": sidebar}

    def save_narrow_geometry(self, width, filename):
        self.root.setWidth(width)
        self.wait_events(150)
        geometry = self.assert_workspace_geometry()
        flickable = self.library_flickable()
        flickable.setProperty("contentY", 0.0)
        self.wait_events()
        save_button = self.item("filmDesignSaveToLibrary")
        self.reveal(save_button)
        recipe_combo = self.item("filmReferenceRecipePicker")
        self.reveal(recipe_combo)
        viewport_point = recipe_combo.mapToItem(flickable, QPointF(0, 0))
        geometry["library_viewport"] = {
            "width": round(float(flickable.width()), 3),
            "height": round(float(flickable.height()), 3),
            "recipe_combo": {
                "x": round(viewport_point.x(), 3),
                "y": round(viewport_point.y(), 3),
                "width": round(float(recipe_combo.width()), 3),
                "height": round(float(recipe_combo.height()), 3),
            },
            "save_button": self.item_rect(save_button),
            "scroll_y": round(float(flickable.property("contentY")), 3),
        }
        self.assertGreaterEqual(float(flickable.width()), 1.0)
        image = self.root.grabWindow()
        self.assertFalse(image.isNull())
        image.save(str(REFERENCE_ARTIFACTS / filename), "PNG")
        return geometry

    def activate_value(self, combo, value):
        index = int(self.qml_value(combo, f'indexOfValue("{value}")'))
        self.assertGreaterEqual(index, 0, f"{value} not available in {combo.objectName()}")
        self.reveal(combo)
        self.click(combo)
        QTest.keyClick(self.root, Qt.Key.Key_Home)
        for _ in range(index):
            QTest.keyClick(self.root, Qt.Key.Key_Down)
        QTest.keyClick(self.root, Qt.Key.Key_Enter)
        self.wait_events(80)
        self.assertEqual(str(combo.property("currentValue")), value,
                         f"Failed selecting {value} in {combo.objectName()}")

    def test_real_tfcalc_mapping_keeps_nm_roles_and_requires_two_confirmations(self):
        before = deepcopy(self.controller.currentDesign())
        before_revision = self.controller.session.snapshot().design_revision
        self.click(self.item("filmDesignSidebarLibrary"))
        recipe_combo = self.item("filmReferenceRecipePicker")
        self.activate_value(recipe_combo, "tfcalc_growing_ar_13")
        panel = self.item("filmDesignReferencePanel")
        selected = panel.property("selectedRecipe")
        if hasattr(selected, "toVariant"):
            selected = selected.toVariant()
        self.assertEqual(len(selected["roles"]), 2)
        material_h = self.item("filmReferenceMaterial_H")
        self.assertTrue(material_h.isVisible())
        self.assertTrue(self.item("filmReferenceMaterial_L").isVisible())

        source_flickable = self.library_flickable()
        source_flickable.setProperty("contentY", 0.0)
        self.wait_events()
        source_first = self.item("filmReferenceSourceLayer_1")
        self.reveal(source_first)
        raw_image = self.root.grabWindow()
        self.assertFalse(raw_image.isNull())
        raw_image.save(str(REFERENCE_ARTIFACTS / "reference-library-tfcalc-source-1100x760.png"), "PNG")
        source_last = self.item("filmReferenceSourceLayer_13")
        self.reveal(source_last)
        last_source_image = self.root.grabWindow()
        self.assertFalse(last_source_image.isNull())
        last_source_image.save(str(REFERENCE_ARTIFACTS / "reference-library-tfcalc-source-last-layer-1100x760.png"), "PNG")

        self.activate_value(self.item("filmReferenceMaterial_H"), "builtin_research_Ge_film")
        self.activate_value(self.item("filmReferenceDataset_H"), "ri_ge_amotchkina_film")
        self.activate_value(self.item("filmReferenceMaterial_L"), "builtin_zns_literature_20261004")
        self.activate_value(self.item("filmReferenceDataset_L"), "ri_zns_amotchkina_n")
        self.activate_value(self.item("filmReferenceIncidentMaterial"), "builtin_ideal_air")
        self.activate_value(self.item("filmReferenceIncidentDataset"), "ideal_air_n1_k0")
        self.activate_value(self.item("filmReferenceSubstrateMaterial"), "builtin_research_Ge_substrate")
        self.activate_value(self.item("filmReferenceSubstrateDataset"), "ri_ge_burnett2016_table1_vacuum")
        assume_unknown = self.item("filmReferenceAssumeUnknownK")
        self.reveal(assume_unknown)
        self.click(assume_unknown)
        confirm_mapping = self.item("filmReferenceMappingConfirmed")
        self.reveal(confirm_mapping)
        self.assertTrue(confirm_mapping.property("enabled"))
        self.click(confirm_mapping)

        preview_button = self.item("filmReferencePreview")
        self.reveal(preview_button)
        self.click(preview_button)
        data = panel.property("previewData")
        if hasattr(data, "toVariant"):
            data = data.toVariant()
        self.assertTrue(data and data.get("preview_token"), panel.property("errorText"))
        self.assertEqual(data["recipe_id"], "tfcalc_growing_ar_13")
        self.assertEqual(data["layer_count"], 13)
        self.assertEqual(data["lambda0_nm"], 10000.0)
        expected = [1076.97, 629.03, 457.49, 131.55, 1234.92, 1142.59,
                    2590.54, 81.75, 1827.50, 84.60, 563.34, 285.97, 157.14]
        self.assertEqual([row["fixed_thickness_nm"] for row in data["mapped_layers_incident_order"]], expected)
        self.assertAlmostEqual(data["total_physical_thickness_nm"], 10263.39)
        ge_layers = [row for row in data["mapped_layers_incident_order"] if row["role"] == "H"]
        self.assertTrue(ge_layers and all(row["k_at_wavelength"] > 1e-9 for row in ge_layers))
        self.assertTrue(all(abs(row["delta_k"] - row["k_at_wavelength"]) < 1e-15
                            for row in ge_layers))
        self.assertTrue(any("保留该 k" in text for text in data["assumptions"]))
        self.assertTrue(any(abs(row["delta_n"]) > 1e-3 for row in ge_layers))
        self.assertNotIn("candidate_design", data)
        self.assertNotIn("reflectance", data)
        self.assertEqual(self.controller.currentDesign(), before)
        self.assertEqual(self.controller.session.snapshot().design_revision, before_revision)

        heading = self.item("filmReferencePreviewHeading")
        self.reveal(heading)
        first_mapped = self.item("filmReferenceMappedLayer_1")
        self.reveal(first_mapped)
        self.assertTrue(first_mapped.isVisible())
        first_text = str(first_mapped.property("text"))
        self.assertNotIn("undefined", first_text.lower())
        self.assertNotIn("nan", first_text.lower())
        self.assertIn("未知", first_text)
        image = self.root.grabWindow()
        self.assertFalse(image.isNull())
        image.save(str(REFERENCE_ARTIFACTS / "reference-library-tfcalc-preview-1100x760.png"), "PNG")
        last_mapped = self.item("filmReferenceMappedLayer_13")
        self.reveal(last_mapped)
        self.assertTrue(last_mapped.isVisible())
        last_text = str(last_mapped.property("text"))
        self.assertNotIn("undefined", last_text.lower())
        self.assertNotIn("nan", last_text.lower())
        last_image = self.root.grabWindow()
        self.assertFalse(last_image.isNull())
        last_image.save(str(REFERENCE_ARTIFACTS / "reference-library-tfcalc-preview-last-layer-1100x760.png"), "PNG")

        apply_button = self.item("filmReferenceApplyPreview")
        self.reveal(apply_button)
        self.click(apply_button)
        dialog = self.root.findChild(__import__("PySide6.QtCore", fromlist=["QObject"]).QObject,
                                     "filmReferenceReplaceConfirmation")
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.property("visible"))
        dialog_image = self.root.grabWindow()
        self.assertFalse(dialog_image.isNull())
        dialog_image.save(str(REFERENCE_ARTIFACTS / "reference-library-replace-confirm-1100x760.png"), "PNG")
        cancel_button = self.qml_value(dialog, "standardButton(4194304)")
        self.assertIsInstance(cancel_button, QQuickItem)
        self.click(cancel_button)
        self.assertFalse(dialog.property("visible"))
        self.assertEqual(self.controller.currentDesign(), before)
        self.assertEqual(self.controller.session.snapshot().design_revision, before_revision)
        self.assertIsNone(self.controller._generator_preview)
        self.assertFalse(panel.property("previewData"))

        self.reveal(preview_button)
        self.click(preview_button)
        self.assertTrue(panel.property("previewData"))
        self.reveal(apply_button)
        self.click(apply_button)
        yes_button = self.qml_value(dialog, "standardButton(16384)")
        self.assertIsInstance(yes_button, QQuickItem)
        self.click(yes_button)
        self.wait_events()
        applied = self.controller.currentDesign()
        self.assertEqual(len(applied["front_layers"]), 13)
        self.assertEqual([row["material_id"] for row in applied["front_layers"]],
                         ["builtin_zns_literature_20261004" if role == "L" else "builtin_research_Ge_film"
                          for role in reversed([row["role"] for row in data["mapped_layers_incident_order"]])])
        self.assertTrue(applied["name"].startswith("参考配方映射"))
        self.assertTrue(self.controller.dirty)
        self.assertTrue(self.controller.canUndoGeneratedDesign)
        self.assertFalse(self.controller.applyGeneratedDesign(data["preview_token"]))

        # A live preview is discarded on library exit; the library form and
        # its scroll/focus state survive returning to the same sidebar page.
        self.reveal(preview_button)
        self.click(preview_button)
        self.assertTrue(panel.property("previewData"))
        role_h_before = self.qml_value(panel, "roleBinding('H').material_id")
        scroll_before_switch = float(source_flickable.property("contentY"))
        recipe_combo.forceActiveFocus()
        self.wait_events()
        self.click(self.item("filmDesignSidebarGenerator"))
        self.assertFalse(panel.property("previewData"))
        self.assertIsNone(self.controller._generator_preview)
        self.assertEqual(str(panel.property("selectedRecipeId")), "tfcalc_growing_ar_13")
        self.click(self.item("filmDesignSidebarLibrary"))
        self.wait_events(100)
        self.assertEqual(str(panel.property("selectedRecipeId")), "tfcalc_growing_ar_13")
        self.assertEqual(str(self.qml_value(panel, "roleBinding('H').material_id")), str(role_h_before))
        self.assertAlmostEqual(float(source_flickable.property("contentY")), scroll_before_switch, delta=1.0)
        self.assertTrue(recipe_combo.property("activeFocus"))

        # F4 closes and restores the current input focus without changing the
        # scroll position or the left design column's usable geometry.
        recipe_combo.forceActiveFocus()
        self.wait_events()
        content_y_before_f4 = float(source_flickable.property("contentY"))
        QTest.keyClick(self.root, Qt.Key.Key_F4)
        self.wait_events(100)
        self.assertFalse(bool(self.qml_value(self.root, "sidebarExpanded")))
        QTest.keyClick(self.root, Qt.Key.Key_F4)
        self.wait_events(150)
        self.assertTrue(bool(self.qml_value(self.root, "sidebarExpanded")))
        self.assertTrue(recipe_combo.property("activeFocus"))
        self.assertAlmostEqual(float(source_flickable.property("contentY")), content_y_before_f4, delta=1.0)
        normal_geometry = self.assert_workspace_geometry()

        # Selecting another public recipe resets role mappings and any token.
        self.reveal(preview_button)
        self.click(preview_button)
        self.assertTrue(panel.property("previewData"))
        self.activate_value(recipe_combo, "comsol_qq_mgf2_cef3")
        self.assertEqual(str(panel.property("selectedRecipeId")), "comsol_qq_mgf2_cef3")
        self.assertFalse(panel.property("previewData"))
        self.assertIsNone(self.controller._generator_preview)
        role_bindings = self.qml_value(panel, "roleBindings")
        self.assertTrue(all(not value.get("material_id") and not value.get("optical_set_id")
                            for value in role_bindings.values()))

        narrow_640 = self.save_narrow_geometry(640, "reference-library-640x760.png")
        narrow_320 = self.save_narrow_geometry(320, "reference-library-320x760.png")
        geometry = {
            "normal_1100x760": normal_geometry,
            "narrow_640x760": narrow_640,
            "minimum_320x760": narrow_320,
            "checks": [
                "1100: design column and expanded sidebar are side by side",
                "640/320: design column stays above sidebar; both remain inside window",
                "visible library controls remain within viewport horizontal bounds",
                "F4 preserves library input focus and scroll position",
                "feature switch clears preview while preserving recipe/role inputs",
            ],
        }
        (REFERENCE_ARTIFACTS / "reference-qt-geometry.json").write_text(
            json.dumps(geometry, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
