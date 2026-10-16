"""Bounded source-channel geometry, legacy sampling and actual editor checks."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/coat-refinement-test-cache")
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
from coat_sim.core.source_profiles import (source_samples, source_cover, source_visibility,
    validate_source_profile, effective_source_exponent)
from coat_sim.core.simulator import Settings


class SourceRefinementPhysicsTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(source_x_mm=0, source_y_mm=0, source_z_mm=0, source_exponent=2)

    def cover(self, **kwargs):
        return source_cover({"kind": "point", "emission_model": {
            "kind": "resistance_boat", "boat_mode": "covered", "cover_height_mm": 10,
            **kwargs}}, self.settings)

    def test_legacy_single_hole_migrates_with_identical_visibility(self):
        profile = {"kind": "disk", "radius_mm": 8, "device_type": "crucible",
                   "emission_model": {"kind": "resistance_boat", "boat_mode": "covered",
                       "cover_height_mm": 10, "cover_thickness_mm": 2,
                       "aperture_kind": "disk", "aperture_radius_mm": 2,
                       "aperture_offset_x_mm": 1, "aperture_offset_y_mm": -1}}
        migrated = validate_source_profile(profile)
        self.assertEqual(migrated["device_type"], "crucible")
        self.assertEqual(len(migrated["emission_model"]["apertures"]), 1)
        hole = migrated["emission_model"]["apertures"][0]
        self.assertEqual((hole["kind"], hole["radius_mm"], hole["offset_x_mm"]), ("disk", 2, 1))
        cover = source_cover(profile, self.settings)
        old_cover = deepcopy(cover)
        old_cover.pop("apertures")
        x = np.linspace(-40, 40, 101)
        np.testing.assert_array_equal(source_visibility(np, x, x/3, 100, 0, 0, 0, cover),
                                      source_visibility(np, x, x/3, 100, 0, 0, 0, old_cover))
        self.assertEqual(validate_source_profile(migrated), migrated)

    def test_finite_plate_requires_same_hole_at_both_faces(self):
        cover = self.cover(cover_thickness_mm=10, apertures=[
            {"kind": "disk", "radius_mm": .3, "offset_x_mm": 0},
            {"kind": "disk", "radius_mm": .3, "offset_x_mm": 4}])
        # First ray enters x=0 at z=10 and exits x=4 at z=20. It crosses solid wall.
        result = source_visibility(np, np.array([8., 0., 4.]), np.zeros(3), 30,
                                   np.array([-4., 0., 4.]), np.zeros(3), 0, cover)
        np.testing.assert_array_equal(result, [False, True, True])
        cover["cover_thickness_mm"] = 0
        self.assertTrue(source_visibility(np, np.array([8.]), np.zeros(1), 30, -4, 0, 0, cover)[0])

    def test_overlapping_holes_do_not_duplicate_weight_and_plate_absorbs(self):
        hole = {"kind": "disk", "radius_mm": 1}
        cover = self.cover(apertures=[hole, hole])
        visible = source_visibility(np, np.array([0., 5., 20.]), np.zeros(3), 100, 0, 0, 0, cover)
        np.testing.assert_array_equal(visible, [True, True, False])
        weights = np.ones(3)/3
        self.assertAlmostEqual(float((visible*weights).sum()), 2/3)

    def test_rotated_local_aperture_and_parallel_rays(self):
        cover = self.cover(apertures=[{"kind": "rectangle", "length_mm": 2,
                                       "width_mm": .2, "angle_deg": 90}])
        cover["angle_deg"] = 90
        np.testing.assert_array_equal(source_visibility(np, np.array([5., 0.]), np.array([0., 5.]),
                                                         100, 0, 0, 0, cover), [True, False])
        with np.errstate(all="raise"):
            self.assertTrue(source_visibility(np, np.array([100.]), np.zeros(1), 0, 0, 0, 0, cover)[0])

    def test_invalid_aperture_lists_and_sampling_orders_are_rejected(self):
        for holes in ([], [{}]*101, [{"kind":"triangle"}], [{"radius_mm":0}], [{"bad":1}]):
            with self.subTest(holes=str(holes)[:50]), self.assertRaises(ValueError):
                self.cover(apertures=holes)
        for order in (0, 2, 4, 43, True, 3.0):
            with self.subTest(order=order), self.assertRaises(ValueError):
                validate_source_profile({"kind":"point", "sampling_order":order})

    def test_default_uniform_source_nodes_are_unchanged(self):
        cases = [(dict(kind="point"), 1), (dict(kind="line", length_mm=20), 9),
                 (dict(kind="disk", radius_mm=10), 12),
                 (dict(kind="annulus", inner_radius_mm=5, outer_radius_mm=10), 16),
                 (dict(kind="rectangle", length_mm=20, width_mm=10), 16)]
        for profile, count in cases:
            with self.subTest(kind=profile["kind"]):
                plain = source_samples(self.settings, profile)
                explicit = source_samples(self.settings, dict(profile, emission_model={"kind":"uniform"}))
                null = source_samples(self.settings, dict(profile, sampling_order=None))
                self.assertEqual(len(plain[1]), count)
                for actual in (explicit, null):
                    for a, b in zip(plain, actual):
                        np.testing.assert_array_equal(a, b)
                refined = source_samples(self.settings, dict(profile, sampling_order=21))
                self.assertAlmostEqual(float(refined[1].sum()), 1)
                if profile["kind"] != "point":
                    self.assertGreater(len(refined[1]), count)

    def test_disk_knife_edge_refinement_against_analytic_area(self):
        profile = {"kind":"disk", "radius_mm":1}
        edge = np.linspace(-1, 1, 401)
        expected = .5 + (np.arcsin(edge) + edge*np.sqrt(1-edge**2))/np.pi
        errors = []
        for source in (profile, dict(profile, sampling_order=41)):
            positions, weights = source_samples(self.settings, source)
            transmission = (positions[:, 0, None] <= edge) * weights[:, None]
            errors.append(float(np.max(np.abs(transmission.sum(axis=0)-expected))))
        self.assertLess(errors[1], errors[0]/3)
        self.assertLess(errors[1], .025)
        self.assertNotIn("emission_model", validate_source_profile(dict(profile, sampling_order=41)))

    def test_angle_exponent_and_spatial_gaussian_remain_independent(self):
        source = {"kind":"disk", "radius_mm":10, "source_exponent":2,
                  "emission_model":{"kind":"electron_beam", "spot_sigma_mm":2}}
        positions, weights = source_samples(self.settings, source)
        angular = dict(source, source_exponent=6)
        ap, aw = source_samples(self.settings, angular)
        np.testing.assert_array_equal(positions, ap)
        np.testing.assert_array_equal(weights, aw)
        spatial = deepcopy(source)
        spatial["emission_model"]["spot_sigma_mm"] = 5
        self.assertEqual(effective_source_exponent(self.settings, spatial), 2)
        self.assertEqual(effective_source_exponent(self.settings, angular), 6)
        self.assertFalse(np.allclose(source_samples(self.settings, spatial)[1], weights))


class SourceRefinementEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from coat_sim.core.api import CoatService
        from coat_sim.ui.source_substrate import SourceBridge
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("PROJECT_ROOT", self.root), ("DATA_ROOT", self.root/"data")):
            patcher = patch("coat_sim.core.data_files."+name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bridge = SourceBridge(CoatService(self.root, restrict_to_root=False))
        self.bridges = [self.bridge]

    def tearDown(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        for bridge in self.bridges:
            bridge.materialLibrary.waitForBackgroundTasks()
            bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_crucible_snapshot_load_preserves_physics_without_ui_device_selector(self):
        legacy = {"kind":"disk", "radius_mm":12, "device_type":"crucible", "source_exponent":3}
        self.bridge.applyDocument(legacy)
        self.assertEqual(self.bridge.documentData()["device_type"], "crucible")
        self.assertNotIn("emission_model", self.bridge.documentData())
        self.assertEqual(self.bridge.emissionFields["kind"], "uniform")
        self.bridge.setField("sampling_order", "21")
        self.assertNotIn("emission_model", self.bridge.documentData())
        self.assertEqual(self.bridge.documentData()["sampling_order"], 21)

    def test_hole_edits_preserve_invalid_draft_and_roundtrip(self):
        from coat_sim.ui.source_substrate import SourceBridge
        self.bridge.setEmissionField("kind", "resistance_boat")
        self.bridge.setEmissionField("boat_mode", "covered")
        self.bridge.setEmissionField("aperture_radius_mm", "3")
        self.bridge.addAperture()
        self.bridge.setApertureField("kind", "disk")
        self.bridge.setApertureField("offset_x_mm", "7")
        self.bridge.setApertureField("radius_mm", "oops")
        self.assertFalse(self.bridge.valid)
        self.assertEqual(self.bridge.apertureFields["radius_mm"], "oops")
        self.bridge.setApertureField("radius_mm", "2")
        self.assertTrue(self.bridge.valid, self.bridge.error)
        before = deepcopy(self.bridge.documentData())
        self.assertTrue(self.bridge.saveAsPath(str(self.root/"holes.csrc")), self.bridge.status)
        restored = SourceBridge(self.bridge.service, str(self.root/"holes.csrc"))
        self.bridges.append(restored)
        self.assertEqual(restored.documentData(), self.bridge.documentData())
        self.assertEqual(len(restored.documentData()["emission_model"]["apertures"]), 2)
        self.bridge.setField("description", "Unsaved cover")
        unsaved = deepcopy(self.bridge.documentData())
        self.bridge.newDocument()
        self.bridge.confirmDiscard(False)
        self.assertEqual(self.bridge.documentData(), unsaved)
        restored.selectAperture(0)
        restored.setApertureField("length_mm", "invalid")
        restored.removeAperture()
        self.assertTrue(restored.valid, restored.error)
        self.assertEqual(restored.apertureFields["offset_x_mm"], "7")

    def test_prepared_material_commit_skips_evidence_revalidation_but_export_stays_strict(self):
        from coat_sim.core.materials import default_material_profile
        from coat_sim.ui.source_substrate import SubstrateBridge
        for bridge, category in ((self.bridge, "film"), (SubstrateBridge(self.bridge.service), "substrate")):
            if bridge is not self.bridge:
                self.bridges.append(bridge)
            material = default_material_profile("Prepared", "prepared-" + category)
            material["category"] = category
            with patch("coat_sim.core.materials.validate_material_profile", side_effect=AssertionError("UI revalidation")):
                bridge._select_prepared_material(material)
                bridge.setField("description", "ordinary UI edit")
                self.assertTrue(bridge.valid, bridge.error)
                self.assertIs(bridge._material, material)
                with self.assertRaisesRegex(AssertionError, "UI revalidation"):
                    bridge.documentData()
            exported = bridge.documentData()
            exported["material"]["name"] = "Changed externally"
            self.assertEqual(bridge.materialName, "Prepared")
            bad = dict(material, name="")
            with self.assertRaises(ValueError):
                bridge._select_material(bad)
            self.assertEqual(bridge.materialName, "Prepared")

    def test_prepared_snapshot_survives_library_optical_copy_on_write(self):
        from coat_sim.core.materials import default_material_profile
        from coat_sim.core.optical_constants import import_optical_text
        from coat_sim.ui.source_substrate import SubstrateBridge
        for bridge, category in ((self.bridge, "film"), (SubstrateBridge(self.bridge.service), "substrate")):
            if bridge is not self.bridge:
                self.bridges.append(bridge)
            material = default_material_profile("Shared immutable evidence", "shared-" + category)
            material["category"] = category
            material["optical_constants"] = [import_optical_text("400,1.5\n700,1.6", name="Original", wavelength_unit="nm")]
            library = bridge.materialLibrary
            library._load_draft(material, [])
            bridge._select_prepared_material(material)
            snapshot = deepcopy(bridge.documentData()["material"])
            identity = material["optical_constants"][0]["id"]
            self.assertTrue(library.editOpticalMetadata(identity, "Library changed", "new notes"))
            library.setField("name", "Different library name")
            self.assertEqual(bridge.documentData()["material"], snapshot)
            library.removeOpticalSet(identity)
            self.assertEqual(bridge.documentData()["material"], snapshot)
            self.assertEqual(library.draftData().get("optical_constants", []), [])

    def test_reference_calculator_uses_weak_parent_and_dies_with_editor(self):
        import weakref
        import shiboken6
        from PySide6.QtCore import QCoreApplication, QEvent
        from coat_sim.ui.source_substrate import SourceBridge
        source = SourceBridge(self.bridge.service)
        calculator = source.referenceCalculator
        self.assertIsInstance(calculator.source, weakref.ProxyTypes)
        source.newDocument()
        calculator.begin()
        self.assertFalse(calculator.available)
        source.materialLibrary.waitForBackgroundTasks()
        source.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.assertFalse(shiboken6.isValid(calculator))

    def test_preview_caches_only_matching_physics_and_recomputes_after_edit(self):
        from coat_sim.core.source_profiles import source_samples as sample
        self.bridge.setField("kind", "disk")
        self.bridge.setEmissionField("kind", "electron_beam")
        with patch("coat_sim.core.source_profiles.source_samples", wraps=sample) as compute:
            first = self.bridge.samplePoints
            self.assertEqual(self.bridge.sampleCount, len(first))
            self.assertIn("源元", self.bridge.previewSummary)
            self.assertEqual(compute.call_count, 1)
            self.bridge.setPreviewRate("100")
            self.bridge.samplePoints
            self.assertEqual(compute.call_count, 2)
            self.bridge.setEmissionField("spot_sigma_mm", "6")
            self.assertNotEqual(first, self.bridge.samplePoints)
            self.assertEqual(compute.call_count, 3)
            self.assertNotIn("material", compute.call_args.args[1])

    def test_material_and_optical_snapshots_are_immutable_in_both_editors(self):
        from coat_sim.core.materials import default_material_profile
        from coat_sim.core.optical_constants import import_optical_text
        from coat_sim.ui.source_substrate import SourceBridge, SubstrateBridge
        dataset = import_optical_text("400,1.52\n700,1.50", name="Measured n", wavelength_unit="nm")
        film = default_material_profile("Film sample", "film-sample")
        film.update(category="film", optical_constants=[dataset])
        self.bridge._select_material(film)
        self.assertEqual(self.bridge.emissionFields["kind"], "uniform")
        self.assertNotIn("emission_model", self.bridge.documentData())
        film["optical_constants"][0]["n"][0] = 8
        self.assertEqual(self.bridge.documentData()["material"]["optical_constants"][0]["n"][0], 1.52)
        substrate = SubstrateBridge(self.bridge.service)
        self.bridges.append(substrate)
        geometry = substrate.documentData()
        material = default_material_profile("Glass sample", "glass-sample")
        material.update(category="substrate", optical_constants=[dataset])
        substrate._select_material(material)
        self.assertTrue(substrate.valid, substrate.error)
        self.assertEqual(substrate.documentData()["outline"], geometry["outline"])
        snapshot = deepcopy(substrate.documentData()["material"])
        material["optical_constants"][0]["n"][0] = 9
        self.assertEqual(substrate.documentData()["material"], snapshot)
        substrate._select_material(film)
        self.assertEqual(substrate.documentData()["material"], snapshot)
        path = self.root / "glass.csub"
        self.assertTrue(substrate.saveAsPath(str(path)), substrate.status)
        restored = SubstrateBridge(self.bridge.service, str(path))
        self.bridges.append(restored)
        self.assertEqual(restored.documentData(), substrate.documentData())
        restored.clearMaterial()
        self.assertNotIn("material", restored.documentData())
        self.assertEqual(restored.documentData()["surface"], geometry["surface"])
        self.bridge._select_material(snapshot)
        self.assertEqual(self.bridge.documentData()["material"]["id"], "film-sample")

    def test_qml_unified_selector_and_multi_hole_controls(self):
        from PySide6.QtCore import QUrl, QMetaObject, QObject, Q_ARG, QCoreApplication, QEvent
        from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
        from coat_sim.ui.theme_service import ThemeService
        engine = QQmlApplicationEngine()
        ThemeService.instance().attach_engine(engine, "Theme")
        warnings = []
        engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
        engine.rootContext().setContextProperty("bridge", self.bridge)
        engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1]/"coat_sim/ui/qml/SourceWindow.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]
        try:
            window.showNormal(); window.resize(780, 540); self.app.processEvents()
            source_loader = window.findChild(QObject, "sourceFormLoader")
            self.assertIsNotNone(source_loader)
            loader_status = QQmlExpression(
                QQmlEngine.contextForObject(source_loader), source_loader,
                "status === Loader.Ready",
            )
            deadline = time.monotonic() + 10
            while True:
                ready, _ = loader_status.evaluate()
                self.assertFalse(loader_status.hasError(), loader_status.error())
                if ready and source_loader.property("item") is not None:
                    break
                self.app.processEvents()
                if time.monotonic() >= deadline:
                    self.fail("asynchronous source form did not load")
                time.sleep(0.005)
            self.app.processEvents()
            combo = window.findChild(QObject, "emissionKind")
            self.assertEqual(combo.property("count"), 3)
            self.assertEqual(combo.property("currentText"), "简单快速模型")
            combo.setProperty("currentIndex", 1)
            self.assertTrue(QMetaObject.invokeMethod(combo, "activated", Q_ARG(int, 1)))
            self.app.processEvents()
            self.assertEqual(self.bridge.fields["device_type"], "resistance_boat")
            self.bridge.setEmissionField("boat_mode", "covered")
            self.app.processEvents()
            add = window.findChild(QObject, "addApertureButton")
            self.assertTrue(QMetaObject.invokeMethod(add, "clicked"))
            self.app.processEvents()
            self.assertEqual(len(self.bridge.apertures), 2)
            offset = window.findChild(QObject, "aperture_offset_x_mm")
            offset.setProperty("text", "11")
            self.assertTrue(QMetaObject.invokeMethod(offset, "textEdited"))
            self.assertEqual(self.bridge.apertureFields["offset_x_mm"], "11")
            remove = window.findChild(QObject, "removeApertureButton")
            self.assertTrue(QMetaObject.invokeMethod(remove, "clicked"))
            self.app.processEvents()
            self.assertEqual(len(self.bridge.apertures), 1)
            self.assertFalse(remove.property("enabled"))
            self.assertIsNone(window.findChild(QObject, "sourceReferenceButton"))
            self.assertFalse(warnings, "\n".join(warnings))
        finally:
            window.setProperty("allowClose", True); window.close()
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
