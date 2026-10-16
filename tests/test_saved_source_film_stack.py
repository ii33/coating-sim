"""Saved source material snapshots remain the authority for generated stacks.

The pre-recovery test blob is unavailable, so these are reconstructed behavioral
regressions from the material-library and film-stack contracts, not a byte-for-
byte restoration of the original module.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from coat_sim.core.api import CoatService
from coat_sim.core.film_stack import (build_film_stack, build_imported_film_stack,
                                      parse_film_stack_file)
from coat_sim.core.film_stack_schema import film_stack_spec_data
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import (builtin_material_profiles, default_material_profile,
                                     delete_material, save_material)
from coat_sim.core.process_profiles import source_selection_options
from coat_sim.core.source_profiles import validate_source_profile
from tests.test_input_snapshot import fixture

from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtWidgets import QApplication


def saved_source(name, identifier, rate):
    material = default_material_profile(name, identifier)
    material["resistance_model"]["defaults"]["reference_rate_nm_s"] = rate
    return validate_source_profile({
        "schema_version": 2,
        "kind": "rectangle",
        "length_mm": 20.0,
        "width_mm": 5.0,
        "device_type": "resistance_boat",
        "material": material,
    })


def source_runtime():
    return {
        "sources": [
            {"file": "saved-silica.json", "profile": saved_source("SiO2", "silica", 0.25)},
            {"file": "saved-titania.json", "profile": saved_source("TiO2", "titania", 0.5)},
        ],
        "masks": [{"profile": {}}, {"profile": {}}],
        "monitors": [{}],
    }


def stack_specification():
    return {
        "mode": "equal",
        "name": "Stack",
        "machine_file": "machine.cmac",
        "substrate_file": "substrate.csub",
        "sources": [
            {"source_slot": 1, "mask_slots": [0]},
            {"source_slot": 2, "mask_slots": [1]},
        ],
        "sequence": ["SiO2", "TiO2"],
        "layer_count": 4,
        "total_thickness_nm": 200.0,
    }


class SavedSourceCoreContractTests(unittest.TestCase):
    def test_generation_uses_detached_saved_source_names_rates_and_mask_slots(self):
        machine, spec = source_runtime(), stack_specification()
        before = deepcopy((machine, spec))
        generated = build_film_stack(spec, machine=machine)
        self.assertEqual((machine, spec), before)
        steps = generated["profile"]["steps"]
        self.assertEqual([step["source_slot"] for step in steps], [1, 2, 1, 2])
        self.assertEqual([step["rate_nm_s"] for step in steps], [0.25, 0.5, 0.25, 0.5])
        self.assertEqual([step["mask_slots"] for step in steps], [[0], [1], [0], [1]])
        self.assertNotIn("material", steps[0])
        self.assertNotIn("material_name", steps[0])
        binding = generated["summary"]["source_bindings"][0]
        self.assertEqual(binding["source_file"], "saved-silica.json")
        self.assertEqual(binding["material_id"], "silica")
        self.assertEqual(binding["material_name"], "SiO2")

    def test_explicit_source_aliases_label_layers_without_defining_materials(self):
        spec = stack_specification()
        spec["sources"][0].update(alias="H", rate_nm_s=0.125)
        spec["sources"][1]["alias"] = "L"
        spec["sequence"] = ["H", "L"]
        result = build_film_stack(spec, machine=source_runtime())
        self.assertEqual([row["rate_nm_s"] for row in result["profile"]["steps"]],
                         [0.125, 0.5, 0.125, 0.5])
        self.assertEqual(result["summary"]["source_bindings"][0]["material_name"], "SiO2")
        spec["sources"][0]["material"] = "invented material"
        with self.assertRaises(ValueError):
            build_film_stack(spec, machine=source_runtime())

    def test_missing_machine_or_invalid_saved_source_fails_without_invention(self):
        with self.assertRaisesRegex(ValueError, "已解析"):
            build_film_stack(stack_specification())
        machine = source_runtime()
        machine["sources"][0]["profile"] = None
        with self.assertRaisesRegex(ValueError, "有效"):
            build_film_stack(stack_specification(), machine=machine)
        options = source_selection_options(machine)
        self.assertEqual([row["slot"] for row in options], [1, 2])
        self.assertFalse(options[0]["valid"])

    def test_same_material_at_two_slots_requires_distinct_aliases(self):
        machine, spec = source_runtime(), stack_specification()
        machine["sources"][1]["profile"] = deepcopy(machine["sources"][0]["profile"])
        with self.assertRaisesRegex(ValueError, "重复"):
            build_film_stack(spec, machine=machine)
        spec["sources"][0]["alias"] = "left"
        spec["sources"][1]["alias"] = "right"
        spec["sequence"] = ["left", "right"]
        result = build_film_stack(spec, machine=machine)
        self.assertEqual([step["source_slot"] for step in result["profile"]["steps"]], [1, 2, 1, 2])
        self.assertEqual(result["summary"]["material_count"], 1)
        self.assertEqual(result["summary"]["source_count"], 2)

    def test_legacy_source_uses_its_filename_and_default_rate_at_its_exact_slot(self):
        machine = source_runtime()
        machine["sources"][0] = {
            "file": "old-source.json",
            "profile": {"schema_version": 2, "kind": "point", "source_exponent": 3.0},
        }
        spec = stack_specification()
        spec["sources"] = [{"source_slot": 1}]
        spec["sequence"] = ["old-source.json"]
        result = build_film_stack(spec, machine=machine)
        self.assertEqual(result["profile"]["steps"][0]["rate_nm_s"], 1.0)
        self.assertEqual(result["summary"]["source_bindings"][0]["material_name"], "")
        self.assertEqual(machine["sources"][0]["profile"]["source_exponent"], 3.0)

    def test_legacy_material_mapping_remains_an_explicit_alias(self):
        spec = stack_specification()
        spec.pop("sources")
        spec["materials"] = [{
            "material": "H", "source_slot": 1, "rate_nm_s": 0.2,
            "target_mode": "mean", "mask_slots": [],
        }]
        spec["sequence"] = ["H"]
        plain = build_film_stack(spec)
        resolved = build_film_stack(spec, machine=source_runtime())
        self.assertEqual(plain["profile"], resolved["profile"])
        self.assertEqual(resolved["summary"]["source_bindings"][0]["material_name"], "SiO2")
        self.assertEqual(resolved["profile"]["steps"][0]["mask_slots"], [])

    def test_mixed_bindings_fractional_slots_and_missing_mask_slots_are_rejected(self):
        spec = stack_specification()
        spec["materials"] = [{
            "material": "A", "source_slot": 1, "rate_nm_s": 1.0,
            "target_mode": "mean",
        }]
        with self.assertRaises(ValueError):
            film_stack_spec_data(spec)
        spec = stack_specification()
        spec["sources"][0]["source_slot"] = 1.5
        with self.assertRaises(ValueError):
            film_stack_spec_data(spec)
        machine = source_runtime()
        machine["masks"] = []
        with self.assertRaisesRegex(ValueError, "修正板"):
            build_film_stack(stack_specification(), machine=machine)

    def test_periodic_and_quarter_wave_modes_preserve_saved_slot_mapping(self):
        spec = stack_specification()
        for key in ("sequence", "layer_count", "total_thickness_nm"):
            spec.pop(key)
        spec.update(mode="periodic", repeat_count=3, period_layers=[
            {"material": "SiO2", "thickness_nm": 12.0},
            {"material": "TiO2", "thickness_nm": 8.0},
        ])
        periodic = build_film_stack(spec, machine=source_runtime())
        self.assertEqual(len(periodic["profile"]["steps"]), 6)
        self.assertEqual(periodic["summary"]["total_thickness_nm"], 60.0)
        spec = stack_specification()
        spec.pop("total_thickness_nm")
        spec.update(mode="quarter_wave", reference_wavelength_nm=600.0)
        spec["sources"][0]["refractive_index"] = 1.5
        spec["sources"][1]["refractive_index"] = 2.5
        quarter_wave = build_film_stack(spec, machine=source_runtime())
        self.assertEqual([step["target_nm"] for step in quarter_wave["profile"]["steps"]],
                         [100.0, 60.0, 100.0, 60.0])
        self.assertTrue(quarter_wave["summary"]["candidate_only"])

    def test_imported_air_to_substrate_angstrom_rows_reverse_with_source_and_mask_identity(self):
        table = parse_film_stack_file(
            "layers.csv", b"Material,Thickness\nH,100\nL,200\n")
        spec = stack_specification()
        for key in ("sequence", "layer_count", "total_thickness_nm"):
            spec.pop(key)
        spec.update(mode="import", columns={"material": "Material", "thickness": "Thickness"},
                    thickness_unit="angstrom", order="air_to_substrate")
        spec["sources"][0]["alias"] = "H"
        spec["sources"][1]["alias"] = "L"
        result = build_imported_film_stack(table, spec, machine=source_runtime())
        self.assertEqual([step["source_slot"] for step in result["profile"]["steps"]], [2, 1])
        self.assertEqual([step["target_nm"] for step in result["profile"]["steps"]], [20.0, 10.0])
        self.assertEqual([step["mask_slots"] for step in result["profile"]["steps"]], [[1], [0]])


class SavedSourceProcessBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from tests.test_process_optimization_modes import fixture as process_fixture
        from coat_sim.ui.process import ProcessBridge

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service, self.path = process_fixture(self.root, mask_count=2, source_count=2)

        installed = [
            ("saved-silica.csrc", saved_source("SiO2", "silica", 0.25)),
            ("saved-titania.csrc", saved_source("TiO2", "titania", 0.5)),
        ]
        for relative_path, profile in installed:
            self.service.save("source", self.root / relative_path, profile)
        machine_path = self.root / "machine.cmac"
        machine = self.service.load("machine", machine_path).data
        for placement, (relative_path, _profile) in zip(machine["sources"], installed):
            placement["file"] = relative_path
        self.service.save("machine", machine_path, machine, overwrite=True)
        self.bridge = ProcessBridge(self.service, str(self.path))
        self.addCleanup(self._release_bridge)

    def _release_bridge(self):
        self.bridge.detach_observers()
        self.bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_source_selection_updates_only_the_selected_process_row(self):
        self.assertTrue(self.bridge.valid, self.bridge.errors)
        initial = self.bridge.document["steps"]
        self.assertEqual(self.bridge.visibleSteps[0]["source_file"], "saved-silica.csrc")
        self.assertEqual(self.bridge.visibleSteps[0]["material_name"], "SiO2")
        self.assertEqual(self.bridge.visibleSteps[1]["source_file"], "saved-titania.csrc")
        self.assertEqual(self.bridge.visibleSteps[1]["material_name"], "TiO2")

        # Pick slot 1 for the second step. It must use that saved source's
        # embedded reference rate while preserving the row's board assignment.
        self.bridge.chooseStepSource(1, 1)
        selected = self.bridge.document["steps"]
        self.assertEqual(selected[0], initial[0])
        self.assertEqual(selected[1]["source_slot"], 1)
        self.assertEqual(selected[1]["rate_nm_s"], 0.25)
        self.assertEqual(selected[1]["mask_slots"], [1])
        self.assertEqual(self.bridge.visibleSteps[1]["material_name"], "SiO2")
        self.assertEqual(self.bridge.visibleSteps[1]["rate_angstrom_s"], 2.5)

        # Switching the first row to slot 2 must not be confused with the
        # second row or lose its distinct installed-mask identity.
        self.bridge.chooseStepSource(0, 2)
        selected = self.bridge.document["steps"]
        self.assertEqual(selected[0]["source_slot"], 2)
        self.assertEqual(selected[0]["rate_nm_s"], 0.5)
        self.assertEqual(selected[0]["mask_slots"], [0])
        self.assertEqual(selected[1]["source_slot"], 1)
        self.assertEqual(selected[1]["mask_slots"], [1])

        before_invalid_choice = deepcopy(selected[1])
        self.bridge.chooseStepSource(1, 999)
        self.assertEqual(self.bridge.document["steps"][1], before_invalid_choice)

        self.bridge.save()
        self.assertFalse(self.bridge.dirty)
        self.bridge.openPath(QUrl.fromLocalFile(str(self.path)))
        reopened = self.bridge.document["steps"]
        self.assertEqual(reopened[0]["source_slot"], 2)
        self.assertEqual(reopened[0]["rate_nm_s"], 0.5)
        self.assertEqual(reopened[0]["mask_slots"], [0])
        self.assertEqual(reopened[1]["source_slot"], 1)
        self.assertEqual(reopened[1]["rate_nm_s"], 0.25)
        self.assertEqual(reopened[1]["mask_slots"], [1])


class SavedSourceFilmStackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service, _recipe = fixture(self.root, mask_count=0)
        self.source_path = self.root / "parts" / "source.csrc"
        self.stack_path = self.root / "designs" / "saved-source-stack.cproc"
        self.stack_path.parent.mkdir(parents=True)

    def save_source(self, material):
        source = validate_source_profile({
            "schema_version": 2,
            "kind": "point",
            "device_type": "resistance_boat",
            "material": material,
        })
        document = self.service.save("source", self.source_path, source, overwrite=True)
        return document.data

    def build_stack(self, *, name="Saved source stack"):
        receipt = self.service.generate_film_stack({
            "mode": "periodic",
            "name": name,
            "machine_file": "../machines/machine.cmac",
            "substrate_file": "../parts/substrate.csub",
            "sources": [{"source_slot": 1, "mask_slots": []}],
            "period_layers": [
                {"material": "ZnS", "thickness_nm": 12.0},
                {"material": "ZnS", "thickness_nm": 18.0},
            ],
            "repeat_count": 2,
        }, self.stack_path)
        self.assertTrue(receipt["ok"])
        return self.service.load("process", self.stack_path)

    def assert_stack_bound_to_saved_source(self, stack, saved_source, rate_nm_s,
                                           targets=(12.0, 18.0, 12.0, 18.0)):
        self.assertTrue(stack.valid, stack.issues)
        self.assertEqual([step["source_slot"] for step in stack.data["steps"]],
                         [1] * len(targets))
        self.assertEqual([step["target_nm"] for step in stack.data["steps"]], list(targets))
        self.assertEqual([step["rate_nm_s"] for step in stack.data["steps"]],
                         [rate_nm_s] * len(targets))
        installed = stack.runtime["machine"]["sources"][0]["profile"]
        self.assertEqual(installed, saved_source)

    def test_catalog_override_and_restore_do_not_rewrite_saved_source_or_stack(self):
        catalog = MaterialLibrary(self.root)
        builtin = next(row for row in builtin_material_profiles()
                       if row["id"] == "builtin_zns_literature_20261004")
        saved_source = self.save_source(builtin)
        initial_rate = saved_source["emission_model"]["reference_rate_nm_s"]
        stack = self.build_stack()
        self.assert_stack_bound_to_saved_source(stack, saved_source, initial_rate)
        source_bytes = self.source_path.read_bytes()
        stack_bytes = self.stack_path.read_bytes()
        stack_data = deepcopy(stack.data)

        revised = deepcopy(builtin)
        revised["name"] = "Revised catalog label"
        revised["resistance_model"]["defaults"]["reference_rate_nm_s"] = 9.0
        catalog.save_builtin_override(revised)
        self.assertNotEqual(catalog.read(builtin["id"])["record"]["name"],
                            saved_source["material"]["name"])

        # A stack is generated from the installed saved-source snapshot, so a
        # library-only edit cannot silently change its defaults or its file.
        after_override_source = self.service.load("source", self.source_path).data
        after_override_stack = self.service.load("process", self.stack_path)
        self.assertEqual(after_override_source, saved_source)
        self.assertEqual(after_override_stack.data, stack_data)
        self.assert_stack_bound_to_saved_source(after_override_stack, saved_source, initial_rate)
        self.assertEqual(self.source_path.read_bytes(), source_bytes)
        self.assertEqual(self.stack_path.read_bytes(), stack_bytes)

        catalog.restore_builtin(builtin["id"])
        after_restore_source = self.service.load("source", self.source_path).data
        after_restore_stack = self.service.load("process", self.stack_path)
        self.assertEqual(after_restore_source, saved_source)
        self.assertEqual(after_restore_stack.data, stack_data)
        self.assert_stack_bound_to_saved_source(after_restore_stack, saved_source, initial_rate)
        self.assertEqual(self.source_path.read_bytes(), source_bytes)
        self.assertEqual(self.stack_path.read_bytes(), stack_bytes)

    def test_custom_material_deletion_does_not_invalidate_source_or_generated_stack(self):
        custom = default_material_profile("QA ZnS source", id="qa-zns-source")
        custom["resistance_model"]["defaults"].update(
            source_exponent=2.25, reference_rate_nm_s=0.4,
            rate_exponent_per_decade=0.3,
        )
        material_directory = self.root / "data" / "materials"
        saved_material = save_material(material_directory, custom)
        saved_source = self.save_source(saved_material)
        self.assertEqual(saved_source["emission_model"]["reference_rate_nm_s"], 0.4)
        # The stack alias is the saved source's snapshot name, not a live
        # material-library lookup.
        stack = self.service.generate_film_stack({
            "mode": "equal",
            "name": "Custom saved source stack",
            "machine_file": "../machines/machine.cmac",
            "substrate_file": "../parts/substrate.csub",
            "sources": [{"source_slot": 1, "mask_slots": []}],
            "sequence": ["QA ZnS source"],
            "layer_count": 3,
            "total_thickness_nm": 30.0,
        }, self.stack_path)
        self.assertTrue(stack["ok"])
        baseline_process = self.service.load("process", self.stack_path)
        self.assert_stack_bound_to_saved_source(
            baseline_process, saved_source, 0.4, targets=(10.0, 10.0, 10.0))
        source_bytes = self.source_path.read_bytes()
        stack_bytes = self.stack_path.read_bytes()

        delete_material(material_directory, saved_material["id"])
        loaded_source = self.service.load("source", self.source_path).data
        loaded_process = self.service.load("process", self.stack_path)
        self.assertEqual(loaded_source, saved_source)
        self.assertEqual(loaded_process.data, baseline_process.data)
        self.assert_stack_bound_to_saved_source(
            loaded_process, saved_source, 0.4, targets=(10.0, 10.0, 10.0))
        self.assertEqual(self.source_path.read_bytes(), source_bytes)
        self.assertEqual(self.stack_path.read_bytes(), stack_bytes)


if __name__ == "__main__":
    unittest.main()
