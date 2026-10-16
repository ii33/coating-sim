"""Limited retry of the blocked film file regressions; no user data is touched."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "artifacts" / "film_design_generator_validation"
sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]

TESTS = ["test_film_quick_controller.FilmDesignQuickControllerTests." + name for name in (
    "test_cfilm_and_sqlite_paths_use_the_existing_service_contract",
    "test_cfilm_thickness_round_trip_keeps_schema_unit",
    "test_deleting_default_symbol_stays_deleted_after_save_and_reload",
    "test_legacy_constant_medium_survives_cfilm_open_and_save",
    "test_opening_empty_symbol_table_does_not_add_default_symbols",
    "test_physical_nm_rounds_for_display_and_keeps_cfilm_precision",
    "test_qml_expression_draft_debounces_and_survives_cfilm_save_reload",
)] + [
    "test_film_design_file_format.FilmDesignFileFormatTests.test_cfilm_round_trip_keeps_schema_v1_json_payload",
    "test_film_design_service.FilmServiceTests.test_persistence_and_csv",
]


class RealGeneratedStorageLifecycle(unittest.TestCase):
    def test_real_save_cancel_failure_undo_and_successful_save_invalidation(self):
        from coat_sim.core.film_design_service import FilmDesignService
        from coat_sim.core.film_design_session import FilmDesignEditSession
        with tempfile.TemporaryDirectory(prefix="generated-storage-") as directory:
            root = Path(directory)
            self.assertTrue(root.resolve().is_relative_to(Path(tempfile.tempdir).resolve()))
            service = FilmDesignService(root, restrict_to_root=True)
            service.materials = FilmDesignService(ROOT, restrict_to_root=False).materials
            session = FilmDesignEditSession(service)
            path = root / "seed.cfilm"
            self.assertTrue(session.save_file(str(path), overwrite=False), session.snapshot().error)
            original = session.snapshot()
            original_bytes = path.read_bytes()
            inputs = {
                "template_id": "hr_hl_qw", "wavelength_nm": 550.0, "pairs": 2,
                "role_bindings": {
                    "H": {"material_id": "builtin_zns_literature_20261004",
                          "optical_set_id": "ri_zns_amotchkina_n"},
                    "L": {"material_id": "builtin_research_YbF3_film",
                          "optical_set_id": "ri_ybf3_amotchkina_n"}},
                "incident_binding": {"material_id": "builtin_ideal_air",
                                     "optical_set_id": "ideal_air_n1_k0"},
                "substrate_binding": {"material_id": "builtin_research_fused_silica_method_demonstrator",
                                      "optical_set_id": "ri_sio2_malitson"},
                "assume_lossless": True,
            }
            def apply():
                preview = session.preview_generated_design(inputs)
                self.assertTrue(session.apply_generated_design(
                    preview["candidate_design"], preview["base_design_revision"]))
                return session.snapshot()
            applied = apply()
            self.assertEqual(applied.path, str(path))
            self.assertTrue(applied.dirty)
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertFalse(session.save_file(str(path), overwrite=False))
            self.assertIsNotNone(session.snapshot().pending_confirmation)
            self.assertFalse(session.resolve_pending_action(False))
            self.assertEqual(session.snapshot().design, applied.design)
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertTrue(session.can_undo_generated_design(applied.design_revision))
            # A directory occupying a file target is a real I/O failure in this
            # test-owned tree, without changing ACLs or mocking the service.
            occupied = root / "occupied.cfilm"
            occupied.mkdir()
            marker = occupied / "marker.txt"
            marker.write_text("keep", encoding="utf-8")
            self.assertFalse(session.save_file(str(occupied), overwrite=True))
            self.assertTrue(session.snapshot().error)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertEqual(session.snapshot().design, applied.design)
            self.assertTrue(session.can_undo_generated_design(applied.design_revision))
            self.assertTrue(session.undo_generated_design(applied.design_revision))
            restored = session.snapshot()
            self.assertEqual(restored.design, original.design)
            self.assertEqual(restored.path, original.path)
            self.assertFalse(restored.dirty)
            self.assertEqual(service.load(path), restored.design)
            self.assertEqual(path.read_bytes(), original_bytes)
            saved_candidate = deepcopy(apply().design)
            self.assertTrue(session.save_current(), session.snapshot().error)
            saved = session.snapshot()
            self.assertFalse(saved.dirty)
            self.assertEqual(service.load(path), saved_candidate)
            self.assertEqual(len(saved.design["front_layers"]), 4)
            self.assertFalse(session.can_undo_generated_design(saved.design_revision))
            self.assertFalse(session.undo_generated_design(saved.design_revision))
            self.assertEqual(service.load(path), saved_candidate)


def main():
    QA.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="storage-escalated-", dir=QA) as directory:
        task_root = Path(directory).resolve()
        if not task_root.is_relative_to(QA.resolve()):
            raise RuntimeError("Test temporary directory escaped the intended QA tree")
        os.environ["TEMP"] = str(task_root)
        os.environ["TMP"] = str(task_root)
        tempfile.tempdir = str(task_root)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        families = []
        for font in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/segoeui.ttf")):
            font_id = QFontDatabase.addApplicationFont(str(font))
            if font_id >= 0:
                families.extend(QFontDatabase.applicationFontFamilies(font_id))
        if families:
            app.setFont(QFont(families[0], 10))
        report = {"timestamp_utc_started": datetime.now(timezone.utc).isoformat(),
                  "fresh_exclusive_temporary_root": str(task_root),
                  "execution_request": "require_escalated limited storage regressions",
                  "tests": TESTS + ["RealGeneratedStorageLifecycle.test_real_save_cancel_failure_undo_and_successful_save_invalidation"],
                  "user_data_accessed_or_modified": False,
                  "loaded_font_families": families, "qt_platform": app.platformName()}
        try:
            source = task_root / "hardlink-source.txt"
            target = task_root / "hardlink-copy.txt"
            source.write_bytes(b"isolated film storage probe")
            os.link(source, target)
            assert target.read_bytes() == source.read_bytes()
            report["hardlink_probe"] = "passed"
            suite = unittest.TestSuite([
                unittest.defaultTestLoader.loadTestsFromNames(TESTS),
                unittest.defaultTestLoader.loadTestsFromTestCase(RealGeneratedStorageLifecycle)])
            with (QA / "storage-escalated-regressions.log").open("w", encoding="utf-8") as log:
                result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
            report.update(run=result.testsRun, failures=len(result.failures),
                          errors=len(result.errors), skipped=len(result.skipped))
            passed = result.wasSuccessful() and result.testsRun == 10 and not result.skipped
            report["status"] = "passed" if passed else "failed"
        except Exception as error:
            report.update(status="execution_error", error=f"{type(error).__name__}: {error}")
        report["timestamp_utc_finished"] = datetime.now(timezone.utc).isoformat()
        report["temporary_cleanup"] = "context manager removes only this freshly-created tree"
        (QA / "storage-escalated-validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
