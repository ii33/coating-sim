"""Independent published-fact and fixed-thickness oracle for reference recipes.

No user designs, materials, database, or solver job are written. Numerical
constants below independently restate the cited tutorial's facts/formula.
"""
from __future__ import annotations

import argparse
import cmath
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
from verify_film_design_generator import characteristic_rta
from coat_sim.core.film_optics import coherent_rta
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.optical_constants import prepare_optical_set

SOURCE = "https://doc.comsol.com/6.4/doc/com.comsol.help.models.roptics.antireflective_coating_multilayer/antireflective_coating_multilayer.html"
TF_SOURCE = "https://www.hulinks.co.jp/en/tfcalc/examples/list-of-coating-design/growing-antireflection/"
TF_SUBSTRATE_OUTWARD = [("L", 157.14), ("H", 285.97), ("L", 563.34), ("H", 84.60),
                        ("L", 1827.50), ("H", 81.75), ("L", 2590.54), ("H", 1142.59),
                        ("L", 1234.92), ("H", 131.55), ("L", 457.49), ("H", 629.03),
                        ("L", 1076.97)]
TF_SPEC = [(role, 4.2 if role == "H" else 2.2, thick)
           for role, thick in reversed(TF_SUBSTRATE_OUTWARD)]
FIXTURES = {
    "comsol_ideal_single_ar": [("F", math.sqrt(1.5), 550.0 / (4 * math.sqrt(1.5)))],
    "comsol_qq_mgf2_cef3": [("F1", 1.38, 550.0 / (4 * 1.38)),
                            ("F2", 1.63, 550.0 / (4 * 1.63))],
    "comsol_qhq_mgf2_zro2_cef3": [("F1", 1.38, 550.0 / (4 * 1.38)),
                                 ("F2", 2.2, 550.0 / (2 * 2.2)),
                                 ("F3", 1.63, 550.0 / (4 * 1.63))],
}


def complex_characteristic_rta(wavelength_nm, indices, thickness_nm):
    """Independent field-transfer matrices for passive N=n+i*k at normal incidence.

    This deliberately does not call the production scattering recursion. The
    bounded reference fixtures avoid the thick-absorber overflow for which the
    production solver uses its more stable scattering representation.
    """
    matrix = np.eye(2, dtype=complex)
    for index, thickness in zip(indices[1:-1], thickness_nm, strict=True):
        delta = 2.0 * math.pi * index * thickness / wavelength_nm
        cosine, sine = cmath.cos(delta), cmath.sin(delta)
        matrix = matrix @ np.array([[cosine, -1j * sine / index],
                                     [-1j * index * sine, cosine]])
    n0, ns = indices[0], indices[-1]
    assert n0.imag == 0.0 and n0.real > 0.0
    field_e = matrix[0, 0] + matrix[0, 1] * ns
    field_h = matrix[1, 0] + matrix[1, 1] * ns
    denominator = n0 * field_e + field_h
    reflected = abs((n0 * field_e - field_h) / denominator) ** 2
    transmitted = abs(2.0 * n0 / denominator) ** 2 * ns.real / n0.real
    return {"R": float(reflected), "T": float(transmitted),
            "A": float(1.0 - reflected - transmitted)}


