"""Small offline regressions for process editing and full-physics board modes."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
import numpy as np
from PySide6.QtCore import QObject, QUrl
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression, QQmlEngine
from PySide6.QtWidgets import QApplication

from coat_sim.core.api import CoatService
from coat_sim.core.mask_optimization import optimize_correction_boards
from coat_sim.core.mask_optimization_workflow import prepare_optimization_runtime
from coat_sim.core.process_simulator import simulate_process
from coat_sim.core.simulator import Settings
from coat_sim.ui.process import ProcessBridge
from coat_sim.ui.film_stack import _qml_film_stack_parameters

ROOT = Path(__file__).resolve().parents[1]


def fixture(root, mask_count=2, source_count=2):
    service = CoatService(root=root)
    service.save("source", root / "source.csrc", {"schema_version": 2, "kind": "point", "device_type": "custom", "source_exponent": 1.0})
    service.save("substrate", root / "substrate.csub", {"schema_version": 1, "name": "Test", "outline": {"kind": "circle", "radius_mm": 10.0}, "surface": {"kind": "flat"}})
    masks = [{"z_mm": 50.0, "outer": [[-5., -5.], [-1., -5.], [-1., 5.], [-5., 5.]], "holes": [[[-4., -1.], [-2., -1.], [-2., 1.], [-4., 1.]]]},
             {"z_mm": 60.0, "outer": [[1., -5.], [5., -5.], [5., 5.], [1., 5.]], "holes": []}]
    for i in range(mask_count):
        service.save("mask", root / f"mask{i}.cmsk", masks[i])
    machine = {"schema_version": 5, "carrier_x_mm": 0., "carrier_y_mm": 0., "planet_offset_mm": 0., "planet_tilt_deg": 0., "substrate_z_mm": 100., "sun_teeth": 120, "planet_teeth": 36, "gear_module_mm": 0., "sources": [{"file": "source.csrc", "x_mm": 0., "y_mm": 0., "z_mm": 0.} for _ in range(source_count)], "masks": [{"file": f"mask{i}.cmsk"} for i in range(mask_count)], "monitors": []}
    service.save("machine", root / "machine.cmac", machine)
    recipe = {"schema_version": 6, "name": "Test", "machine_file": "machine.cmac", "substrate_file": "substrate.csub", "steps": [{"source_slot": i + 1, "target_nm": 1., "rate_nm_s": 1., "target_mode": "mean", "mask_slots": [i] if i < mask_count else []} for i in range(source_count)]}
    path = root / "process.cproc"
    service.save("process", path, recipe)
    return service, path


def options(mode="auto_design", slot=0):
    return {"mode": mode, "mask_slots": [slot], "source_slot": 1, "mask_bounds": [{"slot": slot, "z_mm": 55., "x_min_mm": -8., "x_max_mm": 8., "y_min_mm": -8., "y_max_mm": 8.}], "backend": "CPU", "fast_cycle": False, "search_grid_size": 21, "max_evaluations": 6, "seed_grid_points": 1, "control_points": 6}


class ProcessEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service, self.path = fixture(self.root)
        self.bridge = ProcessBridge(self.service, str(self.path))

    def test_angstrom_display_and_edit_preserve_legacy_physical_rate(self):
        original = self.path.read_bytes()
        self.assertEqual(self.bridge.visibleSteps[0]["rate_angstrom_s"], 10.)
        self.assertEqual(self.bridge.document["steps"][0]["rate_nm_s"], 1.)
        self.bridge.save()
        self.assertEqual(json.loads(self.path.read_text())["steps"][0]["rate_nm_s"], 1.)
        self.bridge.setStep(0, "rate_angstrom_s", "1")
        self.assertAlmostEqual(self.bridge.document["steps"][0]["rate_nm_s"], .1)
        self.assertNotIn("rate_angstrom_s", self.bridge.document["steps"][0])
        converted = _qml_film_stack_parameters({"materials": [{"rate_angstrom_s": 1}]})
        self.assertAlmostEqual(converted["materials"][0]["rate_nm_s"], .1)
        legacy = _qml_film_stack_parameters({"materials": [{"rate_nm_s": 1}]})
        self.assertEqual(legacy["materials"][0]["rate_nm_s"], 1)

    def test_save_as_explicit_confirmation_cancel_overwrite_and_atomic_failure(self):
        target = self.root / "existing.cproc"
        existing = json.loads(self.path.read_text())
        existing["name"] = "Existing recipe"
        old_content = json.dumps(existing)
        target.write_text(old_content, encoding="utf-8")
        requested = []
        self.bridge.overwriteRequested.connect(lambda url: requested.append(url.toLocalFile()))
        self.bridge.saveAsPath(QUrl.fromLocalFile(str(target)))
        self.assertEqual([Path(value).resolve() for value in requested], [target.resolve()])
        self.assertEqual(target.read_text(), old_content)
        self.bridge.confirmSaveAs(False)
        self.assertEqual(target.read_text(), old_content)
        self.bridge.saveAsPath(QUrl.fromLocalFile(str(target)))
        with patch("coat_sim.core.api.os.replace", side_effect=OSError("injected atomic failure")):
            self.bridge.confirmSaveAs(True)
        self.assertEqual(target.read_text(), old_content)
        self.assertFalse(list(self.root.glob(".coat-*")))
        self.bridge.saveAsPath(QUrl.fromLocalFile(str(target)))
        self.bridge.confirmSaveAs(True)
        self.assertEqual(json.loads(target.read_text())["name"], "Test")
        self.assertEqual(self.bridge.currentPath, str(target))
        self.bridge.saveAsPath(QUrl.fromLocalFile(str(target)))
        self.assertEqual(len(requested), 4)
        self.bridge.confirmSaveAs(False)

    def test_new_save_as_atomic_no_clobber_and_failed_publish_cleanup(self):
        target = self.root / "new.cproc"
        real_link = os.link
        def competitor(source, destination):
            Path(destination).write_text("competing writer")
            return real_link(source, destination)
        with patch("coat_sim.core.api.os.link", side_effect=competitor):
            self.bridge.saveAsPath(QUrl.fromLocalFile(str(target)))
        self.assertEqual(target.read_text(), "competing writer")
        self.assertEqual(self.bridge.currentPath, str(self.path))
        self.assertFalse(list(self.root.glob(".coat-*")))
        another = self.root / "failed.cproc"
        with patch("coat_sim.core.api.os.fsync", side_effect=OSError("injected flush failure")):
            self.bridge.saveAsPath(QUrl.fromLocalFile(str(another)))
        self.assertFalse(another.exists())
        self.assertFalse(list(self.root.glob(".coat-*")))
        with patch("coat_sim.core.api.os.link", side_effect=OSError("unsupported filesystem")):
            self.bridge.saveAsPath(QUrl.fromLocalFile(str(another)))
        self.assertFalse(another.exists())
        self.assertFalse(list(self.root.glob(".coat-*")))
        self.bridge.saveAsPath(QUrl.fromLocalFile(str(another)))
        self.assertTrue(another.exists())
        self.assertEqual(self.bridge.currentPath, str(another))

    def test_qml_material_mask_choice_uses_row_not_signal_index(self):
        engine = QQmlApplicationEngine()
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/FilmStackDialog.qml")))
        self.assertFalse(component.isError(), component.errors())
        dialog = component.createWithInitialProperties({"editor": self.bridge})
        self.assertIsNotNone(dialog, component.errors())
        tabs = dialog.findChild(QObject, "filmStackTabs")
        tabs.setProperty("currentIndex", 1)
        self.app.processEvents()
        context = QQmlEngine.contextForObject(dialog)
        first_row, _ = QQmlExpression(context, dialog, "mappingRows.itemAt(0)").evaluate()
        second_row, _ = QQmlExpression(context, dialog, "mappingRows.itemAt(1)").evaluate()
        first = first_row.findChild(QObject, "materialMaskChoice0")
        second = second_row.findChild(QObject, "materialMaskChoice1")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        common, _ = QQmlExpression(context, dialog, "boards").evaluate()
        expected_choices = [None, None, [], [0], [1], [0, 1]]
        common_choices = [None, [], [0], [1], [0, 1]]
        # Exercise every common choice and all A/B override combinations on
        # actual QML ComboBox signals, including override reset and no-board.
        for common_choice in range(5):
            common.setProperty("currentIndex", common_choice)
            for first_choice in range(6):
                for second_choice in range(6):
                    first.setProperty("currentIndex", first_choice)
                    first.activated.emit(first_choice)
                    second.setProperty("currentIndex", second_choice)
                    second.activated.emit(second_choice)
                    expr = QQmlExpression(context, dialog, "buildParameters()")
                    raw, _ = expr.evaluate()
                    self.assertFalse(expr.hasError(), expr.error())
                    mapped = raw.toVariant()["sources"]
                    for row, choice in zip(mapped, (first_choice, second_choice)):
                        expected = common_choices[common_choice] if choice == 0 else expected_choices[choice]
                        self.assertEqual(row.get("mask_slots"), expected, (common_choice, first_choice, second_choice))
        first.setProperty("currentIndex", 3)  # material A -> board 1
        first.activated.emit(3)
        second.setProperty("currentIndex", 4)  # material B -> board 2
        second.activated.emit(4)
        expression = QQmlExpression(context, dialog, "buildParameters()")
        value, _ = expression.evaluate()
        self.assertFalse(expression.hasError(), expression.error())
        payload = value.toVariant() if hasattr(value, "toVariant") else value
        self.assertEqual(payload["total_thickness_nm"], 7000.)
        self.assertEqual([m["mask_slots"] for m in payload["sources"]], [[0], [1]])
        self.assertTrue(self.bridge.filmStack.build(payload, False), self.bridge.filmStack.state)
        steps = self.bridge.document["steps"]
        self.assertEqual(len(steps), 60)
        self.assertTrue(all(step["mask_slots"] == [step["source_slot"] - 1] for step in steps))
        self.assertTrue(all(step["rate_nm_s"] == 1. for step in steps))
        self.bridge.save()
        self.bridge.openPath(QUrl.fromLocalFile(str(self.path)))
        self.bridge.setStepPage(1)
        self.assertTrue(all(step["mask_slots"] == [step["source_slot"] - 1] for step in self.bridge.visibleSteps))
        dialog.deleteLater()
        self.app.processEvents()

    def test_process_qml_loads(self):
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("bridge", self.bridge)
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "coat_sim/ui/qml/ProcessWindow.qml")))
        self.assertFalse(component.isError(), component.errors())
        window = component.create()
        self.assertIsNotNone(window, component.errors())
        window.close()
        window.deleteLater()
        self.app.processEvents()


class OptimizationModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = Settings(grid_size=21, time_steps=4, carrier_rpm=0)

    def test_auto_design_with_no_installed_board_builds_independent_bundle(self):
        service, path = fixture(self.root, mask_count=0, source_count=1)
        before = path.read_bytes()
        result = service.optimize_masks(self.settings, path, options=options())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result["report"]["initialization"], "auto_design")
        self.assertEqual(result["report"]["selection_reason"], "from_scratch_verified_design")
        document = service.load("process", result["input_snapshot"])
        self.assertFalse(document.issues)
        self.assertEqual(document.data["steps"][0]["mask_slots"], [0])
        self.assertEqual(len(document.runtime["machine"]["masks"]), 1)
        self.assertFalse(result["report"]["best_is_original"])

    def test_fixed_mask_bytes_holes_and_physics_survive_new_second_board(self):
        service, path = fixture(self.root, mask_count=1, source_count=1)
        fixed_path = self.root / "mask0.cmsk"
        fixed_path.write_text(json.dumps(json.loads(fixed_path.read_text()), separators=(",", ":")) + "\n\n")
        fixed_bytes = fixed_path.read_bytes()
        config = options("fixed_plus_variable", 1)
        config.update(fixed_mask_slots=[0], variable_mode="auto_design")
        seen = []
        import coat_sim.core.mask_optimization as optimizer
        real_simulate = optimizer.simulate_process
        def capture(settings, runtime, **kwargs):
            seen.append(deepcopy(runtime))
            return real_simulate(settings, runtime, **kwargs)
        with patch.object(optimizer, "simulate_process", side_effect=capture):
            # Inspect the in-process algorithm; the public API now isolates it.
            result = service._optimize_masks_worker(self.settings, path, options=config)
        self.assertEqual((self.root / "mask0.cmsk").read_bytes(), fixed_bytes)
        self.assertEqual(result["machine"]["masks"][0]["profile"], service.load("mask", self.root / "mask0.cmsk").data)
        self.assertEqual(result["process"]["steps"][0]["mask_slots"], [0, 1])
        self.assertEqual(result["report"]["mask_slots"], [1])
        expected = service.load("mask", self.root / "mask0.cmsk").data
        self.assertTrue(all(runtime["machine"]["masks"][0]["profile"] == expected for runtime in seen))
        self.assertTrue(all(0 in runtime["profile"]["steps"][0]["mask_slots"] for runtime in seen))
        self.assertTrue(any(runtime["profile"]["steps"][0]["mask_slots"] == [0, 1] for runtime in seen))
        runtime = service.load("process", result["input_snapshot"]).runtime
        both = simulate_process(self.settings, runtime, backend="CPU")
        only_variable = deepcopy(runtime)
        only_variable["profile"]["steps"][0]["mask_slots"] = [1]
        one = simulate_process(self.settings, only_variable, backend="CPU")
        self.assertGreater(np.nanmax(abs(both["field_nm"] - one["field_nm"])), .01)

    def test_loaded_mode_starts_from_loaded_vertices_without_response_scan(self):
        service, path = fixture(self.root, mask_count=2, source_count=2)
        runtime = service.load("process", path).runtime
        config = options("optimize_loaded", 1)
        config["source_slot"] = 2
        config["mask_bounds"][0]["z_mm"] = 60.
        outputs = []
        result = optimize_correction_boards(self.settings, runtime["profile"], runtime["machine"], config, progress_callback=outputs.append)
        self.assertEqual(result.report["outline_parameterization"], "loaded_polygon_periodic_radial_deformation")
        self.assertEqual(result.report["response_scan"], [])
        self.assertNotIn("response", [item["stage"] for item in outputs])
        self.assertEqual(len(result.candidate_masks[0]["profile"]["outer"]), 4)
        self.assertIsNotNone(result.original_simulation)

    def test_auto_design_verifies_full_grid_and_estimates_added_geometry(self):
        service, path = fixture(self.root, mask_count=0, source_count=1)
        settings = replace(self.settings, grid_size=31)
        config = options()
        estimate = service.estimate_mask_optimization(settings, path, options=config)
        baseline_work = service.work_index(settings, process_path=path)
        self.assertGreater(estimate["work_index"], baseline_work * config["max_evaluations"])
        result = service.optimize_masks(settings, path, options=config)
        self.assertEqual(result["simulation"]["field_nm"].shape, (31, 31))
        self.assertGreater(result["report"]["verification_simulations"], 0)
        self.assertTrue(all(row["is_original"] is False for row in result["report"]["verification_candidates"]))

    def test_fixed_plus_loaded_preserves_both_activation_and_fixed_bytes(self):
        service, path = fixture(self.root, mask_count=2, source_count=1)
        fixed = (self.root / "mask0.cmsk").read_bytes()
        config = options("fixed_plus_variable", 1)
        config.update(fixed_mask_slots=[0], variable_mode="optimize_loaded", max_evaluations=4)
        config["mask_bounds"][0]["z_mm"] = 60.
        result = service.optimize_masks(self.settings, path, options=config)
        self.assertEqual(result["process"]["steps"][0]["mask_slots"], [0, 1])
        self.assertEqual(result["machine"]["masks"][0]["profile"], service.load("mask", self.root / "mask0.cmsk").data)
        self.assertEqual(result["report"]["initialization"], "optimize_loaded")
        self.assertEqual(len(result["machine"]["masks"][1]["profile"]["outer"]), 4)

    def test_cancellation_leaves_no_result_bundle(self):
        import threading
        service, path = fixture(self.root, mask_count=0, source_count=1)
        cancel = threading.Event()
        cancel.set()
        before = path.read_bytes()
        with self.assertRaises(InterruptedError):
            service.optimize_masks(self.settings, path, options=options(), cancel_event=cancel)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "data/tmp/optimizations").exists())

    def test_new_board_height_clearance_and_slot_constraints(self):
        service, path = fixture(self.root, mask_count=0, source_count=1)
        runtime = service.load("process", path).runtime
        for height in (0, 100, float("nan")):
            config = options()
            config["mask_bounds"][0]["z_mm"] = height
            with self.assertRaises(ValueError):
                prepare_optimization_runtime(runtime, config, self.settings)
        with self.assertRaisesRegex(ValueError, "依次安装"):
            prepare_optimization_runtime(runtime, options(slot=1), self.settings)
        with self.assertRaisesRegex(ValueError, "先载入"):
            prepare_optimization_runtime(runtime, options("optimize_loaded"), self.settings)


if __name__ == "__main__":
    unittest.main()
