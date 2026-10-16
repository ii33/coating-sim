"""Bounded real reference-mapping file/SQLite lifecycle in an exclusive QA tree."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / 'artifacts' / 'film_design_reference_library_validation'
sys.path.insert(0, str(ROOT))


class ReferenceStorageLifecycle(unittest.TestCase):
    def setUp(self):
        from coat_sim.core.film_design_service import FilmDesignService
        from coat_sim.ui.film_quick_controller import FilmDesignQuickController
        self.temp = tempfile.TemporaryDirectory(prefix='reference-case-')
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.tempdir).resolve()))
        self.service = FilmDesignService(self.root, restrict_to_root=True)
        self.service.materials = FilmDesignService(ROOT, restrict_to_root=False).materials
        self.controller = FilmDesignQuickController(self.root, service=self.service)
        self.assertTrue(self.controller.initializationReady)
        self.inputs = {
            'recipe_id': 'tfcalc_growing_ar_13', 'mapping_confirmed': True, 'assume_lossless': True,
            'role_bindings': {
                'H': {'material_id': 'builtin_research_Ge_film', 'optical_set_id': 'ri_ge_amotchkina_film'},
                'L': {'material_id': 'builtin_zns_literature_20261004', 'optical_set_id': 'ri_zns_amotchkina_n'}},
            'incident_binding': {'material_id': 'builtin_ideal_air', 'optical_set_id': 'ideal_air_n1_k0'},
            'substrate_binding': {'material_id': 'builtin_research_Ge_substrate',
                                  'optical_set_id': 'ri_ge_burnett2016_table1_vacuum'},
        }

    def tearDown(self):
        self.controller.deleteLater()
        self.temp.cleanup()

    def preview(self):
        before = self.controller.session.snapshot()
        result = self.controller.previewReferenceDesign(deepcopy(self.inputs))
        self.assertTrue(result.get('ok'), result)
        self.assertNotIn('candidate_design', result)
        self.assertEqual(self.controller.session.snapshot().design, before.design)
        self.assertEqual(self.controller.session.snapshot().design_revision, before.design_revision)
        self.assertEqual(result['layer_count'], 13)
        self.assertAlmostEqual(result['total_physical_thickness_nm'], 10263.39, places=8)
        self.assertTrue(all(row['delta_k'] > 0 for row in result['mapped_layers_incident_order'] if row['role'] == 'H'))
        return result

    def apply(self):
        result = self.preview()
        self.assertTrue(self.controller.applyGeneratedDesign(result['preview_token']))
        self.assertTrue(self.controller.dirty)
        self.assertTrue(self.controller.canUndoGeneratedDesign)
        return deepcopy(self.controller.currentDesign())

    def test_cfilm_cancel_failure_undo_and_explicit_success(self):
        path = self.root / 'existing.cfilm'
        self.assertTrue(self.controller.saveFilePath(str(path), False))
        original = self.controller.session.snapshot()
        original_bytes = path.read_bytes()
        applied = self.apply()
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(self.controller.session.snapshot().path, original.path)
        # Existing overwrite request is explicitly rejected; the mapped draft
        # remains undoable and the old file remains byte-for-byte intact.
        self.assertFalse(self.controller.saveFilePath(str(path), False))
        self.assertIsNotNone(self.controller.session.snapshot().pending_confirmation)
        self.assertFalse(self.controller.session.resolve_pending_action(False))
        self.assertEqual(self.controller.currentDesign(), applied)
        self.assertEqual(path.read_bytes(), original_bytes)
        occupied = self.root / 'occupied.cfilm'
        occupied.mkdir()
        marker = occupied / 'keep.txt'
        marker.write_text('keep', encoding='utf-8')
        self.assertFalse(self.controller.saveFilePath(str(occupied), True))
        self.assertTrue(self.controller.session.snapshot().error)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')
        self.assertEqual(self.controller.currentDesign(), applied)
        self.assertTrue(self.controller.canUndoGeneratedDesign)
        self.assertTrue(self.controller.undoGeneratedDesign())
        self.assertEqual(self.controller.currentDesign(), original.design)
        self.assertFalse(self.controller.dirty)
        self.assertEqual(self.service.load(path), original.design)
        mapped = self.apply()
        self.assertTrue(self.controller.saveFilePath(str(path), True))
        self.assertEqual(self.service.load(path), mapped)
        self.assertFalse(self.controller.dirty)
        self.assertFalse(self.controller.canUndoGeneratedDesign)
        self.assertFalse(self.controller.undoGeneratedDesign())
        self.assertEqual(len(mapped['front_layers']), 13)
        self.assertTrue(mapped['use_k'])
        self.assertIn('tfcalc_growing_ar_13', mapped['note'])
        self.assertEqual(json.loads(json.dumps(mapped, allow_nan=False)), mapped)

    def test_user_sqlite_requires_explicit_save_preserves_previous_record(self):
        self.assertTrue(self.controller.saveToLibrary())
        previous = deepcopy(self.controller.currentDesign())
        before_rows = deepcopy(self.service.list_saved_designs())
        self.assertEqual(len(before_rows), 1)
        self.assertEqual(self.service.load_saved_design(previous['id']), previous)
        discarded = self.preview()
        self.controller.clearGeneratorPreview()
        self.assertFalse(self.controller.applyGeneratedDesign(discarded['preview_token']))
        self.assertEqual(self.controller.currentDesign(), previous)
        self.assertEqual(self.service.list_saved_designs(), before_rows)
        mapped = self.apply()
        self.assertNotEqual(mapped['id'], previous['id'])
        self.assertEqual(self.service.list_saved_designs(), before_rows)
        self.assertEqual(self.service.load_saved_design(previous['id']), previous)
        self.assertTrue(self.controller.saveToLibrary())
        self.assertEqual(len(self.service.list_saved_designs()), 2)
        self.assertEqual(self.service.load_saved_design(previous['id']), previous)
        self.assertEqual(self.service.load_saved_design(mapped['id']), mapped)
        self.assertFalse(self.controller.canUndoGeneratedDesign)


def main():
    QA.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='reference-storage-', dir=QA) as directory:
        task_root = Path(directory).resolve()
        if not task_root.is_relative_to(QA.resolve()):
            raise RuntimeError('Exclusive test root escaped the reference QA tree')
        os.environ['TEMP'] = os.environ['TMP'] = str(task_root)
        tempfile.tempdir = str(task_root)
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        os.environ.setdefault('QT_QUICK_BACKEND', 'software')
        os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        report = {'started_utc': datetime.now(timezone.utc).isoformat(),
                  'execution_request': 'require_escalated bounded reference persistence validation',
                  'exclusive_temporary_root': str(task_root), 'path_checked_inside_qa': True,
                  'user_data_written': False, 'materials_source': 'read-only real material library',
                  'acl_permissions_changed': False, 'qt_platform': app.platformName()}
        with (QA / 'reference-storage-unittest.log').open('w', encoding='utf-8') as log:
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(
                unittest.defaultTestLoader.loadTestsFromTestCase(ReferenceStorageLifecycle))
        report.update(run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                      skipped=len(result.skipped), status='passed' if result.wasSuccessful() else 'failed',
                      finished_utc=datetime.now(timezone.utc).isoformat())
        (QA / 'reference-storage-validation.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
