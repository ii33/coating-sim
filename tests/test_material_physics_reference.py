"""Sourced ZnS arithmetic is a conditional source reference, never film growth."""
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest

from coat_sim.core.material_physics import (
    CLOSURES, K_B, MOLAR_MASS, N_A, R, evaluate_source_thermochemistry,
    reference_material_data, species_element_fluxes, species_outgoing_fluxes,
    validate_reference_data, validate_thermochemistry, vendor_pressure_pa,
    zns_equilibrium_pressures,
)
from tests.design_test_fixtures import copy_typed_examples


def config(**changes):
    value = {"model_id": "zns_ri8301_gas_v1", "temperature_K": 1200,
             "closure": "pressure_ratio", "pressure_ratio": 2,
             "alpha": {"Zn": 1, "S2": 1}, "transmission": {"Zn": 1, "S2": 1},
             "emitting_area_m2": 1e-4}
    value.update(changes)
    if value.get("closure") != "pressure_ratio":
        value.pop("pressure_ratio", None)
    return value


def source(c=None, **changes):
    value = {"kind": "rectangle", "length_mm": 40, "width_mm": 20,
             "device_type": "resistance_boat", "material": {"reference_data": reference_material_data()},
             "thermochemistry": config() if c is None else c}
    value.update(changes)
    return value


class ReferenceDatasetTests(unittest.TestCase):
    def test_reference_snapshot_is_complete_sourced_and_independent(self):
        data = reference_material_data()
        self.assertEqual(data["status"], "literature_reference")
        self.assertFalse(data["machine_calibrated"])
        records = {r["record_id"]: r for r in data["property_records"]}
        self.assertEqual(records["IRP-008"]["value"], 4.09)
        self.assertIn("not_film_density", records["IRP-008"]["conditions"]["state"])
        self.assertEqual(records["ZNS-CARD-ZRATIO"]["value"], .775)
        self.assertEqual(records["IRP-009"]["value"], [.8, 2.5])
        self.assertEqual(records["IRP-004"]["value"], 2.17)
        self.assertEqual(records["IRP-006"]["value"], 2.22)
        self.assertEqual(records["IRP-007"]["status"], "quarantined")
        self.assertIsNone(records["IRP-010"]["value"])
        sources = {s["id"]: s for s in data["sources"]}
        for record in data["property_records"]:
            if record["source_id"] is not None:
                self.assertIn(record["source_id"], sources)
                self.assertTrue(sources[record["source_id"]]["url"].startswith("https://"))
            self.assertIn("research_path", record)
            self.assertNotEqual(record["status"], "calibrated")
        self.assertEqual(data["models"]["zns_ri8301_gas_v1"]["pressure_standard_Pa"], 101325)
        self.assertTrue(any("2000" in str(row) for row in data["quarantined"]))
        self.assertTrue(any("Zn(l)" in str(row) for row in data["quarantined"]))
        data["property_records"][0]["value"] = 9
        self.assertEqual(reference_material_data()["property_records"][0]["value"], 2.44)

    def test_tampering_version_boolean_or_calibration_claim_rejected(self):
        valid = reference_material_data()
        copy = validate_reference_data(valid)
        self.assertEqual(copy, valid)
        self.assertIsNot(copy, valid)
        for field, value in (("schema_version", True), ("schema_version", 2),
                             ("status", "calibrated"), ("material_id", "TiO2")):
            changed = deepcopy(valid)
            changed[field] = value
            with self.assertRaises(ValueError):
                validate_reference_data(changed)
        changed = deepcopy(valid)
        changed["models"]["zns_ri8301_gas_v1"]["gibbs_formation_kcal_mol"]["a"] = -90
        with self.assertRaises(ValueError):
            validate_reference_data(changed)

    def test_actual_source_requires_its_reference_snapshot(self):
        s = source()
        self.assertEqual(evaluate_source_thermochemistry(s)["input_origin"], "source_snapshot")
        s["material"].pop("reference_data")
        s["material"]["name"] = "ZnS"
        with self.assertRaisesRegex(ValueError, "reference_data"):
            evaluate_source_thermochemistry(s)
        s["thermochemistry"] = None
        self.assertEqual(evaluate_source_thermochemistry(s)["status"], "disabled")