def nominal_checks():
    rows, maximum, points = [], 0.0, 0
    qq_y = 1.5 * (1.38 / 1.63) ** 2
    qq_r = ((1.0 - qq_y) / (1.0 + qq_y)) ** 2
    for identity, layers in FIXTURES.items():
        n = [1.0] + [item[1] for item in layers] + [1.5]
        d = [item[2] for item in layers]
        center = characteristic_rta(550.0, n, d)
        expected_center = 0.0 if identity == "comsol_ideal_single_ar" else qq_r
        assert abs(center["R"] - expected_center) < 2e-13
        for wavelength in np.linspace(400.0, 800.0, 81):
            oracle = characteristic_rta(float(wavelength), n, d)
            actual = coherent_rta(float(wavelength), n, d)
            error = max(abs(oracle[key] - actual[key]) for key in ("R", "T", "A"))
            maximum = max(maximum, error)
            assert error < 2e-11
            assert abs(actual["R"] + actual["T"] - 1.0) < 2e-12
            points += 1
        rows.append({"recipe_id": identity, "nominal_n0": 1.0, "nominal_ns": 1.5,
                     "lambda0_nm": 550.0, "nominal_k": 0.0,
                     "layers_incident_order": [{"role": role, "n": index, "thickness_nm": thick}
                                               for role, index, thick in layers],
                     "total_nm": sum(d), "center": center,
                     "origin": "project fixation of public single-layer formula" if identity == "comsol_ideal_single_ar"
                               else "official tutorial explicitly ordered multilayer example"})
    # The official IR table is explicitly substrate-outward. Independent
    # matrices check the reversed incident order and the dense worst sample.
    waves = np.linspace(7700.0, 12300.0, 20001)
    n = [1.0] + [index for _, index, _ in TF_SPEC] + [4.0]
    d = [thick for _, _, thick in TF_SPEC]
    assert abs(sum(d) - 10263.39) < 2e-10
    dense = coherent_rta(waves, n, d)
    worst = int(np.argmax(dense["R"]))
    checked = list(range(0, 20001, 100)) + [worst]
    for index in checked:
        matrix = characteristic_rta(float(waves[index]), n, d)
        error = max(abs(float(dense[key][index]) - matrix[key]) for key in ("R", "T", "A"))
        maximum = max(maximum, error)
        assert error < 2e-10
    assert np.max(np.abs(np.asarray(dense["R"]) + np.asarray(dense["T"]) - 1.0)) < 2e-11
    assert float(dense["R"][worst]) < 0.009
    rows.append({"recipe_id": "tfcalc_growing_ar_13", "source": TF_SOURCE,
                 "nominal_n0": 1.0, "nominal_ns": 4.0, "lambda0_nm": 10000.0,
                 "layers_substrate_outward": [{"role": role, "thickness_nm": thick}
                                              for role, thick in TF_SUBSTRATE_OUTWARD],
                 "layers_incident_order": [{"role": role, "n": index, "thickness_nm": thick}
                                           for role, index, thick in TF_SPEC],
                 "total_nm": sum(d), "dense_samples": len(waves),
                 "matrix_samples_including_dense_worst": len(checked),
                 "sampled_maximum_R": float(dense["R"][worst]),
                 "sampled_maximum_R_percent": 100.0 * float(dense["R"][worst]),
                 "worst_sample_wavelength_nm": float(waves[worst]),
                 "scope": "Published fixed table, project reproduction assumptions n0=1/normal/k=0; "
                          "discrete sample result, not a continuous-band or mapped-performance guarantee."})
    return {"source": SOURCE, "checked_date": "2026-10-11",
            "assumptions": "Transparent real constant n, k=0 project reproduction assumption, "
                           "normal incidence, semi-infinite substrate; scan range is not measured-material validity.",
            "recipes": rows, "points": points, "ir_dense_samples": len(waves),
            "maximum_absolute_residual": maximum,
            "scope": "Independent nominal-model mathematics, no measured coating performance."}


