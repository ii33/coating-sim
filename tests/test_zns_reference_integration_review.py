"""Independent bounded CPU review of literature-only ZnS integration.

These tests exercise product boundaries independently of the physics worker:
exact snapshots, typed/service round trips, and unchanged deposition outputs.
"""
from copy import deepcopy
from pathlib import Path
import json
import math
import tempfile
import unittest

import numpy as np
from pydantic import TypeAdapter

from coat_sim.core.api import CoatService
from coat_sim.core.input_snapshot import canonical_json_bytes, freeze_process, runtime_from_snapshot
from coat_sim.core.material_physics import (
    evaluate_source_thermochemistry, reference_material_data,
    validate_reference_data, species_outgoing_fluxes, vendor_pressure_pa,
)
from coat_sim.core.materials import builtin_material_profiles, list_materials, save_material
from coat_sim.core.process_simulator import simulate_process
from coat_sim.core.schemas import SourceDesign, design_data
from coat_sim.core.simulation_assets import (
    export_simulation_asset, read_simulation_asset, save_simulation_input_asset,
)
from coat_sim.core.simulator import Settings
from coat_sim.core.source_profiles import validate_source_profile
from tests.test_input_snapshot import fixture, rehash


def reference_source(**config_changes):
    config = dict(model_id="zns_ri8301_gas_v1", temperature_K=1200,
                  closure="ideal_effusion", emitting_area_m2=1e-4)
    config.update(config_changes)
    return validate_source_profile({"kind": "point", "device_type": "custom",
        "material": builtin_material_profiles()[0], "thermochemistry": config})