class PressureReferenceTests(unittest.TestCase):
    def test_vendor_units_anchors_inverse_temperature_interpolation(self):
        self.assertAlmostEqual(vendor_pressure_pa(1223.15), 1, places=12)
        self.assertAlmostEqual(vendor_pressure_pa(1343.15), 10, places=11)
        reciprocal_midpoint = 2 / (1 / 1223.15 + 1 / 1343.15)
        self.assertAlmostEqual(vendor_pressure_pa(reciprocal_midpoint), math.sqrt(10), places=11)
        for t in (1223.149999, 1343.150001, 1200):
            with self.assertRaises(ValueError):
                vendor_pressure_pa(t)

    def test_vendor_pressure_remains_undefined_and_never_hk(self):
        r = evaluate_source_thermochemistry({"model_id": "zns_vendor_pressure_v1", "temperature_K": 1223.15})
        self.assertEqual(r["status"], "reference_prediction")
        self.assertAlmostEqual(r["pressure"]["vendor_pressure_Pa"], 1)
        self.assertIsNone(r["pressure"]["total_pressure_Pa"])
        self.assertIsNone(r["pressure"]["partial_pressures_Pa"])
        self.assertIsNone(r["pre_transmission"])
        with self.assertRaisesRegex(ValueError, "HK"):
            species_outgoing_fluxes(r["pressure"], alpha={"Zn": 1, "S2": 1}, transmission={"Zn": 1, "S2": 1})

    def test_gas_gibbs_sign_standard_state_and_inclusive_domain(self):
        for t in (1180, 1210):
            p = zns_equilibrium_pressures(t, 2)
            self.assertGreater(p["K_dissociation"], 0)
        for t in (1179.99999, 1210.00001, 1000, 1223.15):
            with self.assertRaises(ValueError):
                zns_equilibrium_pressures(t, 2)
        p = zns_equilibrium_pressures(1200, 2)
        self.assertAlmostEqual(p["gibbs_formation_J_mol"], -146088.544, places=8)
        self.assertEqual(p["pressure_standard_Pa"], 101325)
        zn, s2 = (p["partial_pressures_Pa"][s] for s in ("Zn", "S2"))
        self.assertAlmostEqual((zn / 101325) * math.sqrt(s2 / 101325), p["K_dissociation"], places=18)
        self.assertAlmostEqual(zn / s2, 2)
        self.assertLess(p["K_dissociation"], 1)

    def test_1200_K_independently_reviewed_ideal_effusion_golden(self):
        r = evaluate_source_thermochemistry(config(closure="ideal_effusion", alpha=None, transmission=None))
        self.assertEqual(r["status"], "reference_prediction")
        p, f = r["pressure"], r["post_transmission"]
        expected = {"K_dissociation": 4.3757792766246254e-7,
                    "total_pressure_Pa": 11.03721167474913, "pressure_ratio": 2.0195550549780354,
                    "error_sensitivity_factor": 1.1992761481058911}
        # Audit arithmetic used rounded R=8.314462618; product uses exact N_A*k_B.
        for key, value in expected.items():
            self.assertTrue(math.isclose(p[key], value, rel_tol=1e-9), key)
        self.assertTrue(math.isclose(p["partial_pressures_Pa"]["Zn"], 7.381967284833737, rel_tol=1e-9))
        self.assertTrue(math.isclose(p["partial_pressures_Pa"]["S2"], 3.655244389915393, rel_tol=1e-9))
        self.assertTrue(math.isclose(f["particle_flux_m2_s"]["Zn"], 6.943891251145206e22, rel_tol=1e-9))
        self.assertTrue(math.isclose(f["particle_flux_m2_s"]["S2"], 3.4719456255726032e22, rel_tol=1e-9))
        self.assertTrue(math.isclose(f["mass_flux_kg_m2_s"], .011235419271594521, rel_tol=1e-9))
        self.assertAlmostEqual(f["s_to_zn_atomic_ratio"], 1)
        self.assertAlmostEqual(f["mass_rate_kg_s"], f["mass_flux_kg_m2_s"] * 1e-4)
        factor = p["error_sensitivity_factor"]
        self.assertEqual(p["total_pressure_sensitivity_Pa"], [p["total_pressure_Pa"] / factor, p["total_pressure_Pa"] * factor])
        self.assertIn("不是统计置信区间", p["uncertainty_note"])

    def test_sealed_pressure_and_effusion_closures_are_distinct(self):
        sealed = evaluate_source_thermochemistry(config(closure="sealed_stoichiometric_gas"))
        effusion = evaluate_source_thermochemistry(config(closure="ideal_effusion"))
        self.assertEqual(sealed["status"], "reference_prediction")
        self.assertEqual(sealed["pressure"]["pressure_ratio"], 2)
        self.assertNotEqual(sealed["pressure"]["pressure_ratio"], effusion["pressure"]["pressure_ratio"])
        self.assertIsNone(sealed["pre_transmission"])
        self.assertIsNone(sealed["post_transmission"])
        with self.assertRaises(ValueError):
            species_outgoing_fluxes(sealed["pressure"], alpha={"Zn": 1, "S2": 1}, transmission={"Zn": 1, "S2": 1})


