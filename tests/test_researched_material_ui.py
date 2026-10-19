"""Research-library presentation, guarded selection and real Qt browse controls."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/coat-editor-test-cache")
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from coat_sim.core.api import CoatService
from coat_sim.core.material_research import research_material_records
from coat_sim.core.materials import builtin_material_profiles, default_material_profile, read_material_profile, save_material
from coat_sim.ui.material_research import research_sections
from coat_sim.ui.theme_service import ThemeService
from tests.material_test_helpers import MaterialQtCase
from coat_sim.ui.source_substrate import SourceBridge

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


class ResearchedMaterialUiTests(MaterialQtCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("PROJECT_ROOT", self.root), ("DATA_ROOT", self.root / "data")):
            patcher = patch("coat_sim.core.data_files." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = CoatService(root=self.root, restrict_to_root=False)
        self.source = self.make_source(self.service)
        self.lib = self.source.materialLibrary

    def select_research(self, identity):
        row = next(row for row in builtin_material_profiles()
                   if (row.get("research_data") or {}).get("id") == identity)
        self.assertTrue(self.lib.selectMaterial("builtin:" + row["id"]))
        self.settle_materials()
        self.assertTrue(self.lib.valid, self.lib.error)
        return row

    def open_material_library_dialog(self, engine, window):
        self.lib.prepareForSource()
        self.assertTrue(QMetaObject.invokeMethod(window, "openMaterialLibrary"))
        deadline = time.monotonic() + 8.0
        controller = engine.incubationController()
        dialog = None
        while time.monotonic() < deadline:
            QCoreApplication.processEvents()
            if controller is not None:
                controller.incubateFor(2)
            dialog = window.findChild(QObject, "materialLibraryDialog")
            if dialog is not None and dialog.property("visible"):
                return dialog
            QTest.qWait(10)
        self.fail(
            "SourceWindow did not open its lazy material library within 8 seconds; "
            f"opening={window.property('materialLibraryOpening')}, "
            f"requested={window.property('materialLibraryOpenRequested')}, "
            f"incubator={window.property('materialLibraryDialogIncubator')!r}, "
            f"dialog={dialog!r}"
        )

    def test_default_category_filter_and_search_keep_selection(self):
        self.assertEqual(self.lib.roleFilter, "film")
        self.assertEqual(len(self.lib.entries), len(builtin_material_profiles()))
        self.assertGreater(len(self.lib.filteredEntries), 0)
        self.assertTrue(all(row["category"] == "film" or "film" in row["roles"]
                            for row in self.lib.filteredEntries))
        self.assertIn("substrate", {role for row in self.lib.entries for role in row["roles"]})
        self.assertNotIn("boat", {role for row in self.lib.entries for role in row["roles"]})
        # Proprietary mixtures have a genuinely unknown formula; never join None.
        self.assertTrue(all(isinstance(row["searchText"], str) for row in self.lib.entries))
        self.select_research("ZnS_film")
        original = self.lib.draftData()
        key = self.lib.currentKey
        self.lib.setSearchText("zns")
        self.assertGreaterEqual(len(self.lib.filteredEntries), 1)
        self.lib.setRoleFilter("substrate")
        self.assertGreaterEqual(len(self.lib.filteredEntries), 1)
        self.assertFalse(self.lib.filteredEntries[0]["sourceBindable"])
        self.lib.setSearchText("not-a-material")
        self.assertEqual(self.lib.filteredEntries, [])
        self.lib.setSearchText("")
        self.lib.setRoleFilter("boat")
        self.assertTrue(all("boat" in row["roles"] for row in self.lib.filteredEntries))
        self.lib.setRoleFilter("component")
        self.assertTrue(all("component" in row["roles"] for row in self.lib.filteredEntries))
        self.lib.setRoleFilter("invalid-role")
        self.assertEqual(self.lib.roleFilter, "component")
        self.lib.setRoleFilter("all")
        self.assertEqual(self.lib.draftData(), original)
        self.assertEqual(self.lib.currentKey, key)
        self.assertFalse(self.lib.dirty)

    def test_nonfilm_selection_guard_preserves_source_and_copy_cannot_bypass(self):
        self.select_research("ZnS_film")
        self.assertTrue(self.lib.useMaterial())
        source_before = deepcopy(self.source.documentData())
        for identity in ("ZnS_substrate", "Ge_substrate"):
            with self.subTest(identity=identity):
                self.select_research(identity)
                self.assertFalse(self.lib.sourceBindable)
                self.assertFalse(self.lib.canUseMaterial)
                self.assertFalse(self.lib.useMaterial())
                self.assertIn("不能绑定", self.lib.status)
                self.assertEqual(self.source.documentData(), source_before)
                roles = self.lib.roles
                reference = self.lib.researchData
                self.assertTrue(self.lib.copyMaterial())
                self.settle_materials()
                # Same-scope user names are unique, so each copy gets its own name.
                self.lib.setField("name", f"ZnS copy {identity}")
                self.assertTrue(self.lib.save(), self.lib.status)
                self.settle_materials()
                self.assertEqual(self.lib.roles, roles)
                self.assertEqual(self.lib.researchData, reference)
                self.assertFalse(self.lib.useMaterial())
                self.assertEqual(self.source.documentData(), source_before)

    def test_legacy_roleless_and_multirole_film_profiles_are_still_bindable(self):
        self.lib.newMaterial()
        self.assertTrue(self.lib.canUseMaterial)
        self.assertNotIn("roles", self.lib.draftData())
        self.assertFalse(self.lib.hasReferenceData)
        self.lib.setField("name", "Ta2O5")
        self.assertFalse(self.lib.hasReferenceData)
        self.assertTrue(self.lib.useMaterial())
        self.assertNotIn("research_data", self.source.documentData()["material"])
        profile = default_material_profile("Dual role", id="dual")
        profile["roles"] = ["film", "element"]
        save_material(self.root / "data/materials", profile)
        self.lib.confirmDiscard(False)
        self.settle_materials()
        self.assertTrue(self.lib.save())
        self.settle_materials()
        self.lib.reload()
        self.settle_materials()
        self.assertTrue(self.lib.selectMaterial("user:dual"))
        self.settle_materials()
        self.assertTrue(self.lib.canUseMaterial)
        self.lib.setRoleFilter("element")
        self.assertEqual(self.lib.filteredEntries, [])
        self.lib.setRoleFilter("film")
        self.assertIn("dual", [row["id"] for row in self.lib.filteredEntries])
        self.assertTrue(self.lib.useMaterial())

    def test_research_and_roles_survive_edit_save_reload_source_and_defensive_copies(self):
        original = self.select_research("Ta2O5_film")
        reference = self.lib.researchData
        self.assertTrue(self.lib.hasReferenceData)
        self.assertFalse(self.lib.referenceData)
        self.assertIn("条件性参考", self.lib.propertyCoverage)
        self.assertIn("不支持", self.lib.readiness)
        self.assertTrue(self.lib.builtin)
        self.assertFalse(self.lib.readOnly)
        self.lib.setField("name", "Edited research preset")
        self.assertTrue(self.lib.dirty)
        self.assertTrue(self.lib.save(), self.lib.status)
        self.settle_materials()
        self.assertTrue(self.lib.builtinOverridden)
        self.assertEqual(self.lib.fields["name"], "Edited research preset")
        self.assertEqual(self.lib.researchData, reference)
        self.assertEqual(self.lib.roles, original["roles"])
        exposed = self.lib.researchData
        exposed["properties"].clear()
        exposed_roles = self.lib.roles
        exposed_roles.clear()
        self.assertEqual(self.lib.researchData, reference)
        self.assertEqual(self.lib.roles, original["roles"])
        self.assertTrue(self.lib.copyMaterial())
        self.settle_materials()
        self.lib.setField("name", "My oxide reference")
        self.assertTrue(self.lib.save(), self.lib.status)
        self.settle_materials()
        path = self.root / "data/materials" / (self.lib.currentId + ".json")
        saved = read_material_profile(path)
        self.assertEqual(saved["research_data"], reference)
        self.assertEqual(saved["roles"], ["film"])
        self.assertTrue(self.lib.useMaterial())
        self.assertFalse(self.source.referenceCalculator.available)
        source_snapshot = deepcopy(self.source.documentData())
        self.lib.setField("description", "Later library edit")
        self.assertTrue(self.lib.save())
        self.settle_materials()
        self.lib.reload()
        self.settle_materials()
        self.assertEqual(self.source.documentData(), source_snapshot)
        self.assertEqual(self.lib.researchData, reference)
        other_source = self.make_source(self.service)
        other = other_source.materialLibrary
        self.assertTrue(other.selectMaterial("user:" + saved["id"]))
        self.settle_materials()
        self.assertEqual(other.researchData, reference)

    def test_all_properties_sources_conditions_original_metadata_and_gaps_are_present(self):
        profiles = [profile for profile in builtin_material_profiles() if profile.get("research_data")]
        expected = {record["id"] for record in research_material_records()}
        self.assertEqual({profile["research_data"]["id"] for profile in profiles}, expected)
        for profile in profiles:
            with self.subTest(profile=profile["id"]):
                self.assertTrue(self.lib.selectMaterial("builtin:" + profile["id"]))
                self.settle_materials()
                sections = self.lib.referenceSections
                serialized = json.dumps(sections, ensure_ascii=False)
                research = profile["research_data"]
                property_sections = [s for s in sections if s["title"].startswith("物性 · ")]
                self.assertEqual(len(property_sections), len(research["properties"]))
                for record, section in zip(research["properties"], property_sections):
                    self.assertIn(record["id"], section["title"])
                    self.assertIn(record["status"], section["body"])
                    for source in record["source_ids"]:
                        self.assertIn(source, section["body"])
                for source in research["sources"]:
                    self.assertIn(source["id"], serialized)
                    self.assertIn(source["url"], serialized)
                    self.assertIn(source["local_path"], serialized)
                self.assertIn("未知不是零", serialized)
                self.assertIn("条件", serialized)
        self.select_research("ZnS_film")
        self.assertIn("IR01", json.dumps(self.lib.referenceSections))
        self.assertIn("zns_ri8301_gas_v1", json.dumps(self.lib.referenceData))

    def test_formatter_preserves_zero_unknown_multiple_sources_and_extension_metadata(self):
        research = {"id": "sample", "name": "Sample", "formula": None, "roles": ["element"],
                    "sources": [{"id": "s1", "title": "First", "url": "https://example.com/1", "locator": "page 2"},
                                {"id": "s2", "title": "Second", "url": "https://example.com/2", "locator": "table 5"}],
                    "properties": [{"id": "p1", "name": "density", "value": 0, "unit": "kg/m3", "conditions": {"state": "test"}, "source_ids": ["s1", "s2"], "status": "quarantined", "notes": "Zero is retained, not adopted", "future_metadata": "retained-value"},
                                   {"id": "p2", "name": "missing", "value": None, "unit": "", "source_ids": [], "status": "unknown"}],
                    "gaps": ["temperature-rate-model"], "topics": ["optics"], "future_metadata": "catalog-metadata"}
        before = deepcopy(research)
        result = research_sections(research)
        text = json.dumps(result, ensure_ascii=False)
        self.assertIn("数值 / 关系：0 kg/m3", text)
        self.assertIn("未知 / 未给出", text)
        for value in ("s1", "s2", "page 2", "table 5", "retained-value", "catalog-metadata", "temperature-rate-model"):
            self.assertIn(value, text)
        self.assertTrue(next(s for s in result if "p1" in s["title"])["warning"])
        self.assertEqual(research, before)

    def test_real_qml_filter_guard_close_reopen_and_render_at_minimum_size(self):
        engine = QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda messages: warnings.extend(message.toString() for message in messages))
        engine.rootContext().setContextProperty("bridge", self.source)
        ThemeService.instance().attach_engine(engine, "Theme")
        engine.load(QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/SourceWindow.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        try:
            window.showNormal()
            window.resize(1180, 860)
            dialog = self.open_material_library_dialog(engine, window)
            self.settle_materials()
            QTest.qWait(50)
            self.assertTrue(dialog.property("visible"))
            search = dialog.findChild(QObject, "materialSearchField")
            search.setProperty("text", "ZnS")
            self.assertTrue(QMetaObject.invokeMethod(search, "textEdited"))
            self.settle_materials()
            self.assertEqual(self.lib.searchText, "ZnS")
            self.assertGreaterEqual(len(self.lib.filteredEntries), 1)
            role = dialog.findChild(QObject, "materialRoleFilter")
            self.assertEqual(role.property("count"), 2)
            self.assertGreaterEqual(role.property("currentIndex"), 0)
            role.setProperty("currentIndex", 1)
            role.activated.emit(1)
            self.assertEqual(self.lib.roleFilter, "substrate")
            self.assertGreaterEqual(len(self.lib.filteredEntries), 1)
            self.select_research("ZnS_substrate")
            QTest.qWait(50)
            use = dialog.findChild(QObject, "useMaterialButton")
            self.assertFalse(use.property("enabled"))
            self.assertIn("不能", dialog.findChild(QObject, "materialReadinessLabel").property("text"))
            self.assertFalse(self.lib.useMaterial())
            self.assertFalse(self.source.hasMaterial)
            screenshot_dir = os.environ.get("COAT_PREVIEW_SCREENSHOT_DIR")
            if screenshot_dir:
                Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
            for width, height, name in ((1180, 860, "research-library-substrate.png"), (780, 540, "research-library-minimum.png")):
                window.resize(width, height)
                QTest.qWait(80)
                self.assertLessEqual(dialog.property("width"), width)
                self.assertLessEqual(dialog.property("height"), height)
                image = window.grabWindow()
                self.assertFalse(image.isNull())
                if screenshot_dir:
                    self.assertTrue(image.save(str(Path(screenshot_dir) / name)))
            self.assertTrue(QMetaObject.invokeMethod(dialog, "close"))
            self.settle_materials()
            self.assertTrue(QMetaObject.invokeMethod(window, "openMaterialLibrary"))
            self.settle_materials()
            QTest.qWait(30)
            self.assertEqual(self.lib.roleFilter, "substrate")
            self.assertEqual(self.lib.searchText, "ZnS")
            self.assertFalse(use.property("enabled"))
            self.lib.setRoleFilter("all")
            self.lib.setSearchText("")
            self.select_research("Ta2O5_film")
            QTest.qWait(30)
            self.assertTrue(use.property("enabled"))
            window.resize(1180, 860)
            QTest.qWait(60)
            if screenshot_dir:
                self.assertTrue(window.grabWindow().save(str(Path(screenshot_dir) / "research-library-film.png")))
            scroll = dialog.findChild(QObject, "materialResearchScroll")
            self.assertLessEqual(scroll.property("contentWidth"), scroll.property("availableWidth"))
            flick = scroll.property("contentItem")
            flick.setProperty("contentY", 570)
            QTest.qWait(80)
            if screenshot_dir:
                self.assertTrue(window.grabWindow().save(str(Path(screenshot_dir) / "research-library-properties.png")))
            flick.setProperty("contentY", max(0, flick.property("contentHeight") - scroll.property("availableHeight")))
            QTest.qWait(80)
            if screenshot_dir:
                self.assertTrue(window.grabWindow().save(str(Path(screenshot_dir) / "research-library-sources.png")))
            started = time.perf_counter()
            self.select_research("ITO_film")
            QTest.qWait(100)
            self.assertTrue(self.lib.canUseMaterial)
            self.assertEqual(self.lib.researchData["id"], "ITO_film")
            self.assertGreater(len(self.lib.referenceSections), 100)
            self.assertEqual(flick.property("contentY"), 0)
            image = window.grabWindow()
            self.assertFalse(image.isNull())
            elapsed = time.perf_counter() - started
            if screenshot_dir:
                self.assertTrue(image.save(str(Path(screenshot_dir) / "research-library-ito.png")))
                (Path(screenshot_dir) / "ito-render.json").write_text(json.dumps({"select_and_render_seconds": elapsed, "sections": len(self.lib.referenceSections), "snapshot_bytes": len(json.dumps(self.lib.researchData).encode())}, indent=2))
            self.assertTrue(self.lib.newMaterial())
            self.settle_materials()
            category = dialog.findChild(QObject, "materialCategoryEditor")
            self.assertIsNotNone(category)
            self.assertTrue(category.property("visible"))
            self.assertEqual(category.property("count"), 2)
            category.setProperty("currentIndex", 1)
            category.activated.emit(1)
            self.assertEqual(self.lib.category, "substrate")
            self.assertFalse(warnings, "\n".join(warnings))
        finally:
            window.setProperty("allowClose", True)
            window.close()
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
