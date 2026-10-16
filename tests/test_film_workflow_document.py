"""Unified .cfilm persistence and session-owned page configuration contracts."""
from copy import deepcopy
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QSignalSpy
from coat_sim.core.film_design import load_design, save_design, validate_design, new_design
from coat_sim.core.film_design_session import FilmDesignEditSession
from coat_sim.ui.film_quick_controller import FilmDesignQuickController
from coat_sim.ui.film_simulation_quick_controller import FilmSimulationQuickController, _display_curve_segments
from test_film_design_generator_session import _MemoryService

_APP = QGuiApplication.instance() or QGuiApplication([])


class FileService(_MemoryService):
    def save(self, path, design, *, overwrite=False):
        if self.fail_save:
            raise OSError("synthetic save failure")
        return save_design(path, design, overwrite=overwrite)

    def load(self, path):
        return load_design(path)

    def simulate(self, design, settings, *, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("cancelled")
        return {"wavelength_nm": [settings["wavelength_min_nm"], settings["wavelength_max_nm"]],
                "R": [0.2, 0.4], "T": [0.8, 0.6], "A": [0.0, 0.0],
                "settings": deepcopy(settings), "design_id": design["id"]}


class FilmWorkflowDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = FileService()
        self.design = FilmDesignQuickController(service=self.service)
        self.sim = FilmSimulationQuickController(service=self.service, source_controller=self.design)

    def tearDown(self):
        self.sim.close()
        self.design.markSourceClosed()
        self.temp.cleanup()

    def wait_done(self):
        limit = time.monotonic() + 5
        while self.sim.busy and time.monotonic() < limit:
            _APP.processEvents()
            time.sleep(.005)
        self.assertFalse(self.sim.busy)
        _APP.processEvents()

    def test_old_file_is_unchanged_and_new_all_configuration_roundtrip(self):
        old = self.design.currentDesign()
        self.assertNotIn("workflow_settings", old)
        path = Path(self.temp.name) / "old.cfilm"
        save_design(path, old)
        self.assertEqual(load_design(path), old)
        revision = self.design.session.snapshot().design_revision
        self.sim.setParameter("samples", 123456789)
        self.sim.setParameter("angle_deg", 31.123456789)
        self.sim.setParameter("blackbody_temperature_k", 6532.1234567)
        self.sim.setPlotSettings({"curves": ["R", "T"], "polarizations": ["s"],
                                  "x": {"auto": False, "min": 450.125, "max": 610.5},
                                  "y": {"unit": "percent", "auto": False, "min": 10, "max": 90}})
        self.sim.setCalculationBand(0, 440.5, 680.125)
        self.sim.setSpectrumAnalysisThresholdPercent(61.2345)
        self.sim.setPngExportSettings({"width_in": 12.125, "height_in": 7.375,
                                       "dpi": 188, "transparent": True})
        optimization = {"schema_version": 1, "objectives": [{"id": "t", "sampling": {"step_nm": .3}}],
                        "variables": [{"id": "layer-a", "min_nm": 1.123456789}],
                        "solver": {"method": "de", "seed": 823}}
        self.assertTrue(self.design.setWorkflowConfiguration("optimization", optimization))
        self.assertEqual(self.design.session.snapshot().design_revision, revision)
        self.assertTrue(self.design.dirty)
        config = self.sim.exportSimulationConfiguration()
        target = Path(self.temp.name) / "all.cfilm"
        self.assertTrue(self.design.saveFilePath(str(target)))
        self.assertFalse(self.design.dirty)
        loaded = load_design(target)
        self.assertEqual(loaded["workflow_settings"]["simulation"], config)
        self.assertEqual(loaded["workflow_settings"]["optimization"], optimization)
        separate = FilmSimulationQuickController(service=self.service)
        try:
            separate.loadDesignFromPath(str(target))
            self.assertEqual(separate.exportSimulationConfiguration(), config)
            self.assertEqual(separate.designPath, str(target))
            self.assertFalse(separate.documentDirty)
        finally:
            separate.close()

    def test_legacy_constructor_preserves_design_first_surface_without_configuration(self):
        self.assertTrue(self.design.setDesignValue("first_surface", "back"))
        paired = FilmSimulationQuickController(service=self.service, source_controller=self.design)
        independent = FilmSimulationQuickController(service=self.service, design=self.design.currentDesign())
        try:
            self.assertEqual(paired.settings["first_surface"], "back")
            self.assertEqual(independent.settings["first_surface"], "back")
        finally:
            paired.close()
            independent.close()

    def test_persisted_simulation_first_surface_overrides_design_default_atomically(self):
        revision = self.design.session.snapshot().design_revision
        self.assertFalse(self.design.setDesignValue("first_surface", "invalid"))
        self.assertEqual(self.design.session.snapshot().design_revision, revision)
        self.assertTrue(self.design.setDesignValue("first_surface", "back"))
        self.assertEqual(self.design.session.snapshot().design_revision, revision + 1)
        self.assertEqual(self.sim.settings["first_surface"], "back")
        self.sim.setParameter("first_surface", "front")
        self.assertEqual(self.design.currentDesign()["first_surface"], "back")
        paired = FilmSimulationQuickController(service=self.service, source_controller=self.design)
        independent = FilmSimulationQuickController(service=self.service, design=self.design.currentDesign())
        try:
            self.assertEqual(paired.settings["first_surface"], "front")
            self.assertEqual(independent.settings["first_surface"], "front")
        finally:
            paired.close()
            independent.close()

    def test_sqlite_roundtrip_retains_all_workflow_scopes(self):
        from coat_sim.core.film_design_store import FilmDesignStore
        self.sim.setParameter("angle_deg", 41.123456789)
        self.design.setWorkflowConfiguration("optimization", {"variables": [{"id": "layer1", "max_nm": 134.56789}]})
        document = self.design.currentDesign()
        store = FilmDesignStore(Path(self.temp.name) / "catalog.sqlite")
        self.assertEqual(store.save(document), document)
        self.assertEqual(store.load(document["id"]), document)

    def test_configuration_finite_validation_noop_and_candidate_preserves_latest(self):
        session = self.design.session
        before = session.snapshot()
        self.assertFalse(session.set_workflow_configuration("simulation", {"bad": math.nan}))
        self.assertEqual(session.snapshot().design, before.design)
        self.assertTrue(session.set_workflow_configuration("optimization", {"threshold": .7}))
        current = session.snapshot()
        self.assertTrue(session.set_workflow_configuration("optimization", {"threshold": .7}))
        self.assertEqual(session.snapshot().revision, current.revision)
        candidate = deepcopy(before.design)
        candidate["name"] = "optimized"
        self.assertTrue(session.apply_optimized_design(candidate, before.design_revision))
        self.assertEqual(session.workflow_configuration("optimization"), {"threshold": .7})
        self.assertFalse(session.apply_optimized_design(before.design, before.design_revision))

    def test_configuration_only_edit_keeps_calculation_token_current(self):
        self.sim.startCalculation()
        self.wait_done()
        self.assertTrue(self.sim.hasResult)
        generation = self.sim._generation
        signature = self.sim._signature()
        self.design.setWorkflowConfiguration("optimization", {"solver": {"seed": 11}})
        self.assertEqual(self.sim._generation, generation)
        self.assertEqual(self.sim._signature(), signature)
        self.assertFalse(self.sim.stale)
        self.sim.setUseK(False)
        self.assertFalse(self.design.currentDesign()["use_k"])
        self.assertTrue(self.sim.stale)

    def test_reuse_same_session_calculates_and_other_document_confirmation_is_atomic(self):
        self.sim.setParameter("angle_deg", 25)
        self.assertTrue(self.sim.reuseWindowOpen(payload=self.design))
        self.assertFalse(self.sim.hasPendingOpen)
        self.sim.requestCalculation()
        self.wait_done()
        self.assertTrue(self.sim.hasResult)
        original = self.sim.designJson
        result = self.sim._result
        other = deepcopy(self.design.currentDesign())
        other["id"] = "other-id"
        other["name"] = "other document"
        spy = QSignalSpy(self.sim.confirmationRequested)
        self.assertTrue(self.sim.reuseWindowOpen(payload=other))
        self.assertEqual(spy.count(), 1)
        self.assertTrue(self.sim.hasPendingOpen)
        self.assertEqual(self.sim.designJson, original)
        self.sim.resolvePendingAction(False)
        self.assertEqual(self.sim.designJson, original)
        self.assertIs(self.sim._result, result)
        self.assertTrue(self.sim.reuseWindowOpen(payload=other))
        self.sim.resolvePendingAction(True)
        self.assertEqual(self.sim.designName, "other document")
        self.assertIsNone(self.sim._source_session)
        prior = self.sim.designJson
        self.assertFalse(self.sim.reuseWindowOpen(payload={"invalid": True}))
        self.assertEqual(self.sim.designJson, prior)

    def test_restore_defaults_on_new_document_and_failure_preserves_config(self):
        self.sim.setParameter("angle_deg", 17)
        self.assertEqual(self.design.getWorkflowConfiguration("simulation")["parameters"]["angle_deg"], 17)
        self.design.requestNewDesign()
        self.assertIsNotNone(self.design.session.snapshot().pending_confirmation)
        self.design.resolvePendingAction(False)
        self.assertEqual(self.sim.settings["angle_deg"], 17)
        self.design.requestNewDesign()
        self.design.resolvePendingAction(True)
        self.assertEqual(self.sim.settings["angle_deg"], 0)
        self.assertFalse(self.design.dirty)
        before = self.sim.exportSimulationConfiguration()
        invalid = deepcopy(before)
        invalid["analysis"]["threshold_fraction"] = 2
        self.assertFalse(self.sim.loadSimulationConfiguration(invalid))
        self.assertEqual(self.sim.exportSimulationConfiguration(), before)
        for malformed in ({"plot": []}, {"parameters": []}, {"png_export": []}, {"analysis": []}):
            self.assertFalse(self.sim.loadSimulationConfiguration(malformed))
            self.assertEqual(self.sim.exportSimulationConfiguration(), before)

    def test_partial_valid_curve_range_gaps_and_user_zoom_are_distinct(self):
        self.sim._result = {"wavelength_nm": [400, 450, 500, 550, 600, 650, 700],
                            "R": [None, .2, .4, None, .6, .8, math.nan],
                            "T": [math.nan] * 7, "settings": {"polarization": "unpolarized"}}
        self.sim._refresh_plot_data()
        bounds = self.sim._plot_base_bounds()
        self.assertEqual((bounds["x_min"], bounds["x_max"]), (450, 650))
        series = self.sim.plotData["series"]
        self.assertEqual(len(series), 1)
        self.assertEqual(len(series[0]["segments"]), 2)
        self.assertEqual(series[0]["validPointCount"], 4)
        self.sim.zoomPlot(.5, .5, .5)
        view = deepcopy(self.sim._view_bounds)
        self.sim._refresh_plot_data()
        self.assertEqual(self.sim._plot_bounds(), view)
        self.sim.resetPlotView()
        self.assertEqual(self.sim._plot_bounds(), bounds)
        self.sim.setPlotSettings({"curves": ["T"]})
        self.assertEqual(self.sim.plotData["series"], [])
        fallback = self.sim._plot_bounds()
        self.assertEqual((fallback["x_min"], fallback["x_max"]), (400, 700))
        self.sim._result["T"][3] = .5
        single = self.sim._plot_base_bounds()
        self.assertLess(single["x_min"], 550)
        self.assertGreater(single["x_max"], 550)
        self.sim._result = {"wavelength_nm": [1e-12], "T": [.5]}
        tiny = self.sim._plot_base_bounds()
        self.assertGreater(tiny["x_min"], 0)
        self.assertLessEqual(tiny["x_min"], 1e-12)
        self.assertGreater(tiny["x_max"], 1e-12)

    def test_requested_recompute_after_edit_discards_old_work_and_coalesces(self):
        started = threading.Event()
        calls = []
        original = self.service.simulate

        def slow(design, settings, *, cancel_event=None):
            calls.append(design["name"])
            if len(calls) == 1:
                started.set()
                deadline = time.monotonic() + 5
                while not cancel_event.is_set() and time.monotonic() < deadline:
                    time.sleep(.002)
                if cancel_event.is_set():
                    raise InterruptedError("old generation cancelled")
            return original(design, settings, cancel_event=cancel_event)

        self.service.simulate = slow
        self.sim.requestCalculation()
        self.assertTrue(started.wait(1))
        self.sim.requestCalculation()
        self.assertFalse(self.sim._pending_recompute)
        self.design.setDesignValue("name", "latest unsaved")
        self.sim.requestCalculation()
        self.sim.requestCalculation()
        self.assertTrue(self.sim._pending_recompute)
        self.wait_done()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1], "latest unsaved")
        self.assertFalse(self.sim.stale)
        self.assertTrue(self.sim.hasResult)

    def test_user_zoom_survives_same_document_recompute_and_physical_edit(self):
        self.sim.requestCalculation()
        self.wait_done()
        self.sim.zoomPlot(.5, .5, .5)
        view = deepcopy(self.sim._view_bounds)
        self.sim.requestCalculation()
        self.wait_done()
        self.assertEqual(self.sim._view_bounds, view)
        self.design.setDesignValue("name", "changed name")
        self.assertEqual(self.sim._view_bounds, view)
        self.sim.requestCalculation()
        self.wait_done()
        self.assertEqual(self.sim._view_bounds, view)

    def test_independent_save_overwrite_close_and_failure_preserve_document(self):
        independent = FilmSimulationQuickController(service=self.service, design=self.design.currentDesign())
        try:
            independent.setParameter("angle_deg", 22.125)
            self.assertTrue(independent.documentDirty)
            self.assertFalse(independent.requestClose())
            independent.resolvePendingAction(False)
            self.assertTrue(independent.documentDirty)
            path = Path(self.temp.name) / "independent.cfilm"
            self.assertTrue(independent.saveFilePath(str(path)))
            self.assertFalse(independent.documentDirty)
            self.assertEqual(load_design(path)["workflow_settings"]["simulation"]["parameters"]["angle_deg"], 22.125)
            independent.setParameter("angle_deg", 23)
            original = path.read_bytes()
            self.assertFalse(independent.saveFilePath(str(path)))
            independent.resolvePendingAction(False)
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(independent.documentDirty)
            self.service.fail_save = True
            self.assertFalse(independent.saveCurrent())
            self.assertTrue(independent.documentDirty)
            self.assertEqual(path.read_bytes(), original)
        finally:
            self.service.fail_save = False
            independent.close()

    def test_same_file_reopen_keeps_unsaved_memory_without_confirmation_or_reload(self):
        path = Path(self.temp.name) / "same.cfilm"
        self.assertTrue(self.design.saveFilePath(str(path)))
        self.design.setDesignValue("name", "unsaved memory")
        before = self.design.currentDesign()
        self.assertTrue(self.design.reuseWindowOpen(str(path)))
        self.assertEqual(self.design.currentDesign(), before)
        self.assertIsNone(self.design.session.snapshot().pending_confirmation)
        self.assertTrue(self.sim.reuseWindowOpen(str(path)))
        self.assertFalse(self.sim.hasPendingOpen)
        self.assertEqual(self.sim.designName, "unsaved memory")
        self.assertNotEqual(load_design(path)["name"], "unsaved memory")

    def test_existing_4096_layer_parser_resource_guard_remains(self):
        design = new_design()
        layer = {"id": "a", "material_id": "", "optical_set_id": "",
                 "thickness": {"value": 1, "unit": "nm"}, "note": ""}
        design["front_layers"] = [{**layer, "id": str(index)} for index in range(4097)]
        with self.assertRaisesRegex(ValueError, "4096"):
            validate_design(design)

    def test_display_segments_are_bounded_without_fusing_gaps(self):
        waves = [float(i + 1) for i in range(20000)]
        values = [float(i % 4) if i % 4 < 3 else None for i in range(20000)]
        displayed, valid_count = _display_curve_segments(waves, values, 1800)
        self.assertEqual(valid_count, 15000)
        self.assertLessEqual(sum(map(len, displayed)), 1800)
        self.assertEqual(displayed[0][0], [1.0, 0.0])
        self.assertEqual(displayed[-1][-1], [19999.0, 2.0])

    def test_thickness_summary_formats_only_display_and_excludes_substrate(self):
        self.design.addLayer("front")
        row = self.design.currentDesign()["front_layers"][0]
        self.design.setLayerValue("front", row["id"], "thickness.unit", "nm")
        self.design.setLayerValue("front", row["id"], "thickness.value", 12.123456789)
        self.assertIn("12.123 nm", self.design.thicknessSummaryText)
        self.assertEqual(self.design.currentDesign()["front_layers"][0]["thickness"]["value"], 12.123456789)
