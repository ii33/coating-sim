"""Offscreen end-to-end tests for the generator, notes, and shared sidebar."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QPointF, QObject, QSettings, Qt, QUrl, QCoreApplication, QEvent
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QSignalSpy, QTest

from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.theme_service import ThemeService
from tests.test_film_synthesis_gui_review import find_named_visual_item


ROOT = Path(__file__).parents[1]
ARTIFACTS = ROOT / "artifacts" / "film_design_generator_validation"
_APP = QGuiApplication.instance() or QGuiApplication([])
_APP_FONT_ID = QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\msyh.ttc")
if _APP_FONT_ID >= 0:
    _APP.setFont(QFont(QFontDatabase.applicationFontFamilies(_APP_FONT_ID)[0], 10))


class FilmDesignGeneratorQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory()
        cls.temp_path = Path(cls.temp.name)
        # Keep the material library real while directing user-design storage and
        # settings into this test's temporary directory.
        source_service = FilmDesignService(ROOT, restrict_to_root=False)
        cls.service = FilmDesignService(cls.temp_path, restrict_to_root=False)
        cls.service.materials = source_service.materials

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.settings = QSettings(str(self.temp_path / "generator-geometry.ini"),
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
                                 "\n".join(err.toString() for err in self.component.errors()))
        self.root.setWidth(1100)
        self.root.setHeight(760)
        self.root.show()
        self.wait_events()
        self.assertTrue(self.controller.initializationReady)

    def tearDown(self):
        if self.root is not None:
            self.root.close()
            self.root.deleteLater()
        self.controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _APP.processEvents()
        self.engine.deleteLater()
        _APP.processEvents()

    @staticmethod
    def wait_events(ms=40):
        QCoreApplication.processEvents()
        QTest.qWait(ms)
        QCoreApplication.processEvents()

    def item(self, name):
        result = self.root.findChild(QQuickItem, name)
        self.assertIsNotNone(result, f"Missing QML item {name}")
        return result

    def visual_item(self, name):
        def visit(parent):
            if parent.objectName() == name:
                return parent
            for child in parent.childItems():
                found = visit(child)
                if found is not None:
                    return found
            return None
        assert isinstance(self.root, QQuickWindow)
        result = visit(self.root.contentItem())
        self.assertIsNotNone(result, f"Missing visual QML item {name}")
        return result

    def click(self, item):
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(self.root, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, point)
        self.wait_events()

    def reveal_generator_item(self, item):
        # The preview action lives near the end of a long form. Scroll it into
        # the actual viewport before sending a mouse event to its QML button.
        scroll_item = self.item("filmDesignGeneratorScroll")
        flickables = [candidate for candidate in
                      [scroll_item, *scroll_item.findChildren(QObject)]
                      if candidate.metaObject().className() == "QQuickFlickable"]
        self.assertTrue(flickables, "ScrollView did not expose its actual Flickable")
        flickable = flickables[0]
        content_item = flickable.property("contentItem")
        self.assertIsInstance(content_item, QQuickItem)
        point = item.mapToItem(content_item, QPointF(0, 0))
        target_y = max(0.0, min(content_item.height() - flickable.height(),
                                point.y() + item.height() - flickable.height()))
        self.assertTrue(flickable.setProperty("contentY", target_y))
        self.wait_events(100)
        rect = self.geometry("filmDesignGeneratorPreview")
        scroll = self.geometry("filmDesignGeneratorScroll")
        self.assertGreaterEqual(rect["y"], scroll["y"] - 1,
                                f"preview={rect}, scroll={scroll}, contentY={flickable.property('contentY')!r}")
        self.assertLessEqual(rect["y"] + rect["height"], scroll["y"] + scroll["height"] + 1,
                             f"preview={rect}, scroll={scroll}, contentY={flickable.property('contentY')!r}")

    def generator_flickable(self):
        scroll_item = self.item("filmDesignGeneratorScroll")
        candidates = [scroll_item, *scroll_item.findChildren(QObject)]
        flickables = [candidate for candidate in candidates
                      if candidate.metaObject().className() == "QQuickFlickable"]
        self.assertTrue(flickables, "Generator ScrollView did not expose its Flickable")
        return flickables[0]

    def reveal_generator_control(self, item):
        """Scroll a specific generator control into view before clicking it."""
        flickable = self.generator_flickable()
        content_item = flickable.property("contentItem")
        self.assertIsInstance(content_item, QQuickItem)
        point = item.mapToItem(content_item, QPointF(0, 0))
        target_y = max(0.0, min(
            float(flickable.property("contentHeight")) - float(flickable.height()),
            point.y() + item.height() - flickable.height(),
        ))
        self.assertTrue(flickable.setProperty("contentY", target_y))
        self.wait_events(100)
        point = item.mapToItem(content_item, QPointF(0, 0))
        content_y = float(flickable.property("contentY"))
        self.assertGreaterEqual(point.y(), content_y - 1.0,
                                f"{item.objectName()} is above the generator viewport")
        self.assertLessEqual(point.y() + item.height(),
                             content_y + float(flickable.height()) + 1.0,
                             f"{item.objectName()} is below the generator viewport")

    def add_three_empty_layers_through_qml(self):
        """Reproduce the user's initial three blank rows using actual buttons."""
        self.assertEqual(self.controller.front_layers.rowCount(), 0)
        self.click(self.item("filmDesignEmptyAddFront"))
        first_id = self.controller.front_layers.ids()[0]
        self.wait_events()
        self.assertIsNotNone(self.visual_item("filmLayerMaterial_" + first_id))
        for _ in range(2):
            insert = self.visual_item("filmDesignInsertAfter_" + first_id)
            self.click(insert)
            self.wait_events()
            self.assertEqual(self.controller.front_layers.rowCount(), _ + 2)
        ids = self.controller.front_layers.ids()
        self.assertEqual(len(ids), 3)
        for stable_id in ids:
            picker = self.visual_item("filmLayerMaterial_" + stable_id)
            self.assertEqual(str(picker.property("currentValue")), "")
        return ids

    def amotchkina_film_binding(self, material_name):
        material = next(row for row in self.controller.film_options
                        if row.get("name") == material_name)
        optical = next(row for row in material["optical_sets"]
                       if "Amotchkina" in str(row.get("name", ""))
                       and "仅n" in str(row.get("name", "")))
        return material, optical

    def configure_zns_ybf3_roles(self, panel):
        zns, zns_n = self.amotchkina_film_binding("ZnS")
        ybf3, ybf3_n = self.amotchkina_film_binding("YbF3")
        high, low = (zns, zns_n), (ybf3, ybf3_n)
        self.qml_eval(panel, "changeTemplate('hr_hl_qw')")
        self.qml_eval(panel, f"setRoleMaterial('H', {json.dumps(high[0]['id'])})")
        self.qml_eval(panel, f"setRoleOpticalSet('H', {json.dumps(high[1]['id'])})")
        self.qml_eval(panel, f"setRoleMaterial('L', {json.dumps(low[0]['id'])})")
        self.qml_eval(panel, f"setRoleOpticalSet('L', {json.dumps(low[1]['id'])})")
        self.qml_eval(panel, "setEndpointMaterial('incident', 'medium', 'builtin_ideal_air')")
        self.qml_eval(panel, "setEndpointOpticalSet('incident', 'ideal_air_n1_k0')")
        self.qml_eval(panel, "setEndpointMaterial('substrate', 'substrate', 'film_substrate_schott_nbk7')")
        self.qml_eval(panel, "setEndpointOpticalSet('substrate', 'schott_nbk7_sellmeier')")
        panel.setProperty("assumeUnknownKLossless", True)
        panel.setProperty("wavelengthText", "550")
        panel.setProperty("pairsText", "8")
        self.wait_events()
        return {high[0]["id"]: high[0]["name"], low[0]["id"]: low[0]["name"]}, {
            high[0]["id"]: high[1]["id"], low[0]["id"]: low[1]["id"]
        }

    def apply_generator_preview_by_click(self, panel):
        preview_button = self.item("filmDesignGeneratorPreview")
        self.reveal_generator_control(preview_button)
        self.click(preview_button)
        preview = panel.property("previewData")
        if hasattr(preview, "toVariant"):
            preview = preview.toVariant()
        self.assertTrue(preview.get("ok"), panel.property("errorText"))
        self.assertEqual(preview["template"]["stable_id"], "hr_hl_qw")
        self.assertEqual(preview["layer_count"], 16)

        apply_button = self.item("filmDesignGeneratorApply")
        self.reveal_generator_control(apply_button)
        self.click(apply_button)
        confirmation = self.root.findChild(QObject, "filmDesignGeneratorApplyConfirmation")
        assert confirmation is not None
        self.assertTrue(confirmation.property("visible"))
        generator_preview = self.controller._generator_preview
        assert generator_preview is not None
        candidate = deepcopy(generator_preview["candidate_design"])
        # Accept only after the real Apply click has opened the visible modal.
        self.qml_eval(confirmation, "accept()")
        self.wait_events(100)
        self.assertEqual(self.controller.currentDesign()["front_layers"],
                         candidate["front_layers"])
        return preview

    def assert_generated_qml_rows_match_controller(self, material_names, dataset_ids):
        view = self.item("filmDesignFrontView")
        design_rows = self.controller.currentDesign()["front_layers"]
        self.assertEqual(len(design_rows), 16)
        self.assertEqual(view.property("count"), 16)
        self.assertEqual({row["material_id"] for row in design_rows}, set(material_names))
        self.assertTrue(all(row["optical_set_id"] in dataset_ids.values()
                            for row in design_rows))

        # Visiting every row in both directions forces the production ListView
        # to recycle its delegates, matching the path that exposed the stale picker.
        for index in list(range(16)) + list(reversed(range(16))):
            self.qml_eval(view, f"positionViewAtIndex({index}, 0); forceLayout()")
            self.wait_events(35)
            row_item = self.qml_eval(view, f"itemAtIndex({index})")
            self.assertIsNotNone(row_item, f"Missing row delegate at index {index}")
            stable_id = str(row_item.property("stableId"))
            expected = design_rows[index]
            self.assertEqual(stable_id, expected["id"])
            self.assertEqual(str(row_item.property("materialId")), expected["material_id"])
            self.assertEqual(str(row_item.property("opticalSetId")), expected["optical_set_id"])

            picker = self.visual_item("filmLayerMaterial_" + stable_id)
            expected_name = material_names[expected["material_id"]]
            self.assertEqual(str(picker.property("currentValue")), expected["material_id"],
                             f"picker ID mismatch at layer {index + 1}")
            self.assertEqual(str(picker.property("currentText")), expected_name,
                             f"picker label mismatch at layer {index + 1}")
            input_field = self.visual_item("filmLayerMaterial_" + stable_id + "_input")
            self.assertEqual(str(input_field.property("text")), expected_name,
                             f"picker input text mismatch at layer {index + 1}")

            dataset = self.visual_item("filmLayerOpticalSet_" + stable_id)
            expected_set = expected["optical_set_id"]
            self.assertEqual(str(dataset.property("currentValue")), expected_set,
                             f"dataset ID mismatch at layer {index + 1}")
            expected_dataset_name = next(
                row["name"] for row in self.controller.opticalSetsForFilm(expected["material_id"])
                if row["id"] == expected_set)
            self.assertEqual(str(dataset.property("currentText")), expected_dataset_name,
                             f"dataset label mismatch at layer {index + 1}")

            model_row = self.controller.front_layers.record_for_id(stable_id)
            assert model_row is not None
            self.assertEqual(str(row_item.property("physicalNmText")),
                             model_row["physicalNmText"])
            self.assertEqual(str(row_item.property("qwText")), model_row["qwText"])
            physical_field = self.visual_item("filmLayerNm_" + stable_id)
            qw_field = self.visual_item("filmLayerQw_" + stable_id)
            self.assertAlmostEqual(float(physical_field.property("text")),
                                   float(model_row["physicalNmText"]), places=3)
            self.assertAlmostEqual(float(qw_field.property("text")),
                                   float(model_row["qwText"]), places=3)

    def click_generator_undo(self, panel):
        undo_button = find_named_visual_item(panel, "filmDesignGeneratorUndoButton")
        self.assertTrue(undo_button.isVisible())
        self.click(undo_button)
        self.wait_events(100)

    def test_generated_material_picker_matches_after_blank_rows_and_delegate_reuse(self):
        original_ids = self.add_three_empty_layers_through_qml()
        panel = self.open_generator()
        material_names, dataset_ids = self.configure_zns_ybf3_roles(panel)

        preview = self.apply_generator_preview_by_click(panel)
        first_design = deepcopy(self.controller.currentDesign())
        first_ids = [row["id"] for row in first_design["front_layers"]]
        self.assertEqual(len(first_ids), 16)
        self.assertTrue(set(original_ids).isdisjoint(first_ids))
        self.assertEqual({row["material_id"] for row in first_design["front_layers"]},
                         set(material_names))
        self.assertEqual({row["optical_set_id"] for row in first_design["front_layers"]},
                         set(dataset_ids.values()))
        self.assertEqual(len(preview["layers_incident_order"]), 16)

        # Iterate down and back up: recycled ListView delegates must display
        # their bound model IDs and dataset names, not a prior row's selection.
        self.assert_generated_qml_rows_match_controller(material_names, dataset_ids)

        # The one-level undo from the first replacement must restore the exact
        # three blank rows that were created through the real QML controls.
        self.click_generator_undo(panel)
        self.assertEqual(self.controller.front_layers.ids(), original_ids)
        restored = self.controller.currentDesign()["front_layers"]
        self.assertEqual(len(restored), 3)
        self.assertTrue(all(not row["material_id"] and not row["optical_set_id"]
                            for row in restored))
        for stable_id in original_ids:
            model_row = self.controller.front_layers.record_for_id(stable_id)
            assert model_row is not None
            self.assertEqual(model_row["qwText"], "n不可用")
        view = self.item("filmDesignFrontView")
        for index, stable_id in enumerate(original_ids):
            self.qml_eval(view, f"positionViewAtIndex({index}, 0); forceLayout()")
            self.wait_events(35)
            delegate = self.qml_eval(view, f"itemAtIndex({index})")
            self.assertIsNotNone(delegate)
            self.assertEqual(str(delegate.property("stableId")), stable_id)
            picker = self.visual_item("filmLayerMaterial_" + stable_id)
            self.assertEqual(str(picker.property("currentValue")), "")
            self.assertEqual(str(picker.property("currentText")), "请选择材料")
            self.assertEqual(str(self.visual_item("filmLayerMaterial_" + stable_id + "_input")
                                 .property("text")), "请选择材料")
            dataset = self.visual_item("filmLayerOpticalSet_" + stable_id)
            self.assertEqual(str(dataset.property("currentValue")), "")
            model_row = self.controller.front_layers.record_for_id(stable_id)
            assert model_row is not None
            self.assertEqual(str(delegate.property("physicalNmText")),
                             model_row["physicalNmText"])
            self.assertEqual(str(delegate.property("qwText")), "n不可用")
            self.assertAlmostEqual(
                float(self.visual_item("filmLayerNm_" + stable_id).property("text")),
                float(model_row["physicalNmText"]), places=3)

    def test_repeated_generator_apply_undo_restores_previous_stack_once(self):
        self.add_three_empty_layers_through_qml()
        panel = self.open_generator()
        first_names, first_sets = self.configure_zns_ybf3_roles(panel)
        self.apply_generator_preview_by_click(panel)
        first_design = deepcopy(self.controller.currentDesign())
        first_ids = [row["id"] for row in first_design["front_layers"]]
        self.assert_generated_qml_rows_match_controller(first_names, first_sets)

        # Apply a different real H/L catalog pair so a reused picker cannot pass
        # by retaining the previous selected value at the same model index.
        self.configure_real_material_roles(panel)
        panel.setProperty("pairsText", "8")
        self.wait_events()
        second_names = {
            row["id"]: row["name"] for row in self.controller.film_options
            if row["id"] in ("builtin_research_TiO2_film", "builtin_research_Al2O3_film")
        }
        second_sets = {
            "builtin_research_TiO2_film": "ri_tio2_jolivet_amorphous",
            "builtin_research_Al2O3_film": "ri_al2o3_zhukovsky",
        }
        self.apply_generator_preview_by_click(panel)
        second_design = deepcopy(self.controller.currentDesign())
        second_ids = [row["id"] for row in second_design["front_layers"]]
        self.assertEqual(len(second_ids), 16)
        self.assertTrue(set(first_ids).isdisjoint(second_ids))
        self.assert_generated_qml_rows_match_controller(second_names, second_sets)

        self.click_generator_undo(panel)
        self.assertEqual(self.controller.currentDesign(), first_design)
        self.assertEqual(self.controller.front_layers.ids(), first_ids)
        self.assertFalse(self.controller.canUndoGeneratedDesign)
        self.assertFalse(self.controller.undoGeneratedDesign())

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

    def geometry(self, name):
        item = self.item(name)
        point = item.mapToItem(self.root.contentItem(), QPointF(0, 0))
        return {"x": point.x(), "y": point.y(), "width": item.width(), "height": item.height()}

    def screenshot(self, name):
        path = ARTIFACTS / f"{name}.png"
        image = self.root.grabWindow()
        self.assertFalse(image.isNull(), f"Could not grab {name} window")
        self.assertTrue(image.save(str(path)), f"Could not save {path}")
        return str(path)

    def open_generator(self):
        self.click(self.item("filmDesignSidebarGenerator"))
        self.assertEqual(self.root.property("activeSidebarSection"), "generator")
        return self.item("filmDesignGeneratorPanel")

    def configure_real_material_roles(self, panel, *, fp=False):
        if fp:
            self.qml_eval(panel, "changeTemplate('fp_halfwave_defect')")
            self.qml_eval(panel, "setRoleMaterial('C', 'builtin_research_TiO2_film')")
            self.qml_eval(panel, "setRoleOpticalSet('C', 'ri_tio2_jolivet_amorphous')")
        else:
            self.qml_eval(panel, "changeTemplate('hr_hl_qw')")
        self.qml_eval(panel, "setRoleMaterial('H', 'builtin_research_TiO2_film')")
        self.qml_eval(panel, "setRoleOpticalSet('H', 'ri_tio2_jolivet_amorphous')")
        self.qml_eval(panel, "setRoleMaterial('L', 'builtin_research_Al2O3_film')")
        self.qml_eval(panel, "setRoleOpticalSet('L', 'ri_al2o3_zhukovsky')")
        self.qml_eval(panel, "setEndpointMaterial('incident', 'medium', 'builtin_ideal_air')")
        self.qml_eval(panel, "setEndpointOpticalSet('incident', 'ideal_air_n1_k0')")
        self.qml_eval(panel, "setEndpointMaterial('substrate', 'substrate', 'film_substrate_schott_nbk7')")
        self.qml_eval(panel, "setEndpointOpticalSet('substrate', 'schott_nbk7_sellmeier')")
        panel.setProperty("assumeUnknownKLossless", True)
        panel.setProperty("wavelengthText", "550")
        panel.setProperty("pairsText", "4")
        self.wait_events()

    def assert_side_by_side(self):
        design = self.geometry("filmDesignDesignColumn")
        sidebar = self.geometry("filmDesignExpressionSidebar")
        self.assertLessEqual(design["x"] + design["width"], sidebar["x"] + 1.0)
        for name in ("filmDesignPageTabs", "filmDesignLayerHeader", "filmDesignFrontView"):
            rect = self.geometry(name)
            self.assertLessEqual(rect["x"] + rect["width"],
                                 design["x"] + design["width"] + 1.0,
                                 f"{name}={rect}, design={design}")
        flow = self.geometry("filmDesignHeaderFlow")
        flow_item = self.item("filmDesignHeaderFlow")
        for child in flow_item.childItems():
            if not child.isVisible():
                continue
            self.assertGreaterEqual(child.x(), -1.0, child.objectName())
            self.assertLessEqual(child.x() + child.width(), flow["width"] + 1.0,
                                 child.objectName())
            self.assertLessEqual(child.y() + child.height(), flow["height"] + 1.0,
                                 child.objectName())
        return design, sidebar

    def test_real_qml_previews_confirm_apply_note_cancel_and_sidebar_geometry(self):
        self.assertTrue(self.root.property("sidebarExpanded"))
        panel = self.open_generator()
        self.assertGreaterEqual(self.item("filmDesignGeneratorTemplate").property("currentIndex"), 0)
        self.configure_real_material_roles(panel)

        # The button path exercises QML JS object -> QVariantMap -> controller
        # -> session -> selected real catalog data at 550 nm.
        initial_design = deepcopy(self.controller.currentDesign())
        self.reveal_generator_item(self.item("filmDesignGeneratorPreview"))
        self.click(self.item("filmDesignGeneratorPreview"))
        preview = panel.property("previewData")
        if hasattr(preview, "toVariant"):
            preview = preview.toVariant()
        self.assertTrue(preview.get("ok"), panel.property("errorText"))
        self.assertEqual(preview["template"]["stable_id"], "hr_hl_qw")
        self.assertEqual(preview["layer_count"], 8)
        self.assertEqual(self.controller.currentDesign(), initial_design)
        self.assertTrue(panel.property("previewToken"))
        self.assertIn("TiO2", " ".join(str(row.get("material_name")) for row in preview["layers_incident_order"]))
        self.assertLessEqual(preview["total_physical_thickness_nm"], float("inf"))
        form_scroll = self.generator_flickable()
        form_scroll.setProperty("contentY", 0.0)
        self.wait_events()
        parameters_image = self.screenshot("film-design-generator-parameters-1100x760")
        form_scroll.setProperty(
            "contentY", max(0.0, float(form_scroll.property("contentHeight")) - form_scroll.height()))
        self.wait_events()
        preview_image = self.screenshot("film-design-generator-preview-1100x760")

        # Note edits remain private until accepted. Escape cancels with the
        # original document and dirty state intact; accept commits one field.
        original_note = self.controller.currentDesign().get("note", "")
        was_dirty = self.controller.dirty
        note_revision = self.controller.session.snapshot().design_revision
        self.click(self.item("filmDesignNoteButton"))
        note_dialog = self.root.findChild(QObject, "filmDesignNoteEditDialog")
        note_field = self.item("filmDesignNoteDraft")
        note_field.setProperty("text", "discard this note")
        self.wait_events()
        QTest.keyClick(self.root, Qt.Key.Key_Escape)
        self.wait_events()
        self.assertFalse(note_dialog.property("visible"))
        self.assertEqual(self.controller.currentDesign().get("note", ""), original_note)
        self.assertEqual(self.controller.dirty, was_dirty)
        self.assertEqual(self.controller.session.snapshot().design_revision, note_revision)
        self.click(self.item("filmDesignNoteButton"))
        note_dialog = self.root.findChild(QObject, "filmDesignNoteEditDialog")
        note_field = self.item("filmDesignNoteDraft")
        note_field.setProperty("text", "long note " * 2100)
        self.wait_events()
        self.assertEqual(len(note_field.property("text")), 20000)
        note_image = self.screenshot("film-design-generator-note-edit-1100x760")
        note_dialog.reject()
        self.wait_events()
        self.assertEqual(self.controller.currentDesign().get("note", ""), original_note)
        self.assertEqual(self.controller.dirty, was_dirty)
        self.assertEqual(self.controller.session.snapshot().design_revision, note_revision)
        self.click(self.item("filmDesignNoteButton"))
        note_dialog = self.root.findChild(QObject, "filmDesignNoteEditDialog")
        note_field = self.item("filmDesignNoteDraft")
        note_field.setProperty("text", "confirmed note")
        note_dialog.accept()
        self.wait_events()
        self.assertEqual(self.controller.currentDesign()["note"], "confirmed note")
        self.assertTrue(self.controller.dirty)

        self.generator_flickable().setProperty("contentY", 0.0)
        self.wait_events()
        current_design, current_sidebar = self.assert_side_by_side()
        normal_image = self.screenshot("film-design-generator-normal-1100x760")
        self.assertGreater(current_design["width"], 400)
        self.assertGreater(current_sidebar["width"], 300)

        # F4 folds and unfolds the active feature. The reopened main-column
        # geometry must match its first-open geometry, with the content still
        # constrained beside the sidebar.
        panel_wavelength = self.item("filmDesignGeneratorWavelength")
        panel_wavelength.forceActiveFocus()
        self.wait_events(20)
        QTest.keyClick(self.root, Qt.Key.Key_F4)
        self.wait_events()
        self.assertFalse(self.root.property("sidebarExpanded"))
        self.assertFalse(self.item("filmDesignSidebarStack").isVisible())
        QTest.keyClick(self.root, Qt.Key.Key_F4)
        self.wait_events()
        self.assertTrue(self.root.property("sidebarExpanded"))
        self.assert_side_by_side()
        reopened_design, _ = self.assert_side_by_side()
        self.assertAlmostEqual(reopened_design["width"], current_design["width"], delta=1.0)
        self.assertTrue(panel_wavelength.property("activeFocus"))
        reopen_image = self.screenshot("film-design-generator-reopen-1100x760")

        # Another actual QML button preview exercises the half-wave defect
        # path using the same selected catalog data and fixed integer pairs.
        self.configure_real_material_roles(panel, fp=True)
        self.reveal_generator_item(self.item("filmDesignGeneratorPreview"))
        self.click(self.item("filmDesignGeneratorPreview"))
        fp_preview = panel.property("previewData")
        if hasattr(fp_preview, "toVariant"):
            fp_preview = fp_preview.toVariant()
        self.assertTrue(fp_preview.get("ok"),
                        f"preview={fp_preview!r}; error={panel.property('errorText')!r}; "
                        f"button={self.geometry('filmDesignGeneratorPreview')}; "
                        f"scroll={self.geometry('filmDesignGeneratorScroll')}")
        self.assertEqual(fp_preview["template"]["stable_id"], "fp_halfwave_defect")
        self.assertEqual(fp_preview["layer_count"], 17)
        self.assertEqual(fp_preview["layers_incident_order"][8]["qw_multiplier"], 2)

        # Apply opens an explicit confirmation. Reject leaves the original
        # document intact, and accepting a fresh preview performs one edit.
        before_reject = deepcopy(self.controller.currentDesign())
        self.click(self.item("filmDesignGeneratorApply"))
        apply_dialog = self.root.findChild(QObject, "filmDesignGeneratorApplyConfirmation")
        self.assertIsNotNone(apply_dialog)
        apply_dialog.reject()
        self.wait_events()
        self.assertEqual(self.controller.currentDesign(), before_reject)
        self.configure_real_material_roles(panel, fp=True)
        self.reveal_generator_item(self.item("filmDesignGeneratorPreview"))
        self.click(self.item("filmDesignGeneratorPreview"))
        self.click(self.item("filmDesignGeneratorApply"))
        apply_dialog = self.root.findChild(QObject, "filmDesignGeneratorApplyConfirmation")
        apply_dialog.accept()
        self.wait_events()
        self.assertTrue(self.controller.dirty)
        self.assertEqual(self.controller.currentDesign()["substrate"]["sides"], "single")
        self.assertEqual(len(self.controller.currentDesign()["front_layers"]), 17)
        self.assertIn("已应用到当前设计", str(panel.property("errorText")))
        self.assertIn("尚未保存", str(panel.property("errorText")))
        self.click(self.item("filmDesignSidebarLibrary"))
        self.click(self.item("filmDesignSaveToLibrary"))
        self.assertEqual(len(self.controller.savedDesigns), 1)
        self.assertFalse(self.controller.dirty)
        self.click(self.item("filmDesignSidebarGenerator"))

        # Below 720 px the side feature moves under the layer pane. At 640 px
        # it remains reachable without covering tabs; 320 px is the floor.
        self.root.setWidth(640)
        self.wait_events(80)
        self.assertTrue(self.root.property("sidebarUsesBottomLayout"))
        self.root.setProperty("sidebarExpanded", False)
        self.wait_events()
        self.assertEqual(self.item("filmDesignSidebarRail").property("height"), 64)
        self.click(self.item("filmDesignSidebarGenerator"))
        self.assertTrue(self.root.property("sidebarExpanded"))
        design_narrow = self.geometry("filmDesignDesignColumn")
        sidebar_narrow = self.geometry("filmDesignExpressionSidebar")
        self.assertGreaterEqual(sidebar_narrow["y"], design_narrow["y"] + design_narrow["height"] - 1)
        self.assertLessEqual(design_narrow["height"] + sidebar_narrow["height"], self.root.height() + 2)
        self.assertLessEqual(sidebar_narrow["y"] + sidebar_narrow["height"],
                             self.root.height() + 1)
        for name in ("filmDesignPageTabs", "filmDesignLayerHeader", "filmDesignFrontView"):
            rect = self.geometry(name)
            self.assertLessEqual(rect["x"] + rect["width"],
                                 design_narrow["x"] + design_narrow["width"] + 1.0, name)
        narrow_image = self.screenshot("film-design-generator-narrow-640x760")

        self.root.setWidth(320)
        self.wait_events(80)
        design_min = self.geometry("filmDesignDesignColumn")
        sidebar_min = self.geometry("filmDesignExpressionSidebar")
        self.assertGreaterEqual(design_min["width"], 250)
        self.assertGreaterEqual(sidebar_min["y"], design_min["y"] + design_min["height"] - 1)
        self.assertLessEqual(design_min["x"] + design_min["width"], self.root.width() + 2)
        self.assertLessEqual(sidebar_min["y"] + sidebar_min["height"],
                             self.root.height() + 1)
        min_image = self.screenshot("film-design-generator-minimum-320x760")

        geometry_data = {
            "normal_1100x760": {"design": current_design, "sidebar": current_sidebar,
                                 "image": normal_image},
            "parameters_1100x760": {"image": parameters_image},
            "preview_1100x760": {"image": preview_image},
            "note_edit_1100x760": {"image": note_image},
            "reopen_1100x760": {"design": reopened_design, "image": reopen_image},
            "narrow_640x760": {"design": design_narrow, "sidebar": sidebar_narrow,
                                "image": narrow_image},
            "minimum_320x760": {"design": design_min, "sidebar": sidebar_min,
                                 "image": min_image},
            "status_contract": {"text": self.root.property("statusText"),
                                "severity": self.root.property("statusSeverity"),
                                "top_margin": self.root.property("contentTopMargin")},
        }
        (ARTIFACTS / "generator-qt-geometry.json").write_text(
            json.dumps(geometry_data, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_feature_switch_preserves_private_inputs_and_scroll_without_editing_design(self):
        panel = self.open_generator()
        self.configure_real_material_roles(panel)
        self.reveal_generator_item(self.item("filmDesignGeneratorPreview"))
        self.click(self.item("filmDesignGeneratorPreview"))
        self.assertTrue(panel.property("previewToken"))
        original_design = deepcopy(self.controller.currentDesign())
        original_revision = self.controller.session.snapshot().design_revision
        flickable = self.generator_flickable()
        flickable.setProperty("contentY", 120.0)
        field = self.item("filmDesignGeneratorWavelength")
        field.forceActiveFocus()
        self.wait_events()
        initial_scroll = float(flickable.property("contentY"))
        self.click(self.item("filmDesignSidebarExpression"))
        self.assertFalse(panel.property("previewToken"))
        self.click(self.item("filmDesignSidebarLibrary"))
        self.click(self.item("filmDesignSidebarGenerator"))
        self.assertEqual(panel.property("wavelengthText"), "550")
        self.assertEqual(panel.property("pairsText"), "4")
        binding = panel.property("roleBindings")
        if hasattr(binding, "toVariant"):
            binding = binding.toVariant()
        self.assertEqual(binding["H"]["material_id"], "builtin_research_TiO2_film")
        self.assertEqual(binding["L"]["optical_set_id"], "ri_al2o3_zhukovsky")
        self.assertAlmostEqual(float(flickable.property("contentY")), initial_scroll, delta=1.0)
        self.assertTrue(field.property("activeFocus"))
        self.assertEqual(self.controller.currentDesign(), original_design)
        self.assertEqual(self.controller.session.snapshot().design_revision, original_revision)

    def test_source_close_invalidates_preview_and_recreated_window_keeps_geometry(self):
        panel = self.open_generator()
        self.configure_real_material_roles(panel)
        self.reveal_generator_item(self.item("filmDesignGeneratorPreview"))
        self.click(self.item("filmDesignGeneratorPreview"))
        old_token = str(panel.property("previewToken"))
        self.assertTrue(old_token)
        first_design, first_sidebar = self.assert_side_by_side()
        invalidated = QSignalSpy(self.controller.generatorPreviewInvalidated)

        old_root = self.root
        old_controller = self.controller
        old_root.close()
        self.wait_events(100)
        self.assertTrue(old_controller._source_closed)
        self.assertFalse(old_controller.applyGeneratedDesign(old_token))
        self.assertFalse(old_controller.canUndoGeneratedDesign)
        result = old_controller.generateDesignPreview({})
        self.assertFalse(result.get("ok"))
        self.assertGreaterEqual(invalidated.count(), 2)

        new_controller = FilmDesignQuickController(
            self.temp_path, service=self.service, geometry_settings=self.settings)
        new_root = self.component.createWithInitialProperties({"controller": new_controller})
        self.assertIsNotNone(new_root)
        new_root.setWidth(1100)
        new_root.setHeight(760)
        new_root.show()
        self.root = new_root
        self.controller = new_controller
        self.wait_events(100)
        self.assertTrue(new_controller.initializationReady)
        second_design, second_sidebar = self.assert_side_by_side()
        self.assertAlmostEqual(first_design["width"], second_design["width"], delta=1.0)
        self.assertAlmostEqual(first_sidebar["width"], second_sidebar["width"], delta=1.0)
        self.assertAlmostEqual(first_sidebar["x"], second_sidebar["x"], delta=1.0)
        self.assertEqual(new_controller.currentDesign()["front_layers"], [])
        old_root.deleteLater()
        old_controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _APP.processEvents()


if __name__ == "__main__":
    unittest.main()
