"""Session ownership, atomic apply, staleness, and one-step undo contracts."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_design_session import FilmDesignEditSession


def _set(identifier, n, k=0.0):
    return {
        "id": identifier + "_set", "name": identifier + " dataset", "kind": "tabulated",
        "source": "unit-test data", "wavelength_unit": "nm",
        "wavelength_min": 450.0, "wavelength_max": 650.0,
        "wavelengths": [450.0, 550.0, 650.0], "n": [n, n, n],
        "k": None if k is None else [k, k, k],
    }


def _options():
    def mat(identifier, n, k=0.0):
        return {"id": identifier, "name": identifier,
                "optical_sets": [_set(identifier, n, k)]}
    return {
        "film": [mat("F", 1.38), mat("H", 2.1), mat("L", 1.45)],
        "medium": [mat("air", 1.0)],
        "substrate": [mat("glass", 1.5, None)],
    }


class _MemoryService:
    def __init__(self):
        self.options = _options()
        self.fail_save = False

    def new_design(self):
        return new_design()

    def material_options_many(self, _purposes):
        return deepcopy(self.options)

    def validate(self, design):
        return validate_design(design)

    def list_saved_designs(self):
        return []

    def list_expression_templates(self):
        return []

    def save(self, _path, design, *, overwrite=False):
        if self.fail_save:
            raise OSError("synthetic save failure")
        return validate_design(deepcopy(design))


class FilmDesignGeneratorSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = _MemoryService()
        self.session = FilmDesignEditSession(self.service)
        self.inputs = {
            "template_id": "ar_single_qw", "wavelength_nm": 550.0,
            "role_bindings": {"F": {"material_id": "F", "optical_set_id": "F_set"}},
            "incident_binding": {"material_id": "air", "optical_set_id": "air_set"},
            "substrate_binding": {"material_id": "glass", "optical_set_id": "glass_set"},
            "assume_lossless": True,
        }

    def tearDown(self):
        self.temp.cleanup()

    def preview(self):
        return self.session.preview_generated_design(self.inputs)

    def apply(self, preview):
        self.assertTrue(self.session.apply_generated_design(
            preview["candidate_design"], preview["base_design_revision"]))

    def test_preview_does_not_mutate_and_apply_is_one_reversible_dirty_edit(self):
        path = str(Path(self.temp.name) / "seed.cfilm")
        self.session._path = path
        before = self.session.snapshot()
        preview = self.preview()
        self.assertEqual(preview["candidate_design"]["substrate"]["sides"], "single")
        after_preview = self.session.snapshot()
        self.assertEqual(after_preview.design, before.design)
        self.assertEqual(after_preview.design_revision, before.design_revision)
        self.assertFalse(after_preview.dirty)

        self.apply(preview)
        applied = self.session.snapshot()
        self.assertEqual(applied.path, path)
        self.assertTrue(applied.dirty)
        self.assertEqual(applied.design_revision, before.design_revision + 1)
        self.assertTrue(self.session.can_undo_generated_design(applied.design_revision))
        self.assertTrue(self.session.undo_generated_design(applied.design_revision))
        restored = self.session.snapshot()
        self.assertEqual(restored.design, before.design)
        self.assertEqual(restored.path, path)
        self.assertFalse(restored.dirty)
        self.assertFalse(self.session.can_undo_generated_design(restored.design_revision))

    def test_stale_apply_and_subsequent_user_edit_invalidate_generator_undo(self):
        first = self.preview()
        self.assertTrue(self.session.set_design_value("name", "changed after preview"))
        self.assertFalse(self.session.apply_generated_design(
            first["candidate_design"], first["base_design_revision"]))

        fresh = self.preview()
        self.apply(fresh)
        applied = self.session.snapshot()
        self.assertTrue(self.session.can_undo_generated_design(applied.design_revision))
        self.session.set_design_value("name", "ordinary edit")
        changed = self.session.snapshot()
        self.assertFalse(self.session.can_undo_generated_design(changed.design_revision))
        self.assertFalse(self.session.undo_generated_design(changed.design_revision))

    def test_successful_save_invalidates_undo_but_failed_or_cancelled_save_keeps_it(self):
        path = Path(self.temp.name) / "saved.cfilm"
        path.write_text("existing", encoding="utf-8")
        initial = self.session.snapshot()
        preview = self.preview()
        self.apply(preview)
        applied = self.session.snapshot()
        self.assertFalse(self.session.save_file(str(path), overwrite=False))
        pending = self.session.snapshot()
        self.assertIsNotNone(pending.pending_confirmation)
        self.assertFalse(self.session.resolve_pending_action(False))
        self.assertTrue(self.session.can_undo_generated_design(applied.design_revision))

        self.service.fail_save = True
        self.assertFalse(self.session.save_file(str(path), overwrite=True))
        self.assertTrue(self.session.can_undo_generated_design(applied.design_revision))
        self.service.fail_save = False
        self.assertTrue(self.session.save_file(str(path), overwrite=True))
        saved = self.session.snapshot()
        self.assertFalse(saved.dirty)
        self.assertEqual(saved.path, str(path))
        self.assertFalse(self.session.can_undo_generated_design(saved.design_revision))
        self.assertFalse(self.session.undo_generated_design(saved.design_revision))
        self.assertNotEqual(initial.design, saved.design)

    def test_pending_discard_confirmation_blocks_preview_and_cancel_preserves_current_design(self):
        preview = self.preview()
        self.apply(preview)
        applied = self.session.snapshot()
        self.assertFalse(self.session.request_new_design())
        pending = self.session.snapshot()
        self.assertIsNotNone(pending.pending_confirmation)
        self.assertFalse(self.session.can_undo_generated_design(applied.design_revision))
        with self.assertRaisesRegex(ValueError, "确认"):
            self.preview()
        self.assertFalse(self.session.resolve_pending_action(False))
        after_cancel = self.session.snapshot()
        self.assertEqual(after_cancel.design, applied.design)
        self.assertTrue(self.session.can_undo_generated_design(applied.design_revision))


if __name__ == "__main__":
    unittest.main()