class OutgoingFluxTests(unittest.TestCase):
    def test_particle_element_mass_conservation_and_si_hk_kernel(self):
        r = evaluate_source_thermochemistry(config())
        p, before = r["pressure"]["partial_pressures_Pa"], r["pre_transmission"]
        for s in ("Zn", "S2"):
            j = p[s] * math.sqrt(MOLAR_MASS[s] / (2 * math.pi * R * 1200))
            self.assertAlmostEqual(j, before["species_mass_flux_kg_m2_s"][s], places=14)
        element = before["element_flux_atoms_m2_s"]
        elemental_mass = (element["Zn"] * .06538 + element["S"] * .03206) / N_A
        self.assertAlmostEqual(elemental_mass, before["mass_flux_kg_m2_s"], places=14)
        self.assertEqual(element["Zn"], before["particle_flux_m2_s"]["Zn"])
        self.assertEqual(element["S"], 2 * before["particle_flux_m2_s"]["S2"])
        with self.assertRaises(ValueError):
            species_element_fluxes({"ZnS": 1e20})

    def test_species_transmission_loss_is_not_renormalized(self):
        r = evaluate_source_thermochemistry(config(alpha={"Zn": .7, "S2": .4}, transmission={"Zn": .25, "S2": .8}))
        before, after = r["pre_transmission"], r["post_transmission"]
        for s, tau in (("Zn", .25), ("S2", .8)):
            self.assertEqual(after["particle_flux_m2_s"][s], before["particle_flux_m2_s"][s] * tau)
            self.assertLessEqual(after["species_mass_flux_kg_m2_s"][s], before["species_mass_flux_kg_m2_s"][s])
        lost = before["mass_flux_kg_m2_s"] - after["mass_flux_kg_m2_s"]
        self.assertGreater(lost, 0)
        self.assertAlmostEqual(lost, sum(before["species_mass_flux_kg_m2_s"][s] * (1 - r["resolved_transmission"][s]) for s in ("Zn", "S2")))

    def test_general_congruent_closure_includes_alpha_and_tau(self):
        r = evaluate_source_thermochemistry(config(closure="congruent_outgoing",
            alpha={"Zn": .2, "S2": .8}, transmission={"Zn": .3, "S2": .7}))
        expected = 2 * (.8 * .7) / (.2 * .3) * math.sqrt(.06538 / .06412)
        self.assertAlmostEqual(r["pressure"]["pressure_ratio"], expected)
        self.assertAlmostEqual(r["post_transmission"]["s_to_zn_atomic_ratio"], 1)
        self.assertNotAlmostEqual(r["pre_transmission"]["s_to_zn_atomic_ratio"], 1)

    def test_unknown_alpha_and_transmission_never_become_one(self):
        c = config(alpha=None, transmission=None)
        normalized = validate_thermochemistry(c)
        self.assertEqual(normalized["alpha"], {"Zn": None, "S2": None})
        r = evaluate_source_thermochemistry(c)
        self.assertEqual(r["status"], "missing_inputs")
        self.assertIsNotNone(r["pressure"])
        self.assertIsNone(r["pre_transmission"])
        self.assertIsNone(r["post_transmission"])
        self.assertEqual(set(r["missing_inputs"]), {"alpha.Zn", "alpha.S2", "transmission.Zn", "transmission.S2"})
        r = evaluate_source_thermochemistry(config(transmission=None))
        self.assertIsNotNone(r["pre_transmission"])
        self.assertIsNone(r["post_transmission"])
        r = evaluate_source_thermochemistry(config(closure="congruent_outgoing", alpha=None))
        self.assertIsNone(r["pressure"])
        self.assertIn("alpha.Zn", r["missing_inputs"])

    def test_only_explicit_ideal_selection_resolves_unity(self):
        r = evaluate_source_thermochemistry(config(closure="ideal_effusion", alpha=None, transmission=None))
        self.assertEqual(r["resolved_alpha"], {"Zn": 1, "S2": 1})
        self.assertTrue(any("用户显式选择" in s for s in r["assumptions"]))
        for field in ("alpha", "transmission"):
            with self.assertRaises(ValueError):
                validate_thermochemistry(config(closure="ideal_effusion", **{field: {"Zn": .2}}))

    def test_zero_known_coefficients_are_zero_but_not_congruent_closure(self):
        r = evaluate_source_thermochemistry(config(alpha={"Zn": 0, "S2": 0}))
        self.assertEqual(r["status"], "reference_prediction")
        self.assertEqual(r["pre_transmission"]["mass_flux_kg_m2_s"], 0)
        self.assertEqual(r["post_transmission"]["mass_rate_kg_s"], 0)
        self.assertIsNone(r["post_transmission"]["s_to_zn_atomic_ratio"])
        for field in ("alpha", "transmission"):
            with self.assertRaises(ValueError):
                validate_thermochemistry(config(closure="congruent_outgoing", **{field: {"Zn": 0, "S2": 1}}))