class ZnSIndependentPhysicsReview(unittest.TestCase):
    def test_three_golden_states_against_independent_audit(self):
        # RI8301_FORMULA_REVIEW.md uses R rounded to 8.314462618.
        states = [(1180, 2.355651273514038, 4.757367437190722, .007301859672942842),
                  (1200, 3.655244389915393, 7.381967284833737, .011235419271594521),
                  (1210, 4.528493753744367, 9.145542451810895, .013861957892579348)]
        for temperature, sulfur, zinc, mass in states:
            report = evaluate_source_thermochemistry(reference_source(temperature_K=temperature))
            self.assertEqual(report["status"], "reference_prediction")
            for observed, expected in [(report["pressure"]["partial_pressures_Pa"]["S2"], sulfur),
                                       (report["pressure"]["partial_pressures_Pa"]["Zn"], zinc),
                                       (report["post_transmission"]["mass_flux_kg_m2_s"], mass)]:
                self.assertTrue(math.isclose(observed, expected, rel_tol=1e-9))
            self.assertAlmostEqual(report["post_transmission"]["s_to_zn_atomic_ratio"], 1)
            json.dumps(report, allow_nan=False)

    def test_vendor_pressure_cannot_be_misread_as_total_or_species(self):
        report = evaluate_source_thermochemistry(reference_source(
            model_id="zns_vendor_pressure_v1", temperature_K=1280))
        pressure = report["pressure"]
        self.assertGreater(pressure["vendor_pressure_Pa"], 1)
        self.assertLess(pressure["vendor_pressure_Pa"], 10)
        self.assertIsNone(pressure["total_pressure_Pa"])
        self.assertIsNone(pressure["partial_pressures_Pa"])
        self.assertIsNone(report["pre_transmission"])
        self.assertIsNone(report["post_transmission"])
        with self.assertRaises(ValueError):
            species_outgoing_fluxes(pressure, alpha={"Zn": 1, "S2": 1},
                                     transmission={"Zn": 1, "S2": 1})

    def test_independent_si_interpolation_uses_reciprocal_temperature(self):
        for fraction in (.1, .25, .5, .9):
            temperature = 1 / ((1 - fraction) / 1223.15 + fraction / 1343.15)
            self.assertTrue(math.isclose(vendor_pressure_pa(temperature), 10**fraction, rel_tol=1e-12))

    def test_nonunit_coefficients_propagate_congruent_exit_not_surface_law(self):
        report = evaluate_source_thermochemistry(reference_source(
            closure="congruent_outgoing", alpha={"Zn": .6, "S2": .75},
            transmission={"Zn": .5, "S2": .8}))
        self.assertTrue(math.isclose(report["pressure"]["pressure_ratio"], 4.039110109956071, rel_tol=1e-12))
        self.assertAlmostEqual(report["post_transmission"]["s_to_zn_atomic_ratio"], 1)
        self.assertNotEqual(report["pre_transmission"]["s_to_zn_atomic_ratio"], 1)
        self.assertTrue(any("不是 RI8301 实测动力学" in item for item in report["assumptions"]))
        for key in ("mass_flux_kg_m2_s", "mass_rate_kg_s"):
            self.assertLess(report["post_transmission"][key], report["pre_transmission"][key])

    def test_unknowns_sealed_and_out_of_domain_do_not_leak_flux(self):
        for closure, changes in [(None, {}), ("pressure_ratio", {"pressure_ratio": 2}),
                                 ("congruent_outgoing", {}), ("sealed_stoichiometric_gas", {})]:
            report = evaluate_source_thermochemistry(reference_source(closure=closure, **changes))
            self.assertIsNone(report["pre_transmission"])
            self.assertIsNone(report["post_transmission"])
            if closure == "sealed_stoichiometric_gas":
                self.assertFalse(report["pressure"]["allowed_for_species_HK"])
            else:
                self.assertEqual(report["resolved_alpha"], {"Zn": None, "S2": None})
        for temperature in (1179.9999, 1210.0001, 948, 1180 - 1e-9, 1223.15):
            report = evaluate_source_thermochemistry(reference_source(temperature_K=temperature))
            self.assertEqual(report["status"], "out_of_domain")
            self.assertIsNone(report["pressure"])
            self.assertIsNone(report["post_transmission"])

    def test_exact_snapshot_comparison_rejects_changed_nesting_and_types(self):
        canonical = reference_material_data()
        for mutate in (
            lambda d: d["property_records"][0].update(value=2.441),
            lambda d: d["models"]["zns_ri8301_gas_v1"].update(pressure_standard_Pa=101325.0),
            lambda d: d["sources"][0].update(url="https://example.com/wrong"),
            lambda d: d["models"]["zns_vendor_pressure_v1"].update(allowed_for_species_HK=True),
            lambda d: d.update(machine_calibrated=0),
        ):
            changed = deepcopy(canonical); mutate(changed)
            with self.assertRaises(ValueError):
                validate_reference_data(changed)
        supplied = reference_source()
        supplied_before = deepcopy(supplied)
        report = evaluate_source_thermochemistry(supplied)
        report["provenance"]["sources"][0]["url"] = "edited report"
        self.assertEqual(supplied, supplied_before)
        self.assertEqual(reference_material_data(), canonical)


class ZnSIndependentIntegrationReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service, self.recipe = fixture(self.root, mask_count=0)
        self.path = self.root / "parts/source.csrc"

    def save_reference(self, **changes):
        data = reference_source(**changes)
        self.service.save("source", self.path, data, overwrite=True)
        return data

    def test_typed_json_service_file_report_roundtrip(self):
        source = self.save_reference()
        typed = TypeAdapter(SourceDesign).validate_python(source)
        self.assertEqual(design_data("source", typed), source)
        self.assertEqual(design_data("source", json.loads(typed.model_dump_json())), source)
        direct = self.service.source_material_reference(source)
        saved = self.service.source_material_reference(self.path)
        self.assertEqual(saved, direct)
        self.assertEqual(self.service.load("source", self.path).data, source)

    def test_builtin_lookup_does_not_touch_user_library_or_collision(self):
        folder = self.root / "materials"
        builtin = builtin_material_profiles()[0]
        user = deepcopy(builtin)
        user.update(name="User entry with colliding id", description="Do not overwrite")
        save_material(folder, user)
        before = (folder / (user["id"] + ".json")).read_bytes()
        for unused in range(3):
            retrieved = builtin_material_profiles()[0]
            retrieved["name"] = "Mutating a detached copy"
            self.assertNotEqual(builtin_material_profiles()[0]["name"], retrieved["name"])
        self.assertEqual((folder / (user["id"] + ".json")).read_bytes(), before)
        self.assertEqual(list_materials(folder), [user])

    def test_frozen_reference_and_inputs_survive_saved_source_replacement(self):
        source = self.save_reference()
        snapshot = freeze_process(self.service, self.recipe, self.root)
        before = canonical_json_bytes(snapshot)
        self.save_reference(temperature_K=1210, emitting_area_m2=2e-4)
        runtime = runtime_from_snapshot(snapshot)
        stored = runtime["machine"]["sources"][0]["profile"]
        self.assertEqual(stored["material"]["reference_data"], source["material"]["reference_data"])
        self.assertEqual(stored["thermochemistry"], source["thermochemistry"])
        self.assertEqual(self.service.source_material_reference(stored)["temperature_K"], 1200)
        stored["material"]["reference_data"]["sources"][0]["url"] = "edited copy"
        self.assertEqual(canonical_json_bytes(snapshot), before)

    def test_even_rehashed_tampered_reference_is_not_accepted(self):
        self.save_reference()
        snapshot = freeze_process(self.service, self.recipe, self.root)
        row = next(row for row in snapshot["files"] if row["kind"] == "source")
        row["data"]["material"]["reference_data"]["models"]["zns_ri8301_gas_v1"]["pressure_standard_Pa"] = 1e5
        rehash(row)
        with self.assertRaises(ValueError):
            runtime_from_snapshot(snapshot)

    def test_export_includes_exact_dataset_and_config_without_library(self):
        source = self.save_reference()
        snapshot = freeze_process(self.service, self.recipe, self.root)
        saved = save_simulation_input_asset(self.root, name="zns-reference-review", settings={},
            input_snapshot=snapshot, confirmed=True)
        content = read_simulation_asset(saved["asset_path"], detail="full")
        member = next(row["path"] for row in content["designs"] if row["kind"] == "source")
        destination = self.root / "export"
        exported = export_simulation_asset(saved["asset_path"], destination, members=[member])
        source_path = next(Path(row["path"]) for row in exported["designs"] if row["kind"] == "source")
        loaded = CoatService(destination).load("source", source_path).data
        self.assertEqual(loaded, source)
        self.assertFalse((destination / "data/materials").exists())
        self.assertEqual(self.service.source_material_reference(loaded)["status"], "reference_prediction")

    def test_reference_config_and_calculation_do_not_change_actual_simulation(self):
        source = self.save_reference()
        runtime = self.service.load("process", self.root / "recipe.cproc").runtime
        baseline_runtime = deepcopy(runtime)
        for placed in baseline_runtime["machine"]["sources"]:
            placed["profile"].pop("thermochemistry")
        settings = Settings(grid_size=21, time_steps=4, carrier_rpm=0,
                            source_x_mm=0, planet_offset_mm=0)
        original = deepcopy(runtime)
        self.service.source_material_reference(source)
        baseline = simulate_process(settings, baseline_runtime, backend="CPU")
        actual = simulate_process(settings, runtime, backend="CPU")
        np.testing.assert_array_equal(baseline["field_nm"], actual["field_nm"])
        self.assertEqual(baseline["step_results"], actual["step_results"])
        self.assertEqual(runtime, original)
        for observed, configured in zip(runtime["profile"]["steps"], self.recipe["steps"]):
            for key in ("target_nm", "rate_nm_s", "target_mode"):
                self.assertEqual(observed[key], configured[key])


if __name__ == "__main__":
    unittest.main()