def library_checks():
    from coat_sim.core.film_design import validate_design
    from coat_sim.core.film_design_reference_recipes import (
        reference_recipe_catalog, build_reference_recipe_preview)
    catalog = reference_recipe_catalog()
    assert json.loads(json.dumps(catalog, allow_nan=False)) == catalog
    by_id = {row["stable_id"]: row for row in catalog}
    assert set(FIXTURES).issubset(by_id) and len(by_id) == len(catalog)
    service = FilmDesignService(ROOT)
    options = service.material_options_many(("film", "substrate", "medium"))
    records = {row["id"]: row for rows in options.values() for row in rows}
    low = {"material_id": "builtin_research_YbF3_film", "optical_set_id": "ri_ybf3_amotchkina_n"}
    high = {"material_id": "builtin_zns_literature_20261004", "optical_set_id": "ri_zns_amotchkina_n"}
    incident = {"material_id": "builtin_ideal_air", "optical_set_id": "ideal_air_n1_k0"}
    substrate = {"material_id": "builtin_research_fused_silica_method_demonstrator", "optical_set_id": "ri_sio2_malitson"}
    def evaluator(binding):
        row = next(item for item in records[binding["material_id"]]["optical_sets"]
                   if item["id"] == binding["optical_set_id"])
        return prepare_optical_set(row, k_sampling_policy="source")
    endpoint_evaluators = [evaluator(incident), evaluator(substrate)]
    base = service.new_design()
    base["name"] = "Preserved existing double-face source"
    base["note"] = "Keep this source note unchanged during reference preview."
    base["reference_wavelength_nm"] = 532.0
    base["substrate"]["sides"] = "double"
    base["substrate"]["thickness"] = {"value": 2.75, "unit": "mm"}
    base["back_layers"] = [{"id": "preserved-back-layer", **high,
                            "thickness": {"value": 87.0, "unit": "nm"}}]
    base = service.validate(base)
    source_before = deepcopy(base)
    results, maximum, points = [], 0.0, 0
    last_inputs = None
    for identity, spec in {**FIXTURES, "tfcalc_growing_ar_13": TF_SPEC}.items():
        entry = by_id[identity]
        is_ir = identity == "tfcalc_growing_ar_13"
        wavelength0 = 10000.0 if is_ir else 550.0
        selected_substrate = ({"material_id": "builtin_research_Ge_substrate",
                               "optical_set_id": "ri_ge_burnett2016_table1_vacuum"}
                              if is_ir else substrate)
        assert entry["source_url"] == (TF_SOURCE if is_ir else SOURCE)
        assert entry["publication_date"] is None
        assert entry["checked_date"] == "2026-10-11"
        if not is_ir:
            assert entry["version"] == "6.4"
        else:
            assert entry["version"] == "未标注（官方公开示例）"
            assert "7700–12300" in entry["original_material_band"]
        assert "\ufffd" not in json.dumps(entry, ensure_ascii=False)
        assert float(entry["lambda0_nm"]) == wavelength0
        assert len(entry["layers"]) == len(spec)
        for row, (role, nominal_n, physical) in zip(entry["layers"], spec, strict=True):
            assert row["role"] == role
            assert abs(row["nominal_n"] - nominal_n) < 2e-13
            assert row["nominal_k"] == 0.0
            assert abs(row["thickness_nm"] - physical) < 2e-12
        bindings = {role: deepcopy(low if role in ("F", "F1", "L") else high)
                    for role, _, _ in spec}
        inputs = {"recipe_id": identity, "mapping_confirmed": True,
                  "role_bindings": bindings, "incident_binding": incident,
                  "substrate_binding": selected_substrate, "assume_lossless": True}
        inputs_before = deepcopy(inputs)
        preview = build_reference_recipe_preview(inputs=inputs, base_design=base, material_options=options)
        assert inputs == inputs_before and base == source_before
        design = preview["candidate_design"]
        assert design == validate_design(json.loads(json.dumps(design, allow_nan=False)))
        assert design["reference_wavelength_nm"] == wavelength0
        assert design["layer_order"] == "substrate_outward"
        assert not design["back_layers"] and design["substrate"]["sides"] == "single"
        assert design["substrate"]["thickness"] == source_before["substrate"]["thickness"]
        assert "参考配方映射" in design["name"]
        assert (TF_SOURCE if is_ir else SOURCE) in design["note"] and identity in design["note"]
        source_layers_before = deepcopy(entry["layers"])
        for layer, mapped, (role, nominal_n, physical) in zip(
                reversed(design["front_layers"]), preview["mapped_layers_incident_order"], spec, strict=True):
            assert layer["thickness"]["unit"] == "nm"
            assert abs(layer["thickness"]["value"] - physical) < 2e-12
            assert layer["material_id"] == bindings[role]["material_id"]
            assert layer["optical_set_id"] == bindings[role]["optical_set_id"]
            assert abs(mapped["delta_n"] - (mapped["n_at_wavelength"] - nominal_n)) < 2e-13
            assert abs(mapped["fixed_thickness_nm"] - physical) < 2e-12
        assert reference_recipe_catalog()[catalog.index(entry)]["layers"] == source_layers_before
        scan_start, scan_end = (9000.0, 11000.0) if is_ir else (450.0, 650.0)
        spectrum = service.simulate(design, {"wavelength_min_nm": scan_start, "wavelength_max_nm": scan_end,
                    "samples": 41, "angle_deg": 0.0, "polarization": "unpolarized",
                    "extrapolation_policy": "error"})
        layer_evaluators = [evaluator(bindings[role]) for role, _, _ in spec]
        d = [physical for _, _, physical in spec]
        for index, wavelength in enumerate(spectrum["wavelength_nm"]):
            n = [float(endpoint_evaluators[0](float(wavelength), "nm", extrapolation_policy="error", include_k=False)[0])]
            n += [float(evaluate(float(wavelength), "nm", extrapolation_policy="error", include_k=False)[0])
                  for evaluate in layer_evaluators]
            n += [float(evaluator(selected_substrate)(float(wavelength), "nm", extrapolation_policy="error", include_k=False)[0])]
            expected = characteristic_rta(float(wavelength), n, d)
            error = max(abs(float(spectrum[key][index]) - expected[key]) for key in ("R", "T", "A"))
            maximum = max(maximum, error)
            assert error < 2e-9
            assert abs(float(spectrum["R"][index]) + float(spectrum["T"][index]) - 1.0) < 2e-10
            points += 1
        center = 20
        nominal = characteristic_rta(wavelength0, [1.0] + [n for _, n, _ in spec] + [4.0 if is_ir else 1.5], d)
        actual_center = float(spectrum["R"][center])
        assert abs(actual_center - nominal["R"]) > 1e-6
        results.append({"recipe_id": identity, "layers": len(spec), "physical_nm_preserved": True,
                        "source_immutable": True, "json_schema_round_trip": "passed",
                        "actual_bindings": bindings, "nominal_center_R": nominal["R"],
                        "mapped_actual_center_R": actual_center,
                        "original_performance_not_inherited": True})
        if not is_ir:
            last_inputs = inputs
        else:
            ir_inputs = inputs
    mutated = reference_recipe_catalog()
    mutated[0]["layers"][0]["thickness_nm"] = -1.0
    assert reference_recipe_catalog()[0]["layers"][0]["thickness_nm"] > 0
    negatives = []
    for label, update in (
        ("mapping_confirmation_required", {"mapping_confirmed": False}),
        ("unknown_reference_id", {"recipe_id": "not-a-reference-recipe"}),
        ("missing_role_mapping", {"role_bindings": {}}),
        ("unknown_k_without_separate_assumption", {"assume_lossless": False}),
        ("dataset_does_not_belong_to_material", {"role_bindings": {
            **last_inputs["role_bindings"], "F1": {**low, "optical_set_id": "unknown-dataset"}}}),
    ):
        bad = {**deepcopy(last_inputs), **update}
        try:
            build_reference_recipe_preview(inputs=bad, base_design=base, material_options=options)
        except ValueError as error:
            negatives.append({"case": label, "result": "rejected", "error": str(error)})
        else:
            raise AssertionError(f"Reference library did not reject {label}")
        assert base == source_before
    absorbing_inputs = deepcopy(ir_inputs)
    absorbing_inputs["role_bindings"] = {
        "H": {"material_id": "builtin_research_Ge_film", "optical_set_id": "ri_ge_amotchkina_film"},
        "L": deepcopy(high),
    }
    absorbing = build_reference_recipe_preview(inputs=absorbing_inputs, base_design=base,
                                               material_options=options)
    assert base == source_before and absorbing["candidate_design"]["use_k"] is True
    assert absorbing["candidate_design"] == validate_design(json.loads(
        json.dumps(absorbing["candidate_design"], allow_nan=False)))
    ge_layers = [row for row in absorbing["mapped_layers_incident_order"] if row["role"] == "H"]
    assert len(ge_layers) == 6
    for row in ge_layers:
        assert abs(row["k_at_wavelength"] - 0.00498258) < 2e-12
        assert abs(row["delta_k"] - 0.00498258) < 2e-12
    spectrum = service.simulate(absorbing["candidate_design"], {
        "wavelength_min_nm": 9000.0, "wavelength_max_nm": 11000.0, "samples": 61,
        "angle_deg": 0.0, "polarization": "unpolarized", "extrapolation_policy": "error"})
    absorbing_evaluators = [evaluator(absorbing_inputs["role_bindings"][role]) for role, _, _ in TF_SPEC]
    def complex_index(evaluate, wavelength):
        n_value, k_value, _details = evaluate(float(wavelength), "nm", extrapolation_policy="error", include_k=True)
        # Only unknown k uses the explicit candidate's assumption. Known k is
        # retained even though assume_lossless=True is needed for ZnS/Burnett.
        return complex(float(n_value), 0.0 if k_value is None else float(k_value))
    complex_maximum, absorption_values = 0.0, []
    for sample, wavelength in enumerate(spectrum["wavelength_nm"]):
        indices = [complex_index(evaluator(incident), wavelength)]
        indices += [complex_index(evaluate, wavelength) for evaluate in absorbing_evaluators]
        indices += [complex_index(evaluator(absorbing_inputs["substrate_binding"]), wavelength)]
        oracle = complex_characteristic_rta(float(wavelength), indices, [row[2] for row in TF_SPEC])
        error = max(abs(float(spectrum[key][sample]) - oracle[key]) for key in ("R", "T", "A"))
        complex_maximum = max(complex_maximum, error)
        assert error < 2e-9
        assert float(spectrum["A"][sample]) > 0.0
        assert abs(sum(float(spectrum[key][sample]) for key in ("R", "T", "A")) - 1.0) < 2e-12
        absorption_values.append(float(spectrum["A"][sample]))
    for layer, (_, _, physical) in zip(reversed(absorbing["candidate_design"]["front_layers"]), TF_SPEC, strict=True):
        assert layer["thickness"] == {"value": physical, "unit": "nm"}
    complex_result = {"recipe_id": "tfcalc_growing_ar_13", "points": 61,
        "actual_bindings": absorbing_inputs["role_bindings"], "nominal_k": 0.0,
        "selected_Ge_k_at_10000_nm": ge_layers[0]["k_at_wavelength"],
        "known_k_not_overridden_by_unknown_k_assumption": True, "fixed_nm_preserved": True,
        "maximum_absolute_residual": complex_maximum,
        "actual_absorption_min": min(absorption_values), "actual_absorption_max": max(absorption_values),
        "mapped_center_R": float(spectrum["R"][30]), "mapped_center_A": float(spectrum["A"][30]),
        "convention": "N=n+i*k, characteristic matrices -i*sin(delta); independent of scattering recursion."}
    for label, binding in (
        ("actual_dataset_cannot_cover_IR_reference", {"material_id": "builtin_research_SiO2_film",
                                                     "optical_set_id": "ri_sio2_rodriguez_de_marcos"}),
    ):
        bad = deepcopy(ir_inputs)
        bad["role_bindings"]["H"] = binding
        try:
            build_reference_recipe_preview(inputs=bad, base_design=base, material_options=options)
        except ValueError as error:
            negatives.append({"case": label, "result": "rejected", "error": str(error)})
        else:
            raise AssertionError(f"Reference library did not reject {label}")
        assert base == source_before
    return {"recipes": results, "catalog_count": len(catalog), "mapped_spectrum_points": points,
            "maximum_absolute_residual": maximum, "negative_cases": negatives,
            "real_absorbing_mapping": complex_result,
            "nominal_source_and_input_design_unchanged": True,
            "scope": "Explicit real-dataset mapping at unchanged source thickness, not measured performance."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-library", action="store_true")
    args = parser.parse_args()
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "nominal_models": nominal_checks()}
    if args.with_library:
        result["reference_library"] = library_checks()
    result["status"] = "passed"
    output = ROOT / "artifacts" / "film_design_reference_library_validation" / "independent-oracle.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"status": "passed", "nominal_points": result["nominal_models"]["points"],
                      "mapped_points": result.get("reference_library", {}).get("mapped_spectrum_points", 0),
                      "output": str(output)}))


if __name__ == "__main__":
    main()
