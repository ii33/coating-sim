"""Read-only independent normal-incidence template oracle; writes QA evidence only.

Run from the project root with its Python runtime. Characteristic matrices here
are independent of the production generator and optical scattering recursion.
The constants are mathematical fixtures, never material-library replacements.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from copy import deepcopy

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from coat_sim.core.film_optics import coherent_rta
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.optical_constants import prepare_optical_set


def characteristic_rta(wave, indices, thicknesses):
    matrix = np.eye(2, dtype=complex)
    for n, d in zip(indices[1:-1], thicknesses, strict=True):
        phase = 2.0 * math.pi * n * d / wave
        c, s = math.cos(phase), math.sin(phase)
        matrix = matrix @ np.array([[c, -1j * s / n], [-1j * n * s, c]])
    a = matrix[0, 0] + matrix[0, 1] * indices[-1]
    b = matrix[1, 0] + matrix[1, 1] * indices[-1]
    denominator = indices[0] * a + b
    r = abs((indices[0] * a - b) / denominator) ** 2
    t = indices[-1] / indices[0] * abs(2 * indices[0] / denominator) ** 2
    return {"R": float(r), "T": float(t), "A": float(1 - r - t)}


def oracle_checks():
    wave = 550.0
    h, l, f = 2.1, 1.45, 1.38
    fixtures = [
        ("AR_Q", [1, f, 1.5], [wave/(4*f)],
         ((1.5-f*f)/(1.5+f*f))**2),
        ("AR_Q_ideal", [1, math.sqrt(1.5), 1.5],
         [wave/(4*math.sqrt(1.5))], 0.0),
        ("AR_QQ", [1, 1.38, 1.63, 1.5],
         [wave/(4*1.38), wave/(4*1.63)],
         ((1-1.5*(1.38/1.63)**2)/(1+1.5*(1.38/1.63)**2))**2),
        ("AR_QHQ", [1, 1.38, 2.2, 1.63, 1.5],
         [wave/(4*1.38), wave/(2*2.2), wave/(4*1.63)],
         ((1-1.5*(1.38/1.63)**2)/(1+1.5*(1.38/1.63)**2))**2),
    ]
    for pairs in (1, 6, 8):
        ns = 1.5
        layers = [h, l] * pairs
        y = ns * (h/l)**(2*pairs)
        fixtures.append((f"HR_{pairs}", [1] + layers + [ns],
                         [wave/(4*n) for n in layers], ((1-y)/(1+y))**2))
    for ns in (1.0, 1.5):
        layers = [h, l]*4 + [h] + [l, h]*4
        d = [wave/(4*n) for n in layers]
        d[8] = wave/(2*h)
        fixtures.append((f"FP_4_ns_{ns}", [1] + layers + [ns], d,
                         ((1-ns)/(1+ns))**2))
    results, residual = [], 0.0
    for label, n, d, expected_r in fixtures:
        matrix = characteristic_rta(wave, n, d)
        computed = coherent_rta(wave, n, d)
        errors = [abs(computed[k]-matrix[k]) for k in ("R", "T", "A")]
        residual = max(residual, *errors, abs(computed["R"]-expected_r))
        assert max(errors) < 2e-11, (label, matrix, computed)
        assert abs(computed["R"]-expected_r) < 2e-11, (label, computed)
        assert abs(computed["R"]+computed["T"]-1) < 2e-11
        results.append({"name": label, "layer_count": len(d),
                        "total_thickness_nm": sum(d), "R": computed["R"],
                        "T": computed["T"], "A": computed["A"],
                        "analytic_R": expected_r, "matrix_max_error": max(errors)})
    # Fixed physical thickness over a spectral sweep, including off-center data.
    sweep_checks = 0
    for label, n, d, _ in fixtures:
        waves = np.linspace(430.0, 690.0, 39)
        batch = coherent_rta(waves, n, d)
        for index, wavelength in enumerate(waves):
            expected = characteristic_rta(float(wavelength), n, d)
            for key in ("R", "T", "A"):
                error = abs(batch[key][index] - expected[key])
                residual = max(residual, error)
                assert error < 2e-10, (label, wavelength, key, error)
            sweep_checks += 1
    # Arbitrary incident and substrate ports can initially match
    # before becoming reflective: increasing pair count is not globally monotone.
    nonmonotone = [((3.0 - 1.0*(1.5/1.0)**(2*p)) /
                   (3.0 + 1.0*(1.5/1.0)**(2*p)))**2 for p in (0, 1, 2)]
    assert nonmonotone[1] < nonmonotone[0] and nonmonotone[2] > nonmonotone[1]
    return {"center_fixtures": results, "fixed_thickness_sweep_points": sweep_checks,
            "maximum_absolute_residual": residual,
            "hr_arbitrary_port_monotonicity_counterexample": nonmonotone,
            "scope": "Independent constant-index mathematics; no measured performance claim."}


def real_material_oracle():
    service = FilmDesignService(ROOT)
    catalog = service.material_options_many(("film", "substrate", "medium"))
    records = {row["id"]: row for rows in catalog.values() for row in rows}
    selected = {
        "H": ("builtin_zns_literature_20261004", "ri_zns_amotchkina_n"),
        "L": ("builtin_research_YbF3_film", "ri_ybf3_amotchkina_n"),
        "S": ("builtin_research_fused_silica_method_demonstrator", "ri_sio2_malitson"),
        "I": ("builtin_ideal_air", "ideal_air_n1_k0"),
    }
    evaluators, trace = {}, {}
    for role, (material_id, dataset_id) in selected.items():
        record = next(item for item in records[material_id]["optical_sets"]
                      if item["id"] == dataset_id)
        evaluator = prepare_optical_set(record)
        n, k, _ = evaluator(550, "nm", extrapolation_policy="error", include_k=True)
        assert math.isfinite(n) and n > 0
        assert k is None or abs(k) <= 1e-9
        evaluators[role] = evaluator
        trace[role] = {"material_id": material_id, "optical_set_id": dataset_id,
                       "n_at_550nm": n, "k_at_550nm": k,
                       "source_wavelength_unit": record["wavelength_unit"],
                       "source_range": [record["wavelength_min"], record["wavelength_max"]]}
    specs = {
        "AR_Q": [("L", 1)],
        "AR_QQ": [("L", 1), ("H", 1)],
        "AR_QHQ": [("L", 1), ("H", 2), ("H", 1)],
        "HR_8": [("H", 1), ("L", 1)]*8,
        "FP_8": [("H", 1), ("L", 1)]*8+[("H", 2)]+[("L", 1), ("H", 1)]*8,
    }
    results, max_residual = [], 0.0
    for label, spec in specs.items():
        d = [q*550.0/(4*trace[role]["n_at_550nm"]) for role, q in spec]
        assert all(value > 0 and math.isfinite(value) for value in d)
        n0, ns = trace["I"]["n_at_550nm"], trace["S"]["n_at_550nm"]
        if label == "AR_Q":
            f = trace["L"]["n_at_550nm"]
            expected_r = ((n0*ns-f*f)/(n0*ns+f*f))**2
        elif label in ("AR_QQ", "AR_QHQ"):
            y = ns*(trace["L"]["n_at_550nm"]/trace["H"]["n_at_550nm"])**2
            expected_r = ((n0-y)/(n0+y))**2
        elif label == "HR_8":
            y = ns*(trace["H"]["n_at_550nm"]/trace["L"]["n_at_550nm"])**16
            expected_r = ((n0-y)/(n0+y))**2
        else:
            expected_r = ((n0-ns)/(n0+ns))**2
        center = None
        for wavelength in np.linspace(450.0, 650.0, 41):
            indices = [evaluators["I"](float(wavelength), "nm")[0]]
            indices += [evaluators[role](float(wavelength), "nm")[0] for role, _ in spec]
            indices += [evaluators["S"](float(wavelength), "nm")[0]]
            # Explicit unknown-k lossless fixture; production must ask the user.
            expected = characteristic_rta(float(wavelength), indices, d)
            actual = coherent_rta(float(wavelength), indices, d)
            error = max(abs(actual[key]-expected[key]) for key in ("R", "T", "A"))
            max_residual = max(max_residual, error)
            assert error < 2e-9, (label, wavelength, error)
            assert abs(actual["R"]+actual["T"]-1) < 2e-10
            if wavelength == 550.0:
                assert abs(actual["R"]-expected_r) < 2e-10
                center = {key: actual[key] for key in ("R", "T", "A")}
        assert center is not None
        results.append({"name": label, "layer_count": len(d), "spec": spec,
                        "thicknesses_nm": d, "total_thickness_nm": sum(d),
                        "center": center, "analytic_center_R": expected_r})
    assert results[3]["center"]["R"] > ((n0-ns)/(n0+ns))**2
    return {"materials": trace, "templates": results,
            "fixed_thickness_sweep_points": 205,
            "maximum_absolute_residual": max_residual,
            "assume_unknown_k_lossless": True,
            "scope": "Independent oracle using actual selected datasets, not measured coating performance."}


def production_generator_checks():
    from coat_sim.core.film_design import validate_design
    from coat_sim.core.film_design_templates import build_preview, template_catalog, TemplateGenerationError
    service = FilmDesignService(ROOT)
    options = service.material_options_many(("film", "substrate", "medium"))
    high = {"material_id": "builtin_zns_literature_20261004", "optical_set_id": "ri_zns_amotchkina_n"}
    low = {"material_id": "builtin_research_YbF3_film", "optical_set_id": "ri_ybf3_amotchkina_n"}
    substrate = {"material_id": "builtin_research_fused_silica_method_demonstrator", "optical_set_id": "ri_sio2_malitson"}
    incident = {"material_id": "builtin_ideal_air", "optical_set_id": "ideal_air_n1_k0"}
    bindings = {
        "ar_single_qw": {"F": low},
        "ar_vcoat_qq": {"F1": low, "F2": high},
        "ar_qhq_seed": {"F1": low, "F2": high, "F3": high},
        "hr_hl_qw": {"H": high, "L": low},
        "fp_halfwave_defect": {"H": high, "L": low},
    }
    base = service.new_design()
    base["name"] = "Independent existing double-face design"
    base["note"] = "Existing note belongs to the unchanged source design."
    base["substrate"]["sides"] = "double"
    base["substrate"]["thickness"] = {"value": 2.75, "unit": "mm"}
    base["back_layers"] = [{"id": "existing-back-layer", **high,
                            "thickness": {"value": 87.0, "unit": "nm"}}]
    base = service.validate(base)
    base_before = deepcopy(base)
    expected_counts = [1, 2, 3, 16, 33]
    results = []
    max_error = 0.0
    for (template_id, role_bindings), expected_count in zip(bindings.items(), expected_counts, strict=True):
        inputs = {"template_id": template_id, "wavelength_nm": 550.0,
                  "pairs": 8, "role_bindings": role_bindings,
                  "substrate_binding": substrate, "incident_binding": incident,
                  "assume_lossless": True}
        frozen_inputs = deepcopy(inputs)
        preview = build_preview(inputs=inputs, base_design=base, material_options=options)
        assert base == base_before and inputs == frozen_inputs
        design = preview["candidate_design"]
        assert preview["layer_count"] == expected_count
        assert len(design["front_layers"]) == expected_count and not design["back_layers"]
        assert design["substrate"]["sides"] == "single"
        assert design["substrate"]["thickness"] == base["substrate"]["thickness"]
        assert design["note"] and "single" in design["note"]
        assert design["layer_order"] == "substrate_outward"
        assert all(layer["thickness"]["unit"] == "nm" for layer in design["front_layers"])
        round_trip = validate_design(json.loads(json.dumps(design, allow_nan=False)))
        assert round_trip == design
        incident_layers = list(reversed(design["front_layers"]))
        for layer, display in zip(incident_layers, preview["layers_incident_order"], strict=True):
            assert layer["optical_set_id"] == display["dataset_id"]
            expected_d = display["qw_multiplier"]*550/(4*display["n_at_wavelength"])
            assert abs(layer["thickness"]["value"] - expected_d) < 2e-11
        candidate_before = deepcopy(design)
        spectrum = service.simulate(design, {"wavelength_min_nm": 450,
                   "wavelength_max_nm": 650, "samples": 41,
                   "angle_deg": 0, "polarization": "unpolarized",
                   "extrapolation_policy": "error"})
        assert design == candidate_before
        records = {row["id"]: row for rows in options.values() for row in rows}
        def n_at(binding, wave):
            source = next(row for row in records[binding["material_id"]]["optical_sets"]
                          if row["id"] == binding["optical_set_id"])
            return prepare_optical_set(source)(wave, "nm", extrapolation_policy="error")[0]
        d = [layer["thickness"]["value"] for layer in incident_layers]
        for index, wave in enumerate(spectrum["wavelength_nm"]):
            n = [n_at(incident, wave)] + [n_at(layer, wave) for layer in incident_layers]+[n_at(substrate, wave)]
            oracle = characteristic_rta(wave, n, d)
            for key in ("R", "T", "A"):
                error = abs(spectrum[key][index]-oracle[key])
                max_error = max(max_error, error)
                assert error < 2e-9, (template_id, wave, key, error)
            assert abs(spectrum["R"][index]+spectrum["T"][index]-1) < 2e-10
        center = 20
        expected_r = preview["reference_results"]["center_reflectance"]
        assert abs(spectrum["R"][center]-expected_r) < 2e-9
        qualitative = "center formula and energy conservation"
        if template_id == "hr_hl_qw":
            assert spectrum["R"][center] > 0.99
            qualitative = "high center reflectance for this explicit real-material fixture"
        elif template_id == "fp_halfwave_defect":
            assert spectrum["T"][center] > spectrum["T"][center-1]
            assert spectrum["T"][center] > spectrum["T"][center+1]
            qualitative = "center transmission peak above both neighboring 5 nm samples"
        results.append({"template_id": template_id, "layers": expected_count,
                        "physical_total_nm": preview["total_physical_thickness_nm"],
                        "center_R": spectrum["R"][center], "center_T": spectrum["T"][center],
                        "preview_center_R": expected_r,
                        "round_trip": "passed", "fixed_thickness_scan": "passed",
                        "qualitative_property": qualitative})
    metadata = template_catalog()
    json.dumps(metadata, allow_nan=False)
    negative_base = {"template_id": "ar_single_qw", "wavelength_nm": 550.0,
                     "role_bindings": {"F": low}, "substrate_binding": substrate,
                     "incident_binding": incident, "assume_lossless": True}
    negatives = []
    cases = []
    unknown_k = deepcopy(negative_base)
    unknown_k["assume_lossless"] = False
    cases.append(("unknown_k_without_explicit_assumption", unknown_k))
    absorbing = deepcopy(negative_base)
    absorbing["role_bindings"]["F"] = {"material_id": "builtin_research_Cu_film",
                                       "optical_set_id": "ri_cu_mcpeak"}
    cases.append(("known_absorption_cannot_be_overridden", absorbing))
    outside = deepcopy(negative_base)
    outside["wavelength_nm"] = 15000.0
    cases.append(("out_of_source_n_domain", outside))
    missing = deepcopy(negative_base)
    missing["role_bindings"]["F"]["optical_set_id"] = "not_a_dataset"
    cases.append(("unresolved_dataset_no_name_fallback", missing))
    fractional = deepcopy(negative_base)
    fractional.update(template_id="hr_hl_qw", role_bindings={"H": high, "L": low}, pairs=2.5)
    cases.append(("fractional_pair_count", fractional))
    for label, inputs in cases:
        try:
            build_preview(inputs=inputs, base_design=base, material_options=options)
        except TemplateGenerationError as error:
            negatives.append({"case": label, "result": "rejected", "error": str(error)})
        else:
            raise AssertionError(f"Generator accepted invalid input: {label}")
        assert base == base_before
    return {"templates": results, "sweep_points": 205,
            "catalog_entries": len(metadata), "maximum_absolute_residual": max_error,
            "negative_cases": negatives,
            "existing_double_face_source_unchanged": True,
            "single_face_candidate_keeps_substrate_thickness_and_records_assumptions": True,
            "scope": "Actual generator plus shared service, actual material records, explicit unknown-k lossless assumption."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" /
                        "film_design_generator_validation" / "independent-oracle.json")
    parser.add_argument("--with-generator", action="store_true")
    args = parser.parse_args()
    result = oracle_checks()
    result["real_material_oracle"] = real_material_oracle()
    if args.with_generator:
        result["production_generator"] = production_generator_checks()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                      allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "center_fixtures": len(result["center_fixtures"]),
                      "sweep_points": result["fixed_thickness_sweep_points"],
                      "real_material_sweep_points": result["real_material_oracle"]["fixed_thickness_sweep_points"],
                      "production_generator": "passed" if args.with_generator else "not_requested",
                      "max_error": result["maximum_absolute_residual"],
                      "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