class SourceAreaAndIsolationTests(unittest.TestCase):
    def test_explicit_area_scaling_without_changing_flux(self):
        a = evaluate_source_thermochemistry(config(emitting_area_m2=1e-4))
        b = evaluate_source_thermochemistry(config(emitting_area_m2=3e-4))
        self.assertEqual(a["post_transmission"]["mass_flux_kg_m2_s"], b["post_transmission"]["mass_flux_kg_m2_s"])
        self.assertAlmostEqual(b["post_transmission"]["mass_rate_kg_s"] / a["post_transmission"]["mass_rate_kg_s"], 3)
        missing = evaluate_source_thermochemistry(config(emitting_area_m2=None))
        self.assertEqual(missing["status"], "missing_inputs")
        self.assertIsNotNone(missing["post_transmission"]["mass_flux_kg_m2_s"])
        self.assertIsNone(missing["post_transmission"]["mass_rate_kg_s"])

    def test_geometric_area_mm2_to_m2_explicit_uniform_boundary_assumption(self):
        cases = [(dict(kind="rectangle", length_mm=40, width_mm=20), 800e-6),
                 (dict(kind="disk", radius_mm=10), math.pi * 100e-6),
                 (dict(kind="annulus", inner_radius_mm=5, outer_radius_mm=10), math.pi * 75e-6)]
        for geometry, area in cases:
            s = source(config(area_basis="source_geometry", emitting_area_m2=None), **geometry)
            r = evaluate_source_thermochemistry(s)
            self.assertAlmostEqual(r["area"]["value_m2"], area)
        for kind in ("point", "line"):
            r = evaluate_source_thermochemistry(source(config(area_basis="source_geometry", emitting_area_m2=None), kind=kind))
            self.assertEqual(r["status"], "missing_inputs")
            self.assertIn("source_geometry_area", r["missing_inputs"])

    def test_exit_area_is_not_charge_source_geometry(self):
        for closure in ("ideal_effusion", "congruent_outgoing"):
            with self.assertRaisesRegex(ValueError, "出口"):
                evaluate_source_thermochemistry(source(config(closure=closure, area_basis="source_geometry", emitting_area_m2=None)))
            r = evaluate_source_thermochemistry(source(config(closure=closure, emitting_area_m2=None)))
            self.assertEqual(r["status"], "missing_inputs")
            self.assertIsNone(r["post_transmission"]["mass_rate_kg_s"])

    def test_spatial_model_coefficients_do_not_become_thermochemistry(self):
        s = source(config(area_basis="source_geometry", emitting_area_m2=None))
        s["emission_model"] = {"kind": "resistance_boat", "active_fraction": .05,
                               "boat_mode": "covered", "apertures": [{"kind": "disk", "radius_mm": 1}]}
        a = evaluate_source_thermochemistry(s)
        s["device_type"] = "electron_beam"
        s["emission_model"] = {"kind": "electron_beam", "spot_sigma_mm": 2, "scan_width_mm": 80}
        b = evaluate_source_thermochemistry(s)
        self.assertEqual(a["pressure"], b["pressure"])
        self.assertEqual(a["area"], b["area"])
        self.assertEqual(a["post_transmission"], b["post_transmission"])

    def test_input_source_and_process_targets_are_never_mutated(self):
        s = source()
        s["material"].update(name="ZnS", id="example")
        process = {"sources": [s], "rate_nm_s": 2.5, "target_thickness_nm": 1000}
        before = deepcopy(process)
        r = evaluate_source_thermochemistry(s)
        self.assertEqual(process, before)
        self.assertNotIn("rate_nm_s", r)
        self.assertNotIn("thickness_nm", r)
        json.dumps(r, allow_nan=False)

    def test_disabled_missing_and_out_of_domain_are_explicit(self):
        self.assertEqual(evaluate_source_thermochemistry(None)["status"], "disabled")
        r = evaluate_source_thermochemistry({"model_id": "zns_ri8301_gas_v1"})
        self.assertEqual(r["missing_inputs"], ["temperature_K"])
        r = evaluate_source_thermochemistry({"model_id": "zns_ri8301_gas_v1", "temperature_K": 1200})
        self.assertEqual(r["missing_inputs"], ["closure"])
        r = evaluate_source_thermochemistry(config(temperature_K=1211))
        self.assertEqual(r["status"], "out_of_domain")
        self.assertIsNone(r["pressure"])

    def test_gibbs_sensitivity_propagates_to_flux_and_total_rate(self):
        r = evaluate_source_thermochemistry(config(transmission={"Zn": .4, "S2": .7}))
        factor = r["pressure"]["error_sensitivity_factor"]
        for key in ("pre_transmission", "post_transmission"):
            stage = r[key]
            j, q = stage["mass_flux_kg_m2_s"], stage["mass_rate_kg_s"]
            self.assertEqual(stage["mass_flux_sensitivity_kg_m2_s"], [j / factor, j * factor])
            self.assertEqual(stage["mass_rate_sensitivity_kg_s"], [q / factor, q * factor])
            self.assertIn("不是置信区间", stage["sensitivity_note"])
        no_area = evaluate_source_thermochemistry(config(emitting_area_m2=None))
        self.assertIsNone(no_area["post_transmission"]["mass_rate_sensitivity_kg_s"])

    def test_computed_overflow_never_escapes_as_nonfinite_json(self):
        with self.assertRaisesRegex(ValueError, "有限数值"):
            evaluate_source_thermochemistry(config(emitting_area_m2=1e308))
        with self.assertRaises(ValueError):
            species_element_fluxes({"Zn": 1e308, "S2": 1e308})
        with self.assertRaises(ValueError):
            evaluate_source_thermochemistry(source(config(area_basis="source_geometry", emitting_area_m2=None), length_mm=1e308))
        with self.assertRaises(ValueError):
            evaluate_source_thermochemistry(config(closure="congruent_outgoing", alpha={"Zn": 5e-324, "S2": 1}))

    def test_reference_report_does_not_change_spatial_or_process_solver(self):
        import numpy as np
        from coat_sim.core.api import CoatService
        from coat_sim.core.materials import builtin_material_profiles
        from coat_sim.core.source_profiles import validate_source_profile
        from coat_sim.core.simulator import Settings, simulate
        from coat_sim.core.process_simulator import simulate_process
        s = validate_source_profile({"kind": "disk", "radius_mm": 10,
            "material": builtin_material_profiles()[0], "thermochemistry": config()})
        plain = deepcopy(s)
        plain.pop("thermochemistry")
        settings = Settings(grid_size=21, time_steps=24)
        a = simulate(settings, source_profile=plain, backend="CPU")
        evaluate_source_thermochemistry(s)
        b = simulate(settings, source_profile=s, backend="CPU")
        np.testing.assert_array_equal(a["field"], b["field"])
        self.assertEqual(a["mean_raw"], b["mean_raw"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_typed_examples(root)
            runtime = deepcopy(CoatService(root).load(
                "process", "examples/process_example.cproc").runtime)
        runtime["profile"]["steps"] = [dict(source_slot=1, target_nm=120., rate_nm_s=2.5,
                                               target_mode="mean", mask_slots=[])]
        runtime["machine"]["sources"][0]["profile"] = plain
        a = simulate_process(settings, runtime, backend="CPU", fast_cycle=True)
        runtime["machine"]["sources"][0]["profile"] = s
        b = simulate_process(settings, runtime, backend="CPU", fast_cycle=True)
        np.testing.assert_array_equal(a["field_nm"], b["field_nm"])
        self.assertEqual(a["step_results"], b["step_results"])
        self.assertEqual(runtime["profile"]["steps"][0]["rate_nm_s"], 2.5)
        self.assertEqual(runtime["profile"]["steps"][0]["target_nm"], 120.)

    def test_invalid_nonfinite_units_and_incompatible_fields(self):
        for value in (True, -1, 0, float("inf"), float("nan")):
            for field in ("temperature_K", "emitting_area_m2", "pressure_ratio"):
                with self.assertRaises(ValueError):
                    validate_thermochemistry(config(**{field: value}))
        for value in (-.01, 1.01, float("nan"), True):
            with self.assertRaises(ValueError):
                validate_thermochemistry(config(alpha={"Zn": value}))
        for changes in ({"extra": 1}, {"alpha": {"ZnS": 1}}, {"area_basis": "source_geometry"}):
            with self.assertRaises(ValueError):
                validate_thermochemistry(config(**changes))


if __name__ == "__main__":
    unittest.main()
