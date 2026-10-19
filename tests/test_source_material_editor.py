"""Source/material designer round trips, transactional drafts and actual QML controls."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/coat-editor-test-cache")
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QUrl
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
from PySide6.QtWidgets import QApplication
from tests.qt_test_guard import NoNetworkAccessManagerFactory

from coat_sim.core.api import CoatService
from coat_sim.core.materials import read_material_profile
from tests.material_test_helpers import MaterialQtCase
from tests.launcher_test_isolation import LauncherTestIsolation
from coat_sim.ui import launcher as launcher_module
from coat_sim.ui.source_substrate import SourceBridge, SubstrateBridge

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


class SourceMaterialEditorTests(MaterialQtCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.isolation = LauncherTestIsolation(self.root, launcher_module).apply()
        self.addCleanup(self.isolation.stop)
        self.service = CoatService(root=self.root, restrict_to_root=False)
        self.bridge = self.make_source(self.service)

    def test_empty_library_add_edit_save_reload_and_units(self):
        library = self.bridge.materialLibrary
        self.assertEqual([row for row in library.entries if not row["builtin"]], [])
        self.assertTrue(library.valid, library.error)
        library.setField("name", "ZnS 自定义")
        library.setField("resistance_reference_rate_nm_s", "25")
        library.setField("electron_beam_reference_rate_nm_s", "15")
        library.setField("resistance_source_exponent", "2.4")
        self.assertTrue(library.save(), library.status)
        self.settle_materials()
        identity = library.currentId
        path = self.root / "data/materials" / (identity + ".json")
        saved = read_material_profile(path)
        self.assertEqual(saved["resistance_model"]["defaults"]["reference_rate_nm_s"], 2.5)
        self.assertEqual(saved["electron_beam_model"]["defaults"]["reference_rate_nm_s"], 1.5)
        library.setField("name", "ZnS edited")
        self.assertTrue(library.save())
        self.settle_materials()
        other = self.make_source(self.service).materialLibrary
        self.assertEqual([row for row in other.entries if not row["builtin"]][0]["name"], "ZnS edited")
        self.assertTrue(other.selectMaterial(identity))
        self.settle_materials()
        self.assertEqual(other.fields["resistance_reference_rate_nm_s"], "25")
        self.assertFalse(other.dirty)

    def test_builtin_compatibility_save_uses_record_envelope_and_keeps_snapshots(self):
        from coat_sim.core.material_business import MaterialLibrary

        library = self.bridge.materialLibrary
        builtin = next(row for row in library.entries if row["builtin"] and row["sourceBindable"])
        self.assertTrue(library.selectMaterial(builtin["key"]))
        self.settle_materials()
        self.assertTrue(library.useMaterial())
        frozen_source = deepcopy(self.bridge.documentData())
        stored = MaterialLibrary(self.root)
        original = stored.read_builtin_original(library.currentId)
        identity = library.currentId
        key = library.currentKey
        override_path = self.root / "data/materials/.overrides" / (identity + ".json")

        library.setField("name", "Edited builtin compatibility save")
        draft = library.draftData()
        with patch.object(MaterialLibrary, "save_builtin_override", side_effect=OSError("disk full")):
            self.assertFalse(library.save())
        self.assertTrue(library.dirty)
        self.assertEqual(library.draftData(), draft)
        self.assertEqual(library.currentKey, key)
        self.assertFalse(override_path.exists())

        self.assertTrue(library.save(), library.status)
        self.settle_materials()
        self.assertFalse(library.dirty)
        self.assertEqual(library.currentId, identity)
        self.assertEqual(library.currentKey, key)
        self.assertEqual(library.draftData(), draft)
        self.assertEqual(read_material_profile(override_path), draft)
        self.assertEqual(stored.read(identity)["record"], draft)
        self.assertEqual(stored.read_builtin_original(identity), original)
        self.assertEqual(self.bridge.documentData(), frozen_source)

        # Failure after an existing override must retain both persisted and
        # editor state, and retry must still go through the same explicit route.
        saved_bytes = override_path.read_bytes()
        library.setField("name", "Retry builtin compatibility save")
        retry_draft = library.draftData()
        with patch.object(MaterialLibrary, "save_builtin_override", side_effect=OSError("disk full")):
            self.assertFalse(library.save())
        self.assertTrue(library.dirty)
        self.assertEqual(library.draftData(), retry_draft)
        self.assertEqual(override_path.read_bytes(), saved_bytes)
        self.assertTrue(library.save(), library.status)
        self.settle_materials()
        self.assertFalse(library.dirty)
        self.assertEqual(stored.read(identity)["record"], retry_draft)
        self.assertEqual(self.bridge.documentData(), frozen_source)

    def test_builtin_async_save_retains_same_record_contract(self):
        from coat_sim.core.material_business import MaterialLibrary

        library = self.bridge.materialLibrary
        builtin = next(row for row in library.entries if row["builtin"])
        self.assertTrue(library.selectMaterial(builtin["key"]))
        self.settle_materials()
        identity = library.currentId
        library.setField("name", "Edited builtin async save")
        draft = library.draftData()
        self.assertTrue(library.saveInBackground(), library.status)
        self.settle_materials()
        self.assertFalse(library.dirty)
        self.assertEqual(library.currentKey, builtin["key"])
        self.assertEqual(library.draftData(), draft)
        self.assertEqual(MaterialLibrary(self.root).read(identity)["record"], draft)

    def test_select_copies_material_and_source_specific_models(self):
        source = self.bridge
        source.setField("device_type", "resistance_boat")
        lib = source.materialLibrary
        lib.setField("name", "User material")
        lib.setField("resistance_source_exponent", "2.5")
        lib.setField("electron_beam_source_exponent", "3.5")
        lib.setField("resistance_reference_rate_nm_s", "30")
        self.assertTrue(lib.save())
        self.settle_materials()
        self.assertTrue(lib.useMaterial())
        before = deepcopy(source.documentData()["material"])
        lib.setField("name", "Later revision")
        self.assertTrue(lib.save())
        self.settle_materials()
        self.assertEqual(source.documentData()["material"], before)
        self.assertEqual(source.documentData()["source_exponent"], 2.5)
        self.assertEqual(source.documentData()["emission_model"]["reference_rate_nm_s"], 3)
        source.setEmissionField("center_weight", "0.7")
        self.assertEqual(lib.draftData()["resistance_model"]["defaults"]["center_weight"], 0)
        source.setEmissionField("kind", "electron_beam")
        self.assertEqual(source.documentData()["source_exponent"], 3.5)
        self.assertEqual(source.materialName, "User material")

    def test_roundtrip_cover_and_beam_scan_and_embedded_material(self):
        source = self.bridge
        source.setField("device_type", "resistance_boat")
        source.materialLibrary.setField("name", "Test")
        source.materialLibrary.useMaterial()
        for key, value in {"boat_mode":"covered", "cover_height_mm":"8", "cover_thickness_mm":"2", "quadrature_order":"13"}.items():
            source.setEmissionField(key,value)
        for key, value in {"kind":"disk", "radius_mm":"4", "offset_x_mm":"1"}.items():
            source.setApertureField(key, value)
        coverpath = self.root / "boat.csrc"
        self.assertTrue(source.saveAsPath(str(coverpath)), source.status)
        restored = self.make_source(self.service, str(coverpath))
        self.assertEqual(restored.documentData(), source.documentData())
        restored.setField("device_type", "electron_beam")
        for key, value in {"scan_pattern":"custom", "dwell_points":"-2, 0, 1\n2, 0, 3", "scan_center_x_mm":"1.2", "scan_center_y_mm":"-0.5", "scan_angle_deg":"30", "reference_rate_nm_s":"12.5", "valid_rate_min_nm_s":"10", "valid_rate_max_nm_s":"100"}.items():
            restored.setEmissionField(key,value)
        path = self.root / "beam.csrc"
        self.assertTrue(restored.saveAsPath(str(path)),restored.status)
        beam = self.make_source(self.service, str(path))
        self.assertEqual(beam.documentData(), restored.documentData())
        self.assertEqual(beam.emissionFields["reference_rate_nm_s"], "12.5")
        self.assertTrue(beam.hasMaterial)
        self.assertEqual(len(beam.documentData()["emission_model"]["dwell_points"]), 2)

    def test_failed_save_and_cancel_do_not_lose_material_draft(self):
        lib = self.bridge.materialLibrary
        lib.setField("name", "Keep this")
        original = lib.fields
        self.assertFalse(lib.newMaterial())
        lib.confirmDiscard(False)
        self.settle_materials()
        self.assertEqual(lib.fields, original)
        with patch("coat_sim.ui.source_materials.materials.save_material", side_effect=OSError("disk full")):
            self.assertFalse(lib.save())
            self.settle_materials()
        self.assertEqual(lib.fields, original)
        self.assertTrue(lib.dirty)
        lib.setField("resistance_source_exponent", "not a number")
        self.assertFalse(lib.valid)
        self.assertFalse(lib.save())
        self.settle_materials()
        self.assertEqual(lib.fields["resistance_source_exponent"], "not a number")

    def test_source_cancel_new_open_and_failed_open_keep_draft(self):
        source = self.bridge
        source.setField("source_exponent", "2")
        original = source.documentData()
        source.newDocument()
        self.assertEqual(source.pendingAction, "new")
        source.confirmDiscard(False)
        self.settle_materials()
        self.assertEqual(source.documentData(), original)
        self.assertFalse(source.openPath(str(self.root / "missing.csrc")))
        source.confirmDiscard(True)
        self.settle_materials()
        self.assertEqual(source.documentData(), original)
        self.assertTrue(source.dirty)
        with patch.object(source.service, "save", side_effect=OSError("disk full")) as failing_save:
            self.assertFalse(source.saveAsPath(str(self.root / "source.csrc")))
            failing_save.assert_called_once()
        self.assertEqual(source.documentData(), original)
        self.assertTrue(source.dirty)
        source.newDocument()
        source.confirmDiscard(True)
        self.settle_materials()
        self.assertFalse(source.dirty)
        self.assertEqual(source.documentData()["source_exponent"], 1)

    def test_source_window_close_also_protects_library_draft(self):
        source = self.bridge
        self.assertTrue(source.requestClose())
        source.materialLibrary.setField("name", "Unsaved library")
        self.assertFalse(source.requestClose())
        self.assertEqual(source.pendingAction, "close")
        source.confirmDiscard(False)
        self.settle_materials()
        self.assertTrue(source.materialLibrary.dirty)
        self.assertEqual(source.materialLibrary.fields["name"], "Unsaved library")

    def test_delete_confirm_cancel_and_embedded_source_remain(self):
        lib = self.bridge.materialLibrary
        lib.setField("name", "Deleteable")
        self.assertTrue(lib.save())
        self.settle_materials()
        self.assertTrue(lib.useMaterial())
        before = deepcopy(self.bridge.documentData())
        self.assertFalse(lib.confirmDelete(False))
        self.settle_materials()
        self.assertEqual(len([row for row in lib.entries if not row["builtin"]]), 1)
        self.assertTrue(lib.confirmDelete(True))
        self.settle_materials()
        self.assertEqual([row for row in lib.entries if not row["builtin"]], [])
        self.assertTrue(lib.dirty)
        self.assertEqual(lib.fields["name"], "Deleteable")
        self.assertEqual(self.bridge.documentData(), before)

    def test_embedded_material_can_be_added_to_empty_library(self):
        source = self.bridge
        source.materialLibrary.setField("name", "Imported snapshot")
        source.materialLibrary.useMaterial()
        data = source.documentData()
        restored = self.make_source(self.service)
        restored.applyDocument(data)
        self.assertEqual([row for row in restored.materialLibrary.entries if not row["builtin"]], [])
        self.assertTrue(restored.editMaterialSnapshot())
        self.settle_materials()
        self.assertTrue(restored.materialLibrary.save())
        self.settle_materials()
        self.assertEqual([row for row in restored.materialLibrary.entries if not row["builtin"]][0]["name"], "Imported snapshot")
        self.assertEqual(restored.documentData()["material"], data["material"])

    def test_preview_rate_weights_and_validity_are_independent_of_dirty(self):
        source = self.bridge
        source.setField("device_type", "electron_beam")
        source.setEmissionField("rate_exponent_per_decade", "1")
        self.assertTrue(source.saveAsPath(str(self.root / "source.csrc")))
        self.assertFalse(source.dirty)
        source.setPreviewRate("100")
        self.assertIn("有效角指数 2", source.previewSummary)
        self.assertFalse(source.dirty)
        self.assertGreater(max(p["weight"] for p in source.samplePoints), min(p["weight"] for p in source.samplePoints))
        source.setPreviewRate("bad")
        self.assertIn("预览不可用", source.previewSummary)
        self.assertTrue(source.valid)
        self.assertFalse(source.dirty)

    def test_legacy_source_preview_keeps_its_configured_exponent(self):
        self.bridge.setField("source_exponent", "4")
        self.assertNotIn("emission_model", self.bridge.documentData())
        self.assertIn("有效角指数 4", self.bridge.previewSummary)

    def test_calibrated_without_reference_rejected_and_ranges_converted(self):
        lib = self.bridge.materialLibrary
        lib.setField("resistance_provenance_status", "calibrated")
        self.assertFalse(lib.valid)
        lib.setField("resistance_provenance_reference", "Lab measurement 2026-10-04")
        self.assertTrue(lib.valid, lib.error)
        lib.setField("resistance_valid_rate_min_nm_s", "5")
        lib.setField("resistance_valid_rate_max_nm_s", "20")
        self.assertEqual(lib.draftData()["resistance_model"]["defaults"]["valid_rate_min_nm_s"], 0.5)
        self.assertIn("用户已声明",lib.completeness)

    def test_qml_loads_and_invokes_real_material_controls_at_minimum_size(self):
        source = self.bridge
        source.setField("device_type", "electron_beam")
        QQuickStyle.setStyle("Fusion")
        previous_font = APP.font()
        cjk_font_path = Path("C:/Windows/Fonts/Noto Sans SC (TrueType).otf")
        cjk_font_id = QFontDatabase.addApplicationFont(str(cjk_font_path)) if cjk_font_path.is_file() else -1
        cjk_families = QFontDatabase.applicationFontFamilies(cjk_font_id) if cjk_font_id >= 0 else []
        if cjk_families:
            APP.setFont(QFont(cjk_families[0]))
        from coat_sim.ui.theme_service import ThemeService
        theme = ThemeService.instance()
        previous_mode = theme.current_preference_mode
        previous_theme = theme.current_theme_id
        theme.configure(mode="light", theme_package_id="")
        engine = QQmlApplicationEngine()
        network_factory = NoNetworkAccessManagerFactory()
        engine.setNetworkAccessManagerFactory(network_factory)
        theme.attach_engine(engine)
        warnings = []
        engine.warnings.connect(lambda messages: warnings.extend(str(message.toString()) for message in messages))
        engine.rootContext().setContextProperty("bridge", source)
        engine.load(QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/SourceWindow.qml")))
        self.assertTrue(engine.rootObjects(), "\n".join(warnings))
        window = engine.rootObjects()[0]
        try:
            def wait_until(predicate, message):
                deadline = time.monotonic() + 10
                while not predicate():
                    APP.processEvents()
                    if time.monotonic() >= deadline:
                        self.fail(message)
                    time.sleep(0.005)
                APP.processEvents()

            window.showNormal(); window.resize(780, 540); APP.processEvents()
            source_loader = window.findChild(QObject, "sourceFormLoader")
            self.assertIsNotNone(source_loader)
            loader_status = QQmlExpression(
                QQmlEngine.contextForObject(source_loader), source_loader,
                "status === Loader.Ready",
            )
            def source_form_ready():
                ready, _ = loader_status.evaluate()
                self.assertFalse(loader_status.hasError(), loader_status.error())
                return bool(ready) and source_loader.property("item") is not None
            wait_until(source_form_ready, "asynchronous source form did not load")
            combo = window.findChild(QObject, "emissionKind")
            self.assertEqual(combo.property("currentIndex"), 2)
            self.assertTrue(QMetaObject.invokeMethod(window, "openMaterialLibrary"))
            wait_until(lambda: window.findChild(QObject, "materialLibraryDialog") is not None,
                       "lazy material library dialog did not load")
            dialog = window.findChild(QObject, "materialLibraryDialog")
            wait_until(lambda: bool(dialog.property("visible")),
                       "lazy material library dialog did not open")
            self.assertTrue(QMetaObject.invokeMethod(dialog, "open"))
            self.settle_materials()
            APP.processEvents()
            self.assertTrue(dialog.property("visible"))
            self.assertLessEqual(dialog.property("width"), window.width())
            new = window.findChild(QObject, "newMaterialButton")
            self.assertTrue(QMetaObject.invokeMethod(new, "clicked"))
            self.settle_materials()
            APP.processEvents()
            name = window.findChild(QObject, "materialNameField")
            name.setProperty("text", "QML custom")
            self.assertTrue(QMetaObject.invokeMethod(name, "textEdited"))
            self.settle_materials()
            save = window.findChild(QObject, "saveMaterialButton")
            self.assertTrue(QMetaObject.invokeMethod(save, "clicked"))
            self.settle_materials()
            self.assertEqual([row for row in source.materialLibrary.entries if not row["builtin"]][0]["name"], "QML custom")
            use = window.findChild(QObject, "useMaterialButton")
            self.assertTrue(QMetaObject.invokeMethod(use, "clicked"))
            self.settle_materials()
            APP.processEvents()
            self.assertFalse(dialog.property("visible"))
            self.assertEqual(source.materialName, "QML custom")

            snapshot = deepcopy(source.documentData()["material"])
            section = window.findChild(QObject, "sourceMaterialSnapshotSection")
            library_button = window.findChild(QObject, "materialLibraryButton")
            emission = window.findChild(QObject, "emissionKind")
            self.assertIsNotNone(section)
            self.assertIsNotNone(library_button)
            self.assertIsNotNone(emission)
            for model in ("uniform", "resistance_boat", "electron_beam"):
                source.setEmissionField("kind", model)
                APP.processEvents()
                self.assertEqual(source.documentData()["material"], snapshot)
                self.assertTrue(source.hasMaterial)
                self.assertEqual(emission.property("currentValue"), model)
                self.assertEqual(section.property("visible"), model != "uniform")
                self.assertEqual(library_button.property("visible"), model != "uniform")
            picker_row = window.findChild(QObject, "sourceMaterialPickerRow")
            action_row = window.findChild(QObject, "sourceMaterialActionsRow")
            source.setEmissionField("kind", "uniform")
            APP.processEvents()
            self.assertFalse(picker_row.isVisible())
            self.assertFalse(action_row.isVisible())

            self.assertEqual(network_factory.blocked_urls, [])

            geometry = window.findChild(QObject, "sourceGeometryShape")
            self.assertIsNotNone(geometry)
            for kind, values in (
                ("line", {"length_mm": "12.5", "angle_deg": "30"}),
                ("rectangle", {"length_mm": "14", "width_mm": "6", "angle_deg": "45"}),
                ("disk", {"radius_mm": "8"}),
                ("annulus", {"inner_radius_mm": "2", "outer_radius_mm": "9"}),
            ):
                source.setField("kind", kind)
                for key, value in values.items():
                    source.setField(key, value)
                APP.processEvents()
                self.assertEqual(geometry.property("currentValue"), kind)
                for key, value in values.items():
                    field = window.findChild(QObject, key)
                    self.assertIsNotNone(field, key)
                    self.assertEqual(field.property("text"), value, key)

            # Capture a CJK evidence set at the minimum, medium, and wide form sizes.
            evidence = ROOT / "build" / "qa_tools_pytest" / "source_form_layout_20261008"
            evidence.mkdir(parents=True, exist_ok=True)
            source.setEmissionField("kind", "electron_beam")
            for label, size in (("100", (780, 540)), ("125", (975, 675)), ("150", (1170, 810))):
                window.resize(*size)
                window.showNormal()
                APP.processEvents()
                image = window.grabWindow()
                self.assertFalse(image.isNull(), f"No rendered image for {label}% viewport")
                self.assertTrue(image.save(str(evidence / f"source_form_{label}.png")))

            saved_path = self.root / "source_form_snapshot.csrc"
            self.assertTrue(source.saveAsPath(str(saved_path)), source.status)
            reopened = self.make_source(self.service, str(saved_path))
            self.assertEqual(reopened.documentData()["material"], snapshot)
            self.assertEqual(reopened.fields["kind"], "annulus")
            self.assertEqual(float(reopened.fields["inner_radius_mm"]), 2.0)
            self.assertEqual(float(reopened.fields["outer_radius_mm"]), 9.0)
            self.assertFalse(warnings, "\n".join(warnings))
        finally:
            window.setProperty("allowClose", True); window.close()
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            APP.setFont(previous_font)
            theme.configure(mode=previous_mode, theme_package_id=previous_theme)
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
